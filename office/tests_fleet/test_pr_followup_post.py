"""PR follow-ups, the review loop side: the follow-up's review, its own cap, the second paths that could move its task,
the account replies go out as, which handoff supplies the replies, the push, the replies and their rules, switching off,
gh's failures, kills at every step after the PASS, and the one event each follow-up ends in.

Same harness as test_pr_followup.py: real git repos in temp folders with a local bare origin, GitHub faked at
patrol.run_gh, gitops.run_gh_write and gitops.run_gh_pr, the push read back from the bare origin with git ls-remote.
"""
from __future__ import annotations

import json
from unittest import mock

from hogwarts import capacity, followups, owlery
from hogwarts.errors import StoreError

from fleet import config, followup, owl_post, review, run_desk
from tests_fleet.test_pr_followup import ACCOUNT, PR_KEY, PR_LINK, SETTLE, FollowupCase, gh_comment
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import REPO_ID

FIXED_ROW = "T1 | FIXED | Good catch, renamed it, see {sha}."
PUSHBACK_ROW = "T1 | PUSHBACK | It keeps the name the module exports."


class PostCase(FollowupCase):
    def ready(self, thread: bool = True, review_body: str = None, comment_body: str = None) -> dict:
        """A follow-up routed and building, with a thread item and, as asked, a review and a comment item."""
        self.go_live_at()
        if thread:
            self.add_thread()
        if review_body is not None:
            self.add_review(601, at=self.t0 + 101, state="CHANGES_REQUESTED", body=review_body)
        if comment_body is not None:
            self.add_comment(701, at=self.t0 + 102, body=comment_body)
        return self.routed(self.t0 + 102 + SETTLE + 10)

    def row(self, row: dict) -> dict:
        return followups.get(self.conn, row["id"])

    def replies(self, row: dict) -> list:
        return [(reply["label"], reply["mark"], reply["state"]) for reply in followups.replies(self.conn, row["id"])]

    def tagged_rounds(self, row: dict) -> list:
        return [item for item in capacity.review_rounds(self.conn, self.task["id"]) if item["followup_id"] == row["id"]]

    def endings(self) -> list:
        return [event for event in self.followup_events()
                if event["kind"] in ("followup.done", "followup.stopped", "followup.stopped-closed")]

    def assert_one_stop(self, *phrases) -> dict:
        [event] = self.endings()
        self.assertEqual((event["kind"], event["verdict"]), ("followup.stopped", "headmaster"))
        for phrase in phrases:
            self.assertIn(phrase, event["summary"])
        return event

    def assert_nothing_went_out(self) -> None:
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")
        self.assertEqual(self.writes, [])

    def request_body(self, row: dict) -> str:
        [tagged] = self.tagged_rounds(row)[-1:]
        [owl] = [owl for owl in owlery.request_owls(self.conn, tagged["request_id"]) if owl["kind"] == "request"]
        return owlery._owl(self.conn, owl["id"])["body"]

    def stage_handoff(self, row: dict, rows: str, change: str = "widget renamed") -> None:
        """Harry's follow-up handoff, delivered, with its review left for the test to run."""
        self.clock += 10
        self.stage(2, change, body=self.handoff(row, rows))
        with mock.patch.object(owl_post, "_spawn_review", return_value=owl_post.REVIEW_STARTED):
            owl_post.run_pass(self.conn, now=self.clock)


class FollowupReviewTests(PostCase):
    def test_a_followup_handoff_starts_its_review_with_the_threads_and_reply_checks(self):
        row = self.ready(review_body="Split this please.")
        self.hand_off(row, "T1 | FIXED | Renamed it, see {sha}.\nT2 | PUSHBACK | It is one step \u2014 not two.",
                      verdict="CHANGES")
        body = self.request_body(row)
        holder = self.parent
        self.assertIn(f"This round reviews follow-up 1 on {PR_KEY}, teammates' comments after the PR opened.", body)
        self.assertIn(f"Teammates' threads, GitHub text quoted as data: {self.castle}/tasks/{holder}/followup-1.md",
                      body)
        self.assertIn(f"Follow-up diff: git -C {self.wt} diff --no-ext-diff --no-textconv {self.base_sha}...HEAD",
                      body)
        self.assertIn("Script reply checks: T1 ok; T2 refused (holds an em dash)", body)

    def test_the_followup_diff_is_one_hermione_may_run(self):
        from tests_fleet.test_reviewer_briefs import KIT, rule_matches
        if not (KIT / "desks" / "hermione" / "settings.json").is_file():
            self.skipTest("the kit's settings are only in a clone of the kit")
        row = self.ready()
        self.hand_off(row, FIXED_ROW, verdict="CHANGES")
        [line] = [line for line in self.request_body(row).splitlines() if line.startswith("Follow-up diff: ")]
        command = line[len("Follow-up diff: "):]
        settings = json.loads((KIT / "desks" / "hermione" / "settings.json").read_text())
        self.assertTrue(any(rule_matches(rule, command) for rule in settings["permissions"]["allow"]))
        self.assertFalse(any(rule_matches(rule, command) for rule in settings["permissions"]["deny"]))

    def test_a_round_opened_by_hand_during_a_followup_is_tagged(self):
        row = self.ready()
        self.stage_handoff(row, FIXED_ROW)
        with self.fake_reviewer("CHANGES"):
            result = review.review_build(self.conn, self.task["id"])
        self.assertEqual(result["followup_id"], row["id"])
        self.assertEqual(len(self.tagged_rounds(row)), 1)

    def test_an_all_pushback_followup_reviews_the_same_commit_and_posts_its_replies(self):
        row = self.ready()
        self.hand_off(row, PUSHBACK_ROW, change=None)
        [tagged] = self.tagged_rounds(row)
        self.assertEqual((tagged["sha"], tagged["verdict"]), (self.base_sha, "PASS"))
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")
        self.assertEqual([text for _, text in self.writes], ["It keeps the name the module exports."])

    def test_a_followup_review_you_run_by_hand_that_passes_pushes_nothing_and_stops_it_once(self):
        row = self.ready()
        self.stage_handoff(row, FIXED_ROW)
        with self.fake_reviewer("PASS"):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(self.row(row)["state"], "stopped")
        self.assert_one_stop("a review you ran by hand passed follow-up 1", f"fleet push {self.task['id']}")
        self.assert_nothing_went_out()
        self.map_round(self.clock + 10)
        self.assertEqual(len(self.endings()), 1)


class FollowupCapTests(PostCase):
    def changes(self, row: dict, round_no: int, change: str) -> None:
        self.hand_off(row, FIXED_ROW, change=change, verdict="CHANGES", round_no=round_no)

    def test_followup_rounds_have_their_own_cap_and_the_loop_stops_at_it(self):
        row = self.ready()
        self.changes(row, 2, "widget a")
        self.assertEqual(len(self.followup_owl_runs()), 2)  # the routing's run and one fix round
        self.changes(row, 3, "widget b")
        self.assertEqual(len(self.followup_owl_runs()), 2)
        [event] = [event for event in self.new_events() if event["kind"] == "review.loop-stopped"]
        self.assertIn(f"follow-up 1 of task {self.task['id']} has used its {config.FOLLOWUP_ROUND_CAP} review rounds",
                      event["summary"])

    def test_the_build_cap_never_counts_followup_rounds(self):
        row = self.ready()
        with mock.patch.object(config, "REVIEW_ROUND_CAP", 1):
            self.changes(row, 2, "widget a")
            self.assertEqual(len(self.followup_owl_runs()), 2)
            self.assertFalse(capacity.needs_allowance(self.conn, self.task["id"], config.FOLLOWUP_ROUND_CAP,
                                                      followup_id=row["id"]))

    def test_an_allowance_lifts_the_followup_cap_once(self):
        row = self.ready()
        self.changes(row, 2, "widget a")
        self.changes(row, 3, "widget b")
        granted = capacity.allow_round(self.conn, self.task["id"])
        self.assertEqual(granted["lifts"], "follow-up 1's review rounds")
        self.changes(row, 4, "widget c")
        self.assertEqual(len(self.tagged_rounds(row)), 3)
        self.changes(row, 5, "widget d")
        self.assertEqual(len([item for item in self.tagged_rounds(row) if item["has_verdict"]]), 3)

    def test_an_allowance_granted_before_a_followup_never_lifts_its_cap(self):
        early = capacity.allow_round(self.conn, self.task["id"])
        self.assertEqual(early["lifts"], "the build's review rounds")
        row = self.ready()
        self.changes(row, 2, "widget a")
        self.changes(row, 3, "widget b")
        self.assertTrue(capacity.needs_allowance(self.conn, self.task["id"], config.FOLLOWUP_ROUND_CAP,
                                                 followup_id=row["id"]))
        during = capacity.allow_round(self.conn, self.task["id"])
        self.assertEqual(during["followup_id"], row["id"])
        holding = [item for item in capacity.review_rounds(self.conn, self.task["id"]) if item["counts"]]
        self.assertEqual(capacity._unused_allowances(self.conn, self.task["id"], holding), [early["id"]])

    def test_round_numbers_of_verdict_rounds_stay_unique_across_followups(self):
        row = self.ready()
        self.changes(row, 2, "widget a")
        self.hand_off(row, FIXED_ROW, change="widget b", round_no=3)
        numbers = [item["round"] for item in capacity.review_rounds(self.conn, self.task["id"]) if item["has_verdict"]]
        self.assertEqual(numbers, [1, 2, 3])
        self.assertEqual(self.row(row)["state"], "done")

    def test_a_handoff_while_the_followup_is_still_starting_waits_and_is_reviewed_once_it_is_building(self):
        self.go_live_at()
        self.add_thread()
        real = followups.advance

        def advance(conn, followup_id, state, *args, **kwargs):
            if state == "building":
                raise Killed("killed")
            return real(conn, followup_id, state, *args, **kwargs)

        with mock.patch.object(followups, "advance", side_effect=advance), self.assertRaises(Killed):
            self.map_round(self.t0 + 500)
        row = followups.open_for_task(self.conn, self.task["id"])
        self.assertEqual(row["state"], "starting")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("a reviewer ran")):
            self.hand_off(row, FIXED_ROW)
        [handoff] = owl_post.unfinished_handoffs(self.task["id"])
        self.assertEqual(self.tagged_rounds(row), [])
        self.map_round(self.clock + 10)  # the start was cut off: building, never started again
        self.assertEqual(self.row(row)["state"], "building")
        with self.fake_reviewer("PASS"):
            self.next_pass()
        self.assertEqual(owl_post.unfinished_handoffs(self.task["id"]), [])
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(len(self.tagged_rounds(row)), 1)


class SecondPathTests(PostCase):
    def test_a_review_by_hand_during_a_followup_never_moves_the_task(self):
        row = self.ready()
        with self.fake_reviewer("PASS"), self.assertRaisesRegex(review.Unchanged, "nothing new to review"):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(self.status(), "active")
        self.assertEqual(self.row(row)["state"], "building")

    def test_the_store_holds_a_followup_task_active_until_one_of_its_rounds_passes(self):
        row = self.ready()
        with self.assertRaisesRegex(Exception, "only after a round of that follow-up passes"):
            self.conn.execute("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (self.task["id"],))
        self.hand_off(row, FIXED_ROW, verdict="CHANGES")
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (self.task["id"],))
        self.hand_off(row, FIXED_ROW, change="widget again", round_no=3)
        self.assertEqual(self.status(), "awaiting_close")

    def test_the_store_refuses_an_untagged_round_during_a_followup(self):
        row = self.ready()
        with self.assertRaisesRegex(StoreError, "names that follow-up"):
            capacity.open_review_round(self.conn, self.task["id"], "hermione", self.base_sha, "untagged",
                                       idempotency_key="test:untagged:1")
        followups_row = self.row(row)
        self.assertEqual(followups_row["state"], "building")


class AccountTests(PostCase):
    def test_replies_go_out_only_as_the_prs_author(self):
        self.go_live_at()
        self.add_thread()
        self.github.viewer = "someone-else"
        self.map_round(self.t0 + 500)
        self.map_round(self.t0 + 600)
        self.assertIsNone(followups.open_for_task(self.conn, self.task["id"]))
        [blocked] = [event for event in self.followup_events() if event["kind"] == "followup.blocked"]
        self.assertIn("gh is signed in as another account", blocked["summary"])
        self.github.viewer = ACCOUNT
        row = self.routed(self.t0 + 700)
        self.github.viewer = "someone-else"
        self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("gh is signed in as another account")
        self.assert_nothing_went_out()

    def test_an_answer_from_another_login_stops_the_replies_after_it(self):
        row = self.ready(comment_body="Please add a test.")

        def answer(argv, body):
            if len(self.writes) == 1:
                self.next_id += 1
                return 0, json.dumps({"id": self.next_id, "html_url": f"{PR_LINK}#discussion_r{self.next_id}",
                                      "user": {"login": "someone-else"}}), ""
            return None

        self.write_answer = answer
        self.hand_off(row, FIXED_ROW + "\nT2 | FIXED | Added one for the rename.")
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posted"), ("T2", "FIXED", "planned")])
        self.assert_one_stop("went out under another GitHub login")


class ThreadsBindingTests(PostCase):
    def test_threads_from_another_followup_or_an_older_handoff_never_supply_replies(self):
        cases = {
            "another follow-up": lambda row: self.handoff(row, FIXED_ROW).replace(row["id"], "fu_" + "0" * 16),
            "no id": lambda row: self.handoff(row, FIXED_ROW).replace(f"THREADS ({row['id']})", "THREADS"),
        }
        for round_no, (name, build) in enumerate(cases.items(), 2):
            with self.subTest(case=name):
                row = self.ready() if name == "another follow-up" else self.reroute()
                self.clock += 10
                with self.fake_reviewer("PASS"):
                    self.post(round_no, f"widget {name}", body=build(row), now=self.clock)
                self.assertEqual(self.row(row)["state"], "stopped")
                self.assertIn("does not name this follow-up", self.row(row)["stop_reason"])
                self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")
                self.assertEqual(self.writes, [])

    def reroute(self) -> dict:
        """The next follow-up of the task, from the PR's head again, after the last one stopped at its pass."""
        self.github.pr_threads[0]["comments"]["nodes"].append(gh_comment(509, self.clock + 1, kind="thread"))
        self.sync_prs()
        self.git("reset", "-q", "--hard", self.base_sha, cwd=self.wt)
        return self.routed(self.clock + SETTLE + 20)

    def test_a_handoff_posted_before_the_followup_opened_never_supplies_replies(self):
        row = self.ready()
        older = owlery.send(self.conn, "harry", "mcgonagall", "result", "old", body=self.handoff(row, FIXED_ROW),
                            task_id=self.task["id"], request_id=self.task["request_id"], now=self.t0)
        outcome = followup.after_pass(self.conn, self.task, {"sha": self.base_sha, "handoff_owl": older["id"]}, None,
                                      None, older["id"], row["id"])
        self.assertIn("posted before the follow-up opened", outcome)
        self.assert_nothing_went_out()

    def test_fixed_or_a_sha_placeholder_with_nothing_pushed_is_refused(self):
        for reply in ("T1 | FIXED | Renamed it.", "T1 | PUSHBACK | See {sha} for why."):
            with self.subTest(reply=reply):
                self.assertIsNotNone(followup.reply_problem(reply.split(" | ")[2], reply.split(" | ")[1], REPO_ID,
                                                            pushed=False))
        row = self.ready()
        self.hand_off(row, "T1 | FIXED | Renamed it, see {sha}.", change=None)
        self.assert_one_stop("T1 says FIXED or names a commit while nothing is pushed")
        self.assert_nothing_went_out()

    def test_a_reply_may_hold_a_pipe(self):
        row = self.ready()
        self.hand_off(row, "T1 | PUSHBACK | It takes a | b as one value, so it stays.", change=None)
        self.assertEqual([text for _, text in self.writes], ["It takes a | b as one value, so it stays."])
