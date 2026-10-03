from __future__ import annotations

import hashlib
import sqlite3
import unittest
from unittest import mock

from hogwarts import db, ids, owlery, pensieve
from hogwarts.errors import ConflictError, StoreError, TokenError, ValidationError
from tests.support import NOW, StoreCase


class CloseTokenTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.active = self.started("alpha")

    def mint(self, task_id=None, now=NOW, **kwargs):
        return owlery.mint(self.conn, task_id or self.active["id"], "cli", now=now, **kwargs)

    def test_mint_returns_the_raw_token_once_and_stores_only_its_hash(self):
        minted = self.mint()
        token = minted["token"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(minted["expires_at"], NOW + 600)
        rows = self.conn.execute("SELECT * FROM close_tokens").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        for row in rows:
            self.assertNotIn(token, [str(value) for value in tuple(row)])

    def test_close_with_a_valid_token_consumes_it(self):
        token = self.mint()["token"]
        pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW + 10)
        consumed = self.conn.execute("SELECT consumed_at FROM close_tokens").fetchone()[0]
        self.assertEqual(consumed, NOW + 10)

    def test_token_is_single_use(self):
        token = self.mint()["token"]
        with db.transaction(self.conn):
            owlery.consume(self.conn, self.active["id"], token, now=NOW)
        with self.assertRaises(TokenError):
            with db.transaction(self.conn):
                owlery.consume(self.conn, self.active["id"], token, now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE close_tokens SET consumed_at = NULL")

    def test_expired_token_is_rejected(self):
        token = self.mint(ttl_seconds=60)["token"]
        with self.assertRaises(TokenError):
            pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW + 60)
        self.assertEqual(pensieve.get_task(self.conn, self.active["id"])["status"], "active")

    def test_token_for_another_task_is_rejected(self):
        other = self.started("beta")
        token = self.mint(other["id"])["token"]
        with self.assertRaises(TokenError):
            pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW)

    def test_malformed_tokens_are_rejected(self):
        for token in ("", "short", "x" * 44, "!" * 43, 12345):
            with self.subTest(token=token):
                with self.assertRaises(TokenError):
                    pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW)

    def test_consume_compares_with_hmac_compare_digest(self):
        token = self.mint()["token"]
        with mock.patch.object(owlery.hmac, "compare_digest", wraps=owlery.hmac.compare_digest) as spy:
            pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW)
        self.assertTrue(spy.called)

    def test_consume_must_run_inside_the_close_transaction(self):
        token = self.mint()["token"]
        with self.assertRaises(StoreError):
            owlery.consume(self.conn, self.active["id"], token, now=NOW)

    def test_consume_refuses_a_snapshot(self):
        token = self.mint()["token"]
        with db.snapshot(self.conn):
            with self.assertRaises(StoreError):
                owlery.consume(self.conn, self.active["id"], token, now=NOW)
        self.assertIsNone(self.conn.execute("SELECT consumed_at FROM close_tokens").fetchone()[0])

    def test_token_is_consumed_in_the_same_transaction_as_the_close(self):
        token = self.mint()["token"]
        with mock.patch.object(pensieve, "_set_closed", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW)
        self.assertIsNone(self.conn.execute("SELECT consumed_at FROM close_tokens").fetchone()[0])
        closed = pensieve.close_task(self.conn, self.active["id"], "complete", token, now=NOW)
        self.assertEqual(closed["status"], "closed")

    def test_mint_requires_an_active_or_awaiting_task(self):
        queued = self.task("beta")
        with self.assertRaises(ConflictError):
            self.mint(queued["id"])
        pensieve.mark_awaiting_close(self.conn, self.active["id"])
        self.assertIn("token", self.mint())

    def test_mint_validates_minted_by_and_ttl(self):
        with self.assertRaises(ValidationError):
            owlery.mint(self.conn, self.active["id"], "agent")
        for ttl in (0, -5, 86401, True):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValidationError):
                    self.mint(ttl_seconds=ttl)
        self.assertEqual(owlery.mint(self.conn, self.active["id"], "hook", now=NOW)["minted_by"], "hook")

    def test_mint_refuses_a_now_that_would_overflow_the_expiry(self):
        for now in (ids.MAX_INT, ids.MAX_INT - 600, ids.MAX_TIME + 1, -1):
            with self.subTest(now=now):
                with self.assertRaisesRegex(ValidationError, "timestamp"):
                    self.mint(now=now)
        self.assertEqual(self.count("close_tokens"), 0)
        latest = self.mint(now=ids.MAX_TIME, ttl_seconds=owlery.TOKEN_TTL_MAX)
        self.assertEqual(latest["expires_at"], ids.MAX_TIME + owlery.TOKEN_TTL_MAX)
        stored = self.conn.execute("SELECT minted_at, expires_at FROM close_tokens").fetchone()
        self.assertEqual(tuple(stored), (ids.MAX_TIME, ids.MAX_TIME + owlery.TOKEN_TTL_MAX))

    def test_token_hash_and_task_are_fixed_once_minted(self):
        self.mint()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE close_tokens SET expires_at = expires_at + 1000")


if __name__ == "__main__":
    unittest.main()
