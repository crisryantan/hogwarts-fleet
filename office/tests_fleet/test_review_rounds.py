"""Review rounds through the review script: a capped review waits and the next commit supersedes it,
and a fourth round waits for Ryan's allow-round. Reviewer runs are faked at run_desk.run."""
from __future__ import annotations

import threading
from unittest import mock

from hogwarts import capacity, db, owlery, pensieve
from hogwarts.errors import ConflictError
from tests.support import NOW

from fleet import config, review, run_desk, safefs
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
        def run(conn, desk, owl_id, mcp_job=None, now=None, on_start=None):
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
        def limited(conn, desk, owl_id, mcp_job=None, now=None, on_start=None):
            on_start()
            return {"desk": desk, "run_id": "run-" + "b" * 16, "exit_code": 1, "cap_source": "codex_plan"}
        with mock.patch.object(run_desk, "run", side_effect=limited):
            with self.assertRaisesRegex(FleetError, "cap_source codex_plan.*does not lift it"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)

    def test_a_cap_reached_while_waiting_for_the_desk_lock_leaves_the_round_waiting(self):
        # Another moody run held the lock and used the last run: the check inside the lock refuses.
        first_sha = self.commit("first try")
        with mock.patch.object(run_desk, "over_daily_cap", side_effect=[None, "daily run cap reached"]), \
                mock.patch.object(run_desk, "report_cap") as reported, \
                mock.patch.object(run_desk, "_launch", side_effect=AssertionError("a capped reviewer ran")):
            with self.assertRaisesRegex(run_desk.Capped, "while it waited for its desk lock.*waits as request"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        reported.assert_called_once()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [waiting] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((waiting["sha"], waiting["waiting"]), (first_sha, True))
        request = owlery.get_request(self.conn, waiting["request_id"])
        self.assertEqual((request["phase"], request["outcome"]), ("queued", None))
        self.assertEqual(pensieve.get_task(self.conn, request["task_id"])["status"], "queued")
        second_sha = self.commit("second try")
        result = self.own_review(task["id"], verdict="PASS")
        self.assertEqual((result["sha"], result["round"], result["superseded"]), (second_sha, 1, [waiting["request_id"]]))

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

    def test_two_overlapping_reviews_by_one_reviewer_both_complete_in_turn(self):
        # The second review reaches moody's locks while the first one's run is going. It must not start its
        # task until the first has recorded its verdict and closed its reviewer task.
        first_sha = self.commit("first try")
        order, results, errors = [], {}, {}
        first_running, second_at_lock, second_running = threading.Event(), threading.Event(), threading.Event()
        real_lock, real_finish = safefs.held_lock, review._finish_reviewer_task

        def launch(conn, plan, now):
            owl = next(item for item in owlery.inbox(conn, plan["desk"], include_acked=True)
                       if item["id"] == plan["owl_id"])
            [row] = [row for row in conn.execute("SELECT task_id, sha FROM review_rounds WHERE request_id = ?",
                                                 (owl["request_id"],))]
            name = "first" if row["sha"] == first_sha else "second"
            order.append(f"launch {name}")
            if name == "first":
                first_running.set()
                self.assertTrue(second_at_lock.wait(10), "the second review never reached a lock")
            else:
                second_running.set()
            block = (f"REVIEW {row['task_id']} @ {row['sha']}\nAC\nAC-1 PASS | ok\nBLOCKING\nNON-BLOCKING\n"
                     "VERDICT: CHANGES\n")
            folder = self.office / "runs" / plan["desk"]
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.write_file(folder / f"{plan['run_id']}-last-message.md", block)
            return {"desk": plan["desk"], "run_id": plan["run_id"], "exit_code": 0, "timed_out": False,
                    "cap_source": None}

        def watched_lock(dir_fd, name, blocking, timeout=None):
            if threading.current_thread().name == "second":
                second_at_lock.set()
            return real_lock(dir_fd, name, blocking, timeout)

        def finish(conn, request_id, reviewer_task_id):
            if threading.current_thread().name == "first":
                # Without the review lock the second review takes the desk lock here and starts its task.
                second_running.wait(1.5)
            real_finish(conn, request_id, reviewer_task_id)
            order.append(f"closed {threading.current_thread().name}")

        def review_in_thread(**kwargs) -> None:
            name = threading.current_thread().name
            conn = db.connect(self.db_path)
            try:
                results[name] = review.review_own(conn, str(self.repo), fetch=False, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the main thread reports it
                errors[name] = exc
            finally:
                conn.close()

        with mock.patch.object(run_desk, "_launch", side_effect=launch), \
                mock.patch.object(safefs, "held_lock", side_effect=watched_lock), \
                mock.patch.object(review, "_finish_reviewer_task", side_effect=finish):
            first = threading.Thread(target=review_in_thread, name="first", kwargs={"title": "my own fix"})
            first.start()
            self.assertTrue(first_running.wait(30), "the first review never launched")
            second_sha = self.commit("second try")
            [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
            second = threading.Thread(target=review_in_thread, name="second", kwargs={"task_id": task["id"]})
            second.start()
            first.join(60)
            second.join(60)
        self.assertFalse(first.is_alive() or second.is_alive(), "a review is still waiting")
        self.assertEqual(errors, {})
        self.assertEqual(order, ["launch first", "closed first", "launch second", "closed second"])
        self.assertEqual([(results[name]["sha"], results[name]["round"], results[name]["verdict"])
                          for name in ("first", "second")], [(first_sha, 1, "CHANGES"), (second_sha, 2, "CHANGES")])
        rounds = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual([(row["sha"], row["verdict"], row["counts"], row["request_phase"]) for row in rounds],
                         [(first_sha, "CHANGES", True, "cleaned"), (second_sha, "CHANGES", True, "cleaned")])
        self.assertTrue((self.office / "locks" / "review-moody.lock").exists())

    def test_a_review_that_waits_too_long_for_the_review_lock_opens_no_round(self):
        self.commit("first try")
        with mock.patch.object(config, "REVIEW_LOCK_WAIT_SECONDS", 0.2), \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("ran without the review lock")), \
                safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
                safefs.held_lock(locks_fd, "review-moody.lock", blocking=False):
            with self.assertRaisesRegex(FleetError, "earlier review by moody is still running, so nothing was sent"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.assertEqual(capacity.review_rounds(self.conn, task["id"]), [])
        self.assertEqual(pensieve.list_tasks(self.conn, desk="moody"), [])
        second_sha = self.commit("second try")
        result = self.own_review(task["id"])
        self.assertEqual((result["sha"], result["round"], result["superseded"]), (second_sha, 1, []))

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

        def killed(conn, desk, owl_id, mcp_job=None, now=None, on_start=None):
            on_start()
            raise SystemExit(143)
        with mock.patch.object(run_desk, "run", side_effect=killed):
            with self.assertRaises(SystemExit):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(self.moody_active(), [])
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, task["id"])], [False])
