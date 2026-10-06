"""Auto-close: kills at every step of a close.

Split from test_auto_close.py so the parallel runner can run it beside the rest; it shares CloseCase.
"""
from __future__ import annotations

import os
import subprocess
from unittest import mock

from hogwarts import capacity, owlery, pensieve

from fleet import closer, config, gitops, run_desk, safefs, verify
from tests_fleet.support import fake_children
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import claude_stream
from tests_fleet.test_auto_close import COMMAND_AC, TWO_COMMANDS, WRITTEN_AC, check_run, CloseCase, Counting, \
    lock_busy, tearDownModule  # noqa: F401


class KillTests(CloseCase):
    def test_kill_during_the_judge_keeps_its_run_and_never_starts_a_second(self):
        # The closer is killed before anything kept how its judge run ended, as when it dies while the judge runs on:
        # that run is never a verdict, even one that wrote a PASS, and is never discarded either. It stays unknown with
        # its run id kept, and no other judge run starts, by hand too.
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)
        with self.judge_says("PASS"), mock.patch.object(run_desk, "plan_limit", side_effect=Killed()), \
                self.assertRaises(Killed):
            self.close(ctx)
        first = self.record(ctx["task"])["judge"]
        self.assertEqual((first["outcome"], first["try"]), (None, 1))
        self.assertIsNotNone(first["run_id"])
        with self.judge_says("PASS") as started:
            for manual in (False, True):
                result = self.close(ctx, now=self.t0 + 9000, manual=manual)
                self.assertEqual((result["outcome"], result.get("step"), started.call_count), ("unknown", "judge", 0))
                self.assertIn("no other judge run starts", result["why"])
        self.assertEqual(len(self.judged), 1)
        self.assertEqual(self.record(ctx["task"])["judge"]["run_id"], first["run_id"])
        kept = [name for name in os.listdir(self.reviews(ctx["task"])) if name.startswith("after-merge-review-")]
        self.assertEqual(kept, [])
        self.assertNotIn(f"close-{merge}.judge-try2", os.listdir(self.reviews(ctx["task"])))
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_kill_after_the_judge_ended_before_its_outcome_was_kept_reads_that_run_and_starts_no_other(self):
        # The judge said CHANGES and ended; the closer died before it kept how. The run's own end record says it ended
        # clean, so its CHANGES stands, and no second judge run gets the chance to say PASS.
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        real = closer.write_record

        def killed(record, **kwargs):
            if record["judge"] is not None and record["judge"]["outcome"] is not None:
                raise Killed()
            return real(record, **kwargs)

        with self.judge_says("CHANGES", lines={"AC-3": "CHANGES"}), self.assertRaises(Killed), \
                mock.patch.object(closer, "write_record", side_effect=killed):
            self.close(ctx)
        first = self.record(ctx["task"])["judge"]
        self.assertEqual((first["run_id"] is not None, first["outcome"]), (True, None))
        with self.judge_says("PASS") as started:
            result = self.close(ctx)
        self.assertEqual((result["outcome"], result.get("step"), started.call_count), ("stopped", "judge", 0))
        self.assertEqual((len(self.judged), self.status(ctx["task"])), (1, "awaiting_close"))
        [kept] = [name for name in os.listdir(self.reviews(ctx["task"])) if name.startswith("after-merge-review-")]
        self.assertIn(first["run_id"], kept)
        self.assertEqual(self.record(ctx["task"])["judge"]["outcome"], "ok")

    def test_kill_during_a_judge_run_that_left_no_final_text_starts_the_next_try(self):
        # The closer is killed before anything kept how its judge run ended, and that run, read whole, wrote no result
        # event: it left no verdict that another run could replace, so its try is over and the next one starts.
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)

        def cut_short(argv, **kwargs):
            os.write(kwargs["stdout"], claude_stream("AFTER-MERGE").rsplit("\n", 2)[0].encode("utf-8") + b"\n")
            return subprocess.CompletedProcess(argv, 0)

        with fake_children(cut_short), mock.patch.object(run_desk, "plan_limit", side_effect=Killed()), \
                self.assertRaises(Killed):
            self.close(ctx)
        self.assertEqual(self.record(ctx["task"])["judge"]["outcome"], None)
        with self.judge_says("PASS") as started:
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual((started.call_count, len(self.judged)), (1, 1))
        self.assertIn(f"close-{merge}.judge-try2", os.listdir(self.reviews(ctx["task"])))

    def test_kill_before_the_judge_launch_was_counted_starts_the_next_try(self):
        # Killed after the run id was kept and before the launch was counted: no process started, so that try is over.
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)
        with self.judge_says("PASS") as started, self.assertRaises(Killed), \
                mock.patch.object(capacity, "record_launch", side_effect=Killed()):
            self.close(ctx)
        self.assertEqual(started.call_count, 0)
        first = self.record(ctx["task"])["judge"]
        self.assertEqual((first["run_id"] is not None, first["outcome"]), (True, None))
        with self.judge_says("PASS") as started:
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual((started.call_count, len(self.judged)), (1, 1))
        self.assertIn(f"close-{merge}.judge-try2", os.listdir(self.reviews(ctx["task"])))

    def test_judge_output_too_large_to_read_its_result_whole_keeps_its_run(self):
        # The window run_desk reads cuts through the judge's result event: that is no "no verdict", so the run is kept,
        # no second judge run starts, and once its output can be read whole its own CHANGES stands.
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        with mock.patch.object(run_desk, "RUN_OUTPUT_MAX_BYTES", 4096), \
                self.judge_says("CHANGES", lines={"AC-3": "CHANGES"}, filler="x" * 9000 + "\n"):
            result = self.close(ctx)
            first = self.record(ctx["task"])["judge"]
            self.assertEqual((result["outcome"], result.get("step")), ("stopped", "judge"))
            with self.judge_says("PASS") as started:
                self.assertEqual(self.close(ctx)["outcome"], "not the closer's")
                result = closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 9000)
                self.assertEqual((result["outcome"], result.get("step")), ("stopped", "judge"))
        self.assertEqual(self.record(ctx["task"])["judge"]["run_id"], first["run_id"])
        with self.judge_says("PASS") as later:
            result = closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 9600)
        self.assertEqual((result["outcome"], result.get("step")), ("stopped", "judge"))
        self.assertEqual((started.call_count, later.call_count, len(self.judged)), (0, 0, 1))
        self.assertIn("the after-merge judge said CHANGES", self.close_events()[-1]["summary"])
        self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_kill_after_a_failed_or_capped_judge_run_never_takes_its_verdict(self):
        for index, (exit_code, limit) in enumerate(((1, None), (0, "claude_plan"))):
            with self.subTest(exit_code=exit_code, limit=limit):
                ctx = self.passed_build(after=WRITTEN_AC, branch=f"fix/ended-{index}")
                self.land_pr(ctx)
                real, ended = run_desk.run, []

                def ended_then_killed(*args, **kwargs):
                    ended.append(real(*args, **kwargs))
                    raise Killed()  # the closer dies before it keeps how the run ended

                with self.judge_says("PASS", exit_code=exit_code), self.assertRaises(Killed), \
                        mock.patch.object(run_desk, "plan_limit", return_value=limit), \
                        mock.patch.object(run_desk, "run", side_effect=ended_then_killed):
                    self.close(ctx)
                self.assertEqual((ended[0]["exit_code"], ended[0]["cap_source"]), (exit_code, limit))
                with self.judge_says("CHANGES", lines={"AC-3": "CHANGES"}) as started:
                    result = self.close(ctx)
                self.assertEqual((result["outcome"], result["step"], started.call_count), ("stopped", "judge", 1))
                self.assertEqual(self.status(ctx["task"]), "awaiting_close")

    def test_kill_before_the_close_proves_again_without_rerunning_commands_or_the_judge(self):
        ctx = self.passed_build(after=COMMAND_AC + WRITTEN_AC)
        self.land_pr(ctx)
        runs = Counting()
        with mock.patch.object(verify, "run_check", side_effect=runs), self.judge_says("PASS"):
            with mock.patch.object(pensieve, "close_proven", side_effect=Killed()):
                with self.assertRaises(Killed):
                    self.close(ctx)
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual((len(runs.calls), len(self.judged)), (1, 1))
        self.assertEqual([name for name, _ in self.github.calls], ["landed", "merge_checks"] * 2)
        self.assertEqual(self.kinds(), ["close.proven"])

    def test_kill_after_the_close_finishes_housekeeping_without_a_second_event(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        with mock.patch.object(closer, "housekeep", side_effect=Killed()):
            with self.assertRaises(Killed):
                self.close(ctx)
        name = f"{ctx['task']}.merged-{merge[:12]}"
        self.assertTrue((self.castle / "worktrees" / name).exists())
        self.assertEqual(self.status(ctx["task"]), "closed")
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertFalse((self.castle / "worktrees" / name).exists())
        self.assertEqual((self.kinds(), self.record(ctx["task"])["state"]), (["close.proven"], "done"))

    def test_kill_between_a_stop_event_and_its_record_tells_you_once_and_never_closes(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        real = closer.write_record

        def killed_at_stop(record, **kwargs):
            if record["state"] == "stopped":
                raise Killed()
            return real(record, **kwargs)

        with mock.patch.object(closer, "write_record", side_effect=killed_at_stop):
            with self.assertRaises(Killed):
                self.close(ctx)
        self.assertEqual(self.record(ctx["task"])["state"], "watching")
        # CI turns green before the next pass, which still finds the stop told and closes nothing.
        self.github.checks[merge] = [check_run("build (re-run)")]
        calls = len(self.github.calls)
        self.assertEqual(self.close(ctx)["outcome"], "not the closer's")
        self.assertEqual(self.record(ctx["task"])["stopped"], {"step": "ci", "merge_sha": merge})
        self.assertEqual([task["id"] for task in closer.candidates(self.conn)], [])
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual((self.kinds(), self.status(ctx["task"]), len(self.github.calls)),
                         (["close.stopped"], "awaiting_close", calls))
        # fleet close clears it as it clears any stop, and the green CI closes it then.
        self.assertEqual(closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 9300)["outcome"], "closed")

    def test_kill_while_an_unreadable_record_is_set_aside_never_leaves_it_unstopped(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        path = self.reviews(ctx["task"]) / "close.json"
        self.write_file(path, '{"task_id": "x", "judge": {"run_id": ')
        real = safefs.write_new

        def killed(fd, name, data, *args, **kwargs):
            if name == "close.json":
                raise Killed()
            return real(fd, name, data, *args, **kwargs)

        with mock.patch.object(safefs, "write_new", side_effect=killed), self.assertRaises(Killed):
            self.close(ctx)
        # The kill came after the stop event and before the stopped record: close.json was never missing.
        self.assertEqual(path.read_text(), '{"task_id": "x", "judge": {"run_id": ')
        self.assertEqual(self.kinds(), ["close.stopped"])
        self.assertEqual(self.close(ctx)["outcome"], "not the closer's")
        self.assertEqual((self.record(ctx["task"])["state"], self.kinds(), self.status(ctx["task"])),
                         ("stopped", ["close.stopped"], "awaiting_close"))
        aside = [name for name in os.listdir(self.reviews(ctx["task"])) if name.startswith("close.json.unreadable-")]
        self.assertEqual(len(aside), 2)  # each pass that met it kept its own link to the unreadable record

    def test_kill_during_the_worktree_add_before_its_record_is_taken_back(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        name = f"{ctx['task']}.merged-{merge[:12]}"
        real = gitops.write_record

        def killed(record):
            if record["name"] == name:
                raise Killed()
            return real(record)

        with mock.patch.object(gitops, "write_record", side_effect=killed):
            with self.assertRaises(Killed):
                self.close(ctx)
        self.assertTrue((self.castle / "worktrees" / name).exists())
        self.assertNotIn(f"{name}.json", os.listdir(self.office / "worktrees"))
        self.assertIn(name, self.git("worktree", "list"))
        self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertFalse((self.castle / "worktrees" / name).exists())
        self.assertNotIn(name, self.git("worktree", "list"))

    def test_kill_during_sandboxed_commands_runs_them_again_within_three_tries(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        runs = Counting(kill_at=(1,))
        with mock.patch.object(verify, "run_check", side_effect=runs):
            with self.assertRaises(Killed):
                self.close(ctx)
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual(len(runs.calls), 2)
        tries = sorted(name for name in os.listdir(self.reviews(ctx["task"])) if ".cmd-try" in name)
        self.assertEqual(tries, [f"close-{merge}.AC-2.cmd-try1", f"close-{merge}.AC-2.cmd-try2"])

    def test_kill_during_unsandboxed_commands_stops_and_never_runs_them_again(self):
        runs = Counting(kill_at=(2,))
        with mock.patch.object(verify, "run_check", side_effect=runs):
            ctx = self.passed_own(after=COMMAND_AC)
            self.push_main(ctx["sha"])
            with self.assertRaises(Killed):
                self.close(ctx)
            result = self.close(ctx)
            closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertEqual((result["outcome"], result["step"]), ("stopped", "commands"))
        self.assertEqual(len(runs.calls), 2)
        self.assertEqual(self.kinds(), ["close.stopped"])

    def test_kill_after_every_result_before_the_evidence_runs_no_command_again(self):
        for own in (False, True):
            with self.subTest(own=own):
                ctx = (self.passed_own(after=TWO_COMMANDS, branch="feat/results") if own
                       else self.passed_build(after=TWO_COMMANDS, branch="fix/results"))
                merge = self.merge_commit(ctx["sha"])
                self.push_main(merge)
                self.github.prs = []
                runs = Counting()
                with mock.patch.object(verify, "run_check", side_effect=runs):
                    with mock.patch.object(verify, "write_after_merge", side_effect=Killed()), \
                            self.assertRaises(Killed):
                        self.close(ctx)
                    self.assertEqual(self.close(ctx)["outcome"], "closed")
                self.assertEqual(len(runs.calls), 2)
                evidence = (self.reviews(ctx["task"]) / f"after-merge-evidence-{merge}.md").read_text()
                self.assertIn("AC-2 exit 0\nAC-4 exit 0\n", evidence)

    def test_kill_leaves_results_a_failed_read_never_takes_as_none(self):
        ctx = self.passed_own(after=TWO_COMMANDS)
        merge = self.merge_commit(ctx["sha"])
        self.push_main(merge)
        runs = Counting(then={1: self.opt_out})
        with mock.patch.object(verify, "run_check", side_effect=runs):
            self.assertEqual(self.close(ctx)["outcome"], "off")
            self.opt_in()
            results = self.reviews(ctx["task"]) / f"after-merge-results-{merge}.json"
            os.chmod(results, 0)
            result = self.close(ctx)
            self.assertEqual((result["outcome"], result["step"]), ("unknown", "commands"))
            os.chmod(results, 0o600)
            text = results.read_text()
            self.write_file(results, text.replace(ctx["sha"], "e" * 40))
            result = self.close(ctx)
            self.assertEqual((result["outcome"], result["step"]), ("stopped", "record"))
        self.assertEqual(len(runs.calls), 1)  # neither read ran your own session's first command again

    def test_kill_leaves_a_running_command_its_locks_so_no_pass_takes_back_or_reuses_its_worktree(self):
        ctx = self.passed_build(after=COMMAND_AC)
        merge = self.land_pr(ctx)
        name = f"{ctx['task']}.merged-{merge[:12]}"
        left = []

        def still_running(record, scratch, command, sandboxed=True, keep_fds=()):
            # The closer is killed while its command runs on, with what its process inherited.
            left.append(subprocess.Popen(["/bin/sleep", "60"], cwd=record["path"], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=keep_fds,
                                         start_new_session=True))
            raise Killed()

        with mock.patch.object(verify, "run_check", side_effect=still_running), self.assertRaises(Killed):
            self.close(ctx)
        self.addCleanup(lambda: [child.kill() or child.wait() for child in left])
        self.assertTrue(lock_busy(run_desk.task_lock_name(ctx["task"])))
        self.assertTrue(lock_busy(config.UPDATE_LOCK))  # and no CLI update replaces a binary under it
        result = self.close(ctx)
        self.assertEqual((result["outcome"], result["on"]), ("waiting", "review-loop"))
        self.assertTrue((self.castle / "worktrees" / name).exists())
        # Closed by hand meanwhile: housekeeping leaves the worktree to the command still running in it.
        pensieve.close_task(self.conn, ctx["task"], "complete", owlery.mint(self.conn, ctx["task"], "cli")["token"])
        closer.run_pass(self.conn, now=self.t0 + 9000)
        self.assertTrue((self.castle / "worktrees" / name).exists())
        left[0].kill()
        left[0].wait()
        closer.run_pass(self.conn, now=self.t0 + 9900)
        self.assertFalse((self.castle / "worktrees" / name).exists())
        self.assertEqual((self.kinds(), self.record(ctx["task"])["state"]), ([], "done"))

    def test_kill_after_the_evidence_before_the_record_never_reruns_a_command(self):
        for own in (False, True):
            with self.subTest(own=own):
                ctx = (self.passed_own(after=COMMAND_AC, branch="feat/evidence") if own
                       else self.passed_build(after=COMMAND_AC, branch="fix/evidence"))
                self.push_main(self.merge_commit(ctx["sha"]))
                self.github.prs = []
                real = closer.write_record

                def killed(record, **kwargs):
                    if record["commands"] is not None:
                        raise Killed()
                    return real(record, **kwargs)

                runs = Counting()
                with mock.patch.object(verify, "run_check", side_effect=runs):
                    with mock.patch.object(closer, "write_record", side_effect=killed):
                        with self.assertRaises(Killed):
                            self.close(ctx)
                    holder = self.castle / "tasks" / (ctx["task"] if own else ctx["parent"])
                    os.unlink(holder / "after-merge-evidence.md")  # as if the kill came before its castle copies
                    self.assertEqual(self.close(ctx)["outcome"], "closed")
                self.assertEqual(len(runs.calls), 1)
                self.assertTrue((holder / "after-merge-evidence.md").exists())

    def test_kill_after_the_judge_owl_before_the_record_acks_it_from_the_store(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)
        real = closer.write_record

        def killed(record, **kwargs):
            if record["judge"] is not None and record["judge"]["run_id"] is None:
                raise Killed()
            return real(record, **kwargs)

        with mock.patch.object(closer, "write_record", side_effect=killed):
            with self.assertRaises(Killed):
                self.close(ctx)
        [lost] = [owl for owl in owlery.inbox(self.conn, "hermione") if owl["sender"] == "map"]
        self.assertIsNone(self.record(ctx["task"])["judge"])
        with self.judge_says("PASS"):
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        mine = [owl for owl in owlery.inbox(self.conn, "hermione", include_acked=True) if owl["sender"] == "map"]
        self.assertEqual(len(mine), 2)
        self.assertTrue(all(owl["acked_at"] is not None for owl in mine))
        self.assertIn(lost["id"], [owl["id"] for owl in mine])
        audit = owlery.audit(self.conn, now=self.t0 + 30 * 86400)
        self.assertNotIn(lost["id"], [row["id"] for row in audit.get("escalate_owls", [])])

    def test_kill_after_the_run_before_the_office_copy_keeps_the_verdict_from_the_run_id(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        self.land_pr(ctx)
        with self.judge_says("PASS"), mock.patch.object(closer, "_keep_verdict", side_effect=Killed()):
            with self.assertRaises(Killed):
                self.close(ctx)
        with self.judge_says("PASS") as started:
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual((started.call_count, len(self.judged)), (0, 1))

    def test_kill_after_the_office_copy_publishes_without_a_second_run(self):
        ctx = self.passed_build(after=WRITTEN_AC)
        merge = self.land_pr(ctx)
        with self.judge_says("PASS"), mock.patch.object(closer, "_publish_verdict", side_effect=Killed()):
            with self.assertRaises(Killed):
                self.close(ctx)
        castle = self.castle / "tasks" / ctx["parent"] / f"after-merge-review-{merge[:12]}.md"
        self.assertFalse(castle.exists())
        with self.judge_says("PASS") as started:
            self.assertEqual(self.close(ctx)["outcome"], "closed")
        self.assertEqual((started.call_count, len(self.judged)), (0, 1))
        self.assertTrue(castle.read_text().startswith(f"AFTER-MERGE {ctx['task']} @ {merge}\n"))

    def test_kill_after_a_clear_marker_tells_the_next_stop_once(self):
        ctx = self.passed_build()
        merge = self.land_pr(ctx)
        self.github.checks[merge] = [check_run("build", "FAILURE")]
        self.assertEqual(self.close(ctx)["outcome"], "stopped")
        real = closer.write_record

        def killed(record, **kwargs):
            if record["state"] == "watching":
                raise Killed()
            return real(record, **kwargs)

        with mock.patch.object(closer, "write_record", side_effect=killed):
            with self.assertRaises(Killed):
                closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7300)
        self.assertEqual(self.record(ctx["task"])["state"], "stopped")
        self.assertEqual(closer.close_by_hand(self.conn, ctx["task"], now=self.t0 + 7400)["outcome"], "stopped")
        self.assertEqual(self.kinds(), ["close.stopped", "close.stopped"])
        keys = [row[0] for row in self.conn.execute("SELECT dedupe_key FROM events WHERE kind = 'close.stopped'")]
        self.assertEqual(keys, [f"close:stopped:{ctx['task']}:c0:{merge[:12]}:ci",
                                f"close:stopped:{ctx['task']}:c2:{merge[:12]}:ci"])
