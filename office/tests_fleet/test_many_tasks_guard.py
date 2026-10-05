"""Many tasks per desk: the guards on an own review's branch, checkout and task.

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


class OwnSessionGuardTests(ManyCase):
    def test_a_new_review_of_a_commit_another_task_holds_is_refused_before_anything_is_made(self):
        self.commit("site")
        self.enable("moody")
        with every_slot("moody"):
            queued = review.review_own(self.conn, str(self.repo), title="site", fetch=False)
        first = queued["task_id"]
        self.assertEqual(queued["queued"], queued_text(first))
        with mock.patch.object(pensieve, "create_task", side_effect=AssertionError("made a task")):
            with self.assertRaisesRegex(FleetError, f"is already task {first}; run fleet review own --repo-dir"
                                                    f" <checkout> --task {first} to review it again"):
                review.review_own(self.conn, str(self.repo), title="site", fetch=False)
        self.assertEqual(self.own_tasks(), [first])
        self.assertEqual(self.own_tasks("closed"), [])
        done = self.own_review(first, verdict="PASS")
        self.assertEqual((done["round"], done["verdict"], done["superseded"]), (1, "PASS", [queued["request_id"]]))
        with self.assertRaisesRegex(FleetError, f"already belongs to task {first}, which is awaiting close"):
            review.review_own(self.conn, str(self.repo), title="site again", fetch=False)

    def test_leaving_out_task_never_gets_past_the_round_cap(self):
        self.commit("round one")
        capped = self.own_review()["task_id"]
        for number in (2, 3):
            self.commit(f"round {number}")
            self.assertEqual(self.own_review(capped)["round"], number)
        self.commit("round four")
        with self.assertRaisesRegex(FleetError, "review round 4"):
            self.own_review(capped)
        self.commit("one more change")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.capped_text(capped)):
            self.own_review()
        self.assertEqual(self.own_tasks(), [capped])
        # An unused allowance does not open the branch to a new task: the allowed round goes on the capped one.
        capacity.allow_round(self.conn, capped)
        with self.not_made(), self.assertRaisesRegex(FleetError, self.branch_text(capped) + "$"):
            self.own_review()
        self.assertEqual(capacity.allow_round(self.conn, capped)["created"], False)
        allowed = self.own_review(capped)
        self.assertEqual((allowed["task_id"], allowed["round"]), (capped, 4))
        # Once Ryan closes the capped task, the branch takes a new task with its own count.
        pensieve.close_task(self.conn, capped, "abandoned")
        self.commit("after the close")
        other = self.own_review()
        self.assertEqual((other["round"], other["verdict"]), (1, "CHANGES"))
        self.assertNotEqual(other["task_id"], capped)

    def test_fix_commits_on_one_branch_without_task_go_on_one_task_up_to_the_cap(self):
        # Moody round 3: a fix commit after each CHANGES, sent without --task, must never start a round-one task.
        self.on_branch("fix/site")
        self.commit("round one")
        first = self.own_review()
        self.assertEqual((first["round"], first["verdict"]), (1, "CHANGES"))
        task_id = first["task_id"]
        self.assertEqual(pensieve.get_task(self.conn, task_id)["review_branch"], "fix/site")
        for number in (2, 3):
            self.commit(f"fix after round {number - 1}")
            with self.not_made(), self.assertRaisesRegex(FleetError, self.branch_text(task_id, "fix/site") + "$"):
                self.own_review()
            self.assertEqual(self.own_review(task_id)["round"], number)
        for number in range(2):
            self.commit(f"fix after round 3, try {number}")
            with self.not_made(), self.assertRaisesRegex(FleetError, self.capped_text(task_id, "fix/site")):
                self.own_review()
        # An allowance names no cap any more, but the branch's fix still goes on its task.
        capacity.allow_round(self.conn, task_id)
        self.commit("fix after round 3, allowed")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.branch_text(task_id, "fix/site") + "$"):
            self.own_review()
        allowed = self.own_review(task_id)
        self.assertEqual(allowed["round"], 4)
        self.commit("and again")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.capped_text(task_id, "fix/site")):
            self.own_review()
        self.assertEqual(self.own_tasks(), [task_id])
        self.assertEqual(self.own_tasks("closed"), [])
        counted = [row["round"] for row in capacity.review_rounds(self.conn, task_id) if row["counts"]]
        self.assertEqual(counted, [1, 2, 3, 4])

    def test_another_branch_on_the_same_checkout_starts_its_own_task_and_count(self):
        self.on_branch("pr-one")
        self.commit("one, round one")
        one = self.own_review()["task_id"]
        for number in (2, 3):
            self.commit(f"one, round {number}")
            self.own_review(one)
        self.git("checkout", "-q", "main")
        self.on_branch("pr-two")
        self.commit("two, round one")
        two = self.own_review()
        self.assertEqual((two["round"], two["verdict"]), (1, "CHANGES"))
        self.assertNotEqual(two["task_id"], one)
        self.assertEqual(pensieve.get_task(self.conn, two["task_id"])["review_branch"], "pr-two")
        self.commit("two, round two")
        self.assertEqual(self.own_review(two["task_id"])["round"], 2)
        # Back on pr-one, a fix still goes on its capped task, and --task of the other branch's task is refused.
        self.on_branch("pr-one")
        self.commit("one, after the cap")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.capped_text(one, "pr-one")):
            self.own_review()
        with self.assertRaisesRegex(FleetError, f"branch pr-one on this checkout is task {one}, not task"
                                                f" {two['task_id']}"):
            self.own_review(two["task_id"])
        self.on_branch("pr-three")
        with self.assertRaisesRegex(FleetError, re.escape(
                f"task {two['task_id']} follows branch pr-two, which this checkout still has: check it out to go on"
                f" with that task, or leave out --task to start a new task for branch pr-three")):
            self.own_review(two["task_id"])
        self.assertEqual(sorted(self.own_tasks()), sorted([one, two["task_id"]]))
        self.assertEqual(len(capacity.review_rounds(self.conn, two["task_id"])), 2)

    def test_a_renamed_branch_keeps_its_task_and_count(self):
        self.on_branch("feat")
        self.commit("round one")
        task_id = self.own_review()["task_id"]
        self.git("branch", "-m", "feat", "feat-renamed")
        self.commit("round two")
        gone = re.escape(f"task {task_id} on this checkout follows branch feat, which this checkout no longer has,"
                         f" so it may be this same work renamed: run fleet review own --repo-dir <checkout> --task"
                         f" {task_id} to go on with it here, or close that task first")
        with self.not_made(), self.assertRaisesRegex(FleetError, gone):
            self.own_review()
        self.assertEqual(self.own_review(task_id)["round"], 2)
        self.assertEqual(pensieve.get_task(self.conn, task_id)["review_branch"], "feat-renamed")
        self.commit("round three")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.branch_text(task_id, "feat-renamed")):
            self.own_review()
        self.assertEqual(self.own_review(task_id)["round"], 3)

    def test_a_deleted_branch_holds_new_tasks_until_its_task_goes_on_or_closes(self):
        self.on_branch("spike")
        self.commit("spike")
        spike = self.own_review()["task_id"]
        self.git("checkout", "-q", "main")
        self.git("branch", "-q", "-D", "spike")
        self.on_branch("real-work")
        self.commit("real work")
        with self.not_made(), self.assertRaisesRegex(FleetError, f"task {spike} on this checkout follows branch"
                                                                 " spike, which this checkout no longer has"):
            self.own_review()
        pensieve.close_task(self.conn, spike, "abandoned")
        result = self.own_review()
        self.assertEqual((result["round"], self.own_tasks()), (1, [result["task_id"]]))
        self.assertEqual(pensieve.get_task(self.conn, result["task_id"])["review_branch"], "real-work")

    def test_a_task_from_before_branches_were_recorded_must_go_on_or_close(self):
        self.commit("old round one")
        with mock.patch.object(pensieve, "set_review_branch"):  # as a V6 row, which V7 leaves with no branch
            old = self.own_review()["task_id"]
        self.assertIsNone(pensieve.get_task(self.conn, old)["review_branch"])
        self.on_branch("new-work")
        self.commit("new work")
        with self.not_made(), self.assertRaisesRegex(FleetError, f"task {old} on this checkout names no branch"):
            self.own_review()
        self.assertEqual(self.own_review(old)["round"], 2)
        self.assertEqual(pensieve.get_task(self.conn, old)["review_branch"], "new-work")

    def test_task_never_moves_onto_a_branch_another_task_follows(self):
        self.on_branch("left")
        self.commit("left")
        left = self.own_review()["task_id"]
        self.on_branch("right")
        self.commit("right")
        right = self.own_review()["task_id"]
        self.git("branch", "-q", "-D", "left")
        self.commit("right again")
        with self.assertRaisesRegex(FleetError, f"branch right on this checkout is task {right}, not task {left}"):
            self.own_review(left)
        self.assertEqual(pensieve.get_task(self.conn, left)["review_branch"], "left")
        self.assertEqual(len(capacity.review_rounds(self.conn, left)), 1)

    def test_a_detached_head_makes_nothing(self):
        self.commit("first")
        self.git("checkout", "-q", "--detach")
        with self.not_made(), self.assertRaisesRegex(FleetError, "HEAD is detached"):
            self.own_review()
        self.assertEqual(self.own_tasks(), [])

    def test_ryans_own_branch_with_capitals_at_and_dots_records_and_goes_on(self):
        branch = "Cris-Ryan-Tan/do-the-@pr-feedback-skill.-i-think-some-of-the-rec"
        self.on_branch(branch)
        self.commit("round one")
        first = self.own_review()
        self.assertEqual((first["round"], first["verdict"]), (1, "CHANGES"))
        task_id = first["task_id"]
        self.assertEqual(pensieve.get_task(self.conn, task_id)["review_branch"], branch)
        self.commit("fix after round one")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.branch_text(task_id, branch) + "$"):
            self.own_review()
        self.assertEqual(self.own_review(task_id)["round"], 2)
        # Renamed to another name of Ryan's own, the task moves with --task and keeps its count.
        renamed = "Cris-Ryan-Tan/PR-1234.Feedback@v2"
        self.git("branch", "-m", branch, renamed)
        self.commit("fix after round two")
        with self.not_made(), self.assertRaisesRegex(FleetError, re.escape(
                f"task {task_id} on this checkout follows branch {branch}, which this checkout no longer has")):
            self.own_review()
        self.assertEqual(self.own_review(task_id)["round"], 3)
        self.assertEqual(pensieve.get_task(self.conn, task_id)["review_branch"], renamed)
        # A fleet word in Ryan's own branch is fine too: the name is only compared, never pushed.
        self.git("checkout", "-q", "main")
        self.on_branch("Fix/Moody-Notes")
        self.commit("another PR")
        other = self.own_review()
        self.assertEqual((other["round"], pensieve.get_task(self.conn, other["task_id"])["review_branch"]),
                         (1, "Fix/Moody-Notes"))
        self.assertEqual(sorted(self.own_tasks()), sorted([task_id, other["task_id"]]))

    def test_a_branch_name_git_or_the_store_refuses_makes_nothing(self):
        self.commit("first")
        refusal = re.escape("your checkout's branch cannot name a review: a review branch is a name git takes as"
                            " a branch, in 1 to 255 bytes of printable ASCII with no whitespace")
        for name in ("two words", "main\t", "fix/\x1b[31m", "x" * 256, "a..b", "fix.lock"):
            with self.subTest(branch=name), mock.patch.object(gitops, "current_branch", return_value=name):
                with self.not_made(), self.assertRaisesRegex(FleetError, refusal + "$") as caught:
                    self.own_review()
                self.assertNotIn(name, str(caught.exception))
        self.assertEqual(self.own_tasks(), [])

    def test_git_itself_refuses_a_name_even_past_the_store_rule(self):
        self.commit("first")
        self.on_branch("previous")
        self.git("checkout", "-q", "main")
        common = str(self.repo / ".git")
        with mock.patch.object(pensieve, "check_review_branch", side_effect=lambda name: name):
            for name in ("a..b", "fix.lock", "-x", "HEAD", "a b"):
                with self.subTest(branch=name), self.assertRaisesRegex(FleetError, "git does not take it"):
                    gitops.check_lineage_branch(common, name)
            # --branch would read @{-1} as the branch checked out before, so it must come back unchanged.
            self.assertEqual(self.git("check-ref-format", "--branch", "@{-1}"), "previous")
            with self.assertRaisesRegex(FleetError, "shorthand for another branch"):
                gitops.check_lineage_branch(common, "@{-1}")
            with mock.patch.object(gitops, "current_branch", return_value="x.lock"), self.not_made():
                with self.assertRaisesRegex(FleetError, "cannot name a review: git does not take it"):
                    self.own_review()
        self.assertEqual(gitops.check_lineage_branch(common, "Fix-Upper@v1.2"), "Fix-Upper@v1.2")

    def test_the_store_rule_takes_exactly_the_printable_names_git_takes(self):
        common = str(self.repo / ".git")
        names = ("Cris-Ryan-Tan/do-the-@pr-feedback-skill.-i-think-some-of-the-rec", "Fix", "@", "a@b", "x/HEAD",
                 "fix/moody-notes", "!#$%&'()+,;<=>`{|}\"", "a..b", "x.lock", "x.lock/y", "a.lockx", "-x", "x-",
                 "HEAD", "/x", "x/", "x.", "a//b", ".x", "x/.y", "x./y", "a@{b", "@{-1}", "a~b", "a^b", "a:b",
                 "a?b", "a*b", "a[b", "a]b", "a\\b", "a{b}", "x" * 255)
        for name in names:
            with self.subTest(branch=name):
                done = subprocess.run([config.GIT_BIN, "check-ref-format", "--branch", name], cwd=self.repo,
                                      capture_output=True, env={"HOME": str(self.home_dir), "PATH": config.CHILD_PATH})
                git_takes = done.returncode == 0 and done.stdout.decode() == name + "\n"
                try:
                    pensieve.check_review_branch(name)
                    store_takes = True
                except ValidationError:
                    store_takes = False
                self.assertEqual(store_takes, git_takes)
                if git_takes:
                    self.assertEqual(gitops.check_lineage_branch(common, name), name)

    def test_the_rule_for_branches_the_fleet_makes_is_unchanged(self):
        self.assertEqual(gitops.check_branch("fix/site"), "fix/site")
        for name in ("Fix-Upper", "fix/Site", "a@b", "a..b", "fix.lock", "x" * 101, "fix/"):
            with self.subTest(branch=name), self.assertRaisesRegex(FleetError, "branch names use lowercase"):
                gitops.check_branch(name)
        with self.assertRaisesRegex(FleetError, r"fleet word \(moody\)"):
            gitops.check_branch("fix/moody-notes")
        task, _ = self.harry_request(self.queued_parent())
        with mock.patch.object(run_desk, "spawn"), self.assertRaisesRegex(FleetError, "branch names use lowercase"):
            worktree.create(self.conn, task["id"], str(self.repo), "Fix-Upper", fetch=False)
        self.assert_taken_back(task, "Fix-Upper")

    def test_a_review_starting_on_the_lineage_lock_is_refused_once_the_wait_runs_out(self):
        self.commit("first")
        self.enable("moody")
        with review.own_lineage_lock(), mock.patch.object(review, "OWN_LINEAGE_WAIT_SECONDS", 0), self.not_made():
            with self.assertRaisesRegex(FleetError, "another review of your own sessions is still starting"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(self.own_tasks(), [])

    def test_a_branch_named_in_other_letter_case_of_its_checkout_is_still_that_checkout(self):
        upper = self.home_dir / "REPO"
        if not upper.is_dir():
            self.skipTest("this disk tells letter case apart, so REPO is a different folder")
        self.commit("round one")
        capped = self.own_review()["task_id"]
        for number in (2, 3):
            self.commit(f"round {number}")
            self.own_review(capped)
        self.commit("try the other spelling")
        self.enable("moody")
        with self.not_made(), self.assertRaisesRegex(FleetError, self.capped_text(capped)):
            review.review_own(self.conn, str(upper), title="my own fix", fetch=False)
        self.assertEqual(self.own_tasks(), [capped])
        capacity.allow_round(self.conn, capped)
        with self.fake_reviewer("CHANGES"):
            allowed = review.review_own(self.conn, str(upper), task_id=capped, fetch=False)
        self.assertEqual((allowed["task_id"], allowed["round"]), (capped, 4))

    def test_a_task_on_another_checkout_does_not_take_this_ones_reviews(self):
        self.commit("round one")
        task_id = self.own_review()["task_id"]
        other = self.home_dir / "other"
        self.git("clone", "-q", str(self.repo), str(other))
        self.enable("moody")
        with self.assertRaisesRegex(FleetError, "that task's worktree is for a different checkout"):
            review.review_own(self.conn, str(other), task_id=task_id, fetch=False)

    @staticmethod
    def branch_text(task_id: str, branch: str = "main") -> str:
        return re.escape(f"branch {branch} on this checkout is task {task_id}, so its fix commits go on that task:"
                         f" run fleet review own --repo-dir <checkout> --task {task_id}")

    def capped_text(self, task_id: str, branch: str = "main") -> str:
        return self.branch_text(task_id, branch) + re.escape(
            f" once castle task allow-round {task_id} allows one more round, since it has used its 3 review rounds,"
            " or close that task first")

    def test_a_task_interrupted_after_its_commit_is_recorded_stays_retryable(self):
        sha = self.commit("first")
        real_record = pensieve.record_commit

        def interrupted(*args, **kwargs):
            real_record(*args, **kwargs)
            raise KeyboardInterrupt  # killed between recording the commit and opening the first round

        self.enable("moody")
        with mock.patch.object(pensieve, "record_commit", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        [task_id] = self.own_tasks()
        self.assertEqual(self.own_tasks("closed"), [])
        self.assertEqual(capacity.review_rounds(self.conn, task_id), [])
        self.assertEqual([row["sha"] for row in pensieve.task_commits(self.conn, task_id)], [sha])
        with mock.patch.object(pensieve, "create_task", side_effect=AssertionError("made a task")):
            with self.assertRaisesRegex(FleetError, f"is already task {task_id}; run fleet review own --repo-dir"
                                                    f" <checkout> --task {task_id} to review it again"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        retried = self.own_review(task_id)
        self.assertEqual((retried["task_id"], retried["sha"], retried["round"], retried["verdict"]),
                         (task_id, sha, 1, "CHANGES"))

    def test_a_new_task_that_fails_before_its_first_round_is_closed(self):
        self.commit("first")
        with mock.patch.object(review.verify, "verify", side_effect=FleetError("verify broke")):
            with self.assertRaisesRegex(FleetError, "verify broke"):
                self.own_review()
        self.assertEqual(self.own_tasks(), [])
        [closed] = self.own_tasks("closed")
        self.assertEqual(pensieve.get_task(self.conn, closed)["close_reason"], "abandoned")
        self.assertEqual(self.own_review()["round"], 1)
