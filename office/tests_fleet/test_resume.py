"""Resume: a build turn that died (gone with no end record, killed as the loops stopped, or ended having done nothing)
starts once more by itself on the same worktree and branch, with a line to the owner, and a second death waits for
him. The start itself is faked at worktree.start_desk, which records the launch the run it starts would record."""
from __future__ import annotations

import json
from unittest import mock

from hogwarts import capacity, ids, owlery, pensieve
from tests.support import NOW

from fleet import config, failover, orchestrator, owl_post, resume, review, run_desk, safefs, worktree
from tests_fleet.support import FleetCase

REPO = "acme/web-app"
SHA = "a" * 40
STARTED = "started harry on owl x"


class ResumeCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        opened = owlery.open_request(self.conn, "mcgonagall", "harry", "build it", body="spec", now=NOW)
        self.task = opened["task"]
        pensieve.set_worktree(self.conn, self.task["id"], f"{ids.WORKTREES_ROOT}/{self.task['id']}")
        self.task = pensieve.start_task(self.conn, self.task["id"], now=NOW)
        self.enable("harry")
        self.clock = NOW
        resume.sweep(self.conn, now=self.clock)  # the first sweep only records when it first ran
        self.starts = []
        patcher = mock.patch.object(worktree, "start_desk", side_effect=self.start)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, conn, task, lock_fd) -> str:
        self.assertEqual(task["id"], self.task["id"])
        with self.assertRaises(safefs.Busy):  # started under the task's review lock, which it hands on
            with run_desk.task_lock(task["id"]):
                pass
        self.starts.append(self.launch(ended=False))
        return STARTED

    def tick(self) -> int:
        self.clock += 10
        return self.clock

    def launch(self, exit_code=0, tokens=(0, 0), ended=True, usage=True) -> str:
        """A run of Harry on the task. ended keeps its end record; usage records its tokens, as run_desk does for a
        run that ended on its own (a Codex run killed as the loops stopped records none)."""
        run_id = f"run-{self.tick():016x}"
        capacity.record_launch(self.conn, "harry", run_id, "gpt", task_id=self.task["id"], now=self.clock)
        if ended:
            with safefs.opened_dir(config.OFFICE_ROOT, "runs", "harry", create=True) as fd:
                run_desk._keep_end(fd, run_id, exit_code, None)
        if ended and usage and exit_code is not None:
            capacity.record_launch_usage(self.conn, run_id, tokens[0], tokens[1], 0, 0.0, 1000, now=self.tick())
        return run_id

    def sweep(self) -> list:
        return resume.sweep(self.conn, now=self.tick())

    def events_of(self, kind: str) -> list:
        return [dict(row) for row in self.conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]


class ResumeTests(ResumeCase):
    def test_a_run_gone_with_no_end_record_resumes_once_with_a_line(self):
        dead = self.launch(ended=False)  # the Mac restarted: no end record, no usage, no process
        self.assertFalse(run_desk.died(capacity.newest_launch(self.conn, self.task["id"])))  # still going, as read
        self.assertEqual(self.sweep(), [{"task_id": self.task["id"], "outcome": "started"}])
        self.assertEqual(len(self.starts), 1)
        [line] = self.events_of(resume.RESUMED_KIND)
        self.assertEqual((line["verdict"], line["task_id"], line["dedupe_key"]),
                         ("headmaster", self.task["id"], f"build:resumed:{dead}"))
        self.assertIn("left no end record", line["summary"])
        self.assertIn("same worktree and branch", line["summary"])
        self.assertIn(resume.RESUMED_KIND, config.PHONE_KINDS)
        with run_desk.task_lock(self.task["id"]):  # the run it started holds the lock it was handed
            self.assertEqual(self.sweep(), [])
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.events_of(resume.RESUMED_KIND)), 1)

    def test_a_codex_run_killed_as_the_loops_stopped_resumes(self):
        self.launch(-9, usage=False)  # SIGTERM unwound the launcher: the child was killed, no usage recorded
        self.assertEqual(self.sweep()[0]["outcome"], "started")
        self.assertIn("was killed", self.events_of(resume.RESUMED_KIND)[0]["summary"])

    def test_a_run_that_died_idle_resumes(self):
        self.launch(None)  # never started
        self.assertEqual(self.sweep()[0]["outcome"], "started")

    def test_a_second_death_waits_for_the_owner(self):
        self.launch(ended=False)
        self.sweep()
        [second] = self.starts
        with safefs.opened_dir(config.OFFICE_ROOT, "runs", "harry", create=True) as fd:
            run_desk._keep_end(fd, second, 1, None)
        capacity.record_launch_usage(self.conn, second, 0, 0, 0, 0.0, 1000, now=self.tick())
        self.assertEqual(self.sweep(), [{"task_id": self.task["id"], "outcome": "waits for the owner"}])
        self.assertEqual(len(self.starts), 1)
        [line] = self.events_of(resume.DEAD_KIND)
        self.assertEqual((line["verdict"], line["dedupe_key"]), ("headmaster", f"build:dead:{second}"))
        self.assertIn(f"fleet build {self.task['id']}", line["summary"])
        self.assertIn(resume.DEAD_KIND, config.PHONE_KINDS)
        self.assertEqual(self.sweep(), [])  # told once
        self.assertEqual(len(self.events_of(resume.DEAD_KIND)), 1)

    def test_a_run_still_going_or_a_review_holding_the_lock_is_left(self):
        self.launch(ended=False)
        with run_desk.task_lock(self.task["id"]):  # its process, or a review, still holds the task's lock
            self.assertEqual(self.sweep(), [])
        self.assertEqual((self.starts, self.events_of(resume.RESUMED_KIND)), ([], []))

    def test_runs_that_finished_are_never_resumed(self):
        for exit_code, tokens in ((0, (0, 0)), (1, (300, 20)), (-1, (300, 20))):
            with self.subTest(exit_code=exit_code, tokens=tokens):
                self.launch(exit_code, tokens)  # clean, failed after work, timed out after work
                self.assertEqual(self.sweep(), [])
        self.assertEqual(self.starts, [])

    def test_a_run_that_handed_off_before_it_died_is_the_review_loops(self):
        self.launch(ended=False)
        owlery.send(self.conn, "harry", "mcgonagall", "result", "handoff", body="HANDOFF", task_id=self.task["id"],
                    request_id=self.task["request_id"], idempotency_key="handoff-1", now=self.tick())
        self.assertIsNotNone(review.latest_result_owl(self.conn, self.task))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.starts, [])

    def test_a_run_from_before_the_resume_first_ran_or_outside_the_window_is_left(self):
        with safefs.opened_dir(config.OFFICE_ROOT, resume.DIR) as fd:
            safefs.write_new(fd, resume.SINCE, json.dumps({"state": "since", "at": self.clock + 100}).encode("ascii"))
        self.launch(ended=False)
        self.assertEqual(self.sweep(), [])
        self.clock += config.RESUME_WINDOW_SECONDS + 200
        with safefs.opened_dir(config.OFFICE_ROOT, resume.DIR) as fd:
            safefs.write_new(fd, resume.SINCE, json.dumps({"state": "since", "at": NOW}).encode("ascii"))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.starts, [])

    def test_a_desk_that_cannot_start_is_told_as_waiting_never_as_started(self):
        dead = self.launch(ended=False)
        (self.office / "desks" / "harry" / config.ENABLED_MARKER).unlink()
        self.assertEqual(self.sweep()[0]["outcome"], "not started")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.events_of(resume.RESUMED_KIND), [])
        [line] = self.events_of(resume.DEAD_KIND)
        self.assertEqual(line["dedupe_key"], f"build:dead:{dead}")
        self.assertIn("harry is not enabled", line["summary"])
        self.enable("harry")
        self.assertEqual(self.sweep(), [])  # told once, never started later by itself

    def test_a_restart_that_ended_before_it_launched_waits_for_the_owner_once(self):
        dead = self.launch(ended=False)
        with mock.patch.object(worktree, "start_desk", return_value=STARTED):  # its run gave up before its launch
            self.assertEqual(self.sweep()[0]["outcome"], "started")
        with run_desk.task_lock(self.task["id"]):  # while the restart waits for a slot it holds the task's lock
            self.assertEqual(self.sweep(), [])
        with mock.patch.object(run_desk, "stop_requested", return_value=True):  # a stop holds it to start it later
            self.assertEqual(self.sweep(), [])
        owl_id = worktree._request_owl(self.conn, self.task)
        with failover.held_state() as state:  # its run waits for a model, which starts it again once one is back
            state["waiting"][owl_id] = {"desk": "harry", "models": [], "first": self.clock, "resumed": None,
                                        "resumes": 0}
        self.assertEqual(self.sweep(), [])
        with failover.held_state() as state:
            del state["waiting"][owl_id]
        self.assertEqual(self.sweep(), [{"task_id": self.task["id"], "outcome": "restart never launched"}])
        [line] = self.events_of(resume.DEAD_KIND)
        self.assertEqual(line["dedupe_key"], f"build:dead:{dead}")
        self.assertIn("ended before it launched", line["summary"])
        self.assertEqual((self.sweep(), self.starts), ([], []))  # told once, never started a third time

    def test_the_owl_post_pass_runs_the_resume(self):
        self.launch(ended=False)
        summary = owl_post.run_pass(self.conn, now=self.tick())
        self.assertEqual(summary["builds_resumed"], [{"task_id": self.task["id"], "outcome": "started"}])


class StreakTests(ResumeCase):
    def test_an_earlier_run_gone_or_killed_counts_toward_the_streak(self):
        self.launch(ended=False)  # gone: a later launch followed it, so it is over
        self.launch(-1)  # timed out with no usage: died idle
        self.assertEqual(run_desk.dead_streak(self.conn, self.task["id"]), 2)
        self.launch(0, (500, 20))
        self.assertEqual(run_desk.dead_streak(self.conn, self.task["id"]), 0)
        self.launch(ended=False)  # the newest, read as going until its task's lock says it is over
        self.assertEqual(run_desk.dead_streak(self.conn, self.task["id"]), 0)
        self.assertEqual(run_desk.dead_streak(self.conn, self.task["id"], over=True), 1)

    def test_her_fix_round_retry_refuses_a_run_the_fleet_already_resumed(self):
        pensieve.record_commit(self.conn, self.task["id"], REPO, SHA, now=self.tick())
        opened = capacity.open_review_round(self.conn, self.task["id"], "hermione", SHA, "review it",
                                            idempotency_key=f"round-{self.tick()}", now=self.clock)
        pensieve.start_task(self.conn, opened["task"]["id"], now=self.clock)
        review.record_round_inputs(self.task["id"], opened["request"]["id"], SHA, review.handoff_digest(None))
        capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, "CHANGES", now=self.tick())
        route = {"action": "route_findings_to_harry", "task_id": self.task["id"],
                 "findings_ref": opened["request"]["id"]}
        dead = self.launch(-1)  # the fix round timed out with no usage
        orchestrator.check_legal(self.conn, route)  # legal before the fleet acts
        with mock.patch.object(worktree, "start_desk", return_value=STARTED):  # its run not recorded yet
            self.assertEqual(self.sweep()[0]["outcome"], "started")
        self.assertEqual(capacity.newest_launch(self.conn, self.task["id"])["run_id"], dead)
        with self.assertRaisesRegex(orchestrator.Invalid, "the fleet already started the build again"):
            orchestrator.check_legal(self.conn, route)
        # A dead run the resume left for Ryan is never restarted by her either: the bound held off, by her own check,
        # the resume's line alone refuses it.
        dead = self.launch(-1)
        self.assertEqual(self.sweep()[0]["outcome"], "waits for the owner")
        with mock.patch.object(run_desk, "dead_streak", return_value=1), \
                self.assertRaisesRegex(orchestrator.Invalid, "or told Ryan it waits for him"):
            orchestrator.check_legal(self.conn, route)
        self.assertTrue(resume.acted_on(self.conn, dead))
