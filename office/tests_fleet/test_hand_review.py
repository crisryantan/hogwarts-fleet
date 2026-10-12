"""A review Ryan runs by hand (fleet review <task-id>) tells its verdict as one event, with the kinds and keys the review
loop uses, and a PASS of the task's newest handoff goes on as the loop's PASS does: the draft PR while Ryan has opted
in, through the same push, binding and after record, so it never races the orchestrator's open_draft_pr.

Runs on real git repos in temp folders, as the loop's own draft PR tests do: every push goes to a local bare repo, and
gh is faked at gitops.run_gh_pr, so nothing reaches the network.
"""
from __future__ import annotations

from unittest import mock

from hogwarts import capacity, followups, ids, pensieve

from fleet import config, gitops, go_status, go_watch, orchestrator, owl_post, push, review
from tests_fleet.test_auto_push import PR_URL, AutoPushCase
from tests_fleet.test_review_chain import Killed
from tests_fleet.test_review_loop import HANDOFF, REPO_ID, TASK_MD


class HandReviewCase(AutoPushCase):
    def setUp(self) -> None:
        super().setUp()
        self.spawned_reviews.side_effect = None  # Harry's handoff waits for the loop, and Ryan reviews it first
        self.seen = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM events").fetchone()["id"]

    def by_hand(self, verdict: str) -> dict:
        with self.fake_reviewer(verdict):
            return review.review_build(self.conn, self.task["id"])

    def told_rows(self) -> list:
        """Each headmaster event since the test began, with its dedupe key, oldest first."""
        rows = self.conn.execute("SELECT kind, dedupe_key, summary FROM events WHERE verdict = 'headmaster' AND id > ?"
                                 " ORDER BY id", (self.seen,)).fetchall()
        return [dict(row) for row in rows]

    def told(self) -> list:
        return [(row["kind"], row["dedupe_key"]) for row in self.told_rows()]

    def orchestrator_on(self) -> None:
        self.write_file(self.office / config.ORCHESTRATOR_FILE, "on\n")

    def open_draft_pr(self) -> dict:
        return {"action": "open_draft_pr", "task_id": self.task["id"]}


class HandPassTests(HandReviewCase):
    def test_a_hand_pass_with_draft_prs_on_opens_one_pr_and_tells_it(self):
        self.opt_in()
        self.post(1, "widget")
        result = self.by_hand("PASS")
        sha = self.head()
        self.assertEqual(result["next"], f"opened draft PR {PR_URL}")
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {sha}")
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.told(), [("push.draft-pr", f"push:draft-pr:{self.task['id']}:{sha}")])
        self.assertEqual(followups.pr_for_task(self.conn, self.task["id"])["number"], 7)
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])
        # The loop's pass over the same handoff, and the orchestrator, open nothing more.
        self.next_pass()
        self.orchestrator_on()
        with self.assertRaisesRegex(orchestrator.Invalid, "already has an open PR"):
            orchestrator.check_legal(self.conn, self.open_draft_pr())
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.told(), [("push.draft-pr", f"push:draft-pr:{self.task['id']}:{sha}")])

    def test_a_hand_pass_with_draft_prs_off_is_ready_for_push(self):
        self.post(1, "widget")
        result = self.by_hand("PASS")
        self.assertEqual(result["next"], "ready for push")
        self.assert_nothing_pushed()
        self.assertEqual(self.told(), [("review.ready-for-push", f"review:ready:{self.task['id']}:{self.head()}")])

    def test_a_hand_pass_of_an_older_handoff_stops_at_ready_for_push(self):
        self.opt_in()
        self.post(1, "widget")
        real, calls = review.latest_result_owl, []

        def newer_after_the_review(conn, task):
            calls.append(task["id"])
            # Before and in the review it is this handoff; one came in after its verdict.
            return real(conn, task) if len(calls) <= 2 else {"id": "owl_" + "0" * 16}

        with mock.patch.object(review, "latest_result_owl", side_effect=newer_after_the_review):
            result = self.by_hand("PASS")
        self.assertEqual(result["next"], "ready for push")
        self.assert_nothing_pushed()
        self.assertEqual([kind for kind, _ in self.told()], ["review.ready-for-push"])

    def test_a_hand_pass_after_the_orchestrator_opened_the_pr_opens_none(self):
        self.post(1, "widget")
        with self.fake_reviewer("PASS"):
            self.next_pass()  # the loop's PASS, ready for push with draft PRs off
        self.assertEqual(self.reviews_run[-1]["next"], "ready for push")
        self.opt_in()
        self.orchestrator_on()
        name = "owl_" + "1" * 16
        self.assertEqual(orchestrator.execute(self.conn, self.open_draft_pr(), name), f"opened draft PR {PR_URL}")
        first = self.head()
        # A teammate follow-up that stopped leaves the task active with its PR bound, so Ryan can review again.
        url = followups.pr_url(REPO_ID, 7)
        item = {"label": "T1", "kind": "comment", "thread_id": None, "reply_to": "11", "url": f"{url}#issuecomment-11",
                "quote": None}
        row = followups.open_followup(self.conn, self.task["id"], ids.new_id("followup"), first, [item],
                                      [{"kind": "comment", "comment_id": "11", "label": "T1"}], config.PATROL_SENDER,
                                      "follow-up 1", "the fix request", config.FOLLOWUP_MAX_PER_TASK)
        followups.stop(self.conn, row["id"], "stopped for the test")
        self.write_file(self.wt / "widget.txt", "widget by hand\n")
        result = self.by_hand("PASS")
        second = self.head()
        self.assertNotEqual(first, second)
        self.assertEqual(result["next"], "ready for push to the open PR")
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {first}")
        [event] = [row for row in self.told_rows() if row["dedupe_key"].endswith(second)]
        self.assertEqual(event["kind"], "review.ready-for-push")
        self.assertIn("PR #7 is already open", event["summary"])

    def test_a_hand_pass_killed_while_it_pushed_is_never_pushed_again_and_ryan_hears_once(self):
        self.opt_in()
        self.post(1, "widget")
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        # As the live case: the loop gave this handoff up as blocked on tooling before Ryan ran its review by hand.
        review._auto_finish(self.conn, self.task, owl_id, "BLOCKED-ON-TOOLING after 3 tries", "headmaster", None,
                            kind="review.blocked-on-tooling")
        with self.fake_reviewer("PASS"), mock.patch.object(push, "_push_exact", side_effect=Killed("killed")), \
                self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual([record["step"] for record in owl_post.unfinished_afters(self.task["id"])], ["push"])
        self.orchestrator_on()
        with self.assertRaisesRegex(orchestrator.Invalid, "still acting"):
            orchestrator.check_legal(self.conn, self.open_draft_pr())
        with mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")), \
                mock.patch.object(gitops, "run_gh_pr", side_effect=AssertionError("gh ran again")):
            self.next_pass()
            self.next_pass()
        self.assertEqual([kind for kind, _ in self.told()], ["review.blocked-on-tooling", "review.interrupted"])
        self.assertIn("it may or may not be pushed", self.told_rows()[-1]["summary"])
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])
        with self.assertRaisesRegex(orchestrator.Invalid, "already tried"):
            orchestrator.check_legal(self.conn, self.open_draft_pr())
        self.assert_nothing_pushed()


    def test_a_hand_pass_of_a_handoff_that_names_another_task_never_pushes(self):
        self.opt_in()
        self.post(1, "widget", body=HANDOFF.format(task_id="tk_" + "0" * 16))
        result = self.by_hand("PASS")
        self.assertEqual(result["next"], "ready for push")
        self.assert_nothing_pushed()
        self.assertIn(("review.ready-for-push", f"review:ready:{self.task['id']}:{self.head()}"), self.told())

    def test_a_hand_pass_killed_before_what_follows_it_is_finished_once_by_the_next_pass(self):
        self.opt_in()
        self.post(1, "widget")
        with self.fake_reviewer("PASS"), mock.patch.object(review, "_after_hand_verdict", side_effect=Killed("k")), \
                self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        self.assertEqual([record["state"] for record in owl_post.unfinished_afters(self.task["id"])], ["hand"])
        self.assert_nothing_pushed()
        self.next_pass()
        self.next_pass()
        sha = self.head()
        self.assertEqual(self.told(), [("push.draft-pr", f"push:draft-pr:{self.task['id']}:{sha}")])
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {sha}")
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_recovery_reads_the_after_record_again_under_the_lock(self):
        self.opt_in()
        self.post(1, "widget")
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        with self.fake_reviewer("PASS"), mock.patch.object(push, "_push_exact", side_effect=Killed("killed")), \
                self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        [record] = owl_post.unfinished_afters(self.task["id"])
        # What a pass read before the review run by hand moved its record on to the push and was killed there.
        stale = {**record, "owl_id": owl_id, "state": "hand", "step": None}
        with mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")), \
                mock.patch.object(gitops, "run_gh_pr", side_effect=AssertionError("gh ran again")):
            [outcome] = review._auto_recover(self.conn, self.task, [stale], None)
        self.assertTrue(outcome["outcome"].startswith("interrupted (push)"))
        self.assertEqual([kind for kind, _ in self.told()], ["review.interrupted"])
        self.assert_nothing_pushed()

    def test_a_hand_changes_killed_before_its_event_is_told_once_and_starts_nothing(self):
        self.post(1, "widget")
        with self.fake_reviewer("CHANGES"), mock.patch.object(review, "_after_hand_verdict", side_effect=Killed("k")), \
                self.assertRaises(Killed):
            review.review_build(self.conn, self.task["id"])
        self.next_pass()
        self.next_pass()
        self.harry_runs.assert_not_called()
        self.assertEqual(self.told(), [("review.fix-round", f"review:fix-round:{self.task['id']}:1")])
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])


class HandChangesAndHeadmasterTests(HandReviewCase):
    def test_a_hand_changes_records_its_event_and_starts_nothing(self):
        self.opt_in()
        self.post(1, "widget")
        result = self.by_hand("CHANGES")
        self.assertEqual(result["next"], "no fix round started: the review was run by hand")
        self.harry_runs.assert_not_called()
        self.assertEqual(self.told(), [("review.fix-round", f"review:fix-round:{self.task['id']}:1")])
        self.assertIn(f"fleet build {self.task['id']} starts it", self.told_rows()[0]["summary"])
        self.assert_nothing_pushed()
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_a_hand_changes_at_the_round_cap_tells_the_cap(self):
        self.post(1, "widget")
        with mock.patch.object(capacity, "needs_allowance", return_value=True):
            result = self.by_hand("CHANGES")
        self.assertEqual(result["next"], "stopped at the review round cap")
        self.harry_runs.assert_not_called()
        self.assertEqual(self.told(), [("review.loop-stopped", f"review:loop-stopped:{self.task['id']}:1")])

    def test_a_hand_headmaster_records_its_event_and_starts_nothing(self):
        self.opt_in()
        self.post(1, "widget")
        result = self.by_hand("HEADMASTER")
        self.assertEqual(result["next"], "nothing more starts: the reviewer handed the decision to Ryan")
        self.harry_runs.assert_not_called()
        self.assertEqual(self.told(), [("review.headmaster", f"review:headmaster:{self.task['id']}:{self.head()}")])
        self.assert_nothing_pushed()


class HandGoUpdatesTests(HandReviewCase):
    def parent_task(self) -> str:
        """McGonagall's go task, with the spec its go recorded, so go status and go updates follow its build."""
        task_id = ids.new_id("task")
        folder = self.castle / "tasks" / task_id
        folder.mkdir(mode=0o700)
        self.write_file(folder / "TASK.md", TASK_MD.format(task_id=task_id))
        pensieve.create_task(self.conn, "mcgonagall", "add the widget check",
                             intent_path=f"{ids.TASKS_ROOT}/{task_id}/TASK.md", task_id=task_id)
        pensieve.record_spec(self.conn, task_id, str(self.repo), "fix/widget", config.DEFAULT_BASE, "a" * 64)
        pensieve.start_task(self.conn, task_id)
        return task_id

    def test_go_updates_and_go_status_show_the_pass_of_a_hand_run_review(self):
        self.write_file(self.office / config.GO_UPDATES_FILE, "on\n")
        go_watch.watch(self.conn)  # the baseline
        self.post(1, "widget")
        with mock.patch.object(go_watch, "watch", wraps=go_watch.watch) as watched:
            self.by_hand("PASS")
        watched.assert_called_with(mock.ANY, wait=config.GO_WATCH_WAIT_SECONDS)
        key, _ = go_watch.current(self.conn)[self.parent]
        self.assertEqual((key["build"], key["state"], key["verdict"]), (self.task["id"], "pass", "PASS"))
        label, action = go_watch.STATES["pass"]
        self.assertEqual(self.notified.call_args_list[-1][0][0], f"{self.parent} / {self.task['id']}: {label}. {action}")
        [(go_id, lines)], _, _ = go_status.entries(self.conn, 0)
        self.assertEqual(go_id, self.parent)
        self.assertIn("latest: [review.ready-for-push]", lines[-1])
