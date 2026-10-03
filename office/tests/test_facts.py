from __future__ import annotations

import sqlite3
import unittest

from hogwarts import pensieve
from hogwarts.errors import ConflictError, NotFoundError, ValidationError
from tests.support import DAY, NOW, StoreCase


class FactTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def fact(self, text="web-app bans comments", tier="pinned", scope="fleet", now=NOW, **kwargs):
        return pensieve.add_fact(self.conn, scope, text, tier, "ryan", now=now, **kwargs)

    def test_add_fact_for_fleet_and_desk_scope(self):
        fleet = self.fact()
        desk = self.fact(scope="alpha", tier="aging")
        self.assertEqual((fleet["scope"], fleet["tier"], fleet["last_used_at"]), ("fleet", "pinned", NOW))
        self.assertEqual(desk["scope"], "alpha")

    def test_scope_must_be_fleet_or_a_registered_desk(self):
        with self.assertRaises(NotFoundError):
            self.fact(scope="gamma")
        with self.assertRaises(ValidationError):
            self.fact(scope="Fleet")

    def test_perishable_requires_a_future_expiry(self):
        with self.assertRaises(ValidationError):
            self.fact(tier="perishable")
        with self.assertRaises(ValidationError):
            self.fact(tier="perishable", expires_at=NOW)
        self.assertEqual(self.fact(tier="perishable", expires_at=NOW + DAY)["expires_at"], NOW + DAY)
        with self.assertRaises(ValidationError):
            self.fact(tier="pinned", expires_at=NOW + DAY)

    def test_fact_text_limit_is_300(self):
        self.assertEqual(len(self.fact(text="x" * 300)["text"]), 300)
        with self.assertRaises(ValidationError):
            self.fact(text="x" * 301)

    def test_touch_refreshes_last_used_at(self):
        fact = self.fact(tier="aging")
        self.assertEqual(pensieve.touch(self.conn, fact["id"], now=NOW + 99)["last_used_at"], NOW + 99)
        with self.assertRaises(NotFoundError):
            pensieve.touch(self.conn, 999)

    def test_decay_flags_aging_facts_unused_for_30_days(self):
        fresh = self.fact(tier="aging")
        stale = self.fact(tier="aging", now=NOW - 31 * DAY)
        pensieve.touch(self.conn, fresh["id"], now=NOW - 29 * DAY)
        self.assertEqual(pensieve.decay(self.conn, now=NOW), [stale["id"]])

    def test_decay_flags_perishable_facts_past_expiry(self):
        expired = self.fact(tier="perishable", expires_at=NOW + 10)
        self.fact(tier="perishable", expires_at=NOW + DAY)
        self.assertEqual(pensieve.decay(self.conn, now=NOW + 10), [expired["id"]])

    def test_pinned_facts_never_decay(self):
        self.fact(tier="pinned", now=NOW - 400 * DAY)
        self.assertEqual(pensieve.decay(self.conn, now=NOW), [])

    def test_archive_sets_archived_at_and_removes_from_decay(self):
        stale = self.fact(tier="aging", now=NOW - 40 * DAY)
        result = pensieve.archive(self.conn, [stale["id"], stale["id"]], now=NOW)
        self.assertEqual(result, {"archived": [stale["id"]], "already_archived": []})
        self.assertEqual(pensieve.archive(self.conn, [stale["id"]], now=NOW + 1)["already_archived"], [stale["id"]])
        self.assertEqual(pensieve.decay(self.conn, now=NOW), [])
        self.assertEqual(pensieve.list_facts(self.conn), [])
        self.assertEqual(pensieve.list_facts(self.conn, include_archived=True)[0]["archived_at"], NOW)
        with self.assertRaises(ConflictError):
            pensieve.touch(self.conn, stale["id"])

    def test_archive_stale_rechecks_staleness_when_it_archives(self):
        stale = self.fact(tier="aging", now=NOW - 40 * DAY)
        used = self.fact(tier="aging", now=NOW - 40 * DAY)
        self.assertEqual(pensieve.decay(self.conn, now=NOW), [stale["id"], used["id"]])
        pensieve.touch(self.conn, used["id"], now=NOW)
        self.assertEqual(pensieve.archive_stale(self.conn, now=NOW + 1), {"archived": [stale["id"]]})
        archived = {fact["id"]: fact["archived_at"] for fact in pensieve.list_facts(self.conn, include_archived=True)}
        self.assertEqual(archived, {stale["id"]: NOW + 1, used["id"]: None})
        self.assertEqual(pensieve.archive_stale(self.conn, now=NOW + 2), {"archived": []})

    def test_archive_is_all_or_nothing(self):
        fact = self.fact()
        with self.assertRaises(NotFoundError):
            pensieve.archive(self.conn, [fact["id"], 999])
        self.assertIsNone(pensieve.list_facts(self.conn)[0]["archived_at"])
        with self.assertRaises(ValidationError):
            pensieve.archive(self.conn, [])
        with self.assertRaises(ValidationError):
            pensieve.archive(self.conn, fact["id"])

    def test_nothing_deletes_facts(self):
        self.fact()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM facts")

    def test_database_requires_expiry_only_for_perishable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO facts(scope, text, tier, source, created_at, last_used_at)"
                              " VALUES ('fleet', 'x', 'perishable', 'ryan', 1, 1)")

    def test_context_facts_for_a_desk(self):
        pinned = self.fact(text="fleet pinned")
        mine = self.fact(text="alpha aging", tier="aging", scope="alpha")
        self.fact(text="beta only", scope="beta")
        self.fact(text="expired", tier="perishable", expires_at=NOW + 5)
        live = self.fact(text="live", tier="perishable", expires_at=NOW + DAY)
        context = pensieve.context_facts(self.conn, "alpha", now=NOW + 10)
        self.assertEqual([fact["id"] for fact in context], [pinned["id"], mine["id"], live["id"]])

    def test_list_facts_by_scope(self):
        self.fact()
        self.fact(scope="alpha")
        self.assertEqual(len(pensieve.list_facts(self.conn, scope="alpha")), 1)
        self.assertEqual(len(pensieve.list_facts(self.conn, scope="fleet")), 1)


if __name__ == "__main__":
    unittest.main()
