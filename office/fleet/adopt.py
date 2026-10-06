"""fleet adopt <parent-task-id>: let the closer take a build registered by hand.

The closer only takes a build whose parent on McGonagall's desk has a task spec, and only a go records one
(pensieve.record_spec, with the TASK.md sha256 as Ryan's approval). A build registered with castle task create after a
go could not be applied has none, so it stays his to close. This command records that spec once he has looked:
- the parent must be a queued McGonagall task (the store records a spec only on one) with no spec and its TASK.md at ~/hogwarts/tasks/<id>/TASK.md, with exactly
  one open build-desk task under it that has its worktree and office record;
- the TASK.md Spec block is read with the go hook's own read_spec, and its repo folder, branch and base must equal the
  worktree record's repo_dir, branch and base_ref;
- it shows the parent, the build, the repo, branch, base and TASK.md title, and Ryan types the parent id back. There
  is no --yes: the typed id is his approval, so the command runs only with a terminal on stdin and stdout;
- in one store transaction, after reading TASK.md again and finding the same bytes, it records the spec with the
  sha256 of those bytes. Nothing it prints carries that hash.
"""
from __future__ import annotations

import hashlib
import sys
from typing import Callable, Optional

from hogwarts import db, ids, pensieve

from fleet import common, config, gitops, safefs, verify, worktree
from fleet.hooks import user_prompt_submit as hook
from fleet.safefs import FleetError

def ask_terminal(prompt: str) -> str:
    """Ask on Ryan's terminal. Refused when stdin or stdout is not one, so no script or pipe can answer."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise FleetError("fleet adopt asks you to type the task id back, so it runs only in your own terminal")
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline()


def _child(conn, parent_id: str) -> dict:
    children = [task for task in pensieve.list_tasks(conn, open_only=True)
                if task["parent_task_id"] == parent_id and task["desk"] in config.WORKTREE_DESKS]
    if not children:
        raise FleetError(f"{parent_id} has no open build task under it")
    if len(children) > 1:
        raise FleetError(f"{parent_id} has {len(children)} open build tasks under it"
                         f" ({', '.join(task['id'] for task in children)}), and adopt takes exactly one")
    return children[0]


def _checked(conn, parent_id: str) -> tuple:
    """(parent, child, record) once every store and record check passes."""
    parent = pensieve.get_task(conn, parent_id)
    if parent["desk"] != hook.TASK_DESK:
        raise FleetError(f"{parent_id} is not a task of {hook.TASK_DESK}")
    if parent["status"] != "queued":
        raise FleetError(f"{parent_id} is {parent['status']}, and a spec is recorded only on a queued task, as a go"
                         " records it")
    if pensieve.task_spec(conn, parent_id) is not None:
        raise FleetError(f"{parent_id} already has its spec, from a go or an earlier adopt, and it never changes")
    if parent["intent_path"] != ids.intent_path(parent_id):
        raise FleetError(f"{parent_id} is not registered with its TASK.md at ~/hogwarts/tasks/{parent_id}/TASK.md")
    child = _child(conn, parent_id)
    if not child["worktree"]:
        raise FleetError(f"build task {child['id']} has no worktree yet")
    record = gitops.find_record(worktree.castle_path(child["worktree"]))
    if record is None or record["task_id"] != child["id"]:
        raise FleetError(f"build task {child['id']} has no worktree record")
    if record["base_ref"] is None:
        raise FleetError(f"the worktree record of {child['id']} does not name the base it was made from")
    return parent, child, record


def _task_md(parent_id: str) -> bytes:
    try:
        return verify.read_task_md(parent_id)
    except safefs.Missing:
        raise FleetError(f"there is no TASK.md at ~/hogwarts/tasks/{parent_id}/TASK.md") from None


def adopt(conn, parent_id: str, confirm: Optional[Callable[[str], str]]) -> dict:
    """Record a go spec on a hand-registered parent once Ryan types its id back. confirm is required."""
    if confirm is None:
        raise FleetError("fleet adopt needs you to type the task id back")
    parent_id = ids.check("task", parent_id)
    parent, child, record = _checked(conn, parent_id)
    raw = _task_md(parent_id)
    spec = hook.read_spec(raw, parent_id)
    found = (spec["repo_dir"], spec["branch"], spec["base"])
    if found != (record["repo_dir"], record["branch"], record["base_ref"]):
        raise FleetError(f"the TASK.md Spec says repo {spec['repo_dir']}, branch {spec['branch']}, base {spec['base']},"
                         f" but {child['id']}'s worktree is repo {record['repo_dir']}, branch {record['branch']},"
                         f" base {record['base_ref']}, so nothing was recorded")
    summary = (f"Adopt {parent_id} for auto-close:\n"
               f"  parent: {parent_id} ({parent['status']}) {common.one_line(parent['title'], 120)}\n"
               f"  build:  {child['id']} ({child['desk']}, {child['status']})\n"
               f"  repo:   {spec['repo_dir']}\n"
               f"  branch: {spec['branch']}\n"
               f"  base:   {spec['base']}\n"
               f"  TASK.md title: {common.one_line(spec['title'], 120)}\n"
               "Recording this is your approval of the TASK.md as it is now, as a go would be.\n")
    answer = confirm(summary + "Type the parent task id to record it, or anything else to stop: ")
    if not isinstance(answer, str) or answer.strip() != parent_id:
        raise FleetError("the typed id did not match, so nothing was recorded")
    with db.transaction(conn):
        _, again, _ = _checked(conn, parent_id)  # read again under the transaction
        if again["id"] != child["id"]:
            raise FleetError("the build task under it changed, so nothing was recorded")
        if _task_md(parent_id) != raw:
            raise FleetError("TASK.md changed while you were reading it, so nothing was recorded; run adopt again")
        pensieve.record_spec(conn, parent_id, spec["repo_dir"], spec["branch"], spec["base"],
                             hashlib.sha256(raw).hexdigest())
    return {"task_id": parent_id, "build_task_id": child["id"], "repo_dir": spec["repo_dir"],
            "branch": spec["branch"], "base": spec["base"],
            "note": (f"{parent_id} is adopted: with auto-close on, the closer can now close {child['id']} and"
                     f" {parent_id} once every criterion is proven. Any later edit to its TASK.md leaves it to be"
                     " closed by hand, as after a go.")}
