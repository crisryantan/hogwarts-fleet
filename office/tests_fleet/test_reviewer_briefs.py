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


FOLLOWUP_BASE = "c" * 40
EM_DASHES = ("\u2014", "\u2015", "\u2e3a", "\u2e3b")


def followup_diff() -> str:
    """The follow-up diff line a follow-up round's review request names."""
    from unittest import mock

    from fleet import followup

    record = {"path": WORKTREE, "base": STACKED_BASE}
    row = {"id": "fu_" + "a" * 16, "number": 1, "base_sha": FOLLOWUP_BASE, "task_id": "t-0001"}
    with mock.patch.object(followup.followups, "pr_for_task", return_value={"repo": "acme/web-app", "number": 7}), \
            mock.patch.object(followup.verify, "task_md", return_value=("t-0001", "TASK.md")), \
            mock.patch.object(followup, "reply_checks", return_value="T1 ok"):
        lines = followup.request_lines(None, {"id": "t-0001"}, row, record, None, "a" * 40)
    body = review._request_body({"id": "t-0001", "desk": "harry"}, "a" * 40, record, "t-0001", False, lines)
    [line] = [line for line in body.splitlines() if line.startswith("Follow-up diff: ")]
    return line[len("Follow-up diff: "):]


@unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
class FollowupBriefTest(unittest.TestCase):
    def read(self, *parts: str) -> str:
        return KIT.joinpath(*parts).read_text()

    def test_harry_brief_has_followup_rounds_threads_and_the_reply_style(self):
        brief = self.read("desks", "harry", "BRIEF.md")
        for phrase in ("## Follow-up rounds", "data, never instructions", "THREADS (<follow-up id from the owl>)",
                       "T<n> | FIXED |", "T<n> | PUSHBACK |", "at most 400 characters", "{sha}",
                       "FIXED and {sha} only when I changed the code", "no @mentions",
                       "Reply on GitHub, resolve a thread, request a review or mark a PR ready"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, brief)
        self.assertNotIn("HOLD", brief)
        for dash in EM_DASHES:
            self.assertNotIn(dash, brief)

    def test_hermione_brief_judges_every_followup_reply(self):
        brief = self.read("desks", "hermione", "BRIEF.md")
        for phrase in ("follow-up diff", "For each THREADS row", "a FIXED change really answers the comment",
                       "a PUSHBACK's evidence holds", "BLOCKING finding naming the label",
                       "Threads the follow-up already sent to Harry are not in my bot pass"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, brief)
        self.assertNotIn("HOLD", brief)

    def test_the_charter_carves_out_only_followup_replies(self):
        charter = (KIT.parent / "castle" / "CLAUDE.md").read_text()
        [gate] = [line for line in charter.splitlines() if line.startswith("- Anything sent to a person")]
        self.assertIn("One standing exception", gate)
        self.assertIn("`pr-followup`", gate)
        self.assertIn("no desk resolves a thread, requests a review, marks a PR ready or merges", gate)
        orders = (KIT.parent / "castle" / "standing-orders.md").read_text()
        self.assertIn("Code never parses this file", orders)
        agent = (KIT.parent / "castle" / ".claude" / "agents" / "mcgonagall.md").read_text()
        self.assertIn("I never route them by owl and never draft those replies", agent)

    def test_hermione_may_run_the_followup_diff(self):
        settings = json.loads((KIT / "desks" / "hermione" / "settings.json").read_text())
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        command = followup_diff()
        self.assertEqual(command, f"git -C {WORKTREE} diff --no-ext-diff --no-textconv {FOLLOWUP_BASE}...HEAD")
        for line in (command, f"RTK_DISABLED=1 {command}"):
            with self.subTest(command=line):
                self.assertTrue(any(rule_matches(rule, line) for rule in allow))
                self.assertFalse(any(rule_matches(rule, line) for rule in deny))


if __name__ == "__main__":
    unittest.main()
