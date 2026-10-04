from __future__ import annotations

import json
import os
import subprocess
from unittest import mock

from hogwarts import pensieve

from fleet import run_desk
from tests_fleet.support import fake_children
from tests_fleet.test_run_desk import RunDeskCase

RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 61000, "num_turns": 4,
    "result": "Reviewed.", "total_cost_usd": 0.4213,
    "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 9000,
              "output_tokens": 700},
    "modelUsage": {
        "claude-opus-5-5": {"inputTokens": 100, "outputTokens": 700, "cacheReadInputTokens": 9000,
                            "cacheCreationInputTokens": 50, "costUSD": 0.41},
        "claude-haiku-4-5": {"inputTokens": 30, "outputTokens": 5, "cacheReadInputTokens": 0,
                             "cacheCreationInputTokens": 0, "costUSD": 0.0113},
    },
}
STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "the word \"result\" in passing"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "{\"type\": \"result\"}"}]}},
    RESULT,
]


def stream(events: list, noise: bytes = b"") -> bytes:
    return noise + b"".join(json.dumps(event).encode() + b"\n" for event in events)


class StreamUsageTests(RunDeskCase):
    def test_the_final_result_event_gives_usage_from_model_usage(self):
        self.assertEqual(run_desk.parse_claude_usage(stream(STREAM)),
                         {"input_tokens": 180, "output_tokens": 705, "cache_read_tokens": 9000, "cost_usd": 0.4213,
                          "is_error": False, "subtype": "success"})
        self.assertEqual(run_desk.claude_outcome(stream(STREAM)), {"is_error": False, "subtype": "success"})

    def test_usage_is_the_fallback_without_model_usage(self):
        result = {key: value for key, value in RESULT.items() if key != "modelUsage"}
        self.assertEqual(run_desk.parse_claude_usage(stream(STREAM[:3] + [result])),
                         {"input_tokens": 150, "output_tokens": 700, "cache_read_tokens": 9000, "cost_usd": 0.4213,
                          "is_error": False, "subtype": "success"})
        broken = {**result, "modelUsage": {"claude-opus-5-5": "not counts"}}
        self.assertEqual(run_desk.parse_claude_usage(stream([broken]))["input_tokens"], 150)

    def test_non_json_lines_and_a_cut_first_line_are_tolerated(self):
        raw = b'pe": "result", "half a line"}\nWarning: something on stdout\n' + stream(STREAM) + b'{"type": "res'
        self.assertEqual(run_desk.parse_claude_usage(raw)["cost_usd"], 0.4213)
        self.assertEqual(run_desk.claude_result(raw)["num_turns"], 4)

    def test_the_last_result_wins_and_bad_values_count_as_zero(self):
        first = {"type": "result", "subtype": "error_during_execution", "is_error": True, "total_cost_usd": 9.0}
        self.assertEqual(run_desk.claude_outcome(stream([first])),
                         {"is_error": True, "subtype": "error_during_execution"})
        self.assertEqual(run_desk.parse_claude_usage(stream([first, RESULT]))["cost_usd"], 0.4213)
        bad = {"type": "result", "total_cost_usd": -1, "usage": {"input_tokens": -5, "output_tokens": True,
                                                               "cache_read_input_tokens": 1.5}}
        self.assertEqual(run_desk.parse_claude_usage(stream([bad])),
                         {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0,
                          "is_error": False, "subtype": None})

    def test_no_result_event_records_zero(self):
        for raw in (b"", b"not json at all", stream(STREAM[:3]), b"[]", b'{"type": "result", "deep": ' + b"[" * 100000):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(run_desk.parse_claude_usage(raw)["input_tokens"], 0)
                self.assertEqual(run_desk.claude_outcome(raw), {"is_error": False, "subtype": None})

    def test_the_older_single_result_forms_still_parse(self):
        self.assertEqual(run_desk.parse_claude_usage(json.dumps(RESULT).encode())["cost_usd"], 0.4213)
        self.assertEqual(run_desk.parse_claude_usage(json.dumps([STREAM[0], RESULT]).encode())["output_tokens"], 705)

    def test_one_result_reader_serves_usage_the_model_and_the_plan_limit(self):
        pretty = json.dumps(RESULT, indent=2).encode()
        stream = b"\n".join(json.dumps(event).encode() for event in STREAM)
        for raw in (pretty, stream):
            with self.subTest(form="pretty" if raw is pretty else "stream"):
                self.assertEqual(run_desk.claude_result(raw)["total_cost_usd"], 0.4213)
                self.assertEqual(run_desk.parse_claude_model(raw), "claude-opus-5-5")
        limited = json.dumps({**RESULT, "is_error": True, "subtype": "error_during_execution",
                              "result": "5-hour limit reached"}, indent=2).encode()
        self.assertEqual(run_desk.plan_limit("claude", limited, True), "claude_plan")

    def test_a_real_run_records_usage_from_the_stream(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")

        def fake_claude(argv, **kwargs):
            os.write(kwargs["stdout"], stream(STREAM))
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with fake_children(fake_claude):
            result = run_desk.run(self.conn, "hermione", owl_id)
        self.assertEqual((result["input_tokens"], result["cost_usd"], result["subtype"], result["is_error"]),
                         (180, 0.4213, "success", False))
        [row] = pensieve.summary(self.conn)
        self.assertEqual((row["desk"], row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]),
                         ("hermione", 180, 705, 9000))

    def test_a_stream_longer_than_the_read_limit_still_yields_its_result(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        filler = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "z" * 4000}]}}

        def fake_claude(argv, **kwargs):
            os.write(kwargs["stdout"], stream([filler] * 40 + [RESULT]))
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with fake_children(fake_claude), \
                mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", 65536):
            result = run_desk.run(self.conn, "hermione", owl_id)
        self.assertEqual(result["cost_usd"], 0.4213)
