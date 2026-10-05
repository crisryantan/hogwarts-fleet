"""The worktree script: give a build task its own git worktree, then start the desk.

Ryan runs it from his terminal, after the Owl Post says a build task is waiting for its worktree:
  fleet worktree <task-id> --repo-dir <main checkout> --branch <name> [--base origin/main] [--no-fetch]
It refuses rather than guesses:
- the task must be a queued task of a build desk (Harry) with no worktree yet, and the desk must be free
  to start it: Harry takes many tasks, but no two of his open tasks may share one TASK.md, since the
  evidence, handoff and reviews are written next to it. One worktree command per TASK.md runs at a time:
  it takes that TASK.md's lock without waiting before it checks, and holds it until the task is active, so
  a second command for a task under the same TASK.md is refused at once and changes nothing. The last
  check, the attach and the start share one store transaction. When that transaction refuses, say a
  single-task Harry started a task under another TASK.md meanwhile, or McGonagall closed the parent, the
  command takes back the worktree, its new branch and its record before it says why;
- the repo must be a main checkout in Ryan's home, outside the office and the castle, with a GitHub origin;
- the branch must be new, plain and free of fleet words.
Then it fetches the base (unless --no-fetch), adds ~/hogwarts/worktrees/<task-id> on a new branch,
writes the office record, attaches the worktree to the task, starts the task and its request, and
starts the desk's run if Ryan has enabled the desk.

  fleet build <task-id>     starts the desk again on the same task, for a fix round after a review. The review
                            loop starts a fix round through build() itself after a CHANGES verdict, so this is the
                            fallback. Both run under the task's review lock, so neither starts the desk mid-review.
  fleet worktree-remove <task-id>   removes a closed task's worktree. It never deletes the branch.

castle task start goes through start_task for a build desk's task, which makes the same TASK.md check, so it is
no way round it. Every other desk's task starts as the store allows.

Git always runs through gitops, so no hook in the repo runs.
"""
from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional

from hogwarts import db, ids, owlery, pensieve

from fleet import config, gitops, run_desk, safefs, toolchain, verify
from fleet.safefs import FleetError

WORKTREE_RUNNING = "another worktree command for a task under this TASK.md is running; run it again when it ends"


def _real_worktree(name: str) -> str:
    """The worktree path as the store holds it, always under the real castle."""
    return f"{ids.WORKTREES_ROOT}/{name}"


def castle_path(store_path: Optional[str]) -> Optional[str]:
    """A worktree path from the store, mapped onto config.CASTLE_ROOT (the same folder outside tests)."""
    if not store_path:
        return None
    if not store_path.startswith(ids.WORKTREES_ROOT + "/"):
        raise FleetError("the stored worktree is outside the castle worktrees folder")
    return config.CASTLE_ROOT + store_path[len(ids.CASTLE_ROOT):]


def _request_owl(conn, task: dict) -> str:
    """The request owl that asked this desk for this task."""
    if task["request_id"] is None:
        raise FleetError("this task did not come from a request, so there is no owl to start the desk with")
    for owl in owlery.request_owls(conn, task["request_id"]):
        if owl["kind"] == "request" and owl["recipient"] == task["desk"]:
            return owl["id"]
    raise FleetError("the request owl for this task was not found")


def add_worktree(conn, task_id: str, repo_dir: str, base: str, branch: Optional[str], fetch: bool,
                 detach_at: Optional[str] = None) -> dict:
    """Add ~/hogwarts/worktrees/<task-id> and write its office record. Shared with the review script."""
    repo_dir = gitops.check_repo_dir(repo_dir)
    base = gitops.check_ref(base, "base")
    common_dir = f"{repo_dir}/.git"
    path = config.worktree_dir(task_id)
    if os.path.lexists(path):
        raise FleetError("a worktree folder for this task already exists")
    slug = gitops.repo_slug(gitops.git(["config", "--get", "remote.origin.url"], common_dir))
    if fetch and base.startswith("origin/"):
        gitops.git(["fetch", "--no-tags", "origin", base[len("origin/"):]], common_dir)
    # The record keeps the commit the base names now, not the name: a branch like main moves on, and a review's
    # diff against a moved name shows the wrong change, or nothing at all.
    base_sha = gitops.git(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], common_dir).strip()
    if gitops.SHA.fullmatch(base_sha) is None:
        raise FleetError("git did not return a full commit sha for the base")
    if branch is not None:
        branch = gitops.check_branch(branch)
        if _branch_exists(common_dir, branch):
            raise FleetError("that branch already exists; pick a new branch name")
        gitops.git(["worktree", "add", "-b", branch, path, base_sha], common_dir)
    else:
        if detach_at is None or gitops.SHA.fullmatch(detach_at) is None:
            raise FleetError("a detached worktree needs a full commit sha")
        gitops.git(["worktree", "add", "--detach", path, detach_at], common_dir)
    record = {"name": task_id, "task_id": task_id, "path": path, "repo_dir": repo_dir, "common_dir": common_dir,
              "git_dir": f"{common_dir}/worktrees/{task_id}", "branch": branch, "base": base_sha, "base_ref": base, "repo": slug,
              "links": toolchain.linkable(repo_dir)}
    if not os.path.isdir(record["git_dir"]):
        raise FleetError("git named the worktree differently than expected; remove it by hand and retry")
    toolchain.link_deps(record, record["links"])
    return gitops.write_record(record)


def _branch_exists(common_dir: str, branch: str) -> bool:
    out = gitops.git(["for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"], common_dir, check=False)
    return bool(out.strip())


def _holder(conn, task_id: str) -> str:
    try:
        holder, _ = verify.task_md(conn, task_id)
    except FleetError:
        return task_id
    return holder


def _check_startable(conn, task: dict) -> None:
    """Refuse, before any worktree is added, a task its desk cannot start, or one whose TASK.md another open
    task of the desk already works under."""
    busy = pensieve.blocking_task(conn, task["desk"])
    if busy is not None:
        raise FleetError(f"{task['desk']} already has an active task {busy['id']}")
    holder = _holder(conn, task["id"])
    for other in pensieve.list_tasks(conn, desk=task["desk"], open_only=True):
        if other["id"] != task["id"] and other["status"] != "queued" and _holder(conn, other["id"]) == holder:
            raise FleetError(f"task {other['id']} of {task['desk']} is still open under the same TASK.md"
                             f" ({holder}); finish or close it first")


@contextlib.contextmanager
def holder_lock(holder: str) -> Iterator[None]:
    """One worktree command per TASK.md at a time, taken without waiting before the holder check and held
    until the task is active. A second command under the same TASK.md is refused at once."""
    holder = ids.check("task", holder)
    with contextlib.ExitStack() as stack:
        locks_fd = stack.enter_context(safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True))
        try:
            stack.enter_context(safefs.held_lock(locks_fd, f"worktree-{holder}.lock", blocking=False))
        except safefs.Busy:
            raise FleetError(WORKTREE_RUNNING) from None
        yield


def start_task(conn, task_id: str) -> dict:
    """Start a queued build task for castle task start, with the checks fleet worktree makes before it starts
    one: the desk must be free to start it, and no other open task of the desk may work under the same TASK.md.
    It takes that TASK.md's lock without waiting, and the last check and the start share one store transaction,
    so a start here and a start there never both get through. A task that is not queued gets the store's own
    refusal."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    with holder_lock(_holder(conn, task["id"])):
        with db.transaction(conn):
            task = pensieve.get_task(conn, task["id"])  # read again under the lock and the transaction
            if task["status"] == "queued":
                _check_startable(conn, task)
            return pensieve.start_task(conn, task["id"])


def _take_back(record: dict, made_at: Optional[str], exc: BaseException) -> None:
    """Undo add_worktree for a task that was refused before it got the worktree, so the task stays queued with
    nothing left on disk and the same command can run again. The branch goes only while it still sits where the
    worktree was made. If the undo fails, the refusal says what is left to remove by hand."""
    branch, common_dir = record["branch"], record["common_dir"]
    path, branch_left = f"the worktree {record['path']}", f"branch {branch}"
    left = [path] + ([branch_left] if branch is not None else []) + [f"the record {record['name']}.json"]
    try:
        toolchain.unlink_deps(record)
        gitops.git(["worktree", "remove", record["path"]], common_dir)
        left.remove(path)
        if branch is not None and made_at is not None:
            tip = gitops.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], common_dir, check=False)
            if tip.strip() == made_at:
                gitops.git(["update-ref", "-d", f"refs/heads/{branch}", made_at], common_dir)
            left.remove(branch_left)
        gitops.drop_record(record["name"])
    except Exception as undo:
        raise FleetError(f"{exc}; taking back the new worktree also failed ({undo}), so remove"
                         f" {' and '.join(left)} by hand") from exc


def start_desk(conn, task: dict) -> str:
    """Start the desk's run on its request owl, when Ryan has enabled the desk. Returns what happened."""
    owl_id = _request_owl(conn, task)
    if not run_desk.is_enabled(task["desk"]):
        return f"{task['desk']} is not enabled, so nothing was started"
    if run_desk.over_daily_cap(conn, task["desk"]) is not None:
        run_desk.report_cap(conn, task["desk"])
        return f"{task['desk']} reached its daily cap, so nothing was started"
    run_desk.spawn(task["desk"], owl_id)
    return f"started {task['desk']} on owl {owl_id}"


def create(conn, task_id: str, repo_dir: str, branch: str, base: str = config.DEFAULT_BASE,
           fetch: bool = True) -> dict:
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS:
        raise FleetError("only a build desk's task gets a worktree from this script")
    branch = gitops.check_branch(branch)
    with holder_lock(_holder(conn, task["id"])):
        task = pensieve.get_task(conn, task["id"])  # read again under the lock
        if task["status"] != "queued" or task["worktree"] is not None:
            raise FleetError("the task must be queued and have no worktree yet")
        _check_startable(conn, task)
        record = add_worktree(conn, task["id"], repo_dir, base, branch, fetch)
        made_at = None
        try:
            made_at = gitops.rev(record)
            with db.transaction(conn):
                # BEGIN IMMEDIATE: the check sees every start committed before it, and no start lands in between.
                _check_startable(conn, task)
                pensieve.set_worktree(conn, task["id"], _real_worktree(task["id"]))
                task = pensieve.start_task(conn, task["id"])
        except BaseException as exc:
            _take_back(record, made_at, exc)
            raise
    if task["request_id"] is not None:
        owlery.advance(conn, task["request_id"], "claimed", detail="worktree attached")
        owlery.advance(conn, task["request_id"], "running", detail="build desk started")
    started = start_desk(conn, task)
    return {"task_id": task["id"], "worktree": record["path"], "branch": record["branch"], "base": record["base"],
            "repo": record["repo"], "desk": started}


def build(conn, task_id: str) -> dict:
    """Start the build desk again on its own task, for a fix round."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS or task["status"] != "active" or not task["worktree"]:
        raise FleetError("only an active build task with a worktree can be started again")
    return {"task_id": task["id"], "desk": start_desk(conn, task)}


def remove(conn, task_id: str) -> dict:
    """Remove a closed task's worktree. Git refuses if it has uncommitted changes. The branch stays."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["status"] != "closed":
        raise FleetError("only a closed task's worktree can be removed")
    record = gitops.find_record(castle_path(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree record")
    if gitops.dirty(record):
        raise FleetError("the worktree has uncommitted changes; git would refuse to remove it")
    toolchain.unlink_deps(record)
    gitops.git(["worktree", "remove", record["path"]], record["common_dir"])
    return {"task_id": task["id"], "removed": record["path"], "branch_kept": record["branch"]}
