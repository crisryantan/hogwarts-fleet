"""The worktree script: give a build task its own git worktree, then start the desk.

Ryan runs it from his terminal, after the Owl Post says a build task is waiting for its worktree:
  fleet worktree <task-id> --repo-dir <main checkout> --branch <name> [--base origin/main] [--no-fetch]
It refuses rather than guesses:
- the task must be a queued task of a build desk (Harry) with no worktree yet;
- the repo must be a main checkout in Ryan's home, outside the office and the castle, with a GitHub origin;
- the branch must be new, plain and free of fleet words.
Then it fetches the base (unless --no-fetch), adds ~/hogwarts/worktrees/<task-id> on a new branch,
writes the office record, attaches the worktree to the task, starts the task and its request, and
starts the desk's run if Ryan has enabled the desk.

  fleet build <task-id>     starts the desk again on the same task, for a fix round after a review.
  fleet worktree-remove <task-id>   removes a closed task's worktree. It never deletes the branch.

Git always runs through gitops, so no hook in the repo runs.
"""
from __future__ import annotations

import os
from typing import Optional

from hogwarts import ids, owlery, pensieve

from fleet import config, gitops, run_desk, toolchain
from fleet.safefs import FleetError


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
    gitops.git(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], common_dir)
    if branch is not None:
        branch = gitops.check_branch(branch)
        if _branch_exists(common_dir, branch):
            raise FleetError("that branch already exists; pick a new branch name")
        gitops.git(["worktree", "add", "-b", branch, path, base], common_dir)
    else:
        if detach_at is None or gitops.SHA.fullmatch(detach_at) is None:
            raise FleetError("a detached worktree needs a full commit sha")
        gitops.git(["worktree", "add", "--detach", path, detach_at], common_dir)
    record = {"name": task_id, "task_id": task_id, "path": path, "repo_dir": repo_dir, "common_dir": common_dir,
              "git_dir": f"{common_dir}/worktrees/{task_id}", "branch": branch, "base": base, "repo": slug,
              "links": toolchain.linkable(repo_dir)}
    if not os.path.isdir(record["git_dir"]):
        raise FleetError("git named the worktree differently than expected; remove it by hand and retry")
    toolchain.link_deps(record, record["links"])
    return gitops.write_record(record)


def _branch_exists(common_dir: str, branch: str) -> bool:
    out = gitops.git(["for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"], common_dir, check=False)
    return bool(out.strip())


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
    if task["status"] != "queued" or task["worktree"] is not None:
        raise FleetError("the task must be queued and have no worktree yet")
    branch = gitops.check_branch(branch)
    record = add_worktree(conn, task["id"], repo_dir, base, branch, fetch)
    pensieve.set_worktree(conn, task["id"], _real_worktree(task["id"]))
    task = pensieve.start_task(conn, task["id"])
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
    gitops.git(["worktree", "remove", record["path"]], record["common_dir"])
    return {"task_id": task["id"], "removed": record["path"], "branch_kept": record["branch"]}
