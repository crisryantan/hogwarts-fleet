"""Auto-close: CI on the merge commit and the after-merge commands.

Split from test_auto_close.py so the parallel runner can run it beside the rest; it shares CloseCase.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from unittest import mock

from hogwarts import pensieve

from fleet import closer, common, config, run_desk, safefs, verify
from fleet import worktree
from tests_fleet.support import every_slot
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import REPO_ID
from tests_fleet.test_auto_close import COMMAND_AC, TOKEN, TWO_COMMANDS, WIDE_TOKEN, WRITTEN_AC, check_run, CloseCase, \
    Counting, lock_busy, status_context, tearDownModule  # noqa: F401


def gone(pid: int, seconds: float = 5.0) -> bool:
    """Whether the process has ended (and been reaped by whoever its parent is now) within seconds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class MergeChecksTests(CloseCase):
    def landed(self, after: str = "") -> tuple:
        ctx = self.passed_build(after=after)
        return ctx, self.land_pr(ctx)

    def checks_of(self, ctx: dict, merge: str, landed_seen_at: int = None) -> dict:
        """merge_checks alone, as an attempt that has proven the merge commit reads CI on it."""
        attempt = closer.Attempt(self.conn, ctx["task"], False, self.t0 + 7200, {})
        attempt.found, attempt.merge_sha = {"repo": REPO_ID}, merge
        attempt.record = {"landed_seen_at": self.t0 if landed_seen_at is None else landed_seen_at}
        return closer.merge_checks(attempt)

    def commit_answer(self, merge: str, nodes: list, total: int = None, more: bool = False) -> dict:
        return {"repository": {"object": {"__typename": "Commit", "oid": merge, "statusCheckRollup": {"contexts": {
            "totalCount": len(nodes) if total is None else total, "pageInfo": {"hasNextPage": more},
            "nodes": nodes}}}}}

    def test_merge_checks_success_neutral_skipped_pass_and_pending_waits(self):
        ctx, merge = self.landed()
        self.github.checks[merge] = [check_run("lint", "NEUTRAL"), check_run("docs", "SKIPPED"),
                                     check_run("pending", status="IN_PROGRESS"), status_context()]
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "ci"))
        for status in ("QUEUED", "WAITING", "PENDING", "REQUESTED"):
            self.github.checks[merge][2] = check_run("pending", status=status)
            self.assertEqual(self.close(ctx)["on"], "ci")
        self.github.checks[merge][2] = status_context("ci/old", "PENDING")
        self.assertEqual(self.close(ctx)["on"], "ci")
        self.github.checks[merge][2] = check_run("build", "SUCCESS")
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        closure = pensieve.task_closure(self.conn, ctx["task"])
        self.assertEqual((closure["ci"], closure["ci_checks"]), ("green", 4))

    def test_merge_checks_none_counts_only_after_the_settle_time(self):
        ctx, merge = self.landed()
        self.github.checks[merge] = None
        with mock.patch.object(config, "AUTO_CLOSE_CI_SETTLE_SECONDS", 1800):
            first = self.close(ctx, now=self.t0)
            self.assertEqual((first["outcome"], first["on"]), ("waiting", "ci-settle"))
            self.assertEqual(self.close(ctx, now=self.t0 + 1799)["on"], "ci-settle")
            self.github.checks[merge] = []
            self.assertEqual(self.close(ctx, now=self.t0 + 1800)["outcome"], "closed")
        self.assertEqual(pensieve.task_closure(self.conn, ctx["task"])["ci"], "none")
        self.assertEqual(self.kinds(), ["close.proven"])

    def test_merge_checks_green_counts_only_after_the_settle_time_and_red_stops_at_once(self):
        ctx, merge = self.landed()
        with mock.patch.object(config, "AUTO_CLOSE_CI_SETTLE_SECONDS", 1800):
            self.assertEqual(self.close(ctx, now=self.t0)["on"], "ci-settle")
            self.assertEqual(self.record(ctx["task"])["landed_seen_at"], self.t0)
            self.github.checks[merge] = [check_run(), check_run("slow", "TIMED_OUT")]
            result = self.close(ctx, now=self.t0 + 60)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "ci"))
        self.assertEqual(self.kinds(), ["close.stopped"])

    def test_merge_checks_red_conclusions_and_states_each_stop(self):
        ctx, merge = self.landed()
        reds = [check_run(conclusion=conclusion) for conclusion in
                ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE")]
        self.github.checks[merge] = [check_run(), reds[0]]
        self.assertEqual(self.close(ctx)["step"], "ci")
        for node in reds[1:] + [status_context(state="ERROR"), status_context(state="FAILURE")]:
            with self.subTest(node=node):
                self.github.checks[merge] = [check_run(), node]
                with self.assertRaises(closer.Stop) as stopped:
                    self.checks_of(ctx, merge)
                self.assertEqual(stopped.exception.step, "ci")
        # A record a kill lost still finds the stop told before it, so the task stays stopped and is told once.
        os.unlink(self.reviews(ctx["task"]) / "close.json")
        self.assertEqual(self.close(ctx)["outcome"], "not the closer's")
        self.assertEqual((self.kinds(), self.record(ctx["task"])["state"]), (["close.stopped"], "stopped"))

    def test_merge_checks_any_red_stops_before_unknown_or_pending_siblings(self):
        ctx, merge = self.landed()
        red = check_run("build", "FAILURE")
        for answer in (self.commit_answer(merge, [check_run(conclusion="WEIRD"), red]),
                       self.commit_answer(merge, [check_run(status="IN_PROGRESS"), red]),
                       self.commit_answer(merge, [{"__typename": "Mystery"}, status_context(state="ERROR")]),
                       self.commit_answer(merge, [red], total=150, more=True),
                       self.commit_answer(merge, [check_run(), red], total=3)):
            with self.subTest(answer=json.dumps(answer)[-120:]):
                self.github.checks_answer = answer
                with self.assertRaises(closer.Stop) as stopped:
                    self.checks_of(ctx, merge, landed_seen_at=self.t0 + 7200)  # inside the settle time too
                self.assertEqual(stopped.exception.step, "ci")
        self.github.checks_answer = self.commit_answer(merge, [check_run(conclusion="WEIRD"), red])
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "ci"))
        self.assertEqual(self.kinds(), ["close.stopped"])

    def test_merge_checks_rollup_left_out_is_unknown_and_only_null_is_none(self):
        ctx, merge = self.landed()
        self.github.checks_answer = {"repository": {"object": {"__typename": "Commit", "oid": merge}}}
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("unknown", "ci"))
        self.assertIsNone(pensieve.task_closure(self.conn, ctx["task"]))
        self.github.checks_answer = {"repository": {"object": {"__typename": "Commit", "oid": merge,
                                                               "statusCheckRollup": None}}}
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(pensieve.task_closure(self.conn, ctx["task"])["ci"], "none")

    def test_merge_checks_truncated_or_unknown_values_are_unknown(self):
        ctx, merge = self.landed()
        commit = {"__typename": "Commit", "oid": merge}
        nodes = [check_run()]
        broken = [
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 101, "pageInfo": {"hasNextPage": True}, "nodes": nodes}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 2, "pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 1, "nodes": nodes}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [check_run(conclusion="WEIRD")]}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [check_run(status="LOST")]}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {"contexts": {
                "totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "Mystery"}]}}}}},
            {"repository": {"object": {**commit, "statusCheckRollup": {}}}},
            {"repository": {"object": {"__typename": "Tree", "oid": merge}}},
            {"repository": {"object": {**commit, "oid": "f" * 40, "statusCheckRollup": None}}},
            {"repository": {"object": None}},
        ]
        for answer in broken:
            with self.subTest(answer=json.dumps(answer)[:90]):
                self.github.checks_answer = answer
                result = self.close(ctx)
                self.assertEqual((result["outcome"], result["step"]), ("unknown", "ci"))
        self.github.checks_answer = None
        self.github.failing = {"merge_checks"}
        self.assertEqual(self.close(ctx)["step"], "ci")
        self.assertEqual((self.status(ctx["task"]), self.kinds()), ("awaiting_close", []))

    def test_merge_checks_read_again_before_the_close(self):
        ctx, merge = self.landed(after=WRITTEN_AC)
        with every_slot("hermione"):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "judge-slot"))
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        with self.judge_says("PASS") as started:
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "ci"))
        self.assertEqual(started.call_count, 0)
        self.assertEqual([name for name, _ in self.github.calls].count("merge_checks"), 2)


class AfterMergeCommandTests(CloseCase):
    def counting(self, *kill_at: int):
        runs = Counting(kill_at)
        patcher = mock.patch.object(verify, "run_check", side_effect=runs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runs

    def after_merges(self, ctx: dict) -> list:
        return [call for call in self.runs.calls if call["after"]]

    def test_after_merge_command_runs_in_a_fresh_detached_worktree_at_the_merge_commit(self):
        command = "AC-2 the merge commit is checked out | after merge: `git rev-parse --short=12 HEAD; git symbolic-ref -q HEAD || echo detached; ls`\n"
        ctx = self.passed_build(after=command)
        merge = self.land_pr(ctx)
        self.write_file(ctx["wt"] / "stray.txt", "left in the build's own worktree\n")
        self.runs = self.counting()
        with mock.patch.object(worktree, "add_worktree", wraps=worktree.add_worktree) as added:
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        name = f"{ctx['task']}.merged-{merge[:12]}"
        [call] = added.call_args_list
        self.assertEqual((call.args[1], call.args[3], call.args[4], call.kwargs["detach_at"], call.kwargs["name"]),
                         (ctx["task"], "origin/main", None, merge, name))
        [ran] = self.after_merges(ctx)
        self.assertEqual(ran["path"], str(self.castle / "worktrees" / name))
        evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
        self.assertTrue(evidence.startswith(f"AFTER-MERGE EVIDENCE {ctx['task']} @ {merge}\nPASS {ctx['sha']}\n"))
        self.assertIn(f"    {merge[:12]}\n    detached\n", evidence)
        self.assertNotIn("stray.txt", evidence)
        self.assertIn("AC-2 exit 0\n", evidence)
        castle = self.castle / "tasks" / ctx["parent"]
        self.assertEqual((castle / "after-merge-evidence.md").read_text(), evidence)
        self.assertEqual((castle / f"after-merge-evidence-{merge[:12]}.md").read_text(), evidence)
        # Taken back, through git, once the task closed.
        self.assertFalse((self.castle / "worktrees" / name).exists())
        self.assertNotIn(f"{name}.json", os.listdir(self.office / "worktrees"))
        self.assertNotIn(name, self.git("worktree", "list"))
        self.assertEqual(self.record(ctx["task"])["state"], "done")

    def test_after_merge_command_runs_under_the_same_sandbox_rule_as_verify(self):
        self.runs = self.counting()
        build = self.passed_build(after=COMMAND_AC)
        self.land_pr(build)
        self.assertEqual(self.close(build)["outcome"], "closed")
        own = self.passed_own(after=COMMAND_AC)
        self.push_main(self.merge_commit(own["sha"]))
        self.github.prs = []
        self.assertEqual(self.close(own)["outcome"], "closed")
        by_path = {}
        for call in self.runs.calls:
            by_path.setdefault(call["after"], []).append(call["sandboxed"])
        self.assertEqual(by_path[False], [True, False])  # verify before the merge: Harry sandboxed, yours not
        self.assertEqual(by_path[True], [True, False])  # after the merge: the same rule
        self.assertTrue(verify.sandboxed_for(pensieve.get_task(self.conn, build["task"])))
        self.assertFalse(verify.sandboxed_for(pensieve.get_task(self.conn, own["task"])))

    def test_after_merge_command_evidence_is_scrubbed_before_its_cut(self):
        # The key's header falls before the last 40 lines and its short body lines after, so only a scrub of the whole
        # window read masks them: no line of the body is a lone 40 character base64 line on its own.
        command = ("AC-2 the key stays hidden | after merge: `echo -----BEGIN RSA PRIVATE KEY-----;"
                   " for i in $(seq 10 69); do echo MIIEpAIBAAKCAQEAx${i}Yz9WqYz9Wq; done;"
                   f" echo token {TOKEN}; echo QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg==`\n")
        ctx = self.passed_build(after=command)
        merge = self.land_pr(ctx)
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
        output = evidence.split("output, last 40 lines:\n", 1)[1]
        self.assertNotIn("MIIEpAIBAAKCAQEAx", output)
        self.assertNotIn(TOKEN, evidence)
        self.assertIn("[private_key]", output)
        self.assertIn("    [base64]\n", output)
        self.assertLessEqual(len(output.splitlines()), config.EVIDENCE_EXCERPT_LINES)

    def test_after_merge_command_evidence_is_normalized_before_its_scrub(self):
        # The command prints the token with a zero-width space inside it and once more in fullwidth letters.
        command = (f"AC-2 the hidden token stays hidden | after merge: `printf '%s\\342\\200\\213%s\\n' {TOKEN[:2]}"
                   f" {TOKEN[2:]}; printf '%s\\n' {WIDE_TOKEN}`\n")
        ctx = self.passed_build(after=command, branch="fix/hidden-output")
        merge = self.land_pr(ctx)
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
        output = evidence.split("output, last 40 lines:\n", 1)[1]
        self.assertNotIn(TOKEN, common.normalized(output))
        self.assertNotIn("\u200b", evidence)
        self.assertEqual(output.count("[token]"), 2)

    def test_after_merge_command_output_is_read_through_its_own_descriptor_never_a_link_in_its_place(self):
        # The command swaps its output file, in the scratch folder it can write, for a link to a file outside its reach.
        outside = self.write_file(self.tmp / "outside.txt", "words-from-outside-the-command-reach\n")
        command = ("AC-2 the output is its own | after merge: `echo its own words; for f in \"$HOME\"/../out-*.log;"
                   f" do rm -f \"$f\"; ln -s {outside} \"$f\"; done`\n")
        ctx = self.passed_build(after=command, branch="fix/linked-output")
        merge = self.land_pr(ctx)
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
        self.assertIn("    its own words\n", evidence)
        self.assertNotIn("words-from-outside", evidence)

    def test_after_merge_command_process_group_ends_with_it_and_keeps_its_locks(self):
        work, scratch = self.tmp / "work", self.tmp / "scratch"
        for folder in (work, scratch / "home", scratch / "tmp"):
            folder.mkdir(parents=True)
        record = {"path": str(work), "links": []}
        left = []

        def end_left() -> None:
            for pid in left:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)

        self.addCleanup(end_left)
        result = verify.run_check(record, str(scratch), "sleep 60 & echo $!", sandboxed=False)
        left.append(int(result["lines"][-1]))
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(gone(left[-1]))  # what it left behind ended with it
        with mock.patch.object(config, "VERIFY_TIMEOUT_SECONDS", 1):
            result = verify.run_check(record, str(scratch), "sleep 60 & echo $!; sleep 60", sandboxed=False)
        left.append(int(result["lines"][-1]))
        self.assertEqual(result["exit_code"], -1)
        self.assertTrue(gone(left[-1]))
        # The fds its caller holds for it are its own while it runs.
        with open(self.tmp / "held", "w") as handle:
            result = verify.run_check(record, str(scratch), f"test -e /dev/fd/{handle.fileno()}", sandboxed=False,
                                      keep_fds=(handle.fileno(),))
            self.assertEqual(result["exit_code"], 0)
            result = verify.run_check(record, str(scratch), f"test -e /dev/fd/{handle.fileno()}", sandboxed=False)
            self.assertEqual(result["exit_code"], 1)

    def test_ollivander_stop_gate_is_held_by_each_command_for_its_life(self):
        ctx = self.passed_build(after=TWO_COMMANDS)
        merge = self.land_pr(ctx)
        real, seen = verify.run_check, []

        def probe(record, scratch, command, sandboxed=True, keep_fds=()):
            with safefs.opened_dir(config.OFFICE_ROOT, "locks") as locks:
                wanted = {safefs.lstat(locks, name).st_ino
                          for name in (config.UPDATE_LOCK, run_desk.task_lock_name(ctx["task"]))}
            seen.append({os.fstat(fd).st_ino for fd in keep_fds} == wanted)
            seen.append(lock_busy(config.UPDATE_LOCK))  # no CLI update can start while it runs
            # The command's process holds both itself.
            opened = " && ".join(f"test -e /dev/fd/{fd}" for fd in keep_fds)
            return real(record, scratch, f"{opened} && {command}", sandboxed, keep_fds)

        with mock.patch.object(verify, "run_check", side_effect=probe):
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(seen, [True, True, True, True])
        self.assertFalse(lock_busy(config.UPDATE_LOCK))
        evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
        self.assertIn("AC-2 exit 0\nAC-4 exit 0\n", evidence)

    def test_ollivander_stop_update_running_at_a_command_launch_runs_nothing_and_takes_no_try(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        self.runs = self.counting()
        # A CLI update holds Ollivander's lock, with no stop file or marker in place yet.
        with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks, \
                safefs.held_lock(locks, config.UPDATE_LOCK, blocking=False):
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "ollivander"))
        self.assertEqual((self.after_merges(ctx), self.kinds()), ([], []))
        self.assertEqual([name for name in os.listdir(self.reviews(ctx["task"])) if ".cmd-try" in name], [])
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertIn(f"close-{merge}.AC-2.cmd-try1", os.listdir(self.reviews(ctx["task"])))

    def test_ollivander_stop_placed_during_a_command_starts_no_later_one_and_keeps_its_result(self):
        ctx = self.passed_build(after=TWO_COMMANDS)
        merge = self.land_pr(ctx)
        stop = self.office / config.STATE_DIR / config.STOP_FILE
        self.runs = self.counting()
        self.runs.then = {1: lambda: self.write_file(stop, "stopped for a new Codex\n")}
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "ollivander"))
        self.assertEqual([call["command"] for call in self.after_merges(ctx)], ["test -f widget.txt"])
        names = os.listdir(self.reviews(ctx["task"]))
        self.assertIn(f"close-{merge}.AC-2.cmd-try1", names)
        self.assertNotIn(f"close-{merge}.AC-4.cmd-try1", names)
        kept = json.loads((self.reviews(ctx["task"]) / f"after-merge-results-{merge}.json").read_text())
        self.assertEqual((sorted(kept["results"]), kept["results"]["AC-2"]["exit_code"]), (["AC-2"], 0))
        os.unlink(stop)
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual([call["command"] for call in self.after_merges(ctx)],
                         ["test -f widget.txt", "test -f README.md"])

    def test_after_merge_command_tries_stop_at_three(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        self.runs = self.counting(1, 2, 3)
        for attempt in range(3):
            with self.assertRaises(Killed):
                self.close(ctx)
        self.assertEqual(sorted(name for name in os.listdir(self.reviews(ctx["task"])) if ".cmd-try" in name),
                         [f"close-{merge}.AC-2.cmd-try{n}" for n in (1, 2, 3)])
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "commands"))
        [event] = self.close_events()
        self.assertIn("AC-2 started 3 times and never finished", event["summary"])
        self.assertEqual(len(self.runs.calls), 3)

    def test_after_merge_command_unsandboxed_runs_once_and_a_cut_short_run_stops(self):
        self.runs = self.counting(2)
        ctx = self.passed_own(after=COMMAND_AC)
        self.push_main(ctx["sha"])
        with self.assertRaises(Killed):
            self.close(ctx)
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "commands"))
        self.assertEqual(self.close(ctx)["outcome"], "not the closer's")
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual(len(self.runs.calls), 2)
        [event] = self.close_events()
        self.assertIn("may or may not have run; nothing was run again: fleet close runs them by hand", event["summary"])
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_after_merge_command_unsandboxed_never_reruns_after_a_fleet_close_cut_short(self):
        self.runs = self.counting(2)
        ctx = self.passed_own(after=COMMAND_AC)
        self.push_main(ctx["sha"])
        with self.assertRaises(Killed):
            closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7200)
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "commands"))
        self.assertEqual(len(self.runs.calls), 2)
        # Only another fleet close runs them again, past the automatic cap.
        self.assertEqual(closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7300)["outcome"], "closed")
        self.assertEqual(len(self.runs.calls), 3)

    def test_after_merge_command_waits_on_ollivander_stop_and_takes_no_try(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        self.runs = self.counting()
        stop = self.office / config.STATE_DIR / config.STOP_FILE
        self.write_file(stop, "stopped for a new Codex\n")
        for _ in range(2):
            result = self.close(ctx)
            self.assertEqual((result["outcome"], result["on"]), ("waiting", "ollivander"))
        self.assertEqual([name for name in os.listdir(self.reviews(ctx["task"])) if ".cmd-try" in name], [])
        os.unlink(stop)
        self.write_file(self.office / config.STATE_DIR / config.UPDATING_FILE, "updating\n")
        self.assertEqual(self.close(ctx)["on"], "ollivander")
        os.unlink(self.office / config.STATE_DIR / config.UPDATING_FILE)
        self.assertEqual(self.after_merges(ctx), [])
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(os.listdir(self.reviews(ctx["task"])).count(f"close-{merge}.AC-2.cmd-try1"), 1)

    def test_sibling_switched_off_before_an_after_merge_command_runs_nothing(self):
        ctx = self.passed_build(after=COMMAND_AC)
        self.land_pr(ctx)
        self.runs = self.counting()
        real = worktree.add_worktree

        def then_off(*args, **kwargs):
            made = real(*args, **kwargs)
            self.opt_out()
            return made

        with mock.patch.object(worktree, "add_worktree", side_effect=then_off):
            self.assertEqual(self.close(ctx)["outcome"], "off")
        self.assertEqual(self.after_merges(ctx), [])
        self.assertEqual([name for name in os.listdir(self.reviews(ctx["task"])) if ".cmd-try" in name], [])
        self.assertEqual((self.kinds(), self.status(ctx["task"])), ([], "awaiting_close"))
