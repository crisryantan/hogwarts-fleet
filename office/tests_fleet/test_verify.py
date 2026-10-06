"""How verify reads each acceptance check: one backtick command runs, plain words are an observation, and a check
that mixes the two is flagged as malformed instead of being skipped as an observation.

Runs on real git repos in temp folders, with verify's sandbox replaced by plain bash as in test_review_loop.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest import mock

from fleet import common, config, safefs, verify
from tests_fleet.test_review_loop import LoopCase

TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # shaped like a GitHub token, built so no scanner trips

CHECKS_MD = """# {task_id} Mixed checks

## Intent
Check the acceptance checks.

## Acceptance criteria
AC-1 the readme is there | check: `test -f README.md`
AC-2 the readme check passes | check: `test -f README.md` exits 0 and prints nothing
AC-3 the diff stays small | check: one file changes
AC-4 both files are there | check: `test -f README.md` then `touch both-ran.txt`
AC-5 the marker is written | check: `touch mixed-ran.txt` and the marker exists
AC-6 the quote is closed | check: `touch unclosed-ran.txt

## Out of scope
Anything else.
"""


class VerifyChecksCase(LoopCase):
    def checked(self, text: str = CHECKS_MD) -> tuple:
        """A built task whose TASK.md holds text, verified at a commit: (parent, result, evidence, worktree)."""
        parent, task, _, created, _ = self.build()
        self.write_file(self.castle / "tasks" / parent / "TASK.md", text.format(task_id=parent))
        worktree = Path(created["worktree"])
        self.write_file(worktree / "widget.txt", "widget\n")
        self.git("add", "widget.txt", cwd=worktree)
        self.git("commit", "-q", "-m", "widget", cwd=worktree)
        result = verify.verify(self.conn, task["id"])
        return parent, result, (self.castle / "tasks" / parent / "evidence.md").read_text(), worktree


class MixedCheckTests(VerifyChecksCase):
    def test_mixed_check_is_flagged_malformed_with_a_plain_reason(self):
        _, result, text, _ = self.checked()
        self.assertEqual(result["malformed"], ["AC-2", "AC-4", "AC-5", "AC-6"])
        self.assertIn("AC-2 the readme check passes\ncheck: `test -f README.md` exits 0 and prints nothing\n"
                      "not run: malformed, it holds a backtick command plus other text. "
                      f"{verify.MALFORMED_HINT}\n", text)
        self.assertIn("AC-4 both files are there\ncheck: `test -f README.md` then `touch both-ran.txt`\n"
                      "not run: malformed, it holds 2 backtick commands, and a check runs only one.", text)
        self.assertIn("AC-6 the quote is closed\ncheck: `touch unclosed-ran.txt\n"
                      "not run: malformed, its backticks hold no command verify can run", text)
        self.assertNotIn("prints nothing\nnot run: an observation", text)

    def test_mixed_check_summary_counts_malformed_checks_on_their_own(self):
        _, result, text, _ = self.checked()
        self.assertIn("\nSUMMARY 1 of 1 commands exited 0, 4 malformed checks not run, 1 observations for the reviewer\n",
                      text)
        self.assertEqual((result["checks"], result["failed"]), (6, []))

    def test_mixed_check_command_never_runs(self):
        _, _, _, worktree = self.checked()
        for name in ("mixed-ran.txt", "both-ran.txt", "unclosed-ran.txt"):
            self.assertFalse((worktree / name).exists(), name)

    def test_mixed_check_parse_keeps_the_command_rule(self):
        checks = verify.parse_checks("AC-1 a | check: `make test` passes\nAC-2 b | check: `make test`\n"
                                     "AC-3 c | check: ``\nAC-4 d | check: run `make` twice\n")
        self.assertEqual([(check["id"], check["command"], check["malformed"] is not None) for check in checks],
                         [("AC-1", None, True), ("AC-2", "make test", False), ("AC-3", None, True),
                          ("AC-4", None, True)])
        self.assertEqual(checks[0]["malformed"], "it holds a backtick command plus other text")


class PureAndProseCheckTests(VerifyChecksCase):
    PLAIN_MD = CHECKS_MD.split("AC-2 ")[0] + ("AC-2 the widget fails on purpose | check: `test -f missing.txt`\n"
                                              "AC-3 the diff stays small | check: one file changes\n\n"
                                              "## Out of scope\nAnything else.\n")

    def test_pure_and_prose_checks_run_and_are_observed_as_before(self):
        _, result, text, _ = self.checked(self.PLAIN_MD)
        self.assertEqual((result["checks"], result["failed"], result["malformed"]), (3, ["AC-2"], []))
        self.assertIn("AC-1 the readme is there\ncheck: `test -f README.md`\nexit: 0", text)
        self.assertIn("AC-2 the widget fails on purpose\ncheck: `test -f missing.txt`\nexit: 1", text)
        self.assertIn("AC-3 the diff stays small\ncheck: one file changes\n"
                      "not run: an observation for the reviewer to judge\n", text)
        self.assertIn("\nSUMMARY 1 of 2 commands exited 0, 0 malformed checks not run, 1 observations for the reviewer\n",
                      text)
        self.assertNotIn("malformed,", text)

    def test_pure_and_prose_checks_parse_as_command_and_observation(self):
        checks = verify.parse_checks("AC-1 a | check: `x`\nnoise\nAC-12 b | check: look at it\nAC-x c | check: `y`\n")
        self.assertEqual([(check["id"], check["command"], check["malformed"]) for check in checks],
                         [("AC-1", "x", None), ("AC-12", None, None)])


AFTER_MD = """# {task_id} After-merge checks

## Intent
Check the after-merge checks.

## Acceptance criteria
AC-1 the readme is there | check: `test -f README.md`
AC-2 the diff stays small | check: one file changes
AC-3 main still builds | after merge: `touch after-ran.txt`
AC-4 the dashboard looks right | after merge: the widget count goes up
AC-5 it fails on purpose | after merge: `exit 1`

## Out of scope
Anything else.
"""


class AfterMergeCheckTests(VerifyChecksCase):
    def test_after_merge_check_is_listed_and_never_run_before_merge(self):
        _, result, text, worktree = self.checked(AFTER_MD)
        self.assertFalse((worktree / "after-ran.txt").exists())
        self.assertEqual((result["failed"], result["malformed"], result["after_merge"]), ([], [], ["AC-3", "AC-4", "AC-5"]))
        self.assertIn("AC-3 main still builds\nafter merge: `touch after-ran.txt`\n"
                      "not run: an after-merge check, run at the merge commit\n", text)
        self.assertIn("AC-4 the dashboard looks right\nafter merge: the widget count goes up\n"
                      "not run: an after-merge check, judged after merge\n", text)
        self.assertIn("\nAFTER MERGE 2 commands and 1 written checks, judged after merge and never run before it\n", text)

    def test_after_merge_command_and_words_follow_the_check_rule(self):
        checks = verify.parse_checks("AC-1 a | after merge: `make test`\nAC-2 b | after merge: the build is green\n"
                                     "AC-3 c | after merge: ``\n")
        self.assertEqual([(check["id"], check["when"], check["command"], check["malformed"] is not None)
                          for check in checks],
                         [("AC-1", "after", "make test", False), ("AC-2", "after", None, False),
                          ("AC-3", "after", None, True)])
        self.assertEqual([check["id"] for check in verify.after_merge(checks)], ["AC-1", "AC-2"])

    def test_after_merge_summary_line_is_unchanged_without_after_merge_checks(self):
        _, _, text, _ = self.checked(PureAndProseCheckTests.PLAIN_MD)
        self.assertIn("\nSUMMARY 1 of 2 commands exited 0, 0 malformed checks not run, 1 observations for the reviewer\n\n",
                      text)
        self.assertNotIn("AFTER MERGE", text)

    def test_after_merge_mixed_check_is_malformed_and_never_runs(self):
        text = AFTER_MD.replace("`touch after-ran.txt`", "`touch after-ran.txt` and it builds")
        _, result, evidence, worktree = self.checked(text)
        self.assertEqual(result["malformed"], ["AC-3"])
        self.assertNotIn("AC-3", result["after_merge"])
        self.assertIn("AC-3 main still builds\nafter merge: `touch after-ran.txt` and it builds\n"
                      "not run: malformed, it holds a backtick command plus other text.", evidence)
        self.assertFalse((worktree / "after-ran.txt").exists())

    def test_unknown_check_label_is_malformed_and_never_runs(self):
        text = AFTER_MD.replace("| after merge: `touch after-ran.txt`", "| after-merge: `touch after-ran.txt`")
        for label in ("after-merge:", "After merge:", "chek:", "check :"):
            with self.subTest(label=label):
                checks = verify.parse_checks(f"AC-7 x | {label} `touch label-ran.txt`\n")
                self.assertEqual([(check["id"], check["command"], check["malformed"]) for check in checks],
                                 [("AC-7", None, verify.UNKNOWN_LABEL)])
        _, result, evidence, worktree = self.checked(text)
        self.assertEqual(result["malformed"], ["AC-3"])
        self.assertIn("AC-3 main still builds\nafter-merge: `touch after-ran.txt`\nnot run: malformed, a criterion's check"
                      " is labelled check: or after merge:, and nothing else.", evidence)
        self.assertFalse((worktree / "after-ran.txt").exists())
        self.assertEqual(verify.parse_checks("AC-1 prose with no pipe at all\n"), [])

    def test_duplicate_criterion_id_is_malformed(self):
        checks = verify.parse_checks("AC-1 a | check: `true`\nAC-2 b | check: `true`\nAC-1 c | after merge: `true`\n")
        self.assertEqual([(check["id"], check["command"], check["malformed"]) for check in checks],
                         [("AC-1", None, "AC-1 is used more than once"), ("AC-2", "true", None),
                          ("AC-1", None, "AC-1 is used more than once")])
        _, result, evidence, _ = self.checked(AFTER_MD.replace("AC-5 it fails", "AC-1 it fails"))
        self.assertEqual(result["malformed"], ["AC-1", "AC-1"])
        self.assertEqual(evidence.count("not run: malformed, AC-1 is used more than once."), 2)

    def test_after_merge_two_labels_on_one_line_are_malformed(self):
        for line in ("AC-1 x | check: works | after merge: metric ok",
                     "AC-1 x | check: works | After-Merge: metric ok",
                     "AC-1 x | after merge: `make` | check: done",
                     "AC-1 x | after-merge: foo | check: `bar`"):
            with self.subTest(line=line):
                [check] = verify.parse_checks(line + "\n")
                self.assertEqual((check["command"], check["malformed"] is not None), (None, True))

    def test_malformed_when_anything_label_shaped_follows_a_valid_label(self):
        # Any pipe, a few words and a colon outside the backticks is a second label, known or not, whichever label came
        # first, and an invisible or fullwidth character never hides one.
        for line in ("AC-1 x | check: looks right | later: confirm",
                     "AC-1 x | after merge: looks right | note : confirm",
                     "AC-1 x | check: `make test` | then: done",
                     "AC-1 x | after merge: `make` |later:`rm -rf ~`",
                     "AC-1 x | check: fine | 2nd pass: again",
                     "AC-1 x | check: fine |​later: confirm",
                     "AC-1 x | after merge: fine ｜ later: confirm",
                     "AC-1 x | check: fine | après merge: confirm"):
            with self.subTest(line=line):
                [check] = verify.parse_checks(line + "\n")
                self.assertEqual((check["command"], check["malformed"]), (None, verify.TWO_LABELS))
        # A pipe with no label after it, and one inside the backticks, stay part of the check.
        for line, command in (("AC-1 x | check: one | two", None), ("AC-1 x | check: `a | b: c`", "a | b: c")):
            with self.subTest(line=line):
                [check] = verify.parse_checks(line + "\n")
                self.assertEqual((check["command"], check["malformed"]), (command, None))

    def test_after_merge_pipe_inside_a_backtick_command_is_still_a_command(self):
        [check] = verify.parse_checks("AC-1 x | after merge: `make test | tee out | grep check: | wc -l`\n")
        self.assertEqual((check["command"], check["malformed"], check["when"]),
                         ("make test | tee out | grep check: | wc -l", None, "after"))

    def test_after_merge_summary_counts_only_before_merge_criteria(self):
        _, _, text, _ = self.checked(AFTER_MD)
        self.assertIn("\nSUMMARY 1 of 1 commands exited 0, 0 malformed checks not run, 1 observations for the reviewer\n",
                      text)

    def test_after_merge_command_is_never_in_failed_or_looked_up_in_results(self):
        _, result, _, _ = self.checked(AFTER_MD)
        self.assertNotIn("AC-5", result["failed"])
        checks = verify.parse_checks(AFTER_MD)
        record = {"path": "/x"}
        text = verify.render("tk_" + "0" * 16, "a" * 40, record, "b" * 64, checks,
                             {"AC-1": {"exit_code": 0, "seconds": 0.1, "output_bytes": 0, "lines": []}}, 0)
        self.assertIn("SUMMARY 1 of 1 commands exited 0", text)


OUTPUT_MD = """# {{task_id}} Check output

## Intent
Check what a check's output may carry into the evidence.

## Acceptance criteria
AC-1 the readme is there | check: `{command}`

## Out of scope
Anything else.
"""


def group_gone(pgid: int, seconds: float = 5.0) -> bool:
    """Whether no process is left in the process group within seconds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class CheckOutputTests(VerifyChecksCase):
    def test_check_output_is_read_through_its_own_descriptor_never_a_link_in_its_place(self):
        # The check swaps its output file, in the scratch folder it can write, for a link to a file outside its reach.
        outside = self.write_file(self.tmp / "outside.txt", "words-from-outside-the-check-reach\n")
        command = (f'echo its own words; for f in "$HOME"/../out-*.log; do rm -f "$f"; ln -s {outside} "$f"; done')
        _, result, text, _ = self.checked(OUTPUT_MD.format(command=command))
        self.assertEqual(result["failed"], [])
        self.assertIn("output, last 40 lines:\n    its own words\n", text)
        self.assertNotIn("words-from-outside", text)

    def test_check_output_before_the_merge_is_normalized_and_scrubbed_before_its_cut(self):
        # A token, the same token split by a zero-width space, and a key whose header falls before the last 40 lines.
        command = (f"echo token {TOKEN}; printf '%s\\342\\200\\213%s\\n' {TOKEN[:2]} {TOKEN[2:]};"
                   " echo -----BEGIN RSA PRIVATE KEY-----; for i in $(seq 10 69); do echo MIIEpAIBAAKCAQEAx${{i}}Yz9Wq; done")
        _, result, text, _ = self.checked(OUTPUT_MD.format(command=command))
        output = text.split("output, last 40 lines:\n", 1)[1]
        self.assertNotIn(TOKEN, common.normalized(output))
        self.assertNotIn("\u200b", output)
        self.assertEqual(output.count("[token]"), 2)
        self.assertNotIn("MIIEpAIBAAKCAQEAx", output)
        self.assertIn("[private_key]", output)
        self.assertLessEqual(len(output.splitlines()), config.EVIDENCE_EXCERPT_LINES)
        office = (self.office / "reviews" / result["task_id"] / f"evidence-{result['sha']}.md").read_text()
        self.assertEqual(office, text)

    def test_a_signal_as_a_check_starts_ends_its_group_before_its_caller_lets_go_of_a_lock(self):
        work, scratch = self.tmp / "work", self.tmp / "scratch"
        for folder in (work, scratch / "home", scratch / "tmp"):
            folder.mkdir(parents=True)
        real, children = subprocess.Popen, []

        def start(*args, **kwargs):
            children.append(real(*args, **kwargs))
            os.kill(os.getpid(), signal.SIGTERM)  # lands once the process has started, before its handle is kept
            return children[-1]

        def end_left() -> None:
            for child in children:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

        self.addCleanup(end_left)
        with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks, \
                safefs.held_lock(locks, "review-check.lock", blocking=False) as lock_fd:
            with common.ended_by_signals(), mock.patch.object(subprocess, "Popen", side_effect=start), \
                    self.assertRaises(SystemExit):
                verify.run_check({"path": str(work), "links": []}, str(scratch), "sleep 60 & sleep 60",
                                 sandboxed=False, keep_fds=(lock_fd,))
            # Still inside the lock: the check's process was killed and reaped before the signal left run_check.
            [child] = children
            self.assertIsNotNone(child.returncode, "the check's process outlived the signal that ended its caller")
            self.assertTrue(group_gone(child.pid))
