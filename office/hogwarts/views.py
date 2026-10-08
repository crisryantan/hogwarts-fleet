"""Read-only task views for Ryan's terminal: what needs him, and which build sits behind which go.

Each view is a list of plain text lines, one per task or build, so it fits a screen. Nothing here writes.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import capacity, ids, pensieve

Conn = sqlite3.Connection

LIST_CAP = 30
TITLE_WIDTH = 46
STATE_WIDTH = 14
SHORT_SHA = 12
FALLBACK_STATE = {"queued": "queued", "active": "active", "awaiting_close": "awaiting close", "closed": "closed"}


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _states(conn: Conn, now: int, caps) -> dict:
    """The flight state of every task the board lists, by task id."""
    flight = capacity.in_flight(conn, now, caps.RUNNING_WINDOW_SECONDS, None, caps.REVIEW_ROUND_CAP,
                                caps.FOLLOWUP_ROUND_CAP)
    return {task["id"]: task["state"] for row in flight["desks"] for task in row["tasks"]}


def _state(task: dict, states: dict) -> str:
    if task["status"] == "closed":
        return f"closed {task['close_reason']}"
    return states.get(task["id"]) or FALLBACK_STATE[task["status"]]


def waiting_on(task: dict, state: str) -> str:
    """Who the task waits on: you when it needs the Headmaster, the reviewer while a review is out, else its desk."""
    if state in capacity.NEEDS_RYAN:
        return "you"
    if state in ("in review", "review queued"):
        return "reviewer"
    return task["desk"]


def task_lines(conn: Conn, now: int, caps, desk: Optional[str] = None, cap: int = LIST_CAP) -> list:
    """Open tasks, newest first, one line each: task id, desk, title, state, waiting-on. Reviewer round tasks fold
    into their author task and never show alone."""
    states = _states(conn, now, caps)
    hidden = capacity.review_task_ids(conn)
    tasks = [task for task in reversed(pensieve.list_tasks(conn, desk, None, open_only=True))
             if task["id"] not in hidden]
    if not tasks:
        return ["no open tasks"]
    lines = [f"{'task':<19} {'desk':<10} {'title':<{TITLE_WIDTH}} {'state':<{STATE_WIDTH}} waiting on"]
    for task in tasks[:cap]:
        state = _state(task, states)
        lines.append(f"{task['id']:<19} {task['desk']:<10} {_clip(task['title'], TITLE_WIDTH):<{TITLE_WIDTH}}"
                     f" {state:<{STATE_WIDTH}} {waiting_on(task, state)}")
    if len(tasks) > cap:
        lines.append(f"... {len(tasks) - cap} more open (castle task list --all lists every task)")
    return lines


def _head_sha(conn: Conn, task_id: str) -> Optional[str]:
    """The newest commit a task was reviewed at, else the newest recorded on it."""
    rounds = capacity.review_rounds(conn, task_id)
    if rounds:
        return rounds[-1]["sha"]
    commits = pensieve.task_commits(conn, task_id)
    return commits[-1]["sha"] if commits else None


def _branch(conn: Conn, task: dict) -> str:
    """The branch a build runs on: the one its go recorded on the parent task, else its review branch."""
    spec = pensieve.task_spec(conn, task["parent_task_id"]) if task["parent_task_id"] else None
    return spec["branch"] if spec else (task.get("review_branch") or "-")


def _sha_users(conn: Conn, tasks: list) -> tuple:
    """(head sha by task id, how many of these tasks show each sha)."""
    heads = {task["id"]: _head_sha(conn, task["id"]) for task in tasks}
    users: dict = {}
    for sha in heads.values():
        if sha is not None:
            users[sha] = users.get(sha, 0) + 1
    return heads, users


def build_lines(conn: Conn, now: int, caps, include_closed: bool = False, cap: Optional[int] = LIST_CAP) -> list:
    """One line per build, newest first: go task -> build task -> branch -> state. A build is a task of a worktree
    desk under a parent task (parent_task_id); the branch is the one the go recorded on the parent. A sha that shows on
    several builds is the base they all branched from, so it is labelled as that, not as the build's own work. cap None
    lists every build."""
    tasks = pensieve.list_tasks(conn)
    by_id = {task["id"]: task for task in tasks}
    builds = [task for task in tasks if task["desk"] in caps.WORKTREE_DESKS and task["parent_task_id"] in by_id]
    heads, users = _sha_users(conn, builds)
    states = _states(conn, now, caps)
    shown = [task for task in reversed(builds) if include_closed or task["status"] != "closed"]
    if not shown:
        return ["no open builds"]
    lines = []
    for task in shown[:cap]:  # [:None] is every build
        line = f"{task['parent_task_id']} -> {task['id']} -> {_branch(conn, task)} -> {_state(task, states)}"
        sha = heads[task["id"]]
        if sha is not None:
            line += f" | {'base sha' if users[sha] > 1 else 'sha'} {ids.check('sha', sha)[:SHORT_SHA]}"
        lines.append(line)
    if cap is not None and len(shown) > cap:
        lines.append(f"... {len(shown) - cap} more builds (castle task builds --all lists every one)")
    return lines


def rounds_with_branch(conn: Conn, task_id: str, caps) -> list:
    """A task's review rounds, each with the branch it is on and, when another build shows the same sha, a note that
    it is the base sha and not this branch's own work."""
    rounds = capacity.review_rounds(conn, task_id)
    task = pensieve.get_task(conn, task_id)
    builds = [item for item in pensieve.list_tasks(conn) if item["desk"] in caps.WORKTREE_DESKS
              and item["id"] != task["id"] and item["parent_task_id"] is not None]
    others = {sha: [item["id"] for item in builds if _head_sha(conn, item["id"]) == sha]
              for sha in {row["sha"] for row in rounds}}
    branch = _branch(conn, task)
    return [{**row, "branch": branch,
             "sha_note": f"base sha, also shown on {', '.join(others[row['sha']])}" if others[row["sha"]] else None}
            for row in rounds]
