"""Read-only queries for the live desk feed (fleet feed).

Every function only reads, so each works on a connection from db.connect_readonly. A feed
remembers the highest row it has shown in each table and asks for the rows after it.
A desk of None means every desk.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import db, ids

Conn = sqlite3.Connection
FEED_LIMIT = 200


def _desk(desk: Optional[str]) -> Optional[str]:
    return None if desk is None else ids.check("desk", desk)


def marks(conn: Conn) -> dict:
    """The highest row in each table a feed follows, so a new feed starts at now."""
    row = db.fetch_one(
        conn,
        "SELECT (SELECT COALESCE(MAX(rowid), 0) FROM owls) AS owls,"
        " (SELECT COALESCE(MAX(id), 0) FROM events) AS events,"
        " (SELECT COALESCE(MAX(id), 0) FROM metrics) AS metrics",
    )
    return dict(row)


def owls_after(conn: Conn, after: int, desk: Optional[str] = None, limit: int = FEED_LIMIT) -> list[dict]:
    """Owls to or from a desk, oldest first. Never the body: kind and subject only."""
    after = ids.check_int(after, "after")
    limit = ids.check_int(limit, "limit", minimum=1, maximum=1000)
    desk = _desk(desk)
    return db.fetch_all(
        conn,
        "SELECT rowid AS seq, id, sender, recipient, kind, subject, created_at FROM owls"
        " WHERE rowid > ? AND (? IS NULL OR sender = ? OR recipient = ?) ORDER BY rowid LIMIT ?",
        (after, desk, desk, desk, limit),
    )


def headmaster_events_after(conn: Conn, after: int, desk: Optional[str] = None,
                            limit: int = FEED_LIMIT) -> list[dict]:
    after = ids.check_int(after, "after")
    limit = ids.check_int(limit, "limit", minimum=1, maximum=1000)
    desk = _desk(desk)
    return db.fetch_all(
        conn,
        "SELECT id, ts, desk, kind, summary FROM events WHERE id > ? AND verdict = 'headmaster'"
        " AND (? IS NULL OR desk = ?) ORDER BY id LIMIT ?",
        (after, desk, desk, limit),
    )


def metrics_after(conn: Conn, after: int, desk: Optional[str] = None, limit: int = FEED_LIMIT) -> list[dict]:
    """Finished runs. Every column, so fields a later migration adds reach the feed too."""
    after = ids.check_int(after, "after")
    limit = ids.check_int(limit, "limit", minimum=1, maximum=1000)
    desk = _desk(desk)
    return db.fetch_all(
        conn,
        "SELECT * FROM metrics WHERE id > ? AND (? IS NULL OR desk = ?) ORDER BY id LIMIT ?",
        (after, desk, desk, limit),
    )


def run_recorded(conn: Conn, run_id: str) -> bool:
    """True once run_desk has written the metrics row that ends this run."""
    run_id = ids.check("label", run_id, "run id")
    return db.fetch_one(conn, "SELECT 1 AS found FROM metrics WHERE run_id = ? LIMIT 1", (run_id,)) is not None


def open_runs(conn: Conn, desk: str) -> set:
    """The run ids of the desk's launches whose usage is not recorded yet: runs still going, or ended with no one left
    to record them."""
    desk = ids.check("desk", desk)
    rows = db.fetch_all(conn, "SELECT run_id FROM run_launches WHERE desk = ? AND metric_id IS NULL", (desk,))
    return {row["run_id"] for row in rows}
