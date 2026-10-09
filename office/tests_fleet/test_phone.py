"""Phone delivery: each loud headmaster event pings Ryan once, through the overlay's command when one is set, else the
macOS notification. The command is a fake script these tests write: it records its argv and stdin and exits as asked.
No real notification or message is ever sent."""
from __future__ import annotations

import json
import os
import stat
from unittest import mock

from hogwarts import pensieve
from tests.support import NOW

from fleet import config, markers, phone, safefs
from fleet.safefs import FleetError
from tests_fleet.support import FleetCase

REAL_DELIVER = phone.deliver  # captured before FleetCase replaces it
FAKE = """#!/usr/bin/python3
import json, os, sys, time
state = {state!r}
data = sys.stdin.read()
with open(os.path.join(state, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv, "stdin": data, "env": sorted(os.environ)}}) + "\\n")
mode = open(os.path.join(state, "mode")).read().strip()
if mode == "hang":
    time.sleep(30)
sys.exit(0 if mode == "ok" else 3)
"""
PR = "https://github.com/acme/web-app/pull/7"


class PhoneCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "fake-phone"
        self.state.mkdir(mode=0o700)
        self.mode("ok")
        self.command = self.write_file(self.state / "send", FAKE.format(state=str(self.state)), mode=0o700)
        os.chmod(self.command, stat.S_IRWXU)
        self.task = pensieve.create_task(self.conn, "harry", "build it", now=NOW)
        self.count = 0
        self.deliver()  # the first deliver records where to start, so nothing before this test pings

    def mode(self, value: str) -> None:
        self.write_file(self.state / "mode", value)

    def calls(self) -> list:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def deliver(self) -> list:
        return REAL_DELIVER(self.conn)

    def event(self, kind: str, summary: str = "something needs you", verdict: str = "headmaster") -> int:
        self.count += 1
        return pensieve.add_event(self.conn, "mcgonagall", kind, verdict, summary, task_id=self.task["id"],
                                  dedupe_key=f"test:{self.count}", now=NOW + self.count)["id"]

    def marker(self, event_id: int) -> dict:
        with safefs.opened_dir(config.OFFICE_ROOT, phone.PHONE_DIR) as fd:
            return markers.read(fd, f"ev-{event_id}")

    def configured(self, argv=None):
        return mock.patch.object(config, "PHONE_COMMAND", (str(self.command),) if argv is None else argv)


class FirstRunTests(PhoneCase):
    def test_the_first_deliver_sends_no_backlog(self):
        with safefs.opened_dir(config.OFFICE_ROOT, phone.PHONE_DIR) as fd:
            os.unlink(phone.WATERMARK, dir_fd=fd)
        self.event("go.refused")
        self.assertEqual(self.deliver(), [])
        self.notified.assert_not_called()
        self.event("go.refused")
        self.assertEqual(self.deliver(), ["sent"])


class TransportTests(PhoneCase):
    def test_without_a_command_a_loud_event_falls_back_to_macos(self):
        event_id = self.event("push.draft-pr", f"task passed: draft PR {PR} is open")
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.notified.call_count, 1)
        text, title = self.notified.call_args[0]
        self.assertEqual((text, title), (f"task passed: draft PR {PR} is open {PR}",
                                         f"Hogwarts: push.draft-pr {self.task['id']}"))
        self.assertEqual(self.marker(event_id), {"state": "sent", "via": "macos", "primary": "unconfigured"})

    def test_the_overlay_command_gets_the_payload_on_stdin_and_nothing_in_argv(self):
        event_id = self.event("push.draft-pr", f"draft PR {PR} is open; token sk-ant-{'a' * 40}")
        with self.configured():
            self.assertEqual(self.deliver(), ["sent"])
        self.notified.assert_not_called()
        call = self.calls()[0]
        self.assertEqual(call["argv"], [str(self.command)])
        payload = json.loads(call["stdin"])
        self.assertEqual(set(payload), {"event_id", "kind", "task_id", "line", "pr_link"})
        self.assertEqual((payload["event_id"], payload["kind"], payload["task_id"], payload["pr_link"]),
                         (event_id, "push.draft-pr", self.task["id"], PR))
        self.assertNotIn("a" * 40, payload["line"])
        # Only the fleet's child environment, plus what the /usr/bin/python3 shim adds itself.
        shim = {"__CF_USER_TEXT_ENCODING", "CPATH", "LIBRARY_PATH", "MANPATH", "SDKROOT"}
        self.assertLessEqual(set(call["env"]) - shim, set(phone.run_desk.child_env()))
        self.assertEqual(self.marker(event_id), {"state": "sent", "via": "command", "primary": "ok"})

    def test_a_failing_or_hung_command_falls_back_to_macos(self):
        self.mode("fail")
        first = self.event("review.headmaster")
        with self.configured():
            self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.marker(first), {"state": "sent", "via": "macos", "primary": "failed"})
        self.mode("hang")
        second = self.event("review.headmaster")
        with self.configured(), mock.patch.object(config, "PHONE_COMMAND_TIMEOUT_SECONDS", 1):
            self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.marker(second), {"state": "sent", "via": "macos", "primary": "failed"})
        self.assertEqual(self.notified.call_count, 2)

    def test_a_malformed_command_counts_as_unconfigured(self):
        for argv in (("relative/send",), (), "/bin/echo hi", (str(self.command), 3), (str(self.command) + "\x00",)):
            with self.subTest(argv=argv):
                self.assertIsNone(self._primary(argv))
        event_id = self.event("go.refused")
        with self.configured(("send",)):
            self.deliver()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.marker(event_id)["primary"], "unconfigured")

    def _primary(self, argv):
        with self.configured(argv):
            return phone.primary()

    def test_nothing_delivered_is_recorded_too(self):
        self.notified.return_value = False
        event_id = self.event("go.refused")
        self.assertEqual(self.deliver(), ["undelivered"])
        self.assertEqual(self.marker(event_id), {"state": "undelivered", "via": "none", "primary": "unconfigured"})
        self.assertEqual(self.deliver(), [])  # still one try, never a second ping


class DedupeTests(PhoneCase):
    def test_one_event_one_ping(self):
        self.event("orchestrator.notify")
        with self.configured():
            self.assertEqual(self.deliver(), ["sent"])
            self.assertEqual(self.deliver(), [])
        self.assertEqual(len(self.calls()), 1)

    def test_an_event_another_deliver_took_is_never_sent_again(self):
        event_id = self.event("orchestrator.cap")
        with safefs.opened_dir(config.OFFICE_ROOT, phone.PHONE_DIR) as fd:
            markers.publish(fd, f"ev-{event_id}", {"state": "sending"})
        self.assertEqual(self.deliver(), [])
        self.notified.assert_not_called()

    def test_only_loud_headmaster_events_ping(self):
        self.event("owlpost.rejected")
        self.event("push.draft-pr", verdict="routine")
        self.event("orchestrator.action", verdict="routine")
        self.assertEqual(self.deliver(), [])
        loud = self.event("orchestrator.rejected")
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.marker(loud)["state"], "sent")

    def test_a_burst_pings_each_up_to_the_limit_then_once_for_the_rest(self):
        ids = [self.event("go.refused") for _ in range(config.PHONE_MAX_PER_PASS + 3)]
        self.assertEqual(self.deliver(), ["sent"] * config.PHONE_MAX_PER_PASS + ["batched 3"])
        self.assertEqual(self.notified.call_count, config.PHONE_MAX_PER_PASS + 1)
        self.assertIn("3 more loud events", self.notified.call_args[0][0])
        self.assertTrue(self.marker(ids[-1])["batched"])
        self.assertEqual(self.deliver(), [])

    def test_a_deliver_already_running_sends_nothing(self):
        self.event("go.refused")
        with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
                safefs.held_lock(locks_fd, phone.LOCK, blocking=False):
            self.assertEqual(self.deliver(), ["another deliver is running"])
        self.notified.assert_not_called()
        self.assertEqual(self.deliver(), ["sent"])

    def test_the_kit_ships_no_command(self):
        from tests_fleet.support import kit_setting

        self.assertIsNone(kit_setting("PHONE_COMMAND"))


class HardeningTests(PhoneCase):
    def test_a_link_the_scrub_touches_is_never_sent(self):
        token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        self.event("push.draft-pr", f"draft PR https://github.com/{token}/repo/pull/3 is open")
        with self.configured():
            self.deliver()
        payload = json.loads(self.calls()[0]["stdin"])
        self.assertIsNone(payload["pr_link"])
        self.assertNotIn(token, json.dumps(payload))

    def test_an_unreadable_watermark_sends_nothing_and_skips_nothing(self):
        self.event("go.refused")
        with safefs.opened_dir(config.OFFICE_ROOT, phone.PHONE_DIR) as fd:
            markers.replace(fd, phone.WATERMARK, {"state": "at", "id": "seven"})
        with self.assertRaisesRegex(FleetError, "watermark"):
            self.deliver()
        self.notified.assert_not_called()
        with safefs.opened_dir(config.OFFICE_ROOT, phone.PHONE_DIR) as fd:
            self.assertEqual(markers.read(fd, phone.WATERMARK)["id"], "seven")

    def test_tooling_blocks_outages_and_sign_in_failures_are_loud(self):
        for kind in ("review.blocked-on-tooling", "failover.down", "failover.wait", "failover.wait-ended",
                     "failover.auth"):
            self.assertIn(kind, config.PHONE_KINDS)
        self.assertNotIn("failover.up", config.PHONE_KINDS)

    def test_a_loud_event_settled_before_the_pass_still_pings_once(self):
        refused = self.event("go.refused")
        self.event("go.confirmed", verdict="routine")
        self.assertEqual(pensieve.settle_events(self.conn, now=NOW + 100)["acked"], 1)
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.marker(refused)["state"], "sent")
        self.assertEqual(self.deliver(), [])

    def test_reviews_that_need_ryan_are_loud(self):
        for kind in ("review.auto", "review.unpublished", "review.interrupted", "review.fix-round", "push.draft-pr",
                     "go.refused", "orchestrator.notify", "orchestrator.cap", "orchestrator.ask-snape"):
            self.assertIn(kind, config.PHONE_KINDS)


class GoWatchTests(PhoneCase):
    """While go updates are on, a loud event one of their lines stands for, on a watched go task or build, is not
    pinged here too; every other loud event still is."""

    def setUp(self) -> None:
        super().setUp()
        from hogwarts import ids, owlery

        self.go = "tk_00000000000000a0"
        pensieve.create_task(self.conn, "mcgonagall", "the go", intent_path=ids.intent_path(self.go), task_id=self.go,
                             now=NOW)
        pensieve.record_spec(self.conn, self.go, "/private/tmp/checkout", "fix/widget", "origin/main", "a" * 64,
                             now=NOW)
        self.build = owlery.open_request(self.conn, "mcgonagall", "harry", "build it", parent_task_id=self.go,
                                         now=NOW)["task"]["id"]

    def on(self, value: str = "on") -> None:
        self.write_file(self.office / config.GO_UPDATES_FILE, value + "\n")

    def loud(self, task_id, kind: str) -> int:
        self.count += 1
        return pensieve.add_event(self.conn, "mcgonagall", kind, "headmaster", "something needs you",
                                  task_id=task_id, dedupe_key=f"test:{self.count}", now=NOW + self.count)["id"]

    def standing(self, state: str, line: dict = None):
        """Where the go task stands now, as go updates read it, and the marker of that state's line (none for None)."""
        from fleet import go_watch

        key = {"build": self.build, "state": state, "round": 1, "verdict": None, "event": None}
        line = {"state": "sent", "via": "macos", "primary": "unconfigured"} if line is None else line
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR) as fd:
            name = go_watch._marker(json.loads(safefs.read_regular(fd, go_watch.STATE, 4096))["epoch"], self.go, key)
            if os.path.lexists(self.office / config.GO_WATCH_DIR / name):
                os.unlink(name, dir_fd=fd)
            if line:
                markers.publish(fd, name, line)
        return mock.patch.object(go_watch, "current", return_value={self.go: (key, None)})

    def test_go_watch_on_skips_a_loud_event_its_go_task_stands_in_now(self):
        from fleet import go_watch

        self.on()
        go_watch.watch(self.conn)  # its baseline: the go task and its build are watched from here
        for kind, state in go_watch.COVERS.items():
            with self.subTest(kind=kind), self.standing(state):
                event_id = self.loud(self.go if kind == "go.refused" else self.build, kind)
                self.assertEqual(self.deliver(), [])
                self.assertEqual(self.marker(event_id), {"state": "covered", "via": "go-watch"})
        self.notified.assert_not_called()
        # A real refusal: once go updates sent its line, as the Owl Post's pass does first, that is the one ping.
        refused = self.loud(self.go, "go.refused")
        self.assertEqual(go_watch.watch(self.conn), ["sent"])
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.marker(refused)["state"], "covered")
        self.assertEqual(self.notified.call_count, 1)

    def test_go_watch_on_pings_an_event_whose_line_is_not_claimed_yet(self):
        from fleet import go_watch

        self.on()
        go_watch.watch(self.conn)
        for line in ({}, {"state": "sending"}, {"state": "undelivered", "via": "none", "primary": "unconfigured"},
                     {"state": "sent", "via": "macos", "primary": "unconfigured", "batched": True}):
            with self.subTest(line=line), self.standing("headmaster", line):
                # No line of its own reached Ryan for where it stands (none yet, cut short, failed or only counted in
                # a summary), so the loud event is never held back.
                event_id = self.loud(self.build, "review.headmaster")
                self.assertEqual(self.deliver(), ["sent"])
                self.assertEqual(self.marker(event_id)["state"], "sent")
        self.loud(self.go, "go.refused")
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR) as fd:  # its state, but not one usable key
            safefs.write_new(fd, go_watch.STATE, json.dumps({"state": "watching", "epoch": "0" * 8, "tasks": {
                self.go: {"build": None, "state": [], "round": 0, "verdict": None, "event": None}}}).encode())
        self.assertEqual(self.deliver(), ["sent"])

    def test_go_watch_on_still_pings_every_other_loud_event(self):
        from fleet import go_watch

        self.on()
        go_watch.watch(self.conn)
        with self.standing("handoff"):
            pinged = [self.loud(self.build, "review.headmaster"),  # the go task has moved on from it
                      self.loud(self.build, "push.draft-pr"),  # no PR is bound, so no update carries its link
                      self.loud(self.build, "review.auto"), self.loud(self.build, "push.auto-failed"),
                      self.loud(self.build, "review.interrupted"), self.loud(self.build, "ollivander.stopped"),
                      self.loud(self.task["id"], "review.headmaster"), self.loud(None, "failover.down")]
            with mock.patch.object(config, "PHONE_MAX_PER_PASS", 20):
                self.assertEqual(self.deliver(), ["sent"] * len(pinged))
        for event_id in pinged:
            self.assertEqual(self.marker(event_id)["state"], "sent")
        self.assertEqual(self.deliver(), [])

    def test_go_watch_off_or_unreadable_skips_nothing(self):
        from fleet import go_watch

        self.on()
        go_watch.watch(self.conn)
        self.on("off")
        self.loud(self.build, "review.headmaster")
        self.assertEqual(self.deliver(), ["sent"])
        self.on()
        with self.standing("headmaster"):
            with mock.patch.object(go_watch, "current", side_effect=FleetError("held runs unreadable")):
                self.loud(self.build, "review.headmaster")
                self.assertEqual(self.deliver(), ["sent"])
            with safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR, create=True) as fd:
                safefs.write_new(fd, go_watch.STATE, b"not json\n")
            self.loud(self.build, "review.headmaster")
            self.assertEqual(self.deliver(), ["sent"])

    def test_go_watch_before_its_first_pass_skips_nothing(self):
        self.on()
        self.loud(self.go, "go.refused")
        self.assertEqual(self.deliver(), ["sent"])

    def test_go_watch_on_an_escalation_to_ryan_pings_once_through_its_go_update(self):
        from hogwarts import owlery

        from fleet import go_watch

        self.on()
        go_watch.watch(self.conn)
        pensieve.start_task(self.conn, self.build, now=NOW)
        request = pensieve.get_task(self.conn, self.build)["request_id"]
        # The live sequence: Harry's handoff reaches McGonagall, then her orchestrator turn tells Ryan to rule on
        # scope and records that it did. Each pass runs go updates before phone.deliver, as the orchestrator does.
        self.count += 1
        owlery.send(self.conn, "harry", "mcgonagall", "result", "handoff", body="COMMIT MESSAGE\nwidget\n",
                    task_id=self.build, request_id=request, now=NOW + self.count)
        self.loud(self.build, "owl.to-mcgonagall")
        self.assertEqual(go_watch.watch(self.conn), ["sent"])
        self.assertEqual(self.deliver(), [])
        notify = self.loud(self.build, "orchestrator.notify")
        self.count += 1
        pensieve.add_event(self.conn, "mcgonagall", "orchestrator.action", "routine", "told Ryan", task_id=self.build,
                           dedupe_key=f"test:{self.count}", now=NOW + self.count)
        self.assertEqual(go_watch.watch(self.conn), ["sent"])
        self.assertEqual(self.deliver(), [])
        self.assertEqual(self.marker(notify), {"state": "covered", "via": "go-watch"})
        self.assertEqual(self.notified.call_count, 2)  # handed off, then the escalation's one ping
        self.assertTrue(self.notified.call_args[0][0].endswith(go_watch.STATES["owner"][1]))
        # Two escalations before one pass: the line is the newer one's, so the older one pings itself.
        first, second = self.loud(self.build, "orchestrator.notify"), self.loud(self.go, "orchestrator.notify")
        self.assertEqual(go_watch.watch(self.conn), ["sent"])
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual((self.marker(first)["state"], self.marker(second)["state"]), ("sent", "covered"))
        # An escalation whose line has not gone (a busy watcher, or no pass yet) pings itself, never dropped.
        third = self.loud(self.build, "orchestrator.notify")
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual(self.marker(third)["state"], "sent")
        self.assertEqual(self.notified.call_count, 5)
        # The pass that comes later logs its line, but that ping was the escalation's one.
        self.assertEqual(go_watch.watch(self.conn), ["covered"])
        self.assertEqual(self.notified.call_count, 5)
        self.assertEqual(self.marker(third)["state"], "sent")
        logged = (self.office / "logs" / config.GO_UPDATES_LOG).read_text().splitlines()
        self.assertTrue(logged[-1].endswith(go_watch.STATES["owner"][1]))
        self.assertEqual(self.deliver(), [])
