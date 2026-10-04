"""Git for the fleet scripts, run outside every desk sandbox without trusting what a desk can write.

A desk can rewrite any file in its worktree, including the worktree's own .git pointer file and
hook folders such as .husky. So every git call here:
- takes --git-dir and --work-tree from the office record for that worktree, never from the worktree;
- runs with core.hooksPath=/dev/null and core.fsmonitor=false, so no hook or monitor command runs;
- uses the absolute git binary and a fixed environment. HOME is kept so Ryan's own ~/.gitconfig
  (name, email, credential helper) still applies. No desk can write that file.

Office records live in ~/.hogwarts/worktrees/<name>.json, one per castle worktree, written only by
the worktree and review scripts. They name the main checkout, its .git folder, the branch and base.

This module and run_desk are the only fleet modules that start git or a desk.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from typing import Optional

from hogwarts import ids, pensieve
from hogwarts.errors import ValidationError

from fleet import common, config, safefs
from fleet.safefs import FleetError

RECORD_DIR = "worktrees"
RECORD_MAX_BYTES = 8192
SAFE_PATH = re.compile(r"/[A-Za-z0-9._@+/-]{1,400}")
BRANCH = re.compile(r"[a-z0-9][a-z0-9._/-]{0,99}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GITHUB_URL = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?"
)
SHA = re.compile(r"[0-9a-f]{40}")
WORD_SPLIT = re.compile(r"[^a-z0-9]+")
HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "core.editor=true",
    "-c", "protocol.ext.allow=never",
    "-c", "core.quotePath=true",  # unusual path names come back escaped, one per line
)
OUTPUT_MAX_CHARS = 200_000
CLEAN_MAX_PATHS = 200
CLEAN_MAX_CHARS = 50_000


def check_safe_path(path: object, label: str) -> str:
    """An absolute, normalised path with only plain characters, so it is safe in TOML and argv."""
    if not isinstance(path, str) or SAFE_PATH.fullmatch(path) is None:
        raise FleetError(f"{label} must be an absolute path with plain characters only")
    parts = path.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise FleetError(f"{label} must be a normalised absolute path")
    return path


def check_repo_dir(path: object) -> str:
    """A main checkout in Ryan's home: a real folder with a real .git folder, outside the fleet."""
    path = check_safe_path(path, "repo folder")
    if not path.startswith(config.USER_HOME_DIR + "/"):
        raise FleetError("the repo folder must be inside your home folder")
    for root in (config.OFFICE_ROOT, config.CASTLE_ROOT):
        if path == root or path.startswith(root + "/"):
            raise FleetError("the repo folder must be outside the office and the castle")
    if os.path.realpath(path) != path:
        raise FleetError("the repo folder must not go through a symlink")
    if not os.path.isdir(path) or os.path.islink(path + "/.git") or not os.path.isdir(path + "/.git"):
        raise FleetError("the repo folder must be a main checkout with its own .git folder")
    return path


def same_checkout(first: str, second: str) -> bool:
    """Whether two checkout paths name one real folder, by device and inode rather than by spelling, since a
    case-insensitive disk takes one checkout under paths that differ only in letter case."""
    try:
        one, two = os.lstat(first), os.lstat(second)
    except OSError:
        return False
    return stat.S_ISDIR(one.st_mode) and stat.S_ISDIR(two.st_mode) and os.path.samestat(one, two)


def check_branch(name: object) -> str:
    """A branch the fleet makes and pushes: lowercase, plain and free of fleet words, since teammates see it."""
    if not isinstance(name, str) or BRANCH.fullmatch(name) is None or ".." in name \
            or name.endswith((".lock", "/", ".")) or "//" in name or "/." in name:
        raise FleetError("branch names use lowercase letters, digits, dot, dash, underscore and slash")
    fleet_word = fleet_words_in(name)
    if fleet_word:
        raise FleetError(f"the branch name contains a fleet word ({fleet_word})")
    return name


def check_lineage_branch(git_dir: str, name: object) -> str:
    """A branch Ryan made, which an own-session review records as its lineage: any name git itself takes as a
    branch (git check-ref-format --branch, run here after the store's own rule), in 1 to 255 bytes of
    printable ASCII with no whitespace. Capitals and fleet words are fine, since the name is only compared,
    never pushed or shown to teammates. The refusal never repeats the name."""
    try:
        name = pensieve.check_review_branch(name)
    except ValidationError as exc:
        raise FleetError(str(exc)) from None
    try:
        out = git(["check-ref-format", "--branch", name], git_dir)
    except FleetError:
        raise FleetError("git does not take it as a branch name") from None
    if out != name + "\n":  # --branch expands a shorthand such as @{-1}, so the name must come back unchanged
        raise FleetError("git reads it as a shorthand for another branch, not as a branch name")
    return name


def current_branch(git_dir: str) -> Optional[str]:
    """The branch a checkout has out, unchecked, or None when its HEAD is detached."""
    ref = git(["symbolic-ref", "--quiet", "HEAD"], git_dir, check=False).strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") and len(ref) > len("refs/heads/") else None


def has_branch(git_dir: str, name: str) -> bool:
    """Whether the checkout has exactly this local branch."""
    out = git(["for-each-ref", "--format=%(refname)", f"refs/heads/{name}"], git_dir, check=False)
    return f"refs/heads/{name}" in out.splitlines()


def check_ref(name: object, label: str = "ref") -> str:
    if not isinstance(name, str) or REF.fullmatch(name) is None or ".." in name or name.endswith(".lock"):
        raise FleetError(f"{label} is not a plain git ref")
    return name


def fleet_words_in(text: str) -> Optional[str]:
    """The first fleet word in text, matched as a whole word, or None. Fleet names never leave the fleet."""
    for word in WORD_SPLIT.split(text.lower()):
        if word in config.FLEET_WORDS:
            return word
    return None


def repo_slug(url: str) -> str:
    """owner/repo for a GitHub remote URL, checked against the store's repo id rule."""
    match = GITHUB_URL.fullmatch(url.strip())
    if match is None:
        raise FleetError("the remote is not a plain GitHub URL")
    return ids.check("repo", f"{match.group(1)}/{match.group(2)}")


# Running git


def child_env() -> dict:
    return {
        "HOME": config.USER_HOME_DIR,
        "PATH": config.CHILD_PATH,
        "LANG": "en_US.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "RTK_DISABLED": "1",
    }


# Ancestry is read from the commits as written: replace refs and an info/grafts file can hide a parent link
# without changing any sha, and a commit-graph file can answer for a commit that is gone.
TRUE_HISTORY_FLAGS = ("--no-replace-objects", "-c", "core.commitGraph=false")
TRUE_HISTORY_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null/no-grafts"}


def git(args: list, git_dir: Optional[str], work_tree: Optional[str] = None, check: bool = True,
        timeout: Optional[int] = None, folder: Optional[str] = None) -> str:
    """Run one git command with the hardening flags. Returns stdout. Never uses a shell."""
    code, out, err = _run(args, git_dir, work_tree, timeout, folder)
    if check and code != 0:
        raise FleetError(f"git {args[0]} failed: {common.one_line(err, 300)}")
    return out


def _run(args: list, git_dir: Optional[str], work_tree: Optional[str], timeout: Optional[int],
         folder: Optional[str], true_history: bool = False) -> tuple:
    """(exit code, stdout, stderr) of one hardened git command. true_history reads commits as written."""
    if git_dir is not None:
        argv = [config.GIT_BIN, "--git-dir", git_dir]
        if work_tree is not None:
            argv += ["--work-tree", work_tree]
        cwd = work_tree or git_dir
    elif folder is not None:
        argv, cwd = [config.GIT_BIN, "-C", folder], folder
    else:
        raise FleetError("git needs a git folder or a working folder")
    argv += [*HARDENING, *(TRUE_HISTORY_FLAGS if true_history else ()), *args]
    env = {**child_env(), **(TRUE_HISTORY_ENV if true_history else {})}
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout or config.GIT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise FleetError(f"git {args[0]} timed out") from None
    return (done.returncode, done.stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
            done.stderr.decode("utf-8", "replace"))


def is_ancestor(git_dir: str, ancestor: str, sha: str) -> Optional[bool]:
    """Whether commit ancestor is sha or one of its ancestors, by the parents its commits really name (no
    replace refs or grafts), or None when this checkout cannot tell: it is shallow, so its history may stop
    short of ancestor, or git failed. A commit missing from the object store is no ancestor only when every
    commit sha builds on is there, since a clone cut short with its shallow file removed says it is full."""
    if SHA.fullmatch(ancestor) is None or SHA.fullmatch(sha) is None:
        raise FleetError("an ancestry check needs two full commit shas")
    shallow = git(["rev-parse", "--is-shallow-repository"], git_dir).strip()
    if shallow not in ("true", "false"):
        return None
    if _run(["cat-file", "-e", f"{ancestor}^{{commit}}"], git_dir, None, None, None, true_history=True)[0] != 0:
        if shallow == "true":
            return None
        walked = _run(["rev-list", "--count", sha], git_dir, None, None, None, true_history=True)[0]
        return False if walked == 0 else None
    code = _run(["merge-base", "--is-ancestor", ancestor, sha], git_dir, None, None, None, true_history=True)[0]
    if code == 0:
        return True
    return False if code == 1 and shallow == "false" else None


def is_shallow(git_dir: str) -> bool:
    """Whether this checkout says it is shallow. Only a shallow checkout can be cured with git fetch --unshallow;
    git refuses that on a full clone. False when git says it is full or cannot answer."""
    return git(["rev-parse", "--is-shallow-repository"], git_dir, check=False).strip() == "true"


def git_in(folder: str, args: list, check: bool = True, timeout: int = 10) -> str:
    """git in a folder Ryan's own session works in, found the normal way, with the hardening flags.

    Only the push gate uses this, for checks that must stay fast. Desk worktrees use git() instead.
    """
    return git(args, git_dir=None, work_tree=None, check=check, timeout=timeout, folder=folder)


def rev(record: dict, ref: str = "HEAD") -> str:
    sha = git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], record["git_dir"],
              record["path"]).strip()
    if SHA.fullmatch(sha) is None:
        raise FleetError("git did not return a full commit sha")
    return sha


def remote_slug(record: dict, remote: str = "origin") -> str:
    url = git(["config", "--get", f"remote.{remote}.url"], record["common_dir"]).strip()
    return repo_slug(url)


def link_excludes(record: dict) -> list:
    """Pathspecs that keep the worktree's read-only dependency links out of status and add."""
    return [f":(exclude){name}" for name in record.get("links") or []]


def borrowed_link(record: dict, name: str) -> bool:
    """Whether <worktree>/<name> is still the link to the main checkout's copy that the worktree script made."""
    path = f"{record['path']}/{name}"
    return os.path.islink(path) and os.readlink(path) == f"{record['repo_dir']}/{name}"


def check_links(record: dict) -> None:
    """Refuse a worktree whose dependency link was replaced. Status and add skip that path, so a replacement
    would let checks use files that no commit holds."""
    for name in record.get("links") or []:
        if os.path.lexists(f"{record['path']}/{name}") and not borrowed_link(record, name):
            raise FleetError(f"{name} in the worktree is no longer the read-only link to the main checkout's copy, "
                             "so checks could use files no commit holds; delete it from the worktree first")


def _clean(record: dict, mode: str) -> list:
    out = git(["clean", mode, "-d", "-X", "--", ".", *link_excludes(record)], record["git_dir"], record["path"])
    if len(out) >= OUTPUT_MAX_CHARS:
        raise FleetError("git clean listed more than the fleet can record")
    return out.splitlines()


def clean_ignored(record: dict) -> list:
    """Remove every git-ignored path in the worktree except the dependency links, and return each one as git
    names it (escaped when unusual).

    Status never shows ignored files, so a desk could leave one (a nested node_modules, a .env) that
    checks would use although no commit holds it. A dry run comes first: content git clean would skip,
    such as a nested git repository, or more paths than the evidence can list, is refused before anything
    is removed. Anything ignored that is still there afterwards is refused too.
    """
    planned = _clean(record, "-n")
    skipped = [line for line in planned if not line.startswith("Would remove ")]
    if skipped:
        raise FleetError("the worktree holds ignored content git clean would not remove, such as a nested git "
                         f"repository ({common.one_line(skipped[0], 200)}); remove it by hand, then verify again")
    names = [line[len("Would remove "):] for line in planned]
    if len(names) > CLEAN_MAX_PATHS or sum(len(name) for name in names) > CLEAN_MAX_CHARS:
        raise FleetError(f"the worktree holds {len(names)} git-ignored paths, more than the evidence can list; "
                         "remove them by hand, then verify again")
    removed = [line[len("Removing "):] for line in _clean(record, "-f") if line.startswith("Removing ")]
    left = git(["status", "--porcelain", "--ignored", "--untracked-files=all", "--", ".", *link_excludes(record)],
               record["git_dir"], record["path"]).strip()
    if removed != names or left:
        raise FleetError("git-ignored content is still in the worktree after the cleanup; remove it by hand, "
                         "then verify again")
    return removed


def dirty(record: dict) -> bool:
    """Whether the worktree has changes outside its dependency links. Refuses a replaced link."""
    check_links(record)
    return bool(git(["status", "--porcelain", "--untracked-files=all", "--", ".", *link_excludes(record)],
                    record["git_dir"], record["path"]).strip())


# Office records


def record_name(worktree_path: str) -> str:
    """The record name for a castle worktree path: its folder name under the worktrees root."""
    prefix = config.CASTLE_ROOT + "/worktrees/"
    if not worktree_path.startswith(prefix):
        raise FleetError("the worktree is not under the castle worktrees folder")
    return safefs.check_component(worktree_path[len(prefix):])


def _check_record(data: object, name: str) -> dict:
    if not isinstance(data, dict):
        raise FleetError("worktree record is not a JSON object")
    record = {
        "name": data.get("name"),
        "task_id": data.get("task_id"),
        "path": data.get("path"),
        "repo_dir": data.get("repo_dir"),
        "common_dir": data.get("common_dir"),
        "git_dir": data.get("git_dir"),
        "branch": data.get("branch"),
        "base": data.get("base"),
        "repo": data.get("repo"),
        "links": data.get("links") or [],
    }
    if record["name"] != name:
        raise FleetError("worktree record name does not match its file")
    ids.check("task", record["task_id"])
    if record["path"] != f"{config.CASTLE_ROOT}/worktrees/{name}":
        raise FleetError("worktree record path is not its castle worktree")
    for key in ("repo_dir", "common_dir", "git_dir"):
        check_safe_path(record[key], f"worktree record {key}")
    if record["common_dir"] != record["repo_dir"] + "/.git":
        raise FleetError("worktree record common_dir is not the repo's .git folder")
    if record["git_dir"] != f"{record['common_dir']}/worktrees/{name}":
        raise FleetError("worktree record git_dir is not the repo's entry for this worktree")
    if record["branch"] is not None:
        check_branch(record["branch"])
    check_ref(record["base"], "worktree record base")
    ids.check("repo", record["repo"])
    if not isinstance(record["links"], list) or any(name not in config.LINKABLE_DEPS for name in record["links"]):
        raise FleetError("worktree record links name a folder that is not a linkable dependency")
    return record


def read_record(name: str) -> dict:
    name = safefs.check_component(name)
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR) as fd:
        raw = safefs.read_regular(fd, f"{name}.json", RECORD_MAX_BYTES, "worktree record")
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("worktree record is not strict JSON") from None
    return _check_record(data, name)


def find_record(worktree_path: Optional[str]) -> Optional[dict]:
    """The record for a castle worktree, or None when there is no worktree or no record."""
    if not worktree_path:
        return None
    try:
        return read_record(record_name(worktree_path))
    except safefs.Missing:
        return None


def drop_record(name: str) -> None:
    """Remove the office record of a worktree that was taken back before any task held it."""
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR) as fd:
        os.unlink(f"{safefs.check_component(name)}.json", dir_fd=fd)


def write_record(record: dict) -> dict:
    record = _check_record(record, record["name"])
    data = (json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR, create=True) as fd:
        safefs.write_new(fd, f"{record['name']}.json", data)
    return record
