"""PR follow-ups, the review loop side: switching off, and kills at every step after the PASS.

Split from test_pr_followup_post.py so the parallel runner can run it beside the rest; it shares PostCase.
"""
from __future__ import annotations

from unittest import mock

from hogwarts import followups, pensieve

from fleet import config, followup, gitops, owl_post, push, review, run_desk
from tests_fleet.test_pr_followup import SETTLE, gh_comment
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_pr_followup_post import FIXED_ROW, PUSHBACK_ROW, PostCase


class SwitchOffTests(PostCase):
    def test_switching_off_before_the_push_pushes_and_posts_nothing(self):
        row = self.ready()
        self.switch_off()
        self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("pr-followup is off")
        self.assert_nothing_went_out()

    def test_shadow_mode_before_the_push_pushes_and_posts_nothing(self):
        row = self.ready()
        self.shadow()
        self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("the patrol is in shadow mode")
        self.assert_nothing_went_out()

    def test_switching_off_after_the_replies_are_planned_pushes_nothing(self):
        row = self.ready()
        real = followups.plan_replies

        def plan(*args, **kwargs):
            planned = real(*args, **kwargs)
            self.switch_off()
            return planned

        with mock.patch.object(followups, "plan_replies", side_effect=plan):
            self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("before the push", "pr-followup is off")
        self.assert_nothing_went_out()
        self.assertEqual(self.replies(row), [("T1", "FIXED", "planned")])

    def test_switching_off_or_closing_the_task_between_replies_posts_no_more(self):
        for how in ("off", "closed"):
            with self.subTest(how=how):
                row = self.ready(comment_body="Please add a test.") if how == "off" else self.second()

                def answer(argv, body, how=how):
                    if how == "off":
                        self.switch_off()
                    else:
                        pensieve.close_task(self.conn, self.task["id"], "abandoned")
                    return None

                self.write_answer = answer
                self.writes.clear()
                self.hand_off(row, FIXED_ROW + "\nT2 | FIXED | Added one for the rename.",
                              change="widget renamed" if how == "off" else "widget renamed again",
                              round_no=2 if how == "off" else 3)
                self.write_answer = None
                self.assertEqual(len(self.writes), 1)
                self.assertEqual([state for _, _, state in self.replies(row)], ["posted", "planned"])
                stop = [event for event in self.endings() if event["kind"] == "followup.stopped"][-1]
                self.assertIn("between replies", stop["summary"])
                self.assertIn("1 replies went out", stop["summary"])
                self.switch_on()

    def second(self) -> dict:
        self.add_comment(702, at=self.clock + 1)
        self.github.pr_threads[0]["comments"]["nodes"].append(gh_comment(510, self.clock + 1, kind="thread"))
        self.sync_prs()
        return self.routed(self.clock + SETTLE + 20)

    def test_a_task_closed_after_its_pass_is_never_pushed(self):
        row = self.ready()
        real = followup.build_replies

        def build(*args, **kwargs):
            pensieve.close_task(self.conn, self.task["id"], "abandoned")
            return real(*args, **kwargs)

        with mock.patch.object(followup, "build_replies", side_effect=build):
            self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("the task is no longer awaiting close")
        self.assert_nothing_went_out()

    def test_a_closed_pr_or_moved_head_pushes_nothing_with_one_headmaster_event(self):
        for how in ("closed", "moved"):
            with self.subTest(how=how):
                row = self.ready() if how == "closed" else self.second()
                if how == "closed":
                    self.github.state = "MERGED"
                else:
                    self.github.head = lambda: "e" * 40
                self.hand_off(row, FIXED_ROW + "\nT2 | FIXED | Added one." if how == "moved" else FIXED_ROW,
                              round_no=2 if how == "closed" else 3)
                stop = [event for event in self.endings() if event["kind"] == "followup.stopped"][-1]
                self.assertEqual(stop["verdict"], "headmaster")
                self.assertIn("no longer open" if how == "closed" else "did not build", stop["summary"])
                self.assertEqual(self.writes, [])
                self.github.state = "OPEN"
                self.github.head = lambda: self.git("rev-parse", "refs/heads/fix/widget", cwd=self.bare)
                self.git("reset", "-q", "--hard", self.base_sha, cwd=self.wt)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")

    def test_a_failed_pr_read_at_pass_pushes_nothing_and_is_not_retried(self):
        row = self.ready()
        reads = self.github.followup_reads
        self.github.fail = "gh"
        self.hand_off(row, FIXED_ROW)
        self.assertEqual(self.github.followup_reads, reads + 1)
        self.assert_one_stop("the PR could not be read, and nothing is retried")
        self.assert_nothing_went_out()
        self.next_pass()
        self.assertEqual(self.github.followup_reads, reads + 1)


class KillTests(PostCase):
    def killed_at(self, row: dict, owner, name: str, after: bool = False, rows: str = FIXED_ROW,
                  change: str = "widget renamed", round_no: int = 2) -> None:
        """The automatic review of Harry's handoff, killed when it reaches owner.name, or just after it ran."""
        real = getattr(owner, name)

        def killed(*args, **kwargs):
            if after:
                real(*args, **kwargs)
            raise Killed(name)

        with mock.patch.object(owner, name, side_effect=killed), self.assertRaises(Killed):
            self.hand_off(row, rows, change=change, round_no=round_no)

    def test_killed_before_the_push_began_finishes_once_on_the_next_pass(self):
        row = self.ready()
        self.killed_at(row, followups, "plan_replies")
        self.assertEqual(self.row(row)["state"], "building")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.head()}")
        self.assertEqual(len(self.writes), 1)

    def test_killed_while_pushing_continues_only_when_the_remote_branch_shows_the_push(self):
        row = self.ready()
        self.github.head = lambda: self.base_sha  # GitHub's PR head lags the push; the read-back is the branch itself
        self.killed_at(row, push, "push_followup", after=True)
        self.assertEqual(self.row(row)["state"], "pushing")
        self.github.head = lambda: self.git("rev-parse", "refs/heads/fix/widget", cwd=self.bare)
        with mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(len(self.writes), 1)

    def test_killed_while_pushing_a_push_that_did_not_land_is_never_repeated(self):
        row = self.ready()
        self.killed_at(row, push, "push_followup")
        with mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")):
            self.next_pass()
            self.next_pass()
        self.assert_one_stop("the push was cut off and did not land; nothing was pushed again")
        self.assert_nothing_went_out()

    def test_a_pushback_only_followup_killed_after_its_verdict_posts_once_and_ends_in_one_event(self):
        for where in ("finish_handoff", "after_pass"):
            with self.subTest(killed=where):
                row = self.ready() if where == "finish_handoff" else self.second()
                self.writes.clear()
                owner = owl_post if where == "finish_handoff" else followup
                self.assertTrue(any(event["dedupe_key"].startswith(f"push:draft-pr:{self.task['id']}:{self.base_sha}")
                                    for event in self.all_events()))
                self.killed_at(row, owner, where, rows=PUSHBACK_ROW, change=None,
                               round_no=2 if where == "finish_handoff" else 3)
                self.next_pass()
                self.next_pass()
                self.assertEqual(self.row(row)["state"], "done")
                self.assertEqual([text for _, text in self.writes], ["It keeps the name the module exports."])
                self.assertEqual(len([event for event in self.endings() if event["kind"] == "followup.done"]),
                                 1 if where == "finish_handoff" else 2)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")

    def second(self) -> dict:
        self.github.pr_threads[0]["comments"]["nodes"].append(gh_comment(510, self.clock + 1, kind="thread"))
        self.sync_prs()
        return self.routed(self.clock + SETTLE + 20)

    def all_events(self) -> list:
        return [dict(row) for row in self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()]

    def test_killed_while_posting_finds_the_reply_by_reading_back_and_never_posts_twice(self):
        row = self.ready()
        self.killed_at(row, gitops, "post_reply", after=True)
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posting")])
        posted = self.github.pr_threads[0]["comments"]["nodes"][-1]["fullDatabaseId"]
        with mock.patch.object(gitops, "run_gh_write", side_effect=AssertionError("posted again")):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posted")])
        self.assertEqual(followups.replies(self.conn, row["id"])[0]["posted_id"], posted)
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(len(self.writes), 1)

    def test_a_comment_another_reply_already_holds_is_not_taken_by_the_read_back(self):
        row = self.ready()
        self.killed_at(row, gitops, "post_reply", after=True)
        posted = self.github.pr_threads[0]["comments"]["nodes"][-1]["fullDatabaseId"]
        with mock.patch.object(followups, "posted_ids", return_value={posted}), \
                mock.patch.object(gitops, "run_gh_write", side_effect=AssertionError("posted again")):
            self.next_pass()
        self.assertEqual(self.replies(row), [("T1", "FIXED", "unknown")])
        self.assert_one_stop("cut off and is not on the PR")

    def test_killed_while_posting_a_reply_that_never_landed_is_never_posted_again(self):
        row = self.ready()
        self.killed_at(row, gitops, "post_reply")
        with mock.patch.object(gitops, "run_gh_write", side_effect=AssertionError("posted again")):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.replies(row), [("T1", "FIXED", "unknown")])
        self.assert_one_stop("reply T1", "nothing was posted again")
        self.assertEqual(self.writes, [])

    def test_a_failed_read_back_is_unknown_and_tried_again_until_its_limit(self):
        row = self.ready()
        self.killed_at(row, gitops, "post_reply")
        self.github.fail = "gh"
        updated = self.row(row)["updated_at"]
        review.auto_review(self.conn, self.task["id"], now=updated + 60)
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posting")])
        self.assertEqual(self.endings(), [])
        review.auto_review(self.conn, self.task["id"], now=updated + config.FOLLOWUP_RECONCILE_LIMIT_SECONDS + 1)
        self.assertEqual(self.replies(row), [("T1", "FIXED", "unknown")])
        self.assert_one_stop("could not be read to say whether it is on the PR")
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_killed_after_the_last_reply_ends_with_one_event(self):
        row = self.ready()
        real = followups.advance

        def advance(conn, followup_id, state, *args, **kwargs):
            if state == "done":
                raise Killed("killed")
            return real(conn, followup_id, state, *args, **kwargs)

        with mock.patch.object(followups, "advance", side_effect=advance), self.assertRaises(Killed):
            self.hand_off(row, FIXED_ROW)
        self.assertEqual((self.row(row)["state"], self.endings()), ("posting", []))
        self.next_pass()
        self.next_pass()
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(len(self.endings()), 1)
        self.assertEqual(len(self.writes), 1)

    def test_an_ending_already_told_is_left_as_told(self):
        row = self.ready()
        real = owl_post.write_after

        def write_after(task_id, request_id, owl_id, state, step=None):
            if state == "done":
                raise Killed("killed")
            return real(task_id, request_id, owl_id, state, step)

        with mock.patch.object(owl_post, "write_after", side_effect=write_after), self.assertRaises(Killed):
            self.hand_off(row, FIXED_ROW)
        self.assertEqual(self.row(row)["state"], "done")
        self.next_pass()
        self.next_pass()
        self.assertEqual(len(self.endings()), 1)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])
