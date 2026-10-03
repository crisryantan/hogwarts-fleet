from __future__ import annotations

import sqlite3
import unittest

from hogwarts import pensieve
from hogwarts.errors import ConflictError, NotFoundError, ValidationError
from tests.support import DAY, NOW, StoreCase


class DeskTests(StoreCase):
    def test_add_and_list_desks(self):
        desk = pensieve.add_desk(self.conn, "ryan-claude", "claude", role="builder", model="claude-opus-5-5", now=NOW)
        self.assertEqual(desk, {"name": "ryan-claude", "family": "claude", "role": "builder",
                                "model": "claude-opus-5-5", "created_at": NOW})
        self.desk("beta", "codex")
        self.assertEqual([d["name"] for d in pensieve.list_desks(self.conn)], ["beta", "ryan-claude"])

    def test_desk_family_must_be_known(self):
        for family in ("Claude", "gpt", ""):
            with self.subTest(family=family):
                with self.assertRaises(ValidationError):
                    pensieve.add_desk(self.conn, "alpha", family)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('alpha', 'elf', 1)")

    def test_duplicate_desk_conflicts(self):
        self.desk("alpha")
        with self.assertRaises(ConflictError):
            pensieve.add_desk(self.conn, "alpha", "codex")

    def test_fleet_is_a_reserved_desk_name(self):
        with self.assertRaises(ValidationError):
            pensieve.add_desk(self.conn, "fleet", "script")

    def test_desks_are_immutable_and_never_deleted(self):
        self.desk("alpha")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE desks SET role = 'x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM desks")

    def test_unknown_desk_is_not_found(self):
        with self.assertRaises(NotFoundError):
            pensieve.get_desk(self.conn, "nobody")


class EventTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def event(self, summary, verdict="headmaster", task_id=None, now=NOW, **kwargs):
        return pensieve.add_event(self.conn, "alpha", "build.failed", verdict, summary, task_id=task_id,
                                  now=now, **kwargs)

    def test_add_event(self):
        event = self.event("tests failed on main")
        self.assertTrue(event["created"])
        self.assertEqual((event["verdict"], event["summary"], event["acked_at"]),
                         ("headmaster", "tests failed on main", None))

    def test_dedupe_key_returns_the_existing_event(self):
        first = self.event("one", dedupe_key="ci:build:42")
        again = self.event("two", dedupe_key="ci:build:42")
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.count("events"), 1)

    def test_event_inputs_are_validated(self):
        with self.assertRaises(ValidationError):
            pensieve.add_event(self.conn, "alpha", "Bad Kind", "routine", "x")
        with self.assertRaises(ValidationError):
            pensieve.add_event(self.conn, "alpha", "kind", "urgent", "x")
        with self.assertRaises(NotFoundError):
            pensieve.add_event(self.conn, "alpha", "kind", "routine", "x", task_id="tk_0000000000000000")

    def test_drain_returns_unacked_headmaster_events_newest_per_task_first(self):
        one, two = self.task("alpha"), self.task("beta")
        self.event("routine noise", verdict="routine", now=NOW + 9)
        old_one = self.event("one old", task_id=one["id"], now=NOW + 1)
        new_one = self.event("one new", task_id=one["id"], now=NOW + 5)
        new_two = self.event("two new", task_id=two["id"], now=NOW + 3)
        loose = self.event("no task", now=NOW + 4)
        acked = self.event("seen", now=NOW + 8)
        pensieve.ack(self.conn, acked["id"])
        drained = pensieve.drain(self.conn)
        order = [event["id"] for event in drained["events"]]
        self.assertEqual(order, [new_one["id"], loose["id"], new_two["id"], old_one["id"]])
        self.assertEqual(drained["remaining"], 0)
        self.assertIn("one new", drained["events"][0]["line"])

    def test_drain_cuts_to_max_chars_with_a_remainder(self):
        for index in range(10):
            self.event(f"event number {index} " + "x" * 40, now=NOW + index)
        drained = pensieve.drain(self.conn, max_chars=200)
        self.assertLessEqual(drained["chars"], 200)
        self.assertGreater(len(drained["events"]), 0)
        self.assertEqual(len(drained["events"]) + drained["remaining"], 10)
        self.assertGreater(drained["remaining"], 0)

    def test_drain_does_not_ack(self):
        self.event("still there")
        pensieve.drain(self.conn)
        self.assertEqual(len(pensieve.drain(self.conn)["events"]), 1)

    def test_ack_is_idempotent_and_validated(self):
        event = self.event("x")
        self.assertEqual(pensieve.ack(self.conn, event["id"], now=NOW + 1)["acked_at"], NOW + 1)
        self.assertEqual(pensieve.ack(self.conn, event["id"], now=NOW + 2)["acked_at"], NOW + 1)
        with self.assertRaises(NotFoundError):
            pensieve.ack(self.conn, 999)
        with self.assertRaises(ValidationError):
            pensieve.ack(self.conn, "1")

    def test_events_are_never_deleted(self):
        self.event("x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM events")


class SessionTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desk("alpha")
        pensieve.record_session(self.conn, "session-0001", "web-app", desk="alpha", started_at=NOW)

    def test_record_session_upserts(self):
        updated = pensieve.record_session(self.conn, "session-0001", "web-app", ended_at=NOW + 60,
                                          first_turn_tokens=1200, total_input_tokens=9000)
        self.assertFalse(updated["created"])
        self.assertEqual((updated["started_at"], updated["ended_at"], updated["desk"]), (NOW, NOW + 60, "alpha"))
        self.assertEqual((updated["first_turn_tokens"], updated["total_input_tokens"]), (1200, 9000))

    def test_session_cannot_move_project_or_end_before_start(self):
        with self.assertRaises(ConflictError):
            pensieve.record_session(self.conn, "session-0001", "other-project")
        with self.assertRaises(ValidationError):
            pensieve.record_session(self.conn, "session-0001", "web-app", ended_at=NOW - 1)

    def test_add_extract_assigns_sequence_numbers(self):
        first = pensieve.add_extract(self.conn, "session-0001", "user", "fix the build")
        second = pensieve.add_extract(self.conn, "session-0001", "assistant", "done")
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        with self.assertRaises(ConflictError):
            pensieve.add_extract(self.conn, "session-0001", "user", "dup", seq=2)

    def test_extract_needs_a_known_session_and_role(self):
        with self.assertRaises(NotFoundError):
            pensieve.add_extract(self.conn, "session-9999", "user", "x")
        with self.assertRaises(ValidationError):
            pensieve.add_extract(self.conn, "session-0001", "system", "x")

    def test_extract_limits_per_entry_and_per_session(self):
        with self.assertRaises(ValidationError):
            pensieve.add_extract(self.conn, "session-0001", "user", "x" * 4001)
        for _ in range(4):
            pensieve.add_extract(self.conn, "session-0001", "user", "y" * 4000)
        with self.assertRaises(ValidationError):
            pensieve.add_extract(self.conn, "session-0001", "user", "z")

    def test_keypoint_with_tags(self):
        keypoint = pensieve.add_keypoint(self.conn, "use the ledger", tags="design,ledger",
                                         session_id="session-0001", now=NOW)
        self.assertEqual((keypoint["tags"], keypoint["session_id"]), ("design,ledger", "session-0001"))
        with self.assertRaises(ValidationError):
            pensieve.add_keypoint(self.conn, "x" * 501)

    def test_find_searches_extracts_and_keypoints(self):
        pensieve.add_extract(self.conn, "session-0001", "user", "the launcher attach race is the real cause")
        pensieve.add_keypoint(self.conn, "launcher race fixed by in-memory retry", session_id="session-0001")
        pensieve.add_keypoint(self.conn, "unrelated note")
        found = pensieve.find(self.conn, "launcher race")
        self.assertEqual(sorted(hit["source"] for hit in found), ["extract", "keypoint"])
        self.assertTrue(all(hit["session_id"] == "session-0001" for hit in found))
        self.assertTrue(all("**" in hit["snippet"] for hit in found))
        self.assertEqual(len(pensieve.find(self.conn, "launcher", limit=1)), 1)

    def test_fts_rows_follow_extract_delete(self):
        extract = pensieve.add_extract(self.conn, "session-0001", "user", "ephemeral words")
        self.assertEqual(len(pensieve.find(self.conn, "ephemeral")), 1)
        self.conn.execute("DELETE FROM extracts WHERE id = ?", (extract["id"],))
        self.assertEqual(pensieve.find(self.conn, "ephemeral"), [])


class MetricTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def metric(self, desk, cost, ts):
        return pensieve.add_metric(self.conn, desk, "run-1", "claude-opus-5-5", 100, 20, 50, cost, 1000, ts=ts)

    def test_summary_aggregates_per_desk_since(self):
        self.metric("alpha", 0.25, NOW - DAY)
        self.metric("alpha", 0.5, NOW)
        self.metric("alpha", 0.125, NOW + 1)
        self.metric("beta", 1, NOW)
        rows = {row["desk"]: row for row in pensieve.summary(self.conn, since=NOW)}
        self.assertEqual(rows["alpha"]["runs"], 2)
        self.assertEqual(rows["alpha"]["input_tokens"], 200)
        self.assertAlmostEqual(rows["alpha"]["cost_usd"], 0.625)
        self.assertEqual(rows["beta"]["runs"], 1)

    def test_metric_inputs_are_validated(self):
        with self.assertRaises(ValidationError):
            pensieve.add_metric(self.conn, "alpha", "run-1", "m", -1, 0, 0, 0, 0)
        with self.assertRaises(ValidationError):
            pensieve.add_metric(self.conn, "alpha", "run-1", "m", 0, 0, 0, float("nan"), 0)
        with self.assertRaises(NotFoundError):
            pensieve.add_metric(self.conn, "gamma", "run-1", "m", 0, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
