"""Ollivander: role cards, filing models into lines, picks, apply rules, CLI updates and run_desk's side."""
from __future__ import annotations

import calendar
import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, cli, db, owlery, pensieve, wands
from tests.support import NOW

from fleet import common, config, gitops, ollivander, owl_post, run_desk, safefs, tools
from fleet.safefs import FleetError
from hogwarts.errors import ConflictError, ValidationError
from tests_fleet.support import CODEX_PROFILE, IN_KIT, ONLY_IN_KIT, FleetCase, fake_children, kit_setting

KIT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_models_20261004.json"
OCT4 = calendar.timegm((2026, 10, 4, 6, 0, 0))
AUG1 = calendar.timegm((2026, 8, 1, 6, 0, 0))
ROLE_TABLE = {
    "mcgonagall": ("claude", "frontier", "high", "Scope and spec judgment, used rarely"),
    "hermione": ("claude", "frontier", "high", "Deep reviews of Codex work"),
    "portrait": ("claude", "frontier", "medium", "One nightly memory review"),
    "snape": ("claude", "workhorse", "high", "Accurate SQL at moderate cost"),
    "ron": ("claude", "fast", "low", "Sorts lots of PR and CI updates"),
    "harry": ("codex", "workhorse", "high", "Everyday coding"),
    "moody": ("codex", "frontier", "high", "Security review of Claude work"),
}
HELP = b"""Usage: claude [options] [command] [prompt]

Options:
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fennel', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fennel-5').
  -n, --name <name>                     Set a display name for this session
"""
HEADLESS = ("hermione", "portrait", "ron", "harry", "moody")
# Made-up names only: the kit never names a real model in a blocklist.
MADE_UP_BLOCKS = ("quill", "claude-quill-", "wisp", "claude-wisp-", "fennel", "claude-fennel-", "gpt-zz-")
QUILL = {"slug": "gpt-zz-quill", "description": "Frontier intelligence for the most demanding work.",
         "priority": -5, "visibility": "list", "upgrade": None, "supported_reasoning_levels": ["high"]}


def catalog(edit=None) -> bytes:
    data = json.loads(FIXTURE.read_text())
    if edit is not None:
        edit(data["models"])
    return json.dumps(data).encode("utf-8")


class FakeRunner:
    """Stands in for every command Ollivander starts. Nothing real ever runs."""

    def __init__(self, catalog_bytes: bytes = None, help_bytes: bytes = HELP, codes: dict = None,
                 versions: dict = None, during=None):
        self.catalog = catalog() if catalog_bytes is None else catalog_bytes
        self.help = help_bytes
        self.codes = codes or {}
        # Per binary, what --version prints on each call; the last one repeats.
        self.versions = versions or {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"], config.CODEX_BIN: [b"codex 0.160.0\n"]}
        self.during = during
        self.calls = []

    def __call__(self, argv, timeout, log_name):
        self.calls.append((tuple(argv[1:]), log_name))
        code = self.codes.get(tuple(argv[1:]), 0)
        if argv[1:] == ["debug", "models"]:
            return code, self.catalog
        if argv[1:] == ["--help"]:
            return code, self.help
        if argv[1:] == ["--version"]:
            said = self.versions[argv[0]]
            return code, said.pop(0) if len(said) > 1 else said[0]
        if self.during is not None:
            self.during(argv)
        return code, b""


class OllivanderCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("Popen", "run"):
            patcher = mock.patch.object(subprocess, name, side_effect=AssertionError("no process may start"))
            patcher.start()
            self.addCleanup(patcher.stop)
        # The kit's role cards, from the role table, never the cards this office has been given.
        for desk in config.ROLE_DESKS:
            (self.office / "desks" / desk).mkdir(mode=0o700, exist_ok=True)
            self.write_role(desk)
        (self.office / "desks" / "ollivander").mkdir(mode=0o700)
        self.user_dir = self.tmp / "user"
        for root in (self.castle, self.user_dir):
            (root / ".claude" / "agents").mkdir(parents=True, mode=0o700)
        self.write_agent("mcgonagall", "opus")
        self.write_agent("snape", "sonnet")
        patcher = mock.patch.object(config, "USER_HOME_DIR", str(self.user_dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = FakeRunner()

    def write_agent(self, desk: str, model: str) -> Path:
        root = self.castle if desk == "mcgonagall" else self.user_dir
        text = f"---\nname: {desk}\ndescription: a desk\nmodel: {model}\ntools: Read\n---\n\n# {desk}\n"
        return self.write_file(root / ".claude" / "agents" / f"{desk}.md", text)

    def write_role(self, desk: str, **changes) -> None:
        family, need, effort, why = ROLE_TABLE[desk]
        card = {"family": family, "need": need, "effort": effort, "why": why, **changes}
        self.write_file(self.office / "desks" / desk / "role.json", json.dumps(card))

    def keeper(self, dry_run: bool = False, now: int = OCT4, runner=None, prober=None) -> dict:
        return ollivander.run(self.conn, dry_run=dry_run, now=now, runner=runner or self.runner, prober=prober)

    def desk(self, plan: dict, name: str) -> dict:
        return next(entry for entry in plan["desks"] if entry["desk"] == name)

    def model(self, desk: str) -> dict:
        return wands.get_desk_model(self.conn, desk)

    def events_of(self, kind: str) -> list:
        return [event for event in self.events() if event["kind"] == kind]

    def castle_cli(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        return code, json.loads(out.getvalue() or err.getvalue())


class RoleCardTests(OllivanderCase):
    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_kit_role_cards_match_the_role_table(self):
        self.assertEqual(set(config.ROLE_DESKS), set(ROLE_TABLE))
        for desk, (family, need, effort, why) in ROLE_TABLE.items():
            with self.subTest(desk=desk):
                raw = (KIT / "desks" / desk / "role.json").read_bytes()
                self.assertEqual(ollivander.check_role(raw), {"family": family, "need": need, "effort": effort,
                                                              "why": why})

    def test_bad_cards_are_refused(self):
        good = {"family": "codex", "need": "workhorse", "effort": "high", "why": "Everyday coding"}
        for raw in (b"not json", b"[]", json.dumps({**good, "model": "gpt-6"}).encode(),
                    json.dumps({key: value for key, value in good.items() if key != "why"}).encode(),
                    json.dumps({**good, "family": "human"}).encode(), json.dumps({**good, "need": "cheap"}).encode(),
                    json.dumps({**good, "effort": "ultra"}).encode(), json.dumps({**good, "why": "two\nlines"}).encode(),
                    json.dumps({**good, "why": "x" * 121}).encode(), json.dumps({**good, "why": ""}).encode(),
                    json.dumps({**good, "effort": 3}).encode(),
                    b'{"family": "codex", "family": "claude", "need": "fast", "effort": "low", "why": "w"}'):
            with self.subTest(raw=raw), self.assertRaises(FleetError):
                ollivander.check_role(raw)

    def test_a_bad_card_stops_only_that_desk(self):
        self.write_file(self.office / "desks" / "harry" / "role.json", '{"family": "codex"}')
        plan = self.keeper()
        self.assertEqual(self.desk(plan, "harry")["action"], "bad-role")
        self.assertIsNone(self.model("harry"))
        self.assertEqual(self.model("moody")["model"], "gpt-6-astra")
        [event] = self.events_of("ollivander.bad-role")
        self.assertEqual((event["desk"], event["verdict"]), ("harry", "headmaster"))

    def test_a_symlinked_card_is_refused(self):
        card = self.office / "desks" / "moody" / "role.json"
        target = self.write_file(self.tmp / "elsewhere.json", card.read_text())
        os.unlink(card)
        os.symlink(target, card)
        self.assertEqual(self.desk(self.keeper(), "moody")["action"], "bad-role")

    def test_a_desk_family_never_changes(self):
        self.write_role("harry", family="claude")
        self.write_role("hermione", family="codex")
        plan = self.keeper()
        for desk in ("harry", "hermione"):
            with self.subTest(desk=desk):
                self.assertEqual(self.desk(plan, desk)["action"], "bad-role")
                self.assertIn("family never changes", self.desk(plan, desk)["error"])
                self.assertIsNone(self.model(desk))
        self.assertEqual(pensieve.get_desk(self.conn, "harry")["family"], "codex")
        self.write_role("harry")
        self.write_role("hermione")
        self.keeper(now=OCT4 + 60)
        codex_slugs = {model["slug"] for model in ollivander.parse_catalog(catalog())}
        for desk in HEADLESS:
            family = pensieve.get_desk(self.conn, desk)["family"]
            with self.subTest(desk=desk):
                if family == "codex":
                    self.assertIn(self.model(desk)["model"], codex_slugs)
                else:
                    self.assertIn(self.model(desk)["model"], config.CLAUDE_LINES)


class FilingTests(OllivanderCase):
    def test_the_fixture_is_filed_by_its_descriptions(self):
        plan = self.keeper(dry_run=True)
        statuses = {model["slug"]: (model["status"], model["line"]) for model in plan["catalog"]["codex"]["models"]}
        self.assertEqual(statuses["gpt-6.1-sol"], ("tier", "workhorse"))
        self.assertEqual(statuses["gpt-6-astra"], ("tier", "frontier"))
        self.assertEqual(statuses["gpt-6-luna"], ("tier", "fast"))
        for slug in ("gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"):
            self.assertEqual(statuses[slug], ("excluded", None), slug)
        self.assertEqual(plan["unclassified"], ["fennel"])

    def test_ryans_filing_outranks_the_keywords(self):
        wands.classify(self.conn, "gpt-6.1-sol", "ignore", now=OCT4)
        wands.classify(self.conn, "gpt-6-sol", "workhorse", now=OCT4)
        self.assertEqual(self.desk(self.keeper(), "harry")["pick"], "gpt-6-sol")

    def test_an_unknown_name_gets_one_fyi_and_is_never_picked(self):
        def add(models):
            models.append({"slug": "gpt-7-nova", "display_name": "Nova", "description": "Experimental preview model.",
                           "priority": -1, "visibility": "list", "upgrade": None, "supported_reasoning_levels": []})
            models.append({"slug": "gpt-7-both", "description": "Fast and affordable frontier model.",
                           "priority": -2, "visibility": "list", "upgrade": None})
            models.append({"slug": "gpt-secret", "description": "Internal.", "priority": 1, "visibility": "hide"})
        runner = FakeRunner(catalog(add))
        for when in (OCT4, OCT4 + 86400):
            plan = self.keeper(now=when, runner=runner)
        self.assertEqual(sorted(plan["unclassified"]), ["fennel", "gpt-7-both", "gpt-7-nova"])
        summaries = sorted(event["summary"] for event in self.events_of("ollivander.unclassified"))
        self.assertEqual(len(summaries), 3)
        self.assertTrue(all("castle model line" in summary for summary in summaries))
        self.assertFalse(any("gpt-secret" in summary for summary in summaries))
        self.assertEqual((self.model("harry")["model"], self.model("moody")["model"]), ("gpt-6.1-sol", "gpt-6-astra"))
        wands.classify(self.conn, "fennel", "frontier", now=OCT4 + 2 * 86400)
        self.assertNotIn("fennel", self.keeper(now=OCT4 + 2 * 86400, runner=runner)["unclassified"])

    def test_ryans_latest_alias_filing_wins_a_tie(self):
        wands.classify(self.conn, "fennel", "frontier", now=OCT4)
        plan = self.keeper()
        self.assertEqual(self.desk(plan, "hermione")["pick"], "fennel")
        self.assertIn("as Ryan filed it", self.desk(plan, "hermione")["reason"])
        self.assertEqual(self.desk(plan, "ron")["pick"], "haiku")

    def test_a_help_parse_miss_never_fails_the_pass(self):
        self.keeper()
        for runner in (FakeRunner(help_bytes=b"nothing useful"), FakeRunner(codes={("--help",): 1})):
            with self.subTest(runner=runner):
                plan = self.keeper(now=OCT4 + 60, runner=runner)
                self.assertFalse(plan["catalog"]["claude"]["detected"])
                self.assertEqual([item["alias"] for item in plan["catalog"]["claude"]["aliases"]],
                                 ["fennel", "haiku", "opus", "sonnet"])

    def test_the_help_aliases(self):
        self.assertEqual(ollivander.parse_aliases(HELP.decode()), ["fennel", "opus", "sonnet"])
        self.assertIsNone(ollivander.parse_aliases("no model option here"))

    def test_only_safe_slugs_survive_the_catalog(self):
        def add(models):
            for slug in ("GPT-7", "../evil", "a", "x;y", "gpt 7", 7):
                models.append({"slug": slug, "description": "Frontier intelligence.", "priority": 0, "visibility": "list"})
        slugs = [model["slug"] for model in ollivander.parse_catalog(catalog(add))]
        self.assertEqual(len(slugs), 10)
        self.assertTrue(all(wands.MODEL_NAME.fullmatch(slug) for slug in slugs))
        for raw in (b"{}", b"[]", b"nope", b'{"models": "x"}'):
            with self.subTest(raw=raw), self.assertRaises(FleetError):
                ollivander.parse_catalog(raw)

    def test_a_catalog_failure_leaves_the_codex_desks_alone(self):
        plan = self.keeper(runner=FakeRunner(codes={("debug", "models"): 1}))
        self.assertEqual(self.desk(plan, "harry")["action"], "no-catalog")
        self.assertEqual((self.model("harry")["model"], self.model("harry")["need"]), (None, "workhorse"))
        self.assertEqual(self.model("hermione")["model"], "opus")
        self.assertEqual(len(self.events_of("ollivander.catalog")), 1)


class PickTests(OllivanderCase):
    def test_every_desk_gets_its_role_pick_from_the_fixture(self):
        plan = self.keeper(dry_run=True)
        picks = {entry["desk"]: (entry["pick"], entry["pick_effort"]) for entry in plan["desks"]}
        self.assertEqual(picks, {
            "harry": ("gpt-6.1-sol", "high"), "moody": ("gpt-6-astra", "high"), "hermione": ("opus", "high"),
            "portrait": ("opus", "medium"), "ron": ("haiku", "low"), "mcgonagall": ("opus", "high"),
            "snape": ("sonnet", "high"),
        })
        self.assertEqual(self.desk(plan, "harry")["reason"],
                         "role need workhorse; gpt-6.1-sol is the newest workhorse model in the Codex catalog")

    def test_hidden_and_retiring_models_are_skipped(self):
        codex = ollivander.fetch_codex(FakeRunner(catalog(lambda models: models[4].update(priority=-1))))
        self.assertEqual(codex["models"][4]["slug"], "gpt-reserve")
        self.assertEqual(ollivander.pick_codex("fast", codex, {}, OCT4)["model"], "gpt-6-luna")
        filed = {"gpt-5.5": {"line": "workhorse", "id": 1}, "gpt-6.1-sol": {"line": "ignore", "id": 2}}
        self.assertEqual(ollivander.pick_codex("workhorse", codex, filed, AUG1)["model"], "gpt-5.5")
        self.assertIsNone(ollivander.pick_codex("workhorse", codex, filed, OCT4))
        self.assertTrue(ollivander.retiring({"upgrade": {"retirement_at": "soon"}}, OCT4))
        self.assertTrue(ollivander.retiring({"upgrade": {}}, OCT4))
        self.assertFalse(ollivander.retiring({"upgrade": None}, OCT4))

    def test_gpt_5_5_is_never_picked(self):
        wands.classify(self.conn, "gpt-5.5", "workhorse", now=OCT4)
        wands.classify(self.conn, "gpt-6.1-sol", "ignore", now=OCT4)
        plan = self.keeper()
        self.assertEqual(self.desk(plan, "harry")["action"], "no-pick")
        self.assertIsNone(self.model("harry")["model"])
        self.assertEqual(len(self.events_of("ollivander.no-pick")), 1)
        self.assertNotIn("gpt-5.5", [entry.get("pick") for entry in plan["desks"]])

    def test_effort_drops_to_the_nearest_listed_level(self):
        self.assertEqual(ollivander.fit_effort("max", ["low", "medium", "high", "xhigh"]), "xhigh")
        self.assertEqual(ollivander.fit_effort("medium", ["low", "high"]), "low")
        self.assertEqual(ollivander.fit_effort("low", ["high", "max"]), "high")
        self.assertEqual(ollivander.fit_effort("high", None), "high")
        self.write_role("harry", effort="max")
        no_max = catalog(lambda models: models[0].update(supported_reasoning_levels=["low", "medium", "high"]))
        self.assertEqual(self.desk(self.keeper(runner=FakeRunner(no_max)), "harry")["pick_effort"], "high")


class ApplyTests(OllivanderCase):
    def test_the_first_pass_applies_each_pick_as_initial(self):
        self.keeper()
        for desk, model, effort in (("hermione", "opus", "high"), ("portrait", "opus", "medium"),
                                    ("ron", "haiku", "low"), ("harry", "gpt-6.1-sol", "high"),
                                    ("moody", "gpt-6-astra", "high")):
            with self.subTest(desk=desk):
                row = self.model(desk)
                self.assertEqual((row["model"], row["effort"], row["pinned"]), (model, effort, 0))
                self.assertEqual([change["reason"] for change in wands.changes(self.conn, desk)], ["initial"])
        applied = self.events_of("ollivander.applied")
        self.assertEqual(len(applied), 5)
        self.assertTrue(all("initial pick" in event["summary"] for event in applied))
        self.assertEqual({event["verdict"] for event in applied}, {"headmaster"})

    def test_a_steady_pass_changes_nothing(self):
        self.keeper()
        events, changes = len(self.events()), self.conn.execute("SELECT COUNT(*) FROM model_changes").fetchone()[0]
        plan = self.keeper(now=OCT4 + 86400)
        self.assertEqual({self.desk(plan, desk)["action"] for desk in HEADLESS}, {"keep"})
        self.assertEqual(len(self.events()), events)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_changes").fetchone()[0], changes)

    def test_a_dearer_pick_waits_until_ryan_approves(self):
        self.keeper()
        self.write_role("harry", need="frontier")
        for when in (OCT4 + 60, OCT4 + 120):
            self.assertEqual(self.desk(self.keeper(now=when), "harry")["action"], "pending")
        row = self.model("harry")
        self.assertEqual((row["model"], row["pending_model"], row["pending_effort"]), ("gpt-6.1-sol", "gpt-6-astra", "high"))
        [event] = self.events_of("ollivander.pending")
        self.assertIn("castle desk model harry --approve", event["summary"])
        code, out = self.castle_cli("desk", "models")
        harry = next(item for item in out["data"] if item["desk"] == "harry")
        self.assertEqual(harry["pending_pick"], {"model": "gpt-6-astra", "effort": "high"})
        code, out = self.castle_cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["data"]["model"]), (0, "gpt-6-astra"))
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 180), "harry")["action"], "keep")

    def test_a_pick_that_waits_again_reaches_ryan_again(self):
        self.keeper()
        self.write_role("harry", need="frontier")
        self.keeper(now=OCT4 + 60)
        self.keeper(now=OCT4 + 120)
        self.assertEqual(len(self.events_of("ollivander.pending")), 1)
        self.write_role("harry")
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 180), "harry")["action"], "keep")
        self.assertIsNone(self.model("harry")["pending_model"])
        self.write_role("harry", need="frontier")
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 240), "harry")["action"], "pending")
        self.assertEqual(len(self.events_of("ollivander.pending")), 2)

    def test_an_agent_file_that_drifts_back_is_reported_again(self):
        self.keeper()
        self.write_agent("snape", "opus")
        self.keeper(now=OCT4 + 60)
        self.keeper(now=OCT4 + 120)
        self.assertEqual(len(self.events_of("ollivander.agent-file")), 1)
        self.write_agent("snape", "sonnet")
        self.keeper(now=OCT4 + 180)
        self.write_agent("snape", "opus")
        self.keeper(now=OCT4 + 240)
        self.assertEqual(len(self.events_of("ollivander.agent-file")), 2)
        self.write_role("snape", need="fast")
        self.keeper(now=OCT4 + 300)
        events = self.events_of("ollivander.agent-file")
        self.assertEqual(len(events), 3)
        self.assertIn("change that one line to model: haiku", events[-1]["summary"])

    def test_a_cheaper_or_equal_pick_applies_with_a_fyi(self):
        self.keeper()
        self.write_role("moody", need="workhorse")
        self.write_role("hermione", effort="medium")
        plan = self.keeper(now=OCT4 + 60)
        self.assertEqual((self.desk(plan, "moody")["action"], self.desk(plan, "hermione")["action"]), ("apply", "apply"))
        self.assertEqual(self.model("moody")["model"], "gpt-6.1-sol")
        self.assertEqual(self.model("hermione")["effort"], "medium")
        summaries = [event["summary"] for event in self.events_of("ollivander.applied") if event["desk"] == "moody"]
        self.assertIn("role need workhorse; gpt-6.1-sol is the newest workhorse model in the Codex catalog",
                      summaries[-1])
        self.assertIn("was gpt-6-astra", summaries[-1])

    def test_a_pinned_desk_is_left_alone_until_unpinned(self):
        self.keeper()
        self.assertEqual(self.castle_cli("desk", "model", "harry", "gpt-6-sol")[0], 0)
        self.assertEqual(self.castle_cli("desk", "model", "harry", "opus")[0], 2)
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 60), "harry")["action"], "pinned")
        self.assertEqual(self.model("harry")["model"], "gpt-6-sol")
        self.assertEqual(self.castle_cli("desk", "model", "harry", "--role")[0], 0)
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 120), "harry")["action"], "apply")
        self.assertEqual(self.model("harry")["model"], "gpt-6.1-sol")

    def test_agent_file_desks_are_only_reported(self):
        snape_file = self.user_dir / ".claude" / "agents" / "snape.md"
        self.keeper()
        self.assertEqual(self.events_of("ollivander.agent-file"), [])
        self.write_agent("snape", "opus")
        before = snape_file.read_text()
        plan = self.keeper(now=OCT4 + 60)
        entry = self.desk(plan, "snape")
        self.assertEqual((entry["action"], entry["file_model"], entry["pick"]), ("report", "opus", "sonnet"))
        [event] = self.events_of("ollivander.agent-file")
        self.assertIn(str(snape_file), event["summary"])
        self.assertIn("change that one line to model: sonnet", event["summary"])
        self.assertEqual(snape_file.read_text(), before)
        self.assertEqual(wands.changes(self.conn, "snape"), [])
        self.assertEqual(self.model("snape")["model"], "opus")
        self.assertEqual(self.model("mcgonagall")["need"], "frontier")


class DryRunTests(OllivanderCase):
    def state(self) -> tuple:
        tables = ("events", "desk_models", "model_catalog", "model_changes", "model_lines")
        counts = tuple(self.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables)
        files = sorted(str(path.relative_to(self.tmp)) for path in self.tmp.rglob("*"))
        return counts, files

    def test_a_dry_run_changes_nothing(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        self.write_role("harry", family="claude")
        before = self.state()
        plan = self.keeper(dry_run=True)
        self.assertEqual(self.state(), before)
        self.assertEqual([call[0] for call in self.runner.calls], [("debug", "models"), ("--help",)])
        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["updates"]["enabled"])
        self.assertEqual(plan["updates"]["ran"], [])
        self.assertEqual(self.desk(plan, "moody")["action"], "initial")
        self.assertIn("ollivander.bad-role", [notice["kind"] for notice in plan["notices"]])
        json.dumps(plan, ensure_ascii=True, allow_nan=False)

    def test_fleet_ollivander_dry_run(self):
        out = io.StringIO()
        with mock.patch.object(ollivander, "run_command", self.runner), \
                mock.patch.object(common, "now_stamp", return_value=OCT4), contextlib.redirect_stdout(out):
            code = tools.main(["ollivander", "--dry-run"])
        data = json.loads(out.getvalue())
        self.assertEqual((code, data["ok"]), (0, True))
        self.assertTrue(out.getvalue().isascii())
        self.assertEqual(self.desk(data["data"], "harry")["pick"], "gpt-6.1-sol")
        self.assertEqual(self.events(), [])


class UpdateTests(OllivanderCase):
    def test_no_marker_no_update(self):
        plan = self.keeper()
        self.assertFalse(plan["updates"]["enabled"])
        self.assertEqual([call[0] for call in self.runner.calls], [("debug", "models"), ("--help",)])

    def test_updates_run_then_checks_and_all_pass(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        self.enable("moody")
        plan = self.keeper()
        self.assertEqual([call for call in self.runner.calls if call[1] is not None],
                         [(("update",), ollivander.LOG_NAME), (("upgrade", "--cask", "codex"), ollivander.LOG_NAME)])
        self.assertEqual([call[0] for call in self.runner.calls].count(("--version",)), 4)
        self.assertEqual([check["ok"] for check in plan["updates"]["checks"]], [True, True, True])
        self.assertEqual(plan["updates"]["versions"]["after"],
                         {"claude": "2.0.0 (Claude Code)", "codex": "codex 0.160.0"})
        self.assertFalse(plan["updates"]["stopped"])
        self.assertFalse(run_desk.stop_requested())
        self.assertEqual(self.events_of("ollivander.cli-version"), [])

    def test_headless_launches_wait_while_the_update_runs(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        seen = []
        plan = self.keeper(runner=FakeRunner(during=lambda argv: seen.append(run_desk.stop_requested())))
        self.assertEqual(seen, [True, True])
        self.assertFalse(plan["updates"]["stopped"])
        self.assertFalse(run_desk.stop_requested())

    def test_an_update_that_dies_part_way_leaves_launches_stopped(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)

        def die(argv):
            raise FleetError("the pass died")

        with self.assertRaises(FleetError):
            self.keeper(runner=FakeRunner(during=die))
        self.assertTrue(run_desk.stop_requested())
        self.assertEqual(self.castle_cli("ollivander", "clear")[1]["data"]["cleared"], True)
        self.assertFalse(run_desk.stop_requested())

    def test_a_new_codex_version_stops_the_codex_family_until_the_boundary_is_proven_again(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                                      config.CODEX_BIN: [b"codex 0.160.0\n", b"codex 0.161.0\n"]})
        plan = self.keeper(runner=runner)
        self.assertEqual((plan["updates"]["stopped"], plan["updates"]["stopped_families"]), (True, ["codex"]))
        state = self.office / "state"
        self.assertTrue((state / config.STOP_FAMILY_FILES["codex"]).exists())
        self.assertFalse((state / config.STOP_FILE).exists())
        self.assertFalse((state / config.STOP_FAMILY_FILES["claude"]).exists())
        self.assertTrue(run_desk.stop_requested("codex"))
        self.assertFalse(run_desk.stop_requested("claude"))
        self.assertTrue(run_desk.stop_requested())  # a launch of no one family waits on either family's stop
        [event] = self.events_of("ollivander.stopped")
        self.assertIn("from codex 0.160.0 to codex 0.161.0", event["summary"])
        self.assertIn("codex-boundary-test.sh", event["summary"])
        self.assertIn("Ollivander stopped every codex headless desk run and turn", event["summary"])
        self.assertIn("in your terminal; the claude desks run on. Why:", event["summary"])
        self.assertEqual(self.events_of("ollivander.cli-version"), [])
        data = self.castle_cli("ollivander", "clear")[1]["data"]
        self.assertEqual(data["removed"], [config.STOP_FAMILY_FILES["codex"]])
        self.assertEqual(data["stopped"][0]["stopped"], "codex")
        self.assertIn("removed the stop of the codex family (why: codex --version went from codex 0.160.0",
                      data["note"])
        self.assertFalse(run_desk.stop_requested())

    def test_a_family_stop_event_never_says_the_other_family_runs_while_an_earlier_stop_holds_it(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        ollivander.write_stop("a new claude version", NOW, family="claude")
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                                      config.CODEX_BIN: [b"codex 0.160.0\n", b"codex 0.161.0\n"]})
        self.assertEqual(self.keeper(runner=runner)["updates"]["stopped_families"], ["codex"])
        [event] = self.events_of("ollivander.stopped")
        self.assertIn("Ollivander stopped every headless desk run, and each stays blocked", event["summary"])
        self.assertNotIn("run on", event["summary"])

    def test_a_new_claude_version_that_stops_stops_only_the_claude_family(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n", b"2.0.1 (Claude Code)\n"],
                                      config.CODEX_BIN: [b"codex 0.160.0\n"]})
        with mock.patch.object(config, "VERSION_STOPS", ("claude", "codex")):
            plan = self.keeper(runner=runner)
        self.assertEqual(plan["updates"]["stopped_families"], ["claude"])
        self.assertTrue(run_desk.stop_requested("claude"))
        self.assertFalse(run_desk.stop_requested("codex"))
        [event] = self.events_of("ollivander.stopped")
        self.assertIn("Ollivander stopped every claude headless desk run and turn", event["summary"])
        self.assertIn("the codex desks run on", event["summary"])
        self.assertNotIn("codex-boundary-test.sh", event["summary"])

    def test_a_new_claude_version_is_a_fyi_and_launches_go_on(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n", b"2.0.1 (Claude Code)\n"],
                                      config.CODEX_BIN: [b"codex 0.160.0\n"]})
        plan = self.keeper(runner=runner)
        self.assertFalse(plan["updates"]["stopped"])
        self.assertFalse(run_desk.stop_requested())
        [event] = self.events_of("ollivander.cli-version")
        self.assertIn("from 2.0.0 (Claude Code) to 2.0.1 (Claude Code)", event["summary"])

    def test_a_failed_update_or_check_stops_the_family_whose_cli_failed(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        for now, codes, families in ((OCT4, {("upgrade", "--cask", "codex"): 1}, ["codex"]),
                                     (OCT4 + 60, {("update",): 1}, ["claude"]),
                                     (OCT4 + 120, {("--version",): -1}, ["claude", "codex"])):
            with self.subTest(codes=codes):
                plan = self.keeper(now=now, runner=FakeRunner(codes=codes))
                self.assertEqual((plan["updates"]["stopped"], plan["updates"]["stopped_families"]), (True, families))
                for family in config.STOP_FAMILY_FILES:
                    self.assertEqual(run_desk.stop_requested(family), family in families)
                summary = self.events_of("ollivander.stopped")[-1]["summary"]
                if len(families) == 2:
                    self.assertIn("Ollivander stopped every headless desk run, and each stays blocked", summary)
                else:
                    self.assertIn(f"Ollivander stopped every {families[0]} headless desk run and turn", summary)
                cleared = self.castle_cli("ollivander", "clear")[1]["data"]
                self.assertEqual([item["stopped"] for item in cleared["stopped"]], families)
                self.assertFalse(run_desk.stop_requested())
        self.assertEqual(len(self.events_of("ollivander.stopped")), 3)

    def test_a_desk_whose_dry_run_fails_stops_launches_of_its_family(self):
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        self.enable("hermione")
        os.unlink(self.office / "desks" / "hermione" / "BRIEF.md")
        plan = self.keeper()
        self.assertEqual(plan["updates"]["checks"][-1], {"check": "run_desk hermione --dry-run", "ok": False})
        self.assertEqual(plan["updates"]["stopped_families"], ["claude"])
        self.assertTrue(run_desk.stop_requested("claude"))
        self.assertFalse(run_desk.stop_requested("codex"))


SHELL_PASS = (b"Codex version: codex 0.161.0\n\nSetting up a throwaway test folder\n   OK      made a folder\n\n"
              b"All 11 checks passed. The profile keeps a Codex desk out of the office and out of\n")
SHELL_FAIL = b"Codex version: codex 0.161.0\n   FAILED  reached example.com\n\n1 check(s) failed, 10 passed. The test folder\n"
EXEC_PASS = (b"Launcher code: the installed office\nNetwork control outside the sandbox: reached example.com\n\n"
             b"All 27 checks passed for both desks under real codex exec runs. Test folders deleted.\n")
EXEC_FAIL = (b"Launcher code: the installed office\n   INCONCLUSIVE has no network: the probe printed nothing\n\n"
             b"1 check(s) failed or were inconclusive, 26 passed. Copy everything above and paste it to Claude.\n")
NEW_CODEX = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"], config.CODEX_BIN: [b"codex 0.160.0\n", b"codex 0.161.0\n"]}


class FakeProber:
    """Stands in for both boundary probes: the shell probe's and the exec probe's (exit code, stdout), or a
    callable run first. Records each probe it was asked to run."""

    def __init__(self, shell=(0, SHELL_PASS), exec_probe=(0, EXEC_PASS), during=None):
        self.results = {config.PROBE_SHELL: shell, config.PROBE_EXEC: exec_probe}
        self.during = during
        self.calls = []

    def __call__(self, argv, timeout, log_name):
        name = os.path.basename(argv[-1])
        self.calls.append((tuple(argv), timeout))
        if self.during is not None:
            self.during(name)
        return self.results[name]


class AutoProveTests(OllivanderCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)
        self.probes = self.office / config.PROBES_DIR
        self.probes.mkdir(mode=0o700)
        self.write_file(self.probes / config.PROBE_SHELL, f"#!/bin/sh\nset -u\n\nCODEX={config.CODEX_BIN}\n")
        self.write_file(self.probes / config.PROBE_EXEC, "import sys\n")
        self.state = self.office / "state"

    def switch_on(self) -> None:
        self.write_file(self.office / config.AUTO_PROVE_FILE, "on\n")

    def codex_stop(self) -> Path:
        return self.state / config.STOP_FAMILY_FILES["codex"]

    def prove_pass(self, now: int = OCT4, prober=None, versions=None) -> dict:
        moved = {name: list(said) for name, said in (versions or NEW_CODEX).items()}
        return self.keeper(now=now, runner=FakeRunner(versions=moved), prober=prober)["updates"]

    def test_two_passes_clear_only_the_codex_stop_and_record_the_proof(self):
        self.switch_on()
        ollivander.write_stop("a new claude version", NOW, family="claude")
        prober = FakeProber()
        updates = self.prove_pass(prober=prober)
        self.assertEqual(prober.calls, [
            (("/bin/sh", str(self.probes / config.PROBE_SHELL)), config.PROBE_SHELL_TIMEOUT_SECONDS),
            ((*config.PYTHON_WRAPPER[2:], str(self.probes / config.PROBE_EXEC)), config.PROBE_EXEC_TIMEOUT_SECONDS)])
        self.assertEqual((updates["prove"]["proven"], updates["prove"]["cleared"]), (True, True))
        self.assertFalse(self.codex_stop().exists())
        self.assertTrue((self.state / config.STOP_FAMILY_FILES["claude"]).exists())  # never another family's stop
        self.assertFalse(run_desk.stop_requested("codex"))
        self.assertTrue(run_desk.stop_requested("claude"))
        [note] = self.events_of("ollivander.proven")
        self.assertEqual((note["summary"], note["verdict"]), ("Codex codex 0.161.0 proven; Codex desks resumed.",
                                                              "headmaster"))
        [record] = self.events_of(ollivander.PROVE_RESULT_KIND)
        self.assertEqual(record["verdict"], "routine")
        self.assertIn("Codex codex 0.161.0 proven.", record["summary"])
        self.assertIn("Shell probe: All 11 checks passed.", record["summary"])
        self.assertIn("Exec probe: All 27 checks passed for both desks", record["summary"])
        [claim] = pensieve.events_with_key_prefix(self.conn, "ollivander:prove:")
        self.assertEqual((claim["ts"], claim["kind"], claim["verdict"]), (OCT4, ollivander.PROVE_KIND, "routine"))
        [stopped] = self.events_of("ollivander.stopped")
        self.assertIn("Ollivander runs both boundary probes on it now", stopped["summary"])
        self.assertEqual(self.events_of("ollivander.prove-failed"), [])

    def test_a_failed_or_timed_out_probe_keeps_the_stop_and_names_it(self):
        self.switch_on()
        cases = (
            ((0, SHELL_FAIL), (0, EXEC_PASS), 1, "the shell boundary probe failed (1 check(s) failed, 10 passed."),
            ((-1, b""), (0, EXEC_PASS), 1, "the shell boundary probe failed (timed out after 300 seconds)"),
            ((1, b"Codex version: codex 0.161.0\n"), (0, EXEC_PASS), 1, "the shell boundary probe failed (exit 1 with no"),
            ((0, SHELL_PASS.replace(b"0.161.0", b"0.150.0")), (0, EXEC_PASS), 1, "(it tested Codex codex 0.150.0, not"),
            ((0, SHELL_PASS), (1, EXEC_FAIL), 2, "the exec boundary probe failed (exit 1: 1 check(s) failed or were"),
            ((0, SHELL_PASS), (-1, b""), 2, "the exec boundary probe failed (timed out after 1500 seconds)"),
            ((0, SHELL_PASS), (0, EXEC_PASS.replace(b"the installed office", b"this kit checkout")), 2,
             "the exec boundary probe failed (it did not run the installed office's launcher code)"),
        )
        for index, ((code, shell), exec_probe, runs, said) in enumerate(cases):
            with self.subTest(said=said):
                self.castle_cli("ollivander", "clear")
                version = f"0.2{index}.0".encode()
                prober = FakeProber(shell=(code, shell.replace(b"0.161.0", version)), exec_probe=exec_probe)
                versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                            config.CODEX_BIN: [b"codex 0.160.0\n", b"codex " + version + b"\n"]}
                updates = self.prove_pass(now=OCT4 + index * 60, prober=prober, versions=versions)
                self.assertEqual(len(prober.calls), runs)
                self.assertEqual((updates["prove"]["proven"], updates["prove"]["cleared"]), (False, False))
                self.assertTrue(self.codex_stop().exists())
                self.assertTrue(run_desk.stop_requested("codex"))
                note = self.events_of("ollivander.prove-failed")[-1]["summary"]
                self.assertTrue(note.startswith(f"Codex codex {version.decode()} not proven: "), note)
                self.assertIn(said, note)
                self.assertIn("so the Codex desks stay stopped", note)
                self.assertIn("castle ollivander clear", note)
                self.assertIn(ollivander.RERUN, note)
        self.assertEqual(self.events_of("ollivander.proven"), [])
        self.assertEqual(len(self.events_of(ollivander.PROVE_RESULT_KIND)), len(cases))

    def test_probes_missing_or_testing_another_codex_are_a_failure_with_no_probe_run(self):
        self.switch_on()
        self.write_file(self.probes / config.PROBE_SHELL, "#!/bin/sh\nCODEX=/usr/local/bin/other-codex\n")
        prober = FakeProber()
        self.prove_pass(prober=prober)
        self.assertEqual(prober.calls, [])
        self.assertIn(f"not run: the shell probe does not test {config.CODEX_BIN}",
                      self.events_of("ollivander.prove-failed")[-1]["summary"])
        self.assertTrue(self.codex_stop().exists())
        self.castle_cli("ollivander", "clear")
        os.unlink(self.probes / config.PROBE_EXEC)
        versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                    config.CODEX_BIN: [b"codex 0.161.0\n", b"codex 0.162.0\n"]}
        self.prove_pass(now=OCT4 + 60, prober=prober, versions=versions)
        self.assertEqual(prober.calls, [])
        self.assertIn("are not both in the office probes folder", self.events_of("ollivander.prove-failed")[-1]["summary"])
        self.assertTrue(self.codex_stop().exists())

    def test_one_automatic_try_per_version_and_a_manual_rerun(self):
        self.switch_on()
        prober = FakeProber(shell=(0, SHELL_FAIL))
        self.prove_pass(prober=prober)
        self.assertEqual(len(prober.calls), 1)
        # The same version arrives again later (a rollback and a second update): no second automatic try.
        self.castle_cli("ollivander", "clear")
        later = self.prove_pass(now=OCT4 + config.DAY_SECONDS, prober=prober)
        self.assertEqual(len(prober.calls), 1)
        self.assertIsNone(later["prove"])
        self.assertTrue(self.codex_stop().exists())
        [stopped] = self.events_of("ollivander.stopped")[-1:]
        self.assertIn("codex-boundary-test.sh", stopped["summary"])
        self.assertEqual(len(self.events_of(ollivander.PROVE_KIND)), 1)
        # That stop is not the one the try was for, so a rerun refuses to clear it.
        with self.assertRaises(FleetError) as caught:
            ollivander.prove_again(self.conn, now=OCT4 + 2 * config.DAY_SECONDS,
                                   runner=FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                                                               config.CODEX_BIN: [b"codex 0.161.0\n"]}),
                                   prober=FakeProber())
        self.assertIn("no longer in place", str(caught.exception))
        self.assertTrue(self.codex_stop().exists())

    def test_the_manual_rerun_proves_the_version_a_try_failed_on(self):
        self.switch_on()
        self.prove_pass(prober=FakeProber(exec_probe=(-1, b"")))
        self.assertTrue(self.codex_stop().exists())
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"], config.CODEX_BIN: [b"codex 0.161.0\n"]})
        prober = FakeProber()
        found = ollivander.prove_again(self.conn, now=OCT4 + 600, runner=runner, prober=prober)
        self.assertEqual((found["proven"], found["cleared"], found["stamp"]), (True, True, OCT4))
        self.assertEqual(len(prober.calls), 2)
        self.assertFalse(self.codex_stop().exists())
        self.assertEqual(self.events_of("ollivander.proven")[-1]["summary"],
                         "Codex codex 0.161.0 proven; Codex desks resumed.")
        # A version no try was claimed for has nothing to rerun.
        runner = FakeRunner(versions={config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"], config.CODEX_BIN: [b"codex 9.9.9\n"]})
        with self.assertRaises(FleetError) as caught:
            ollivander.prove_again(self.conn, now=OCT4 + 900, runner=runner, prober=prober)
        self.assertIn("never tried to prove", str(caught.exception))
        self.assertEqual(len(prober.calls), 2)

    def test_the_switch_off_runs_nothing(self):
        for index, text in enumerate((None, "off\n", "yes\n")):
            with self.subTest(text=text):
                if text is not None:
                    self.write_file(self.office / config.AUTO_PROVE_FILE, text)
                self.castle_cli("ollivander", "clear")
                prober = FakeProber()
                versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                            config.CODEX_BIN: [b"codex 0.1.0\n", f"codex 0.{index}.5\n".encode()]}
                updates = self.prove_pass(now=OCT4 + index * 60, prober=prober, versions=versions)
                self.assertFalse(updates["auto_prove"])
                self.assertIsNone(updates["prove"])
                self.assertEqual(prober.calls, [])
                self.assertTrue(self.codex_stop().exists())
                self.assertIn("codex-boundary-test.sh", self.events_of("ollivander.stopped")[-1]["summary"])
        self.assertEqual([event for event in self.events() if event["kind"].startswith("ollivander.prove")], [])
        self.switch_on()
        self.assertTrue(self.keeper(dry_run=True, prober=FakeProber())["updates"]["auto_prove"])

    def test_no_try_when_the_codex_stop_would_hold_more_than_the_new_version(self):
        self.switch_on()
        cases = {
            "a codex stop already in place": lambda: ollivander.write_stop("an earlier failure", NOW, family="codex"),
            "a stop of every family in place": lambda: ollivander.write_stop("written by hand", NOW),
            "the codex update failed too": None,
        }
        for index, (why, before) in enumerate(cases.items()):
            with self.subTest(why=why):
                self.castle_cli("ollivander", "clear")
                if before is not None:
                    before()
                codes = {("upgrade", "--cask", "codex"): 1} if before is None else {}
                versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                            config.CODEX_BIN: [b"codex 0.160.0\n", f"codex 0.17{index}.0\n".encode()]}
                prober = FakeProber()
                runner = FakeRunner(versions=versions, codes=codes)
                updates = self.keeper(now=OCT4 + index * 60, runner=runner, prober=prober)["updates"]
                self.assertIsNone(updates["prove"])
                self.assertEqual(prober.calls, [])
                self.assertTrue(self.codex_stop().exists())
                if before is not None and "every" in why:
                    self.assertTrue((self.state / config.STOP_FILE).exists())
        self.assertEqual(self.events_of(ollivander.PROVE_KIND), [])

    def test_a_stop_or_update_marker_that_lands_during_the_proof_is_never_cleared(self):
        self.switch_on()
        for index, name in enumerate((config.STOP_FILE, config.UPDATING_FILE)):
            with self.subTest(name=name):
                self.castle_cli("ollivander", "clear")

                def land(probe, name=name):
                    if probe == config.PROBE_EXEC:
                        ollivander.write_stop("landed meanwhile", OCT4, name)

                versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                            config.CODEX_BIN: [b"codex 0.160.0\n", f"codex 0.18{index}.0\n".encode()]}
                prober = FakeProber(during=land)
                for key, result in prober.results.items():
                    prober.results[key] = (result[0], result[1].replace(b"0.161.0", f"0.18{index}.0".encode()))
                updates = self.prove_pass(now=OCT4 + index * 60, prober=prober, versions=versions)
                self.assertEqual((updates["prove"]["proven"], updates["prove"]["cleared"]), (True, False))
                self.assertTrue(self.codex_stop().exists())
                self.assertTrue((self.state / name).exists())
                note = self.events_of("ollivander.proven")[-1]["summary"]
                self.assertIn("proven, but", note)
                self.assertIn("left the Codex stop in place", note)
                self.assertNotIn("resumed", note)
        self.assertEqual(self.events_of("ollivander.prove-failed"), [])

    def test_a_codex_stop_written_during_the_update_is_never_overwritten_or_cleared(self):
        self.switch_on()

        def land(argv):
            if argv[1:] == ["upgrade", "--cask", "codex"]:
                ollivander.write_stop("written by hand during the update", OCT4 - 5, family="codex")

        prober = FakeProber()
        runner = FakeRunner(versions={name: list(said) for name, said in NEW_CODEX.items()}, during=land)
        updates = self.keeper(runner=runner, prober=prober)["updates"]
        self.assertIsNone(updates["prove"])
        self.assertEqual(prober.calls, [])
        self.assertTrue(self.codex_stop().exists())
        self.assertIn("codex-boundary-test.sh", self.events_of("ollivander.stopped")[-1]["summary"])
        self.assertEqual(self.events_of(ollivander.PROVE_KIND), [])

    def test_a_codex_stop_replaced_during_the_proof_stays_and_the_proof_is_stored_first(self):
        self.switch_on()

        def replace(probe):
            if probe == config.PROBE_EXEC:
                ollivander.write_stop("a newer reason", OCT4 + 7, family="codex")

        updates = self.prove_pass(prober=FakeProber(during=replace))
        self.assertEqual((updates["prove"]["proven"], updates["prove"]["cleared"]), (True, False))
        self.assertTrue(self.codex_stop().read_text().startswith(f"{OCT4 + 7} a newer reason"))
        self.assertIn("not the one this proof is for", self.events_of("ollivander.proven")[-1]["summary"])
        self.assertEqual(len(self.events_of(ollivander.PROVE_RESULT_KIND)), 1)
        # The removal itself moves the stop aside and checks it there: one of another stamp goes back untouched.
        self.assertEqual(ollivander._remove_codex_stop(OCT4), "the Codex stop changed while Ollivander cleared it")
        self.assertTrue(self.codex_stop().read_text().startswith(f"{OCT4 + 7} a newer reason"))
        self.assertEqual(sorted(path.name for path in self.state.iterdir() if path.name.startswith(".")), [])
        self.assertIsNone(ollivander._remove_codex_stop(OCT4 + 7))
        self.assertFalse(self.codex_stop().exists())
        self.assertEqual(ollivander._remove_codex_stop(OCT4 + 7), ollivander.NOTHING_TO_CLEAR)

    def test_codex_moving_while_the_probes_ran_is_no_proof(self):
        self.switch_on()
        versions = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"],
                    config.CODEX_BIN: [b"codex 0.160.0\n", b"codex 0.161.0\n", b"codex 0.162.0\n"]}
        updates = self.prove_pass(prober=FakeProber(), versions=versions)
        self.assertEqual(updates["prove"]["proven"], False)
        self.assertIn("Codex moved to codex 0.162.0 while it ran", self.events_of("ollivander.prove-failed")[-1]["summary"])
        self.assertTrue(self.codex_stop().exists())

    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_parsers_match_the_kits_probes_and_install_copies_them(self):
        shell = (KIT.parent / "scripts" / config.PROBE_SHELL).read_text()
        self.assertEqual(ollivander._SHELL_CODEX.findall(shell), [kit_setting("CODEX_BIN")])
        for line in ('echo "Codex version: ', 'echo "All $passed checks passed.', 'echo "$failed check(s) failed, $passed passed.'):
            self.assertIn(line, shell)
        probe = (KIT.parent / "scripts" / config.PROBE_EXEC).read_text()
        for line in ('print(f"Launcher code: {CODE_FROM}")', 'return INSTALLED, "the installed office", None',
                     'print(f"All {passed} checks passed for ',
                     'print(f"{failed} check(s) failed or were inconclusive, {passed} passed.'):
            self.assertIn(line, probe)
        self.assertTrue(ollivander._SHELL_PASSED.match("All 11 checks passed. The profile"))
        self.assertTrue(ollivander._SHELL_FAILED.match("1 check(s) failed, 10 passed. The test folder"))
        self.assertTrue(ollivander._EXEC_FAILED.match("1 check(s) failed or were inconclusive, 26 passed. Copy"))
        install = (KIT.parent / "install.sh").read_text()
        self.assertIn(f'mkdir -m 700 "$OFFICE/{config.PROBES_DIR}"', install)
        self.assertIn(f'cp "$REPO_DIR/scripts/{config.PROBE_SHELL}" "$REPO_DIR/scripts/{config.PROBE_EXEC}"'
                      f' "$OFFICE/{config.PROBES_DIR}/"', install)
        self.assertIn('sed "s#^CODEX=.*#CODEX=$codex_bin#" "$probe"', install)


class RunProbeTests(FleetCase):
    """run_probe starts a real process: its stdout comes back, and a timeout kills its whole process group."""

    def test_output_comes_back_and_a_timeout_kills_the_group(self):
        script = self.write_file(self.tmp / "probe.sh", "echo started\n/bin/sleep 30 &\necho $! > \"$1\"\nwait\n")
        marker = self.tmp / "child.pid"
        self.assertEqual(ollivander.run_probe(["/bin/sh", "-c", "echo All 1 checks passed."], 30),
                         (0, b"All 1 checks passed.\n"))
        code, out = ollivander.run_probe(["/bin/sh", str(script), str(marker)], 1)
        self.assertEqual((code, out), (-1, b""))
        child = int(marker.read_text().strip())
        for _ in range(100):  # the killed child is reaped once it is reparented
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the probe's child outlived its timeout")


class RunDeskModelTests(OllivanderCase):
    def setUp(self):
        super().setUp()
        # What the last pass saw, as Ollivander records it before any switch: a revert lands only on a
        # model the latest catalog still lists.
        wands.record_catalog(self.conn, "codex", ["gpt-6-astra", "gpt-6.1-sol"], now=NOW - 1)
        wands.record_catalog(self.conn, "claude", ["opus", "sonnet", "haiku", "quill"], now=NOW - 1)

    def dry_run(self, desk: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = run_desk.main([desk, "--dry-run"])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def overrides(self, argv: list) -> list:
        return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]

    def request(self, recipient: str) -> str:
        payload = {"to": recipient, "kind": "request", "subject": "do it", "body": "please"}
        self.write_owl("mcgonagall", f"{recipient}-{len(os.listdir(self.outbox('mcgonagall')))}.json", payload)
        with mock.patch.object(run_desk, "spawn"):
            [delivered] = owl_post.run_pass(self.conn, now=NOW)["delivered"]
        return delivered["owl_id"]

    def real_run(self, desk: str, code: int, out: bytes = b"", err: bytes = b""):
        """A launch with a fake CLI: the exit code, or "capped" when the cap refused it."""
        def fake(argv, **kwargs):
            os.write(kwargs["stdout"], out)
            os.write(kwargs["stderr"], err)
            return subprocess.CompletedProcess(args=argv, returncode=code)

        owl_id = self.request(desk)
        with fake_children(fake):
            try:
                return run_desk.run(self.conn, desk, owl_id, now=NOW)["exit_code"]
            except run_desk.Capped:
                return "capped"

    def test_a_claude_desk_passes_its_model_and_effort(self):
        wands.apply_model(self.conn, "hermione", "sonnet", "xhigh", "workhorse", "initial", now=NOW)
        plan = self.dry_run("hermione")
        argv = plan["argv"]
        self.assertEqual(argv[argv.index("--model") + 1: argv.index("--model") + 4], ["sonnet", "--effort", "xhigh"])
        self.assertEqual((plan["model"], plan["effort"]), ("sonnet", "xhigh"))
        self.assertNotIn("--effort", self.dry_run("ron")["argv"])
        self.assertEqual(self.dry_run("ron")["argv"][self.dry_run("ron")["argv"].index("--model") + 1], "haiku")

    def test_a_codex_desk_passes_its_model_and_effort(self):
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "xhigh", "frontier", "initial", now=NOW)
        plan = self.dry_run("moody")
        argv = plan["argv"]
        self.assertEqual(argv[:4], [config.CODEX_BIN, "exec", "--ignore-user-config", "--ignore-rules"])
        overrides = self.overrides(argv)
        self.assertIn('model="gpt-6-astra"', overrides)
        self.assertEqual([item for item in overrides if item.startswith("model_reasoning_effort=")],
                         ['model_reasoning_effort="xhigh"'])
        self.assertIn('approval_policy="never"', overrides)
        self.assertNotIn("-m", argv)
        self.assertEqual(plan["model"], "gpt-6-astra")

    def test_a_codex_desk_without_a_model_keeps_todays_argv(self):
        plan = self.dry_run("moody")
        overrides = self.overrides(plan["argv"])
        self.assertFalse(any(item.startswith("model=") for item in overrides))
        self.assertIn('model_reasoning_effort="high"', overrides)
        self.assertEqual((plan["model"], plan["effort"]), ("codex-default", None))
        self.enable("moody")
        self.assertEqual(self.real_run("moody", 0), 0)
        self.assertEqual(wands.last_run_model(self.conn, "moody"), "codex-default")

    def test_the_real_model_is_recorded_and_a_move_is_reported(self):
        self.enable("hermione")

        def result(main: str) -> bytes:
            return json.dumps({"type": "result", "modelUsage": {
                "claude-haiku-4-5": {"outputTokens": 40}, main: {"outputTokens": 900}}}).encode()

        self.assertEqual(self.real_run("hermione", 0, result("claude-opus-5-5")), 0)
        self.assertEqual(wands.last_run_model(self.conn, "hermione"), "claude-opus-5-5")
        self.assertEqual(self.events_of("ollivander.moved"), [])
        self.real_run("hermione", 0, result("claude-opus-6"))
        [event] = self.events_of("ollivander.moved")
        self.assertEqual(event["summary"], "Hermione - Staff Engineer moved from claude-opus-5-5 to claude-opus-6")
        self.assertIsNone(run_desk.parse_claude_model(b"not json"))
        self.assertIsNone(run_desk.parse_claude_model(json.dumps({"modelUsage": {"bad name!": {}}}).encode()))

    def test_a_run_with_no_model_usage_reports_no_move(self):
        self.enable("hermione")
        pensieve.add_metric(self.conn, "hermione", "run-old", "opus", 1, 1, 0, 0.0, 1, ts=NOW)
        good = json.dumps({"type": "result", "modelUsage": {"claude-opus-5-5": {"outputTokens": 9}}}).encode()
        self.assertEqual(self.real_run("hermione", 0, good), 0)
        self.assertEqual(self.real_run("hermione", -1), -1)
        self.assertEqual(wands.last_run_model(self.conn, "hermione"), "opus")
        self.assertEqual(self.real_run("hermione", 0, good), 0)
        self.assertEqual(self.events_of("ollivander.moved"), [])

    def test_a_run_in_flight_across_a_switch_never_counts_toward_its_trial(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 0), 0)

        def switch_mid_run(argv, **kwargs):
            wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        owl_id = self.request("moody")
        with fake_children(switch_mid_run):
            run_desk.run(self.conn, "moody", owl_id, now=NOW)
        self.assertEqual(self.model("moody")["trial_failures"], 0)
        self.real_run("moody", 1)
        self.real_run("moody", 1)
        self.assertEqual(self.model("moody")["model"], "gpt-6-astra")

    def test_two_failed_runs_after_ryans_choice_are_reported_not_reverted(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 0), 0)
        wands.record_catalog(self.conn, "codex", ["gpt-6-astra", "gpt-6.1-sol"], now=NOW)
        wands.pin(self.conn, "moody", "gpt-6.1-sol", now=NOW + 1)
        self.real_run("moody", 1)
        self.real_run("moody", 1)
        self.assertEqual((self.model("moody")["model"], self.model("moody")["pinned"]), ("gpt-6.1-sol", 1))
        self.assertEqual(self.events_of("ollivander.reverted"), [])
        [event] = self.events_of("ollivander.trial-failed")
        self.assertIn("castle desk model moody gpt-6-astra", event["summary"])

    def test_a_stop_written_while_a_run_waits_on_the_lock_holds_it(self):
        self.enable("moody")
        owl_id = self.request("moody")
        real_lock = safefs.held_lock

        @contextlib.contextmanager
        def slow_lock(*args, **kwargs):
            self.write_file(self.office / "state" / config.STOP_FILE, "1 stopped\n")
            with real_lock(*args, **kwargs) as lock_fd:
                yield lock_fd

        with mock.patch.object(safefs, "held_lock", slow_lock), self.assertRaises(run_desk.Stopped):
            run_desk.run(self.conn, "moody", owl_id, now=NOW)
        self.assertEqual(wands.last_run_model(self.conn, "moody"), None)

    def test_a_run_that_waited_on_the_lock_takes_the_latest_model(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        owl_id = self.request("moody")
        real_lock = safefs.held_lock
        ran = []

        @contextlib.contextmanager
        def slow_lock(*args, **kwargs):
            wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "medium", "workhorse", "role", now=NOW + 1)
            with real_lock(*args, **kwargs) as lock_fd:
                yield lock_fd

        def fake(argv, **kwargs):
            ran.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(safefs, "held_lock", slow_lock), fake_children(fake):
            run_desk.run(self.conn, "moody", owl_id, now=NOW)
        self.assertIn('model="gpt-6.1-sol"', self.overrides(ran[0]))
        self.assertEqual(wands.last_run_model(self.conn, "moody"), "gpt-6.1-sol")
        self.assertIsNone(self.model("moody")["trial_failures"])

    def test_the_keeper_name_never_leaves_the_fleet(self):
        self.assertEqual(gitops.fleet_words_in("fix Ollivander model pick"), "ollivander")

    def test_two_failed_runs_after_a_switch_revert_and_pin(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 0), 0)
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        self.assertEqual(self.real_run("moody", 1), 1)
        self.assertEqual(self.model("moody")["model"], "gpt-6.1-sol")
        self.assertEqual(self.real_run("moody", 1), 1)
        row = self.model("moody")
        self.assertEqual((row["model"], row["pinned"]), ("gpt-6-astra", 1))
        [event] = self.events_of("ollivander.reverted")
        self.assertIn("castle desk model moody --role", event["summary"])
        self.assertEqual(self.overrides(self.dry_run("moody")["argv"]).count('model="gpt-6-astra"'), 1)

    def test_cap_refusals_and_usage_limits_never_count_toward_a_revert(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        limited = json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}).encode()
        self.assertEqual(self.real_run("moody", 1, out=limited), 1)
        self.assertEqual(self.real_run("moody", 1, err=b"ERROR: 429 Too Many Requests"), 1)
        self.assertEqual(self.model("moody")["trial_failures"], 0)
        with mock.patch.object(config, "DAILY_RUN_CAP", {**config.DAILY_RUN_CAP, "moody": 0}):
            self.assertEqual(self.real_run("moody", 1), "capped")
            self.assertEqual(self.real_run("moody", 1), "capped")
        self.assertEqual(self.model("moody")["model"], "gpt-6.1-sol")
        self.assertEqual(self.model("moody")["trial_failures"], 0)
        self.assertEqual(self.real_run("moody", 1), 1)
        self.assertEqual(self.real_run("moody", -9), -9)
        self.assertEqual(self.model("moody")["model"], "gpt-6-astra")

    def test_usage_limit_detection_reads_only_what_the_cli_reported(self):
        claude_limit = json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "x"}).encode()
        self.assertEqual(run_desk.plan_limit("claude", claude_limit, True), "claude_plan")
        self.assertEqual(run_desk.plan_limit("claude", b"", True, b"Claude AI usage limit reached"), "claude_plan")
        credit = json.dumps({"type": "result", "is_error": True, "result": "Credit balance is too low"}).encode()
        self.assertEqual(run_desk.plan_limit("claude", credit, True), "claude_plan")
        work = json.dumps({"type": "result", "is_error": False, "result": "notes on the rate limit code"}).encode()
        self.assertIsNone(run_desk.plan_limit("claude", work, True))
        self.assertIsNone(run_desk.plan_limit("claude", work, True, b"rate limit"))  # a result ended the run
        message = json.dumps({"type": "item.completed", "item": {"text": "usage limit docs"}}).encode()
        self.assertIsNone(run_desk.plan_limit("codex", message, True, b"exit 1"))
        failed = json.dumps({"type": "error", "message": "Quota exceeded for this account"}).encode()
        self.assertEqual(run_desk.plan_limit("codex", message + b"\n" + failed, True), "codex_plan")
        self.assertEqual(run_desk.plan_limit("codex", message, True, b"ERROR: your credit balance is empty"),
                         "codex_plan")
        crash = json.dumps({"type": "turn.failed", "error": {"message": "sandbox denied"}}).encode()
        self.assertIsNone(run_desk.plan_limit("codex", crash, True, b"WARN rate limit, retrying"))
        # Only a failed run is read, whatever it printed.
        self.assertIsNone(run_desk.plan_limit("codex", message + b"\n" + failed, False, b"quota"))
        self.assertIsNone(run_desk.plan_limit("claude", b"", False, b"quota"))
        self.assertFalse(hasattr(config, "USAGE_LIMIT_WORDS"))
        self.assertFalse(hasattr(run_desk, "hit_usage_limit"))

    def test_a_timeout_or_a_crash_is_never_read_as_a_vendor_limit_from_stderr(self):
        cut = json.dumps({"type": "system", "subtype": "init"}).encode()
        retried = b"API Error (429 rate_limit_error) Retrying in 4s"
        self.assertEqual(run_desk.plan_limit("claude", cut, True, retried), "claude_plan")
        self.assertIsNone(run_desk.plan_limit("claude", cut, True, retried, timed_out=True))
        self.assertIsNone(run_desk.plan_limit("codex", b"", True, b"stream error: 429 Too Many Requests; retrying 1/5",
                                              timed_out=True))
        self.assertIsNone(run_desk.plan_limit("claude", b"", True, b"Error: boom\n    at Foo (/opt/claude/cli.js:429:17)\n"))
        self.assertIsNone(run_desk.plan_limit("claude", b"", True, b"EDQUOT: disk quota exceeded"))
        self.assertIsNone(run_desk.plan_limit("codex", b"", True, b"WARN rate limit, retrying\nError: sandbox denied"))
        self.assertEqual(run_desk.plan_limit("codex", b"", True, b"stream error: 429 Too Many Requests"), "codex_plan")

    def test_a_run_that_timed_out_counts_toward_the_trial_whatever_its_stderr_says(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)

        def hang(argv, **kwargs):
            os.write(kwargs["stderr"], b"stream error: 429 Too Many Requests; retrying 1/5\n")
            raise subprocess.TimeoutExpired(argv, 1)

        for _ in range(2):
            owl_id = self.request("moody")
            with fake_children(hang):
                result = run_desk.run(self.conn, "moody", owl_id, now=NOW)
            self.assertEqual((result["timed_out"], result["cap_source"]), (True, None))
        self.assertEqual(capacity.list_cap_hits(self.conn, "moody"), [])
        self.assertEqual(self.model("moody")["model"], "gpt-6-astra")

    def test_stream_json_output_names_the_model_and_the_limit(self):
        init = json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"}).encode()
        talk = json.dumps({"type": "assistant", "message": {"content": [{"type": "text",
                                                                         "text": "notes on the rate limit"}]}}).encode()
        done = json.dumps({"type": "result", "is_error": False,
                           "modelUsage": {"claude-opus-5-5": {"outputTokens": 9}}}).encode()
        self.assertEqual(run_desk.parse_claude_model(b"\n".join((init, talk, done))), "claude-opus-5-5")
        self.assertIsNone(run_desk.plan_limit("claude", b"\n".join((init, talk, done)), True))
        limited = json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "x"}).encode()
        self.assertEqual(run_desk.plan_limit("claude", b"\n".join((init, talk, limited)), True), "claude_plan")

    def test_a_stderr_only_vendor_limit_is_labelled_and_never_counts_toward_a_revert(self):
        self.enable("hermione")
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        for _ in range(2):
            self.assertEqual(self.real_run("hermione", 1, err=b"Error: quota exceeded for this organization"), 1)
        self.assertEqual(self.model("hermione")["trial_failures"], 0)
        self.assertEqual([(hit["cap"], hit["cap_source"]) for hit in capacity.list_cap_hits(self.conn, "hermione")],
                         [("plan", "claude_plan"), ("plan", "claude_plan")])
        self.assertEqual(self.real_run("hermione", 1), 1)
        self.assertEqual(self.real_run("hermione", 1), 1)
        self.assertEqual(self.model("hermione")["model"], "opus")

    def test_a_run_the_caps_label_a_plan_limit_never_counts_toward_a_revert(self):
        self.enable("hermione")
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        limited = json.dumps({"type": "result", "is_error": True, "result": "5-hour limit reached"}).encode()
        self.assertEqual(self.real_run("hermione", 1, out=limited), 1)
        self.assertEqual(self.real_run("hermione", 1, out=limited), 1)
        self.assertEqual(self.model("hermione")["trial_failures"], 0)
        self.assertEqual(self.model("hermione")["model"], "sonnet")
        self.assertEqual(self.events_of("ollivander.reverted"), [])

    def test_a_desk_on_a_blocked_model_is_never_launched(self):
        self.enable("hermione")
        wands.apply_model(self.conn, "hermione", "claude-quill-2", "high", "frontier", "initial", now=NOW)
        owl_id = self.request("hermione")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS), \
                mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")):
            for _ in range(2):
                with self.assertRaisesRegex(run_desk.Blocked, "claude-quill-2 is blocked"):
                    run_desk.run(self.conn, "hermione", owl_id, now=NOW)
            self.assertTrue(self.dry_run("hermione")["blocked"])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch("time.time", return_value=NOW):
                self.assertEqual(run_desk.main(["hermione", "--owl", owl_id]), 1)
        [event] = self.events_of("rundesk.blocked")
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn("castle desk model hermione <model>", event["summary"])
        self.assertEqual(self.events_of("rundesk.failed"), [])
        self.assertIsNone(wands.last_run_model(self.conn, "hermione"))
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "hermione")], [owl_id])
        self.assertFalse(self.dry_run("hermione")["blocked"])

    def test_a_registry_model_that_is_blocked_is_refused_too(self):
        self.enable("ron")
        real_get_desk = pensieve.get_desk

        def registry(conn, name):  # registry rows are immutable, so ron's registry model is read as wisp
            row = real_get_desk(conn, name)
            return {**row, "model": "wisp"} if row["name"] == "ron" else row

        owl_id = self.request("ron")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS), \
                mock.patch.object(pensieve, "get_desk", side_effect=registry), \
                mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")):
            self.assertEqual(self.dry_run("ron")["model"], "wisp")
            with self.assertRaises(run_desk.Blocked):
                run_desk.run(self.conn, "ron", owl_id, now=NOW)
        self.assertEqual(len(self.events_of("rundesk.blocked")), 1)

    def test_an_alias_that_resolved_to_a_blocked_id_is_refused_on_every_desk(self):
        self.enable("hermione")
        out = json.dumps({"type": "result", "modelUsage": {"claude-quill-9": {"outputTokens": 9}}}).encode()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)):
            self.assertEqual(self.real_run("hermione", 0, out), 0)
            [event] = self.events_of("rundesk.blocked")
            self.assertIn("ran on claude-quill-9, which is blocked here: opus resolved to it", event["summary"])
            owl_id = self.request("hermione")
            with mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")):
                for _ in range(2):
                    with self.assertRaisesRegex(run_desk.Blocked, "opus, which once resolved to claude-quill-9,"):
                        run_desk.run(self.conn, "hermione", owl_id, now=NOW)
            self.assertTrue(self.dry_run("hermione")["blocked"])
            self.assertEqual(len(self.events_of("rundesk.blocked")), 2)
            wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
            self.assertFalse(self.dry_run("hermione")["blocked"])
            # What opus resolved to is known office-wide, not only on the desk that ran it.
            self.assertTrue(self.dry_run("portrait")["blocked"])

    def test_a_failed_trial_never_reverts_onto_a_model_blocked_since(self):
        self.enable("hermione")
        wands.apply_model(self.conn, "hermione", "quill", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW + 1)
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS):
            self.assertEqual(self.real_run("hermione", 1), 1)
            self.assertEqual(self.real_run("hermione", 1), 1)
            row = self.model("hermione")
            self.assertEqual((row["model"], row["pinned"], row["trial_failures"]), ("opus", 0, None))
            self.assertEqual(self.events_of("ollivander.reverted"), [])
            [event] = self.events_of("ollivander.revert-blocked")
            self.assertIn("quill, the model it came from, is blocked here, so it stays on opus", event["summary"])
            self.assertEqual(self.real_run("hermione", 0), 0)

    def test_a_blocked_model_set_in_the_codex_profile_is_refused(self):
        self.enable("moody")
        profile = self.office / "desks" / "moody" / config.CODEX_PROFILE_FILE
        self.write_file(profile, 'model = "gpt-zz-quill"\n' + CODEX_PROFILE)
        self.assertEqual(self.dry_run("moody")["model"], "gpt-zz-quill")
        owl_id = self.request("moody")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS), \
                mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")):
            self.assertTrue(self.dry_run("moody")["blocked"])
            with self.assertRaisesRegex(run_desk.Blocked, "gpt-zz-quill is blocked"):
                run_desk.run(self.conn, "moody", owl_id, now=NOW)
            # A profile the codex.toml selects names the model of a desk with none of its own, so it is checked.
            self.write_file(profile, 'profile = "deep"\n' + CODEX_PROFILE + '[profiles.deep]\nmodel = "gpt-zz-quill"\n')
            self.assertEqual(self.dry_run("moody")["model"], "gpt-zz-quill")
            self.assertTrue(self.dry_run("moody")["blocked"])
            # Once the desk has a model, it replaces the profile's, so the blocked one never runs.
            wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
            self.assertEqual(self.dry_run("moody")["model"], "gpt-6-astra")
            self.assertFalse(self.dry_run("moody")["blocked"])
        self.write_file(profile, 'model = "gpt zz"\n' + CODEX_PROFILE)
        with self.assertRaisesRegex(FleetError, "codex profile model is not a plain name"):
            run_desk.build_plan(self.conn, "moody")

    def test_a_registry_model_with_a_label_suffix_is_checked_and_launches(self):
        self.enable("ron")
        real_get_desk = pensieve.get_desk

        def registry(conn, name):
            row = real_get_desk(conn, name)
            return {**row, "model": "opus[1m]"} if row["name"] == "ron" else row

        with mock.patch.object(pensieve, "get_desk", side_effect=registry):
            self.assertFalse(self.dry_run("ron")["blocked"])
            self.assertEqual(self.real_run("ron", 0), 0)
            with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("opus",)):
                self.assertTrue(self.dry_run("ron")["blocked"])
        self.assertEqual(self.events_of("rundesk.failed"), [])

    def test_a_model_label_check_never_skips_the_trial(self):
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        plan = {"desk": "hermione", "run_id": "run-1", "model": "sonnet", "default_model": "opus",
                "change_id": wands.current_change(self.conn, "hermione")}
        for _ in range(2):
            run_desk.after_run(self.conn, plan, None, "claude-quill-9-9[1m]", 1, NOW)
        self.assertEqual((self.model("hermione")["model"], self.model("hermione")["pinned"]), ("opus", 1))

    def test_the_stop_file_blocks_every_launch(self):
        self.enable("hermione")
        self.write_file(self.office / "state" / config.STOP_FILE, "1 stopped\n")
        owl_id = self.request("hermione")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = run_desk.main(["hermione", "--owl", owl_id])
        self.assertEqual(code, 1)
        self.assertIn("stop file", err.getvalue())
        self.assertEqual(self.events_of("rundesk.failed"), [])
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "hermione")], [owl_id])
        self.assertTrue(self.dry_run("hermione")["stopped"])
        self.assertEqual(config.STOP_FILE, wands.STOP_FILE)
        self.assertEqual(config.STOP_FAMILY_FILES, wands.STOP_FAMILY_FILES)

    def test_a_family_stop_holds_only_that_familys_launches_and_turns(self):
        desks = {"codex": "moody", "claude": "hermione"}
        for desk in desks.values():
            self.enable(desk)
        for stopped, other in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(stopped=stopped):
                ollivander.write_stop(f"a new {stopped} version", NOW, family=stopped)
                owl_id = self.request(desks[stopped])
                with self.assertRaises(run_desk.Stopped) as caught:
                    run_desk.run(self.conn, desks[stopped], owl_id, now=NOW)
                self.assertIn(f"stop file for the {stopped} family is in place, so no {stopped} headless desk",
                              str(caught.exception))
                self.assertTrue(self.dry_run(desks[stopped])["stopped"])
                self.assertFalse(self.dry_run(desks[other])["stopped"])
                self.assertEqual(self.real_run(desks[other], 0), 0)  # the other family launches as before
                if stopped == "claude":  # the headless Claude turns (owl report, Hermione's chat review) wait too
                    with self.assertRaises(run_desk.Stopped):
                        run_desk.check_report_launch(self.conn)
                else:
                    run_desk.check_report_launch(self.conn)
                os.unlink(self.office / "state" / config.STOP_FAMILY_FILES[stopped])

    def test_a_stop_of_every_family_old_or_by_hand_holds_both_families(self):
        old = "1 codex --version went from 0.1.0 to 0.2.0, so run the real boundary probe first\n"
        for text in (old, ""):  # the format an older office wrote, and a file made by hand
            with self.subTest(text=text):
                self.write_file(self.office / "state" / config.STOP_FILE, text)
                for desk in ("moody", "hermione"):
                    self.assertTrue(self.dry_run(desk)["stopped"])
                for family in (*config.STOP_FAMILY_FILES, None):
                    self.assertTrue(run_desk.stop_requested(family))
                with self.assertRaises(run_desk.Stopped) as caught:
                    run_desk.check_report_launch(self.conn)
                self.assertIn("stop file for every family", str(caught.exception))
                data = self.castle_cli("ollivander", "clear")[1]["data"]
                self.assertEqual([item["stopped"] for item in data["stopped"]], ["every"])
                self.assertFalse(run_desk.stop_requested())

    # Moody B2: an alias's resolution history survives every switch.

    def test_a_launch_after_switching_away_and_back_is_still_refused(self):
        self.enable("hermione")
        ran = json.dumps({"type": "result", "modelUsage": {"claude-quill-9": {"outputTokens": 9}}}).encode()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)):
            self.assertEqual(self.real_run("hermione", 0, ran), 0)
            self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-quill-9")
            wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
            sonnet = json.dumps({"type": "result", "modelUsage": {"claude-sonnet-5": {"outputTokens": 9}}}).encode()
            self.assertEqual(self.real_run("hermione", 0, sonnet), 0)
            # Every way back onto opus is refused while its latest known full id is blocked.
            code, out = self.castle_cli("desk", "model", "hermione", "opus")
            self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
            self.assertIn("opus once ran as claude-quill-9", out["error"]["message"])
            with self.assertRaises(ConflictError):
                wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW + 2,
                                  blocked=config.BLOCKED_MODEL_PREFIXES)
            # A switch that did not check (the prefix was added after it) still never launches.
            wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW + 2)
            owl_id = self.request("hermione")
            with mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")):
                with self.assertRaisesRegex(run_desk.Blocked, "opus, which once resolved to claude-quill-9,"):
                    run_desk.run(self.conn, "hermione", owl_id, now=NOW + 3)
            self.assertTrue(self.dry_run("hermione")["blocked"])
        self.assertEqual(wands.last_run_model(self.conn, "hermione"), "claude-sonnet-5")

    def test_a_failed_trial_never_reverts_onto_an_alias_that_ran_as_a_blocked_id(self):
        self.enable("hermione")
        ran = json.dumps({"type": "result", "modelUsage": {"claude-quill-9": {"outputTokens": 9}}}).encode()
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("hermione", 0, ran), 0)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)):
            self.assertEqual(self.real_run("hermione", 1), 1)
            self.assertEqual(self.real_run("hermione", 1), 1)
            row = self.model("hermione")
            self.assertEqual((row["model"], row["pinned"], row["trial_failures"]), ("sonnet", 0, None))
            self.assertEqual(self.events_of("ollivander.reverted"), [])
            [event] = self.events_of("ollivander.revert-blocked")
            self.assertIn("opus, the model it came from, once ran as claude-quill-9, which is blocked here, so it"
                          " stays on sonnet", event["summary"])
            self.assertEqual(self.real_run("hermione", 0), 0)
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "hermione")], ["initial", "role"])

    def test_ollivander_never_picks_an_alias_that_ran_as_a_blocked_id(self):
        wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW)
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)):
            plan = self.keeper()
        self.assertEqual(plan["catalog"]["claude"]["ran_as"], {"opus": "claude-quill-9"})
        self.assertIn("opus", plan["blocked"])
        hermione = self.desk(plan, "hermione")
        self.assertEqual((hermione["action"], hermione["skipped_blocked"]), ("blocked", ["opus"]))
        self.assertNotIn("opus", [self.model(desk)["model"] for desk in HEADLESS])

    # Moody B3: the desk's model and effort always win over a profile the codex.toml selects.

    def deep_profile(self, model: str = "gpt-zz-other", effort: str = "low") -> None:
        self.write_file(self.office / "desks" / "moody" / config.CODEX_PROFILE_FILE,
                        'profile = "deep"\n' + CODEX_PROFILE
                        + f'[profiles.deep]\nmodel = "{model}"\nmodel_reasoning_effort = "{effort}"\n')

    def test_a_pinned_desk_runs_its_pin_whatever_the_selected_profile_says(self):
        self.enable("moody")
        self.deep_profile()
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.record_catalog(self.conn, "codex", ["gpt-6-astra", "gpt-6.1-sol"], now=NOW)
        wands.pin(self.conn, "moody", "gpt-6.1-sol", now=NOW + 1)
        plan = self.dry_run("moody")
        overrides = self.overrides(plan["argv"])
        self.assertEqual((plan["model"], plan["effort"]), ("gpt-6.1-sol", "high"))
        self.assertIn('profile="deep"', overrides)
        self.assertIn('profiles.deep.model="gpt-6.1-sol"', overrides)
        self.assertIn('profiles.deep.model_reasoning_effort="high"', overrides)
        self.assertEqual([item for item in overrides if "gpt-zz-other" in item or item.endswith('="low"')], [])
        self.assertEqual(self.real_run("moody", 0), 0)
        self.assertEqual(wands.last_run_model(self.conn, "moody"), "gpt-6.1-sol")
        self.assertEqual(capacity.list_launches(self.conn, "moody")[-1]["model"], "gpt-6.1-sol")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("gpt-zz-",)):
            self.assertFalse(self.dry_run("moody")["blocked"])  # the profile's own model never runs

    def test_a_trial_counts_the_model_that_ran_not_the_selected_profiles(self):
        self.enable("moody")
        self.deep_profile()
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 0), 0)
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "medium", "workhorse", "role", now=NOW + 1)
        launched = []

        def failing(argv, **kwargs):
            launched.append(self.overrides(argv))
            return subprocess.CompletedProcess(args=argv, returncode=1)

        for _ in range(2):
            owl_id = self.request("moody")
            with fake_children(failing):
                run_desk.run(self.conn, "moody", owl_id, now=NOW + 2)
        for overrides in launched:
            self.assertIn('profiles.deep.model="gpt-6.1-sol"', overrides)
            self.assertIn('profiles.deep.model_reasoning_effort="medium"', overrides)
        self.assertEqual([launch["model"] for launch in capacity.list_launches(self.conn, "moody")],
                         ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-sol"])
        row = self.model("moody")
        self.assertEqual((row["model"], row["pinned"]), ("gpt-6-astra", 1))  # gpt-6.1-sol's own trial failed
        self.assertIn('profiles.deep.model="gpt-6-astra"', self.overrides(self.dry_run("moody")["argv"]))

    def test_a_selected_profile_whose_model_cannot_be_set_is_refused(self):
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.write_file(self.office / "desks" / "moody" / config.CODEX_PROFILE_FILE,
                        'profile = "deep.one"\n' + CODEX_PROFILE)
        with self.assertRaisesRegex(FleetError, "not a bare key"):
            run_desk.build_plan(self.conn, "moody")

    # Moody B1: a launch and a CLI update never overlap.

    def update_marker(self) -> None:
        self.write_file(self.office / "desks" / "ollivander" / config.UPDATE_MARKER, "", mode=0o644)

    def update_lock_free(self) -> bool:
        with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as fd:
            try:
                with safefs.held_lock(fd, config.UPDATE_LOCK, blocking=False):
                    return True
            except safefs.Busy:
                return False

    def test_an_update_cannot_start_between_a_runs_last_stop_check_and_its_launch(self):
        # The interleaving Moody found: the run passes its last stop check, then an update starts and replaces
        # the binary before the run launches it. The update now waits for the launch, and a pass that cannot
        # wait skips the update: no marker, no command, nothing replaced.
        self.enable("moody")
        self.update_marker()
        owl_id = self.request("moody")
        seen = {}

        def updater_arrives():
            with mock.patch.object(config, "UPDATE_LOCK_WAIT_SECONDS", 0):
                seen["during_launch"] = ollivander.update_clis(self.conn, self.runner, OCT4, False)
            seen["marker"] = run_desk.stop_requested()
            seen["commands"] = list(self.runner.calls)

        def desk_runs(argv, **kwargs):
            # The process runs and holds the gate it inherited: no update may replace its binary now.
            lock = os.stat(self.office / "locks" / config.UPDATE_LOCK).st_ino
            seen["handed_down"] = lock in [os.fstat(fd).st_ino for fd in kwargs["pass_fds"]]
            seen["lock_free"] = self.update_lock_free()
            with mock.patch.object(config, "UPDATE_LOCK_WAIT_SECONDS", 0):
                seen["while_running"] = ollivander.update_clis(self.conn, self.runner, OCT4, False)
            seen["commands_while_running"] = list(self.runner.calls)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with fake_children(desk_runs):
            result = run_desk.run(self.conn, "moody", owl_id, now=NOW, on_start=updater_arrives)
        self.assertEqual(result["exit_code"], 0)
        during = seen["during_launch"]
        self.assertEqual((during["busy"], during["ran"], during["versions"]), (True, [], None))
        self.assertEqual((seen["marker"], seen["commands"]), (False, []))
        self.assertEqual((seen["handed_down"], seen["lock_free"]), (True, False))
        while_running = seen["while_running"]
        self.assertEqual((while_running["busy"], while_running["ran"]), (True, []))
        self.assertEqual(seen["commands_while_running"], [])
        # Once the run has ended, the gate is down and the next pass updates.
        self.assertTrue(self.update_lock_free())
        after = ollivander.update_clis(self.conn, self.runner, OCT4, False)
        self.assertEqual([item["exit_code"] for item in after["ran"]], [0, 0])
        self.assertFalse(run_desk.stop_requested())

    def test_a_launch_never_waits_for_an_update_and_never_starts_during_one(self):
        self.enable("moody")
        owl_id = self.request("moody")
        with ollivander.update_lock():
            # The marker is not written yet: the lock alone refuses, at once, with the desk lock taken first.
            self.assertFalse(run_desk.stop_requested())
            with mock.patch.object(config, "DESK_LOCK_WAIT_SECONDS", 0), \
                    self.assertRaisesRegex(run_desk.Stopped, "a CLI update is running"):
                run_desk.run(self.conn, "moody", owl_id, now=NOW)
        self.assertEqual(capacity.list_launches(self.conn, "moody"), [])
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "moody")], [owl_id])
        with fake_children():
            self.assertEqual(run_desk.run(self.conn, "moody", owl_id, now=NOW)["exit_code"], 0)

    def test_launches_share_the_gate_and_an_update_holding_it_takes_no_desk_lock(self):
        # Lock order: desk lock, then the update lock without waiting, for a run; ollivander.lock, then the
        # update lock, and never a desk lock, for an update. So an update runs while every desk lock is held.
        self.enable("moody")
        self.enable("hermione")
        self.update_marker()
        with run_desk.launch_gate(), run_desk.launch_gate():  # two launches at once never wait on each other
            with mock.patch.object(config, "UPDATE_LOCK_WAIT_SECONDS", 0):
                self.assertTrue(self.keeper()["updates"]["busy"])
        with contextlib.ExitStack() as held:
            for desk in HEADLESS:
                held.enter_context(run_desk.desk_lock(desk, wait=False))
            with mock.patch.object(config, "UPDATE_LOCK_WAIT_SECONDS", 0):
                updates = self.keeper(now=OCT4 + 60)["updates"]
        self.assertFalse(updates["busy"])
        self.assertEqual([check["ok"] for check in updates["checks"]], [True, True, True, True])
        self.assertFalse(run_desk.stop_requested())
        self.assertTrue(self.update_lock_free())


    # Review of the keeper after Moody's round.

    def test_an_update_that_died_part_way_stops_launches_on_the_next_pass(self):
        # Pass 1 replaces codex and dies before its checks. Pass 2 reads the new binary as "before", so a
        # before and after compare can never see the move: the marker it left must stop launches instead.
        self.update_marker()
        self.enable("moody")
        moved = {config.CLAUDE_BIN: [b"2.0.0 (Claude Code)\n"], config.CODEX_BIN: [b"codex 0.160.0\n"]}

        def die(argv):
            moved[config.CODEX_BIN][:] = [b"codex 0.161.0\n"]
            raise FleetError("the pass died")

        with self.assertRaises(FleetError):
            self.keeper(runner=FakeRunner(versions=moved, during=die))
        self.assertTrue(run_desk.stop_requested())
        self.assertEqual(self.events_of("ollivander.stopped"), [])
        runner = FakeRunner(versions=moved)
        plan = self.keeper(now=OCT4 + 60, runner=runner)
        updates = plan["updates"]
        self.assertEqual((updates["unfinished"], updates["stopped"], updates["ran"]), (True, True, []))
        self.assertEqual([call for call in runner.calls if call[1] is not None], [])
        self.assertTrue((self.office / "state" / config.STOP_FILE).exists())
        self.assertTrue((self.office / "state" / config.UPDATING_FILE).exists())
        self.assertEqual(updates["stopped_families"], ["every"])  # no one CLI to pin it on: both families stop
        self.assertTrue(all(run_desk.stop_requested(family) for family in config.STOP_FAMILY_FILES))
        [event] = self.events_of("ollivander.stopped")
        self.assertIn("a CLI update did not finish", event["summary"])
        self.assertIn("codex-boundary-test.sh", event["summary"])
        # Later passes keep it stopped and tell Ryan once; a dry run only reports it.
        self.keeper(now=OCT4 + 120, runner=FakeRunner(versions=moved))
        self.assertEqual(len(self.events_of("ollivander.stopped")), 1)
        self.assertTrue(self.keeper(dry_run=True, runner=FakeRunner(versions=moved))["updates"]["unfinished"])
        owl_id = self.request("moody")
        with self.assertRaises(run_desk.Stopped):
            run_desk.run(self.conn, "moody", owl_id, now=NOW)
        self.assertEqual(self.castle_cli("ollivander", "clear")[1]["data"]["cleared"], True)
        self.assertFalse(run_desk.stop_requested())
        later = self.keeper(now=OCT4 + 180, runner=FakeRunner(versions=moved))["updates"]
        self.assertEqual((later["unfinished"], later["stopped"], len(later["ran"])), (False, False, 2))

    def test_a_switch_landing_while_the_plan_reads_the_model_never_pairs_it_with_the_old_model(self):
        self.enable("hermione")
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        real_change = wands.current_change
        landed = []

        def ollivander_switches(conn, desk):
            # Ollivander's own connection applies a role pick between the two reads.
            if desk == "hermione" and not landed:
                other = common.connect()
                try:
                    landed.append(wands.apply_model(other, "hermione", "sonnet", "high", "workhorse", "role",
                                                    now=NOW + 1)["change_id"])
                finally:
                    other.close()
            return real_change(conn, desk)

        with mock.patch.object(wands, "current_change", side_effect=ollivander_switches):
            plan = run_desk.build_plan(self.conn, "hermione")
        self.assertEqual(len(landed), 1)
        [planned] = [change for change in wands.changes(self.conn, "hermione") if change["id"] == plan["change_id"]]
        self.assertEqual((plan["model"], planned["to_model"]), ("opus", "opus"))
        # So a failed opus run never counts toward sonnet's trial.
        run_desk.after_run(self.conn, plan, None, None, 1, NOW + 2)
        self.assertEqual(self.model("hermione")["trial_failures"], 0)

    def test_a_blocked_model_that_did_less_of_the_work_is_still_caught(self):
        self.enable("hermione")
        self.enable("portrait")
        mixed = json.dumps({"type": "result", "modelUsage": {
            "claude-quill-9": {"outputTokens": 5}, "claude-haiku-4-5": {"outputTokens": 40}}}).encode()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)):
            self.assertEqual(self.real_run("hermione", 0, mixed), 0)
            [event] = self.events_of("rundesk.blocked")
            self.assertIn("ran on claude-quill-9", event["summary"])
            self.assertEqual(wands.blocked_resolution(self.conn, "opus", config.BLOCKED_MODEL_PREFIXES),
                             "claude-quill-9")
            self.assertEqual(wands.last_run_model(self.conn, "hermione"), "claude-haiku-4-5")
            self.assertTrue(self.dry_run("hermione")["blocked"])
            # A run that launched before that one ended, and ends after it on an allowed id, never lifts it.
            allowed = json.dumps({"type": "result", "modelUsage": {"claude-opus-5-5": {"outputTokens": 9}}}).encode()
            plan = run_desk.build_plan(self.conn, "portrait")
            self.assertEqual(plan["model"], "opus")
            wands.record_resolution(self.conn, "opus", run_desk.parse_claude_model(allowed), now=NOW + 1)
            self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-opus-5-5")
            self.assertTrue(self.dry_run("hermione")["blocked"])
            self.assertTrue(self.dry_run("portrait")["blocked"])
            owl_id = self.request("hermione")
            with mock.patch.object(run_desk, "start_child", side_effect=AssertionError("a blocked model ran")), \
                    self.assertRaisesRegex(run_desk.Blocked, "opus, which once resolved to claude-quill-9,"):
                run_desk.run(self.conn, "hermione", owl_id, now=NOW + 2)
        self.assertEqual(run_desk.full_ids(["claude-quill-9[1m]", "opus", "claude-quill-9", "x y"]), ["claude-quill-9"])

    def test_a_labelled_alias_and_its_bare_form_share_one_history(self):
        self.enable("ron")
        self.enable("hermione")
        real_get_desk = pensieve.get_desk

        def registry(conn, name):
            row = real_get_desk(conn, name)
            return {**row, "model": "opus[1m]"} if row["name"] == "ron" else row

        quill = json.dumps({"type": "result", "modelUsage": {"claude-quill-9[1m]": {"outputTokens": 9}}}).encode()
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ("claude-quill-",)), \
                mock.patch.object(pensieve, "get_desk", side_effect=registry):
            self.assertEqual(self.dry_run("ron")["model"], "opus[1m]")
            self.assertEqual(self.real_run("ron", 0, quill), 0)
            self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-quill-9")
            self.assertTrue(self.dry_run("ron")["blocked"])
            self.assertTrue(self.dry_run("hermione")["blocked"])  # hermione runs plain opus
            with self.assertRaises(ConflictError):
                wands.pin(self.conn, "hermione", "opus", now=NOW + 1, blocked=config.BLOCKED_MODEL_PREFIXES)
            with self.assertRaises(ConflictError):
                wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW + 1,
                                  blocked=config.BLOCKED_MODEL_PREFIXES)
            plan = self.keeper()
        self.assertEqual(plan["catalog"]["claude"]["ran_as"], {"opus": "claude-quill-9"})
        self.assertIn("opus", plan["blocked"])

    # Moody round 2 B1: with anything blocked, the unchecked Codex CLI default never launches.

    def test_with_a_blocklist_a_codex_desk_on_the_cli_default_is_never_launched(self):
        self.enable("moody")
        owl_id = self.request("moody")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS), \
                mock.patch.object(run_desk, "start_child", side_effect=AssertionError("the CLI default ran")):
            plan = self.dry_run("moody")
            self.assertEqual((plan["model"], plan["blocked"]), ("codex-default", True))
            for _ in range(2):
                with self.assertRaisesRegex(run_desk.Blocked, "no model of its own, so it would run the Codex CLI"
                                                              " default, which cannot be checked"):
                    run_desk.run(self.conn, "moody", owl_id, now=NOW)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch("time.time", return_value=NOW):
                self.assertEqual(run_desk.main(["moody", "--owl", owl_id]), 1)
        [event] = self.events_of("rundesk.blocked")
        self.assertEqual(event["verdict"], "headmaster")
        self.assertIn("Run fleet ollivander to give it its role's pick, or castle desk model moody <model>",
                      event["summary"])
        self.assertEqual(self.events_of("rundesk.failed"), [])
        self.assertIsNone(wands.last_run_model(self.conn, "moody"))
        self.assertEqual([owl["id"] for owl in owlery.inbox(self.conn, "moody")], [owl_id])
        # With nothing blocked, the CLI default launches as it always has.
        self.assertFalse(self.dry_run("moody")["blocked"])
        self.assertEqual(self.real_run("moody", 0), 0)
        self.assertEqual(wands.last_run_model(self.conn, "moody"), "codex-default")

    def test_ollivanders_first_pass_gives_codex_desks_a_model_they_can_launch(self):
        self.enable("moody")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS):
            self.assertTrue(all(self.dry_run(desk)["blocked"] for desk in ("harry", "moody")))
            self.keeper()
            for desk, model in (("harry", "gpt-6.1-sol"), ("moody", "gpt-6-astra")):
                with self.subTest(desk=desk):
                    plan = self.dry_run(desk)
                    self.assertEqual((plan["model"], plan["blocked"]), (model, False))
            self.assertEqual(self.real_run("moody", 0), 0)
        self.assertEqual(wands.last_run_model(self.conn, "moody"), "gpt-6-astra")
        self.assertEqual(self.events_of("rundesk.blocked"), [])

    def test_a_failed_trial_never_reverts_onto_the_cli_default_while_anything_is_blocked(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS):
            self.assertEqual(self.real_run("moody", 1), 1)
            self.assertEqual(self.real_run("moody", 1), 1)
            row = self.model("moody")
            self.assertEqual((row["model"], row["pinned"], row["trial_end"]), ("gpt-6.1-sol", 0, "revert_blocked"))
            self.assertEqual(self.events_of("ollivander.reverted"), [])
            [event] = self.events_of("ollivander.revert-blocked")
            self.assertIn("it came from no model of its own, the CLI default, which cannot be checked", event["summary"])
            self.assertIn("so it stays on gpt-6.1-sol", event["summary"])
            self.assertFalse(self.dry_run("moody")["blocked"])
        # With nothing blocked, a trial from the CLI default reverts to it as before.
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        plan = {"desk": "harry", "run_id": "run-1", "model": "gpt-6.1-sol", "default_model": None,
                "change_id": wands.current_change(self.conn, "harry")}
        for _ in range(2):
            run_desk.after_run(self.conn, plan, None, "gpt-6.1-sol", 1, NOW)
        self.assertEqual((self.model("harry")["model"], self.model("harry")["pinned"]), (None, 1))
        self.assertEqual(self.dry_run("harry")["model"], "codex-default")

    def test_a_desk_a_revert_pinned_to_the_cli_default_is_told_how_to_launch_again(self):
        self.enable("moody")
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 1), 1)
        self.assertEqual(self.real_run("moody", 1), 1)
        self.assertEqual((self.model("moody")["model"], self.model("moody")["pinned"]), (None, 1))
        owl_id = self.request("moody")
        with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", MADE_UP_BLOCKS):
            with self.assertRaises(run_desk.Blocked):
                run_desk.run(self.conn, "moody", owl_id, now=NOW)
            [event] = self.events_of("rundesk.blocked")
            self.assertIn("It is pinned to no model, so Ollivander leaves it alone: castle desk model moody <model>"
                          " pins an allowed one, or castle desk model moody --role lets fleet ollivander give it"
                          " its role's pick", event["summary"])
            self.assertNotIn("Run fleet ollivander", event["summary"])
            # Ollivander's pass leaves the pinned desk alone, but says how to launch it again, once a day.
            for when in (OCT4, OCT4 + 60):
                self.assertEqual(self.desk(self.keeper(now=when), "moody")["action"], "pinned")
            [notice] = self.events_of("ollivander.pinned-default")
            self.assertIn("pinned to no model of its own", notice["summary"])
            self.assertIn("castle desk model moody --role hands it back to Ollivander", notice["summary"])
        # With nothing blocked the CLI default launches, so there is nothing to say.
        self.keeper(now=OCT4 + 86400)
        self.assertEqual(len(self.events_of("ollivander.pinned-default")), 1)

    def test_a_failed_trial_never_reverts_onto_a_model_the_latest_catalog_hides(self):
        """A revert pins. Onto a hidden model it would lock the desk where no pass would put it."""
        self.enable("harry")
        self.keeper()
        self.assertEqual(self.model("harry")["model"], "gpt-6.1-sol")
        newer = dict(QUILL, slug="gpt-6.2-sol", description="Latest workhorse model for coding.", priority=-1)

        def hide_and_add(models):
            models[0].update(visibility="hide")
            models.append(newer)

        later = FakeRunner(catalog(hide_and_add))
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 60, runner=later), "harry")["action"], "apply")
        self.assertEqual(self.model("harry")["model"], "gpt-6.2-sol")
        self.assertEqual(self.real_run("harry", 1), 1)
        self.assertEqual(self.real_run("harry", 1), 1)
        row = self.model("harry")
        self.assertEqual((row["model"], row["pinned"], row["trial_end"]), ("gpt-6.2-sol", 0, "revert_blocked"))
        self.assertEqual(self.events_of("ollivander.reverted"), [])
        [event] = self.events_of("ollivander.revert-blocked")
        self.assertIn("gpt-6.1-sol, the model it came from, no longer qualifies: the latest codex catalog hides it,"
                      " so it stays on gpt-6.2-sol", event["summary"])
        # Not pinned, so the next pass still looks after it.
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 120, runner=later), "harry")["action"], "keep")

    # Moody round 2 B2: pinning the model a trial is on ends the trial, so two failures never revert.

    def test_pinning_the_current_model_during_a_trial_survives_two_failures(self):
        self.enable("moody")
        wands.record_catalog(self.conn, "codex", ["gpt-6-astra", "gpt-6.1-sol"], now=NOW)
        wands.apply_model(self.conn, "moody", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(self.real_run("moody", 0), 0)
        wands.apply_model(self.conn, "moody", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        self.assertEqual(self.real_run("moody", 1), 1)
        code, out = self.castle_cli("desk", "model", "moody", "gpt-6.1-sol")
        self.assertEqual((code, out["data"]["trial_ended"], out["data"]["trial_end"]), (0, True, "pinned"))
        self.assertEqual(self.real_run("moody", 1), 1)
        self.assertEqual(self.real_run("moody", 1), 1)
        row = self.model("moody")
        self.assertEqual((row["model"], row["pinned"], row["trial_failures"], row["trial_end"]),
                         ("gpt-6.1-sol", 1, None, "pinned"))
        self.assertEqual(self.events_of("ollivander.reverted"), [])
        self.assertEqual(self.events_of("ollivander.trial-failed"), [])
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "moody")], ["initial", "role"])
        self.assertIn('model="gpt-6.1-sol"', self.overrides(self.dry_run("moody")["argv"]))


class BlockedModelTests(OllivanderCase):
    def blocks(self, prefixes=MADE_UP_BLOCKS):
        return mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", prefixes)

    @unittest.skipUnless(IN_KIT, ONLY_IN_KIT)
    def test_the_kit_blocks_nothing(self):
        self.assertEqual(kit_setting("BLOCKED_MODEL_PREFIXES"), ())

    def test_the_tests_block_nothing_unless_they_say_so(self):
        self.assertEqual(config.BLOCKED_MODEL_PREFIXES, ())

    def test_a_prefix_blocks_the_names_it_starts(self):
        self.assertEqual(wands.blocked_by("claude-fennel-5", ("fennel", "claude-fennel-")), "claude-fennel-")
        self.assertEqual(wands.blocked_by("fennel", ("fennel", "claude-fennel-")), "fennel")
        self.assertIsNone(wands.blocked_by("claude-opus-5-5", ("fennel", "claude-fennel-")))
        for bad in (("Fennel",), ("",), (" fennel",), "fennel", (7,)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                wands.blocked_by("fennel", bad)

    def test_a_blocked_alias_is_skipped_silently_and_never_picked(self):
        wands.classify(self.conn, "fennel", "frontier", now=OCT4)  # filed before the organisation blocked it
        with self.blocks():
            plan = self.keeper()
        self.assertEqual(plan["unclassified"], [])
        self.assertEqual(plan["blocked"], ["fennel"])
        self.assertEqual(self.events_of("ollivander.unclassified"), [])
        hermione = self.desk(plan, "hermione")
        self.assertEqual((hermione["pick"], hermione["skipped_blocked"]), ("opus", ["fennel"]))
        aliases = {item["alias"]: item["blocked"] for item in plan["catalog"]["claude"]["aliases"]}
        self.assertEqual(aliases, {"fennel": True, "haiku": False, "opus": False, "sonnet": False})
        self.assertNotIn("fennel", [self.model(desk)["model"] for desk in HEADLESS])

    def test_the_dry_run_lists_the_blocked_names_it_skipped(self):
        runner = FakeRunner(catalog(lambda models: models.append(dict(QUILL))))
        self.assertEqual(self.desk(self.keeper(dry_run=True, runner=runner), "moody")["pick"], "gpt-zz-quill")
        with self.blocks():
            plan = self.keeper(dry_run=True, runner=runner)
        self.assertEqual(plan["blocked"], ["fennel", "gpt-zz-quill"])
        moody = self.desk(plan, "moody")
        self.assertEqual((moody["pick"], moody["skipped_blocked"]), ("gpt-6-astra", ["gpt-zz-quill"]))
        quill = next(model for model in plan["catalog"]["codex"]["models"] if model["slug"] == "gpt-zz-quill")
        self.assertTrue(quill["blocked"])
        self.assertEqual(self.events(), [])

    def test_a_tier_with_only_blocked_models_keeps_the_current_one_with_one_fyi(self):
        lines = {"opus": "frontier", "sonnet": "workhorse", "wisp": "fast"}
        with mock.patch.object(config, "CLAUDE_LINES", lines):
            self.keeper()
            self.assertEqual(self.model("ron")["model"], "wisp")
            with self.blocks():
                for when in (OCT4 + 60, OCT4 + 86400):
                    plan = self.keeper(now=when)
                    self.assertEqual(self.desk(plan, "ron")["action"], "blocked")
        self.assertEqual(self.model("ron")["model"], "wisp")
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "ron")], ["initial"])
        [event] = self.events_of("ollivander.blocked")
        self.assertEqual((event["desk"], event["verdict"]), ("ron", "headmaster"))
        self.assertIn("every fast model for a claude desk is blocked here (wisp), so it keeps wisp", event["summary"])
        self.assertIn("castle desk model ron <model>", event["summary"])
        self.assertEqual(self.events_of("ollivander.no-pick"), [])

    def test_castle_refuses_to_pin_file_or_approve_a_blocked_model(self):
        self.keeper()
        with self.blocks():
            code, out = self.castle_cli("desk", "model", "hermione", "claude-quill-2")
            self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
            self.assertIn("claude-quill-2 is a blocked model here", out["error"]["message"])
            self.assertEqual(self.castle_cli("model", "line", "fennel", "frontier")[0], 3)
            self.assertEqual(self.castle_cli("model", "line", "fennel", "ignore")[0], 3)
            self.assertEqual(self.castle_cli("model", "line", "gpt-zz-quill", "fast")[0], 3)
            self.assertEqual(self.castle_cli("desk", "model", "hermione", "sonnet")[0], 0)
        self.assertEqual(wands.ryan_lines(self.conn), {})
        self.assertEqual(self.model("hermione")["model"], "sonnet")
        wands.set_pending(self.conn, "harry", "gpt-zz-quill", "high", "frontier", now=OCT4)
        with self.blocks():
            self.assertEqual(self.castle_cli("desk", "model", "harry", "--approve")[0], 3)
            with self.assertRaises(ConflictError):
                wands.apply_model(self.conn, "harry", "gpt-zz-quill", "high", "frontier", "role",
                                  blocked=config.BLOCKED_MODEL_PREFIXES)
        self.assertEqual(self.model("harry")["model"], "gpt-6.1-sol")


class PendingPickTests(OllivanderCase):
    """Moody round 2 B3: a pending pick lives only while the passes keep making it, and --approve checks it."""

    def wait(self, now: int) -> None:
        self.write_role("harry", need="frontier")
        self.assertEqual(self.desk(self.keeper(now=now), "harry")["action"], "pending")
        self.assertEqual(self.model("harry")["pending_model"], "gpt-6-astra")

    def test_the_catalog_kept_in_the_store_says_what_approval_needs(self):
        self.keeper()
        astra = wands.catalog_entry(self.conn, "codex", "gpt-6-astra")
        self.assertEqual((astra["visible"], astra["line"], astra["retires_at"]), (1, "frontier", None))
        self.assertEqual(wands.catalog_entry(self.conn, "codex", "gpt-reserve")["visible"], 0)
        self.assertEqual(wands.catalog_entry(self.conn, "codex", "gpt-5.5")["retires_at"],
                         calendar.timegm((2026, 10, 14, 19, 0, 0)))
        self.assertIsNone(wands.catalog_entry(self.conn, "codex", "gpt-6-sol")["line"])  # excluded words
        self.assertEqual(wands.catalog_entry(self.conn, "claude", "haiku")["line"], "fast")
        self.assertEqual(ollivander.retires_at({"upgrade": {"retirement_at": "soon"}}), 0)

    def test_a_pending_pick_a_later_pass_does_not_make_is_dropped_with_a_routine_note(self):
        self.keeper()
        hidden = FakeRunner(catalog(lambda models: models[1].update(visibility="hide")))
        retiring = FakeRunner(catalog(lambda models: models[1].update(
            upgrade={"model": "gpt-6.1-sol", "retirement_at": "2026-10-20T00:00:00Z"})))
        unread = FakeRunner(codes={("debug", "models"): 1})
        when = OCT4
        for name, runner, blocks, action, because in (
                ("hidden", hidden, (), "no-pick", "no model of its need is left to pick now"),
                ("retiring", retiring, (), "no-pick", "no model of its need is left to pick now"),
                ("blocked", None, ("gpt-6-astra",), "blocked", "every model of its need is blocked here"),
                ("no catalog", unread, (), "no-catalog", "the Codex catalog could not be read"),
                ("bad role", None, (), "bad-role", "its role card is not valid"),
                ("role back", None, (), "keep", "its role now picks the model it already runs")):
            with self.subTest(case=name):
                when += 600
                self.wait(when)
                if name == "bad role":
                    self.write_role("harry", need="cheap")
                elif name == "role back":
                    self.write_role("harry")
                with mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", blocks):
                    plan = self.keeper(now=when + 60, runner=runner)
                self.assertEqual(self.desk(plan, "harry")["action"], action)
                row = self.model("harry")
                self.assertEqual((row["model"], row["pending_model"]), ("gpt-6.1-sol", None))
                note = self.events_of("ollivander.pending-dropped")[-1]
                self.assertEqual((note["desk"], note["verdict"]), ("harry", "routine"))
                self.assertIn("the pick gpt-6-astra at high effort that waited for your approval is dropped, because "
                              + because, note["summary"])
                code, out = self.castle_cli("desk", "model", "harry", "--approve")
                self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
                self.assertIn("no pending pick", out["error"]["message"])
        self.assertEqual(len(self.events_of("ollivander.pending-dropped")), 6)
        self.assertEqual(len(self.events_of("ollivander.pending")), 6)

    def test_a_pending_pick_replaced_or_applied_over_is_noted_too(self):
        self.keeper()
        self.wait(OCT4 + 60)
        quill = FakeRunner(catalog(lambda models: models.append(dict(QUILL))))
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 120, runner=quill), "harry")["action"], "pending")
        self.assertEqual(self.model("harry")["pending_model"], "gpt-zz-quill")
        [note] = self.events_of("ollivander.pending-dropped")
        self.assertIn("because its role now picks gpt-zz-quill, which waits in its place", note["summary"])
        self.write_role("harry", need="fast")
        self.assertEqual(self.desk(self.keeper(now=OCT4 + 180, runner=quill), "harry")["action"], "apply")
        note = self.events_of("ollivander.pending-dropped")[-1]
        self.assertIn("gpt-zz-quill at high effort that waited for your approval is dropped, because Ollivander"
                      " moved the desk to gpt-6-luna instead", note["summary"])
        self.assertIsNone(self.model("harry")["pending_model"])

    def test_an_approval_during_a_pass_waits_for_the_pass_and_its_catalog_look(self):
        self.keeper()
        self.wait(OCT4 + 60)
        other = db.connect(self.db_path, create=False)
        self.addCleanup(other.close)
        other.execute("PRAGMA busy_timeout=50")
        tried = []
        real_plan = ollivander.make_plan

        def plan_while_ryan_approves(*args, **kwargs):
            try:
                tried.append(wands.approve(other, "harry", now=OCT4 + 100)["model"])
            except ConflictError as exc:
                tried.append(str(exc))
            return real_plan(*args, **kwargs)

        hidden = FakeRunner(catalog(lambda models: models[1].update(visibility="hide")))
        with mock.patch.object(ollivander, "make_plan", plan_while_ryan_approves):
            plan = self.keeper(now=OCT4 + 120, runner=hidden)
        self.assertEqual(tried, ["database is busy, try again"])
        self.assertEqual(self.desk(plan, "harry")["action"], "no-pick")
        row = self.model("harry")
        self.assertEqual((row["model"], row["pending_model"]), ("gpt-6.1-sol", None))
        [note] = self.events_of("ollivander.pending-dropped")
        self.assertIn("no model of its need is left to pick now", note["summary"])
        code, out = self.castle_cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
        self.assertIn("no pending pick", out["error"]["message"])

    def test_approve_checks_the_pick_against_the_latest_catalog_in_the_store(self):
        self.keeper()
        self.wait(OCT4 + 60)
        # A look that hides the pick lands, and no pass has dropped the pick yet.
        codex = ollivander.fetch_codex(FakeRunner(catalog(lambda models: models[1].update(visibility="hide"))))
        entries, _ = ollivander._catalog_entries(codex, {"aliases": []}, wands.ryan_lines(self.conn))
        wands.record_catalog(self.conn, "codex", entries, now=OCT4 + 90)
        code, out = self.castle_cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["error"]["type"]), (3, "ConflictError"))
        self.assertIn("the pending pick gpt-6-astra no longer qualifies: the latest codex catalog hides it",
                      out["error"]["message"])
        self.assertEqual(self.model("harry")["model"], "gpt-6.1-sol")
        # Listed again, but now retiring within the window.
        codex = ollivander.fetch_codex(FakeRunner(catalog(lambda models: models[1].update(
            upgrade={"model": "gpt-6.1-sol", "retirement_at": "2026-10-20T00:00:00Z"}))))
        entries, _ = ollivander._catalog_entries(codex, {"aliases": []}, wands.ryan_lines(self.conn))
        wands.record_catalog(self.conn, "codex", entries, now=OCT4 + 120)
        with mock.patch("time.time", return_value=OCT4 + 150):
            code, out = self.castle_cli("desk", "model", "harry", "--approve")
        self.assertEqual(code, 3)
        self.assertIn("it retires within 30 days", out["error"]["message"])
        # Back to the look the pick came from: it qualifies again and switches.
        self.keeper(now=OCT4 + 180)
        code, out = self.castle_cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["data"]["model"]), (0, "gpt-6-astra"))
