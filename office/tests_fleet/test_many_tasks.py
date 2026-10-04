"""Many tasks per desk: Harry, Hermione, Moody, Ron and Ryan's own sessions each hold many tasks in flight.

Only a running model process stays one at a time per desk (the desk lock). A task waiting for fixes blocks
nothing, a reviewer is busy only while its desk lock is held, and each pad desk keeps one pad per task.
Reviewer and desk runs are faked at run_desk.run or run_desk.start_child; no model ever runs.
"""
from __future__ import annotations

import json
import os
import contextlib
import re
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, db, ids, owlery, pensieve
from hogwarts.errors import ConflictError
from tests.support import NOW

from fleet import config, owl_post, push, review, run_desk, worktree
from fleet.hooks import pre_compact, session_start
from fleet.safefs import FleetError
from tests_fleet.support import IN_KIT, MANY_TASK_DESKS, ONLY_IN_KIT, fake_children
from tests_fleet.test_hooks import HookCase
from tests_fleet.test_push import GateCase
from tests_fleet.test_review_loop import HANDOFF, TASK_MD, LoopCase
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

    def recovered(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.recovered"]


class OwnSessionTests(ManyCase):
    def test_a_task_parked_in_changes_blocks_no_new_own_review(self):
        # Ryan's case: a website review sits in CHANGES on ryan-claude-1 while four more reviews come in.
        self.commit("website first try")
        parked = self.own_review()
        self.assertEqual((parked["round"], parked["verdict"]), (1, "CHANGES"))
        others = []
        for number in range(4):
            self.commit(f"other change {number}")
            result = self.own_review()
            self.assertEqual((result["round"], result["verdict"], result["queued"]), (1, "CHANGES", None))
            others.append(result["task_id"])
        self.assertEqual(len(set(others)), 4)
        self.assertEqual(sorted(self.own_tasks()), sorted([parked["task_id"], *others]))
        self.commit("website fixed")
        fixed = self.own_review(parked["task_id"], verdict="PASS")
        self.assertEqual((fixed["round"], fixed["verdict"]), (2, "PASS"))
        self.assertEqual(pensieve.get_task(self.conn, parked["task_id"])["status"], "awaiting_close")
        self.assertEqual(sorted(self.own_tasks()), sorted(others))

    def test_a_store_that_keeps_own_sessions_single_names_the_fix(self):
        self.commit("first")
        self.own_review()
        self.commit("second")
        with mock.patch.object(pensieve, "start_task", side_effect=ConflictError("desk already has an active task")):
            with self.assertRaisesRegex(FleetError, "castle desk many-tasks ryan-claude-1"):
                self.own_review()
        [abandoned] = self.own_tasks("closed")
        self.assertEqual(pensieve.get_task(self.conn, abandoned)["close_reason"], "abandoned")


class OwnSessionGuardTests(ManyCase):
    def test_a_new_review_of_a_commit_another_task_holds_is_refused_before_anything_is_made(self):
        self.commit("site")
        self.enable("moody")
        with run_desk.desk_lock("moody", wait=False):
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
        with mock.patch.object(pensieve, "create_task", side_effect=AssertionError("made a task")):
            with self.assertRaisesRegex(FleetError, f"task {capped} on this checkout is at its review round cap;"
                                                    f" castle task allow-round {capped}"):
                self.own_review()
        self.assertEqual(self.own_tasks(), [capped])
        capacity.allow_round(self.conn, capped)
        other = self.own_review()
        self.assertEqual((other["round"], other["verdict"]), (1, "CHANGES"))
        self.assertNotEqual(other["task_id"], capped)

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


class ParallelAuthorTests(ManyCase):
    def test_parallel_authors_keep_their_own_queue_rounds_and_locks(self):
        self.commit("a one")
        first = self.own_review()
        self.commit("b one")
        second = self.own_review()
        a, b = first["task_id"], second["task_id"]
        not_run = mock.patch.object(run_desk, "run", side_effect=AssertionError("ran while moody was busy"))
        self.commit("a two")
        with run_desk.desk_lock("moody", wait=False), not_run:
            queued_a = review.review_own(self.conn, str(self.repo), task_id=a, fetch=False)
        self.commit("b two")
        with run_desk.desk_lock("moody", wait=False), not_run:
            queued_b = review.review_own(self.conn, str(self.repo), task_id=b, fetch=False)
        self.assertEqual((queued_a["round"], queued_b["round"], queued_b["superseded"]), (2, 2, []))
        self.assertTrue(capacity.review_rounds(self.conn, a)[-1]["waiting"])
        # A second review of A while one runs is refused by A's review lock; B only queues on the desk lock.
        with review.task_review_lock(a), run_desk.desk_lock("moody", wait=False), not_run:
            with self.assertRaisesRegex(FleetError, review.REVIEW_RUNNING):
                review.review_own(self.conn, str(self.repo), task_id=a, fetch=False)
            self.commit("b three")
            queued_b2 = review.review_own(self.conn, str(self.repo), task_id=b, fetch=False)
        self.assertEqual(queued_b2["superseded"], [queued_b["request_id"]])
        self.assertTrue(capacity.review_rounds(self.conn, a)[-1]["waiting"])
        self.commit("a three")
        done_a = self.own_review(a)
        self.assertEqual((done_a["round"], done_a["superseded"]), (2, [queued_a["request_id"]]))
        self.assertTrue(capacity.review_rounds(self.conn, b)[-1]["waiting"])
        self.commit("a four")
        self.assertEqual(self.own_review(a)["round"], 3)
        self.commit("a five")
        with self.assertRaisesRegex(FleetError, "review round 4"):
            self.own_review(a)
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
        with mock.patch.object(run_desk, "spawn") as again:
            self.assertIn("started harry", worktree.build(self.conn, first["id"])["desk"])
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
        self.assertIn(run_desk.PAD_LINE.format(task_id=opened["task"]["id"], pad=pad), plan["argv"][-1])
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
        self.assertNotIn("Your pad is", plan["argv"][-1])
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
        self.assertIn(f"This run is for task {task_id}. Your pad is {pads}/{task_id}.md", started.call_args.args[0][-1])
        [launch] = capacity.list_launches(self.conn, "ron")
        self.assertEqual(launch["task_id"], task_id)

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
        with mock.patch.object(config, "DESK_LOCK_WAIT_SECONDS", 0), run_desk.desk_lock("hermione", wait=False), \
                fake_children() as started:
            code, _, _ = self.main("hermione", "--owl", owl_id)
        self.assertEqual(code, 1)
        started.assert_not_called()
        self.assertEqual(self.events_of("rundesk.failed"), [])
        [event] = self.events_of("rundesk.lock-wait")
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn(f"hermione waited 0 minutes for its desk lock behind its other runs and gave up, so owl {owl_id}",
                      event["summary"])


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


class SeedTests(unittest.TestCase):
    def test_the_tests_grant_the_seed_desks(self):
        self.assertEqual(MANY_TASK_DESKS, db.MANY_TASK_DESKS_SEED)
