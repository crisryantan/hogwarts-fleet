"""Auto-portrait: the office applies Dumbledore's own additions the night he writes them, while Ryan has switched it
on.

It is off by default. It is on only while the plain office file config.AUTO_PORTRAIT_FILE holds exactly "on", read
through common.opt_in_on, the one reader of every office switch, so no flag, store row, desk file or owl turns it
on. The nightly job (fleet/portrait.py) reads it before it takes Dumbledore's run slot, and apply reads it again
just before anything is applied, on the first night and on resume alike: off at either read and nothing applies.

A night with the switch on (night), after the export:

1. Take Dumbledore's only run slot (run_desk.desk_lock) and, under it, read his patch for the night's date once
   (portrait_patch.read_state). Arm the night in the store (pensieve.arm_auto_patch) with what was there: absent,
   present (and its sha256) or unreadable.
2. Run him through run_desk.run with that slot (lock_held). A run that is refused, capped, stopped, blocked or
   fails ends the night stopped with no event of its own: the run's own event tells Ryan.
3. Still under the slot, read the patch once more (validate). Only a patch that was absent before the run goes
   on: the job held the only slot from before the first read until this one, and a killed launcher's process keeps
   that slot (run_desk.desk_lock), so no other run of his wrote it in between. A patch that was there before, even
   one tonight's run rewrote, could carry another run's ops, so it stops the night. The patch is parsed and checked
   (portrait_patch.parse_patch), and the store keeps one snapshot (pensieve.snapshot_auto_patch): its sha256, the
   checked fact_add and memory_note_add ops as canonical ASCII JSON, and the op ids in patch order, held for Ryan
   and out of schema. No raw patch bytes are ever stored, so a secret in an op out of schema never reaches the store.
4. Let the slot go and apply (apply) from the snapshot, never from the file again. One transaction re-reads the
   night, checks the stored plan, stops for a patch Ryan has started applying by hand, then runs each addition in
   its own savepoint with a check of its effect (portrait_patch.memory_marks before and after: exactly one current
   fact or key point added, nothing taken away), and writes its ledger row under the manual path's key, the night's
   event and its ending. An op the store refuses, or one that would change memory already there, waits.

Every op that retires, edits or moves memory waits for Ryan, and so does every addition that did not apply. Ollivander's
stop file holds the apply too: with it in place, or the switch off, the night ends off with today's patch-ready event.

Events. A night with a patch ends in exactly one headmaster event for Ryan, built by this script from the date, op ids
(pattern and scrub checked when parsed), counts, the sha256 and fixed words, never from op text or refusal reasons:
portrait.auto when the night applied what it could (what applied, what waits, and the exact castle portrait apply
command for the rest, or castle portrait show <date> when that does not fit); portrait.auto-stopped, keyed by date and
attempt, when the night stopped after it was armed (its text goes through common.scrubbed_line and never carries the
sha256); portrait.patch-ready when the switch was off or the stop file in place at the apply. A clean run that wrote no
patch ends done with no event. A run that failed or was refused raises only its own event. The same line is the
night's outcome in the store, which castle portrait patches and show print.

Kills and signals. A SIGKILL leaves the store as it was; resume, at the start of the next nightly job, finishes every
night still armed or validated without reading the castle file again: an armed night whose run is over is told once
and closed (unless its run's own owl-keyed event told Ryan already), and a validated one is applied from its snapshot.
The ledger keys make an op apply at most once whatever was cut. SIGTERM, SIGHUP or Ctrl+C from arming until the
snapshot commits ends the night stopped with one event; after the snapshot it leaves the night validated for resume,
exactly as a SIGKILL there would, since the apply transaction rolls back whole.

It starts no process of its own.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

from hogwarts import db, ids, pensieve
from hogwarts.errors import StoreError, ValidationError

from fleet import common, config, portrait_patch, run_desk
from fleet.safefs import FleetError

DESK = portrait_patch.DESK
AUTO_KIND = "portrait.auto"
AUTO_KEY = "portrait:auto:{date}"
STOPPED_KIND = "portrait.auto-stopped"
STOPPED_KEY = "portrait:auto-stopped:{date}:{attempt}"
# The owl-keyed events run_desk raises for a run that failed or gave up waiting for its slot.
RUN_TOLD_KEYS = ("rundesk:failed:portrait:{owl}", "rundesk:lock-wait:portrait:{owl}")
STOP_LIMIT = 480
SHA_SHOWN = 12

STOP_SUMMARY = ("auto-portrait applied nothing from Dumbledore's {date} patch. castle portrait show {date} lists it,"
                " and you apply what you accept by hand. Why: {why}")
WHY_PART_WAY = "the night was stopped part way, before the patch was stored"
WHY_CUT_OFF = "the night was cut off before it read the patch"
WHY_REFUSED = "the patch file was refused ({reason})"
WHY_BEFORE_UNREADABLE = ("the patch file could not be read before tonight's review, so auto-portrait cannot tell"
                         " whether tonight's review wrote it")
WHY_BEFORE_PRESENT = ("a patch for this date was already there before tonight's review, so auto-portrait cannot tell"
                      " which run wrote which op")
WHY_MALFORMED = "the patch is not a valid patch ({reason})"
WHY_NOT_STORED = "the patch could not be stored ({reason})"
WHY_PLAN = "the plan it stored no longer checks out"
WHY_BY_HAND = "you already applied some of it by hand, so the rest is yours too"
WHY_APPLY_FAILED = "it could not apply it and will not try again ({reason})"

NO_PATCH = "Dumbledore wrote no patch for {date}, so nothing was applied"
RUN_REFUSED = "the run was refused or failed before it ended, so nothing was read or applied; its own event says why"
RUN_FAILED = "the run did not end cleanly, so nothing was read or applied; its own event says why"
RUN_TOLD = "the run did not end cleanly and its own event told you, so nothing was read or applied"
OFF_TONIGHT = "auto-portrait was off for the run of {date}, so nothing was applied by it"
OFF_AT_APPLY = ("auto-portrait was off or Ollivander's stop was in place when it came to apply, so nothing was"
                " applied; castle portrait show {date} lists the patch")


class _ChangesMemory(StoreError):
    """An addition that would take away or change memory already there, whatever its type says."""


class _Refused(Exception):
    """A stored plan the apply will not run, with the reason it stops the night."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def auto_portrait_on() -> bool:
    """Whether Ryan has switched auto-portrait on: the office file config.AUTO_PORTRAIT_FILE holds exactly "on"."""
    return common.opt_in_on(config.AUTO_PORTRAIT_FILE)


# Event and outcome text, built here from checked parts only


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _stop_line(date: str, why: str) -> str:
    """A stop event's summary: scrubbed before and after its cut, never holding the sha256, and naming castle portrait
    show <date> ahead of the reason, so a long reason never cuts the command off."""
    return common.scrubbed_line(STOP_SUMMARY.format(date=date, why=why), STOP_LIMIT)


def apply_command(date: str, sha: str, order: list, waiting: list) -> str:
    """The castle portrait apply command for the ops that wait, as castle portrait show prints it."""
    command = f"castle portrait apply {date} --sha256 {sha}"
    return command + (" --only " + ",".join(waiting) if len(waiting) < len(order) else "")


def _done_parts(date: str, sha: str, counts: tuple, lists: Optional[tuple], command: Optional[str]) -> str:
    total, applied, waiting, refused, unfit = counts
    line = (f"auto-portrait applied {applied} of {_plural(total, 'op', 'ops')} from Dumbledore's {date} patch"
            f" (sha256 {sha[:SHA_SHOWN]})")
    if lists is not None:
        line += ": " + (", ".join(lists[0]) or "none")
    line += "."
    if waiting:
        line += " " + _plural(waiting, "waits", "wait") + " for you"
        if lists is not None:
            line += ": " + ", ".join(lists[1])
        if refused:
            named = ", ".join(lists[2]) if lists is not None else f"{refused} of them"
            line += f" (the store refused {named} tonight; show says why)"
    if unfit:
        line += ("; " if waiting else " ") + f"{unfit} out of schema, never applied"
        if lists is not None:
            line += ": " + ", ".join(lists[3])
    if waiting or unfit:
        line += "."
    if waiting and command is not None:
        line += f" Read them with castle portrait show {date}, then apply the rest with {command}"
    elif waiting:
        line += f" castle portrait show {date} prints the command for the rest"
    elif unfit:
        line += f" castle portrait show {date} lists them. Nothing else waits for you"
    else:
        line += " Nothing waits for you"
    return line


def done_line(date: str, sha: str, order: list, applied: list, refused: list, unfit: list) -> str:
    """The night's one line once it applied what it could: what applied, what waits (every op in schema that did not
    apply, in patch order, refused ones named), what is out of schema, and the command for the rest. Held to
    pensieve.SUMMARY_LIMIT: the id lists become counts, then the command becomes castle portrait show <date>."""
    waiting = [op_id for op_id in order if op_id not in applied and op_id not in unfit]
    refused = [op_id for op_id in order if op_id in refused]
    counts = (len(order), len(applied), len(waiting), len(refused), len(unfit))
    lists = ([op_id for op_id in order if op_id in applied], waiting, refused,
             [op_id for op_id in order if op_id in unfit])
    command = apply_command(date, sha, order, waiting)
    for line in (_done_parts(date, sha, counts, lists, command), _done_parts(date, sha, counts, None, command),
                 _done_parts(date, sha, counts, None, None)):
        if len(line) <= pensieve.SUMMARY_LIMIT and _script_built(line, sha):
            return line
    return _done_parts(date, sha, counts, None, None)[:pensieve.SUMMARY_LIMIT]


def _script_built(line: str, sha: str) -> bool:
    """True when line is printable ASCII and, the sha256 aside, nothing in it is shaped like a secret."""
    rest = line.replace(sha, "")
    return line.isascii() and line.isprintable() and pensieve.scrub(rest) == rest


# Store writes, each best effort where the night must not fail on them


def _ts(now: Optional[int]) -> int:
    return ids.stamp(now)


def _write_stop(conn, row: dict, why: str, ts: int) -> str:
    """End the night stopped with its one headmaster event, inside the caller's transaction."""
    line = _stop_line(row["date"], why)
    pensieve.end_auto_patch(conn, row["date"], "stopped", line, now=ts)
    pensieve.add_event(conn, DESK, STOPPED_KIND, "headmaster", line,
                       dedupe_key=STOPPED_KEY.format(date=row["date"], attempt=row["attempt"]), now=ts)
    return line


def _stop(conn, date: str, attempt: int, why: str, now: Optional[int], states: tuple = ("armed",)) -> dict:
    """End attempt's night stopped with one headmaster event, in one transaction, while it is still in one of states.
    A store that refuses the write leaves the night as it was, for the next job. Never raises but for a signal."""
    try:
        with db.transaction(conn):
            row = pensieve.auto_patch(conn, date)
            if row is None or row["state"] not in states or row["attempt"] != attempt:
                return {"state": None if row is None else row["state"]}
            _write_stop(conn, row, why, _ts(now))
    except Exception:  # noqa: BLE001 - the night stays as it was and the next job finishes it
        return {"state": "left", "left": "the stop could not be written"}
    return {"state": "stopped"}


def _end_quietly(conn, date: str, attempt: int, state: str, outcome: str, now: Optional[int]) -> dict:
    """End attempt's armed night with no event of its own, since another event told Ryan. Best effort."""
    try:
        with db.transaction(conn):
            row = pensieve.auto_patch(conn, date)
            if row is None or row["state"] != "armed" or row["attempt"] != attempt:
                return {"state": None if row is None else row["state"]}
            pensieve.end_auto_patch(conn, date, state, outcome, now=_ts(now))
    except Exception:  # noqa: BLE001 - the night stays armed and the next job closes it
        return {"state": "left", "left": "the ending could not be written"}
    return {"state": state}


def close_unarmed(conn, date: str, now: Optional[int] = None) -> Optional[dict]:
    """With the switch off tonight, end a same-date night an earlier killed attempt left armed as off, with no event:
    tonight's own patch-ready or failure event tells Ryan. Best effort."""
    try:
        row = pensieve.auto_patch(conn, date)
    except Exception:  # noqa: BLE001 - nothing to close that can be read; resume tries again
        return None
    if row is None or row["state"] != "armed":
        return None
    return _end_quietly(conn, date, row["attempt"], "off", OFF_TONIGHT.format(date=date), now)


# The night


def night(conn, date: str, owl_id: str, now: Optional[int] = None, progress: Optional[dict] = None) -> tuple:
    """Run the night with the switch on: (the run's result, what the lane did, ids and counts only). progress gets
    "attempt" once this attempt's night is armed, in one step, so the caller tells Ryan of a signal itself only
    before that and a night never gets both. A FleetError or StoreError raised here comes from before the run
    returned, and the caller reports it as today; once the run has returned clean nothing the lane does raises one."""
    progress = {} if progress is None else progress
    with run_desk.desk_lock(DESK) as slot:
        try:
            before, value = portrait_patch.read_state(date)
            before_sha = hashlib.sha256(value).hexdigest() if before == "present" else None
            progress["attempt"] = pensieve.arm_auto_patch(conn, date, owl_id, before, before_sha, now=now)["attempt"]
            result = run_desk.run(conn, DESK, owl_id, config.PORTRAIT_MCP_JOB, now=now, lock_held=slot)
        except (FleetError, StoreError):
            if "attempt" in progress:
                _end_quietly(conn, date, progress["attempt"], "stopped", RUN_REFUSED, now)
            raise
        except BaseException:
            if "attempt" in progress:
                _stop(conn, date, progress["attempt"], WHY_PART_WAY, now)
            raise
        attempt = progress["attempt"]
        if result["exit_code"] != 0 or result["cap_source"] is not None:
            if result["cap_source"] is None:
                run_desk.report_failure(conn, DESK, owl_id, now)
            ended = _end_quietly(conn, date, attempt, "stopped", RUN_FAILED, now)
            return result, {"state": ended["state"]}
        try:
            checked = validate(conn, date, {"attempt": attempt, "before": before}, now)
        except BaseException:
            # A signal, or an error validate did not expect, before the snapshot committed. After it, the night
            # stays validated and the next job applies it.
            _stop(conn, date, attempt, WHY_PART_WAY, now)
            raise
    if checked["state"] != "validated":
        return result, checked
    return result, apply(conn, date, now)


def validate(conn, date: str, row: dict, now: Optional[int] = None) -> dict:
    """Read the patch once after a clean run, under the slot, and end the armed night (row: its attempt and what was
    there before the run) or store its one snapshot. Never raises but for a signal: a store that refuses an ending
    leaves the night armed for the next job."""
    attempt = row["attempt"]
    state, value = portrait_patch.read_state(date)
    if state == "absent":
        ended = _end_quietly(conn, date, attempt, "done", NO_PATCH.format(date=date), now)
        return {"state": ended["state"], "patch": False}
    if state == "unreadable":
        return _stop(conn, date, attempt, WHY_REFUSED.format(reason=value), now)
    sha = hashlib.sha256(value).hexdigest()
    if row["before"] == "unreadable":
        return {**_stop(conn, date, attempt, WHY_BEFORE_UNREADABLE, now), "sha256": sha}
    if row["before"] != "absent":
        return {**_stop(conn, date, attempt, WHY_BEFORE_PRESENT, now), "sha256": sha}
    try:
        plan = portrait_patch.classify(portrait_patch.parse_patch(value, date))
    except ValidationError as exc:
        return {**_stop(conn, date, attempt, WHY_MALFORMED.format(reason=common.one_line(exc, 200)), now),
                "sha256": sha}
    ops = json.dumps(plan["auto"], ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    try:
        pensieve.snapshot_auto_patch(conn, date, sha, ops, plan["order"], plan["held"], plan["unfit"], now=now)
    except (StoreError, sqlite3.Error) as exc:
        return {**_stop(conn, date, attempt, WHY_NOT_STORED.format(reason=common.one_line(exc, 200)), now),
                "sha256": sha}
    return {"state": "validated", "sha256": sha}


# The apply


def _stored_plan(row: dict) -> list:
    """The night's stored additions in patch order, each exactly as check_op gives it back and of a type auto-portrait
    applies, or _Refused. A plan that cannot be read is never an empty plan."""
    order, held, unfit = (portrait_patch.split_ids(row[name]) for name in ("order_ids", "held_ids", "unfit_ids"))
    try:
        ops = common.strict_json(row["ops"].encode("ascii"))
    except (UnicodeError, ValueError, RecursionError, AttributeError):
        raise _Refused(WHY_PLAN) from None
    if not isinstance(ops, list):
        raise _Refused(WHY_PLAN)
    found = {}
    for op in ops:
        op_id = op.get("id") if isinstance(op, dict) else None
        if (not isinstance(op_id, str) or portrait_patch.OP_ID.fullmatch(op_id) is None
                or pensieve.scrub(op_id) != op_id or op_id in found or op_id not in order or op_id in held
                or op_id in unfit or op.get("type") not in portrait_patch.AUTO_TYPES):
            raise _Refused(WHY_PLAN)
        try:
            checked = portrait_patch.check_op(op)
        except StoreError:
            raise _Refused(WHY_PLAN) from None
        if checked != op:
            raise _Refused(WHY_PLAN)
        found[op_id] = checked
    if set(found) != set(order) - set(held) - set(unfit):
        raise _Refused(WHY_PLAN)
    return [found[op_id] for op_id in order if op_id in found]


def _check_effect(kind: str, before: tuple, after: tuple) -> None:
    """An addition adds exactly one current fact or one key point and takes nothing away: _ChangesMemory otherwise."""
    (facts_before, points_before), (facts_after, points_after) = before, after
    if kind == "fact_add":
        fine = facts_before <= facts_after and len(facts_after - facts_before) == 1 and points_after == points_before
    elif kind == "memory_note_add":
        fine = points_before <= points_after and len(points_after - points_before) == 1 and facts_after == facts_before
    else:
        fine = False
    if not fine:
        raise _ChangesMemory("it would change memory already there")


def _apply_ops(conn, date: str, sha: str, plan: list, ts: int) -> tuple:
    """Each stored addition in patch order, each in its own savepoint with its effect checked: (applied, refused). The
    ledger key _apply_one writes makes each op apply at most once, whichever path gets there first."""
    applied, refused = [], []
    for op in plan:
        try:
            with db.transaction(conn):
                before = portrait_patch.memory_marks(conn, ts)
                portrait_patch._apply_one(conn, op, date, sha, ts, kind=portrait_patch.AUTO_APPLIED_KIND,
                                          summary=portrait_patch.AUTO_APPLIED_SUMMARY)
                _check_effect(op["type"], before, portrait_patch.memory_marks(conn, ts))
        except (StoreError, sqlite3.IntegrityError):
            refused.append(op["id"])
        else:
            applied.append(op["id"])
    return applied, refused


def _hold_for_you(conn, date: str, now: Optional[int]) -> dict:
    """The switch is off or the stop file in place at the apply: end the night off with today's patch-ready event,
    in one transaction. A store that refuses it leaves the night validated for the next job."""
    try:
        with db.transaction(conn):
            row = pensieve.auto_patch(conn, date)
            if row is None or row["state"] != "validated":
                return {"state": None if row is None else row["state"]}
            ts = _ts(now)
            pensieve.end_auto_patch(conn, date, "off", OFF_AT_APPLY.format(date=date), now=ts)
            pensieve.add_event(conn, DESK, portrait_patch.READY_KIND, "headmaster",
                               portrait_patch.READY_SUMMARY.format(date=date),
                               dedupe_key=portrait_patch.READY_KEY.format(date=date), now=ts)
    except Exception:  # noqa: BLE001 - the night stays validated and the next job holds or applies it
        return {"state": "validated", "left": "the hold could not be written"}
    return {"state": "off", "patch_ready": True, "sha256": row["sha256"]}


def apply(conn, date: str, now: Optional[int] = None) -> dict:
    """Apply a validated night's stored additions, for the first night and for resume. Off, or Ollivander's stop in
    place: the night ends off and nothing applies. Otherwise one transaction applies what the store takes and ends
    the night done with its one event. A signal rolls it back whole and leaves the night validated for the next job;
    any other failure ends it stopped with one event, or, if even that cannot be written, leaves it validated."""
    if not auto_portrait_on() or run_desk.stop_requested():
        return _hold_for_you(conn, date, now)
    try:
        with db.transaction(conn):
            row = pensieve.auto_patch(conn, date)
            if row is None or row["state"] != "validated":
                return {"state": None if row is None else row["state"]}
            ts = _ts(now)
            try:
                plan = _stored_plan(row)
                if portrait_patch.hand_applied(conn, date):
                    raise _Refused(WHY_BY_HAND)
            except _Refused as refusal:
                _write_stop(conn, row, refusal.why, ts)
                return {"state": "stopped", "sha256": row["sha256"]}
            order, unfit = portrait_patch.split_ids(row["order_ids"]), portrait_patch.split_ids(row["unfit_ids"])
            applied, refused = _apply_ops(conn, date, row["sha256"], plan, ts)
            line = done_line(date, row["sha256"], order, applied, refused, unfit)
            pensieve.add_event(conn, DESK, AUTO_KIND, "headmaster", line, dedupe_key=AUTO_KEY.format(date=date), now=ts)
            pensieve.end_auto_patch(conn, date, "done", line, applied, now=ts)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - busy past the timeout, the disk, or anything unexpected
        return _stop(conn, date, _attempt(conn, date), WHY_APPLY_FAILED.format(reason=type(exc).__name__), now,
                     states=("validated",))
    waiting = [op_id for op_id in order if op_id not in applied and op_id not in unfit]
    return {"state": "done", "sha256": row["sha256"], "applied": applied, "waiting": waiting, "refused": refused,
            "unfit": unfit}


def _attempt(conn, date: str) -> Optional[int]:
    try:
        row = pensieve.auto_patch(conn, date)
    except Exception:  # noqa: BLE001 - unknown, so _stop leaves the night as it is
        return None
    return None if row is None else row["attempt"]


# Resume


def _run_told(conn, owl_id: str) -> bool:
    """Whether the run of this owl raised its own failed or lock-wait event. A read that fails raises, so the night is
    left for the next job rather than guessed either way."""
    return any(pensieve.events_with_key_prefix(conn, key.format(owl=owl_id)) for key in RUN_TOLD_KEYS)


def _resume_one(conn, row: dict, today: str, now: Optional[int]) -> dict:
    date = row["date"]
    if row["state"] == "validated":
        return {"date": date, **apply(conn, date, now)}
    if date > today or (date == today and row["owl_acked_at"] is None):
        return {"date": date, "state": "armed"}  # tonight's run arms it again, or the switch off closes it
    if _run_told(conn, row["owl_id"]):
        return {"date": date, **_end_quietly(conn, date, row["attempt"], "stopped", RUN_TOLD, now)}
    return {"date": date, **_stop(conn, date, row["attempt"], WHY_CUT_OFF, now)}


def resume(conn, today: str, now: Optional[int] = None) -> list:
    """Finish every night an earlier job left armed or validated, oldest first, without reading the castle. An armed
    night whose run is over is told once and closed; a validated one is applied from its snapshot. Each night that
    cannot be finished is left as it was for the next job. Never raises but for a signal. Ids and states only."""
    try:
        rows = pensieve.open_auto_patches(conn)
    except Exception as exc:  # noqa: BLE001 - every night stays as it was
        return [{"error": type(exc).__name__}]
    done = []
    for row in rows:
        try:
            done.append(job_view(_resume_one(conn, row, today, now)))
        except Exception as exc:  # noqa: BLE001 - this night stays as it was; the next job tries again
            done.append({"date": row["date"], "state": row["state"], "left": type(exc).__name__})
    return done


def job_view(found: dict) -> dict:
    """What the job prints of a night: its date, state, sha256 and op ids, never op text or refusal reasons."""
    keys = ("date", "state", "sha256", "applied", "waiting", "refused", "unfit", "patch_ready", "left")
    return {key: found[key] for key in keys if key in found}
