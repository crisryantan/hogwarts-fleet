"""The push gate hook and the push script, on real git repos in temp folders.

origin keeps its GitHub URL, so the store sees the real repo id, but url.<bare>.insteadOf sends
every push to a local bare repo. Nothing in these tests reaches the network.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest import mock

from hogwarts import owlery, pensieve
from hogwarts.errors import StoreError

from fleet import common, config, push, review
from fleet.hooks import push_gate
from fleet.safefs import FleetError
from tests_fleet.test_review_loop import HANDOFF, ORIGIN, REPO_ID, LoopCase


class GateCase(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        self.bare = self.home_dir / "bare.git"
        self.git("init", "-q", "--bare", str(self.bare), cwd=self.home_dir)
        self.git("config", f"url.{self.bare}.insteadOf", ORIGIN)

    def grant_pass(self, sha: str) -> None:
        """A PASS from Moody on a commit from Ryan's own sessions, recorded the way the review script does."""
        task = pensieve.create_task(self.conn, "ryan-claude-1", "own work")
        pensieve.start_task(self.conn, task["id"])
        pensieve.record_commit(self.conn, task["id"], REPO_ID, sha)
        owlery.open_request(self.conn, "ryan-claude-1", "moody", "review", parent_task_id=task["id"])
        owlery.record_review(self.conn, REPO_ID, sha, task["id"], "moody", "PASS")
        pensieve.mark_awaiting_close(self.conn, task["id"])

    def gate(self, command, cwd=None) -> tuple:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or self.repo),
                   "hook_event_name": "PreToolUse", "session_id": "session-0001"}
        err = io.StringIO()
        code = push_gate.main(stdin=io.BytesIO(json.dumps(payload).encode()), stderr=err)
        return code, err.getvalue()

    def assert_allowed(self, command, cwd=None):
        code, err = self.gate(command, cwd)
        self.assertEqual((code, err), (0, ""), command)

    def assert_blocked(self, command, cwd=None, why=None):
        code, err = self.gate(command, cwd)
        self.assertEqual(code, push_gate.BLOCK_EXIT, command)
        self.assertTrue(err.startswith(push_gate.REASON_PREFIX), err)
        if why:
            self.assertIn(why, err)


class PushGateTests(GateCase):
    def test_commands_without_a_push_are_left_alone(self):
        for command in ("git status", "ls -la", "grep -r push src > out.txt", 'git commit -m "push the fix"',
                        "git log --oneline | head", "echo push"):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_a_push_without_a_pass_is_blocked(self):
        self.assert_blocked("git push", why="no review pass")
        self.assert_blocked("git push origin main", why="no review pass")

    def test_a_push_of_a_passed_commit_is_allowed(self):
        self.grant_pass(self.git("rev-parse", "HEAD"))
        for command in ("git push", "git push origin main", "git push -u origin HEAD:refs/heads/fix",
                        "git push origin main 2>&1", "RTK_DISABLED=1 git push", "rtk git push origin main",
                        f"cd {self.repo} && git push", f"git -C {self.repo} push origin main",
                        "git --no-pager push --porcelain origin main"):
            with self.subTest(command=command):
                self.assert_allowed(command, cwd=self.home_dir if "cd " in command or "-C " in command else None)

    def test_a_new_commit_needs_its_own_pass(self):
        self.grant_pass(self.git("rev-parse", "HEAD"))
        self.write_file(self.repo / "more.txt", "more\n")
        self.git("add", "more.txt")
        self.git("commit", "-q", "-m", "more")
        self.assert_blocked("git push", why="no review pass")

    def test_rewriting_deleting_and_unreadable_pushes_are_blocked_even_with_a_pass(self):
        self.grant_pass(self.git("rev-parse", "HEAD"))
        for command in ("git push --force", "git push -f origin main", "git push --force-with-lease",
                        "git push origin +main", "git push origin :main", "git push origin 'refs/heads/*'",
                        "git push --all", "git push --tags", "git push --mirror", "git push --no-verify",
                        "git push -o ci.skip", "git push --delete origin main",
                        "git push origin main && echo done", "echo hi; git push", "git push origin main\n",
                        'bash -c "git push"', "sh -c 'git push origin main'", "eval git push",
                        "git -c core.sshCommand=x push", "git push $(echo origin) main", "git push `echo origin`",
                        "git push origin main > /tmp/out", "cd /private/tmp && cd repo && git push",
                        'git pu""sh --force', f"git push {ORIGIN} main", "git push ../elsewhere main",
                        "git push & ", "git --git-dir=.git push", "env -S 'git push'"):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_a_remote_that_is_not_github_is_blocked(self):
        self.grant_pass(self.git("rev-parse", "HEAD"))
        self.git("remote", "add", "mirror", str(self.bare))
        self.assert_blocked("git push mirror main", why="not a plain GitHub repo")

    def test_a_detached_head_needs_a_named_ref(self):
        sha = self.git("rev-parse", "HEAD")
        self.grant_pass(sha)
        self.git("checkout", "-q", "--detach", sha)
        self.assert_blocked("git push", why="detached")
        self.assert_allowed("git push origin HEAD:refs/heads/fix")

    def test_the_gate_fails_closed(self):
        self.grant_pass(self.git("rev-parse", "HEAD"))
        with mock.patch.object(common, "connect", side_effect=StoreError("database is gone")):
            self.assert_blocked("git push", why="could not check")
        code = push_gate.main(stdin=io.BytesIO(b"{not json, but it says git push"), stderr=io.StringIO())
        self.assertEqual(code, push_gate.BLOCK_EXIT)
        self.assert_blocked("git push", cwd=self.home_dir / "missing")

    def test_unreadable_input_without_a_push_passes(self):
        self.assertEqual(push_gate.main(stdin=io.BytesIO(b"not json at all"), stderr=io.StringIO()), 0)


class PushScriptTests(GateCase):
    def reviewed(self, verdict: str = "PASS") -> tuple:
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer(verdict):
            result = review.review_build(self.conn, task["id"])
        return task, created, result

    def test_a_passed_task_pushes_exactly_its_reviewed_commit(self):
        task, _, result = self.reviewed()
        prompts = []
        pushed = push.push(self.conn, task["id"], confirm=lambda text: prompts.append(text) or "fix/widget\n")
        self.assertEqual((pushed["branch"], pushed["sha"]), ("fix/widget", result["sha"]))
        self.assertIn("Commit " + result["sha"], prompts[0])
        self.assertEqual(self.git("rev-parse", "refs/heads/fix/widget", cwd=self.bare), result["sha"])
        self.assertIn("gh pr create --draft", pushed["draft_pr_command"])
        self.assertIn('--title "Add the widget file"', pushed["draft_pr_command"])

    def test_nothing_is_pushed_without_the_typed_branch_name(self):
        task, _, _ = self.reviewed()
        with self.assertRaises(FleetError):
            push.push(self.conn, task["id"], confirm=lambda text: "yes\n")
        self.assertEqual(self.git("for-each-ref", cwd=self.bare), "")

    def test_no_pass_means_no_push(self):
        task, _, _ = self.reviewed("CHANGES")
        with self.assertRaises(FleetError):
            push.check(self.conn, task["id"])

    def test_a_dirty_worktree_or_a_fleet_word_blocks_the_push(self):
        task, created, _ = self.reviewed()
        self.write_file(Path(created["worktree"]) / "stray.txt", "stray\n")
        with self.assertRaises(FleetError):
            push.check(self.conn, task["id"])
        (Path(created["worktree"]) / "stray.txt").unlink()
        record_path = Path(created["worktree"])
        self.write_file(record_path / "notes.txt", "Hermione said this is fine\n")
        self.git("add", "notes.txt", cwd=record_path)
        self.git("commit", "-q", "-m", "notes", cwd=record_path)
        sha = self.git("rev-parse", "HEAD", cwd=record_path)
        pensieve.record_commit(self.conn, task["id"], REPO_ID, sha)
        owlery.open_request(self.conn, "harry", "hermione", "review again", parent_task_id=task["id"])
        owlery.record_review(self.conn, REPO_ID, sha, task["id"], "hermione", "PASS")
        with self.assertRaises(FleetError) as caught:
            push.check(self.conn, task["id"])
        self.assertIn("notes.txt:1 (hermione)", str(caught.exception))

    def test_the_kit_repo_may_carry_fleet_words(self):
        task, created, _ = self.reviewed()
        path = Path(created["worktree"])
        self.write_file(path / "notes.txt", "Hermione said this is fine\n")
        self.git("add", "notes.txt", cwd=path)
        self.git("commit", "-q", "-m", "notes for harry", cwd=path)
        sha = self.git("rev-parse", "HEAD", cwd=path)
        pensieve.record_commit(self.conn, task["id"], REPO_ID, sha)
        owlery.open_request(self.conn, "harry", "hermione", "review again", parent_task_id=task["id"])
        owlery.record_review(self.conn, REPO_ID, sha, task["id"], "hermione", "PASS")
        with self.assertRaises(FleetError):
            push.check(self.conn, task["id"])
        with mock.patch.object(config, "FLEET_WORDS_ALLOWED_REPOS", (REPO_ID,)):
            self.assertEqual(push.check(self.conn, task["id"])["sha"], sha)

    def test_own_session_work_is_pushed_by_hand(self):
        self.enable("moody")
        self.write_file(self.repo / "fix.txt", "fix\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", "my fix")
        with self.fake_reviewer("PASS"):
            result = review.review_own(self.conn, str(self.repo), title="my fix", fetch=False)
        with self.assertRaises(FleetError):
            push.check(self.conn, result["task_id"])
        self.assert_allowed("git push origin main")
