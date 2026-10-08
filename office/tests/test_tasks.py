from __future__ import annotations

import multiprocessing
import sqlite3
import unittest

from hogwarts import db, owlery, pensieve
from hogwarts.errors import ConflictError, NotFoundError, TokenError, ValidationError
from tests.support import MERGE_SHA, NOW, REPO, SHA, StoreCase, go_build, intent_file, proof, worktree

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
        pensieve.record_commit(self.conn, first["id"], REPO, "e" * 40, now=NOW + 1)
        self.assertEqual([row["sha"] for row in pensieve.task_commits(self.conn, first["id"])], [SHA, "e" * 40])
        self.assertEqual(pensieve.task_commits(self.conn, second["id"]), [])
        # A slug in another letter case is another row; commits_with_sha finds both, oldest first.
        pensieve.record_commit(self.conn, second["id"], REPO.upper(), SHA, now=NOW + 2)
        self.assertEqual([(row["repo"], row["task_id"]) for row in pensieve.commits_with_sha(self.conn, SHA)],
                         [(REPO, first["id"]), (REPO.upper(), second["id"])])
        self.assertEqual(pensieve.commits_with_sha(self.conn, "d" * 40), [])
        with self.assertRaises(ValidationError):
            pensieve.commits_with_sha(self.conn, "HEAD")
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


class ManyTaskDeskTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        pensieve.allow_many_tasks(self.conn, "beta", now=NOW)

    def test_a_many_task_desk_starts_three_tasks(self):
        started = [self.started("beta")["id"] for _ in range(3)]
        self.assertEqual([task["id"] for task in pensieve.list_tasks(self.conn, desk="beta", status="active")], started)
        self.assertIsNone(pensieve.blocking_task(self.conn, "beta"))

    def test_a_single_desk_still_refuses_through_the_api_and_a_raw_write(self):
        first = self.started("alpha")
        self.assertEqual(pensieve.blocking_task(self.conn, "alpha")["id"], first["id"])
        second = self.task("alpha")
        with self.assertRaisesRegex(ConflictError, f"desk already has an active task {first['id']}"):
            pensieve.start_task(self.conn, second["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "desk already has an active task"):
            self.conn.execute("UPDATE tasks SET status = 'active', started_at = 1 WHERE id = ?", (second["id"],))
        self.assertEqual(pensieve.get_task(self.conn, second["id"])["status"], "queued")

    def test_a_raw_write_on_a_many_task_desk_is_allowed(self):
        self.started("beta")
        second = self.task("beta")
        self.conn.execute("UPDATE tasks SET status = 'active', started_at = 1 WHERE id = ?", (second["id"],))
        self.assertEqual(len(pensieve.list_tasks(self.conn, desk="beta", status="active")), 2)

    def test_the_session_rule_still_holds_on_a_many_task_desk(self):
        self.started("beta", session_id="session-aaaa1")
        same = self.task("beta", session_id="session-aaaa1")
        with self.assertRaisesRegex(ConflictError, "session already has an active task"):
            pensieve.start_task(self.conn, same["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status = 'active', started_at = 1 WHERE id = ?", (same["id"],))
        self.assertEqual(self.started("beta", session_id="session-bbbb2")["status"], "active")

    def test_the_grant_is_one_way_and_idempotent(self):
        again = pensieve.allow_many_tasks(self.conn, "beta", now=NOW + 5)
        self.assertEqual((again["name"], again["many_tasks"], again["created"]), ("beta", 1, False))
        self.assertEqual(self.conn.execute("SELECT granted_at FROM many_task_desks WHERE desk = 'beta'").fetchone()[0],
                         NOW)
        self.assertEqual({desk["name"]: desk["many_tasks"] for desk in pensieve.list_desks(self.conn)},
                         {"alpha": 0, "beta": 1})
        with self.assertRaisesRegex(sqlite3.IntegrityError, "desk task modes are fixed"):
            self.conn.execute("UPDATE many_task_desks SET desk = 'alpha'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "desk task modes are never deleted"):
            self.conn.execute("DELETE FROM many_task_desks")
        self.assertTrue(pensieve.takes_many_tasks(self.conn, "beta"))

    def test_an_unknown_or_reserved_desk_is_refused(self):
        with self.assertRaises(NotFoundError):
            pensieve.allow_many_tasks(self.conn, "gamma")
        with self.assertRaises(ValidationError):
            pensieve.allow_many_tasks(self.conn, "fleet")
        self.assertEqual(self.count("many_task_desks"), 1)

    def test_mcgonagall_snape_dumbledore_ryan_and_the_scripts_are_refused_many_tasks(self):
        singles = (("mcgonagall", "claude"), ("snape", "claude"), ("portrait", "claude"), ("ryan", "human"),
                   ("owl-post", "script"))
        for name, family in singles:
            self.desk(name, family)
            with self.subTest(desk=name):
                with self.assertRaisesRegex(ValidationError, f"{name} keeps one active task at a time"):
                    pensieve.allow_many_tasks(self.conn, name)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "keeps one active task at a time"):
                    self.conn.execute("INSERT INTO many_task_desks(desk, granted_at) VALUES (?, 1)", (name,))
                self.assertFalse(pensieve.takes_many_tasks(self.conn, name))
        self.assertEqual(self.count("many_task_desks"), 1)

    def test_list_tasks_open_filter(self):
        queued = self.task("beta")
        active = self.started("beta")
        waiting = pensieve.mark_awaiting_close(self.conn, self.started("beta")["id"])
        closed = self.task("beta")
        pensieve.close_task(self.conn, closed["id"], "abandoned", now=NOW)
        self.assertEqual([task["id"] for task in pensieve.list_tasks(self.conn, desk="beta", open_only=True)],
                         [queued["id"], active["id"], waiting["id"]])
        with self.assertRaises(ValidationError):
            pensieve.list_tasks(self.conn, desk="beta", status="active", open_only=True)


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

    def test_two_processes_racing_on_a_many_task_desk_both_succeed(self):
        context = multiprocessing.get_context("spawn")
        self.desk("racer", "codex")
        pensieve.allow_many_tasks(self.conn, "racer", now=NOW)
        first, second = self.task("racer")["id"], self.task("racer")["id"]
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = [context.Process(target=_race_worker, args=(str(self.db_path), task_id, barrier, results))
                   for task_id in (first, second)]
        for worker in workers:
            worker.start()
        outcomes = sorted(results.get(timeout=60) for _ in workers)
        for worker in workers:
            worker.join(timeout=60)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(outcomes, sorted([("ok", first), ("ok", second)]))
        self.assertEqual(len(pensieve.list_tasks(self.conn, desk="racer", status="active")), 2)


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

    def test_a_review_branch_is_set_on_an_active_task_moves_and_is_never_cleared(self):
        queued = self.task()
        with self.assertRaisesRegex(ConflictError, "active task"):
            pensieve.set_review_branch(self.conn, queued["id"], "fix/site")
        task = self.started()
        self.assertIsNone(task["review_branch"])
        self.assertEqual(pensieve.set_review_branch(self.conn, task["id"], "fix/site")["review_branch"], "fix/site")
        self.assertEqual(pensieve.set_review_branch(self.conn, task["id"], "fix/site")["review_branch"], "fix/site")
        self.assertEqual(pensieve.set_review_branch(self.conn, task["id"], "fix/renamed")["review_branch"],
                         "fix/renamed")
        for bad in ("", "-x", "a..b", "a b", "x" * 256, None, "fix/\n"):
            with self.subTest(branch=bad), self.assertRaises(ValidationError):
                pensieve.set_review_branch(self.conn, task["id"], bad)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never cleared"):
            self.conn.execute("UPDATE tasks SET review_branch = NULL WHERE id = ?", (task["id"],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
            self.conn.execute("UPDATE tasks SET review_branch = 'Upper case' WHERE id = ?", (task["id"],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "set on an active task"):
            self.conn.execute("INSERT INTO tasks(id, desk, title, status, created_at, review_branch)"
                              " VALUES ('tk_00000000000000aa', 'alpha', 'x', 'queued', 1, 'main')")
        pensieve.mark_awaiting_close(self.conn, task["id"])
        with self.assertRaisesRegex(ConflictError, "active task"):
            pensieve.set_review_branch(self.conn, task["id"], "fix/other")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "set on an active task"):
            self.conn.execute("UPDATE tasks SET review_branch = 'fix/other' WHERE id = ?", (task["id"],))
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["review_branch"], "fix/renamed")

    def test_a_review_branch_is_any_name_git_takes_in_printable_ascii(self):
        task = self.started()
        for good in ("Cris-Ryan-Tan/do-the-@pr-feedback-skill.-i-think-some-of-the-rec", "Fix-Upper", "a@b", "@",
                     "fix/moody-notes", "x/HEAD", "v1.2.3", "!#$%&'()+,;<=>`{|}\"", "x" * 255):
            with self.subTest(branch=good):
                self.assertEqual(pensieve.set_review_branch(self.conn, task["id"], good)["review_branch"], good)
        for bad in ("", None, b"main", 7, "a b", " main", "main ", "a\tb", "a\x01b", "a\x7fb", "caf\u00e9", "x" * 256,
                    "a..b", "x.lock", "x.lock/y", "-x", "HEAD", "/x", "x/", "x.", "a//b", ".x", "x/.y", "a@{b",
                    "@{-1}", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b"):
            with self.subTest(branch=bad), self.assertRaisesRegex(ValidationError, "a name git takes as a branch"):
                pensieve.set_review_branch(self.conn, task["id"], bad)
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["review_branch"], "x" * 255)

    def test_the_review_branch_check_holds_raw_writes_to_255_bytes_of_printable_ascii(self):
        task = self.started()
        for good in ("Cris-Ryan-Tan/do-the-@pr-feedback-skill.-i-think-some-of-the-rec", "!", "~" * 255):
            with self.subTest(branch=good):
                self.conn.execute("UPDATE tasks SET review_branch = ? WHERE id = ?", (good, task["id"]))
                self.assertEqual(pensieve.get_task(self.conn, task["id"])["review_branch"], good)
        # A blob is refused as a STRICT column's type where SQLite has them, and by the CHECK's typeof otherwise.
        for bad in ("", "a b", "a\tb", "a\nb", "a\x00b", "a\x7fb", "caf\u00e9", "x" * 256, b"main"):
            with self.subTest(branch=bad), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK|BLOB"):
                self.conn.execute("UPDATE tasks SET review_branch = ? WHERE id = ?", (bad, task["id"]))
        self.assertEqual(pensieve.get_task(self.conn, task["id"])["review_branch"], "~" * 255)

    def test_a_task_awaiting_close_or_closed_takes_no_worktree(self):
        task = self.task()
        pensieve.start_task(self.conn, task["id"])
        pensieve.mark_awaiting_close(self.conn, task["id"])
        with self.assertRaises(ConflictError):
            pensieve.set_worktree(self.conn, task["id"], worktree())
        with self.assertRaises(NotFoundError):
            pensieve.set_worktree(self.conn, "tk_ffffffffffffffff", worktree())


class CloseProvenTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.desks()
        self.desk("gamma", "claude")
        self.go = go_build(self.conn)

    def closed(self, **kwargs) -> dict:
        return pensieve.close_proven(self.conn, self.go["build"], proof(), "closed on its proof",
                                     f"close:proven:{self.go['build']}", now=NOW + 60, **kwargs)

    def events(self) -> list:
        return [dict(row) for row in self.conn.execute("SELECT kind, verdict, task_id FROM events ORDER BY id")]

    def test_close_proven_closes_the_build_and_its_go_parent_with_one_headmaster_event(self):
        result = self.closed(parent_task_id=self.go["parent"])
        for task, kind in ((result["task"], "proven"), (result["parent"], "parent")):
            self.assertEqual((task["status"], task["close_reason"]), ("closed", "complete"))
            closure = pensieve.task_closure(self.conn, task["id"])
            self.assertEqual((closure["kind"], closure["merge_sha"], closure["pass_sha"]), (kind, MERGE_SHA, SHA))
        self.assertEqual(pensieve.task_closure(self.conn, self.go["parent"])["via_task_id"], self.go["build"])
        self.assertEqual(self.events(), [{"kind": "close.proven", "verdict": "headmaster", "task_id": self.go["build"]}])
        with self.assertRaisesRegex(ConflictError, "already closed"):
            self.closed()
        self.assertEqual(len(self.events()), 1)

    def test_close_proven_refuses_a_parent_with_other_open_work_and_rolls_back(self):
        other = owlery.open_request(self.conn, "gamma", "beta", "more", parent_task_id=self.go["parent"])["task"]
        with self.assertRaisesRegex(ConflictError, "other open work"):
            self.closed(parent_task_id=self.go["parent"])
        self.assertEqual(pensieve.get_task(self.conn, self.go["build"])["status"], "awaiting_close")
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "queued")
        self.assertIsNone(pensieve.task_closure(self.conn, self.go["build"]))
        self.assertEqual(self.events(), [])
        result = self.closed()
        self.assertEqual((result["task"]["status"], result["parent"]), ("closed", None))
        self.assertEqual(pensieve.get_task(self.conn, self.go["parent"])["status"], "queued")
        self.assertEqual(pensieve.get_task(self.conn, other["id"])["status"], "queued")

    def test_close_proven_refuses_a_task_with_open_descendants(self):
        child = pensieve.create_task(self.conn, "alpha", "a review left open", parent_task_id=self.go["build"])
        with self.assertRaisesRegex(ConflictError, "open work under it"):
            self.closed(parent_task_id=self.go["parent"])
        for task_id in (self.go["build"], child["id"], self.go["parent"]):
            self.assertNotEqual(pensieve.get_task(self.conn, task_id)["status"], "closed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_closures").fetchone()[0], 0)

    def test_close_proven_refuses_a_task_not_awaiting_close(self):
        active = self.started("beta")
        with self.assertRaisesRegex(ConflictError, "only a task awaiting close"):
            pensieve.close_proven(self.conn, active["id"], proof(), "nope", "close:proven:x")
        with self.assertRaisesRegex(ConflictError, "not this task's parent"):
            self.closed(parent_task_id=active["id"])
        for broken in (proof(landed="pr", pr_number=None), proof(ci="none"), proof(judge_desk=None),
                       proof(evidence_path="/etc/passwd"), proof(pass_sha="X" * 40), {**proof(), "extra": 1}):
            with self.subTest(broken=broken), self.assertRaises(ValidationError):
                pensieve.close_proven(self.conn, self.go["build"], broken, "nope", "close:proven:y")
        self.assertEqual((pensieve.get_task(self.conn, self.go["build"])["status"], self.events()),
                         ("awaiting_close", []))


class CloseParentProvenTests(StoreCase):
    """A go task moves on once its last build has closed, only on a build under it the closer closed proven."""

    def setUp(self):
        super().setUp()
        self.desks()
        self.desk("gamma", "claude")
        self.go = go_build(self.conn)
        self.other = owlery.open_request(self.conn, "gamma", "beta", "more", parent_task_id=self.go["parent"],
                                         now=NOW)["task"]["id"]

    def moved_on(self) -> dict:
        return pensieve.close_parent_proven(self.conn, self.go["parent"], "moved on", now=NOW + 60)

    def test_it_waits_for_the_last_build_then_closes_on_the_proven_ones_proof(self):
        with self.assertRaisesRegex(ConflictError, "open work under it"):
            self.moved_on()
        pensieve.close_proven(self.conn, self.go["build"], proof(), "closed", f"close:proven:{self.go['build']}",
                              now=NOW + 60)
        with self.assertRaisesRegex(ConflictError, "open work under it"):
            self.moved_on()
        pensieve.close_task(self.conn, self.other, "abandoned", now=NOW + 60)
        result = self.moved_on()
        self.assertEqual((result["task"]["status"], result["task"]["close_reason"], result["via"]),
                         ("closed", "complete", self.go["build"]))
        closure = pensieve.task_closure(self.conn, self.go["parent"])
        self.assertEqual((closure["kind"], closure["via_task_id"], closure["merge_sha"]),
                         ("parent", self.go["build"], MERGE_SHA))
        with self.assertRaisesRegex(ConflictError, "already closed"):
            self.moved_on()

    def test_no_proven_build_or_no_go_changes_nothing(self):
        pensieve.close_task(self.conn, self.other, "abandoned", now=NOW + 60)
        pensieve.close_task(self.conn, self.go["build"], "abandoned", now=NOW + 60)
        with self.assertRaisesRegex(ConflictError, "no build under the task was closed by a proven close"):
            self.moved_on()
        plain = pensieve.create_task(self.conn, "gamma", "not a go", now=NOW)["id"]
        with self.assertRaisesRegex(ConflictError, "not registered by a go"):
            pensieve.close_parent_proven(self.conn, plain, "moved on", now=NOW)
        self.assertEqual(pensieve.get_task(self.conn, self.go["parent"])["status"], "queued")
        self.assertIsNone(pensieve.task_closure(self.conn, self.go["parent"]))


class OpenQuestionTests(StoreCase):
    def test_a_question_is_open_until_it_is_answered_or_acked(self):
        self.desks()
        task = self.started("alpha")["id"]
        asked = owlery.send(self.conn, "beta", "alpha", "question", "which?", task_id=task, now=NOW)["id"]
        other = owlery.send(self.conn, "beta", "alpha", "question", "and?", task_id=task, now=NOW + 1)["id"]
        self.assertEqual([owl["id"] for owl in owlery.open_questions(self.conn, task)], [asked, other])
        owlery.send(self.conn, "alpha", "beta", "answer", "this", in_reply_to=asked, task_id=task, now=NOW + 2)
        owlery.read(self.conn, other, "alpha", now=NOW + 3)
        owlery.ack(self.conn, other, "alpha", now=NOW + 3)
        self.assertEqual(owlery.open_questions(self.conn, task), [])
