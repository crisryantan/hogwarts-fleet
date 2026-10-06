"""The review loop end to end on real git repos in temp folders: worktree, verify, review.

Reviewer runs are faked at run_desk.run, so no model is ever called. verify's sandbox is
replaced with plain bash in these tests; test_verify_builds_the_sandbox_profile checks the
real argv without running it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from hogwarts import capacity, ids, owlery, pensieve

from fleet import config, gitops, owl_post, review, run_desk, safefs, verify, worktree
from fleet.safefs import FleetError
from tests_fleet.support import FleetCase

ORIGIN = "https://github.com/acme/web-app.git"
REPO_ID = "acme/web-app"
TASK_MD = """# {task_id} Add the widget check

## Intent
Add a check that the widget file exists.

## Acceptance criteria
AC-1 the readme is there | check: `test -f README.md`
AC-2 the widget is there | check: `test -f widget.txt`
AC-3 the diff stays small | check: one file changes

## Out of scope
Anything else.
"""
REAL_SANDBOX_ARGV = verify.sandbox_argv  # captured before any test patches it
HANDOFF = """HANDOFF {task_id} round 1
CHANGED
- widget.txt | the widget
COMMIT MESSAGE
Add the widget file

It holds the widget.
PR BODY DRAFT
Adds the widget.
CHECKPOINT
done
"""


class LoopCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.home_dir = self.tmp / "home"
        self.home_dir.mkdir(mode=0o700)
        self.write_file(self.home_dir / ".gitconfig", "[user]\n\tname = Test Person\n\temail = test@example.invalid\n")
        patcher = mock.patch.object(config, "USER_HOME_DIR", str(self.home_dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.home_dir / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write_file(self.repo / "README.md", "readme\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "first")
        self.git("config", "remote.origin.url", ORIGIN)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        sandbox = mock.patch.object(verify, "sandbox_argv",
                                    lambda record, scratch, command: [config.BASH_BIN, "--noprofile", "--norc", "-c",
                                                                      command])
        sandbox.start()
        self.addCleanup(sandbox.stop)
        self.user_temp = self.tmp / "usertemp"
        self.user_temp.mkdir(mode=0o700)
        temp = mock.patch.object(run_desk, "user_temp_dir", return_value=str(self.user_temp))
        temp.start()
        self.addCleanup(temp.stop)

    def git(self, *args, cwd=None) -> str:
        done = subprocess.run([config.GIT_BIN, *args], cwd=cwd or self.repo, capture_output=True, check=True,
                              env={"HOME": str(self.home_dir), "PATH": config.CHILD_PATH})
        return done.stdout.decode().strip()

    def parent_task(self) -> str:
        task_id = ids.new_id("task")
        folder = self.castle / "tasks" / task_id
        folder.mkdir(mode=0o700)
        self.write_file(folder / "TASK.md", TASK_MD.format(task_id=task_id))
        pensieve.create_task(self.conn, "mcgonagall", "add the widget check",
                             intent_path=f"{ids.TASKS_ROOT}/{task_id}/TASK.md", task_id=task_id)
        pensieve.start_task(self.conn, task_id)
        return task_id

    def harry_task(self, enabled: bool = True) -> tuple:
        parent = self.parent_task()
        if enabled:
            self.enable("harry")
        self.write_owl("mcgonagall", "build.json", {"to": "harry", "kind": "request", "subject": "build it",
                                                    "body": "see TASK.md", "task_id": parent})
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned too early")):
            [delivered] = owl_post.run_pass(self.conn)["delivered"]
        owl = next(item for item in owlery.inbox(self.conn, "harry") if item["id"] == delivered["owl_id"])
        task = pensieve.get_task(self.conn, owlery.get_request(self.conn, owl["request_id"])["task_id"])
        return parent, task, owl["id"], delivered["doorbell"]

    def build(self, enabled: bool = True) -> tuple:
        parent, task, owl_id, _ = self.harry_task(enabled)
        with mock.patch.object(run_desk, "spawn") as spawn:
            created = worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        return parent, pensieve.get_task(self.conn, task["id"]), owl_id, created, spawn

    def handoff(self, task: dict, text: str) -> None:
        self.write_file(self.outbox("harry") / "handoff.md", text)
        self.write_owl("harry", "result.json", {
            "to": "mcgonagall", "kind": "result", "subject": "ready", "task_id": task["id"],
            "request_id": task["request_id"], "body_path": self.outbox_path("harry", "handoff.md")})
        owl_post.run_pass(self.conn)

    def fake_reviewer(self, verdict: str = "PASS", task_id: str = None, sha: str = None):
        """run_desk.run that writes the reviewer's output the way each family does."""
        def run(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            if on_start is not None:
                on_start()
            owl = next(item for item in owlery.inbox(conn, desk, include_acked=True) if item["id"] == owl_id)
            request = owlery.get_request(conn, owl["request_id"])
            author_task = pensieve.get_task(conn, request["parent_task_id"])
            record = gitops.find_record(worktree.castle_path(author_task["worktree"]))
            head = sha or gitops.rev(record)
            block = (f"Notes first.\nREVIEW {task_id or author_task['id']} @ {head}\nAC\nAC-1 PASS | ok\n"
                     f"BLOCKING\nNON-BLOCKING\nVERDICT: {verdict}\n")
            run_id = "run-" + "a" * 16
            folder = self.office / "runs" / desk
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if desk in config.HEADLESS_CODEX:
                self.write_file(folder / f"{run_id}-last-message.md", block)
            else:
                self.write_file(folder / f"{run_id}.out", claude_stream(block))
            return {"desk": desk, "run_id": run_id, "exit_code": 0}
        return mock.patch.object(run_desk, "run", side_effect=run)


def claude_stream(result: str, filler: str = "") -> str:
    """A Claude reviewer's .out the way run_desk's stream-json argv writes it: one event per line."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1", "tools": ["Read", "Grep"]},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the diff."}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": filler}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": result, "total_cost_usd": 0.1},
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


class WorktreeTests(LoopCase):
    def test_harry_waits_for_his_worktree_then_starts(self):
        _, task, owl_id, doorbell = self.harry_task()
        self.assertEqual(doorbell, "waiting for a worktree")
        with mock.patch.object(run_desk, "spawn") as spawn:
            created = worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        spawn.assert_called_once_with("harry", owl_id, hold_fd=mock.ANY)
        path = Path(created["worktree"])
        self.assertTrue((path / "README.md").is_file())
        self.assertEqual(created["branch"], "fix/widget")
        task = pensieve.get_task(self.conn, task["id"])
        self.assertEqual((task["status"], task["worktree"]), ("active", f"{ids.WORKTREES_ROOT}/{task['id']}"))
        self.assertEqual(owlery.get_request(self.conn, task["request_id"])["phase"], "running")
        record = gitops.read_record(task["id"])
        self.assertEqual((record["repo"], record["repo_dir"]), (REPO_ID, str(self.repo)))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path), "fix/widget")

    def test_a_disabled_desk_gets_its_worktree_but_no_run(self):
        _, task, _, _ = self.harry_task(enabled=False)
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            created = worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        self.assertIn("not enabled", created["desk"])

    def test_bad_requests_are_refused_before_anything_changes(self):
        parent, task, _, _ = self.harry_task()
        other = self.home_dir / "other"
        other.mkdir()
        cases = [
            (task["id"], str(self.repo), "harry/widget"),
            (task["id"], str(self.repo), "Fix/Widget"),
            (task["id"], str(self.repo), "fix/../widget"),
            (task["id"], str(other), "fix/widget"),
            (task["id"], str(self.castle), "fix/widget"),
            (task["id"], "/private/tmp", "fix/widget"),
            (parent, str(self.repo), "fix/widget"),
        ]
        for task_id, repo_dir, branch in cases:
            with self.subTest(repo=repo_dir, branch=branch), mock.patch.object(run_desk, "spawn"), \
                    self.assertRaises(FleetError):
                worktree.create(self.conn, task_id, repo_dir, branch, fetch=False)
        self.assertIsNone(pensieve.get_task(self.conn, task["id"])["worktree"])
        self.assertEqual(os.listdir(self.castle / "worktrees"), [])

    def test_an_existing_branch_is_refused(self):
        _, task, _, _ = self.harry_task()
        self.git("branch", "fix/widget")
        with mock.patch.object(run_desk, "spawn"), self.assertRaises(FleetError):
            worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)

    def test_a_record_that_points_elsewhere_is_refused(self):
        _, task, _, _, _ = self.build()
        path = self.office / "worktrees" / f"{task['id']}.json"
        data = json.loads(path.read_text())
        for key, value in (("common_dir", "/private/tmp/evil/.git"), ("path", "/private/tmp/x"),
                           ("git_dir", data["common_dir"] + "/worktrees/other")):
            with self.subTest(key=key):
                self.write_file(path, json.dumps({**data, key: value}))
                with self.assertRaises(FleetError):
                    gitops.read_record(task["id"])


class VerifyTests(LoopCase):
    def test_evidence_records_each_check_for_the_commit(self):
        parent, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / "widget.txt", "widget\n")
        with self.assertRaises(FleetError):
            verify.verify(self.conn, task["id"])
        self.git("add", "widget.txt", cwd=wt)
        self.git("commit", "-q", "-m", "widget", cwd=wt)
        result = verify.verify(self.conn, task["id"])
        sha = self.git("rev-parse", "HEAD", cwd=wt)
        self.assertEqual((result["sha"], result["checks"], result["failed"]), (sha, 3, []))
        text = (self.castle / "tasks" / parent / "evidence.md").read_text()
        self.assertTrue(text.startswith(f"EVIDENCE {task['id']} @ {sha}\n"))
        self.assertIn("\nCLEANED nothing: the worktree held no git-ignored files besides its dependency links\n", text)
        self.assertIn("AC-1 the readme is there\ncheck: `test -f README.md`\nexit: 0", text)
        self.assertIn("AC-3 the diff stays small\ncheck: one file changes\nnot run:", text)
        office = self.office / "reviews" / task["id"] / f"evidence-{sha}.md"
        self.assertEqual(office.read_text(), text)

    def test_ignored_files_are_removed_before_the_checks(self):
        parent, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / ".gitignore", "widget.txt\nbuild/\n")
        self.git("add", ".gitignore", cwd=wt)
        self.git("commit", "-q", "-m", "ignore rules", cwd=wt)
        self.write_file(wt / "widget.txt", "left behind, never committed\n")
        (wt / "build").mkdir()
        self.write_file(wt / "build" / "out.o", "object\n")
        result = verify.verify(self.conn, task["id"])
        self.assertEqual(result["failed"], ["AC-2"])
        self.assertFalse((wt / "widget.txt").exists())
        self.assertFalse((wt / "build").exists())
        text = (self.castle / "tasks" / parent / "evidence.md").read_text()
        self.assertIn("\nCLEANED 2 git-ignored paths before the checks, each as git names it:\n"
                      "    build/\n    widget.txt\nRAN ", text)

    def ignored_worktree(self, rules: str) -> tuple:
        parent, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / ".gitignore", rules)
        self.git("add", ".gitignore", cwd=wt)
        self.git("commit", "-q", "-m", "ignore rules", cwd=wt)
        return parent, task, wt

    def test_every_removed_path_is_named_in_full_and_escaped(self):
        parent, task, wt = self.ignored_worktree("*.log\n")
        long_name = "x" * 150 + ".log"
        names = [f"run-{n:02d}.log" for n in range(12)] + [long_name, "two\nlines.log"]
        for name in names:
            self.write_file(wt / name, "log\n")
        verify.verify(self.conn, task["id"])
        self.assertEqual([name for name in names if (wt / name).exists()], [])
        text = (self.castle / "tasks" / parent / "evidence.md").read_text()
        self.assertIn("\nCLEANED 14 git-ignored paths before the checks, each as git names it:\n", text)
        listed = text.split("each as git names it:\n", 1)[1].split("\nRAN ", 1)[0].splitlines()
        self.assertEqual(sorted(line.strip() for line in listed),
                         sorted([f"run-{n:02d}.log" for n in range(12)] + [long_name, '"two\\nlines.log"']))

    def test_too_many_ignored_paths_stop_verify_before_anything_is_removed(self):
        _, task, wt = self.ignored_worktree("*.log\n")
        for n in range(gitops.CLEAN_MAX_PATHS + 1):
            self.write_file(wt / f"run-{n:03d}.log", "log\n")
        with self.assertRaisesRegex(FleetError, "more than the evidence can list"):
            verify.verify(self.conn, task["id"])
        self.assertTrue((wt / "run-000.log").exists())

    def test_a_nested_repository_stop_verify_before_anything_is_removed(self):
        _, task, wt = self.ignored_worktree("vendor/\n*.log\n")
        (wt / "vendor" / "lib").mkdir(parents=True)
        self.git("init", "-q", cwd=wt / "vendor" / "lib")
        self.write_file(wt / "vendor" / "lib" / "index.js", "module.exports = 2\n")
        self.write_file(wt / "stray.log", "log\n")
        with self.assertRaisesRegex(FleetError, "nested git repository"):
            verify.verify(self.conn, task["id"])
        self.assertTrue((wt / "stray.log").exists())
        self.assertTrue((wt / "vendor" / "lib" / "index.js").exists())

    def test_a_failing_check_is_recorded_not_hidden(self):
        _, task, _, created, _ = self.build()
        result = verify.verify(self.conn, task["id"])
        self.assertEqual(result["failed"], ["AC-2"])

    def test_verify_builds_the_sandbox_profile(self):
        _, task, _, _, _ = self.build()
        record = gitops.read_record(task["id"])
        scratch = f"{self.user_temp}/hogwarts-verify-x"
        argv = REAL_SANDBOX_ARGV(record, scratch, "make test")
        self.assertEqual(argv[:2], [config.CODEX_BIN, "sandbox"])
        self.assertEqual(argv[argv.index("-P") + 1], "fleet-verify")
        self.assertEqual(argv[argv.index("-C") + 1], record["path"])
        self.assertEqual(argv[-5:], [config.BASH_BIN, "--noprofile", "--norc", "-c", "make test"])
        table = argv[argv.index("-c") + 1]
        self.assertIn(f'"{config.OFFICE_ROOT}"="deny"', table)
        self.assertIn('":workspace_roots"={"."="write"}', table)
        self.assertIn(f'"{record["common_dir"]}"="read"', table)
        self.assertIn(f'"{scratch}"="write"', table)
        self.assertIn(f'"{config.SHARED_TEMP_ROOT}"="deny"', table)
        self.assertIn(f'"{self.user_temp}/xcrun_db"="read"', table)
        self.assertNotIn(f'"{self.user_temp}"=', table)
        self.assertTrue(table.endswith("network={enabled=false}}"))
        self.assertNotIn("--sandbox", argv)

    def test_each_verify_run_gets_a_private_scratch_folder_that_is_removed(self):
        _, task, _, _, _ = self.build()
        seen = []
        real_check = verify.run_check

        def check(record, scratch, command, sandboxed=True, **kwargs):
            seen.append(scratch)
            self.assertTrue(os.path.isdir(f"{scratch}/home") and os.path.isdir(f"{scratch}/tmp"))
            return real_check(record, scratch, command, sandboxed, **kwargs)

        with mock.patch.object(verify, "run_check", side_effect=check):
            verify.verify(self.conn, task["id"])
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(seen[0].startswith(f"{self.user_temp}/hogwarts-verify-"))
        self.assertFalse(os.path.exists(seen[0]))

    def test_build_desk_checks_always_run_under_codex_sandbox(self):
        parent, task, _, _, _ = self.build()
        launched = []

        def fake_run(argv, cwd, env, out_fd, keep_fds=()):
            launched.append(argv)
            return 0

        with mock.patch.object(verify, "sandbox_argv", REAL_SANDBOX_ARGV), \
                mock.patch.object(verify, "run_command", side_effect=fake_run):
            result = verify.verify(self.conn, task["id"])
        self.assertEqual(result["checks"], 3)
        self.assertEqual(len(launched), 2)
        for argv in launched:
            self.assertEqual(argv[:2], [config.CODEX_BIN, "sandbox"])
            self.assertEqual(argv[argv.index("-P") + 1], "fleet-verify")
            self.assertIn(f'"{config.OFFICE_ROOT}"="deny"', argv[argv.index("-c") + 1])
        text = (self.castle / "tasks" / parent / "evidence.md").read_text()
        self.assertIn("under codex sandbox: worktree write, repo .git read, no network, no office", text)
        self.assertNotIn("without the Codex sandbox", text)

    def test_own_session_checks_run_without_the_codex_sandbox_and_say_so(self):
        self.write_file(self.repo / "fix.txt", "fix\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", "my own fix")
        self.enable("moody")
        built = []
        real = verify.sandbox_argv
        with mock.patch.object(verify, "sandbox_argv", side_effect=lambda *a: built.append(a) or real(*a)), \
                self.fake_reviewer("PASS"):
            result = review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False,
                                       intent="Fix it.\nAC-1 the fix file is there | check: `test -f fix.txt`")
        self.assertEqual(built, [])
        self.assertEqual(result["failed_checks"], [])
        text = (self.castle / "tasks" / result["task_id"] / "evidence.md").read_text()
        self.assertIn("without the Codex sandbox, because Ryan's own session wrote this code", text)
        self.assertIn("AC-1 the fix file is there\ncheck: `test -f fix.txt`\nexit: 0", text)
        task_md = (self.castle / "tasks" / result["task_id"] / "TASK.md").read_text()
        self.assertIn("## Intent\nFix it.\n\n## Acceptance criteria\nAC-1 the fix file is there", task_md)

    def test_parse_checks(self):
        checks = verify.parse_checks("AC-1 a | check: `x`\nnoise\nAC-12 b | check: look at it\nAC-x c | check: `y`\n")
        self.assertEqual([(c["id"], c["command"]) for c in checks], [("AC-1", "x"), ("AC-12", None)])


class ReviewTests(LoopCase):
    def test_a_codex_task_is_committed_reviewed_by_hermione_and_awaits_close(self):
        parent, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer("PASS"):
            result = review.review_build(self.conn, task["id"])
        sha = self.git("rev-parse", "HEAD", cwd=wt)
        self.assertEqual((result["verdict"], result["reviewer"], result["sha"]), ("PASS", "hermione", sha))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=wt), "Add the widget file")
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "awaiting_close")
        self.assertTrue(owlery.has_pass(self.conn, REPO_ID, sha))
        self.assertIn(f"REVIEW {task['id']} @ {sha}", (self.castle / "tasks" / parent / "review-latest.md").read_text())
        self.assertIn("COMMIT MESSAGE", (self.castle / "tasks" / parent / "handoff.md").read_text())
        reviewer_tasks = pensieve.list_tasks(self.conn, desk="hermione")
        self.assertEqual([(t["status"], t["close_reason"]) for t in reviewer_tasks], [("closed", "superseded")])
        mcgonagall_owls = owlery.inbox(self.conn, "mcgonagall")
        self.assertTrue(all(owl["read_at"] is None for owl in mcgonagall_owls))

    def test_fleet_words_in_a_commit_message_pass_only_in_the_kit_repo(self):
        _, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]).replace("Add the widget file", "Add Harry's widget file"))
        self.enable("hermione")
        with self.assertRaisesRegex(FleetError, "fleet word"):
            review.review_build(self.conn, task["id"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), self.git("rev-parse", "origin/main"))
        with mock.patch.object(config, "FLEET_WORDS_ALLOWED_REPOS", (REPO_ID,)), self.fake_reviewer("PASS"):
            result = review.review_build(self.conn, task["id"])
        self.assertEqual(self.git("log", "-1", "--format=%s", result["sha"], cwd=wt), "Add Harry's widget file")

    def test_changes_leaves_the_task_active_and_no_pass(self):
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer("CHANGES"):
            result = review.review_build(self.conn, task["id"])
        self.assertEqual(result["verdict"], "CHANGES")
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "active")
        self.assertFalse(owlery.has_pass(self.conn, REPO_ID, result["sha"]))

    def test_a_review_block_for_another_commit_records_nothing(self):
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer("PASS", sha="f" * 40), self.assertRaises(FleetError):
            review.review_build(self.conn, task["id"])
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "active")
        rows = self.conn.execute("SELECT COUNT(*) FROM review_passes").fetchone()[0]
        self.assertEqual(rows, 0)
        self.assertEqual([t["status"] for t in pensieve.list_tasks(self.conn, desk="hermione")], ["closed"])

    def test_no_review_runs_while_the_reviewer_is_disabled(self):
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("ran")), self.assertRaises(FleetError):
            review.review_build(self.conn, task["id"])

    def test_dirty_work_without_a_handoff_is_not_committed(self):
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.enable("hermione")
        with self.fake_reviewer("PASS"), self.assertRaises(FleetError):
            review.review_build(self.conn, task["id"])

    def test_own_session_commit_goes_to_moody_in_a_detached_worktree(self):
        self.write_file(self.repo / "fix.txt", "fix\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", "my own fix")
        sha = self.git("rev-parse", "HEAD")
        self.enable("moody")
        with self.fake_reviewer("PASS"):
            result = review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual((result["verdict"], result["reviewer"], result["sha"]), ("PASS", "moody", sha))
        task = pensieve.get_task(self.conn, result["task_id"])
        self.assertEqual((task["desk"], task["status"]), ("ryan-claude-1", "awaiting_close"))
        self.assertTrue(owlery.has_pass(self.conn, REPO_ID, sha))
        self.assertTrue((self.castle / "tasks" / task["id"] / "TASK.md").is_file())

    def test_own_session_fix_round_reuses_its_task(self):
        self.enable("moody")
        self.write_file(self.repo / "fix.txt", "fix\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", "my own fix")
        with self.fake_reviewer("CHANGES"):
            first = review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.write_file(self.repo / "fix.txt", "fixed\n")
        self.git("commit", "-q", "-am", "address review")
        with self.fake_reviewer("PASS"):
            second = review.review_own(self.conn, str(self.repo), task_id=first["task_id"], fetch=False)
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertNotEqual(second["sha"], first["sha"])
        self.assertTrue(owlery.has_pass(self.conn, REPO_ID, second["sha"]))
        self.assertFalse(owlery.has_pass(self.conn, REPO_ID, first["sha"]))


class ParsingTests(LoopCase):
    def test_review_block(self):
        task, sha = "tk_" + "1" * 16, "a" * 40
        text = f"REVIEW {task} @ {'b' * 40}\nVERDICT: PASS\nlater\nREVIEW {task} @ {sha}\nAC\nVERDICT: CHANGES\n"
        self.assertEqual(review.review_block(text, task, sha)[0], "CHANGES")
        for bad in ("no block here", f"REVIEW {task} @ {sha}\nno verdict\n", f"REVIEW {task} @ {'c' * 40}\nVERDICT: PASS\n"):
            with self.subTest(bad=bad[:20]), self.assertRaises(FleetError):
                review.review_block(bad, task, sha)

    def write_reviewer_out(self, text: str) -> None:
        folder = self.office / "runs" / "hermione"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.write_file(folder / ("run-" + "b" * 16 + ".out"), text)

    def test_reviewer_output_reads_the_claude_result_event_from_the_stream(self):
        self.write_reviewer_out("not json, a stray warning\n" + claude_stream("REVIEW text"))
        self.assertEqual(review.reviewer_output("hermione", "claude", "run-" + "b" * 16), "REVIEW text")

    def test_reviewer_output_reads_only_the_tail_of_a_long_stream(self):
        self.write_reviewer_out(claude_stream("REVIEW text", filler="x" * 5000))
        with mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", 1024):
            self.assertEqual(review.reviewer_output("hermione", "claude", "run-" + "b" * 16), "REVIEW text")

    def test_run_output_never_takes_a_result_the_read_window_cut_for_no_result(self):
        run_id = "run-" + "b" * 16
        self.write_reviewer_out(claude_stream("REVIEW text " + "x" * 5000))
        with mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", 1024):
            self.assertEqual(review.run_output("hermione", "claude", run_id), ("malformed", None))
            with self.assertRaisesRegex(FleetError, "too large to find its result event whole"):
                review.reviewer_output("hermione", "claude", run_id)
        self.assertEqual(review.run_output("hermione", "claude", run_id), ("ok", "REVIEW text " + "x" * 5000))

    def test_run_output_never_parses_a_line_the_read_window_cut_as_an_event(self):
        # The window starts inside a line whose end looks like a result event: that piece is never read as one.
        forged = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "forged"})
        tail = forged + "\n" + json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
        self.write_reviewer_out("WARNING " + tail)
        with mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", len(tail)):
            self.assertEqual(review.run_output("hermione", "claude", "run-" + "b" * 16), ("malformed", None))

    def test_reviewer_output_without_a_result_event_is_refused(self):
        self.write_reviewer_out(claude_stream("x").rsplit("\n", 2)[0] + "\n")
        with self.assertRaises(FleetError):
            review.reviewer_output("hermione", "claude", "run-" + "b" * 16)

    def test_commit_message(self):
        self.assertEqual(review.commit_message(HANDOFF.format(task_id="tk_x")),
                         ("Add the widget file", "It holds the widget."))
        for bad in ("no section", "COMMIT MESSAGE\n\nPR BODY DRAFT\n", "COMMIT MESSAGE\nFix it for Harry\n",
                    "COMMIT MESSAGE\n" + "x" * 101 + "\n"):
            with self.subTest(bad=bad[:24]), self.assertRaises(FleetError):
                review.commit_message(bad)


class RepeatReviewTests(LoopCase):
    """A build task's review is refused when HEAD and the desk's latest handoff are what the last verdict judged."""

    def changes_round(self) -> tuple:
        """A build task whose first review recorded CHANGES: (parent, task, worktree, first result)."""
        parent, task, _, created, _ = self.build()
        wt = Path(created["worktree"])
        self.write_file(wt / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer("CHANGES"):
            first = review.review_build(self.conn, task["id"])
        return parent, task, wt, first

    def hermione_requests(self) -> list:
        return [owl for owl in owlery.inbox(self.conn, "hermione", include_acked=True) if owl["kind"] == "request"]

    def next_handoff(self, task: dict, round_no: int, checkpoint: str = "done") -> None:
        """Harry's handoff for a later round, in an owl file of its own, so the store keeps it as a new owl."""
        text = HANDOFF.format(task_id=task["id"]).replace("round 1", f"round {round_no}").replace(
            "CHECKPOINT\ndone", f"CHECKPOINT\n{checkpoint}")
        self.write_file(self.outbox("harry") / f"handoff-r{round_no}.md", text)
        self.write_owl("harry", f"result-r{round_no}.json", {
            "to": "mcgonagall", "kind": "result", "subject": f"round {round_no} ready", "task_id": task["id"],
            "request_id": task["request_id"], "body_path": self.outbox_path("harry", f"handoff-r{round_no}.md")})
        owl_post.run_pass(self.conn)

    def test_unchanged_sha_and_handoff_open_no_round_and_say_why(self):
        parent, task, wt, first = self.changes_round()
        handoff_file = self.castle / "tasks" / parent / "handoff.md"
        os.unlink(handoff_file)
        requests = self.hermione_requests()
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")), \
                mock.patch.object(verify, "verify", side_effect=AssertionError("verify ran")):
            with self.assertRaisesRegex(review.Unchanged, f"nothing new to review: HEAD {first['sha'][:12]} and harry's"
                                        " latest handoff are what round 1 already judged \\(CHANGES\\)"):
                review.review_build(self.conn, task["id"])
        self.assertEqual([row["round"] for row in capacity.review_rounds(self.conn, task["id"])], [1])
        self.assertEqual(self.hermione_requests(), requests)
        self.assertFalse(handoff_file.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), first["sha"])

    def test_unchanged_sha_and_handoff_after_headmaster_is_refused_too(self):
        _, task, _, _ = self.changes_round()
        self.next_handoff(task, 2)
        with self.fake_reviewer("HEADMASTER"):
            second = review.review_build(self.conn, task["id"])
        with self.assertRaisesRegex(review.Unchanged, "what round 2 already judged \\(HEADMASTER\\)"):
            review.review_build(self.conn, task["id"])
        self.assertEqual(second["round"], 2)

    def test_new_handoff_or_sha_new_handoff_on_the_same_sha_opens_a_round(self):
        _, task, _, first = self.changes_round()
        self.next_handoff(task, 2, "the finding is wrong: the widget is already there")
        with self.fake_reviewer("PASS"):
            second = review.review_build(self.conn, task["id"])
        self.assertEqual((second["sha"], second["round"], second["verdict"]), (first["sha"], 2, "PASS"))

    def test_new_handoff_or_sha_new_sha_with_the_same_handoff_opens_a_round(self):
        _, task, wt, first = self.changes_round()
        self.write_file(wt / "widget.txt", "a better widget\n")
        with self.fake_reviewer("CHANGES"):
            second = review.review_build(self.conn, task["id"])
        self.assertNotEqual(second["sha"], first["sha"])
        self.assertEqual(second["round"], 2)

    def test_a_round_without_a_readable_record_never_blocks_a_review(self):
        _, task, _, first = self.changes_round()
        record = self.office / "reviews" / task["id"] / f"round-{first['request_id']}.json"
        task_md = (self.castle / "tasks" / task["parent_task_id"] / "TASK.md").read_bytes()
        self.assertEqual(json.loads(record.read_text()), {
            "request_id": first["request_id"], "sha": first["sha"],
            "handoff_sha256": review.handoff_digest(HANDOFF.format(task_id=task["id"])),
            "task_md_sha256": hashlib.sha256(task_md).hexdigest()})
        for broken in ("{not json", json.dumps({"request_id": first["request_id"], "sha": first["sha"]}), None):
            with self.subTest(broken=broken):
                if broken is None:
                    os.unlink(record)
                else:
                    self.write_file(record, broken)
                self.assertIsNone(review.round_inputs(task["id"], first["request_id"]))
        with self.fake_reviewer("CHANGES"):
            again = review.review_build(self.conn, task["id"])
        self.assertEqual((again["sha"], again["round"]), (first["sha"], 2))


class RoundRecordTests(LoopCase):
    changes_round = RepeatReviewTests.changes_round
    next_handoff = RepeatReviewTests.next_handoff

    def own_review(self, title: str = "my own fix", intent: str = None) -> dict:
        self.git("checkout", "-q", "-b", "feat/own")
        self.write_file(self.repo / "own.txt", "own\n")
        self.git("add", "own.txt")
        self.git("commit", "-q", "-m", "own")
        self.enable("moody")
        with self.fake_reviewer("PASS"):
            return review.review_own(self.conn, str(self.repo), title=title, intent=intent, fetch=False)

    def test_round_record_keeps_the_task_md_digest_for_build_and_own_rounds(self):
        parent, task, _, first = self.changes_round()
        build_md = (self.castle / "tasks" / parent / "TASK.md").read_bytes()
        state, data = review.round_record(task["id"], first["request_id"])
        self.assertEqual((state, data["task_md_sha256"]), ("ok", hashlib.sha256(build_md).hexdigest()))
        own = self.own_review()
        own_md = (self.castle / "tasks" / own["task_id"] / "TASK.md").read_bytes()
        state, data = review.round_record(own["task_id"], own["request_id"])
        self.assertEqual((state, data["sha"], data["handoff_sha256"], data["task_md_sha256"]),
                         ("ok", own["sha"], None, hashlib.sha256(own_md).hexdigest()))

    def test_round_record_without_a_digest_still_guards_an_unchanged_review(self):
        _, task, _, first = self.changes_round()
        record = self.office / "reviews" / task["id"] / f"round-{first['request_id']}.json"
        data = json.loads(record.read_text())
        del data["task_md_sha256"]
        self.write_file(record, json.dumps(data))
        self.assertEqual(review.round_inputs(task["id"], first["request_id"])["task_md_sha256"], None)
        self.assertEqual(review.round_record(task["id"], first["request_id"])[1]["task_md_sha256"], None)
        with self.assertRaises(review.Unchanged):
            review.review_build(self.conn, task["id"])

    def test_round_record_verify_keeps_the_task_md_in_the_office(self):
        parent, task, _, first = self.changes_round()
        raw = (self.castle / "tasks" / parent / "TASK.md").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        kept = self.office / "reviews" / task["id"] / f"task-md-{digest}.md"
        self.assertEqual(kept.read_bytes(), raw)
        self.write_file(kept, b"damaged")
        self.assertEqual(verify.verify(self.conn, task["id"])["task_md_sha256"], digest)
        self.assertEqual(kept.read_bytes(), raw)  # healed: the name is its content's hash

    def test_round_record_reader_tells_missing_unreadable_and_malformed_apart(self):
        _, task, _, first = self.changes_round()
        request = first["request_id"]
        record = self.office / "reviews" / task["id"] / f"round-{request}.json"
        good = record.read_text()
        self.assertEqual(review.round_record(task["id"], request)[0], "ok")
        for broken in ("{not json", json.dumps({"request_id": request}), json.dumps({**json.loads(good), "extra": 1}),
                       json.dumps({**json.loads(good), "task_md_sha256": "XYZ"}),
                       json.dumps({**json.loads(good), "task_md_sha256": None})):
            with self.subTest(broken=broken[:40]):
                self.write_file(record, broken)
                self.assertEqual(review.round_record(task["id"], request), ("malformed", None))
        self.write_file(record, good)
        os.chmod(record, 0o000)
        self.assertEqual(review.round_record(task["id"], request), ("unreadable", None))
        os.chmod(record, 0o600)
        os.unlink(record)
        self.assertEqual(review.round_record(task["id"], request), ("missing", None))
        os.symlink(self.tmp / "elsewhere.json", record)
        self.assertEqual(review.round_record(task["id"], request), ("malformed", None))

    def test_round_record_verify_that_cannot_keep_the_task_md_opens_no_round(self):
        parent, task, _, first = self.changes_round()
        self.next_handoff(task, 2)
        with mock.patch.object(verify, "keep_task_md", side_effect=OSError("disk full")), \
                self.fake_reviewer("PASS"):
            with self.assertRaises(OSError):
                review.review_build(self.conn, task["id"])
        self.assertEqual(len(capacity.review_rounds(self.conn, task["id"])), 1)

    def test_own_approval_file_is_written_once_with_the_frozen_task_md(self):
        own = self.own_review(intent="Do it.\nAC-1 it works | after merge: `true`\n")
        folder = self.office / "reviews" / own["task_id"]
        raw = (self.castle / "tasks" / own["task_id"] / "TASK.md").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        self.assertIn(b"## Acceptance criteria\nAC-1 it works | after merge: `true`\n", raw)
        self.assertEqual((folder / review.APPROVED_FILE).read_text(), digest + "\n")
        self.assertEqual((folder / f"task-md-{digest}.md").read_bytes(), raw)
        self.assertEqual(review.approved_digest(own["task_id"]), ("ok", digest))
        with self.assertRaisesRegex(FleetError, "could not be kept in the office"):
            review._write_own_task_md(own["task_id"], "again", "Something else.")
        self.assertEqual((folder / review.APPROVED_FILE).read_text(), digest + "\n")

    def test_own_approval_file_that_cannot_be_written_refuses_the_review_before_its_task_exists(self):
        self.git("checkout", "-q", "-b", "feat/own")
        self.write_file(self.repo / "own.txt", "own\n")
        self.git("add", "own.txt")
        self.git("commit", "-q", "-m", "own")
        self.enable("moody")
        before = len(pensieve.list_tasks(self.conn))
        with mock.patch.object(verify, "keep_task_md", side_effect=OSError("disk full")), \
                self.fake_reviewer("PASS"), self.assertRaisesRegex(FleetError, "could not be kept in the office"):
            review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        real = safefs.create_new

        def refused(fd, name, mode=0o600):
            if name == review.APPROVED_FILE:
                raise OSError("read-only")
            return real(fd, name, mode)

        with mock.patch.object(safefs, "create_new", side_effect=refused), self.fake_reviewer("PASS"), \
                self.assertRaisesRegex(FleetError, "could not be kept in the office"):
            review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        self.assertEqual(len(pensieve.list_tasks(self.conn)), before)
