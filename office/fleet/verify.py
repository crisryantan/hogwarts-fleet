"""The verify script: run each acceptance check for one commit and record the evidence.

  fleet verify <task-id>

- Finds TASK.md up the task chain (the nearest task with one) and the task's worktree record.
- Reads lines shaped "AC-<n> <what must be true> | check: <check>". A check that is one backtick command
  and nothing else is a command. A check with no backticks is an observation for the reviewer to judge,
  recorded as not run. A check that holds backticks any other way, such as a backtick command plus other
  text, is malformed: it is not run, the evidence gives the plain reason, and SUMMARY counts it on its own.
- Refuses to run on a worktree with uncommitted changes, so the evidence belongs to one commit.
  Then removes every git-ignored path except the dependency links, and the evidence names each one,
  so no check can lean on a file the commit doesn't hold. Ignored content git clean would skip, such
  as a nested git repository, or more than the evidence can list, stops verify before anything is removed.
- Code from Ryan's own sessions is checked the way he would check it himself: plain bash, no Codex
  sandbox, but still a fixed environment with a throwaway home and temp folder. The fleet's own file
  layer opens every folder from / down, which a Codex sandbox refuses, so its suites can only pass there.
- Every other author's code runs with bash in the worktree under `codex sandbox` and a fleet permission profile:
  the worktree writable, the repo's .git readable, a throwaway home and temp folder writable in its
  own <user temp>/hogwarts-verify-<random> folder, xcrun's cache readable, /private/tmp and the office
  denied, no network, nothing else. codex sandbox runs no model and spends no tokens.
  Code a desk wrote therefore never runs with Ryan's own reach, even when he starts the check.
- Writes evidence.md next to TASK.md in the castle (with a per-commit copy), and the same text in
  the office reviews folder, where no desk can change it.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import time
from typing import Optional

from hogwarts import ids, pensieve

from fleet import common, config, gitops, run_desk, safefs, toolchain
from fleet.safefs import FleetError

AC_LINE = re.compile(r"AC-(\d{1,3})\s+(.+?)\s*\|\s*check:\s*(.+?)\s*")
COMMAND = re.compile(r"`([^`\x00-\x1f]{1,1000})`")
TASK_CHAIN_LIMIT = 16
TASK_MD_MAX_BYTES = 65536
PROFILE_NAME = "fleet-verify"
MALFORMED_HINT = ("Write the check as one backtick command and nothing else, which verify runs, or as plain words"
                  " with no backticks, which the reviewer judges")


def task_md(conn, task_id: str) -> tuple:
    """(id of the task that holds TASK.md, its castle path) found up the chain from task_id."""
    task = pensieve.get_task(conn, task_id)
    for _ in range(TASK_CHAIN_LIMIT):
        if task["intent_path"] is not None:
            return task["id"], config.CASTLE_ROOT + task["intent_path"][len(ids.CASTLE_ROOT):]
        if task["parent_task_id"] is None:
            break
        task = pensieve.get_task(conn, task["parent_task_id"])
    raise FleetError("no TASK.md was found up this task's chain")


def read_task_md(holder_id: str) -> bytes:
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", holder_id) as fd:
        return safefs.read_regular(fd, "TASK.md", TASK_MD_MAX_BYTES, "TASK.md")


def parse_checks(text: str) -> list:
    """Each acceptance criterion as {id, what, check, command, malformed}, in file order. command is set only
    for a check that is one backtick command and nothing else. malformed is the plain reason a check that holds
    backticks is not that, and None for a command or for an observation, which has no backticks at all."""
    checks = []
    for line in text.splitlines():
        match = AC_LINE.fullmatch(line.strip())
        if match is None:
            continue
        command = COMMAND.fullmatch(match.group(3))
        checks.append({"id": f"AC-{match.group(1)}", "what": match.group(2), "check": match.group(3),
                       "command": None if command is None else command.group(1),
                       "malformed": None if command is not None else malformed_reason(match.group(3))})
    return checks


def malformed_reason(check: str) -> Optional[str]:
    """Why a check that is not one backtick command still holds backticks, or None when it has none. What
    counts as a command stays COMMAND alone, so a malformed check is never run."""
    if "`" not in check:
        return None
    found = COMMAND.findall(check)
    if len(found) > 1:
        return f"it holds {len(found)} backtick commands, and a check runs only one"
    if found:
        return "it holds a backtick command plus other text"
    return ("its backticks hold no command verify can run (empty, unclosed, over 1000 characters or with"
            " control characters)")


def sandbox_argv(record: dict, scratch: str, command: str) -> list:
    entries = ['":minimal"="read"']
    entries += [f'"{gitops.check_safe_path(path, "a verify read path")}"="read"' for path in config.CODEX_EXTRA_READS]
    entries.append('":workspace_roots"={"."="write"}')
    entries.append(f'"{gitops.check_safe_path(record["common_dir"], "the repo .git folder")}"="read"')
    tools = toolchain.for_record(record)
    for path in tools["read"] + tools["path"]:
        entries.append(f'"{gitops.check_safe_path(path, "a toolchain folder")}"="read"')
    entries.append(f'"{gitops.check_safe_path(scratch, "the scratch folder")}"="write"')
    entries.append(f'"{gitops.check_safe_path(config.SHARED_TEMP_ROOT, "the shared temp folder")}"="deny"')
    cache = run_desk.xcrun_cache()
    if cache is not None:
        entries.append(f'"{cache}"="read"')
    entries.append(f'"{gitops.check_safe_path(config.OFFICE_ROOT, "the office")}"="deny"')
    table = "{filesystem={" + ", ".join(entries) + "}, network={enabled=false}}"
    return [config.CODEX_BIN, "sandbox", "-c", f"permissions.{PROFILE_NAME}={table}", "-P", PROFILE_NAME,
            "-C", record["path"], "--", config.BASH_BIN, "--noprofile", "--norc", "-c", command]


def child_env(scratch: str, record: Optional[dict] = None) -> dict:
    tools = toolchain.for_record(record, f"{scratch}/tmp")
    return {"HOME": f"{scratch}/home", "TMPDIR": f"{scratch}/tmp", "PATH": ":".join([*tools["path"], config.CHILD_PATH]),
            "LANG": "en_US.UTF-8", "CI": "1", "RTK_DISABLED": "1", **config.GIT_NO_LAZY_FETCH_ENV,
            **tools["env"]}


def _tail(path: str) -> tuple:
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        handle.seek(max(0, size - config.VERIFY_OUTPUT_MAX_BYTES))
        data = handle.read()
    lines = data.decode("utf-8", "replace").splitlines()[-config.EVIDENCE_EXCERPT_LINES:]
    return lines, size


def run_check(record: dict, scratch: str, command: str, sandboxed: bool = True) -> dict:
    out_path = f"{scratch}/out-{secrets.token_hex(4)}.log"
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    started = time.monotonic()
    try:
        argv = sandbox_argv(record, scratch, command) if sandboxed else [
            config.BASH_BIN, "--noprofile", "--norc", "-c", command]
        done = subprocess.run(argv, cwd=record["path"], env=child_env(scratch, record),
                              stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                              timeout=config.VERIFY_TIMEOUT_SECONDS, check=False)
        exit_code = done.returncode
    except subprocess.TimeoutExpired:
        exit_code = -1
    finally:
        os.close(fd)
    lines, size = _tail(out_path)
    return {"exit_code": exit_code, "seconds": round(time.monotonic() - started, 1), "lines": lines,
            "output_bytes": size}


def _make_scratch() -> str:
    """A private folder for one verify run, <user temp>/hogwarts-verify-<random>, with a home and a temp."""
    scratch = run_desk.fresh_temp(run_desk.desk_temp_dir(f"verify-{secrets.token_hex(8)}"))
    os.mkdir(f"{scratch}/home", 0o700)
    os.mkdir(f"{scratch}/tmp", 0o700)
    return scratch


def render(task_id: str, sha: str, record: dict, md_digest: str, checks: list, results: dict, now: int,
           sandboxed: bool = True, cleaned: tuple = ()) -> str:
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    ran = sum(1 for check in checks if check["command"] is not None)
    passed = sum(1 for check in checks if check["command"] is not None and results[check["id"]]["exit_code"] == 0)
    malformed = sum(1 for check in checks if check.get("malformed") is not None)
    lines = [
        f"EVIDENCE {task_id} @ {sha}",
        f"TASK.md sha256 {md_digest}",
        f"WORKTREE {record['path']}",
    ]
    if cleaned:
        lines.append(f"CLEANED {len(cleaned)} git-ignored paths before the checks, each as git names it:")
        lines += ["    " + name for name in cleaned]
    else:
        lines.append("CLEANED nothing: the worktree held no git-ignored files besides its dependency links")
    lines += [
        (f"RAN {when} under codex sandbox: worktree write, repo .git read, no network, no office" if sandboxed else
         f"RAN {when} without the Codex sandbox, because Ryan's own session wrote this code; throwaway HOME and TMPDIR"),
        (f"SUMMARY {passed} of {ran} commands exited 0, {malformed} malformed checks not run,"
         f" {len(checks) - ran - malformed} observations for the reviewer"),
        "",
    ]
    if not checks:
        lines.append("No acceptance criteria with a check were found in TASK.md.")
    for check in checks:
        lines += [f"{check['id']} {check['what']}", f"check: {check['check']}"]
        if check.get("malformed") is not None:
            lines += [f"not run: malformed, {check['malformed']}. {MALFORMED_HINT}", ""]
            continue
        if check["command"] is None:
            lines += ["not run: an observation for the reviewer to judge", ""]
            continue
        result = results[check["id"]]
        exit_text = "timed out" if result["exit_code"] == -1 else str(result["exit_code"])
        lines += [f"exit: {exit_text} | {result['seconds']}s | {result['output_bytes']} bytes of output",
                  f"output, last {config.EVIDENCE_EXCERPT_LINES} lines:"]
        lines += ["    " + line for line in result["lines"]] or ["    (no output)"]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _replace(dir_fd: int, name: str, data: bytes) -> None:
    temp = f".{name}.{secrets.token_hex(4)}.tmp"
    safefs.write_new(dir_fd, temp, data)
    safefs.move(dir_fd, temp, dir_fd, name)


def write_evidence(task_id: str, holder_id: str, sha: str, text: str) -> dict:
    data = text.encode("utf-8")
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", holder_id) as fd:
        _replace(fd, "evidence.md", data)
        _replace(fd, f"evidence-{sha[:12]}.md", data)
    with safefs.opened_dir(config.OFFICE_ROOT, "reviews", task_id, create=True) as fd:
        _replace(fd, f"evidence-{sha}.md", data)
    return {"castle": f"{config.CASTLE_ROOT}/tasks/{holder_id}/evidence.md",
            "office": f"{config.OFFICE_ROOT}/reviews/{task_id}/evidence-{sha}.md"}


def verify(conn, task_id: str, now: Optional[int] = None) -> dict:
    task = pensieve.get_task(conn, ids.check("task", task_id))
    record = gitops.find_record(_castle(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree with an office record")
    if gitops.dirty(record):
        raise FleetError("the worktree has uncommitted changes; evidence must belong to one commit")
    cleaned = gitops.clean_ignored(record)
    sha = gitops.rev(record)
    holder_id, _ = task_md(conn, task["id"])
    raw = read_task_md(holder_id)
    checks = parse_checks(raw.decode("utf-8", "replace"))
    results = {}
    sandboxed = task["desk"] != config.OWN_SESSION_DESK
    if any(check["command"] is not None for check in checks):
        scratch = _make_scratch()
        try:
            for check in checks:
                if check["command"] is not None:
                    results[check["id"]] = run_check(record, scratch, check["command"], sandboxed)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if gitops.rev(record) != sha:
        raise FleetError("HEAD moved while the checks ran; run verify again")
    text = render(task["id"], sha, record, hashlib.sha256(raw).hexdigest(), checks, results, common.now_stamp(now),
                  sandboxed, cleaned)
    paths = write_evidence(task["id"], holder_id, sha, text)
    failed = [check["id"] for check in checks if check["command"] is not None and results[check["id"]]["exit_code"] != 0]
    malformed = [check["id"] for check in checks if check["malformed"] is not None]
    return {"task_id": task["id"], "sha": sha, "checks": len(checks), "failed": failed, "malformed": malformed,
            "left_changes": gitops.dirty(record), "evidence": paths}


def _castle(store_path: Optional[str]) -> Optional[str]:
    if not store_path:
        return None
    if not store_path.startswith(ids.WORKTREES_ROOT + "/"):
        raise FleetError("the stored worktree is outside the castle worktrees folder")
    return config.CASTLE_ROOT + store_path[len(ids.CASTLE_ROOT):]
