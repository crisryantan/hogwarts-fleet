from __future__ import annotations

import os
import sqlite3

from hogwarts import db, owlery, pensieve, watch
from hogwarts.errors import IntegrityError, NotFoundError, ValidationError
from tests.support import NOW, StoreCase


class ReadOnlyOpenTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.desks()
        self.reader = db.connect_readonly(self.db_path)
        self.addCleanup(self.reader.close)

    def test_a_write_attempt_fails(self):
        for sql in ("INSERT INTO desks(name, family, created_at) VALUES ('gamma', 'claude', 1)",
                    "UPDATE events SET acked_at = 1", "CREATE TABLE planted (x INTEGER)"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.OperationalError):
                    self.reader.execute(sql)
        self.assertEqual(self.count("desks"), 2)
        self.assertEqual(self.reader.total_changes, 0)

    def test_the_file_itself_is_opened_read_only(self):
        self.reader.execute("PRAGMA query_only=OFF")
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.reader.execute("INSERT INTO desks(name, family, created_at) VALUES ('gamma', 'claude', 1)")
        self.assertIn("readonly", str(caught.exception))

    def test_the_reader_sees_new_commits(self):
        self.assertEqual(watch.marks(self.reader)["events"], 0)
        pensieve.add_event(self.conn, "alpha", "rundesk.failed", "headmaster", "it failed", now=NOW)
        self.assertEqual(watch.marks(self.reader)["events"], 1)

    def test_a_missing_database_is_never_created(self):
        missing = self.tmp / "nowhere" / "pensieve.db"
        with self.assertRaises(NotFoundError):
            db.connect_readonly(missing)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(NotFoundError):
            db.connect_readonly(self.tmp / "state" / "other.db")
        self.assertFalse((self.tmp / "state" / "other.db").exists())

    def test_unsafe_paths_are_refused(self):
        link = self.tmp / "state" / "link.db"
        os.symlink(self.db_path, link)
        with self.assertRaises(IntegrityError):
            db.connect_readonly(link)
        with self.assertRaises(ValidationError):
            db.connect_readonly("relative/pensieve.db")

    def test_a_readable_file_is_not_tightened(self):
        os.chmod(self.db_path, 0o644)
        db.connect_readonly(self.db_path).close()
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o644)


class WatchQueryTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.desks()
        self.desk("gamma", "codex")
        self.reader = db.connect_readonly(self.db_path)
        self.addCleanup(self.reader.close)

    def test_owls_after_a_mark_for_one_desk_or_all(self):
        owlery.send(self.conn, "alpha", "beta", "fyi", "first", body="never shown", now=NOW)
        mark = watch.marks(self.reader)["owls"]
        owlery.send(self.conn, "beta", "gamma", "question", "second", now=NOW + 1)
        owlery.send(self.conn, "gamma", "alpha", "fyi", "third", now=NOW + 2)
        self.assertEqual([row["subject"] for row in watch.owls_after(self.reader, 0)], ["first", "second", "third"])
        self.assertEqual([row["subject"] for row in watch.owls_after(self.reader, mark)], ["second", "third"])
        self.assertEqual([row["subject"] for row in watch.owls_after(self.reader, 0, "alpha")], ["first", "third"])
        self.assertNotIn("body", watch.owls_after(self.reader, 0)[0])

    def test_only_headmaster_events_after_a_mark(self):
        pensieve.add_event(self.conn, "alpha", "owl.doorbell", "routine", "ring", now=NOW)
        pensieve.add_event(self.conn, "alpha", "rundesk.failed", "headmaster", "failed", now=NOW)
        pensieve.add_event(self.conn, "beta", "rundesk.cap", "headmaster", "capped", now=NOW)
        self.assertEqual([row["kind"] for row in watch.headmaster_events_after(self.reader, 0)],
                         ["rundesk.failed", "rundesk.cap"])
        self.assertEqual([row["kind"] for row in watch.headmaster_events_after(self.reader, 0, "beta")],
                         ["rundesk.cap"])
        self.assertEqual(watch.headmaster_events_after(self.reader, 3), [])

    def test_metrics_after_and_run_recorded(self):
        pensieve.add_metric(self.conn, "alpha", "run-0123456789abcdef", "opus", 1, 2, 3, 0.5, 10, ts=NOW)
        pensieve.add_metric(self.conn, "beta", "run-fedcba9876543210", "codex-default", 1, 2, 3, 0.0, 10, ts=NOW)
        rows = watch.metrics_after(self.reader, 0, "alpha")
        self.assertEqual([(row["run_id"], row["cost_usd"]) for row in rows], [("run-0123456789abcdef", 0.5)])
        self.assertEqual(len(watch.metrics_after(self.reader, 0)), 2)
        self.assertTrue(watch.run_recorded(self.reader, "run-0123456789abcdef"))
        self.assertFalse(watch.run_recorded(self.reader, "run-0000000000000000"))

    def test_inputs_are_validated(self):
        for call in (lambda: watch.owls_after(self.reader, -1), lambda: watch.owls_after(self.reader, 0, "Bad Desk"),
                     lambda: watch.metrics_after(self.reader, "0"), lambda: watch.run_recorded(self.reader, "../x"),
                     lambda: watch.headmaster_events_after(self.reader, 0, limit=0)):
            with self.assertRaises(ValidationError):
                call()
