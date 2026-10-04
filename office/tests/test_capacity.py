from __future__ import annotations

import sqlite3
from unittest import mock

from hogwarts import capacity, cli, db, owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from tests.support import DAY, NOW, REPO, StoreCase, temp_dir
from tests.test_cli import CliCase

DAY_START = NOW - NOW % DAY
RESET = DAY_START + DAY
SHAS = [str(digit) * 40 for digit in range(1, 8)]


class CapDayTests(StoreCase):
    def test_day_bounds_follow_utc_midnight_and_the_offset(self):
        self.assertEqual(capacity.day_bounds(NOW), (DAY_START, RESET))
        self.assertEqual(capacity.day_bounds(DAY_START), (DAY_START, RESET))
        self.assertEqual(capacity.day_bounds(RESET - 1), (DAY_START, RESET))
        self.assertEqual(capacity.day_bounds(NOW, 9 * 3600), (DAY_START - DAY + 9 * 3600, DAY_START + 9 * 3600))
        self.assertEqual(capacity.utc_text(RESET), "2027-01-16T00:00:00Z")
        for bad in (-1, DAY, "0"):
            with self.subTest(offset=bad), self.assertRaises(ValidationError):
                capacity.day_bounds(NOW, bad)


    def test_day_bounds_follow_local_midnight_when_the_offset_is_none(self):
        for zone in (0, 10 * 3600, -5 * 3600, 5 * 3600 + 1800):
            with self.subTest(zone=zone), mock.patch.object(capacity, "local_utc_offset", return_value=zone):
                start, end = capacity.day_bounds(NOW, None)
                self.assertEqual((start + zone) % DAY, 0)
                self.assertEqual(end - start, DAY)
                self.assertTrue(start <= NOW < end)
        with mock.patch.object(capacity, "local_utc_offset", return_value=-(5 * 3600 + 1800)):
            self.assertEqual(capacity.local_text(RESET), "2027-01-15T18:30:00-05:30")
        self.assertIsInstance(capacity.local_utc_offset(NOW), int)


class BumpTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_bump_ranges(self):
        for kind, amount in (("runs", 0), ("runs", 501), ("runs", 1.5), ("spend", 0.001), ("spend", 500.01),
                             ("spend", -1), ("hours", 1)):
            with self.subTest(kind=kind, amount=amount), self.assertRaises(ValidationError):
                capacity.add_bump(self.conn, "alpha", kind, amount, RESET, now=NOW)
        with self.assertRaises(ValidationError):
            capacity.add_bump(self.conn, "alpha", "runs", 5, NOW, now=NOW)
        with self.assertRaises(NotFoundError):
            capacity.add_bump(self.conn, "gamma", "runs", 5, RESET, now=NOW)
        self.assertEqual(capacity.add_bump(self.conn, "alpha", "runs", 500, RESET, now=NOW)["amount"], 500)
        self.assertEqual(capacity.add_bump(self.conn, "alpha", "spend", 0.01, RESET, now=NOW)["amount"], 0.01)

    def test_a_bump_is_stored_with_when_and_expires_at_the_reset(self):
        bump = capacity.add_bump(self.conn, "alpha", "runs", 5, RESET, now=NOW)
        self.assertEqual((bump["desk"], bump["kind"], bump["amount"], bump["created_at"], bump["expires_at"]),
                         ("alpha", "runs", 5, NOW, RESET))
        capacity.add_bump(self.conn, "alpha", "runs", 2, RESET, now=NOW + 60)
        capacity.add_bump(self.conn, "alpha", "spend", 1.25, RESET, now=NOW)
        self.assertEqual(capacity.active_bumps(self.conn, "alpha", NOW + 60), {"runs": 7, "spend": 1.25})
        self.assertEqual(capacity.active_bumps(self.conn, "alpha", RESET - 1), {"runs": 7, "spend": 1.25})
        self.assertEqual(capacity.active_bumps(self.conn, "alpha", RESET), {"runs": 0, "spend": 0.0})
        self.assertEqual(capacity.active_bumps(self.conn, "beta", NOW), {"runs": 0, "spend": 0.0})
        self.assertEqual(len(capacity.list_bumps(self.conn, "alpha")), 3)

    def test_bumps_are_immutable(self):
        capacity.add_bump(self.conn, "alpha", "runs", 5, RESET, now=NOW)
        for statement in ("UPDATE cap_bumps SET amount = 50", "DELETE FROM cap_bumps"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)

    def test_cap_status_counts_this_day_and_adds_the_bump(self):
        for ts in (DAY_START - 1, DAY_START, NOW, RESET):
            pensieve.add_metric(self.conn, "alpha", f"run-{ts}", "model-x", 1, 1, 0, 1.5, 10, ts=ts)
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW)
        self.assertEqual((status["runs_used"], status["spend_used_usd"], status["reached"]), (2, 3.0, "runs"))
        capacity.add_bump(self.conn, "alpha", "runs", 1, RESET, now=NOW)
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW)
        self.assertEqual((status["runs_limit"], status["reached"]), (3, "spend"))
        capacity.add_bump(self.conn, "alpha", "spend", 0.5, RESET, now=NOW)
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW)
        self.assertEqual((status["spend_limit_usd"], status["reached"]), (3.5, None))
        self.assertEqual((status["resets_at"], status["resets_at_utc"]), (RESET, "2027-01-16T00:00:00Z"))
        self.assertIsNone(capacity.cap_status(self.conn, "beta", 2, None, NOW)["spend_limit_usd"])


class CapHitTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_cap_hits_pair_the_cap_with_its_source(self):
        capacity.record_cap_hit(self.conn, "alpha", "runs", "fleet", now=NOW)
        capacity.record_cap_hit(self.conn, "alpha", "plan", "claude_plan", run_id="run-1", now=NOW)
        capacity.record_cap_hit(self.conn, "beta", "plan", "codex_plan", now=NOW)
        for cap, source in (("plan", "fleet"), ("runs", "claude_plan"), ("spend", "codex_plan"), ("runs", "moon")):
            with self.subTest(cap=cap, source=source), self.assertRaises(ValidationError):
                capacity.record_cap_hit(self.conn, "alpha", cap, source, now=NOW)
        with self.assertRaises(IntegrityError):
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO cap_hits(ts, desk, cap, cap_source) VALUES (1, 'alpha', 'plan', 'fleet')")
        self.assertEqual([(hit["desk"], hit["cap"], hit["cap_source"]) for hit in capacity.list_cap_hits(self.conn)],
                         [("alpha", "runs", "fleet"), ("alpha", "plan", "claude_plan"), ("beta", "plan", "codex_plan")])


class LaunchTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_a_launch_counts_at_once_and_a_killed_run_stays_counted(self):
        capacity.record_launch(self.conn, "alpha", "run-1", "model-x", now=NOW)
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW)
        self.assertEqual((status["runs_used"], status["spend_used_usd"], status["reached"]), (1, 0.0, None))
        metric = capacity.record_launch_usage(self.conn, "run-1", 10, 5, 0, 1.25, 900, now=NOW + 5)
        self.assertEqual((metric["desk"], metric["run_id"], metric["model"], metric["cost_usd"]),
                         ("alpha", "run-1", "model-x", 1.25))
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW + 5)
        self.assertEqual((status["runs_used"], status["spend_used_usd"]), (1, 1.25))
        capacity.record_launch(self.conn, "alpha", "run-2", "model-x", now=NOW + 6)  # killed: no usage, ever
        pensieve.add_metric(self.conn, "alpha", "run-by-hand", "model-x", 1, 1, 0, 0.5, 10, ts=NOW + 7)
        status = capacity.cap_status(self.conn, "alpha", 2, 3.0, NOW + 8)
        self.assertEqual((status["runs_used"], status["spend_used_usd"], status["reached"]), (3, 1.75, "runs"))
        self.assertEqual([(row["run_id"], row["metric_id"]) for row in capacity.list_launches(self.conn, "alpha")],
                         [("run-1", metric["id"]), ("run-2", None)])
        self.assertEqual(capacity.list_launches(self.conn, "beta"), [])

    def test_a_run_counts_on_the_cap_day_it_launched(self):
        capacity.record_launch(self.conn, "alpha", "run-late", "model-x", now=RESET - 1)
        capacity.record_launch_usage(self.conn, "run-late", 1, 1, 0, 0.5, 10, now=RESET + 60)
        self.assertEqual(capacity.cap_status(self.conn, "alpha", 2, 3.0, RESET - 1)["runs_used"], 1)
        after = capacity.cap_status(self.conn, "alpha", 2, 3.0, RESET + 61)
        self.assertEqual((after["runs_used"], after["spend_used_usd"]), (0, 0.5))

    def test_a_launch_is_recorded_once_and_its_usage_once(self):
        capacity.record_launch(self.conn, "alpha", "run-1", "model-x", now=NOW)
        with self.assertRaises(ConflictError):
            capacity.record_launch(self.conn, "alpha", "run-1", "model-x", now=NOW)
        capacity.record_launch_usage(self.conn, "run-1", 1, 1, 0, 0.1, 10, now=NOW)
        with self.assertRaises(ConflictError):
            capacity.record_launch_usage(self.conn, "run-1", 1, 1, 0, 0.1, 10, now=NOW)
        with self.assertRaises(NotFoundError):
            capacity.record_launch_usage(self.conn, "run-unknown", 1, 1, 0, 0.1, 10, now=NOW)
        with self.assertRaises(NotFoundError):
            capacity.record_launch(self.conn, "gamma", "run-2", "model-x", now=NOW)
        capacity.record_launch(self.conn, "alpha", "run-3", "model-x", now=NOW)
        with self.assertRaises(ValidationError):
            capacity.record_launch_usage(self.conn, "run-3", -1, 1, 0, 0.1, 10, now=NOW)
        self.assertEqual(capacity.list_launches(self.conn, "alpha")[-1]["metric_id"], None)

    def test_the_store_keeps_launches_fixed_and_their_usage_their_own(self):
        capacity.record_launch(self.conn, "alpha", "run-1", "model-x", now=NOW)
        capacity.record_launch(self.conn, "alpha", "run-2", "model-x", now=NOW)
        other = capacity.record_launch_usage(self.conn, "run-2", 1, 1, 0, 0.1, 10, now=NOW)
        stray = pensieve.add_metric(self.conn, "beta", "run-1", "model-x", 1, 1, 0, 0.1, 10, ts=NOW)
        update = "UPDATE run_launches SET metric_id = ? WHERE run_id = ?"
        for metric_id in (other["id"], stray["id"]):  # another run's usage, or another desk's
            with self.subTest(metric=metric_id), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(update, (metric_id, "run-1"))
        with self.assertRaises(sqlite3.IntegrityError):  # usage is final
            self.conn.execute(update, (None, "run-2"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE run_launches SET launched_at = 1 WHERE run_id = 'run-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM run_launches")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO run_launches(run_id, desk, model, launched_at, metric_id)"
                              " VALUES ('run-3', 'alpha', 'model-x', 1, ?)", (other["id"],))


class RoundCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.author = self.started("alpha")["id"]

    def round(self, sha: str, now: int = NOW, **kwargs) -> dict:
        return capacity.open_review_round(self.conn, self.author, "beta", sha, f"review {sha[:12]}",
                                          body="read the diff", now=now, **kwargs)

    def run_reviewer(self, opened: dict, verdict: bool = True, published: bool = True) -> None:
        """What the review script does once the reviewer's run starts and finishes: with a verdict it records
        it on the round, then posts the review result on the request; a run that crashed or hit a limit only
        frees the reviewer. published=False is a verdict recorded before publishing the review failed."""
        request_id, task_id = opened["request"]["id"], opened["task"]["id"]
        pensieve.start_task(self.conn, task_id, now=NOW)
        owlery.advance(self.conn, request_id, "claimed", now=NOW)
        owlery.advance(self.conn, request_id, "running", now=NOW)
        if verdict:
            self.record_verdict(opened)
        if verdict and published:
            owlery.send(self.conn, "beta", "alpha", "result", "review CHANGES", body="REVIEW", task_id=task_id,
                        request_id=request_id, now=NOW)
            owlery.advance(self.conn, request_id, "result_posted", detail="CHANGES", now=NOW)
        pensieve.close_task(self.conn, task_id, "superseded", now=NOW)

    def record_verdict(self, opened: dict, verdict: str = "CHANGES") -> dict:
        sha = self.row(opened)["sha"]
        pensieve.record_commit(self.conn, self.author, REPO, sha, now=NOW)
        return capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, verdict, now=NOW)

    def row(self, opened: dict) -> dict:
        rows = {row["request_id"]: row for row in capacity.review_rounds(self.conn, self.author)}
        return rows[opened["request"]["id"]]

    def live(self) -> list:
        return [(row["sha"], row["round"]) for row in capacity.review_rounds(self.conn, self.author)
                if row["superseded_by"] is None]


class CoalescingTests(RoundCase):
    def test_only_the_newest_sha_of_waiting_reviews_is_kept(self):
        first = self.round(SHAS[0])
        second = self.round(SHAS[1], now=NOW + 10)
        third = self.round(SHAS[2], now=NOW + 20)
        self.assertEqual([item["sha"] for item in second["superseded"]], [SHAS[0]])
        self.assertEqual([item["sha"] for item in third["superseded"]], [SHAS[1]])
        self.assertEqual(self.live(), [(SHAS[2], 1)])
        rows = {row["request_id"]: row for row in capacity.review_rounds(self.conn, self.author)}
        self.assertEqual(rows[first["request"]["id"]]["superseded_by"], second["request"]["id"])
        self.assertEqual(rows[first["request"]["id"]]["superseded_at"], NOW + 10)
        self.assertEqual(rows[second["request"]["id"]]["superseded_by"], third["request"]["id"])
        self.assertTrue(rows[third["request"]["id"]]["waiting"])
        for old in (first, second):
            request = owlery.get_request(self.conn, old["request"]["id"])
            self.assertEqual((request["outcome"], request["reason"]), ("deferred", "conflict"))
            task = pensieve.get_task(self.conn, old["task"]["id"])
            self.assertEqual((task["status"], task["close_reason"]), ("closed", "superseded"))
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "beta")], [third["owl"]["id"]])
        self.assertEqual([row["id"] for row in capacity.waiting_requests(self.conn, "beta")], [third["request"]["id"]])

    def test_a_review_whose_run_started_is_never_superseded(self):
        first = self.round(SHAS[0])
        self.run_reviewer(first)
        second = self.round(SHAS[1])
        self.assertEqual(second["superseded"], [])
        self.assertEqual(self.live(), [(SHAS[0], 1), (SHAS[1], 2)])

    def test_a_started_but_running_review_is_not_waiting(self):
        first = self.round(SHAS[0])
        pensieve.start_task(self.conn, first["task"]["id"], now=NOW)
        self.assertEqual(self.round(SHAS[1])["superseded"], [])
        self.assertIsNone(owlery.get_request(self.conn, first["request"]["id"])["outcome"])

    def test_superseded_rows_are_final(self):
        first = self.round(SHAS[0])
        self.round(SHAS[1])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE review_rounds SET superseded_by = NULL, superseded_at = NULL"
                              " WHERE request_id = ?", (first["request"]["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM review_rounds")


class RoundCapTests(RoundCase):
    def three_rounds(self) -> None:
        for sha in SHAS[:3]:
            self.run_reviewer(self.round(sha))

    def test_a_run_without_a_verdict_does_not_use_up_a_round(self):
        crashed = self.round(SHAS[0])
        self.run_reviewer(crashed, verdict=False)
        self.assertEqual(self.round(SHAS[1])["round"], 1)
        rows = {row["request_id"]: row for row in capacity.review_rounds(self.conn, self.author)}
        self.assertEqual((rows[crashed["request"]["id"]]["counts"], rows[crashed["request"]["id"]]["has_verdict"]),
                         (False, False))

    def test_a_result_owl_from_a_run_that_then_crashed_does_not_use_up_a_round(self):
        # The reviewer desk can post a result owl itself; only a verdict recorded on the round counts.
        crashed = self.round(SHAS[0])
        request_id, task_id = crashed["request"]["id"], crashed["task"]["id"]
        pensieve.start_task(self.conn, task_id, now=NOW)
        owlery.advance(self.conn, request_id, "claimed", now=NOW)
        owlery.advance(self.conn, request_id, "running", now=NOW)
        owlery.send(self.conn, "beta", "alpha", "result", "my review", body="REVIEW", task_id=task_id,
                    request_id=request_id, now=NOW)
        pensieve.close_task(self.conn, task_id, "superseded", now=NOW)
        rows = {row["request_id"]: row for row in capacity.review_rounds(self.conn, self.author)}
        self.assertEqual((rows[request_id]["counts"], rows[request_id]["has_verdict"]), (False, False))
        self.assertEqual(self.round(SHAS[1])["round"], 1)

    def test_a_result_posted_phase_without_a_recorded_verdict_does_not_use_up_a_round(self):
        crashed = self.round(SHAS[0])
        request_id, task_id = crashed["request"]["id"], crashed["task"]["id"]
        pensieve.start_task(self.conn, task_id, now=NOW)
        for phase in ("claimed", "running"):
            owlery.advance(self.conn, request_id, phase, now=NOW)
        owlery.send(self.conn, "beta", "alpha", "result", "my review", body="REVIEW", task_id=task_id,
                    request_id=request_id, now=NOW)
        owlery.advance(self.conn, request_id, "result_posted", now=NOW)
        pensieve.close_task(self.conn, task_id, "superseded", now=NOW)
        self.assertEqual((self.row(crashed)["counts"], self.row(crashed)["has_verdict"]), (False, False))
        self.assertEqual(self.round(SHAS[1])["round"], 1)

    def test_a_verdict_whose_publication_failed_still_uses_up_its_round_and_allowance(self):
        self.three_rounds()
        allowance = capacity.allow_round(self.conn, self.author, now=NOW)
        fourth = self.round(SHAS[3])
        self.assertEqual(fourth["allowance_id"], allowance["id"])
        self.run_reviewer(fourth, published=False)
        row = self.row(fourth)
        self.assertEqual((row["counts"], row["has_verdict"], row["verdict"], row["request_phase"]),
                         (True, True, "CHANGES", "running"))
        self.assertEqual(row["reviewer_task_status"], "closed")
        with self.assertRaises(capacity.RoundCapReached) as refused:
            self.round(SHAS[4])
        self.assertEqual(refused.exception.round, 5)
        again = capacity.allow_round(self.conn, self.author, now=NOW + 1)
        self.assertEqual((again["created"], again["rounds"]), (True, 4))
        self.assertNotEqual(again["id"], allowance["id"])

    def test_three_verdict_rounds_then_a_fourth_is_refused_whatever_crashed_between(self):
        for index, sha in enumerate(SHAS[:3]):
            self.run_reviewer(self.round(sha, now=NOW + index), verdict=False)
            opened = self.round(sha, now=NOW + index, idempotency_key=f"retry-{index}-key")
            self.assertEqual(opened["round"], index + 1)
            self.run_reviewer(opened)
        rounds = capacity.review_rounds(self.conn, self.author)
        self.assertEqual([row["counts"] for row in rounds], [False, True] * 3)
        with self.assertRaises(capacity.RoundCapReached) as refused:
            self.round(SHAS[3])
        self.assertEqual(refused.exception.round, 4)

    def test_a_run_still_in_progress_holds_its_round(self):
        for sha in SHAS[:2]:
            self.run_reviewer(self.round(sha))
        running = self.round(SHAS[2])
        pensieve.start_task(self.conn, running["task"]["id"], now=NOW)
        with self.assertRaises(capacity.RoundCapReached):
            self.round(SHAS[3])

    def test_stranded_rounds_are_the_reviewer_tasks_still_active_for_that_reviewer(self):
        self.run_reviewer(self.round(SHAS[0]))
        self.assertEqual(capacity.stranded_rounds(self.conn, "beta"), [])
        running = self.round(SHAS[1])
        pensieve.start_task(self.conn, running["task"]["id"], now=NOW)
        self.assertEqual(capacity.stranded_rounds(self.conn, "alpha"), [])
        [row] = capacity.stranded_rounds(self.conn, "beta")
        self.assertEqual((row["request_id"], row["task_id"], row["reviewer_task_id"], row["has_verdict"]),
                         (running["request"]["id"], self.author, running["task"]["id"], False))
        self.record_verdict(running)
        self.assertTrue(capacity.stranded_rounds(self.conn, "beta")[0]["has_verdict"])
        pensieve.close_task(self.conn, running["task"]["id"], "superseded", now=NOW)
        self.assertEqual(capacity.stranded_rounds(self.conn, "beta"), [])

    def test_an_allowance_taken_by_a_run_without_a_verdict_is_given_back(self):
        self.three_rounds()
        allowance = capacity.allow_round(self.conn, self.author, now=NOW)
        crashed = self.round(SHAS[3])
        self.assertEqual(crashed["allowance_id"], allowance["id"])
        self.run_reviewer(crashed, verdict=False)
        again = capacity.allow_round(self.conn, self.author, now=NOW + 1)
        self.assertEqual((again["id"], again["created"], again["rounds"]), (allowance["id"], False, 3))
        retry = self.round(SHAS[3], idempotency_key="retry-key-1")
        self.assertEqual((retry["round"], retry["allowance_id"]), (4, allowance["id"]))
        self.run_reviewer(retry)
        with self.assertRaises(capacity.RoundCapReached):
            self.round(SHAS[4])

    def test_round_four_is_refused_then_allowed_once_then_refused_again(self):
        self.three_rounds()
        with self.assertRaises(capacity.RoundCapReached) as refused:
            self.round(SHAS[3])
        self.assertEqual((refused.exception.task_id, refused.exception.round, refused.exception.max_rounds),
                         (self.author, 4, 3))
        self.assertIn(f"castle task allow-round {self.author}", str(refused.exception))
        self.assertEqual(len(capacity.review_rounds(self.conn, self.author)), 3)
        allowance = capacity.allow_round(self.conn, self.author, now=NOW + 5)
        self.assertEqual((allowance["task_id"], allowance["granted_at"], allowance["created"], allowance["rounds"]),
                         (self.author, NOW + 5, True, 3))
        fourth = self.round(SHAS[3])
        self.assertEqual((fourth["round"], fourth["allowance_id"]), (4, allowance["id"]))
        self.run_reviewer(fourth)
        with self.assertRaises(capacity.RoundCapReached) as again:
            self.round(SHAS[4])
        self.assertEqual(again.exception.round, 5)

    def test_allow_round_twice_before_use_allows_one_round(self):
        self.three_rounds()
        first = capacity.allow_round(self.conn, self.author, now=NOW)
        second = capacity.allow_round(self.conn, self.author, now=NOW + 1)
        self.assertEqual((second["id"], second["created"]), (first["id"], False))
        self.run_reviewer(self.round(SHAS[3]))
        with self.assertRaises(capacity.RoundCapReached):
            self.round(SHAS[4])

    def test_coalescing_spends_no_round_and_passes_the_allowance_on(self):
        self.three_rounds()
        capacity.allow_round(self.conn, self.author, now=NOW)
        waiting = self.round(SHAS[3])
        newer = self.round(SHAS[4])
        self.assertEqual((newer["round"], newer["allowance_id"]), (4, waiting["allowance_id"]))
        self.assertEqual([item["request_id"] for item in newer["superseded"]], [waiting["request"]["id"]])
        self.assertEqual(self.live()[-1], (SHAS[4], 4))

    def test_a_closed_task_takes_no_allowance(self):
        pensieve.close_task(self.conn, self.author, "abandoned", now=NOW)
        with self.assertRaises(ConflictError):
            capacity.allow_round(self.conn, self.author, now=NOW)

    def test_a_refusal_changes_nothing(self):
        self.three_rounds()
        before = self.conn.total_changes
        with self.assertRaises(capacity.RoundCapReached):
            self.round(SHAS[3])
        self.assertEqual(self.conn.total_changes, before)


class RoundVerdictTests(RoundCase):
    def test_the_verdict_is_stored_and_tied_to_its_round_in_one_step(self):
        opened = self.round(SHAS[0])
        review = self.record_verdict(opened, "HEADMASTER")
        self.assertEqual((review["sha"], review["task_id"], review["reviewer_desk"], review["verdict"]),
                         (SHAS[0], self.author, "beta", "HEADMASTER"))
        self.assertEqual((self.row(opened)["review_id"], self.row(opened)["verdict"]), (review["id"], "HEADMASTER"))
        with self.assertRaisesRegex(ConflictError, "already has a verdict"):
            capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, "CHANGES", now=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM review_passes").fetchone()[0], 1)

    def test_a_failed_review_record_leaves_the_round_without_a_verdict(self):
        opened = self.round(SHAS[0])
        before = self.conn.total_changes
        with self.assertRaises(IntegrityError):  # the commit was never recorded on the author task
            capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, "CHANGES", now=NOW)
        self.assertEqual(self.conn.total_changes, before)
        self.assertIsNone(self.row(opened)["review_id"])

    def test_superseded_withdrawn_and_unknown_rounds_take_no_verdict(self):
        first = self.round(SHAS[0])
        second = self.round(SHAS[1])
        pensieve.record_commit(self.conn, self.author, REPO, SHAS[0], now=NOW)
        with self.assertRaisesRegex(ConflictError, "superseded"):
            capacity.record_round_verdict(self.conn, first["request"]["id"], REPO, "CHANGES", now=NOW)
        owlery.decline(self.conn, second["request"]["id"], "safety", now=NOW)
        pensieve.record_commit(self.conn, self.author, REPO, SHAS[1], now=NOW)
        with self.assertRaisesRegex(ConflictError, "outcome"):
            capacity.record_round_verdict(self.conn, second["request"]["id"], REPO, "CHANGES", now=NOW)
        plain = owlery.open_request(self.conn, "alpha", "beta", "not a round", body="x", now=NOW)
        with self.assertRaisesRegex(ConflictError, "not opened as a review round"):
            capacity.record_round_verdict(self.conn, plain["request"]["id"], REPO, "CHANGES", now=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM review_passes").fetchone()[0], 0)

    def test_the_store_binds_a_round_verdict_to_that_round_and_sets_it_once(self):
        first, other = self.round(SHAS[0]), self.round(SHAS[1])
        self.run_reviewer(other)
        other_review = self.row(other)["review_id"]
        update = "UPDATE review_rounds SET review_id = ? WHERE request_id = ?"
        with self.assertRaises(sqlite3.IntegrityError):  # first was superseded by other
            self.conn.execute(update, (other_review, first["request"]["id"]))
        third = self.round(SHAS[2])
        with self.assertRaises(sqlite3.IntegrityError):  # a review of a different commit
            self.conn.execute(update, (other_review, third["request"]["id"]))
        with self.assertRaises(sqlite3.IntegrityError):  # a verdict is final
            self.conn.execute(update, (None, other["request"]["id"]))
        with self.assertRaises(sqlite3.IntegrityError):  # a round with a verdict is never superseded
            self.conn.execute("UPDATE review_rounds SET superseded_by = ?, superseded_at = 1 WHERE request_id = ?",
                              (third["request"]["id"], other["request"]["id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO review_rounds(request_id, task_id, reviewer, sha, round, created_at,"
                              " review_id) VALUES ('rq_0000000000000000', ?, 'beta', ?, 1, 1, ?)",
                              (self.author, SHAS[1], other_review))


class MigrationV4Tests(StoreCase):
    def test_a_v3_database_gains_the_capacity_tables(self):
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:3]), mock.patch.object(db, "SCHEMA_VERSION", 3):
            conn = db.connect(path)
            pensieve.add_desk(conn, "alpha", "claude", now=NOW)
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(db.schema_version(conn), 4)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"cap_bumps", "cap_hits", "round_allowances", "review_rounds", "run_launches"} <= names)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(review_rounds)")}
        self.assertIn("review_id", columns)
        self.assertEqual(capacity.add_bump(conn, "alpha", "runs", 1, RESET, now=NOW)["amount"], 1)
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), 4)
        self.assertEqual(conn.total_changes, changes)


class CapCliTests(CliCase):
    def setUp(self):
        super().setUp()
        zone = mock.patch.object(capacity, "local_utc_offset", return_value=0)
        zone.start()
        self.addCleanup(zone.stop)
        self.ok("init")
        for name, family in (("moody", "codex"), ("hermione", "claude"), ("snape", "claude")):
            self.ok("desk", "add", name, "--family", family)
        clock = mock.patch.object(cli, "_clock", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_desk_cap_bumps_runs_and_spend_until_the_reset(self):
        data = self.ok("desk", "cap", "moody", "--runs", "+20")
        self.assertEqual((data["bump"]["kind"], data["bump"]["amount"], data["bump"]["created_at"],
                          data["bump"]["expires_at"]), ("runs", 20, NOW, RESET))
        self.assertEqual((data["caps"]["run_cap"], data["caps"]["runs_bump"], data["caps"]["runs_limit"]), (80, 20, 100))
        data = self.ok("desk", "cap", "hermione", "--spend", "+12.50")
        self.assertEqual((data["bump"]["amount"], data["caps"]["spend_limit_usd"]), (12.5, 72.5))

    def test_desk_cap_refuses_bad_amounts_and_uncapped_desks(self):
        for argv in (("moody", "--runs", "20"), ("moody", "--runs", "+0"), ("moody", "--runs", "+501"),
                     ("moody", "--runs", "+1000"), ("hermione", "--spend", "+0.001"), ("hermione", "--spend", "+500.5"),
                     ("moody", "--spend", "+5"), ("snape", "--runs", "+5"), ("moody",),
                     ("moody", "--runs", "+5", "--spend", "+5")):
            with self.subTest(argv=argv):
                self.fails(2, "ValidationError", "desk", "cap", *argv)
        self.fails(4, "NotFoundError", "desk", "cap", "ron", "--runs", "+5")

    def test_desk_caps_lists_each_registered_capped_desk(self):
        self.ok("metric", "add", "--desk", "moody", "--run-id", "run-1", "--model", "codex-default",
                "--input-tokens", "1", "--output-tokens", "1", "--cache-read-tokens", "0", "--duration-ms", "1",
                "--cost-usd", "0", "--ts", str(NOW - 60))
        self.ok("desk", "cap", "moody", "--runs", "+5")
        rows = {row["desk"]: row for row in self.ok("desk", "caps")}
        self.assertEqual(sorted(rows), ["hermione", "moody"])
        moody = rows["moody"]
        self.assertEqual((moody["runs_used"], moody["run_cap"], moody["runs_bump"], moody["spend_used_usd"],
                          moody["spend_cap_usd"], moody["spend_bump_usd"], moody["resets_at_utc"]),
                         (1, 80, 5, 0.0, None, 0.0, "2027-01-16T00:00:00Z"))
        self.assertEqual((rows["hermione"]["spend_cap_usd"], rows["hermione"]["run_cap"]), (60.0, 80))

    def test_task_allow_round_is_recorded_with_when(self):
        task = self.ok("task", "create", "--desk", "moody", "--title", "build")["id"]
        data = self.ok("task", "allow-round", task)
        self.assertEqual((data["task_id"], data["granted_at"], data["created"]), (task, NOW, True))
        self.assertFalse(self.ok("task", "allow-round", task)["created"])
        self.assertEqual(self.ok("task", "rounds", task), [])
        self.fails(4, "NotFoundError", "task", "allow-round", "tk_0000000000000000")

