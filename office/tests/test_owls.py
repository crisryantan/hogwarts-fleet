from __future__ import annotations

import builtins
import os
import sqlite3
import unittest
from unittest import mock

from hogwarts import owlery
from hogwarts.errors import ConflictError, NotFoundError, ValidationError
from tests.support import DAY, NOW, StoreCase, outbox_file


class OwlTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def send(self, sender="alpha", recipient="beta", kind="fyi", subject="hello", **kwargs):
        kwargs.setdefault("now", NOW)
        return owlery.send(self.conn, sender, recipient, kind, subject, **kwargs)

    def request(self):
        return owlery.open_request(self.conn, "alpha", "beta", "please check", body="details", now=NOW)

    def test_send_and_inbox_return_metadata_only(self):
        sent = self.send(body="private words")
        self.assertTrue(sent["created"])
        self.assertTrue(sent["has_body"])
        self.assertNotIn("body", sent)
        self.assertNotIn("idem_hash", sent)
        inbox = owlery.inbox(self.conn, "beta")
        self.assertEqual([owl["id"] for owl in inbox], [sent["id"]])
        self.assertNotIn("body", inbox[0])
        self.assertEqual(owlery.inbox(self.conn, "alpha"), [])

    def test_sender_must_differ_from_recipient(self):
        with self.assertRaises(ValidationError):
            self.send("alpha", "alpha")

    def test_both_desks_must_be_registered(self):
        with self.assertRaises(NotFoundError):
            self.send("alpha", "gamma")
        with self.assertRaises(NotFoundError):
            self.send("gamma", "alpha")

    def test_duplicate_send_returns_existing_owl(self):
        first = self.send(body="same")
        again = self.send(body="same", now=NOW + 100)
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(again["created"])
        self.assertEqual(self.count("owls"), 1)
        self.assertTrue(self.send(body="different")["created"])

    def test_identical_owl_after_ack_is_a_new_owl(self):
        first = self.send(body="ping")
        owlery.read(self.conn, first["id"], "beta")
        owlery.ack(self.conn, first["id"], "beta")
        later = self.send(body="ping", now=NOW + 30 * DAY)
        self.assertTrue(later["created"])
        self.assertNotEqual(later["id"], first["id"])
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "beta")], [later["id"]])
        self.assertFalse(self.send(body="ping", now=NOW + 31 * DAY)["created"])

    def test_idempotency_key_dedupes_and_rejects_a_changed_payload(self):
        first = self.send(body="v1", idempotency_key="outbox-file-0001")
        self.assertFalse(self.send(body="v1", idempotency_key="outbox-file-0001")["created"])
        with self.assertRaises(ConflictError):
            self.send(body="v2", idempotency_key="outbox-file-0001")
        other = owlery.send(self.conn, "beta", "alpha", "fyi", "hello", body="v1", idempotency_key="outbox-file-0001")
        self.assertNotEqual(other["id"], first["id"])

    def test_answer_must_reply_to_a_question_addressed_to_the_sender(self):
        question = self.send(kind="question", subject="which branch?")
        fyi = self.send(subject="note")
        with self.assertRaises(ValidationError):
            self.send("beta", "alpha", "answer", "main", in_reply_to=fyi["id"])
        with self.assertRaises(ValidationError):
            self.send("beta", "alpha", "answer", "main")
        self.desk("gamma", "codex")
        with self.assertRaises(ValidationError):
            self.send("gamma", "alpha", "answer", "main", in_reply_to=question["id"])
        answer = self.send("beta", "alpha", "answer", "main", in_reply_to=question["id"])
        self.assertTrue(answer["created"])

    def test_one_answer_per_question(self):
        question = self.send(kind="question", subject="which branch?")
        self.send("beta", "alpha", "answer", "main", in_reply_to=question["id"])
        with self.assertRaises(ConflictError):
            self.send("beta", "alpha", "answer", "develop", in_reply_to=question["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO owls(id, idem_hash, sender, recipient, kind, in_reply_to, subject, created_at)"
                " VALUES ('owl_00000000000000ff', 'x', 'beta', 'alpha', 'answer', ?, 's', 1)",
                (question["id"],),
            )

    def test_result_requires_a_request_whose_recipient_is_the_sender(self):
        opened = self.request()
        request_id = opened["request"]["id"]
        with self.assertRaises(ValidationError):
            self.send("beta", "alpha", "result", "done")
        with self.assertRaises(ValidationError):
            self.send("alpha", "beta", "result", "done", request_id=request_id)
        result = self.send("beta", "alpha", "result", "done", request_id=request_id, body="all green")
        self.assertEqual(result["request_id"], request_id)

    def test_result_owl_task_must_belong_to_the_request(self):
        parent = self.started("alpha")
        opened = owlery.open_request(self.conn, "alpha", "beta", "please check", parent_task_id=parent["id"], now=NOW)
        request_id = opened["request"]["id"]
        unrelated = self.task("beta")
        with self.assertRaises(ValidationError):
            self.send("beta", "alpha", "result", "done", request_id=request_id, task_id=unrelated["id"])
        for task_id in (opened["task"]["id"], parent["id"]):
            with self.subTest(task_id=task_id):
                sent = self.send("beta", "alpha", "fyi", f"note {task_id}", request_id=request_id, task_id=task_id)
                self.assertTrue(sent["created"])

    def test_request_kind_only_comes_from_open_request(self):
        with self.assertRaises(ValidationError):
            self.send(kind="request")

    def test_request_scoped_owls_must_stay_between_the_parties(self):
        request_id = self.request()["request"]["id"]
        self.desk("gamma", "codex")
        with self.assertRaises(ValidationError):
            self.send("gamma", "beta", "fyi", "psst", request_id=request_id)
        self.assertTrue(self.send("beta", "alpha", "question", "scope?", request_id=request_id)["created"])

    def test_replies_stay_within_their_thread(self):
        question = self.send(kind="question", subject="q")
        self.desk("gamma", "codex")
        with self.assertRaises(ValidationError):
            self.send("gamma", "beta", "fyi", "re", in_reply_to=question["id"])
        with self.assertRaises(NotFoundError):
            self.send("beta", "alpha", "fyi", "re", in_reply_to="owl_0000000000000000")

    def test_read_sets_read_at_and_returns_the_body(self):
        sent = self.send(body="line one\nline two")
        read = owlery.read(self.conn, sent["id"], "beta", now=NOW + 7)
        self.assertEqual(read["body"], "line one\nline two")
        self.assertEqual(read["read_at"], NOW + 7)
        self.assertEqual(read["delivered_at"], NOW + 7)
        again = owlery.read(self.conn, sent["id"], "beta", now=NOW + 99)
        self.assertEqual(again["read_at"], NOW + 7)

    def test_only_the_recipient_can_read_or_ack(self):
        sent = self.send(body="private")
        with self.assertRaises(NotFoundError):
            owlery.read(self.conn, sent["id"], "alpha")
        owlery.read(self.conn, sent["id"], "beta")
        with self.assertRaises(NotFoundError):
            owlery.ack(self.conn, sent["id"], "alpha")

    def test_ack_requires_read_first(self):
        sent = self.send()
        with self.assertRaises(ConflictError):
            owlery.ack(self.conn, sent["id"], "beta")
        owlery.read(self.conn, sent["id"], "beta", now=NOW)
        acked = owlery.ack(self.conn, sent["id"], "beta", now=NOW + 3)
        self.assertEqual(acked["acked_at"], NOW + 3)
        self.assertEqual(owlery.inbox(self.conn, "beta"), [])
        self.assertEqual(len(owlery.inbox(self.conn, "beta", include_acked=True)), 1)

    def test_database_refuses_ack_without_read(self):
        sent = self.send()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE owls SET acked_at = 1 WHERE id = ?", (sent["id"],))

    def test_mark_delivered_sets_delivered_at_once(self):
        sent = self.send()
        self.assertEqual(owlery.mark_delivered(self.conn, sent["id"], now=NOW + 1)["delivered_at"], NOW + 1)
        self.assertEqual(owlery.mark_delivered(self.conn, sent["id"], now=NOW + 2)["delivered_at"], NOW + 1)
        with self.assertRaises(NotFoundError):
            owlery.mark_delivered(self.conn, "owl_0000000000000000")

    def test_body_and_body_path_are_exclusive(self):
        with self.assertRaises(ValidationError):
            self.send(body="x", body_path=outbox_file("alpha"))

    def test_body_path_must_be_under_the_sender_outbox(self):
        for path in ("/Users/crisryantan/.ssh/id_ed25519", outbox_file("beta"), outbox_file("alpha")[:-9]):
            with self.subTest(path=path):
                with self.assertRaises(ValidationError):
                    self.send(body_path=path)
        with self.assertRaises(ValidationError):
            owlery.open_request(self.conn, "alpha", "beta", "ask", body_path=outbox_file("beta"))
        self.assertEqual(self.send(body_path=outbox_file("alpha"))["body_path"], outbox_file("alpha"))

    def test_body_path_root_is_the_senders_castle_outbox(self):
        for path in ("/Users/crisryantan/hogwarts-fleet/desks/alpha/outbox/body.txt",
                     "/Users/crisryantan/hogwarts/desks/alpha/inbox/body.txt",
                     "/Users/crisryantan/hogwarts/desks/alpha/outbox",
                     "/Users/crisryantan/.hogwarts/desks/alpha/outbox/body.txt"):
            with self.subTest(path=path):
                with self.assertRaises(ValidationError):
                    self.send(body_path=path)
        path = "/Users/crisryantan/hogwarts/desks/alpha/outbox/body.txt"
        self.assertEqual(self.send(body_path=path)["body_path"], path)

    def test_body_path_is_validated_and_never_opened(self):
        path = outbox_file("alpha", "nested/body.txt")
        opened = []
        real_open, real_os_open = builtins.open, os.open

        def spy_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        def spy_os_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_os_open(file, *args, **kwargs)

        with mock.patch("builtins.open", spy_open), mock.patch("os.open", spy_os_open):
            sent = self.send(body_path=path)
            read = owlery.read(self.conn, sent["id"], "beta")
        self.assertEqual(read["body_path"], path)
        self.assertIsNone(read["body"])
        self.assertNotIn(path, opened)
        for bad in ("relative/body.txt", outbox_file("alpha", "../../beta/outbox/x")):
            with self.assertRaises(ValidationError):
                self.send(body_path=bad)

    def test_body_limit_is_16000(self):
        self.assertTrue(self.send(body="x" * 16000)["created"])
        with self.assertRaises(ValidationError):
            self.send(body="y" * 16001)

    def test_task_reference_must_exist(self):
        with self.assertRaises(NotFoundError):
            self.send(task_id="tk_0000000000000000")


if __name__ == "__main__":
    unittest.main()
