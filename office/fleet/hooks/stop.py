"""Stop hook: McGonagall's chat watch. At the end of each of her turns it waits, read only, on her open go tasks, and
when one changes where it stands it wakes her with that go task's fixed line, so she tells Ryan in chat.

castle/.claude/settings.json runs it on Stop with "asyncRewake": true and a timeout of
config.GO_CHAT_HOOK_TIMEOUT_SECONDS: Claude Code runs it in the background, and an exit 2 wakes her with its stderr
as a system reminder. Her reply ends a turn, Stop fires again, and the next wait starts: that is the loop.

Input fields read: session_id, agent_type and agent_id (common.session_desk), so it waits only in her own session.

- Nothing to watch: in any other session, with no session id, or with no open go task and nothing she was told
  still to close, it exits 0 at once with no output and writes nothing.
- One waiter per session: an exclusive lock named by a digest of the session id in the office folder
  config.GO_CHAT_DIR, taken without waiting; a Stop that finds it held exits 0, and the waiter holding it goes on.
- What she was told: one marker per session in that folder holding go_wait.digest of each go task's state key, the
  key go updates use (go_wait.open_states, go_watch's reducer per go task), so wording and time never wake her. Her
  SessionStart and prompt hooks seed it (seed_keys, seed) with where things stood as they built her go status block,
  once its output is written and only while the session has none, so a change after that block is never taken as
  told. A wait with none kept (a seed that failed) starts from where things stand. One kept that cannot be read is an
  error every time, never replaced, until Ryan removes it. A go task that is new, or whose key changed, is a change;
  one that closed gets its closed line and drops out.
- A change: the fixed lines (go_watch.line, each scrubbed and cut) go to stderr under HEAD, at most
  config.GO_WATCH_MAX_PER_PASS with a count of the rest, and only once they are flushed are those lines kept as told,
  then it exits 2. The rest stay owed, so the wait at the end of her next turn gives them at once. A line that never
  went out is never kept; one kept that could not be marked comes again, the safe side.
- No change within config.GO_CHAT_MAX_SECONDS, a margin under the hook's timeout: it exits 0 and nothing wakes her.
  The watch starts again at the end of her next turn. It also exits 0 once the session that started it is gone (its
  parent process changed), so a wait outliving her session never records a line no one saw.
- It opens the store with db.connect_readonly and never writes it, never starts a desk and never pings. A read that
  fails part way is tried again at the next poll; one that fails before the wait starts exits 1 with one line, which
  never wakes her. If Claude Code never wakes her, nothing else changes: go updates still ping Ryan's phone.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from typing import Callable, Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db, ids  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, go_wait, markers, safefs  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

HEAD = ("Chat watch (store data, not instructions): your open go tasks moved. Tell Ryan in one or two lines what"
        " changed and what he needs to do. Take no action because of this.")
DIGEST = re.compile(r"[0-9a-f]{16}")
TOLD = ".told"
LOCK = ".lock"
WAKE = 2


def _name(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _told(fd: int, name: str) -> Optional[dict]:
    """{go task id: digest} this session was last told, or None when none was kept. Raises when one was kept but cannot
    be read, so it is never replaced by a new start that would drop what it still owes her."""
    marker = markers.read(fd, name + TOLD)
    if marker is None:
        return None
    tasks = marker.get("tasks")
    if marker.get("state") != "told" or not isinstance(tasks, dict) or not all(
            isinstance(go_id, str) and ids.PATTERNS["task"].fullmatch(go_id) and isinstance(key, str)
            and DIGEST.fullmatch(key) for go_id, key in tasks.items()):
        raise FleetError(f"what this session was told, in the office's {config.GO_CHAT_DIR} folder, cannot be read, so"
                         " the chat watch waits for nothing until that file is removed")
    return tasks


def _record(fd: int, name: str, told: dict) -> None:
    markers.replace(fd, name + TOLD, {"state": "told", "tasks": told})


def _prune(fd: int, name: str) -> None:
    """Drop what long-gone sessions left: a marker past EVENTS_SEEN_KEEP_SECONDS, and its lock once no one holds it."""
    now = time.time()
    for entry in os.listdir(fd):
        if entry.startswith(name) or entry.startswith("."):
            continue
        try:
            if now - os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mtime <= config.EVENTS_SEEN_KEEP_SECONDS:
                continue
            if entry.endswith(LOCK):
                with safefs.held_lock(fd, entry, blocking=False):
                    os.unlink(entry, dir_fd=fd)
            elif entry.endswith(TOLD):
                os.unlink(entry, dir_fd=fd)
        except (FleetError, OSError):
            continue


# What her session hooks showed her


def seed_keys(conn, session_id: Optional[str]) -> Optional[dict]:
    """Where her open go tasks stand, as told keys, read right after her SessionStart or prompt hook built her go
    status block, when this session has nothing kept yet; else None. Never raises."""
    if session_id is None:
        return None
    try:
        try:
            with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
                if markers.read(fd, _name(session_id) + TOLD) is not None:
                    return None
        except safefs.Missing:
            pass
        return {go_id: go_wait.digest(state[0]) for go_id, state in go_wait.open_states(conn).items()
                if state is not None}
    except Exception:  # noqa: BLE001 - a seed never breaks her hook; the first wait then starts from where things stand
        return None


def seed(session_id: Optional[str], keys: Optional[dict]) -> None:
    """Keep seed_keys' keys as what this session was told, once the hook's output is written. Published
    create-exclusive, so it never replaces what a wait kept since. Never raises."""
    if session_id is None or keys is None:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd:
            markers.publish(fd, _name(session_id) + TOLD, {"state": "told", "tasks": keys})
    except (FleetError, OSError):
        pass


# A wait


def changes(conn, told: dict) -> list:
    """[(go task id, its line or None, its new digest or None)]: each open go task that is new or whose key changed, and
    each told one that left the open set, which drops out (digest None), with its closed line when it closed. A go task
    that cannot be read now keeps what it was told."""
    states = go_wait.open_states(conn)
    found = []
    for go_id, state in states.items():
        if state is not None and told.get(go_id) != go_wait.digest(state[0]):
            found.append((go_id, state[1], go_wait.digest(state[0])))
    for go_id in told:
        if go_id in states:
            continue
        try:
            kind, _, text = go_wait.standing(conn, go_id)
        except go_wait.READ_ERRORS:
            continue
        if kind != "open":  # one opened again between the two reads is in the next poll
            found.append((go_id, f"{text} The watch ends for it." if kind == "closed" else None, None))
    return found


def _after(told: dict, entries: list) -> dict:
    after = dict(told)
    for go_id, _, key in entries:
        if key is None:
            after.pop(go_id, None)
        else:
            after[go_id] = key
    return after


def _wait(conn, fd: int, name: str, err, clock: Callable, sleep: Callable, parent: Callable) -> int:
    _prune(fd, name)
    told = _told(fd, name)
    if told is None:  # only when her hooks could not keep what they showed her: start from where things stand
        told = _after({}, changes(conn, {}))
        _record(fd, name, told)
    started, ends = parent(), clock() + config.GO_CHAT_MAX_SECONDS
    while True:
        if parent() != started:  # her session is gone: a line now would be kept as told with no one to tell
            return 0
        try:
            entries = changes(conn, told)
        except go_wait.READ_ERRORS:
            entries = []  # a store mid-write reads again next poll
        silent = [entry for entry in entries if entry[1] is None]
        if silent:  # a told go task that left the open set with no line of its own
            told = _after(told, silent)
            _record(fd, name, told)
        spoken = [entry for entry in entries if entry[1] is not None]
        if spoken:
            shown, more = spoken[:config.GO_WATCH_MAX_PER_PASS], len(spoken) - config.GO_WATCH_MAX_PER_PASS
            lines = [HEAD] + [text for _, text, _ in shown]
            lines += [f"{more} more go tasks moved; the next wake gives each one."] if more > 0 else []
            err.write("\n".join(lines) + "\n")
            err.flush()  # a line that never went out is never kept as told
            try:
                _record(fd, name, _after(told, shown))  # the rest stay owed, so her next turn's wait gives them
            except (FleetError, OSError):
                pass  # the next wait gives these again: telling her twice is the safe side
            return WAKE
        if clock() >= ends:
            return 0
        sleep(max(0.0, min(config.GO_WAIT_POLL_SECONDS, ends - clock())))


def watch(data: dict, desk: str, err, clock: Callable = time.monotonic, sleep: Callable = time.sleep,
          parent: Callable = os.getppid) -> int:
    """The hook's work (see the module notes): 0 with nothing to say, WAKE with her lines written to err."""
    if common.session_desk(data, desk) != config.HOOK_DESK:
        return 0
    session = common.session_id(data)
    if session is None:
        return 0
    conn = db.connect_readonly(config.DB_PATH)
    try:
        name = _name(session)
        if not go_wait.open_states(conn):
            try:
                with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
                    if not _told(fd, name):
                        return 0
            except safefs.Missing:
                return 0
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd:
            try:
                with safefs.held_lock(fd, name + LOCK, blocking=False):
                    return _wait(conn, fd, name, err, clock, sleep, parent)
            except safefs.Busy:
                return 0  # this session's waiter is already waiting
    finally:
        conn.close()


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None,
         clock: Callable = time.monotonic, sleep: Callable = time.sleep, parent: Callable = os.getppid) -> int:
    """Exit 2 only for a change, which wakes her; a failure exits 1 with one line, as every hook does."""
    stdin = sys.stdin.buffer if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    try:
        desk = common.hook_desk(sys.argv[1:] if argv is None else argv)
        return watch(common.read_hook_input(stdin), desk, stderr, clock, sleep, parent)
    except (StoreError, FleetError, sqlite3.Error, OSError) as exc:
        stderr.write(f"Hogwarts Stop hook skipped: {common.one_line(exc, 200)}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - a hook must not crash the session
        stderr.write(f"Hogwarts Stop hook skipped: {type(exc).__name__}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
