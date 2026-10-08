"""Graceful model failover: a circuit breaker per model, and the model a desk runs on while its own is down.

A run's failure is classed only from the CLI's own structured output, never from anything the model wrote: Claude's
result event (api_error_status) and the error field of its assistant events, and the error that ended a Codex run
(a status field, or the HTTP status the CLI put in its error message). 429 is a rate limit, 529 an overload, 5xx an
outage (FAILOVER_STATUS_CLASSES). 401 and 403, and Claude's authentication and billing errors, are auth: the owner
is told and nothing fails over, since another model of the same account would fail the same way.

The breaker: FAILOVER_TRIP_FAILURES outage-class failures in a row mark a model down for FAILOVER_DOWN_SECONDS.
After that it is half-open: the next launch that wants it takes it as the one probe, and every other launch sees it
down for another FAILOVER_DOWN_SECONDS. A clean run closes it, an outage-class failure opens it again. The owner
gets one headmaster event when a model goes down and one when it comes back, keyed to the moment it went down, so
a reopen after a failed probe says nothing new. The state is one JSON file in the office state folder
(FAILOVER_STATE_FILE), rewritten whole (safefs.write_new) under FAILOVER_LOCK, a leaf lock: nothing else is taken
while it is held. A state that cannot be read counts as nothing down, so the breaker never stops a launch by itself.

The fallback is the next model Ollivander would pick for the desk's role: each pass he writes, per headless desk and
family, the names of its need and then of each cheaper need in his own pick order, blocked names left out
(FAILOVER_LADDERS_FILE). At launch no fallback dearer than the model the desk is approved on now is taken (_cap). A desk
falls back only in its own family, since the cross-family review depends on it. When every model of its family is down,
the desk waits: no run starts, its owl stays in its inbox, the owner hears once, and the Owl Post starts the run again
once one of those models is up or half-open (resume_waiting). Only while the owner's cross-family-failover switch is on
(common.opt_in_on) may a desk whose own family is all down run on the other family, and only a desk with launch settings
for it whose runs no review reads (can_flip): the review loop and the closer pick a reviewer and read its run by the
desk's registered family, so a build desk or a reviewer never flips. A review round whose author's latest run on the
task used the reviewer's own family is refused (_check_review), so a review is never same-family.
"""
from __future__ import annotations

import contextlib
import json
import re
from typing import Callable, Iterator, Optional

from hogwarts import capacity, db, owlery, pensieve, wands
from hogwarts.errors import StoreError

from fleet import common, config, safefs
from fleet.safefs import FleetError

OUTAGE_CLASSES = ("outage", "overload", "rate_limit")
CLASS_TEXT = {"outage": "an outage error", "overload": "an overloaded error", "rate_limit": "a rate limit error"}
# Claude's assistant event error field, as the CLI names it.
CLAUDE_ERROR_CLASSES = {"authentication_failed": "auth", "billing_error": "auth", "rate_limit": "rate_limit",
                        "server_error": "outage"}
# The status a Codex error carries: a status field, or the CLI's "status: NNN" or "NNN Reason Phrase" in its message.
STATUS_KEYS = ("status", "status_code", "http_status", "http_status_code")
STATUS_IN_ERROR = (re.compile(r"\bstatus(?: code)?[:=]?\s*([45][0-9]{2})\b"),
                   re.compile(r"\b([45][0-9]{2}) (?:Too Many Requests|Unauthorized|Forbidden|Internal Server Error|"
                              r"Bad Gateway|Service Unavailable|Gateway Timeout)\b"))
ERROR_TEXT_MAX = 4096


class ModelsDown(FleetError):
    """No model the desk may run is up, so it waits. key dedupes the owner's one event for this wait."""

    def __init__(self, message: str, key: str, family: str, models: tuple) -> None:
        super().__init__(message)
        self.key, self.family, self.models = key, family, models


def other_family(family: str) -> str:
    return "codex" if family == "claude" else "claude"


def model_key(family: str, model: str) -> str:
    return f"{family}:{model}"


# Classing a failed run


def _status_class(status: object) -> Optional[str]:
    return config.FAILOVER_STATUS_CLASSES.get(status) if type(status) is int else None


def classify(family: str, raw: bytes, exit_code: int, result: Optional[dict] = None) -> Optional[str]:
    """outage, overload, rate_limit or auth for a failed run whose CLI said so in its structured output, else None.
    result is Claude's result event (run_desk.claude_result). A run killed or timed out (exit code below 0) says
    nothing either way."""
    if exit_code < 0:
        return None
    if family == "claude":
        result = result or {}
        if exit_code == 0 and result.get("is_error") is not True:
            return None
        return _status_class(result.get("api_error_status")) or _claude_event_class(raw)
    return None if exit_code == 0 else _codex_class(raw)


def _events(raw: bytes) -> Iterator[dict]:
    for line in raw.split(b"\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _claude_event_class(raw: bytes) -> Optional[str]:
    """The class of the error field on the last assistant event that carries one."""
    found = None
    for event in _events(raw):
        if event.get("type") == "assistant" and isinstance(event.get("error"), str):
            found = CLAUDE_ERROR_CLASSES.get(event["error"])
    return found


def _codex_class(raw: bytes) -> Optional[str]:
    """The class of the error that ended a failed Codex run: its last turn.failed, else its last error event that no
    turn.completed followed, as run_desk reads the vendor limit."""
    turn_failed, last_error = None, None
    for event in _events(raw):
        kind = event.get("type")
        if kind == "turn.completed":
            last_error = None
        elif kind == "error":
            last_error = event
        elif kind == "turn.failed" and isinstance(event.get("error"), dict):
            turn_failed = event["error"]
    error = turn_failed if turn_failed is not None else last_error
    if error is None:
        return None
    for key in STATUS_KEYS:
        if _status_class(error.get(key)) is not None:
            return _status_class(error[key])
    message = error.get("message")
    if not isinstance(message, str):
        return None
    for pattern in STATUS_IN_ERROR:
        match = pattern.search(message[:ERROR_TEXT_MAX])
        if match is not None and _status_class(int(match.group(1))) is not None:
            return _status_class(int(match.group(1)))
    return None


# The state file


def _empty() -> dict:
    return {"models": {}, "notices": [], "runs": [], "waiting": {}}


def _valid(data: object) -> bool:
    return (isinstance(data, dict) and set(data) == set(_empty()) and isinstance(data["models"], dict)
            and isinstance(data["notices"], list) and isinstance(data["runs"], list)
            and isinstance(data["waiting"], dict))


def _read(state_fd: int) -> dict:
    """The breaker state, or an empty one when there is none or it does not read whole: nothing down."""
    try:
        data = common.strict_json(safefs.read_regular(state_fd, config.FAILOVER_STATE_FILE,
                                                      config.FAILOVER_STATE_MAX_BYTES, "failover state"))
    except (FleetError, OSError, UnicodeDecodeError, ValueError):
        return _empty()
    return data if _valid(data) else _empty()


def read_state() -> dict:
    """The breaker state as last written, without the lock: every write replaces the file whole."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
            return _read(fd)
    except (FleetError, OSError):
        return _empty()


@contextlib.contextmanager
def held_state() -> Iterator[dict]:
    """The breaker state under FAILOVER_LOCK, written back whole when the block changed it. Busy when the lock stays
    taken for FAILOVER_LOCK_WAIT_SECONDS."""
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
            safefs.held_lock(locks_fd, config.FAILOVER_LOCK, blocking=True,
                             timeout=config.FAILOVER_LOCK_WAIT_SECONDS), \
            safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
        state = _read(fd)
        before = json.dumps(state, sort_keys=True)
        yield state
        after = json.dumps(state, sort_keys=True)
        if after != before:
            safefs.write_new(fd, config.FAILOVER_STATE_FILE, (after + "\n").encode("ascii"))


def _entry(state: dict, family: str, model: str) -> dict:
    entry = state["models"].get(model_key(family, model))
    return entry if isinstance(entry, dict) else {}


def is_down(state: dict, family: str, model: str, now: int) -> bool:
    """Open, or half-open with another run's probe on it."""
    until = _entry(state, family, model).get("until")
    return type(until) is int and now < until


def _take(state: dict, family: str, model: str, now: int, claim: bool) -> bool:
    """Whether a launch may use the model now. With claim, a half-open model becomes this launch's probe, and down
    for every other launch until the probe ends, or for as long as a run can last (RUNNING_WINDOW_SECONDS)."""
    if is_down(state, family, model, now):
        return False
    entry = _entry(state, family, model)
    if claim and entry.get("since") is not None:
        entry["until"] = now + max(config.FAILOVER_DOWN_SECONDS, config.RUNNING_WINDOW_SECONDS)
    return True


# The breaker


def record(conn, family: str, model: str, failure: Optional[str], clean: bool, now: Optional[int] = None,
           tell: bool = True) -> None:
    """Count a run's end toward its model's breaker: an outage-class failure counts up, a clean run closes it, and
    anything else (auth, a timeout, a failure the CLI did not class) leaves it as it was. tell False (the patrol's
    shadow mode) leaves a down or up for the next flush, the Owl Post's at the latest. Never raises."""
    if failure not in OUTAGE_CLASSES and not clean:
        return
    ts = common.now_stamp(now)
    key = model_key(family, model)
    if clean and key not in read_state()["models"]:
        return  # a clean run on a model with nothing counted changes nothing, so no lock is taken
    with contextlib.suppress(FleetError, OSError):
        with held_state() as state:
            entry = _entry(state, family, model)
            if clean:
                if entry.get("since") is not None:
                    state["notices"].append({"kind": "up", "family": family, "model": model,
                                             "since": entry["since"], "class": entry.get("class")})
                state["models"].pop(key, None)
            else:
                entry = {**entry, "failures": int(entry.get("failures") or 0) + 1, "class": failure}
                if entry.get("since") is None and entry["failures"] >= config.FAILOVER_TRIP_FAILURES:
                    entry["since"] = ts
                    state["notices"].append({"kind": "down", "family": family, "model": model, "since": ts,
                                             "class": failure})
                if entry.get("since") is not None:
                    # Opened, or opened again after its probe; never shortening a probe another run holds.
                    held = entry.get("until") if type(entry.get("until")) is int else 0
                    entry["until"] = max(held, ts + config.FAILOVER_DOWN_SECONDS)
                entry.setdefault("since", None)
                entry.setdefault("until", None)
                state["models"][key] = entry
            if tell:
                flush(conn, state, ts)


def flush(conn, state: dict, now: int) -> None:
    """Tell the owner of each down or up the state still holds, oldest first, and drop each once the store has it.
    Its dedupe key is the moment the model went down, so one told already is never told twice."""
    while state["notices"]:
        notice = state["notices"][0]
        family, model, since = notice.get("family"), notice.get("model"), notice.get("since")
        if notice.get("kind") not in ("down", "up") or family not in db.MODEL_FAMILIES or not isinstance(model, str) \
                or wands.MODEL_NAME.fullmatch(model) is None or type(since) is not int:
            state["notices"].pop(0)
            continue
        minutes = config.FAILOVER_DOWN_SECONDS // 60
        if notice["kind"] == "down":
            summary = (f"{model} ({family}) is down: {config.FAILOVER_TRIP_FAILURES} runs in a row ended with"
                       f" {CLASS_TEXT.get(notice.get('class'), 'an outage error')}. Its desks fall back within their"
                       f" family, and one run tries it again in about {minutes} minutes.")
        else:
            summary = f"{model} ({family}) is back: a run on it ended cleanly, so its desks run on it again."
        try:
            pensieve.add_event(conn, config.OLLIVANDER_DESK, f"failover.{notice['kind']}", "headmaster", summary,
                               dedupe_key=f"failover:{notice['kind']}:{family}:{model}:{since}", now=now)
        except StoreError:
            return  # kept for the next flush
        state["notices"].pop(0)


# Ladders, from Ollivander


def write_ladders(ladders: dict, now: int) -> None:
    """Keep Ollivander's ladders: {desk: {family: [{model, effort, line}]}}. A family left out of a desk's entry (its
    catalog could not be read this pass) keeps the ladder it had."""
    with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
        kept = _read_ladders(fd)
        for desk, families in ladders.items():
            kept[desk] = {**kept.get(desk, {}), **families}
        data = {"at": now, "desks": kept}
        safefs.write_new(fd, config.FAILOVER_LADDERS_FILE, (json.dumps(data, sort_keys=True) + "\n").encode("ascii"))


def _read_ladders(state_fd: int) -> dict:
    try:
        data = common.strict_json(safefs.read_regular(state_fd, config.FAILOVER_LADDERS_FILE,
                                                      config.FAILOVER_STATE_MAX_BYTES, "model ladders"))
    except (FleetError, OSError, UnicodeDecodeError, ValueError):
        return {}
    desks = data.get("desks") if isinstance(data, dict) else None
    return desks if isinstance(desks, dict) else {}


def ladder(desk: str, family: str) -> list:
    """The desk's ladder in a family, as Ollivander last wrote it: [{model, effort, line}], only safe names, efforts
    and lines."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
            found = _read_ladders(fd).get(desk, {})
    except (FleetError, OSError):
        return []
    rows = found.get(family) if isinstance(found, dict) else None
    entries = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("model"), str):
            continue
        effort, line = row.get("effort"), row.get("line")
        if wands.MODEL_NAME.fullmatch(row["model"]) is None or (effort is not None and effort not in db.MODEL_EFFORTS) \
                or line not in db.MODEL_NEEDS:
            continue
        entries.append({"model": row["model"], "effort": effort, "line": line})
    return entries


# Choosing the model a launch runs on


def cross_family_on() -> bool:
    return common.opt_in_on(config.CROSS_FAMILY_FAILOVER_FILE)


def can_launch(desk: str, family: str) -> bool:
    """Whether run_desk has launch settings for the desk in that family: always its own, the other only when the
    office config gives it that family's tools and budget (Claude) or access (Codex)."""
    if family == "claude":
        return all(desk in table for table in (config.CLAUDE_TOOLS, config.MAX_BUDGET_USD, config.CLAUDE_READ_DIRS))
    return desk in config.CODEX_ACCESS


def can_flip(desk: str, family: str) -> bool:
    """Whether the desk may ever run on the other family than its own. Never a build desk or a reviewer: the review
    loop and the closer read those runs, and pick their reviewer, by the desk's registered family."""
    return (desk not in config.WORKTREE_DESKS and desk not in config.REVIEWER_FOR_FAMILY.values()
            and can_launch(desk, other_family(family)))


def _blocked(conn, family: str, model: str) -> bool:
    """Blocked here by name, or a Claude alias that ever ran as a blocked id; unreadable blocklists count as blocked."""
    try:
        blocked = wands.check_blocklist(config.BLOCKED_MODEL_PREFIXES)
        if wands.blocked_by(model, blocked) is not None:
            return True
        return family == "claude" and bool(blocked) and wands.blocked_resolution(conn, model, blocked) is not None
    except StoreError:
        return True


def _line_of(conn, family: str, model: str) -> Optional[str]:
    """The class a model costs, as Ollivander files it: Ryan's filing, then CLAUDE_LINES, then its catalog line."""
    filed = wands.ryan_lines(conn).get(model)
    if filed is not None and filed["line"] in db.MODEL_NEEDS:
        return filed["line"]
    if family == "claude" and wands.base_alias(model) in config.CLAUDE_LINES:
        return config.CLAUDE_LINES[wands.base_alias(model)]
    row = db.fetch_one(conn, "SELECT line FROM model_catalog WHERE family = ? AND name = ?", (family, model))
    return row["line"] if row is not None and row["line"] in db.MODEL_NEEDS else None


def _cap(conn, desk: str, family: str, model: str) -> str:
    """The dearest class a fallback may cost: that of the model the desk is approved on now, read at launch so a
    cheaper pin or revert since Ollivander's last pass counts. When that cannot be told, only the cheapest class."""
    row = wands.get_desk_model(conn, desk)
    line = row["line"] if row is not None and row["model"] == model and row["line"] in db.MODEL_NEEDS else None
    return line or _line_of(conn, family, model) or config.COST_ORDER[0]


def model_family(conn, name: str) -> Optional[str]:
    """The family a model name belongs to, from the store: a Claude line alias or full id, the Codex CLI default
    (run_desk.CODEX_DEFAULT), or the one family whose catalog ever listed it. None when that is not known."""
    base = wands.base_alias(name)
    if base in config.CLAUDE_LINES or wands.CLAUDE_ID.fullmatch(base) is not None:
        return "claude"
    if name == "codex-default":
        return "codex"
    rows = db.fetch_all(conn, "SELECT DISTINCT family FROM model_catalog WHERE name = ?", (base,))
    return rows[0]["family"] if len(rows) == 1 else None


def _check_review(conn, plan: dict, now: int) -> None:
    """Refuse a review round that would not be cross-family: its author's latest run on the task used the reviewer's
    family, or, for an author that can flip, a model whose family the store cannot tell. Read from the launch rows,
    so it holds whatever the breaker state says."""
    task_id = plan["review_round"]["task_id"]
    row = db.fetch_one(conn, "SELECT desk, model FROM run_launches WHERE task_id = ?"
                             " ORDER BY launched_at DESC, rowid DESC LIMIT 1", (task_id,))
    if row is None:
        return
    author = pensieve.get_desk(conn, row["desk"])["family"]
    ran = model_family(conn, row["model"])
    if ran is None and not can_flip(row["desk"], author):
        ran = author
    if ran is not None and ran != plan["desk_family"]:
        return
    raise ModelsDown(f"{plan['desk']} waits: the latest run on task {task_id} used {row['model']}, so a review by"
                     f" {plan['desk']} would not be cross-family. Review it by hand, or run its author again",
                     f"failover:wait:{plan['desk']}:review:{task_id}:{now // config.DAY_SECONDS}", plan["desk_family"],
                     ())


def choose(conn, plan: dict, now: Optional[int] = None, claim: bool = False) -> Optional[dict]:
    """The model the planned run should use instead of its own, {model, effort, family, from}, or None when its own
    is up (or half-open). claim (the last plan, under the launch lock) makes a half-open model this launch's probe.
    ModelsDown when nothing it may run is up, or when a review round would not be cross-family."""
    ts = common.now_stamp(now)
    desk, own, model = plan["desk"], plan["desk_family"], plan["model"]
    if plan.get("review_round"):
        _check_review(conn, plan, ts)
    seen = read_state()
    if _entry(seen, own, model).get("since") is None:
        return None  # never went down, so there is nothing to claim and no lock to take
    try:
        with (held_state() if claim else contextlib.nullcontext(seen)) as state:
            if _take(state, own, model, ts, claim):
                return None
            tried = [(own, model)]
            # The other family only by the owner's switch, and only for a desk whose runs no review reads.
            flip = can_flip(desk, own) and cross_family_on()
            cap = config.COST_ORDER.index(_cap(conn, desk, own, model))
            for family in (own, other_family(own)) if flip else (own,):
                for entry in ladder(desk, family):
                    name = entry["model"]
                    if (family, name) == (own, model) or config.COST_ORDER.index(entry["line"]) > cap \
                            or _blocked(conn, family, name):
                        continue
                    tried.append((family, name))
                    if _take(state, family, name, ts, claim):
                        return {"model": name, "effort": entry["effort"], "family": family, "from": model}
    except (FleetError, OSError):
        return None  # the breaker could not be read: the run goes on its own model
    # Keyed to the latest moment one of them went down, so the owner hears once per outage, not once per owl.
    since = max([value for value in (_entry(state, family, name).get("since") for family, name in tried)
                 if type(value) is int] or [0])
    key = f"failover:wait:{desk}:{own}:{since or 'day' + str(ts // config.DAY_SECONDS)}"
    names = ", ".join(name for _, name in tried)
    scope = "either family" if flip else f"its {own} family"
    raise ModelsDown(f"{desk} waits: every model of {scope} it may run is down ({names}). Its owl stays in its"
                     " inbox and runs again once one is back", key, own, tuple(tried))


# Fallback runs and waiting owls


def note_run(run_id: str, desk: str, family: str, came_from: str) -> None:
    """Keep that this run is a fallback, before its process starts, so the moved check skips it. Never raises."""
    with contextlib.suppress(FleetError, OSError):
        with held_state() as state:
            state["runs"].append({"run": run_id, "desk": desk, "family": family, "from": came_from})
            del state["runs"][:-config.FAILOVER_RUNS_KEPT]


def fallback_runs() -> set:
    return {item["run"] for item in read_state()["runs"] if isinstance(item, dict) and isinstance(item.get("run"), str)}


def note_waiting(desk: str, owl_id: str, down: ModelsDown, now: Optional[int] = None) -> None:
    """Keep an owl whose run waits for a model, for resume_waiting. An owl that waited before keeps its first wait,
    which bounds how long it is started again. Never raises."""
    with contextlib.suppress(FleetError, OSError):
        with held_state() as state:
            prior = state["waiting"].get(owl_id)
            prior = prior if isinstance(prior, dict) else {}
            first, resumes = prior.get("first"), prior.get("resumes")
            state["waiting"][owl_id] = {"desk": desk, "models": [list(pair) for pair in down.models],
                                        "first": first if type(first) is int else common.now_stamp(now),
                                        "resumed": None, "resumes": resumes if type(resumes) is int else 0}


def resume_waiting(conn, spawn: Callable[[str, str], None], now: Optional[int] = None) -> list:
    """Start the run of each waiting owl once a model it waited for is up or half-open. The entry stays, marked resumed,
    so no pass starts it again while its run may still go (see _due), and a run that finds them down again waits again;
    at most FAILOVER_MAX_RESUMES starts in all. An owl acked or answered since is dropped, and one still waiting
    FAILOVER_WAIT_LIMIT_SECONDS after its first wait is dropped once the owner's event is in. Also tells the owner of
    any down or up a breaker update left untold. Returns the owls started."""
    ts = common.now_stamp(now)
    ready = []
    seen = read_state()
    if not seen["waiting"] and not seen["notices"]:
        return []  # the common case takes no lock
    try:
        with held_state() as state:
            flush(conn, state, ts)
            for owl_id, item in list(state["waiting"].items()):
                if not isinstance(item, dict) or not _still_waiting(conn, owl_id, item, ts):
                    state["waiting"].pop(owl_id)
                elif _due(item, ts) and any(not is_down(state, family, name, ts) for family, name in item["models"]):
                    item["resumed"], item["resumes"] = ts, item["resumes"] + 1
                    ready.append((item["desk"], owl_id))
    except (FleetError, OSError, StoreError):
        return []
    started, failed = [], []
    for desk, owl_id in ready:
        try:
            spawn(desk, owl_id)
            started.append(owl_id)
        except (FleetError, OSError):
            failed.append(owl_id)
    if failed:
        # Not started: waiting again, so the next pass tries it. A launch refused later tells the owner itself.
        with contextlib.suppress(FleetError, OSError), held_state() as state:
            for owl_id in failed:
                item = state["waiting"].get(owl_id)
                if isinstance(item, dict) and type(item.get("resumes")) is int:
                    item["resumed"], item["resumes"] = None, max(0, item["resumes"] - 1)
    return started


def _due(item: dict, now: int) -> bool:
    """Whether a waiting owl may be started now: never started for this wait, or started so long ago that its run
    must have ended without reaching its model (a crash before the start, a launch refused), and fewer than
    FAILOVER_MAX_RESUMES times in all."""
    resumed = item.get("resumed")
    return (item["resumes"] < config.FAILOVER_MAX_RESUMES
            and (resumed is None or (type(resumed) is int and now - resumed >= config.RUNNING_WINDOW_SECONDS)))


def _still_waiting(conn, owl_id: str, item: dict, now: int) -> bool:
    """Whether a waiting entry stays: its owl unacked in its desk's inbox, a model to wait for, within the limit."""
    first, models, desk = item.get("first"), item.get("models"), item.get("desk")
    if type(first) is not int or not isinstance(models, list) or not models or not isinstance(desk, str) \
            or not all(isinstance(pair, list) and len(pair) == 2 for pair in models) \
            or type(item.get("resumes")) is not int:
        return False  # nothing to wait for: the owner was told and sends it again
    if not any(owl["id"] == owl_id for owl in owlery.inbox(conn, desk)):
        return False
    if now - first < config.FAILOVER_WAIT_LIMIT_SECONDS:
        return True
    try:
        pensieve.add_event(conn, desk, "failover.wait-ended", "headmaster",
                           f"{desk}'s owl {owl_id} is still in its inbox {config.FAILOVER_WAIT_LIMIT_SECONDS // 3600}"
                           " hours after its models went down, and is no longer started again by itself; send it"
                           " again once one is back.", dedupe_key=f"failover:wait-ended:{owl_id}:{first}", now=now)
    except StoreError:
        return True  # kept until the owner is told, under the same dedupe key
    return False


def auth_note(desk: str, family: str, now: Optional[int] = None) -> tuple:
    """(summary, dedupe key) of the owner's one event a day per desk and family for a run its CLI could not sign in or
    bill for. Nothing fails over: another model of the same account fails the same way."""
    day_start, _ = capacity.day_bounds(common.now_stamp(now), config.CAP_RESET_UTC_SECONDS)
    return (f"{desk}'s {family} CLI could not sign in or bill its run. No other model is tried, since they share the"
            " account: sign the CLI in again, then send the owl again.", f"failover:auth:{desk}:{family}:{day_start}")


def clean(family: str, raw: bytes, exit_code: int, whole: bool, result: Optional[dict] = None) -> bool:
    """Proof the model answered: exit 0, output read whole, and Claude's result event with is_error false, or a Codex
    turn.completed. Only this closes a breaker."""
    if exit_code != 0 or not whole:
        return False
    if family == "claude":
        return bool(result) and result.get("is_error") is False
    return any(event.get("type") == "turn.completed" for event in _events(raw))


def retryable(result: dict) -> bool:
    """A run its model's outage, overload or rate limit cut off, which main runs again from its checkpoint."""
    return result.get("failure_class") in OUTAGE_CLASSES
