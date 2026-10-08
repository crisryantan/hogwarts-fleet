from __future__ import annotations

import unittest
from types import SimpleNamespace

from hogwarts import capacity, owlery, pensieve, views
from tests.support import NOW, SHA, StoreCase, go_build, intent_file

CAPS = SimpleNamespace(RUNNING_WINDOW_SECONDS=3600, REVIEW_ROUND_CAP=3, FOLLOWUP_ROUND_CAP=2, WORKTREE_DESKS=("beta",))


class TaskListTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def lines(self, **kwargs) -> list:
        return views.task_lines(self.conn, NOW, CAPS, **kwargs)

    def test_it_lists_open_tasks_newest_first_with_who_they_wait_on(self):
        old = self.task("alpha", "the older job", now=NOW)
        new = self.task("beta", "the newer job", now=NOW + 5)
        done = self.task("alpha", "already closed", now=NOW + 9)
        pensieve.close_task(self.conn, done["id"], "abandoned", now=NOW + 10)
        pensieve.start_task(self.conn, old["id"], now=NOW + 1)
        pensieve.mark_awaiting_close(self.conn, old["id"], now=NOW + 2)
        header, first, second = self.lines()
        self.assertTrue(header.startswith("task") and header.endswith("waiting on"))
        self.assertTrue(first.startswith(new["id"]), first)
        self.assertTrue(second.startswith(old["id"]), second)
        self.assertIn("queued", first)
        self.assertTrue(first.endswith("beta"))  # a queued task waits on its desk
        self.assertIn("awaiting close", second)
        self.assertTrue(second.endswith("you"))  # a task awaiting close waits on Ryan
        self.assertNotIn(done["id"], "\n".join(self.lines()))

    def test_a_desk_filter_a_long_title_and_nothing_open(self):
        self.assertEqual(self.lines(), ["no open tasks"])
        self.task("alpha", "x" * 120)
        self.task("beta", "other desk")
        [_, line] = self.lines(desk="alpha")
        self.assertIn("...", line)
        self.assertNotIn("x" * 60, line)

    def test_it_is_capped_to_one_screen_with_a_count_of_the_rest(self):
        for index in range(views.LIST_CAP + 3):
            self.task("alpha", f"job {index}", now=NOW + index)
        lines = self.lines()
        self.assertEqual(len(lines), 1 + views.LIST_CAP + 1)
        self.assertIn("3 more open", lines[-1])
        self.assertIn("job 32", lines[1])  # newest first


class BuildLinesTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.desk("gamma")

    def lines(self, **kwargs) -> list:
        return views.build_lines(self.conn, NOW, CAPS, **kwargs)

    def second_build(self) -> dict:
        parent = "tk_00000000000000b1"
        pensieve.create_task(self.conn, "gamma", "second go", intent_path=intent_file(parent), task_id=parent, now=NOW)
        pensieve.record_spec(self.conn, parent, "/private/tmp/checkout", "fix/other", "origin/main", "d" * 64, now=NOW)
        build = owlery.open_request(self.conn, "gamma", "beta", "build two", parent_task_id=parent, now=NOW)["task"]["id"]
        return {"parent": parent, "build": build}

    def test_one_line_per_build_goes_go_to_child_to_branch_to_state(self):
        built = go_build(self.conn)
        self.assertEqual(self.lines(), [f"{built['parent']} -> {built['build']} -> fix/site -> awaiting close"
                                        f" | sha {SHA[:12]}"])
        self.assertEqual(self.lines(), views.build_lines(self.conn, NOW, CAPS, False))

    def test_closed_builds_show_only_with_all_and_a_build_with_no_spec_shows_a_dash(self):
        built = go_build(self.conn)
        token = owlery.mint(self.conn, built["build"], "cli", now=NOW)["token"]
        pensieve.close_task(self.conn, built["build"], "abandoned", token, now=NOW)
        self.assertEqual(self.lines(), ["no open builds"])
        self.assertIn("closed abandoned", self.lines(include_closed=True)[0])
        plain = pensieve.create_task(self.conn, "gamma", "by hand", now=NOW)
        child = owlery.open_request(self.conn, "gamma", "beta", "hand build", parent_task_id=plain["id"],
                                    now=NOW)["task"]["id"]
        self.assertTrue(self.lines()[0].startswith(f"{plain['id']} -> {child} -> - -> queued"))

    def test_a_sha_that_shows_on_two_builds_is_labelled_as_their_base(self):
        first = go_build(self.conn)
        second = self.second_build()
        pensieve.start_task(self.conn, second["build"], now=NOW)
        capacity.open_review_round(self.conn, second["build"], "alpha", SHA, "review two", now=NOW)
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(f"base sha {SHA[:12]}", line)
        self.assertTrue(lines[0].startswith(second["parent"]))  # newest first
        self.assertIn("fix/other", lines[0])
        rounds = views.rounds_with_branch(self.conn, first["build"], CAPS)
        self.assertEqual([row["branch"] for row in rounds], ["fix/site"])
        self.assertIn(second["build"], rounds[0]["sha_note"])

    def test_the_cap_cuts_the_default_view_and_no_cap_lists_every_build(self):
        go_build(self.conn)
        self.second_build()
        capped = self.lines(cap=1)
        self.assertEqual(len(capped), 2)
        self.assertIn("1 more builds (castle task builds --all lists every one)", capped[-1])
        self.assertEqual(len(self.lines(cap=None)), 2)
        self.assertNotIn("more builds", "\n".join(self.lines(cap=None)))

    def test_a_sha_on_one_build_only_is_not_called_a_base(self):
        first = go_build(self.conn)
        rounds = views.rounds_with_branch(self.conn, first["build"], CAPS)
        self.assertIsNone(rounds[0]["sha_note"])
        self.assertNotIn("base sha", self.lines()[0])


if __name__ == "__main__":
    unittest.main()
