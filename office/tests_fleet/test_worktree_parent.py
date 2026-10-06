"""fleet worktree given the parent id: McGonagall's TASK.md carries her own task's id, so the command takes it and uses
the one open build task under it, saying so, and refuses with zero or several, naming them."""
from __future__ import annotations

import os
from unittest import mock

from hogwarts import ids, owlery, pensieve
from tests.support import NOW

from fleet import config, gitops, run_desk, tools, worktree
from fleet.safefs import FleetError
from tests_fleet.test_go import BRANCH, TASK_ID, GoCase


class WorktreeParentTests(GoCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        pensieve.create_task(self.conn, "mcgonagall", "registered by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)

    def route(self, title: str = "build it") -> dict:
        return owlery.open_request(self.conn, "mcgonagall", "harry", title, body="see TASK.md",
                                   parent_task_id=TASK_ID, now=NOW)["task"]

    def create(self, task_id: str = TASK_ID) -> dict:
        with mock.patch.object(run_desk, "spawn"):
            return worktree.create(self.conn, task_id, str(self.repo), BRANCH, fetch=False)

    def test_the_parent_id_uses_its_one_open_build_task_and_says_so(self):
        child = self.route()
        made = self.create()
        self.assertEqual(made["task_id"], child["id"])
        self.assertEqual(made["used"], f"used Harry's task {child['id']} under {TASK_ID}")
        task = pensieve.get_task(self.conn, child["id"])
        self.assertEqual((task["status"], task["worktree"]), ("active", f"{ids.WORKTREES_ROOT}/{child['id']}"))
        self.assertEqual(gitops.read_record(child["id"])["branch"], BRANCH)
        self.assertEqual(pensieve.get_task(self.conn, TASK_ID)["worktree"], None)

    def test_a_build_task_id_works_as_before_with_nothing_said(self):
        child = self.route()
        made = self.create(child["id"])
        self.assertEqual(made["task_id"], child["id"])
        self.assertNotIn("used", made)

    def test_a_closed_build_task_under_the_parent_does_not_count(self):
        old = self.route("first try")
        pensieve.close_task(self.conn, old["id"], "abandoned")
        child = self.route()
        self.assertEqual(self.create()["task_id"], child["id"])

    def test_no_open_build_task_under_the_parent_is_refused(self):
        with self.assertRaisesRegex(FleetError, f"^only a build desk's task gets a worktree from this script, and"
                                                f" {TASK_ID} has no open build task under it$"):
            self.create()
        self.assertEqual(os.listdir(self.castle / "worktrees"), [])

    def test_several_open_build_tasks_under_the_parent_are_refused_and_named(self):
        first, second = self.route("one"), self.route("two")
        with self.assertRaises(FleetError) as refused:
            self.create()
        self.assertIn(f"{TASK_ID} has 2 open build tasks under it ({first['id']}, {second['id']}); name the one to use",
                      str(refused.exception))
        self.assertEqual(os.listdir(self.castle / "worktrees"), [])
        for task in (first, second):
            self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "queued")

    def test_every_other_check_still_applies_to_the_build_task_it_uses(self):
        child = self.route()
        self.git("branch", BRANCH)
        with self.assertRaisesRegex(FleetError, "that branch already exists"):
            self.create()
        self.assertEqual(pensieve.get_task(self.conn, child["id"])["status"], "queued")
        self.assertFalse(os.path.lexists(config.worktree_dir(child["id"])))

    def test_the_command_prints_which_task_it_used(self):
        child = self.route()
        args = tools.build_parser().parse_args(["worktree", TASK_ID, "--repo-dir", str(self.repo), "--branch", BRANCH,
                                                "--no-fetch"])
        with mock.patch.object(run_desk, "spawn"):
            made = tools.run(self.conn, args)
        self.assertEqual(made["used"], f"used Harry's task {child['id']} under {TASK_ID}")
