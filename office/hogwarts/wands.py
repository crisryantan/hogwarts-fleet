"""Ollivander's ledger: how Ryan files model names, what each family offered, and each desk's model.

A desk's family never changes here. A Claude desk only ever holds a Claude alias or a full Claude id,
and a Codex desk only a slug from the last Codex catalog. Every switch is a model_changes row, and the
first two runs after a switch are a trial: if both fail, the desk goes back to its previous model, pinned.
A trial Ryan's own switch started, or one running when Ryan pins the desk, never reverts: his choice stands.
A pending pick is approved only while the latest catalog still offers it to the desk's tier, and a trial
never reverts onto a model Ryan filed as ignore or one the latest catalog no longer lists, hides or retires
soon, since a revert pins the desk there.

An organisation may forbid some models. The calls that file, pin, approve or apply a model take the
office's blocked prefixes (the fleet's BLOCKED_MODEL_PREFIXES) and refuse a name one of them matches,
and a trial that fails never reverts onto a blocked model. A Claude alias is also judged by every full
id a run on it reported (model_resolutions keeps each one, across switches and desks, and an alias with a
label such as opus[1m] shares the bare alias's history), so an alias that ever ran as a blocked id is
refused for a pin, an approval, a switch, a revert or a launch while that id's prefix stays blocked. A
later sighting on an allowed id never lifts it: no run on a refused alias starts, so such a sighting can
only come from a run that launched before the blocked one was known. While anything is blocked, a trial
never reverts onto no model at all, since for a Codex desk that is the CLI default no one can check.
"""
from __future__ import annotations

import errno
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import db, ids, pensieve
from .errors import ConflictError, NotFoundError, ValidationError

Conn = sqlite3.Connection

# Aliases and slugs, and full Claude ids. Anything else never reaches a command line.
MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.\-]{1,62}")
CLAUDE_ID = re.compile(r"claude-[a-z0-9-]{1,60}")
CATALOG_LIMIT = 500
REVERT_AFTER_FAILURES = 2
# The file in the office state folder that stops run_desk launching headless desks.
STOP_FILE = "ollivander-stop"
# Present while a CLI update runs. run_desk honours it like the stop file, and a crash leaves it for Ryan.
UPDATING_FILE = "ollivander-updating"
# Switches Ryan made. Two failed runs after one of these are reported, never reverted over his choice.
RYAN_REASONS = ("pin", "approved")
# A pending pick whose model retires within this many seconds is no longer approved (the fleet's own window).
RETIRING_SOON_SECONDS = 30 * 86400


def check_name(value: object, field: str = "model name") -> str:
    if not isinstance(value, str) or (MODEL_NAME.fullmatch(value) is None and CLAUDE_ID.fullmatch(value) is None):
        raise ValidationError(f"invalid {field}")
    return value


def _optional_enum(value: object, allowed: tuple, field: str) -> Optional[str]:
    return None if value is None else ids.check_enum(value, allowed, field)


def check_blocklist(blocked: Iterable[str]) -> tuple:
    """The blocked prefixes, each a short lowercase name, or ValidationError."""
    if isinstance(blocked, str) or not isinstance(blocked, (list, tuple)):
        raise ValidationError("blocked model prefixes must be a list")
    for prefix in blocked:
        if not isinstance(prefix, str) or not prefix or prefix != prefix.strip().lower() or len(prefix) > 64:
            raise ValidationError("a blocked model prefix must be a short lowercase name")
    return tuple(blocked)


def blocked_by(model: str, blocked: Iterable[str]) -> Optional[str]:
    """The blocked prefix that matches this alias, full id or slug, or None. Any model string is matched
    lowercased, so a label such as opus[1m] is checked too; only a malformed blocklist raises. It only
    compares, so it never needs the stricter check_name that guards what the store keeps."""
    prefixes = check_blocklist(blocked)
    if not isinstance(model, str):
        raise ValidationError("invalid model name")
    lowered = model.lower()
    return next((prefix for prefix in prefixes if lowered.startswith(prefix)), None)


def _check_allowed(name: str, blocked: Iterable[str], conn: Optional[Conn] = None) -> str:
    """name, unless a blocked prefix matches it or, with conn, any full id it ever ran as."""
    if blocked_by(name, blocked) is not None:
        raise ConflictError(f"{name} is a blocked model here, so no desk may use it")
    full = None if conn is None else blocked_resolution(conn, name, blocked)
    if full is not None:
        raise ConflictError(f"{name} once ran as {full}, a blocked model here, so no desk may use it")
    return name


# What each Claude alias was seen to run as


def base_alias(alias: str) -> str:
    """The alias without a trailing label, so opus[1m] and opus share one resolution history."""
    return alias.split("[", 1)[0] if isinstance(alias, str) and alias.endswith("]") else alias


def record_resolution(conn: Conn, alias: str, full_id: str, now: Optional[int] = None) -> dict:
    """A run on this alias reported this full Claude id. Every pairing is kept with its first and last
    sighting, and seq orders the sightings, so the latest is known whatever switches came between."""
    alias = base_alias(ids.check("label", alias, "model alias"))
    if CLAUDE_ID.fullmatch(alias) is not None:
        raise ValidationError("a full Claude id is not an alias")
    if not isinstance(full_id, str) or CLAUDE_ID.fullmatch(full_id) is None:
        raise ValidationError("invalid full Claude id")
    ts = ids.stamp(now)
    with db.transaction(conn):
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM model_resolutions").fetchone()[0]
        conn.execute(
            "INSERT INTO model_resolutions(alias, full_id, first_seen, last_seen, seq) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(alias, full_id) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen),"
            " seq = excluded.seq",
            (alias, full_id, ts, ts, seq),
        )
    return db.fetch_one(conn, "SELECT * FROM model_resolutions WHERE alias = ? AND full_id = ?", (alias, full_id))


def resolutions(conn: Conn, alias: str) -> list:
    """Every full id this alias was seen to run as, latest sighting first."""
    return db.fetch_all(conn, "SELECT * FROM model_resolutions WHERE alias = ? ORDER BY seq DESC",
                        (base_alias(alias),))


def resolved_id(conn: Conn, alias: str) -> Optional[str]:
    """The full id this alias last ran as, on any desk, or None when no run has said."""
    if not isinstance(alias, str):
        return None
    row = db.fetch_one(conn, "SELECT full_id FROM model_resolutions WHERE alias = ? ORDER BY seq DESC LIMIT 1",
                       (base_alias(alias),))
    return None if row is None else row["full_id"]


def blocked_resolutions(conn: Conn, blocked: Iterable[str]) -> dict:
    """{alias: full id} for every alias that ever ran as a full id a blocked prefix matches (its latest such)."""
    blocked = check_blocklist(blocked)
    if not blocked:
        return {}
    found = {}
    for row in db.fetch_all(conn, "SELECT alias, full_id FROM model_resolutions ORDER BY seq"):
        if blocked_by(row["full_id"], blocked) is not None:
            found[row["alias"]] = row["full_id"]
    return dict(sorted(found.items()))


def blocked_resolution(conn: Conn, model: Optional[str], blocked: Iterable[str]) -> Optional[str]:
    """The latest blocked full id this alias (or its bare form) ever ran as, else None."""
    blocked = check_blocklist(blocked)
    if not blocked or not isinstance(model, str):
        return None
    for row in resolutions(conn, model):
        if blocked_by(row["full_id"], blocked) is not None:
            return row["full_id"]
    return None


# Ryan's filing of model names


def classify(conn: Conn, name: str, line: str, now: Optional[int] = None, blocked: Iterable[str] = ()) -> dict:
    """File a model name under a line. The latest filing of a name wins over keywords and over earlier filings.
    A blocked name is never filed, not even as ignore."""
    name = check_name(name)
    line = ids.check_enum(line, db.MODEL_LINES, "model line")
    _check_allowed(name, blocked)
    ts = ids.stamp(now)
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO model_lines(name, line, classified_at) VALUES (?, ?, ?)", (name, line, ts)
        )
        line_id = cursor.lastrowid
    return db.fetch_one(conn, "SELECT * FROM model_lines WHERE id = ?", (line_id,))


def ryan_lines(conn: Conn) -> dict:
    """{name: {line, classified_at, id}} for the latest filing of each name."""
    rows = db.fetch_all(
        conn,
        "SELECT id, name, line, classified_at FROM model_lines"
        " WHERE id IN (SELECT MAX(id) FROM model_lines GROUP BY name) ORDER BY id",
    )
    return {row["name"]: row for row in rows}


# What each family offered at the last look


def _catalog_entry(item: object) -> tuple:
    """(name, visible, line, retires_at) from a bare name, or from {name, visible, line, retires_at}. A bare
    name is listed, filed under no line and never retires, so an approval never finds it in a tier."""
    if isinstance(item, str):
        return check_name(item), 1, None, None
    if not isinstance(item, dict) or set(item) != {"name", "visible", "line", "retires_at"}:
        raise ValidationError("a catalog entry is a name or {name, visible, line, retires_at}")
    if not isinstance(item["visible"], bool):
        raise ValidationError("a catalog entry's visible must be true or false")
    retires_at = item["retires_at"]
    if retires_at is not None and (type(retires_at) is not int or retires_at < 0):
        raise ValidationError("a catalog entry's retires_at must be a whole timestamp or null")
    line = _optional_enum(item["line"], db.MODEL_NEEDS, "model line")
    return check_name(item["name"]), int(item["visible"]), line, retires_at


def record_catalog(conn: Conn, family: str, names: Iterable, now: Optional[int] = None) -> list:
    """Keep one look at a family's catalog. Each entry is a name, or a dict that also says whether the
    catalog listed it, the line it was filed under at this look, and when it retires (0 for an upgrade
    with no clear date)."""
    family = ids.check_enum(family, db.MODEL_FAMILIES, "model family")
    if isinstance(names, str) or not isinstance(names, (list, tuple, set, frozenset)):
        raise ValidationError("catalog names must be a list")
    entries = {}
    for item in names:
        entry = _catalog_entry(item)
        entries[entry[0]] = entry
    if not 1 <= len(entries) <= CATALOG_LIMIT:
        raise ValidationError(f"a catalog holds between 1 and {CATALOG_LIMIT} names")
    ts = ids.stamp(now)
    with db.transaction(conn):
        for name, visible, line, retires_at in sorted(entries.values()):
            conn.execute(
                "INSERT INTO model_catalog(family, name, seen_at, visible, line, retires_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(family, name) DO UPDATE SET seen_at = excluded.seen_at, visible = excluded.visible,"
                " line = excluded.line, retires_at = excluded.retires_at",
                (family, name, ts, visible, line, retires_at),
            )
    return sorted(entries)


def last_catalog(conn: Conn, family: str) -> list:
    family = ids.check_enum(family, db.MODEL_FAMILIES, "model family")
    rows = db.fetch_all(
        conn,
        "SELECT name FROM model_catalog WHERE family = ?"
        " AND seen_at = (SELECT MAX(seen_at) FROM model_catalog WHERE family = ?) ORDER BY name",
        (family, family),
    )
    return [row["name"] for row in rows]


def catalog_entry(conn: Conn, family: str, name: str) -> Optional[dict]:
    """The name's row in the family's latest catalog look, or None when that look did not offer it."""
    family = ids.check_enum(family, db.MODEL_FAMILIES, "model family")
    name = check_name(name)
    return db.fetch_one(
        conn,
        "SELECT * FROM model_catalog WHERE family = ? AND name = ?"
        " AND seen_at = (SELECT MAX(seen_at) FROM model_catalog WHERE family = ?)",
        (family, name, family),
    )


# Each desk's model


def get_desk_model(conn: Conn, desk: str) -> Optional[dict]:
    return db.fetch_one(conn, "SELECT * FROM desk_models WHERE desk = ?", (ids.check("desk", desk),))


def list_desk_models(conn: Conn) -> list:
    """Every Claude and Codex desk: its role need, effort, model, pin and pending pick. A null model means
    the registry model for a Claude desk and the CLI default for a Codex desk."""
    rows = db.fetch_all(
        conn,
        """SELECT desks.name AS desk, desks.family AS family, desk_models.need AS need,
                  desk_models.effort AS effort, COALESCE(desk_models.model, desks.model) AS model,
                  COALESCE(desk_models.pinned, 0) AS pinned, desk_models.pending_model AS pending_model,
                  desk_models.pending_effort AS pending_effort
           FROM desks LEFT JOIN desk_models ON desk_models.desk = desks.name
           WHERE desks.family IN ('claude', 'codex') ORDER BY desks.name""",
    )
    return [{
        "desk": row["desk"], "family": row["family"], "need": row["need"], "effort": row["effort"],
        "model": row["model"], "pinned": bool(row["pinned"]),
        "pending_pick": None if row["pending_model"] is None
        else {"model": row["pending_model"], "effort": row["pending_effort"]},
    } for row in rows]


def _model_desk(conn: Conn, desk: str) -> str:
    family = pensieve.get_desk(conn, desk)["family"]
    if family not in db.MODEL_FAMILIES:
        raise ConflictError("only Claude and Codex desks have a model")
    return family


def _ensure(conn: Conn, desk: str, ts: int) -> dict:
    conn.execute(
        "INSERT INTO desk_models(desk, updated_at) VALUES (?, ?) ON CONFLICT(desk) DO NOTHING", (desk, ts)
    )
    return db.fetch_one(conn, "SELECT * FROM desk_models WHERE desk = ?", (desk,))


def _switch(conn: Conn, current: dict, model: Optional[str], effort: Optional[str], line: Optional[str],
            reason: str, pinned: int, ts: int) -> int:
    """Move a desk to a model. The old one becomes the revert target, and a trial starts unless this is a revert."""
    trial = None if reason == "revert" else 0
    trial_end = "reverted" if reason == "revert" else None
    cursor = conn.execute(
        "INSERT INTO model_changes(desk, ts, from_model, to_model, effort, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (current["desk"], ts, current["model"], model, effort, reason),
    )
    # SQLite reads every right-hand side from the old row, so previous_* take the old values.
    conn.execute(
        "UPDATE desk_models SET previous_model = model, previous_effort = effort, previous_line = line,"
        " model = ?, effort = ?, line = ?, pinned = ?, pending_model = NULL, pending_effort = NULL,"
        " pending_line = NULL, trial_failures = ?, trial_end = ?, changed_at = ?, updated_at = ? WHERE desk = ?",
        (model, effort, line, pinned, trial, trial_end, ts, ts, current["desk"]),
    )
    return cursor.lastrowid


def set_need(conn: Conn, desk: str, need: str, now: Optional[int] = None) -> dict:
    desk = ids.check("desk", desk)
    need = ids.check_enum(need, db.MODEL_NEEDS, "role need")
    ts = ids.stamp(now)
    with db.transaction(conn):
        _model_desk(conn, desk)
        _ensure(conn, desk, ts)
        conn.execute("UPDATE desk_models SET need = ?, updated_at = ? WHERE desk = ?", (need, ts, desk))
    return get_desk_model(conn, desk)


def record_agent_file(conn: Conn, desk: str, need: str, model: Optional[str], effort: Optional[str],
                      now: Optional[int] = None) -> dict:
    """A desk whose agent file sets its model: record the role and what the file says. Never a switch."""
    desk = ids.check("desk", desk)
    need = ids.check_enum(need, db.MODEL_NEEDS, "role need")
    model = None if model is None else check_name(model)
    effort = _optional_enum(effort, db.MODEL_EFFORTS, "effort")
    ts = ids.stamp(now)
    with db.transaction(conn):
        _model_desk(conn, desk)
        _ensure(conn, desk, ts)
        conn.execute("UPDATE desk_models SET need = ?, model = ?, effort = ?, updated_at = ? WHERE desk = ?",
                     (need, model, effort, ts, desk))
    return get_desk_model(conn, desk)


def apply_model(conn: Conn, desk: str, model: str, effort: Optional[str], line: Optional[str], reason: str,
                now: Optional[int] = None, blocked: Iterable[str] = ()) -> dict:
    """Ollivander's own switch, for the role's pick. Refused on a pinned desk, or for a blocked model."""
    desk = ids.check("desk", desk)
    model = _check_allowed(check_name(model), blocked)
    effort = _optional_enum(effort, db.MODEL_EFFORTS, "effort")
    line = _optional_enum(line, db.MODEL_NEEDS, "model line")
    reason = ids.check_enum(reason, ("initial", "role"), "change reason")
    ts = ids.stamp(now)
    with db.transaction(conn):
        _model_desk(conn, desk)
        _check_allowed(model, blocked, conn)
        current = _ensure(conn, desk, ts)
        if current["pinned"]:
            raise ConflictError("this desk is pinned, so only Ryan changes its model")
        change_id = _switch(conn, current, model, effort, line, reason, 0, ts)
    return {**get_desk_model(conn, desk), "change_id": change_id}


def set_pending(conn: Conn, desk: str, model: str, effort: Optional[str], line: str,
                now: Optional[int] = None, blocked: Iterable[str] = ()) -> bool:
    """Hold a pick for Ryan's approval. True when the pending pick is new or different."""
    desk = ids.check("desk", desk)
    model = _check_allowed(check_name(model), blocked)
    effort = _optional_enum(effort, db.MODEL_EFFORTS, "effort")
    line = ids.check_enum(line, db.MODEL_NEEDS, "model line")
    ts = ids.stamp(now)
    with db.transaction(conn):
        _model_desk(conn, desk)
        _check_allowed(model, blocked, conn)
        current = _ensure(conn, desk, ts)
        if (current["pending_model"], current["pending_effort"], current["pending_line"]) == (model, effort, line):
            return False
        conn.execute(
            "UPDATE desk_models SET pending_model = ?, pending_effort = ?, pending_line = ?, updated_at = ?"
            " WHERE desk = ?",
            (model, effort, line, ts, desk),
        )
    return True


def clear_pending(conn: Conn, desk: str, now: Optional[int] = None) -> dict:
    """Drop the desk's pending pick. cleared is the pick that was dropped, or None when none waited."""
    desk = ids.check("desk", desk)
    ts = ids.stamp(now)
    with db.transaction(conn):
        row = get_desk_model(conn, desk)
        cleared = None if row is None or row["pending_model"] is None else {
            "model": row["pending_model"], "effort": row["pending_effort"], "line": row["pending_line"]}
        conn.execute(
            "UPDATE desk_models SET pending_model = NULL, pending_effort = NULL, pending_line = NULL,"
            " updated_at = ? WHERE desk = ? AND pending_model IS NOT NULL",
            (ts, desk),
        )
    return {**(get_desk_model(conn, desk) or {"desk": desk}), "cleared": cleared}


def _not_listed(entry: Optional[dict], family: str) -> Optional[str]:
    """Why the latest look at the family's catalog does not list this entry, or None when it does."""
    if entry is None:
        return f"the latest {family} catalog no longer offers it"
    if not entry["visible"]:
        return f"the latest {family} catalog hides it"
    return None


def _retiring_soon(entry: dict, ts: int, retiring_within: int) -> Optional[str]:
    if entry["retires_at"] is not None and entry["retires_at"] - ts <= retiring_within:
        return f"it retires within {retiring_within // 86400} days"
    return None


def _unavailable(conn: Conn, family: str, model: str, ts: int, retiring_within: int) -> Optional[str]:
    """Why no automatic move may land on this model now, or None: Ryan filed it as ignore, or the latest
    catalog no longer lists it, hides it or retires it soon. A full Claude id is in no catalog, so only the
    blocklist judges it."""
    if family == "claude" and CLAUDE_ID.fullmatch(model) is not None:
        return None
    filed = ryan_lines(conn).get(model)
    if filed is not None and filed["line"] == "ignore":
        return "it is filed as ignore"
    entry = catalog_entry(conn, family, model)
    return _not_listed(entry, family) or _retiring_soon(entry, ts, retiring_within)


def _pick_still_qualifies(conn: Conn, family: str, current: dict, ts: int, retiring_within: int) -> None:
    """Refuse a pending pick the latest stored catalog no longer offers to the desk's tier: gone from its
    family's catalog, hidden, filed under another line (Ryan's latest filing wins), or retiring soon."""
    model, line = current["pending_model"], current["pending_line"]
    entry = catalog_entry(conn, family, model)
    why = _not_listed(entry, family)
    if why is None:
        filed = ryan_lines(conn).get(model)
        filed_line = entry["line"] if filed is None else filed["line"] if filed["line"] in db.MODEL_NEEDS else None
        tier = current["need"] or line
        if tier != line:
            why = f"the desk's tier is now {tier}, not {line}"
        elif filed_line != line:
            why = f"it is now filed under {filed_line or 'no line'}, not {line}"
        else:
            why = _retiring_soon(entry, ts, retiring_within)
    if why is not None:
        raise ConflictError(f"the pending pick {model} no longer qualifies: {why}. Ollivander's next pass"
                            " picks again")


def approve(conn: Conn, desk: str, now: Optional[int] = None, blocked: Iterable[str] = (),
            retiring_within: int = RETIRING_SOON_SECONDS) -> dict:
    """Ryan approves the pending pick: the desk switches to it now, unless it was blocked since, or the
    latest stored catalog no longer offers it to the desk's tier (see _pick_still_qualifies)."""
    desk = ids.check("desk", desk)
    if type(retiring_within) is not int or retiring_within < 0:
        raise ValidationError("the retiring window must be a whole number of seconds")
    ts = ids.stamp(now)
    with db.transaction(conn):
        family = _model_desk(conn, desk)
        current = get_desk_model(conn, desk)
        if current is None or current["pending_model"] is None:
            raise ConflictError("this desk has no pending pick to approve")
        _check_allowed(current["pending_model"], blocked, conn)
        _pick_still_qualifies(conn, family, current, ts, retiring_within)
        change_id = _switch(conn, current, current["pending_model"], current["pending_effort"],
                            current["pending_line"], "approved", current["pinned"], ts)
    return {**get_desk_model(conn, desk), "change_id": change_id}


def _known(conn: Conn, family: str, value: str) -> bool:
    if family == "claude":
        return CLAUDE_ID.fullmatch(value) is not None or value in last_catalog(conn, "claude")
    return value in last_catalog(conn, "codex")


def pin(conn: Conn, desk: str, value: str, now: Optional[int] = None, blocked: Iterable[str] = (),
        retiring_within: int = RETIRING_SOON_SECONDS) -> dict:
    """Ryan pins a desk to a model of its own family, never a blocked one. Ollivander leaves a pinned desk alone.
    Pinning the model the desk already runs ends any trial on it (trial_end pinned), so no revert follows.
    His choice stands even on a model the latest catalog hides or retires soon, but warning then says so."""
    desk = ids.check("desk", desk)
    value = _check_allowed(check_name(value, "model"), blocked)
    if type(retiring_within) is not int or retiring_within < 0:
        raise ValidationError("the retiring window must be a whole number of seconds")
    ts = ids.stamp(now)
    with db.transaction(conn):
        family = _model_desk(conn, desk)
        _check_allowed(value, blocked, conn)
        if not _known(conn, family, value):
            if family == "claude":
                raise ValidationError("a Claude desk pins to a known alias or a full claude- model id")
            raise ValidationError("a Codex desk pins to a slug from the last Codex catalog")
        current = _ensure(conn, desk, ts)
        change_id = None
        trial_ended = False
        if current["model"] == value:
            trial_ended = current["trial_failures"] is not None
            conn.execute(
                "UPDATE desk_models SET pinned = 1, pending_model = NULL, pending_effort = NULL,"
                " pending_line = NULL, trial_failures = NULL,"
                " trial_end = CASE WHEN trial_failures IS NULL THEN trial_end ELSE 'pinned' END,"
                " updated_at = ? WHERE desk = ?",
                (ts, desk),
            )
        else:
            filed = ryan_lines(conn).get(value)
            line = filed["line"] if filed is not None and filed["line"] in db.MODEL_NEEDS else None
            change_id = _switch(conn, current, value, current["effort"], line, "pin", 1, ts)
        warning = _pin_warning(conn, family, value, ts, retiring_within)
    return {**get_desk_model(conn, desk), "change_id": change_id, "trial_ended": trial_ended, "warning": warning}


def _pin_warning(conn: Conn, family: str, value: str, ts: int, retiring_within: int) -> Optional[str]:
    """What Ryan should know about a model he pinned that the latest catalog hides or retires soon."""
    if family == "claude" and CLAUDE_ID.fullmatch(value) is not None:
        return None
    entry = catalog_entry(conn, family, value)
    if entry is None:
        return None
    why = _not_listed(entry, family) or _retiring_soon(entry, ts, retiring_within)
    return None if why is None else f"pinned as asked, but {why}"


def unpin(conn: Conn, desk: str, now: Optional[int] = None) -> dict:
    """Hand the desk back to its role. Ollivander's next run resolves it under the usual rules."""
    desk = ids.check("desk", desk)
    ts = ids.stamp(now)
    with db.transaction(conn):
        _model_desk(conn, desk)
        _ensure(conn, desk, ts)
        conn.execute("UPDATE desk_models SET pinned = 0, updated_at = ? WHERE desk = ?", (ts, desk))
    return get_desk_model(conn, desk)


# Runs


def last_run_model(conn: Conn, desk: str, claude_ids_only: bool = False) -> Optional[str]:
    """The model the desk's latest run recorded. claude_ids_only skips runs that recorded only an alias."""
    desk = ids.check("desk", desk)
    if claude_ids_only:
        row = db.fetch_one(conn, "SELECT model FROM metrics WHERE desk = ? AND model GLOB 'claude-*'"
                           " ORDER BY id DESC LIMIT 1", (desk,))
    else:
        row = db.fetch_one(conn, "SELECT model FROM metrics WHERE desk = ? ORDER BY id DESC LIMIT 1", (desk,))
    return None if row is None else row["model"]


def desk_choice(conn: Conn, desk: str) -> dict:
    """The desk's model and effort with the id of the switch that set them, read in one snapshot, so a switch
    that lands meanwhile never pairs one model with another's trial. Empty values mean none is set."""
    desk = ids.check("desk", desk)
    with db.snapshot(conn):
        row = get_desk_model(conn, desk) or {}
        return {"model": row.get("model"), "effort": row.get("effort"), "change_id": current_change(conn, desk)}


def current_change(conn: Conn, desk: str) -> Optional[int]:
    """The id of the desk's latest switch, or None. A run counts toward a trial only if planned under it."""
    row = db.fetch_one(conn, "SELECT MAX(id) AS id FROM model_changes WHERE desk = ?", (ids.check("desk", desk),))
    return None if row is None else row["id"]


def record_outcome(conn: Conn, desk: str, ok: bool, change_id: Optional[int], now: Optional[int] = None,
                   blocked: Iterable[str] = (), default_model: Optional[str] = None,
                   retiring_within: int = RETIRING_SOON_SECONDS) -> dict:
    """Count a finished run toward the trial after a switch. change_id is the switch the run was planned
    under: a run planned before the latest switch never counts. Two failures in a row revert and pin,
    unless Ryan made the switch or pinned the desk since: then the trial ends and run_desk tells him, but his
    choice stands. A revert never lands on a blocked model: when the previous model (default_model, the
    install default, when there was none) is blocked, or is an alias that ever ran as a blocked id, or is
    no model at all (a Codex desk's unchecked CLI default) while anything is blocked, the trial ends without
    a revert and run_desk tells Ryan. The same holds when the previous model is one Ryan filed as ignore, or
    one the latest catalog no longer lists, hides or retires soon (see _unavailable): a revert pins, so it
    must never lock a desk onto a model no pass would give it. trial_end records how each trial ended."""
    desk = ids.check("desk", desk)
    blocked = check_blocklist(blocked)
    if not isinstance(ok, bool):
        raise ValidationError("ok must be true or false")
    if change_id is not None and (type(change_id) is not int or change_id < 1):
        raise ValidationError("invalid change id")
    if type(retiring_within) is not int or retiring_within < 0:
        raise ValidationError("the retiring window must be a whole number of seconds")
    ts = ids.stamp(now)
    with db.transaction(conn):
        current = get_desk_model(conn, desk)
        if current is None or current["trial_failures"] is None:
            return {"reverted": False}
        latest = db.fetch_one(conn, "SELECT id, reason FROM model_changes WHERE desk = ? ORDER BY id DESC LIMIT 1",
                              (desk,))
        if latest is None or change_id != latest["id"]:
            return {"reverted": False}
        if ok:
            _end_trial(conn, desk, "passed", ts)
            return {"reverted": False}
        failures = current["trial_failures"] + 1
        if failures < REVERT_AFTER_FAILURES:
            conn.execute("UPDATE desk_models SET trial_failures = ?, updated_at = ? WHERE desk = ?",
                         (failures, ts, desk))
            return {"reverted": False}
        # pinned after an automatic switch means Ryan pinned the desk during its trial: his choice stands.
        if latest["reason"] in RYAN_REASONS or current["pinned"]:
            _end_trial(conn, desk, "held", ts)
            return {"reverted": False, "held": True, "model": current["model"],
                    "previous_model": current["previous_model"], "change_id": latest["id"]}
        target = current["previous_model"] if current["previous_model"] is not None else default_model
        ran_as = None if target is None else blocked_resolution(conn, target, blocked)
        unchecked = target is None and bool(blocked)
        barred = target is not None and (blocked_by(target, blocked) is not None or ran_as is not None)
        # The install default (no previous model) is the office's own setting, so only the blocklist judges it.
        unavailable = None if barred or current["previous_model"] is None \
            else _unavailable(conn, _model_desk(conn, desk), target, ts, retiring_within)
        if unchecked or barred or unavailable is not None:
            _end_trial(conn, desk, "revert_blocked", ts)
            return {"reverted": False, "revert_blocked": True, "model": current["model"],
                    "previous_model": target, "ran_as": ran_as, "unchecked": unchecked, "unavailable": unavailable,
                    "change_id": latest["id"]}
        change_id = _switch(conn, current, current["previous_model"], current["previous_effort"],
                            current["previous_line"], "revert", 1, ts)
    return {"reverted": True, "from_model": current["model"], "to_model": current["previous_model"],
            "change_id": change_id}


def _end_trial(conn: Conn, desk: str, how: str, ts: int) -> None:
    conn.execute("UPDATE desk_models SET trial_failures = NULL, trial_end = ?, updated_at = ? WHERE desk = ?",
                 (how, ts, desk))


def changes(conn: Conn, desk: str) -> list:
    return db.fetch_all(conn, "SELECT * FROM model_changes WHERE desk = ? ORDER BY id", (ids.check("desk", desk),))


def last_event_key(conn: Conn, desk: str, kind: str) -> Optional[str]:
    """The dedupe key of the desk's latest event of this kind, so a notice is raised again only on a change."""
    row = db.fetch_one(conn, "SELECT dedupe_key FROM events WHERE desk = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                       (ids.check("desk", desk), ids.check("kind", kind, "event kind")))
    return None if row is None else row["dedupe_key"]


# The stop file


def clear_stop(db_path: Path) -> dict:
    """Remove the stop file, and any update marker a crashed update left, next to the database. Ryan runs
    this through castle once he has looked."""
    state = Path(db_path).parent
    try:
        fd = os.open(str(state), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise NotFoundError("the office state folder does not exist") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValidationError("the office state folder is not a plain folder") from None
        raise
    cleared = False
    try:
        for name in (STOP_FILE, UPDATING_FILE):
            try:
                os.unlink(name, dir_fd=fd)
                cleared = True
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)
    return {"cleared": cleared, "stop_file": str(state / STOP_FILE)}
