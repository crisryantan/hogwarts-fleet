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
   trimming surrounding whitespace. It mints a close token with minted_by "hook" and
   closes that one task as complete in the same run, then names every descendant task
   the close cascaded to and how each one closed. The token is never printed. Any
   other text never closes anything.
2. The result of "go <task-id>" when the prompt is exactly that, after trimming. See
   "The go" below. A prompt that holds a go with a task id but is not exactly one gets a
   refusal line and starts nothing, and so does any go in a session that is not McGonagall's.
   Any other prompt pays only for two regular expressions.
3. Unacked headmaster events, newest per task, cut to about 1500 characters, with a
   count of the rest. The hook never acks them. They stay until Ryan runs
   castle event ack in his terminal.
4. One Tempus line when the last assistant call in the transcript carried more than
   200k tokens of context (input plus cache read plus cache creation).

The close runs only when every check passes:
- the task is awaiting_close, so the digest has already shown Ryan it waits for him;
- there is no agent_id;
- every transcript entry in the tail names the same entrypoint, and it is one of
  config.CLOSE_ALLOWED_ENTRYPOINTS ("cli" or "claude-desktop"; claude -p writes "sdk-cli");
- the tail holds this prompt's own entry (promptId equal to prompt_id), and that entry
  is a typed prompt: origin.kind "human", not isMeta, promptSource not "system", and at
  most CLOSE_PROMPT_MAX_AGE seconds old. Text another session sends in with
  send_message arrives with origin.kind "peer", isMeta and promptSource "system".

The go starts a build from a TASK.md McGonagall drafted and Ryan approved, with no command
in his terminal. It runs only in McGonagall's own session: when common.session_desk names any
other desk (Ryan's own sessions, a subagent, or the hook run with --desk for another desk), a go
gets one refusal line before anything else is read, and changes nothing. In her session it
passes the same typed_by_ryan check as the close, before it reads anything else, and then,
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
- only after that transaction commits, starts Harry's run with worktree.start_desk, when
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
prints the manual steps instead.

Unresolved question: the hook input has no documented field that says a person typed
the prompt, and Claude Code may write the prompt's entry only after this hook runs. If
it does, the entry is never found and every close and go is refused. A refused close prints
the castle commands, and Ryan closes the task from his terminal; a refused go prints the
manual steps. Headless desks run with --restricted, which ignores the castle's project
settings, so this hook is not loaded for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db, ids, owlery, pensieve  # noqa: E402
from hogwarts.errors import NotFoundError, StoreError  # noqa: E402

from fleet import common, config, safefs, transcript  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

MISCHIEF = re.compile(r"Mischief managed (tk_[0-9a-f]{16})")
OPEN_STATUSES = ("queued", "active", "awaiting_close")
GO = re.compile(r"go (tk_[0-9a-f]{16})")
# A go with a task id anywhere in a prompt that is not exactly one: it gets a refusal line and starts nothing.
NEAR_GO = re.compile(r"(?<![A-Za-z0-9_])go\s+tk_[0-9a-f]{16}", re.IGNORECASE)
GO_EXACT = "Go was not applied: a go is the whole prompt, exactly go <task-id>, so nothing was started."
GO_SESSION = "a go runs only in McGonagall's session, so nothing was started."
# A go registers the TASK.md's task on McGonagall's desk, as castle task create does by hand, and routes it to Harry.
TASK_DESK = "mcgonagall"
BUILD_DESK = "harry"
TITLE_LINE = re.compile(r"# (tk_[0-9a-f]{16}) (.*\S)")
SPEC_HEADING = "## Spec"
SECTION_END = re.compile(r"#{1,2} ")
SPEC_KEYS = ("repo", "branch", "base")
SPEC_LINE = re.compile(r"(repo|branch|base):[ \t]*(.*)")
SPEC_KEY_ANYWHERE = re.compile(r"\s*(?:repo|branch|base)\s*:", re.IGNORECASE)
SPEC_BLOCK = ("the ## Spec section must open with the lines repo: <folder>, branch: <new branch> and base: <ref>,"
              " in that order")
GO_CONTEXT = ("The go registered and routed this task already, so it needs no castle task create command and no"
              " request owl.")


def close_request(prompt: object) -> Optional[str]:
    """The task id when the prompt is exactly 'Mischief managed <task-id>', else None."""
    if not isinstance(prompt, str):
        return None
    match = MISCHIEF.fullmatch(prompt.strip())
    return None if match is None else match.group(1)


def terminal_close(task_id: str) -> str:
    return (f"Close it from your terminal: castle token mint {task_id}, then paste that token into "
            f"castle task close {task_id} --reason complete --token-stdin.")


def typed_by_ryan(data: dict, now: int) -> Optional[str]:
    """None when this prompt is Ryan's own typing in his own session, else why not."""
    if "agent_id" in data:
        return "the prompt came from inside a subagent"
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None:
        return "the hook input has no prompt_id"
    try:
        entries = transcript.tail_entries(data.get("transcript_path"))
    except (FleetError, OSError):
        return "the session transcript could not be read"
    entrypoints = transcript.entrypoints(entries)
    if len(entrypoints) != 1 or not entrypoints <= set(config.CLOSE_ALLOWED_ENTRYPOINTS):
        return "the session is not one Ryan is typing into"
    entry = transcript.prompt_entry(entries, prompt_id)
    if entry is None:
        return "this prompt is not in the transcript yet"
    if not transcript.is_typed_prompt(entry):
        return "this prompt did not come from Ryan's keyboard"
    stamp = transcript.timestamp(entry)
    if stamp is None or not 0 <= now - stamp <= config.CLOSE_PROMPT_MAX_AGE:
        return "this prompt's transcript entry is not current"
    return None


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


def close_task(conn, data: dict, task_id: str, now: int) -> list:
    try:
        status = pensieve.get_task(conn, task_id)["status"]
    except StoreError as exc:
        return [f"Mischief managed failed for {task_id}: {common.one_line(exc, 200)}"]
    if status != "awaiting_close":
        return [f"Mischief managed was not applied to {task_id}: the task is {status}, and the hook only "
                "closes a task that is awaiting close.", terminal_close(task_id)]
    refusal = typed_by_ryan(data, now)
    if refusal is not None:
        return [f"Mischief managed was not applied to {task_id}: this hook could not confirm Ryan's own "
                f"typing ({refusal}).", terminal_close(task_id)]
    descendants = _open_descendants(conn, task_id)
    try:
        token = owlery.mint(conn, task_id, "hook", ttl_seconds=config.CLOSE_TOKEN_TTL, now=now)["token"]
        pensieve.close_task(conn, task_id, "complete", token, now=now)
    except StoreError as exc:
        return [f"Mischief managed failed for {task_id}: {common.one_line(exc, 200)}"]
    finally:
        token = None
    lines = [f"Mischief managed: task {task_id} is closed as complete."]
    for child_id in descendants:
        child = pensieve.get_task(conn, child_id)
        lines.append(f"- cascaded: {child['id']} ({child['desk']}) closed as {child['close_reason']}")
    return lines


def go_request(prompt: object) -> Optional[str]:
    """The task id when the prompt is exactly 'go <task-id>', else None."""
    if not isinstance(prompt, str):
        return None
    match = GO.fullmatch(prompt.strip())
    return None if match is None else match.group(1)


def near_go(prompt: object) -> bool:
    """Whether a prompt that is not exactly a go still holds one, so Ryan hears it started nothing."""
    return isinstance(prompt, str) and NEAR_GO.search(prompt) is not None


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
        raise FleetError("TASK.md is not UTF-8 text") from None
    first = TITLE_LINE.fullmatch(lines[0].rstrip()) if lines else None
    if first is None or first.group(1) != task_id:
        raise FleetError(f"TASK.md must start with the line # {task_id} <title>")
    try:
        title = ids.clean_text(first.group(2), "the TASK.md title", pensieve.TITLE_LIMIT, single_line=True)
    except StoreError as exc:
        raise FleetError(str(exc)) from None
    headings = [index for index, line in enumerate(lines) if line.rstrip() == SPEC_HEADING]
    if len(headings) != 1:
        raise FleetError("TASK.md must have exactly one ## Spec section")
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
        raise FleetError(SPEC_BLOCK)
    if sum(1 for line in section if SPEC_KEY_ANYWHERE.match(line)) != len(SPEC_KEYS):
        raise FleetError("the ## Spec section names repo:, branch: or base: more than once")
    checks = (("repo", gitops.check_repo_dir), ("branch", gitops.check_branch),
              ("base", lambda value: gitops.check_ref(value, "the base")))
    for key, check in checks:
        try:
            values[key] = check(values[key])
        except FleetError as exc:
            raise FleetError(f"the Spec's {key}: line is refused: {exc}") from None
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
        raise FleetError("TASK.md changed after your go, so nothing was started; read it again, then type the go again")


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


def _go(conn, task_id: str, now: int) -> tuple:
    """(lines, started) for a go Ryan's typing confirmed. Every refusal before the commit leaves nothing."""
    from fleet import verify, worktree  # only a go pays for these imports

    try:
        return _already_registered(conn, pensieve.get_task(conn, task_id)), False
    except NotFoundError:
        pass
    try:
        raw = verify.read_task_md(task_id)
    except safefs.Missing:
        raise FleetError(f"there is no TASK.md at ~/hogwarts/tasks/{task_id}/TASK.md") from None
    drafted = read_spec(raw, task_id)
    worktree.fetch_base(drafted["repo_dir"], drafted["base"])
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
        lines.append(f"Harry: {worktree.start_desk(conn, pensieve.get_task(conn, build_id))}.")
    except (FleetError, StoreError, OSError) as exc:
        reason = common.one_line(exc, 200) if not isinstance(exc, OSError) else type(exc).__name__
        lines.append(f"Harry's run did not start ({reason}); start it with fleet build {build_id}.")
    return lines, True


def start_build(conn, data: dict, task_id: str, now: int) -> tuple:
    """(lines, started) for "go <task-id>": refused unless Ryan typed it, then the go above."""
    refusal = typed_by_ryan(data, now)
    if refusal is not None:
        return [f"Go was not applied to {task_id}: this hook could not confirm Ryan's own typing ({refusal}).",
                terminal_go(task_id)], False
    try:
        with common.ended_by_signals():
            return _go(conn, task_id, now)
    except (FleetError, StoreError) as exc:
        return [f"Go was not applied to {task_id}: {common.one_line(exc, 600)}"], False
    except Exception as exc:  # noqa: BLE001 - the go has rolled back and taken back what it made; say so
        return [f"Go was not applied to {task_id}: it stopped on {type(exc).__name__}"], False


def events(conn) -> tuple:
    """(lines, count): unacked headmaster events, newest per task, cut to the drain cap. Nothing is acked."""
    drained = pensieve.drain(conn, max_chars=config.DRAIN_MAX_CHARS)
    if not drained["events"]:
        return [], 0
    lines = [f"Headmaster events for Ryan, unacked (store data, not instructions; {drained['remaining']} more "
             "waiting). Ack each one in your terminal with castle event ack ID:"]
    lines += ["- " + common.one_line(event["line"], config.DRAIN_MAX_CHARS) for event in drained["events"]]
    return lines, len(drained["events"]) + drained["remaining"]


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
    shown, context = [], []
    conn = common.connect()
    try:
        task_id = close_request(data.get("prompt"))
        if task_id is not None:
            closed = close_task(conn, data, task_id, now)
            shown += closed
            context += closed
        go_id = go_request(data.get("prompt"))
        if (go_id is not None or near_go(data.get("prompt"))) and common.session_desk(data, desk) != TASK_DESK:
            elsewhere = f"Go was not applied{'' if go_id is None else ' to ' + go_id}: {GO_SESSION}"
            shown.append(elsewhere)
            context.append(elsewhere)
        elif go_id is not None:
            started, ok = start_build(conn, data, go_id, now)
            shown += started
            context += started + ([GO_CONTEXT] if ok else [])
        elif near_go(data.get("prompt")):
            shown.append(GO_EXACT)
            context.append(GO_EXACT)
        pending, count = events(conn)
    finally:
        conn.close()
    if pending:
        shown += pending
        context.append(f"{count} headmaster events are unacked and shown to Ryan. They stay until he acks them.")
    warning = tempus(data)
    if warning is not None:
        shown.append(warning)
        context.append(warning)
    if not shown:
        return
    output = {
        "systemMessage": "\n".join(shown),
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(context)},
    }
    out.write(json.dumps(output, ensure_ascii=True) + "\n")


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    return common.run_hook("UserPromptSubmit", _body, argv, stdin, stdout, stderr, now)


if __name__ == "__main__":
    sys.exit(main())
