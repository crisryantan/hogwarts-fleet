"""The verify script: run each acceptance check for one commit and record the evidence.

  fleet verify <task-id>

- Finds TASK.md up the task chain (the nearest task with one) and the task's worktree record.
- Reads lines shaped "AC-<n> <what must be true> | check: <check>". A check wrapped in backticks is
  a command. Anything else is an observation for the reviewer to judge, recorded as not run.
- Refuses to run on a worktree with uncommitted changes, so the evidence belongs to one commit.
- Runs each command with bash in the worktree under `codex sandbox` and a fleet permission profile:
  the worktree writable, the repo's .git readable, a throwaway home and temp folder writable,
  no network, the office denied, nothing else. codex sandbox runs no model and spends no tokens.
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
SCRATCH_ROOT = "/private/tmp"
PROFILE_NAME = "fleet-verify"


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
    """Each acceptance criterion as {id, what, check, command or None}, in file order."""
    checks = []
    for line in text.splitlines():
        match = AC_LINE.fullmatch(line.strip())
        if match is None:
            continue
        command = COMMAND.fullmatch(match.group(3))
        checks.append({"id": f"AC-{match.group(1)}", "what": match.group(2), "check": match.group(3),
                       "command": None if command is None else command.group(1)})
    return checks


def sandbox_argv(record: dict, scratch: str, command: str) -> list:
    entries = ['":minimal"="read"']
    entries += [f'"{gitops.check_safe_path(path, "a verify read path")}"="read"' for path in config.CODEX_EXTRA_READS]
    entries.append('":workspace_roots"={"."="write"}')
    entries.append(f'"{gitops.check_safe_path(record["common_dir"], "the repo .git folder")}"="read"')
    tools = toolchain.for_record(record)
    for path in tools["read"] + tools["path"]:
        entries.append(f'"{gitops.check_safe_path(path, "a toolchain folder")}"="read"')
    entries.append(f'"{gitops.check_safe_path(scratch, "the scratch folder")}"="write"')
    entries.append(f'"{gitops.check_safe_path(config.TMP_WRITE_ROOT, "the temp folder")}"="write"')
    temp = run_desk.user_temp_dir()
    if temp is not None:
        entries.append(f'"{temp}"="write"')
    entries.append(f'"{gitops.check_safe_path(config.OFFICE_ROOT, "the office")}"="deny"')
    table = "{filesystem={" + ", ".join(entries) + "}, network={enabled=false}}"
    return [config.CODEX_BIN, "sandbox", "-c", f"permissions.{PROFILE_NAME}={table}", "-P", PROFILE_NAME,
            "-C", record["path"], "--", config.BASH_BIN, "--noprofile", "--norc", "-c", command]


def child_env(scratch: str, record: Optional[dict] = None) -> dict:
    tools = toolchain.for_record(record)
    return {"HOME": f"{scratch}/home", "TMPDIR": f"{scratch}/tmp", "PATH": ":".join([*tools["path"], config.CHILD_PATH]),
            "LANG": "en_US.UTF-8", "CI": "1", "RTK_DISABLED": "1", **tools["env"]}


def _tail(path: str) -> tuple:
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        handle.seek(max(0, size - config.VERIFY_OUTPUT_MAX_BYTES))
        data = handle.read()
    lines = data.decode("utf-8", "replace").splitlines()[-config.EVIDENCE_EXCERPT_LINES:]
    return lines, size


def run_check(record: dict, scratch: str, command: str) -> dict:
    out_path = f"{scratch}/out-{secrets.token_hex(4)}.log"
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    started = time.monotonic()
    try:
        done = subprocess.run(sandbox_argv(record, scratch, command), cwd=record["path"], env=child_env(scratch, record),
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
    scratch = f"{SCRATCH_ROOT}/hogwarts-verify-{secrets.token_hex(8)}"
    os.mkdir(scratch, 0o700)
    os.mkdir(f"{scratch}/home", 0o700)
    os.mkdir(f"{scratch}/tmp", 0o700)
    return scratch


def render(task_id: str, sha: str, record: dict, md_digest: str, checks: list, results: dict, now: int) -> str:
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    ran = sum(1 for check in checks if check["command"] is not None)
    passed = sum(1 for check in checks if check["command"] is not None and results[check["id"]]["exit_code"] == 0)
    lines = [
        f"EVIDENCE {task_id} @ {sha}",
        f"TASK.md sha256 {md_digest}",
        f"WORKTREE {record['path']}",
        f"RAN {when} under codex sandbox: worktree write, repo .git read, no network, no office",
        f"SUMMARY {passed} of {ran} commands exited 0, {len(checks) - ran} observations for the reviewer",
        "",
    ]
    if not checks:
        lines.append("No acceptance criteria with a check were found in TASK.md.")
    for check in checks:
        lines += [f"{check['id']} {check['what']}", f"check: {check['check']}"]
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
    sha = gitops.rev(record)
    holder_id, _ = task_md(conn, task["id"])
    raw = read_task_md(holder_id)
    checks = parse_checks(raw.decode("utf-8", "replace"))
    results = {}
    if any(check["command"] is not None for check in checks):
        scratch = _make_scratch()
        try:
            for check in checks:
                if check["command"] is not None:
                    results[check["id"]] = run_check(record, scratch, check["command"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if gitops.rev(record) != sha:
        raise FleetError("HEAD moved while the checks ran; run verify again")
    text = render(task["id"], sha, record, hashlib.sha256(raw).hexdigest(), checks, results, common.now_stamp(now))
    paths = write_evidence(task["id"], holder_id, sha, text)
    failed = [check["id"] for check in checks if check["command"] is not None and results[check["id"]]["exit_code"] != 0]
    return {"task_id": task["id"], "sha": sha, "checks": len(checks), "failed": failed,
            "left_changes": gitops.dirty(record), "evidence": paths}


def _castle(store_path: Optional[str]) -> Optional[str]:
    if not store_path:
        return None
    if not store_path.startswith(ids.WORKTREES_ROOT + "/"):
        raise FleetError("the stored worktree is outside the castle worktrees folder")
    return config.CASTLE_ROOT + store_path[len(ids.CASTLE_ROOT):]
