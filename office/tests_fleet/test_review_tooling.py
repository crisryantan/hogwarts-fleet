"""Review tooling: the review script hands the reviewer its diff as a file, a review whose tooling failed reports as
BLOCKED-ON-TOOLING and is tried again, never as a HEADMASTER or CHANGES decision, a dead worktree is found before the
review launches with the command that rebuilds it, and a changed TASK.md reaches the next round.

Runs on real git repos in temp folders, with reviewer runs faked at run_desk.run as in test_review_chain.
"""
from __future__ import annotations

import contextlib
import hashlib
import shutil
from pathlib import Path
from unittest import mock

from hogwarts import capacity, owlery, pensieve
from tests.support import NOW

from fleet import config, gitops, owl_post, review, run_desk, tools, verify, worktree
from tests_fleet.test_review_chain import ChainCase, Killed
from tests_fleet.test_review_loop import LoopCase, TASK_MD, claude_stream

RUN_ID = "run-" + "b" * 16


def block(task_id: str, sha: str, last: str) -> str:
    return f"Notes first.\nREVIEW {task_id} @ {sha}\nAC\nAC-1 PASS | ok\nBLOCKING\nNON-BLOCKING\n{last}\n"


class ToolingMixin:
    @contextlib.contextmanager
    def reviewer(self, last: str = "VERDICT: PASS", exit_code: int = 0, output: bool = True):
        """run_desk.run as a reviewer whose block ends with last. Yields the request bodies it was handed."""
        bodies = []

        def run(conn, desk, owl_id, mcp_job=None, now=None, on_start=None, lock_held=False, keep_fds=()):
            if on_start is not None:
                on_start()
            bodies.append(owlery._owl(conn, owl_id)["body"])
            request = owlery.get_request(conn, owlery._owl(conn, owl_id)["request_id"])
            author = pensieve.get_task(conn, request["parent_task_id"])
            head = gitops.rev(gitops.find_record(worktree.castle_path(author["worktree"])))
            folder = self.office / "runs" / desk
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            text = block(author["id"], head, last) if output else "I could not start.\n"
            if desk in config.HEADLESS_CODEX:
                self.write_file(folder / f"{RUN_ID}-last-message.md", text)
            else:
                self.write_file(folder / f"{RUN_ID}.out", claude_stream(text))
            return {"desk": desk, "run_id": RUN_ID, "exit_code": exit_code}

        with mock.patch.object(run_desk, "run", side_effect=run):
            yield bodies

    def kinds(self) -> list:
        return [event["kind"] for event in self.events()]



class ReviewInputTests(ToolingMixin, ChainCase):
    def test_the_request_names_a_review_input_with_the_log_stat_and_whole_diff(self):
        with self.reviewer("VERDICT: CHANGES") as bodies:
            self.post(1, "widget one")
        [result] = self.reviews_run
        sha = result["review"]["sha"]
        path = Path(result["review"]["review_input"])
        self.assertEqual(path, self.castle / "tasks" / self.parent / f"review-input-{sha[:12]}.txt")
        self.assertIn(f"Review input, the log, stat and full diff above made by the review script: {path}", bodies[0])
        text = path.read_text()
        self.assertTrue(text.startswith(f"REVIEW INPUT {self.task['id']} @ {sha}\n"))
        for heading in ("LOG  git log", "STAT  git diff", "DIFF  git diff --no-ext-diff --no-textconv"):
            self.assertIn(heading, text)
        self.assertIn("Add the widget file", text)
        self.assertIn("+widget one", text)
        self.assertIn("BLOCKED-ON-TOOLING: <what failed>", bodies[0])

    def test_a_review_input_git_cannot_make_is_blocked_on_tooling_and_opens_no_counted_round(self):
        with mock.patch.object(review, "write_review_input",
                               side_effect=review.ToolingBlocked("the diff could not be read")), \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.post(1, "widget one")
        [result] = self.reviews_run
        self.assertIn("waiting: BLOCKED-ON-TOOLING on try 1 of 3: the diff could not be read", result["outcome"])
        self.assertEqual(self.rounds(), [])
        self.assertEqual(self.new_events(), [])


class BlockedOnToolingTests(ToolingMixin, ChainCase):
    def assert_blocked_then_reviewed(self, **reviewer) -> None:
        with self.reviewer(**reviewer):
            self.post(1, "widget one")
        [blocked] = self.reviews_run
        self.assertTrue(blocked["outcome"].startswith("waiting: BLOCKED-ON-TOOLING on try 1 of 3"), blocked)
        self.assertEqual(self.rounds(), [(1, None)])
        self.assertEqual(len(owl_post.unfinished_handoffs(self.task["id"])), 1)
        self.assertEqual(self.new_events(), [])
        with self.reviewer("VERDICT: CHANGES"):
            self.next_pass()
        self.assertEqual(self.reviews_run[-1]["outcome"], "reviewed: CHANGES")
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, self.task["id"])], [False, True])

    def test_a_declared_tooling_block_is_tried_again_never_told_as_a_decision(self):
        self.assert_blocked_then_reviewed(last="BLOCKED-ON-TOOLING: git diff was denied")

    def test_a_declared_tooling_block_stands_over_a_headmaster_verdict_in_the_same_block(self):
        self.assert_blocked_then_reviewed(last="BLOCKED-ON-TOOLING: git diff was denied\nVERDICT: HEADMASTER")
        self.assertNotIn("review.headmaster", self.kinds())

    def test_a_reviewer_run_that_exits_non_zero_is_blocked_on_tooling(self):
        self.assert_blocked_then_reviewed(exit_code=1)

    def test_a_reviewer_run_with_no_review_block_is_blocked_on_tooling(self):
        self.assert_blocked_then_reviewed(output=False)

    def test_a_reviewer_block_with_no_verdict_is_blocked_on_tooling(self):
        self.assert_blocked_then_reviewed(last="FOLLOW-UPS (not this PR)")

    def test_tooling_blocked_every_try_tells_ryan_blocked_on_tooling_once(self):
        with self.reviewer("BLOCKED-ON-TOOLING: Bash denied"):
            self.post(1, "widget one")
            for _ in range(config.AUTO_REVIEW_MAX_TRIES):
                self.next_pass()
        outcomes = [result["outcome"] for result in self.reviews_run]
        # The pass after the last try only marks that try's round as followed by nothing.
        self.assertEqual(outcomes[-1], "no handoff of this task waits for its review")
        self.assertTrue(outcomes[-2].startswith("BLOCKED-ON-TOOLING after 3 tries, so no reviewer judged it"),
                        outcomes)
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.blocked-on-tooling")
        self.assertIn("the reviewer declared it could not gather its evidence: Bash denied", event["summary"])
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.assertEqual([row["counts"] for row in capacity.review_rounds(self.conn, self.task["id"])],
                         [False] * config.AUTO_REVIEW_MAX_TRIES)
        self.assertNotIn("review.headmaster", self.kinds())

    def test_a_manual_review_names_the_tooling_block(self):
        self.write_file(self.wt / "widget.txt", "widget\n")
        self.stage(1)
        with mock.patch.object(run_desk, "spawn_review"):
            owl_post.run_pass(self.conn)
        with self.reviewer(exit_code=2), self.assertRaisesRegex(review.ToolingBlocked,
                                                                 "BLOCKED-ON-TOOLING: the hermione run did not"):
            review.review_build(self.conn, self.task["id"])


class DeadWorktreeTests(ToolingMixin, ChainCase):
    def entry(self) -> Path:
        return self.repo / ".git" / "worktrees" / self.task["id"]

    def blocked_events(self) -> list:
        return [event for event in self.new_events() if event["kind"] == "review.blocked-on-tooling"]

    def hook_marker(self) -> Path:
        """A repo hook that leaves a marker if it ever runs, as a repo-controlled post-checkout hook would."""
        hooks = self.home_dir / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = self.home_dir / "hook-ran"
        self.write_file(hooks / "post-checkout", f"#!/bin/sh\ntouch {marker}\n", mode=0o700)
        self.git("config", "core.hooksPath", str(hooks))
        return marker

    def assert_waits_then_reviews_once_rebuilt(self, kind: str, change_after: bool = False) -> None:
        marker = self.hook_marker()
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.post(1)
            self.next_pass()
        self.assertEqual([result["outcome"][:9] for result in self.reviews_run], ["waiting: "] * 2)
        # The second pass, while it was still gone, told Ryan nothing more and used up no try.
        [event] = self.blocked_events()
        self.assertIn(f"BLOCKED-ON-TOOLING: task {self.task['id']}'s review did not start; rebuild its worktree with:"
                      f" fleet worktree-rebuild {self.task['id']}; once it is back", event["summary"])
        self.assertEqual(self.rounds(), [])
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        rebuilt = tools.run(self.conn, tools.build_parser().parse_args(["worktree-rebuild", self.task["id"]]))
        self.assertEqual(rebuilt["rebuilt"], kind)
        self.assertFalse(marker.exists())
        if change_after:
            self.write_file(self.wt / "widget.txt", "widget\n")
        with self.reviewer("VERDICT: CHANGES"):
            self.next_pass()
        self.assertEqual(self.reviews_run[-1]["outcome"], "reviewed: CHANGES")
        self.assertEqual(self.rounds(), [(1, "CHANGES")])
        folder = self.office / "reviews" / self.task["id"]
        self.assertEqual(((folder / f"auto-{owl_id}.try1").exists(), (folder / f"auto-{owl_id}.try2").exists()),
                         (True, False))

    def test_a_worktree_whose_git_entry_is_gone_is_rebuilt_with_its_uncommitted_work(self):
        self.write_file(self.wt / "widget.txt", "pending widget\n")
        shutil.rmtree(self.entry())
        self.assert_waits_then_reviews_once_rebuilt("entry")
        self.assertEqual(self.git("show", "HEAD:widget.txt", cwd=self.wt), "pending widget")
        self.assertEqual(sorted(path.name for path in self.wt.parent.iterdir()), [self.task["id"]])

    def test_a_worktree_folder_that_is_gone_is_checked_out_again_with_no_repo_hook(self):
        shutil.rmtree(self.wt)
        self.assert_waits_then_reviews_once_rebuilt("folder", change_after=True)

    def test_a_manual_review_of_a_dead_worktree_names_the_rebuild_command_and_changes_nothing(self):
        shutil.rmtree(self.entry())
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")), \
                self.assertRaisesRegex(review.WorktreeGone,
                                       f"rebuild the worktree with: fleet worktree-rebuild {self.task['id']}"):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(self.rounds(), [])

    def killed_rebuild(self, point: str) -> None:
        """fleet worktree-rebuild of a folder whose entry is gone, killed at point: right after its marker, once the
        new entry is made, or once the files are back but before the index reset."""
        real_rename, real_git = worktree.os.rename, gitops.git

        def rename(src, dst, *args, **kwargs):
            if point == "marker" and src == str(self.wt):
                raise Killed(point)
            return real_rename(src, dst, *args, **kwargs)

        def replace(src, dst, *args, **kwargs):
            raise Killed(point)

        def git(args, *rest, **kwargs):
            if point == "reset" and args[0] == "reset":
                raise Killed(point)
            return real_git(args, *rest, **kwargs)

        patches = {"marker": mock.patch.object(worktree.os, "rename", side_effect=rename),
                   "entry": mock.patch.object(worktree.os, "replace", side_effect=replace),
                   "reset": mock.patch.object(gitops, "git", side_effect=git)}
        with patches[point], self.assertRaises(Killed):
            worktree.rebuild(self.conn, self.task["id"])

    def assert_a_killed_rebuild_blocks_reviews_until_the_next_one_finishes_it(self, point: str) -> None:
        self.write_file(self.wt / "widget.txt", "pending widget\n")
        shutil.rmtree(self.entry())
        self.killed_rebuild(point)
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")), \
                self.assertRaisesRegex(review.WorktreeGone, "a rebuild of the worktree .* was cut short"):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(self.rounds(), [])
        self.assertEqual(worktree.rebuild(self.conn, self.task["id"])["rebuilt"], "rebuilding")
        self.assertEqual((self.wt / "widget.txt").read_text(), "pending widget\n")
        self.assertEqual(sorted(path.name for path in self.wt.parent.iterdir()), [self.task["id"]])
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "?? widget.txt")
        self.assertIsNone(worktree.problem(gitops.find_record(str(self.wt))))

    def test_a_rebuild_killed_after_its_marker_is_finished_by_the_next_one(self):
        self.assert_a_killed_rebuild_blocks_reviews_until_the_next_one_finishes_it("marker")

    def test_a_rebuild_killed_after_its_new_entry_is_finished_by_the_next_one(self):
        self.assert_a_killed_rebuild_blocks_reviews_until_the_next_one_finishes_it("entry")

    def test_a_rebuild_killed_before_its_index_reset_is_finished_by_the_next_one(self):
        self.assert_a_killed_rebuild_blocks_reviews_until_the_next_one_finishes_it("reset")

    def test_a_worktree_git_reads_is_never_rebuilt(self):
        with self.assertRaisesRegex(review.FleetError, "nothing to rebuild"):
            worktree.rebuild(self.conn, self.task["id"])

    def test_a_dead_worktree_still_gone_after_the_wait_limit_gives_up_as_blocked_on_tooling(self):
        shutil.rmtree(self.entry())
        self.post(1)
        owl = review.latest_result_owl(self.conn, pensieve.get_task(self.conn, self.task["id"]))
        late = review.auto_review(self.conn, self.task["id"],
                                  now=owl["created_at"] + config.AUTO_REVIEW_WAIT_LIMIT_SECONDS)
        self.assertIn("waited 4 hours and gave up", late["outcome"])
        first, gave_up = self.blocked_events()
        self.assertIn("rebuild its worktree with", first["summary"])
        self.assertIn("gave up", gave_up["summary"])
        self.assertEqual(len(self.new_events()), 2)
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])


class ChangedIntentTests(ToolingMixin, ChainCase):
    def test_a_changed_task_md_reaches_the_next_round_and_its_record(self):
        with self.reviewer("VERDICT: CHANGES") as bodies:
            self.post(1, "widget one")
            task_md = self.castle / "tasks" / self.parent / "TASK.md"
            first_digest = hashlib.sha256(task_md.read_bytes()).hexdigest()
            new_text = TASK_MD.format(task_id=self.parent).replace("exists.", "exists and is not empty.")
            self.write_file(task_md, new_text)
            self.post(2, "widget two")
        second_digest = hashlib.sha256(new_text.encode()).hexdigest()
        self.assertIn(f"TASK.md for this round: sha256 {first_digest}", bodies[0])
        self.assertNotIn("TASK.md changed", bodies[0])
        self.assertIn(f"TASK.md for this round: sha256 {second_digest}", bodies[1])
        self.assertIn(f"TASK.md changed since round 1 (sha256 {first_digest})", bodies[1])
        first, second = capacity.review_rounds(self.conn, self.task["id"])
        self.assertEqual(review.round_inputs(self.task["id"], first["request_id"])["task_md_sha256"], first_digest)
        self.assertEqual(review.round_inputs(self.task["id"], second["request_id"])["task_md_sha256"], second_digest)
        self.assertEqual([result["review"]["task_md_changed"] for result in self.reviews_run], [False, True])


    def test_a_task_md_change_alone_opens_the_next_round_at_the_same_commit_and_handoff(self):
        with self.reviewer("VERDICT: CHANGES"):
            self.post(1, "widget one")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")), \
                self.assertRaises(review.Unchanged):
            review.review_build(self.conn, self.task["id"])
        task_md = self.castle / "tasks" / self.parent / "TASK.md"
        self.write_file(task_md, TASK_MD.format(task_id=self.parent).replace("exists.", "exists and is not empty."))
        with self.reviewer("VERDICT: PASS") as bodies:
            second = review.review_build(self.conn, self.task["id"])
        self.assertEqual((second["round"], second["verdict"], second["task_md_changed"]), (2, "PASS", True))
        self.assertIn("TASK.md changed since round 1", bodies[0])


class OwnIntentTests(ToolingMixin, LoopCase):
    def setUp(self) -> None:
        super().setUp()
        clock = mock.patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.enable("moody")

    def commit(self, text: str) -> None:
        self.write_file(self.repo / "fix.txt", text + "\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", text)

    def test_an_intent_file_on_a_fix_round_rewrites_task_md_and_moves_the_approval(self):
        self.commit("first try")
        with self.reviewer("VERDICT: CHANGES") as bodies:
            first = review.review_own(self.conn, str(self.repo), title="my own fix", intent="Do it.", fetch=False)
            self.commit("second try")
            second = review.review_own(self.conn, str(self.repo), task_id=first["task_id"],
                                       intent="Do it better.\nAC-1 it works | check: `true`\n", fetch=False)
        task_md = (self.castle / "tasks" / first["task_id"] / "TASK.md").read_bytes()
        self.assertIn(b"Do it better.", task_md)
        digest = hashlib.sha256(task_md).hexdigest()
        self.assertEqual((second["task_md_sha256"], second["task_md_changed"]), (digest, True))
        self.assertEqual(review.approved_digest(first["task_id"]), ("ok", digest))
        self.assertEqual((self.office / "reviews" / first["task_id"] / verify.frozen_name(digest)).read_bytes(),
                         task_md)
        self.assertIn("TASK.md changed since round 1", bodies[1])
        self.assertIn("AC-1", (self.castle / "tasks" / first["task_id"] / "evidence.md").read_text())

    def test_a_fix_round_without_an_intent_file_keeps_task_md_and_its_approval(self):
        self.commit("first try")
        with self.reviewer("VERDICT: CHANGES"):
            first = review.review_own(self.conn, str(self.repo), title="my own fix", intent="Do it.", fetch=False)
            before = review.approved_digest(first["task_id"])
            self.commit("second try")
            second = review.review_own(self.conn, str(self.repo), task_id=first["task_id"], fetch=False)
        self.assertEqual(review.approved_digest(first["task_id"]), before)
        self.assertEqual((second["task_md_sha256"], second["task_md_changed"]), (before[1], False))
