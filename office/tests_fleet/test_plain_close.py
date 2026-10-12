"""The plain close phrases: close <task-id> and drop <task-id>, each the whole message Ryan typed.

They run through the prompt hook on transcripts written here, or through the go confirmer when the prompt's entry is not
written yet. Tasks are put in each state straight through the store; no desk or reviewer ever runs.
"""
from __future__ import annotations

import json
from unittest import mock

from hogwarts import capacity, pensieve
from tests.support import NOW, SHA

from fleet import closer, go_confirm, run_desk
from fleet.hooks import user_prompt_submit as hook
from tests_fleet.support import PROMPT_ID, assistant_entry, peer_entry, user_entry
from tests_fleet.test_bulk_close import CloseCase
from tests_fleet.test_go_confirm import Clock


class PlainCloseTests(CloseCase):
    def reason(self, task_id: str) -> tuple:
        task = pensieve.get_task(self.conn, task_id)
        return task["status"], task["close_reason"]

    def test_close_closes_a_ready_task_as_mischief_managed_does(self):
        task = self.passed(title="ready")
        other = self.passed(title="left alone")
        shown, context = self.said(f"close {task['id']}")
        self.assertTrue(shown.startswith(f"Close: task {task['id']} is closed as complete."), shown)
        self.assertIn(f"Close: task {task['id']} is closed as complete.", context)
        self.assertEqual(self.reason(task["id"]), ("closed", "complete"))
        self.assertEqual(self.status(other["id"]), "awaiting_close")
        minted = self.conn.execute("SELECT task_id, minted_by, consumed_at FROM close_tokens").fetchall()
        self.assertEqual([tuple(row) for row in minted], [(task["id"], "hook", NOW)])

    def test_close_refuses_a_task_in_flight_by_name_with_its_fix_line(self):
        task = self.built(title="still building")
        shown, _ = self.said(f"close {task['id']}")
        self.assertIn(f"Close was not applied to {task['id']}: the task is active", shown)
        self.assertIn(f"Fix: type close {task['id']} again once its review passes and it awaits close", shown)
        self.assertEqual(self.status(task["id"]), "active")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_close_of_a_go_task_closes_its_builds_and_their_reviews_with_it(self):
        go_id = self.go_task()
        build = self.passed(parent=go_id, title="the build")
        review = pensieve.create_task(self.conn, "hermione", "a review still queued", parent_task_id=build["id"],
                                      now=NOW)
        shown, _ = self.said(f"close {go_id}")
        self.assertTrue(shown.startswith(f"Close: go task {go_id} is closed as superseded, since a go task holds no"
                                         " work of its own"), shown)
        self.assertIn(f"- build {build['id']} (harry) closed as complete", shown)
        self.assertIn(f"- cascaded: {review['id']} (hermione) closed as superseded", shown)
        self.assertEqual(self.reason(build["id"]), ("closed", "complete"))
        self.assertEqual(self.status(review["id"]), "closed")
        self.assertEqual(self.reason(go_id), ("closed", "superseded"))

    def test_close_of_a_go_task_moves_it_on_on_a_proven_build(self):
        self.auto_close_on()
        go_id = self.go_task()
        first = self.passed(parent=go_id, title="first build")
        self.proven(first)
        second = self.passed(parent=go_id, title="second build")
        shown, _ = self.said(f"close {go_id}")
        self.assertTrue(shown.startswith(f"Close: go task {go_id} is closed as complete, on a build's proof."), shown)
        self.assertEqual(self.reason(second["id"]), ("closed", "complete"))
        self.assertEqual(self.reason(go_id), ("closed", "complete"))

    def test_close_of_a_go_task_with_a_build_in_flight_closes_nothing(self):
        go_id = self.go_task()
        ready = self.passed(parent=go_id, title="ready")
        live = self.built(parent=go_id, title="still building")
        shown, _ = self.said(f"close {go_id}")
        self.assertIn(f"Close was not applied to {go_id}: its build {live['id']} (active) does not await close yet,"
                      " so nothing was closed.", shown)
        self.assertIn(f"Fix: type close {go_id} again once its review passes, or type drop {go_id}", shown)
        self.assertEqual([self.status(task_id) for task_id in (ready["id"], live["id"], go_id)],
                         ["awaiting_close", "active", "queued"])

    def test_close_of_a_go_task_with_a_build_held_by_a_review_closes_nothing(self):
        go_id = self.go_task()
        first = self.passed(parent=go_id, title="first")
        second = self.passed(parent=go_id, title="second, held")
        with run_desk.task_lock(second["id"]):
            shown, _ = self.said(f"close {go_id}")
        self.assertIn(f"Close was not applied to {go_id}: a review, a run or auto-close is working on its build"
                      f" {second['id']} right now, so nothing was closed.", shown)
        self.assertEqual([self.status(task_id) for task_id in (first["id"], second["id"], go_id)],
                         ["awaiting_close", "awaiting_close", "queued"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_a_go_close_whose_second_build_cannot_close_rolls_the_first_back(self):
        go_id = self.go_task()
        first = self.passed(parent=go_id, title="first")
        second = self.passed(parent=go_id, title="second")
        real = hook.close_locked

        def close_locked(conn, task_id, now, settled=False, words=hook.MISCHIEF_WORDS):
            if task_id == second["id"]:
                return [f"Close failed for {task_id}: the store refused it", hook.close_fix(task_id, words=words)]
            return real(conn, task_id, now, settled, words)

        with mock.patch.object(hook, "close_locked", side_effect=close_locked):
            shown, _ = self.said(f"close {go_id}")
        self.assertIn(f"Close was not applied to {go_id}, so nothing was closed: its build {second['id']} could not"
                      " close.", shown)
        self.assertEqual([self.status(task_id) for task_id in (first["id"], second["id"], go_id)],
                         ["awaiting_close", "awaiting_close", "queued"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_drop_closes_a_headmaster_build_with_its_go_task(self):
        go_id = self.go_task()
        build = self.built(parent=go_id, title="the decision is yours")
        self.round(build, "HEADMASTER")
        review = pensieve.create_task(self.conn, "hermione", "a review still queued", parent_task_id=build["id"],
                                      now=NOW)
        shown, context = self.said(f"drop {build['id']}")
        self.assertTrue(shown.startswith(f"Drop: task {build['id']} is closed as superseded."), shown)
        self.assertIn(f"- cascaded: {review['id']} (hermione) closed as superseded", shown)
        self.assertIn(f"- with it: its go task {go_id} closed as superseded, since nothing else was open under it",
                      context)
        for task_id in (build["id"], review["id"], go_id):
            self.assertEqual(self.reason(task_id), ("closed", "superseded"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_drop_of_a_go_task_drops_its_builds_and_a_build_with_a_sibling_keeps_its_go_task(self):
        go_id = self.go_task()
        first = self.built(parent=go_id, title="first")
        second = self.built(parent=go_id, title="second")
        shown, _ = self.said(f"drop {first['id']}")
        self.assertNotIn("with it", shown)
        self.assertEqual((self.status(first["id"]), self.status(second["id"]), self.status(go_id)),
                         ("closed", "active", "queued"))
        shown, _ = self.said(f"drop {go_id}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc11")
        self.assertTrue(shown.startswith(f"Drop: go task {go_id} is closed as superseded."), shown)
        self.assertIn(f"- cascaded: {second['id']} (harry) closed as superseded", shown)
        self.assertEqual(self.reason(go_id), ("closed", "superseded"))

    def test_drop_is_refused_while_a_review_or_a_run_is_going(self):
        go_id = self.go_task()
        build = self.built(parent=go_id, title="in review")
        self.round(build)
        with run_desk.task_lock(build["id"]):
            shown, _ = self.said(f"drop {go_id}")
        self.assertIn(f"Drop was not applied to {go_id}: a review, a run or auto-close is working on {build['id']}"
                      " right now, so nothing was dropped.", shown)
        self.assertIn(f"Fix: wait for it to finish, then type drop {go_id} again", shown)
        capacity.record_launch(self.conn, "harry", "run-" + "a" * 16, "a-model", task_id=build["id"], now=NOW - 60)
        shown, _ = self.said(f"drop {build['id']}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc12")
        self.assertIn(f"Drop was not applied to {build['id']}: harry has a run going on {build['id']}", shown)
        self.assertEqual([self.status(task_id) for task_id in (build["id"], go_id)], ["active", "queued"])

    def test_drop_of_a_review_waits_for_its_author_and_a_launch_waits_too(self):
        build = self.built(title="in review")
        review = self.round(build)["task"]
        with run_desk.task_lock(build["id"]):  # a review holds its author's lock, not its own task's
            shown, _ = self.said(f"drop {review['id']}")
        self.assertIn(f"Drop was not applied to {review['id']}: a review, a run or auto-close is working on"
                      f" {build['id']} right now", shown)
        with run_desk.launch_lock("harry"):
            shown, _ = self.said(f"drop {build['id']}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc16")
        self.assertIn(f"Drop was not applied to {build['id']}: a run of harry is launching right now, so nothing was"
                      " dropped.", shown)
        self.assertEqual((self.status(build["id"]), self.status(review["id"])), ("active", "queued"))

    def test_drop_is_refused_for_a_merged_task_which_is_a_close(self):
        merged = self.passed(title="merged")
        closer.write_record({**closer.fresh_record(merged["id"]), "merge_sha": SHA,
                             "landed": {"how": "pr", "pr": 7}, "landed_seen_at": NOW,
                             "waiting": {"what": "ci", "since": NOW}})
        shown, _ = self.said(f"drop {merged['id']}")
        self.assertIn(f"Drop was not applied to {merged['id']}: it merged, and a merged task is closed, not dropped.",
                      shown)
        self.assertIn(f"Fix: type close {merged['id']} instead.", shown)
        self.assertEqual(self.status(merged["id"]), "awaiting_close")
        go_id = self.go_task()
        landed = self.passed(parent=go_id, title="landed")
        self.proven(landed)
        self.built(parent=go_id, title="a second try")
        shown, _ = self.said(f"drop {go_id}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc13")
        self.assertIn(f"Drop was not applied to {go_id}: {landed['id']} under it merged, proven by auto-close", shown)
        self.assertEqual(self.status(go_id), "queued")

    def test_a_dropped_pr_drops(self):
        task = self.passed(title="closed unmerged")
        self.stopped(task, "landed", closer.PR_DROPPED)
        shown, _ = self.said(f"drop {task['id']}")
        self.assertTrue(shown.startswith(f"Drop: task {task['id']} is closed as superseded."), shown)

    def test_the_old_phrases_work_as_before(self):
        one, two = self.passed(title="one"), self.passed(title="two")
        shown, _ = self.said(f"Mischief managed {one['id']}")
        self.assertTrue(shown.startswith(f"Mischief managed: task {one['id']} is closed as complete."), shown)
        live = self.built(title="live")
        shown, _ = self.said(f"Mischief managed {live['id']}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc14")
        self.assertIn(f"Fix: type Mischief managed {live['id']} again once its review passes", shown)
        shown, _ = self.said(hook.BULK_PHRASE, prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc15")
        self.assertIn(f"{hook.BULK_PHRASE}: 1 closed, 1 refused", shown)
        self.assertEqual(self.status(two["id"]), "closed")

    def test_a_phrase_inside_a_longer_message_or_with_no_exact_id_does_nothing(self):
        task = self.passed(title="ready")
        for text in (f"please close {task['id']}", f"close {task['id']} now", f"close {task['id']}.",
                     f"close {task['id']}\nthanks", f"`close {task['id']}`", "close it", "drop it",
                     f"close {task['id'][:-1]}", f"close {task['id'].upper()}", f"close  {task['id']}",
                     f"drop {task['id']} and close the rest", f"- drop {task['id']}"):
            with self.subTest(text=text):
                shown, _ = self.said(text)
                self.assertNotIn(task["id"], shown)
                self.assertEqual(self.status(task["id"]), "awaiting_close")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_a_phrase_mcgonagall_writes_does_nothing(self):
        task = self.passed(title="ready")
        line = f"close {task['id']}"
        # Her reply holds the line, and Ryan's own prompt is something else: nothing closes.
        path = self.write_transcript([assistant_entry("msg_1", [{"type": "text", "text": f"Send:\n{line}"}]),
                                      user_entry("thanks", promptId=PROMPT_ID)])
        shown, _ = self.said("thanks", transcript=path)
        self.assertNotIn(task["id"], shown)
        # Text another session sends in is not Ryan's typing either.
        for text in (line, f"drop {task['id']}"):
            with self.subTest(text=text):
                path = self.write_transcript([peer_entry(text, promptId=PROMPT_ID)], name="peer.jsonl")
                shown, _ = self.said(text, transcript=path)
                self.assertIn("was not applied", shown)
                self.assertIn("could not confirm Ryan's own typing", shown)
        self.assertEqual(self.status(task["id"]), "awaiting_close")

    def test_a_deferred_drop_is_confirmed_as_a_close_event(self):
        go_id = self.go_task()
        build = self.built(parent=go_id, title="blocked on tooling")
        path = self.write_transcript([user_entry("earlier")], name="later.jsonl")
        shown, context = self.said(f"drop {build['id']}", transcript=path)
        self.assertIn(f"Drop for {build['id']}: Claude Code writes this prompt", shown)
        self.assertIn(f"castle task show {build['id']}", context)
        self.assertEqual(self.status(build["id"]), "active")
        payload = self.spawned_confirms.call_args[0][0]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(user_entry(f"drop {build['id']}", promptId=PROMPT_ID)) + "\n")
        clock = Clock()
        go_confirm.confirm(payload, clock=clock, sleep=clock.sleep)
        self.assertEqual((self.reason(build["id"]), self.reason(go_id)),
                         (("closed", "superseded"), ("closed", "superseded")))
        [event] = [dict(row) for row in self.conn.execute(
            "SELECT task_id, summary FROM events WHERE kind = 'close.confirmed'")]
        self.assertEqual(event["task_id"], build["id"])
        self.assertTrue(event["summary"].startswith(f"Drop: task {build['id']} is closed as superseded."))
        self.assertEqual(go_confirm.interrupted_lines("drop", build["id"]),
                         [f"The confirmation of drop {build['id']} was interrupted; check castle task show"
                          f" {build['id']} and type drop {build['id']} again if it is not closed."])
