"""Many tasks per desk: an own review's lineage, read from real git history.

Split from test_many_tasks.py so the parallel runner can run it beside the rest; it shares ManyCase.
"""
from __future__ import annotations

import json
import os
import contextlib
import io
import re
import shlex
import stat
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, cli, db, ids, owlery, pensieve
from hogwarts.errors import ConflictError, ValidationError
from tests.support import NOW, temp_dir

from fleet import config, gitops, owl_post, push, review, run_desk, verify, worktree
from fleet.hooks import pre_compact, session_start
from fleet.safefs import FleetError
from tests_fleet.support import IN_KIT, MANY_TASK_DESKS, ONLY_IN_KIT, every_slot, fake_children
from tests_fleet.test_review_loop import HANDOFF, ORIGIN, TASK_MD
from tests_fleet.test_review_rounds import queued_text
from tests_fleet.test_many_tasks import KIT, ManyCase


class OwnLineageAncestryTests(ManyCase):
    """Moody round 4: a review's lineage is also its commits. Work built on an open own task's recorded commits
    goes on that task, whatever branch or clone it was made on."""

    def capped(self, branch: str = "fix/site") -> str:
        """An own task on branch with its three rounds used, each CHANGES."""
        self.on_branch(branch)
        self.commit("round one")
        task_id = self.own_review()["task_id"]
        for number in (2, 3):
            self.commit(f"round {number}")
            self.assertEqual(self.own_review(task_id)["round"], number)
        return task_id

    def lineage_text(self, task_id: str, sha: str) -> str:
        return re.escape(f"HEAD builds on commit {sha[:12]} of task {task_id}, so it is that task's work and goes"
                         f" on its round count: run fleet review own --repo-dir <checkout> --task {task_id}")

    def clone(self, name: str, *args: str) -> Path:
        """Another clone of Ryan's repository in his home, with the same origin."""
        folder = self.home_dir / name
        self.git("clone", "-q", *args, f"file://{self.repo}", str(folder))
        self.git("config", "remote.origin.url", ORIGIN, cwd=folder)
        return folder

    def review_in(self, folder: Path, task_id: str = None) -> dict:
        self.enable("moody")
        with self.fake_reviewer("CHANGES"):
            if task_id is None:
                return review.review_own(self.conn, str(folder), title="from a clone", fetch=False)
            return review.review_own(self.conn, str(folder), task_id=task_id, fetch=False)

    def test_a_branch_made_off_a_capped_branch_goes_on_its_task_and_cap(self):
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        # Moody's sequence: a new branch from the capped one, the capped branch kept, and a fix commit.
        self.stacked("fix/site-retry")
        self.commit("fix after the cap")
        self.assertEqual(self.git("branch", "--list", "fix/site"), "fix/site")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last) + re.escape(
                f" once castle task allow-round {capped} allows one more round, since it has used its 3 review"
                " rounds, or close that task first") + "$"):
            self.own_review()
        with self.assertRaisesRegex(FleetError, "review round 4"):
            self.own_review(capped)
        capacity.allow_round(self.conn, capped)
        allowed = self.own_review(capped)
        self.assertEqual((allowed["task_id"], allowed["round"]), (capped, 4))
        self.assertEqual(pensieve.get_task(self.conn, capped)["review_branch"], "fix/site-retry")
        # The old branch is that task's work too, and so is a rename of the new one.
        self.on_branch("fix/site")
        self.commit("back on the old branch")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last)):
            self.own_review()
        self.git("checkout", "-q", "fix/site-retry")
        self.git("branch", "-m", "fix/site-retry", "fix/site-third")
        self.commit("renamed again")
        with self.not_made(), self.assertRaisesRegex(FleetError, f"task {capped} on this checkout follows branch"
                                                                 " fix/site-retry, which this checkout no longer"):
            self.own_review()
        self.assertEqual(self.own_tasks(), [capped])
        self.assertEqual([row["round"] for row in capacity.review_rounds(self.conn, capped) if row["counts"]],
                         [1, 2, 3, 4])

    def test_task_of_another_task_never_takes_work_built_on_a_capped_one(self):
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        self.on_branch("spike")
        self.commit("spike")
        spike = self.own_review()["task_id"]
        self.git("checkout", "-q", "fix/site")
        self.git("branch", "-q", "-D", "spike")
        self.git("branch", "-m", "fix/site", "fix/site-renamed")
        self.commit("the capped fix under the spike's count")
        with self.assertRaisesRegex(FleetError, re.escape(
                f"HEAD builds on commit {last[:12]} of task {capped}, not task {spike}, so it goes on that task's"
                f" round count: run fleet review own --repo-dir <checkout> --task {capped} once castle task"
                f" allow-round {capped} allows one more round")):
            self.own_review(spike)
        self.assertEqual(pensieve.get_task(self.conn, spike)["review_branch"], "spike")
        self.assertEqual(len(capacity.review_rounds(self.conn, spike)), 1)

    def test_task_does_not_move_to_a_branch_that_does_not_build_on_it(self):
        self.on_branch("pr-one")
        self.commit("one")
        one = self.own_review()["task_id"]
        self.on_branch("pr-two")
        self.commit("two")
        with self.assertRaisesRegex(FleetError, f"task {one} follows branch pr-one, which this checkout still has"):
            self.own_review(one)
        self.assertEqual(pensieve.get_task(self.conn, one)["review_branch"], "pr-one")

    def test_a_second_clone_branching_off_an_open_task_is_refused(self):
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        other = self.clone("second")
        self.git("checkout", "-q", "-b", "fix/site-again", "origin/fix/site", cwd=other)
        self.write_file(other / "fix.txt", "from the second clone\n")
        self.git("commit", "-q", "-am", "from the second clone", cwd=other)
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last) + ".*" + re.escape(
                f"(that task's worktree is for the checkout {self.repo}, so bring this commit there and run it"
                " from that checkout)")):
            self.review_in(other)
        with self.assertRaisesRegex(FleetError, "that task's worktree is for a different checkout"):
            self.review_in(other, capped)
        self.assertEqual(self.own_tasks(), [capped])

    def test_an_unrelated_branch_from_main_starts_its_own_task(self):
        capped = self.capped()
        self.on_branch("another-pr")
        self.commit("unrelated work")
        result = self.own_review()
        self.assertEqual((result["round"], result["verdict"]), (1, "CHANGES"))
        self.assertEqual(sorted(self.own_tasks()), sorted([capped, result["task_id"]]))

    def test_a_branch_stacked_on_an_open_task_waits_until_it_passes(self):
        self.on_branch("base-pr")
        first = self.commit("base work")
        base = self.own_review()["task_id"]
        self.stacked("stacked-pr")
        self.commit("stacked work")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(base, first) + "$"):
            self.own_review()
        self.on_branch("base-pr")
        self.commit("base fixed")
        self.assertEqual(self.own_review(base, verdict="PASS")["verdict"], "PASS")
        self.assertEqual(pensieve.get_task(self.conn, base)["status"], "awaiting_close")
        self.on_branch("stacked-pr")
        stacked = self.own_review()
        self.assertEqual((stacked["round"], self.own_tasks()), (1, [stacked["task_id"]]))

    def test_a_full_clone_without_the_tasks_commits_starts_its_own_task(self):
        # A full clone keeps every commit its branches reach, so a recorded commit it lacks is no ancestor.
        capped = self.capped()
        other = self.clone("main-only", "--single-branch", "--branch", "main")
        last = self.git("rev-parse", "HEAD")
        self.assertNotEqual(subprocess.run([config.GIT_BIN, "cat-file", "-e", last], cwd=other).returncode, 0)
        self.git("checkout", "-q", "-b", "elsewhere", cwd=other)
        self.write_file(other / "other.txt", "other work\n")
        self.git("add", "other.txt", cwd=other)
        self.git("commit", "-q", "-m", "other work", cwd=other)
        result = self.review_in(other)
        self.assertEqual(result["round"], 1)
        self.assertEqual(sorted(self.own_tasks()), sorted([capped, result["task_id"]]))

    def test_a_shallow_clone_that_cannot_tell_is_refused(self):
        # A shallow clone's history may stop short of a recorded commit, so the review refuses rather than guess.
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        other = self.clone("shallow", "--depth", "1", "--branch", "main")
        self.git("checkout", "-q", "-b", "elsewhere", cwd=other)
        self.write_file(other / "other.txt", "other work\n")
        self.git("add", "other.txt", cwd=other)
        self.git("commit", "-q", "-m", "other work", cwd=other)
        with self.not_made(), self.assertRaisesRegex(FleetError, re.escape(
                f"this checkout is shallow, so the review cannot tell whether HEAD builds on commit {last[:12]}"
                f" of task {capped}: fetch its full history (git fetch --unshallow) and run this again")):
            self.review_in(other)
        self.assertEqual(self.own_tasks(), [capped])
        common = str(other / ".git")
        head = self.git("rev-parse", "HEAD", cwd=other)
        self.assertTrue(gitops.is_shallow(common))
        self.assertIsNone(gitops.is_ancestor(common, last, head))
        self.assertTrue(gitops.is_ancestor(common, head, head))

    def test_an_origin_url_in_another_letter_case_is_still_that_repository(self):
        # GitHub takes a slug in any letter case, so an origin respelled in capitals is the same repository.
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        self.git("config", "remote.origin.url", ORIGIN.replace("acme/web-app", "Acme/Web-App"))
        self.stacked("fix/site-same")
        with self.not_made(), self.assertRaisesRegex(FleetError, re.escape(
                f"HEAD {last[:12]} is already task {capped}; run fleet review own --repo-dir <checkout> --task"
                f" {capped} to review it again")):
            self.own_review()
        self.stacked("fix/site-retry")
        self.commit("fix after the cap")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last)):
            self.own_review()
        self.assertEqual(self.own_tasks(), [capped])

    def test_replace_refs_and_grafts_do_not_hide_a_tasks_commits(self):
        # Ancestry is read from the parents the commits really name, which is what a push sends.
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        self.stacked("fix/site-retry")
        head = self.commit("fix after the cap")
        main = self.git("rev-parse", "origin/main")
        common = str(self.repo / ".git")
        self.git("replace", "--graft", head, main)
        self.assertNotEqual(subprocess.run([config.GIT_BIN, "merge-base", "--is-ancestor", last, head],
                                           cwd=self.repo).returncode, 0)
        self.assertTrue(gitops.is_ancestor(common, last, head))
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last)):
            self.own_review()
        self.git("replace", "-d", head)
        self.write_file(self.repo / ".git" / "info" / "grafts", f"{head} {main}\n")
        self.assertTrue(gitops.is_ancestor(common, last, head))
        with self.not_made(), self.assertRaisesRegex(FleetError, self.lineage_text(capped, last)):
            self.own_review()
        self.assertEqual(self.own_tasks(), [capped])

    def test_a_clone_cut_short_with_its_shallow_file_removed_is_refused(self):
        # Without .git/shallow a cut clone says it is full, but HEAD's missing parents still give it away.
        capped = self.capped()
        last = self.git("rev-parse", "HEAD")
        self.stacked("fix/site-again")
        self.commit("built on the capped fix")
        other = self.clone("cut", "--depth", "1", "--branch", "fix/site-again")
        (other / ".git" / "shallow").unlink()
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", cwd=other), "false")
        self.git("checkout", "-q", "-b", "elsewhere", cwd=other)
        self.write_file(other / "other.txt", "other work\n")
        self.git("add", "other.txt", cwd=other)
        self.git("commit", "-q", "-m", "other work", cwd=other)
        head = self.git("rev-parse", "HEAD", cwd=other)
        self.assertIsNone(gitops.is_ancestor(str(other / ".git"), last, head))
        self.assertFalse(gitops.is_shallow(str(other / ".git")))
        with self.not_made(), self.assertRaises(FleetError) as caught:
            self.review_in(other)
        # Git refuses --unshallow on a full clone, so this one is not told to run it.
        text = str(caught.exception)
        self.assertIn(f"cannot tell whether HEAD builds on commit {last[:12]} of task {capped}", text)
        self.assertIn("this checkout's history is incomplete or unreadable", text)
        self.assertIn("git fetch origin", text)
        self.assertNotIn("unshallow", text)
        self.assertEqual(self.own_tasks(), [capped])

    def test_a_new_tasks_commit_is_its_lineage_before_its_checks_run(self):
        # A review on a branch stacked on a commit whose own review is still running its checks goes on that task.
        self.on_branch("fix/a")
        first = self.commit("a work")
        real_verify, refused = verify.verify, []

        def stacked_meanwhile(conn, task_id, **kwargs):
            if not refused:
                self.stacked("fix/a-more")
                self.commit("more on a")
                with self.assertRaises(FleetError) as caught:
                    review.review_own(self.conn, str(self.repo), title="stacked", fetch=False)
                refused.append(str(caught.exception))
            return real_verify(conn, task_id, **kwargs)

        with mock.patch.object(verify, "verify", side_effect=stacked_meanwhile):
            task_id = self.own_review()["task_id"]
        self.assertEqual(len(refused), 1)
        self.assertRegex(refused[0], self.lineage_text(task_id, first))
        self.assertEqual(self.own_tasks(), [task_id])
