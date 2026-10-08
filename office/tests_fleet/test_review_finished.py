"""A finished review task stops blocking the next review; one in flight still does.

An own task whose newest verdict is PASS or HEADMASTER, with no round waiting or running and its review lock free, no
longer holds back a new review of your own sessions, and a single-task reviewer's round task whose verdict is recorded
no longer makes that reviewer look busy to the review loop.
"""
from __future__ import annotations

import re

from hogwarts import capacity, pensieve
from tests.support import NOW, REPO, SHA

from fleet import review, run_desk
from fleet.safefs import FleetError
from tests_fleet.support import MANY_TASK_DESKS, FleetCase
from tests_fleet.test_many_tasks import ManyCase


class FinishedOwnReviewTests(ManyCase):
    def headmaster_base(self) -> tuple:
        self.on_branch("base-pr")
        first = self.commit("base work")
        base = self.own_review(verdict="HEADMASTER")["task_id"]
        self.assertEqual(pensieve.get_task(self.conn, base)["status"], "active")
        return base, first

    def test_a_finished_own_review_no_longer_blocks_the_next_one(self):
        base, _ = self.headmaster_base()
        self.assertTrue(review.review_finished(self.conn, pensieve.get_task(self.conn, base)))
        self.stacked("stacked-pr")
        self.commit("stacked work")
        stacked = self.own_review()
        self.assertNotEqual(stacked["task_id"], base)
        self.assertEqual((stacked["round"], sorted(self.own_tasks())), (1, sorted([base, stacked["task_id"]])))
        # The same branch, too: its next commit is a new review, not one held back by the finished task.
        self.git("checkout", "-q", "base-pr")
        self.commit("base again")
        again = self.own_review()
        self.assertNotIn(again["task_id"], (base, stacked["task_id"]))

    def test_an_own_review_in_flight_still_blocks(self):
        base, first = self.headmaster_base()
        self.stacked("stacked-pr")
        self.commit("stacked work")
        held_back = re.escape(f"HEAD builds on commit {first[:12]} of task {base}")
        # A review or a run holds its lock: in flight.
        with run_desk.task_lock(base), self.not_made(), self.assertRaisesRegex(FleetError, held_back):
            self.own_review()
        # A round of it waiting for its reviewer: in flight.
        self.git("checkout", "-q", "base-pr")
        self.commit("base fixed")
        task = pensieve.get_task(self.conn, base)
        sha = self.git("rev-parse", "HEAD")
        capacity.open_review_round(self.conn, base, "moody", sha, "review it", now=NOW)
        self.assertFalse(review.review_finished(self.conn, task))
        self.git("checkout", "-q", "stacked-pr")
        with self.not_made(), self.assertRaisesRegex(FleetError, held_back):
            self.own_review()

    def test_changes_still_blocks(self):
        self.on_branch("base-pr")
        self.commit("base work")
        base = self.own_review(verdict="CHANGES")["task_id"]
        self.assertFalse(review.review_finished(self.conn, pensieve.get_task(self.conn, base)))


class FinishedReviewerTaskTests(FleetCase):
    many_task_desks = tuple(desk for desk in MANY_TASK_DESKS if desk != "hermione")

    def test_a_round_task_with_its_verdict_recorded_no_longer_makes_its_reviewer_busy(self):
        task = pensieve.create_task(self.conn, "harry", "build it", now=NOW)
        pensieve.start_task(self.conn, task["id"], now=NOW)
        pensieve.record_commit(self.conn, task["id"], REPO, SHA, now=NOW)
        opened = capacity.open_review_round(self.conn, task["id"], "hermione", SHA, "review it", now=NOW)
        pensieve.start_task(self.conn, opened["task"]["id"], now=NOW)
        self.assertTrue(review._reviewer_busy(self.conn, "hermione", NOW))  # in review: it blocks
        capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, "PASS", now=NOW)
        self.assertEqual(pensieve.blocking_task(self.conn, "hermione")["id"], opened["task"]["id"])
        self.assertFalse(review._reviewer_busy(self.conn, "hermione", NOW))  # finished: it no longer does
        other = pensieve.create_task(self.conn, "hermione", "something else", now=NOW)
        pensieve.close_task(self.conn, opened["task"]["id"], "superseded", now=NOW)
        pensieve.start_task(self.conn, other["id"], now=NOW)
        self.assertTrue(review._reviewer_busy(self.conn, "hermione", NOW))  # its own work still does
