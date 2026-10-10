"""Every castle and fleet command is classified, and every list-style one keeps its default output under a fixed byte
budget on a store seeded with many rows, while --all still prints everything. A new subcommand fails here until it is
classified, so a new unbounded default cannot slip in."""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout

from hogwarts import capacity, cli, facts, followups, ids, owlery, pensieve, wands

from fleet import config, tools
from tests_fleet.support import FleetCase

# The most a default listing may print, newline included.
BUDGET_BYTES = 20 * 1024
# Old enough that the audit counts the seeded tasks as long active.
T0 = 1_700_000_000
SHA = "{:040x}"
REPO = "acme/web-app"

# Commands that change something: their output is about that one change.
ACTIONS = {
    "init", "desk add", "desk cap", "desk model", "desk many-tasks", "model line", "ollivander clear", "task create",
    "task start", "task await-close", "task commit", "task worktree", "task close", "task allow-round", "token mint",
    "owl send", "owl read", "owl ack", "request open", "request advance", "request defer", "request decline",
    "review record", "event add", "event ack", "event settle", "pensieve session", "pensieve extract",
    "pensieve keypoint", "fact add", "fact touch", "fact archive", "fact supersede", "fact withdraw", "fact expire",
    "fact apply", "portrait apply", "metric add", "purge",
}
# One record each, bounded by what it is: a task row, one request with its few phases and owls, one commit's latest
# review, a task's follow-ups (at most FOLLOWUP_MAX_PER_TASK), one patch file (at most PATCH_MAX_BYTES), the store's
# own health checks.
SINGLE = {"task show", "request show", "review check", "followup show", "portrait show", "doctor"}
# Fleet commands: each acts on one task or job, feed prints only what is new from the moment it starts, and go-wait
# prints one line about one go task.
FLEET_COMMANDS = {
    "worktree", "build", "worktree-remove", "worktree-rebuild", "verify", "review", "loops", "feed", "go-wait", "push",
    "ollivander", "close", "patrol-restart", "adopt", "gringotts",
}


def castle_commands(parser: argparse.ArgumentParser) -> set:
    found = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                found |= castle_commands(child)
    key = parser.get_default("handler_key")
    return found | ({key} if key else set())


class ListBudgetTests(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    # seeding

    def seed(self) -> None:
        conn = self.conn
        for index in range(30):  # a registry larger than one listing, each desk with a task in flight
            pensieve.add_desk(conn, f"extra-{index}", "claude", now=T0)
            pensieve.add_metric(conn, f"extra-{index}", f"run-extra-{index}", "model-x", 1, 1, 1, 0.01, 1, ts=T0)
            pensieve.start_task(conn, pensieve.create_task(conn, f"extra-{index}", "spread", now=T0 - 1)["id"],
                                now=T0 - 1)
        for index in range(200):
            task = pensieve.create_task(conn, "hermione", f"active task {index} " + "t" * 60, now=T0 + index)
            pensieve.start_task(conn, task["id"], now=T0 + index)
        for index in range(300):
            task = pensieve.create_task(conn, "ron", f"queued task {index} " + "t" * 60, now=T0 + index)
            if index < 100:
                pensieve.close_task(conn, task["id"], "abandoned", now=T0 + index)
        for index in range(500):
            pensieve.add_event(conn, "ron", "ci.red", "headmaster", f"red run {index} " + "e" * 80, now=T0 + index)
            owlery.send(conn, "mcgonagall", "hermione", "fyi", f"note {index} " + "o" * 60, now=T0 + index)
            pensieve.add_metric(conn, ("hermione", "ron", "harry")[index % 3], f"run-{index}", "model-x", 1, 1, 1,
                                0.01, 1, ts=T0 + index)
            pensieve.add_fact(conn, "fleet", f"needle fact {index} about the release train " + "f" * 60, "aging",
                              "ryan", subject_key=f"key.{index}", now=T0)
        for index in range(60):
            owlery.open_request(conn, "mcgonagall", "harry", f"request {index}", now=T0 + index)
            pensieve.add_keypoint(conn, f"needle keypoint {index} " + "k" * 200, now=T0 + index)
        for index in range(50):
            facts.supersede(conn, "fleet", "ci.main", f"main runs the deploy gate version {index}", "ryan",
                            valid_from=T0 + index)
        self.round_task = pensieve.start_task(conn, pensieve.create_task(conn, "hermione", "reviewed fifty times",
                                                                         now=T0)["id"], now=T0)["id"]
        for index in range(50):
            capacity.open_review_round(conn, self.round_task, "moody", SHA.format(index + 1), f"review {index}",
                                       idempotency_key=ids.new_id("request"), now=T0 + index)
        go = pensieve.create_task(conn, "mcgonagall", "go", now=T0)["id"]
        for index in range(30):  # builds that all branched from the latest round's sha
            build = pensieve.create_task(conn, "harry", f"build {index}", parent_task_id=go, now=T0)["id"]
            pensieve.start_task(conn, build, now=T0)
            capacity.open_review_round(conn, build, "hermione", SHA.format(50), "review",
                                       idempotency_key=ids.new_id("request"), now=T0)
        self.seed_followups(25)
        self.seed_patches(40)

    def seed_followups(self, count: int) -> None:
        conn = self.conn
        parent = pensieve.start_task(conn, pensieve.create_task(conn, "mcgonagall", "parent", now=T0)["id"],
                                     now=T0)["id"]
        for index in range(count):
            opened = owlery.open_request(conn, "mcgonagall", "harry", f"build {index}", parent_task_id=parent, now=T0)
            task_id = opened["task"]["id"]
            pensieve.start_task(conn, task_id, now=T0)
            pensieve.set_worktree(conn, task_id, f"{ids.WORKTREES_ROOT}/wt-{index}")
            sha = SHA.format(1000 + index)
            pensieve.record_commit(conn, task_id, REPO, sha, now=T0)
            round_opened = capacity.open_review_round(conn, task_id, "hermione", sha, "review",
                                                      idempotency_key=ids.new_id("request"), now=T0)
            pensieve.start_task(conn, round_opened["task"]["id"], now=T0)
            capacity.record_round_verdict(conn, round_opened["request"]["id"], REPO, "PASS", now=T0)
            pensieve.mark_awaiting_close(conn, task_id, now=T0)
            followups.bind_pr(conn, task_id, REPO, 100 + index, "fix/widget", "main", sha,
                              f"https://github.com/{REPO}/pull/{100 + index}", now=T0)
            comment = str(1000 + index)
            item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": comment, "quote": None,
                    "url": f"https://github.com/{REPO}/pull/{100 + index}#issuecomment-{comment}"}
            followups.open_followup(conn, task_id, ids.new_id("followup"), sha, [item],
                                    [{"kind": "comment", "comment_id": comment, "label": "T1"}], "map", "follow-up",
                                    "the fix request", config.FOLLOWUP_MAX_PER_TASK, now=T0)

    def seed_patches(self, count: int) -> None:
        outbox = self.castle / "desks" / "portrait" / "outbox"
        for index in range(count):
            self.write_file(outbox / f"patch-2026-{1 + index // 28:02d}-{1 + index % 28:02d}.ops", "[]")

    # running

    def run_castle(self, *argv: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue(), json.loads(out.getvalue())

    def listings(self) -> dict:
        """Each list-style command and the argv of its default run."""
        return {
            "task list": [("task", "list"), ("task", "list", "--status", "queued"), ("task", "list", "--open")],
            "task builds": [("task", "builds")],
            "task board": [("task", "board")],
            "task rounds": [("task", "rounds", self.round_task)],
            "followup list": [("followup", "list")],
            "owl inbox": [("owl", "inbox", "hermione")],
            "request list": [("request", "list")],
            "event drain": [("event", "drain")],
            "pensieve find": [("pensieve", "find", "needle")],
            "fact list": [("fact", "list"), ("fact", "list", "--context", "hermione")],
            "fact current": [("fact", "current")],
            "fact find": [("fact", "find", "needle")],
            "fact as-of": [("fact", "as-of", "--world", str(2 ** 40))],
            "fact history": [("fact", "history", "--scope", "fleet", "--subject-key", "ci.main")],
            "fact candidates": [("fact", "candidates", "--since", "0")],
            "fact decay": [("fact", "decay")],
            "portrait patches": [("portrait", "patches")],
            "metric summary": [("metric", "summary")],
            "desk list": [("desk", "list")],
            "desk caps": [("desk", "caps")],
            "desk models": [("desk", "models")],
            "audit": [("audit",)],
        }

    # tests

    def test_every_command_is_classified(self):
        lists = set(self.listings())
        self.assertEqual(lists & ACTIONS, set())
        self.assertEqual((lists | ACTIONS) & SINGLE, set())
        self.assertEqual(castle_commands(cli.build_parser()), lists | ACTIONS | SINGLE)
        [fleet] = [action for action in tools.build_parser()._actions
                   if isinstance(action, argparse._SubParsersAction)]
        self.assertEqual(set(fleet.choices), FLEET_COMMANDS)

    def test_default_listings_stay_under_the_budget(self):
        for key, runs in self.listings().items():
            for argv in runs:
                with self.subTest(argv=" ".join(argv)):
                    raw, _ = self.run_castle(*argv)
                    self.assertLess(len(raw.encode("utf-8")), BUDGET_BYTES)

    def test_a_cut_listing_says_so_and_all_prints_everything(self):
        expected = {
            ("task", "list", "--status", "queued"): len(pensieve.list_tasks(self.conn, status="queued")),
            ("task", "rounds", self.round_task): 50,
            ("followup", "list"): 25,
            ("owl", "inbox", "hermione"): len(owlery.inbox(self.conn, "hermione")),
            ("request", "list"): len(owlery.list_requests(self.conn)),
            ("fact", "list"): len(pensieve.list_facts(self.conn)),
            ("fact", "current"): len(facts.current_facts(self.conn)),
            ("fact", "history", "--scope", "fleet", "--subject-key", "ci.main"): 50,
            ("fact", "decay"): len(pensieve.decay(self.conn)),
            ("desk", "list"): len(pensieve.list_desks(self.conn)),
            ("desk", "models"): len(wands.list_desk_models(self.conn)),
            ("metric", "summary"): len(pensieve.summary(self.conn)),
            ("portrait", "patches"): 40,
        }
        for argv, total in expected.items():
            with self.subTest(argv=" ".join(argv)):
                _, cut = self.run_castle(*argv)
                self.assertEqual((cut["total"], cut["truncated"]), (total, True))
                self.assertEqual(cut["note"], f"showing {cut['shown']} of {total}, use --all for everything")
                rows = (lambda data: data["stale"]) if argv == ("fact", "decay") else (lambda data: data)
                self.assertEqual(len(rows(cut["data"])), cut["shown"])
                _, everything = self.run_castle(*argv, "--all")
                self.assertEqual((len(rows(everything["data"])), everything["truncated"]), (total, False))
                self.assertNotIn("note", everything)
                _, three = self.run_castle(*argv, "--limit", "3")
                listed = rows(everything["data"])  # portrait patches lists newest first, the rest oldest first
                self.assertEqual(rows(three["data"]), listed[:3] if argv == ("portrait", "patches") else listed[-3:])
        _, board = self.run_castle("task", "board")
        self.assertEqual((board["total"], board["shown"]), (board["data"]["tasks"], cli.LIST_LIMIT))
        self.assertEqual(board["data"]["desks_shown"], len(board["data"]["desks"]))
        self.assertGreater(board["data"]["desks_total"], board["data"]["desks_shown"])
        _, rounds = self.run_castle("task", "rounds", self.round_task)
        self.assertTrue(rounds["data"][-1]["sha_note"].endswith("and 25 more"))
        _, board = self.run_castle("task", "board", "--all")
        self.assertEqual(sum(len(row["tasks"]) for row in board["data"]["desks"]), board["data"]["tasks"])
        _, audit = self.run_castle("audit")
        self.assertTrue(audit["truncated"])
        self.assertEqual(len(audit["data"]["long_active_tasks"]), cli.LIST_LIMIT)
        _, audit = self.run_castle("audit", "--all")
        self.assertEqual(len(audit["data"]["long_active_tasks"]), audit["total"] - sum(
            len(rows) for key, rows in audit["data"].items() if isinstance(rows, list) and key != "long_active_tasks"))
        _, lines = self.run_castle("task", "list")
        self.assertIn("castle task list --all lists every task", lines["data"][-1])
        _, everything = self.run_castle("task", "list", "--all")
        self.assertEqual(len(everything["data"]), len(pensieve.list_tasks(self.conn)))
