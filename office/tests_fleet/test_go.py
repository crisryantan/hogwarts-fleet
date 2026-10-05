"""The go: "go <task-id>" typed by Ryan registers a drafted TASK.md, routes it to Harry, makes its worktree
and starts his run, all from the UserPromptSubmit hook.

Each test runs the real hook against a temp store, castle and office, and a real git checkout whose origin
is a bare repo in the temp folder, reached through url.<path>.insteadOf, so the go's fetch runs with no
network. No desk process starts: run_desk.spawn is faked wherever a run may start.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
from unittest import mock

from hogwarts import ids, owlery, pensieve
from hogwarts.errors import ConflictError
from tests.support import NOW

from fleet import config, gitops, owl_post, run_desk, worktree
from fleet.hooks import user_prompt_submit
from fleet.safefs import FleetError
from tests_fleet.support import (
    MANY_TASK_DESKS, PROMPT_ID, TOKEN_SHAPE, FleetCase, assistant_entry, peer_entry, user_entry,
)

ORIGIN = "https://github.com/acme/web-app.git"
REPO_ID = "acme/web-app"
TASK_ID = "tk_0123456789abcdef"
OTHER_ID = "tk_fedcba9876543210"
BRANCH = "fix/widget"
SESSION = "0b6f8c1e-1111-4222-8333-944455556666"
SPEC = "repo: {repo}\nbranch: {branch}\nbase: {base}\nDesk: Harry.\nFiles: widget.txt.\n"
TASK_MD = """# {task_id} Add the widget check

## Intent
Add a check that the widget file exists.

## Acceptance criteria
AC-1 the readme is there | check: `test -f README.md`
AC-2 the widget is there | check: `test -f widget.txt`

## Spec
{spec}
## Out of scope
Anything else.
"""
STORE_TABLES = ("tasks", "task_specs", "requests", "request_phases", "owls", "run_launches", "close_tokens")


class GoCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.home_dir = self.tmp / "home"
        self.home_dir.mkdir(mode=0o700)
        self.write_file(self.home_dir / ".gitconfig", "[user]\n\tname = Test Person\n\temail = test@example.invalid\n")
        patcher = mock.patch.object(config, "USER_HOME_DIR", str(self.home_dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.home_dir / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write_file(self.repo / "README.md", "readme\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "first")
        # The origin is a bare repo here: remote.origin.url still names GitHub, and git rewrites it to the folder.
        self.origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        self.git("config", "remote.origin.url", ORIGIN)
        self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        self.git("config", f"url.{self.origin}.insteadOf", ORIGIN)
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")

    def git(self, *args, cwd=None) -> str:
        done = subprocess.run([config.GIT_BIN, *args], cwd=cwd or self.repo, capture_output=True, check=True,
                              env={"HOME": str(self.home_dir), "PATH": config.CHILD_PATH})
        return done.stdout.decode().strip()

    def origin_moves_on(self) -> str:
        """A new commit on the origin's main that the checkout has not fetched yet."""
        moved = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "on the origin")
        self.git("push", "-q", "origin", f"{moved}:refs/heads/main")
        return moved

    # TASK.md and the prompt

    def spec(self, repo=None, branch: str = BRANCH, base: str = "origin/main") -> str:
        return SPEC.format(repo=self.repo if repo is None else repo, branch=branch, base=base)

    def task_md(self, task_id: str = TASK_ID, spec: str = None, text: str = None) -> bytes:
        folder = self.castle / "tasks" / task_id
        folder.mkdir(mode=0o700, exist_ok=True)
        data = (TASK_MD.format(task_id=task_id, spec=self.spec() if spec is None else spec)
                if text is None else text).encode("utf-8")
        self.write_file(folder / "TASK.md", data)
        return data

    def transcript(self, text: str, prompt: dict = None, entrypoint: str = "claude-desktop",
                   name: str = "session.jsonl") -> str:
        current = user_entry(text, entrypoint=entrypoint, promptId=PROMPT_ID) if prompt is None else prompt
        return self.write_transcript([
            user_entry("write the TASK.md", entrypoint=entrypoint),
            assistant_entry("msg_1", [{"type": "text", "text": "drafted"}], entrypoint=entrypoint),
            current,
        ], name=name)

    def prompt(self, text: str, transcript: str = None, **fields) -> tuple:
        path = self.transcript(text) if transcript is None else transcript
        fields = {key: value for key, value in {"prompt_id": PROMPT_ID, **fields}.items() if value is not None}
        payload = {"session_id": SESSION, "transcript_path": path, "cwd": str(self.castle),
                   "hook_event_name": "UserPromptSubmit", "prompt": text, **fields}
        return self.run_hook(user_prompt_submit, payload)

    def said(self, text: str, transcript: str = None, **fields) -> tuple:
        """(shown to Ryan, given to the session, raw output) from one prompt, or ("", "", "") when silent."""
        code, out, err = self.prompt(text, transcript, **fields)
        self.assertEqual(code, 0, err)
        if not out:
            return "", "", ""
        data = json.loads(out)
        return data["systemMessage"], data["hookSpecificOutput"]["additionalContext"], out

    # what a go may leave behind

    def snapshot(self) -> dict:
        found = {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in STORE_TABLES}
        for name, folder in (("inbox", self.inbox("harry")), ("worktrees", self.castle / "worktrees"),
                             ("records", self.office / "worktrees")):
            found[name] = sorted(os.listdir(folder)) if folder.exists() else []
        found["branches"] = self.git("for-each-ref", "--format=%(refname)", "refs/heads")
        found["git worktrees"] = self.git("worktree", "list", "--porcelain").count("worktree ")
        return found

    def assert_unchanged(self, before: dict) -> None:
        self.assertEqual(self.snapshot(), before)

    def harry_task(self, task_id: str = TASK_ID) -> dict:
        [task] = [task for task in pensieve.list_tasks(self.conn, desk="harry") if task["parent_task_id"] == task_id]
        return task

    def go_ok(self, task_id: str = TASK_ID) -> tuple:
        """A go that gets through with Harry enabled: (shown, context, raw output, the spawn mock)."""
        self.enable("harry")
        with mock.patch.object(run_desk, "spawn") as spawn:
            shown, context, out = self.said(f"go {task_id}")
        self.assertIn(f"Go: {task_id} is registered", shown)
        return shown, context, out, spawn


class GoTests(GoCase):
    def test_go_registers_routes_makes_the_worktree_and_starts_harry_in_one_hook_run(self):
        data = self.task_md()
        moved = self.origin_moves_on()
        shown, context, out, spawn = self.go_ok()
        # McGonagall's task, registered with its TASK.md, and the spec the go read from it.
        task = pensieve.get_task(self.conn, TASK_ID)
        self.assertEqual((task["desk"], task["status"], task["title"], task["intent_path"]),
                         ("mcgonagall", "queued", "Add the widget check", ids.intent_path(TASK_ID)))
        spec = pensieve.task_spec(self.conn, TASK_ID)
        self.assertEqual((spec["repo_dir"], spec["branch"], spec["base"], spec["intent_sha256"]),
                         (str(self.repo), BRANCH, "origin/main", hashlib.sha256(data).hexdigest()))
        # Harry's task, his request and its owl, delivered to his inbox as the Owl Post delivers one.
        built = self.harry_task()
        request = owlery.get_request(self.conn, built["request_id"])
        self.assertEqual((request["requester"], request["recipient"], request["parent_task_id"], request["phase"]),
                         ("mcgonagall", "harry", TASK_ID, "running"))
        [owl] = owlery.inbox(self.conn, "harry")
        self.assertEqual((owl["kind"], owl["sender"], owl["task_id"], owl["request_id"]),
                         ("request", "mcgonagall", built["id"], request["id"]))
        self.assertIsNotNone(owl["delivered_at"])
        copy = json.loads((self.inbox("harry") / f"{owl['id']}.json").read_text())
        self.assertEqual((copy["delivered_by"], copy["task_md"], copy["parent_task_id"]),
                         ("owl-post", ids.intent_path(TASK_ID), TASK_ID))
        self.assertIn(f"Branch {BRANCH} from origin/main", copy["body"])
        # The worktree, on the new branch from the base the go fetched, and Harry's run on that owl.
        self.assertEqual((built["status"], built["worktree"]), ("active", f"{ids.WORKTREES_ROOT}/{built['id']}"))
        path = config.worktree_dir(built["id"])
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path), BRANCH)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), moved)
        record = gitops.read_record(built["id"])
        self.assertEqual((record["repo_dir"], record["branch"], record["base"], record["base_ref"], record["repo"]),
                         (str(self.repo), BRANCH, moved, "origin/main", REPO_ID))
        spawn.assert_called_once_with("harry", owl["id"])
        self.assertIn(f"Harry's task {built['id']} has its worktree on the new branch {BRANCH}", shown)
        self.assertIn(f"Harry: started harry on owl {owl['id']}.", shown)
        self.assertIn(user_prompt_submit.GO_CONTEXT, context)
        # Nothing printed carries a token, the TASK.md hash or the prompt's id.
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9_-])" + TOKEN_SHAPE + r"(?![A-Za-z0-9_-])", out))
        self.assertNotIn(spec["intent_sha256"], out)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", out))
        self.assertNotIn(PROMPT_ID, out)

    def test_a_disabled_harry_gets_his_task_and_worktree_but_no_run(self):
        self.task_md()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn("Harry: harry is not enabled, so nothing was started.", shown)
        self.assertEqual(self.harry_task()["status"], "active")

    def test_a_run_that_cannot_start_after_the_commit_says_how_to_start_it(self):
        self.task_md()
        self.enable("harry")
        with mock.patch.object(run_desk, "spawn", side_effect=OSError("no fork")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        built = self.harry_task()
        self.assertIn(f"Go: {TASK_ID} is registered", shown)
        self.assertIn(f"Harry's run did not start (OSError); start it with fleet build {built['id']}.", shown)
        self.assertEqual(built["status"], "active")

    def test_other_text_and_a_go_that_is_not_the_whole_prompt_change_nothing(self):
        self.task_md()
        self.enable("harry")
        before = self.snapshot()
        silent = ("hello", "go", "go ahead", "going to look at it", f"Mischief managed {TASK_ID}",
                  "tk_0123456789abcdef looks good")
        near = (f"Go {TASK_ID}", f"GO {TASK_ID}", f"go {TASK_ID}.", f"please go {TASK_ID}", f"go  {TASK_ID}",
                f"go {TASK_ID}\nand also tidy the readme", f"go {TASK_ID} {OTHER_ID}", f"go {TASK_ID.upper()}",
                f"looks good, go {TASK_ID}", f"go\t{TASK_ID}")
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")), \
                mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            for text in silent + near:
                with self.subTest(text=text):
                    shown, _, _ = self.said(text)
                    self.assertNotIn("Go:", shown)
                    self.assertNotIn("Go was not applied to", shown)
                    self.assertEqual(user_prompt_submit.GO_EXACT in shown, text in near)
                    self.assert_unchanged(before)

    def test_a_go_the_hook_cannot_confirm_ryan_typed_changes_nothing(self):
        self.task_md()
        self.enable("harry")
        before = self.snapshot()
        text = f"go {TASK_ID}"
        typed = {"entrypoint": "claude-desktop", "promptId": PROMPT_ID}
        stale = user_entry(text, timestamp="2027-01-15T07:50:00.000Z", **typed)
        mixed = self.write_transcript([user_entry("hello", entrypoint="sdk-cli"), user_entry(text, **typed)],
                                      name="mixed.jsonl")
        cases = (
            ("print mode", self.transcript(text, entrypoint="sdk-cli", name="print.jsonl"), {}),
            ("resumed with claude -p", mixed, {}),
            ("subagent", self.transcript(text, name="sub.jsonl"), {"agent_id": "agent-1"}),
            ("no transcript", str(self.transcripts / "-project" / "missing.jsonl"), {}),
            ("outside root", "/private/tmp/elsewhere.jsonl", {}),
            ("peer message", self.transcript(text, name="peer.jsonl", prompt=peer_entry(text, promptId=PROMPT_ID)), {}),
            ("meta only", self.transcript(text, name="meta.jsonl", prompt=user_entry(text, isMeta=True, **typed)), {}),
            ("system source", self.transcript(text, name="sys.jsonl",
                                              prompt=user_entry(text, promptSource="system", **typed)), {}),
            ("prompt not written yet", self.transcript(text, name="later.jsonl", prompt=assistant_entry(
                "msg_2", [{"type": "text", "text": "ok"}])), {}),
            ("no prompt id", self.transcript(text, name="noid.jsonl"), {"prompt_id": None}),
            ("stale entry", self.transcript(text, name="stale.jsonl", prompt=stale), {}),
        )
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")), \
                mock.patch.object(user_prompt_submit, "_go", side_effect=AssertionError("a go ran")):
            for label, transcript, extra in cases:
                with self.subTest(label=label):
                    shown, context, _ = self.said(text, transcript=transcript, **extra)
                    self.assertIn(f"Go was not applied to {TASK_ID}: this hook could not confirm Ryan's own typing",
                                  shown)
                    self.assertIn(f"castle task create --id {TASK_ID} --desk mcgonagall", shown)
                    self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
                    self.assert_unchanged(before)

    def test_a_go_for_a_task_with_no_task_md_changes_nothing(self):
        self.enable("harry")
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
            self.assertIn(f"Go was not applied to {TASK_ID}: there is no TASK.md at ~/hogwarts/tasks/{TASK_ID}/TASK.md",
                          shown)
            # Another task's TASK.md does not stand in for it.
            self.task_md(OTHER_ID)
            shown, _, _ = self.said(f"go {TASK_ID}")
            self.assertIn("there is no TASK.md", shown)
        self.assert_unchanged(before)

    def test_a_go_for_a_task_that_is_already_registered_changes_nothing(self):
        self.task_md()
        self.enable("harry")
        pensieve.create_task(self.conn, "mcgonagall", "registered by hand", intent_path=ids.intent_path(TASK_ID),
                             task_id=TASK_ID, now=NOW)
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, context, _ = self.said(f"go {TASK_ID}")
        self.assertIn(f"Go was not applied to {TASK_ID}: the task is already registered (queued)", shown)
        self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
        self.assert_unchanged(before)
        self.assertIsNone(pensieve.task_spec(self.conn, TASK_ID))

    def test_a_second_go_changes_nothing_and_names_the_fallback_for_a_run_that_never_started(self):
        self.task_md()
        self.go_ok()
        built = self.harry_task()
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn("the task is already registered (queued)", shown)
        self.assertIn(f"If Harry's run on {built['id']} did not start, start it with fleet build {built['id']}.", shown)
        self.assert_unchanged(before)

    def test_an_edit_to_task_md_after_go_changes_none_of_the_stored_values(self):
        self.task_md()
        self.go_ok()
        built = self.harry_task()
        spec, record = pensieve.task_spec(self.conn, TASK_ID), gitops.read_record(built["id"])
        [owl] = owlery.inbox(self.conn, "harry")
        body = owlery.read(self.conn, owl["id"], "harry", now=NOW)["body"]
        other = self.home_dir / "other"
        other.mkdir()
        self.git("init", "-q", "-b", "main", cwd=other)
        self.task_md(spec=self.spec(repo=other, branch="fix/other", base="origin/dev"))
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn("already registered", shown)
        self.assertEqual(pensieve.task_spec(self.conn, TASK_ID), spec)
        self.assertEqual(gitops.read_record(built["id"]), record)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=config.worktree_dir(built["id"])), BRANCH)
        self.assertEqual(owlery.read(self.conn, owl["id"], "harry", now=NOW)["body"], body)
        # fleet worktree is no way round it: a later build task under this TASK.md takes only the stored values.
        pensieve.close_task(self.conn, built["id"], "abandoned")
        later = owlery.open_request(self.conn, "mcgonagall", "harry", "build it again", body="see TASK.md",
                                    parent_task_id=TASK_ID, now=NOW)["task"]
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            for repo_dir, branch, base in ((str(other), "fix/other", "origin/dev"),
                                           (str(self.repo), "fix/other", "origin/main"),
                                           (str(self.repo), BRANCH, "main")):
                with self.subTest(repo=repo_dir, branch=branch, base=base), \
                        self.assertRaisesRegex(FleetError, "was started with go, so its worktree takes only"):
                    worktree.create(self.conn, later["id"], repo_dir, branch, base, fetch=False)
        self.assertFalse(os.path.lexists(config.worktree_dir(later["id"])))

    def test_task_md_changed_after_registration_refuses_and_rolls_back(self):
        self.task_md()
        self.enable("harry")
        before = self.snapshot()
        real_open = owlery.open_request

        def open_then_edit(*args, **kwargs):
            opened = real_open(*args, **kwargs)
            self.task_md(spec=self.spec(branch="fix/edited"))  # an edit lands after the go read the file
            return opened

        with mock.patch.object(owlery, "open_request", side_effect=open_then_edit), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, context, _ = self.said(f"go {TASK_ID}")
        self.assertIn(f"Go was not applied to {TASK_ID}: TASK.md changed after your go, so nothing was started", shown)
        self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
        self.assert_unchanged(before)
        # The edited file is a fresh draft: typing go again starts it, from what it says now.
        self.go_ok()
        self.assertEqual(pensieve.task_spec(self.conn, TASK_ID)["branch"], "fix/edited")

    def test_task_md_changed_while_the_worktree_is_made_takes_everything_back(self):
        self.task_md()
        self.enable("harry")
        before = self.snapshot()
        real_add = worktree.add_worktree

        def add_then_edit(*args, **kwargs):
            added = real_add(*args, **kwargs)
            self.task_md(spec=self.spec(branch="fix/edited"))
            return added

        with mock.patch.object(worktree, "add_worktree", side_effect=add_then_edit), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn("TASK.md changed after your go, so nothing was started", shown)
        self.assert_unchanged(before)

    def test_a_missing_or_malformed_spec_block_refuses_with_a_plain_reason_and_creates_nothing(self):
        self.enable("harry")
        outside = self.tmp / "outside"
        outside.mkdir()
        self.git("init", "-q", "-b", "main", cwd=outside)
        plain = self.home_dir / "plain"
        plain.mkdir()
        linked = self.home_dir / "linked"
        os.symlink(self.repo, linked)
        no_origin = self.home_dir / "no-origin"
        no_origin.mkdir()
        self.git("init", "-q", "-b", "main", cwd=no_origin)
        elsewhere = self.home_dir / "elsewhere"
        elsewhere.mkdir()
        self.git("init", "-q", "-b", "main", cwd=elsewhere)
        self.git("config", "remote.origin.url", "https://example.invalid/acme/web-app.git", cwd=elsewhere)
        good = self.spec()
        repo, branch, base = f"repo: {self.repo}", f"branch: {BRANCH}", "base: origin/main"
        cases = (
            ("no Spec section", None, TASK_MD.format(task_id=TASK_ID, spec=good).replace("## Spec\n", ""),
             "TASK.md must have exactly one ## Spec section"),
            ("two Spec sections", None, TASK_MD.format(task_id=TASK_ID, spec=good + "\n## Spec\n" + good),
             "TASK.md must have exactly one ## Spec section"),
            ("no repo line", f"{branch}\n{base}\n", None, user_prompt_submit.SPEC_BLOCK),
            ("no branch line", f"{repo}\n{base}\n", None, user_prompt_submit.SPEC_BLOCK),
            ("no base line", f"{repo}\n{branch}\nDesk: Harry.\n", None, user_prompt_submit.SPEC_BLOCK),
            ("out of order", f"{branch}\n{repo}\n{base}\n", None, user_prompt_submit.SPEC_BLOCK),
            ("prose first", f"Desk: Harry.\n{repo}\n{branch}\n{base}\n", None, user_prompt_submit.SPEC_BLOCK),
            ("capitalised key", f"Repo: {self.repo}\n{branch}\n{base}\n", None, user_prompt_submit.SPEC_BLOCK),
            ("named twice", f"{repo}\n{branch}\n{base}\nbranch: fix/other\n", None, "more than once"),
            ("named twice, indented", f"{repo}\n{branch}\n{base}\n  Base: main\n", None, "more than once"),
            ("empty repo", f"repo:\n{branch}\n{base}\n", None, "the Spec's repo: line is refused"),
            ("relative repo", f"repo: repo\n{branch}\n{base}\n", None, "the Spec's repo: line is refused"),
            ("repo in backticks", f"repo: `{self.repo}`\n{branch}\n{base}\n", None, "the Spec's repo: line is refused"),
            ("repo outside home", f"repo: {outside}\n{branch}\n{base}\n", None, "inside your home folder"),
            ("repo in the castle", f"repo: {self.castle}\n{branch}\n{base}\n", None, "the Spec's repo: line"),
            ("repo with no .git", f"repo: {plain}\n{branch}\n{base}\n", None, "main checkout with its own .git"),
            ("repo through a symlink", f"repo: {linked}\n{branch}\n{base}\n", None, "must not go through a symlink"),
            ("repo with no origin", f"repo: {no_origin}\n{branch}\n{base}\n", None, "the repo has no origin remote"),
            ("repo with another origin", f"repo: {elsewhere}\n{branch}\n{base}\n", None, "not a plain GitHub URL"),
            ("empty branch", f"{repo}\nbranch:\n{base}\n", None, "the Spec's branch: line is refused"),
            ("capital branch", f"{repo}\nbranch: Fix/Widget\n{base}\n", None, "the Spec's branch: line is refused"),
            ("fleet word branch", f"{repo}\nbranch: harry/widget\n{base}\n", None, "fleet word"),
            ("dotted branch", f"{repo}\nbranch: fix/../widget\n{base}\n", None, "the Spec's branch: line"),
            ("empty base", f"{repo}\n{branch}\nbase:\n", None, "the Spec's base: line is refused"),
            ("spaced base", f"{repo}\n{branch}\nbase: origin main\n", None, "the base is not a plain git ref"),
            ("dotted base", f"{repo}\n{branch}\nbase: origin/../main\n", None, "the base is not a plain git ref"),
            ("missing base ref", f"{repo}\n{branch}\nbase: origin/nothing-here\n", None, "git fetch failed"),
            ("no title line", None, TASK_MD.format(task_id=TASK_ID, spec=good).split("\n", 1)[1],
             f"TASK.md must start with the line # {TASK_ID} <title>"),
            ("another task's title", None, TASK_MD.format(task_id=OTHER_ID, spec=good),
             f"TASK.md must start with the line # {TASK_ID} <title>"),
            ("not UTF-8", None, None, "TASK.md is not UTF-8 text"),
        )
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            for label, spec, text, reason in cases:
                with self.subTest(label=label):
                    if label == "not UTF-8":
                        self.task_md()
                        path = self.castle / "tasks" / TASK_ID / "TASK.md"
                        self.write_file(path, path.read_bytes() + b"\xff\xfe")
                    else:
                        self.task_md(spec=spec, text=text)
                    shown, context, _ = self.said(f"go {TASK_ID}")
                    self.assertIn(f"Go was not applied to {TASK_ID}: ", shown)
                    self.assertIn(reason, shown)
                    self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
                    self.assert_unchanged(before)

    def test_drafting_and_editing_a_task_md_before_go_starts_nothing(self):
        self.enable("harry")
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            self.task_md()
            for text in ("here is the draft", f"read {TASK_ID} and change AC-2", "looks good, but tighten AC-1"):
                self.said(text)
            self.task_md(spec=self.spec(branch="fix/other"))
            self.said("that is better")
            owl_post.run_pass(self.conn)
        self.assert_unchanged(before)
        self.assertEqual(pensieve.list_tasks(self.conn), [])


class GoWorktreeChecksTests(GoCase):
    """Each refusal fleet worktree makes still fires when a go asks for the worktree, and leaves nothing."""

    def refused(self, reason: str) -> None:
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, context, _ = self.said(f"go {TASK_ID}")
        self.assertIn(f"Go was not applied to {TASK_ID}: {reason}", shown)
        self.assertNotIn(user_prompt_submit.GO_CONTEXT, context)
        self.assert_unchanged(before)

    def setUp(self) -> None:
        super().setUp()
        self.task_md()
        self.enable("harry")

    def test_an_existing_branch_is_refused_through_go(self):
        self.git("branch", BRANCH)
        self.refused("that branch already exists; pick a new branch name")

    def test_a_held_task_md_lock_is_refused_through_go(self):
        with worktree.holder_lock(TASK_ID):
            self.refused(worktree.WORKTREE_RUNNING)

    def test_a_refused_attach_takes_the_worktree_back_through_go(self):
        with mock.patch.object(pensieve, "set_worktree", side_effect=ConflictError("refused late")):
            self.refused("refused late")

    def test_a_failed_take_back_names_what_is_left(self):
        real_git = worktree.gitops.git

        def failing_remove(args, *rest, **kwargs):
            if args[:2] == ["worktree", "remove"]:
                raise FleetError("git worktree failed: locked")
            return real_git(args, *rest, **kwargs)

        with mock.patch.object(pensieve, "set_worktree", side_effect=ConflictError("refused late")), \
                mock.patch.object(worktree.gitops, "git", side_effect=failing_remove), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertRegex(shown, r"Go was not applied to tk_0123456789abcdef: refused late; taking back the new"
                                r" worktree also failed \(git worktree failed: locked\), so remove the worktree")
        self.assertEqual(pensieve.list_tasks(self.conn), [])

    def test_a_refusal_after_the_worktree_was_made_takes_it_back(self):
        with mock.patch.object(user_prompt_submit, "_deliver", side_effect=FleetError("recipient has no castle inbox")):
            self.refused("recipient has no castle inbox")

    def test_a_signal_after_the_worktree_was_made_takes_everything_back(self):
        real_deliver = user_prompt_submit._deliver

        def deliver_then_terminated(*args, **kwargs):
            real_deliver(*args, **kwargs)
            signal.raise_signal(signal.SIGTERM)  # the hook's timeout, after the inbox copy is written

        before = self.snapshot()
        with mock.patch.object(user_prompt_submit, "_deliver", side_effect=deliver_then_terminated), \
                mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            with self.assertRaises(SystemExit) as stopped:
                self.prompt(f"go {TASK_ID}")
        self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
        self.assert_unchanged(before)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class SingleHarryGoTests(GoCase):
    # A store where Harry takes one task at a time, so an active task of his makes him unable to start another.
    many_task_desks = tuple(desk for desk in MANY_TASK_DESKS if desk != "harry")

    def test_a_desk_that_cannot_start_the_task_is_refused_through_go(self):
        busy = pensieve.create_task(self.conn, "harry", "another build", now=NOW)
        pensieve.start_task(self.conn, busy["id"], now=NOW)
        self.task_md()
        self.enable("harry")
        before = self.snapshot()
        with mock.patch.object(run_desk, "spawn", side_effect=AssertionError("spawned")):
            shown, _, _ = self.said(f"go {TASK_ID}")
        self.assertIn(f"Go was not applied to {TASK_ID}: harry already has an active task {busy['id']}", shown)
        self.assert_unchanged(before)
