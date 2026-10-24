"""Ron's weekly scoreboard, Mondays at 09:00.

Every number comes from this script, over the seven days before the run:
- PRs merged: Ryan's PRs merged on GitHub, from the patrol's read-only query (fleet/patrol.py);
- time to first review: from opening to the first review by a person other than the author, as p50, p75,
  p90 and p95 over the merged PRs that had one, plus how many had none. Bot reviews don't count;
- review rounds: per merged PR, how many of its commits a person other than the author reviewed;
- red rate on main: per watched repo, the share of main-branch commits whose checks finished red, out of
  those whose checks finished at all;
- cost per desk: runs, tokens and dollars per desk from the store's run metrics;
- Map rounds that ran no model, from patrol/map/rounds.jsonl. The target is at least 75%;
- build review rounds: over the fourteen days before the run, the average review rounds per build task with a
  verdict recorded then, from the store, overall and split by whether the task's first build turn was given lessons
  from past reviews (its build.lessons event, fleet/lessons.py), so you can see whether they help.

It writes them to patrol/scoreboard/<date>.md and wakes Ron, on the fast tier, for the words only. In shadow
mode that file is all. Once Ryan removes the shadow file, he also gets one headmaster event that names it.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.scoreboard import main; sys.exit(main())'
"""
from __future__ import annotations

import math
import re
import sys
import time
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db, pensieve  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, patrol  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

WEEK = 7 * 86400
PERCENTILES = (50, 75, 90, 95)
FINISHED = ("SUCCESS", "FAILURE", "ERROR")
FORTNIGHT = 14 * 86400
# The build.lessons event's summary starts with the count (fleet/lessons.py record).
LESSONS_EVENT = "build.lessons"
LESSONS_COUNT = re.compile(r"([0-9]{1,4}) lessons ")


def percentile(values: list, pct: int) -> Optional[float]:
    """The nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def merged_prs(since: int) -> tuple:
    """Ryan's PRs merged since a time, each with its time to first review and its review rounds."""
    login = patrol.account()
    day = time.strftime("%Y-%m-%d", time.gmtime(since))
    data = patrol.gh_query("merged", {"merged": f"is:pr is:merged author:{login} merged:>={day}"})
    found = []
    for node in patrol.nodes(data, "merged"):
        created, merged = patrol.parse_ts(node.get("createdAt")), patrol.parse_ts(node.get("mergedAt"))
        found_pr = patrol.repo_number(node)
        if created is None or merged is None or merged < since or found_pr is None:
            continue
        repo, number = found_pr
        author = patrol.get(node, "author", "login")
        people = [item for item in patrol.nodes(node, "reviews")
                  if patrol.is_person(item.get("author")) and patrol.get(item, "author", "login") != author]
        times = [stamp for stamp in (patrol.parse_ts(item.get("submittedAt")) for item in people) if stamp is not None]
        commits = {patrol.get(item, "commit", "oid") for item in people if patrol.get(item, "commit", "oid")}
        found.append({"pr": patrol.pr_key(repo, number), "first_review": min(times) - created if times else None,
                      "rounds": len(commits), "merged_at": merged})
    total = patrol.get(data, "merged", "issueCount")
    return found, type(total) is int and total > len(patrol.nodes(data, "merged"))


def red_rates(repos: list, since: int) -> list:
    rows = []
    for repo in repos:
        try:
            commits = patrol.main_commits(repo, since)
        except FleetError as exc:
            rows.append((repo, "not read", "-", common.one_line(exc, 120)))
            continue
        finished = [commit for commit in commits if commit["checks"] in FINISHED]
        red = sum(1 for commit in finished if commit["checks"] in patrol.RED)
        rate = f"{100 * red / len(finished):.0f}%" if finished else "not computed"
        rows.append((repo, rate, f"{red} of {len(finished)}", f"{len(commits)} commits, 50 at most"))
    return rows


def build_rounds(conn, since: int) -> list:
    """{task_id, rounds, lessons} for each build task with a review verdict recorded since then: how many of its
    rounds got a verdict then, and how many lessons its first build turn was given (None when it has no record)."""
    desks = ",".join("?" for _ in config.WORKTREE_DESKS)
    rows = db.fetch_all(conn, "SELECT review_rounds.task_id, COUNT(*) AS rounds FROM review_rounds"
                              " JOIN review_passes ON review_passes.id = review_rounds.review_id"
                              " JOIN tasks ON tasks.id = review_rounds.task_id"
                              f" WHERE review_passes.created_at >= ? AND tasks.desk IN ({desks})"
                              " GROUP BY review_rounds.task_id ORDER BY review_rounds.task_id",
                        (since, *config.WORKTREE_DESKS))
    found = []
    for row in rows:
        told = db.fetch_one(conn, "SELECT summary FROM events WHERE task_id = ? AND kind = ? ORDER BY id LIMIT 1",
                            (row["task_id"], LESSONS_EVENT))
        match = None if told is None else LESSONS_COUNT.match(told["summary"])
        found.append({"task_id": row["task_id"], "rounds": int(row["rounds"]),
                      "lessons": None if match is None else int(match.group(1))})
    return found


def average(values: list) -> str:
    return f"{sum(values) / len(values):.1f}" if values else "-"


def build_rows(builds: list) -> list:
    rounds = [item["rounds"] for item in builds]
    given = [item["rounds"] for item in builds if item["lessons"]]
    none = [item["rounds"] for item in builds if item["lessons"] == 0]
    return [("build tasks with a review verdict", len(builds)), ("review rounds with a verdict", sum(rounds)),
            ("average review rounds per task", average(rounds)),
            ("tasks whose first build turn got lessons", len(given)),
            ("average review rounds, given lessons", average(given)),
            ("average review rounds, given none", average(none))]


def hours(seconds: Optional[float]) -> str:
    return "-" if seconds is None else f"{seconds / 3600:.1f}h"


def render(merged: list, more: bool, rates: list, costs: list, rounds: list, ts: int, since: int,
           builds: list = ()) -> str:
    reviewed = [item["first_review"] for item in merged if item["first_review"] is not None]
    rounds_per_pr = [item["rounds"] for item in merged]
    model_rounds = sum(1 for row in rounds if row.get("model") is True)
    ok_rounds = [row for row in rounds if row.get("ok") is True]
    zero = len(ok_rounds) - sum(1 for row in ok_rounds if row.get("model") is True)
    parts = [f"# Weekly scoreboard {patrol.local_day(ts)}\n\n",
             f"From {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} to"
             f" {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}. Every number here comes from a script.\n\n",
             "## PRs\n\n",
             patrol.table(("number", "value"), [
                 ("PRs merged", f"{len(merged)}{' (more than one query returns)' if more else ''}"),
                 *[(f"time to first review p{pct}", hours(percentile(reviewed, pct))) for pct in PERCENTILES],
                 ("merged with no review from a person", sum(1 for item in merged if item["first_review"] is None)),
                 ("review rounds, total", sum(rounds_per_pr)),
                 ("review rounds per PR p50", percentile(rounds_per_pr, 50) if rounds_per_pr else "-"),
                 ("review rounds per PR, most", max(rounds_per_pr) if rounds_per_pr else "-"),
             ]),
             "\n## Red rate on main\n\n",
             patrol.table(("repo", "red rate", "red of finished", "note"), rates) if rates else "No repos to read.\n",
             "\n## Cost per desk, from the store\n\n",
             patrol.table(("desk", "runs", "input tokens", "output tokens", "cost"),
                          [(row["desk"], row["runs"], row["input_tokens"], row["output_tokens"],
                            f"${row['cost_usd'] or 0:.2f}") for row in costs]) if costs else "No runs recorded.\n",
             "\n## Map rounds\n\n",
             patrol.table(("number", "value"), [
                 ("rounds that read GitHub", len(ok_rounds)),
                 ("rounds that ran no model", zero),
                 ("share with no model (target 75% or more)",
                  f"{100 * zero / len(ok_rounds):.0f}%" if ok_rounds else "not computed"),
                 ("rounds that ran a model", model_rounds),
                 ("rounds that could not read GitHub", len(rounds) - len(ok_rounds)),
             ]),
             "\n## Build review rounds, last two weeks\n\n",
             patrol.table(("number", "value"), build_rows(list(builds)))]
    return "".join(parts)


def scoreboard(conn, now: Optional[int] = None) -> dict:
    """Compute the week's numbers, write them, then wake Ron for the words."""
    ts = patrol.stamp(now)
    shadow = patrol.shadow_on()
    since = ts - WEEK
    merged, more = merged_prs(since)
    seen = patrol.fetch_prs()
    rates = red_rates(patrol.main_repos(seen), since)
    costs = pensieve.summary(conn, since)
    rounds = patrol.read_rows("map", "rounds.jsonl", since)
    builds = build_rounds(conn, ts - FORTNIGHT)
    text = render(merged, more, rates, costs, rounds, ts, since, builds)
    out = f"{patrol.local_day(ts)}.md"
    path = patrol.write_text("scoreboard", out, text)
    try:
        woke = patrol.wake(conn, "ron", "scoreboard", "weekly scoreboard", text, out, now, shadow=shadow)
    except (FleetError, StoreError) as exc:
        woke = {"launched": False, "clean": False, "error": common.one_line(exc, 200)}
    patrol.tell_ryan(conn, shadow, "scoreboard", f"Ron's weekly scoreboard is in {path}",
                     f"patrol:scoreboard:{patrol.local_day(ts)}", now)
    return {"ok": True, "shadow": shadow, "file": path, "merged": len(merged), "model": bool(woke.get("launched")),
            "woke": woke}


def main(argv: Optional[list] = None) -> int:
    return patrol.run_job("scoreboard", scoreboard, argv)


if __name__ == "__main__":
    sys.exit(main())
