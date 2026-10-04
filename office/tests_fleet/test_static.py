from __future__ import annotations

import ast
import json
import plistlib
import re
import unittest
from pathlib import Path

from hogwarts import db, ids

from fleet import config
from tests_fleet.support import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
FLEET = ROOT / "fleet"
SOURCES = sorted(FLEET.rglob("*.py"))
TESTS = sorted((ROOT / "tests_fleet").glob("*.py"))
LAUNCHD = ROOT / "launchd"
PENDING = ROOT / "pending"
ENV_NAMES = {
    "environ", "environb", "getenv", "getenvb", "putenv", "unsetenv", "expanduser", "expandvars",
    "home", "gettempdir", "gettempdirb", "getuser",
}
PROCESS_MODULES = {"subprocess", "pty", "multiprocessing", "asyncio"}
PROCESS_CALLS = {
    "system", "popen", "execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
    "posix_spawnp", "fork", "forkpty", "startfile", "create_subprocess_exec", "create_subprocess_shell",
}
EVAL_NAMES = {"eval", "exec", "compile", "__import__", "pickle", "marshal", "shelve", "importlib"}
TEMPFILE_CALLS = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile",
                  "SpooledTemporaryFile"}
FLAG_SHAPE = re.compile(r"-{1,2}[A-Za-z][A-Za-z0-9-]*")
KNOWN_FLAGS = {
    "-p", "--restricted", "--settings", "--strict-mcp-config", "--mcp-config", "--tools", "--permission-mode",
    "--model", "--append-system-prompt", "--output-format", "--max-budget-usd", "exec", "--ignore-user-config",
    "--ignore-rules",
    "-c", "--sandbox", "-C", "--add-dir", "--ephemeral", "--json", "--output-last-message", "--owl", "--dry-run",
    "--mcp-job", "--desk", "-i", "-I", "-B", "-X",
    # git, run only through gitops with the hardening flags
    "--git-dir", "--work-tree", "--verify", "--end-of-options", "--get", "--porcelain", "--no-verify",
    "--no-ext-diff", "--no-textconv", "--detach", "-b", "-m", "-F", "--stdin", "--force", "-z",
    "--no-decorate", "--oneline", "--format", "--is-ancestor", "--quiet", "--name-only",
    "-f", "-d", "-n", "--ignored",  # git clean -n/-f -d -X: ignored files only, never tracked or untracked ones
    # the review, worktree, verify and push scripts and the push gate
    "--task", "--repo-dir", "--branch", "--base", "--title", "--no-fetch", "--yes", "--mode",
    "--intent-file", "--permission-profile", "--cd", "--noprofile", "--norc", "sandbox",
    "-A", "-P", "--no-tags", "-U0",
    # Ollivander: claude --effort, the CLI version and help probes, and brew upgrade --cask codex
    "--effort", "--version", "--help", "--cask",
}
# Modules whose flag-shaped constants describe commands they read and refuse, never ones they run.
FLAG_TABLE_MODULES = {"push_gate.py"}
# Modules allowed to start processes, and the only module each may use for it.
# ollivander.py reads the CLI catalogs and help, and runs the CLI updates and their checks.
PROCESS_MODULES_ALLOWED = {"run_desk.py": {"subprocess"}, "gitops.py": {"subprocess"}, "verify.py": {"subprocess"},
                           "ollivander.py": {"subprocess"}}
PLISTS = ("owlpost", "map", "morning", "keeper", "portrait", "gringotts", "ollivander")
REAL_OFFICE = "/Users/crisryantan/.hogwarts"
REAL_CASTLE = "/Users/crisryantan/hogwarts"


def env_problems(source: str) -> list:
    found = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [part for alias in node.names for part in alias.name.split(".") + [alias.asname or ""]]
            if isinstance(node, ast.ImportFrom) and node.module:
                names += node.module.split(".")
        found += [f"line {node.lineno}: {name}" for name in names if name in ENV_NAMES]
    return found


def imported_modules(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


class EnvironmentTests(unittest.TestCase):
    def test_fleet_reads_no_environment_variables(self):
        self.assertGreater(len(SOURCES), 10)
        for path in SOURCES + TESTS:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(env_problems(path.read_text()), [])

    def test_the_checker_catches_indirect_reads(self):
        for snippet in ('x = os.getenv("HOME")', 'p = Path.home()', 'p = os.path.expanduser("~")',
                        "from os import environ", "import os.environ as e", "d = tempfile.gettempdir()",
                        "u = getpass.getuser()", "x = os.environ['PATH']"):
            with self.subTest(snippet=snippet):
                self.assertNotEqual(env_problems(snippet), [])

    def test_locations_default_to_the_real_homes(self):
        self.assertEqual(config.OFFICE_ROOT, REAL_OFFICE)
        self.assertEqual(config.CASTLE_ROOT, REAL_CASTLE)
        self.assertEqual(config.CASTLE_ROOT, ids.CASTLE_ROOT)
        self.assertEqual(config.OFFICE_ROOT, ids.OFFICE_ROOT)
        self.assertEqual(config.DB_PATH, str(db.DEFAULT_DB))
        for value in (config.TRANSCRIPTS_ROOT, config.CLAUDE_BIN, config.CODEX_BIN, config.USER_HOME_DIR):
            self.assertTrue(value.startswith("/"), value)
        self.assertEqual(config.PYTHON_WRAPPER,
                         ("/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty"))

    def test_desk_sets_match_the_registry(self):
        families = {name: family for name, family, _, _ in REGISTRY}
        for desk in config.CASTLE_DESKS + config.INTERACTIVE_DESKS:
            self.assertIn(desk, families)
        self.assertTrue(all(families[desk] == "claude" for desk in config.HEADLESS_CLAUDE))
        self.assertTrue(all(families[desk] == "codex" for desk in config.HEADLESS_CODEX))
        self.assertEqual(set(config.CLAUDE_TOOLS), set(config.HEADLESS_CLAUDE))
        self.assertEqual(set(config.MAX_BUDGET_USD), set(config.HEADLESS_CLAUDE))
        self.assertEqual(set(config.CODEX_ACCESS), set(config.HEADLESS_CODEX))
        self.assertEqual(config.CODEX_ACCESS, {"harry": "write", "moody": "read"})


class ProcessTests(unittest.TestCase):
    def test_only_run_desk_starts_processes(self):
        for path in SOURCES:
            tree = ast.parse(path.read_text())
            calls = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                     and node.attr in PROCESS_CALLS]
            modules = imported_modules(tree) & PROCESS_MODULES
            with self.subTest(path=path.name):
                if path.name in PROCESS_MODULES_ALLOWED:
                    self.assertEqual((modules, calls), (PROCESS_MODULES_ALLOWED[path.name], []))
                else:
                    self.assertEqual((sorted(modules), calls), ([], []))

    def test_no_process_module_uses_a_shell_keyword(self):
        for name in PROCESS_MODULES_ALLOWED:
            path = FLEET / name
            if not path.exists():
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        with self.subTest(path=name, line=node.lineno):
                            self.assertNotEqual(keyword.arg, "shell")

    def test_nothing_is_evaluated(self):
        for path in SOURCES:
            tree = ast.parse(path.read_text())
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= imported_modules(tree)
            with self.subTest(path=path.name):
                self.assertEqual(names & EVAL_NAMES, set())

    def test_every_flag_in_the_code_is_a_known_safe_flag(self):
        for path in SOURCES:
            if path.name in FLAG_TABLE_MODULES:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and FLAG_SHAPE.fullmatch(node.value):
                    with self.subTest(path=path.name, flag=node.value):
                        self.assertIn(node.value, KNOWN_FLAGS)

    def test_tests_use_a_constant_temp_root(self):
        for path in TESTS:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in TEMPFILE_CALLS:
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertIn("dir", [keyword.arg for keyword in node.keywords])


class LaunchdTests(unittest.TestCase):
    def load(self, name: str) -> dict:
        with open(LAUNCHD / f"com.hogwarts.{name}.plist", "rb") as handle:
            return plistlib.load(handle)

    def test_every_job_runs_the_wrapper_line_and_logs_in_the_office(self):
        for name in PLISTS:
            with self.subTest(job=name):
                job = self.load(name)
                self.assertEqual(job["Label"], f"com.hogwarts.{name}")
                args = job["ProgramArguments"]
                self.assertEqual(tuple(args[:7]), config.PYTHON_WRAPPER)
                self.assertEqual(args[7], "-c")
                self.assertIn('sys.path.insert(0, "/Users/crisryantan/.hogwarts")', args[8])
                self.assertRegex(args[8], r"from fleet\.[a-z_.]+ import main; sys\.exit\(main\(\)\)$")
                for key in ("StandardOutPath", "StandardErrorPath"):
                    self.assertTrue(job[key].startswith(f"{REAL_OFFICE}/logs/"), job[key])
                self.assertEqual(job["WorkingDirectory"], REAL_OFFICE)
                self.assertEqual(job["Umask"], 0o077)
                self.assertNotIn("EnvironmentVariables", job)
                for arg in args:
                    self.assertNotIn("dangerously", arg)

    def test_owl_post_watches_every_desk_outbox_and_sweeps(self):
        job = self.load("owlpost")
        self.assertEqual(job["WatchPaths"], [f"{REAL_CASTLE}/desks/{desk}/outbox" for desk in config.CASTLE_DESKS])
        self.assertIn("from fleet.owl_post import main", job["ProgramArguments"][8])
        self.assertTrue(job["AbandonProcessGroup"])
        self.assertEqual(job["StartInterval"], 300)

    def test_scheduled_jobs_have_a_schedule(self):
        for name in PLISTS:
            with self.subTest(job=name):
                job = self.load(name)
                self.assertTrue("StartCalendarInterval" in job or "StartInterval" in job)


class PendingTests(unittest.TestCase):
    def test_user_settings_deny_the_office_to_every_file_tool(self):
        deny = json.loads((PENDING / "a1-user-settings-deny.merge.json").read_text())["permissions"]["deny"]
        for tool in ("Read", "Edit", "Write"):
            self.assertIn(f"{tool}(~/.hogwarts/**)", deny)

    def test_pending_files_are_valid(self):
        self.assertTrue((PENDING / "README.md").exists())
        snippets = sorted(PENDING.glob("*.json"))
        self.assertGreater(len(snippets), 0)
        for path in snippets:
            with self.subTest(path=path.name):
                json.loads(path.read_text())
