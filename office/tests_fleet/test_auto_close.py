"""Auto-close: the closer proves a passed task landed, CI on the merge commit is green and every after-merge check
holds, then closes it, or stops with one event.

Runs on real git repos in temp folders. origin keeps its GitHub URL, but url.<bare>.insteadOf sends every fetch and
push to a local bare repo, so nothing reaches the network. GitHub is faked at patrol.run_gh, which still runs the
patrol's guard on every command. The pre-push reviewer is faked at run_desk.run, as in test_review_loop; the
after-merge judge runs through the real run_desk.run with its process faked at run_desk.start_child. verify's sandbox
is plain bash here, as in test_review_loop.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from hogwarts import capacity, db, followups, ids, owlery, pensieve
from hogwarts.errors import StoreError

from fleet import closer, common, config, map as patrol_map, patrol, push, review, run_desk, safefs, verify
from fleet import worktree
from fleet.safefs import FleetError
from tests.support import TEST_TMP_ROOT
from tests_fleet.support import FleetCase, fake_children
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import ORIGIN, REPO_ID, LoopCase, claude_stream

BUILD_MD = """# {task_id} Add the widget check

## Intent
Add a check that the widget file exists.

## Acceptance criteria
AC-1 the readme is there | check: `test -f README.md`
{after}
## Spec
repo: {repo}
branch: fix/widget
base: origin/main

## Out of scope
Anything else.
"""
COMMAND_AC = "AC-2 the widget is on main | after merge: `test -f widget.txt`\n"
WRITTEN_AC = "AC-3 the merged widget reads well | after merge: the widget file says widget\n"
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # shaped like a GitHub token, built so no scanner trips
JUDGE_HEADER = re.compile(r"AFTER-MERGE (tk_[0-9a-f]{16}) @ ([0-9a-f]{40})")
SPLIT_TOKEN = TOKEN[:2] + "\u200b" + TOKEN[2:]  # a zero-width space inside it, so it shows whole
WIDE_TOKEN = "".join(chr(ord(char) + 0xFEE0) for char in TOKEN)  # fullwidth letters, read as the token
TWO_COMMANDS = COMMAND_AC + "AC-4 the readme is on main | after merge: `test -f README.md`\n"


def lock_busy(name: str, shared: bool = False) -> bool:
    """Whether some process holds the office lock name so that this one cannot take it now."""
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as fd:
        try:
            with safefs.held_lock(fd, name, blocking=False, shared=shared):
                return False
        except safefs.Busy:
            return True


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def check_run(name: str = "build", conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def status_context(context: str = "ci/legacy", state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state}


class FakeGitHub:
    """Answers the closer's two fixed queries from test data, after the patrol's own guard, and records each call."""

    def __init__(self) -> None:
        self.prs: list = []
        self.checks: dict = {}
        self.calls: list = []
        self.failing: set = set()
        self.landed_answer = None
        self.checks_answer = None

    def __call__(self, argv: list) -> bytes:
        patrol.guard(argv)
        query = argv[4][len("query="):]
        name = next(key for key, text in patrol.QUERIES.items() if text == query)
        variables = dict(field.split("=", 1) for field in argv[6::2])
        self.calls.append((name, variables))
        if name in self.failing:
            raise FleetError("gh failed: HTTP 502")
        if name == "landed":
            data = self.landed_answer or {"repository": {"pullRequests": {"totalCount": len(self.prs),
                                                                          "nodes": self.prs}}}
        elif name == "merge_checks":
            oid = variables["oid"]
            nodes = self.checks.get(oid, [check_run()])
            rollup = None if nodes is None else {"contexts": {"totalCount": len(nodes),
                                                              "pageInfo": {"hasNextPage": False}, "nodes": nodes}}
            data = self.checks_answer or {"repository": {"object": {"__typename": "Commit", "oid": oid,
                                                                    "statusCheckRollup": rollup}}}
        else:
            raise AssertionError(f"the closer asked GitHub {name}")
        return json.dumps({"data": data}).encode("utf-8")


_TEMPLATE: dict = {}


def _template_home() -> Path:
    """A home folder made once per test process and copied into each test: your git config, a checkout with one
    commit whose origin is the GitHub URL, and a bare repo standing in for that origin. Copying it saves each test
    the git runs that make it."""
    if "made" not in _TEMPLATE:
        root = Path(tempfile.mkdtemp(prefix="hogwarts-test-close-", dir=TEST_TMP_ROOT))
        made = root / "home"
        made.mkdir(mode=0o700)
        (made / ".gitconfig").write_text("[user]\n\tname = Test Person\n\temail = test@example.invalid\n")
        repo, bare = made / "repo", made / "bare.git"
        repo.mkdir()

        def git(*args, cwd=repo):
            subprocess.run([config.GIT_BIN, *args], cwd=cwd, capture_output=True, check=True,
                           env={"HOME": str(made), "PATH": config.CHILD_PATH})

        git("init", "-q", "-b", "main")
        (repo / "README.md").write_text("readme\n")
        os.chmod(repo / "README.md", 0o600)
        git("add", "README.md")
        git("commit", "-q", "-m", "first")
        git("config", "remote.origin.url", ORIGIN)
        git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("init", "-q", "--bare", "-b", "main", str(bare), cwd=made)
        git("push", "-q", str(bare), "main")
        _TEMPLATE.update(made=made, root=root)
    return _TEMPLATE["made"]


def tearDownModule() -> None:
    if "root" in _TEMPLATE:
        shutil.rmtree(_TEMPLATE["root"], ignore_errors=True)


class CloseCase(LoopCase):
    def setUp(self) -> None:
        FleetCase.setUp(self)  # LoopCase's own repo comes from the template below
        self.home_dir = self.tmp / "home"
        shutil.copytree(_template_home(), self.home_dir, symlinks=True)
        self.repo = self.home_dir / "repo"
        self.bare = self.home_dir / "bare.git"
        self.user_temp = self.tmp / "usertemp"
        self.user_temp.mkdir(mode=0o700)
        for patcher in (mock.patch.object(config, "USER_HOME_DIR", str(self.home_dir)),
                        mock.patch.object(verify, "sandbox_argv", lambda record, scratch, command: [
                            config.BASH_BIN, "--noprofile", "--norc", "-c", command]),
                        mock.patch.object(run_desk, "user_temp_dir", return_value=str(self.user_temp))):
            patcher.start()
            self.addCleanup(patcher.stop)
        # What git config url.<bare>.insteadOf would write to the checkout's own config, without a git run per test.
        with open(self.repo / ".git" / "config", "a") as config_file:
            config_file.write(f'[url "{self.bare}"]\n\tinsteadOf = {ORIGIN}\n')
        self.github = FakeGitHub()
        for patcher in (mock.patch.object(patrol, "run_gh", side_effect=self.github),
                        mock.patch.object(config, "AUTO_CLOSE_CI_SETTLE_SECONDS", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        spawn = mock.patch.object(run_desk, "spawn_closer")
        self.spawned_closers = spawn.start()
        self.addCleanup(spawn.stop)
        self.enable("hermione")
        self.enable("moody")
        self.t0 = int(time.time())
        self.judged = []
        self.opt_in()

    # switches and files

    def opt_in(self, text: str = "on\n") -> None:
        self.write_file(self.office / config.AUTO_CLOSE_FILE, text)

    def opt_out(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.office / config.AUTO_CLOSE_FILE)

    def reviews(self, task_id: str) -> Path:
        return self.office / "reviews" / task_id

    def record(self, task_id: str) -> dict:
        return json.loads((self.reviews(task_id) / "close.json").read_text())

    def close_events(self) -> list:
        return [event for event in self.events() if event["kind"].startswith("close.")]

    def kinds(self) -> list:
        return [event["kind"] for event in self.close_events()]

    # a passed build registered by a go

    def passed_build(self, after: str = "", verdict: str = "PASS", branch: str = "fix/widget",
                     files: dict = None) -> dict:
        """McGonagall's task registered as a go does (its spec with the TASK.md digest), Harry's task with its worktree
        on branch, his commit, and a review round that recorded verdict."""
        parent = ids.new_id("task")
        folder = self.castle / "tasks" / parent
        folder.mkdir(mode=0o700)
        text = BUILD_MD.format(task_id=parent, after=after, repo=self.repo).replace("fix/widget", branch)
        self.write_file(folder / "TASK.md", text)
        pensieve.create_task(self.conn, "mcgonagall", "add the widget check", intent_path=ids.intent_path(parent),
                             task_id=parent)
        pensieve.record_spec(self.conn, parent, str(self.repo), branch, "origin/main",
                             hashlib.sha256(text.encode("utf-8")).hexdigest())
        opened = owlery.open_request(self.conn, "mcgonagall", "harry", "add the widget check", body="build it",
                                     parent_task_id=parent, idempotency_key=f"go:{parent}")
        with mock.patch.object(run_desk, "spawn"):
            created = worktree.create(self.conn, opened["task"]["id"], str(self.repo), branch, fetch=False,
                                      start=False)
        wt = Path(created["worktree"])
        for name, text in {"widget.txt": f"widget for {branch}\n", **(files or {})}.items():
            self.write_file(wt / name, text)
            self.git("add", name, cwd=wt)
        self.git("commit", "-q", "-m", "Add the widget file", cwd=wt)
        with self.fake_reviewer(verdict):
            result = review.review_build(self.conn, opened["task"]["id"])
        return {"parent": parent, "task": result["task_id"], "sha": result["sha"], "wt": wt, "kind": "build",
                "request": result["request_id"]}

    def passed_own(self, after: str = "", branch: str = "feat/own", verdict: str = "PASS") -> dict:
        """A commit from your own session on branch, reviewed by Moody with fleet review own."""
        self.git("checkout", "-q", "-b", branch)
        self.write_file(self.repo / "widget.txt", f"widget from {branch}\n")
        self.git("add", "widget.txt")
        self.git("commit", "-q", "-m", "Add the widget file")
        intent = "Add the widget file.\nAC-1 the readme is there | check: `test -f README.md`\n" + after
        with self.fake_reviewer(verdict):
            result = review.review_own(self.conn, str(self.repo), title="add the widget", intent=intent, fetch=False)
        self.git("checkout", "-q", "main")
        return {"parent": None, "task": result["task_id"], "sha": result["sha"], "kind": "own",
                "request": result["request_id"]}

    # landing on origin

    def push_main(self, sha: str, branch: str = "main") -> None:
        """Move the bare origin's branch to sha, by its path, so the checkout's origin/main moves only on a fetch."""
        self.git("push", "-q", "-f", str(self.bare), f"{sha}:refs/heads/{branch}")

    def main_tip(self) -> str:
        return self.git("rev-parse", "main", cwd=self.bare)

    def merge_commit(self, sha: str, squash: bool = False, parent: str = None) -> str:
        tip = parent or self.main_tip()
        parents = ["-p", tip] if squash else ["-p", tip, "-p", sha]
        return self.git("commit-tree", f"{sha}^{{tree}}", *parents, "-m", "Merge pull request #7")

    def pr(self, ctx: dict, number: int = 7, state: str = "MERGED", head: str = None, base: str = "main",
           merge: str = None, cross: bool = False, head_repo: str = REPO_ID, merged_at: int = None) -> dict:
        merged = state == "MERGED"
        return {"number": number, "state": state, "merged": merged,
                "mergedAt": iso(merged_at or self.t0 + 3600) if merged else None,
                "createdAt": iso(self.t0), "closedAt": None if state == "OPEN" else iso(self.t0 + 3600),
                "headRefOid": head or ctx["sha"], "baseRefName": base, "isCrossRepository": cross,
                "headRepository": {"nameWithOwner": head_repo}, "mergeCommit": {"oid": merge} if merged else None}

    def land_pr(self, ctx: dict, squash: bool = False, number: int = 7) -> str:
        merge = self.merge_commit(ctx["sha"], squash=squash)
        self.push_main(merge)
        self.github.prs = [self.pr(ctx, number=number, merge=merge)]
        return merge

    def land_ff(self, ctx: dict) -> str:
        self.push_main(ctx["sha"])
        self.github.prs = []
        return ctx["sha"]

    # the closer

    def close(self, ctx: dict, now: int = None, manual: bool = False) -> dict:
        return closer.close_one(self.conn, ctx["task"], manual=manual, now=self.t0 + 7200 if now is None else now)

    def status(self, task_id: str) -> str:
        return pensieve.get_task(self.conn, task_id)["status"]

    # the after-merge judge, through the real run_desk.run with its process faked

    @contextlib.contextmanager
    def judge_says(self, verdict: str = "PASS", lines: dict = None, exit_code: int = 0, output=None, filler: str = ""):
        """The judge's process: reads its owl from the prompt and ends with an after-merge block whose verdict line
        is verdict and whose check lines are lines (id to word, every written check PASS by default), after filler."""
        def run(argv, **kwargs):
            match = JUDGE_HEADER.search(argv[-1])
            task_id, merge_sha = match.group(1), match.group(2)
            said = lines if lines is not None else {"AC-3": "PASS"}
            block = output if output is not None else (
                f"{filler}I read the pack.\nAFTER-MERGE {task_id} @ {merge_sha}\n"
                + "".join(f"{key} {word} | the pack shows it\n" for key, word in said.items())
                + f"VERDICT: {verdict}\n")
            if "--output-last-message" in argv:
                desk = Path(argv[argv.index("--output-last-message") + 1]).parent.name
            else:
                desk = Path(kwargs["cwd"]).name
            self.judged.append({"desk": desk, "prompt": argv[-1], "argv": argv})
            if "--output-last-message" in argv:
                Path(argv[argv.index("--output-last-message") + 1]).write_text(block)
            else:
                os.write(kwargs["stdout"], claude_stream(block).encode("utf-8"))
            return subprocess.CompletedProcess(args=argv, returncode=exit_code)
        with fake_children(run) as started:
            yield started


class OptInTests(CloseCase):
    def test_opt_in_off_by_default_and_only_the_office_file_holding_on_counts(self):
        self.opt_out()
        self.assertFalse(closer.auto_close_on())
        self.assertNotIn(config.AUTO_CLOSE_FILE, os.listdir(self.office))
        path = self.office / config.AUTO_CLOSE_FILE
        for text, on in (("on\n", True), (" on \n", True), ("", False), ("ON\n", False), ("on please\n", False),
                         ("on \nextra", False), ("yes\n", False)):
            with self.subTest(text=text):
                self.opt_in(text)
                self.assertEqual(closer.auto_close_on(), on)
        self.opt_in()
        os.chmod(path, 0o620)
        self.assertFalse(closer.auto_close_on())
        os.unlink(path)
        target = self.write_file(self.tmp / "elsewhere", "on\n")
        os.symlink(target, path)
        self.assertFalse(closer.auto_close_on())
        os.unlink(path)
        os.link(target, path)
        self.assertFalse(closer.auto_close_on())
        os.unlink(path)
        with mock.patch.object(safefs, "_check_owned", side_effect=safefs.Unsafe("not yours")):
            self.opt_in()
            self.assertFalse(closer.auto_close_on())
        os.unlink(path)
        for folder in (self.castle / "desks" / "harry", self.castle / "desks" / "mcgonagall", self.castle,
                       self.castle / "desks" / "hermione" / "inbox", self.office / "desks" / "harry"):
            self.write_file(folder / config.AUTO_CLOSE_FILE, "on\n")
        self.write_file(self.castle / "standing-orders.md", "# Standing orders\n\nauto-close: on\n")
        self.write_owl("mcgonagall", "order.json", {"to": "harry", "kind": "fyi", "subject": "auto-close on",
                                                    "body": "auto-close on"})
        self.assertFalse(closer.auto_close_on())

    def test_opt_in_every_switch_reads_through_one_reader(self):
        from fleet import followup, portrait_auto
        with mock.patch.object(common, "opt_in_on", return_value=True) as reader:
            self.assertTrue(push.auto_draft_pr_on())
            self.assertTrue(portrait_auto.auto_portrait_on())
            self.assertTrue(followup.switched_on())
            self.assertTrue(closer.auto_close_on())
        self.assertEqual([call.args for call in reader.call_args_list],
                         [(config.AUTO_DRAFT_PR_FILE,), (config.AUTO_PORTRAIT_FILE,), (config.PR_FOLLOWUP_FILE,),
                          (config.AUTO_CLOSE_FILE,)])
        self.write_file(self.office / "auto-everything", "on\n")
        self.assertFalse(common.opt_in_on("auto-everything"))
        self.assertTrue(common.opt_in_on(config.AUTO_CLOSE_FILE))
        self.assertEqual(config.OPT_IN_FILES, (config.AUTO_DRAFT_PR_FILE, config.AUTO_PORTRAIT_FILE,
                                               config.PR_FOLLOWUP_FILE, config.AUTO_CLOSE_FILE,
                                               config.WORKTREE_CLEANUP_FILE, config.OWL_REPORTS_FILE,
                                               config.CROSS_FAMILY_FAILOVER_FILE, config.ORCHESTRATOR_FILE))


class MapRoundCase(CloseCase):
    def setUp(self) -> None:
        super().setUp()
        from tests_fleet.test_patrol import FakeGitHub as PatrolGitHub
        self.patrol_github = PatrolGitHub()
        for patcher in (mock.patch.object(patrol, "run_gh", side_effect=self.patrol_github),
                        mock.patch.object(patrol_map, "lineup_due", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.office / "patrol").mkdir(mode=0o700)
        self.write_file(self.office / "patrol" / "shadow", "shadow mode\n")

    def awaiting(self) -> str:
        """Your own sessions' task awaiting close, as castle task await-close leaves one."""
        task = pensieve.create_task(self.conn, "ryan-claude-1", "own work")
        pensieve.start_task(self.conn, task["id"])
        pensieve.mark_awaiting_close(self.conn, task["id"])
        return task["id"]

    def round_row(self) -> dict:
        return patrol.read_rows("map", "rounds.jsonl")[-1]


class MapRoundTests(MapRoundCase):
    def test_map_round_starts_the_closer_while_on_in_shadow_mode_too(self):
        self.awaiting()
        with mock.patch.object(config, "GITHUB_ACCOUNT", "octo"):
            result = patrol_map.run_round(self.conn, now=self.t0)
        self.assertTrue(result["ok"] and result["shadow"])
        self.assertEqual((result["closer"], self.round_row()["closer"]), ("started", "started"))
        self.spawned_closers.assert_called_once_with()

    def test_map_round_starts_the_closer_on_a_round_that_could_not_read_github(self):
        self.awaiting()
        with mock.patch.object(config, "GITHUB_ACCOUNT", "<github-account>"):  # not set, as before onboarding
            result = patrol_map.run_round(self.conn, now=self.t0)
        self.assertFalse(result["ok"])
        self.assertEqual((result["closer"], self.round_row()["closer"]), ("started", "started"))
        self.patrol_github.endless = True
        with mock.patch.object(config, "GITHUB_ACCOUNT", "octo"):
            self.patrol_github.prs = [{"number": 1}]
            result = patrol_map.run_round(self.conn, now=self.t0 + 900)
        self.assertTrue(result.get("incomplete"))
        self.assertEqual(self.round_row()["closer"], "started")
        self.assertEqual(self.spawned_closers.call_count, 2)

    def test_map_round_starts_nothing_while_off_or_while_a_closer_runs(self):
        self.awaiting()
        self.opt_out()
        self.assertEqual(patrol_map.run_round(self.conn, now=self.t0)["closer"], "off")
        self.opt_in()
        with closer.closer_lock():
            self.assertEqual(patrol_map.run_round(self.conn, now=self.t0 + 900)["closer"], "running")
        self.spawned_closers.assert_not_called()
        self.assertEqual(self.kinds(), [])

    def test_map_round_closer_that_cannot_start_is_one_event_a_day(self):
        self.awaiting()
        self.spawned_closers.side_effect = OSError("no python")
        noon = int(time.mktime((2027, 1, 15, 12, 0, 0, 0, 0, -1)))  # local noon, so every round below is one day
        for offset in (0, 900):
            self.assertEqual(patrol_map.run_round(self.conn, now=noon + offset)["closer"], "failed")
        [event] = self.close_events()
        self.assertEqual((event["kind"], event["verdict"]), ("close.sweep-failed", "headmaster"))
        with mock.patch.object(pensieve, "list_tasks", side_effect=StoreError("store is busy")):
            self.assertEqual(closer.sweep(self.conn, now=noon + 1800), "failed")
        self.assertEqual(len(self.close_events()), 1)
        self.assertEqual(patrol_map.run_round(self.conn, now=noon + 86400)["closer"], "failed")
        self.assertEqual(len(self.close_events()), 2)  # the next day is told once more

    def test_map_round_starts_the_closer_for_any_awaiting_task_without_judging_candidates(self):
        self.assertEqual(closer.sweep(self.conn, now=self.t0), "idle")
        self.awaiting()  # no round, no PASS: not a candidate, but the sweep never judges that
        with mock.patch.object(closer, "candidates", side_effect=AssertionError("the sweep judged candidates")), \
                mock.patch.object(closer, "candidacy", side_effect=AssertionError("the sweep judged candidates")):
            self.assertEqual(closer.sweep(self.conn, now=self.t0), "started")
        self.spawned_closers.reset_mock()
        (self.office / "worktrees").mkdir(mode=0o700, exist_ok=True)
        with mock.patch.object(pensieve, "list_tasks", return_value=[]):
            self.write_file(self.office / "worktrees" / f"{ids.new_id('task')}.merged-{'a' * 12}.json", "{}")
            self.assertEqual(closer.sweep(self.conn, now=self.t0), "started")
            with mock.patch.object(closer, "_merged_records", side_effect=safefs.Unsafe("not a plain folder")):
                self.assertEqual(closer.sweep(self.conn, now=self.t0), "started")
        self.assertEqual(self.spawned_closers.call_count, 2)


class CandidateTests(CloseCase):
    def test_candidates_are_go_builds_and_own_tasks_after_a_round_pass(self):
        # A build under a task registered by hand (no go spec), passed the same way.
        _, by_hand, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / "other.txt", "other\n")
        self.git("add", "other.txt", cwd=wt)
        self.git("commit", "-q", "-m", "other", cwd=wt)
        with self.fake_reviewer("PASS"):
            review.review_build(self.conn, by_hand["id"])
        build = self.passed_build(branch="fix/go")
        own = self.passed_own(branch="feat/own-a")
        # A PASS written with castle review record, which no round holds.
        recorded = pensieve.create_task(self.conn, "ryan-claude-1", "recorded pass")
        pensieve.start_task(self.conn, recorded["id"])
        pensieve.set_review_branch(self.conn, recorded["id"], "feat/recorded")
        pensieve.set_worktree(self.conn, recorded["id"], f"{ids.WORKTREES_ROOT}/{recorded['id']}")
        pensieve.record_commit(self.conn, recorded["id"], REPO_ID, "f" * 40)
        owlery.open_request(self.conn, "ryan-claude-1", "moody", "review", parent_task_id=recorded["id"])
        owlery.record_review(self.conn, REPO_ID, "f" * 40, recorded["id"], "moody", "PASS")
        pensieve.mark_awaiting_close(self.conn, recorded["id"])
        active = pensieve.create_task(self.conn, "ryan-claude-1", "still active")
        pensieve.start_task(self.conn, active["id"])
        closed = pensieve.create_task(self.conn, "ryan-claude-1", "closed")
        pensieve.close_task(self.conn, closed["id"], "abandoned")
        found = [task["id"] for task in closer.candidates(self.conn)]
        self.assertEqual(found, [build["task"], own["task"]])
        self.assertEqual(closer.candidacy(self.conn, pensieve.get_task(self.conn, build["task"]))["kind"], "build")
        self.assertEqual(closer.candidacy(self.conn, pensieve.get_task(self.conn, own["task"]))["kind"], "own")
        for task_id in (by_hand["id"], recorded["id"], active["id"], closed["id"]):
            with self.subTest(task=task_id):
                self.assertIsNone(closer.candidacy(self.conn, pensieve.get_task(self.conn, task_id)))
                self.assertEqual(closer.close_one(self.conn, task_id, now=self.t0)["outcome"], "not the closer's")
        self.assertEqual(self.kinds(), [])

    def test_candidates_unreadable_office_record_is_unknown_never_skipped(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        record = self.office / "worktrees" / f"{ctx['task']}.json"
        os.chmod(record, 0o000)
        self.addCleanup(os.chmod, record, 0o600)
        self.assertEqual([task["id"] for task in closer.candidates(self.conn)], [ctx["task"]])
        first = self.close(ctx, now=self.t0)
        self.assertEqual((first["outcome"], first["step"]), ("unknown", "record"))
        self.close(ctx, now=self.t0 + config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS - 1)
        self.assertEqual(self.kinds(), [])
        self.close(ctx, now=self.t0 + config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS)
        self.close(ctx, now=self.t0 + config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS + 900)
        self.assertEqual(self.kinds(), ["close.unknown"])
        os.chmod(record, 0o600)
        data = json.loads(record.read_text())
        self.write_file(record, json.dumps({**data, "branch": "fix/other"}))
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "record"))
        self.assertEqual(self.kinds(), ["close.unknown", "close.stopped"])
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_candidates_skip_a_task_when_a_followup_is_open(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        self.assertFalse(closer.followup_open(self.conn, ctx["task"]))
        with mock.patch.object(closer, "followup_open", return_value=True) as followup:
            self.assertEqual(closer.candidates(self.conn), [])
            result = self.close(ctx)
            self.assertEqual((result["outcome"], result["why"]), ("not the closer's", "a follow-up is open on it"))
            with self.assertRaisesRegex(FleetError, "follow-up is open"):
                closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7200)
        followup.assert_called_with(self.conn, ctx["task"])
        self.assertEqual((self.status(ctx["task"]), self.kinds(), self.github.calls), ("awaiting_close", [], []))
        self.assertEqual(self.close(ctx)["outcome"], "closed")


class FollowupTests(CloseCase):
    """The closer beside a teammate PR follow-up (fleet/followup.py), with the follow-up's own rows in the store
    (hogwarts.followups), never a mock: while it has not ended, nothing the closer does closes its task."""

    def followup_passed(self, ctx: dict, push_needed: bool) -> dict:
        """The PR the review loop opened, bound to the build, and a follow-up routed, built and passed by its own round
        at the reviewed commit (replies with no code change). The task awaits close again with the follow-up pushing or
        posting, as it does from the follow-up's PASS until its last reply."""
        url = followups.pr_url(REPO_ID, 7)
        followups.bind_pr(self.conn, ctx["task"], REPO_ID, 7, "fix/widget", "main", ctx["sha"], url)
        item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": "11", "url": f"{url}#issuecomment-11",
                "quote": None}
        row = followups.open_followup(self.conn, ctx["task"], ids.new_id("followup"), ctx["sha"], [item],
                                      [{"kind": "comment", "comment_id": "11", "label": "T1"}], config.PATROL_SENDER,
                                      "follow-up 1", "the fix request", config.FOLLOWUP_MAX_PER_TASK)
        self.assertEqual((self.status(ctx["task"]), closer.candidates(self.conn)), ("active", []))
        followups.advance(self.conn, row["id"], "starting")
        followups.advance(self.conn, row["id"], "building")
        first = capacity.review_rounds(self.conn, ctx["task"])[-1]
        tagged = capacity.open_review_round(self.conn, ctx["task"], first["reviewer"], ctx["sha"], "follow-up review",
                                            followup_id=row["id"], followup_max_rounds=config.FOLLOWUP_ROUND_CAP)
        capacity.record_round_verdict(self.conn, tagged["request"]["id"], REPO_ID, "PASS")
        review.record_round_inputs(ctx["task"], tagged["request"]["id"], ctx["sha"], None,
                                   review.round_inputs(ctx["task"], first["request_id"])["task_md_sha256"])
        pensieve.close_task(self.conn, tagged["task"]["id"], "superseded")
        pensieve.mark_awaiting_close(self.conn, ctx["task"])
        return followups.plan_replies(self.conn, row["id"], ctx["sha"],
                                      [{"label": "T1", "mark": "PUSHBACK", "body": "It keeps its name."}], push_needed)

    def assert_left_alone(self, ctx: dict) -> None:
        task = pensieve.get_task(self.conn, ctx["task"])
        self.assertEqual(closer.candidacy(self.conn, task)["round"]["followup_id"],
                         followups.open_for_task(self.conn, ctx["task"])["id"])  # the follow-up is the one reason
        self.assertTrue(closer.followup_open(self.conn, ctx["task"]))
        self.assertEqual(closer.candidates(self.conn), [])
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["why"]), ("not the closer's", "a follow-up is open on it"))
        with self.assertRaisesRegex(FleetError, "follow-up is open"):
            closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7200)
        worked = [item.get("task_id") for item in closer.run_pass(self.conn, now=self.t0 + 7200)]
        self.assertNotIn(ctx["task"], worked)
        self.assertEqual((self.status(ctx["task"]), self.kinds(), self.github.calls), ("awaiting_close", [], []))

    def test_an_open_followup_holds_the_closer_off_until_it_is_done(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        row = self.followup_passed(ctx, push_needed=True)
        self.assertEqual(row["state"], "pushing")
        self.assert_left_alone(ctx)
        followups.advance(self.conn, row["id"], "posting")
        self.assert_left_alone(ctx)
        followups.begin_reply(self.conn, row["id"], "T1")
        self.assert_left_alone(ctx)
        followups.end_reply(self.conn, row["id"], "T1", "posted", "501")
        followups.advance(self.conn, row["id"], "done")
        self.assertFalse(closer.followup_open(self.conn, ctx["task"]))
        self.assertEqual([task["id"] for task in closer.candidates(self.conn)], [ctx["task"]])
        result = self.close(ctx)
        self.assertEqual((result["outcome"], self.status(ctx["task"]), self.kinds()),
                         ("closed", "closed", ["close.proven"]))

    def test_a_followup_that_stopped_leaves_the_task_to_the_closer(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        row = self.followup_passed(ctx, push_needed=False)
        self.assert_left_alone(ctx)
        followups.stop(self.conn, row["id"], "stopped by a test")
        self.assertEqual(self.close(ctx)["outcome"], "closed")

    def test_a_followup_the_closer_cannot_read_is_unknown_never_none_open(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        with mock.patch.object(followups, "open_for_task", side_effect=StoreError("database is locked")):
            self.assertEqual([task["id"] for task in closer.candidates(self.conn)], [ctx["task"]])
            result = self.close(ctx)
            self.assertEqual((result["outcome"], result["step"]), ("unknown", "error"))
            with self.assertRaises(StoreError):
                closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7200)
        self.assertEqual((self.status(ctx["task"]), self.kinds(), self.github.calls), ("awaiting_close", [], []))
        self.assertEqual(self.close(ctx)["outcome"], "closed")

    def test_a_followup_opened_while_the_closer_worked_keeps_the_task_open(self):
        """A follow-up that opens after the closer's gates keeps its task open: the closer sees the task change before
        the close, and when every check of its own is passed by, the store refuses the close and the closer then
        finds the follow-up, so it is the follow-up's, never an unknown."""
        ctx = self.passed_build()
        self.land_ff(ctx)
        real_gates, opened = closer._gates, []

        def gates_then_followup(a):
            real_gates(a)
            if not opened:
                opened.append(self.followup_passed(ctx, push_needed=False))

        with mock.patch.object(closer, "_gates", side_effect=gates_then_followup):
            result = self.close(ctx)
        self.assertEqual(result["outcome"], "not the closer's")
        self.assertEqual((self.status(ctx["task"]), pensieve.task_closure(self.conn, ctx["task"])),
                         ("awaiting_close", None))
        checks = iter([False, False])  # the gates and the check right before the close miss it
        with mock.patch.object(closer, "followup_open", side_effect=lambda conn, task_id: next(checks, True)), \
                mock.patch.object(pensieve, "close_proven", wraps=pensieve.close_proven) as close_proven:
            result = self.close(ctx)
        close_proven.assert_called_once()
        self.assertEqual((result["outcome"], result["why"]), ("not the closer's", "a follow-up is open on it"))
        self.assertEqual((self.status(ctx["task"]), pensieve.task_closure(self.conn, ctx["task"]), self.kinds()),
                         ("awaiting_close", None, []))
        # The store refused the close and the follow-up cannot be read now: unknown at the close, never closed.
        answers = iter([False, False])

        def then_unreadable(conn, task_id):
            answer = next(answers, None)
            if answer is None:
                raise StoreError("database is locked")
            return answer

        with mock.patch.object(closer, "followup_open", side_effect=then_unreadable):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("unknown", "close"))
        self.assertEqual((self.status(ctx["task"]), pensieve.task_closure(self.conn, ctx["task"])),
                         ("awaiting_close", None))


class ApprovedTaskMdTests(CloseCase):
    def round_record_path(self, ctx: dict) -> Path:
        return self.reviews(ctx["task"]) / f"round-{ctx['request']}.json"

    def test_approved_task_md_go_build_stops_when_the_pass_round_read_another_task_md(self):
        ctx = self.passed_build(after=COMMAND_AC)
        self.land_ff(ctx)
        path = self.round_record_path(ctx)
        other = b"# another TASK.md\nAC-2 x | after merge: `touch outside`\n"
        verify.keep_task_md(ctx["task"], other)
        data = json.loads(path.read_text())
        self.write_file(path, json.dumps({**data, "task_md_sha256": hashlib.sha256(other).hexdigest()}))
        with mock.patch.object(verify, "run_check", side_effect=AssertionError("a command ran")):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "taskmd"))
        [event] = self.close_events()
        self.assertIn("TASK.md is not the one you approved; close it by hand", event["summary"])

    def test_approved_task_md_edited_between_go_and_pass_stops_with_one_event(self):
        parent = ids.new_id("task")
        folder = self.castle / "tasks" / parent
        folder.mkdir(mode=0o700)
        approved = BUILD_MD.format(task_id=parent, after="", repo=self.repo)
        self.write_file(folder / "TASK.md", approved)
        pensieve.create_task(self.conn, "mcgonagall", "add the widget check", intent_path=ids.intent_path(parent),
                             task_id=parent)
        pensieve.record_spec(self.conn, parent, str(self.repo), "fix/widget", "origin/main",
                             hashlib.sha256(approved.encode("utf-8")).hexdigest())
        opened = owlery.open_request(self.conn, "mcgonagall", "harry", "add the widget check", body="build it",
                                     parent_task_id=parent)
        with mock.patch.object(run_desk, "spawn"):
            created = worktree.create(self.conn, opened["task"]["id"], str(self.repo), "fix/widget", fetch=False,
                                      start=False)
        # McGonagall's ask-level edit after the go: a new after-merge command nobody approved.
        self.write_file(folder / "TASK.md", approved.replace("## Spec", COMMAND_AC + "\n## Spec"))
        wt = Path(created["worktree"])
        self.write_file(wt / "widget.txt", "widget\n")
        self.git("add", "widget.txt", cwd=wt)
        self.git("commit", "-q", "-m", "Add the widget file", cwd=wt)
        with self.fake_reviewer("PASS"):
            result = review.review_build(self.conn, opened["task"]["id"])
        ctx = {"task": result["task_id"], "sha": result["sha"], "parent": parent}
        self.land_ff(ctx)
        with mock.patch.object(verify, "run_check", side_effect=AssertionError("a command ran")):
            for _ in range(2):
                self.close(ctx)
            closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual(self.kinds(), ["close.stopped"])
        self.assertEqual(self.record(ctx["task"])["stopped"]["step"], "taskmd")
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_approved_task_md_own_task_stops_when_it_differs_from_the_approval_file(self):
        ctx = self.passed_own()
        self.land_ff(ctx)
        approval = self.reviews(ctx["task"]) / review.APPROVED_FILE
        self.assertEqual(approval.read_text(), json.loads(self.round_record_path(ctx).read_text())["task_md_sha256"]
                         + "\n")
        os.chmod(approval, 0o600)
        self.write_file(approval, "d" * 64 + "\n")
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "taskmd"))
        self.assertEqual(self.kinds(), ["close.stopped"])

    def test_approved_task_md_own_task_without_an_approval_file_is_legacy(self):
        ctx = self.passed_own()
        self.land_ff(ctx)
        os.unlink(self.reviews(ctx["task"]) / review.APPROVED_FILE)
        self.assertEqual(self.close(ctx)["outcome"], "legacy")
        [event] = self.close_events()
        self.assertEqual((event["kind"], event["verdict"]), ("close.legacy", "routine"))
        self.assertEqual(self.record(ctx["task"])["state"], "legacy")
        self.assertEqual(closer.candidates(self.conn), [])

    def test_approved_task_md_unreadable_approval_file_is_unknown_not_legacy(self):
        ctx = self.passed_own()
        self.land_ff(ctx)
        approval = self.reviews(ctx["task"]) / review.APPROVED_FILE
        os.chmod(approval, 0o000)
        self.addCleanup(os.chmod, approval, 0o600)
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("unknown", "taskmd"))
        self.assertEqual(self.record(ctx["task"])["state"], "watching")
        self.assertEqual(self.kinds(), [])
        os.chmod(approval, 0o600)
        self.assertEqual(self.close(ctx)["outcome"], "closed")


class Counting:
    """verify.run_check wrapped, counting each command run, or ending the process at the given runs. then maps a run's
    number to what happens while it runs (called once it has ended, before the closer sees its result)."""

    def __init__(self, kill_at: tuple = (), then: dict = None) -> None:
        self.calls, self.kill_at, self.then = [], kill_at, then or {}
        self.real = verify.run_check

    def __call__(self, record, scratch, command, sandboxed=True, keep_fds=()):
        self.calls.append({"path": record["path"], "command": command, "sandboxed": sandboxed,
                           "after": ".merged-" in record["path"], "keep_fds": keep_fds})
        number = len(self.calls)
        if number in self.kill_at:
            raise Killed()
        result = self.real(record, scratch, command, sandboxed, keep_fds)
        if number in self.then:
            self.then[number]()
        return result


class ClosesTests(CloseCase):
    def test_closes_the_build_and_its_go_parent_with_one_headmaster_event(self):
        ctx = self.passed_build(after=COMMAND_AC + WRITTEN_AC)
        merge = self.land_pr(ctx)
        with self.judge_says("PASS"):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["parent"]), ("closed", ctx["parent"]))
        for task_id, kind in ((ctx["task"], "proven"), (ctx["parent"], "parent")):
            task = pensieve.get_task(self.conn, task_id)
            self.assertEqual((task["status"], task["close_reason"]), ("closed", "complete"))
            self.assertEqual(pensieve.task_closure(self.conn, task_id)["kind"], kind)
        [event] = self.close_events()
        self.assertEqual((event["kind"], event["verdict"], event["desk"]), ("close.proven", "headmaster", "harry"))
        for named in (ctx["task"], "PR #7", ctx["sha"][:12], merge[:12], "AC-2 exited 0", "AC-3 passed by hermione",
                      f"go task {ctx['parent']} closed with it"):
            self.assertIn(named, event["summary"])
        closure = pensieve.task_closure(self.conn, ctx["task"])
        evidence = (self.reviews(ctx["task"]) / f"close-evidence-{merge}.md").read_bytes()
        self.assertEqual((closure["command_checks"], closure["written_checks"], closure["judge_desk"]),
                         (1, 1, "hermione"))
        self.assertEqual(closure["evidence_sha256"], hashlib.sha256(evidence).hexdigest())
        self.assertEqual(closure["evidence_path"], f"{ids.REVIEWS_ROOT}/{ctx['task']}/close-evidence-{merge}.md")
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual(len(self.close_events()), 1)

    def test_closes_the_build_alone_when_its_parent_has_other_open_work(self):
        ctx = self.passed_build()
        later = owlery.open_request(self.conn, "mcgonagall", "harry", "a follow-on", body="more",
                                    parent_task_id=ctx["parent"])
        self.land_pr(ctx)
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["parent"]), ("closed", None))
        self.assertEqual((self.status(ctx["task"]), self.status(ctx["parent"])), ("closed", "queued"))
        self.assertEqual(self.status(later["task"]["id"]), "queued")
        self.assertIsNone(pensieve.task_closure(self.conn, ctx["parent"]))
        [event] = self.close_events()
        self.assertIn(f"its go task {ctx['parent']} stays open, since 1 other open tasks are under it", event["summary"])

    def test_closes_an_own_session_task_alone(self):
        ctx = self.passed_own(after=COMMAND_AC)
        self.github.prs = [self.pr(ctx, number=11, merge=self.merge_commit(ctx["sha"]))]
        self.push_main(self.github.prs[0]["mergeCommit"]["oid"])
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["parent"]), ("closed", None))
        self.assertEqual(pensieve.get_task(self.conn, ctx["task"])["close_reason"], "complete")
        [event] = self.close_events()
        self.assertEqual((event["desk"], event["verdict"]), ("ryan-claude-1", "headmaster"))
        self.assertIn("PR #11", event["summary"])
        self.assertEqual(self.github.calls[0], ("landed", {"owner": "acme", "name": "web-app", "head": "feat/own"}))

    def test_closes_nothing_a_hand_close_got_to_first(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        real = pensieve.close_proven

        def you_first(conn, task_id, *args, **kwargs):
            token = owlery.mint(conn, task_id, "cli")["token"]
            pensieve.close_task(conn, task_id, "complete", token)
            return real(conn, task_id, *args, **kwargs)

        with mock.patch.object(pensieve, "close_proven", side_effect=you_first):
            self.assertEqual(self.close(ctx)["outcome"], "closed by hand")
        self.assertEqual(self.kinds(), [])
        self.assertIsNone(pensieve.task_closure(self.conn, ctx["task"]))
        self.assertEqual(self.record(ctx["task"])["state"], "done")
        self.assertEqual(self.close(ctx)["outcome"], "not the closer's")


class LegacyTests(CloseCase):
    def test_legacy_pass_without_a_frozen_task_md_is_one_routine_event(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                ctx = self.passed_build(branch=f"fix/legacy-{int(missing)}")
                self.land_pr(ctx)
                record = self.reviews(ctx["task"]) / f"round-{ctx['request']}.json"
                if missing:
                    os.unlink(record)
                else:
                    data = json.loads(record.read_text())
                    del data["task_md_sha256"]
                    self.write_file(record, json.dumps(data))
                self.assertEqual(self.close(ctx)["outcome"], "legacy")
                closer.run_pass(self.conn, now=self.t0 + 9000)
                event = self.close_events()[-1]
                self.assertEqual((event["kind"], event["verdict"]), ("close.legacy", "routine"))
                self.assertIn(f"Mischief managed {ctx['task']}", event["summary"])
                self.assertEqual(self.status(ctx["task"]), "awaiting_close")
        self.assertEqual(self.kinds(), ["close.legacy", "close.legacy"])
        with self.assertRaisesRegex(FleetError, "Mischief managed closes it by hand"):
            closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 9900)


class FleetCloseTests(CloseCase):
    def test_fleet_close_retries_a_stopped_task_once_and_keeps_a_kept_verdict(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)
        with self.judge_says("PASS"), mock.patch.object(pensieve, "close_proven",
                                                        side_effect=StoreError("disk full")):
            self.assertEqual(self.close(ctx, now=self.t0)["outcome"], "unknown")
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        self.assertEqual(self.close(ctx, now=self.t0)["outcome"], "stopped")
        self.github.checks[merge] = [check_run("build (re-run)")]
        with self.judge_says("PASS") as started:
            code, out = self.fleet("close", ctx["task"])
        self.assertEqual((code, out["data"]["outcome"]), (0, "closed"), out)
        self.assertEqual((started.call_count, len(self.judged)), (0, 1))
        self.assertIn("close-clear1", os.listdir(self.reviews(ctx["task"])))
        self.assertEqual(self.kinds(), ["close.stopped", "close.proven"])

    def fleet(self, *args) -> tuple:
        from fleet import tools
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(common, "connect", side_effect=lambda: db.connect(self.db_path)):
            code = tools.main(list(args))
        return code, json.loads(out.getvalue())

    def test_fleet_close_refuses_while_auto_close_is_off(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.opt_out()
        code, out = self.fleet("close", ctx["task"])
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("auto-close is off; Mischief managed closes it by hand", out["error"])
        self.opt_in()
        with closer.closer_lock():
            with self.assertRaisesRegex(FleetError, "an auto-close pass is running"):
                closer.close_by_hand(self.conn, ctx["task"])
        self.assertEqual((self.status(ctx["task"]), self.github.calls), ("awaiting_close", []))

    def test_fleet_close_a_stop_after_the_retry_is_heard_again(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        self.assertEqual(self.close(ctx)["outcome"], "stopped")
        self.assertEqual(closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7300)["outcome"], "stopped")
        self.assertEqual(self.kinds(), ["close.stopped", "close.stopped"])
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual(len(self.close_events()), 2)

    def test_fleet_close_never_overturns_a_kept_changes_verdict_or_a_red(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        with self.judge_says("CHANGES", lines={"AC-3": "CHANGES"}):
            self.assertEqual(self.close(ctx)["step"], "judge")
        with self.judge_says("PASS") as started:
            result = closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7300)
        self.assertEqual((result["outcome"], result["step"], started.call_count), ("stopped", "judge", 0))
        red = self.passed_build(branch="fix/red")
        merge = self.land_pr(red)
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        self.assertEqual(self.close(red)["step"], "ci")
        self.assertEqual(closer.close_by_hand(self.conn, red["task"], now=self.t0 + 7300)["step"], "ci")
        other_head = self.passed_build(branch="fix/other-head")
        self.push_main(self.merge_commit(other_head["sha"]))
        self.github.prs = [self.pr(other_head, head="e" * 40, merge=self.main_tip())]
        self.assertEqual(self.close(other_head)["step"], "landed")
        self.assertEqual(closer.close_by_hand(self.conn, other_head["task"], now=self.t0 + 7300)["step"], "landed")
        self.assertTrue(all(self.status(item["task"]) == "awaiting_close" for item in (ctx, red, other_head)))
