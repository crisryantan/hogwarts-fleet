"""The reviewer briefs, Hermione's allow rules and the review request name the same diff.

A stacked branch has its own base, so a brief that names a fixed base would read a diff the
review request did not ask for, and Hermione's allow rules would refuse the one it did.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from fleet import review
from tests_fleet.support import IN_KIT, ONLY_IN_KIT

KIT = Path(__file__).resolve().parents[1]
REVIEWERS = ("moody", "hermione")
WORKTREE = "/Users/crisryantan/hogwarts/worktrees/t-0001"
STACKED_BASE = "origin/feat/desk-feeds"


def request_diff(base: str) -> str:
    task = {"id": "t-0001", "desk": "harry"}
    record = {"path": WORKTREE, "base": base}
    body = review._request_body(task, "a" * 40, record, "t-0001", handoff=False)
    lines = [line for line in body.splitlines() if line.startswith("Diff: ")]
    if len(lines) != 1:
        raise AssertionError("the review request has no single Diff line")
    return lines[0][len("Diff: "):]


def rule_matches(rule: str, command: str) -> bool:
    """A Claude Code Bash rule against one command, where * matches any run of characters."""
    inner = re.fullmatch(r"Bash\((.*)\)", rule)
    if inner is None:
        return False
    pattern = ".*".join(re.escape(part) for part in inner.group(1).split("*"))
    return re.fullmatch(pattern, command, re.DOTALL) is not None


# The kit's briefs and settings; an installed office keeps its own, which may be worded privately.
@unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
class ReviewerBriefTest(unittest.TestCase):
    def test_the_briefs_read_the_diff_the_request_names(self):
        command = request_diff(STACKED_BASE)
        self.assertIn(f"{STACKED_BASE}...HEAD", command)
        template = command.replace(WORKTREE, "<worktree>").replace(STACKED_BASE, "<base>")
        for desk in REVIEWERS:
            with self.subTest(desk=desk):
                brief = (KIT / "desks" / desk / "BRIEF.md").read_text()
                self.assertIn(template, brief)
                self.assertIn("review request", brief)
                self.assertNotIn("origin/main", brief)
                self.assertIn("If I can't read the full diff, my verdict can't be PASS.", brief)

    def test_hermione_may_run_the_request_diff_for_any_base(self):
        settings = json.loads((KIT / "desks" / "hermione" / "settings.json").read_text())
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        self.assertFalse([rule for rule in allow if "origin/main" in rule])
        for base in ("origin/main", STACKED_BASE):
            diff = request_diff(base)
            commands = (diff, diff.replace("--no-textconv ", "--no-textconv --stat "),
                        f"git -C {WORKTREE} log --no-decorate --oneline {base}..HEAD",
                        f"git -C {WORKTREE} rev-parse HEAD")
            for command in commands + tuple(f"RTK_DISABLED=1 {line}" for line in commands):
                with self.subTest(command=command):
                    self.assertTrue(any(rule_matches(rule, command) for rule in allow))
                    self.assertFalse(any(rule_matches(rule, command) for rule in deny))

    def test_hermione_denies_the_diff_flags_that_write_or_leave_the_repo(self):
        settings = json.loads((KIT / "desks" / "hermione" / "settings.json").read_text())
        deny = settings["permissions"]["deny"]
        for flag in ("--output=/tmp/x", "--ext-diff", "--textconv", "--no-index"):
            command = f"git -C {WORKTREE} diff --no-ext-diff --no-textconv {flag} {STACKED_BASE}...HEAD"
            for line in (command, f"RTK_DISABLED=1 {command}"):
                with self.subTest(command=line):
                    self.assertTrue(any(rule_matches(rule, line) for rule in deny))


if __name__ == "__main__":
    unittest.main()
