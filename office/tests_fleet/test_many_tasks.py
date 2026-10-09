"""Many tasks per desk: Harry, Hermione, Moody, Ron and Ryan's own sessions each hold many tasks in flight.

Only running model processes stay bounded per desk (its run slots). A task waiting for fixes blocks
nothing, a reviewer is busy only while every run slot of it is held, and each pad desk keeps one pad per task.
Reviewer and desk runs are faked at run_desk.run or run_desk.start_child; no model ever runs.
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
import time
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, cli, db, ids, owlery, pensieve
from hogwarts.errors import ConflictError, ValidationError
from tests.support import NOW, temp_dir

from fleet import config, gitops, owl_post, push, review, run_desk, verify, worktree
from fleet.hooks import pre_compact, session_start, user_prompt_submit
from fleet.safefs import FleetError
from tests_fleet.support import IN_KIT, MANY_TASK_DESKS, ONLY_IN_KIT, every_slot, fake_children
from tests_fleet.test_hooks import HookCase
from tests_fleet.test_push import GateCase
from tests_fleet.test_review_loop import HANDOFF, ORIGIN, TASK_MD, LoopCase
from tests_fleet.test_review_rounds import queued_text
from tests_fleet.test_run_desk import RunDeskCase

KIT = Path(__file__).resolve().parents[2]


class ManyCase(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        clock = mock.patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.owls = 0

    def on_branch(self, name: str) -> None:
        """Check out branch name in Ryan's checkout, making it from the base (origin/main) when it is new, as a
        separate PR is. A branch built on another task's commits is stacked() instead."""
        if self.git("branch", "--list", name):
            self.git("checkout", "-q", name)
        else:
            self.git("checkout", "-q", "-b", name, "origin/main")

    def stacked(self, name: str) -> None:
        """Make branch name from HEAD and check it out, keeping the branch it was made from."""
        self.git("checkout", "-q", "-b", name)

    def commit(self, text: str) -> str:
        self.write_file(self.repo / "fix.txt", text + "\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def own_review(self, task_id: str = None, verdict: str = "CHANGES") -> dict:
        self.enable("moody")
        with self.fake_reviewer(verdict):
            if task_id is None:
                return review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
            return review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)

    def queued_parent(self, title: str = "add the widget check") -> str:
        """McGonagall's registered task with its TASK.md. It stays queued, as castle task create leaves it."""
        task_id = ids.new_id("task")
        folder = self.castle / "tasks" / task_id
        folder.mkdir(mode=0o700)
        self.write_file(folder / "TASK.md", TASK_MD.format(task_id=task_id))
        pensieve.create_task(self.conn, "mcgonagall", title, intent_path=f"{ids.TASKS_ROOT}/{task_id}/TASK.md",
                             task_id=task_id)
        return task_id

    def harry_request(self, parent: str, subject: str = "build it") -> tuple:
        """McGonagall's build request to Harry under parent: (Harry's task, the request owl id)."""
        self.enable("harry")
        self.owls += 1
        self.write_owl("mcgonagall", f"build-{self.owls}.json", {"to": "harry", "kind": "request", "subject": subject,
                                                                 "body": "see TASK.md", "task_id": parent})
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned too early")):
            [delivered] = owl_post.run_pass(self.conn)["delivered"]
        owl = next(item for item in owlery.inbox(self.conn, "harry") if item["id"] == delivered["owl_id"])
        return pensieve.get_task(self.conn, owlery.get_request(self.conn, owl["request_id"])["task_id"]), owl["id"]

    def built(self, branch: str, parent: str = None) -> tuple:
        """A Harry task with its worktree, started: (task, created, spawn mock)."""
        task, _ = self.harry_request(parent or self.queued_parent())
        with mock.patch.object(run_desk, "spawn") as spawn:
            created = worktree.create(self.conn, task["id"], str(self.repo), branch, fetch=False)
        return pensieve.get_task(self.conn, task["id"]), created, spawn

    def harry_handoff(self, task: dict) -> None:
        self.owls += 1
        name = f"handoff-{task['id']}-r{self.owls}.md"
        self.write_file(self.outbox("harry") / name, HANDOFF.format(task_id=task["id"]))
        self.write_owl("harry", f"{task['id']}-r{self.owls}.json", {
            "to": "mcgonagall", "kind": "result", "subject": "ready", "task_id": task["id"],
            "request_id": task["request_id"], "body_path": self.outbox_path("harry", name)})
        owl_post.run_pass(self.conn)

    def build_review(self, task: dict, created: dict, verdict: str, text: str) -> dict:
        self.write_file(Path(created["worktree"]) / "widget.txt", text + "\n")
        self.harry_handoff(task)
        self.enable("hermione")
        with self.fake_reviewer(verdict):
            return review.review_build(self.conn, task["id"])

    def race(self, first: dict, second: dict) -> dict:
        """Two worktree commands, each on its own connection and thread. The first pauses
        inside add_worktree, after its holder check, while the second runs start to finish. Each outcome is
        the command's result or its FleetError."""
        real_add, inside, release, outcomes = worktree.add_worktree, threading.Event(), threading.Event(), {}

        def paused_add(conn, task_id, *args, **kwargs):
            if task_id == first["id"]:
                inside.set()
                release.wait(10)
            return real_add(conn, task_id, *args, **kwargs)

        def run(task: dict, branch: str) -> None:
            conn = db.connect(self.db_path)
            try:
                outcomes[task["id"]] = worktree.create(conn, task["id"], str(self.repo), branch, fetch=False)
            except FleetError as exc:
                outcomes[task["id"]] = exc
            finally:
                conn.close()

        with mock.patch.object(worktree, "add_worktree", side_effect=paused_add), mock.patch.object(run_desk, "spawn"):
            one = threading.Thread(target=run, args=(first, "fix/widget"))
            one.start()
            try:
                self.assertTrue(inside.wait(10))
                two = threading.Thread(target=run, args=(second, "fix/other"))
                two.start()
                two.join(10)
            finally:
                release.set()
                one.join(10)
        self.assertEqual(set(outcomes), {first["id"], second["id"]})
        return outcomes

    def harry_active(self) -> list:
        return [task["id"] for task in pensieve.list_tasks(self.conn, desk="harry", status="active")]

    def assert_taken_back(self, task: dict, branch: str) -> None:
        """A refused worktree command left its task queued with nothing on disk: no worktree, branch or record."""
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "queued")
        self.assertIsNone(pensieve.get_task(self.conn, task["id"])["worktree"])
        self.assertFalse(os.path.lexists(config.worktree_dir(task["id"])))
        self.assertFalse(os.path.lexists(self.office / "worktrees" / f"{task['id']}.json"))
        self.assertEqual(self.git("branch", "--list", branch), "")
        self.assertNotIn(task["id"], self.git("worktree", "list"))

    def own_tasks(self, status: str = "active") -> list:
        return [task["id"] for task in pensieve.list_tasks(self.conn, desk="ryan-claude-1", status=status)]

    @staticmethod
    def not_made():
        return mock.patch.object(pensieve, "create_task", side_effect=AssertionError("made a task"))

    def recovered(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.recovered"]


class OwnSessionTests(ManyCase):
    def test_a_task_parked_in_changes_blocks_no_new_own_review_on_another_branch(self):
        # Ryan's case: a website review sits in CHANGES on ryan-claude-1 while four more PRs come in.
        self.on_branch("website")
        self.commit("website first try")
        parked = self.own_review()
        self.assertEqual((parked["round"], parked["verdict"]), (1, "CHANGES"))
        others = []
        for number in range(4):
            self.on_branch(f"other-{number}")
            self.commit(f"other change {number}")
            result = self.own_review()
            self.assertEqual((result["round"], result["verdict"], result["queued"]), (1, "CHANGES", None))
            others.append(result["task_id"])
        self.assertEqual(len(set(others)), 4)
        self.assertEqual(sorted(self.own_tasks()), sorted([parked["task_id"], *others]))
        self.on_branch("website")
        self.commit("website fixed")
        fixed = self.own_review(parked["task_id"], verdict="PASS")
        self.assertEqual((fixed["round"], fixed["verdict"]), (2, "PASS"))
        self.assertEqual(pensieve.get_task(self.conn, parked["task_id"])["status"], "awaiting_close")
        self.assertEqual(sorted(self.own_tasks()), sorted(others))

    def test_the_review_base_is_the_commit_the_task_started_from(self):
        # Ryan's case: the change merged to main between rounds, and a base kept as the name main then showed
        # the reviewer an empty diff.
        self.on_branch("speedup")
        start = self.git("rev-parse", "origin/main")
        self.commit("first try")
        first = self.own_review()
        record = gitops.read_record(first["task_id"])
        self.assertEqual((record["base"], record["base_ref"]), (start, "origin/main"))
        self.commit("second try")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")  # the branch lands on main
        again = self.own_review(first["task_id"])
        self.assertEqual(again["round"], 2)
        self.assertEqual(gitops.read_record(first["task_id"])["base"], start)
        diff = self.git("-C", record["path"], "diff", "--name-only", f"{record['base']}...HEAD")
        self.assertIn("fix.txt", diff)

    def test_a_store_that_keeps_own_sessions_single_names_the_fix(self):
        self.commit("first")
        self.own_review()
        self.on_branch("second")
        self.commit("second")
        with mock.patch.object(pensieve, "start_task", side_effect=ConflictError("desk already has an active task")):
            with self.assertRaisesRegex(FleetError, "castle desk many-tasks ryan-claude-1"):
                self.own_review()
        [abandoned] = self.own_tasks("closed")
        self.assertEqual(pensieve.get_task(self.conn, abandoned)["close_reason"], "abandoned")


class ParallelAuthorTests(ManyCase):
    def test_parallel_authors_keep_their_own_queue_rounds_and_locks(self):
        self.on_branch("a")
        self.commit("a one")
        first = self.own_review()
        self.on_branch("b")
        self.commit("b one")
        second = self.own_review()
        a, b = first["task_id"], second["task_id"]
        not_run = mock.patch.object(run_desk, "run", side_effect=AssertionError("ran while moody was busy"))
        self.on_branch("a")
        self.commit("a two")
        with every_slot("moody"), not_run:
            queued_a = review.review_own(self.conn, str(self.repo), task_id=a, fetch=False)
        self.on_branch("b")
        self.commit("b two")
        with every_slot("moody"), not_run:
            queued_b = review.review_own(self.conn, str(self.repo), task_id=b, fetch=False)
        self.assertEqual((queued_a["round"], queued_b["round"], queued_b["superseded"]), (2, 2, []))
        self.assertTrue(capacity.review_rounds(self.conn, a)[-1]["waiting"])
        # A second review of A while one runs is refused by A's review lock; B only queues on the desk lock.
        with review.task_review_lock(a), every_slot("moody"), not_run:
            with self.assertRaisesRegex(FleetError, review.REVIEW_RUNNING):
                review.review_own(self.conn, str(self.repo), task_id=a, fetch=False)
            self.on_branch("b")
            self.commit("b three")
            queued_b2 = review.review_own(self.conn, str(self.repo), task_id=b, fetch=False)
        self.assertEqual(queued_b2["superseded"], [queued_b["request_id"]])
        self.assertTrue(capacity.review_rounds(self.conn, a)[-1]["waiting"])
        self.on_branch("a")
        self.commit("a three")
        done_a = self.own_review(a)
        self.assertEqual((done_a["round"], done_a["superseded"]), (2, [queued_a["request_id"]]))
        self.assertTrue(capacity.review_rounds(self.conn, b)[-1]["waiting"])
        self.on_branch("a")
        self.commit("a four")
        self.assertEqual(self.own_review(a)["round"], 3)
        self.on_branch("a")
        self.commit("a five")
        with self.assertRaisesRegex(FleetError, "review round 4"):
            self.own_review(a)
        self.on_branch("b")
        self.commit("b four")
        self.assertEqual(self.own_review(b)["round"], 2)


class ReviewerBusyTests(ManyCase):
    def test_a_stranded_round_is_closed_but_the_reviewers_other_task_is_not(self):
        other = pensieve.create_task(self.conn, "moody", "a task of moody's own")
        pensieve.start_task(self.conn, other["id"])
        self.commit("first try")
        with mock.patch.object(review, "_finish_reviewer_task"):  # killed before its cleanup
            def failed(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
                on_start()
                return {"desk": desk, "run_id": "run-" + "c" * 16, "exit_code": 1, "cap_source": None}
            self.enable("moody")
            with mock.patch.object(run_desk, "run", side_effect=failed), self.assertRaises(FleetError):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        [task_id] = self.own_tasks()
        stranded = [row["reviewer_task_id"] for row in capacity.stranded_rounds(self.conn, "moody")]
        self.assertEqual(len(stranded), 1)
        self.commit("second try")
        result = self.own_review(task_id)
        self.assertEqual((result["round"], result["verdict"]), (1, "CHANGES"))
        self.assertEqual(pensieve.get_task(self.conn, stranded[0])["close_reason"], "superseded")
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "active")
        [event] = self.recovered()
        self.assertIn(stranded[0], event["summary"])


class FailedReviewTests(ManyCase):
    def crashed_review(self, task_id: str = None) -> None:
        """A reviewer run that starts and exits with no verdict: run_review's own cleanup closes its task."""
        def failed(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            on_start()
            return {"desk": desk, "run_id": "run-" + "d" * 16, "exit_code": 1, "cap_source": None}

        self.enable("moody")
        with mock.patch.object(run_desk, "run", side_effect=failed), self.assertRaises(FleetError):
            if task_id is None:
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
            else:
                review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)

    def board(self, task_id: str) -> dict:
        found = capacity.in_flight(self.conn, NOW, config.RUNNING_WINDOW_SECONDS, max_rounds=config.REVIEW_ROUND_CAP)
        return next(task for row in found["desks"] for task in row["tasks"] if task["id"] == task_id)

    def digest_line(self, task_id: str) -> str:
        lines = session_start.digest(self.conn, "mcgonagall", now=NOW)
        return next(line for line in lines if line.startswith(f"- {task_id} "))

    def test_a_crash_after_changes_shows_review_died_not_the_older_verdict(self):
        self.commit("first try")
        task_id = self.own_review()["task_id"]
        self.commit("second try")
        self.crashed_review(task_id)
        [first, second] = capacity.review_rounds(self.conn, task_id)
        self.assertEqual((first["verdict"], first["counts"]), ("CHANGES", True))
        self.assertEqual(pensieve.get_task(self.conn, second["reviewer_task_id"])["status"], "closed")
        self.assertEqual((second["has_verdict"], second["counts"]), (False, False))
        self.assertEqual(capacity.stranded_rounds(self.conn, "moody"), [])
        row = self.board(task_id)
        self.assertEqual((row["state"], row["round"], row["verdict"], row["rounds_used"]), ("review died", 2, None, 1))
        self.assertEqual(self.digest_line(task_id), f"- {task_id} ryan-claude-1 review died r2: my own fix | its review"
                                                    " run ended with no verdict; run the review again")
        retried = self.own_review(task_id)
        self.assertEqual((retried["round"], retried["verdict"]), (2, "CHANGES"))
        self.assertEqual((self.board(task_id)["state"], self.board(task_id)["rounds_used"]), ("CHANGES", 2))

    def test_a_first_round_that_crashed_shows_review_died_not_working(self):
        self.commit("first try")
        self.crashed_review()
        [task_id] = self.own_tasks()
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task_id)], [False])
        self.assertEqual((self.board(task_id)["state"], self.board(task_id)["rounds_used"]), ("review died", 0))
        self.assertIn("ryan-claude-1 review died r1: my own fix | its review run ended with no verdict; run the"
                      " review again", self.digest_line(task_id))
        self.assertEqual(self.own_review(task_id)["round"], 1)


class SingleReviewerTests(ManyCase):
    # A store where Moody takes one task at a time: an active task of its own still makes it busy.
    many_task_desks = tuple(desk for desk in MANY_TASK_DESKS if desk != "moody")

    def test_a_single_task_reviewer_with_an_active_task_queues(self):
        other = pensieve.create_task(self.conn, "moody", "a task of moody's own")
        pensieve.start_task(self.conn, other["id"])
        self.commit("first try")
        self.enable("moody")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("ran while moody was busy")):
            queued = review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(queued["queued"], queued_text(queued["task_id"]))
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "active")
        self.assertEqual(self.recovered(), [])


class BuildDeskTests(ManyCase):
    def test_harry_with_a_task_parked_in_changes_starts_a_second_task(self):
        first, created, _ = self.built("fix/widget")
        parked = self.build_review(first, created, "CHANGES", "widget")
        self.assertEqual(parked["verdict"], "CHANGES")
        second, created_two, spawn = self.built("fix/other")
        self.assertEqual(second["status"], "active")
        spawn.assert_called_once()
        self.assertEqual(sorted(task["id"] for task in pensieve.list_tasks(self.conn, desk="harry", status="active")),
                         sorted([first["id"], second["id"]]))
        self.assertNotEqual(created["worktree"], created_two["worktree"])
        with mock.patch.object(run_desk, "spawn") as again, run_desk.task_lock(first["id"]) as lock_fd:
            self.assertIn("started harry", worktree.build(self.conn, first["id"], lock_fd)["desk"])
        again.assert_called_once()

    def test_a_second_open_build_task_under_one_task_md_is_refused_before_any_worktree(self):
        parent = self.queued_parent()
        first, _, _ = self.built("fix/widget", parent)
        second, _ = self.harry_request(parent, subject="build the other half")
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(FleetError, f"task {first['id']} of harry is still open under the same TASK.md"):
                worktree.create(self.conn, second["id"], str(self.repo), "fix/other", fetch=False)
        self.assertFalse(os.path.lexists(config.worktree_dir(second["id"])))
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")
        self.assertNotIn("fix/other", self.git("branch", "--list", "fix/other"))

    def run_castle(self, *argv) -> tuple:
        """castle <argv> against the test store: (exit code, the JSON it printed)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_castle_task_start_makes_the_same_task_md_check_as_fleet_worktree(self):
        parent = self.queued_parent()
        first, _, _ = self.built("fix/widget", parent)
        second, _ = self.harry_request(parent, subject="build the other half")
        code, out = self.run_castle("task", "start", second["id"])
        self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
        self.assertIn(f"task {first['id']} of harry is still open under the same TASK.md", out["error"]["message"])
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")
        # A worktree command for a task under this TASK.md that is still running refuses the start too.
        with worktree.holder_lock(parent):
            code, out = self.run_castle("task", "start", second["id"])
        self.assertEqual((code, out["error"]["message"]), (3, worktree.WORKTREE_RUNNING))
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")
        # Once the first is closed the same command starts the second.
        pensieve.close_task(self.conn, first["id"], "abandoned")
        code, out = self.run_castle("task", "start", second["id"])
        self.assertEqual((code, out["ok"], out["data"]["status"]), (0, True, "active"))
        # A task that is already active gets the store's own refusal, not the check's.
        code, out = self.run_castle("task", "start", second["id"])
        self.assertEqual((code, out["error"]["message"]), (3, "only queued tasks can start"))

    def test_castle_task_start_leaves_other_desks_to_the_store(self):
        parent = self.queued_parent()
        code, out = self.run_castle("task", "start", parent)
        self.assertEqual((code, out["data"]["status"]), (0, "active"))
        # Hermione's tasks may share a TASK.md, so a second one under it still starts.
        for subject in ("review the first change", "review the second change"):
            self.enable("hermione")
            self.owls += 1
            self.write_owl("mcgonagall", f"review-{self.owls}.json", {"to": "hermione", "kind": "request",
                                                                     "subject": subject, "body": "see TASK.md",
                                                                     "task_id": parent})
            with mock.patch.object(run_desk, "spawn"):
                [delivered] = owl_post.run_pass(self.conn)["delivered"]
            owl = next(item for item in owlery.inbox(self.conn, "hermione") if item["id"] == delivered["owl_id"])
            task_id = owlery.get_request(self.conn, owl["request_id"])["task_id"]
            code, out = self.run_castle("task", "start", task_id)
            self.assertEqual((code, out["data"]["status"]), (0, "active"))

    def test_two_worktree_commands_under_one_task_md_race_and_exactly_one_starts(self):
        parent = self.queued_parent()
        first, _ = self.harry_request(parent)
        second, _ = self.harry_request(parent, subject="build the other half")
        outcomes = self.race(first, second)
        self.assertEqual(outcomes[first["id"]]["task_id"], first["id"])
        self.assertIsInstance(outcomes[second["id"]], FleetError)
        self.assertEqual(str(outcomes[second["id"]]), worktree.WORKTREE_RUNNING)
        self.assertEqual(self.harry_active(), [first["id"]])
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")
        self.assertFalse(os.path.lexists(config.worktree_dir(second["id"])))
        # Run again once the first is active, the second is refused by the holder check instead.
        with self.assertRaisesRegex(FleetError, f"task {first['id']} of harry is still open under the same TASK.md"):
            worktree.create(self.conn, second["id"], str(self.repo), "fix/other", fetch=False)

    def test_without_the_lock_the_start_transaction_still_lets_exactly_one_start(self):
        parent = self.queued_parent()
        first, _ = self.harry_request(parent)
        second, _ = self.harry_request(parent, subject="build the other half")
        with mock.patch.object(worktree, "holder_lock", side_effect=lambda holder: contextlib.nullcontext()):
            outcomes = self.race(first, second)
        # Both passed the first check; the second started, and the first's check inside the start refused it.
        self.assertEqual(outcomes[second["id"]]["task_id"], second["id"])
        self.assertRegex(str(outcomes[first["id"]]), f"task {second['id']} of harry is still open under the same")
        self.assertEqual(self.harry_active(), [second["id"]])
        self.assert_taken_back(first, "fix/widget")
        # Once the second is closed, the same command starts the first.
        pensieve.close_task(self.conn, second["id"], "abandoned")
        with mock.patch.object(run_desk, "spawn"):
            again = worktree.create(self.conn, first["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertEqual(again["branch"], "fix/widget")
        self.assertEqual(self.harry_active(), [first["id"]])

    def test_a_parent_closed_while_the_worktree_is_added_takes_the_worktree_back(self):
        parent = self.queued_parent()
        task, _ = self.harry_request(parent)
        real_add = worktree.add_worktree

        def add_then_close(conn, *args, **kwargs):
            added = real_add(conn, *args, **kwargs)
            pensieve.close_task(self.conn, parent, "abandoned")  # McGonagall closes the parent meanwhile
            return added

        with mock.patch.object(worktree, "add_worktree", side_effect=add_then_close), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(ConflictError, "a worktree can only be attached to a queued or active task"):
                worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "closed")
        self.assertFalse(os.path.lexists(config.worktree_dir(task["id"])))
        self.assertFalse(os.path.lexists(self.office / "worktrees" / f"{task['id']}.json"))
        self.assertEqual(self.git("branch", "--list", "fix/widget"), "")

    def test_a_branch_that_moved_is_kept_when_the_worktree_is_taken_back(self):
        parent = self.queued_parent()
        task, _ = self.harry_request(parent)

        def moved_then_refused(conn, refused):
            if refused["id"] == task["id"] and os.path.lexists(config.worktree_dir(task["id"])):
                moved = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "work on the branch")
                self.git("update-ref", "refs/heads/fix/widget", moved)
                raise FleetError("refused late")

        with mock.patch.object(worktree, "_check_startable", side_effect=moved_then_refused), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(FleetError, "^refused late$"):
                worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "queued")
        self.assertFalse(os.path.lexists(config.worktree_dir(task["id"])))
        self.assertFalse(os.path.lexists(self.office / "worktrees" / f"{task['id']}.json"))
        self.assertIn("fix/widget", self.git("branch", "--list", "fix/widget"))

    def test_a_failed_take_back_names_what_is_left(self):
        parent = self.queued_parent()
        task, _ = self.harry_request(parent)
        real_git = worktree.gitops.git

        def failing_remove(args, *rest, **kwargs):
            if args[:2] == ["worktree", "remove"]:
                raise FleetError("git worktree failed: locked")
            return real_git(args, *rest, **kwargs)

        with mock.patch.object(worktree, "_check_startable", side_effect=[None, FleetError("refused late")]), \
                mock.patch.object(worktree.gitops, "git", side_effect=failing_remove), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(FleetError, "^refused late; taking back the new worktree also failed"
                                                    r" \(git worktree failed: locked\), so remove the worktree"
                                                    f" {re.escape(config.worktree_dir(task['id']))} and branch"
                                                    f" fix/widget and the record {task['id']}.json by hand$"):
                worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "queued")
        self.assertTrue(os.path.lexists(config.worktree_dir(task["id"])))

    def test_a_single_build_desk_refuses_before_any_worktree(self):
        first, _, _ = self.built("fix/widget")
        second, _ = self.harry_request(self.queued_parent("another"))
        with mock.patch.object(pensieve, "blocking_task", return_value=first):
            with self.assertRaisesRegex(FleetError, f"harry already has an active task {first['id']}"):
                worktree.create(self.conn, second["id"], str(self.repo), "fix/other", fetch=False)
        self.assertFalse(os.path.lexists(config.worktree_dir(second["id"])))


class SingleBuildDeskRaceTests(ManyCase):
    # A store that never ran castle desk many-tasks harry: the holder locks differ, so only the start
    # transaction keeps a second task of Harry's from starting.
    many_task_desks = tuple(desk for desk in MANY_TASK_DESKS if desk != "harry")

    def test_a_single_harry_racing_under_two_task_mds_starts_one_and_takes_the_other_back(self):
        self.assertFalse(pensieve.takes_many_tasks(self.conn, "harry"))
        first, _ = self.harry_request(self.queued_parent("one"))
        second, _ = self.harry_request(self.queued_parent("two"))
        outcomes = self.race(first, second)
        self.assertEqual(outcomes[second["id"]]["task_id"], second["id"])
        self.assertEqual(str(outcomes[first["id"]]), f"harry already has an active task {second['id']}")
        self.assertEqual(self.harry_active(), [second["id"]])
        self.assert_taken_back(first, "fix/widget")
        pensieve.close_task(self.conn, second["id"], "abandoned")
        with mock.patch.object(run_desk, "spawn"):
            self.assertEqual(worktree.create(self.conn, first["id"], str(self.repo), "fix/widget",
                                             fetch=False)["task_id"], first["id"])


class PushTests(ManyCase, GateCase):
    def test_a_passed_task_pushes_while_its_sibling_waits_for_fixes(self):
        x, created_x, _ = self.built("fix/widget")
        passed = self.build_review(x, created_x, "PASS", "widget")
        y, created_y, _ = self.built("fix/other")
        changed = self.build_review(y, created_y, "CHANGES", "other widget")
        self.assertEqual(pensieve.get_task(self.conn, x["id"])["status"], "awaiting_close")
        self.assertEqual(pensieve.get_task(self.conn, y["id"])["status"], "active")
        pushed = push.push(self.conn, x["id"])
        self.assertEqual((pushed["branch"], pushed["sha"]), ("fix/widget", passed["sha"]))
        with self.assertRaisesRegex(FleetError, "no review pass"):
            push.check(self.conn, y["id"])
        self.assert_blocked("git push origin HEAD:refs/heads/fix/other", cwd=created_y["worktree"],
                            why="no review pass")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=created_y["worktree"]), changed["sha"])
        self.assert_allowed("git push origin HEAD:refs/heads/fix/widget", cwd=created_x["worktree"])


class RunDeskTaskTests(ManyCase):
    def review_owl(self, desk: str = "hermione") -> tuple:
        """A review request from Harry's task to Hermione, delivered: (parent, Harry's task, opened)."""
        parent = self.queued_parent()
        task, created, _ = self.built("fix/widget", parent)
        sha = self.git("rev-parse", "HEAD", cwd=created["worktree"])
        opened = capacity.open_review_round(self.conn, task["id"], desk, sha, "review it", body="read the diff")
        review._deliver(self.conn, opened["owl"]["id"], desk, "read the diff")
        return parent, task, opened

    def test_a_review_rounds_pad_is_its_author_tasks(self):
        _, task, opened = self.review_owl()
        plan = run_desk.build_plan(self.conn, "hermione", opened["owl"]["id"])
        pad = f"{self.castle}/desks/hermione/pads/{task['id']}.md"
        self.assertEqual((plan["task_id"], plan["pad"], plan["pad_key"]), (opened["task"]["id"], pad, task["id"]))
        self.assertIn(run_desk.PAD_LINE.format(task_id=opened["task"]["id"], pad=pad), plan["stdin"])
        self.assertFalse((self.castle / "desks" / "hermione" / "pads").exists())

    def test_another_task_under_the_same_task_md_gets_its_own_pad(self):
        parent, task, opened = self.review_owl()
        triage = owlery.open_request(self.conn, "mcgonagall", "hermione", "triage the PR comments",
                                     body="read the comments", parent_task_id=parent)
        review._deliver(self.conn, triage["owl"]["id"], "hermione", "read the comments")
        plan = run_desk.build_plan(self.conn, "hermione", triage["owl"]["id"])
        self.assertEqual((plan["task_id"], plan["pad_key"]), (triage["task"]["id"], triage["task"]["id"]))
        review_plan = run_desk.build_plan(self.conn, "hermione", opened["owl"]["id"])
        self.assertNotEqual(plan["pad"], review_plan["pad"])
        self.assertEqual(capacity.round_author(self.conn, opened["task"]["id"]), task["id"])
        self.assertIsNone(capacity.round_author(self.conn, triage["task"]["id"]))

    def test_only_pad_desks_get_a_pad(self):
        _, task, _ = self.review_owl()
        owl = next(item for item in owlery.inbox(self.conn, "harry", include_acked=True)
                   if item["request_id"] == task["request_id"])
        plan = run_desk.build_plan(self.conn, "harry", owl["id"])
        self.assertEqual((plan["task_id"], plan["pad"]), (task["id"], None))
        self.assertNotIn("Your pad is", plan["stdin"])
        self.assertEqual(config.TASK_PAD_DESKS, ("hermione", "ron"))


class RunDeskPadTests(RunDeskCase):
    def titled(self, recipient: str, subject: str, worktree: str = None) -> tuple:
        """McGonagall's request with its own subject, so two requests to one desk never fold into one."""
        with mock.patch.object(run_desk, "spawn"):
            owl_id = self.deliver("mcgonagall", recipient, kind="request", subject=subject)
        owl = next(item for item in owlery.inbox(self.conn, recipient) if item["id"] == owl_id)
        task_id = owlery.get_request(self.conn, owl["request_id"])["task_id"]
        if worktree is not None:
            (self.castle / "worktrees" / worktree).mkdir(mode=0o700)
            self.conn.execute("UPDATE tasks SET worktree = ? WHERE id = ?", (f"{ids.WORKTREES_ROOT}/{worktree}", task_id))
        return owl_id, task_id

    def real_run(self, desk: str, owl_id: str, returncode: int = 0) -> tuple:
        with fake_children(returncode=returncode) as started:
            code, out, err = self.main(desk, "--owl", owl_id)
        return code, err, started

    def test_a_pad_is_made_at_launch_but_not_in_a_dry_run(self):
        self.enable("ron")
        owl_id, task_id = self.request("ron")
        plan = self.dry_run("ron", "--owl", owl_id)
        pads = self.castle / "desks" / "ron" / "pads"
        self.assertEqual((plan["task_id"], plan["pad"]), (task_id, f"{pads}/{task_id}.md"))
        self.assertFalse(pads.exists())
        code, err, started = self.real_run("ron", owl_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.lstat(pads).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(pads / f"{task_id}.md").st_mode), 0o600)
        self.assertEqual((pads / f"{task_id}.md").read_text(), f"# Pad {task_id}\n\n## Checkpoint\n")
        self.assertIn(f"This run is for task {task_id}. Your pad is {pads}/{task_id}.md",
                      started.call_args.kwargs["input"].decode())
        [launch] = capacity.list_launches(self.conn, "ron")
        self.assertEqual(launch["task_id"], task_id)

    def test_a_launch_rotates_the_pad_and_the_scratchpad_unless_another_slot_runs(self):
        self.enable("hermione")
        owl_id, task_id = self.request("hermione")
        folder = self.castle / "desks" / "hermione"
        (folder / "pads").mkdir(mode=0o700)
        blocks = "".join(f"\n### Checkpoint 2027-01-0{n}\n- round {n}\n" for n in (1, 2))
        pad = self.write_file(folder / "pads" / f"{task_id}.md", "# Pad\n\n## Checkpoint\n" + blocks)
        scratch = self.write_file(folder / "scratchpad.md", "# Scratchpad\n\n## Notes\n- kept\n" + blocks)
        shared = self.write_file(folder / "pads" / "bot-pass.md", "# Pad\n\n## Checkpoint\n" + blocks)
        for path in (pad, scratch, shared):  # written a while ago, so no desk is still writing them
            os.utime(path, (time.time() - 600, time.time() - 600))
        # A slot past today's count, held by a run from before RUN_SLOTS shrank, may be writing the scratchpad.
        with run_desk.slot_lock("hermione", db.RUN_SLOT_LIMIT - 1):
            code, err, _ = self.real_run("hermione", owl_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(pad.read_text(), "# Pad\n\n## Checkpoint\n\n### Checkpoint 2027-01-02\n- round 2\n")
        [archived] = (folder / "pads" / config.SCRATCHPAD_ARCHIVE_DIR).iterdir()
        self.assertTrue(archived.name.startswith(f"{task_id}-"))
        self.assertIn("- round 1", archived.read_text())
        self.assertIn("- round 1", scratch.read_text())
        self.assertIn("- round 1", shared.read_text())
        os.unlink(scratch)
        os.symlink(self.tmp, scratch)  # a refused scratchpad never stops the shared pad's rotation or the run
        second, _ = self.titled("hermione", "triage the comments")
        with fake_children():
            code, out, err = self.main("hermione", "--owl", second)
        self.assertEqual(code, 0, err)
        self.assertIn("Scratchpad rotation skipped", out)
        self.assertNotIn("- round 1", shared.read_text())
        os.unlink(scratch)
        self.write_file(scratch, "# Scratchpad\n\n## Notes\n- kept\n" + blocks)
        os.utime(scratch, (time.time() - 600, time.time() - 600))
        third, _ = self.titled("hermione", "triage the replies")
        code, err, _ = self.real_run("hermione", third)
        self.assertEqual(code, 0, err)
        self.assertEqual(scratch.read_text(),
                         "# Scratchpad\n\n## Notes\n- kept\n\n### Checkpoint 2027-01-02\n- round 2\n")
        self.assertIn("- round 1", next((folder / config.SCRATCHPAD_ARCHIVE_DIR).iterdir()).read_text())

    def test_an_existing_pad_is_kept_and_a_linked_pad_is_refused(self):
        self.enable("hermione")
        owl_id, task_id = self.request("hermione")
        pads = self.castle / "desks" / "hermione" / "pads"
        pads.mkdir(mode=0o700)
        self.write_file(pads / f"{task_id}.md", "# Pad\n\n## Checkpoint\nround one notes\n")
        code, err, _ = self.real_run("hermione", owl_id)
        self.assertEqual(code, 0, err)
        self.assertIn("round one notes", (pads / f"{task_id}.md").read_text())
        second, other = self.titled("hermione", "triage the comments")
        target = self.write_file(self.tmp / "elsewhere.md", "keep me\n")
        os.symlink(target, pads / f"{other}.md")
        with fake_children() as started:
            code, _, err = self.main("hermione", "--owl", second)
        self.assertEqual(code, 1)
        self.assertIn("pad", err)
        started.assert_not_called()
        self.assertEqual(target.read_text(), "keep me\n")
        self.assertEqual(len(capacity.list_launches(self.conn, "hermione")), 1)

    def test_two_owls_of_harrys_two_tasks_run_in_two_worktrees(self):
        first, first_task = self.titled("harry", "build one", worktree="tk-one")
        second, second_task = self.titled("harry", "build two", worktree="tk-two")
        plans = [self.dry_run("harry", "--owl", owl) for owl in (first, second)]
        self.assertEqual([plan["cwd"] for plan in plans],
                         [f"{self.castle}/worktrees/tk-one", f"{self.castle}/worktrees/tk-two"])
        self.assertEqual([plan["task_id"] for plan in plans], [first_task, second_task])

    def test_a_closed_task_is_not_run_and_raises_no_failure(self):
        self.enable("harry")
        owl_id, task_id = self.request("harry", worktree="tk-one")
        pensieve.close_task(self.conn, task_id, "abandoned")
        with fake_children() as started:
            code, _, err = self.main("harry", "--owl", owl_id)
        self.assertEqual(code, 1)
        self.assertIn(f"task {task_id} is closed", err)
        started.assert_not_called()
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_a_run_that_gives_up_on_the_desk_lock_says_so(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        with mock.patch.object(config, "DESK_LOCK_WAIT_SECONDS", 0), every_slot("hermione"), \
                fake_children() as started:
            code, _, _ = self.main("hermione", "--owl", owl_id)
        self.assertEqual(code, 1)
        started.assert_not_called()
        self.assertEqual(self.events_of("rundesk.failed"), [])
        [event] = self.events_of("rundesk.lock-wait")
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn(f"hermione waited 0 minutes for its desk lock behind its other runs and gave up, so owl {owl_id}",
                      event["summary"])


class OwlPostBoardTests(RunDeskCase):
    def setUp(self) -> None:
        super().setUp()
        clock = mock.patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def board(self) -> list:
        found = capacity.in_flight(self.conn, NOW, config.RUNNING_WINDOW_SECONDS, max_rounds=config.REVIEW_ROUND_CAP)
        return [(task["id"], task["desk"], task["status"], task["state"], task["running"])
                for row in found["desks"] for task in row["tasks"]]

    def test_an_ordinary_request_run_shows_running_on_the_board_while_its_task_is_queued(self):
        # Owl Post rings run_desk for an ordinary request without on_start, so the task stays queued all run.
        for desk in ("ron", "hermione"):
            with self.subTest(desk=desk):
                self.enable(desk)
                seen = {}

                def during(argv, **kwargs):
                    [task_id] = [task["id"] for task in pensieve.list_tasks(self.conn, desk=desk)]
                    seen["task"] = task_id
                    seen["board"] = self.board()
                    seen["digest"] = session_start.digest(self.conn, "mcgonagall", now=NOW)
                    return subprocess.CompletedProcess(args=argv, returncode=0)

                def ring(recipient: str, owl_id: str) -> None:
                    with fake_children(during) as started, contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(run_desk.main([recipient, "--owl", owl_id]), 0)
                    started.assert_called_once()

                self.write_owl("mcgonagall", f"{desk}-request.json", {"to": desk, "kind": "request",
                                                                      "subject": f"triage for {desk}", "body": "go"})
                with mock.patch.object(run_desk, "spawn", side_effect=ring) as spawn:
                    owl_post.run_pass(self.conn, now=NOW)
                spawn.assert_called_once()
                task_id = seen["task"]
                self.assertEqual(seen["board"], [(task_id, desk, "queued", "running", True)])
                self.assertIn(f"- {task_id} {desk} running: triage for {desk} | a run is going", seen["digest"])
                self.assertFalse(any(line.startswith(f"- task {task_id} ") for line in seen["digest"]))
                [launch] = capacity.list_launches(self.conn, desk)
                self.assertEqual(launch["task_id"], task_id)
                # Once the run records its usage it is no longer going, and the queued task leaves the board.
                self.assertEqual(pensieve.get_task(self.conn, task_id)["status"], "queued")
                self.assertEqual(self.board(), [])

    def test_a_queued_task_with_no_run_going_stays_off_the_board(self):
        self.enable("ron")
        owl_id, task_id = self.request("ron")
        self.assertEqual(self.board(), [])
        capacity.record_launch(self.conn, "ron", "run-" + "e" * 16, "sonnet", task_id=task_id, now=NOW - 7200)
        self.assertEqual(self.board(), [])
        capacity.record_launch(self.conn, "ron", "run-" + "f" * 16, "sonnet", task_id=task_id, now=NOW - 60)
        self.assertEqual(self.board(), [(task_id, "ron", "queued", "running", True)])


class DigestTests(HookCase):
    def digest(self) -> list:
        code, out, err = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def test_needs_you_comes_first_with_one_summary_line_per_desk(self):
        working = self.started_task("harry", "build the widget")
        waiting = pensieve.mark_awaiting_close(self.conn, self.started_task("ryan-claude-1", "site copy")["id"])
        lines = self.digest()
        start = lines.index("In flight: 2 tasks on 2 desks")
        self.assertEqual(lines[start + 1:start + 3], ["- harry: 1 (1 working)", "- ryan-claude-1: 1 (1 awaiting close)"])
        self.assertEqual(lines[start + 3], "Needs you (1 shown, 0 more):")
        self.assertEqual(lines[start + 4],
                         f'- {waiting["id"]} ryan-claude-1 awaiting close: site copy | gate: "Mischief managed'
                         f' {waiting["id"]}"')
        self.assertEqual(lines[start + 5:start + 7],
                         ["Moving (1 shown, 0 more):", f"- {working['id']} harry working: build the widget | working"])

    def test_review_round_tasks_fold_into_their_author(self):
        author = self.started_task("ryan-claude-1", "site copy")
        pensieve.record_commit(self.conn, author["id"], "acme/web-app", "a" * 40)
        opened = capacity.open_review_round(self.conn, author["id"], "moody", "a" * 40, "review it")
        lines = self.digest()
        text = "\n".join(lines)
        self.assertNotIn(opened["task"]["id"], text)
        self.assertIn(f"- {author['id']} ryan-claude-1 review queued r1: site copy", text)
        self.assertIn("Queued work: none", lines)
        _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="resume"))
        self.assertIn("1 in flight", out)
        self.assertIn("0 queued", out)

    def test_a_round_whose_review_died_needs_ryan_and_never_says_a_run_is_going(self):
        author = self.started_task("ryan-claude-1", "site copy")
        pensieve.record_commit(self.conn, author["id"], "acme/web-app", "a" * 40)
        opened = capacity.open_review_round(self.conn, author["id"], "moody", "a" * 40, "review it")
        pensieve.start_task(self.conn, opened["task"]["id"])  # the review died before closing it, and no run is going
        lines = self.digest()
        self.assertIn("Needs you (1 shown, 0 more):", lines)
        self.assertIn(f"- {author['id']} ryan-claude-1 review died r1: site copy | its review run ended with no"
                      " verdict; run the review again", lines)
        self.assertNotIn("a run is going", "\n".join(lines))

    def test_thirty_open_tasks_stay_inside_the_line_budget(self):
        desks = ("harry", "ryan-claude-1", "moody", "ron", "hermione")
        for index in range(30):
            task = self.started_task(desks[index % len(desks)], f"task {index}")
            if index % 3 == 0:
                pensieve.mark_awaiting_close(self.conn, task["id"])
        for index in range(25):
            pensieve.create_task(self.conn, "ron", f"queued job {index}", now=NOW)
        self.headmaster(10)
        lines = self.digest()
        self.assertLessEqual(len(lines), config.DIGEST_MAX_LINES)
        self.assertIn("In flight: 30 tasks on 5 desks", lines)
        task_lines = [line for line in lines if re.match(r"- tk_[0-9a-f]{16} ", line)]
        self.assertEqual(len(task_lines), config.INFLIGHT_CAP)
        self.assertIn("Needs you (10 shown, 0 more):", lines)
        self.assertIn("Moving (0 shown, 20 more):", lines)

    def test_mcgonagall_stays_single_so_pre_compact_names_her_one_task(self):
        first = self.started_task("mcgonagall", "route the review")
        second = pensieve.create_task(self.conn, "mcgonagall", "another", now=NOW)
        with self.assertRaisesRegex(ConflictError, "desk already has an active task"):
            pensieve.start_task(self.conn, second["id"])
        self.assertEqual([desk["name"] for desk in pensieve.list_desks(self.conn) if desk["many_tasks"]],
                         sorted(MANY_TASK_DESKS))
        code, _, err = self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="auto"))
        self.assertEqual(code, 0, err)
        self.assertIn(f"- Task: {first['id']} active, route the review",
                      (self.castle / "desks" / "mcgonagall" / "scratchpad.md").read_text())


class CastleGitTests(ManyCase):
    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_castles_git_ignores_task_pads_but_not_scratchpads(self):
        folder = self.tmp / "castle-git"
        for desk in config.TASK_PAD_DESKS:
            (folder / "desks" / desk / "pads").mkdir(parents=True)
            self.write_file(folder / "desks" / desk / "pads" / "tk_0123456789abcdef.md", "# Pad\n")
            self.write_file(folder / "desks" / desk / "scratchpad.md", "# Scratchpad\n")
        self.write_file(folder / ".gitignore", (KIT / "castle" / ".gitignore").read_text())
        self.git("init", "-q", "-b", "main", cwd=folder)
        status = self.git("status", "--porcelain", "--untracked-files=all", cwd=folder).splitlines()
        self.assertEqual(sorted(line[3:] for line in status),
                         [".gitignore"] + sorted(f"desks/{desk}/scratchpad.md" for desk in config.TASK_PAD_DESKS))


# The kit's briefs, settings, charter and install.sh; an installed office keeps its own desk files.
@unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
class DeskTextTests(unittest.TestCase):
    def text(self, *parts: str) -> str:
        return (KIT.joinpath(*parts)).read_text()

    def test_pad_desk_settings_may_edit_their_own_pads_only(self):
        for desk in ("hermione", "ron", "portrait", "snape"):
            allow = json.loads(self.text("office", "desks", desk, "settings.json"))["permissions"]["allow"]
            pads = [rule for rule in allow if "/pads" in rule]
            with self.subTest(desk=desk):
                expected = [f"Edit(~/hogwarts/desks/{desk}/pads/**)"] if desk in config.TASK_PAD_DESKS else []
                self.assertEqual(pads, expected)

    def test_the_briefs_and_the_charter_name_the_pads(self):
        for desk in config.TASK_PAD_DESKS:
            with self.subTest(desk=desk):
                brief = self.text("office", "desks", desk, "BRIEF.md")
                self.assertIn(f"~/hogwarts/desks/{desk}/pads/<key>.md", brief)
                self.assertIn({"hermione": "<task-id>-drafts-r<round>.md", "ron": "<owl-id>-report.md"}[desk], brief)
        self.assertIn("task pad", self.text("castle", "CLAUDE.md"))

    def test_harry_and_moody_keep_no_scratchpad(self):
        for desk in ("harry", "moody"):
            with self.subTest(desk=desk):
                brief = self.text("office", "desks", desk, "BRIEF.md")
                self.assertNotIn("scratchpad", brief)
                self.assertNotIn("one task at a time", brief)
                self.assertNotIn("one review at a time", brief)

    def test_install_grants_the_seed_desks(self):
        loop = re.search(r"for desk in ([a-z0-9 -]+); do\n\t\"\$CASTLE_CLI\" desk many-tasks", self.text("install.sh"))
        self.assertIsNotNone(loop)
        self.assertEqual(tuple(loop.group(1).split()), db.MANY_TASK_DESKS_SEED)

    def test_the_installed_office_check_runs_the_installer_with_a_cleared_environment(self):
        # An inherited GIT_DIR or similar must never reach install.sh, or its git init could land in the real castle.
        script = self.text("scripts/installed-office-check.sh")
        runs = [line for line in script.splitlines() if "$REPO_DIR/install.sh" in line and "[ -f" not in line]
        self.assertEqual(runs, ['/usr/bin/env -i HOME="$FAKE_HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin'
                                ' LANG=en_US.UTF-8 /bin/sh "$REPO_DIR/install.sh" \\'])

    def test_the_placeholder_scan_skips_test_folders_but_lists_every_real_file(self):
        # Runs only the scan function from install.sh, on a made-up tree. install.sh itself never runs here.
        script = self.text("install.sh")
        scan = re.search(r"^unfilled_placeholders\(\) \{\n.*?^\}\n", script, re.DOTALL | re.MULTILINE)
        placeholders = re.search(r"^PLACEHOLDERS='([^']+)'$", script, re.MULTILINE)
        self.assertIsNotNone(scan)
        self.assertIsNotNone(placeholders)
        root = temp_dir(self)
        office, castle = root / "office", root / "castle"
        real = [office / "fleet" / "config.py", office / "desks" / "ron" / "settings.json",
                castle / ".claude" / "settings.json", castle / "tests" / "notes.md"]
        fixtures = [office / "tests" / "test_a.py", office / "tests_fleet" / "test_b.py",
                    office / "tests_fleet" / "fixtures" / "live-tools" / "snape.json"]
        for path in real + fixtures:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("a placeholder: <chat-mcp>\n")
        (office / "fleet" / "filled.py").write_text("nothing left to fill in\n")
        shell = (f"OFFICE={shlex.quote(str(office))}\nCASTLE={shlex.quote(str(castle))}\n"
                 f"PLACEHOLDERS={shlex.quote(placeholders.group(1))}\n{scan.group(0)}\n"
                 'unfilled_placeholders "$OFFICE" "$CASTLE" "$OFFICE/missing-agent.md"\n')
        done = subprocess.run(["/bin/sh", "-c", shell], capture_output=True, check=True,
                              env={"PATH": config.CHILD_PATH})
        self.assertEqual(sorted(done.stdout.decode().splitlines()), sorted(str(path) for path in real))


    def test_mcgonagall_writes_task_md_when_she_shows_the_draft_and_the_refusal_says_so(self):
        agent = self.text("castle", ".claude", "agents", "mcgonagall.md")
        self.assertIn("When I show him the draft I write it to ~/hogwarts/tasks/<id>/TASK.md in the same turn", agent)
        with self.assertRaises(user_prompt_submit.Refused) as raised:
            user_prompt_submit.read_spec(b"", "tk_0123456789abcdef")
        self.assertIn("TASK.md", str(raised.exception))
        self.assertIn("write the draft to that path before the go", user_prompt_submit.MISSING_TASK_MD_FIX)

    def path_block(self, user: Path, path: str) -> str:
        """Run only install.sh's PATH section against a made-up home folder, and return what it said, then path_line."""
        script = self.text("install.sh")
        block = re.search(r"^USER_BIN=.*?^fi\n", script, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(block)
        shell = (f"HOME={shlex.quote(str(user))}\nOFFICE={shlex.quote(str(user / '.hogwarts'))}\n"
                 f"PATH={shlex.quote(path)}\nsay() {{ printf '%s\\n' \"$*\"; }}\n{block.group(0)}\n"
                 'printf "LINE=%s\\n" "$path_line"\n')
        done = subprocess.run(["/bin/sh", "-c", shell], capture_output=True, check=True, env={"PATH": config.CHILD_PATH})
        return done.stdout.decode()

    def test_the_installer_links_castle_and_fleet_into_an_existing_user_bin_and_edits_no_profile(self):
        user = temp_dir(self)
        (user / ".local" / "bin").mkdir(parents=True)
        (user / ".zshrc").write_text("# mine\n")
        said = self.path_block(user, "/usr/bin:/bin")
        for tool in ("castle", "fleet"):
            self.assertEqual(os.readlink(user / ".local" / "bin" / tool), str(user / ".hogwarts" / "bin" / tool))
        self.assertIn(f'LINE=export PATH="{user}/.local/bin:$PATH"', said)  # not on PATH yet: the line is printed
        self.assertEqual((user / ".zshrc").read_text(), "# mine\n")
        again = self.path_block(user, f"/usr/bin:/bin:{user}/.local/bin")
        self.assertIn("already points at", again)
        self.assertIn("LINE=\n", again)  # already on PATH: nothing to add

    def test_the_installer_never_replaces_a_file_in_user_bin_and_prints_the_line_when_there_is_no_user_bin(self):
        user = temp_dir(self)
        (user / ".local" / "bin").mkdir(parents=True)
        (user / ".local" / "bin" / "castle").write_text("mine\n")
        said = self.path_block(user, "/usr/bin:/bin")
        self.assertEqual((user / ".local" / "bin" / "castle").read_text(), "mine\n")
        self.assertIn("castle was not linked there", said)
        self.assertTrue((user / ".local" / "bin" / "fleet").is_symlink())
        bare = temp_dir(self)
        said = self.path_block(bare, "/usr/bin:/bin")
        self.assertIn(f'LINE=export PATH="{bare}/.hogwarts/bin:$PATH"', said)
        self.assertFalse((bare / ".local").exists())


class SeedTests(unittest.TestCase):
    def test_the_tests_grant_the_seed_desks(self):
        self.assertEqual(MANY_TASK_DESKS, db.MANY_TASK_DESKS_SEED)
