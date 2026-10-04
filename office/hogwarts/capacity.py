"""Busy-day capacity: cap bumps, which cap stopped a desk, and review rounds per author task.

The cap numbers live in the fleet's config. The store keeps what changes during a day: the bumps
Ryan makes with castle desk cap, one row each time a cap refuses a run or a vendor limit stops one,
and one row per review request, so a newer commit supersedes a review that is still waiting and a
round past the cap waits for Ryan's castle task allow-round. Only a reviewer run that recorded a
verdict uses up a round, and the proof is the verdict itself: record_round_verdict stores the review and
ties it to its round in one transaction, so a review recorded before a later step failed still counts.
A result owl or a request phase alone is not proof, since the reviewer desk can post a result owl itself.
The daily run caps still bound the retries of runs that did not record one.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import db, ids, owlery, pensieve
from .errors import ConflictError, ValidationError

DAY = 86400
RUNS_BUMP_MAX = 500
SPEND_BUMP_MIN = 0.01
SPEND_BUMP_MAX = 500.0
RUN_CAP_MAX = 100000
MAX_ROUNDS_LIMIT = 100

_ROUND_ROWS = """SELECT review_rounds.*, requests.phase AS request_phase, requests.outcome AS request_outcome,
       tasks.status AS reviewer_task_status, review_rounds.review_id IS NOT NULL AS has_verdict,
       review_passes.verdict AS verdict
   FROM review_rounds JOIN requests ON requests.id = review_rounds.request_id
   LEFT JOIN tasks ON tasks.id = requests.task_id
   LEFT JOIN review_passes ON review_passes.id = review_rounds.review_id
   WHERE review_rounds.task_id = ? ORDER BY review_rounds.created_at, review_rounds.rowid"""

Conn = sqlite3.Connection


class RoundCapReached(ConflictError):
    """An author task asked for a review round past its cap, with no allowance from Ryan left."""

    def __init__(self, task_id: str, round_no: int, max_rounds: int) -> None:
        super().__init__(f"task {task_id} asked for review round {round_no}; rounds past {max_rounds}"
                         f" need castle task allow-round {task_id}")
        self.task_id, self.round, self.max_rounds = task_id, round_no, max_rounds


# The cap day


def local_utc_offset(ts: int) -> int:
    """Seconds east of UTC in this Mac's own time zone at ts, daylight saving included."""
    return time.localtime(ts).tm_gmtoff


def _utc_of_local(local: int) -> int:
    # The moment whose local clock reads local. The second pass settles the offset across a clock change.
    return local - local_utc_offset(local - local_utc_offset(local))


def day_bounds(now: Optional[int] = None, reset_offset: Optional[int] = 0) -> tuple:
    """(start, end) of the cap day holding now. A cap day starts reset_offset seconds after UTC midnight,
    or at local midnight when reset_offset is None, so a day can be 23 or 25 hours long."""
    ts = ids.stamp(now)
    if reset_offset is None:
        midnight = (ts + local_utc_offset(ts)) // DAY * DAY
        start, end = _utc_of_local(midnight), _utc_of_local(midnight + DAY)
        if start <= ts < end:
            return start, end
        start = midnight - local_utc_offset(ts)  # a midnight the clock change skipped: keep today's offset
        return start, start + DAY
    reset_offset = ids.check_int(reset_offset, "reset offset", maximum=DAY - 1)
    start = (ts - reset_offset) // DAY * DAY + reset_offset
    return start, start + DAY


def utc_text(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ids.check_int(ts, "timestamp", maximum=ids.MAX_TIME)))


def local_text(ts: int) -> str:
    """ts on this Mac's own clock, with its offset from UTC, for events Ryan reads."""
    ts = ids.check_int(ts, "timestamp", maximum=ids.MAX_TIME)
    offset = local_utc_offset(ts)
    sign, minutes = ("-" if offset < 0 else "+"), abs(offset) // 60
    clock = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts + offset))
    return f"{clock}{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# Bumps


def _bump_amount(kind: str, amount: object) -> float:
    if kind == "runs":
        return float(ids.check_int(amount, "runs bump", minimum=1, maximum=RUNS_BUMP_MAX))
    value = ids.check_amount(amount, "spend bump", maximum=SPEND_BUMP_MAX)
    if value < SPEND_BUMP_MIN:
        raise ValidationError("invalid spend bump")
    return value


def _bump_row(row: dict) -> dict:
    return {**row, "amount": int(row["amount"]) if row["kind"] == "runs" else row["amount"]}


def add_bump(conn: Conn, desk: str, kind: str, amount: float, expires_at: int, now: Optional[int] = None) -> dict:
    """Raise one desk's runs or spend cap until expires_at, the next cap reset."""
    desk = ids.check("desk", desk)
    kind = ids.check_enum(kind, db.CAP_KINDS, "cap kind")
    value = _bump_amount(kind, amount)
    ts = ids.stamp(now)
    expires_at = ids.check_int(expires_at, "expires at", minimum=ts + 1, maximum=ids.MAX_TIME)
    with db.transaction(conn):
        pensieve.get_desk(conn, desk)
        cursor = conn.execute(
            "INSERT INTO cap_bumps(desk, kind, amount, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (desk, kind, value, ts, expires_at),
        )
        bump_id = cursor.lastrowid
    return _bump_row(db.fetch_one(conn, "SELECT * FROM cap_bumps WHERE id = ?", (bump_id,)))


def active_bumps(conn: Conn, desk: str, now: Optional[int] = None) -> dict:
    desk = ids.check("desk", desk)
    ts = ids.stamp(now)
    rows = db.fetch_all(
        conn,
        "SELECT kind, SUM(amount) AS total FROM cap_bumps WHERE desk = ? AND created_at <= ? AND expires_at > ?"
        " GROUP BY kind",
        (desk, ts, ts),
    )
    totals = {row["kind"]: row["total"] for row in rows}
    return {"runs": int(totals.get("runs") or 0), "spend": round(float(totals.get("spend") or 0.0), 6)}


def list_bumps(conn: Conn, desk: Optional[str] = None) -> list[dict]:
    desk = ids.optional("desk", desk)
    rows = db.fetch_all(conn, "SELECT * FROM cap_bumps WHERE (? IS NULL OR desk = ?) ORDER BY id", (desk, desk))
    return [_bump_row(row) for row in rows]


# Cap status and cap hits


def cap_status(conn: Conn, desk: str, run_cap: int, spend_cap: Optional[float] = None,
               now: Optional[int] = None, reset_offset: Optional[int] = 0) -> dict:
    """Runs and spend this cap day against the desk's caps plus today's bumps, and which cap is reached."""
    desk = ids.check("desk", desk)
    run_cap = ids.check_int(run_cap, "run cap", maximum=RUN_CAP_MAX)
    spend_cap = None if spend_cap is None else ids.check_amount(spend_cap, "spend cap")
    ts = ids.stamp(now)
    start, end = day_bounds(ts, reset_offset)
    with db.snapshot(conn):
        pensieve.get_desk(conn, desk)
        used = db.fetch_one(
            conn,
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost_usd FROM metrics"
            " WHERE desk = ? AND ts >= ? AND ts < ?",
            (desk, start, end),
        )
        bumps = active_bumps(conn, desk, ts)
    runs_limit = run_cap + bumps["runs"]
    spend_limit = None if spend_cap is None else round(spend_cap + bumps["spend"], 6)
    spend_used = round(float(used["cost_usd"]), 6)
    reached = None
    if used["runs"] >= runs_limit:
        reached = "runs"
    elif spend_limit is not None and spend_used >= spend_limit:
        reached = "spend"
    return {
        "desk": desk, "day_start": start, "resets_at": end, "resets_at_utc": utc_text(end),
        "resets_at_local": local_text(end),
        "runs_used": used["runs"], "run_cap": run_cap, "runs_bump": bumps["runs"], "runs_limit": runs_limit,
        "spend_used_usd": spend_used, "spend_cap_usd": spend_cap, "spend_bump_usd": bumps["spend"],
        "spend_limit_usd": spend_limit, "reached": reached,
    }


def record_cap_hit(conn: Conn, desk: str, cap: str, cap_source: str, run_id: Optional[str] = None,
                   now: Optional[int] = None) -> dict:
    """One refusal by a fleet cap (runs or spend), or one run a vendor's own limit stopped (plan)."""
    desk = ids.check("desk", desk)
    cap = ids.check_enum(cap, db.CAP_HIT_CAPS, "cap")
    cap_source = ids.check_enum(cap_source, db.CAP_SOURCES, "cap source")
    if (cap_source == "fleet") == (cap == "plan"):
        raise ValidationError("a fleet cap is runs or spend, and a vendor limit is plan")
    run_id = ids.optional("label", run_id, "run id")
    ts = ids.stamp(now)
    with db.transaction(conn):
        pensieve.get_desk(conn, desk)
        cursor = conn.execute(
            "INSERT INTO cap_hits(ts, desk, cap, cap_source, run_id) VALUES (?, ?, ?, ?, ?)",
            (ts, desk, cap, cap_source, run_id),
        )
        hit_id = cursor.lastrowid
    return db.fetch_one(conn, "SELECT * FROM cap_hits WHERE id = ?", (hit_id,))


def list_cap_hits(conn: Conn, desk: Optional[str] = None, since: int = 0) -> list[dict]:
    desk = ids.optional("desk", desk)
    since = ids.check_int(since, "since")
    return db.fetch_all(
        conn, "SELECT * FROM cap_hits WHERE (? IS NULL OR desk = ?) AND ts >= ? ORDER BY id", (desk, desk, since)
    )


def waiting_requests(conn: Conn, desk: str) -> list[dict]:
    """Requests addressed to desk that are still queued: delivered, but no run of the desk has taken them."""
    desk = pensieve.get_desk(conn, desk)["name"]
    return db.fetch_all(
        conn,
        "SELECT id, requester, parent_task_id, task_id, title, created_at FROM requests"
        " WHERE recipient = ? AND phase = 'queued' AND outcome IS NULL ORDER BY created_at, rowid",
        (desk,),
    )


# Review rounds


def _round_rows(conn: Conn, task_id: str) -> list[dict]:
    return db.fetch_all(conn, _ROUND_ROWS, (task_id,))


def _is_waiting(row: dict) -> bool:
    # Waiting: requested and delivered, but the reviewer's run never started its task.
    return (row["superseded_by"] is None and row["request_outcome"] is None and row["request_phase"] == "queued"
            and row["reviewer_task_status"] == "queued")


def _ended_without_verdict(row: dict) -> bool:
    # The reviewer's run is over (crashed, timed out, refused or stopped at a vendor limit) and no review
    # result was posted, or the request was deferred or declined: the round is not used up.
    if row["has_verdict"]:
        return False
    return row["request_outcome"] in ("deferred", "declined") or row["reviewer_task_status"] == "closed"


def _holds_round(row: dict) -> bool:
    """A live round that counts toward the cap: it recorded a verdict, or its run has not ended yet."""
    return row["superseded_by"] is None and not _ended_without_verdict(row)


def _unused_allowances(conn: Conn, task_id: str, holding: list) -> list[int]:
    used = {row["allowance_id"] for row in holding if row["allowance_id"] is not None}
    rows = db.fetch_all(conn, "SELECT id FROM round_allowances WHERE task_id = ? ORDER BY id", (task_id,))
    return [row["id"] for row in rows if row["id"] not in used]


def review_rounds(conn: Conn, task_id: str) -> list[dict]:
    """Every review round of an author task. counts says whether it uses up a round: a recorded verdict
    does, and so does a round whose run has not ended yet; a run that ended without one does not."""
    task_id = pensieve.get_task(conn, ids.check("task", task_id))["id"]
    return [{**row, "has_verdict": bool(row["has_verdict"]), "waiting": _is_waiting(row),
             "counts": _holds_round(row)} for row in _round_rows(conn, task_id)]


def allow_round(conn: Conn, task_id: str, now: Optional[int] = None) -> dict:
    """Ryan's allowance for exactly one more review round. A second call before it is used changes nothing."""
    task_id = ids.check("task", task_id)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if pensieve.get_task(conn, task_id)["status"] == "closed":
            raise ConflictError("task is closed")
        holding = [row for row in _round_rows(conn, task_id) if _holds_round(row)]
        unused = _unused_allowances(conn, task_id, holding)
        created = not unused
        if created:
            allowance_id = conn.execute(
                "INSERT INTO round_allowances(task_id, granted_at) VALUES (?, ?)", (task_id, ts)
            ).lastrowid
        else:
            allowance_id = unused[0]
        row = db.fetch_one(conn, "SELECT * FROM round_allowances WHERE id = ?", (allowance_id,))
    return {**row, "created": created, "rounds": len(holding)}


def _ack_request_owl(conn: Conn, request_id: str, reviewer: str, ts: int) -> None:
    owl = db.fetch_one(conn, "SELECT id FROM owls WHERE request_id = ? AND kind = 'request'", (request_id,))
    if owl is not None:
        owlery.read(conn, owl["id"], reviewer, now=ts)
        owlery.ack(conn, owl["id"], reviewer, now=ts)


def open_review_round(conn: Conn, task_id: str, reviewer_desk: str, sha: str, title: str,
                      body: Optional[str] = None, max_rounds: int = 3, idempotency_key: Optional[str] = None,
                      now: Optional[int] = None) -> dict:
    """Open the review request for one commit of an author task, as its next round.

    A review of this task still waiting for its reviewer's run is superseded by this one: its request
    is deferred (reason conflict), its owl acked, and its row names this request. Only a round whose
    reviewer run recorded a verdict (PASS, CHANGES or HEADMASTER) uses up a round, and a round whose
    run has not ended yet holds its place until it does. Superseded rounds, and runs that crashed,
    timed out, were refused by a cap or stopped at a vendor limit, do not count, and give back any
    allowance they took. A round past max_rounds takes one of Ryan's allowances, or is refused.
    """
    task_id = ids.check("task", task_id)
    reviewer_desk = ids.check("desk", reviewer_desk, "reviewer desk")
    sha = ids.check("sha", sha)
    max_rounds = ids.check_int(max_rounds, "max rounds", minimum=1, maximum=MAX_ROUNDS_LIMIT)
    idempotency_key = ids.optional("key", idempotency_key)
    ts = ids.stamp(now)
    with db.transaction(conn):
        task = pensieve.get_task(conn, task_id)
        live = [row for row in _round_rows(conn, task_id) if row["superseded_by"] is None]
        waiting = {row["request_id"]: row for row in live if _is_waiting(row) and row["reviewer"] == reviewer_desk}
        counted = [row for row in live if row["request_id"] not in waiting and _holds_round(row)]
        round_no = len(counted) + 1
        allowance_id = None
        if round_no > max_rounds:
            free = _unused_allowances(conn, task_id, counted)
            if not free:
                raise RoundCapReached(task_id, round_no, max_rounds)
            allowance_id = free[0]
        opened = owlery.open_request(conn, task["desk"], reviewer_desk, title, body=body, parent_task_id=task_id,
                                     idempotency_key=idempotency_key, now=ts)
        request_id = opened["request"]["id"]
        if not opened["created"]:
            existing = db.fetch_one(conn, "SELECT * FROM review_rounds WHERE request_id = ?", (request_id,))
            if existing is None:
                raise ConflictError("that request was not opened as a review round")
            return {**opened, "round": existing["round"], "max_rounds": max_rounds,
                    "allowance_id": existing["allowance_id"], "superseded": []}
        superseded = []
        for old_id, row in waiting.items():
            conn.execute(
                "UPDATE review_rounds SET superseded_by = ?, superseded_at = ? WHERE request_id = ?"
                " AND superseded_by IS NULL",
                (request_id, ts, old_id),
            )
            owlery.defer(conn, old_id, "conflict", now=ts)
            _ack_request_owl(conn, old_id, reviewer_desk, ts)
            superseded.append({"request_id": old_id, "sha": row["sha"], "round": row["round"]})
        conn.execute(
            "INSERT INTO review_rounds(request_id, task_id, reviewer, sha, round, allowance_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, task_id, reviewer_desk, sha, round_no, allowance_id, ts),
        )
    return {**opened, "round": round_no, "max_rounds": max_rounds, "allowance_id": allowance_id,
            "superseded": superseded}


def record_round_verdict(conn: Conn, request_id: str, repo: str, verdict: str, review_path: Optional[str] = None,
                         now: Optional[int] = None) -> dict:
    """Record the reviewer's verdict for one review round and tie it to that round, in one transaction.

    The round counts from this moment, whatever happens after: a failure to publish the review or to
    post its result owl cannot give the round, or the allowance it took, back.
    """
    request_id = ids.check("request", request_id)
    repo = ids.check("repo", repo)
    verdict = ids.check_enum(verdict, db.REVIEW_VERDICTS, "review verdict")
    review_path = ids.optional_path(review_path, "review path", ids.REVIEWS_ROOT)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = db.fetch_one(conn, "SELECT * FROM review_rounds WHERE request_id = ?", (request_id,))
        if row is None:
            raise ConflictError("that request was not opened as a review round")
        if row["superseded_by"] is not None:
            raise ConflictError("that review round was superseded by a newer commit")
        if row["review_id"] is not None:
            raise ConflictError("that review round already has a verdict")
        if owlery.get_request(conn, request_id)["outcome"] is not None:
            raise ConflictError("that review request already has an outcome")
        review = owlery.record_review(conn, repo, row["sha"], row["task_id"], row["reviewer"], verdict,
                                      review_path=review_path, now=ts)
        conn.execute("UPDATE review_rounds SET review_id = ? WHERE request_id = ? AND review_id IS NULL",
                     (review["id"], request_id))
    return review
