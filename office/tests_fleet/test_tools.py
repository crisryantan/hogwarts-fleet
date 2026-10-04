"""The fleet command line: argument rules, JSON output and the intent file checks."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
from unittest import mock

from fleet import config, tools
from fleet.safefs import FleetError
from tests_fleet.support import FleetCase


class ToolsTests(FleetCase):
    def main(self, *argv) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tools.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_review_own_needs_a_repo_and_exactly_one_of_title_or_task(self):
        for argv in (("review", "own"), ("review", "own", "--repo-dir", "/x"),
                     ("review", "own", "--repo-dir", "/x", "--title", "t", "--task", "tk_" + "1" * 16)):
            with self.subTest(argv=argv):
                code, out = self.main(*argv)
                self.assertEqual((code, out["ok"]), (1, False))

    def test_a_build_review_takes_only_its_task_id(self):
        code, out = self.main("review", "tk_" + "1" * 16, "--title", "x")
        self.assertEqual((code, out["ok"]), (1, False))

    def test_an_unknown_task_is_a_clean_json_refusal(self):
        for argv in (("verify", "tk_" + "2" * 16), ("push", "tk_" + "2" * 16, "--yes"), ("build", "tk_" + "2" * 16)):
            with self.subTest(argv=argv):
                code, out = self.main(*argv)
                self.assertEqual((code, out["ok"]), (1, False))
                self.assertIn("error", out)

    def test_the_intent_file_must_be_a_plain_file_in_home(self):
        home_dir = self.tmp / "home"
        home_dir.mkdir()
        good = self.write_file(home_dir / "intent.md", "Fix the widget.\n")
        os.symlink(good, home_dir / "link.md")
        big = self.write_file(home_dir / "big.md", "x" * (tools.INTENT_MAX_BYTES + 1))
        outside = self.write_file(self.tmp / "outside.md", "nope\n")
        with mock.patch.object(config, "USER_HOME_DIR", str(home_dir)):
            self.assertEqual(tools.read_intent(str(good)), "Fix the widget.\n")
            for path in (home_dir / "link.md", big, outside, home_dir / "missing.md", "relative.md"):
                with self.subTest(path=str(path)), self.assertRaises((FleetError, OSError)):
                    tools.read_intent(str(path))

    def test_sigterm_and_sighup_end_the_command_through_its_finally_blocks(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                before, cleaned = signal.getsignal(number), []
                with self.assertRaises(SystemExit) as caught:
                    with tools.ended_by_signals():
                        try:
                            os.kill(os.getpid(), number)
                            self.fail("the signal did not end the command")
                        finally:
                            cleaned.append(number)
                self.assertEqual((caught.exception.code, cleaned), (128 + number, [number]))
                self.assertIs(signal.getsignal(number), before)
