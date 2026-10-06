from __future__ import annotations

import contextlib
import io
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path
from unittest import mock

from hogwarts import owlery, pensieve
from tests.support import NOW

from fleet import config, owl_post
from fleet.hooks import pre_compact, session_end, session_start, user_prompt_submit
from tests_fleet.support import (
    PROMPT_ID, SCRATCHPAD, TOKEN_SHAPE, FleetCase, assistant_entry, peer_entry, tool_result_entry, user_entry,
)

SESSION = "0b6f8c1e-1111-4222-8333-944455556666"
# Refusals where only the prompt's own transcript entry is missing, which a go confirmer takes over.
DEFERRED = ("no entrypoint", "no transcript", "prompt not written yet")


def remembered(conn) -> list:
    """Memory with distinctive text, as auto-portrait leaves it: a fact and a key point from a patch, and the routine
    ledger rows that record them. Returns the texts that must never reach a desk's context."""
    fact = pensieve.add_fact(conn, "fleet", "zebra crossings need a lollipop man", "aging", "portrait:2027-01-15:f1",
                             now=NOW)
    point = pensieve.add_keypoint(conn, "quokkas guard the backup drive", ["portrait"], now=NOW)
    pensieve.add_fact(conn, "mcgonagall", "narwhals approve every release", "pinned", "portrait:2027-01-15:f2", now=NOW)
    for op_id in ("f1", "f2", "n1"):
        pensieve.add_event(conn, "portrait", "portrait.auto-applied", "routine", f"auto-portrait applied {op_id}",
                           dedupe_key=f"portrait:applied:2027-01-15:{op_id}", now=NOW)
    return [fact["text"], point["text"], "narwhals approve every release"]


class HookCase(FleetCase):
    def hook_input(self, event: str, transcript: str = "", **fields) -> dict:
        return {"session_id": SESSION, "transcript_path": transcript, "cwd": str(self.castle),
                "hook_event_name": event, **fields}

    def started_task(self, desk: str, title: str) -> dict:
        task = pensieve.create_task(self.conn, desk, title, now=NOW)
        return pensieve.start_task(self.conn, task["id"], now=NOW)

    def headmaster(self, count: int, summary_size: int = 60) -> None:
        for index in range(count):
            pensieve.add_event(self.conn, "ron", "ci.red", "headmaster", f"red build {index} " + "x" * summary_size,
                               now=NOW + index)


class SessionStartTests(HookCase):
    def test_digest_order_is_state_then_events_then_queue_then_memory(self):
        self.started_task("harry", "fix the push gate")
        self.headmaster(2)
        pensieve.create_task(self.conn, "ron", "morning lineup", now=NOW)
        code, out, err = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        heads = [next(i for i, line in enumerate(lines) if line.startswith(prefix))
                 for prefix in ("In flight", "Headmaster events", "Queued work", "Memory pointers")]
        self.assertEqual(heads, sorted(heads))
        self.assertIn("fix the push gate | working", out)
        self.assertIn("morning lineup", out)

    def test_digest_stays_under_40_lines_and_caps_the_queue(self):
        for desk in ("harry", "hermione", "moody", "ron", "portrait"):
            self.started_task(desk, f"work for {desk}")
        for index in range(30):
            pensieve.create_task(self.conn, "ron", f"queued job {index}", now=NOW)
        self.headmaster(25)
        code, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertLess(len(lines), 40)
        queued = [line for line in lines if "queued job" in line]
        self.assertLessEqual(len(queued), config.QUEUED_CAP)

    def test_queue_cap_and_remainder_when_there_is_room(self):
        for index in range(25):
            pensieve.create_task(self.conn, "ron", f"queued job {index}", now=NOW)
        _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="clear"))
        lines = out.splitlines()
        self.assertEqual(len([line for line in lines if "queued job" in line]), 20)
        self.assertIn("- ... and 5 more queued", lines)
        self.assertLess(len(lines), 40)

    def test_awaiting_close_shows_the_close_gate(self):
        task = self.started_task("harry", "ship it")
        pensieve.mark_awaiting_close(self.conn, task["id"], now=NOW)
        _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertIn(f'{task["id"]} harry awaiting close: ship it | gate: "Mischief managed {task["id"]}"', out)

    def test_resume_prints_one_line(self):
        self.started_task("harry", "fix it")
        self.headmaster(3)
        for source in ("resume", "fork"):
            with self.subTest(source=source):
                _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source=source))
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn("1 in flight, 3 headmaster events unacked", out)

    def test_the_digest_acks_no_events(self):
        self.headmaster(2)
        self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertTrue(all(event["acked_at"] is None for event in self.events()))

    def test_the_digest_acks_the_informational_owls_it_shows(self):
        for name, kind in (("f.json", "fyi"), ("q.json", "question")):
            self.write_owl("hermione", name, {"to": "mcgonagall", "kind": kind, "subject": f"a {kind}", "body": "b"})
        self.write_owl("ron", "r.json", {"to": "mcgonagall", "kind": "request", "subject": "a request", "body": "b"})
        owl_post.run_pass(self.conn, now=NOW)
        _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(out.count("- owl "), 3)
        self.assertEqual(sorted(owl["kind"] for owl in owlery.inbox(self.conn, "mcgonagall")), ["question", "request"])
        _, again, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(again.count("- owl "), 2)

    def test_a_resume_acks_nothing(self):
        self.write_owl("hermione", "f.json", {"to": "mcgonagall", "kind": "fyi", "subject": "s", "body": "b"})
        owl_post.run_pass(self.conn, now=NOW)
        self.run_hook(session_start, self.hook_input("SessionStart", source="resume"))
        self.assertEqual(len(owlery.inbox(self.conn, "mcgonagall")), 1)

    def test_the_digest_never_prints_fact_or_key_point_text(self):
        texts = remembered(self.conn)
        self.headmaster(2)
        for argv in ([], *(["--desk", desk] for desk in config.INTERACTIVE_DESKS)):
            for source in ("startup", "clear", "compact", "resume"):
                with self.subTest(argv=argv, source=source):
                    code, out, err = self.run_hook(session_start, self.hook_input("SessionStart", source=source), argv)
                    self.assertEqual(code, 0, err)
                    self.assertTrue(out)
                    for text in texts:
                        self.assertNotIn(text, out)

    def test_bad_input_never_exits_2(self):
        for raw in (b"{not json", b"[1]", b'{"source": NaN}'):
            with self.subTest(raw=raw):
                code, out, err = self.run_hook(session_start, raw)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("SessionStart hook skipped", err)

    def test_unknown_desk_argument_is_refused(self):
        code, _, _ = self.run_hook(session_start, self.hook_input("SessionStart"), argv=["--desk", "harry"])
        self.assertEqual(code, 1)


class UserPromptSubmitTests(HookCase):
    def transcript(self, usage: int = 1000, entrypoint: str = "claude-desktop", name: str = "session.jsonl",
                   prompt: dict = None) -> str:
        """An earlier turn, then this prompt's own entry (promptId PROMPT_ID), typed by Ryan unless given."""
        current = user_entry("this prompt", entrypoint=entrypoint, promptId=PROMPT_ID) if prompt is None else prompt
        return self.write_transcript([
            user_entry("hello", entrypoint=entrypoint),
            assistant_entry("msg_1", [{"type": "text", "text": "hi"}], usage=usage, entrypoint=entrypoint),
            current,
        ], name=name)

    def prompt(self, text: str, transcript: str = None, **fields) -> tuple:
        path = self.transcript() if transcript is None else transcript
        fields = {key: value for key, value in {"prompt_id": PROMPT_ID, **fields}.items() if value is not None}
        return self.run_hook(user_prompt_submit,
                             self.hook_input("UserPromptSubmit", path, prompt=text, **fields))

    def said(self, text: str, transcript: str = None, **fields) -> tuple:
        """(shown to Ryan, given to the session) from one prompt, or ("", "") when the hook was silent."""
        code, out, err = self.prompt(text, transcript, **fields)
        self.assertEqual(code, 0, err)
        if not out:
            return "", ""
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return data["systemMessage"], data["hookSpecificOutput"]["additionalContext"]

    def waiting_task(self, desk: str, title: str) -> dict:
        task = self.started_task(desk, title)
        return pensieve.mark_awaiting_close(self.conn, task["id"], now=NOW)

    def test_events_are_capped_shown_to_ryan_and_never_acked(self):
        self.headmaster(40, summary_size=120)
        shown, context = self.said("what's up")
        lines = [line for line in shown.splitlines() if line.startswith("- [ci.red]")]
        self.assertLessEqual(sum(len(line) for line in lines), config.DRAIN_MAX_CHARS + 2 * len(lines))
        self.assertGreater(len(lines), 0)
        self.assertLess(len(lines), 40)
        self.assertIn(f"{40 - len(lines)} more waiting", shown)
        self.assertIn("castle event ack", shown)
        self.assertEqual(context, "40 headmaster events are unacked and shown to Ryan. They stay until he acks them.")
        self.assertTrue(all(event["acked_at"] is None for event in self.events()))
        _, digest, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertIn("Headmaster events, unacked (8 shown, 32 more)", digest)

    def test_the_prompt_hook_never_prints_fact_or_key_point_text(self):
        texts = remembered(self.conn)
        self.headmaster(2)
        for prompt in ("what's up", "castle fact list", texts[0]):
            with self.subTest(prompt=prompt):
                code, out, err = self.prompt(prompt)
                self.assertEqual(code, 0, err)
                self.assertTrue(out)
                data = json.loads(out)
                said = data["systemMessage"] + data["hookSpecificOutput"]["additionalContext"]
                for text in texts:
                    if text != prompt:  # a prompt Ryan typed is his own text
                        self.assertNotIn(text, said)

    def test_routine_events_are_never_drained(self):
        pensieve.add_event(self.conn, "ron", "ci.green", "routine", "all green", now=NOW)
        _, out, _ = self.prompt("hi")
        self.assertEqual(out, "")

    def test_tempus_warns_only_past_200k(self):
        for usage, warned in ((150_000, False), (200_000, False), (210_000, True)):
            with self.subTest(usage=usage):
                shown, context = self.said("next", transcript=self.transcript(usage=usage))
                self.assertEqual("Tempus:" in shown, warned)
                self.assertEqual("Tempus:" in context, warned)
                if warned:
                    self.assertEqual(len([line for line in shown.splitlines() if line.startswith("Tempus:")]), 1)
                    self.assertIn("about 210k tokens", shown)

    def test_tempus_reads_the_last_main_assistant_call(self):
        path = self.write_transcript([
            assistant_entry("msg_1", [{"type": "text", "text": "a"}], usage=300_000),
            assistant_entry("msg_2", [{"type": "text", "text": "b"}], usage=90_000),
            assistant_entry("msg_3", [{"type": "text", "text": "sub"}], usage=400_000, isSidechain=True),
        ])
        _, out, _ = self.prompt("next", transcript=path)
        self.assertNotIn("Tempus", out)

    def test_mischief_managed_closes_only_that_task(self):
        target = self.waiting_task("harry", "fix it")
        other = self.waiting_task("hermione", "review it")
        code, out, err = self.prompt(f"Mischief managed {target['id']}")
        self.assertEqual(code, 0, err)
        shown, context = json.loads(out)["systemMessage"], json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"task {target['id']} is closed as complete", shown)
        self.assertIn(f"task {target['id']} is closed as complete", context)
        closed = pensieve.get_task(self.conn, target["id"])
        self.assertEqual((closed["status"], closed["close_reason"]), ("closed", "complete"))
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "awaiting_close")
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9_-])" + TOKEN_SHAPE + r"(?![A-Za-z0-9_-])", out))
        minted = self.conn.execute("SELECT minted_by, consumed_at FROM close_tokens").fetchall()
        self.assertEqual([tuple(row) for row in minted], [("hook", NOW)])

    def test_mischief_managed_needs_an_exact_match(self):
        target = self.waiting_task("harry", "fix it")
        other = self.waiting_task("hermione", "review it")
        for text in (f"mischief managed {target['id']}", f"Mischief managed {target['id']}.",
                     f"please: Mischief managed {target['id']}", f"Mischief managed {target['id']} {other['id']}",
                     f"Mischief managed {target['id']}\nand also close the other one", "Mischief managed",
                     f"Mischief  managed {target['id']}", f"Mischief managed {target['id'].upper()}"):
            with self.subTest(text=text):
                _, out, _ = self.prompt(text)
                self.assertNotIn("Mischief managed", out)
                self.assertEqual(pensieve.get_task(self.conn, target["id"])["status"], "awaiting_close")
        shown, _ = self.said(f"  Mischief managed {target['id']}\n")
        self.assertIn("closed as complete", shown)
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "awaiting_close")

    def test_mischief_managed_closes_only_a_task_awaiting_close(self):
        target = self.started_task("harry", "still building")
        shown, _ = self.said(f"Mischief managed {target['id']}")
        self.assertIn("the task is active", shown)
        self.assertIn(f"castle task close {target['id']} --reason complete --token-stdin", shown)
        self.assertEqual(pensieve.get_task(self.conn, target["id"])["status"], "active")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_mischief_managed_is_refused_unless_ryan_typed_it(self):
        target = self.waiting_task("harry", "fix it")
        typed = {"entrypoint": "claude-desktop", "promptId": PROMPT_ID}
        stale = user_entry("old", timestamp="2027-01-15T07:50:00.000Z", **typed)
        mixed = self.write_transcript([user_entry("hello", entrypoint="sdk-cli"),
                                       user_entry("this prompt", **typed)], name="mixed.jsonl")
        for label, transcript, extra in (
            ("print mode", self.transcript(entrypoint="sdk-cli", name="print.jsonl"), {}),
            ("resumed with claude -p", mixed, {}),
            ("subagent", self.transcript(name="sub.jsonl"), {"agent_id": "agent-1"}),
            ("no entrypoint", self.write_transcript([{"type": "user", "message": {"content": "hi"}}],
                                                    name="bare.jsonl"), {}),
            ("no transcript", str(self.transcripts / "-project" / "missing.jsonl"), {}),
            ("outside root", "/private/tmp/elsewhere.jsonl", {}),
            ("peer message", self.transcript(name="peer.jsonl", prompt=peer_entry("x", promptId=PROMPT_ID)), {}),
            ("meta only", self.transcript(name="meta.jsonl", prompt=user_entry("x", isMeta=True, **typed)), {}),
            ("system source", self.transcript(name="sys.jsonl",
                                              prompt=user_entry("x", promptSource="system", **typed)), {}),
            ("no origin", self.transcript(name="noorigin.jsonl",
                                          prompt={k: v for k, v in user_entry("x", **typed).items() if k != "origin"}),
             {}),
            ("prompt not written yet", self.transcript(name="later.jsonl", prompt=assistant_entry(
                "msg_2", [{"type": "text", "text": "ok"}])), {}),
            ("no prompt id", self.transcript(name="noid.jsonl"), {"prompt_id": None}),
            ("stale entry", self.transcript(name="stale.jsonl", prompt=stale), {}),
        ):
            with self.subTest(label=label):
                # Each deferred prompt gets its own id, since one confirmer is claimed per prompt.
                fields = {"prompt_id": f"5d0c9a3e-7777-4888-9999-00000000000{DEFERRED.index(label)}", **extra} \
                    if label in DEFERRED else extra
                shown, _ = self.said(f"Mischief managed {target['id']}", transcript=transcript, **fields)
                if label in DEFERRED:  # only the entry is missing, so a confirmer takes it from here
                    self.assertIn("your typing is being confirmed", shown)
                else:
                    self.assertIn("was not applied", shown)
                    self.assertIn("castle token mint", shown)
                self.assertEqual(pensieve.get_task(self.conn, target["id"])["status"], "awaiting_close")
        self.assertEqual(self.spawned_confirms.call_count, len(DEFERRED))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_mischief_managed_names_every_task_the_close_cascaded_to(self):
        parent = self.waiting_task("mcgonagall", "the ask")
        built = pensieve.create_task(self.conn, "harry", "build", parent_task_id=parent["id"], now=NOW)
        pensieve.start_task(self.conn, built["id"], now=NOW)
        later = pensieve.create_task(self.conn, "hermione", "review", parent_task_id=built["id"], now=NOW)
        shown, _ = self.said(f"Mischief managed {parent['id']}")
        self.assertIn(f"- cascaded: {built['id']} (harry) closed as complete", shown)
        self.assertIn(f"- cascaded: {later['id']} (hermione) closed as superseded", shown)

    def test_mischief_managed_reports_a_store_refusal(self):
        shown, _ = self.said("Mischief managed tk_0000000000000000")
        self.assertIn("Mischief managed failed for tk_0000000000000000", shown)

    def test_a_store_failure_never_blocks_the_prompt(self):
        os.unlink(self.db_path)
        code, _, err = self.prompt("hello")
        self.assertEqual(code, 1)
        self.assertIn("UserPromptSubmit hook skipped", err)


class PreCompactTests(HookCase):
    def test_a_full_scratchpad_gets_no_stub(self):
        pad = self.castle / "desks" / "mcgonagall" / "scratchpad.md"
        self.write_file(pad, SCRATCHPAD + "x" * (config.SCRATCHPAD_BUDGET_BYTES - len(SCRATCHPAD) - 50))
        before = pad.read_text()
        code, out, err = self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="auto"))
        self.assertEqual(code, 0, err)
        self.assertIn("6KB budget", out)
        self.assertEqual(pad.read_text(), before)

    def test_appends_a_checkpoint_stub_with_the_active_task(self):
        task = self.started_task("mcgonagall", "route the review")
        code, _, err = self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="auto"))
        self.assertEqual(code, 0, err)
        text = (self.castle / "desks" / "mcgonagall" / "scratchpad.md").read_text()
        self.assertTrue(text.startswith("# Scratchpad\n"))
        self.assertIn("### Checkpoint 2027-01-15 08:00 UTC (pre-compact, auto)", text)
        self.assertIn(f"- Task: {task['id']} active, route the review", text)
        self.assertIn(f"- Session: {SESSION}", text)

    def test_with_no_active_task(self):
        self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="manual"))
        text = (self.castle / "desks" / "mcgonagall" / "scratchpad.md").read_text()
        self.assertIn("- Task: none active", text)

    def test_a_symlinked_or_hard_linked_scratchpad_is_refused(self):
        pad = self.castle / "desks" / "mcgonagall" / "scratchpad.md"
        target = self.write_file(self.tmp / "settings.json", "{}")
        for make in (os.symlink, os.link):
            with self.subTest(link=make.__name__):
                os.unlink(pad)
                make(target, pad)
                code, _, err = self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="auto"))
                self.assertEqual(code, 1)
                self.assertIn("scratchpad", err)
                self.assertEqual(target.read_text(), "{}")


class SessionEndTests(HookCase):
    def session_transcript(self, extra: list = ()) -> str:
        return self.write_transcript([
            {"type": "queue-operation", "operation": "enqueue"},
            user_entry("Please review the diff. Mail me at ryan@example.com"),
            assistant_entry("msg_1", [{"type": "text", "text": "Let me check the diff first."},
                                      {"type": "tool_use", "id": "tool_1", "name": "Bash",
                                       "input": {"command": "git diff TOOLINPUT-MARKER"}}], usage=50_000),
            tool_result_entry("tool_1", "TOOLOUTPUT-MARKER api_key=sk-ant-abcdefghijklmnopqrstuvwxyz0123"),
            assistant_entry("msg_2", [{"type": "thinking", "thinking": "THINKING-MARKER"}], usage=60_000),
            assistant_entry("msg_2", [{"type": "text", "text": "The diff looks right. token=abc123def456"}],
                            usage=60_000),
            user_entry("<local-command-stdout>LOCALCMD-MARKER</local-command-stdout>"),
            user_entry("META-MARKER", isMeta=True),
            user_entry("SIDECHAIN-MARKER", isSidechain=True),
            user_entry([{"type": "text", "text": "<system-reminder>REMINDER-MARKER</system-reminder>"},
                        {"type": "text", "text": "Ship it after review."}]),
            user_entry("NOTIFICATION-MARKER", origin={"kind": "task-notification"}),
            assistant_entry("msg_3", [{"type": "text", "text": "Waiting for your go."}], usage=70_000),
            *extra,
        ])

    def stored(self) -> list:
        rows = self.conn.execute("SELECT seq, role, text FROM extracts WHERE session_id = ? ORDER BY seq",
                                 (SESSION,)).fetchall()
        return [tuple(row) for row in rows]

    def test_keeps_prompts_and_final_replies_only(self):
        code, _, err = self.run_hook(session_end, self.hook_input("SessionEnd", self.session_transcript(),
                                                                 reason="other", agent_type="mcgonagall"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), [
            (1, "user", "Please review the diff. Mail me at [email]"),
            (2, "assistant", "The diff looks right. token=[secret]"),
            (3, "user", "Ship it after review."),
            (4, "assistant", "Waiting for your go."),
        ])
        text = " ".join(row[2] for row in self.stored())
        for marker in ("TOOLOUTPUT", "TOOLINPUT", "THINKING", "LOCALCMD", "META", "SIDECHAIN", "REMINDER",
                       "NOTIFICATION", "Let me check", "sk-ant", "ryan@example.com"):
            self.assertNotIn(marker, text)
        session = pensieve.get_session(self.conn, SESSION)
        self.assertEqual((session["desk"], session["project"], session["model"]),
                         ("mcgonagall", str(self.castle), "claude-opus-5-5"))
        self.assertEqual((session["first_turn_tokens"], session["total_input_tokens"]), (50_000, 180_000))

    def test_a_session_without_mcgonagalls_agent_is_ryans_own(self):
        for label, extra in (("no agent", {}), ("other agent", {"agent_type": "general-purpose"}),
                             ("subagent", {"agent_type": "mcgonagall", "agent_id": "agent-1"})):
            with self.subTest(label=label):
                session = f"session-{label.replace(' ', '-')}"
                fields = {**self.hook_input("SessionEnd", self.session_transcript()), "session_id": session, **extra}
                code, _, err = self.run_hook(session_end, fields)
                self.assertEqual(code, 0, err)
                self.assertEqual(pensieve.get_session(self.conn, session)["desk"], "ryan-claude-1")

    def test_entries_and_the_session_are_capped(self):
        long = "word " * 2000
        entries = []
        for index in range(6):
            entries.append(user_entry(f"{index} {long}"))
            entries.append(assistant_entry(f"msg_{index}", [{"type": "text", "text": long}]))
        self.run_hook(session_end, self.hook_input("SessionEnd", self.write_transcript(entries)))
        stored = self.stored()
        self.assertTrue(all(len(row[2]) <= config.EXTRACT_CAP for row in stored))
        self.assertLessEqual(sum(len(row[2]) for row in stored), config.SESSION_CAP)
        self.assertEqual(len(stored), 4)

    def test_scrubbing_holds_after_the_cut(self):
        text = "z" * 3992 + " 1.2.3.4.5"
        self.run_hook(session_end, self.hook_input("SessionEnd", self.write_transcript([user_entry(text)])))
        [(_, _, stored)] = self.stored()
        self.assertTrue(stored.endswith(" [ipv4]"))
        self.assertEqual(stored, pensieve.scrub(stored))
        self.assertLessEqual(len(stored), config.EXTRACT_CAP)

    def test_a_second_end_adds_only_new_entries(self):
        path = self.session_transcript()
        self.run_hook(session_end, self.hook_input("SessionEnd", path))
        first = self.stored()
        path = self.session_transcript([user_entry("One more thing."),
                                        assistant_entry("msg_9", [{"type": "text", "text": "Done."}])])
        self.run_hook(session_end, self.hook_input("SessionEnd", path))
        self.assertEqual(self.stored(), first + [(5, "user", "One more thing."), (6, "assistant", "Done.")])

    def test_a_transcript_outside_the_root_is_refused(self):
        outside = self.write_file(self.tmp / "other.jsonl", "")
        code, _, err = self.run_hook(session_end, self.hook_input("SessionEnd", str(outside)))
        self.assertEqual(code, 1)
        self.assertIn("outside the transcripts folder", err)
        self.assertEqual(self.stored(), [])

    def test_a_symlinked_transcript_is_refused(self):
        real = self.write_transcript([user_entry("hello")], name="real.jsonl")
        link = self.transcripts / "-project" / "link.jsonl"
        os.symlink(real, link)
        code, _, _ = self.run_hook(session_end, self.hook_input("SessionEnd", str(link)))
        self.assertEqual(code, 1)


class RoundTripTests(HookCase):
    def test_an_owl_round_trip_reaches_the_digest(self):
        self.write_owl("hermione", "a.json", {"to": "mcgonagall", "kind": "fyi", "subject": "review done",
                                              "body": "VERDICT: PASS"})
        [delivered] = owl_post.run_pass(self.conn, now=NOW)["delivered"]
        _, out, _ = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertIn(f"- owl {delivered['owl_id']} from hermione (fyi): review done", out)
        self.assertNotIn("VERDICT", out)
        self.assertEqual(owlery.inbox(self.conn, "mcgonagall"), [])


class ScriptPathTests(HookCase):
    """Each hook module also runs as a script path under the wrapper line."""

    def run_script(self, name: str, payload: dict) -> tuple:
        path = Path(__file__).resolve().parents[1] / "fleet" / "hooks" / f"{name}.py"
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")))
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [str(path)]), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "path", list(sys.path)), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(path), run_name="__main__")
        return exited.exception.code, out.getvalue()

    def test_session_start_runs_as_a_script(self):
        code, out = self.run_script("session_start", self.hook_input("SessionStart", source="resume"))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Hogwarts: mcgonagall session resumed."))

    def test_the_import_form_never_blocks_a_prompt_when_the_fleet_is_missing(self):
        empty = self.tmp / "no-fleet"
        empty.mkdir(mode=0o700)
        boot = (f"import sys; sys.path.insert(0, {json.dumps(str(empty))}); "
                "from fleet.hooks.user_prompt_submit import main; sys.exit(main())")
        done = subprocess.run(["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty",
                               "-c", boot], input=b"{}", capture_output=True, check=False)
        self.assertEqual(done.returncode, 1)

    def test_every_hook_has_a_script_entry_point(self):
        folder = Path(__file__).resolve().parents[1] / "fleet" / "hooks"
        for name in ("session_start", "user_prompt_submit", "pre_compact", "session_end"):
            with self.subTest(hook=name):
                text = (folder / f"{name}.py").read_text()
                self.assertIn('if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:', text)
                self.assertTrue(text.rstrip().endswith('if __name__ == "__main__":\n    sys.exit(main())'))
