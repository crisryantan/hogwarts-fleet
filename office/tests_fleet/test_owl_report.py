"""Owl reports: with the owl-reports switch on, a headless McGonagall turn reads each owl delivered to her and its
one-line report reaches Ryan as a notification. The claude binary is a fake script these tests write, which records
its argv and prints what each test asks for."""
from __future__ import annotations

import json
import os
import shutil
import stat
from unittest import mock

from hogwarts import pensieve
from tests.support import NOW

from fleet import config, mcgonagall_inbox, owl_post, owl_report, run_desk
from fleet.safefs import FleetError
from tests_fleet.support import OFFICE, FleetCase

REAL_ANNOUNCE = mcgonagall_inbox.announce
SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
FAKE = """#!/usr/bin/python3
import json, os, sys
state = {state!r}
mode = open(os.path.join(state, "mode")).read().strip()
with open(os.path.join(state, "argv.jsonl"), "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv, "cwd": os.getcwd()}}) + "\\n")
batch = json.load(open("owl-report-batch.json"))["owls"]
if mode == "auth":
    sys.stderr.write("Invalid API key. Please run /login\\n")
    sys.exit(1)
if mode == "fail":
    sys.exit(2)
if mode == "skip-first":
    batch = batch[1:]
print("Here are the reports:")
for owl in batch:
    summary = "ron says the build is green" if mode != "secret" else "token " + {secret!r} + "\\nsecond line"
    print(json.dumps({{"owl": owl, "summary": summary, "title": "EVIL TITLE"}}))
print(json.dumps({{"owl": "owl_00000000000000ff", "summary": "not in the batch"}}))
"""


class OwlReportCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_file(self.office / config.OWL_REPORTS_FILE, "on\n")
        desk = self.office / "desks" / "mcgonagall"
        desk.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(OFFICE / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE,
                        desk / config.OWL_REPORT_SETTINGS_FILE)
        self.state = self.tmp / "fake-claude"
        self.state.mkdir(mode=0o700)
        self.mode("ok")
        binary = self.write_file(self.state / "claude", FAKE.format(state=str(self.state), secret=SECRET), mode=0o700)
        os.chmod(binary, stat.S_IRWXU)
        for name, value in (("CLAUDE_BIN", str(binary)),):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        spawn = mock.patch.object(run_desk, "spawn_owl_report")
        self.spawned = spawn.start()
        self.addCleanup(spawn.stop)
        self.task = pensieve.create_task(self.conn, "harry", "build it", now=NOW)
        self.count = 0

    def mode(self, value: str) -> None:
        self.write_file(self.state / "mode", value)

    def send(self, sender: str = "ron", subject: str = "status") -> str:
        self.count += 1
        self.write_owl(sender, f"o{self.count:02d}.json", {"to": "mcgonagall", "kind": "fyi", "subject": subject,
                                                           "body": f"body text {self.count}",
                                                           "task_id": self.task["id"]})
        delivered = owl_post.run_pass(self.conn, now=NOW)["delivered"]
        return delivered[-1]["owl_id"]

    def runs(self) -> list:
        path = self.state / "argv.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def log(self) -> list:
        path = self.castle / "desks" / "mcgonagall" / owl_report.LOG_FILE
        return path.read_text().splitlines() if path.exists() else []

    def pending(self) -> list:
        folder = self.office / owl_report.MARKER_DIR
        return sorted(path.name for path in folder.iterdir() if path.name.startswith("owl_")) if folder.exists() else []

    def reports(self) -> list:
        return [call for call in self.notified.call_args_list if len(call[0]) == 2]


class HappyPathTests(OwlReportCase):
    def test_each_owl_gets_one_notification_and_one_log_line_with_the_title_from_the_store(self):
        first, second = self.send(subject="first"), self.send("hermione", subject="second")
        self.assertEqual(self.spawned.call_count, 2)  # one per pass that had owls pending
        self.assertEqual(self.pending(), sorted([first, second]))
        self.assertEqual(owl_report.run(self.conn, NOW), ["done"])
        titles = sorted(call[0][1] for call in self.reports())
        self.assertEqual(titles, [f"Owl: hermione {self.task['id']}", f"Owl: ron {self.task['id']}"])
        self.assertTrue(all(call[0][0] == "ron says the build is green" for call in self.reports()))
        lines = self.log()
        self.assertEqual(len(lines), 2)
        for owl_id, sender in ((first, "ron"), (second, "hermione")):
            [line] = [line for line in lines if owl_id in line]
            self.assertRegex(line, rf"^\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ {sender} {self.task['id']} {owl_id}"
                                   r" ron says the build is green$")
        self.assertEqual(self.pending(), [])
        self.assertNotIn("EVIL", "\n".join(lines) + json.dumps([call[0] for call in self.notified.call_args_list]))
        self.assertEqual(owl_report.run(self.conn, NOW), [])  # nothing left: no second report
        self.assertEqual(len(self.reports()), 2)

    def test_argv_and_prompt_carry_no_owl_text_or_id_and_only_read_tools(self):
        owl_id = self.send(subject="SUBJECT-MARKER")
        owl_report.run(self.conn, NOW)
        [run] = self.runs()
        argv = run["argv"]
        joined = " ".join(argv)
        for marker in (owl_id, "SUBJECT-MARKER", "body text", self.task["id"]):
            self.assertNotIn(marker, joined)
        self.assertEqual(argv[-1], owl_report.PROMPT)
        self.assertEqual(argv[1:4], ["-p", "--restricted", "--settings"])
        self.assertEqual(argv[4], f"{self.office}/desks/mcgonagall/{config.OWL_REPORT_SETTINGS_FILE}")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual(argv[argv.index("--model") + 1], config.OWL_REPORT_MODEL)
        self.assertEqual(run["cwd"], str(self.castle / "desks" / "mcgonagall"))
        batch = json.loads((self.castle / "desks" / "mcgonagall" / owl_report.BATCH_FILE).read_text())
        self.assertEqual(batch, {"owls": [owl_id]})

    def test_the_report_settings_refuse_anything_but_reads(self):
        raw = (OFFICE / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE).read_bytes()
        data = run_desk.check_report_settings(raw)
        self.assertEqual(data["permissions"]["allow"], ["Read(~/hogwarts/desks/mcgonagall/inbox/**)",
                                                        "Read(~/hogwarts/desks/mcgonagall/owl-report-batch.json)"])
        base = json.loads(raw)
        changes = (
            ("a hook", lambda d: d.update(hooks={"Stop": []})),
            ("hooks left on", lambda d: d.pop("disableAllHooks")),
            ("a write allowed", lambda d: d["sandbox"]["filesystem"].update(allowWrite=["/tmp"])),
            ("Bash not denied", lambda d: d["permissions"]["deny"].remove("Bash")),
            ("an edit allowed", lambda d: d["permissions"]["allow"].append("Edit(~/hogwarts/**)")),
            ("sandbox off", lambda d: d["sandbox"].update(enabled=False)),
        )
        for label, change in changes:
            with self.subTest(change=label):
                data = json.loads(json.dumps(base))
                change(data)
                with self.assertRaises(FleetError):
                    run_desk.check_report_settings(json.dumps(data).encode())
        # A settings file that fails the check runs nothing, and the owl gets the plain notification once.
        self.write_file(self.office / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE,
                        json.dumps({**base, "hooks": {"Stop": []}}))
        self.send()
        self.assertEqual(owl_report.run(self.conn, NOW), ["failed"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(len([call for call in self.notified.call_args_list if len(call[0]) == 1]), 1)

    def test_a_credential_in_her_summary_is_scrubbed_to_one_line(self):
        self.mode("secret")
        self.send()
        owl_report.run(self.conn, NOW)
        [call] = self.reports()
        self.assertNotIn(SECRET, call[0][0])
        self.assertNotIn("second line", call[0][0])
        self.assertTrue(call[0][0].startswith("token "))
        self.assertNotIn(SECRET, "\n".join(self.log()))


class RetryTests(OwlReportCase):
    def test_an_owl_her_output_skips_is_retried_then_reported_with_the_fallback(self):
        owls = {self.send(subject="first"), self.send(subject="second")}
        self.mode("skip-first")
        self.assertEqual(owl_report.run(self.conn, NOW), ["done"] * config.OWL_REPORT_MAX_TRIES)
        self.assertEqual(len(self.runs()), config.OWL_REPORT_MAX_TRIES)
        texts = sorted(call[0][0] for call in self.reports())
        self.assertEqual(texts, sorted(["ron says the build is green", owl_report.FALLBACK]))
        lines = self.log()
        self.assertEqual(sorted(line.split()[3] for line in lines), sorted(owls))
        self.assertEqual(sum(line.endswith(owl_report.FALLBACK) for line in lines), 1)
        self.assertEqual(self.pending(), [])

    def test_a_killed_reporter_is_retried_by_the_next_pass_with_no_duplicate(self):
        first, second = self.send(subject="first"), self.send(subject="second")
        calls = []

        def killed_on_the_second(text, title="Hogwarts"):
            calls.append(text)
            if len(calls) == 2:
                raise SystemExit(143)  # SIGTERM while the second report is being shown
            return True

        self.notified.side_effect = killed_on_the_second
        with self.assertRaises(SystemExit):
            owl_report.run(self.conn, NOW)
        self.assertEqual(len(self.pending()), 1)
        self.notified.side_effect = None
        spawned = self.spawned.call_count
        owl_post.run_pass(self.conn, now=NOW)  # the next pass finds the owl still pending
        self.assertEqual(self.spawned.call_count, spawned + 1)
        owl_report.run(self.conn, NOW)
        self.assertEqual(self.pending(), [])
        for owl_id in (first, second):
            self.assertEqual(len([line for line in self.log() if owl_id in line]), 1)

    def test_a_failed_turn_sends_the_plain_notification_once_and_retries(self):
        self.send(subject="first")
        self.mode("fail")
        self.assertEqual(owl_report.run(self.conn, NOW), ["failed"])
        self.assertEqual(owl_report.run(self.conn, NOW), ["failed"])
        plain = [call for call in self.notified.call_args_list if len(call[0]) == 1]
        self.assertEqual(len(plain), 1)
        self.assertEqual(plain[0][0][0], f"ron on {self.task['id']}: fyi: first")
        self.mode("ok")
        owl_report.run(self.conn, NOW)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.log()), 1)

    def test_a_reporter_that_cannot_start_falls_back_to_the_plain_notification_once(self):
        self.spawned.side_effect = OSError("no fork")
        self.send(subject="first")
        owl_post.run_pass(self.conn, now=NOW)
        plain = [call for call in self.notified.call_args_list if len(call[0]) == 1]
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(self.pending()), 1)  # still reported once a reporter can start


class AuthTests(OwlReportCase):
    def test_an_auth_failure_tells_once_and_waits_for_the_next_new_owl(self):
        first = self.send(subject="first")
        self.mode("auth")
        self.assertEqual(owl_report.run(self.conn, NOW), ["auth"])
        self.assertEqual(owl_report.run(self.conn, NOW), [])  # nothing new: no retry
        events = self.conn.execute("SELECT kind, summary FROM events WHERE kind = 'owl-report.auth'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertNotIn("API key", events[0]["summary"])
        notices = [call[0][0] for call in self.notified.call_args_list]
        self.assertEqual(notices.count("owl watcher: auth failed"), 1)
        self.assertEqual(self.pending(), [first])
        spawned = self.spawned.call_count
        owl_post.run_pass(self.conn, now=NOW)
        self.assertEqual(self.spawned.call_count, spawned)  # still waiting
        self.mode("ok")
        second = self.send(subject="second")
        self.assertEqual(self.spawned.call_count, spawned + 1)
        owl_report.run(self.conn, NOW)
        self.assertEqual(self.pending(), [])
        self.assertEqual(sorted(line.split()[3] for line in self.log()), sorted([first, second]))
        self.assertEqual(notices.count("owl watcher: auth failed"), 1)


class SwitchTests(OwlReportCase):
    def test_switched_off_nothing_spawns_and_the_plain_notification_stays(self):
        os.unlink(self.office / config.OWL_REPORTS_FILE)
        with mock.patch.object(mcgonagall_inbox, "announce", REAL_ANNOUNCE):
            self.send(subject="first")
        self.spawned.assert_not_called()
        self.assertEqual(self.pending(), [])
        self.assertEqual([call[0] for call in self.notified.call_args_list],
                         [(f"ron on {self.task['id']}: fyi: first",)])

    def test_switched_on_the_plain_notification_is_suppressed_but_the_event_stays(self):
        with mock.patch.object(mcgonagall_inbox, "announce", REAL_ANNOUNCE):
            self.send(subject="first")
        self.notified.assert_not_called()
        events = self.conn.execute("SELECT kind FROM events WHERE kind = 'owl.to-mcgonagall'").fetchall()
        self.assertEqual(len(events), 1)
