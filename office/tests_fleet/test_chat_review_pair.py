"""Pair reviews: a review request to one Chat app that also names the other runs both desks on the same head, each in
its own sandbox with its own brief and caps, and the app mentioned posts one combined review, in one Chat reply and one
GitHub review. gh, Chat and every turn are fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the
machine and no model runs."""
from __future__ import annotations

import json
import re
import time

from fleet import chat_review, config, run_desk
from tests_fleet.test_chat_review import (HEAD, MENTION, MOODY_APP_USER, MOODY_NAME, NEWER, NOT_HUMAN, SENDER, SPACE,
                                          THREAD, URL, ChatCase, block, caps_used, msg)

HERMIONE_NAME = "Hermione (Staff Engineer)"
SECOND = "Second Reviewer"  # the second app's short name: its name less the part in brackets
HERMIONE_SUMMARY = "The change does what it says, with one loose end."
MOODY_SUMMARY = "Input reaches the shell unchecked. Not shipping that."


class PairCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.second_app()
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 20)
        self.answers = {"hermione": ("ok", block(verdict="PASS", blocking=(), summary=HERMIONE_SUMMARY,
                                                 non_blocking=("x.py:1 | the comment could say why",),
                                                 not_verified=("the runtime behavior",))),
                        "moody": ("ok", block(summary=MOODY_SUMMARY, risk="high",
                                              blocking=("x.py:1 | the input reaches a shell | quote it",
                                                        "x.py:3 | prints a secret | drop the print"),
                                              non_blocking=(), not_verified=("the runtime behavior",
                                                                             "the deploy config")))}

    def turn(self, turn, cwd, hand, on_start, on_result=None) -> tuple:
        self.answer = self.answers[turn.desk]
        return super().turn(turn, cwd, hand, on_start, on_result)

    def pair(self, text: str = f"review {URL} with Moody", **kwargs) -> dict:
        self.deliver(msg(text=text, **kwargs))
        return self.run_pass()

    def record(self) -> dict:
        [record] = self.records()
        return record


class PairGrammarTests(PairCase):
    SERVED = ["hermione", "moody"]

    def test_each_phrasing_names_the_other_app(self):
        for text in (f"review {URL} with Moody", f"review {URL} and moody", f"<{URL}> both of you please",
                     f"pair review {URL}", f"Pair-review {URL}", f"review {URL} with {MOODY_NAME}",
                     f"review {URL} with {SECOND.upper()}", f"review {URL}, moody too"):
            with self.subTest(text=text):
                self.assertEqual(chat_review.parse_pair(text, "hermione", self.SERVED), "moody")
        for text in (f"review {URL} with Hermione", f"review {URL} with {HERMIONE_NAME}", f"pair review {URL}"):
            with self.subTest(text=text):
                self.assertEqual(chat_review.parse_pair(text, "moody", self.SERVED), "hermione")

    def test_what_is_no_pair_request(self):
        for text, served in ((f"review {URL} with Moody", ["hermione"]),  # the other app is not served
                             (f"pair review {URL}", ["hermione"]),
                             (f"review {URL}", self.SERVED), (f"review {URL} with hermione", self.SERVED),
                             (f"review {URL} moodyish", self.SERVED),  # no whole word
                             (f"review {URL} https://example.org/moody/both", self.SERVED),  # inside an address
                             (f"review <{URL}|moody>", self.SERVED),  # a link's label
                             (f"review {URL} <users/555000111>", self.SERVED),  # mention markup
                             (f"review {URL} pair", self.SERVED), ("", self.SERVED), (None, self.SERVED)):
            with self.subTest(text=text, served=served):
                self.assertIsNone(chat_review.parse_pair(text, "hermione", served))

    def test_a_pair_request_is_led_by_the_app_mentioned(self):
        self.assertEqual(self.pair()["taken"], ["review"])
        self.assertEqual((self.record()["app"], self.record()["pair"]), ("hermione", "moody"))

    def test_the_second_app_can_lead_a_pair(self):
        self.pair(f"review {URL} with Hermione", app=MOODY_APP_USER)
        self.assertEqual([turn["desk"] for turn in self.turns], ["moody", "hermione"])
        self.assertEqual([message["app"] for message in self.google.messages], ["moody"])

    def test_a_message_mentioning_both_apps_is_two_reviews_not_a_pair(self):
        self.deliver(msg(text=f"pair review {URL}", also=(MOODY_APP_USER,)))
        self.assertEqual(self.run_pass()["taken"], ["review", "review"])
        self.assertTrue(all("pair" not in record for record in self.records()))
        self.assertEqual(sorted(message["app"] for message in self.google.messages), ["hermione", "moody"])

    def test_naming_an_app_that_is_not_served_is_one_review(self):
        for name, value in {"CHAT_REVIEW_MOODY_NAME": "<second-chat-app-name>",
                            "CHAT_REVIEW_MOODY_USER_KEYCHAIN_SERVICE": "<second-chat-review-user-keychain-service>",
                            "CHAT_REVIEW_MOODY_SERVICE_ACCOUNT": "<second-chat-app-service-account>",
                            "CHAT_REVIEW_MOODY_KEYCHAIN_SERVICE": "<second-chat-review-keychain-service>"}.items():
            self.patch(config, name, value)
        self.pair()
        self.assertNotIn("pair", self.record())
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione"])

    def test_a_question_naming_the_other_app_is_no_pair(self):
        self.pair(f"review {URL}")
        self.deliver(msg(text="what did moody think?", message="m2", at=time.time() + 1))
        self.run_pass()
        self.assertEqual([record.get("kind") for record in self.records()], ["review", "question"])
        self.assertTrue(all("pair" not in record for record in self.records()))


class PairRunTests(PairCase):
    def test_both_desks_review_the_same_head_each_with_its_own_brief_and_input(self):
        self.pair()
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione", "moody"])
        for turn in self.turns:
            self.assertIn(f"REVIEW PR acme/web-app#7 @ {HEAD}", turn["prompt"])
            self.assertTrue(turn["input"].startswith(f"CHAT REVIEW INPUT acme/web-app#7 @ {HEAD}"))
            self.assertIn(chat_review.VOICES[turn["desk"]].brief + chat_review.PAIR_BRIEF[turn["desk"]],
                          turn["brief"])
            self.assertEqual(len(turn["hand"]), 5)  # both PR locks, its slot, the gate and its run lock
        [ack] = self.google.acks
        self.assertEqual(ack["app"], "hermione")
        self.assertTrue(self.plans()[0].endswith(
            f"Pair review: Hermione takes correctness, tests and fit, {SECOND} takes security first."))
        record = self.record()
        self.assertEqual((record["sha"], record["reviewers"], record["outcome"]),
                         (HEAD, ["hermione", "moody"], "reviewed"))
        self.assertEqual(sorted(record["counted_by"]), ["hermione", "moody"])

    def test_the_combined_review_is_built_by_the_job(self):
        self.pair()
        record = self.record()
        self.assertEqual((record["verdict"], record["risk"]), ("CHANGES", "high"))
        self.assertEqual(record["blocking"], [
            f"B1 x.py:1 | {SECOND}: the input reaches a shell; Hermione: the comment could say why | quote it",
            f"B2 x.py:3 | {SECOND}: prints a secret | drop the print"])
        self.assertEqual(record["non_blocking"], [])
        self.assertEqual(record["not_verified"], ["the runtime behavior", "the deploy config"])
        self.assertEqual(record["summary"], f"Hermione: {HERMIONE_SUMMARY}\n{SECOND}: {MOODY_SUMMARY}")
        self.assertEqual(record["halves"]["hermione"]["verdict"], "PASS")
        self.assertEqual(record["halves"]["moody"]["blocking"][0], "B1 x.py:1 | the input reaches a shell | quote it")

    def test_combine_tags_each_finding_and_merges_two_on_the_same_line_into_one_naming_both(self):
        def parsed(verdict, blocking, non_blocking, risk, summary="s", not_verified=()):
            return chat_review.Parsed(verdict, list(blocking), list(non_blocking), summary, risk, list(not_verified))
        halves = {"hermione": parsed("CHANGES", ["B1 a.py:4 | breaks the loop | fix the bound"],
                                     ["N1 b.py:2 | naming", "N2 PR title | vague"], "medium", "h", ["v1"]),
                  "moody": parsed("CHANGES", ["B1 a.py:4 | unchecked input | validate it",
                                              f"B2 c.py:9 | {chat_review.WITHHELD}"],
                                  ["N1 b.py:2 | a log line", "N2 b.py:3 | spacing"], "low", "m", ["v1", "v2"])}
        joined = chat_review.combine(halves, ["hermione", "moody"])
        self.assertEqual(joined["blocking"], [
            f"B1 a.py:4 | Hermione: breaks the loop; {SECOND}: unchecked input | Hermione: fix the bound;"
            f" {SECOND}: validate it",
            f"B2 c.py:9 | {SECOND}: {chat_review.WITHHELD}"])
        self.assertEqual(joined["non_blocking"], [f"N1 b.py:2 | Hermione: naming; {SECOND}: a log line",
                                                  "N2 PR title | Hermione: vague", f"N3 b.py:3 | {SECOND}: spacing"])
        self.assertEqual((joined["verdict"], joined["risk"], joined["not_verified"], joined["summary"]),
                         ("CHANGES", "medium", ["v1", "v2"], f"Hermione: h\n{SECOND}: m"))
        passing = chat_review.combine({"hermione": parsed("PASS", [], [], "low"), "moody": parsed("PASS", [], [], "low")},
                                      ["hermione", "moody"])
        self.assertEqual((passing["verdict"], passing["risk"], passing["blocking"]), ("PASS", "low", []))

    def test_one_chat_reply_and_one_github_review_with_inline_comments_from_both(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.pair()
        [message] = self.google.messages
        self.assertEqual((message["app"], message["thread"]), ("hermione", THREAD))
        text = MENTION.sub("", message["text"], count=1)
        self.assertTrue(text.startswith(f"Pair review by Hermione and {SECOND}.\nHermione: {HERMIONE_SUMMARY}\n"
                                        f"{SECOND}: {MOODY_SUMMARY}\nRisk: high\nVerdict: CHANGES"))
        self.assertIn(f"- x.py:3: {SECOND}: prints a secret (fix: drop the print)", text)
        [(_, payload)] = self.writes
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(payload["body"].splitlines()[0],
                         f"This is an automated pair review by {HERMIONE_NAME} and {MOODY_NAME}, requested by Dev"
                         " Person in Chat." + NOT_HUMAN)
        self.assertIn(f"Pair review by Hermione and {SECOND}\\.", payload["body"])
        self.assertEqual(sorted((comment["path"], comment["line"]) for comment in payload["comments"]),
                         [("x.py", 1), ("x.py", 3)])
        first = next(comment for comment in payload["comments"] if comment["line"] == 1)
        self.assertEqual(first["body"], f"Blocking: {SECOND}\\: the input reaches a shell; Hermione\\: the comment"
                                        " could say why\n\nFix: quote it")
        record = self.record()
        self.assertEqual((record["github"], record["github_inline"]), ("posted", 2))
        marker = chat_review._pair_marker("acme/web-app", 7, HEAD, ("moody", "hermione"))
        self.assertTrue((self.office / chat_review.DIR / chat_review.GITHUB / marker).exists())

    def test_the_pair_marker_leaves_one_review_of_the_same_head_to_each_app(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.pair()
        self.deliver(msg(message="m2", at=time.time() + 1, thread=f"{SPACE}/threads/t2", app=MOODY_APP_USER))
        self.run_pass()
        self.assertEqual(len(self.writes), 2)  # the pair's, then Moody's own
        self.assertTrue(self.writes[1][1]["body"].startswith(f"This is an automated review by {MOODY_NAME},"))
        # A second pair of the same head, led by the other app, finds the pair's marker.
        self.deliver(msg(text=f"review {URL} with hermione", message="m3", at=time.time() + 2,
                         thread=f"{SPACE}/threads/t3", app=MOODY_APP_USER))
        self.run_pass()
        self.assertEqual(len(self.writes), 2)
        self.assertEqual([record["github"] for record in self.records() if record.get("pair") == "hermione"],
                         ["already"])

    def test_a_pair_approves_only_when_both_pass_below_high_risk_with_nothing_withheld(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.switch(config.CHAT_REVIEW_APPROVE_FILE, True)
        passing = ("ok", block(verdict="PASS", blocking=(), non_blocking=()))
        for moody, event in ((passing, "APPROVE"),
                             (("ok", block(verdict="PASS", blocking=(), non_blocking=(), risk="high")), "COMMENT"),
                             (("ok", block(verdict="PASS", blocking=(), non_blocking=(),
                                           summary=self.pr["body"])), "COMMENT")):
            with self.subTest(event=event):
                self.answers = {"hermione": passing, "moody": moody}
                self.pr["head"]["sha"] = head = f"{len(self.writes) + 2:x}" * 40
                self.answers = {desk: ("ok", answer[1].replace(HEAD, head)) for desk, answer in self.answers.items()}
                self.deliver(msg(text=f"review {URL} with moody", message=f"a{len(self.writes)}",
                                 thread=f"{SPACE}/threads/a{len(self.writes)}"))
                self.run_pass()
                self.assertEqual(self.writes[-1][1]["event"], event)

    def test_caps_count_the_person_once_and_the_fleet_twice(self):
        self.pair()
        self.assertEqual(caps_used(SENDER), (2, 1))
        rows = self.conn.execute("SELECT desk FROM run_launches ORDER BY desk").fetchall()
        self.assertEqual([row[0] for row in rows], ["hermione", "moody"])  # each desk's own launch, toward its caps

    def test_the_fleet_cap_reached_at_the_second_turn_posts_the_first_marked_single(self):
        self.patch(config, "CHAT_REVIEW_DAILY", 1)
        self.pair()
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione"])
        self.assertEqual(caps_used(SENDER), (1, 1))
        record = self.record()
        self.assertEqual((record["reviewers"], record["single"]), (["hermione"], {"desk": "moody", "why": "failed"}))
        self.assertTrue(self.texts()[0].startswith(f"Single review by Hermione: {SECOND} could not finish.\n"))

    def test_a_resumed_pair_counts_no_cap_again(self):
        record = {"state": "queued", "kind": "review", "source": "chat", "app": "hermione", "pair": "moody",
                  "space": SPACE, "thread": THREAD, "requester": SENDER, "requester_name": "Dev Person",
                  "repo": "acme/web-app", "number": 7, "at": int(time.time()), "counted": True,
                  "counted_by": ["hermione"], "resumes": 1}
        chat_review._create("f" * 32, record)
        self.run_pass()
        self.assertEqual(caps_used(SENDER), (1, 0))  # only Moody's turn, which had not launched, counts the fleet
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione", "moody"])


class PairSingleTests(PairCase):
    def test_a_stopped_family_runs_only_the_other_reviewer_and_the_lead_says_so(self):
        self.write_file(self.office / "state" / config.STOP_FAMILY_FILES["codex"], "1 a new codex version\n")
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.pair()
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione"])
        self.assertNotIn(chat_review.PAIR_BRIEF["hermione"], self.turns[0]["brief"])
        self.assertEqual(len(self.turns[0]["hand"]), 4)
        self.assertTrue(self.plans()[0].endswith(
            f"Single review: {SECOND} is paused by a fleet stop, so Hermione reviews it alone."))
        heading = f"Single review by Hermione: {SECOND} is paused by a fleet stop."
        self.assertTrue(self.texts()[0].startswith(f"{heading}\nHermione: {HERMIONE_SUMMARY}\n"))
        [(_, payload)] = self.writes
        self.assertTrue(payload["body"].startswith(f"This is an automated review by {HERMIONE_NAME}, requested by"))
        self.assertIn("Single review by Hermione", payload["body"])
        record = self.record()
        self.assertEqual((record["verdict"], record["reviewers"], record["single"]),
                         ("PASS", ["hermione"], {"desk": "moody", "why": "stopped"}))
        self.assertEqual(caps_used(SENDER), (1, 1))

    def test_a_stopped_lead_family_runs_only_the_other_and_the_lead_still_posts(self):
        self.write_file(self.office / "state" / config.STOP_FAMILY_FILES["claude"], "1 a new claude version\n")
        self.pair()
        self.assertEqual([turn["desk"] for turn in self.turns], ["moody"])
        [message] = self.google.messages
        self.assertEqual(message["app"], "hermione")
        self.assertTrue(MENTION.sub("", message["text"], count=1).startswith(
            f"Single review by {SECOND}: Hermione is paused by a fleet stop.\n{SECOND}: {MOODY_SUMMARY}"))
        self.assertEqual(self.record()["verdict"], "CHANGES")

    def test_both_families_stopped_waits(self):
        for family in ("claude", "codex"):
            self.write_file(self.office / "state" / config.STOP_FAMILY_FILES[family], "1 an update\n")
        self.pair()
        self.assertEqual((self.turns, self.record()["state"]), ([], "queued"))

    def test_one_turn_failing_posts_the_other_marked_single(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        for answer in (("failed", ""), ("ok", "no block at all")):
            with self.subTest(answer=answer):
                self.answers["moody"] = answer
                head = f"{len(self.writes) + 2:x}" * 40
                self.pr["head"]["sha"] = head
                self.answers["hermione"] = ("ok", self.answers["hermione"][1].replace(HEAD, head))
                thread = f"{SPACE}/threads/f{len(self.writes)}"
                self.deliver(msg(message=f"f{len(self.writes)}", thread=thread, text=f"review {URL} with moody"))
                self.run_pass()
                [record] = [record for record in self.records() if record["thread"] == thread]
                self.assertEqual((record["outcome"], record["reviewers"], record["single"]["why"]),
                                 ("reviewed", ["hermione"], "failed"))
                self.assertTrue(self.texts()[-1].startswith(f"Single review by Hermione: {SECOND} could not finish."))
                self.assertEqual(self.writes[-1][1]["event"], "COMMENT")
                self.answers["hermione"] = ("ok", self.answers["hermione"][1].replace(head, HEAD))

    def test_both_turns_failing_is_the_fixed_failure(self):
        self.answers = {"hermione": ("failed", ""), "moody": ("failed", "")}
        self.pair()
        self.assertEqual(self.texts(), [chat_review.COULD_NOT])
        self.assertEqual(self.record()["outcome"], "turn-failed")


class PairAfterTests(PairCase):
    def test_each_app_follows_the_pr_and_writes_its_own_memory_note(self):
        self.pair()
        for app in ("hermione", "moody"):
            follow = chat_review.read_follow(chat_review._follow_key(app, "acme/web-app", 7))
            self.assertEqual((follow["app"], follow["sha"], follow["thread"]), (app, HEAD, THREAD))
        notes = {}
        for path in (self.office / chat_review.DIR / chat_review.MEMORY).glob("*.json"):
            note = json.loads(path.read_text())
            notes[note["app"]] = note
        self.assertEqual(sorted(notes), ["hermione", "moody"])
        self.assertEqual((notes["hermione"]["verdict"], notes["hermione"]["summary"], notes["hermione"]["risk"]),
                         ("PASS", HERMIONE_SUMMARY, "low"))
        self.assertEqual((notes["moody"]["verdict"], notes["moody"]["summary"], notes["moody"]["risk"]),
                         ("CHANGES", MOODY_SUMMARY, "high"))
        self.assertEqual(notes["moody"]["findings"][0], {"kind": "blocking", "place": "x.py:1",
                                                         "gist": "the input reaches a shell"})
        for note in notes.values():
            self.assertNotIn(SECOND + ":", json.dumps(note))  # each half untagged

    def test_a_single_review_in_a_pair_is_followed_and_remembered_only_by_the_app_that_reviewed(self):
        self.answers["moody"] = ("failed", "")
        self.pair()
        self.assertIsNotNone(chat_review.read_follow(chat_review._follow_key("hermione", "acme/web-app", 7)))
        self.assertIsNone(chat_review.read_follow(chat_review._follow_key("moody", "acme/web-app", 7)))
        apps = [json.loads(path.read_text())["app"]
                for path in (self.office / chat_review.DIR / chat_review.MEMORY).glob("*.json")]
        self.assertEqual(apps, ["hermione"])

    def test_the_status_reply_names_a_pair_review(self):
        self.write_file(self.office / "state" / config.STOP_FAMILY_FILES["claude"], "1 an update\n")
        self.write_file(self.office / "state" / config.STOP_FAMILY_FILES["codex"], "1 an update\n")
        self.pair()
        self.assertIn("pair review of acme/web-app#7", chat_review.status_text("hermione", int(time.time())))

    def test_a_killed_second_turn_is_resumed_and_its_files_cleared_by_its_own_desk(self):
        record = {"state": "running", "kind": "review", "source": "chat", "app": "hermione", "pair": "moody",
                  "space": SPACE, "thread": THREAD, "requester": SENDER, "requester_name": "Dev Person",
                  "repo": "acme/web-app", "number": 7, "at": int(time.time()), "counted": True,
                  "counted_by": ["hermione", "moody"], "run_id": "chat-" + "1" * 16, "run_desk": "moody",
                  "started": int(time.time()) - 5}
        left = self.office / "runs" / "moody" / run_desk.last_message_name(record["run_id"])
        left.parent.mkdir(parents=True, exist_ok=True)
        self.write_file(left, "the last message")
        self.assertEqual(chat_review._run_desk_of(record), "moody")
        chat_review._create("e" * 32, record)
        self.assertEqual(self.run_pass()["settled"], ["resumed"])
        self.assertFalse(left.exists())  # the killed Codex turn's file, cleared from its own desk's folder
        self.assertNotIn("run_desk", self.record())
        self.run_pass()
        self.assertEqual([turn["desk"] for turn in self.turns], ["hermione", "moody"])
        self.assertEqual(caps_used(SENDER), (0, 0))  # both counted at their first launch
        self.assertEqual(self.record()["outcome"], "reviewed")

    def test_the_pair_marker_is_one_whichever_app_leads(self):
        self.assertEqual(chat_review._pair_marker("acme/web-app", 7, NEWER, ("hermione", "moody")),
                         chat_review._pair_marker("Acme/Web-App", 7, NEWER, ("moody", "hermione")))
        self.assertNotIn(chat_review._pair_marker("acme/web-app", 7, NEWER, ("hermione", "moody")),
                         (chat_review._github_marker("acme/web-app", 7, NEWER, "hermione"),
                          chat_review._github_marker("acme/web-app", 7, NEWER, "moody")))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}\.json",
                                     chat_review._pair_marker("acme/web-app", 7, NEWER, ("hermione", "moody"))))
