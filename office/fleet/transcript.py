"""Read Claude Code session transcripts (JSONL) for the hooks.

Only the path Claude Code passes in the hook input is opened, and only when it
sits under TRANSCRIPTS_ROOT with no symlink on the way. Entries are parsed as
data. Nothing in them is executed or followed.

Fields used, all optional and checked for type:
- type: "user" or "assistant"; isSidechain, isMeta, isCompactSummary, toolUseResult
- message.content (text or a list of blocks), message.id, message.model, message.usage
- origin.kind ("human" on typed prompts, "peer" on text another session sent in),
  promptSource ("system" on injected turns), promptId, entrypoint, timestamp
"""
from __future__ import annotations

import calendar
import json
import os
import stat
import time
from typing import Iterator, Optional

from hogwarts import ids
from hogwarts.errors import StoreError

from . import config
from .safefs import Missing, Unsafe

_SKIP_TEXT_PREFIXES = (
    "<command-", "<local-command-", "<system-reminder>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "<task-notification>", "<user-memory-input>",
)


def open_transcript(path: object) -> int:
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        raise Unsafe("transcript path is not a .jsonl file")
    try:
        ids.check_absolute(path, "transcript path")
    except StoreError:
        raise Unsafe("transcript path is not absolute and normalised") from None
    root = config.TRANSCRIPTS_ROOT
    if not path.startswith(root + "/"):
        raise Unsafe("transcript is outside the transcripts folder")
    if os.path.realpath(path) != path:
        raise Unsafe("transcript path goes through a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise Missing("transcript does not exist") from None
    except OSError as exc:
        raise Unsafe("transcript cannot be opened") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise Unsafe("transcript is not a regular file owned by the current user")
    return fd


def _parse(line: bytes) -> Optional[dict]:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def iter_entries(path: object, conversation_only: bool = False) -> Iterator[dict]:
    """Every entry in file order. conversation_only skips tool output lines without parsing them."""
    fd = open_transcript(path)
    with os.fdopen(fd, "rb") as handle:
        for line in handle:
            if conversation_only and (b'"toolUseResult"' in line
                                      or (b'"user"' not in line and b'"assistant"' not in line)):
                continue
            entry = _parse(line)
            if entry is not None:
                yield entry


def tail_entries(path: object, max_bytes: Optional[int] = None) -> list:
    """Entries from the last max_bytes of the file, in file order."""
    limit = config.TRANSCRIPT_TAIL_BYTES if max_bytes is None else max_bytes
    fd = open_transcript(path)
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, remaining = [], size - start
        while remaining > 0:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    lines = b"".join(chunks).split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # the first line is cut part way
    return [entry for entry in (_parse(line) for line in lines if line.strip()) if entry is not None]


def _main_chain(entry: dict) -> bool:
    return entry.get("isSidechain") is not True


def _message(entry: dict) -> dict:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def usage_total(entry: dict) -> Optional[int]:
    """Context carried by one assistant call: input plus cache read plus cache creation."""
    usage = _message(entry).get("usage")
    if not isinstance(usage, dict):
        return None
    return (_int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
            + _int(usage.get("cache_creation_input_tokens")))


def last_usage(entries: list) -> Optional[int]:
    for entry in reversed(entries):
        if entry.get("type") == "assistant" and _main_chain(entry):
            total = usage_total(entry)
            if total is not None:
                return total
    return None


def entrypoints(entries: list) -> set:
    """Every entrypoint value the entries name. A value that is not text counts as a distinct one."""
    found = set()
    for entry in entries:
        if "entrypoint" in entry:
            value = entry["entrypoint"]
            found.add(value if isinstance(value, str) and value else "<invalid>")
    return found


def _is_tool_result(entry: dict) -> bool:
    content = _message(entry).get("content")
    return "toolUseResult" in entry or (isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content))


def prompt_entry(entries: list, prompt_id: str) -> Optional[dict]:
    """The user entry that carries this prompt id and is not a tool result, if the tail has one."""
    for entry in entries:
        if entry.get("type") == "user" and entry.get("promptId") == prompt_id and not _is_tool_result(entry):
            return entry
    return None


def is_typed_prompt(entry: dict) -> bool:
    """True only for a prompt a person typed: origin.kind human, not meta, not a system-sourced turn."""
    origin = entry.get("origin")
    return (entry.get("type") == "user" and _main_chain(entry)
            and isinstance(origin, dict) and origin.get("kind") == "human"
            and entry.get("isMeta") is not True and entry.get("promptSource") != "system"
            and entry.get("isCompactSummary") is not True and entry.get("isVisibleInTranscriptOnly") is not True
            and not _is_tool_result(entry))


def timestamp(entry: dict) -> Optional[int]:
    value = entry.get("timestamp")
    if not isinstance(value, str) or len(value) < 20:
        return None
    try:
        parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(parsed)


def _user_text(entry: dict) -> Optional[str]:
    """What Ryan typed, or None for tool output, injected context and anything not typed."""
    if entry.get("type") != "user" or not _main_chain(entry):
        return None
    if entry.get("isMeta") is True or entry.get("isCompactSummary") is True:
        return None
    if entry.get("isVisibleInTranscriptOnly") is True or "toolUseResult" in entry:
        return None
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = _message(entry).get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return None
        parts = [block.get("text") for block in content
                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    else:
        return None
    kept = [part.strip() for part in parts if part.strip() and not part.lstrip().startswith(_SKIP_TEXT_PREFIXES)]
    return "\n".join(kept) if kept else None


def _assistant_text(entry: dict) -> Optional[str]:
    content = _message(entry).get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [block.get("text") for block in content
             if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    text = "\n".join(part.strip() for part in parts if part.strip())
    return text or None


class _Scan:
    """One pass over the entries: Ryan's prompts, final replies and session numbers."""

    def __init__(self) -> None:
        self.pairs: list = []
        self.reply_id = None
        self.reply: list = []
        self.seen: set = set()
        self.stats = {"started_at": None, "ended_at": None, "model": None, "first_turn_tokens": None,
                      "total_input_tokens": 0}

    def flush(self) -> None:
        if self.reply:
            self.pairs.append(("assistant", "\n".join(self.reply)))
        self.reply_id, self.reply = None, []

    def feed(self, entry: dict) -> None:
        stamp = timestamp(entry)
        if stamp is not None:
            if self.stats["started_at"] is None:
                self.stats["started_at"] = stamp
            self.stats["ended_at"] = stamp
        kind = entry.get("type")
        if kind == "user":
            text = _user_text(entry)
            if text is not None:
                self.flush()
                self.pairs.append(("user", text))
        elif kind == "assistant" and _main_chain(entry):
            self._usage(entry)
            text = _assistant_text(entry)
            if text is None:
                return
            message_id = _message(entry).get("id")
            if message_id is not None and message_id == self.reply_id:
                self.reply.append(text)
            else:
                self.reply_id, self.reply = message_id, [text]

    def _usage(self, entry: dict) -> None:
        message = _message(entry)
        if isinstance(message.get("model"), str) and not message["model"].startswith("<"):
            self.stats["model"] = message["model"]
        total = usage_total(entry)
        key = message.get("id")
        if total is None or (key is not None and key in self.seen):
            return
        if key is not None:
            self.seen.add(key)
        if self.stats["first_turn_tokens"] is None:
            self.stats["first_turn_tokens"] = total
        self.stats["total_input_tokens"] += total


def summarize(entries) -> tuple:
    """(pairs, stats): Ryan's prompts and each turn's final reply in order, plus session numbers."""
    scan = _Scan()
    for entry in entries:
        scan.feed(entry)
    scan.flush()
    return scan.pairs, scan.stats


def conversation(entries) -> list:
    return summarize(entries)[0]


def session_stats(entries) -> dict:
    return summarize(entries)[1]
