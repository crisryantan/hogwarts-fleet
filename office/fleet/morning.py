"""Ron's morning lineup, weekdays at 08:30.

The script gathers every number, with the patrol's read-only GitHub queries (fleet/patrol.py):
- Ryan's open PRs with their checks, approvals, changes requested, unresolved review threads and age;
- the open PRs that ask Ryan for a review;
- overnight reds: his PRs whose checks are red now, and main-branch commits of the watched repos that went red
  since the last weekday morning (since Friday's on a Monday);
- the portrait's morning note, his newest morning-<date>.md in the castle, when there is one;
- the PR follow-ups from the store (fleet/followup.py), open or ended in the last day.

It writes those tables to patrol/lineup/<date>.md, once a day: when that file is already there, from this job or
from a Map round that wrote a missed lineup, it changes nothing. Then it wakes Ron, on the fast tier, to write the lineup's words
from them. Ron computes nothing, and his words are appended to the same file. In shadow mode that file is all.
Once Ryan removes the shadow file, he also gets one headmaster event a day that names the file.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.morning import main; sys.exit(main())'
"""
from __future__ import annotations

import os
import re
import stat
import sys
import time
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, followup, patrol, safefs  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

DAY = 86400
# The portrait's nightly note, morning-<date>.md in his outbox, as portrait_patch.NOTE_NAME names it.
MORNING_NOTE = re.compile(r"morning-([0-9]{4}-[0-9]{2}-[0-9]{2})\.md")
NOTE_MAX_LINES = 10


def since_last_morning(ts: int) -> int:
    """The last weekday morning: a day ago, or three days ago on a Monday."""
    return ts - (3 * DAY if time.localtime(ts).tm_wday == 0 else DAY)


def portrait_note(since: int) -> Optional[dict]:
    """The portrait's newest morning note written since a time, at most ten lines. The whole file is the note;
    heading lines are left out."""
    newest = None
    for parts in (("outbox",), ("outbox", ".sent")):
        try:
            with safefs.opened_dir(config.CASTLE_ROOT, "desks", "portrait", *parts) as fd:
                for name in os.listdir(fd):
                    match = MORNING_NOTE.fullmatch(name)
                    info = None if match is None else safefs.lstat(fd, name)
                    if info is None or not stat.S_ISREG(info.st_mode) or info.st_mtime < since:
                        continue
                    if newest is None or (match.group(1), info.st_mtime) > newest[0]:
                        newest = ((match.group(1), info.st_mtime), parts, name)
        except (FleetError, OSError):
            continue
    if newest is None:
        return None
    _, parts, name = newest
    try:
        with safefs.opened_dir(config.CASTLE_ROOT, "desks", "portrait", *parts) as fd:
            text = safefs.read_regular(fd, name, config.BODY_FILE_MAX_BYTES, "portrait note").decode("utf-8", "replace")
    except (FleetError, OSError):
        return None
    lines = [line.rstrip() for line in patrol.clean(text).splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    path = "/".join((config.castle_desk_dir("portrait"), *parts, name))
    return {"file": path, "lines": lines[:NOTE_MAX_LINES]} if lines else None


def main_reds(seen: dict, since: int) -> tuple:
    """Main-branch commits of the watched repos that went red since a time, and the repos that could not be read."""
    reds, errors = [], []
    for repo in patrol.main_repos(seen):
        try:
            commits = patrol.main_commits(repo, since)
        except FleetError as exc:
            errors.append(f"{repo}: {common.one_line(exc, 150)}")
            continue
        reds += [commit for commit in commits if commit["checks"] in patrol.RED]
    return reds, errors


def render(seen: dict, reds: list, errors: list, note: Optional[dict], ts: int, since: int,
           followups_text: Optional[str] = None) -> str:
    pr_reds = [(key, (record["head"] or "-")[:12], patrol.checks_text(record), record["url"])
               for key, record in sorted(seen["prs"].items()) if record["checks"] in patrol.RED]
    main_rows = [(f"{commit['repo']} main", commit["sha"][:12], patrol.checks_text(commit), commit["url"])
                 for commit in reds]
    parts = [f"# Morning lineup {patrol.local_day(ts)}\n\n",
             "## Ryan's open PRs\n\n", patrol.prs_table(seen["prs"], ts),
             "\n## Reviews waiting on Ryan\n\n", patrol.asked_table(seen["asked"], ts),
             f"\n## Overnight reds, since {time.strftime('%a %H:%M', time.localtime(since))}\n\n",
             patrol.table(("where", "sha", "checks", "link"), pr_reds + main_rows) if pr_reds or main_rows
             else "No reds.\n"]
    if errors:
        parts.append("\nNot read: " + "; ".join(errors) + "\n")
    if not seen["complete"]:
        parts.append("\nGitHub's list of open PRs could not be read to its end, so this list is cut.\n")
    if followups_text is not None:
        parts.append("\n## Follow-ups\n\n" + followups_text)
    parts.append("\n## The portrait's note\n\n")
    if note is None:
        parts.append("No note from the portrait this morning.\n")
    else:
        parts.append(f"From {note['file']}:\n\n" + "\n".join(note["lines"]) + "\n")
    return "".join(parts)


def lineup(conn, now: Optional[int] = None) -> dict:
    """Write the morning lineup's tables, then wake Ron for its words."""
    ts = patrol.stamp(now)
    out = f"{patrol.local_day(ts)}.md"
    if os.path.lexists(patrol.file_path("lineup", out)):
        # One lineup a day: the Map's catch-up or an earlier run wrote it, so its file and Ron's words stay.
        return {"ok": True, "skipped": "today's lineup is already written", "file": patrol.file_path("lineup", out),
                "model": False}
    shadow = patrol.shadow_on()
    seen = patrol.fetch_prs()
    since = since_last_morning(ts)
    reds, errors = main_reds(seen, since)
    note = portrait_note(since)
    text = render(seen, reds, errors, note, ts, since, followup.lineup_text(conn, now))
    path = patrol.write_text("lineup", out, text)
    try:
        woke = patrol.wake(conn, "ron", "lineup", "morning lineup", text, out, now, shadow=shadow)
    except (FleetError, StoreError) as exc:
        woke = {"launched": False, "clean": False, "error": common.one_line(exc, 200)}
    patrol.tell_ryan(conn, shadow, "lineup", f"Ron's morning lineup is in {path}",
                     f"patrol:lineup:{patrol.local_day(ts)}", now)
    return {"ok": True, "shadow": shadow, "file": path, "prs": len(seen["prs"]), "asked": len(seen["asked"]),
            "reds": len(reds) + sum(1 for record in seen["prs"].values() if record["checks"] in patrol.RED),
            "note": note is not None, "model": bool(woke.get("launched")), "woke": woke}


def main(argv: Optional[list] = None) -> int:
    return patrol.run_job("lineup", lineup, argv)


if __name__ == "__main__":
    sys.exit(main())
