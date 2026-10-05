"""Dumbledore's dated patches: list them, check them and apply what Ryan accepts.

Dumbledore (the portrait desk) never changes a fact or a memory himself. Each night he writes a dated
patch, desks/portrait/outbox/patch-<YYYY-MM-DD>.ops in the castle: one JSON object of typed operations,
each with its reason and source. Ryan reads it with castle portrait show and applies what he accepts with
castle portrait apply. This module sits on that trust boundary, so everything in a patch is untrusted data
from a desk:

- The file is read once through safefs: no link anywhere on the way, a plain file Ryan owns that nobody
  else can write, one hard link, at most 256KB. It must be strict JSON: UTF-8, no repeated key inside an
  object, no NaN or Infinity, no decimals, and no number longer than 18 digits.
- The patch must be exactly {"format": "portrait-patch-1", "date": <its file's date>, "ops": [...]}, with 1
  to 100 ops, each an object with its own unique id. Anything else refuses the whole file.
- Each op is checked against its type's schema: exactly its fields, each of the right JSON type, ids and
  keys by their patterns, and every text one printable line within its limit that the store's scrubber
  would leave alone, so no secret, token, email, IP address or long hex string rides along. An op that
  fails is shown with its problem and can never be applied. Leave it out with --only.
- Apply needs the sha256 that show printed, so the bytes Ryan reviewed are the bytes applied. Every op it
  takes must be in schema and not applied before, and they all run in one store transaction, in patch
  order: one refusal leaves nothing applied, and the error names the op.
- Ops run only through the store's own APIs: facts.add_fact, facts.set_key, facts.supersede,
  facts.withdraw, pensieve.archive and pensieve.add_keypoint. An archive move is never carried out. It
  is a note Ryan acts on by hand if he agrees.
- Each applied op is recorded as a routine portrait.applied event on the portrait desk, whose dedupe key
  names the patch date and op id, so an op is applied at most once.

Nothing in a patch is executed, evaluated or used as a path. Show checks the ops that are in schema by
running them in a store transaction that is always rolled back, so it reports what the store would refuse
without changing anything.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import unicodedata
from typing import Iterator, Optional

from hogwarts import db, facts, ids, pensieve
from hogwarts.errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

from fleet import common, config, safefs
from fleet.safefs import FleetError, Missing

DESK = "portrait"
FORMAT = "portrait-patch-1"
PATCH_NAME = "patch-{date}.ops"
NOTE_NAME = "morning-{date}.md"
PATCH_FILE = re.compile(r"patch-([0-9]{4}-[0-9]{2}-[0-9]{2})\.ops")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
OP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PATCH_MAX_BYTES = 256 * 1024
MAX_OPS = 100
MAX_TAGS = 8
MAX_DIGITS = 18
REASON_LIMIT = 400
SOURCE_LIMIT = 200
ENTRY_LIMIT = 200
TARGET_LIMIT = 100
LIST_LIMIT = 30
RETIRE_HOW = ("archive", "withdraw")
NOTE_TAG = "portrait"
APPLIED_KIND = "portrait.applied"
APPLIED_KEY = "portrait:applied:{date}:{op_id}"
# The fact source a patch op leaves on the facts it writes, so each one points back at its patch and op.
SOURCE_LABEL = "portrait:{date}:{op_id}"
APPLIED_SUMMARY = "Ryan applied {type} {op_id} from Dumbledore's {date} patch (sha256 {sha}): {done}"
MOVE_BY_HAND = "accepted: the move is yours to make by hand"
COMMON_FIELDS = ("id", "type", "reason", "source")
# Each op type: its required fields, then its optional ones.
TYPES = {
    "fact_add": (("scope", "text", "tier"), ("subject_key", "lookup", "expires_at")),
    "fact_retire": (("fact_id", "how"), ()),
    "fact_edit": (("fact_id", "text"), ("subject_key", "tier", "lookup", "expires_at")),
    "memory_note_add": (("text",), ("tags",)),
    "archive_move": (("entry", "to"), ()),
}
# Control characters, and the line and paragraph separators some terminals treat as a line break.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


# Reading


def check_date(value: object) -> str:
    """A patch date: YYYY-MM-DD, and a real day on the calendar."""
    if not isinstance(value, str) or DATE.fullmatch(value) is None:
        raise ValidationError("a patch date is YYYY-MM-DD")
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError("a patch date is YYYY-MM-DD") from None
    return value


def _outbox():
    """Dumbledore's castle outbox, opened with no link anywhere on the way."""
    return safefs.opened_dir(config.CASTLE_ROOT, "desks", DESK, "outbox")


def read_patch(date: str) -> bytes:
    """The bytes of the patch for date, read once. A missing file is NotFoundError, an unsafe one ValidationError."""
    name = PATCH_NAME.format(date=check_date(date))
    try:
        with _outbox() as fd:
            return safefs.read_regular(fd, name, PATCH_MAX_BYTES, "patch file")
    except Missing:
        raise NotFoundError(f"there is no patch for {date}") from None
    except FleetError as exc:
        raise ValidationError(f"the patch for {date} was refused: {common.one_line(exc, 200)}") from None


def patch_exists(date: str) -> bool:
    """True when a plain patch file for date is in Dumbledore's outbox."""
    try:
        name = PATCH_NAME.format(date=check_date(date))
        with _outbox() as fd:
            return safefs.is_safe_regular(fd, name)
    except (FleetError, StoreError):
        return False


def _unique_pairs(pairs: list) -> dict:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("repeated key")
    return dict(pairs)


def _whole(text: str) -> int:
    if len(text.lstrip("-")) > MAX_DIGITS:
        raise ValueError("number too long")
    return int(text)


def _refuse(text: str) -> None:
    raise ValueError("not allowed")


def _load(raw: bytes) -> object:
    """Strict JSON: UTF-8, no repeated key, no NaN or Infinity, no decimals and no very long number."""
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_refuse,
                          parse_float=_refuse, parse_int=_whole)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValidationError("the patch is not strict UTF-8 JSON: no repeated key, NaN, Infinity, decimal"
                              f" or number over {MAX_DIGITS} digits") from None


# The schema


def _line(value: object, field: str, limit: int) -> str:
    """One printable line, with no space at either end, that the store's scrubber would leave alone."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{field} is not valid text") from None
    if _UNPRINTABLE.search(value) or any(unicodedata.category(char) == "Cf" for char in value):
        raise ValidationError(f"{field} must be one line of printable text")
    if not value.strip() or value != value.strip() or len(value) > limit:
        raise ValidationError(f"{field} must be 1 to {limit} characters with no space at either end")
    if pensieve.scrub(value) != value:
        raise ValidationError(f"{field} holds what looks like a secret or personal data, which never goes in a patch")
    return value


def _unscrubbed(value: str, field: str) -> str:
    """A keyed value the store's scrubber would leave alone: its pattern alone admits IPs, long hex and tokens."""
    if pensieve.scrub(value) != value:
        raise ValidationError(f"{field} holds what looks like a secret or personal data, which never goes in a patch")
    return value


def _tags(value: object) -> list:
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise ValidationError(f"tags must be a list of at most {MAX_TAGS} tags")
    tags = [_unscrubbed(ids.check("tag", tag), "a tag") for tag in value]
    if len(set(tags)) != len(tags):
        raise ValidationError("tags repeat a tag")
    return tags


def _field(name: str, value: object, kind: str) -> object:
    """One op field, checked by its own rule. Text limits follow the store's own."""
    if name == "scope":
        return "fleet" if value == "fleet" else _unscrubbed(ids.check("desk", value, "scope"), "scope")
    if name == "text":
        limit = pensieve.KEYPOINT_LIMIT if kind == "memory_note_add" else pensieve.FACT_LIMIT
        return _line(value, "text", limit)
    if name == "tier":
        return ids.check_enum(value, db.FACT_TIERS, "tier")
    if name == "subject_key":
        return _unscrubbed(ids.check("subject_key", value), "subject_key")
    if name == "lookup":
        return _line(value, "lookup", db.LOOKUP_LIMIT)
    if name == "expires_at":
        return ids.check_int(value, "expires_at", maximum=ids.MAX_TIME)
    if name == "fact_id":
        return ids.check_int(value, "fact_id", minimum=1)
    if name == "how":
        return ids.check_enum(value, RETIRE_HOW, "how")
    if name == "tags":
        return _tags(value)
    if name == "entry":
        return _line(value, "entry", ENTRY_LIMIT)
    return _line(value, "to", TARGET_LIMIT)


def check_op(op: dict) -> dict:
    """A copy of one op with every field checked, or ValidationError saying what is wrong with it."""
    kind = op.get("type")
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValidationError("type must be one of " + ", ".join(TYPES))
    required, optional = TYPES[kind]
    missing = [name for name in COMMON_FIELDS + required if name not in op]
    if missing:
        raise ValidationError("missing " + ", ".join(missing))
    extra = sorted(set(op) - set(COMMON_FIELDS + required + optional))
    if extra:
        # The names are untrusted text, so they are counted, never shown.
        raise ValidationError(f"{kind} was given {len(extra)} field(s) it does not take")
    checked = {"id": op["id"], "type": kind, "reason": _line(op["reason"], "reason", REASON_LIMIT),
               "source": _line(op["source"], "source", SOURCE_LIMIT)}
    for name in required + optional:
        if name in op:
            checked[name] = _field(name, op[name], kind)
    if kind == "fact_add" and (checked["tier"] == "perishable") != ("expires_at" in checked):
        raise ValidationError("a perishable fact needs expires_at, and only a perishable fact takes it")
    return checked


def parse_patch(raw: bytes, date: str) -> dict:
    """The patch's ops, each with its checked fields or the problem that keeps it out. A file that is not a
    patch for date raises ValidationError."""
    data = _load(raw)
    if not isinstance(data, dict) or set(data) != {"format", "date", "ops"}:
        raise ValidationError("a patch is one object with exactly format, date and ops")
    if data["format"] != FORMAT:
        raise ValidationError(f"the patch format must be {FORMAT}")
    if data["date"] != date:
        raise ValidationError("the date inside the patch is not the date in its file name")
    ops = data["ops"]
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        raise ValidationError(f"ops must be a list of 1 to {MAX_OPS} operations")
    entries, seen = [], set()
    for index, op in enumerate(ops):
        op_id = op.get("id") if isinstance(op, dict) else None
        if not isinstance(op_id, str) or OP_ID.fullmatch(op_id) is None or pensieve.scrub(op_id) != op_id:
            # Checked before any message can name it: an id the scrubber would change is never shown.
            raise ValidationError(f"op {index} needs an id of 1 to 24 lowercase letters, digits and hyphens"
                                  " that does not look like a secret")
        if op_id in seen:
            raise ValidationError(f"op id {op_id} is used twice")
        seen.add(op_id)
        kind = op.get("type") if isinstance(op.get("type"), str) and op.get("type") in TYPES else None
        try:
            entries.append({"id": op_id, "type": kind, "op": check_op(op), "problem": None})
        except ValidationError as exc:
            entries.append({"id": op_id, "type": kind, "op": None, "problem": str(exc)})
    return {"date": date, "ops": entries}


# Applying


def applied_ops(conn, date: str) -> dict:
    """The ops of date's patch already applied: op id to the event that recorded each."""
    prefix = APPLIED_KEY.format(date=check_date(date), op_id="")
    return {event["dedupe_key"][len(prefix):]: event for event in pensieve.events_with_key_prefix(conn, prefix)}


def _current_fact(conn, fact_id: int, ts: int) -> dict:
    found = next((row for row in facts.current_facts(conn, now=ts) if row["id"] == fact_id), None)
    if found is None:
        raise ConflictError(f"fact {fact_id} is not a current fact")
    return found


def _edit_key(fact: dict, given: Optional[str]) -> str:
    if fact["subject_key"] is not None:
        if given not in (None, fact["subject_key"]):
            raise ConflictError(f"fact {fact['id']} already has a subject key, and the edit names another")
        return fact["subject_key"]
    if given is None:
        raise ValidationError(f"fact {fact['id']} has no subject key, so the edit must name one")
    return given


def _run_op(conn, op: dict, date: str, ts: int) -> dict:
    """Carry out one checked op through the store's own APIs, and say what it did."""
    kind, label = op["type"], SOURCE_LABEL.format(date=date, op_id=op["id"])
    if kind == "fact_add":
        row = facts.add_fact(conn, op["scope"], op["text"], op["tier"], label, op.get("expires_at"),
                             op.get("subject_key"), None, op.get("lookup"), now=ts)
        return {"fact_id": row["id"], "done": f"added fact {row['id']}"}
    if kind == "memory_note_add":
        row = pensieve.add_keypoint(conn, op["text"], [NOTE_TAG, *op.get("tags", [])], now=ts)
        return {"keypoint_id": row["id"], "done": f"added key point {row['id']}"}
    if kind == "archive_move":
        return {"done": MOVE_BY_HAND}
    fact = _current_fact(conn, op["fact_id"], ts)
    if kind == "fact_retire" and op["how"] == "archive":
        pensieve.archive(conn, [fact["id"]], now=ts)
        return {"fact_id": fact["id"], "done": f"archived fact {fact['id']}"}
    if kind == "fact_retire":
        restored = facts.withdraw(conn, fact["id"], DESK, now=ts)["restored_id"]
        brought = "" if restored is None else f", which brought back the fact it replaced as fact {restored}"
        return {"fact_id": fact["id"], "restored_id": restored, "done": f"withdrew fact {fact['id']}{brought}"}
    key = _edit_key(fact, op.get("subject_key"))
    if fact["subject_key"] is None:
        facts.set_key(conn, fact["id"], key, now=ts)
    tier = op.get("tier", fact["tier"])
    expires = op.get("expires_at", fact["expires_at"] if tier == "perishable" else None)
    replaced = facts.supersede(conn, fact["scope"], key, op["text"], label, tier, None,
                               op.get("lookup", fact["lookup"]), expires, now=ts)
    return {"fact_id": replaced["fact_id"], "replaced_id": fact["id"],
            "done": f"fact {fact['id']} replaced by fact {replaced['fact_id']}"}


def _apply_one(conn, op: dict, date: str, sha: str, ts: int) -> dict:
    """Run one op and record it as applied. Errors name the op."""
    try:
        done = _run_op(conn, op, date, ts)
        event = pensieve.add_event(
            conn, DESK, APPLIED_KIND, "routine",
            APPLIED_SUMMARY.format(type=op["type"], op_id=op["id"], date=date, sha=sha[:12], done=done["done"]),
            dedupe_key=APPLIED_KEY.format(date=date, op_id=op["id"]), now=ts)
        if not event["created"]:
            raise ConflictError("it was already applied")
    except sqlite3.IntegrityError as exc:
        raise IntegrityError(f"op {op['id']}: {common.one_line(exc, 200)}") from exc
    except StoreError as exc:
        raise type(exc)(f"op {op['id']}: {exc}") from exc
    return done


class _Undo(Exception):
    """Raised inside a trial so its transaction always rolls back."""


def _trial(conn, entries: list, date: str, sha: str, ts: int) -> dict:
    """Run the given ops in patch order in a transaction that is always rolled back: op id to what it would
    do, or to the store's reason for refusing it. Each op sees the ops before it."""
    checks: dict = {}
    try:
        with db.transaction(conn):
            for entry in entries:
                try:
                    with db.transaction(conn):
                        checks[entry["id"]] = {"would": _apply_one(conn, entry["op"], date, sha, ts)["done"]}
                except (StoreError, sqlite3.IntegrityError) as exc:
                    checks[entry["id"]] = {"problem": common.one_line(exc, 300)}
            raise _Undo()
    except _Undo:
        pass
    return checks


def _shown(entry: dict, applied: dict, checks: dict) -> dict:
    item = {"id": entry["id"], "type": entry["type"]}
    if entry["problem"] is not None:
        return {**item, "status": "out of schema", "problem": entry["problem"]}
    item["fields"] = {name: value for name, value in entry["op"].items() if name not in ("id", "type")}
    if entry["id"] in applied:
        return {**item, "status": "applied", "applied_at": applied[entry["id"]]["ts"]}
    check = checks[entry["id"]]
    if "problem" in check:
        return {**item, "status": "the store would refuse it", "problem": check["problem"]}
    return {**item, "status": "ready", "would": check["would"]}


def show(conn, date: str, now: Optional[int] = None) -> dict:
    """Every op in date's patch with its status (ready, applied, out of schema, or what the store would
    refuse), its fields, and the exact command that applies the ready ones. Changes nothing."""
    date = check_date(date)
    raw = read_patch(date)
    sha = hashlib.sha256(raw).hexdigest()
    patch = parse_patch(raw, date)
    applied = applied_ops(conn, date)
    pending = [entry for entry in patch["ops"] if entry["problem"] is None and entry["id"] not in applied]
    checks = _trial(conn, pending, date, sha, ids.stamp(now))
    ops = [_shown(entry, applied, checks) for entry in patch["ops"]]
    ready = [item["id"] for item in ops if item["status"] == "ready"]
    command = None
    if ready:
        command = f"castle portrait apply {date} --sha256 {sha}"
        if len(ready) < len(ops):
            command += " --only " + ",".join(ready)
    return {"date": date, "sha256": sha, "ops": ops, "ready": ready, "apply_command": command}


def _check_sha(value: object) -> str:
    text = value.lower() if isinstance(value, str) else ""
    if SHA256.fullmatch(text) is None:
        raise ValidationError("--sha256 must be the 64 hex characters castle portrait show printed")
    return text


def _wanted(only: Optional[list]) -> Optional[list]:
    """The op ids --only names, in the order given. Each value may hold several, separated by commas."""
    if only is None:
        return None
    wanted = []
    for value in only:
        for part in (value.split(",") if isinstance(value, str) else [None]):
            if part == "":
                continue
            if not isinstance(part, str) or OP_ID.fullmatch(part) is None:
                raise ValidationError("--only takes op ids of lowercase letters, digits and hyphens")
            if part in wanted:
                raise ValidationError(f"--only names {part} twice")
            wanted.append(part)
    if not wanted:
        raise ValidationError("--only needs at least one op id")
    return wanted


def _choose(entries: list, only: Optional[list]) -> list:
    wanted = _wanted(only)
    if wanted is None:
        return list(entries)
    known = {entry["id"] for entry in entries}
    unknown = [op_id for op_id in wanted if op_id not in known]
    if unknown:
        raise ValidationError("--only names ops this patch does not have: " + ", ".join(unknown))
    return [entry for entry in entries if entry["id"] in wanted]


def apply(conn, date: str, sha256: str, only: Optional[list] = None, now: Optional[int] = None) -> dict:
    """Apply date's patch, or the ops --only names, if the file still hashes to sha256. Every chosen op must
    be in schema and not yet applied. They run in patch order in one transaction, so a refusal applies none."""
    date = check_date(date)
    expected = _check_sha(sha256)
    raw = read_patch(date)
    sha = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(sha, expected):
        raise IntegrityError("the patch changed since it was shown, so nothing was applied. Run castle portrait"
                             " show again and read it before you apply it")
    patch = parse_patch(raw, date)
    chosen = _choose(patch["ops"], only)
    bad = [f"{entry['id']} ({entry['problem']})" for entry in chosen if entry["problem"] is not None]
    if bad:
        raise ValidationError("out of schema, so nothing was applied: " + "; ".join(bad)[:300]
                              + ". Leave them out with --only")
    ts = ids.stamp(now)
    with db.transaction(conn):
        applied = applied_ops(conn, date)
        already = [entry["id"] for entry in chosen if entry["id"] in applied]
        if already:
            raise ConflictError("already applied, so nothing was applied: " + ", ".join(already))
        done = [{"id": entry["id"], "type": entry["type"], **_apply_one(conn, entry["op"], date, sha, ts)}
                for entry in chosen]
    return {
        "date": date, "sha256": sha, "applied": done,
        "for_you": [{"id": entry["id"], "entry": entry["op"]["entry"], "to": entry["op"]["to"]}
                    for entry in chosen if entry["type"] == "archive_move"],
        "left_out": [entry["id"] for entry in patch["ops"] if entry not in chosen],
    }


# Listing


def _patch_names() -> Iterator[str]:
    try:
        with _outbox() as fd:
            names = [name for name in os.listdir(fd) if PATCH_FILE.fullmatch(name)]
    except Missing:
        raise NotFoundError("Dumbledore's outbox does not exist") from None
    except FleetError as exc:
        raise ValidationError(f"Dumbledore's outbox was refused: {common.one_line(exc, 200)}") from None
    return iter(sorted(names, reverse=True)[:LIST_LIMIT])


def patches(conn) -> list:
    """The newest patches in Dumbledore's outbox, newest first: each one's sha256, its op ids, the ones out
    of schema and the ones applied. A patch that cannot be read or parsed says why instead."""
    found = []
    for name in _patch_names():
        item = {"file": name}
        try:
            date = check_date(PATCH_FILE.fullmatch(name).group(1))
            item.update(date=date, applied=sorted(applied_ops(conn, date)))
            raw = read_patch(date)
            patch = parse_patch(raw, date)
            item.update(sha256=hashlib.sha256(raw).hexdigest(), ops=[entry["id"] for entry in patch["ops"]],
                        out_of_schema=[entry["id"] for entry in patch["ops"] if entry["problem"] is not None])
        except StoreError as exc:
            item["problem"] = common.one_line(exc, 200)
        found.append(item)
    return found
