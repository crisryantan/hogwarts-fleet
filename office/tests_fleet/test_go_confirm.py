"""The deferred go and close: Claude Code writes a prompt's transcript entry only after the hook returns, so the hook
hands a go or a Mischief managed whose entry is not there yet to one detached confirmer (fleet/go_confirm.py).

The hook's start of the confirmer is recorded (support.FleetCase patches run_desk.spawn_go_confirm), and each test runs
go_confirm.confirm on the exact bytes the hook piped, with a fake clock whose sleep can write the entry meanwhile. Also
here: several gos in one prompt, and which prompts that mention a go get a refusal and which get nothing.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from unittest import mock

from hogwarts import pensieve
from tests.support import NOW

from fleet import common, config, go_confirm, run_desk, worktree
from fleet.hooks import user_prompt_submit
from tests_fleet.support import PROMPT_ID, TOKEN_SHAPE, assistant_entry, peer_entry, user_entry
from tests_fleet.test_go import (
    OTHER_ID, TASK_ID, GoCase, committed_then_unreadable, then_terminated, unreadable_tasks,
)

REAL_SPAWN = run_desk.spawn_go_confirm  # before FleetCase patches it
THIRD_ID = "tk_00000000000000aa"


class Clock:
    """time.time and time.sleep for one confirmer run: sleeping moves the clock on, and on_sleep(n) runs after the
    n-th sleep, so a test can write the transcript entry while the confirmer waits."""

    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = float(NOW), 0, on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


class ConfirmCase(GoCase):
    def later(self, name: str = "later.jsonl") -> str:
        """A transcript as it is at hook time: the prompt's own entry is not written yet."""
        return self.transcript("", name=name, prompt=assistant_entry("msg_2", [{"type": "text", "text": "ok"}]))

    def append(self, path: str, entry=None, raw: str = None) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps(entry) + "\n")

    def deferred(self, text: str, transcript: str = None, **fields) -> tuple:
        """(shown, context, payload) for a prompt the hook hands to a confirmer."""
        path = self.later() if transcript is None else transcript
        shown, context, out = self.said(text, transcript=path, **fields)
        self.assertIn("your typing is being confirmed", shown)
        self.assertNotIn(PROMPT_ID, out)
        payload = self.spawned_confirms.call_args[0][0]
        return shown, context, payload

    def confirm(self, payload: bytes, on_sleep=None) -> list:
        clock = Clock(on_sleep)
        with mock.patch.object(run_desk, "spawn") as self.harry_spawn:
            return go_confirm.confirm(payload, clock=clock, sleep=clock.sleep)

    def appears(self, path: str, entry: dict, after: int = 3):
        """on_sleep that writes entry after the given number of sleeps."""
        def on_sleep(count: int) -> None:
            if count == after:
                self.append(path, entry)
        return on_sleep

    def headmaster_events(self) -> list:
        rows = self.conn.execute("SELECT kind, desk, task_id, summary, dedupe_key FROM events"
                                 " WHERE verdict = 'headmaster' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def assert_nothing_secret(self, text: str) -> None:
        self.assertNotIn(PROMPT_ID, text)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", text))
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9_-])" + TOKEN_SHAPE + r"(?![A-Za-z0-9_-])", text))


class DeferredGoTests(ConfirmCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        self.enable("harry")

    def test_an_entry_written_after_the_hook_runs_the_go_once(self):
        path = self.later()
        before = self.snapshot()
        shown, context, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        self.assertIn("The result arrives as a headmaster event within about half a minute.", shown)
        self.assertIn("Your go status block shows where it stands once it applies.", context)
        self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
        self.assert_unchanged(before)  # the hook itself changed nothing
        self.assertNotIn(PROMPT_ID, " ".join(map(str, self.spawned_confirms.call_args[0][1:])))
        lines = self.confirm(payload, self.appears(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID)))
        built = self.harry_task()
        self.assertEqual(built["status"], "active")
        self.harry_spawn.assert_called_once()
        [event] = self.headmaster_events()
        self.assertEqual((event["kind"], event["desk"], event["task_id"]), ("go.confirmed", "mcgonagall", TASK_ID))
        self.assertIn(f"Go: {TASK_ID} is registered, and Harry's task {built['id']}", event["summary"])
        self.assertEqual(lines, [event["summary"]])
        for text in (event["summary"], event["dedupe_key"], " ".join(lines)):
            self.assert_nothing_secret(text)
        # The same input again, a retried start, runs nothing.
        again = self.snapshot()
        self.assertEqual(self.confirm(payload), ["refused: this prompt was confirmed already"])
        self.assert_unchanged(again)
        # The outcome shows on McGonagall's next prompt through the drain.
        shown, _, _ = self.said("thanks")
        self.assertIn(f"[go.confirmed] #{1} mcgonagall {TASK_ID}: Go: {TASK_ID} is registered", shown)

    def test_an_entry_that_never_appears_starts_nothing_and_says_how_to_start_it_by_hand(self):
        before = self.snapshot()
        _, _, payload = self.deferred(f"go {TASK_ID}")
        clock_sleeps = []
        lines = self.confirm(payload, clock_sleeps.append)
        self.assertEqual(len(clock_sleeps), int(config.GO_CONFIRM_WAIT_SECONDS / config.GO_CONFIRM_POLL_SECONDS))
        self.assert_unchanged(before)
        [event] = self.headmaster_events()
        self.assertEqual((event["kind"], event["task_id"]), ("go.refused", None))
        self.assertIn(f"Go was not applied to {TASK_ID}: this hook could not confirm Ryan's own typing (this prompt's"
                      f" transcript entry did not appear within {config.GO_CONFIRM_WAIT_SECONDS} seconds).",
                      event["summary"])
        self.assertIn("Fix: type it again as a new message of its own", event["summary"])
        self.assertIn(f"castle task create --id {TASK_ID} --desk mcgonagall", event["summary"])
        self.assertEqual(lines, [event["summary"]])

    def test_a_long_reason_is_cut_but_the_fix_line_survives_the_summary_cap(self):
        fix = "Fix: edit the base: line, then type the go again."
        summary = go_confirm.fitted([f"Go was not applied to {TASK_ID}: " + "x" * 700, fix])
        self.assertLessEqual(len(summary), pensieve.SUMMARY_LIMIT)
        self.assertTrue(summary.endswith(fix))
        self.assertIn("... Fix:", summary)
        steps = "Start it by hand. " * 30
        with_steps = go_confirm.fitted([f"Go was not applied to {TASK_ID}: " + "x" * 700, fix, steps])
        self.assertLessEqual(len(with_steps), pensieve.SUMMARY_LIMIT)
        self.assertIn(fix, with_steps)
        short = go_confirm.fitted(["Go was not applied: short", fix])
        self.assertEqual(short, f"Go was not applied: short {fix}")

    def test_a_missing_file_or_a_cut_last_line_is_not_yet_never_a_pass(self):
        self.later()  # the project folder exists; this session's file does not yet
        path = str(self.transcripts / "-project" / "new-session.jsonl")
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        whole = json.dumps(user_entry(f"go {TASK_ID}", promptId=PROMPT_ID))
        seen = []

        def on_sleep(count: int) -> None:
            if count == 2:  # the file appears with the entry cut part way
                self.write_file(path, whole[:40])
            elif count == 4:
                seen.append(user_prompt_submit.typing_entry(json.loads(payload), NOW)[0])
                self.append(path, raw=whole[40:] + "\n")

        self.confirm(payload, on_sleep)
        self.assertEqual(seen, [user_prompt_submit.NOT_YET])
        self.assertEqual(self.harry_task()["status"], "active")

    def test_an_entry_that_is_not_typed_refuses(self):
        typed = {"promptId": PROMPT_ID}
        text = f"go {TASK_ID}"
        cases = (
            ("peer message", peer_entry(text, **typed), "this prompt did not come from Ryan's keyboard"),
            ("meta", user_entry(text, isMeta=True, **typed), "this prompt did not come from Ryan's keyboard"),
            ("system source", user_entry(text, promptSource="system", **typed),
             "this prompt did not come from Ryan's keyboard"),
            ("print mode", user_entry(text, entrypoint="sdk-cli", **typed), "the session is not one Ryan is typing"),
            ("stale", user_entry(text, timestamp="2027-01-15T07:50:00.000Z", **typed), "is not current"),
        )
        before = self.snapshot()
        for index, (label, entry, reason) in enumerate(cases):
            with self.subTest(label=label):
                prompt_id = f"5d0c9a3e-7777-4888-9999-00000000000{index}"
                entry = {**entry, "promptId": prompt_id}
                path = self.later(name=f"case-{index}.jsonl")
                _, _, payload = self.deferred(text, transcript=path, prompt_id=prompt_id)
                self.confirm(payload, self.appears(path, entry, after=1))
                self.assert_unchanged(before)
                event = self.headmaster_events()[-1]
                self.assertEqual(event["kind"], "go.refused")
                self.assertIn(reason, event["summary"])

    def test_transcript_text_that_differs_from_what_the_hook_saw_refuses(self):
        self.task_md(OTHER_ID)
        path = self.later()
        before = self.snapshot()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        self.confirm(payload, self.appears(path, user_entry(f"go {OTHER_ID}", promptId=PROMPT_ID)))
        self.assert_unchanged(before)
        [event] = self.headmaster_events()
        self.assertIn(go_confirm.DIFFERENT, event["summary"])

    def test_two_hooks_for_one_prompt_start_one_confirmer_and_a_forged_input_runs_nothing(self):
        path = self.later()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        shown, _, _ = self.said(f"go {TASK_ID}", transcript=path)
        self.assertIn(f"Go for {TASK_ID} is already being confirmed for this prompt", shown)
        self.assertEqual(self.spawned_confirms.call_count, 1)
        # Even once the entry is there, a retried hook leaves the go to the confirmer.
        self.append(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID))
        shown, _, _ = self.said(f"go {TASK_ID}", transcript=path)
        self.assertIn("already being confirmed", shown)
        before = self.snapshot()
        forged = payload.replace(b'"agent_type": "mcgonagall"', b'"agent_type": "mcgonagall", "x": 1')
        self.assertEqual(self.confirm(forged), ["refused: the input is not the one the hook claimed"])
        other = json.loads(payload)
        other["prompt_id"] = "5d0c9a3e-7777-4888-9999-0aaabbbccc00"
        self.assertEqual(self.confirm(json.dumps(other).encode()), ["refused: no claim was made for this input"])
        self.assert_unchanged(before)
        self.confirm(payload)
        self.assertEqual(self.harry_task()["status"], "active")
        self.assertEqual(len(self.headmaster_events()), 1)

    def test_a_deferred_go_outside_mcgonagalls_session_is_never_started(self):
        before = self.snapshot()
        for fields in ({"agent_type": None}, {"agent_id": "agent-1"}):
            with self.subTest(fields=fields):
                shown, _, _ = self.said(f"go {TASK_ID}", transcript=self.later(), **fields)
                self.assertEqual(shown, f"Go was not applied to {TASK_ID}: {user_prompt_submit.GO_SESSION}\n"
                                        f"{user_prompt_submit.GO_SESSION_FIX}")
        self.spawned_confirms.assert_not_called()
        # A claim and input forged for another session is refused by the confirmer too.
        data = {"prompt": f"go {TASK_ID}", "prompt_id": PROMPT_ID, "transcript_path": self.later(),
                "kind": "go", "desk": "mcgonagall"}
        raw = json.dumps(data, sort_keys=True).encode()
        folder = self.office / config.GO_CONFIRM_DIR
        folder.mkdir(mode=0o700, exist_ok=True)
        self.write_file(folder / go_confirm.claim_key(PROMPT_ID), json.dumps(
            {"state": "claimed", "sha256": hashlib.sha256(raw).hexdigest(), "kind": "go", "task_ids": [TASK_ID]}))
        self.confirm(raw)
        self.assert_unchanged(before)
        [event] = self.headmaster_events()
        self.assertIn(user_prompt_submit.GO_SESSION, event["summary"])

    def test_a_signal_while_the_confirmer_runs_the_go_takes_it_back_and_says_so(self):
        path = self.later()
        before = self.snapshot()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        real_deliver = user_prompt_submit._deliver

        def deliver_then_terminated(*args, **kwargs):
            real_deliver(*args, **kwargs)
            signal.raise_signal(signal.SIGTERM)

        with mock.patch.object(user_prompt_submit, "_deliver", side_effect=deliver_then_terminated), \
                self.assertRaises(SystemExit):
            with common.ended_by_signals():
                self.confirm(payload, self.appears(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID), after=1))
        self.assert_unchanged(before)
        [event] = self.headmaster_events()
        self.assertIn(f"Go for {TASK_ID} was stopped by a signal while it ran", event["summary"])
        self.assertIn(f"castle task show {TASK_ID}", event["summary"])


class DeferredCloseTests(ConfirmCase):
    def test_a_deferred_mischief_managed_closes_once_the_entry_is_there(self):
        task = pensieve.create_task(self.conn, "harry", "fix it", now=NOW)
        pensieve.start_task(self.conn, task["id"], now=NOW)
        pensieve.mark_awaiting_close(self.conn, task["id"], now=NOW)
        path = self.later()
        text = f"Mischief managed {task['id']}"
        shown, context, payload = self.deferred(text, transcript=path, agent_type=None)
        self.assertIn(f"Mischief managed for {task['id']}", shown)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "awaiting_close")
        lines = self.confirm(payload, self.appears(path, user_entry(text, promptId=PROMPT_ID)))
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "closed")
        [event] = self.headmaster_events()
        self.assertEqual(event["kind"], "close.confirmed")
        self.assertIn(f"Mischief managed: task {task['id']} is closed as complete.", event["summary"])
        self.assert_nothing_secret(" ".join(lines) + event["dedupe_key"])


class ManyGosTests(ConfirmCase):
    def test_several_gos_start_each_in_order_and_one_refused_stops_none(self):
        self.task_md()
        self.task_md(THIRD_ID, spec=self.spec(branch="fix/third"))
        self.enable("harry")
        text = f"go {OTHER_ID}\n\n  go {TASK_ID}\ngo {THIRD_ID}\ngo {TASK_ID}\n"
        with mock.patch.object(run_desk, "spawn"):
            shown, context, _ = self.said(text, transcript=self.transcript(text))
        lines = shown.split("\nYour open go tasks")[0].splitlines()  # her go status block follows the gos
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(f"Go was not applied to {OTHER_ID}: there is no TASK.md"))
        self.assertTrue(lines[1].startswith(f"Go: {TASK_ID} is registered"))
        self.assertTrue(lines[2].startswith(f"Go: {THIRD_ID} is registered"))
        self.assertIn(user_prompt_submit.GO_CONTEXT, context)
        self.assertEqual(len(pensieve.list_tasks(self.conn, desk="harry")), 2)

    def test_several_deferred_gos_are_confirmed_together_with_one_event_each(self):
        self.task_md()
        self.enable("harry")
        text = f"go {OTHER_ID}\ngo {TASK_ID}"
        path = self.later()
        _, context, payload = self.deferred(text, transcript=path)
        self.assertIn(f"nothing is applied for {OTHER_ID}, {TASK_ID}", context)
        self.assertIn("Your go status block shows where it stands once it applies.", context)
        self.confirm(payload, self.appears(path, user_entry(text, promptId=PROMPT_ID)))
        kinds = [(event["kind"], event["dedupe_key"].split(":")[1]) for event in self.headmaster_events()]
        self.assertEqual(kinds, [("go.refused", OTHER_ID), ("go.confirmed", TASK_ID)])

    def test_more_than_five_gos_start_nothing(self):
        ids = [f"tk_00000000000000{index:02x}" for index in range(6)]
        text = "\n".join(f"go {task_id}" for task_id in ids)
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            shown, _, _ = self.said(text, transcript=self.transcript(text))
        self.assertEqual(shown, f"{user_prompt_submit.GO_TOO_MANY}\n{user_prompt_submit.GO_TOO_MANY_FIX}")


class GoNoiseTests(ConfirmCase):
    def test_prose_that_mentions_a_go_gets_nothing_and_a_decorated_go_gets_the_refusal(self):
        prose = (f"I think go {TASK_ID} is ready", f"Once you are happy, type go {TASK_ID} in her session.",
                 f"go {TASK_ID}\nand tidy the readme", f"Ready:\n- go {TASK_ID}")
        decorated = (f"- go {TASK_ID}", f"* go {TASK_ID}", f"1. go {TASK_ID}", f"2) go {TASK_ID}", f"`go {TASK_ID}`",
                     f"\"go {TASK_ID}\"", f"- `go {TASK_ID}`\n- `go {OTHER_ID}`", f"go {TASK_ID}, {OTHER_ID}")
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            for text in prose:
                with self.subTest(text=text):
                    self.assertEqual(self.said(text, transcript=self.transcript(text))[0], "")
                    self.assertEqual(self.said(text, agent_type=None)[0], "")  # no session refusal either
            for text in decorated:
                with self.subTest(text=text):
                    fix = (user_prompt_submit.GO_ONE_PER_LINE_FIX if user_prompt_submit.several_on_a_line(text)
                           else user_prompt_submit.GO_EXACT_FIX)
                    self.assertEqual(self.said(text, transcript=self.transcript(text))[0],
                                     f"{user_prompt_submit.GO_EXACT}\n{fix}")
                    self.assertEqual(self.said(text, agent_type=None)[0],
                                     f"Go was not applied: {user_prompt_submit.GO_SESSION}\n"
                                     f"{user_prompt_submit.GO_SESSION_FIX}")
        self.spawned_confirms.assert_not_called()


class SpawnTests(ConfirmCase):
    def test_the_confirmer_gets_its_input_on_a_pipe_with_nothing_in_argv_and_an_empty_environment(self):
        payload = json.dumps({"prompt_id": PROMPT_ID, "prompt": f"go {TASK_ID}"}).encode()
        child = mock.Mock()
        with mock.patch.object(run_desk.subprocess, "Popen", return_value=child) as popen:
            REAL_SPAWN(payload)
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertNotIn(PROMPT_ID, " ".join(argv))
        self.assertNotIn(TASK_ID, " ".join(argv))
        self.assertEqual(list(argv[:len(config.PYTHON_WRAPPER)]), list(config.PYTHON_WRAPPER))
        self.assertIn("from fleet.go_confirm import main", argv[-1])
        self.assertEqual((kwargs["env"], kwargs["stdin"], kwargs["start_new_session"], kwargs["close_fds"],
                          kwargs["cwd"]), ({}, subprocess.PIPE, True, True, str(self.office)))
        self.assertNotIn("pass_fds", kwargs)
        child.stdin.write.assert_called_once_with(payload)
        child.stdin.close.assert_called_once_with()
        self.assertTrue((self.office / "logs" / "go-confirm.log").is_file())

    def test_a_confirmer_that_cannot_start_leaves_a_line_and_no_claim(self):
        self.task_md()
        self.spawned_confirms.side_effect = OSError("no fork")
        shown, _, _ = self.said(f"go {TASK_ID}", transcript=self.later())
        self.assertIn(f"Go was not applied to {TASK_ID}: this hook could not confirm Ryan's own typing (its"
                      " confirmation could not start: OSError)", shown)
        self.assertIn("castle task create", shown)
        self.assertEqual(list((self.office / config.GO_CONFIRM_DIR).iterdir()), [])

    def test_main_logs_one_line_per_outcome_with_no_prompt_id(self):
        out = io.StringIO()
        code = go_confirm.main(stdin=io.BytesIO(json.dumps({"prompt_id": PROMPT_ID}).encode()), stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("go-confirm: refused: the input is not a confirmer request", out.getvalue())
        self.assertNotIn(PROMPT_ID, out.getvalue())

    def test_old_claims_are_pruned_and_this_prompts_claim_is_kept(self):
        _, _, payload = self.deferred(f"go {TASK_ID}")
        folder = self.office / config.GO_CONFIRM_DIR
        old = self.write_file(folder / ("0" * 32), "stale\n")
        os.utime(old, (1, 1))
        self.confirm(payload)
        names = sorted(path.name for path in folder.iterdir())
        key = go_confirm.claim_key(PROMPT_ID)
        self.assertEqual(names, [key, f"{key}.ran"])


def dead_pid() -> int:
    """A process id no process has now."""
    pid = 999_000
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            pass
        pid += 1


class ImmediateMismatchTests(ConfirmCase):
    """The immediate path runs only what the verified entry's own text asks for (shared with the confirmer)."""

    def test_an_entry_naming_other_gos_than_the_hook_input_runs_nothing(self):
        self.task_md()
        self.task_md(OTHER_ID)
        self.enable("harry")
        before = self.snapshot()
        cases = ((f"go {TASK_ID}", f"go {OTHER_ID}"), (f"go {TASK_ID}", f"go {TASK_ID}\ngo {OTHER_ID}"),
                 (f"go {TASK_ID}\ngo {OTHER_ID}", f"go {OTHER_ID}\ngo {TASK_ID}"), (f"go {TASK_ID}", "hello"))
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            for index, (prompt, typed) in enumerate(cases):
                with self.subTest(prompt=prompt, typed=typed):
                    path = self.transcript(typed, name=f"mismatch-{index}.jsonl",
                                           prompt=user_entry(typed, promptId=PROMPT_ID))
                    shown, _, _ = self.said(prompt, transcript=path)
                    self.assertIn(f"Go was not applied to {TASK_ID}: this hook could not confirm Ryan's own typing"
                                  f" ({user_prompt_submit.DIFFERENT})", shown)
        self.assert_unchanged(before)
        self.spawned_confirms.assert_not_called()

    def test_an_entry_naming_another_task_than_the_close_closes_nothing(self):
        tasks = []
        for title in ("one", "two"):
            task = pensieve.create_task(self.conn, "harry", title, now=NOW)
            pensieve.start_task(self.conn, task["id"], now=NOW)
            tasks.append(pensieve.mark_awaiting_close(self.conn, task["id"], now=NOW))
        typed = f"Mischief managed {tasks[1]['id']}"
        path = self.transcript(typed, prompt=user_entry(typed, promptId=PROMPT_ID))
        shown, _, _ = self.said(f"Mischief managed {tasks[0]['id']}", transcript=path, agent_type=None)
        self.assertIn(user_prompt_submit.DIFFERENT, shown)
        self.assertIn("castle token mint", shown)
        for task in tasks:
            self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "awaiting_close")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)


class ClaimTests(ConfirmCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        self.enable("harry")
        self.folder = self.office / config.GO_CONFIRM_DIR
        self.key = go_confirm.claim_key(PROMPT_ID)

    def files(self) -> list:
        return sorted(path.name for path in self.folder.iterdir()) if self.folder.exists() else []

    def test_a_claim_that_cannot_be_written_leaves_nothing_and_the_next_hook_claims_it(self):
        with mock.patch.object(go_confirm.safefs, "write_all", side_effect=OSError("disk full")):
            shown, _, _ = self.said(f"go {TASK_ID}", transcript=self.later())
        self.assertIn("its confirmation could not start: OSError", shown)
        self.assertEqual(self.files(), [])
        self.spawned_confirms.assert_not_called()
        self.deferred(f"go {TASK_ID}")
        self.assertEqual(self.files(), [self.key])

    def test_a_confirmer_that_dies_before_it_reads_its_input_is_reaped_and_its_claim_removed(self):
        child = mock.Mock()
        child.stdin.close.side_effect = [BrokenPipeError(), None]
        with mock.patch.object(run_desk, "spawn_go_confirm", REAL_SPAWN), \
                mock.patch.object(run_desk.subprocess, "Popen", return_value=child):
            shown, _, _ = self.said(f"go {TASK_ID}", transcript=self.later())
        self.assertIn("its confirmation could not start: the confirmer did not get its whole input", shown)
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)
        self.assertEqual(self.files(), [])

    def test_a_signal_after_the_confirmer_has_its_input_never_removes_its_claim(self):
        self.spawned_confirms.side_effect = lambda payload: signal.raise_signal(signal.SIGINT)
        data = {"prompt": f"go {TASK_ID}", "prompt_id": PROMPT_ID, "transcript_path": self.later(),
                "agent_type": "mcgonagall"}
        with self.assertRaises(KeyboardInterrupt):
            go_confirm.start(data, "mcgonagall", "go")
        self.spawned_confirms.assert_called_once()
        self.assertEqual(self.files(), [self.key])


class InterruptedTests(ConfirmCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        self.enable("harry")
        self.folder = self.office / config.GO_CONFIRM_DIR
        self.key = go_confirm.claim_key(PROMPT_ID)
        self.old = int(time.time()) - config.GO_CONFIRM_WAIT_SECONDS - config.GO_CONFIRM_STALE_MARGIN_SECONDS - 5

    def marker(self) -> dict:
        return json.loads((self.folder / f"{self.key}.ran").read_text())

    def pending(self, pid: int, stamp: int, made: list = ()) -> None:
        self.write_file(self.folder / f"{self.key}.ran", json.dumps(
            {"state": "pending", "pid": pid, "at": stamp, "kind": "go", "task_ids": [TASK_ID], "made": list(made)}))

    def assert_interrupted_once(self, shown: str = None) -> dict:
        events = self.headmaster_events()
        self.assertEqual(len(events), 1)
        said = (f"The confirmation for {TASK_ID} was interrupted; check castle task show {TASK_ID}. Type the go again"
                " if it is not registered.")
        self.assertEqual(events[0]["summary"], said)
        self.assertEqual(events[0]["dedupe_key"], f"go-confirm:{TASK_ID}:{self.key[:16]}")
        if shown is not None:
            self.assertIn(said, shown)
        self.assertEqual(self.marker(), {"state": "done"})
        return events[0]

    def test_a_confirmer_that_died_is_reported_by_the_next_hook_and_never_run_again(self):
        path = self.later()
        before = self.snapshot()
        self.deferred(f"go {TASK_ID}", transcript=path)
        self.pending(dead_pid(), self.old)
        self.append(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID))
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            shown, _, _ = self.said(f"go {TASK_ID}", transcript=path)
            self.assert_interrupted_once(shown)
            shown, _, _ = self.said(f"go {TASK_ID}", transcript=path)
        self.assertIn(f"Go for {TASK_ID} was confirmed already for this prompt", shown)
        self.assert_interrupted_once()
        self.assert_unchanged(before)

    def test_the_owl_post_sweep_reports_a_dead_confirmer_with_no_retry_after_time_passes(self):
        path = self.later()
        before = self.snapshot()
        self.deferred(f"go {TASK_ID}", transcript=path)
        self.pending(dead_pid(), int(time.time()))
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            from fleet import owl_post
            owl_post.run_pass(self.conn, now=NOW)
            self.assertEqual(self.headmaster_events(), [])  # not old enough yet
            later = time.time() + config.GO_CONFIRM_WAIT_SECONDS + config.GO_CONFIRM_STALE_MARGIN_SECONDS + 1
            with mock.patch.object(go_confirm.time, "time", return_value=later):
                owl_post.run_pass(self.conn, now=NOW)
                owl_post.run_pass(self.conn, now=NOW)
        self.assert_interrupted_once()
        self.assert_unchanged(before)

    def test_the_sweep_reports_a_claim_no_confirmer_ever_took(self):
        self.deferred(f"go {TASK_ID}")
        self.assertEqual(go_confirm.sweep(self.conn), [])
        os.utime(self.folder / self.key, (self.old, self.old))
        self.assertEqual(len(go_confirm.sweep(self.conn)), 1)
        self.assert_interrupted_once()
        self.assertEqual(go_confirm.sweep(self.conn), [])

    def test_a_go_killed_while_making_its_worktree_names_what_to_remove(self):
        self.deferred(f"go {TASK_ID}")
        made = {"task_id": TASK_ID, "repo_dir": str(self.repo), "branch": "fix/widget",
                "worktree": str(self.castle / "worktrees" / "tk_00000000000000bb")}
        self.pending(dead_pid(), self.old, [made])
        go_confirm.sweep(self.conn)
        [event] = self.headmaster_events()
        self.assertIn(f"It was making the worktree {made['worktree']} on branch fix/widget in {self.repo}: if castle"
                      " task show finds no Harry task with this worktree, remove that worktree and branch by hand,"
                      " then type the go again.", event["summary"])

    def test_a_live_confirmer_is_left_alone_and_a_second_one_refuses(self):
        path = self.later()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        self.pending(os.getpid(), self.old)
        shown, _, _ = self.said(f"go {TASK_ID}", transcript=path)
        self.assertIn("is already being confirmed", shown)
        self.assertEqual(self.confirm(payload), ["refused: another confirmer has this prompt"])
        self.assertEqual(go_confirm.sweep(self.conn), [])
        self.assertEqual(self.headmaster_events(), [])
        self.assertEqual(self.marker()["state"], "pending")

    def test_a_confirmer_finding_a_dead_ones_marker_reports_it_and_runs_nothing(self):
        path = self.later()
        before = self.snapshot()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        self.pending(dead_pid(), self.old)
        self.append(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID))
        with mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            self.confirm(payload)
        self.assert_interrupted_once()
        self.assert_unchanged(before)

    def test_a_store_outage_leaves_the_marker_pending_for_the_sweep(self):
        path = self.later()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        entry = user_entry(f"go {TASK_ID}", promptId=PROMPT_ID)
        with mock.patch.object(go_confirm.common, "connect", side_effect=sqlite3.OperationalError("locked")):
            lines = self.confirm(payload, self.appears(path, entry, after=1))
        self.assertIn("the store could not be opened", lines[0])
        marker = self.marker()
        self.assertEqual((marker["state"], marker["pid"], marker["kind"], marker["task_ids"]),
                         ("pending", os.getpid(), "go", [TASK_ID]))
        self.assertNotIn(PROMPT_ID, json.dumps(marker))
        self.pending(dead_pid(), self.old)  # its process has ended
        go_confirm.sweep(self.conn)
        self.assert_interrupted_once()

    def test_a_confirmed_go_records_its_worktree_first_and_ends_done(self):
        path = self.later()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        seen = []
        real = worktree.create

        def create(*args, **kwargs):
            seen.append(self.marker()["made"])  # recorded before git makes anything
            return real(*args, **kwargs)

        with mock.patch.object(worktree, "create", side_effect=create):
            self.confirm(payload, self.appears(path, user_entry(f"go {TASK_ID}", promptId=PROMPT_ID), after=1))
        built = self.harry_task()
        self.assertEqual(seen, [[{"task_id": TASK_ID, "repo_dir": str(self.repo), "branch": "fix/widget",
                                  "worktree": config.worktree_dir(built["id"])}]])
        self.assertEqual(self.marker(), {"state": "done"})


class KilledWhileMakingTheWorktreeTests(ConfirmCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        self.enable("harry")

    def test_sigterm_after_git_made_the_worktree_takes_it_all_back_and_says_so(self):
        path = self.later()
        before = self.snapshot()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        entry = user_entry(f"go {TASK_ID}", promptId=PROMPT_ID)
        with mock.patch.object(worktree, "add_worktree", side_effect=then_terminated(worktree.add_worktree)), \
                self.assertRaises(SystemExit), common.ended_by_signals():
            self.confirm(payload, self.appears(path, entry, after=1))
        self.assert_unchanged(before)
        [event] = self.headmaster_events()
        self.assertIn(f"Go for {TASK_ID} was stopped by a signal while it ran", event["summary"])
        self.assertEqual(json.loads((self.office / config.GO_CONFIRM_DIR / f"{go_confirm.claim_key(PROMPT_ID)}.ran")
                                    .read_text()), {"state": "done"})

    def test_a_kill_after_the_commit_when_the_store_cannot_say_names_what_is_left(self):
        path = self.later()
        _, _, payload = self.deferred(f"go {TASK_ID}", transcript=path)
        entry = user_entry(f"go {TASK_ID}", promptId=PROMPT_ID)
        state = {}
        with mock.patch.object(user_prompt_submit, "db", committed_then_unreadable(state)), \
                mock.patch.object(pensieve, "get_task", side_effect=unreadable_tasks(state)):
            self.confirm(payload, self.appears(path, entry, after=1))
        built = self.harry_task()
        summary = " ".join(event["summary"] for event in self.headmaster_events())
        self.assertIn(f"the store could not say whether task {built['id']} kept its worktree, so nothing was taken"
                      " back", summary)
        self.assertIn(f"the record {built['id']}.json", summary)
