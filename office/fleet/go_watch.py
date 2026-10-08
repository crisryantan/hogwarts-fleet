"""Go updates: one line to Ryan each time one of McGonagall's open go tasks changes where it stands, so he hears it
without asking. Off by default: on only while the office file config.GO_UPDATES_FILE holds exactly "on", read through
the shared switch reader (common.opt_in_on). Read and report only: nothing here writes the store, starts a desk or
changes what any run may do.

- Source: go_status.open_go_tasks, the view McGonagall's hooks show, reduced per go task to a state key built only
  from store fields: its newest build, the state from the fixed table below (task status, the build's PR binding and
  proven close, its live review round and that round's verdict, the newest result owl from its desk, Ollivander's held
  runs, and the loud events a state stands for), the round and verdict that state is about, and the id of the event
  a state is made of (a go refusal, a tooling block, a refused round). No timestamp or wording is in the key, so a
  cosmetic change never pings.
- Kept: the last key sent for each go task, in one state file in the office folder GO_WATCH_DIR, read and written
  whole under its own lock. A pass compares the current keys with it: a go task whose key changed gets one line, one
  that left the open set gets one final line and its entry goes. A pass with no change sends and writes nothing. A
  state file, a store or a held-runs folder that cannot be read whole sends nothing and keeps the file as it was.
- The line is `<go id> / <build id or "no build">: <state>. <action>`, both from the fixed table (STATES), never from
  an owl or a model, scrubbed and cut to GO_WATCH_LINE_CHARS. It goes through phone.send: the overlay's command when
  one is configured, else the macOS notification.
- Never twice: before a line is sent, a marker named by a digest of (epoch, go id, key) is published create-exclusive
  in the folder, then records the outcome as phone.py's do, so a rerun, a second watcher or a kill never sends it
  again. The first pass with the switch on only records a baseline (with a new epoch), so there is no backlog; with
  the switch off the state file is removed, so turning it on again starts from a new baseline.
- At most GO_WATCH_MAX_PER_PASS lines ping one by one each pass; any more go as one summary ping naming the count.
- No double ping: phone.deliver skips a loud event whose kind a state here stands for (COVERS) when it is on a go
  task this file keeps, or its build, that go task stands in that state now, and that state's own line was sent
  (watching, covers). Every other loud event, of these tasks or any other, pings as before, and so does every
  one while the kept state or the store cannot be read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from typing import Optional

from hogwarts import capacity, db, followups, ids, pensieve

from fleet import common, config, go_status, markers, phone, safefs
from fleet.safefs import FleetError

STATE = "state.json"
LOCK = "go-watch.lock"
STATE_MAX_BYTES = 1 << 20
# A line's marker: <epoch>.<go task id>.<digest of the epoch, go task id and key>.
MARKER = re.compile(r"([0-9a-f]{8})\.(tk_[0-9a-f]{16})\.[0-9a-f]{32}")
KEY_FIELDS = ("build", "state", "round", "verdict", "event")
# The fixed table: state -> (what the line calls it, what Ryan does). Nothing else is ever put in a line.
STATES = {
    "confirmed": ("go confirmed, build started", "Nothing for you."),
    "refused": ("go refused", "Fix the TASK.md as the refusal says, then send the go again."),
    "handoff": ("Harry handed off, review running", "Nothing for you."),
    "changes": ("review CHANGES, fix round started", "Nothing for you."),
    "pass": ("review PASS", "Nothing for you; the draft PR opens next if auto-draft-pr is on, otherwise push it"
                            " yourself."),
    "headmaster": ("review HEADMASTER", "Decide: read the review and tell McGonagall."),
    "tooling": ("blocked on tooling (tries spent)", "Look at the review's BLOCKED-ON-TOOLING line; the fix is usually"
                                                    " in the sandbox or the check command."),
    "round-cap": ("round cap hit", "Decide: allow another round or take it over."),
    "held": ("Ollivander stop holding a build", "Run castle ollivander clear once the CLI works."),
    "draft-pr": ("draft PR opened", "Review and merge the PR when you're happy."),
    "merged": ("merged", "Nothing for you; auto-close checks it."),
    "closed": ("closed", "Nothing for you."),
}
# The loud events a state above stands for, and that state. phone.deliver leaves one to go updates while its go task
# stands in that state; any other loud kind (a review that stopped, a failed push, the orchestrator, Ollivander's
# stop) still pings itself.
COVERS = {"go.refused": "refused", "review.headmaster": "headmaster", "review.round-cap": "round-cap",
          "review.loop-stopped": "round-cap", "review.blocked-on-tooling": "tooling", "review.ready-for-push": "pass",
          "push.draft-pr": "draft-pr"}


def on() -> bool:
    return common.opt_in_on(config.GO_UPDATES_FILE)


# Where each go task stands


def _newest(conn, task_id: str, kinds: tuple) -> Optional[dict]:
    """The newest loud event of these kinds on the task, or None."""
    marks = ", ".join("?" for _ in kinds)
    return db.fetch_one(conn, f"SELECT id, ts, kind FROM events WHERE task_id = ? AND verdict = 'headmaster'"
                              f" AND kind IN ({marks}) ORDER BY id DESC LIMIT 1", (task_id, *kinds))


def _key(build: Optional[str], state: str, round_no: int = 0, verdict: Optional[str] = None,
         event: Optional[dict] = None) -> dict:
    return {"build": build, "state": state, "round": round_no, "verdict": verdict,
            "event": None if event is None else int(event["id"])}


def _link(pr: Optional[dict]) -> Optional[str]:
    """A bound PR's URL, only when it is exactly a GitHub pull request URL."""
    url = None if pr is None else pr.get("url")
    return url if isinstance(url, str) and phone.PR_LINK.fullmatch(url) else None


def _build_state(conn, build: dict, held: set) -> tuple:
    """(key, PR link) for a go task's newest build, from its store rows alone."""
    build_id = build["id"]
    pr = followups.pr_for_task(conn, build_id)
    if pensieve.task_closure(conn, build_id) is not None:
        return _key(build_id, "merged"), _link(pr)
    if build["status"] == "closed":  # closed by hand, or with its go task, with no proven merge
        return _key(build_id, "closed"), None
    if build_id in held:
        return _key(build_id, "held"), None
    live = [row for row in capacity.review_rounds(conn, build_id) if row["superseded_by"] is None]
    latest = live[-1] if live else None
    round_no, round_at = (latest["round"], latest["created_at"]) if latest else (0, 0)
    handoff = db.fetch_one(conn, "SELECT COALESCE(MAX(created_at), 0) AS at FROM owls WHERE task_id = ? AND sender = ?"
                                 " AND kind = 'result'", (build_id, build["desk"]))["at"]
    since = max(round_at, handoff)
    # The newer of a tooling block and a refused or stopped round, by event id, after the newest handoff and round is
    # where it stands until the next handoff.
    stop = _newest(conn, build_id, ("review.blocked-on-tooling", "review.round-cap", "review.loop-stopped"))
    if stop is not None and stop["ts"] >= since:
        return _key(build_id, COVERS[stop["kind"]], round_no, event=stop), None
    if handoff > round_at:
        return _key(build_id, "handoff", round_no + 1), None  # the round this handoff opens next
    if latest is None:
        return _key(build_id, "confirmed"), None
    if pr is not None and pr["opened_at"] >= round_at:
        return _key(build_id, "draft-pr", round_no, latest["verdict"]), _link(pr)
    if not latest["has_verdict"]:
        return _key(build_id, "handoff", round_no), None
    verdict = latest["verdict"]
    if verdict == "PASS":
        return _key(build_id, "pass", round_no, verdict), None
    if verdict == "HEADMASTER":
        return _key(build_id, "headmaster", round_no, verdict), None
    group = latest.get("followup_id")
    cap = config.REVIEW_ROUND_CAP if group is None else config.FOLLOWUP_ROUND_CAP
    if capacity.needs_allowance(conn, build_id, cap, followup_id=group):
        return _key(build_id, "round-cap", round_no, verdict), None
    return _key(build_id, "changes", round_no, verdict), None


def _go_state(conn, item: dict) -> tuple:
    """(key, PR link) for one open go task: its newest build's state, or its go's refusal when that is newer."""
    go = item["task"]
    builds = [child for child in item["children"] if child["desk"] in config.WORKTREE_DESKS]
    refusal = _newest(conn, go["id"], ("go.refused",))
    if not builds:
        return (_key(None, "refused", event=refusal) if refusal is not None else _key(None, "confirmed")), None
    build = builds[-1]
    key, link = _build_state(conn, build, item["held"])
    if refusal is not None and key["state"] != "merged" and refusal["ts"] >= _latest_at(conn, build):
        return _key(build["id"], "refused", event=refusal), None
    return key, link


def _latest_at(conn, build: dict) -> int:
    """When the build last moved, by its store rows: made, a round opened, a handoff, a loud event or its PR."""
    row = db.fetch_one(conn, "SELECT MAX(COALESCE((SELECT MAX(created_at) FROM review_rounds WHERE task_id = :id), 0),"
                             " COALESCE((SELECT MAX(created_at) FROM owls WHERE task_id = :id AND kind = 'result'), 0),"
                             " COALESCE((SELECT MAX(ts) FROM events WHERE task_id = :id"
                             " AND verdict = 'headmaster'), 0),"
                             " COALESCE((SELECT opened_at FROM task_prs WHERE task_id = :id), 0)) AS at",
                       {"id": build["id"]})
    return max(int(build["created_at"]), int(row["at"]))


def current(conn) -> dict:
    """{go task id: (key, PR link)} for every open go task, oldest first, read in one store snapshot. Raises when the
    store or the held runs cannot be read, so a partial read never stands for where things are."""
    with db.snapshot(conn):
        items = go_status.open_go_tasks(conn, strict=True)
        return {item["task"]["id"]: _go_state(conn, item) for item in reversed(items)}


def line(go_id: str, key: dict) -> str:
    label, action = STATES[key["state"]]
    return common.scrubbed_line(f"{go_id} / {key['build'] or 'no build'}: {label}. {action}",
                                config.GO_WATCH_LINE_CHARS)


# What was sent


def _valid_key(key: object) -> bool:
    return (isinstance(key, dict) and set(key) == set(KEY_FIELDS) and isinstance(key["state"], str)
            and key["state"] in STATES
            and (key["build"] is None or (isinstance(key["build"], str)
                                          and ids.PATTERNS["task"].fullmatch(key["build"]) is not None))
            and type(key["round"]) is int and key["round"] >= 0
            and (key["verdict"] is None or key["verdict"] in db.REVIEW_VERDICTS)
            and (key["event"] is None or (type(key["event"]) is int and key["event"] > 0)))


def _read_state(fd: int) -> Optional[dict]:
    """The kept state, None when there is none yet, or FleetError when it cannot be read whole: then nothing is sent
    and nothing replaces it until Ryan looks at the folder."""
    try:
        raw = safefs.read_regular(fd, STATE, STATE_MAX_BYTES, "the go watch state")
    except safefs.Missing:
        return None
    try:
        data = common.strict_json(raw)
    except ValueError:
        data = None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(data, dict) or set(data) != {"state", "epoch", "tasks"} or data["state"] != "watching" \
            or not isinstance(data["epoch"], str) or re.fullmatch(r"[0-9a-f]{8}", data["epoch"]) is None \
            or not isinstance(tasks, dict) or not all(isinstance(go, str) and ids.PATTERNS["task"].fullmatch(go)
                                                      and _valid_key(key) for go, key in tasks.items()):
        raise FleetError("the go watch state cannot be read, so no go update was sent")
    return data


def _write_state(fd: int, epoch: str, tasks: dict) -> None:
    data = {"state": "watching", "epoch": epoch, "tasks": tasks}
    safefs.write_new(fd, STATE, (json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii"))


def _marker(epoch: str, go_id: str, key: dict) -> str:
    digest = hashlib.sha256(json.dumps([epoch, go_id, key], sort_keys=True).encode("ascii")).hexdigest()[:32]
    return f"{epoch}.{go_id}.{digest}"


def _prune(fd: int, epoch: str, keep: set) -> None:
    """Drop the markers of an older epoch and of go tasks no longer kept: none of them can be sent again."""
    for name in os.listdir(fd):
        match = MARKER.fullmatch(name)
        if match is not None and (match.group(1) != epoch or match.group(2) not in keep):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass


# A pass


def watch(conn) -> list:
    """One pass (see the module notes). Returns one outcome per line looked at. Raises when what it reads cannot be
    read whole; then nothing was sent or written. With the switch off it only forgets the kept state."""
    if not on():
        _forget()
        return []
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd:
        try:
            with safefs.held_lock(locks_fd, LOCK, blocking=False), \
                    safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR, create=True) as fd:
                return _watch(conn, fd)
        except safefs.Busy:
            return ["another go watch is running"]


def _forget() -> None:
    """The switch is off: the kept state goes, under the watcher's lock, so the next pass with it on starts from a new
    baseline. A pass that holds the lock reads the switch again before it writes, and forgets it then itself."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, "locks") as locks_fd, \
                safefs.held_lock(locks_fd, LOCK, blocking=False), \
                safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR) as fd:
            _unlink_state(fd)
    except (safefs.Missing, safefs.Busy):
        pass


def _unlink_state(fd: int) -> None:
    try:
        os.unlink(STATE, dir_fd=fd)
    except FileNotFoundError:
        pass


def _watch(conn, fd: int) -> list:
    kept = _read_state(fd)
    found = current(conn)
    keys = {go_id: key for go_id, (key, _) in found.items()}
    if kept is None:  # the first pass with the switch on: no backlog
        if not on():
            return []
        epoch = secrets.token_hex(4)
        _write_state(fd, epoch, keys)
        _prune(fd, epoch, set(keys))
        return []
    epoch, before = kept["epoch"], kept["tasks"]
    changed = [(go_id, key, link) for go_id, (key, link) in found.items() if before.get(go_id) != key]
    changed += [(go_id, _key(key["build"], "closed"), None) for go_id, key in before.items() if go_id not in found]
    if not changed:
        return []
    outcomes, batched = [], []
    for go_id, key, link in changed:
        name = _marker(epoch, go_id, key)
        if not markers.publish(fd, name, {"state": "sending"}):
            continue  # another watcher, or this one before a kill, took it: never twice
        if len(outcomes) >= config.GO_WATCH_MAX_PER_PASS:
            batched.append(name)
            continue
        outcome = phone.send({"event_id": 0, "kind": "go.update", "task_id": go_id, "line": line(go_id, key),
                              "pr_link": link})
        markers.replace(fd, name, outcome)
        outcomes.append(outcome["state"])
    if batched:
        outcome = phone.send({"event_id": 0, "kind": "go.update", "task_id": None, "pr_link": None,
                              "line": f"{len(batched)} more go tasks changed where they stand; castle task builds"
                                      " shows each one"})
        for name in batched:
            markers.replace(fd, name, {**outcome, "batched": True})
        outcomes.append(f"batched {len(batched)}")
    if not on():  # switched off during this pass: forget, as an off pass would have
        _unlink_state(fd)
        return outcomes
    _write_state(fd, epoch, keys)
    _prune(fd, epoch, set(keys))
    return outcomes


# What phone.deliver leaves to go updates


def watching(conn) -> dict:
    """{task id: the state its go task stands in now} for each go task the kept state names and its build, only where
    that state's own line was sent (its marker says sent, not batched, sending or undelivered). Empty while the switch
    is off, before the first pass, or when anything cannot be read whole: then phone.deliver pings everything as
    before. A loud event that lands before its line is sent pings too. Never raises."""
    try:
        if not on():
            return {}
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_WATCH_DIR) as fd:
            kept = _read_state(fd)
            if kept is None:
                return {}
            found = current(conn)
            states = {}
            for go_id in set(kept["tasks"]) & set(found):
                key = found[go_id][0]
                sent = markers.read(fd, _marker(kept["epoch"], go_id, key))
                if sent is None or sent.get("state") != "sent" or sent.get("batched"):
                    continue  # no line of its own reached Ryan yet for where it stands now
                for task_id in (go_id, key["build"], kept["tasks"][go_id]["build"]):
                    if task_id is not None:
                        states[task_id] = key["state"]
            return states
    except Exception:  # noqa: BLE001 - a go update that cannot be read never holds back a loud event
        return {}


def covers(event: dict, states: dict) -> bool:
    """Whether a go update stands for this loud event, so phone.deliver does not ping it too: its task's go task stands
    now in the state the event's kind tells, and that line was sent. An event its go task has moved on from, or whose
    line was not sent, pings as before."""
    state = COVERS.get(event["kind"])
    return state is not None and states.get(event["task_id"]) == state
