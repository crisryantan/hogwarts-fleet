"""A repo folder that is a linked git worktree: the fleet builds from it, with every check on it and its main checkout.

gitops.repo_dirs reads the worktree's .git file and its entry's commondir and gitdir files, never through git, and
the castle worktree, the fetch and the branch are made in the main checkout. Each refusal here is a way a .git file
could try to make the fleet write somewhere it should not.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest import mock

from hogwarts import pensieve

from fleet import config, gitops, review, run_desk, toolchain, worktree
from fleet.safefs import FleetError
from tests_fleet.test_many_tasks import ManyCase


class LinkedCase(ManyCase):
    def setUp(self) -> None:
        super().setUp()
        self.linked = self.add_linked("linked", "feature/linked")

    def add_linked(self, name: str, branch: str, *flags: str, repo: Path = None) -> Path:
        """A linked worktree of repo (Ryan's main checkout unless named) at ~/<name>, on a new branch."""
        folder = self.home_dir / name
        self.git("worktree", "add", "-q", *flags, "-b", branch, str(folder), "HEAD", cwd=repo or self.repo)
        return folder

    def entry(self, folder: Path, repo: Path = None) -> Path:
        return (repo or self.repo) / ".git" / "worktrees" / folder.name

    def refused(self, folder, reason: str) -> None:
        with self.assertRaisesRegex(FleetError, reason):
            gitops.repo_dirs(str(folder))
        with self.assertRaisesRegex(FleetError, reason):
            gitops.check_repo_dir(str(folder))


class RepoDirsTests(LinkedCase):
    def test_a_main_checkout_is_its_own_git_and_common_folder(self):
        self.assertEqual(gitops.repo_dirs(str(self.repo)),
                         {"repo_dir": str(self.repo), "git_dir": f"{self.repo}/.git",
                          "common_dir": f"{self.repo}/.git", "main_dir": str(self.repo)})

    def test_a_linked_worktree_names_its_entry_and_its_main_checkout(self):
        expected = {"repo_dir": str(self.linked), "git_dir": str(self.entry(self.linked)),
                    "common_dir": f"{self.repo}/.git", "main_dir": str(self.repo)}
        self.assertEqual(gitops.repo_dirs(str(self.linked)), expected)
        self.assertEqual(gitops.check_repo_dir(str(self.linked)), str(self.linked))
        relative = self.add_linked("relative", "feature/relative", "--relative-paths")
        self.assertFalse((relative / ".git").read_text().startswith("gitdir: /"))  # git wrote relative pointers
        self.assertEqual(gitops.repo_dirs(str(relative))["common_dir"], f"{self.repo}/.git")

    def test_a_symlinked_git_file_is_refused(self):
        other = self.home_dir / "other"
        other.mkdir()
        os.symlink(self.linked / ".git", other / ".git")
        self.refused(other, "must be a main checkout with its own .git folder, or a linked worktree")

    def test_a_git_file_pointing_outside_home_or_into_the_fleet_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.git("init", "-q", "-b", "main", cwd=outside)
        self.git("commit", "-q", "--allow-empty", "-m", "first", cwd=outside)
        away = self.add_linked("away", "feature/away", repo=outside)
        self.refused(away, "the worktree's main checkout must be inside your home folder")
        # The same through a .git file rewritten by hand to name an entry outside the home folder.
        (self.linked / ".git").write_text(f"gitdir: {self.entry(away, outside)}\n")
        self.refused(self.linked, "must be inside your home folder")

    def test_a_worktree_of_a_repo_in_the_castle_or_the_office_is_refused(self):
        for root in ("CASTLE_ROOT", "OFFICE_ROOT"):
            with self.subTest(root=root):
                fleet_root = self.home_dir / root.lower()
                fleet_root.mkdir()
                inside = fleet_root / "repo"
                inside.mkdir()
                self.git("init", "-q", "-b", "main", cwd=inside)
                self.git("commit", "-q", "--allow-empty", "-m", "first", cwd=inside)
                folder = self.add_linked(f"from-{root.lower()}", "feature/x", repo=inside)
                with mock.patch.object(config, root, str(fleet_root)):
                    self.refused(folder, "the worktree's main checkout must be outside the office and the castle")

    def test_a_git_file_whose_entry_points_back_to_another_folder_is_refused(self):
        copy = self.home_dir / "copy"
        copy.mkdir()
        shutil.copy(self.linked / ".git", copy / ".git")  # a .git file naming another worktree's entry
        self.refused(copy, "points back to another folder")
        (self.entry(self.linked) / "gitdir").write_text(f"{copy}/.git\n")  # the entry's gitdir no longer names it
        self.refused(self.linked, "points back to another folder")

    def test_an_entry_whose_commondir_names_another_folder_is_refused(self):
        other = self.home_dir / "other"
        other.mkdir()
        self.git("init", "-q", "-b", "main", cwd=other)
        (self.entry(self.linked) / "commondir").write_text(f"{other}/.git\n")
        self.refused(self.linked, "commondir does not name its main checkout's .git folder")

    def test_a_symlink_on_either_path_is_refused(self):
        os.symlink(self.repo, self.home_dir / "repo-link")
        (self.linked / ".git").write_text(f"gitdir: {self.home_dir}/repo-link/.git/worktrees/linked\n")
        self.refused(self.linked, "must not go through a symlink")
        (self.linked / ".git").write_text(f"gitdir: {self.entry(self.linked)}\n")
        os.symlink(self.linked, self.home_dir / "linked-link")
        self.refused(self.home_dir / "linked-link", "the repo folder must not go through a symlink")
        moved = self.home_dir / "entry-elsewhere"
        os.replace(self.entry(self.linked), moved)
        os.symlink(moved, self.entry(self.linked))
        self.refused(self.linked, "must not go through a symlink")

    def test_a_git_file_that_names_no_worktree_entry_is_refused(self):
        cases = (
            ("no gitdir line", "ref: refs/heads/main\n", "does not name a git folder"),
            ("the main .git folder", f"gitdir: {self.repo}/.git\n", "does not name a worktree entry"),
            ("a climb inside the path", f"gitdir: ../repo/.git/../.git/worktrees/linked\n", "not a plain path"),
            ("two lines", f"gitdir: {self.entry(self.linked)}\nextra\n", "not one plain line"),
            ("odd characters", f"gitdir: {self.repo}/.git/worktrees/a b\n", "plain characters"),
        )
        for label, text, reason in cases:
            with self.subTest(label=label):
                (self.linked / ".git").write_text(text)
                self.refused(self.linked, reason)

    def test_a_worktree_of_a_bare_repo_is_refused(self):
        bare = self.home_dir / "bare.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(bare), cwd=self.home_dir)
        folder = self.add_linked("from-bare", "feature/bare", repo=bare)
        self.refused(folder, "a worktree of a bare repo")

    def test_a_worktree_whose_entry_was_pruned_or_main_checkout_moved_is_refused_plainly(self):
        shutil.rmtree(self.entry(self.linked))
        self.refused(self.linked, "whose main checkout, or its entry there, is gone")
        other = self.add_linked("other", "feature/other")
        os.replace(self.repo, self.home_dir / "repo-moved")
        self.refused(other, "whose main checkout, or its entry there, is gone")


class BuildFromLinkedTests(LinkedCase):
    def test_a_build_from_a_linked_worktree_makes_its_worktree_and_branch_in_the_main_checkout(self):
        _, task, _, _ = self.harry_task()
        with mock.patch.object(run_desk, "spawn"):
            created = worktree.create(self.conn, task["id"], str(self.linked), "fix/widget", fetch=False)
        record = gitops.read_record(task["id"])
        self.assertEqual((record["repo_dir"], record["common_dir"], record["git_dir"]),
                         (str(self.linked), f"{self.repo}/.git", f"{self.repo}/.git/worktrees/{task['id']}"))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=created["worktree"]), "fix/widget")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/heads/fix/widget"), "refs/heads/fix/widget")
        self.assertIn(created["worktree"], gitops.worktree_paths(f"{self.repo}/.git"))

    def test_a_linked_worktree_and_its_main_checkout_share_one_branch_lock(self):
        with worktree.branch_claim(str(self.linked), "fix/widget"):
            with self.assertRaisesRegex(FleetError, "another worktree command for this branch"):
                with worktree.branch_claim(str(self.repo), "fix/widget"):
                    pass

    def test_dependencies_are_borrowed_from_the_linked_worktree(self):
        self.write_file(self.linked / "package.json", '{"name": "web-app"}\n')
        self.write_file(self.linked / ".gitignore", "node_modules/\n")
        (self.linked / "node_modules" / "left-pad").mkdir(parents=True)
        dirs = gitops.repo_dirs(str(self.linked))
        self.assertEqual(toolchain.linkable(dirs["repo_dir"], dirs["git_dir"]), ["node_modules"])

    def test_an_own_review_reads_head_and_branch_from_the_linked_worktree(self):
        self.write_file(self.linked / "fix.txt", "linked fix\n")
        self.git("add", "fix.txt", cwd=self.linked)
        self.git("commit", "-q", "-m", "linked fix", cwd=self.linked)
        head = self.git("rev-parse", "HEAD", cwd=self.linked)
        self.assertNotEqual(head, self.git("rev-parse", "HEAD"))  # the main checkout is elsewhere
        self.enable("moody")
        with self.fake_reviewer("CHANGES"):
            result = review.review_own(self.conn, str(self.linked), title="my own fix", fetch=False)
        record = gitops.read_record(result["task_id"])
        self.assertEqual(gitops.rev(record), head)
        self.assertEqual(record["repo_dir"], str(self.linked))
        self.assertEqual(pensieve.get_task(self.conn, result["task_id"])["review_branch"], "feature/linked")

    def test_a_record_whose_common_dir_is_not_a_git_folder_in_home_is_refused(self):
        _, task, _, _ = self.harry_task()
        with mock.patch.object(run_desk, "spawn"):
            worktree.create(self.conn, task["id"], str(self.linked), "fix/widget", fetch=False)
        record = gitops.read_record(task["id"])
        for common, reason in ((f"{self.tmp}/elsewhere/.git", "must be inside your home folder"),
                               (f"{self.repo}/git", "is not a main checkout's .git folder")):
            with self.subTest(common=common):
                with self.assertRaisesRegex(FleetError, reason):
                    gitops._check_record({**record, "common_dir": common,
                                          "git_dir": f"{common}/worktrees/{record['name']}"}, record["name"])
