"""SessionEnd hook: store a capped, scrubbed extract of the session in the Pensieve.

Reads transcript_path from the hook input and keeps only Ryan's typed prompts and the
final assistant reply of each turn. Tool calls, tool output, injected context, meta
entries and subagent (sidechain) entries are never read into an extract.

Each entry is cleaned, scrubbed with the store's scrub(), and cut to 4000 characters.
The session keeps at most 16000 characters. Entries are numbered in transcript order,
so a second SessionEnd for a resumed session adds only the new entries.

The session is recorded under McGonagall only when the hook input's agent_type says
the session runs her agent. Any other session in the castle is recorded as Ryan's own.

Input fields read: session_id, transcript_path, cwd, agent_type, agent_id.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import ids, pensieve  # noqa: E402
from hogwarts.errors import ConflictError, StoreError, ValidationError  # noqa: E402

from fleet import common, config, transcript  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

_PROJECT_CHARS = re.compile(r"[^A-Za-z0-9._/-]+")
SCRUB_ROUNDS = 4


def project_name(cwd: object) -> str:
    if not isinstance(cwd, str):
        return "unknown"
    name = _PROJECT_CHARS.sub("-", cwd).strip("-")[:200]
    return name or "unknown"


def _model(value: Optional[str]) -> Optional[str]:
    try:
        return None if value is None else ids.check("label", value, "model")
    except StoreError:
        return None


def capped(pairs: list) -> list:
    """Clean, scrub and cut each entry, keeping the session under its total cap."""
    kept, used = [], 0
    for role, raw in pairs:
        room = min(config.EXTRACT_CAP, config.SESSION_CAP - used)
        if room <= 0:
            break
        try:
            text = ids.clean_text(raw, "extract text", 10 ** 9)
        except ValidationError:
            continue
        for _ in range(SCRUB_ROUNDS):
            text = pensieve.scrub(text)[:room]
        if pensieve.scrub(text) != text or not text.strip():
            continue
        kept.append((role, text))
        used += len(text)
    return kept


def store(conn, data: dict, desk: str, now: int) -> dict:
    session = common.session_id(data)
    if session is None:
        raise FleetError("hook input has no usable session_id")
    pairs, stats = transcript.summarize(transcript.iter_entries(data.get("transcript_path"), conversation_only=True))
    pieces = capped(pairs)
    started = stats["started_at"] if stats["started_at"] is not None else now
    started = min(started, now)
    pensieve.record_session(
        conn, session, project_name(data.get("cwd")), desk=common.session_desk(data, desk),
        model=_model(stats["model"]),
        started_at=started, ended_at=now, first_turn_tokens=stats["first_turn_tokens"],
        total_input_tokens=stats["total_input_tokens"],
    )
    added = skipped = 0
    for seq, (role, text) in enumerate(pieces, 1):
        try:
            pensieve.add_extract(conn, session, role, text, seq=seq, now=now)
        except ConflictError:
            skipped += 1
            continue
        except ValidationError as exc:
            if "exceed" in str(exc):
                break
            raise
        added += 1
    return {"added": added, "already_stored": skipped}


def _body(data: dict, desk: str, out, now: int) -> None:
    conn = common.connect()
    try:
        result = store(conn, data, desk, now)
    finally:
        conn.close()
    out.write(f"Pensieve: {result['added']} extracts stored.\n")


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    return common.run_hook("SessionEnd", _body, argv, stdin, stdout, stderr, now)


if __name__ == "__main__":
    sys.exit(main())
