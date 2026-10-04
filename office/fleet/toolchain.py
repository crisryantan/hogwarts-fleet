"""What a worktree's own tools need inside a Codex sandbox with no network.

A desk cannot install anything, so a worktree borrows what Ryan's main checkout and machine already
have, read-only:
- Node: the version the worktree's .nvmrc names, chosen from the versions installed under nvm. Its
  bin folder goes first on PATH, and its install folder becomes readable. A desk can change .nvmrc,
  but that only picks among installed versions.
- Dependencies: names in config.LINKABLE_DEPS (node_modules) that exist as real, git-ignored folders in the
  main checkout are linked into the worktree. The main checkout's folder is readable, never writable,
  so a desk can use the packages but cannot change them. Git is told to ignore the links.
- Go: a worktree with go.mod reads the module cache, gets GOPROXY=off so nothing is fetched, and keeps
  its build cache under the temp folder the desk may write.

Nothing here reads an environment variable. Paths come from config and the office worktree record.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from fleet import config, gitops
from fleet.safefs import FleetError

NVM_SUFFIX = ".nvm/versions/node"
GO_MOD_SUFFIX = "go/pkg/mod"
GO_BUILD_CACHE = "fleet-go-build"
VERSION_DIR = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
NVMRC = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
NVMRC_MAX_BYTES = 64


def _read_small(path: str) -> Optional[str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return os.read(fd, NVMRC_MAX_BYTES + 1)[:NVMRC_MAX_BYTES].decode("ascii", "replace")
    finally:
        os.close(fd)


def node_dir(worktree: str) -> Optional[str]:
    """The installed nvm Node folder that best matches the worktree's .nvmrc, or None."""
    wanted = _read_small(f"{worktree}/.nvmrc")
    match = None if wanted is None else NVMRC.fullmatch(wanted.strip())
    if match is None:
        return None
    root = f"{config.USER_HOME_DIR}/{NVM_SUFFIX}"
    try:
        names = os.listdir(root)
    except OSError:
        return None
    want = [int(part) for part in match.groups() if part is not None]
    found = []
    for name in names:
        version = VERSION_DIR.fullmatch(name)
        if version is None or os.path.islink(f"{root}/{name}"):
            continue
        numbers = [int(part) for part in version.groups()]
        if numbers[:len(want)] == want:
            found.append((numbers, name))
    if not found:
        return None
    return gitops.check_safe_path(f"{root}/{max(found)[1]}", "the Node folder")


def linkable(repo_dir: str) -> list:
    """Dependency folders in the main checkout that a new worktree should link to, read-only."""
    if not os.path.isfile(f"{repo_dir}/package.json"):
        return []
    names = []
    for name in config.LINKABLE_DEPS:
        path = f"{repo_dir}/{name}"
        if os.path.isdir(path) and not os.path.islink(path) and _ignored(repo_dir, name):
            names.append(name)
    return names


def _ignored(repo_dir: str, name: str) -> bool:
    out = gitops.git(["check-ignore", "--", f"{name}/"], f"{repo_dir}/.git", repo_dir, check=False)
    return out.strip() == f"{name}/"


def link_deps(record: dict, names: list) -> None:
    for name in names:
        target = f"{record['repo_dir']}/{name}"
        link = f"{record['path']}/{name}"
        if os.path.lexists(link):
            raise FleetError(f"{name} already exists in the worktree")
        os.symlink(target, link)


def unlink_deps(record: dict) -> None:
    """Drop the links link_deps made, so git sees no untracked files when the worktree is removed.

    A link counts only while it still points where link_deps aimed it. Anything else a desk left under
    that name stays, and git refuses the removal.
    """
    for name in record.get("links") or []:
        link = f"{record['path']}/{name}"
        if os.path.islink(link) and os.readlink(link) == f"{record['repo_dir']}/{name}":
            os.unlink(link)


def for_record(record: Optional[dict]) -> dict:
    """{"path": [...], "read": [...], "env": {...}} that the worktree's tools need, or an empty plan."""
    plan = {"path": [], "read": [], "env": {}}
    if record is None:
        return plan
    for name in record.get("links") or []:
        plan["read"].append(gitops.check_safe_path(f"{record['repo_dir']}/{name}", "a linked folder"))
    node = node_dir(record["path"])
    if node is not None:
        plan["path"].append(f"{node}/bin")
        plan["read"].append(node)
    if os.path.isfile(f"{record['path']}/go.mod"):
        cache = gitops.check_safe_path(f"{config.USER_HOME_DIR}/{GO_MOD_SUFFIX}", "the Go module cache")
        plan["read"].append(cache)
        plan["env"].update({"GOPROXY": "off", "GOFLAGS": "-mod=readonly", "GOMODCACHE": cache,
                            "GOPATH": f"{config.USER_HOME_DIR}/go",
                            "GOCACHE": f"{config.TMP_WRITE_ROOT}/{GO_BUILD_CACHE}"})
    return plan


def excludes(record: Optional[dict]) -> list:
    """Pathspecs that keep linked folders out of git status and git add."""
    if record is None:
        return []
    return [f":(exclude){name}" for name in (record.get("links") or [])]
