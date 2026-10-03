from __future__ import annotations

import unittest

from hogwarts import db, owlery, pensieve
from tests.support import DAY, HOUR, NOW, REPO, SHA, StoreCase


class PurgeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def owl(self, body, created, acked=True):
        sent = owlery.send(self.conn, "alpha", "beta", "fyi", "note", body=body, now=created)
        if acked:
            owlery.read(self.conn, sent["id"], "beta", now=created)
            owlery.ack(self.conn, sent["id"], "beta", now=created)
        return sent

    def body(self, owl_id):
        return self.conn.execute("SELECT body, purged_at FROM owls WHERE id = ?", (owl_id,)).fetchone()

    def test_purge_clears_old_acked_bodies_only(self):
        old_acked = self.owl("old acked", NOW - 31 * DAY)
        old_unacked = self.owl("old unacked", NOW - 31 * DAY, acked=False)
        recent = self.owl("recent acked", NOW - 29 * DAY)
        result = owlery.purge(self.conn, now=NOW)
        self.assertEqual(result["owl_bodies_purged"], 1)
        self.assertEqual(tuple(self.body(old_acked["id"])), (None, NOW))
        self.assertEqual(self.body(old_unacked["id"])["body"], "old unacked")
        self.assertEqual(self.body(recent["id"])["body"], "recent acked")
        self.assertEqual(owlery.purge(self.conn, now=NOW)["owl_bodies_purged"], 0)
        self.assertEqual(owlery.read(self.conn, old_acked["id"], "beta")["purged_at"], NOW)

    def test_purge_deletes_old_extracts_and_their_fts_rows(self):
        pensieve.record_session(self.conn, "session-old1", "proj", started_at=NOW - 100 * DAY)
        pensieve.record_session(self.conn, "session-new1", "proj", started_at=NOW - DAY)
        pensieve.add_extract(self.conn, "session-old1", "user", "forgotten words", now=NOW - 91 * DAY)
        pensieve.add_extract(self.conn, "session-new1", "user", "remembered words", now=NOW - DAY)
        result = owlery.purge(self.conn, now=NOW)
        self.assertEqual(result["extracts_deleted"], 1)
        self.assertEqual(pensieve.find(self.conn, "forgotten"), [])
        self.assertEqual(len(pensieve.find(self.conn, "remembered")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extracts_fts").fetchone()[0], 1)

    def test_purge_keeps_fresh_extracts_in_an_old_open_session(self):
        pensieve.record_session(self.conn, "session-old1", "proj", started_at=NOW - 100 * DAY)
        pensieve.add_extract(self.conn, "session-old1", "user", "stale zebra", now=NOW - 95 * DAY)
        pensieve.add_extract(self.conn, "session-old1", "user", "fresh zebra", now=NOW - 60)
        self.assertEqual(owlery.purge(self.conn, now=NOW)["extracts_deleted"], 1)
        self.assertEqual([hit["snippet"] for hit in pensieve.find(self.conn, "zebra")], ["fresh **zebra**"])

    def test_purge_truncates_the_wal_while_another_connection_is_open(self):
        broker = db.connect(self.db_path)
        self.addCleanup(broker.close)
        marker = "PURGE-ME-" + "q" * 24
        self.owl(marker, NOW - 40 * DAY)
        result = owlery.purge(self.conn, now=NOW)
        self.assertEqual((result["owl_bodies_purged"], result["wal_checkpoint_busy"]), (1, False))
        for suffix in ("", "-wal"):
            with self.subTest(file="pensieve.db" + suffix):
                path = str(self.db_path) + suffix
                with open(path, "rb") as handle:
                    self.assertNotIn(marker.encode(), handle.read())

    def test_purge_reports_a_busy_checkpoint(self):
        reader = db.connect(self.db_path)
        self.addCleanup(reader.close)
        self.owl("old body", NOW - 40 * DAY)
        reader.execute("PRAGMA busy_timeout=0")
        self.conn.execute("PRAGMA busy_timeout=0")
        with db.snapshot(reader):
            reader.execute("SELECT COUNT(*) FROM owls").fetchone()
            result = owlery.purge(self.conn, now=NOW)
        self.assertTrue(result["wal_checkpoint_busy"])

    def test_purge_never_touches_facts_tasks_events_or_reviews(self):
        self.desk("gamma", "human")
        task = self.started("alpha", now=NOW - 400 * DAY)
        pensieve.mark_awaiting_close(self.conn, task["id"], REPO, SHA, now=NOW - 400 * DAY)
        pensieve.add_fact(self.conn, "fleet", "old fact", "pinned", "ryan", now=NOW - 400 * DAY)
        pensieve.add_event(self.conn, "alpha", "note", "routine", "old event", now=NOW - 400 * DAY)
        owlery.record_review(self.conn, REPO, SHA, task["id"], "gamma", "PASS", now=NOW - 400 * DAY)
        pensieve.add_keypoint(self.conn, "old keypoint", now=NOW - 400 * DAY)
        before = [self.count(table) for table in ("facts", "tasks", "events", "review_passes", "keypoints")]
        owlery.purge(self.conn, now=NOW, body_days=1, extract_days=1)
        after = [self.count(table) for table in ("facts", "tasks", "events", "review_passes", "keypoints")]
        self.assertEqual(before, after)


class AuditTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_audit_lists_stale_requests(self):
        opened = owlery.open_request(self.conn, "alpha", "beta", "stale one", now=NOW - 2 * HOUR)
        owlery.open_request(self.conn, "alpha", "beta", "fresh one", now=NOW - 10 * 60)
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["stale_requests"]], [opened["request"]["id"]])

    def test_audit_owl_rering_and_escalate_lists(self):
        rering = owlery.send(self.conn, "alpha", "beta", "fyi", "rering me", now=NOW - HOUR)
        owlery.mark_delivered(self.conn, rering["id"], now=NOW - 45 * 60)
        escalate = owlery.send(self.conn, "alpha", "beta", "fyi", "escalate me", now=NOW - 5 * HOUR)
        owlery.mark_delivered(self.conn, escalate["id"], now=NOW - 3 * HOUR)
        fresh = owlery.send(self.conn, "alpha", "beta", "fyi", "fresh", now=NOW)
        owlery.mark_delivered(self.conn, fresh["id"], now=NOW - 60)
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["rering_owls"]], [rering["id"]])
        self.assertEqual([row["id"] for row in report["escalate_owls"]], [escalate["id"]])
        self.assertTrue(all("body" not in row for row in report["rering_owls"] + report["escalate_owls"]))

    def test_audit_lists_owls_that_were_never_delivered(self):
        undelivered = owlery.send(self.conn, "alpha", "beta", "fyi", "never delivered", body="b", now=NOW - 5 * HOUR)
        owlery.send(self.conn, "alpha", "beta", "fyi", "just sent", now=NOW - 60)
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["undelivered_owls"]], [undelivered["id"]])
        self.assertNotIn(undelivered["id"], [row["id"] for row in report["rering_owls"] + report["escalate_owls"]])
        self.assertNotIn("body", report["undelivered_owls"][0])
        first = owlery.audit(self.conn, now=NOW, escalate=True)["escalated"]
        again = owlery.audit(self.conn, now=NOW + 60, escalate=True)["escalated"]
        self.assertEqual((first["created"], again["created"]), (1, 0))
        self.assertEqual(pensieve.drain(self.conn)["events"][0]["kind"], "audit.undelivered-owl")

    def test_audit_lists_queued_tasks_whose_parent_is_closed(self):
        parent = self.task("alpha")
        orphan = self.task("beta", parent_task_id=parent["id"])
        self.conn.execute("UPDATE tasks SET status = 'closed', close_reason = 'abandoned', closed_at = 1"
                          " WHERE id = ?", (parent["id"],))
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["orphan_queued_tasks"]], [orphan["id"]])
        escalated = owlery.audit(self.conn, now=NOW, escalate=True)["escalated"]
        self.assertEqual(escalated["created"], 1)
        self.assertEqual(pensieve.drain(self.conn)["events"][0]["kind"], "audit.orphan-queued-task")

    def test_acked_owls_are_not_reported(self):
        owl = owlery.send(self.conn, "alpha", "beta", "fyi", "seen", now=NOW - 5 * HOUR)
        owlery.read(self.conn, owl["id"], "beta", now=NOW - 4 * HOUR)
        owlery.ack(self.conn, owl["id"], "beta", now=NOW - 4 * HOUR)
        self.assertEqual(owlery.audit(self.conn, now=NOW)["escalate_owls"], [])

    def test_audit_long_active_and_stale_awaiting_close_tasks(self):
        long_active = self.started("alpha", now=NOW - 9 * HOUR)
        waiting = self.started("beta", now=NOW - 25 * HOUR)
        pensieve.mark_awaiting_close(self.conn, waiting["id"])
        self.desk("gamma", "codex")
        self.started("gamma", now=NOW - HOUR)
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["long_active_tasks"]], [long_active["id"]])
        self.assertEqual([row["id"] for row in report["stale_awaiting_close"]], [waiting["id"]])

    def test_audit_lists_review_passes_whose_task_is_missing(self):
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "INSERT INTO task_commits(repo, sha, task_id, recorded_at) VALUES (?, ?, 'tk_00000000000000aa', 1)",
            (REPO, SHA),
        )
        self.conn.execute(
            "INSERT INTO review_passes(id, repo, sha, task_id, author_desk, author_family, reviewer_desk,"
            " reviewer_family, verdict, created_at) VALUES ('rv_00000000000000aa', ?, ?, 'tk_00000000000000aa',"
            " 'alpha', 'claude', 'beta', 'codex', 'PASS', 1)",
            (REPO, SHA),
        )
        self.conn.execute("PRAGMA foreign_keys=ON")
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual([row["id"] for row in report["orphan_reviews"]], ["rv_00000000000000aa"])

    def test_audit_without_escalate_is_read_only(self):
        owlery.open_request(self.conn, "alpha", "beta", "stale", now=NOW - 2 * HOUR)
        self.started("alpha", now=NOW - 9 * HOUR)
        before = self.conn.total_changes
        report = owlery.audit(self.conn, now=NOW)
        self.assertEqual(self.conn.total_changes, before)
        self.assertNotIn("escalated", report)
        self.assertEqual(self.count("events"), 0)

    def test_audit_escalate_inserts_headmaster_events_once(self):
        owlery.open_request(self.conn, "alpha", "beta", "stale", now=NOW - 2 * HOUR)
        self.started("alpha", now=NOW - 9 * HOUR)
        first = owlery.audit(self.conn, now=NOW, escalate=True)
        self.assertEqual(first["escalated"], {"created": 3, "skipped": 0})
        events = pensieve.drain(self.conn)["events"]
        self.assertEqual(sorted(event["kind"] for event in events),
                         ["audit.long-active-task", "audit.stale-request", "audit.undelivered-owl"])
        second = owlery.audit(self.conn, now=NOW + 60, escalate=True)
        self.assertEqual(second["escalated"]["created"], 0)
        self.assertEqual(self.count("events"), 3)


if __name__ == "__main__":
    unittest.main()
