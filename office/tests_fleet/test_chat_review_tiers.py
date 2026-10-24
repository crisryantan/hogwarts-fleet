"""Chat review tiers: a review and a question about one run on the reviewing desk's own model, and a mention answer or a
conversation turn on the workhorse line of the desk's own family, from Ollivander's ladder, at the chat budget. gh, Chat
and every turn are fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the machine and no model runs."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from fleet import chat_material, chat_review, config, failover, run_desk
from hogwarts import capacity, wands
from tests_fleet.support import IN_KIT, NOW, ONLY_IN_KIT, kit_setting
from tests_fleet.test_chat_review import MOODY_APP_USER, ChatCase, block, msg

QUESTION = "What is a race condition?"
ANSWER = "Two threads touch shared state and the result depends on who gets there first."
CLAUDE_LADDER = [{"model": "opus", "effort": "high", "line": "frontier"},
                 {"model": "sonnet", "effort": "medium", "line": "workhorse"},
                 {"model": "haiku", "effort": "low", "line": "fast"}]
CODEX_LADDER = [{"model": "gpt-6-astra", "effort": "high", "line": "frontier"},
                {"model": "gpt-6.1-sol", "effort": "medium", "line": "workhorse"},
                {"model": "gpt-6-luna", "effort": "low", "line": "fast"}]


class TierCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch(config, "CHAT_REVIEW_PER_PERSON_DAILY", 20)
        self.patch(config, "CHAT_REVIEW_CHAT_BUDGET_USD", "0.25")
        self.patch(config, "CHAT_REVIEW_QUESTION_BUDGET_USD", "0.50")
        self.patch(config, "CHAT_REVIEW_ESCALATE_CHARS", 20_000)
        self.patch(config, "BLOCKED_MODEL_PREFIXES", ())
        failover.write_ladders({"hermione": {"claude": CLAUDE_LADDER, "codex": CODEX_LADDER},
                                "moody": {"claude": CLAUDE_LADDER, "codex": CODEX_LADDER}}, NOW)
        self.answer = ("ok", f"Here.\nANSWER\n{ANSWER}\nEND\n")

    def mention(self, text: str = QUESTION, message: str = "q1", **kwargs) -> list:
        self.deliver(msg(text=text, message=message, at=time.time() + 1, **kwargs))
        return [outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"]

    @staticmethod
    def flag(argv: list, name: str):
        return argv[argv.index(name) + 1] if name in argv else None

    @staticmethod
    def codex_values(argv: list) -> list:
        return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]

    def launched(self, desk: str = "hermione") -> list:
        """The models on the desk's launch rows, oldest first."""
        return [row["model"] for row in capacity.list_launches(self.conn, desk)]

    def record_of(self, message: str) -> dict:
        return next(record for record in self.records() if record["message"].endswith(f"/{message}"))


class TurnKindTests(TierCase):
    def test_a_review_and_a_question_about_it_run_on_the_desks_own_model(self):
        self.answer = ("ok", block())
        self.deliver(msg(at=time.time() - 5))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], ["review"])
        self.answer = ("ok", f"Here.\nANSWER\n{ANSWER}\nEND\n")
        self.assertEqual(self.mention("Why does that print matter?"), ["question"])
        review, question = self.turns
        self.assertEqual([self.flag(turn["argv"], "--model") for turn in (review, question)], ["opus", "opus"])
        self.assertEqual(self.flag(review["argv"], "--max-budget-usd"), config.MAX_BUDGET_USD["hermione"])
        self.assertEqual(self.flag(question["argv"], "--max-budget-usd"), "0.50")
        self.assertEqual(self.launched(), ["opus", "opus"])
        self.assertEqual([record["tier"] for record in self.records()], ["review", "review"])

    def test_a_mention_answer_runs_on_its_familys_workhorse_at_the_chat_budget(self):
        self.assertEqual(self.mention(), ["mention"])
        [turn] = self.turns
        argv = turn["argv"]
        self.assertEqual((self.flag(argv, "--model"), self.flag(argv, "--effort")), ("sonnet", "medium"))
        self.assertEqual(self.flag(argv, "--max-budget-usd"), "0.25")
        self.assertEqual(turn["model"], "sonnet")
        self.assertEqual(self.launched(), ["sonnet"])  # the launch row names the model that ran
        record = self.record_of("q1")
        self.assertEqual((record["outcome"], record["tier"]), ("answered", "chat"))

    def test_a_conversation_turn_runs_on_the_chat_tier_unless_its_thread_was_reviewed(self):
        self.google.echo = False
        self.assertEqual(self.mention("test test you there", message="m1"), ["mention"])
        self.deliver(msg(text="What is a widget store?", message="c1", mention=False, at=time.time() + 2))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], ["mention"])
        self.assertEqual(self.record_of("c1")["mentioned"], False)
        self.assertEqual([self.flag(turn["argv"], "--model") for turn in self.turns], ["sonnet", "sonnet"])
        # In a thread the app reviewed, the same turn is a question about the review: the review tier.
        thread = f"{config.CHAT_REVIEW_SPACES[0]}/threads/t9"
        self.answer = ("ok", block())
        self.deliver(msg(at=time.time() + 3, message="r1", thread=thread))
        self.run_pass()
        self.answer = ("ok", f"Here.\nANSWER\n{ANSWER}\nEND\n")
        self.deliver(msg(text="Why does that print matter?", message="c2", mention=False, at=time.time() + 4,
                         thread=thread))
        self.assertEqual([outcome for outcome in self.run_pass()["taken"] if outcome != "seen before"], ["question"])
        self.assertEqual([self.flag(turn["argv"], "--model") for turn in self.turns[2:]], ["opus", "opus"])
        self.assertEqual(self.flag(self.turns[-1]["argv"], "--max-budget-usd"), "0.50")

    def test_moodys_chat_tier_takes_the_codex_workhorse_and_his_reviews_keep_his_model(self):
        self.second_app()
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "role", now=NOW)
        self.assertEqual(self.mention(app=MOODY_APP_USER), ["mention"])
        [turn] = self.turns
        values = self.codex_values(turn["argv"])
        self.assertIn('model="gpt-6.1-sol"', values)
        self.assertIn('model_reasoning_effort="medium"', values)
        self.assertNotIn('model="gpt-6-astra"', values)
        self.assertNotIn("--max-budget-usd", turn["argv"])  # a Codex turn takes no budget
        self.assertEqual((turn["family"], turn["model"]), ("codex", "gpt-6.1-sol"))
        self.assertEqual(self.launched("moody"), ["gpt-6.1-sol"])
        review = run_desk.chat_review_argv(self.conn, chat_review.MOODY_BRIEF, "p", "chat-" + "1" * 16, "moody")
        self.assertEqual(review.model, "gpt-6-astra")
        self.assertIn('model="gpt-6-astra"', self.codex_values(review.argv))
        # A profile his codex.toml selects gets the chat model too, so its own model never outranks it.
        self.write_file(self.office / "desks" / "moody" / config.CODEX_PROFILE_FILE,
                        'profile = "deep"\n[profiles.deep]\nmodel = "gpt-deep"\n')
        chat = run_desk.chat_review_argv(self.conn, chat_review.MOODY_BRIEF, "p", "chat-" + "2" * 16, "moody",
                                         tier="chat")
        self.assertEqual(chat.model, "gpt-6.1-sol")
        self.assertIn('profiles.deep.model="gpt-6.1-sol"', self.codex_values(chat.argv))

    def test_a_tier_that_is_neither_is_refused(self):
        with self.assertRaises(chat_review.FleetError):
            run_desk.chat_review_argv(self.conn, "b", "p", "chat-" + "1" * 16, "hermione", tier="fast")


class FallbackTests(TierCase):
    def models(self, desk: str = "hermione") -> dict:
        return run_desk.chat_models(self.conn, desk)

    def test_the_workhorse_comes_from_the_same_familys_ladder(self):
        self.assertEqual(self.models(), {"review": "opus", "chat": "sonnet"})
        failover.write_ladders({"hermione": {"claude": [CLAUDE_LADDER[0], CLAUDE_LADDER[2]]}}, NOW)
        # The Codex ladder still has a workhorse, but a Claude desk never takes the other family's model.
        self.assertEqual(self.models(), {"review": "opus", "chat": "opus"})

    def test_a_blocked_or_missing_workhorse_falls_back_to_the_desks_own_model(self):
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-")):
            self.assertEqual(self.models(), {"review": "opus", "chat": "opus"})  # never haiku, the next line down
            self.assertEqual(self.mention(), ["mention"])
            self.assertEqual(self.flag(self.turns[-1]["argv"], "--model"), "opus")
            self.assertEqual(self.launched(), ["opus"])
        # The second workhorse of the line is taken when the first is blocked, and that is the model that launches.
        second = {**CLAUDE_LADDER[1], "model": "workhorse-two"}
        failover.write_ladders({"hermione": {"claude": [CLAUDE_LADDER[0], CLAUDE_LADDER[1], second]}}, NOW)
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("sonnet", "claude-sonnet-")):
            self.assertEqual(self.models(), {"review": "opus", "chat": "workhorse-two"})
            self.assertEqual(self.mention(message="q2"), ["mention"])
            self.assertEqual(self.flag(self.turns[-1]["argv"], "--model"), "workhorse-two")
            self.assertEqual(self.launched(), ["opus", "workhorse-two"])
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("workhorse-",)):
            self.assertEqual(self.models()["chat"], "sonnet")
        # No ladder at all: the desk's own model.
        (self.office / config.STATE_DIR / config.FAILOVER_LADDERS_FILE).unlink()
        self.assertEqual(self.models(), {"review": "opus", "chat": "opus"})

    def test_a_desk_on_a_cheaper_model_than_the_workhorse_keeps_it(self):
        wands.apply_model(self.conn, "hermione", "haiku", "low", "fast", "role", now=NOW)
        self.assertEqual(self.models(), {"review": "haiku", "chat": "haiku"})

    def test_moody_never_takes_the_claude_workhorse(self):
        self.second_app()
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "role", now=NOW)
        self.assertEqual(self.models("moody"), {"review": "gpt-6-astra", "chat": "gpt-6.1-sol"})
        failover.write_ladders({"moody": {"codex": [CODEX_LADDER[0]]}}, NOW)
        self.assertEqual(self.models("moody"), {"review": "gpt-6-astra", "chat": "gpt-6-astra"})
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("gpt-6.1-",)):
            failover.write_ladders({"moody": {"codex": CODEX_LADDER}}, NOW)
            self.assertEqual(self.models("moody")["chat"], "gpt-6-astra")


class EscalationTests(TierCase):
    def test_a_mention_naming_a_pr_it_did_not_take_as_a_request_runs_on_the_review_tier(self):
        for index, text in enumerate(("is github.com/acme/web-app/pull/7 a good idea?",
                                      "seen www.GitHub.com/acme/web-app/pull/8/files yet?")):
            with self.subTest(text=text):
                self.assertEqual(self.mention(text, message=f"p{index}"), ["mention"])
                argv = self.turns[-1]["argv"]
                self.assertEqual((self.flag(argv, "--model"), self.flag(argv, "--max-budget-usd")), ("opus", "0.50"))
                self.assertEqual(self.record_of(f"p{index}")["tier"], "review")
        self.assertEqual(self.launched(), ["opus", "opus"])
        self.assertEqual(self.writes, [])  # still no review and nothing on GitHub

    def test_attached_material_over_the_threshold_runs_on_the_review_tier(self):
        self.patch(config, "CHAT_REVIEW_ESCALATE_CHARS", 10)
        small = chat_material.Gathered([], {}, "x" * 10, {})
        large = chat_material.Gathered([], {}, "x" * 11, {})
        self.assertEqual(chat_review.mention_tier(QUESTION, chat_material.NOTHING), "chat")
        self.assertEqual(chat_review.mention_tier(QUESTION, small), "chat")
        self.assertEqual(chat_review.mention_tier(QUESTION, large), "review")
        self.assertEqual(chat_review.mention_tier("about github.com/acme/web-app/pull/7", small), "review")
        self.patch(chat_review, "_gather", return_value=large)
        self.assertEqual(self.mention(), ["mention"])
        self.assertEqual(self.flag(self.turns[-1]["argv"], "--model"), "opus")
        self.assertEqual(self.flag(self.turns[-1]["argv"], "--max-budget-usd"), "0.50")


class SettingTests(TierCase):
    def test_a_bad_chat_budget_or_threshold_refuses_to_start(self):
        for name, value in (("CHAT_REVIEW_CHAT_BUDGET_USD", "0.00"), ("CHAT_REVIEW_CHAT_BUDGET_USD", "1; rm"),
                            ("CHAT_REVIEW_CHAT_BUDGET_USD", 0.25), ("CHAT_REVIEW_ESCALATE_CHARS", -1),
                            ("CHAT_REVIEW_ESCALATE_CHARS", "20000")):
            with self.subTest(name=name, value=value), mock.patch.object(config, name, value):
                with self.assertRaises(chat_review.FleetError) as caught:
                    self.run_pass()
                self.assertIn(name, str(caught.exception))
        self.assertEqual(self.turns, [])

    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_kit_ships_a_chat_budget_below_the_question_budget(self):
        self.assertEqual(kit_setting("CHAT_REVIEW_CHAT_BUDGET_USD"), "0.25")
        self.assertEqual(kit_setting("CHAT_REVIEW_ESCALATE_CHARS"), 20_000)
        self.assertLess(float(kit_setting("CHAT_REVIEW_CHAT_BUDGET_USD")),
                        float(kit_setting("CHAT_REVIEW_QUESTION_BUDGET_USD")))


class StatusModelTests(TierCase):
    def test_status_shows_each_apps_review_and_chat_models(self):
        self.second_app()
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "role", now=NOW)
        now = int(time.time())
        self.assertIn("Models: reviews on opus, chat on sonnet.", chat_review.status_text("hermione", now).splitlines())
        self.assertIn("Models: reviews on gpt-6-astra, chat on gpt-6.1-sol.",
                      chat_review.status_text("moody", now).splitlines())
        self.patch(run_desk, "chat_models", side_effect=chat_review.FleetError("no profile"))
        self.assertIn("Models: could not be read.", chat_review.status_text("hermione", now).splitlines())


if __name__ == "__main__":
    unittest.main()
