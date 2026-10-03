from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import secrets
import sqlite3
from typing import Optional

from . import db, ids, pensieve
from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, TokenError, ValidationError

REQUEST_PHASES = db.REQUEST_PHASES
DEFERRABLE_PHASES = ("queued", "claimed", "running")
SUBJECT_LIMIT = 200
BODY_LIMIT = 16000
DETAIL_LIMIT = 500
TOKEN_TTL_DEFAULT = 600
TOKEN_TTL_MAX = 86400
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
DAY = 86400
REQUEST_STALE_AFTER = 3600
OWL_RERING_AFTER = 1800
OWL_ESCALATE_AFTER = 7200
ACTIVE_STALE_AFTER = 8 * 3600
AWAITING_STALE_AFTER = 24 * 3600

_HASHED_FIELDS = (
    "sender", "recipient", "kind", "task_id", "request_id", "in_reply_to", "subject", "body", "body_path",
)
_META_COLUMNS = (
    "id", "sender", "recipient", "kind", "task_id", "request_id", "in_reply_to", "subject",
    "body_path", "created_at", "delivered_at", "read_at", "acked_at", "purged_at",
)
_SELECT_META = "SELECT " + ", ".join(_META_COLUMNS) + ", body IS NOT NULL AS has_body FROM owls"

Conn = sqlite3.Connection


def _digest(*parts: object) -> str:
    material = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("ascii")).hexdigest()


# Owls


def _as_meta(row: dict) -> dict:
    return {**row, "has_body": bool(row["has_body"])}


def _owl_meta(conn: Conn, owl_id: str) -> dict:
    row = db.fetch_one(conn, _SELECT_META + " WHERE id = ?", (owl_id,))
    if row is None:
        raise NotFoundError("owl not found")
    return _as_meta(row)


def _owl(conn: Conn, owl_id: str) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM owls WHERE id = ?", (owl_id,))


def _owl_fields(sender: str, recipient: str, kind: str, subject: str, body: Optional[str],
                body_path: Optional[str], task_id: Optional[str], request_id: Optional[str],
                in_reply_to: Optional[str]) -> dict:
    fields = {
        "sender": ids.check("desk", sender, "sender"),
        "recipient": ids.check("desk", recipient, "recipient"),
        "kind": ids.check_enum(kind, db.OWL_KINDS, "owl kind"),
        "task_id": ids.optional("task", task_id),
        "request_id": ids.optional("request", request_id),
        "in_reply_to": ids.optional("owl", in_reply_to, "reply owl id"),
        "subject": ids.clean_text(subject, "subject", SUBJECT_LIMIT, single_line=True),
        "body": ids.optional_text(body, "body", BODY_LIMIT, keep_format=True),
        "body_path": ids.optional_path(body_path, "body path", ids.outbox_root(sender)),
    }
    if fields["sender"] == fields["recipient"]:
        raise ValidationError("an owl cannot be sent to its own sender")
    if fields["body"] is not None and fields["body_path"] is not None:
        raise ValidationError("an owl carries a body or a body path, not both")
    return fields


def send(conn: Conn, sender: str, recipient: str, kind: str, subject: str,
         body: Optional[str] = None, body_path: Optional[str] = None,
         task_id: Optional[str] = None, request_id: Optional[str] = None,
         in_reply_to: Optional[str] = None, idempotency_key: Optional[str] = None,
         now: Optional[int] = None) -> dict:
    fields = _owl_fields(sender, recipient, kind, subject, body, body_path, task_id, request_id, in_reply_to)
    if fields["kind"] == "request":
        raise ValidationError("request owls are created by open_request")
    key = ids.optional("key", idempotency_key)
    ts = ids.stamp(now)
    with db.transaction(conn):
        return _deliver(conn, fields, key, ts)


def _deliver(conn: Conn, fields: dict, key: Optional[str], ts: int) -> dict:
    if key is None:
        idem_hash, existing = _unacked_twin(conn, fields)
    else:
        idem_hash = _digest("key", fields["sender"], key)
        existing = db.fetch_one(conn, "SELECT * FROM owls WHERE idem_hash = ?", (idem_hash,))
        if existing is not None:
            _require_same_owl(existing, fields)
    if existing is not None:
        return {**_owl_meta(conn, existing["id"]), "created": False}
    _check_owl_rules(conn, fields)
    owl_id = ids.new_id("owl")
    conn.execute(
        "INSERT INTO owls(id, idem_hash, sender, recipient, kind, task_id, request_id, in_reply_to,"
        " subject, body, body_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (owl_id, idem_hash, *(fields[name] for name in _HASHED_FIELDS), ts),
    )
    return {**_owl_meta(conn, owl_id), "created": True}


def _unacked_twin(conn: Conn, fields: dict) -> tuple:
    # Content dedupe only folds into an owl that is still unacked; each acked copy bumps the generation.
    for generation in itertools.count():
        idem_hash = _digest("content", generation, *(fields[name] for name in _HASHED_FIELDS))
        existing = db.fetch_one(conn, "SELECT * FROM owls WHERE idem_hash = ?", (idem_hash,))
        if existing is None or existing["acked_at"] is None:
            return idem_hash, existing


def _require_same_owl(existing: dict, fields: dict) -> None:
    compared = [name for name in _HASHED_FIELDS if not (name == "body" and existing["purged_at"])]
    if any(existing[name] != fields[name] for name in compared):
        raise ConflictError("idempotency key was already used for a different owl")


def _check_owl_rules(conn: Conn, fields: dict) -> None:
    pensieve.get_desk(conn, fields["sender"])
    pensieve.get_desk(conn, fields["recipient"])
    if fields["task_id"] is not None:
        pensieve.get_task(conn, fields["task_id"])
    request = None if fields["request_id"] is None else _require_request(conn, fields["request_id"])
    if request is not None and {fields["sender"], fields["recipient"]} != {request["requester"], request["recipient"]}:
        raise ValidationError("owl parties must match the request parties")
    if request is not None and fields["task_id"] not in (None, request["task_id"], request["parent_task_id"]):
        raise ValidationError("owl task must be the request task or its parent task")
    replied = None
    if fields["in_reply_to"] is not None:
        replied = _owl(conn, fields["in_reply_to"])
        if replied is None:
            raise NotFoundError("replied owl not found")
        if fields["sender"] not in (replied["sender"], replied["recipient"]):
            raise ValidationError("an owl can only reply within its own thread")
    if fields["kind"] == "request" and (request is None or request["requester"] != fields["sender"]):
        raise ValidationError("a request owl must come from the requester")
    if fields["kind"] == "answer":
        _check_answer(conn, fields, replied)
    if fields["kind"] == "result":
        _check_result(fields, request)


def _check_answer(conn: Conn, fields: dict, question: Optional[dict]) -> None:
    if question is None or question["kind"] != "question":
        raise ValidationError("an answer must reply to a question")
    if question["recipient"] != fields["sender"] or question["sender"] != fields["recipient"]:
        raise ValidationError("only the asked desk can answer, and only to the asker")
    if db.fetch_one(
        conn, "SELECT id FROM owls WHERE kind = 'answer' AND in_reply_to = ?", (question["id"],)
    ) is not None:
        raise ConflictError("question already has an answer")


def _check_result(fields: dict, request: Optional[dict]) -> None:
    if request is None:
        raise ValidationError("a result must carry a request id")
    if request["recipient"] != fields["sender"]:
        raise ValidationError("only the request recipient can post its result")
    if request["outcome"] in ("deferred", "declined"):
        raise ConflictError("request was deferred or declined")


def inbox(conn: Conn, recipient: str, include_acked: bool = False) -> list[dict]:
    recipient = pensieve.get_desk(conn, recipient)["name"]
    rows = db.fetch_all(
        conn,
        _SELECT_META + " WHERE recipient = ? AND (? OR acked_at IS NULL) ORDER BY created_at, rowid",
        (recipient, 1 if include_acked else 0),
    )
    return [_as_meta(row) for row in rows]


def request_owls(conn: Conn, request_id: str) -> list[dict]:
    request = _require_request(conn, ids.check("request", request_id))
    rows = db.fetch_all(conn, _SELECT_META + " WHERE request_id = ? ORDER BY created_at, rowid", (request["id"],))
    return [_as_meta(row) for row in rows]


def _addressed(conn: Conn, owl_id: str, desk: str) -> dict:
    owl = _owl(conn, owl_id)
    if owl is None or owl["recipient"] != desk:
        raise NotFoundError("owl not found")
    return owl


def read(conn: Conn, owl_id: str, desk: str, now: Optional[int] = None) -> dict:
    owl_id = ids.check("owl", owl_id)
    desk = ids.check("desk", desk)
    ts = ids.stamp(now)
    with db.transaction(conn):
        body = _addressed(conn, owl_id, desk)["body"]
        conn.execute(
            "UPDATE owls SET read_at = COALESCE(read_at, ?), delivered_at = COALESCE(delivered_at, ?)"
            " WHERE id = ?",
            (ts, ts, owl_id),
        )
    return {**_owl_meta(conn, owl_id), "body": body}


def ack(conn: Conn, owl_id: str, desk: str, now: Optional[int] = None) -> dict:
    owl_id = ids.check("owl", owl_id)
    desk = ids.check("desk", desk)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if _addressed(conn, owl_id, desk)["read_at"] is None:
            raise ConflictError("read the owl before acking it")
        conn.execute("UPDATE owls SET acked_at = COALESCE(acked_at, ?) WHERE id = ?", (ts, owl_id))
    return _owl_meta(conn, owl_id)


def mark_delivered(conn: Conn, owl_id: str, now: Optional[int] = None) -> dict:
    owl_id = ids.check("owl", owl_id)
    ts = ids.stamp(now)
    with db.transaction(conn):
        _owl_meta(conn, owl_id)
        conn.execute("UPDATE owls SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?", (ts, owl_id))
    return _owl_meta(conn, owl_id)


# Requests


def _request(conn: Conn, request_id: str) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM requests WHERE id = ?", (request_id,))


def _require_request(conn: Conn, request_id: str) -> dict:
    request = _request(conn, request_id)
    if request is None:
        raise NotFoundError("request not found")
    return request


def _record_phase(conn: Conn, request_id: str, phase: str, ts: int, detail: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO request_phases(request_id, phase, ts, detail) VALUES (?, ?, ?, ?)",
        (request_id, phase, ts, detail),
    )


def get_request(conn: Conn, request_id: str) -> dict:
    request = _require_request(conn, ids.check("request", request_id))
    history = db.fetch_all(
        conn,
        "SELECT phase, ts, detail FROM request_phases WHERE request_id = ? ORDER BY id",
        (request["id"],),
    )
    return {**request, "history": history}


def list_requests(conn: Conn, desk: Optional[str] = None, phase: Optional[str] = None,
                  open_only: bool = False) -> list[dict]:
    desk = ids.optional("desk", desk)
    phase = None if phase is None else ids.check_enum(phase, REQUEST_PHASES, "request phase")
    return db.fetch_all(
        conn,
        """SELECT * FROM requests
           WHERE (? IS NULL OR requester = ? OR recipient = ?)
             AND (? IS NULL OR phase = ?)
             AND (? = 0 OR (phase <> 'cleaned' AND (outcome IS NULL OR outcome = 'done')))
           ORDER BY created_at, rowid""",
        (desk, desk, desk, phase, phase, 1 if open_only else 0),
    )


def _opened(conn: Conn, request_id: str, created: bool) -> dict:
    request = get_request(conn, request_id)
    owl = db.fetch_one(conn, _SELECT_META + " WHERE request_id = ? AND kind = 'request'", (request_id,))
    return {
        "request": request,
        "task": pensieve.get_task(conn, request["task_id"]),
        "owl": _as_meta(owl),
        "created": created,
    }


def open_request(conn: Conn, requester: str, recipient: str, title: str,
                 body: Optional[str] = None, body_path: Optional[str] = None,
                 parent_task_id: Optional[str] = None, idempotency_key: Optional[str] = None,
                 now: Optional[int] = None) -> dict:
    fields = _owl_fields(requester, recipient, "request", title, body, body_path, None, None, None)
    parent_task_id = ids.optional("task", parent_task_id, "parent task id")
    key = ids.optional("key", idempotency_key)
    ts = ids.stamp(now)
    with db.transaction(conn):
        if key is None:
            idem_key, existing = _live_twin(conn, fields, parent_task_id)
        else:
            idem_key = _digest("key", fields["sender"], key)
            existing = db.fetch_one(conn, "SELECT * FROM requests WHERE idem_key = ?", (idem_key,))
            if existing is not None:
                _require_same_request(conn, existing, fields, parent_task_id)
        if existing is not None:
            return _opened(conn, existing["id"], created=False)
        pensieve.get_desk(conn, fields["sender"])
        pensieve.get_desk(conn, fields["recipient"])
        if parent_task_id is not None:
            _check_parent(conn, parent_task_id, fields["sender"])
        request_id = ids.new_id("request")
        conn.execute(
            "INSERT INTO requests(id, requester, recipient, title, parent_task_id, phase, idem_key,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (request_id, fields["sender"], fields["recipient"], fields["subject"], parent_task_id,
             idem_key, ts, ts),
        )
        _record_phase(conn, request_id, "queued", ts, None)
        task = pensieve.create_task(
            conn, fields["recipient"], fields["subject"], parent_task_id=parent_task_id,
            request_id=request_id, now=ts,
        )
        conn.execute("UPDATE requests SET task_id = ? WHERE id = ?", (task["id"], request_id))
        _deliver(conn, {**fields, "task_id": task["id"], "request_id": request_id}, None, ts)
        return _opened(conn, request_id, created=True)


def _live_twin(conn: Conn, fields: dict, parent_task_id: Optional[str]) -> tuple:
    # Content dedupe only folds into a request that is still in flight; finished ones bump the generation.
    for generation in itertools.count():
        idem_key = _digest("content", generation, fields["sender"], fields["recipient"], fields["subject"],
                           fields["body"], fields["body_path"], parent_task_id)
        existing = db.fetch_one(conn, "SELECT * FROM requests WHERE idem_key = ?", (idem_key,))
        if existing is None or _in_flight(existing):
            return idem_key, existing


def _in_flight(request: dict) -> bool:
    return request["outcome"] is None and request["phase"] not in ("task_closed", "cleaned")


def _require_same_request(conn: Conn, existing: dict, fields: dict, parent_task_id: Optional[str]) -> None:
    owl = db.fetch_one(
        conn, "SELECT body, body_path, purged_at FROM owls WHERE request_id = ? AND kind = 'request'",
        (existing["id"],),
    )
    same = (existing["recipient"], existing["title"], existing["parent_task_id"]) == (
        fields["recipient"], fields["subject"], parent_task_id)
    same = same and owl is not None and owl["body_path"] == fields["body_path"]
    same = same and (owl["purged_at"] is not None or owl["body"] == fields["body"])
    if not same:
        raise ConflictError("idempotency key was already used for a different request")


def _check_parent(conn: Conn, parent_task_id: str, requester: str) -> None:
    parent = pensieve.get_task(conn, parent_task_id)
    if parent["desk"] != requester:
        raise ValidationError("parent task must belong to the requester")
    if parent["status"] == "closed":
        raise ConflictError("parent task is closed")


def advance(conn: Conn, request_id: str, to_phase: str, detail: Optional[str] = None,
            now: Optional[int] = None) -> dict:
    request_id = ids.check("request", request_id)
    to_phase = ids.check_enum(to_phase, REQUEST_PHASES, "request phase")
    detail = ids.optional_text(detail, "detail", DETAIL_LIMIT, single_line=True)
    ts = ids.stamp(now)
    with db.transaction(conn):
        request = _require_request(conn, request_id)
        if request["outcome"] in ("deferred", "declined"):
            raise ConflictError("request was deferred or declined")
        current = REQUEST_PHASES.index(request["phase"])
        target = REQUEST_PHASES.index(to_phase)
        if target == current:
            return get_request(conn, request_id)
        if target != current + 1:
            following = REQUEST_PHASES[current + 1] if current + 1 < len(REQUEST_PHASES) else "none"
            raise ConflictError(f"request is at {request['phase']}, the next phase is {following}")
        _require_phase_evidence(conn, request, to_phase)
        outcome = _closed_outcome(conn, request) if to_phase == "task_closed" else request["outcome"]
        conn.execute(
            "UPDATE requests SET phase = ?, outcome = ?, updated_at = ? WHERE id = ?",
            (to_phase, outcome, ts, request_id),
        )
        _record_phase(conn, request_id, to_phase, ts, detail)
    return get_request(conn, request_id)


def _require_phase_evidence(conn: Conn, request: dict, to_phase: str) -> None:
    if to_phase == "running":
        task = None if request["task_id"] is None else pensieve.get_task(conn, request["task_id"])
        if task is None or task["started_at"] is None:
            raise ConflictError("the request task has not started yet")
    if to_phase == "result_posted" and db.fetch_one(
        conn, "SELECT id FROM owls WHERE request_id = ? AND kind = 'result' LIMIT 1", (request["id"],)
    ) is None:
        raise ConflictError("no result owl has been posted for this request")


def _closed_outcome(conn: Conn, request: dict) -> Optional[str]:
    if request["task_id"] is None:
        return "done"
    task = pensieve.get_task(conn, request["task_id"])
    if task["status"] != "closed":
        raise ConflictError("the request task is not closed yet")
    return "done" if task["close_reason"] == "complete" else None


def _cascade_task_closed(conn: Conn, request_id: str, closed_by: str, now: Optional[int] = None) -> None:
    request_id = ids.check("request", request_id)
    closed_by = ids.check("task", closed_by)
    ts = ids.stamp(now)
    with db.transaction(conn):
        request = _require_request(conn, request_id)
        if request["outcome"] in ("deferred", "declined"):
            return
        if REQUEST_PHASES.index(request["phase"]) >= REQUEST_PHASES.index("task_closed"):
            return
        task = None if request["task_id"] is None else pensieve.get_task(conn, request["task_id"])
        if task is None or task["status"] != "closed":
            raise IntegrityError("a cascade needs the request task to be closed")
        if closed_by not in pensieve.closed_ancestors(conn, task["id"]):
            raise IntegrityError("a cascade must come from a closed ancestor of the request task")
        conn.execute(
            "UPDATE requests SET phase = 'task_closed', outcome = ?, updated_at = ? WHERE id = ?",
            (_closed_outcome(conn, request), ts, request_id),
        )
        _record_phase(conn, request_id, "task_closed", ts, f"closed with parent task {closed_by}")


def defer(conn: Conn, request_id: str, reason: str, now: Optional[int] = None) -> dict:
    return _finish(conn, request_id, "deferred", reason, now)


def decline(conn: Conn, request_id: str, reason: str, now: Optional[int] = None) -> dict:
    return _finish(conn, request_id, "declined", reason, now)


def _finish(conn: Conn, request_id: str, outcome: str, reason: str, now: Optional[int]) -> dict:
    request_id = ids.check("request", request_id)
    reason = ids.check_enum(reason, db.REQUEST_REASONS, "reason")
    ts = ids.stamp(now)
    with db.transaction(conn):
        request = _require_request(conn, request_id)
        if (request["outcome"], request["reason"]) == (outcome, reason):
            return get_request(conn, request_id)
        if request["outcome"] is not None:
            raise ConflictError("request already has an outcome")
        if request["phase"] not in DEFERRABLE_PHASES:
            raise ConflictError(f"a request at {request['phase']} can no longer be {outcome}")
        conn.execute(
            "UPDATE requests SET outcome = ?, reason = ?, updated_at = ? WHERE id = ?",
            (outcome, reason, ts, request_id),
        )
        _record_phase(conn, request_id, outcome, ts, reason)
        if request["task_id"] is not None:
            if pensieve.get_task(conn, request["task_id"])["status"] != "closed":
                pensieve.close_task(conn, request["task_id"], "superseded", now=ts)
    return get_request(conn, request_id)


# Review passes


def record_review(conn: Conn, repo: str, sha: str, task_id: str, reviewer_desk: str, verdict: str,
                  review_path: Optional[str] = None, now: Optional[int] = None) -> dict:
    repo = ids.check("repo", repo)
    sha = ids.check("sha", sha)
    task_id = ids.check("task", task_id)
    reviewer_desk = ids.check("desk", reviewer_desk, "reviewer desk")
    verdict = ids.check_enum(verdict, db.REVIEW_VERDICTS, "review verdict")
    review_path = ids.optional_path(review_path, "review path", ids.REVIEWS_ROOT)
    ts = ids.stamp(now)
    review_id = ids.new_id("review")
    with db.transaction(conn):
        task = pensieve.get_task(conn, task_id)
        author = pensieve.get_desk(conn, task["desk"])
        reviewer = pensieve.get_desk(conn, reviewer_desk)
        commit = pensieve.get_commit(conn, repo, sha)
        if commit is None or commit["task_id"] != task_id:
            raise IntegrityError("that commit is not recorded on that task")
        if verdict == "PASS":
            _check_pass(conn, task, author, reviewer)
        conn.execute(
            "INSERT INTO review_passes(id, repo, sha, task_id, author_desk, author_family,"
            " reviewer_desk, reviewer_family, verdict, review_path, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, repo, sha, task_id, author["name"], author["family"], reviewer["name"],
             reviewer["family"], verdict, review_path, ts),
        )
    return db.fetch_one(conn, "SELECT * FROM review_passes WHERE id = ?", (review_id,))


def _check_pass(conn: Conn, task: dict, author: dict, reviewer: dict) -> None:
    if reviewer["family"] not in db.PASS_FAMILIES:
        raise IntegrityError(f"a {reviewer['family']} desk cannot record a PASS")
    if author["family"] == reviewer["family"]:
        raise IntegrityError("a PASS needs a reviewer from a different family than the author")
    if reviewer["family"] != "human" and db.fetch_one(
        conn,
        "SELECT id FROM requests WHERE requester = ? AND recipient = ? AND parent_task_id = ?"
        " AND (outcome IS NULL OR outcome = 'done') LIMIT 1",
        (author["name"], reviewer["name"], task["id"]),
    ) is None:
        raise IntegrityError("a PASS needs a review request from the author task to the reviewer")


def latest_review(conn: Conn, repo: str, sha: str) -> Optional[dict]:
    return db.fetch_one(
        conn,
        "SELECT * FROM review_passes WHERE repo = ? AND sha = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (ids.check("repo", repo), ids.check("sha", sha)),
    )


def has_pass(conn: Conn, repo: str, sha: str) -> bool:
    repo = ids.check("repo", repo)
    sha = ids.check("sha", sha)
    with db.snapshot(conn):
        commit = pensieve.get_commit(conn, repo, sha)
        if commit is None:
            return False
        task = pensieve.get_task(conn, commit["task_id"])
        if task["status"] != "awaiting_close" and task["close_reason"] != "complete":
            return False
        review = latest_review(conn, repo, sha)
        if review is None or review["verdict"] != "PASS" or review["task_id"] != task["id"]:
            return False
        author = pensieve.get_desk(conn, task["desk"])
        reviewer = pensieve.get_desk(conn, review["reviewer_desk"])
        return reviewer["family"] in db.PASS_FAMILIES and reviewer["family"] != author["family"]


# Close tokens


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def mint(conn: Conn, task_id: str, minted_by: str, ttl_seconds: int = TOKEN_TTL_DEFAULT,
         now: Optional[int] = None) -> dict:
    task_id = ids.check("task", task_id)
    minted_by = ids.check_enum(minted_by, db.TOKEN_MINTERS, "minted by")
    ttl_seconds = ids.check_int(ttl_seconds, "ttl seconds", minimum=1, maximum=TOKEN_TTL_MAX)
    ts = ids.stamp(now)
    token = secrets.token_urlsafe(32)
    with db.transaction(conn):
        if pensieve.get_task(conn, task_id)["status"] not in ("active", "awaiting_close"):
            raise ConflictError("close tokens are only minted for active or awaiting_close tasks")
        conn.execute(
            "INSERT INTO close_tokens(task_id, token_hash, minted_by, minted_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, _token_hash(token), minted_by, ts, ts + ttl_seconds),
        )
    return {"token": token, "task_id": task_id, "minted_by": minted_by, "expires_at": ts + ttl_seconds}


def consume(conn: Conn, task_id: str, token: str, now: Optional[int] = None) -> None:
    task_id = ids.check("task", task_id)
    ts = ids.stamp(now)
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        raise TokenError("invalid close token")
    if not db.in_write_transaction(conn):
        raise StoreError("consume must run inside the close transaction")
    digest = _token_hash(token)
    candidates = db.fetch_all(
        conn,
        "SELECT id, token_hash, expires_at FROM close_tokens WHERE task_id = ? AND consumed_at IS NULL",
        (task_id,),
    )
    match = None
    for candidate in candidates:
        if hmac.compare_digest(candidate["token_hash"], digest):
            match = candidate
    if match is None:
        raise TokenError("invalid close token")
    if match["expires_at"] <= ts:
        raise TokenError("close token expired")
    conn.execute(
        "UPDATE close_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL", (ts, match["id"])
    )


# Retention and audit


def purge(conn: Conn, now: Optional[int] = None, body_days: int = 30, extract_days: int = 90) -> dict:
    ts = ids.stamp(now)
    body_days = ids.check_int(body_days, "body days", minimum=1, maximum=36500)
    extract_days = ids.check_int(extract_days, "extract days", minimum=1, maximum=36500)
    with db.transaction(conn):
        owls = conn.execute(
            "UPDATE owls SET body = NULL, purged_at = ?"
            " WHERE purged_at IS NULL AND acked_at IS NOT NULL AND created_at < ?",
            (ts, ts - body_days * DAY),
        ).rowcount
        extracts = conn.execute(
            "DELETE FROM extracts WHERE created_at < ?", (ts - extract_days * DAY,)
        ).rowcount
        if extracts:
            conn.execute("INSERT INTO extracts_fts(extracts_fts) VALUES ('optimize')")
    return {"owl_bodies_purged": owls, "extracts_deleted": extracts, "wal_checkpoint_busy": _truncate_wal(conn)}


def _truncate_wal(conn: Conn) -> Optional[bool]:
    # Purged bytes linger in the -wal file until a TRUNCATE checkpoint; None means an outer transaction is open.
    if conn.in_transaction:
        return None
    return bool(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])


def _findings(conn: Conn, ts: int) -> dict:
    return {
        "stale_requests": db.fetch_all(
            conn,
            "SELECT id, requester, recipient, task_id, phase, updated_at FROM requests"
            " WHERE phase <> 'cleaned' AND (outcome IS NULL OR outcome = 'done') AND updated_at <= ?"
            " ORDER BY updated_at, id",
            (ts - REQUEST_STALE_AFTER,),
        ),
        "rering_owls": [_as_meta(row) for row in db.fetch_all(
            conn,
            _SELECT_META + " WHERE delivered_at IS NOT NULL AND acked_at IS NULL"
            " AND delivered_at <= ? AND delivered_at > ? ORDER BY delivered_at, id",
            (ts - OWL_RERING_AFTER, ts - OWL_ESCALATE_AFTER),
        )],
        "escalate_owls": [_as_meta(row) for row in db.fetch_all(
            conn,
            _SELECT_META + " WHERE delivered_at IS NOT NULL AND acked_at IS NULL"
            " AND delivered_at <= ? ORDER BY delivered_at, id",
            (ts - OWL_ESCALATE_AFTER,),
        )],
        "undelivered_owls": [_as_meta(row) for row in db.fetch_all(
            conn,
            _SELECT_META + " WHERE delivered_at IS NULL AND acked_at IS NULL"
            " AND created_at <= ? ORDER BY created_at, id",
            (ts - OWL_RERING_AFTER,),
        )],
        "long_active_tasks": db.fetch_all(
            conn,
            "SELECT id, desk, started_at FROM tasks WHERE status = 'active' AND started_at <= ?"
            " ORDER BY started_at, id",
            (ts - ACTIVE_STALE_AFTER,),
        ),
        "stale_awaiting_close": db.fetch_all(
            conn,
            "SELECT id, desk, started_at FROM tasks WHERE status = 'awaiting_close' AND started_at <= ?"
            " ORDER BY started_at, id",
            (ts - AWAITING_STALE_AFTER,),
        ),
        "orphan_queued_tasks": db.fetch_all(
            conn,
            "SELECT child.id, child.desk, child.parent_task_id FROM tasks AS child"
            " JOIN tasks AS parent ON parent.id = child.parent_task_id"
            " WHERE child.status = 'queued' AND parent.status = 'closed'"
            " ORDER BY child.created_at, child.id",
        ),
        "orphan_reviews": db.fetch_all(
            conn,
            "SELECT review_passes.id, review_passes.repo, review_passes.sha, review_passes.task_id,"
            " review_passes.reviewer_desk FROM review_passes"
            " LEFT JOIN tasks ON tasks.id = review_passes.task_id WHERE tasks.id IS NULL"
            " ORDER BY review_passes.created_at, review_passes.id",
        ),
    }


def _alerts(report: dict) -> list[dict]:
    alerts = [
        {"desk": row["recipient"], "kind": "audit.stale-request", "task_id": row["task_id"],
         "summary": f"request {row['id']} has been at {row['phase']} since {row['updated_at']}",
         "dedupe_key": f"audit:request:{row['id']}:{row['updated_at']}"}
        for row in report["stale_requests"]
    ]
    alerts += [
        {"desk": row["recipient"], "kind": "audit.unacked-owl", "task_id": row["task_id"],
         "summary": f"owl {row['id']} delivered at {row['delivered_at']} is still unacked",
         "dedupe_key": f"audit:owl:{row['id']}"}
        for row in report["escalate_owls"]
    ]
    alerts += [
        {"desk": row["recipient"], "kind": "audit.undelivered-owl", "task_id": row["task_id"],
         "summary": f"owl {row['id']} sent at {row['created_at']} was never delivered",
         "dedupe_key": f"audit:owl-undelivered:{row['id']}"}
        for row in report["undelivered_owls"]
    ]
    alerts += [
        {"desk": row["desk"], "kind": "audit.long-active-task", "task_id": row["id"],
         "summary": f"task {row['id']} has been active since {row['started_at']}",
         "dedupe_key": f"audit:task-active:{row['id']}:{row['started_at']}"}
        for row in report["long_active_tasks"]
    ]
    alerts += [
        {"desk": row["desk"], "kind": "audit.stale-awaiting-close", "task_id": row["id"],
         "summary": f"task {row['id']} is still awaiting close",
         "dedupe_key": f"audit:task-awaiting:{row['id']}"}
        for row in report["stale_awaiting_close"]
    ]
    alerts += [
        {"desk": row["desk"], "kind": "audit.orphan-queued-task", "task_id": row["id"],
         "summary": f"task {row['id']} is queued under closed parent {row['parent_task_id']}",
         "dedupe_key": f"audit:task-orphan:{row['id']}"}
        for row in report["orphan_queued_tasks"]
    ]
    alerts += [
        {"desk": row["reviewer_desk"], "kind": "audit.orphan-review", "task_id": None,
         "summary": f"review {row['id']} points at a missing task",
         "dedupe_key": f"audit:review:{row['id']}"}
        for row in report["orphan_reviews"]
    ]
    return alerts


def _escalate(conn: Conn, report: dict, ts: int) -> dict:
    created = skipped = 0
    for alert in _alerts(report):
        try:
            event = pensieve.add_event(conn, verdict="headmaster", now=ts, **alert)
        except NotFoundError:
            skipped += 1
            continue
        created += 1 if event["created"] else 0
    return {"created": created, "skipped": skipped}


def audit(conn: Conn, now: Optional[int] = None, escalate: bool = False) -> dict:
    ts = ids.stamp(now)
    scope = db.transaction if escalate else db.snapshot
    with scope(conn):
        report = _findings(conn, ts)
        if escalate:
            report["escalated"] = _escalate(conn, report, ts)
    return {"now": ts, **report}
