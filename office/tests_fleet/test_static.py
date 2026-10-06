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
# The parallel suite runner, which starts one test process per module.
RUNNER = ROOT / "run_suites.py"
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
    # run_desk: the task's review lock that fleet build, fleet worktree or the review loop hands a build desk's run
    "--task-lock-fd",
    # Claude's stream-json output, which print mode only writes with --verbose, and fleet feed --all
    "--verbose", "--all",
    # git, run only through gitops with the hardening flags
    "--git-dir", "--work-tree", "--verify", "--end-of-options", "--get", "--porcelain", "--no-verify",
    "--no-ext-diff", "--no-textconv", "--detach", "-b", "-m", "-F", "--stdin", "--force", "-z",
    "--no-decorate", "--oneline", "--format", "--is-ancestor", "--quiet", "--name-only",
    "--is-shallow-repository", "-e",  # read-only probes: a shallow checkout, and git cat-file -e for a commit
    "--count", "--no-replace-objects",  # git rev-list --count walks HEAD's history as its commits name it
    "-f", "-d", "-n", "--ignored",  # git clean -n/-f -d -X: ignored files only, never tracked or untracked ones
    # git ls-files --others --ignored: a read-only list of a worktree's ignored files, before a removal
    "--others", "--exclude-standard", "--directory", "--no-empty-directory",
    "-v",  # git ls-files -v: which tracked files are marked assume-unchanged or skip-worktree
    # the review, worktree, verify and push scripts and the push gate
    "--task", "--repo-dir", "--branch", "--base", "--title", "--no-fetch", "--yes", "--mode",
    "--intent-file", "--permission-profile", "--cd", "--noprofile", "--norc", "sandbox",
    "-A", "-P", "--no-tags", "-U0",
    # Ollivander: claude --effort, the CLI version and help probes, and brew upgrade --cask codex
    "--effort", "--version", "--help", "--cask",
    # Gringotts' restore drill
    "--drill",
    # the review loop's draft PR, gh pr create --draft with its body on stdin (gitops.draft_pr_argv)
    "--draft", "--repo", "--head", "--body-file",
    # a PR follow-up's replies, gh api --method POST with their JSON on stdin (gitops.reply_argv, pr_comment_argv)
    "--method", "--input",
    # Dumbledore's nightly job: the export alone, with no owl and no run
    "--export-only",
    # the closer: the first-parent line of the fetched base, and the merged diff's stat in the judge's pack
    "--first-parent", "--stat",
}
# Modules whose flag-shaped constants describe commands they read and refuse, never ones they run.
FLAG_TABLE_MODULES = {"push_gate.py"}
# Modules allowed to start processes, and the only module each may use for it.
# ollivander.py reads the CLI catalogs and help, and runs the CLI updates and their checks. patrol.py runs gh
# api graphql with its fixed read-only queries.
PROCESS_MODULES_ALLOWED = {"run_desk.py": {"subprocess"}, "gitops.py": {"subprocess"}, "verify.py": {"subprocess"},
                           "ollivander.py": {"subprocess"}, "patrol.py": {"subprocess"}}
PLISTS = ("owlpost", "map", "morning", "keeper", "scoreboard", "portrait", "gringotts", "ollivander")
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


OPT_IN_NAMES = ("auto-draft-pr", "auto-portrait", "pr-followup", "auto-close", "worktree-cleanup", "owl-reports")
# An opt-in file named as a file: the whole string, or the last part of a path. The feature's name in a message
# ("auto-portrait applied ...") is not a file name.
OPT_IN_FILE_SHAPE = re.compile(r"(?:^|/)(?:" + "|".join(map(re.escape, OPT_IN_NAMES)) + r")/?$")
OPT_IN_ATTRIBUTES = {"AUTO_DRAFT_PR_FILE", "AUTO_PORTRAIT_FILE", "PR_FOLLOWUP_FILE", "AUTO_CLOSE_FILE",
                     "WORKTREE_CLEANUP_FILE", "OWL_REPORTS_FILE", "OPT_IN_FILES"}


def _docstrings(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def _is_opt_in_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "opt_in_on"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "common")


def opt_in_problems(name: str, source: str) -> list:
    """Each place a fleet module named name reads an office opt-in file other than through common.opt_in_on."""
    tree = ast.parse(source)
    found = []
    if name != "config.py":
        docs = _docstrings(tree)
        found += [f"line {node.lineno}: names an opt-in file" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                  and OPT_IN_FILE_SHAPE.search(node.value.strip())]
    if name not in ("config.py", "common.py"):
        through_reader = {id(node.args[0]) for node in ast.walk(tree) if _is_opt_in_call(node) and node.args}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in OPT_IN_ATTRIBUTES and id(node) not in through_reader:
                found.append(f"line {node.lineno}: {node.attr} outside common.opt_in_on")
            elif isinstance(node, ast.Name) and node.id in OPT_IN_ATTRIBUTES:
                found.append(f"line {node.lineno}: {node.id} by name")
            elif isinstance(node, ast.ImportFrom) and any(alias.name in OPT_IN_ATTRIBUTES for alias in node.names):
                found.append(f"line {node.lineno}: imports an opt-in name")
    if name == "common.py":
        for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "read_regular"):
                    continue
                named = node.args[1] if len(node.args) > 1 else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "name"), None)
                if not isinstance(named, ast.Constant) and function.name != "opt_in_on":
                    found.append(f"line {node.lineno}: {function.name} reads an office file by a name it is given")
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
        for path in SOURCES + TESTS + [RUNNER]:
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


class OptInTests(unittest.TestCase):
    def test_opt_in_files_are_read_only_through_the_shared_reader(self):
        self.assertEqual(set(config.OPT_IN_FILES), {config.AUTO_DRAFT_PR_FILE, config.AUTO_PORTRAIT_FILE,
                                                    config.PR_FOLLOWUP_FILE, config.AUTO_CLOSE_FILE,
                                                    config.WORKTREE_CLEANUP_FILE, config.OWL_REPORTS_FILE})
        self.assertEqual(len(config.OPT_IN_FILES), len(set(config.OPT_IN_FILES)))
        self.assertEqual(set(OPT_IN_NAMES), set(config.OPT_IN_FILES))
        self.assertIn(FLEET / "hooks" / "session_start.py", SOURCES)
        self.assertIn(FLEET / "portrait_auto.py", SOURCES)
        self.assertIn(FLEET / "followup.py", SOURCES)
        self.assertIn(FLEET / "closer.py", SOURCES)
        self.assertIn(FLEET / "worktree.py", SOURCES)
        # One reader and one list of switches, wherever a merge may have put a second copy.
        definitions = {(path.name, node.name if isinstance(node, ast.FunctionDef) else target.id)
                       for path in SOURCES for node in ast.parse(path.read_text()).body
                       for target in (node.targets if isinstance(node, ast.Assign) else [node])
                       if (isinstance(node, ast.FunctionDef) and node.name == "opt_in_on")
                       or (isinstance(node, ast.Assign) and isinstance(target, ast.Name)
                           and target.id in ("OPT_IN_FILES", "OPT_IN_MAX_BYTES"))}
        self.assertEqual(definitions, {("common.py", "opt_in_on"), ("common.py", "OPT_IN_MAX_BYTES"),
                                       ("config.py", "OPT_IN_FILES"), ("push.py", "OPT_IN_MAX_BYTES")})
        self.assertEqual(sum(1 for path in SOURCES for node in ast.walk(ast.parse(path.read_text()))
                             if isinstance(node, ast.FunctionDef) and node.name == "opt_in_on"), 1)
        for path in SOURCES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(opt_in_problems(path.name, path.read_text()), [])

    def test_the_shared_reader_check_catches_every_other_read(self):
        for name, snippet in (
            ("push.py", 'x = "auto-portrait"'),
            ("push.py", 'path = f"{root}/auto-draft-pr"'),
            ("owl_post.py", 'raw = safefs.read_regular(fd, "auto-portrait", 64)'),
            ("review.py", "raw = safefs.read_regular(fd, config.AUTO_DRAFT_PR_FILE, 64)"),
            ("portrait_auto.py", "on = config.AUTO_PORTRAIT_FILE in names"),
            ("portrait_auto.py", "from fleet.config import AUTO_PORTRAIT_FILE"),
            ("portrait_auto.py", "on = common.opt_in_on(name) or config.OPT_IN_FILES"),
            ("common.py", "def other(name):\n    return safefs.read_regular(fd, name, 64)"),
            ("hooks/x.py", "on = other.opt_in_on(config.AUTO_PORTRAIT_FILE)"),
            ("followup.py", 'x = "pr-followup"'),
            ("map.py", 'raw = safefs.read_regular(fd, "pr-followup", 64)'),
            ("patrol.py", "raw = safefs.read_regular(fd, config.PR_FOLLOWUP_FILE, 64)"),
            ("followup.py", "from fleet.config import PR_FOLLOWUP_FILE"),
            ("followup.py", "on = PR_FOLLOWUP_FILE in names"),
            ("closer.py", 'x = "auto-close"'),
            ("closer.py", "raw = safefs.read_regular(fd, config.AUTO_CLOSE_FILE, 64)"),
            ("closer.py", "from fleet.config import AUTO_CLOSE_FILE"),
            ("map.py", 'path = f"{root}/auto-close"'),
            ("worktree.py", 'x = "worktree-cleanup"'),
            ("worktree.py", "raw = safefs.read_regular(fd, config.WORKTREE_CLEANUP_FILE, 64)"),
            ("map.py", "from fleet.config import WORKTREE_CLEANUP_FILE"),
        ):
            with self.subTest(snippet=snippet):
                self.assertNotEqual(opt_in_problems(name.rsplit("/", 1)[-1], snippet), [])
        for name, snippet in (
            ("push.py", "on = common.opt_in_on(config.AUTO_DRAFT_PR_FILE)"),
            ("portrait_auto.py", 'SUMMARY = "auto-portrait applied {count} ops"'),
            ("portrait_auto.py", '"""Reads config.AUTO_PORTRAIT_FILE, the auto-portrait file."""'),
            ("followup.py", "on = common.opt_in_on(config.PR_FOLLOWUP_FILE)"),
            ("followup.py", 'STOP = "pr-followup is off"'),
            ("closer.py", "return common.opt_in_on(config.AUTO_CLOSE_FILE)"),
            ("closer.py", 'raise FleetError("auto-close is off; close it by hand")'),
            ("worktree.py", "return common.opt_in_on(config.WORKTREE_CLEANUP_FILE)"),
            ("worktree.py", 'KIND = "worktree-cleanup:kept"'),
            ("common.py", "def opt_in_on(name):\n    return safefs.read_regular(fd, name, 64)"),
            ("common.py", 'def other():\n    return safefs.read_regular(fd, "fixed", 64)'),
        ):
            with self.subTest(allowed=snippet):
                self.assertEqual(opt_in_problems(name, snippet), [])


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

    def test_only_gitops_writes_to_github_in_three_shapes(self):
        # A gh command that writes is built only in gitops, from its fixed shapes: no other fleet module names a gh
        # write method or the draft PR command, and followup.py, which posts the replies, starts no process at all.
        writes = {"--method", "--input", "--draft", "--body-file", "POST", "PATCH", "PUT", "DELETE"}
        for path in SOURCES:
            if path.name == "gitops.py":
                continue
            constants = {node.value for node in ast.walk(ast.parse(path.read_text()))
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            with self.subTest(path=path.name):
                self.assertEqual(constants & writes, set())
        self.assertNotIn("followup.py", PROCESS_MODULES_ALLOWED)
        self.assertEqual(imported_modules(ast.parse((FLEET / "followup.py").read_text())) & PROCESS_MODULES, set())

    def test_no_process_module_uses_a_shell_keyword(self):
        for path in [FLEET / name for name in PROCESS_MODULES_ALLOWED] + [RUNNER]:
            if not path.exists():
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        with self.subTest(path=path.name, line=node.lineno):
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


def _references(tree: ast.AST, names: set) -> list:
    """Every attribute or name in tree that is one of names, with its line."""
    found = []
    for node in ast.walk(tree):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in names:
            found.append((name, node.lineno))
    return found


class AutoCloseTests(unittest.TestCase):
    STORE = ROOT / "hogwarts"

    def test_only_the_closer_takes_the_proven_close(self):
        for path in SOURCES + sorted(self.STORE.glob("*.py")):
            if path.name in ("closer.py",) or path == self.STORE / "pensieve.py":
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(_references(ast.parse(path.read_text()), {"close_proven", "_insert_closure"}), [])
                self.assertNotIn("task_closures", path.read_text() if path.name != "db.py" else "")
        closer = ast.parse((FLEET / "closer.py").read_text())
        self.assertEqual([name for name, _ in _references(closer, {"close_proven"})], ["close_proven"])

    def test_closer_imports_no_github_write_path(self):
        tree = ast.parse((FLEET / "closer.py").read_text())
        writes = {"open_draft_pr", "run_gh_pr", "push_draft_pr", "_push_exact", "draft_pr_argv", "push", "run_gh"}
        self.assertEqual(_references(tree, writes), [])
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    for alias in node.names}
        self.assertNotIn("push", imported)
        git_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "attr", None)
                     == "git" and getattr(node.func.value, "id", None) == "gitops"]
        self.assertTrue(git_calls)
        for call in git_calls:  # the closer runs git only to read a diff for the judge's pack
            self.assertIsInstance(call.args[0], ast.List)
            self.assertEqual(call.args[0].elts[0].value, "diff")

    def test_closer_removes_worktrees_only_through_git(self):
        closer = ast.parse((FLEET / "closer.py").read_text())
        worktree = ast.parse((FLEET / "worktree.py").read_text())
        [remove_merged] = [node for node in worktree.body if isinstance(node, ast.FunctionDef)
                           and node.name == "remove_merged"]
        for label, tree in (("closer.py", closer), ("remove_merged", remove_merged)):
            with self.subTest(code=label):
                self.assertEqual(_references(tree, {"rmtree", "rmdir", "removedirs"}), [])
                strings = [inner.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                           for inner in ast.walk(node) if isinstance(inner, ast.Constant) and isinstance(inner.value, str)]
                self.assertFalse([value for value in strings if "prune" in value])
        self.assertEqual(_references(remove_merged, {"unlink", "remove"}), [])
        for node in ast.walk(closer):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("unlink", "remove"):
                with self.subTest(line=node.lineno):
                    self.assertIn("dir_fd", [keyword.arg for keyword in node.keywords])
        git_calls = [node for node in ast.walk(remove_merged) if isinstance(node, ast.Call)
                     and getattr(node.func, "attr", None) == "git"]
        self.assertEqual(len(git_calls), 1)
        self.assertEqual([element.value for element in git_calls[0].args[0].elts[:3]], ["worktree", "remove", "--force"])
