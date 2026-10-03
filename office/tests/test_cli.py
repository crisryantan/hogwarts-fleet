from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hogwarts import cli, db
from tests.support import REPO, SHA, review_file, temp_dir, worktree

ROOT = Path(__file__).resolve().parents[1]
CASTLE = ROOT / "bin" / "castle"
INSTALLED = "/Users/crisryantan/.hogwarts"
INTERPRETER = ["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty"]
WRAPPER = (
    "exec " + " ".join(INTERPRETER) + " -c 'import sys; sys.path.insert(0, \"/Users/crisryantan/.hogwarts\");"
    " from hogwarts.cli import main; sys.exit(main())' \"$@\""
)


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.db_path = self.tmp / "state" / "pensieve.db"
        self.unused_default = self.tmp / "default" / "pensieve.db"
        patcher = mock.patch.object(db, "DEFAULT_DB", self.unused_default)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertFalse(self.unused_default.parent.exists()))

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(list(argv), db_path=self.db_path)
        self.raw = out.getvalue() + err.getvalue()
        stdout = json.loads(out.getvalue()) if out.getvalue() else None
        stderr = json.loads(err.getvalue()) if err.getvalue() else None
        return code, stdout, stderr

    def ok(self, *argv, stdin=""):
        code, out, err = self.run_cli(*argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        self.assertTrue(out["ok"])
        return out["data"]

    def fails(self, code, error_type, *argv, stdin=""):
        actual, out, err = self.run_cli(*argv, stdin=stdin)
        self.assertEqual(actual, code, (out, err))
        self.assertIsNone(out)
        self.assertEqual(err["error"]["type"], error_type)
        self.assertEqual(err["error"]["code"], code)
        return err


class InitTests(CliCase):
    def test_init_creates_the_database_and_is_safe_to_run_twice(self):
        first = self.ok("init")
        self.assertEqual((first["created"], first["schema_version"]), (True, db.SCHEMA_VERSION))
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path.parent).st_mode), 0o700)
        self.assertFalse(self.ok("init")["created"])

    def test_commands_need_an_initialised_database(self):
        self.fails(4, "NotFoundError", "desk", "list")
        self.assertFalse(self.db_path.exists())

    def test_a_symlinked_sidecar_exits_5_as_an_integrity_error(self):
        self.ok("init")
        target = self.tmp / "elsewhere"
        target.write_bytes(b"")
        for suffix in db.SIDECARS:
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.db_path) + suffix)
                if os.path.lexists(sidecar):
                    os.remove(sidecar)
                os.symlink(target, sidecar)
                err = self.fails(5, "IntegrityError", "desk", "list")
                self.assertIn(f"database {suffix[1:]} file is a symlink", err["error"]["message"])
                self.assertEqual(self.run_cli("doctor")[0], 5)
                os.remove(sidecar)
        self.assertEqual(self.ok("desk", "list"), [])
        self.assertEqual(target.read_bytes(), b"")

    def test_doctor_reports_health_and_fails_on_loose_permissions(self):
        self.ok("init")
        self.assertTrue(self.ok("doctor")["ok"])
        os.chmod(self.db_path.parent, 0o777)
        self.addCleanup(os.chmod, self.db_path.parent, 0o700)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 5)
        self.assertFalse(out["ok"])
        self.assertFalse(out["data"]["ok"])


class CommandTests(CliCase):
    def setUp(self):
        super().setUp()
        self.ok("init")
        self.ok("desk", "add", "alpha", "--family", "claude", "--role", "builder")
        self.ok("desk", "add", "beta", "--family", "codex")

    def test_desk_list(self):
        self.assertEqual([desk["name"] for desk in self.ok("desk", "list")], ["alpha", "beta"])

    def test_task_lifecycle(self):
        task = self.ok("task", "create", "--desk", "alpha", "--title", "ship it", "--worktree", worktree())
        self.assertEqual(task["status"], "queued")
        self.assertEqual(self.ok("task", "start", task["id"])["status"], "active")
        self.assertEqual(self.ok("task", "await-close", task["id"])["status"], "awaiting_close")
        token = self.ok("token", "mint", task["id"], "--ttl", "120")["token"]
        closed = self.ok("task", "close", task["id"], "--reason", "complete", "--token-stdin", stdin=token + "\n")
        self.assertEqual((closed["status"], closed["close_reason"]), ("closed", "complete"))
        self.assertEqual(self.ok("task", "show", task["id"])["status"], "closed")
        self.assertEqual(len(self.ok("task", "list", "--desk", "alpha", "--status", "closed")), 1)

    def test_task_create_takes_a_minted_id_and_its_own_intent_path(self):
        task_id = "tk_0123456789abcdef"
        intent = f"/Users/crisryantan/hogwarts/tasks/{task_id}/TASK.md"
        task = self.ok("task", "create", "--desk", "alpha", "--title", "ship it", "--id", task_id,
                       "--intent-path", intent)
        self.assertEqual((task["id"], task["intent_path"]), (task_id, intent))
        self.fails(3, "ConflictError", "task", "create", "--desk", "beta", "--title", "again", "--id", task_id)
        self.fails(2, "ValidationError", "task", "create", "--desk", "alpha", "--title", "x", "--intent-path", intent)
        self.fails(2, "ValidationError", "task", "create", "--desk", "alpha", "--title", "x",
                   "--id", "tk_00000000000000ff", "--intent-path", intent)
        self.assertEqual(len(self.ok("task", "list")), 1)

    def test_tokens_and_bodies_are_never_taken_from_argv(self):
        task = self.ok("task", "create", "--desk", "alpha", "--title", "a")
        self.ok("task", "start", task["id"])
        token = self.ok("token", "mint", task["id"])["token"]
        self.fails(2, "ValidationError", "task", "close", task["id"], "--reason", "complete", "--token", token)
        self.fails(2, "ValidationError", "owl", "send", "--from", "alpha", "--to", "beta", "--kind", "fyi",
                   "--subject", "s", "--body", "private detail")
        self.fails(2, "ValidationError", "request", "open", "--from", "alpha", "--to", "beta", "--title", "t",
                   "--body", "private detail")
        self.ok("task", "close", task["id"], "--reason", "complete", "--token-stdin", stdin=token)
        other = self.ok("task", "create", "--desk", "alpha", "--title", "b")
        self.assertEqual(self.ok("task", "close", other["id"], "--reason", "abandoned")["close_reason"], "abandoned")

    def test_owl_send_inbox_read_ack(self):
        sent = self.ok("owl", "send", "--from", "alpha", "--to", "beta", "--kind", "question",
                       "--subject", "which branch?", "--body-stdin", stdin="private detail")
        inbox = self.ok("owl", "inbox", "beta")
        self.assertEqual([owl["id"] for owl in inbox], [sent["id"]])
        self.assertNotIn("private detail", self.raw)
        read = self.ok("owl", "read", sent["id"], "--as", "beta")
        self.assertEqual(read["body"], "private detail")
        self.assertIsNotNone(self.ok("owl", "ack", sent["id"], "--as", "beta")["acked_at"])
        self.assertEqual(self.ok("owl", "inbox", "beta"), [])
        self.assertEqual(len(self.ok("owl", "inbox", "beta", "--all")), 1)
        answer = self.ok("owl", "send", "--from", "beta", "--to", "alpha", "--kind", "answer",
                         "--subject", "main", "--reply-to", sent["id"], "--body-stdin", stdin="use main")
        self.assertEqual(self.ok("owl", "read", answer["id"], "--as", "alpha")["body"], "use main")

    def test_request_flow(self):
        opened = self.ok("request", "open", "--from", "alpha", "--to", "beta", "--title", "review this",
                         "--body-stdin", "--key", "outbox-0000001", stdin="diff details")
        request_id = opened["request"]["id"]
        self.assertEqual(opened["task"]["desk"], "beta")
        self.assertEqual(self.ok("request", "advance", request_id, "claimed", "--detail", "broker")["phase"], "claimed")
        shown = self.ok("request", "show", request_id)
        self.assertEqual([row["phase"] for row in shown["history"]], ["queued", "claimed"])
        self.assertEqual([(owl["kind"], owl["has_body"]) for owl in shown["owls"]], [("request", True)])
        self.assertNotIn("diff details", self.raw)
        self.assertEqual(len(self.ok("request", "list", "--desk", "beta", "--open")), 1)
        self.assertEqual(self.ok("request", "defer", request_id, "--reason", "conflict")["outcome"], "deferred")
        second = self.ok("request", "open", "--from", "alpha", "--to", "beta", "--title", "another")
        declined = self.ok("request", "decline", second["request"]["id"], "--reason", "safety")
        self.assertEqual(declined["outcome"], "declined")
        self.assertEqual(self.ok("request", "list", "--open"), [])

    def test_review_record_and_check(self):
        task = self.ok("task", "create", "--desk", "alpha", "--title", "code")
        self.ok("task", "start", task["id"])
        self.ok("request", "open", "--from", "alpha", "--to", "beta", "--title", "review code", "--parent", task["id"])
        self.assertFalse(self.ok("review", "check", "--repo", REPO, "--sha", SHA)["pass"])
        commit = self.ok("task", "commit", task["id"], "--repo", REPO, "--sha", SHA)
        self.assertEqual((commit["task_id"], commit["created"]), (task["id"], True))
        self.ok("review", "record", "--repo", REPO, "--sha", SHA, "--task", task["id"], "--reviewer", "beta",
                "--verdict", "PASS", "--review-path", review_file())
        self.assertFalse(self.ok("review", "check", "--repo", REPO, "--sha", SHA)["pass"])
        self.ok("task", "await-close", task["id"], "--repo", REPO, "--sha", SHA)
        check = self.ok("review", "check", "--repo", REPO, "--sha", SHA)
        self.assertTrue(check["pass"])
        self.assertEqual(check["latest"]["reviewer_family"], "codex")

    def test_event_add_drain_ack(self):
        event = self.ok("event", "add", "--desk", "alpha", "--kind", "ci.failed", "--verdict", "headmaster",
                        "--summary", "main is red", "--dedupe-key", "ci:1")
        drained = self.ok("event", "drain", "--max-chars", "500")
        self.assertEqual([item["id"] for item in drained["events"]], [event["id"]])
        self.assertIsNotNone(self.ok("event", "ack", str(event["id"]))["acked_at"])
        self.assertEqual(self.ok("event", "drain")["events"], [])

    def test_pensieve_commands(self):
        session = self.ok("pensieve", "session", "session-0001", "--project", "web-app", "--desk", "alpha",
                          "--first-turn-tokens", "1500")
        self.assertTrue(session["created"])
        self.ok("pensieve", "extract", "session-0001", "--role", "user", "--text", "contact me at a@b.co")
        stored = self.ok("pensieve", "extract", "session-0001", "--role", "assistant", "--text-stdin",
                         stdin="the launcher race")
        self.assertEqual(stored["seq"], 2)
        self.ok("pensieve", "keypoint", "--text", "launcher race explained", "--tags", "site,race",
                "--session", "session-0001")
        hits = self.ok("pensieve", "find", "launcher", "--limit", "5")
        self.assertEqual(len(hits), 2)
        self.assertIn("[email]", self.ok("pensieve", "find", "contact")[0]["snippet"])
        self.assertNotIn("a@b.co", self.raw)

    def test_fact_commands(self):
        fact = self.ok("fact", "add", "--scope", "fleet", "--tier", "aging", "--text", "use the ledger")
        self.ok("fact", "add", "--scope", "alpha", "--tier", "pinned", "--text", "alpha only")
        self.assertEqual(len(self.ok("fact", "list")), 2)
        self.assertEqual(len(self.ok("fact", "list", "--context", "beta")), 1)
        self.assertEqual(self.ok("fact", "touch", str(fact["id"]))["id"], fact["id"])
        self.assertEqual(self.ok("fact", "decay"), {"stale": []})
        self.assertEqual(self.ok("fact", "decay", "--archive"), {"archived": []})
        self.assertEqual(self.ok("fact", "archive", str(fact["id"]))["archived"], [fact["id"]])
        self.assertEqual(len(self.ok("fact", "list", "--archived")), 2)

    def test_metric_commands(self):
        self.ok("metric", "add", "--desk", "alpha", "--run-id", "run-1", "--model", "claude-opus-5-5",
                "--input-tokens", "100", "--output-tokens", "10", "--cache-read-tokens", "5",
                "--cost-usd", "0.25", "--duration-ms", "900")
        summary = self.ok("metric", "summary")
        self.assertEqual((summary[0]["desk"], summary[0]["runs"], summary[0]["cost_usd"]), ("alpha", 1, 0.25))

    def test_purge_and_audit(self):
        self.assertEqual(self.ok("purge"),
                         {"extracts_deleted": 0, "owl_bodies_purged": 0, "wal_checkpoint_busy": False})
        report = self.ok("audit")
        self.assertEqual(report["stale_requests"], [])
        self.assertEqual(self.ok("audit", "--escalate")["escalated"], {"created": 0, "skipped": 0})

    def test_exit_codes_follow_the_error_class(self):
        self.fails(2, "ValidationError", "task", "show", "tk_nothex")
        self.fails(3, "ConflictError", "desk", "add", "alpha", "--family", "claude")
        self.fails(4, "NotFoundError", "task", "show", "tk_0000000000000000")
        task = self.ok("task", "create", "--desk", "alpha", "--title", "x")
        self.ok("desk", "add", "gamma", "--family", "claude")
        self.ok("task", "start", task["id"])
        self.ok("task", "commit", task["id"], "--repo", REPO, "--sha", SHA)
        self.fails(5, "IntegrityError", "review", "record", "--repo", REPO, "--sha", SHA, "--task", task["id"],
                   "--reviewer", "gamma", "--verdict", "PASS")
        self.fails(6, "TokenError", "task", "close", task["id"], "--reason", "complete", "--token-stdin",
                   stdin="A" * 43)
        self.fails(6, "TokenError", "task", "close", task["id"], "--reason", "complete")

    def test_argument_errors_are_json_with_exit_2(self):
        self.fails(2, "ValidationError", "bogus")
        self.fails(2, "ValidationError", "task", "create", "--desk", "alpha")
        self.fails(2, "ValidationError", "event", "ack", "١")
        self.fails(2, "ValidationError", "metric", "summary", "--since", "-5")
        self.fails(2, "ValidationError", "task", "close", "tk_0000000000000000", "--reason", "complete",
                   "--token", "a", "--token-stdin")

    def test_abbreviated_options_are_refused(self):
        self.fails(2, "ValidationError", "desk", "add", "gamma", "--fam", "claude")


class WrapperTests(unittest.TestCase):
    def test_wrapper_script_is_exact_and_mode_0700(self):
        lines = CASTLE.read_text().splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(lines[1:], [WRAPPER])
        self.assertEqual(stat.S_IMODE(os.stat(CASTLE).st_mode), 0o700)

    def test_wrapper_clears_the_environment_and_ignores_cached_bytecode(self):
        line = CASTLE.read_text().splitlines()[1]
        self.assertTrue(line.startswith("exec /usr/bin/env -i /usr/bin/python3 "), line)
        self.assertIn(" -X pycache_prefix=/var/empty ", line)

    def test_wrapper_ignores_a_hostile_developer_dir(self):
        tmp = temp_dir(self)
        wrapper = tmp / "castle"
        wrapper.write_text(CASTLE.read_text().replace(json.dumps(INSTALLED), json.dumps(str(ROOT))))
        os.chmod(wrapper, 0o700)
        hostile = {"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": str(tmp / "fake-developer-dir")}
        result = subprocess.run([str(wrapper), "no-such-command"], capture_output=True, text=True,
                                env=hostile, cwd="/", timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["error"]["type"], "ValidationError")

    def test_wrapper_interpreter_runs_the_code_under_test_against_a_temp_default_db(self):
        tmp = temp_dir(self)
        default_db = tmp / "state" / "pensieve.db"
        script = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
                  " from hogwarts import cli, db; db.DEFAULT_DB = Path(sys.argv[2]); sys.exit(cli.main(sys.argv[3:]))")

        def castle(*argv):
            result = subprocess.run(INTERPRETER + ["-c", script, str(ROOT), str(default_db), *argv],
                                    capture_output=True, text=True, cwd="/", timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["data"]

        self.assertEqual(castle("init")["db"], str(default_db))
        self.assertEqual(castle("desk", "list"), [])
        self.assertEqual(stat.S_IMODE(os.stat(default_db).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
