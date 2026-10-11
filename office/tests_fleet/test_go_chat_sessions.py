"""Several McGonagall sessions at once (fleet/go_owners.py): a go task belongs to the session that typed its go, and
each session's chat watch (fleet/hooks/stop.py), go status and go-changed blocks cover only the go tasks it watches.
A session that ended hands its go tasks to the next one that starts, and "watch <task-id>" moves one."""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
from unittest import mock

from hogwarts import pensieve

from fleet import config, go_owners, go_status, safefs
from fleet.hooks import session_start, stop, user_prompt_submit
from tests_fleet.test_go_chat import ChatCase
from tests_fleet.test_go_wait import Clock

SESSIONS = tuple(f"0b6f8c1e-1111-4222-8333-94445555{index:04d}" for index in range(1, 7))


def dead_pid() -> int:
    child = subprocess.Popen(["/usr/bin/true"])
    child.wait()
    return child.pid


class SessionsCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        pensieve.close_task(self.conn, self.go, "abandoned", now=self.tick())  # no go typed it: no session's
        self.said: list = []
        self.pids: dict = {}

    def alive(self, session: str) -> int:
        """A running process standing in for each session's Claude Code process."""
        if session not in self.pids:
            child = subprocess.Popen(["/bin/sleep", "300"])
            self.addCleanup(child.wait)
            self.addCleanup(child.kill)
            self.pids[session] = child.pid
        return self.pids[session]

    def prompt(self, session: str, text: str = "how is it going", pid: int = None, **fields) -> str:
        payload = {**self.payload(session_id=session), "hook_event_name": "UserPromptSubmit", "prompt": text,
                   "prompt_id": "5d0c9a3e-7777-4888-9999-0aaabbbcccdd", **fields}
        with mock.patch.object(go_owners, "session_pid", return_value=pid or self.alive(session)):
            code, out, err = self.run_hook(user_prompt_submit, payload)
        self.assertEqual(code, 0, err)
        self.said += [out, err]
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def start(self, session: str, source: str = "startup", pid: int = None) -> str:
        payload = {**self.payload(session_id=session), "hook_event_name": "SessionStart", "source": source}
        with mock.patch.object(go_owners, "session_pid", return_value=pid or self.alive(session)):
            code, out, err = self.run_hook(session_start, payload)
        self.assertEqual(code, 0, err)
        self.said += [out, err]
        return out

    def stop_in(self, session: str, clock: Clock = None) -> tuple:
        code, said, clock = self.stop(clock=clock, payload=self.payload(session_id=session))
        self.said.append(said)
        return code, said, clock.sleeps

    def typed_go(self, session: str, *indexes: int, pid: int = None) -> list:
        """Gos typed in a session's prompt, then applied as the go confirmer would."""
        go_ids = [f"tk_{index + 0xa0:016x}" for index in indexes]
        self.prompt(session, "\n".join(f"go {go_id}" for go_id in go_ids), pid=pid)
        return [self.go_task(index) for index in indexes]

    def owner(self, go_id: str) -> str:
        with open(self.folder() / go_owners.OWNERS) as handle:
            return json.load(handle)["tasks"][go_id][0]


class OwnTests(SessionsCase):
    def test_own_three_sessions_each_typing_a_go_hear_only_their_own(self):
        gos = {session: self.typed_go(session, index)[0] for index, session in enumerate(SESSIONS[:3], 1)}
        for session in SESSIONS[:3]:
            self.assertEqual(self.owner(gos[session]), go_owners.name(session))
            code, said, _ = self.stop_in(session)
            self.assertEqual((code, said), (stop.WAKE, self.woke(self.sent(gos[session], None, "confirmed"))))
        build = self.build(gos[SESSIONS[0]])
        self.handoff(build)
        self.assertEqual(self.stop_in(SESSIONS[0])[1], self.woke(self.sent(gos[SESSIONS[0]], build["id"], "handoff")))
        for session in SESSIONS[1:3]:
            self.assertEqual(self.stop_in(session), (0, "", 4))  # waited its whole wait, woken by nothing
        # Each one's go status covers its own go task, with a count of the others.
        context = self.prompt(SESSIONS[1])
        self.assertIn(gos[SESSIONS[1]], context)
        for other in (SESSIONS[0], SESSIONS[2]):
            self.assertNotIn(gos[other], context)
        self.assertIn(go_status.others_line(2), context)

    def test_own_a_session_with_no_go_gets_no_wake_and_only_a_count(self):
        gos = [self.typed_go(session, index)[0] for index, session in enumerate(SESSIONS[:2], 1)]
        self.assertEqual(self.stop_in(SESSIONS[3]), (0, "", 0))  # nothing it watches: it never waits
        context = self.prompt(SESSIONS[3])
        self.assertIn(go_status.others_line(2), context)
        self.assertFalse([go_id for go_id in gos if go_id in context])
        self.handoff(self.build(gos[0]))
        self.assertEqual(self.stop_in(SESSIONS[3]), (0, "", 0))
        self.assertEqual(self.prompt(SESSIONS[3]), "")  # its block changes with nothing of another session's

    def test_own_several_gos_in_one_message_stay_that_sessions(self):
        first, second = self.typed_go(SESSIONS[0], 1, 2)
        self.assertEqual({self.owner(first), self.owner(second)}, {go_owners.name(SESSIONS[0])})
        said = self.stop_in(SESSIONS[0])[1]
        self.assertEqual(said, self.woke(self.sent(first, None, "confirmed"), self.sent(second, None, "confirmed")))
        self.assertEqual(self.stop_in(SESSIONS[1]), (0, "", 0))

    def test_own_a_go_retyped_in_another_session_keeps_its_owner(self):
        go_id = self.typed_go(SESSIONS[0], 1)[0]
        self.prompt(SESSIONS[1], f"go {go_id}")  # refused: the store knows it already
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[0]))

    def test_own_one_session_is_unchanged(self):
        go_id = self.typed_go(SESSIONS[0], 1)[0]
        other = self.go_task(2)  # no session owns it: watched as before
        self.assertEqual(self.stop_in(SESSIONS[0])[1], self.woke(self.sent(go_id, None, "confirmed"),
                                                                   self.sent(other, None, "confirmed")))
        self.assertEqual(self.stop_in(SESSIONS[0]), (0, "", 4))
        context = self.prompt(SESSIONS[0])
        self.assertNotIn("Other sessions", context)
        self.assertEqual(self.owner(other), go_owners.name(SESSIONS[0]))  # her prompt took it, silently
        out = self.start(SESSIONS[0], source="resume")  # the same session in a new process
        self.assertNotIn("Chat watch:", out)


class HandoverTests(SessionsCase):
    def test_handover_a_dead_sessions_gos_move_to_the_next_session_once(self):
        gone = self.typed_go(SESSIONS[0], 1, pid=dead_pid())[0]
        kept = self.typed_go(SESSIONS[1], 2)[0]
        out = self.start(SESSIONS[2])
        self.assertIn("\n" + session_start.taken_line([gone])[0] + "\n", out)
        self.assertNotIn(f"- {kept} ->", out)  # its go status stays in its own session
        self.assertEqual(self.owner(gone), go_owners.name(SESSIONS[2]))
        self.assertEqual(self.owner(kept), go_owners.name(SESSIONS[1]))
        self.assertEqual(self.stop_in(SESSIONS[2]), (0, "", 4))  # its digest showed it, so that is told
        build = self.build(gone)
        self.assertEqual(self.stop_in(SESSIONS[2])[1], self.woke(self.sent(gone, build["id"], "confirmed")))
        self.assertEqual(self.stop_in(SESSIONS[1])[1], self.woke(self.sent(kept, None, "confirmed")))  # its own only
        self.assertNotIn("Chat watch:", self.start(SESSIONS[3]))  # said once: the next session takes nothing

    def test_handover_a_clear_in_the_same_process_keeps_its_go_tasks(self):
        go_id = self.typed_go(SESSIONS[0], 1, pid=4242)[0]
        out = self.start(SESSIONS[1], source="clear", pid=4242)
        self.assertIn(session_start.taken_line([go_id])[0], out)
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[1]))

    def test_handover_watch_moves_one_open_go_task(self):
        moved, stays = self.typed_go(SESSIONS[0], 1, 2)
        self.assertEqual(self.stop_in(SESSIONS[0])[0], stop.WAKE)
        self.assertIn(moved, self.prompt(SESSIONS[0]))
        context = self.prompt(SESSIONS[1], f"  watch {moved}  ")
        self.assertIn(user_prompt_submit.WATCH_SAID["moved"].format(id=moved), context)
        self.assertIn(moved, context.split("Watch moved")[1])  # its go status shows it here now
        self.assertEqual((self.owner(moved), self.owner(stays)),
                         (go_owners.name(SESSIONS[1]), go_owners.name(SESSIONS[0])))
        self.assertIn(user_prompt_submit.WATCH_SAID["already"].format(id=moved),
                      self.prompt(SESSIONS[1], f"watch {moved}"))
        build = self.build(moved)
        self.assertEqual(self.stop_in(SESSIONS[1])[1], self.woke(self.sent(moved, build["id"], "confirmed")))
        self.assertEqual(self.stop_in(SESSIONS[0]), (0, "", 4))
        self.assertIn(f"- {moved}: {go_status.MOVED}", self.prompt(SESSIONS[0]))

    def test_handover_watch_takes_only_an_open_go_task_in_her_session(self):
        go_id = self.typed_go(SESSIONS[0], 1)[0]
        for text in (f"watch {self.go}", "watch tk_" + "f" * 16):
            context = self.prompt(SESSIONS[1], text)
            self.assertIn(user_prompt_submit.WATCH_SAID["not open"].format(id=text.split()[1]), context)
        context = self.prompt(SESSIONS[1], f"watch {go_id}", agent_type=None)
        self.assertIn(user_prompt_submit.WATCH_SESSION, context)
        self.assertEqual(self.prompt(SESSIONS[1], f"please watch {go_id}"), "")  # prose is not the phrase
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[0]))

    def test_handover_owners_that_cannot_be_read_leave_every_go_task_watched(self):
        go_id = self.typed_go(SESSIONS[0], 1)[0]
        (self.folder() / go_owners.OWNERS).write_text("not json\n")
        self.assertEqual(self.stop_in(SESSIONS[1])[1], self.woke(self.sent(go_id, None, "confirmed")))


class PrivacyTests(SessionsCase):
    def test_privacy_session_ids_never_appear_where_a_model_reads(self):
        gone = self.typed_go(SESSIONS[0], 1, pid=dead_pid())[0]
        mine = self.typed_go(SESSIONS[1], 2)[0]
        self.stop_in(SESSIONS[1])
        self.start(SESSIONS[2])
        self.prompt(SESSIONS[2], f"watch {mine}")
        self.handoff(self.build(gone))
        self.stop_in(SESSIONS[2])
        self.prompt(SESSIONS[1])
        said = "\n".join(self.said)
        self.assertIn(gone, said)
        for session in SESSIONS[:3]:
            self.assertNotIn(session, said)
            self.assertNotIn(go_owners.name(session), said)
        for path in self.folder().iterdir():
            for session in SESSIONS:
                self.assertNotIn(session, path.name)
                self.assertNotIn(session, path.read_text(errors="replace"))


class RaceTests(SessionsCase):
    def test_race_a_go_task_moved_then_closed_drops_out_of_the_old_session_quietly(self):
        go_id = self.typed_go(SESSIONS[0], 1)[0]
        self.stop_in(SESSIONS[0])
        self.assertIn(go_id, self.prompt(SESSIONS[0]))
        self.prompt(SESSIONS[1], f"watch {go_id}")
        pensieve.close_task(self.conn, go_id, "abandoned", now=self.tick())
        self.assertEqual(self.stop_in(SESSIONS[0]), (0, "", 0))  # no closed line here: it was moved
        self.assertIn(f"{go_id} / no build: closed (abandoned).", self.stop_in(SESSIONS[1])[1])
        self.assertIn(f"- {go_id}: {go_status.MOVED}", self.prompt(SESSIONS[0]))

    def test_race_the_session_whose_go_applied_owns_it(self):
        go_id = f"tk_{1 + 0xa0:016x}"
        go_owners.claim(self.conn, SESSIONS[0], [go_id])
        go_owners.claim(self.conn, SESSIONS[1], [go_id])  # typed again elsewhere while the first is confirmed
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[1]))
        self.go_task(1)
        go_owners.applied(self.conn, SESSIONS[0], go_id)  # the first go committed
        go_owners.claim(self.conn, SESSIONS[1], [go_id])  # too late: the store knows it, so it keeps its owner
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[0]))

    def test_race_a_start_whose_digest_never_went_out_takes_nothing(self):
        go_id = self.typed_go(SESSIONS[0], 1, pid=dead_pid())[0]
        with mock.patch.object(go_owners, "session_pid", return_value=self.alive(SESSIONS[1])):
            self.assertEqual(go_owners.start(self.conn, SESSIONS[1]), [go_id])
        self.assertEqual(self.owner(go_id), go_owners.name(SESSIONS[0]))
        self.assertIn(session_start.taken_line([go_id])[0], self.start(SESSIONS[2]))

    def test_race_a_live_sessions_marker_outlives_the_cleanup(self):
        self.typed_go(SESSIONS[0], 1)
        self.typed_go(SESSIONS[1], 2, pid=dead_pid())
        old = time.time() - config.EVENTS_SEEN_KEEP_SECONDS - 60
        for session in SESSIONS[:2]:
            os.utime(self.folder() / (go_owners.name(session) + go_owners.SESSION), (old, old))
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
            stop._prune(fd, go_owners.name(SESSIONS[2]))
        names = {path.name for path in self.folder().iterdir()}
        self.assertIn(go_owners.name(SESSIONS[0]) + go_owners.SESSION, names)
        self.assertNotIn(go_owners.name(SESSIONS[1]) + go_owners.SESSION, names)
        self.assertIn(go_owners.OWNERS_LOCK, names)

    def test_race_owners_are_bounded(self):
        go_owners.claim(self.conn, SESSIONS[0], [f"tk_{index:016x}" for index in range(go_owners.MAX_OWNERS + 6)])
        with open(self.folder() / go_owners.OWNERS) as handle:
            self.assertEqual(len(json.load(handle)["tasks"]), go_owners.MAX_OWNERS)
