"""The review loop's automatic draft PR after a PASS, which runs only while Ryan has opted in with one office file.

Runs on real git repos in temp folders. origin keeps its GitHub URL, but url.<bare>.insteadOf sends every push to a
local bare repo, and gh is faked at gitops.run_gh_pr, so nothing reaches the network.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from unittest import mock

from fleet import config, gitops, owl_post, push, review, worktree
from fleet.safefs import FleetError
from tests_fleet.test_push import GateCase
from tests_fleet.test_review_chain import ChainCase, Killed
from tests_fleet.test_review_loop import HANDOFF, REPO_ID

REAL_RUN_GH_PR = gitops.run_gh_pr  # captured before any test replaces it
PR_URL = "https://github.com/acme/web-app/pull/7"
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # shaped like a GitHub token, built so no scanner trips
EMAIL = "a.person" + "@" + "example.invalid"
# A password whose quoted value holds scrub's own [secret] mark, so scrubbing it leaves as many marks as it found.
HIDDEN = "pass" + 'word="hunter22 [secret]"'
# A quoted password long enough that the cut an error is kept to falls inside its quotes, after where it begins.
LONG_SECRET = "hunter22" + "Zq7" * 30
QUOTED = "pass" + f'word="{LONG_SECRET}"'
# A private key whose last line is part of its body, as an error may end.
KEY_LINE = "MIIEpAIBAAKCAQEAx" + "Yz9Wq" * 10
KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + KEY_LINE + "\n"


def past_the_cut(limit: int) -> str:
    """Error text that a cut at limit characters splits inside QUOTED, with its first characters before the cut."""
    return "x" * (limit - 50) + " " + QUOTED


class AutoPushCase(ChainCase, GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.gh_calls = []
        self.gh_answer = (0, f"Creating draft pull request\n{PR_URL}\n", "")

        def fake_gh(argv, body):
            gitops.check_draft_pr_argv(argv)
            self.gh_calls.append((argv, body))
            return self.gh_answer

        gh = mock.patch.object(gitops, "run_gh_pr", side_effect=fake_gh)
        gh.start()
        self.addCleanup(gh.stop)

    def opt_in(self, text: str = "on\n") -> None:
        self.write_file(self.office / config.AUTO_DRAFT_PR_FILE, text)

    def passed(self, change: str = "widget", body: str = None) -> dict:
        with self.fake_reviewer("PASS"):
            self.post(1, change, body=body)
        [ran] = self.reviews_run
        return ran

    def remote(self) -> str:
        return self.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.bare)

    def assert_nothing_pushed(self) -> None:
        self.assertEqual(self.remote(), "")
        self.assertEqual(self.gh_calls, [])


class OptInOffTests(AutoPushCase):
    def test_opt_in_off_a_pass_pushes_nothing_and_opens_no_pr(self):
        ran = self.passed()
        self.assertEqual((ran["outcome"], ran["next"]), ("reviewed: PASS", "ready for push"))
        self.assert_nothing_pushed()
        self.assertEqual([event["kind"] for event in self.new_events()], ["review.ready-for-push"])
        self.assertFalse(push.auto_draft_pr_on())

    def test_opt_in_off_unless_the_office_file_holds_exactly_on(self):
        path = self.office / config.AUTO_DRAFT_PR_FILE
        for text, on in (("on\n", True), (" on \n", True), ("", False), ("yes\n", False), ("ON\n", False),
                         ("on please\n", False), ("on\non\n", False)):
            with self.subTest(text=text):
                self.opt_in(text)
                self.assertEqual(push.auto_draft_pr_on(), on)
        os.chmod(path, 0o620)
        self.assertFalse(push.auto_draft_pr_on())
        os.unlink(path)
        target = self.write_file(self.tmp / "elsewhere", "on\n")
        os.symlink(target, path)
        self.assertFalse(push.auto_draft_pr_on())
        os.unlink(path)
        os.link(target, path)
        self.assertFalse(push.auto_draft_pr_on())


class OptInTests(AutoPushCase):
    def test_pass_pushes_reviewed_sha_as_draft(self):
        self.opt_in()
        ran = self.passed()
        sha = ran["review"]["sha"]
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {sha}")
        [(argv, body)] = self.gh_calls
        self.assertEqual(argv, [config.GH_BIN, "pr", "create", "--draft", "--repo", REPO_ID, "--head", "fix/widget",
                                "--base", "main", "--title", "Add the widget file", "--body-file", "-"])
        self.assertEqual(body, b"Adds the widget.\n")
        self.assertEqual(ran["next"], f"opened draft PR {PR_URL}")

    def test_pass_pushes_reviewed_sha_as_draft_through_every_push_check(self):
        self.opt_in()
        with mock.patch.object(push, "check", wraps=push.check) as checked:
            self.passed()
        checked.assert_called_once_with(self.conn, self.task["id"])

    def test_pr_url_event_is_the_one_event(self):
        self.opt_in()
        self.passed()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.draft-pr")
        self.assertIn(f"draft PR {PR_URL} is open", event["summary"])
        self.assertIn(f"task {self.task['id']} passed review", event["summary"])


class CheckedHandoffTests(AutoPushCase):
    def test_a_review_that_read_another_handoff_than_the_one_checked_never_pushes(self):
        self.opt_in()
        real = review._review_build

        def read_another(conn, task_id, lock_fd, handoff_owl=None):
            return {**real(conn, task_id, lock_fd, handoff_owl), "handoff_owl": "owl_" + "0" * 16}

        with mock.patch.object(review, "_review_build", side_effect=read_another):
            ran = self.passed()
        self.assertEqual(ran["next"], "ready for push")
        self.assert_nothing_pushed()
        self.assertEqual([event["kind"] for event in self.new_events()], ["review.ready-for-push"])


class NeverReadyMergeForceOrOtherShaTests(AutoPushCase):
    def test_never_ready_merge_force_or_other_sha_in_the_gh_command(self):
        good = gitops.draft_pr_argv(REPO_ID, "fix/widget", "main", "Add the widget file")
        bad = [
            [part for part in good if part != "--draft"],
            [*good[:3], "--draft", "--fill", *good[4:]],
            [config.GH_BIN, "pr", "merge", "--draft", *good[4:]],
            [config.GH_BIN, "pr", "ready", "--draft", *good[4:]],
            good[:11] + ["-t"] + good[12:],
            good[:11] + ["--web"] + good[12:],
            [*good, "--web"],
            ["/usr/bin/gh", *good[1:]],
        ]
        for argv in bad:
            with self.subTest(argv=argv[1:5]), self.assertRaises(FleetError):
                REAL_RUN_GH_PR(argv, b"body")
        for title in ("--web", "-x", "two\nlines", "x" * 101):
            with self.subTest(title=title[:8]), self.assertRaises(FleetError):
                gitops.draft_pr_argv(REPO_ID, "fix/widget", "main", title)

    def test_never_ready_merge_force_or_other_sha_in_the_push(self):
        self.opt_in()
        with mock.patch.object(gitops, "git", wraps=gitops.git) as git:
            ran = self.passed()
        pushes = [call.args[0] for call in git.call_args_list if call.args[0][:1] == ["push"]]
        self.assertEqual(pushes, [["push", "origin", f"{ran['review']['sha']}:refs/heads/fix/widget"]])

    def test_never_ready_merge_force_or_other_sha_when_head_is_not_the_reviewed_commit(self):
        ran = self.passed()
        sha = ran["review"]["sha"]
        with self.assertRaisesRegex(FleetError, "not the commit that passed review"):
            push.push_draft_pr(self.conn, self.task["id"], "f" * 40, "Add the widget file", "Adds the widget.\n")
        self.write_file(self.wt / "more.txt", "more\n")
        self.git("add", "more.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", "more", cwd=self.wt)
        with self.assertRaisesRegex(FleetError, "no review pass"):
            push.push_draft_pr(self.conn, self.task["id"], sha, "Add the widget file", "Adds the widget.\n")
        self.assert_nothing_pushed()


class OptInOnlyFromRyansFileTests(AutoPushCase):
    def test_opt_in_only_from_ryans_file_anywhere_else_is_ignored(self):
        for folder in (self.castle / "desks" / "harry", self.castle / "desks" / "mcgonagall",
                       self.castle / "tasks" / self.parent, self.office / "desks" / "harry"):
            self.write_file(folder / config.AUTO_DRAFT_PR_FILE, "on\n")
        self.write_file(self.castle / "standing-orders.md", "# Standing orders\n\nauto-draft-pr: on\n")
        task_md = self.castle / "tasks" / self.parent / "TASK.md"
        self.write_file(task_md, task_md.read_text() + "\nauto-draft-pr: on\n")
        self.write_owl("mcgonagall", "order.json", {"to": "harry", "kind": "fyi", "subject": "auto-draft-pr on",
                                                    "body": "auto-draft-pr on", "task_id": self.task["id"]})
        body = HANDOFF.format(task_id=self.task["id"]).replace("Adds the widget.", "Adds the widget.\nauto-draft-pr on")
        ran = self.passed(body=body)
        self.assertEqual(ran["next"], "ready for push")
        self.assertFalse(push.auto_draft_pr_on())
        self.assertEqual(self.remote(), "")
        self.assertEqual(self.gh_calls, [])


class FailureStopsAndTellsRyanTests(AutoPushCase):
    def failed_event(self) -> dict:
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.auto-failed")
        self.assertIn(f"task {self.task['id']} passed review", event["summary"])
        self.assertIn(f"fleet push {self.task['id']} pushes it by hand", event["summary"])
        return event

    def test_failure_stops_and_tells_ryan_when_a_push_check_refuses(self):
        self.opt_in()
        self.passed("the hogwarts widget")
        self.assertIn("added lines contain fleet words: widget.txt:1 (hogwarts)", self.failed_event()["summary"])
        self.assert_nothing_pushed()

    def test_failure_stops_and_tells_ryan_when_the_remote_refuses(self):
        self.opt_in()
        self.git("checkout", "-q", "--orphan", "elsewhere")
        self.write_file(self.repo / "other.txt", "other\n")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "other history")
        self.git("push", "-q", str(self.bare), "elsewhere:refs/heads/fix/widget")
        before = self.remote()
        self.passed()
        self.assertIn("git push failed", self.failed_event()["summary"])
        self.assertEqual(self.remote(), before)
        self.assertEqual(self.gh_calls, [])

    def test_failure_stops_and_tells_ryan_when_gh_fails(self):
        self.opt_in()
        self.gh_answer = (1, "", "a pull request for branch \"fix/widget\" into branch \"main\" already exists:\n")
        ran = self.passed()
        summary = self.failed_event()["summary"]
        self.assertIn("is pushed to fix/widget, but the draft PR did not open: gh pr create failed", summary)
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {ran['review']['sha']}")

    def test_failure_stops_and_tells_ryan_on_an_auth_error_and_never_repeats_it(self):
        for answer in ((4, "", f"To get started with GitHub CLI, please run: gh auth login\n{TOKEN}\n"),
                       (1, "", f"HTTP 401: Bad credentials (https://api.github.com/graphql)\nauthorization: token"
                               f" {TOKEN}\n")):
            with self.subTest(code=answer[0]):
                self.gh_answer = answer
                with self.assertRaisesRegex(FleetError, "not signed in to GitHub") as caught:
                    gitops.open_draft_pr(REPO_ID, "fix/widget", "main", "Add the widget file", "Adds it.\n")
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("Bad credentials", str(caught.exception))
        self.gh_calls.clear()
        self.opt_in()
        self.gh_answer = (4, "", f"token {TOKEN} expired\n")
        self.passed()
        summary = self.failed_event()["summary"]
        self.assertIn("not signed in to GitHub", summary)
        self.assertNotIn(TOKEN, summary)
        self.assertEqual(len(self.gh_calls), 1)

    def test_failure_stops_and_tells_ryan_when_the_pr_text_holds_an_email_or_token(self):
        self.opt_in()
        body = HANDOFF.format(task_id=self.task["id"]).replace("Adds the widget.",
                                                               f"Adds the widget. Ask {EMAIL}.")
        self.passed(body=body)
        self.assertIn("the PR text holds what looks like a credential or personal data (email)",
                      self.failed_event()["summary"])
        self.assert_nothing_pushed()
        self.assertIsNotNone(push.sensitive_mark(f"see {TOKEN}"))
        self.assertIsNone(push.sensitive_mark("fixes 0123456789abcdef0123456789abcdef01234567 on 1.2.3.4"))

    def test_failure_stops_and_tells_ryan_when_gh_names_no_pr(self):
        self.opt_in()
        self.gh_answer = (0, "https://github.com/someone/else/pull/3\n", "")
        self.passed()
        self.assertIn("gh named no PR of this repo", self.failed_event()["summary"])
        self.assertEqual(len(self.gh_calls), 1)


class ScrubbedBeforeCutTests(AutoPushCase):
    """Error text that can quote git, gh or a desk is scrubbed whole before it is cut, at every layer it passes, so a
    credential the cut would split never reaches an event, a log or a record in part."""

    def assert_no_secret(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn(LONG_SECRET[:12], text)
            self.assertNotIn(KEY_LINE[:12], text)

    def test_git_error_text_is_scrubbed_whole_before_it_is_cut(self):
        failed = subprocess.CompletedProcess([], 128, stdout=b"", stderr=past_the_cut(300).encode())
        with mock.patch.object(subprocess, "run", return_value=failed), self.assertRaises(FleetError) as caught:
            gitops.git(["push", "origin", "HEAD"], "/private/tmp/x/.git")
        self.assertIn("git push failed: xxx", str(caught.exception))
        self.assert_no_secret(str(caught.exception))

    def test_a_refused_automatic_push_never_tells_part_of_a_credential(self):
        self.opt_in()
        real = gitops._run

        def run(args, *rest, **kwargs):
            if args[0] == "push":
                return 1, "", past_the_cut(300)
            return real(args, *rest, **kwargs)

        with mock.patch.object(gitops, "_run", side_effect=run):
            ran = self.passed()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.auto-failed")
        self.assertIn("git push failed", event["summary"])
        self.assert_no_secret(event["summary"], ran["next"])

    def test_gh_error_text_is_scrubbed_whole_before_its_last_line_is_kept(self):
        self.gh_answer = (1, "", "could not open the pull request\n" + KEY)
        with self.assertRaisesRegex(FleetError, "gh pr create failed") as caught:
            gitops.open_draft_pr(REPO_ID, "fix/widget", "main", "Add the widget file", "Adds it.\n")
        self.assert_no_secret(str(caught.exception))

    def test_an_automatic_review_error_is_scrubbed_whole_before_it_is_cut(self):
        self.spawned_reviews.side_effect = None
        self.post(1, "widget")
        [owl_id] = owl_post.unfinished_handoffs(self.task["id"])
        with mock.patch.object(review, "_review_build", side_effect=FleetError(past_the_cut(300))):
            result = review.auto_review(self.conn, self.task["id"])
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.auto")
        done = (self.office / "reviews" / self.task["id"] / f"auto-{owl_id}.done").read_text()
        self.assertIn("the automatic review stopped: xxx", done)
        self.assert_no_secret(event["summary"], result["outcome"], done)

    def test_a_fix_round_error_is_scrubbed_whole_before_it_is_cut(self):
        with mock.patch.object(worktree, "build", side_effect=FleetError(past_the_cut(200))), \
                self.fake_reviewer("CHANGES"):
            self.post(1, "widget")
        [ran] = self.reviews_run
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.fix-round")
        self.assertIn("it did not start (xxx", ran["next"])
        self.assert_no_secret(event["summary"], ran["next"])

    def test_the_automatic_review_log_never_holds_part_of_a_credential(self):
        out = io.StringIO()
        with mock.patch.object(review, "auto_review", side_effect=FleetError(past_the_cut(600))), \
                contextlib.redirect_stdout(out):
            self.assertEqual(review.main([self.task["id"]]), 1)
        logged = json.loads(out.getvalue())
        self.assertTrue(logged["error"].startswith("xxx"))
        self.assert_no_secret(out.getvalue())


class SensitiveTextTests(AutoPushCase):
    def test_a_mark_already_in_the_text_never_hides_a_credential(self):
        for text in (HIDDEN, "pass" + "word=[secret]", f"[email] {EMAIL}", f"[token] {TOKEN}",
                     f"https://[credentials]@x:{TOKEN}@github.com/acme/web-app"):
            with self.subTest(text=text[:12]):
                self.assertIsNotNone(push.sensitive_mark(text))
        self.assertEqual(push.sensitive_mark(HIDDEN), "secret")
        self.assertIsNone(push.sensitive_mark("[secret] and [email] are only words here, at 1.2.3.4"))

    def test_failure_stops_and_tells_ryan_when_the_pr_text_hides_a_credential_behind_a_mark(self):
        self.opt_in()
        self.passed(body=HANDOFF.format(task_id=self.task["id"]).replace("Adds the widget.",
                                                                          f"Adds the widget. {HIDDEN}"))
        self.assertIn("the PR text holds what looks like a credential or personal data (secret)",
                      self.failed_event()["summary"])
        self.assert_nothing_pushed()

    def test_failure_stops_and_tells_ryan_when_a_commit_message_hides_a_credential_behind_a_mark(self):
        self.opt_in()
        self.passed(body=HANDOFF.format(task_id=self.task["id"]).replace("It holds the widget.",
                                                                          f"It holds the widget. {HIDDEN}"))
        self.assertIn("a commit message holds what looks like a credential or personal data (secret)",
                      self.failed_event()["summary"])
        self.assert_nothing_pushed()

    failed_event = FailureStopsAndTellsRyanTests.failed_event


class WholeOutputTests(AutoPushCase):
    """Every scan reads all that git or gh printed, or refuses: nothing past a cut is ever passed unread."""

    def committed_with_pass(self, message: str, text: str = "more\n") -> str:
        """A commit on top of a passed one, its message and its file as given, with a pass of its own."""
        self.passed()
        self.write_file(self.wt / "more.txt", text)
        self.git("add", "more.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", message, cwd=self.wt)
        sha = self.git("rev-parse", "HEAD", cwd=self.wt)
        self.grant_pass(sha)
        return sha

    def test_every_commit_message_is_scanned_whole_for_a_credential(self):
        sha = self.committed_with_pass("More of it\n\n" + "A line of padding.\n" * 40 + f"Ask {EMAIL}.")
        self.opt_in()
        with mock.patch.object(gitops, "OUTPUT_MAX_CHARS", 200), \
                self.assertRaisesRegex(FleetError, r"a commit message holds .* \(email\)"):
            push.push_draft_pr(self.conn, self.task["id"], sha, "Add the widget file", "Adds the widget.\n")
        self.assert_nothing_pushed()

    def test_every_commit_message_and_added_line_is_scanned_whole_for_a_fleet_word(self):
        sha = self.committed_with_pass("More of it\n\n" + "A line of padding.\n" * 40 + "Ask hogwarts.")
        with mock.patch.object(gitops, "OUTPUT_MAX_CHARS", 200), \
                self.assertRaisesRegex(FleetError, r"a commit message contains a fleet word \(hogwarts\)"):
            push.check(self.conn, self.task["id"])
        self.git("commit", "-q", "--amend", "-m", "More of it", cwd=self.wt)
        self.write_file(self.wt / "more.txt", "a line of padding\n" * 40 + "the hogwarts line\n")
        self.git("commit", "-q", "-a", "-m", "Still more", cwd=self.wt)
        self.grant_pass(self.git("rev-parse", "HEAD", cwd=self.wt))
        with mock.patch.object(gitops, "OUTPUT_MAX_CHARS", 200), \
                self.assertRaisesRegex(FleetError, r"added lines contain fleet words: more.txt:41 \(hogwarts\)"):
            push.check(self.conn, self.task["id"])
        self.assertEqual(self.remote(), "")

    def test_output_too_long_to_scan_whole_is_refused(self):
        sha = self.committed_with_pass("More of it\n\n" + "A line of padding.\n" * 40)
        self.opt_in()
        with mock.patch.object(gitops, "SCAN_MAX_CHARS", 200), \
                self.assertRaisesRegex(FleetError, "too much to check whole"):
            push.push_draft_pr(self.conn, self.task["id"], sha, "Add the widget file", "Adds the widget.\n")
        self.assert_nothing_pushed()

    def test_gh_output_that_may_have_been_cut_is_never_read(self):
        def cut(last: str) -> str:
            """Text as long as run_gh_pr keeps, so it may have been cut, ending in last."""
            return "x" * (gitops.OUTPUT_MAX_CHARS - len(last) - 1) + "\n" + last

        for answer in ((0, cut(PR_URL), ""), (1, "", cut(f"last {TOKEN[:12]}"))):
            with self.subTest(code=answer[0]):
                self.gh_answer = answer
                with self.assertRaisesRegex(FleetError, "printed more than the fleet reads") as caught:
                    gitops.open_draft_pr(REPO_ID, "fix/widget", "main", "Add the widget file", "Adds it.\n")
                self.assertNotIn(PR_URL, str(caught.exception))
                self.assertNotIn(TOKEN[:12], str(caught.exception))


class InterruptedPushTests(AutoPushCase):
    """A push or a PR the loop began is never begun again after a kill: Ryan hears once that it may or may not have
    happened. One the loop had not begun runs on the next pass, once."""

    def setUp(self) -> None:
        super().setUp()
        self.opt_in()
        self.spawned_reviews.side_effect = None  # the first review is run, and killed, by each test
        self.post(1, "widget")

    def interrupted_event(self) -> dict:
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.interrupted")
        return event

    def test_killed_while_it_pushed_the_push_is_never_repeated_and_ryan_hears_once(self):
        self.killed_review("PASS", push, "_push_exact")
        with mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")):
            self.next_pass()
            self.next_pass()
        summary = self.interrupted_event()["summary"]
        self.assertIn("it may or may not be pushed, and no draft PR was opened", summary)
        self.assertIn(f"fleet push {self.task['id']} pushes it by hand", summary)
        self.assert_nothing_pushed()

    def test_killed_while_it_opened_the_pr_the_pr_is_never_repeated_and_ryan_hears_once(self):
        self.killed_review("PASS", gitops, "run_gh_pr")
        sha = self.git("rev-parse", "HEAD", cwd=self.wt)
        with mock.patch.object(gitops, "run_gh_pr", side_effect=AssertionError("gh ran again")), \
                mock.patch.object(push, "_push_exact", side_effect=AssertionError("pushed again")):
            self.next_pass()
            self.next_pass()
        summary = self.interrupted_event()["summary"]
        self.assertIn(f"{sha[:12]} is pushed, but the review loop was stopped while it opened the draft PR", summary)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {sha}")

    def test_killed_before_the_push_began_it_runs_on_the_next_pass_once(self):
        self.killed_review("PASS", owl_post, "finish_handoff")
        self.assert_nothing_pushed()
        self.next_pass()
        self.next_pass()
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.draft-pr")
        self.assertEqual(len(self.gh_calls), 1)
        self.assertEqual(self.remote(), f"refs/heads/fix/widget {self.git('rev-parse', 'HEAD', cwd=self.wt)}")

    def test_killed_after_the_pr_was_told_nothing_more_is_said(self):
        real = owl_post.write_after

        def write_after(task_id, request_id, owl_id, state, step=None):
            if state == "done":
                raise Killed("killed")
            return real(task_id, request_id, owl_id, state, step)

        with self.fake_reviewer("PASS"), mock.patch.object(owl_post, "write_after", side_effect=write_after), \
                self.assertRaises(Killed):
            review.auto_review(self.conn, self.task["id"])
        self.next_pass()
        self.assertEqual([event["kind"] for event in self.new_events()], ["push.draft-pr"])
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])
        self.assertEqual(len(self.gh_calls), 1)

    def test_killed_after_a_failure_before_the_push_was_told_it_is_never_tried_again(self):
        refused = FleetError("the worktree's base is not a branch of origin, so the PR has no base branch to name")
        self.killed_before_done("PASS", mock.patch.object(push, "pr_base", side_effect=refused))
        [event] = self.new_events()
        self.assertEqual(event["kind"], "push.auto-failed")
        self.next_pass()  # what stopped it has cleared by now
        self.next_pass()
        self.assertEqual(self.new_events(), [event])
        self.assert_nothing_pushed()
        self.assertEqual(owl_post.unfinished_afters(self.task["id"]), [])

    def test_killed_after_ready_for_push_was_told_a_later_opt_in_pushes_nothing(self):
        os.unlink(self.office / config.AUTO_DRAFT_PR_FILE)
        self.killed_before_done("PASS")
        [event] = self.new_events()
        self.assertEqual(event["kind"], "review.ready-for-push")
        self.opt_in()
        self.next_pass()
        self.assertEqual(self.new_events(), [event])
        self.assert_nothing_pushed()
