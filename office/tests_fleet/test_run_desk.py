from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest import mock

from hogwarts import capacity, ids, owlery, pensieve
from tests.support import NOW

from fleet import common, config, gitops, owl_post, review, run_desk, safefs, verify
from tests_fleet.support import FleetCase, claude_settings, fake_children
from tests_fleet.test_review_chain import Killed

REAL_POPEN = subprocess.Popen
REAL_START_CHILD = run_desk.start_child
BYPASS_WORDS = ("dangerously", "bypass", "skip-permissions", "danger-full-access", "approve-for-me", "yolo")


class RunDeskCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        never = mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no process may start"))
        never_run = mock.patch.object(subprocess, "run", side_effect=AssertionError("no process may start"))
        for patcher in (never, never_run):
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *args) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_desk.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def dry_run(self, desk: str, *extra) -> dict:
        code, out, err = self.main(desk, "--dry-run", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def deliver(self, sender: str, recipient: str, **fields) -> str:
        payload = {"to": recipient, "kind": "fyi", "subject": "review this", "body": "diff attached", **fields}
        self.write_owl(sender, f"{recipient}-{len(os.listdir(self.outbox(sender)))}.json", payload)
        [delivered] = owl_post.run_pass(self.conn, now=NOW)["delivered"]
        return delivered["owl_id"]

    def request(self, recipient: str, worktree: str = None) -> tuple:
        """McGonagall's request to a desk, optionally giving the recipient's own task a worktree."""
        with mock.patch.object(run_desk, "spawn"):
            owl_id = self.deliver("mcgonagall", recipient, kind="request", subject="build it")
        owl = next(item for item in owlery.inbox(self.conn, recipient) if item["id"] == owl_id)
        task_id = owlery.get_request(self.conn, owl["request_id"])["task_id"]
        if worktree is not None:
            (self.castle / "worktrees" / worktree).mkdir(mode=0o700)
            self.conn.execute("UPDATE tasks SET worktree = ? WHERE id = ?",
                              (f"{ids.WORKTREES_ROOT}/{worktree}", task_id))
            self.conn.commit()
        return owl_id, task_id

    def events_of(self, kind: str) -> list:
        return [event for event in self.events() if event["kind"] == kind]

    def assert_no_bypass(self, argv: list) -> None:
        for arg in argv:  # no element is free text: the brief and the prompt never go in argv
            for word in BYPASS_WORDS:
                self.assertNotIn(word, arg.lower())
        self.assertNotIn("bypassPermissions", argv)


class ClaudeDeskTests(RunDeskCase):
    def test_each_claude_desk_gets_the_restricted_command(self):
        for desk, model in (("hermione", "opus"), ("ron", "haiku"), ("portrait", "opus")):
            with self.subTest(desk=desk):
                plan = self.dry_run(desk)
                argv = plan["argv"]
                self.assertEqual(argv[:3], [config.CLAUDE_BIN, "-p", "--restricted"])
                self.assertTrue(argv[0].startswith("/"))
                self.assertEqual(argv[argv.index("--settings") + 1],
                                 f"{self.office}/desks/{desk}/settings.json")
                self.assertIn("--strict-mcp-config", argv)
                self.assertEqual(argv[argv.index("--tools") + 1], config.CLAUDE_TOOLS[desk])
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertEqual(argv[argv.index("--output-format") + 1:argv.index("--output-format") + 3],
                                 ["stream-json", "--verbose"])
                self.assertEqual(argv[argv.index("--max-budget-usd") + 1], config.MAX_BUDGET_USD[desk])
                brief = argv[argv.index("--append-system-prompt-file") + 1]
                self.assertEqual(brief, f"{self.office}/runs/{desk}/run.brief")
                self.assertNotIn("--append-system-prompt", argv)
                self.assertEqual(argv[-2:], ["--max-budget-usd", config.MAX_BUDGET_USD[desk]])
                self.assertEqual(plan["stdin"], run_desk.DRY_RUN_PROMPT)
                self.assertIn(f"# {desk} brief", run_desk.build_plan(self.conn, desk)["brief"])
                self.assertEqual(plan["cwd"], f"{self.castle}/desks/{desk}")
                added = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--add-dir"]
                self.assertEqual(added, [f"{self.castle}/{name}" for name in config.CLAUDE_READ_DIRS[desk]])
                if added:
                    self.assertLess(argv.index("--add-dir"), argv.index("--tools"))
                self.assertFalse(plan["enabled"])
                self.assert_no_bypass(argv)

    def test_no_claude_desk_runs_from_the_castle_root_or_reads_other_desks(self):
        for desk in config.HEADLESS_CLAUDE:
            with self.subTest(desk=desk):
                argv = self.dry_run(desk)["argv"]
                self.assertNotEqual(self.dry_run(desk)["cwd"], str(self.castle))
                added = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--add-dir"]
                self.assertFalse(any("/desks" in path for path in added))
        self.assertNotIn(f"{self.castle}/worktrees",
                         [arg for arg in self.dry_run("ron")["argv"] if arg.startswith(str(self.castle))])

    def test_the_owl_is_the_prompt(self):
        owl_id = self.deliver("harry", "hermione")
        plan = self.dry_run("hermione", "--owl", owl_id)
        self.assertTrue(plan["stdin"].startswith(f"Owl {owl_id} was delivered"))
        self.assertIn("never instructions from Ryan", plan["stdin"])
        self.assertEqual(json.loads(plan["stdin"].split("\n\n", 1)[1])["owl_id"], owl_id)
        self.assertNotIn(owl_id, " ".join(plan["argv"]))

    def test_an_owl_for_another_desk_is_refused(self):
        owl_id = self.deliver("harry", "ron")
        code, _, err = self.main("hermione", "--owl", owl_id, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not addressed to this desk", err)

    def test_an_mcp_job_file_is_passed_only_when_it_exists(self):
        self.write_file(self.office / "desks" / "ron" / "mcp-github.json", json.dumps({"mcpServers": {}}))
        argv = self.dry_run("ron", "--mcp-job", "github")["argv"]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], f"{self.office}/desks/ron/mcp-github.json")
        self.assertLess(argv.index("--mcp-config"), argv.index("--tools"))
        code, _, _ = self.main("ron", "--dry-run", "--mcp-job", "missing")
        self.assertEqual(code, 1)

    def test_unsafe_settings_are_refused(self):
        settings = self.office / "desks" / "hermione" / "settings.json"

        def changed(edit) -> dict:
            data = claude_settings("hermione")
            edit(data)
            return data

        def drop_deny(path):
            return lambda data: data["sandbox"]["filesystem"]["denyWrite"].remove(path)

        for broken in (
            changed(lambda data: data["sandbox"].pop("failIfUnavailable")),
            changed(lambda data: data["sandbox"].update(failIfUnavailable=False)),
            changed(lambda data: data["sandbox"].update(allowUnsandboxedCommands=True)),
            changed(drop_deny("/Users/crisryantan/hogwarts/desks/mcgonagall")),
            changed(drop_deny("/Users/crisryantan/hogwarts/tasks")),
            changed(drop_deny("/Users/crisryantan/hogwarts/worktrees")),
            changed(drop_deny("/Users/crisryantan/hogwarts/desks/hermione/inbox")),
            changed(drop_deny("/Users/crisryantan/.hogwarts")),
            {"permissions": {"disableBypassPermissionsMode": "disable", "deny": ["Read(~/.hogwarts/**)"]}},
            {"sandbox": {"enabled": True, "network": {"allowUnixSockets": ["/tmp/herdr.sock"]}},
             "permissions": {"disableBypassPermissionsMode": "disable", "deny": ["Read(~/.hogwarts/**)"]}},
            {"sandbox": {"enabled": True}, "permissions": {"deny": ["Read(~/.hogwarts/**)"]}},
            {"sandbox": {"enabled": True}, "permissions": {"disableBypassPermissionsMode": "disable", "deny": []}},
            {"sandbox": {"enabled": True}, "permissions": {"disableBypassPermissionsMode": "disable",
                                                            "defaultMode": "bypassPermissions",
                                                            "deny": ["Read(~/.hogwarts/**)"]}},
        ):
            with self.subTest(settings=broken):
                self.write_file(settings, json.dumps(broken))
                code, _, err = self.main("hermione", "--dry-run")
                self.assertEqual(code, 1)
                self.assertIn("settings", err)

    def test_a_symlinked_brief_is_refused(self):
        brief = self.office / "desks" / "ron" / "BRIEF.md"
        target = self.write_file(self.tmp / "evil.md", "# evil")
        os.unlink(brief)
        os.symlink(target, brief)
        code, _, err = self.main("ron", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("brief", err)


class CodexDeskTests(RunDeskCase):
    def test_codex_desks_ignore_user_config_and_execpolicy_rules(self):
        for desk in config.HEADLESS_CODEX:
            with self.subTest(desk=desk):
                argv = self.dry_run(desk)["argv"]
                self.assertEqual(argv[:4], [config.CODEX_BIN, "exec", "--ignore-user-config", "--ignore-rules"])

    def overrides(self, argv: list) -> list:
        return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]

    def profile(self, argv: list, desk: str) -> str:
        [table] = [item for item in self.overrides(argv) if item.startswith(f"permissions.fleet-{desk}=")]
        return table

    def test_codex_desks_never_get_the_sandbox_flag_or_add_dir(self):
        for desk in config.HEADLESS_CODEX:
            with self.subTest(desk=desk):
                argv = self.dry_run(desk)["argv"]
                self.assertNotIn("--sandbox", argv)
                self.assertNotIn("--add-dir", argv)
                self.assertIn(f'default_permissions="fleet-{desk}"', self.overrides(argv))

    def test_moody_runs_read_only_with_the_fleet_profile(self):
        with mock.patch.object(run_desk, "user_temp_dir", return_value="/private/var/folders/ab/cd/T"):
            plan = self.dry_run("moody")
        argv = plan["argv"]
        self.assertEqual(argv[:4], [config.CODEX_BIN, "exec", "--ignore-user-config", "--ignore-rules"])
        self.assertIn("--ephemeral", argv)
        last = argv[argv.index("--output-last-message") + 1]
        self.assertTrue(last.startswith(f"{self.office}/runs/moody/run-"))
        self.assertEqual(self.overrides(argv)[:3], ['model_reasoning_effort="high"', 'approval_policy="never"',
                                                    'shell_environment_policy.inherit="core"'])
        table = self.profile(argv, "moody")
        self.assertIn('":workspace_roots"={"."="read"}', table)
        self.assertIn(f'"{self.office}"="deny"', table)
        self.assertIn("network={enabled=false}", table)
        self.assertNotIn('="write"', table)
        self.assertIn(f'"{config.SHARED_TEMP_ROOT}"="deny"', table)
        self.assertIn('"/private/var/folders/ab/cd/T/xcrun_db"="read"', table)
        self.assertNotIn('"/private/var/folders/ab/cd/T"=', table)
        policy = next(item for item in argv if item.startswith("shell_environment_policy.set="))
        self.assertEqual(policy, 'shell_environment_policy.set={GIT_CONFIG_GLOBAL="/dev/null", '
                                 'GIT_NO_LAZY_FETCH="1", XDG_CONFIG_HOME="/dev/null"}')
        self.assertEqual(argv[-1], "-")
        self.assertTrue(plan["stdin"].startswith("# moody brief"))
        self.assertTrue(plan["stdin"].endswith("\n\n" + run_desk.DRY_RUN_PROMPT))
        self.assertIsNone(plan.get("brief"))
        self.assert_no_bypass(argv)

    def test_harry_writes_only_his_worktree_and_outbox(self):
        owl_id, _ = self.request("harry", worktree="tk-demo")
        with mock.patch.object(run_desk, "user_temp_dir", return_value="/private/var/folders/ab/cd/T"):
            plan = self.dry_run("harry", "--owl", owl_id)
        argv = plan["argv"]
        self.assertEqual(argv[:4], [config.CODEX_BIN, "exec", "--ignore-user-config", "--ignore-rules"])
        self.assertEqual(argv[argv.index("-C") + 1], f"{self.castle}/worktrees/tk-demo")
        table = self.profile(argv, "harry")
        writes = [entry for entry in table.split(", ") if entry.endswith('="write"') or '"."="write"' in entry]
        self.assertEqual(writes, ['":workspace_roots"={"."="write"}', f'"{self.castle}/desks/harry/outbox"="write"',
                                  '"/private/var/folders/ab/cd/T/hogwarts-harry"="write"'])
        self.assertIn(f'"{config.SHARED_TEMP_ROOT}"="deny"', table)
        self.assertIn('"/private/var/folders/ab/cd/T/xcrun_db"="read"', table)
        self.assertNotIn('"/private/var/folders/ab/cd/T"=', table)
        policy = next(item for item in argv if item.startswith("shell_environment_policy.set="))
        # Every temp the kit's own tests and xcrun make stays in his temp folder too, as in a verify run.
        self.assertIn('TMPDIR="/private/var/folders/ab/cd/T/hogwarts-harry"', policy)
        self.assertIn('TEST_TMP_ROOT="/private/var/folders/ab/cd/T/hogwarts-harry"', policy)
        self.assertIn('xcrun_db="/private/var/folders/ab/cd/T/hogwarts-harry/xcrun_db"', policy)
        self.assertIn('GIT_CONFIG_GLOBAL="/dev/null"', policy)
        self.assertIn('XDG_CONFIG_HOME="/dev/null"', policy)
        self.assertIn('GIT_NO_LAZY_FETCH="1"', policy)
        self.assertNotIn("GIT_CONFIG_GLOBAL", json.dumps(plan.get("env", {})))
        self.assertIn(f'"{self.castle}/CLAUDE.md"="read"', table)
        self.assertIn(f'"{self.castle}/AGENTS.md"="read"', table)
        self.assertIn(f'"{self.castle}/desks/harry"="read"', table)
        self.assertIn(f'"{self.castle}/tasks"="read"', table)
        self.assertIn(f'"{self.office}"="deny"', table)
        self.assertTrue(table.endswith("network={enabled=false}}"))
        self.assertIn(f"Owl {owl_id} was delivered", plan["stdin"])
        self.assertEqual(argv[-1], "-")
        self.assert_no_bypass(argv)

    def test_a_desk_that_writes_needs_its_own_temp_folder(self):
        with self.assertRaises(safefs.FleetError):
            run_desk.codex_permissions("harry", None)
        self.assertIn('"/private/var/folders/ab/cd/T/hogwarts-harry"="write"',
                      run_desk.codex_permissions("harry", None, (), "/private/var/folders/ab/cd/T/hogwarts-harry")[1])
        for name in ("../x", "Harry", "", "a" * 65, "x/y"):
            with self.subTest(name=name), self.assertRaises(safefs.FleetError):
                run_desk.desk_temp_dir(name)

    def test_fresh_temp_replaces_a_link_and_empties_a_folder(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.write_file(target / "keep.txt", "keep\n")
        path = self.tmp / "hogwarts-harry"
        os.symlink(target, path)
        run_desk.fresh_temp(str(path))
        self.assertFalse(path.is_symlink())
        self.assertEqual(os.listdir(path), [])
        self.assertTrue((target / "keep.txt").exists())
        (path / "sub").mkdir()
        self.write_file(path / "sub" / "old.txt", "old\n")
        run_desk.fresh_temp(str(path))
        self.assertEqual(os.listdir(path), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        os.rmdir(path)
        self.write_file(path, "a plain file where the folder goes\n")
        run_desk.fresh_temp(str(path))
        self.assertTrue(path.is_dir())
        self.assertEqual(os.listdir(path), [])

    def test_the_repo_git_folder_is_readable_only_from_the_office_record(self):
        owl_id, task_id = self.request("harry", worktree="tk-demo")
        repo = self.tmp / "repo"
        (repo / ".git" / "worktrees" / "tk-demo").mkdir(parents=True)
        self.assertNotIn('/.git"="read"', self.profile(self.dry_run("harry", "--owl", owl_id)["argv"], "harry"))
        (self.office / "worktrees").mkdir(mode=0o700)
        record = {"name": "tk-demo", "task_id": task_id, "path": f"{self.castle}/worktrees/tk-demo",
                  "repo_dir": str(repo), "common_dir": f"{repo}/.git", "git_dir": f"{repo}/.git/worktrees/tk-demo",
                  "branch": "fix/widget", "base": "origin/main", "repo": "acme/web-app"}
        self.write_file(self.office / "worktrees" / "tk-demo.json", json.dumps(record))
        table = self.profile(self.dry_run("harry", "--owl", owl_id)["argv"], "harry")
        self.assertIn(f'"{repo}/.git"="read"', table)

    def test_a_profile_path_with_unsafe_characters_is_refused(self):
        for path in ('/tmp/a"b', "/tmp/a b", "relative", "/tmp/../etc", "/tmp//x"):
            with self.subTest(path=path), mock.patch.object(config, "CODEX_EXTRA_READS", (path,)):
                code, _, err = self.main("harry", "--dry-run")
                self.assertEqual(code, 1)
                self.assertIn("codex profile path", err)

    def test_another_desks_task_never_gives_harry_its_worktree(self):
        (self.castle / "worktrees" / "hermione-task").mkdir(mode=0o700)
        theirs = pensieve.create_task(self.conn, "hermione", "her own work",
                                      worktree=f"{ids.WORKTREES_ROOT}/hermione-task", now=NOW)
        owl_id = self.deliver("ron", "harry", task_id=theirs["id"])
        plan = self.dry_run("harry", "--owl", owl_id)
        self.assertEqual(plan["cwd"], f"{self.castle}/desks/harry/work")
        self.assertEqual(plan["argv"][plan["argv"].index("-C") + 1], f"{self.castle}/desks/harry/work")

    def test_a_request_task_of_another_desk_gives_no_worktree(self):
        _, task_id = self.request("moody", worktree="moody-task")
        self.conn.execute("UPDATE requests SET recipient = 'hermione' WHERE task_id = ?", (task_id,))
        self.conn.commit()
        owl_id, _ = self.request("harry")
        self.conn.execute("UPDATE requests SET task_id = ? WHERE id = (SELECT request_id FROM owls WHERE id = ?)",
                          (task_id, owl_id))
        self.conn.commit()
        self.assertEqual(self.dry_run("harry", "--owl", owl_id)["cwd"], f"{self.castle}/desks/harry/work")

    def test_harry_without_a_worktree_never_runs_in_his_desk_folder(self):
        owl_id, _ = self.request("harry")
        for plan in (self.dry_run("harry"), self.dry_run("harry", "--owl", owl_id)):
            with self.subTest(owl=plan["owl_id"]):
                self.assertEqual(plan["cwd"], f"{self.castle}/desks/harry/work")
                self.assertFalse(f"{self.castle}/desks/harry/inbox".startswith(plan["cwd"] + "/"))

    def test_profiles_that_widen_the_sandbox_are_refused(self):
        profile = self.office / "desks" / "harry" / "codex.toml"
        for text in ('sandbox_mode = "danger-full-access"\n', 'sandbox_mode = "workspace-write"\n',
                     "[sandbox_workspace_write]\nnetwork_access = true\n",
                     "[sandbox_workspace_write]\nnetwork_access = false\n",
                     'default_permissions = "mine"\n', '[permissions.mine]\ndescription = "x"\n',
                     'permissions.mine.description = "x"\n',
                     'notify = "bypass-me"\n', "[[profiles]]\nname = 1\n",
                     'model = """multi"""\n', "key = value with spaces\n"):
            with self.subTest(text=text):
                self.write_file(profile, text)
                code, _, err = self.main("harry", "--dry-run")
                self.assertEqual(code, 1)
                self.assertIn("codex profile", err)


class GuardTests(RunDeskCase):
    def test_only_headless_desks_can_be_launched(self):
        for desk in ("mcgonagall", "snape", "ryan", "ryan-claude-1", "owl-post", "map", "gringotts", "ollivander",
                     "voldemort"):
            with self.subTest(desk=desk):
                code, _, err = self.main(desk, "--dry-run")
                self.assertEqual(code, 1)

    def test_the_guard_refuses_bypass_flags_anywhere_in_argv(self):
        for flag in ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                     "--allow-dangerously-skip-permissions", "--approve-for-me", "danger-full-access",
                     "bypassPermissions"):
            with self.subTest(flag=flag):
                with self.assertRaises(safefs.FleetError):
                    run_desk.guard(["/bin/tool", flag, "-"])
                with self.assertRaises(safefs.FleetError):  # no argv holds free text, so the last element too
                    run_desk.guard(["/bin/tool", "-", flag])
        run_desk.guard(["/bin/tool", "--append-system-prompt-file", "/office/runs/ron/run-0.brief", "-"])
        with self.assertRaises(safefs.FleetError):
            run_desk.guard(["tool", "-"])

    def test_a_registry_family_mismatch_is_refused(self):
        with mock.patch.object(config, "HEADLESS_CLAUDE", ("hermione", "ron", "portrait", "harry")), \
                mock.patch.object(config, "HEADLESS_CODEX", ("moody",)):
            with self.assertRaises(safefs.FleetError):
                run_desk.build_plan(self.conn, "harry")


class StdinTests(RunDeskCase):
    """No brief or prompt is ever in a launch's argv, where a process listing shows it: the prompt goes through a pipe
    on the process's stdin, and a Claude desk's brief through a file only Ryan can read, there only while it runs."""

    def runs_dir(self, desk: str) -> Path:
        return self.office / "runs" / desk

    def test_no_launch_argv_holds_the_brief_or_the_prompt(self):
        for desk in ("hermione", "ron", "harry", "moody"):
            with self.subTest(desk=desk):
                owl_id = self.deliver("mcgonagall", desk, subject="SUBJECT-MARKER", body="BODY-MARKER")
                plan = run_desk.build_plan(self.conn, desk, owl_id)
                flat = "\n".join(plan["argv"])
                for marker in (owl_id, "SUBJECT-MARKER", "BODY-MARKER", "was delivered", f"# {desk} brief"):
                    self.assertNotIn(marker, flat)
                    self.assertIn(marker, plan["stdin"] + (plan["brief"] or ""))
                self.assertIn("BODY-MARKER", plan["stdin"])
                if desk in config.HEADLESS_CLAUDE:
                    self.assertIn(f"# {desk} brief", plan["brief"])
                    self.assertNotIn(f"# {desk} brief", plan["stdin"])
                else:
                    self.assertTrue(plan["stdin"].startswith(f"# {desk} brief"))
                    self.assertEqual(plan["argv"][-1], "-")

    def cat_run(self, desk: str, stale: bool = False) -> tuple:
        """A real run of desk on a new owl, whose process is /bin/cat: what it read from stdin lands in the run's
        output. Returns (owl id, run result, what the brief file held and its mode while the process started)."""
        self.enable(desk)
        owl_id, _ = self.request(desk)
        if stale:  # a brief a killed run left in slot 0
            self.runs_dir(desk).mkdir(mode=0o700, exist_ok=True)
            self.write_file(self.runs_dir(desk) / "run.brief", "stale brief")
        seen = {}

        def start(argv, **kwargs):
            if "--append-system-prompt-file" in argv:
                path = argv[argv.index("--append-system-prompt-file") + 1]
                seen["brief"] = Path(path).read_text()
                seen["mode"] = os.lstat(path).st_mode & 0o777
            seen["argv"] = argv
            with mock.patch.object(subprocess, "Popen", REAL_POPEN):
                return REAL_START_CHILD(["/bin/cat"], **kwargs)

        with mock.patch.object(run_desk, "start_child", side_effect=start):
            result = run_desk.run(self.conn, desk, owl_id, now=NOW)
        return owl_id, result, seen

    def test_a_claude_run_reads_its_prompt_on_stdin_and_its_brief_from_a_private_file_removed_after(self):
        owl_id, result, seen = self.cat_run("hermione")
        out = (self.runs_dir("hermione") / f"{result['run_id']}.out").read_text()
        self.assertTrue(out.startswith(f"Owl {owl_id} was delivered"))
        self.assertIn("# hermione brief", seen["brief"])
        self.assertEqual(seen["mode"], 0o600)
        self.assertNotIn(owl_id, " ".join(seen["argv"]))
        self.assertEqual([name for name in os.listdir(self.runs_dir("hermione")) if name.endswith(".brief")], [])

    def test_a_brief_a_killed_run_left_in_the_slot_is_replaced_then_removed(self):
        _, _, seen = self.cat_run("hermione", stale=True)
        self.assertIn("# hermione brief", seen["brief"])
        self.assertFalse((self.runs_dir("hermione") / "run.brief").exists())

    def test_each_slot_has_its_own_brief_file(self):
        argv = run_desk.build_plan(self.conn, "hermione", slot=1)["argv"]
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], f"{self.office}/runs/hermione/run.slot1.brief")

    def test_a_codex_run_reads_its_brief_and_prompt_on_stdin(self):
        owl_id, result, seen = self.cat_run("moody")
        out = (self.runs_dir("moody") / f"{result['run_id']}.out").read_text()
        self.assertTrue(out.startswith("# moody brief"))
        self.assertIn(f"\n\nOwl {owl_id} was delivered", out)
        self.assertNotIn("brief", seen)
        self.assertEqual(seen["argv"][-1], "-")

    def test_a_start_that_fails_still_removes_the_brief_file(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        with mock.patch.object(run_desk, "start_child", side_effect=safefs.FleetError("no input")), \
                self.assertRaises(safefs.FleetError):
            run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        self.assertEqual([name for name in os.listdir(self.runs_dir("hermione")) if name.endswith(".brief")], [])

    def started(self, argv: list, data: bytes) -> list:
        """start_child on a real process that will not take its input: raises FleetError. Returns the process."""
        children = []

        def popen(*args, **kwargs):
            children.append(REAL_POPEN(*args, **kwargs))
            self.addCleanup(children[-1].wait)
            return children[-1]

        with mock.patch.object(subprocess, "Popen", side_effect=popen), \
                self.assertRaisesRegex(safefs.FleetError, "whole input"):
            REAL_START_CHILD(argv, cwd="/", env={}, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             pass_fds=())
        return children

    def test_a_dead_reader_is_killed_and_reaped(self):
        [child] = self.started(["/bin/sh", "-c", "exec 0<&-; exec /bin/sleep 60"], b"x" * (1 << 20))
        self.assertIsNotNone(child.returncode)

    def test_a_reader_that_never_takes_its_input_is_killed_at_the_timeout(self):
        with mock.patch.object(run_desk, "STDIN_TIMEOUT_SECONDS", 0.5):
            began = time.monotonic()
            [child] = self.started(["/bin/sleep", "60"], b"x" * (1 << 20))
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(child.returncode, -signal.SIGKILL)


class BuildRunTaskLockTests(RunDeskCase):
    """A build desk's run on its own task holds the task's review lock from before it waits for a slot until its
    process has exited, and the process inherits it, so no review of the task runs beside it."""

    def setUp(self) -> None:
        super().setUp()
        self.enable("harry")
        self.owl_id, self.task_id = self.request("harry", worktree="tk-one")

    def review_free(self) -> bool:
        try:
            with review.task_review_lock(self.task_id):
                return True
        except safefs.FleetError:
            return False

    def test_a_build_run_holds_its_task_review_lock_until_its_process_has_exited(self):
        seen = {}

        def desk(argv, **kwargs):
            seen.update(free=self.review_free(), inherited=[os.fstat(fd).st_ino for fd in kwargs["pass_fds"]])
            return subprocess.CompletedProcess(argv, 0)

        with fake_children(desk):
            self.assertEqual(run_desk.run(self.conn, "harry", self.owl_id, now=NOW)["exit_code"], 0)
        self.assertFalse(seen["free"])
        self.assertIn(os.stat(self.office / "locks" / f"review-{self.task_id}.lock").st_ino, seen["inherited"])
        self.assertTrue(self.review_free())

    def test_a_build_run_is_refused_while_a_review_of_its_task_runs(self):
        with review.task_review_lock(self.task_id), fake_children() as started:
            with self.assertRaisesRegex(safefs.FleetError, f"a review of task {self.task_id} is running"):
                run_desk.run(self.conn, "harry", self.owl_id, now=NOW)
        started.assert_not_called()
        self.assertEqual(capacity.list_launches(self.conn, "harry"), [])

    def test_a_build_run_keeps_the_lock_it_was_handed_and_refuses_any_other_fd(self):
        seen = {}

        def desk(argv, **kwargs):
            seen["fds"] = kwargs["pass_fds"]
            return subprocess.CompletedProcess(argv, 0)

        with review.task_review_lock(self.task_id) as lock_fd, fake_children(desk):
            self.assertEqual(run_desk.run(self.conn, "harry", self.owl_id, now=NOW, task_lock_fd=lock_fd)["exit_code"],
                             0)
            self.assertIn(lock_fd, seen["fds"])
        with run_desk.slot_lock("ron", 0) as other, fake_children() as started:
            with self.assertRaisesRegex(safefs.FleetError, "is not task .* review lock"):
                run_desk.run(self.conn, "harry", self.owl_id, now=NOW, task_lock_fd=other.fd)
        started.assert_not_called()

    def sleeper(self, kwargs: dict) -> subprocess.Popen:
        """A real process in place of the desk's, inheriting what the run's would: /bin/sleep, never a desk CLI."""
        child = REAL_POPEN(["/bin/sleep", "60"], stdin=subprocess.DEVNULL, stdout=kwargs["stdout"],
                                 stderr=kwargs["stderr"], pass_fds=kwargs["pass_fds"], close_fds=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def signalled_start(self, children: list):
        """start_child, where SIGTERM lands once the process has started and before its handle is returned."""

        def start(argv, **kwargs):
            children.append(self.sleeper(kwargs))
            os.kill(os.getpid(), signal.SIGTERM)
            return children[-1]

        return mock.patch.object(run_desk, "start_child", side_effect=start)

    def test_a_signal_as_the_desk_process_starts_ends_it_before_the_review_lock_is_let_go(self):
        children = []
        with common.ended_by_signals(), self.signalled_start(children), self.assertRaises(SystemExit):
            run_desk.run(self.conn, "harry", self.owl_id, now=NOW)
        [child] = children
        self.assertIsNotNone(child.poll(), "the desk process outlived the review lock it held")
        self.assertTrue(self.review_free())

    def test_a_signal_as_a_handed_run_starts_ends_it_before_the_handed_lock_is_let_go(self):
        children = []
        with review.task_review_lock(self.task_id) as lock_fd:
            with common.ended_by_signals(), self.signalled_start(children), self.assertRaises(SystemExit):
                run_desk.run(self.conn, "harry", self.owl_id, now=NOW, task_lock_fd=lock_fd)
            [child] = children
            self.assertIsNotNone(child.poll(), "the desk process outlived the review lock it was handed")

    def test_a_signal_before_the_wait_begins_still_ends_the_desk_process(self):
        children = []

        def start(argv, **kwargs):
            children.append(self.sleeper(kwargs))
            return children[-1]

        with mock.patch.object(run_desk, "start_child", side_effect=start), \
                mock.patch.object(run_desk, "wait_child", side_effect=SystemExit(128 + signal.SIGTERM)), \
                self.assertRaises(SystemExit):
            run_desk.run(self.conn, "harry", self.owl_id, now=NOW)
        [child] = children
        self.assertIsNotNone(child.poll(), "the desk process outlived the review lock it held")
        self.assertTrue(self.review_free())


class RunEndTests(RunDeskCase):
    """Every run keeps how its process ended in runs/<desk>/<run_id>.end, before it records anything else, so a caller
    killed before it kept the run's result can still read how it ended (run_end)."""

    def setUp(self) -> None:
        super().setUp()
        self.enable("hermione")
        self.owl_id, _ = self.request("hermione")

    def run_id(self) -> str:
        """The run id of the desk's latest launch."""
        return capacity.list_launches(self.conn, "hermione")[-1]["run_id"]

    def end_file(self) -> Path:
        return self.office / "runs" / "hermione" / f"{self.run_id()}.end"

    def test_a_run_keeps_its_exit_code_and_vendor_limit_before_its_usage(self):
        for exit_code, limit in ((0, None), (3, None), (1, "claude_plan")):
            with self.subTest(exit_code=exit_code, limit=limit), mock.patch.object(run_desk, "spawn"):
                owl_id = self.deliver("mcgonagall", "hermione", subject=f"read this {exit_code} {limit}")
                seen, real = [], capacity.record_launch_usage

                def recorded(conn, run_id, *args, **kwargs):
                    seen.append(json.loads(self.end_file().read_text()))
                    return real(conn, run_id, *args, **kwargs)

                with fake_children(returncode=exit_code), \
                        mock.patch.object(run_desk, "plan_limit", return_value=limit), \
                        mock.patch.object(capacity, "record_launch_usage", side_effect=recorded):
                    run_desk.run(self.conn, "hermione", owl_id, now=NOW)
                self.assertEqual(seen, [{"run_id": self.run_id(), "exit_code": exit_code, "cap_source": limit}])
                self.assertEqual(run_desk.run_end("hermione", self.run_id()),
                                 {"exit_code": exit_code, "cap_source": limit})

    def test_a_run_killed_here_or_never_started_keeps_that_too(self):
        def desk(argv, **kwargs):
            raise SystemExit(128 + signal.SIGTERM)  # the signal lands while the run waits for its process

        with fake_children(desk), self.assertRaises(SystemExit):
            run_desk.run(self.conn, "hermione", self.owl_id, now=NOW)
        self.assertEqual(json.loads(self.end_file().read_text())["exit_code"], -9)
        for raised in (FileNotFoundError("no such binary"), SystemExit(128 + signal.SIGTERM)):
            with self.subTest(raised=raised), mock.patch.object(run_desk, "spawn"):
                owl_id = self.deliver("mcgonagall", "hermione", subject=f"read this {type(raised).__name__}")
                with mock.patch.object(run_desk, "start_child", side_effect=raised), self.assertRaises(type(raised)):
                    run_desk.run(self.conn, "hermione", owl_id, now=NOW)
                self.assertEqual(run_desk.run_end("hermione", self.run_id()), {"exit_code": None, "cap_source": None})

    def test_a_run_with_no_end_kept_reads_as_none_and_a_record_that_does_not_read_whole_is_refused(self):
        with fake_children(), mock.patch.object(run_desk, "_keep_end", side_effect=Killed()), \
                self.assertRaises(Killed):
            run_desk.run(self.conn, "hermione", self.owl_id, now=NOW)
        self.assertIsNone(run_desk.run_end("hermione", self.run_id()))
        path = self.end_file()
        for text in ("{", json.dumps({"run_id": "run-" + "0" * 16, "exit_code": 0, "cap_source": None}),
                     json.dumps({"run_id": self.run_id(), "exit_code": 0, "cap_source": "plan"}),
                     json.dumps({"run_id": self.run_id(), "exit_code": True, "cap_source": None})):
            with self.subTest(text=text):
                self.write_file(path, text + "\n")
                with self.assertRaises(safefs.Unsafe):
                    run_desk.run_end("hermione", self.run_id())
        os.unlink(path)
        os.symlink(self.tmp / "elsewhere", path)
        with self.assertRaises(safefs.Unsafe):
            run_desk.run_end("hermione", self.run_id())

    def test_a_claude_run_whose_result_the_read_window_cut_is_charged_its_budget(self):
        def desk(argv, **kwargs):
            event = {"type": "result", "subtype": "success", "is_error": False, "result": "x" * 5000,
                     "total_cost_usd": 0.01}
            os.write(kwargs["stdout"], (json.dumps(event) + "\n").encode("utf-8"))
            return subprocess.CompletedProcess(argv, 0)

        with fake_children(desk), mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", 1024):
            result = run_desk.run(self.conn, "hermione", self.owl_id, now=NOW)
        self.assertEqual((result["exit_code"], result["cost_usd"], result.get("spend_unknown")),
                         (0, float(config.MAX_BUDGET_USD["hermione"]), True))


class LockInheritanceTests(FleetCase):
    """A killed run_desk or review leaves its desk process running. That process inherited the lock fds, so the
    lock stays held until it ends too. Real processes: a Python holder and /bin/sleep, never a desk CLI."""

    def test_a_desk_lock_handed_to_the_desk_process_outlives_a_killed_holder(self):
        holder = ("import os, subprocess, sys\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from fleet import config, run_desk\n"
                  "config.OFFICE_ROOT = sys.argv[2]\n"
                  "with run_desk.desk_lock('ron', wait=False) as slot:\n"
                  "    child = subprocess.Popen(['/bin/sleep', '60'], pass_fds=(slot.fd,),\n"
                  "                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "    print(child.pid, flush=True)\n"
                  "    os.kill(os.getpid(), 9)\n")
        root = str(Path(__file__).resolve().parents[1])
        done = subprocess.run(["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty",
                               "-c", holder, root, config.OFFICE_ROOT], capture_output=True, timeout=60, check=False)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
        orphan = int(done.stdout.decode().strip())
        try:
            with self.assertRaises(safefs.Busy):
                with run_desk.desk_lock("ron", wait=False):
                    pass
        finally:
            os.kill(orphan, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while True:
            try:
                with run_desk.desk_lock("ron", wait=False):
                    break
            except safefs.Busy:
                self.assertLess(time.monotonic(), deadline, "the lock was never freed after its holder ended")
                time.sleep(0.1)


class RealRunTests(RunDeskCase):
    def real_run(self, desk: str, owl_id: str, returncode: int) -> tuple:
        with fake_children(returncode=returncode) as started:
            code, out, err = self.main(desk, "--owl", owl_id)
        return code, out, err, started

    def test_a_real_run_is_refused_while_the_desk_is_disabled(self):
        owl_id = self.deliver("harry", "hermione")
        code, out, err = self.main("hermione", "--owl", owl_id)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not enabled", err)
        self.assertEqual([(event["desk"], event["verdict"]) for event in self.events_of("rundesk.failed")],
                         [("hermione", "headmaster")])

    def test_a_clean_run_acks_its_owl_and_records_usage(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        code, _, err, started = self.real_run("hermione", owl_id, 0)
        self.assertEqual(code, 0, err)
        self.assertEqual(started.call_args.kwargs["cwd"], f"{self.castle}/desks/hermione")
        # Its slot and update locks, and its own run lock (Hermione holds spend), held while it runs.
        self.assertEqual(len(started.call_args.kwargs["pass_fds"]), 3)
        self.assertEqual(owlery.inbox(self.conn, "hermione"), [])
        self.assertEqual([row["runs"] for row in pensieve.summary(self.conn)], [1])
        [launch] = capacity.list_launches(self.conn, "hermione")
        [metric] = self.conn.execute("SELECT id, run_id FROM metrics").fetchall()
        self.assertEqual((launch["metric_id"], launch["run_id"]), (metric["id"], metric["run_id"]))
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_a_failed_run_leaves_its_owl_and_raises_an_event(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        code, _, _, _ = self.real_run("hermione", owl_id, 3)
        self.assertEqual(code, 1)
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "hermione")], [owl_id])
        self.assertEqual(len(self.events_of("rundesk.failed")), 1)

    def test_harry_gets_a_fresh_private_temp_at_launch_but_not_in_a_dry_run(self):
        user_temp = self.tmp / "usertemp"
        (user_temp / "hogwarts-harry").mkdir(parents=True)
        self.write_file(user_temp / "hogwarts-harry" / "stale.txt", "from an earlier run\n")
        self.enable("harry")
        owl_id, _ = self.request("harry", worktree="tk-demo")
        with mock.patch.object(run_desk, "user_temp_dir", return_value=str(user_temp)):
            self.dry_run("harry", "--owl", owl_id)
            self.assertTrue((user_temp / "hogwarts-harry" / "stale.txt").exists())
            code, _, err, started = self.real_run("harry", owl_id, 0)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.listdir(user_temp / "hogwarts-harry"), [])
        self.assertEqual(started.call_args.kwargs["env"]["TMPDIR"], f"{user_temp}/hogwarts-harry")

    def test_a_codex_run_without_a_worktree_gets_its_own_work_folder(self):
        self.enable("moody")
        owl_id, _ = self.request("moody")
        code, _, err, started = self.real_run("moody", owl_id, 0)
        self.assertEqual(code, 0, err)
        self.assertEqual(started.call_args.kwargs["cwd"], f"{self.castle}/desks/moody/work")
        self.assertTrue((self.castle / "desks" / "moody" / "work").is_dir())

    def test_daily_caps_come_from_the_metrics(self):
        for index in range(config.DAILY_RUN_CAP["portrait"] - 1):
            pensieve.add_metric(self.conn, "portrait", f"run-{index}", "opus", 1, 1, 0, 0.1, 10, ts=NOW - 60)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "portrait", NOW))
        pensieve.add_metric(self.conn, "portrait", "run-last", "opus", 1, 1, 0, 0.1, 10, ts=NOW - 60)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "portrait", NOW), "daily run cap reached")
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "portrait", NOW + config.DAY_SECONDS))
        pensieve.add_metric(self.conn, "ron", "run-big", "haiku", 1, 1, 0, config.DAILY_SPEND_CAP_USD["ron"], 10,
                            ts=NOW - 60)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "ron", NOW), "daily spend cap reached")

    def test_a_capped_desk_is_not_launched(self):
        self.enable("ron")
        owl_id, _ = self.request("ron")
        for index in range(config.DAILY_RUN_CAP["ron"]):
            pensieve.add_metric(self.conn, "ron", f"run-{index}", "haiku", 1, 1, 0, 0.0, 10)
        code, _, err, started = self.real_run("ron", owl_id, 0)
        self.assertEqual(code, 1)
        self.assertIn("daily run cap", err)
        started.assert_not_called()
        self.assertEqual(len(self.events_of("rundesk.cap")), 1)
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_a_real_run_needs_an_owl(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run_desk.main(["hermione"])

    def test_spawn_refuses_a_disabled_desk_before_starting_anything(self):
        with self.assertRaises(safefs.FleetError):
            run_desk.spawn("hermione", "owl_0123456789abcdef")

    def test_the_desk_lock_is_exclusive_and_a_second_run_waits(self):
        with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as fd:
            with safefs.held_lock(fd, "desk-ron.lock", blocking=False):
                with self.assertRaises(safefs.Busy):
                    with safefs.held_lock(fd, "desk-ron.lock", blocking=False):
                        pass
                with self.assertRaises(safefs.Busy):
                    with safefs.held_lock(fd, "desk-ron.lock", blocking=True, timeout=0.2):
                        pass
            with safefs.held_lock(fd, "desk-ron.lock", blocking=True, timeout=0.2):
                pass

    def test_usage_parsing(self):
        claude = json.dumps({"type": "result", "total_cost_usd": 0.42, "usage": {
            "input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 9000,
            "output_tokens": 700}}).encode()
        self.assertEqual(run_desk.parse_claude_usage(claude),
                         {"input_tokens": 150, "output_tokens": 700, "cache_read_tokens": 9000, "cost_usd": 0.42,
                          "is_error": False, "subtype": None})
        self.assertEqual(run_desk.parse_claude_usage(b"not json")["input_tokens"], 0)
        codex = b"\n".join(json.dumps(event).encode() for event in (
            {"type": "thread.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 2}},
        ))
        self.assertEqual(run_desk.parse_codex_usage(codex),
                         {"input_tokens": 15, "output_tokens": 5, "cache_read_tokens": 5, "cost_usd": 0.0})

    def test_the_run_folder_must_be_a_plain_castle_folder(self):
        run_desk.require_castle_dir(str(self.castle))
        run_desk.require_castle_dir(f"{self.castle}/desks/harry")
        os.symlink(self.tmp, self.castle / "worktrees" / "escape")
        for path in (str(self.tmp), f"{self.castle}/worktrees/escape", f"{self.castle}/worktrees/missing",
                     f"{self.castle}x/desks"):
            with self.subTest(path=path):
                with self.assertRaises(safefs.FleetError):
                    run_desk.require_castle_dir(path)

    def test_the_child_environment_is_fixed(self):
        env = run_desk.child_env()
        self.assertEqual(sorted(env), ["GIT_NO_LAZY_FETCH", "HOME", "LANG", "LOGNAME", "PATH", "RTK_DISABLED", "SHELL",
                                       "USER"])
        self.assertEqual(env["USER"], env["LOGNAME"])
        self.assertEqual(env["HOME"].rsplit("/", 1)[1], env["USER"])

    def test_no_git_the_fleet_runs_fetches_a_missing_object_silently(self):
        # A partial clone would fetch an object it lacks from its remote, quietly and with Ryan's credentials.
        self.assertEqual(config.GIT_NO_LAZY_FETCH_ENV, {"GIT_NO_LAZY_FETCH": "1"})
        for name, env in (("fleet git", gitops.child_env()), ("a desk", run_desk.child_env()),
                          ("a verify check", verify.child_env("/private/tmp/x"))):
            with self.subTest(env=name):
                self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
        # What gitops really hands git, not only what it builds.
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"true\n", stderr=b"")
        with mock.patch.object(subprocess, "run", return_value=done) as ran:
            gitops.git(["rev-parse", "--is-shallow-repository"], "/private/tmp/x/.git")
        self.assertEqual(ran.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
