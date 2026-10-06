"""The worktree script: give a build task its own git worktree, then start the desk.

Most builds never need the command. When Ryan types "go <task-id>" on a drafted TASK.md, the UserPromptSubmit
hook registers the task, routes it to Harry and calls create here with the repo folder, branch and base it
stored from the TASK.md Spec, then start_locked once its store transaction has committed (see
fleet/hooks/user_prompt_submit.py). Every check below runs for it the same way. The command is the fallback,
for a go the hook could not confirm or a build task McGonagall routed by owl. Ryan runs it from his terminal,
after the Owl Post says a build task is waiting for its worktree:
  fleet worktree <task-id> --repo-dir <main checkout> --branch <name> [--base origin/main] [--no-fetch]
The task id may be McGonagall's own, the one her TASK.md carries: when it is not a build desk's task and exactly one
open build-desk task sits under it, that one is used and the result says so; with none or several it refuses and
names them (build_task). It refuses rather than guesses:
- the task must be a queued task of a build desk (Harry) with no worktree yet, and the desk must be free
  to start it: Harry takes many tasks, but no two of his open tasks may share one TASK.md, since the
  evidence, handoff and reviews are written next to it. One worktree command per TASK.md runs at a time:
  it takes that TASK.md's lock without waiting before it checks, and holds it until the task is active, so
  a second command for a task under the same TASK.md is refused at once and changes nothing. The last
  check, the attach, the start and the request's claimed and running phases share one store transaction.
  When anything refuses after git starts making the worktree and before that transaction commits, say a
  single-task Harry started a task under another TASK.md meanwhile, or McGonagall closed the parent, the
  command takes back the worktree, its new branch and its record, however far it got, before it says why.
  When the store cannot say whether the task kept its worktree, it takes back nothing and says what is left;
- the repo must be a main checkout in Ryan's home, outside the office and the castle, with a GitHub origin, and on
  macOS outside the home folders launchd jobs cannot read (config.PROTECTED_HOME_DIRS: Documents, Desktop, Downloads,
  iCloud Drive), since the Owl Post, the Map and the closer run as launchd jobs;
- the branch must be new, plain and free of fleet words. One command makes a given branch in a given repo at a
  time, whichever TASK.md asks for it: it takes that branch's lock without waiting before it checks the branch
  is new, and holds it until the task has the worktree or it is taken back. A take-back removes the branch only
  when git made it for that command;
- under a TASK.md that a go started, the repo folder, branch and base must be the ones the go stored.
Then it fetches the base (unless --no-fetch), adds ~/hogwarts/worktrees/<task-id> on a new branch,
writes the office record, attaches the worktree to the task, starts the task and its request, and
starts the desk's run if Ryan has enabled the desk.

  fleet build <task-id>     starts the desk again on the same task, for a fix round after a review. The review
                            loop starts a fix round through build() itself after a CHANGES verdict, so this is the
                            fallback. During a PR follow-up it starts the desk on that follow-up's own owl. Both run under the task's review lock, so neither starts the desk mid-review,
                            and hand that lock to the run they start, which holds it until its process ends, so no
                            review starts while the desk may still be writing (see run_desk.task_lock). fleet
                            worktree starts the first run the same way.
  fleet worktree-remove <task-id>   removes a closed task's worktree, under the task's review lock. It never deletes
                                    the branch, and it finishes a removal a kill cut short.

Closed tasks' worktrees also go by themselves, through remove_closed, the same path and checks as the command. The
closer removes the worktree of a build it closes on proof, in the same pass, under auto-close's switch. With the
worktree-cleanup switch on, each Map round (sweep_closed) removes the worktree of every build task closed by any path at
least config.WORKTREE_CLEANUP_AFTER_SECONDS ago. Either one removes a worktree only while git lists it, its folder is a
plain folder of yours in the castle worktrees folder, it has no uncommitted changes, no git-ignored files (the
toolchain's own dependency links aside, each checked against the office record) and no tracked file marked
assume-unchanged or skip-worktree, whose changes git status hides, and its HEAD is somewhere a removal cannot lose:
the commit the close proved, or a commit on the base or the branch on origin after a fetch. Anything it cannot read
keeps the worktree, and you hear once. Only git worktree remove runs, without --force: no branch is
deleted, and git worktree prune never runs. Marker files next to the office records make a removal a kill cut short
finish on a later round, or tell you once when it cannot: the closer's intent (<task>.closing), written before its
close commits, then <task>.removing, <task>.removed and the sweep's <task>.unreported, which share the removal's own
identity so no removal is told twice. The switch that started a removal is read again right before git worktree
remove: off, nothing is removed and its intent or marker stays. Your own fleet worktree-remove keeps git's own rule,
which deletes ignored files.

castle task start goes through start_task for a build desk's task, which makes the same TASK.md check, so it is
no way round it. Every other desk's task starts as the store allows.

Git always runs through gitops, so no hook in the repo runs.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from typing import Iterator, Optional

from hogwarts import db, ids, owlery, pensieve
from hogwarts.errors import NotFoundError, StoreError

from fleet import common, config, gitops, run_desk, safefs, toolchain, verify
from fleet.safefs import FleetError

WORKTREE_RUNNING = "another worktree command for a task under this TASK.md is running; run it again when it ends"
BRANCH_RUNNING = "another worktree command for this branch in this repo is running; run it again when it ends"


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
                 detach_at: Optional[str] = None, *, claim: dict, name: Optional[str] = None) -> dict:
    """Add ~/hogwarts/worktrees/<name> and write its office record, name being the task id unless the caller names
    another (the closer's merged worktree, <task-id>.merged-<12 hex>). Shared with the review script and the closer.

    The caller owns taking back whatever this makes: before the first git change, claim["record"] holds the record
    this will write, so take_back(claim, exc) undoes the worktree, its new branch, the links and the record, however
    far it got, with no record saved yet. A new branch needs a claim from branch_claim for it, held from before the
    check that the branch is new until the caller has taken back what this made or its task has the worktree.
    claim["made_branch"] is set only once git has made the branch, so a take-back never removes one it did not."""
    repo_dir = gitops.check_repo_dir(repo_dir)
    base = gitops.check_ref(base, "base")
    common_dir = f"{repo_dir}/.git"
    name = ids.check("task", task_id) if name is None else safefs.check_component(name)
    path = config.worktree_dir(name)
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
        if claim.get("branch_lock") != _branch_lock_name(common_dir, branch):
            raise FleetError("a new branch is made only under its branch lock")
        if _branch_exists(common_dir, branch):
            raise FleetError("that branch already exists; pick a new branch name")
        add = ["worktree", "add", path, branch]
    else:
        if detach_at is None or gitops.SHA.fullmatch(detach_at) is None:
            raise FleetError("a detached worktree needs a full commit sha")
        add = ["worktree", "add", "--detach", path, detach_at]
    record = {"name": name, "task_id": task_id, "path": path, "repo_dir": repo_dir, "common_dir": common_dir,
              "git_dir": f"{common_dir}/worktrees/{name}", "branch": branch, "base": base_sha, "base_ref": base,
              "repo": slug, "links": toolchain.linkable(repo_dir)}
    claim["record"] = record  # from here the caller takes back what git makes
    if branch is not None:
        # git branch makes the branch only when it does not exist yet, so a branch it made is this command's. From a
        # commit sha it sets up no tracking, as git worktree add -b from the same sha did.
        gitops.git(["branch", branch, base_sha], common_dir)
        claim["made_branch"] = True
    gitops.git(add, common_dir)
    if not os.path.isdir(record["git_dir"]):
        raise FleetError("git named the worktree differently than expected; remove it by hand and retry")
    toolchain.link_deps(record, record["links"])
    return gitops.write_record(record)


def _branch_exists(common_dir: str, branch: str) -> bool:
    # A read git fails refuses, rather than passing for a branch that is not there.
    out = gitops.git(["for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"], common_dir)
    return bool(out.strip())


def _branch_lock_name(common_dir: str, branch: str) -> str:
    """The lock file for making branch in the repo whose .git folder is common_dir. The repo is named by device and
    inode, as gitops.same_checkout does, so two spellings of one checkout share the lock."""
    st = os.lstat(common_dir)
    return f"branch-{hashlib.sha256(f'{st.st_dev}:{st.st_ino}:{branch}'.encode('ascii')).hexdigest()[:32]}.lock"


@contextlib.contextmanager
def branch_claim(repo_dir: str, branch: str) -> Iterator[dict]:
    """A claim for add_worktree that holds the lock on making branch in repo_dir until the with block ends. One
    command makes a given branch in a given repo at a time, whichever TASK.md it works under: the lock is taken
    without waiting before the check that the branch is new, and the caller keeps the block open until its task has
    the worktree or it has taken back what add_worktree made, so no other command finds the branch missing while
    this one may still remove it. A second command is refused at once and changes nothing."""
    repo_dir, branch = gitops.check_repo_dir(repo_dir), gitops.check_branch(branch)
    name = _branch_lock_name(f"{repo_dir}/.git", branch)
    with contextlib.ExitStack() as stack:
        locks_fd = stack.enter_context(safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True))
        try:
            stack.enter_context(safefs.held_lock(locks_fd, name, blocking=False))
        except safefs.Busy:
            raise FleetError(BRANCH_RUNNING) from None
        claim = {"branch_lock": name}
        try:
            yield claim
        finally:
            del claim["branch_lock"]  # the claim no longer holds the lock


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


def cause(exc: BaseException) -> str:
    """What stopped a command, for its refusal: the error's own text, or that a signal stopped it."""
    return str(exc) if isinstance(exc, Exception) else "it was stopped by a signal"


def _take_back(record: dict, made_branch: bool, exc: BaseException) -> None:
    """Undo add_worktree, however far it got, for a task that never got the worktree, so the task stays queued with
    nothing left on disk and the same command can run again. The new branch goes only when git made it for this
    command and it still sits at the base commit add_worktree resolved before it made anything. A branch git was not
    seen to make is kept and named, since something else may have made it. If the undo fails, or the branch's tip
    cannot be read, the refusal says what is left to remove by hand."""
    branch, common_dir, base_sha = record["branch"], record["common_dir"], record["base"]
    path, branch_left = f"the worktree {record['path']}", f"branch {branch}"
    left = [path] + ([branch_left] if made_branch else []) + [f"the record {record['name']}.json"]
    try:
        toolchain.unlink_deps(record)
        if os.path.lexists(record["path"]):
            gitops.git(["worktree", "remove", record["path"]], common_dir)
        left.remove(path)
        if made_branch:
            tip = gitops.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], common_dir, check=False)
            if tip.strip() == base_sha:
                gitops.git(["update-ref", "-d", f"refs/heads/{branch}", base_sha], common_dir)
            elif not tip.strip() and gitops.has_branch(common_dir, branch):
                raise FleetError(f"git could not read where branch {branch} points")
            left.remove(branch_left)
        with contextlib.suppress(FileNotFoundError, safefs.Missing):
            gitops.drop_record(record["name"])
        unseen = branch is not None and not made_branch and gitops.has_branch(common_dir, branch)
    except Exception as undo:
        raise FleetError(f"{cause(exc)}; taking back the new worktree also failed ({undo}), so remove"
                         f" {' and '.join(left)} by hand") from exc
    if unseen:
        raise FleetError(f"{cause(exc)}; branch {branch} is kept, since this command never saw git make it: remove"
                         " it by hand only if nothing else made it") from exc


def take_back(claim: dict, exc: BaseException) -> None:
    """Undo add_worktree for the caller that owns claim, so nothing is left on disk: the worktree, the branch git
    made for it (while it still sits at the base it was made at), the links and the record go, from the record in
    claim, so a partial worktree with no saved record goes too. An empty claim has nothing to take back. If the undo
    fails, or leaves a branch, the FleetError says so."""
    if "record" in claim:
        _take_back(claim["record"], claim.get("made_branch", False), exc)


def kept(conn, claim: dict) -> Optional[bool]:
    """Whether the store has committed the worktree in claim to its task. True: it is the task's, and nothing takes
    it back. False: the store says the task does not have it (nothing was made yet, no such task, or a task with no
    worktree), so it can be taken back. None: the store cannot say, since a transaction is still open or the read
    failed, so nothing may be taken back."""
    if "record" not in claim:
        return False
    if conn.in_transaction:
        return None
    try:
        task = pensieve.get_task(conn, claim["record"]["task_id"])
    except NotFoundError:
        return False
    except Exception:  # noqa: BLE001 - a store that cannot be read cannot say whose the worktree is
        return None
    return task["worktree"] is not None


def unsure(claim: dict, exc: BaseException, *more: str) -> FleetError:
    """The refusal when kept(claim) is None: nothing was taken back, since the worktree may be its task's, and it
    names everything left, more included, for Ryan to remove by hand if the task has no worktree. The cause is cut
    short so the list always fits in the line a go or a fleet command prints."""
    record = claim["record"]
    left = ([f"the worktree {record['path']}"] + ([f"branch {record['branch']}"] if claim.get("made_branch") else [])
            + [f"the record {record['name']}.json", *more])
    return FleetError(f"{common.one_line(cause(exc), 120)}; the store could not say whether task {record['task_id']}"
                      f" kept its worktree, so nothing was taken back: if castle task show {record['task_id']} lists"
                      f" none, remove {' and '.join(left)} by hand")


def settle(conn, claim: dict, exc: BaseException) -> None:
    """For a caller stopped by exc before it knew its task had the worktree in claim: take back what add_worktree
    made when the store says the task does not have it, keep it when the store says it does, and when the store
    cannot say, keep it all and raise the refusal that says so."""
    owned = kept(conn, claim)
    if owned is None:
        raise unsure(claim, exc) from exc
    if not owned:
        take_back(claim, exc)


def start_desk(conn, task: dict, lock_fd: int) -> str:
    """Start the desk's run on its request owl, when Ryan has enabled the desk. Returns what happened. Called under
    the task's review lock (lock_fd), which the run is handed, so no review of the task starts before the run holds
    it (see run_desk.task_lock). While the task's PR follow-up is starting or building, the run starts on that
    follow-up's own owl instead, with its threads file written again from the office copy first; while it is still
    being routed, nothing starts (fleet/followup.py, run_owl)."""
    from fleet import followup  # here, not at the top: the follow-up module builds on this one

    owl_id, open_followup = followup.run_owl(conn, task)
    if not run_desk.is_enabled(task["desk"]):
        return f"{task['desk']} is not enabled, so nothing was started"
    if run_desk.over_daily_cap(conn, task["desk"]) is not None:
        run_desk.report_cap(conn, task["desk"])
        return f"{task['desk']} reached its daily cap, so nothing was started"
    if open_followup is not None:
        followup.publish_threads(conn, task, open_followup)
    run_desk.spawn(task["desk"], owl_id, hold_fd=lock_fd)
    return f"started {task['desk']} on owl {owl_id}"


def start_locked(conn, task: dict) -> str:
    """start_desk under the task's review lock, which the run is handed, so no review of the task starts before the
    run holds it. While a review of the task holds the lock, nothing starts, and the result says so."""
    with contextlib.ExitStack() as held:
        try:
            lock_fd = held.enter_context(run_desk.task_lock(task["id"]))
        except safefs.Busy:
            return (f"a review of this task is running, so {task['desk']} was not started; fleet build {task['id']}"
                    " starts it once the review has ended")
        return start_desk(conn, task, lock_fd)


def create(conn, task_id: str, repo_dir: str, branch: str, base: str = config.DEFAULT_BASE,
           fetch: bool = True, start: bool = True, claim: Optional[dict] = None) -> dict:
    """Give a queued build task its worktree and start it, then start the desk's run. With start=False the run
    is left to the caller, which starts it with start_locked once its own store transaction has committed, so the
    run never reads a store that has not got its task yet (the go hook).

    Without claim, create holds the branch's lock itself and settles what it made when anything stops it before
    its store transaction commits. A caller that runs create inside its own transaction passes claim, from
    branch_claim for this repo and branch, and owns that from the start: claim["record"] is set before the first git
    change, and the caller keeps the claim's with block open until its own transaction commits or it has called
    settle or take_back, whether create refused or something after it did."""
    task, used = build_task(conn, pensieve.get_task(conn, ids.check("task", task_id)))
    if isinstance(repo_dir, str):
        gitops.check_unprotected(repo_dir)  # a build's checkout must be one the fleet's background jobs can read
    branch = gitops.check_branch(branch)
    holder = _holder(conn, task["id"])
    with holder_lock(holder):
        task = pensieve.get_task(conn, task["id"])  # read again under the lock
        if task["status"] != "queued" or task["worktree"] is not None:
            raise FleetError("the task must be queued and have no worktree yet")
        _check_spec(conn, holder, repo_dir, branch, base)
        _check_startable(conn, task)
        with (branch_claim(repo_dir, branch) if claim is None else contextlib.nullcontext(claim)) as made:
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
                if claim is None:
                    settle(conn, made, exc)
                raise
    started = start_locked(conn, task) if start else None
    made = {"task_id": task["id"], "worktree": record["path"], "branch": record["branch"], "base": record["base"],
            "repo": record["repo"], "desk": started}
    return made if used is None else {**made, "used": used}


def build_task(conn, task: dict) -> tuple:
    """(task, used): a build desk's task as it is, with used None. For any other task, its one open child on a build
    desk, with used saying so, so fleet worktree takes the parent id McGonagall's TASK.md carries. Zero or several
    open build children refuse, naming them."""
    if task["desk"] in config.WORKTREE_DESKS:
        return task, None
    children = [child for child in pensieve.list_tasks(conn, open_only=True)
                if child["parent_task_id"] == task["id"] and child["desk"] in config.WORKTREE_DESKS]
    if not children:
        raise FleetError(f"only a build desk's task gets a worktree from this script, and {task['id']} has no open"
                         " build task under it")
    if len(children) > 1:
        raise FleetError(f"only a build desk's task gets a worktree from this script, and {task['id']} has"
                         f" {len(children)} open build tasks under it ({', '.join(child['id'] for child in children)});"
                         " name the one to use")
    child = children[0]
    return child, f"used {child['desk'].capitalize()}'s task {child['id']} under {task['id']}"


def build(conn, task_id: str, lock_fd: int) -> dict:
    """Start the build desk again on its own task, for a fix round, under the task's review lock (lock_fd), which
    the run it starts is handed."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS or task["status"] != "active" or not task["worktree"]:
        raise FleetError("only an active build task with a worktree can be started again")
    return {"task_id": task["id"], "desk": start_desk(conn, task, lock_fd)}


MERGED_NAME = re.compile(r"(tk_[0-9a-f]{16})\.merged-([0-9a-f]{12})")


def merged_name(task_id: str, merge_sha: str) -> str:
    """The name of the closer's worktree for a task at a merge commit: <task-id>.merged-<first 12 of the sha>."""
    return f"{ids.check('task', task_id)}.merged-{ids.check('sha', merge_sha)[:12]}"


def remove_merged(build_record: dict, name: str) -> dict:
    """Take back the closer's merged worktree name of the task whose own worktree record is build_record, whether or
    not a killed try wrote its record: the record it expects is built from build_record and the name alone (path
    worktrees/<name>, the same repo, .git folder and links). The name must be exactly <task-id>.merged-<12 hex> for
    that task. When git lists the path as a worktree of that repo, git worktree remove --force takes it back (it is
    detached and holds no branch, and its checks may leave untracked files). A folder git does not list, or a path
    git lists whose folder is gone, is never deleted by path and git worktree prune is never run: that fails, for you
    to look at. Then the office record goes, when there is one. {name, removed}."""
    match = MERGED_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None or match.group(1) != build_record["task_id"]:
        raise FleetError("that is not a merged worktree of this task")
    common_dir = build_record["common_dir"]
    expected = {"name": name, "task_id": build_record["task_id"], "path": config.worktree_dir(name),
                "repo_dir": build_record["repo_dir"], "common_dir": common_dir,
                "git_dir": f"{common_dir}/worktrees/{name}", "links": list(build_record.get("links") or [])}
    path = expected["path"]
    toolchain.unlink_deps(expected)
    listed = bool({path, os.path.realpath(path)} & set(gitops.worktree_paths(common_dir)))
    exists = os.path.lexists(path)
    removed = False
    if listed and exists:
        gitops.git(["worktree", "remove", "--force", path], common_dir)
        removed = True
    elif listed:
        raise FleetError(f"git lists the merged worktree {name} but its folder is gone, so it was left for you")
    elif exists:
        raise FleetError(f"the merged worktree folder {name} is not one git lists, so it was left for you")
    with contextlib.suppress(FileNotFoundError, safefs.Missing):
        gitops.drop_record(name)
    return {"name": name, "removed": removed}


# Removing a closed task's worktree


MARKER_MAX_BYTES = 4096
MARKER = re.compile(r"(tk_[0-9a-f]{16})\.(closing|removing|removed|unreported)")
# Who started a removal: the closer (auto-close's switch), the sweep (the worktree-cleanup switch), you with fleet
# worktree-remove, or nobody, for a worktree that was already gone.
REMOVED_BY = ("closer", "sweep", "hand", "gone")
# One removal's own identity, from its removing marker on, so its markers and its report never stand for another.
REMOVAL = re.compile(r"[0-9a-f]{16}")
# One Map round's batch of removals: its time and a random tag, so two rounds never share one.
BATCH = re.compile(r"[0-9]{1,12}-[0-9a-f]{8}")
KEPT_KIND, REMOVED_KIND = "worktree.kept", "worktree.removed"
IGNORED_KEPT = "ignored files that exist only here"
FLAGGED_KEPT = "tracked files git is told not to check (assume-unchanged or skip-worktree)"
SWITCH_OFF_KEPT = "its removal was cut short and the switch that started it is off now"
IDS_SHOWN = 6


class Kept(FleetError):
    """A closed task's worktree left where it is, and why: whoever removes it automatically tells you once."""


class SwitchedOff(FleetError):
    """The switch that started an automatic removal was off at the last check before git worktree remove: nothing was
    removed, the worktree is whole, and a removal a kill cut short keeps its marker (resumed)."""

    def __init__(self, resumed: bool) -> None:
        super().__init__(SWITCH_OFF_KEPT if resumed else "the switch that started its removal was off by the time git"
                         " would have removed it")
        self.resumed = resumed


def cleanup_on() -> bool:
    """Whether you switched the worktree cleanup on: the office file config.WORKTREE_CLEANUP_FILE holds exactly "on"."""
    return common.opt_in_on(config.WORKTREE_CLEANUP_FILE)


def _marker(task_id: str, kind: str) -> str:
    return f"{ids.check('task', task_id)}.{kind}"


def _is(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_batch(value: object) -> bool:
    return value is None or _is(BATCH, value)


def read_marker(task_id: str, kind: str) -> Optional[dict]:
    """A removal marker in the office worktrees folder, or None when there is none. One that cannot be read whole
    raises, so no failed read is ever taken for no marker. closing is the closer's intent, written before its close
    commits; removing, removed and unreported carry the removal's own identity."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, gitops.RECORD_DIR) as fd:
            raw = safefs.read_regular(fd, _marker(task_id, kind), MARKER_MAX_BYTES, "worktree removal marker")
    except safefs.Missing:
        return None
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("a worktree removal marker is not strict JSON") from None
    if not isinstance(data, dict) or data.get("task_id") != task_id or data.get("by") not in REMOVED_BY \
            or not isinstance(data.get("path"), str):
        raise FleetError("a worktree removal marker is malformed")
    if kind == "closing":
        good = data["by"] == "closer" and all(_is(gitops.SHA, data.get(key))
                                              for key in ("pass_sha", "merge_sha", "tip"))
    else:
        good = _is(REMOVAL, data.get("removal")) and {
            "removing": lambda: _is(gitops.SHA, data.get("head")),
            "removed": lambda: _is_batch(data.get("reported")),
            "unreported": lambda: _is_batch(data.get("batch")),
        }[kind]()
    if not good:
        raise FleetError("a worktree removal marker is malformed")
    return data


def _write_marker(task_id: str, kind: str, data: dict) -> None:
    raw = (json.dumps({"task_id": task_id, **data}, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    with safefs.opened_dir(config.OFFICE_ROOT, gitops.RECORD_DIR, create=True) as fd:
        safefs.write_new(fd, _marker(task_id, kind), raw)


def _drop_marker(task_id: str, kind: str) -> None:
    with contextlib.suppress(FileNotFoundError, safefs.Missing), \
            safefs.opened_dir(config.OFFICE_ROOT, gitops.RECORD_DIR) as fd:
        os.unlink(_marker(task_id, kind), dir_fd=fd)


def markers(kind: str) -> list:
    """The task ids with a removal marker of this kind, sorted. A folder that cannot be listed raises."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, gitops.RECORD_DIR) as fd:
            names = os.listdir(fd)
    except safefs.Missing:
        return []
    return sorted(match.group(1) for match in map(MARKER.fullmatch, names)
                  if match is not None and match.group(2) == kind)


def _listed(record: dict) -> bool:
    """Whether git lists the worktree for its repo. A read git fails raises, never "not listed"."""
    path = record["path"]
    return bool({path, os.path.realpath(path)} & set(gitops.worktree_paths(record["common_dir"])))


def _present(record: dict) -> bool:
    """Whether the worktree folder is there, as a plain folder of yours in the castle worktrees folder, with no link
    on the way. A folder that is there in any other shape keeps the worktree."""
    try:
        with safefs.opened_dir(config.CASTLE_ROOT, "worktrees", record["name"]):
            return True
    except safefs.Missing:
        return False
    except safefs.Unsafe:
        raise Kept("its folder is not a plain folder of yours in the worktrees folder") from None


def _ignored_here(record: dict) -> bool:
    """Whether the worktree holds git-ignored content, which no commit holds and git worktree remove deletes. The only
    exception is each dependency link the toolchain made for it: a name the office record lists that is still exactly
    the link to the main checkout's copy (gitops.borrowed_link), never a name alone."""
    links = set(record.get("links") or [])
    return any(name not in links or not gitops.borrowed_link(record, name) for name in gitops.ignored(record))


def _check_head(record: dict, head_ok, resumed: Optional[str] = None, automatic: bool = True) -> str:
    """The worktree's HEAD once nothing in it would be lost: no uncommitted changes, for an automatic removal no
    ignored files and no tracked file marked assume-unchanged or skip-worktree either, and HEAD passes head_ok (None
    skips it, for your own command), or is the HEAD a removal a kill cut short had checked already. Kept otherwise."""
    cut = " (a removal was cut short part way: finish it with git worktree remove --force once nothing in it is" \
          " yours)" if resumed is not None else ""
    if gitops.dirty(record):
        raise Kept("uncommitted changes" + cut)
    if automatic and _ignored_here(record):
        raise Kept(IGNORED_KEPT + cut)
    if automatic and gitops.flagged(record):
        raise Kept(FLAGGED_KEPT + cut)
    head = gitops.rev(record)
    if head_ok is None or head == resumed:
        return head
    found = head_ok(record, head)
    if found is None:
        raise Kept("its commits could not be confirmed on origin (a fetch or a read failed, or its branch is gone"
                   " from origin)")
    if not found:
        raise Kept("its HEAD has commits that are neither pushed nor merged")
    return head


def why_kept(record: dict, head_ok) -> Optional[str]:
    """Why remove_closed would keep this worktree now, or None when it would remove it: the closer asks before its
    close, so the close event can name a worktree it keeps."""
    try:
        if not _listed(record):
            return "git does not list it as a worktree"
        if not _present(record):
            return "git lists it but its folder is gone"
        _check_head(record, head_ok)
    except (FleetError, OSError) as exc:
        return str(exc) if isinstance(exc, Kept) else f"it could not be read ({common.scrubbed_line(exc, 160)})"
    return None


def _settle_done(task_id: str, done: dict) -> None:
    """A removal whose removed marker is written: what a kill or a failed write left after it is finished. The
    sweep's removal gets its unreported marker unless its removed marker says it was told already, and the removing
    marker goes, so no leftover marker ever reads as a removal still under way."""
    if done["by"] == "sweep" and done.get("reported") is None and read_marker(task_id, "unreported") is None:
        _write_marker(task_id, "unreported", {"by": "sweep", "path": done["path"], "removal": done["removal"],
                                              "batch": None})
    _drop_marker(task_id, "removing")


def _finished(record: dict, by: str, removal: str) -> None:
    """A removal done: the removed marker first, with the removal's identity, then the sweep's unreported marker for
    its batched row, then the removing marker goes. A kill or a failed write at any point leaves the markers a later
    call finishes from (_settle_done), and none of them is ever written for a removal that was told already."""
    done = {"by": by, "path": record["path"], "removal": removal, "reported": None}
    _write_marker(record["task_id"], "removed", done)
    _settle_done(record["task_id"], done)


def _relink(record: dict) -> None:
    with contextlib.suppress(FleetError, OSError):
        toolchain.link_deps(record, [name for name in record.get("links") or []
                                     if not os.path.lexists(f"{record['path']}/{name}")])


def remove_closed(conn, task_id: str, by: str, head_ok=None) -> dict:
    """Remove a closed task's worktree, the one path every removal takes. The caller holds the task's review lock,
    which every run and review of the task holds too, so a worktree in use is never removed.

    The task must be closed, its record its own, its stored worktree in the castle worktrees folder. A worktree git
    lists, whose folder is there, goes only with no uncommitted changes and a HEAD that passes head_ok(record, head):
    True to remove, False or None (unknown) to keep. The closer and the sweep also keep one with git-ignored files,
    the toolchain's own dependency links aside, or with a tracked file marked assume-unchanged or skip-worktree, whose
    changes git status hides, since git worktree remove would delete them; head_ok None is your own command, which
    keeps git's own rule. Right before git worktree remove, the switch that started an automatic removal is read
    again: off, nothing is removed, a removal a kill cut short keeps its marker, and SwitchedOff says so. A removing
    marker, with the removal's own identity, is written before anything changes, and git worktree remove runs without
    --force, so git checks once more. A marker left by a kill
    finishes the removal on a later call: a folder git no longer lists is done, and an entry git lists whose folder is
    gone is removed by git worktree remove for that path alone. Without such a marker, either is left for you, and
    nothing is ever deleted by path. Kept, or a FleetError for a failed read, says what stays. {task_id, removed,
    branch_kept, state}: removed, gone (it was gone already) or already, which first settles any marker a cut short
    finish left."""
    if by not in REMOVED_BY[:3]:
        raise FleetError("a worktree removal must say who started it")
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["status"] != "closed":
        raise FleetError("only a closed task's worktree can be removed")
    record = gitops.find_record(castle_path(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree record")
    if record["name"] != task["id"] or record["task_id"] != task["id"]:
        raise FleetError("this task's worktree record is not the task's own worktree")
    path, common_dir = record["path"], record["common_dir"]
    result = {"task_id": task["id"], "removed": path, "branch_kept": record["branch"]}
    done = read_marker(task["id"], "removed")
    if done is not None:
        _settle_done(task["id"], done)
        return {**result, "state": "already"}
    removing = read_marker(task["id"], "removing")
    removal = secrets.token_hex(8) if removing is None else removing["removal"]
    listed, present = _listed(record), _present(record)
    if not listed:
        if present:
            raise Kept("its folder is not a worktree git lists, so it was left for you")
        _finished(record, removing["by"] if removing else "gone", removal)
        return {**result, "state": "removed" if removing else "gone"}
    if not present:
        if removing is None:
            raise Kept("git lists it but its folder is gone; nothing removes that entry but you")
        _still_on(by, resumed=True)
        gitops.git(["worktree", "remove", path], common_dir)  # the folder is gone: git drops only this path's entry
        if _listed(record):
            raise Kept("git still lists it after its removal was finished")
        _finished(record, removing["by"], removal)
        return {**result, "state": "removed"}
    head = _check_head(record, head_ok, resumed=None if removing is None else removing["head"],
                       automatic=by != "hand")
    started_by = by if removing is None else removing["by"]
    if removing is None:
        _write_marker(task["id"], "removing", {"by": by, "path": path, "head": head, "removal": removal})
    toolchain.unlink_deps(record)
    try:
        _still_on(by, resumed=removing is not None)
    except SwitchedOff:
        _relink(record)
        if removing is None:
            _drop_marker(task["id"], "removing")  # nothing started: the next round with the switch on starts again
        raise
    try:
        gitops.git(["worktree", "remove", path], common_dir)
    except FleetError as exc:
        # git refuses before it removes anything: a worktree it left whole gets its links back and no marker.
        try:
            whole = _listed(record) and _present(record) and not gitops.dirty(record)
        except (FleetError, OSError):
            whole = False
        if whole:
            _relink(record)
            _drop_marker(task["id"], "removing")
            raise Kept(f"git would not remove it ({common.scrubbed_line(exc, 160)})") from None
        raise Kept(f"its removal stopped part way ({common.scrubbed_line(exc, 160)}); finish it with git worktree"
                   " remove --force once nothing in it is yours") from None
    if _listed(record) or _present(record):
        raise Kept("git said it removed the worktree, but it is still there")
    _finished(record, started_by, removal)
    return {**result, "state": "removed"}


def _still_on(by: str, resumed: bool) -> None:
    """The last check before git worktree remove, under the task's review lock: the closer's and the sweep's removals
    need their own switch on still. Your own command needs none."""
    if by != "hand" and not _switch_for(by):
        raise SwitchedOff(resumed)


def remove(conn, task_id: str) -> dict:
    """fleet worktree-remove: remove a closed task's worktree, under its review lock. Git refuses if it has
    uncommitted changes. The branch stays. A removal a kill cut short is finished."""
    task_id = ids.check("task", task_id)
    try:
        with run_desk.task_lock(task_id):
            done = remove_closed(conn, task_id, "hand")
    except safefs.Busy:
        raise FleetError("a run or review of this task holds its lock; run it again once it has ended") from None
    if done["state"] == "already":
        raise FleetError("this task's worktree was removed already")
    return {"task_id": done["task_id"], "removed": done["removed"], "branch_kept": done["branch_kept"],
            "state": done["state"]}


def closer_head_ok(pass_sha: str, tip: str):
    """head_ok for the closer: HEAD is the commit its close proved, or one on the base tip it fetched."""
    def check(record: dict, head: str) -> Optional[bool]:
        return True if head == pass_sha else gitops.is_ancestor(record["common_dir"], head, tip)
    return check


def intend_removal(record: dict, pass_sha: str, merge_sha: str, tip: str) -> None:
    """The closer's intent to remove a build's worktree once its close commits, written before the close transaction,
    under the task's review lock. It names the close it waits for, so a later round finishes the removal when that
    close committed and drops the intent when it did not (sweep_closed), whatever kills the closer in between."""
    _write_marker(record["task_id"], "closing", {"by": "closer", "path": record["path"], "pass_sha": pass_sha,
                                                 "merge_sha": merge_sha, "tip": tip})


def drop_intent(task_id: str) -> None:
    _drop_marker(task_id, "closing")


def _close_committed(conn, task_id: str, intent: dict) -> bool:
    """Whether the proven close the intent names committed. A store that cannot be read raises."""
    closure = pensieve.task_closure(conn, task_id)
    return closure is not None and closure["kind"] == "proven" and closure["pass_sha"] == intent["pass_sha"] \
        and closure["merge_sha"] == intent["merge_sha"]


def kept_summary(task_id: str, path: str, why: object) -> str:
    return common.scrubbed_line(f"kept worktree {path} of closed task {task_id}: {why}. Its branch is untouched;"
                                f" once nothing in it is needed, fleet worktree-remove {task_id} removes it", 480)


def tell_kept(conn, task: dict, path: str, why: object, now: Optional[int] = None) -> None:
    """One headmaster event per worktree, whatever kept it and however often."""
    pensieve.add_event(conn, task["desk"], KEPT_KIND, "headmaster", kept_summary(task["id"], path, why),
                       task_id=task["id"], dedupe_key=f"worktree:kept:{task['id']}", now=now)


def _on_origin(fetched: dict):
    """head_ok for the sweep: HEAD is on the base or the task's branch on origin, each fetched once per round."""
    def check(record: dict, head: str) -> Optional[bool]:
        branches = []
        base_ref = record.get("base_ref")
        if isinstance(base_ref, str) and base_ref.startswith("origin/"):
            branches.append(base_ref[len("origin/"):])
        if record.get("branch"):
            branches.append(record["branch"])
        unknown = not branches
        for branch in branches:
            key = (record["common_dir"], branch)
            if key not in fetched:
                try:
                    fetched[key] = gitops.fetch_branch(record["common_dir"], branch)
                except (FleetError, OSError):
                    fetched[key] = None
            if fetched[key] is None:
                unknown = True
                continue
            held = gitops.is_ancestor(record["common_dir"], head, fetched[key])
            if held:
                return True
            unknown = unknown or held is None
        return None if unknown else False
    return check


def _stored_path(task: dict) -> str:
    try:
        return castle_path(task["worktree"]) or "-"
    except FleetError:
        return "-"


def _sweep_one(conn, task: dict, by: str, fetched: dict, counts: dict, now: Optional[int]) -> None:
    try:
        with run_desk.task_lock(task["id"]):
            done = remove_closed(conn, task["id"], by, _on_origin(fetched))
    except safefs.Busy:
        counts["busy"] += 1  # a run or review holds the task: a later round looks again
        return
    except SwitchedOff as exc:
        if exc.resumed:  # its marker stays, and finishes once the switch is on again
            counts["kept"] += 1
            tell_kept(conn, task, _stored_path(task), exc, now)
        return  # a removal that never started starts again on a round with the switch on
    except (FleetError, OSError, StoreError) as exc:
        counts["kept"] += 1
        tell_kept(conn, task, _stored_path(task), exc if isinstance(exc, Kept) else
                  f"it could not be read ({common.scrubbed_line(exc, 160)})", now)
        return
    if done["state"] == "removed":
        counts["removed"] += 1


def _switch_for(by: str) -> bool:
    if by == "sweep":
        return cleanup_on()
    if by == "closer":
        return common.opt_in_on(config.AUTO_CLOSE_FILE)
    return False


def _count(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


def _report(conn, now: Optional[int]) -> int:
    """One routine row per round for the sweep's removals: each unreported marker joins this round's batch, each
    batch is one event keyed by its round, then each removed marker records the batch that told it and the markers go.
    A kill before the event leaves the batch for the next round, a kill after it finds the event there already, and
    an unreported marker for a removal its removed marker says was told in another batch goes untold, so no removal is
    left out or told twice."""
    batches: dict = {}
    this_round = f"{common.now_stamp(now)}-{secrets.token_hex(4)}"
    for task_id in markers("unreported"):
        marker = read_marker(task_id, "unreported")
        if marker is None:
            continue
        done = read_marker(task_id, "removed")
        if done is not None and done["removal"] == marker["removal"] and done["reported"] is not None \
                and done["reported"] != marker["batch"]:
            _drop_marker(task_id, "unreported")  # told already, in its own batch
            continue
        if marker["batch"] is None:
            marker = {**marker, "batch": this_round}
            _write_marker(task_id, "unreported", {key: value for key, value in marker.items() if key != "task_id"})
        batches.setdefault(marker["batch"], []).append((task_id, marker["removal"]))
    for batch, entries in sorted(batches.items()):
        task_ids = [task_id for task_id, _ in entries]
        shown = task_ids[:IDS_SHOWN]
        more = f" and {len(task_ids) - len(shown)} more" if len(task_ids) > len(shown) else ""
        pensieve.add_event(conn, config.PATROL_SENDER, REMOVED_KIND, "routine",
                           f"removed {_count(len(task_ids), 'worktree')} of closed tasks: {', '.join(shown)}{more};"
                           " their branches are kept", dedupe_key=f"worktree:removed:{batch}", now=now)
        for task_id, removal in entries:
            done = read_marker(task_id, "removed")
            if done is not None and done["removal"] == removal and done["reported"] != batch:
                _write_marker(task_id, "removed", {**{key: value for key, value in done.items() if key != "task_id"},
                                                   "reported": batch})
            _drop_marker(task_id, "unreported")
    return sum(len(entries) for entries in batches.values())


def _resume(conn, task_id: str, fetched: dict, counts: dict, now: Optional[int]) -> None:
    """A removal a kill cut short: one whose removed marker is written is only settled, with no word, and the rest is
    finished while the switch that started it is still on, or told once."""
    try:
        task = pensieve.get_task(conn, task_id)
    except NotFoundError:
        return  # a marker no task of the store owns: nothing here removes anything for it
    try:
        done = read_marker(task_id, "removed")
        if done is not None:
            _settle_done(task_id, done)
            return
        marker = read_marker(task_id, "removing")
    except (FleetError, OSError) as exc:
        counts["kept"] += 1
        tell_kept(conn, task, _stored_path(task), f"its removal marker could not be read ({exc})", now)
        return
    if marker is None:
        return  # finished since it was listed
    if not _switch_for(marker["by"]):
        counts["kept"] += 1
        why = (f"its removal was cut short; fleet worktree-remove {task_id} finishes it" if marker["by"] == "hand"
               else SWITCH_OFF_KEPT)
        tell_kept(conn, task, _stored_path(task), why, now)
        return
    _sweep_one(conn, task, marker["by"], fetched, counts, now)


def _reconcile_intent(conn, task_id: str, counts: dict, now: Optional[int]) -> None:
    """The closer's intent, against the close it names, under the task's review lock, so no close in flight is read
    half way: a task still open, or a close that never committed, drops it; a committed close whose worktree is removed
    already drops it; one with a removal under way is left to that removal's own marker; otherwise its removal is
    finished while auto-close is on, through remove_closed with the closer's own checks, and told once when it is kept
    or auto-close is off. A failed read keeps the intent for a later round."""
    try:
        task = pensieve.get_task(conn, task_id)
    except NotFoundError:
        return
    try:
        with run_desk.task_lock(task_id):
            # The close and its closure row commit in one transaction, so a task still open never had that close.
            if pensieve.get_task(conn, task_id)["status"] != "closed":
                drop_intent(task_id)
                return
            intent = read_marker(task_id, "closing")
            if intent is None:
                return
            if not _close_committed(conn, task_id, intent):
                drop_intent(task_id)
                return
            done = read_marker(task_id, "removed")
            if done is not None:
                _settle_done(task_id, done)
                drop_intent(task_id)
                return
            if read_marker(task_id, "removing") is not None:
                return  # _resume finishes it or tells you
            if not _switch_for("closer"):
                counts["kept"] += 1
                tell_kept(conn, task, _stored_path(task), SWITCH_OFF_KEPT, now)
                return
            try:
                done = remove_closed(conn, task_id, "closer", closer_head_ok(intent["pass_sha"], intent["tip"]))
            except Kept as exc:
                counts["kept"] += 1
                tell_kept(conn, task, _stored_path(task), exc, now)
                drop_intent(task_id)
                return
            except SwitchedOff:
                counts["kept"] += 1  # auto-close went off at the last check: the intent stays for when it is on
                tell_kept(conn, task, _stored_path(task), SWITCH_OFF_KEPT, now)
                return
            drop_intent(task_id)
    except safefs.Busy:
        counts["busy"] += 1  # the closer or a run holds the task: a later round looks again
        return
    except (FleetError, OSError, StoreError) as exc:
        counts["kept"] += 1
        tell_kept(conn, task, _stored_path(task), f"it could not be read ({common.scrubbed_line(exc, 160)})", now)
        return
    if done["state"] == "removed":
        counts["removed"] += 1


def sweep_closed(conn, now: Optional[int] = None) -> dict:
    """The Map round's worktree cleanup. Never raises. Whatever the worktree-cleanup switch says, it first finishes
    each removal a kill cut short while the switch that started it is still on, and tells you once of each it cannot,
    then settles each of the closer's removal intents against the close it names. Then, while the worktree-cleanup
    switch is on (read again before each removal), it removes the worktree of each build task closed at least
    WORKTREE_CLEANUP_AFTER_SECONDS ago, through remove_closed under the task's review lock, with HEAD on origin's base
    or branch after a fetch. Last, the round's removals are one routine row. {state, removed, kept, busy, reported}."""
    counts = {"state": "on" if cleanup_on() else "off", "removed": 0, "kept": 0, "busy": 0, "reported": 0}
    try:
        fetched: dict = {}
        for task_id in markers("removing"):
            _resume(conn, task_id, fetched, counts, now)
        for task_id in markers("closing"):
            _reconcile_intent(conn, task_id, counts, now)
        if counts["state"] == "on":
            cutoff = common.now_stamp(now) - config.WORKTREE_CLEANUP_AFTER_SECONDS
            pending = set(markers("removing"))
            done = set(markers("removed"))
            for task in pensieve.list_tasks(conn, status="closed"):
                if task["status"] != "closed" or task["desk"] not in config.WORKTREE_DESKS or not task["worktree"] \
                        or not isinstance(task["closed_at"], int) or task["closed_at"] > cutoff \
                        or task["id"] in done or task["id"] in pending:
                    continue
                if not cleanup_on():
                    counts["state"] = "off"
                    break
                _sweep_one(conn, task, "sweep", fetched, counts, now)
        counts["reported"] = _report(conn, now)
        return counts
    except Exception as exc:  # noqa: BLE001 - the Map's round must never fail on the cleanup
        day = time.strftime("%Y-%m-%d", time.localtime(common.now_stamp(now)))
        with contextlib.suppress(Exception):
            pensieve.add_event(conn, config.PATROL_SENDER, "worktree.cleanup-failed", "headmaster",
                               common.scrubbed_line(f"the worktree cleanup could not finish its round: {exc}", 480),
                               dedupe_key=f"worktree:cleanup-failed:{day}", now=now)
        return {**counts, "state": "failed"}
