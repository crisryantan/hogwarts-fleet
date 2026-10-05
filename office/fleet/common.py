"""Helpers shared by the fleet scripts and hooks: store access, hook input, exit codes."""
from __future__ import annotations

import contextlib
import json
import re
import signal
import sys
import threading
import time
from typing import Callable, Iterator, Optional, Sequence

from hogwarts import db, ids, pensieve
from hogwarts.errors import StoreError

from . import config
from .safefs import FleetError

_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def connect():
    """Open the store. Fleet code never creates the database; castle init does that."""
    return db.connect(config.DB_PATH, create=False)


def now_stamp(now: Optional[int] = None) -> int:
    return int(time.time()) if now is None else now


@contextlib.contextmanager
def ended_by_signals() -> Iterator[None]:
    """SIGTERM or SIGHUP (a closed terminal, a caller's timeout) ends the command through its finally blocks,
    so a review closes its reviewer task and a desk run kills its child, instead of Python dying mid-step."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame) -> None:
        raise SystemExit(128 + signum)

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


@contextlib.contextmanager
def signals_held() -> Iterator[None]:
    """Hold back SIGTERM, SIGHUP and SIGINT for a step that must never be cut in two, such as starting a process and
    keeping its handle, then raise each one that came, once, to the handler that was there before, as the block ends."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    came = []
    previous = {number: signal.signal(number, lambda signum, frame: came.append(signum))
                for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)
        for number in dict.fromkeys(came):
            signal.raise_signal(number)


def _no_constants(name: str) -> None:
    raise ValueError("NaN and Infinity are not allowed")


def _unique_pairs(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("repeated key")
    return dict(pairs)


def strict_json(raw: bytes):
    """UTF-8 JSON with no repeated keys and no NaN or Infinity."""
    text = raw.decode("utf-8")
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_no_constants)


def read_hook_input(stream) -> dict:
    raw = stream.read(config.HOOK_INPUT_MAX_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > config.HOOK_INPUT_MAX_BYTES:
        raise FleetError("hook input is too large")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("hook input is not JSON") from None
    if not isinstance(data, dict):
        raise FleetError("hook input is not a JSON object")
    return data


def text_field(data: dict, key: str, limit: int = 4096) -> Optional[str]:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        return None
    return value


def session_id(data: dict) -> Optional[str]:
    value = text_field(data, "session_id", 80)
    try:
        return None if value is None else ids.check("session", value)
    except StoreError:
        return None


def one_line(text: object, limit: int) -> str:
    """Printable ASCII on one line, cut to limit. Used for desk-written titles and subjects."""
    cleaned = _PRINTABLE.sub(" ", str(text)).strip()
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned if len(cleaned) <= limit else cleaned[: max(limit - 3, 0)] + "..."


def scrubbed_line(text: object, limit: int) -> str:
    """one_line for text that can quote git, gh or a desk: all of it is scrubbed of anything shaped like a credential
    (pensieve.scrub) before it is cut, so a cut never leaves part of one unscrubbed, and again once it is on one line,
    since joining its lines can shape one."""
    return one_line(pensieve.scrub(one_line(pensieve.scrub(str(text)), sys.maxsize)), limit)


def hook_desk(argv: Sequence[str]) -> str:
    """The desk a hook speaks for: --desk NAME for an interactive castle desk, else the default."""
    args = list(argv)
    if not args:
        return config.HOOK_DESK
    if len(args) == 2 and args[0] == "--desk":
        desk = ids.check("desk", args[1])
        if desk in config.INTERACTIVE_DESKS:
            return desk
    raise FleetError("hooks take only --desk NAME for an interactive desk")


def session_desk(data: dict, desk: str) -> str:
    """The desk a castle session belongs to: the hook desk only when Claude Code says it runs that agent.

    agent_type is present on the main thread of a session started with an agent. A session
    without it, or inside a subagent, is Ryan's own.
    """
    if desk != config.HOOK_DESK:
        return desk
    if data.get("agent_type") == config.HOOK_DESK and "agent_id" not in data:
        return desk
    return config.OWN_SESSION_DESK


def run_hook(name: str, body: Callable, argv: Optional[Sequence[str]], stdin, stdout, stderr,
             now: Optional[int]) -> int:
    """Run a hook body. Never exits 2, so a fleet failure never blocks Ryan's prompt."""
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        desk = hook_desk(sys.argv[1:] if argv is None else argv)
        data = read_hook_input(stdin)
        body(data, desk, stdout, now_stamp(now))
    except (StoreError, FleetError) as exc:
        stderr.write(f"Hogwarts {name} hook skipped: {one_line(exc, 200)}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - a hook must not crash the session
        stderr.write(f"Hogwarts {name} hook skipped: {type(exc).__name__}\n")
        return 1
    return 0
