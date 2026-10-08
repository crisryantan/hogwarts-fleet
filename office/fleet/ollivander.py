"""Ollivander - Model Keeper: keeps each desk on the model that fits its role.

A desk's role card (office desks/<desk>/role.json) says what it needs: a frontier, workhorse or fast
model, at an effort, with one line of why. Ollivander picks by that need, never by model line, and
never changes a desk's family, because the cross-family review rule depends on it.

One pass:
1. CLI updates, only while Ryan keeps office desks/ollivander/update-clis: claude update and
   brew upgrade --cask codex, output to the office logs. An update marker in the office state folder
   stops headless launches from before the first update until every check passes. Then checks: both
   --version commands and run_desk's dry run for every enabled headless desk. Any failure, or a new
   Codex version (its sandbox boundary was proven on one version), writes the stop file, so run_desk
   launches no headless desk until Ryan runs castle ollivander clear. A new Claude Code version is a fyi.
   The update lock (config.UPDATE_LOCK) is held exclusively from before the marker is written until the
   checks end and the marker is gone. run_desk holds it shared from its last stop check until the desk's
   process has exited, and hands it to that process, so no launch can pass its check and then start a
   binary that an update is replacing, and no update replaces a binary while a desk still runs it.
   Lock order: Ollivander takes ollivander.lock, then the update lock, and never a desk lock; run_desk
   takes a run slot of the desk (after the review lock, in a review), then the desk's launch lock, then
   the update lock without waiting. No holder of the update lock ever waits for another lock, so no
   deadlock can form. When runs hold it for longer than UPDATE_LOCK_WAIT_SECONDS, the pass skips the
   update (updates.busy) and tries again next time.
   A marker already there when a pass starts is an update that never finished: the binaries may have
   moved with no check run. The pass then updates nothing, writes the stop file, tells Ryan, and leaves
   the marker for castle ollivander clear.
2. Reads the Codex catalog (codex debug models) and the Claude Code aliases (CLAUDE_LINES, plus any
   alias claude --help names, best effort; a parse miss falls back to the last aliases seen).
3. Files each visible name into a line. Ryan's castle model line filing comes first, then
   CLAUDE_LINES or the catalog description's keywords, where an excluded word wins. A name that fits
   no line, or more than one, gets one headmaster fyi asking Ryan to file it, and is never picked.
   A name the organisation blocks (BLOCKED_MODEL_PREFIXES) is skipped silently: never picked, never
   a nag to file it, and listed under blocked in the plan. So is a Claude alias that any run ever saw
   run as a blocked full id (plan catalog claude ran_as), however long ago and whatever it ran as since.
4. Picks per desk. A Claude desk takes the alias whose line is the role's need (Ryan's latest filing
   wins a tie). A Codex desk takes the visible model of that line with the lowest priority, skipping
   any that retires within 30 days. Effort is the card's, lowered to the nearest level the model lists.
   When every candidate of the need is blocked, the desk keeps its model and Ryan gets one fyi.
5. Applies to the headless desks. The first pick for a desk applies by itself and is marked initial
   (Ryan approved the role table on 4 October). After that, a move to the same or a cheaper class
   applies by itself with a fyi, and a dearer one waits as a pending pick until Ryan runs
   castle desk model <desk> --approve. A pending pick a later pass no longer makes, for any reason (the
   role moved, the model went hidden, retiring or blocked, the catalog could not be read), is dropped
   with a routine note, and --approve checks the pick again against the latest catalog kept in the store.
   A pinned desk is left alone. McGonagall and Snape take their model from agent files Ollivander never
   edits: for them he only reports the one-line change.
   While BLOCKED_MODEL_PREFIXES is set, run_desk will not launch a Codex desk with no model of its own,
   since the CLI default cannot be checked, so the first pass, which gives each unpinned Codex desk its
   pick, or a pin, comes before its first launch. A desk a revert pinned to no model is left alone, and
   each day's pass tells Ryan to pin one or hand it back with --role.
   Each pass also keeps every headless desk's fallbacks in pick order, its need's and then each cheaper
   need's, never above the line of its current model, per family (failover.write_ladders), for run_desk
   while a model is down (see fleet/failover.py).
   A pass records the catalogs it read, plans and writes in one store transaction, so a pin, unpin or
   approval Ryan makes meanwhile lands wholly before the plan (which then sees it) or wholly after the
   new catalog look (which --approve then checks against).

--dry-run prints the whole plan as JSON and changes nothing: no update runs and nothing is written.

Run it with the wrapper line:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.ollivander import main; sys.exit(main())' [--dry-run]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Callable, Iterator, Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db, pensieve, wands  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, failover, run_desk, safefs  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

ROLE_KEYS = ("family", "need", "effort", "why")
WHY_MAX_CHARS = 120
LOG_NAME = "ollivander-update.log"
_HELP_ALIAS = re.compile(r"'([a-z][a-z0-9-]{1,30})'")
_AGENT_MODEL = re.compile(r"model:\s*(\S+)")

Runner = Callable[[list, int, Optional[str]], tuple]


# Commands


def run_command(argv: list, timeout: int, log_name: Optional[str] = None) -> tuple:
    """(exit code, stdout). No shell, the fixed child environment, a timeout. -1 is a timeout and -2 a
    binary that would not start. With a log name, stdout and stderr go to that office log instead."""
    run_desk.guard(argv)
    try:
        if log_name is None:
            done = subprocess.run(argv, cwd=config.OFFICE_ROOT, env=run_desk.child_env(), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            return done.returncode, done.stdout or b""
        with safefs.opened_dir(config.OFFICE_ROOT, "logs", create=True) as logs_fd:
            log_fd = safefs.open_append(logs_fd, log_name, "Ollivander log")
            try:
                safefs.write_all(log_fd, ("$ " + " ".join(argv) + "\n").encode("utf-8"))
                done = subprocess.run(argv, cwd=config.OFFICE_ROOT, env=run_desk.child_env(),
                                      stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, timeout=timeout,
                                      check=False)
            finally:
                os.close(log_fd)
        return done.returncode, b""
    except subprocess.TimeoutExpired:
        return -1, b""
    except OSError:
        return -2, b""


# Role cards


def check_role(raw: bytes) -> dict:
    """A role card is strict JSON with exactly family, need, effort and why. Anything else is refused."""
    try:
        card = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("the role card is not strict JSON") from None
    if not isinstance(card, dict) or sorted(card) != sorted(ROLE_KEYS):
        raise FleetError("the role card must hold exactly family, need, effort and why")
    if card["family"] not in db.MODEL_FAMILIES:
        raise FleetError("the role card family must be claude or codex")
    if card["need"] not in db.MODEL_NEEDS:
        raise FleetError("the role card need must be frontier, workhorse or fast")
    if card["effort"] not in db.MODEL_EFFORTS:
        raise FleetError("the role card effort must be low, medium, high, xhigh or max")
    why = card["why"]
    if not isinstance(why, str) or not why.strip() or len(why) > WHY_MAX_CHARS or common.one_line(why, 200) != why:
        raise FleetError(f"the role card why must be one plain line of at most {WHY_MAX_CHARS} characters")
    return card


def load_role(desk: str) -> dict:
    with safefs.opened_dir(config.OFFICE_ROOT, "desks", desk) as fd:
        return check_role(safefs.read_regular(fd, config.ROLE_FILE, config.ROLE_MAX_BYTES, "role card"))


# Catalogs


def _levels(value: object) -> Optional[list]:
    """The efforts a model lists, in our order. None when it lists none, so the card's effort stands."""
    if not isinstance(value, list):
        return None
    named = [item.get("effort") if isinstance(item, dict) else item for item in value]
    found = [effort for effort in db.MODEL_EFFORTS if effort in named]
    return found or None


def parse_catalog(raw: bytes) -> list:
    """The models in codex debug models output. An entry without a safe slug is dropped."""
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("the Codex catalog is not JSON") from None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise FleetError("the Codex catalog has no models list")
    found = []
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        if wands.MODEL_NAME.fullmatch(item["slug"]) is None:
            continue
        priority = item.get("priority")
        upgrade = item.get("upgrade")
        found.append({
            "slug": item["slug"],
            "description": item["description"] if isinstance(item.get("description"), str) else "",
            "priority": priority if type(priority) is int else None,
            "visible": item.get("visibility") == "list",
            "upgrade": upgrade if isinstance(upgrade, dict) else None,
            "levels": _levels(item.get("supported_reasoning_levels")),
        })
    return found


def retires_at(model: dict) -> Optional[int]:
    """When the model retires: None without an upgrade, 0 for an upgrade that names no clear date."""
    upgrade = model.get("upgrade")
    if upgrade is None:
        return None
    when = upgrade.get("retirement_at")
    if not isinstance(when, str):
        return 0
    try:
        moment = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        return 0
    return max(0, int(moment.timestamp()))


def retiring(model: dict, now: int) -> bool:
    """True when the model has an upgrade and retires within RETIRING_SOON_SECONDS, or names no clear date."""
    when = retires_at(model)
    return when is not None and when - now <= config.RETIRING_SOON_SECONDS


def parse_aliases(text: str) -> Optional[list]:
    """Aliases named in the --model part of claude --help, or None when that part is not found."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip().startswith("--model ")), None)
    if start is None:
        return None
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip().startswith("-"):
            break
        block.append(line)
    named = _HELP_ALIAS.findall(" ".join(block))
    return sorted({name for name in named if not name.startswith("claude-") and wands.MODEL_NAME.fullmatch(name)})


def fetch_codex(runner: Runner) -> dict:
    code, out = runner([config.CODEX_BIN, "debug", "models"], config.CATALOG_TIMEOUT_SECONDS, None)
    if code != 0:
        return {"ok": False, "error": f"codex debug models exited {code}", "models": []}
    if len(out) > config.CATALOG_MAX_BYTES:
        return {"ok": False, "error": "the Codex catalog is too large", "models": []}
    try:
        models = parse_catalog(out)
    except FleetError as exc:
        return {"ok": False, "error": str(exc), "models": []}
    if not models:
        return {"ok": False, "error": "the Codex catalog lists no usable model", "models": []}
    return {"ok": True, "error": None, "models": models}


def fetch_claude(conn, runner: Runner) -> dict:
    """CLAUDE_LINES plus aliases the help names. A miss never fails the pass: the last aliases stand in."""
    code, out = runner([config.CLAUDE_BIN, "--help"], config.CHECK_TIMEOUT_SECONDS, None)
    detected = None
    if code == 0 and len(out) <= config.HELP_MAX_BYTES:
        detected = parse_aliases(out.decode("utf-8", "replace"))
    known = detected if detected is not None else wands.last_catalog(conn, "claude")
    return {"detected": detected is not None, "aliases": sorted(set(config.CLAUDE_LINES) | set(known)),
            "ran_as": wands.blocked_resolutions(conn, config.BLOCKED_MODEL_PREFIXES)}


# Filing names into lines


def blocked(name: str, ran_as: Optional[dict] = None) -> bool:
    """True when the organisation forbids this alias, full id or slug (BLOCKED_MODEL_PREFIXES), or when
    ran_as (fetch_claude's alias to blocked full id map) says this alias ever ran as a blocked id."""
    return (wands.blocked_by(name, config.BLOCKED_MODEL_PREFIXES) is not None
            or wands.base_alias(name) in (ran_as or {}))


def _word_lines(description: str) -> list:
    text = description.lower()
    return [line for line, words in config.CODEX_LINE_WORDS.items() if any(word in text for word in words)]


def file_codex(model: dict, filed: dict) -> dict:
    """{line, status, source}. status is tier, excluded, ignored or unclassified."""
    mine = filed.get(model["slug"])
    if mine is not None:
        if mine["line"] == "ignore":
            return {"line": None, "status": "ignored", "source": "ryan"}
        return {"line": mine["line"], "status": "tier", "source": "ryan"}
    if any(word in model["description"].lower() for word in config.CODEX_EXCLUDED_WORDS):
        return {"line": None, "status": "excluded", "source": "keywords"}
    lines = _word_lines(model["description"])
    if len(lines) != 1:
        return {"line": None, "status": "unclassified", "source": "keywords"}
    return {"line": lines[0], "status": "tier", "source": "keywords"}


def file_claude(alias: str, filed: dict) -> dict:
    mine = filed.get(alias)
    if mine is not None:
        if mine["line"] == "ignore":
            return {"line": None, "status": "ignored", "source": "ryan"}
        return {"line": mine["line"], "status": "tier", "source": "ryan"}
    if alias in config.CLAUDE_LINES:
        return {"line": config.CLAUDE_LINES[alias], "status": "tier", "source": "config"}
    return {"line": None, "status": "unclassified", "source": "config"}


def cost_line(name: Optional[str], codex: dict, filed: dict) -> Optional[str]:
    """The class a model costs, for comparing a current model with a pick. Excluded words do not hide it."""
    if name is None:
        return None
    mine = filed.get(name)
    if mine is not None and mine["line"] in db.MODEL_NEEDS:
        return mine["line"]
    if name in config.CLAUDE_LINES:
        return config.CLAUDE_LINES[name]
    model = next((item for item in codex["models"] if item["slug"] == name), None)
    lines = [] if model is None else _word_lines(model["description"])
    return lines[0] if len(lines) == 1 else None


def fit_effort(effort: str, levels: Optional[list]) -> str:
    """The card's effort, or the nearest level the model lists: the highest below it, else the lowest."""
    if levels is None or effort in levels:
        return effort
    order = db.MODEL_EFFORTS
    lower = [level for level in levels if order.index(level) < order.index(effort)]
    return lower[-1] if lower else levels[0]


# Picking


def claude_fits(need: str, claude: dict, filed: dict) -> list:
    """Every alias filed under the need, blocked or not."""
    return [alias for alias in claude["aliases"] if file_claude(alias, filed)["line"] == need]


def codex_fits(need: str, codex: dict, filed: dict, now: int) -> list:
    """Every visible, ranked Codex model of the need that is not retiring, blocked or not."""
    return [model for model in codex["models"]
            if model["visible"] and model["priority"] is not None and not retiring(model, now)
            and file_codex(model, filed)["line"] == need]


def claude_order(need: str, claude: dict, filed: dict) -> list:
    """The unblocked aliases of the need in pick order: Ryan's most recent filing first, then an alias known only
    from CLAUDE_LINES."""
    fits = [alias for alias in claude_fits(need, claude, filed) if not blocked(alias, claude.get("ran_as"))]
    return sorted(fits, key=lambda name: (filed[name]["id"] if name in filed else 0, name), reverse=True)


def codex_order(need: str, codex: dict, filed: dict, now: int) -> list:
    """The unblocked Codex models of the need in pick order, lowest priority first."""
    fits = [model for model in codex_fits(need, codex, filed, now) if not blocked(model["slug"])]
    return sorted(fits, key=lambda item: (item["priority"], item["slug"]))


def pick_claude(need: str, claude: dict, filed: dict) -> Optional[dict]:
    fits = claude_order(need, claude, filed)
    if not fits:
        return None
    alias = fits[0]
    how = "as Ryan filed it" if alias in filed else "in Claude Code"
    return {"model": alias, "line": need, "levels": None,
            "reason": f"role need {need}; {alias} is the alias for the newest {need} model {how}"}


def pick_codex(need: str, codex: dict, filed: dict, now: int) -> Optional[dict]:
    fits = codex_order(need, codex, filed, now)
    if not fits:
        return None
    model = fits[0]
    return {"model": model["slug"], "line": need, "levels": model["levels"],
            "reason": f"role need {need}; {model['slug']} is the newest {need} model in the Codex catalog"}


def ladders(conn, cards: dict, codex: dict, claude: dict, filed: dict, now: int) -> dict:
    """Each headless desk's fallbacks per family for fleet/failover.py: its need's models in pick order, then each
    cheaper need's, each at the card's effort fitted to the model. Never above the line of the model the desk is on
    now, so a dearer pick that waits for Ryan's approval never runs as a fallback. A family whose catalog could not
    be read is left out, so the last ladder stands."""
    found = {}
    for desk in config.HEADLESS_DESKS:
        card = cards.get(desk)
        if not isinstance(card, dict):
            continue
        row = wands.get_desk_model(conn, desk)
        top = config.COST_ORDER.index(card["need"])
        if row is not None and row["model"] is not None:
            held = row["line"] or cost_line(row["model"], codex, filed) or config.COST_ORDER[0]
            top = min(top, config.COST_ORDER.index(held))
        needs = config.COST_ORDER[:top + 1][::-1]
        found[desk] = {"claude": [{"model": alias, "effort": card["effort"], "line": need}
                                  for need in needs for alias in claude_order(need, claude, filed)]}
        if codex["ok"]:
            found[desk]["codex"] = [{"model": model["slug"], "effort": fit_effort(card["effort"], model["levels"]),
                                     "line": need} for need in needs for model in codex_order(need, codex, filed, now)]
    return found


# Agent files


def agent_file(desk: str) -> tuple:
    """(root, folders, file name, full path) of the agent file that sets this desk's model."""
    where, inside = config.AGENT_FILE_DESKS[desk]
    root = config.CASTLE_ROOT if where == "castle" else config.USER_HOME_DIR
    parts = inside.split("/")
    return root, tuple(parts[:-1]), parts[-1], f"{root}/{inside}"


def agent_file_model(desk: str) -> Optional[str]:
    """The model line in the agent file's frontmatter, or None when it cannot be read safely."""
    root, folders, name, _ = agent_file(desk)
    try:
        with safefs.opened_dir(root, *folders) as fd:
            text = safefs.read_regular(fd, name, config.BRIEF_MAX_BYTES, "agent file").decode("utf-8")
    except (FleetError, OSError, UnicodeDecodeError):
        return None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        match = _AGENT_MODEL.fullmatch(line.strip())
        if match is not None:
            try:
                return wands.check_name(match.group(1))
            except StoreError:
                return None
    return None


# The plan


def _notice(plan: dict, desk: str, kind: str, summary: str, key: str, verdict: str = "headmaster") -> None:
    notice = {"desk": desk, "kind": kind, "summary": common.one_line(summary, 480), "dedupe_key": key}
    if verdict != "headmaster":
        notice["verdict"] = verdict
    plan["notices"].append(notice)


def _no_default_launch(family: str, current: Optional[str], desk: str) -> str:
    """The extra words for a Codex desk with no model of its own while models are blocked: run_desk will
    not launch the CLI default, which cannot be checked, until Ryan pins one."""
    if family != "codex" or current is not None or not config.BLOCKED_MODEL_PREFIXES:
        return ""
    return (" It has no model of its own, and while models are blocked run_desk will not launch the unchecked"
            f" Codex CLI default, so pin one: castle desk model {desk} <model>")


def _display(conn, desk: str) -> str:
    try:
        return pensieve.get_desk(conn, desk)["role"] or desk
    except StoreError:
        return desk


def _catalog_view(codex: dict, claude: dict, filed: dict, now: int) -> dict:
    return {
        "codex": {"ok": codex["ok"], "error": codex["error"], "models": [
            {"slug": model["slug"], "priority": model["priority"], "visible": model["visible"],
             "retiring": retiring(model, now), "blocked": blocked(model["slug"]), **file_codex(model, filed)}
            for model in codex["models"]]},
        "claude": {"detected": claude["detected"],
                   "aliases": [{"alias": alias, "blocked": blocked(alias, claude.get("ran_as")),
                                **file_claude(alias, filed)} for alias in claude["aliases"]],
                   "ran_as": claude.get("ran_as") or {}},
    }


def make_plan(conn, cards: dict, codex: dict, claude: dict, now: int) -> dict:
    """What this pass does, from the role cards, both catalogs and the store. Reads only."""
    filed = wands.ryan_lines(conn)
    # The names this pass skipped because the organisation blocks them. They are never filed or picked.
    skipped = sorted({model["slug"] for model in codex["models"] if model["visible"] and blocked(model["slug"])}
                     | {alias for alias in claude["aliases"] if blocked(alias, claude.get("ran_as"))})
    plan = {"now": now, "catalog": _catalog_view(codex, claude, filed, now), "unclassified": [],
            "blocked": skipped, "desks": [], "notices": []}
    day = now // config.DAY_SECONDS
    unclassified = [("codex", model["slug"]) for model in codex["models"]
                    if model["visible"] and file_codex(model, filed)["status"] == "unclassified"
                    and not blocked(model["slug"])]
    unclassified += [("claude", alias) for alias in claude["aliases"]
                     if file_claude(alias, filed)["status"] == "unclassified"
                     and not blocked(alias, claude.get("ran_as"))]
    for family, name in unclassified:
        plan["unclassified"].append(name)
        what = "Codex model" if family == "codex" else "Claude Code alias"
        _notice(plan, config.OLLIVANDER_DESK, "ollivander.unclassified",
                f"The {what} {name} fits no line, so no desk gets it. File it with: "
                f"castle model line {name} <frontier|workhorse|fast|ignore>",
                f"ollivander:unclassified:{name}")
    if not codex["ok"]:
        _notice(plan, config.OLLIVANDER_DESK, "ollivander.catalog",
                f"Ollivander could not read the Codex catalog ({codex['error']}), so the Codex desks keep their models.",
                f"ollivander:catalog:{day}")
    for desk in config.ROLE_DESKS:
        plan["desks"].append(_plan_desk(conn, desk, cards[desk], codex, claude, filed, now, plan))
    return plan


def _plan_desk(conn, desk: str, card, codex: dict, claude: dict, filed: dict, now: int, plan: dict) -> dict:
    display = _display(conn, desk)
    day = now // config.DAY_SECONDS
    entry = {"desk": desk, "display": display}
    try:
        if isinstance(card, Exception):
            raise card
        family = pensieve.get_desk(conn, desk)["family"]
        if card["family"] != family:
            raise FleetError(f"its family {card['family']} is not the registry family {family}, "
                             "and a desk's family never changes")
    except (FleetError, StoreError) as exc:
        reason = common.one_line(exc, 200)
        _notice(plan, desk, "ollivander.bad-role",
                f"{display}: its role card is not valid ({reason}), so Ollivander left its model alone.",
                f"ollivander:bad-role:{desk}:{day}")
        return {**entry, "action": "bad-role", "error": reason}
    entry.update(family=family, need=card["need"], effort=card["effort"], why=card["why"])
    row = wands.get_desk_model(conn, desk)
    entry.update(current=None if row is None else row["model"], current_effort=None if row is None else row["effort"],
                 pinned=bool(row and row["pinned"]),
                 pending=None if row is None or row["pending_model"] is None
                 else {"model": row["pending_model"], "effort": row["pending_effort"]})
    if family == "claude":
        pick = pick_claude(card["need"], claude, filed)
        fits = claude_fits(card["need"], claude, filed)
    elif codex["ok"]:
        pick = pick_codex(card["need"], codex, filed, now)
        fits = [model["slug"] for model in codex_fits(card["need"], codex, filed, now)]
    else:
        return {**entry, "action": "no-catalog"}
    ran_as = claude.get("ran_as")
    entry["skipped_blocked"] = sorted(name for name in fits if blocked(name, ran_as))
    if pick is None and fits:
        return _plan_blocked(desk, entry, card["need"], family, plan, ran_as)
    if pick is None:
        current = entry["current"] or "its current model"
        _notice(plan, desk, "ollivander.no-pick",
                f"{display}: no {card['need']} model to pick for a {family} desk, so it keeps {current}."
                + _no_default_launch(family, entry["current"], desk),
                f"ollivander:no-pick:{desk}:{day}")
        return {**entry, "action": "no-pick"}
    effort = fit_effort(card["effort"], pick["levels"])
    entry.update(pick=pick["model"], pick_effort=effort, pick_line=pick["line"], reason=pick["reason"])
    if desk in config.AGENT_FILE_DESKS:
        return _plan_report(conn, desk, entry, row, plan, ran_as)
    if row is not None and row["pinned"]:
        if _no_default_launch(family, entry["current"], desk):
            # A revert to the CLI default pins the desk to no model, which run_desk refuses while models are
            # blocked. Ollivander leaves a pinned desk alone, so Ryan must pin one or hand it back.
            _notice(plan, desk, "ollivander.pinned-default",
                    f"{display}: it is pinned to no model of its own, and while models are blocked run_desk will"
                    " not launch the unchecked Codex CLI default. Ollivander leaves a pinned desk alone, so pin"
                    f" one: castle desk model {desk} <model>, or castle desk model {desk} --role hands it back"
                    " to Ollivander.",
                    f"ollivander:pinned-default:{desk}:{day}")
        return {**entry, "action": "pinned"}
    if row is None or row["changed_at"] is None:
        _notice(plan, desk, "ollivander.applied",
                f"{display}: initial pick {pick['model']} at {effort} effort. {pick['reason']}.",
                f"ollivander:applied:{desk}:{pick['model']}:{effort}:{now}")
        return {**entry, "action": "initial"}
    if (row["model"], row["effort"]) == (pick["model"], effort):
        return {**entry, "action": "keep"}
    registry = pensieve.get_desk(conn, desk)["model"]
    held = row["line"] or cost_line(row["model"] or registry, codex, filed) or config.COST_ORDER[0]
    current = row["model"] or registry or "the CLI default"
    if config.COST_ORDER.index(pick["line"]) <= config.COST_ORDER.index(held):
        _notice(plan, desk, "ollivander.applied",
                f"{display}: now {pick['model']} at {effort} effort, was {current}. {pick['reason']}.",
                f"ollivander:applied:{desk}:{pick['model']}:{effort}:{now}")
        return {**entry, "action": "apply"}
    # Keyed on this pass: carry_out raises it only when the pending pick is new, so a pick that waits
    # a second time, after being cleared, reaches Ryan again.
    _notice(plan, desk, "ollivander.pending",
            f"{display}: its role now picks {pick['model']} at {effort} effort, a {pick['line']} model, which costs "
            f"more than {current} ({held}). It waits for you: castle desk model {desk} --approve",
            f"ollivander:pending:{desk}:{pick['model']}:{effort}:{now}")
    return {**entry, "action": "pending"}


def _plan_blocked(desk: str, entry: dict, need: str, family: str, plan: dict,
                  ran_as: Optional[dict] = None) -> dict:
    """Every candidate of the need is blocked: the desk keeps its model, and Ryan hears once per set."""
    names = entry["skipped_blocked"]
    current = entry["current"] or "its current model"
    summary = (f"{entry['display']}: every {need} model for a {family} desk is blocked here"
               f" ({', '.join(names)}), so it keeps {current}.")
    if entry["current"] is not None and blocked(entry["current"], ran_as):
        summary += (f" {entry['current']} is blocked too, so run_desk will not launch it until you pin an"
                    f" allowed model: castle desk model {desk} <model>")
    summary += _no_default_launch(family, entry["current"], desk)
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
    _notice(plan, desk, "ollivander.blocked", summary, f"ollivander:blocked:{desk}:{need}:{digest}")
    return {**entry, "action": "blocked"}


def _plan_report(conn, desk: str, entry: dict, row: Optional[dict], plan: dict,
                 ran_as: Optional[dict] = None) -> dict:
    _, _, _, path = agent_file(desk)
    found = agent_file_model(desk)
    pinned = row is not None and bool(row["pinned"]) and bool(row["model"])
    target = row["model"] if pinned else entry["pick"]
    entry.update(agent_file=path, file_model=found, action="report")
    if found is None:
        _notice(plan, desk, "ollivander.agent-file",
                f"{entry['display']}: Ollivander could not read a model line in {path}. Its role picks {target}.",
                f"ollivander:agent-file:{desk}:unreadable:{plan['now'] // config.DAY_SECONDS}")
    elif found != target:
        # Raised again only when the mismatch is new: another file model or target than the last notice,
        # or a file that changed since the last pass (the store keeps the file's model unless pinned).
        base = f"ollivander:agent-file:{desk}:{found}:{target}"
        last = wands.last_event_key(conn, desk, "ollivander.agent-file")
        moved = not pinned and (row is None or row["model"] != found)
        if moved or last is None or not last.startswith(base + ":"):
            barred = f" {found} is blocked here." if blocked(found, ran_as) else ""
            _notice(plan, desk, "ollivander.agent-file",
                    f"{entry['display']}: its role picks {target}, and {path} says model: {found}.{barred} "
                    f"Ollivander never edits that file. To follow the role, change that one line to model: {target}",
                    f"{base}:{plan['now']}")
    return entry


# Why a pending pick was dropped, by the action of the pass that no longer made it.
DROPPED_BECAUSE = {
    "keep": "its role now picks the model it already runs",
    "pinned": "the desk is pinned",
    "no-pick": "no model of its need is left to pick now",
    "blocked": "every model of its need is blocked here",
    "no-catalog": "the Codex catalog could not be read, so nothing was picked this pass",
    "bad-role": "its role card is not valid",
    "report": "its model comes from its agent file",
}


def _catalog_entries(codex: dict, claude: dict, filed: dict) -> tuple:
    """What each family offered at this look, with whether it was listed, its line and when it retires,
    so castle desk model --approve can check a pending pick against it later."""
    codex_entries = [{"name": model["slug"], "visible": model["visible"], "line": file_codex(model, filed)["line"],
                      "retires_at": retires_at(model)} for model in codex["models"]]
    claude_entries = [{"name": alias, "visible": True, "line": file_claude(alias, filed)["line"], "retires_at": None}
                      for alias in claude["aliases"]]
    return codex_entries, claude_entries


def _waiting(conn, desk: str) -> Optional[dict]:
    row = wands.get_desk_model(conn, desk)
    if row is None or row["pending_model"] is None:
        return None
    return {"model": row["pending_model"], "effort": row["pending_effort"], "line": row["pending_line"]}


def _note_dropped(plan: dict, entry: dict, dropped: dict, why: str) -> None:
    """A routine note that a pending pick was dropped, so Ryan's --approve finds nothing and knows why."""
    desk = entry["desk"]
    effort = f" at {dropped['effort']} effort" if dropped["effort"] else ""
    _notice(plan, desk, "ollivander.pending-dropped",
            f"{entry.get('display') or desk}: the pick {dropped['model']}{effort} that waited for your approval is"
            f" dropped, because {why}.",
            f"ollivander:pending-dropped:{desk}:{dropped['model']}:{plan['now']}", verdict="routine")


def carry_out(conn, plan: dict, codex: dict, claude: dict, now: int) -> None:
    """Write the plan: the catalogs seen, each desk's need and model, then the notices for Ryan. A pending
    pick this pass did not make again, for any reason, is dropped with a routine note."""
    codex_entries, claude_entries = _catalog_entries(codex, claude, wands.ryan_lines(conn))
    if codex["ok"]:
        wands.record_catalog(conn, "codex", codex_entries, now=now)
    wands.record_catalog(conn, "claude", claude_entries, now=now)
    unchanged = set()
    for entry in plan["desks"]:
        action, desk = entry["action"], entry["desk"]
        if action == "report":
            row = wands.get_desk_model(conn, desk)
            if row is not None and row["pinned"]:
                wands.set_need(conn, desk, entry["need"], now=now)
            else:
                wands.record_agent_file(conn, desk, entry["need"], entry["file_model"], entry["pick_effort"], now=now)
        elif action != "bad-role":
            wands.set_need(conn, desk, entry["need"], now=now)
        if action in ("initial", "apply"):
            waiting = _waiting(conn, desk)
            reason = "initial" if action == "initial" else "role"
            wands.apply_model(conn, desk, entry["pick"], entry["pick_effort"], entry["pick_line"], reason, now=now,
                              blocked=config.BLOCKED_MODEL_PREFIXES)
            if waiting is not None:
                _note_dropped(plan, entry, waiting, f"Ollivander moved the desk to {entry['pick']} instead")
        elif action == "pending":
            waiting = _waiting(conn, desk)
            if not wands.set_pending(conn, desk, entry["pick"], entry["pick_effort"], entry["pick_line"], now=now,
                                     blocked=config.BLOCKED_MODEL_PREFIXES):
                unchanged.add(desk)
            elif waiting is not None:
                _note_dropped(plan, entry, waiting, f"its role now picks {entry['pick']}, which waits in its place")
        else:
            dropped = wands.clear_pending(conn, desk, now=now)["cleared"]
            if dropped is not None:
                _note_dropped(plan, entry, dropped, DROPPED_BECAUSE.get(action, "this pass did not pick it again"))
    for notice in plan["notices"]:
        if notice["kind"] == "ollivander.pending" and notice["desk"] in unchanged:
            notice["raised"] = False  # the same pick already waits, and Ryan was told when it began
            continue
        notice["raised"] = pensieve.add_event(conn, notice["desk"], notice["kind"],
                                              notice.get("verdict", "headmaster"), notice["summary"],
                                              dedupe_key=notice["dedupe_key"], now=now)["created"]


# CLI updates


def update_marker() -> bool:
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, "desks", config.OLLIVANDER_DESK) as fd:
            return safefs.is_safe_regular(fd, config.UPDATE_MARKER)
    except (FleetError, OSError):
        return False


def update_commands() -> list:
    return [[config.CLAUDE_BIN, "update"], [config.BREW_BIN, "upgrade", "--cask", "codex"]]


def check_commands() -> list:
    return [[config.CLAUDE_BIN, "--version"], [config.CODEX_BIN, "--version"]]


def write_stop(reason: str, now: int, name: Optional[str] = None) -> None:
    with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
        safefs.write_new(fd, name or config.STOP_FILE, f"{now} {common.one_line(reason, 300)}\n".encode("ascii"))


def remove_updating() -> None:
    with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
        try:
            os.unlink(config.UPDATING_FILE, dir_fd=fd)
        except FileNotFoundError:
            pass


def cli_versions(runner: Runner) -> dict:
    """{claude, codex}: what each --version printed, as one short ASCII line, or None when it failed."""
    found = {}
    for family, argv in zip(("claude", "codex"), check_commands()):
        code, out = runner(argv, config.CHECK_TIMEOUT_SECONDS, None)
        text = common.one_line(out.decode("ascii", "replace"), config.VERSION_MAX_CHARS).strip() if code == 0 else ""
        found[family] = text or None
    return found


@contextlib.contextmanager
def update_lock() -> Iterator[int]:
    """The update lock, held exclusively. Waits at most UPDATE_LOCK_WAIT_SECONDS for launches to let go,
    then raises safefs.Busy."""
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
            safefs.held_lock(locks_fd, config.UPDATE_LOCK, blocking=True,
                             timeout=config.UPDATE_LOCK_WAIT_SECONDS) as lock_fd:
        yield lock_fd


def leftover_update() -> Optional[str]:
    """The stamp in an update marker a pass left behind, "unknown" when it cannot be read, or None when
    there is no marker. Only a pass holding ollivander.lock writes one, so at the start of a pass any marker
    is an update that died part way."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
            if safefs.lstat(fd, config.UPDATING_FILE) is None:
                return None
            raw = safefs.read_regular(fd, config.UPDATING_FILE, 1024, "update marker")
    except (FleetError, OSError):
        return "unknown"
    stamp = raw.split(b" ", 1)[0]
    return stamp.decode("ascii") if stamp.isdigit() and len(stamp) <= 12 else "unknown"


def _stop_after_unfinished(conn, stamp: str, now: int, result: dict) -> None:
    """An update that never finished: stop launches and tell Ryan. The marker stays for castle ollivander clear."""
    reason = ("a CLI update did not finish, so the CLIs may have moved with no check run. Check both versions"
              " and run the real boundary probe (scripts/codex-boundary-test.sh) on the Codex one")
    write_stop(reason, now)
    result["stopped"] = True
    key = stamp if stamp != "unknown" else f"unknown-{now // config.DAY_SECONDS}"
    pensieve.add_event(conn, config.OLLIVANDER_DESK, "ollivander.stopped", "headmaster",
                       common.one_line(f"Ollivander stopped every headless desk: {reason}. The log is "
                                       f"{config.logs_dir()}/{LOG_NAME}. Once done: castle ollivander clear", 480),
                       dedupe_key=f"ollivander:unfinished:{key}", now=now)


def update_clis(conn, runner: Runner, now: int, dry_run: bool) -> dict:
    """Update both CLIs and check them, only while the update-clis file is there. Headless launches wait
    from before the first update until every check passes. Any failure, a new Codex version, or an update
    an earlier pass left unfinished, stops them. The whole update runs under the update lock, so no launch
    is between its stop check and its process, and no desk process runs while a binary is replaced."""
    enabled = update_marker()
    stamp = leftover_update()
    result = {"enabled": enabled, "commands": [" ".join(argv) for argv in update_commands()], "ran": [],
              "checks": [], "versions": None, "stopped": False, "busy": False, "unfinished": stamp is not None}
    if dry_run:
        return result
    if stamp is not None:
        _stop_after_unfinished(conn, stamp, now, result)
        return result
    if not enabled:
        return result
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(update_lock())
        except safefs.Busy:
            result["busy"] = True  # launches kept it; the next pass tries again
            return result
        _update_locked(conn, runner, now, result)
    return result


def _update_locked(conn, runner: Runner, now: int, result: dict) -> None:
    before = cli_versions(runner)
    # Left in place if this pass dies part way: run_desk keeps refusing, and the next pass stops launches.
    write_stop("a CLI update is running", now, config.UPDATING_FILE)
    for argv in update_commands():
        code, _ = runner(argv, config.UPDATE_TIMEOUT_SECONDS, LOG_NAME)
        result["ran"].append({"command": " ".join(argv), "exit_code": code})
    after = cli_versions(runner)
    result["versions"] = {"before": before, "after": after}
    for family, argv in zip(("claude", "codex"), check_commands()):
        result["checks"].append({"check": " ".join(argv), "ok": after[family] is not None})
    for desk in config.HEADLESS_DESKS:
        if not run_desk.is_enabled(desk):
            continue
        try:
            run_desk.build_plan(conn, desk)
            ok = True
        except (FleetError, StoreError, OSError):
            ok = False
        result["checks"].append({"check": f"run_desk {desk} --dry-run", "ok": ok})
    failed = [item["command"] for item in result["ran"] if item["exit_code"] != 0]
    failed += [item["check"] for item in result["checks"] if not item["ok"]]
    moved = [family for family in ("claude", "codex") if after[family] is not None and before[family] != after[family]]
    reasons = ["after the CLI update these failed: " + "; ".join(failed)] if failed else []
    notes = []
    for family in moved:
        what = f"{family} --version went from {before[family] or 'unknown'} to {after[family]}"
        if family in config.VERSION_STOPS:
            reasons.append(f"{what}, and the Codex sandbox boundary is proven per version, so run the real "
                           "boundary probe (scripts/codex-boundary-test.sh) on it first")
        else:
            notes.append((family, what))
    # The stop file is written before any event, so a store error below never loses it.
    if reasons:
        reason = ". ".join(reasons)
        write_stop(reason, now)
        result["stopped"] = True
        pensieve.add_event(conn, config.OLLIVANDER_DESK, "ollivander.stopped", "headmaster",
                           common.one_line(f"Ollivander stopped every headless desk: {reason}. The log is "
                                           f"{config.logs_dir()}/{LOG_NAME}. Once done: castle ollivander clear", 480),
                           dedupe_key=f"ollivander:stopped:{now}", now=now)
    for family, what in notes:
        pensieve.add_event(conn, config.OLLIVANDER_DESK, "ollivander.cli-version", "headmaster",
                           common.one_line(f"Ollivander updated the CLIs: {what}.", 480),
                           dedupe_key=f"ollivander:cli-version:{family}:{now}", now=now)
    remove_updating()


# Entry points


def run(conn, dry_run: bool = False, now: Optional[int] = None, runner: Optional[Runner] = None) -> dict:
    ts = common.now_stamp(now)
    runner = run_command if runner is None else runner
    if dry_run:
        return _pass(conn, True, ts, runner)
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
            safefs.held_lock(locks_fd, "ollivander.lock", blocking=False):
        return _pass(conn, False, ts, runner)


def _pass(conn, dry_run: bool, now: int, runner: Runner) -> dict:
    updates = update_clis(conn, runner, now, dry_run)
    codex = fetch_codex(runner)
    claude = fetch_claude(conn, runner)
    cards = {}
    for desk in config.ROLE_DESKS:
        try:
            cards[desk] = load_role(desk)
        except FleetError as exc:
            cards[desk] = exc
    if dry_run:
        plan = make_plan(conn, cards, codex, claude, now)
    else:
        # One write transaction from the plan's reads to its last write: a pin, unpin or --approve that
        # lands meanwhile either commits first, and the plan sees it, or waits until the new catalog look
        # is stored, and --approve checks the pick against that look.
        with db.transaction(conn):
            claude["ran_as"] = wands.blocked_resolutions(conn, config.BLOCKED_MODEL_PREFIXES)
            plan = make_plan(conn, cards, codex, claude, now)
            carry_out(conn, plan, codex, claude, now)
            found = ladders(conn, cards, codex, claude, wands.ryan_lines(conn), now)
        # After the transaction, written whole. A miss keeps the last ladders, which run_desk reads only when a
        # model is down.
        with contextlib.suppress(FleetError, OSError):
            failover.write_ladders(found, now)
    plan.update(dry_run=dry_run, updates=updates)
    return plan


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="ollivander", description="Keep each desk on the model its role needs.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        conn = common.connect()
    except StoreError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 300)}, ensure_ascii=True) + "\n")
        return 1
    try:
        data = run(conn, dry_run=args.dry_run)
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=True, indent=2) + "\n")
        return 0
    except (FleetError, StoreError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 600)}, ensure_ascii=True) + "\n")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
