"""UserPromptSubmit hook: show headmaster events, warn at 200k context, close on "Mischief managed".

Input fields read:
- prompt: the text Ryan submitted (the documented UserPromptSubmit field).
- prompt_id: the id Claude Code gives this prompt. Its transcript entries carry it as promptId.
- transcript_path: the session transcript, read from its tail only.
- agent_id: present only inside a subagent; a close is refused there.

Output is one JSON object. systemMessage is shown to Ryan. The same text goes to the
session as additionalContext, except that headmaster events appear there as a count
only, because the startup digest already lists them for the session.

What it says (each part only when there is something to say):
1. The result of "Mischief managed <task-id>" when the prompt is exactly that, after
   trimming surrounding whitespace. It mints a close token with minted_by "hook" and
   closes that one task as complete in the same run, then names every descendant task
   the close cascaded to and how each one closed. The token is never printed. Any
   other text never closes anything.
2. Unacked headmaster events, newest per task, cut to about 1500 characters, with a
   count of the rest. The hook never acks them. They stay until Ryan runs
   castle event ack in his terminal.
3. One Tempus line when the last assistant call in the transcript carried more than
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

Unresolved question: the hook input has no documented field that says a person typed
the prompt, and Claude Code may write the prompt's entry only after this hook runs. If
it does, the entry is never found and every close is refused. A refused close prints
the castle commands, and Ryan closes the task from his terminal. Headless desks run
with --restricted, which ignores the castle's project settings, so this hook is not
loaded for them.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import owlery, pensieve  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, transcript  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

MISCHIEF = re.compile(r"Mischief managed (tk_[0-9a-f]{16})")
OPEN_STATUSES = ("queued", "active", "awaiting_close")


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
