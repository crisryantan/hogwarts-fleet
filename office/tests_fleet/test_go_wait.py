"""fleet go-wait (fleet/go_wait.py): wait, read only, until one go task changes where it stands, then print one line
with go_watch's fixed text and the key for the next wait. The store is written only by the test itself, through its
own connection, standing in for the desks and scripts that move a go task on."""
from __future__ import annotations

import hashlib
import io
import os
import threading
from unittest import mock

from hogwarts import db, ids, pensieve

from fleet import config, go_wait, go_watch, stops, tools
from fleet.safefs import FleetError
from tests_fleet.test_go_watch import SHAS, GoWatchCase


class Clock:
    """A monotonic clock that only moves when the waiter sleeps, and runs an action on a chosen sleep."""

    def __init__(self, actions: dict = None):
        self.now, self.sleeps, self.actions = 1000.0, 0, actions or {}

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.sleeps in self.actions:
            self.actions[self.sleeps]()


class GoWaitCase(GoWatchCase):
    def run_wait(self, since: str = None, go_id: str = None, clock: Clock = None) -> tuple:
        out, clock = io.StringIO(), clock or Clock()
        code = go_wait.main(go_id or self.go, since, out=out, clock=clock, sleep=clock.sleep)
        return code, out.getvalue(), clock

    def key_of(self, output: str) -> str:
        self.assertIn(" Next: --since ", output)
        return output.rstrip("\n").rsplit(" ", 1)[1]

    def expected(self, word: str, state: str, build: dict = None) -> str:
        return f"{word} {self.sent(self.go, build and build['id'], state)} Next: --since "


class ReturnTests(GoWaitCase):
    def test_return_no_since_prints_where_it_stands_now_at_once(self):
        build = self.build()
        code, output, clock = self.run_wait()
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(self.expected("now", "confirmed", build)), output)
        self.assertEqual(clock.sleeps, 0)
        self.assertEqual(output.count("\n"), 1)

    def test_return_a_changed_key_returns_at_once_with_the_new_state(self):
        build = self.build()
        before = self.key_of(self.run_wait()[1])
        self.handoff(build)
        code, output, clock = self.run_wait(since=before)
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(self.expected("changed", "handoff", build)), output)
        self.assertNotEqual(self.key_of(output), before)
        self.assertEqual(clock.sleeps, 0)

    def test_return_blocks_until_a_state_change_is_written_then_returns_it(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        clock = Clock({3: lambda: self.handoff(build)})
        code, output, clock = self.run_wait(since=since, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, 3)  # quiet polls first, then the one that read the handoff
        self.assertTrue(output.startswith(self.expected("changed", "handoff", build)), output)

    def test_return_a_real_waiter_in_its_own_thread_sees_another_connection_commit(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        out, started = io.StringIO(), threading.Event()

        def sleep(seconds):
            started.set()
            threading.Event().wait(seconds)

        with mock.patch.object(config, "GO_WAIT_POLL_SECONDS", 0.02):
            waiter = threading.Thread(target=go_wait.main, args=(self.go, since),
                                      kwargs={"out": out, "sleep": sleep})
            waiter.start()
            self.assertTrue(started.wait(5))
            self.handoff(build)
            waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(out.getvalue().startswith(self.expected("changed", "handoff", build)), out.getvalue())

    def test_return_wording_or_time_alone_never_ends_a_wait(self):
        build = self.build()
        self.handoff(build)
        since = self.key_of(self.run_wait()[1])
        clock = Clock({1: lambda: self.event(self.go, "go.confirmed", "said again", verdict="routine"),
                       2: lambda: self.tick()})
        with mock.patch.object(config, "GO_WAIT_MAX_SECONDS", 20):
            code, output, clock = self.run_wait(since=since, clock=clock)
        self.assertTrue(output.startswith(self.expected("still", "handoff", build)), output)
        self.assertEqual(self.key_of(output), since)

    def test_return_a_read_that_fails_mid_wait_is_tried_again(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        real, calls = go_watch._go_state, []

        def flaky(conn, item):
            calls.append(1)
            if len(calls) == 2:
                raise db.sqlite3.OperationalError("database is locked")
            return real(conn, item)

        clock = Clock({2: lambda: self.handoff(build)})
        with mock.patch.object(go_watch, "_go_state", side_effect=flaky):
            code, output, _ = self.run_wait(since=since, clock=clock)
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(self.expected("changed", "handoff", build)), output)


    def test_return_another_go_task_it_cannot_read_never_stands_in_its_way(self):
        build = self.build()
        other = self.build(self.go_task(1))
        since = self.key_of(self.run_wait()[1])
        self.handoff(other)
        evidence = self.office / "reviews" / other["id"]
        evidence.chmod(0)  # the other build's review folder cannot be read
        self.addCleanup(evidence.chmod, 0o700)
        clock = Clock({2: lambda: pensieve.close_task(self.conn, self.go, "abandoned", now=self.tick())})
        output = self.run_wait(since=since, clock=clock)[1]
        self.assertEqual(output, f"closed {self.go} / {build['id']}: closed (abandoned). Nothing for you."
                                 " The watch ends.\n")


    def test_return_a_held_run_marker_it_cannot_read_is_never_read_as_not_held(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        held = self.office / config.STATE_DIR / config.STOP_HELD_DIR
        held.mkdir(parents=True, exist_ok=True)
        marker = held / "ow_unreadable"
        marker.write_text("{not json")
        with self.assertRaises(FleetError):
            stops.held(strict=True)
        marker.unlink()

        def unreadable():
            marker.write_text("{not json")
            self.handoff(build)

        # While it cannot be read, no state is reported; once it can, the change is.
        clock = Clock({1: unreadable, 3: marker.unlink})
        code, output, clock = self.run_wait(since=since, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, 3)
        self.assertTrue(output.startswith(self.expected("changed", "handoff", build)), output)


class EndTests(GoWaitCase):
    def test_end_a_closed_go_task_says_closed_and_the_watch_ends(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        clock = Clock({2: lambda: pensieve.close_task(self.conn, self.go, "abandoned", now=self.tick())})
        code, output, _ = self.run_wait(since=since, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(output, f"closed {self.go} / {build['id']}: closed (abandoned). Nothing for you."
                                 " The watch ends.\n")
        self.assertNotIn("--since", output)
        # Waited on again, a closed go task ends the watch at once, whatever key it is given.
        self.assertEqual(self.run_wait(since="0" * 16)[1], output)
        self.assertEqual(self.run_wait()[1], output)

    def test_end_the_max_wait_says_still_with_the_same_key(self):
        build = self.build()
        since = self.key_of(self.run_wait()[1])
        code, output, clock = self.run_wait(since=since)
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(self.expected("still", "confirmed", build)), output)
        self.assertEqual(self.key_of(output), since)
        self.assertEqual(clock.now - 1000.0, config.GO_WAIT_MAX_SECONDS)
        self.assertEqual(clock.sleeps, config.GO_WAIT_MAX_SECONDS // config.GO_WAIT_POLL_SECONDS)
        self.assertEqual(config.GO_WAIT_MAX_SECONDS, 30 * 60)

    def test_end_reads_that_keep_failing_to_the_deadline_are_an_error_never_a_stale_still(self):
        self.build()
        since = self.key_of(self.run_wait()[1])
        real, calls = go_watch._go_state, []

        def locked_after_the_first(conn, item):
            calls.append(1)
            if len(calls) > 1:
                raise db.sqlite3.OperationalError("database is locked")
            return real(conn, item)

        with mock.patch.object(go_watch, "_go_state", side_effect=locked_after_the_first), \
                mock.patch.object(config, "GO_WAIT_MAX_SECONDS", 20):
            code, output, clock = self.run_wait(since=since)
        self.assertEqual(code, 1)
        self.assertTrue(output.startswith("error: the store cannot be read"), output)
        self.assertEqual(clock.sleeps, 4)

    def test_end_a_go_not_applied_yet_is_pending_until_its_spec_lands(self):
        task_id = "tk_" + "e" * 16
        pensieve.create_task(self.conn, "mcgonagall", "go later", intent_path=ids.intent_path(task_id),
                             task_id=task_id, now=self.tick())
        code, output, _ = self.run_wait(go_id=task_id)
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith(f"now {task_id} / no build: go not applied yet. "), output)
        since = self.key_of(output)
        self.assertEqual(self.key_of(self.run_wait(go_id="tk_" + "f" * 16)[1]), since)  # never registered: pending
        clock = Clock({2: lambda: pensieve.record_spec(self.conn, task_id, "/private/tmp/checkout", "fix/later",
                                                       "origin/main", "a" * 64, now=self.tick())})
        output = self.run_wait(since=since, go_id=task_id, clock=clock)[1]
        self.assertTrue(output.startswith(f"changed {self.sent(task_id, None, 'confirmed')} Next: --since "), output)


class ReadOnlyTests(GoWaitCase):
    def office_files(self) -> dict:
        """Every file under the office but the store's shared-memory index, which readers lock in, with a digest."""
        found = {}
        for folder, _, names in os.walk(self.office):
            for name in names:
                path = os.path.join(folder, name)
                if not path.endswith("-shm"):
                    with open(path, "rb") as handle:
                        found[path] = hashlib.sha256(handle.read()).hexdigest()
        return found

    def test_read_only_store_opened_read_only_and_nothing_in_the_office_changes(self):
        build = self.build()
        self.handoff(build)
        self.round(build, SHAS[0])
        self.verified(build, SHAS[0])
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.office_files()
        opened = []
        real = db.connect_readonly

        def readonly(path):
            conn = real(path)
            opened.append(conn)
            return conn

        def refuse_writes():
            conn = opened[0]
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(db.sqlite3.Error):
                conn.execute("UPDATE tasks SET title = 'x'")

        since = self.key_of(self.run_wait()[1])
        with mock.patch.object(db, "connect_readonly", side_effect=readonly), \
                mock.patch.object(db, "connect", side_effect=AssertionError("go-wait opened the store to write")), \
                mock.patch.object(config, "GO_WAIT_MAX_SECONDS", 15):
            code, output, _ = self.run_wait(since=since, clock=Clock({1: refuse_writes}))
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith("still "), output)
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.office_files(), before)
        self.notified.assert_not_called()
        self.phoned.assert_not_called()
        self.spawned_reviews.assert_not_called()

    def test_read_only_bad_arguments_and_a_missing_store_are_errors(self):
        for go_id, since, said in (("tk_nope", None, "error: not a task id"),
                                   (self.go, "../../etc", "error: --since takes the key"),
                                   (self.go, "A" * 16, "error: --since takes the key")):
            with self.subTest(go_id=go_id, since=since):
                code, output, _ = self.run_wait(since=since, go_id=go_id)
                self.assertEqual(code, 1)
                self.assertTrue(output.startswith(said), output)
        with mock.patch.object(config, "DB_PATH", str(self.office / "state" / "missing.db")):
            code, output, _ = self.run_wait()
        self.assertEqual(code, 1)
        self.assertTrue(output.startswith("error: the store cannot be read"), output)
        self.assertFalse((self.office / "state" / "missing.db").exists())


class CommandTests(GoWaitCase):
    def test_command_fleet_go_wait_runs_the_waiter_without_the_read_write_store(self):
        with mock.patch.object(go_wait, "main", return_value=0) as waiter, \
                mock.patch("fleet.common.connect", side_effect=AssertionError("fleet opened the store to write")):
            self.assertEqual(tools.main(["go-wait", self.go, "--since", "0" * 16]), 0)
            self.assertEqual(tools.main(["go-wait", self.go]), 0)
        self.assertEqual(waiter.call_args_list, [mock.call(self.go, "0" * 16), mock.call(self.go, None)])
