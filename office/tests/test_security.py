from __future__ import annotations

import ast
import inspect
import io
import json
import os
import py_compile
import shutil
import sqlite3
import stat
import subprocess
import sys
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hogwarts import capacity, cli, db, facts, ids, owlery, pensieve
from hogwarts.errors import IntegrityError, ValidationError
from tests.support import DAY, NOW, REPO, SHA, TEST_TMP_ROOT, StoreCase, temp_dir

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "hogwarts"
SOURCES = sorted(PACKAGE.glob("*.py"))
TESTS = sorted((ROOT / "tests").glob("*.py"))
DEFAULT_DB_TEXT = "/Users/crisryantan/.hogwarts/state/pensieve.db"
ENV_NAMES = {
    "environ", "environb", "getenv", "getenvb", "putenv", "unsetenv", "expanduser", "expandvars",
    "home", "gettempdir", "gettempdirb", "getuser",
}
SQL_ARG = {"execute": 0, "executemany": 0, "executescript": 0, "fetch_one": 1, "fetch_all": 1}
SQL_PASS_THROUGH = {("fetch_one", "sql"), ("fetch_all", "sql"), ("migrate", "statement")}
TEMPFILE_CALLS = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile",
                  "SpooledTemporaryFile"}
WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}


def env_problems(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [part for alias in node.names for part in alias.name.split(".") + [alias.asname or ""]]
        found += [f"line {node.lineno}: {name}" for name in names if name in ENV_NAMES]
    return found


def _module_constants(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _local_names(function: ast.AST) -> set[str]:
    names = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
    names.update(node.id for node in ast.walk(function)
                 if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    return names


def _sql_is_safe(node: ast.AST, constants: set[str], local: set[str], allowed: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id in allowed or (node.id in constants and node.id not in local)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return all(_sql_is_safe(side, constants, local, allowed) for side in (node.left, node.right))
    if isinstance(node, ast.IfExp):
        return all(_sql_is_safe(side, constants, local, allowed) for side in (node.body, node.orelse))
    return False


def _sql_calls(tree: ast.Module):
    scopes = [("<module>", tree)] + [(node.name, node) for node in ast.walk(tree)
                                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    seen = set()
    for name, scope in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen or not isinstance(node, ast.Call):
                continue
            seen.add(id(node))
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if attr in SQL_ARG:
                yield name, scope, attr, node


def sql_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    constants = _module_constants(tree)
    found = []
    for name, scope, attr, call in _sql_calls(tree):
        index = SQL_ARG[attr]
        allowed = {arg for function, arg in SQL_PASS_THROUGH if function == name}
        local = _local_names(scope) if scope is not tree else set()
        if attr == "executescript" or len(call.args) <= index:
            found.append(f"line {call.lineno}: {attr} without a fixed SQL argument")
        elif not _sql_is_safe(call.args[index], constants, local, allowed):
            found.append(f"line {call.lineno}: {attr} SQL is built from a runtime value")
    return found


def public_functions(module) -> list:
    return [(name, function) for name, function in inspect.getmembers(module, inspect.isfunction)
            if not name.startswith("_") and function.__module__ == module.__name__]


def remove_entry(path: Path) -> None:
    if os.path.lexists(path):
        (os.rmdir if stat.S_ISDIR(os.lstat(path).st_mode) else os.remove)(path)


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


class EnvironmentTests(unittest.TestCase):
    def test_no_environment_variables_are_read(self):
        for path in SOURCES + TESTS:
            with self.subTest(path=path.name):
                self.assertEqual(env_problems(path.read_text()), [])

    def test_environment_checker_catches_indirect_reads(self):
        for snippet in ('x = os.getenvb(b"HOME")', 'p = Path.home() / ".hogwarts"',
                        'p = os.path.expanduser("~/.hogwarts")', "from os import environ",
                        "import os.environ as e", "d = tempfile.gettempdir()", "u = getpass.getuser()"):
            with self.subTest(snippet=snippet):
                self.assertNotEqual(env_problems(snippet), [])

    def test_default_db_is_a_constant_path(self):
        self.assertEqual(db.DEFAULT_DB, Path(DEFAULT_DB_TEXT))
        tree = ast.parse((PACKAGE / "db.py").read_text())
        assigned = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and [getattr(target, "id", None) for target in node.targets] == ["DEFAULT_DB"]]
        self.assertEqual(len(assigned), 1)
        value = assigned[0]
        self.assertIsInstance(value, ast.Call)
        self.assertEqual((getattr(value.func, "id", None), value.keywords), ("Path", []))
        self.assertEqual(len(value.args), 1)
        self.assertIsInstance(value.args[0], ast.Constant)
        self.assertEqual(value.args[0].value, DEFAULT_DB_TEXT)

    def test_tests_use_a_constant_temp_root(self):
        self.assertEqual(temp_dir(self).parent, TEST_TMP_ROOT)
        for path in TESTS:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in TEMPFILE_CALLS:
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertIn("dir", [keyword.arg for keyword in node.keywords])

    def test_cli_has_no_db_flag(self):
        parser = cli.build_parser()
        options = set()
        pending = [parser]
        while pending:
            current = pending.pop()
            for action in current._actions:
                options.update(action.option_strings)
                if action.choices and isinstance(action.choices, dict):
                    pending.extend(action.choices.values())
        self.assertNotIn("--db", options)
        self.assertFalse(any("db" in option for option in options), options)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--db", "/tmp/elsewhere.db", "desk", "list"], db_path=Path("/nonexistent/state/x.db"))
        self.assertEqual(code, 2)


class ConnectSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.state = self.tmp / "state"
        self.db_path = self.state / "pensieve.db"

    def connect(self):
        conn = db.connect(self.db_path)
        conn.close()

    def test_refuses_a_symlinked_db_path(self):
        os.mkdir(self.state, 0o700)
        target = self.tmp / "elsewhere.db"
        target.touch(mode=0o600)
        os.symlink(target, self.db_path)
        with self.assertRaises(IntegrityError):
            self.connect()

    def test_refuses_a_dangling_symlink_db_path(self):
        os.mkdir(self.state, 0o700)
        os.symlink(self.tmp / "missing.db", self.db_path)
        with self.assertRaises(IntegrityError):
            self.connect()
        self.assertFalse((self.tmp / "missing.db").exists())

    def test_refuses_a_symlinked_parent_directory(self):
        real = self.tmp / "real"
        os.mkdir(real, 0o700)
        os.symlink(real, self.state)
        with self.assertRaises(IntegrityError):
            self.connect()
        self.assertEqual(list(real.iterdir()), [])

    def test_refuses_a_group_writable_parent(self):
        os.mkdir(self.state, 0o700)
        os.chmod(self.state, 0o770)
        with self.assertRaises(IntegrityError):
            self.connect()

    def test_refuses_a_world_writable_parent(self):
        os.mkdir(self.state, 0o700)
        os.chmod(self.state, 0o703)
        with self.assertRaises(IntegrityError):
            self.connect()

    def test_refuses_a_group_or_world_writable_db_file(self):
        self.connect()
        for mode in (0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.db_path, mode)
                with self.assertRaises(IntegrityError):
                    self.connect()

    def test_refuses_a_db_not_owned_by_the_current_user(self):
        self.connect()
        real_lstat = os.lstat

        def foreign_db(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == self.db_path:
                values = list(result)
                values[stat.ST_UID] = result.st_uid + 1
                return os.stat_result(values)
            return result

        with mock.patch.object(db.os, "lstat", foreign_db):
            with self.assertRaises(IntegrityError):
                self.connect()

    def test_refuses_a_parent_not_owned_by_the_current_user(self):
        self.connect()
        with mock.patch.object(db, "_uid", return_value=os.getuid() + 1):
            with self.assertRaises(IntegrityError):
                self.connect()

    def test_refuses_a_db_path_that_is_not_a_regular_file(self):
        os.makedirs(self.db_path, 0o700)
        with self.assertRaises(IntegrityError):
            self.connect()

    def test_tightens_an_existing_readable_db_to_0600(self):
        self.connect()
        os.chmod(self.db_path, 0o644)
        self.connect()
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)

    def test_creates_the_db_with_0600_and_parent_with_0700(self):
        self.connect()
        self.assertEqual(stat.S_IMODE(os.lstat(self.db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.state).st_mode), 0o700)

    def plant_sidecar(self, suffix: str, make) -> Path:
        self.connect()
        sidecar = Path(str(self.db_path) + suffix)
        if os.path.lexists(sidecar):
            os.remove(sidecar)
        make(sidecar)
        self.addCleanup(remove_entry, sidecar)
        return sidecar

    def test_refuses_a_symlinked_or_irregular_wal_or_shm_sidecar(self):
        target = self.tmp / "elsewhere"
        target.write_bytes(b"")
        makers = {
            "symlink": lambda path: os.symlink(target, path),
            "dangling symlink": lambda path: os.symlink(self.tmp / "missing", path),
            "fifo": lambda path: os.mkfifo(path, 0o600),
            "directory": lambda path: os.mkdir(path, 0o700),
        }
        for suffix in db.SIDECARS:
            for kind, make in makers.items():
                with self.subTest(suffix=suffix, kind=kind):
                    sidecar = self.plant_sidecar(suffix, make)
                    with self.assertRaisesRegex(IntegrityError, f"database {suffix[1:]} file is"):
                        self.connect()
                    remove_entry(sidecar)
        self.assertEqual(target.read_bytes(), b"")
        self.assertFalse(os.path.lexists(self.tmp / "missing"))
        self.connect()

    def test_a_sidecar_planted_after_the_checks_still_raises_integrity_error(self):
        target = self.tmp / "elsewhere"
        target.write_bytes(b"")
        for suffix in db.SIDECARS:
            with self.subTest(suffix=suffix):
                sidecar = self.plant_sidecar(suffix, lambda path: os.symlink(target, path))
                with mock.patch.object(db, "tighten_sidecars"):
                    with self.assertRaisesRegex(IntegrityError, "is a symlink") as caught:
                        self.connect()
                self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
                remove_entry(sidecar)
        self.assertEqual(target.read_bytes(), b"")

    def test_other_open_errors_are_not_relabelled(self):
        self.connect()
        failure = sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(db, "migrate", side_effect=failure):
            with self.assertRaises(sqlite3.OperationalError):
                self.connect()


class SqlTests(unittest.TestCase):
    def test_sql_is_never_built_from_runtime_values(self):
        for path in SOURCES:
            with self.subTest(path=path.name):
                self.assertEqual(sql_problems(path.read_text()), [])

    def test_sql_checker_covers_the_fetch_helpers(self):
        self.assertGreater(sum(1 for path in SOURCES for _, _, attr, _ in _sql_calls(ast.parse(path.read_text()))
                               if attr.startswith("fetch_")), 30)

    def test_sql_checker_rejects_injection_shapes(self):
        snippets = (
            "def get(conn, task_id):\n    return db.fetch_one(conn, f\"SELECT * FROM tasks WHERE id = '{task_id}'\")",
            "def get(conn, task_id):\n    conn.execute(\"SELECT * FROM tasks WHERE id = '\" + task_id + \"'\")",
            "def get(conn, where):\n    return db.fetch_all(conn, \"SELECT * FROM tasks WHERE \" + where)",
            "_SELECT = 'SELECT 1'\ndef get(conn, _SELECT):\n    conn.execute(_SELECT)",
            "def get(conn, table):\n    conn.execute(\"SELECT * FROM {}\".format(table))",
            "def get(conn):\n    conn.executescript('DELETE FROM tasks')",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet):
                self.assertNotEqual(sql_problems(snippet), [])
        self.assertEqual(sql_problems("_SELECT = 'SELECT 1'\ndef get(conn):\n    conn.execute(_SELECT + ' LIMIT 1')"), [])


class InputSecurityTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    HOSTILE = ["tk_' OR 1=1 --", "x; DROP TABLE tasks", "", "TK_0123456789ABCDEF?", "alpha\n", "../../etc"]
    IDENTIFIERS = {
        "name", "desk", "task_id", "request_id", "owl_id", "sender", "recipient", "requester", "repo", "sha",
        "session_id", "scope", "reviewer_desk", "parent_task_id", "in_reply_to", "closed_by", "run_id",
        "idempotency_key", "dedupe_key", "subject_key",
    }
    DEFAULTS = {
        "name": "gamma", "family": "claude", "role": None, "model": "model-x", "desk": "alpha", "title": "title",
        "intent_path": None, "parent_task_id": None, "request_id": "rq_0000000000000000", "session_id": "session-0001",
        "worktree": None, "task_id": "tk_0000000000000000", "kind": "fyi", "verdict": "routine", "summary": "summary",
        "dedupe_key": None, "text": "text", "seq": None, "tags": (), "scope": "fleet", "tier": "pinned",
        "source": "ryan", "expires_at": None, "run_id": "run-1", "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cost_usd": 0, "duration_ms": 0, "ts": NOW, "now": NOW, "fact_ids": [1],
        "fact_id": 1, "event_id": 1, "reason": "abandoned", "token": None, "repo": REPO, "sha": SHA,
        "max_chars": 1500, "query": "words", "limit": 10, "include_archived": False, "include_closed": False,
        "status": None, "include_acked": False, "phase": None, "open_only": False, "to_phase": "claimed", "detail": None,
        "owl_id": "owl_0000000000000000", "sender": "alpha", "recipient": "beta", "requester": "alpha",
        "subject": "subject", "body": None, "body_path": None, "in_reply_to": None, "idempotency_key": None,
        "reviewer_desk": "beta", "review_path": None, "minted_by": "cli", "ttl_seconds": 600, "body_days": 30,
        "extract_days": 90, "escalate": False, "project": "project", "started_at": None, "ended_at": None,
        "first_turn_tokens": None, "total_input_tokens": None, "since": 0, "closed_by": "tk_0000000000000000",
        "subject_key": "web-app.comments", "valid_from": None, "lookup": None, "include_history": False, "t": NOW,
        "limit_per_fact": 3, "ops": [{"op": "archive", "fact_id": 1}],
        "amount": 1, "run_cap": 3, "spend_cap": None, "reset_offset": 0, "cap": "runs", "cap_source": "fleet",
        "max_rounds": 3,
    }
    OVERRIDES = {
        ("record_review", "verdict"): "CHANGES", ("record_round_verdict", "verdict"): "CHANGES",
        ("defer", "reason"): "conflict", ("decline", "reason"): "safety",
        ("consume", "token"): "x" * 43, ("add_extract", "role"): "user",
        ("set_worktree", "worktree"): f"{ids.WORKTREES_ROOT}/wt",
        ("add_bump", "kind"): "runs", ("add_bump", "expires_at"): NOW + DAY,
    }

    def targets(self) -> list:
        found = [function for module in (pensieve, owlery, facts, capacity)
                 for _, function in public_functions(module)]
        found.append(owlery._cascade_task_closed)
        return [function for function in found if self.IDENTIFIERS & set(inspect.signature(function).parameters)]

    def arguments(self, function, conn) -> dict:
        names = [name for name in inspect.signature(function).parameters if name != "conn"]
        unknown = [name for name in names if name not in self.DEFAULTS]
        self.assertEqual(unknown, [], f"classify the new parameters of {function.__name__}")
        values = {name: self.OVERRIDES.get((function.__name__, name), self.DEFAULTS[name]) for name in names}
        return {"conn": conn, **values} if "conn" in inspect.signature(function).parameters else values

    def test_generated_sweep_validates_every_identifier_parameter(self):
        baseline = db.connect(self.tmp / "baseline" / "pensieve.db")
        self.addCleanup(baseline.close)
        for function in self.targets():
            with self.subTest(function=function.__name__, hostile=None):
                try:
                    function(**self.arguments(function, baseline))
                except ValidationError as exc:
                    self.fail(f"valid defaults for {function.__name__} were rejected: {exc}")
                except Exception:
                    pass
        before = self.conn.total_changes
        for function in self.targets():
            for parameter in sorted(self.IDENTIFIERS & set(inspect.signature(function).parameters)):
                for bad in self.HOSTILE:
                    with self.subTest(function=function.__name__, parameter=parameter, value=bad):
                        with self.assertRaises(ValidationError):
                            function(**{**self.arguments(function, self.conn), parameter: bad})
        self.assertEqual(self.conn.total_changes, before)

    def test_public_functions_validate_every_id_and_name(self):
        hostile = self.HOSTILE
        calls = [
            lambda bad: pensieve.add_desk(self.conn, bad, "claude"),
            lambda bad: pensieve.get_desk(self.conn, bad),
            lambda bad: pensieve.create_task(self.conn, bad, "title"),
            lambda bad: pensieve.create_task(self.conn, "alpha", "title", parent_task_id=bad),
            lambda bad: pensieve.create_task(self.conn, "alpha", "title", request_id=bad),
            lambda bad: pensieve.create_task(self.conn, "alpha", "title", session_id=bad),
            lambda bad: pensieve.create_task(self.conn, "alpha", "title", task_id=bad),
            lambda bad: pensieve.start_task(self.conn, bad),
            lambda bad: pensieve.mark_awaiting_close(self.conn, bad),
            lambda bad: pensieve.close_task(self.conn, bad, "abandoned"),
            lambda bad: pensieve.get_task(self.conn, bad),
            lambda bad: pensieve.list_tasks(self.conn, desk=bad),
            lambda bad: pensieve.add_event(self.conn, bad, "kind", "routine", "summary"),
            lambda bad: pensieve.add_event(self.conn, "alpha", "kind", "routine", "summary", task_id=bad),
            lambda bad: pensieve.record_session(self.conn, bad, "project"),
            lambda bad: pensieve.add_extract(self.conn, bad, "user", "text"),
            lambda bad: pensieve.add_keypoint(self.conn, "text", session_id=bad),
            lambda bad: pensieve.add_fact(self.conn, bad, "text", "pinned", "ryan"),
            lambda bad: pensieve.context_facts(self.conn, bad),
            lambda bad: pensieve.add_metric(self.conn, bad, "run", "model", 0, 0, 0, 0, 0),
            lambda bad: owlery.send(self.conn, bad, "beta", "fyi", "s"),
            lambda bad: owlery.send(self.conn, "alpha", bad, "fyi", "s"),
            lambda bad: owlery.send(self.conn, "alpha", "beta", "fyi", "s", task_id=bad),
            lambda bad: owlery.send(self.conn, "alpha", "beta", "fyi", "s", request_id=bad),
            lambda bad: owlery.send(self.conn, "alpha", "beta", "fyi", "s", in_reply_to=bad),
            lambda bad: owlery.inbox(self.conn, bad),
            lambda bad: owlery.read(self.conn, bad, "beta"),
            lambda bad: owlery.read(self.conn, "owl_0000000000000000", bad),
            lambda bad: owlery.ack(self.conn, bad, "beta"),
            lambda bad: owlery.mark_delivered(self.conn, bad),
            lambda bad: owlery.open_request(self.conn, bad, "beta", "t"),
            lambda bad: owlery.open_request(self.conn, "alpha", "beta", "t", parent_task_id=bad),
            lambda bad: owlery.advance(self.conn, bad, "claimed"),
            lambda bad: owlery.defer(self.conn, bad, "conflict"),
            lambda bad: owlery.decline(self.conn, bad, "safety"),
            lambda bad: owlery.get_request(self.conn, bad),
            lambda bad: owlery.list_requests(self.conn, desk=bad),
            lambda bad: owlery.record_review(self.conn, bad, SHA, "tk_0000000000000000", "beta", "PASS"),
            lambda bad: owlery.record_review(self.conn, REPO, bad, "tk_0000000000000000", "beta", "PASS"),
            lambda bad: owlery.record_review(self.conn, REPO, SHA, bad, "beta", "PASS"),
            lambda bad: owlery.record_review(self.conn, REPO, SHA, "tk_0000000000000000", bad, "PASS"),
            lambda bad: owlery.has_pass(self.conn, bad, SHA),
            lambda bad: owlery.mint(self.conn, bad, "cli"),
            lambda bad: owlery.consume(self.conn, bad, "x" * 43),
        ]
        before = self.conn.total_changes
        for index, call in enumerate(calls):
            for bad in hostile:
                with self.subTest(call=index, value=bad):
                    with self.assertRaises(ValidationError):
                        call(bad)
        self.assertEqual(self.conn.total_changes, before)

    def test_injection_text_is_stored_literally(self):
        title = "x'); DROP TABLE tasks; --"
        task = self.task(title=title)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["title"], title)
        self.assertEqual(len(pensieve.list_tasks(self.conn)), 1)

    def test_text_limits(self):
        pensieve.record_session(self.conn, "session-0001", "project")
        cases = [
            ("title 200", lambda text: self.task(title=text), 200),
            ("subject 200", lambda text: owlery.send(self.conn, "alpha", "beta", "fyi", text), 200),
            ("summary 500", lambda text: pensieve.add_event(self.conn, "alpha", "k", "routine", text), 500),
            ("body 16000", lambda text: owlery.send(self.conn, "alpha", "beta", "fyi", "s", body=text), 16000),
            ("extract 4000", lambda text: pensieve.add_extract(self.conn, "session-0001", "user", text), 4000),
            ("fact 300", lambda text: pensieve.add_fact(self.conn, "fleet", text, "pinned", "ryan"), 300),
        ]
        for name, call, limit in cases:
            with self.subTest(name=name):
                call("a" * limit)
                with self.assertRaises(ValidationError):
                    call("b" * (limit + 1))

    def test_nul_bytes_are_rejected(self):
        pensieve.record_session(self.conn, "session-0001", "project")
        calls = [
            lambda: self.task(title="a\x00b"),
            lambda: owlery.send(self.conn, "alpha", "beta", "fyi", "s", body="a\x00b"),
            lambda: owlery.send(self.conn, "alpha", "beta", "fyi", "a\x00b"),
            lambda: pensieve.add_event(self.conn, "alpha", "k", "routine", "a\x00b"),
            lambda: pensieve.add_extract(self.conn, "session-0001", "user", "a\x00b"),
            lambda: pensieve.add_fact(self.conn, "fleet", "a\x00b", "pinned", "ryan"),
        ]
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                with self.assertRaises(ValidationError):
                    call()

    def test_control_characters_are_stripped_before_storage(self):
        sent = owlery.send(self.conn, "alpha", "beta", "fyi", "s\x07ubject", body="red\x1b[31m\tok\nnext\x08")
        row = self.conn.execute("SELECT subject, body FROM owls WHERE id = ?", (sent["id"],)).fetchone()
        self.assertEqual((row["subject"], row["body"]), ("subject", "red[31m\tok\nnext"))


class FtsSecurityTests(StoreCase):
    def setUp(self):
        super().setUp()
        pensieve.record_session(self.conn, "session-0001", "project")
        pensieve.add_extract(self.conn, "session-0001", "user", "alpha beta")
        pensieve.add_extract(self.conn, "session-0001", "user", "gamma delta")

    def test_every_token_becomes_a_quoted_phrase(self):
        self.assertEqual(pensieve.fts_query('a"b NEAR(x) col:val OR'), '"a""b" "NEAR(x)" "col:val" "OR"')

    def test_fts_syntax_in_user_text_cannot_change_the_query(self):
        for query in ("alpha OR gamma", "text:gamma", "NEAR(alpha gamma)", "alpha NOT beta", "{text}: gamma"):
            with self.subTest(query=query):
                self.assertEqual(pensieve.find(self.conn, query), [])
        self.assertEqual(len(pensieve.find(self.conn, "gamma*")), 1)
        self.assertEqual(len(pensieve.find(self.conn, "alpha beta")), 1)

    def test_queries_without_words_are_rejected_not_passed_through(self):
        for query in ('"', "*", "()", "^ -", ""):
            with self.subTest(query=query):
                with self.assertRaises(ValidationError):
                    pensieve.find(self.conn, query)


class ScrubTests(StoreCase):
    def test_scrubs_emails(self):
        self.assertEqual(pensieve.scrub("mail jane.doe+x@example.com now"), "mail [email] now")

    def test_scrubs_ipv4(self):
        self.assertEqual(pensieve.scrub("from 10.0.12.255, then 192.168.1.1."), "from [ipv4], then [ipv4].")
        self.assertEqual(pensieve.scrub("version 1.2.3 and 999.1.1.1"), "version 1.2.3 and 999.1.1.1")

    def test_scrubs_ipv6(self):
        self.assertEqual(pensieve.scrub("hosts fe80::1ff:fe23:4567:890a and 2001:db8::1"), "hosts [ipv6] and [ipv6]")
        self.assertEqual(pensieve.scrub("std::vector x[::2] at 12:30:45"), "std::vector x[::2] at 12:30:45")

    def test_scrubs_long_hex(self):
        self.assertEqual(pensieve.scrub("sha " + "ab" * 16 + " short " + "c" * 31), "sha [hex] short " + "c" * 31)

    def test_scrubs_jwts(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        self.assertEqual(pensieve.scrub(f"auth {jwt} end"), "auth [jwt] end")

    def test_scrubs_aws_access_keys(self):
        self.assertEqual(pensieve.scrub("key AKIAIOSFODNN7EXAMPLE used"), "key [aws_key] used")

    def test_scrubs_private_key_blocks(self):
        pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\nAAAAMwAAAAtzc2gtZWQy\n"
               "-----END OPENSSH PRIVATE KEY-----")
        self.assertEqual(pensieve.scrub(f"key:\n{pem}\nafter"), "key:\n[private_key]\nafter")
        self.assertEqual(pensieve.scrub("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA"), "[private_key]")

    def test_scrubs_url_credentials_whole(self):
        self.assertEqual(pensieve.scrub("postgres://admin:S3cr3t!pw@db.internal:5432/app"),
                         "postgres://[credentials]@db.internal:5432/app")
        self.assertEqual(pensieve.scrub("see https://example.com/a?b=1"), "see https://example.com/a?b=1")

    def test_scrubs_known_token_prefixes(self):
        for token in ("ghp_" + "Z" * 36, "gho_" + "a1" * 12, "github_pat_" + "A1_" * 10, "xoxb-1234567890-abcdefghijkl",
                      "sk-ant-api03-" + "Q" * 40, "sk-" + "a" * 30):
            with self.subTest(token=token[:12]):
                self.assertEqual(pensieve.scrub(f"use {token} now"), "use [token] now")
        self.assertEqual(pensieve.scrub("task-sk-something risk-assessment"), "task-sk-something risk-assessment")

    def test_scrubs_authorization_headers(self):
        for scheme in ("Basic dXNlcjpodW50ZXIy", "Bearer abcdefghijklmnop"):
            with self.subTest(scheme=scheme[:5]):
                self.assertEqual(pensieve.scrub(f"Authorization: {scheme}"),
                                 f"Authorization: {scheme.split()[0]} [token]")

    def test_scrubs_pwd_values(self):
        self.assertEqual(pensieve.scrub("pwd=hunter2 next"), "pwd=[secret] next")

    def test_scrubs_key_value_secrets(self):
        cases = {
            "password=hunter2": "password=[secret]",
            "DB_PASSWORD: s3cr3t!": "DB_PASSWORD: [secret]",
            '{"api_key": "abc123"}': '{"api_key": [secret]}',
            "token=xyz&next=1": "token=[secret]&next=1",
            "client_secret='q w e'": "client_secret=[secret]",
            "Authorization: Bearer abcdefghijklmnop": "Authorization: Bearer [token]",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(pensieve.scrub(raw), expected)

    def test_leaves_ordinary_text_alone(self):
        text = "max_tokens=5 and token_count: 9 on desk ryan-claude, task tk_0123456789abcdef"
        self.assertEqual(pensieve.scrub(text), text)

    def test_extracts_and_keypoints_are_scrubbed_before_storage(self):
        pensieve.record_session(self.conn, "session-0001", "project")
        secret = "email a@b.io ip 10.1.2.3 password=hunter2"
        extract = pensieve.add_extract(self.conn, "session-0001", "user", secret)
        keypoint = pensieve.add_keypoint(self.conn, secret)
        stored = [
            self.conn.execute("SELECT text FROM extracts WHERE id = ?", (extract["id"],)).fetchone()[0],
            self.conn.execute("SELECT text FROM keypoints WHERE id = ?", (keypoint["id"],)).fetchone()[0],
        ]
        for text in stored:
            self.assertEqual(text, "email [email] ip [ipv4] password=[secret]")
        self.assertEqual(pensieve.find(self.conn, "hunter2"), [])


class TransactionCoverageTests(unittest.TestCase):
    def run_every_write(self, conn) -> None:
        for name, family in (("alpha", "claude"), ("beta", "codex"), ("ryan", "human")):
            pensieve.add_desk(conn, name, family, now=NOW)
        task = pensieve.start_task(conn, pensieve.create_task(conn, "alpha", "build", now=NOW)["id"], now=NOW)["id"]
        pensieve.record_commit(conn, task, REPO, SHA, now=NOW)
        capacity.open_review_round(conn, task, "beta", SHA, "review one", now=NOW)
        two = capacity.open_review_round(conn, task, "beta", SHA, "review two", now=NOW)
        capacity.record_round_verdict(conn, two["request"]["id"], REPO, "CHANGES", now=NOW)
        capacity.allow_round(conn, task, now=NOW)
        capacity.add_bump(conn, "alpha", "runs", 5, NOW + DAY, now=NOW)
        capacity.record_cap_hit(conn, "alpha", "plan", "claude_plan", run_id="run-1", now=NOW)
        capacity.record_launch(conn, "alpha", "run-launched", "model-x", now=NOW)
        capacity.record_launch_usage(conn, "run-launched", 1, 1, 0, 0.25, 10, now=NOW)
        pensieve.set_worktree(conn, pensieve.create_task(conn, "beta", "wt", now=NOW)["id"], f"{ids.WORKTREES_ROOT}/wt")
        pensieve.mark_awaiting_close(conn, task, now=NOW)
        owlery.open_request(conn, "alpha", "beta", "child", parent_task_id=task, now=NOW)
        owlery.record_review(conn, REPO, SHA, task, "ryan", "PASS", now=NOW)
        pensieve.close_task(conn, task, "complete", owlery.mint(conn, task, "cli", now=NOW)["token"], now=NOW)
        pensieve.ack(conn, pensieve.add_event(conn, "alpha", "note", "headmaster", "s", now=NOW)["id"], now=NOW)
        pensieve.record_session(conn, "session-0001", "proj", started_at=NOW)
        pensieve.add_extract(conn, "session-0001", "user", "words", now=NOW)
        pensieve.add_keypoint(conn, "point", session_id="session-0001", now=NOW)
        aging = pensieve.add_fact(conn, "fleet", "aging fact", "aging", "ryan", now=NOW - 40 * DAY)
        pensieve.touch(conn, aging["id"], now=NOW - 35 * DAY)
        pensieve.archive_stale(conn, now=NOW)
        pensieve.archive(conn, [pensieve.add_fact(conn, "alpha", "pinned", "pinned", "ryan", now=NOW)["id"]], now=NOW)
        older = facts.supersede(conn, "fleet", "ci.main", "main needs two approvals", "ryan", now=NOW)["fact_id"]
        newer = facts.supersede(conn, "fleet", "ci.main", "main needs one approval", "ryan", now=NOW + 1)["fact_id"]
        facts.withdraw(conn, newer, desk="alpha", now=NOW + 2)
        facts.set_key(conn, pensieve.add_fact(conn, "alpha", "keyless", "aging", "ryan", now=NOW)["id"], "alpha.note",
                      now=NOW)
        facts.expire(conn, now=NOW + 100 * DAY)
        facts.apply_ops(conn, [{"op": "archive", "fact_id": older}], now=NOW + 3)
        pensieve.add_metric(conn, "alpha", "run-1", "model-x", 1, 1, 1, 0.5, 1, ts=NOW)
        owl = owlery.send(conn, "alpha", "beta", "fyi", "note", body="b", now=NOW)["id"]
        owlery.mark_delivered(conn, owl, now=NOW)
        owlery.read(conn, owl, "beta", now=NOW)
        owlery.ack(conn, owl, "beta", now=NOW)
        deferred = owlery.open_request(conn, "alpha", "beta", "defer me", now=NOW)["request"]["id"]
        owlery.advance(conn, deferred, "claimed", now=NOW)
        owlery.defer(conn, deferred, "conflict", now=NOW)
        owlery.decline(conn, owlery.open_request(conn, "alpha", "beta", "decline me", now=NOW)["request"]["id"],
                       "safety", now=NOW)
        owlery.purge(conn, now=NOW + 100 * DAY)
        owlery.audit(conn, now=NOW + 100 * DAY, escalate=True)

    def test_every_write_runs_inside_a_transaction(self):
        outside, called = [], set()
        real_open = db._open

        def traced_open(path):
            conn = real_open(path)

            def trace(statement):
                frames = [frame for frame in traceback.extract_stack() if frame.filename.startswith(str(PACKAGE))]
                called.update((Path(frame.filename).stem, frame.name) for frame in frames)
                verb = (statement.split(None, 1) or [""])[0].upper()
                if verb in WRITE_VERBS and not conn.in_transaction:
                    where = frames[-1] if frames else None
                    outside.append(f"{Path(where.filename).name}:{where.lineno} {where.name}" if where else statement)

            conn.set_trace_callback(trace)
            return conn

        with mock.patch.object(db, "_open", traced_open):
            conn = db.connect(temp_dir(self) / "state" / "pensieve.db")
            self.addCleanup(conn.close)
            self.run_every_write(conn)
        self.assertEqual(outside, [])
        writers = {(module.__name__.split(".")[-1], name) for module in (pensieve, owlery, facts, capacity)
                   for name, function in public_functions(module) if "db.transaction" in inspect.getsource(function)}
        self.assertEqual(writers - called, set())

    def test_tracer_flags_a_write_outside_a_transaction(self):
        conn = db.connect(temp_dir(self) / "state" / "pensieve.db")
        self.addCleanup(conn.close)
        seen = []
        conn.set_trace_callback(lambda statement: seen.append((statement.split()[0], conn.in_transaction)))
        conn.execute("INSERT INTO desks(name, family, created_at) VALUES ('alpha', 'claude', 1)")
        self.assertIn(("INSERT", False), seen)


class BytecodeTests(unittest.TestCase):
    def test_wrapper_flags_never_load_planted_bytecode(self):
        copy = temp_dir(self)
        shutil.copytree(PACKAGE, copy / "hogwarts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        source = copy / "hogwarts" / "__init__.py"
        planted = copy / "hogwarts" / "__pycache__" / f"__init__.{sys.implementation.cache_tag}.pyc"
        reviewed = source.read_text()
        source.write_text('__version__ = "PLANTED"\n')
        py_compile.compile(str(source), cfile=str(planted), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        source.write_text(reviewed)
        script = "import sys; sys.path.insert(0, sys.argv[1]); import hogwarts; print(hogwarts.__version__)"

        def version(*flags):
            result = subprocess.run(["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", *flags, "-c", script,
                                     str(copy)], capture_output=True, text=True, cwd="/", timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        self.assertEqual(version(), "PLANTED")
        self.assertEqual(version("-X", "pycache_prefix=/var/empty"), "1.0.0")


class OutputSecurityTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        return code, out.getvalue() + err.getvalue()

    def test_cli_output_is_ascii_json_with_escapes(self):
        sent = owlery.send(self.conn, "alpha", "beta", "fyi", "café ✓", body="line\ttab\nnext ‮")
        code, raw = self.cli("owl", "read", sent["id"], "--as", "beta")
        self.assertEqual(code, 0)
        self.assertTrue(all(ch == "\n" or 32 <= ord(ch) < 127 for ch in raw))
        self.assertIn("\\u00e9", raw)
        self.assertIn("\\t", raw)
        self.assertIn("\\u202e", raw)
        self.assertEqual(json.loads(raw)["data"]["body"], "line\ttab\nnext ‮")

    def test_list_views_never_include_owl_bodies(self):
        owlery.open_request(self.conn, "alpha", "beta", "req", body="request body secret", now=NOW - 3 * 3600)
        sent = owlery.send(self.conn, "alpha", "beta", "fyi", "s", body="owl body secret", now=NOW - 3 * 3600)
        owlery.mark_delivered(self.conn, sent["id"], now=NOW - 3 * 3600)
        for argv in (("owl", "inbox", "beta", "--all"), ("request", "list"), ("audit",), ("task", "list")):
            with self.subTest(argv=argv):
                code, raw = self.cli(*argv)
                self.assertEqual(code, 0)
                self.assertNotIn("body secret", raw)
                self.assertNotIn("body", set(walk_keys(json.loads(raw))))


if __name__ == "__main__":
    unittest.main()
