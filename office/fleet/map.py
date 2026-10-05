"""The Marauder's Map - PR Watcher: one round, every 15 minutes on weekdays from 08:00 to 19:00.

No model of its own. A round reads Ryan's open PRs, and the open PRs asking him for a review, with the
patrol's read-only GitHub queries (fleet/patrol.py), page by page, compares them with the last snapshot and
writes, in the office patrol/map folder:
- snapshot.json, what the next round compares against;
- outcomes.jsonl, one row per change, marked routine or for-me;
- rounds.jsonl, one row per round, saying whether any model ran in it.

A round that could not read either list to its end changes nothing: no snapshot, no rows, no wake. Its
round row says it was incomplete, and once live Ryan hears once a day.

A round with a for-me row wakes Ron, on the fast tier, with the round's rows, and his words land in
round-<stamp>.md next to them. A round with only routine rows, or with no change, runs no model. The first
round, with no snapshot or a new GITHUB_ACCOUNT, only takes a baseline.

For-me rows: checks went red, a gate waits on a person, a PR was approved or got changes requested, a person
opened a review thread, or someone asked Ryan for a review. A PR first seen after the baseline counts each of
these it already has. Everything else is routine.

Hermione's bot pass, in draft mode: once a PR is BOT_PASS_DELAY_SECONDS old, a round that finds unresolved
review threads she has not seen fetches that PR's threads and wakes her. She writes a triage table and reply
drafts, which land in patrol/bot-pass. The threads count as seen only once the patrol has taken her drafts;
until then they wait on her pending owl and are not sent again. Nothing is ever posted anywhere.

Last, the round sends again each patrol owl whose file the patrol has not taken yet (patrol.resend_pending).

In shadow mode that is all. Once Ryan removes the shadow file, each for-me row is also a headmaster event,
with a summary the script builds from the repo, the PR number and the kind of change.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.map import main; sys.exit(main())'
"""
from __future__ import annotations

import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, patrol, run_desk  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

SNAPSHOT = "snapshot.json"
OUTCOMES = "outcomes.jsonl"
ROUNDS = "rounds.jsonl"
BOT_PASS_STATE = patrol.BOT_PASS_STATE
FOR_ME, ROUTINE = "for-me", "routine"
COMMENT_MAX = 3000
HUNK_MAX = 1500
THREADS_TEXT_MAX = 200_000


def _row(key: str, change: str, detail: str, mark: str) -> dict:
    return {"pr": key, "change": change, "detail": common.one_line(detail, 300), "mark": mark}


def _names(values) -> str:
    return ", ".join(sorted(values)[:5])


def pr_changes(key: str, was: Optional[dict], now: Optional[dict]) -> list:
    """The rows for one of Ryan's PRs between two rounds."""
    if was is None:  # first seen after the baseline: every signal it already has counts
        rows = [_row(key, "opened", "now watched", ROUTINE)]
        if now["checks"] in patrol.RED:
            rows.append(_row(key, "checks red", _names(now["failing"]), FOR_ME))
        if now["waiting"]:
            rows.append(_row(key, "waiting on a person", _names(now["waiting"]), FOR_ME))
        if now["decision"] == "APPROVED":
            rows.append(_row(key, "approved", f"{now['approvals']} approval(s), waiting on you", FOR_ME))
        elif now["decision"] == "CHANGES_REQUESTED":
            rows.append(_row(key, "changes requested", f"{now['changes']} reviewer(s)", FOR_ME))
        people = set(now["human_threads"])
        if people:
            rows.append(_row(key, "review thread from a person", f"{len(people)} new", FOR_ME))
        if set(now["open_threads"]) - people:
            rows.append(_row(key, "bot review thread", f"{len(set(now['open_threads']) - people)} new", ROUTINE))
        return rows
    if now is None:
        return [_row(key, "left", "merged or closed", ROUTINE)]
    rows = []
    if was.get("head") != now["head"]:
        rows.append(_row(key, "new commits", f"head {(now['head'] or '-')[:12]}", ROUTINE))
    if was.get("checks") != now["checks"]:
        if now["checks"] in patrol.RED:
            rows.append(_row(key, "checks red", _names(now["failing"]), FOR_ME))
        elif now["checks"] == "SUCCESS":
            rows.append(_row(key, "checks green", "every check passed", ROUTINE))
    elif now["checks"] in patrol.RED and set(now["failing"]) - set(was.get("failing", [])):
        rows.append(_row(key, "checks red", _names(set(now["failing"]) - set(was.get("failing", []))), FOR_ME))
    gates = set(now["waiting"]) - set(was.get("waiting", []))
    if gates:
        rows.append(_row(key, "waiting on a person", _names(gates), FOR_ME))
    if was.get("decision") != now["decision"]:
        if now["decision"] == "APPROVED":
            rows.append(_row(key, "approved", f"{now['approvals']} approval(s), waiting on you", FOR_ME))
        elif now["decision"] == "CHANGES_REQUESTED":
            rows.append(_row(key, "changes requested", f"{now['changes']} reviewer(s)", FOR_ME))
        else:
            rows.append(_row(key, "review decision", f"{was.get('decision')} to {now['decision']}", ROUTINE))
    elif now["approvals"] > was.get("approvals", 0):
        rows.append(_row(key, "approval", f"{now['approvals']} approval(s)", ROUTINE))
    fresh = set(now["open_threads"]) - set(was.get("open_threads", []))
    people = fresh & set(now["human_threads"])
    if people:
        rows.append(_row(key, "review thread from a person", f"{len(people)} new", FOR_ME))
    if fresh - people:
        rows.append(_row(key, "bot review thread", f"{len(fresh - people)} new", ROUTINE))
    if was.get("draft") != now["draft"]:
        rows.append(_row(key, "back to draft" if now["draft"] else "ready for review", "", ROUTINE))
    return rows


def changes(before: dict, after: dict) -> list:
    """Every row between the last snapshot and this round, Ryan's PRs first, then review requests."""
    rows = []
    old, new = before.get("prs") or {}, after["prs"]
    for key in sorted(set(old) | set(new)):
        was = old.get(key) if isinstance(old.get(key), dict) else None
        if was is None and key not in new:
            continue
        rows += pr_changes(key, was, new.get(key))
    old_asked = before.get("asked") if isinstance(before.get("asked"), dict) else {}
    for key in sorted(set(after["asked"]) - set(old_asked)):
        rows.append(_row(key, "review requested from you", f"by {after['asked'][key]['author']}", FOR_ME))
    for key in sorted(set(old_asked) - set(after["asked"])):
        rows.append(_row(key, "review request gone", "reviewed, closed or withdrawn", ROUTINE))
    return rows


def render_round(rows: list, seen: dict, ts: int) -> str:
    changed = [(row["mark"], row["pr"], row["change"], row["detail"]) for row in rows]
    return (f"# Map round {patrol.file_stamp(ts)}\n\n"
            "## Changes since the last round\n\n" + patrol.table(("mark", "PR", "change", "detail"), changed)
            + "\n## Ryan's open PRs now\n\n" + patrol.prs_table(seen["prs"], ts)
            + "\n## Reviews waiting on Ryan\n\n" + patrol.asked_table(seen["asked"], ts))


# Hermione's bot pass


def fetch_threads(record: dict) -> list:
    """The unresolved review threads of one PR, with their comments, cleaned and cut."""
    owner, name = record["repo"].split("/", 1)
    data = patrol.gh_query("threads", {"owner": owner, "name": name, "number": record["number"]})
    threads = []
    for node in patrol.nodes(data, "repository", "pullRequest", "reviewThreads"):
        thread_id = node.get("id")
        if node.get("isResolved") is True or not isinstance(thread_id, str) \
                or patrol.THREAD_ID.fullmatch(thread_id) is None:
            continue
        comments = []
        for item in patrol.nodes(node, "comments"):
            login = patrol.get(item, "author", "login")
            comments.append({
                "author": login if isinstance(login, str) and patrol.LOGIN.fullmatch(login) else "unknown",
                "person": patrol.is_person(item.get("author")),
                "url": patrol.safe_url(item.get("url")),
                "body": patrol.clean(item.get("body") or "")[:COMMENT_MAX],
                "hunk": patrol.clean(item.get("diffHunk") or "")[:HUNK_MAX],
            })
        line = node.get("line")
        threads.append({"id": thread_id, "path": common.one_line(patrol.clean(node.get("path") or "-"), 200),
                        "line": line if type(line) is int else None, "outdated": node.get("isOutdated") is True,
                        "comments": comments})
    return threads


def render_threads(key: str, record: dict, threads: list, fresh: list) -> tuple:
    """The pass's input and the ids of the threads it carries. Threads are carried whole up to THREADS_TEXT_MAX
    and the rest are left out, not cut, so they stay unseen for a later pass. Only a first thread that is too
    big on its own is cut, so a pass always carries something."""
    head = "".join([f"# Bot pass for {key}\n\n", f"Title: {record['title']}\n", f"Link: {record['url'] or '-'}\n",
                    f"Unresolved review threads: {len(threads)}, of which {len(set(fresh))} are new since the last"
                    " pass.\n"])
    parts, carried, size = [head], [], len(head)
    for number, thread in enumerate(threads, 1):
        marks = (" NEW" if thread["id"] in fresh else "") + (" OUTDATED" if thread["outdated"] else "")
        where = thread["path"] + (f":{thread['line']}" if thread["line"] else "")
        block = [f"\n## Thread {number}{marks}: {where}\n"]
        hunk = thread["comments"][0]["hunk"] if thread["comments"] else ""
        if hunk:
            block.append("\n```diff\n" + hunk.replace("```", "'''") + "\n```\n")
        for comment in thread["comments"]:
            who = comment["author"] + ("" if comment["person"] else " (bot)")
            quoted = "\n".join("> " + line for line in comment["body"].splitlines()) or "> (empty)"
            block.append(f"\n{who}, {comment['url'] or '-'}:\n{quoted}\n")
        text = "".join(block)
        if size + len(text) > THREADS_TEXT_MAX:
            if carried:
                break
            text = text[:THREADS_TEXT_MAX - size] + "\n\n(cut here: this one thread is longer than a pass carries)\n"
        parts.append(text)
        size += len(text)
        carried.append(thread["id"])
    if len(carried) < len(threads):
        parts.append(f"\n\n({len(threads) - len(carried)} more thread(s) did not fit; a later pass carries them.)\n")
    return "".join(parts), carried


def seed_bot_passes(seen: dict, ts: int) -> None:
    """On a baseline round, count every thread already open as seen, so taking the Map on wakes no one."""
    patrol.write_state("map", BOT_PASS_STATE, {key: {"threads": record["open_threads"], "at": ts}
                                               for key, record in seen["prs"].items()})


def bot_passes(conn, seen: dict, ts: int, now: Optional[int] = None, shadow: bool = True) -> list:
    """Wake Hermione for each PR old enough to have its bot reviews with threads she has not seen yet. A
    thread counts as seen once the patrol has taken her drafts for it (patrol.finish); until then it waits
    on her pending owl and no second pass sends it."""
    state = patrol.read_state("map", BOT_PASS_STATE, {})
    state = {key: value for key, value in (state.items() if isinstance(state, dict) else ())
             if key in seen["prs"] and isinstance(value, dict) and isinstance(value.get("threads"), list)}
    patrol.write_state("map", BOT_PASS_STATE, state)
    sent = patrol.in_flight("bot-pass")
    due = []
    for key, record in sorted(seen["prs"].items()):
        if not record["created_at"] or ts - record["created_at"] < config.BOT_PASS_DELAY_SECONDS:
            continue
        done = set(state.get(key, {}).get("threads", [])) | sent.get(key, set())
        fresh = [thread for thread in record["open_threads"] if thread not in done]
        if fresh:
            due.append((key, record, fresh))
    passes = []
    if due and not run_desk.is_enabled("hermione"):
        due, passes = [], [{"skipped": "hermione is not enabled", "due": len(due)}]
    for key, record, fresh in due[:config.BOT_PASS_MAX_PER_ROUND]:
        tag = f"{record['repo'].replace('/', '--')}-{record['number']}"
        out = f"{tag}-{patrol.file_stamp(now)}.md"
        try:
            threads = fetch_threads(record)
            text, carried = render_threads(key, record, threads, fresh)
            # Seen once taken: the threads this pass carries, and open ones the fetch no longer returns (resolved
            # since, or unreadable), which no pass could carry. Threads left out for size wait for a later pass.
            fetched = {thread["id"] for thread in threads}
            marks = carried + [thread for thread in record["open_threads"] if thread not in fetched]
            patrol.write_text("bot-pass", out, text)
            woke = patrol.wake(conn, "hermione", "bot-pass", "bot pass", text, out, now, tag=tag, shadow=shadow,
                               marks=marks, subject=key)
        except (FleetError, StoreError) as exc:
            woke = {"launched": False, "clean": False, "error": common.one_line(exc, 200)}
        passes.append({"pr": key, "file": patrol.file_path("bot-pass", out), **woke})
    return passes


# The round


def run_round(conn, now: Optional[int] = None) -> dict:
    """One Map round. Returns its round row plus what it woke and sent again."""
    ts = patrol.stamp(now)
    shadow = patrol.shadow_on()
    try:
        login = patrol.account()
        seen = patrol.fetch_prs()
    except FleetError as exc:
        error = common.one_line(exc, 200)
        patrol.append_row("map", ROUNDS, {"ts": ts, "ok": False, "shadow": shadow, "error": error, "model": False})
        patrol.tell_ryan(conn, shadow, "map-failed", f"the Map could not read GitHub: {error}",
                         f"patrol:map-failed:{patrol.local_day(now)}", now)
        return {"ok": False, "error": error}
    if not seen["complete"]:
        error = "GitHub's list of open PRs could not be read to its end, so the snapshot was left as it was"
        patrol.append_row("map", ROUNDS, {"ts": ts, "ok": False, "incomplete": True, "shadow": shadow, "error": error,
                                          "prs": len(seen["prs"]), "asked": len(seen["asked"]), "model": False})
        patrol.tell_ryan(conn, shadow, "map-incomplete", f"the Map read only part of your PRs: {error}",
                         f"patrol:map-incomplete:{patrol.local_day(now)}", now)
        return {"ok": False, "incomplete": True, "error": error}
    before = patrol.read_state("map", SNAPSHOT, None)
    baseline = not isinstance(before, dict) or before.get("account") != login or not isinstance(before.get("prs"), dict)
    rows = [] if baseline else changes(before, seen)
    patrol.write_state("map", SNAPSHOT, {"account": login, "taken_at": ts, "prs": seen["prs"], "asked": seen["asked"]})
    for row in rows:
        patrol.append_row("map", OUTCOMES, {"ts": ts, **row})
    for_me = [row for row in rows if row["mark"] == FOR_ME]
    woke = None
    if for_me:
        out = f"round-{patrol.file_stamp(now)}.md"
        text = render_round(rows, seen, ts)
        patrol.write_text("map", out, text)
        for row in for_me:
            patrol.tell_ryan(conn, shadow, "for-me", f"{row['pr']}: {row['change']} ({row['detail']})",
                             f"patrol:map:{ts}:{row['pr']}:{row['change']}", now)
        try:
            woke = patrol.wake(conn, "ron", "map", "map round", text, out, now, shadow=shadow)
        except (FleetError, StoreError) as exc:
            woke = {"launched": False, "clean": False, "error": common.one_line(exc, 200)}
    if baseline:
        seed_bot_passes(seen, ts)
        passes = []
    else:
        passes = bot_passes(conn, seen, ts, now, shadow)
    resent = patrol.resend_pending(conn, shadow, now)
    model = bool(woke and woke.get("launched")) or any(item.get("launched") for item in passes) or resent["launched"]
    row = {"ts": ts, "ok": True, "shadow": shadow, "baseline": baseline, "prs": len(seen["prs"]),
           "asked": len(seen["asked"]), "changes": len(rows), "for_me": len(for_me), "model": model}
    patrol.append_row("map", ROUNDS, row)
    return {**row, "woke": woke, "bot_passes": passes, "resent": resent}


def main(argv: Optional[list] = None) -> int:
    return patrol.run_job("map", run_round, argv)


if __name__ == "__main__":
    sys.exit(main())
