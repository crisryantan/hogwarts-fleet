"""Mention answers and attached material: a mention of an app that is no review request, follow-up or question about
a review is answered by a read-only turn of the app's desk, and the images, gists and web pages a message attaches
are read by the job (never the model) and handed to its turn as untrusted, fenced data. Chat, gh, the web and every
turn are fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the machine and no model runs."""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from unittest import mock

from fleet import chat_material, chat_review, config, gitops, google_chat, run_desk
from fleet.safefs import FleetError
from tests_fleet.test_chat_review import (APP_USER, CHAT_TOKEN, MOODY_APP_USER, PLANTED, SECRET_MARK, SENDER,
                                          SENDER_USER, SPACE, URL, USER_TOKEN, ChatCase, block, msg, said)

REAL_GET = chat_material._get  # captured before ChatCase replaces these
REAL_RESOLVE = chat_material._resolve
REAL_FETCH_MEDIA = google_chat.fetch_media
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 32
QUESTION = "What is a race condition?"
ANSWER = "Two threads touch shared state and the result depends on who gets there first. Guard it with a lock."
PAGE = "https://docs.example.org/guide?token=query-part-that-stays-out"
PAGE_TEXT = "Retries with jitter spread the load after an outage instead of hitting the server in lockstep again"
HTML = (f"<html><head><title>Retry guide</title><script>stealCookies()</script><style>p {{}}</style></head>"
        f"<body><h1>Retries</h1><p>{PAGE_TEXT}</p><noscript>hidden words</noscript></body></html>").encode("utf-8")


def uploaded(resource: str, kind: str = "image/png", source: str = "UPLOADED_CONTENT") -> dict:
    """One attachment as spaces.messages.list gives it."""
    return {"name": f"{SPACE}/messages/m1/attachments/{resource}", "contentName": "screenshot.png",
            "contentType": kind, "attachmentDataRef": {"resourceName": resource}, "source": source}


def attached(item: dict, *attachments: dict) -> dict:
    return {**item, "attachment": list(attachments)}


class MaterialCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 20)
        self.answer = ("ok", f"Here.\nANSWER\n{ANSWER}\nEND\n")

    def mention(self, text: str = QUESTION, message: str = "q1", attachments: tuple = (), **kwargs) -> list:
        self.deliver(attached(msg(text=text, message=message, at=time.time() + 1, **kwargs), *attachments))
        return [outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"]

    def mention_records(self) -> list:
        return [record for record in self.records() if record["kind"] == "mention"]

    def fenced(self, text: str) -> str:
        """What the turn's input holds between its material marker lines."""
        opening = next(line for line in text.splitlines() if line.startswith("<<<UNTRUSTED ATTACHED MATERIAL "))
        nonce = opening[len("<<<UNTRUSTED ATTACHED MATERIAL "):-3]
        return text.split(opening)[1].split(f"<<<END OF UNTRUSTED ATTACHED MATERIAL {nonce}>>>")[0]


class MentionAnswerTests(MaterialCase):
    """A mention from an allowed sender, anywhere the app is allowed, that is no request, follow-up or question about
    a review, is answered briefly in the app's voice by a read-only turn, and never reaches GitHub."""

    def test_a_mention_anywhere_is_answered_by_a_read_only_turn(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.switch(config.CHAT_REVIEW_APPROVE_FILE, True)
        self.assertEqual(self.mention(), ["mention"])
        [turn] = self.turns
        self.assertEqual((turn["brief"], turn["brief_file"], turn["files"]),
                         (chat_review.MENTION_BRIEF, chat_review.MENTION_BRIEF, [chat_review.QUESTION_FILE]))
        argv = turn["argv"]
        self.assertEqual(argv[:3], [config.CLAUDE_BIN, "-p", "--restricted"])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], config.CHAT_REVIEW_QUESTION_BUDGET_USD)
        self.assertIn("--no-session-persistence", argv)
        nonce = turn["prompt"].split("<<<UNTRUSTED QUESTION ")[1][:32]
        self.assertEqual(turn["prompt"], chat_review.mention_prompt(nonce))
        asked = turn["input"].split(f"<<<UNTRUSTED QUESTION {nonce}>>>")[1].split("<<<END OF UNTRUSTED QUESTION")[0]
        self.assertEqual(asked.strip(), QUESTION)
        self.assertEqual(self.google.messages[-1]["text"], said(ANSWER))  # "@Name <answer>"
        self.assertEqual(self.google.acks, [])
        [record] = self.mention_records()
        self.assertEqual((record["state"], record["outcome"], record["requester_user"]),
                         ("done", "answered", SENDER_USER))
        for gone in ("question", "repo", "number", "verdict", "github"):
            self.assertNotIn(gone, record)
        self.assertEqual((self.reads, self.writes), ([], []))  # no PR read, nothing on GitHub
        with self.assertRaises(FleetError):
            chat_review.post_github("0" * 32, {**record, "repo": "acme/web-app", "number": 7, "sha": "a" * 40,
                                               "verdict": "PASS", "blocking": []})
        self.assertEqual(self.writes, [])

    def test_the_brief_answers_engineering_and_casual_questions_without_pretending_to_access(self):
        for brief in (chat_review.MENTION_BRIEF, chat_review.MOODY_MENTION_BRIEF):
            self.assertIn("software engineering questions, casual questions and light chit-chat alike", brief)
            self.assertIn(f"at most {chat_review.ANSWER_MAX} characters", brief)
            self.assertIn("no access to the owner's repositories, files or systems", brief)
            self.assertIn("never pretend to", brief)
            self.assertIn("Never claim to have done anything", brief)
            self.assertNotIn("off-topic", brief)
        self.assertIn("brisk, precise", chat_review.MENTION_BRIEF)
        self.assertIn("gruff and terse", chat_review.MOODY_MENTION_BRIEF)
        self.assertEqual(self.mention("how is your day going?"), ["mention"])
        self.assertEqual(self.texts()[-1], ANSWER)

    def test_the_help_reply_stays_for_what_a_mention_answer_cannot_use(self):
        texts = ("", "   ", "help", "Help?", "please review this", "approve it", f"what about {URL} and {URL}?",
                 "is https://github.com/acme/other/pull/3 any good", "x" * (chat_review.QUESTION_MAX + 1))
        for index, text in enumerate(texts):
            with self.subTest(text=text[:40]):
                self.assertEqual(self.mention(text, message=f"h{index}", thread=f"{SPACE}/threads/h{index}"),
                                 ["help"])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.texts(), [chat_review.help_text("hermione")] * len(texts))
        for voice in chat_review.VOICES.values():
            self.assertIn("review PRs", voice.does)
            self.assertIn("answer questions", voice.does)
        self.assertIn("any other question, about my review or anything else", chat_review.HELP)

    def test_over_a_cap_the_mention_gets_the_help_reply(self):
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 1)
        self.assertEqual(self.mention(), ["mention"])
        self.assertEqual(self.mention("And a deadlock?", message="q2"), ["mention"])
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.texts(), [ANSWER, chat_review.help_text("hermione")])
        self.assertEqual(sorted(record["outcome"] for record in self.mention_records()), ["answered", "capped"])

    def test_help_in_a_reviewed_thread_is_the_help_reply_never_a_turn(self):
        self.answer = ("ok", block())
        self.assertEqual(self.mention(f"review {URL}", message="r1"), ["review"])
        for index, text in enumerate(("help", "Help!", "what can you do?")):
            with self.subTest(text=text):
                self.assertEqual(self.mention(text, message=f"h{index}"), ["help"])
        self.assertEqual(len(self.turns), 1)
        self.assertFalse(chat_review.parse_question("help"))

    def test_requests_follow_ups_and_review_questions_keep_their_paths(self):
        self.answer = ("ok", block())
        self.assertEqual(self.mention(f"review {URL}", message="r1"), ["review"])
        self.answer = ("ok", f"ANSWER\n{ANSWER}\nEND")
        self.assertEqual(self.mention("why is line 2 a problem?", message="q2"), ["question"])
        self.answer = ("ok", block())
        self.assertEqual(self.mention("review again", message="q3"), ["review"])
        self.assertEqual(self.mention_records(), [])
        self.assertEqual([turn["brief"] for turn in self.turns],
                         [chat_review.BRIEF, chat_review.QUESTION_BRIEF, chat_review.BRIEF])

    def test_an_unmentioned_message_is_never_answered(self):
        self.assertEqual(self.mention(mention=False), [])
        self.assertEqual(self.mention("help", message="h1"), ["help"])  # the thread is the app's now
        self.assertEqual(self.mention(message="q2", mention=False), [])
        self.assertEqual((self.mention_records(), self.turns), ([], []))

    def test_the_message_never_reaches_the_log_the_office_after_or_github(self):
        self.switch(config.CHAT_REVIEW_GITHUB_FILE, True)
        self.assertEqual(self.mention(f"{SECRET_MARK} what is a mutex?"), ["mention"])
        self.assertIn(SECRET_MARK, self.turns[0]["input"])
        self.assertNotIn(SECRET_MARK, json.dumps(self.log_lines()))
        self.assertNotIn(SECRET_MARK, self.office_text())
        lines = [line for line in self.log_lines() if line["event"].startswith("mention-")]
        self.assertEqual([line["event"] for line in lines], ["mention-received", "mention-started", "mention-ended"])
        for line in lines:
            self.assertEqual(set(line), {"at", "event", "request", "app", "outcome"})
            self.assertNotIn(SENDER, json.dumps(line))
        self.assertEqual(self.writes, [])

    def test_an_answer_is_read_strictly_scrubbed_and_defanged(self):
        self.answer = ("ok", "I pushed the fix.")
        self.assertEqual(self.mention(), ["mention"])
        self.assertEqual(self.texts()[-1], chat_review.COULD_NOT_ANSWER)
        self.answer = ("ok", "ANSWER\n<users/all> see https://evil.test and mail owner@example.com\nEND")
        self.assertEqual(self.mention("and a semaphore?", message="q2"), ["mention"])
        for bad in ("<users/all>", "https://evil", "owner@example.com"):
            self.assertNotIn(bad, self.texts()[-1])

    def test_the_second_app_answers_a_mention_with_a_read_only_codex_turn(self):
        self.second_app()
        self.assertEqual(self.mention(app=MOODY_APP_USER), ["mention"])
        [turn] = self.turns
        self.assertEqual((turn["desk"], turn["family"], turn["brief"]), ("moody", "codex",
                                                                         chat_review.MOODY_MENTION_BRIEF))
        run_desk.guard(turn["argv"])
        self.assertIn('default_permissions="fleet-chat-review-moody"', turn["argv"])
        self.assertNotIn("--image", turn["argv"])
        self.assertEqual((self.google.messages[-1]["app"], self.texts()[-1]), ("moody", ANSWER))


class ImageTests(MaterialCase):
    def test_images_are_downloaded_with_the_read_token_checked_by_their_bytes_and_capped(self):
        self.media = {"r1": PNG, "r2": b"not an image at all", "r3": b"\x89PNG" + b"x" * chat_material.IMAGE_MAX_BYTES,
                      "r4": JPEG}
        self.answer = ("ok", block())
        self.deliver(attached(msg(text=f"review {URL}", at=time.time() + 1), uploaded("r1"), uploaded("r2"),
                              uploaded("r3", "image/jpeg"), uploaded("r4"), uploaded("d1", source="DRIVE_FILE"),
                              uploaded("t1", "text/plain")))
        self.assertEqual(self.run_pass()["taken"], ["review"])
        [turn] = self.turns
        self.assertEqual(turn["files"], ["image-1.png", chat_review.INPUT_FILE])
        self.assertEqual(turn["images"], {"image-1.png": PNG})
        self.assertNotIn("--image", turn["argv"])  # Hermione reads it with Read
        self.assertEqual([token for _, token in self.media_calls], [USER_TOKEN] * 3)
        self.assertEqual([urllib.parse.urlsplit(url).path for url, _ in self.media_calls],
                         ["/v1/media/r1", "/v1/media/r2", "/v1/media/r3"])
        fenced = self.fenced(turn["input"])
        self.assertIn("IMAGES attached to the message, in your working directory: image-1.png", fenced)
        self.assertIn(f"IMAGE 2: {chat_material.COULD_NOT_IMAGE}", fenced)
        self.assertIn(f"IMAGE 3: {chat_material.COULD_NOT_IMAGE}", fenced)
        self.assertIn("1 more images not read", fenced)
        [line] = [line for line in self.log_lines() if line["event"] == "material"]
        self.assertEqual((line["images"], line["images_failed"]), (1, 2))
        self.assertNotIn("r1", json.dumps([line for line in self.log_lines() if line["event"] != "received"]))
        [record] = self.records()
        self.assertEqual(record["outcome"], "reviewed")
        self.assertNotIn("material", record)  # the names went once the turn ran

    def test_each_image_kind_is_told_by_its_first_bytes(self):
        for raw, kind in ((PNG, "png"), (JPEG, "jpg"), (b"GIF89a....", "gif"), (b"GIF87a....", "gif"), (WEBP, "webp"),
                          (b"RIFF\x00\x00\x00\x00WAVE", None), (b"<svg></svg>", None), (b"", None)):
            with self.subTest(kind=kind):
                self.assertEqual(chat_material.image_kind(raw), kind)

    def test_moodys_turn_gets_its_images_with_the_image_flag_and_keeps_its_sandbox(self):
        self.second_app()
        self.media = {"r1": PNG, "r2": WEBP}
        self.assertEqual(self.mention("what does this screen show?", app=MOODY_APP_USER,
                                      attachments=(uploaded("r1"), uploaded("r2", "image/webp"))), ["mention"])
        [turn] = self.turns
        argv = turn["argv"]
        run_desk.guard(argv)
        self.assertEqual(turn["images"], {"image-1.png": PNG, "image-2.webp": WEBP})
        first = argv.index("--image")
        self.assertEqual(argv[first:first + 4], ["--image", "image-1.png", "--image", "image-2.webp"])
        self.assertEqual(argv[first + 4], "--skip-git-repo-check")  # a flag follows, so no image takes what is after
        self.assertEqual(argv[-1], "-")
        profile = next(arg for arg in argv if arg.startswith("permissions.fleet-chat-review-moody="))
        self.assertIn("network={enabled=false}", profile)
        self.assertNotIn('"write"', profile)
        for bad in (("../x.png",), ("image-1.exe",), ("a",) * 4, ["image-1.png"]):
            with self.subTest(images=bad), self.assertRaises(FleetError):
                run_desk.chat_review_argv(self.conn, "b", "p", "chat-" + "1" * 16, "moody", None, bad)

    def test_with_images_switched_off_none_is_downloaded_and_the_turn_is_told(self):
        self.patch(config, "CHAT_REVIEW_IMAGES", False)
        self.media = {"r1": PNG}
        self.assertEqual(self.mention(attachments=(uploaded("r1"),)), ["mention"])
        [turn] = self.turns
        self.assertEqual((self.media_calls, turn["images"]), ([], {}))
        self.assertIn("image reading is switched off", self.fenced(turn["input"]))
        self.assertEqual(self.texts(), [ANSWER])

    def test_the_media_download_takes_only_a_read_token_and_a_plain_resource_name(self):
        google_chat._READ["hermione"] = {"token": USER_TOKEN, "until": int(time.time()) + 3600}
        google_chat._TOKENS[("hermione", google_chat.CHAT_BOT_SCOPE)] = (CHAT_TOKEN, int(time.time()) + 3600)
        self.media = {"spaces/AAAA/attachments/x1": PNG}
        self.assertEqual(google_chat.download_media("spaces/AAAA/attachments/x1", USER_TOKEN, 100), PNG)
        for resource, token in (("spaces/AAAA/attachments/x1", CHAT_TOKEN), ("../../v1/spaces", USER_TOKEN),
                                ("/etc", USER_TOKEN), ("a b", USER_TOKEN), ("x?alt=json", USER_TOKEN)):
            with self.subTest(resource=resource), self.assertRaises(google_chat.GoogleError):
                google_chat.download_media(resource, token, 100)
        with mock.patch.object(google_chat, "_opener", side_effect=AssertionError("sent")):
            for url, token in ((f"{google_chat.CHAT_ROOT}/media/x", CHAT_TOKEN),
                               ("https://example.org/media/x", USER_TOKEN),
                               (f"{google_chat.CHAT_ROOT}/spaces/AAAA/messages", USER_TOKEN)):
                with self.subTest(url=url), self.assertRaises(google_chat.GoogleError):
                    REAL_FETCH_MEDIA(url, token, 100)
        self.assertEqual(len(self.media_calls), 1)
        broken = mock.Mock()
        broken.open.side_effect = http.client.IncompleteRead(b"x")
        with mock.patch.object(google_chat, "_opener", return_value=broken), \
                self.assertRaises(google_chat.GoogleError):  # a protocol error is an unreadable image, never a crash
            REAL_FETCH_MEDIA(f"{google_chat.CHAT_ROOT}/media/x?alt=media", USER_TOKEN, 100)

    def test_list_messages_gives_back_only_uploaded_image_attachments(self):
        google_chat._READ["hermione"] = {"token": USER_TOKEN, "until": int(time.time()) + 3600}
        self.deliver(attached(msg(), uploaded("r1"), uploaded("d1", source="DRIVE_FILE"), uploaded("t1", "text/plain"),
                              uploaded("../x"), "junk"))
        found, _ = google_chat.list_messages(SPACE, 0, APP_USER, USER_TOKEN)
        self.assertEqual(found[0]["attachments"], ["r1"])


class GistTests(MaterialCase):
    GIST = "0123456789abcdef0123"

    def gist(self, gist: str = GIST, **files) -> dict:
        return {"id": gist, "files": files or {"retry.py": {"filename": "retry.py", "type": "application/x-python",
                                                            "content": "def retry():\n    return 1\n",
                                                            "truncated": False}}}

    def test_a_gist_link_is_read_with_the_fixed_gh_shape_text_files_only(self):
        other = "fedcba9876543210fedc"
        self.gists = {self.GIST: self.gist(), other: self.gist(
            other, **{"notes.md": {"type": "text/markdown", "content": "# Notes", "truncated": False},
                      "pic.png": {"type": "image/png", "content": "binary", "truncated": False},
                      "big.txt": {"type": "text/plain", "content": "cut", "truncated": True}})}
        text = (f"what does this do? https://gist.github.com/someone/{self.GIST} and"
                f" <https://gist.githubusercontent.com/someone/{other}/raw/abc/notes.md|notes>"
                " https://gist.github.com/someone/not-a-gist-id https://gist.github.com/someone/00000000000000000000")
        self.assertEqual(self.mention(text), ["mention"])
        self.assertEqual(self.reads, ["gist", "gist", "gist"])
        fenced = self.fenced(self.turns[0]["input"])
        self.assertIn("GIST 1\nFILE retry.py\ndef retry():", fenced)
        self.assertIn("FILE notes.md\n# Notes", fenced)
        self.assertIn("FILE pic.png: not read (not a whole text file)", fenced)
        self.assertIn("FILE big.txt: not read (not a whole text file)", fenced)
        self.assertIn(f"GIST 3: {chat_material.COULD_NOT_GIST}", fenced)
        self.assertEqual(self.web_calls, [])  # a gist link is never fetched as a page
        self.assertEqual(self.texts(), [ANSWER])

    def test_the_gist_read_passes_the_guard_and_nothing_else_does(self):
        self.assertEqual(gitops.check_read_argv(gitops.gist_read_argv(self.GIST)), "gist")
        for argv in ([config.GH_BIN, "api", "gists/ABCDEF"], [config.GH_BIN, "api", "gists/abc/star"],
                     [config.GH_BIN, "api", "gists/../repos/acme/web-app"], [config.GH_BIN, "api", "gists"],
                     [config.GH_BIN, "api", f"gists/{self.GIST}", "-X", "PATCH"],
                     [config.GH_BIN, "api", "--method", "DELETE", f"gists/{self.GIST}"],
                     [config.GH_BIN, "api", "-H", gitops.DIFF_ACCEPT, f"gists/{self.GIST}"],
                     ["gh", "api", f"gists/{self.GIST}"], [config.GH_BIN, "api", "gists/"]):
            with self.subTest(argv=argv), self.assertRaises(FleetError):
                gitops.check_read_argv(argv)
        for bad in ("ABC", "../x", "", None, "a" * 65):
            with self.subTest(gist=bad), self.assertRaises(FleetError):
                gitops.gist_read_argv(bad)

    def test_a_gist_answer_for_another_gist_is_refused(self):
        self.gists = {self.GIST: self.gist("ffffffffffffffffffff")}
        self.assertEqual(self.mention(f"see https://gist.github.com/someone/{self.GIST}"), ["mention"])
        self.assertIn(f"GIST 1: {chat_material.COULD_NOT_GIST}", self.fenced(self.turns[0]["input"]))


class WebTests(MaterialCase):
    def test_a_page_is_fetched_by_the_job_reduced_to_text_and_fenced(self):
        self.web = {PAGE: (200, {"content-type": "text/html; charset=utf-8"}, HTML)}
        self.assertEqual(self.mention(f"is this advice right? {PAGE}"), ["mention"])
        self.assertEqual(self.web_calls, [("docs.example.org", "93.184.216.34", "/guide?token=query-part-that-stays-out")])
        fenced = self.fenced(self.turns[0]["input"])
        self.assertIn("LINK 1 (docs.example.org)\nTITLE Retry guide\nRetries\n" + PAGE_TEXT, fenced)
        for gone in ("stealCookies", "hidden words", "<p>", "query-part"):
            self.assertNotIn(gone, fenced)
        self.assertIn("between <<<UNTRUSTED ATTACHED MATERIAL", self.turns[0]["prompt"])
        # Only counts reach the log, and nothing of the link or the page stays in the office once it is answered.
        [line] = [line for line in self.log_lines() if line["event"] == "mention-material"]
        self.assertEqual(set(line), {"at", "event", "request", "app", "links"})
        for found in (json.dumps(self.log_lines()), self.office_text()):
            for word in ("docs.example.org", "query-part", "Retry guide", PAGE_TEXT[:30]):
                self.assertNotIn(word, found)

    def test_a_failed_fetch_is_a_note_and_never_the_end_of_the_turn(self):
        self.assertEqual(self.mention(f"thoughts on {PAGE}"), ["mention"])  # the fake answers 404
        self.assertIn(f"LINK 1: {chat_material.COULD_NOT_LINK}", self.fenced(self.turns[0]["input"]))
        self.assertEqual(self.texts(), [ANSWER])
        with mock.patch.object(chat_material, "gather", side_effect=OSError("boom")):
            self.assertEqual(self.mention(f"and {PAGE}", message="q2"), ["mention"])
        self.assertIn("could not read the attached material", self.fenced(self.turns[1]["input"]))
        self.assertEqual(self.texts(), [ANSWER, ANSWER])

    def test_at_most_three_links_are_read_and_a_pr_link_never_is(self):
        links = [f"https://site{index}.example.org/a" for index in range(5)]
        self.answer = ("ok", block())
        self.deliver(attached(msg(text=f"review {URL} " + " ".join(links), at=time.time() + 1)))
        self.assertEqual(self.run_pass()["taken"], ["review"])
        self.assertEqual([call[0] for call in self.web_calls], [f"site{index}.example.org" for index in range(3)])
        fenced = self.fenced(self.turns[0]["input"])
        self.assertIn("2 more links not read: at most 3 are", fenced)
        self.assertEqual(chat_material.plan(f"see {URL} and https://GitHub.com/acme/x/pull/9/files"), {})

    def test_the_link_rules(self):
        for url in ("http://docs.example.org/", "https://docs.example.org:8443/", "https://user:pw@docs.example.org/",
                    "https://localhost/", "https://127.0.0.1/", "https://[::1]/", "https://2130706433/",
                    "https://0x7f.1/", "https://10.0.0.1/", "https://intranet/", "ftp://docs.example.org/",
                    "https://docs.example.org/a b", "https://döcs.example.org/", "https://docs.example.org:0/",
                    "https://docs.example.org:443@evil.example.org/", "https://" + "a" * 2000 + ".org/"):
            with self.subTest(url=url[:60]), self.assertRaises(chat_material.Refused):
                chat_material.check_url(url)
        self.assertEqual(chat_material.check_url("https://Docs.Example.org:443/a?b=c#frag"),
                         ("docs.example.org", "/a?b=c"))
        self.assertEqual(chat_material.check_url("https://docs.example.org"), ("docs.example.org", "/"))

    def test_only_public_addresses_are_connected_to(self):
        for address in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                        "0.0.0.0", "224.0.0.1", "239.1.2.3", "255.255.255.255", "240.0.0.1", "192.0.2.1", "::1",
                        "::", "fc00::1", "fd12::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1", "2002:7f00:1::1",
                        "2001:0:4136:e378::1", "64:ff9b::7f00:1", "fec0::1", "2001:db8::1", "not-an-address"):
            with self.subTest(address=address):
                self.assertFalse(chat_material.public(address))
        for address in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(chat_material.public(address))

    def test_a_host_resolving_to_any_private_address_is_never_fetched(self):
        for addresses in (["10.0.0.5"], ["169.254.169.254"], ["::1"], ["fc00::5"], ["93.184.216.34", "127.0.0.1"],
                          []):
            with self.subTest(addresses=addresses):
                self.resolved = {"docs.example.org": addresses}
                with self.assertRaises(chat_material.Refused):
                    chat_material.fetch_page(PAGE)
        self.assertEqual(self.web_calls, [])

    def test_every_redirect_hop_is_checked_the_same_way(self):
        start = "https://docs.example.org/start"
        self.resolved = {"evil.example.org": ["127.0.0.1"]}
        for target in ("https://evil.example.org/", "http://docs.example.org/", "https://docs.example.org:8443/",
                       "https://10.0.0.1/", "https://[::1]/latest/meta-data", ""):
            with self.subTest(target=target):
                self.web_calls.clear()
                self.web = {start: (302, {"location": target} if target else {}, b"")}
                with self.assertRaises(chat_material.Refused):
                    chat_material.fetch_page(start)
                self.assertEqual(len(self.web_calls), 1)  # the refused hop is never asked for
        # A loop, and one hop too many.
        self.web = {start: (301, {"location": "/b"}, b""), "https://docs.example.org/b": (307, {"location": start}, b"")}
        with self.assertRaises(chat_material.Refused):
            chat_material.fetch_page(start)
        self.web = {f"https://docs.example.org/{index}": (302, {"location": f"/{index + 1}"}, b"") for index in range(9)}
        self.web_calls.clear()
        with self.assertRaises(chat_material.Refused):
            chat_material.fetch_page("https://docs.example.org/0")
        self.assertEqual(len(self.web_calls), chat_material.REDIRECT_MAX + 1)
        # Within the limit, a relative redirect to a public page is followed.
        self.web = {start: (302, {"location": "/guide"}, b""),
                    "https://docs.example.org/guide": (200, {"content-type": "text/plain"}, b"plain words")}
        self.assertEqual(chat_material.fetch_page(start), ("docs.example.org", "plain words"))

    def test_the_type_size_and_status_caps(self):
        for kind in ("image/png", "application/octet-stream", "application/pdf", "", "video/mp4"):
            with self.subTest(kind=kind):
                self.web = {PAGE: (200, {"content-type": kind}, b"\x89PNG")}
                with self.assertRaises(chat_material.Refused):
                    chat_material.fetch_page(PAGE)
        for status in (204, 403, 404, 500):
            with self.subTest(status=status):
                self.web = {PAGE: (status, {"content-type": "text/plain"}, b"x")}
                with self.assertRaises(chat_material.Refused):
                    chat_material.fetch_page(PAGE)
        self.web = {PAGE: (200, {"content-type": "text/plain", "content-encoding": "gzip"}, b"\x1f\x8b")}
        with self.assertRaises(chat_material.Refused):
            chat_material.fetch_page(PAGE)
        self.web = {PAGE: (200, {"content-type": "application/json"}, b"x" * (chat_material.PAGE_MAX_BYTES + 500))}
        _, text = chat_material.fetch_page(PAGE)
        self.assertEqual(len(text), chat_material.PAGE_TEXT_MAX)

    def test_the_fetch_connects_to_the_vetted_address_with_the_host_name_checked(self):
        connected = []

        def connect(address, timeout):
            connected.append((address, timeout))
            raise OSError("no network in tests")

        with mock.patch.object(chat_material.socket, "create_connection", side_effect=connect), \
                self.assertRaises(OSError):
            REAL_GET("docs.example.org", "93.184.216.34", "/guide", time.monotonic() + 60)
        self.assertEqual(connected, [(("93.184.216.34", 443), chat_material.FETCH_TIMEOUT_SECONDS)])
        pinned = chat_material._Pinned("docs.example.org", "93.184.216.34")
        self.assertEqual((pinned.host, pinned.port, pinned._context.check_hostname), ("docs.example.org", 443, True))

    def test_a_server_that_drips_its_answer_is_cut_at_the_deadline(self):
        cut = threading.Event()

        class Sock:
            def shutdown(self, how):
                cut.set()

        class Dripping:
            """A connection whose headers never finish arriving, until it is cut."""

            def __init__(self, host, address, timeout):
                self.sock = Sock()

            def request(self, method, target, headers):
                pass

            def getresponse(self):
                if not cut.wait(5):
                    raise AssertionError("never cut")
                raise OSError("the connection was cut")

            def close(self):
                pass

        begun = time.monotonic()
        with mock.patch.object(chat_material, "_Pinned", Dripping), self.assertRaises(OSError):
            REAL_GET("docs.example.org", "93.184.216.34", "/guide", time.monotonic() + 0.3)
        self.assertTrue(cut.is_set())
        self.assertLess(time.monotonic() - begun, 3)
        with self.assertRaises(chat_material.Refused):  # nothing starts once the deadline passed
            REAL_GET("docs.example.org", "93.184.216.34", "/guide", time.monotonic() - 1)
        self.assertLessEqual(chat_material.LINK_DEADLINE_SECONDS, 30)

    def test_a_slow_name_lookup_is_given_up_at_the_deadline(self):
        def slow(*args, **kwargs):
            time.sleep(2)
            return [(None, None, None, "", ("93.184.216.34", 443))]

        begun = time.monotonic()
        with mock.patch.object(chat_material.socket, "getaddrinfo", side_effect=slow), \
                self.assertRaises(chat_material.Refused):
            REAL_RESOLVE("docs.example.org", time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - begun, 1.5)
        with mock.patch.object(chat_material.socket, "getaddrinfo",
                               return_value=[(None, None, None, "", ("93.184.216.34", 443))] * 2):
            self.assertEqual(REAL_RESOLVE("docs.example.org", time.monotonic() + 5), ["93.184.216.34"])

    def test_an_echo_of_fetched_text_is_withheld_everywhere(self):
        self.web = {PAGE: (200, {"content-type": "text/html"}, HTML)}
        self.answer = ("ok", f"ANSWER\nThe page says: {PAGE_TEXT}\nEND")
        self.assertEqual(self.mention(f"summarize {PAGE}"), ["mention"])
        self.assertEqual(self.texts()[-1], chat_review.MENTION_WITHHELD)
        self.assertEqual(self.mention_records()[0]["outcome"], "withheld")
        # A review's finding that repeats it is withheld too, as one repeating the PR's own text is.
        self.answer = ("ok", block(non_blocking=(f"x.py:1 | {PAGE_TEXT}",)))
        self.assertEqual(self.mention(f"review {URL} with {PAGE}", message="r1", thread=f"{SPACE}/threads/r1"),
                         ["review"])
        [review] = [record for record in self.records() if record["kind"] == "review"]
        self.assertTrue(review["non_blocking"][0].endswith(chat_review.WITHHELD))
        # And a question's answer.
        self.answer = ("ok", f"ANSWER\nIt says {PAGE_TEXT}\nEND")
        self.assertEqual(self.mention(f"does {PAGE} apply here?", message="q3", thread=f"{SPACE}/threads/r1"),
                         ["question"])
        self.assertEqual(self.texts()[-1], chat_review.ANSWER_WITHHELD)
        self.assertNotIn(PLANTED, " ".join(self.texts()))
