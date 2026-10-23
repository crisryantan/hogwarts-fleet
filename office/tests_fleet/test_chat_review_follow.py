"""Following a PR from a chat review: after an app posts a review of a PR from Chat it follows that PR, and reviews it
again in the same thread whenever its head moves, until it is merged or closed, goes quiet, or a teammate says stop.
gh, Chat and every turn are fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the machine and no model
runs."""
from __future__ import annotations

import json
import time
from unittest import mock

from fleet import chat_review, config
from fleet.safefs import FleetError
from tests_fleet.test_chat_review import (HEAD, LEAD, LEAD_USER, MENTION, MOODY_APP_USER, NEWER, SENDER,
                                          SENDER_USER, SPACE, THREAD, URL, ChatCase, block, caps_used, msg)

THIRD = "c" * 40
FOURTH = "d" * 40
LINK = "<https://mail.google.com/chat/u/0/#chat/space/AAAA/t1|thread>"


class FollowCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 20)
        self.patch(config, "CHAT_REVIEW_FOLLOW_CHECK_SECONDS", 0)  # every pass checks, but where a test says otherwise
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)

    def reviewed(self, **kwargs) -> None:
        self.deliver(msg(at=time.time() - 5, **kwargs))
        self.run_pass()

    def push(self, sha: str, verdict: str = "CHANGES") -> None:
        """New commits on the PR: its head moves, and the desk's next review is of that head."""
        self.pr["head"]["sha"] = sha
        self.answer = ("ok", block(sha=sha, verdict=verdict, blocking=() if verdict == "PASS" else
                                   ("x.py:2 | prints at import | remove it",)))

    def follow(self, app: str = "hermione") -> dict:
        return chat_review.read_follow(chat_review._follow_key(app, "acme/web-app", 7))

    def edit_follow(self, app: str = "hermione", **fields) -> None:
        key = chat_review._follow_key(app, "acme/web-app", 7)
        chat_review._save_follow(key, {**chat_review.read_follow(key), **fields})

    def notes(self) -> list:
        return [MENTION.sub("", note["text"], count=1) for note in self.google.notes]

    def events(self) -> list:
        return [payload["event"] for _, payload in self.writes]

    def follow_records(self) -> list:
        return [record for record in self.records() if record.get("source") == "follow"]


class FollowGrammarTests(FollowCase):
    def test_the_follow_command_grammar(self):
        for text in (f"follow {URL}", f"watch <{URL}>", f"please follow PR {URL}", f"Follow {URL}.",
                     f"can you watch this {URL}", f"follow <{URL}|the PR>"):
            with self.subTest(text=text):
                self.assertEqual(chat_review.parse_follow_command(text), ("acme/web-app", 7))
        for text in (f"review {URL}", f"follow {URL} {URL}", f"follow {URL} https://example.org", URL,
                     f"follow the build {URL}", "follow https://github.com/acme/other/pull/7", "follow", "", None):
            with self.subTest(text=text):
                self.assertIsNone(chat_review.parse_follow_command(text))

    def test_the_stop_following_grammar(self):
        for text in ("stop following", "Unfollow", "stop watching this PR", "ok, stop following it please",
                     "unfollow!"):
            with self.subTest(text=text):
                self.assertTrue(chat_review.parse_unfollow(text))
                self.assertTrue(chat_review.unmentioned_asks(text))
                self.assertFalse(chat_review.parse_conversation(text))
        for text in ("stop following me around, the build is red again today", f"unfollow {URL}", "following",
                     "stop", "unfollow https://example.org", "", None):
            with self.subTest(text=text):
                self.assertFalse(chat_review.parse_unfollow(text))


    def test_a_bad_follow_setting_refuses_to_start_and_names_it(self):
        for name, value in (("CHAT_REVIEW_FOLLOW_DAYS", 0), ("CHAT_REVIEW_FOLLOW_REVIEWS_PER_DAY", "6"),
                            ("CHAT_REVIEW_FOLLOW_CHECK_SECONDS", -1)):
            with self.subTest(name=name), mock.patch.object(config, name, value):
                with self.assertRaises(FleetError) as caught:
                    self.run_pass()
                self.assertIn(name, str(caught.exception))
        self.assertEqual(self.google.calls, [])


class FollowTests(FollowCase):
    def test_a_review_from_chat_starts_a_follow_with_no_email_in_it(self):
        self.reviewed()
        follow = self.follow()
        self.assertEqual((follow["app"], follow["repo"], follow["number"], follow["sha"], follow["space"],
                          follow["thread"], follow["requester_user"], follow["requester_name"], follow["checked"]),
                         ("hermione", "acme/web-app", 7, HEAD, SPACE, THREAD, SENDER_USER, "Dev Person", None))
        self.assertNotIn(SENDER, json.dumps(follow))
        self.assertIn("followed", [line["event"] for line in self.log_lines()])

    def test_an_unmoved_head_does_nothing(self):
        self.reviewed()
        self.assertEqual(self.run_pass()["followed"], ["unchanged"])
        self.assertEqual(self.run_pass()["followed"], ["unchanged"])
        self.assertEqual((len(self.turns), len(self.google.acks), len(self.writes), self.notes()), (1, 1, 1, []))

    def test_a_follow_is_not_checked_before_its_interval(self):
        self.patch(config, "CHAT_REVIEW_FOLLOW_CHECK_SECONDS", 900)
        self.reviewed()
        self.push(NEWER)
        reads = self.reads.count("pr")
        self.assertEqual(self.run_pass()["followed"], [])
        self.assertEqual((self.reads.count("pr"), len(self.turns)), (reads, 1))
        self.edit_follow(since=int(time.time()) - 901)
        self.assertEqual(self.run_pass()["followed"], ["queued"])

    def test_a_moved_head_is_reviewed_once_in_the_thread_and_posted_once(self):
        self.reviewed()
        self.push(NEWER)
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual(len(self.turns), 2)
        ack = self.google.acks[-1]
        self.assertEqual((MENTION.sub("", ack["text"], count=1), ack["thread"], ack["text"][:len(SENDER_USER) + 2]),
                         ("New commits on #7 (bbbbbbb). Reviewing again.", THREAD, f"<{SENDER_USER}>"))
        self.assertEqual(self.google.messages[-1]["thread"], THREAD)
        self.assertIn(f"Verdict: CHANGES on acme/web-app#7 at {NEWER[:12]}", self.google.messages[-1]["text"])
        self.assertEqual(self.events(), ["REQUEST_CHANGES", "REQUEST_CHANGES"])  # one post for each head
        [record] = self.follow_records()
        self.assertEqual((record["state"], record["outcome"], record["sha"], record["github"]),
                         ("done", "reviewed", NEWER, "posted"))
        self.assertTrue(self.writes[-1][1]["body"].startswith(
            "This is an automated review by Hermione (Staff Engineer), requested by Dev Person in Chat."))
        self.assertEqual(self.follow()["sha"], NEWER)
        for _ in range(2):
            self.assertEqual(self.run_pass()["followed"], ["unchanged"])
        self.assertEqual((len(self.turns), len(self.google.acks), len(self.writes)), (2, 2, 2))

    def test_a_clean_pass_after_changes_approves_only_through_the_switches(self):
        self.reviewed()
        self.push(NEWER, "PASS")
        self.run_pass()
        self.switch(config.CHAT_REVIEW_APPROVE_FILE, True)
        self.push(THIRD, "PASS")
        self.run_pass()
        self.assertEqual(self.events(), ["REQUEST_CHANGES", "COMMENT", "APPROVE"])

    def test_merged_and_closed_each_end_the_follow_with_one_line(self):
        for merged, line in ((True, "#7 was merged; I've stopped following it."),
                             (False, "#7 was closed; I've stopped following it.")):
            with self.subTest(merged=merged):
                self.google.notes.clear()
                self.pr.update({"state": "open", "merged": False})
                self.reviewed(message=f"m-{merged}")
                self.assertIsNotNone(self.follow())
                self.pr.update({"state": "closed", "merged": merged})
                self.assertEqual(self.run_pass()["followed"], ["merged" if merged else "closed"])
                self.assertEqual(self.notes(), [line])
                self.assertEqual(self.google.notes[0]["thread"], THREAD)
                self.assertIsNone(self.follow())
                self.assertEqual(self.run_pass()["followed"], [])
                self.assertEqual(len(self.google.notes), 1)

    def test_a_follow_with_no_new_commits_for_its_days_ends_with_one_line(self):
        self.reviewed()
        self.edit_follow(moved=int(time.time()) - config.CHAT_REVIEW_FOLLOW_DAYS * 86400 - 1)
        self.assertEqual(self.run_pass()["followed"], ["quiet"])
        self.assertEqual(self.notes(), ["No new commits on #7 for 7 days; I've stopped following it."])
        self.assertIsNone(self.follow())

    def test_new_commits_keep_a_follow_from_going_quiet(self):
        self.reviewed()
        self.edit_follow(moved=int(time.time()) - config.CHAT_REVIEW_FOLLOW_DAYS * 86400 - 1)
        self.push(NEWER)
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual(self.notes(), [])
        self.assertIsNotNone(self.follow())

    def test_the_per_day_limit_waits_for_the_next_day_and_says_so_once(self):
        self.patch(config, "CHAT_REVIEW_FOLLOW_REVIEWS_PER_DAY", 1)
        self.reviewed()
        self.push(NEWER)
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.push(THIRD)
        self.assertEqual(self.run_pass()["followed"], ["limited"])
        self.push(FOURTH)
        self.assertEqual(self.run_pass()["followed"], ["limited"])
        self.assertEqual(self.notes(), ["That's today's limit of reviews for #7 (1). I'll look at its new commits"
                                        " again tomorrow."])
        self.assertEqual(len(self.turns), 2)
        for key, record in chat_review._records():  # a day on, the day's review is yesterday's
            if record.get("source") == "follow":
                chat_review._save(key, {**record, "at": record["at"] - 86400})
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual(len(self.turns), 3)
        self.assertEqual(len(self.google.notes), 1)

    def test_a_later_review_in_another_thread_moves_the_follow_there(self):
        self.reviewed()
        other = f"{SPACE}/threads/t2"
        self.deliver(msg(sender=LEAD_USER, message="m2", thread=other, name="Lead Person"))
        self.run_pass()
        follow = self.follow()
        self.assertEqual((follow["thread"], follow["requester_user"], follow["requester_name"]),
                         (other, LEAD_USER, "Lead Person"))
        self.push(NEWER)
        self.run_pass()
        self.assertEqual((self.google.acks[-1]["thread"], self.google.messages[-1]["thread"]), (other, other))
        self.assertTrue(self.google.acks[-1]["text"].startswith(f"<{LEAD_USER}> New commits on #7"))

    def test_a_failed_review_never_starts_a_follow(self):
        self.answer = ("failed", "")
        self.reviewed()
        self.assertIsNone(self.follow())
        self.pr["state"] = "closed"
        self.reviewed(message="m2")
        self.assertIsNone(self.follow())


class FollowCommandTests(FollowCase):
    def test_stop_following_ends_the_follow_with_one_line(self):
        self.reviewed()
        self.deliver(msg(text="stop following", message="m2"))
        self.assertIn("unfollow", self.run_pass()["taken"])
        self.assertEqual(self.texts()[-1], "Stopped following #7.")
        self.assertIsNone(self.follow())
        self.push(NEWER)
        self.assertEqual(self.run_pass()["followed"], [])
        self.assertEqual(len(self.turns), 1)

    def test_an_unmentioned_stop_in_its_own_thread_ends_it_and_elsewhere_is_dropped(self):
        self.reviewed()
        self.deliver(msg(text="unfollow", message="q1", mention=False, at=time.time() + 1))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], ["unfollow"])
        self.assertEqual(self.texts()[-1], "Stopped following #7.")
        self.assertIsNone(self.follow())
        replies = len(self.google.messages)
        self.deliver(msg(text="stop watching", message="q2", mention=False, at=time.time() + 2))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], [])
        self.assertEqual(len(self.google.messages), replies)  # nothing followed there: no reply, no record
        self.assertFalse([record for record in self.records() if record["message"].endswith("/q2")])

    def test_a_mentioned_stop_with_nothing_followed_says_so(self):
        self.deliver(msg(text="stop following"))
        self.assertEqual(self.run_pass()["taken"], ["unfollow"])
        self.assertEqual(self.texts(), [chat_review.NOT_FOLLOWING])

    def test_a_stop_also_ends_its_review_still_queued(self):
        self.reviewed()
        self.push(NEWER)
        with chat_review.pr_lock("acme/web-app", 7, "hermione"):  # the review of new commits waits
            self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.deliver(msg(text="stop following", message="m2"))
        self.run_pass()
        [record] = self.follow_records()
        self.assertEqual((record["state"], record["outcome"]), ("done", "unfollowed"))
        self.assertEqual(len(self.turns), 1)

    def test_follow_with_a_link_starts_a_follow_whose_first_check_reviews_it(self):
        self.patch(config, "CHAT_REVIEW_FOLLOW_CHECK_SECONDS", 900)
        self.deliver(msg(text=f"follow {URL}"))
        self.assertEqual(self.run_pass()["taken"], ["follow"])
        self.assertEqual((self.texts(), self.turns),
                         (["Following #7. I'll review it whenever it gets new commits."], []))
        follow = self.follow()
        self.assertEqual((follow["sha"], follow["thread"], follow["requester_user"]), (None, THREAD, SENDER_USER))
        self.edit_follow(since=int(time.time()) - 901)
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(MENTION.sub("", self.google.acks[-1]["text"], count=1),
                         "New commits on #7 (aaaaaaa). Reviewing again.")
        self.assertEqual(self.follow()["sha"], HEAD)

    def test_status_lists_the_followed_prs(self):
        self.reviewed()
        now = int(time.time())
        lines = chat_review.status_text("hermione", now).splitlines()
        self.assertEqual(lines[-2:], ["Following (1):",
                                      f"- acme/web-app#7: last reviewed {HEAD[:12]}, not checked yet {LINK}"])
        self.run_pass()
        self.assertEqual(chat_review.status_text("hermione", now).splitlines()[-1],
                         f"- acme/web-app#7: last reviewed {HEAD[:12]}, checked just now {LINK}")
        self.assertNotIn("Following", chat_review.status_text("moody", now))


class FollowLimitTests(FollowCase):
    def test_a_follows_review_counts_toward_the_fleet_cap_but_not_the_persons(self):
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 1)
        self.reviewed()
        self.assertEqual(caps_used(), (1, 1))
        self.push(NEWER)
        self.run_pass()
        self.assertEqual(len(self.turns), 2)
        self.assertEqual(caps_used(), (2, 1))
        self.patch(config, "CHAT_REVIEW_DAILY", 2)
        self.push(THIRD)
        self.run_pass()
        self.assertEqual(len(self.turns), 2)
        self.assertEqual(self.texts()[-1], chat_review.CAPPED_FLEET)

    def test_a_follows_review_counts_toward_the_desks_own_caps(self):
        self.patch(config, "DAILY_RUN_CAP", {**config.DAILY_RUN_CAP, "hermione": 1})
        self.reviewed()
        self.push(NEWER)
        self.run_pass()
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.follow_records()[0]["outcome"], "desk-capped")
        self.assertEqual(self.run_pass()["followed"], ["unchanged"])  # that head is never queued again

    def test_the_intake_off_checks_nothing_and_a_family_stop_holds_its_follows(self):
        self.reviewed()
        self.push(NEWER)
        reads = self.reads.count("pr")
        self.switch(config.CHAT_REVIEW_FILE, False)
        self.assertEqual(self.run_pass(), {"state": "off"})
        self.assertEqual(self.reads.count("pr"), reads)
        self.switch(config.CHAT_REVIEW_FILE, True)
        stop = self.office / "state" / config.STOP_FAMILY_FILES["claude"]
        self.write_file(stop, "1 a new claude version\n")
        self.assertEqual(self.run_pass()["followed"], ["waiting"])
        self.assertEqual((self.reads.count("pr"), len(self.turns)), (reads, 1))
        stop.unlink()
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual(len(self.turns), 2)

    def test_both_apps_follow_independently(self):
        self.second_app()
        self.reviewed(also=(MOODY_APP_USER,))
        self.assertIsNotNone(self.follow("hermione"))
        self.assertIsNotNone(self.follow("moody"))
        self.deliver(msg(text="stop following", message="m2", at=time.time() + 1))  # mentions Hermione's app only
        self.run_pass()
        self.assertIsNone(self.follow("hermione"))
        self.push(NEWER)
        self.assertEqual(self.run_pass()["followed"], ["queued"])
        self.assertEqual([turn["desk"] for turn in self.turns][2:], ["moody"])
        self.assertEqual(self.google.acks[-1]["app"], "moody")
        self.assertEqual([record["app"] for record in self.follow_records()], ["moody"])

    def test_a_kill_after_queueing_never_queues_a_second_review(self):
        self.reviewed()
        self.push(NEWER)
        real = chat_review._save_follow
        with mock.patch.object(chat_review, "_save_follow", side_effect=SystemExit(143)), \
                self.assertRaises(SystemExit):
            self.run_pass()
        self.assertEqual(self.follow().get("pending"), None)  # killed before the follow said so
        with mock.patch.object(chat_review, "_save_follow", side_effect=real):
            self.run_pass()
            self.run_pass()
        self.assertEqual(len(self.follow_records()), 1)
        self.assertEqual((len(self.turns), len(self.google.acks), len(self.writes)), (2, 2, 2))
        self.assertEqual(self.follow()["sha"], NEWER)

    def test_a_kill_during_the_review_resumes_it_and_queues_none_more(self):
        self.reviewed()
        self.push(NEWER)
        self.answer = SystemExit(143)
        with self.assertRaises(SystemExit):
            self.run_pass()
        self.answer = ("ok", block(sha=NEWER))
        for _ in range(3):
            self.run_pass()
        [record] = self.follow_records()
        self.assertEqual((record["outcome"], record["resumes"]), ("reviewed", 1))
        self.assertEqual((len(self.turns), len(self.writes)), (3, 2))
        self.assertEqual(caps_used(), (2, 1))  # the review of new commits counted once, toward the fleet's cap only
        self.assertEqual(caps_used(LEAD), (2, 0))


class FollowKillTests(FollowCase):
    """A kill at any step of a follow command, a stop or a queued review never undoes a later command, never lets a
    stopped follow's review run and never loses a review from the day's count."""

    def dying_at(self, event: str):
        """chat_review.log, killed at the log line of one event: just after that step, before what comes next."""
        real = chat_review.log

        def log(name: str, **fields) -> None:
            if name == event:
                raise SystemExit(143)
            real(name, **fields)
        return mock.patch.object(chat_review, "log", side_effect=log)

    def killed_queueing(self) -> None:
        with mock.patch.object(chat_review, "_save_follow", side_effect=SystemExit(143)), \
                self.assertRaises(SystemExit):
            self.run_pass()

    def test_a_stop_after_a_kill_left_a_review_queued_still_ends_that_review(self):
        self.reviewed()
        self.push(NEWER)
        self.killed_queueing()
        self.assertIsNone(self.follow()["pending"])
        self.deliver(msg(text="stop following", message="m2", at=time.time() + 1))
        self.run_pass()
        [record] = self.follow_records()
        self.assertEqual((record["state"], record["outcome"]), ("done", "unfollowed"))
        self.assertEqual((len(self.turns), self.texts()[-1]), (1, "Stopped following #7."))

    def test_a_follow_command_carried_out_after_a_later_stop_starts_nothing(self):
        self.deliver(msg(text=f"follow {URL}", at=time.time() - 5))
        with self.dying_at("follow-started"), self.assertRaises(SystemExit):
            self.run_pass()
        self.assertIsNotNone(self.follow())  # started, but the command not marked done
        self.deliver(msg(text="stop following", message="m2"))
        self.run_pass()
        self.assertIsNone(self.follow())
        self.assertEqual(self.texts()[-1], "Stopped following #7.")

    def test_a_stop_carried_out_after_a_later_follow_command_ends_nothing(self):
        self.reviewed()
        self.deliver(msg(text="stop following", message="m2", at=time.time() - 3))
        with self.dying_at("follow-ended"), self.assertRaises(SystemExit):
            self.run_pass()
        self.assertIsNone(self.follow())
        self.deliver(msg(text=f"follow {URL}", message="m3"))
        self.run_pass()
        self.assertIsNotNone(self.follow())

    def test_a_kill_after_queueing_still_counts_toward_the_days_limit(self):
        self.patch(config, "CHAT_REVIEW_FOLLOW_REVIEWS_PER_DAY", 1)
        self.reviewed()
        self.push(NEWER)
        self.killed_queueing()
        self.run_pass()
        self.push(THIRD)
        self.assertEqual(self.run_pass()["followed"], ["limited"])
        self.assertEqual(len(self.turns), 2)

    def test_stopping_and_following_again_keeps_the_days_count(self):
        self.patch(config, "CHAT_REVIEW_FOLLOW_REVIEWS_PER_DAY", 1)
        self.reviewed()
        self.push(NEWER)
        self.run_pass()
        self.deliver(msg(text="stop following", message="m2", at=time.time() - 3))
        self.run_pass()
        self.push(THIRD)
        self.deliver(msg(text=f"follow {URL}", message="m3"))
        self.assertEqual(self.run_pass()["followed"], ["limited"])
        self.assertEqual(len(self.turns), 2)

    def test_a_thread_started_with_follow_takes_an_unmentioned_stop(self):
        self.deliver(msg(text=f"follow {URL}", at=time.time() - 5))
        self.run_pass()
        self.deliver(msg(text="stop following", message="q1", mention=False, at=time.time() + 1))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], ["unfollow"])
        self.assertIsNone(self.follow())

    def test_a_review_reply_sent_again_after_a_later_stop_starts_no_follow(self):
        self.deliver(msg(at=time.time() - 5))
        with self.dying_at("followed"), self.assertRaises(SystemExit):
            self.run_pass()  # the follow started, the review's record still waits to say so
        self.assertIsNotNone(self.follow())
        self.deliver(msg(text="stop following", message="m2"))
        self.run_pass()
        self.assertIsNone(self.follow())
        [review] = [record for record in self.records() if record["message"].endswith("/m1")]
        self.assertEqual((review["state"], review["follow_done"]), ("done", True))
        self.assertEqual(len(self.google.messages), 2)  # the review's reply, once, and the stop's

    def test_a_reply_that_went_late_never_takes_the_follow_back_to_an_older_head(self):
        self.reviewed()
        [(key, record)] = chat_review._records()
        started = record["started"]
        for sha, at, kept in ((NEWER, started - 10, HEAD), (THIRD, started + 10, THIRD)):
            with self.subTest(sha=sha):
                late = {**record, "sha": sha, "started": at, "follow_done": False}
                chat_review._followed("f" * 31 + sha[0], late)
                self.assertEqual(self.follow()["sha"], kept)

    def test_an_unmentioned_stop_whose_thread_turns_out_shared_ends_nothing(self):
        self.reviewed()
        with mock.patch.object(chat_review, "_follow_command", side_effect=FleetError("cut short")):
            self.deliver(msg(text="stop following", message="q1", mention=False, at=time.time() + 1))
            self.assertIn("unfollow", self.run_pass()["taken"])
        [stop] = [record for record in self.records() if record["message"].endswith("/q1")]
        # The other app's mention from before it, listed only now: the thread was never this app's alone then.
        chat_review._create("e" * 32, {"state": "done", "kind": "help", "source": "chat", "app": "moody",
                                       "space": SPACE, "thread": THREAD, "message": f"{SPACE}/messages/x1",
                                       "at": stop["at"] - 1, "at_us": stop["at_us"] - 1})
        replies = len(self.google.messages)
        self.run_pass()
        self.assertIsNotNone(self.follow())
        self.assertEqual(len(self.google.messages), replies)
        [stop] = [record for record in self.records() if record["message"].endswith("/q1")]
        self.assertEqual((stop["state"], stop["kind"]), ("done", "dropped"))
