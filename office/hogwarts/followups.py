"""PR follow-ups in the store: teammates' review comments on a PR the review loop opened.

The review loop's draft PR step binds its PR to the task (bind_pr), once. A Map round that finds comments from
teammates on that PR opens one follow-up (open_followup): in one transaction it sends the fix request owl from the
patrol's desk to the build desk, records the follow-up, what it asks the build desk to answer (its items) and every
GitHub comment it handled (so none is routed twice), and moves the task from awaiting close back to active, which the
store allows only in that transaction, once per follow-up. The follow-up then moves routing, starting, building,
then pushing (when the reviewed commit is new) or straight to posting, then done, or stops from any state that is not
final. Each reply is planned at the PASS and moves planned, posting, then posted, failed or unknown, so a reply cut
off mid-post is read back, never posted again.

Every write here is one transaction. A final state and its one event are written in the same transaction
(advance, stop, abandon_routing, end_closed), so a kill leaves both or neither. A reply that failed or may or may not
be on the PR ends only in the transaction that stops its follow-up (stop with end_replies), and a reply begins only
while every reply begun before it is posted; the store's triggers hold both, whatever code asks. Every read raises on
a store error: a caller that cannot read treats that as unknown, never as nothing.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from . import db, ids, owlery, pensieve
from .errors import ConflictError, NotFoundError, ValidationError

Conn = sqlite3.Connection
OPEN_STATES = db.FOLLOWUP_OPEN_STATES
FINAL_STATES = db.FOLLOWUP_FINAL_STATES
SUBJECT_LIMIT = owlery.SUBJECT_LIMIT
MAX_ITEMS = 999
# What a stop reason may hold: one printable ASCII line, which the script builds.
_REASON = db.FOLLOWUP_REASON_MAX


def pr_url(repo: str, number: int) -> str:
    """The link of a bound PR, as the store keeps it."""
    repo = ids.check("repo", repo)
    number = ids.check_int(number, "PR number", minimum=1, maximum=db.PR_NUMBER_MAX)
    return f"https://github.com/{repo}/pull/{number}"


def _printable_line(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or not value.isascii() \
            or any(not " " <= char <= "~" for char in value):
        raise ValidationError(f"{field} must be one printable ASCII line of at most {limit} characters")
    return value


def _event(conn: Conn, task: dict, event: Optional[tuple], now: int) -> Optional[dict]:
    """One event of a follow-up, (kind, verdict, summary, dedupe_key), written in the caller's transaction."""
    if event is None:
        return None
    kind, verdict, summary, dedupe_key = event
    return pensieve.add_event(conn, task["desk"], kind, verdict, summary, task_id=task["id"], dedupe_key=dedupe_key,
                              now=now)


# The PR binding


def bind_pr(conn: Conn, task_id: str, repo: str, number: int, branch: str, base: str, sha: str, url: str,
            now: Optional[int] = None) -> dict:
    """Bind the PR the review loop opened to its task, once. A second call with the same values returns the row with
    created False; any other value is a ConflictError, and so is a PR bound to another task already."""
    task_id = ids.check("task", task_id)
    repo = ids.check("repo", repo)
    number = ids.check_int(number, "PR number", minimum=1, maximum=db.PR_NUMBER_MAX)
    branch = ids.check("branch", branch)
    base = ids.check("ref", base, "PR base")
    sha = ids.check("sha", sha)
    if url != pr_url(repo, number):
        raise ValidationError("a PR link is https://github.com/<repo>/pull/<number>")
    ts = ids.stamp(now)
    with db.transaction(conn):
        existing = pr_for_task(conn, task_id)
        if existing is not None:
            if (existing["repo"], existing["number"], existing["branch"], existing["base"], existing["opened_sha"],
                    existing["url"]) != (repo, number, branch, base, sha, url):
                raise ConflictError("this task is already bound to another PR")
            return {**existing, "created": False}
        if db.fetch_one(conn, "SELECT task_id FROM task_prs WHERE lower(repo) = lower(?) AND number = ?",
                        (repo, number)) is not None:
            raise ConflictError("that PR is already bound to another task")
        task = pensieve.get_task(conn, task_id)
        if task["status"] != "awaiting_close" or task["worktree"] is None:
            raise ConflictError("a PR is bound to a task awaiting close with its worktree")
        conn.execute("INSERT INTO task_prs(task_id, repo, number, branch, base, opened_sha, url, opened_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (task_id, repo, number, branch, base, sha, url, ts))
    return {**pr_for_task(conn, task_id), "created": True}


def pr_for_task(conn: Conn, task_id: str) -> Optional[dict]:
    """The PR bound to a task, or None."""
    return db.fetch_one(conn, "SELECT * FROM task_prs WHERE task_id = ?", (ids.check("task", task_id),))


def bindings(conn: Conn) -> list:
    """Every bound PR, with its task's desk and status, oldest first."""
    return db.fetch_all(conn, "SELECT task_prs.*, tasks.desk AS desk, tasks.status AS status FROM task_prs"
                              " JOIN tasks ON tasks.id = task_prs.task_id ORDER BY task_prs.opened_at, task_prs.rowid")


def routed_thread_ids(conn: Conn, repo: str, number: int) -> set:
    """The ids of every review thread a follow-up of this PR took, whatever became of it."""
    repo = ids.check("repo", repo)
    number = ids.check_int(number, "PR number", minimum=1, maximum=db.PR_NUMBER_MAX)
    rows = db.fetch_all(conn, "SELECT items.thread_id FROM pr_followup_items AS items"
                              " JOIN pr_followups AS f ON f.id = items.followup_id"
                              " JOIN task_prs AS p ON p.task_id = f.task_id"
                              " WHERE lower(p.repo) = lower(?) AND p.number = ? AND items.thread_id IS NOT NULL",
                        (repo, number))
    return {row["thread_id"] for row in rows}


def handled(conn: Conn, repo: str) -> set:
    """(kind, comment id) of every GitHub comment of this repo a follow-up already handled."""
    repo = ids.check("repo", repo).lower()
    return {(row["kind"], row["comment_id"])
            for row in db.fetch_all(conn, "SELECT kind, comment_id FROM pr_comments WHERE repo = ?", (repo,))}


def handled_in(conn: Conn, repo: str) -> dict:
    """(kind, comment id) to the number of the follow-up that handled it, for this repo."""
    repo = ids.check("repo", repo).lower()
    rows = db.fetch_all(conn, "SELECT c.kind, c.comment_id, f.number FROM pr_comments AS c"
                              " JOIN pr_followups AS f ON f.id = c.followup_id WHERE c.repo = ?", (repo,))
    return {(row["kind"], row["comment_id"]): row["number"] for row in rows}


def posted_ids(conn: Conn, task_id: str) -> set:
    """The GitHub ids of every reply a follow-up of this task posted."""
    rows = db.fetch_all(conn, "SELECT r.posted_id FROM pr_replies AS r JOIN pr_followups AS f ON f.id = r.followup_id"
                              " WHERE f.task_id = ? AND r.posted_id IS NOT NULL", (ids.check("task", task_id),))
    return {row["posted_id"] for row in rows}


def reply_bodies(conn: Conn, task_id: str) -> set:
    """The text of every reply a follow-up of this task planned, posted or not."""
    rows = db.fetch_all(conn, "SELECT r.body FROM pr_replies AS r JOIN pr_followups AS f ON f.id = r.followup_id"
                              " WHERE f.task_id = ?", (ids.check("task", task_id),))
    return {row["body"] for row in rows}


# Live periods


def current_live(conn: Conn) -> Optional[dict]:
    """The open live period, or None."""
    return db.fetch_one(conn, "SELECT * FROM followup_live WHERE until IS NULL")


def live_periods(conn: Conn) -> list:
    """Every live period, oldest first: a comment written inside any of them may be routed."""
    return db.fetch_all(conn, "SELECT * FROM followup_live ORDER BY since, id")


def see_live(conn: Conn, live: bool, now: Optional[int] = None) -> Optional[dict]:
    """Record what a Map round saw: open a live period when live and none is open, close the open one when not live.
    The open period, or None."""
    if not isinstance(live, bool):
        raise ValidationError("live is true or false")
    ts = ids.stamp(now)
    with db.transaction(conn):
        open_period = current_live(conn)
        if live and open_period is None:
            conn.execute("INSERT INTO followup_live(since) VALUES (?)", (ts,))
        elif not live and open_period is not None:
            conn.execute("UPDATE followup_live SET until = ? WHERE id = ? AND until IS NULL",
                         (max(ts, open_period["since"]), open_period["id"]))
        return current_live(conn)


# Reading follow-ups


def get(conn: Conn, followup_id: str) -> dict:
    row = db.fetch_one(conn, "SELECT * FROM pr_followups WHERE id = ?", (ids.check("followup", followup_id),))
    if row is None:
        raise NotFoundError("follow-up not found")
    return row


def by_owl(conn: Conn, owl_id: str) -> Optional[dict]:
    """The follow-up whose fix request is this owl, or None."""
    return db.fetch_one(conn, "SELECT * FROM pr_followups WHERE owl_id = ?", (ids.check("owl", owl_id),))


def open_for_task(conn: Conn, task_id: str) -> Optional[dict]:
    """The task's follow-up that has not ended, or None."""
    return db.fetch_one(conn, "SELECT * FROM pr_followups WHERE task_id = ? AND state NOT IN ('done', 'stopped')",
                        (ids.check("task", task_id),))


def open_for(conn: Conn, task_id: str) -> bool:
    """Whether the task has a follow-up that has not ended. A store error raises: whoever asks treats it as unknown,
    so a closer never takes a task it cannot read."""
    return open_for_task(conn, task_id) is not None


def count_for_task(conn: Conn, task_id: str) -> int:
    return db.fetch_one(conn, "SELECT COUNT(*) AS n FROM pr_followups WHERE task_id = ?",
                        (ids.check("task", task_id),))["n"]


def list_followups(conn: Conn, task_id: Optional[str] = None, since: Optional[int] = None,
                   open_only: bool = False) -> list:
    """Follow-ups oldest first, of one task or all, updated at or after since, or only those that have not ended."""
    task_id = ids.optional("task", task_id)
    since = ids.optional_int(since, "since")
    return db.fetch_all(conn, "SELECT f.*, p.repo AS repo, p.number AS pr_number, p.url AS pr_url FROM pr_followups AS f"
                              " JOIN task_prs AS p ON p.task_id = f.task_id"
                              " WHERE (? IS NULL OR f.task_id = ?) AND (? IS NULL OR f.updated_at >= ?)"
                              " AND (? = 0 OR f.state NOT IN ('done', 'stopped')) ORDER BY f.created_at, f.rowid",
                        (task_id, task_id, since, since, 1 if open_only else 0))


def items(conn: Conn, followup_id: str) -> list:
    """A follow-up's items in label order."""
    rows = db.fetch_all(conn, "SELECT * FROM pr_followup_items WHERE followup_id = ?",
                        (ids.check("followup", followup_id),))
    return sorted(rows, key=lambda row: int(row["label"][1:]))


def comments(conn: Conn, followup_id: str) -> list:
    """The GitHub comments a follow-up handled, by label."""
    return db.fetch_all(conn, "SELECT * FROM pr_comments WHERE followup_id = ? ORDER BY label, kind, comment_id",
                        (ids.check("followup", followup_id),))


def replies(conn: Conn, followup_id: str) -> list:
    """A follow-up's replies in label order."""
    rows = db.fetch_all(conn, "SELECT * FROM pr_replies WHERE followup_id = ?", (ids.check("followup", followup_id),))
    return sorted(rows, key=lambda row: int(row["label"][1:]))


def show(conn: Conn, task_id: str) -> dict:
    """A task's binding and every follow-up of it, with its items, the comments it handled and its replies."""
    task_id = pensieve.get_task(conn, ids.check("task", task_id))["id"]
    with db.snapshot(conn):
        found = []
        for row in list_followups(conn, task_id):
            found.append({**row, "items": items(conn, row["id"]), "comments": len(comments(conn, row["id"])),
                          "replies": replies(conn, row["id"])})
        return {"task_id": task_id, "pr": pr_for_task(conn, task_id), "followups": found}


# Opening a follow-up


def _check_items(item_rows: Iterable[dict]) -> list:
    checked = []
    for number, item in enumerate(item_rows, 1):
        if not isinstance(item, dict):
            raise ValidationError("a follow-up item is an object")
        label = ids.check("item_label", item.get("label"))
        if label != f"T{number}":
            raise ValidationError("follow-up items are labelled T1, T2 and on, in order")
        kind = ids.check_enum(item.get("kind"), db.FOLLOWUP_ITEM_KINDS, "item kind")
        thread_id = item.get("thread_id")
        if (kind == "thread") != (thread_id is not None):
            raise ValidationError("a thread item, and only a thread item, names its thread")
        if thread_id is not None and (not isinstance(thread_id, str) or not 0 < len(thread_id) <= 100
                                      or any(not (char.isascii() and (char.isalnum() or char in "_=-"))
                                             for char in thread_id)):
            raise ValidationError("invalid thread id")
        quote = item.get("quote")
        if quote is not None:
            if kind == "thread":
                raise ValidationError("a thread item carries no quote")
            _printable_line(quote, "quote", db.QUOTE_MAX)
        url = item.get("url")
        _printable_line(url, "item link", db.PR_URL_MAX)
        checked.append({"label": label, "kind": kind, "thread_id": thread_id,
                        "reply_to": ids.check("github_id", item.get("reply_to")), "url": url, "quote": quote})
    if not 0 < len(checked) <= MAX_ITEMS:
        raise ValidationError("a follow-up has at least one item")
    return checked


def _check_comments(comment_rows: Iterable[dict], labels: set) -> list:
    checked, seen = [], set()
    for comment in comment_rows:
        if not isinstance(comment, dict):
            raise ValidationError("a handled comment is an object")
        kind = ids.check_enum(comment.get("kind"), db.FOLLOWUP_ITEM_KINDS, "comment kind")
        comment_id = ids.check("github_id", comment.get("comment_id"))
        label = ids.check("item_label", comment.get("label"))
        if label not in labels or (kind, comment_id) in seen:
            raise ValidationError("each handled comment is listed once, with an item of its follow-up")
        seen.add((kind, comment_id))
        checked.append({"kind": kind, "comment_id": comment_id, "label": label})
    return checked


def open_followup(conn: Conn, task_id: str, followup_id: str, base_sha: str, item_rows: list, comment_rows: list,
                  sender: str, subject: str, body: str, max_per_task: int, now: Optional[int] = None) -> dict:
    """Open one follow-up, in one transaction under BEGIN IMMEDIATE: the task is read again (awaiting close, bound,
    no follow-up open, fewer than max_per_task so far), the fix request owl is sent from sender to the task's desk,
    the follow-up is recorded routing with its items and every comment it handled, and the task goes back to active.
    The store allows that move only here, once per follow-up. The follow-up, with its owl."""
    task_id = ids.check("task", task_id)
    followup_id = ids.check("followup", followup_id)
    base_sha = ids.check("sha", base_sha)
    sender = ids.check("desk", sender, "sender")
    max_per_task = ids.check_int(max_per_task, "follow-ups per task", minimum=1, maximum=1000)
    checked_items = _check_items(item_rows)
    checked_comments = _check_comments(comment_rows, {item["label"] for item in checked_items})
    if {comment["label"] for comment in checked_comments} != {item["label"] for item in checked_items}:
        raise ValidationError("every item of a follow-up answers at least one comment")
    ts = ids.stamp(now)
    with db.transaction(conn):
        task = pensieve.get_task(conn, task_id)
        binding = pr_for_task(conn, task_id)
        if task["status"] != "awaiting_close" or binding is None:
            raise ConflictError("a follow-up opens on a task awaiting close with its PR bound")
        if open_for_task(conn, task_id) is not None:
            raise ConflictError("this task already has a follow-up open")
        done = count_for_task(conn, task_id)
        if done >= max_per_task:
            raise ConflictError(f"this task has had its {max_per_task} follow-ups")
        owl = owlery.send(conn, sender, task["desk"], "fyi", subject, body=body, task_id=task_id,
                          idempotency_key=f"followup:{followup_id}", now=ts)
        if not owl["created"]:
            raise ConflictError("that follow-up's owl was already sent")
        conn.execute("INSERT INTO pr_followups(id, task_id, number, state, base_sha, owl_id, created_at, updated_at)"
                     " VALUES (?, ?, ?, 'routing', ?, ?, ?, ?)", (followup_id, task_id, done + 1, base_sha, owl["id"],
                                                                 ts, ts))
        for item in checked_items:
            conn.execute("INSERT INTO pr_followup_items(followup_id, label, kind, thread_id, reply_to, url, quote)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)", (followup_id, item["label"], item["kind"], item["thread_id"],
                                                           item["reply_to"], item["url"], item["quote"]))
        repo = binding["repo"].lower()
        for comment in checked_comments:
            if db.fetch_one(conn, "SELECT 1 AS found FROM pr_comments WHERE repo = ? AND kind = ? AND comment_id = ?",
                            (repo, comment["kind"], comment["comment_id"])) is not None:
                raise ConflictError("a comment of this follow-up was already handled")
            conn.execute("INSERT INTO pr_comments(repo, kind, comment_id, task_id, followup_id, label, recorded_at)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)", (repo, comment["kind"], comment["comment_id"], task_id,
                                                           followup_id, comment["label"], ts))
        moved = conn.execute("UPDATE tasks SET status = 'active' WHERE id = ? AND status = 'awaiting_close'",
                             (task_id,)).rowcount
        if moved != 1:
            raise ConflictError("the task could not go back to active")
        return {**get(conn, followup_id), "owl": owl}


# Moving a follow-up


def advance(conn: Conn, followup_id: str, state: str, now: Optional[int] = None, pass_sha: Optional[str] = None,
            event: Optional[tuple] = None) -> dict:
    """Move a follow-up one step, with its event (kind, verdict, summary, dedupe key) in the same transaction."""
    followup_id = ids.check("followup", followup_id)
    state = ids.check_enum(state, db.FOLLOWUP_STATES, "follow-up state")
    if state == "stopped":
        raise ValidationError("a follow-up stops through stop, with its reason")
    pass_sha = ids.optional("sha", pass_sha)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get(conn, followup_id)
        if f"{row['state']}>{state}" not in db.FOLLOWUP_EDGES:
            raise ConflictError(f"a follow-up {row['state']} does not move to {state}")
        conn.execute("UPDATE pr_followups SET state = ?, pass_sha = COALESCE(?, pass_sha), updated_at = ? WHERE id = ?"
                     " AND state = ?", (state, pass_sha, ts, followup_id, row["state"]))
        _event(conn, pensieve.get_task(conn, row["task_id"]), event, ts)
        return get(conn, followup_id)


def stop(conn: Conn, followup_id: str, reason: str, now: Optional[int] = None, event: Optional[tuple] = None,
         end_replies: Optional[dict] = None) -> dict:
    """Stop a follow-up that has not ended, with its one event in the same transaction. end_replies, {label: (state,
    posted id or None)}, ends replies that were being posted in that same transaction, after the follow-up stops:
    failed, unknown, or posted when the answer itself is why it stops (it came back from another login), so a reply's
    outcome and the stop it implies land together or not at all. A posted id another reply of the task holds is a
    ConflictError and nothing changes. A follow-up that already ended is returned as it is, and nothing is written."""
    followup_id = ids.check("followup", followup_id)
    reason = _printable_line(reason, "stop reason", _REASON)
    endings = _check_endings(end_replies)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get(conn, followup_id)
        if row["state"] in FINAL_STATES:
            return row
        conn.execute("UPDATE pr_followups SET state = 'stopped', stop_reason = ?, updated_at = ? WHERE id = ?"
                     " AND state = ?", (reason, ts, followup_id, row["state"]))
        for label, (state, posted_id) in endings:
            _end(conn, row, label, state, posted_id, ts)
        _event(conn, pensieve.get_task(conn, row["task_id"]), event, ts)
        return get(conn, followup_id)


def _check_endings(end_replies: Optional[dict]) -> list:
    """end_replies checked: [(label, (state, posted id or None))] in label order."""
    if end_replies is None:
        return []
    if not isinstance(end_replies, dict):
        raise ValidationError("reply endings are a map of label to (state, posted id)")
    checked = []
    for label, ending in end_replies.items():
        if not isinstance(ending, tuple) or len(ending) != 2:
            raise ValidationError("a reply ending is (state, posted id)")
        state = ids.check_enum(ending[0], db.REPLY_FINAL_STATES, "reply ending")
        posted_id = ids.optional("github_id", ending[1])
        if (state == "posted") != (posted_id is not None):
            raise ValidationError("a posted reply, and only a posted one, has its GitHub id")
        checked.append((ids.check("item_label", label), (state, posted_id)))
    return sorted(checked, key=lambda pair: int(pair[0][1:]))


def _end(conn: Conn, row: dict, label: str, state: str, posted_id: Optional[str], ts: int) -> None:
    """End one reply that was being posted, in the caller's transaction."""
    if _reply(conn, row["id"], label)["state"] != "posting":
        raise ConflictError("only a reply being posted ends")
    if posted_id is not None and posted_id in posted_ids(conn, row["task_id"]):
        raise ConflictError("that comment is already another reply of this task")
    conn.execute("UPDATE pr_replies SET state = ?, posted_id = ?, ended_at = ? WHERE followup_id = ? AND label = ?"
                 " AND state = 'posting'", (state, posted_id, ts, row["id"], label))


def abandon_routing(conn: Conn, followup_id: str, reason: str, now: Optional[int] = None,
                    event: Optional[tuple] = None) -> dict:
    """Undo a follow-up still routing, which no build run of ever began, in one transaction: it stops, its task goes
    back to awaiting close, and its one event is written. One that already ended is returned as it is."""
    followup_id = ids.check("followup", followup_id)
    reason = _printable_line(reason, "stop reason", _REASON)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get(conn, followup_id)
        if row["state"] in FINAL_STATES:
            return row
        if row["state"] != "routing":
            raise ConflictError("only a follow-up still routing is undone")
        conn.execute("UPDATE pr_followups SET state = 'stopped', stop_reason = ?, updated_at = ? WHERE id = ?"
                     " AND state = 'routing'", (reason, ts, followup_id))
        task = pensieve.get_task(conn, row["task_id"])
        if task["status"] == "active":
            pensieve.mark_awaiting_close(conn, task["id"], now=ts)
        _event(conn, task, event, ts)
        return get(conn, followup_id)


def end_closed(conn: Conn, followup_id: str, reason: str, now: Optional[int] = None,
               event: Optional[tuple] = None) -> dict:
    """End the follow-up of a task that was closed, in one transaction: a reply still posting may or may not be on the
    PR, so it ends unknown and is never posted again, the follow-up stops, and its one event is written."""
    followup_id = ids.check("followup", followup_id)
    reason = _printable_line(reason, "stop reason", _REASON)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get(conn, followup_id)
        if row["state"] in FINAL_STATES:
            return row
        conn.execute("UPDATE pr_followups SET state = 'stopped', stop_reason = ?, updated_at = ? WHERE id = ?"
                     " AND state = ?", (reason, ts, followup_id, row["state"]))
        conn.execute("UPDATE pr_replies SET state = 'unknown', ended_at = ? WHERE followup_id = ? AND state = 'posting'",
                     (ts, followup_id))
        _event(conn, pensieve.get_task(conn, row["task_id"]), event, ts)
        return get(conn, followup_id)


# Replies


def plan_replies(conn: Conn, followup_id: str, pass_sha: str, reply_rows: list, push_needed: bool,
                 now: Optional[int] = None) -> dict:
    """Record every reply of a follow-up that passed, exactly one per item, and move it to pushing (the reviewed commit
    is new) or straight to posting, in one transaction. This is the mark that the push and the posting began."""
    followup_id = ids.check("followup", followup_id)
    pass_sha = ids.check("sha", pass_sha)
    if not isinstance(push_needed, bool):
        raise ValidationError("push_needed is true or false")
    checked = []
    for reply in reply_rows:
        if not isinstance(reply, dict):
            raise ValidationError("a reply is an object")
        body = reply.get("body")
        if not isinstance(body, str) or not 0 < len(body) <= db.REPLY_BODY_MAX \
                or any(not (" " <= char <= "~" or char == "\n") for char in body):
            raise ValidationError("a reply is printable ASCII text")
        checked.append({"label": ids.check("item_label", reply.get("label")),
                        "mark": ids.check_enum(reply.get("mark"), db.REPLY_MARKS, "reply mark"), "body": body})
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get(conn, followup_id)
        if row["state"] != "building":
            raise ConflictError("replies are planned once, while the follow-up is building")
        labels = [item["label"] for item in items(conn, followup_id)]
        if sorted(reply["label"] for reply in checked) != sorted(labels) or len(checked) != len(labels):
            raise ValidationError("a follow-up plans exactly one reply for each of its items")
        for reply in checked:
            conn.execute("INSERT INTO pr_replies(followup_id, label, mark, body, state) VALUES (?, ?, ?, ?, 'planned')",
                         (followup_id, reply["label"], reply["mark"], reply["body"]))
        conn.execute("UPDATE pr_followups SET state = ?, pass_sha = ?, updated_at = ? WHERE id = ? AND state = 'building'",
                     ("pushing" if push_needed else "posting", pass_sha, ts, followup_id))
        return get(conn, followup_id)


def _reply(conn: Conn, followup_id: str, label: str) -> dict:
    row = db.fetch_one(conn, "SELECT * FROM pr_replies WHERE followup_id = ? AND label = ?", (followup_id, label))
    if row is None:
        raise NotFoundError("reply not found")
    return row


def begin_reply(conn: Conn, followup_id: str, label: str, now: Optional[int] = None) -> dict:
    """Mark a planned reply as being posted, before it is posted. Only while its follow-up is posting and every reply
    begun before it is posted: one still posting, failed or unknown holds back every reply after it."""
    followup_id = ids.check("followup", followup_id)
    label = ids.check("item_label", label)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if get(conn, followup_id)["state"] != "posting" or _reply(conn, followup_id, label)["state"] != "planned":
            raise ConflictError("only a planned reply of a follow-up that is posting begins")
        if any(reply["state"] in ("posting", "failed", "unknown") for reply in replies(conn, followup_id)):
            raise ConflictError("a reply begins only while every reply begun before it is posted")
        conn.execute("UPDATE pr_replies SET state = 'posting', begun_at = ? WHERE followup_id = ? AND label = ?"
                     " AND state = 'planned'", (ts, followup_id, label))
        return _reply(conn, followup_id, label)


def end_reply(conn: Conn, followup_id: str, label: str, state: str, posted_id: Optional[str] = None,
              now: Optional[int] = None) -> dict:
    """End a reply that was being posted with a clean post: posted, with its GitHub id. A reply that failed or may or
    may not be on the PR ends only through stop (end_replies), with the stop it implies. A GitHub id another reply of
    the same task already holds is refused."""
    followup_id = ids.check("followup", followup_id)
    label = ids.check("item_label", label)
    state = ids.check_enum(state, db.REPLY_FINAL_STATES, "reply ending")
    posted_id = ids.optional("github_id", posted_id)
    if (state == "posted") != (posted_id is not None):
        raise ValidationError("a posted reply, and only a posted one, has its GitHub id")
    if state != "posted":
        raise ValidationError("a reply that failed or may or may not be posted ends only as its follow-up stops")
    ts = ids.stamp(now)
    with db.transaction(conn):
        _end(conn, get(conn, followup_id), label, state, posted_id, ts)
        return _reply(conn, followup_id, label)
