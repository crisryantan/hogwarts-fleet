from __future__ import annotations

import unittest
from unittest import mock

from hogwarts import owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from tests.support import DAY, NOW, StoreCase, outbox_file


class RequestTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def open(self, title="review the diff", **kwargs):
        kwargs.setdefault("now", NOW)
        return owlery.open_request(self.conn, "alpha", "beta", title, **kwargs)

    def walk(self, request_id, *phases, now=NOW):
        for phase in phases:
            owlery.advance(self.conn, request_id, phase, now=now)

    def run_request(self, opened, now=NOW):
        request_id = opened["request"]["id"]
        self.walk(request_id, "claimed", now=now)
        pensieve.start_task(self.conn, opened["task"]["id"], now=now)
        self.walk(request_id, "running", now=now)
        return request_id

    def test_request_phases_constant(self):
        self.assertEqual(owlery.REQUEST_PHASES,
                         ("queued", "claimed", "running", "result_posted", "task_closed", "cleaned"))

    def test_open_request_creates_request_task_and_owl_together(self):
        parent = self.started("alpha")
        opened = self.open(body="the details", parent_task_id=parent["id"])
        request, task, owl = opened["request"], opened["task"], opened["owl"]
        self.assertTrue(opened["created"])
        self.assertEqual((request["phase"], request["requester"], request["recipient"]), ("queued", "alpha", "beta"))
        self.assertEqual((task["desk"], task["status"], task["parent_task_id"], task["request_id"]),
                         ("beta", "queued", parent["id"], request["id"]))
        self.assertEqual(request["task_id"], task["id"])
        self.assertEqual((owl["kind"], owl["sender"], owl["recipient"], owl["request_id"], owl["task_id"]),
                         ("request", "alpha", "beta", request["id"], task["id"]))
        self.assertNotIn("body", owl)
        self.assertEqual(owlery.read(self.conn, owl["id"], "beta")["body"], "the details")
        self.assertEqual([row["phase"] for row in request["history"]], ["queued"])

    def test_open_request_rolls_back_entirely_on_failure(self):
        with mock.patch.object(owlery, "_deliver", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.open()
        self.assertEqual((self.count("requests"), self.count("tasks"), self.count("owls"),
                          self.count("request_phases")), (0, 0, 0, 0))

    def test_open_request_is_idempotent(self):
        first = self.open(body="same")
        again = self.open(body="same")
        self.assertFalse(again["created"])
        self.assertEqual(again["request"]["id"], first["request"]["id"])
        self.assertEqual(self.count("tasks"), 1)
        keyed = self.open(title="other", idempotency_key="outbox-req-0001")
        self.assertEqual(self.open(title="other", idempotency_key="outbox-req-0001")["request"]["id"],
                         keyed["request"]["id"])
        with self.assertRaises(ConflictError):
            self.open(title="changed", idempotency_key="outbox-req-0001")

    def test_keyed_request_with_a_different_body_or_parent_conflicts(self):
        parent = self.started("alpha")
        self.open(title="keyed", body="A", idempotency_key="outbox-req-0002")
        for kwargs in ({"body": "B"}, {"body_path": outbox_file("alpha")}, {"body": "A", "parent_task_id": parent["id"]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConflictError):
                    self.open(title="keyed", idempotency_key="outbox-req-0002", **kwargs)
        self.assertFalse(self.open(title="keyed", body="A", idempotency_key="outbox-req-0002")["created"])

    def test_identical_request_after_it_finished_is_a_new_request(self):
        declined = self.open(body="same ask")
        owlery.decline(self.conn, declined["request"]["id"], "safety")
        reopened = self.open(body="same ask", now=NOW + DAY)
        self.assertTrue(reopened["created"])
        self.assertNotEqual(reopened["request"]["id"], declined["request"]["id"])
        request_id = self.run_request(reopened)
        self.post_result(request_id)
        self.walk(request_id, "result_posted")
        self.close_complete(reopened["task"]["id"])
        self.walk(request_id, "task_closed", "cleaned")
        third = self.open(body="same ask", now=NOW + 2 * DAY)
        self.assertTrue(third["created"])
        self.assertFalse(self.open(body="same ask", now=NOW + 3 * DAY)["created"])

    def test_parent_task_must_belong_to_the_requester_and_be_open(self):
        foreign = self.started("beta")
        with self.assertRaises(ValidationError):
            self.open(parent_task_id=foreign["id"])
        mine = self.task("alpha")
        pensieve.close_task(self.conn, mine["id"], "abandoned")
        with self.assertRaises(ConflictError):
            self.open(parent_task_id=mine["id"])

    def test_requester_and_recipient_must_differ_and_exist(self):
        with self.assertRaises(ValidationError):
            owlery.open_request(self.conn, "alpha", "alpha", "self")
        with self.assertRaises(NotFoundError):
            owlery.open_request(self.conn, "alpha", "gamma", "nobody")

    def test_advance_allows_only_the_next_phase(self):
        opened = self.open()
        request_id = opened["request"]["id"]
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "running")
        self.run_request(opened)
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "claimed")
        with self.assertRaises(ValidationError):
            owlery.advance(self.conn, request_id, "finished")

    def test_advance_is_idempotent_at_the_current_phase(self):
        request_id = self.open()["request"]["id"]
        self.walk(request_id, "claimed")
        again = owlery.advance(self.conn, request_id, "claimed", now=NOW + 50)
        self.assertEqual(again["phase"], "claimed")
        self.assertEqual([row["phase"] for row in again["history"]], ["queued", "claimed"])

    def test_advance_records_history_with_detail(self):
        request_id = self.open()["request"]["id"]
        owlery.advance(self.conn, request_id, "claimed", detail="picked up by broker", now=NOW + 1)
        history = owlery.get_request(self.conn, request_id)["history"]
        self.assertEqual(history[-1], {"phase": "claimed", "ts": NOW + 1, "detail": "picked up by broker"})

    def test_running_requires_the_task_to_have_started(self):
        opened = self.open()
        request_id = opened["request"]["id"]
        self.walk(request_id, "claimed")
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "running")
        pensieve.start_task(self.conn, opened["task"]["id"])
        pensieve.mark_awaiting_close(self.conn, opened["task"]["id"])
        self.assertEqual(owlery.advance(self.conn, request_id, "running")["phase"], "running")

    def test_result_posted_requires_a_result_owl(self):
        request_id = self.run_request(self.open())
        owlery.send(self.conn, "beta", "alpha", "question", "which file?", request_id=request_id)
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "result_posted")
        self.post_result(request_id)
        self.assertEqual(owlery.advance(self.conn, request_id, "result_posted")["phase"], "result_posted")

    def test_task_closed_requires_the_request_task_to_be_closed(self):
        opened = self.open()
        request_id, task_id = opened["request"]["id"], opened["task"]["id"]
        self.run_request(opened)
        self.post_result(request_id)
        self.walk(request_id, "result_posted")
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "task_closed")
        self.close_complete(task_id)
        done = owlery.advance(self.conn, request_id, "task_closed")
        self.assertEqual((done["phase"], done["outcome"]), ("task_closed", "done"))
        cleaned = owlery.advance(self.conn, request_id, "cleaned")
        self.assertEqual(cleaned["phase"], "cleaned")
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "queued")

    def test_abandoned_task_does_not_mark_the_request_done(self):
        opened = self.open()
        request_id = self.run_request(opened)
        self.post_result(request_id)
        self.walk(request_id, "result_posted")
        pensieve.close_task(self.conn, opened["task"]["id"], "abandoned")
        self.assertIsNone(owlery.advance(self.conn, request_id, "task_closed")["outcome"])

    def test_defer_from_open_phases_closes_the_task_as_superseded(self):
        for phases in ((), ("claimed",), ("claimed", "running")):
            with self.subTest(phases=phases):
                opened = self.open(title=f"defer after {len(phases)}")
                request_id = opened["request"]["id"]
                if phases[-1:] == ("running",):
                    self.run_request(opened)
                else:
                    self.walk(request_id, *phases)
                deferred = owlery.defer(self.conn, request_id, "conflict", now=NOW + 5)
                self.assertEqual((deferred["outcome"], deferred["reason"]), ("deferred", "conflict"))
                self.assertEqual(deferred["history"][-1]["phase"], "deferred")
                task = pensieve.get_task(self.conn, opened["task"]["id"])
                self.assertEqual((task["status"], task["close_reason"]), ("closed", "superseded"))

    def test_decline_sets_outcome_and_is_idempotent(self):
        request_id = self.open()["request"]["id"]
        declined = owlery.decline(self.conn, request_id, "safety")
        self.assertEqual(declined["outcome"], "declined")
        self.assertEqual(owlery.decline(self.conn, request_id, "safety")["outcome"], "declined")
        with self.assertRaises(ConflictError):
            owlery.defer(self.conn, request_id, "conflict")

    def test_defer_and_decline_are_refused_after_running(self):
        opened = self.open()
        request_id = self.run_request(opened)
        self.post_result(request_id)
        self.walk(request_id, "result_posted")
        with self.assertRaises(ConflictError):
            owlery.defer(self.conn, request_id, "conflict")
        with self.assertRaises(ConflictError):
            owlery.decline(self.conn, request_id, "safety")

    def test_reasons_are_validated(self):
        request_id = self.open()["request"]["id"]
        with self.assertRaises(ValidationError):
            owlery.defer(self.conn, request_id, "busy")

    def test_terminal_requests_cannot_advance_or_take_results(self):
        request_id = self.open()["request"]["id"]
        owlery.decline(self.conn, request_id, "missing_access")
        with self.assertRaises(ConflictError):
            owlery.advance(self.conn, request_id, "claimed")
        with self.assertRaises(ConflictError):
            owlery.send(self.conn, "beta", "alpha", "result", "late", request_id=request_id)

    def test_unknown_request_is_not_found(self):
        with self.assertRaises(NotFoundError):
            owlery.advance(self.conn, "rq_0000000000000000", "claimed")
        with self.assertRaises(NotFoundError):
            owlery.get_request(self.conn, "rq_0000000000000000")

    def test_deferring_a_request_leaves_sub_request_outcomes_unset(self):
        self.desk("gamma", "claude")
        top = self.open(title="top")
        self.run_request(top)
        sub = owlery.open_request(self.conn, "beta", "gamma", "sub work", parent_task_id=top["task"]["id"])
        self.run_request(sub)
        owlery.defer(self.conn, top["request"]["id"], "conflict")
        sub_request = owlery.get_request(self.conn, sub["request"]["id"])
        sub_task = pensieve.get_task(self.conn, sub["task"]["id"])
        self.assertEqual((sub_task["status"], sub_task["close_reason"]), ("closed", "superseded"))
        self.assertEqual((sub_request["phase"], sub_request["outcome"]), ("task_closed", None))
        self.assertEqual([row["phase"] for row in sub_request["history"]], ["queued", "claimed", "running", "task_closed"])

    def test_declining_a_request_supersedes_delegated_sub_work(self):
        self.desk("gamma", "claude")
        top = self.open(title="top")
        sub = owlery.open_request(self.conn, "beta", "gamma", "sub work", parent_task_id=top["task"]["id"])
        self.run_request(sub)
        owlery.decline(self.conn, top["request"]["id"], "safety")
        self.assertEqual(pensieve.get_task(self.conn, sub["task"]["id"])["close_reason"], "superseded")
        self.assertIsNone(owlery.get_request(self.conn, sub["request"]["id"])["outcome"])
        self.assertEqual(owlery.request_owls(self.conn, sub["request"]["id"])[0]["kind"], "request")

    def test_cascade_helper_is_private_and_checks_ancestry(self):
        self.assertFalse(hasattr(owlery, "cascade_task_closed"))
        self.desk("gamma", "claude")
        opened = self.open()
        unrelated = self.task("gamma")
        with self.assertRaises(IntegrityError):
            owlery._cascade_task_closed(self.conn, opened["request"]["id"], unrelated["id"])
        pensieve.close_task(self.conn, opened["task"]["id"], "abandoned")
        pensieve.close_task(self.conn, unrelated["id"], "abandoned")
        with self.assertRaises(IntegrityError):
            owlery._cascade_task_closed(self.conn, opened["request"]["id"], unrelated["id"])
        request = owlery.get_request(self.conn, opened["request"]["id"])
        self.assertEqual((request["phase"], request["outcome"]), ("queued", None))

    def test_request_owls_lists_metadata_only(self):
        opened = self.open(body="the ask")
        request_id = self.run_request(opened)
        owlery.send(self.conn, "beta", "alpha", "question", "scope?", body="secret detail", request_id=request_id,
                    now=NOW)
        result = self.post_result(request_id, now=NOW + 1)
        owls = owlery.request_owls(self.conn, request_id)
        self.assertEqual([owl["kind"] for owl in owls], ["request", "question", "result"])
        self.assertEqual(owls[-1]["id"], result["id"])
        self.assertTrue(all("body" not in owl for owl in owls))
        with self.assertRaises(NotFoundError):
            owlery.request_owls(self.conn, "rq_0000000000000000")

    def test_list_requests_filters(self):
        first = self.open(title="one")["request"]["id"]
        self.open(title="two")
        owlery.decline(self.conn, first, "ambiguous_scope")
        self.assertEqual(len(owlery.list_requests(self.conn, desk="beta")), 2)
        self.assertEqual(len(owlery.list_requests(self.conn, open_only=True)), 1)
        self.assertEqual(len(owlery.list_requests(self.conn, phase="claimed")), 0)
        self.assertEqual(owlery.list_requests(self.conn, desk="gamma"), [])


if __name__ == "__main__":
    unittest.main()
