from __future__ import annotations

import re
import sqlite3
import urllib.parse
from collections import Counter
from typing import Optional

from . import db, ids, pensieve
from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

VOLATILE_SECONDS = 7 * 86400
CANDIDATE_TOKENS = 32
CANDIDATE_LIMIT = 20
OPS_LIMIT = 1000

VOLATILE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bdraft\b", r"\bopen\b", r"\bopened\b", r"\bmerged\b", r"\bclosed\b", r"\bgreen\b", r"\bred\b",
    r"\bfailing\b", r"\bpassing\b", r"\blive\b", r"\brolled[\s-]+back\b", r"\bramp\b", r"\bramped\b",
    r"\bin[\s-]+progress\b", r"\bblocked\b", r"\bpending\b", r"\bdeployed\b", r"\breleased\b", r"\bshipped\b",
    r"\bunmerged\b", r"\b\d{1,3}\s*%", r"\bPR\s*#?\d+\b", r"(?<!\w)#\d{2,}\b", r"\bbuild\s*#?\d+\b",
))

# A lookup is an https URL or a gh or bk command made of plain words, so no shell syntax can be stored in it.
LOOKUP_COMMAND = re.compile(r"(?:gh|bk)(?: +[A-Za-z0-9._/:=@,-]+)+")
_URL_UNSAFE = re.compile(r"[\s\"'`<>\\{}|^$;()]")
_URL_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_PLACEHOLDER = re.compile(r"\[(private_key|credentials|jwt|token|secret|aws_key|email|ipv6|ipv4|hex)\]")

_CURRENT = (
    "facts.archived_at IS NULL AND facts.valid_to IS NULL"
    " AND (facts.expires_at IS NULL OR facts.expires_at > ?)"
)
_HELD = "scope = ? AND subject_key = ? AND valid_to IS NULL AND archived_at IS NULL"
_LAPSED = "tier = 'perishable' AND valid_to IS NULL AND expires_at <= ?"
_EXPIRE = "UPDATE facts SET end_reason = 'expired', valid_to = expires_at, closed_at = ? WHERE "

Conn = sqlite3.Connection


# Validation


def volatile_match(text: str) -> Optional[str]:
    for pattern in VOLATILE_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None


def _lint(fields: dict) -> None:
    term = None if fields["lookup"] is not None else volatile_match(fields["text"])
    if term is None:
        return
    if fields["tier"] == "perishable" and fields["expires_at"] <= fields["valid_from"] + VOLATILE_SECONDS:
        return
    raise ValidationError(
        f"fact text looks volatile ('{term}'). If the fact is lasting, reword it. Otherwise add a lookup that"
        " fetches the live value, or make it perishable with expires_at at most 7 days after valid_from"
    )


def _https_url(value: str) -> bool:
    if _URL_UNSAFE.search(value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        return False
    return (port_ok and parts.scheme == "https" and "@" not in parts.netloc and parts.hostname is not None
            and _URL_HOST.fullmatch(parts.hostname) is not None)


def _scrubbed_kinds(value: str) -> list[str]:
    added = Counter(_PLACEHOLDER.findall(pensieve.scrub(value))) - Counter(_PLACEHOLDER.findall(value))
    return sorted(added)


def _lookup(value: object) -> Optional[str]:
    lookup = ids.optional_text(value, "lookup", db.LOOKUP_LIMIT, single_line=True)
    if lookup is None:
        return None
    if pensieve.scrub(lookup) != lookup:
        kinds = _scrubbed_kinds(lookup)
        if kinds == ["hex"]:
            raise ValidationError("lookup holds a hex string of 32 or more characters, which could be a secret."
                                  " Use a short sha or a branch or tag name")
        found = ", ".join(kinds) or "scrubbed text"
        raise ValidationError(f"lookup looks like it holds a secret or personal data ({found}),"
                              " store a command that reads it")
    if LOOKUP_COMMAND.fullmatch(lookup) is None and not _https_url(lookup):
        raise ValidationError("lookup must be an https URL without credentials, or a gh or bk command made of"
                              " letters, digits and ._/:=@,- with no shell syntax")
    return lookup


def _scope_name(scope: object) -> str:
    return "fleet" if scope == "fleet" else ids.check("desk", scope, "fact scope")


def _optional_scope(scope: object) -> Optional[str]:
    return None if scope is None else _scope_name(scope)


def _fact_id(value: object) -> int:
    return ids.check_int(value, "fact id", minimum=1)


def _new_fact(scope: object, text: object, tier: object, source: object, expires_at: object,
              subject_key: object, valid_from: object, lookup: object, ts: int) -> dict:
    fields = {
        "scope": _scope_name(scope),
        "text": ids.clean_text(text, "fact text", pensieve.FACT_LIMIT),
        "tier": ids.check_enum(tier, db.FACT_TIERS, "fact tier"),
        "source": ids.check("label", source, "fact source"),
        "subject_key": ids.optional("subject_key", subject_key),
        "valid_from": ts if valid_from is None else ids.check_int(valid_from, "valid from"),
        "lookup": _lookup(lookup),
        "expires_at": None,
    }
    if fields["valid_from"] > ts:
        raise ValidationError("valid from cannot be in the future")
    if fields["tier"] == "perishable":
        fields["expires_at"] = ids.check_int(expires_at, "expires at", minimum=ts + 1)
    elif expires_at is not None:
        raise ValidationError("only perishable facts expire")
    _lint(fields)
    return fields


def _replacement(scope: object, subject_key: object, text: object, source: object, tier: object,
                 valid_from: object, lookup: object, expires_at: object, ts: int) -> dict:
    subject_key = ids.check("subject_key", subject_key)
    return _new_fact(scope, text, tier, source, expires_at, subject_key, valid_from, lookup, ts)


# Rows


def _fact(conn: Conn, fact_id: int) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM facts WHERE id = ?", (fact_id,))


def _require_fact(conn: Conn, fact_id: int) -> dict:
    fact = _fact(conn, fact_id)
    if fact is None:
        raise NotFoundError("fact not found")
    return fact


def _check_scope(conn: Conn, scope: str) -> None:
    if scope != "fleet":
        pensieve.get_desk(conn, scope)


def _insert(conn: Conn, fields: dict, ts: int) -> int:
    cursor = conn.execute(
        "INSERT INTO facts(scope, text, tier, expires_at, source, created_at, last_used_at, subject_key,"
        " valid_from, recorded_at, lookup) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fields["scope"], fields["text"], fields["tier"], fields["expires_at"], fields["source"], ts, ts,
         fields["subject_key"], fields["valid_from"], ts, fields["lookup"]),
    )
    return cursor.lastrowid


def _lapsed(fact: dict, ts: int) -> bool:
    return fact["tier"] == "perishable" and fact["valid_to"] is None and fact["expires_at"] <= ts


def _claim(conn: Conn, scope: str, subject_key: Optional[str], valid_from: int, ts: int,
           replace: bool = False) -> Optional[dict]:
    # A lapsed perishable still holds its key in the unique index, so close it as expired first,
    # unless a replacement starts before it lapsed. With no holder, the new start must come after the key's history.
    if subject_key is None:
        return None
    holder = db.fetch_one(conn, "SELECT * FROM facts WHERE " + _HELD, (scope, subject_key))
    if holder is not None and _lapsed(holder, ts) and not (replace and valid_from < holder["expires_at"]):
        conn.execute(_EXPIRE + "id = ?", (ts, holder["id"]))
        holder = None
    if holder is None:
        _after_history(conn, scope, subject_key, valid_from)
    return holder


def _after_history(conn: Conn, scope: str, subject_key: str, valid_from: int) -> None:
    edge = db.fetch_one(
        conn,
        "SELECT MAX(COALESCE(valid_to, valid_from)) AS edge FROM facts WHERE scope = ? AND subject_key = ?"
        " AND (end_reason IS NULL OR end_reason <> 'withdrawn')",
        (scope, subject_key),
    )["edge"]
    if edge is not None and valid_from < edge:
        raise ConflictError(f"subject key history runs to {edge}, so a new fact for it cannot start earlier")


# Writes


def add_fact(conn: Conn, scope: str, text: str, tier: str, source: str, expires_at: Optional[int] = None,
             subject_key: Optional[str] = None, valid_from: Optional[int] = None, lookup: Optional[str] = None,
             now: Optional[int] = None) -> dict:
    ts = ids.stamp(now)
    fields = _new_fact(scope, text, tier, source, expires_at, subject_key, valid_from, lookup, ts)
    with db.transaction(conn):
        _check_scope(conn, fields["scope"])
        if _claim(conn, fields["scope"], fields["subject_key"], fields["valid_from"], ts) is not None:
            raise ConflictError("that subject key already has a current fact, use supersede to replace it")
        fact_id = _insert(conn, fields, ts)
    return _fact(conn, fact_id)


def supersede(conn: Conn, scope: str, subject_key: str, text: str, source: str, tier: str = "aging",
              valid_from: Optional[int] = None, lookup: Optional[str] = None, expires_at: Optional[int] = None,
              now: Optional[int] = None) -> dict:
    ts = ids.stamp(now)
    fields = _replacement(scope, subject_key, text, source, tier, valid_from, lookup, expires_at, ts)
    with db.transaction(conn):
        _check_scope(conn, fields["scope"])
        current = _claim(conn, fields["scope"], fields["subject_key"], fields["valid_from"], ts, replace=True)
        if current is not None:
            _retire(conn, current, fields["valid_from"], ts)
        fact_id = _insert(conn, fields, ts)
        if current is not None:
            conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (fact_id, current["id"]))
    return {"fact_id": fact_id, "superseded_id": None if current is None else current["id"]}


def _retire(conn: Conn, current: dict, valid_from: int, ts: int) -> None:
    if valid_from < current["valid_from"]:
        raise ConflictError(
            f"current fact {current['id']} is valid from {current['valid_from']}, so a replacement cannot start earlier"
        )
    conn.execute(
        "UPDATE facts SET valid_to = ?, closed_at = ?, end_reason = 'superseded' WHERE id = ?",
        (valid_from, ts, current["id"]),
    )


def withdraw(conn: Conn, fact_id: int, desk: Optional[str] = None, now: Optional[int] = None) -> dict:
    fact_id = _fact_id(fact_id)
    desk = ids.optional("desk", desk)
    ts = ids.stamp(now)
    with db.transaction(conn):
        fact = _require_fact(conn, fact_id)
        if fact["closed_at"] is not None:
            raise ConflictError(f"fact is already closed as {fact['end_reason']}")
        previous = _restorable(conn, fact)
        event_desk = None if previous is None else _event_desk(previous, desk)
        conn.execute(
            "UPDATE facts SET end_reason = 'withdrawn', valid_to = valid_from, closed_at = ? WHERE id = ?",
            (ts, fact_id),
        )
        restored = None if previous is None else _restore(conn, previous, fact, event_desk, ts)
    return {
        "fact": _fact(conn, fact_id),
        "reopened_id": None if previous is None else previous["id"],
        "restored_id": None if restored is None else restored["id"],
        "reopened_expired": restored is not None and restored["end_reason"] == "expired",
    }


def _restorable(conn: Conn, fact: dict) -> Optional[dict]:
    # Restoring must not overlap any other row on the key: an open one (live, lapsed or archived),
    # or a closed one that ends after the predecessor was replaced.
    previous = db.fetch_one(
        conn, "SELECT * FROM facts WHERE superseded_by = ? AND archived_at IS NULL ORDER BY id DESC LIMIT 1",
        (fact["id"],),
    )
    if previous is None or db.fetch_one(
        conn,
        "SELECT id FROM facts WHERE scope = ? AND subject_key = ? AND id NOT IN (?, ?)"
        " AND (end_reason IS NULL OR end_reason <> 'withdrawn') AND (valid_to IS NULL OR valid_to > ?)",
        (previous["scope"], previous["subject_key"], previous["id"], fact["id"], previous["valid_to"]),
    ) is not None:
        return None
    return previous


def _restore(conn: Conn, previous: dict, withdrawn: dict, desk: str, ts: int) -> dict:
    # The predecessor stays closed so belief history is never rewritten. A new row carries it on from where
    # the withdrawn fact began, and one that lapsed in the meantime comes back closed as expired at its expires_at.
    expired = previous["tier"] == "perishable" and previous["expires_at"] <= ts
    cursor = conn.execute(
        "INSERT INTO facts(scope, text, tier, expires_at, source, created_at, last_used_at, subject_key, valid_from,"
        " recorded_at, lookup, restores, valid_to, closed_at, end_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (previous["scope"], previous["text"], previous["tier"], previous["expires_at"], previous["source"], ts, ts,
         previous["subject_key"], withdrawn["valid_from"], ts, previous["lookup"], previous["id"],
         previous["expires_at"] if expired else None, ts if expired else None, "expired" if expired else None),
    )
    restored = _fact(conn, cursor.lastrowid)
    outcome = "restored as expired fact" if expired else "restored as fact"
    pensieve.add_event(
        conn, desk, "fact_reopened", "routine",
        f"fact {previous['id']} {outcome} {restored['id']} because fact {withdrawn['id']} was withdrawn",
        dedupe_key=f"fact-reopened:{previous['id']}:{withdrawn['id']}", now=ts,
    )
    return restored


def _event_desk(fact: dict, desk: Optional[str]) -> str:
    if desk is not None:
        return desk
    if fact["scope"] != "fleet":
        return fact["scope"]
    raise ValidationError("restoring a fleet fact logs an event, so name the desk to log it under")


def expire(conn: Conn, now: Optional[int] = None) -> list[int]:
    ts = ids.stamp(now)
    with db.transaction(conn):
        lapsed = db.fetch_all(conn, "SELECT id FROM facts WHERE " + _LAPSED + " ORDER BY id", (ts,))
        conn.execute(_EXPIRE + _LAPSED, (ts, ts))
    return [row["id"] for row in lapsed]


def set_key(conn: Conn, fact_id: int, subject_key: str, now: Optional[int] = None) -> dict:
    fact_id = _fact_id(fact_id)
    subject_key = ids.check("subject_key", subject_key)
    ts = ids.stamp(now)
    with db.transaction(conn):
        fact = _require_fact(conn, fact_id)
        if fact["closed_at"] is not None:
            raise ConflictError("a closed fact keeps its subject key")
        if fact["subject_key"] == subject_key:
            return fact
        holder = _claim(conn, fact["scope"], subject_key, fact["valid_from"], ts)
        if holder is not None:
            raise ConflictError(f"subject key already has current fact {holder['id']}")
        conn.execute("UPDATE facts SET subject_key = ? WHERE id = ?", (subject_key, fact_id))
    return _fact(conn, fact_id)


# Reads


def current_facts(conn: Conn, scope: Optional[str] = None, now: Optional[int] = None) -> list[dict]:
    scope = _optional_scope(scope)
    ts = ids.stamp(now)
    return db.fetch_all(
        conn, "SELECT * FROM facts WHERE (? IS NULL OR scope = ?) AND " + _CURRENT + " ORDER BY id",
        (scope, scope, ts),
    )


def find_facts(conn: Conn, query: str, scope: Optional[str] = None, include_history: bool = False,
               limit: int = 10, now: Optional[int] = None) -> list[dict]:
    match = pensieve.fts_query(query)
    scope = _optional_scope(scope)
    limit = ids.check_int(limit, "limit", minimum=1, maximum=100)
    ts = ids.stamp(now)
    return db.fetch_all(
        conn,
        "SELECT facts.*, snippet(facts_fts, 0, '**', '**', '...', 12) AS snippet, bm25(facts_fts) AS score"
        " FROM facts_fts JOIN facts ON facts.id = facts_fts.rowid"
        " WHERE facts_fts MATCH ? AND (? IS NULL OR facts.scope = ?) AND (? OR (" + _CURRENT + "))"
        " ORDER BY score, facts.id LIMIT ?",
        (match, scope, scope, 1 if include_history else 0, ts, limit),
    )


def as_of_world(conn: Conn, t: int, scope: Optional[str] = None) -> list[dict]:
    t = ids.check_int(t, "time")
    scope = _optional_scope(scope)
    return db.fetch_all(
        conn,
        "SELECT * FROM facts WHERE (? IS NULL OR scope = ?) AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        " AND (expires_at IS NULL OR expires_at > ?) AND (end_reason IS NULL OR end_reason <> 'withdrawn') ORDER BY id",
        (scope, scope, t, t, t),
    )


def as_of_belief(conn: Conn, t: int, scope: Optional[str] = None) -> list[dict]:
    t = ids.check_int(t, "time")
    scope = _optional_scope(scope)
    return db.fetch_all(
        conn,
        "SELECT * FROM facts WHERE (? IS NULL OR scope = ?) AND recorded_at <= ? AND (closed_at IS NULL OR closed_at > ?)"
        " AND (archived_at IS NULL OR archived_at > ?) ORDER BY id",
        (scope, scope, t, t, t),
    )


def history(conn: Conn, scope: str, subject_key: str) -> list[dict]:
    scope = _scope_name(scope)
    subject_key = ids.check("subject_key", subject_key)
    return db.fetch_all(
        conn,
        "SELECT * FROM facts WHERE scope = ? AND subject_key = ? ORDER BY valid_from, recorded_at, id",
        (scope, subject_key),
    )


def contradiction_candidates(conn: Conn, since: int, limit_per_fact: int = 3,
                             now: Optional[int] = None) -> list[dict]:
    since = ids.check_int(since, "since")
    limit_per_fact = ids.check_int(limit_per_fact, "limit per fact", minimum=1, maximum=CANDIDATE_LIMIT)
    ts = ids.stamp(now)
    pairs = []
    with db.snapshot(conn):
        recent = db.fetch_all(
            conn, "SELECT * FROM facts WHERE facts.recorded_at >= ? AND " + _CURRENT + " ORDER BY id", (since, ts)
        )
        for fact in recent:
            pairs += _candidates(conn, fact, limit_per_fact, ts)
    return pairs


def _candidates(conn: Conn, fact: dict, limit: int, ts: int) -> list[dict]:
    phrases = pensieve.fts_phrases(fact["text"], CANDIDATE_TOKENS)
    if not phrases:
        return []
    # facts_one_current leaves no other current row on this fact's own key, so no key filter is needed.
    rows = db.fetch_all(
        conn,
        "SELECT facts.id, facts.text, bm25(facts_fts) AS score FROM facts_fts JOIN facts ON facts.id = facts_fts.rowid"
        " WHERE facts_fts MATCH ? AND facts.scope = ? AND facts.id <> ? AND " + _CURRENT +
        " ORDER BY score, facts.id LIMIT ?",
        (" OR ".join(phrases), fact["scope"], fact["id"], ts, limit),
    )
    return [
        {"scope": fact["scope"], "fact_id": fact["id"], "fact_text": fact["text"], "candidate_id": row["id"],
         "candidate_text": row["text"], "score": row["score"]}
        for row in rows
    ]


# Approved patches

_OP_FIELDS = {
    "supersede": ({"scope", "subject_key", "text", "source"}, {"tier", "valid_from", "lookup", "expires_at"}),
    "withdraw": ({"fact_id"}, {"desk"}),
    "set_key": ({"fact_id", "subject_key"}, set()),
    "archive": ({"fact_id"}, set()),
}

_OP_CHECKS = {
    "supersede": lambda op, ts: _replacement(
        op["scope"], op["subject_key"], op["text"], op["source"], op.get("tier", "aging"), op.get("valid_from"),
        op.get("lookup"), op.get("expires_at"), ts),
    "withdraw": lambda op, ts: (_fact_id(op["fact_id"]), ids.optional("desk", op.get("desk"))),
    "set_key": lambda op, ts: (_fact_id(op["fact_id"]), ids.check("subject_key", op["subject_key"])),
    "archive": lambda op, ts: _fact_id(op["fact_id"]),
}

_OP_RUNS = {
    "supersede": lambda conn, op, ts: supersede(
        conn, op["scope"], op["subject_key"], op["text"], op["source"], op.get("tier", "aging"),
        op.get("valid_from"), op.get("lookup"), op.get("expires_at"), now=ts),
    "withdraw": lambda conn, op, ts: withdraw(conn, op["fact_id"], op.get("desk"), now=ts),
    "set_key": lambda conn, op, ts: set_key(conn, op["fact_id"], op["subject_key"], now=ts),
    "archive": lambda conn, op, ts: pensieve.archive(conn, [op["fact_id"]], now=ts),
}


def _check_op(op: object, ts: int) -> dict:
    if not isinstance(op, dict) or not all(isinstance(key, str) for key in op):
        raise ValidationError("an op must be an object")
    if op.get("op") not in _OP_FIELDS:
        raise ValidationError("unknown op")
    required, optional = _OP_FIELDS[op["op"]]
    given = set(op) - {"op"}
    if required - given:
        raise ValidationError("missing " + ", ".join(sorted(required - given)))
    if given - required - optional:
        raise ValidationError("unknown field " + ", ".join(sorted(given - required - optional))[:100])
    _OP_CHECKS[op["op"]](op, ts)
    return dict(op)


def _numbered(index: int, run, *args):
    try:
        return run(*args)
    except sqlite3.IntegrityError as exc:
        raise IntegrityError(f"op {index}: {str(exc)[:200]}") from exc
    except StoreError as exc:
        raise type(exc)(f"op {index}: {exc}") from exc


def apply_ops(conn: Conn, ops: list, now: Optional[int] = None) -> list[dict]:
    ts = ids.stamp(now)
    if not isinstance(ops, list) or not 1 <= len(ops) <= OPS_LIMIT:
        raise ValidationError(f"ops must be a list of 1 to {OPS_LIMIT} operations")
    checked = [_numbered(index, _check_op, op, ts) for index, op in enumerate(ops)]
    with db.transaction(conn):
        return [
            {"op": op["op"], "result": _numbered(index, _OP_RUNS[op["op"]], conn, op, ts)}
            for index, op in enumerate(checked)
        ]
