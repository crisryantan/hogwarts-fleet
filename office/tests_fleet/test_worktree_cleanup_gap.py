"""Worktree cleanup: a kill between a close and its removal, hidden changes, and the switch read again before
removal.

Split from test_worktree_cleanup.py so the parallel runner can run it beside the rest; it shares CleanupCase.
"""
from __future__ import annotations

import contextlib
import json
import os
from unittest import mock

from hogwarts import pensieve
from hogwarts.errors import StoreError

from fleet import closer, gitops, run_desk, worktree
from tests_fleet.test_auto_close import tearDownModule  # noqa: F401
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_worktree_cleanup import DAYS, CleanupCase


class CloseGapTests(CleanupCase):
    """A kill anywhere between the closer's intent and its removal marker: the next Map round finishes the removal of a
    close that committed, and drops the intent of one that did not, with the cleanup switch on or off."""

    def kill_in_the_gap(self, step: str):
        real_close, real_record = pensieve.close_proven, closer.write_record

        def close_proven(*args, **kwargs):
            if step == "before the close commits":
                raise Killed()
            closed = real_close(*args, **kwargs)
            if step == "after the close commits":
                raise Killed()
            return closed

        def write_record(record, *args, **kwargs):
            if step == "at its close record" and record.get("state") == "closed":
                raise Killed()
            return real_record(record, *args, **kwargs)

        patches = [mock.patch.object(pensieve, "close_proven", side_effect=close_proven),
                   mock.patch.object(closer, "write_record", side_effect=write_record)]
        if step == "at housekeeping":
            patches.append(mock.patch.object(closer, "housekeep", side_effect=Killed()))
        if step == "before its removal marker":
            patches.append(mock.patch.object(worktree, "remove_closed", side_effect=Killed()))
        stack = contextlib.ExitStack()
        for patcher in patches:
            stack.enter_context(patcher)
        return stack

    STEPS = ("before the close commits", "after the close commits", "at its close record", "at housekeeping",
             "before its removal marker")

    def test_a_kill_anywhere_between_the_close_and_its_removal_never_leaves_the_worktree_in_silence(self):
        for cleanup in (False, True):
            for step in self.STEPS:
                with self.subTest(cleanup=cleanup, step=step):
                    self.switch_on() if cleanup else self.switch_off()
                    branch = f"fix/{'on' if cleanup else 'off'}-{step.replace(' ', '-')}"
                    ctx = self.passed_build(branch=branch)
                    self.land_pr(ctx)
                    with self.kill_in_the_gap(step), self.assertRaises(Killed):
                        self.close(ctx)
                    self.assertTrue(self.intent(ctx["task"]).exists())  # written before the close transaction
                    self.assertTrue(ctx["wt"].is_dir())
                    committed = step != "before the close commits"
                    self.assertEqual(self.status(ctx["task"]), "closed" if committed else "awaiting_close")
                    result = self.sweep(at=self.t0 + 9000)
                    self.assertFalse(self.intent(ctx["task"]).exists())
                    if committed:
                        self.assertEqual((result["removed"], result["kept"]), (1, 0))
                        self.assert_removed(ctx, branch)
                        self.assertEqual(json.loads(self.marker(ctx["task"], "removed").read_text())["by"], "closer")
                    else:  # nothing closed, so nothing is removed, and the next pass closes and removes it
                        self.assertEqual(result["removed"], 0)
                        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
                        self.assertEqual(self.close(ctx, now=self.t0 + 9900)["worktree"], {"removed": str(ctx["wt"])})
                        self.assert_removed(ctx, branch)
                    self.assertEqual(self.removed_rows(ctx["task"]), [])  # its close event is the word
        self.assertEqual(self.cleanup_events("worktree.kept"), [])

    def test_a_close_killed_before_its_removal_tells_you_once_while_auto_close_is_off(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with self.kill_in_the_gap("after the close commits"), self.assertRaises(Killed):
            self.close(ctx)
        self.opt_out()
        for offset in (0, 900):
            self.assertEqual(self.sweep(at=self.t0 + 9000 + offset)["kept"], 1)
        self.assert_kept(ctx, "its removal was cut short and the switch that started it is off now")
        self.assertTrue(self.intent(ctx["task"]).exists())
        self.opt_in()
        self.assertEqual(self.sweep(at=self.t0 + 9900)["removed"], 1)
        self.assert_removed(ctx)
        self.assertFalse(self.intent(ctx["task"]).exists())

    def test_an_intent_is_read_against_the_close_it_names_under_the_task_lock(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with self.kill_in_the_gap("after the close commits"), self.assertRaises(Killed):
            self.close(ctx)
        with run_desk.task_lock(ctx["task"]):
            self.assertEqual(self.sweep(at=self.t0 + 9000)["busy"], 1)
        self.assertTrue(self.intent(ctx["task"]).exists() and ctx["wt"].is_dir())
        with mock.patch.object(pensieve, "task_closure", side_effect=StoreError("store is busy")):
            self.assertEqual(self.sweep(at=self.t0 + 9900)["kept"], 1)  # a failed read never drops the intent
        self.assertTrue(self.intent(ctx["task"]).exists() and ctx["wt"].is_dir())
        self.write_file(self.intent(ctx["task"]), "not json\n")
        self.assertEqual(self.sweep(at=self.t0 + 10800)["kept"], 1)
        self.assertTrue(self.intent(ctx["task"]).exists() and ctx["wt"].is_dir())
        [event] = self.cleanup_events("worktree.kept")
        self.assertIn("it could not be read", event["summary"])

    def test_an_unreadable_intent_of_a_task_still_open_is_dropped_without_a_word(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with self.kill_in_the_gap("before the close commits"), self.assertRaises(Killed):
            self.close(ctx)
        self.write_file(self.intent(ctx["task"]), "not json\n")
        self.assertEqual(self.sweep(at=self.t0 + 9000)["kept"], 0)
        self.assertFalse(self.intent(ctx["task"]).exists())
        self.assertEqual(self.cleanup_events(), [])
        self.assertEqual(self.close(ctx, now=self.t0 + 9900)["worktree"], {"removed": str(ctx["wt"])})

    def test_an_intent_whose_close_another_path_made_is_dropped_and_removes_nothing(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with self.kill_in_the_gap("before the close commits"), self.assertRaises(Killed):
            self.close(ctx)
        pensieve.close_task(self.conn, ctx["task"], "abandoned", now=self.t0 + 8000)  # closed by hand meanwhile
        self.assertEqual(self.sweep(at=self.t0 + 9000)["removed"], 0)
        self.assertFalse(self.intent(ctx["task"]).exists())
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))


class HiddenChangeTests(CleanupCase):
    """A tracked file marked assume-unchanged or skip-worktree hides its changes from git status, and git worktree
    remove deletes them, so no automatic removal takes such a worktree, on any path."""

    def test_proven_close_keeps_a_worktree_with_hidden_changes_to_tracked_files(self):
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                ctx = self.passed_build(branch=f"fix/close{flag}")
                self.land_pr(ctx)
                self.hide_change(ctx, flag)
                result = self.close(ctx)
                self.assertEqual(result["outcome"], "closed")
                self.assertEqual(result["worktree"]["kept"], str(ctx["wt"]))
                self.assertEqual((ctx["wt"] / "README.md").read_text(), "changed here, hidden from git status\n")
                [event] = [event for event in self.close_events() if ctx["task"] in event["summary"]]
                self.assertIn(f"Kept worktree {ctx['wt']}: {self.FLAGGED}.", event["summary"])
                self.assertFalse(self.intent(ctx["task"]).exists())

    def test_proven_close_rechecks_hidden_changes_after_the_close(self):
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                ctx = self.passed_build(branch=f"fix/recheck{flag}")
                self.land_pr(ctx)
                self.hide_change(ctx, flag)
                with mock.patch.object(worktree, "why_kept", return_value=None):
                    self.assertEqual(self.close(ctx)["worktree"]["kept"], str(ctx["wt"]))
                self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
                [event] = self.kept_for(ctx["task"])
                self.assertIn(self.FLAGGED, event["summary"])

    def test_sweep_keeps_a_worktree_with_hidden_changes_tells_once_and_looks_again(self):
        self.switch_on()
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                ctx = self.closed_build(branch=f"fix/sweep{flag}")
                self.hide_change(ctx, flag)
                for offset in (0, 900):
                    self.sweep(ctx, after=3 * DAYS + offset)
                self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
                self.assertEqual((ctx["wt"] / "README.md").read_text(), "changed here, hidden from git status\n")
                [event] = self.kept_for(ctx["task"])
                self.assertIn(self.FLAGGED, event["summary"])
                self.assertFalse(self.marker(ctx["task"], "removing").exists())
                self.git("update-index", flag.replace("--", "--no-"), "README.md", cwd=ctx["wt"])
                self.git("checkout", "--", "README.md", cwd=ctx["wt"])
                self.sweep(ctx, after=3 * DAYS + 1800)
                self.assert_removed(ctx, f"fix/sweep{flag}")

    def test_recovery_of_an_intent_or_a_marker_keeps_hidden_changes(self):
        for flag in self.FLAGS:
            for left in ("intent", "marker"):
                with self.subTest(flag=flag, left=left):
                    branch = f"fix/{left}{flag}"
                    if left == "intent":
                        ctx = self.passed_build(branch=branch)
                        self.land_pr(ctx)
                        with CloseGapTests.kill_in_the_gap(self, "after the close commits"), self.assertRaises(Killed):
                            self.close(ctx)
                    else:
                        self.switch_on()
                        ctx = self.closed_build(branch=branch)
                        with self.kill_at_remove(), self.assertRaises(Killed):
                            self.sweep(ctx)
                    self.hide_change(ctx, flag)
                    self.assertEqual(self.sweep(at=self.t0 + 3 * DAYS + 9000)["removed"], 0)
                    self.assertEqual((ctx["wt"] / "README.md").read_text(), "changed here, hidden from git status\n")
                    self.assertTrue(self.listed(ctx["wt"]))
                    [event] = self.kept_for(ctx["task"])
                    self.assertIn(self.FLAGGED, event["summary"])
                    self.switch_off()


class LastCheckTests(CleanupCase):
    """The switch that started a removal is read again right before git worktree remove."""

    def test_the_sweep_removes_nothing_once_its_switch_went_off_during_its_checks(self):
        self.switch_on()
        ctx = self.closed_build()
        link = self.link_deps(ctx)
        real = gitops.fetch_branch

        def fetch_then_off(*args, **kwargs):
            self.switch_off()
            return real(*args, **kwargs)
        with mock.patch.object(gitops, "fetch_branch", side_effect=fetch_then_off):
            result = self.sweep(ctx)
        self.assertEqual((result["removed"], result["kept"]), (0, 0))
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        self.assertTrue(gitops.borrowed_link(gitops.find_record(str(ctx["wt"])), "node_modules"))  # linked again
        self.assertTrue(os.path.islink(link))
        self.assertFalse(self.marker(ctx["task"], "removing").exists())
        self.assertEqual(self.cleanup_events(), [])
        self.switch_on()
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["removed"], 1)
        self.assert_removed(ctx)

    def test_the_closer_keeps_its_intent_when_auto_close_went_off_before_the_removal(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        real = closer.housekeep

        def housekeep_then_off(*args, **kwargs):
            self.opt_out()
            return real(*args, **kwargs)
        with mock.patch.object(closer, "housekeep", side_effect=housekeep_then_off):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["worktree"]["kept"]), ("closed", str(ctx["wt"])))
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        self.assertTrue(self.intent(ctx["task"]).exists())
        self.assertFalse(self.marker(ctx["task"], "removing").exists())
        self.assertEqual(self.sweep(at=self.t0 + 9000)["removed"], 0)
        self.assertEqual(len(self.kept_for(ctx["task"])), 1)
        self.opt_in()
        self.assertEqual(self.sweep(at=self.t0 + 9900)["removed"], 1)
        self.assert_removed(ctx)
        self.assertFalse(self.intent(ctx["task"]).exists())

    def test_intent_recovery_keeps_its_intent_when_auto_close_went_off_during_its_checks(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with CloseGapTests.kill_in_the_gap(self, "after the close commits"), self.assertRaises(Killed):
            self.close(ctx)
        real = gitops.rev

        def rev_then_off(*args, **kwargs):
            self.opt_out()
            return real(*args, **kwargs)
        with mock.patch.object(gitops, "rev", side_effect=rev_then_off):
            self.assertEqual(self.sweep(at=self.t0 + 9000)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        self.assertTrue(self.intent(ctx["task"]).exists())
        self.opt_in()
        self.assertEqual(self.sweep(at=self.t0 + 9900)["removed"], 1)
        self.assert_removed(ctx)

    def test_marker_recovery_keeps_its_marker_when_the_switch_went_off_during_its_checks(self):
        self.switch_on()
        ctx = self.closed_build()
        with self.kill_at_remove(), self.assertRaises(Killed):
            self.sweep(ctx)
        real = gitops.dirty

        def dirty_then_off(*args, **kwargs):
            self.switch_off()
            return real(*args, **kwargs)
        with mock.patch.object(gitops, "dirty", side_effect=dirty_then_off):
            self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        self.assertTrue(self.marker(ctx["task"], "removing").exists())
        self.assert_kept(ctx, "its removal was cut short and the switch that started it is off now")
        self.switch_on()
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 1800)["removed"], 1)
        self.assert_removed(ctx)
