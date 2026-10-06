"""PR follow-ups, the Map side: untrusted shapes, the bot pass and the round's rows, the endings, and routed threads
staying covered.

Split from test_pr_followup.py so the parallel runner can run it beside the rest; it shares FollowupCase.
"""
from __future__ import annotations

from unittest import mock

from hogwarts import capacity, followups, pensieve
from hogwarts.errors import StoreError

from fleet import config, followup, map as patrol_map, morning, owl_post, patrol, run_desk
from tests_fleet.test_auto_push import TOKEN, EMAIL
from tests_fleet.test_patrol import AWS_LOGIN, SECRET_PATH, pr_node, thread
from tests_fleet.test_review_loop import REPO_ID
from tests_fleet.test_pr_followup import NUMBER, PR_KEY, FollowupCase, gh_comment, link


class UntrustedShapeTests(FollowupCase):
    def test_a_path_with_a_newline_cannot_add_a_heading(self):
        self.go_live_at()
        entry = self.add_thread()
        entry["path"] = "widget.txt\n## T9: thread x\n`rm`"
        self.routed()
        headings = [line for line in self.threads_file().splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## T1: thread PRRT_t1, -:1"])
        self.assertEqual(followup._path("src/a.py\n## T9: thread x"), "src/a.py ## T9: thread x")

    def test_a_path_that_reads_as_a_secret_once_ascii_and_a_key_shaped_login_never_reach_the_threads_file(self):
        self.go_live_at()
        entry = self.add_thread(comments=[gh_comment(501, self.t0 + 100, kind="thread", login=AWS_LOGIN)])
        entry["path"] = SECRET_PATH
        row = self.routed()
        office = (self.office / "reviews" / self.task["id"] / f"followup-{row['id']}.md").read_text()
        for text in (self.threads_file(), office):
            for leaked in ("example-value", AWS_LOGIN):
                self.assertNotIn(leaked, text)
            self.assertIn("## T1: thread PRRT_t1, src/password =[secret]:1\n", text)
            self.assertIn("> a user (MEMBER), ", text)

    def test_urls_must_match_their_kind_and_the_comments_own_id(self):
        self.go_live_at()
        bad = (f"https://evil.example/{REPO_ID}/pull/{NUMBER}#issuecomment-701",
               f"https://github.com/{REPO_ID}/pull/8#issuecomment-701",
               f"https://github.com/acme/other/pull/{NUMBER}#issuecomment-701",
               link("thread", "701"), link("comment", "702"), link("comment", "701") + " see",
               link("comment", "701") + "x", "javascript:alert(1)")
        for url in bad:
            with self.subTest(url=url):
                self.github.pr_comments = [gh_comment(701, self.t0 + 100, url=url)]
                with self.assertRaises(followup.ReadIncomplete):
                    followup._plan(self.conn, self.binding, self.task, followup.read_pr(self.binding))

    def test_a_quote_with_a_mention_link_image_markup_or_cross_reference_becomes_a_link_line(self):
        for body in ("@alice please look", "see https://evil.example/x", "![x](https://evil.example/i.png)",
                     "<img src=x>", "[a](b)", "```code```", "other/repo#12 broke it", "other/repo@abcdef1 broke it",
                     f"ask {EMAIL}", f"the key {TOKEN}", "hogwarts words", "a dash \u2014 here", "a -- b",
                     "caf\u00e9 time", "word" * 40):
            with self.subTest(body=body[:20]):
                self.assertIsNone(followup.make_quote(body, REPO_ID))
        self.assertEqual(followup.make_quote("> > Please split this.\nmore", REPO_ID), "Please split this.")
        quote = followup.make_quote(("word " * 30).strip(), REPO_ID)
        self.assertLessEqual(len(quote), config.FOLLOWUP_QUOTE_MAX)
        self.assertTrue(quote.endswith("word"))
        self.go_live_at()
        self.add_review(601, state="CHANGES_REQUESTED", body="@alice see https://evil.example")
        row = self.routed()
        [item] = followups.items(self.conn, row["id"])
        self.assertIsNone(item["quote"])


    def test_a_quote_is_taken_only_from_a_comment_scrubbed_whole_before_any_cut(self):
        # A quoted password with spaces, longer than a quote: cut first, its closing quote would be lost and the
        # credential's start would read as plain text.
        secret = "correct horse battery staple " * 6
        body = f'Please move it out: password = "{secret.strip()}" and rename the widget.'
        self.assertGreater(len(body), config.FOLLOWUP_QUOTE_MAX)
        self.assertIsNone(followup.make_quote(body, REPO_ID))
        self.assertIsNone(followup.make_quote(f"-----BEGIN RSA PRIVATE KEY-----\nMIIB{TOKEN}\n", REPO_ID))
        # Something scrub would change after the first line leaves the quote alone.
        self.assertEqual(followup.make_quote(f"Please rename it.\nmail {EMAIL}", REPO_ID), "Please rename it.")
        self.go_live_at()
        self.add_review(601, state="CHANGES_REQUESTED", body=body)
        row = self.routed()
        [item] = followups.items(self.conn, row["id"])
        self.assertIsNone(item["quote"])
        self.assertNotIn("correct horse", self.threads_file())

    def test_a_quote_with_any_link_or_inline_markup_becomes_a_link_line(self):
        for body in (f"See https://github.com/{REPO_ID}/pull/{NUMBER} for why.", f"Same as github.com/{REPO_ID} at"
                     f" https://github.com/{REPO_ID}/", "Use `run_desk` here", "This is *wrong*", "This is _wrong_",
                     "This is __wrong__", "This was ~~wrong~~", "This was ~wrong~"):
            with self.subTest(body=body):
                self.assertIsNone(followup.make_quote(body, REPO_ID))
        self.assertEqual(followup.make_quote("Rename max_rounds please.", REPO_ID), "Rename max_rounds please.")


class PatrolTests(FollowupCase):
    BOT_AT = 2000  # past BOT_PASS_DELAY_SECONDS after the PR opened

    def threads_answer(self) -> None:
        """The bot pass's own read of the PR's threads, in the threads query's shape."""
        nodes = []
        for entry in self.github.pr_threads:
            comments = [{"author": item["author"], "body": item["body"], "createdAt": item["createdAt"],
                         "url": item["url"], "diffHunk": item.get("diffHunk")} for item in entry["comments"]["nodes"]]
            nodes.append({"id": entry["id"], "isResolved": entry["isResolved"], "isOutdated": False,
                          "path": entry["path"], "line": entry["line"], "comments": {"nodes": comments}})
        self.github.threads[PR_KEY] = nodes

    def with_bot_thread(self) -> None:
        self.add_thread("PRRT_bot", [gh_comment(551, self.t0 + 90, kind="thread", login="lint-bot", typename="Bot")])
        self.github.prs = [self.pr_node((thread("PRRT_t1", by="alice"), thread("PRRT_bot", by="lint-bot",
                                                                                  person=False)))]
        self.threads_answer()

    def bot_marks(self) -> list:
        return [kwargs["marks"] for desk, job, _, kwargs in self.wakes if job == "bot-pass"]

    def test_bot_pass_skips_only_threads_a_followup_routed(self):
        self.go_live_at()
        self.add_thread()
        self.with_bot_thread()
        self.routed()
        self.map_round(self.t0 + self.BOT_AT)
        self.assertEqual(self.bot_marks(), [["PRRT_bot"]])

    def test_bot_pass_skips_nothing_while_followups_are_off(self):
        self.go_live_at()
        self.add_thread()
        self.with_bot_thread()
        self.switch_off()
        self.map_round(self.t0 + self.BOT_AT)
        self.assertEqual([sorted(marks) for marks in self.bot_marks()], [["PRRT_bot", "PRRT_t1"]])

    def test_a_store_failure_skips_the_bot_pass_for_that_pr_only(self):
        self.go_live_at()
        self.add_thread()
        self.with_bot_thread()
        other = pr_node(number=8, repo=REPO_ID, created=self.t0, threads=(thread("PRRT_eight", by="lint-bot",
                                                                                   person=False),))
        self.github.prs.append(other)
        self.github.threads[f"{REPO_ID}#8"] = [{"id": "PRRT_eight", "isResolved": False, "isOutdated": False,
                                                "path": "a.txt", "line": 1, "comments": {"nodes": []}}]
        with mock.patch.object(followups, "routed_thread_ids", side_effect=StoreError("the store is gone")):
            self.map_round(self.t0 + self.BOT_AT)
        subjects = [kwargs["subject"] for desk, job, _, kwargs in self.wakes if job == "bot-pass"]
        self.assertEqual(subjects, [f"{REPO_ID}#8"])

    def test_a_persons_thread_a_followup_covers_is_a_routine_row(self):
        self.go_live_at()
        self.add_thread()
        self.routed()
        [row] = [row for row in patrol.read_rows("map", "outcomes.jsonl")
                 if row["change"] == "review thread from a person"]
        self.assertEqual(row["mark"], "routine")
        self.assertIn("the follow-up takes it", row["detail"])
        self.assertEqual([event for event in self.events() if event["kind"] == "patrol.for-me"], [])

    def test_rows_the_followup_cannot_cover_stay_for_me(self):
        self.go_live_at()
        self.add_thread("PRRT_t1")
        self.add_thread("PRRT_c", [gh_comment(511, self.t0 + 100, kind="thread", login="dave",
                                              association="CONTRIBUTOR")])
        self.github.prs = [self.pr_node((thread("PRRT_t1", by="alice"), thread("PRRT_c", by="dave")))]
        self.github.prs[0]["reviewDecision"] = "CHANGES_REQUESTED"
        self.routed()
        rows = {row["change"]: row["mark"] for row in patrol.read_rows("map", "outcomes.jsonl")}
        self.assertEqual(rows["review thread from a person"], "for-me")
        self.assertEqual(rows["changes requested"], "for-me")
        person = {"pr": PR_KEY, "change": "review thread from a person", "detail": "1 new", "mark": "for-me"}
        seen = {"prs": {PR_KEY: {"human_threads": ["PRRT_t1"], "open_threads": ["PRRT_t1"]}}}
        self.assertEqual(followup.mark_covered([person], {"prs": {}}, seen, {"covered": {PR_KEY: None}}), [person])
        self.assertEqual(followup.mark_covered([person], {"prs": {}}, seen, {"covered": {}}), [person])
        self.assertEqual(followup.mark_covered([person], {"prs": {}}, seen, {"covered": {PR_KEY: {"PRRT_t1"}}})[0]["mark"],
                         "routine")

    def test_followup_rows_are_routine_and_raise_no_second_event(self):
        self.go_live_at()
        self.add_comment(701)
        self.routed()
        rows = [row for row in patrol.read_rows("map", "outcomes.jsonl") if row["change"].startswith("follow-up")]
        self.assertEqual([(row["change"], row["mark"]) for row in rows], [("follow-up routed", "routine")])
        self.assertEqual([event for event in self.events() if event["kind"].startswith("patrol.")], [])

    def test_the_lineup_and_round_file_show_followups_from_the_store(self):
        self.go_live_at()
        self.add_comment(701)
        row = self.routed()
        text = followup.lineup_text(self.conn, self.clock)
        self.assertIn(PR_KEY, text)
        self.assertIn(f"| {self.task['id']} | 1 | building | 0 of {config.FOLLOWUP_ROUND_CAP} | 1 | 0 of 0 | - |",
                      text)
        seen = patrol.fetch_prs()
        self.assertIn("## Follow-ups\n\n" + text, patrol_map.render_round([], seen, self.clock, text))
        lineup = morning.render(seen, [], [], None, self.clock, self.clock - 86400, text)
        self.assertIn("## Follow-ups\n\n" + text, lineup)
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")

    def test_a_lineup_that_cannot_read_followups_says_so(self):
        with mock.patch.object(followups, "list_followups", side_effect=StoreError("the store is gone")):
            text = followup.lineup_text(self.conn, self.t0)
        self.assertTrue(text.startswith("Follow-ups: not read ("))
        self.assertNotIn("|", text)

    def test_a_followup_error_never_stops_the_map_round(self):
        self.go_live_at()
        self.add_thread()
        with mock.patch.object(followup, "_consider", side_effect=KeyError("a bug")):
            row = self.map_round(self.t0 + 500)
        self.assertTrue(row["ok"])
        self.assertEqual(row["followups"]["errors"], 1)
        person = [r for r in patrol.read_rows("map", "outcomes.jsonl") if r["change"] == "review thread from a person"]
        self.assertEqual([r["mark"] for r in person], ["for-me"])  # nothing the follow-up could not judge is taken
        self.assertEqual(patrol.read_state("map", "snapshot.json", None)["taken_at"], self.t0 + 500)

    def test_the_digest_and_the_board_show_the_open_followup(self):
        from fleet.hooks import session_start

        self.go_live_at()
        self.add_comment(701)
        self.routed()
        lines = session_start._inflight(self.conn, "mcgonagall", self.clock)
        [line] = [line for line in lines if line.startswith(f"- {self.task['id']}")]
        self.assertIn(f"follow-up 1, round 0 of {config.FOLLOWUP_ROUND_CAP}, {PR_KEY}", line)
        board = capacity.in_flight(self.conn, self.clock, config.RUNNING_WINDOW_SECONDS, None, config.REVIEW_ROUND_CAP,
                                   config.FOLLOWUP_ROUND_CAP)
        [task] = [task for desk in board["desks"] for task in desk["tasks"] if task["id"] == self.task["id"]]
        self.assertEqual(task["followup"]["max_rounds"], config.FOLLOWUP_ROUND_CAP)

    def test_a_round_that_started_harry_counts_as_a_model_round(self):
        self.go_live_at()
        self.add_comment(701)
        row = self.map_round(self.t0 + 500)
        self.assertTrue(row["model"])
        self.assertEqual(row["followups"], {"live": True, "routed": 1, "errors": 0})
        quiet = self.map_round(self.t0 + 600)
        self.assertFalse(quiet["model"])


class EndingTests(FollowupCase):
    def building(self) -> dict:
        self.go_live_at()
        self.add_comment(701)
        self.add_comment(702, at=self.t0 + 101)
        return self.routed()

    def test_a_closed_task_ends_its_followup_with_one_routine_event(self):
        row = self.building()
        self.switch_off()
        pensieve.close_task(self.conn, self.task["id"], "abandoned")
        with run_desk.task_lock(self.task["id"]):  # a review still finishing its own step: the next round ends it
            self.map_round(self.clock + 10)
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")
        self.map_round(self.clock + 10)
        self.map_round(self.clock + 10)
        row = followups.get(self.conn, row["id"])
        self.assertEqual(row["state"], "stopped")
        [event] = [event for event in self.followup_events() if event["kind"].startswith("followup.stop")]
        self.assertEqual((event["kind"], event["verdict"]), ("followup.stopped-closed", "routine"))
        self.assertIn("0 replies were posted, 0 may or may not have been, 0 were not", event["summary"])

    def test_a_closed_task_with_a_reply_cut_off_mid_post_ends_it_unknown_and_tells_you(self):
        row = self.building()
        task_id = self.task["id"]
        rounds = capacity.open_review_round(self.conn, task_id, "hermione", self.base_sha, "r", followup_id=row["id"],
                                            followup_max_rounds=2, idempotency_key="test:round:2")
        pensieve.start_task(self.conn, rounds["task"]["id"])
        capacity.record_round_verdict(self.conn, rounds["request"]["id"], REPO_ID, "PASS")
        pensieve.mark_awaiting_close(self.conn, task_id)
        followups.plan_replies(self.conn, row["id"], self.base_sha,
                               [{"label": "T1", "mark": "PUSHBACK", "body": "No."},
                                {"label": "T2", "mark": "PUSHBACK", "body": "Not here."}], False)
        followups.begin_reply(self.conn, row["id"], "T1")
        pensieve.close_task(self.conn, task_id, "abandoned")
        self.map_round(self.clock + 10)
        states = [reply["state"] for reply in followups.replies(self.conn, row["id"])]
        self.assertEqual(states, ["unknown", "planned"])
        [event] = [event for event in self.followup_events() if event["kind"].startswith("followup.stop")]
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn("0 replies were posted, 1 may or may not have been, 1 were not", event["summary"])
        self.assertEqual(self.writes, [])

    def test_a_followup_with_nothing_going_for_hours_raises_one_headmaster_event(self):
        row = self.building()
        later = self.clock + config.FOLLOWUP_STALL_SECONDS + 10
        self.map_round(self.clock + 60)
        self.assertEqual([e for e in self.followup_events() if e["kind"] == "followup.stalled"], [])
        # Harry running, a handoff waiting, or a headmaster event told since: no stall event.
        capacity.record_launch(self.conn, "harry", "run-going", "model-x", task_id=self.task["id"], now=later - 60)
        self.map_round(later)
        capacity.record_launch_usage(self.conn, "run-going", 1, 1, 0, 0.1, 10, now=later - 30)
        owl_post.claim_handoff(self.task["id"], "owl_" + "2" * 16)
        self.map_round(later + 10 + config.FOLLOWUP_STALL_SECONDS)
        owl_post.finish_handoff(self.task["id"], "owl_" + "2" * 16, "done")
        pensieve.add_event(self.conn, "harry", "review.round-cap", "headmaster", "capped", task_id=self.task["id"],
                           now=later + 20 + config.FOLLOWUP_STALL_SECONDS)
        self.map_round(later + 40 + config.FOLLOWUP_STALL_SECONDS)
        self.assertEqual([e for e in self.followup_events() if e["kind"] == "followup.stalled"], [])
        # Something new happens after that event (a run of Harry's ends with no handoff), then nothing for hours.
        capacity.record_launch(self.conn, "harry", "run-later", "model-x", task_id=self.task["id"],
                               now=later + 50 + config.FOLLOWUP_STALL_SECONDS)
        capacity.record_launch_usage(self.conn, "run-later", 1, 1, 0, 0.1, 10,
                                     now=later + 55 + config.FOLLOWUP_STALL_SECONDS)
        final = later + 100 + 2 * config.FOLLOWUP_STALL_SECONDS
        self.map_round(final)
        self.map_round(final + 10)
        [event] = [e for e in self.followup_events() if e["kind"] == "followup.stalled"]
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn(f"fleet build {self.task['id']}", event["summary"])
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")


class RoutedThreadsStayCoveredTests(FollowupCase):
    """Threads a follow-up took never go to Hermione's bot pass while it may still be open, whatever the switch, shadow
    mode or a failed step says in a later round; a round that stops before it could load them runs no bot pass."""

    def routed_thread(self) -> None:
        self.go_live_at()
        self.add_thread()
        self.assertIsNotNone(self.routed())

    def covered(self, at: int) -> dict:
        return followup.patrol_round(self.conn, patrol.fetch_prs(), at, at, patrol.shadow_on(), False)["covered"]

    def test_switched_off_after_routing_the_thread_stays_covered(self):
        self.routed_thread()
        self.switch_off()
        self.assertEqual(self.covered(self.clock + 60), {PR_KEY: {"PRRT_t1"}})

    def test_in_shadow_mode_after_routing_the_thread_stays_covered(self):
        self.routed_thread()
        self.shadow()
        self.assertEqual(self.covered(self.clock + 60), {PR_KEY: {"PRRT_t1"}})

    def test_a_live_period_that_cannot_be_recorded_still_covers_the_thread(self):
        self.routed_thread()
        with mock.patch.object(followups, "see_live", side_effect=StoreError("the store is locked")):
            self.assertEqual(self.covered(self.clock + 60), {PR_KEY: {"PRRT_t1"}})

    def test_a_round_that_stops_before_loading_them_leaves_every_pr_unknown(self):
        self.routed_thread()
        with mock.patch.object(followup, "_covered_routed", side_effect=RuntimeError("cut")):
            self.assertEqual(self.covered(self.clock + 60), {PR_KEY: None})

    def test_the_bot_pass_never_gets_a_routed_thread_after_the_switch_goes_off(self):
        self.routed_thread()
        self.switch_off()
        self.enable("hermione")
        with mock.patch.object(patrol, "wake", side_effect=AssertionError("a bot pass ran")) as wake:
            self.map_round(self.clock + config.BOT_PASS_DELAY_SECONDS + 60)
        wake.assert_not_called()
