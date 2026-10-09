"""UserPromptSubmit hook: show headmaster events, warn at 200k context, start a build on "go", close on
"Mischief managed".

Input fields read:
- prompt: the text Ryan submitted (the documented UserPromptSubmit field).
- prompt_id: the id Claude Code gives this prompt. Its transcript entries carry it as promptId.
- transcript_path: the session transcript, read from its tail only.
- agent_id: present only inside a subagent; a close or a go is refused there.
- agent_type: names the agent a session's main thread runs. A go runs only when common.session_desk
  says the session is McGonagall's.

Output is one JSON object. systemMessage is shown to Ryan. The same text goes to the
session as additionalContext, except that headmaster events appear there as a count
only, because the startup digest already lists them for the session.

What it says (each part only when there is something to say):
1. The result of "Mischief managed <task-id>" when the prompt is exactly that, after
   trimming surrounding whitespace. Under the task's review lock, taken without waiting,
   it mints a close token with minted_by "hook" and closes that one task as complete in
   the same run, then names every descendant task the close cascaded to and how each one
   closed, a go task that moved on because this was its last build (on a build's proof,
   fleet/closer.py advance_parents), and what to type next. The token is never printed.
   "Mischief managed everything", exactly that once trimmed, is a gate of its own with the
   same checks: every task in a closeable state closes through that same close, and every
   other open task is refused by name with its reason (fleet/bulk_close.py). Every refused
   close ends with a Fix: line saying exactly what to type next. Any other text never
   closes anything.
2. The result of each go when every non-empty line of the prompt, trimmed, is exactly
   "go <task-id>": at most config.GO_MAX_PER_PROMPT distinct ids, repeats counted once,
   each started in order and on its own, so one refused never stops the others, with one
   result line per id when there are several. See "The go" below. A prompt that only looks
   like gos (each line a go once list markers, backticks, quotes and the spaces round them
   are stripped) gets one refusal line and starts nothing, and so does a go or a go attempt
   in a session that is not McGonagall's. Prose that only mentions a go gets nothing.
   Any other prompt pays only for a few regular expressions.
3. Unacked headmaster events, newest per task, cut to about 1500 characters, with a
   count of the rest. A session is shown the full list once (the session start digest, or its first
   prompt when none was recorded); each later prompt lists only events newer than the last one shown
   to that session (fleet/events_seen.py), and nothing new means nothing. The hook acks only events
   the fleet already settled (pensieve.settle_events): a go or close refusal that a later
   confirmation of the same task followed, and an owl-to-McGonagall event whose owl she has read.
   The rest stay until Ryan runs castle event ack in his terminal.
4. In McGonagall's session only, her delivered owls she has not read and has not been shown,
   one line each with a scrubbed one-line status, at most config.INBOX_NOTICE_CAP and a count
   of the rest, each listed once (fleet/mcgonagall_inbox.py). No owl body beyond that line.
   Then her go status: each open go task's build, branch, state, who it waits on and newest event,
   in full once per session and then only what changed (fleet/go_status.py), so she reads where her
   work stands without asking anyone to run castle.
5. Ollivander's stop, while one is in place, as the first line of the events part on every prompt in
   every desk session, with the exact command that clears it (fleet/stops.py).
6. One Tempus line when the last assistant call in the transcript carried more than
   200k tokens of context (input plus cache read plus cache creation).

The close runs only when every check passes (for the bulk close, every check but the first, which
it makes per task):
- the task is awaiting_close, so the digest has already shown Ryan it waits for him;
- there is no agent_id;
- every transcript entry in the tail names the same entrypoint, and it is one of
  config.CLOSE_ALLOWED_ENTRYPOINTS ("cli" or "claude-desktop"; claude -p writes "sdk-cli");
- the tail holds this prompt's own entry (promptId equal to prompt_id), and that entry
  is a typed prompt: origin.kind "human", not isMeta, promptSource not "system", and at
  most CLOSE_PROMPT_MAX_AGE seconds old. Text another session sends in with
  send_message arrives with origin.kind "peer", isMeta and promptSource "system".

Deferred confirmation. Claude Code writes the prompt's own transcript entry only after this
hook returns, and for a new session the transcript file too, so at hook time the entry is
usually missing. When that is the only thing missing (no agent_id, a prompt_id, for a close
a task awaiting close, for a go McGonagall's session, and no other check failed), the hook
refuses nothing and starts nothing itself: it claims the prompt in the office (one claim per
prompt id, so a retried hook never starts a second confirmer) and starts one detached
confirmer, fleet/go_confirm.py, with its input on a pipe and nothing in its argv, then says
the go or close is being confirmed and that its result arrives as a headmaster event within
about half a minute. The confirmer waits up to config.GO_CONFIRM_WAIT_SECONDS for the entry,
runs the same checks on it, takes the gos or the close from the entry's own text and refuses
when it differs from what the hook saw, then runs the code below unchanged. Its outcome, a
refusal with the manual steps included, is a headmaster event on McGonagall's desk. A missing
file or a cut last line is "not yet", never a pass. When the entry is already there at hook
time, the hook runs the go or the close itself, as below. Either way, what runs is taken from
the verified entry's own text and must equal what the hook input asks for
(verified_requests); anything else is refused.

The go starts a build from a TASK.md McGonagall drafted and Ryan approved, with no command
in his terminal. It runs only in McGonagall's own session: when common.session_desk names any
other desk (Ryan's own sessions, a subagent, or the hook run with --desk for another desk), a go
gets one refusal line before anything else is read, and changes nothing. In her session it
passes the same typed check as the close (typing_entry) (or is deferred to the confirmer as above),
before it reads anything else, and then,
only for a task id the store does not know yet whose TASK.md is at
~/hogwarts/tasks/<task-id>/TASK.md:
- reads TASK.md once (no link, at most 64KB) and takes its title from the first line,
  "# <task-id> <title>", and the repo folder, branch and base from the block the ## Spec
  section must open with: the lines "repo: <folder>", "branch: <new branch>" and
  "base: <ref>", in that order, each once. Anything else refuses with a plain reason;
- checks them as fleet worktree checks --repo-dir, --branch and --base, and fetches the
  base before any store write, so no network wait holds the store;
- takes the lock on making that branch in that repo without waiting (worktree.branch_claim),
  and holds it until its transaction commits or its take-back ends, so a fleet worktree or
  another go for the same branch, under any TASK.md, is refused at once meanwhile;
- then, in one store transaction: registers the task on McGonagall's desk with its
  TASK.md, records the repo folder, branch, base and the TASK.md sha256 with it
  (pensieve.record_spec, never changed after), opens the request to Harry, which makes
  his queued task and the request owl, checks TASK.md still hashes to the stored sha256,
  makes the worktree through worktree.create with the stored values only (every check of
  fleet worktree, the holder lock and the take-back included), delivers the owl to Harry's
  inbox as the Owl Post would, and checks the sha256 once more;
- only after that transaction commits, starts Harry's run with worktree.start_locked, when
  Ryan has enabled him and he is under his cap.
A refusal anywhere before the commit, or SIGTERM or SIGHUP (a hook timeout), rolls the
store back and takes back the worktree, however far git got with it, its branch, its record
and the inbox copy, so a go that did not get through leaves nothing and can be typed again.
The go owns that take-back from before git makes anything: worktree.create fills its claim
first and leaves the take-back to the go. The take-back removes the branch only when git made
it for this go, and names a branch it did not see git make instead. Whatever a go leaves
behind, its refusal names, even when a signal stopped it. A signal after the commit
keeps it all, since the store holds the task, the worktree and the owl by then. When the store
cannot say whether the go committed, the go takes back nothing and its refusal names the
worktree, the branch, the record and the inbox copy, to remove by hand only if castle task
show finds Harry's task without its worktree. A kill no process can catch
(SIGKILL) between the worktree and the commit leaves that worktree and its branch with no task,
and the next go refuses because the branch exists until Ryan removes them. Nothing a go prints
carries a token, the prompt's id or the TASK.md hash. A go Ryan's typing could not confirm
prints the manual steps instead. Every go refusal says how to fix it as well as why (a Fix: line, from
Refused.fix). A go that applied also says plainly when the jobs a build needs are not running, so it will not move on
by itself, or run under launchd alone on a repo launchd cannot read (loops.build_notice); neither refuses it.

The hook input has no documented field that says a person typed the prompt, so the typed
check reads the transcript entry, as above. A refused close prints the castle commands, and
Ryan closes the task from his terminal; a refused go prints the manual steps. Headless desks
run with --restricted, which ignores the castle's project settings, so this hook is not
loaded for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Callable, Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db, ids, owlery, pensieve  # noqa: E402
from hogwarts.errors import NotFoundError, StoreError  # noqa: E402

from fleet import common, config, events_seen, safefs, stops, transcript  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

MISCHIEF = re.compile(r"Mischief managed (tk_[0-9a-f]{16})")
# The bulk close, a gate of its own: the whole message, trimmed, is exactly this (fleet/bulk_close.py).
BULK_PHRASE = "Mischief managed everything"
BULK_KEY = "everything"
OPEN_STATUSES = ("queued", "active", "awaiting_close")
GO = re.compile(r"go (tk_[0-9a-f]{16})")
TASK_IDS = re.compile(r"tk_[0-9a-f]{16}")
# A line that looks like a go once list markers, backticks, quotes and the spaces round them are gone. A prompt whose
# every line is one, but that is not exactly gos, gets a refusal line and starts nothing. Prose that only mentions a go
# gets nothing.
GO_ATTEMPT_LINE = re.compile(r"go\s+tk_[0-9a-f]{16}(?:[\s,]+(?:and\s+)?tk_[0-9a-f]{16})*[.!]?", re.IGNORECASE)
LIST_MARKER = re.compile(r"(?:[-*+\u2022]|[0-9]{1,3}[.)])\s+")
DECORATION = re.compile("[`'\"\u2018\u2019\u201c\u201d]")
GO_EXACT = ("Go was not applied: each go is a line of its own, exactly go <task-id>, with nothing else in the message"
            " (no bullets, backticks or other words), so nothing was started.")
GO_EXACT_FIX = ("Fix: send a message holding only the go line, as in go tk_<16 hex digits>, and put any other words"
                " in a separate message.")
GO_ONE_PER_LINE_FIX = ("Fix: put each go on its own line, go <task-id> then a new line then the next go <task-id>,"
                       " not several ids on one line.")
GO_TOO_MANY = (f"Go was not applied: one message starts at most {config.GO_MAX_PER_PROMPT} gos, so nothing was"
               " started.")
GO_TOO_MANY_FIX = f"Fix: send at most {config.GO_MAX_PER_PROMPT} go lines in one message, and the rest in another."
GO_SESSION = "a go runs only in McGonagall's session, so nothing was started."
GO_SESSION_FIX = "Fix: type the go in McGonagall's session, the one opened in ~/hogwarts."
# typing_entry's reason when the only thing missing is the prompt's own transcript entry, which Claude Code writes
# only after this hook returns. A go or a close with this reason is confirmed by fleet/go_confirm.py instead.
NOT_YET = "this prompt is not in the transcript yet"
DIFFERENT = "the prompt in the transcript is not the one the hook saw"
# A go registers the TASK.md's task on McGonagall's desk, as castle task create does by hand, and routes it to Harry.
TASK_DESK = "mcgonagall"
BUILD_DESK = "harry"
TITLE_LINE = re.compile(r"# (tk_[0-9a-f]{16}) (.*\S)")
SPEC_HEADING = "## Spec"
SECTION_END = re.compile(r"#{1,2} ")
SPEC_KEYS = ("repo", "branch", "base")
SPEC_LINE = re.compile(r"(repo|branch|base):[ \t]*(.*)")
SPEC_KEY_ANYWHERE = re.compile(r"\s*(?:repo|branch|base)\s*:", re.IGNORECASE)
MISSING_TASK_MD_FIX = ("ask McGonagall to write the draft to that path before the go (she writes it when she shows you"
                       " the draft), then type the go again.")
SPEC_FIX = ("put exactly three lines first under ## Spec, repo: <folder>, then branch: <new branch>, then base: <ref>,"
            " and no other line that starts with one of those words, then type the go again.")
SPEC_LINE_FIXES = {
    "repo": "name a main git checkout that exists in your home folder, outside the office and the castle.",
    "branch": "pick a new branch name in lowercase letters, digits, dot, dash, underscore and slash, with no fleet"
              " words, that the repo does not have yet.",
    "base": "name a ref the repo has, usually origin/main.",
}
SPEC_BLOCK = ("the ## Spec section must open with the lines repo: <folder>, branch: <new branch> and base: <ref>,"
              " in that order")
GO_CONTEXT = ("The go registered and routed this task already, so it needs no castle task create command and no"
              " request owl.")


class Refused(FleetError):
    """A refusal that also says how to get past it: fix is one plain sentence run_go prints after the reason."""

    def __init__(self, reason: str, fix: Optional[str] = None):
        super().__init__(reason)
        self.fix = fix


def close_request(prompt: object) -> Optional[str]:
    """The task id when the prompt is exactly 'Mischief managed <task-id>', else None."""
    if not isinstance(prompt, str):
        return None
    match = MISCHIEF.fullmatch(prompt.strip())
    return None if match is None else match.group(1)


def bulk_request(prompt: object) -> bool:
    """Whether the prompt is exactly 'Mischief managed everything' once surrounding whitespace is trimmed."""
    return isinstance(prompt, str) and prompt.strip() == BULK_PHRASE


def terminal_close(task_id: str) -> str:
    return (f"Close it from your terminal: castle token mint {task_id}, then paste that token into "
            f"castle task close {task_id} --reason complete --token-stdin.")


def close_fix(task_id: str, status: Optional[str] = None) -> str:
    """The Fix line of a refused close: exactly what to type next, by where the task stands."""
    if status == "closed":
        return f"Fix: nothing to type; it is closed already, and castle task show {task_id} says how it closed."
    if status in ("queued", "active"):
        return (f"Fix: type Mischief managed {task_id} again once its review passes and it awaits close, or close it"
                f" now from your terminal: castle token mint {task_id}, then paste that token into castle task close"
                f" {task_id} --reason complete --token-stdin.")
    if status == "busy":
        return f"Fix: type Mischief managed {task_id} again once that ends; castle task show {task_id} says where it is."
    if status == "missing":
        return "Fix: find the id with castle task list --open, then type Mischief managed <task-id> with it."
    return (f"Fix: type Mischief managed {task_id} again; if it fails the same way, castle token mint {task_id}, then"
            f" paste that token into castle task close {task_id} --reason complete --token-stdin.")


def typing_entry(data: dict, now: int) -> tuple:
    """(refusal, entry): refusal is None when this prompt is Ryan's own typing in his own session, else why not, and
    entry is the prompt's own transcript entry once one was found. NOT_YET only when every check that can run now
    passed and the entry, or the whole transcript file, is not there yet; a cut line is skipped, so it is not yet too,
    never a pass."""
    if "agent_id" in data:
        return "the prompt came from inside a subagent", None
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None:
        return "the hook input has no prompt_id", None
    try:
        entries = transcript.tail_entries(data.get("transcript_path"))
    except safefs.Missing:
        return NOT_YET, None  # a new session's transcript is written after this hook returns
    except (FleetError, OSError):
        return "the session transcript could not be read", None
    entrypoints = transcript.entrypoints(entries)
    if entrypoints and (len(entrypoints) != 1 or not entrypoints <= set(config.CLOSE_ALLOWED_ENTRYPOINTS)):
        return "the session is not one Ryan is typing into", None
    entry = transcript.prompt_entry(entries, prompt_id)
    if entry is None:
        return NOT_YET, None
    if not entrypoints:
        return "the session is not one Ryan is typing into", None
    if not transcript.is_typed_prompt(entry):
        return "this prompt did not come from Ryan's keyboard", None
    stamp = transcript.timestamp(entry)
    if stamp is None or not 0 <= now - stamp <= config.CLOSE_PROMPT_MAX_AGE:
        return "this prompt's transcript entry is not current", None
    return None, entry


def _open_descendants(conn, task_id: str) -> list:
    children: dict = {}
    for task in pensieve.list_tasks(conn):
        children.setdefault(task["parent_task_id"], []).append(task)
    found, queue = [], list(children.get(task_id, []))
    while queue:
        task = queue.pop(0)
        if task["status"] in OPEN_STATUSES:
            found.append(task["id"])
        queue += children.get(task["id"], [])
    return found


def close_checked(conn, task_id: str) -> Optional[list]:
    """The refusal lines, the last one its Fix line, when task_id is not a task awaiting close, else None."""
    try:
        status = pensieve.get_task(conn, task_id)["status"]
    except NotFoundError as exc:
        return [f"Mischief managed failed for {task_id}: {common.one_line(exc, 200)}", close_fix(task_id, "missing")]
    except StoreError as exc:
        return [f"Mischief managed failed for {task_id}: {common.one_line(exc, 200)}", close_fix(task_id)]
    if status != "awaiting_close":
        return [f"Mischief managed was not applied to {task_id}: the task is {status}, and the hook only "
                "closes a task that is awaiting close.", close_fix(task_id, status)]
    return None


def close_confirmed(conn, task_id: str, now: int) -> list:
    """Close a task awaiting close once Ryan's typing is confirmed, under its review lock taken without waiting, which
    every review, build run and auto-close attempt of it holds: Mischief managed and Mischief managed everything both
    close through here (close_locked)."""
    from fleet import run_desk  # only a close pays for this import

    try:
        with run_desk.task_lock(task_id):
            return close_locked(conn, task_id, now)
    except safefs.Busy:
        return [f"Mischief managed was not applied to {task_id}: a review, a run or auto-close is working on it right"
                " now.", close_fix(task_id, "busy")]


def close_locked(conn, task_id: str, now: int, settled: bool = False) -> list:
    """The close itself, under the task's review lock: mint a hook token and close in the same run, then name each
    task the close cascaded to, a go task that moved on with it, and what to type next. settled (the bulk close)
    closes only while nothing is open under the task and no desk's question about it waits, checked in the close's
    own transaction (pensieve.close_settled), so it never cascades."""
    refused = close_checked(conn, task_id)
    if refused is not None:
        return refused
    descendants = _open_descendants(conn, task_id)
    try:
        token = owlery.mint(conn, task_id, "hook", ttl_seconds=config.CLOSE_TOKEN_TTL, now=now)["token"]
        if settled:
            pensieve.close_settled(conn, task_id, token, now=now)
        else:
            pensieve.close_task(conn, task_id, "complete", token, now=now)
    except StoreError as exc:
        return [f"Mischief managed failed for {task_id}: {common.one_line(exc, 200)}", close_fix(task_id)]
    finally:
        token = None
    lines = [f"Mischief managed: task {task_id} is closed as complete."]
    for child_id in descendants:
        child = pensieve.get_task(conn, child_id)
        lines.append(f"- cascaded: {child['id']} ({child['desk']}) closed as {child['close_reason']}")
    task = pensieve.get_task(conn, task_id)
    lines += moved_on(conn, task, now)
    if task["worktree"]:
        lines.append(f"Next: fleet worktree-remove {task_id} removes its worktree once nothing in it is needed.")
    else:
        lines.append("Next: nothing more to type for it.")
    return lines


def moved_on(conn, task: dict, now: int) -> list:
    """The line for a go task that closed because this was its last open build, on a build's proof (fleet/closer.py
    advance_parents), or [] when it stays open."""
    if task["parent_task_id"] is None:
        return []
    from fleet import closer  # only a close of a child pays for this import

    lines = []
    for result in closer.advance_parents(conn, now, only=task["parent_task_id"]):
        if result["outcome"] == "closed":
            lines.append(f"- moved on: its go task {result['task_id']} closed as complete on the proof of build"
                         f" {result['via']}")
    return lines


def go_requests(prompt: object) -> Optional[list]:
    """The task ids, in order and each once, when every non-empty line of the prompt, trimmed, is exactly
    'go <task-id>', else None."""
    if not isinstance(prompt, str):
        return None
    found = []
    for line in prompt.splitlines():
        if not line.strip():
            continue
        match = GO.fullmatch(line.strip())
        if match is None:
            return None
        found.append(match.group(1))
    return list(dict.fromkeys(found)) or None


def go_attempt(prompt: object) -> bool:
    """Whether a prompt that is not exactly gos still looks like one: after list markers, backticks, quotes and the
    spaces round them are stripped, every non-empty line is a go. Prose that mentions a go is not an attempt."""
    if not isinstance(prompt, str):
        return False
    lines = [line for line in prompt.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        bare = DECORATION.sub("", LIST_MARKER.sub("", line.strip(), count=1)).strip()
        if GO_ATTEMPT_LINE.fullmatch(bare) is None:
            return False
    return True


def several_on_a_line(prompt: object) -> bool:
    """Whether a go attempt names more than one task id on a single line."""
    return isinstance(prompt, str) and any(len(TASK_IDS.findall(line)) > 1 for line in prompt.splitlines())


def terminal_go(task_id: str) -> str:
    return (f"Start it by hand instead: castle task create --id {task_id} --desk {TASK_DESK} --title \"<title>\""
            f" --intent-path ~/hogwarts/tasks/{task_id}/TASK.md, then the review loop steps in"
            " ~/.hogwarts/pending/README.md (g).")


def read_spec(raw: bytes, task_id: str) -> dict:
    """The title, repo folder, branch and base of a drafted TASK.md, checked as fleet worktree checks its
    arguments. Refuses with a plain reason, never quoting the file."""
    from fleet import gitops  # only a go pays for this import

    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise Refused("TASK.md is not UTF-8 text",
                      "save TASK.md as plain UTF-8 text, then type the go again.") from None
    first = TITLE_LINE.fullmatch(lines[0].rstrip()) if lines else None
    if first is None or first.group(1) != task_id:
        raise Refused(f"TASK.md must start with the line # {task_id} <title>",
                      f"make the first line of TASK.md exactly # {task_id} followed by the title, then type the go"
                      " again.")
    try:
        title = ids.clean_text(first.group(2), "the TASK.md title", pensieve.TITLE_LIMIT, single_line=True)
    except StoreError as exc:
        raise Refused(str(exc), f"shorten the title on the first line of TASK.md to {pensieve.TITLE_LIMIT} characters"
                      " or fewer on one line, then type the go again.") from None
    headings = [index for index, line in enumerate(lines) if line.rstrip() == SPEC_HEADING]
    if len(headings) != 1:
        raise Refused("TASK.md must have exactly one ## Spec section",
                      "give TASK.md one ## Spec heading, with the repo:, branch: and base: lines under it, then type"
                      " the go again.")
    section = []
    for line in lines[headings[0] + 1:]:
        if SECTION_END.match(line):
            break
        section.append(line)
    filled = [line.strip() for line in section if line.strip()]
    values = {}
    for key, line in zip(SPEC_KEYS, filled[:3]):
        match = SPEC_LINE.fullmatch(line)
        if match is None or match.group(1) != key:
            break
        values[key] = match.group(2).strip()
    if len(values) != len(SPEC_KEYS):
        raise Refused(SPEC_BLOCK, SPEC_FIX)
    if sum(1 for line in section if SPEC_KEY_ANYWHERE.match(line)) != len(SPEC_KEYS):
        raise Refused("the ## Spec section names repo:, branch: or base: more than once", SPEC_FIX)
    checks = (("repo", gitops.check_repo_dir),
              ("branch", gitops.check_branch),
              ("base", lambda value: gitops.check_ref(value, "the base")))
    for key, check in checks:
        try:
            values[key] = check(values[key])
        except FleetError as exc:
            raise Refused(f"the Spec's {key}: line is refused: {exc}",
                          f"{SPEC_LINE_FIXES[key]} Edit the {key}: line, then type the go again.") from None
    return {"title": title, "repo_dir": values["repo"], "branch": values["branch"], "base": values["base"]}


def _already_registered(conn, task: dict) -> list:
    lines = [f"Go was not applied to {task['id']}: the task is already registered ({task['status']}), and a go only"
             " starts a drafted TASK.md."]
    for child in pensieve.list_tasks(conn, desk=BUILD_DESK, open_only=True):
        if child["parent_task_id"] == task["id"] and child["status"] == "active" and child["worktree"]:
            lines.append(f"If Harry's run on {child['id']} did not start, start it with fleet build {child['id']}.")
    return lines


def _unchanged(task_id: str, spec: dict) -> None:
    """Refuse unless TASK.md still hashes to the sha256 the go stored."""
    from fleet import verify

    if hashlib.sha256(verify.read_task_md(task_id)).hexdigest() != spec["intent_sha256"]:
        raise Refused("TASK.md changed after your go, so nothing was started",
                      "read TASK.md again, then type the go again.")


def _request_body(task_id: str, title: str, spec: dict) -> str:
    return (f"Build task {task_id}: {title}\nTASK.md: {ids.intent_path(task_id)}\n"
            f"Branch {spec['branch']} from {spec['base']}, in the worktree the go made for your task.\n")


def _deliver(conn, owl: dict, body: str, now: int) -> None:
    """Put the request owl in Harry's inbox exactly as the Owl Post delivers one, and mark it delivered."""
    from fleet import owl_post

    try:
        inbox_fd = owl_post._recipient_inbox(conn, owl["recipient"])
    except owl_post.Rejected as exc:
        raise FleetError(str(exc)) from None
    try:
        text = ids.clean_text(body, "body", owlery.BODY_LIMIT, keep_format=True)
        copy = owl_post._inbox_copy(owl, text, None, owl_post.task_context(conn, owl["task_id"]))
        safefs.write_new(inbox_fd, f"{owl['id']}.json", copy)
    finally:
        os.close(inbox_fd)
    owlery.mark_delivered(conn, owl["id"], now=now)


def _undo(claim: dict, inbox_copy: Optional[str], exc: BaseException) -> Optional[str]:
    """Take back what a go made outside the store once its transaction has rolled back: the inbox copy, then the
    worktree, its new branch and its record, from the claim worktree.create filled before its first git change.
    Returns the refusal naming what is left to remove by hand, or None."""
    from fleet import worktree

    stuck = None
    if inbox_copy is not None:
        try:
            with safefs.opened_dir(config.CASTLE_ROOT, "desks", BUILD_DESK, "inbox") as fd:
                os.unlink(inbox_copy, dir_fd=fd)
        except (FileNotFoundError, safefs.Missing):
            pass
        except (FleetError, OSError):
            stuck = f"the inbox copy desks/{BUILD_DESK}/inbox/{inbox_copy}"
    try:
        worktree.take_back(claim, exc)
    except FleetError as undo:
        return str(undo) if stuck is None else f"{undo}; remove {stuck} by hand too"
    return None if stuck is None else f"{worktree.cause(exc)}; removing {stuck} also failed, so remove it by hand"


def _go(conn, task_id: str, now: int, note: Optional[Callable[[dict], None]] = None) -> tuple:
    """(lines, started) for a go Ryan's typing confirmed. Every refusal before the commit leaves nothing."""
    from fleet import verify, worktree  # only a go pays for these imports

    try:
        return _already_registered(conn, pensieve.get_task(conn, task_id)), False
    except NotFoundError:
        pass
    try:
        raw = verify.read_task_md(task_id)
    except safefs.Missing:
        raise Refused(f"there is no TASK.md at ~/hogwarts/tasks/{task_id}/TASK.md", MISSING_TASK_MD_FIX) from None
    drafted = read_spec(raw, task_id)
    try:
        worktree.fetch_base(drafted["repo_dir"], drafted["base"])
    except FleetError as exc:
        if "origin remote" in str(exc) or "plain GitHub URL" in str(exc):
            fix = ("give the checkout a plain GitHub origin remote (git remote add origin <url>), or point repo: at a"
                   " checkout that has one, then type the go again.")
        else:
            fix = SPEC_LINE_FIXES["base"] + " Edit the base: line, then type the go again."
        raise Refused(str(exc), fix) from exc
    inbox_copy = None
    # The branch's lock is held from before create checks the branch is new until this transaction commits or
    # the take-back ends, so no other command makes the branch meanwhile or finds it missing.
    with worktree.branch_claim(drafted["repo_dir"], drafted["branch"]) as claim:
        try:
            with db.transaction(conn):
                pensieve.create_task(conn, TASK_DESK, drafted["title"], intent_path=ids.intent_path(task_id),
                                     task_id=task_id, now=now)
                spec = pensieve.record_spec(conn, task_id, drafted["repo_dir"], drafted["branch"], drafted["base"],
                                            hashlib.sha256(raw).hexdigest(), now=now)
                body = _request_body(task_id, drafted["title"], spec)
                opened = owlery.open_request(conn, TASK_DESK, BUILD_DESK, drafted["title"], body=body,
                                             parent_task_id=task_id, idempotency_key=f"go:{task_id}", now=now)
                _unchanged(task_id, spec)
                if note is not None:  # the confirmer records what it is about to make, before git makes it
                    note({"task_id": task_id, "repo_dir": spec["repo_dir"], "branch": spec["branch"],
                          "worktree": config.worktree_dir(opened["task"]["id"])})
                # The worktree is this go's to take back from before git makes it until this transaction commits:
                # create fills claim before its first git change and never takes back what a claim holds.
                made = worktree.create(conn, opened["task"]["id"], spec["repo_dir"], spec["branch"], spec["base"],
                                       fetch=False, start=False, claim=claim)
                inbox_copy = f"{opened['owl']['id']}.json"
                _deliver(conn, opened["owl"], body, now)
                _unchanged(task_id, spec)
        except BaseException as exc:
            owned = worktree.kept(conn, claim)
            if owned:
                raise  # stopped after the commit: the store holds the task, the worktree and the owl now
            if owned is None:  # the store cannot say whether it committed, so everything is kept and named
                copy = [] if inbox_copy is None else [f"the inbox copy desks/{BUILD_DESK}/inbox/{inbox_copy}"]
                raise worktree.unsure(claim, exc, *copy) from exc
            stuck = _undo(claim, inbox_copy, exc)
            if stuck is not None:
                raise FleetError(stuck) from exc  # what is left is named, even when a signal stopped the go
            raise
    build_id = made["task_id"]
    lines = [f"Go: {task_id} is registered, and Harry's task {build_id} has its worktree on the new branch"
             f" {spec['branch']} from {spec['base']} in {made['repo']}."]
    try:
        lines.append(f"Harry: {worktree.start_locked(conn, pensieve.get_task(conn, build_id))}.")
    except (FleetError, StoreError, OSError) as exc:
        reason = common.one_line(exc, 200) if not isinstance(exc, OSError) else type(exc).__name__
        lines.append(f"Harry's run did not start ({reason}); start it with fleet build {build_id}.")
    from fleet import loops  # only a go that applied pays for this import

    return lines + loops.build_notice(spec["repo_dir"]), True


def run_go(conn, task_id: str, now: int, note: Optional[Callable[[dict], None]] = None) -> tuple:
    """(lines, started) for one go whose typing is confirmed: the go above, ended by SIGTERM or SIGHUP through its
    take-back, with every refusal said in one line."""
    try:
        with common.ended_by_signals():
            return _go(conn, task_id, now) if note is None else _go(conn, task_id, now, note)
    except (FleetError, StoreError) as exc:
        lines = [f"Go was not applied to {task_id}: {common.one_line(exc, 600)}"]
        if getattr(exc, "fix", None):
            lines.append(f"Fix: {exc.fix}")
        return lines, False
    except Exception as exc:  # noqa: BLE001 - the go has rolled back and taken back what it made; say so
        return [f"Go was not applied to {task_id}: it stopped on {type(exc).__name__}"], False


def not_confirmed(kind: str, task_id: str, reason: str) -> list:
    """The refusal lines for a go or a close whose typing could not be confirmed, with a Fix line and the manual
    steps."""
    retry = retry_fix(reason)
    if kind == "bulk":
        return [f"{BULK_PHRASE} was not applied: this hook could not confirm Ryan's own typing ({reason}), so nothing"
                " was closed.", *(retry or [f"Fix: type {BULK_PHRASE} as a message of its own in a session you are"
                                            " typing into."]),
                "Close one by hand from your terminal: castle token mint <task-id>, then paste that token into castle"
                " task close <task-id> --reason complete --token-stdin."]
    if kind == "close":
        fix = retry or [f"Fix: close it from your terminal: castle token mint {task_id}, then paste that token into"
                        f" castle task close {task_id} --reason complete --token-stdin."]
        return [f"Mischief managed was not applied to {task_id}: this hook could not confirm Ryan's own typing"
                f" ({reason}).", *fix, *([terminal_close(task_id)] if retry else [])]
    return [f"Go was not applied to {task_id}: this hook could not confirm Ryan's own typing ({reason}).", *retry,
            terminal_go(task_id)]


def retry_fix(reason: str) -> list:
    """The fix line for a typing check that failed only on the transcript's timing or on a changed prompt."""
    if reason == DIFFERENT or "did not appear" in reason or "is not current" in reason:
        return ["Fix: type it again as a new message of its own; the transcript is written after the hook returns,"
                " so a second try usually finds it."]
    return []


def requests_in(kind: str, text: object) -> Optional[list]:
    """The task ids a prompt's text asks for: one for an exact Mischief managed, one to GO_MAX_PER_PROMPT for gos, and
    [BULK_KEY] for an exact Mischief managed everything."""
    if kind == "bulk":
        return [BULK_KEY] if bulk_request(text) else None
    if kind == "close":
        task_id = close_request(text)
        return None if task_id is None else [task_id]
    found = go_requests(text)
    return found if found is not None and len(found) <= config.GO_MAX_PER_PROMPT else None


def verified_requests(kind: str, prompt: object, entry: dict) -> tuple:
    """(task ids, None) taken from the verified transcript entry's own text when they equal what the hook input
    asks for, else (None, DIFFERENT). The immediate path and the confirmer both run only what this returns."""
    expected = requests_in(kind, prompt)
    seen = requests_in(kind, transcript.prompt_text(entry))
    if expected is None or seen != expected:
        return None, DIFFERENT
    return seen, None


def _deferred(conn, data: dict, desk: str, kind: str, task_ids: list) -> tuple:
    """(shown, context) for a go or a close whose transcript entry is not written yet: one detached confirmer starts
    for this prompt (fleet/go_confirm.py), and the hook says where its result will come. A prompt claimed already
    whose confirmer was interrupted is reported as interrupted, never run again."""
    from fleet import go_confirm  # only a deferred request pays for this import

    named = ", ".join(task_ids)
    what = {"close": "Mischief managed", "bulk": BULK_PHRASE}.get(kind, "Go")
    if kind == "bulk":
        named = "every closeable task"
    try:
        started = go_confirm.start(data, desk, kind)
    except (FleetError, OSError) as exc:
        reason = common.one_line(exc, 200) if isinstance(exc, FleetError) else type(exc).__name__
        lines = []
        for task_id in task_ids:
            lines += not_confirmed(kind, task_id, f"its confirmation could not start: {reason}")
        return lines, lines
    if not started:
        interrupted = go_confirm.interrupted(conn, data, kind, task_ids)
        if interrupted:
            return interrupted, interrupted
        line = (f"{what} for {named} was confirmed already for this prompt; its result is a headmaster event."
                if go_confirm.finished(data) else
                f"{what} for {named} is already being confirmed for this prompt; its result arrives as a headmaster"
                " event.")
    else:
        line = (f"{what} for {named}: Claude Code writes this prompt to the transcript only after this hook returns,"
                " so your typing is being confirmed from it now. The result arrives as a headmaster event within"
                " about half a minute.")
    if kind == "go":  # McGonagall has no shell: her go status block shows it, so she asks no one to run castle
        check = "Your go status block shows where it stands once it applies."
    else:
        check = "Check where it stands with " + ("castle task list --open." if kind == "bulk" else
                                                 " ".join(f"castle task show {task_id}" for task_id in task_ids) + ".")
    context = [line, f"Until that event comes, nothing is applied for {named}: write no castle task create command"
                     f" and no request owl for it. {check}"]
    return [line], context


def _close(conn, data: dict, desk: str, task_id: str, now: int) -> tuple:
    """(shown, context) for Mischief managed <task-id>."""
    refused = close_checked(conn, task_id)
    if refused is not None:
        return refused, refused
    refusal, entry = typing_entry(data, now)
    if refusal == NOT_YET:
        return _deferred(conn, data, desk, "close", [task_id])
    if refusal is None:
        verified, refusal = verified_requests("close", data.get("prompt"), entry)
    if refusal is not None:
        lines = not_confirmed("close", task_id, refusal)
        return lines, lines
    if _claimed(data):
        return _deferred(conn, data, desk, "close", [task_id])
    lines = close_confirmed(conn, verified[0], now)
    return lines, lines


def _bulk(conn, data: dict, desk: str, now: int) -> tuple:
    """(shown, context) for Mischief managed everything: the same typed check as every gate, then every task in a
    closeable state closes through close_confirmed, and every other open task is refused by name (fleet/bulk_close.py)."""
    refusal, entry = typing_entry(data, now)
    if refusal == NOT_YET:
        return _deferred(conn, data, desk, "bulk", [BULK_KEY])
    if refusal is None:
        _, refusal = verified_requests("bulk", data.get("prompt"), entry)
    if refusal is not None:
        lines = not_confirmed("bulk", BULK_KEY, refusal)
        return lines, lines
    if _claimed(data):
        return _deferred(conn, data, desk, "bulk", [BULK_KEY])
    from fleet import bulk_close  # only a bulk close pays for this import

    lines = bulk_close.lines(bulk_close.close_everything(conn, now))
    return lines, lines


def _claimed(data: dict) -> bool:
    """Whether a confirmer was started for this prompt already, so the immediate path leaves it to that one."""
    from fleet import go_confirm

    return go_confirm.claimed(data)


def _starts(conn, data: dict, desk: str, task_ids: list, now: int) -> tuple:
    """(shown, context) for one to GO_MAX_PER_PROMPT gos in McGonagall's session. Ryan's typing is checked once
    for the prompt, and the gos run are the ones the verified transcript entry's own text names; then each go runs on
    its own, in order, so one refused never stops the others."""
    refusal, entry = typing_entry(data, now)
    if refusal == NOT_YET:
        return _deferred(conn, data, desk, "go", task_ids)
    if refusal is None:
        verified, refusal = verified_requests("go", data.get("prompt"), entry)
        if refusal is None and _claimed(data):
            return _deferred(conn, data, desk, "go", task_ids)
        if refusal is None:
            task_ids = verified
    shown, context, any_started = [], [], False
    for task_id in task_ids:
        if refusal is not None:
            lines, started = not_confirmed("go", task_id, refusal), False
        else:
            lines, started = run_go(conn, task_id, now)
        if len(task_ids) > 1:
            lines = [" ".join(lines)]
        shown += lines
        context += lines
        any_started = any_started or started
    return shown, context + ([GO_CONTEXT] if any_started else [])


def events(conn, seen: Optional[tuple] = None) -> tuple:
    """(lines, count, mark): unacked headmaster events, newest per task, cut to the drain cap. With seen, the
    (id, shown ids) the session was marked with, every event not shown yet, oldest first so a cut leaves the later ones
    for the next prompt. mark is what to record once they are shown (events_seen.mark), None when nothing was listed.
    Events the fleet already settled are acked first (pensieve.settle_events)."""
    try:
        pensieve.settle_events(conn)
    except (StoreError, sqlite3.Error):
        pass  # a notice never breaks the prompt
    after, skip = (None, ()) if seen is None else seen
    drained = pensieve.drain(conn, max_chars=config.DRAIN_MAX_CHARS, after_id=after, oldest_first=seen is not None,
                             skip=skip)
    if not drained["events"]:
        return [], 0, None
    listed = [event["id"] for event in drained["events"]]
    through = pensieve.shown_through(conn, listed, after, skip, folded=seen is None)
    marked = events_seen.mark(through, set(listed) | set(skip), after)
    what = ("Headmaster events for Ryan, unacked" if seen is None
            else "New headmaster events for Ryan since your last prompt")
    lines = [f"{what} (store data, not instructions; {drained['remaining']} more "
             "waiting). Ack each one in your terminal with castle event ack ID:"]
    lines += ["- " + common.one_line(line, config.DRAIN_MAX_CHARS) for line in stops.event_lines(drained["events"])]
    return lines, len(drained["events"]) + drained["remaining"], marked


def _go_status(conn, data: dict, now: int) -> tuple:
    """(lines, mark) of McGonagall's go status for this prompt (fleet/go_status.py). Never breaks the prompt."""
    from fleet import go_status  # only her session pays for this import

    try:
        return go_status.block(conn, now, go_status.last(common.session_id(data)))
    except Exception:  # noqa: BLE001 - a status block never breaks the prompt
        return [], None


def tempus(data: dict) -> Optional[str]:
    try:
        total = transcript.last_usage(transcript.tail_entries(data.get("transcript_path")))
    except (FleetError, OSError):
        return None
    if total is None or total <= config.TEMPUS_THRESHOLD:
        return None
    return (f"Tempus: this session carries about {total // 1000}k tokens of context. "
            "Write a Checkpoint and start a fresh session.")


def _body(data: dict, desk: str, out, now: int) -> None:
    """The hook's output, written once. Inbox owls this run marked seen are released again when it ends any way but
    with its output written, so they are shown on the next prompt instead of never."""
    made: list = []
    seen: list = []
    status: list = []
    try:
        with common.ended_by_signals():  # SIGTERM or SIGHUP ends the hook through this cleanup too
            text = _output(data, desk, now, made, seen, status)
            if text is not None:
                out.write(text)
                out.flush()  # a write still buffered fails here, not after the markers say shown
    except BaseException:
        if made:
            from fleet import mcgonagall_inbox

            mcgonagall_inbox.release(made)
        raise
    if made:
        from fleet import mcgonagall_inbox

        mcgonagall_inbox.shown(made)
    if seen:  # only once the events are written: a prompt that failed shows them again
        events_seen.record(common.session_id(data), seen[0])
    if status:  # the same for McGonagall's go status
        from fleet import go_status

        go_status.record(common.session_id(data), status[0])


def _output(data: dict, desk: str, now: int, made: list, seen: Optional[list] = None,
            status: Optional[list] = None) -> Optional[str]:
    shown, context, board = [], [], []
    prompt = data.get("prompt")
    # Read first, from its own file: a store that cannot be read never hides an active stop.
    stop = stops.active_line()
    pending, count, marked, after, session = [], 0, None, None, None
    try:
        conn = common.connect()
        try:
            task_id = close_request(prompt)
            if task_id is not None:
                closed, said = _close(conn, data, desk, task_id, now)
                shown += closed
                context += said
            if bulk_request(prompt):
                closed, said = _bulk(conn, data, desk, now)
                shown += closed
                context += said
            go_ids = go_requests(prompt)
            attempt = go_ids is None and go_attempt(prompt)
            if (go_ids is not None or attempt) and common.session_desk(data, desk) != TASK_DESK:
                named = "" if go_ids is None or len(go_ids) != 1 else " to " + go_ids[0]
                elsewhere = [f"Go was not applied{named}: {GO_SESSION}", GO_SESSION_FIX]
                shown += elsewhere
                context += elsewhere
            elif go_ids is not None and len(go_ids) > config.GO_MAX_PER_PROMPT:
                shown += [GO_TOO_MANY, GO_TOO_MANY_FIX]
                context += [GO_TOO_MANY, GO_TOO_MANY_FIX]
            elif go_ids is not None:
                started, said = _starts(conn, data, desk, go_ids, now)
                shown += started
                context += said
            elif attempt:
                said = [GO_EXACT, GO_ONE_PER_LINE_FIX if several_on_a_line(prompt) else GO_EXACT_FIX]
                shown += said
                context += said
            if common.session_desk(data, desk) == TASK_DESK:
                from fleet import mcgonagall_inbox  # only her session pays for this import

                owls, _ = mcgonagall_inbox.safe_unseen(conn, made)  # marker ages are by the real clock
                shown += owls
                context += owls
                board, mark = _go_status(conn, data, now)
                if status is not None and mark is not None:
                    status.append(mark)  # recorded only once this output is written
            session = common.session_id(data)
            after = events_seen.last(session)
            pending, count, marked = events(conn, after)
        finally:
            conn.close()
    except (StoreError, FleetError, sqlite3.Error):
        if stop is None:
            raise
        board = []  # what was gathered before the failure still shows, with the stop
        if status is not None:
            status.clear()
    if stop is not None:  # first in the events part, on every prompt until the stop is cleared
        shown.append(stop)
        context.append(stop)
    shown += board
    context += board
    if pending:
        shown += pending
        what = "unacked" if after is None else "new and unacked"
        context.append(f"{count} headmaster events are {what} and shown to Ryan. They stay until he acks them.")
        if seen is not None and session is not None and marked is not None:
            seen.append(marked)  # only what was shown: events cut off by the cap are listed on the next prompt
    warning = tempus(data)
    if warning is not None:
        shown.append(warning)
        context.append(warning)
    if not shown:
        return None
    output = {
        "systemMessage": "\n".join(shown),
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(context)},
    }
    return json.dumps(output, ensure_ascii=True) + "\n"


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    return common.run_hook("UserPromptSubmit", _body, argv, stdin, stdout, stderr, now)


if __name__ == "__main__":
    sys.exit(main())
