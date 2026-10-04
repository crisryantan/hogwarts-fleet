"""Worktree toolchains: Node from .nvmrc, read-only dependency links, offline Go, and what desks get."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from hogwarts import pensieve

from fleet import config, gitops, review, run_desk, toolchain, verify, worktree
from fleet.safefs import FleetError
from tests_fleet.test_review_loop import HANDOFF, REAL_SANDBOX_ARGV, LoopCase


class ToolchainCase(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        self.nvm = self.home_dir / ".nvm/versions/node"
        for version in ("v24.14.0", "v24.19.0", "v25.8.1", "v22.14.0"):
            (self.nvm / version / "bin").mkdir(parents=True)

    def node_repo(self) -> None:
        self.write_file(self.repo / "package.json", '{"name": "web-app"}\n')
        self.write_file(self.repo / ".gitignore", "node_modules/\n")
        self.write_file(self.repo / ".nvmrc", "v24\n")
        self.git("add", "package.json", ".gitignore", ".nvmrc")
        self.git("commit", "-q", "-m", "node app")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.repo / "node_modules" / "left-pad").mkdir(parents=True)
        self.write_file(self.repo / "node_modules" / "left-pad" / "index.js", "module.exports = 1\n")


class NodeVersionTests(ToolchainCase):
    def test_nvmrc_picks_the_newest_installed_match(self):
        tree = self.tmp / "tree"
        tree.mkdir()
        for wanted, expected in (("v24\n", "v24.19.0"), ("24.14\n", "v24.14.0"), ("v25.8.1", "v25.8.1"),
                                 ("22", "v22.14.0")):
            with self.subTest(wanted=wanted):
                self.write_file(tree / ".nvmrc", wanted)
                self.assertEqual(toolchain.node_dir(str(tree)), f"{self.nvm}/{expected}")

    def test_no_match_or_a_strange_nvmrc_gives_no_node(self):
        tree = self.tmp / "tree"
        tree.mkdir()
        self.assertIsNone(toolchain.node_dir(str(tree)))
        for wanted in ("v26", "lts/iron", "../../etc", "v24; rm -rf /", ""):
            with self.subTest(wanted=wanted):
                self.write_file(tree / ".nvmrc", wanted)
                self.assertIsNone(toolchain.node_dir(str(tree)))

    def test_a_symlinked_version_is_never_chosen(self):
        os.symlink(self.tmp, self.nvm / "v24.99.0")
        tree = self.tmp / "tree"
        tree.mkdir()
        self.write_file(tree / ".nvmrc", "v24")
        self.assertEqual(toolchain.node_dir(str(tree)), f"{self.nvm}/v24.19.0")


class DependencyLinkTests(ToolchainCase):
    def test_a_node_worktree_borrows_node_modules_read_only(self):
        self.node_repo()
        _, task, _, created, _ = self.build()
        path = Path(created["worktree"])
        link = path / "node_modules"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.repo / "node_modules"))
        record = gitops.read_record(task["id"])
        self.assertEqual(record["links"], ["node_modules"])
        self.assertFalse(gitops.dirty(record))
        tools = toolchain.for_record(record)
        self.assertIn(str(self.repo / "node_modules"), tools["read"])
        self.assertEqual(tools["path"], [f"{self.nvm}/v24.19.0/bin"])

    def test_the_link_is_never_committed(self):
        self.node_repo()
        _, task, _, created, _ = self.build()
        self.write_file(Path(created["worktree"]) / "widget.txt", "widget\n")
        self.handoff(task, HANDOFF.format(task_id=task["id"]))
        self.enable("hermione")
        with self.fake_reviewer("PASS"):
            result = review.review_build(self.conn, task["id"])
        files = self.git("show", "--name-only", "--format=", result["sha"], cwd=created["worktree"]).split()
        self.assertEqual(files, ["widget.txt"])

    def test_removing_a_closed_task_drops_the_link_and_keeps_the_dependencies(self):
        self.node_repo()
        _, task, _, created, _ = self.build()
        path = Path(created["worktree"])
        self.assertIn("?? node_modules", self.git("status", "--porcelain", cwd=str(path)))
        pensieve.close_task(self.conn, task["id"], "abandoned")
        result = worktree.remove(self.conn, task["id"])
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(result["branch_kept"], "fix/widget")
        self.assertTrue((self.repo / "node_modules" / "left-pad" / "index.js").is_file())

    def test_removal_leaves_a_dirty_tree_and_a_link_it_did_not_make(self):
        self.node_repo()
        _, task, _, created, _ = self.build()
        path = Path(created["worktree"])
        pensieve.close_task(self.conn, task["id"], "abandoned")
        self.write_file(path / "widget.txt", "widget\n")
        with self.assertRaisesRegex(FleetError, "uncommitted"):
            worktree.remove(self.conn, task["id"])
        self.assertTrue((path / "node_modules").is_symlink())
        os.remove(path / "widget.txt")
        os.unlink(path / "node_modules")
        os.symlink(self.tmp, path / "node_modules")
        with self.assertRaisesRegex(FleetError, "worktree failed"):
            worktree.remove(self.conn, task["id"])
        self.assertEqual(os.readlink(path / "node_modules"), str(self.tmp))

    def test_a_repo_without_package_json_or_ignore_rule_gets_no_link(self):
        (self.repo / "node_modules").mkdir()
        self.assertEqual(toolchain.linkable(str(self.repo)), [])
        self.write_file(self.repo / "package.json", "{}\n")
        self.assertEqual(toolchain.linkable(str(self.repo)), [])

    def test_a_record_naming_another_folder_is_refused(self):
        self.node_repo()
        _, task, _, _, _ = self.build()
        path = self.office / "worktrees" / f"{task['id']}.json"
        data = json.loads(path.read_text())
        self.write_file(path, json.dumps({**data, "links": ["../.ssh"]}))
        with self.assertRaises(FleetError):
            gitops.read_record(task["id"])


class DeskToolTests(ToolchainCase):
    def test_harry_gets_node_on_path_and_read_only_dependencies(self):
        self.node_repo()
        _, task, owl_id, created, _ = self.build()
        plan = run_desk.build_plan(self.conn, "harry", owl_id)
        table = next(item for item in plan["argv"] if item.startswith("permissions.fleet-harry="))
        self.assertIn(f'"{self.repo}/node_modules"="read"', table)
        self.assertIn(f'"{self.nvm}/v24.19.0"="read"', table)
        self.assertNotIn(f'"{self.repo}/node_modules"="write"', table)
        self.assertTrue(plan["env"]["PATH"].startswith(f"{self.nvm}/v24.19.0/bin:"))

    def test_go_repos_work_offline(self):
        self.write_file(self.repo / "go.mod", "module example.com/app\n\ngo 1.26\n")
        self.git("add", "go.mod")
        self.git("commit", "-q", "-m", "go module")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        _, task, owl_id, _, _ = self.build()
        plan = run_desk.build_plan(self.conn, "harry", owl_id)
        self.assertEqual(plan["env"]["GOPROXY"], "off")
        policy = next(item for item in plan["argv"] if item.startswith("shell_environment_policy.set="))
        self.assertIn('GOPROXY="off"', policy)
        self.assertIn(f'"{self.home_dir}/go/pkg/mod"="read"',
                      next(item for item in plan["argv"] if item.startswith("permissions.fleet-harry=")))
        record = gitops.read_record(task["id"])
        argv = REAL_SANDBOX_ARGV(record, "/private/tmp/hogwarts-verify-x", "go test ./...")
        self.assertIn(f'"{self.home_dir}/go/pkg/mod"="read"', argv[argv.index("-c") + 1])
        self.assertEqual(verify.child_env("/private/tmp/x", record)["GOPROXY"], "off")

    def test_environment_values_that_need_quoting_are_refused(self):
        for value in ('a"b', "a b", "a\\b", ""):
            with self.subTest(value=value), self.assertRaises(FleetError):
                run_desk._toml_env({"GOFLAGS": value})
        with self.assertRaises(FleetError):
            run_desk._toml_env({"bad key": "x"})
