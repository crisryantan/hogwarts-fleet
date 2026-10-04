from __future__ import annotations

import sqlite3
from unittest import mock

from hogwarts import capacity, cli, db, owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from tests.support import DAY, NOW, StoreCase, temp_dir
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


class RoundCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.author = self.started("alpha")["id"]

    def round(self, sha: str, now: int = NOW, **kwargs) -> dict:
        return capacity.open_review_round(self.conn, self.author, "beta", sha, f"review {sha[:12]}",
                                          body="read the diff", now=now, **kwargs)

    def run_reviewer(self, opened: dict) -> None:
        """What the review script does once the reviewer's run starts and finishes."""
        pensieve.start_task(self.conn, opened["task"]["id"], now=NOW)
        pensieve.close_task(self.conn, opened["task"]["id"], "superseded", now=NOW)

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
        self.assertTrue({"cap_bumps", "cap_hits", "round_allowances", "review_rounds"} <= names)
        self.assertEqual(capacity.add_bump(conn, "alpha", "runs", 1, RESET, now=NOW)["amount"], 1)
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), 4)
        self.assertEqual(conn.total_changes, changes)


class CapCliTests(CliCase):
    def setUp(self):
        super().setUp()
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

