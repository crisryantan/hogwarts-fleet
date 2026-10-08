"""Mischief managed everything: close every task in a closeable state, and refuse every other open task by name.

The owner's rule. The phrase is a gate of its own: the whole message, trimmed, is exactly "Mischief managed
everything", and it passes the same typed check as Mischief managed <task-id> (the hook, or the go confirmer when the
prompt's transcript entry is not written yet). Then every open task, oldest first and go tasks last, is either closed
or refused, never skipped:
- Closeable: a task awaiting close whose newest verdict is a PASS, whether it is reviewed and not merged yet, dropped
  (auto-close stopped it because its PR was closed unmerged), or merged with its after-merge checks proven. Each closes
  through the hook's own close (user_prompt_submit.close_locked) under its review lock, taken without waiting: a hook
  token minted and spent in the same run. So a bulk close is just that many single closes, with the same records.
- Refused, with the reason and a Fix line saying what to type next: a task a desk asked a question on that has no
  answer yet, and every task in flight: queued or active (with a desk, in review, awaiting a verdict, a live build, a
  fix round, a HEADMASTER verdict waiting for the owner), any task whose review lock is held or whose review loop or PR
  follow-up has unfinished work, one auto-close stopped short of proof or still awaits a judge on, one with open work
  under it, and one with no PASS on record.
- A go task closes once its last build has, on a build's proof (closer.advance_parents); otherwise it is refused,
  naming the builds it waits on or why no proof closes it.
Each task is judged again under its own review lock right before its close, so a task that went into flight after the
list was read is refused, never closed. The reply, or the confirmer's events, list what closed and what was refused.
"""
from __future__ import annotations

from typing import Callable, Optional

from hogwarts import capacity, owlery, pensieve
from hogwarts.errors import StoreError

from fleet import closer, common, config, owl_post, run_desk, safefs
from fleet.hooks import user_prompt_submit as hook
from fleet.safefs import FleetError

PHRASE = hook.BULK_PHRASE
LATER = f"Fix: let it finish, then type {PHRASE} again."


def _result(task: dict, closed: bool, lines: list) -> dict:
    return {"task_id": task["id"], "desk": task["desk"], "closed": closed, "lines": lines}


def _refused(task: dict, reason: str, fix: str) -> dict:
    return _result(task, False, [f"{PHRASE} refused {task['id']} ({task['desk']}): {reason}.", fix])


def _is_go(conn, task: dict) -> bool:
    return task["desk"] == hook.TASK_DESK and pensieve.task_spec(conn, task["id"]) is not None


def _run_going(conn, task: dict, now: int) -> bool:
    """Whether a launch on this task has recorded no usage yet and may still be running."""
    since = now - config.RUNNING_WINDOW_SECONDS
    return any(row["task_id"] == task["id"] and row["metric_id"] is None and row["launched_at"] > since
               for row in capacity.list_launches(conn))


def _question(conn, task: dict) -> Optional[tuple]:
    """The refusal for an open question from a desk on the task or on open work under it, else None."""
    for task_id in [task["id"], *(row["id"] for row in pensieve.open_descendants(conn, task["id"]))]:
        for owl in owlery.open_questions(conn, task_id):
            return (f"{owl['sender']} asked a question on {task_id} that has no answer yet (owl {owl['id']})",
                    f"Fix: answer that question, then type {PHRASE} again.")
    return None


def _rounds(conn, task_id: str) -> tuple:
    """(the round awaiting a verdict or None, the newest round with a verdict or None)."""
    rows = capacity.review_rounds(conn, task_id)
    pending = [row for row in rows if not row["has_verdict"] and (row["waiting"] or row["counts"])]
    judged = [row for row in rows if row["has_verdict"]]
    return (pending[-1] if pending else None), (judged[-1] if judged else None)


def _active(conn, task: dict) -> tuple:
    """Why an active task is in flight, with its Fix line."""
    task_id = task["id"]
    pending, judged = _rounds(conn, task_id)
    if pending is not None:
        return f"it awaits the verdict of review round {pending['round']}", LATER
    if judged is not None and judged["verdict"] == "CHANGES":
        return f"review round {judged['round']} recorded CHANGES, so it waits for its fix round", LATER
    if judged is not None and judged["verdict"] == "HEADMASTER":
        return (f"review round {judged['round']} recorded HEADMASTER, so the decision is yours",
                f"Fix: read review-latest.md next to its TASK.md; to close it anyway, castle token mint {task_id},"
                f" then paste that token into castle task close {task_id} --reason complete --token-stdin.")
    if judged is not None:
        return "it passed review, but its PR follow-up keeps it active", LATER
    return f"it is a live build on {task['desk']} that has not been reviewed yet", LATER


def _awaiting(conn, task: dict) -> Optional[tuple]:
    """Why a task awaiting close is not closeable, with its Fix line, or None when it is."""
    task_id = task["id"]
    if owl_post.unfinished_handoffs(task_id) or owl_post.unfinished_afters(task_id) \
            or owl_post.auto_review_running(task_id):
        return "its review loop still has work on it", LATER
    if closer.followup_open(conn, task_id):
        return "a PR follow-up is open on it, so replies to teammates may still be on their way", LATER
    under = [row["id"] for row in pensieve.open_descendants(conn, task_id)]
    if under:
        return (f"open work is under it ({', '.join(under[:4])}{' and more' if len(under) > 4 else ''})",
                f"Fix: let that finish, then type {PHRASE} again, or type Mischief managed {task_id} to close it with"
                " everything under it.")
    state = closer.close_state(conn, task_id)
    if state["unreadable"]:
        return ("its auto-close record cannot be read whole",
                f"Fix: look at it with castle task show {task_id}, then type Mischief managed {task_id}.")
    if state["judging"]:
        return "its after-merge judge's verdict is still awaited", LATER
    if state["merged"] and not state["dropped"]:
        return ("it merged, and auto-close has not proven its after-merge checks yet",
                f"Fix: let auto-close finish proving it, or type Mischief managed {task_id} to close it yourself.")
    if state["stopped"] is not None and not state["dropped"]:
        return (f"auto-close stopped it at {state['stopped']}, so its after-merge checks are not proven",
                f"Fix: read its close.stopped event, then type Mischief managed {task_id} to close it yourself, or"
                f" run fleet close {task_id} to try once more.")
    pending, judged = _rounds(conn, task_id)
    if pending is not None:
        return f"review round {pending['round']} of it awaits its verdict", LATER
    if judged is None or judged["verdict"] != "PASS":
        return ("it has no PASS on record",
                f"Fix: type Mischief managed {task_id} to close it by hand if it is done.")
    return None


def _why_not(conn, task: dict, now: int) -> Optional[tuple]:
    """(reason, Fix line) for a task that is not closeable now, read under its review lock, or None."""
    task_id = task["id"]
    if task["status"] == "closed":
        return f"it closed meanwhile, as {task['close_reason']}", "Fix: nothing to type for it."
    asked = _question(conn, task)
    if asked is not None:
        return asked
    author = capacity.round_author(conn, task_id)
    if author is not None:
        doing = "waiting for its run" if task["status"] == "queued" else "in review"
        return (f"it is {task['desk']}'s review of {author}, {doing}",
                f"Fix: nothing to type for it; it closes when that review ends, and {PHRASE} again takes the rest.")
    if _run_going(conn, task, now):
        return f"{task['desk']} has a run going on it", LATER
    if task["status"] == "queued":
        return f"it is queued for {task['desk']} and has not started", LATER
    if task["status"] == "active":
        return _active(conn, task)
    return _awaiting(conn, task)


def _label(conn, task_id: str) -> str:
    state = closer.close_state(conn, task_id)
    return f"dropped ({closer.PR_DROPPED})" if state["dropped"] else "reviewed (PASS)"


def _one(conn, task: dict, now: int) -> dict:
    """Judge one task again under its review lock, then close it through the single close's own path, or refuse it."""
    task_id = task["id"]
    try:
        with run_desk.task_lock(task_id):
            task = pensieve.get_task(conn, task_id)
            why = _why_not(conn, task, now)
            if why is not None:
                return _refused(task, *why)
            label = _label(conn, task_id)
            said = hook.close_locked(conn, task_id, now, settled=True)
    except safefs.Busy:
        return _refused(task, "a review, a run or auto-close is working on it right now", LATER)
    except (FleetError, StoreError, OSError) as exc:
        reason = common.scrubbed_line(exc, 200) if not isinstance(exc, OSError) else type(exc).__name__
        return _refused(task, f"it could not be read ({reason})",
                        f"Fix: type {PHRASE} again; if it fails the same way, type Mischief managed {task_id}.")
    if not said[0].startswith("Mischief managed:"):
        return _result(task, False, [f"{PHRASE} refused {task_id} ({task['desk']}): {said[0]}", *said[1:]])
    return _result(task, True, [f"{PHRASE} closed {task_id} ({task['desk']}), {label}, as complete.", *said[1:]])


def _go_task(conn, task_id: str, now: int) -> dict:
    """A go task, after every other task: closed once its last build has, on a build's proof, or refused saying why."""
    task = pensieve.get_task(conn, task_id)
    if task["status"] == "closed":
        return _result(task, True, [f"{PHRASE} closed {task_id} ({task['desk']}): its go task moved on with its last"
                                    " build, on a build's proof."])
    asked = _question(conn, task)
    if asked is not None:
        return _refused(task, *asked)
    under = pensieve.open_descendants(conn, task_id)
    if under:
        named = ", ".join(f"{row['id']} ({row['status'].replace('_', ' ')})" for row in under[:4])
        return _refused(task, f"its go task waits on its build {named}", LATER)
    [moved] = closer.advance_parents(conn, now, only=task_id)
    if moved["outcome"] == "closed":
        return _result(task, True, [f"{PHRASE} closed {task_id} ({task['desk']}): its builds have all closed, so its"
                                    f" go task moved on, on the proof of build {moved['via']}."])
    by_hand = (f"Fix: castle task close {task_id} --reason superseded in your terminal closes it, since it holds no"
               " work of its own.")
    if moved.get("why") == closer.AUTO_CLOSE_OFF:
        return _refused(task, "auto-close is off, so no build's proof can close it", by_hand)
    if moved.get("why") == closer.NO_PROOF:
        return _refused(task, "its builds have all closed, but none by a proven close, so no proof closes it", by_hand)
    return _refused(task, f"its go task cannot move on yet: {moved.get('why')}", LATER)


def close_everything(conn, now: int, on_result: Optional[Callable[[dict], None]] = None) -> list:
    """One result per open task, oldest first and go tasks last: {task_id, desk, closed, lines}. lines[0] says what
    became of it; a refusal's next line is its Fix line. on_result gets each result as soon as that task is done, so a
    caller can record it before the next close begins."""
    tasks = pensieve.list_tasks(conn, open_only=True)
    goes = [task["id"] for task in tasks if _is_go(conn, task)]
    results = []
    for task in [task for task in tasks if task["id"] not in goes] + [task for task in tasks if task["id"] in goes]:
        results.append(_go_task(conn, task["id"], now) if task["id"] in goes else _one(conn, task, now))
        if on_result is not None:
            on_result(results[-1])
    return results


def head(results: list) -> str:
    closed = [result["task_id"] for result in results if result["closed"]]
    if not results:
        return f"{PHRASE}: no task is open, so nothing was closed."
    return (f"{PHRASE}: {len(closed)} closed, {len(results) - len(closed)} refused; every open task is named below,"
            " each refusal with what to type next.")


def lines(results: list) -> list:
    """The reply: the head line, then each task's lines, indented under its own first line."""
    out = [head(results)]
    for result in results:
        first, *rest = result["lines"]
        out.append(f"- {first}")
        out += [f"  {line}" for line in rest]
    return out
