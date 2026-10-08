"""Scratchpad rotation: a desk's scratchpad, or one of its task pads, keeps only its latest Checkpoint block.

A Checkpoint block starts at a markdown heading whose text begins with "Checkpoint" and has something under it, and
runs to the next Checkpoint heading or the next heading at its own level or above. Everything else stays where it is:
the Now and Notes sections, the bare "## Checkpoint" heading, and the latest block. The older blocks are appended to a
monthly file in config.SCRATCHPAD_ARCHIVE_DIR next to the file (<YYYY-MM>.md for a scratchpad, <key>-<YYYY-MM>.md for
a pad). The archive is written and fsynced before the live file is replaced through a temp file and a rename, so a
rotation cut off part way can repeat a block in the archive but never loses one.

A rotation holds the desk's scratchpad lock, opens every file one folder at a time with no link followed, and refuses
a file that is not a plain, singly linked file of the current user's. It streams the file twice, once to split it into
blocks and once to copy them, holding at most SCRATCHPAD_READ_MAX_BYTES of one line in memory, and leaves a file over
SCRATCHPAD_ROTATE_MAX_BYTES as it is with a warning. A file written in the last SCRATCHPAD_QUIET_SECONDS, or written
while it is being rotated, is left as it is this time. The old file stays open through the rename: if a desk wrote to it
in that last moment after all, its whole content is copied to a recovered-<timestamp>.md file in the archive, with a
warning. A rotation killed between its rename and that copy loses such a write; it takes a write in that moment to
matter.
The SessionStart and PreCompact hooks rotate the interactive desk's scratchpad, and run_desk rotates a headless desk's
scratchpad, its shared pads and the run's pad before its launch.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import time
from typing import Iterator, Optional

from . import config, safefs
from .safefs import Missing

SCRATCHPAD = "scratchpad.md"
COPY_CHUNK_BYTES = 65536
_HEADING = re.compile(rb"(#{1,6})[ \t]+([^\r\n]*)\r?\n?")
_CHECKPOINT = re.compile(rb"checkpoint\b", re.IGNORECASE)
_FENCE = re.compile(rb" {0,3}(`{3,}|~{3,})")


def lock_name(desk: str) -> str:
    return f"scratchpad-{safefs.check_component(desk)}.lock"


@contextlib.contextmanager
def desk_lock(desk: str) -> Iterator[None]:
    """The desk's scratchpad lock, waited for at most SCRATCHPAD_LOCK_WAIT_SECONDS, then safefs.Busy."""
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as fd, \
            safefs.held_lock(fd, lock_name(desk), blocking=True, timeout=config.SCRATCHPAD_LOCK_WAIT_SECONDS):
        yield


class _Splitter:
    """Splits a file fed to it a piece at a time into [kind, level, start, end, has body] runs: kind "checkpoint" for
    a Checkpoint block, else "keep". A heading inside a code fence is text. A line longer than
    SCRATCHPAD_READ_MAX_BYTES is text from there on, so memory stays bounded."""

    def __init__(self) -> None:
        self.runs: list = []
        self.fence = None  # the open fence's marker character and length
        self.offset = 0
        self.partial = b""
        self.overlong = False

    def feed(self, data: bytes) -> None:
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            self._line(line + b"\n", not self.overlong)
            self.overlong = False
        if len(self.partial) > config.SCRATCHPAD_READ_MAX_BYTES:
            self._line(self.partial, not self.overlong)
            self.partial, self.overlong = b"", True

    def finish(self) -> list:
        if self.partial:
            self._line(self.partial, not self.overlong)
            self.partial = b""
        for run in self.runs:  # a Checkpoint heading with nothing under it is the section heading, not a block
            if run[0] == "checkpoint" and not run[4]:
                run[0] = "keep"
        return self.runs

    def _line(self, line: bytes, starts: bool) -> None:
        heading = _HEADING.fullmatch(line) if starts and self.fence is None else None
        runs = self.runs
        if heading is not None:
            level = len(heading.group(1))
            if _CHECKPOINT.match(heading.group(2)):
                runs.append(["checkpoint", level, self.offset, self.offset, False])
            elif runs and runs[-1][0] == "checkpoint" and level <= runs[-1][1]:
                runs.append(["keep", 0, self.offset, self.offset, False])
        if not runs:
            runs.append(["keep", 0, self.offset, self.offset, False])
        if heading is None and line.strip():
            runs[-1][4] = True
        marker = _FENCE.match(line) if starts else None
        if marker is not None:  # a fence closes only on its own character, at least as long, with nothing after
            run = marker.group(1)
            if self.fence is None:
                self.fence = (run[:1], len(run))
            elif run[:1] == self.fence[0] and len(run) >= self.fence[1] and not line[marker.end():].strip():
                self.fence = None
        self.offset += len(line)
        runs[-1][3] = self.offset


def segments(data: bytes) -> list:
    """data as [kind, level, start, end, has body] runs in order (see _Splitter)."""
    splitter = _Splitter()
    splitter.feed(data)
    return splitter.finish()


def _target(desk: str, key: Optional[str]) -> tuple:
    """(folder parts under the castle, file name, archive file stem, label) of a scratchpad, or of the pad key."""
    desk = safefs.check_component(desk)
    if key is None:
        return ("desks", desk), SCRATCHPAD, "", f"{desk}'s scratchpad"
    key = safefs.check_component(key)
    return ("desks", desk, config.PADS_DIR), f"{key}.md", f"{key}-", f"{desk}'s pad {key}"


def _same(before: os.stat_result, after: Optional[os.stat_result]) -> bool:
    return after is not None and stat.S_ISREG(after.st_mode) and after.st_nlink == 1 and (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))


def _stream(fd: int, end: int, offset: int = 0) -> Iterator[bytes]:
    """Bytes offset to end of fd, a chunk at a time."""
    while offset < end:
        chunk = os.pread(fd, min(COPY_CHUNK_BYTES, end - offset), offset)
        if not chunk:
            raise safefs.Unsafe("the file got shorter while it was read")
        offset += len(chunk)
        yield chunk


def _append_archive(dir_fd: int, name: str, header: bytes, pieces) -> None:
    """Append header and pieces to the archive file, then fsync it, its folder and the folder holding that, so it is on
    disk and reachable before the live file changes. The folder is kept 0700 and the file 0600."""
    archive_fd = safefs.open_subdir(dir_fd, config.SCRATCHPAD_ARCHIVE_DIR, create=True)
    try:
        os.fchmod(archive_fd, 0o700)
        fd = safefs.open_append(archive_fd, name, "scratchpad archive")
        try:
            os.fchmod(fd, 0o600)
            safefs.write_all(fd, header)
            last = b"\n"
            for piece in pieces:
                if piece:
                    safefs.write_all(fd, piece)
                    last = piece[-1:]
            if last != b"\n":
                safefs.write_all(fd, b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(archive_fd)
        os.fsync(dir_fd)
    finally:
        os.close(archive_fd)


def _recover(dir_fd: int, old_fd: int, stem: str, now: int) -> str:
    """Copy the old file, still open after the rename, to a new recovered-<stem><timestamp>.md in the archive folder
    and fsync it: at most SCRATCHPAD_ROTATE_MAX_BYTES, with a note when it was cut. Returns the file's name."""
    limit = config.SCRATCHPAD_ROTATE_MAX_BYTES
    size = os.fstat(old_fd).st_size
    archive_fd = safefs.open_subdir(dir_fd, config.SCRATCHPAD_ARCHIVE_DIR, create=True)
    try:
        os.fchmod(archive_fd, 0o700)
        name = f"recovered-{stem}{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}.md"
        try:
            fd = safefs.create_new(archive_fd, name)
        except FileExistsError:
            name = name[:-3] + f"-{secrets.token_hex(4)}.md"
            fd = safefs.create_new(archive_fd, name)
        try:
            for chunk in _stream(old_fd, min(size, limit)):
                safefs.write_all(fd, chunk)
            if size > limit:
                safefs.write_all(fd, f"\n(cut at {limit} of {size} bytes)\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(archive_fd)
        os.fsync(dir_fd)
    finally:
        os.close(archive_fd)
    return name


def _replace(dir_fd: int, name: str, pieces, before: os.stat_result, old_fd: int, stem: str, now: int) -> tuple:
    """Replace name with pieces through a fsynced temp file, checking just before the rename that nothing wrote the
    live file since it was read: (False, None), with the temp file gone and the live file untouched, when something
    had. After the rename the old file, held open through it, is looked at once more: if a desk wrote to it after all,
    its whole content is copied to a recovered file in the archive (_recover) and (True, that file's name) comes back.
    Nothing is renamed again, so the live file keeps whatever is in it. The copy is tried on any failure after the
    rename too, before the old file is let go."""
    temp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = safefs.create_new(dir_fd, temp, stat.S_IMODE(before.st_mode))
    try:
        try:
            for piece in pieces:
                safefs.write_all(fd, piece)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not _same(before, safefs.lstat(dir_fd, name)):
            os.unlink(temp, dir_fd=dir_fd)
            return False, None
        safefs.move(dir_fd, temp, dir_fd, name)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=dir_fd)
        raise
    try:
        os.fsync(dir_fd)
        after = os.fstat(old_fd)
        if (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
            return True, None
        return True, _recover(dir_fd, old_fd, stem, now)
    except BaseException:
        with contextlib.suppress(Exception):
            _recover(dir_fd, old_fd, stem, now)
        raise


def _over_budget(label: str, kept: int, latest: int) -> Optional[str]:
    budget = config.SCRATCHPAD_BUDGET_BYTES
    if kept <= budget:
        return None
    if latest > budget:
        return (f"The latest Checkpoint in {label} is {latest} bytes, over its {budget} byte budget, so it was kept "
                "whole. Keep the next one shorter.")
    return (f"{label} is {kept} bytes with only its latest Checkpoint left, over its {budget} byte budget. Move "
            "durable notes to TASK.md or the memory store.")


def _changed(result: dict, size: int) -> dict:
    """A file written while it was being rotated is left as it is this time, with nothing to say: the next rotation
    takes it."""
    result.update(archived=0, bytes=size, warning=None)
    return result


def _ranges(fd: int, ranges: list) -> Iterator[bytes]:
    for start, end in ranges:
        yield from _stream(fd, end, start)


def _rotate_open(dir_fd: int, name: str, stem: str, label: str, now: int) -> dict:
    result = {"file": label, "archived": 0, "bytes": 0, "warning": None}
    fd = safefs.open_regular(dir_fd, name, label)
    try:
        before = os.fstat(fd)
        size = result["bytes"] = before.st_size
        if abs(now - before.st_mtime) < config.SCRATCHPAD_QUIET_SECONDS:
            return result  # written a moment ago, so its desk may still be writing: the next rotation takes it
        if size > config.SCRATCHPAD_ROTATE_MAX_BYTES:
            result["warning"] = (f"{label} is {size} bytes, over the {config.SCRATCHPAD_ROTATE_MAX_BYTES} bytes a "
                                 "rotation reads, so it was left as it is. Trim it by hand.")
            return result
        splitter = _Splitter()
        for chunk in _stream(fd, size):  # all of it or a refusal, so a short read never replaces the file
            splitter.feed(chunk)
        runs = splitter.finish()
        blocks = [run for run in runs if run[0] == "checkpoint"]
        latest = blocks[-1][3] - blocks[-1][2] if blocks else 0
        if len(blocks) < 2:
            result["warning"] = _over_budget(label, size, latest)
            return result
        older = blocks[:-1]
        kept = [(run[2], run[3]) for run in runs if run not in older]
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
        archive = f"{stem}{time.strftime('%Y-%m', time.gmtime(now))}.md"
        if not _same(before, safefs.lstat(dir_fd, name)):
            return _changed(result, size)
        _append_archive(dir_fd, archive, f"\n## Archived from {name} at {stamp}\n\n".encode("ascii"),
                        _ranges(fd, [(run[2], run[3]) for run in older]))
        replaced, recovered = _replace(dir_fd, name, _ranges(fd, kept), before, fd, stem, now)
        if not replaced:  # the archive may repeat these blocks; none is lost
            return _changed(result, size)
        result["archived"] = len(older)
        result["bytes"] = sum(end - start for start, end in kept)
        if recovered:
            result["warning"] = (f"{label} was written while it was rotated; its whole content then is in "
                                 f"{config.SCRATCHPAD_ARCHIVE_DIR}/{recovered}, so copy back anything the live file "
                                 "is missing.")
        else:
            result["warning"] = _over_budget(label, result["bytes"], latest)
        return result
    finally:
        os.close(fd)


def rotate_held(desk: str, now: int, key: Optional[str] = None) -> dict:
    """Rotate the desk's scratchpad, or its pad key, while the caller holds desk_lock(desk). A desk folder or file
    that is not there has nothing to rotate. Returns the file's label, how many blocks were archived, its size after,
    and one warning line or None."""
    parts, name, stem, label = _target(desk, key)
    try:
        with safefs.opened_dir(config.CASTLE_ROOT, *parts) as dir_fd:
            return _rotate_open(dir_fd, name, stem, label, now)
    except Missing:
        return {"file": label, "archived": 0, "bytes": 0, "warning": None}


def rotate(desk: str, now: int, key: Optional[str] = None) -> dict:
    """rotate_held under the desk's scratchpad lock."""
    with desk_lock(desk):
        return rotate_held(desk, now, key)
