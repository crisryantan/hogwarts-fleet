"""close <task-id> and drop <task-id>: the owner's plain close phrases, each a gate of its own, as Mischief managed is.

Each is the whole message, trimmed, with the task id matched exactly, and passes the same typed check as Mischief
managed <task-id> (the prompt hook, or the go confirmer when the prompt's transcript entry is not written yet). So only
a prompt Ryan typed in his own session closes anything; text a desk writes, or a phrase inside a longer message, never
does. The outcome is the hook's reply and her context, or the confirmer's headmaster event.
- close <task-id> closes a task exactly as Mischief managed <task-id> does (user_prompt_submit.close_confirmed), with
  its lines saying close: a task awaiting close (reviewed, dropped, or merged and proven) closes as complete with
  every open task under it, and one in flight is refused by name with its Fix line. For a go task, every open build
  under it must await close and no review lock of one may be held, or the whole close is refused before anything
  closes; then, with every build's lock held, each closes through that same close (close_locked), its review tasks
  with it, and the go task moves on, on a build's proof (closer.advance_parents), or else closes as superseded under
  its own review lock, since a go task holds no work of its own.
- drop <task-id> closes a task that is not going to land as superseded, with every open task under it, as castle task
  close <task-id> --reason superseded does; a build's go task closes with it once nothing else is open under that go
  task. Refused, with what to do next, while a review, a run or auto-close holds any of those tasks or the author
  whose review round one of them is (their review locks, taken without waiting and held through the drop, with each
  of their desks' launch locks, so no run launches on one meanwhile), a run launched on one may still be going, or
  the review loop holds one, so Ryan waits; and refused for a task with merged work at or under it, as far as the
  office knows (a proven close, or auto-close's record of the merge commit), since a merged task is closed, not
  dropped.
"""
from __future__ import annotations

import contextlib
from typing import Optional

from hogwarts import capacity, db, pensieve
from hogwarts.errors import ConflictError, NotFoundError, StoreError

from fleet import closer, common, config, owl_post, run_desk, safefs
from fleet.hooks import user_prompt_submit as hook
from fleet.safefs import FleetError

CLOSE = "close"
DROP = "Drop"
PHRASES = {"plain": CLOSE, "drop": "drop"}


def is_go(conn, task: dict) -> bool:
    return task["desk"] == hook.TASK_DESK and pensieve.task_spec(conn, task["id"]) is not None


def _builds(conn, go_id: str) -> list:
    """The open tasks right under a go task: its builds."""
    return [pensieve.get_task(conn, row["id"]) for row in pensieve.open_descendants(conn, go_id)
            if row["parent_task_id"] == go_id]


def _subtree(conn, task_id: str) -> list:
    """Every task under task_id, open or closed, nearest first."""
    children: dict = {}
    for task in pensieve.list_tasks(conn):
        children.setdefault(task["parent_task_id"], []).append(task)
    found, queue = [], list(children.get(task_id, []))
    while queue:
        task = queue.pop(0)
        found.append(task)
        queue += children.get(task["id"], [])
    return found


def _go_refused(conn, go_id: str) -> Optional[list]:
    """The refusal of close <go task>, when a build under it does not await close yet, else None."""
    waiting = [row for row in _builds(conn, go_id) if row["status"] != "awaiting_close"]
    if not waiting:
        return None
    named = ", ".join(f"{row['id']} ({row['status'].replace('_', ' ')})" for row in waiting[:4])
    return [f"Close was not applied to {go_id}: its build {named} does not await close yet, so nothing was closed.",
            f"Fix: type close {go_id} again once its review passes, or type drop {go_id} if it is not going to land."]


def _drop_fix(task_id: str) -> str:
    return f"Fix: wait for it to finish, then type drop {task_id} again; castle task show {task_id} says where it is."


def _merged(conn, task_id: str) -> Optional[tuple]:
    """(reason, Fix line) when the task or a task under it merged as far as the office knows, else None."""
    for row in [pensieve.get_task(conn, task_id), *_subtree(conn, task_id)]:
        which = "it" if row["id"] == task_id else f"{row['id']} under it"
        if pensieve.task_closure(conn, row["id"]) is not None:
            return (f"{which} merged, proven by auto-close, and a merged task is closed, not dropped",
                    f"Fix: type close {task_id} instead.")
        state = closer.close_state(conn, row["id"])
        if state["unreadable"]:
            return (f"the auto-close record of {row['id']} cannot be read whole, so whether it merged is unknown",
                    f"Fix: look at it with castle task show {row['id']}; to drop it anyway, castle task close"
                    f" {task_id} --reason superseded in your terminal.")
        if state["merged"] and not state["dropped"]:
            return (f"{which} merged, and a merged task is closed, not dropped",
                    f"Fix: type close {task_id} instead.")
    return None


def _drop_checked(conn, task_id: str) -> Optional[list]:
    """The refusal of drop <task-id> for a task that is missing, closed or merged, else None."""
    try:
        task = pensieve.get_task(conn, task_id)
    except NotFoundError as exc:
        return [f"{DROP} failed for {task_id}: {common.one_line(exc, 200)}",
                "Fix: find the id with castle task list --open, then type drop <task-id> with it."]
    if task["status"] == "closed":
        return [f"{DROP} was not applied to {task_id}: it is closed already, as {task['close_reason']}.",
                f"Fix: nothing to type; castle task show {task_id} says how it closed."]
    merged = _merged(conn, task_id)
    if merged is not None:
        return [f"{DROP} was not applied to {task_id}: {merged[0]}.", merged[1]]
    return None


def checked(conn, kind: str, task_id: str) -> Optional[list]:
    """The refusal lines before the typed check, the last one its Fix line, or None: close <task-id> and drop
    <task-id> refuse here what the close itself would refuse, so a refused one never waits on a confirmer."""
    try:
        if kind == "drop":
            return _drop_checked(conn, task_id)
        try:
            task = pensieve.get_task(conn, task_id)
        except NotFoundError:
            task = None
        if task is not None and task["status"] != "closed" and is_go(conn, task):
            return _go_refused(conn, task_id)
        return hook.close_checked(conn, task_id, CLOSE)
    except (StoreError, FleetError) as exc:
        word = "Close" if kind == "plain" else DROP
        return [f"{word} failed for {task_id}: {common.one_line(exc, 200)}",
                f"Fix: type {PHRASES[kind]} {task_id} again; castle task show {task_id} says where it stands."]


def _next(conn, task_ids: list) -> list:
    """The worktree lines for the tasks just closed, or the line saying there is nothing more to type."""
    lines = [f"Next: fleet worktree-remove {task_id} removes its worktree once nothing in it is needed."
             for task_id in task_ids if pensieve.get_task(conn, task_id)["worktree"]]
    return lines or ["Next: nothing more to type for it."]


def _close_go(conn, go_id: str, now: int) -> tuple:
    """(lines, closed) for close <go task>: with every open build's review lock held, taken without waiting, and every
    build read again under them, each closes through the hook's close (close_locked); then the go task moves on on a
    build's proof, or closes as superseded under its own review lock. A held lock or a build in flight closes nothing."""
    refused = _go_refused(conn, go_id)
    if refused is not None:
        return refused, False
    builds, done = _builds(conn, go_id), []
    with contextlib.ExitStack() as locks:
        for build in builds:
            try:
                locks.enter_context(run_desk.task_lock(build["id"]))
            except safefs.Busy:
                return [f"Close was not applied to {go_id}: a review, a run or auto-close is working on its build"
                        f" {build['id']} right now, so nothing was closed.",
                        f"Fix: type close {go_id} again once that ends; castle task show {build['id']} says where it"
                        " is."], False
        refused = _go_refused(conn, go_id)  # read again under the locks
        if refused is not None:
            return refused, False
        for build in builds:
            said = hook.close_locked(conn, build["id"], now, words=CLOSE)
            if not said[0].startswith("Close:"):
                closed = [f"- closed before it: {line}" for line in done]
                return [f"Close of go task {go_id} stopped at its build {build['id']}, so the go task stays open."
                        f" {said[0]}", *said[1:], *closed], False
            done.append(f"build {build['id']} ({build['desk']}) closed as complete")
            done += [line[2:] for line in said[1:] if line.startswith("- cascaded:")]
    how = "as complete, on a build's proof"
    if pensieve.get_task(conn, go_id)["status"] != "closed":
        closer.advance_parents(conn, now, only=go_id)
        if pensieve.get_task(conn, go_id)["status"] != "closed":
            try:
                with run_desk.task_lock(go_id), db.transaction(conn):
                    if pensieve.open_descendants(conn, go_id):
                        raise ConflictError("new work opened under it meanwhile")
                    pensieve.close_task(conn, go_id, "superseded", now=now)
            except safefs.Busy:
                return [f"Close of go task {go_id} closed its builds, but a review, a run or auto-close holds the go"
                        " task right now.", f"Fix: type close {go_id} again once that ends.",
                        *(f"- {line}" for line in done)], False
            except StoreError as exc:
                return [f"Close of go task {go_id} closed its builds, but not the go task: {common.one_line(exc, 200)}",
                        f"Fix: type close {go_id} again; castle task show {go_id} says where it stands.",
                        *(f"- {line}" for line in done)], False
            how = "as superseded, since a go task holds no work of its own and no proven build close moved it on"
    lines = [f"Close: go task {go_id} is closed {how}.", *(f"- {line}" for line in done)]
    return lines + _next(conn, [build["id"] for build in builds]), True


def _held(conn, tasks: list, guards: list, now: int) -> Optional[str]:
    """Why a drop waits: a launch on one of the tasks that may still be running, or the review loop on one of the
    guarded tasks (the tasks and the authors whose review rounds they are), else None."""
    since = now - config.RUNNING_WINDOW_SECONDS
    going = {row["task_id"] for row in capacity.list_launches(conn)
             if row["metric_id"] is None and row["launched_at"] > since}
    for task in tasks:
        if task["id"] in going:
            return f"{task['desk']} has a run going on {task['id']}"
    for task_id in guards:
        if owl_post.auto_review_running(task_id):
            return f"the review loop is working on {task_id}"
    return None


def _drop(conn, task_id: str, now: int) -> tuple:
    """(lines, dropped) for drop <task-id>: the task and every open task under it close as superseded, and a build's
    go task with it once nothing else is open under that go task, in one transaction under all their review locks."""
    refused = _drop_checked(conn, task_id)
    if refused is not None:
        return refused, False
    task = pensieve.get_task(conn, task_id)
    under = [pensieve.get_task(conn, row["id"]) for row in pensieve.open_descendants(conn, task_id)]
    parent = None
    if task["parent_task_id"] is not None:
        above = pensieve.get_task(conn, task["parent_task_id"])
        if above["status"] != "closed" and is_go(conn, above) and _merged(conn, above["id"]) is None \
                and {row["id"] for row in pensieve.open_descendants(conn, above["id"])} \
                <= {task_id, *(row["id"] for row in under)}:
            parent = above
    tasks = [task, *under, *([parent] if parent else [])]
    # A review holds its author task's review lock and loop lock, never its reviewer task's own, so a reviewer task
    # is guarded by its author's too.
    authors = [capacity.round_author(conn, row["id"]) for row in tasks]
    guards = list(dict.fromkeys([row["id"] for row in tasks] + [author for author in authors if author]))
    try:
        with contextlib.ExitStack() as locks:
            for guarded in guards:
                try:
                    locks.enter_context(run_desk.task_lock(guarded))
                except safefs.Busy:
                    return [f"{DROP} was not applied to {task_id}: a review, a run or auto-close is working on"
                            f" {guarded} right now, so nothing was dropped.", _drop_fix(task_id)], False
            # Every desk's launch lock too, so no run on one of these tasks launches between the look below and the
            # close: a launch checks its task is open and records its launch under that lock.
            for desk in sorted({row["desk"] for row in tasks}):
                if not locks.enter_context(run_desk.launch_lock_if_free(desk)):
                    return [f"{DROP} was not applied to {task_id}: a run of {desk} is launching right now, so"
                            " nothing was dropped.", _drop_fix(task_id)], False
            refused = _drop_checked(conn, task_id)  # read again under the locks: a merge auto-close just recorded
            if refused is not None:
                return refused, False
            held = _held(conn, tasks, guards, now)
            if held is not None:
                return [f"{DROP} was not applied to {task_id}: {held}, so nothing was dropped.",
                        _drop_fix(task_id)], False
            with db.transaction(conn):
                locked = {row["id"] for row in tasks}
                if not {row["id"] for row in pensieve.open_descendants(conn, task_id)} <= locked:
                    raise ConflictError("new work opened under it meanwhile")
                pensieve.close_task(conn, task_id, "superseded", now=now)
                if parent is not None:
                    if pensieve.open_descendants(conn, parent["id"]):
                        raise ConflictError("new work opened under its go task meanwhile")
                    pensieve.close_task(conn, parent["id"], "superseded", now=now)
    except (StoreError, FleetError) as exc:
        return [f"{DROP} failed for {task_id}: {common.one_line(exc, 200)}, so nothing was dropped.",
                f"Fix: type drop {task_id} again; castle task show {task_id} says where it stands."], False
    what = "go task" if is_go(conn, task) else "task"
    lines = [f"{DROP}: {what} {task_id} is closed as superseded."]
    for row in under:
        lines.append(f"- cascaded: {row['id']} ({row['desk']}) closed as"
                     f" {pensieve.get_task(conn, row['id'])['close_reason']}")
    if parent is not None:
        lines.append(f"- with it: its go task {parent['id']} closed as superseded, since nothing else was open under"
                     " it")
    return lines + _next(conn, [row["id"] for row in [task, *under]]), True


def applied(conn, kind: str, task_id: str, now: int) -> tuple:
    """(lines, closed) for close <task-id> (kind plain) or drop <task-id> once Ryan's typing is confirmed."""
    try:
        if kind == "drop":
            return _drop(conn, task_id, now)
        try:
            task = pensieve.get_task(conn, task_id)
        except NotFoundError:
            task = None
        if task is not None and task["status"] != "closed" and is_go(conn, task):
            return _close_go(conn, task_id, now)
        lines = hook.close_confirmed(conn, task_id, now, CLOSE)
        return lines, lines[0].startswith("Close:")
    except (StoreError, FleetError, OSError) as exc:
        reason = common.scrubbed_line(exc, 200) if not isinstance(exc, OSError) else type(exc).__name__
        word = "Close" if kind == "plain" else DROP
        return [f"{word} failed for {task_id}: {reason}",
                f"Fix: type {PHRASES[kind]} {task_id} again; castle task show {task_id} says where it stands."], False
