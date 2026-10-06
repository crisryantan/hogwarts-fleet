"""Small JSON marker files in office folders, shared by the go confirmer, McGonagall's inbox notices and her
pending announcements.

A marker is one JSON object with a "state". A process that takes something writes {"state": "pending", "pid": <its
pid>, "at": <unix time>, ...} create-exclusive (publish: a temp file linked into place, so no reader sees half of
it and a failed write leaves nothing), and replaces it whole (replace) once the thing is finished. gone and age tell a
reader whether a pending marker's process has ended and how old it is, so it can take the marker over. Markers never
hold a prompt id, a token or a hash of a TASK.md.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Optional

from fleet import safefs
from fleet.safefs import FleetError

MAX_BYTES = 8192
UNKNOWN = {"state": "unknown"}


def _encode(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def pending(**extra) -> dict:
    return {"state": "pending", "pid": os.getpid(), "at": int(time.time()), **extra}


def publish(fd: int, name: str, data: dict) -> bool:
    """Put data at name, create-exclusive and whole. False when name exists already."""
    temp = f".{name}.{secrets.token_hex(6)}.tmp"
    temp_fd = safefs.create_new(fd, temp)
    try:
        try:
            safefs.write_all(temp_fd, _encode(data))
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except OSError:
            pass


def replace(fd: int, name: str, data: dict) -> None:
    """Replace name whole (a temp file renamed over it)."""
    safefs.write_new(fd, name, _encode(data))


def read(fd: int, name: str) -> Optional[dict]:
    """The marker, None when there is none, or UNKNOWN when it cannot be read as one."""
    try:
        raw = safefs.read_regular(fd, name, MAX_BYTES, "marker")
    except safefs.Missing:
        return None
    except (FleetError, OSError):
        return dict(UNKNOWN)
    try:
        data = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return dict(UNKNOWN)
    return data if isinstance(data, dict) and isinstance(data.get("state"), str) else dict(UNKNOWN)


def gone(marker: dict) -> bool:
    """Whether a pending marker's process has ended (or the marker names none)."""
    pid = marker.get("pid")
    if type(pid) is not int or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def age(fd: int, name: str, marker: dict, now: float) -> float:
    """Seconds since the marker's "at", or since the file was written when it has none."""
    stamp = marker.get("at")
    if type(stamp) is not int:
        info = safefs.lstat(fd, name)
        stamp = info.st_mtime if info is not None else now
    return now - stamp
