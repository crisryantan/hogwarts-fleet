"""fleet loops: the fleet's background jobs run from a terminal you start, instead of launchd.

macOS keeps launchd jobs out of ~/Documents (and Desktop, Downloads, iCloud Drive) without a privacy grant, so the
Owl Post, the Map and the closer fail on a repo cloned there. A process started from a terminal window gets that
terminal's folder access instead, and so does every process it starts. This module is that process: one foreground
supervisor that runs each job named in the office file loops/jobs on the schedule its own launchd/ plist gives.

- The plist stays the one source of each job: ProgramArguments (only the fleet's wrapper line), WorkingDirectory,
  StandardOutPath and StandardErrorPath (only in the office logs folder), Umask, EnvironmentVariables, RunAtLoad,
  StartInterval, StartCalendarInterval, WatchPaths (polled every LOOPS_TICK_SECONDS by mtime), KeepAlive (true or
  false), ThrottleInterval, AbandonProcessGroup and ProcessType (Background runs under taskpolicy -b, as launchd
  throttles it; Standard runs as is). A plist with any other key or value is refused, never half run.
- One run of a job at a time. A trigger while it runs is kept and runs it once more after it ends, as one.
- A run that fails is logged, and the job may not start again for a backoff that doubles with each failure in a row
  (config.LOOPS_BACKOFF_*). A job with StartInterval, WatchPaths or KeepAlive is retried then; a calendar job waits
  for its next slot, as under launchd. A failure in one job, or in the supervisor's handling of it, never stops another.
- A job whose plist is also in ~/Library/LaunchAgents is skipped, checked every tick, so launchd and fleet loops never
  both run it. A RunAtLoad or KeepAlive start waits until launchd no longer has it; other triggers are dropped.
- Unless AbandonProcessGroup, a run's process group is SIGKILLed as its leader ends, as launchd does: the group id is
  not reused while any member lives, and macOS hands out pids in order, so only the run's own leftovers are hit.
- One supervisor at a time (locks/loops.lock, held for its life). SIGINT, SIGTERM or SIGHUP stops it: each running job
  gets SIGTERM (its whole process group unless AbandonProcessGroup), then SIGKILL after LOOPS_STOP_GRACE_SECONDS, or
  at once on a second signal. While it runs, loops/running.json names the jobs it runs here, launchd's left out; it
  removes any older marker as soon as it holds the lock, and a reader trusts the marker only while the lock is held
  and its pid lives (running_jobs).
- The on/off files in the office (desks/<desk>/enabled, patrol/shadow, auto-close and the other opt-ins) are read by
  the jobs themselves, which run exactly the command launchd would, so they switch the same things.
- build_notice is what a go and fleet worktree say about the jobs a build needs: start fleet loops when nothing runs
  them, or a warning when launchd alone runs them and the repo sits in a folder launchd jobs cannot read. Neither
  refuses anything.
"""
from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from typing import Callable, Iterator, NamedTuple, Optional

from fleet import config, gitops, safefs
from fleet.safefs import FleetError

NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
JOBS_MAX_BYTES = 4096
PLIST_MAX_BYTES = 256 * 1024
RUNNING_MAX_BYTES = 4096
PLIST_KEYS = {
    "Label", "ProgramArguments", "WorkingDirectory", "StandardOutPath", "StandardErrorPath", "Umask", "ProcessType",
    "EnvironmentVariables", "RunAtLoad", "StartInterval", "StartCalendarInterval", "WatchPaths", "KeepAlive",
    "ThrottleInterval", "AbandonProcessGroup",
}
CALENDAR_RANGES = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}
LAUNCHD_UMASK = 0o022
STOP_POLL_SECONDS = 0.1
# How each ProcessType launchd knows is applied here. Background is darwin's background policy, as launchd applies it.
PROCESS_TYPES = {"Background": ("/usr/sbin/taskpolicy", "-b"), "Standard": ()}
# The triggers a job keeps while launchd also has it: the start launchd itself would make on load.
LOAD_REASONS = ("load", "keep-alive")
# A reader's shared lock lasts an instant, so a starting supervisor waits this long for it rather than refusing.
LOCK_WAIT_SECONDS = 3


class Job(NamedTuple):
    name: str
    argv: tuple
    cwd: str
    out_log: str
    err_log: str
    umask: int
    env: dict
    run_at_load: bool
    keep_alive: bool
    interval: Optional[int]
    calendar: dict  # frozenset of keys -> set of value tuples in sorted key order
    watch: tuple
    throttle: int
    abandon_group: bool


# The job list and the plists


def chosen_jobs() -> Optional[list]:
    """The job names in loops/jobs, in order and once each (# starts a comment). None when the file is not there,
    which means launchd runs the loops."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.LOOPS_DIR) as fd:
            raw = safefs.read_regular(fd, config.LOOPS_JOBS_FILE, JOBS_MAX_BYTES, "the loops job list")
    except safefs.Missing:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FleetError("the loops job list is not UTF-8") from None
    names = []
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if NAME.fullmatch(name) is None:
            raise FleetError("the loops job list names something that is not a job name")
        if name not in names:
            names.append(name)
    return names


def _flag(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if type(value) is not bool:
        raise FleetError(f"{key} must be true or false")
    return value


def _count(data: dict, key: str, default: Optional[int], least: int) -> Optional[int]:
    if key not in data:
        return default
    value = data[key]
    if type(value) is not int or value < least:
        raise FleetError(f"{key} must be a whole number of at least {least}")
    return value


def _log_name(data: dict, key: str) -> str:
    """A log path the plist names: a plain file right inside the office logs folder."""
    value = data.get(key)
    if not isinstance(value, str) or os.path.dirname(value) != config.logs_dir():
        raise FleetError(f"{key} must be a file in {config.logs_dir()}")
    return safefs.check_component(os.path.basename(value))


def _calendar(value: object) -> dict:
    entries = [value] if isinstance(value, dict) else value
    if not isinstance(entries, list) or not entries:
        raise FleetError("StartCalendarInterval must be a dict or a list of dicts")
    index: dict = {}
    for entry in entries:
        if not isinstance(entry, dict) or not set(entry) <= set(CALENDAR_RANGES):
            raise FleetError("StartCalendarInterval has an entry fleet loops cannot read")
        if "Day" in entry and "Weekday" in entry:
            raise FleetError("StartCalendarInterval entries with both Day and Weekday are not supported")
        for key, number in entry.items():
            low, high = CALENDAR_RANGES[key]
            if type(number) is not int or not low <= number <= high:
                raise FleetError(f"StartCalendarInterval {key} must be {low} to {high}")
        keys = tuple(sorted(entry))
        values = tuple(entry[key] % 7 if key == "Weekday" else entry[key] for key in keys)
        index.setdefault(keys, set()).add(values)
    return index


def parse_job(name: str, raw: bytes) -> Job:
    """A job from its plist bytes, refused unless every key is one fleet loops runs the way launchd does."""
    label = f"com.hogwarts.{name}"
    try:
        data = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - plistlib raises several types for a bad file
        raise FleetError(f"{label}.plist is not a valid plist") from None
    if not isinstance(data, dict):
        raise FleetError(f"{label}.plist is not a dict")
    unknown = sorted(set(data) - PLIST_KEYS)
    if unknown:
        raise FleetError(f"{label}.plist has keys fleet loops does not run: {', '.join(unknown)[:120]}")
    if data.get("Label") != label:
        raise FleetError(f"{label}.plist has another Label")
    argv = data.get("ProgramArguments")
    if (not isinstance(argv, list) or len(argv) != len(config.PYTHON_WRAPPER) + 2
            or tuple(argv[:-2]) != config.PYTHON_WRAPPER or argv[-2] != "-c" or not isinstance(argv[-1], str)
            or "\x00" in argv[-1]):
        raise FleetError(f"{label}.plist must run the fleet's wrapper line with -c")
    if data.get("WorkingDirectory") != config.OFFICE_ROOT:
        raise FleetError(f"{label}.plist must work in the office")
    env = data.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) and key and "=" not in key
                                            and "\x00" not in key + value for key, value in env.items()):
        raise FleetError(f"{label}.plist EnvironmentVariables must map names to text")
    process_type = data.get("ProcessType", "Standard")
    if process_type not in PROCESS_TYPES:
        raise FleetError(f"{label}.plist ProcessType must be one of {', '.join(sorted(PROCESS_TYPES))}")
    try:
        umask = _count(data, "Umask", LAUNCHD_UMASK, 0)
        if umask > 0o777:
            raise FleetError("Umask must be at most 0777")
        watch = data.get("WatchPaths", [])
        if not isinstance(watch, list) or not all(isinstance(path, str) and path.startswith("/") and "\x00" not in path
                                                  for path in watch):
            raise FleetError("WatchPaths must list absolute paths")
        if isinstance(data.get("KeepAlive"), dict):
            raise FleetError("KeepAlive conditions are not supported, only true or false")
        job = Job(name=name, argv=PROCESS_TYPES[process_type] + tuple(argv), cwd=config.OFFICE_ROOT,
                  out_log=_log_name(data, "StandardOutPath"),
                  err_log=_log_name(data, "StandardErrorPath"), umask=umask, env=dict(env),
                  run_at_load=_flag(data, "RunAtLoad", False), keep_alive=_flag(data, "KeepAlive", False),
                  interval=_count(data, "StartInterval", None, 1),
                  calendar=_calendar(data["StartCalendarInterval"]) if "StartCalendarInterval" in data else {},
                  watch=tuple(watch), throttle=_count(data, "ThrottleInterval", config.LOOPS_THROTTLE_SECONDS, 0),
                  abandon_group=_flag(data, "AbandonProcessGroup", False))
    except FleetError as exc:
        raise FleetError(f"{label}.plist: {exc}") from None
    if not (job.run_at_load or job.keep_alive or job.interval or job.calendar or job.watch):
        raise FleetError(f"{label}.plist has nothing that starts it")
    return job


def load_job(name: str) -> Job:
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise FleetError("that is not a job name")
    with safefs.opened_dir(config.OFFICE_ROOT, "launchd") as fd:
        raw = safefs.read_regular(fd, f"com.hogwarts.{name}.plist", PLIST_MAX_BYTES, f"the {name} plist")
    return parse_job(name, raw)


def launchd_has(name: str) -> bool:
    """Whether launchd may run the job too: its plist is in ~/Library/LaunchAgents, loaded or not."""
    return os.path.lexists(f"{config.launch_agents_dir()}/com.hogwarts.{name}.plist")


# Triggers


def calendar_matches(index: dict, moment: time.struct_time) -> bool:
    """launchd's rule: every key an entry names matches this local minute (Weekday 0 and 7 are Sunday)."""
    have = {"Minute": moment.tm_min, "Hour": moment.tm_hour, "Day": moment.tm_mday,
            "Weekday": (moment.tm_wday + 1) % 7, "Month": moment.tm_mon}
    return any(tuple(have[key] for key in keys) in values for keys, values in index.items())


def calendar_due(index: dict, after_minute: int, upto_minute: int,
                 localtime: Callable[[float], time.struct_time] = time.localtime) -> bool:
    """Whether a slot falls in the minutes after after_minute up to upto_minute (minutes since the epoch), looking
    back at most config.LOOPS_CATCH_UP_SECONDS. Slots missed in one gap run once, as launchd coalesces them."""
    first = max(after_minute + 1, upto_minute - config.LOOPS_CATCH_UP_SECONDS // 60)
    return any(calendar_matches(index, localtime(minute * 60)) for minute in range(first, upto_minute + 1))


def watch_mark(path: str) -> Optional[tuple]:
    """What a change to a watched path changes, read without following a link; None when it is not there."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns)


# Starting and stopping runs


def start_job(job: Job) -> subprocess.Popen:
    """The job's own command, as launchd runs it: its working folder, environment and umask, appending to its logs,
    in a new session, so the terminal's Ctrl+C reaches only the supervisor, which stops it in turn."""
    with safefs.opened_dir(config.OFFICE_ROOT, "logs", create=True) as logs:
        out = safefs.open_append(logs, job.out_log, "a job log")
        try:
            err = out if job.err_log == job.out_log else safefs.open_append(logs, job.err_log, "a job log")
            try:
                return subprocess.Popen(list(job.argv), cwd=job.cwd, env=dict(job.env), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, start_new_session=True, close_fds=True,
                                        umask=job.umask)
            finally:
                if err != out:
                    os.close(err)
        finally:
            os.close(out)


def _signal_run(process: subprocess.Popen, number: int, whole_group: bool) -> None:
    """Signal a run still unreaped, so its pid and process group id are still its own."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if whole_group:
            os.killpg(process.pid, number)
        else:
            process.send_signal(number)


def _end_leftovers(slot: "Slot", process: subprocess.Popen) -> None:
    """SIGKILL what is left of a run's process group as its leader is reaped, unless the job abandons its group. The
    id names no other group: it is not reused while a member lives, and macOS hands out new pids in order."""
    if not slot.job.abandon_group:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


class Slot:
    """One job's state in the supervisor."""

    def __init__(self, job: Job, now: float) -> None:
        self.job = job
        self.process: Optional[subprocess.Popen] = None
        self.pending: Optional[str] = "load" if job.run_at_load or job.keep_alive else None
        self.last_start: Optional[float] = None
        self.failures = 0
        self.not_before = 0.0
        self.next_interval = None if job.interval is None else now + job.interval
        self.last_minute = int(now // 60)
        self.marks = {path: watch_mark(path) for path in job.watch}
        self.skipped = False


class Supervisor:
    """The scheduling half, with the clock, the start and the log handed in, so tests drive it one tick at a time."""

    def __init__(self, jobs: list, now: float, say: Callable[[str], None],
                 start: Callable[[Job], subprocess.Popen] = start_job,
                 owned_by_launchd: Callable[[str], bool] = launchd_has) -> None:
        self.say, self.start, self.owned_by_launchd = say, start, owned_by_launchd
        self.slots = []
        for job in jobs:
            try:
                slot = Slot(job, now)
                self._ownership(slot)
            except Exception as exc:  # noqa: BLE001 - one job's trouble never stops the others
                say(f"{job.name}: not run, {type(exc).__name__}: {str(exc)[:200]}")
                continue
            self.slots.append(slot)

    def tick(self, now: float) -> None:
        for slot in self.slots:
            try:
                self._reap(slot, now)
                self._ownership(slot)
                self._trigger(slot, now)
                self._maybe_start(slot, now)
            except Exception as exc:  # noqa: BLE001 - one job's trouble never stops the others
                self.say(f"{slot.job.name}: supervisor error, {type(exc).__name__}: {str(exc)[:200]}")
                self._failed(slot, now)

    def running(self) -> list:
        return [slot for slot in self.slots if slot.process is not None]

    def runs_here(self) -> list:
        """The jobs this supervisor runs now: every loaded one launchd does not also have."""
        return [slot.job.name for slot in self.slots if not slot.skipped]

    def _ownership(self, slot: Slot) -> None:
        name = slot.job.name
        owned = self.owned_by_launchd(name)
        if owned and not slot.skipped:
            self.say(f"{name}: skipped, launchd has com.hogwarts.{name}.plist in LaunchAgents; unload and remove it to"
                     " run this job here")
        elif slot.skipped and not owned:
            self.say(f"{name}: launchd no longer has it, so it runs here again")
        slot.skipped = owned

    def _failed(self, slot: Slot, now: float) -> None:
        slot.failures += 1
        wait = min(config.LOOPS_BACKOFF_FIRST_SECONDS * 2 ** min(slot.failures - 1, 20),
                   config.LOOPS_BACKOFF_MAX_SECONDS)
        slot.not_before = now + wait
        job = slot.job
        retried = bool(job.interval or job.watch or job.keep_alive)
        if retried:
            slot.pending = slot.pending or "retry"
        self.say(f"{job.name}: failure {slot.failures} in a row; "
                 + (f"retrying in {wait}s" if retried else f"next slot, no sooner than {wait}s"))

    def _reap(self, slot: Slot, now: float) -> None:
        if slot.process is None:
            return
        code = slot.process.poll()
        if code is None:
            return
        process, slot.process = slot.process, None
        _end_leftovers(slot, process)
        how = f"exit {code}" if code >= 0 else f"killed by signal {-code}"
        self.say(f"{slot.job.name}: ended, {how}")
        if code == 0:
            slot.failures, slot.not_before = 0, 0.0
        else:
            self._failed(slot, now)
        if slot.job.keep_alive:
            slot.pending = slot.pending or "keep-alive"

    def _trigger(self, slot: Slot, now: float) -> None:
        job = slot.job
        if slot.next_interval is not None and now >= slot.next_interval:
            slot.pending = slot.pending or "interval"
            slot.next_interval = now + job.interval
        elif slot.next_interval is not None and slot.next_interval - now > job.interval:
            slot.next_interval = now + job.interval  # the clock was set back
        minute = int(now // 60)
        if job.calendar and minute > slot.last_minute and calendar_due(job.calendar, slot.last_minute, minute):
            slot.pending = slot.pending or "calendar"
        slot.last_minute = minute  # a clock set back only moves this back, starting nothing
        for path in job.watch:
            mark = watch_mark(path)
            if mark != slot.marks[path]:
                slot.marks[path] = mark
                slot.pending = slot.pending or "watch"

    def _maybe_start(self, slot: Slot, now: float) -> None:
        if slot.pending is None or slot.process is not None:
            return
        # A clock set back never holds a job for longer than its throttle or the longest backoff.
        slot.not_before = min(slot.not_before, now + config.LOOPS_BACKOFF_MAX_SECONDS)
        slot.last_start = None if slot.last_start is None else min(slot.last_start, now)
        earliest = slot.not_before if slot.last_start is None else max(slot.not_before,
                                                                         slot.last_start + slot.job.throttle)
        if now < earliest:
            return
        name = slot.job.name
        if slot.skipped:
            if slot.pending not in LOAD_REASONS:
                slot.pending = None
            return
        reason, slot.pending, slot.last_start = slot.pending, None, now
        slot.process = self.start(slot.job)
        self.say(f"{name}: started pid {slot.process.pid} ({reason})")

    def stop(self, clock: Callable[[], float], sleep: Callable[[float], None], hurry: Callable[[], bool]) -> None:
        """SIGTERM every running job, then SIGKILL what is left after the grace period or once hurry() says so."""
        for slot in self.running():
            self.say(f"{slot.job.name}: stopping pid {slot.process.pid}")
            _signal_run(slot.process, signal.SIGTERM, not slot.job.abandon_group)
        deadline = clock() + config.LOOPS_STOP_GRACE_SECONDS
        while self.running() and clock() < deadline and not hurry():
            for slot in self.running():
                code = slot.process.poll()
                if code is not None:
                    process, slot.process = slot.process, None
                    _end_leftovers(slot, process)
                    self.say(f"{slot.job.name}: stopped, " + (f"exit {code}" if code >= 0 else f"signal {-code}"))
            if self.running():
                sleep(STOP_POLL_SECONDS)
        for slot in self.running():
            self.say(f"{slot.job.name}: killing pid {slot.process.pid}")
            _signal_run(slot.process, signal.SIGKILL, not slot.job.abandon_group)
            slot.process.wait()
            slot.process = None


# The marker a live supervisor keeps


def write_running(jobs: list) -> None:
    data = {"pid": os.getpid(), "jobs": jobs, "started": int(time.time())}
    with safefs.opened_dir(config.OFFICE_ROOT, config.LOOPS_DIR, create=True) as fd:
        safefs.write_new(fd, config.LOOPS_RUNNING_FILE, json.dumps(data, ensure_ascii=True).encode("ascii"))


def clear_running() -> None:
    with contextlib.suppress(FleetError, OSError), safefs.opened_dir(config.OFFICE_ROOT, config.LOOPS_DIR) as fd:
        os.unlink(config.LOOPS_RUNNING_FILE, dir_fd=fd)


def supervisor_holds_lock() -> bool:
    """Whether a fleet loops holds locks/loops.lock now: a shared lock taken without waiting is refused. The kernel
    drops the lock with its holder, so a killed supervisor never reads as live."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, "locks") as fd, \
                safefs.held_lock(fd, config.LOOPS_LOCK, blocking=False, shared=True):
            return False
    except safefs.Busy:
        return True
    except (FleetError, OSError):
        return False


def running_jobs() -> tuple:
    """The jobs a live fleet loops runs here, from its marker, or () when none runs. Live means the supervisor lock is
    held and the marker's pid runs as one of your processes, so neither a stale marker nor a reused pid counts."""
    if not supervisor_holds_lock():
        return ()
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.LOOPS_DIR) as fd:
            data = json.loads(safefs.read_regular(fd, config.LOOPS_RUNNING_FILE, RUNNING_MAX_BYTES, "the loops marker"))
        pid, jobs = data["pid"], data["jobs"]
        if type(pid) is not int or pid <= 1 or not isinstance(jobs, list) or not all(isinstance(j, str) for j in jobs):
            return ()
        os.kill(pid, 0)
    except (FleetError, OSError, ValueError, KeyError, TypeError):
        return ()
    return tuple(jobs)


def blind_folder(path: str) -> Optional[str]:
    """On macOS, the folder of config.LAUNCHD_BLIND_DIRS a repo folder sits in, else None. Elsewhere always None."""
    if sys.platform != "darwin" or not isinstance(path, str):
        return None
    folded = path.lower()
    for name in config.LAUNCHD_BLIND_DIRS:
        root = f"{config.USER_HOME_DIR}/{name}".lower()
        if folded == root or folded.startswith(root + "/"):
            return name
    return None


def build_notice(repo_dir: Optional[str] = None) -> list:
    """What a go or fleet worktree says about the jobs a build needs (config.BUILD_JOBS), [] when they run where the
    repo lets them. Not running at all: start fleet loops. Run by launchd alone on macOS while the repo sits in a
    folder launchd jobs cannot read: a warning. Never refuses and never raises."""
    try:
        live = set(running_jobs())
        launchd = {job for job in config.BUILD_JOBS if job not in live and launchd_has(job)}
    except Exception:  # noqa: BLE001 - a notice never stops a go
        return []
    missing = [job for job in config.BUILD_JOBS if job not in live and job not in launchd]
    if missing:
        try:
            listed = chosen_jobs() is not None
        except (FleetError, OSError):
            listed = True  # a list is there, only unreadable
        hint = "" if listed else " There is no loops job list yet: sh scripts/loops-setup.sh makes one."
        return [f"Start `fleet loops`, the background jobs are not running, so the build will not move on by itself"
                f" ({_named(missing)} under neither fleet loops nor launchd).{hint}"]
    folder = blind_folder(repo_dir) if launchd else None
    if folder is None and launchd and repo_dir is not None:
        # The jobs run git in a linked worktree's main checkout too, so that folder counts as well.
        with contextlib.suppress(Exception):
            folder = blind_folder(gitops.repo_dirs(repo_dir)["main_dir"])
    if folder is None:
        return []
    return [f"Warning: the repo is inside ~/{folder}, which macOS keeps launchd jobs out of, and"
            f" {_named(sorted(launchd))} under launchd here, so git fails for them and the build will not move on by"
            " itself. Run the jobs from a terminal with `fleet loops` (sh scripts/loops-setup.sh moves them there)."]


def _named(jobs: list) -> str:
    return f"{' and '.join(jobs)} {'runs' if len(jobs) == 1 else 'run'}"


# The command


def _say(line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n"
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(stamped)
        sys.stdout.flush()
    with contextlib.suppress(FleetError, OSError), safefs.opened_dir(config.OFFICE_ROOT, "logs", create=True) as fd:
        safefs.append_regular(fd, config.LOOPS_LOG, stamped.encode("utf-8", "replace"), "the loops log")


def _jobs_or_refuse() -> list:
    names = chosen_jobs()
    if names is None:
        raise FleetError(f"{config.OFFICE_ROOT}/{config.LOOPS_DIR}/{config.LOOPS_JOBS_FILE} is not there, so launchd"
                         " runs the loops; scripts/loops-setup.sh moves them to a terminal")
    if not names:
        raise FleetError("the loops job list names no job")
    return names


def plan() -> dict:
    """fleet loops --dry-run: what each listed job would do, started nothing."""
    jobs = []
    for name in _jobs_or_refuse():
        try:
            job = load_job(name)
        except Exception as exc:  # noqa: BLE001 - every listed job is reported, a bad one with why
            jobs.append({"job": name, "error": str(exc)[:300]})
            continue
        entries = sum(len(values) for values in job.calendar.values())
        jobs.append({"job": name, "run_at_load": job.run_at_load, "keep_alive": job.keep_alive,
                     "interval": job.interval, "calendar_entries": entries, "watch": list(job.watch),
                     "logs": [job.out_log, job.err_log], "launchd_too": launchd_has(name)})
    return {"jobs": jobs, "running": list(running_jobs())}


@contextlib.contextmanager
def _signals_counted() -> Iterator[list]:
    """SIGINT, SIGTERM and SIGHUP only counted here; the loop reads the count and stops."""
    came: list = []
    previous = {number: signal.signal(number, lambda signum, frame: came.append(signum))
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield came
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def supervise(clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              say: Callable[[str], None] = _say) -> int:
    """Run the listed jobs until a signal. Exit 1 when another supervisor runs or no job can be run."""
    try:
        names = _jobs_or_refuse()
    except FleetError as exc:
        say(f"fleet loops: {exc}")
        return 1
    with _signals_counted() as came:
        try:
            with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as fd, \
                    safefs.held_lock(fd, config.LOOPS_LOCK, blocking=True, timeout=LOCK_WAIT_SECONDS):
                return _supervise_locked(names, clock, sleep, say, came)
        except safefs.Busy:
            say("fleet loops: another fleet loops is already running")
            return 1


def _supervise_locked(names: list, clock: Callable[[], float], sleep: Callable[[float], None],
                      say: Callable[[str], None], came: list) -> int:
    # A marker a killed supervisor left goes before anything else, so while the lock is held the marker is this one's.
    clear_running()
    jobs = []
    for name in names:
        try:
            jobs.append(load_job(name))
        except Exception as exc:  # noqa: BLE001 - one bad plist never stops the other jobs
            say(f"{name}: not run, {str(exc)[:300]}")
    supervisor = Supervisor(jobs, clock(), say)
    if not supervisor.slots:
        say("fleet loops: no job could be loaded")
        return 1
    try:
        here = supervisor.runs_here()
        write_running(here)
        loaded = ", ".join(slot.job.name for slot in supervisor.slots)
        say(f"fleet loops: running {loaded} (pid {os.getpid()}); Ctrl+C stops them")
        while not came:
            supervisor.tick(clock())
            if supervisor.runs_here() != here:
                here = supervisor.runs_here()
                write_running(here)
            sleep(config.LOOPS_TICK_SECONDS)
    finally:
        seen = len(came)
        say("fleet loops: stopping")
        supervisor.stop(clock, sleep, lambda: len(came) > seen)
        clear_running()
        say("fleet loops: stopped")
    return 0
