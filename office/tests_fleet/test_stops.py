"""Ollivander's stop reaches the owner and every desk session, and the desk runs it refused start again once it is
cleared (fleet/stops.py): one loud event per stop, the stop at the top of each session's events until it is cleared,
each refused run held once, and the Owl Post's first pass after the clear starting each one again, once, through the
normal launch, with one event saying what it started."""
from __future__ import annotations

import json
import os
from unittest import mock

from hogwarts import owlery
from tests.support import NOW

from fleet import config, ollivander, owl_post, run_desk, stops
from fleet.hooks import session_start, user_prompt_submit
from tests_fleet.test_run_desk import RunDeskCase

SESSION = "0b6f8c1e-1111-4222-8333-944455556666"
REASON = "after the CLI update these failed: codex --version"


class StopsCase(RunDeskCase):
    def stop(self, text: str = f"{NOW} {REASON}\n", name: str = config.STOP_FILE) -> None:
        self.write_file(self.office / "state" / name, text)

    def clear(self) -> None:
        for name in (config.STOP_FILE, config.UPDATING_FILE):
            path = self.office / "state" / name
            if path.exists():
                os.unlink(path)

    def held_owls(self) -> list:
        folder = self.office / "state" / config.STOP_HELD_DIR
        return sorted(name for name in os.listdir(folder) if not name.startswith(".")) if folder.exists() else []

    def events_of(self, kind: str) -> list:
        return [event for event in self.events() if event["kind"] == kind]


class StopNoticeTests(StopsCase):
    def test_the_stop_is_one_loud_event_naming_the_failed_check_and_the_exact_clear_command(self):
        self.assertIn("ollivander.stopped", config.PHONE_KINDS)
        ollivander.write_stop(REASON, NOW)
        for _ in range(2):  # a later pass that finds the same unfinished update says nothing new
            ollivander._stop_after_unfinished(self.conn, str(NOW), NOW, {})
        [event] = self.events_of("ollivander.stopped")
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn("Ollivander stopped every headless desk run, and each stays blocked until you run castle"
                      " ollivander clear in your terminal. Why: a CLI update did not finish", event["summary"])
        long_reason = "after the CLI update these failed: " + "; ".join(f"run_desk d{n} --dry-run" for n in range(40))
        self.assertIn("castle ollivander clear", ollivander.stopped_summary(long_reason))

    def test_an_active_stop_heads_the_events_of_every_desk_session_until_it_is_cleared(self):
        self.assertIsNone(stops.active_line())
        self.stop()
        line = stops.active_line()
        self.assertIn(f"Ollivander's stop is on since 2027-01-15 08:00 UTC: {REASON}.", line)
        self.assertIn("Every headless desk run is blocked until `castle ollivander clear` runs in your terminal", line)
        payload = {"session_id": SESSION, "transcript_path": "", "cwd": str(self.castle),
                   "hook_event_name": "UserPromptSubmit", "prompt": "how is it going"}
        for _ in range(2):  # every prompt, not once
            code, out, _ = self.run_hook(user_prompt_submit, payload)
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["systemMessage"].splitlines()[0], line)
            self.assertIn(line, data["hookSpecificOutput"]["additionalContext"])
        code, out, _ = self.run_hook(session_start, {**payload, "hook_event_name": "SessionStart",
                                                     "source": "startup"})
        lines = out.splitlines()
        self.assertEqual(lines.index(line) + 1, next(i for i, text in enumerate(lines)
                                                     if text.startswith("Headmaster events")))
        for source in ("resume", "fork"):
            code, out, _ = self.run_hook(session_start, {**payload, "hook_event_name": "SessionStart",
                                                         "source": source})
            self.assertEqual(out.splitlines()[1:], [line])
        self.clear()
        self.stop("1 a CLI update is running\n", config.UPDATING_FILE)
        self.assertIn("Ollivander's CLI update marker is in place since", stops.active_line())
        self.clear()
        self.assertIsNone(stops.active_line())
        code, out, _ = self.run_hook(user_prompt_submit, payload)
        self.assertNotIn("Ollivander", out)

    def test_a_store_that_cannot_be_read_never_hides_an_active_stop(self):
        payload = {"session_id": SESSION, "transcript_path": "", "cwd": str(self.castle),
                   "hook_event_name": "UserPromptSubmit", "prompt": "how is it going"}
        broken = mock.patch.object(user_prompt_submit.common, "connect", side_effect=stops.StoreError("locked"))
        with broken:
            code, _, _ = self.run_hook(user_prompt_submit, payload)
            self.assertEqual(code, 1)  # no stop: skipped as before
            self.stop()
            code, out, _ = self.run_hook(user_prompt_submit, payload)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["systemMessage"], stops.active_line())
            code, out, _ = self.run_hook(session_start, {**payload, "hook_event_name": "SessionStart",
                                                         "source": "startup"})
            self.assertEqual((code, out), (0, stops.active_line() + "\n"))

    def test_an_unreadable_stop_still_shows(self):
        os.mkdir(self.office / "state" / config.STOP_FILE)
        self.assertIn("Ollivander's stop is on: it could not be read.", stops.active_line())


class HeldRunTests(StopsCase):
    def refused(self, desk: str = "moody") -> str:
        """A request to the desk whose run the stop refused, as the Owl Post's or a go's detached run_desk."""
        self.enable(desk)
        owl_id, _ = self.request(desk)
        self.stop()
        before = len(self.events())
        for _ in range(2):  # refused twice, held once, and no event per run
            code, _, err = self.main(desk, "--owl", owl_id)
            self.assertEqual(code, 1)
            self.assertIn("Ollivander's stop file", err)
        self.assertEqual(len(self.events()), before)
        self.assertIn(owl_id, self.held_owls())
        return owl_id

    def marker(self, owl_id: str) -> dict:
        return json.loads((self.office / "state" / config.STOP_HELD_DIR / owl_id).read_text())

    def test_a_run_the_stop_refused_starts_again_once_on_the_first_pass_after_the_clear(self):
        owl_id = self.refused()
        task_id = next(owl for owl in owlery.inbox(self.conn, "moody") if owl["id"] == owl_id)["task_id"]
        with mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])  # still stopped
            spawn.assert_not_called()
            self.clear()
            summary = owl_post.run_pass(self.conn, now=NOW + 120)
            self.assertEqual(summary["restarted"], [{"desk": "moody", "owl": owl_id, "task": task_id}])
            spawn.assert_called_once_with("moody", owl_id)
            # Claimed until the run itself says it started: never launched twice while it may still be starting.
            self.assertEqual((self.marker(owl_id)["state"], self.marker(owl_id)["resumes"]), ("resumed", 1))
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 180)["restarted"], [])
            spawn.assert_called_once()
        [event] = self.events_of(stops.EVENT_KIND)
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn(f"is starting again the runs it held: moody on owl {owl_id} (task {task_id})", event["summary"])
        stops.release("moody", owl_id)  # what run_desk main does first
        self.assertEqual(self.held_owls(), [])

    def test_a_restarted_run_a_new_stop_refuses_is_held_again(self):
        owl_id = self.refused()
        self.clear()
        with mock.patch.object(run_desk, "spawn"):
            owl_post.run_pass(self.conn, now=NOW + 60)
        self.stop()
        code, _, _ = self.main("moody", "--owl", owl_id)
        self.assertEqual(code, 1)
        self.assertEqual(self.marker(owl_id)["state"], "held")

    def test_a_claim_no_run_released_is_started_again_then_given_up_and_said(self):
        owl_id = self.refused()
        self.clear()
        later = NOW
        with mock.patch.object(run_desk, "spawn") as spawn:
            for _ in range(config.STOP_RESUMES_MAX):
                later += config.RUNNING_WINDOW_SECONDS
                self.assertEqual(len(owl_post.run_pass(self.conn, now=later)["restarted"]), 1)
            later += config.RUNNING_WINDOW_SECONDS
            self.assertEqual(owl_post.run_pass(self.conn, now=later)["restarted"], [])
        self.assertEqual(spawn.call_count, config.STOP_RESUMES_MAX)
        self.assertEqual(self.held_owls(), [])
        self.assertIn(f"never started after {config.STOP_RESUMES_MAX} tries",
                      self.events_of(stops.EVENT_KIND)[-1]["summary"])

    def test_given_up_runs_stay_until_their_event_is_stored_and_a_pass_handles_a_bounded_batch(self):
        owls = [self.refused(desk) for desk in ("moody", "hermione", "ron")]
        self.clear()
        for owl_id in owls:
            marker = self.marker(owl_id)
            self.write_file(self.office / "state" / config.STOP_HELD_DIR / owl_id,
                            json.dumps({**marker, "resumes": config.STOP_RESUMES_MAX}))
        with mock.patch.object(stops.pensieve, "add_event", side_effect=stops.StoreError("locked")):
            owl_post.run_pass(self.conn, now=NOW + 60)
        self.assertEqual(self.held_owls(), sorted(owls))  # no event, so nothing given up yet
        with mock.patch.object(config, "STOP_RESTARTS_PER_PASS", 2), mock.patch.object(run_desk, "spawn") as spawn:
            owl_post.run_pass(self.conn, now=NOW + 120)
            self.assertEqual(len(self.held_owls()), 1)
            owl_post.run_pass(self.conn, now=NOW + 180)
        spawn.assert_not_called()
        self.assertEqual(self.held_owls(), [])
        events = self.events_of(stops.EVENT_KIND)
        self.assertEqual(len(events), 2)
        for owl_id in owls:
            self.assertEqual(sum(owl_id in event["summary"] for event in events), 1)

    def test_no_event_means_no_launch_and_the_claim_is_given_back(self):
        owl_id = self.refused()
        self.clear()
        with mock.patch.object(stops.pensieve, "add_event", side_effect=stops.StoreError("locked")), \
                mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])
        spawn.assert_not_called()
        self.assertEqual((self.marker(owl_id)["state"], self.marker(owl_id)["resumes"]), ("held", 0))

    def test_a_held_run_whose_owl_was_acked_meanwhile_is_dropped_without_a_run(self):
        owl_id = self.refused()
        owlery.read(self.conn, owl_id, "moody", now=NOW)
        owlery.ack(self.conn, owl_id, "moody", now=NOW)
        self.clear()
        with mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])
        spawn.assert_not_called()
        self.assertEqual(self.held_owls(), [])
        self.assertEqual(self.events_of(stops.EVENT_KIND), [])

    def test_a_launch_that_fails_to_start_stays_held(self):
        owl_id = self.refused()
        self.clear()
        with mock.patch.object(run_desk, "spawn", side_effect=OSError("no fork")):
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])
        self.assertEqual((self.marker(owl_id)["state"], self.marker(owl_id)["resumes"]), ("held", 0))
        with mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(len(owl_post.run_pass(self.conn, now=NOW + 120)["restarted"]), 1)
        spawn.assert_called_once_with("moody", owl_id)

    def test_a_stop_check_that_cannot_read_the_state_folder_restarts_nothing(self):
        self.refused()
        self.clear()
        with mock.patch.object(run_desk, "stop_requested", return_value=True), \
                mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])
        spawn.assert_not_called()

    def test_a_held_marker_that_does_not_read_whole_is_kept_never_dropped(self):
        owl_id = self.refused()
        self.write_file(self.office / "state" / config.STOP_HELD_DIR / owl_id, '{"state": "held", "desk"')
        self.clear()
        with mock.patch.object(run_desk, "spawn") as spawn:
            self.assertEqual(owl_post.run_pass(self.conn, now=NOW + 60)["restarted"], [])
        spawn.assert_not_called()
        self.assertEqual(self.held_owls(), [owl_id])
