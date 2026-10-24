"""McGonagall's Chat app: the owner drafts a task in a direct message with her app, starts it with a go and a one-time
code, asks for the fleet's status and hears go updates there. Only the owner, only in that direct message. Chat, the
Keychain, openssl and every turn are fakes (tests_fleet.test_chat_review.ChatCase), and the go runs the hook's own code
against a real git checkout whose origin is a bare repo in the temp folder (tests_fleet.test_go.GoCase): no call leaves
the machine and no model runs."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import pensieve
from hogwarts.errors import NotFoundError

from fleet import chat_chief, chat_review, common, config, go_watch, google_chat, run_desk
from fleet.hooks import user_prompt_submit as hook
from tests_fleet import test_chat_review as chat_tests
from tests_fleet.support import IN_KIT, OFFICE, ONLY_IN_KIT, kit_setting
from tests_fleet.test_chat_review import CHIEF_PLACEHOLDERS, LEAD, LEAD_USER, SENDER, SENDER_USER, SPACE, ChatCase, msg
from tests_fleet.test_go import GoCase

DM = "spaces/DMDM"
CHIEF_APP_USER = "users/777000222"
CHIEF_CLIENT_ID = "3234567890-thirdclient0123.apps.googleusercontent.com"
CHIEF_USER_TOKEN = "ya29.fake-third-app-read-token-7777"
CHIEF_CHAT_TOKEN = "ya29.fake-third-app-bot-token-8888"
CHIEF_SERVICE_ACCOUNT = "front-worker@acme-front-789.iam.gserviceaccount.com"
CHIEF_KEYCHAIN_SERVICE = "hogwarts-chat-front"
CHIEF_USER_KEYCHAIN_SERVICE = "hogwarts-chat-front-read"
CHIEF_USER_BLOB = base64.b64encode(json.dumps({"client_id": CHIEF_CLIENT_ID, "client_secret": chat_tests.CLIENT_SECRET,
                                               "refresh_token": chat_tests.REFRESH}).encode("ascii"))
# The third app's settings, filled as an owner adding it does (ChatCase patches in the kit's placeholders): tests
# patch them in, never read them from config, since an installed office's overlay may fill them.
CHIEF_SETTINGS = {"CHAT_REVIEW_CHIEF_NAME": "Front Desk", "CHAT_REVIEW_CHIEF_SERVICE_ACCOUNT": CHIEF_SERVICE_ACCOUNT,
                  "CHAT_REVIEW_CHIEF_USER_KEYCHAIN_SERVICE": CHIEF_USER_KEYCHAIN_SERVICE,
                  "CHAT_REVIEW_CHIEF_KEYCHAIN_SERVICE": CHIEF_KEYCHAIN_SERVICE}
ASK = "Please add a check that the widget file exists, in the web app repo. OWNER-WORDS-7731"
CODE_LINE = re.compile(r"send go (tk_[0-9a-f]{16}) ([A-Z0-9]{6}) within")


class ChiefGoogle(chat_tests.FakeGoogle):
    """The fake Google, with the third app's read token answered for its own memberships (chief_in)."""

    def __init__(self) -> None:
        super().__init__()
        self.chief_in = {DM, SPACE}

    def send(self, url: str, token, payload, timeout=None) -> dict:
        if token != CHIEF_USER_TOKEN:
            return super().send(url, token, payload, timeout)
        kept = self.moody_in
        self.moody_in = self.chief_in
        try:
            return super().send(url, token, payload, timeout)
        finally:
            self.moody_in = kept


class ChiefCase(ChatCase, GoCase):
    """The owner is LEAD, with a direct message (DM) with McGonagall's app, which is in the shared space too."""

    def setUp(self) -> None:
        for name, values in (("READ_TOKENS", {CHIEF_CLIENT_ID: CHIEF_USER_TOKEN}),
                             ("APP_TOKENS", {CHIEF_SERVICE_ACCOUNT: CHIEF_CHAT_TOKEN}),
                             ("APP_USERS", {CHIEF_USER_TOKEN: CHIEF_APP_USER}),
                             ("APPS_BY_TOKEN", {CHIEF_CHAT_TOKEN: "mcgonagall"})):
            patcher = mock.patch.dict(getattr(chat_tests, name), values)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.google = ChiefGoogle()
        self.google.members[DM] = {LEAD: LEAD_USER}
        self.patch(google_chat, "send", side_effect=self.google.send)
        for name, value in {**CHIEF_SETTINGS, "CHAT_REVIEW_OWNER": LEAD, "CHAT_REVIEW_SPACES": (SPACE, DM),
                            "CHAT_CHIEF_ROOT": str(self.tmp / "chief-root")}.items():
            self.patch(config, name, value)
        desk = self.office / "desks" / "mcgonagall"
        desk.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(OFFICE / "desks" / "mcgonagall" / config.OWL_REPORT_SETTINGS_FILE,
                        desk / config.OWL_REPORT_SETTINGS_FILE)
        self.enable("harry")
        self.spawn = self.patch(run_desk, "spawn")
        self.sent = 0

    def keychain(self, service: str, account: str) -> tuple:
        if service == CHIEF_USER_KEYCHAIN_SERVICE:
            self.keychain_calls.append((service, account))
            return (0, CHIEF_USER_BLOB + b"\n") if account == google_chat.USER_TOKEN_ACCOUNT else (44, b"")
        return super().keychain(service, account)

    # driving

    def dm(self, text: str, sender: str = LEAD_USER, space: str = DM, mention: bool = False) -> list:
        """One message to the app and a pass: the replies McGonagall's app posted in it."""
        self.sent += 1
        self.deliver(msg(text=text, sender=sender, space=space, message=f"c{self.sent}", mention=mention,
                         app=CHIEF_APP_USER, at=time.time() + self.sent * 0.001))
        before = len(self.chief_posts())
        self.result = self.run_pass()
        return [post["text"] for post in self.chief_posts()[before:]]

    def chief_posts(self) -> list:
        return [post for post in self.google.messages if post["app"] == "mcgonagall"]

    def draft_answer(self, **fields) -> tuple:
        found = {"action": "draft", "task_id": None, "title": "Add the widget check", "repo": str(self.repo),
                 "branch": "fix/widget", "base": "origin/main",
                 "criteria": [{"what": "the readme is there", "label": "check", "check": "`test -f README.md`"},
                              {"what": "the change reads well", "label": "check", "check": "the reviewer reads it"}],
                 "spec": "Desk: Harry.\nFiles: widget.txt.", "out_of_scope": "Anything else."}
        return "ok", json.dumps({**found, **fields})

    def drafted(self, text: str = ASK) -> str:
        """A draft from the owner's message; its task id."""
        self.answer = self.draft_answer()
        [reply] = self.dm(text)
        return re.match(r"Drafted (tk_[0-9a-f]{16}):", reply).group(1)

    def code_for(self, task_id: str) -> str:
        [reply] = self.dm(f"go {task_id}")
        found = CODE_LINE.search(reply)
        self.assertIsNotNone(found, reply)
        self.assertEqual(found.group(1), task_id)
        return found.group(2)

    def registered(self, task_id: str) -> bool:
        try:
            pensieve.get_task(self.conn, task_id)
        except NotFoundError:
            return False
        return True

    def chat_events(self) -> list:
        return [dict(row) for row in self.conn.execute("SELECT kind, verdict, task_id, summary FROM events"
                                                       " WHERE kind LIKE 'go.%' ORDER BY id")]

    def castle_files(self) -> dict:
        found = {}
        for root, _, files in os.walk(self.castle):
            for name in files:
                path = Path(root, name)
                found[str(path.relative_to(self.castle))] = path.read_bytes()
        return found


class AppTests(ChiefCase):
    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_kit_ships_the_third_app_unfilled_and_its_updates_switch_off(self):
        for name, value in CHIEF_PLACEHOLDERS.items():
            self.assertEqual(kit_setting(name), value)
        self.assertEqual(list(kit_setting("CHAT_REVIEW_APPS")), ["hermione", "moody", "mcgonagall"])
        self.assertEqual(kit_setting("CHAT_REVIEW_OWNER"), "<chat-review-owner-email>")
        self.assertIn(config.CHAT_GO_UPDATES_FILE, config.OPT_IN_FILES)
        self.assertFalse(common.opt_in_on(config.CHAT_GO_UPDATES_FILE))

    def test_an_unfilled_third_app_is_not_served_and_the_reviewers_are_unchanged(self):
        for name, value in CHIEF_PLACEHOLDERS.items():
            self.patch(config, name, value)
        self.assertEqual(chat_review.served_apps(), ["hermione"])
        self.assertFalse(chat_review.chief_served())
        self.assertEqual(chat_review.config_problems(intake=True), [])
        self.assertNotIn("chief", self.run_pass())
        self.assertNotIn(CHIEF_USER_KEYCHAIN_SERVICE, [service for service, _ in self.keychain_calls])

    def test_the_filled_third_app_is_never_a_reviewer(self):
        self.assertEqual(chat_review.served_apps(), ["hermione"])
        self.assertTrue(chat_review.chief_served())
        self.assertEqual(chat_review.config_problems(intake=True), [])
        # A review request that mentions her app is no review: no record of a review, and no reviewer turn.
        self.deliver(msg(text=f"review {chat_tests.URL}", sender=SENDER_USER, message="r1", app=CHIEF_APP_USER))
        self.run_pass()
        self.assertEqual(self.turns, [])
        self.assertEqual([post for post in self.google.messages], [])

    def test_a_served_third_app_needs_the_owner_filled(self):
        self.patch(config, "CHAT_REVIEW_OWNER", "<chat-review-owner-email>")
        problems = chat_review.config_problems(intake=True)
        self.assertTrue(any("CHAT_REVIEW_OWNER" in problem for problem in problems), problems)

    def test_the_third_app_shares_no_keychain_item_or_identity(self):
        self.patch(config, "CHAT_REVIEW_CHIEF_KEYCHAIN_SERVICE", chat_tests.KEYCHAIN_SERVICE)
        problems = chat_review.config_problems(intake=True)
        self.assertTrue(any("CHAT_REVIEW_CHIEF_KEYCHAIN_SERVICE" in problem for problem in problems), problems)

    def test_the_second_app_still_reviews_beside_the_third(self):
        self.second_app()
        self.assertEqual(chat_review.served_apps(), ["hermione", "moody"])
        self.answer = ("ok", chat_tests.block(verdict="PASS", blocking=()))
        self.deliver(msg(text=f"review {chat_tests.URL}", sender=SENDER_USER, message="r2",
                         app=chat_tests.MOODY_APP_USER))
        self.run_pass()
        self.assertEqual([turn["desk"] for turn in self.turns], ["moody"])
        self.assertEqual([post["app"] for post in self.google.messages], ["moody"])


class WhoAndWhereTests(ChiefCase):
    def test_status_in_the_direct_message_is_the_fleet_answer(self):
        [reply] = self.dm("status")
        self.assertTrue(reply.startswith(("Nothing open.", "Open (")), reply)
        [post] = self.chief_posts()
        self.assertEqual((post["space"], post["app"]), (DM, "mcgonagall"))
        self.assertEqual(self.turns, [])

    def test_the_owner_anywhere_else_gets_one_fixed_line_and_anyone_else_nothing(self):
        self.assertEqual(self.dm("status", space=SPACE, mention=True), [chat_chief.DM_ONLY])
        self.assertEqual(self.dm(f"go tk_{'1' * 16}", space=SPACE, mention=True), [chat_chief.DM_ONLY])
        self.assertEqual(self.dm(ASK, space=SPACE, mention=True), [chat_chief.DM_ONLY])
        for text in ("status", ASK, f"go tk_{'1' * 16}"):
            self.assertEqual(self.dm(text, sender=SENDER_USER, space=SPACE, mention=True), [])
        self.assertEqual(self.turns, [])
        self.assertNotIn("OWNER-WORDS-7731", self.office_text())

    def test_a_direct_message_with_anyone_else_in_it_gets_nothing(self):
        self.google.members[DM][SENDER] = SENDER_USER
        for text in ("status", ASK, f"go tk_{'1' * 16}"):
            self.assertEqual(self.dm(text), [])
        self.assertEqual(self.dm("status", mention=True), [chat_chief.DM_ONLY])  # only a mention, and only one line
        self.assertEqual(self.turns, [])

    def test_a_message_from_anyone_but_the_owner_in_the_direct_message_is_dropped(self):
        # A conversation of the owner and the app alone has no one else in it; a stray sender is never answered.
        self.assertEqual(self.dm("status", sender=SENDER_USER), [])
        self.assertEqual(self.dm("status", sender=chat_tests.STRANGER_USER), [])

    def test_a_reply_is_dropped_when_the_conversation_stops_being_the_direct_message_before_it_is_sent(self):
        real = chat_chief._still_dm

        def joined_meanwhile(record, token):
            self.google.members[DM][SENDER] = SENDER_USER
            return real(record, token)

        with mock.patch.object(chat_chief, "_still_dm", side_effect=joined_meanwhile):
            self.assertEqual(self.dm("status"), [])
        [(_, record)] = chat_chief._records()
        self.assertEqual((record["state"], record["outcome"]), ("done", "not-dm"))

    def test_close_drop_and_watch_stay_in_the_terminal(self):
        self.task_md()
        task_id = "tk_0123456789abcdef"
        for text in (f"close {task_id}", f"drop {task_id}", f"Mischief managed {task_id}",
                     "Mischief managed everything", f"watch {task_id}"):
            self.assertEqual(self.dm(text), [chat_chief.TERMINAL_ONLY])
        self.assertFalse(self.registered(task_id))

    def test_help_and_lines_that_only_look_like_gos(self):
        self.assertEqual(self.dm("help"), [chat_chief.HELP])
        [reply] = self.dm("- go tk_0123456789abcdef")
        self.assertTrue(reply.startswith(hook.GO_EXACT), reply)
        self.assertEqual(self.turns, [])


class DraftTests(ChiefCase):
    def test_a_drafting_turn_writes_only_task_md_and_replies_with_the_summary(self):
        before = self.castle_files()
        task_id = self.drafted()
        [turn] = self.turns
        argv = turn["argv"]
        self.assertIn("--restricted", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--model") + 1], config.CHAT_CHIEF_MODEL)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], config.CHAT_CHIEF_MAX_BUDGET_USD)
        self.assertEqual(argv[argv.index("--json-schema") + 1], chat_chief.ANSWER_SCHEMA)
        self.assertEqual(turn["files"], [chat_chief.INPUT_FILE])
        self.assertEqual(turn["brief"], chat_chief.BRIEF)
        self.assertEqual(turn["timeout"], config.CHAT_CHIEF_TIMEOUT_SECONDS)
        nonce = re.search(r"OWNER MESSAGE ([0-9a-f]{16})\n", turn["input"]).group(1)
        self.assertIn(f"OWNER MESSAGE {nonce}\n{ASK}\nEND OWNER MESSAGE {nonce}", turn["input"])
        after = self.castle_files()
        self.assertEqual(set(after) - set(before), {f"tasks/{task_id}/TASK.md"})
        self.assertEqual({name: data for name, data in after.items() if name in before}, before)
        raw = after[f"tasks/{task_id}/TASK.md"]
        text = raw.decode()
        self.assertTrue(text.startswith(f"# {task_id} Add the widget check\n\n## Intent\n{ASK}\n"), text)
        spec = hook.read_spec(raw, task_id)
        self.assertEqual((spec["repo_dir"], spec["branch"], spec["base"]), (str(self.repo), "fix/widget",
                                                                            "origin/main"))
        self.assertFalse(self.registered(task_id))
        [reply] = self.chief_posts()
        self.assertIn(f"Drafted {task_id}: \"Add the widget check\"", reply["text"])
        self.assertIn(f"Repo {self.repo}, branch fix/widget from origin/main, 2 acceptance criteria:", reply["text"])
        self.assertIn(f"Send go {task_id} to start it", reply["text"])
        self.assertNotIn("OWNER-WORDS-7731", (self.office / "logs" / chat_review.LOG).read_text())
        [(_, record)] = chat_chief._records()
        self.assertNotIn("text", record)  # the owner's words are kept only until the turn has run

    def test_the_earlier_messages_and_open_drafts_are_context(self):
        task_id = self.drafted()
        self.answer = ("ok", json.dumps({"action": "reply", "reply": "Which repo? <users/all> see https://x.example"}))
        [reply] = self.dm("and another one, about the docs")
        self.assertNotIn("<users/all>", reply)
        self.assertNotIn("https://", reply)
        turn = self.turns[-1]
        self.assertIn(f"FROM OWNER: {ASK}", turn["input"])
        self.assertIn(f"=== {task_id} ===", turn["input"])
        self.assertIn(f"# {task_id} Add the widget check", turn["input"])

    def test_an_answer_that_is_not_a_valid_draft_writes_nothing(self):
        before = self.castle_files()
        bad = (
            {"spec": "repo: /elsewhere\nDesk: Harry."},
            {"spec": "## Spec\nmore"},
            {"criteria": [{"what": "two", "label": "check", "check": "`ls` and `ls`"}]},
            {"criteria": []},
            {"repo": "/nowhere/at/all"},
            {"branch": "Not A Branch"},
            {"title": "two\nlines"},
            {"action": "revise", "task_id": "tk_0123456789abcdef"},
            {"extra": "field"},
        )
        for fields in bad:
            with self.subTest(fields=fields):
                self.answer = self.draft_answer(**fields)
                [reply] = self.dm(ASK)
                self.assertTrue(reply.startswith("I couldn't make a valid draft from that"), reply)
        self.answer = ("ok", "not json at all")
        [reply] = self.dm(ASK)
        self.assertTrue(reply.startswith("I couldn't make a valid draft from that"), reply)
        self.answer = ("failed", "")
        self.assertEqual(self.dm(ASK), [chat_chief.COULD_NOT])
        self.assertEqual(self.castle_files(), before)

    def test_a_revision_rewrites_only_its_own_untouched_draft(self):
        task_id = self.drafted()
        self.answer = self.draft_answer(action="revise", task_id=task_id, title="Add the widget check, twice")
        [reply] = self.dm("make it check twice")
        self.assertTrue(reply.startswith(f"Revised {task_id}: \"Add the widget check, twice\""), reply)
        text = (self.castle / "tasks" / task_id / "TASK.md").read_text()
        self.assertIn(f"## Intent\n{ASK}\n\nmake it check twice\n", text)
        # Edited by hand since: left as it is.
        (self.castle / "tasks" / task_id / "TASK.md").write_text(text + "\nby hand\n")
        [reply] = self.dm("and thrice")
        self.assertIn("changed since I wrote it", reply)
        self.assertTrue((self.castle / "tasks" / task_id / "TASK.md").read_text().endswith("by hand\n"))

    def test_a_revision_of_a_task_this_app_did_not_draft_is_refused(self):
        self.task_md()
        before = self.castle_files()
        self.answer = self.draft_answer(action="revise", task_id="tk_0123456789abcdef")
        [reply] = self.dm("change that task")
        self.assertIn("names none of the open drafts", reply)
        self.assertEqual(self.castle_files(), before)

    def test_the_daily_turn_cap_holds(self):
        self.patch(config, "CHAT_CHIEF_TURNS_PER_DAY", 1)
        self.drafted()
        self.assertEqual(self.dm("one more"), [chat_chief.CAPPED])
        self.assertEqual(len(self.turns), 1)

    def test_a_stop_holds_the_turn_uncounted(self):
        with mock.patch.object(run_desk, "check_report_launch", side_effect=run_desk.Stopped("stopped")):
            self.assertEqual(self.dm(ASK), [])
        self.assertEqual(self.turns, [])
        self.answer = self.draft_answer()
        self.run_pass()
        self.assertEqual(len(self.turns), 1)
        self.assertTrue(self.chief_posts()[-1]["text"].startswith("Drafted tk_"))


class GoTests(ChiefCase):
    def test_a_go_needs_its_code_and_then_runs_the_hooks_own_go(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        self.assertFalse(self.registered(task_id))
        self.assertEqual(self.spawn.call_count, 0)
        self.assertNotIn(code, self.office_text())  # only a digest of it is kept
        [reply] = self.dm(f"go {task_id} {code}")
        self.assertTrue(reply.startswith(f"Go: {task_id} is registered"), reply)
        self.assertTrue(self.registered(task_id))
        self.assertEqual(self.harry_task(task_id)["desk"], "harry")
        [event] = self.chat_events()
        self.assertEqual((event["kind"], event["verdict"], event["task_id"]), ("go.confirmed", "headmaster", task_id))
        self.assertTrue(event["summary"].startswith("Go from Chat: Go: "), event["summary"])

    def test_the_code_works_once(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        self.dm(f"Go {task_id} {code.lower()}")
        self.assertTrue(self.registered(task_id))
        [reply] = self.dm(f"go {task_id} {code}")
        self.assertIn("no go is waiting for a code for it", reply)
        self.assertEqual(len(self.chat_events()), 1)

    def test_a_wrong_code_is_refused_and_three_void_it(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        wrong = "ZZZZZZ" if code != "ZZZZZZ" else "YYYYYY"
        for _ in range(chat_chief.CODE_TRIES):
            [reply] = self.dm(f"go {task_id} {wrong}")
            self.assertIn("that code is wrong", reply)
        [reply] = self.dm(f"go {task_id} {code}")
        self.assertIn("no go is waiting for a code for it", reply)
        self.assertFalse(self.registered(task_id))
        self.assertEqual(self.chat_events(), [])

    def test_a_code_expires(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        later = int(time.time()) + config.CHAT_CHIEF_GO_CODE_SECONDS + 5
        reply = chat_chief.confirm_go(self.conn, [(task_id, code)], "0" * 32, later)
        self.assertIn("its code expired", reply)
        self.assertFalse(self.registered(task_id))

    def test_a_task_md_changed_after_its_code_starts_nothing(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        path = self.castle / "tasks" / task_id / "TASK.md"
        path.write_text(path.read_text() + "\none more line\n")
        [reply] = self.dm(f"go {task_id} {code}")
        self.assertIn("TASK.md changed since its code was sent", reply)
        self.assertFalse(self.registered(task_id))

    def test_a_go_gets_the_hooks_own_refusals(self):
        missing = "tk_" + "e" * 16
        [reply] = self.dm(f"go {missing}")
        self.assertIn(f"there is no TASK.md at ~/hogwarts/tasks/{missing}/TASK.md", reply)
        self.assertIn("Fix: ", reply)
        broken = "tk_" + "d" * 16
        self.task_md(broken, spec="branch: fix/widget\nrepo: /x\nbase: origin/main\n")
        [reply] = self.dm(f"go {broken}")
        self.assertIn(hook.SPEC_BLOCK, reply)
        self.assertNotIn("within", reply)
        # One that passes the read-only checks but meets an existing branch once its code is sent: the hook refuses.
        self.task_md()
        code = self.code_for("tk_0123456789abcdef")
        self.git("branch", "fix/widget")
        [reply] = self.dm(f"go tk_0123456789abcdef {code}")
        self.assertTrue(reply.startswith("Go was not applied to tk_0123456789abcdef:"), reply)
        self.assertFalse(self.registered("tk_0123456789abcdef"))
        [event] = self.chat_events()
        self.assertEqual(event["kind"], "go.refused")
        self.assertTrue(event["summary"].startswith("Go from Chat: Go was not applied"), event["summary"])

    def test_a_go_for_a_registered_task_says_so_and_issues_no_code(self):
        task_id = self.drafted()
        self.dm(f"go {task_id} {self.code_for(task_id)}")
        [reply] = self.dm(f"go {task_id}")
        self.assertIn("is already registered", reply)
        self.assertIsNone(CODE_LINE.search(reply))

    def test_several_gos_each_get_their_own_code(self):
        first = self.drafted()
        self.answer = self.draft_answer(branch="fix/second")
        [reply] = self.dm("a second one")
        second = re.match(r"Drafted (tk_[0-9a-f]{16}):", reply).group(1)
        [reply] = self.dm(f"go {first}\ngo {second}")
        codes = dict(CODE_LINE.findall(reply))
        self.assertEqual(set(codes), {first, second})
        [reply] = self.dm(f"go {first} {codes[first]}\ngo {second} {codes[second]}")
        self.assertTrue(self.registered(first) and self.registered(second), reply)
        [reply] = self.dm(f"go {first} {codes[first]}\ngo {second}")
        self.assertIn("separate messages", reply)

    def test_a_go_from_anyone_else_or_anywhere_else_starts_nothing(self):
        task_id = self.drafted()
        self.dm(f"go {task_id}", sender=SENDER_USER, space=SPACE, mention=True)
        self.dm(f"go {task_id}", space=SPACE, mention=True)
        self.google.members[DM][SENDER] = SENDER_USER
        self.dm(f"go {task_id}")
        self.assertFalse(self.registered(task_id))
        codes = self.office / chat_review.DIR / chat_chief.CODES
        self.assertEqual(os.listdir(codes) if codes.exists() else [], [])

    def test_no_code_reaches_a_drafting_turn(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        # Her own reply with the code, as Chat lists it in the conversation, and the owner's wrong try.
        self.google.posted[DM].append({"name": f"{DM}/messages/own-1", "thread": {"name": f"{DM}/threads/t1"},
                                       "createTime": google_chat.rfc3339(int(time.time() * 1e6)),
                                       "sender": {"name": CHIEF_APP_USER, "type": "BOT", "displayName": "Front Desk"},
                                       "text": f"To start it, send go {task_id} {code} within 10 minutes.",
                                       "space": {"name": DM}})
        self.dm(f"go {task_id} {code.lower()}x")
        self.answer = ("ok", json.dumps({"action": "reply", "reply": "Noted."}))
        self.dm("one more thing")
        turn = self.turns[-1]
        self.assertIn(f"FROM YOU: To start it, send go {task_id} [code] within", turn["input"])
        self.assertIn(f"FROM OWNER: go {task_id} [code]", turn["input"])
        for shown in (code, code.lower()):
            self.assertNotIn(f"{task_id} {shown}", turn["input"])

    def test_a_go_cut_off_part_way_is_never_run_again(self):
        task_id = self.drafted()
        code = self.code_for(task_id)
        with mock.patch.object(chat_chief, "confirm_go", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.dm(f"go {task_id} {code}")
        self.run_pass()
        self.assertIn("cut off part way", self.chief_posts()[-1]["text"])
        self.assertFalse(self.registered(task_id))


class UpdateTests(ChiefCase):
    def line(self, name: str = "0a1b2c3d.tk_0123456789abcdef." + "e" * 32, text: str = "tk_0123456789abcdef / no"
             " build: go confirmed, build started. Nothing for you.") -> str:
        go_watch._to_chat(name, text)
        return name

    def test_go_updates_post_in_the_direct_message_while_both_switches_are_on(self):
        self.switch(config.GO_UPDATES_FILE, True)
        self.switch(config.CHAT_GO_UPDATES_FILE, True)
        self.dm("status")  # the direct message is known from the owner's message
        name = self.line()
        self.run_pass()
        post = self.chief_posts()[-1]
        self.assertEqual((post["space"], post["app"]), (DM, "mcgonagall"))
        self.assertTrue(post["text"].startswith("tk_0123456789abcdef / no build: go confirmed"), post["text"])
        self.assertFalse((self.office / config.CHAT_GO_UPDATES_DIR / f"{name}.json").exists())
        count = len(self.chief_posts())
        self.line()  # the same line again is a new file, but Chat keeps one message per request id
        self.run_pass()
        self.assertEqual(len(self.chief_posts()), count)

    def test_no_line_is_left_or_posted_while_the_switch_is_off(self):
        self.switch(config.GO_UPDATES_FILE, True)
        self.dm("status")
        self.line()
        self.assertFalse((self.office / config.CHAT_GO_UPDATES_DIR).exists())
        self.switch(config.CHAT_GO_UPDATES_FILE, True)
        self.line()
        self.switch(config.CHAT_GO_UPDATES_FILE, False)
        count = len(self.chief_posts())
        self.run_pass()
        self.assertEqual(len(self.chief_posts()), count)
        self.assertEqual(os.listdir(self.office / config.CHAT_GO_UPDATES_DIR), [])

    def test_no_line_goes_to_a_conversation_that_is_no_longer_the_direct_message(self):
        self.switch(config.GO_UPDATES_FILE, True)
        self.switch(config.CHAT_GO_UPDATES_FILE, True)
        self.dm("status")
        self.google.members[DM][SENDER] = SENDER_USER
        self.line()
        count = len(self.chief_posts())
        self.run_pass()
        self.assertEqual(len(self.chief_posts()), count)

    def test_the_go_watch_leaves_each_line_it_sends(self):
        self.switch(config.GO_UPDATES_FILE, True)
        self.switch(config.CHAT_GO_UPDATES_FILE, True)
        with mock.patch.object(go_watch, "current", return_value={}):
            go_watch.watch(self.conn)  # the baseline
        key = go_watch._key(None, "confirmed")
        with mock.patch.object(go_watch, "current", return_value={"tk_0123456789abcdef": (key, None)}), \
                mock.patch.object(go_watch.phone, "send", return_value={"state": "sent"}):
            go_watch.watch(self.conn)
        [name] = os.listdir(self.office / config.CHAT_GO_UPDATES_DIR)
        data = json.loads((self.office / config.CHAT_GO_UPDATES_DIR / name).read_text())
        self.assertEqual(data["line"], go_watch.line("tk_0123456789abcdef", key))


if __name__ == "__main__":
    unittest.main()
