"""The parallel suite runner, run_suites.py: its arguments, which modules it finds, the line each child
runs, and the summary and exit code it reports. Every child here is faked, so no real suite runs."""
from __future__ import annotations

import contextlib
import io
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

import run_suites
from run_suites import Children, Module, Outcome, RunnerError

from fleet import config
from tests.support import temp_dir

RULE = "-" * 70


def unittest_output(ran: int, status: str = "OK", dots: str = "....") -> str:
    """What unittest prints for one module run."""
    return f"{dots}\n{RULE}\nRan {ran} test{'' if ran == 1 else 's'} in 0.012s\n\n{status}\n"


def write(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#" * size)
    return path


class FakeChild:
    """A module process. Each communicate() call takes the next step: bytes it printed, or an
    exception to raise. A step may also be a function, called with the child, that returns one."""

    def __init__(self, argv: list, steps: list, code: int, **kwargs):
        self.args, self.kwargs, self.steps, self.code = argv, kwargs, list(steps), code
        self.returncode = None
        self.stdout = mock.Mock()

    def communicate(self, timeout=None):
        step = self.steps.pop(0)
        if callable(step):
            step = step(self)
        if isinstance(step, BaseException):
            raise step
        self.returncode = self.code
        return step, None

    def wait(self):
        self.returncode = self.code
        return self.code


class ArgumentTests(unittest.TestCase):
    def parse(self, *argv):
        return run_suites.parse_args(list(argv))

    def test_both_suites_run_by_default_six_at_a_time_or_one_per_core(self):
        for cores, jobs in ((16, 6), (6, 6), (4, 4), (1, 1), (None, 1)):
            with self.subTest(cores=cores), mock.patch.object(run_suites.os, "cpu_count", return_value=cores):
                args = self.parse()
                self.assertEqual((args.suites, args.jobs), (["tests", "tests_fleet"], jobs))

    def test_suites_and_jobs_can_be_named(self):
        args = self.parse("--jobs", "3", "tests_fleet")
        self.assertEqual((args.suites, args.jobs), (["tests_fleet"], 3))
        self.assertEqual(self.parse("tests_fleet", "tests", "tests_fleet").suites, ["tests_fleet", "tests"])

    def test_an_unknown_suite_or_a_bad_job_count_is_refused(self):
        for argv in (("tests_other",), ("../tests",), ("office/tests",), ("--jobs", "0"), ("--jobs", "-1"),
                     ("--jobs", "x"), ("--jobs", "33"), ("--jobs",), ("--verbose",)):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                self.parse(*argv)
            self.assertEqual(raised.exception.code, 2)


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.office = temp_dir(self) / "office"
        self.suite = self.office / "tests"

    def test_it_finds_the_modules_discover_would_load_with_their_expected_seconds(self):
        write(self.suite / "__init__.py")
        write(self.suite / "support.py")
        write(self.suite / "helper_test.py")
        write(self.suite / "test_notes.txt")
        write(self.suite / "test_slow.py", 50_000)
        write(self.suite / "test_fast.py", 5_000)
        write(self.suite / "fixtures" / "test_fixture.py")  # not a package, so discover never looks inside
        with mock.patch.object(run_suites, "RECORDED_SECONDS", {"tests/test_fast.py": 40}):
            modules = run_suites.discover(self.office, "tests")
        self.assertEqual(modules, [Module("tests", "test_fast.py", 40.0), Module("tests", "test_slow.py", 2.0)])
        self.assertEqual(modules[0].label, "tests/test_fast.py")

    def test_a_suite_it_could_not_run_module_by_module_exactly_is_refused(self):
        cases = {
            "missing": [],
            "no test modules": ["support.py"],
            "a test package": ["test_a.py", "deeper/__init__.py", "deeper/test_b.py"],
            "a name -p would read as a pattern": ["test_a.py", "test_[ab].py"],
            "a name unittest would refuse": ["test_a.py", "test-b.py"],
        }
        for case, files in cases.items():
            with self.subTest(case=case):
                office = temp_dir(self) / "office"
                for name in files:
                    write(office / "tests" / name)
                with self.assertRaises(RunnerError):
                    run_suites.discover(office, "tests")

    def test_the_real_suites_are_found_from_the_runners_own_folder(self):
        self.assertEqual(run_suites.OFFICE, Path(__file__).resolve().parents[1])
        for suite in run_suites.SUITES:
            with self.subTest(suite=suite):
                names = [module.name for module in run_suites.discover(run_suites.OFFICE, suite)]
                self.assertEqual(names, sorted(path.name for path in (run_suites.OFFICE / suite).glob("test*.py")))
        fleet = run_suites.discover(run_suites.OFFICE, "tests_fleet")
        self.assertIn("test_run_suites.py", [module.name for module in fleet])

    def test_recorded_and_serial_modules_name_real_modules(self):
        known = {module.label for suite in run_suites.SUITES
                 for module in run_suites.discover(run_suites.OFFICE, suite)}
        self.assertLessEqual(set(run_suites.RECORDED_SECONDS), known)
        self.assertLessEqual(set(run_suites.SERIAL), known)


class ChildTests(unittest.TestCase):
    OFFICE = Path("/nonexistent/office")
    MODULE = Module("tests_fleet", "test_caps.py", 1.0)

    def setUp(self) -> None:
        self.started, self.killed = [], []

    def children(self, *steps, code: int = 0, start_error: BaseException = None) -> Children:
        def popen(argv, **kwargs):
            if start_error is not None:
                raise start_error
            child = FakeChild(argv, steps, code, **kwargs)
            self.started.append(child)
            return child
        return Children(self.OFFICE, popen=popen, kill=self.killed.append, timeout=600)

    def test_each_module_runs_the_reference_line_in_the_office_with_an_empty_environment(self):
        self.assertEqual(run_suites.WRAPPER, config.PYTHON_WRAPPER)
        outcome = self.children(unittest_output(4).encode()).run(self.MODULE)
        [child] = self.started
        self.assertEqual(child.args, [*config.PYTHON_WRAPPER, "-m", "unittest", "discover",
                                      "-s", "/nonexistent/office/tests_fleet", "-t", "/nonexistent/office",
                                      "-p", "test_caps.py"])
        self.assertEqual(child.kwargs, {"cwd": "/nonexistent/office", "env": {}, "stdin": subprocess.DEVNULL,
                                        "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                                        "start_new_session": True})
        self.assertEqual((outcome.returncode, outcome.output, outcome.problem), (0, unittest_output(4), None))
        self.assertTrue(run_suites.passed(outcome))
        self.assertEqual(self.killed, [])

    def test_a_temp_root_the_runner_was_given_is_the_one_variable_each_child_gets(self):
        folder = temp_dir(self)
        with mock.patch.dict("os.environ", {"TEST_TMP_ROOT": str(folder)}):
            self.assertEqual(run_suites.temp_root(), str(folder))
        for bad in ("relative/tmp", "/nonexistent/tmp", ""):
            with mock.patch.dict("os.environ", {"TEST_TMP_ROOT": bad}):
                self.assertIsNone(run_suites.temp_root())
        children = Children(self.OFFICE, popen=lambda argv, **kwargs: self.started.append(argv) or FakeChild(
            argv, (unittest_output(1).encode(),), 0, **kwargs), kill=self.killed.append, timeout=600,
            temp_root=str(folder))
        children.run(self.MODULE)
        [argv] = self.started
        self.assertEqual(argv[:6], ["/usr/bin/env", "-i", f"TEST_TMP_ROOT={folder}", f"TMPDIR={folder}",
                                    f"xcrun_db={folder}/xcrun_db", "/usr/bin/python3"])
        self.assertEqual(argv[6:], [*config.PYTHON_WRAPPER[3:], "-m", "unittest", "discover", "-s",
                                    "/nonexistent/office/tests_fleet", "-t", "/nonexistent/office", "-p",
                                    "test_caps.py"])

    def test_a_module_that_runs_too_long_is_killed_and_keeps_what_it_printed(self):
        late = subprocess.TimeoutExpired(["x"], 600, output=b"....")
        outcome = self.children(late, b"....\nTraceback", code=-9).run(self.MODULE)
        [child] = self.started
        self.assertEqual(self.killed, [child])
        self.assertEqual((outcome.returncode, outcome.output), (-9, "....\nTraceback"))
        self.assertEqual(outcome.problem, "timed out after 600s and was killed")
        self.assertFalse(run_suites.passed(outcome))

    def test_a_killed_module_whose_pipe_stays_open_still_ends_with_its_output(self):
        held = subprocess.TimeoutExpired(["x"], 10, output=b"..partial")
        outcome = self.children(subprocess.TimeoutExpired(["x"], 600), held, code=-9).run(self.MODULE)
        [child] = self.started
        child.stdout.close.assert_called_once_with()
        self.assertEqual((outcome.returncode, outcome.output), (-9, "..partial"))
        self.assertFalse(run_suites.passed(outcome))

    def test_a_module_whose_output_cannot_be_read_is_killed_and_failed(self):
        outcome = self.children(OSError(5, "Input/output error")).run(self.MODULE)
        self.assertEqual(self.killed, self.started)
        self.assertEqual(outcome.problem, "its output could not be read (OSError), so it was killed")
        self.assertFalse(run_suites.passed(outcome))

    def test_a_module_that_cannot_start_is_reported_not_skipped(self):
        outcome = self.children(start_error=PermissionError(13, "Permission denied", "/usr/bin/env")).run(self.MODULE)
        self.assertEqual((outcome.returncode, outcome.problem), (None, "could not be started (PermissionError)"))
        self.assertNotIn("/usr/bin/env", outcome.problem)
        self.assertFalse(run_suites.passed(outcome))

    def test_an_interrupt_kills_the_running_children_and_starts_no_more(self):
        children = None

        def interrupted(child):
            children.stop()  # Ctrl-C arrives while this module runs
            return b""
        children = self.children(interrupted, code=-9)
        outcome = children.run(self.MODULE)
        self.assertEqual(self.killed, self.started)
        self.assertEqual(outcome.problem, "stopped, because the run was interrupted")
        self.assertFalse(run_suites.passed(outcome))
        later = children.run(Module("tests", "test_db.py", 1.0))
        self.assertEqual((len(self.started), later.returncode), (1, None))
        self.assertEqual(later.problem, "not started, because the run was interrupted")

    def test_an_interrupt_while_waiting_kills_that_child_before_it_goes_on(self):
        children = self.children(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            children.run(self.MODULE)
        self.assertEqual(self.killed, self.started)
        self.assertEqual(children.live, set())


class SummaryTests(unittest.TestCase):
    def test_counts_come_from_unittests_closing_lines(self):
        cases = (
            (unittest_output(4), {"ran": 4, "ok": True}),
            (unittest_output(1, "OK (skipped=2)"), {"ran": 1, "ok": True, "skipped": 2}),
            (unittest_output(9, "FAILED (failures=1, errors=2, skipped=1)"),
             {"ran": 9, "ok": False, "failures": 1, "errors": 2, "skipped": 1}),
            (unittest_output(3, "FAILED (expected failures=1, unexpected successes=1)"),
             {"ran": 3, "ok": False, "expected failures": 1, "unexpected successes": 1}),
        )
        for output, expected in cases:
            with self.subTest(output=output.splitlines()[-1]):
                found = run_suites.summary(output)
                self.assertEqual({key: value for key, value in found.items() if value or key in ("ran", "ok")},
                                 expected)

    def test_only_the_last_summary_counts(self):
        stray = "a test printed this:\nRan 99 tests in 1.0s\n\nOK\n"
        self.assertEqual(run_suites.summary(stray + unittest_output(2, "FAILED (errors=1)"))["ran"], 2)
        self.assertIsNone(run_suites.summary(unittest_output(2) + "Ran 5 tests in 0.1s\n"))

    def test_output_that_does_not_end_in_a_summary_has_none(self):
        for output in ("", "....", "Traceback (most recent call last):\n", f"....\n{RULE}\nRan 4 tests in 0.012s\n",
                       unittest_output(4, "FAILED (oops=1)"), unittest_output(4, "PASSED")):
            with self.subTest(output=output):
                self.assertIsNone(run_suites.summary(output))

    def test_a_module_passes_only_with_exit_code_zero_and_an_ok_summary(self):
        module = Module("tests", "test_a.py", 1.0)
        cases = (
            (Outcome(module, 0, unittest_output(4), 1.0), True),
            (Outcome(module, 1, unittest_output(4), 1.0), False),
            (Outcome(module, 0, unittest_output(4, "FAILED (failures=1)"), 1.0), False),
            (Outcome(module, 0, "", 1.0), False),
            (Outcome(module, -9, "", 1.0), False),
            (Outcome(module, 0, unittest_output(4), 1.0, "stopped, because the run was interrupted"), False),
        )
        for outcome, expected in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(run_suites.passed(outcome), expected)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.office = temp_dir(self) / "office"
        for name in ("test_a.py", "test_b.py"):
            write(self.office / "tests" / name)
        for name in ("test_c.py", "test_d.py", "test_e.py"):
            write(self.office / "tests_fleet" / name)
        self.results = {}
        self.ran = []

    def fake_run(self, module: Module) -> Outcome:
        self.ran.append(module.label)
        code, output, problem = self.results.get(module.label, (0, unittest_output(3), None))
        return Outcome(module, code, output, 0.5, problem)

    def main(self, *argv) -> tuple:
        out = io.StringIO()
        code = run_suites.main(list(argv), out=out, office=self.office, run=self.fake_run)
        return code, out.getvalue()

    def test_a_clean_run_prints_one_line_per_suite_and_exits_zero(self):
        self.results["tests/test_b.py"] = (0, unittest_output(2, "OK (skipped=1)"), None)
        code, out = self.main("--jobs", "2")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "Running 5 test modules from tests and tests_fleet, 2 at a time.")
        self.assertEqual(lines[1:3], ["tests: Ran 5 tests in 2 modules, OK (skipped=1)",
                                      "tests_fleet: Ran 9 tests in 3 modules, OK"])
        self.assertRegex(lines[3], r"^OK: all 5 modules, in \d+\.\ds \(slowest: tests/test_a\.py, 0\.5s\)$")
        self.assertEqual(len(lines), 4)
        self.assertNotIn("....", out)
        self.assertEqual(sorted(self.ran), ["tests/test_a.py", "tests/test_b.py", "tests_fleet/test_c.py",
                                            "tests_fleet/test_d.py", "tests_fleet/test_e.py"])

    def test_a_failing_module_prints_its_whole_output_and_exits_one(self):
        failing = "F..\n" + "=" * 70 + "\nFAIL: test_x (tests_fleet.test_d.X)\nAssertionError\n" + \
                  unittest_output(3, "FAILED (failures=1)", dots="")
        self.results["tests_fleet/test_d.py"] = (1, failing, None)
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn(f"===== tests_fleet/test_d.py: exit code 1 =====\n{failing}", out)
        self.assertIn("tests: Ran 6 tests in 2 modules, OK\n", out)
        self.assertIn("tests_fleet: Ran 9 tests in 3 modules, FAILED (failures=1); failed: test_d.py\n", out)
        self.assertTrue(out.splitlines()[-1].startswith("FAILED: 1 of 5 modules, in "))

    def test_a_module_that_ends_without_a_summary_fails_the_run(self):
        self.results["tests/test_a.py"] = (0, "", None)
        self.results["tests_fleet/test_c.py"] = (-9, "..", None)
        self.results["tests_fleet/test_e.py"] = (None, "", "could not be started (PermissionError)")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("===== tests/test_a.py: exit code 0, with no unittest summary in its output =====", out)
        self.assertIn("===== tests_fleet/test_c.py: killed by signal 9 =====\n..\n", out)
        self.assertIn("===== tests_fleet/test_e.py: could not be started (PermissionError) =====", out)
        self.assertIn("tests: Ran 3 tests in 2 modules, FAILED (modules with no summary=1); failed: test_a.py\n", out)
        self.assertIn("tests_fleet: Ran 3 tests in 3 modules, FAILED (modules with no summary=2); "
                      "failed: test_c.py, test_e.py\n", out)

    def test_only_the_named_suite_runs(self):
        code, out = self.main("tests_fleet")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.ran), ["tests_fleet/test_c.py", "tests_fleet/test_d.py", "tests_fleet/test_e.py"])
        self.assertNotIn("tests:", out)

    def test_a_suite_it_cannot_run_exactly_stops_the_run_before_any_module(self):
        write(self.office / "tests" / "deeper" / "__init__.py")
        code, out = self.main()
        self.assertEqual((code, self.ran), (1, []))
        self.assertEqual(out, "run_suites.py: tests holds a test package, 'deeper', and this runner only runs "
                              "top-level modules\n")

    def test_an_interrupt_keeps_the_failures_already_seen_and_reports_nothing_as_passed(self):
        failing = unittest_output(3, "FAILED (errors=1)")
        self.results["tests_fleet/test_c.py"] = (1, failing, None)

        def run(module):
            if module.name == "test_e.py":
                raise KeyboardInterrupt
            return self.fake_run(module)
        out = io.StringIO()
        code = run_suites.main(["--jobs", "1"], out=out, office=self.office, run=run)
        self.assertEqual(code, 130)
        self.assertIn(f"===== tests_fleet/test_c.py: exit code 1 =====\n{failing}", out.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-1],
                         "Interrupted after 4 of 5 modules ended, 1 of them failed. Every running module was "
                         "killed, and nothing is reported as passed.")
        self.assertNotIn("OK", out.getvalue())


class ScheduleTests(unittest.TestCase):
    MODULES = [Module("tests", "test_a.py", 2.0), Module("tests_fleet", "test_b.py", 85.0),
               Module("tests", "test_c.py", 9.0), Module("tests_fleet", "test_d.py", 30.0)]

    def test_the_slowest_modules_start_first(self):
        order = []

        def run(module):
            order.append(module.name)
            return Outcome(module, 0, unittest_output(1), 0.0)
        outcomes = run_suites.run_all(run, self.MODULES, 1)
        self.assertEqual(order, ["test_b.py", "test_d.py", "test_c.py", "test_a.py"])
        self.assertEqual([outcome.module.name for outcome in outcomes], order)

    def test_serial_modules_run_alone_after_the_parallel_batch(self):
        lock, active, seen = threading.Lock(), [0], []

        def run(module):
            with lock:
                active[0] += 1
                seen.append((module.name, active[0]))
            try:
                return Outcome(module, 0, unittest_output(1), 0.0)
            finally:
                with lock:
                    active[0] -= 1
        with mock.patch.object(run_suites, "SERIAL", frozenset({"tests_fleet/test_b.py", "tests/test_a.py"})):
            outcomes = run_suites.run_all(run, self.MODULES, 4)
        self.assertEqual(seen[-2:], [("test_b.py", 1), ("test_a.py", 1)])
        self.assertEqual(sorted(name for name, _ in seen[:2]), ["test_c.py", "test_d.py"])
        self.assertEqual(len(outcomes), 4)
