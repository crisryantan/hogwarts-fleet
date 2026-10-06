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
  the office reviews folder, where no desk can change it. Every run also keeps the exact TASK.md bytes it read in
  the office, as reviews/<task>/task-md-<sha256>.md, and its result names that digest, so a review round can record
  which TASK.md its verify read.

After-merge checks. A criterion may say "| after merge: <check>" instead of "| check: <check>": the same command,
written or malformed rule, but judged after the merge. verify lists each one under its own line and never runs it,
never counts it as failed, and SUMMARY keeps its words with counts of the before-merge criteria only. The closer
(fleet/closer.py) runs the after-merge commands through run_after_merge, in a fresh detached worktree at the merge
commit, under the same sandbox rule (sandboxed_for). Any other label, anything shaped like a second label after the
first (a pipe, a few words and a colon, outside the backticks, read as it shows), or an id used on two lines is a
malformed criterion: it never runs, and the evidence gives the plain reason.

Every check runs in a process group of its own that ends with it, and inherits the locks its caller holds for it
(run_command): the task's review lock, and for the closer Ollivander's launch gate, so a check still running after
its caller is killed keeps every other review, verify or closer pass off its worktree.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from typing import Callable, Optional

from hogwarts import ids, pensieve

from fleet import common, config, gitops, run_desk, safefs, toolchain
from fleet.safefs import FleetError

AC_LINE = re.compile(r"AC-(\d{1,3})\s+(.+?)\s*\|\s*(check|after merge):\s*(.+?)\s*")
COMMAND = re.compile(r"`([^`\x00-\x1f]{1,1000})`")
# A line that starts like a criterion and holds a pipe, but is not one, is a malformed criterion: never prose.
CRITERION_START = re.compile(r"AC-(\d+)\s+(.*)")
# Anything shaped like a label, a pipe then a few words and a colon, inside what must be true or inside a check outside
# its backticks, known label or not ("| check:", "| later:"): a criterion has one label.
LABEL_SHAPED = re.compile(r"\|\s*\w[\w -]{0,40}:")
BACKTICKED = re.compile(r"`[^`]*`")
WHEN = {"check": "before", "after merge": "after"}
UNKNOWN_LABEL = "a criterion's check is labelled check: or after merge:, and nothing else"
TWO_LABELS = "a criterion has one label"
# A lone line of this many base64 characters or more is masked in after-merge evidence.
LONE_BASE64 = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
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
    """Each acceptance criterion as {id, what, label, when, check, command, malformed}, in file order. label is
    "check" (when "before": run or judged before the merge) or "after merge" (when "after": judged after the
    merge, never before it). command is set only for a check that is one backtick command and nothing else.
    malformed is the plain reason a criterion is neither a command nor plain words, and None otherwise; a
    malformed criterion never runs. A line that starts with AC-<n> and holds a pipe but is not a criterion (an
    unknown label, a misspelt one) is malformed too, never prose, and so is every line of an id used twice."""
    checks = []
    for line in text.splitlines():
        stripped = line.strip()
        match = AC_LINE.fullmatch(stripped)
        if match is None:
            start = CRITERION_START.fullmatch(stripped)
            if start is None or "|" not in common.normalized(start.group(2)):
                continue
            what, _, rest = start.group(2).partition("|")
            checks.append({"id": f"AC-{start.group(1)}", "what": what.strip(), "label": None, "when": "before",
                           "check": rest.strip(), "command": None, "malformed": UNKNOWN_LABEL})
            continue
        what, label, check = match.group(2), match.group(3), match.group(4)
        command = COMMAND.fullmatch(check)
        # Read as it shows, so no invisible character hides a second label from this.
        if LABEL_SHAPED.search(common.normalized(BACKTICKED.sub("", check))) or LABEL_SHAPED.search(
                common.normalized(what)):
            command, malformed = None, TWO_LABELS
        else:
            malformed = None if command is not None else malformed_reason(check)
        checks.append({"id": f"AC-{match.group(1)}", "what": what, "label": label, "when": WHEN[label],
                       "check": check, "command": None if command is None else command.group(1),
                       "malformed": malformed})
    used = [check["id"] for check in checks]
    for check in checks:
        if used.count(check["id"]) > 1:
            check["command"], check["malformed"] = None, f"{check['id']} is used more than once"
    return checks


def before_merge(checks: list) -> list:
    """The criteria verify runs or the reviewer judges before the merge."""
    return [check for check in checks if check["when"] == "before"]


def after_merge(checks: list) -> list:
    """The after-merge criteria that are not malformed: judged after the merge, never run before it."""
    return [check for check in checks if check["when"] == "after" and check["malformed"] is None]


def sandboxed_for(task: dict) -> bool:
    """Whether a task's checks run under the Codex sandbox: every author's but your own sessions', whose code runs
    the way you would run it yourself. verify and run_after_merge both ask this, so the two never disagree."""
    return task["desk"] != config.OWN_SESSION_DESK


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


def _scrubbed_tail(path: str) -> tuple:
    """The last lines of a check's output for after-merge evidence: the window read starts at a line boundary, is
    normalized and scrubbed whole (common.untrusted_text), and only then cut to the last EVIDENCE_EXCERPT_LINES lines,
    so a credential whose start fell before the cut, or that an invisible character split, is still masked. A lone
    line of 40 or more base64 characters is masked too."""
    size = os.path.getsize(path)
    start = max(0, size - config.VERIFY_OUTPUT_MAX_BYTES)
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read()
    if start > 0:
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1:]
    text = common.untrusted_text(data.decode("utf-8", "replace"))
    lines = ["[base64]" if LONE_BASE64.fullmatch(line.strip()) else line for line in text.splitlines()]
    return lines[-config.EVIDENCE_EXCERPT_LINES:], size


def run_command(argv: list, cwd: str, env: dict, out_fd: int, keep_fds: tuple = ()) -> int:
    """Run one check's process and give its exit code, or -1 once it ran past VERIFY_TIMEOUT_SECONDS. It starts in a
    session and process group of its own and inherits no fd but its output and keep_fds, the locks its caller holds
    for it (a task's review lock, Ollivander's launch gate), which it keeps held for as long as it runs, even if this
    process is killed. Its whole group is killed once it ends, times out or this process is interrupted, so nothing it
    started goes on in the worktree after it."""
    with common.signals_held():  # a process that started always has its handle here, to be ended below
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out_fd,
                                 stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                 pass_fds=tuple(keep_fds))
    try:
        return child.wait(timeout=config.VERIFY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return -1
    finally:
        with common.signals_held():
            _end_group(child)


def _end_group(child: subprocess.Popen) -> None:
    """Kill what is left of the process group a check's process leads, then reap the process."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def run_check(record: dict, scratch: str, command: str, sandboxed: bool = True, scrub: bool = False,
              keep_fds: tuple = ()) -> dict:
    out_path = f"{scratch}/out-{secrets.token_hex(4)}.log"
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    started = time.monotonic()
    try:
        argv = sandbox_argv(record, scratch, command) if sandboxed else [
            config.BASH_BIN, "--noprofile", "--norc", "-c", command]
        exit_code = run_command(argv, record["path"], child_env(scratch, record), fd, keep_fds)
    finally:
        os.close(fd)
    lines, size = _scrubbed_tail(out_path) if scrub else _tail(out_path)
    return {"exit_code": exit_code, "seconds": round(time.monotonic() - started, 1), "lines": lines,
            "output_bytes": size}


def _make_scratch() -> str:
    """A private folder for one verify run, <user temp>/hogwarts-verify-<random>, with a home and a temp."""
    scratch = run_desk.fresh_temp(run_desk.desk_temp_dir(f"verify-{secrets.token_hex(8)}"))
    os.mkdir(f"{scratch}/home", 0o700)
    os.mkdir(f"{scratch}/tmp", 0o700)
    return scratch


def _cleaned_lines(cleaned: tuple, what: str) -> list:
    if cleaned:
        return ([f"CLEANED {len(cleaned)} git-ignored paths before the {what}, each as git names it:"]
                + ["    " + name for name in cleaned])
    return ["CLEANED nothing: the worktree held no git-ignored files besides its dependency links"]


def _ran_line(when: str, sandboxed: bool) -> str:
    if sandboxed:
        return f"RAN {when} under codex sandbox: worktree write, repo .git read, no network, no office"
    return f"RAN {when} without the Codex sandbox, because Ryan's own session wrote this code; throwaway HOME and TMPDIR"


def _label_line(check: dict) -> str:
    """The check as TASK.md labels it. A criterion with no known label shows what followed its pipe."""
    return check["check"] if check["label"] is None else f"{check['label']}: {check['check']}"


def _result_lines(result: dict) -> list:
    exit_text = "timed out" if result["exit_code"] == -1 else str(result["exit_code"])
    lines = [f"exit: {exit_text} | {result['seconds']}s | {result['output_bytes']} bytes of output",
             f"output, last {config.EVIDENCE_EXCERPT_LINES} lines:"]
    return lines + (["    " + line for line in result["lines"]] or ["    (no output)"])


def render(task_id: str, sha: str, record: dict, md_digest: str, checks: list, results: dict, now: int,
           sandboxed: bool = True, cleaned: tuple = ()) -> str:
    """The evidence for one commit. SUMMARY counts the before-merge criteria (and every malformed one); an
    after-merge criterion is listed with its own line and never looked up in results, since it never runs here."""
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    before = [check for check in before_merge(checks) if check["malformed"] is None]
    commands = [check for check in before if check["command"] is not None]
    ran = len(commands)
    passed = sum(1 for check in commands if results[check["id"]]["exit_code"] == 0)
    malformed = sum(1 for check in checks if check["malformed"] is not None)
    later = after_merge(checks)
    later_commands = sum(1 for check in later if check["command"] is not None)
    lines = [f"EVIDENCE {task_id} @ {sha}", f"TASK.md sha256 {md_digest}", f"WORKTREE {record['path']}",
             *_cleaned_lines(cleaned, "checks"), _ran_line(when, sandboxed),
             (f"SUMMARY {passed} of {ran} commands exited 0, {malformed} malformed checks not run,"
              f" {len(before) - ran} observations for the reviewer")]
    if later:
        lines.append(f"AFTER MERGE {later_commands} commands and {len(later) - later_commands} written checks, judged"
                     " after merge and never run before it")
    lines.append("")
    if not checks:
        lines.append("No acceptance criteria with a check were found in TASK.md.")
    for check in checks:
        lines += [f"{check['id']} {check['what']}", _label_line(check)]
        if check["malformed"] is not None:
            lines += [f"not run: malformed, {check['malformed']}. {MALFORMED_HINT}", ""]
        elif check["when"] == "after":
            kind = "run at the merge commit" if check["command"] is not None else "judged after merge"
            lines += [f"not run: an after-merge check, {kind}", ""]
        elif check["command"] is None:
            lines += ["not run: an observation for the reviewer to judge", ""]
        else:
            lines += [*_result_lines(results[check["id"]]), ""]
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


def frozen_name(digest: str) -> str:
    """The office copy of the TASK.md bytes whose sha256 is digest: reviews/<task>/task-md-<digest>.md."""
    return f"task-md-{ids.check('sha256', digest)}.md"


def keep_task_md(task_id: str, raw: bytes) -> str:
    """Keep the exact TASK.md bytes a check run read in the office, where no desk can write, named by their sha256.
    Written through a temp file and a rename every time, so a damaged copy heals; every reader hashes it again.
    Returns the digest."""
    digest = hashlib.sha256(raw).hexdigest()
    with safefs.opened_dir(config.OFFICE_ROOT, "reviews", ids.check("task", task_id), create=True) as fd:
        safefs.write_new(fd, frozen_name(digest), raw)
    return digest


def verify(conn, task_id: str, now: Optional[int] = None, keep_fds: tuple = ()) -> dict:
    """Run the before-merge checks of a task's worktree and write the evidence. keep_fds is the task's review lock
    the caller holds: each check's process inherits it, so no other review or verify starts on the worktree while a
    check is still running, even after this process is killed."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    record = gitops.find_record(_castle(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree with an office record")
    if gitops.dirty(record):
        raise FleetError("the worktree has uncommitted changes; evidence must belong to one commit")
    holder_id, _ = task_md(conn, task["id"])
    raw = read_task_md(holder_id)
    # Kept before anything runs: a run that cannot keep it fails here, before any round could record its digest.
    md_digest = keep_task_md(task["id"], raw)
    cleaned = gitops.clean_ignored(record)
    sha = gitops.rev(record)
    checks = parse_checks(raw.decode("utf-8", "replace"))
    commands = [check for check in before_merge(checks) if check["command"] is not None]
    results = {}
    sandboxed = sandboxed_for(task)
    if commands:
        scratch = _make_scratch()
        try:
            for check in commands:
                results[check["id"]] = run_check(record, scratch, check["command"], sandboxed, keep_fds=keep_fds)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if gitops.rev(record) != sha:
        raise FleetError("HEAD moved while the checks ran; run verify again")
    text = render(task["id"], sha, record, md_digest, checks, results, common.now_stamp(now), sandboxed, cleaned)
    paths = write_evidence(task["id"], holder_id, sha, text)
    failed = [check["id"] for check in commands if results[check["id"]]["exit_code"] != 0]
    malformed = [check["id"] for check in checks if check["malformed"] is not None]
    return {"task_id": task["id"], "sha": sha, "checks": len(checks), "failed": failed, "malformed": malformed,
            "after_merge": [check["id"] for check in after_merge(checks)], "task_md_sha256": md_digest,
            "left_changes": gitops.dirty(record), "evidence": paths}


# After the merge (the closer, fleet/closer.py)

AFTER_EVIDENCE_HEADER = re.compile(r"AFTER-MERGE EVIDENCE (tk_[0-9a-f]{16}) @ ([0-9a-f]{40})")
AFTER_EXIT_LINE = re.compile(r"(AC-\d{1,3}) exit (-?\d{1,3})")


def after_evidence_name(merge_sha: str) -> str:
    return f"after-merge-evidence-{ids.check('sha', merge_sha)}.md"


def render_after_merge(task_id: str, merge_sha: str, pass_sha: str, md_digest: str, worktree_path: str,
                       commands: list, results: dict, sandboxed: bool) -> str:
    """The after-merge evidence. Its head, up to the first blank line, holds only script values: the task, the merge
    commit, the reviewed commit, the approved TASK.md digest and one exit line per command, written after any scrub,
    since pensieve.scrub would mask a full sha. Everything else from the repository, TASK.md or a command was
    normalized and scrubbed whole before any cut (common.untrusted_text), since this evidence goes to the judge's
    pack. A command's result may come from an earlier pass that a stop or a kill ended (see run_after_merge)."""
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(results[check["id"]]["ran_at"] for check in commands)))
    passed = sum(1 for check in commands if results[check["id"]]["exit_code"] == 0)
    cleaned = tuple(dict.fromkeys(name for check in commands for name in results[check["id"]]["cleaned"]))
    lines = [f"AFTER-MERGE EVIDENCE {task_id} @ {merge_sha}", f"PASS {pass_sha}", f"TASK.md sha256 {md_digest}",
             f"WORKTREE {worktree_path}", *_cleaned_lines(cleaned, "commands"), _ran_line(when, sandboxed),
             f"SUMMARY {passed} of {len(commands)} after-merge commands exited 0"]
    lines += [f"{check['id']} exit {results[check['id']]['exit_code']}" for check in commands]
    lines.append("")
    for check in commands:
        lines += [f"{check['id']} {common.untrusted_text(check['what'])}",
                  f"after merge: `{common.untrusted_text(check['command'])}`", *_result_lines(results[check["id"]]),
                  ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_after_evidence(text: str, task_id: str, merge_sha: str, pass_sha: str, md_digest: str,
                         command_ids: list) -> Optional[dict]:
    """The exit code of every after-merge command from an evidence file, or None unless its head parses whole:
    it names this task, merge commit, reviewed commit and TASK.md digest, and holds exactly one exit line for each
    command id and no other."""
    head = text.split("\n\n", 1)[0].splitlines()
    if len(head) < 3 or head[0] != f"AFTER-MERGE EVIDENCE {task_id} @ {merge_sha}" or head[1] != f"PASS {pass_sha}" \
            or head[2] != f"TASK.md sha256 {md_digest}":
        return None
    exits = {}
    for line in head:
        match = AFTER_EXIT_LINE.fullmatch(line)
        if match is not None:
            if match.group(1) in exits:
                return None
            exits[match.group(1)] = int(match.group(2))
    if sorted(exits) != sorted(command_ids):
        return None
    return exits


def after_results_name(merge_sha: str) -> str:
    """The office record of each after-merge command that has ended at a merge commit: after-merge-results-<sha>.json."""
    return f"after-merge-results-{ids.check('sha', merge_sha)}.json"


AFTER_RESULT_KEYS = {"exit_code", "seconds", "output_bytes", "lines", "ran_at", "cleaned"}


def dump_after_results(task_id: str, merge_sha: str, pass_sha: str, md_digest: str, results: dict) -> bytes:
    """The results record, bound to the task, the merge commit, the reviewed commit and the approved TASK.md."""
    data = {"task_id": task_id, "merge_sha": merge_sha, "pass_sha": pass_sha, "task_md_sha256": md_digest,
            "results": {key: {field: value[field] for field in sorted(AFTER_RESULT_KEYS)}
                        for key, value in sorted(results.items())}}
    return (json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _after_result(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == AFTER_RESULT_KEYS
            and type(value["exit_code"]) is int and -255 <= value["exit_code"] <= 255
            and type(value["seconds"]) in (int, float) and 0 <= value["seconds"] <= 10 ** 7
            and type(value["output_bytes"]) is int and value["output_bytes"] >= 0
            and type(value["ran_at"]) is int and 0 <= value["ran_at"] <= ids.MAX_TIME
            and isinstance(value["lines"], list) and len(value["lines"]) <= config.EVIDENCE_EXCERPT_LINES
            and all(isinstance(line, str) for line in value["lines"])
            and isinstance(value["cleaned"], list) and all(isinstance(name, str) for name in value["cleaned"]))


def parse_after_results(raw: bytes, task_id: str, merge_sha: str, pass_sha: str, md_digest: str,
                        command_ids: list) -> Optional[dict]:
    """The kept result of each after-merge command that ended, {id: result}, or None unless the record reads whole:
    strict JSON naming this task, merge commit, reviewed commit and TASK.md digest, with results only for these
    command ids, each of its shape."""
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"task_id", "merge_sha", "pass_sha", "task_md_sha256", "results"} \
            or (data["task_id"], data["merge_sha"], data["pass_sha"], data["task_md_sha256"]) != (
                task_id, merge_sha, pass_sha, md_digest):
        return None
    results = data["results"]
    if not isinstance(results, dict) or not set(results) <= set(command_ids) \
            or not all(_after_result(value) for value in results.values()):
        return None
    return results


def run_after_merge(conn, task: dict, merged_record: dict, merge_sha: str, pass_sha: str, task_md_sha256: str,
                    checks: list, now: Optional[int] = None, done: Optional[dict] = None,
                    launch: Optional[Callable] = None, keep: Optional[Callable] = None) -> dict:
    """Run each after-merge command that has no result in done ({id: result}, the results kept from passes a stop or
    a kill ended) in the fresh detached worktree at the merge commit (merged_record), under the same sandbox rule as
    verify (sandboxed_for), then write the evidence of every command (write_after_merge). Refuses a dirty worktree,
    removes every git-ignored path first, and refuses when HEAD is not the merge commit before the commands or after
    any of them. launch(check) is a context entered right before each command starts, which checks again whatever
    may stop it and yields the fds the command's process inherits for its whole life; a raise from it starts nothing
    more. keep(results) is called with every result so far as each command ends, so one that ended is never lost to a
    stop or a kill, and never run again. {exits, failed, evidence_sha256}."""
    commands = [check for check in after_merge(checks) if check["command"] is not None]
    if not commands:
        raise FleetError("there is no after-merge command to run")
    results = dict(done or {})
    todo = [check for check in commands if check["id"] not in results]
    if todo:
        if gitops.dirty(merged_record):
            raise FleetError("the merged worktree has uncommitted changes")
        cleaned = [common.untrusted_text(name) for name in gitops.clean_ignored(merged_record)]
        if gitops.rev(merged_record) != merge_sha:
            raise FleetError("the merged worktree is not at the merge commit")
        sandboxed = sandboxed_for(task)
        scratch = _make_scratch()
        try:
            for check in todo:
                with (contextlib.nullcontext(()) if launch is None else launch(check)) as fds:
                    result = run_check(merged_record, scratch, check["command"], sandboxed, scrub=True,
                                       keep_fds=tuple(fds))
                if gitops.rev(merged_record) != merge_sha:
                    raise FleetError("HEAD moved while the after-merge commands ran")
                results[check["id"]] = {**result, "ran_at": common.now_stamp(now), "cleaned": cleaned}
                if keep is not None:
                    keep(results)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return write_after_merge(conn, task, merge_sha, pass_sha, task_md_sha256, checks, results, merged_record["path"])


def write_after_merge(conn, task: dict, merge_sha: str, pass_sha: str, task_md_sha256: str, checks: list,
                      results: dict, worktree_path: str) -> dict:
    """The after-merge evidence from a result for every after-merge command: the office copy in one atomic rename,
    then the castle copies next to TASK.md. {exits, failed, evidence_sha256}."""
    commands = [check for check in after_merge(checks) if check["command"] is not None]
    if not commands or any(check["id"] not in results for check in commands):
        raise FleetError("an after-merge command has no result to write evidence for")
    text = render_after_merge(task["id"], merge_sha, pass_sha, task_md_sha256, worktree_path, commands, results,
                              sandboxed_for(task))
    data = text.encode("utf-8")
    holder_id, _ = task_md(conn, task["id"])
    with safefs.opened_dir(config.OFFICE_ROOT, "reviews", task["id"], create=True) as fd:
        safefs.write_new(fd, after_evidence_name(merge_sha), data)
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", holder_id) as fd:
        _replace(fd, "after-merge-evidence.md", data)
        _replace(fd, f"after-merge-evidence-{merge_sha[:12]}.md", data)
    exits = {check["id"]: results[check["id"]]["exit_code"] for check in commands}
    return {"exits": exits, "failed": [key for key, code in exits.items() if code != 0],
            "evidence_sha256": hashlib.sha256(data).hexdigest()}


def _castle(store_path: Optional[str]) -> Optional[str]:
    if not store_path:
        return None
    if not store_path.startswith(ids.WORKTREES_ROOT + "/"):
        raise FleetError("the stored worktree is outside the castle worktrees folder")
    return config.CASTLE_ROOT + store_path[len(ids.CASTLE_ROOT):]
