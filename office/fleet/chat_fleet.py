"""The owner's fleet answers in Chat: what the fleet has in flight, and where a PR it opened stands, as fixed lines
built from the store, never by a model.

Read only. The store is opened read only (db.connect_readonly) and the Map's last snapshot is read without making a
folder, so asking changes nothing. The caller (fleet/chat_review.py) has already decided who may ask and where: only
the owner, in a direct message with the app.

What a reply may hold, and nothing else: a task's id, its title (scrubbed and cut to TITLE_MAX by the caller's clean),
its stage from the fixed table below, a closed task's reason, a PR's repo and number, a review round's number, verdict,
reviewer desk and the first 12 characters of the commit it read, the Map's last read of the PR's checks as one fixed
word and a count, and how many follow-ups are open with their state. Never TASK.md beyond its title, a diff, a path, a
teammate's comment, an email or a branch name.

Stages, from store rows alone: queued; building (a run is going, a fix round or a PR follow-up is under way); in review
round N; passed (a PASS the owner has not pushed); draft PR open and awaiting merge (the PR is bound to the task; the
Map last saw it as a draft, or as ready); blocked, with its reason class: needs the owner (HEADMASTER), round cap,
review died, tooling, review loop stopped. A go task whose build is open is folded into the build.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from hogwarts import capacity, db, followups, ids, pensieve
from hogwarts.errors import StoreError

from fleet import common, config, safefs
from fleet.safefs import FleetError

TITLE_MAX = 60
OPEN_MAX = 12
DONE_MAX = 5
ROUNDS_SHOWN = 6
SHA_SHOWN = 12
SNAPSHOT_FILE = "snapshot.json"
UNREADABLE = "I couldn't read the fleet's store just now."
NOT_FLEET = "That PR isn't one the fleet opened."
UNTITLED = "untitled"
# The reason class of a blocked task, by the store's own sign of it: a flight state, or a loud event after its newest
# round.
BLOCKED_STATES = {"HEADMASTER": "needs the owner", "round cap": "round cap", "review died": "review died"}
BLOCKED_EVENTS = {"review.blocked-on-tooling": "tooling", "review.loop-stopped": "review loop stopped",
                  "review.round-cap": "round cap"}
# The Map's rollup of a PR's checks, as one word.
CHECKS = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing", "PENDING": "pending", "EXPECTED": "pending"}

Clean = Callable[[object, int], str]
Ago = Callable[[int], str]


def _snapshot() -> tuple:
    """(the Map's last read of each PR by lower-case repo#number, when it was taken or None), or ({}, None) when there
    is none or it cannot be read. Never makes a folder."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.PATROL_DIR, "map") as fd:
            raw = safefs.read_regular(fd, SNAPSHOT_FILE, config.PATROL_STATE_MAX_BYTES, "the Map's snapshot")
        data = common.strict_json(raw)
    except (FleetError, OSError, UnicodeDecodeError, ValueError):
        return {}, None
    prs = data.get("prs") if isinstance(data, dict) else None
    taken = data.get("taken_at") if isinstance(data, dict) else None
    if not isinstance(prs, dict):
        return {}, None
    return ({key.lower(): value for key, value in prs.items() if isinstance(key, str) and isinstance(value, dict)},
            taken if type(taken) is int else None)


def _ready(snapshot: dict, repo: str, number: int) -> bool:
    """Whether the Map last saw the PR as ready for review (not a draft). Not seen is not ready."""
    return snapshot.get(f"{repo}#{number}".lower(), {}).get("draft") is False


def _blocked_by_event(conn, task_id: str, since: int) -> Optional[str]:
    """The reason class of the newest loud stop event of the task at or after since, else None."""
    kinds = tuple(BLOCKED_EVENTS)
    row = db.fetch_one(conn, "SELECT kind FROM events WHERE task_id = ? AND verdict = 'headmaster' AND ts >= ?"
                             f" AND kind IN ({', '.join('?' for _ in kinds)}) ORDER BY id DESC LIMIT 1",
                       (task_id, since, *kinds))
    return None if row is None else BLOCKED_EVENTS[row["kind"]]


def _stage(conn, task: dict, row: Optional[dict], snapshot: dict) -> str:
    """The stage of one open task, from the fixed table in the module notes."""
    if row is None:  # not active and no run going: it has not started
        return "queued"
    if row["followup"] is not None:
        return "building"
    pr = followups.pr_for_task(conn, task["id"])
    if pr is not None:
        return "awaiting merge" if _ready(snapshot, pr["repo"], pr["number"]) else "draft PR open"
    state = row["state"]
    if state == "awaiting close":
        return "passed"
    live = [item for item in capacity.review_rounds(conn, task["id"]) if item["superseded_by"] is None]
    since = max(live[-1]["created_at"] if live else 0, int(task["started_at"] or task["created_at"]))
    stopped = _blocked_by_event(conn, task["id"], since) or BLOCKED_STATES.get(state)
    if stopped is not None:
        return f"blocked: {stopped}"
    if state in ("review queued", "in review"):
        return f"in review round {row['round']}"
    return "building"


def _shown(task_id: object) -> str:
    return task_id if isinstance(task_id, str) and ids.PATTERNS["task"].fullmatch(task_id) else "a task"


def _title(clean: Clean, text: object) -> str:
    return clean(text, TITLE_MAX) or UNTITLED


def _open(conn) -> list:
    """Every open task that is not a review round's, newest first, a go task folded into its open build."""
    hidden = capacity.review_task_ids(conn)
    tasks = [task for task in reversed(pensieve.list_tasks(conn, open_only=True)) if task["id"] not in hidden]
    folded = {task["parent_task_id"] for task in tasks
              if task["desk"] in config.WORKTREE_DESKS and task["parent_task_id"] is not None}
    return [task for task in tasks if task["id"] not in folded]


def _closed(conn, now: int) -> list:
    """The newest DONE_MAX tasks closed this cap day, newest first, that are not a review round's."""
    start = capacity.day_bounds(now, config.CAP_RESET_UTC_SECONDS)[0]
    hidden = capacity.review_task_ids(conn)
    rows = db.fetch_all(conn, "SELECT * FROM tasks WHERE status = 'closed' AND closed_at >= ?"
                              " ORDER BY closed_at DESC, rowid DESC", (start,))
    return [task for task in rows if task["id"] not in hidden][:DONE_MAX]


def _closed_stage(conn, task: dict) -> str:
    return "merged" if pensieve.task_closure(conn, task["id"]) is not None else f"closed ({task['close_reason']})"


def _store(read: Callable) -> str:
    """read(conn), with the store opened read only; the fixed unreadable line when it cannot be read."""
    try:
        conn = db.connect_readonly(config.DB_PATH)
        try:
            return read(conn)
        finally:
            conn.close()
    except (FleetError, StoreError, OSError, sqlite3.Error):
        return UNREADABLE


def fleet_text(now: int, clean: Clean) -> str:
    """The reply to "fleet status": the open tasks, newest first, at most OPEN_MAX with a count of the rest, each its
    id, title and stage, then the last DONE_MAX closed this cap day."""

    def read(conn) -> str:
        tasks, snapshot = _open(conn), _snapshot()[0]
        flight = capacity.in_flight(conn, now, config.RUNNING_WINDOW_SECONDS, None, config.REVIEW_ROUND_CAP,
                                    config.FOLLOWUP_ROUND_CAP)
        rows = {item["id"]: item for desk in flight["desks"] for item in desk["tasks"]}
        lines = [f"Open ({len(tasks)}):" if tasks else "Nothing open."]
        lines += [f"- {_shown(task['id'])}: \"{_title(clean, task['title'])}\" |"
                  f" {_stage(conn, task, rows.get(task['id']), snapshot)}" for task in tasks[:OPEN_MAX]]
        if len(tasks) > OPEN_MAX:
            lines.append(f"({len(tasks) - OPEN_MAX} more not shown)")
        closed = _closed(conn, now)
        if closed:
            lines.append(f"Closed today (latest {len(closed)}):")
            lines += [f"- {_shown(task['id'])}: \"{_title(clean, task['title'])}\" | {_closed_stage(conn, task)}"
                      for task in closed]
        else:
            lines.append("Nothing closed today.")
        return "\n".join(lines)

    return _store(read)


def _round_line(item: dict) -> str:
    """One review round on one line: its number, how it stands, its reviewer desk and the commit it read."""
    if item["superseded_by"] is not None:
        how = "superseded by a newer commit"
    elif item["has_verdict"]:
        how = item["verdict"]
    elif item["waiting"]:
        how = "waiting for its reviewer"
    elif item["counts"]:
        how = "in progress"
    else:
        how = "ended with no verdict"
    return f"- round {item['round']}: {how} by {item['reviewer']} at {item['sha'][:SHA_SHOWN]}"


def _checks_line(snapshot: dict, taken: Optional[int], repo: str, number: int, now: int, ago: Ago) -> str:
    """The Map's last read of the PR's checks, with the head and the age of that read when it gave them."""
    seen = snapshot.get(f"{repo}#{number}".lower())
    if seen is None:
        return "CI: the Map has not seen this PR."
    word = CHECKS.get(seen.get("checks"), "no checks reported")
    failing = seen.get("failing")
    if word == "failing" and isinstance(failing, list) and failing:
        word = f"failing ({len(failing)} check{'' if len(failing) == 1 else 's'})"
    head = seen.get("head")
    shown = [f"head {head[:SHA_SHOWN]}"] if isinstance(head, str) and ids.PATTERNS["sha"].fullmatch(head) else []
    shown += [ago(max(0, now - taken))] if taken is not None else []
    return f"CI as the Map last saw it{' (' + ', '.join(shown) + ')' if shown else ''}: {word}."


def pr_text(repo: str, number: int, now: int, clean: Clean, ago: Ago) -> str:
    """The reply to "fleet <PR link>": for a PR the fleet opened, its task, stage, review rounds with their verdicts and
    reviewer desks, the commit the newest live round read, CI as the Map last saw it and the follow-ups still open; for
    any other PR, NOT_FLEET."""

    def read(conn) -> str:
        bound = db.fetch_one(conn, "SELECT * FROM task_prs WHERE lower(repo) = lower(?) AND number = ?", (repo, number))
        if bound is None:
            return NOT_FLEET
        task = pensieve.get_task(conn, bound["task_id"])
        snapshot, taken = _snapshot()
        if task["status"] == "closed":
            stage = _closed_stage(conn, task)
        else:
            flight = capacity.in_flight(conn, now, config.RUNNING_WINDOW_SECONDS, None, config.REVIEW_ROUND_CAP,
                                        config.FOLLOWUP_ROUND_CAP)
            rows = {item["id"]: item for desk in flight["desks"] for item in desk["tasks"]}
            stage = _stage(conn, task, rows.get(task["id"]), snapshot)
        lines = [f"{bound['repo']}#{bound['number']}: task {_shown(task['id'])}, \"{_title(clean, task['title'])}\","
                 f" {stage}.", f"Opened by the fleet {ago(max(0, now - bound['opened_at']))}."]
        rounds = capacity.review_rounds(conn, task["id"])
        if rounds:
            lines.append(f"Review rounds ({len(rounds)}):")
            lines += [_round_line(item) for item in rounds[-ROUNDS_SHOWN:]]
            live = [item for item in rounds if item["superseded_by"] is None]
            if live:
                lines.append(f"Head reviewed: {live[-1]['sha'][:SHA_SHOWN]}.")
        else:
            lines.append("No review round yet.")
        lines.append(_checks_line(snapshot, taken, bound["repo"], bound["number"], now, ago))
        opened = followups.list_followups(conn, task["id"], open_only=True)
        lines.append(f"Open follow-ups ({len(opened)}): " + ", ".join(f"#{item['number']} {item['state']}"
                                                                      for item in opened) + "."
                     if opened else "Open follow-ups: none.")
        return "\n".join(lines)

    return _store(read)
