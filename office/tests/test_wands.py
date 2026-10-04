"""Ollivander's ledger in the store: model lines, catalogs, desk models, pins, approval and reverts."""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hogwarts import cli, db, pensieve, wands
from hogwarts.errors import ConflictError, ValidationError
from tests.support import NOW, StoreCase, temp_dir

HOSTILE = ("", "../x", "a b", "x;drop", "A" * 200, None, 7, "\x00", "Opus")
CLAUDE = {"opus": "frontier", "sonnet": "workhorse", "haiku": "fast"}
CODEX = {"gpt-6.1-sol": "workhorse", "gpt-6-astra": "frontier", "gpt-6-luna": "fast"}


def entries(lines: dict, **changes) -> list:
    """Catalog entries as Ollivander records them: listed, filed under their line, never retiring.
    changes maps a name to the fields that differ for it."""
    return [{"name": name, "visible": True, "line": line, "retires_at": None, **changes.get(name, {})}
            for name, line in lines.items()]


class WandsCase(StoreCase):
    def setUp(self):
        super().setUp()
        for name, family, model in (("hermione", "claude", "opus"), ("harry", "codex", None), ("ryan", "human", None)):
            pensieve.add_desk(self.conn, name, family, model=model, now=NOW)
        wands.record_catalog(self.conn, "claude", entries(CLAUDE), now=NOW)
        wands.record_catalog(self.conn, "codex", entries(CODEX), now=NOW)

    def change(self, desk: str = "harry"):
        return wands.current_change(self.conn, desk)

    def cli(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        raw = out.getvalue() + err.getvalue()
        return code, json.loads(raw), raw


class MigrationTests(StoreCase):
    def test_v5_adds_the_ledger_to_a_populated_v4_database(self):
        path = temp_dir(self) / "state" / "pensieve.db"
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:4]), mock.patch.object(db, "SCHEMA_VERSION", 4):
            conn = db.connect(path)
            pensieve.add_desk(conn, "alpha", "claude", model="opus", now=NOW)
            conn.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"model_lines", "model_catalog", "desk_models", "model_changes", "model_resolutions"},
                             names)
        self.assertEqual(pensieve.get_desk(conn, "alpha")["model"], "opus")
        self.assertEqual(wands.list_desk_models(conn)[0]["model"], "opus")


class LineTests(WandsCase):
    def test_the_latest_filing_of_a_name_wins(self):
        wands.classify(self.conn, "fennel", "fast", now=NOW)
        wands.classify(self.conn, "fennel", "frontier", now=NOW + 1)
        wands.classify(self.conn, "gpt-6-sol", "ignore", now=NOW + 2)
        lines = wands.ryan_lines(self.conn)
        self.assertEqual(lines["fennel"]["line"], "frontier")
        self.assertEqual(lines["gpt-6-sol"]["line"], "ignore")
        self.assertEqual(self.count("model_lines"), 3)

    def test_filings_are_never_changed_or_deleted(self):
        wands.classify(self.conn, "fennel", "fast", now=NOW)
        for statement in ("UPDATE model_lines SET line = 'frontier'", "DELETE FROM model_lines"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                self.conn.execute(statement)

    def test_bad_names_and_lines_are_refused(self):
        for bad in HOSTILE:
            with self.subTest(name=bad), self.assertRaises(ValidationError):
                wands.classify(self.conn, bad, "fast")
        with self.assertRaises(ValidationError):
            wands.classify(self.conn, "fennel", "cheap")
        self.assertEqual(self.count("model_lines"), 0)

    def test_the_name_rule(self):
        for good in ("opus", "gpt-6.1-sol", "claude-opus-5-5", "o3"):
            self.assertEqual(wands.check_name(good), good)
        for bad in ("-opus", "gpt_6", "GPT-6", "a", "x" * 64, "opus[1m]", "gpt 6", ".hidden"):
            with self.subTest(name=bad), self.assertRaises(ValidationError):
                wands.check_name(bad)


class CatalogTests(WandsCase):
    def test_the_last_catalog_is_the_latest_look(self):
        self.assertEqual(wands.last_catalog(self.conn, "codex"), ["gpt-6-astra", "gpt-6-luna", "gpt-6.1-sol"])
        wands.record_catalog(self.conn, "codex", ["gpt-6.1-sol"], now=NOW + 10)
        self.assertEqual(wands.last_catalog(self.conn, "codex"), ["gpt-6.1-sol"])
        self.assertEqual(wands.last_catalog(self.conn, "claude"), ["haiku", "opus", "sonnet"])

    def test_a_catalog_needs_safe_names(self):
        for names in ([], ["ok-name", "../x"], "opus", [None]):
            with self.subTest(names=names), self.assertRaises(ValidationError):
                wands.record_catalog(self.conn, "codex", names)
        with self.assertRaises(ValidationError):
            wands.record_catalog(self.conn, "human", ["opus"])


class PinTests(WandsCase):
    def test_a_claude_desk_pins_a_known_alias_or_a_full_id(self):
        self.assertEqual(wands.pin(self.conn, "hermione", "sonnet", now=NOW)["model"], "sonnet")
        row = wands.pin(self.conn, "hermione", "claude-opus-5-5", now=NOW + 1)
        self.assertEqual((row["model"], row["pinned"], row["previous_model"]), ("claude-opus-5-5", 1, "sonnet"))
        with self.assertRaises(ValidationError):
            wands.pin(self.conn, "hermione", "fennel")

    def test_a_desk_never_pins_the_other_family(self):
        with self.assertRaises(ValidationError):
            wands.pin(self.conn, "hermione", "gpt-6.1-sol")
        for value in ("opus", "claude-opus-5-5", "gpt-9"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                wands.pin(self.conn, "harry", value)
        with self.assertRaises(ConflictError):
            wands.pin(self.conn, "ryan", "opus")

    def test_pin_takes_a_filed_line_and_unpin_hands_the_desk_back(self):
        wands.classify(self.conn, "gpt-6-luna", "fast", now=NOW)
        row = wands.pin(self.conn, "harry", "gpt-6-luna", now=NOW)
        self.assertEqual((row["model"], row["line"], row["pinned"]), ("gpt-6-luna", "fast", 1))
        with self.assertRaises(ConflictError):
            wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role")
        self.assertEqual(wands.unpin(self.conn, "harry", now=NOW + 1)["pinned"], 0)
        self.assertEqual(wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role",
                                           now=NOW + 2)["model"], "gpt-6.1-sol")
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "harry")], ["pin", "role"])

    def test_a_pin_on_a_hidden_or_retiring_model_stands_but_warns(self):
        self.assertIsNone(wands.pin(self.conn, "harry", "gpt-6.1-sol", now=NOW)["warning"])
        wands.record_catalog(self.conn, "codex", entries(CODEX, **{
            "gpt-6-astra": {"visible": False}, "gpt-6-luna": {"retires_at": NOW + 86400}}), now=NOW + 1)
        for value, why in (("gpt-6-astra", "the latest codex catalog hides it"),
                           ("gpt-6-luna", "it retires within 30 days")):
            with self.subTest(value=value):
                row = wands.pin(self.conn, "harry", value, now=NOW + 2)
                self.assertEqual((row["model"], row["pinned"], row["warning"]),
                                 (value, 1, "pinned as asked, but " + why))
        self.assertIsNone(wands.pin(self.conn, "hermione", "claude-quill-2", now=NOW + 3)["warning"])
        with self.assertRaises(ValidationError):
            wands.pin(self.conn, "harry", "gpt-6.1-sol", retiring_within=-1)

    def test_pinning_the_current_model_records_no_switch(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        row = wands.pin(self.conn, "harry", "gpt-6.1-sol", now=NOW + 1)
        self.assertEqual((row["pinned"], row["change_id"]), (1, None))
        self.assertEqual(len(wands.changes(self.conn, "harry")), 1)


class BlockedTests(WandsCase):
    BLOCKED = ("quill", "claude-quill-", "gpt-zz-")  # made-up names only

    def test_a_blocked_name_is_never_filed_pinned_applied_or_approved(self):
        before = self.conn.total_changes
        for call in (lambda: wands.classify(self.conn, "quill", "frontier", now=NOW, blocked=self.BLOCKED),
                     lambda: wands.classify(self.conn, "gpt-zz-1", "ignore", now=NOW, blocked=self.BLOCKED),
                     lambda: wands.pin(self.conn, "hermione", "claude-quill-2", now=NOW, blocked=self.BLOCKED),
                     lambda: wands.apply_model(self.conn, "harry", "gpt-zz-1", "high", "frontier", "role", now=NOW,
                                               blocked=self.BLOCKED),
                     lambda: wands.set_pending(self.conn, "harry", "gpt-zz-1", "high", "frontier", now=NOW,
                                               blocked=self.BLOCKED)):
            with self.subTest(call=call), self.assertRaisesRegex(ConflictError, "blocked model"):
                call()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(wands.pin(self.conn, "hermione", "claude-opus-5-5", now=NOW, blocked=self.BLOCKED)["model"],
                         "claude-opus-5-5")
        wands.set_pending(self.conn, "harry", "gpt-6-astra", "high", "frontier", now=NOW)
        with self.assertRaises(ConflictError):
            wands.approve(self.conn, "harry", now=NOW, blocked=("gpt-6-a",))
        self.assertEqual(wands.approve(self.conn, "harry", now=NOW, blocked=self.BLOCKED)["model"], "gpt-6-astra")

    def test_prefixes_match_aliases_ids_and_slugs(self):
        self.assertEqual(wands.blocked_by("quill", self.BLOCKED), "quill")
        self.assertEqual(wands.blocked_by("claude-quill-2-1", self.BLOCKED), "claude-quill-")
        self.assertEqual(wands.blocked_by("gpt-zz-1", self.BLOCKED), "gpt-zz-")
        self.assertIsNone(wands.blocked_by("claude-opus-5-5", self.BLOCKED))
        self.assertIsNone(wands.blocked_by("quill", ()))
        for bad in (("Quill",), ("",), ("quill ",), "quill", ["x" * 65], (None,)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                wands.blocked_by("quill", bad)


class ResolutionTests(WandsCase):
    BLOCKED = ("quill", "claude-quill-")  # made-up names only

    def test_every_resolution_is_kept_and_the_latest_sighting_wins(self):
        self.assertIsNone(wands.resolved_id(self.conn, "opus"))
        wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW)
        wands.record_resolution(self.conn, "opus", "claude-opus-5-5", now=NOW)
        self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-opus-5-5")
        again = wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW + 5)
        self.assertEqual((again["first_seen"], again["last_seen"]), (NOW, NOW + 5))
        self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-quill-9")
        late = wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW + 1)  # a run that ended late
        self.assertEqual((late["first_seen"], late["last_seen"]), (NOW, NOW + 5))
        self.assertEqual([row["full_id"] for row in wands.resolutions(self.conn, "opus")],
                         ["claude-quill-9", "claude-opus-5-5"])
        wands.record_resolution(self.conn, "sonnet", "claude-sonnet-5", now=NOW)
        self.assertEqual(wands.blocked_resolutions(self.conn, self.BLOCKED), {"opus": "claude-quill-9"})
        self.assertEqual(wands.blocked_resolutions(self.conn, ()), {})
        self.assertEqual(wands.blocked_resolution(self.conn, "opus", self.BLOCKED), "claude-quill-9")
        self.assertIsNone(wands.blocked_resolution(self.conn, "sonnet", self.BLOCKED))
        self.assertIsNone(wands.blocked_resolution(self.conn, "opus", ()))

    def test_resolutions_are_never_rewritten_or_deleted(self):
        wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW)
        for statement in ("UPDATE model_resolutions SET full_id = 'claude-opus-5-5'",
                          "UPDATE model_resolutions SET alias = 'sonnet'",
                          "UPDATE model_resolutions SET first_seen = 1", "DELETE FROM model_resolutions"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                self.conn.execute(statement)
        before = self.conn.total_changes
        for alias, full in (("claude-opus-5-5", "claude-opus-5-5"), ("opus", "opus"), ("opus", "gpt-6-astra"),
                            ("a b", "claude-opus-5-5"), (None, "claude-opus-5-5"), ("opus", None)):
            with self.subTest(alias=alias, full=full), self.assertRaises(ValidationError):
                wands.record_resolution(self.conn, alias, full, now=NOW)
        self.assertEqual(self.conn.total_changes, before)

    def test_an_alias_that_ran_as_a_blocked_id_is_never_pinned_approved_or_applied(self):
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "initial", now=NOW)
        wands.set_pending(self.conn, "hermione", "opus", "high", "frontier", now=NOW)
        wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW)
        before = self.conn.total_changes
        for call in (lambda: wands.pin(self.conn, "hermione", "opus", now=NOW, blocked=self.BLOCKED),
                     lambda: wands.approve(self.conn, "hermione", now=NOW, blocked=self.BLOCKED),
                     lambda: wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW,
                                               blocked=self.BLOCKED),
                     lambda: wands.set_pending(self.conn, "hermione", "opus", "max", "frontier", now=NOW,
                                               blocked=self.BLOCKED)):
            with self.subTest(call=call), self.assertRaisesRegex(ConflictError, "opus once ran as claude-quill-9"):
                call()
        self.assertEqual(self.conn.total_changes, before)
        # A later sighting on an allowed id, such as a run that launched before the blocked one was known and
        # ended after it, never lifts it. Only a blocklist without the prefix does.
        wands.record_resolution(self.conn, "opus", "claude-opus-5-5", now=NOW + 1)
        self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-opus-5-5")
        self.assertEqual(wands.blocked_resolution(self.conn, "opus", self.BLOCKED), "claude-quill-9")
        self.assertEqual(wands.blocked_resolutions(self.conn, self.BLOCKED), {"opus": "claude-quill-9"})
        with self.assertRaisesRegex(ConflictError, "opus once ran as claude-quill-9"):
            wands.pin(self.conn, "hermione", "opus", now=NOW + 1, blocked=self.BLOCKED)
        self.assertEqual(wands.pin(self.conn, "hermione", "opus", now=NOW + 1, blocked=("quill",))["pinned"], 1)

    def test_an_alias_with_a_label_shares_the_bare_aliases_history(self):
        wands.record_resolution(self.conn, "opus[1m]", "claude-quill-9", now=NOW)
        self.assertEqual(wands.resolved_id(self.conn, "opus"), "claude-quill-9")
        self.assertEqual([row["alias"] for row in wands.resolutions(self.conn, "opus[1m]")], ["opus"])
        self.assertEqual(wands.blocked_resolution(self.conn, "opus", self.BLOCKED), "claude-quill-9")
        self.assertEqual(wands.blocked_resolution(self.conn, "opus[1m]", self.BLOCKED), "claude-quill-9")
        with self.assertRaisesRegex(ConflictError, "opus once ran as claude-quill-9"):
            wands.pin(self.conn, "hermione", "opus", now=NOW, blocked=self.BLOCKED)
        with self.assertRaises(ValidationError):
            wands.record_resolution(self.conn, "claude-opus-5-5[1m]", "claude-opus-5-5", now=NOW)

    def test_a_trial_never_reverts_onto_an_alias_that_ran_as_a_blocked_id(self):
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "initial", now=NOW)
        wands.record_resolution(self.conn, "opus", "claude-quill-9", now=NOW)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        wands.record_resolution(self.conn, "sonnet", "claude-sonnet-5", now=NOW + 2)
        change = self.change("hermione")
        wands.record_outcome(self.conn, "hermione", False, change, now=NOW + 2, blocked=self.BLOCKED)
        outcome = wands.record_outcome(self.conn, "hermione", False, change, now=NOW + 3, blocked=self.BLOCKED)
        self.assertEqual((outcome["reverted"], outcome["revert_blocked"], outcome["previous_model"], outcome["ran_as"]),
                         (False, True, "opus", "claude-quill-9"))
        self.assertEqual(wands.get_desk_model(self.conn, "hermione")["model"], "sonnet")
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "hermione")], ["initial", "role"])


class ChoiceTests(WandsCase):
    def test_the_model_and_its_switch_are_read_together(self):
        self.assertEqual(wands.desk_choice(self.conn, "harry"), {"model": None, "effort": None, "change_id": None})
        first = wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertEqual(wands.desk_choice(self.conn, "harry"),
                         {"model": "gpt-6-astra", "effort": "high", "change_id": first["change_id"]})
        # A switch from another connection that commits between the two reads is not seen by either of them.
        other = db.connect(self.db_path)
        self.addCleanup(other.close)
        real_change = wands.current_change
        landed = []

        def switch_between(conn, desk):
            if not landed:
                landed.append(wands.apply_model(other, "harry", "gpt-6.1-sol", "low", "workhorse", "role",
                                                now=NOW + 1)["change_id"])
            return real_change(conn, desk)

        with mock.patch.object(wands, "current_change", side_effect=switch_between):
            choice = wands.desk_choice(self.conn, "harry")
        self.assertEqual(len(landed), 1)
        self.assertEqual(choice, {"model": "gpt-6-astra", "effort": "high", "change_id": first["change_id"]})
        self.assertEqual(wands.desk_choice(self.conn, "harry"),
                         {"model": "gpt-6.1-sol", "effort": "low", "change_id": landed[0]})


class PendingTests(WandsCase):
    def test_a_pending_pick_waits_until_approved(self):
        wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "initial", now=NOW)
        self.assertTrue(wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW + 1))
        self.assertFalse(wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW + 2))
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["model"], "gpt-6-luna")
        row = wands.approve(self.conn, "harry", now=NOW + 3)
        self.assertEqual((row["model"], row["line"], row["pending_model"]), ("gpt-6.1-sol", "workhorse", None))
        self.assertEqual(wands.changes(self.conn, "harry")[-1]["reason"], "approved")
        with self.assertRaises(ConflictError):
            wands.approve(self.conn, "harry")

    def test_clear_pending(self):
        wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW)
        row = wands.clear_pending(self.conn, "harry", now=NOW)
        self.assertEqual((row["pending_model"], row["cleared"]),
                         (None, {"model": "gpt-6.1-sol", "effort": "high", "line": "workhorse"}))
        self.assertIsNone(wands.clear_pending(self.conn, "harry", now=NOW)["cleared"])
        self.assertIsNone(wands.clear_pending(self.conn, "hermione", now=NOW)["cleared"])

    # Moody round 2 B3: approval checks the pick against the latest stored catalog.

    def wait_for(self, model: str = "gpt-6-astra", line: str = "frontier") -> None:
        wands.set_need(self.conn, "harry", line, now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "initial", now=NOW)
        self.assertTrue(wands.set_pending(self.conn, "harry", model, "high", line, now=NOW + 1))

    def test_approval_refuses_a_pick_the_latest_catalog_no_longer_offers_to_the_tier(self):
        month = 30 * 86400
        cases = {
            "gone": (lambda: wands.record_catalog(self.conn, "codex", entries({"gpt-6.1-sol": "workhorse"}),
                                                  now=NOW + 5), "latest codex catalog no longer offers it"),
            "hidden": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"visible": False}}), now=NOW + 5), "latest codex catalog hides it"),
            "retiring": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"retires_at": NOW + month - 60}}), now=NOW + 5), "retires within 30 days"),
            "no date": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"retires_at": 0}}), now=NOW + 5), "retires within 30 days"),
            "unfiled": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"line": None}}), now=NOW + 5), "now filed under no line, not frontier"),
            "ryan filed it": (lambda: wands.classify(self.conn, "gpt-6-astra", "ignore", now=NOW + 5),
                              "now filed under no line, not frontier"),
            "ryan moved it": (lambda: wands.classify(self.conn, "gpt-6-astra", "workhorse", now=NOW + 5),
                              "now filed under workhorse, not frontier"),
            "tier moved": (lambda: wands.set_need(self.conn, "harry", "workhorse", now=NOW + 5),
                           "tier is now workhorse, not frontier"),
        }
        for name, (change, message) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.wait_for()
                change()
                with self.assertRaisesRegex(ConflictError, "pending pick gpt-6-astra no longer qualifies: .*" + message):
                    wands.approve(self.conn, "harry", now=NOW + 10)
                row = wands.get_desk_model(self.conn, "harry")
                self.assertEqual((row["model"], row["pending_model"]), ("gpt-6-luna", "gpt-6-astra"))
                self.assertEqual(len(wands.changes(self.conn, "harry")), 1)

    def test_approval_refuses_a_pick_from_the_other_family(self):
        self.wait_for("opus")
        with self.assertRaisesRegex(ConflictError, "latest codex catalog no longer offers it"):
            wands.approve(self.conn, "harry", now=NOW + 10)

    def test_approval_takes_a_pick_that_still_qualifies(self):
        self.wait_for()
        far = NOW + 31 * 86400
        wands.record_catalog(self.conn, "codex", entries(CODEX, **{"gpt-6-astra": {"retires_at": far}}), now=NOW + 5)
        self.assertEqual(wands.approve(self.conn, "harry", now=NOW + 10)["model"], "gpt-6-astra")
        # Ryan's filing outranks the line the catalog look kept.
        self.setUp()
        self.wait_for()
        wands.record_catalog(self.conn, "codex", entries(CODEX, **{"gpt-6-astra": {"line": None}}), now=NOW + 5)
        wands.classify(self.conn, "gpt-6-astra", "frontier", now=NOW + 6)
        self.assertEqual(wands.approve(self.conn, "harry", now=NOW + 10)["model"], "gpt-6-astra")
        with self.assertRaises(ValidationError):
            wands.approve(self.conn, "harry", retiring_within=-1)

    def test_a_catalog_entry_must_be_well_formed(self):
        good = {"name": "gpt-6-astra", "visible": True, "line": "frontier", "retires_at": None}
        for bad in ({**good, "visible": 1}, {**good, "line": "cheap"}, {**good, "retires_at": -1},
                    {**good, "retires_at": 1.5}, {**good, "extra": 1}, {"name": "gpt-6-astra"}, {**good, "name": "../x"}):
            with self.subTest(entry=bad), self.assertRaises(ValidationError):
                wands.record_catalog(self.conn, "codex", [bad], now=NOW + 1)
        self.assertEqual(wands.catalog_entry(self.conn, "codex", "gpt-6-astra")["line"], "frontier")
        self.assertIsNone(wands.catalog_entry(self.conn, "claude", "gpt-6-astra"))


class TrialTests(WandsCase):
    def test_two_failures_after_a_switch_revert_and_pin(self):
        wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "medium", "workhorse", "role", now=NOW + 1)
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2), {"reverted": False})
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3)
        self.assertEqual((outcome["reverted"], outcome["from_model"], outcome["to_model"]),
                         (True, "gpt-6.1-sol", "gpt-6-astra"))
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["effort"], row["line"], row["pinned"], row["trial_failures"]),
                         ("gpt-6-astra", "high", "frontier", 1, None))
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 4), {"reverted": False})

    def test_a_success_ends_the_trial(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 1)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW + 2)
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3), {"reverted": False})
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 4), {"reverted": False})
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["model"], "gpt-6.1-sol")

    def test_reverting_an_initial_pick_goes_back_to_the_install_default(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 1)
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2)["to_model"], None)
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["pinned"]), (None, 1))

    def test_a_trial_never_reverts_onto_a_blocked_model(self):
        wands.apply_model(self.conn, "harry", "gpt-zz-quill", "high", "frontier", "initial", now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        blocked = ("gpt-zz-",)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2, blocked=blocked)
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3, blocked=blocked)
        self.assertEqual((outcome["reverted"], outcome["revert_blocked"], outcome["model"], outcome["previous_model"]),
                         (False, True, "gpt-6.1-sol", "gpt-zz-quill"))
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["pinned"], row["trial_failures"]), ("gpt-6.1-sol", 0, None))
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "harry")], ["initial", "role"])

    def test_a_trial_never_reverts_onto_a_blocked_install_default(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 1, blocked=("gpt-zz-",),
                             default_model="gpt-zz-quill")
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2, blocked=("gpt-zz-",),
                                       default_model="gpt-zz-quill")
        self.assertEqual((outcome["reverted"], outcome.get("revert_blocked")), (False, True))
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["model"], "gpt-6.1-sol")

    def test_blocked_by_matches_any_model_label_and_raises_only_on_a_bad_blocklist(self):
        self.assertIsNone(wands.blocked_by("claude-quill-9-9[1m]", ()))
        self.assertIsNone(wands.blocked_by("opus[1m]", ("quill",)))
        self.assertEqual(wands.blocked_by("Claude-Quill-9[1m]", ("claude-quill-",)), "claude-quill-")
        with self.assertRaises(ValidationError):
            wands.blocked_by("opus[1m]", ("Quill",))

    def test_a_run_planned_before_the_latest_switch_never_counts(self):
        wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        planned = self.change()
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        # A run planned on gpt-6-astra ends after the switch: it neither ends nor starts the new trial.
        self.assertEqual(wands.record_outcome(self.conn, "harry", True, planned, now=NOW + 2), {"reverted": False})
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["trial_failures"], 0)
        wands.record_outcome(self.conn, "harry", False, planned, now=NOW + 3)
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["trial_failures"], 0)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 4)
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 5)
        self.assertEqual((outcome["reverted"], outcome["to_model"]), (True, "gpt-6-astra"))

    def test_two_failures_after_ryans_own_switch_end_the_trial_but_keep_his_choice(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        wands.set_pending(self.conn, "harry", "gpt-6-astra", "high", "frontier", now=NOW + 1)
        for choose in (lambda: wands.approve(self.conn, "harry", now=NOW + 2),
                       lambda: wands.pin(self.conn, "harry", "gpt-6-luna", now=NOW + 3)):
            with self.subTest(choose=choose):
                chosen = choose()["model"]
                pinned = wands.get_desk_model(self.conn, "harry")["pinned"]
                self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 4),
                                 {"reverted": False})
                outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 5)
                self.assertEqual((outcome["reverted"], outcome["held"], outcome["model"]), (False, True, chosen))
                row = wands.get_desk_model(self.conn, "harry")
                self.assertEqual((row["model"], row["pinned"], row["trial_failures"]), (chosen, pinned, None))
        self.assertNotIn("revert", [change["reason"] for change in wands.changes(self.conn, "harry")])

    # Moody round 2 B1: with anything blocked, a trial never reverts onto the unchecked CLI default.

    def test_a_trial_never_reverts_onto_no_model_while_anything_is_blocked(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 1, blocked=("gpt-zz-",))
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2, blocked=("gpt-zz-",))
        self.assertEqual((outcome["reverted"], outcome["revert_blocked"], outcome["unchecked"],
                          outcome["previous_model"]), (False, True, True, None))
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["pinned"], row["trial_failures"], row["trial_end"]),
                         ("gpt-6.1-sol", 0, None, "revert_blocked"))
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "harry")], ["initial"])

    def test_a_trial_never_reverts_onto_a_model_no_pass_would_give_the_desk(self):
        """A revert pins, so it lands only on a model Ryan has not filed as ignore and the latest catalog
        still lists, shows and keeps. Otherwise the desk stays, unpinned, and Ollivander moves it on."""
        month = 30 * 86400
        cases = {
            "gone": (lambda: wands.record_catalog(self.conn, "codex", entries({"gpt-6.1-sol": "workhorse"}),
                                                  now=NOW + 2), "the latest codex catalog no longer offers it"),
            "hidden": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"visible": False}}), now=NOW + 2), "the latest codex catalog hides it"),
            "retiring": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"retires_at": NOW + month - 60}}), now=NOW + 2), "it retires within 30 days"),
            "no date": (lambda: wands.record_catalog(self.conn, "codex", entries(
                CODEX, **{"gpt-6-astra": {"retires_at": 0}}), now=NOW + 2), "it retires within 30 days"),
            "ignored": (lambda: wands.classify(self.conn, "gpt-6-astra", "ignore", now=NOW + 2),
                        "it is filed as ignore"),
        }
        for name, (change, why) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
                wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
                change()
                wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3, blocked=())
                outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 4, blocked=())
                self.assertEqual((outcome["reverted"], outcome["revert_blocked"], outcome["previous_model"],
                                  outcome["unavailable"]), (False, True, "gpt-6-astra", why))
                row = wands.get_desk_model(self.conn, "harry")
                self.assertEqual((row["model"], row["pinned"], row["trial_failures"], row["trial_end"]),
                                 ("gpt-6.1-sol", 0, None, "revert_blocked"))
                self.assertEqual([item["reason"] for item in wands.changes(self.conn, "harry")], ["initial", "role"])

    def test_a_claude_trial_reverts_only_onto_a_listed_alias_or_a_full_id(self):
        wands.apply_model(self.conn, "hermione", "haiku", "high", "fast", "initial", now=NOW)
        wands.apply_model(self.conn, "hermione", "sonnet", "high", "workhorse", "role", now=NOW + 1)
        wands.classify(self.conn, "haiku", "ignore", now=NOW + 2)
        for when in (NOW + 3, NOW + 4):
            outcome = wands.record_outcome(self.conn, "hermione", False, self.change("hermione"), now=when)
        self.assertEqual((outcome["revert_blocked"], outcome["unavailable"]), (True, "it is filed as ignore"))
        # A full id is in no catalog: only the blocklist judges it.
        wands.apply_model(self.conn, "hermione", "claude-quill-2", "high", None, "role", now=NOW + 5)
        wands.apply_model(self.conn, "hermione", "opus", "high", "frontier", "role", now=NOW + 6)
        for when in (NOW + 7, NOW + 8):
            outcome = wands.record_outcome(self.conn, "hermione", False, self.change("hermione"), now=when)
        self.assertEqual((outcome["reverted"], outcome["to_model"]), (True, "claude-quill-2"))
        with self.assertRaises(ValidationError):
            wands.record_outcome(self.conn, "hermione", False, None, retiring_within=-1)

    # Moody round 2 B2: Ryan's pin during an automatic trial ends it, and his pin always stands.

    def test_pinning_the_current_model_during_a_trial_ends_it_and_no_revert_follows(self):
        wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        planned = self.change()
        wands.record_outcome(self.conn, "harry", False, planned, now=NOW + 2)
        row = wands.pin(self.conn, "harry", "gpt-6.1-sol", now=NOW + 3)
        self.assertEqual((row["change_id"], row["trial_ended"], row["trial_failures"], row["trial_end"]),
                         (None, True, None, "pinned"))
        for when in (NOW + 4, NOW + 5):
            self.assertEqual(wands.record_outcome(self.conn, "harry", False, planned, now=when), {"reverted": False})
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["pinned"], row["trial_failures"], row["trial_end"]),
                         ("gpt-6.1-sol", 1, None, "pinned"))
        self.assertEqual([change["reason"] for change in wands.changes(self.conn, "harry")], ["initial", "role"])
        # Pinning again with no trial running leaves how the last one ended alone.
        self.assertFalse(wands.pin(self.conn, "harry", "gpt-6.1-sol", now=NOW + 6)["trial_ended"])
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["trial_end"], "pinned")

    def test_a_desk_pinned_after_its_trial_began_is_never_reverted(self):
        wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        # A pin that left the trial running (an older office, a write that raced it) still holds.
        self.conn.execute("UPDATE desk_models SET pinned = 1 WHERE desk = 'harry'")
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2)
        outcome = wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3)
        self.assertEqual((outcome["reverted"], outcome["held"], outcome["model"]), (False, True, "gpt-6.1-sol"))
        row = wands.get_desk_model(self.conn, "harry")
        self.assertEqual((row["model"], row["pinned"], row["trial_end"]), ("gpt-6.1-sol", 1, "held"))

    def test_each_trial_records_how_it_ended(self):
        wands.apply_model(self.conn, "harry", "gpt-6-astra", "high", "frontier", "initial", now=NOW)
        self.assertIsNone(wands.get_desk_model(self.conn, "harry")["trial_end"])
        wands.record_outcome(self.conn, "harry", True, self.change(), now=NOW)
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["trial_end"], "passed")
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW + 1)
        self.assertIsNone(wands.get_desk_model(self.conn, "harry")["trial_end"])
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 2)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW + 3)
        self.assertEqual(wands.get_desk_model(self.conn, "harry")["trial_end"], "reverted")

    def test_no_switch_no_trial(self):
        self.assertEqual(wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW), {"reverted": False})
        with self.assertRaises(ValidationError):
            wands.record_outcome(self.conn, "harry", 0, None)

    def test_the_last_run_model_comes_from_the_metrics(self):
        self.assertIsNone(wands.last_run_model(self.conn, "harry"))
        pensieve.add_metric(self.conn, "harry", "run-1", "codex-default", 1, 1, 0, 0.0, 1, ts=NOW)
        pensieve.add_metric(self.conn, "harry", "run-2", "gpt-6.1-sol", 1, 1, 0, 0.0, 1, ts=NOW)
        self.assertEqual(wands.last_run_model(self.conn, "harry"), "gpt-6.1-sol")
        pensieve.add_metric(self.conn, "hermione", "run-3", "claude-opus-5-5", 1, 1, 0, 0.0, 1, ts=NOW)
        pensieve.add_metric(self.conn, "hermione", "run-4", "opus", 1, 1, 0, 0.0, 1, ts=NOW)
        self.assertEqual(wands.last_run_model(self.conn, "hermione"), "opus")
        self.assertEqual(wands.last_run_model(self.conn, "hermione", claude_ids_only=True), "claude-opus-5-5")
        self.assertIsNone(wands.last_run_model(self.conn, "harry", claude_ids_only=True))

    def test_the_last_event_key_of_a_kind(self):
        self.assertIsNone(wands.last_event_key(self.conn, "harry", "ollivander.pending"))
        for key in ("ollivander:pending:harry:a:1", "ollivander:pending:harry:b:2"):
            pensieve.add_event(self.conn, "harry", "ollivander.pending", "headmaster", "x", dedupe_key=key, now=NOW)
        pensieve.add_event(self.conn, "harry", "ollivander.applied", "headmaster", "x", dedupe_key="other", now=NOW)
        self.assertEqual(wands.last_event_key(self.conn, "harry", "ollivander.pending"), "ollivander:pending:harry:b:2")


class SafetyTests(WandsCase):
    def test_hostile_values_are_refused_without_a_write(self):
        before = self.conn.total_changes
        calls = [
            lambda bad: wands.pin(self.conn, bad, "opus"),
            lambda bad: wands.pin(self.conn, "hermione", bad),
            lambda bad: wands.unpin(self.conn, bad),
            lambda bad: wands.approve(self.conn, bad),
            lambda bad: wands.set_need(self.conn, bad, "fast"),
            lambda bad: wands.set_need(self.conn, "harry", bad),
            lambda bad: wands.apply_model(self.conn, "harry", bad, "high", "fast", "role"),
            lambda bad: wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", bad),
            lambda bad: wands.set_pending(self.conn, "harry", bad, "high", "fast"),
            lambda bad: wands.set_pending(self.conn, "harry", "gpt-6-luna", "high", bad),
            lambda bad: wands.get_desk_model(self.conn, bad),
            lambda bad: wands.last_run_model(self.conn, bad),
            lambda bad: wands.record_outcome(self.conn, bad, True, None),
            lambda bad: wands.last_event_key(self.conn, bad, "ollivander.pending"),
        ]
        for index, call in enumerate(calls):
            for bad in HOSTILE:
                with self.subTest(call=index, value=bad), self.assertRaises(ValidationError):
                    call(bad)
        for call in (lambda: wands.apply_model(self.conn, "harry", "gpt-6-luna", "extreme", "fast", "role"),
                     lambda: wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "revert"),
                     lambda: wands.record_agent_file(self.conn, "hermione", "fast", "../x", "high"),
                     lambda: wands.record_agent_file(self.conn, "hermione", "cheap", None, None),
                     lambda: wands.record_outcome(self.conn, "harry", True, "1"),
                     lambda: wands.record_outcome(self.conn, "harry", True, 0),
                     lambda: wands.record_outcome(self.conn, "harry", True, True)):
            with self.subTest(call=call), self.assertRaises(ValidationError):
                call()
        self.assertEqual(self.conn.total_changes, before)

    def test_switches_are_never_changed_or_deleted_and_desk_models_never_deleted(self):
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "initial", now=NOW)
        for statement in ("UPDATE model_changes SET to_model = 'x'", "DELETE FROM model_changes",
                          "DELETE FROM desk_models"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                self.conn.execute(statement)

    def test_every_write_runs_inside_a_transaction(self):
        outside = []

        def trace(statement):
            verb = (statement.split(None, 1) or [""])[0].upper()
            if verb in ("INSERT", "UPDATE", "DELETE", "REPLACE") and not self.conn.in_transaction:
                outside.append(statement)

        self.conn.set_trace_callback(trace)
        wands.classify(self.conn, "fennel", "fast", now=NOW)
        wands.record_catalog(self.conn, "codex", entries(CODEX), now=NOW)
        wands.set_need(self.conn, "harry", "workhorse", now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "initial", now=NOW)
        wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW)
        wands.clear_pending(self.conn, "harry", now=NOW)
        wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW)
        wands.approve(self.conn, "harry", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW)
        wands.unpin(self.conn, "harry", now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", "role", now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW)
        wands.record_outcome(self.conn, "harry", False, self.change(), now=NOW)
        wands.pin(self.conn, "hermione", "sonnet", now=NOW)
        wands.record_agent_file(self.conn, "hermione", "frontier", "opus", "high", now=NOW)
        wands.record_resolution(self.conn, "opus", "claude-opus-5-5", now=NOW)
        wands.record_resolution(self.conn, "opus", "claude-opus-5-5", now=NOW + 1)
        self.conn.set_trace_callback(None)
        self.assertEqual(outside, [])


class CliTests(WandsCase):
    def test_desk_model_pins_unpins_and_approves(self):
        code, out, raw = self.cli("desk", "model", "harry", "gpt-6-astra")
        self.assertEqual((code, out["data"]["model"], out["data"]["pinned"]), (0, "gpt-6-astra", 1))
        self.assertTrue(raw.isascii())
        code, out, _ = self.cli("desk", "model", "harry", "--role")
        self.assertEqual((code, out["data"]["pinned"]), (0, 0))
        code, out, _ = self.cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["ok"]), (ConflictError.exit_code, False))
        wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW)
        code, out, _ = self.cli("desk", "model", "harry", "--approve")
        self.assertEqual((code, out["data"]["model"]), (0, "gpt-6.1-sol"))

    def test_desk_model_needs_exactly_one_choice(self):
        for argv in (("desk", "model", "harry"), ("desk", "model", "harry", "gpt-6-astra", "--role"),
                     ("desk", "model", "harry", "--role", "--approve")):
            with self.subTest(argv=argv):
                code, out, _ = self.cli(*argv)
                self.assertEqual((code, out["ok"]), (ValidationError.exit_code, False))

    def test_desk_models_lists_need_effort_model_pin_and_pending(self):
        wands.set_need(self.conn, "harry", "workhorse", now=NOW)
        wands.apply_model(self.conn, "harry", "gpt-6-luna", "high", "fast", "initial", now=NOW)
        wands.set_pending(self.conn, "harry", "gpt-6.1-sol", "high", "workhorse", now=NOW)
        code, out, _ = self.cli("desk", "models")
        self.assertEqual(code, 0)
        rows = {row["desk"]: row for row in out["data"]}
        self.assertEqual(sorted(rows), ["harry", "hermione"])
        self.assertEqual(rows["harry"], {"desk": "harry", "family": "codex", "need": "workhorse", "effort": "high",
                                         "model": "gpt-6-luna", "pinned": False,
                                         "pending_pick": {"model": "gpt-6.1-sol", "effort": "high"}})
        self.assertEqual(rows["hermione"]["model"], "opus")

    def test_model_line_files_a_name(self):
        code, out, _ = self.cli("model", "line", "fennel", "frontier")
        self.assertEqual((code, out["data"]["line"]), (0, "frontier"))
        code, out, _ = self.cli("model", "line", "fennel", "cheap")
        self.assertEqual(code, ValidationError.exit_code)

    def test_ollivander_clear_removes_only_the_stop_file(self):
        stop = self.db_path.parent / wands.STOP_FILE
        code, out, _ = self.cli("ollivander", "clear")
        self.assertEqual((code, out["data"]["cleared"]), (0, False))
        stop.write_text("1 stopped\n")
        code, out, _ = self.cli("ollivander", "clear")
        self.assertEqual((code, out["data"]["cleared"]), (0, True))
        self.assertFalse(stop.exists())
        target = self.tmp / "keep.txt"
        target.write_text("keep\n")
        os.symlink(target, stop)
        self.cli("ollivander", "clear")
        self.assertFalse(os.path.lexists(stop))
        self.assertTrue(target.exists())
        updating = self.db_path.parent / wands.UPDATING_FILE
        updating.write_text("1 a CLI update is running\n")
        code, out, _ = self.cli("ollivander", "clear")
        self.assertEqual((code, out["data"]["cleared"]), (0, True))
        self.assertFalse(updating.exists())
