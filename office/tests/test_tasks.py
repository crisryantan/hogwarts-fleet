from __future__ import annotations

import multiprocessing
import sqlite3
import unittest

from hogwarts import db, owlery, pensieve
from hogwarts.errors import ConflictError, NotFoundError, TokenError, ValidationError
from tests.support import NOW, REPO, SHA, StoreCase, intent_file, worktree

TASK_ID = "tk_0123456789abcdef"


def _race_worker(db_path: str, task_id: str, barrier, results) -> None:
    conn = db.connect(db_path, create=False)
    try:
        barrier.wait(timeout=20)
        try:
            pensieve.start_task(conn, task_id)
            results.put(("ok", task_id))
        except ConflictError:
            results.put(("conflict", task_id))
        except Exception as exc:
            results.put(("error", repr(exc)))
    finally:
        conn.close()


class TaskLifecycleTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_create_task_is_queued(self):
        task = self.task(task_id=TASK_ID, intent_path=intent_file(TASK_ID), worktree=worktree(),
                         session_id="session-1")
        self.assertEqual(task["status"], "queued")
        self.assertIsNone(task["started_at"])
        self.assertEqual((task["id"], task["intent_path"]),
                         (TASK_ID, "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/TASK.md"))
        self.assertEqual(task["worktree"], "/Users/crisryantan/hogwarts/worktrees/wt")

    def test_intent_path_and_worktree_must_stay_under_their_roots(self):
        for kwargs in ({"task_id": TASK_ID, "intent_path": intent_file("tk_00000000000000ff")},
                       {"task_id": TASK_ID, "intent_path": "/tmp/intent.md"},
                       {"task_id": TASK_ID, "intent_path": "/Users/crisryantan/hogwarts/desks/alpha/TASK.md"},
                       {"task_id": TASK_ID,
                        "intent_path": "/Users/crisryantan/hogwarts-fleet/tasks/tk_0123456789abcdef/TASK.md"},
                       {"worktree": "/Users/crisryantan/.ssh"}, {"worktree": intent_file(TASK_ID)},
                       {"worktree": "/Users/crisryantan/hogwarts-fleet/worktrees/wt"},
                       {"worktree": "/Users/crisryantan/hogwarts/worktrees"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    self.task(**kwargs)
        self.assertEqual(self.count("tasks"), 0)

    def test_intent_path_must_be_the_tasks_own_task_md(self):
        with self.assertRaisesRegex(ValidationError, "pass that task's id"):
            self.task(intent_path=intent_file(TASK_ID))
        with self.assertRaisesRegex(ValidationError, "intent path must be " + intent_file(TASK_ID)):
            self.task(task_id=TASK_ID, intent_path=intent_file("tk_00000000000000ff"))
        self.assertEqual(self.count("tasks"), 0)
        self.assertEqual(self.task(task_id=TASK_ID, intent_path=intent_file(TASK_ID))["intent_path"],
                         intent_file(TASK_ID))

    def test_create_task_takes_a_caller_minted_id_once(self):
        self.assertEqual(self.task(task_id=TASK_ID)["id"], TASK_ID)
        with self.assertRaisesRegex(ConflictError, "already exists"):
            self.task("beta", task_id=TASK_ID)
        for bad in ("TK_0123456789ABCDEF", "tk_0123", "rq_0123456789abcdef", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.task(task_id=bad)
        self.assertRegex(self.task()["id"], r"^tk_[0-9a-f]{16}$")
        self.assertEqual(self.count("tasks"), 2)

    def test_tasks_must_be_inserted_queued(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO tasks(id, desk, title, status, close_reason, created_at, closed_at)"
                              " VALUES ('tk_00000000000000aa', 'alpha', 't', 'closed', 'complete', 1, 1)")

    def test_database_refuses_complete_without_a_consumed_token(self):
        task = self.started()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'closed', close_reason = 'complete', closed_at = 1"
                              " WHERE id = ?", (task["id"],))
        owlery.mint(self.conn, task["id"], "cli", now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'closed', close_reason = 'complete', closed_at = 1"
                              " WHERE id = ?", (task["id"],))
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "active")

    def test_desk_role_takes_a_display_name(self):
        desk = pensieve.add_desk(self.conn, "delta", "claude", role="Snape - Data Analyst", now=NOW)
        self.assertEqual(desk["role"], "Snape - Data Analyst")
        with self.assertRaises(ValidationError):
            pensieve.add_desk(self.conn, "epsilon", "claude", role="bad\nrole", now=NOW)

    def test_create_task_requires_registered_desk(self):
        with self.assertRaises(NotFoundError):
            self.task("gamma")

    def test_create_task_requires_existing_open_parent(self):
        with self.assertRaises(NotFoundError):
            self.task(parent_task_id="tk_0000000000000000")
        parent = self.task()
        pensieve.close_task(self.conn, parent["id"], "abandoned", now=NOW)
        with self.assertRaises(ConflictError):
            self.task(parent_task_id=parent["id"])

    def test_create_task_requires_existing_request(self):
        with self.assertRaises(NotFoundError):
            self.task(request_id="rq_0000000000000000")

    def test_start_task_moves_queued_to_active(self):
        task = pensieve.start_task(self.conn, self.task()["id"], now=NOW + 5)
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["started_at"], NOW + 5)

    def test_only_queued_tasks_can_start(self):
        task = self.started()
        with self.assertRaises(ConflictError):
            pensieve.start_task(self.conn, task["id"])
        pensieve.mark_awaiting_close(self.conn, task["id"])
        with self.assertRaises(ConflictError):
            pensieve.start_task(self.conn, task["id"])

    def test_second_active_task_on_a_desk_conflicts(self):
        self.started()
        second = self.task()
        with self.assertRaises(ConflictError):
            pensieve.start_task(self.conn, second["id"])
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")

    def test_one_active_task_per_session(self):
        self.started("alpha", session_id="session-aaaa1")
        same_session = self.task("beta", session_id="session-aaaa1")
        with self.assertRaises(ConflictError):
            pensieve.start_task(self.conn, same_session["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'active', started_at = 1 WHERE id = ?", (same_session["id"],))
        self.assertEqual(self.started("beta", session_id="session-bbbb2")["status"], "active")

    def test_start_task_refuses_a_task_whose_ancestor_is_closed(self):
        parent = self.task("alpha")
        child = self.task("beta", parent_task_id=parent["id"])
        grandchild = self.task("alpha", parent_task_id=child["id"])
        self.conn.execute("UPDATE tasks SET status = 'closed', close_reason = 'abandoned', closed_at = 1"
                          " WHERE id = ?", (parent["id"],))
        for task in (child, grandchild):
            with self.subTest(task=task["id"]):
                with self.assertRaises(ConflictError):
                    pensieve.start_task(self.conn, task["id"])
                self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "queued")

    def test_other_desks_can_start_in_parallel(self):
        self.started("alpha")
        self.assertEqual(self.started("beta")["status"], "active")

    def test_partial_unique_index_blocks_two_active_tasks(self):
        self.started()
        second = self.task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'active', started_at = 1 WHERE id = ?", (second["id"],))

    def test_mark_awaiting_close_frees_the_desk(self):
        first = self.started()
        awaiting = pensieve.mark_awaiting_close(self.conn, first["id"])
        self.assertEqual(awaiting["status"], "awaiting_close")
        self.assertEqual(self.started()["status"], "active")

    def test_only_active_tasks_can_await_close(self):
        with self.assertRaises(ConflictError):
            pensieve.mark_awaiting_close(self.conn, self.task()["id"])

    def test_mark_awaiting_close_records_the_head_commit(self):
        task = self.started()
        pensieve.mark_awaiting_close(self.conn, task["id"], REPO, SHA, now=NOW + 3)
        commit = pensieve.get_commit(self.conn, REPO, SHA)
        self.assertEqual((commit["task_id"], commit["recorded_at"]), (task["id"], NOW + 3))
        with self.assertRaises(ValidationError):
            pensieve.mark_awaiting_close(self.conn, self.started("beta")["id"], REPO)

    def test_a_commit_belongs_to_one_task(self):
        first, second = self.started("alpha"), self.started("beta")
        self.assertTrue(pensieve.record_commit(self.conn, first["id"], REPO, SHA)["created"])
        self.assertFalse(pensieve.record_commit(self.conn, first["id"], REPO, SHA)["created"])
        with self.assertRaises(ConflictError):
            pensieve.record_commit(self.conn, second["id"], REPO, SHA)
        with self.assertRaises(ConflictError):
            pensieve.record_commit(self.conn, self.task("alpha")["id"], REPO, "f" * 40)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE task_commits SET task_id = ?", (second["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM task_commits")

    def test_close_complete_requires_a_token(self):
        task = self.started()
        with self.assertRaises(TokenError):
            pensieve.close_task(self.conn, task["id"], "complete")
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["status"], "active")

    def test_close_complete_with_token(self):
        task = self.started()
        closed = self.close_complete(task["id"], now=NOW + 9)
        self.assertEqual((closed["status"], closed["close_reason"], closed["closed_at"]),
                         ("closed", "complete", NOW + 9))

    def test_close_complete_needs_an_active_or_awaiting_task(self):
        with self.assertRaises(ConflictError):
            pensieve.close_task(self.conn, self.task()["id"], "complete", "x" * 43)

    def test_abandoned_and_superseded_need_no_token_and_are_recorded(self):
        for reason in ("abandoned", "superseded"):
            with self.subTest(reason=reason):
                task = self.task()
                closed = pensieve.close_task(self.conn, task["id"], reason, now=NOW)
                self.assertEqual((closed["status"], closed["close_reason"]), ("closed", reason))

    def test_close_reason_is_validated(self):
        with self.assertRaises(ValidationError):
            pensieve.close_task(self.conn, self.task()["id"], "done")

    def test_closed_task_never_reopens(self):
        task = self.task()
        pensieve.close_task(self.conn, task["id"], "abandoned")
        with self.assertRaises(ConflictError):
            pensieve.start_task(self.conn, task["id"])
        with self.assertRaises(ConflictError):
            pensieve.close_task(self.conn, task["id"], "superseded")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'queued', close_reason = NULL, closed_at = NULL"
                              " WHERE id = ?", (task["id"],))

    def test_tasks_are_never_deleted(self):
        task = self.task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))

    def test_list_tasks_filters_by_desk_and_status(self):
        self.started("alpha")
        self.task("alpha")
        self.task("beta")
        self.assertEqual(len(pensieve.list_tasks(self.conn)), 3)
        self.assertEqual(len(pensieve.list_tasks(self.conn, desk="alpha")), 2)
        self.assertEqual(len(pensieve.list_tasks(self.conn, desk="alpha", status="queued")), 1)
        with self.assertRaises(ValidationError):
            pensieve.list_tasks(self.conn, status="running")


class CascadeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.desk("gamma", "codex")
        self.desk("delta", "human")

    def test_close_cascades_complete_to_open_descendants(self):
        parent = self.started("alpha")
        child = self.started("beta", parent_task_id=parent["id"])
        grandchild = self.started("gamma", parent_task_id=child["id"])
        pensieve.mark_awaiting_close(self.conn, grandchild["id"])
        self.close_complete(parent["id"], now=NOW + 50)
        for task in (child, grandchild):
            closed = pensieve.get_task(self.conn, task["id"])
            self.assertEqual((closed["status"], closed["close_reason"], closed["closed_at"]),
                             ("closed", "complete", NOW + 50))

    def test_closing_a_parent_supersedes_queued_children(self):
        parent = self.started("alpha")
        queued = self.task("delta", parent_task_id=parent["id"])
        self.close_complete(parent["id"], now=NOW + 50)
        closed = pensieve.get_task(self.conn, queued["id"])
        self.assertEqual((closed["status"], closed["close_reason"]), ("closed", "superseded"))

    def test_complete_cascade_supersedes_work_below_a_queued_child(self):
        parent = self.started("alpha")
        queued = self.task("beta", parent_task_id=parent["id"])
        below = self.started("gamma", parent_task_id=queued["id"])
        self.close_complete(parent["id"], now=NOW + 50)
        self.assertEqual(pensieve.get_task(self.conn, below["id"])["close_reason"], "superseded")

    def test_cascade_from_abandoned_parent_supersedes_children(self):
        parent = self.started("alpha")
        child = self.started("beta", parent_task_id=parent["id"])
        grandchild = self.started("gamma", parent_task_id=child["id"])
        pensieve.close_task(self.conn, parent["id"], "abandoned")
        for task in (child, grandchild):
            self.assertEqual(pensieve.get_task(self.conn, task["id"])["close_reason"], "superseded")
        self.assertEqual(self.count("close_tokens"), 0)

    def test_cascade_advances_descendant_requests_to_task_closed(self):
        parent = self.started("alpha")
        opened = owlery.open_request(self.conn, "alpha", "beta", "check it", parent_task_id=parent["id"], now=NOW)
        request_id = opened["request"]["id"]
        owlery.advance(self.conn, request_id, "claimed", now=NOW)
        pensieve.start_task(self.conn, opened["task"]["id"], now=NOW)
        owlery.advance(self.conn, request_id, "running", now=NOW)
        self.close_complete(parent["id"], now=NOW + 60)
        request = owlery.get_request(self.conn, request_id)
        self.assertEqual((request["phase"], request["outcome"]), ("task_closed", "done"))
        self.assertEqual(request["history"][-1]["phase"], "task_closed")
        self.assertIn(parent["id"], request["history"][-1]["detail"])

    def test_cascade_from_abandoned_parent_leaves_request_outcome_unset(self):
        parent = self.started("alpha")
        running = owlery.open_request(self.conn, "alpha", "beta", "running child", parent_task_id=parent["id"])
        queued = owlery.open_request(self.conn, "alpha", "gamma", "queued child", parent_task_id=parent["id"])
        owlery.advance(self.conn, running["request"]["id"], "claimed")
        pensieve.start_task(self.conn, running["task"]["id"])
        owlery.advance(self.conn, running["request"]["id"], "running")
        pensieve.close_task(self.conn, parent["id"], "abandoned")
        for opened in (running, queued):
            with self.subTest(request=opened["request"]["title"]):
                request = owlery.get_request(self.conn, opened["request"]["id"])
                self.assertEqual((request["phase"], request["outcome"]), ("task_closed", None))
                self.assertIn(parent["id"], request["history"][-1]["detail"])
                task = pensieve.get_task(self.conn, opened["task"]["id"])
                self.assertEqual((task["status"], task["close_reason"]), ("closed", "superseded"))

    def test_failed_cascade_rolls_back_the_whole_close(self):
        parent = self.started("alpha")
        child = self.started("beta", parent_task_id=parent["id"])
        token = owlery.mint(self.conn, parent["id"], "cli", now=NOW)["token"]
        original = pensieve._set_closed

        def flaky(conn, task_id, reason, ts):
            if task_id == child["id"]:
                raise RuntimeError("disk full")
            original(conn, task_id, reason, ts)

        pensieve._set_closed = flaky
        self.addCleanup(setattr, pensieve, "_set_closed", original)
        with self.assertRaises(RuntimeError):
            pensieve.close_task(self.conn, parent["id"], "complete", token, now=NOW)
        self.assertEqual(pensieve.get_task(self.conn, parent["id"])["status"], "active")
        self.assertIsNone(self.conn.execute("SELECT consumed_at FROM close_tokens").fetchone()[0])


class StartRaceTests(StoreCase):
    def test_two_processes_racing_start_task_exactly_one_succeeds(self):
        context = multiprocessing.get_context("spawn")
        for round_number in range(3):
            desk = f"racer-{round_number}"
            self.desk(desk, "claude")
            first, second = self.task(desk)["id"], self.task(desk)["id"]
            barrier = context.Barrier(2)
            results = context.Queue()
            workers = [
                context.Process(target=_race_worker, args=(str(self.db_path), task_id, barrier, results))
                for task_id in (first, second)
            ]
            for worker in workers:
                worker.start()
            outcomes = sorted(results.get(timeout=60) for _ in workers)
            for worker in workers:
                worker.join(timeout=60)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([kind for kind, _ in outcomes], ["conflict", "ok"], outcomes)
            active = pensieve.list_tasks(self.conn, desk=desk, status="active")
            self.assertEqual(len(active), 1)
            winner = next(value for kind, value in outcomes if kind == "ok")
            self.assertEqual(active[0]["id"], winner)


if __name__ == "__main__":
    unittest.main()


class WorktreeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()

    def test_a_worktree_attaches_once_to_an_open_task(self):
        task = self.task()
        attached = pensieve.set_worktree(self.conn, task["id"], worktree("tk-one"))
        self.assertEqual(attached["worktree"], worktree("tk-one"))
        self.assertEqual(pensieve.set_worktree(self.conn, task["id"], worktree("tk-one"))["worktree"],
                         worktree("tk-one"))
        with self.assertRaises(ConflictError):
            pensieve.set_worktree(self.conn, task["id"], worktree("tk-two"))
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["worktree"], worktree("tk-one"))

    def test_a_worktree_must_sit_under_the_castle_worktrees(self):
        task = self.task()
        for path in ("/Users/crisryantan/hogwarts/tasks/x", "/Users/crisryantan/.hogwarts/x",
                     worktree("a/../b"), "relative/path", worktree("x") + "\n"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                pensieve.set_worktree(self.conn, task["id"], path)
        self.assertIsNone(pensieve.get_task(self.conn, task["id"])["worktree"])

    def test_a_task_awaiting_close_or_closed_takes_no_worktree(self):
        task = self.task()
        pensieve.start_task(self.conn, task["id"])
        pensieve.mark_awaiting_close(self.conn, task["id"])
        with self.assertRaises(ConflictError):
            pensieve.set_worktree(self.conn, task["id"], worktree())
        with self.assertRaises(NotFoundError):
            pensieve.set_worktree(self.conn, "tk_ffffffffffffffff", worktree())
