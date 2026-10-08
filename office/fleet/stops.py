"""Ollivander's stop as every desk session sees it, and the desk runs it held back.

- active_line: one line naming the stop in place (the stop file, else the update marker), with why and the exact
  command that clears it, or None. The prompt and session start hooks put it at the top of the events banner in every
  desk session until castle ollivander clear removes it. A stop that cannot be read still shows, as unreadable.
- hold: run_desk keeps a launch the stop refused (desk, owl, task) as one marker per owl in the office state folder
  (config.STOP_HELD_DIR), written create-exclusive, so a run refused again and again is kept once. Never raises.
- resume: the Owl Post's pass, once no stop or update marker is in place, starts each held run again once through
  the normal launch (run_desk.spawn, with its caps and locks). It claims each first (resumed), then writes one
  headmaster event naming them, then launches them. A held owl its desk has acked since, or whose task closed, is
  dropped with no run.
- release: the started run itself (run_desk main, before its stop check) removes its marker, which is how a pass
  knows the launch took. A claim no run released is due again after RUNNING_WINDOW_SECONDS, so a launch or a pass
  that died part way never loses a held run, and the run is never started twice while it may still be starting.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from typing import Callable, Optional

from hogwarts import ids, owlery, pensieve
from hogwarts.errors import NotFoundError, StoreError

from fleet import common, config, markers, safefs
from fleet.safefs import FleetError

CLEAR = "castle ollivander clear"
EVENT_KIND = "owlpost.stop-restarted"


def _stop_text(fd: int, name: str) -> Optional[tuple]:
    """(stamp or None, reason) from one stop file, None when it is not there."""
    if safefs.lstat(fd, name) is None:
        return None
    try:
        raw = safefs.read_regular(fd, name, config.STOP_TEXT_MAX_BYTES, "the stop file")
        stamp, _, reason = raw.decode("ascii").strip().partition(" ")
    except (FleetError, OSError, UnicodeDecodeError):
        return None, "it could not be read"
    return (int(stamp) if stamp.isdigit() and len(stamp) <= 12 else None), (reason or "no reason given")


def active_line() -> Optional[str]:
    """The banner line for a stop in place, or None. Never raises."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR) as fd:
            found = _stop_text(fd, config.STOP_FILE)
            updating = None if found is not None else _stop_text(fd, config.UPDATING_FILE)
    except (FleetError, OSError):
        return None
    if found is None and updating is None:
        return None
    stamp, reason = found if found is not None else updating
    since = "" if stamp is None else time.strftime(" since %Y-%m-%d %H:%M UTC", time.gmtime(stamp))
    what = "Ollivander's stop is on" if found is not None else "Ollivander's CLI update marker is in place"
    return (f"{what}{since}: {common.one_line(reason, 300)}. Every headless desk run is blocked until `{CLEAR}` runs"
            " in your terminal; the Owl Post starts the runs it held once it is clear.")


# Runs the stop held back


def _held_dir(create: bool = False):
    return safefs.opened_dir(config.OFFICE_ROOT, config.STATE_DIR, config.STOP_HELD_DIR, create=create)


def hold(desk: str, owl_id: str, task_id: Optional[str], now: Optional[int] = None) -> bool:
    """Keep one launch the stop refused, for resume. A run the Owl Post had started again (resumed) that a new stop
    refused is held again, keeping its count. False when it was held already or could not be kept. Never raises."""
    try:
        data = {"state": "held", "desk": ids.check("desk", desk), "owl": ids.check("owl", owl_id),
                "task": ids.optional("task", task_id), "at": common.now_stamp(now), "resumes": 0}
        with _held_dir(create=True) as fd:
            if markers.publish(fd, data["owl"], data):
                return True
            found = markers.read(fd, data["owl"]) or {}
            if found.get("state") == "held":
                return False
            resumes = found.get("resumes")
            markers.replace(fd, data["owl"], {**data, "resumes": resumes if type(resumes) is int else 0})
            return True
    except (FleetError, StoreError, OSError):
        return False


def release(desk: str, owl_id: str) -> None:
    """A run of this owl has started (run_desk main, before its own stop check): the Owl Post no longer needs to start
    it, so its marker goes. If the stop refuses this run too, hold keeps it again. Never raises."""
    with contextlib.suppress(FleetError, StoreError, OSError):
        ids.check("desk", desk)
        name = ids.check("owl", owl_id)
        with _held_dir() as fd:
            os.unlink(name, dir_fd=fd)


def held() -> list:
    """The held runs as (owl, marker), oldest first. Never raises."""
    try:
        with _held_dir() as fd:
            names = sorted(name for name in os.listdir(fd) if not name.startswith("."))
            found = [(name, markers.read(fd, name)) for name in names]
    except (FleetError, OSError):
        return []
    found = [(name, marker) for name, marker in found if marker is not None]
    return sorted(found, key=lambda item: (item[1].get("at") if type(item[1].get("at")) is int else 0, item[0]))


def _drop(fd: int, name: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=fd)


def _wanted(conn, marker: dict, name: str) -> Optional[bool]:
    """Whether a held run is still worth starting: True for its owl unacked in its desk's inbox and its task, when it
    names one, not closed; False once that is no longer so; None for a marker that did not read whole, which is kept
    as it is, never dropped on a bad read."""
    desk, owl, task, resumes = marker.get("desk"), marker.get("owl"), marker.get("task"), marker.get("resumes")
    if marker.get("state") not in ("held", "resumed") or owl != name or desk not in config.HEADLESS_DESKS \
            or not (task is None or isinstance(task, str)) or type(resumes) is not int:
        return None
    if not any(item["id"] == owl for item in owlery.inbox(conn, desk)):
        return False
    if task is not None:
        try:
            return pensieve.get_task(conn, task)["status"] != "closed"
        except NotFoundError:
            return False
    return True


def _due(marker: dict, now: int) -> bool:
    """A held run is due once the stop is clear. A resumed one is due again only when its run never started (release
    never came) within RUNNING_WINDOW_SECONDS: the launch or the pass died before it."""
    if marker.get("state") == "held":
        return True
    started = marker.get("resumed_at")
    return type(started) is int and now - started >= config.RUNNING_WINDOW_SECONDS


def resume(conn, spawn: Callable[[str, str], None], stopped: Callable[[], bool], now: Optional[int] = None) -> list:
    """Start each held run again once, only while stopped() is False, and return them as dicts (desk, owl, task).
    Each is claimed first (resumed, with a count), then one headmaster event names them all, and only then are they
    launched, so a pass killed anywhere loses neither a run nor its event: a claim whose run never started is due
    again after RUNNING_WINDOW_SECONDS, at most config.STOP_RESUMES_MAX launches in all, and then it is dropped and
    said so. At most config.STOP_RESTARTS_PER_PASS runs a pass, so its one event names every one. A launch that fails
    to start is held again; one refused later tells the owner itself. One run that cannot be read or claimed never
    stops the others."""
    found = held()
    if not found or stopped():
        return []  # the common case reads one folder
    ts = common.now_stamp(now)
    claimed, given_up = [], []
    with _held_dir() as fd:
        for name, marker in found:
            if len(claimed) + len(given_up) >= config.STOP_RESTARTS_PER_PASS:
                break  # the rest wait for the next pass, so one event names every run it handles
            if not _due(marker, ts):
                continue
            try:
                wanted = _wanted(conn, marker, name)
            except (StoreError, sqlite3.Error):
                continue  # read again on the next pass
            if wanted is None:
                continue
            item = {"desk": marker["desk"], "owl": name, "task": marker.get("task")}
            if not wanted:
                _drop(fd, name)
                continue
            if marker["resumes"] >= config.STOP_RESUMES_MAX:
                given_up.append((name, item))  # dropped only once the event saying so is stored
                continue
            try:
                markers.replace(fd, name, {**marker, "state": "resumed", "resumed_at": ts,
                                           "resumes": marker["resumes"] + 1})
            except (FleetError, OSError):
                continue
            claimed.append((name, marker, item))
        if not claimed and not given_up:
            return []
        if not _tell(conn, [item for _, _, item in claimed], [item for _, item in given_up], ts):
            for name, marker, _ in claimed:  # no event, no launch: each is due again on the next pass
                with contextlib.suppress(FleetError, OSError):
                    markers.replace(fd, name, marker)
            return []
        for name, _ in given_up:
            _drop(fd, name)
        started = []
        for name, marker, item in claimed:
            try:
                spawn(item["desk"], name)
            except (FleetError, OSError):
                with contextlib.suppress(FleetError, OSError):
                    markers.replace(fd, name, {**marker, "state": "held"})
                continue
            started.append(item)
    return started


def _named(items: list) -> str:
    return ", ".join(f"{item['desk']} on owl {item['owl']}" + (f" (task {item['task']})" if item["task"] else "")
                     for item in items)


def _tell(conn, starting: list, given_up: list, ts: int) -> bool:
    """One headmaster event naming the runs this pass starts again, and any it gave up on. Whether it is stored."""
    parts = []
    if given_up:  # first, so no cut ever hides a run that is no longer started by itself
        parts.append(f"These never started after {config.STOP_RESUMES_MAX} tries and are no longer started by"
                     f" themselves: {_named(given_up)}; start each with fleet build <task-id>.")
    if starting:
        parts.append("Ollivander's stop is clear, so the Owl Post is starting again the runs it held:"
                     f" {_named(starting)}.")
    items = starting + given_up
    tasks = {item["task"] for item in items}
    try:
        pensieve.add_event(conn, items[0]["desk"], EVENT_KIND, "headmaster", common.one_line(" ".join(parts), 480),
                           task_id=next(iter(tasks)) if len(tasks) == 1 else None,
                           dedupe_key=f"owlpost:stop-restarted:{ts}:{items[0]['owl']}", now=ts)
    except (StoreError, FleetError, sqlite3.Error):
        return False
    return True
