"""Ron's keeper's watch, weekdays at 09:00, 13:00 and 17:00.

The script reads, with the patrol's read-only GitHub queries (fleet/patrol.py), the checks on Ryan's open PRs
and on the newest main-branch commit of each watched repo, and writes what it found to
patrol/keeper/<date>-<HHMM>.md.

- All green, or only reds Ron has already judged: no model runs.
- A new red (a PR or main commit, at its sha, with its failing checks): it wakes Ron, on the fast tier, who
  calls each one REAL, FLAKY, INFRA or UNSURE and writes a fix brief for each REAL one. His words land in the
  same file. Ron never retries or unblocks anything.
- A gate waiting on a person (a check run waiting for approval, or one that asks for action) is a row for Ryan.

In shadow mode the file is all. Once Ryan removes the shadow file, each new gate, and each watch where Ron
marked a row headmaster, is also a headmaster event that names the file.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.keeper import main; sys.exit(main())'
"""
from __future__ import annotations

import sys
import time
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, patrol  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

JUDGED = "judged.json"
JUDGED_KEEP = 500
MAIN_LOOKBACK_SECONDS = 7 * 86400


def signature(item: dict) -> str:
    """One red as Ron judges it: where, at which sha, with which checks failing."""
    return f"{item['where']}@{item['sha']}:{','.join(item['failing'])}"


def watched(seen: dict, ts: int) -> tuple:
    """Every PR and newest main commit that is red or waits on a person, and the repos that could not be read."""
    items, errors = [], []
    for key, record in sorted(seen["prs"].items()):
        if record["checks"] in patrol.RED or record["waiting"]:
            items.append({"where": key, "sha": record["head"] or "-", "url": record["url"], "checks": record["checks"],
                          "failing": record["failing"], "waiting": record["waiting"]})
    for repo in patrol.main_repos(seen):
        try:
            commits = patrol.main_commits(repo, ts - MAIN_LOOKBACK_SECONDS)
        except FleetError as exc:
            errors.append(f"{repo}: {common.one_line(exc, 150)}")
            continue
        newest = commits[0] if commits else None
        if newest is not None and (newest["checks"] in patrol.RED or newest["waiting"]):
            items.append({"where": f"{repo} main", "sha": newest["sha"], "url": newest["url"],
                          "checks": newest["checks"], "failing": newest["failing"], "waiting": newest["waiting"]})
    return items, errors


def render(reds: list, fresh: list, gates: list, errors: list, ts: int) -> str:
    def rows(items: list) -> list:
        return [(item["where"], item["sha"][:12], patrol.checks_text(item), item["url"]) for item in items]

    parts = [f"# Keeper's watch {patrol.local_day(ts)} {time.strftime('%H:%M', time.localtime(ts))}\n\n"]
    if not reds and not gates:
        parts.append("All green. No model ran.\n")
    if fresh:
        parts += ["## New reds for Ron to call\n\n", patrol.table(("where", "sha", "checks", "link"), rows(fresh))]
    old = [item for item in reds if item not in fresh]
    if old:
        parts += ["\n## Reds Ron already called\n\n", patrol.table(("where", "sha", "checks", "link"), rows(old))]
    if gates:
        parts += ["\n## For Ryan: gates waiting on a person\n\n",
                  patrol.table(("where", "sha", "checks", "link"), rows(gates))]
    if errors:
        parts.append("\nNot read: " + "; ".join(errors) + "\n")
    return "".join(parts)


def watch(conn, now: Optional[int] = None) -> dict:
    """One keeper's watch: no model when green or already judged, Ron on a new red."""
    ts = patrol.stamp(now)
    shadow = patrol.shadow_on()
    seen = patrol.fetch_prs()
    items, errors = watched(seen, ts)
    reds = [item for item in items if item["checks"] in patrol.RED]
    gates = [item for item in items if item["waiting"]]
    judged = patrol.read_state("keeper", JUDGED, [])
    judged = [value for value in judged if isinstance(value, str)] if isinstance(judged, list) else []
    fresh = [item for item in reds if signature(item) not in judged]
    out = f"{patrol.file_stamp(ts)}.md"
    text = render(reds, fresh, gates, errors, ts)
    path = patrol.write_text("keeper", out, text)
    for gate in gates:
        patrol.tell_ryan(conn, shadow, "gate", f"{gate['where']}: a gate waits on a person"
                         f" ({', '.join(gate['waiting'][:3])}), see {path}",
                         f"patrol:gate:{gate['where']}:{gate['sha']}:{','.join(gate['waiting'])}", now)
    woke = None
    if fresh:
        patrol.write_state("keeper", JUDGED, (judged + [signature(item) for item in fresh])[-JUDGED_KEEP:])
        try:
            woke = patrol.wake(conn, "ron", "keeper", "keeper's watch", text, out, now, shadow=shadow)
        except (FleetError, StoreError) as exc:
            woke = {"launched": False, "clean": False, "error": common.one_line(exc, 200)}
    return {"ok": True, "shadow": shadow, "file": path, "reds": len(reds), "new_reds": len(fresh),
            "gates": len(gates), "model": bool(woke and woke.get("launched")), "woke": woke, "errors": errors}


def main(argv: Optional[list] = None) -> int:
    return patrol.run_job("keeper", watch, argv)


if __name__ == "__main__":
    sys.exit(main())
