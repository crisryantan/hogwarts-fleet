"""The go confirmer: finish a go or a Mischief managed whose transcript entry Claude Code had not written yet.

Claude Code writes a prompt's own transcript entry only after the UserPromptSubmit hook returns, so at hook time the
hook cannot see that Ryan typed the prompt. When that entry is the only thing missing (no agent_id, a prompt_id, and
for a go McGonagall's session), the hook calls start: it writes one claim for the prompt in the office and starts this
module as one detached process (run_desk.spawn_go_confirm), with its input on a pipe, never in argv or a file. Then:
- the claim holds the sha256 of that input, so the process runs only on the input the hook checked, and only once per
  prompt: it makes a second marker that must not exist yet before it reads the transcript;
- it reads the transcript tail every config.GO_CONFIRM_POLL_SECONDS for at most config.GO_CONFIRM_WAIT_SECONDS, until
  the prompt's own entry is there. A missing file or a cut last line is "not yet", never a pass;
- it runs the hook's own checks on that entry (user_prompt_submit.typing_entry: entrypoint, typed prompt, at most
  CLOSE_PROMPT_MAX_AGE seconds old);
- it takes the gos or the close from the entry's own text, never from its input, and refuses when they differ from
  what the hook saw. So the most a forged call could do is replay what Ryan typed in the last half minute;
- it runs each go through user_prompt_submit.run_go, the hook's own code, or the close through close_confirmed;
- it reports each outcome as one headmaster event on McGonagall's desk, so it shows on the next prompt, and writes
  the same line to logs/go-confirm.log. Neither carries the prompt id, a token or the TASK.md hash.
SIGTERM or SIGHUP ends a go through its take-back, as in the hook, and the event says the confirmer was stopped.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from typing import Callable, Optional

from hogwarts import pensieve
from hogwarts.errors import StoreError

from fleet import common, config, run_desk, safefs, transcript
from fleet.hooks import user_prompt_submit as hook
from fleet.safefs import FleetError

KINDS = ("go", "close")
INPUT_FIELDS = ("prompt", "prompt_id", "transcript_path", "session_id", "agent_type")
CLAIM_MAX_BYTES = 128
DIFFERENT = "the prompt in the transcript is not the one the hook saw"


def claim_key(prompt_id: str) -> str:
    """The claim's file name: a digest of the prompt id, so the id itself is never written anywhere."""
    return hashlib.sha256(prompt_id.encode("utf-8")).hexdigest()[:32]


def _payload(data: dict, desk: str, kind: str) -> bytes:
    fields = {key: data[key] for key in INPUT_FIELDS if isinstance(data.get(key), str)}
    return json.dumps({**fields, "kind": kind, "desk": desk}, ensure_ascii=True, sort_keys=True).encode("ascii")


def claimed(data: dict) -> bool:
    """Whether a confirmer was already started for this prompt."""
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None:
        return False
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
            return safefs.lstat(fd, claim_key(prompt_id)) is not None
    except safefs.Missing:
        return False


def start(data: dict, desk: str, kind: str) -> bool:
    """Claim this prompt and start its confirmer. False when one was claimed for it already, so nothing starts."""
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None or kind not in KINDS:
        raise FleetError("the hook input has no prompt_id")
    payload = _payload(data, desk, kind)
    if len(payload) > config.GO_CONFIRM_INPUT_MAX_BYTES:
        raise FleetError("the prompt is too long to confirm")
    key = claim_key(prompt_id)
    with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR, create=True) as fd:
        try:
            claim_fd = safefs.create_new(fd, key)
        except FileExistsError:
            return False
        try:
            safefs.write_all(claim_fd, hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n")
        finally:
            os.close(claim_fd)
        try:
            with common.signals_held():
                run_desk.spawn_go_confirm(payload)
        except BaseException:
            os.unlink(key, dir_fd=fd)  # nothing confirms this prompt, so the claim goes with it
            raise
    return True


def _read_input(raw: bytes) -> dict:
    if len(raw) > config.GO_CONFIRM_INPUT_MAX_BYTES:
        raise FleetError("the input is too large")
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("the input is not strict JSON") from None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise FleetError("the input is not a confirmer request")
    if data.get("desk") != config.HOOK_DESK and data.get("desk") not in config.INTERACTIVE_DESKS:
        raise FleetError("the input names no hook desk")
    if common.text_field(data, "prompt_id", 200) is None or not isinstance(data.get("prompt"), str):
        raise FleetError("the input has no prompt")
    return data


def _requests(kind: str, text: object) -> Optional[list]:
    if kind == "close":
        task_id = hook.close_request(text)
        return None if task_id is None else [task_id]
    found = hook.go_requests(text)
    return found if found is not None and len(found) <= config.GO_MAX_PER_PROMPT else None


def _report(conn, kind: str, task_id: str, key: str, lines: list, ok: bool) -> str:
    """Record one outcome as a headmaster event on McGonagall's desk, once per prompt and task. Returns its line."""
    summary = common.scrubbed_line(" ".join(lines), pensieve.SUMMARY_LIMIT)
    if conn is None:
        return summary
    try:
        try:
            pensieve.get_task(conn, task_id)
            linked = task_id
        except StoreError:
            linked = None
        pensieve.add_event(conn, hook.TASK_DESK, f"{kind}.{'confirmed' if ok else 'refused'}", "headmaster", summary,
                           task_id=linked, dedupe_key=f"{kind}-confirm:{task_id}:{key[:16]}")
    except Exception as exc:  # noqa: BLE001 - the log still has it
        return f"{summary} (its headmaster event could not be written: {type(exc).__name__})"
    return summary


def _prune(fd: int, key: str) -> None:
    """Remove other prompts' claims and markers older than GO_CONFIRM_KEEP_SECONDS, by the files' own clock."""
    now = time.time()
    for name in os.listdir(fd):
        if name.split(".")[0] == key:
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if now - info.st_mtime > config.GO_CONFIRM_KEEP_SECONDS:
                os.unlink(name, dir_fd=fd)
        except OSError:
            continue


def _wait(data: dict, clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple:
    """typing_entry once the prompt's entry is there, or once GO_CONFIRM_WAIT_SECONDS have gone by."""
    deadline = clock() + config.GO_CONFIRM_WAIT_SECONDS
    while True:
        refusal, entry = hook.typing_entry(data, int(clock()))
        if refusal != hook.NOT_YET or clock() >= deadline:
            return refusal, entry
        sleep(config.GO_CONFIRM_POLL_SECONDS)


def _confirmed(data: dict, conn, key: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> list:
    kind, desk = data["kind"], data["desk"]
    expected = _requests(kind, data["prompt"])
    if expected is None:
        return ["refused: the input holds no go or close"]
    out, done, running = [], [], None
    try:
        if kind == "go" and common.session_desk(data, desk) != hook.TASK_DESK:
            return [_report(conn, kind, task_id, key, [f"Go was not applied to {task_id}: {hook.GO_SESSION}"], False)
                    for task_id in expected]
        refusal, entry = _wait(data, clock, sleep)
        if refusal == hook.NOT_YET:
            refusal = (f"this prompt's transcript entry did not appear within {config.GO_CONFIRM_WAIT_SECONDS}"
                       " seconds")
        seen = None if refusal is not None else _requests(kind, transcript.prompt_text(entry))
        if refusal is None and seen != expected:
            refusal = DIFFERENT
        if refusal is not None:
            return [_report(conn, kind, task_id, key, hook.not_confirmed(kind, task_id, refusal), False)
                    for task_id in expected]
        for task_id in seen:  # taken from the entry's own text, which equals what the hook saw
            if conn is None:
                out.append(f"{task_id}: the store could not be opened, so nothing was applied")
                done.append(task_id)
                continue
            running = task_id
            if kind == "close":
                lines = hook.close_confirmed(conn, task_id, int(clock()))
                ok = lines[0].startswith("Mischief managed:")
            else:
                lines, ok = hook.run_go(conn, task_id, int(clock()))
            out.append(_report(conn, kind, task_id, key, lines, ok))
            done.append(task_id)
    except SystemExit:
        what = "Mischief managed" if kind == "close" else "Go"
        for task_id in expected:
            if task_id in done:
                continue
            if task_id == running:
                said = (f"{what} for {task_id} was stopped by a signal while it ran; whatever it made before its"
                        f" commit was taken back, so check castle task show {task_id} before you type it again.")
            else:
                said = (f"{what} for {task_id} was stopped by a signal before it ran, so nothing was applied for it;"
                        " type it again.")
            out.append(_report(conn, kind, task_id, key, [said], False))
        raise
    return out


def confirm(raw: bytes, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> list:
    """The log lines of one confirmer run on raw, the input the hook piped in."""
    try:
        data = _read_input(raw)
    except FleetError as exc:
        return [f"refused: {exc}"]
    key = claim_key(data["prompt_id"])
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
            try:
                claim = safefs.read_regular(fd, key, CLAIM_MAX_BYTES, "confirm claim")
            except safefs.Missing:
                return ["refused: no claim was made for this input"]
            if not hmac.compare_digest(claim.strip(), hashlib.sha256(raw).hexdigest().encode("ascii")):
                return ["refused: the input is not the one the hook claimed"]
            try:
                os.close(safefs.create_new(fd, f"{key}.ran"))
            except FileExistsError:
                return ["refused: this prompt was confirmed already"]
            _prune(fd, key)
    except (FleetError, OSError) as exc:
        return [f"refused: the claim could not be read ({type(exc).__name__})"]
    try:
        conn = common.connect()
    except Exception:  # noqa: BLE001 - nothing can be applied, and the log says so
        conn = None
    try:
        return _confirmed(data, conn, key, clock, sleep)
    finally:
        if conn is not None:
            conn.close()


def main(stdin=None, stdout=None) -> int:
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with common.ended_by_signals():
            lines = confirm(stdin.read(config.GO_CONFIRM_INPUT_MAX_BYTES + 1))
    except SystemExit:
        stdout.write(f"{stamp} go-confirm: stopped by a signal\n")
        raise
    for line in lines:
        stdout.write(f"{stamp} go-confirm: {common.one_line(line, 800)}\n")
    stdout.flush()
    return 0 if lines and not lines[0].startswith("refused:") else 1


if __name__ == "__main__":
    sys.exit(main())
