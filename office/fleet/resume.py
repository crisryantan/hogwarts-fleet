"""Resume: a build desk's turn that died is started once more by itself, on the same worktree and branch, and the
owner hears it in one line. A second death in a row waits for him.

- When: at the end of each Owl Post pass (sweep), before the orchestrator, so a turn it starts is the one any later
  step sees. The first sweep only records when it first ran (the office resume/since marker): a run launched by
  then, or more than RESUME_WINDOW_SECONDS ago, is never started again by itself.
- Which: each active task of a build desk (config.WORKTREE_DESKS) with a worktree whose newest launch may have died,
  as run_desk reads it from the run's end record and recorded usage: it died idle, or it reads as going, or it was
  killed by a signal. A task whose newest run posted its handoff after it launched is the review loop's, and so is one
  the loop holds or has work after a verdict on that it has not finished: those are left alone.
- How it knows the run is over: without waiting it takes the task's review lock, which every build run of the task
  holds until its process ends (run_desk.task_lock) and every review holds while it runs. A lock it cannot take is a
  run still going, or a review, and the task waits for a later pass. Under the lock no run of the task is left, so
  run_desk.dead_streak with over counts a newest run gone with no end record (a restart, a SIGKILL) or killed as the
  loops stopped as dead too, with the runs that died idle.
- What: with exactly one dead run at the head of the task's launches, it starts the desk again through
  worktree.start_desk under that lock, which it hands to the run: the path fleet build takes, on the task's request
  owl or its PR follow-up's own owl, in the same worktree and branch. The owner gets one build.resumed line. With
  config.ORCHESTRATOR_DEAD_RUNS_IN_A_ROW or more, the bound McGonagall's fix-round retry keeps too, nothing starts and
  he gets one build.dead line saying it waits for him, as a second death always has. A start that did not start (the
  desk is not enabled, or at its daily cap) is a build.dead line saying why.
- Once: each line's dedupe key names the dead run, and a run that has either line is never started again. The line
  is written before the start, so a kill between them never gives two starts. A restart that ended before its launch
  counted (its run gave up waiting for a slot, hit a cap or was refused) leaves the dead run the newest launch with its
  task's review lock free, since the restart's process holds that lock from its start: the owner then hears once that
  the task waits for him (build.dead), unless a stop holds the restart or it waits for a model, either of which
  starts it again by itself later.
  McGonagall's fix-round retry refuses a run this has already started again (orchestrator._why_not_route).
- Read and act only through those paths: nothing here writes a worktree, a branch or a review.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hogwarts import capacity, db, pensieve
from hogwarts.errors import StoreError

from fleet import common, config, markers, owl_post, review, run_desk, safefs, worktree
from fleet.safefs import FleetError

DIR = "resume"
SINCE = "since"
RESUMED_KIND, DEAD_KIND = "build.resumed", "build.dead"


def resumed_key(run_id: str) -> str:
    return f"build:resumed:{run_id}"


def dead_key(run_id: str) -> str:
    return f"build:dead:{run_id}"


def acted_on(conn, run_id: str) -> bool:
    """Whether a dead run already has its line: started again, or left for the owner."""
    return db.fetch_one(conn, "SELECT id FROM events WHERE dedupe_key IN (?, ?)",
                        (resumed_key(run_id), dead_key(run_id))) is not None


def _told(conn, key: str) -> bool:
    return db.fetch_one(conn, "SELECT id FROM events WHERE dedupe_key = ?", (key,)) is not None


def _since(now: int) -> int:
    with safefs.opened_dir(config.OFFICE_ROOT, DIR, create=True) as fd:
        marker = markers.read(fd, SINCE)
        if marker is None:
            markers.publish(fd, SINCE, {"state": "since", "at": now})
            marker = markers.read(fd, SINCE) or {}
    value = marker.get("at")
    return value if type(value) is int else now


def _newest(conn, task_id: str) -> Optional[dict]:
    """The task's newest launch with the tokens its usage recorded, as run_desk.run_outcome reads them."""
    launches = capacity.task_launches(conn, task_id)
    return launches[-1] if launches else None


def _how(launch: dict) -> str:
    """How a dead run ended, in a few fixed words."""
    try:
        end = run_desk.run_end(launch["desk"], launch["run_id"])
    except (FleetError, OSError):
        return "its end could not be read"
    if end is None:
        return "it left no end record, so its process was gone"
    code = end["exit_code"]
    if code is None:
        return "it never started"
    return "it timed out" if code == -1 else "it was killed" if code < 0 else f"it exited {code} having done nothing"


def _maybe_dead(launch: dict) -> bool:
    """A cheap look before taking the lock: whether the launch could be a dead run once it is over."""
    try:
        return run_desk.died(launch, over=True)
    except (FleetError, StoreError, OSError):
        return False


def _handed_off(conn, task: dict, launch: dict) -> bool:
    newest = review.latest_result_owl(conn, task)
    return newest is not None and newest["created_at"] >= launch["launched_at"]


def _loop_busy(task_id: str) -> bool:
    return owl_post.auto_review_running(task_id) or bool(owl_post.unfinished_afters(task_id))


def resume_task(conn, task: dict, now: Optional[int] = None, floor: int = 0) -> Optional[str]:
    """Start one task's dead build turn again, or tell the owner it waits for him. Returns what it did, or None when
    nothing was due. A run launched at or before floor is left alone."""
    launch = _newest(conn, task["id"])
    if launch is None or launch["launched_at"] <= floor or _told(conn, dead_key(launch["run_id"])):
        return None
    if _told(conn, resumed_key(launch["run_id"])):
        return _restart_ended(conn, task, launch, now)
    if not _maybe_dead(launch) or _handed_off(conn, task, launch) or _loop_busy(task["id"]):
        return None
    try:
        with run_desk.task_lock(task["id"]) as lock_fd:
            # Read again under the lock: no run of the task can be going now, and none can start meanwhile.
            task = pensieve.get_task(conn, task["id"])
            if task["status"] != "active" or not task["worktree"] or task["desk"] not in config.WORKTREE_DESKS:
                return None
            newest = _newest(conn, task["id"])
            if newest is None or newest["run_id"] != launch["run_id"] or acted_on(conn, launch["run_id"]):
                return None
            streak = run_desk.dead_streak(conn, task["id"], over=True)
            if streak == 0 or _handed_off(conn, task, launch):
                return None
            return _act(conn, task, launch, streak, lock_fd, now)
    except safefs.Busy:
        return None  # a run of the task, or a review, is still going


def _restart_ended(conn, task: dict, launch: dict, now: Optional[int]) -> Optional[str]:
    """The restart of a dead run that never launched: still the task's newest launch, with its task's review lock free.
    The restart's process inherits that lock from its start (start_desk hands it on) and holds it while it waits for a
    run slot, so a free lock means it ended before its launch counted (a slot wait given up, a cap, a refusal). The
    owner hears once that the task waits for him; it is not started a third time. A restart something will start
    again by itself is no such end, so nothing is said while one waits (_restart_waits)."""
    if _restart_waits(conn, task):
        return None
    try:
        with run_desk.task_lock(task["id"]):
            newest = _newest(conn, task["id"])
            if newest is None or newest["run_id"] != launch["run_id"] or _told(conn, dead_key(launch["run_id"])):
                return None
            _tell(conn, task, DEAD_KIND, dead_key(launch["run_id"]),
                  f"{task['desk']}'s build turn on task {task['id']} died and the fleet's restart of it ended before it"
                  " launched (a run slot wait, a cap or a refusal), so it waits for you: fleet build"
                  f" {task['id']}", now)
            return "restart never launched"
    except safefs.Busy:
        return None  # the restart is still waiting for a run slot, or a review runs


def _restart_waits(conn, task: dict) -> bool:
    """Whether the restart's run is kept to start again by itself: a stop of the desk's family holds it (or holds the
    desk now), or it waits for a model (failover.note_waiting), on the owl start_desk gave it. One that cannot be read
    counts as waiting, so the owner is never told it waits while it may still start."""
    from fleet import failover, followup, stops  # here: followup builds on worktree, as start_desk does

    if run_desk.stop_requested(config.desk_family(task["desk"])):
        return True
    try:
        owl_id, _ = followup.run_owl(conn, task)
        if owl_id in failover.read_state()["waiting"]:
            return True
        return any(owl == owl_id and (marker or {}).get("state") == "held" for owl, marker in stops.held(strict=True))
    except (FleetError, StoreError, OSError):
        return True


def _act(conn, task: dict, launch: dict, streak: int, lock_fd: int, now: Optional[int]) -> str:
    """Under the task's review lock: the owner's line, then the start, or the line that it waits for him."""
    how, run_id, desk = _how(launch), launch["run_id"], task["desk"]
    if streak >= config.ORCHESTRATOR_DEAD_RUNS_IN_A_ROW:
        _tell(conn, task, DEAD_KIND, dead_key(run_id),
              f"{desk}'s build turn on task {task['id']} died again ({how}) after the fleet started it once more, so it"
              f" waits for you: read its run log, then fleet build {task['id']}", now)
        return "waits for the owner"
    # What start_desk would refuse is told as that, so a turn that cannot start is never told as started.
    held = (f"{desk} is not enabled" if not run_desk.is_enabled(desk)
            else f"{desk} reached its daily cap" if run_desk.over_daily_cap(conn, desk) is not None else None)
    if held is None:
        _tell(conn, task, RESUMED_KIND, resumed_key(run_id),
              f"{desk}'s build turn on task {task['id']} died ({how}), so the fleet started it once more on the same"
              " worktree and branch; if it dies again it waits for you", now)
        try:
            started = worktree.start_desk(conn, task, lock_fd)
        except (FleetError, StoreError, OSError) as exc:
            started = common.scrubbed_line(exc, 160)
        if started.startswith(f"started {desk} "):
            return "started"
        held = started
    _tell(conn, task, DEAD_KIND, dead_key(run_id),
          f"{desk}'s build turn on task {task['id']} died ({how}), and the fleet could not start it again"
          f" ({common.one_line(held, 160)}), so it waits for you: fleet build {task['id']}", now)
    return "not started"


def _tell(conn, task: dict, kind: str, key: str, summary: str, now: Optional[int]) -> None:
    pensieve.add_event(conn, task["desk"], kind, "headmaster", common.scrubbed_line(summary, pensieve.SUMMARY_LIMIT),
                       task_id=task["id"], dedupe_key=key, now=now)


def sweep(conn, now: Optional[int] = None) -> list:
    """Resume each active build task's dead turn, once (resume_task). Returns one {task_id, outcome} per task acted
    on. A task whose state cannot be read is left for the next pass, with its error as its outcome."""
    stamp = common.now_stamp(now)
    floor = max(_since(stamp), stamp - config.RESUME_WINDOW_SECONDS)
    done = []
    for task in pensieve.list_tasks(conn, status="active"):
        if task["desk"] not in config.WORKTREE_DESKS or not task["worktree"]:
            continue
        try:
            outcome = resume_task(conn, task, now, floor)
        except (StoreError, FleetError, OSError, sqlite3.Error) as exc:
            outcome = "error: " + common.one_line(exc, 200)
        if outcome is not None:
            done.append({"task_id": task["id"], "outcome": outcome})
    return done
