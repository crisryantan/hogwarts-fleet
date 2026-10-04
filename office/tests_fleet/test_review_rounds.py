"""Review rounds through the review script: a capped review waits and the next commit supersedes it,
and a fourth round waits for Ryan's allow-round. Reviewer runs are faked at run_desk.run."""
from __future__ import annotations

from unittest import mock

from hogwarts import capacity, owlery, pensieve
from tests.support import NOW

from fleet import review, run_desk
from fleet.safefs import FleetError
from tests_fleet.test_review_loop import LoopCase, REPO_ID


class ReviewRoundTests(LoopCase):
    def setUp(self) -> None:
        super().setUp()
        clock = mock.patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.enable("moody")

    def commit(self, text: str) -> str:
        self.write_file(self.repo / "fix.txt", text + "\n")
        self.git("add", "fix.txt")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def own_review(self, task_id: str = None, verdict: str = "CHANGES") -> dict:
        with self.fake_reviewer(verdict):
            if task_id is None:
                return review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
            return review.review_own(self.conn, str(self.repo), task_id=task_id, fetch=False)

    def round_cap_events(self) -> list:
        return [event for event in self.events() if event["kind"] == "review.round-cap"]

    def test_a_capped_review_waits_and_the_next_commit_supersedes_it(self):
        first_sha = self.commit("first try")
        with mock.patch.object(run_desk, "over_daily_cap", return_value="daily run cap reached"), \
                mock.patch.object(run_desk, "report_cap") as reported, \
                mock.patch.object(run_desk, "run", side_effect=AssertionError("a capped reviewer ran")):
            with self.assertRaisesRegex(run_desk.Capped, "waits as request"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        reported.assert_called_once()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [waiting] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((waiting["sha"], waiting["round"], waiting["waiting"]), (first_sha, 1, True))
        self.assertEqual([row["id"] for row in capacity.waiting_requests(self.conn, "moody")], [waiting["request_id"]])
        second_sha = self.commit("second try")
        result = self.own_review(task["id"], verdict="PASS")
        self.assertEqual((result["sha"], result["round"], result["superseded"]), (second_sha, 1, [waiting["request_id"]]))
        old = owlery.get_request(self.conn, waiting["request_id"])
        self.assertEqual((old["outcome"], old["reason"]), ("deferred", "conflict"))
        reviewed = [row[0] for row in self.conn.execute("SELECT sha FROM review_passes")]
        self.assertEqual(reviewed, [second_sha])
        self.assertTrue(owlery.has_pass(self.conn, REPO_ID, second_sha))
        self.assertEqual(capacity.waiting_requests(self.conn, "moody"), [])
        [old_owl] = [owl for owl in owlery.request_owls(self.conn, waiting["request_id"]) if owl["kind"] == "request"]
        self.assertIsNotNone(old_owl["acked_at"])

    def test_round_four_waits_for_allow_round_and_round_five_is_refused_again(self):
        first = self.own_review()
        task_id = first["task_id"]
        for text in ("round two", "round three"):
            self.commit(text)
            self.own_review(task_id)
        self.commit("round four")
        with mock.patch.object(run_desk, "run", side_effect=AssertionError("round four ran")):
            with self.assertRaisesRegex(FleetError, "review round 4"):
                self.own_review(task_id)
        [event] = self.round_cap_events()
        self.assertEqual((event["verdict"], event["desk"]), ("headmaster", "ryan-claude-1"))
        self.assertIn(f"task {task_id} asked for review round 4", event["summary"])
        self.assertIn(f"castle task allow-round {task_id}", event["summary"])
        self.assertEqual(len(capacity.review_rounds(self.conn, task_id)), 3)
        capacity.allow_round(self.conn, task_id)
        fourth = self.own_review(task_id)
        self.assertEqual((fourth["round"], fourth["verdict"]), (4, "CHANGES"))
        self.commit("round five")
        with self.assertRaisesRegex(FleetError, "review round 5"):
            self.own_review(task_id)
        self.assertEqual([event["summary"].split(",")[0] for event in self.round_cap_events()],
                         [f"task {task_id} asked for review round 4", f"task {task_id} asked for review round 5"])

    def test_a_vendor_limit_on_the_reviewer_is_named_and_never_offered_a_bump(self):
        self.commit("my fix")
        def limited(conn, desk, owl_id, mcp_job=None, now=None, on_start=None):
            on_start()
            return {"desk": desk, "run_id": "run-" + "b" * 16, "exit_code": 1, "cap_source": "codex_plan"}
        with mock.patch.object(run_desk, "run", side_effect=limited):
            with self.assertRaisesRegex(FleetError, "cap_source codex_plan.*does not lift it"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)

    def test_a_cap_reached_while_waiting_for_the_desk_lock_leaves_the_round_waiting(self):
        # Another moody run held the lock and used the last run: the check inside the lock refuses.
        first_sha = self.commit("first try")
        with mock.patch.object(run_desk, "over_daily_cap", side_effect=[None, "daily run cap reached"]), \
                mock.patch.object(run_desk, "report_cap") as reported, \
                mock.patch.object(run_desk, "_launch", side_effect=AssertionError("a capped reviewer ran")):
            with self.assertRaisesRegex(run_desk.Capped, "while it waited for its desk lock.*waits as request"):
                review.review_own(self.conn, str(self.repo), title="my own fix", fetch=False)
        reported.assert_called_once()
        [task] = pensieve.list_tasks(self.conn, desk="ryan-claude-1")
        [waiting] = capacity.review_rounds(self.conn, task["id"])
        self.assertEqual((waiting["sha"], waiting["waiting"]), (first_sha, True))
        request = owlery.get_request(self.conn, waiting["request_id"])
        self.assertEqual((request["phase"], request["outcome"]), ("queued", None))
        self.assertEqual(pensieve.get_task(self.conn, request["task_id"])["status"], "queued")
        second_sha = self.commit("second try")
        result = self.own_review(task["id"], verdict="PASS")
        self.assertEqual((result["sha"], result["round"], result["superseded"]), (second_sha, 1, [waiting["request_id"]]))
