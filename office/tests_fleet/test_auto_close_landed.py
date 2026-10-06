"""Auto-close: proving a passed task landed, the unknown or stalled reads that tell you once, and the sibling checks.

Split from test_auto_close.py so the parallel runner can run it beside the rest; it shares CloseCase.
"""
from __future__ import annotations

import contextlib
import json
import os
from unittest import mock

from hogwarts import ids, pensieve

from fleet import closer, common, config, gitops, patrol, review, run_desk, verify
from fleet.safefs import FleetError
from tests_fleet.support import every_slot
from tests_fleet.test_auto_close import COMMAND_AC, SPLIT_TOKEN, TOKEN, TWO_COMMANDS, WIDE_TOKEN, WRITTEN_AC, \
    check_run, CloseCase, Counting, tearDownModule  # noqa: F401


class LandedTests(CloseCase):
    def test_landed_by_a_merged_pr_at_the_pass_sha_squash_included(self):
        for squash in (False, True):
            with self.subTest(squash=squash):
                ctx = self.passed_build(branch=f"fix/widget-{int(squash)}")
                merge = self.land_pr(ctx, squash=squash)
                result = self.close(ctx)
                self.assertEqual((result["outcome"], result["merge_sha"]), ("closed", merge))
                self.assertEqual(self.record(ctx["task"])["landed"], {"how": "pr", "pr": 7})
                closure = pensieve.task_closure(self.conn, ctx["task"])
                self.assertEqual((closure["landed"], closure["pr_number"], closure["merge_sha"], closure["pass_sha"]),
                                 ("pr", 7, merge, ctx["sha"]))
        [(name, variables)] = [call for call in self.github.calls if call[0] == "landed"][:1]
        self.assertEqual(variables, {"owner": "acme", "name": "web-app", "head": "fix/widget-0"})

    def test_landed_by_ancestry_after_a_fetch_when_no_pr_merged(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        self.assertNotEqual(self.git("rev-parse", "origin/main"), ctx["sha"])  # only the fetch can see it
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["merge_sha"]), ("closed", ctx["sha"]))
        self.assertEqual(self.git("rev-parse", "origin/main"), ctx["sha"])
        self.assertEqual(self.record(ctx["task"])["landed"], {"how": "ancestry", "pr": None})
        # Merged into the base through a merge commit nobody named: the commit that brought it in is the merge commit.
        other = self.passed_build(branch="fix/other")
        merge = self.merge_commit(other["sha"])
        self.push_main(merge)
        self.assertEqual(self.close(other)["merge_sha"], merge)

    def test_landed_never_by_a_fork_or_a_pr_into_another_base(self):
        ctx = self.passed_build()
        merge = self.merge_commit(ctx["sha"])
        self.push_main(merge)
        self.github.prs = [self.pr(ctx, merge=merge, cross=True, head_repo="someone/web-app"),
                           self.pr(ctx, number=8, merge=merge, head_repo="someone/web-app")]
        result = self.close(ctx)
        # The forks are dropped, so it landed by ancestry, never by their word.
        self.assertEqual((result["outcome"], self.record(ctx["task"])["landed"]["how"]), ("closed", "ancestry"))
        other = self.passed_build(branch="fix/stacked")
        merge = self.merge_commit(other["sha"], parent=self.git("rev-parse", "origin/main"))
        self.push_main(merge, "release")
        self.github.prs = [self.pr(other, merge=merge, base="release")]
        result = self.close(other)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "other-base"))
        self.assertEqual(self.status(other["task"]), "awaiting_close")

    def test_landed_waits_quietly_while_the_pr_is_open(self):
        ctx = self.passed_build()
        self.github.prs = [self.pr(ctx, state="OPEN")]
        with mock.patch.object(gitops, "fetch_branch", side_effect=AssertionError("fetched")):
            for offset in (0, config.AUTO_CLOSE_STALL_SECONDS * 3):
                result = self.close(ctx, now=self.t0 + offset)
                self.assertEqual((result["outcome"], result["on"]), ("waiting", "pr-open"))
        self.assertEqual(self.kinds(), [])

    def test_landed_unknown_when_the_pr_list_cannot_be_read_whole_and_never_falls_to_ancestry(self):
        ctx = self.passed_build()
        self.land_ff(ctx)
        broken = [
            {"repository": {"pullRequests": {"totalCount": 21, "nodes": []}}},
            {"repository": {"pullRequests": {"totalCount": 2, "nodes": [self.pr(ctx, merge=ctx["sha"])]}}},
            {"repository": {"pullRequests": {"totalCount": 1, "nodes": [{**self.pr(ctx, merge=ctx["sha"]),
                                                                          "headRefOid": "nope"}]}}},
            {"repository": {"pullRequests": {"totalCount": 1, "nodes": [{**self.pr(ctx, merge=ctx["sha"]),
                                                                          "mergedAt": "yesterday"}]}}},
            {"repository": {"pullRequests": {"totalCount": 1, "nodes": [{**self.pr(ctx, merge=ctx["sha"]),
                                                                          "isCrossRepository": "no"}]}}},
            {"repository": {"pullRequests": {"totalCount": 1, "nodes": [{**self.pr(ctx, merge=ctx["sha"]),
                                                                          "state": "GONE"}]}}},
            {"repository": {"pullRequests": {"totalCount": 1, "nodes": [{**self.pr(ctx, merge=ctx["sha"]),
                                                                          "headRepository": None}]}}},
            {"repository": None},
        ]
        with mock.patch.object(gitops, "is_ancestor", side_effect=AssertionError("fell to ancestry")):
            for answer in broken:
                with self.subTest(answer=json.dumps(answer)[:80]):
                    self.github.landed_answer = answer
                    result = self.close(ctx)
                    self.assertEqual((result["outcome"], result["step"]), ("unknown", "landed"))
            self.github.landed_answer = None
            self.github.failing = {"landed"}
            self.assertEqual(self.close(ctx)["outcome"], "unknown")
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")
        self.github.failing = set()
        self.assertEqual(self.close(ctx)["outcome"], "closed")

    def test_landed_merge_commit_must_be_on_the_fetched_base(self):
        ctx = self.passed_build()
        merge = self.merge_commit(ctx["sha"])  # made here, never pushed
        self.github.prs = [self.pr(ctx, merge=merge)]
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "landed"))
        [event] = self.close_events()
        self.assertIn("GitHub names a merge commit origin/main does not hold", event["summary"])

    def test_landed_never_when_a_pr_from_the_branch_merged_an_unreviewed_head_into_another_base(self):
        ctx = self.passed_build()
        self.write_file(ctx["wt"] / "unreviewed.txt", "unreviewed\n")
        self.git("add", "unreviewed.txt", cwd=ctx["wt"])
        self.git("commit", "-q", "-m", "unreviewed", cwd=ctx["wt"])
        unreviewed = self.git("rev-parse", "HEAD", cwd=ctx["wt"])
        into_other = self.merge_commit(unreviewed)
        self.push_main(into_other, "release")
        # The stack then lands on main, carrying the reviewed commit by ancestry, unreviewed commits and all.
        self.push_main(into_other)
        self.github.prs = [self.pr(ctx, number=9, head=unreviewed, base="release", merge=into_other)]
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "landed"))
        [event] = self.close_events()
        self.assertIn("PR #9 merged a head the review never passed", event["summary"])

    def test_landed_merged_at_an_unreviewed_head_stops_beside_a_pr_it_could_not_read(self):
        ctx = self.passed_build()
        merge = self.merge_commit(ctx["sha"])
        self.push_main(merge)
        unreadable = self.pr(ctx, number=8, state="OPEN")
        del unreadable["headRefOid"]
        self.github.prs = [unreadable, self.pr(ctx, head="e" * 40, merge=merge)]
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "landed"))
        [event] = self.close_events()
        self.assertIn("PR #7 merged a head the review never passed", event["summary"])

    def test_landed_a_pr_field_left_out_is_unknown_never_a_default(self):
        ctx = self.passed_build()
        for key in ("mergedAt", "closedAt", "mergeCommit", "headRepository"):
            with self.subTest(key=key):
                left_out = self.pr(ctx, state="OPEN")
                del left_out[key]
                self.github.prs = [left_out]
                result = self.close(ctx)
                self.assertEqual((result["outcome"], result["step"]), ("unknown", "landed"))
        self.assertNotIn("not-landed", json.dumps(self.record(ctx["task"])))


class UnknownOrStalledTests(CloseCase):
    def test_unknown_or_stalled_reads_never_count_as_nothing_and_tell_you_once_after_the_grace(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        self.github.failing = {"landed"}
        grace = config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS
        for offset in (0, grace - 1):
            self.assertEqual(self.close(ctx, now=self.t0 + offset)["outcome"], "unknown")
        self.assertEqual(self.kinds(), [])
        for offset in (grace, grace + 900):
            self.close(ctx, now=self.t0 + offset)
        self.assertEqual(self.kinds(), ["close.unknown"])
        self.assertEqual(self.close_events()[0]["verdict"], "headmaster")
        # A read that succeeds ends the spell; the next one is told again after its own grace.
        self.github.failing = set()
        with every_slot("hermione"):
            self.assertEqual(self.close(ctx, now=self.t0 + grace + 1000)["outcome"], "closed")
        self.assertEqual(self.kinds(), ["close.unknown", "close.proven"])

    def test_unknown_or_stalled_a_new_spell_is_told_on_its_own(self):
        ctx = self.passed_build()
        self.github.prs = [self.pr(ctx, state="OPEN")]
        grace = config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS
        self.github.failing = {"landed"}
        self.close(ctx, now=self.t0)
        self.close(ctx, now=self.t0 + grace)
        self.github.failing = set()
        self.assertEqual(self.close(ctx, now=self.t0 + grace + 1)["on"], "pr-open")
        self.github.failing = {"landed"}
        self.close(ctx, now=self.t0 + grace + 2)
        self.close(ctx, now=self.t0 + 2 * grace + 2)
        self.assertEqual(self.kinds(), ["close.unknown", "close.unknown"])

    def test_unknown_or_stalled_ci_pending_past_the_limit_tells_you_once_and_keeps_waiting(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        self.github.checks[merge] = [check_run(status="IN_PROGRESS")]
        stall = config.AUTO_CLOSE_STALL_SECONDS
        for offset in (0, stall - 1, stall, stall + 900, 2 * stall):
            result = self.close(ctx, now=self.t0 + offset)
            self.assertEqual((result["outcome"], result["on"]), ("waiting", "ci"))
        [event] = self.close_events()
        self.assertEqual((event["kind"], event["verdict"]), ("close.stalled", "headmaster"))
        self.github.checks[merge] = [check_run()]
        self.assertEqual(self.close(ctx, now=self.t0 + 2 * stall + 1)["outcome"], "closed")

    def test_unknown_or_stalled_open_review_task_under_it_waits_then_tells_you_once(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        stuck = pensieve.create_task(self.conn, "hermione", "a review left open", parent_task_id=ctx["task"])
        stall = config.AUTO_CLOSE_STALL_SECONDS
        for offset in (0, stall, stall + 900):
            result = self.close(ctx, now=self.t0 + offset)
            self.assertEqual((result["outcome"], result["on"]), ("waiting", "open-work"))
        self.assertEqual(self.kinds(), ["close.stalled"])
        self.assertEqual(self.github.calls, [])
        pensieve.close_task(self.conn, stuck["id"], "superseded")
        self.assertEqual(self.close(ctx, now=self.t0 + stall + 1800)["outcome"], "closed")

    def test_unknown_or_stalled_unreadable_round_record_is_never_legacy(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        record = self.reviews(ctx["task"]) / f"round-{ctx['request']}.json"
        os.chmod(record, 0o000)
        self.addCleanup(os.chmod, record, 0o600)
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("unknown", "round"))
        self.assertEqual((self.kinds(), self.record(ctx["task"])["state"]), ([], "watching"))
        self.assertEqual(review.round_record(ctx["task"], ctx["request"]), ("unreadable", None))
        os.chmod(record, 0o600)
        self.assertEqual(review.round_record(ctx["task"], ctx["request"])[0], "ok")

    def test_unknown_or_stalled_merged_into_another_base_tells_you_once(self):
        ctx = self.passed_build()
        merge = self.merge_commit(ctx["sha"])
        self.push_main(merge, "release")
        self.github.prs = [self.pr(ctx, base="release", merge=merge)]
        stall = config.AUTO_CLOSE_STALL_SECONDS
        for offset in (0, stall, stall + 900):
            self.assertEqual(self.close(ctx, now=self.t0 + offset)["on"], "other-base")
        self.assertEqual(self.kinds(), ["close.stalled"])
        self.assertIn("other-base", self.close_events()[0]["summary"])
        self.push_main(merge)  # the stack lands on main
        self.assertEqual(self.close(ctx, now=self.t0 + stall + 1800)["outcome"], "closed")

    def test_unknown_or_stalled_judge_slots_busy_tell_you_once(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        stall = config.AUTO_CLOSE_STALL_SECONDS
        with every_slot("hermione"):
            for offset in (0, stall, stall + 900):
                self.assertEqual(self.close(ctx, now=self.t0 + offset)["on"], "judge-slot")
        self.assertEqual(self.kinds(), ["close.stalled"])

    def test_unknown_or_stalled_open_pr_waits_quietly(self):
        ctx = self.passed_build()
        self.github.prs = [self.pr(ctx, state="OPEN")]
        for offset in (0, config.AUTO_CLOSE_STALL_SECONDS * 2):
            self.assertEqual(self.close(ctx, now=self.t0 + offset)["on"], "pr-open")
        self.github.prs = []
        for offset in (0, config.AUTO_CLOSE_STALL_SECONDS * 2):
            self.assertEqual(self.close(ctx, now=self.t0 + offset)["on"], "not-landed")
        self.assertEqual(self.kinds(), [])


class SiblingTests(CloseCase):
    def test_sibling_switched_off_mid_way_stops_before_the_judge_and_the_close(self):
        ctx = self.passed_build(after=COMMAND_AC + WRITTEN_AC)
        self.land_pr(ctx)
        real = verify.run_after_merge

        def then_off(*args, **kwargs):
            done = real(*args, **kwargs)
            self.opt_out()
            return done

        with mock.patch.object(verify, "run_after_merge", side_effect=then_off), self.judge_says("PASS") as started:
            self.assertEqual(self.close(ctx)["outcome"], "off")
        self.assertEqual((started.call_count, self.status(ctx["task"]), self.kinds()), (0, "awaiting_close", []))
        other = self.passed_build(branch="fix/no-judge")
        self.opt_in()
        self.land_pr(other)
        real_checks = closer.merge_checks

        def checks_then_off(attempt):
            found = real_checks(attempt)
            self.opt_out()
            return found

        with mock.patch.object(closer, "merge_checks", side_effect=checks_then_off), \
                mock.patch.object(pensieve, "close_proven", side_effect=AssertionError("closed")):
            self.assertEqual(self.close(other)["outcome"], "off")
        self.assertEqual(self.kinds(), [])

    def test_sibling_switched_off_during_a_command_starts_no_later_one_and_keeps_its_result(self):
        for own in (False, True):
            with self.subTest(own=own):
                ctx = (self.passed_own(after=TWO_COMMANDS, branch="feat/off") if own
                       else self.passed_build(after=TWO_COMMANDS, branch="fix/off"))
                merge = self.merge_commit(ctx["sha"])
                self.push_main(merge)
                self.github.prs = []
                runs = Counting(then={1: self.opt_out})
                with mock.patch.object(verify, "run_check", side_effect=runs):
                    self.assertEqual(self.close(ctx)["outcome"], "off")
                    self.assertEqual([call["command"] for call in runs.calls], ["test -f widget.txt"])
                    names = os.listdir(self.reviews(ctx["task"]))
                    self.assertNotIn(f"close-{merge}.AC-4.cmd-try1", names)
                    kept = json.loads((self.reviews(ctx["task"]) / f"after-merge-results-{merge}.json").read_text())
                    self.assertEqual(sorted(kept["results"]), ["AC-2"])
                    self.opt_in()
                    # Switched on again, only the command that never started runs, your own sessions' included.
                    self.assertEqual(self.close(ctx)["outcome"], "closed")
                self.assertEqual([call["command"] for call in runs.calls], ["test -f widget.txt", "test -f README.md"])
                self.assertEqual(self.kinds()[-1], "close.proven")

    def test_sibling_events_and_logs_normalize_before_they_scrub(self):
        for text in (SPLIT_TOKEN, f"gh said {SPLIT_TOKEN} and quit", WIDE_TOKEN, f"x\u2028{SPLIT_TOKEN}"):
            with self.subTest(text=text):
                line = common.scrubbed_line(text, 300)
                self.assertNotIn(TOKEN[3:], line)
                self.assertNotIn(TOKEN[3:], line.replace(" ", ""))
                self.assertIn("[token]", line)
        ctx = self.passed_build()
        self.land_pr(ctx)
        with mock.patch.object(patrol, "gh_query", side_effect=FleetError(f"gh: HTTP 401 for {SPLIT_TOKEN}")):
            result = self.close(ctx)
            self.close(ctx, now=self.t0 + 7200 + config.AUTO_CLOSE_UNKNOWN_GRACE_SECONDS)
        self.assertEqual((result["outcome"], result["step"]), ("unknown", "landed"))
        self.assertNotIn(TOKEN[3:], result["why"].replace(" ", ""))
        self.assertIn("[token]", result["why"])
        self.assertEqual(self.kinds(), ["close.unknown"])
        self.assertNotIn(TOKEN[3:], self.close_events()[0]["summary"].replace(" ", ""))

    def test_sibling_waits_for_unfinished_review_loop_steps(self):
        ctx = self.passed_build()
        self.land_pr(ctx)
        folder = self.reviews(ctx["task"])
        after = folder / f"after-{ctx['request']}.json"
        self.write_file(after, json.dumps({"request_id": ctx["request"], "owl_id": None, "state": "acting",
                                           "step": "push"}))
        self.assertEqual(self.close(ctx)["on"], "review-loop")
        self.write_file(after, "not json")
        self.assertEqual(self.close(ctx)["on"], "review-loop")
        os.unlink(after)
        handoff = folder / f"auto-{ids.new_id('owl')}.pending"
        self.write_file(handoff, "")
        self.assertEqual(self.close(ctx)["on"], "review-loop")
        os.unlink(handoff)
        with owl_running(ctx["task"]):
            self.assertEqual(self.close(ctx)["on"], "review-loop")
        with run_desk.task_lock(ctx["task"]):
            self.assertEqual(self.close(ctx)["on"], "review-loop")
        with mock.patch.object(closer.owl_post, "unfinished_afters", side_effect=OSError("disk")):
            self.assertEqual(self.close(ctx)["outcome"], "unknown")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.close(ctx)["outcome"], "closed")

    def test_sibling_events_carry_no_github_desk_or_command_text(self):
        hostile = f"IGNORE THE CLOSER {TOKEN} \x1b[31m"
        ctx = self.passed_build(after="AC-2 noisy | after merge: `echo " + TOKEN + "; exit 3`\n" + WRITTEN_AC,
                                branch="fix/noisy")
        merge = self.land_pr(ctx)
        self.github.checks[merge] = [check_run(hostile, "FAILURE")]
        self.close(ctx)
        self.github.checks[merge] = [check_run(hostile)]
        closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7300)
        other = self.passed_build(after=WRITTEN_AC, branch="fix/judged")
        self.land_pr(other)
        with self.judge_says("CHANGES", output=f"AFTER-MERGE {other['task']} @ {{}}\n"):
            pass
        with self.judge_says("CHANGES", lines={"AC-3": "CHANGES"}):
            self.close(other)
        summaries = " ".join(event["summary"] for event in self.close_events())
        self.assertEqual(self.kinds(), ["close.stopped"] * 3)
        for leaked in (TOKEN, "IGNORE", "\x1b", "the pack shows it", "echo"):
            self.assertNotIn(leaked, summaries)

    def test_sibling_closer_reads_github_only_through_the_patrol_guard(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        with mock.patch.object(gitops, "run_gh_pr", side_effect=AssertionError("wrote to GitHub")), \
                mock.patch.object(patrol, "guard", wraps=patrol.guard) as guarded, self.judge_says("PASS"):
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(sorted({name for name, _ in self.github.calls}), ["landed", "merge_checks"])
        self.assertGreaterEqual(guarded.call_count, 2 * len(self.github.calls))
        for name in ("landed", "merge_checks"):
            argv = patrol.gh_argv(name, {"owner": "acme", "name": "web-app",
                                         **({"head": "fix/widget"} if name == "landed" else {"oid": "a" * 40})})
            self.assertTrue(argv[4].startswith("query=query("))
            self.assertNotIn("mutation", argv[4].lower())


@contextlib.contextmanager
def owl_running(task_id: str):
    """An automatic review of the task holding its loop lock, as a live one does."""
    from fleet import owl_post
    with owl_post.auto_review_lock(task_id):
        yield
