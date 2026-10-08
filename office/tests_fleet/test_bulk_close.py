"""Closing: Mischief managed everything, a go task that moves on once its last build closes, and the close's Fix lines.

The bulk close runs through the prompt hook on transcripts written here, or through the go confirmer when the prompt's
entry is not written yet. Tasks are put in each state straight through the store; no desk or reviewer ever runs.
"""
from __future__ import annotations

import json
from unittest import mock

from hogwarts import capacity, ids, owlery, pensieve
from tests.support import NOW, REPO, SHA, intent_file, proof

from fleet import bulk_close, closer, config, go_confirm, run_desk, safefs
from fleet.hooks import user_prompt_submit as hook
from tests_fleet.support import PROMPT_ID, user_entry
from tests_fleet.test_go_confirm import Clock
from tests_fleet.test_hooks import HookCase

PHRASE = "Mischief managed everything"


class CloseCase(HookCase):
    def setUp(self) -> None:
        super().setUp()
        self.shas = iter(f"{index:040x}" for index in range(1, 1000))

    # prompts

    def said(self, text: str, transcript: str = None, prompt_id: str = PROMPT_ID, **fields) -> tuple:
        """(shown to Ryan, given to the session) for one prompt Ryan typed, or ("", "") when the hook was silent."""
        path = transcript or self.write_transcript([user_entry(text, promptId=prompt_id)])
        fields = {key: value for key, value in {"prompt_id": prompt_id, **fields}.items() if value is not None}
        code, out, err = self.run_hook(hook, self.hook_input("UserPromptSubmit", path, prompt=text, **fields))
        self.assertEqual(code, 0, err)
        if not out:
            return "", ""
        data = json.loads(out)
        return data["systemMessage"], data["hookSpecificOutput"]["additionalContext"]

    # tasks in each state

    def built(self, desk: str = "harry", parent: str = None, title: str = "build it") -> dict:
        if parent is not None:
            task = owlery.open_request(self.conn, "mcgonagall", desk, title, parent_task_id=parent, now=NOW)["task"]
        else:
            task = pensieve.create_task(self.conn, desk, title, now=NOW)
        return pensieve.start_task(self.conn, task["id"], now=NOW)

    def reviewer(self, desk: str) -> str:
        return config.REVIEWER_FOR_FAMILY[pensieve.get_desk(self.conn, desk)["family"]]

    def round(self, task: dict, verdict: str = None) -> dict:
        """A review round of the task at a new commit, with its verdict recorded and its reviewer task closed."""
        sha = next(self.shas)
        pensieve.record_commit(self.conn, task["id"], REPO, sha, now=NOW)
        opened = capacity.open_review_round(self.conn, task["id"], self.reviewer(task["desk"]), sha, "review it",
                                            now=NOW)
        if verdict is not None:
            pensieve.start_task(self.conn, opened["task"]["id"], now=NOW)
            capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, verdict, now=NOW)
            pensieve.close_task(self.conn, opened["task"]["id"], "superseded", now=NOW)
        return {**opened, "sha": sha}

    def passed(self, desk: str = "harry", parent: str = None, title: str = "passed") -> dict:
        task = self.built(desk, parent, title)
        self.round(task, "PASS")
        return pensieve.mark_awaiting_close(self.conn, task["id"], now=NOW)

    def go_task(self, title: str = "the ask") -> str:
        task_id = ids.new_id("task")
        task = pensieve.create_task(self.conn, "mcgonagall", title, intent_path=intent_file(task_id), task_id=task_id,
                                    now=NOW)
        pensieve.record_spec(self.conn, task["id"], "/private/tmp/checkout", "fix/site", "origin/main", "c" * 64,
                             now=NOW)
        return task["id"]

    def stopped(self, task: dict, step: str, why: str) -> None:
        """Auto-close stopped on the task at step, as its event and record say."""
        record = {**closer.fresh_record(task["id"]), "state": "stopped", "stopped": {"step": step, "merge_sha": None}}
        closer.write_record(record)
        pensieve.add_event(self.conn, task["desk"], "close.stopped", "headmaster",
                           f"auto-close stopped on task {task['id']} at {step}: {why}. Read the files",
                           task_id=task["id"], dedupe_key=f"close:stopped:{task['id']}:c0:pre:{step}", now=NOW)

    def auto_close_on(self) -> None:
        self.write_file(self.office / config.AUTO_CLOSE_FILE, "on\n")

    def proven(self, build: dict, parent: str = None) -> None:
        """The closer's proven close of a passed build, its go parent with it when given."""
        sha = pensieve.task_commits(self.conn, build["id"])[-1]["sha"]
        pensieve.close_proven(self.conn, build["id"], proof(pass_sha=sha, written_checks=0, judge_desk=None),
                              "closed on its proof", f"close:proven:{build['id']}", parent_task_id=parent, now=NOW)

    def status(self, task_id: str) -> str:
        return pensieve.get_task(self.conn, task_id)["status"]


class BulkCloseTests(CloseCase):
    def mixed(self) -> dict:
        """One task of each kind the rule names."""
        tasks = {"passed": self.passed(title="passed one"), "own": self.passed("ryan-claude-1", title="own passed")}
        tasks["dropped"] = self.passed(title="dropped one")
        self.stopped(tasks["dropped"], "landed", closer.PR_DROPPED)
        tasks["stopped"] = self.passed(title="red after merge")
        self.stopped(tasks["stopped"], "ci", "CI on the merge commit is red (1 of 1 checks read)")
        tasks["asked"] = self.passed(title="asked about")
        owlery.send(self.conn, "harry", "mcgonagall", "question", "which base?", body="main or next?",
                    task_id=tasks["asked"]["id"], now=NOW)
        tasks["in_review"] = self.built(title="in review")
        tasks["review_round"] = self.round(tasks["in_review"])
        tasks["changes"] = self.built(title="in its fix round")
        self.round(tasks["changes"], "CHANGES")
        tasks["live"] = self.built(title="a live build")
        tasks["queued"] = pensieve.create_task(self.conn, "harry", "not started", now=NOW)
        return tasks

    def test_a_mix_closes_only_the_closeable_and_names_every_refusal_with_its_reason(self):
        tasks = self.mixed()
        shown, _ = self.said(PHRASE)
        for key in ("passed", "own", "dropped"):
            self.assertEqual((self.status(tasks[key]["id"]),
                              pensieve.get_task(self.conn, tasks[key]["id"])["close_reason"]), ("closed", "complete"))
        self.assertIn(f"- {PHRASE} closed {tasks['passed']['id']} (harry), reviewed (PASS), as complete.", shown)
        self.assertIn(f"- {PHRASE} closed {tasks['dropped']['id']} (harry), dropped ({closer.PR_DROPPED}), as"
                      " complete.", shown)
        reviewer_task = tasks["review_round"]["task"]["id"]
        refused = {
            "stopped": "auto-close stopped it at ci, so its after-merge checks are not proven",
            "asked": f"harry asked a question on {tasks['asked']['id']} that has no answer yet",
            "in_review": "it awaits the verdict of review round 1",
            "changes": "review round 1 recorded CHANGES, so it waits for its fix round",
            "live": "it is a live build on harry that has not been reviewed yet",
            "queued": "it is queued for harry and has not started",
        }
        for key, reason in refused.items():
            task = tasks[key]
            self.assertIn(f"- {PHRASE} refused {task['id']} ({task['desk']}): {reason}", shown)
            self.assertNotEqual(self.status(task["id"]), "closed")
        self.assertIn(f"- {PHRASE} refused {reviewer_task} (hermione): it is hermione's review of"
                      f" {tasks['in_review']['id']}, waiting for its run.", shown)
        open_now = [task["id"] for task in pensieve.list_tasks(self.conn, open_only=True)]
        self.assertEqual(sorted(open_now), sorted([*(tasks[key]["id"] for key in refused), reviewer_task]))
        self.assertTrue(shown.startswith(f"{PHRASE}: 3 closed, {len(open_now)} refused;"))
        # Every refusal is followed by its Fix line; nothing is skipped.
        listed = [line for line in shown.splitlines() if line.startswith(f"- {PHRASE}")]
        self.assertEqual(len(listed), 3 + len(open_now))
        for index, line in enumerate(shown.splitlines()):
            if line.startswith(f"- {PHRASE} refused "):
                self.assertTrue(shown.splitlines()[index + 1].startswith("  Fix: "), line)

    def test_the_phrase_must_be_the_whole_message(self):
        task = self.passed()
        for text in ("mischief managed everything", f"{PHRASE}.", f"{PHRASE} please", f"please {PHRASE}",
                     f"{PHRASE}\nand tidy up", "Mischief managed  everything", f"`{PHRASE}`",
                     f"{PHRASE} {task['id']}"):
            with self.subTest(text=text):
                shown, _ = self.said(text)
                self.assertNotIn(PHRASE, shown)
                self.assertEqual(self.status(task["id"]), "awaiting_close")
        shown, _ = self.said(f"  {PHRASE}\n")
        self.assertIn(f"{PHRASE}: 1 closed, 0 refused", shown)
        self.assertEqual(self.status(task["id"]), "closed")

    def test_nothing_open_says_so(self):
        shown, _ = self.said(PHRASE)
        self.assertEqual(shown, f"{PHRASE}: no task is open, so nothing was closed.")

    def test_it_is_refused_whole_unless_ryan_typed_it(self):
        task = self.passed()
        shown, _ = self.said(PHRASE, agent_id="agent-1")
        self.assertIn(f"{PHRASE} was not applied: this hook could not confirm Ryan's own typing", shown)
        self.assertIn(f"Fix: type {PHRASE} as a message of its own", shown)
        self.assertEqual(self.status(task["id"]), "awaiting_close")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 0)

    def test_each_close_is_a_single_close_with_its_own_hook_token(self):
        tasks = [self.passed(title=f"passed {index}") for index in range(3)]
        self.said(PHRASE)
        minted = self.conn.execute("SELECT task_id, minted_by, consumed_at FROM close_tokens ORDER BY rowid").fetchall()
        self.assertEqual([tuple(row) for row in minted], [(task["id"], "hook", NOW) for task in tasks])

    def test_a_task_held_by_a_review_or_run_is_refused_and_left_alone(self):
        held, free = self.passed(title="held"), self.passed(title="free")
        with run_desk.task_lock(held["id"]):
            shown, _ = self.said(PHRASE)
        self.assertIn(f"{PHRASE} refused {held['id']} (harry): a review, a run or auto-close is working on it right"
                      " now.", shown)
        self.assertEqual((self.status(held["id"]), self.status(free["id"])), ("awaiting_close", "closed"))

    def test_a_task_that_goes_into_flight_mid_run_is_judged_again_under_its_lock_and_never_closed(self):
        first, second, third = (self.passed(title=name) for name in ("first", "second", "third"))
        real = hook.close_locked

        def close_locked(conn, task_id, now, settled=False):
            lines = real(conn, task_id, now, settled)
            if task_id == first["id"]:
                # While the bulk close works on the first, a desk asks about the second and a review takes the third.
                owlery.send(conn, "harry", "mcgonagall", "question", "one more thing", task_id=second["id"], now=NOW)
                self.held = run_desk.task_lock(third["id"])
                self.held.__enter__()
            return lines

        with mock.patch.object(hook, "close_locked", side_effect=close_locked):
            shown, _ = self.said(PHRASE)
        self.held.__exit__(None, None, None)
        self.assertEqual([self.status(task["id"]) for task in (first, second, third)],
                         ["closed", "awaiting_close", "awaiting_close"])
        self.assertIn(f"refused {second['id']} (harry): harry asked a question on {second['id']}", shown)
        self.assertIn(f"refused {third['id']} (harry): a review, a run or auto-close is working on it", shown)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM close_tokens").fetchone()[0], 1)

    def test_a_task_with_open_work_under_it_or_no_pass_is_refused(self):
        bare = pensieve.mark_awaiting_close(self.conn, self.built(title="marked by hand")["id"], now=NOW)
        top = self.passed(title="has open work")
        under = pensieve.create_task(self.conn, "hermione", "still going", parent_task_id=top["id"], now=NOW)
        shown, _ = self.said(PHRASE)
        self.assertIn(f"refused {bare['id']} (harry): it has no PASS on record.", shown)
        self.assertIn(f"refused {top['id']} (harry): open work is under it ({under['id']}).", shown)
        self.assertEqual((self.status(bare["id"]), self.status(top["id"])), ("awaiting_close", "awaiting_close"))

    def test_a_follow_up_or_a_pending_judge_keeps_a_task_open(self):
        judged = self.passed(title="judged")
        record = {**closer.fresh_record(judged["id"]), "merge_sha": SHA,
                  "judge": {"merge_sha": SHA, "try": 1, "owls": [], "run_id": "run-" + "b" * 16, "outcome": None,
                            "pack_sha256": "e" * 64}}
        closer.write_record(record)
        with mock.patch.object(closer, "followup_open", side_effect=lambda conn, task_id: task_id != judged["id"]):
            other = self.passed(title="following up")
            shown, _ = self.said(PHRASE)
        self.assertIn(f"refused {judged['id']} (harry): its after-merge judge's verdict is still awaited.", shown)
        self.assertIn(f"refused {other['id']} (harry): a PR follow-up is open on it", shown)

    def test_a_deferred_bulk_close_is_confirmed_and_names_each_task_in_its_own_event(self):
        closed, refused = self.passed(title="done"), self.built(title="still building")
        path = self.write_transcript([user_entry("earlier")], name="later.jsonl")
        shown, context = self.said(PHRASE, transcript=path)
        self.assertIn(f"{PHRASE} for every closeable task: Claude Code writes this prompt", shown)
        self.assertIn("castle task list --open", context)
        self.assertEqual(self.status(closed["id"]), "awaiting_close")
        payload = self.spawned_confirms.call_args[0][0]

        def appears(count: int) -> None:
            if count == 2:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(user_entry(PHRASE, promptId=PROMPT_ID)) + "\n")

        clock = Clock(appears)
        lines = go_confirm.confirm(payload, clock=clock, sleep=clock.sleep)
        self.assertEqual((self.status(closed["id"]), self.status(refused["id"])), ("closed", "active"))
        rows = [dict(row) for row in self.conn.execute(
            "SELECT kind, task_id, summary FROM events WHERE verdict = 'headmaster' ORDER BY id")]
        self.assertEqual([(row["kind"], row["task_id"]) for row in rows],
                         [("close.confirmed", closed["id"]), ("close.refused", refused["id"]),
                          ("close.confirmed", None)])
        self.assertTrue(rows[2]["summary"].startswith(f"{PHRASE}: 1 closed, 1 refused"))
        self.assertIn("it is a live build on harry", rows[1]["summary"])
        self.assertIn(f"Fix: let it finish, then type {PHRASE} again.", rows[1]["summary"])
        self.assertEqual(len(lines), 3)
        self.assertEqual(go_confirm.confirm(payload, clock=clock, sleep=clock.sleep),
                         ["refused: this prompt was confirmed already"])

    def test_a_deferred_bulk_close_records_each_outcome_before_the_next_close(self):
        first, second = self.passed(title="first"), self.passed(title="second")
        path = self.write_transcript([user_entry("earlier")], name="later.jsonl")
        self.said(PHRASE, transcript=path)
        payload = self.spawned_confirms.call_args[0][0]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(user_entry(PHRASE, promptId=PROMPT_ID)) + "\n")
        real = hook.close_locked

        def close_locked(conn, task_id, now, settled=False):
            if task_id == second["id"]:
                # The first task's event is in the store before the second close begins; then a signal comes.
                told = [row[0] for row in conn.execute("SELECT task_id FROM events WHERE kind = 'close.confirmed'")]
                self.assertEqual(told, [first["id"]])
                raise SystemExit(143)
            return real(conn, task_id, now, settled)

        clock = Clock()
        with mock.patch.object(hook, "close_locked", side_effect=close_locked), self.assertRaises(SystemExit):
            go_confirm.confirm(payload, clock=clock, sleep=clock.sleep)
        self.assertEqual((self.status(first["id"]), self.status(second["id"])), ("closed", "awaiting_close"))
        summaries = [row[0] for row in self.conn.execute("SELECT summary FROM events WHERE task_id IS NULL")]
        self.assertEqual(summaries, [f"{PHRASE} was stopped by a signal; the tasks it closed before that stay closed,"
                                     " so check castle task list --open before you type it again."])

    def test_open_work_or_a_question_that_lands_after_the_look_still_stops_the_close(self):
        first, second = self.passed(title="first"), self.passed(title="second")
        real = bulk_close._why_not

        def why_not(conn, task, now):
            found = real(conn, task, now)
            # After the look and before the close: a child under the first, a question on the second.
            if task["id"] == first["id"]:
                pensieve.create_task(conn, "hermione", "late child", parent_task_id=first["id"], now=NOW)
            if task["id"] == second["id"]:
                owlery.send(conn, "harry", "mcgonagall", "question", "late question", task_id=second["id"], now=NOW)
            return found

        with mock.patch.object(bulk_close, "_why_not", side_effect=why_not):
            shown, _ = self.said(PHRASE)
        self.assertEqual((self.status(first["id"]), self.status(second["id"])), ("awaiting_close", "awaiting_close"))
        self.assertIn(f"refused {first['id']} (harry): Mischief managed failed for {first['id']}: the task has open"
                      " work under it", shown)
        self.assertIn(f"refused {second['id']} (harry): Mischief managed failed for {second['id']}: a question about"
                      " the task waits for an answer", shown)
        late = [task for task in pensieve.list_tasks(self.conn) if task["title"] == "late child"]
        self.assertEqual(late[0]["status"], "queued")

    def test_a_merged_task_auto_close_has_not_proven_yet_is_refused(self):
        merged = self.passed(title="merged, checks pending")
        closer.write_record({**closer.fresh_record(merged["id"]), "merge_sha": SHA,
                             "landed": {"how": "pr", "pr": 7}, "landed_seen_at": NOW,
                             "waiting": {"what": "ci", "since": NOW}})
        shown, _ = self.said(PHRASE)
        self.assertIn(f"refused {merged['id']} (harry): it merged, and auto-close has not proven its after-merge"
                      " checks yet.", shown)
        self.assertEqual(self.status(merged["id"]), "awaiting_close")

    def test_an_interrupted_bulk_confirmation_says_what_to_check(self):
        lines = go_confirm.interrupted_lines("bulk", hook.BULK_KEY)
        self.assertEqual(lines, [f"The confirmation of {PHRASE} was interrupted, so some tasks may be closed and others"
                                 f" not; check castle task list --open and type {PHRASE} again."])


class ParentMovesOnTests(CloseCase):
    def setUp(self) -> None:
        super().setUp()
        self.auto_close_on()
        self.parent = self.go_task()
        self.first = self.passed(parent=self.parent, title="first build")
        self.second = self.passed(parent=self.parent, title="second build")

    def test_the_go_task_closes_only_once_its_last_build_has_on_the_proven_ones_proof(self):
        self.proven(self.first)
        self.assertEqual(closer.advance_parents(self.conn, NOW), [])
        self.assertEqual(self.status(self.parent), "queued")
        shown, _ = self.said(f"Mischief managed {self.second['id']}")
        self.assertIn(f"- moved on: its go task {self.parent} closed as complete on the proof of build"
                      f" {self.first['id']}", shown)
        parent = pensieve.get_task(self.conn, self.parent)
        self.assertEqual((parent["status"], parent["close_reason"]), ("closed", "complete"))
        closure = pensieve.task_closure(self.conn, self.parent)
        self.assertEqual((closure["kind"], closure["via_task_id"]), ("parent", self.first["id"]))
        [event] = [row for row in self.events() if row["kind"] == "close.proven" and self.parent in row["summary"]]
        self.assertIn(f"task {self.parent} closed as complete by auto-close: its last build closed", event["summary"])

    def test_with_no_proven_build_or_auto_close_off_it_stays_and_says_why(self):
        shown, _ = self.said(f"Mischief managed {self.first['id']}")
        self.assertNotIn("moved on", shown)
        shown, _ = self.said(PHRASE, prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc02")
        self.assertIn(f"refused {self.parent} (mcgonagall): its builds have all closed, but none by a proven close,"
                      " so no proof closes it.", shown)
        self.assertIn(f"Fix: castle task close {self.parent} --reason superseded", shown)
        self.assertEqual(self.status(self.parent), "queued")

    def test_auto_close_off_never_moves_it_on(self):
        self.proven(self.first)
        (self.office / config.AUTO_CLOSE_FILE).unlink()
        shown, _ = self.said(PHRASE)
        self.assertIn(f"closed {self.second['id']} (harry), reviewed (PASS)", shown)
        self.assertIn(f"refused {self.parent} (mcgonagall): auto-close is off, so no build's proof can close it.",
                      shown)
        self.assertEqual(self.status(self.parent), "queued")

    def test_the_bulk_close_moves_the_go_task_on_after_its_last_build(self):
        self.proven(self.first)
        shown, _ = self.said(PHRASE)
        self.assertEqual(self.status(self.parent), "closed")
        self.assertIn(f"{PHRASE} closed {self.parent} (mcgonagall): its go task moved on with its last build", shown)

    def test_a_go_task_waiting_on_an_open_build_is_refused_by_name(self):
        self.proven(self.first)
        shown, _ = self.said(PHRASE)
        self.assertNotIn("waits on its build", shown)  # its last build closed in the same run
        other = self.go_task("another ask")
        build = self.built(parent=other, title="still building")
        shown, _ = self.said(PHRASE, prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc03")
        self.assertIn(f"refused {other} (mcgonagall): its go task waits on its build {build['id']} (active).", shown)

    def test_a_question_on_the_go_task_a_held_lock_or_a_switch_turned_off_keeps_it_open(self):
        self.proven(self.first)
        pensieve.close_task(self.conn, self.second["id"], "abandoned", now=NOW)
        asked = owlery.send(self.conn, "harry", "mcgonagall", "question", "which base?", task_id=self.parent,
                            now=NOW)
        [kept] = closer.advance_parents(self.conn, NOW, only=self.parent)
        self.assertEqual(kept["outcome"], "kept")
        self.assertIn(f"harry asked a question on it that has no answer yet (owl {asked['id']})", kept["why"])
        self.assertEqual(closer.advance_parents(self.conn, NOW), [])
        owlery.read(self.conn, asked["id"], "mcgonagall", now=NOW)
        owlery.ack(self.conn, asked["id"], "mcgonagall", now=NOW)
        with run_desk.task_lock(self.parent):
            [kept] = closer.advance_parents(self.conn, NOW)
        self.assertEqual(kept["why"], "a review, a run or auto-close holds it right now")
        with mock.patch.object(closer, "auto_close_on", side_effect=[True, False]):
            [kept] = closer.advance_parents(self.conn, NOW)
        self.assertEqual(kept["why"], closer.AUTO_CLOSE_OFF)
        self.assertEqual(self.status(self.parent), "queued")
        [moved] = closer.advance_parents(self.conn, NOW)
        self.assertEqual(moved["outcome"], "closed")

    def test_the_last_build_closing_never_carries_a_go_task_that_has_a_question_open(self):
        self.proven(self.first)
        owlery.send(self.conn, "harry", "mcgonagall", "question", "which base?", task_id=self.parent, now=NOW)
        shown, _ = self.said(PHRASE)
        self.assertEqual((self.status(self.second["id"]), self.status(self.parent)), ("closed", "queued"))
        self.assertNotIn("moved on", shown)
        self.assertIn(f"refused {self.parent} (mcgonagall): harry asked a question on {self.parent}", shown)

    def test_the_closer_pass_and_the_map_sweep_move_it_on(self):
        self.proven(self.first)
        pensieve.close_task(self.conn, self.second["id"], "abandoned", now=NOW)
        with mock.patch.object(run_desk, "spawn_closer") as spawn:
            self.assertEqual(closer.sweep(self.conn, NOW), "started")
        spawn.assert_called_once_with()
        results = closer.run_pass(self.conn, NOW)
        self.assertIn({"parent": {"task_id": self.parent, "outcome": "closed", "via": self.first["id"]}}, results)
        self.assertEqual(self.status(self.parent), "closed")
        with mock.patch.object(run_desk, "spawn_closer") as spawn:
            self.assertEqual(closer.sweep(self.conn, NOW), "idle")


class CloseFixLineTests(CloseCase):
    def test_every_refused_close_says_exactly_what_to_type_next(self):
        active = self.built(title="building")
        closed = self.passed(title="done")
        pensieve.close_task(self.conn, closed["id"], "abandoned", now=NOW)
        cases = (
            (active["id"], f"Fix: type Mischief managed {active['id']} again once its review passes and it awaits"
                           f" close, or close it now from your terminal: castle token mint {active['id']}"),
            (closed["id"], f"Fix: nothing to type; it is closed already, and castle task show {closed['id']}"),
            ("tk_0000000000000000", "Fix: find the id with castle task list --open"),
        )
        for task_id, fix in cases:
            with self.subTest(task_id=task_id):
                shown, _ = self.said(f"Mischief managed {task_id}")
                self.assertIn(fix, shown)

    def test_a_close_while_its_review_lock_is_held_waits_and_says_so(self):
        task = self.passed()
        with run_desk.task_lock(task["id"]):
            shown, _ = self.said(f"Mischief managed {task['id']}")
        self.assertIn("a review, a run or auto-close is working on it right now.", shown)
        self.assertIn(f"Fix: type Mischief managed {task['id']} again once that ends", shown)
        self.assertEqual(self.status(task["id"]), "awaiting_close")
        shown, _ = self.said(f"Mischief managed {task['id']}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc04")
        self.assertIn(f"Mischief managed: task {task['id']} is closed as complete.", shown)

    def test_a_close_says_what_comes_next(self):
        bare = self.passed(title="no worktree")
        shown, _ = self.said(f"Mischief managed {bare['id']}")
        self.assertIn("Next: nothing more to type for it.", shown)
        kept = self.built(title="with a worktree")
        pensieve.set_worktree(self.conn, kept["id"], f"{ids.WORKTREES_ROOT}/{kept['id']}")
        self.round(kept, "PASS")
        pensieve.mark_awaiting_close(self.conn, kept["id"], now=NOW)
        shown, _ = self.said(f"Mischief managed {kept['id']}", prompt_id="5d0c9a3e-7777-4888-9999-0aaabbbccc05")
        self.assertIn(f"Next: fleet worktree-remove {kept['id']} removes its worktree", shown)

    def test_a_refusal_that_could_not_confirm_typing_leads_with_its_fix(self):
        task = self.passed()
        shown, _ = self.said(f"Mischief managed {task['id']}", agent_id="agent-1")
        lines = shown.splitlines()
        self.assertTrue(lines[0].startswith(f"Mischief managed was not applied to {task['id']}"))
        self.assertEqual(lines[1], f"Fix: close it from your terminal: castle token mint {task['id']}, then paste that"
                                   f" token into castle task close {task['id']} --reason complete --token-stdin.")

    def test_the_fix_line_survives_the_event_cap(self):
        lines = [f"Mischief managed was not applied to tk_0000000000000000: {'x' * 700}",
                 hook.close_fix("tk_0000000000000000", "active")]
        summary = go_confirm.fitted(lines)
        self.assertLessEqual(len(summary), 500)
        self.assertTrue(summary.endswith(lines[1]))
