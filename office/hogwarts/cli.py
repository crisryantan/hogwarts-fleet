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

from . import capacity, db, facts, followups, ids, owlery, pensieve, wands
from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

_WHOLE = re.compile(r"[0-9]{1,18}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_AMOUNT = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9})?")
_PLUS_WHOLE = re.compile(r"\+[0-9]{1,3}")
_PLUS_AMOUNT = re.compile(r"\+[0-9]{1,3}(?:\.[0-9]{1,2})?")
TOKEN_LINE_LIMIT = 200
OPS_FILE_LIMIT = 256 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


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


def _fact_decay(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    return pensieve.archive_stale(conn) if args.archive else {"stale": pensieve.decay(conn)}


def _fact_list(conn: sqlite3.Connection, args: argparse.Namespace) -> list:
    if args.context is not None:
        if args.archived or args.history:
            raise ValidationError("--context lists current facts only, so it takes no --archived or --history")
        return pensieve.context_facts(conn, args.context)
    return pensieve.list_facts(conn, args.scope, args.archived, args.history)


def _fact_as_of(conn: sqlite3.Connection, args: argparse.Namespace) -> list:
    if args.world is not None:
        return facts.as_of_world(conn, args.world, args.scope)
    return facts.as_of_belief(conn, args.belief, args.scope)


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


def _task_board(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    """Every active or awaiting-close author task by desk, and any task with a run going for it: its round,
    verdict and whether a run is going."""
    caps = _fleet_caps()
    return capacity.in_flight(conn, _clock(), caps.RUNNING_WINDOW_SECONDS, args.desk, caps.REVIEW_ROUND_CAP,
                              caps.FOLLOWUP_ROUND_CAP)


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


HANDLERS: dict[str, Callable] = {
    "desk add": lambda c, a: pensieve.add_desk(c, a.name, a.family, a.role, a.model),
    "desk list": lambda c, a: pensieve.list_desks(c),
    "desk cap": _desk_cap,
    "desk caps": _desk_caps,
    "desk model": _desk_model,
    "desk models": lambda c, a: wands.list_desk_models(c),
    "desk many-tasks": lambda c, a: pensieve.allow_many_tasks(c, a.desk),
    "model line": lambda c, a: wands.classify(c, a.name, a.line, blocked=_blocked_models()),
    "task create": lambda c, a: pensieve.create_task(
        c, a.desk, a.title, a.intent_path, a.parent, a.request, a.session, a.worktree, a.id),
    "task start": _task_start,
    "task await-close": lambda c, a: pensieve.mark_awaiting_close(c, a.task, a.repo, a.sha),
    "task commit": lambda c, a: pensieve.record_commit(c, a.task, a.repo, a.sha),
    "task worktree": lambda c, a: pensieve.set_worktree(c, a.task, a.path),
    "task close": lambda c, a: pensieve.close_task(c, a.task, a.reason, _token(a)),
    "task show": lambda c, a: pensieve.get_task(c, a.task),
    "task list": lambda c, a: pensieve.list_tasks(c, a.desk, a.status, a.open),
    "task board": _task_board,
    "task allow-round": lambda c, a: capacity.allow_round(c, a.task, _clock()),
    "task rounds": lambda c, a: capacity.review_rounds(c, a.task),
    "followup list": lambda c, a: followups.list_followups(c, a.task),
    "followup show": lambda c, a: followups.show(c, a.task),
    "token mint": lambda c, a: owlery.mint(c, a.task, "cli", a.ttl),
    "owl send": lambda c, a: owlery.send(
        c, a.sender, a.recipient, a.kind, a.subject, _body(a), a.body_path, a.task, a.request,
        a.reply_to, a.key),
    "owl inbox": lambda c, a: owlery.inbox(c, a.desk, a.all),
    "owl read": lambda c, a: owlery.read(c, a.owl, a.as_desk),
    "owl ack": lambda c, a: owlery.ack(c, a.owl, a.as_desk),
    "request open": lambda c, a: owlery.open_request(
        c, a.sender, a.recipient, a.title, _body(a), a.body_path, a.parent, a.key),
    "request advance": lambda c, a: owlery.advance(c, a.request, a.phase, a.detail),
    "request defer": lambda c, a: owlery.defer(c, a.request, a.reason),
    "request decline": lambda c, a: owlery.decline(c, a.request, a.reason),
    "request show": _request_show,
    "request list": lambda c, a: owlery.list_requests(c, a.desk, a.phase, a.open),
    "review record": lambda c, a: owlery.record_review(
        c, a.repo, a.sha, a.task, a.reviewer, a.verdict, a.review_path),
    "review check": _review_check,
    "event add": lambda c, a: pensieve.add_event(c, a.desk, a.kind, a.verdict, a.summary, a.task, a.dedupe_key),
    "event drain": lambda c, a: pensieve.drain(c, a.max_chars),
    "event ack": lambda c, a: pensieve.ack(c, a.event),
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
    "fact current": lambda c, a: facts.current_facts(c, a.scope),
    "fact find": lambda c, a: facts.find_facts(c, a.query, a.scope, a.history, a.limit),
    "fact as-of": _fact_as_of,
    "fact history": lambda c, a: facts.history(c, a.scope, a.subject_key),
    "fact candidates": lambda c, a: facts.contradiction_candidates(c, a.since, a.limit_per_fact),
    "fact apply": lambda c, a: facts.apply_ops(c, _ops(a)),
    "portrait patches": lambda c, a: _portrait().patches(c),
    "portrait show": lambda c, a: _portrait().show(c, a.date),
    "portrait apply": lambda c, a: _portrait().apply(c, a.date, a.sha256, a.only),
    "metric add": lambda c, a: pensieve.add_metric(
        c, a.desk, a.run_id, a.model, a.input_tokens, a.output_tokens, a.cache_read_tokens,
        a.cost_usd, a.duration_ms, a.ts),
    "metric summary": lambda c, a: pensieve.summary(c, a.since),
    "purge": lambda c, a: owlery.purge(c, body_days=a.body_days, extract_days=a.extract_days),
    "audit": lambda c, a: owlery.audit(c, escalate=a.escalate),
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


def _desk_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "desk")
    add = _sub(group, "add", "desk add")
    add.add_argument("name")
    add.add_argument("--family", required=True)
    add.add_argument("--role")
    add.add_argument("--model")
    _sub(group, "list", "desk list")
    bump = _sub(group, "cap", "desk cap")
    bump.add_argument("desk")
    amount = bump.add_mutually_exclusive_group(required=True)
    amount.add_argument("--runs", type=_plus_whole)
    amount.add_argument("--spend", type=_plus_amount)
    _sub(group, "caps", "desk caps")
    _sub(group, "many-tasks", "desk many-tasks").add_argument("desk")
    _desk_model_parsers(group)


def _desk_model_parsers(group: argparse._SubParsersAction) -> None:
    model = _sub(group, "model", "desk model")
    model.add_argument("desk")
    choice = model.add_mutually_exclusive_group(required=True)
    choice.add_argument("value", nargs="?", help="pin to this alias, full claude id or catalog slug")
    choice.add_argument("--role", action="store_true", help="unpin, so the role picks again")
    choice.add_argument("--approve", action="store_true", help="switch to the pending pick")
    _sub(group, "models", "desk models")


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
    for name in ("start", "show", "allow-round", "rounds"):
        _sub(group, name, f"task {name}").add_argument("task")
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
    which.add_argument("--status")
    which.add_argument("--open", action="store_true", help="queued, active or awaiting close")
    _sub(group, "board", "task board").add_argument("--desk")


def _followup_parsers(commands: argparse._SubParsersAction) -> None:
    """PR follow-ups, read only: there is no command that changes one."""
    group = _group(commands, "followup")
    _sub(group, "list", "followup list").add_argument("--task")
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
    inbox.add_argument("--all", action="store_true")
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
    _sub(group, "ack", "event ack").add_argument("event", type=_whole)


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
    _sub(group, "decay", "fact decay").add_argument("--archive", action="store_true")
    _sub(group, "archive", "fact archive").add_argument("facts", type=_whole, nargs="+")
    listing = _sub(group, "list", "fact list")
    view = listing.add_mutually_exclusive_group()
    view.add_argument("--scope")
    view.add_argument("--context")
    listing.add_argument("--archived", action="store_true")
    listing.add_argument("--history", action="store_true")
    _temporal_fact_parsers(group)


def _temporal_fact_parsers(group: argparse._SubParsersAction) -> None:
    supersede = _sub(group, "supersede", "fact supersede")
    _new_fact_options(supersede, "aging")
    supersede.add_argument("--subject-key", required=True)
    withdraw = _sub(group, "withdraw", "fact withdraw")
    withdraw.add_argument("fact", type=_whole)
    withdraw.add_argument("--desk")
    _sub(group, "expire", "fact expire")
    _sub(group, "current", "fact current").add_argument("--scope")
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
    history = _sub(group, "history", "fact history")
    history.add_argument("--scope", required=True)
    history.add_argument("--subject-key", required=True)
    candidates = _sub(group, "candidates", "fact candidates")
    candidates.add_argument("--since", type=_whole, required=True)
    candidates.add_argument("--limit-per-fact", type=_whole, default=3)
    apply = _sub(group, "apply", "fact apply")
    apply.add_argument("--file", required=True)
    apply.add_argument("--sha256")


def _portrait_parsers(commands: argparse._SubParsersAction) -> None:
    group = _group(commands, "portrait")
    _sub(group, "patches", "portrait patches")
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
    _sub(group, "summary", "metric summary").add_argument("--since", type=_whole, default=0)


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
    _sub(commands, "audit", "audit").add_argument("--escalate", action="store_true")
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
    _emit(sys.stdout, {"ok": code == 0, "data": data})
    return code
