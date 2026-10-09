"""Model failover: classing failed runs from fake CLI output, the breaker's timing, family-only fallback, the
whole-family wait, auth, the single down and up events, the retry from the checkpoint, the opt-in flip, and the patrol
and portrait callers never taking a run its CLI marked failed."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from unittest import mock

from hogwarts import capacity, owlery, wands
from tests.support import NOW

from fleet import config, failover, ollivander, owl_post, patrol, run_desk
from tests_fleet import test_ollivander as olli
from tests_fleet import test_patrol as tp
from tests_fleet import test_portrait_auto as tpa
from tests_fleet import test_run_desk as rd
from tests_fleet.support import CODEX_PROFILE, fake_children

LADDER = {"claude": [{"model": "opus", "effort": "high", "line": "frontier"},
                     {"model": "sonnet", "effort": "high", "line": "workhorse"},
                     {"model": "haiku", "effort": "high", "line": "fast"}],
          "codex": [{"model": "gpt-6-astra", "effort": "high", "line": "frontier"}]}
DOWN = config.FAILOVER_DOWN_SECONDS


def lines(events: list) -> bytes:
    return b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)


def claude_failed(status=None, error=None, text="API Error") -> bytes:
    """A claude -p stream that ended in an error, with the CLI's own status or assistant error field."""
    events = [{"type": "system", "subtype": "init"}]
    if error is not None:
        events.append({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": text}]},
                       "error": error})
    result = {"type": "result", "subtype": "success", "is_error": True, "result": text}
    if status is not None:
        result["api_error_status"] = status
    return lines(events + [result])


CLAUDE_OK = lines([{"type": "result", "subtype": "success", "is_error": False, "result": "done",
                     "total_cost_usd": 0.1}])


def codex_failed(message: str, **fields) -> bytes:
    return lines([{"type": "thread.started"}, {"type": "turn.started"},
                  {"type": "turn.failed", "error": {"message": message, **fields}}])


class ClassifyTests(rd.RunDeskCase):
    def test_each_error_class_comes_from_the_clis_structured_output(self):
        for family, raw, code, expected in (
            ("claude", claude_failed(529), 1, "overload"),
            ("claude", claude_failed(429), 1, "rate_limit"),
            ("claude", claude_failed(503), 1, "outage"),
            ("claude", claude_failed(500), 1, "outage"),
            ("claude", claude_failed(401), 1, "auth"),
            ("claude", claude_failed(error="authentication_failed"), 1, "auth"),
            ("claude", claude_failed(error="billing_error"), 1, "auth"),
            ("claude", claude_failed(error="rate_limit"), 1, "rate_limit"),
            ("claude", claude_failed(error="server_error"), 1, "outage"),
            ("codex", codex_failed("unexpected status 503 Service Unavailable: try again"), 1, "outage"),
            ("codex", codex_failed("exceeded retry limit, last status: 429 Too Many Requests"), 1, "rate_limit"),
            ("codex", codex_failed("upstream said no", status=529), 1, "overload"),
            ("codex", codex_failed("unexpected status 401 Unauthorized"), 1, "auth"),
            ("codex", lines([{"type": "error", "message": "stream error: status: 502"}]), 1, "outage"),
        ):
            with self.subTest(family=family, raw=raw[-90:]):
                result = run_desk.claude_result(raw) if family == "claude" else None
                self.assertEqual(failover.classify(family, raw, code, result), expected)

    def test_model_text_a_clean_run_or_a_killed_run_is_never_classed(self):
        # The model saying 429 or 503 in its own words is not the CLI's error.
        words = lines([{"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": "The API returned 429 Too Many Requests and 503 Service Unavailable"}]}},
            {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "429 rate limit"}])
        codex_words = lines([{"type": "item.completed", "item": {"type": "agent_message",
                                                                  "text": "status: 503 Service Unavailable"}},
                             {"type": "turn.failed", "error": {"message": "the sandbox refused a command"}}])
        recovered = lines([{"type": "error", "message": "unexpected status 503 Service Unavailable"},
                           {"type": "turn.completed", "usage": {}}])
        for family, raw, code in (("claude", words, 1), ("codex", codex_words, 1), ("codex", recovered, 1),
                                  ("claude", claude_failed(529), -1), ("claude", CLAUDE_OK, 0),
                                  ("codex", codex_failed("unexpected status 503 Service Unavailable"), 0),
                                  ("claude", lines([{"type": "result", "is_error": False, "api_error_status": 529}]),
                                   0)):
            with self.subTest(family=family, raw=raw[-80:], code=code):
                result = run_desk.claude_result(raw) if family == "claude" else None
                self.assertIsNone(failover.classify(family, raw, code, result))


class FailoverCase(rd.RunDeskCase):
    def setUp(self) -> None:
        super().setUp()
        ron = {"claude": [{"model": "haiku", "effort": "low", "line": "fast"}]}
        failover.write_ladders({"hermione": LADDER, "ron": ron}, NOW)

    def trip(self, family: str, model: str, at: int = NOW - 10) -> None:
        for _ in range(config.FAILOVER_TRIP_FAILURES):
            failover.record(self.conn, family, model, "overload", False, at)

    def plan(self, desk: str = "hermione", model: str = "opus", family: str = "claude", **extra) -> dict:
        return {"desk": desk, "desk_family": family, "model": model, "review_round": None, **extra}

    def kinds(self, prefix: str = "failover.") -> list:
        return [(event["kind"], event["desk"], event["verdict"]) for event in self.events()
                if event["kind"].startswith(prefix)]

    def fake(self, outputs: dict, codes: dict = None):
        """A desk process whose output and exit code depend on the model in its argv."""
        def run(argv, **kwargs):
            model = self.argv_model(argv)
            os.write(kwargs["stdout"], outputs[model])
            return subprocess.CompletedProcess(args=argv, returncode=(codes or {}).get(model, 0))
        return fake_children(run)

    @staticmethod
    def argv_model(argv: list) -> str:
        if "--model" in argv:
            return argv[argv.index("--model") + 1]
        return next(arg.split("=", 1)[1].strip('"') for arg in argv if arg.startswith("model="))


class BreakerTests(FailoverCase):
    def test_two_failures_in_a_row_open_it_and_it_half_opens_after_fifteen_minutes(self):
        failover.record(self.conn, "claude", "opus", "outage", False, NOW)
        failover.record(self.conn, "claude", "opus", None, True, NOW + 1)  # a clean run in between resets the count
        failover.record(self.conn, "claude", "opus", "outage", False, NOW + 2)
        self.assertFalse(failover.is_down(failover.read_state(), "claude", "opus", NOW + 2))
        self.assertIsNone(failover.choose(self.conn, self.plan(), NOW + 2, claim=True))
        failover.record(self.conn, "claude", "opus", "rate_limit", False, NOW + 3)
        state = failover.read_state()
        self.assertTrue(failover.is_down(state, "claude", "opus", NOW + 3 + DOWN - 1))
        self.assertFalse(failover.is_down(state, "claude", "opus", NOW + 3 + DOWN))
        self.assertEqual(failover.choose(self.conn, self.plan(), NOW + 3 + DOWN - 1)["model"], "sonnet")
        # Half-open: the first launch to claim it is the one probe, and every other one falls back meanwhile.
        self.assertIsNone(failover.choose(self.conn, self.plan(), NOW + 3 + DOWN, claim=True))
        self.assertEqual(failover.choose(self.conn, self.plan(), NOW + 3 + DOWN, claim=True)["model"], "sonnet")
        self.assertEqual(failover.choose(self.conn, self.plan(), NOW + 3 + 2 * DOWN - 1, claim=True)["model"],
                         "sonnet")
        # The probe failed: open again for another window, still one down event.
        failover.record(self.conn, "claude", "opus", "overload", False, NOW + 3 + DOWN + 5)
        self.assertTrue(failover.is_down(failover.read_state(), "claude", "opus", NOW + 3 + 2 * DOWN + 4))
        self.assertEqual(self.kinds(), [("failover.down", "ollivander", "headmaster")])
        # A clean probe closes it, with one up event.
        failover.record(self.conn, "claude", "opus", None, True, NOW + 3 + 2 * DOWN + 10)
        self.assertEqual(failover.read_state()["models"], {})
        failover.record(self.conn, "claude", "opus", None, True, NOW + 3 + 2 * DOWN + 11)
        self.assertEqual(self.kinds(), [("failover.down", "ollivander", "headmaster"),
                                        ("failover.up", "ollivander", "headmaster")])

    def test_only_outage_overload_and_rate_limit_count(self):
        for failure in ("auth", None, None, "auth"):
            failover.record(self.conn, "claude", "opus", failure, False, NOW)
        self.assertEqual(failover.read_state()["models"], {})
        for failure in ("outage", "overload"):
            failover.record(self.conn, "codex", "gpt-6-astra", failure, False, NOW)
        self.assertTrue(failover.is_down(failover.read_state(), "codex", "gpt-6-astra", NOW))
        self.assertFalse(failover.is_down(failover.read_state(), "claude", "gpt-6-astra", NOW))

    def test_the_owner_hears_once_when_a_model_goes_down_and_once_when_it_is_back(self):
        for at in range(6):
            failover.record(self.conn, "claude", "opus", "overload", False, NOW + at)
        [down] = [event for event in self.events() if event["kind"] == "failover.down"]
        self.assertIn("opus (claude) is down", down["summary"])
        for at in range(3):
            failover.record(self.conn, "claude", "opus", None, True, NOW + DOWN * 3 + at)
        [up] = [event for event in self.events() if event["kind"] == "failover.up"]
        self.assertIn("opus (claude) is back", up["summary"])
        self.assertEqual(len(self.kinds()), 2)
        # Down again later is news again.
        self.trip("claude", "opus", NOW + DOWN * 5)
        self.assertEqual([kind for kind, _, _ in self.kinds()], ["failover.down", "failover.up", "failover.down"])

    def test_an_event_the_store_refused_is_told_on_the_next_update(self):
        with mock.patch.object(failover.pensieve, "add_event", side_effect=failover.StoreError("busy")):
            self.trip("claude", "opus")
        self.assertEqual(self.kinds(), [])
        self.assertEqual(len(failover.read_state()["notices"]), 1)
        failover.record(self.conn, "claude", "opus", "overload", False, NOW)
        self.assertEqual(self.kinds(), [("failover.down", "ollivander", "headmaster")])
        self.assertEqual(failover.read_state()["notices"], [])

    def test_only_a_proven_clean_run_closes_it(self):
        ok = run_desk.claude_result(CLAUDE_OK)
        self.assertTrue(failover.clean("claude", CLAUDE_OK, 0, True, ok))
        self.assertFalse(failover.clean("claude", CLAUDE_OK, 0, False, ok))  # output not read whole
        self.assertFalse(failover.clean("claude", b"", 0, True, {}))  # no result event
        failed = run_desk.claude_result(claude_failed())
        self.assertFalse(failover.clean("claude", claude_failed(), 0, True, failed))  # is_error, unclassed
        self.assertFalse(failover.clean("codex", b"", 0, True))
        self.assertTrue(failover.clean("codex", lines([{"type": "turn.completed", "usage": {}}]), 0, True))
        self.trip("claude", "opus")
        failover.record(self.conn, "claude", "opus", None, False, NOW)
        self.assertTrue(failover.is_down(failover.read_state(), "claude", "opus", NOW))

    def test_a_probe_holds_the_model_for_as_long_as_a_run_can_last(self):
        self.trip("claude", "opus", NOW - DOWN)
        self.assertIsNone(failover.choose(self.conn, self.plan(), NOW, claim=True))
        held = max(DOWN, config.RUNNING_WINDOW_SECONDS)
        failover.record(self.conn, "claude", "opus", "outage", False, NOW + 1)  # an older run's late failure
        self.assertEqual(failover.choose(self.conn, self.plan(), NOW + held - 1, claim=True)["model"], "sonnet")
        self.assertIsNone(failover.choose(self.conn, self.plan(), NOW + held, claim=True))

    def test_an_unreadable_state_is_nothing_down(self):
        self.write_file(self.office / "state" / config.FAILOVER_STATE_FILE, "{not json")
        self.assertEqual(failover.read_state(), failover._empty())
        self.assertIsNone(failover.choose(self.conn, self.plan(), NOW, claim=True))


class FallbackTests(FailoverCase):
    def run_hermione(self, outputs: dict = None, codes: dict = None) -> tuple:
        self.enable("hermione")
        owl_id, task_id = self.request("hermione")
        with self.fake(outputs or {"opus": CLAUDE_OK, "sonnet": CLAUDE_OK, "haiku": CLAUDE_OK}, codes) as started:
            result = run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        return result, started, task_id

    def test_a_down_model_falls_back_to_the_next_pick_in_its_family(self):
        self.trip("claude", "opus")
        result, started, task_id = self.run_hermione()
        self.assertEqual(self.argv_model(started.call_args.args[0]), "sonnet")
        self.assertEqual(started.call_args.args[0][0], config.CLAUDE_BIN)
        self.assertEqual((result["model"], result["family"], result["failover_from"], result["exit_code"]),
                         ("sonnet", "claude", "opus", 0))
        # The run record names the model that really ran.
        [launch] = capacity.list_launches(self.conn, "hermione")
        self.assertEqual((launch["model"], launch["task_id"]), ("sonnet", task_id))
        self.assertEqual([item["run"] for item in failover.read_state()["runs"]], [result["run_id"]])
        self.assertEqual(self.events_of("ollivander.moved"), [])

    def test_a_blocked_or_down_fallback_is_skipped_and_the_family_never_changes(self):
        self.trip("claude", "opus")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-")):
            result, started, _ = self.run_hermione()
        self.assertEqual(result["model"], "haiku")
        self.trip("claude", "haiku")
        with self.assertRaises(failover.ModelsDown):  # sonnet is up, but blocked: Codex is never tried
            with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-")):
                self.run_hermione()

    def test_a_run_on_its_own_model_comes_back_with_no_move_and_closes_the_breaker(self):
        self.trip("claude", "opus", NOW - DOWN - 10)
        result, started, _ = self.run_hermione()
        self.assertEqual((result["model"], result["failover_from"]), ("opus", None))  # its probe
        self.assertEqual(self.kinds(), [("failover.down", "ollivander", "headmaster"),
                                        ("failover.up", "ollivander", "headmaster")])

    def test_a_fallback_is_never_dearer_than_the_model_the_desk_is_on_now(self):
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW - 100)
        self.trip("claude", "sonnet")
        result, _, _ = self.run_hermione()
        self.assertEqual(result["model"], "haiku")  # opus heads the ladder, but costs more than sonnet
        self.trip("claude", "haiku")
        with self.assertRaises(failover.ModelsDown):
            self.run_hermione()

    def test_a_fallback_run_never_counts_toward_a_trial(self):
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW - 100)
        self.trip("claude", "opus")
        self.run_hermione(codes={"sonnet": 1})
        self.assertEqual(wands.get_desk_model(self.conn, "hermione")["trial_failures"], 0)


class WaitTests(FailoverCase):
    def test_a_whole_family_down_waits_with_one_event_and_starts_no_process(self):
        self.enable("hermione")
        for model in ("opus", "sonnet", "haiku"):
            self.trip("claude", model)
        for _ in range(2):
            owl_id, _ = self.request("hermione")
            with self.assertRaises(failover.ModelsDown):
                run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        [wait] = self.events_of("failover.wait")
        self.assertEqual((wait["desk"], wait["verdict"]), ("hermione", "headmaster"))
        self.assertIn("every model of its claude family it may run is down", wait["summary"])
        self.assertEqual(capacity.list_launches(self.conn, "hermione"), [])

    def test_a_waiting_owl_runs_again_once_a_model_is_back(self):
        now = int(time.time())
        self.enable("hermione")
        for model in ("opus", "sonnet", "haiku"):
            self.trip("claude", model, now)
        owl_id, _ = self.request("hermione")
        code, _, err = self.main("hermione", "--owl", owl_id)
        self.assertEqual(code, 1)
        self.assertIn("waits", err)
        self.assertEqual(self.events_of("rundesk.failed"), [])
        self.assertEqual(list(failover.read_state()["waiting"]), [owl_id])
        with mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=now + 60)["resumed"], [])
            self.assertEqual(owl_post.run_pass(self.conn, now=now + DOWN)["resumed"], [owl_id])
            self.assertEqual(owl_post.run_pass(self.conn, now=now + DOWN + 60)["resumed"], [])
        spawn.assert_called_once_with("hermione", owl_id)
        self.assertEqual(failover.read_state()["waiting"][owl_id]["resumed"], now + DOWN)

    def test_an_owl_still_waiting_after_the_limit_is_dropped_with_one_event(self):
        owl_id, _ = self.request("hermione")
        down = failover.ModelsDown("down", "k", "claude", (("claude", "opus"),))
        self.trip("claude", "opus", NOW)
        failover.note_waiting("hermione", owl_id, down, NOW)
        failover.note_waiting("hermione", owl_id, down, NOW + 600)  # waiting again keeps its first wait
        spawn = mock.Mock()
        limit = config.FAILOVER_WAIT_LIMIT_SECONDS
        self.trip("claude", "opus", NOW + limit - 10)
        self.assertEqual(failover.resume_waiting(self.conn, spawn, NOW + limit - 1), [])
        self.assertEqual(failover.resume_waiting(self.conn, spawn, NOW + limit), [])
        spawn.assert_not_called()
        self.assertEqual(failover.read_state()["waiting"], {})
        [ended] = self.events_of("failover.wait-ended")
        self.assertEqual((ended["desk"], ended["verdict"]), ("hermione", "headmaster"))

    def test_a_resume_that_could_not_start_waits_again(self):
        owl_id, _ = self.request("hermione")
        failover.note_waiting("hermione", owl_id, failover.ModelsDown("down", "k", "claude", (("claude", "opus"),)),
                              NOW)
        spawn = mock.Mock(side_effect=run_desk.FleetError("hermione is not enabled"))
        self.assertEqual(failover.resume_waiting(self.conn, spawn, NOW), [])
        self.assertIsNone(failover.read_state()["waiting"][owl_id]["resumed"])
        self.assertEqual(failover.resume_waiting(self.conn, mock.Mock(), NOW + 1), [owl_id])

    def test_a_shadow_patrol_run_keeps_its_wait_note_out_of_the_events(self):
        self.enable("hermione")
        for model in ("opus", "sonnet", "haiku"):
            self.trip("claude", model)
        owl_id, _ = self.request("hermione")
        with self.assertRaises(failover.ModelsDown) as caught:
            run_desk.run(self.conn, "hermione", owl_id, now=NOW, shadow=True)
        self.assertIn("waits", str(caught.exception))  # its reason, as a cap refusal's is the Capped error
        self.assertEqual(self.events_of("failover.wait"), [])
        # A breaker update from a shadow run leaves its event to the next flush, the Owl Post's at the latest.
        failover.record(self.conn, "claude", "gpt-x", "outage", False, NOW, tell=False)
        failover.record(self.conn, "claude", "gpt-x", "outage", False, NOW, tell=False)
        def told() -> list:
            return [event for event in self.events_of("failover.down") if "gpt-x" in event["summary"]]

        self.assertEqual(told(), [])
        failover.resume_waiting(self.conn, mock.Mock(), NOW)
        self.assertEqual(len(told()), 1)

    def test_a_refused_expiry_event_keeps_the_owl_until_it_is_told(self):
        owl_id, _ = self.request("hermione")
        failover.note_waiting("hermione", owl_id, failover.ModelsDown("down", "k", "claude", (("claude", "opus"),)),
                              NOW)
        self.trip("claude", "opus", NOW + config.FAILOVER_WAIT_LIMIT_SECONDS)
        with mock.patch.object(failover.pensieve, "add_event", side_effect=failover.StoreError("busy")):
            failover.resume_waiting(self.conn, mock.Mock(), NOW + config.FAILOVER_WAIT_LIMIT_SECONDS)
        self.assertIn(owl_id, failover.read_state()["waiting"])
        failover.resume_waiting(self.conn, mock.Mock(), NOW + config.FAILOVER_WAIT_LIMIT_SECONDS + 1)
        self.assertEqual(failover.read_state()["waiting"], {})
        self.assertEqual(len(self.events_of("failover.wait-ended")), 1)

    def test_a_started_owl_that_never_reached_a_model_is_started_again_a_bounded_number_of_times(self):
        owl_id, _ = self.request("hermione")
        failover.note_waiting("hermione", owl_id, failover.ModelsDown("down", "k", "claude", (("claude", "opus"),)),
                              NOW)
        spawn = mock.Mock()
        window = config.RUNNING_WINDOW_SECONDS
        started = [failover.resume_waiting(self.conn, spawn, NOW + step) for step in (0, 60, window, 2 * window,
                                                                                     3 * window, 4 * window)]
        self.assertEqual(started, [[owl_id], [], [owl_id], [owl_id], [], []])
        self.assertEqual(spawn.call_count, config.FAILOVER_MAX_RESUMES)

    def test_an_acked_waiting_owl_is_dropped(self):
        failover.note_waiting("hermione", "owl_0000000000000001",
                              failover.ModelsDown("down", "k", "claude", (("claude", "opus"),)), NOW)
        spawn = mock.Mock()
        self.assertEqual(failover.resume_waiting(self.conn, spawn, NOW), [])
        spawn.assert_not_called()
        self.assertEqual(failover.read_state()["waiting"], {})


class RetryAndAuthTests(FailoverCase):
    def test_a_run_an_outage_cut_off_goes_again_and_fails_over_once_the_model_is_down(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        with self.fake({"opus": claude_failed(529), "sonnet": CLAUDE_OK}, {"opus": 1}) as started:
            code, out, err = self.main("hermione", "--owl", owl_id)
        self.assertEqual(code, 0, err)
        self.assertEqual([self.argv_model(call.args[0]) for call in started.call_args_list], ["opus", "opus", "sonnet"])
        self.assertEqual(json.loads(out)["failover_from"], "opus")
        self.assertEqual(owlery.inbox(self.conn, "hermione"), [])
        self.assertEqual([kind for kind, _, _ in self.kinds()], ["failover.down"])
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_retries_stop_at_their_cap(self):
        self.enable("ron")
        owl_id, _ = self.request("ron")
        with self.fake({"haiku": claude_failed(503)}, {"haiku": 1}) as started:
            code, _, _ = self.main("ron", "--owl", owl_id)
        self.assertEqual(code, 1)
        # Its own model twice, which trips it, then the wait: ron's only fast model is down.
        self.assertEqual(started.call_count, config.FAILOVER_TRIP_FAILURES)
        self.assertEqual(len(self.events_of("failover.wait")), 1)
        self.assertEqual(list(failover.read_state()["waiting"]), [owl_id])

    def test_a_structured_error_with_exit_zero_is_not_a_clean_run(self):
        self.enable("ron")
        owl_id, _ = self.request("ron")
        with self.fake({"haiku": claude_failed(529)}) as started:
            code, out, _ = self.main("ron", "--owl", owl_id)
        self.assertEqual((code, started.call_count), (1, config.FAILOVER_TRIP_FAILURES))
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "ron")], [owl_id])

    def test_an_auth_failure_tells_the_owner_and_never_fails_over(self):
        self.enable("hermione")
        owl_id, _ = self.request("hermione")
        with self.fake({"opus": claude_failed(401)}, {"opus": 1}) as started:
            code, _, _ = self.main("hermione", "--owl", owl_id)
        self.assertEqual((code, started.call_count), (1, 1))
        [auth] = self.events_of("failover.auth")
        self.assertEqual((auth["desk"], auth["verdict"]), ("hermione", "headmaster"))
        self.assertEqual(len(self.events_of("rundesk.failed")), 1)
        self.assertEqual(failover.read_state()["models"], {})


class CrossFamilyTests(FailoverCase):
    def setUp(self) -> None:
        super().setUp()
        for model in ("opus", "sonnet", "haiku"):
            self.trip("claude", model)
        for desk in ("ron", "hermione"):
            self.enable(desk)
            self.write_file(self.office / "desks" / desk / "codex.toml", CODEX_PROFILE)
        failover.write_ladders({"ron": {"codex": [{"model": "gpt-6-luna", "effort": "low", "line": "fast"}]},
                                "hermione": LADDER}, NOW)

    def switch_on(self) -> None:
        self.write_file(self.office / config.CROSS_FAMILY_FAILOVER_FILE, "on\n")

    def test_the_flip_is_off_by_default_and_needs_launch_settings(self):
        owl_id, _ = self.request("ron")
        with mock.patch.dict(config.CODEX_ACCESS, {"ron": "read"}), self.assertRaises(failover.ModelsDown):
            run_desk.run(self.conn, "ron", owl_id, now=NOW)
        self.switch_on()
        with self.assertRaises(failover.ModelsDown):  # on, but ron has no Codex launch settings
            run_desk.run(self.conn, "ron", owl_id, now=NOW)

    def test_a_reviewer_or_a_build_desk_never_flips(self):
        self.switch_on()
        self.assertFalse(failover.can_flip("harry", "codex"))
        owl_id, _ = self.request("hermione")
        with mock.patch.dict(config.CODEX_ACCESS, {"hermione": "read"}), self.assertRaises(failover.ModelsDown):
            run_desk.run(self.conn, "hermione", owl_id, now=NOW)
        with mock.patch.dict(config.CLAUDE_TOOLS, {"harry": "Read"}), \
                mock.patch.dict(config.MAX_BUDGET_USD, {"harry": "1.00"}), \
                mock.patch.dict(config.CLAUDE_READ_DIRS, {"harry": ()}):
            self.assertFalse(failover.can_flip("harry", "codex"))

    def test_with_the_switch_on_a_desk_flips_and_no_review_of_it_is_same_family(self):
        self.switch_on()
        with mock.patch.dict(config.CODEX_ACCESS, {"ron": "read"}):
            owl_id, task_id = self.request("ron")
            with self.fake({"gpt-6-luna": lines([{"type": "turn.completed", "usage": {}}])}) as started:
                result = run_desk.run(self.conn, "ron", owl_id, now=NOW)
            argv = started.call_args.args[0]
            self.assertEqual((argv[0], self.argv_model(argv)), (config.CODEX_BIN, "gpt-6-luna"))
            self.assertEqual((result["family"], result["failover_from"], result["exit_code"]), ("codex", "haiku", 0))
            # Moody reviews Claude work. The author's run was Codex, or a model the store cannot place: refused.
            review = self.plan("moody", "gpt-6-astra", "codex", review_round={"task_id": task_id, "slot": 0})
            with self.assertRaises(failover.ModelsDown) as caught:
                failover.choose(self.conn, review, NOW)
            self.assertIn("would not be cross-family", str(caught.exception))
            wands.record_catalog(self.conn, "codex", ["gpt-6-luna"], now=NOW)
            with self.assertRaises(failover.ModelsDown):
                failover.choose(self.conn, review, NOW)
            # Once the author ran on its own family again, the review goes ahead.
            capacity.record_launch(self.conn, "ron", "run-0000000000000002", "haiku", task_id=task_id, now=NOW + 1)
            self.assertIsNone(failover.choose(self.conn, review, NOW + 2))
        # Without the flip, the review guard reads the same launch rows and lets a normal review through.
        self.assertIsNone(failover.choose(self.conn, review, NOW + 2))


class LadderTests(olli.OllivanderCase):
    def ladders(self) -> dict:
        return json.loads((self.office / "state" / config.FAILOVER_LADDERS_FILE).read_text())["desks"]

    def names(self, desk: str, family: str) -> list:
        return [entry["model"] for entry in self.ladders()[desk][family]]

    def test_each_pass_keeps_the_picks_in_order_and_never_a_dearer_need(self):
        self.keeper(dry_run=True)
        self.assertFalse((self.office / "state" / config.FAILOVER_LADDERS_FILE).exists())
        self.keeper()
        self.assertEqual(self.names("hermione", "claude"), ["opus", "sonnet", "haiku"])
        self.assertEqual(self.names("moody", "codex"), ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(self.names("harry", "codex"), ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(self.names("ron", "claude"), ["haiku"])
        self.assertEqual({entry["effort"] for entry in self.ladders()["ron"]["codex"]}, {"low"})
        self.assertEqual(self.ladders()["moody"]["codex"][0], {"model": "gpt-6-astra", "effort": "high",
                                                               "line": "frontier"})
        # Ollivander's own pick heads the ladder.
        self.assertEqual(self.model("moody")["model"], self.names("moody", "codex")[0])

    def test_a_dearer_pick_waiting_for_approval_is_never_a_fallback(self):
        wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "role", now=olli.OCT4 - 60)
        self.keeper()
        self.assertEqual(self.model("harry")["pending_model"], "gpt-6.1-sol")
        self.assertEqual(self.names("harry", "codex"), ["gpt-6-luna"])

    def test_a_pinned_desk_has_no_fallback_dearer_than_its_pin(self):
        self.keeper()
        code, _ = self.castle_cli("desk", "model", "hermione", "sonnet")
        self.assertEqual(code, 0)
        self.keeper(now=olli.OCT4 + 60)
        self.assertEqual(self.names("hermione", "claude"), ["sonnet", "haiku"])

    def test_blocked_names_are_left_out_and_an_unread_catalog_keeps_the_last_ladder(self):
        self.keeper()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-", "gpt-6.1-")):
            self.keeper(now=olli.OCT4 + 60, runner=olli.FakeRunner(codes={("debug", "models"): 1}))
        self.assertEqual(self.names("hermione", "claude"), ["opus", "haiku"])
        self.assertEqual(self.names("harry", "codex"), ["gpt-6.1-sol", "gpt-6-luna"])  # the last good look
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-", "gpt-6.1-")):
            self.keeper(now=olli.OCT4 + 120)
        self.assertEqual(self.names("harry", "codex"), ["gpt-6-luna"])

    def test_a_pick_order_matches_ollivanders_pick(self):
        codex = {"ok": True, "error": None, "models": ollivander.parse_catalog(olli.catalog())}
        for need in ("frontier", "workhorse", "fast"):
            with self.subTest(need=need):
                self.assertEqual(ollivander.codex_order(need, codex, {}, olli.OCT4)[0]["slug"],
                                 ollivander.pick_codex(need, codex, {}, olli.OCT4)["model"])


# An exit 0 whose CLI said a vendor error cut it off: never a clean run, wherever a caller reads it.
CLAUDE_529 = {"type": "result", "subtype": "success", "is_error": True, "result": "API Error", "api_error_status": 529}


class PatrolFailureTests(tp.PatrolCase):
    def desk_says(self, result: dict, returncode: int = 0):
        """Ron writing his file, then his CLI ending with result."""
        def run(argv, cwd, env, input, stdout, stderr, timeout, check, pass_fds=()):
            owl = re.search(r"Owl (owl_[0-9a-f]{16}) was delivered", input.decode()).group(1)
            self.write_file(self.outbox("ron") / f"{owl}-{patrol.REPORT_SUFFIX['ron']}.md", tp.RON_WORDS)
            os.write(stdout, json.dumps(result).encode("utf-8"))
            return subprocess.CompletedProcess(args=argv, returncode=returncode)
        return fake_children(run)

    def wake_ron(self, result: dict, returncode: int = 0) -> dict:
        self.github.prs = [tp.pr_node()]
        self.round(NOW - 900)
        self.github.prs = [tp.pr_node(rollup="FAILURE", contexts=(tp.check("build"),))]
        with self.desk_says(result, returncode):
            return self.round(NOW)["woke"]

    def test_a_file_written_before_a_structured_failure_never_finishes_the_owl(self):
        woke = self.wake_ron(CLAUDE_529)
        self.assertEqual((woke["launched"], woke["clean"], woke["collected"]), (True, False, False))
        self.assertEqual(list(self.pending()), [woke["owl_id"]])
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "ron")], [woke["owl_id"]])
        self.assertTrue((self.outbox("ron") / f"{woke['owl_id']}-report.md").exists())  # never taken

    def test_an_auth_failure_stops_the_owl_until_an_explicit_restart(self):
        woke = self.wake_ron({**CLAUDE_529, "api_error_status": 401}, returncode=1)
        owl_id = woke["owl_id"]
        self.assertTrue(self.pending()[owl_id]["auth_stop"])
        with fake_children() as started:
            later = self.round(NOW + config.PATROL_RESEND_AFTER_SECONDS * 3)
        self.assertEqual((started.call_count, later["resent"]["resent"]), (0, []))
        with self.assertRaises(run_desk.FleetError):
            patrol.restart("owl_0000000000000001")
        self.assertEqual(patrol.restart(owl_id), {"restarted": owl_id, "desk": "ron", "job": "map"})
        with self.desk_says({**CLAUDE_529, "is_error": False, "api_error_status": None}) as started:
            again = self.round(NOW + config.PATROL_RESEND_AFTER_SECONDS * 4)
        self.assertEqual(started.call_count, 1)
        self.assertEqual([item["owl_id"] for item in again["resent"]["resent"]], [owl_id])
        self.assertEqual(self.pending(), {})


class PortraitFailureTests(tpa.AutoCase):
    def failed_night(self) -> dict:
        with mock.patch.object(tpa, "CLAUDE_OK", CLAUDE_529):
            return self.night(self.ops)

    def test_a_night_its_cli_marked_failed_is_never_patch_ready(self):
        result = self.failed_night()
        self.assertEqual((result["ok"], result["patch_ready"]), (False, False))
        self.assertEqual(self.events_of("portrait.patch-ready"), [])
        self.assertEqual(len(self.events_of("rundesk.failed")), 1)

    def test_auto_portrait_never_validates_or_applies_its_patch(self):
        self.opt_in()
        before = self.current_ids()
        self.failed_night()
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.current_ids(), before)
        self.assertEqual(self.row()["state"], "stopped")
        self.assertEqual(self.events_of("portrait.auto"), [])
