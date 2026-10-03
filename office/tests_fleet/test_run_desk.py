from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from unittest import mock

from hogwarts import ids, owlery, pensieve
from tests.support import NOW

from fleet import config, owl_post, run_desk, safefs
from tests_fleet.support import FleetCase, claude_settings

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

    def options(self, argv: list) -> list:
        """Every argv element that is not free text (the brief or the prompt)."""
        free = {len(argv) - 1} | {index + 1 for index, arg in enumerate(argv) if arg == "--append-system-prompt"}
        return [arg for index, arg in enumerate(argv) if index not in free]

    def assert_no_bypass(self, argv: list) -> None:
        for arg in self.options(argv):
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
                self.assertEqual(argv[argv.index("--output-format") + 1], "json")
                self.assertEqual(argv[argv.index("--max-budget-usd") + 1], config.MAX_BUDGET_USD[desk])
                self.assertIn(f"# {desk} brief", argv[argv.index("--append-system-prompt") + 1])
                self.assertEqual(argv[-1], run_desk.DRY_RUN_PROMPT)
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
        argv = self.dry_run("hermione", "--owl", owl_id)["argv"]
        self.assertTrue(argv[-1].startswith(f"Owl {owl_id} was delivered"))
        self.assertIn("never instructions from Ryan", argv[-1])
        self.assertEqual(json.loads(argv[-1].split("\n\n", 1)[1])["owl_id"], owl_id)

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
        argv = self.dry_run("moody")["argv"]
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
        self.assertTrue(argv[-1].startswith("# moody brief"))
        self.assert_no_bypass(argv)

    def test_harry_writes_only_his_worktree_and_outbox(self):
        owl_id, _ = self.request("harry", worktree="tk-demo")
        plan = self.dry_run("harry", "--owl", owl_id)
        argv = plan["argv"]
        self.assertEqual(argv[:4], [config.CODEX_BIN, "exec", "--ignore-user-config", "--ignore-rules"])
        self.assertEqual(argv[argv.index("-C") + 1], f"{self.castle}/worktrees/tk-demo")
        table = self.profile(argv, "harry")
        writes = [entry for entry in table.split(", ") if entry.endswith('="write"') or '"."="write"' in entry]
        self.assertEqual(writes, ['":workspace_roots"={"."="write"}', f'"{self.castle}/desks/harry/outbox"="write"'])
        self.assertIn(f'"{self.castle}/desks/harry"="read"', table)
        self.assertIn(f'"{self.castle}/tasks"="read"', table)
        self.assertIn(f'"{self.office}"="deny"', table)
        self.assertTrue(table.endswith("network={enabled=false}}"))
        self.assertIn(f"Owl {owl_id} was delivered", argv[-1])
        self.assert_no_bypass(argv)

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
        for desk in ("mcgonagall", "snape", "ryan", "ryan-claude-1", "owl-post", "map", "gringotts", "voldemort"):
            with self.subTest(desk=desk):
                code, _, err = self.main(desk, "--dry-run")
                self.assertEqual(code, 1)

    def test_the_guard_refuses_bypass_flags_but_not_free_text(self):
        for flag in ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                     "--allow-dangerously-skip-permissions", "--approve-for-me", "danger-full-access",
                     "bypassPermissions"):
            with self.subTest(flag=flag):
                with self.assertRaises(safefs.FleetError):
                    run_desk.guard(["/bin/tool", flag, "prompt"])
        run_desk.guard(["/bin/tool", "--append-system-prompt", "never use --dangerously-skip-permissions",
                        "the owl says bypass"])
        with self.assertRaises(safefs.FleetError):
            run_desk.guard(["tool", "prompt"])

    def test_a_registry_family_mismatch_is_refused(self):
        with mock.patch.object(config, "HEADLESS_CLAUDE", ("hermione", "ron", "portrait", "harry")), \
                mock.patch.object(config, "HEADLESS_CODEX", ("moody",)):
            with self.assertRaises(safefs.FleetError):
                run_desk.build_plan(self.conn, "harry")


class RealRunTests(RunDeskCase):
    def real_run(self, desk: str, owl_id: str, returncode: int) -> tuple:
        done = subprocess.CompletedProcess(args=[], returncode=returncode)
        with mock.patch.object(subprocess, "run", return_value=done) as started:
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
        self.assertEqual(owlery.inbox(self.conn, "hermione"), [])
        self.assertEqual([row["runs"] for row in pensieve.summary(self.conn)], [1])
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_a_failed_run_leaves_its_owl_and_raises_an_event(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        code, _, _, _ = self.real_run("hermione", owl_id, 3)
        self.assertEqual(code, 1)
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "hermione")], [owl_id])
        self.assertEqual(len(self.events_of("rundesk.failed")), 1)

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
                         {"input_tokens": 150, "output_tokens": 700, "cache_read_tokens": 9000, "cost_usd": 0.42})
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
        self.assertEqual(sorted(run_desk.child_env()), ["HOME", "LANG", "PATH", "RTK_DISABLED", "SHELL"])
