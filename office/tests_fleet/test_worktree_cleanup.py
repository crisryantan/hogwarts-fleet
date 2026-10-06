"""Worktree cleanup: the closer removes the worktree of a build it closes on proof, and the Map round's sweep removes the
worktree of a build task closed long enough ago, each only when nothing in it can be lost.

Runs on real git repos in temp folders, on the auto-close test case: origin keeps its GitHub URL, and
url.<bare>.insteadOf sends every fetch to a local bare repo, so nothing reaches the network.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from unittest import mock

from hogwarts import owlery, pensieve
from hogwarts.errors import StoreError

from fleet import closer, config, gitops, run_desk, worktree
from fleet.safefs import FleetError
from tests_fleet.test_auto_close import TOKEN, CloseCase, tearDownModule  # noqa: F401
from tests_fleet.test_review_chain import Killed

DAYS = 86400


class GitSpy:
    """gitops.git wrapped: every argv the fleet ran, and an optional action before or instead of one."""

    def __init__(self, before=None) -> None:
        self.calls: list = []
        self.before = before
        self.real = gitops.git

    def __call__(self, args, *rest, **kwargs):
        self.calls.append(list(args))
        if self.before is not None:
            self.before(list(args))
        return self.real(args, *rest, **kwargs)

    def worktree_calls(self) -> list:
        return [call for call in self.calls if call and call[0] == "worktree"]


class CleanupCase(CloseCase):
    def switch_on(self, text: str = "on\n") -> None:
        self.write_file(self.office / config.WORKTREE_CLEANUP_FILE, text)

    def switch_off(self) -> None:
        if os.path.lexists(self.office / config.WORKTREE_CLEANUP_FILE):
            os.unlink(self.office / config.WORKTREE_CLEANUP_FILE)

    def closed_build(self, branch: str = "fix/widget", reason: str = "abandoned", at: int = None,
                     pushed: str = "main") -> dict:
        """A passed build closed by hand at at (t0 by default), its HEAD pushed to origin's main, to its own branch on
        origin, or nowhere (pushed None)."""
        ctx = self.passed_build(branch=branch)
        if pushed == "main":
            self.push_main(ctx["sha"])
        elif pushed == "branch":
            self.push_main(ctx["sha"], branch=branch)
        at = self.t0 if at is None else at
        if reason == "complete":  # Mischief managed: a close token, then the close
            token = owlery.mint(self.conn, ctx["task"], "cli")["token"]
            pensieve.close_task(self.conn, ctx["task"], "complete", token, now=at)
        else:
            pensieve.close_task(self.conn, ctx["task"], reason, now=at)
        ctx["closed_at"] = at
        return ctx

    def sweep(self, ctx: dict = None, after: int = 3 * DAYS, at: int = None) -> dict:
        when = at if at is not None else (ctx["closed_at"] if ctx else self.t0) + after
        return worktree.sweep_closed(self.conn, now=when)

    def listed(self, path: Path) -> bool:
        return str(path) in gitops.worktree_paths(str(self.repo / ".git"))

    def has_branch(self, branch: str) -> bool:
        return gitops.has_branch(str(self.repo / ".git"), branch)

    def cleanup_events(self, kind: str = None) -> list:
        return [event for event in self.events() if event["kind"].startswith("worktree.")
                and (kind is None or event["kind"] == kind)]

    def marker(self, task_id: str, kind: str) -> Path:
        return self.office / "worktrees" / f"{task_id}.{kind}"

    def assert_kept(self, ctx: dict, why: str) -> None:
        self.assertTrue(ctx["wt"].is_dir())
        self.assertTrue(self.listed(ctx["wt"]))
        [event] = self.cleanup_events("worktree.kept")
        self.assertEqual((event["verdict"], event["desk"]), ("headmaster", "harry"))
        self.assertIn(f"kept worktree {ctx['wt']} of closed task {ctx['task']}: {why}", event["summary"])
        self.assertFalse(self.marker(ctx["task"], "removed").exists())

    def kill_at_remove(self, after_folder: bool = False, after_git: bool = False):
        """Kill the removal at git worktree remove: before git runs, after git deleted the folder but not its entry,
        or after git finished."""
        real = gitops.git

        def git(args, *rest, **kwargs):
            if list(args[:2]) == ["worktree", "remove"]:
                if after_git:
                    real(args, *rest, **kwargs)
                elif after_folder:
                    shutil.rmtree(args[2])
                raise Killed()
            return real(args, *rest, **kwargs)
        return mock.patch.object(gitops, "git", side_effect=git)

    def ignore(self, *patterns: str) -> None:
        """Patterns every worktree of the repo ignores, through its info/exclude, so no worktree shows a change."""
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        with open(info / "exclude", "a") as handle:
            handle.write("".join(f"{pattern}\n" for pattern in patterns))

    def link_deps(self, ctx: dict, in_record: bool = True, target: str = None) -> Path:
        """A node_modules link in the worktree, git-ignored, as the toolchain makes one: listed in the office record and
        pointing at the main checkout's copy, unless in_record is False or target names somewhere else."""
        (self.repo / "node_modules").mkdir(exist_ok=True)
        self.write_file(self.repo / "node_modules" / "pkg.js", "shared\n")
        self.ignore("node_modules")
        record = gitops.find_record(str(ctx["wt"]))
        if in_record:
            gitops.write_record({**record, "links": ["node_modules"]})
        link = ctx["wt"] / "node_modules"
        os.symlink(target or f"{record['repo_dir']}/node_modules", link)
        return link

    FLAGS = ("--assume-unchanged", "--skip-worktree")
    FLAGGED = "tracked files git is told not to check (assume-unchanged or skip-worktree)"

    def hide_change(self, ctx: dict, flag: str) -> None:
        """A change to a tracked file that git status no longer shows, through flag."""
        self.write_file(ctx["wt"] / "README.md", "changed here, hidden from git status\n")
        self.git("update-index", flag, "README.md", cwd=ctx["wt"])
        self.assertFalse(gitops.dirty(gitops.find_record(str(ctx["wt"]))))

    def kept_for(self, task_id: str) -> list:
        return [event for event in self.cleanup_events("worktree.kept") if task_id in event["summary"]]

    def intent(self, task_id: str) -> Path:
        return self.marker(task_id, "closing")

    def removed_rows(self, task_id: str) -> list:
        return [event for event in self.cleanup_events("worktree.removed") if task_id in event["summary"]]

    def assert_removed(self, ctx: dict, branch: str = "fix/widget") -> None:
        self.assertFalse(os.path.lexists(ctx["wt"]))
        self.assertFalse(self.listed(ctx["wt"]))
        self.assertTrue(self.has_branch(branch))
        self.assertTrue(self.marker(ctx["task"], "removed").exists())
        self.assertFalse(self.marker(ctx["task"], "removing").exists())


class CloserTests(CleanupCase):
    def test_proven_close_removes_the_clean_build_worktree_and_keeps_its_branch(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        spy = GitSpy()
        with mock.patch.object(gitops, "git", side_effect=spy):
            result = self.close(ctx)
        self.assertEqual(result["outcome"], "closed")
        self.assertEqual(result["worktree"], {"removed": str(ctx["wt"])})
        self.assert_removed(ctx)
        [event] = self.close_events()
        # The close commits before the removal, so its event says what is under way, not what is done.
        self.assertIn("closed with it. Its clean worktree is being removed and its branch kept. Evidence:",
                      event["summary"])
        self.assertFalse(self.intent(ctx["task"]).exists())
        self.assertEqual(self.cleanup_events(), [])  # the close event is the word, no row of its own
        self.assertEqual(json.loads(self.marker(ctx["task"], "removed").read_text())["by"], "closer")
        self.assertIn(["worktree", "remove", str(ctx["wt"])], spy.worktree_calls())
        self.assertFalse(any("prune" in call or "--force" in call and str(ctx["wt"]) in call
                             for call in spy.worktree_calls()))

    def test_proven_close_keeps_a_dirty_worktree_and_names_it_in_the_close_event(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.write_file(ctx["wt"] / "notes.txt", "mine, never committed\n")
        result = self.close(ctx)
        self.assertEqual(result["outcome"], "closed")
        self.assertEqual(result["worktree"]["kept"], str(ctx["wt"]))
        self.assertEqual((ctx["wt"] / "notes.txt").read_text(), "mine, never committed\n")
        self.assertTrue(self.listed(ctx["wt"]))
        [event] = self.close_events()
        self.assertIn(f"Kept worktree {ctx['wt']}: uncommitted changes.", event["summary"])
        self.assertIn("Evidence: close-evidence-", event["summary"])

    def test_proven_close_keeps_a_worktree_whose_head_moved_past_the_proved_commit(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.write_file(ctx["wt"] / "later.txt", "a later commit, pushed nowhere\n")
        self.git("add", "later.txt", cwd=ctx["wt"])
        self.git("commit", "-q", "-m", "later", cwd=ctx["wt"])
        later = self.git("rev-parse", "HEAD", cwd=ctx["wt"])
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=ctx["wt"]), later)
        [event] = self.close_events()
        self.assertIn(f"Kept worktree {ctx['wt']}: its HEAD has commits that are neither pushed nor merged",
                      event["summary"])

    def test_proven_close_removes_a_worktree_whose_head_is_on_the_merged_base(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        self.git("checkout", "-q", "--detach", merge, cwd=ctx["wt"])  # past the proved commit, but on the base
        result = self.close(ctx)
        self.assertEqual(result["worktree"], {"removed": str(ctx["wt"])})
        self.assert_removed(ctx)

    def test_proven_close_keeps_a_worktree_it_cannot_read(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        with mock.patch.object(gitops, "dirty", side_effect=FleetError(f"git status failed: {TOKEN}")):
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        [event] = self.close_events()
        self.assertIn(f"Kept worktree {ctx['wt']}: it could not be read", event["summary"])
        self.assertNotIn(TOKEN, event["summary"])

    def test_proven_close_rechecks_after_the_close_and_tells_you_once_when_it_keeps(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.write_file(ctx["wt"] / "notes.txt", "written while the close was proving\n")
        with mock.patch.object(worktree, "why_kept", return_value=None):  # the plan saw it clean
            result = self.close(ctx)
        self.assertEqual(result["worktree"]["kept"], str(ctx["wt"]))
        self.assertTrue((ctx["wt"] / "notes.txt").is_file())
        self.assert_kept(ctx, "uncommitted changes")
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual(len(self.cleanup_events("worktree.kept")), 1)

    def test_proven_close_keeps_a_worktree_with_ignored_files_that_exist_only_here(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.ignore(".env")
        self.write_file(ctx["wt"] / ".env", "a local secret no commit holds\n")
        self.assertEqual(gitops.dirty(gitops.find_record(str(ctx["wt"]))), False)  # git status counts it clean
        result = self.close(ctx)
        self.assertEqual(result["outcome"], "closed")
        self.assertEqual(result["worktree"]["kept"], str(ctx["wt"]))
        self.assertEqual((ctx["wt"] / ".env").read_text(), "a local secret no commit holds\n")
        [event] = self.close_events()
        self.assertIn(f"Kept worktree {ctx['wt']}: ignored files that exist only here.", event["summary"])
        self.assertFalse(self.intent(ctx["task"]).exists())

    def test_proven_close_rechecks_ignored_files_after_the_close(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.ignore("build/")
        (ctx["wt"] / "build").mkdir()
        self.write_file(ctx["wt"] / "build" / "out.bin", "built here\n")
        with mock.patch.object(worktree, "why_kept", return_value=None):  # the plan saw nothing ignored
            result = self.close(ctx)
        self.assertEqual(result["worktree"]["kept"], str(ctx["wt"]))
        self.assertTrue((ctx["wt"] / "build" / "out.bin").is_file())
        self.assert_kept(ctx, "ignored files that exist only here")
        self.assertFalse(self.intent(ctx["task"]).exists())

    def test_proven_close_of_your_own_sessions_task_touches_no_worktree(self):
        ctx = self.passed_own()
        self.github.prs = [self.pr(ctx, number=11, merge=self.merge_commit(ctx["sha"]))]
        self.push_main(self.github.prs[0]["mergeCommit"]["oid"])
        with mock.patch.object(worktree, "remove_closed", side_effect=AssertionError("removed an own worktree")):
            result = self.close(ctx)
        self.assertEqual(result["outcome"], "closed")
        self.assertNotIn("worktree", result)

    def test_only_auto_close_governs_the_close_time_removal(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.switch_on()
        self.opt_out()
        self.assertEqual(self.close(ctx)["outcome"], "off")
        self.assertTrue(ctx["wt"].is_dir())
        self.assertEqual(self.sweep(at=self.t0 + 30 * DAYS)["removed"], 0)  # still awaiting close: never the sweep's
        self.assertTrue(ctx["wt"].is_dir())
        self.switch_off()
        self.opt_in()
        self.assertEqual(self.close(ctx)["worktree"], {"removed": str(ctx["wt"])})


class SweepTests(CleanupCase):
    def test_sweep_off_by_default_and_on_only_through_the_shared_reader(self):
        ctx = self.closed_build()
        self.assertEqual(self.sweep(ctx, after=30 * DAYS)["state"], "off")  # auto-close is on, which is not this
        self.assertTrue(ctx["wt"].is_dir())
        for text in ("ON\n", "on please\n", ""):
            self.switch_on(text)
            self.assertFalse(worktree.cleanup_on())
        target = self.write_file(self.tmp / "elsewhere", "on\n")
        self.switch_off()
        os.symlink(target, self.office / config.WORKTREE_CLEANUP_FILE)
        self.assertFalse(worktree.cleanup_on())
        os.unlink(self.office / config.WORKTREE_CLEANUP_FILE)
        self.write_file(self.castle / "desks" / "harry" / config.WORKTREE_CLEANUP_FILE, "on\n")
        self.assertEqual(self.sweep(ctx, after=30 * DAYS)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir())
        self.assertEqual(self.cleanup_events(), [])

    def test_sweep_removes_worktrees_of_tasks_closed_by_any_path_after_three_days_in_one_row(self):
        self.switch_on()
        merged = self.closed_build(reason="complete")
        pushed = self.closed_build(branch="fix/gadget", reason="superseded", pushed="branch")
        dropped = self.closed_build(branch="fix/gizmo", reason="abandoned", pushed="branch")
        result = self.sweep(at=self.t0 + 3 * DAYS - 1)
        self.assertEqual((result["state"], result["removed"]), ("on", 0))
        for ctx in (merged, pushed, dropped):
            self.assertTrue(ctx["wt"].is_dir())
        spy = GitSpy()
        with mock.patch.object(gitops, "git", side_effect=spy):
            result = self.sweep(at=self.t0 + 3 * DAYS)
        self.assertEqual((result["removed"], result["kept"], result["reported"]), (3, 0, 3))
        for ctx, branch in ((merged, "fix/widget"), (pushed, "fix/gadget"), (dropped, "fix/gizmo")):
            self.assert_removed(ctx, branch)
        [row] = self.cleanup_events()
        self.assertEqual((row["kind"], row["verdict"], row["desk"]), ("worktree.removed", "routine", "map"))
        self.assertIn("removed 3 worktrees of closed tasks", row["summary"])
        for ctx in (merged, pushed, dropped):
            self.assertIn(ctx["task"], row["summary"])
        removes = [call for call in spy.worktree_calls() if call[1] == "remove"]
        self.assertEqual(sorted(removes), sorted(["worktree", "remove", str(ctx["wt"])]
                                                 for ctx in (merged, pushed, dropped)))
        self.assertFalse(any(call[0] in ("branch", "update-ref") or "prune" in call or "--force" in call
                             for call in spy.calls))
        result = self.sweep(at=self.t0 + 4 * DAYS)
        self.assertEqual((result["removed"], result["reported"]), (0, 0))
        self.assertEqual(len(self.cleanup_events()), 1)

    def test_sweep_keeps_dirty_worktrees_tells_once_and_looks_again(self):
        self.switch_on()
        ctx = self.closed_build()
        self.write_file(ctx["wt"] / "notes.txt", "mine\n")
        for offset in (0, 900):
            self.assertEqual(self.sweep(ctx, after=3 * DAYS + offset)["kept"], 1)
        self.assert_kept(ctx, "uncommitted changes")
        os.unlink(ctx["wt"] / "notes.txt")
        self.write_file(ctx["wt"] / "README.md", "changed, never committed\n")
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 1800)["kept"], 1)
        self.write_file(ctx["wt"] / "README.md", "readme\n")
        self.git("add", "README.md", cwd=ctx["wt"])
        self.write_file(ctx["wt"] / "README.md", "staged, then changed back\n")
        self.git("add", "README.md", cwd=ctx["wt"])
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 2700)["kept"], 1)
        self.assertEqual((ctx["wt"] / "README.md").read_text(), "staged, then changed back\n")
        self.git("reset", "-q", "--hard", cwd=ctx["wt"])
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 3600)["removed"], 1)
        self.assert_removed(ctx)
        self.assertEqual(len(self.cleanup_events("worktree.kept")), 1)

    def test_sweep_keeps_commits_that_are_neither_pushed_nor_merged(self):
        self.switch_on()
        ctx = self.closed_build(pushed="branch")  # the branch on origin holds the reviewed commit, not the later one
        self.write_file(ctx["wt"] / "later.txt", "later\n")
        self.git("add", "later.txt", cwd=ctx["wt"])
        self.git("commit", "-q", "-m", "later", cwd=ctx["wt"])
        for offset in (0, 900):
            self.assertEqual(self.sweep(ctx, after=3 * DAYS + offset)["kept"], 1)
        self.assert_kept(ctx, "its HEAD has commits that are neither pushed nor merged")
        self.push_main(self.git("rev-parse", "HEAD", cwd=ctx["wt"]), branch="fix/widget")
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 1800)["removed"], 1)
        self.assert_removed(ctx)

    def test_sweep_keeps_a_worktree_whenever_a_fetch_or_read_fails(self):
        self.switch_on()
        ctx = self.closed_build()
        failures = (
            (gitops, "worktree_paths", {"side_effect": FleetError(f"git worktree failed: {TOKEN}")},
             "it could not be read"),
            (gitops, "fetch_branch", {"side_effect": FleetError("git fetch failed: offline")},
             "its commits could not be confirmed on origin"),
            (gitops, "is_ancestor", {"return_value": None}, "its commits could not be confirmed on origin"),
            (gitops, "dirty", {"side_effect": FleetError("git status failed")}, "it could not be read"),
            (gitops, "flagged", {"side_effect": FleetError("git ls-files failed")}, "it could not be read"),
            (gitops, "rev", {"side_effect": FleetError("git rev-parse failed")}, "it could not be read"),
            (worktree, "read_marker", {"side_effect": FleetError("a worktree removal marker is malformed")},
             "it could not be read"),
        )
        for number, (module, name, how, why) in enumerate(failures):
            with self.subTest(failing=name), mock.patch.object(module, name, **how):
                result = self.sweep(ctx, after=3 * DAYS + 900 * number)
                self.assertEqual((result["removed"], result["kept"]), (0, 1))
            self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
            self.assertFalse(self.marker(ctx["task"], "removed").exists())
            if number == 0:
                self.assert_kept(ctx, why)
        [event] = self.cleanup_events()
        self.assertNotIn(TOKEN, event["summary"])
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 9000)["removed"], 1)
        self.assert_removed(ctx)

    def test_sweep_keeps_a_worktree_with_ignored_files_tells_once_and_looks_again(self):
        self.switch_on()
        ctx = self.closed_build()
        self.ignore(".env", "build/")
        for name, setup in ((".env", lambda: self.write_file(ctx["wt"] / ".env", "local only\n")),
                            ("build", lambda: ((ctx["wt"] / "build").mkdir(),
                                               self.write_file(ctx["wt"] / "build" / "out.bin", "built\n")))):
            with self.subTest(ignored=name):
                setup()
                for offset in (0, 900):
                    self.assertEqual(self.sweep(ctx, after=3 * DAYS + offset)["kept"], 1)
                self.assert_kept(ctx, "ignored files that exist only here")
                self.assertFalse(self.marker(ctx["task"], "removing").exists())
                shutil.rmtree(ctx["wt"] / "build") if name == "build" else os.unlink(ctx["wt"] / ".env")
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 1800)["removed"], 1)
        self.assert_removed(ctx)

    def test_sweep_takes_the_toolchains_own_dependency_link_by_the_record_never_by_its_name(self):
        self.switch_on()
        ctx = self.closed_build()
        stray = self.link_deps(ctx, in_record=False)  # the right name and target, but the record lists no link
        self.assertEqual(self.git("ls-files", "-o", "-i", "--exclude-standard", "--directory", cwd=ctx["wt"]),
                         "node_modules")  # git ignores it, so git status counts the worktree clean
        self.assertEqual(self.sweep(ctx)["kept"], 1)
        self.assert_kept(ctx, "ignored files that exist only here")
        os.unlink(stray)
        elsewhere = self.tmp / "elsewhere-modules"
        elsewhere.mkdir()
        self.link_deps(ctx, target=str(elsewhere))  # in the record, but not the link the toolchain made
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["kept"], 1)
        self.assertTrue(os.path.islink(ctx["wt"] / "node_modules") and self.listed(ctx["wt"]))
        os.unlink(ctx["wt"] / "node_modules")
        os.symlink(f"{gitops.find_record(str(ctx['wt']))['repo_dir']}/node_modules", ctx["wt"] / "node_modules")
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 1800)["removed"], 1)
        self.assert_removed(ctx)
        self.assertEqual((self.repo / "node_modules" / "pkg.js").read_text(), "shared\n")

    def test_sweep_never_removes_a_worktree_in_use(self):
        self.switch_on()
        ctx = self.closed_build()
        with run_desk.task_lock(ctx["task"]):
            result = self.sweep(ctx)
        self.assertEqual((result["removed"], result["busy"], result["kept"]), (0, 1, 0))
        self.assertTrue(ctx["wt"].is_dir() and self.listed(ctx["wt"]))
        self.assertEqual(self.cleanup_events(), [])
        self.assertEqual(self.sweep(ctx, after=3 * DAYS + 900)["removed"], 1)
        self.assert_removed(ctx)

    def test_sweep_never_touches_a_task_that_is_not_closed(self):
        self.switch_on()
        ctx = self.passed_build()  # awaiting close, its HEAD on origin's main
        self.push_main(ctx["sha"])
        self.assertEqual(self.sweep(at=self.t0 + 30 * DAYS)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir())
        with mock.patch.object(pensieve, "list_tasks", return_value=[pensieve.get_task(self.conn, ctx["task"])]):
            self.assertEqual(self.sweep(at=self.t0 + 30 * DAYS)["removed"], 0)
        self.assertTrue(ctx["wt"].is_dir())
        with run_desk.task_lock(ctx["task"]), self.assertRaisesRegex(FleetError, "only a closed task"):
            worktree.remove_closed(self.conn, ctx["task"], "sweep", lambda record, head: True)
        self.assertTrue(ctx["wt"].is_dir())

    def test_sweep_leaves_what_git_does_not_list_and_never_prunes(self):
        self.switch_on()
        gone = self.closed_build()
        stray = self.closed_build(branch="fix/gadget")
        shutil.rmtree(gone["wt"])  # git still lists it, its folder is gone, and no removal of ours started
        self.git("worktree", "remove", str(stray["wt"]))
        stray["wt"].mkdir()
        self.write_file(stray["wt"] / "keep.txt", "yours\n")
        spy = GitSpy()
        with mock.patch.object(gitops, "git", side_effect=spy):
            for offset in (0, 900):
                self.assertEqual(self.sweep(at=self.t0 + 3 * DAYS + offset)["kept"], 2)
        self.assertTrue(self.listed(gone["wt"]))
        self.assertEqual((stray["wt"] / "keep.txt").read_text(), "yours\n")
        self.assertEqual([call for call in spy.worktree_calls() if call[1] != "list"], [])
        summaries = sorted(event["summary"] for event in self.cleanup_events("worktree.kept"))
        self.assertEqual(len(summaries), 2)
        self.assertTrue(any("git lists it but its folder is gone" in text for text in summaries))
        self.assertTrue(any("its folder is not a worktree git lists" in text for text in summaries))

    def test_sweep_never_follows_a_link_in_place_of_the_worktree_folder(self):
        self.switch_on()
        ctx = self.closed_build()
        moved = self.tmp / "moved-away"
        os.rename(ctx["wt"], moved)
        os.symlink(moved, ctx["wt"])
        self.assertEqual(self.sweep(ctx)["kept"], 1)
        self.assertTrue((moved / "widget.txt").is_file())
        self.assertTrue(os.path.islink(ctx["wt"]))
        [event] = self.cleanup_events()
        self.assertIn("not a plain folder of yours in the worktrees folder", event["summary"])

    def test_sweep_round_that_cannot_finish_is_one_event_a_day_and_never_raises(self):
        self.switch_on()
        self.closed_build()
        noon = int(time.mktime((2027, 1, 15, 12, 0, 0, 0, 0, -1)))  # local noon, so both rounds below are one day
        with mock.patch.object(pensieve, "list_tasks", side_effect=StoreError("store is busy")):
            for offset in (0, 900):
                self.assertEqual(self.sweep(at=noon + offset)["state"], "failed")
        [event] = self.cleanup_events()
        self.assertEqual((event["kind"], event["verdict"]), ("worktree.cleanup-failed", "headmaster"))
