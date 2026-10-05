"""The review loop: Harry's handoff starts its task's review through the Owl Post, CHANGES starts the fix round,
the loop stops at the round cap, and PASS and HEADMASTER start nothing more.

Runs on real git repos in temp folders. The automatic review's own process is replaced by a call to
review.auto_review in this process, reviewer runs are faked at run_desk.run, and Harry's runs at run_desk.spawn.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from unittest import mock

from hogwarts import capacity, db, ids, owlery, pensieve

from fleet import config, gitops, owl_post, push, review, run_desk, safefs, tools, verify, worktree
from fleet.safefs import FleetError
from tests_fleet.test_review_loop import HANDOFF, LoopCase

REAL_SPAWN_REVIEW = run_desk.spawn_review  # captured before the test base patches it
REAL_SPAWN = run_desk.spawn
REAL_POPEN = subprocess.Popen


class Killed(BaseException):
    """The process ends here, as a kill would end it: nothing after this point runs, and no except Exception sees it."""


@contextlib.contextmanager
def sleeping_runs(case):
    """Harry's detached runs, started by the real run_desk.spawn, are sleepers that inherit exactly the fds a run
    would, so a lock the run is handed stays held until the test ends, as the real run holds it until it ends.
    Yields the (argv, pass_fds) of each start."""
    started = []

    def popen(argv, **kwargs):
        fds = kwargs.get("pass_fds", ())
        child = REAL_POPEN(["/bin/sleep", "60"], pass_fds=fds, close_fds=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        case.addCleanup(child.wait)
        case.addCleanup(child.kill)
        started.append((argv, tuple(fds)))
        return child

    def spawn(*args, **kwargs):
        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            return REAL_SPAWN(*args, **kwargs)

    with mock.patch.object(run_desk, "spawn", side_effect=spawn):
        yield started


@contextlib.contextmanager
def signalled_runs(case, start: bool = True):
    """Harry's detached runs, started by the real run_desk.spawn, where SIGTERM ends the process that starts them just
    as Popen returns, the way common.ended_by_signals ends it (SystemExit). With start, the run had started: a sleeper
    that inherits exactly the fds a run would, as in sleeping_runs. Without it, no process started at all."""

    def popen(argv, **kwargs):
        if start:
            child = REAL_POPEN(["/bin/sleep", "60"], pass_fds=kwargs.get("pass_fds", ()), close_fds=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            case.addCleanup(child.wait)
            case.addCleanup(child.kill)
        raise SystemExit(128 + signal.SIGTERM)

    def spawn(*args, **kwargs):
        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            return REAL_SPAWN(*args, **kwargs)

    with mock.patch.object(run_desk, "spawn", side_effect=spawn):
        yield


class ChainCase(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        self.reviews_run = []
        self.spawned_reviews.side_effect = lambda task_id: self.reviews_run.append(
            review.auto_review(self.conn, task_id))
        self.parent, self.task, self.request_owl, created, _ = self.build()
        self.wt = Path(created["worktree"])
        self.enable("hermione")
        self.before = self.headmaster_events()
        harry = mock.patch.object(run_desk, "spawn")
        self.harry_runs = harry.start()
        self.addCleanup(harry.stop)

    def headmaster_events(self) -> list:
        return [event for event in self.events() if event["verdict"] == "headmaster"]

    def new_events(self) -> list:
        return [event for event in self.headmaster_events() if event not in self.before]

    def rounds(self) -> list:
        return [(row["round"], row["verdict"]) for row in capacity.review_rounds(self.conn, self.task["id"])]

    def stage(self, round_no: int, change: str = None, sender: str = "harry", task_id: str = None,
              body: str = None, **fields) -> None:
        """A result owl in its own files, as Harry posts his handoff, after he changed widget.txt."""
        if change is not None:
            self.write_file(self.wt / "widget.txt", change + "\n")
        text = body if body is not None else HANDOFF.format(task_id=self.task["id"]).replace(
            "round 1", f"round {round_no}")
        self.write_file(self.outbox(sender) / f"handoff-r{round_no}.md", text)
        self.write_owl(sender, f"result-r{round_no}.json", {
            "to": "mcgonagall", "kind": "result", "subject": f"round {round_no} ready",
            "task_id": task_id or self.task["id"], "request_id": self.task["request_id"],
            "body_path": self.outbox_path(sender, f"handoff-r{round_no}.md"), **fields})

    def post(self, round_no: int, change: str = None, sender: str = "harry", task_id: str = None,
             body: str = None, now: int = None, **fields) -> dict:
        self.stage(round_no, change, sender, task_id, body, **fields)
        return owl_post.run_pass(self.conn, now=now)

    def round_with(self, verdict: str, round_no: int, change: str = None, now: int = None) -> dict:
        with self.fake_reviewer(verdict):
            return self.post(round_no, change if change is not None else f"widget {round_no}", now=now)

    @contextlib.contextmanager
    def owl_ids(self, *wanted):
        """The next owls the store makes get these ids, in order, as random ids may fall in any order."""
        real, queue = ids.new_id, list(wanted)
        with mock.patch.object(ids, "new_id", side_effect=lambda kind: queue.pop(0) if kind == "owl" and queue
                               else real(kind)):
            yield

    def next_pass(self) -> dict:
        """The Owl Post's next pass, with each automatic review it starts run here."""
        self.spawned_reviews.side_effect = lambda task_id: self.reviews_run.append(
            review.auto_review(self.conn, task_id))
        return owl_post.run_pass(self.conn)

    def killed_review(self, verdict: str, owner, name: str) -> None:
        """An automatic review of the posted handoff, killed when it reaches owner.name."""
        with self.fake_reviewer(verdict), mock.patch.object(owner, name, side_effect=Killed(name)), \
                self.assertRaises(Killed):
            review.auto_review(self.conn, self.task["id"])

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.wt)

    def latest_review(self) -> Optional[str]:
        """The text of review-latest.md next to the task's TASK.md, or None while there is none."""
        holder, _ = verify.task_md(self.conn, self.task["id"])
        path = self.castle / "tasks" / holder / "review-latest.md"
        return path.read_text() if path.exists() else None

    def killed_before_done(self, verdict: str, *patches) -> None:
        """An automatic review of the posted handoff, killed as it marks what follows its verdict done, so whatever
        that told Ryan was told."""
        real = owl_post.write_after

        def write_after(task_id, request_id, owl_id, state, step=None):
            if state == "done":
                raise Killed("killed")
            return real(task_id, request_id, owl_id, state, step)

        with contextlib.ExitStack() as stack:
            stack.enter_context(self.fake_reviewer(verdict))
            stack.enter_context(mock.patch.object(owl_post, "write_after", side_effect=write_after))
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(Killed):
                review.auto_review(self.conn, self.task["id"])


class HandoffStartsReviewTests(ChainCase):
    def test_a_harry_handoff_opens_one_review_round_with_no_command(self):
        summary = self.round_with("PASS", 1)
        [delivered] = summary["delivered"]
        self.assertEqual((delivered["review"], delivered["task_id"]), (owl_post.REVIEW_STARTED, self.task["id"]))
        self.spawned_reviews.assert_called_once_with(self.task["id"])
        self.assertEqual(self.rounds(), [(1, "PASS")])
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file")
        [ran] = self.reviews_run
        self.assertEqual((ran["outcome"], ran["review"]["reviewer"]), ("reviewed: PASS", "hermione"))
        self.assertEqual(summary["reviews"], [])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])

    def test_changes_starts_the_fix_round_and_the_next_handoff_opens_round_two(self):
        self.round_with("CHANGES", 1)
        self.harry_runs.assert_called_once_with("harry", self.request_owl, hold_fd=mock.ANY)
        self.assertEqual(self.reviews_run[0]["next"], f"started harry on owl {self.request_owl}")
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "active")
        self.round_with("PASS", 2)
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "PASS")])
        self.assertEqual(self.harry_runs.call_count, 1)

    def test_the_loop_stops_at_the_round_cap_with_one_event(self):
        for round_no in range(1, config.REVIEW_ROUND_CAP + 1):
            self.round_with("CHANGES", round_no)
        self.assertEqual(self.rounds(), [(n, "CHANGES") for n in range(1, config.REVIEW_ROUND_CAP + 1)])
        self.assertEqual(self.harry_runs.call_count, config.REVIEW_ROUND_CAP - 1)
        self.assertEqual(self.reviews_run[-1]["next"], "stopped at the review round cap")
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.loop-stopped")
        self.assertIn(f"task {self.task['id']} has used its {config.REVIEW_ROUND_CAP} review rounds", event["summary"])
        self.assertIn(f"recorded CHANGES", event["summary"])
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.assertEqual(owl_post.run_pass(self.conn)["reviews"], [])
        self.assertEqual(self.harry_runs.call_count, config.REVIEW_ROUND_CAP - 1)

    def test_pass_starts_nothing_more_and_says_it_is_ready_for_push(self):
        with mock.patch.object(push, "push", side_effect=AssertionError("pushed")), \
                mock.patch.object(gitops, "git", wraps=gitops.git) as git:
            self.round_with("PASS", 1)
        self.assertFalse(any(call.args[0][:1] == ["push"] for call in git.call_args_list))
        self.harry_runs.assert_not_called()
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "awaiting_close")
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.ready-for-push")
        self.assertIn(f"task {self.task['id']} passed review", event["summary"])
        self.assertIn(f"fleet push {self.task['id']}", event["summary"])

    def test_headmaster_starts_nothing_more(self):
        self.round_with("HEADMASTER", 1)
        self.harry_runs.assert_not_called()
        self.assertEqual([event["kind"] for event in self.new_events()], ["review.headmaster"])
        self.assertEqual(owl_post.run_pass(self.conn)["reviews"], [])
        self.assertEqual(self.rounds(), [(1, "HEADMASTER")])


class HandoffRefusalTests(ChainCase):
    def skipped(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.auto-skipped"]

    def test_the_same_handoff_delivered_twice_starts_one_review(self):
        [first] = self.round_with("CHANGES", 1)["delivered"]
        for name in ("result-r1.json", "handoff-r1.md"):
            os.rename(self.outbox("harry") / ".sent" / f"{first['owl_id']}-{name}", self.outbox("harry") / name)
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            [again] = owl_post.run_pass(self.conn)["delivered"]
        self.assertEqual((again["owl_id"], again["new"]), (first["owl_id"], False))
        self.assertEqual(again["review"], "no review: this handoff already went to the review loop")
        self.assertEqual(self.spawned_reviews.call_count, 1)
        self.assertEqual(self.rounds(), [(1, "CHANGES")])

    def test_the_same_handoff_in_two_files_starts_one_review(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        text = HANDOFF.format(task_id=self.task["id"])
        for name in ("a", "b"):
            self.write_owl("harry", f"{name}.json", {
                "to": "mcgonagall", "kind": "result", "subject": "ready", "task_id": self.task["id"],
                "request_id": self.task["request_id"], "idempotency_key": "handoff-r1", "body": text})
        with self.fake_reviewer("CHANGES"):
            delivered = owl_post.run_pass(self.conn)["delivered"]
        self.assertEqual([entry["review"] for entry in delivered],
                         [owl_post.REVIEW_STARTED, "no review: this handoff already went to the review loop"])
        self.assertEqual(self.rounds(), [(1, "CHANGES")])

    def test_a_handoff_for_a_closed_task_starts_nothing_and_says_why(self):
        pensieve.close_task(self.conn, self.task["id"], "abandoned")
        [delivered] = self.post(1, "widget")["delivered"]
        self.assertEqual(delivered["review"], "no review: the task is closed, not active")
        self.spawned_reviews.assert_not_called()
        [event] = self.skipped()
        self.assertEqual((event["verdict"], event["desk"]), ("routine", "harry"))
        self.assertIn("the task is closed, not active", event["summary"])

    def test_a_handoff_for_a_task_awaiting_close_starts_nothing_and_says_why(self):
        self.round_with("PASS", 1)
        [delivered] = self.round_with("PASS", 2)["delivered"]
        self.assertEqual(delivered["review"], "no review: the task is awaiting close, not active")
        self.assertEqual(self.rounds(), [(1, "PASS")])
        self.assertIn("awaiting close", self.skipped()[-1]["summary"])

    def test_a_handoff_for_a_task_with_no_worktree_starts_nothing_and_says_why(self):
        # McGonagall holds one task at a time, so the setup's parent closes first (with its Harry task) to make room.
        pensieve.close_task(self.conn, self.parent, "abandoned")
        _, other, _, _ = self.harry_task()
        pensieve.start_task(self.conn, other["id"])
        text = HANDOFF.format(task_id=other["id"])
        self.write_file(self.outbox("harry") / "handoff-other.md", text)
        self.write_owl("harry", "other.json", {
            "to": "mcgonagall", "kind": "result", "subject": "ready", "task_id": other["id"],
            "request_id": other["request_id"], "body_path": self.outbox_path("harry", "handoff-other.md")})
        [delivered] = owl_post.run_pass(self.conn)["delivered"]
        self.assertEqual(delivered["review"], "no review: the task has no worktree")
        self.spawned_reviews.assert_not_called()
        self.assertIn("the task has no worktree", self.skipped()[-1]["summary"])

    def test_an_owl_from_another_desk_starts_no_review(self):
        body = HANDOFF.format(task_id=self.task["id"])
        self.write_owl("ron", "forged.json", {"to": "mcgonagall", "kind": "fyi", "subject": "ready", "from": "harry",
                                              "task_id": self.task["id"], "body": body})
        self.write_owl("hermione", "forged.json", {"to": "harry", "kind": "fyi", "subject": "ready",
                                                   "task_id": self.task["id"], "body": body})
        summary = owl_post.run_pass(self.conn)
        self.assertEqual(sorted(entry["from"] for entry in summary["delivered"]), ["hermione", "ron"])
        self.assertTrue(all("review" not in entry for entry in summary["delivered"]))
        self.spawned_reviews.assert_not_called()
        self.assertEqual(self.rounds(), [])

    def test_a_handoff_that_does_not_match_its_request_starts_nothing(self):
        cases = [
            ({"task_id": self.parent}, "no review: the owl's task is not the task its request opened"),
            ({"body": "HANDOFF tk_00112233aabbccdd round 1\nCOMMIT MESSAGE\nx\n"},
             "no review: the handoff names another task"),
            ({"body": "I could not build it.\n"}, "no review: the owl carries no handoff: its text does not start"
                                                  " with a HANDOFF line"),
        ]
        for number, (fields, why) in enumerate(cases, start=1):
            with self.subTest(why=why):
                [delivered] = self.post(number, "widget", **fields)["delivered"]
                self.assertEqual(delivered["review"], why)
        self.spawned_reviews.assert_not_called()
        self.assertEqual(self.rounds(), [])


class ManualAndAutomaticTests(ChainCase):
    def test_a_manual_and_an_automatic_review_share_one_lock_and_open_one_round(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        held, release, outcome = threading.Event(), threading.Event(), {}

        def manual() -> None:
            conn = db.connect(self.db_path)
            try:
                with review.task_review_lock(self.task["id"]):
                    held.set()
                    release.wait(10)
                with self.fake_reviewer("CHANGES"):
                    outcome["manual"] = review.review_build(conn, self.task["id"])
            finally:
                conn.close()

        thread = threading.Thread(target=manual)
        thread.start()
        self.assertTrue(held.wait(10))
        try:
            summary = self.post(1)
            [ran] = self.reviews_run
            self.assertEqual(ran["outcome"], "waiting: another review of this task is running; the Owl Post tries"
                                             " again on its next pass")
            args = argparse.Namespace(command="build", task=self.task["id"])
            with self.assertRaisesRegex(FleetError, re.escape(review.REVIEW_RUNNING)):
                tools.run(self.conn, args)
            self.harry_runs.assert_not_called()
        finally:
            release.set()
            thread.join(30)
        self.assertEqual(outcome["manual"]["round"], 1)
        self.assertEqual(summary["delivered"][0]["review"], owl_post.REVIEW_STARTED)
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a second reviewer ran")):
            resumed = owl_post.run_pass(self.conn)["reviews"]
        self.assertEqual(resumed, [{"task_id": self.task["id"], "review": owl_post.REVIEW_STARTED}])
        self.assertIn("nothing new to review", self.reviews_run[-1]["outcome"])
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])

    def test_manual_build_still_starts_a_fix_round_outside_a_review(self):
        args = argparse.Namespace(command="build", task=self.task["id"])
        self.assertEqual(tools.run(self.conn, args)["desk"], f"started harry on owl {self.request_owl}")
        self.harry_runs.assert_called_once_with("harry", self.request_owl, hold_fd=mock.ANY)


class ResumeTests(ChainCase):
    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # the Owl Post starts a process that never ran

    def test_a_review_that_never_finished_is_started_again_by_the_next_pass(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        self.spawned_reviews.assert_called_once_with(self.task["id"])
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        self.assertEqual(owl_post.run_pass(self.conn)["reviews"],
                         [{"task_id": self.task["id"], "review": owl_post.REVIEW_STARTED}])
        with owl_post.auto_review_lock(self.task["id"]):
            self.assertEqual(owl_post.run_pass(self.conn)["reviews"], [])
        with self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"])
        self.assertEqual((result["owl_id"], result["outcome"]), (owl_id, "reviewed: CHANGES"))
        self.assertEqual(owl_post.run_pass(self.conn)["reviews"], [])
        self.assertEqual(self.spawned_reviews.call_count, 2)

    def test_a_review_killed_every_try_gives_up_and_says_so(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        for _ in range(config.AUTO_REVIEW_MAX_TRIES):
            self.assertIsNotNone(owl_post.take_try(self.task["id"], owl_id))
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            result = review.auto_review(self.conn, self.task["id"])
        self.assertIn(f"it started {config.AUTO_REVIEW_MAX_TRIES} times", result["outcome"])
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.auto")
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])

    def test_a_waiting_review_gives_up_after_the_wait_limit(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        owl = review.latest_result_owl(self.conn, pensieve.get_task(self.conn, self.task["id"]))
        with review.task_review_lock(self.task["id"]):
            waiting = review.auto_review(self.conn, self.task["id"])
            late = review.auto_review(self.conn, self.task["id"],
                                      now=owl["created_at"] + config.AUTO_REVIEW_WAIT_LIMIT_SECONDS)
        self.assertTrue(waiting["outcome"].startswith("waiting: another review of this task is running"))
        self.assertIn("waited 4 hours and gave up", late["outcome"])
        self.assertEqual([event["kind"] for event in self.new_events()], ["review.auto"])
        self.assertEqual(self.rounds(), [])

    def test_a_busy_reviewer_hands_back_its_try(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        with mock.patch.object(review, "_reviewer_busy", return_value=False), \
                mock.patch.object(run_desk, "desk_lock", side_effect=safefs.Busy("every slot is held")):
            result = review.auto_review(self.conn, self.task["id"])
        self.assertTrue(result["outcome"].startswith("waiting: hermione became busy"))
        self.assertEqual(owl_post.take_try(self.task["id"], owl_id), 1)
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [owl_id])

    def test_an_author_run_still_going_holds_the_review_back(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        capacity.record_launch(self.conn, "harry", "run-" + "d" * 16, "codex-default", task_id=self.task["id"])
        with mock.patch.object(config, "AUTO_REVIEW_AUTHOR_WAIT_SECONDS", 0), \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            result = review.auto_review(self.conn, self.task["id"])
        self.assertEqual(result["outcome"], "waiting: harry's run on it is still going; the Owl Post tries again on"
                                            " its next pass")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), self.git("rev-parse", "origin/main"))


class EntryTests(ChainCase):
    def test_the_spawned_review_inherits_nothing_and_runs_the_review_main(self):
        with mock.patch.object(run_desk.subprocess, "Popen") as popen:
            REAL_SPAWN_REVIEW(self.task["id"])
            with self.assertRaises(Exception):
                REAL_SPAWN_REVIEW("not-a-task")
        [call] = popen.call_args_list
        argv, kwargs = call.args[0], call.kwargs
        self.assertEqual(tuple(argv[:7]), config.PYTHON_WRAPPER)
        self.assertIn("from fleet.review import main; sys.exit(main())", argv[8])
        self.assertEqual(argv[9:], [self.task["id"]])
        self.assertEqual((kwargs["env"], kwargs["close_fds"], kwargs["start_new_session"]), ({}, True, True))
        self.assertTrue((self.office / "logs" / "review-auto.log").exists())

    def test_main_takes_exactly_one_task_id(self):
        for argv in ([], ["tk_x"], [self.task["id"], "--force"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(review.main(argv), 2)

    def test_main_prints_one_json_object(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(review.main([self.task["id"]]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["data"]["outcome"], "no handoff of this task waits for its review")


class SameSecondTests(ChainCase):
    """Owls of one second keep the order they came in. Owl ids are random, so they never order anything."""

    def test_same_second_handoffs_with_descending_ids_open_the_next_round(self):
        stamp = int(time.time())
        with self.owl_ids("owl_" + "f" * 16):
            self.round_with("CHANGES", 1, now=stamp)
        with self.owl_ids("owl_" + "0" * 16):
            self.round_with("CHANGES", 2, now=stamp)
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "CHANGES")])
        newest = review.latest_result_owl(self.conn, pensieve.get_task(self.conn, self.task["id"]))
        self.assertEqual(newest["id"], "owl_" + "0" * 16)
        self.assertEqual(self.reviews_run[-1]["owl_id"], "owl_" + "0" * 16)
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])

    def test_same_second_handoffs_with_descending_ids_review_the_one_posted_last(self):
        self.spawned_reviews.side_effect = None
        stamp = int(time.time())
        later = HANDOFF.format(task_id=self.task["id"]).replace("round 1", "round 2").replace(
            "Add the widget file", "Add the widget file again")
        self.stage(1, "widget")
        self.stage(2, body=later)
        with self.owl_ids("owl_" + "f" * 16, "owl_" + "0" * 16):
            owl_post.run_pass(self.conn, now=stamp)
        with self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=stamp)
        self.assertEqual((result["owl_id"], result["outcome"]), ("owl_" + "0" * 16, "reviewed: CHANGES"))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file again")
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])


class CheckedHandoffSelectionTests(ChainCase):
    """Each later owl is posted a second after the one before, so it is newer whatever the order of owl ids."""

    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # each test runs the automatic review itself
        self.stamp = int(time.time())

    def test_a_later_result_that_the_owl_post_refused_never_erases_the_waiting_handoff(self):
        self.post(1, "widget", now=self.stamp)
        [waiting] = owl_post.unfinished_handoffs(self.task["id"])
        for number, body in ((2, "I could not build it.\n"),
                             (3, HANDOFF.format(task_id="tk_00112233aabbccdd").replace("round 1", "round 3"))):
            [delivered] = self.post(number, body=body, now=self.stamp + number)["delivered"]
            self.assertTrue(delivered["review"].startswith("no review: "), delivered["review"])
        with self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=self.stamp + 3)
        self.assertEqual((result["outcome"], result.get("owl_id")), ("reviewed: CHANGES", waiting))
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file")

    def wait_bringing(self, body: str):
        """The author wait, during which Harry posts one more result owl with this text."""
        real = review._author_run_over

        def wait(conn, task, now, wait):
            if wait:
                self.post(2, body=body, now=self.stamp + 1)
            return real(conn, task, now, wait)

        return mock.patch.object(review, "_author_run_over", side_effect=wait)

    def test_a_handoff_that_fails_its_checks_during_the_wait_never_supplies_the_commit(self):
        self.post(1, "widget", now=self.stamp)
        [checked] = owl_post.unfinished_handoffs(self.task["id"])
        sneaky = HANDOFF.format(task_id="tk_00112233aabbccdd").replace("Add the widget file", "Slip another change in")
        with self.wait_bringing(sneaky), self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=self.stamp + 1)
        self.assertEqual((result["owl_id"], result["review"]["handoff_owl"]), (checked, checked))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file")
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])

    def test_a_handoff_that_passes_its_checks_during_the_wait_is_checked_and_reviewed_instead(self):
        self.post(1, "widget", now=self.stamp)
        [first] = owl_post.unfinished_handoffs(self.task["id"])
        newer = HANDOFF.format(task_id=self.task["id"]).replace("round 1", "round 2").replace(
            "Add the widget file", "Add the widget file again")
        with self.wait_bringing(newer), self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=self.stamp + 1)
        self.assertNotEqual(result["owl_id"], first)
        self.assertEqual(result["review"]["handoff_owl"], result["owl_id"])
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file again")
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.assertEqual(self.rounds(), [(1, "CHANGES")])

    def test_a_handoff_delivered_after_the_choice_is_never_superseded_and_is_reviewed_next(self):
        self.post(1, "widget", now=self.stamp)
        [chosen] = owl_post.unfinished_handoffs(self.task["id"])
        later = HANDOFF.format(task_id=self.task["id"]).replace("round 1", "round 2").replace(
            "Add the widget file", "Add the widget file again")
        real, choices = review._pending_handoff, []

        def choose(conn, task, now):
            picked = real(conn, task, now)
            choices.append(picked)
            if len(choices) == 2:  # the choice under the review lock: the Owl Post delivers Harry's next one now
                self.post(2, body=later, now=self.stamp + 1)
            return picked

        with mock.patch.object(review, "_pending_handoff", side_effect=choose), self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=self.stamp + 1)
        self.assertEqual((result["owl_id"], result["outcome"]), (chosen, "reviewed: CHANGES"))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file")
        [waiting] = owl_post.unfinished_handoffs(self.task["id"])
        self.assertNotEqual(waiting, chosen)
        self.write_file(self.wt / "widget.txt", "widget again\n")  # Harry's fix round
        with self.fake_reviewer("CHANGES"):
            result = review.auto_review(self.conn, self.task["id"], now=self.stamp + 2)
        self.assertEqual((result["owl_id"], result["outcome"]), (waiting, "reviewed: CHANGES"))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "Add the widget file again")
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "CHANGES")])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])


class BuildRunHoldsTheReviewLockTests(ChainCase):
    def assert_review_refused(self, why: str = None) -> None:
        """A manual review now changes nothing, though the worktree holds work the run has begun."""
        head = self.head()
        self.write_file(self.wt / "widget.txt", "half of the fix\n")
        with self.fake_reviewer("PASS"), self.assertRaisesRegex(FleetError, why or re.escape(review.REVIEW_RUNNING)):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(self.head(), head)

    def test_fleet_build_hands_the_review_lock_to_its_run(self):
        self.round_with("CHANGES", 1)
        args = argparse.Namespace(command="build", task=self.task["id"])
        with sleeping_runs(self) as started:
            self.assertEqual(tools.run(self.conn, args)["desk"], f"started harry on owl {self.request_owl}")
        self.assert_review_refused()
        self.spawned_reviews.side_effect = None
        self.post(2)
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            waiting = review.auto_review(self.conn, self.task["id"])
        self.assertTrue(waiting["outcome"].startswith("waiting: another review of this task is running"))
        [(argv, fds)] = started
        self.assertEqual(argv[-4:], ["--owl", self.request_owl, "--task-lock-fd", str(fds[0])])

    def test_the_loop_fix_round_hands_the_review_lock_to_its_run(self):
        with sleeping_runs(self) as started:
            self.round_with("CHANGES", 1)
        self.assertEqual(self.reviews_run[0]["next"], f"started harry on owl {self.request_owl}")
        self.assert_review_refused()
        [(argv, fds)] = started
        self.assertEqual(argv[-2:], ["--task-lock-fd", str(fds[0])])

    def test_a_manual_review_is_refused_while_the_author_run_may_still_be_going(self):
        self.spawned_reviews.side_effect = None
        self.post(1)
        capacity.record_launch(self.conn, "harry", "run-" + "e" * 16, "codex-default", task_id=self.task["id"])
        self.assert_review_refused("harry's run on this task may still be going")
        self.assertEqual(self.rounds(), [])


class SignalDuringTheHandOverTests(ChainCase):
    """SIGTERM or SIGHUP at the moment a run is started never lets go of the review lock the run was handed: the lock
    stays with the run, so no review starts while it may be writing. When no run started, the lock is free."""

    def assert_review_lock_held(self, held: bool = True) -> None:
        if held:
            with self.assertRaisesRegex(FleetError, re.escape(review.REVIEW_RUNNING)):
                with review.task_review_lock(self.task["id"]):
                    pass
        else:
            with review.task_review_lock(self.task["id"]):
                pass

    def test_a_signal_as_fleet_build_starts_the_run_leaves_the_review_lock_with_the_run(self):
        args = argparse.Namespace(command="build", task=self.task["id"])
        with signalled_runs(self), self.assertRaises(SystemExit):
            tools.run(self.conn, args)
        self.assert_review_lock_held()

    def test_a_signal_as_the_loop_starts_the_fix_round_leaves_the_review_lock_with_the_run(self):
        self.spawned_reviews.side_effect = None
        self.post(1, "widget")
        with signalled_runs(self), self.fake_reviewer("CHANGES"), self.assertRaises(SystemExit):
            review.auto_review(self.conn, self.task["id"])
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.assert_review_lock_held()

    def test_a_signal_before_any_run_started_leaves_the_review_lock_free(self):
        args = argparse.Namespace(command="build", task=self.task["id"])
        with signalled_runs(self, start=False), self.assertRaises(SystemExit):
            tools.run(self.conn, args)
        self.assert_review_lock_held(False)


class WorktreeHandsTheReviewLockTests(LoopCase):
    def test_a_signal_as_fleet_worktree_starts_the_first_run_leaves_the_review_lock_with_it(self):
        _, task, _, _ = self.harry_task()
        with signalled_runs(self), self.assertRaises(SystemExit):
            worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        with self.assertRaisesRegex(FleetError, re.escape(review.REVIEW_RUNNING)):
            with review.task_review_lock(task["id"]):
                pass

    def test_fleet_worktree_hands_the_review_lock_to_the_first_run(self):
        _, task, owl_id, _ = self.harry_task()
        with sleeping_runs(self) as started:
            created = worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertEqual(created["desk"], f"started harry on owl {owl_id}")
        with self.assertRaisesRegex(FleetError, re.escape(review.REVIEW_RUNNING)):
            with review.task_review_lock(task["id"]):
                pass
        [(argv, fds)] = started
        self.assertEqual(argv[-2:], ["--task-lock-fd", str(fds[0])])


class AfterTheVerdictTests(ChainCase):
    """What follows a verdict of the loop survives a kill at any point: a step that had not begun runs on the next
    pass, once, and one that had begun is never run again, and Ryan hears so once. No round is opened again."""

    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # the first review is run, and killed, by each test

    def assert_nothing_more(self) -> None:
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.assertEqual(self.next_pass()["reviews"], [])

    def test_killed_before_its_handoff_was_finished_the_fix_round_still_starts_once(self):
        self.post(1, "widget")
        self.killed_review("CHANGES", owl_post, "finish_handoff")
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.harry_runs.assert_not_called()
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.next_pass()
        self.harry_runs.assert_called_once_with("harry", self.request_owl, hold_fd=mock.ANY)
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.assert_nothing_more()
        self.assertEqual(self.harry_runs.call_count, 1)

    def test_killed_after_its_handoff_was_finished_the_fix_round_still_starts_once(self):
        self.post(1, "widget")
        self.killed_review("CHANGES", review, "_after_verdict")
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.harry_runs.assert_not_called()
        self.assertEqual(self.next_pass()["reviews"], [{"task_id": self.task["id"], "review": owl_post.REVIEW_STARTED}])
        self.harry_runs.assert_called_once_with("harry", self.request_owl, hold_fd=mock.ANY)
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        self.assert_nothing_more()
        self.assertEqual(self.harry_runs.call_count, 1)

    def test_killed_at_the_round_cap_the_loop_stopped_event_still_comes_once(self):
        self.spawned_reviews.side_effect = lambda task_id: self.reviews_run.append(
            review.auto_review(self.conn, task_id))
        for round_no in range(1, config.REVIEW_ROUND_CAP):
            self.round_with("CHANGES", round_no)
        self.spawned_reviews.side_effect = None
        self.post(config.REVIEW_ROUND_CAP, "widget last")
        self.killed_review("CHANGES", review, "_after_verdict")
        self.assertEqual(self.new_events(), [])
        self.next_pass()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.loop-stopped")
        self.assert_nothing_more()
        self.assertEqual(len(self.new_events()), 1)
        self.assertEqual(self.harry_runs.call_count, config.REVIEW_ROUND_CAP - 1)

    def test_killed_while_the_fix_round_started_it_is_never_started_again_and_ryan_hears_once(self):
        self.post(1, "widget")
        self.killed_review("CHANGES", worktree, "build")
        self.next_pass()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.interrupted")
        self.assertIn("may or may not have started", event["summary"])
        self.assertIn(f"fleet build {self.task['id']}", event["summary"])
        self.harry_runs.assert_not_called()
        self.assert_nothing_more()
        self.assertEqual(len(self.new_events()), 1)
        self.assertEqual(self.rounds(), [(1, "CHANGES")])

    def test_a_manual_review_killed_after_its_pass_was_recorded_is_settled_by_the_next_one(self):
        self.post(1, "widget")
        with self.fake_reviewer("PASS"), mock.patch.object(pensieve, "mark_awaiting_close",
                                                           side_effect=Killed("killed")), self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "active")
        with self.assertRaises(review.Unchanged):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "awaiting_close")
        self.assertEqual(self.rounds(), [(1, "PASS")])

    def test_killed_after_the_round_cap_was_told_an_allowance_starts_nothing_by_itself(self):
        self.spawned_reviews.side_effect = lambda task_id: self.reviews_run.append(
            review.auto_review(self.conn, task_id))
        for round_no in range(1, config.REVIEW_ROUND_CAP):
            self.round_with("CHANGES", round_no)
        self.spawned_reviews.side_effect = None
        self.post(config.REVIEW_ROUND_CAP, "widget last")
        self.killed_before_done("CHANGES")
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.loop-stopped")
        capacity.allow_round(self.conn, self.task["id"])  # Ryan, who was told fleet build starts it after this
        self.next_pass()
        self.assert_nothing_more()
        self.assertEqual(self.harry_runs.call_count, config.REVIEW_ROUND_CAP - 1)
        self.assertEqual(len(self.new_events()), 1)
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_killed_after_a_fix_round_that_did_not_start_was_told_ryan_hears_it_once(self):
        self.post(1, "widget")
        self.killed_before_done("CHANGES", mock.patch.object(worktree, "build",
                                                             side_effect=FleetError("harry is at his daily cap")))
        self.next_pass()
        self.assert_nothing_more()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.fix-round")
        self.assertIn("harry is at his daily cap", event["summary"])
        self.harry_runs.assert_not_called()
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_a_review_that_stopped_after_its_verdict_and_told_ryan_is_never_carried_on_by_itself(self):
        self.post(1, "widget")
        real = review._castle_task_file

        def castle_file(holder_id, name, text):
            if name == "review-latest.md":
                raise FleetError("the task folder is not a plain folder")
            return real(holder_id, name, text)

        with self.fake_reviewer("CHANGES"), mock.patch.object(review, "_castle_task_file", side_effect=castle_file):
            stopped = review.auto_review(self.conn, self.task["id"])
        self.assertIn("the automatic review stopped", stopped["outcome"])
        self.next_pass()
        self.assert_nothing_more()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.auto")
        self.harry_runs.assert_not_called()
        # Ryan runs it by hand, as he was told: the review is published, and nothing new is reviewed.
        with self.assertRaises(review.Unchanged):
            review.review_build(self.conn, self.task["id"])
        self.assertIn(f"REVIEW {self.task['id']} @ {self.head()}", self.latest_review() or "")
        self.assertEqual(self.rounds(), [(1, "CHANGES")])


class UnpublishedVerdictTests(ChainCase):
    """A review killed after its verdict was recorded but before it published the review has it published from the
    copy kept in the office before anything acts on that verdict: a fix round, the HEADMASTER event, a settled PASS.
    When it cannot be published, Ryan hears so once and nothing follows the verdict."""

    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # the first review is run, and killed, by each test

    def killed_before_publication(self, verdict: str, manual: bool = False) -> None:
        real = review._castle_task_file

        def castle_file(holder_id, name, text):
            if name == "review-latest.md":
                raise Killed("killed")
            return real(holder_id, name, text)

        with self.fake_reviewer(verdict), mock.patch.object(review, "_castle_task_file", side_effect=castle_file), \
                self.assertRaises(Killed):
            if manual:
                review.review_build(self.conn, self.task["id"])
            else:
                review.auto_review(self.conn, self.task["id"])

    def result_owls(self, round_no: int) -> list:
        row = capacity.review_rounds(self.conn, self.task["id"])[round_no - 1]
        return [owl for owl in owlery.request_owls(self.conn, row["request_id"]) if owl["kind"] == "result"]

    def assert_nothing_more(self) -> None:
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.assertEqual(self.next_pass()["reviews"], [])

    def test_a_fix_round_starts_only_once_the_killed_round_s_review_is_published(self):
        self.spawned_reviews.side_effect = lambda task_id: self.reviews_run.append(
            review.auto_review(self.conn, task_id))
        self.round_with("CHANGES", 1)
        self.spawned_reviews.side_effect = None
        self.post(2, "widget 2")
        self.killed_before_publication("CHANGES")
        sha = self.head()
        self.assertNotIn(f"@ {sha}", self.latest_review())  # still round 1's
        seen = []
        self.harry_runs.side_effect = lambda *args, **kwargs: seen.append(self.latest_review())
        self.next_pass()
        [published] = seen
        self.assertIn(f"REVIEW {self.task['id']} @ {sha}", published or "")
        self.assertEqual(len(self.result_owls(2)), 1)
        self.assert_nothing_more()
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "CHANGES")])

    def test_a_headmaster_verdict_is_told_only_once_its_review_is_published(self):
        self.post(1, "widget")
        self.killed_before_publication("HEADMASTER")
        self.assertIsNone(self.latest_review())
        self.assertEqual(self.new_events(), [])
        self.next_pass()
        self.assertEqual([event["kind"] for event in self.new_events()], ["review.headmaster"])
        self.assertIn(f"REVIEW {self.task['id']} @ {self.head()}", self.latest_review() or "")
        self.assertEqual(len(self.result_owls(1)), 1)
        self.assert_nothing_more()

    def test_a_manual_review_killed_before_publication_is_published_by_the_next_one(self):
        self.post(1, "widget")
        self.killed_before_publication("PASS", manual=True)
        self.assertIsNone(self.latest_review())
        with self.assertRaises(review.Unchanged):
            review.review_build(self.conn, self.task["id"])
        self.assertIn(f"REVIEW {self.task['id']} @ {self.head()}", self.latest_review() or "")
        [owl] = self.result_owls(1)
        self.assertIsNotNone(owl["acked_at"])
        row = capacity.review_rounds(self.conn, self.task["id"])[0]
        self.assertEqual(owlery.get_request(self.conn, row["request_id"])["phase"], "cleaned")
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "awaiting_close")

    def test_a_review_killed_after_its_result_owl_was_sent_is_finished_with_that_same_owl(self):
        self.post(1, "widget")
        with self.fake_reviewer("CHANGES"), mock.patch.object(owlery, "ack", side_effect=Killed("killed")), \
                self.assertRaises(Killed):
            review.auto_review(self.conn, self.task["id"])
        [sent] = self.result_owls(1)
        self.assertIsNone(sent["acked_at"])
        self.next_pass()
        [owl] = self.result_owls(1)
        self.assertEqual(owl["id"], sent["id"])
        self.assertIsNotNone(owl["acked_at"])
        row = capacity.review_rounds(self.conn, self.task["id"])[0]
        self.assertEqual(owlery.get_request(self.conn, row["request_id"])["phase"], "cleaned")
        self.harry_runs.assert_called_once_with("harry", self.request_owl, hold_fd=mock.ANY)
        self.assert_nothing_more()

    def test_a_review_that_cannot_be_published_stops_with_one_event_and_nothing_follows_it(self):
        self.post(1, "widget")
        self.killed_before_publication("CHANGES")
        [kept] = (self.office / "reviews" / self.task["id"]).glob("review-*-hermione-*.md")
        kept.unlink()
        self.next_pass()
        self.assert_nothing_more()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.unpublished")
        self.assertIn("could not be published again", event["summary"])
        self.harry_runs.assert_not_called()
        self.assertIsNone(self.latest_review())
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_a_later_round_s_review_stays_the_latest_when_an_earlier_one_is_published_again(self):
        self.post(1, "widget")
        self.killed_before_publication("CHANGES")
        first = self.head()
        self.write_file(self.wt / "widget.txt", "widget by hand\n")
        with self.fake_reviewer("PASS"):
            review.review_build(self.conn, self.task["id"])
        second = self.head()
        self.next_pass()
        self.assertIn(f"@ {second}", self.latest_review() or "")
        holder, _ = verify.task_md(self.conn, self.task["id"])
        self.assertIn(f"@ {first}", (self.castle / "tasks" / holder / f"review-{first[:12]}-hermione.md").read_text())
        self.assertEqual(len(self.result_owls(1)), 1)


class NewerVerdictTests(ChainCase):
    """A loop round whose review was killed after its verdict is history once a newer round of the task has been
    opened, by hand or otherwise: recovery keeps its review published but acts on nothing, so no fix round, no
    settled PASS and no decision for Ryan follows a verdict a newer one replaced."""

    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # the first review is run, and killed, by each test

    def manual_round(self, verdict: str) -> None:
        """A review Ryan runs by hand on a new commit, which stops at its verdict."""
        self.write_file(self.wt / "widget.txt", "widget by hand\n")
        with self.fake_reviewer(verdict):
            review.review_build(self.conn, self.task["id"])

    def test_an_older_changes_starts_no_fix_round_after_a_newer_headmaster(self):
        self.post(1, "widget")
        self.killed_review("CHANGES", owl_post, "finish_handoff")
        self.manual_round("HEADMASTER")
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "HEADMASTER")])
        self.next_pass()
        self.harry_runs.assert_not_called()
        self.assertEqual(self.rounds(), [(1, "CHANGES"), (2, "HEADMASTER")])
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])
        self.assertIn(f"REVIEW {self.task['id']} @ {self.head()}", self.latest_review() or "")

    def test_an_older_pass_is_never_settled_after_a_newer_changes(self):
        self.post(1, "widget")
        self.killed_review("PASS", review, "settle_verdict")
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "active")
        self.manual_round("CHANGES")
        self.next_pass()
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "active")
        self.assertEqual([event["kind"] for event in self.new_events()], [])
        self.harry_runs.assert_not_called()
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_an_older_headmaster_published_late_tells_ryan_nothing_after_a_newer_round(self):
        self.post(1, "widget")
        real = review._castle_task_file

        def castle_file(holder_id, name, text):
            if name == "review-latest.md":
                raise Killed("killed")
            return real(holder_id, name, text)

        with self.fake_reviewer("HEADMASTER"), \
                mock.patch.object(review, "_castle_task_file", side_effect=castle_file), self.assertRaises(Killed):
            review.auto_review(self.conn, self.task["id"])
        self.manual_round("CHANGES")
        newer = self.latest_review()
        self.next_pass()
        self.assertEqual([event["kind"] for event in self.new_events()], [])
        self.assertEqual(self.latest_review(), newer)
        first = capacity.review_rounds(self.conn, self.task["id"])[0]
        self.assertEqual(owlery.get_request(self.conn, first["request_id"])["phase"], "cleaned")
        self.harry_runs.assert_not_called()
