"""On macOS a build's repo folder must be outside the home folders launchd jobs cannot read (Documents, Desktop,
Downloads, iCloud Drive): the go, fleet worktree and fleet adopt refuse one there with the reason and where to clone it,
fleet review own only warns, and nothing changes on any other platform."""
from __future__ import annotations

import os
import sys
from unittest import mock

from hogwarts import ids, owlery, pensieve
from tests.support import NOW

from fleet import adopt, config, gitops, review, run_desk, worktree
from fleet.hooks import user_prompt_submit
from fleet.safefs import FleetError
from tests_fleet.test_go import BRANCH, TASK_ID, GoCase


def on(platform: str):
    return mock.patch.object(sys, "platform", platform)


class ProtectedFolderTests(GoCase):
    def setUp(self) -> None:
        super().setUp()
        self.documents = self.home_dir / "Documents" / "web-app"
        self.documents.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", cwd=self.documents)

    def test_each_protected_folder_is_found_on_macos_only(self):
        root = str(self.home_dir)
        cases = ((f"{root}/Documents/web-app", "Documents"), (f"{root}/Desktop/a/b", "Desktop"),
                 (f"{root}/Downloads/x", "Downloads"), (f"{root}/Library/Mobile Documents/com~apple~CloudDocs/x",
                                                        "Library/Mobile Documents"),
                 (f"{root}/documents/web-app", "Documents"), (f"{root}/DocumentsX/web-app", None),
                 (f"{root}/fleet-repos/web-app", None), (f"{root}/code/Documents/web-app", None))
        for path, found in cases:
            with self.subTest(path=path):
                with on("darwin"):
                    self.assertEqual(gitops.protected_folder(path), found)
                with on("linux"):
                    self.assertIsNone(gitops.protected_folder(path))
                    self.assertEqual(gitops.check_unprotected(path), path)

    def test_a_go_for_a_repo_in_documents_is_refused_with_where_to_clone_it(self):
        self.task_md(spec=self.spec(repo=self.documents))
        self.enable("harry")
        before = self.snapshot()
        with on("darwin"), mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn(f"Go was not applied to {TASK_ID}: the Spec's repo: line is refused: the repo folder is inside"
                      " ~/Documents, which macOS keeps from the fleet's background jobs", shown)
        self.assertIn("for example ~/fleet-repos/web-app", shown)
        self.assert_unchanged(before)
        # Elsewhere the same Spec reads as before.
        with on("linux"):
            spec = user_prompt_submit.read_spec(self.task_md(spec=self.spec(repo=self.documents)), TASK_ID)
        self.assertEqual(spec["repo_dir"], str(self.documents))

    def test_the_protected_folder_refusal_keeps_its_own_text_and_adds_no_second_fix(self):
        with on("darwin"), self.assertRaises(user_prompt_submit.Refused) as raised:
            user_prompt_submit.read_spec(self.task_md(spec=self.spec(repo=self.documents)), TASK_ID)
        self.assertIsNone(raised.exception.fix)
        self.assertIn("clone the repo elsewhere in your home folder", str(raised.exception))
        with self.assertRaises(user_prompt_submit.Refused) as raised:
            user_prompt_submit.read_spec(self.task_md(spec=self.spec(repo=self.tmp / "missing")), TASK_ID)
        self.assertIn("outside ~/Documents", raised.exception.fix)

    def test_fleet_worktree_refuses_a_repo_in_a_protected_folder(self):
        self.task_md()
        pensieve.create_task(self.conn, "mcgonagall", "by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)
        child = owlery.open_request(self.conn, "mcgonagall", "harry", "build it", body="see TASK.md",
                                    parent_task_id=TASK_ID, now=NOW)["task"]
        with on("darwin"), mock.patch.object(run_desk, "spawn"), \
                self.assertRaisesRegex(FleetError, "inside ~/Documents.*~/fleet-repos/web-app"):
            worktree.create(self.conn, child["id"], str(self.documents), BRANCH, fetch=False)
        self.assertEqual(pensieve.get_task(self.conn, child["id"])["status"], "queued")
        self.assertEqual(os.listdir(self.castle / "worktrees"), [])

    def test_fleet_adopt_refuses_a_spec_whose_repo_is_in_a_protected_folder(self):
        self.task_md()
        pensieve.create_task(self.conn, "mcgonagall", "by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)
        child = owlery.open_request(self.conn, "mcgonagall", "harry", "build it", body="see TASK.md",
                                    parent_task_id=TASK_ID, now=NOW)["task"]
        with mock.patch.object(run_desk, "spawn"):
            worktree.create(self.conn, child["id"], str(self.repo), BRANCH, fetch=False)
        self.task_md(spec=self.spec(repo=self.documents))
        with on("darwin"), self.assertRaisesRegex(FleetError, "inside ~/Documents"):
            adopt.adopt(self.conn, TASK_ID, lambda prompt: TASK_ID)
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_review_own_is_not_refused_but_warns_that_auto_close_cannot_take_it(self):
        with mock.patch.object(review, "_review_own", return_value={"task_id": TASK_ID}):
            with on("darwin"):
                made = review.review_own(self.conn, str(self.documents), title="own work")
                plain = review.review_own(self.conn, str(self.repo), title="own work")
            with on("linux"):
                elsewhere = review.review_own(self.conn, str(self.documents), title="own work")
        self.assertIn("inside ~/Documents", made["warning"])
        self.assertIn("the closer cannot auto-close this task", made["warning"])
        self.assertNotIn("warning", plain)
        self.assertNotIn("warning", elsewhere)
        self.assertEqual(config.PROTECTED_HOME_DIRS,
                         ("Documents", "Desktop", "Downloads", "Library/Mobile Documents"))
