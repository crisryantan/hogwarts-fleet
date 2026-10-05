"""The worktree script: give a build task its own git worktree, then start the desk.

Most builds never need the command. When Ryan types "go <task-id>" on a drafted TASK.md, the UserPromptSubmit
hook registers the task, routes it to Harry and calls create here with the repo folder, branch and base it
stored from the TASK.md Spec, then start_desk once its store transaction has committed (see
fleet/hooks/user_prompt_submit.py). Every check below runs for it the same way. The command is the fallback,
for a go the hook could not confirm or a build task McGonagall routed by owl. Ryan runs it from his terminal,
after the Owl Post says a build task is waiting for its worktree:
  fleet worktree <task-id> --repo-dir <main checkout> --branch <name> [--base origin/main] [--no-fetch]
It refuses rather than guesses:
- the task must be a queued task of a build desk (Harry) with no worktree yet, and the desk must be free
  to start it: Harry takes many tasks, but no two of his open tasks may share one TASK.md, since the
  evidence, handoff and reviews are written next to it. One worktree command per TASK.md runs at a time:
  it takes that TASK.md's lock without waiting before it checks, and holds it until the task is active, so
  a second command for a task under the same TASK.md is refused at once and changes nothing. The last
  check, the attach, the start and the request's claimed and running phases share one store transaction.
  When anything refuses after git starts making the worktree and before that transaction commits, say a
  single-task Harry started a task under another TASK.md meanwhile, or McGonagall closed the parent, the
  command takes back the worktree, its new branch and its record, however far it got, before it says why;
- the repo must be a main checkout in Ryan's home, outside the office and the castle, with a GitHub origin;
- the branch must be new, plain and free of fleet words;
- under a TASK.md that a go started, the repo folder, branch and base must be the ones the go stored.
Then it fetches the base (unless --no-fetch), adds ~/hogwarts/worktrees/<task-id> on a new branch,
writes the office record, attaches the worktree to the task, starts the task and its request, and
starts the desk's run if Ryan has enabled the desk.

  fleet build <task-id>     starts the desk again on the same task, for a fix round after a review.
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


def _fetch(common_dir: str, base: str) -> None:
    if base.startswith("origin/"):
        gitops.git(["fetch", "--no-tags", "origin", base[len("origin/"):]], common_dir)


def fetch_base(repo_dir: str, base: str) -> None:
    """The fetch add_worktree makes, after the same checks of the checkout and its GitHub origin. The go hook
    runs it before it opens its store transaction, so no network wait holds the store, then calls create with
    fetch=False, which checks everything again."""
    repo_dir = gitops.check_repo_dir(repo_dir)
    base = gitops.check_ref(base, "base")
    common_dir = f"{repo_dir}/.git"
    origin = gitops.git(["config", "--get", "remote.origin.url"], common_dir, check=False)
    if not origin.strip():
        raise FleetError("the repo has no origin remote")
    gitops.repo_slug(origin)
    _fetch(common_dir, base)


def add_worktree(conn, task_id: str, repo_dir: str, base: str, branch: Optional[str], fetch: bool,
                 detach_at: Optional[str] = None, *, claim: dict) -> dict:
    """Add ~/hogwarts/worktrees/<task-id> and write its office record. Shared with the review script.

    The caller owns taking back whatever this makes: before the first git change, claim["record"] holds the record
    this will write, so take_back(claim, exc) undoes the worktree, its new branch, the links and the record, however
    far it got, with no record saved yet."""
    repo_dir = gitops.check_repo_dir(repo_dir)
    base = gitops.check_ref(base, "base")
    common_dir = f"{repo_dir}/.git"
    path = config.worktree_dir(task_id)
    if os.path.lexists(path):
        raise FleetError("a worktree folder for this task already exists")
    slug = gitops.repo_slug(gitops.git(["config", "--get", "remote.origin.url"], common_dir))
    if fetch:
        _fetch(common_dir, base)
    # The record keeps the commit the base names now, not the name: a branch like main moves on, and a review's
    # diff against a moved name shows the wrong change, or nothing at all.
    base_sha = gitops.git(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], common_dir).strip()
    if gitops.SHA.fullmatch(base_sha) is None:
        raise FleetError("git did not return a full commit sha for the base")
    if branch is not None:
        branch = gitops.check_branch(branch)
        if _branch_exists(common_dir, branch):
            raise FleetError("that branch already exists; pick a new branch name")
        add = ["worktree", "add", "-b", branch, path, base_sha]
    else:
        if detach_at is None or gitops.SHA.fullmatch(detach_at) is None:
            raise FleetError("a detached worktree needs a full commit sha")
        add = ["worktree", "add", "--detach", path, detach_at]
    record = {"name": task_id, "task_id": task_id, "path": path, "repo_dir": repo_dir, "common_dir": common_dir,
              "git_dir": f"{common_dir}/worktrees/{task_id}", "branch": branch, "base": base_sha, "base_ref": base, "repo": slug,
              "links": toolchain.linkable(repo_dir)}
    claim["record"] = record  # from here the caller takes back what git makes
    gitops.git(add, common_dir)
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


def _check_spec(conn, holder: str, repo_dir: str, branch: str, base: str) -> None:
    """A task under a TASK.md that Ryan's go started gets its worktree only from the repo folder, branch and
    base the go stored, whichever command asks for it."""
    spec = pensieve.task_spec(conn, holder)
    if spec is not None and (repo_dir, branch, base) != (spec["repo_dir"], spec["branch"], spec["base"]):
        raise FleetError(f"the TASK.md of {holder} was started with go, so its worktree takes only the repo folder,"
                         " branch and base stored at go")


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


def _take_back(record: dict, exc: BaseException) -> None:
    """Undo add_worktree, however far it got, for a task that never got the worktree, so the task stays queued with
    nothing left on disk and the same command can run again. The new branch goes only while it still sits at the
    base commit add_worktree resolved before it made anything. If the undo fails, or the branch's tip cannot be
    read, the refusal says what is left to remove by hand."""
    branch, common_dir, base_sha = record["branch"], record["common_dir"], record["base"]
    path, branch_left = f"the worktree {record['path']}", f"branch {branch}"
    left = [path] + ([branch_left] if branch is not None else []) + [f"the record {record['name']}.json"]
    try:
        toolchain.unlink_deps(record)
        if os.path.lexists(record["path"]):
            gitops.git(["worktree", "remove", record["path"]], common_dir)
        left.remove(path)
        if branch is not None:
            tip = gitops.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], common_dir, check=False)
            if tip.strip() == base_sha:
                gitops.git(["update-ref", "-d", f"refs/heads/{branch}", base_sha], common_dir)
            elif not tip.strip() and gitops.has_branch(common_dir, branch):
                raise FleetError(f"git could not read where branch {branch} points")
            left.remove(branch_left)
        with contextlib.suppress(FileNotFoundError, safefs.Missing):
            gitops.drop_record(record["name"])
    except Exception as undo:
        raise FleetError(f"{exc}; taking back the new worktree also failed ({undo}), so remove"
                         f" {' and '.join(left)} by hand") from exc


def take_back(claim: dict, exc: BaseException) -> None:
    """Undo add_worktree for the caller that owns claim, so nothing is left on disk: the worktree, its new branch
    (while it still sits at the base it was made at), the links and the record go, from the record in claim, so a
    partial worktree with no saved record goes too. An empty claim has nothing to take back. If the undo fails, the
    FleetError says what is left to remove by hand."""
    if "record" in claim:
        _take_back(claim["record"], exc)


def kept(conn, claim: dict) -> bool:
    """Whether the store has committed the worktree in claim to its task, so it is the task's and nothing takes it
    back. False while a transaction is open, since it may still roll back, and when the store cannot say."""
    if "record" not in claim or conn.in_transaction:
        return False
    try:
        return pensieve.get_task(conn, claim["record"]["task_id"])["worktree"] is not None
    except Exception:  # noqa: BLE001 - a task the store does not have, or cannot read, never kept a worktree
        return False


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
           fetch: bool = True, start: bool = True, claim: Optional[dict] = None) -> dict:
    """Give a queued build task its worktree and start it, then start the desk's run. With start=False the run
    is left to the caller, which starts it with start_desk once its own store transaction has committed, so the
    run never reads a store that has not got its task yet (the go hook).

    Without claim, create takes back what it made when anything refuses before its store transaction commits. A
    caller that runs create inside its own transaction passes claim, an empty dict, and owns that from the start:
    claim["record"] is set before the first git change, and the caller calls take_back(claim, exc) when its own
    transaction rolls back, whether create refused or something after it did."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS:
        raise FleetError("only a build desk's task gets a worktree from this script")
    branch = gitops.check_branch(branch)
    made = {} if claim is None else claim
    holder = _holder(conn, task["id"])
    with holder_lock(holder):
        task = pensieve.get_task(conn, task["id"])  # read again under the lock
        if task["status"] != "queued" or task["worktree"] is not None:
            raise FleetError("the task must be queued and have no worktree yet")
        _check_spec(conn, holder, repo_dir, branch, base)
        _check_startable(conn, task)
        try:
            record = add_worktree(conn, task["id"], repo_dir, base, branch, fetch, claim=made)
            with db.transaction(conn):
                # BEGIN IMMEDIATE: the check sees every start committed before it, and no start lands in between.
                _check_startable(conn, task)
                pensieve.set_worktree(conn, task["id"], _real_worktree(task["id"]))
                task = pensieve.start_task(conn, task["id"])
                if task["request_id"] is not None:
                    owlery.advance(conn, task["request_id"], "claimed", detail="worktree attached")
                    owlery.advance(conn, task["request_id"], "running", detail="build desk started")
        except BaseException as exc:
            if claim is None and not kept(conn, made):
                take_back(made, exc)
            raise
    started = start_desk(conn, task) if start else None
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
