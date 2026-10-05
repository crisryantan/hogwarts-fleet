"""Gringotts: the nightly backup leaves credentials and git metadata out and blanks secret-named values, keeps
14 days, and its restore drill restores into a temp folder inside the backups folder and checks every file.

The Claude and Codex folders, the office and the castle are temp folders from tests_fleet.support, so the real
ones are never read. Git runs only inside the temp castle, with a temp home. Time is always injected.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import zlib
from unittest import mock

from tests.support import NOW

from fleet import config, gringotts
from tests_fleet.support import FleetCase

DAY = 86400
SECRET = "s3cret-value-123"


class GringottsCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.claude = self.tmp / "claude"
        self.codex = self.tmp / "codex"
        for folder in (self.claude, self.codex, self.claude / "agents", self.claude / "projects",
                       self.claude / "projects" / "-work", self.claude / "projects" / "-work" / "memory",
                       self.office / "logs", self.castle / "worktrees" / "wt", self.office / "patrol"):
            folder.mkdir(mode=0o700, exist_ok=True)
        self.write_file(self.claude / "settings.json", json.dumps(
            {"env": {"API_TOKEN": SECRET}, "permissions": {"deny": ["Read(~/.hogwarts/**)"]},
             "mcpServers": {"x": {"headers": {"Authorization": SECRET}}}}))
        self.write_file(self.claude / ".credentials.json", SECRET)
        self.write_file(self.claude / "history.jsonl", SECRET)
        self.write_file(self.claude / "CLAUDE.md", "# my rules\n")
        self.write_file(self.claude / "agents" / "snape.md", "---\nname: snape\n---\n")
        self.write_file(self.claude / "projects" / "-work" / "memory" / "MEMORY.md", "- a memory\n")
        self.write_file(self.claude / "projects" / "-work" / "session.jsonl", SECRET)
        self.write_file(self.codex / "config.toml",
                        'model = "gpt"\n\n[mcp_servers.docs]\ncommand = "docs"\nenv = { "DOCS_TOKEN" = "%s" }\n'
                        '\n[mcp_servers.docs.env]\nOTHER = "%s"\n\n[shell_environment_policy]\nset = { A = "%s" }\n'
                        'api_key = "%s"\n' % (SECRET, SECRET, SECRET, SECRET))
        self.write_file(self.codex / "auth.json", SECRET)
        self.write_file(self.codex / "AGENTS.md", "# codex rules\n")
        self.write_file(self.office / "logs" / "map.out.log", SECRET)
        self.write_file(self.office / "patrol" / "shadow", "shadow\n")
        self.write_file(self.castle / "worktrees" / "wt" / "code.py", SECRET)
        self.write_file(self.castle / "desks" / "ron" / "id_rsa", SECRET)
        os.symlink(self.claude / ".credentials.json", self.castle / "desks" / "ron" / "looks-harmless.md")
        for name, value in (("CLAUDE_CONFIG_DIR", str(self.claude)), ("CODEX_CONFIG_DIR", str(self.codex))):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def backups(self) -> list:
        return sorted(os.listdir(self.office / config.BACKUP_DIR))

    def members(self, path) -> dict:
        with tarfile.open(path, "r:gz") as tar:
            return {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isreg()}

    def git(self, *args, cwd=None) -> None:
        subprocess.run([config.GIT_BIN, *args], cwd=cwd or self.castle, capture_output=True, check=True,
                       env={"HOME": str(self.tmp), "PATH": config.CHILD_PATH, "GIT_CONFIG_NOSYSTEM": "1"})


def scrubbed_toml(text: str):
    out = gringotts._scrub_toml(text.encode("utf-8"))
    return None if out is None else out.decode("utf-8")


class BackupTests(GringottsCase):
    def test_the_archive_is_private_and_leaves_secrets_out(self):
        result = gringotts.backup(now=NOW)
        self.assertTrue(result["ok"])
        mode = stat.S_IMODE(os.stat(result["archive"]).st_mode)
        self.assertEqual(mode, 0o600)
        members = self.members(result["archive"])
        for name in ("claude/CLAUDE.md", "claude/agents/snape.md", "claude/projects/-work/memory/MEMORY.md",
                     "codex/AGENTS.md", "codex/config.toml", "office/patrol/shadow", "castle/desks/ron/scratchpad.md",
                     "office/state/pensieve.db", gringotts.MANIFEST):
            with self.subTest(present=name):
                self.assertIn(name, members)
        for name in ("claude/.credentials.json", "claude/history.jsonl", "claude/projects/-work/session.jsonl",
                     "codex/auth.json", "office/logs/map.out.log", "castle/worktrees/wt/code.py",
                     "castle/desks/ron/id_rsa", "castle/desks/ron/looks-harmless.md"):
            with self.subTest(absent=name):
                self.assertNotIn(name, members)
        for name, data in members.items():
            with self.subTest(secret_in=name):
                self.assertNotIn(SECRET.encode(), data)
        settings = json.loads(members["claude/settings.json"])
        self.assertEqual(settings["env"], {"API_TOKEN": ""})
        self.assertEqual(settings["mcpServers"]["x"]["headers"], {"Authorization": ""})
        self.assertEqual(settings["permissions"], {"deny": ["Read(~/.hogwarts/**)"]})
        self.assertIn(b'model = "gpt"', members["codex/config.toml"])
        manifest = json.loads(members[gringotts.MANIFEST])
        # Claude's and Codex's folders are read from a list of names, so their credential files are never walked.
        # A credential-named file in a folder that is walked whole is left out and listed.
        self.assertIn("castle/desks/ron/id_rsa", [item["path"] for item in manifest["skipped"]])

    def test_old_archives_go_after_fourteen_days_and_the_newest_stays(self):
        folder = self.office / config.BACKUP_DIR
        folder.mkdir(mode=0o700)
        old = [gringotts.archive_name(NOW - days * DAY) for days in (20, 15, 1)]
        for name in old:
            self.write_file(folder / name, b"old")
        result = gringotts.backup(now=NOW)
        self.assertEqual(sorted(result["pruned"]), sorted(old[:2]))
        self.assertEqual(self.backups(), sorted([old[2], gringotts.archive_name(NOW)]))

    def test_toml_values_that_span_lines_are_left_out_whole(self):
        self.assertIsNone(gringotts._scrub_toml(b'api_key = """\nsecret\n"""\n'))
        self.assertIsNone(gringotts._scrub_toml(b'[mcp.env]\nTOKENS = [\n"a",\n]\n'))
        kept = gringotts._scrub_toml(b'[profiles.fast]\nmodel = "x"\n')
        self.assertEqual(kept, b'[profiles.fast]\nmodel = "x"\n')

    def test_toml_blanks_a_value_when_any_part_of_its_full_path_is_secret_named(self):
        cases = {
            "a dotted key under an MCP server table":
                ('[mcp_servers.x]\ncommand = "npx"\nhttp_headers.Authorization = "%s"\n',
                 '[mcp_servers.x]\ncommand = "npx"\nhttp_headers.Authorization = ""\n'),
            "a dotted key at the top": ('mcp_servers.x.env.TOKEN = "%s"\n', 'mcp_servers.x.env.TOKEN = ""\n'),
            "an env table": ('[mcp_servers.x.env]\nANY = "%s"\n', '[mcp_servers.x.env]\nANY = ""\n'),
            "a quoted headers table": ('[mcp_servers."my server".http_headers]\n"Authorization" = \'%s\'\n',
                                       '[mcp_servers."my server".http_headers]\n"Authorization" = ""\n'),
            "an inline env table": ('[mcp_servers.x]\nenv = { A = "%s", B = { C = "%s" } }\n',
                                    '[mcp_servers.x]\nenv = {}\n'),
            "an array of tables": ('[[mcp_servers.x.tools]]\napi_key = "%s"\nname = "t"\n',
                                   '[[mcp_servers.x.tools]]\napi_key = ""\nname = "t"\n'),
            "a secret-named array": ('tokens = ["%s", "b"]\n', 'tokens = []\n'),
            "an escaped quote in the value": ('api_key = "a\\"%s"\n', 'api_key = ""\n'),
            "the shell environment set table":
                ('[shell_environment_policy]\nset = { PATH = "%s" }\ninherit = "core"\n',
                 '[shell_environment_policy]\nset = {}\ninherit = "core"\n'),
            "a commented-out secret": ('# api_key = "%s"\nmodel = "m" # %s\n', '\nmodel = "m"\n'),
            "a key named key": ('[srv]\nkey = "%s"\nmonkey = "kept"\n', '[srv]\nkey = ""\nmonkey = "kept"\n'),
            "numbers and dates stay": ('max_output_tokens = 4000\nwhen = 1979-05-27T07:32:00Z\n',
                                       'max_output_tokens = 4000\nwhen = 1979-05-27T07:32:00Z\n'),
        }
        for name, (given, expected) in cases.items():
            with self.subTest(form=name):
                self.assertEqual(scrubbed_toml(given.replace("%s", SECRET)), expected)
                self.assertFalse(gringotts._toml_left(expected.encode("utf-8")))

    def test_toml_forms_the_scrubber_does_not_fully_understand_leave_the_file_out(self):
        for name, text in (
            ("an inline parent table",
             '[mcp_servers]\nx = { command = "a", http_headers = { Authorization = "%s" } }\n'),
            ("an inline table in an array", 'servers = [{ token = "%s" }]\n'),
            ("a multi-line string", 'notes = """\n%s\n"""\n'),
            ("a one-line triple-quoted string", "notes = \'\'\'%s\'\'\'\n"),
            ("a multi-line array", 'args = [\n"%s",\n]\n'),
            ("an array of tables it cannot place", '[[mcp_servers.x]\ntoken = "%s"\n'),
            ("a table header with junk after it", '[mcp_servers.x] junk\ntoken = "%s"\n'),
            ("a quoted key with an escape", '"\\u0074oken" = "%s"\n'),
            ("a bare word", "token = %s\n"),
            ("a line that is not TOML", "%s\n"),
        ):
            with self.subTest(form=name):
                self.assertIsNone(scrubbed_toml(text.replace("%s", SECRET)))

    def test_json_blanks_every_secret_named_value_at_any_depth(self):
        given = {"apiKeyHelper": SECRET, "includeCoAuthoredBy": True, "permissions": {"deny": ["Read(x)"]},
                 "mcpServers": {"x": {"command": "npx", "args": ["-y", "server"], "timeout": 30,
                                      "requestInit": {"Authorization": f"Bearer {SECRET}"},
                                      "nested": {"clientSecret": SECRET, "list": [{"password": SECRET}]},
                                      "env": {"N": SECRET}, "headers": {"X": [SECRET]}}}}
        out = json.loads(gringotts._scrub_json(json.dumps(given).encode("utf-8")))
        self.assertNotIn(SECRET, json.dumps(out))
        server = out["mcpServers"]["x"]
        self.assertEqual((out["apiKeyHelper"], server["requestInit"], server["nested"]),
                         ("", {"Authorization": ""}, {"clientSecret": "", "list": [{"password": ""}]}))
        self.assertEqual((server["env"], server["headers"]), ({"N": ""}, {"X": [""]}))
        self.assertEqual((out["includeCoAuthoredBy"], out["permissions"], server["args"], server["timeout"]),
                         (True, {"deny": ["Read(x)"]}, ["-y", "server"], 30))

    def test_scrubbed_files_reach_the_archive_and_the_rest_is_left_out(self):
        self.write_file(self.castle / ".mcp.json", json.dumps({"mcpServers": {"x": {"apiKey": SECRET}}}))
        self.write_file(self.codex / "config.toml", '[mcp_servers]\nx = { http_headers = { Authorization = "%s" } }\n'
                        % SECRET)
        result = gringotts.backup(now=NOW)
        members = self.members(result["archive"])
        self.assertEqual(json.loads(members["castle/.mcp.json"]), {"mcpServers": {"x": {"apiKey": ""}}})
        self.assertNotIn("codex/config.toml", members)
        manifest = json.loads(members[gringotts.MANIFEST])
        self.assertIn({"path": "codex/config.toml", "why": "could not be scrubbed, so it was left out"},
                      manifest["skipped"])
        for name, data in members.items():
            with self.subTest(secret_in=name):
                self.assertNotIn(SECRET.encode(), data)

    def test_git_history_never_enters_the_archive(self):
        notes = self.castle / "desks" / "ron" / "notes.md"
        self.write_file(notes, f"token {SECRET}\n")
        self.git("init", "-q")
        self.git("add", "desks/ron/notes.md")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "notes")
        self.write_file(notes, "nothing secret now\n")
        nested = self.castle / "desks" / "ron" / "checkout"
        nested.mkdir(mode=0o700)
        self.write_file(nested / ".git", "gitdir: /somewhere/else\n")
        bare = self.castle / "desks" / "ron" / "mirror"
        for part in ("objects", "refs"):
            (bare / part).mkdir(mode=0o700, parents=True)
        self.write_file(bare / "HEAD", "ref: refs/heads/main\n")
        self.write_file(bare / "objects" / "loose", SECRET)
        result = gringotts.backup(now=NOW)
        members = self.members(result["archive"])
        self.assertEqual(members["castle/desks/ron/notes.md"], b"nothing secret now\n")
        self.assertEqual([name for name in members if ".git" in name.split("/") or "/mirror/" in name], [])
        for name, data in members.items():
            with self.subTest(secret_in=name):
                self.assertNotIn(SECRET.encode(), data)
                try:
                    self.assertNotIn(SECRET.encode(), zlib.decompress(data))
                except zlib.error:
                    pass
        skipped = {item["path"]: item["why"] for item in json.loads(members[gringotts.MANIFEST])["skipped"]}
        for path in ("castle/.git", "castle/desks/ron/checkout/.git", "castle/desks/ron/mirror"):
            with self.subTest(left_out=path):
                self.assertEqual(skipped.get(path), "git metadata")


class DrillTests(GringottsCase):
    def test_the_drill_restores_into_a_temp_folder_and_passes(self):
        made = gringotts.backup(now=NOW)
        result = gringotts.drill(now=NOW + 60)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["files"], made["files"] - 1)
        self.assertEqual([name for name in self.backups() if name.startswith("drill-")], [])
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), "# my rules\n")

    def test_the_drill_keeps_its_folder_inside_the_backups_folder_when_asked(self):
        gringotts.backup(now=NOW)
        result = gringotts.drill(now=NOW + 60, keep=True)
        self.assertTrue(result["restored_to"].startswith(str(self.office / config.BACKUP_DIR) + "/drill-"))
        self.assertTrue(os.path.isfile(os.path.join(result["restored_to"], "claude", "CLAUDE.md")))

    def rewrite(self, path, change) -> None:
        """Rebuild an archive with change(name, data) applied to each file, and extra entries change may add."""
        with tarfile.open(path, "r:gz") as tar:
            files = [(member.name, tar.extractfile(member).read()) for member in tar.getmembers() if member.isreg()]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, data in change(files):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        self.write_file(path, buffer.getvalue())

    def test_the_drill_finds_a_changed_file(self):
        archive = gringotts.backup(now=NOW)["archive"]
        self.rewrite(archive, lambda files: [(name, b"changed" if name == "claude/CLAUDE.md" else data)
                                             for name, data in files])
        result = gringotts.drill(now=NOW + 60)
        self.assertFalse(result["ok"])
        self.assertIn("claude/CLAUDE.md does not match the manifest", result["problems"])

    def test_the_drill_finds_a_secret_left_in_json_or_toml_and_git_metadata(self):
        archive = gringotts.backup(now=NOW)["archive"]
        extra = [("claude/settings.json", json.dumps({"mcpServers": {"x": {"apiKey": "left"}}}).encode()),
                 ("codex/config.toml", b'[mcp_servers.x]\nhttp_headers.Authorization = "left"\n'),
                 ("castle/.git/config", b"[core]\n")]
        names = {name for name, _ in extra}
        self.rewrite(archive, lambda files: [(name, data) for name, data in files if name not in names] + extra)
        problems = gringotts.drill(now=NOW + 60)["problems"]
        for problem in ("claude/settings.json still has a value under a secret-named key",
                        "codex/config.toml still has a value under a secret-named key",
                        "castle/.git/config is git metadata"):
            with self.subTest(problem=problem):
                self.assertIn(problem, problems)

    def test_the_drill_refuses_odd_entries_and_credentials(self):
        archive = gringotts.backup(now=NOW)["archive"]
        self.rewrite(archive, lambda files: files + [("../escape.txt", b"x"), ("claude/.credentials.json", b"x")])
        result = gringotts.drill(now=NOW + 60)
        self.assertFalse(result["ok"])
        self.assertTrue(any("plain name" in problem for problem in result["problems"]))
        self.assertTrue(any("named like a credential" in problem for problem in result["problems"]))
        self.assertFalse((self.office / "escape.txt").exists())
        self.assertFalse((self.office / config.BACKUP_DIR / "escape.txt").exists())

    def test_a_loose_archive_mode_is_a_problem(self):
        archive = gringotts.backup(now=NOW)["archive"]
        os.chmod(archive, 0o644)
        self.assertIn("the archive is not a private plain file of yours with mode 0600",
                      gringotts.drill(now=NOW + 60)["problems"])

    def test_no_archive_yet_is_refused(self):
        (self.office / config.BACKUP_DIR).mkdir(mode=0o700)
        with self.assertRaises(gringotts.Problem):
            gringotts.drill(now=NOW)


class FailureTests(GringottsCase):
    def run_main(self, *args) -> int:
        with mock.patch("sys.stdout", io.StringIO()):
            return gringotts.main(list(args))

    def test_a_failure_reaches_ryan_only_once_live(self):
        with mock.patch.object(gringotts, "backup", side_effect=gringotts.Problem("disk full")):
            self.assertEqual(self.run_main(), 1)
            self.assertEqual(self.events(), [])
            os.unlink(self.office / "patrol" / "shadow")
            self.assertEqual(self.run_main(), 1)
        events = [event for event in self.events() if event["kind"] == "patrol.gringotts"]
        self.assertEqual([(event["desk"], event["verdict"]) for event in events], [("gringotts", "headmaster")])
        self.assertIn("disk full", events[0]["summary"])

    def test_arguments(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_main("--now"), 2)
            self.assertEqual(self.run_main("--drill", "a", "b"), 2)
