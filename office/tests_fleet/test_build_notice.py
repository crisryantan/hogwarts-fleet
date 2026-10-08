"""A repo in ~/Documents (or Desktop, Downloads, iCloud Drive) is never refused: the go, fleet worktree and fleet adopt
apply to it. What they say instead is whether the background jobs a build needs run: start fleet loops when they run
nowhere, a warning when launchd alone runs them on a folder launchd cannot read, and nothing while fleet loops runs
them."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from unittest import mock

from hogwarts import ids, owlery, pensieve
from tests.support import NOW

from fleet import adopt, config, review, run_desk, safefs, tools, worktree
from fleet.hooks import user_prompt_submit
from tests_fleet.test_go import BRANCH, TASK_ID, GoCase

NOT_RUNNING = "Start `fleet loops`, the background jobs are not running, so the build will not move on by itself"


def on(platform: str):
    return mock.patch.object(sys, "platform", platform)


class BuildNoticeTests(GoCase):
    def setUp(self) -> None:
        super().setUp()
        self.documents = self.home_dir / "Documents" / "web-app"
        self.documents.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", cwd=self.documents)
        self.agents = self.home_dir / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True, mode=0o700)

    def in_launchd(self) -> None:
        for job in config.BUILD_JOBS:
            self.write_file(self.agents / f"com.hogwarts.{job}.plist", "plist")

    def loops_running(self):
        """A live fleet loops running both jobs: its marker, and its lock held from another open file."""
        (self.office / "loops").mkdir(mode=0o700, exist_ok=True)
        (self.office / "locks").mkdir(mode=0o700, exist_ok=True)
        self.write_file(self.office / "loops" / "running.json",
                        json.dumps({"pid": os.getpid(), "jobs": list(config.BUILD_JOBS), "started": 1}))
        stack = contextlib.ExitStack()
        fd = stack.enter_context(safefs.opened_dir(str(self.office), "locks"))
        stack.enter_context(safefs.held_lock(fd, config.LOOPS_LOCK, blocking=False))
        self.addCleanup(stack.close)

    def repo_in_documents(self) -> None:
        """The test's own checkout, with its GitHub origin, moved under ~/Documents."""
        target = self.home_dir / "Documents" / "repo"
        os.rename(self.repo, target)
        self.repo = target

    def test_a_go_for_a_repo_in_documents_applies_and_says_the_jobs_are_not_running(self):
        self.repo_in_documents()
        self.task_md()
        with on("darwin"):
            shown, context, _, spawn = self.go_ok()
        self.assertIn(NOT_RUNNING, shown)
        self.assertIn(NOT_RUNNING, context)
        self.assertIsNotNone(self.harry_task()["worktree"])
        spawn.assert_called_once()

    def test_under_launchd_alone_a_go_in_documents_applies_with_a_warning(self):
        self.repo_in_documents()
        self.in_launchd()
        self.task_md()
        with on("darwin"):
            shown, _, _, _ = self.go_ok()
        self.assertIn("Warning: the repo is inside ~/Documents, which macOS keeps launchd jobs out of", shown)
        self.assertNotIn(NOT_RUNNING, shown)

    def test_under_live_loops_a_go_in_documents_says_nothing_more(self):
        self.repo_in_documents()
        self.loops_running()
        self.task_md()
        with on("darwin"):
            shown, _, _, _ = self.go_ok()
        self.assertNotIn("fleet loops", shown)
        self.assertNotIn("Warning", shown)

    def test_a_repo_line_refusal_carries_a_fix_line_and_no_folder_advice(self):
        with self.assertRaises(user_prompt_submit.Refused) as raised:
            user_prompt_submit.read_spec(self.task_md(spec=self.spec(repo=self.tmp / "missing")), TASK_ID)
        self.assertIn("in your home folder, outside the office and the castle", raised.exception.fix)
        self.assertNotIn("Documents", raised.exception.fix)
        with on("darwin"):
            spec = user_prompt_submit.read_spec(self.task_md(spec=self.spec(repo=self.documents)), TASK_ID)
        self.assertEqual(spec["repo_dir"], str(self.documents))

    def child(self) -> dict:
        self.task_md()
        pensieve.create_task(self.conn, "mcgonagall", "by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)
        return owlery.open_request(self.conn, "mcgonagall", "harry", "build it", body="see TASK.md",
                                   parent_task_id=TASK_ID, now=NOW)["task"]

    def test_fleet_worktree_makes_a_worktree_in_documents_and_warns(self):
        self.repo_in_documents()
        child = self.child()
        args = tools.build_parser().parse_args(["worktree", child["id"], "--repo-dir", str(self.repo), "--branch",
                                                BRANCH, "--no-fetch"])
        with on("darwin"), mock.patch.object(run_desk, "spawn"):
            made = tools.run(self.conn, args)
        self.assertIn(NOT_RUNNING, made["warning"])
        self.assertIsNotNone(pensieve.get_task(self.conn, child["id"])["worktree"])

    def test_fleet_adopt_takes_a_spec_whose_repo_is_in_documents(self):
        self.repo_in_documents()
        child = self.child()
        with mock.patch.object(run_desk, "spawn"):
            worktree.create(self.conn, child["id"], str(self.repo), BRANCH, fetch=False)
        with on("darwin"):
            adopt.adopt(self.conn, TASK_ID, lambda prompt: TASK_ID)
        self.assertEqual(pensieve.task_spec(self.conn, TASK_ID)["repo_dir"], str(self.repo))

    def test_review_own_warns_only_when_the_jobs_cannot_take_its_task(self):
        with mock.patch.object(review, "_review_own", return_value={"task_id": TASK_ID}):
            with on("darwin"):
                nowhere = review.review_own(self.conn, str(self.repo), title="own work")
                self.in_launchd()
                blind = review.review_own(self.conn, str(self.documents), title="own work")
                plain = review.review_own(self.conn, str(self.repo), title="own work")
            with on("linux"):
                elsewhere = review.review_own(self.conn, str(self.documents), title="own work")
        self.assertIn(NOT_RUNNING, nowhere["warning"])
        self.assertIn("inside ~/Documents", blind["warning"])
        self.assertNotIn("warning", plain)
        self.assertNotIn("warning", elsewhere)
