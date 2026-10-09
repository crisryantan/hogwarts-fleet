from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import capacity, db, facts, followups, ids, owlery, pensieve, views, wands
from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

_WHOLE = re.compile(r"[0-9]{1,18}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_AMOUNT = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9})?")
_PLUS_WHOLE = re.compile(r"\+[0-9]{1,3}")
_PLUS_AMOUNT = re.compile(r"\+[0-9]{1,3}(?:\.[0-9]{1,2})?")
TOKEN_LINE_LIMIT = 200
OPS_FILE_LIMIT = 256 * 1024
# What a list command prints by default, newest kept, so a paste into a session stays small. --all prints every row,
# --limit N at most N. A per-task history (review rounds, a fact's versions) keeps its last HISTORY_LIMIT entries.
LIST_LIMIT = 20
HISTORY_LIMIT = 10


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


class Listing:
    """A list command's rows after its cap, with how many there were. main prints the rows as data and adds total,
    shown and truncated beside them, and a note when rows were left out."""

    def __init__(self, data: object, total: int, shown: int) -> None:
        self.data, self.total, self.shown = data, total, shown

    def fields(self) -> dict:
        fields = {"total": self.total, "shown": self.shown, "truncated": self.shown < self.total}
        if fields["truncated"]:
            fields["note"] = f"showing {self.shown} of {self.total}, use --all for everything"
        return fields


def _cap(args: argparse.Namespace, default: int) -> Optional[int]:
    """The row cap a list command was given: None with --all, else --limit or the command's default."""
    return None if args.all else (args.limit or default)


def _newest(rows: list, args: argparse.Namespace, default: int = LIST_LIMIT) -> Listing:
    """The newest rows of an oldest-first list, kept in their order."""
    cap = _cap(args, default)
    shown = rows if cap is None or len(rows) <= cap else rows[-cap:]
    return Listing(shown, len(rows), len(shown))


def _positive(value: str) -> int:
    number = _whole(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a whole number of at least 1")
    return number


def _whole(value: str) -> int:
    if _WHOLE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a whole number")
    return int(value)


def _amount(value: str) -> float:
    if _AMOUNT.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a decimal amount")
    return float(value)


def _plus_whole(value: str) -> int:
    if _PLUS_WHOLE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected +N, a whole number of extra runs")
    return int(value[1:])


def _plus_amount(value: str) -> float:
    if _PLUS_AMOUNT.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected +X, extra dollars with at most two decimals")
    return float(value[1:])


def _stdin_text(limit: int) -> str:
    text = sys.stdin.read(limit + 1)
    if len(text) > limit:
        raise ValidationError(f"stdin is longer than {limit} characters")
    return text


def _body(args: argparse.Namespace) -> Optional[str]:
    return _stdin_text(owlery.BODY_LIMIT) if args.body_stdin else None


def _text(args: argparse.Namespace, limit: int) -> str:
    return _stdin_text(limit) if args.text_stdin else args.text


def _token(args: argparse.Namespace) -> Optional[str]:
    return sys.stdin.readline(TOKEN_LINE_LIMIT).strip() if args.token_stdin else None


def _check_ops_parents(path: str) -> None:
    # Every directory above the file must be a real directory that no other user can change.
    parent = os.path.dirname(path)
    while True:
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            raise NotFoundError("ops file not found") from None
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError("a directory above the ops file is a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError("the ops file path runs through something that is not a directory")
        if info.st_uid not in (0, db._uid()):
            raise ValidationError("a directory above the ops file is owned by another user")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValidationError("a directory above the ops file is group or world writable")
        if parent == "/":
            return
        parent = os.path.dirname(parent)


def _open_ops_file(value: str) -> int:
    path = ids.check_absolute(value, "ops file path")
    _check_ops_parents(path)
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise NotFoundError("ops file not found") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValidationError("ops file must not be a symlink") from None
        raise


def _check_ops_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("ops file must be a regular file")
    if info.st_uid != db._uid():
        raise ValidationError("ops file must be owned by the current user")
    if info.st_mode & 0o022:
        raise ValidationError("ops file must not be group or world writable")


def _read_ops_file(value: str) -> bytes:
    fd = _open_ops_file(value)
    try:
        _check_ops_file(fd)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(OPS_FILE_LIMIT + 1)
    if len(data) > OPS_FILE_LIMIT:
        raise ValidationError("ops file is larger than 256KB")
    return data


def _unique_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValidationError("ops file repeats a key inside one object")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValidationError(f"ops file uses {name}, which is not JSON")


def _check_digest(data: bytes, expected: Optional[str]) -> None:
    if expected is None:
        return
    expected = expected.lower()
    if _SHA256.fullmatch(expected) is None:
        raise ValidationError("--sha256 must be 64 hex characters")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise IntegrityError("ops file does not match --sha256, so it changed after it was reviewed")


def _ops(args: argparse.Namespace) -> list:
    data = _read_ops_file(args.file)
    _check_digest(data, args.sha256)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ValidationError("ops file is not valid UTF-8 JSON") from None


# Handlers


def _init(path: Path, args: argparse.Namespace) -> dict:
    existed = os.path.lexists(path)
    conn = db.connect(path, create=True)
    try:
        version = db.schema_version(conn)
    finally:
        conn.close()
    return {"db": str(path), "created": not existed, "schema_version": version}


def _doctor(path: Path, args: argparse.Namespace) -> dict:
    return db.doctor(path)


def _review_check(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    return {
        "repo": args.repo,
        "sha": args.sha,
        "pass": owlery.has_pass(conn, args.repo, args.sha),
        "latest": owlery.latest_review(conn, args.repo, args.sha),
    }


def _request_show(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    return {**owlery.get_request(conn, args.request), "owls": owlery.request_owls(conn, args.request)}


def _event_ack(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    """Ack one event by id, or every unacked headmaster event with --all, of --kind, or of --task."""
    picked = [args.event is not None, args.all, args.kind is not None or args.task is not None]
    if sum(picked) != 1:
        raise ValidationError("name one event id, or --all, or --kind and/or --task")
    if args.event is not None:
        return pensieve.ack(conn, args.event)
    return pensieve.ack_matching(conn, None if args.all else args.kind, None if args.all else args.task)


def _fact_decay(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    """The stale fact ids, or with --archive the ids it archived (every stale fact, whatever the cap), newest kept."""
    key, ids_found = ("archived", pensieve.archive_stale(conn)["archived"]) if args.archive else (
        "stale", pensieve.decay(conn))
    kept = _newest(ids_found, args)
    return Listing({key: kept.data}, kept.total, kept.shown)


def _fact_list(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    if args.context is not None:
        if args.archived or args.history:
            raise ValidationError("--context lists current facts only, so it takes no --archived or --history")
        # Most used first, so the cap keeps the head.
        rows = pensieve.context_facts(conn, args.context)
        cap = _cap(args, LIST_LIMIT)
        shown = rows if cap is None else rows[:cap]
        return Listing(shown, len(rows), len(shown))
    return _newest(pensieve.list_facts(conn, args.scope, args.archived, args.history), args)


def _fact_as_of(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    if args.world is not None:
        return _newest(facts.as_of_world(conn, args.world, args.scope), args)
    return _newest(facts.as_of_belief(conn, args.belief, args.scope), args)


def _audit(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    """The audit, each finding list cut to its newest rows. An escalation still raises every finding first."""
    report = owlery.audit(conn, escalate=args.escalate)
    total = shown = 0
    for key, rows in report.items():
        if isinstance(rows, list):
            kept = _newest(rows, args)
            report[key] = kept.data
            total, shown = total + kept.total, shown + kept.shown
    return Listing(report, total, shown)


def _clock() -> int:
    return ids.stamp(None)


def _fleet_caps():
    # The cap numbers are the fleet's settings, kept in fleet/config.py next to this package in the office.
    from fleet import config as fleet_config

    return fleet_config


def _cap_status(conn: sqlite3.Connection, desk: str, now: int) -> dict:
    """One desk's caps as the launcher reads them (fleet/run_desk.py next to this package), with the spend its runs
    still going hold (spend_held_usd), so these numbers agree with the launcher's refusals. It settles nothing: a
    run that ended with no one left to record it stays held at its budget until the next launch decision records
    it, so these numbers run high, never low."""
    from fleet import run_desk as fleet_run_desk

    return fleet_run_desk.cap_status(conn, desk, now)


def _desk_cap(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    """A bump to one capped desk's runs or spend cap, until the next cap reset."""
    caps = _fleet_caps()
    desk = pensieve.get_desk(conn, args.desk)["name"]
    kind, amount = ("runs", args.runs) if args.runs is not None else ("spend", args.spend)
    capped = caps.DAILY_RUN_CAP if kind == "runs" else caps.DAILY_SPEND_CAP_USD
    if desk not in capped:
        raise ValidationError(f"{desk} has no fleet {kind} cap to bump")
    now = _clock()
    _, resets_at = capacity.day_bounds(now, caps.CAP_RESET_UTC_SECONDS)
    bump = capacity.add_bump(conn, desk, kind, amount, resets_at, now=now)
    return {"bump": bump, "caps": _cap_status(conn, desk, now)}


def _desk_caps(conn: sqlite3.Connection, args: argparse.Namespace) -> list:
    caps = _fleet_caps()
    now = _clock()
    registered = {desk["name"] for desk in pensieve.list_desks(conn)}
    return [_cap_status(conn, desk, now) for desk in sorted(caps.DAILY_RUN_CAP) if desk in registered]


def _blocked_models() -> tuple:
    # Models the organisation forbids, kept in fleet/config.py with the other fleet settings.
    from fleet import config as fleet_config

    return fleet_config.BLOCKED_MODEL_PREFIXES


def _retiring_window() -> int:
    from fleet import config as fleet_config

    return fleet_config.RETIRING_SOON_SECONDS


def _task_board(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    """Every active or awaiting-close author task by desk, and any task with a run going for it: its round,
    verdict and whether a run is going. Each desk keeps its counts; its task rows are the newest across the board."""
    caps = _fleet_caps()
    board = capacity.in_flight(conn, _clock(), caps.RUNNING_WINDOW_SECONDS, args.desk, caps.REVIEW_ROUND_CAP,
                               caps.FOLLOWUP_ROUND_CAP)
    rows = sorted((task for row in board["desks"] for task in row["tasks"]),
                  key=lambda task: (task["created_at"], task["id"]))
    kept = {task["id"] for task in _newest(rows, args).data}
    for row in board["desks"]:
        row["tasks"] = [task for task in row["tasks"] if task["id"] in kept]
    # A desk with none of the rows shown is counted, not listed, so the groups stay as few as the rows.
    shown = [row for row in board["desks"] if row["tasks"]]
    board.update(desks=shown, desks_total=len(board["desks"]), desks_shown=len(shown))
    return Listing(board, len(rows), len(kept))


def _task_list(conn: sqlite3.Connection, args: argparse.Namespace) -> object:
    """The open tasks as one line each, newest first, unless --all, --open or --status asks for the full records:
    every task with --all alone, else the newest of those asked for."""
    if args.all or args.open or args.status:
        return _newest(pensieve.list_tasks(conn, args.desk, args.status, args.open), args)
    return views.task_lines(conn, _clock(), _fleet_caps(), args.desk, args.limit or views.LIST_CAP)


def _task_builds(conn: sqlite3.Connection, args: argparse.Namespace) -> list:
    cap = _cap(args, views.LIST_CAP)
    return views.build_lines(conn, _clock(), _fleet_caps(), args.all, cap)


def _task_start(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    """Start a queued task. A build desk's task gets the TASK.md check that fleet worktree makes before it starts
    one (kept in fleet/worktree.py next to this package), so this command is no way round it. Every other desk's
    task starts as the store allows."""
    task = pensieve.get_task(conn, args.task)
    if task["desk"] not in _fleet_caps().WORKTREE_DESKS:
        return pensieve.start_task(conn, task["id"])
    from fleet import worktree as fleet_worktree
    from fleet.safefs import FleetError

    try:
        return fleet_worktree.start_task(conn, task["id"])
    except FleetError as exc:
        raise ConflictError(str(exc)) from None


def _task_run(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The task's newest run launch as the launcher left it (fleet/run_desk.py next to this package): its id, model and
    start, its end record and last output write from the office runs folder, and going: no usage recorded, no end
    record (one that cannot be read is left to the other two) and launched inside the running window, as the board's
    running. No pid is kept for a run. None when no run was launched for the task. Read only."""
    launch = capacity.newest_launch(conn, task_id)
    if launch is None:
        return None
    from fleet import run_desk as fleet_run_desk

    seen = fleet_run_desk.run_seen(launch["desk"], launch["run_id"])
    recorded = launch["metric_id"] is not None
    recent = _clock() - launch["launched_at"] < _fleet_caps().RUNNING_WINDOW_SECONDS
    return {"run_id": launch["run_id"], "desk": launch["desk"], "model": launch["model"],
            "launched_at": launch["launched_at"], "usage_recorded": recorded, **seen,
            "going": not recorded and recent and not isinstance(seen["end"], dict)}


def _task_show(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    """The task's record, its newest run (run) and its newest event of any kind (newest_event). Read only."""
    task = pensieve.get_task(conn, args.task)
    return {**task, "run": _task_run(conn, task["id"]),
            "newest_event": pensieve.newest_events(conn, [task["id"]]).get(task["id"])}


def _desk_model(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    if args.approve:
        return wands.approve(conn, args.desk, blocked=_blocked_models(), retiring_within=_retiring_window())
    if args.role:
        return wands.unpin(conn, args.desk)
    return wands.pin(conn, args.desk, args.value, blocked=_blocked_models(), retiring_within=_retiring_window())


def _ollivander_clear(path: Path, args: argparse.Namespace) -> dict:
    return wands.clear_stop(path)


def _portrait():
    # Dumbledore's patches are castle files. The fleet's patch module next to this package reads them through
    # safefs, checks them against its schema and applies accepted ops through this package's own APIs.
    from fleet import portrait_patch

    return portrait_patch


def _portrait_patches(conn: sqlite3.Connection, args: argparse.Namespace) -> Listing:
    patch = _portrait()
    found, total = patch.patch_listing(conn, _cap(args, patch.LIST_LIMIT))
    return Listing(found, total, len(found))


HANDLERS: dict[str, Callable] = {
    "desk add": lambda c, a: pensieve.add_desk(c, a.name, a.family, a.role, a.model),
    "desk list": lambda c, a: _newest(pensieve.list_desks(c), a),
    "desk cap": _desk_cap,
    "desk caps": lambda c, a: _newest(_desk_caps(c, a), a),
    "desk model": _desk_model,
    "desk models": lambda c, a: _newest(wands.list_desk_models(c), a),
    "desk many-tasks": lambda c, a: pensieve.allow_many_tasks(c, a.desk),
    "model line": lambda c, a: wands.classify(c, a.name, a.line, blocked=_blocked_models()),
    "task create": lambda c, a: pensieve.create_task(
        c, a.desk, a.title, a.intent_path, a.parent, a.request, a.session, a.worktree, a.id),
    "task start": _task_start,
    "task await-close": lambda c, a: pensieve.mark_awaiting_close(c, a.task, a.repo, a.sha),
    "task commit": lambda c, a: pensieve.record_commit(c, a.task, a.repo, a.sha),
    "task worktree": lambda c, a: pensieve.set_worktree(c, a.task, a.path),
    "task close": lambda c, a: pensieve.close_task(c, a.task, a.reason, _token(a)),
    "task show": _task_show,
    "task list": lambda c, a: _task_list(c, a),
    "task builds": _task_builds,
    "task board": _task_board,
    "task allow-round": lambda c, a: capacity.allow_round(c, a.task, _clock()),
    "task rounds": lambda c, a: _newest(views.rounds_with_branch(c, a.task, _fleet_caps()), a, HISTORY_LIMIT),
    "followup list": lambda c, a: _newest(followups.list_followups(c, a.task), a),
    "followup show": lambda c, a: followups.show(c, a.task),
    "token mint": lambda c, a: owlery.mint(c, a.task, "cli", a.ttl),
    "owl send": lambda c, a: owlery.send(
        c, a.sender, a.recipient, a.kind, a.subject, _body(a), a.body_path, a.task, a.request,
        a.reply_to, a.key),
    "owl inbox": lambda c, a: _newest(owlery.inbox(c, a.desk, a.all), a),
    "owl read": lambda c, a: owlery.read(c, a.owl, a.as_desk),
    "owl ack": lambda c, a: owlery.ack(c, a.owl, a.as_desk),
    "request open": lambda c, a: owlery.open_request(
        c, a.sender, a.recipient, a.title, _body(a), a.body_path, a.parent, a.key),
    "request advance": lambda c, a: owlery.advance(c, a.request, a.phase, a.detail),
    "request defer": lambda c, a: owlery.defer(c, a.request, a.reason),
    "request decline": lambda c, a: owlery.decline(c, a.request, a.reason),
    "request show": _request_show,
    "request list": lambda c, a: _newest(owlery.list_requests(c, a.desk, a.phase, a.open), a),
    "review record": lambda c, a: owlery.record_review(
        c, a.repo, a.sha, a.task, a.reviewer, a.verdict, a.review_path),
    "review check": _review_check,
    "event add": lambda c, a: pensieve.add_event(c, a.desk, a.kind, a.verdict, a.summary, a.task, a.dedupe_key),
    "event drain": lambda c, a: pensieve.drain(c, a.max_chars),
    "event ack": lambda c, a: _event_ack(c, a),
    "event settle": lambda c, a: pensieve.settle_events(c),
    "pensieve session": lambda c, a: pensieve.record_session(
        c, a.session, a.project, a.desk, a.model, a.started_at, a.ended_at, a.first_turn_tokens,
        a.total_input_tokens),
    "pensieve extract": lambda c, a: pensieve.add_extract(
        c, a.session, a.role, _text(a, pensieve.EXTRACT_LIMIT), a.seq),
    "pensieve keypoint": lambda c, a: pensieve.add_keypoint(
        c, _text(a, pensieve.KEYPOINT_LIMIT), a.tags, a.session),
    "pensieve find": lambda c, a: pensieve.find(c, a.query, a.limit),
    "fact add": lambda c, a: pensieve.add_fact(
        c, a.scope, a.text, a.tier, a.source, a.expires_at, a.subject_key, a.valid_from, a.lookup),
    "fact touch": lambda c, a: pensieve.touch(c, a.fact),
    "fact decay": _fact_decay,
    "fact archive": lambda c, a: pensieve.archive(c, a.facts),
    "fact list": _fact_list,
    "fact supersede": lambda c, a: facts.supersede(
        c, a.scope, a.subject_key, a.text, a.source, a.tier, a.valid_from, a.lookup, a.expires_at),
    "fact withdraw": lambda c, a: facts.withdraw(c, a.fact, a.desk),
    "fact expire": lambda c, a: {"expired": facts.expire(c)},
    "fact current": lambda c, a: _newest(facts.current_facts(c, a.scope), a),
    "fact find": lambda c, a: facts.find_facts(c, a.query, a.scope, a.history, a.limit),
    "fact as-of": _fact_as_of,
    "fact history": lambda c, a: _newest(facts.history(c, a.scope, a.subject_key), a, HISTORY_LIMIT),
    "fact candidates": lambda c, a: _newest(facts.contradiction_candidates(c, a.since, a.limit_per_fact), a),
    "fact apply": lambda c, a: facts.apply_ops(c, _ops(a)),
    "portrait patches": _portrait_patches,
    "portrait show": lambda c, a: _portrait().show(c, a.date),
    "portrait apply": lambda c, a: _portrait().apply(c, a.date, a.sha256, a.only),
    "metric add": lambda c, a: pensieve.add_metric(
        c, a.desk, a.run_id, a.model, a.input_tokens, a.output_tokens, a.cache_read_tokens,
        a.cost_usd, a.duration_ms, a.ts),
    "metric summary": lambda c, a: _newest(pensieve.summary(c, a.since), a),
    "purge": lambda c, a: owlery.purge(c, body_days=a.body_days, extract_days=a.extract_days),
    "audit": _audit,
}

PATH_HANDLERS: dict[str, Callable] = {"init": _init, "doctor": _doctor, "ollivander clear": _ollivander_clear}


# Parser


def _sub(group: argparse._SubParsersAction, name: str, key: str) -> argparse.ArgumentParser:
    parser = group.add_parser(name, allow_abbrev=False)
    parser.set_defaults(handler_key=key)
    return parser


def _group(commands: argparse._SubParsersAction, name: str) -> argparse._SubParsersAction:
    parser = commands.add_parser(name, allow_abbrev=False)
    return parser.add_subparsers(dest="action", required=True, parser_class=_Parser)


def _body_options(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--body-path")
    source.add_argument("--body-stdin", action="store_true")


def _text_options(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-stdin", action="store_true")


def _limit_options(parser: argparse.ArgumentParser, default: int, rows: str, every: Optional[str] = None) -> None:
    """--all for every row and --limit N for at most N; with neither, the newest default rows."""
    cap = parser.add_mutually_exclusive_group()
    cap.add_argument("--all", action="store_true", help=every or f"every {rows}, with no cap")
    cap.add_argument("--limit", type=_positive, help=f"at most N {rows}, newest kept (default {default})")


def _desk_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "desk")
    add = _sub(group, "add", "desk add")
    add.add_argument("name")
    add.add_argument("--family", required=True)
    add.add_argument("--role")
    add.add_argument("--model")
    _limit_options(_sub(group, "list", "desk list"), LIST_LIMIT, "desks")
    bump = _sub(group, "cap", "desk cap")
    bump.add_argument("desk")
    amount = bump.add_mutually_exclusive_group(required=True)
    amount.add_argument("--runs", type=_plus_whole)
    amount.add_argument("--spend", type=_plus_amount)
    _limit_options(_sub(group, "caps", "desk caps"), LIST_LIMIT, "desks")
    _sub(group, "many-tasks", "desk many-tasks").add_argument("desk")
    _desk_model_parsers(group)


def _desk_model_parsers(group: argparse._SubParsersAction) -> None:
    model = _sub(group, "model", "desk model")
    model.add_argument("desk")
    choice = model.add_mutually_exclusive_group(required=True)
    choice.add_argument("value", nargs="?", help="pin to this alias, full claude id or catalog slug")
    choice.add_argument("--role", action="store_true", help="unpin, so the role picks again")
    choice.add_argument("--approve", action="store_true", help="switch to the pending pick")
    _limit_options(_sub(group, "models", "desk models"), LIST_LIMIT, "desks")


def _model_parsers(commands: argparse._SubParsersAction) -> None:
    line = _sub(_group(commands, "model"), "line", "model line")
    line.add_argument("name")
    line.add_argument("line", choices=db.MODEL_LINES)
    _sub(_group(commands, "ollivander"), "clear", "ollivander clear")


def _task_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "task")
    create = _sub(group, "create", "task create")
    create.add_argument("--desk", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--id")
    create.add_argument("--intent-path")
    create.add_argument("--parent")
    create.add_argument("--request")
    create.add_argument("--session")
    create.add_argument("--worktree")
    for name in ("start", "show", "allow-round"):
        _sub(group, name, f"task {name}").add_argument("task")
    rounds = _sub(group, "rounds", "task rounds")
    rounds.add_argument("task")
    _limit_options(rounds, HISTORY_LIMIT, "review rounds")
    awaiting = _sub(group, "await-close", "task await-close")
    awaiting.add_argument("task")
    awaiting.add_argument("--repo")
    awaiting.add_argument("--sha")
    attach = _sub(group, "worktree", "task worktree")
    attach.add_argument("task")
    attach.add_argument("--path", required=True)
    commit = _sub(group, "commit", "task commit")
    commit.add_argument("task")
    commit.add_argument("--repo", required=True)
    commit.add_argument("--sha", required=True)
    close = _sub(group, "close", "task close")
    close.add_argument("task")
    close.add_argument("--reason", required=True)
    close.add_argument("--token-stdin", action="store_true")
    listing = _sub(group, "list", "task list")
    listing.add_argument("--desk")
    which = listing.add_mutually_exclusive_group()
    which.add_argument("--status", help="full records of one status")
    which.add_argument("--open", action="store_true", help="full records of queued, active or awaiting close tasks")
    _limit_options(listing, LIST_LIMIT, "tasks", "full records of every task (of --status or --open when given), "
                   "oldest first, with no cap")
    board = _sub(group, "board", "task board")
    board.add_argument("--desk")
    _limit_options(board, LIST_LIMIT, "task rows")
    builds = _sub(group, "builds", "task builds")
    _limit_options(builds, views.LIST_CAP, "builds", "every build, closed ones too, with no cap")


def _followup_parsers(commands: argparse._SubParsersAction) -> None:
    """PR follow-ups, read only: there is no command that changes one."""
    group = _group(commands, "followup")
    listing = _sub(group, "list", "followup list")
    listing.add_argument("--task")
    _limit_options(listing, LIST_LIMIT, "follow-ups")
    _sub(group, "show", "followup show").add_argument("task")


def _token_parsers(commands: argparse._SubParsersAction) -> None:
    mint = _sub(_group(commands, "token"), "mint", "token mint")
    mint.add_argument("task")
    mint.add_argument("--ttl", type=_whole, default=owlery.TOKEN_TTL_DEFAULT)


def _owl_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "owl")
    send = _sub(group, "send", "owl send")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="recipient", required=True)
    send.add_argument("--kind", required=True)
    send.add_argument("--subject", required=True)
    _body_options(send)
    send.add_argument("--task")
    send.add_argument("--request")
    send.add_argument("--reply-to")
    send.add_argument("--key")
    inbox = _sub(group, "inbox", "owl inbox")
    inbox.add_argument("desk")
    _limit_options(inbox, LIST_LIMIT, "unacked owls", "every owl, acked ones too, with no cap")
    for name in ("read", "ack"):
        parser = _sub(group, name, f"owl {name}")
        parser.add_argument("owl")
        parser.add_argument("--as", dest="as_desk", required=True)


def _request_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "request")
    opened = _sub(group, "open", "request open")
    opened.add_argument("--from", dest="sender", required=True)
    opened.add_argument("--to", dest="recipient", required=True)
    opened.add_argument("--title", required=True)
    _body_options(opened)
    opened.add_argument("--parent")
    opened.add_argument("--key")
    advance = _sub(group, "advance", "request advance")
    advance.add_argument("request")
    advance.add_argument("phase")
    advance.add_argument("--detail")
    for name in ("defer", "decline"):
        parser = _sub(group, name, f"request {name}")
        parser.add_argument("request")
        parser.add_argument("--reason", required=True)
    _sub(group, "show", "request show").add_argument("request")
    listing = _sub(group, "list", "request list")
    listing.add_argument("--desk")
    listing.add_argument("--phase")
    listing.add_argument("--open", action="store_true")
    _limit_options(listing, LIST_LIMIT, "requests")


def _review_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "review")
    record = _sub(group, "record", "review record")
    for name in ("--repo", "--sha", "--task", "--reviewer", "--verdict"):
        record.add_argument(name, required=True)
    record.add_argument("--review-path")
    check = _sub(group, "check", "review check")
    check.add_argument("--repo", required=True)
    check.add_argument("--sha", required=True)


def _event_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "event")
    add = _sub(group, "add", "event add")
    for name in ("--desk", "--kind", "--verdict", "--summary"):
        add.add_argument(name, required=True)
    add.add_argument("--task")
    add.add_argument("--dedupe-key")
    _sub(group, "drain", "event drain").add_argument("--max-chars", type=_whole, default=1500)
    ack = _sub(group, "ack", "event ack")
    ack.add_argument("event", type=_whole, nargs="?", help="one event id")
    ack.add_argument("--all", action="store_true", help="every unacked headmaster event")
    ack.add_argument("--kind", help="every unacked event of this kind, such as review.ready-for-push")
    ack.add_argument("--task", help="every unacked event of this task")
    _sub(group, "settle", "event settle")


def _pensieve_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "pensieve")
    session = _sub(group, "session", "pensieve session")
    session.add_argument("session")
    session.add_argument("--project", required=True)
    session.add_argument("--desk")
    session.add_argument("--model")
    for name in ("--started-at", "--ended-at", "--first-turn-tokens", "--total-input-tokens"):
        session.add_argument(name, type=_whole)
    extract = _sub(group, "extract", "pensieve extract")
    extract.add_argument("session")
    extract.add_argument("--role", required=True)
    extract.add_argument("--seq", type=_whole)
    _text_options(extract)
    keypoint = _sub(group, "keypoint", "pensieve keypoint")
    _text_options(keypoint)
    keypoint.add_argument("--tags", default="")
    keypoint.add_argument("--session")
    find = _sub(group, "find", "pensieve find")
    find.add_argument("query")
    find.add_argument("--limit", type=_whole, default=10)


def _new_fact_options(parser: argparse.ArgumentParser, tier_default: Optional[str]) -> None:
    parser.add_argument("--scope", required=True)
    parser.add_argument("--tier", required=tier_default is None, default=tier_default)
    parser.add_argument("--text", required=True)
    parser.add_argument("--expires-at", type=_whole)
    parser.add_argument("--source", default="ryan")
    parser.add_argument("--valid-from", type=_whole)
    parser.add_argument("--lookup")


def _fact_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "fact")
    add = _sub(group, "add", "fact add")
    _new_fact_options(add, None)
    add.add_argument("--subject-key")
    _sub(group, "touch", "fact touch").add_argument("fact", type=_whole)
    decay = _sub(group, "decay", "fact decay")
    decay.add_argument("--archive", action="store_true")
    _limit_options(decay, LIST_LIMIT, "fact ids")
    _sub(group, "archive", "fact archive").add_argument("facts", type=_whole, nargs="+")
    listing = _sub(group, "list", "fact list")
    view = listing.add_mutually_exclusive_group()
    view.add_argument("--scope")
    view.add_argument("--context")
    listing.add_argument("--archived", action="store_true")
    listing.add_argument("--history", action="store_true")
    _limit_options(listing, LIST_LIMIT, "facts")
    _temporal_fact_parsers(group)


def _temporal_fact_parsers(group: argparse._SubParsersAction) -> None:
    supersede = _sub(group, "supersede", "fact supersede")
    _new_fact_options(supersede, "aging")
    supersede.add_argument("--subject-key", required=True)
    withdraw = _sub(group, "withdraw", "fact withdraw")
    withdraw.add_argument("fact", type=_whole)
    withdraw.add_argument("--desk")
    _sub(group, "expire", "fact expire")
    current = _sub(group, "current", "fact current")
    current.add_argument("--scope")
    _limit_options(current, LIST_LIMIT, "facts")
    find = _sub(group, "find", "fact find")
    find.add_argument("query")
    find.add_argument("--scope")
    find.add_argument("--history", action="store_true")
    find.add_argument("--limit", type=_whole, default=10)
    as_of = _sub(group, "as-of", "fact as-of")
    moment = as_of.add_mutually_exclusive_group(required=True)
    moment.add_argument("--world", type=_whole)
    moment.add_argument("--belief", type=_whole)
    as_of.add_argument("--scope")
    _limit_options(as_of, LIST_LIMIT, "facts")
    history = _sub(group, "history", "fact history")
    history.add_argument("--scope", required=True)
    history.add_argument("--subject-key", required=True)
    _limit_options(history, HISTORY_LIMIT, "versions")
    candidates = _sub(group, "candidates", "fact candidates")
    candidates.add_argument("--since", type=_whole, required=True)
    candidates.add_argument("--limit-per-fact", type=_whole, default=3)
    _limit_options(candidates, LIST_LIMIT, "candidate pairs")
    apply = _sub(group, "apply", "fact apply")
    apply.add_argument("--file", required=True)
    apply.add_argument("--sha256")


def _portrait_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "portrait")
    _limit_options(_sub(group, "patches", "portrait patches"), 30, "patches")
    _sub(group, "show", "portrait show").add_argument("date")
    apply = _sub(group, "apply", "portrait apply")
    apply.add_argument("date")
    apply.add_argument("--sha256", required=True, help="the hash castle portrait show printed")
    apply.add_argument("--only", nargs="+", help="op ids to apply, the rest left out")


def _metric_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "metric")
    add = _sub(group, "add", "metric add")
    add.add_argument("--desk", required=True)
    add.add_argument("--run-id", required=True)
    add.add_argument("--model", required=True)
    for name in ("--input-tokens", "--output-tokens", "--cache-read-tokens", "--duration-ms"):
        add.add_argument(name, type=_whole, required=True)
    add.add_argument("--cost-usd", type=_amount, required=True)
    add.add_argument("--ts", type=_whole)
    summary = _sub(group, "summary", "metric summary")
    summary.add_argument("--since", type=_whole, default=0)
    _limit_options(summary, LIST_LIMIT, "desks")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="castle", description="Hogwarts store. All output is JSON.", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("init", "doctor"):
        _sub(commands, name, name)
    for build in (_desk_parsers, _task_parsers, _followup_parsers, _token_parsers, _owl_parsers, _request_parsers,
                  _review_parsers, _event_parsers, _pensieve_parsers, _fact_parsers, _metric_parsers,
                  _model_parsers, _portrait_parsers):
        build(commands)
    purge = _sub(commands, "purge", "purge")
    purge.add_argument("--body-days", type=_whole, default=30)
    purge.add_argument("--extract-days", type=_whole, default=90)
    audit = _sub(commands, "audit", "audit")
    audit.add_argument("--escalate", action="store_true")
    _limit_options(audit, LIST_LIMIT, "rows of each finding")
    return parser


# Entry point


def _emit(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n")
    stream.flush()


def _run(args: argparse.Namespace, path: Path) -> tuple[int, object]:
    if args.handler_key in PATH_HANDLERS:
        data = PATH_HANDLERS[args.handler_key](path, args)
        failed = args.handler_key == "doctor" and not data["ok"]
        return (IntegrityError.exit_code if failed else 0), data
    conn = db.connect(path, create=False)
    try:
        return 0, HANDLERS[args.handler_key](conn, args)
    finally:
        conn.close()


def _failure(kind: str, code: int, message: str) -> dict:
    return {"ok": False, "error": {"type": kind, "code": code, "message": message[:500]}}


def main(argv: Optional[Sequence[str]] = None, db_path: Optional[Path] = None) -> int:
    path = db.DEFAULT_DB if db_path is None else Path(db_path)
    try:
        args = build_parser().parse_args(argv)
        code, data = _run(args, path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except StoreError as exc:
        _emit(sys.stderr, _failure(type(exc).__name__, exc.exit_code, str(exc)))
        return exc.exit_code
    except Exception as exc:
        _emit(sys.stderr, _failure(type(exc).__name__, 1, str(exc)))
        return 1
    if isinstance(data, Listing):
        _emit(sys.stdout, {"ok": code == 0, "data": data.data, **data.fields()})
    else:
        _emit(sys.stdout, {"ok": code == 0, "data": data})
    return code
