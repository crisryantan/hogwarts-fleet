"""Which of McGonagall's sessions watches each open go task, so that with several of her sessions open, each one's chat
watch, go status and go-changed blocks cover only the go tasks typed there.

- A go task belongs to the session whose prompt typed its go: her prompt hook claims each id of a go (claim) before
  the go runs, unless the store knows that task already, since that go is then refused and the task keeps its owner.
  Several gos in one message are all that session's.
- Kept in the office folder config.GO_CHAT_DIR, beside the chat watch's markers (fleet/hooks/stop.py):
  OWNERS, {"state": "owners", "tasks": {go task id: [session name, unix time]}}, replaced whole under OWNERS_LOCK, and
  one SESSION marker per session, {"state": "session", "pid": its Claude Code process, "at": unix time}, which her
  SessionStart and prompt hooks write (register), with the hook's parent process as the pid. A session name is
  name(session id), a digest: the raw id is never kept, and no line a model reads names a session at all.
- A session watches the open go tasks it owns, and any no session owns yet (one typed before owners were kept), as
  every session did before (watching). With one session that is every go task, as before.
- A session is gone once its SESSION marker is missing or its process has ended. A session that starts in a process
  another session used (a clear) ends that one, since one process runs one session.
- Handover: her SessionStart takes over each open go task whose owner is gone or that no one owns, and the digest says
  so once (start; a notice whose output never went out is said by her next prompt, owed and said). Her prompt hook takes over only those no one owns, silently (prompt). Ryan's whole message
  "watch <go task id>" in her session moves that one open go task to that session (move), and nothing else.
- A reader that cannot read OWNERS treats every go task as no one's, the old behaviour, and the next write starts it
  again. An open go task always keeps its owner; one not open (a go still being confirmed, or one closed or moved)
  keeps it for a week, at most MAX_OWNERS of them. Nothing here writes the store.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from typing import Callable, Iterable, Optional

from hogwarts import ids, pensieve
from hogwarts.errors import NotFoundError, StoreError

from fleet import config, go_status, markers, safefs
from fleet.safefs import FleetError

OWNERS = "owners"
# A dot name: the chat watch's cleanup never removes it, so every writer locks the same file.
OWNERS_LOCK = ".owners.lock"
SESSION = ".session"
LOCK_SECONDS = 5
HANDOVER = ".handover"
# Go tasks no longer open keep their owner for config.EVENTS_SEEN_KEEP_SECONDS, as long as a session's told marker
# can still name them, at most this many, the newest, so OWNERS stays well under markers.MAX_BYTES. An open go task
# always keeps its owner.
MAX_OWNERS = 48
NAME = re.compile(r"[0-9a-f]{32}")
ERRORS = (StoreError, sqlite3.Error, FleetError, OSError)


def name(session_id: str) -> str:
    """The digest a session is kept under. The raw session id is never written."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def session_pid() -> int:
    """The session's Claude Code process: a hook's parent."""
    return os.getppid()


# Reading


def _read(fd: int) -> dict:
    """{go task id: (session name, unix time)}, {} when there is none or it cannot be read."""
    marker = markers.read(fd, OWNERS)
    tasks = None if marker is None else marker.get("tasks")
    if not isinstance(tasks, dict):
        return {}
    found = {}
    for go_id, entry in tasks.items():
        if isinstance(go_id, str) and ids.PATTERNS["task"].fullmatch(go_id) and isinstance(entry, list) \
                and len(entry) == 2 and isinstance(entry[0], str) and NAME.fullmatch(entry[0]) \
                and type(entry[1]) is int:
            found[go_id] = (entry[0], entry[1])
    return found


def owners(fd: int) -> dict:
    """{go task id: session name} from the folder fd."""
    return {go_id: entry[0] for go_id, entry in _read(fd).items()}


def watcher(owned: dict, me: str) -> Callable[[str], bool]:
    """Whether the session named me watches a go task: it owns it, or no session does."""
    return lambda go_id: owned.get(go_id) in (None, me)


def watching(session_id: Optional[str]) -> Callable[[str], bool]:
    """For her hooks: which go tasks this session watches. Every one with no session id or no owners to read."""
    if session_id is None:
        return lambda go_id: True
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
            owned = owners(fd)
    except (FleetError, OSError):
        owned = {}
    return watcher(owned, name(session_id))


def alive(fd: int, session: str) -> bool:
    marker = markers.read(fd, session + SESSION)
    return marker is not None and marker.get("state") == "session" and not markers.gone(marker)


# Writing, each under OWNERS_LOCK


def _open_ids(conn) -> list:
    """Her open go tasks' ids, oldest first."""
    return [item["task"]["id"] for item in reversed(go_status.open_go_tasks(conn))]


def _write(fd: int, owned: dict, open_ids: Iterable[str]) -> None:
    now, still = int(time.time()), set(open_ids)
    done = sorted(((at, go_id) for go_id, (_, at) in owned.items()
                   if go_id not in still and now - at <= config.EVENTS_SEEN_KEEP_SECONDS), reverse=True)[:MAX_OWNERS]
    kept = still | {go_id for _, go_id in done}
    markers.replace(fd, OWNERS, {"state": "owners", "tasks": {go_id: [session, at] for go_id, (session, at)
                                                              in owned.items() if go_id in kept}})


def _locked(fd: int):
    return safefs.held_lock(fd, OWNERS_LOCK, blocking=True, timeout=LOCK_SECONDS)


def register(fd: int, session_id: str, pid: int) -> None:
    """Mark this session as running in process pid, and end any other session that ran in that process."""
    me = name(session_id)
    for entry in os.listdir(fd):
        if not entry.endswith(SESSION) or entry.startswith(".") or entry == me + SESSION:
            continue
        marker = markers.read(fd, entry)
        if marker is not None and marker.get("pid") == pid:
            try:
                os.unlink(entry, dir_fd=fd)
            except FileNotFoundError:
                pass
    markers.replace(fd, me + SESSION, {"state": "session", "pid": pid, "at": int(time.time())})


def _adopt(fd: int, me: str, open_ids: list, gone_too: bool) -> list:
    """The open go tasks this session takes over: those no one owns, and with gone_too those whose owner is gone."""
    owned, now, taken, lives = _read(fd), int(time.time()), [], {}
    for go_id in open_ids:
        owner = owned.get(go_id)
        if owner is not None and (owner[0] == me or not gone_too):
            continue
        if owner is not None:
            if owner[0] not in lives:
                lives[owner[0]] = alive(fd, owner[0])
            if lives[owner[0]]:
                continue
        owned[go_id] = (me, now)
        taken.append(go_id)
    if taken:
        _write(fd, owned, open_ids)
    return taken


def start(conn, session_id: Optional[str], pid: Optional[int] = None) -> list:
    """Her SessionStart: register this session and take over every open go task whose owner is gone or that no one
    owns, under OWNERS_LOCK, so two starts never take the same one. The ids to say, oldest first: those taken now and
    any a hook of this session took but never said, kept in its HANDOVER marker until said clears it. Never raises."""
    if session_id is None:
        return []
    try:
        open_ids = _open_ids(conn)
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd, _locked(fd):
            register(fd, session_id, session_pid() if pid is None else pid)
            me = name(session_id)
            taken = _adopt(fd, me, open_ids, True)
            owed = list(dict.fromkeys(_owed(fd, me) + taken))
            if taken:
                markers.replace(fd, me + HANDOVER, {"state": "handover", "tasks": owed})
            return owed
    except ERRORS:
        return []  # every go task stays watched where it was, or by every session when no one owns it


def _owed(fd: int, me: str) -> list:
    marker = markers.read(fd, me + HANDOVER)
    tasks = None if marker is None else marker.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [go_id for go_id in tasks if isinstance(go_id, str) and ids.PATTERNS["task"].fullmatch(go_id)]


def handover_line(taken: list) -> list:
    """The one line saying which go tasks this session took over at its start, if any, said once."""
    if not taken:
        return []
    named = ", ".join(taken[:config.GO_STATUS_CAP])
    more = f" and {len(taken) - config.GO_STATUS_CAP} more" if len(taken) > config.GO_STATUS_CAP else ""
    return [f"Chat watch: this session took over {len(taken)} open go tasks whose session ended or that no session"
            f" watched: {named}{more}. Their changes wake this session now."]


def owed(session_id: Optional[str]) -> list:
    """The go tasks this session took over at a start whose output never went out, to say now. Never raises."""
    if session_id is None:
        return []
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
            return _owed(fd, name(session_id))
    except (FleetError, OSError):
        return []


def said(session_id: Optional[str]) -> None:
    """The handover was said, once its output is written: clear what this session owed. Never raises."""
    if session_id is None:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR) as fd:
            os.unlink(name(session_id) + HANDOVER, dir_fd=fd)
    except (FleetError, OSError):
        pass


def prompt(conn, session_id: Optional[str], pid: Optional[int] = None) -> None:
    """Her prompt hook: register this session, and take over the open go tasks no one owns. Never raises."""
    if session_id is None:
        return
    try:
        open_ids = _open_ids(conn)
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd, _locked(fd):
            register(fd, session_id, session_pid() if pid is None else pid)
            _adopt(fd, name(session_id), open_ids, False)
    except ERRORS:
        pass


def claim(conn, session_id: Optional[str], task_ids: Iterable[str]) -> None:
    """A go typed in her session: each of its go tasks the store does not know yet is this session's, checked under
    OWNERS_LOCK, so a go another session applied meanwhile (applied, after its commit) keeps that session. Never
    raises."""
    if session_id is None:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd, _locked(fd):
            new = []
            for task_id in task_ids:
                try:
                    pensieve.get_task(conn, task_id)
                except NotFoundError:
                    new.append(task_id)
            if not new:
                return
            owned, now = _read(fd), int(time.time())
            owned.update({task_id: (name(session_id), now) for task_id in new})
            _write(fd, owned, _open_ids(conn))
    except ERRORS:
        pass  # no owner: every session watches it, as before, until a session takes it over


def applied(conn, session_id: Optional[str], task_id: str) -> None:
    """A go that committed: its go task is the typing session's, whatever a claim from another session said while it
    was being confirmed. Never raises."""
    if session_id is None:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd, _locked(fd):
            owned = _read(fd)
            owned[task_id] = (name(session_id), int(time.time()))
            _write(fd, owned, _open_ids(conn))
    except ERRORS:
        pass


def move(conn, session_id: str, go_id: str) -> str:
    """Ryan's "watch <go task id>": moves that open go task to this session. "moved", "already" or "not open". Raises
    when the owners cannot be written."""
    open_ids = _open_ids(conn)
    if go_id not in open_ids:
        return "not open"
    me = name(session_id)
    with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CHAT_DIR, create=True) as fd, _locked(fd):
        owned = _read(fd)
        if owned.get(go_id, (None,))[0] == me:
            return "already"
        owned[go_id] = (me, int(time.time()))
        _write(fd, owned, open_ids)
    return "moved"
