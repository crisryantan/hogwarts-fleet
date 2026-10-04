"""Review rounds through the review script: one review per task at a time, nothing waits in line, a queued
review is superseded by the next commit, and a fourth round waits for Ryan's allow-round. Reviewer runs
are faked at run_desk.run, or at subprocess.run under the real run_desk.run; no reviewer ever runs."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import threading
import time
from unittest import mock

from hogwarts import capacity, db, owlery, pensieve
from hogwarts.errors import ConflictError
from tests.support import NOW

from fleet import config, review, run_desk, tools
from fleet.safefs import FleetError
from tests_fleet.test_review_loop import LoopCase, REPO_ID


class ReviewRoundTests(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        clock = mock.patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.enable("moody")

    def commit(self, text: str) -> str:
        self.write_file(self.repo / "fix.txt", text + "\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def own_review(self, task_id: str = None, verdict: str = "CHANGES") -> dict:
        with self.fake_reviewer(verdict):
            if task_id is None:
                return review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
            return review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)

    def round_cap_events(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.round-cap"]

    def test_a_capped_review_waits_and_the_next_commit_supersedes_it(self):
        first_sha = self.commit("first try")
        with mock.patch.object(run_desk, "over_daily_cap", return_value="daily run cap reached"), \
                mock.patch.object(run_desk, "report_cap") as reported, \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("a capped reviewer ran")):
            with self.assertRaisesRegex(run_desk.Capped, "waits as request"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        reported.assert_called_once()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [waiting] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((waiting["sha"], waiting["round"], waiting["waiting"]), (first_sha, 1, True))
        self.assertEqual([row["id"] for row in capacity.waiting_requests(self.conn, "moody")], [waiting["request_id"]])
        second_sha = self.commit("second try")
        result = self.own_review(task["id"], verdict="PASS")
        self.assertEqual((result["sha"], result["round"], result["superseded"]), (second_sha, 1, [waiting["request_id"]]))
        old = owlery.get_request(self.conn, waiting["request_id"])
        self.assertEqual((old["outcome"], old["reason"]), ("deferred", "conflict"))
        reviewed = [row[0] for row in self.conn.execute("SELECT sha FROM review_passes")]
        self.assertEqual(reviewed, [second_sha])
        self.assertTrue(owlery.has_pass(self.conn, REPO_ID, second_sha))
        self.assertEqual(capacity.waiting_requests(self.conn, "moody"), [])
        [old_owl] = [owl for owl in owlery.request_owls(self.conn, waiting["request_id"]) if owl["kind"] == "request"]
        self.assertIsNotNone(old_owl["acked_at"])

    def test_round_four_waits_for_allow_round_and_round_five_is_refused_again(self):
        first = self.own_review()
        task_id = first["task_id"]
        for text in ("round two", "round three"):
            self.commit(text)
            self.own_review(task_id)
        self.commit("round four")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("round four ran")):
            with self.assertRaisesRegex(FleetError, "review round 4"):
                self.own_review(task_id)
        [event] = self.round_cap_events()
        self.assertEqual((event["verdict"], event["desk"]), ("headmaster", "ryan-claude-1"))
        self.assertIn(f"task {task_id} asked for review round 4", event["summary"])
        self.assertIn(f"castle task allow-round {task_id}", event["summary"])
        self.assertEqual(len(capacity.review_rounds(self.conn, task_id)), 3)
        capacity.allow_round(self.conn, task_id)
        fourth = self.own_review(task_id)
        self.assertEqual((fourth["round"], fourth["verdict"]), (4, "CHANGES"))
        self.commit("round five")
        with self.assertRaisesRegex(FleetError, "review round 5"):
            self.own_review(task_id)
        self.assertEqual([event["summary"].split(",")[0] for event in self.round_cap_events()],
                         [f"task {task_id} asked for review round 4", f"task {task_id} asked for review round 5"])

    def failed_run(self, task_id: str, cap_source=None, error=FleetError) -> None:
        """A reviewer run that started and ended without a verdict: a crash, or a vendor's own limit."""
        def run(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            on_start()
            return {"desk": desk, "run_id": "run-" + "c" * 16, "exit_code": 1, "cap_source": cap_source}
        with mock.patch.object(run_desk, "run", side_effect=run):
            with self.assertRaises(error):
                if task_id is None:
                    review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
                else:
                    review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)

    def test_crashed_and_vendor_limited_rounds_do_not_count(self):
        self.commit("first try")
        self.failed_run(None)
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        task_id = task["id"]
        self.failed_run(task_id, cap_source="claude_plan")
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task_id)], [False, False])
        first = self.own_review(task_id)
        self.assertEqual(first["round"], 1)
        for number, text in ((2, "round two"), (3, "round three")):
            self.commit(text)
            self.failed_run(task_id)
            self.assertEqual(self.own_review(task_id)["round"], number)
        self.commit("round four")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("round four ran")):
            with self.assertRaisesRegex(FleetError, "review round 4"):
                self.own_review(task_id)
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task_id)],
                         [False, False, True, False, True, False, True])

    def test_a_vendor_limit_on_the_reviewer_is_named_and_never_offered_a_bump(self):
        self.commit("my fix")
        def limited(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            on_start()
            return {"desk": desk, "run_id": "run-" + "b" * 16, "exit_code": 1, "cap_source": "codex_plan"}
        with mock.patch.object(run_desk, "run", side_effect=limited):
            with self.assertRaisesRegex(FleetError, "cap_source codex_plan.*does not lift it"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)

    def test_a_verdict_recorded_before_publishing_failed_still_uses_up_its_round(self):
        first = self.own_review()
        task_id = first["task_id"]
        for text in ("round two", "round three"):
            self.commit(text)
            self.own_review(task_id)
        allowance = capacity.allow_round(self.conn, task_id)
        fourth_sha = self.commit("round four")
        with mock.patch.object(review, "_castle_task_file", side_effect=FleetError("the castle disk is full")):
            with self.assertRaisesRegex(FleetError, "disk is full"):
                self.own_review(task_id)
        fourth = capacity.review_rounds(self.conn, task_id)[-1]
        self.assertEqual((fourth["sha"], fourth["round"], fourth["allowance_id"]), (fourth_sha, 4, allowance["id"]))
        self.assertEqual((fourth["has_verdict"], fourth["verdict"], fourth["counts"]), (True, "CHANGES", True))
        self.assertEqual((fourth["request_phase"], fourth["reviewer_task_status"]), ("running", "closed"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM review_passes WHERE sha = ?",
                                           (fourth_sha,)).fetchone()[0], 1)
        self.commit("round five")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("round five ran")):
            with self.assertRaisesRegex(FleetError, "review round 5"):
                self.own_review(task_id)
        self.assertTrue(capacity.allow_round(self.conn, task_id)["created"])

    def moody_active(self) -> list:
        return [task["id"] for task in pensieve.list_tasks(self.conn, desk="moody", status="active")]

    def test_a_failure_right_after_the_reviewer_task_starts_still_closes_it(self):
        # The store is busy when the request moves to claimed: the task started, so it must close.
        self.commit("first try")
        real_advance, calls = owlery.advance, []

        def busy_once(conn, request_id, to_phase, detail=None, now=None):
            if to_phase == "claimed" and not calls:
                calls.append(to_phase)
                raise ConflictError("database is busy, try again")
            return real_advance(conn, request_id, to_phase, detail=detail, now=now)

        with mock.patch.object(owlery, "advance", side_effect=busy_once):
            with self.assertRaisesRegex(ConflictError, "database is busy"):
                self.own_review()
        self.assertEqual(self.moody_active(), [])
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [dead] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((dead["has_verdict"], dead["reviewer_task_status"], dead["counts"]), (False, "closed", False))
        result = self.own_review(task["id"], verdict="PASS")
        self.assertEqual((result["round"], result["verdict"]), (1, "PASS"))

    def test_a_cleanup_that_fails_keeps_the_real_error_and_the_next_review_closes_the_task(self):
        self.commit("first try")
        with mock.patch.object(pensieve, "close_task", side_effect=ConflictError("database is busy, try again")):
            self.failed_run(None)
        [stranded] = self.moody_active()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [dead] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((dead["reviewer_task_status"], dead["counts"]), ("active", True))
        self.commit("second try")
        result = self.own_review(task["id"])
        self.assertEqual((result["round"], result["verdict"]), (1, "CHANGES"))
        self.assertEqual(pensieve.get_task(self.conn, stranded)["close_reason"], "superseded")
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task["id"])], [False, True])
        [event] = [event for event in self.events() if event["kind"] == "review.recovered"]
        self.assertEqual((event["desk"], event["verdict"]), ("moody", "routine"))
        self.assertIn(f"closing moody's task {stranded}", event["summary"])
        self.assertIn("its round does not count", event["summary"])

    def test_a_review_killed_before_cleanup_frees_its_round_for_the_next_review(self):
        # A kill (SIGKILL, or a SIGTERM in an older fleet) skips every finally: round three is left with
        # moody's task active. The next review closes it before it opens its own round, so the cap still fits.
        first = self.own_review()
        task_id = first["task_id"]
        self.commit("round two")
        self.own_review(task_id)
        self.commit("round three")
        with mock.patch.object(review, "_finish_reviewer_task"):
            self.failed_run(task_id)
        self.assertEqual(len(self.moody_active()), 1)
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task_id)], [True, True, True])
        result = self.own_review(task_id)
        self.assertEqual((result["round"], result["verdict"]), (3, "CHANGES"))
        self.assertEqual(self.moody_active(), [])
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task_id)],
                         [True, True, False, True])

    def test_a_review_killed_after_its_verdict_keeps_the_round_when_recovered(self):
        self.commit("first try")
        with mock.patch.object(review, "_castle_task_file", side_effect=SystemExit(143)), \
                mock.patch.object(review, "_finish_reviewer_task"):
            with self.assertRaises(SystemExit):
                self.own_review()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.commit("second try")
        self.assertEqual(self.own_review(task["id"])["round"], 2)
        [event] = [event for event in self.events() if event["kind"] == "review.recovered"]
        self.assertIn("its recorded verdict still counts", event["summary"])
        self.assertEqual(self.moody_active(), [])

    def test_a_signal_during_the_run_closes_the_reviewer_task(self):
        # The fleet command turns SIGTERM and SIGHUP into SystemExit, which the review cleans up after.
        self.commit("first try")

        def killed(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            on_start()
            raise SystemExit(143)
        with mock.patch.object(run_desk, "run", side_effect=killed):
            with self.assertRaises(SystemExit):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(self.moody_active(), [])
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task["id"])], [False])

    def head_and_evidence(self, task_id: str) -> tuple:
        head = self.git("rev-parse", "HEAD", cwd=config.worktree_dir(task_id))
        evidence = (self.castle / "tasks" / task_id / "evidence.md").read_text().splitlines()[0]
        return head, evidence

    def recovered_events(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.recovered"]

    def test_a_second_review_of_the_same_task_is_refused_at_once_and_changes_nothing(self):
        first_sha = self.commit("first try")
        running, release, seen, results, errors = threading.Event(), threading.Event(), {}, {}, {}

        def slow_reviewer(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            on_start()
            running.set()
            self.assertTrue(release.wait(30), "the test never let the first review finish")
            owl = next(item for item in owlery.inbox(conn, desk, include_acked=True) if item["id"] == owl_id)
            author = owlery.get_request(conn, owl["request_id"])["parent_task_id"]
            seen["head"], seen["evidence"] = self.head_and_evidence(author)
            run_id = "run-" + "d" * 16
            folder = self.office / "runs" / desk
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.write_file(folder / f"{run_id}-last-message.md",
                            f"REVIEW {author} @ {seen['head']}\nAC\nBLOCKING\nNON-BLOCKING\nVERDICT: CHANGES\n")
            return {"desk": desk, "run_id": run_id, "exit_code": 0}

        def first_review() -> None:
            conn = db.connect(self.db_path)
            try:
                results["first"] = review.review_own(conn, str(self.repo), title="my own fix", fetch=False)
            except Exception as exc:  # noqa: BLE001 - the main thread reports it
                errors["first"] = exc
            finally:
                conn.close()

        with mock.patch.object(run_desk, "run", side_effect=slow_reviewer):
            first = threading.Thread(target=first_review)
            first.start()
            try:
                self.assertTrue(running.wait(30), "the first review never started its reviewer")
                [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
                self.commit("second try")
                before = (self.head_and_evidence(task["id"]), capacity.review_rounds(self.conn, task["id"]),
                          self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                for attempt in (lambda: review.review_own(self.conn, str(self.repo), task_id=task["id"], fetch=False),
                                lambda: tools.run(self.conn, tools.build_parser().parse_args(["verify", task["id"]]))):
                    with self.assertRaisesRegex(FleetError, "^a review of this task is already running; run it again"
                                                            " when it ends$"):
                        attempt()
                after = (self.head_and_evidence(task["id"]), capacity.review_rounds(self.conn, task["id"]),
                         self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
                self.assertEqual(after, before)
            finally:
                release.set()
                first.join(60)
        self.assertFalse(first.is_alive(), "the first review never finished")
        self.assertEqual(errors, {})
        self.assertEqual((seen["head"], seen["evidence"]), (first_sha, f"EVIDENCE {task['id']} @ {first_sha}"))
        self.assertEqual((results["first"]["sha"], results["first"]["round"], results["first"]["verdict"]),
                         (first_sha, 1, "CHANGES"))
        second = self.own_review(task["id"])
        self.assertEqual((second["round"], second["verdict"]), (2, "CHANGES"))
        self.assertEqual(self.head_and_evidence(task["id"]),
                         (second["sha"], f"EVIDENCE {task['id']} @ {second['sha']}"))

    def test_a_review_while_the_reviewer_is_busy_is_queued_then_superseded_by_the_next_review(self):
        first_sha = self.commit("first try")
        not_run = mock.patch.object(run_desk, "run", side_effect=AssertionError("ran while moody was busy"))
        with run_desk.desk_lock("moody", wait=False), not_run:  # another review's run holds moody's desk
            queued = review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(queued["queued"], "queued: moody is busy; run fleet review again later")
        self.assertEqual((queued["sha"], queued["round"], queued["verdict"], queued["review"]), (first_sha, 1, None, None))
        task_id = queued["task_id"]
        [row] = capacity.review_rounds(self.conn, task_id)
        self.assertEqual((row["request_id"], row["waiting"], row["has_verdict"]), (queued["request_id"], True, False))
        self.assertEqual([item["id"] for item in capacity.waiting_requests(self.conn, "moody")], [queued["request_id"]])
        other = pensieve.create_task(self.conn, "moody", "a task of moody's own")
        pensieve.start_task(self.conn, other["id"])
        second_sha = self.commit("second try")
        with not_run:  # moody's desk lock is free, but moody has an active task
            again = review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)
        self.assertEqual((again["sha"], again["round"], again["superseded"], again["queued"]),
                         (second_sha, 1, [queued["request_id"]], queued["queued"]))
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "active")
        self.assertEqual(self.recovered_events(), [])
        pensieve.close_task(self.conn, other["id"], "superseded")
        third_sha = self.commit("third try")
        done = self.own_review(task_id, verdict="PASS")
        self.assertEqual((done["sha"], done["round"], done["verdict"], done["superseded"], done["queued"]),
                         (third_sha, 1, "PASS", [again["request_id"]], None))
        for old in (queued, again):
            request = owlery.get_request(self.conn, old["request_id"])
            self.assertEqual((request["outcome"], request["reason"]), ("deferred", "conflict"))
        self.assertEqual([(row["sha"], row["counts"]) for row in capacity.review_rounds(self.conn, task_id)],
                         [(first_sha, False), (second_sha, False), (third_sha, True)])
        self.assertEqual(capacity.waiting_requests(self.conn, "moody"), [])

    def test_sigterm_during_the_run_keeps_the_launch_counted_and_the_cap_enforced(self):
        # The real run_desk.run, with only the reviewer's own process faked: SIGTERM arrives while it runs.
        self.commit("first try")
        real_run, launched = subprocess.run, []

        def interrupted(argv, *args, **kwargs):
            if argv[0] != config.CODEX_BIN:
                return real_run(argv, *args, **kwargs)
            launched.append(argv)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            raise AssertionError("SIGTERM did not end the run")

        task_id = None
        with mock.patch.dict(config.DAILY_RUN_CAP, {"moody": 2}), \
                mock.patch.object(subprocess, "run", side_effect=interrupted):
            for _ in range(2):
                with self.assertRaises(SystemExit) as caught, tools.ended_by_signals():
                    if task_id is None:
                        review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
                    else:
                        review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)
                self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
                task_id = task_id or pensieve.list_tasks(self.conn, desk="ryan-claude-1")[0]["id"]
                self.assertEqual(self.moody_active(), [])
            with self.assertRaisesRegex(run_desk.Capped, "moody is at its fleet daily cap"):
                review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)
        self.assertEqual(len(launched), 2)
        self.assertEqual([launch["metric_id"] for launch in capacity.list_launches(self.conn, "moody")], [None, None])
        self.assertEqual(run_desk.cap_status(self.conn, "moody")["runs_used"], 2)
        rounds = capacity.review_rounds(self.conn, task_id)
        self.assertEqual([(row["counts"], row["waiting"]) for row in rounds], [(False, False), (False, False), (False, True)])

    def test_a_stranded_reviewer_task_is_recovered_only_when_the_desk_lock_is_free(self):
        self.commit("first try")
        with mock.patch.object(review, "_finish_reviewer_task"):  # killed before its cleanup
            self.failed_run(None)
        [stranded] = self.moody_active()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.commit("second try")
        with run_desk.desk_lock("moody", wait=False), \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("ran under a held desk lock")):
            queued = review.review_own(self.conn, str(self.repo), task_id=task["id"], fetch=False)
        self.assertEqual(queued["queued"], "queued: moody is busy; run fleet review again later")
        self.assertEqual(self.moody_active(), [stranded])
        self.assertEqual(self.recovered_events(), [])
        result = self.own_review(task["id"])
        self.assertEqual((result["round"], result["verdict"], result["superseded"]), (1, "CHANGES", [queued["request_id"]]))
        self.assertEqual(pensieve.get_task(self.conn, stranded)["close_reason"], "superseded")
        self.assertEqual(self.moody_active(), [])
        [event] = self.recovered_events()
        self.assertEqual((event["desk"], event["verdict"]), ("moody", "routine"))
        self.assertIn("its round does not count", event["summary"])
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task["id"])], [False, False, True])

    def test_a_round_left_by_a_killed_review_is_not_counted_while_the_reviewer_is_busy(self):
        # Round three was killed before its cleanup, so moody's task is still active with no verdict. A review
        # that finds moody busy cannot close it, but holds the task's lock, so that round is over: the new
        # review queues as round three, with no round-cap event.
        first = self.own_review()
        task_id = first["task_id"]
        self.commit("round two")
        self.own_review(task_id)
        self.commit("round three")
        with mock.patch.object(review, "_finish_reviewer_task"):
            self.failed_run(task_id)
        [stranded] = self.moody_active()
        self.commit("round three again")
        with run_desk.desk_lock("moody", wait=False), \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("ran under a held desk lock")):
            queued = review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)
        self.assertEqual((queued["round"], queued["queued"]), (3, "queued: moody is busy; run fleet review again later"))
        self.assertEqual((self.round_cap_events(), self.moody_active()), ([], [stranded]))
        result = self.own_review(task_id)
        self.assertEqual((result["round"], result["verdict"], result["superseded"]), (3, "CHANGES", [queued["request_id"]]))
        self.assertEqual(pensieve.get_task(self.conn, stranded)["close_reason"], "superseded")
        self.commit("round four")
        with self.assertRaisesRegex(FleetError, "review round 4"):
            self.own_review(task_id)

    def test_allow_round_twice_with_a_queued_round_allows_one_more_round(self):
        first = self.own_review()
        task_id = first["task_id"]
        for text in ("round two", "round three"):
            self.commit(text)
            self.own_review(task_id)
        allowance = capacity.allow_round(self.conn, task_id)
        self.commit("round four")
        with run_desk.desk_lock("moody", wait=False):
            queued = review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)
        self.assertEqual((queued["round"], queued["queued"] is not None), (4, True))
        again = capacity.allow_round(self.conn, task_id)
        self.assertEqual((again["id"], again["created"], again["rounds"]), (allowance["id"], False, 3))
        fourth = self.own_review(task_id)
        self.assertEqual((fourth["round"], fourth["superseded"]), (4, [queued["request_id"]]))
        self.commit("round five")
        with self.assertRaisesRegex(FleetError, "review round 5"):
            self.own_review(task_id)

    def test_the_reviewers_process_inherits_the_task_and_desk_locks(self):
        # A review killed mid-run leaves its reviewer running. That process holds both locks, so no review of
        # the task can move its worktree or evidence, and no review can close its reviewer task, until it ends.
        self.commit("first try")
        real_run, seen = subprocess.run, {}

        def reviewer(argv, *args, **kwargs):
            if argv[0] != config.CODEX_BIN:
                return real_run(argv, *args, **kwargs)
            [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
            locks = {os.stat(self.office / "locks" / name).st_ino: name
                     for name in (f"review-{task['id']}.lock", "desk-moody.lock")}
            seen["inherited"] = sorted(locks.get(os.fstat(fd).st_ino) for fd in kwargs["pass_fds"])
            seen["read_only"] = [fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
                                 for fd in kwargs["pass_fds"]]
            return subprocess.CompletedProcess(argv, 1)

        with mock.patch.object(subprocess, "run", side_effect=reviewer):
            with self.assertRaisesRegex(FleetError, "did not finish cleanly"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.assertEqual(seen, {"inherited": ["desk-moody.lock", f"review-{task['id']}.lock"],
                                "read_only": [True, True]})
