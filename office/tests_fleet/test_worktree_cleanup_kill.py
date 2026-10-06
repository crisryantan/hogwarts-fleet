"""Worktree cleanup: kills during a removal, leftover markers, fleet worktree remove by hand, and the Map round's
sweep.

Split from test_worktree_cleanup.py so the parallel runner can run it beside the rest; it shares CleanupCase.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from hogwarts import pensieve

from fleet import config, gitops, map as patrol_map, run_desk, safefs, worktree
from fleet.safefs import FleetError
from tests_fleet.test_auto_close import MapRoundCase, tearDownModule  # noqa: F401
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_worktree_cleanup import DAYS, CleanupCase, GitSpy


class KillTests(CleanupCase):
    def test_kill_at_each_step_of_a_removal_finishes_it_on_a_later_round_and_tells_it_once(self):
        for step in ("before git", "after the folder", "after git"):
            with self.subTest(step=step):
                self.switch_on()
                branch = f"fix/{step.replace(' ', '-')}"
                ctx = self.closed_build(branch=branch)
                with self.kill_at_remove(after_folder=step == "after the folder", after_git=step == "after git"), \
                        self.assertRaises(Killed):
                    self.sweep(ctx)
                self.assertTrue(self.marker(ctx["task"], "removing").exists())
                self.assertEqual(self.listed(ctx["wt"]), step != "after git")
                result = self.sweep(ctx, after=3 * DAYS + 900)
                self.assertEqual((result["removed"], result["kept"]), (1, 0))
                self.assert_removed(ctx, branch)
                [row] = [event for event in self.cleanup_events("worktree.removed") if ctx["task"] in event["summary"]]
                self.assertIn("removed 1 worktree of closed tasks", row["summary"])
        self.assertEqual(self.cleanup_events("worktree.kept"), [])

    def test_kill_after_its_marker_finishes_a_removal_whose_head_was_checked(self):
        self.switch_on()
        ctx = self.closed_build()
        with self.kill_at_remove(), self.assertRaises(Killed):
            self.sweep(ctx)
        with mock.patch.object(gitops, "fetch_branch", side_effect=AssertionError("fetched again")):
            self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["removed"], 1)
        self.assert_removed(ctx)

    def test_kill_part_way_through_the_folder_is_told_once_and_never_forced(self):
        self.switch_on()
        ctx = self.closed_build()

        def part(args):
            if args[:2] == ["worktree", "remove"]:
                os.unlink(Path(args[2]) / "widget.txt")
                raise Killed()
        with mock.patch.object(gitops, "git", side_effect=GitSpy(before=part)), self.assertRaises(Killed):
            self.sweep(ctx)
        spy = GitSpy()
        with mock.patch.object(gitops, "git", side_effect=spy):
            for offset in (900, 1800):
                self.assertEqual(self.sweep(ctx, after=3 * DAYS + offset)["kept"], 1)
        self.assert_kept(ctx, "uncommitted changes (a removal was cut short part way")
        self.assertTrue((ctx["wt"] / "README.md").is_file())
        self.assertFalse(any("--force" in call or "prune" in call for call in spy.calls))

    def test_kill_around_the_round_row_never_loses_or_repeats_it(self):
        self.switch_on()
        first = self.closed_build()
        real = pensieve.add_event

        def die_before(conn, desk, kind, *args, **kwargs):
            if kind == worktree.REMOVED_KIND:
                raise Killed()
            return real(conn, desk, kind, *args, **kwargs)
        with mock.patch.object(pensieve, "add_event", side_effect=die_before), self.assertRaises(Killed):
            self.sweep(first)
        self.assert_removed(first)
        self.assertEqual(self.cleanup_events(), [])
        second = self.closed_build(branch="fix/gadget", at=self.t0 + 900)
        real_drop = worktree._drop_marker

        def die_after_the_row(task_id, kind):
            if kind == "unreported":
                raise Killed()
            return real_drop(task_id, kind)
        with mock.patch.object(worktree, "_drop_marker", side_effect=die_after_the_row), self.assertRaises(Killed):
            self.sweep(second)
        self.assert_removed(second, "fix/gadget")
        [row] = self.cleanup_events("worktree.removed")  # the killed round's batch, told before this kill
        self.assertIn(first["task"], row["summary"])
        self.assertNotIn(second["task"], row["summary"])
        self.assertEqual(self.sweep(second, after=3 * DAYS + 900)["reported"], 2)
        rows = self.cleanup_events("worktree.removed")
        self.assertEqual(len(rows), 2)  # the first is never told again
        self.assertIn(second["task"], rows[1]["summary"])
        self.assertNotIn(first["task"], rows[1]["summary"])
        self.assertEqual(worktree.markers("unreported"), [])

    def test_a_cut_short_removal_is_finished_only_under_the_switch_that_started_it(self):
        self.switch_on()
        swept = self.closed_build()
        with self.kill_at_remove(), self.assertRaises(Killed):
            self.sweep(swept)
        self.switch_off()  # auto-close stays on, and never finishes the sweep's removal
        for offset in (900, 1800):
            self.assertEqual(self.sweep(swept, after=3 * DAYS + offset)["kept"], 1)
        self.assert_kept(swept, "its removal was cut short and the switch that started it is off now")
        self.switch_on()
        self.assertEqual(self.sweep(swept, after=3 * DAYS + 2700)["removed"], 1)
        self.assert_removed(swept)

        ctx = self.passed_build(branch="fix/gadget")
        self.land_pr(ctx)
        with self.kill_at_remove(), self.assertRaises(Killed):
            self.close(ctx)
        self.assertEqual(json.loads(self.marker(ctx["task"], "removing").read_text())["by"], "closer")
        self.opt_out()  # the cleanup switch is on, and never finishes the closer's removal
        self.assertEqual(self.sweep(at=self.t0 + 3 * DAYS + 3600)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir())
        self.opt_in()
        self.assertEqual(self.sweep(at=self.t0 + 3 * DAYS + 4500)["removed"], 1)
        self.assert_removed(ctx, "fix/gadget")
        self.assertEqual(len([event for event in self.cleanup_events("worktree.removed")
                              if ctx["task"] in event["summary"]]), 0)  # its close event already said so


class MarkerTests(CleanupCase):
    def kill_finish(self, kind: str):
        """Kill the removal as its finish drops the marker named kind."""
        real = worktree._drop_marker

        def drop(task_id, dropped):
            if dropped == kind:
                raise Killed()
            return real(task_id, dropped)
        return mock.patch.object(worktree, "_drop_marker", side_effect=drop)

    def test_leftover_markers_of_a_finished_removal_never_warn_and_are_settled(self):
        self.switch_on()
        ctx = self.closed_build()
        with self.kill_finish("removing"), self.assertRaises(Killed):
            self.sweep(ctx)
        self.assertTrue(self.marker(ctx["task"], "removed").exists())
        self.assertTrue(self.marker(ctx["task"], "removing").exists())
        self.switch_off()  # the switch that started it is off now, and it is done anyway
        result = self.sweep(ctx, after=3 * DAYS + 900)
        self.assertEqual((result["kept"], result["reported"]), (0, 1))
        self.assert_removed(ctx)
        self.assertEqual(self.cleanup_events("worktree.kept"), [])
        self.assertEqual(len(self.removed_rows(ctx["task"])), 1)

    def test_the_already_removed_path_settles_leftover_markers(self):
        self.switch_on()
        ctx = self.closed_build()
        with self.kill_finish("removing"), self.assertRaises(Killed):
            self.sweep(ctx)
        with self.assertRaisesRegex(FleetError, "removed already"):
            worktree.remove(self.conn, ctx["task"])
        self.assertFalse(self.marker(ctx["task"], "removing").exists())
        self.switch_off()
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["kept"], 0)
        self.assertEqual(self.cleanup_events("worktree.kept"), [])
        self.assertEqual(len(self.removed_rows(ctx["task"])), 1)

    def fail_once(self, kind: str):
        """The first write of the marker named kind fails."""
        real, failed = worktree._write_marker, []

        def write(task_id, written, data):
            if written == kind and not failed:
                failed.append(written)
                raise OSError("No space left on device")
            return real(task_id, written, data)
        return mock.patch.object(worktree, "_write_marker", side_effect=write)

    def test_a_removal_is_told_once_whichever_finish_marker_fails_to_write(self):
        for kind in ("removed", "unreported"):
            with self.subTest(failing=kind):
                self.switch_on()
                ctx = self.closed_build(branch=f"fix/{kind}")
                with self.fail_once(kind):
                    self.sweep(ctx)
                self.assertFalse(os.path.lexists(ctx["wt"]))
                for offset in (900, 1800):
                    self.sweep(ctx, after=3 * DAYS + offset)
                self.assert_removed(ctx, f"fix/{kind}")
                self.assertEqual(len(self.removed_rows(ctx["task"])), 1)
                self.assertEqual(worktree.markers("unreported"), [])
                removal = json.loads(self.marker(ctx["task"], "removed").read_text())
                self.assertIsNotNone(removal["reported"])

    def test_a_removal_told_before_its_removing_marker_went_is_never_told_again(self):
        self.switch_on()
        ctx = self.closed_build()
        real, failed = worktree._drop_marker, []

        def drop(task_id, kind):
            if kind == "removing" and not failed:
                failed.append(kind)
                raise OSError("Operation not permitted")
            return real(task_id, kind)
        with mock.patch.object(worktree, "_drop_marker", side_effect=drop):
            self.assertEqual(self.sweep(ctx)["reported"], 1)  # its round tells it, with removing still there
        self.assertTrue(self.marker(ctx["task"], "removing").exists())
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["reported"], 0)
        self.assert_removed(ctx)
        self.assertEqual(len(self.removed_rows(ctx["task"])), 1)


class HandTests(CleanupCase):
    def test_fleet_worktree_remove_takes_the_task_lock_and_finishes_a_cut_short_removal(self):
        self.switch_on()
        ctx = self.closed_build()
        with run_desk.task_lock(ctx["task"]), self.assertRaisesRegex(FleetError, "holds its lock"):
            worktree.remove(self.conn, ctx["task"])
        self.assertTrue(ctx["wt"].is_dir())
        with self.kill_at_remove(), self.assertRaises(Killed):
            self.sweep(ctx)
        self.assertEqual(worktree.remove(self.conn, ctx["task"])["removed"], str(ctx["wt"]))
        self.assert_removed(ctx)
        with self.assertRaisesRegex(FleetError, "removed already"):
            worktree.remove(self.conn, ctx["task"])

    def test_fleet_worktree_remove_keeps_gits_rule_for_your_own_command(self):
        ctx = self.closed_build(pushed=None)  # nothing pushed: your command, your call
        self.write_file(ctx["wt"] / "notes.txt", "mine\n")
        with self.assertRaisesRegex(FleetError, "uncommitted changes"):
            worktree.remove(self.conn, ctx["task"])
        os.unlink(ctx["wt"] / "notes.txt")
        self.assertEqual(worktree.remove(self.conn, ctx["task"])["branch_kept"], "fix/widget")
        self.assert_removed(ctx)
        self.assertEqual(json.loads(self.marker(ctx["task"], "removed").read_text())["by"], "hand")
        self.switch_on()
        self.assertEqual(self.sweep(ctx)["reported"], 0)


    def test_fleet_worktree_remove_keeps_gits_rule_for_ignored_files(self):
        ctx = self.closed_build()
        self.ignore(".env")
        self.write_file(ctx["wt"] / ".env", "yours to keep or not\n")
        self.assertEqual(worktree.remove(self.conn, ctx["task"])["state"], "removed")
        self.assert_removed(ctx)


class MapTests(CleanupCase, MapRoundCase):
    def test_map_round_runs_the_sweep_on_every_path_and_its_row_says_what_it_did(self):
        self.switch_on()
        ctx = self.closed_build()
        with mock.patch.object(config, "GITHUB_ACCOUNT", "<github-account>"):  # GitHub cannot be read
            result = patrol_map.run_round(self.conn, now=self.t0 + 3 * DAYS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["worktrees"]["removed"], 1)
        self.assertEqual(self.round_row()["worktrees"]["removed"], 1)
        self.assert_removed(ctx)
        with mock.patch.object(config, "GITHUB_ACCOUNT", "octo"):
            result = patrol_map.run_round(self.conn, now=self.t0 + 3 * DAYS + 900)
        self.assertEqual(self.round_row()["worktrees"], {"state": "on", "removed": 0, "kept": 0, "busy": 0,
                                                         "reported": 0})
        with mock.patch.object(worktree, "markers", side_effect=safefs.Unsafe("not a plain folder")):
            self.assertEqual(patrol_map.run_round(self.conn, now=self.t0 + 3 * DAYS + 1800)["worktrees"]["state"],
                             "failed")
