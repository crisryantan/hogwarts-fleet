"""Fleet answers in Chat: the owner asks an app what the fleet has in flight, or where a PR the fleet opened stands, in a
direct message with the app, and gets fixed lines read from the store with no turn. Anyone else gets the help reply, and
the owner in a conversation with other people gets one line pointing at the direct message. Chat, gh and every turn are
fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the machine and no model runs."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest import mock

from hogwarts import capacity, db, followups, ids, pensieve
from fleet import chat_fleet, chat_review, config, google_chat
from tests_fleet.test_chat_review import LEAD, LEAD_USER, MENTION, SENDER, SENDER_USER, SPACE, ChatCase, msg

DM = "spaces/DMDM"
REPO = "acme/web-app"
SHAS = ["1" * 40, "2" * 40, "3" * 40]
FLEET_PR = "https://github.com/acme/web-app/pull/7"
OTHER_PR = "https://github.com/acme/web-app/pull/99"
SECRET_BRANCH = "fix/secret-branch-name"


class FleetCase(ChatCase):
    """The owner is LEAD, who has a direct message with the app (DM) as well as the shared space."""

    def setUp(self) -> None:
        super().setUp()
        self.patch(config, "CHAT_REVIEW_OWNER", LEAD)
        self.patch(config, "CHAT_REVIEW_SPACES", (SPACE, DM))
        self.google.members[DM] = {LEAD: LEAD_USER}
        self.google.app_in.add(DM)
        started = int((time.time() - 60) * 1e6)
        chat_review._save_marker(DM, "hermione", started, started, True)
        self.stamp = int(time.time())
        self.shas, self.heads = 0, {}  # commits made so far, and the one each task's newest round read

    # the store

    def tick(self) -> int:
        self.stamp += 1
        return self.stamp

    def task(self, title: str, desk: str = "harry", start: bool = True, parent: str = None) -> str:
        task_id = pensieve.create_task(self.conn, desk, title, parent_task_id=parent, now=self.tick())["id"]
        if start:
            pensieve.start_task(self.conn, task_id, now=self.tick())
        return task_id

    def fresh(self) -> str:
        self.shas += 1
        return f"{self.shas:040x}"

    def review(self, task_id: str, verdict: str = None, reviewer: str = "hermione", sha: str = None) -> str:
        """A review round of the task at sha (a fresh commit when none), with its verdict when given; its request's id."""
        sha = sha or self.fresh()
        self.heads[task_id] = sha
        opened = capacity.open_review_round(self.conn, task_id, reviewer, sha, "review",
                                            idempotency_key=ids.new_id("request"), now=self.tick())
        if verdict is not None:
            pensieve.record_commit(self.conn, task_id, REPO, sha, now=self.tick())
            pensieve.start_task(self.conn, opened["task"]["id"], now=self.tick())
            capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, verdict, now=self.tick())
        return opened["request"]["id"]

    def passed(self, task_id: str, sha: str = None) -> None:
        self.review(task_id, "PASS", sha=sha)
        pensieve.set_worktree(self.conn, task_id, f"{ids.WORKTREES_ROOT}/{task_id}")
        pensieve.mark_awaiting_close(self.conn, task_id, now=self.tick())

    def bind(self, task_id: str, number: int = 7) -> None:
        followups.bind_pr(self.conn, task_id, REPO, number, SECRET_BRANCH, "main", self.heads[task_id],
                          f"https://github.com/{REPO}/pull/{number}", now=self.tick())

    def map_saw(self, number: int, head: str, **fields) -> None:
        """The Map's last snapshot, with that PR in it."""
        record = {"repo": REPO, "number": number, "draft": True, "head": head, "checks": "SUCCESS", "failing": [],
                  **fields}
        folder = self.office / config.PATROL_DIR / "map"
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.office / config.PATROL_DIR).chmod(0o700)
        self.write_file(folder / chat_fleet.SNAPSHOT_FILE, json.dumps(
            {"taken_at": int(time.time()) - 180, "prs": {f"{REPO}#{number}": record}, "asked": {}}))

    # the chat

    def ask(self, text: str, sender: str = LEAD_USER, space: str = DM, message: str = "f1") -> list:
        """Deliver one mention and run a pass: the outcomes it took, and the reply it posted last, if any."""
        before = len(self.google.messages)
        self.deliver(msg(text=text, sender=sender, space=space, message=message, at=time.time() + 1))
        taken = [outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"]
        self.last = MENTION.sub("", self.google.messages[-1]["text"], count=1) \
            if len(self.google.messages) > before else None
        return taken

    def counts(self) -> dict:
        return {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "events", "review_rounds", "run_launches", "metrics", "task_prs", "owls")}


class GrammarTests(unittest.TestCase):
    def test_the_grammar_takes_two_fixed_asks_and_nothing_else(self):
        for text in ("fleet status", "Fleet status?", "hey, fleet status update", "what's the fleet doing",
                     "What’s the fleet doing right now?", "whats the fleet up to", "what is the fleet working on"):
            with self.subTest(text=text):
                self.assertEqual(chat_review.parse_fleet(text), ("status", None))
        for text, link in ((f"fleet {FLEET_PR}", (REPO, 7)), (f"fleet status {FLEET_PR}", (REPO, 7)),
                           (f"Fleet <{FLEET_PR}>", (REPO, 7)), (f"ok fleet <{FLEET_PR}|the PR>", (REPO, 7)),
                           (f"fleet {OTHER_PR}.", (REPO, 99)), ("fleet https://github.com/other/thing/pull/3",
                                                                ("other/thing", 3))):
            with self.subTest(text=text):
                self.assertEqual(chat_review.parse_fleet(text), ("pr", link))

    def test_anything_longer_or_looser_is_no_fleet_ask(self):
        for text in ("", None, "status", "fleet", "fleet status now please and then some", "what is a fleet?",
                     "fleet status of the deploy", "the fleet status", f"review {FLEET_PR}", f"status {FLEET_PR}",
                     f"fleet {FLEET_PR} {OTHER_PR}", f"fleet status {FLEET_PR} https://example.org/x",
                     "fleet status https://example.org/x", "fleet <https://example.org/x|status>",
                     f"fleet <{FLEET_PR}|{OTHER_PR}>", f"fleet please tell me everything about {FLEET_PR}",
                     "fleet status for acme/web-app#7", f"fleet {FLEET_PR}/files"[:-6] + "x"):
            with self.subTest(text=text):
                self.assertIsNone(chat_review.parse_fleet(text))


class StageTests(FleetCase):
    def stages(self) -> dict:
        text = chat_fleet.fleet_text(int(time.time()), chat_review._memory_text)
        return {line.split(": ", 1)[0][2:]: line.rsplit(" | ", 1)[1] for line in text.splitlines()
                if line.startswith("- ") and " | " in line}

    def test_stages_map_from_the_stores_states(self):
        queued = self.task("queued one", start=False)
        building = self.task("building one")
        reviewing = self.task("reviewing one")
        self.review(reviewing)
        passed = self.task("passed one")
        self.passed(passed)
        drafted = self.task("drafted one")
        self.passed(drafted)
        self.bind(drafted, 7)
        merging = self.task("merging one")
        self.passed(merging)
        self.bind(merging, 8)
        self.map_saw(8, self.heads[merging], draft=False)
        owner = self.task("owner one")
        self.review(owner, "HEADMASTER")
        tooling = self.task("tooling one")
        self.review(tooling, "CHANGES")
        pensieve.add_event(self.conn, "harry", "review.blocked-on-tooling", "headmaster", "the sandbox", task_id=tooling,
                           now=self.tick())
        capped = self.task("capped one")
        self.review(capped, "CHANGES")
        found = self.stages()
        with mock.patch.object(config, "REVIEW_ROUND_CAP", 1):
            capped_stage = self.stages()[capped]
        self.assertEqual(found[queued], "queued")
        self.assertEqual(found[building], "building")
        self.assertEqual(found[reviewing], "in review round 1")
        self.assertEqual(found[passed], "passed")
        self.assertEqual(found[drafted], "draft PR open")
        self.assertEqual(found[merging], "awaiting merge")
        self.assertEqual(found[owner], "blocked: needs the owner")
        self.assertEqual(found[tooling], "blocked: tooling")
        self.assertEqual(found[capped], "building")  # CHANGES with a round to spare: the fix round is under way
        self.assertEqual(capped_stage, "blocked: round cap")

    def test_a_changes_verdict_with_a_round_to_spare_is_a_fix_round_under_way(self):
        fixing = self.task("fixing one")
        self.review(fixing, "CHANGES")
        self.assertEqual(self.stages()[fixing], "building")

    def test_a_go_task_is_folded_into_its_open_build_and_a_review_rounds_task_is_never_listed(self):
        go = self.task("the go task", desk="mcgonagall")
        build = self.task("the build", parent=go)
        self.review(build)
        found = self.stages()
        self.assertEqual(found, {build: "in review round 1"})

    def test_the_last_five_closed_today_are_listed_with_how_they_closed(self):
        old = self.task("closed long ago")
        pensieve.close_task(self.conn, old, "abandoned", now=int(time.time()) - 5 * 86400)
        closed = []
        for index in range(7):
            closed.append(self.task(f"closed {index}", desk="ron"))
            pensieve.close_task(self.conn, closed[-1], "abandoned", now=int(time.time()))
        text = chat_fleet.fleet_text(int(time.time()), chat_review._memory_text)
        self.assertIn("Nothing open.", text)
        lines = text.splitlines()
        self.assertEqual(lines[1], "Closed today (latest 5):")
        self.assertEqual([line.split(": ")[0][2:] for line in lines[2:]], closed[:1:-1])
        self.assertTrue(all(line.endswith("| closed (abandoned)") for line in lines[2:]))
        self.assertNotIn(old, text)

    def test_only_the_newest_open_tasks_are_shown_with_a_count_of_the_rest(self):
        for index in range(chat_fleet.OPEN_MAX + 3):
            self.task(f"queued {index}", start=False)
        lines = chat_fleet.fleet_text(int(time.time()), chat_review._memory_text).splitlines()
        self.assertEqual(lines[0], f"Open ({chat_fleet.OPEN_MAX + 3}):")
        self.assertIn(f"queued {chat_fleet.OPEN_MAX + 2}", lines[1])
        self.assertEqual(lines[chat_fleet.OPEN_MAX + 1], "(3 more not shown)")


class RoundLineTests(unittest.TestCase):
    def test_a_round_reads_as_how_it_stands_with_its_reviewer_and_head(self):
        row = {"round": 2, "reviewer": "moody", "sha": SHAS[0], "superseded_by": None, "has_verdict": False,
               "waiting": False, "counts": False, "verdict": None}
        head = f" by moody at {SHAS[0][:12]}"
        for change, how in (({}, "ended with no verdict"), ({"waiting": True}, "waiting for its reviewer"),
                            ({"counts": True}, "in progress"), ({"has_verdict": True, "verdict": "PASS"}, "PASS"),
                            ({"superseded_by": "rq_" + "0" * 16, "waiting": True}, "superseded by a newer commit")):
            with self.subTest(how=how):
                self.assertEqual(chat_fleet._round_line({**row, **change}), f"- round 2: {how}{head}")


class OwnerTests(FleetCase):
    def test_the_owner_in_a_direct_message_gets_the_list_with_no_turn(self):
        building = self.task("Fix the widget")
        reviewing = self.task("Add the gadget")
        self.review(reviewing)
        done = self.task("Tidy the shed", desk="ron")
        pensieve.close_task(self.conn, done, "abandoned", now=int(time.time()))
        self.assertEqual(self.ask("fleet status"), ["fleet"])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.last.splitlines(), [
            "Open (2):", f"- {reviewing}: \"Add the gadget\" | in review round 1",
            f"- {building}: \"Fix the widget\" | building", "Closed today (latest 1):",
            f"- {done}: \"Tidy the shed\" | closed (abandoned)"])
        [record] = [record for record in self.records() if record["kind"] == "fleet"]
        self.assertEqual((record["state"], record["outcome"]), ("done", "fleet"))
        self.assertEqual(self.google.messages[-1]["thread"], f"{DM}/threads/t1")

    def test_the_owner_in_a_direct_message_gets_the_pr_view(self):
        build = self.task("Fix the widget")
        self.review(build, "CHANGES", reviewer="moody", sha=SHAS[0])
        self.passed(build, SHAS[1])
        self.bind(build, 7)
        self.map_saw(7, SHAS[1], draft=True, checks="FAILURE", failing=["unit", "lint"])
        self.assertEqual(self.ask(f"fleet {FLEET_PR}"), ["fleet"])
        self.assertEqual(self.last.splitlines(), [
            f"{REPO}#7: task {build}, \"Fix the widget\", draft PR open.",
            "Opened by the fleet just now.",
            "Review rounds (2):", f"- round 1: CHANGES by moody at {SHAS[0][:12]}",
            f"- round 2: PASS by hermione at {SHAS[1][:12]}", f"Head reviewed: {SHAS[1][:12]}.",
            f"CI as the Map last saw it (head {SHAS[1][:12]}, 3m ago): failing (2 checks).",
            "Open follow-ups: none."])
        self.assertEqual(self.ask(f"fleet status {FLEET_PR}", message="f2"), ["fleet"])
        self.assertEqual(self.last.splitlines()[0], f"{REPO}#7: task {build}, \"Fix the widget\", draft PR open.")

    def test_a_pr_view_names_an_open_follow_up_and_never_what_a_teammate_wrote(self):
        build = self.task("Fix the widget")
        self.passed(build)
        self.bind(build)
        item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": "5", "quote": None,
                "url": f"https://github.com/{REPO}/pull/7#issuecomment-5"}
        followups.open_followup(self.conn, build, ids.new_id("followup"), self.heads[build], [item],
                                [{"kind": "comment", "comment_id": "5", "label": "T1"}], "map", "follow-up",
                                "TEAMMATE-WORDS-THAT-MUST-STAY-OUT", config.FOLLOWUP_MAX_PER_TASK, now=self.tick())
        self.ask(f"fleet {FLEET_PR}")
        self.assertIn(f"task {build}, \"Fix the widget\", building.", self.last)
        self.assertIn("Open follow-ups (1): #1 ", self.last)
        self.assertIn("CI: the Map has not seen this PR.", self.last)
        self.assertNotIn("TEAMMATE-WORDS", self.last)
        self.assertNotIn("TEAMMATE-WORDS", json.dumps(self.records()))

    def test_a_pr_the_fleet_did_not_open_says_so_and_a_closed_one_shows_how_it_closed(self):
        self.assertEqual(self.ask(f"fleet {OTHER_PR}"), ["fleet"])
        self.assertEqual(self.last, "That PR isn't one the fleet opened.")
        self.assertEqual(self.ask("fleet https://github.com/other/thing/pull/3", message="f2"), ["fleet"])
        self.assertEqual(self.last, "That PR isn't one the fleet opened.")
        build = self.task("Fix the widget")
        self.passed(build)
        self.bind(build)
        pensieve.close_task(self.conn, build, "abandoned", now=self.tick())
        self.ask(f"fleet {FLEET_PR}", message="f3")
        self.assertEqual(self.last.splitlines()[0], f"{REPO}#7: task {build}, \"Fix the widget\", closed (abandoned).")

    def test_a_pr_of_any_repo_is_found_in_any_letter_case(self):
        build = self.task("Fix the widget")
        self.passed(build)
        self.bind(build)
        self.ask("fleet https://github.com/ACME/Web-App/pull/7")
        self.assertIn(f"task {build}", self.last)

    def test_the_fleet_ask_wins_over_a_review_request_for_the_same_link(self):
        self.assertEqual(self.ask(f"fleet {FLEET_PR}"), ["fleet"])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.ask(f"review {FLEET_PR}", message="f2"), ["review"])

    def test_the_store_unreadable_is_one_fixed_line(self):
        with mock.patch.object(db, "connect_readonly", side_effect=chat_review.StoreError("no store")):
            self.assertEqual(self.ask("fleet status"), ["fleet"])
        self.assertEqual(self.last, "I couldn't read the fleet's store just now.")


class WhereTests(FleetCase):
    """Fleet data goes only into a direct message: a conversation with the owner and the app alone."""

    def test_the_owner_in_a_shared_space_gets_the_dm_line_and_no_fleet_data(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        with mock.patch.object(chat_fleet, "fleet_text", side_effect=AssertionError("the store was read")), \
                mock.patch.object(chat_fleet, "pr_text", side_effect=AssertionError("the store was read")):
            self.assertEqual(self.ask("fleet status", space=SPACE), ["fleet"])
            self.assertEqual(self.last, "Ask me in our DM for fleet status.")
            self.assertEqual(self.ask(f"fleet {FLEET_PR}", space=SPACE, message="f2"), ["fleet"])
            self.assertEqual(self.last, "Ask me in our DM for fleet status.")
        self.assertNotIn("TITLE-THAT-MUST-STAY-OUT", json.dumps(self.google.messages))

    def test_a_conversation_with_anyone_else_in_it_is_no_direct_message(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        cases = {"a second joined person": lambda: self.google.members[DM].update({SENDER: SENDER_USER}),
                 "an invited person": lambda: self.google.invited.update({DM: ["users/444"]})}
        for index, (name, change) in enumerate(cases.items()):
            with self.subTest(name):
                change()
                self.assertEqual(self.ask("fleet status", message=f"w{index}"), ["fleet"])
                self.assertEqual(self.last, "Ask me in our DM for fleet status.")
                self.google.members[DM] = {LEAD: LEAD_USER}
                self.google.invited.clear()
        self.assertEqual(self.ask("fleet status", message="w9"), ["fleet"])
        self.assertIn("TITLE-THAT-MUST-STAY-OUT", self.last)

    def test_a_membership_list_that_cannot_be_read_whole_is_no_direct_message(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        self.google.members_fail[DM] = 403
        self.assertEqual(self.ask("fleet status"), ["fleet"])
        self.assertEqual(self.last, "Ask me in our DM for fleet status.")
        del self.google.members_fail[DM]
        with mock.patch.object(google_chat, "MEMBERS_PAGE_SIZE", 1):  # a list longer than one page is never private
            self.assertEqual(self.ask("fleet status", message="f2"), ["fleet"])
        self.assertEqual(self.last, "Ask me in our DM for fleet status.")

    def test_a_bot_other_than_the_app_makes_it_shared_too(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        real = google_chat.members
        with mock.patch.object(google_chat, "members", side_effect=lambda *args: real(*args) + [
                ("users/777", "BOT", "JOINED")]):
            self.assertEqual(self.ask("fleet status"), ["fleet"])
        self.assertEqual(self.last, "Ask me in our DM for fleet status.")


class NotTheOwnerTests(FleetCase):
    def test_another_allowed_sender_gets_the_help_reply_and_the_store_is_never_read(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        self.google.members[DM][SENDER] = SENDER_USER
        with mock.patch.object(chat_fleet, "fleet_text", side_effect=AssertionError("the store was read")), \
                mock.patch.object(chat_fleet, "pr_text", side_effect=AssertionError("the store was read")):
            for message, text in (("n1", "fleet status"), ("n2", f"fleet {FLEET_PR}"), ("n3", "what's the fleet doing")):
                with self.subTest(text=text):
                    self.assertEqual(self.ask(text, sender=SENDER_USER, message=message), ["help"])
                    self.assertEqual(self.last, chat_review.help_text("hermione"))
        self.assertEqual(self.turns, [])

    def test_an_ordinary_message_from_a_non_owner_is_answered_as_before(self):
        self.assertEqual(self.ask(f"review {FLEET_PR}", sender=SENDER_USER, space=SPACE), ["review"])
        self.assertEqual(self.ask("status", sender=SENDER_USER, space=SPACE, message="n2"), ["status"])
        self.assertNotIn("Fleet", self.last)

    def test_a_person_not_on_the_list_still_gets_the_refusal(self):
        self.assertEqual(self.ask("fleet status", sender="users/333", space=SPACE), ["refusal"])
        self.assertEqual(self.last, chat_review.SENDER_REFUSED)

    def test_while_the_owner_is_a_placeholder_nobody_gets_fleet_answers(self):
        self.task("TITLE-THAT-MUST-STAY-OUT")
        with mock.patch.object(config, "CHAT_REVIEW_OWNER", "<chat-review-owner-email>"):
            self.assertEqual(self.ask("fleet status"), ["help"])
            self.assertEqual(self.last, chat_review.help_text("hermione"))
            self.assertEqual(self.ask("fleet status", message="n2", sender=SENDER_USER, space=SPACE), ["help"])
        for value in ("<chat-review-owner-email>", "", None, 7, "not an email"):
            with self.subTest(owner=value), mock.patch.object(config, "CHAT_REVIEW_OWNER", value):
                self.assertFalse(chat_review._is_owner(LEAD))

    def test_the_owner_matches_without_letter_case_and_must_be_an_allowed_sender(self):
        with mock.patch.object(config, "CHAT_REVIEW_OWNER", LEAD.upper()):
            self.assertEqual(self.ask("fleet status"), ["fleet"])
            self.assertTrue(self.last.startswith("Nothing open."))
        for value, problem in ((LEAD, False), ("<chat-review-owner-email>", False), ("other@example.com", True),
                               ("nonsense", True)):
            with self.subTest(owner=value), mock.patch.object(config, "CHAT_REVIEW_OWNER", value):
                self.assertEqual(any("CHAT_REVIEW_OWNER" in text for text in chat_review.config_problems(True)),
                                 problem)


class AllowedFieldsTests(FleetCase):
    def test_nothing_but_the_allowed_fields_appears(self):
        title = ("Fix <users/all> the widget at dev@example.com with `code` and ya29.abcdefghijklmnopqrstuvwxyz0123456789"
                 " plus a very long tail that goes on well past the sixty characters a title may show")
        build = self.task(title)
        self.passed(build)
        self.bind(build)
        pensieve.add_event(self.conn, "harry", "review.verdict", "routine", "EVENT-SUMMARY-THAT-MUST-STAY-OUT",
                           task_id=build, now=self.tick())
        self.map_saw(7, self.heads[build], failing=["CHECK-NAME-THAT-MUST-STAY-OUT"], checks="FAILURE")
        self.ask("fleet status")
        listing = self.last
        self.ask(f"fleet {FLEET_PR}", message="f2")
        for text in (listing, self.last):
            for absent in ("dev@example.com", "<users/all>", "`", "ya29.", "EVENT-SUMMARY", "CHECK-NAME", SECRET_BRANCH,
                           "main", "TASK.md", "worktrees", "@"):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, text)
        title_line = [line for line in listing.splitlines() if build in line][0]
        shown = title_line.split('"')[1]
        self.assertLessEqual(len(shown), chat_fleet.TITLE_MAX)
        self.assertTrue(shown.startswith("Fix (users/all) the widget at [email]"))

    def test_the_record_keeps_no_title_beyond_the_reply(self):
        build = self.task("Fix the widget")
        self.ask("fleet status")
        [record] = [record for record in self.records() if record["kind"] == "fleet"]
        self.assertNotIn("question", record)
        self.assertEqual(json.dumps(record).count("Fix the widget"), 1)  # in the reply alone
        self.assertIn(build, record["reply"])

    def test_a_long_list_is_cut_whole_lines_to_what_chat_takes(self):
        for index in range(chat_fleet.OPEN_MAX):
            self.task("T" * 60 + str(index), start=False)
        with mock.patch.object(chat_review, "MENTION_ROOM", google_chat.MESSAGE_MAX_CHARS - 400):
            self.ask("fleet status")
        self.assertLessEqual(len(self.last), 400)
        self.assertRegex(self.last.splitlines()[-1], r"\([0-9]+ more lines not shown\)")


class ReadOnlyTests(FleetCase):
    def office_files(self) -> list:
        return sorted(str(path.relative_to(self.office)) for path in self.office.rglob("*"))

    def test_reading_changes_nothing_in_the_store_or_the_office(self):
        build = self.task("Fix the widget")
        self.passed(build)
        self.bind(build)
        before = (self.counts(), self.conn.execute("PRAGMA data_version").fetchone()[0], self.office_files())
        now = int(time.time())
        with mock.patch.object(db, "connect_readonly", wraps=db.connect_readonly) as opened, \
                mock.patch.object(db, "connect", side_effect=AssertionError("a writable store was opened")):
            chat_fleet.fleet_text(now, chat_review._memory_text)
            chat_fleet.pr_text(REPO, 7, now, chat_review._memory_text, chat_review._ago)
            chat_fleet.pr_text(REPO, 99, now, chat_review._memory_text, chat_review._ago)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual((self.counts(), self.conn.execute("PRAGMA data_version").fetchone()[0], self.office_files()),
                         before)
        self.assertFalse((self.office / config.PATROL_DIR).exists())  # no snapshot to read is no folder made

    def test_the_store_is_opened_read_only_so_a_write_through_it_is_refused(self):
        conn = db.connect_readonly(config.DB_PATH)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.Error):
            conn.execute("UPDATE tasks SET title = 'x'")


if __name__ == "__main__":
    unittest.main()
