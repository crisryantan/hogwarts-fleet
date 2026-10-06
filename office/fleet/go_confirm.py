"""The go confirmer: finish a go or a Mischief managed whose transcript entry Claude Code had not written yet.

Claude Code writes a prompt's own transcript entry only after the UserPromptSubmit hook returns, so at hook time the
hook cannot see that Ryan typed the prompt. When that entry is the only thing missing (no agent_id, a prompt_id, and
for a go McGonagall's session), the hook calls start: it writes one claim for the prompt in the office and starts this
module as one detached process (run_desk.spawn_go_confirm), with its input on a pipe, never in argv or a file. Then:
- the claim holds the sha256 of that input and is published whole (temp file, then linked into place), so the process
  runs only on the input the hook checked. Once the process has its whole input the claim is its own, whatever signal
  comes; if it never got it, the process is reaped and the claim removed;
- it takes the prompt with a run marker, <key>.ran, made create-exclusive as "pending <pid> <time>", and sets it to
  "done" only once every outcome is a headmaster event. A marker whose process is gone, or a claim no confirmer
  took, past the wait window plus GO_CONFIRM_STALE_MARGIN_SECONDS, is reported by the next hook or confirmer for that
  prompt as an interrupted confirmation, under the outcome's own dedupe key, and the go or close is never run again;
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
import secrets
import sys
import time
from typing import Callable, Optional

from hogwarts import pensieve

from fleet import common, config, run_desk, safefs, transcript
from fleet.hooks import user_prompt_submit as hook
from fleet.safefs import FleetError

KINDS = ("go", "close")
INPUT_FIELDS = ("prompt", "prompt_id", "transcript_path", "session_id", "agent_type")
CLAIM_MAX_BYTES = 128
DIFFERENT = hook.DIFFERENT


def claim_key(prompt_id: str) -> str:
    """The claim's file name: a digest of the prompt id, so the id itself is never written anywhere."""
    return hashlib.sha256(prompt_id.encode("utf-8")).hexdigest()[:32]


def _payload(data: dict, desk: str, kind: str) -> bytes:
    fields = {key: data[key] for key in INPUT_FIELDS if isinstance(data.get(key), str)}
    return json.dumps({**fields, "kind": kind, "desk": desk}, ensure_ascii=True, sort_keys=True).encode("ascii")


def _publish(fd: int, name: str, data: bytes) -> bool:
    """Put a whole file at name, create-exclusive: written to a temp file first, then linked into place, so no reader
    ever sees it half written and a failed write leaves nothing. False when name exists already."""
    temp = f".{name}.{secrets.token_hex(6)}.tmp"
    temp_fd = safefs.create_new(fd, temp)
    try:
        try:
            safefs.write_all(temp_fd, data)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except OSError:
            pass


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
    """Claim this prompt and start its confirmer. False when one was claimed for it already, so nothing starts.
    The claim is removed when the confirmer could not be handed its whole input, and never once it has been: from
    then on it is the confirmer's, whatever signal comes."""
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None or kind not in KINDS:
        raise FleetError("the hook input has no prompt_id")
    payload = _payload(data, desk, kind)
    if len(payload) > config.GO_CONFIRM_INPUT_MAX_BYTES:
        raise FleetError("the prompt is too long to confirm")
    key = claim_key(prompt_id)
    with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR, create=True) as fd:
        if not _publish(fd, key, hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n"):
            return False
        handed = False
        try:
            with common.signals_held():
                run_desk.spawn_go_confirm(payload)
                handed = True
        except BaseException:
            if not handed:  # nothing confirms this prompt, so the claim goes with it
                os.unlink(key, dir_fd=fd)
            raise
    return True


# The run marker, <key>.ran: "pending <pid> <unix time>" from the moment a confirmer takes the prompt, "done" once
# its outcome is recorded. A pending marker whose process is gone, or a claim no confirmer ever marked, older than
# the wait window plus GO_CONFIRM_STALE_MARGIN_SECONDS, is an interrupted confirmation: it is reported, never run.

def _ran(fd: int, key: str) -> Optional[tuple]:
    """("done",), ("pending", pid, stamp), ("unknown",) for an unreadable marker, or None when there is none."""
    try:
        raw = safefs.read_regular(fd, f"{key}.ran", 64, "confirm marker")
    except safefs.Missing:
        return None
    except (FleetError, OSError):
        return ("unknown",)
    words = raw.decode("ascii", "replace").split()
    if words == ["done"]:
        return ("done",)
    if len(words) == 3 and words[0] == "pending" and words[1].isdigit() and words[2].isdigit():
        return ("pending", int(words[1]), int(words[2]))
    return ("unknown",)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _stale(fd: int, key: str) -> bool:
    """Whether this prompt's confirmation was interrupted: its confirmer is gone with no outcome recorded."""
    limit = config.GO_CONFIRM_WAIT_SECONDS + config.GO_CONFIRM_STALE_MARGIN_SECONDS
    now = time.time()
    state = _ran(fd, key)
    if state is None:
        info = safefs.lstat(fd, key)
        return info is not None and now - info.st_mtime > limit
    if state[0] == "pending":
        return not _alive(state[1]) and now - state[2] > limit
    if state[0] == "unknown":
        info = safefs.lstat(fd, f"{key}.ran")
        return info is not None and now - info.st_mtime > limit
    return False


def _finish(fd: int, key: str) -> None:
    safefs.write_new(fd, f"{key}.ran", b"done\n")


def interrupted_lines(kind: str, task_id: str) -> list:
    if kind == "close":
        return [f"The confirmation of Mischief managed for {task_id} was interrupted; check castle task show {task_id}"
                " and type Mischief managed again if it is not closed."]
    return [f"The confirmation for {task_id} was interrupted; check castle task show {task_id} and type the go again"
            " if it is not registered."]


def _report_interrupted(conn, fd: int, key: str, kind: str, task_ids: list) -> list:
    """One headmaster event per task saying the confirmation was interrupted, under the same dedupe key as an outcome,
    so a recorded outcome is never told twice; then the marker is done once every event is written."""
    out, written = [], True
    for task_id in task_ids:
        line, ok = _report(conn, kind, task_id, key, interrupted_lines(kind, task_id), False)
        out.append(line)
        written = written and ok
    if written:
        _finish(fd, key)
    return out


def finished(data: dict) -> bool:
    """Whether this prompt's confirmation has recorded its outcome."""
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None:
        return False
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
            return _ran(fd, claim_key(prompt_id)) == ("done",)
    except (FleetError, OSError):
        return False


def interrupted(conn, data: dict, kind: str, task_ids: list) -> list:
    """For a hook that found this prompt claimed: the interrupted lines when its confirmation was interrupted (and
    they are recorded as events), else []. It never runs the go or the close."""
    prompt_id = common.text_field(data, "prompt_id", 200)
    if prompt_id is None:
        return []
    key = claim_key(prompt_id)
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
            if not _stale(fd, key):
                return []
            _report_interrupted(conn, fd, key, kind, task_ids)
    except (FleetError, OSError):
        return []
    lines = []
    for task_id in task_ids:
        lines += interrupted_lines(kind, task_id)
    return lines


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


def _report(conn, kind: str, task_id: str, key: str, lines: list, ok: bool) -> tuple:
    """Record one outcome as a headmaster event on McGonagall's desk, once per prompt and task. Returns (its line,
    whether the event is in the store)."""
    summary = common.scrubbed_line(" ".join(lines), pensieve.SUMMARY_LIMIT)
    if conn is None:
        return f"{summary} (its headmaster event could not be written: no store)", False
    try:
        try:
            pensieve.get_task(conn, task_id)
            linked = task_id
        except Exception:  # noqa: BLE001 - no task, or a store that cannot read it: the event goes unlinked
            linked = None
        pensieve.add_event(conn, hook.TASK_DESK, f"{kind}.{'confirmed' if ok else 'refused'}", "headmaster", summary,
                           task_id=linked, dedupe_key=f"{kind}-confirm:{task_id}:{key[:16]}")
    except Exception as exc:  # noqa: BLE001 - the log still has it, and the marker stays pending
        return f"{summary} (its headmaster event could not be written: {type(exc).__name__})", False
    return summary, True


def _prune(fd: int, key: str) -> None:
    """Remove other prompts' claims and markers older than GO_CONFIRM_KEEP_SECONDS, by the files' own clock."""
    now = time.time()
    for name in os.listdir(fd):
        if name.lstrip(".").split(".")[0] == key:
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


def _confirmed(data: dict, conn, key: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple:
    """(log lines, whether every outcome is recorded as an event)."""
    kind, desk = data["kind"], data["desk"]
    expected = hook.requests_in(kind, data["prompt"])
    if expected is None:
        return ["refused: the input holds no go or close"], True
    out, done, running, written = [], [], None, []

    def report(task_id: str, lines: list, ok: bool) -> None:
        line, recorded = _report(conn, kind, task_id, key, lines, ok)
        out.append(line)
        written.append(recorded)

    try:
        if kind == "go" and common.session_desk(data, desk) != hook.TASK_DESK:
            for task_id in expected:
                report(task_id, [f"Go was not applied to {task_id}: {hook.GO_SESSION}"], False)
            return out, all(written)
        refusal, entry = _wait(data, clock, sleep)
        if refusal == hook.NOT_YET:
            refusal = (f"this prompt's transcript entry did not appear within {config.GO_CONFIRM_WAIT_SECONDS}"
                       " seconds")
        seen = None
        if refusal is None:
            seen, refusal = hook.verified_requests(kind, data["prompt"], entry)
        if refusal is not None:
            for task_id in expected:
                report(task_id, hook.not_confirmed(kind, task_id, refusal), False)
            return out, all(written)
        for task_id in seen:  # taken from the entry's own text, which equals what the hook saw
            if conn is None:
                out.append(f"{task_id}: the store could not be opened, so nothing was applied")
                written.append(False)
                done.append(task_id)
                continue
            running = task_id
            if kind == "close":
                lines = hook.close_confirmed(conn, task_id, int(clock()))
                ok = lines[0].startswith("Mischief managed:")
            else:
                lines, ok = hook.run_go(conn, task_id, int(clock()))
            report(task_id, lines, ok)
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
            report(task_id, [said], False)
        _done_if(key, all(written))
        raise
    return out, all(written)


def _done_if(key: str, recorded: bool) -> None:
    """Mark the prompt's confirmation done once every outcome is in the store; otherwise it stays pending, so a later
    hook reports it as interrupted once this process is gone."""
    if not recorded:
        return
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
            _finish(fd, key)
    except (FleetError, OSError):
        pass


def confirm(raw: bytes, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> list:
    """The log lines of one confirmer run on raw, the input the hook piped in."""
    try:
        data = _read_input(raw)
    except FleetError as exc:
        return [f"refused: {exc}"]
    key = claim_key(data["prompt_id"])
    try:
        conn = common.connect()
    except Exception:  # noqa: BLE001 - nothing can be applied, and the marker stays pending
        conn = None
    try:
        try:
            with safefs.opened_dir(config.OFFICE_ROOT, config.GO_CONFIRM_DIR) as fd:
                try:
                    claim = safefs.read_regular(fd, key, CLAIM_MAX_BYTES, "confirm claim")
                except safefs.Missing:
                    return ["refused: no claim was made for this input"]
                if not hmac.compare_digest(claim.strip(), hashlib.sha256(raw).hexdigest().encode("ascii")):
                    return ["refused: the input is not the one the hook claimed"]
                marker = f"pending {os.getpid()} {int(time.time())}\n".encode("ascii")
                if not _publish(fd, f"{key}.ran", marker):
                    state = _ran(fd, key)
                    if state is not None and state[0] != "done" and _stale(fd, key):
                        expected = hook.requests_in(data["kind"], data["prompt"]) or []
                        return _report_interrupted(conn, fd, key, data["kind"], expected)
                    if state is not None and state[0] == "done":
                        return ["refused: this prompt was confirmed already"]
                    return ["refused: another confirmer has this prompt"]
                _prune(fd, key)
        except (FleetError, OSError) as exc:
            return [f"refused: the claim could not be read ({type(exc).__name__})"]
        lines, recorded = _confirmed(data, conn, key, clock, sleep)
        _done_if(key, recorded)
        return lines
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
