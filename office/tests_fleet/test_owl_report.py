"""Owl reports: with the owl-reports switch on, a headless McGonagall turn reads each owl delivered to her, one owl a
turn in its own folder, and her one-line report reaches Ryan as a notification. The claude binary is a fake script
these tests write: it records its argv, its folder and what it could read there, and prints what each test asks."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from unittest import mock

from hogwarts import pensieve
from tests.support import NOW

from fleet import config, markers, mcgonagall_inbox, owl_post, owl_report, run_desk
from fleet.safefs import FleetError
from tests_fleet.support import OFFICE, FleetCase

REAL_ANNOUNCE = mcgonagall_inbox.announce
SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
FAKE = """#!/usr/bin/python3
import json, os, sys, time
if sys.argv[-1] == "hang":
    time.sleep(60)
    sys.exit(0)
state = {state!r}
mode = open(os.path.join(state, "mode")).read().strip()
owl = json.load(open("owl.json"))
brief = sys.argv[sys.argv.index("--append-system-prompt-file") + 1]
with open(os.path.join(state, "runs.jsonl"), "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv, "cwd": os.getcwd(), "files": sorted(os.listdir(".")),
                             "owl": owl["owl_id"], "stdin": sys.stdin.read(), "brief_path": brief,
                             "brief": open(brief).read(), "brief_mode": os.lstat(brief).st_mode & 0o777}}) + "\\n")
def result(text, error=False, subtype="success", **extra):
    print(json.dumps({{"type": "result", "subtype": subtype, "is_error": error, "result": text, **extra}}))
if mode == "auth":
    result("Invalid API key \\u00b7 Please run /login", error=True)
    sys.exit(1)
if mode == "auth401":
    result("Request failed", error=True, api_error_status=401)
    sys.exit(1)
if mode == "fail":
    sys.exit(2)
if mode == "overloaded":
    result("Overloaded", error=True)
    sys.exit(1)
if mode == "big":
    sys.stdout.write("x" * 70000)
    sys.exit(0)
if mode == "skip:" + owl["owl_id"]:
    result("   ")
    sys.exit(0)
if mode == "secret":
    result("token " + {secret!r} + "\\nsecond line")
elif mode == "login-words":
    result("Hermione says the build passed; please run /login is not needed")
else:
    result(owl["from"] + " says " + owl["subject"])
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
        self.root = self.tmp / "owl-report-root"
        for name, value in (("CLAUDE_BIN", str(binary)), ("OWL_REPORT_ROOT", str(self.root))):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        darwin = mock.patch.object(run_desk, "notifications_on", return_value=True)
        darwin.start()
        self.addCleanup(darwin.stop)
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
        return owl_post.run_pass(self.conn, now=NOW + self.count)["delivered"][-1]["owl_id"]

    def runs(self) -> list:
        path = self.state / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def log(self) -> list:
        path = self.castle / "desks" / "mcgonagall" / owl_report.LOG_FILE
        return path.read_text().splitlines() if path.exists() else []

    def folder(self):
        return self.office / owl_report.MARKER_DIR

    def pending(self) -> dict:
        folder = self.folder()
        if not folder.exists():
            return {}
        return {path.name: json.loads(path.read_text()) for path in folder.iterdir() if path.name.startswith("owl_")}

    def reports(self) -> list:
        return [call for call in self.notified.call_args_list if call[0][1:2] and call[0][1].startswith("Owl: ")]

    def plain(self) -> list:
        return [call for call in self.notified.call_args_list if not (call[0][1:2] and call[0][1].startswith("Owl: "))]


class HappyPathTests(OwlReportCase):
    def test_one_owl_a_turn_in_its_own_folder_one_notification_and_one_log_line_each(self):
        first, second = self.send(subject="first"), self.send("hermione", subject="second")
        self.assertEqual(self.spawned.call_count, 2)
        self.assertEqual(owl_report.run(self.conn), ["done", "done"])
        runs = self.runs()
        self.assertEqual(sorted(run["owl"] for run in runs), sorted([first, second]))
        self.assertNotEqual(runs[0]["cwd"], runs[1]["cwd"])
        for run in runs:
            self.assertEqual(run["files"], ["owl.json"])
            self.assertTrue(run["cwd"].startswith(str(self.root) + "/turn-"))
        self.assertEqual(os.listdir(self.root), [])  # each folder went after its turn
        self.assertEqual(sorted((call[0][1], call[0][0]) for call in self.reports()),
                         [(f"Owl: hermione {self.task['id']}", "hermione says second"),
                          (f"Owl: ron {self.task['id']}", "ron says first")])
        self.assertEqual(sorted(line.split()[3] for line in self.log()), sorted([first, second]))
        self.assertEqual(self.pending(), {})
        self.assertEqual(owl_report.run(self.conn), [])

    def test_argv_and_prompt_carry_no_owl_text_or_id(self):
        owl_id = self.send(subject="SUBJECT-MARKER")
        owl_report.run(self.conn)
        [run] = self.runs()
        argv = run["argv"]
        for marker in (owl_id, "SUBJECT-MARKER", "body text", self.task["id"], owl_report.PROMPT, owl_report.BRIEF):
            self.assertNotIn(marker, " ".join(argv))
        self.assertEqual(run["stdin"], owl_report.PROMPT)
        self.assertEqual((run["brief"], run["brief_mode"]), (owl_report.BRIEF, 0o600))
        self.assertEqual(run["brief_path"], f"{self.office}/runs/mcgonagall/owl-report.brief")
        self.assertFalse(os.path.lexists(run["brief_path"]))  # removed once the turn ended
        self.assertNotIn("--append-system-prompt", argv)
        self.assertEqual(argv[-2:], ["--max-budget-usd", config.OWL_REPORT_MAX_BUDGET_USD])
        self.assertEqual(argv[1:4], ["-p", "--restricted", "--settings"])
        self.assertEqual(argv[4], f"{self.office}/desks/mcgonagall/{config.OWL_REPORT_SETTINGS_FILE}")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--strict-mcp-config", argv)
        for flag in ("--mcp-config", "--add-dir", "--allowed-tools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_her_answer_is_scrubbed_cut_to_its_first_line_and_bound_to_the_owl_it_ran(self):
        self.mode("secret")
        owl_id = self.send()
        owl_report.run(self.conn)
        [call] = self.reports()
        self.assertTrue(call[0][0].startswith("token "))
        for text in (call[0][0], "\n".join(self.log())):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("second line", text)
        self.assertEqual(call[0][1], f"Owl: ron {self.task['id']}")
        self.assertIn(f"\t{owl_id}\ttoken ", self.log()[0])

    def test_a_brief_a_killed_reporter_left_is_replaced_and_removed(self):
        (self.office / "runs" / "mcgonagall").mkdir(parents=True, mode=0o700, exist_ok=True)
        self.write_file(self.office / "runs" / "mcgonagall" / "owl-report.brief", "stale brief")
        self.send()
        owl_report.run(self.conn)
        [run] = self.runs()
        self.assertEqual(run["brief"], owl_report.BRIEF)
        self.assertFalse(os.path.lexists(run["brief_path"]))

    def test_a_folder_a_killed_run_left_is_removed_before_the_next_turn(self):
        stale = self.root / "turn-00000000000000aa"
        stale.mkdir(parents=True)
        self.write_file(stale / "owl.json", "{}")
        self.send()
        owl_report.run(self.conn)
        self.assertEqual(os.listdir(self.root), [])

    def test_the_lock_is_handed_to_the_turn_so_it_outlives_a_dead_reporter(self):
        self.send()
        real = subprocess.Popen
        seen = []

        def spy(*args, **kwargs):
            seen.append(kwargs.get("pass_fds"))
            return real(*args, **kwargs)

        with mock.patch.object(run_desk.subprocess, "Popen", side_effect=spy):
            owl_report.run(self.conn)
        [fds] = seen
        self.assertEqual(len(fds), 2)  # the reporter lock and the launch gate
        self.assertFalse(owl_report._running())  # released once the reporter and its turn are done


class SettingsTests(OwlReportCase):
    def settings(self) -> dict:
        return json.loads((OFFICE / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE).read_text())

    def refused(self, data: dict) -> None:
        with self.assertRaises(FleetError):
            run_desk.check_report_settings(json.dumps(data).encode())

    def test_the_kit_file_has_the_exact_shape(self):
        data = run_desk.check_report_settings(json.dumps(self.settings()).encode())
        self.assertEqual(data["permissions"]["allow"], [])

    def test_every_tool_and_every_required_entry_is_checked(self):
        for rule in run_desk.REPORT_REQUIRED_DENY:
            with self.subTest(missing_deny=rule):
                data = self.settings()
                data["permissions"]["deny"].remove(rule)
                self.refused(data)
        for path in run_desk.REPORT_REQUIRED_DENY_READ:
            with self.subTest(missing_deny_read=path):
                data = self.settings()
                data["sandbox"]["filesystem"]["denyRead"] = [entry for entry in data["sandbox"]["filesystem"]["denyRead"]
                                                             if not entry.endswith(path)]
                self.refused(data)
        for tool in ("Read", "Grep", "Glob", "Edit", "Write", "Bash", "WebFetch", "Task", "mcp__any__tool"):
            with self.subTest(allowed=tool):
                data = self.settings()
                data["permissions"]["allow"] = [f"{tool}(~/**)"]
                self.refused(data)
        broader = (
            ("a hook", lambda d: d.update(hooks={"Stop": []})),
            ("hooks on", lambda d: d.update(disableAllHooks=False)),
            ("an extra key", lambda d: d.update(env={"X": "1"})),
            ("a write", lambda d: d["sandbox"]["filesystem"].update(allowWrite=["/tmp"])),
            ("an extra read", lambda d: d["sandbox"]["filesystem"].update(allowRead=["/tmp"])),
            ("a domain", lambda d: d["sandbox"]["network"].update(allowedDomains=["example.com"])),
            ("sandbox off", lambda d: d["sandbox"].update(enabled=False)),
            ("a mode", lambda d: d["permissions"].update(defaultMode="acceptEdits")),
        )
        for label, change in broader:
            with self.subTest(broader=label):
                data = self.settings()
                change(data)
                self.refused(data)

    def test_a_refused_settings_file_runs_nothing_and_sends_the_plain_notification(self):
        data = self.settings()
        data["permissions"]["allow"] = ["Read(~/**)"]
        self.write_file(self.office / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE, json.dumps(data))
        self.send()
        self.assertEqual(owl_report.run(self.conn), ["failed"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.plain()), 1)


class RetryTests(OwlReportCase):
    def test_a_skipped_owl_is_tried_three_times_then_reported_with_the_fallback(self):
        skipped = self.send(subject="first")
        other = self.send(subject="second")
        self.mode(f"skip:{skipped}")
        owl_report.run(self.conn)
        self.assertEqual(sum(run["owl"] == skipped for run in self.runs()), config.OWL_REPORT_MAX_TRIES)
        texts = {call[0][0] for call in self.reports()}
        self.assertEqual(texts, {"ron says second", owl_report.FALLBACK})
        self.assertEqual(sorted(line.split()[3] for line in self.log()), sorted([skipped, other]))
        self.assertEqual(self.pending(), {})

    def test_an_owl_out_of_tries_gets_its_fallback_without_another_turn(self):
        owl_id = self.send()
        self.write_file(self.folder() / owl_id, json.dumps({"state": "pending", "tries": 3}))
        self.assertEqual(owl_report.run(self.conn), ["fallback"])
        self.assertEqual(self.runs(), [])
        self.assertTrue(self.log()[0].endswith(owl_report.FALLBACK))

    def test_a_reporter_killed_while_notifying_finishes_on_the_next_run_with_no_duplicate(self):
        first, second = self.send(subject="first"), self.send(subject="second")
        calls = []

        def killed_on_the_second(text, title="Hogwarts"):
            calls.append(text)
            if len(calls) == 2:
                raise SystemExit(143)
            return True

        self.notified.side_effect = killed_on_the_second
        with self.assertRaises(SystemExit):
            owl_report.run(self.conn)
        self.notified.side_effect = None
        self.notified.return_value = True
        [(left, marker)] = self.pending().items()
        self.assertEqual(marker["state"], "logged")
        spawned = self.spawned.call_count
        owl_post.run_pass(self.conn, now=NOW)
        self.assertEqual(self.spawned.call_count, spawned + 1)
        owl_report.run(self.conn)
        self.assertEqual(self.pending(), {})
        self.assertEqual(len(self.runs()), 2)  # no second turn for the owl already reported
        for owl_id in (first, second):
            self.assertEqual(len([line for line in self.log() if owl_id in line]), 1)

    def test_a_kill_between_the_log_line_and_its_marker_never_logs_twice(self):
        owl_id = self.send()
        real = markers.replace

        def killed_after_logging(fd, name, data):
            if data.get("state") == "logged":
                raise SystemExit(143)
            return real(fd, name, data)

        with mock.patch.object(owl_report.markers, "replace", side_effect=killed_after_logging), \
                self.assertRaises(SystemExit):
            owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id]["state"], "logging")
        owl_report.run(self.conn)
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.runs()), 1)

    def test_a_failed_notification_is_retried_then_kept_as_notify_failed_with_one_event(self):
        owl_id = self.send()
        self.notified.return_value = False
        owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id], {"state": "logged", "summary": "ron says status", "notify_tries": 1})
        owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id]["notify_tries"], 2)
        owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id]["state"], "done")  # kept, never deleted, and not retried
        owl_report.run(self.conn)
        self.assertEqual(len(self.reports()), config.OWL_REPORT_NOTIFY_TRIES)
        events = self.conn.execute("SELECT summary FROM events WHERE kind = 'owl-report.notify-failed'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn(f"The owl report on {owl_id} from ron", events[0]["summary"])
        self.assertIn("owl-reports.log, but its notification failed 3 times", events[0]["summary"])
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(len(self.runs()), 1)

    def test_a_kill_after_notify_failed_still_raises_its_event_once(self):
        owl_id = self.send()
        self.notified.return_value = False
        real = owl_report.pensieve.add_event

        def killed(conn, desk, kind, *args, **kwargs):
            if kind == "owl-report.notify-failed":
                raise SystemExit(143)
            return real(conn, desk, kind, *args, **kwargs)

        owl_report.run(self.conn)
        owl_report.run(self.conn)
        with mock.patch.object(owl_report.pensieve, "add_event", side_effect=killed), self.assertRaises(SystemExit):
            owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id]["state"], "notify_failed")
        owl_report.run(self.conn)
        owl_report.run(self.conn)
        self.assertEqual(self.pending()[owl_id]["state"], "done")
        count = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'owl-report.notify-failed'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_failed_turn_sends_the_plain_notification_once_it_shows(self):
        owl_id = self.send(subject="first")
        self.mode("fail")
        self.notified.return_value = False
        owl_report.run(self.conn)
        self.assertNotIn("plain_sent", self.pending()[owl_id])  # it did not show
        self.notified.return_value = True
        owl_report.run(self.conn)
        self.assertTrue(self.pending()[owl_id]["plain_sent"])
        owl_report.run(self.conn)
        plain = self.plain()
        self.assertEqual(len(plain), 2)
        self.assertEqual(plain[-1][0][0], f"ron on {self.task['id']}: fyi: first")
        self.assertEqual(self.pending()[owl_id]["tries"], 3)
        owl_report.run(self.conn)  # out of tries: the fallback, no fourth turn
        self.assertEqual(len(self.runs()), 3)
        self.assertTrue(self.log()[0].endswith(owl_report.FALLBACK))

    def test_output_past_the_cap_is_a_failed_turn(self):
        self.send()
        self.mode("big")
        self.assertEqual(owl_report.run(self.conn), ["failed"])
        self.assertEqual(self.reports(), [])

    def test_a_reporter_that_cannot_start_falls_back_to_the_plain_notification_once(self):
        self.spawned.side_effect = OSError("no fork")
        self.send(subject="first")
        owl_post.run_pass(self.conn, now=NOW)
        self.assertEqual(len(self.plain()), 1)
        self.assertEqual(len(self.pending()), 1)


class AuthTests(OwlReportCase):
    def test_an_auth_failure_alerts_once_and_waits_for_an_owl_outside_its_snapshot(self):
        first = self.send(subject="first")
        self.mode("auth")
        self.assertEqual(owl_report.run(self.conn), ["auth"])
        self.assertEqual(owl_report.run(self.conn), [])
        events = self.conn.execute("SELECT summary FROM events WHERE kind = 'owl-report.auth'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertNotIn("API key", events[0]["summary"])
        self.assertEqual([call[0][0] for call in self.plain()].count("owl watcher: auth failed"), 1)
        self.assertEqual(self.pending()[first]["tries"], 0)
        spawned = self.spawned.call_count
        owl_post.run_pass(self.conn, now=NOW)
        self.assertEqual(self.spawned.call_count, spawned)
        self.mode("ok")
        second = self.send(subject="second")
        self.assertEqual(self.spawned.call_count, spawned + 1)
        owl_report.run(self.conn)
        self.assertEqual(sorted(line.split()[3] for line in self.log()), sorted([first, second]))
        self.assertEqual([call[0][0] for call in self.plain()].count("owl watcher: auth failed"), 1)

    def test_an_api_401_is_an_auth_failure_and_report_text_never_is(self):
        self.send()
        self.mode("auth401")
        self.assertEqual(owl_report.run(self.conn), ["auth"])
        self.mode("login-words")
        self.send()
        self.assertEqual(owl_report.run(self.conn), ["done", "done"])
        self.assertEqual(self.pending(), {})

    def test_a_failure_that_is_not_about_sign_in_blocks_nothing(self):
        self.send()
        self.mode("overloaded")
        self.assertEqual(owl_report.run(self.conn), ["failed"])
        self.assertFalse((self.folder() / owl_report.BLOCKED).exists())

    def test_an_alert_a_kill_cut_short_is_finished_by_the_next_pass(self):
        self.send()
        self.mode("auth")
        with mock.patch.object(owl_report, "_shown", side_effect=SystemExit(143)), self.assertRaises(SystemExit):
            owl_report.run(self.conn)
        blocked = json.loads((self.folder() / owl_report.BLOCKED).read_text())
        self.assertEqual(blocked["alert"], "pending")
        owl_post.run_pass(self.conn, now=NOW)
        owl_post.run_pass(self.conn, now=NOW)
        self.assertEqual([call[0][0] for call in self.plain()].count("owl watcher: auth failed"), 1)
        events = self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'owl-report.auth'").fetchone()[0]
        self.assertEqual(events, 1)
        self.assertEqual(json.loads((self.folder() / owl_report.BLOCKED).read_text())["alert"], "sent")


class SwitchTests(OwlReportCase):
    def test_switched_off_nothing_spawns_and_the_plain_notification_stays(self):
        os.unlink(self.office / config.OWL_REPORTS_FILE)
        with mock.patch.object(mcgonagall_inbox, "announce", REAL_ANNOUNCE):
            self.send(subject="first")
        self.spawned.assert_not_called()
        self.assertEqual(self.pending(), {})
        self.assertEqual([call[0][0] for call in self.notified.call_args_list],
                         [f"ron on {self.task['id']}: fyi: first"])

    def test_switched_on_the_plain_notification_is_held_back_only_for_a_marked_owl(self):
        with mock.patch.object(mcgonagall_inbox, "announce", REAL_ANNOUNCE):
            self.send(subject="first")
            self.notified.assert_not_called()
            with mock.patch.object(owl_report, "mark", side_effect=OSError("disk full")):
                self.send(subject="second")
        self.assertEqual([call[0][0] for call in self.notified.call_args_list],
                         [f"ron on {self.task['id']}: fyi: second"])
        events = self.conn.execute("SELECT kind FROM events WHERE kind = 'owl.to-mcgonagall'").fetchall()
        self.assertEqual(len(events), 2)

    def test_switching_off_mid_run_stops_and_keeps_the_markers(self):
        self.send(subject="first")
        self.send(subject="second")
        real = owl_report.on
        calls = []

        def off_after_the_first_turn():
            calls.append(1)
            return real() if len(self.runs()) < 1 else False

        with mock.patch.object(owl_report, "on", side_effect=off_after_the_first_turn):
            owl_report.run(self.conn)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(len(self.pending()), 2)  # the first is held before publishing, the second untouched
        self.assertEqual(self.reports(), [])


class LogRecordTests(OwlReportCase):
    def log_path(self):
        return self.castle / "desks" / "mcgonagall" / owl_report.LOG_FILE

    def test_an_owl_id_inside_another_report_does_not_count_as_logged(self):
        first = self.send(subject="first")
        second = self.send(subject="second")
        self.write_file(self.log_path(), owl_report.record(
            {"sender": "ron", "task_id": None, "id": first}, f"see also {second} and\tmore", NOW).decode())
        owl_report.run(self.conn)
        records = [line.split("\t") for line in self.log()]
        self.assertTrue(all(len(fields) == 5 for fields in records))
        self.assertEqual(sorted(fields[3] for fields in records), sorted([first, second]))
        self.assertEqual(records[0][4], f"see also {second} and more")  # no tab inside a field

    def test_a_record_cut_short_never_counts_and_the_next_one_starts_on_its_own_line(self):
        owl_id = self.send()
        whole = owl_report.record({"sender": "ron", "task_id": self.task["id"], "id": owl_id}, "ron says status", NOW)
        self.write_file(self.log_path(), whole[:-10])  # a kill or a full disk cut it short
        self.write_file(self.folder() / owl_id, json.dumps({"state": "logging", "summary": "ron says status"}))
        owl_report.run(self.conn)
        raw = self.log_path().read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        lines = raw.decode().split("\n")[:-1]
        self.assertEqual(lines[0], whole[:-10].decode())
        self.assertEqual(lines[1].split("\t")[3:], [owl_id, "ron says status"])
        self.assertEqual(self.runs(), [])  # recovered from the marker's own summary, no new turn
        self.assertEqual(self.pending(), {})


class AuthWatermarkTests(OwlReportCase):
    def test_four_hundred_pending_owls_block_with_a_small_watermark(self):
        from hogwarts import owlery
        for index in range(400):
            owl = owlery.send(self.conn, "ron", "mcgonagall", "fyi", f"note {index}", body="x", now=NOW)
            owlery.mark_delivered(self.conn, owl["id"], now=NOW + index)
            self.write_file((self.folder() if self.folder().exists() else self._folder()) / owl["id"],
                            json.dumps({"state": "pending", "tries": 0}))
        self.mode("auth")
        self.assertEqual(owl_report.run(self.conn), ["auth"])
        raw = (self.folder() / owl_report.BLOCKED).read_bytes()
        self.assertLess(len(raw), 512)
        self.assertEqual(json.loads(raw)["mark"][0], NOW + 399)
        self.assertEqual(owl_report.run(self.conn), [])
        self.assertEqual(owl_report.kick(self.conn), "waiting for a new owl after an auth failure")
        self.mode("ok")
        self.count = 500
        self.send(subject="after")
        self.assertIn("done", owl_report.run(self.conn))

    def _folder(self):
        self.folder().mkdir(mode=0o700)
        return self.folder()

    def test_a_stale_writer_never_restores_an_old_block_and_the_alert_is_sent_once(self):
        self.send()
        self.mode("auth")
        with mock.patch.object(owl_report, "_shown", return_value=False):
            owl_report.run(self.conn)  # the alert could not show: still pending
        fd = os.open(self.folder(), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        old = markers.read(fd, owl_report.BLOCKED)
        self.assertEqual(old["alert"], "pending")
        os.unlink(self.folder() / owl_report.BLOCKED)  # a reporter lifted it meanwhile
        self.assertFalse(owl_report._replace_block(fd, old["token"], {**old, "alert": "sent"}))
        self.assertFalse((self.folder() / owl_report.BLOCKED).exists())
        markers.replace(fd, owl_report.BLOCKED, old)
        with owl_report._alert_lock() as mine:  # another publisher holds the alert lock: kick leaves it to them
            self.assertTrue(mine)
            owl_report.kick(self.conn)
        self.assertEqual([call[0][0] for call in self.plain()].count("owl watcher: auth failed"), 0)
        owl_report.kick(self.conn)
        owl_report.kick(self.conn)
        self.assertEqual([call[0][0] for call in self.plain()].count("owl watcher: auth failed"), 1)

    def test_a_pending_alert_waits_while_the_switch_is_off(self):
        self.send()
        self.mode("auth")
        with mock.patch.object(owl_report, "_shown", return_value=False):
            owl_report.run(self.conn)
        os.unlink(self.office / config.OWL_REPORTS_FILE)
        fd = os.open(self.folder(), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        owl_report._alert(self.conn, fd)
        self.assertEqual(self.plain(), [])
        self.assertEqual(markers.read(fd, owl_report.BLOCKED)["alert"], "pending")


class GateTests(OwlReportCase):
    def test_a_stop_or_an_update_runs_no_turn_and_counts_no_try(self):
        owl_id = self.send()
        state = self.office / config.STATE_DIR
        state.mkdir(mode=0o700, exist_ok=True)
        self.write_file(state / config.STOP_FILE, "")
        self.assertEqual(owl_report.run(self.conn), ["stopped"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.pending()[owl_id]["tries"], 0)
        os.unlink(state / config.STOP_FILE)
        with run_desk.safefs.opened_dir(str(self.office), "locks", create=True) as locks_fd, \
                run_desk.safefs.held_lock(locks_fd, config.UPDATE_LOCK, blocking=False):
            self.assertEqual(owl_report.run(self.conn), ["stopped"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.pending()[owl_id]["tries"], 0)
        self.assertEqual(owl_report.run(self.conn), ["done"])

    def test_a_blocked_model_runs_no_turn_and_sends_the_plain_notification_once(self):
        self.send()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("haiku",)):
            self.assertEqual(owl_report.run(self.conn), ["stopped"])
            self.assertEqual(owl_report.run(self.conn), ["stopped"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.plain()), 1)


class HungTurnTests(OwlReportCase):
    def test_a_hung_turn_a_dead_reporter_left_is_killed_once_past_its_deadline(self):
        self.mode("ok")
        settings = f"{self.office}/desks/mcgonagall/{config.OWL_REPORT_SETTINGS_FILE}"
        hung = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        self.addCleanup(hung.kill)
        self.assertFalse(run_desk.kill_report_turn(hung.pid))  # not the report command: left alone
        self.assertIsNone(hung.poll())
        fake = subprocess.Popen([config.CLAUDE_BIN, "--settings", settings, "hang"], start_new_session=True,
                                cwd=str(self.tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(fake.kill)
        with mock.patch.object(run_desk, "report_turn_command",
                               return_value=f"{config.CLAUDE_BIN} --settings {settings} hang"):
            self.send()
            fd = os.open(self.folder(), os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            old = int(time.time()) - config.OWL_REPORT_TIMEOUT_SECONDS - owl_report.REAP_MARGIN_SECONDS - 5
            markers.replace(fd, owl_report.TURN, {"state": "running", "pid": fake.pid, "at": int(time.time())})
            with mock.patch.object(owl_report, "_running", return_value=True):
                self.assertEqual(owl_report.kick(self.conn), "a reporter is running")  # not past its deadline
                markers.replace(fd, owl_report.TURN, {"state": "running", "pid": fake.pid, "at": old})
                self.assertEqual(owl_report.kick(self.conn), "a hung turn was stopped")
        self.assertIsNone(hung.poll())
        fake.wait(timeout=10)
        self.assertEqual(fake.returncode, -9)  # killed, not ended on its own
        self.assertFalse((self.folder() / owl_report.TURN).exists())
