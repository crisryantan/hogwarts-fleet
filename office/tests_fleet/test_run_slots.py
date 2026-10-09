"""Run slots: a desk runs as many model processes at once as config.RUN_SLOTS gives it.

Moody and Hermione have two, so two reviews of different tasks run at once while a third is queued, and one
review of a task at a time still holds. Each slot has its own lock, Codex work folder and temp folder, and the
Codex profile grants a run only its own. A dead review's reviewer task is closed only by whoever holds that
round's own slot, never while a live review, or the reviewer process a killed review left, holds it. The caps
hold across slots. A desk with one slot behaves as before. Desk processes are faked at run_desk.start_child under
the real run_desk.run; no model ever runs.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, cli, db, owlery, pensieve
from hogwarts.errors import StoreError
from tests.support import NOW

from fleet import config, review, run_desk, safefs
from fleet.safefs import FleetError
from tests_fleet.support import IN_KIT, ONLY_IN_KIT, FleetCase, every_slot, fake_children, kit_setting
from tests_fleet.test_many_tasks import ManyCase
from tests_fleet.test_review_rounds import queued_text
from tests_fleet.test_run_desk import RunDeskCase

REVIEW_LINE = re.compile(r"REVIEW (tk_[0-9a-f]{16}) @ ([0-9a-f]{40})")
USER_TEMP = "/private/var/folders/ab/cd/T"


def lock_names(office, pass_fds) -> list:
    """The office lock files among the fds a desk process inherited, by name."""
    folder = office / "locks"
    by_inode = {os.stat(folder / name).st_ino: name for name in os.listdir(folder)}
    return sorted(by_inode[os.fstat(fd).st_ino] for fd in pass_fds if os.fstat(fd).st_ino in by_inode)


class Background:
    """A call on its own thread and store connection, as a second fleet command would make it."""

    def __init__(self, db_path, call, name: str = "background") -> None:
        self.result, self.error = None, None

        def body() -> None:
            conn = db.connect(db_path)
            try:
                self.result = call(conn)
            except BaseException as exc:  # noqa: BLE001 - the test reads it
                self.error = exc
            finally:
                conn.close()

        self.thread = threading.Thread(target=body, name=name)
        self.thread.start()

    def join(self) -> "Background":
        self.thread.join(120)
        if self.thread.is_alive():
            raise AssertionError("a background call never finished")
        return self


class ConcurrentReviewTests(ManyCase):
    """Reviews of Ryan's own sessions under the real run_desk.run, with Moody's process faked."""

    def setUp(self) -> None:
        super().setUp()
        self.enable("moody")
        self.gates, self.seen, self.failing = {}, {}, set()
        children = fake_children(self.reviewer)
        children.start()
        self.addCleanup(children.stop)
        self.addCleanup(self.release_all)

    def release_all(self) -> None:
        for _, release in self.gates.values():
            release.set()

    def reviewer(self, argv, **kwargs):
        """Moody's process: it writes the review block its prompt asks for. A commit in self.gates waits, once its
        reviewer runs, until the test releases it; one in self.failing exits 1 with no review."""
        task_id, sha = REVIEW_LINE.search(argv[-1]).groups()
        self.seen[sha] = {"locks": lock_names(self.office, kwargs["pass_fds"]), "cwd": kwargs["cwd"]}
        if sha in self.gates:
            running, release = self.gates[sha]
            running.set()
            if not release.wait(60):
                return subprocess.CompletedProcess(argv, 1)
        if sha in self.failing:
            return subprocess.CompletedProcess(argv, 1)
        last = argv[argv.index("--output-last-message") + 1]
        self.write_file(last, f"REVIEW {task_id} @ {sha}\nAC\nBLOCKING\nNON-BLOCKING\nVERDICT: CHANGES\n")
        return subprocess.CompletedProcess(argv, 0)

    def new_commit(self, branch: str, held: bool = False) -> str:
        self.on_branch(branch)
        sha = self.commit(f"{branch} work")
        if held:
            self.gates[sha] = (threading.Event(), threading.Event())
        return sha

    def review_in_background(self, title: str) -> Background:
        """A new review of the commit checked out now, on its own thread; returns once its reviewer runs."""
        sha = self.git("rev-parse", "HEAD")
        started = Background(self.db_path, lambda conn: review.review_own(conn, str(self.repo), title=title,
                                                                         fetch=False))
        if not self.gates[sha][0].wait(60):
            started.join()
            raise AssertionError(f"the review of {title} never started its reviewer: {started.error!r}")
        return started

    def finish(self, sha: str, started: Background) -> dict:
        self.gates[sha][1].set()
        started.join()
        self.assertIsNone(started.error)
        return started.result

    def task_of(self, sha: str) -> str:
        [row] = [row for row in pensieve.commits_with_sha(self.conn, sha)]
        return row["task_id"]

    def round_of(self, task_id: str) -> dict:
        return capacity.review_rounds(self.conn, task_id)[-1]

    def reviewer_task(self, task_id: str) -> dict:
        return pensieve.get_task(self.conn, self.round_of(task_id)["reviewer_task_id"])

    def dead_review(self, branch: str) -> str:
        """A review whose reviewer run fails and whose cleanup never ran, as when it is killed: its reviewer task
        stays active while its slot is free again. Returns its author task."""
        sha = self.new_commit(branch)
        self.failing.add(sha)
        with mock.patch.object(review, "_finish_reviewer_task"), \
                self.assertRaisesRegex(FleetError, "did not finish cleanly"):
            review.review_own(self.conn, str(self.repo), title=branch, fetch=False)
        task_id = self.task_of(sha)
        self.assertEqual(self.reviewer_task(task_id)["status"], "active")
        return task_id

    def test_two_reviews_of_different_tasks_run_at_once_and_a_third_is_queued(self):
        first_sha = self.new_commit("a", held=True)
        first = self.review_in_background("a")
        first_task = self.task_of(first_sha)
        # While A's reviewer runs in one slot, B's runs in the other, from start to verdict.
        second_sha = self.new_commit("b", held=True)
        second = self.review_in_background("b")
        second_task = self.task_of(second_sha)
        self.assertTrue(first.thread.is_alive() and second.thread.is_alive())
        self.assertEqual(self.seen[first_sha]["locks"], sorted([f"review-{first_task}.lock", "desk-moody.lock",
                                                                config.UPDATE_LOCK]))
        self.assertEqual(self.seen[second_sha]["locks"], sorted([f"review-{second_task}.lock",
                                                                 "desk-moody.slot1.lock", config.UPDATE_LOCK]))
        # Every slot is held now: a third task's review is queued, and nothing waits.
        self.new_commit("c")
        with mock.patch.object(run_desk, "start_child", side_effect=AssertionError("ran while moody was busy")):
            queued = review.review_own(self.conn, str(self.repo), title="c", fetch=False)
        self.assertEqual(queued["queued"], queued_text(queued["task_id"]))
        self.assertEqual((self.round_of(queued["task_id"])["slot"], self.round_of(queued["task_id"])["waiting"]),
                         (None, True))
        # A second review of a task under review is still refused, slots or not.
        self.on_branch("a")
        self.commit("a again")
        with self.assertRaisesRegex(FleetError, review.REVIEW_RUNNING):
            review.review_own(self.conn, str(self.repo), task_id=first_task, fetch=False)
        done_second = self.finish(second_sha, second)
        done_first = self.finish(first_sha, first)
        for done, task_id, slot in ((done_first, first_task, 0), (done_second, second_task, 1)):
            with self.subTest(task=task_id):
                self.assertEqual((done["verdict"], done["round"], done["queued"]), ("CHANGES", 1, None))
                self.assertEqual(self.round_of(task_id)["slot"], slot)
                self.assertEqual(self.reviewer_task(task_id)["close_reason"], "superseded")
        self.on_branch("c")
        third = review.review_own(self.conn, str(self.repo), task_id=queued["task_id"], fetch=False)
        self.assertEqual((third["verdict"], third["superseded"]), ("CHANGES", [queued["request_id"]]))
        self.assertEqual(self.recovered(), [])
        self.assertEqual(pensieve.list_tasks(self.conn, desk="moody", status="active"), [])
        self.assertEqual(len(capacity.list_launches(self.conn, "moody")), 3)

    def test_a_dead_review_in_one_slot_is_closed_but_never_the_live_review_in_the_other(self):
        live_sha = self.new_commit("a", held=True)
        live = self.review_in_background("a")
        live_task = self.task_of(live_sha)
        dead_task = self.dead_review("d")  # in slot 1, the one A left free
        self.assertEqual((self.round_of(live_task)["slot"], self.round_of(dead_task)["slot"]), (0, 1))
        # E takes slot 1, the dead review's own: it closes that task, and never A's, whose slot A still holds.
        self.new_commit("e")
        done = review.review_own(self.conn, str(self.repo), title="e", fetch=False)
        self.assertEqual((done["verdict"], self.round_of(done["task_id"])["slot"]), ("CHANGES", 1))
        self.assertEqual(self.reviewer_task(dead_task)["close_reason"], "superseded")
        self.assertEqual(self.reviewer_task(live_task)["status"], "active")
        [event] = self.recovered()
        self.assertIn(self.round_of(dead_task)["reviewer_task_id"], event["summary"])
        # A was never disturbed: it records its verdict and closes its own task.
        finished = self.finish(live_sha, live)
        self.assertEqual((finished["verdict"], finished["round"]), ("CHANGES", 1))
        self.assertEqual(self.reviewer_task(live_task)["close_reason"], "superseded")
        self.assertEqual(len(self.recovered()), 1)

    def test_a_stranded_task_in_another_slot_is_closed_only_once_that_slot_is_free(self):
        with run_desk.slot_lock("moody", 0):
            dead_task = self.dead_review("d")
        self.assertEqual(self.round_of(dead_task)["slot"], 1)
        stranded = self.round_of(dead_task)["reviewer_task_id"]
        # The reviewer process a killed review leaves running still holds slot 1: no review closes its task.
        with run_desk.slot_lock("moody", 1):
            self.new_commit("f")
            done = review.review_own(self.conn, str(self.repo), title="f", fetch=False)
        self.assertEqual((done["verdict"], self.round_of(done["task_id"])["slot"]), ("CHANGES", 0))
        self.assertEqual(pensieve.get_task(self.conn, stranded)["status"], "active")
        self.assertEqual(self.recovered(), [])
        # Once slot 1 is free, the next review closes it from slot 0, holding slot 1 only while it does.
        self.new_commit("g")
        done = review.review_own(self.conn, str(self.repo), title="g", fetch=False)
        self.assertEqual(self.round_of(done["task_id"])["slot"], 0)
        self.assertEqual(pensieve.get_task(self.conn, stranded)["close_reason"], "superseded")
        [event] = self.recovered()
        self.assertIn(stranded, event["summary"])
        with every_slot("moody"):
            pass  # the probe let slot 1 go again

    def test_a_round_that_recorded_no_slot_counts_as_slot_0(self):
        self.new_commit("q")
        with every_slot("moody"):
            queued = review.review_own(self.conn, str(self.repo), title="q", fetch=False)
        hand = self.round_of(queued["task_id"])
        self.assertIsNone(hand["slot"])
        pensieve.start_task(self.conn, hand["reviewer_task_id"])  # started by hand: a stranded round with no slot
        with run_desk.slot_lock("moody", 0):
            self.new_commit("h")
            review.review_own(self.conn, str(self.repo), title="h", fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, hand["reviewer_task_id"])["status"], "active")
        self.new_commit("i")
        review.review_own(self.conn, str(self.repo), title="i", fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, hand["reviewer_task_id"])["close_reason"], "superseded")

    def round_owl(self, task_id: str) -> str:
        """The request owl of the task's last review round, delivered to Moody's inbox."""
        [owl] = [owl for owl in owlery.request_owls(self.conn, self.round_of(task_id)["request_id"])
                 if owl["kind"] == "request"]
        return owl["id"]

    def test_a_rounds_owl_runs_only_from_its_own_review_in_its_rounds_slot(self):
        live_sha = self.new_commit("a", held=True)
        live = self.review_in_background("a")
        live_task = self.task_of(live_sha)
        owl_id = self.round_owl(live_task)
        self.assertEqual(self.round_of(live_task)["slot"], 0)
        # By hand, the way the Owl Post's spawn runs it too: refused before it waits for a slot, with no event.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_desk.main(["moody", "--owl", owl_id])
        self.assertEqual(code, 1)
        self.assertIn(f"is a round of the review of task {live_task}, and only that review runs it", err.getvalue())
        # The way a patrol or the portrait calls run, and with slot 1, free and held, which is not the round's.
        with run_desk.slot_lock("moody", 1) as other:
            for held in (None, other):
                with self.subTest(held=held), self.assertRaisesRegex(run_desk.ReviewOwl, live_task):
                    run_desk.run(self.conn, "moody", owl_id, now=NOW, lock_held=held)
        self.assertEqual(len(capacity.list_launches(self.conn, "moody")), 1)
        self.assertEqual([event for event in self.events() if event["kind"].startswith("rundesk.")], [])
        self.assertEqual(self.finish(live_sha, live)["verdict"], "CHANGES")
        # A round queued while every slot was busy recorded no slot, so even a caller holding slot 0 never runs it.
        self.new_commit("q")
        with every_slot("moody"):
            queued = review.review_own(self.conn, str(self.repo), title="q", fetch=False)
        with run_desk.slot_lock("moody", 0) as slot, self.assertRaises(run_desk.ReviewOwl):
            run_desk.run(self.conn, "moody", self.round_owl(queued["task_id"]), now=NOW, lock_held=slot)
        self.assertEqual(len(capacity.list_launches(self.conn, "moody")), 1)

    def test_a_reviewer_task_is_never_closed_while_its_slot_is_held(self):
        # The live review holds slot 0. Every review that starts meanwhile, in slot 1, finds its task active with
        # no verdict, and leaves it alone each time.
        live_sha = self.new_commit("a", held=True)
        live = self.review_in_background("a")
        live_task = self.task_of(live_sha)
        for branch in ("b", "c", "d"):
            self.new_commit(branch)
            done = review.review_own(self.conn, str(self.repo), title=branch, fetch=False)
            self.assertEqual(self.round_of(done["task_id"])["slot"], 1)
            self.assertEqual(self.reviewer_task(live_task)["status"], "active")
        self.assertEqual(self.recovered(), [])
        self.assertEqual(self.finish(live_sha, live)["verdict"], "CHANGES")


class SlotCapTests(RunDeskCase):
    """The caps belong to the desk: its runs in every slot count toward them, and two launches at once never pass
    a cap that only one of them fits under."""

    def setUp(self) -> None:
        super().setUp()
        self.enable("hermione")
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def held_desk(self, argv, **kwargs):
        """Hermione's process: it keeps running until the test releases it, then reports a 50 cent result."""
        self.release.wait(60)
        os.write(kwargs["stdout"], json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                               "total_cost_usd": 0.5, "result": "done"}).encode() + b"\n")
        return subprocess.CompletedProcess(argv, 0)

    def test_two_launches_at_once_never_pass_a_run_cap_only_one_fits_under(self):
        first_owl, _ = self.request("hermione")
        second_owl, _ = self.request("hermione")
        inside, checked, seen = threading.Event(), threading.Event(), {}
        real_over = run_desk.over_daily_cap

        def over(conn, desk, now=None, **kwargs):
            if threading.current_thread().name == "second":
                checked.set()
            return real_over(conn, desk, now, **kwargs)

        def first_launching() -> None:
            # Called inside the first run's launch section, before its launch counts. The second run, in the other
            # slot, must not read the caps until that launch is recorded.
            inside.set()
            seen["second_read_the_caps_meanwhile"] = checked.wait(2)

        with mock.patch.dict(config.DAILY_RUN_CAP, {"hermione": 1}), fake_children(self.held_desk) as started, \
                mock.patch.object(run_desk, "over_daily_cap", side_effect=over):
            first = Background(self.db_path, lambda conn: run_desk.run(conn, "hermione", first_owl, now=NOW,
                                                                       on_start=first_launching), name="first")
            self.assertTrue(inside.wait(60))
            second = Background(self.db_path, lambda conn: run_desk.run(conn, "hermione", second_owl, now=NOW),
                                name="second").join()
            self.release.set()
            first.join()
        self.assertEqual(seen, {"second_read_the_caps_meanwhile": False})
        self.assertIsNone(first.error)
        self.assertEqual(first.result["exit_code"], 0)
        self.assertIsInstance(second.error, run_desk.Capped)
        self.assertEqual(str(second.error), "daily run cap reached")
        self.assertEqual(started.call_count, 1)
        self.assertEqual([launch["run_id"] for launch in capacity.list_launches(self.conn, "hermione")],
                         [first.result["run_id"]])
        self.assertEqual(len(self.events_of("rundesk.cap")), 1)

    def test_a_run_going_in_another_slot_holds_its_budget_against_the_spend_cap(self):
        first_owl, _ = self.request("hermione")
        second_owl, _ = self.request("hermione")
        limit = config.DAILY_SPEND_CAP_USD["hermione"]
        budget = float(config.MAX_BUDGET_USD["hermione"])
        pensieve.add_metric(self.conn, "hermione", "run-earlier", "opus", 1, 1, 0, limit - budget + 0.5, 10,
                            ts=NOW - 60)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "hermione", NOW))
        with fake_children(self.held_desk) as started, mock.patch("time.time", return_value=NOW):
            first = Background(self.db_path, lambda conn: run_desk.run(conn, "hermione", first_owl, now=NOW))
            for _ in range(600):
                if capacity.list_launches(self.conn, "hermione"):
                    break
                threading.Event().wait(0.1)
            status = run_desk.cap_status(self.conn, "hermione", NOW)
            self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], status["reached"]),
                             (round(limit - budget + 0.5, 6), budget, "spend"))
            code, _, err = self.main("hermione", "--owl", second_owl)
            self.assertEqual(code, 1)
            self.assertIn("daily spend cap reached", err)
            [event] = self.events_of("rundesk.cap")
            self.assertIn(f"(${limit + 0.5:.2f} of ${limit:.2f}, ${budget:.2f} of it held for runs still going)",
                          event["summary"])
            self.release.set()
            first.join()
            self.assertEqual(started.call_count, 1)
            # The first run reported its real cost, so nothing is held and the second fits under the cap now.
            self.assertEqual(run_desk.cap_status(self.conn, "hermione", NOW)["spend_held_usd"], 0.0)
            self.assertEqual(self.main("hermione", "--owl", second_owl)[0], 0)
        self.assertIsNone(first.error)

    def test_a_desk_with_one_slot_holds_nothing_for_a_launch_with_no_usage(self):
        # Exactly as before slots: Ron's open launch, from a run killed before it recorded usage, holds nothing.
        pensieve.add_metric(self.conn, "ron", "run-earlier", "haiku", 1, 1, 0, config.DAILY_SPEND_CAP_USD["ron"] - 0.1,
                            10, ts=NOW - 60)
        capacity.record_launch(self.conn, "ron", "run-" + "e" * 16, "haiku", now=NOW - 60)
        status = run_desk.cap_status(self.conn, "ron", NOW)
        self.assertEqual((status["spend_held_usd"], status["reached"]), (0.0, None))
        # It takes no run lock either, and a lock file left beside its runs is never read or charged.
        self.orphan("run-" + "f" * 16, self.reported(0.3), desk="ron")
        owl_id, _ = self.request("ron")
        self.enable("ron")
        with fake_children(self.locks_seen("ron")):
            run_desk.run(self.conn, "ron", owl_id, now=NOW)
        self.assertEqual(self.seen_locks, [("run-" + "f" * 16 + ".lock", False, False)])
        self.assertEqual(run_desk.reconcile_launches(self.conn, "ron", NOW), [])
        self.assertEqual(self.run_locks("ron"), ["run-" + "f" * 16 + ".lock"])
        self.assertEqual(run_desk.cap_status(self.conn, "ron", NOW)["spend_held_usd"], 0.0)

    @staticmethod
    def reported(cost: float) -> bytes:
        return json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                           "result": "done"}).encode() + b"\n"

    @staticmethod
    def streamed() -> bytes:
        """What a Claude run killed before its result event wrote: one assistant message and its usage."""
        usage = {"input_tokens": 7, "output_tokens": 3}
        return json.dumps({"type": "assistant", "message": {"id": "m1", "usage": usage}}).encode() + b"\n"

    def run_locks(self, desk: str = "hermione") -> list:
        folder = self.office / "runs" / desk
        return sorted(name for name in os.listdir(folder) if name.endswith(".lock")) if folder.exists() else []

    def orphan(self, run_id: str, output: bytes, desk: str = "hermione",
               launched_at: int = NOW - config.RUNNING_WINDOW_SECONDS - 600) -> Path:
        """A run whose launcher was killed and whose process has ended since: its launch has no usage, and its
        output and its run lock file, which no process holds now, are left in the runs folder."""
        capacity.record_launch(self.conn, desk, run_id, "opus" if desk == "hermione" else "haiku", now=launched_at)
        folder = self.office / "runs" / desk
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.write_file(folder / f"{run_id}.out", output)
        return self.write_file(folder / run_desk.run_lock_name(run_id), "")

    def locks_seen(self, desk: str, cost: float = 0.25):
        """A desk process that notes the run lock files it inherited and whether each is held, then reports cost."""
        self.seen_locks = []

        def desk_process(argv, **kwargs):
            inherited = {os.fstat(fd).st_ino for fd in kwargs["pass_fds"]}
            for name in self.run_locks(desk):
                path = self.office / "runs" / desk / name
                with open(path, "rb") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held = False
                    except BlockingIOError:
                        held = True
                self.seen_locks.append((name, os.stat(path).st_ino in inherited, held))
            os.write(kwargs["stdout"], self.reported(cost))
            return subprocess.CompletedProcess(argv, 0)

        return desk_process

    def test_each_run_holds_its_own_lock_and_hands_it_to_its_process_until_its_usage_is_in(self):
        owl_id, _ = self.request("hermione")
        with fake_children(self.locks_seen("hermione")):
            result = run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        self.assertEqual(self.seen_locks, [(f"{result['run_id']}.lock", True, True)])
        self.assertEqual((result["cost_usd"], self.run_locks()), (0.25, []))
        # A run killed by a signal records its usage at its budget, so its lock goes as well.
        second, _ = self.request("hermione")

        def interrupted(argv, **kwargs):
            os.write(kwargs["stdout"], self.streamed())
            raise KeyboardInterrupt

        with fake_children(interrupted), self.assertRaises(KeyboardInterrupt):
            run_desk.run(self.conn, "hermione", second, now=NOW)
        self.assertEqual(self.run_locks(), [])
        self.assertEqual([launch["metric_id"] is not None for launch in capacity.list_launches(self.conn, "hermione")],
                         [True, True])
        # A run whose process never started spent nothing: no lock is left, and only the running window holds it.
        third, _ = self.request("hermione")
        with mock.patch.object(run_desk, "start_child", side_effect=OSError("no such binary")), \
                self.assertRaises(OSError):
            run_desk.run(self.conn, "hermione", third, now=NOW)
        self.assertEqual(self.run_locks(), [])
        later = NOW + config.RUNNING_WINDOW_SECONDS + 1
        self.assertEqual(run_desk.reconcile_launches(self.conn, "hermione", later), [])
        self.assertEqual(run_desk.cap_status(self.conn, "hermione", later)["spend_held_usd"], 0.0)

    def test_a_run_whose_usage_was_not_recorded_holds_its_budget_until_the_next_cap_check_records_it(self):
        budget = float(config.MAX_BUDGET_USD["hermione"])
        owl_id, _ = self.request("hermione")
        with fake_children(self.locks_seen("hermione", 0.25)), self.assertRaises(StoreError), \
                mock.patch.object(capacity, "record_launch_usage", side_effect=StoreError("the store is busy")):
            run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        [launch] = capacity.list_launches(self.conn, "hermione")
        self.assertEqual(self.run_locks(), [f"{launch['run_id']}.lock"])
        # Long past the running window its budget is still held, and the next launch decision records what it said.
        later = NOW + config.RUNNING_WINDOW_SECONDS + 600
        self.assertEqual(run_desk.cap_status(self.conn, "hermione", later)["spend_held_usd"], budget)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "hermione", later))
        status = run_desk.cap_status(self.conn, "hermione", later)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], self.run_locks()), (0.25, 0.0, []))

    def test_an_ended_orphan_holds_its_budget_until_its_spend_is_recorded(self):
        limit = config.DAILY_SPEND_CAP_USD["hermione"]
        budget = float(config.MAX_BUDGET_USD["hermione"])
        pensieve.add_metric(self.conn, "hermione", "run-earlier", "opus", 1, 1, 0, limit - 3.0, 10, ts=NOW - 60)
        cut_short, finished, going = ("run-" + digit * 16 for digit in "abc")
        self.orphan(cut_short, self.streamed())
        self.orphan(finished, self.reported(0.3))
        still = self.orphan(going, self.streamed())
        # All three are long past the running window. Each lock file left holds its run's budget.
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_held_usd"], status["reached"]), (3 * budget, "spend"))
        with open(still, "rb") as process:
            fcntl.flock(process, fcntl.LOCK_EX)  # the third run's process still holds its lock
            self.assertEqual(run_desk.over_daily_cap(self.conn, "hermione", NOW), "daily spend cap reached")
            status = run_desk.cap_status(self.conn, "hermione", NOW)
            self.assertEqual((status["spend_used_usd"], status["spend_held_usd"]),
                             (round(limit - 3.0 + budget + 0.3, 6), budget))
            self.assertEqual(self.run_locks(), [f"{going}.lock"])
        # The run cut short is charged its budget as an estimate; the one that finished, what it reported.
        costs = {row["run_id"]: row["cost_usd"] for row in self.conn.execute("SELECT run_id, cost_usd FROM metrics")}
        self.assertEqual((costs[cut_short], costs[finished]), (budget, 0.3))
        [unknown] = [event for event in self.events() if event["kind"] == capacity.SPEND_UNKNOWN_KIND]
        self.assertIn(cut_short, unknown["summary"])
        # Once its process ends, the next launch decision records the third too, and nothing is held.
        self.assertEqual(run_desk.reconcile_launches(self.conn, "hermione", NOW), [going])
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], status["reached"]),
                         (round(limit - 3.0 + 2 * budget + 0.3, 6), 0.0, "spend"))
        self.assertEqual((self.run_locks(), capacity.open_launches(self.conn, "hermione")), ([], []))

    def test_runs_are_settled_only_under_the_desks_launch_lock(self):
        budget = float(config.MAX_BUDGET_USD["hermione"])
        orphan = "run-" + "c" * 16
        self.orphan(orphan, self.reported(0.3))
        with run_desk.launch_lock("hermione"):
            # A launch of Hermione holds it, reading the caps: nothing is settled under it, and its budget stays held.
            self.assertEqual(run_desk.reconcile_launches(self.conn, "hermione", NOW), [])
            self.assertIsNone(run_desk.over_daily_cap(self.conn, "hermione", NOW))
            status = run_desk.cap_status(self.conn, "hermione", NOW)
            self.assertEqual((status["spend_used_usd"], status["spend_held_usd"]), (0.0, budget))
            self.assertEqual(self.run_locks(), [f"{orphan}.lock"])
        self.assertEqual(run_desk.reconcile_launches(self.conn, "hermione", NOW), [orphan])
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], self.run_locks()), (0.3, 0.0, []))

    def test_a_check_before_a_launch_never_settles_a_run_while_the_launch_reads_the_caps(self):
        limit = config.DAILY_SPEND_CAP_USD["hermione"]
        budget = float(config.MAX_BUDGET_USD["hermione"])
        owl_id, _ = self.request("hermione")
        pensieve.add_metric(self.conn, "hermione", "run-earlier", "opus", 1, 1, 0, limit - 1.0, 10, ts=NOW - 60)
        orphan = "run-" + "a" * 16
        lock = self.orphan(orphan, self.streamed())  # cut short, so it is charged its budget, which passes the cap
        settling, go = threading.Event(), threading.Event()
        real_record, real_open = run_desk._record_orphan, capacity.open_launches

        def record(conn, plan, run_fd, row, now):
            # The Owl Post's check holds the orphan's lock and is about to record what it spent.
            settling.set()
            go.wait(3)
            return real_record(conn, plan, run_fd, row, now)

        def open_launches(conn, desk):
            found = real_open(conn, desk)
            if threading.current_thread().name == "launch" and conn.in_transaction:
                # The launch's cap snapshot has begun: the check finishes settling before the launch looks for locks.
                go.set()
                deadline = time.monotonic() + 10
                while lock.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            return found

        with fake_children(self.held_desk) as started, \
                mock.patch.object(run_desk, "_record_orphan", side_effect=record), \
                mock.patch.object(capacity, "open_launches", side_effect=open_launches):
            check = Background(self.db_path, lambda conn: run_desk.over_daily_cap(conn, "hermione", NOW),
                               name="check")
            self.assertTrue(settling.wait(60))
            self.release.set()
            launch = Background(self.db_path, lambda conn: run_desk.run(conn, "hermione", owl_id, now=NOW),
                                name="launch").join()
            check.join()
        self.assertIsNone(check.error)
        self.assertIsInstance(launch.error, run_desk.Capped)
        self.assertEqual(str(launch.error), "daily spend cap reached")
        self.assertEqual(started.call_count, 0)
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], status["reached"]),
                         (round(limit - 1.0 + budget, 6), 0.0, "spend"))

    def test_the_caps_never_miss_a_run_settled_while_they_are_read(self):
        limit = config.DAILY_SPEND_CAP_USD["hermione"]
        budget = float(config.MAX_BUDGET_USD["hermione"])
        pensieve.add_metric(self.conn, "hermione", "run-earlier", "opus", 1, 1, 0, limit - 1.0, 10, ts=NOW - 60)
        run_id = "run-" + "b" * 16  # still going, long past the running window, its lock file there
        lock = self.orphan(run_id, b"")
        settled = []
        real_open = capacity.open_launches

        def open_launches(conn, desk):
            found = real_open(conn, desk)
            if conn.in_transaction and not settled:
                # Its launcher records its usage and removes its lock file after this snapshot began.
                other = db.connect(self.db_path)
                try:
                    capacity.record_launch_usage(other, run_id, 1, 1, 0, 1.5, 10, now=NOW)
                finally:
                    other.close()
                os.unlink(lock)
                settled.append(run_id)
            return found

        with mock.patch.object(capacity, "open_launches", side_effect=open_launches), \
                mock.patch.object(cli, "_clock", return_value=NOW):
            rows = {row["desk"]: row for row in cli._desk_caps(self.conn, mock.Mock())}
        self.assertEqual(settled, [run_id])
        status = rows["hermione"]
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], status["reached"]),
                         (round(limit - 1.0, 6), budget, "spend"))
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"], status["reached"]),
                         (round(limit + 0.5, 6), 0.0, "spend"))

    def test_castle_desk_caps_and_a_bump_count_what_runs_still_going_hold(self):
        limit = config.DAILY_SPEND_CAP_USD["hermione"]
        budget = float(config.MAX_BUDGET_USD["hermione"])
        pensieve.add_metric(self.conn, "hermione", "run-earlier", "opus", 1, 1, 0, limit - budget + 0.5, 10,
                            ts=NOW - 60)
        capacity.record_launch(self.conn, "hermione", "run-" + "d" * 16, "opus", now=NOW - 60)  # going in a slot
        launcher = run_desk.cap_status(self.conn, "hermione", NOW)
        with mock.patch.object(cli, "_clock", return_value=NOW):
            rows = {row["desk"]: row for row in cli._desk_caps(self.conn, mock.Mock())}
            bumped = cli._desk_cap(self.conn, mock.Mock(desk="hermione", runs=None, spend=5.0))
        self.assertEqual(rows["hermione"], launcher)
        self.assertEqual((launcher["spend_held_usd"], launcher["reached"]), (budget, "spend"))
        self.assertEqual((rows["ron"]["spend_held_usd"], rows["ron"]["reached"]), (0.0, None))
        caps = bumped["caps"]
        self.assertEqual((caps["spend_limit_usd"], caps["spend_held_usd"], caps["reached"]),
                         (limit + 5.0, budget, None))


class OrphanRunTests(FleetCase):
    """A launcher killed outright leaves its desk process running, holding the run's own lock it inherited. Real
    processes: a Python holder and /bin/sleep, never a desk CLI."""

    def test_a_killed_launchers_run_holds_its_budget_while_its_process_runs_then_its_spend_is_recorded(self):
        run_id = "run-" + "c" * 16
        capacity.record_launch(self.conn, "hermione", run_id, "opus", now=NOW - config.RUNNING_WINDOW_SECONDS - 600)
        holder = ("import os, subprocess, sys\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from fleet import config, run_desk\n"
                  "config.OFFICE_ROOT = sys.argv[2]\n"
                  "with run_desk.run_lock('hermione', sys.argv[3]) as own:\n"
                  "    own.keep = True\n"
                  "    child = subprocess.Popen(['/bin/sleep', '60'], pass_fds=(own.fd,),\n"
                  "                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "    print(child.pid, flush=True)\n"
                  "    os.kill(os.getpid(), 9)\n")
        root = str(Path(__file__).resolve().parents[1])
        done = subprocess.run(["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty",
                               "-c", holder, root, config.OFFICE_ROOT, run_id], capture_output=True, timeout=60,
                              check=False)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
        process = int(done.stdout.decode().strip())
        budget = float(config.MAX_BUDGET_USD["hermione"])
        try:
            # Its process still runs, long past the running window: its budget stays held and nothing is recorded.
            self.assertEqual(run_desk.reconcile_launches(self.conn, "hermione", NOW), [])
            self.assertEqual(run_desk.cap_status(self.conn, "hermione", NOW)["spend_held_usd"], budget)
            result = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.4, "result": "ok"}
            self.write_file(self.office / "runs" / "hermione" / f"{run_id}.out", json.dumps(result) + "\n")
        finally:
            os.kill(process, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while not run_desk.reconcile_launches(self.conn, "hermione", NOW):
            self.assertLess(time.monotonic(), deadline, "the run's lock was never freed after its process ended")
            time.sleep(0.1)
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_used_usd"], status["spend_held_usd"]), (0.4, 0.0))
        self.assertEqual(os.listdir(self.office / "runs" / "hermione"), [f"{run_id}.out"])


class SlotFolderTests(RunDeskCase):
    def profile(self, argv: list, desk: str) -> str:
        [table] = [argv[index + 1] for index, arg in enumerate(argv)
                   if arg == "-c" and argv[index + 1].startswith(f"permissions.fleet-{desk}=")]
        return table

    def test_each_slot_has_its_own_folders_and_the_profile_grants_a_run_only_its_own(self):
        owl_id, _ = self.request("harry")  # no worktree: the run works in its slot's work folder
        with mock.patch.dict(config.RUN_SLOTS, {"harry": 2}), \
                mock.patch.object(run_desk, "user_temp_dir", return_value=USER_TEMP):
            plans = [run_desk.build_plan(self.conn, "harry", owl_id, slot=index) for index in (0, 1)]
        temps = [f"{USER_TEMP}/hogwarts-harry", f"{USER_TEMP}/hogwarts-harry.slot1"]
        works = [f"{self.castle}/desks/harry/work", f"{self.castle}/desks/harry/work.slot1"]
        tables = []
        for index, plan in enumerate(plans):
            with self.subTest(slot=index):
                argv, table = plan["argv"], self.profile(plan["argv"], "harry")
                tables.append(table)
                self.assertEqual((plan["slot"], plan["temp"], plan["cwd"], argv[argv.index("-C") + 1]),
                                 (index, temps[index], works[index], works[index]))
                writes = [entry for entry in table.split(", ") if entry.endswith('="write"') or '"."="write"' in entry]
                self.assertEqual(writes, ['":workspace_roots"={"."="write"}',
                                          f'"{self.castle}/desks/harry/outbox"="write"', f'"{temps[index]}"="write"'])
                self.assertNotIn(temps[1 - index] + '"', table)
                self.assertNotIn(works[1 - index], table)
                self.assertEqual(plan["env"]["TMPDIR"], temps[index])
                policy = next(item for item in argv if item.startswith("shell_environment_policy.set="))
                self.assertIn(f'TMPDIR="{temps[index]}"', policy)
                self.assertIn(f'TEST_TMP_ROOT="{temps[index]}"', policy)
                self.assertIn(f'xcrun_db="{temps[index]}/xcrun_db"', policy)
        # The two profiles are the same but for the temp folder, so every deny is exactly as strict in both.
        self.assertEqual(tables[0].replace(temps[0], "TEMP"), tables[1].replace(temps[1], "TEMP"))
        self.assertIn(f'"{self.office}"="deny"', tables[1])
        self.assertIn(f'"{config.SHARED_TEMP_ROOT}"="deny"', tables[1])

    def test_a_run_in_slot_1_empties_only_its_own_temp_folder(self):
        user_temp = self.tmp / "usertemp"
        for name in ("hogwarts-harry", "hogwarts-harry.slot1"):
            (user_temp / name).mkdir(parents=True)
            self.write_file(user_temp / name / "stale.txt", "from an earlier run\n")
        self.enable("harry")
        owl_id, task_id = self.request("harry")
        seen = {}

        def desk(argv, **kwargs):
            seen.update(cwd=kwargs["cwd"], tmpdir=kwargs["env"]["TMPDIR"], locks=lock_names(self.office,
                                                                                             kwargs["pass_fds"]))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.dict(config.RUN_SLOTS, {"harry": 2}), \
                mock.patch.object(run_desk, "user_temp_dir", return_value=str(user_temp)), fake_children(desk):
            with run_desk.slot_lock("harry", 0):  # another run of Harry's holds slot 0
                result = run_desk.run(self.conn, "harry", owl_id, now=NOW)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(seen, {"cwd": f"{self.castle}/desks/harry/work.slot1",
                                "tmpdir": f"{user_temp}/hogwarts-harry.slot1",
                                "locks": sorted(["desk-harry.slot1.lock", config.UPDATE_LOCK,
                                                 run_desk.task_lock_name(task_id)])})
        self.assertEqual(os.listdir(user_temp / "hogwarts-harry.slot1"), [])
        self.assertEqual(os.listdir(user_temp / "hogwarts-harry"), ["stale.txt"])

    def test_moody_runs_read_only_in_either_slot(self):
        for index in (0, 1):
            with self.subTest(slot=index):
                plan = run_desk.build_plan(self.conn, "moody", slot=index)
                table = self.profile(plan["argv"], "moody")
                self.assertIsNone(plan["temp"])
                self.assertEqual(plan["cwd"], f"{self.castle}/desks/moody/{'work' if index == 0 else 'work.slot1'}")
                self.assertNotIn('="write"', table)
                self.assertIn('":workspace_roots"={"."="read"}', table)


class SlotLockTests(RunDeskCase):
    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_kit_gives_the_reviewers_two_slots_and_every_other_desk_one(self):
        self.assertEqual(kit_setting("RUN_SLOTS"), {"hermione": 2, "ron": 1, "portrait": 1, "harry": 1, "moody": 2})
        self.assertEqual({desk: run_desk.run_slots(desk) for desk in config.HEADLESS_DESKS},
                         {"hermione": 2, "ron": 1, "portrait": 1, "harry": 1, "moody": 2})
        with mock.patch.object(config, "RUN_SLOTS", {}):
            self.assertEqual(run_desk.run_slots("moody"), 1)

    def test_a_desk_with_one_slot_keeps_the_old_lock_and_folders_and_runs_one_at_a_time(self):
        self.assertEqual((run_desk.slot_lock_name("ron", 0), run_desk.work_dir("harry")),
                         ("desk-ron.lock", f"{self.castle}/desks/harry/work"))
        with mock.patch.object(run_desk, "user_temp_dir", return_value=USER_TEMP):
            self.assertEqual(run_desk.desk_temp_dir("harry"), f"{USER_TEMP}/hogwarts-harry")
        with run_desk.desk_lock("ron", wait=False) as slot:
            self.assertEqual((slot.desk, slot.index), ("ron", 0))
            self.assertEqual(os.fstat(slot.fd).st_ino, os.stat(self.office / "locks" / "desk-ron.lock").st_ino)
            with self.assertRaises(safefs.Busy):
                with run_desk.desk_lock("ron", wait=False):
                    pass
        self.enable("ron")
        owl_id, _ = self.request("ron")
        with mock.patch.object(config, "DESK_LOCK_WAIT_SECONDS", 0), run_desk.desk_lock("ron", wait=False), \
                fake_children() as started:
            code, _, _ = self.main("ron", "--owl", owl_id)
        self.assertEqual(code, 1)
        started.assert_not_called()
        self.assertEqual(len(self.events_of("rundesk.lock-wait")), 1)
        self.assertFalse(any(name.startswith("desk-ron.slot") for name in os.listdir(self.office / "locks")))

    def test_a_run_takes_any_free_slot_and_waits_only_when_every_slot_is_held(self):
        with run_desk.desk_lock("moody", wait=False) as first, run_desk.desk_lock("moody", wait=False) as second:
            self.assertEqual((first.index, second.index), (0, 1))
            with self.assertRaisesRegex(safefs.Busy, "every run slot of moody is held"):
                with run_desk.desk_lock("moody", wait=False):
                    pass
        with run_desk.slot_lock("moody", 0), run_desk.desk_lock("moody", wait=False) as taken:
            self.assertEqual(taken.index, 1)
        # A run that may wait takes a slot as soon as another run lets one go.
        held, release = threading.Event(), threading.Event()

        def other_run() -> None:
            with run_desk.slot_lock("moody", 1):
                held.set()
                release.wait(30)

        other = threading.Thread(target=other_run)
        other.start()
        self.assertTrue(held.wait(30))
        threading.Timer(0.3, release.set).start()
        try:
            with run_desk.slot_lock("moody", 0), mock.patch.object(config, "DESK_LOCK_WAIT_SECONDS", 30), \
                    run_desk.desk_lock("moody", wait=True) as waited:
                self.assertEqual(waited.index, 1)
        finally:
            release.set()
            other.join(30)

    def test_slots_and_held_slots_are_checked(self):
        for bad in (0, 9, True, "2", None):
            with self.subTest(slots=bad), mock.patch.dict(config.RUN_SLOTS, {"moody": bad}), \
                    self.assertRaisesRegex(FleetError, "RUN_SLOTS"):
                run_desk.run_slots("moody")
        for bad in (-1, 8, True, "1", None):
            with self.subTest(slot=bad), self.assertRaisesRegex(FleetError, "invalid run slot"):
                run_desk.build_plan(self.conn, "moody", slot=bad)
        self.enable("moody")
        owl_id, _ = self.request("moody")
        with run_desk.slot_lock("hermione", 1) as theirs:
            for held in (True, 1, theirs):
                with self.subTest(held=held), fake_children() as started, \
                        self.assertRaisesRegex(FleetError, "lock_held must be the run slot of this desk"):
                    run_desk.run(self.conn, "moody", owl_id, now=NOW, lock_held=held)
                started.assert_not_called()

    def test_a_held_slot_is_handed_to_the_process_even_when_left_out_of_keep_fds(self):
        self.enable("moody")
        owl_id, _ = self.request("moody")
        seen = {}

        def desk(argv, **kwargs):
            seen["locks"] = lock_names(self.office, kwargs["pass_fds"])
            return subprocess.CompletedProcess(argv, 0)

        with run_desk.slot_lock("moody", 1) as slot, fake_children(desk):
            run_desk.run(self.conn, "moody", owl_id, now=NOW, lock_held=slot)
        self.assertEqual(seen["locks"], sorted(["desk-moody.slot1.lock", config.UPDATE_LOCK]))
