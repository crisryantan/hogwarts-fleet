"""Run the office's test suites with every test module in its own process, several at a time.

  /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty <office>/run_suites.py [--jobs N] [SUITE ...]

SUITE is tests or tests_fleet, and both run when none is named. --jobs is how many modules run at once:
6 by default, or the number of cores on a Mac with fewer.

- Finds the office folder from this file's own path, so it runs the same from any folder.
- Runs each test module alone in a child process, in the office folder, with an empty environment (but for
  TEST_TMP_ROOT, passed on when this runner has it, which tests/support.py makes its temp folders in, with TMPDIR
  and xcrun's cache set inside it) and
  the same hardened line as the reference discover run, narrowed to that one module file:
    /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty
        -m unittest discover -s <office>/<suite> -t <office> -p <module file>
  A module's tests run together in one process as they do in a serial run, and no two modules share one.
- Starts the slowest modules first, so the run ends soon after its slowest module does. The modules in
  SERIAL run one at a time after every other module has finished.
- Without TEST_TMP_ROOT, first checks that it may make a folder in /private/tmp, where the tests make theirs, and
  stops before any module when it may not (a sandbox that denies it), saying to set TEST_TMP_ROOT.
- Prints the full output of every module that failed, then one line per suite, and exits 1 if any module
  failed, timed out, could not be started or printed no unittest summary, else 0. A module that ends
  without a summary counts as failed, never as passed with no tests.

The discover commands in README.md stay the reference way to run the suites. This is a faster way to run
exactly the same tests.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, NamedTuple, Optional

OFFICE = Path(__file__).resolve().parent
SUITES = ("tests", "tests_fleet")
# The interpreter line every fleet script, hook and launchd job uses (fleet/config.py PYTHON_WRAPPER).
WRAPPER = ("/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty")
DEFAULT_JOBS = 6
MAX_JOBS = 32
# unittest's own default pattern, and the names this runner can pass to -p as one exact file name.
DISCOVER_PATTERN = "test*.py"
# Where the tests make their temp folders, when the runner's own environment names one: a sandbox that denies the
# default /private/tmp, such as fleet verify's, sets it to a folder it may write.
TEMP_ROOT_ENV = "TEST_TMP_ROOT"
# Where the tests make their temp folders without one (tests/support.py).
SHARED_TEMP_ROOT = "/private/tmp"
NO_TEMP_ROOT = (f"run_suites.py: this process may not make a folder in {SHARED_TEMP_ROOT}, where the tests make their temp"
                f" folders, so every module would fail; set {TEMP_ROOT_ENV} to an absolute folder it may write, and"
                " each module gets TMPDIR and xcrun's cache inside it too\n")
MODULE_FILE = re.compile(r"test[A-Za-z0-9_]*\.py")
# A module still running after this long is killed with everything it started, and counts as failed.
MODULE_TIMEOUT_SECONDS = 600
KILL_WAIT_SECONDS = 10
# Seconds each slow module took alone, measured on a serial run, so the slowest start first. Any other
# module is estimated from its file size, which puts it after these.
RECORDED_SECONDS = {
    "tests_fleet/test_review_chain.py": 45,
    "tests_fleet/test_auto_close_merge.py": 43,
    "tests_fleet/test_auto_close_kill.py": 42,
    "tests_fleet/test_pr_followup.py": 40,
    "tests_fleet/test_worktree_cleanup_gap.py": 40,
    "tests_fleet/test_auto_close_landed.py": 39,
    "tests_fleet/test_pr_followup_post.py": 38,
    "tests_fleet/test_go.py": 38,
    "tests_fleet/test_auto_close_judge.py": 36,
    "tests_fleet/test_pr_followup_routing.py": 36,
    "tests_fleet/test_auto_push.py": 35,
    "tests_fleet/test_pr_followup_post_kill.py": 35,
    "tests_fleet/test_pr_followup_post_outcome.py": 35,
    "tests_fleet/test_worktree_cleanup.py": 33,
    "tests_fleet/test_pr_followup_post_reply.py": 33,
    "tests_fleet/test_many_tasks_guard.py": 32,
    "tests_fleet/test_pr_followup_patrol.py": 32,
    "tests_fleet/test_auto_close.py": 31,
    "tests_fleet/test_many_tasks.py": 31,
    "tests_fleet/test_many_tasks_lineage.py": 30,
    "tests_fleet/test_review_loop.py": 30,
    "tests_fleet/test_review_rounds.py": 29,
    "tests_fleet/test_spaces.py": 26,
    "tests_fleet/test_worktree_cleanup_kill.py": 22,
    "tests_fleet/test_run_slots.py": 21,
    "tests_fleet/test_verify.py": 15,
    "tests_fleet/test_portrait_cuts.py": 14,
    "tests_fleet/test_push.py": 12,
    "tests_fleet/test_toolchain.py": 9,
}
BYTES_PER_SECOND = 25_000
# Modules that must never run beside another module, each as "<suite>/<module file>". They run alone
# after the parallel batch. None needs it: each module makes its own temp folders under /private/tmp, and
# the shared fleet test base gives every test its own per-user temp folder, so no test reaches a fixed path.
SERIAL = frozenset()
RAN_LINE = re.compile(r"^Ran (\d+) tests? in \d+(?:\.\d+)?s$", re.MULTILINE)
STATUS_LINE = re.compile(r"(OK|FAILED)(?: \(([a-z ]+=\d+(?:, [a-z ]+=\d+)*)\))?")
COUNT_NAMES = ("failures", "errors", "skipped", "expected failures", "unexpected successes")


class RunnerError(Exception):
    """A suite this runner cannot run exactly as a serial discover run would."""


class Module(NamedTuple):
    suite: str
    name: str
    seconds: float  # recorded or estimated, used only to choose the order

    @property
    def label(self) -> str:
        return f"{self.suite}/{self.name}"


class Outcome(NamedTuple):
    module: Module
    returncode: Optional[int]  # None when the module never started
    output: str
    seconds: float
    problem: Optional[str] = None  # why the module did not run to its end, when it did not


# arguments


def suite_name(value: str) -> str:
    if value not in SUITES:
        raise argparse.ArgumentTypeError(f"unknown suite (choose from {', '.join(SUITES)})")
    return value


def job_count(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_JOBS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_JOBS}")
    return int(value)


def default_jobs() -> int:
    return min(DEFAULT_JOBS, os.cpu_count() or 1)


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_suites.py",
                                     description="Run the office's test suites, one process per test module.")
    parser.add_argument("--jobs", type=job_count, default=default_jobs(),
                        help=f"modules to run at once (default {default_jobs()})")
    parser.add_argument("suites", nargs="*", type=suite_name, metavar="SUITE",
                        help=f"{' or '.join(SUITES)} (default: both)")
    args = parser.parse_args(argv)
    args.suites = list(dict.fromkeys(args.suites or SUITES))
    return args


# modules


def discover(office: Path, suite: str) -> list:
    """Every module a serial discover run of the suite would load, each with its expected seconds.

    Refuses a suite it could not run module by module exactly: one it cannot list, one with no test
    modules, a module name -p would not match as one exact file, or a test package below the suite
    folder, whose modules a top-level listing would miss."""
    modules = []
    try:
        for entry in sorted(os.scandir(office / suite), key=lambda entry: entry.name):
            if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "__init__.py")):
                raise RunnerError(f"{suite} holds a test package, {entry.name!r}, and this runner only runs "
                                  "top-level modules")
            if not (entry.is_file() and fnmatch.fnmatchcase(entry.name, DISCOVER_PATTERN)):
                continue
            if MODULE_FILE.fullmatch(entry.name) is None:
                raise RunnerError(f"{suite} has a test module whose name is not plain letters, digits and underscores")
            recorded = RECORDED_SECONDS.get(f"{suite}/{entry.name}")
            seconds = float(recorded) if recorded is not None else entry.stat().st_size / BYTES_PER_SECOND
            modules.append(Module(suite, entry.name, seconds))
    except OSError as error:
        raise RunnerError(f"could not read the {suite} suite ({type(error).__name__})") from None
    if not modules:
        raise RunnerError(f"the {suite} suite has no test modules")
    return modules


def command(office: Path, module: Module, temp_root: Optional[str] = None) -> list:
    """The reference discover line for one suite, narrowed to one module file, with TEST_TMP_ROOT (and TMPDIR and
    xcrun_db inside it) set only when the runner was given one."""
    wrapper = list(WRAPPER)
    if temp_root is not None:
        # After env -i, so these are the only variables set: the test temp root, and TMPDIR and xcrun's cache in it, so
        # nothing a module starts writes a temp outside it.
        wrapper[2:2] = [f"{TEMP_ROOT_ENV}={temp_root}", f"TMPDIR={temp_root}", f"xcrun_db={temp_root}/xcrun_db"]
    return [*wrapper, "-m", "unittest", "discover", "-s", str(office / module.suite), "-t", str(office),
            "-p", module.name]


def temp_root() -> Optional[str]:
    """TEST_TMP_ROOT from this runner's environment when it names an absolute folder, else None."""
    value = os.environ.get("TEST_TMP_ROOT")  # the kit's one allowed environment read (tests/test_security.py)
    return value if value and os.path.isabs(value) and os.path.isdir(value) and "=" not in value else None


def shared_temp_usable(root: str = SHARED_TEMP_ROOT) -> bool:
    """Whether a folder can be made, and removed, in root."""
    try:
        os.rmdir(tempfile.mkdtemp(prefix="hogwarts-test-", dir=root))
    except OSError:
        return False
    return True


# running


def kill_group(child) -> None:
    """Kill a child and every process it started. Each child leads its own process group."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass  # the group has already gone


class Children:
    """Runs one module per child process and tracks the live ones, so an interrupt can stop them all."""

    def __init__(self, office: Path, popen=subprocess.Popen, kill: Callable = kill_group,
                 timeout: float = MODULE_TIMEOUT_SECONDS, temp_root: Optional[str] = None):
        self.office, self.popen, self.kill, self.timeout = office, popen, kill, timeout
        self.temp_root = temp_root
        self.lock = threading.Lock()
        self.live = set()
        self.stopped = False

    def run(self, module: Module) -> Outcome:
        started = time.monotonic()
        with self.lock:  # so stop() either sees this child or this call sees stop()
            if self.stopped:
                return Outcome(module, None, "", 0.0, "not started, because the run was interrupted")
            try:
                child = self.popen(command(self.office, module, self.temp_root), cwd=str(self.office), env={},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
            except OSError as error:
                return Outcome(module, None, "", 0.0, f"could not be started ({type(error).__name__})")
            self.live.add(child)
        problem = None
        try:
            raw, _ = child.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            problem = f"timed out after {self.timeout:g}s and was killed"
            raw = self._end(child)
        except Exception as error:
            self.kill(child)
            problem, raw = f"its output could not be read ({type(error).__name__}), so it was killed", b""
        except BaseException:
            self.kill(child)  # interrupted while waiting: the child must not outlive the run
            raise
        finally:
            with self.lock:
                self.live.discard(child)
        if problem is None and self.stopped:
            problem = "stopped, because the run was interrupted"
        return Outcome(module, child.returncode, (raw or b"").decode("utf-8", "replace"),
                       time.monotonic() - started, problem)

    def _end(self, child) -> bytes:
        """Kill a child that ran too long and return everything it printed before it died."""
        self.kill(child)
        try:
            raw, _ = child.communicate(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired as late:
            # Something it started left its process group and still holds the pipe open. Keep what came.
            raw = late.output
            child.stdout.close()
            child.wait()
        return raw or b""

    def stop(self) -> None:
        """Start no more modules, and kill every running one with everything it started."""
        with self.lock:
            self.stopped = True
            for child in self.live:
                self.kill(child)


class Interrupted(Exception):
    """The run was interrupted. Holds the outcomes of the modules that had already ended on their own."""

    def __init__(self, finished: list):
        super().__init__("interrupted")
        self.finished = finished


def run_all(run: Callable[[Module], Outcome], modules: list, jobs: int) -> list:
    """Run the modules jobs at a time, slowest first, then the SERIAL ones alone, one after another."""
    together = sorted((module for module in modules if module.label not in SERIAL), key=lambda module: -module.seconds)
    alone = sorted((module for module in modules if module.label in SERIAL), key=lambda module: -module.seconds)
    finished, lock = [], threading.Lock()

    def one(module: Module) -> Outcome:
        outcome = run(module)
        with lock:
            finished.append(outcome)
        return outcome

    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [pool.submit(one, module) for module in together]
        outcomes = [future.result() for future in futures]
        outcomes += [one(module) for module in alone]
    except KeyboardInterrupt:
        with lock:
            ended = list(finished)
        raise Interrupted(ended) from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return outcomes


# reporting


def summary(output: str) -> Optional[dict]:
    """The counts in unittest's closing lines, or None when the output does not end with them."""
    ran = None
    for ran in RAN_LINE.finditer(output):
        pass
    if ran is None:
        return None
    rest = [line for line in output[ran.end():].splitlines() if line.strip()]
    status = STATUS_LINE.fullmatch(rest[0].rstrip()) if rest else None
    if status is None:
        return None
    counts = dict.fromkeys(COUNT_NAMES, 0)
    for part in status.group(2).split(", ") if status.group(2) else ():
        name, value = part.split("=")
        if name not in counts:
            return None
        counts[name] = int(value)
    return {"ran": int(ran.group(1)), "ok": status.group(1) == "OK", **counts}


def passed(outcome: Outcome) -> bool:
    found = summary(outcome.output)
    return outcome.problem is None and outcome.returncode == 0 and found is not None and found["ok"]


def why(outcome: Outcome) -> str:
    if outcome.problem is not None:
        return outcome.problem
    if outcome.returncode is not None and outcome.returncode < 0:
        return f"killed by signal {-outcome.returncode}"
    if summary(outcome.output) is None:
        return f"exit code {outcome.returncode}, with no unittest summary in its output"
    return f"exit code {outcome.returncode}"


def suite_line(suite: str, outcomes: list) -> str:
    totals = dict.fromkeys(("ran",) + COUNT_NAMES, 0)
    unfinished = 0
    for outcome in outcomes:
        found = summary(outcome.output)
        if found is None:
            unfinished += 1
            continue
        for name in totals:
            totals[name] += found[name]
    failed = [outcome.module.name for outcome in outcomes if not passed(outcome)]
    parts = [f"{name}={totals[name]}" for name in COUNT_NAMES if totals[name]]
    if unfinished:
        parts.append(f"modules with no summary={unfinished}")
    line = f"{suite}: Ran {totals['ran']} tests in {len(outcomes)} modules, {'FAILED' if failed else 'OK'}"
    line += f" ({', '.join(parts)})" if parts else ""
    return line + (f"; failed: {', '.join(failed)}" if failed else "")


def print_failures(outcomes: list, out) -> list:
    """Print the full output of every module that failed, and return those outcomes."""
    failed = [outcome for outcome in outcomes if not passed(outcome)]
    for outcome in failed:
        out.write(f"\n===== {outcome.module.label}: {why(outcome)} =====\n")
        out.write(outcome.output if outcome.output.endswith("\n") or not outcome.output else outcome.output + "\n")
    if failed:
        out.write("\n")
    return failed


def report(outcomes: list, suites: list, seconds: float, out) -> int:
    failed = print_failures(outcomes, out)
    for suite in suites:
        out.write(suite_line(suite, [outcome for outcome in outcomes if outcome.module.suite == suite]) + "\n")
    verdict = f"FAILED: {len(failed)} of {len(outcomes)} modules" if failed else f"OK: all {len(outcomes)} modules"
    slowest = max(outcomes, key=lambda outcome: outcome.seconds)
    out.write(f"{verdict}, in {seconds:.1f}s (slowest: {slowest.module.label}, {slowest.seconds:.1f}s)\n")
    return 1 if failed else 0


def main(argv: Optional[list] = None, out=None, office: Path = OFFICE,
         run: Optional[Callable[[Module], Outcome]] = None) -> int:
    out = sys.stdout if out is None else out
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        modules = [module for suite in args.suites for module in discover(office, suite)]
    except RunnerError as error:
        out.write(f"run_suites.py: {error}\n")
        return 1
    root = temp_root()
    if root is None and not shared_temp_usable():
        out.write(NO_TEMP_ROOT)
        return 1
    children = Children(office, temp_root=root)
    out.write(f"Running {len(modules)} test modules from {' and '.join(args.suites)}, {args.jobs} at a time.\n")
    out.flush()
    started = time.monotonic()
    try:
        outcomes = run_all(children.run if run is None else run, modules, args.jobs)
    except Interrupted as interrupted:
        children.stop()
        failed = print_failures(interrupted.finished, out)
        out.write(f"Interrupted after {len(interrupted.finished)} of {len(modules)} modules ended, {len(failed)} "
                  "of them failed. Every running module was killed, and nothing is reported as passed.\n")
        return 130
    return report(outcomes, args.suites, time.monotonic() - started, out)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # A terminal closing or a kill reaches only this process, since each child leads its own group,
    # so both stop the children the way Ctrl-C does.
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGHUP, _interrupt)
    sys.exit(main())
