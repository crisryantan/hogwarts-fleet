"""Dumbledore's nightly review: the export and its run, and the patch checks behind castle portrait.

Every test runs in the temp office and castle from FleetCase. No desk process starts unless a test fakes one.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
from unittest import mock

from hogwarts import cli, facts, owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from tests.support import DAY, NOW

from fleet import config, portrait, portrait_patch, safefs
from fleet.safefs import FleetError
from tests_fleet.support import FleetCase, fake_children

DATE = "2027-01-15"  # NOW is 2027-01-15T08:00:00Z, and the tests' clock is UTC
DAY_START = NOW - NOW % DAY
FORMAT = portrait_patch.FORMAT
CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.7,
             "modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5}}}


def op(op_id: str, kind: str, **fields) -> dict:
    """A patch op with a reason and a source, as Dumbledore writes one."""
    return {"id": op_id, "type": kind, "reason": "the day showed it", "source": "extract 1", **fields}


class PortraitCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("Popen", "run"):
            patcher = mock.patch.object(subprocess, name, side_effect=AssertionError("no process may start"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def patch_path(self, date: str = DATE):
        return self.outbox("portrait") / f"patch-{date}.ops"

    def write_raw(self, raw: bytes, date: str = DATE) -> str:
        self.write_file(self.patch_path(date), raw)
        return hashlib.sha256(raw).hexdigest()

    def write_patch(self, ops: list, date: str = DATE) -> str:
        return self.write_raw(json.dumps({"format": FORMAT, "date": date, "ops": ops}).encode("utf-8"), date)

    def fact(self, text: str, key: str = None, scope: str = "fleet", now: int = NOW - 1000) -> int:
        return pensieve.add_fact(self.conn, scope, text, "aging", "ryan", subject_key=key, now=now)["id"]

    def counts(self) -> tuple:
        return tuple(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("facts", "keypoints", "events"))

    def fact_row(self, fact_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone())


# The export and the nightly run


class ExportTests(PortraitCase):
    def setUp(self) -> None:
        super().setUp()
        pensieve.record_session(self.conn, "session-0001", "web-app", desk="mcgonagall", started_at=NOW - DAY)

    def extract(self, text: str, now: int) -> int:
        return pensieve.add_extract(self.conn, "session-0001", "user", text, now=now)["id"]

    def export_file(self, date: str = DATE) -> dict:
        raw = (self.inbox("portrait") / f"export-{date}.json").read_bytes()
        raw.decode("ascii")
        return json.loads(raw)

    def test_the_export_holds_the_days_extracts_candidates_and_facts(self):
        self.extract("yesterday's words", DAY_START - 10)
        long_line = "deploys need a second approval " * 30
        today = self.extract("first line\n" + long_line, NOW - 60)
        old = self.fact("web-app deploys need one approval", now=DAY_START - DAY)
        new = self.fact("web-app deploys need two approvals", key="web-app.approvals", now=NOW - 30)
        result = portrait.export_day(self.conn, now=NOW)
        self.assertEqual((result["date"], result["reviewed"], result["extracts"]), (DATE, False, 1))
        export = self.export_file()
        self.assertEqual((export["format"], export["date"]), ("pensieve-export-1", DATE))
        self.assertEqual((export["window"]["from"], export["window"]["to"]), (DAY_START, NOW))
        [item] = export["extracts"]
        self.assertEqual((item["id"], item["desk"], item["project"], item["role"]), (today, "mcgonagall", "web-app", "user"))
        self.assertEqual(item["lines"][0], "first line")
        self.assertTrue(all(len(line) <= portrait.LINE_CHUNK for line in item["lines"]))
        self.assertEqual("".join(item["lines"][1:]), long_line)
        self.assertEqual([(pair["fact_id"], pair["candidate_id"]) for pair in export["fact_candidates"]], [(new, old)])
        self.assertEqual([fact["id"] for fact in export["current_facts"]], [old, new])
        self.assertTrue(export["patch_file"].endswith(f"/desks/portrait/outbox/patch-{DATE}.ops"))
        self.assertTrue(export["morning_note_file"].endswith(f"/desks/portrait/outbox/morning-{DATE}.md"))
        self.assertIn("instruction", export["note"])

    def test_store_text_is_scrubbed_before_it_reaches_the_inbox(self):
        # The store keeps fact text as written, so the export scrubs every string it takes from the store.
        self.extract("ping someone@example.com about 10.1.2.3", NOW - 60)
        old = self.fact("mail someone@example.com before deploys", key="host:10.1.2.3", now=DAY_START - DAY)
        new = self.fact("mail someone@example.com after deploys", now=NOW - 30)
        portrait.export_day(self.conn, now=NOW)
        export = self.export_file()
        text = json.dumps(export)
        for value in ("someone@example.com", "10.1.2.3"):
            with self.subTest(value=value):
                self.assertNotIn(value, text)
        self.assertEqual([(pair["fact_id"], pair["candidate_id"]) for pair in export["fact_candidates"]], [(new, old)])
        self.assertIn("[email]", export["fact_candidates"][0]["candidate_text"])
        self.assertIn("[email]", export["extracts"][0]["lines"][0])
        self.assertEqual(export["fact_candidates_left_out"], 0)

    def test_candidates_past_the_total_budget_are_counted(self):
        pairs = [{"scope": "fleet", "fact_id": n, "fact_text": "a", "candidate_id": n + 1, "candidate_text": "b",
                  "score": -1.0} for n in range(portrait.CANDIDATES_TOTAL + 5)]
        with mock.patch.object(portrait.facts, "contradiction_candidates", return_value=pairs):
            export = portrait.build_export(self.conn, DATE, DAY_START, NOW)
        self.assertEqual((len(export["fact_candidates"]), export["fact_candidates_left_out"]),
                         (portrait.CANDIDATES_TOTAL, 5))

    def test_the_owl_comes_from_the_owl_post_with_its_inbox_copy(self):
        owl_id = portrait.export_day(self.conn, now=NOW)["owl_id"]
        [owl] = owlery.inbox(self.conn, "portrait")
        self.assertEqual((owl["id"], owl["sender"], owl["kind"], owl["subject"]),
                         (owl_id, "owl-post", "fyi", f"Pensieve export {DATE}"))
        self.assertIsNotNone(owl["delivered_at"])
        copy = json.loads((self.inbox("portrait") / f"{owl_id}.json").read_text())
        self.assertEqual((copy["owl_id"], copy["from"], copy["to"]), (owl_id, "owl-post", "portrait"))
        self.assertIn(f"/desks/portrait/inbox/export-{DATE}.json", copy["body"])
        self.assertIn(f"/desks/portrait/outbox/patch-{DATE}.ops", copy["body"])

    def test_a_rerun_the_same_day_reuses_the_owl_and_rewrites_the_export(self):
        first = portrait.export_day(self.conn, now=NOW)
        self.extract("a later thought", NOW + 30)
        again = portrait.export_day(self.conn, now=NOW + 60)
        self.assertEqual(again["owl_id"], first["owl_id"])
        self.assertEqual(len(owlery.inbox(self.conn, "portrait", include_acked=True)), 1)
        self.assertEqual(len(self.export_file()["extracts"]), 1)

    def test_a_reviewed_day_is_left_alone(self):
        owl_id = portrait.export_day(self.conn, now=NOW)["owl_id"]
        owlery.read(self.conn, owl_id, "portrait", now=NOW)
        owlery.ack(self.conn, owl_id, "portrait", now=NOW)
        before = (self.inbox("portrait") / f"export-{DATE}.json").read_bytes()
        self.extract("after the review", NOW + 30)
        self.assertEqual(portrait.export_day(self.conn, now=NOW + 60), {"date": DATE, "owl_id": owl_id, "reviewed": True})
        self.assertEqual((self.inbox("portrait") / f"export-{DATE}.json").read_bytes(), before)

    def test_the_next_day_gets_its_own_export_and_owl(self):
        first = portrait.export_day(self.conn, now=NOW)
        second = portrait.export_day(self.conn, now=NOW + DAY)
        self.assertNotEqual(first["owl_id"], second["owl_id"])
        self.assertEqual(second["date"], "2027-01-16")
        self.assertTrue((self.inbox("portrait") / "export-2027-01-16.json").is_file())

    def test_export_only_files_no_owl_and_starts_nothing(self):
        result = portrait.nightly(self.conn, export_only=True, now=NOW)
        self.assertEqual((result["ok"], result["ran"], result["owl_id"]), (True, False, None))
        self.assertEqual(owlery.inbox(self.conn, "portrait"), [])
        self.assertTrue((self.inbox("portrait") / f"export-{DATE}.json").is_file())

    def test_extracts_past_the_byte_budget_are_left_out(self):
        for index in range(3):
            self.extract(f"note {index} " + "x" * 300, NOW - 60 + index)
        with mock.patch.object(config, "PORTRAIT_EXPORT_MAX_BYTES", 1000):
            portrait.export_day(self.conn, now=NOW)
        export = self.export_file()
        self.assertEqual((len(export["extracts"]), export["extracts_left_out"]), (2, 1))

    def test_the_export_refuses_a_linked_inbox(self):
        target = self.tmp / "elsewhere"
        target.mkdir(mode=0o700)
        inbox = self.inbox("portrait")
        os.rename(inbox, self.tmp / "moved-inbox")
        os.symlink(target, inbox)
        with self.assertRaises(FleetError):
            portrait.export_day(self.conn, now=NOW)
        self.assertEqual(os.listdir(target), [])


class NightlyRunTests(PortraitCase):
    def desk_writes(self, files: dict, returncode: int = 0):
        """A portrait run that writes files into its outbox and prints a Claude result."""
        def run(argv, cwd, env, stdin, stdout, stderr, timeout, check, pass_fds=()):
            for name, text in files.items():
                self.write_file(self.outbox("portrait") / name, text)
            os.write(stdout, (json.dumps(CLAUDE_OK) + "\n").encode("utf-8"))
            return subprocess.CompletedProcess(args=argv, returncode=returncode)
        return fake_children(run)

    def events_of(self, kind: str) -> list:
        return [event for event in self.events() if event["kind"] == kind]

    def test_the_night_runs_the_portrait_and_flags_its_patch(self):
        self.enable("portrait")
        files = {f"patch-{DATE}.ops": "{}", f"morning-{DATE}.md": "Nothing urgent.\n"}
        with self.desk_writes(files) as started:
            result = portrait.nightly(self.conn, now=NOW)
        self.assertEqual((result["ok"], result["ran"], result["patch_ready"], result["exit_code"]), (True, True, True, 0))
        argv = started.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--mcp-config", argv)
        self.assertTrue(argv[-1].startswith(f"Owl {result['owl_id']} was delivered"))
        self.assertEqual(started.call_args.kwargs["cwd"], f"{self.castle}/desks/portrait")
        self.assertEqual(owlery.inbox(self.conn, "portrait"), [])
        [ready] = self.events_of("portrait.patch-ready")
        self.assertEqual(ready["verdict"], "headmaster")
        self.assertIn(f"castle portrait show {DATE}", ready["summary"])
        with fake_children() as again:
            self.assertTrue(portrait.nightly(self.conn, now=NOW + 60)["reviewed"])
        again.assert_not_called()

    def test_a_clean_run_without_a_patch_flags_nothing(self):
        self.enable("portrait")
        with self.desk_writes({f"morning-{DATE}.md": "A quiet day.\n"}):
            result = portrait.nightly(self.conn, now=NOW)
        self.assertEqual((result["ok"], result["patch_ready"]), (True, False))
        self.assertEqual(self.events_of("portrait.patch-ready"), [])

    def test_a_failed_run_is_reported_and_leaves_the_owl(self):
        self.enable("portrait")
        with self.desk_writes({f"patch-{DATE}.ops": "{}"}, returncode=1):
            result = portrait.nightly(self.conn, now=NOW)
        self.assertEqual((result["ok"], result["patch_ready"]), (False, False))
        self.assertEqual([event["desk"] for event in self.events_of("rundesk.failed")], ["portrait"])
        self.assertEqual(self.events_of("portrait.patch-ready"), [])
        self.assertEqual(len(owlery.inbox(self.conn, "portrait")), 1)

    def test_a_disabled_portrait_is_a_failed_night(self):
        with fake_children() as started:
            with self.assertRaises(FleetError):
                portrait.nightly(self.conn, now=NOW)
        started.assert_not_called()
        self.assertEqual([event["verdict"] for event in self.events_of("rundesk.failed")], ["headmaster"])

    def test_the_chat_job_is_passed_only_when_configured(self):
        self.enable("portrait")
        self.write_file(self.office / "desks" / "portrait" / "mcp-chat.json", json.dumps({"mcpServers": {}}))
        with mock.patch.object(config, "PORTRAIT_MCP_JOB", "chat"), self.desk_writes({}) as started:
            portrait.nightly(self.conn, now=NOW)
        argv = started.call_args.args[0]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], f"{self.office}/desks/portrait/mcp-chat.json")

    def test_main_prints_json_and_runs_one_job_at_a_time(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = portrait.main(["--export-only"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["ran"], False)
        (self.office / "locks").mkdir(mode=0o700, exist_ok=True)
        with safefs.opened_dir(str(self.office), "locks") as fd, safefs.held_lock(fd, portrait.JOB_LOCK, blocking=False):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(portrait.main([]), 1)
        self.assertIn("lock", json.loads(err.getvalue())["error"])


# The patch: schema, show, apply and the list


class PatchTests(PortraitCase):
    def setUp(self) -> None:
        super().setUp()
        self.stale = self.fact("the release train leaves on tuesdays", key="release.train")
        self.keyed = self.fact("web-app deploys need one approval", key="web-app.approvals")
        self.ops = [
            op("f1", "fact_add", scope="fleet", text="the store runs on the system python", tier="pinned",
               subject_key="store.python"),
            op("f2", "fact_retire", fact_id=self.stale, how="archive"),
            op("f3", "fact_edit", fact_id=self.keyed, text="web-app deploys need two approvals"),
            op("n1", "memory_note_add", text="desks asked twice where the charter lives", tags=["charter"]),
            op("m1", "archive_move", entry="old note about the launcher race", to="memory archive"),
        ]

    def test_show_lists_each_op_with_its_status_and_changes_nothing(self):
        sha = self.write_patch(self.ops + [op("x1", "fact_wipe", fact_id=1)])
        before = self.counts()
        shown = portrait_patch.show(self.conn, DATE, now=NOW)
        self.assertEqual(self.counts(), before)
        self.assertEqual(shown["sha256"], sha)
        statuses = {item["id"]: item["status"] for item in shown["ops"]}
        self.assertEqual(statuses, {"f1": "ready", "f2": "ready", "f3": "ready", "n1": "ready", "m1": "ready",
                                    "x1": "out of schema"})
        self.assertEqual(shown["ready"], ["f1", "f2", "f3", "n1", "m1"])
        self.assertEqual(shown["apply_command"], f"castle portrait apply {DATE} --sha256 {sha} --only f1,f2,f3,n1,m1")
        f3 = next(item for item in shown["ops"] if item["id"] == "f3")
        self.assertEqual(f3["fields"]["text"], "web-app deploys need two approvals")
        self.assertIn(f"fact {self.keyed} replaced by fact", f3["would"])
        self.assertNotIn("fields", next(item for item in shown["ops"] if item["id"] == "x1"))

    def test_a_patch_with_every_op_ready_gets_a_plain_command(self):
        sha = self.write_patch(self.ops)
        self.assertEqual(portrait_patch.show(self.conn, DATE, now=NOW)["apply_command"],
                         f"castle portrait apply {DATE} --sha256 {sha}")

    def test_apply_runs_each_op_through_the_store_and_records_it(self):
        sha = self.write_patch(self.ops)
        result = portrait_patch.apply(self.conn, DATE, sha, now=NOW)
        done = {item["id"]: item for item in result["applied"]}
        self.assertEqual(list(done), ["f1", "f2", "f3", "n1", "m1"])
        added = self.fact_row(done["f1"]["fact_id"])
        self.assertEqual((added["text"], added["tier"], added["subject_key"], added["source"]),
                         ("the store runs on the system python", "pinned", "store.python", f"portrait:{DATE}:f1"))
        self.assertIsNotNone(self.fact_row(self.stale)["archived_at"])
        old, new = self.fact_row(self.keyed), self.fact_row(done["f3"]["fact_id"])
        self.assertEqual((old["end_reason"], old["superseded_by"]), ("superseded", new["id"]))
        self.assertEqual((new["text"], new["subject_key"], new["source"]),
                         ("web-app deploys need two approvals", "web-app.approvals", f"portrait:{DATE}:f3"))
        keypoint = dict(self.conn.execute("SELECT * FROM keypoints").fetchone())
        self.assertEqual((keypoint["id"], keypoint["text"], keypoint["tags"]),
                         (done["n1"]["keypoint_id"], "desks asked twice where the charter lives", "portrait,charter"))
        self.assertEqual(result["for_you"], [{"id": "m1", "entry": "old note about the launcher race",
                                              "to": "memory archive"}])
        self.assertEqual(result["left_out"], [])
        recorded = [event for event in self.events() if event["kind"] == "portrait.applied"]
        self.assertEqual(len(recorded), 5)
        self.assertTrue(all(event["verdict"] == "routine" and event["desk"] == "portrait" for event in recorded))
        self.assertIn(sha[:12], recorded[0]["summary"])
        self.assertEqual(sorted(portrait_patch.applied_ops(self.conn, DATE)), ["f1", "f2", "f3", "m1", "n1"])
        shown = portrait_patch.show(self.conn, DATE, now=NOW + 10)
        self.assertEqual({item["status"] for item in shown["ops"]}, {"applied"})
        self.assertIsNone(shown["apply_command"])

    def test_only_applies_the_named_ops_and_the_rest_can_follow(self):
        sha = self.write_patch(self.ops)
        first = portrait_patch.apply(self.conn, DATE, sha, only=["n1,f2"], now=NOW)
        self.assertEqual([item["id"] for item in first["applied"]], ["f2", "n1"])
        self.assertEqual(first["left_out"], ["f1", "f3", "m1"])
        self.assertIsNone(self.fact_row(self.keyed)["closed_at"])
        rest = portrait_patch.apply(self.conn, DATE, sha, only=["f1", "f3", "m1"], now=NOW + 10)
        self.assertEqual([item["id"] for item in rest["applied"]], ["f1", "f3", "m1"])

    def test_an_op_is_never_applied_twice(self):
        sha = self.write_patch(self.ops)
        portrait_patch.apply(self.conn, DATE, sha, only=["n1"], now=NOW)
        before = self.counts()
        for only in (["n1"], None):
            with self.subTest(only=only):
                with self.assertRaisesRegex(ConflictError, "already applied.*n1"):
                    portrait_patch.apply(self.conn, DATE, sha, only=only, now=NOW + 10)
        self.assertEqual(self.counts(), before)

    def test_the_reviewed_bytes_are_the_applied_bytes(self):
        sha = self.write_patch(self.ops)
        before = self.counts()
        with self.assertRaises(IntegrityError):
            portrait_patch.apply(self.conn, DATE, "0" * 64, now=NOW)
        self.write_patch(self.ops[:1])
        with self.assertRaises(IntegrityError):
            portrait_patch.apply(self.conn, DATE, sha, now=NOW)
        for bad in ("", "abc", "g" * 64, None):
            with self.subTest(sha=bad):
                with self.assertRaises(ValidationError):
                    portrait_patch.apply(self.conn, DATE, bad, now=NOW)
        self.assertEqual(self.counts(), before)

    def test_one_store_refusal_applies_nothing(self):
        ops = [op("f1", "fact_add", scope="fleet", text="the charter sits at the castle root", tier="aging"),
               op("f2", "fact_add", scope="fleet", text="the main build is green", tier="aging")]
        sha = self.write_patch(ops)
        shown = {item["id"]: item for item in portrait_patch.show(self.conn, DATE, now=NOW)["ops"]}
        self.assertEqual(shown["f1"]["status"], "ready")
        self.assertEqual(shown["f2"]["status"], "the store would refuse it")
        self.assertIn("volatile", shown["f2"]["problem"])
        before = self.counts()
        with self.assertRaisesRegex(ValidationError, "op f2"):
            portrait_patch.apply(self.conn, DATE, sha, now=NOW)
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(portrait_patch.apply(self.conn, DATE, sha, only=["f1"], now=NOW)["applied"]), 1)

    def test_an_op_out_of_schema_is_never_applied(self):
        sha = self.write_patch(self.ops + [op("x1", "fact_add", scope="fleet", text="mail ryan@example.com",
                                              tier="aging")])
        before = self.counts()
        for only in (None, ["x1"], ["f1", "x1"]):
            with self.subTest(only=only):
                with self.assertRaisesRegex(ValidationError, "x1"):
                    portrait_patch.apply(self.conn, DATE, sha, only=only, now=NOW)
        self.assertEqual(self.counts(), before)

    def test_keys_and_tags_that_hold_secrets_are_shown_out_of_schema_and_never_applied(self):
        risky = [op("k1", "fact_add", scope="fleet", text="the box is slow", tier="aging", subject_key="host:10.1.2.3"),
                 op("t1", "memory_note_add", text="the box is slow", tags=["ab" * 16])]
        sha = self.write_patch(self.ops + risky)
        shown = portrait_patch.show(self.conn, DATE, now=NOW)
        statuses = {item["id"]: item["status"] for item in shown["ops"]}
        self.assertEqual((statuses["k1"], statuses["t1"]), ("out of schema", "out of schema"))
        self.assertNotIn("10.1.2.3", json.dumps(shown))
        self.assertNotIn("ab" * 16, json.dumps(shown))
        before = self.counts()
        for only in (["k1"], ["t1"], None):
            with self.subTest(only=only):
                with self.assertRaises(ValidationError):
                    portrait_patch.apply(self.conn, DATE, sha, only=only, now=NOW)
        self.assertEqual(self.counts(), before)

    def test_token_like_op_ids_and_scopes_are_never_shown_or_applied(self):
        token = "xoxb-abcdefghij"
        sha = self.write_patch(self.ops + [op(token, "archive_move", entry="e", to="t")])
        for call in (lambda: portrait_patch.show(self.conn, DATE, now=NOW),
                     lambda: portrait_patch.apply(self.conn, DATE, sha, now=NOW)):
            with self.assertRaises(ValidationError) as caught:
                call()
            self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(token, json.dumps(portrait_patch.patches(self.conn)))
        sha = self.write_patch(self.ops + [op("s1", "fact_add", scope=token, text="t", tier="aging")])
        shown = portrait_patch.show(self.conn, DATE, now=NOW)
        [entry] = [item for item in shown["ops"] if item["id"] == "s1"]
        self.assertEqual(entry["status"], "out of schema")
        self.assertNotIn(token, json.dumps(shown))
        before = self.counts()
        with self.assertRaises(ValidationError) as caught:
            portrait_patch.apply(self.conn, DATE, sha, only=["s1"], now=NOW)
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_unknown_field_names_are_counted_never_shown(self):
        secret = "ghp_" + "a" * 30
        sha = self.write_patch(self.ops + [op("s1", "archive_move", entry="e", to="t", **{secret: "x"})])
        shown = portrait_patch.show(self.conn, DATE, now=NOW)
        [entry] = [item for item in shown["ops"] if item["id"] == "s1"]
        self.assertEqual(entry["status"], "out of schema")
        self.assertNotIn(secret, json.dumps(shown))
        with self.assertRaises(ValidationError) as caught:
            portrait_patch.apply(self.conn, DATE, sha, now=NOW)
        self.assertNotIn(secret, str(caught.exception))

    def test_only_must_name_ops_of_the_patch(self):
        sha = self.write_patch(self.ops)
        for only in (["zz"], ["F1"], ["f1", "f1"], ["f1,f1"], [","], ["f1;n1"]):
            with self.subTest(only=only):
                with self.assertRaises(ValidationError):
                    portrait_patch.apply(self.conn, DATE, sha, only=only, now=NOW)
        self.assertEqual(portrait_patch.applied_ops(self.conn, DATE), {})

    def test_an_edit_of_a_keyless_fact_names_its_key(self):
        loose = self.fact("reviews start on their own after a handoff")
        self.write_patch([op("e1", "fact_edit", fact_id=loose, text="reviews start by themselves after a handoff")])
        [shown] = portrait_patch.show(self.conn, DATE, now=NOW)["ops"]
        self.assertIn("has no subject key", shown["problem"])
        sha = self.write_patch([op("e1", "fact_edit", fact_id=loose, subject_key="reviews.start",
                                   text="reviews start by themselves after a handoff")])
        [done] = portrait_patch.apply(self.conn, DATE, sha, now=NOW)["applied"]
        self.assertEqual((self.fact_row(loose)["subject_key"], self.fact_row(loose)["end_reason"]),
                         ("reviews.start", "superseded"))
        self.assertEqual(self.fact_row(done["fact_id"])["subject_key"], "reviews.start")

    def test_an_edit_keeps_a_keyed_fact_on_its_key(self):
        self.write_patch([op("e1", "fact_edit", fact_id=self.keyed, subject_key="web-app.other",
                             text="web-app deploys need two approvals")])
        [shown] = portrait_patch.show(self.conn, DATE, now=NOW)["ops"]
        self.assertIn("already has a subject key", shown["problem"])

    def test_withdraw_brings_back_the_fact_it_replaced(self):
        replaced = facts.supersede(self.conn, "fleet", "release.train", "the release train leaves on mondays",
                                   "ryan", now=NOW - 500)["fact_id"]
        sha = self.write_patch([op("w1", "fact_retire", fact_id=replaced, how="withdraw")])
        [done] = portrait_patch.apply(self.conn, DATE, sha, now=NOW)["applied"]
        self.assertEqual(self.fact_row(replaced)["end_reason"], "withdrawn")
        restored = self.fact_row(done["restored_id"])
        self.assertEqual((restored["text"], restored["restores"]), ("the release train leaves on tuesdays", self.stale))

    def test_retire_and_edit_need_a_current_fact(self):
        pensieve.archive(self.conn, [self.stale], now=NOW - 10)
        self.write_patch([op("r1", "fact_retire", fact_id=self.stale, how="archive"),
                          op("r2", "fact_retire", fact_id=9999, how="withdraw"),
                          op("e1", "fact_edit", fact_id=self.stale, text="the release train leaves on fridays")])
        for item in portrait_patch.show(self.conn, DATE, now=NOW)["ops"]:
            with self.subTest(op=item["id"]):
                self.assertEqual(item["status"], "the store would refuse it")
                self.assertIn("is not a current fact", item["problem"])

    def test_each_op_is_held_to_its_schema(self):
        cases = {
            "unknown type": op("a", "fact_delete", fact_id=1),
            "type that is a list": op("a", ["fact_add"]),
            "no reason": {"id": "a", "type": "archive_move", "source": "s", "entry": "e", "to": "t"},
            "a field of another type": op("a", "fact_retire", fact_id=1, how="archive", text="x"),
            "a stray field": op("a", "archive_move", entry="e", to="t", run="rm -rf /"),
            "fact id as true": op("a", "fact_retire", fact_id=True, how="archive"),
            "fact id as text": op("a", "fact_retire", fact_id="12", how="archive"),
            "fact id zero": op("a", "fact_retire", fact_id=0, how="archive"),
            "retire how unknown": op("a", "fact_retire", fact_id=1, how="delete"),
            "text over two lines": op("a", "memory_note_add", text="one\ntwo"),
            "text with a tab": op("a", "memory_note_add", text="one\ttwo"),
            "text with a line separator": op("a", "memory_note_add", text="one\u2028two"),
            "text with a zero width space": op("a", "memory_note_add", text="ign\u200bore"),
            "text with a bidi control": op("a", "memory_note_add", text="abc\u202edef"),
            "text with an escape": op("a", "memory_note_add", text="red \x1b[31m text"),
            "text with a space at the end": op("a", "memory_note_add", text="note "),
            "empty text": op("a", "memory_note_add", text=""),
            "an email": op("a", "memory_note_add", text="ask someone@example.com first"),
            "an ip address": op("a", "memory_note_add", text="the box at 10.1.2.3 is slow"),
            "a token": op("a", "memory_note_add", text="key ghp_" + "a" * 30),
            "a long hex string": op("a", "memory_note_add", text="hash " + "ab" * 20),
            "a secret in the reason": {**op("a", "archive_move", entry="e", to="t"), "reason": "password=hunter2"},
            "a reason too long": {**op("a", "archive_move", entry="e", to="t"), "reason": "r" * 401},
            "a fact text too long": op("a", "fact_add", scope="fleet", text="t" * 301, tier="aging"),
            "a note too long": op("a", "memory_note_add", text="t" * 501),
            "an unknown tier": op("a", "fact_add", scope="fleet", text="t", tier="forever"),
            "a bad scope": op("a", "fact_add", scope="Fleet", text="t", tier="aging"),
            "a bad subject key": op("a", "fact_add", scope="fleet", text="t", tier="aging", subject_key="Bad Key"),
            "perishable without expiry": op("a", "fact_add", scope="fleet", text="t", tier="perishable"),
            "aging with an expiry": op("a", "fact_add", scope="fleet", text="t", tier="aging", expires_at=NOW + 10),
            "a lookup with credentials": op("a", "fact_add", scope="fleet", text="t", tier="aging",
                                            lookup="https://user:pw@example.com/x"),
            "tags not a list": op("a", "memory_note_add", text="t", tags="design"),
            "too many tags": op("a", "memory_note_add", text="t", tags=[f"t{n}" for n in range(9)]),
            "a repeated tag": op("a", "memory_note_add", text="t", tags=["x", "x"]),
            "a bad tag": op("a", "memory_note_add", text="t", tags=["Bad Tag"]),
            "a subject key holding an ip": op("a", "fact_add", scope="fleet", text="t", tier="aging",
                                              subject_key="host:10.1.2.3"),
            "a subject key holding long hex": op("a", "fact_add", scope="fleet", text="t", tier="aging",
                                                 subject_key="hash." + "ab" * 20),
            "a tag holding an ip": op("a", "memory_note_add", text="t", tags=["10.1.2.3"]),
            "a tag holding long hex": op("a", "memory_note_add", text="t", tags=["ab" * 16]),
            "an entry too long": op("a", "archive_move", entry="e" * 201, to="t"),
            "a target that is a path list": op("a", "archive_move", entry="e", to=["a", "b"]),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                self.write_patch([case])
                [entry] = portrait_patch.parse_patch(self.patch_path().read_bytes(), DATE)["ops"]
                self.assertIsNone(entry["op"])
                self.assertTrue(entry["problem"])

    def test_malformed_patches_are_refused_whole(self):
        good = op("a", "archive_move", entry="e", to="t")
        cases = {
            "not json": b"ops: none",
            "not utf-8": b'{"format": "\xff"}',
            "a repeated key": b'{"format": "portrait-patch-1", "format": "x", "date": "2027-01-15", "ops": []}',
            "nan": json.dumps({"format": FORMAT, "date": DATE, "ops": [{**good, "fact_id": "NaN"}]}).encode()
            .replace(b'"NaN"', b"NaN"),
            "a decimal": json.dumps({"format": FORMAT, "date": DATE, "ops": [good]}).encode().replace(b"}]", b', "n": 1.5}]'),
            "a huge number": json.dumps({"format": FORMAT, "date": DATE, "ops": [good]}).encode()
            .replace(b"}]", b', "n": ' + b"9" * 400 + b"}]"),
            "a list": json.dumps([good]).encode(),
            "an extra top field": json.dumps({"format": FORMAT, "date": DATE, "ops": [good], "run": "x"}).encode(),
            "another format": json.dumps({"format": "portrait-patch-2", "date": DATE, "ops": [good]}).encode(),
            "another date": json.dumps({"format": FORMAT, "date": "2027-01-14", "ops": [good]}).encode(),
            "no ops": json.dumps({"format": FORMAT, "date": DATE, "ops": []}).encode(),
            "too many ops": json.dumps({"format": FORMAT, "date": DATE,
                                        "ops": [{**good, "id": f"a{n}"} for n in range(101)]}).encode(),
            "ops not a list": json.dumps({"format": FORMAT, "date": DATE, "ops": {"a": good}}).encode(),
            "an op that is not an object": json.dumps({"format": FORMAT, "date": DATE, "ops": ["a"]}).encode(),
            "an op without an id": json.dumps({"format": FORMAT, "date": DATE,
                                               "ops": [{k: v for k, v in good.items() if k != "id"}]}).encode(),
            "an id with capitals": json.dumps({"format": FORMAT, "date": DATE, "ops": [{**good, "id": "A1"}]}).encode(),
            "an id too long": json.dumps({"format": FORMAT, "date": DATE, "ops": [{**good, "id": "a" * 25}]}).encode(),
            "a repeated id": json.dumps({"format": FORMAT, "date": DATE, "ops": [good, good]}).encode(),
            "deep nesting": b'{"format": "portrait-patch-1", "date": "2027-01-15", "ops": ' + b"[" * 100000
            + b"]" * 100000 + b"}",
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.write_raw(raw)
                with self.assertRaises(ValidationError):
                    portrait_patch.show(self.conn, DATE, now=NOW)

    def test_unsafe_patch_files_are_refused(self):
        sha = self.write_patch(self.ops)
        elsewhere = self.tmp / "elsewhere.ops"
        os.rename(self.patch_path(), elsewhere)
        os.symlink(elsewhere, self.patch_path())
        with self.assertRaises(ValidationError):
            portrait_patch.apply(self.conn, DATE, sha, now=NOW)
        os.unlink(self.patch_path())
        os.link(elsewhere, self.patch_path())
        with self.assertRaises(ValidationError):
            portrait_patch.show(self.conn, DATE, now=NOW)
        os.unlink(elsewhere)
        os.chmod(self.patch_path(), 0o620)
        with self.assertRaises(ValidationError):
            portrait_patch.show(self.conn, DATE, now=NOW)
        self.write_file(self.patch_path(), b" " * (portrait_patch.PATCH_MAX_BYTES + 1))
        with self.assertRaises(ValidationError):
            portrait_patch.show(self.conn, DATE, now=NOW)
        with self.assertRaises(NotFoundError):
            portrait_patch.show(self.conn, "2027-01-14", now=NOW)
        self.assertEqual(portrait_patch.applied_ops(self.conn, DATE), {})

    def test_dates_are_checked_before_any_file_is_named(self):
        for bad in ("2027-1-15", "2027-02-30", "../2027-01-15", "2027-01-15/../x", "", None, 20270115):
            with self.subTest(date=bad):
                with self.assertRaises(ValidationError):
                    portrait_patch.show(self.conn, bad, now=NOW)

    def test_patches_lists_the_newest_with_what_was_applied(self):
        sha = self.write_patch(self.ops)
        self.write_patch([op("a", "archive_move", entry="e", to="t")], date="2027-01-14")
        self.write_raw(b"not json", date="2027-01-13")
        self.write_file(self.outbox("portrait") / "notes.md", "not a patch")
        self.write_file(self.outbox("portrait") / "patch-2027-13-01.ops", "{}")
        portrait_patch.apply(self.conn, DATE, sha, only=["n1"], now=NOW)
        listed = portrait_patch.patches(self.conn)
        self.assertEqual([item.get("date", item["file"]) for item in listed],
                         ["patch-2027-13-01.ops", DATE, "2027-01-14", "2027-01-13"])
        self.assertIn("YYYY-MM-DD", listed[0]["problem"])
        self.assertEqual((listed[1]["sha256"], listed[1]["ops"], listed[1]["applied"], listed[1]["out_of_schema"]),
                         (sha, ["f1", "f2", "f3", "n1", "m1"], ["n1"], []))
        self.assertIn("not strict", listed[3]["problem"])


class CastleCommandTests(PortraitCase):
    def run_castle(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_show_then_apply_from_the_command_line(self):
        sha = self.write_patch([
            op("f1", "fact_add", scope="ron", text="the flaky ledger lives under notes", tier="aging"),
            op("n1", "memory_note_add", text="the export reached the inbox"),
        ])
        code, shown = self.run_castle("portrait", "show", DATE)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["data"]["apply_command"], f"castle portrait apply {DATE} --sha256 {sha}")
        code, listed = self.run_castle("portrait", "patches")
        self.assertEqual((code, listed["data"][0]["date"]), (0, DATE))
        code, applied = self.run_castle("portrait", "apply", DATE, "--sha256", sha, "--only", "n1")
        self.assertEqual((code, [item["id"] for item in applied["data"]["applied"]]), (0, ["n1"]))
        code, applied = self.run_castle("portrait", "apply", DATE, "--sha256", sha.upper())
        self.assertEqual(code, 3, applied)
        code, applied = self.run_castle("portrait", "apply", DATE, "--sha256", sha, "--only", "f1")
        self.assertEqual((code, applied["data"]["applied"][0]["id"]), (0, "f1"))

    def test_bad_commands_exit_with_the_store_codes(self):
        sha = self.write_patch([op("n1", "memory_note_add", text="a note")])
        for argv, code in ((("portrait", "apply", DATE), 2), (("portrait", "show", "2027-13-01"), 2),
                           (("portrait", "show", "2027-01-14"), 4), (("portrait", "apply", DATE, "--sha256", "0" * 64), 5),
                           (("portrait", "apply", DATE, "--sha256", sha, "--only"), 2)):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_castle(*argv)[0], code)
