"""Busy-day capacity in the fleet: the cap numbers, the cap day, bumps, warnings, refusals and vendor limits.

No process starts: subprocess.run is faked to write the run output a fixture gives. Time is always injected.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from unittest import mock

from hogwarts import capacity, owlery, pensieve
from tests.support import DAY, NOW

from fleet import config, run_desk
from tests_fleet.test_run_desk import RunDeskCase

DAY_START = NOW - NOW % DAY
RESET = DAY_START + DAY
RESET_TEXT = "2027-01-16T00:00:00+00:00"
ZONE = 10 * 3600  # a desk clock ten hours east of UTC
LOCAL_START = DAY_START - ZONE
LOCAL_RESET = DAY_START + DAY - ZONE

CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5,
             "result": "REVIEW notes: the rate limit on the login form looks right. VERDICT: PASS"}
CLAUDE_USAGE_LIMIT = {"type": "result", "subtype": "success", "is_error": True, "total_cost_usd": 0,
                      "result": "Claude AI usage limit reached|1800010800"}
CLAUDE_FIVE_HOUR = {"type": "result", "subtype": "success", "is_error": True, "total_cost_usd": 0,
                    "result": "5-hour limit reached - resets 3pm"}
CLAUDE_HIT_LIMIT = {"type": "result", "subtype": "success", "is_error": True,
                    "result": "You've hit your limit - resets 3pm (UTC)"}
CLAUDE_429 = {"type": "result", "subtype": "error_during_execution", "is_error": True, "api_error_status": 429,
              "result": "API Error: request rejected"}
CLAUDE_BUDGET = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                 "result": "Reached the maximum budget; usage limit for this run"}
CLAUDE_CRASH = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                "result": "Tool failed: no such file"}
CODEX_USAGE_LIMIT = [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro or try again in 2 hours."},
    {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again later."}},
]
CODEX_RATE_LIMIT = [
    {"type": "turn.started"},
    {"type": "turn.failed", "error": {"message": "stream error: exceeded retry limit, last status: 429 Too Many Requests"}},
]
CODEX_OK = [
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "the usage limit check is fine"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
]
CODEX_CRASH = [{"type": "turn.failed", "error": {"message": "sandbox denied the write"}}]
# A transient rate limit Codex retried through: the run went on and finished.
CODEX_RETRIED = [
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion: Rate limit reached)"},
    {"type": "error", "message": "stream error: 429 Too Many Requests; retrying 2/5"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "REVIEW done. VERDICT: PASS"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
]
# Retried through a 429, then failed for another reason.
CODEX_RETRIED_THEN_CRASH = CODEX_RETRIED[:3] + [{"type": "turn.failed", "error": {"message": "sandbox denied"}}]
CLAUDE_CONTEXT = {"type": "result", "subtype": "success", "is_error": True, "result": "Context limit reached"}
CLAUDE_SESSION = {"type": "result", "subtype": "success", "is_error": True,
                  "result": "Session limit reached - resets 3pm"}
CLAUDE_INIT = {"type": "system", "subtype": "init", "session_id": "s-1", "tools": ["Read"] * 400}
CLAUDE_TALK = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "The login form has a rate limit; too many requests get a 429. " * 40}]}}


def claude_out(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")


def stream_out(events: list) -> bytes:
    """claude -p --output-format stream-json: one event per line."""
    return "".join(json.dumps(event) + "\n" for event in events).encode("utf-8")


def codex_out(events: list) -> bytes:
    return "".join(json.dumps(event) + "\n" for event in events).encode("utf-8")


class CapCase(RunDeskCase):
    def runs(self, desk: str, count: int, ts: int = NOW - 60, cost: float = 0.0) -> None:
        for index in range(count):
            pensieve.add_metric(self.conn, desk, f"run-{ts}-{index}", "model-x", 1, 1, 0, cost, 10, ts=ts)

    def fake_output(self, raw: bytes, returncode: int):
        """subprocess.run that writes raw to the run's stdout file, as the desk would."""
        def run(argv, cwd, env, stdin, stdout, stderr, timeout, check, pass_fds=()):
            os.write(stdout, raw)
            return subprocess.CompletedProcess(args=argv, returncode=returncode)
        return mock.patch.object(subprocess, "run", side_effect=run)

    def summaries(self, kind: str) -> list:
        return [event["summary"] for event in self.events_of(kind)]


class CapNumberTests(CapCase):
    def test_busy_day_cap_numbers(self):
        self.assertEqual(config.DAILY_RUN_CAP, {"moody": 80, "hermione": 80, "harry": 40, "ron": 120, "portrait": 3})
        self.assertEqual(config.DAILY_SPEND_CAP_USD, {"hermione": 60.0, "ron": 10.0, "portrait": 4.0})
        self.assertEqual(config.MAX_BUDGET_USD, {"hermione": "2.00", "ron": "0.25", "portrait": "2.00"})
        self.assertEqual(set(config.DAILY_RUN_CAP), set(config.HEADLESS_DESKS))
        self.assertEqual((config.REVIEW_ROUND_CAP, config.CAP_WARN_FRACTION, config.CAP_RESET_UTC_SECONDS), (3, 0.8, None))


class CapDayTests(CapCase):
    def test_runs_count_from_the_cap_day_start(self):
        self.runs("moody", 80, ts=DAY_START - 1)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", NOW))
        self.runs("moody", 79, ts=DAY_START)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", NOW))
        self.runs("moody", 1, ts=NOW - 1)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "moody", NOW), "daily run cap reached")
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", RESET))

    def test_the_reset_offset_moves_the_cap_day(self):
        self.assertEqual(capacity.day_bounds(NOW), (DAY_START, RESET))
        offset = 16 * 3600
        with mock.patch.object(config, "CAP_RESET_UTC_SECONDS", offset):
            status = run_desk.cap_status(self.conn, "moody", NOW)
        self.assertEqual((status["day_start"], status["resets_at"]), (DAY_START - DAY + offset, DAY_START + offset))

    def test_a_bump_counts_until_the_reset_then_expires(self):
        self.runs("moody", 80)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "moody", NOW), "daily run cap reached")
        capacity.add_bump(self.conn, "moody", "runs", 5, RESET, now=NOW)
        status = run_desk.cap_status(self.conn, "moody", NOW)
        self.assertEqual((status["runs_used"], status["run_cap"], status["runs_bump"], status["runs_limit"]),
                         (80, 80, 5, 85))
        self.assertIsNone(status["reached"])
        self.runs("moody", 5, ts=NOW + 10)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "moody", NOW + 20), "daily run cap reached")
        self.runs("moody", 85, ts=RESET)
        after = run_desk.cap_status(self.conn, "moody", RESET + 1)
        self.assertEqual((after["runs_bump"], after["runs_limit"], after["reached"]), (0, 80, "runs"))

    def test_a_spend_bump_lifts_the_spend_cap(self):
        self.runs("hermione", 1, cost=60.0)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "hermione", NOW), "daily spend cap reached")
        capacity.add_bump(self.conn, "hermione", "spend", 15.5, RESET, now=NOW)
        status = run_desk.cap_status(self.conn, "hermione", NOW)
        self.assertEqual((status["spend_limit_usd"], status["reached"]), (75.5, None))


class CapEventTests(CapCase):
    def test_a_refusal_names_the_cap_the_waiting_requests_and_the_reset(self):
        self.enable("ron")
        self.request("ron")
        owl_id, _ = self.request("ron")
        self.runs("ron", 120)
        with self.assertRaises(run_desk.Capped):
            run_desk.run(self.conn, "ron", owl_id, now=NOW)
        [summary] = self.summaries("rundesk.cap")
        self.assertIn("fleet daily runs cap is reached (120 of 120 runs)", summary)
        self.assertIn("cap_source fleet", summary)
        self.assertIn("2 request(s) waiting for ron", summary)
        self.assertIn(f"resets at {RESET_TEXT}", summary)
        self.assertIn("castle desk cap ron --runs +N", summary)
        hits = capacity.list_cap_hits(self.conn, "ron")
        self.assertEqual([(hit["cap"], hit["cap_source"]) for hit in hits], [("runs", "fleet")])

    def test_no_duplicate_for_the_same_desk_cap_and_day(self):
        self.runs("ron", 120)
        for offset in (0, 60, 3600):
            run_desk.report_cap(self.conn, "ron", NOW + offset)
        self.assertEqual(len(self.events_of("rundesk.cap")), 1)
        self.assertEqual(len(capacity.list_cap_hits(self.conn, "ron")), 3)
        self.runs("ron", 120, ts=RESET + 10)
        run_desk.report_cap(self.conn, "ron", RESET + 20)
        self.assertEqual(len(self.events_of("rundesk.cap")), 2)

    def test_reaching_a_bumped_limit_the_same_day_is_a_new_event(self):
        self.runs("ron", 120)
        run_desk.report_cap(self.conn, "ron", NOW)
        capacity.add_bump(self.conn, "ron", "runs", 5, RESET, now=NOW + 10)
        self.runs("ron", 5, ts=NOW + 20)
        run_desk.report_cap(self.conn, "ron", NOW + 30)
        run_desk.report_cap(self.conn, "ron", NOW + 40)
        first, second = self.summaries("rundesk.cap")
        self.assertIn("(120 of 120 runs)", first)
        self.assertIn("(125 of 125 runs)", second)
        self.assertEqual(len(capacity.list_cap_hits(self.conn, "ron")), 3)

    def test_reaching_a_bumped_spend_limit_is_a_new_event(self):
        self.runs("portrait", 1, cost=4.0)
        run_desk.report_cap(self.conn, "portrait", NOW)
        capacity.add_bump(self.conn, "portrait", "spend", 0.5, RESET, now=NOW + 10)
        self.runs("portrait", 1, ts=NOW + 20, cost=0.5)
        run_desk.report_cap(self.conn, "portrait", NOW + 30)
        run_desk.report_cap(self.conn, "portrait", NOW + 40)
        self.assertEqual([summary.split("(")[1].split(")")[0] for summary in self.summaries("rundesk.cap")],
                         ["$4.00 of $4.00", "$4.50 of $4.50"])

    def test_the_spend_cap_has_its_own_event(self):
        self.runs("portrait", 1, cost=4.0)
        run_desk.report_cap(self.conn, "portrait", NOW)
        [summary] = self.summaries("rundesk.cap")
        self.assertIn("fleet daily spend cap is reached ($4.00 of $4.00)", summary)
        self.assertIn("castle desk cap portrait --spend +X", summary)

    def test_the_80_percent_warning_fires_once_per_desk_cap_and_day(self):
        self.runs("moody", 63)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW), [])
        self.runs("moody", 1)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW), ["runs"])
        self.runs("moody", 10)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW + 60), [])
        [summary] = self.summaries("rundesk.cap-near")
        self.assertIn("moody has used 64 of 80 runs of its fleet daily runs cap today", summary)
        self.assertIn(RESET_TEXT, summary)
        self.assertEqual(self.events_of("rundesk.cap-near")[0]["verdict"], "headmaster")
        self.runs("moody", 64, ts=RESET + 1)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", RESET + 2), ["runs"])
        self.assertEqual(len(self.events_of("rundesk.cap-near")), 2)

    def test_a_bump_warns_again_near_the_raised_limit_once(self):
        self.runs("moody", 64)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW), ["runs"])
        capacity.add_bump(self.conn, "moody", "runs", 20, RESET, now=NOW + 10)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW + 20), [])  # 64 of 100 is under 80%
        self.runs("moody", 16, ts=NOW + 30)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW + 40), ["runs"])
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW + 50), [])
        first, second = self.summaries("rundesk.cap-near")
        self.assertIn("64 of 80 runs", first)
        self.assertIn("80 of 100 runs", second)

    def test_runs_and_spend_warn_separately(self):
        self.runs("hermione", 1, cost=48.0)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "hermione", NOW), ["spend"])
        self.runs("hermione", 63)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "hermione", NOW), ["runs"])
        self.assertEqual(run_desk.warn_near_cap(self.conn, "hermione", NOW), [])

    def test_a_finished_run_checks_the_warning(self):
        self.enable("portrait")
        owl_id, _ = self.request("portrait")
        self.runs("portrait", 2)
        with self.fake_output(claude_out(CLAUDE_OK), 0):
            result = run_desk.run(self.conn, "portrait", owl_id, now=NOW)
        self.assertEqual((result["exit_code"], result["cap_source"]), (0, None))
        self.assertEqual(len(self.events_of("rundesk.cap-near")), 1)


class PlanLimitTests(CapCase):
    def test_claude_fixtures(self):
        for name, data, failed, expected in (
            ("usage limit", CLAUDE_USAGE_LIMIT, True, "claude_plan"),
            ("five hour", CLAUDE_FIVE_HOUR, True, "claude_plan"),
            ("hit your limit", CLAUDE_HIT_LIMIT, True, "claude_plan"),
            ("api 429", CLAUDE_429, True, "claude_plan"),
            ("a review that talks about rate limits", CLAUDE_OK, False, None),
            ("the fleet's own per-run budget", CLAUDE_BUDGET, True, None),
            ("an ordinary failure", CLAUDE_CRASH, True, None),
        ):
            with self.subTest(name=name):
                self.assertEqual(run_desk.plan_limit("claude", claude_out(data), failed), expected)
        self.assertEqual(run_desk.plan_limit("claude", claude_out([{"type": "system"}, CLAUDE_USAGE_LIMIT]), True),
                         "claude_plan")
        self.assertEqual(run_desk.plan_limit("claude", b"Claude AI usage limit reached\n", True), "claude_plan")
        self.assertIsNone(run_desk.plan_limit("claude", b"Claude AI usage limit reached\n", False))
        self.assertIsNone(run_desk.plan_limit("claude", b"", True))

    def test_codex_fixtures(self):
        for name, events, expected in (
            ("usage limit", CODEX_USAGE_LIMIT, "codex_plan"),
            ("429 rate limit", CODEX_RATE_LIMIT, "codex_plan"),
            ("a clean turn that mentions a usage limit", CODEX_OK, None),
            ("an ordinary failure", CODEX_CRASH, None),
        ):
            with self.subTest(name=name):
                self.assertEqual(run_desk.plan_limit("codex", codex_out(events), True), expected)
        self.assertIsNone(run_desk.plan_limit("codex", b"not json\n\xff\n", True))

    def test_a_claude_plan_limit_is_labelled_and_never_offered_a_bump(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        with self.fake_output(claude_out(CLAUDE_USAGE_LIMIT), 1):
            result = run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        self.assertEqual(result["cap_source"], "claude_plan")
        [summary] = self.summaries("rundesk.plan-limit")
        self.assertIn("Claude plan's own usage or rate limit, cap_source claude_plan", summary)
        self.assertIn("not a fleet cap: castle desk cap does not lift it", summary)
        self.assertNotIn("--runs", summary)
        hits = capacity.list_cap_hits(self.conn, "hermione")
        self.assertEqual([(hit["cap"], hit["cap_source"], hit["run_id"]) for hit in hits],
                         [("plan", "claude_plan", result["run_id"])])
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "hermione")], [owl_id])

    def test_a_codex_plan_limit_skips_the_generic_failure_event(self):
        self.enable("moody")
        owl_id, _ = self.request("moody")
        with self.fake_output(codex_out(CODEX_USAGE_LIMIT), 1), mock.patch("time.time", return_value=NOW):
            code, out, err = self.main("moody", "--owl", owl_id)
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["cap_source"], "codex_plan")
        self.assertEqual(self.events_of("rundesk.failed"), [])
        [summary] = self.summaries("rundesk.plan-limit")
        self.assertIn("Codex plan's own usage or rate limit, cap_source codex_plan", summary)

    def test_an_ordinary_failure_keeps_the_failure_event(self):
        self.enable("moody")
        owl_id, _ = self.request("moody")
        with self.fake_output(codex_out(CODEX_CRASH), 1), mock.patch("time.time", return_value=NOW):
            code, out, _ = self.main("moody", "--owl", owl_id)
        self.assertEqual((code, json.loads(out)["cap_source"]), (1, None))
        self.assertEqual(len(self.events_of("rundesk.failed")), 1)
        self.assertEqual(self.events_of("rundesk.plan-limit"), [])

    def test_the_patterns_are_valid_regular_expressions(self):
        for pattern in config.CLAUDE_PLAN_LIMIT_PATTERNS + config.CODEX_PLAN_LIMIT_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(re.compile(pattern))


class PlanLimitFalsePositiveTests(CapCase):
    def test_a_codex_run_that_exits_0_is_never_a_plan_limit(self):
        self.assertIsNone(run_desk.plan_limit("codex", codex_out(CODEX_RETRIED), False))
        self.assertIsNone(run_desk.plan_limit("codex", codex_out(CODEX_USAGE_LIMIT), False))

    def test_a_codex_error_the_run_recovered_from_is_not_what_stopped_it(self):
        self.assertIsNone(run_desk.plan_limit("codex", codex_out(CODEX_RETRIED_THEN_CRASH), True))
        recovered = CODEX_RETRIED + [{"type": "error", "message": "connection closed"}]
        self.assertIsNone(run_desk.plan_limit("codex", codex_out(recovered), True))
        stopped = [{"type": "error", "message": "You've hit your usage limit. Try again later."}]
        self.assertEqual(run_desk.plan_limit("codex", codex_out(stopped), True), "codex_plan")

    def test_a_retried_codex_review_is_acked_and_reported_clean(self):
        self.enable("moody")
        owl_id, _ = self.request("moody")
        with self.fake_output(codex_out(CODEX_RETRIED), 0), mock.patch("time.time", return_value=NOW):
            code, out, err = self.main("moody", "--owl", owl_id)
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)["ok"], json.loads(out)["cap_source"]), (True, None))
        self.assertEqual(self.events_of("rundesk.plan-limit"), [])
        self.assertEqual(capacity.list_cap_hits(self.conn, "moody"), [])
        self.assertEqual(owlery.inbox(self.conn, "moody"), [])

    def test_a_claude_limit_that_is_not_the_plan_is_not_labelled(self):
        self.assertIsNone(run_desk.plan_limit("claude", claude_out(CLAUDE_CONTEXT), True))
        self.assertEqual(run_desk.plan_limit("claude", claude_out(CLAUDE_SESSION), True), "claude_plan")

    def test_stream_json_output_reads_its_result_event(self):
        limited = stream_out([CLAUDE_INIT, CLAUDE_TALK, CLAUDE_USAGE_LIMIT])
        self.assertGreater(len(limited), run_desk.ERROR_TEXT_MAX)
        self.assertEqual(run_desk.plan_limit("claude", limited, True), "claude_plan")
        self.assertEqual(run_desk.plan_limit("claude", limited, False), "claude_plan")
        self.assertIsNone(run_desk.plan_limit("claude", stream_out([CLAUDE_INIT, CLAUDE_TALK, CLAUDE_CRASH]), True))
        self.assertIsNone(run_desk.plan_limit("claude", stream_out([CLAUDE_INIT, CLAUDE_TALK, CLAUDE_OK]), False))
        cut = stream_out([CLAUDE_INIT, CLAUDE_TALK])[:-200]  # killed before any result event
        self.assertIsNone(run_desk.plan_limit("claude", cut, True))
        self.assertIsNone(run_desk.plan_limit("claude", stream_out([CLAUDE_INIT, CLAUDE_TALK]), True))


class LocalCapDayTests(CapCase):
    def setUp(self) -> None:
        super().setUp()
        zone = mock.patch.object(capacity, "local_utc_offset", return_value=ZONE)
        zone.start()
        self.addCleanup(zone.stop)

    def test_the_cap_day_runs_from_local_midnight_to_local_midnight(self):
        status = run_desk.cap_status(self.conn, "moody", NOW)
        self.assertEqual((status["day_start"], status["resets_at"]), (LOCAL_START, LOCAL_RESET))
        self.assertEqual(status["resets_at_local"], "2027-01-16T00:00:00+10:00")
        self.assertEqual(status["resets_at_utc"], "2027-01-15T14:00:00Z")
        self.runs("moody", 80, ts=LOCAL_START - 1)
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", NOW))
        self.runs("moody", 80, ts=LOCAL_START)
        self.assertEqual(run_desk.over_daily_cap(self.conn, "moody", NOW), "daily run cap reached")
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", LOCAL_RESET))

    def test_a_bump_made_through_castle_expires_at_local_midnight(self):
        from hogwarts import cli

        self.runs("moody", 80)
        with mock.patch.object(cli, "_clock", return_value=NOW), mock.patch.object(cli, "_fleet_caps",
                                                                                    return_value=config):
            made = cli._desk_cap(self.conn, mock.Mock(desk="moody", runs=5, spend=None))
        self.assertEqual((made["bump"]["created_at"], made["bump"]["expires_at"]), (NOW, LOCAL_RESET))
        self.assertIsNone(run_desk.over_daily_cap(self.conn, "moody", LOCAL_RESET - 1))
        self.runs("moody", 80, ts=LOCAL_RESET)
        after = run_desk.cap_status(self.conn, "moody", LOCAL_RESET + 1)
        self.assertEqual((after["runs_bump"], after["reached"]), (0, "runs"))

    def test_cap_events_dedupe_by_the_local_day(self):
        self.runs("ron", 120)
        run_desk.report_cap(self.conn, "ron", NOW)
        run_desk.report_cap(self.conn, "ron", LOCAL_RESET - 1)
        [summary] = self.summaries("rundesk.cap")
        self.assertIn("resets at 2027-01-16T00:00:00+10:00", summary)
        self.runs("ron", 120, ts=LOCAL_RESET)
        run_desk.report_cap(self.conn, "ron", LOCAL_RESET + 1)
        self.assertEqual(len(self.events_of("rundesk.cap")), 2)
        self.runs("moody", 64)
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", NOW), ["runs"])
        self.assertEqual(run_desk.warn_near_cap(self.conn, "moody", LOCAL_RESET - 1), [])

    def test_a_daylight_saving_day_is_23_or_25_hours(self):
        spring = LOCAL_START + 2 * 3600  # clocks go forward an hour at 02:00 local

        def zone(ts):
            return ZONE if ts < spring else ZONE + 3600

        with mock.patch.object(capacity, "local_utc_offset", side_effect=zone):
            start, end = capacity.day_bounds(NOW, None)
            self.assertEqual((start, end - start), (LOCAL_START, DAY - 3600))
            self.assertEqual(capacity.day_bounds(LOCAL_START - 1, None), (LOCAL_START - DAY, LOCAL_START))
        autumn = LOCAL_START + 2 * 3600  # clocks go back an hour at 02:00 local

        def back(ts):
            return ZONE if ts < autumn else ZONE - 3600

        with mock.patch.object(capacity, "local_utc_offset", side_effect=back):
            start, end = capacity.day_bounds(NOW, None)
            self.assertEqual((start, end - start), (LOCAL_START, DAY + 3600))
