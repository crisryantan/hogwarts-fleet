"""fleet go-wait: wait, read only, until one go task changes where it stands, then print one line and exit, so a
caller that runs it again with the key it printed hears each change of that go task once, until it closes.

  fleet go-wait <go-task-id> [--since <key>]

- Where it stands is go_watch's state key for that go task (go_watch._go_state, the reducer go updates use, run on
  that go task alone), so only a real change ends a wait: no timestamp or wording is in the key. --since is the short
  digest of a key (KEY) that an earlier wait printed. With no --since it prints where the go task stands now and
  exits at once.
- It opens the store with db.connect_readonly, reads it every config.GO_WAIT_POLL_SECONDS, and exits as soon as the
  key differs from --since, or after config.GO_WAIT_MAX_SECONDS with "still", so a session is never stuck on it.
  A read that fails part way is tried again at the next poll, and "still" needs one more whole read at the deadline:
  a wait that cannot read the store then is an error, never a stale "still".
- A go task that is not open with a recorded go spec yet (its go is still being confirmed, or was refused before it
  was registered) stands as pending. One that closed prints closed, and the watch ends there.
- One line, from fixed text only (go_watch.line, or PENDING here), scrubbed and cut to config.GO_WATCH_LINE_CHARS
  before the key is added:
    now|changed|still <go id> / <build id or "no build">: <state>. <action> Next: --since <key>
    closed <go id> / <build id or "no build">: closed (<reason>). Nothing for you. The watch ends.
  Exit 0 for each of those, 1 with "error: ..." for a bad argument or a store it cannot open at all.
- It never writes the store or the office, never starts a desk and never pings.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from typing import Callable, Optional

from hogwarts import db, ids, pensieve
from hogwarts.errors import NotFoundError, StoreError

from fleet import common, config, go_status, go_watch
from fleet.safefs import FleetError

KEY = re.compile(r"[0-9a-f]{16}")
PENDING = ("go not applied yet", "Nothing for you; the go's result shows here once it lands.")
READ_ERRORS = (StoreError, sqlite3.Error, FleetError, OSError)


def digest(key: dict) -> str:
    """The short, shell-safe form of a state key that --since takes."""
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode("ascii")).hexdigest()[:16]


def _newest_build(conn, go_id: str) -> Optional[str]:
    builds = [task["id"] for task in pensieve.list_tasks(conn)
              if task["parent_task_id"] == go_id and task["desk"] in config.WORKTREE_DESKS]
    return builds[-1] if builds else None


def _open_go_tasks(conn) -> list:
    """go_status.open_go_tasks, strict about Ollivander's held runs, as go updates are: a held run marker that cannot be
    read names no task, so no build can be told apart from held then, and the read fails rather than say it is not."""
    return go_status.open_go_tasks(conn, strict=True)


def standing(conn, go_id: str) -> tuple:
    """(kind, key, text) for the go task now, read in one store snapshot: kind is open, pending or closed, key
    go_watch's state key for it (or a pending or closed one built the same way), text its line without the key. Only
    this go task is reduced, so another go task's files never stand in its way. Raises when it cannot be read whole."""
    with db.snapshot(conn):
        try:
            task = pensieve.get_task(conn, go_id)
        except NotFoundError:
            task = None
        if task is not None and task["status"] == "closed":
            key = {"build": _newest_build(conn, go_id), "state": "closed", "round": 0, "verdict": None, "event": None}
            text = f"{go_id} / {key['build'] or 'no build'}: closed ({task['close_reason']}). Nothing for you."
            return "closed", key, common.scrubbed_line(text, config.GO_WATCH_LINE_CHARS)
        item = None if task is None else next((item for item in _open_go_tasks(conn) if item["task"]["id"] == go_id),
                                              None)
        if item is not None:
            key = go_watch._go_state(conn, item)[0]
            return "open", key, go_watch.line(go_id, key)
    key = {"build": None, "state": "pending", "round": 0, "verdict": None, "event": None}
    text = f"{go_id} / no build: {PENDING[0]}. {PENDING[1]}"
    return "pending", key, common.scrubbed_line(text, config.GO_WATCH_LINE_CHARS)


def open_states(conn) -> dict:
    """{go task id: (key, text)} for every open go task, oldest first, read in one store snapshot, for the chat watch
    (fleet/hooks/stop.py). A go task whose own rows or files cannot be read maps to None, so it never stands in the
    others' way. Raises when the open go tasks themselves cannot be read."""
    with db.snapshot(conn):
        found = {}
        for item in reversed(_open_go_tasks(conn)):
            go_id = item["task"]["id"]
            try:
                key = go_watch._go_state(conn, item)[0]
            except READ_ERRORS:
                found[go_id] = None
                continue
            found[go_id] = (key, go_watch.line(go_id, key))
        return found


def _said(word: str, kind: str, key: dict, text: str) -> str:
    if kind == "closed":
        return f"closed {text} The watch ends."
    return f"{word} {text} Next: --since {digest(key)}"


def wait(conn, go_id: str, since: Optional[str], clock: Callable = time.monotonic, sleep: Callable = time.sleep,
         max_seconds: Optional[float] = None) -> str:
    """The one line to print (see the module notes). Reads only, through conn."""
    limit = config.GO_WAIT_MAX_SECONDS if max_seconds is None else max_seconds
    kind, key, text = standing(conn, go_id)
    if since is None or kind == "closed" or digest(key) != since:
        return _said("now" if since is None else "changed", kind, key, text)
    ends = clock() + limit
    while clock() < ends:
        sleep(max(0.0, min(config.GO_WAIT_POLL_SECONDS, ends - clock())))
        try:
            kind, key, text = standing(conn, go_id)
        except READ_ERRORS:
            continue  # a store mid-write or a folder mid-move reads again next poll
        if kind == "closed" or digest(key) != since:
            return _said("changed", kind, key, text)
    # The deadline: one last whole read stands for "still", so a wait whose reads kept failing never says it.
    kind, key, text = standing(conn, go_id)
    return _said("still" if kind != "closed" and digest(key) == since else "changed", kind, key, text)


def main(go_id: str, since: Optional[str], out=None, clock: Callable = time.monotonic,
         sleep: Callable = time.sleep) -> int:
    """fleet go-wait's entry, with its arguments as fleet's parser read them. Tests pass a clock and a sleep."""
    out = sys.stdout if out is None else out
    if ids.PATTERNS["task"].fullmatch(go_id) is None:
        out.write("error: not a task id\n")
        return 1
    if since is not None and KEY.fullmatch(since) is None:
        out.write("error: --since takes the key an earlier fleet go-wait printed\n")
        return 1
    try:
        conn = db.connect_readonly(config.DB_PATH)
    except READ_ERRORS as exc:
        out.write(f"error: the store cannot be read: {common.one_line(exc, 200)}\n")
        return 1
    try:
        line = wait(conn, go_id, since, clock=clock, sleep=sleep)
    except READ_ERRORS as exc:
        out.write(f"error: the store cannot be read: {common.one_line(exc, 200)}\n")
        return 1
    finally:
        conn.close()
    out.write(line + "\n")
    out.flush()
    return 0
