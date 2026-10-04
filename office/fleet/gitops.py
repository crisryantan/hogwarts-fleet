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
import subprocess
from typing import Optional

from hogwarts import ids

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
)
OUTPUT_MAX_CHARS = 200_000


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


def check_branch(name: object) -> str:
    if not isinstance(name, str) or BRANCH.fullmatch(name) is None or ".." in name \
            or name.endswith((".lock", "/", ".")) or "//" in name or "/." in name:
        raise FleetError("branch names use lowercase letters, digits, dot, dash, underscore and slash")
    fleet_word = fleet_words_in(name)
    if fleet_word:
        raise FleetError(f"the branch name contains a fleet word ({fleet_word})")
    return name


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


def git(args: list, git_dir: Optional[str], work_tree: Optional[str] = None, check: bool = True,
        timeout: Optional[int] = None, folder: Optional[str] = None) -> str:
    """Run one git command with the hardening flags. Returns stdout. Never uses a shell."""
    if git_dir is not None:
        argv = [config.GIT_BIN, "--git-dir", git_dir]
        if work_tree is not None:
            argv += ["--work-tree", work_tree]
        cwd = work_tree or git_dir
    elif folder is not None:
        argv, cwd = [config.GIT_BIN, "-C", folder], folder
    else:
        raise FleetError("git needs a git folder or a working folder")
    argv += [*HARDENING, *args]
    try:
        done = subprocess.run(argv, cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout or config.GIT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise FleetError(f"git {args[0]} timed out") from None
    out = done.stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS]
    if check and done.returncode != 0:
        err = common.one_line(done.stderr.decode("utf-8", "replace"), 300)
        raise FleetError(f"git {args[0]} failed: {err}")
    return out


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


def dirty(record: dict) -> bool:
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


def write_record(record: dict) -> dict:
    record = _check_record(record, record["name"])
    data = (json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR, create=True) as fd:
        safefs.write_new(fd, f"{record['name']}.json", data)
    return record
