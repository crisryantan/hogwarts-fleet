"""fleet adopt: a build registered by hand gets the go spec the closer needs, once its TASK.md Spec matches the build's
worktree and the parent id is typed back, never otherwise and never twice."""
from __future__ import annotations

import hashlib
import io
import json
import re
from unittest import mock

from hogwarts import ids, owlery, pensieve
from tests.support import NOW

from fleet import adopt, closer, run_desk, tools, worktree
from fleet.safefs import FleetError
from tests_fleet.test_go import BRANCH, OTHER_ID, TASK_ID, GoCase


class AdoptTests(GoCase):
    def setUp(self) -> None:
        super().setUp()
        self.raw = self.task_md()
        pensieve.create_task(self.conn, "mcgonagall", "registered by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)

    def route(self, title: str = "build it") -> dict:
        return owlery.open_request(self.conn, "mcgonagall", "harry", title, body="see TASK.md",
                                   parent_task_id=TASK_ID, now=NOW)["task"]

    def built(self) -> dict:
        """Harry's task under the parent, given its worktree by fleet worktree from the TASK.md's own Spec."""
        child = self.route()
        with mock.patch.object(run_desk, "spawn"):
            worktree.create(self.conn, child["id"], str(self.repo), BRANCH, fetch=False)
        return child

    def adopt(self, answer: str = TASK_ID, task_id: str = TASK_ID) -> tuple:
        asked = []

        def confirm(prompt: str) -> str:
            asked.append(prompt)
            return answer + "\n"
        return adopt.adopt(self.conn, task_id, confirm), asked

    def test_a_matching_spec_typed_back_records_it_and_the_closer_takes_the_build(self):
        child = self.built()
        made, [asked] = self.adopt()
        spec = pensieve.task_spec(self.conn, TASK_ID)
        self.assertEqual((spec["repo_dir"], spec["branch"], spec["base"], spec["intent_sha256"]),
                         (str(self.repo), BRANCH, "origin/main", hashlib.sha256(self.raw).hexdigest()))
        for line in (f"parent: {TASK_ID} (queued) registered by hand", f"build:  {child['id']} (harry, active)",
                     f"repo:   {self.repo}", f"branch: {BRANCH}", "base:   origin/main",
                     "TASK.md title: Add the widget check", "Type the parent task id"):
            self.assertIn(line, asked)
        self.assertEqual((made["build_task_id"], made["branch"]), (child["id"], BRANCH))
        self.assertIn("the closer can now close", made["note"])
        self.assertIn("Any later edit to its TASK.md leaves it to be closed by hand", made["note"])
        printed = json.dumps(made) + asked
        self.assertIsNone(re.search(r"[0-9a-f]{64}", printed))
        # The closer's candidacy check on the parent now passes: it has its spec.
        task = pensieve.get_task(self.conn, child["id"])
        parent = pensieve.get_task(self.conn, task["parent_task_id"])
        self.assertEqual(parent["desk"], closer.TASK_DESK)
        self.assertIsNotNone(pensieve.task_spec(self.conn, parent["id"]))

    def test_a_spec_that_differs_from_the_worktree_is_refused_before_asking(self):
        self.built()
        self.task_md(spec=self.spec(branch="fix/other"))
        with self.assertRaisesRegex(FleetError, "the TASK.md Spec says .* branch fix/other, .* so nothing was recorded"):
            self.adopt(answer="never asked")
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_a_wrong_typed_id_records_nothing(self):
        self.built()
        for answer in ("", "yes", OTHER_ID, f"{TASK_ID} please"):
            with self.subTest(answer=answer), self.assertRaisesRegex(FleetError, "the typed id did not match"):
                self.adopt(answer=answer)
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_a_task_md_edited_while_asking_records_nothing(self):
        self.built()

        def edit_then_answer(prompt: str) -> str:
            self.write_file(self.castle / "tasks" / TASK_ID / "TASK.md", self.raw + b"\nAn edit.\n")
            return TASK_ID
        with self.assertRaisesRegex(FleetError, "TASK.md changed while you were reading it"):
            adopt.adopt(self.conn, TASK_ID, edit_then_answer)
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_a_parent_that_already_has_its_spec_is_refused(self):
        self.built()
        self.adopt()
        with self.assertRaisesRegex(FleetError, "already has its spec"):
            self.adopt()

    def test_two_open_build_tasks_or_none_are_refused_and_named(self):
        with self.assertRaisesRegex(FleetError, f"{TASK_ID} has no open build task under it"):
            self.adopt()
        first = self.built()
        second = self.route("again")
        with self.assertRaisesRegex(FleetError, f"has 2 open build tasks under it \\({first['id']}, {second['id']}\\)"):
            self.adopt()
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_a_build_with_no_worktree_or_a_parent_of_another_desk_is_refused(self):
        self.route()
        with self.assertRaisesRegex(FleetError, "has no worktree yet"):
            self.adopt()
        other = pensieve.create_task(self.conn, "hermione", "not hers", now=NOW)
        with self.assertRaisesRegex(FleetError, "is not a task of mcgonagall"):
            self.adopt(task_id=other["id"])

    def test_adopt_runs_only_with_a_terminal_and_has_no_yes(self):
        self.built()
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            tools.build_parser().parse_args(["adopt", TASK_ID, "--yes"])
        args = tools.build_parser().parse_args(["adopt", TASK_ID])
        piped = io.StringIO(TASK_ID + "\n")
        with mock.patch("sys.stdin", piped), mock.patch("sys.stdout", io.StringIO()), \
                self.assertRaisesRegex(FleetError, "runs only in your own terminal"):
            tools.run(self.conn, args)
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))
        with self.assertRaisesRegex(FleetError, "needs you to type the task id back"):
            adopt.adopt(self.conn, TASK_ID, None)
