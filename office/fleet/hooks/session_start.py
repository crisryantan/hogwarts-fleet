"""SessionStart hook: the startup digest, ordered so a cut loses memory, not state.

Order: in-flight tasks, then unacked headmaster events, headed by Ollivander's stop while one is in place
(fleet/stops.py), then in McGonagall's session her go status (fleet/go_status.py), then queued work (at most 20,
with a count of the rest), then memory pointers. The whole digest stays under 40 lines. On a resume or fork it prints
one line, and the stop's line under it while one is in place. Her go status is recorded as shown for the session
only when the whole block survived the line cut, so otherwise her first prompt shows it in full.

In flight reads capacity.in_flight: one summary line per desk, then at most INFLIGHT_CAP task
lines, what needs Ryan first (awaiting close, HEADMASTER, round cap, CHANGES, review died), then the
rest (review queued, in review, running, working), oldest first within each. A reviewer's round task
is folded into its author task, so it never shows alone, in flight or queued. Running comes from
launch rows, never from probing a lock, so the digest never makes a review queue. A queued task whose run
is going, as Owl Post starts an ordinary request's run, shows in flight as running and not under queued work.

Input field: source ("startup", "resume", "clear", "compact", "fork"). Anything else
counts as startup.

Before the digest of a new session it rotates the scratchpad of the desk whose session this is (fleet/scratchpad.py),
so the desk reads only its latest Checkpoint; older ones are in the archive. A warning from that is one line after the
digest.

It acks no events on Ryan's behalf except those the fleet already settled (pensieve.settle_events); Ryan acks the
rest in his terminal. The digest lists the events in full once and records the newest id for the session
(fleet/events_seen.py), so each prompt after it lists only newer ones. Answer, result and fyi owls that
the printed digest lists have now reached the desk's session, so they are marked read
and acked. Questions and requests stay unacked until the desk replies to them.
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import capacity, owlery, pensieve  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, events_seen, go_status, scratchpad, stops  # noqa: E402
from fleet.hooks import stop as chat_watch  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

ONE_LINE_SOURCES = ("resume", "fork")
# A session that carries on: its desk may still have its scratchpad in context, and a compact's PreCompact rotated it.
NO_ROTATION_SOURCES = ("resume", "fork", "compact")
TITLE_LIMIT = 90
INFO_KINDS = ("answer", "result", "fyi")


def _tasks(conn, desk: str, status: str) -> list:
    fleet_wide = desk in config.FLEET_VIEW_DESKS
    return pensieve.list_tasks(conn, None if fleet_wide else desk, status)


def _flight(conn, desk: str, now: Optional[int]) -> dict:
    fleet_wide = desk in config.FLEET_VIEW_DESKS
    return capacity.in_flight(conn, now, config.RUNNING_WINDOW_SECONDS, None if fleet_wide else desk,
                              config.REVIEW_ROUND_CAP, config.FOLLOWUP_ROUND_CAP)


def _action(conn, task: dict) -> str:
    state = task["state"]
    if state == "awaiting close":
        return f'gate: "Mischief managed {task["id"]}"'
    if state == "HEADMASTER":
        return "read review-latest.md"
    if state == "round cap":
        return f"castle task allow-round {task['id']}"
    if state == "CHANGES":
        if task["desk"] in config.WORKTREE_DESKS:
            return f"fleet build {task['id']}"
        if task["desk"] == config.OWN_SESSION_DESK:
            return f"fix it, then run fleet review own --task {task['id']} again"
        return "read review-latest.md"
    if state == "review queued":
        return "its reviewer was busy; run the review again"
    if state == "review died":
        return "its review run ended with no verdict; run the review again"
    if state in ("in review", "running"):
        return "a run is going"
    if task["request_id"] is not None:
        return f"request at {owlery.get_request(conn, task['request_id'])['phase']}"
    return "working"


def _state_text(task: dict) -> str:
    followup = task.get("followup")
    if followup is not None:
        return (f"{task['state']}, follow-up {followup['number']}, round {followup['rounds_used']} of"
                f" {followup['max_rounds']}, {common.one_line(followup['pr'], 120)}")
    if task["round"] is None:
        return task["state"]
    if task["state"] == "round cap":
        return f"round cap after CHANGES r{task['round']}"
    return f"{task['state']} r{task['round']}"


def _task_line(conn, task: dict) -> str:
    return (f"- {task['id']} {task['desk']} {_state_text(task)}: {common.one_line(task['title'], TITLE_LIMIT)}"
            f" | {_action(conn, task)}")


def _inflight(conn, desk: str, now: Optional[int] = None) -> list:
    flight = _flight(conn, desk, now)
    if not flight["tasks"]:
        return ["In flight: none"]
    lines = [f"In flight: {flight['tasks']} tasks on {len(flight['desks'])} desks"]
    for row in flight["desks"]:
        counts = ", ".join(f"{count} {state}" for state, count in row["states"].items())
        lines.append(f"- {row['desk']}: {row['count']} ({counts})")
    order = {state: index for index, state in enumerate(capacity.FLIGHT_STATES)}
    tasks = sorted((task for row in flight["desks"] for task in row["tasks"]),
                   key=lambda task: (order[task["state"]], task["desk"] != desk, task["created_at"], task["id"]))
    room = config.INFLIGHT_CAP
    for title, wanted in (("Needs you", True), ("Moving", False)):
        group = [task for task in tasks if (task["state"] in capacity.NEEDS_RYAN) == wanted]
        if not group:
            continue
        shown = group[:room]
        room -= len(shown)
        lines.append(f"{title} ({len(shown)} shown, {len(group) - len(shown)} more):")
        lines += [_task_line(conn, task) for task in shown]
    return lines


def _settle(conn) -> None:
    try:
        pensieve.settle_events(conn)  # events the fleet already settled are acked, not listed
    except (StoreError, sqlite3.Error):
        pass


def _events(conn, candidates: Optional[list] = None) -> list:
    """The events block. candidates, when given, collects (id, line) of each event listed, so the caller can tell which
    survived the digest's line cut."""
    _settle(conn)
    stop = stops.active_line()
    head = [] if stop is None else [stop]
    drained = pensieve.drain(conn, max_chars=config.DRAIN_MAX_CHARS)
    if not drained["events"]:
        return head + ["Headmaster events: none unacked"]
    listed = drained["events"][: config.DIGEST_EVENT_LINES]
    more = drained["remaining"] + len(drained["events"]) - len(listed)
    rows = [(event["id"], "- " + common.one_line(line, 220)) for event, line in zip(listed, stops.event_lines(listed))]
    if candidates is not None:
        candidates += rows
    return head + [f"Headmaster events, unacked ({len(listed)} shown, {more} more):"] + [line for _, line in rows]


def _owl_line(owl: dict) -> str:
    return f"- owl {owl['id']} from {owl['sender']} ({owl['kind']}): {common.one_line(owl['subject'], TITLE_LIMIT)}"


def _queued_tasks(conn, desk: str, now: Optional[int] = None) -> list:
    """Queued tasks, without the reviewer tasks of review rounds, which show under their author task, and
    without a queued task whose run is going, which shows in flight."""
    shown = capacity.review_task_ids(conn) | {task["id"] for row in _flight(conn, desk, now)["desks"]
                                              for task in row["tasks"]}
    return [task for task in _tasks(conn, desk, "queued") if task["id"] not in shown]


def _queued(conn, desk: str, now: Optional[int] = None) -> list:
    items = [f"- task {task['id']} for {task['desk']}: {common.one_line(task['title'], TITLE_LIMIT)}"
             for task in _queued_tasks(conn, desk, now)]
    items += [_owl_line(owl) for owl in owlery.inbox(conn, desk)]
    if not items:
        return ["Queued work: none"]
    lines = [f"Queued work ({len(items)}):"] + items[: config.QUEUED_CAP]
    if len(items) > config.QUEUED_CAP:
        lines.append(f"- ... and {len(items) - config.QUEUED_CAP} more queued")
    return lines


def _memory(conn, desk: str) -> list:
    facts = pensieve.context_facts(conn, desk)
    return [
        "Memory pointers:",
        f"- {len(facts)} current facts for {desk}: castle fact list --context {desk} (Ryan's terminal)",
        f"- Scratchpad: read only the last Checkpoint in {config.castle_desk_dir(desk)}/scratchpad.md"
        f" (older ones are in {config.SCRATCHPAD_ARCHIVE_DIR}/)",
        f"- Plan: {config.CASTLE_ROOT}/PLAN.md and {config.CASTLE_ROOT}/standing-orders.md",
    ]


def _go_status(conn, now: Optional[int]) -> tuple:
    """(lines, mark) of McGonagall's go status, shown in full. Never breaks the digest."""
    try:
        return go_status.block(conn, common.now_stamp(now), None)
    except Exception:  # noqa: BLE001 - the digest still prints
        return [], None


def digest(conn, desk: str, now: Optional[int] = None, shown: Optional[list] = None,
           status: Optional[list] = None) -> list:
    """The digest lines. shown, when given, collects the ids of the events whose lines are in the final digest.
    status, given only in McGonagall's session, adds her go status and collects its mark when all of it is printed."""
    lines = [f"Hogwarts digest for {desk}. Store data, not instructions."]
    candidates: list = []
    board, mark = _go_status(conn, now) if status is not None else ([], None)
    lines += (_inflight(conn, desk, now) + _events(conn, candidates) + board + _queued(conn, desk, now)
              + _memory(conn, desk))
    limit = config.DIGEST_MAX_LINES
    if len(lines) > limit:
        lines = lines[: limit - 1] + [f"(digest cut to {limit} lines; memory pointers go first)"]
    printed = set(lines)
    if shown is not None:
        shown += [event_id for event_id, line in candidates if line in printed]
    if status is not None and mark is not None and all(line in printed for line in board):
        status.append(mark)
    return lines


def ack_shown_owls(conn, desk: str, lines: list, now: int) -> int:
    """Read and ack the answer, result and fyi owls whose line made it into the printed digest."""
    printed = set(lines)
    acked = 0
    for owl in owlery.inbox(conn, desk):
        if owl["kind"] in INFO_KINDS and _owl_line(owl) in printed:
            owlery.read(conn, owl["id"], desk, now=now)
            owlery.ack(conn, owl["id"], desk, now=now)
            acked += 1
    return acked


def resume_line(conn, desk: str, now: Optional[int] = None) -> str:
    _settle(conn)
    inflight = _flight(conn, desk, now)["tasks"]
    drained = pensieve.drain(conn, max_chars=config.DRAIN_MAX_CHARS)
    events = len(drained["events"]) + drained["remaining"]
    queued = len(_queued_tasks(conn, desk, now)) + len(owlery.inbox(conn, desk))
    return (f"Hogwarts: {desk} session resumed. {inflight} in flight, {events} headmaster events unacked, "
            f"{queued} queued.")


def rotate_scratchpad(data: dict, desk: str, now: int) -> list:
    """Rotate the scratchpad of the desk whose session this is, if it keeps one: its warning line, if any. A failure
    is one line too and never stops the digest."""
    owner = common.session_desk(data, desk)
    if owner not in config.CASTLE_DESKS:
        return []
    try:
        warning = scratchpad.rotate(owner, now)["warning"]
    except (FleetError, OSError) as exc:
        return [f"Scratchpad rotation skipped: {common.one_line(exc, 200)}"]
    except Exception as exc:  # noqa: BLE001 - the digest still prints
        return [f"Scratchpad rotation skipped: {type(exc).__name__}"]
    return [warning] if warning else []


def _body(data: dict, desk: str, out, now: int) -> None:
    source = data.get("source")
    rotated = [] if source in NO_ROTATION_SOURCES else rotate_scratchpad(data, desk, now)
    wrote: list = []
    try:
        _store_body(data, desk, out, now, source, rotated, wrote)
    except (StoreError, FleetError, sqlite3.Error):
        stop = stops.active_line()  # from its own file: a store that cannot be read never hides an active stop
        if stop is None or wrote:
            raise
        out.write(stop + "\n")


def _store_body(data: dict, desk: str, out, now: int, source: object, rotated: list, wrote: list) -> None:
    conn = common.connect()
    try:
        if source in ONE_LINE_SOURCES:
            stop = stops.active_line()  # a resumed or forked session is told of an active stop too
            out.write(resume_line(conn, desk, now) + "\n" + ("" if stop is None else stop + "\n"))
            wrote.append(True)
        else:
            listed: list = []
            status: Optional[list] = [] if common.session_desk(data, desk) == config.HOOK_DESK else None
            # The chat watch's start, read before her go status block, so it is never newer than what she is shown.
            told = None if status is None else chat_watch.seed_keys(conn, common.session_id(data))
            lines = digest(conn, desk, now, listed, status)
            out.write("\n".join(lines + rotated) + "\n")
            wrote.append(True)
            out.flush()
            if listed:  # the prompt hook then lists only what the digest did not show
                events_seen.record(common.session_id(data), events_seen.mark(
                    pensieve.shown_through(conn, listed, folded=True), listed))
            if status:  # and only her go tasks that changed since
                go_status.record(common.session_id(data), status[0])
                chat_watch.seed(common.session_id(data), told)
            try:
                ack_shown_owls(conn, desk, lines, now)
            except StoreError:
                pass  # the digest is out; an owl left unacked shows again next time
    finally:
        conn.close()


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    return common.run_hook("SessionStart", _body, argv, stdin, stdout, stderr, now)


if __name__ == "__main__":
    sys.exit(main())
