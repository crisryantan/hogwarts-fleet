"""An automatic review whose reviewer's whole family is down waits, and the Owl Post starts it again once one is back.

The reviewer runs through the real run_desk.run, which refuses before any process starts once the breaker says every
model it may run is down (failover.ModelsDown, told once as failover.wait). The clock is pinned, so the breaker, the
wait and the Owl Post's passes all read the same time.
"""
from __future__ import annotations

import contextlib
from unittest import mock

from hogwarts import capacity, pensieve
from tests.support import NOW

from fleet import config, failover, owl_post, review, run_desk
from tests_fleet.test_review_chain import ChainCase

REAL_RUN = run_desk.run


class ModelsDownCase(ChainCase):
    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # the Owl Post's starts are recorded, never run
        clock = mock.patch("time.time", return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.post(1)
        [self.owl_id] = owl_post.unfinished_handoffs(self.task["id"])

    @contextlib.contextmanager
    def reviewer_down(self):
        """The reviewer's own model goes down in the breaker just before its run, so the real run waits."""
        def run(conn, desk, owl_id, **kwargs):
            model = run_desk.build_plan(conn, desk, owl_id)["model"]
            now = int(self.clock.return_value)
            with failover.held_state() as state:
                state["models"][failover.model_key("claude", model)] = {
                    "failures": 2, "class": "outage", "since": now, "until": now + config.FAILOVER_DOWN_SECONDS}
            return REAL_RUN(conn, desk, owl_id, **kwargs)

        with mock.patch.object(run_desk, "run", side_effect=run):
            yield

    def waiting(self) -> dict:
        return failover.read_state()["waiting"]


class ModelsDownTests(ModelsDownCase):
    def test_the_review_waits_and_starts_again_once_a_model_is_back(self):
        task_id = self.task["id"]
        with self.reviewer_down():
            result = review.auto_review(self.conn, task_id)
        self.assertEqual(result["outcome"], "waiting: every model hermione may run is down, so it starts again once"
                                            " one is back; the Owl Post tries again on its next pass")
        self.assertEqual(owl_post.unfinished_handoffs(task_id), [self.owl_id])  # still pending, never finished
        self.assertEqual(owl_post.unfinished_afters(task_id), [])  # the round it opened ends with no verdict
        self.assertEqual(owl_post.take_try(task_id, self.owl_id), 1)  # its try was handed back
        owl_post.give_back_try(task_id, self.owl_id, 1)
        self.assertEqual([event["kind"] for event in self.new_events()], ["failover.wait"])
        self.assertEqual([row["has_verdict"] for row in capacity.review_rounds(self.conn, task_id)], [False])
        [item] = self.waiting().values()
        self.assertEqual((item["task"], item["owl"], item["desk"], item["resumes"]), (task_id, self.owl_id,
                                                                                    "hermione", 0))
        # While the family is down, no pass starts it again.
        self.spawned_reviews.reset_mock()
        summary = owl_post.run_pass(self.conn)
        self.assertEqual((summary["reviews"], summary["resumed"]), ([], []))
        self.spawned_reviews.assert_not_called()
        # Back (half-open): the Owl Post starts the review once, and the next pass leaves it to the usual resume.
        self.clock.return_value = NOW + config.FAILOVER_DOWN_SECONDS + 1
        summary = owl_post.run_pass(self.conn)
        self.assertEqual((summary["reviews"], summary["resumed"]), ([], [self.owl_id]))
        self.spawned_reviews.assert_called_once_with(task_id)
        self.assertEqual(owl_post.run_pass(self.conn)["resumed"], [])
        with self.fake_reviewer("PASS"):
            result = review.auto_review(self.conn, task_id)
        self.assertEqual(result["outcome"], "reviewed: PASS")
        self.assertEqual(self.rounds(), [(1, None), (1, "PASS")])
        owl_post.run_pass(self.conn)
        self.assertEqual(self.waiting(), {})

    def test_it_stops_after_the_resume_cap_and_says_so(self):
        task_id = self.task["id"]
        for resumed in range(config.FAILOVER_MAX_RESUMES):
            with self.subTest(resumed=resumed), self.reviewer_down():
                self.assertTrue(review.auto_review(self.conn, task_id)["outcome"].startswith("waiting: every model"))
            self.clock.return_value += config.FAILOVER_DOWN_SECONDS + 1
            self.assertEqual(owl_post.run_pass(self.conn)["resumed"], [self.owl_id])
        self.assertEqual(self.spawned_reviews.call_count, 1 + config.FAILOVER_MAX_RESUMES)
        with self.reviewer_down():
            result = review.auto_review(self.conn, task_id)
        self.assertIn(f"every model hermione may run was still down after it was started again"
                      f" {config.FAILOVER_MAX_RESUMES} times", result["outcome"])
        self.assertEqual(owl_post.unfinished_handoffs(task_id), [])
        self.assertIn("review.auto", [event["kind"] for event in self.new_events()])
        owl_post.run_pass(self.conn)
        self.assertEqual(self.waiting(), {})
        self.assertEqual([row["has_verdict"] for row in capacity.review_rounds(self.conn, task_id)].count(True), 0)

    def test_the_review_loops_deadline_holds_while_it_waits(self):
        task_id = self.task["id"]
        with self.reviewer_down():
            review.auto_review(self.conn, task_id)
        # Still down at the handoff's deadline: the Owl Post starts it once more, and it ends with the owner told.
        self.clock.return_value = NOW + config.AUTO_REVIEW_WAIT_LIMIT_SECONDS
        self.assertFalse(failover.review_waiting(task_id))
        summary = owl_post.run_pass(self.conn)
        self.assertEqual(summary["reviews"], [{"task_id": task_id, "review": owl_post.REVIEW_STARTED}])
        with self.reviewer_down():
            result = review.auto_review(self.conn, task_id)
        self.assertIn("waited 4 hours and gave up", result["outcome"])
        self.assertEqual(owl_post.unfinished_handoffs(task_id), [])
        owl_post.run_pass(self.conn)
        self.assertEqual(self.waiting(), {})

    def test_a_refusal_with_no_model_to_wait_for_ends_the_handoff(self):
        down = failover.ModelsDown("hermione waits: a review by hermione would not be cross-family", "failover:k",
                                   "claude", ())
        with mock.patch.object(run_desk, "run", side_effect=down):
            result = review.auto_review(self.conn, self.task["id"])
        self.assertIn("the automatic review stopped: hermione waits", result["outcome"])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.assertEqual(self.waiting(), {})

    def test_a_waiting_review_is_dropped_once_its_handoff_is_finished(self):
        with self.reviewer_down():
            review.auto_review(self.conn, self.task["id"])
        owl_post.finish_handoff(self.task["id"], self.owl_id, "reviewed by hand")
        self.clock.return_value = NOW + config.FAILOVER_DOWN_SECONDS + 1
        self.assertEqual(owl_post.run_pass(self.conn)["resumed"], [])
        self.assertEqual(self.waiting(), {})
        self.assertEqual(pensieve.get_task(self.conn, self.task["id"])["status"], "active")
