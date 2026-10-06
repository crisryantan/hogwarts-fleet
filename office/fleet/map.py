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

PR follow-ups (fleet/followup.py): before the round writes its snapshot, so a round killed mid-routing is computed
again and loses no row, the follow-up tidies what only the store can finish and, while the Headmaster has it on and
live, sends teammates' comments on PRs the review loop opened back to the build desk. A round that cannot read GitHub
whole (offline, gh not signed in, a list cut short) still does that tidying, and routes nothing. A person's thread
the follow-up takes becomes a routine row, since the follow-up raises its own events, and the bot pass leaves those
threads alone. The round file lists the follow-ups from the store.

Last, the round sends again each patrol owl whose file the patrol has not taken yet (patrol.resend_pending).

Auto-close. Before any GitHub read, on every path and whatever shadow mode says, the round calls closer.sweep, which
starts one detached closer pass while you have switched auto-close on and none is running (fleet/closer.py). Each
round row says what the sweep did under "closer": off, running, started, idle or failed. The pass runs beside the
rest of the round, so the follow-up's routing below can reach a task the pass is working. Both act on a task only under
its review lock (routing takes it without waiting and tries again next round), the closer never takes a task with an
open follow-up, the store refuses a proven close of one, and a follow-up opens only on a task still awaiting close.
So whichever commits first wins, and the other leaves the task alone.

Worktree cleanup. Right after the closer's sweep, the round calls worktree.sweep_closed, which finishes a worktree
removal a kill cut short and, while you have the worktree-cleanup switch on, removes the worktree of each build task
closed long enough ago, once git shows nothing in it would be lost. Its removals are one routine row per round, and
each round row says what it did under "worktrees".

In shadow mode that is all. Once Ryan removes the shadow file, each for-me row is also a headmaster event,
with a summary the script builds from the repo, the PR number and the kind of change.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.map import main; sys.exit(main())'
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts.errors import StoreError  # noqa: E402

from fleet import closer, common, config, followup, morning, patrol, run_desk, worktree  # noqa: E402
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
    """One outcomes row. Its detail can join GitHub check names and logins, so it is scrubbed again on one line."""
    return {"pr": key, "change": change, "detail": common.scrubbed_line(detail, 300), "mark": mark}


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
    # A key of the last snapshot is used only when it is one this round could have made (patrol.pr_key_ok): a snapshot
    # an older patrol wrote may hold a repository name it now refuses, which no row names.
    old = {key: value for key, value in (before.get("prs") or {}).items() if patrol.pr_key_ok(key)}
    new = after["prs"]
    for key in sorted(set(old) | set(new)):
        was = old.get(key) if isinstance(old.get(key), dict) else None
        if was is None and key not in new:
            continue
        rows += pr_changes(key, was, new.get(key))
    old_asked = before.get("asked") if isinstance(before.get("asked"), dict) else {}
    old_asked = {key: value for key, value in old_asked.items() if patrol.pr_key_ok(key)}
    for key in sorted(set(after["asked"]) - set(old_asked)):
        asked = after["asked"][key]
        if asked.get("bot"):  # a bot's request is listed in the lineup but never wakes Ron
            rows.append(_row(key, "review requested from you by a bot", f"by {asked['author']}", ROUTINE))
        else:
            rows.append(_row(key, "review requested from you", f"by {asked['author']}", FOR_ME))
    for key in sorted(set(old_asked) - set(after["asked"])):
        rows.append(_row(key, "review request gone", "reviewed, closed or withdrawn", ROUTINE))
    return rows


def render_round(rows: list, seen: dict, ts: int, followups_text: Optional[str] = None) -> str:
    changed = [(row["mark"], row["pr"], row["change"], row["detail"]) for row in rows]
    text = (f"# Map round {patrol.file_stamp(ts)}\n\n"
            "## Changes since the last round\n\n" + patrol.table(("mark", "PR", "change", "detail"), changed)
            + "\n## Ryan's open PRs now\n\n" + patrol.prs_table(seen["prs"], ts)
            + "\n## Reviews waiting on Ryan\n\n" + patrol.asked_table(seen["asked"], ts))
    if followups_text is not None:
        text += "\n## Follow-ups\n\n" + followups_text
    return text


# Hermione's bot pass


def fetch_threads(record: dict) -> list:
    """The unresolved review threads of one PR, with their comments. Every comment, hunk and path is normalized and
    scrubbed whole (patrol.github_text) before it is cut, as the PR follow-up's are; the path is scrubbed again once it
    is on one line (common.scrubbed_line), since making it ASCII can shape a credential. An author is shown only as
    patrol.shown_login gives it. A comment's commit is the sha
    GitHub gives in its own field (originalCommit), kept only when it is exactly 40 lowercase hex, and written next to
    the scrubbed text, since the scrub masks a full sha."""
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
            comments.append({
                "author": patrol.shown_login(patrol.get(item, "author", "login")),
                "person": patrol.is_person(item.get("author")),
                "url": patrol.safe_url(item.get("url")),
                "commit": patrol.sha(patrol.get(item, "originalCommit", "oid")),
                "body": patrol.github_text(item.get("body"))[:COMMENT_MAX],
                "hunk": patrol.github_text(item.get("diffHunk"))[:HUNK_MAX],
            })
        line = node.get("line")
        threads.append({"id": thread_id, "path": common.scrubbed_line(patrol.github_text(node.get("path")), 200) or "-",
                        "line": line if type(line) is int else None, "outdated": node.get("isOutdated") is True,
                        "comments": comments})
    return threads


def render_threads(key: str, record: dict, threads: list, fresh: list) -> tuple:
    """The pass's input and the ids of the threads it carries. Threads are carried whole up to THREADS_TEXT_MAX
    and the rest are left out, not cut, so they stay unseen for a later pass. Only a first thread that is too
    big on its own is cut, so a pass always carries something. Every piece of GitHub text in it was scrubbed before
    any cut (fetch_threads, patrol.pr_record); the commit shas are GitHub's own fields, added after the scrub."""
    head = "".join([f"# Bot pass for {key}\n\n", f"Title: {record['title']}\n", f"Link: {record['url'] or '-'}\n",
                    f"Head commit: {patrol.sha(record.get('head')) or '-'}\n",
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
            commit = patrol.sha(comment.get("commit"))
            on = f", on commit {commit}" if commit else ""
            block.append(f"\n{who}, {comment['url'] or '-'}{on}:\n{quoted}\n")
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


def bot_passes(conn, seen: dict, ts: int, now: Optional[int] = None, shadow: bool = True,
               skip: Optional[dict] = None) -> list:
    """Wake Hermione for each PR old enough to have its bot reviews with threads she has not seen yet. A
    thread counts as seen once the patrol has taken her drafts for it (patrol.finish); until then it waits
    on her pending owl and no second pass sends it. skip maps a PR to the threads a PR follow-up takes, which she
    leaves alone; a PR whose follow-up threads the store could not read (None) gets no bot pass this round."""
    skip = skip or {}
    state = patrol.read_state("map", BOT_PASS_STATE, {})
    state = {key: value for key, value in (state.items() if isinstance(state, dict) else ())
             if key in seen["prs"] and isinstance(value, dict) and isinstance(value.get("threads"), list)}
    patrol.write_state("map", BOT_PASS_STATE, state)
    sent = patrol.in_flight("bot-pass")
    due = []
    for key, record in sorted(seen["prs"].items()):
        if not record["created_at"] or ts - record["created_at"] < config.BOT_PASS_DELAY_SECONDS:
            continue
        if key in skip and skip[key] is None:
            continue
        done = set(state.get(key, {}).get("threads", [])) | sent.get(key, set()) | set(skip.get(key) or ())
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
            # Seen once taken: the threads this pass carries, and open ones the fetch no longer returns (resolved
            # since, or unreadable), which no pass could carry. Threads left out for size wait for a later pass, and
            # threads a PR follow-up took are not hers to draft at all.
            fetched = {thread["id"] for thread in threads}
            threads = [thread for thread in threads if thread["id"] not in set(skip.get(key) or ())]
            text, carried = render_threads(key, record, threads, fresh)
            marks = carried + [thread for thread in record["open_threads"] if thread not in fetched]
            patrol.write_text("bot-pass", out, text)
            woke = patrol.wake(conn, "hermione", "bot-pass", "bot pass", text, out, now, tag=tag, shadow=shadow,
                               marks=marks, subject=key)
        except (FleetError, StoreError) as exc:
            woke = {"launched": False, "clean": False, "error": common.scrubbed_line(exc, 200)}
        passes.append({"pr": key, "file": patrol.file_path("bot-pass", out), **woke})
    return passes


# The round


def lineup_due(ts: int) -> bool:
    """Whether today's morning lineup should exist by now: a weekday, at or after its time, with no lineup file."""
    local = time.localtime(ts)
    if local.tm_wday not in config.LINEUP_WEEKDAYS or (local.tm_hour, local.tm_min) < config.LINEUP_AT:
        return False
    return not os.path.lexists(patrol.file_path("lineup", f"{patrol.local_day(ts)}.md"))


def catch_up_lineup(conn, ts: int, now: Optional[int]) -> Optional[dict]:
    """Today's lineup when its own job missed it, written by a round that could read GitHub. Never raises, so a
    lineup that fails again leaves the round as it was and the next round tries once more."""
    if not lineup_due(ts):
        return None
    try:
        return morning.lineup(conn, now)
    except (FleetError, StoreError, OSError) as exc:
        return {"ok": False, "error": common.scrubbed_line(exc, 200)}


def _store_only(conn, ts: int, now: Optional[int]) -> dict:
    """A round that could not read GitHub whole still finishes what only the store can for open PR follow-ups
    (followup.store_round), and routes nothing from the partial read. Its rows go to the outcomes; the counts go to the
    round row."""
    fu = followup.store_round(conn, ts, now)
    for row in fu["rows"]:
        patrol.append_row("map", OUTCOMES, {"ts": ts, **row})
    return {"live": fu["live"], "routed": 0, "errors": fu["errors"], "rows": len(fu["rows"])}


def run_round(conn, now: Optional[int] = None) -> dict:
    """One Map round. Returns its round row plus what it woke and sent again."""
    ts = patrol.stamp(now)
    shadow = patrol.shadow_on()
    # Before any GitHub read, so they run on every path, whatever shadow mode says. Neither ever raises.
    closing = closer.sweep(conn, now)
    cleaned = worktree.sweep_closed(conn, now)
    try:
        login = patrol.account()
        seen = patrol.fetch_prs()
    except FleetError as exc:
        error = common.scrubbed_line(exc, 200)
        fu = _store_only(conn, ts, now)
        patrol.append_row("map", ROUNDS, {"ts": ts, "ok": False, "shadow": shadow, "error": error, "model": False,
                                          "followups": fu, "closer": closing, "worktrees": cleaned})
        patrol.tell_ryan(conn, shadow, "map-failed", f"the Map could not read GitHub: {error}",
                         f"patrol:map-failed:{patrol.local_day(now)}", now)
        return {"ok": False, "error": error, "followups": fu, "closer": closing, "worktrees": cleaned}
    if not seen["complete"]:
        error = "GitHub's list of open PRs could not be read to its end, so the snapshot was left as it was"
        fu = _store_only(conn, ts, now)
        patrol.append_row("map", ROUNDS, {"ts": ts, "ok": False, "incomplete": True, "shadow": shadow, "error": error,
                                          "prs": len(seen["prs"]), "asked": len(seen["asked"]), "model": False,
                                          "followups": fu, "closer": closing, "worktrees": cleaned})
        patrol.tell_ryan(conn, shadow, "map-incomplete", f"the Map read only part of your PRs: {error}",
                         f"patrol:map-incomplete:{patrol.local_day(now)}", now)
        return {"ok": False, "incomplete": True, "error": error, "followups": fu, "closer": closing,
                "worktrees": cleaned}
    before = patrol.read_state("map", SNAPSHOT, None)
    baseline = not isinstance(before, dict) or before.get("account") != login or not isinstance(before.get("prs"), dict)
    rows = [] if baseline else changes(before, seen)
    # Before the snapshot is written: a round killed while it routes is computed again, and the store covers what it
    # routed, so no row is lost and nothing routes twice.
    fu = followup.patrol_round(conn, seen, ts, now, shadow, baseline)
    rows = followup.mark_covered(rows, before if not baseline else {}, seen, fu)
    rows += fu["rows"]
    # PRs whose repository name the patrol refuses: one row, only when their count changes, never naming the repository.
    refused = seen.get("refused", 0)
    if refused and refused != (before.get("refused") if isinstance(before, dict) else None):
        rows.append(_row("-", "PRs left out", patrol.refused_text(refused), ROUTINE))
    patrol.write_state("map", SNAPSHOT, {"account": login, "taken_at": ts, "prs": seen["prs"], "asked": seen["asked"],
                                         "refused": refused})
    for row in rows:
        patrol.append_row("map", OUTCOMES, {"ts": ts, **row})
    for_me = [row for row in rows if row["mark"] == FOR_ME]
    woke = None
    if for_me:
        out = f"round-{patrol.file_stamp(now)}.md"
        text = render_round(rows, seen, ts, followup.lineup_text(conn, now))
        patrol.write_text("map", out, text)
        for row in for_me:
            patrol.tell_ryan(conn, shadow, "for-me", f"{row['pr']}: {row['change']} ({row['detail']})",
                             f"patrol:map:{ts}:{row['pr']}:{row['change']}", now)
        try:
            woke = patrol.wake(conn, "ron", "map", "map round", text, out, now, shadow=shadow)
        except (FleetError, StoreError) as exc:
            woke = {"launched": False, "clean": False, "error": common.scrubbed_line(exc, 200)}
    if baseline:
        seed_bot_passes(seen, ts)
        passes = []
    else:
        passes = bot_passes(conn, seen, ts, now, shadow, skip=fu["covered"])
    resent = patrol.resend_pending(conn, shadow, now)
    lineup = catch_up_lineup(conn, ts, now)
    model = bool(woke and woke.get("launched")) or any(item.get("launched") for item in passes) or resent["launched"] \
        or bool(lineup and lineup.get("model")) or fu["model"]
    row = {"ts": ts, "ok": True, "shadow": shadow, "baseline": baseline, "prs": len(seen["prs"]),
           "asked": len(seen["asked"]), "refused": refused, "changes": len(rows), "for_me": len(for_me), "model": model,
           "followups": {"live": fu["live"], "routed": fu["routed"], "errors": fu["errors"]}, "closer": closing,
           "worktrees": cleaned}
    if lineup is not None:
        row["lineup"] = "written" if lineup.get("ok") else "failed"
    patrol.append_row("map", ROUNDS, row)
    return {**row, "woke": woke, "bot_passes": passes, "resent": resent, "lineup": lineup}


def main(argv: Optional[list] = None) -> int:
    return patrol.run_job("map", run_round, argv)


if __name__ == "__main__":
    sys.exit(main())
