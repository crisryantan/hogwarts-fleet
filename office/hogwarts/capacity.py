"""Busy-day capacity: cap bumps, which cap stopped a desk, run launches, and review rounds per author task.

The cap numbers live in the fleet's config. The store keeps what changes during a day: the bumps
Ryan makes with castle desk cap, one row each time a cap refuses a run or a vendor limit stops one,
one launch row per headless run, and one row per review request.

A run counts toward the daily run cap from its launch row, written before its process starts, so a run
that is killed, crashes or is interrupted before it records usage still counts. The row names the desk's own
task the run is for, so in_flight can tell which of a desk's many tasks has a run going. Its usage and cost go on
a metrics row tied to that launch when it ends, and the spend cap reads the recorded cost.

A newer commit supersedes a review that is still waiting, and a round past the cap waits for Ryan's
castle task allow-round. A waiting round holds nothing, not even an allowance it took, since the next
review of the task supersedes it. Only a reviewer run that recorded a verdict uses up a round, and the proof is
the verdict itself: record_round_verdict stores the review and ties it to its round in one transaction,
so a review recorded before a later step failed still counts. A result owl or a request phase alone is
not proof, since the reviewer desk can post a result owl itself. The daily run caps bound the retries
of runs that did not record one.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import db, ids, owlery, pensieve
from .errors import ConflictError, NotFoundError, ValidationError

DAY = 86400
RUNS_BUMP_MAX = 500
SPEND_BUMP_MIN = 0.01
SPEND_BUMP_MAX = 500.0
RUN_CAP_MAX = 100000
MAX_ROUNDS_LIMIT = 100

_ROUND_ROWS = """SELECT review_rounds.*, requests.phase AS request_phase, requests.outcome AS request_outcome,
       requests.task_id AS reviewer_task_id, tasks.status AS reviewer_task_status,
       review_rounds.review_id IS NOT NULL AS has_verdict,
       review_passes.verdict AS verdict
   FROM review_rounds JOIN requests ON requests.id = review_rounds.request_id
   LEFT JOIN tasks ON tasks.id = requests.task_id
   LEFT JOIN review_passes ON review_passes.id = review_rounds.review_id
   WHERE review_rounds.task_id = ? ORDER BY review_rounds.created_at, review_rounds.rowid"""

# Runs this cap day: every launch, plus usage recorded without one (castle metric add). Spend is recorded cost.
_USED_TODAY = """SELECT
       (SELECT COUNT(*) FROM run_launches WHERE desk = ? AND launched_at >= ? AND launched_at < ?)
     + (SELECT COUNT(*) FROM metrics WHERE desk = ? AND ts >= ? AND ts < ?
          AND NOT EXISTS (SELECT 1 FROM run_launches WHERE run_launches.metric_id = metrics.id)) AS runs,
       (SELECT COALESCE(SUM(cost_usd), 0) FROM metrics WHERE desk = ? AND ts >= ? AND ts < ?) AS cost_usd"""

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
        used = db.fetch_one(conn, _USED_TODAY, (desk, start, end) * 3)
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


# Run launches


def record_launch(conn: Conn, desk: str, run_id: str, model: str, task_id: Optional[str] = None,
                  now: Optional[int] = None) -> dict:
    """A headless run about to start. It counts toward the desk's daily run cap from now, however it ends.
    task_id is the desk's own task the run is for, when it has one, and never changes."""
    desk = ids.check("desk", desk)
    run_id = ids.check("label", run_id, "run id")
    model = ids.check("label", model, "model")
    task_id = ids.optional("task", task_id)
    ts = ids.stamp(now)
    with db.transaction(conn):
        pensieve.get_desk(conn, desk)
        if task_id is not None and pensieve.get_task(conn, task_id)["desk"] != desk:
            raise ConflictError("a run launch names a task of its own desk")
        if db.fetch_one(conn, "SELECT run_id FROM run_launches WHERE run_id = ?", (run_id,)) is not None:
            raise ConflictError("that run was already launched")
        conn.execute("INSERT INTO run_launches(run_id, desk, model, launched_at, task_id) VALUES (?, ?, ?, ?, ?)",
                     (run_id, desk, model, ts, task_id))
    return db.fetch_one(conn, "SELECT * FROM run_launches WHERE run_id = ?", (run_id,))


def record_launch_usage(conn: Conn, run_id: str, input_tokens: int, output_tokens: int, cache_read_tokens: int,
                        cost_usd: float, duration_ms: int, model: Optional[str] = None,
                        now: Optional[int] = None) -> dict:
    """The usage of a launched run that ended: its metrics row, tied to the launch in one transaction.
    model is the one that really ran when the run said so, else the one it was launched with."""
    run_id = ids.check("label", run_id, "run id")
    model = ids.optional("label", model, "model")
    with db.transaction(conn):
        launch = db.fetch_one(conn, "SELECT * FROM run_launches WHERE run_id = ?", (run_id,))
        if launch is None:
            raise NotFoundError("run launch not found")
        if launch["metric_id"] is not None:
            raise ConflictError("that run's usage is already recorded")
        metric = pensieve.add_metric(conn, launch["desk"], run_id, model or launch["model"], input_tokens,
                                     output_tokens, cache_read_tokens, cost_usd, duration_ms, ts=now)
        conn.execute("UPDATE run_launches SET metric_id = ? WHERE run_id = ? AND metric_id IS NULL",
                     (metric["id"], run_id))
    return metric


def list_launches(conn: Conn, desk: Optional[str] = None) -> list[dict]:
    desk = ids.optional("desk", desk)
    return db.fetch_all(conn, "SELECT * FROM run_launches WHERE (? IS NULL OR desk = ?) ORDER BY launched_at, rowid",
                        (desk, desk))


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
    """A live round that counts toward the cap: it recorded a verdict, or its run started and has not ended
    yet. A waiting round holds nothing, and neither does the allowance it took: the next review supersedes it."""
    return row["superseded_by"] is None and not _is_waiting(row) and not _ended_without_verdict(row)


def _left_by_dead_review(row: dict) -> bool:
    # Its reviewer task is still active with no verdict. Under the task's review lock, which every live review
    # and its reviewer's own process hold, that run is over: the review died before closing the task.
    return not row["has_verdict"] and row["reviewer_task_status"] == "active"


def _unused_allowances(conn: Conn, task_id: str, holding: list) -> list[int]:
    used = {row["allowance_id"] for row in holding if row["allowance_id"] is not None}
    rows = db.fetch_all(conn, "SELECT id FROM round_allowances WHERE task_id = ? ORDER BY id", (task_id,))
    return [row["id"] for row in rows if row["id"] not in used]


def _needs_allowance(conn: Conn, task_id: str, holding: list, max_rounds: int) -> bool:
    return len(holding) >= max_rounds and not _unused_allowances(conn, task_id, holding)


def needs_allowance(conn: Conn, task_id: str, max_rounds: int = 3) -> bool:
    """Whether the task's next review round waits for Ryan: max_rounds rounds count, and none of his
    allowances for the task is unused."""
    task_id = pensieve.get_task(conn, ids.check("task", task_id))["id"]
    max_rounds = ids.check_int(max_rounds, "max rounds", minimum=1, maximum=MAX_ROUNDS_LIMIT)
    holding = [row for row in _round_rows(conn, task_id) if _holds_round(row)]
    return _needs_allowance(conn, task_id, holding, max_rounds)


def review_rounds(conn: Conn, task_id: str) -> list[dict]:
    """Every review round of an author task. counts says whether it uses up a round: a recorded verdict
    does, and so does a round whose run started and has not ended yet; a run that ended without one does
    not, and neither does a round still waiting for its reviewer's run."""
    task_id = pensieve.get_task(conn, ids.check("task", task_id))["id"]
    return [{**row, "has_verdict": bool(row["has_verdict"]), "waiting": _is_waiting(row),
             "counts": _holds_round(row)} for row in _round_rows(conn, task_id)]


def stranded_rounds(conn: Conn, reviewer_desk: str) -> list[dict]:
    """Review rounds addressed to reviewer_desk whose reviewer task is still active. A review holds the
    reviewer's desk lock until its reviewer task is closed, so whoever holds that lock and finds one here
    has found a task left by a review that died. Only review-round tasks are listed: the reviewer's other
    active tasks are not stranded, since the desk may hold many."""
    reviewer_desk = pensieve.get_desk(conn, ids.check("desk", reviewer_desk, "reviewer desk"))["name"]
    rows = db.fetch_all(
        conn,
        "SELECT review_rounds.request_id, review_rounds.task_id, requests.task_id AS reviewer_task_id,"
        " review_rounds.review_id IS NOT NULL AS has_verdict"
        " FROM review_rounds JOIN requests ON requests.id = review_rounds.request_id"
        " JOIN tasks ON tasks.id = requests.task_id"
        " WHERE review_rounds.reviewer = ? AND tasks.status = 'active'"
        " ORDER BY review_rounds.created_at, review_rounds.rowid",
        (reviewer_desk,),
    )
    return [{**row, "has_verdict": bool(row["has_verdict"])} for row in rows]


def allow_round(conn: Conn, task_id: str, now: Optional[int] = None) -> dict:
    """Ryan's allowance for exactly one more review round. A second call before it is used changes nothing,
    and a round still waiting does not use it: the review that supersedes that round takes it instead."""
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
                      review_locked: bool = False, now: Optional[int] = None) -> dict:
    """Open the review request for one commit of an author task, as its next round.

    A review of this task still waiting for its reviewer's run is superseded by this one: its request
    is deferred (reason conflict), its owl acked, and its row names this request. Only a round whose
    reviewer run recorded a verdict (PASS, CHANGES or HEADMASTER) uses up a round, and a round whose
    run has not ended yet holds its place until it does. Superseded rounds, and runs that crashed,
    timed out, were refused by a cap or stopped at a vendor limit, do not count, and give back any
    allowance they took. A round past max_rounds takes one of Ryan's allowances, or is refused.

    review_locked means the caller holds this task's review lock, which every live review of the task
    and its reviewer's own process hold. A round whose reviewer task is still active with no verdict was
    then left by a review that died, so it does not count, even before the next review that can take the
    reviewer's desk lock closes that task.
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
        counted = [row for row in live if _holds_round(row) and not (review_locked and _left_by_dead_review(row))]
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


# What is in flight


# The order Ryan reads tasks in: what needs him first, oldest first within a state.
FLIGHT_STATES = ("awaiting close", "HEADMASTER", "round cap", "CHANGES", "review died", "review queued", "in review",
                 "running", "working")
NEEDS_RYAN = ("awaiting close", "HEADMASTER", "round cap", "CHANGES", "review died")


def review_task_ids(conn: Conn) -> set:
    """The ids of every reviewer task a review round opened."""
    rows = db.fetch_all(conn, "SELECT requests.task_id FROM review_rounds JOIN requests"
                              " ON requests.id = review_rounds.request_id WHERE requests.task_id IS NOT NULL")
    return {row["task_id"] for row in rows}


def round_author(conn: Conn, reviewer_task_id: str) -> Optional[str]:
    """The author task whose review round opened this reviewer task, or None for any other task."""
    row = db.fetch_one(conn, "SELECT review_rounds.task_id FROM review_rounds JOIN requests"
                             " ON requests.id = review_rounds.request_id WHERE requests.task_id = ?",
                       (ids.check("task", reviewer_task_id),))
    return None if row is None else row["task_id"]


# A launch with no usage yet, recent enough to still be running, for the task or one of its rounds' reviewers.
_RUNNING = """SELECT 1 AS found FROM run_launches WHERE metric_id IS NULL AND launched_at > ? AND (task_id = ?
       OR task_id IN (SELECT requests.task_id FROM review_rounds JOIN requests ON requests.id = review_rounds.request_id
                      WHERE review_rounds.task_id = ?))"""


def _running(conn: Conn, task_id: str, since: int) -> bool:
    return db.fetch_one(conn, _RUNNING, (since, task_id, task_id)) is not None


def _flight_state(task: dict, latest: Optional[dict], running: bool, needs_allowance: bool) -> str:
    if task["status"] == "awaiting_close":
        return "awaiting close"
    if latest is not None and latest["waiting"]:
        return "review queued"
    if latest is not None and not latest["has_verdict"]:
        # A round with no verdict and no run going was left by a review that died before closing its round.
        return "in review" if running else "review died"
    if latest is not None and latest["verdict"] == "HEADMASTER":
        return "HEADMASTER"
    if latest is not None and latest["verdict"] == "CHANGES" and not running:
        return "round cap" if needs_allowance else "CHANGES"
    return "running" if running else "working"


def _flight_row(conn: Conn, task: dict, since: int, max_rounds: int) -> dict:
    rows = review_rounds(conn, task["id"])
    # A round whose run ended without a verdict neither counts nor says where the task stands.
    live = [row for row in rows if row["superseded_by"] is None and (row["counts"] or row["waiting"])]
    latest = live[-1] if live else None
    holding = [row for row in rows if row["counts"]]
    needs_allowance = _needs_allowance(conn, task["id"], holding, max_rounds)
    running = _running(conn, task["id"], since)
    return {
        "id": task["id"], "desk": task["desk"], "title": task["title"], "status": task["status"],
        "created_at": task["created_at"], "worktree": task["worktree"], "request_id": task["request_id"],
        "round": None if latest is None else latest["round"],
        "verdict": None if latest is None else latest["verdict"],
        "waiting": bool(latest is not None and latest["waiting"]),
        "rounds_used": len(holding), "max_rounds": max_rounds, "needs_allowance": needs_allowance,
        "running": running, "state": _flight_state(task, latest, running, needs_allowance),
    }


def in_flight(conn: Conn, now: Optional[int] = None, running_window: int = 3600, desk: Optional[str] = None,
              max_rounds: int = 3) -> dict:
    """Every active or awaiting-close author task, grouped by desk, with where it stands.

    A reviewer's round task is folded into its author task and never listed alone. running means a launch
    for the task, or for one of its rounds' reviewer tasks, has no usage yet and started within
    running_window: a run killed before it recorded usage stops counting as running once the window passes.
    A latest round with no verdict reads "in review" while a run is going and "review died" once none is.
    Read from launch rows only, never by probing a lock, so reading it never makes a review queue.
    """
    ts = ids.stamp(now)
    running_window = ids.check_int(running_window, "running window", minimum=1, maximum=7 * DAY)
    desk = ids.optional("desk", desk)
    max_rounds = ids.check_int(max_rounds, "max rounds", minimum=1, maximum=MAX_ROUNDS_LIMIT)
    with db.snapshot(conn):
        if desk is not None:
            pensieve.get_desk(conn, desk)
        reviews = review_task_ids(conn)
        tasks = [task for task in db.fetch_all(
            conn, "SELECT * FROM tasks WHERE status IN ('active', 'awaiting_close') AND (? IS NULL OR desk = ?)"
                  " ORDER BY created_at, rowid", (desk, desk)) if task["id"] not in reviews]
        rows = [_flight_row(conn, task, ts - running_window, max_rounds) for task in tasks]
    desks: dict = {}
    for row in rows:
        desks.setdefault(row["desk"], []).append(row)
    grouped = []
    for name in sorted(desks):
        states: dict = {}
        for row in desks[name]:
            states[row["state"]] = states.get(row["state"], 0) + 1
        grouped.append({"desk": name, "count": len(desks[name]),
                        "running": sum(1 for row in desks[name] if row["running"]),
                        "states": {state: states[state] for state in FLIGHT_STATES if state in states},
                        "tasks": desks[name]})
    return {"now": ts, "tasks": len(rows), "desks": grouped}
