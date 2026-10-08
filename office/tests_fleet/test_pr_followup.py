"""PR follow-ups, the Map side: the switch, the binding, what qualifies, routing, the build desk's run, a routing cut off
by a kill, housekeeping, the bot pass and the round's rows, the lineup and the endings only the store can finish.

Runs on real git repos in temp folders, as the review loop's tests do: origin keeps its GitHub URL and pushes go to a
local bare repo. GitHub is faked at patrol.run_gh (every read, the follow-up query included) and gitops.run_gh_write and
gitops.run_gh_pr (every write), so nothing reaches the network and no gh process starts. Harry's runs are faked at
run_desk.spawn, Hermione's at run_desk.run, and the patrol's desk wakes at patrol.wake.
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from hogwarts import capacity, followups, pensieve
from hogwarts.errors import StoreError

from fleet import closer, common, config, followup, gitops, map as patrol_map, owl_post, patrol, portrait_auto, push, \
    review, run_desk, worktree
from fleet.safefs import FleetError
from tests_fleet.test_auto_push import AutoPushCase, TOKEN, EMAIL
from tests_fleet.test_patrol import FakeGitHub, iso, pr_node, thread
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import REPO_ID

ACCOUNT = "octo"
NUMBER = 7
PR_KEY = f"{REPO_ID}#{NUMBER}"
PR_LINK = f"https://github.com/{REPO_ID}/pull/{NUMBER}"
SETTLE = config.FOLLOWUP_SETTLE_SECONDS


def link(kind: str, comment_id: str, repo: str = REPO_ID, number: int = NUMBER) -> str:
    return f"https://github.com/{repo}/pull/{number}{followup.FRAGMENTS[kind]}{comment_id}"


def gh_comment(comment_id, at: int, login: str = "alice", association: str = "MEMBER", body: str = "Please rename it.",
               kind: str = "comment", typename: str = "User", url: str = None, state: str = "COMMENTED",
               hunk: str = "@@ -1 +1 @@\n-widget\n+widget 2") -> dict:
    """A comment, review or review-thread comment node as the follow-up query returns it."""
    node = {"fullDatabaseId": None if comment_id is None else str(comment_id),
            "author": None if typename is None else {"__typename": typename, "login": login},
            "authorAssociation": association, "body": body,
            "url": url if url is not None else link(kind, str(comment_id))}
    if kind == "review":
        node.update({"state": state, "submittedAt": iso(at)})
    else:
        node["createdAt"] = iso(at)
    if kind == "thread":
        node["diffHunk"] = hunk
    return node


def gh_thread(thread_id: str, comments: list, resolved: bool = False, path: str = "widget.txt", line=1) -> dict:
    return {"id": thread_id, "isResolved": resolved, "isOutdated": False, "path": path, "line": line,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": comments}}


class FollowupGitHub(FakeGitHub):
    """The patrol's fake GitHub, also answering the follow-up query for each PR from its own data. head is a function
    that reads the PR's head the way GitHub would: the tip of its branch on the remote."""

    def __init__(self, head) -> None:
        super().__init__()
        self.head = head
        self.viewer = ACCOUNT
        self.author = ACCOUNT
        self.state = "OPEN"
        self.head_repo = REPO_ID
        self.head_ref = "fix/widget"
        self.pr_threads, self.pr_reviews, self.pr_comments = [], [], []
        self.more = {}
        self.fail = None
        self.down = False
        self.followup_reads = 0

    def __call__(self, argv: list) -> bytes:
        if self.down:  # offline, or gh no longer signed in: every read fails
            raise FleetError("gh could not reach GitHub")
        query = argv[4][len("query="):]
        name = next(key for key, text in patrol.QUERIES.items() if text == query)
        if name != "followup":
            return super().__call__(argv)
        variables = dict(field.split("=", 1) for field in argv[6::2])
        self.calls.append((name, variables))
        self.followup_reads += 1
        if self.fail == "gh":
            raise FleetError("gh failed: something broke")
        if self.fail == "graphql":
            return json.dumps({"errors": [{"message": "no"}], "data": None}).encode()

        def page(name: str, nodes: list) -> dict:
            return {"pageInfo": {"hasNextPage": self.more.get(name, False)}, "nodes": nodes}

        pull = {"number": NUMBER, "state": self.state, "url": PR_LINK, "headRefName": self.head_ref,
                "headRefOid": self.head(), "baseRefName": "main", "author": {"login": self.author},
                "headRepository": {"nameWithOwner": self.head_repo},
                "reviewThreads": page("reviewThreads", self.pr_threads), "reviews": page("reviews", self.pr_reviews),
                "comments": page("comments", self.pr_comments)}
        return json.dumps({"data": {"viewer": {"login": self.viewer}, "repository": {"pullRequest": pull}}}).encode()


class FollowupCase(AutoPushCase):
    """A build task whose PASS opened draft PR #7, bound to it, with follow-ups switched on and the patrol live."""

    def setUp(self) -> None:
        super().setUp()
        self.github = FollowupGitHub(lambda: self.git("rev-parse", "refs/heads/fix/widget", cwd=self.bare))
        self.writes = []
        self.next_id = 9000
        self.write_answer = None
        self.wakes = []
        for patcher in (mock.patch.object(config, "GITHUB_ACCOUNT", ACCOUNT),
                        mock.patch.object(config, "WATCHED_REPOS", ("<repos-to-watch>",)),
                        mock.patch.object(patrol, "run_gh", side_effect=self.github),
                        mock.patch.object(patrol_map, "lineup_due", return_value=False),
                        mock.patch.object(patrol, "wake", side_effect=self.fake_wake),
                        mock.patch.object(gitops, "run_gh_write", side_effect=self.fake_write)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.office / "patrol").mkdir(mode=0o700)
        self.opt_in()
        self.first = self.passed()
        self.base_sha = self.first["review"]["sha"]
        self.binding = followups.pr_for_task(self.conn, self.task["id"])
        self.t0 = self.binding["opened_at"]
        self.github.prs = [self.pr_node()]
        self.switch_on()
        self.before = self.headmaster_events()

    # switches and GitHub

    def switch_on(self, text: str = "on\n") -> None:
        self.write_file(self.office / config.PR_FOLLOWUP_FILE, text)

    def switch_off(self) -> None:
        os.unlink(self.office / config.PR_FOLLOWUP_FILE)

    def shadow(self) -> None:
        self.write_file(self.office / "patrol" / "shadow", "shadow mode\n")

    def fake_wake(self, conn, desk, job, kind, data, out_name, now=None, **kwargs):
        self.wakes.append((desk, job, data, kwargs))
        return {"launched": False, "clean": False}

    def fake_write(self, argv, body):
        kind = gitops.check_write_argv(argv)
        self.writes.append((argv, json.loads(body.decode("ascii"))["body"]))
        if self.write_answer is not None:
            answer = self.write_answer(argv, body)
            if answer is not None:
                return answer
        self.next_id += 1
        fragment = "discussion_r" if kind == "reply" else "issuecomment-"
        posted = {"id": self.next_id, "html_url": f"{PR_LINK}#{fragment}{self.next_id}", "user": {"login": ACCOUNT}}
        self.land(kind, argv, self.next_id, json.loads(body.decode("ascii"))["body"])
        return 0, json.dumps(posted), ""

    def land(self, kind: str, argv: list, comment_id: int, text: str, at: int = None, login: str = ACCOUNT) -> None:
        """A comment that went out shows up on the fake PR, as GitHub would show it."""
        node = gh_comment(comment_id, at or int(time.time()), login=login, association="OWNER", body=text,
                          kind="thread" if kind == "reply" else "comment")
        if kind == "reply":
            target = argv[4].split("/comments/")[1].split("/")[0]
            for entry in self.github.pr_threads:
                if entry["comments"]["nodes"][0]["fullDatabaseId"] == target:
                    entry["comments"]["nodes"].append(node)
        else:
            self.github.pr_comments.append(node)

    def pr_node(self, threads: tuple = ()) -> dict:
        return pr_node(number=NUMBER, repo=REPO_ID, created=self.t0, head=self.github.head(), threads=threads)

    def sync_prs(self) -> None:
        """The Map's own list of open PRs, with the person threads the fake PR now has."""
        threads = tuple(thread(entry["id"], by=entry["comments"]["nodes"][0]["author"]["login"])
                        for entry in self.github.pr_threads if not entry["isResolved"])
        self.github.prs = [self.pr_node(threads)]

    def add_thread(self, thread_id: str = "PRRT_t1", comments: list = None, at: int = None) -> dict:
        at = at or self.t0 + 100
        entry = gh_thread(thread_id, comments or [gh_comment(501, at, kind="thread")])
        self.github.pr_threads.append(entry)
        self.sync_prs()
        return entry

    def add_review(self, review_id: int = 601, at: int = None, **fields) -> dict:
        node = gh_comment(review_id, at or self.t0 + 100, kind="review", **fields)
        self.github.pr_reviews.append(node)
        return node

    def add_comment(self, comment_id: int = 701, at: int = None, **fields) -> dict:
        node = gh_comment(comment_id, at or self.t0 + 100, kind="comment", **fields)
        self.github.pr_comments.append(node)
        return node

    # rounds

    def map_round(self, at: int) -> dict:
        self.clock = max(getattr(self, "clock", 0), at)
        return patrol_map.run_round(self.conn, now=at)

    def go_live_at(self, at: int = None) -> None:
        """The Map's first round once follow-ups are on: a baseline that opens the live period."""
        self.map_round(at or self.t0 + 50)

    def routed(self, at: int = None) -> dict:
        """A Map round late enough for every comment so far to have settled; the open follow-up after it."""
        self.map_round(at or self.t0 + 100 + SETTLE + 10)
        return followups.open_for_task(self.conn, self.task["id"])

    def followup_owl_runs(self) -> list:
        return [call for call in self.harry_runs.call_args_list if call.args[1] != self.request_owl]

    def threads_file(self, number: int = 1) -> str:
        holder = self.parent
        return (self.castle / "tasks" / holder / f"followup-{number}.md").read_text()

    def hand_off(self, row: dict, rows: str, change: str = "widget renamed", verdict: str = "PASS",
                 round_no: int = 2) -> dict:
        """Harry's follow-up handoff, posted after the round that routed it, and its automatic review."""
        self.clock += 10
        with self.fake_reviewer(verdict):
            return self.post(round_no, change, body=self.handoff(row, rows, round_no), now=self.clock)

    def handoff(self, row: dict, rows: str, round_no: int = 2) -> str:
        return (f"HANDOFF {self.task['id']} round {round_no}\nCHANGED\n- widget.txt | renamed\n"
                f"THREADS ({row['id']})\n{rows}\nCOMMIT MESSAGE\nRename the widget\n\nIt renames the widget.\n"
                "PR BODY DRAFT\nunchanged\nCHECKPOINT\ndone\n")

    def followup_events(self) -> list:
        return [event for event in self.events() if event["kind"].startswith("followup.")]

    def status(self) -> str:
        return pensieve.get_task(self.conn, self.task["id"])["status"]


class SmokeTests(FollowupCase):
    def test_a_teammate_comment_goes_round_the_loop_and_is_answered_once(self):
        self.go_live_at()
        self.add_thread()
        row = self.routed()
        self.assertEqual(row["state"], "building")
        self.assertEqual(self.status(), "active")
        [run] = self.followup_owl_runs()
        self.assertEqual(run.args[:2], ("harry", row["owl_id"]))
        self.assertIn("> Please rename it.", self.threads_file())
        self.hand_off(row, "T1 | FIXED | Good catch, renamed it, see {sha}.")
        row = followups.get(self.conn, row["id"])
        self.assertEqual(row["state"], "done", self.followup_events())
        head = self.head()
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {head}")
        [(argv, text)] = self.writes
        self.assertEqual(argv[4], f"repos/{REPO_ID}/pulls/{NUMBER}/comments/501/replies")
        self.assertEqual(text, f"Good catch, renamed it, see {head[:7]}.")
        [done] = [event for event in self.followup_events() if event["kind"] == "followup.done"]
        self.assertEqual(done["verdict"], "headmaster")
        self.assertIn(PR_LINK, done["summary"])
        self.assertIn(head[:12], done["summary"])


class SwitchTests(FollowupCase):
    def test_followup_on_only_while_the_office_file_holds_exactly_on(self):
        path = self.office / config.PR_FOLLOWUP_FILE
        for text, on in (("on\n", True), (" on \n", True), ("", False), ("yes\n", False), ("ON\n", False),
                         ("on\non\n", False)):
            with self.subTest(text=text):
                self.switch_on(text)
                self.assertEqual(followup.switched_on(), on)
        self.switch_on()
        os.chmod(path, 0o620)
        self.assertFalse(followup.switched_on())
        os.unlink(path)
        target = self.write_file(self.tmp / "elsewhere", "on\n")
        os.symlink(target, path)
        self.assertFalse(followup.switched_on())
        os.unlink(path)
        os.link(target, path)
        self.assertFalse(followup.switched_on())

    def test_an_opt_in_anywhere_else_is_ignored(self):
        self.switch_off()
        for folder in (self.castle / "desks" / "harry", self.castle / "desks" / "mcgonagall",
                       self.castle / "tasks" / self.parent, self.office / "desks" / "harry"):
            self.write_file(folder / config.PR_FOLLOWUP_FILE, "on\n")
        self.write_file(self.castle / "standing-orders.md", "# Standing orders\n\npr-followup: on\n")
        self.write_owl("mcgonagall", "order.json", {"to": "harry", "kind": "fyi", "subject": "pr-followup on",
                                                    "body": "pr-followup on", "task_id": self.task["id"]})
        owl_post.run_pass(self.conn)
        self.assertFalse(followup.switched_on())
        self.assertFalse(followup.live())

    def test_all_switches_read_through_one_reader(self):
        for value in (True, False):
            with self.subTest(value=value), mock.patch.object(common, "opt_in_on", return_value=value) as reader:
                self.assertEqual((followup.switched_on(), push.auto_draft_pr_on(), portrait_auto.auto_portrait_on(),
                                  closer.auto_close_on(), worktree.cleanup_on()), (value,) * 5)
                self.assertEqual([call.args[0] for call in reader.call_args_list],
                                 [config.PR_FOLLOWUP_FILE, config.AUTO_DRAFT_PR_FILE, config.AUTO_PORTRAIT_FILE,
                                  config.AUTO_CLOSE_FILE, config.WORKTREE_CLEANUP_FILE])
        self.write_file(self.office / "other-switch", "on\n")
        self.assertFalse(common.opt_in_on("other-switch"))
        self.assertEqual(config.OPT_IN_FILES, (config.AUTO_DRAFT_PR_FILE, config.AUTO_PORTRAIT_FILE,
                                               config.PR_FOLLOWUP_FILE, config.AUTO_CLOSE_FILE,
                                               config.WORKTREE_CLEANUP_FILE, config.OWL_REPORTS_FILE,
                                               config.CROSS_FAMILY_FAILOVER_FILE))

    def test_opt_in_off_routes_nothing_and_writes_nothing(self):
        self.switch_off()
        self.add_thread()
        owls = self.count_rows("owls")
        for at in (self.t0 + 50, self.t0 + 500, self.t0 + 900):
            self.map_round(at)
        for table in ("pr_followups", "followup_live", "pr_comments"):
            self.assertEqual(self.count_rows(table), 0, table)
        self.assertEqual(self.count_rows("owls"), owls)
        self.assertEqual(self.followup_owl_runs(), [])
        self.assertEqual(self.followup_events(), [])
        self.assertFalse((self.office / "patrol" / "followup").exists())
        self.assertEqual([name for name, _ in self.github.calls if name == "followup"], [])
        self.assertEqual(self.status(), "awaiting_close")

    def test_shadow_mode_routes_nothing_and_writes_only_the_dry_run_file(self):
        self.go_live_at()
        self.shadow()
        self.add_thread(comments=[gh_comment(501, self.t0 + 100, kind="thread", body=f"Rename it. {TOKEN} {EMAIL}")])
        self.add_comment()
        # A block that would fire live, and Harry at his cap: neither is told, and no cap hit is written.
        events, hits = self.count_rows("events"), self.count_rows("cap_hits")
        with mock.patch.object(run_desk, "cap_status", return_value={"reached": "runs"}):
            self.map_round(self.t0 + 500)
        self.assertEqual((self.count_rows("events"), self.count_rows("cap_hits")), (events, hits))
        self.assertEqual(self.count_rows("pr_followups"), 0)
        self.assertEqual([(period["since"], period["until"]) for period in followups.live_periods(self.conn)],
                         [(self.t0 + 50, self.t0 + 500)])
        [name] = os.listdir(self.office / "patrol" / "followup")
        text = (self.office / "patrol" / "followup" / name).read_text()
        self.assertIn(PR_KEY, text)
        self.assertIn("T1, T2", text)
        self.assertIn("desk-capped", text)
        for secret in ("Rename it", TOKEN, EMAIL, "Please rename"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.followup_owl_runs(), [])

    def test_a_copy_kept_in_shadow_mode_from_the_start_still_writes_what_it_would_route(self):
        self.shadow()
        self.map_round(self.t0 + 50)  # the first round: a baseline
        self.add_thread()
        self.add_comment(701, at=self.t0 + 120)
        self.add_comment(702, at=self.t0 - config.FOLLOWUP_SHADOW_WINDOW_SECONDS)  # before the PR opened: never
        events = self.count_rows("events")
        self.map_round(self.t0 + 120 + SETTLE + 10)
        [name] = os.listdir(self.office / "patrol" / "followup")
        text = (self.office / "patrol" / "followup" / name).read_text()
        self.assertIn(f"| {PR_KEY} | {self.task['id']} | route | T1, T2 | 2 | - |", text)
        self.assertIn("as if follow-ups had been live then", text)
        # The window is simulated: no live period opened, nothing routed, recorded or told.
        for table in ("followup_live", "pr_followups", "pr_comments"):
            self.assertEqual(self.count_rows(table), 0, table)
        self.assertEqual(self.count_rows("events"), events)
        self.assertEqual(self.status(), "awaiting_close")
        self.assertEqual(self.followup_owl_runs(), [])
        # Going live routes only what is written while live: the comments the dry run counted stay unrouted.
        os.unlink(self.office / "patrol" / "shadow")
        self.map_round(self.t0 + 1000)
        self.map_round(self.t0 + 1000 + SETTLE + 10)
        self.assertIsNone(followups.open_for_task(self.conn, self.task["id"]))

    def test_a_switch_change_opens_and_closes_one_live_period(self):
        self.go_live_at(self.t0 + 10)
        self.map_round(self.t0 + 20)
        self.switch_off()
        self.map_round(self.t0 + 30)
        self.switch_on()
        self.shadow()
        self.map_round(self.t0 + 40)
        os.unlink(self.office / "patrol" / "shadow")
        self.map_round(self.t0 + 50)
        self.assertEqual([(period["since"], period["until"]) for period in followups.live_periods(self.conn)],
                         [(self.t0 + 10, self.t0 + 30), (self.t0 + 50, None)])


class BindingTests(FollowupCase):
    def test_the_draft_pr_step_binds_the_pr_to_its_task_branch_and_number(self):
        self.assertEqual({key: self.binding[key] for key in ("task_id", "repo", "number", "branch", "base",
                                                             "opened_sha", "url")},
                         {"task_id": self.task["id"], "repo": REPO_ID, "number": NUMBER, "branch": "fix/widget",
                          "base": "main", "opened_sha": self.base_sha, "url": PR_LINK})

    def test_a_bound_task_never_opens_a_second_pr(self):
        self.go_live_at()
        self.add_thread()
        row = self.routed()
        followups.stop(self.conn, row["id"], "stopped by hand")
        self.gh_calls.clear()
        self.clock += 10
        with self.fake_reviewer("PASS"):
            self.post(3, "widget again", now=self.clock)
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.base_sha}")
        [event] = [event for event in self.new_events() if event["kind"] == "review.ready-for-push"]
        self.assertIn(f"PR #{NUMBER} is already open, so nothing was pushed", event["summary"])
        self.assertEqual(self.writes, [])

    def test_fleet_push_on_a_bound_task_names_the_open_pr(self):
        pushed = push.push(self.conn, self.task["id"], confirm=lambda text: "fix/widget\n")
        self.assertEqual(pushed["pr"], PR_LINK)
        self.assertNotIn("draft_pr_command", pushed)

    def test_only_prs_the_loop_opened_are_followed(self):
        self.go_live_at()
        other = pr_node(number=8, repo=REPO_ID, created=self.t0, threads=(thread("PRRT_other", by="alice"),))
        self.github.prs = [self.pr_node(), other]
        own = pensieve.start_task(self.conn, pensieve.create_task(self.conn, "ryan-claude-1", "own work")["id"])
        self.assertIsNone(followups.pr_for_task(self.conn, own["id"]))
        self.map_round(self.t0 + 500)
        read = {variables["number"] for name, variables in self.github.calls if name == "followup"}
        self.assertEqual(read, {str(NUMBER)})


class DraftPrBindingTests(AutoPushCase):
    def test_a_binding_that_fails_is_told_in_the_one_draft_pr_event(self):
        self.opt_in()
        with mock.patch.object(followups, "bind_pr", side_effect=StoreError("the store said no")):
            ran = self.passed()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.draft-pr")
        self.assertTrue(event["summary"].endswith("; it could not be recorded for teammate follow-ups"))
        self.assertIsNone(followups.pr_for_task(self.conn, self.task["id"]))
        self.assertTrue(ran["next"].startswith("opened draft PR"))


def count_rows(case, table: str) -> int:
    return case.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


FollowupCase.count_rows = count_rows


class QualifyTests(FollowupCase):
    def plan(self) -> list:
        return followup._plan(self.conn, self.binding, self.task, followup.read_pr(self.binding))

    def test_only_write_access_people_other_than_the_author_and_never_bots_are_routed(self):
        self.go_live_at()
        people = [("alice", "OWNER", "User"), ("bob", "MEMBER", "User"), ("carol", "COLLABORATOR", "User")]
        others = [("dave", "CONTRIBUTOR", "User"), ("erin", "NONE", "User"), ("lint-bot", "MEMBER", "Bot"),
                  ("ghost", "MEMBER", "Mannequin"), (ACCOUNT, "OWNER", "User"), (ACCOUNT.upper(), "OWNER", "User")]
        for index, (login, association, typename) in enumerate(people + others):
            self.add_comment(800 + index, login=login, association=association, typename=typename)
        self.add_comment(899, typename=None)
        self.assertEqual([item["reply_to"] for item in self.plan()], ["800", "801", "802"])

    def test_review_bodies_and_conversation_comments_are_routed_as_their_own_items(self):
        self.go_live_at()
        self.add_thread()
        self.add_review(601, at=self.t0 + 110, state="CHANGES_REQUESTED", body="Split this function please.")
        self.add_comment(701, at=self.t0 + 120)
        row = self.routed(self.t0 + 120 + SETTLE + 10)
        items = followups.items(self.conn, row["id"])
        self.assertEqual([(item["label"], item["kind"], item["reply_to"]) for item in items],
                         [("T1", "thread", "501"), ("T2", "review", "601"), ("T3", "comment", "701")])
        self.assertEqual(items[1]["quote"], "Split this function please.")
        self.assertEqual({(c["kind"], c["comment_id"]) for c in followups.comments(self.conn, row["id"])},
                         {("thread", "501"), ("review", "601"), ("comment", "701")})

    def test_approvals_resolved_threads_and_empty_bodies_are_not_routed(self):
        self.go_live_at()
        self.add_review(601, state="APPROVED", body="Looks good.")
        self.add_review(602, state="DISMISSED", body="Changes.")
        self.add_review(603, state="CHANGES_REQUESTED", body="   ")
        self.add_comment(701, body="\n  \n")
        entry = self.add_thread()
        entry["isResolved"] = True
        self.assertEqual(self.plan(), [])

    def test_a_comment_is_routed_once_by_its_id_even_after_an_edit(self):
        self.go_live_at()
        node = self.add_comment(701)
        row = self.routed()
        self.end(row)
        node["body"] = "Edited: please rename it to gadget."
        self.map_round(self.clock + SETTLE + 10)
        self.assertEqual(followups.count_for_task(self.conn, self.task["id"]), 1)
        self.assertEqual(self.plan(), [])

    def end(self, row: dict) -> None:
        followups.stop(self.conn, row["id"], "ended by the test")
        pensieve.mark_awaiting_close(self.conn, self.task["id"])

    def test_comments_from_before_the_pr_opened_or_the_live_period_began_are_never_routed(self):
        self.add_comment(701, at=self.t0 - 10)
        self.add_comment(702, at=self.t0 + 20)
        self.go_live_at(self.t0 + 50)
        self.add_comment(703, at=self.t0 + 60)
        self.assertEqual([item["reply_to"] for item in self.plan()], ["703"])

    def test_a_comment_from_an_earlier_live_period_is_still_routed_later(self):
        os.unlink(self.office / "desks" / "harry" / config.ENABLED_MARKER)
        self.go_live_at(self.t0 + 50)
        self.add_comment(701, at=self.t0 + 60)
        self.map_round(self.t0 + 500)  # held back: Harry is off
        self.switch_off()
        self.map_round(self.t0 + 600)
        self.add_comment(702, at=self.t0 + 650)  # written while off
        self.switch_on()
        self.map_round(self.t0 + 700)
        self.enable("harry")
        row = self.routed(self.t0 + 1200)
        self.assertEqual([item["reply_to"] for item in followups.items(self.conn, row["id"])], ["701"])

    def test_ignored_logins_are_never_routed(self):
        self.go_live_at()
        self.add_comment(701, login="ci-runner", association="MEMBER")
        self.add_comment(702, login="alice")
        with mock.patch.object(config, "FOLLOWUP_IGNORED_LOGINS", ("CI-Runner",)):
            self.assertEqual([item["reply_to"] for item in self.plan()], ["702"])

    def test_routing_waits_for_the_newest_comment_to_settle(self):
        self.go_live_at()
        self.add_comment(701, at=self.t0 + 100)
        self.add_comment(702, at=self.t0 + 300)
        self.map_round(self.t0 + 100 + SETTLE + 10)
        self.assertIsNone(followups.open_for_task(self.conn, self.task["id"]))
        row = self.routed(self.t0 + 300 + SETTLE + 1)
        self.assertEqual(len(followups.items(self.conn, row["id"])), 2)

    def test_items_past_the_size_or_count_cap_wait_for_the_next_followup(self):
        self.go_live_at()
        for index in range(3):
            self.add_comment(701 + index, at=self.t0 + 100 + index)
        with mock.patch.object(config, "FOLLOWUP_MAX_ITEMS", 2):
            row = self.routed()
        self.assertEqual([item["reply_to"] for item in followups.items(self.conn, row["id"])], ["701", "702"])
        self.assertNotIn(("comment", "703"), followups.handled(self.conn, REPO_ID))
        self.end(row)
        with mock.patch.object(config, "FOLLOWUP_TEXT_MAX", 10):
            second = self.routed(self.clock + 10)
        self.assertEqual([item["reply_to"] for item in followups.items(self.conn, second["id"])], ["703"])
        self.assertIn("cut here", (self.office / "reviews" / self.task["id"]
                                   / f"followup-{second['id']}.md").read_text())

    def test_an_incomplete_or_failed_read_routes_nothing_and_changes_nothing(self):
        self.go_live_at()
        self.add_thread()
        self.add_comment(701)
        cases = [("more", "reviewThreads"), ("more", "reviews"), ("more", "comments"), ("no id", None),
                 ("graphql", None), ("gh", None)]
        at = self.t0 + 500
        for case, name in cases:
            with self.subTest(case=case, list=name):
                self.github.more, self.github.fail = {}, None
                if case == "more":
                    self.github.more = {name: True}
                elif case == "no id":
                    self.add_comment(None, url=link("comment", "777"))
                else:
                    self.github.fail = case
                rows = self.count_rows("pr_followups"), self.count_rows("pr_comments")
                self.map_round(at)
                at += 10
                self.assertEqual((self.count_rows("pr_followups"), self.count_rows("pr_comments")), rows)
                if case == "no id":
                    self.github.pr_comments.pop()
        self.assertEqual(self.status(), "awaiting_close")
        blocked = [event for event in self.followup_events() if event["kind"] == "followup.blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("handle them by hand", blocked[0]["summary"])
        self.github.more, self.github.fail = {}, None
        self.assertIsNotNone(self.routed(at + 10))


class HousekeepingTests(FollowupCase):
    def building(self) -> dict:
        self.go_live_at()
        self.add_comment(701)
        return self.routed()

    def test_housekeeping_runs_whatever_the_switch_says_and_only_while_a_followup_is_open(self):
        with mock.patch.object(followups, "list_followups", wraps=followups.list_followups) as listed:
            self.assertEqual(followup.housekeeping(self.conn, False, self.t0), [])
        listed.assert_called_once_with(self.conn, open_only=True)
        row = self.building()
        self.switch_off()
        pensieve.close_task(self.conn, self.task["id"], "abandoned")
        self.map_round(self.clock + 10)
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "stopped")
        self.map_round(self.clock + 10)
        self.assertEqual(len([event for event in self.followup_events() if event["kind"].startswith("followup.stop")]),
                         1)

    def test_a_round_that_cannot_read_github_still_ends_a_closed_tasks_followup(self):
        row = self.building()
        pensieve.close_task(self.conn, self.task["id"], "abandoned")
        self.github.down = True
        result = self.map_round(self.clock + 10)
        self.assertFalse(result["ok"])
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "stopped")
        [event] = [event for event in self.followup_events() if event["kind"].startswith("followup.stop")]
        self.assertEqual((event["kind"], event["verdict"]), ("followup.stopped-closed", "routine"))
        self.assertEqual(patrol.read_rows("map", "rounds.jsonl")[-1]["followups"]["errors"], 0)
        self.map_round(self.clock + 10)
        self.assertEqual(len([event for event in self.followup_events() if event["kind"].startswith("followup.stop")]),
                         1)

    def test_a_round_with_a_partial_list_of_prs_still_settles_a_start_cut_off(self):
        self.go_live_at()
        self.add_comment(701)
        real = followups.advance

        def advance(conn, followup_id, state, *args, **kwargs):
            if state == "building":
                raise Killed("killed")
            return real(conn, followup_id, state, *args, **kwargs)

        with mock.patch.object(followups, "advance", side_effect=advance), self.assertRaises(Killed):
            self.map_round(self.t0 + 100 + SETTLE + 10)
        self.github.endless = True  # the list of open PRs never ends: the round reads only part of it
        result = self.map_round(self.clock + 10)
        self.assertEqual((result["ok"], result["incomplete"]), (False, True))
        row = followups.open_for_task(self.conn, self.task["id"])
        self.assertEqual(row["state"], "building")
        self.assertEqual(len(self.followup_owl_runs()), 1)
        [event] = self.followup_events()
        self.assertEqual(event["kind"], "followup.start-unsure")

    def test_a_round_that_cannot_read_github_routes_nothing_and_opens_no_live_period(self):
        self.go_live_at()
        self.add_comment(701)
        with mock.patch.object(followup, "finish_routing", side_effect=Killed("killed")), self.assertRaises(Killed):
            self.map_round(self.t0 + 100 + SETTLE + 10)
        row = followups.open_for_task(self.conn, self.task["id"])
        self.github.down = True
        # Live, but GitHub could not be read: a cut-off routing waits for a round with a whole read.
        self.map_round(self.clock + 10)
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.status()), ("routing", "active"))
        self.assertEqual(self.followup_owl_runs(), [])
        # Switched off: undone from the store alone, with one event, and the live period closes.
        self.switch_off()
        self.map_round(self.clock + 10)
        self.assertEqual((followups.get(self.conn, row["id"])["state"], self.status()), ("stopped", "awaiting_close"))
        self.assertEqual([event["kind"] for event in self.followup_events()], ["followup.stopped"])
        self.assertEqual([period["until"] for period in followups.live_periods(self.conn)], [self.clock])
        # On again while GitHub still cannot be read: no live period opens from a round that read nothing.
        self.switch_on()
        self.map_round(self.clock + 10)
        self.assertIsNone(followups.current_live(self.conn))
        self.assertEqual(self.followup_owl_runs(), [])
        self.github.down = False
        self.map_round(self.clock + 10)
        self.assertIsNotNone(followups.current_live(self.conn))

    def test_every_final_state_and_its_event_land_together(self):
        row = self.building()
        pensieve.close_task(self.conn, self.task["id"], "abandoned")
        with mock.patch.object(pensieve, "add_event", side_effect=Killed("killed")), self.assertRaises(Killed):
            followup.housekeeping(self.conn, True, self.clock + 10)
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")
        self.assertEqual([event for event in self.followup_events() if event["kind"].startswith("followup.stop")], [])
        followup.housekeeping(self.conn, True, self.clock + 20)
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "stopped")
        self.assertEqual(len([event for event in self.followup_events()
                              if event["kind"] == "followup.stopped-closed"]), 1)

    def test_a_manual_pass_killed_before_the_followup_stopped_is_stopped_next_round(self):
        row = self.building()
        self.write_file(self.wt / "widget.txt", "widget renamed\n")
        self.clock += 10
        self.stage(2, None, body=self.handoff(row, "T1 | FIXED | Renamed, see {sha}."))
        with mock.patch.object(owl_post, "_start_review", return_value=None):
            owl_post.run_pass(self.conn, now=self.clock)
        with self.fake_reviewer("PASS"), \
                mock.patch.object(followup, "stop_after_manual_pass", side_effect=Killed("killed")), \
                self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")
        self.map_round(self.clock + 10)
        row = followups.get(self.conn, row["id"])
        self.assertEqual(row["state"], "stopped")
        [event] = [event for event in self.followup_events() if event["kind"] == "followup.stopped"]
        self.assertIn("a review you ran by hand passed", event["summary"])
        self.assertEqual(self.writes, [])

    def test_housekeeping_never_stops_a_followup_the_review_loop_is_still_acting_on(self):
        row = self.building()
        task = pensieve.get_task(self.conn, self.task["id"])
        opened = capacity.open_review_round(self.conn, task["id"], "hermione", self.base_sha, "r", followup_id=row["id"],
                                            followup_max_rounds=2, idempotency_key="test:round:1")
        pensieve.start_task(self.conn, opened["task"]["id"])
        capacity.record_round_verdict(self.conn, opened["request"]["id"], REPO_ID, "PASS")
        owl_post.write_after(task["id"], opened["request"]["id"], None, "acting", "followup")
        self.assertIsNone(followup.stop_after_manual_pass(self.conn, task, row, self.clock))
        owl_post.write_after(task["id"], opened["request"]["id"], None, "done")
        with owl_post.auto_review_lock(task["id"]):
            self.assertIsNone(followup.stop_after_manual_pass(self.conn, task, row, self.clock))
        self.assertEqual(followups.get(self.conn, row["id"])["state"], "building")
        self.assertIsNotNone(followup.stop_after_manual_pass(self.conn, task, row, self.clock))
