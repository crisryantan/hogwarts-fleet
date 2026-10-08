"""PreCompact hook: append a timestamped Checkpoint stub to McGonagall's scratchpad.

The stub names the desk's active task so the session can pick up after the compact.
Input fields read: trigger ("manual" or "auto") and session_id. The scratchpad is
opened one folder at a time with O_NOFOLLOW and must be a plain, singly linked
file owned by Ryan, so a desk cannot steer this append onto another file.

First it rotates the scratchpad (fleet/scratchpad.py): older Checkpoint blocks move to the
archive and only the latest stays. A scratchpad has a 6KB budget. When the stub would still
take it over, nothing is appended and the hook says so, so the desk trims it and writes the
Checkpoint itself.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import pensieve  # noqa: E402

from fleet import common, config, safefs, scratchpad  # noqa: E402

SCRATCHPAD = scratchpad.SCRATCHPAD
TRIGGERS = ("manual", "auto")


def stub(conn, desk: str, data: dict, now: int) -> str:
    trigger = data.get("trigger") if data.get("trigger") in TRIGGERS else "unknown"
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
    active = pensieve.list_tasks(conn, desk, "active")
    if active:
        task = active[0]
        task_line = f"- Task: {task['id']} active, {common.one_line(task['title'], 120)}"
    else:
        task_line = "- Task: none active"
    session = common.session_id(data)
    lines = [
        "",
        f"### Checkpoint {stamp} (pre-compact, {trigger})",
        task_line,
        f"- Session: {session or 'unknown'}",
        "- Next: write the next step here.",
        "",
    ]
    return "\n".join(lines)


def _body(data: dict, desk: str, out, now: int) -> None:
    conn = common.connect()
    try:
        text = stub(conn, desk, data, now)
    finally:
        conn.close()
    data_bytes = text.encode("utf-8")
    # One lock over the rotation and the append, so no other rotation replaces the file between them.
    with scratchpad.desk_lock(desk):
        warning = scratchpad.rotate_held(desk, now)["warning"]
        if warning:
            out.write(warning + "\n")
        with safefs.opened_dir(config.CASTLE_ROOT, "desks", desk) as fd:
            pad = safefs.open_append(fd, SCRATCHPAD, "scratchpad")
            try:
                if os.fstat(pad).st_size + len(data_bytes) > config.SCRATCHPAD_BUDGET_BYTES:
                    out.write(f"{desk}'s scratchpad is at its 6KB budget, so no Checkpoint stub was added. "
                              "Trim it, then write the Checkpoint.\n")
                    return
                safefs.write_all(pad, data_bytes)
            finally:
                os.close(pad)
    out.write(f"Checkpoint stub added to {desk}'s scratchpad.\n")


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    return common.run_hook("PreCompact", _body, argv, stdin, stdout, stderr, now)


if __name__ == "__main__":
    sys.exit(main())
