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

    def test_reviews_that_need_ryan_are_loud(self):
        for kind in ("review.auto", "review.unpublished", "review.interrupted", "review.fix-round", "push.draft-pr",
                     "go.refused", "orchestrator.notify", "orchestrator.cap", "orchestrator.ask-snape"):
            self.assertIn(kind, config.PHONE_KINDS)
