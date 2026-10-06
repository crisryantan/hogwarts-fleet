"""PR follow-ups in the store: the PR binding, the one way back to active, follow-up states, items, the handled
ledger, the reply ledger, live periods and the review round groups. Every guard is checked through the API and
through raw SQL, which the triggers refuse just the same."""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hogwarts import capacity, cli, db, followups, ids, owlery, pensieve
from hogwarts.errors import ConflictError, IntegrityError, StoreError, ValidationError
from tests.support import NOW, REPO, SHA, StoreCase, worktree

SHA2 = "1" * 40
SHA3 = "2" * 40
PR = 7
URL = f"https://github.com/{REPO}/pull/{PR}"


class Killed(BaseException):
    """The process ends here: nothing after it runs, and no except Exception sees it."""


def comment_item(label: str = "T1", comment_id: str = "11", kind: str = "comment", quote=None,
                 number: int = PR) -> dict:
    fragment = {"thread": "#discussion_r", "review": "#pullrequestreview-", "comment": "#issuecomment-"}[kind]
    return {"label": label, "kind": kind, "thread_id": "PRRT_a1" if kind == "thread" else None, "reply_to": comment_id,
            "url": f"https://github.com/{REPO}/pull/{number}{fragment}{comment_id}", "quote": quote}


class FollowupStoreCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        for name, family in (("mcgonagall", "claude"), ("harry", "codex"), ("hermione", "claude"), ("map", "script"),
                             ("ryan", "human")):
            pensieve.add_desk(self.conn, name, family, now=NOW)
        for name in ("harry", "hermione"):
            pensieve.allow_many_tasks(self.conn, name, now=NOW)
        self.parent = pensieve.start_task(self.conn, pensieve.create_task(self.conn, "mcgonagall", "parent",
                                                                          now=NOW)["id"], now=NOW)["id"]
        self.task_id = self.build_task()

    def build_task(self, title: str = "build it") -> str:
        """A build task of harry's with a worktree, started and active."""
        opened = owlery.open_request(self.conn, "mcgonagall", "harry", title, parent_task_id=self.parent, now=NOW)
        task_id = opened["task"]["id"]
        pensieve.start_task(self.conn, task_id, now=NOW)
        pensieve.set_worktree(self.conn, task_id, worktree(task_id))
        return task_id

    def round(self, task_id: str, sha: str, verdict: str = None, followup_id: str = None, max_rounds: int = 3,
              followup_max_rounds: int = 2) -> dict:
        pensieve.record_commit(self.conn, task_id, REPO, sha)
        opened = capacity.open_review_round(self.conn, task_id, "hermione", sha, f"review {sha[:12]}",
                                            max_rounds=max_rounds, idempotency_key=ids.new_id("request"),
                                            followup_id=followup_id, followup_max_rounds=followup_max_rounds, now=NOW)
        reviewer_task = opened["task"]["id"]
        pensieve.start_task(self.conn, reviewer_task, now=NOW)
        if verdict is not None:
            capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO, verdict)
        return opened

    def passed(self, task_id: str = None, number: int = PR, sha: str = SHA, bind: bool = True) -> str:
        """The task passes review, awaits close and, with bind, has its PR bound."""
        task_id = task_id or self.task_id
        self.round(task_id, sha, "PASS")
        pensieve.mark_awaiting_close(self.conn, task_id, now=NOW)
        if bind:
            followups.bind_pr(self.conn, task_id, REPO, number, "fix/widget", "main", sha,
                              f"https://github.com/{REPO}/pull/{number}", now=NOW)
        return task_id

    def open_followup(self, task_id: str = None, followup_id: str = None, item_rows=None, comment_rows=None,
                      base_sha: str = SHA, max_per_task: int = 5) -> dict:
        task_id = task_id or self.task_id
        followup_id = followup_id or ids.new_id("followup")
        item_rows = item_rows if item_rows is not None else [comment_item()]
        comment_rows = comment_rows if comment_rows is not None else [
            {"kind": item["kind"], "comment_id": item["reply_to"], "label": item["label"]} for item in item_rows]
        return followups.open_followup(self.conn, task_id, followup_id, base_sha, item_rows, comment_rows, "map",
                                       "follow-up", "the fix request", max_per_task, now=NOW)

    def building(self, task_id: str = None) -> dict:
        row = self.open_followup(task_id)
        followups.advance(self.conn, row["id"], "starting", now=NOW)
        return followups.advance(self.conn, row["id"], "building", now=NOW)

    def status(self, task_id: str = None) -> str:
        return pensieve.get_task(self.conn, task_id or self.task_id)["status"]

    def raw(self, sql: str, params=()) -> None:
        self.conn.execute(sql, params)


class BindingTests(FollowupStoreCase):
    def test_a_pr_is_bound_once_to_a_passed_task_with_a_worktree_and_never_changes(self):
        with self.assertRaisesRegex(ConflictError, "awaiting close"):
            followups.bind_pr(self.conn, self.task_id, REPO, PR, "fix/widget", "main", SHA, URL, now=NOW)
        self.passed(bind=False)
        bound = followups.bind_pr(self.conn, self.task_id, REPO, PR, "fix/widget", "main", SHA, URL, now=NOW)
        self.assertTrue(bound["created"])
        again = followups.bind_pr(self.conn, self.task_id, REPO, PR, "fix/widget", "main", SHA, URL, now=NOW)
        self.assertFalse(again["created"])
        for change in ({"number": 8, "url": f"https://github.com/{REPO}/pull/8"}, {"branch": "fix/other"},
                       {"base": "dev"}, {"sha": SHA2}):
            values = {"repo": REPO, "number": PR, "branch": "fix/widget", "base": "main", "sha": SHA, "url": URL,
                      **change}
            with self.subTest(change=change), self.assertRaisesRegex(ConflictError, "already bound"):
                followups.bind_pr(self.conn, self.task_id, now=NOW, **values)
        for column, value in (("number", 8), ("branch", "fix/other"), ("url", "https://example.com"),
                              ("opened_sha", SHA2), ("repo", "acme/other")):
            with self.subTest(raw=column), self.assertRaises(sqlite3.IntegrityError):
                self.raw(f"UPDATE task_prs SET {column} = ? WHERE task_id = ?", (value, self.task_id))
        # The same PR, its repo spelled in another case, is still bound once.
        other = self.passed(self.build_task("other"), sha=SHA3, bind=False)
        with self.assertRaisesRegex(ConflictError, "another task"):
            followups.bind_pr(self.conn, other, REPO.upper(), PR, "fix/other", "main", SHA2,
                              f"https://github.com/{REPO.upper()}/pull/{PR}", now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("INSERT INTO task_prs(task_id, repo, number, branch, base, opened_sha, url, opened_at)"
                     " VALUES (?, ?, ?, 'fix/other', 'main', ?, ?, ?)",
                     (other, REPO.upper(), PR, SHA2, f"https://github.com/{REPO.upper()}/pull/{PR}", NOW))
        self.assertEqual(followups.pr_for_task(self.conn, self.task_id)["url"], URL)

    def test_a_pr_link_must_be_the_link_of_its_repo_and_number(self):
        self.passed(bind=False)
        for url in (f"https://github.com/{REPO}/pull/8", f"http://github.com/{REPO}/pull/7", URL + "/files",
                    f"https://github.com/acme/other/pull/7"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                followups.bind_pr(self.conn, self.task_id, REPO, PR, "fix/widget", "main", SHA, url, now=NOW)
            with self.subTest(raw=url), self.assertRaises(sqlite3.IntegrityError):
                self.raw("INSERT INTO task_prs(task_id, repo, number, branch, base, opened_sha, url, opened_at)"
                         " VALUES (?, ?, 7, 'fix/widget', 'main', ?, ?, ?)", (self.task_id, REPO, SHA, url, NOW))


class ReopenTests(FollowupStoreCase):
    def test_a_task_awaiting_close_goes_back_to_active_only_in_the_transaction_that_opens_a_followup(self):
        self.passed()
        row = self.open_followup()
        self.assertEqual(self.status(), "active")
        self.assertEqual(row["state"], "routing")
        self.assertIsNotNone(row["reopened_at"])
        self.assertEqual(owlery._owl(self.conn, row["owl_id"])["sender"], "map")

    def test_raw_sql_cannot_reopen_a_task_any_other_way(self):
        self.passed()
        for status in ("active", "queued"):
            with self.subTest(status=status), self.assertRaisesRegex(sqlite3.IntegrityError, "only to start a PR"):
                self.raw("UPDATE tasks SET status = ? WHERE id = ?", (status, self.task_id))
        with self.assertRaisesRegex(ConflictError, "only queued"):
            pensieve.start_task(self.conn, self.task_id, now=NOW)
        self.assertEqual(self.status(), "awaiting_close")

    def test_a_followup_reopens_its_task_only_once(self):
        self.passed()
        row = self.open_followup()
        # A raw move back, as a cut-off routing could leave things, then a second raw reopen: refused, since this
        # follow-up already reopened its task.
        self.raw("UPDATE pr_followups SET state = 'stopped', stop_reason = 'x', updated_at = ? WHERE id = ?",
                 (NOW, row["id"]))
        pensieve.mark_awaiting_close(self.conn, self.task_id, now=NOW)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only to start a PR"):
            self.raw("UPDATE tasks SET status = 'active' WHERE id = ?", (self.task_id,))
        # And with a routing row left reopened, as a kill after the opening transaction leaves it.
        other = self.passed(self.build_task("other"), number=8, sha=SHA3)
        routing = self.open_followup(other, item_rows=[{**comment_item(comment_id="12"),
                                                        "url": f"https://github.com/{REPO}/pull/8#issuecomment-12"}])
        self.assertEqual(followups.get(self.conn, routing["id"])["state"], "routing")
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (other,))
        self.assertEqual(self.status(other), "active")

    def test_a_followup_task_stays_active_until_a_round_of_it_passes(self):
        self.passed()
        row = self.building()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only after a round of that follow-up passes"):
            self.raw("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (self.task_id,))
        with self.assertRaises(IntegrityError):
            pensieve.mark_awaiting_close(self.conn, self.task_id, now=NOW)
        self.round(self.task_id, SHA2, "CHANGES", followup_id=row["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (self.task_id,))
        self.round(self.task_id, SHA3, "PASS", followup_id=row["id"])
        pensieve.mark_awaiting_close(self.conn, self.task_id, now=NOW)
        self.assertEqual(self.status(), "awaiting_close")


class FollowupStateTests(FollowupStoreCase):
    def test_a_task_has_at_most_one_open_followup_and_numbers_count_up(self):
        self.passed()
        first = self.open_followup()
        self.assertEqual(first["number"], 1)
        with self.assertRaisesRegex(ConflictError, "awaiting close"):
            self.open_followup(item_rows=[comment_item(comment_id="12")])
        followups.abandon_routing(self.conn, first["id"], "undone", now=NOW)
        second = self.open_followup(item_rows=[comment_item(comment_id="12")])
        self.assertEqual(second["number"], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("INSERT INTO pr_followups(id, task_id, number, state, base_sha, owl_id, created_at, updated_at)"
                     " VALUES ('fu_00000000000000aa', ?, 3, 'routing', ?, ?, ?, ?)",
                     (self.task_id, SHA, second["owl_id"], NOW, NOW))
        followups.abandon_routing(self.conn, second["id"], "undone", now=NOW)
        for limit in (2,):
            with self.assertRaisesRegex(ConflictError, "follow-ups"):
                self.open_followup(item_rows=[comment_item(comment_id="13")], max_per_task=limit)
        self.assertEqual(followups.count_for_task(self.conn, self.task_id), 2)

    def test_followup_states_move_only_along_their_edges_and_end_once(self):
        self.passed()
        row = self.open_followup()
        for bad in ("building", "pushing", "posting", "done"):
            with self.subTest(bad=bad), self.assertRaises(ConflictError):
                followups.advance(self.conn, row["id"], bad, now=NOW)
            with self.subTest(raw=bad), self.assertRaises(sqlite3.IntegrityError):
                self.raw("UPDATE pr_followups SET state = ?, pass_sha = ? WHERE id = ?", (bad, SHA, row["id"]))
        followups.advance(self.conn, row["id"], "starting", now=NOW)
        followups.advance(self.conn, row["id"], "building", now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):  # pushing needs the commit that passed
            self.raw("UPDATE pr_followups SET state = 'pushing' WHERE id = ?", (row["id"],))
        followups.plan_replies(self.conn, row["id"], SHA2, [{"label": "T1", "mark": "FIXED", "body": "Done, see x."}],
                               True, now=NOW)
        followups.advance(self.conn, row["id"], "posting", now=NOW)
        followups.advance(self.conn, row["id"], "done", now=NOW)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ended is final|only along its states"):
            self.raw("UPDATE pr_followups SET state = 'stopped', stop_reason = 'x' WHERE id = ?", (row["id"],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ended is final"):
            self.raw("UPDATE pr_followups SET updated_at = 1 WHERE id = ?", (row["id"],))
        self.assertEqual(followups.stop(self.conn, row["id"], "late", now=NOW)["state"], "done")
        for column, value in (("base_sha", SHA3), ("owl_id", "owl_0000000000000000"), ("number", 9),
                              ("task_id", self.parent)):
            with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                self.raw(f"UPDATE pr_followups SET {column} = ? WHERE id = ?", (value, row["id"]))

    def test_a_followup_owl_must_be_an_fyi_from_map_to_the_task_desk_about_that_task(self):
        self.passed()
        cases = (
            ("mcgonagall", "harry", "fyi", self.task_id),
            ("map", "hermione", "fyi", self.task_id),
            ("map", "harry", "question", self.task_id),
            ("map", "harry", "fyi", self.parent),
        )
        for index, (sender, recipient, kind, task_id) in enumerate(cases):
            owl = owlery.send(self.conn, sender, recipient, kind, f"x{index}", body="b", task_id=task_id, now=NOW)
            with self.subTest(sender=sender, recipient=recipient, kind=kind), \
                    self.assertRaisesRegex(sqlite3.IntegrityError, "fyi from map"):
                self.raw("INSERT INTO pr_followups(id, task_id, number, state, base_sha, owl_id, created_at,"
                         " updated_at) VALUES (?, ?, 1, 'routing', ?, ?, ?, ?)",
                         (f"fu_00000000000000{index:02d}", self.task_id, SHA, owl["id"], NOW, NOW))
        with self.assertRaises(StoreError):
            followups.open_followup(self.conn, self.task_id, ids.new_id("followup"), SHA, [comment_item()],
                                    [{"kind": "comment", "comment_id": "11", "label": "T1"}], "mcgonagall", "s", "b",
                                    5, now=NOW)
        self.assertEqual(self.status(), "awaiting_close")

    def test_open_followup_is_all_or_nothing(self):
        self.passed()
        owls = self.count("owls")
        with self.assertRaises(StoreError):
            self.open_followup(item_rows=[comment_item(), comment_item("T2", "12")],
                               comment_rows=[{"kind": "comment", "comment_id": "11", "label": "T1"},
                                             {"kind": "comment", "comment_id": "12", "label": "T2"},
                                             {"kind": "comment", "comment_id": "12", "label": "T1"}])
        # Killed as its last read runs, after the owl, the rows and the task's move were written.
        with mock.patch.object(followups, "get", side_effect=Killed("killed")), self.assertRaises(Killed):
            self.open_followup()
        self.assertEqual((self.count("pr_followups"), self.count("pr_followup_items"), self.count("pr_comments"),
                          self.count("owls")), (0, 0, 0, owls))
        self.assertEqual(self.status(), "awaiting_close")

    def test_abandon_routing_is_all_or_nothing(self):
        self.passed()
        row = self.open_followup()
        with mock.patch.object(pensieve, "add_event", side_effect=Killed("killed")), self.assertRaises(Killed):
            followups.abandon_routing(self.conn, row["id"], "undone", now=NOW,
                                      event=("followup.stopped", "headmaster", "undone", f"followup:stopped:{row['id']}"))
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.status()), ("routing", "active"))
        followups.abandon_routing(self.conn, row["id"], "undone", now=NOW,
                                  event=("followup.stopped", "headmaster", "undone", f"followup:stopped:{row['id']}"))
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.status()), ("stopped", "awaiting_close"))
        self.assertEqual(self.count("events"), 1)
        building = followups.open_followup(self.conn, self.task_id, ids.new_id("followup"), SHA,
                                           [comment_item(comment_id="12")],
                                           [{"kind": "comment", "comment_id": "12", "label": "T1"}], "map", "s", "b", 5,
                                           now=NOW)
        followups.advance(self.conn, building["id"], "starting", now=NOW)
        with self.assertRaisesRegex(ConflictError, "still routing"):
            followups.abandon_routing(self.conn, building["id"], "undone", now=NOW)

    def test_a_final_state_and_its_event_are_one_transaction(self):
        self.passed()
        row = self.building()
        event = ("followup.stopped", "headmaster", "it stopped", f"followup:stopped:{row['id']}")
        with mock.patch.object(pensieve, "add_event", side_effect=Killed("killed")), self.assertRaises(Killed):
            followups.stop(self.conn, row["id"], "it stopped", now=NOW, event=event)
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.count("events")), ("building", 0))
        followups.stop(self.conn, row["id"], "it stopped", now=NOW, event=event)
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.count("events")), ("stopped", 1))
        followups.stop(self.conn, row["id"], "it stopped", now=NOW, event=event)
        self.assertEqual(self.count("events"), 1)


class OpenForTests(FollowupStoreCase):
    def test_open_for_says_whether_a_task_has_a_followup_open(self):
        self.passed()
        self.assertFalse(followups.open_for(self.conn, self.task_id))
        row = self.open_followup()
        self.assertTrue(followups.open_for(self.conn, self.task_id))
        followups.abandon_routing(self.conn, row["id"], "undone", now=NOW)
        self.assertFalse(followups.open_for(self.conn, self.task_id))
        with mock.patch.object(db, "fetch_one", side_effect=StoreError("the store is gone")), \
                self.assertRaises(StoreError):
            followups.open_for(self.conn, self.task_id)
        with self.assertRaises(ValidationError):
            followups.open_for(self.conn, "x")


class ItemAndCommentTests(FollowupStoreCase):
    def test_items_are_written_only_while_routing_and_never_change(self):
        self.passed()
        row = self.building()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only while their follow-up is routing"):
            self.raw("INSERT INTO pr_followup_items(followup_id, label, kind, reply_to, url) VALUES (?, 'T2',"
                     " 'comment', '12', ?)", (row["id"], f"{URL}#issuecomment-12"))
        for column, value in (("url", f"{URL}#issuecomment-99"), ("quote", "x"), ("reply_to", "99")):
            with self.subTest(column=column), self.assertRaisesRegex(sqlite3.IntegrityError, "never change"):
                self.raw(f"UPDATE pr_followup_items SET {column} = ? WHERE followup_id = ?", (value, row["id"]))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            self.raw("DELETE FROM pr_followup_items WHERE followup_id = ?", (row["id"],))

    def test_an_item_links_only_to_its_own_comment_on_its_followups_pr(self):
        self.passed()
        for url in (f"https://github.com/acme/other/pull/7#issuecomment-11", f"{URL}#issuecomment-12",
                    f"{URL}#discussion_r11", f"{URL}#issuecomment-11 extra", f"https://evil.example/{REPO}/pull/7"):
            item = {**comment_item(), "url": url}
            with self.subTest(url=url), self.assertRaises(StoreError):
                self.open_followup(item_rows=[item])
        row = self.open_followup(item_rows=[{**comment_item(), "url": f"https://github.com/ACME/Web-App/pull/7"
                                                                      f"#issuecomment-11"}])
        self.assertEqual(len(followups.items(self.conn, row["id"])), 1)

    def test_a_comment_is_handled_once_and_only_with_an_item_of_a_routing_followup(self):
        self.passed()
        row = self.open_followup()
        self.assertEqual(followups.handled(self.conn, REPO.upper()), {("comment", "11")})
        followups.abandon_routing(self.conn, row["id"], "undone", now=NOW)
        with self.assertRaisesRegex(ConflictError, "already handled"):
            self.open_followup()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "routing follow-up"):
            self.raw("INSERT INTO pr_comments(repo, kind, comment_id, task_id, followup_id, label, recorded_at)"
                     " VALUES (?, 'comment', '99', ?, ?, 'T1', ?)", (REPO, self.task_id, row["id"], NOW))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never change"):
            self.raw("UPDATE pr_comments SET comment_id = '12'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            self.raw("DELETE FROM pr_comments")


class ReplyLedgerTests(FollowupStoreCase):
    def replies(self, labels=("T1",)) -> list:
        return [{"label": label, "mark": "PUSHBACK", "body": f"Not here, {label}."} for label in labels]

    def test_replies_are_planned_only_at_pass_and_begin_only_while_posting(self):
        self.passed()
        row = self.open_followup()
        with self.assertRaisesRegex(ConflictError, "while the follow-up is building"):
            followups.plan_replies(self.conn, row["id"], SHA, self.replies(), False, now=NOW)
        followups.advance(self.conn, row["id"], "starting", now=NOW)
        followups.advance(self.conn, row["id"], "building", now=NOW)
        for bad in ([], self.replies(("T1", "T2")), self.replies(("T2",))):
            with self.subTest(bad=bad), self.assertRaises(StoreError):
                followups.plan_replies(self.conn, row["id"], SHA, bad, False, now=NOW)
        for body in ("", "two\x00", "caf\u00e9", "x" * 1001):
            with self.subTest(body=body[:5]), self.assertRaises(ValidationError):
                followups.plan_replies(self.conn, row["id"], SHA, [{"label": "T1", "mark": "FIXED", "body": body}],
                                       False, now=NOW)
        with self.assertRaises(ValidationError):
            followups.plan_replies(self.conn, row["id"], SHA, [{"label": "T1", "mark": "HOLD", "body": "x"}], False,
                                   now=NOW)
        planned = followups.plan_replies(self.conn, row["id"], SHA, self.replies(), True, now=NOW)
        self.assertEqual((planned["state"], planned["pass_sha"]), ("pushing", SHA))
        with self.assertRaisesRegex(ConflictError, "follow-up that is posting"):
            followups.begin_reply(self.conn, row["id"], "T1", now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE pr_replies SET state = 'posting', begun_at = 1 WHERE followup_id = ?", (row["id"],))
        followups.advance(self.conn, row["id"], "posting", now=NOW)
        self.assertEqual(followups.begin_reply(self.conn, row["id"], "T1", now=NOW)["state"], "posting")
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("INSERT INTO pr_replies(followup_id, label, mark, body, state) VALUES (?, 'T1', 'FIXED', 'x',"
                     " 'planned')", (row["id"],))

    def posting(self, labels=("T1",), task_id: str = None, number: int = PR, sha: str = SHA) -> dict:
        """A follow-up of a passed task whose replies are planned and which is posting them."""
        task_id = self.passed(task_id, number=number, sha=sha)
        items = [comment_item(label, str(100 * number + index), number=number) for index, label in enumerate(labels, 1)]
        row = self.open_followup(task_id, item_rows=items, base_sha=sha)
        followups.advance(self.conn, row["id"], "starting", now=NOW)
        followups.advance(self.conn, row["id"], "building", now=NOW)
        return followups.plan_replies(self.conn, row["id"], sha, self.replies(labels), False, now=NOW)

    def reply_states(self, row: dict) -> dict:
        return {reply["label"]: (reply["state"], reply["posted_id"])
                for reply in followups.replies(self.conn, row["id"])}

    def test_a_reply_moves_planned_posting_then_posted_failed_or_unknown_and_never_back(self):
        row = self.posting(("T1", "T2"))
        with self.assertRaisesRegex(ConflictError, "being posted"):
            followups.end_reply(self.conn, row["id"], "T1", "posted", "501", now=NOW)
        followups.begin_reply(self.conn, row["id"], "T1", now=NOW)
        followups.end_reply(self.conn, row["id"], "T1", "posted", "501", now=NOW)
        followups.begin_reply(self.conn, row["id"], "T2", now=NOW)
        followups.stop(self.conn, row["id"], "refused", now=NOW, end_replies={"T2": ("failed", None)})
        other = self.posting(task_id=self.build_task("another"), number=PR + 1, sha=SHA2)
        followups.begin_reply(self.conn, other["id"], "T1", now=NOW)
        followups.stop(self.conn, other["id"], "unclear", now=NOW, end_replies={"T1": ("unknown", None)})
        self.assertEqual(self.reply_states(row), {"T1": ("posted", "501"), "T2": ("failed", None)})
        self.assertEqual(self.reply_states(other), {"T1": ("unknown", None)})
        for followup_id, label in ((row["id"], "T1"), (row["id"], "T2"), (other["id"], "T1")):
            for state in ("planned", "posting", "posted"):
                with self.subTest(label=label, state=state), self.assertRaises(sqlite3.IntegrityError):
                    self.raw("UPDATE pr_replies SET state = ? WHERE followup_id = ? AND label = ?",
                             (state, followup_id, label))
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE pr_replies SET body = 'other' WHERE followup_id = ?", (row["id"],))
        self.assertEqual(followups.posted_ids(self.conn, self.task_id), {"501"})

    def test_a_reply_ends_failed_or_unknown_only_in_the_transaction_that_stops_its_followup(self):
        row = self.posting()
        followups.begin_reply(self.conn, row["id"], "T1", now=NOW)
        for ending in ("failed", "unknown"):
            with self.subTest(ending=ending):
                with self.assertRaisesRegex(ValidationError, "only as its follow-up stops"):
                    followups.end_reply(self.conn, row["id"], "T1", ending, now=NOW)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.raw("UPDATE pr_replies SET state = ?, ended_at = 1 WHERE followup_id = ?", (ending, row["id"]))
        event = ("followup.stopped", "headmaster", "it stopped", f"followup:stopped:{row['id']}")
        with mock.patch.object(pensieve, "add_event", side_effect=Killed("killed")), self.assertRaises(Killed):
            followups.stop(self.conn, row["id"], "refused", now=NOW, event=event, end_replies={"T1": ("failed", None)})
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.reply_states(row), self.count("events")),
                         ("posting", {"T1": ("posting", None)}, 0))
        followups.stop(self.conn, row["id"], "refused", now=NOW, event=event, end_replies={"T1": ("failed", None)})
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.reply_states(row), self.count("events")),
                         ("stopped", {"T1": ("failed", None)}, 1))

    def test_no_reply_begins_while_one_begun_before_it_is_not_posted(self):
        row = self.posting(("T1", "T2"))
        followups.begin_reply(self.conn, row["id"], "T1", now=NOW)
        with self.assertRaisesRegex(ConflictError, "every reply begun before it is posted"):
            followups.begin_reply(self.conn, row["id"], "T2", now=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE pr_replies SET state = 'posting', begun_at = 1 WHERE followup_id = ? AND label = 'T2'",
                     (row["id"],))
        followups.end_reply(self.conn, row["id"], "T1", "posted", "501", now=NOW)
        self.assertEqual(followups.begin_reply(self.conn, row["id"], "T2", now=NOW)["state"], "posting")

    def test_a_github_id_another_reply_holds_is_never_taken_again(self):
        row = self.posting(("T1", "T2", "T3"))
        followups.begin_reply(self.conn, row["id"], "T1", now=NOW)
        followups.end_reply(self.conn, row["id"], "T1", "posted", "501", now=NOW)
        followups.begin_reply(self.conn, row["id"], "T2", now=NOW)
        with self.assertRaisesRegex(ConflictError, "another reply"):
            followups.end_reply(self.conn, row["id"], "T2", "posted", "501", now=NOW)
        # Taken as the reason a follow-up stops, the same id is refused too, and the whole stop with it.
        with self.assertRaisesRegex(ConflictError, "another reply"):
            followups.stop(self.conn, row["id"], "another login", now=NOW, end_replies={"T2": ("posted", "501")},
                           event=("followup.stopped", "headmaster", "x", f"followup:stopped:{row['id']}"))
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.count("events")), ("posting", 0))
        self.assertEqual(self.reply_states(row)["T2"], ("posting", None))


class RoundGroupTests(FollowupStoreCase):
    def test_a_round_names_only_an_open_building_followup_of_its_own_task_and_keeps_it(self):
        self.passed()
        row = self.building()
        other = self.build_task("other")
        with self.assertRaisesRegex(StoreError, "names that follow-up"):
            self.round(other, SHA3, followup_id=row["id"])
        opened = self.round(self.task_id, SHA2, followup_id=row["id"])
        self.assertEqual(capacity.request_round(self.conn, opened["request"]["id"])["followup_id"], row["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "keeps its follow-up"):
            self.raw("UPDATE review_rounds SET followup_id = NULL WHERE request_id = ?", (opened["request"]["id"],))

    def test_an_untagged_round_is_refused_while_a_followup_is_building(self):
        self.passed()
        row = self.open_followup()
        for state in ("routing", "starting"):
            with self.subTest(state=state), self.assertRaisesRegex(StoreError, "still starting"):
                self.round(self.task_id, SHA2)
            if state == "routing":
                followups.advance(self.conn, row["id"], "starting", now=NOW)
        with self.assertRaisesRegex(StoreError, "still starting"):
            self.round(self.task_id, SHA2, followup_id=row["id"])
        followups.advance(self.conn, row["id"], "building", now=NOW)
        with self.assertRaisesRegex(StoreError, "names that follow-up"):
            self.round(self.task_id, SHA2)

    def test_followup_rounds_count_only_against_their_followup_cap(self):
        self.passed()
        row = self.building()
        for sha in (SHA2, SHA3):
            self.round(self.task_id, sha, "CHANGES", followup_id=row["id"])
        self.assertTrue(capacity.needs_allowance(self.conn, self.task_id, 2, followup_id=row["id"]))
        self.assertFalse(capacity.needs_allowance(self.conn, self.task_id, 3))
        with self.assertRaises(capacity.RoundCapReached) as caught:
            self.round(self.task_id, "3" * 40, followup_id=row["id"])
        self.assertEqual((caught.exception.followup_number, caught.exception.max_rounds), (1, 2))
        self.assertIn("follow-up 1", str(caught.exception))

    def test_the_build_cap_never_counts_followup_rounds(self):
        self.round(self.task_id, SHA2, "CHANGES")
        self.round(self.task_id, SHA3, "CHANGES")
        self.passed(sha="4" * 40)
        row = self.building()
        opened = self.round(self.task_id, "5" * 40, "CHANGES", followup_id=row["id"])
        self.assertEqual(opened["round"], 4)  # the round number stays global
        held = [r for r in capacity.review_rounds(self.conn, self.task_id) if r["counts"]]
        self.assertEqual(len([r for r in held if r["followup_id"] is None]), 3)
        self.assertTrue(capacity.needs_allowance(self.conn, self.task_id, 3))
        self.assertFalse(capacity.needs_allowance(self.conn, self.task_id, 2, followup_id=row["id"]))

    def test_an_allowance_lifts_only_the_group_open_when_it_was_granted(self):
        early = capacity.allow_round(self.conn, self.task_id, now=NOW)
        self.assertEqual((early["followup_id"], early["lifts"]), (None, "the build's review rounds"))
        self.passed()
        row = self.building()
        for sha in (SHA2, SHA3):
            self.round(self.task_id, sha, "CHANGES", followup_id=row["id"])
        # The allowance granted before the follow-up opened belongs to the build, so the follow-up is capped.
        self.assertTrue(capacity.needs_allowance(self.conn, self.task_id, 2, followup_id=row["id"]))
        granted = capacity.allow_round(self.conn, self.task_id, now=NOW)
        self.assertEqual((granted["followup_id"], granted["lifts"]), (row["id"], "follow-up 1's review rounds"))
        self.assertFalse(capacity.needs_allowance(self.conn, self.task_id, 2, followup_id=row["id"]))
        opened = self.round(self.task_id, "6" * 40, "PASS", followup_id=row["id"])
        self.assertEqual(opened["allowance_id"], granted["id"])
        # It cannot be recorded for another group by raw SQL either.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "follow-up open when it is granted"):
            self.raw("INSERT INTO round_allowances(task_id, granted_at) VALUES (?, ?)", (self.task_id, NOW))
        pensieve.mark_awaiting_close(self.conn, self.task_id, now=NOW)
        followups.stop(self.conn, row["id"], "ended", now=NOW)
        # One granted during this follow-up never lifts the build's cap or a later follow-up's.
        later = self.open_followup(item_rows=[comment_item(comment_id="12")])
        followups.advance(self.conn, later["id"], "starting", now=NOW)
        followups.advance(self.conn, later["id"], "building", now=NOW)
        rows = [r for r in capacity.review_rounds(self.conn, self.task_id) if r["counts"]]
        self.assertEqual(capacity._unused_allowances(self.conn, self.task_id, rows, later["id"]), [])

    def test_in_flight_rows_carry_the_open_followup(self):
        self.passed()
        row = self.building()
        self.round(self.task_id, SHA2, "CHANGES", followup_id=row["id"])
        flight = capacity.in_flight(self.conn, now=NOW, max_rounds=3, followup_max_rounds=2)
        [task] = [task for desk in flight["desks"] for task in desk["tasks"] if task["id"] == self.task_id]
        self.assertEqual(task["followup"], {"number": 1, "state": "building", "pr": f"{REPO}#{PR}",
                                            "rounds_used": 1, "max_rounds": 2})
        self.assertEqual((task["rounds_used"], task["max_rounds"]), (1, 2))


class LivePeriodTests(FollowupStoreCase):
    def test_one_live_period_is_open_at_a_time_and_closes_once(self):
        self.assertIsNone(followups.see_live(self.conn, False, NOW))
        opened = followups.see_live(self.conn, True, NOW)
        self.assertEqual((opened["since"], opened["until"]), (NOW, None))
        self.assertEqual(followups.see_live(self.conn, True, NOW + 10)["id"], opened["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only while none is open"):
            self.raw("INSERT INTO followup_live(since) VALUES (?)", (NOW,))
        self.assertIsNone(followups.see_live(self.conn, False, NOW + 20))
        for sql in ("UPDATE followup_live SET until = 1", "UPDATE followup_live SET until = NULL",
                    "UPDATE followup_live SET since = 5", "DELETE FROM followup_live"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.raw(sql)
        self.assertEqual([(period["since"], period["until"]) for period in followups.live_periods(self.conn)],
                         [(NOW, NOW + 20)])

    def test_every_live_period_is_kept_for_the_qualify_rule(self):
        followups.see_live(self.conn, True, NOW)
        followups.see_live(self.conn, False, NOW + 100)
        followups.see_live(self.conn, True, NOW + 200)
        self.assertEqual([(period["since"], period["until"]) for period in followups.live_periods(self.conn)],
                         [(NOW, NOW + 100), (NOW + 200, None)])
        self.assertEqual(followups.current_live(self.conn)["since"], NOW + 200)


class FollowupSecurityTests(FollowupStoreCase):
    HOSTILE = ["fu_' OR 1=1 --", "x; DROP TABLE tasks", "", "FU_0123456789ABCDEF", "T1\n", "../../etc", None]

    def test_hostile_ids_labels_urls_and_text_are_refused_at_every_followup_entry_point(self):
        self.passed()
        row = self.building()
        calls = [
            lambda bad: followups.get(self.conn, bad),
            lambda bad: followups.by_owl(self.conn, bad),
            lambda bad: followups.open_for_task(self.conn, bad),
            lambda bad: followups.advance(self.conn, bad, "pushing", now=NOW),
            lambda bad: followups.advance(self.conn, row["id"], bad, now=NOW),
            lambda bad: followups.stop(self.conn, bad, "x", now=NOW),
            lambda bad: followups.stop(self.conn, row["id"], "x", now=NOW, end_replies={bad: ("failed", None)}),
            lambda bad: followups.stop(self.conn, row["id"], "x", now=NOW, end_replies={"T1": ("posted", bad)}),
            lambda bad: followups.stop(self.conn, row["id"], "x", now=NOW, end_replies={"T1": (bad, None)}),
            lambda bad: followups.begin_reply(self.conn, row["id"], bad, now=NOW),
            lambda bad: followups.end_reply(self.conn, row["id"], "T1", "posted", bad, now=NOW),
            lambda bad: followups.handled(self.conn, bad),
            lambda bad: followups.routed_thread_ids(self.conn, bad, 7),
            lambda bad: followups.bind_pr(self.conn, self.task_id, REPO, PR, bad, "main", SHA, URL, now=NOW),
            lambda bad: followups.plan_replies(self.conn, row["id"], SHA, [{"label": bad, "mark": "FIXED",
                                                                          "body": "x"}], False, now=NOW),
        ]
        for call in calls:
            for bad in self.HOSTILE:
                with self.subTest(call=call, bad=bad), self.assertRaises(StoreError):
                    call(bad)
        other = self.passed(self.build_task("other"), number=8, sha=SHA3)
        bad_items = (
            {**comment_item(), "label": "T0"}, {**comment_item(), "kind": "issue"},
            {**comment_item(), "reply_to": "011"}, {**comment_item(), "reply_to": "1" * 21},
            {**comment_item(), "quote": "caf\u00e9"}, {**comment_item(), "quote": "x" * 121},
            {**comment_item(kind="thread"), "thread_id": "bad id"}, {**comment_item(kind="thread"), "quote": "x"},
            {**comment_item(), "url": "javascript:alert(1)"},
        )
        for item in bad_items:
            with self.subTest(item=item), self.assertRaises(StoreError):
                self.open_followup(other, item_rows=[item])
        self.assertEqual(self.status(other), "awaiting_close")

    def test_no_followup_row_is_ever_deleted(self):
        self.passed()
        row = self.building()
        followups.see_live(self.conn, True, NOW)
        followups.plan_replies(self.conn, row["id"], SHA, [{"label": "T1", "mark": "PUSHBACK", "body": "No."}], False,
                               now=NOW)
        for table in ("task_prs", "followup_live", "pr_followups", "pr_followup_items", "pr_comments", "pr_replies"):
            with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
                self.raw(f"DELETE FROM {table}")


class FollowupCliTests(FollowupStoreCase):
    def cli(self, *argv) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), db_path=self.db_path)
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())["data"]

    def test_castle_followup_show_and_list_only_read(self):
        self.passed()
        row = self.building()
        before = self.conn.total_changes
        shown = self.cli("followup", "show", self.task_id)
        self.assertEqual(shown["pr"]["url"], URL)
        [followup] = shown["followups"]
        self.assertEqual((followup["id"], followup["state"], followup["comments"]),
                         (row["id"], "building", 1))
        self.assertEqual([item["label"] for item in followup["items"]], ["T1"])
        listed = self.cli("followup", "list", "--task", self.task_id)
        self.assertEqual([item["id"] for item in listed], [row["id"]])
        self.assertEqual(len(self.cli("followup", "list")), 1)
        changes = sqlite3.connect(str(self.db_path))
        self.addCleanup(changes.close)
        self.assertEqual(self.conn.total_changes, before)
        parser = cli.build_parser()
        actions = {action.dest: action for action in parser._subparsers._group_actions[0].choices["followup"]
                   ._subparsers._group_actions}
        self.assertEqual(set(actions["action"].choices), {"list", "show"})

    def test_allow_round_says_which_group_it_lifts(self):
        self.passed()
        self.building()
        granted = self.cli("task", "allow-round", self.task_id)
        self.assertEqual(granted["lifts"], "follow-up 1's review rounds")
