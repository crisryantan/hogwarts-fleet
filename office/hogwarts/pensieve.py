from __future__ import annotations

import ipaddress
import re
import sqlite3
import time
from typing import Iterable, Optional

from . import db, ids
from .errors import ConflictError, NotFoundError, TokenError, ValidationError

TITLE_LIMIT = 200
SUMMARY_LIMIT = 500
EXTRACT_LIMIT = 4000
SESSION_EXTRACT_LIMIT = 16000
KEYPOINT_LIMIT = 500
FACT_LIMIT = 300
QUERY_LIMIT = 200
AGING_SECONDS = 30 * 86400
RESERVED_DESKS = ("fleet",)

Conn = sqlite3.Connection


# Desks


def add_desk(conn: Conn, name: str, family: str, role: Optional[str] = None,
             model: Optional[str] = None, now: Optional[int] = None) -> dict:
    name = ids.check("desk", name)
    if name in RESERVED_DESKS:
        raise ValidationError("desk name is reserved")
    family = ids.check_enum(family, db.DESK_FAMILIES, "desk family")
    role = ids.optional("display", role, "role")
    model = ids.optional("label", model, "model")
    ts = ids.stamp(now)
    with db.transaction(conn):
        if _desk(conn, name) is not None:
            raise ConflictError("desk already exists")
        conn.execute(
            "INSERT INTO desks(name, family, role, model, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, family, role, model, ts),
        )
    return get_desk(conn, name)


def _desk(conn: Conn, name: str) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM desks WHERE name = ?", (name,))


def get_desk(conn: Conn, name: str) -> dict:
    desk = _desk(conn, ids.check("desk", name))
    if desk is None:
        raise NotFoundError("desk not found")
    return desk


def list_desks(conn: Conn) -> list[dict]:
    """Every desk, with many_tasks 1 for a desk that may hold many active tasks at once."""
    return db.fetch_all(conn, "SELECT desks.*, many_task_desks.desk IS NOT NULL AS many_tasks FROM desks"
                              " LEFT JOIN many_task_desks ON many_task_desks.desk = desks.name ORDER BY desks.name")


def allow_many_tasks(conn: Conn, desk: str, now: Optional[int] = None) -> dict:
    """Let a desk hold many active tasks at once. One way: a desk never goes back to one task at a time,
    and a second call changes nothing. McGonagall, Snape, Dumbledore, Ryan and the scripts are refused."""
    desk = ids.check("desk", desk)
    if desk in RESERVED_DESKS:
        raise ValidationError("desk name is reserved")
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get_desk(conn, desk)
        if desk in db.SINGLE_TASK_DESKS or row["family"] in db.SINGLE_TASK_FAMILIES:
            raise ValidationError(f"{desk} keeps one active task at a time")
        created = not takes_many_tasks(conn, desk)
        if created:
            conn.execute("INSERT INTO many_task_desks(desk, granted_at) VALUES (?, ?)", (desk, ts))
    return {**get_desk(conn, desk), "many_tasks": 1, "created": created}


def takes_many_tasks(conn: Conn, desk: str) -> bool:
    return db.fetch_one(conn, "SELECT 1 AS found FROM many_task_desks WHERE desk = ?",
                        (ids.check("desk", desk),)) is not None


def blocking_task(conn: Conn, desk: str) -> Optional[dict]:
    """The active task that stops a single desk from starting another, or None. Always None for a desk
    that takes many tasks."""
    desk = ids.check("desk", desk)
    if takes_many_tasks(conn, desk):
        return None
    return db.fetch_one(conn, "SELECT * FROM tasks WHERE desk = ? AND status = 'active' ORDER BY rowid LIMIT 1",
                        (desk,))


# Tasks


def _task(conn: Conn, task_id: str) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))


def get_task(conn: Conn, task_id: str) -> dict:
    task = _task(conn, ids.check("task", task_id))
    if task is None:
        raise NotFoundError("task not found")
    return task


def create_task(conn: Conn, desk: str, title: str, intent_path: Optional[str] = None,
                parent_task_id: Optional[str] = None, request_id: Optional[str] = None,
                session_id: Optional[str] = None, worktree: Optional[str] = None,
                task_id: Optional[str] = None, now: Optional[int] = None) -> dict:
    desk = ids.check("desk", desk)
    title = ids.clean_text(title, "title", TITLE_LIMIT, single_line=True)
    task_id = ids.optional("task", task_id)
    intent_path = ids.optional_intent_path(intent_path, task_id)
    parent_task_id = ids.optional("task", parent_task_id, "parent task id")
    request_id = ids.optional("request", request_id)
    session_id = ids.optional("session", session_id)
    worktree = ids.optional_path(worktree, "worktree", ids.WORKTREES_ROOT)
    ts = ids.stamp(now)
    task_id = ids.new_id("task") if task_id is None else task_id
    with db.transaction(conn):
        get_desk(conn, desk)
        if _task(conn, task_id) is not None:
            raise ConflictError("task id already exists")
        if parent_task_id is not None and get_task(conn, parent_task_id)["status"] == "closed":
            raise ConflictError("parent task is closed")
        if request_id is not None and db.fetch_one(
            conn, "SELECT id FROM requests WHERE id = ?", (request_id,)
        ) is None:
            raise NotFoundError("request not found")
        conn.execute(
            "INSERT INTO tasks(id, desk, title, intent_path, status, parent_task_id, request_id,"
            " session_id, worktree, created_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (task_id, desk, title, intent_path, parent_task_id, request_id, session_id, worktree, ts),
        )
    return get_task(conn, task_id)


def start_task(conn: Conn, task_id: str, now: Optional[int] = None) -> dict:
    task_id = ids.check("task", task_id)
    ts = ids.stamp(now)
    with db.transaction(conn):
        task = get_task(conn, task_id)
        if task["status"] != "queued":
            raise ConflictError("only queued tasks can start")
        if closed_ancestors(conn, task_id):
            raise ConflictError("a parent task is closed, so this task can no longer start")
        busy = blocking_task(conn, task["desk"])
        if busy is not None:
            raise ConflictError(f"desk already has an active task {busy['id']}")
        if task["session_id"] is not None and db.fetch_one(
            conn, "SELECT id FROM tasks WHERE session_id = ? AND status = 'active'", (task["session_id"],)
        ) is not None:
            raise ConflictError("session already has an active task")
        try:
            conn.execute(
                "UPDATE tasks SET status = 'active', started_at = ? WHERE id = ? AND status = 'queued'",
                (ts, task_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("desk already has an active task") from exc
    return get_task(conn, task_id)


def record_spec(conn: Conn, task_id: str, repo_dir: str, branch: str, base: str, intent_sha256: str,
                now: Optional[int] = None) -> dict:
    """Record what Ryan's go approved for a task: the repo folder, branch and base its TASK.md Spec names and
    the sha256 of the TASK.md bytes read at go. Only on a queued task with its TASK.md, once, and it never
    changes. The fleet checks the folder, branch and base against git and its own rules first; the store
    keeps them to plain shapes, with the folder outside the castle and the office."""
    task_id = ids.check("task", task_id)
    repo_dir = ids.check_absolute(repo_dir, "repo folder")
    for root in (ids.CASTLE_ROOT, ids.OFFICE_ROOT):
        if repo_dir == root or repo_dir.startswith(root + "/"):
            raise ValidationError("the repo folder must be outside the castle and the office")
    branch = ids.check("branch", branch)
    base = ids.check("ref", base, "base")
    if ".." in branch or ".." in base:
        raise ValidationError("a branch or base never holds two dots in a row")
    intent_sha256 = ids.check("sha256", intent_sha256, "TASK.md sha256")
    ts = ids.stamp(now)
    with db.transaction(conn):
        task = get_task(conn, task_id)
        if task["status"] != "queued" or task["intent_path"] is None:
            raise ConflictError("a spec is recorded on a queued task with its TASK.md")
        if task_spec(conn, task_id) is not None:
            raise ConflictError("this task already has its spec, and it never changes")
        conn.execute(
            "INSERT INTO task_specs(task_id, repo_dir, branch, base, intent_sha256, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, repo_dir, branch, base, intent_sha256, ts),
        )
    return task_spec(conn, task_id)


def task_spec(conn: Conn, task_id: str) -> Optional[dict]:
    """The repo folder, branch, base and TASK.md sha256 a go recorded on this task, or None."""
    return db.fetch_one(conn, "SELECT * FROM task_specs WHERE task_id = ?", (ids.check("task", task_id),))


# Auto-portrait nights: one row per local date the nightly job took on with the opt-in on (see db.AUTO_PATCHES).

AUTO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AUTO_OP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
AUTO_ENDINGS = ("done", "stopped", "off")
_AUTO_SELECT = ("SELECT auto_patches.*, owls.acked_at AS owl_acked_at FROM auto_patches"
                " JOIN owls ON owls.id = auto_patches.owl_id")
_PRINTABLE_ASCII = re.compile(r"[ -~]*")


def _auto_date(value: object) -> str:
    if not isinstance(value, str) or AUTO_DATE.fullmatch(value) is None:
        raise ValidationError("an auto-portrait date is YYYY-MM-DD")
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError("an auto-portrait date is YYYY-MM-DD") from None
    return value


def _op_ids(values: object, field: str, empty: bool = True) -> str:
    """Patch op ids as the store keeps them: one comma list, each id once."""
    if not isinstance(values, (list, tuple)) or (not values and not empty):
        raise ValidationError(f"{field} must be a list of op ids")
    if any(not isinstance(value, str) or AUTO_OP_ID.fullmatch(value) is None for value in values):
        raise ValidationError(f"{field} holds something that is not an op id")
    if len(set(values)) != len(values):
        raise ValidationError(f"{field} names an op twice")
    return ",".join(values)


def _ascii_line(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or _PRINTABLE_ASCII.fullmatch(value) is None:
        raise ValidationError(f"{field} must be 1 to {limit} characters of printable ASCII")
    return value


def auto_patch(conn: Conn, date: str) -> Optional[dict]:
    """The auto-portrait night of date, with its owl's acked_at as owl_acked_at, or None. Read only."""
    return db.fetch_one(conn, _AUTO_SELECT + " WHERE auto_patches.date = ?", (_auto_date(date),))


def open_auto_patches(conn: Conn) -> list[dict]:
    """The nights still armed or validated, oldest date first, each with its owl's acked_at. Read only."""
    return db.fetch_all(conn, _AUTO_SELECT + " WHERE auto_patches.state IN ('armed', 'validated')"
                        " ORDER BY auto_patches.date")


def recent_auto_patches(conn: Conn, limit: int = 30) -> list[dict]:
    """The newest auto-portrait nights, newest date first. Read only."""
    limit = ids.check_int(limit, "limit", minimum=1, maximum=1000)
    return db.fetch_all(conn, _AUTO_SELECT + " ORDER BY auto_patches.date DESC LIMIT ?", (limit,))


def arm_auto_patch(conn: Conn, date: str, owl_id: str, before: str, before_sha256: Optional[str],
                   now: Optional[int] = None) -> dict:
    """Arm the night of date for its export owl, recording what Dumbledore's outbox held for that date just before
    his run: absent, present (with its sha256) or unreadable. A new night is attempt 1. A night that took no snapshot
    and is armed, stopped or off is armed again as its next attempt; any other night is a ConflictError."""
    date = _auto_date(date)
    owl_id = ids.check("owl", owl_id)
    before = ids.check_enum(before, db.AUTO_PATCH_BEFORE, "before")
    if (before == "present") != (before_sha256 is not None):
        raise ValidationError("a patch that was there before the run is recorded with its sha256, and only that one")
    if before_sha256 is not None:
        before_sha256 = ids.check("sha256", before_sha256, "the earlier patch's sha256")
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = db.fetch_one(conn, "SELECT * FROM auto_patches WHERE date = ?", (date,))
        if row is None:
            conn.execute(
                "INSERT INTO auto_patches(date, attempt, state, owl_id, armed_at, before, before_sha256)"
                " VALUES (?, 1, 'armed', ?, ?, ?, ?)",
                (date, owl_id, ts, before, before_sha256),
            )
        elif row["sha256"] is None and row["state"] in ("armed", "stopped", "off"):
            conn.execute(
                "UPDATE auto_patches SET attempt = attempt + 1, state = 'armed', owl_id = ?, armed_at = ?, before = ?,"
                " before_sha256 = ?, outcome = NULL, applied_ids = NULL, finished_at = NULL WHERE date = ?",
                (owl_id, ts, before, before_sha256, date),
            )
        else:
            raise ConflictError("this night took its snapshot or finished, so it is never armed again")
    return auto_patch(conn, date)


def snapshot_auto_patch(conn: Conn, date: str, sha256: str, ops_json: str, order_ids: list, held_ids: list,
                        unfit_ids: list, now: Optional[int] = None) -> dict:
    """Move an armed night to validated with its one snapshot: the patch's sha256, the checked additions as
    canonical ASCII JSON, and the op ids in patch order, held for Ryan and out of schema."""
    date = _auto_date(date)
    sha256 = ids.check("sha256", sha256, "the patch's sha256")
    if (not isinstance(ops_json, str) or not 2 <= len(ops_json) <= db.AUTO_PATCH_OPS_MAX
            or _PRINTABLE_ASCII.fullmatch(ops_json) is None):
        raise ValidationError(f"the stored ops must be 2 to {db.AUTO_PATCH_OPS_MAX} characters of printable ASCII")
    order = _op_ids(order_ids, "the op order", empty=False)
    held, unfit = _op_ids(held_ids, "the held ops"), _op_ids(unfit_ids, "the ops out of schema")
    if not set(held_ids) | set(unfit_ids) <= set(order_ids) or set(held_ids) & set(unfit_ids):
        raise ValidationError("held and out of schema ops are ops of the patch, and never both")
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = db.fetch_one(conn, "SELECT state FROM auto_patches WHERE date = ?", (date,))
        if row is None:
            raise NotFoundError("this night was never armed")
        if row["state"] != "armed":
            raise ConflictError("only an armed night takes its snapshot")
        conn.execute(
            "UPDATE auto_patches SET state = 'validated', sha256 = ?, ops = ?, order_ids = ?, held_ids = ?,"
            " unfit_ids = ?, validated_at = ? WHERE date = ?",
            (sha256, ops_json, order, held, unfit, ts, date),
        )
    return auto_patch(conn, date)


def end_auto_patch(conn: Conn, date: str, state: str, outcome: str, applied_ids: Optional[list] = None,
                   now: Optional[int] = None) -> dict:
    """End an armed or validated night as done, stopped or off with its outcome line, and for done the ops it
    applied. An ending never changes."""
    date = _auto_date(date)
    state = ids.check_enum(state, AUTO_ENDINGS, "auto-portrait ending")
    outcome = _ascii_line(outcome, "the outcome", db.AUTO_PATCH_OUTCOME_MAX)
    if applied_ids is not None and state != "done":
        raise ValidationError("only a night that is done records the ops it applied")
    applied = None if applied_ids is None else _op_ids(applied_ids, "the applied ops")
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = db.fetch_one(conn, "SELECT state FROM auto_patches WHERE date = ?", (date,))
        if row is None:
            raise NotFoundError("this night was never armed")
        if row["state"] not in ("armed", "validated"):
            raise ConflictError("this night has ended, and its ending never changes")
        conn.execute(
            "UPDATE auto_patches SET state = ?, outcome = ?, applied_ids = ?, finished_at = ? WHERE date = ?",
            (state, outcome, applied, ts, date),
        )
    return auto_patch(conn, date)


def set_worktree(conn: Conn, task_id: str, worktree: str) -> dict:
    """Attach a worktree to a queued or active task that has none. Write once: it never changes."""
    task_id = ids.check("task", task_id)
    worktree = ids.check_path(worktree, "worktree", ids.WORKTREES_ROOT)
    with db.transaction(conn):
        task = get_task(conn, task_id)
        if task["status"] not in ("queued", "active"):
            raise ConflictError("a worktree can only be attached to a queued or active task")
        if task["worktree"] == worktree:
            return task
        if task["worktree"] is not None:
            raise ConflictError("this task already has a different worktree")
        conn.execute("UPDATE tasks SET worktree = ? WHERE id = ? AND worktree IS NULL", (worktree, task_id))
    return get_task(conn, task_id)


# Anything outside printable ASCII (whitespace and control characters included), and what git's own ref rules
# never allow anywhere in a name: ~ ^ : ? * [ \, two dots, @{ and an empty path part.
REVIEW_BRANCH_FORBIDDEN = re.compile(r"[^!-~]|[~^:?*\[\\]|\.\.|@\{|//")


def check_review_branch(branch: object) -> str:
    """A branch an own-session review records: a name git itself takes as a branch (git check-ref-format
    --branch), in 1 to REVIEW_BRANCH_MAX bytes of printable ASCII with no whitespace. Letter case and fleet words
    are not checked, since the name is Ryan's own branch and is only compared, never pushed."""
    if (not isinstance(branch, str) or not 0 < len(branch) <= db.REVIEW_BRANCH_MAX
            or REVIEW_BRANCH_FORBIDDEN.search(branch) is not None or branch == "HEAD"
            or branch.startswith(("-", "/")) or branch.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise ValidationError(f"a review branch is a name git takes as a branch, in 1 to {db.REVIEW_BRANCH_MAX}"
                              " bytes of printable ASCII with no whitespace")
    return branch


def set_review_branch(conn: Conn, task_id: str, branch: str) -> dict:
    """Record the branch an active own-session review task follows, so a fix commit on that branch goes on
    this task. It only moves to another branch, never back to none, and only while the task is active."""
    task_id = ids.check("task", task_id)
    branch = check_review_branch(branch)
    with db.transaction(conn):
        task = get_task(conn, task_id)
        if task["status"] != "active":
            raise ConflictError("a review branch is set on an active task")
        if task["review_branch"] != branch:
            conn.execute("UPDATE tasks SET review_branch = ? WHERE id = ?", (branch, task_id))
    return get_task(conn, task_id)


def closed_ancestors(conn: Conn, task_id: str) -> list[str]:
    return [row["id"] for row in db.fetch_all(
        conn,
        """WITH RECURSIVE up(id) AS (
               SELECT parent_task_id FROM tasks WHERE id = ? AND parent_task_id IS NOT NULL
               UNION
               SELECT tasks.parent_task_id FROM tasks JOIN up ON tasks.id = up.id
               WHERE tasks.parent_task_id IS NOT NULL
           )
           SELECT tasks.id FROM tasks JOIN up ON tasks.id = up.id WHERE tasks.status = 'closed'
           ORDER BY tasks.id""",
        (ids.check("task", task_id),),
    )]


def mark_awaiting_close(conn: Conn, task_id: str, repo: Optional[str] = None, sha: Optional[str] = None,
                        now: Optional[int] = None) -> dict:
    task_id = ids.check("task", task_id)
    repo = ids.optional("repo", repo)
    sha = ids.optional("sha", sha)
    if (repo is None) != (sha is None):
        raise ValidationError("repo and sha go together")
    ts = ids.stamp(now)
    with db.transaction(conn):
        if get_task(conn, task_id)["status"] != "active":
            raise ConflictError("only active tasks can await close")
        conn.execute("UPDATE tasks SET status = 'awaiting_close' WHERE id = ?", (task_id,))
        if repo is not None:
            _record_commit(conn, task_id, repo, sha, ts)
    return get_task(conn, task_id)


def record_commit(conn: Conn, task_id: str, repo: str, sha: str, now: Optional[int] = None) -> dict:
    task_id = ids.check("task", task_id)
    repo = ids.check("repo", repo)
    sha = ids.check("sha", sha)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if get_task(conn, task_id)["status"] not in ("active", "awaiting_close"):
            raise ConflictError("commits are only recorded on active or awaiting_close tasks")
        return _record_commit(conn, task_id, repo, sha, ts)


def _record_commit(conn: Conn, task_id: str, repo: str, sha: str, ts: int) -> dict:
    existing = get_commit(conn, repo, sha)
    if existing is not None:
        if existing["task_id"] != task_id:
            raise ConflictError("that commit is already recorded on another task")
        return {**existing, "created": False}
    conn.execute(
        "INSERT INTO task_commits(repo, sha, task_id, recorded_at) VALUES (?, ?, ?, ?)",
        (repo, sha, task_id, ts),
    )
    return {**get_commit(conn, repo, sha), "created": True}


def get_commit(conn: Conn, repo: str, sha: str) -> Optional[dict]:
    return db.fetch_one(
        conn,
        "SELECT * FROM task_commits WHERE repo = ? AND sha = ?",
        (ids.check("repo", repo), ids.check("sha", sha)),
    )


def commits_with_sha(conn: Conn, sha: str) -> list[dict]:
    """Every recorded commit with this sha, in any repository, in the order they were recorded."""
    return db.fetch_all(conn, "SELECT * FROM task_commits WHERE sha = ? ORDER BY rowid", (ids.check("sha", sha),))


def task_commits(conn: Conn, task_id: str) -> list[dict]:
    """The commits recorded on a task, in the order they were recorded."""
    return db.fetch_all(conn, "SELECT * FROM task_commits WHERE task_id = ? ORDER BY rowid",
                        (ids.check("task", task_id),))


def close_task(conn: Conn, task_id: str, reason: str, token: Optional[str] = None,
               now: Optional[int] = None) -> dict:
    # owlery imports this module, so import it here to avoid a cycle.
    from . import owlery

    task_id = ids.check("task", task_id)
    reason = ids.check_enum(reason, db.CLOSE_REASONS, "close reason")
    ts = ids.stamp(now)
    with db.transaction(conn):
        task = get_task(conn, task_id)
        if task["status"] == "closed":
            raise ConflictError("task is already closed")
        if reason == "complete":
            if task["status"] not in ("active", "awaiting_close"):
                raise ConflictError("only active or awaiting_close tasks can complete")
            if token is None:
                raise TokenError("closing as complete needs a close token")
            owlery.consume(conn, task_id, token, now=ts)
        _set_closed(conn, task_id, reason, ts)
        for child in _open_descendants(conn, task_id):
            _set_closed(conn, child["id"], _cascade_reason(conn, child), ts)
            for request_id in _requests_for_task(conn, child["id"]):
                owlery._cascade_task_closed(conn, request_id, task_id, now=ts)
    return get_task(conn, task_id)


def _cascade_reason(conn: Conn, child: dict) -> str:
    parent = get_task(conn, child["parent_task_id"])
    if child["status"] != "queued" and parent["close_reason"] == "complete":
        return "complete"
    return "superseded"


def _set_closed(conn: Conn, task_id: str, reason: str, ts: int) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'closed', close_reason = ?, closed_at = ? WHERE id = ?",
        (reason, ts, task_id),
    )


def _open_descendants(conn: Conn, task_id: str) -> list[dict]:
    return db.fetch_all(
        conn,
        """WITH RECURSIVE tree(id, depth) AS (
               SELECT id, 1 FROM tasks WHERE parent_task_id = ?
               UNION
               SELECT tasks.id, tree.depth + 1 FROM tasks JOIN tree ON tasks.parent_task_id = tree.id
           )
           SELECT tasks.id, tasks.parent_task_id, tasks.status FROM tasks JOIN tree ON tasks.id = tree.id
           WHERE tasks.status IN ('queued', 'active', 'awaiting_close')
           ORDER BY tree.depth, tasks.created_at, tasks.rowid""",
        (task_id,),
    )


def _requests_for_task(conn: Conn, task_id: str) -> list[str]:
    rows = db.fetch_all(conn, "SELECT id FROM requests WHERE task_id = ? ORDER BY rowid", (task_id,))
    return [row["id"] for row in rows]


def list_tasks(conn: Conn, desk: Optional[str] = None, status: Optional[str] = None,
               open_only: bool = False) -> list[dict]:
    """Tasks oldest first, optionally of one desk, and either of one status or open (queued, active or
    awaiting close)."""
    desk = ids.optional("desk", desk)
    status = None if status is None else ids.check_enum(status, db.TASK_STATUSES, "task status")
    if open_only and status is not None:
        raise ValidationError("pass a status or open, not both")
    return db.fetch_all(
        conn,
        "SELECT * FROM tasks WHERE (? IS NULL OR desk = ?) AND (? IS NULL OR status = ?)"
        " AND (? = 0 OR status IN ('queued', 'active', 'awaiting_close')) ORDER BY created_at, rowid",
        (desk, desk, status, status, 1 if open_only else 0),
    )


# Events


def _event(conn: Conn, event_id: int) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM events WHERE id = ?", (event_id,))


def add_event(conn: Conn, desk: str, kind: str, verdict: str, summary: str,
              task_id: Optional[str] = None, dedupe_key: Optional[str] = None,
              now: Optional[int] = None) -> dict:
    desk = ids.check("desk", desk)
    kind = ids.check("kind", kind, "event kind")
    verdict = ids.check_enum(verdict, db.EVENT_VERDICTS, "event verdict")
    summary = ids.clean_text(summary, "summary", SUMMARY_LIMIT, single_line=True)
    task_id = ids.optional("task", task_id)
    dedupe_key = ids.optional("dedupe", dedupe_key)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if dedupe_key is not None:
            existing = db.fetch_one(conn, "SELECT * FROM events WHERE dedupe_key = ?", (dedupe_key,))
            if existing is not None:
                return {**existing, "created": False}
        get_desk(conn, desk)
        if task_id is not None:
            get_task(conn, task_id)
        cursor = conn.execute(
            "INSERT INTO events(ts, desk, task_id, kind, verdict, summary, dedupe_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, desk, task_id, kind, verdict, summary, dedupe_key),
        )
        event_id = cursor.lastrowid
    return {**_event(conn, event_id), "created": True}


def events_with_key_prefix(conn: Conn, prefix: str) -> list[dict]:
    """Every event whose dedupe key starts with prefix, oldest first. Read only. The prefix is compared as
    plain text, so no character in it works as a wildcard."""
    prefix = ids.check("dedupe", prefix, "dedupe key prefix")
    return db.fetch_all(conn, "SELECT * FROM events WHERE substr(dedupe_key, 1, ?) = ? ORDER BY id",
                        (len(prefix), prefix))


def _event_line(event: dict) -> str:
    return f"[{event['kind']}] #{event['id']} {event['desk']} {event['task_id'] or '-'}: {event['summary']}"


def drain(conn: Conn, max_chars: int = 1500) -> dict:
    max_chars = ids.check_int(max_chars, "max chars", minimum=1, maximum=100000)
    pending = db.fetch_all(
        conn,
        """SELECT id, ts, desk, task_id, kind, summary,
                  ROW_NUMBER() OVER (
                      PARTITION BY COALESCE(task_id, 'event:' || id) ORDER BY ts DESC, id DESC
                  ) AS rank_in_task
           FROM events WHERE verdict = 'headmaster' AND acked_at IS NULL
           ORDER BY rank_in_task, ts DESC, id DESC""",
    )
    picked, used = [], 0
    for event in pending:
        line = _event_line(event)
        if used + len(line) + 1 > max_chars:
            break
        used += len(line) + 1
        picked.append({**event, "line": line})
    return {"events": picked, "remaining": len(pending) - len(picked), "chars": used}


def ack(conn: Conn, event_id: int, now: Optional[int] = None) -> dict:
    event_id = ids.check_int(event_id, "event id", minimum=1)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if _event(conn, event_id) is None:
            raise NotFoundError("event not found")
        conn.execute("UPDATE events SET acked_at = COALESCE(acked_at, ?) WHERE id = ?", (ts, event_id))
    return _event(conn, event_id)


# Sessions, extracts and key points


def get_session(conn: Conn, session_id: str) -> dict:
    session = db.fetch_one(
        conn, "SELECT * FROM sessions WHERE session_id = ?", (ids.check("session", session_id),)
    )
    if session is None:
        raise NotFoundError("session not found")
    return session


def record_session(conn: Conn, session_id: str, project: str, desk: Optional[str] = None,
                   model: Optional[str] = None, started_at: Optional[int] = None,
                   ended_at: Optional[int] = None, first_turn_tokens: Optional[int] = None,
                   total_input_tokens: Optional[int] = None) -> dict:
    session_id = ids.check("session", session_id)
    project = ids.check("project", project)
    desk = ids.optional("desk", desk)
    model = ids.optional("label", model, "model")
    started_at = ids.stamp(started_at)
    ended_at = ids.optional_int(ended_at, "ended at")
    first_turn_tokens = ids.optional_int(first_turn_tokens, "first turn tokens")
    total_input_tokens = ids.optional_int(total_input_tokens, "total input tokens")
    with db.transaction(conn):
        if desk is not None:
            get_desk(conn, desk)
        existing = db.fetch_one(conn, "SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        start = started_at if existing is None else existing["started_at"]
        if ended_at is not None and ended_at < start:
            raise ValidationError("session cannot end before it starts")
        if existing is None:
            conn.execute(
                "INSERT INTO sessions(session_id, desk, project, model, started_at, ended_at,"
                " first_turn_tokens, total_input_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, desk, project, model, started_at, ended_at, first_turn_tokens,
                 total_input_tokens),
            )
        else:
            _require_same_session(existing, project, desk)
            conn.execute(
                "UPDATE sessions SET desk = COALESCE(desk, ?), model = COALESCE(?, model),"
                " ended_at = COALESCE(?, ended_at), first_turn_tokens = COALESCE(?, first_turn_tokens),"
                " total_input_tokens = COALESCE(?, total_input_tokens) WHERE session_id = ?",
                (desk, model, ended_at, first_turn_tokens, total_input_tokens, session_id),
            )
    return {**get_session(conn, session_id), "created": existing is None}


def _require_same_session(existing: dict, project: str, desk: Optional[str]) -> None:
    if existing["project"] != project:
        raise ConflictError("session belongs to another project")
    if desk is not None and existing["desk"] not in (None, desk):
        raise ConflictError("session belongs to another desk")


def add_extract(conn: Conn, session_id: str, role: str, text: str, seq: Optional[int] = None,
                now: Optional[int] = None) -> dict:
    session_id = ids.check("session", session_id)
    role = ids.check_enum(role, db.EXTRACT_ROLES, "extract role")
    text = _scrubbed(text, "extract text", EXTRACT_LIMIT)
    seq = ids.optional_int(seq, "seq")
    ts = ids.stamp(now)
    with db.transaction(conn):
        get_session(conn, session_id)
        used = conn.execute(
            "SELECT COALESCE(SUM(length(text)), 0) FROM extracts WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if used + len(text) > SESSION_EXTRACT_LIMIT:
            raise ValidationError(f"session extracts would exceed {SESSION_EXTRACT_LIMIT} characters")
        if seq is None:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM extracts WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        elif db.fetch_one(
            conn, "SELECT id FROM extracts WHERE session_id = ? AND seq = ?", (session_id, seq)
        ) is not None:
            raise ConflictError("extract seq already exists for this session")
        cursor = conn.execute(
            "INSERT INTO extracts(session_id, seq, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, role, text, ts),
        )
        extract_id = cursor.lastrowid
    return db.fetch_one(conn, "SELECT * FROM extracts WHERE id = ?", (extract_id,))


def add_keypoint(conn: Conn, text: str, tags: Iterable[str] = (), session_id: Optional[str] = None,
                 now: Optional[int] = None) -> dict:
    text = _scrubbed(text, "key point", KEYPOINT_LIMIT)
    tags = ids.check_tags(tags)
    session_id = ids.optional("session", session_id)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if session_id is not None:
            get_session(conn, session_id)
        cursor = conn.execute(
            "INSERT INTO keypoints(session_id, text, tags, created_at) VALUES (?, ?, ?, ?)",
            (session_id, text, tags, ts),
        )
        keypoint_id = cursor.lastrowid
    return db.fetch_one(conn, "SELECT * FROM keypoints WHERE id = ?", (keypoint_id,))


def extracts_between(conn: Conn, since: int, until: int, limit: int = 2000) -> list[dict]:
    """The extracts recorded from since up to (not including) until, oldest first, each with its session's
    desk and project. Read only. It feeds the nightly Pensieve export."""
    since = ids.check_int(since, "since", maximum=ids.MAX_TIME)
    until = ids.check_int(until, "until", maximum=ids.MAX_TIME)
    limit = ids.check_int(limit, "limit", minimum=1, maximum=10000)
    return db.fetch_all(
        conn,
        "SELECT extracts.id, extracts.session_id, sessions.desk, sessions.project, extracts.seq, extracts.role,"
        " extracts.text, extracts.created_at FROM extracts JOIN sessions ON sessions.session_id = extracts.session_id"
        " WHERE extracts.created_at >= ? AND extracts.created_at < ? ORDER BY extracts.created_at, extracts.id"
        " LIMIT ?",
        (since, until, limit),
    )


def _scrubbed(text: object, field: str, limit: int) -> str:
    cleaned = scrub(ids.clean_text(text, field, limit))
    if len(cleaned) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters after scrubbing")
    return cleaned


def fts_phrases(text: str, limit: int = 16) -> list[str]:
    words = [word for word in text.split() if any(ch.isalnum() for ch in word)][:limit]
    return ['"' + word.replace('"', '""') + '"' for word in words]


def fts_query(text: str) -> str:
    phrases = fts_phrases(ids.clean_text(text, "query", QUERY_LIMIT, single_line=True))
    if not phrases:
        raise ValidationError("query has no searchable words")
    return " ".join(phrases)


def find(conn: Conn, query: str, limit: int = 10) -> list[dict]:
    match = fts_query(query)
    limit = ids.check_int(limit, "limit", minimum=1, maximum=100)
    return db.fetch_all(
        conn,
        """SELECT 'extract' AS source, extracts.id AS id, extracts.session_id AS session_id,
                  snippet(extracts_fts, 0, '**', '**', '...', 12) AS snippet,
                  bm25(extracts_fts) AS score
           FROM extracts_fts JOIN extracts ON extracts.id = extracts_fts.rowid
           WHERE extracts_fts MATCH ?
           UNION ALL
           SELECT 'keypoint', keypoints.id, keypoints.session_id,
                  snippet(keypoints_fts, 0, '**', '**', '...', 12),
                  bm25(keypoints_fts)
           FROM keypoints_fts JOIN keypoints ON keypoints.id = keypoints_fts.rowid
           WHERE keypoints_fts MATCH ?
           ORDER BY score, source, id
           LIMIT ?""",
        (match, match, limit),
    )


# Scrubbing

def _ipv6(match: re.Match) -> str:
    candidate = match.group(0)
    groups = [group for group in re.split(r"[:.]", candidate) if group]
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return candidate
    return "[ipv6]" if len(groups) >= 2 else candidate


def _keep_prefix(placeholder: str):
    return lambda match: match.group(1) + placeholder


# Order matters: key blocks, URL credentials and tokens go before the email pattern can split them.
_SCRUBBERS = (
    (re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[A-Za-z0-9+/=\s]*)",
        re.S,
    ), "[private_key]"),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s]*@"), "[credentials]@"),
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[jwt]"),
    (re.compile(
        r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer|digest|token|negotiate)\s+)"
        r"[A-Za-z0-9._~+/=-]{6,}"
    ), _keep_prefix("[token]")),
    (re.compile(r"(?i)(?<![A-Za-z0-9])(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), _keep_prefix("[token]")),
    (re.compile(
        r"(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})"
    ), "[token]"),
    (re.compile(
        r"(?i)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*(?:password|passwd|passphrase|pwd|secret|token|"
        r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth[_-]?key|credentials?)"
        r"[\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&\"']+)"
    ), _keep_prefix("[secret]")),
    (re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), "[aws_key]"),
    (re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(
        r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|[0-9A-Fa-f]{1,4})?(?![\w:])"
    ), _ipv6),
    (re.compile(
        r"(?<![0-9])(?<![0-9]\.)(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}"
        r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?!\.?[0-9])"
    ), "[ipv4]"),
    (re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"), "[hex]"),
)


def scrub(text: str) -> str:
    if not isinstance(text, str):
        raise ValidationError("scrub needs text")
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    return text


# Facts


def _fact(conn: Conn, fact_id: int) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM facts WHERE id = ?", (fact_id,))


def add_fact(conn: Conn, scope: str, text: str, tier: str, source: str, expires_at: Optional[int] = None,
             subject_key: Optional[str] = None, valid_from: Optional[int] = None, lookup: Optional[str] = None,
             now: Optional[int] = None) -> dict:
    # facts imports this module, so import it here to avoid a cycle.
    from . import facts

    return facts.add_fact(conn, scope, text, tier, source, expires_at, subject_key, valid_from, lookup, now)


def touch(conn: Conn, fact_id: int, now: Optional[int] = None) -> dict:
    fact_id = ids.check_int(fact_id, "fact id", minimum=1)
    ts = ids.stamp(now)
    with db.transaction(conn):
        fact = _fact(conn, fact_id)
        if fact is None:
            raise NotFoundError("fact not found")
        if fact["archived_at"] is not None:
            raise ConflictError("fact is archived")
        conn.execute("UPDATE facts SET last_used_at = ? WHERE id = ?", (ts, fact_id))
    return _fact(conn, fact_id)


# Only open rows go stale. Closed history keeps its window, so withdraw can still restore it.
_STALE_FACTS = """SELECT id FROM facts WHERE archived_at IS NULL AND valid_to IS NULL AND (
       (tier = 'aging' AND last_used_at <= ?) OR (tier = 'perishable' AND expires_at <= ?)
   ) ORDER BY id"""


def _stale_ids(conn: Conn, ts: int) -> list[int]:
    return [row["id"] for row in db.fetch_all(conn, _STALE_FACTS, (ts - AGING_SECONDS, ts))]


def decay(conn: Conn, now: Optional[int] = None) -> list[int]:
    return _stale_ids(conn, ids.stamp(now))


def archive_stale(conn: Conn, now: Optional[int] = None) -> dict:
    ts = ids.stamp(now)
    with db.transaction(conn):
        stale = _stale_ids(conn, ts)
        for fact_id in stale:
            conn.execute("UPDATE facts SET archived_at = ? WHERE id = ?", (ts, fact_id))
    return {"archived": stale}


def archive(conn: Conn, fact_ids: Iterable[int], now: Optional[int] = None) -> dict:
    if not isinstance(fact_ids, (list, tuple, set, frozenset)):
        raise ValidationError("archive takes a list of fact ids")
    wanted = list(dict.fromkeys(ids.check_int(value, "fact id", minimum=1) for value in fact_ids))
    if not 1 <= len(wanted) <= 1000:
        raise ValidationError("archive takes between 1 and 1000 fact ids")
    ts = ids.stamp(now)
    archived, already = [], []
    with db.transaction(conn):
        for fact_id in wanted:
            fact = _fact(conn, fact_id)
            if fact is None:
                raise NotFoundError("fact not found")
            if fact["archived_at"] is not None:
                already.append(fact_id)
                continue
            conn.execute("UPDATE facts SET archived_at = ? WHERE id = ?", (ts, fact_id))
            archived.append(fact_id)
    return {"archived": archived, "already_archived": already}


def list_facts(conn: Conn, scope: Optional[str] = None, include_archived: bool = False,
               include_closed: bool = False) -> list[dict]:
    if scope is not None and scope != "fleet":
        scope = ids.check("desk", scope, "fact scope")
    return db.fetch_all(
        conn,
        "SELECT * FROM facts WHERE (? IS NULL OR scope = ?) AND (? OR archived_at IS NULL)"
        " AND (? OR valid_to IS NULL) ORDER BY id",
        (scope, scope, 1 if include_archived else 0, 1 if include_closed else 0),
    )


def context_facts(conn: Conn, desk: str, now: Optional[int] = None) -> list[dict]:
    desk = get_desk(conn, desk)["name"]
    ts = ids.stamp(now)
    return db.fetch_all(
        conn,
        """SELECT * FROM facts
           WHERE scope IN ('fleet', ?) AND archived_at IS NULL AND valid_to IS NULL
             AND (tier <> 'perishable' OR expires_at > ?)
           ORDER BY CASE tier WHEN 'pinned' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END,
                    last_used_at DESC, id""",
        (desk, ts),
    )


# Metrics


def add_metric(conn: Conn, desk: str, run_id: str, model: str, input_tokens: int,
               output_tokens: int, cache_read_tokens: int, cost_usd: float, duration_ms: int,
               ts: Optional[int] = None) -> dict:
    desk = ids.check("desk", desk)
    run_id = ids.check("label", run_id, "run id")
    model = ids.check("label", model, "model")
    counts = [ids.check_int(value, field) for value, field in (
        (input_tokens, "input tokens"), (output_tokens, "output tokens"),
        (cache_read_tokens, "cache read tokens"), (duration_ms, "duration ms"),
    )]
    cost_usd = ids.check_amount(cost_usd, "cost usd")
    ts = ids.stamp(ts)
    with db.transaction(conn):
        get_desk(conn, desk)
        cursor = conn.execute(
            "INSERT INTO metrics(ts, desk, run_id, model, input_tokens, output_tokens,"
            " cache_read_tokens, cost_usd, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, desk, run_id, model, counts[0], counts[1], counts[2], cost_usd, counts[3]),
        )
        metric_id = cursor.lastrowid
    return db.fetch_one(conn, "SELECT * FROM metrics WHERE id = ?", (metric_id,))


def summary(conn: Conn, since: int = 0) -> list[dict]:
    since = ids.check_int(since, "since")
    return db.fetch_all(
        conn,
        """SELECT desk, COUNT(*) AS runs, SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
                  ROUND(SUM(cost_usd), 6) AS cost_usd, SUM(duration_ms) AS duration_ms,
                  MIN(ts) AS first_ts, MAX(ts) AS last_ts
           FROM metrics WHERE ts >= ? GROUP BY desk ORDER BY desk""",
        (since,),
    )
