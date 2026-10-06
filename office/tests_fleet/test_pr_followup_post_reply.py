"""PR follow-ups, the review loop side: the closer, the push, the replies and their rules.

Split from test_pr_followup_post.py so the parallel runner can run it beside the rest; it shares PostCase.
"""
from __future__ import annotations

from unittest import mock

from hogwarts import followups
from hogwarts.errors import StoreError

from fleet import closer, config, followup, gitops, push, run_desk
from fleet.safefs import FleetError
from tests_fleet.test_auto_push import EMAIL, TOKEN
from tests_fleet.test_pr_followup import NUMBER, PR_LINK, SETTLE, link
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import REPO_ID
from tests_fleet.test_pr_followup_post import FIXED_ROW, PUSHBACK_ROW, PostCase


class CloserPredicateTests(PostCase):
    def test_open_for_marks_a_task_the_closer_must_skip(self):
        row = self.ready()
        for state in ("pushing", "posting"):
            with self.subTest(state=state), mock.patch.object(followups, "open_for_task",
                                                              return_value={**row, "state": state}):
                self.assertTrue(followups.open_for(self.conn, self.task["id"]))
        self.assertTrue(followups.open_for(self.conn, self.task["id"]))
        with mock.patch.object(followups.db, "fetch_one", side_effect=StoreError("gone")), \
                self.assertRaises(StoreError):
            followups.open_for(self.conn, self.task["id"])
        self.hand_off(row, FIXED_ROW)
        self.assertFalse(followups.open_for(self.conn, self.task["id"]))


class CloserTests(PostCase):
    """The auto-close closer (fleet/closer.py) beside a real follow-up, routed by a Map round and passed by its own
    review, cut off by kills while it pushes and while it posts. test_auto_close.FollowupTests shows the closer closes
    such a task once its follow-up ends."""

    def assert_closer_skips(self, row: dict, state: str) -> None:
        self.assertEqual((self.row(row)["state"], self.status()), (state, "awaiting_close"))
        self.assertTrue(closer.followup_open(self.conn, self.task["id"]))
        self.assertNotIn(self.task["id"], [task["id"] for task in closer.candidates(self.conn)])
        with self.assertRaisesRegex(FleetError, "a follow-up is open on this task"):
            closer.close_by_hand(self.conn, self.task["id"])
        with mock.patch.object(followups.db, "fetch_one", side_effect=StoreError("database is locked")), \
                self.assertRaises(StoreError):
            closer.followup_open(self.conn, self.task["id"])  # unknown, never "none open"

    @staticmethod
    def killed_after(owner, name: str):
        real = getattr(owner, name)

        def killed(*args, **kwargs):
            real(*args, **kwargs)
            raise Killed(name)

        return mock.patch.object(owner, name, side_effect=killed)

    def test_the_closer_skips_a_task_with_an_open_followup(self):
        spawn = mock.patch.object(run_desk, "spawn_closer")  # the Map's sweep starts no real closer pass here
        self.spawned_closers = spawn.start()
        self.addCleanup(spawn.stop)
        self.write_file(self.office / config.AUTO_CLOSE_FILE, "on\n")
        row = self.ready()
        with self.killed_after(push, "push_followup"), self.assertRaises(Killed):
            self.hand_off(row, FIXED_ROW)
        self.assert_closer_skips(row, "pushing")
        with self.killed_after(gitops, "post_reply"), self.assertRaises(Killed):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posting")])
        self.assert_closer_skips(row, "posting")
        with mock.patch.object(gitops, "run_gh_write", side_effect=AssertionError("posted again")):
            self.next_pass()
            self.next_pass()
        self.assertEqual(self.row(row)["state"], "done")
        self.assertFalse(closer.followup_open(self.conn, self.task["id"]))
        # Now only what any task needs keeps it from the closer: this build was registered by hand, not by a go.
        self.assertEqual(closer.close_by_hand(self.conn, self.task["id"])["outcome"], "not the closer's")
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(self.spawned_closers.called)  # the Map's rounds started the closer while it was on


class PushTests(PostCase):
    def test_pass_pushes_exactly_the_reviewed_commit_fast_forward_to_the_pr_branch(self):
        row = self.ready()
        with mock.patch.object(gitops, "git", wraps=gitops.git) as git:
            self.hand_off(row, FIXED_ROW)
        head = self.head()
        pushes = [call.args[0] for call in git.call_args_list if call.args[0][:1] == ["push"]]
        self.assertEqual(pushes, [["push", "origin", f"{head}:refs/heads/fix/widget"]])
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {head}")
        self.assertEqual(self.git("rev-parse", f"{head}~1", cwd=self.bare), self.base_sha)
        self.assertEqual(self.row(row)["pass_sha"], head)

    def test_pass_pushes_through_every_push_check(self):
        row = self.ready()
        with mock.patch.object(push, "check", wraps=push.check) as checked:
            self.hand_off(row, FIXED_ROW)
        checked.assert_called_with(self.conn, self.task["id"])
        row2 = self.reroute_with_fleet_word()
        self.assertEqual(self.row(row2)["state"], "stopped")
        self.assertIn("a push check refused it", self.row(row2)["stop_reason"])

    def reroute_with_fleet_word(self) -> dict:
        self.add_comment(702, at=self.clock + 1)
        row = self.routed(self.clock + SETTLE + 20)
        self.writes.clear()
        self.hand_off(row, "T1 | FIXED | Done, see {sha}.", change="the hogwarts widget", round_no=3)
        return row

    def test_pass_never_opens_a_pr_even_with_draft_prs_on(self):
        row = self.ready()
        self.gh_calls.clear()
        self.hand_off(row, FIXED_ROW)
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual([event for event in self.new_events() if event["kind"] == "push.draft-pr"], [])

    def test_a_commit_that_does_not_build_on_the_pr_head_is_never_pushed(self):
        row = self.ready()
        with mock.patch.object(gitops, "is_ancestor", return_value=False):
            self.hand_off(row, FIXED_ROW)
        self.assert_one_stop("does not build on the PR's head")
        self.assert_nothing_went_out()

    def test_a_remote_that_moved_refuses_the_push_and_nothing_is_retried(self):
        row = self.ready()
        self.github.head = lambda: self.base_sha  # GitHub's view lags; the branch itself moved
        self.git("checkout", "-q", "--orphan", "elsewhere")
        self.write_file(self.repo / "other.txt", "other\n")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "other history")
        self.git("push", "-q", "--force", str(self.bare), "elsewhere:refs/heads/fix/widget")
        moved = self.remote()
        with mock.patch.object(push, "_push_exact", wraps=push._push_exact) as pushed:
            self.hand_off(row, FIXED_ROW)
        self.assertEqual(pushed.call_count, 1)
        self.assertEqual(self.remote(), moved)
        self.assert_one_stop("the push was refused, and nothing was retried")
        self.assertEqual(self.writes, [])

    def test_nothing_to_push_goes_straight_to_the_replies(self):
        row = self.ready()
        with mock.patch.object(push, "push_followup", side_effect=AssertionError("pushed")):
            self.hand_off(row, PUSHBACK_ROW, change=None)
        self.assertEqual(self.row(row)["state"], "done")
        self.assertEqual(self.row(row)["pass_sha"], self.base_sha)
        self.assertEqual(len(self.writes), 1)


class ReplyTests(PostCase):
    def test_replies_go_to_review_threads_and_as_pr_comments_for_reviews_and_comments(self):
        row = self.ready(review_body="Split this function please.", comment_body="Could this use the cache?")
        self.hand_off(row, "T1 | FIXED | Renamed it, see {sha}.\nT2 | PUSHBACK | It reads better as one step.\n"
                           "T3 | PUSHBACK | Not yet, the cache lands in the next change.")
        head = self.head()
        self.assertEqual([(argv[4], text) for argv, text in self.writes], [
            (f"repos/{REPO_ID}/pulls/{NUMBER}/comments/501/replies", f"Renamed it, see {head[:7]}."),
            (f"repos/{REPO_ID}/issues/{NUMBER}/comments", "> Split this function please.\n\nIt reads better as one step."),
            (f"repos/{REPO_ID}/issues/{NUMBER}/comments",
             "> Could this use the cache?\n\nNot yet, the cache lands in the next change."),
        ])
        self.assertEqual(self.replies(row), [("T1", "FIXED", "posted"), ("T2", "PUSHBACK", "posted"),
                                             ("T3", "PUSHBACK", "posted")])

    def test_a_quote_that_is_not_plain_and_safe_becomes_a_link(self):
        row = self.ready(thread=False, review_body="@alice see https://evil.example/x and ![i](https://evil.example)")
        self.hand_off(row, "T1 | PUSHBACK | It stays as it is.", change=None)
        [(argv, text)] = self.writes
        self.assertEqual(text, f"> {link('review', '601')}\n\nIt stays as it is.")

    def test_the_sha_placeholder_becomes_the_pushed_commit(self):
        row = self.ready()
        self.hand_off(row, "T1 | FIXED | Pushed the fix, see {sha}.")
        self.assertEqual(self.writes[0][1], f"Pushed the fix, see {self.head()[:7]}.")

    def test_the_ending_is_one_headmaster_event_naming_what_went_out(self):
        row = self.ready(comment_body="Please add a test.")
        self.hand_off(row, FIXED_ROW + "\nT2 | FIXED | Added one for the rename.")
        [event] = self.endings()
        self.assertEqual((event["kind"], event["verdict"]), ("followup.done", "headmaster"))
        self.assertIn(f"pushed {self.head()[:12]}", event["summary"])
        self.assertIn("posted 2 replies in your name", event["summary"])
        self.assertTrue(event["summary"].endswith(PR_LINK))

    def test_each_reply_is_posted_once_in_label_order(self):
        row = self.ready(review_body="Split it.", comment_body="Add a test.")
        self.hand_off(row, "T3 | FIXED | Added one.\nT1 | FIXED | Renamed, see {sha}.\nT2 | PUSHBACK | One step reads"
                           " better.")
        self.assertEqual([argv[4].split("/")[-2] if "replies" in argv[4] else argv[4] for argv, _ in self.writes],
                         ["501", f"repos/{REPO_ID}/issues/{NUMBER}/comments", f"repos/{REPO_ID}/issues/{NUMBER}/comments"])
        self.assertEqual([text.split("\n")[-1] for _, text in self.writes],
                         [f"Renamed, see {self.head()[:7]}.", "One step reads better.", "Added one."])
        self.next_pass()
        self.assertEqual(len(self.writes), 3)


class ReplyRuleTests(PostCase):
    RULES = (
        ("It is one step \u2014 not two.", "holds an em dash"),
        ("It is one step -- not two.", "holds an em dash"),
        ("It is one step,\u00a0not two.", "is not one line of printable ASCII"),
        ("x" * 401, "is longer than 400 characters"),
        ("Moved it, as hermione asked.", "holds a fleet word"),
        (f"Ask {EMAIL}.", "holds what looks like a credential or personal data"),
        (f"Use {TOKEN}.", "holds what looks like a credential or personal data"),
        ("Thanks @alice, done.", "mentions someone"),
        ("See https://evil.example/x for why.", "links outside this repo"),
        ("See <b>this</b>.", "holds markup"),
        ("See [the doc](x).", "holds markup"),
        ("Look: ![x](y).", "holds markup"),
        ("```code```", "holds markup"),
        ("# heading", "holds markup"),
        ("> quoted", "holds markup"),
        ("Same as owner/name#12.", "names another repository's issue, PR or commit"),
        ("Same as owner/name@abc1234.", "names another repository's issue, PR or commit"),
        ("Fixed in {sha}.", "opens with a Fixed in formula"),
        ("Addressed in abc1234 now.", "opens with a Fixed in formula"),
        ("Renamed {sha} and {sha}.", "has a stray brace or a second {sha}"),
        ("Renamed {it}.", "has a stray brace or a second {sha}"),
        (f"See https://github.com/{REPO_ID}/../../other/repo/issues/1 for why.", "links outside this repo"),
        (f"See https://github.com/{REPO_ID}/%2e%2e/%2E%2E/other/repo/issues/1 for why.", "links outside this repo"),
        (f"See https://github.com/{REPO_ID}/pull/7/%2f..%2fx for why.", "links outside this repo"),
        (f"See https://github.com/{REPO_ID}/./pull/7 for why.", "links outside this repo"),
        (f"See https://github.com/{REPO_ID}-evil/pull/7 for why.", "links outside this repo"),
        (f"See https://github.com:8443/{REPO_ID}/pull/7 for why.", "links outside this repo"),
        (f"See https://github.com.evil.example/{REPO_ID}/pull/7 for why.", "links outside this repo"),
        (f"See //github.com/{REPO_ID}/pull/7 for why.", "links outside this repo"),
        ("Use `run_desk` there.", "holds markup"),
        ("It is *one* step.", "holds markup"),
        ("It is _one_ step.", "holds markup"),
        ("It is __one__ step.", "holds markup"),
        ("It was ~~two~~ steps.", "holds markup"),
    )

    def test_each_reply_rule_refuses_before_anything_is_pushed(self):
        for reply, rule in self.RULES:
            with self.subTest(rule=rule, reply=reply[:20]):
                self.assertEqual(followup.reply_problem(reply, "FIXED", REPO_ID, pushed=True), rule)
        self.assertIsNone(followup.reply_problem(f"See https://github.com/{REPO_ID}/pull/7 for it, {{sha}}.", "FIXED",
                                                 REPO_ID, pushed=True))
        self.assertIsNone(followup.reply_problem(f"Like {REPO_ID}#3 did.", "PUSHBACK", REPO_ID, pushed=False))
        self.assertIsNone(followup.reply_problem(f"See https://github.com/{REPO_ID} and https://github.com/"
                                                 f"{REPO_ID.upper()}/pull/7#discussion_r5 on max_rounds.", "PUSHBACK",
                                                 REPO_ID, pushed=False))
        row = self.ready()
        self.hand_off(row, f"T1 | FIXED | Moved it, ask {EMAIL}, see {{sha}}.")
        event = self.assert_one_stop("T1 holds what looks like a credential or personal data")
        self.assertNotIn(EMAIL, event["summary"])
        self.assertNotIn("Moved it", event["summary"])
        self.assert_nothing_went_out()

    def test_fleet_words_pass_only_in_the_kit_repo(self):
        reply = "Moved it into the castle folder, as harry did."
        self.assertEqual(followup.reply_problem(reply, "PUSHBACK", REPO_ID, pushed=False), "holds a fleet word")
        with mock.patch.object(config, "FLEET_WORDS_ALLOWED_REPOS", (REPO_ID,)):
            self.assertIsNone(followup.reply_problem(reply, "PUSHBACK", REPO_ID, pushed=False))

    def test_a_handoff_that_does_not_mark_every_item_once_pushes_nothing(self):
        labels = ["T1", "T2"]
        fu_id = "fu_" + "a" * 16
        good = f"THREADS ({fu_id})\nT1 | FIXED | One.\nT2 | PUSHBACK | Two.\n"
        self.assertEqual(followup.parse_threads(good, fu_id, labels), {"T1": ("FIXED", "One."),
                                                                       "T2": ("PUSHBACK", "Two.")})
        bad = {
            "no THREADS section": "COMMIT MESSAGE\nx\n",
            "two THREADS sections": good + good,
            "an item this follow-up does not have": good + "T3 | FIXED | Three.\n",
            "T1 is marked twice": good + "T1 | FIXED | Again.\n",
            "T2 is not marked": f"THREADS ({fu_id})\nT1 | FIXED | One.\n",
            "label | mark | reply": f"THREADS ({fu_id})\nT1 FIXED One.\nT2 | PUSHBACK | Two.\n",
            "neither FIXED nor PUSHBACK": f"THREADS ({fu_id})\nT1 | HOLD | -\nT2 | PUSHBACK | Two.\n",
            "T1 has no reply": f"THREADS ({fu_id})\nT1 | FIXED | \nT2 | PUSHBACK | Two.\n",
        }
        for why, text in bad.items():
            with self.subTest(why=why), self.assertRaisesRegex(followup.ReplyRefused, why):
                followup.parse_threads(text, fu_id, labels)
        row = self.ready()
        self.hand_off(row, "T1 | HOLD | -")
        self.assert_one_stop("marked neither FIXED nor PUSHBACK")
        self.assert_nothing_went_out()
