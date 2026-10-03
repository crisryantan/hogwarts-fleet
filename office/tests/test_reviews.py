from __future__ import annotations

import sqlite3
import unittest

from hogwarts import owlery, pensieve
from hogwarts.errors import IntegrityError, NotFoundError, ValidationError
from tests.support import NOW, REPO, SHA, StoreCase, review_file

OTHER_SHA = "f" * 40


class ReviewTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desk("ryan-claude", "claude")
        self.desk("claude-two", "claude")
        self.desk("codex-one", "codex")
        self.desk("codex-two", "codex")
        self.desk("ryan", "human")
        self.desk("lint-bot", "script")
        self.authored = self.started("ryan-claude")
        self.request_review(self.authored["id"], "codex-one")
        pensieve.mark_awaiting_close(self.conn, self.authored["id"], REPO, SHA, now=NOW)

    def request_review(self, task_id, reviewer):
        return owlery.open_request(self.conn, "ryan-claude", reviewer, f"review {task_id}", parent_task_id=task_id)

    def record(self, reviewer, verdict, now=NOW, sha=SHA, task_id=None, **kwargs):
        return owlery.record_review(self.conn, REPO, sha, task_id or self.authored["id"], reviewer, verdict,
                                    now=now, **kwargs)

    def insert_review(self, reviewer, reviewer_family, sha=SHA):
        self.conn.execute(
            "INSERT INTO review_passes(id, repo, sha, task_id, author_desk, author_family, reviewer_desk,"
            " reviewer_family, verdict, created_at) VALUES ('rv_0000000000000000', ?, ?, ?, 'ryan-claude',"
            " 'claude', ?, ?, 'PASS', 1)",
            (REPO, sha, self.authored["id"], reviewer, reviewer_family),
        )

    def test_record_review_looks_up_desks_and_families(self):
        review = self.record("codex-one", "PASS", review_path=review_file())
        self.assertRegex(review["id"], r"^rv_[0-9a-f]{16}$")
        self.assertEqual((review["author_desk"], review["author_family"]), ("ryan-claude", "claude"))
        self.assertEqual((review["reviewer_desk"], review["reviewer_family"]), ("codex-one", "codex"))
        self.assertEqual(review["review_path"], review_file())

    def test_caller_cannot_supply_a_family(self):
        with self.assertRaises(TypeError):
            owlery.record_review(self.conn, REPO, SHA, self.authored["id"], "claude-two", "PASS",
                                 reviewer_family="codex")
        with self.assertRaises(TypeError):
            owlery.record_review(self.conn, REPO, SHA, self.authored["id"], "claude-two", "PASS",
                                 author_family="codex")

    def test_same_family_pass_raises_integrity_error(self):
        with self.assertRaises(IntegrityError):
            self.record("claude-two", "PASS")
        with self.assertRaises(IntegrityError):
            self.record("ryan-claude", "PASS")
        self.assertEqual(self.count("review_passes"), 0)

    def test_same_family_changes_is_recorded(self):
        self.assertEqual(self.record("claude-two", "CHANGES")["verdict"], "CHANGES")

    def test_pass_must_cite_the_task_that_recorded_the_commit(self):
        unrelated = self.started("codex-two")
        with self.assertRaises(IntegrityError):
            self.record("claude-two", "PASS", task_id=unrelated["id"])
        with self.assertRaises(IntegrityError):
            self.record("codex-one", "CHANGES", sha=OTHER_SHA)
        self.assertEqual(self.count("review_passes"), 0)
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))

    def test_script_desks_cannot_pass(self):
        with self.assertRaises(IntegrityError):
            self.record("lint-bot", "PASS")
        self.assertEqual(self.record("lint-bot", "CHANGES")["verdict"], "CHANGES")
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))

    def test_pass_needs_a_review_request_unless_the_reviewer_is_human(self):
        with self.assertRaises(IntegrityError):
            self.record("codex-two", "PASS")
        self.assertEqual(self.record("ryan", "PASS")["reviewer_family"], "human")
        self.assertTrue(owlery.has_pass(self.conn, REPO, SHA))

    def test_declined_review_request_does_not_authorise_a_pass(self):
        task = self.started("ryan-claude")
        request = self.request_review(task["id"], "codex-two")["request"]
        pensieve.record_commit(self.conn, task["id"], REPO, OTHER_SHA)
        owlery.decline(self.conn, request["id"], "conflict")
        with self.assertRaises(IntegrityError):
            self.record("codex-two", "PASS", sha=OTHER_SHA, task_id=task["id"])

    def test_database_check_blocks_a_same_family_pass(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_review("claude-two", "claude")

    def test_database_refuses_a_script_pass(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_review("lint-bot", "script")

    def test_database_refuses_a_review_of_an_unrecorded_commit(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_review("codex-one", "codex", sha=OTHER_SHA)

    def test_has_pass_true_for_latest_cross_family_pass(self):
        self.record("codex-one", "CHANGES", now=NOW)
        self.record("ryan", "PASS", now=NOW + 1)
        self.assertTrue(owlery.has_pass(self.conn, REPO, SHA))

    def test_has_pass_false_when_latest_is_not_pass(self):
        self.record("codex-one", "PASS", now=NOW)
        self.record("codex-one", "HEADMASTER", now=NOW + 1)
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))
        self.record("codex-one", "PASS", now=NOW + 2)
        self.record("claude-two", "CHANGES", now=NOW + 2)
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))

    def test_has_pass_false_without_reviews_or_for_other_shas(self):
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))
        self.record("codex-one", "PASS")
        self.assertFalse(owlery.has_pass(self.conn, REPO, OTHER_SHA))
        self.assertFalse(owlery.has_pass(self.conn, "acme/other", SHA))

    def test_has_pass_needs_awaiting_close_or_complete(self):
        task = self.started("ryan-claude")
        self.request_review(task["id"], "codex-one")
        pensieve.record_commit(self.conn, task["id"], REPO, OTHER_SHA)
        self.record("codex-one", "PASS", sha=OTHER_SHA, task_id=task["id"])
        self.assertFalse(owlery.has_pass(self.conn, REPO, OTHER_SHA))
        pensieve.mark_awaiting_close(self.conn, task["id"])
        self.assertTrue(owlery.has_pass(self.conn, REPO, OTHER_SHA))
        self.close_complete(task["id"])
        self.assertTrue(owlery.has_pass(self.conn, REPO, OTHER_SHA))

    def test_has_pass_ignores_abandoned_tasks(self):
        self.record("codex-one", "PASS")
        self.assertTrue(owlery.has_pass(self.conn, REPO, SHA))
        pensieve.close_task(self.conn, self.authored["id"], "abandoned")
        self.assertFalse(owlery.has_pass(self.conn, REPO, SHA))

    def test_review_inputs_are_validated(self):
        task_id = self.authored["id"]
        bad_calls = (
            ("acme", SHA, task_id, "codex-one", "PASS"),
            (REPO, "abc", task_id, "codex-one", "PASS"),
            (REPO, SHA, "tk_bad", "codex-one", "PASS"),
            (REPO, SHA, task_id, "Codex", "PASS"),
            (REPO, SHA, task_id, "codex-one", "pass"),
        )
        for args in bad_calls:
            with self.subTest(args=args):
                with self.assertRaises(ValidationError):
                    owlery.record_review(self.conn, *args)
        with self.assertRaises(ValidationError):
            self.record("codex-one", "PASS", review_path="/tmp/review.md")
        with self.assertRaises(ValidationError):
            owlery.has_pass(self.conn, REPO, "A" * 40)

    def test_review_path_root_is_the_office_which_no_desk_can_write(self):
        for path in ("/Users/crisryantan/hogwarts/reviews/review.md",
                     "/Users/crisryantan/hogwarts-fleet/reviews/review.md",
                     "/Users/crisryantan/.hogwarts/state/pensieve.db",
                     "/Users/crisryantan/.hogwarts/reviews/../state/pensieve.db",
                     "/Users/crisryantan/.hogwarts/reviews"):
            with self.subTest(path=path):
                with self.assertRaises(ValidationError):
                    self.record("codex-one", "PASS", review_path=path)
        self.assertEqual(self.count("review_passes"), 0)
        path = "/Users/crisryantan/.hogwarts/reviews/acme-web-app/review.md"
        self.assertEqual(self.record("codex-one", "PASS", review_path=path)["review_path"], path)

    def test_unknown_task_or_reviewer_is_not_found(self):
        with self.assertRaises(NotFoundError):
            owlery.record_review(self.conn, REPO, SHA, "tk_0000000000000000", "codex-one", "PASS")
        with self.assertRaises(NotFoundError):
            self.record("nobody", "PASS")

    def test_review_passes_are_immutable(self):
        review = self.record("codex-one", "CHANGES")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE review_passes SET verdict = 'PASS' WHERE id = ?", (review["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM review_passes WHERE id = ?", (review["id"],))

    def test_desks_cannot_change_family(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE desks SET family = 'codex' WHERE name = 'claude-two'")


if __name__ == "__main__":
    unittest.main()
