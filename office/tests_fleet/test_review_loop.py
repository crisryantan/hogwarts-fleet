"""The review loop end to end on real git repos in temp folders: worktree, verify, review.

Reviewer runs are faked at run_desk.run, so no model is ever called. verify's sandbox is
replaced with plain bash in these tests; test_verify_builds_the_sandbox_profile checks the
real argv without running it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from hogwarts import ids, owlery, pensieve

from fleet import config, gitops, owl_post, review, run_desk, verify, worktree
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
        def run(conn, desk, owl_id, mcp_job=None, now=None):
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
                self.write_file(folder / f"{run_id}.out", json.dumps({"type": "result", "result": block}))
            return {"desk": desk, "run_id": run_id, "exit_code": 0}
        return mock.patch.object(run_desk, "run", side_effect=run)


class WorktreeTests(LoopCase):
    def test_harry_waits_for_his_worktree_then_starts(self):
        _, task, owl_id, doorbell = self.harry_task()
        self.assertEqual(doorbell, "waiting for a worktree")
        with mock.patch.object(run_desk, "spawn") as spawn:
            created = worktree.create(self.conn, task["id"], str(self.repo), "fix/widget", fetch=False)
        spawn.assert_called_once_with("harry", owl_id)
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

    def test_a_nested_repository_stops_verify_before_anything_is_removed(self):
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
        argv = REAL_SANDBOX_ARGV(record, "/private/tmp/hogwarts-verify-x", "make test")
        self.assertEqual(argv[:2], [config.CODEX_BIN, "sandbox"])
        self.assertEqual(argv[argv.index("-P") + 1], "fleet-verify")
        self.assertEqual(argv[argv.index("-C") + 1], record["path"])
        self.assertEqual(argv[-5:], [config.BASH_BIN, "--noprofile", "--norc", "-c", "make test"])
        table = argv[argv.index("-c") + 1]
        self.assertIn(f'"{config.OFFICE_ROOT}"="deny"', table)
        self.assertIn('":workspace_roots"={"."="write"}', table)
        self.assertIn(f'"{record["common_dir"]}"="read"', table)
        self.assertIn('"/private/tmp/hogwarts-verify-x"="write"', table)
        self.assertTrue(table.endswith("network={enabled=false}}"))
        self.assertNotIn("--sandbox", argv)

    def test_build_desk_checks_always_run_under_codex_sandbox(self):
        parent, task, _, _, _ = self.build()
        launched = []
        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            if argv[0] == config.GIT_BIN:
                return real_run(argv, **kwargs)
            launched.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(verify, "sandbox_argv", REAL_SANDBOX_ARGV), \
                mock.patch.object(verify.subprocess, "run", side_effect=fake_run):
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

    def test_commit_message(self):
        self.assertEqual(review.commit_message(HANDOFF.format(task_id="tk_x")),
                         ("Add the widget file", "It holds the widget."))
        for bad in ("no section", "COMMIT MESSAGE\n\nPR BODY DRAFT\n", "COMMIT MESSAGE\nFix it for Harry\n",
                    "COMMIT MESSAGE\n" + "x" * 101 + "\n"):
            with self.subTest(bad=bad[:24]), self.assertRaises(FleetError):
                review.commit_message(bad)
