"""fleet loops (fleet/loops.py): the launchd/ jobs run from a terminal, on their plists' own schedules.

Every job here is a fake module in the test's temp folder, run through the fleet's wrapper line. The scheduling tests
drive the supervisor one tick at a time with an injected clock and fake runs; the process tests start a real
supervisor in a child process against the temp office, so its lock, signals and kills are the real ones. launchctl
never runs, and the real office, castle and LaunchAgents folder are never read.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from unittest import mock

from fleet import config, gitops, loops, safefs, tools
from fleet.safefs import FleetError
from tests_fleet.support import OFFICE, FleetCase, kit_setting

KIT_JOBS = ("owlpost", "map", "morning", "keeper", "scoreboard", "portrait", "gringotts", "ollivander")
# A fake job's -c line, the shape the fleet's own plists use.
FAKE_LINE = 'import sys; sys.path.insert(0, "{fake}"); from {module} import main; sys.exit(main())'
T0 = 1_760_000_000.0  # a fixed wall clock for the scheduling tests
WAIT = 15.0
# A run under taskpolicy -b gets little CPU while the Mac is busy, such as during a parallel suite run.
BACKGROUND_WAIT = 180.0


class FakeRun:
    """A run the supervisor started, ended by the test."""

    count = 0

    def __init__(self) -> None:
        FakeRun.count += 1
        self.pid, self.returncode = 900000 + FakeRun.count, None

    def poll(self):
        return self.returncode


def eventually(check, timeout: float = WAIT):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.05)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class LoopsCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.home_dir = self.tmp / "home"
        self.agents = self.home_dir / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True, mode=0o700)
        homed = mock.patch.object(config, "USER_HOME_DIR", str(self.home_dir))
        homed.start()
        self.addCleanup(homed.stop)
        for folder in ("launchd", "logs", "loops", "locks"):
            (self.office / folder).mkdir(mode=0o700, exist_ok=True)
        self.fake = self.tmp / "fake"
        self.fake.mkdir(mode=0o700)
        self.said: list = []
        self.started: list = []

    def module(self, module: str, body: str) -> None:
        self.write_file(self.fake / f"{module}.py", body)

    def plist(self, name: str, module: str = "quiet", **keys) -> dict:
        data = {"Label": f"com.hogwarts.{name}",
                "ProgramArguments": list(config.PYTHON_WRAPPER) + ["-c", FAKE_LINE.format(fake=self.fake,
                                                                                           module=module)],
                "WorkingDirectory": str(self.office), "StandardOutPath": f"{self.office}/logs/{name}.out.log",
                "StandardErrorPath": f"{self.office}/logs/{name}.err.log", "Umask": 0o077, **keys}
        self.write_file(self.office / "launchd" / f"com.hogwarts.{name}.plist", plistlib.dumps(data))
        return data

    def job_list(self, text: str) -> None:
        self.write_file(self.office / "loops" / "jobs", text)

    def supervisor(self, *names: str, now: float = T0, start=None, owned=None) -> loops.Supervisor:
        return loops.Supervisor([loops.load_job(name) for name in names], now, self.said.append,
                                start=start or self.fake_start, owned_by_launchd=owned or (lambda name: False))

    def fake_start(self, job):
        run = FakeRun()
        self.started.append((job.name, run))
        return run


class PlistTests(LoopsCase):
    def test_every_kit_plist_parses_with_the_schedule_launchd_gives_it(self):
        office = kit_setting("OFFICE_ROOT")
        with mock.patch.object(config, "OFFICE_ROOT", office):
            jobs = {name: loops.parse_job(name, (OFFICE / "launchd" / f"com.hogwarts.{name}.plist").read_bytes())
                    for name in KIT_JOBS}
        owl = jobs["owlpost"]
        self.assertEqual((owl.interval, len(owl.watch), owl.abandon_group, owl.throttle), (300, 7, True, 10))
        self.assertEqual(sum(len(v) for v in jobs["map"].calendar.values()), 5 * 45)
        self.assertEqual(jobs["gringotts"].calendar, {("Hour", "Minute"): {(23, 30)}})
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertEqual((job.cwd, job.umask, job.run_at_load, job.keep_alive), (office, 0o077, False, False))
                self.assertEqual((job.out_log, job.err_log), (f"{name}.out.log", f"{name}.err.log"))
                # ProcessType Background runs under darwin's background policy, as launchd runs it.
                self.assertEqual(job.argv[:2], ("/usr/sbin/taskpolicy", "-b"))
                self.assertEqual(job.argv[2:9], config.PYTHON_WRAPPER)

    def test_a_plist_it_cannot_run_the_way_launchd_would_is_refused(self):
        wrapper = list(config.PYTHON_WRAPPER)
        cases = {
            "an unknown key": {"UserName": "root", "StartInterval": 60},
            "keep-alive conditions": {"KeepAlive": {"SuccessfulExit": False}},
            "another program": {"ProgramArguments": ["/bin/sh", "-c", "true"], "StartInterval": 60},
            "the wrapper without -c": {"ProgramArguments": wrapper + ["-m", "x"], "StartInterval": 60},
            "a log elsewhere": {"StandardOutPath": "/private/tmp/x.log", "StartInterval": 60},
            "another folder": {"WorkingDirectory": "/private/tmp", "StartInterval": 60},
            "day and weekday": {"StartCalendarInterval": {"Day": 1, "Weekday": 1}},
            "an hour out of range": {"StartCalendarInterval": {"Hour": 24}},
            "nothing that starts it": {},
            "another label": {"Label": "com.hogwarts.other", "StartInterval": 60},
            "a text interval": {"StartInterval": "60"},
            "a process type it cannot apply": {"ProcessType": "Interactive", "StartInterval": 60},
        }
        for why, keys in cases.items():
            with self.subTest(why=why):
                self.plist("odd", **keys)
                with self.assertRaises(FleetError):
                    loops.load_job("odd")
        # Values only a binary plist can hold: a null where a number goes, a NUL in a path, an = in a name.
        base = self.plist("odd", StartInterval=60)
        for why, keys in {"a null umask": {"Umask": None}, "a null throttle": {"ThrottleInterval": None},
                          "a NUL in a watched path": {"WatchPaths": ["/private/tmp/a\x00b"]},
                          "an = in a variable name": {"EnvironmentVariables": {"A=B": "c"}}}.items():
            with self.subTest(why=why), mock.patch.object(loops.plistlib, "loads", return_value={**base, **keys}), \
                    self.assertRaises(FleetError):
                loops.load_job("odd")
        with self.assertRaises(FleetError):
            loops.load_job("../odd")
        launchd = self.office / "launchd"
        os.symlink(launchd / "com.hogwarts.odd.plist", launchd / "com.hogwarts.link.plist")
        with self.assertRaises(FleetError):
            loops.load_job("link")

    def test_the_job_list_reads_names_once_each_and_refuses_anything_else(self):
        self.assertIsNone(loops.chosen_jobs())
        self.job_list("# terminal loops\nowlpost\n\nmap  # the Map\nowlpost\n")
        self.assertEqual(loops.chosen_jobs(), ["owlpost", "map"])
        for bad in ("../map\n", "Map\n", "map;rm\n"):
            with self.subTest(bad=bad):
                self.job_list(bad)
                with self.assertRaises(FleetError):
                    loops.chosen_jobs()

    def test_calendar_weekdays_are_launchds_with_sunday_as_0_or_7(self):
        monday_0900 = time.strptime("2026-10-05 09:00", "%Y-%m-%d %H:%M")
        sunday_0900 = time.strptime("2026-10-04 09:00", "%Y-%m-%d %H:%M")
        self.assertTrue(loops.calendar_matches(loops._calendar({"Weekday": 1, "Hour": 9, "Minute": 0}), monday_0900))
        self.assertFalse(loops.calendar_matches(loops._calendar({"Weekday": 1, "Hour": 9, "Minute": 0}), sunday_0900))
        for sunday in (0, 7):
            self.assertTrue(loops.calendar_matches(loops._calendar({"Weekday": sunday, "Hour": 9}), sunday_0900))
        self.assertTrue(loops.calendar_matches(loops._calendar({"Minute": 0}), monday_0900))
        self.assertTrue(loops.calendar_matches(loops._calendar({}), monday_0900))


class ScheduleTests(LoopsCase):
    def minute_of(self, text: str) -> float:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))

    def test_a_monthly_slot_missed_in_a_long_sleep_still_runs_once_on_wake(self):
        index = loops._calendar({"Day": 1, "Hour": 3, "Minute": 0})
        slept = int(self.minute_of("2026-08-20 10:00") // 60)
        woke = int(self.minute_of("2026-10-07 10:00") // 60)
        self.assertTrue(loops.calendar_due(index, slept, woke))
        self.assertFalse(loops.calendar_due(index, int(self.minute_of("2026-10-01 03:00") // 60), woke))

    def test_an_interval_job_runs_each_interval_and_not_at_load(self):
        self.plist("sweep", StartInterval=300)
        supervisor = self.supervisor("sweep")
        supervisor.tick(T0)
        supervisor.tick(T0 + 299)
        self.assertEqual(self.started, [])
        supervisor.tick(T0 + 300)
        self.assertEqual(len(self.started), 1)
        self.started[0][1].returncode = 0
        supervisor.tick(T0 + 301)
        supervisor.tick(T0 + 600)
        self.assertEqual(len(self.started), 2)

    def test_run_at_load_starts_at_the_first_tick(self):
        self.plist("now", RunAtLoad=True)
        self.supervisor("now").tick(T0)
        self.assertEqual([name for name, _ in self.started], ["now"])

    def test_a_calendar_job_runs_once_in_its_minute_and_once_for_slots_missed_in_one_gap(self):
        self.plist("nightly", StartCalendarInterval=[{"Hour": 22, "Minute": 30}, {"Hour": 23, "Minute": 30}])
        start = self.minute_of("2026-10-07 22:29")
        supervisor = self.supervisor("nightly", now=start)
        supervisor.tick(start + 59)
        self.assertEqual(self.started, [])
        for second in (60, 61, 90, 119):
            supervisor.tick(start + second)
        self.assertEqual(len(self.started), 1)
        self.started[0][1].returncode = 0
        # The Mac slept from 22:31 to 23:45: both later slots fell in the gap, and they run once, on wake.
        supervisor.tick(start + 120)
        supervisor.tick(self.minute_of("2026-10-07 23:45"))
        self.assertEqual(len(self.started), 2)
        # A clock set back starts nothing.
        self.started[1][1].returncode = 0
        supervisor.tick(self.minute_of("2026-10-07 23:46"))
        supervisor.tick(start)
        self.assertEqual(len(self.started), 2)

    def test_a_watched_folder_change_starts_the_job_and_a_change_during_its_run_runs_it_once_more(self):
        outbox = self.tmp / "outbox"
        outbox.mkdir()
        self.plist("post", WatchPaths=[str(outbox)], ThrottleInterval=0)
        supervisor = self.supervisor("post")
        supervisor.tick(T0)
        self.assertEqual(self.started, [])
        (outbox / "one.json").write_text("{}")
        supervisor.tick(T0 + 1)
        self.assertEqual(len(self.started), 1)
        (outbox / "two.json").write_text("{}")
        supervisor.tick(T0 + 2)
        (outbox / "three.json").write_text("{}")
        supervisor.tick(T0 + 3)
        self.assertEqual(len(self.started), 1)
        self.started[0][1].returncode = 0
        supervisor.tick(T0 + 4)
        self.assertEqual(len(self.started), 2)
        self.started[1][1].returncode = 0
        supervisor.tick(T0 + 5)
        self.assertEqual(len(self.started), 2)
        # A watched folder replaced by a link counts as a change, read without following it.
        os.rename(outbox, self.tmp / "moved")
        os.symlink(self.tmp / "moved", outbox)
        supervisor.tick(T0 + 6)
        self.assertEqual(len(self.started), 3)

    def test_the_throttle_interval_spaces_two_starts(self):
        self.plist("busy", StartInterval=1)
        supervisor = self.supervisor("busy")
        supervisor.tick(T0 + 1)
        self.started[0][1].returncode = 0
        for second in range(2, 11):
            supervisor.tick(T0 + second)
        self.assertEqual(len(self.started), 1)
        supervisor.tick(T0 + 11)
        self.assertEqual(len(self.started), 2)

    def test_a_failing_job_backs_off_and_retries_while_the_others_run_on(self):
        self.plist("flaky", StartInterval=3600, RunAtLoad=True, ThrottleInterval=0)
        self.plist("nightly", StartCalendarInterval={"Minute": 0}, ThrottleInterval=0)
        self.plist("steady", StartInterval=5, ThrottleInterval=0)
        start = self.minute_of("2026-10-07 09:59")
        supervisor = self.supervisor("flaky", "nightly", "steady", now=start)
        supervisor.tick(start)
        [(_, flaky)] = self.started
        flaky.returncode = 1
        supervisor.tick(start + 1)
        self.assertIn("flaky: failure 1 in a row; retrying in 30s", self.said)
        supervisor.tick(start + 30)
        self.assertEqual([name for name, _ in self.started], ["flaky", "steady"])
        self.started[1][1].returncode = 0
        supervisor.tick(start + 31)
        self.assertEqual([name for name, _ in self.started], ["flaky", "steady", "flaky"])
        self.started[2][1].returncode = -9
        supervisor.tick(start + 32)
        self.assertIn("flaky: ended, killed by signal 9", self.said)
        self.assertIn("flaky: failure 2 in a row; retrying in 60s", self.said)
        # A calendar job that fails waits for its next slot, never retried.
        supervisor.tick(start + 60)
        nightly = [run for name, run in self.started if name == "nightly"]
        nightly[0].returncode = 1
        supervisor.tick(start + 61)
        supervisor.tick(start + 3000)
        self.assertEqual(len([name for name, _ in self.started if name == "nightly"]), 1)
        self.assertIn("nightly: failure 1 in a row; next slot, no sooner than 30s", self.said)
        # Its next slot runs it again.
        supervisor.tick(start + 3600 + 60)
        self.assertEqual(len([name for name, _ in self.started if name == "nightly"]), 2)
        self.assertGreaterEqual(len([name for name, _ in self.started if name == "steady"]), 2)

    def test_a_start_that_raises_is_one_failure_of_that_job_only(self):
        self.plist("broken", RunAtLoad=True, StartInterval=600)
        self.plist("fine", RunAtLoad=True)

        def start(job):
            if job.name == "broken":
                raise OSError("no such file")
            return self.fake_start(job)

        supervisor = self.supervisor("broken", "fine", start=start)
        supervisor.tick(T0)
        self.assertEqual([name for name, _ in self.started], ["fine"])
        self.assertIn("broken: supervisor error, OSError: no such file", self.said)
        self.assertIn("broken: failure 1 in a row; retrying in 30s", self.said)

    def test_a_job_launchd_also_has_is_skipped_until_its_plist_leaves_launch_agents(self):
        self.plist("post", StartInterval=10, ThrottleInterval=0)
        self.plist("keeper", KeepAlive=True)
        self.plist("other", StartInterval=3600)
        for name in ("post", "keeper"):
            (self.agents / f"com.hogwarts.{name}.plist").write_text("x")
        supervisor = loops.Supervisor([loops.load_job(name) for name in ("post", "keeper", "other")], T0,
                                      self.said.append, start=self.fake_start)
        # Only what runs here is what the marker names.
        self.assertEqual(supervisor.runs_here(), ["other"])
        supervisor.tick(T0 + 10)
        supervisor.tick(T0 + 20)
        self.assertEqual(self.started, [])
        self.assertEqual(len([line for line in self.said if "post: skipped, launchd has" in line]), 1)
        for name in ("post", "keeper"):
            (self.agents / f"com.hogwarts.{name}.plist").unlink()
        supervisor.tick(T0 + 25)
        # The KeepAlive start waited for launchd to let go; the interval triggers it had while skipped were dropped.
        self.assertEqual([name for name, _ in self.started], ["keeper"])
        self.assertEqual(supervisor.runs_here(), ["post", "keeper", "other"])
        self.assertIn("post: launchd no longer has it, so it runs here again", self.said)
        supervisor.tick(T0 + 30)
        self.assertEqual([name for name, _ in self.started], ["keeper", "post"])

    def test_a_job_that_cannot_be_set_up_is_left_out_and_the_others_run(self):
        self.plist("first", RunAtLoad=True)
        self.plist("second", RunAtLoad=True)

        def owned(name):
            if name == "first":
                raise OSError("unreadable")
            return False

        supervisor = self.supervisor("first", "second", owned=owned)
        supervisor.tick(T0)
        self.assertEqual([name for name, _ in self.started], ["second"])
        self.assertIn("first: not run, OSError: unreadable", self.said)


class RunTests(LoopsCase):
    """Real processes: the fake jobs and, for the lock and signal tests, a real supervisor in a child process."""

    def child_supervisor(self, grace: float = 5.0) -> subprocess.Popen:
        code = (f"import sys; sys.path.insert(0, {str(OFFICE)!r}); from fleet import config, loops; "
                f"config.OFFICE_ROOT = {str(self.office)!r}; config.USER_HOME_DIR = {str(self.home_dir)!r}; "
                f"config.LOOPS_TICK_SECONDS = 0.05; config.LOOPS_STOP_GRACE_SECONDS = {grace}; "
                "config.LOOPS_BACKOFF_FIRST_SECONDS = 0.2; config.LOOPS_BACKOFF_MAX_SECONDS = 0.4; "
                "sys.exit(loops.supervise())")
        out = os.open(self.tmp / f"supervisor-{time.monotonic_ns()}.out", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            child = subprocess.Popen(list(config.PYTHON_WRAPPER) + ["-c", code], stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=out, start_new_session=True)
        finally:
            os.close(out)

        def cleanup() -> None:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        self.addCleanup(cleanup)
        return child

    def running_marker(self) -> dict:
        path = self.office / "loops" / "running.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def log(self) -> str:
        path = self.office / "logs" / "loops.log"
        return path.read_text() if path.exists() else ""

    def sleeper(self, ignore_term: bool = False, wait: bool = True, **keys) -> None:
        # Writes its pid and that of a grandchild in its process group that ignores SIGTERM, then sleeps or ends.
        self.module("sleeper", f"""import os, signal, subprocess, time
def main():
    if {ignore_term!r}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; exec /bin/sleep 60"])
    with open({str(self.tmp / 'pids')!r}, "w") as out:
        out.write(f"{{os.getpid()}} {{child.pid}}")
    if {wait!r}:
        time.sleep(60)
    return 0
""")
        self.plist("sleeper", "sleeper", RunAtLoad=True, **keys)
        self.job_list("sleeper\n")

    def pids(self) -> list:
        path = self.tmp / "pids"
        text = path.read_text() if path.exists() else ""
        return [int(part) for part in text.split()] if len(text.split()) == 2 else []

    def test_a_marker_left_by_a_killed_supervisor_goes_as_soon_as_the_lock_is_held(self):
        self.plist("quiet", StartInterval=3600)
        self.job_list("quiet\n")
        marker = self.office / "loops" / "running.json"
        self.write_file(marker, json.dumps({"pid": os.getpid(), "jobs": ["owlpost", "map"], "started": 1}))
        seen, real_load = [], loops.load_job

        def load(name):
            seen.append(marker.exists())
            return real_load(name)

        def sleep(seconds):
            seen.append(json.loads(marker.read_text())["jobs"])
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.object(loops, "load_job", side_effect=load):
            self.assertEqual(loops.supervise(sleep=sleep, say=self.said.append), 0)
        self.assertEqual(seen[:2], [False, ["quiet"]])
        self.assertFalse(marker.exists())

    def test_only_one_supervisor_runs_at_a_time(self):
        self.plist("quiet", StartInterval=3600)
        self.module("quiet", "def main():\n    return 0\n")
        self.job_list("quiet\n")
        first = self.child_supervisor()
        self.assertTrue(eventually(lambda: self.running_marker().get("pid") == first.pid))
        second = self.child_supervisor()
        self.assertEqual(second.wait(timeout=WAIT), 1)
        self.assertIn("another fleet loops is already running", self.log())
        self.assertEqual(self.running_marker().get("jobs"), ["quiet"])
        os.kill(first.pid, signal.SIGTERM)
        self.assertEqual(first.wait(timeout=WAIT), 0)
        self.assertEqual(self.running_marker(), {})
        # The lock went with it, so the next one starts.
        third = self.child_supervisor()
        self.assertTrue(eventually(lambda: self.running_marker().get("pid") == third.pid))
        os.kill(third.pid, signal.SIGINT)
        self.assertEqual(third.wait(timeout=WAIT), 0)

    def test_sigterm_sigint_and_sighup_stop_each_running_job_with_its_process_group(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=number):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.tmp / "pids")
                self.sleeper()
                child = self.child_supervisor()
                pids = eventually(self.pids)
                self.assertEqual(len(pids), 2)
                os.kill(child.pid, number)
                self.assertEqual(child.wait(timeout=WAIT), 0)
                self.assertFalse(alive(pids[0]))
                self.assertTrue(eventually(lambda: not alive(pids[1])), "the job's own child was left running")
                self.assertEqual(self.running_marker(), {})
                self.assertIn("sleeper: stopping pid", self.log())

    def test_a_finished_run_leaves_nothing_in_its_process_group_unless_it_abandons_it(self):
        for abandon in (False, True):
            with self.subTest(abandon=abandon):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.tmp / "pids")
                self.sleeper(wait=False, AbandonProcessGroup=abandon, StartInterval=3600)
                child = self.child_supervisor()
                pids = eventually(self.pids)
                self.assertEqual(len(pids), 2)
                self.assertTrue(eventually(lambda: "sleeper: ended, exit 0" in self.log()))
                if abandon:
                    time.sleep(0.5)
                    self.assertTrue(alive(pids[1]), "an abandoned group's process was killed")
                    os.kill(pids[1], signal.SIGKILL)
                else:
                    self.assertTrue(eventually(lambda: not alive(pids[1])), "the run's leftover was left running")
                os.kill(child.pid, signal.SIGTERM)
                self.assertEqual(child.wait(timeout=WAIT), 0)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.office / "logs" / "loops.log")

    def test_a_job_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        self.sleeper(ignore_term=True)
        child = self.child_supervisor(grace=0.5)
        pids = eventually(self.pids)
        self.assertEqual(len(pids), 2)
        os.kill(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=WAIT), 0)
        self.assertFalse(alive(pids[0]))
        self.assertTrue(eventually(lambda: not alive(pids[1])))
        self.assertIn(f"sleeper: killing pid {pids[0]}", self.log())

    def test_a_crashing_job_is_restarted_with_backoff_and_never_stops_another(self):
        runs = self.tmp / "runs"
        for module, code in (("crasher", 1), ("steady", 0)):
            self.module(module, f"""def main():
    with open({str(runs)!r}, "a") as out:
        out.write("{module}\\n")
    if {code}:
        raise RuntimeError("boom")
    return 0
""")
        self.plist("crasher", "crasher", RunAtLoad=True, StartInterval=3600, ThrottleInterval=0)
        self.plist("steady", "steady", StartInterval=1, ThrottleInterval=0)
        self.job_list("crasher\nsteady\n")
        child = self.child_supervisor()

        def counts() -> dict:
            lines = runs.read_text().split() if runs.exists() else []
            return {name: lines.count(name) for name in ("crasher", "steady")}

        self.assertTrue(eventually(lambda: counts()["crasher"] >= 3 and counts()["steady"] >= 2), counts())
        os.kill(child.pid, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=WAIT), 0)
        self.assertIn("crasher: ended, exit 1", self.log())
        self.assertIn("crasher: failure 2 in a row", self.log())
        self.assertIn("RuntimeError: boom", (self.office / "logs" / "crasher.err.log").read_text())
        self.assertNotIn("steady: ended, exit 1", self.log())

    def test_a_job_runs_exactly_as_launchd_would_so_the_office_switches_work_the_same(self):
        # The fake reads a switch file in the office the way a fleet job reads desks/<desk>/enabled.
        self.module("switchy", f"""import os
def main():
    on = os.path.exists({str(self.office / 'desks' / 'ron' / 'enabled')!r})
    print("on" if on else "off", os.getcwd(), oct(os.umask(0)))
    return 0
""")
        # Background, as the kit's jobs are: darwin's background policy runs it slower on a busy Mac, never differently.
        self.plist("switchy", "switchy", RunAtLoad=True, ProcessType="Background")
        job = loops.load_job("switchy")
        for _ in range(2):
            run = loops.start_job(job)
            self.addCleanup(lambda run=run: run.poll() is None and run.kill())
            self.assertEqual(run.wait(timeout=BACKGROUND_WAIT), 0)
            self.write_file(self.office / "desks" / "ron" / "enabled", "")
        out = (self.office / "logs" / "switchy.out.log").read_text().splitlines()
        self.assertEqual(out, [f"off {self.office} 0o77", f"on {self.office} 0o77"])
        self.assertEqual(os.stat(self.office / "logs" / "switchy.out.log").st_mode & 0o777, 0o600)


class GuardSeamTests(LoopsCase):
    def marker(self, pid: int, jobs: list) -> None:
        self.write_file(self.office / "loops" / "running.json", json.dumps({"pid": pid, "jobs": jobs, "started": 1}))

    def held(self):
        # The supervisor's lock, held from another open file, as a live fleet loops holds it.
        stack = contextlib.ExitStack()
        fd = stack.enter_context(safefs.opened_dir(str(self.office), "locks"))
        stack.enter_context(safefs.held_lock(fd, config.LOOPS_LOCK, blocking=False))
        return stack

    def test_the_protected_folder_guard_stays_on_unless_the_seam_is_switched_on_and_the_loops_run(self):
        repo = f"{self.home_dir}/Documents/web-app"
        with mock.patch.object(sys, "platform", "darwin"):
            # A marker whose pid lives but whose lock no one holds is a stale one: a reused pid never counts.
            self.marker(os.getpid(), ["owlpost", "map", "morning"])
            self.assertEqual(loops.running_jobs(), ())
            with mock.patch.object(config, "PROTECTED_DIRS_OK_UNDER_TERMINAL_LOOPS", True):
                self.assertIn("inside ~/Documents", gitops.protected_reason(repo))
            held = self.held()
            self.addCleanup(held.close)
            self.assertEqual(loops.running_jobs(), ("owlpost", "map", "morning"))
            self.assertFalse(config.PROTECTED_DIRS_OK_UNDER_TERMINAL_LOOPS)
            self.assertIn("inside ~/Documents", gitops.protected_reason(repo))
            with mock.patch.object(config, "PROTECTED_DIRS_OK_UNDER_TERMINAL_LOOPS", True):
                self.assertIsNone(gitops.protected_reason(repo))
                self.assertEqual(gitops.check_unprotected(repo), repo)
                self.marker(os.getpid(), ["owlpost"])
                self.assertIn("inside ~/Documents", gitops.protected_reason(repo))
                gone = subprocess.Popen(["/usr/bin/true"])
                gone.wait()
                self.marker(gone.pid, ["owlpost", "map"])
                self.assertEqual(loops.running_jobs(), ())
                self.assertIn("inside ~/Documents", gitops.protected_reason(repo))
                os.unlink(self.office / "loops" / "running.json")
                self.assertIn("inside ~/Documents", gitops.protected_reason(repo))

    def test_the_kit_ships_the_seam_off(self):
        self.assertIs(kit_setting("PROTECTED_DIRS_OK_UNDER_TERMINAL_LOOPS"), False)
        self.assertEqual(kit_setting("TERMINAL_LOOPS_REPO_JOBS"), ("owlpost", "map"))


class CommandTests(LoopsCase):
    def main(self, *argv) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tools.main(list(argv))
        return code, out.getvalue()

    def test_dry_run_prints_each_jobs_schedule_and_starts_nothing(self):
        outbox = self.tmp / "outbox"
        self.plist("post", StartInterval=300, WatchPaths=[str(outbox)])
        self.plist("odd", UserName="root", StartInterval=60)
        self.job_list("post\nodd\n")
        (self.agents / "com.hogwarts.post.plist").write_text("x")
        with mock.patch.object(loops, "start_job", side_effect=AssertionError("started")):
            code, out = self.main("loops", "--dry-run")
        data = json.loads(out)["data"]
        self.assertEqual(code, 0)
        post, odd = data["jobs"]
        self.assertEqual((post["interval"], post["watch"], post["launchd_too"]), (300, [str(outbox)], True))
        self.assertIn("keys fleet loops does not run: UserName", odd["error"])
        self.assertEqual(data["running"], [])

    def test_without_the_job_list_launchd_runs_the_loops_and_fleet_loops_refuses(self):
        code, out = self.main("loops", "--dry-run")
        self.assertEqual((code, json.loads(out)["ok"]), (1, False))
        self.assertIn("launchd runs the loops", json.loads(out)["error"])
        said: list = []
        self.assertEqual(loops.supervise(say=said.append), 1)
        self.assertIn("launchd runs the loops", said[0])
        self.assertFalse((self.office / "loops" / "running.json").exists())
