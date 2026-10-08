"""Which headmaster events a session has been shown, so a prompt lists only what arrived since.

The session start digest (or a session's first prompt) shows the unacked list; the marker records the id every event up
to which was shown, plus the ids shown above it, so an event the cap cut off is listed on the next prompt and one
already shown is not listed again. Each later prompt lists the rest, oldest first, and records again. One small marker
per session id in the office (events-seen/<digest of the session id>), never the id itself. A missing or unreadable
marker means "show all".
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

from fleet import config, markers, safefs


SHOWN_KEEP = 200


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def last(session_id: Optional[str]) -> Optional[tuple]:
    """(newest event id this session was shown through, ids shown above it), or None when it was shown none (or the
    marker cannot be read)."""
    if session_id is None:
        return None
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.EVENTS_SEEN_DIR) as fd:
            marker = markers.read(fd, _key(session_id))
    except (safefs.FleetError, OSError):
        return None
    if marker is None or type(marker.get("last_id")) is not int or marker["last_id"] < 0:
        return None
    shown = marker.get("shown")
    ids_above = {item for item in shown if type(item) is int} if isinstance(shown, list) else set()
    return marker["last_id"], frozenset(ids_above)


def mark(through: Optional[int], shown, after: Optional[int] = None) -> tuple:
    """The (last id, ids above it) to record once these event ids were shown: through when every event up to it was,
    else where the session stood before (after, or 0). At most SHOWN_KEEP of the newest ids above it are kept."""
    base = through if through is not None else (after or 0)
    above = sorted((item for item in shown if item > base), reverse=True)[:SHOWN_KEEP]
    return base, above


def record(session_id: Optional[str], marked: Optional[tuple]) -> None:
    """Remember a mark(...) for this session, and drop markers of long-gone sessions."""
    if session_id is None or marked is None:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.EVENTS_SEEN_DIR, create=True) as fd:
            key = _key(session_id)
            markers.replace(fd, key, {"state": "seen", "last_id": marked[0], "shown": sorted(marked[1])})
            now = time.time()
            for name in os.listdir(fd):
                if name == key or name.startswith("."):
                    continue
                try:
                    if now - os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime > config.EVENTS_SEEN_KEEP_SECONDS:
                        os.unlink(name, dir_fd=fd)
                except OSError:
                    continue
    except (safefs.FleetError, OSError):
        pass  # the next prompt shows the list again, which is the safe way to fail
