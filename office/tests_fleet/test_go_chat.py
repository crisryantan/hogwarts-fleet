"""McGonagall's chat watch, the Stop hook (fleet/hooks/stop.py): at the end of each of her turns it waits, read only,
on her open go tasks, and exits 2 with each changed go task's fixed line, which wakes her. The store is written only
by the test itself, standing in for the desks and scripts that move a go task on."""
from __future__ import annotations

import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import db, pensieve

from fleet import config, safefs
from fleet.hooks import stop
from tests_fleet.test_go_wait import Clock
from tests_fleet.support import IN_KIT, ONLY_IN_KIT
from tests_fleet.test_go_watch import SHAS, GoWatchCase

SESSION = "0b6f8c1e-1111-4222-8333-944455556666"
KIT = Path(__file__).resolve().parents[2]
COMMAND = ("/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0,"
           " \"/Users/crisryantan/.hogwarts\"); from fleet.hooks.stop import main; sys.exit(main())'")


class ChatCase(GoWatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = 4242

    def payload(self, **fields) -> dict:
        return {"session_id": SESSION, "transcript_path": "", "cwd": str(self.castle), "hook_event_name": "Stop",
                "stop_hook_active": False, "agent_type": "mcgonagall", **fields}

    def stop(self, clock: Clock = None, payload: dict = None, max_seconds: int = 20) -> tuple:
        clock = clock or Clock()
        out, err = io.StringIO(), io.StringIO()
        raw = json.dumps(self.payload() if payload is None else payload).encode("utf-8")
        with mock.patch.object(config, "GO_CHAT_MAX_SECONDS", max_seconds):
            code = stop.main(argv=[], stdin=io.BytesIO(raw), stdout=out, stderr=err, clock=clock, sleep=clock.sleep,
                             parent=lambda: self.parent)
        self.assertEqual(out.getvalue(), "")
        return code, err.getvalue(), clock

    def woke(self, *lines: str) -> str:
        return "\n".join([stop.HEAD, *lines]) + "\n"

    def folder(self) -> Path:
        return self.office / config.GO_CHAT_DIR

    def told(self) -> dict:
        name = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:32] + stop.TOLD
        return json.loads((self.folder() / name).read_text())["tasks"]

    def baseline(self) -> None:
        """The first Stop of the session: where things stand is taken as told, and nothing wakes her."""
        self.assertEqual(self.stop()[:2], (0, ""))


class QuietTests(ChatCase):
    def test_quiet_no_open_go_task_exits_at_once_with_nothing_and_writes_nothing(self):
        pensieve.close_task(self.conn, self.go, "abandoned", now=self.tick())
        code, said, clock = self.stop()
        self.assertEqual((code, said, clock.sleeps), (0, "", 0))
        self.assertFalse(self.folder().exists())

    def test_quiet_another_session_or_a_subagent_never_waits(self):
        for payload in (self.payload(agent_type=None), self.payload(agent_id="a1"), self.payload(session_id=None),
                        {k: v for k, v in self.payload().items() if k != "agent_type"}):
            with self.subTest(payload=payload):
                code, said, clock = self.stop(payload=payload)
                self.assertEqual((code, said, clock.sleeps), (0, "", 0))
        self.assertFalse(self.folder().exists())

    def test_quiet_the_first_wait_takes_where_things_stand_as_told(self):
        build = self.build()
        self.handoff(build)
        code, said, clock = self.stop()
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(clock.sleeps, 4)  # it waited the whole max wait
        self.assertEqual(set(self.told()), {self.go})

    def test_quiet_a_second_waiter_for_the_same_session_exits_at_once(self):
        self.baseline()
        self.handoff(self.build())
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
            name = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:32] + stop.LOCK
            with safefs.held_lock(fd, name, blocking=False):
                code, said, clock = self.stop()
        self.assertEqual((code, said, clock.sleeps), (0, "", 0))
        self.assertEqual(self.stop()[0], stop.WAKE)  # the change is still there for the waiter that holds the lock

    def test_quiet_max_wait_with_no_change_exits_0(self):
        self.baseline()
        code, said, clock = self.stop(max_seconds=config.GO_WAIT_POLL_SECONDS * 3)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(clock.sleeps, 3)
        self.assertLess(config.GO_CHAT_MAX_SECONDS, config.GO_CHAT_HOOK_TIMEOUT_SECONDS)

    def test_quiet_a_wait_whose_session_is_gone_exits_without_recording(self):
        self.baseline()
        before, made = self.told(), []

        def gone():
            self.parent = 1
            made.append(self.build())

        code, said, _ = self.stop(clock=Clock({1: gone}))
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(self.told(), before)
        self.parent = 4242
        self.assertEqual(self.stop()[1], self.woke(self.sent(self.go, made[0]["id"], "confirmed")))


class WakeTests(ChatCase):
    def test_wake_a_change_exits_2_with_the_fixed_line_and_is_never_repeated(self):
        build = self.build()
        self.baseline()
        clock = Clock({2: lambda: self.handoff(build)})
        code, said, clock = self.stop(clock=clock)
        self.assertEqual(code, stop.WAKE)
        self.assertEqual(said, self.woke(self.sent(self.go, build["id"], "handoff")))
        self.assertEqual(clock.sleeps, 2)
        # Relayed already: the next Stop waits again and wakes her for nothing until it moves.
        self.assertEqual(self.stop()[:2], (0, ""))
        self.round(build, SHAS[0], "PASS")
        self.assertEqual(self.stop()[1], self.woke(self.sent(self.go, build["id"], "pass")))

    def test_wake_wording_or_time_alone_never_wakes_her(self):
        build = self.build()
        self.handoff(build)
        self.baseline()
        clock = Clock({1: lambda: self.event(self.go, "go.confirmed", "said again", verdict="routine"),
                       2: lambda: self.tick()})
        self.assertEqual(self.stop(clock=clock)[:2], (0, ""))

    def test_wake_a_new_go_task_is_a_change(self):
        self.baseline()
        other = self.go_task(1)
        self.assertEqual(self.stop()[1], self.woke(self.sent(other, None, "confirmed")))

    def test_wake_a_closed_go_task_gets_its_closed_line_and_drops_out(self):
        build = self.build()
        self.baseline()
        pensieve.close_task(self.conn, self.go, "abandoned", now=self.tick())
        code, said, _ = self.stop()
        self.assertEqual(code, stop.WAKE)
        self.assertEqual(said, self.woke(f"{self.go} / {build['id']}: closed (abandoned). Nothing for you."
                                         " The watch ends for it."))
        self.assertEqual(self.told(), {})
        code, said, clock = self.stop()  # nothing left to watch
        self.assertEqual((code, said, clock.sleeps), (0, "", 0))

    def test_wake_many_changes_are_capped_with_a_count(self):
        self.baseline()
        others = [self.go_task(index) for index in range(1, config.GO_WATCH_MAX_PER_PASS + 3)]
        said = self.stop()[1].splitlines()
        self.assertEqual(said[0], stop.HEAD)
        self.assertEqual(said[1:-1], [self.sent(go_id, None, "confirmed")
                                      for go_id in others[:config.GO_WATCH_MAX_PER_PASS]])
        self.assertEqual(said[-1], "2 more go tasks moved; your go status block shows each one.")
        self.assertEqual(len(self.told()), len(others) + 1)

    def test_wake_one_go_task_it_cannot_read_never_holds_back_another(self):
        build = self.build()
        other = self.build(self.go_task(1))
        self.handoff(other)
        self.baseline()
        evidence = self.office / "reviews" / other["id"]
        evidence.chmod(0)
        self.addCleanup(evidence.chmod, 0o700)
        self.handoff(build)
        self.assertEqual(self.stop()[1], self.woke(self.sent(self.go, build["id"], "handoff")))


class ReadOnlyTests(ChatCase):
    def store_files(self) -> dict:
        found = {}
        for path in sorted((self.office / "state").iterdir()):
            if not path.name.endswith("-shm"):
                found[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found

    def test_read_only_the_store_is_opened_read_only_and_never_written(self):
        build = self.build()
        self.baseline()
        self.handoff(build)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.store_files()
        with mock.patch.object(db, "connect", side_effect=AssertionError("the chat watch opened the store to write")), \
                mock.patch.object(db, "connect_readonly", wraps=db.connect_readonly) as readonly:
            code, said, _ = self.stop()
        self.assertEqual(code, stop.WAKE)
        self.assertEqual(readonly.call_count, 1)
        self.assertEqual(self.store_files(), before)
        self.notified.assert_not_called()
        self.phoned.assert_not_called()
        self.spawned_reviews.assert_not_called()

    def test_read_only_a_store_it_cannot_open_exits_1_and_never_wakes(self):
        with mock.patch.object(config, "DB_PATH", str(self.office / "state" / "missing.db")):
            code, said, _ = self.stop()
        self.assertEqual(code, 1)
        self.assertTrue(said.startswith("Hogwarts Stop hook skipped:"), said)


@unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
class SettingsTests(ChatCase):
    def test_settings_the_kit_runs_this_one_script_on_stop_with_async_rewake_and_nothing_else_changes(self):
        settings = json.loads((KIT / "castle" / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["hooks"]["Stop"], [{"hooks": [{
            "type": "command", "command": COMMAND, "asyncRewake": True,
            "timeout": config.GO_CHAT_HOOK_TIMEOUT_SECONDS}]}])
        stops = [hook for event, entries in settings["hooks"].items() for entry in entries
                 for hook in entry["hooks"] if "fleet.hooks.stop" in hook["command"] or hook.get("asyncRewake")]
        self.assertEqual(len(stops), 1)
        # Her session still has no shell: no Bash allow rule, the bare Bash deny stays, the sandbox is unchanged.
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        self.assertFalse([rule for rule in allow + settings["permissions"]["ask"] if rule.startswith("Bash")])
        self.assertIn("Bash", deny)
        self.assertNotIn("excludedCommands", settings["sandbox"])
        self.assertIs(settings["sandbox"]["allowUnsandboxedCommands"], False)
        agent = (KIT / "castle" / ".claude" / "agents" / "mcgonagall.md").read_text()
        self.assertIn("\ntools: Read, Write, Edit, Glob, Grep, mcp__", agent)
        self.assertNotIn("Bash", agent.split("\n---\n", 1)[0])

    def test_settings_the_hook_runs_as_a_script_too(self):
        text = (KIT / "office" / "fleet" / "hooks" / "stop.py").read_text()
        self.assertIn("from fleet.hooks.stop import main", COMMAND)
        self.assertTrue(text.rstrip().endswith('if __name__ == "__main__":\n    sys.exit(main())'))
