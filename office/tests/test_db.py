from __future__ import annotations

import multiprocessing
import os
import py_compile
import shutil
import sqlite3
import stat
import sys
import unittest
from unittest import mock

from hogwarts import capacity, db, followups, ids, owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError
from tests.support import MERGE_SHA, NOW, REPO, SHA, StoreCase, go_build, insert_closure, intent_file, proof, temp_dir

PACKAGE = db.CODE_ROOT / "hogwarts"


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _open_worker(db_path: str, barrier, results) -> None:
    barrier.wait(timeout=20)
    try:
        db.connect(db_path).close()
        results.put("ok")
    except ConflictError:
        results.put("conflict")
    except Exception as exc:
        results.put(repr(exc))


class _LockedThenWal:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def execute(self, sql: str):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return self

    def fetchone(self):
        return ("wal",)


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.db_path = self.tmp / "state" / "pensieve.db"

    def open(self, **kwargs):
        conn = db.connect(self.db_path, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def test_connect_creates_parent_with_0700(self):
        self.open()
        self.assertEqual(mode(self.db_path.parent), 0o700)

    def test_connect_creates_db_file_with_0600(self):
        self.open()
        self.assertEqual(mode(self.db_path), 0o600)

    def test_wal_and_shm_files_are_0600(self):
        conn = self.open()
        pensieve.add_desk(conn, "alpha", "claude", now=NOW)
        for suffix in ("-wal", "-shm"):
            sidecar = str(self.db_path) + suffix
            self.assertTrue(os.path.exists(sidecar), suffix)
            self.assertEqual(mode(sidecar), 0o600, suffix)

    def test_loose_sidecar_is_tightened_on_connect(self):
        pensieve.add_desk(self.open(), "alpha", "claude", now=NOW)
        sidecar = str(self.db_path) + "-wal"
        os.chmod(sidecar, 0o640)
        self.open()
        self.assertEqual(mode(sidecar), 0o600)

    def test_pragmas_wal_foreign_keys_busy_timeout(self):
        conn = self.open()
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA trusted_schema").fetchone()[0], 0)

    def test_create_false_requires_existing_database(self):
        with self.assertRaises(NotFoundError):
            db.connect(self.db_path, create=False)
        self.assertFalse(os.path.exists(self.db_path.parent))

    def test_missing_grandparent_is_not_created(self):
        with self.assertRaises(NotFoundError):
            db.connect(self.tmp / "missing" / "state" / "pensieve.db")

    def test_relative_path_is_refused(self):
        with self.assertRaises(ValidationError):
            db.connect("state/pensieve.db")


class FirstOpenRaceTests(unittest.TestCase):
    def test_wal_switch_retries_while_the_database_is_locked(self):
        conn = _LockedThenWal(failures=3)
        with mock.patch.object(db.time, "sleep") as sleep:
            db._enable_wal(conn)
        self.assertEqual((conn.calls, sleep.call_count), (4, 3))

    def test_wal_switch_gives_up_with_conflict_error(self):
        conn = _LockedThenWal(failures=db.WAL_ATTEMPTS)
        with mock.patch.object(db.time, "sleep"):
            with self.assertRaises(ConflictError):
                db._enable_wal(conn)

    def test_locked_errors_while_opening_or_migrating_become_conflict_errors(self):
        tmp = temp_dir(self)
        locked = sqlite3.OperationalError("database is locked")
        for target in ("_enable_wal", "migrate"):
            with self.subTest(target=target):
                path = tmp / target / "pensieve.db"
                with mock.patch.object(db, target, side_effect=locked):
                    with self.assertRaises(ConflictError):
                        db.connect(path)

    def test_racing_first_opens_of_a_fresh_database_all_succeed(self):
        tmp = temp_dir(self)
        context = multiprocessing.get_context("spawn")
        for round_number in range(2):
            path = tmp / f"round-{round_number}" / "pensieve.db"
            barrier, results = context.Barrier(6), context.Queue()
            workers = [context.Process(target=_open_worker, args=(str(path), barrier, results)) for _ in range(6)]
            for worker in workers:
                worker.start()
            outcomes = [results.get(timeout=60) for _ in workers]
            for worker in workers:
                worker.join(timeout=60)
            self.assertEqual(outcomes, ["ok"] * 6)


class MigrationTests(StoreCase):
    def test_schema_version_is_recorded(self):
        self.assertEqual(db.schema_version(self.conn), db.SCHEMA_VERSION)

    def test_migrations_are_idempotent(self):
        db.migrate(self.conn)
        second = db.connect(self.db_path)
        self.addCleanup(second.close)
        db.migrate(second)
        self.assertEqual(self.count("schema_version"), len(db.MIGRATIONS))

    def test_rerunning_v1_statements_changes_nothing(self):
        with db.transaction(self.conn):
            for statement in db.V1:
                self.conn.execute(statement)
        self.assertEqual(db.schema_version(self.conn), db.SCHEMA_VERSION)

    def test_migration_numbers_have_no_gap(self):
        self.assertEqual([version for version, _ in db.MIGRATIONS], list(range(1, db.SCHEMA_VERSION + 1)))

    def test_newer_schema_is_refused(self):
        self.conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (99, 0)")
        with self.assertRaises(IntegrityError):
            db.connect(self.db_path)

    def test_strict_tables_when_supported(self):
        if sqlite3.sqlite_version_info < (3, 37, 0):
            self.skipTest("SQLite is older than 3.37")
        for table in ("desks", "tasks", "events", "owls", "requests", "facts", "close_tokens"):
            sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            self.assertTrue(sql.rstrip().endswith("STRICT"), table)
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("INSERT INTO metrics(ts, desk, run_id, model, input_tokens, output_tokens,"
                              " cache_read_tokens, cost_usd, duration_ms) VALUES ('x', 'a', 'r', 'm', 1, 1, 1, 1, 1)")

    def test_all_v1_tables_exist(self):
        names = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        expected = {"schema_version", "desks", "tasks", "events", "sessions", "extracts", "extracts_fts",
                    "keypoints", "keypoints_fts", "facts", "metrics", "owls", "requests",
                    "request_phases", "review_passes", "close_tokens"}
        self.assertTrue(expected <= names, expected - names)

    def test_timestamps_are_integers(self):
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('alpha', 'claude', 'now')")


class MigrationV7Tests(unittest.TestCase):
    def v6_database(self):
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:6]), mock.patch.object(db, "SCHEMA_VERSION", 6):
            conn = db.connect(path)
            # Raw rows: the task API reads the V7 grant table, which a V6 store does not have yet.
            for index, (name, family) in enumerate((("harry", "codex"), ("mcgonagall", "claude"), ("moody", "codex"))):
                conn.execute("INSERT INTO desks(name, family, created_at) VALUES (?, ?, ?)", (name, family, NOW))
                conn.execute("INSERT INTO tasks(id, desk, title, status, created_at) VALUES (?, ?, 'work', 'queued', ?)",
                             (f"tk_000000000000000{index}", name, NOW))
                conn.execute("UPDATE tasks SET status = 'active', started_at = ? WHERE desk = ?", (NOW, name))
            index_sql = conn.execute("SELECT name FROM sqlite_master WHERE name = 'tasks_one_active_per_desk'")
            self.assertIsNotNone(index_sql.fetchone())
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_a_v6_database_migrates_to_7_and_grants_only_the_seed_desks_it_has(self):
        conn = self.v6_database()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        granted = [row[0] for row in conn.execute("SELECT desk FROM many_task_desks ORDER BY desk")]
        self.assertEqual(granted, ["harry", "moody"])
        self.assertTrue(set(granted) <= set(db.MANY_TASK_DESKS_SEED))
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'tasks_one_active_per_desk'")
                          .fetchone())
        self.assertEqual([(row[0], row[1]) for row in conn.execute("SELECT desk, status FROM tasks ORDER BY id")],
                         [("harry", "active"), ("mcgonagall", "active"), ("moody", "active")])
        second = pensieve.create_task(conn, "harry", "more work", now=NOW)
        self.assertEqual(pensieve.start_task(conn, second["id"], now=NOW)["status"], "active")
        third = pensieve.create_task(conn, "mcgonagall", "more work", now=NOW)
        with self.assertRaises(ConflictError):
            pensieve.start_task(conn, third["id"], now=NOW)
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        self.assertEqual(conn.total_changes, changes)

    def test_a_raw_write_never_moves_an_active_task_onto_a_single_desk(self):
        conn = self.v6_database()
        # harry is granted many tasks and mcgonagall is single; both hold an active task.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "a task keeps its desk"):
            conn.execute("UPDATE tasks SET desk = 'mcgonagall' WHERE id = 'tk_0000000000000000'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "a task keeps its desk"):
            conn.execute("UPDATE tasks SET desk = 'harry' WHERE id = 'tk_0000000000000001'")
        self.assertEqual([row[0] for row in conn.execute("SELECT desk FROM tasks WHERE status = 'active' ORDER BY id")],
                         ["harry", "mcgonagall", "moody"])

    def test_v7_leaves_rows_from_before_it_valid_with_no_review_branch(self):
        conn = self.v6_database()
        self.assertEqual([row[0] for row in conn.execute("SELECT review_branch FROM tasks")], [None, None, None])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        pensieve.set_review_branch(conn, "tk_0000000000000002", "fix/site")
        self.assertEqual(pensieve.get_task(conn, "tk_0000000000000002")["review_branch"], "fix/site")
        pensieve.set_review_branch(conn, "tk_0000000000000002", "Cris-Ryan-Tan/fix-the-@pr-skill.-v2")
        self.assertEqual(pensieve.get_task(conn, "tk_0000000000000002")["review_branch"],
                         "Cris-Ryan-Tan/fix-the-@pr-skill.-v2")

    def test_a_fresh_database_grants_no_desk(self):
        conn = db.connect(temp_dir(self) / "state" / "pensieve.db")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM many_task_desks").fetchone()[0], 0)

    def test_a_launch_keeps_its_task(self):
        conn = self.v6_database()
        conn.execute("INSERT INTO run_launches(run_id, desk, model, launched_at, task_id)"
                     " VALUES ('run-1', 'harry', 'm', 1, 'tk_0000000000000000')")
        for value in ("tk_0000000000000002", None):
            with self.subTest(value=value), self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its task"):
                conn.execute("UPDATE run_launches SET task_id = ? WHERE run_id = 'run-1'", (value,))
        conn.execute("INSERT INTO run_launches(run_id, desk, model, launched_at) VALUES ('run-2', 'harry', 'm', 1)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its task"):
            conn.execute("UPDATE run_launches SET task_id = 'tk_0000000000000000' WHERE run_id = 'run-2'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "a task of its own desk"):
            conn.execute("INSERT INTO run_launches(run_id, desk, model, launched_at, task_id)"
                         " VALUES ('run-3', 'harry', 'm', 1, 'tk_0000000000000002')")


class MigrationV8Tests(unittest.TestCase):
    SHA = "a" * 40

    def v7_database(self):
        """A V7 store with one review round opened before rounds recorded a run slot."""
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:7]), mock.patch.object(db, "SCHEMA_VERSION", 7):
            conn = db.connect(path)
            for name, family in (("alpha", "claude"), ("beta", "codex")):
                pensieve.add_desk(conn, name, family, now=NOW)
            pensieve.allow_many_tasks(conn, "beta", now=NOW)
            self.author = pensieve.start_task(conn, pensieve.create_task(conn, "alpha", "build", now=NOW)["id"],
                                              now=NOW)["id"]
            opened = owlery.open_request(conn, "alpha", "beta", "review it", parent_task_id=self.author, now=NOW)
            self.old_request = opened["request"]["id"]
            conn.execute("INSERT INTO review_rounds(request_id, task_id, reviewer, sha, round, created_at)"
                         " VALUES (?, ?, 'beta', ?, 1, ?)", (self.old_request, self.author, self.SHA, NOW))
            self.assertNotIn("slot", [row[1] for row in conn.execute("PRAGMA table_info(review_rounds)")])
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_a_v7_database_migrates_to_8_and_its_rounds_record_no_slot(self):
        conn = self.v7_database()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        [old] = capacity.review_rounds(conn, self.author)
        self.assertIsNone(old["slot"])
        opened = capacity.open_review_round(conn, self.author, "beta", self.SHA, "review again", slot=1, now=NOW)
        rows = {row["request_id"]: row["slot"] for row in capacity.review_rounds(conn, self.author)}
        self.assertEqual(rows, {self.old_request: None, opened["request"]["id"]: 1})
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        with db.transaction(conn):
            for statement in db.pending_statements(conn, db.V8):
                conn.execute(statement)
        self.assertEqual(conn.total_changes, changes)

    def test_a_round_keeps_the_slot_it_opened_with_and_a_slot_is_in_range(self):
        conn = self.v7_database()
        opened = capacity.open_review_round(conn, self.author, "beta", self.SHA, "review again", slot=0, now=NOW)
        request_id = opened["request"]["id"]
        for value in (1, None):
            with self.subTest(value=value), self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its slot"):
                conn.execute("UPDATE review_rounds SET slot = ? WHERE request_id = ?", (value, request_id))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its slot"):
            conn.execute("UPDATE review_rounds SET slot = 0 WHERE request_id = ?", (self.old_request,))
        for bad in (-1, db.RUN_SLOT_LIMIT, True, "1", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                capacity.open_review_round(conn, self.author, "beta", self.SHA, "review", slot=bad, now=NOW)
        spare = owlery.open_request(conn, "alpha", "beta", "spare", parent_task_id=self.author, now=NOW)
        for bad in (-1, db.RUN_SLOT_LIMIT):
            with self.subTest(raw=bad), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
                conn.execute("INSERT INTO review_rounds(request_id, task_id, reviewer, sha, round, created_at, slot)"
                             " VALUES (?, ?, 'beta', ?, 9, ?, ?)", (spare["request"]["id"], self.author, self.SHA,
                                                                   NOW, bad))
        self.assertEqual([row["slot"] for row in capacity.review_rounds(conn, self.author)], [None, 0])


class MigrationV9Tests(unittest.TestCase):
    DIGEST = "c" * 64
    DRAFTED = "tk_00000000000000d1"

    def v8_database(self):
        """A V8 store with a task registered by hand, with its TASK.md, before go specs existed."""
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:8]), mock.patch.object(db, "SCHEMA_VERSION", 8):
            conn = db.connect(path)
            pensieve.add_desk(conn, "alpha", "claude", now=NOW)
            pensieve.create_task(conn, "alpha", "drafted by hand", intent_path=ids.intent_path(self.DRAFTED),
                                 task_id=self.DRAFTED, now=NOW)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'task_specs'").fetchone())
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def drafted(self, conn, task_id: str) -> str:
        return pensieve.create_task(conn, "alpha", "drafted", intent_path=ids.intent_path(task_id), task_id=task_id,
                                    now=NOW)["id"]

    def test_a_v8_database_migrates_to_9_and_its_tasks_have_no_spec(self):
        conn = self.v8_database()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        self.assertIsNone(pensieve.task_spec(conn, self.DRAFTED))
        spec = pensieve.record_spec(conn, self.DRAFTED, "/private/tmp/checkout", "fix/site", "origin/main", self.DIGEST,
                                    now=NOW)
        self.assertEqual({key: spec[key] for key in ("task_id", "repo_dir", "branch", "base", "intent_sha256")},
                         {"task_id": self.DRAFTED, "repo_dir": "/private/tmp/checkout", "branch": "fix/site",
                          "base": "origin/main", "intent_sha256": self.DIGEST})
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        with db.transaction(conn):
            for statement in db.pending_statements(conn, db.V9):
                conn.execute(statement)
        self.assertEqual(conn.total_changes, changes)

    def test_a_spec_never_changes_and_is_never_deleted(self):
        conn = self.v8_database()
        pensieve.record_spec(conn, self.DRAFTED, "/private/tmp/checkout", "fix/site", "origin/main", self.DIGEST,
                             now=NOW)
        with self.assertRaisesRegex(ConflictError, "never changes"):
            pensieve.record_spec(conn, self.DRAFTED, "/private/tmp/other", "fix/other", "origin/main", self.DIGEST,
                                 now=NOW)
        for column, value in (("repo_dir", "/private/tmp/other"), ("branch", "fix/other"), ("base", "origin/dev"),
                              ("intent_sha256", "d" * 64), ("recorded_at", NOW + 1)):
            with self.subTest(column=column), self.assertRaisesRegex(sqlite3.IntegrityError, "keeps the spec"):
                conn.execute(f"UPDATE task_specs SET {column} = ? WHERE task_id = ?", (value, self.DRAFTED))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            conn.execute("DELETE FROM task_specs WHERE task_id = ?", (self.DRAFTED,))
        self.assertEqual(pensieve.task_spec(conn, self.DRAFTED)["branch"], "fix/site")

    def test_a_spec_is_recorded_only_on_a_queued_task_with_its_task_md(self):
        conn = self.v8_database()
        bare = pensieve.create_task(conn, "alpha", "no TASK.md", now=NOW)["id"]
        started = self.drafted(conn, "tk_00000000000000d2")
        pensieve.start_task(conn, started, now=NOW)
        for task_id in (bare, started):
            with self.subTest(task=task_id):
                with self.assertRaisesRegex(ConflictError, "queued task with its TASK.md"):
                    pensieve.record_spec(conn, task_id, "/private/tmp/checkout", "fix/site", "origin/main",
                                         self.DIGEST, now=NOW)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "queued task with its TASK.md"):
                    conn.execute("INSERT INTO task_specs(task_id, repo_dir, branch, base, intent_sha256, recorded_at)"
                                 " VALUES (?, '/private/tmp/checkout', 'fix/site', 'origin/main', ?, ?)",
                                 (task_id, self.DIGEST, NOW))
        with self.assertRaises(NotFoundError):
            pensieve.record_spec(conn, "tk_00000000000000ff", "/private/tmp/checkout", "fix/site", "origin/main",
                                 self.DIGEST, now=NOW)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_specs").fetchone()[0], 0)

    def test_a_spec_keeps_plain_shapes(self):
        conn = self.v8_database()
        good = {"repo_dir": "/private/tmp/checkout", "branch": "fix/site", "base": "origin/main",
                "intent_sha256": self.DIGEST}
        for field, bad in (("repo_dir", "checkout"), ("repo_dir", "/private/tmp/../etc"), ("repo_dir", ids.CASTLE_ROOT),
                           ("repo_dir", ids.TASKS_ROOT + "/x"), ("repo_dir", ids.OFFICE_ROOT + "/state"),
                           ("branch", "Fix/Site"), ("branch", "fix site"), ("branch", "fix/../site"), ("branch", ""),
                           ("base", "origin main"), ("base", "origin/..main"), ("base", "-x"),
                           ("intent_sha256", "C" * 64), ("intent_sha256", "c" * 63), ("intent_sha256", None)):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValidationError):
                pensieve.record_spec(conn, self.DRAFTED, **{**good, field: bad}, now=NOW)
        for column, bad in (("repo_dir", "checkout"), ("branch", "Fix/Site"), ("branch", "x" * 101),
                            ("base", "origin main"), ("intent_sha256", "C" * 64)):
            values = {**good, column: bad}
            with self.subTest(raw=column), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
                conn.execute("INSERT INTO task_specs(task_id, repo_dir, branch, base, intent_sha256, recorded_at)"
                             " VALUES (?, ?, ?, ?, ?, ?)", (self.DRAFTED, values["repo_dir"], values["branch"],
                                                           values["base"], values["intent_sha256"], NOW))
        self.assertIsNone(pensieve.task_spec(conn, self.DRAFTED))


class MigrationAutoPatchesTests(unittest.TestCase):
    """The auto-portrait migration, found by name: its number is kept in db.MIGRATIONS only."""

    SHA = "e" * 64
    OTHER = "f" * 64

    @staticmethod
    def version() -> int:
        return next(version for version, statements in db.MIGRATIONS if statements is db.AUTO_PATCHES)

    def older_database(self):
        """A store from just before the migration, with an owl filed."""
        path = temp_dir(self) / "state" / "pensieve.db"
        older = tuple(migration for migration in db.MIGRATIONS if migration[0] < self.version())
        with mock.patch.object(db, "MIGRATIONS", older), mock.patch.object(db, "SCHEMA_VERSION", older[-1][0]):
            conn = db.connect(path)
            pensieve.add_desk(conn, "alpha", "claude", now=NOW)
            pensieve.add_desk(conn, "beta", "script", now=NOW)
            self.owl = owlery.send(conn, "beta", "alpha", "fyi", "export", body="the day", now=NOW)["id"]
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'auto_patches'").fetchone())
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def armed(self, conn, date: str = "2027-01-15", before: str = "absent", before_sha=None) -> dict:
        return pensieve.arm_auto_patch(conn, date, self.owl, before, before_sha, now=NOW)

    def validated(self, conn, date: str = "2027-01-15") -> dict:
        self.armed(conn, date)
        return pensieve.snapshot_auto_patch(conn, date, self.SHA, "[]", ["f1", "f2"], ["f2"], [], now=NOW + 1)

    def test_an_older_database_migrates_with_no_auto_rows(self):
        conn = self.older_database()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM auto_patches").fetchone()[0], 0)
        self.assertEqual(pensieve.open_auto_patches(conn), [])
        row = self.armed(conn)
        self.assertEqual((row["state"], row["attempt"], row["owl_acked_at"]), ("armed", 1, None))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        with db.transaction(conn):
            for statement in db.pending_statements(conn, db.AUTO_PATCHES):
                conn.execute(statement)
        self.assertEqual(conn.total_changes, changes)

    def test_an_auto_row_opens_armed_with_no_snapshot(self):
        conn = self.older_database()
        row = self.armed(conn, before="present", before_sha=self.OTHER)
        self.assertEqual((row["state"], row["attempt"], row["before"], row["before_sha256"], row["sha256"],
                          row["ops"], row["outcome"], row["finished_at"]),
                         ("armed", 1, "present", self.OTHER, None, None, None, None))
        base = {"date": "'2027-01-16'", "attempt": "1", "state": "'armed'", "owl_id": "?", "armed_at": "?",
                "before": "'absent'"}
        for column, value in (("state", "'validated'"), ("attempt", "2"), ("sha256", f"'{self.SHA}'"),
                              ("outcome", "'x'"), ("applied_ids", "''"), ("finished_at", "1")):
            values = {**base, column: value}
            with self.subTest(insert=column), self.assertRaisesRegex(sqlite3.IntegrityError, "opens armed"):
                conn.execute(f"INSERT INTO auto_patches({', '.join(values)}) VALUES ({', '.join(values.values())})",
                             (self.owl, NOW))
        self.assertIsNone(pensieve.auto_patch(conn, "2027-01-16"))

    def test_a_snapshot_is_written_once(self):
        conn = self.older_database()
        row = self.validated(conn)
        self.assertEqual((row["state"], row["sha256"], row["ops"], row["order_ids"], row["held_ids"], row["unfit_ids"]),
                         ("validated", self.SHA, "[]", "f1,f2", "f2", ""))
        with self.assertRaisesRegex(ConflictError, "only an armed night"):
            pensieve.snapshot_auto_patch(conn, "2027-01-15", self.OTHER, "[]", ["f1"], [], [], now=NOW)
        for column, value in (("sha256", self.OTHER), ("ops", "[1]"), ("order_ids", "f1"), ("held_ids", ""),
                              ("unfit_ids", "f2"), ("validated_at", NOW + 5)):
            with self.subTest(column=column), self.assertRaisesRegex(sqlite3.IntegrityError, "written once"):
                conn.execute(f"UPDATE auto_patches SET {column} = ? WHERE date = '2027-01-15'", (value,))
        pensieve.end_auto_patch(conn, "2027-01-15", "done", "applied f1", ["f1"], now=NOW + 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "written once"):
            conn.execute("UPDATE auto_patches SET sha256 = ? WHERE date = '2027-01-15'", (self.OTHER,))
        # A snapshot is written only as an armed night is validated, never on the way to another state.
        self.armed(conn, "2027-01-16")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "written once"):
            conn.execute("UPDATE auto_patches SET state = 'done', sha256 = ?, ops = '[]', order_ids = 'f1',"
                         " held_ids = '', unfit_ids = '', validated_at = 1, outcome = 'x', finished_at = 1"
                         " WHERE date = '2027-01-16'", (self.SHA,))
        self.assertEqual(pensieve.auto_patch(conn, "2027-01-15")["sha256"], self.SHA)

    def test_states_only_move_forward_and_a_night_that_read_nothing_can_rearm(self):
        conn = self.older_database()
        self.armed(conn)
        stopped = pensieve.end_auto_patch(conn, "2027-01-15", "stopped", "cut off", now=NOW + 1)
        self.assertEqual((stopped["state"], stopped["outcome"], stopped["finished_at"]), ("stopped", "cut off", NOW + 1))
        with self.assertRaisesRegex(ConflictError, "never changes"):
            pensieve.end_auto_patch(conn, "2027-01-15", "off", "again", now=NOW + 2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ending is written"):
            conn.execute("UPDATE auto_patches SET outcome = 'rewritten' WHERE date = '2027-01-15'")
        again = self.armed(conn, before="present", before_sha=self.OTHER)
        self.assertEqual((again["attempt"], again["state"], again["outcome"], again["finished_at"], again["before"]),
                         (2, "armed", None, None, "present"))
        pensieve.end_auto_patch(conn, "2027-01-15", "off", "switched off", now=NOW + 3)
        self.assertEqual(self.armed(conn)["attempt"], 3)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "armed again only"):
            conn.execute("UPDATE auto_patches SET attempt = attempt + 2 WHERE date = '2027-01-15'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "armed again only"):
            conn.execute("UPDATE auto_patches SET before = 'unreadable' WHERE date = '2027-01-15'")
        # After a snapshot: forward to an ending only, and no ending is ever armed again.
        self.validated(conn, "2027-01-16")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only moves forward|armed again only"):
            conn.execute("UPDATE auto_patches SET state = 'armed' WHERE date = '2027-01-16'")
        pensieve.end_auto_patch(conn, "2027-01-16", "stopped", "could not apply", now=NOW + 4)
        with self.assertRaisesRegex(ConflictError, "never armed again"):
            self.armed(conn, "2027-01-16")
        for state in ("armed", "validated", "done", "off"):
            with self.subTest(state=state), self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE auto_patches SET state = ? WHERE date = '2027-01-16'", (state,))
        self.validated(conn, "2027-01-17")
        pensieve.end_auto_patch(conn, "2027-01-17", "done", "applied f1", ["f1"], now=NOW + 5)
        for state in ("armed", "validated", "stopped", "off"):
            with self.subTest(done_to=state), self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE auto_patches SET state = ? WHERE date = '2027-01-17'", (state,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ending is written"):
            conn.execute("UPDATE auto_patches SET applied_ids = 'f2' WHERE date = '2027-01-17'")
        # A night with no patch ends done with no snapshot, and done is final.
        self.armed(conn, "2027-01-18")
        pensieve.end_auto_patch(conn, "2027-01-18", "done", "no patch", now=NOW + 6)
        with self.assertRaisesRegex(ConflictError, "never armed again"):
            self.armed(conn, "2027-01-18")
        self.assertEqual([row["date"] for row in pensieve.open_auto_patches(conn)], ["2027-01-15"])

    def test_auto_rows_are_never_deleted(self):
        conn = self.older_database()
        self.armed(conn)
        self.validated(conn, "2027-01-16")
        for date in ("2027-01-15", "2027-01-16"):
            with self.subTest(date=date), self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
                conn.execute("DELETE FROM auto_patches WHERE date = ?", (date,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM auto_patches").fetchone()[0], 2)

    def test_an_auto_row_keeps_its_date(self):
        conn = self.older_database()
        self.armed(conn, "2027-01-11")
        self.validated(conn, "2027-01-12")
        self.validated(conn, "2027-01-13")
        pensieve.end_auto_patch(conn, "2027-01-13", "done", "applied", [], now=NOW)
        for date, state in (("2027-01-14", "stopped"), ("2027-01-15", "off")):
            self.armed(conn, date)
            pensieve.end_auto_patch(conn, date, state, "ended", now=NOW)
        for row in conn.execute("SELECT date, state FROM auto_patches").fetchall():
            with self.subTest(state=row["state"]), self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its date"):
                conn.execute("UPDATE auto_patches SET date = '2027-02-01' WHERE date = ?", (row["date"],))
        self.assertIsNone(pensieve.auto_patch(conn, "2027-02-01"))

    def test_auto_rows_keep_plain_shapes(self):
        conn = self.older_database()
        for date in ("2027-1-15", "2027-02-30", "../2027-01-15", "", None):
            with self.subTest(date=date), self.assertRaises(ValidationError):
                pensieve.arm_auto_patch(conn, date, self.owl, "absent", None, now=NOW)
        for before, before_sha in (("present", None), ("absent", self.SHA), ("present", "E" * 64), ("there", None)):
            with self.subTest(before=before, sha=before_sha), self.assertRaises(ValidationError):
                pensieve.arm_auto_patch(conn, "2027-01-15", self.owl, before, before_sha, now=NOW)
        with self.assertRaises(ValidationError):
            pensieve.arm_auto_patch(conn, "2027-01-15", "owl_nope", "absent", None, now=NOW)
        self.armed(conn)
        good = {"sha256": self.SHA, "ops_json": "[]", "order_ids": ["f1"], "held_ids": [], "unfit_ids": []}
        for field, bad in (("sha256", "E" * 64), ("sha256", "e" * 63), ("ops_json", "[\"caf\u00e9\"]"),
                           ("ops_json", "[\"a\nb\"]"), ("ops_json", "x" * (db.AUTO_PATCH_OPS_MAX + 1)),
                           ("order_ids", []), ("order_ids", ["F1"]), ("order_ids", ["f1", "f1"]),
                           ("order_ids", ["f,1"]), ("held_ids", ["f2"]), ("unfit_ids", ["a" * 25]),
                           ("held_ids", "f1")):
            with self.subTest(field=field, bad=str(bad)[:20]), self.assertRaises(ValidationError):
                pensieve.snapshot_auto_patch(conn, "2027-01-15", **{**good, field: bad}, now=NOW)
        for outcome in ("", "caf\u00e9", "two\nlines", "x" * (db.AUTO_PATCH_OUTCOME_MAX + 1), None):
            with self.subTest(outcome=str(outcome)[:20]), self.assertRaises(ValidationError):
                pensieve.end_auto_patch(conn, "2027-01-15", "stopped", outcome, now=NOW)
        with self.assertRaises(ValidationError):
            pensieve.end_auto_patch(conn, "2027-01-15", "stopped", "x", ["f1"], now=NOW)
        with self.assertRaises(ValidationError):
            pensieve.end_auto_patch(conn, "2027-01-15", "validated", "x", now=NOW)
        self.assertEqual(pensieve.auto_patch(conn, "2027-01-15")["state"], "armed")
        full = '"' + "a" * (db.AUTO_PATCH_OPS_MAX - 2) + '"'
        stored = pensieve.snapshot_auto_patch(conn, "2027-01-15", self.SHA, full, ["f1"], [], [], now=NOW)
        self.assertEqual(len(stored["ops"]), db.AUTO_PATCH_OPS_MAX)
        # The same shapes hold for raw writes.
        raw = ("INSERT INTO auto_patches(date, attempt, state, owl_id, armed_at, before, before_sha256)"
               " VALUES (?, 1, 'armed', ?, ?, ?, ?)")
        for date, before, before_sha in (("2027-1-15", "absent", None), ("2027-01-1x", "absent", None),
                                         ("2027-01-16", "present", None), ("2027-01-16", "absent", self.SHA),
                                         ("2027-01-16", "present", "E" * 64), ("2027-01-16", "there", None)):
            with self.subTest(raw_date=date, before=before), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                conn.execute(raw, (date, self.owl, NOW, before, before_sha))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            conn.execute(raw, ("2027-01-16", "owl_0000000000000000", NOW, "absent", None))
        self.armed(conn, "2027-01-16")
        snapshot = ("UPDATE auto_patches SET state = 'validated', sha256 = ?, ops = ?, order_ids = ?, held_ids = '',"
                    " unfit_ids = '', validated_at = 1 WHERE date = '2027-01-16'")
        for sha, ops, order in ((self.SHA, "[\"caf\u00e9\"]", "f1"), (self.SHA, "x" * (db.AUTO_PATCH_OPS_MAX + 1), "f1"),
                                (self.SHA, "[]", "F1"), (self.SHA, "[]", "f1;f2"), ("E" * 64, "[]", "f1"),
                                (self.SHA, "[]", "")):
            with self.subTest(raw_ops=ops[:12], order=order), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                conn.execute(snapshot, (sha, ops, order))
        conn.execute(snapshot, (self.SHA, "x" * db.AUTO_PATCH_OPS_MAX, "f1"))
        for outcome in ("caf\u00e9", "x" * (db.AUTO_PATCH_OUTCOME_MAX + 1), ""):
            with self.subTest(raw_outcome=outcome[:12]), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                conn.execute("UPDATE auto_patches SET state = 'stopped', outcome = ?, finished_at = 1"
                             " WHERE date = '2027-01-16'", (outcome,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
            conn.execute("UPDATE auto_patches SET state = 'stopped', outcome = 'x', finished_at = 1,"
                         " applied_ids = 'f1' WHERE date = '2027-01-16'")


class TransactionTests(StoreCase):
    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('alpha', 'claude', 1)")
                raise RuntimeError("boom")
        self.assertEqual(self.count("desks"), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_transaction_takes_the_write_lock_up_front(self):
        other = sqlite3.connect(str(self.db_path), timeout=0, isolation_level=None)
        self.addCleanup(other.close)
        with db.transaction(self.conn):
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")

    def test_busy_database_becomes_conflict_error(self):
        other = sqlite3.connect(str(self.db_path), timeout=0, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: other.in_transaction and other.execute("ROLLBACK"))
        self.conn.execute("PRAGMA busy_timeout=0")
        with self.assertRaises(ConflictError):
            with db.transaction(self.conn):
                pass

    def test_nested_transactions_join_the_outer_one(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                pensieve.add_desk(self.conn, "alpha", "claude", now=NOW)
                raise RuntimeError("boom")
        self.assertEqual(self.count("desks"), 0)

    def test_a_failed_nested_write_undoes_only_its_own_changes(self):
        with db.transaction(self.conn):
            pensieve.add_desk(self.conn, "alpha", "claude", now=NOW)
            with self.assertRaises(RuntimeError):
                with db.transaction(self.conn):
                    self.conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('beta', 'codex', 1)")
                    raise RuntimeError("boom")
            self.assertTrue(self.conn.in_transaction)
            pensieve.add_desk(self.conn, "gamma", "codex", now=NOW)
        self.assertEqual([desk["name"] for desk in pensieve.list_desks(self.conn)], ["alpha", "gamma"])
        self.assertFalse(self.conn.in_transaction)

    def test_writes_inside_a_snapshot_are_refused(self):
        other = db.connect(self.db_path)
        self.addCleanup(other.close)
        with db.snapshot(self.conn):
            self.conn.execute("SELECT COUNT(*) FROM desks").fetchone()
            pensieve.add_desk(other, "beta", "codex", now=NOW)
            with self.assertRaisesRegex(StoreError, "snapshot"):
                pensieve.add_desk(self.conn, "alpha", "claude", now=NOW)
        self.assertEqual([desk["name"] for desk in pensieve.list_desks(self.conn)], ["beta"])

    def test_writes_inside_a_transaction_the_caller_opened_are_refused(self):
        self.conn.execute("BEGIN")
        self.addCleanup(lambda: self.conn.in_transaction and self.conn.execute("ROLLBACK"))
        with self.assertRaisesRegex(StoreError, "caller opened"):
            pensieve.add_desk(self.conn, "alpha", "claude", now=NOW)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.count("desks"), 0)

    def test_sqlite_integrity_error_becomes_store_integrity_error(self):
        with self.assertRaises(IntegrityError):
            with db.transaction(self.conn):
                self.conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('alpha', 'elf', 1)")


class DoctorTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.code = self.tmp / "code"
        shutil.copytree(PACKAGE, self.code / "hogwarts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.code / "bin").mkdir()
        for name, module in (("castle", "hogwarts.cli"), ("fleet", "fleet.tools")):
            wrapper = self.code / "bin" / name
            wrapper.write_text(
                "#!/bin/sh\n"
                "exec /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty "
                "-c 'import sys; sys.path.insert(0, \"/Users/crisryantan/.hogwarts\"); "
                f"from {module} import main; sys.exit(main())' \"$@\"\n"
            )
            wrapper.chmod(0o700)
        patcher = mock.patch.object(db, "CODE_ROOT", self.code)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_doctor_reports_a_healthy_database(self):
        report = db.doctor(self.db_path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["schema_version"], db.SCHEMA_VERSION)
        self.assertEqual(report["integrity_check"], "ok")
        self.assertTrue(report["fts5"])
        self.assertEqual(report["checks"]["database"]["mode"], "0o600")

    def test_doctor_reports_a_missing_database_without_creating_it(self):
        missing = self.tmp / "other" / "pensieve.db"
        report = db.doctor(missing)
        self.assertFalse(report["ok"])
        self.assertFalse(os.path.exists(missing.parent))

    def test_doctor_reports_both_healthy_wrappers(self):
        report = db.doctor(self.db_path, code_root=self.code)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(set(report["wrappers"]), {"castle", "fleet"})
        for name, check in report["wrappers"].items():
            self.assertEqual(check["path"], str(self.code / "bin" / name))
            self.assertTrue(check["exists"])
            self.assertEqual(check["mode"], "0o700")
            self.assertEqual(check["problems"], [])

    def test_doctor_rejects_missing_edited_or_unsafe_wrappers(self):
        for name in ("castle", "fleet"):
            wrapper = self.code / "bin" / name
            expected = wrapper.read_bytes()
            for defect in ("missing", "edited", "binary", "mode", "symlink", "directory", "owner"):
                with self.subTest(wrapper=name, defect=defect):
                    wrapper.unlink()
                    wrapper.write_bytes(expected)
                    wrapper.chmod(0o700)
                    if defect == "missing":
                        wrapper.unlink()
                    elif defect == "edited":
                        wrapper.write_bytes(expected + b"echo changed\n")
                    elif defect == "binary":
                        wrapper.write_bytes(b"\xff")
                    elif defect == "mode":
                        wrapper.chmod(0o755)
                    elif defect == "symlink":
                        wrapper.unlink()
                        wrapper.symlink_to(self.code / "bin" / ("fleet" if name == "castle" else "castle"))
                    elif defect == "directory":
                        wrapper.unlink()
                        wrapper.mkdir(mode=0o700)
                    try:
                        with mock.patch.object(db, "_uid", return_value=os.getuid() + 1 if defect == "owner" else os.getuid()):
                            report = db.doctor(self.db_path, code_root=self.code)
                        self.assertFalse(report["ok"])
                        problems = report["wrappers"][name]["problems"]
                        self.assertTrue(problems)
                        self.assertTrue(all(problem in report["problems"] for problem in problems))
                        if defect != "owner":
                            other = "fleet" if name == "castle" else "castle"
                            self.assertEqual(report["wrappers"][other]["problems"], [])
                    finally:
                        if defect == "directory":
                            wrapper.rmdir()
                        elif os.path.lexists(wrapper):
                            wrapper.unlink()
                        wrapper.write_bytes(expected)
                        wrapper.chmod(0o700)

    def test_doctor_flags_loose_permissions(self):
        os.chmod(self.db_path.parent, 0o770)
        self.addCleanup(os.chmod, self.db_path.parent, 0o700)
        report = db.doctor(self.db_path)
        self.assertFalse(report["ok"])
        self.assertIn("database directory is group or world writable", report["problems"])

    def test_doctor_fails_on_bytecode_that_could_shadow_the_source(self):
        source = self.code / "hogwarts" / "__init__.py"
        planted = self.code / "hogwarts" / "__pycache__" / f"__init__.{sys.implementation.cache_tag}.pyc"
        py_compile.compile(str(source), cfile=str(planted), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        (self.code / "hogwarts" / "shadow.cpython-39-darwin.so").write_bytes(b"")
        report = db.doctor(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["bytecode"], ["hogwarts/__pycache__", "hogwarts/shadow.cpython-39-darwin.so"])
        self.assertTrue(any("hogwarts/__pycache__" in problem for problem in report["problems"]))

    def test_doctor_scans_the_code_root_by_default(self):
        with mock.patch.object(db, "stray_bytecode", return_value=[]) as scan:
            db.doctor(self.db_path)
        scan.assert_called_once_with(self.code)


class MigrationPrFollowupTests(unittest.TestCase):
    """The PR follow-up migration, found by its name, so it can take another number without these tests changing."""

    def number(self) -> int:
        return next(number for number, statements in db.MIGRATIONS if statements is db.V_PR_FOLLOWUPS)

    def older_database(self):
        """A store from just before the follow-up migration, with a build task that passed and a review round."""
        path = temp_dir(self) / "state" / "pensieve.db"
        older = [migration for migration in db.MIGRATIONS if migration[0] < self.number()]
        with mock.patch.object(db, "MIGRATIONS", tuple(older)), mock.patch.object(db, "SCHEMA_VERSION", older[-1][0]):
            conn = db.connect(path)
            for name, family in (("alpha", "claude"), ("beta", "codex")):
                pensieve.add_desk(conn, name, family, now=NOW)
            pensieve.allow_many_tasks(conn, "beta", now=NOW)
            task = pensieve.start_task(conn, pensieve.create_task(conn, "beta", "build", now=NOW)["id"], now=NOW)["id"]
            pensieve.record_commit(conn, task, "acme/web-app", "a" * 40)
            opened = owlery.open_request(conn, "beta", "alpha", "review it", parent_task_id=task, now=NOW)
            conn.execute("INSERT INTO review_rounds(request_id, task_id, reviewer, sha, round, created_at)"
                         " VALUES (?, ?, 'alpha', ?, 1, ?)", (opened["request"]["id"], task, "a" * 40, NOW))
            conn.execute("INSERT INTO round_allowances(task_id, granted_at) VALUES (?, ?)", (task, NOW))
            pensieve.mark_awaiting_close(conn, task, now=NOW)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'pr_followups'").fetchone())
            conn.close()
        self.task = task
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_an_older_database_migrates_and_no_task_has_a_pr_or_followup(self):
        conn = self.older_database()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        self.assertGreaterEqual(db.SCHEMA_VERSION, self.number())
        for table in ("task_prs", "followup_live", "pr_followups", "pr_followup_items", "pr_comments", "pr_replies"):
            with self.subTest(table=table):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual([row[0] for row in conn.execute("SELECT followup_id FROM review_rounds")], [None])
        self.assertEqual([row[0] for row in conn.execute("SELECT followup_id FROM round_allowances")], [None])
        self.assertEqual(pensieve.get_task(conn, self.task)["status"], "awaiting_close")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # An older round and allowance count for the build, as before.
        self.assertFalse(capacity.needs_allowance(conn, self.task, 1))
        changes = conn.total_changes
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        with db.transaction(conn):
            for statement in db.pending_statements(conn, db.V_PR_FOLLOWUPS):
                conn.execute(statement)
        self.assertEqual(conn.total_changes, changes)
        # The one way back to active is in place: no raw write reopens a passed task.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only to start a PR follow-up"):
            conn.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (self.task,))


class MigrationProvenCloseTests(unittest.TestCase):
    """The proven-close migration, found by name, so its number can change at a merge without touching these tests."""

    def migration(self) -> tuple:
        index = next(index for index, (_, statements) in enumerate(db.MIGRATIONS) if statements is db.V_PROVEN_CLOSES)
        return db.MIGRATIONS[:index], db.MIGRATIONS[index - 1][0]

    def before_proven_closes(self):
        """A store at the version before the migration, holding a go build that passed its round, opened at the
        newest."""
        earlier, version = self.migration()
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", earlier), mock.patch.object(db, "SCHEMA_VERSION", version):
            conn = db.connect(path)
            self.registry(conn)
            self.go = go_build(conn)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'task_closures'").fetchone())
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def fresh(self):
        conn = db.connect(temp_dir(self) / "state" / "pensieve.db")
        self.addCleanup(conn.close)
        self.registry(conn)
        self.go = go_build(conn)
        return conn

    @staticmethod
    def registry(conn) -> None:
        for name, family in (("alpha", "claude"), ("beta", "codex"), ("gamma", "claude"), ("delta", "codex")):
            pensieve.add_desk(conn, name, family, now=NOW)
        pensieve.allow_many_tasks(conn, "beta", now=NOW)

    def close_raw(self, conn, task_id: str) -> None:
        conn.execute("UPDATE tasks SET status = 'closed', close_reason = 'complete', closed_at = ? WHERE id = ?",
                     (NOW, task_id))

    @staticmethod
    def bound_build(conn, parent: str = "tk_00000000000000a1", repo: str = REPO, sha: str = SHA) -> str:
        """A go build as go_build makes one, with its worktree, awaiting close after a round PASS at sha, and the PR the
        review loop opened bound to it. Needs desks alpha, beta and gamma."""
        pensieve.create_task(conn, "gamma", "the go task", intent_path=intent_file(parent), task_id=parent, now=NOW)
        pensieve.record_spec(conn, parent, "/private/tmp/checkout", "fix/site", "origin/main", "c" * 64, now=NOW)
        build = owlery.open_request(conn, "gamma", "beta", "build it", parent_task_id=parent, now=NOW)["task"]["id"]
        pensieve.start_task(conn, build, now=NOW)
        pensieve.set_worktree(conn, build, f"{ids.WORKTREES_ROOT}/{build}")
        pensieve.record_commit(conn, build, repo, sha, now=NOW)
        opened = capacity.open_review_round(conn, build, "alpha", sha, "review it", now=NOW)
        capacity.record_round_verdict(conn, opened["request"]["id"], repo, "PASS", now=NOW)
        pensieve.close_task(conn, opened["task"]["id"], "superseded", now=NOW)
        pensieve.mark_awaiting_close(conn, build, now=NOW)
        followups.bind_pr(conn, build, repo, 7, "fix/site", "main", sha, f"https://github.com/{repo}/pull/7", now=NOW)
        return build

    @staticmethod
    def passed_followup(conn, build: str, push_needed: bool, repo: str = REPO, sha: str = SHA) -> dict:
        """A teammate follow-up of the build whose own round passed at sha: pushing, or straight to posting."""
        item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": "11",
                "url": f"https://github.com/{repo}/pull/7#issuecomment-11", "quote": None}
        row = followups.open_followup(conn, build, ids.new_id("followup"), sha, [item],
                                      [{"kind": "comment", "comment_id": "11", "label": "T1"}], "map", "follow-up 1",
                                      "the fix request", 5, now=NOW)
        followups.advance(conn, row["id"], "starting", now=NOW)
        followups.advance(conn, row["id"], "building", now=NOW)
        tagged = capacity.open_review_round(conn, build, "alpha", sha, "follow-up review", followup_id=row["id"],
                                            followup_max_rounds=2, now=NOW)
        capacity.record_round_verdict(conn, tagged["request"]["id"], repo, "PASS", now=NOW)
        pensieve.close_task(conn, tagged["task"]["id"], "superseded", now=NOW)
        pensieve.mark_awaiting_close(conn, build, now=NOW)
        return followups.plan_replies(conn, row["id"], sha, [{"label": "T1", "mark": "PUSHBACK", "body": "It stays."}],
                                      push_needed, now=NOW)

    def test_a_followup_opened_before_the_migration_holds_off_a_proven_close_until_it_ends(self):
        earlier, version = self.migration()
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", earlier), mock.patch.object(db, "SCHEMA_VERSION", version):
            conn = db.connect(path)
            self.registry(conn)
            pensieve.add_desk(conn, "map", "script", now=NOW)
            build = self.bound_build(conn)
            row = self.passed_followup(conn, build, push_needed=True)
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        refused = "a task with an open PR follow-up is never closed by proof"
        for state in ("pushing", "posting"):
            with self.subTest(state=state):
                if state == "posting":
                    followups.advance(conn, row["id"], "posting", now=NOW)
                self.assertEqual(followups.get(conn, row["id"])["state"], state)
                with self.assertRaisesRegex(IntegrityError, refused):
                    pensieve.close_proven(conn, build, proof(), "closed", "close:proven:x")
                with self.assertRaisesRegex(sqlite3.IntegrityError, refused):
                    insert_closure(conn, build)
                self.assertEqual(pensieve.get_task(conn, build)["status"], "awaiting_close")
                self.assertIsNone(pensieve.task_closure(conn, build))
        followups.advance(conn, row["id"], "done", now=NOW)
        pensieve.close_proven(conn, build, proof(), "closed", "close:proven:x")
        self.assertEqual(pensieve.get_task(conn, build)["close_reason"], "complete")

    def test_a_task_closed_by_proof_never_opens_a_followup_and_a_stopped_one_lets_the_close_through(self):
        conn = db.connect(temp_dir(self) / "state" / "pensieve.db")
        self.addCleanup(conn.close)
        self.registry(conn)
        pensieve.add_desk(conn, "map", "script", now=NOW)
        build = self.bound_build(conn)
        pensieve.close_proven(conn, build, proof(), "closed", "close:proven:x")
        item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": "11",
                "url": f"https://github.com/{REPO}/pull/7#issuecomment-11", "quote": None}
        with self.assertRaisesRegex(ConflictError, "awaiting close"):
            followups.open_followup(conn, build, ids.new_id("followup"), SHA, [item],
                                    [{"kind": "comment", "comment_id": "11", "label": "T1"}], "map", "follow-up 1",
                                    "the fix request", 5, now=NOW)
        self.assertEqual(followups.list_followups(conn, build), [])
        # A follow-up that stopped has ended: it holds off no close.
        repo, sha = "acme/other-app", "4" * 40
        other = self.bound_build(conn, parent="tk_00000000000000b1", repo=repo, sha=sha)
        row = self.passed_followup(conn, other, push_needed=False, repo=repo, sha=sha)
        with self.assertRaisesRegex(IntegrityError, "never closed by proof"):
            pensieve.close_proven(conn, other, proof(repo=repo, pass_sha=sha), "closed", "close:proven:y")
        followups.stop(conn, row["id"], "stopped by a test", now=NOW)
        pensieve.close_proven(conn, other, proof(repo=repo, pass_sha=sha), "closed", "close:proven:y")
        self.assertEqual(pensieve.get_task(conn, other)["close_reason"], "complete")

    def test_adds_task_closures_and_keeps_rows(self):
        conn = self.before_proven_closes()
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        self.assertEqual(db.MIGRATIONS[-1], (db.SCHEMA_VERSION, db.V_PROVEN_CLOSES))
        self.assertEqual(pensieve.get_task(conn, self.go["build"])["status"], "awaiting_close")
        self.assertEqual(pensieve.task_spec(conn, self.go["parent"])["intent_sha256"], "c" * 64)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_closures").fetchone()[0], 0)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        with db.transaction(conn):
            for statement in db.pending_statements(conn, db.V_PROVEN_CLOSES):
                conn.execute(statement)
        self.assertEqual(conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall(), schema)
        pensieve.close_proven(conn, self.go["build"], proof(), "closed", "close:proven:x", self.go["parent"])
        self.assertEqual(pensieve.get_task(conn, self.go["parent"])["close_reason"], "complete")

    def test_complete_needs_a_token_a_complete_parent_or_a_closure(self):
        conn = self.fresh()
        other = pensieve.start_task(conn, pensieve.create_task(conn, "beta", "plain")["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "complete needs a consumed close token or a parent that"
                                                            " closed complete, or a proven close"):
            self.close_raw(conn, other["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "or a proven close"):
            self.close_raw(conn, self.go["build"])
        token = owlery.mint(conn, other["id"], "cli")["token"]
        pensieve.close_task(conn, other["id"], "complete", token)
        insert_closure(conn, self.go["build"])
        self.close_raw(conn, self.go["build"])
        self.assertEqual(pensieve.get_task(conn, self.go["build"])["close_reason"], "complete")

    def test_proven_closure_needs_a_round_pass_from_the_other_family_on_an_awaiting_task(self):
        conn = self.fresh()
        refused = "a proven close needs a task awaiting close whose commit a review round passed from the other family"
        active = pensieve.start_task(conn, pensieve.create_task(conn, "beta", "active")["id"])
        pensieve.record_commit(conn, active["id"], "acme/other", SHA)
        with self.assertRaisesRegex(sqlite3.IntegrityError, refused):
            insert_closure(conn, active["id"], repo="acme/other")
        # A PASS written with castle review record, which no round holds, on a task awaiting close.
        recorded = pensieve.start_task(conn, pensieve.create_task(conn, "beta", "recorded")["id"])
        pensieve.record_commit(conn, recorded["id"], "acme/recorded", SHA)
        owlery.open_request(conn, "beta", "alpha", "review", parent_task_id=recorded["id"])
        owlery.record_review(conn, "acme/recorded", SHA, recorded["id"], "alpha", "PASS")
        pensieve.mark_awaiting_close(conn, recorded["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, refused):
            insert_closure(conn, recorded["id"], repo="acme/recorded")
        # A commit the task never recorded.
        with self.assertRaisesRegex(sqlite3.IntegrityError, refused):
            insert_closure(conn, self.go["build"], pass_sha=MERGE_SHA)
        # A later review of the same commit that is not a PASS.
        owlery.record_review(conn, REPO, SHA, self.go["build"], "alpha", "CHANGES", now=NOW + 1)
        with self.assertRaisesRegex(sqlite3.IntegrityError, refused):
            insert_closure(conn, self.go["build"])

    def test_judge_must_be_of_the_other_family(self):
        conn = self.fresh()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "an after-merge judge is from the other family"):
            insert_closure(conn, self.go["build"], judge_desk="delta")
        insert_closure(conn, self.go["build"], judge_desk="alpha")
        self.close_raw(conn, self.go["build"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "an after-merge judge is from the other family"):
            insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"], judge_desk="delta")
        for bad in ({"written_checks": 0}, {"written_checks": 1, "judge_desk": None}, {"ci": "none"},
                    {"landed": "ancestry"}, {"merge_sha": "X" * 40}, {"evidence_path": "/etc/close.md"}):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"], **bad)

    def test_parent_closure_follows_its_proven_child(self):
        conn = self.fresh()
        follows = "a parent closes only after its proven child, on the same proof, and only when a go registered it"
        with self.assertRaisesRegex(sqlite3.IntegrityError, follows):
            insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"])
        insert_closure(conn, self.go["build"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, follows):  # the child is not closed yet
            insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"])
        self.close_raw(conn, self.go["build"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, follows):
            insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"], merge_sha="3" * 40)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_closure(conn, self.go["parent"], kind="parent", via=None)
        insert_closure(conn, self.go["parent"], kind="parent", via=self.go["build"])
        self.close_raw(conn, self.go["parent"])
        self.assertEqual(pensieve.get_task(conn, self.go["parent"])["close_reason"], "complete")

    def test_parent_closure_needs_a_go_registered_parent(self):
        conn = self.fresh()
        by_hand = pensieve.create_task(conn, "gamma", "registered by hand",
                                       intent_path=intent_file("tk_00000000000000b1"), task_id="tk_00000000000000b1")
        build = owlery.open_request(conn, "gamma", "beta", "build", parent_task_id=by_hand["id"])["task"]["id"]
        pensieve.start_task(conn, build)
        pensieve.record_commit(conn, build, "acme/hand", SHA)
        opened = capacity.open_review_round(conn, build, "alpha", SHA, "review")
        capacity.record_round_verdict(conn, opened["request"]["id"], "acme/hand", "PASS")
        pensieve.close_task(conn, opened["task"]["id"], "superseded")
        pensieve.mark_awaiting_close(conn, build)
        with self.assertRaisesRegex(ConflictError, "not registered by a go"):
            pensieve.close_proven(conn, build, proof(repo="acme/hand"), "x", "close:proven:y",
                                  parent_task_id=by_hand["id"])
        self.assertEqual(pensieve.get_task(conn, build)["status"], "awaiting_close")
        insert_closure(conn, build, repo="acme/hand")
        self.close_raw(conn, build)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only when a go registered it"):
            insert_closure(conn, by_hand["id"], kind="parent", via=build, repo="acme/hand")

    def test_closures_are_immutable_and_never_deleted(self):
        conn = self.fresh()
        pensieve.close_proven(conn, self.go["build"], proof(), "closed", "close:proven:x", self.go["parent"])
        for column, value in (("merge_sha", "3" * 40), ("kind", "parent"), ("evidence_sha256", "e" * 64)):
            with self.subTest(column=column), self.assertRaisesRegex(sqlite3.IntegrityError, "a task closure is fixed"):
                conn.execute(f"UPDATE task_closures SET {column} = ? WHERE task_id = ?", (value, self.go["build"]))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            conn.execute("DELETE FROM task_closures WHERE task_id = ?", (self.go["build"],))
        with self.assertRaises(sqlite3.IntegrityError):  # a closed task takes no closure, however it closed
            insert_closure(conn, self.go["build"])
        # The go task closed some other way after its child's proven close: no parent row lands on it.
        other = self.fresh()
        insert_closure(other, self.go["build"])
        self.close_raw(other, self.go["build"])
        other.execute("UPDATE tasks SET status = 'closed', close_reason = 'abandoned', closed_at = ? WHERE id = ?",
                      (NOW, self.go["parent"]))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "a closure is recorded on an open task"):
            insert_closure(other, self.go["parent"], kind="parent", via=self.go["build"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_closures").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
