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

from hogwarts import db, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError
from tests.support import NOW, StoreCase, temp_dir

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
        self.assertEqual((db.SCHEMA_VERSION, db.schema_version(conn)), (7, 7))
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
        self.assertEqual(db.migrate(conn), 7)
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


if __name__ == "__main__":
    unittest.main()
