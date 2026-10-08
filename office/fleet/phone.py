"""Phone delivery: the loud headmaster events reach Ryan off the machine, one ping per event.

Every headmaster event is a row in the store, written by many processes, some inside a transaction of their own. So
nothing pings from inside the writer: deliver reads the store after it is written, at the end of each Owl Post pass
and each orchestrator run, and sends every new event whose kind is in config.PHONE_KINDS, oldest first.

- Transports: the command transport is primary when config.PHONE_COMMAND names one (set only in the private
  overlay, never in the kit). It starts that absolute argv with an empty-ish environment and writes one JSON object
  to its stdin: {"event_id", "kind", "task_id", "line", "pr_link"}. Exit 0 within PHONE_COMMAND_TIMEOUT_SECONDS is
  delivered. No secret, URL or text is ever in its argv; the command keeps its own credentials. A command that is not
  configured, fails or times out falls back to the macOS notification (run_desk.notify_desktop, osascript).
- One event, one ping: before anything is sent, a marker ev-<id> is published create-exclusive in the office phone
  folder, so a second deliverer, a rerun or a kill never pings it again. The marker then records the outcome: via
  (command, macos or none) and how the primary went (ok, failed or unconfigured).
- The first deliver ever only records the newest event id, so switching this on sends no backlog. At most
  PHONE_MAX_PER_PASS events ping one by one each pass; any more loud events in that pass go as one summary ping.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from hogwarts import db

from fleet import common, config, markers, run_desk, safefs
from fleet.safefs import FleetError

PHONE_DIR = "phone"
WATERMARK = "watermark"
LOCK = "phone.lock"
EVENT_MARKER = re.compile(r"ev-([1-9][0-9]{0,18})")
LINE_MAX = 200
SCAN_LIMIT = 500
# Markers this many events behind the watermark are pruned; they can never be scanned again.
KEEP_BEHIND = 2000
PR_LINK = re.compile(r"https://github\.com/[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}/pull/[0-9]{1,10}")


class MacTransport:
    """A macOS notification through the fleet's one osascript path. False off macOS or with notifications off."""
    name = "macos"

    def send(self, payload: dict) -> bool:
        title = f"Hogwarts: {payload['kind']} {payload['task_id'] or ''}".strip()
        text = payload["line"] if payload["pr_link"] is None else f"{payload['line']} {payload['pr_link']}"
        return run_desk.notify_desktop(text, title) is True


class CommandTransport:
    """The overlay's command: an absolute argv, the payload as JSON on stdin, exit 0 means delivered."""
    name = "command"

    def __init__(self, argv: object) -> None:
        if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(part, str) and part and "\x00" not in
                                                                       part for part in argv):
            raise ValueError("the phone command must be a non-empty list of text arguments")
        if not argv[0].startswith("/"):
            raise ValueError("the phone command must name its program by absolute path")
        self.argv = list(argv)

    def send(self, payload: dict) -> bool:
        data = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
        try:
            done = subprocess.run(self.argv, input=data, env=run_desk.child_env(), cwd="/", stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=config.PHONE_COMMAND_TIMEOUT_SECONDS,
                                  check=False, close_fds=True)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return done.returncode == 0


def primary() -> Optional[CommandTransport]:
    """The command transport when the overlay configured one that is well formed, else None."""
    if config.PHONE_COMMAND is None:
        return None
    try:
        return CommandTransport(config.PHONE_COMMAND)
    except ValueError:
        return None


def payload(event: dict) -> dict:
    """What a ping carries: the event's id, kind and task, its summary scrubbed to one line, and a PR link if any."""
    line = common.scrubbed_line(event["summary"], LINE_MAX)
    link = PR_LINK.search(common.untrusted_text(event["summary"] or ""))  # from the scrubbed text, so whole or none
    return {"event_id": int(event["id"]), "kind": event["kind"], "task_id": event["task_id"], "line": line,
            "pr_link": None if link is None else link.group(0)}


def send(data: dict) -> dict:
    """Send one payload: the primary first, then macOS when it is missing or fails. Returns the outcome."""
    command = primary()
    if command is not None:
        if command.send(data):
            return {"state": "sent", "via": command.name, "primary": "ok"}
        how = "failed"
    else:
        how = "unconfigured"
    if MacTransport().send(data):
        return {"state": "sent", "via": "macos", "primary": how}
    return {"state": "undelivered", "via": "none", "primary": how}


def _watermark(fd: int) -> Optional[int]:
    """The last event id looked at, None before the first deliver, or FleetError when it cannot be read: then nothing
    is sent and nothing is skipped until Ryan looks at the phone folder."""
    marker = markers.read(fd, WATERMARK)
    if marker is None:
        return None
    value = marker.get("id")
    if marker.get("state") != "at" or type(value) is not int or value < 0:
        raise FleetError("the phone watermark cannot be read, so no ping was sent")
    return value


def _prune(fd: int, below: int) -> None:
    for name in os.listdir(fd):
        match = EVENT_MARKER.fullmatch(name)
        if match is not None and int(match.group(1)) < below:
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass


def deliver(conn) -> list:
    """Ping every loud headmaster event written since the last deliver (see the module notes). Returns one outcome
    per event looked at. Never raises for a failed ping: its marker says so."""
    outcomes = []
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd:
        try:
            with safefs.held_lock(locks_fd, LOCK, blocking=False), \
                    safefs.opened_dir(config.OFFICE_ROOT, PHONE_DIR, create=True) as fd:
                outcomes = _deliver(conn, fd)
        except safefs.Busy:
            return ["another deliver is running"]
    return outcomes


def _deliver(conn, fd: int) -> list:
    mark = _watermark(fd)
    if mark is None:  # the first deliver: no backlog
        newest = db.fetch_one(conn, "SELECT COALESCE(MAX(id), 0) AS id FROM events")["id"]
        markers.replace(fd, WATERMARK, {"state": "at", "id": int(newest)})
        return []
    rows = db.fetch_all(conn, "SELECT id, kind, task_id, summary FROM events WHERE id > ? AND verdict = 'headmaster'"
                              " ORDER BY id LIMIT ?", (mark, SCAN_LIMIT))
    loud = [row for row in rows if row["kind"] in config.PHONE_KINDS]
    outcomes, batched = [], []
    for row in loud:
        name = f"ev-{int(row['id'])}"
        if not markers.publish(fd, name, {"state": "sending"}):
            continue  # another deliver, or this one before a kill, took it: never twice
        if len(outcomes) >= config.PHONE_MAX_PER_PASS:
            batched.append(name)
            continue
        outcome = send(payload(row))
        markers.replace(fd, name, outcome)
        outcomes.append(outcome["state"])
    if batched:
        summary = {"event_id": 0, "kind": "phone.batch", "task_id": None, "pr_link": None,
                   "line": f"{len(batched)} more loud events are waiting; see the headmaster events in the castle"}
        outcome = send(summary)
        for name in batched:
            markers.replace(fd, name, {**outcome, "batched": True})
        outcomes.append(f"batched {len(batched)}")
    if rows:
        newest = int(rows[-1]["id"])
        markers.replace(fd, WATERMARK, {"state": "at", "id": newest})
        _prune(fd, newest - KEEP_BEHIND)
    return outcomes
