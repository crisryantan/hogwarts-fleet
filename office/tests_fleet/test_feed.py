from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import unittest
from unittest import mock

from hogwarts import db, owlery, pensieve
from tests.support import NOW

from fleet import config, feed, tools
from tests_fleet.support import FleetCase

RUN_A = "run-0123456789abcdef"
RUN_B = "run-fedcba9876543210"
LONG_COMMAND = "git diff --stat " + "x" * 300
CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": ["Read", "Bash"]},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "Looking at the diff now.\nTwo files changed."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": LONG_COMMAND}},
    ]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/Users/crisryantan/hogwarts/PLAN.md"}},
        {"type": "tool_use", "id": "t3", "name": "Edit", "input": {"file_path": "/tmp/a.py", "old_string": "x"}},
        {"type": "tool_use", "id": "t4", "name": "TodoWrite", "input": {"todos": []}},
    ]}},
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.42, "num_turns": 3,
     "result": "Reviewed it. PASS.", "usage": {"input_tokens": 5, "output_tokens": 7}},
]
CLAUDE_LINES = [
    "claude started, model claude-opus-5-5",
    "says: Looking at the diff now. Two files changed.",
    "tool: Bash " + LONG_COMMAND[:157] + "...",
    "tool: Read /Users/crisryantan/hogwarts/PLAN.md",
    "tool: Edit /tmp/a.py",
    "tool: TodoWrite",
    "result: success: Reviewed it. PASS.",
]
CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "th_1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "i0", "type": "command_execution", "command": "bash -lc ls",
                                      "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i0", "type": "command_execution", "command": "bash -lc ls",
                                        "aggregated_output": "a\nb\n", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "file_change", "status": "completed",
                                        "changes": [{"path": "/w/app.py", "kind": "update"},
                                                    {"path": "/w/new.py", "kind": "add"}]}},
    {"type": "item.completed", "item": {"id": "i2", "type": "reasoning", "text": "private"}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Done, tests pass."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}},
]
CODEX_LINES = ["codex started", "command (exit 0): bash -lc ls", "files: update /w/app.py, add /w/new.py",
               "says: Done, tests pass."]


def jsonl(events: list) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def texts(lines: list) -> list:
    """Feed lines without their HH:MM:SS stamp."""
    for printed in lines:
        assert printed[2] == ":" and printed[5] == ":" and printed[8] == " ", printed
    return [printed[9:] for printed in lines]


class SanitizeTests(unittest.TestCase):
    def test_an_osc_52_clipboard_write_is_removed(self):
        for text in ("a\x1b]52;c;ZWNobyBwd25lZA==\x07b", "a\x1b]52;c;ZWNobyBwd25lZA==\x1b\\b",
                     "a\x1b]0;window title\x9cb", "a\x1b]52;c;ZWNobyBwd25lZA=="):
            with self.subTest(text=text):
                self.assertIn(feed.sanitize(text), ("ab", "a"))
                self.assertNotIn("52", feed.sanitize(text))

    def test_a_csi_clear_screen_is_removed(self):
        self.assertEqual(feed.sanitize("\x1b[2J\x1b[H\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(feed.sanitize("cut \x1b[2"), "cut ")

    def test_a_bell_is_dropped(self):
        self.assertEqual(feed.sanitize("ding\x07dong"), "dingdong")

    def test_c1_controls_are_dropped(self):
        self.assertEqual(feed.sanitize("a\x9b2Jb"), "ab")
        self.assertEqual(feed.sanitize("a\x9d52;c;QUFB\x9cb"), "ab")
        self.assertEqual(feed.sanitize("a\x90q#1\x1b\\b"), "ab")
        self.assertEqual(feed.sanitize("a\x85\x8db"), "ab")

    def test_other_escapes_dcs_and_a_lone_esc_are_removed(self):
        self.assertEqual(feed.sanitize("a\x1bPqpayload\x1b\\b"), "ab")
        self.assertEqual(feed.sanitize("a\x1bcb"), "ab")  # ESC c resets the terminal
        self.assertEqual(feed.sanitize("a\x1b(Bb"), "ab")
        self.assertEqual(feed.sanitize("end\x1b"), "end")

    def test_c0_and_del_are_dropped_but_newline_and_tab_kept(self):
        self.assertEqual(feed.sanitize("a\x00b\x08c\x7fd\te\nf\rg"), "abcd\te\nfg")

    def test_other_non_printables_become_question_marks(self):
        self.assertEqual(feed.sanitize("left\u202eright\u2028x"), "left?right?x")
        self.assertEqual(feed.sanitize("caf\u00e9 \u2713"), "caf\u00e9 \u2713")

    def test_a_printed_line_is_one_capped_line(self):
        printed = feed.line(NOW, "x" * 1000 + "\nsecond")
        self.assertEqual(len(printed), feed.LINE_MAX_CHARS)
        self.assertNotIn("\n", feed.line(NOW, "one\ntwo"))
        self.assertEqual(printed[:8], feed.stamp(NOW))

    def test_desk_output_cannot_reach_the_terminal_through_any_event(self):
        hostile = "ok\x1b]52;c;cm0gLXJmIH4=\x07\x1b[2J\x07\x9b2J\u202e"
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": hostile},
                                                          {"type": "tool_use", "name": hostile,
                                                           "input": {"command": hostile}}]}},
            {"type": "result", "subtype": hostile, "result": hostile},
            {"type": "item.completed", "item": {"type": "agent_message", "text": hostile}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": hostile, "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": hostile,
                                                                                   "kind": hostile}]}},
            {"type": "turn.failed", "error": {"message": hostile}},
        ]
        for event in events:
            for text in feed.render_event(json.loads(json.dumps(event))):
                printed = feed.line(NOW, text)
                with self.subTest(printed=printed):
                    self.assertEqual([char for char in printed if char < " " or "\x7f" <= char <= "\x9f"], [])
                    self.assertNotIn("52;", printed)


class RenderTests(unittest.TestCase):
    def test_claude_stream_json(self):
        rendered = [text for event in CLAUDE_STREAM for text in feed.render_event(event)]
        self.assertEqual(rendered, CLAUDE_LINES)

    def test_codex_jsonl(self):
        rendered = [text for event in CODEX_STREAM for text in feed.render_event(event)]
        self.assertEqual(rendered, CODEX_LINES)

    def test_failures_are_labelled(self):
        self.assertEqual(feed.render_event({"type": "result", "subtype": "error_max_budget_usd", "is_error": True}),
                         ["result: error_max_budget_usd (error)"])
        self.assertEqual(feed.render_event({"type": "turn.failed", "error": {"message": "rate limited"}}),
                         ["turn failed: rate limited"])
        self.assertEqual(feed.render_event({"type": "item.completed", "item": {
            "type": "command_execution", "command": ["git", "push"], "exit_code": 128}}),
            ["command (exit 128): git push"])

    def test_long_text_is_cut(self):
        [text] = feed.render_event({"type": "assistant", "message": {"content": [{"type": "text",
                                                                                  "text": "y" * 900}]}})
        self.assertEqual(len(text), len("says: ") + feed.TEXT_MAX_CHARS)

    def test_unknown_or_malformed_events_are_skipped(self):
        for event in ({"type": "rate_limit"}, {"type": "item.completed"}, {"type": "assistant", "message": "x"},
                      {"type": "assistant", "message": {"content": "x"}}, {"type": 3}, [], "text", None,
                      {"type": "item.completed", "item": {"type": "agent_message", "text": 5}}):
            with self.subTest(event=event):
                self.assertEqual(feed.render_event(event), [])


class FeedCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.reader = db.connect_readonly(self.db_path)
        self.addCleanup(self.reader.close)

    def run_file(self, desk: str, run_id: str, data: bytes, mtime: int = NOW) -> str:
        folder = self.office / "runs" / desk
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{run_id}.out"
        self.write_file(path, data)
        os.utime(path, (mtime, mtime))
        return str(path)

    def append(self, path: str, data: bytes) -> None:
        with open(path, "ab") as handle:
            handle.write(data)


class FeedTests(FeedCase):
    def test_a_desk_feed_follows_owls_runs_events_and_run_ends(self):
        follow = feed.Feed(self.reader, "hermione")
        self.assertEqual(follow.poll(NOW), [])
        owlery.send(self.conn, "mcgonagall", "hermione", "fyi", "look at the diff", body="secret body", now=NOW)
        owlery.send(self.conn, "harry", "moody", "fyi", "not hers", now=NOW)
        stream = jsonl(CLAUDE_STREAM)
        cut = stream.index(b"\n", stream.index(b"\n") + 1) + 20
        path = self.run_file("hermione", RUN_A, stream[:cut])
        lines = texts(follow.poll(NOW + 1))
        self.assertEqual(lines, ["owl mcgonagall -> hermione fyi: look at the diff", f"run start {RUN_A}"]
                         + CLAUDE_LINES[:3])
        self.append(path, stream[cut:])
        pensieve.add_metric(self.conn, "hermione", RUN_A, "opus", 150, 700, 9000, 0.42, 65000, ts=NOW + 2)
        pensieve.add_event(self.conn, "hermione", "rundesk.failed", "headmaster", "a run did not finish", now=NOW + 2)
        pensieve.add_event(self.conn, "hermione", "owl.doorbell", "routine", "ring", now=NOW + 2)
        pensieve.add_event(self.conn, "ron", "rundesk.cap", "headmaster", "not hers", now=NOW + 2)
        lines = texts(follow.poll(NOW + 2))
        self.assertEqual(lines, CLAUDE_LINES[3:] + [
            f"run end {RUN_A}: model opus, 1m05s, tokens in 150 out 700 cache 9000, $0.42, status success",
            "headmaster rundesk.failed: a run did not finish",
        ])
        self.assertEqual(follow.poll(NOW + 3), [])
        self.assertNotIn("secret body", "".join(lines))

    def test_a_partial_last_line_waits_for_its_newline(self):
        follow = feed.Feed(self.reader, "harry")
        follow.poll(NOW)
        line = json.dumps(CODEX_STREAM[3]).encode()
        path = self.run_file("harry", RUN_A, jsonl(CODEX_STREAM[:1]) + line[:10])
        self.assertEqual(texts(follow.poll(NOW + 1)), [f"run start {RUN_A}", "codex started"])
        self.append(path, line[10:])
        self.assertEqual(follow.poll(NOW + 2), [])
        self.append(path, b"\n")
        self.assertEqual(texts(follow.poll(NOW + 3)), ["command (exit 0): bash -lc ls"])

    def test_a_new_run_finishes_the_old_one_first(self):
        follow = feed.Feed(self.reader, "harry")
        follow.poll(NOW)
        old = self.run_file("harry", RUN_A, jsonl(CODEX_STREAM[:1]), mtime=NOW)
        self.assertEqual(texts(follow.poll(NOW + 1)), [f"run start {RUN_A}", "codex started"])
        self.append(old, json.dumps(CODEX_STREAM[6]).encode())  # the last event, with no newline
        self.run_file("harry", RUN_B, jsonl(CODEX_STREAM[:1]), mtime=NOW + 5)
        self.assertEqual(texts(follow.poll(NOW + 6)),
                         ["says: Done, tests pass.", f"run start {RUN_B}", "codex started"])

    def test_on_start_a_run_in_progress_is_shown_from_its_start_and_a_finished_one_is_not(self):
        self.run_file("hermione", RUN_A, jsonl(CLAUDE_STREAM[:2]), mtime=NOW - 30)
        self.run_file("ron", RUN_B, jsonl(CLAUDE_STREAM[:2]), mtime=NOW - 30)
        pensieve.add_metric(self.conn, "ron", RUN_B, "haiku", 1, 1, 0, 0.01, 10, ts=NOW - 20)
        self.assertEqual(texts(feed.Feed(self.reader, "hermione").poll(NOW))[:2],
                         [f"run in progress {RUN_A}, shown from its start", CLAUDE_LINES[0]])
        self.assertEqual(feed.Feed(self.reader, "ron").poll(NOW), [])
        self.run_file("moody", RUN_A, jsonl(CODEX_STREAM[:1]), mtime=NOW - config.RUN_TIMEOUT_SECONDS - 600)
        self.assertEqual(feed.Feed(self.reader, "moody").poll(NOW), [])

    def test_missing_folders_and_files_are_quiet(self):
        follow = feed.Feed(self.reader, "hermione")
        self.assertEqual(follow.poll(NOW), [])
        path = self.run_file("hermione", RUN_A, b"not json\n" + jsonl(CLAUDE_STREAM[:1]) + b"[1, 2]\n{bad\n")
        self.assertEqual(texts(follow.poll(NOW + 1)), [f"run start {RUN_A}", CLAUDE_LINES[0]])
        os.unlink(path)
        self.assertEqual(follow.poll(NOW + 2), [])
        self.assertEqual(feed.Feed(self.reader, None).poll(NOW), [])

    def test_a_symlinked_run_file_is_not_read(self):
        target = self.tmp / "elsewhere.jsonl"
        self.write_file(target, jsonl(CLAUDE_STREAM))
        folder = self.office / "runs" / "hermione"
        folder.mkdir(parents=True, mode=0o700)
        follow = feed.Feed(self.reader, "hermione")
        follow.poll(NOW)
        os.symlink(target, folder / f"{RUN_A}.out")
        self.assertEqual(follow.poll(NOW + 1), [])

    def test_owl_post_shows_every_owl_and_its_own_events(self):
        follow = feed.Feed(self.reader, "owl-post")
        owlery.send(self.conn, "harry", "moody", "fyi", "please review", now=NOW)
        owlery.send(self.conn, "hermione", "mcgonagall", "fyi", "done", now=NOW)
        pensieve.add_event(self.conn, "harry", "owlpost.rejected", "headmaster", "an owl was rejected", now=NOW)
        pensieve.add_event(self.conn, "harry", "rundesk.failed", "headmaster", "not owl traffic", now=NOW)
        self.run_file("harry", RUN_A, jsonl(CODEX_STREAM))
        self.assertEqual(texts(follow.poll(NOW + 1)), [
            "owl harry -> moody fyi: please review",
            "owl hermione -> mcgonagall fyi: done",
            "harry: headmaster owlpost.rejected: an owl was rejected",
        ])

    def test_the_all_feed_names_each_desk(self):
        follow = feed.Feed(self.reader, None)
        follow.poll(NOW)
        self.run_file("harry", RUN_A, jsonl(CODEX_STREAM[:1]))
        self.run_file("hermione", RUN_B, jsonl(CLAUDE_STREAM[:1]))
        pensieve.add_event(self.conn, "ron", "rundesk.cap", "headmaster", "ron is capped", now=NOW)
        self.assertEqual(texts(follow.poll(NOW + 1)), [
            f"harry: run start {RUN_A}", "harry: codex started",
            f"hermione: run start {RUN_B}", f"hermione: {CLAUDE_LINES[0]}",
            "ron: headmaster rundesk.cap: ron is capped",
        ])

    def test_a_run_end_waits_for_the_output_the_feed_has_not_read_yet(self):
        follow = feed.Feed(self.reader, "hermione")
        follow.poll(NOW)
        filler = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "y" * 1000}]}}
        events = CLAUDE_STREAM[:2] + [filler] * 30 + CLAUDE_STREAM[2:]
        path = self.run_file("hermione", RUN_A, jsonl(events))
        with mock.patch.object(feed, "READ_CHUNK_BYTES", 4096):
            self.assertEqual(texts(follow.poll(NOW + 1)), [f"run start {RUN_A}"] + CLAUDE_LINES[:3])
            pensieve.add_metric(self.conn, "hermione", RUN_A, "opus", 1, 2, 3, 0.5, 2000, ts=NOW + 2)
            lines = texts(follow.poll(NOW + 2))
        self.assertEqual(lines, CLAUDE_LINES[3:] + [
            f"run end {RUN_A}: model opus, 2.0s, tokens in 1 out 2 cache 3, $0.50, status success"])
        self.assertEqual(follow.tails["hermione"].offset, os.path.getsize(path))
        self.assertEqual(follow.poll(NOW + 3), [])

    def test_a_run_far_behind_skips_to_its_end_for_the_result(self):
        follow = feed.Feed(self.reader, "hermione")
        follow.poll(NOW)
        filler = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "y" * 1000}]}}
        events = CLAUDE_STREAM[:2] + [filler] * 100 + CLAUDE_STREAM[-1:]
        self.run_file("hermione", RUN_A, jsonl(events))
        with mock.patch.object(feed, "READ_CHUNK_BYTES", 4096), mock.patch.object(feed, "FINAL_READ_CHUNKS", 2):
            follow.poll(NOW + 1)
            pensieve.add_metric(self.conn, "hermione", RUN_A, "opus", 1, 2, 3, 0.5, 2000, ts=NOW + 2)
            lines = texts(follow.poll(NOW + 2))
        self.assertTrue(lines[0].startswith("skipped ") and lines[0].endswith(" bytes of run output"), lines)
        self.assertEqual(lines[1:], [CLAUDE_LINES[-1],
                                     f"run end {RUN_A}: model opus, 2.0s, tokens in 1 out 2 cache 3, $0.50, status success"])

    def test_a_run_that_ends_as_the_next_starts_ends_before_it(self):
        follow = feed.Feed(self.reader, "harry")
        follow.poll(NOW)
        old = self.run_file("harry", RUN_A, jsonl(CODEX_STREAM[:1]), mtime=NOW)
        follow.poll(NOW + 1)
        self.append(old, jsonl(CODEX_STREAM[6:]))
        pensieve.add_metric(self.conn, "harry", RUN_A, "codex", 1, 2, 3, 0.0, 500, ts=NOW + 4)
        self.run_file("harry", RUN_B, jsonl(CODEX_STREAM[:1]), mtime=NOW + 5)
        self.assertEqual(texts(follow.poll(NOW + 6)), [
            "says: Done, tests pass.",
            f"run end {RUN_A}: model codex, 0.5s, tokens in 1 out 2 cache 3, $0.00, status completed",
            f"run start {RUN_B}", "codex started"])

    def test_a_run_end_seen_after_the_next_run_started_keeps_its_status(self):
        follow = feed.Feed(self.reader, "harry")
        follow.poll(NOW)
        old = self.run_file("harry", RUN_A, jsonl(CODEX_STREAM[:1]), mtime=NOW)
        follow.poll(NOW + 1)
        self.append(old, jsonl(CODEX_STREAM[6:]))
        self.run_file("harry", RUN_B, jsonl(CODEX_STREAM[:1]), mtime=NOW + 5)
        follow.poll(NOW + 6)
        pensieve.add_metric(self.conn, "harry", RUN_A, "codex", 1, 2, 3, 0.0, 500, ts=NOW + 7)
        self.assertEqual(texts(follow.poll(NOW + 7)), [
            f"run end {RUN_A}: model codex, 0.5s, tokens in 1 out 2 cache 3, $0.00, status completed"])
        self.assertIsNone(follow.tails["harry"].earlier)

    def test_metric_fields_a_later_migration_adds_are_shown(self):
        self.conn.execute("ALTER TABLE metrics ADD COLUMN exit_code INTEGER")
        self.conn.execute("ALTER TABLE metrics ADD COLUMN cap_label TEXT")
        follow = feed.Feed(self.reader, "ron")
        self.conn.execute("INSERT INTO metrics(ts, desk, run_id, model, input_tokens, output_tokens,"
                          " cache_read_tokens, cost_usd, duration_ms, exit_code, cap_label)"
                          " VALUES (?, 'ron', ?, 'haiku', 1, 2, 3, 0.004, 900, 1, 'daily\x1b[2J cap')",
                          (NOW, RUN_A))
        self.assertEqual(texts(follow.poll(NOW)), [
            f"run end {RUN_A}: model haiku, 0.9s, tokens in 1 out 2 cache 3, $0.00, exit_code 1, cap_label daily cap"])

    def test_the_feed_never_writes(self):
        follow = feed.Feed(self.reader, None)
        owlery.send(self.conn, "harry", "moody", "fyi", "please review", now=NOW)
        self.run_file("harry", RUN_A, jsonl(CODEX_STREAM))
        pensieve.add_metric(self.conn, "harry", RUN_A, "codex-default", 1, 1, 0, 0.0, 10, ts=NOW)
        follow.poll(NOW)
        follow.poll(NOW + 1)
        self.assertEqual(self.reader.total_changes, 0)
        with self.assertRaises(sqlite3.OperationalError):
            follow.conn.execute("UPDATE owls SET read_at = 1")
        self.assertEqual(sorted(os.listdir(self.office / "runs" / "harry")), [f"{RUN_A}.out"])

    def test_a_busy_store_is_retried_next_poll(self):
        follow = feed.Feed(self.reader, "hermione")
        pensieve.add_event(self.conn, "hermione", "rundesk.failed", "headmaster", "failed", now=NOW)
        with mock.patch.object(feed.watch, "headmaster_events_after", side_effect=sqlite3.OperationalError("busy")):
            self.assertEqual(follow.poll(NOW), [])
        self.assertEqual(texts(follow.poll(NOW + 1)), ["headmaster rundesk.failed: failed"])


class FollowTests(FeedCase):
    def follow(self, *args, **kwargs) -> tuple:
        out = io.StringIO()
        code = feed.follow(*args, out=out, clock=lambda: NOW, **kwargs)
        return code, out.getvalue().splitlines()

    def test_it_polls_and_sleeps_about_once_a_second(self):
        naps = []
        owlery.send(self.conn, "harry", "hermione", "fyi", "ping", now=NOW)
        with mock.patch.object(feed.watch, "marks", return_value={"owls": 0, "events": 0, "metrics": 0}):
            code, lines = self.follow("hermione", sleep=naps.append, polls=3)
        self.assertEqual(code, 0)
        self.assertEqual(texts(lines), ["watching hermione, read-only. Ctrl+C stops.",
                                        "owl harry -> hermione fyi: ping"])
        self.assertEqual(naps, [feed.POLL_SECONDS, feed.POLL_SECONDS])

    def test_ctrl_c_exits_cleanly(self):
        def interrupt(_):
            raise KeyboardInterrupt
        code, lines = self.follow(None, sleep=interrupt)
        self.assertEqual(code, 0)
        self.assertEqual(texts(lines), ["watching every desk, read-only. Ctrl+C stops."])

    def test_a_missing_store_is_reported_and_never_created(self):
        missing = self.office / "state" / "gone.db"
        with mock.patch.object(config, "DB_PATH", str(missing)):
            code, lines = self.follow("ron", sleep=lambda _: None, polls=1)
        self.assertEqual((code, lines), (1, ["fleet feed: database does not exist, run castle init"]))
        self.assertFalse(missing.exists())

    def test_the_fleet_command_runs_the_feed_without_the_read_write_store(self):
        with mock.patch.object(feed, "follow", return_value=0) as started, \
                mock.patch.object(tools.common, "connect", side_effect=AssertionError("no read-write store")):
            self.assertEqual(tools.main(["feed", "--desk", "owl-post"]), 0)
            self.assertEqual(tools.main(["feed", "--all"]), 0)
        self.assertEqual([call.args for call in started.call_args_list], [("owl-post",), (None,)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tools.main(["feed", "--desk", "Not A Desk"]), 1)
        self.assertIn("invalid desk name", out.getvalue())
        for argv in (["feed"], ["feed", "--all", "--desk", "ron"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                tools.main(argv)
