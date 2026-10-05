"""fleet feed: watch one desk, or the whole fleet, as it works. Strictly read-only.

  fleet feed --desk <name>     one desk; --desk owl-post shows every owl and the Owl Post's events
  fleet feed --all             every desk

About once a second it prints what is new, one line each, stamped with local HH:MM:SS:
owls to or from the desk (kind and subject, never the body), the start and end of each run,
the desk's headmaster events, and the live output of its current run, read from that run's
.out file in the office runs folder (Claude stream-json or Codex exec --json). A desk with more
than one run slot (config.RUN_SLOTS) can have several runs going: the feed follows whichever wrote
last and keeps its place in the others, so each line of each run is shown once.

The store is opened with db.connect_readonly and nothing is ever written. Every printed line
goes through sanitize first, so nothing a desk writes can drive the terminal. Ctrl+C stops it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
import time
from typing import Callable, Optional

from hogwarts import db, ids, watch
from hogwarts.errors import StoreError

from . import config, safefs
from .safefs import FleetError

POLL_SECONDS = 1.0
LINE_MAX_CHARS = 400
TEXT_MAX_CHARS = 300
ARG_MAX_CHARS = 160
NAME_MAX_CHARS = 60
CLIP_SCAN_CHARS = 65536
# Bytes read from one run file per poll, and the longest event kept while waiting for its newline.
READ_CHUNK_BYTES = 1024 * 1024
EVENT_MAX_BYTES = 4 * 1024 * 1024
FINAL_READ_CHUNKS = 32
OWL_POST_DESK = "owl-post"
OWL_POST_KIND_PREFIX = "owlpost."
RUN_OUT = re.compile(r"(run-[0-9a-f]{16})\.out")
# The input field that best says what a Claude tool call is doing.
TOOL_ARGS = {
    "Bash": "command", "Read": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
    "NotebookEdit": "notebook_path", "Grep": "pattern", "Glob": "pattern", "WebFetch": "url", "WebSearch": "query",
    "Task": "description", "Agent": "description",
}
METRIC_FIELDS = ("id", "ts", "desk", "run_id", "model", "input_tokens", "output_tokens", "cache_read_tokens",
                 "cost_usd", "duration_ms")

# Terminal escape sequences, removed whole: CSI; OSC, DCS, SOS, PM and APC strings up to their
# terminator or the end of the text; every other ESC sequence and a lone ESC; and the C1 forms.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]?"
    r"|\x1b[\]PX^_].*?(?:\x07|\x1b\\|\x9c|\Z)"
    r"|\x1b[ -/]*[0-~]?"
    r"|\x9b[0-?]*[ -/]*[@-~]?"
    r"|[\x90\x98\x9d\x9e\x9f].*?(?:\x07|\x1b\\|\x9c|\Z)",
    re.DOTALL,
)


def sanitize(text: str) -> str:
    """Text that cannot drive a terminal: no escape sequences, no C0 controls but newline and tab,
    no DEL, no C1 controls, and "?" for anything else that is not printable."""
    kept = []
    for char in _ESCAPES.sub("", text):
        if char in "\n\t":
            kept.append(char)
        elif char < " " or "\x7f" <= char <= "\x9f":
            continue
        else:
            kept.append(char if char.isprintable() else "?")
    return "".join(kept)


def clip(value: object, limit: int) -> str:
    """One clean line of at most limit characters. Only the first CLIP_SCAN_CHARS are looked at."""
    text = " ".join(sanitize(str(value)[:CLIP_SCAN_CHARS]).split())
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."


def stamp(ts: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def line(ts: int, text: str) -> str:
    """The printed form of one feed line, sanitized again whole and capped."""
    full = sanitize(f"{stamp(ts)} {text}").replace("\n", " ")
    return full if len(full) <= LINE_MAX_CHARS else full[: LINE_MAX_CHARS - 3] + "..."


def duration(ms: object) -> str:
    seconds = ms / 1000 if type(ms) is int and ms >= 0 else 0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}s"


# Rendering run output


def _tool_line(block: dict) -> str:
    name = block.get("name") if isinstance(block.get("name"), str) else "?"
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    arg = tool_input.get(TOOL_ARGS.get(name, ""))
    text = f"tool: {clip(name, NAME_MAX_CHARS)}"
    return text + f" {clip(arg, ARG_MAX_CHARS)}" if isinstance(arg, str) and arg.strip() else text


def _claude_lines(event: dict) -> list:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        model = event.get("model")
        return ["claude started" + (f", model {clip(model, NAME_MAX_CHARS)}" if isinstance(model, str) else "")]
    if kind == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        lines = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                lines.append("says: " + clip(block["text"], TEXT_MAX_CHARS))
            elif block.get("type") == "tool_use":
                lines.append(_tool_line(block))
        return lines
    if kind == "result":
        subtype = event.get("subtype")
        text = "result: " + (clip(subtype, NAME_MAX_CHARS) if isinstance(subtype, str) else "?")
        if event.get("is_error") is True:
            text += " (error)"
        if isinstance(event.get("result"), str) and event["result"].strip():
            text += ": " + clip(event["result"], TEXT_MAX_CHARS)
        return [text]
    return []


def _codex_item(item: dict) -> list:
    kind = item.get("type")
    if kind == "agent_message" and isinstance(item.get("text"), str) and item["text"].strip():
        return ["says: " + clip(item["text"], TEXT_MAX_CHARS)]
    if kind == "command_execution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        code = item.get("exit_code")
        status = f"exit {code}" if type(code) is int else clip(item.get("status") or "?", NAME_MAX_CHARS)
        return [f"command ({status}): {clip(command, ARG_MAX_CHARS)}"]
    if kind == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        parts = [f"{change.get('kind', '?')} {change.get('path', '?')}" for change in changes
                 if isinstance(change, dict)]
        return ["files: " + clip(", ".join(parts) or "?", TEXT_MAX_CHARS)]
    return []


def _codex_lines(event: dict) -> list:
    kind = event.get("type")
    if kind == "thread.started":
        return ["codex started"]
    if kind == "item.completed" and isinstance(event.get("item"), dict):
        return _codex_item(event["item"])
    if kind in ("turn.failed", "error"):
        error = event.get("error") if isinstance(event.get("error"), dict) else event
        message = error.get("message")
        label = "turn failed" if kind == "turn.failed" else "error"
        return [label + (": " + clip(message, TEXT_MAX_CHARS) if isinstance(message, str) else "")]
    return []


def render_event(event: object) -> list:
    """Feed lines for one run output event, Claude stream-json or Codex JSONL. Unknown events give none."""
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return []
    return _claude_lines(event) or _codex_lines(event)


def outcome(event: dict) -> Optional[str]:
    """How a run says it ended, from its last result or turn event, for the run end line."""
    kind = event.get("type")
    if kind == "result":
        subtype = clip(event.get("subtype") or "?", NAME_MAX_CHARS)
        return f"error {subtype}" if event.get("is_error") is True else subtype
    if kind == "turn.completed":
        return "completed"
    if kind == "turn.failed":
        return "turn failed"
    return None


# Following the store and the run files


class RunTail:
    """Where the feed is in one desk's current run file, and how that run and the one before it ended."""

    def __init__(self) -> None:
        self.name: Optional[str] = None
        self.offset = 0
        self.pending = b""
        self.skipping = False
        self.outcome: Optional[str] = None
        self.earlier: Optional[tuple] = None  # (run id, outcome) of the run before, for its run end line

    def switch(self, name: str, offset: int) -> None:
        if self.name is not None and name != self.name:
            self.earlier = (RUN_OUT.fullmatch(self.name).group(1), self.outcome)
        self.name, self.offset, self.pending, self.skipping, self.outcome = name, offset, b"", False, None


class Feed:
    """One feed. desk None follows every desk. poll returns the new lines, ready to print."""

    def __init__(self, conn, desk: Optional[str]) -> None:
        self.conn = conn
        self.desk = None if desk is None else ids.check("desk", desk)
        self.marks = watch.marks(conn)
        self.tails: dict = {}
        # Per desk, the runs still going that the feed left when another run of the same desk wrote later, by
        # file name. Only a desk with more than one run slot has any.
        self.paused: dict = {}
        self.first = True

    def target(self) -> str:
        if self.desk is None:
            return "every desk"
        return "all owl traffic" if self.desk == OWL_POST_DESK else self.desk

    def poll(self, now: int) -> list:
        lines = self._owls()
        if self.desk != OWL_POST_DESK:
            lines += self._runs(now)
            lines += self._metrics()
        lines += self._events()
        self.first = False
        return lines

    def _prefix(self, desk: str) -> str:
        return f"{desk}: " if self.desk is None else ""

    # store

    def _store_rows(self, read: Callable) -> list:
        """Rows from one store read. A busy or failing read is retried on the next poll."""
        try:
            return read()
        except (StoreError, sqlite3.Error):
            return []

    def _owls(self) -> list:
        desk = None if self.desk == OWL_POST_DESK else self.desk
        rows = self._store_rows(lambda: watch.owls_after(self.conn, self.marks["owls"], desk))
        lines = []
        for row in rows:
            self.marks["owls"] = row["seq"]
            lines.append(line(row["created_at"], f"owl {row['sender']} -> {row['recipient']} {row['kind']}: "
                                                 f"{clip(row['subject'], ARG_MAX_CHARS)}"))
        return lines

    def _events(self) -> list:
        desk = None if self.desk == OWL_POST_DESK else self.desk
        rows = self._store_rows(lambda: watch.headmaster_events_after(self.conn, self.marks["events"], desk))
        lines = []
        for row in rows:
            self.marks["events"] = row["id"]
            if self.desk == OWL_POST_DESK and not (row["desk"] == OWL_POST_DESK
                                                   or row["kind"].startswith(OWL_POST_KIND_PREFIX)):
                continue
            prefix = f"{row['desk']}: " if self.desk in (None, OWL_POST_DESK) else ""
            lines.append(line(row["ts"], f"{prefix}headmaster {row['kind']}: {clip(row['summary'], TEXT_MAX_CHARS)}"))
        return lines

    def _metrics(self) -> list:
        rows = self._store_rows(lambda: watch.metrics_after(self.conn, self.marks["metrics"], self.desk))
        lines = []
        for row in rows:
            self.marks["metrics"] = row["id"]
            ended = None
            tail = self.tails.get(row["desk"])
            paused = self.paused.get(row["desk"], {}).pop(f"{row['run_id']}.out", None)
            if tail is not None and tail.name == f"{row['run_id']}.out":
                lines += self._drain(row["desk"], tail, row["ts"])  # its output first, then its end
                ended = tail.outcome
            elif paused is not None:
                lines += self._drain(row["desk"], paused, row["ts"])
                ended = paused.outcome
            elif tail is not None and tail.earlier is not None and tail.earlier[0] == row["run_id"]:
                ended, tail.earlier = tail.earlier[1], None
            cost = row["cost_usd"] if isinstance(row["cost_usd"], (int, float)) else 0.0
            text = (f"run end {row['run_id']}: model {clip(row['model'], NAME_MAX_CHARS)}, "
                    f"{duration(row['duration_ms'])}, tokens in {row['input_tokens']} out {row['output_tokens']}"
                    f" cache {row['cache_read_tokens']}, ${cost:.2f}")
            if ended is not None:
                text += f", status {ended}"
            # Columns a later migration adds, such as an exit code or a cap or failure label.
            for key, value in row.items():
                if key not in METRIC_FIELDS and value is not None:
                    text += f", {clip(key, NAME_MAX_CHARS)} {clip(value, NAME_MAX_CHARS)}"
            lines.append(line(row["ts"], self._prefix(row["desk"]) + text))
        return lines

    # run files

    def _run_desks(self) -> list:
        if self.desk is not None:
            return [self.desk]
        try:
            with safefs.opened_dir(config.OFFICE_ROOT, "runs") as fd:
                names = os.listdir(fd)
        except (FleetError, OSError):
            return []
        return sorted(name for name in names if ids.PATTERNS["desk"].fullmatch(name))

    def _runs(self, now: int) -> list:
        lines = []
        for desk in self._run_desks():
            try:
                with safefs.opened_dir(config.OFFICE_ROOT, "runs", desk) as fd:
                    lines += self._follow(fd, desk, now)
            except (FleetError, OSError):
                continue  # no runs yet, or the folder is not safe to read
        return lines

    @staticmethod
    def _newest(fd: int) -> Optional[tuple]:
        found = []
        for name in os.listdir(fd):
            if RUN_OUT.fullmatch(name) is None:
                continue
            info = safefs.lstat(fd, name)
            if info is not None and stat.S_ISREG(info.st_mode):
                found.append((info.st_mtime_ns, name, info))
        return max(found, key=lambda item: item[:2])[1:] if found else None

    def _running(self, run_id: str, info: os.stat_result, now: int) -> bool:
        """A run is still going while its file is fresh and run_desk has not recorded its end."""
        if info.st_mtime < now - config.RUN_TIMEOUT_SECONDS - 60:
            return False
        return not self._store_rows(lambda: [True] if watch.run_recorded(self.conn, run_id) else [])

    @staticmethod
    def _slots(desk: str) -> int:
        """How many runs the desk may have going at once (config.RUN_SLOTS), one when that is not a count."""
        count = config.RUN_SLOTS.get(desk, 1)
        return count if type(count) is int and count > 0 else 1

    def _going(self, fd: int, name: str, now: int) -> bool:
        info = safefs.lstat(fd, name)
        return info is not None and stat.S_ISREG(info.st_mode) and \
            self._running(RUN_OUT.fullmatch(name).group(1), info, now)

    def _follow(self, fd: int, desk: str, now: int) -> list:
        newest = self._newest(fd)
        if newest is None:
            return []
        name, info = newest
        run_id = RUN_OUT.fullmatch(name).group(1)
        tail = self.tails.setdefault(desk, RunTail())
        paused = self.paused.setdefault(desk, {})
        for left in [item for item in paused if item != name]:
            gone = safefs.lstat(fd, left)
            if gone is None or gone.st_mtime < now - config.RUN_TIMEOUT_SECONDS - 60:
                del paused[left]  # killed before its end was recorded: nothing more will come
        lines = []
        if name != tail.name:
            if tail.name is None and self.first:
                # The feed just started: follow a run still going from its start, and pass over a finished one.
                if not self._running(run_id, info, now):
                    tail.switch(name, info.st_size)
                    return []
                lines.append(line(now, self._prefix(desk) + f"run in progress {run_id}, shown from its start"))
                tail.switch(name, 0)
            else:
                earlier, was_paused = None, name in paused
                if tail.name is not None and self._slots(desk) > 1 and self._going(fd, tail.name, now):
                    # Another run of this desk wrote later while this one still goes in its own run slot: keep
                    # this one's place, and carry on from there when it writes again.
                    paused[tail.name] = tail
                    tail = self.tails[desk] = RunTail()
                elif tail.name is not None:
                    lines += self._read(fd, desk, tail, now, final=True)
                    lines += self._metrics()  # the old run's end before the new run's start
                    earlier = (RUN_OUT.fullmatch(tail.name).group(1), tail.outcome)
                again = paused.pop(name, None)
                if again is not None:
                    again.earlier = earlier or again.earlier
                    tail = self.tails[desk] = again  # its start was shown when the feed first followed it
                elif was_paused:
                    # Its end came in with the old run's, and the rest of its output was shown with it.
                    ended = safefs.lstat(fd, name)
                    tail.switch(name, info.st_size if ended is None else ended.st_size)
                else:
                    lines.append(line(now, self._prefix(desk) + f"run start {run_id}"))
                    tail.switch(name, 0)
        return lines + self._read(fd, desk, tail, now)

    def _read(self, fd: int, desk: str, tail: RunTail, now: int, final: bool = False) -> list:
        """New complete lines of the run file. A partial last line waits for its newline, unless the
        run is over (final), when it is all there will be."""
        lines = []
        for _ in range(FINAL_READ_CHUNKS if final else 1):
            try:
                data, size = safefs.read_range(fd, tail.name, tail.offset, READ_CHUNK_BYTES, "run output")
            except FleetError:
                return lines
            if size < tail.offset:
                tail.switch(tail.name, 0)  # the file was cut short; read it again from the top
                return lines
            tail.offset += len(data)
            parts = (tail.pending + data).split(b"\n")
            tail.pending = parts.pop()
            if final and not data and tail.pending:
                parts.append(tail.pending)
                tail.pending = b""
            for raw in parts:
                if tail.skipping:
                    tail.skipping = False  # the rest of an event too long to keep
                    continue
                lines += self._render(raw, desk, tail, now)
            if len(tail.pending) > EVENT_MAX_BYTES:
                tail.pending, tail.skipping = b"", True
            if not data:
                break
        return lines

    def _drain(self, desk: str, tail: RunTail, now: int) -> list:
        """The rest of a run that has ended. A run far behind skips to its last chunk, where its result is."""
        try:
            with safefs.opened_dir(config.OFFICE_ROOT, "runs", desk) as fd:
                lines = self._read(fd, desk, tail, now, final=True)
                info = safefs.lstat(fd, tail.name)
                if info is None or tail.offset >= info.st_size:
                    return lines
                start = max(tail.offset, info.st_size - READ_CHUNK_BYTES)
                lines.append(line(now, self._prefix(desk) + f"skipped {start - tail.offset} bytes of run output"))
                tail.offset, tail.pending, tail.skipping = start, b"", True  # the cut first line is dropped
                return lines + self._read(fd, desk, tail, now, final=True)
        except (FleetError, OSError):
            return []

    def _render(self, raw: bytes, desk: str, tail: RunTail, now: int) -> list:
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            return []
        if not isinstance(event, dict):
            return []
        ended = outcome(event)
        if ended is not None:
            tail.outcome = ended
        return [line(now, self._prefix(desk) + text) for text in render_event(event)]


def follow(desk: Optional[str], out=None, clock: Callable = time.time, sleep: Callable = time.sleep,
           polls: Optional[int] = None) -> int:
    """Print the feed until Ctrl+C. Tests pass a clock, a sleep and a number of polls."""
    out = sys.stdout if out is None else out
    try:
        conn = db.connect_readonly(config.DB_PATH)
    except StoreError as exc:
        out.write(f"fleet feed: {clip(exc, 200)}\n")
        return 1
    try:
        feed = Feed(conn, desk)
        out.write(line(int(clock()), f"watching {feed.target()}, read-only. Ctrl+C stops.") + "\n")
        out.flush()
        count = 0
        while polls is None or count < polls:
            for text in feed.poll(int(clock())):
                out.write(text + "\n")
            out.flush()
            count += 1
            if polls is None or count < polls:
                sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    except (StoreError, sqlite3.Error) as exc:
        out.write(f"fleet feed: {clip(exc, 200)}\n")
        return 1
    finally:
        conn.close()
    return 0
