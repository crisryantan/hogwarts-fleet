"""Auto-portrait cut at every write: each night ends told by exactly one owner row, whatever was killed or refused.

Every way a night ends (each row of NIGHTS) runs under every cut of the store writes that arm, snapshot, end or tell
a night (an event of a rundesk or portrait kind): a SIGKILL at the k-th, the store refusing the k-th, the store
refusing the k-th and a SIGKILL at a later one, and the store refusing every write from the k-th on. Two more nightly
jobs then finish what was left. A SIGKILL is simulated as in test_portrait_auto: from that write on, nothing more is
written. No desk process starts.
"""
from __future__ import annotations

import contextlib
import os
from unittest import mock

from hogwarts import pensieve
from hogwarts.errors import StoreError
from tests.support import DAY, NOW

from fleet import config, portrait, portrait_auto, run_desk, safefs
from fleet.safefs import FleetError
from tests_fleet.test_portrait_auto import AutoCase, _Killed, date_of

WRITES = ("arm_auto_patch", "snapshot_auto_patch", "end_auto_patch", "add_event")
OWNER_KINDS = ("rundesk.", "portrait.")


class EveryCutTests(AutoCase):
    def setUp(self) -> None:
        super().setUp()
        self.opt_in()
        self.days = 0
        self.checked = 0

    # the cut

    @contextlib.contextmanager
    def cut(self, kill_at: int = None, fail_at: int = None, fail_from: int = None):
        """Count each write that arms, snapshots, ends or tells a night. The kill_at-th is a SIGKILL; the fail_at-th,
        and every one from fail_from on, is refused by the store. Yields whether the SIGKILL came."""
        seen = {"writes": 0, "dead": False}

        def counted(name, real):
            def write(*args, **kwargs):
                if seen["dead"]:
                    raise _Killed()
                kind = args[2] if len(args) > 2 else kwargs.get("kind")
                if name == "add_event" and not str(kind).startswith(OWNER_KINDS):
                    return real(*args, **kwargs)
                seen["writes"] += 1
                if seen["writes"] == kill_at:
                    seen["dead"] = True
                    raise _Killed()
                if seen["writes"] == fail_at or (fail_from is not None and seen["writes"] >= fail_from):
                    raise StoreError("the store said no")
                return real(*args, **kwargs)
            return write
        with contextlib.ExitStack() as stack:
            for name in WRITES:
                stack.enter_context(mock.patch.object(pensieve, name, counted(name, getattr(pensieve, name))))
            yield seen

    def told_since(self, mark: int) -> list:
        rows = self.conn.execute("SELECT kind FROM events WHERE id > ? AND verdict = 'headmaster' ORDER BY id",
                                 (mark,)).fetchall()
        return [row["kind"] for row in rows if row["kind"].startswith(OWNER_KINDS)]

    def restore(self) -> None:
        """What a scenario changed for its own night, put back for the jobs that follow."""
        self.enable("portrait")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.stop_file())
        self.opt_in()

    def check(self, label: str, night, prepare, quiet: bool, **cut) -> bool:
        """One night of its own date, cut so, then the next two jobs. True when its SIGKILL came."""
        self.days += 3
        now = NOW + self.days * DAY
        date = date_of(now)
        if prepare is not None:
            prepare(now)
        mark = self.last_event_id()
        with self.cut(**cut) as seen, contextlib.suppress(_Killed, FleetError, StoreError, SystemExit):
            night(now)
        self.restore()
        self.quiet_night(now + DAY)
        self.quiet_night(now + 2 * DAY)
        row, told = self.row(date), self.told_since(mark)
        self.checked += 1
        with self.subTest(night=label, **{key: value for key, value in cut.items() if value is not None}):
            if row is None:
                # Cut before the lane armed it: a plain run's night, told at most by its own row.
                self.assertLessEqual(len(told), 1, told)
            else:
                self.assertIn(row["state"], ("done", "stopped", "off"))
                untold = ((row["state"], row["outcome"]) == ("done", portrait_auto.NO_PATCH.format(date=date))
                          or (quiet and row["state"] == "off"))
                self.assertEqual(len(told), 0 if untold else 1, (told, dict(row)))
        return seen["dead"]

    def every_cut(self, label: str, night, prepare=None, quiet: bool = False) -> None:
        self.check(label, night, prepare, quiet)
        at = 1
        while self.check(label, night, prepare, quiet, kill_at=at):
            self.check(label, night, prepare, quiet, fail_at=at)
            self.check(label, night, prepare, quiet, fail_from=at)
            later = at + 1
            while self.check(label, night, prepare, quiet, fail_at=at, kill_at=later):
                later += 1
            at += 1

    # the nights

    def signal(self):
        raise SystemExit(143)

    def lane_nights(self):
        """(label, night(now), quiet) for each way a night with the switch on ends."""
        def refused(now):
            os.unlink(self.office / "desks" / "portrait" / config.ENABLED_MARKER)
            self.night(self.ops, now=now)

        def stopped(now):
            self.write_file(self.stop_file(), "")
            self.night(self.ops, now=now)

        def there_before(now):
            self.write_patch(self.ops, date=date_of(now))
            self.night(now=now)

        def ending(setup, returncode):
            def night(now):
                with setup():
                    self.night(self.ops, now=now, returncode=returncode)
            return night

        def blocked_model(returncode, vendor_limit=False):
            def night(now):
                limit = mock.patch.object(run_desk, "plan_limit", return_value="claude_plan") if vendor_limit \
                    else contextlib.nullcontext()
                with limit, self.calls_a_blocked_model(now, returncode):
                    portrait.nightly(self.conn, now=now)
            return night

        def slot_wait(now):
            with mock.patch.object(run_desk, "build_plan", side_effect=safefs.Busy("a run slot is held")):
                self.night(self.ops, now=now)

        def signal_after_snapshot(now):
            with mock.patch.object(pensieve, "add_keypoint", side_effect=SystemExit(143)):
                self.night(self.ops, now=now)
        yield "applied", lambda now: self.night(self.ops, now=now), False
        yield "no patch", lambda now: self.night(now=now), True
        yield "a malformed patch", lambda now: self.night(raw=b"nope", now=now), False
        yield "a patch there before", there_before, False
        yield "a failed run", lambda now: self.night(self.ops, now=now, returncode=1), False
        yield "a refused run", refused, False
        yield "a lock wait after arming", slot_wait, False
        for name, _, setup, _ in self.refusals():
            yield name, ending(setup, 1 if name == "vendor limit" else 0), False
        yield "a clean run on a blocked model", blocked_model(0), False
        yield "a failed run on a blocked model", blocked_model(1), False
        yield "a vendor limit on a blocked model", blocked_model(1, vendor_limit=True), False
        yield "Ollivander's stop", stopped, False
        yield "off at the apply", lambda now: self.night(self.ops, now=now, during=self.opt_out), False
        yield "a signal mid run", lambda now: self.night(self.ops, now=now, during=self.signal), False
        yield "a signal after the snapshot", signal_after_snapshot, False

    def off_nights(self):
        """(label, night(now), quiet) for each way a rerun with the switch off ends, the same day as a killed
        attempt that left the night armed."""
        def later(**kwargs):
            def night(now):
                self.night(now=now + 60, **kwargs)
            return night

        def capped(now):
            with self.refusals()[0][2]():
                self.night(self.ops, now=now + 60)

        def blocked_model(now):
            with self.calls_a_blocked_model(now):
                portrait.nightly(self.conn, now=now + 60)
        yield "off: a patch", later(ops=self.ops), False
        yield "off: no patch", later(), True
        yield "off: a failed run", later(ops=self.ops, returncode=1), False
        yield "off: capped", capped, False
        yield "off: a clean run on a blocked model", blocked_model, False
        yield "off: a signal mid run", later(ops=self.ops, during=self.signal), False

    def killed_then_off(self, now: int) -> None:
        self.killed_attempt(now)
        self.opt_out()

    # the tests

    def test_every_night_with_the_switch_on_ends_in_one_row_however_it_is_cut(self):
        for label, night, quiet in self.lane_nights():
            self.every_cut(label, night, quiet=quiet)
        self.assertGreater(self.checked, 250)

    def test_every_rerun_with_the_switch_off_ends_a_killed_night_in_one_row_however_it_is_cut(self):
        for label, night, quiet in self.off_nights():
            self.every_cut(label, night, prepare=self.killed_then_off, quiet=quiet)
        self.assertGreater(self.checked, 50)
