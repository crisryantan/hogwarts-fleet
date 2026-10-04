"""File access that refuses symlinks, hard links and loose permissions.

Desks can write inside the castle, and these scripts run outside every sandbox,
so every castle path is opened one component at a time with O_NOFOLLOW from a
fixed root. A desk cannot swap a folder or file for a link and steer a write or
read somewhere else.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import stat
import time
from typing import Iterator, Optional

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Read-only, since a run hands its lock fds to the desk process it starts (see run_desk._launch).
LOCK_FLAGS = os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC

_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,255}")


class FleetError(Exception):
    """A fleet script refused something. The message never carries file content."""


class Missing(FleetError):
    pass


class Unsafe(FleetError):
    pass


class Busy(FleetError):
    pass


def check_component(name: str) -> str:
    if not isinstance(name, str) or _COMPONENT.fullmatch(name) is None or name in (".", ".."):
        raise Unsafe("path component is not allowed")
    return name


def _check_owned(st: os.stat_result, label: str) -> None:
    if st.st_uid != os.getuid():
        raise Unsafe(f"{label} is not owned by the current user")
    if st.st_mode & 0o022:
        raise Unsafe(f"{label} is group or world writable")


def _open_component(parent_fd: int, name: str, label: str, create: bool, mode: int) -> int:
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise Missing(f"{label} does not exist") from None
        try:
            os.mkdir(name, mode, dir_fd=parent_fd)
        except FileExistsError:
            pass
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except (NotADirectoryError, OSError) as exc:
        # ELOOP for a symlink, ENOTDIR for a file.
        raise Unsafe(f"{label} is not a plain directory") from exc
    return fd


def open_root(path: str) -> int:
    """Open an absolute directory with no symlink anywhere on the way down."""
    if not isinstance(path, str) or not path.startswith("/") or os.path.normpath(path) != path or path == "/":
        raise Unsafe("root must be an absolute normalised path")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.strip("/").split("/"):
            child = _open_component(fd, part, "root path", create=False, mode=0o700)
            os.close(fd)
            fd = child
        _check_owned(os.fstat(fd), "root folder")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir(root: str, *parts: str, create: bool = False, mode: int = 0o700) -> int:
    """Open root/parts... as a directory fd. Each part must be a plain directory owned by us."""
    fd = open_root(root)
    try:
        for part in parts:
            check_component(part)
            child = _open_component(fd, part, part, create, mode)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise Unsafe(f"{part} is not a directory")
            _check_owned(st, part)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def opened_dir(root: str, *parts: str, create: bool = False) -> Iterator[int]:
    fd = open_dir(root, *parts, create=create)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_regular(st: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise Unsafe(f"{label} is not a regular file")
    if st.st_nlink != 1:
        raise Unsafe(f"{label} has more than one hard link")
    _check_owned(st, label)


def lstat(dir_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def read_regular(dir_fd: int, name: str, max_bytes: int, label: str = "file") -> bytes:
    try:
        fd = os.open(name, READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise Missing(f"{label} does not exist") from None
    except OSError as exc:
        raise Unsafe(f"{label} is a symlink or cannot be opened") from exc
    try:
        st = os.fstat(fd)
        _check_regular(st, label)
        if st.st_size > max_bytes:
            raise Unsafe(f"{label} is larger than {max_bytes} bytes")
        chunks, total = [], 0
        while total <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            raise Unsafe(f"{label} is larger than {max_bytes} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_range(dir_fd: int, name: str, offset: Optional[int], max_bytes: int, label: str = "file") -> tuple:
    """Up to max_bytes from offset of a plain file, and its size. An offset of None reads the tail."""
    try:
        fd = os.open(name, READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise Missing(f"{label} does not exist") from None
    except OSError as exc:
        raise Unsafe(f"{label} is a symlink or cannot be opened") from exc
    try:
        st = os.fstat(fd)
        _check_regular(st, label)
        start = max(0, st.st_size - max_bytes) if offset is None else min(offset, st.st_size)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, total = [], 0
        while total < max_bytes:
            chunk = os.read(fd, min(65536, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), st.st_size
    finally:
        os.close(fd)


def is_safe_regular(dir_fd: int, name: str) -> bool:
    st = lstat(dir_fd, name)
    if st is None:
        return False
    try:
        _check_regular(st, name)
    except Unsafe:
        return False
    return True


def write_new(dir_fd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    """Write a file atomically: a fresh temp file, then rename over the target name."""
    check_component(name)
    temp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, NEW_FLAGS, mode, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(temp, dir_fd=dir_fd)
        raise
    os.close(fd)
    os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def open_append(dir_fd: int, name: str, label: str = "file", mode: int = 0o600) -> int:
    """Open (or create) a plain file for appending. The caller closes the fd."""
    check_component(name)
    try:
        fd = os.open(name, APPEND_FLAGS, mode, dir_fd=dir_fd)
    except OSError as exc:
        raise Unsafe(f"{label} is a symlink or cannot be opened") from exc
    try:
        _check_regular(os.fstat(fd), label)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def append_regular(dir_fd: int, name: str, data: bytes, label: str = "file", mode: int = 0o600) -> None:
    fd = open_append(dir_fd, name, label, mode)
    try:
        write_all(fd, data)
    finally:
        os.close(fd)


def create_new(dir_fd: int, name: str, mode: int = 0o600) -> int:
    """Create a file that must not exist yet. The caller closes the fd."""
    check_component(name)
    fd = os.open(name, NEW_FLAGS, mode, dir_fd=dir_fd)
    os.fchmod(fd, mode)
    return fd


def move(src_dir_fd: int, name: str, dst_dir_fd: int, new_name: str) -> None:
    check_component(new_name)
    os.rename(name, new_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def _take_lock(fd: int, blocking: bool, timeout: Optional[float], shared: bool = False) -> None:
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if blocking and timeout is None:
        fcntl.flock(fd, mode)
        return
    deadline = time.monotonic() + (timeout or 0)
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if not blocking or time.monotonic() >= deadline:
                raise Busy("another run holds this lock") from None
            time.sleep(0.5)


@contextlib.contextmanager
def held_lock(dir_fd: int, name: str, blocking: bool, timeout: Optional[float] = None,
              shared: bool = False) -> Iterator[int]:
    """An exclusive lock, or a shared one that only an exclusive holder excludes, yielding its fd. blocking
    with a timeout waits at most that long, then raises Busy. A process that inherits the fd keeps the lock
    held after this one dies without unlocking it."""
    check_component(name)
    fd = os.open(name, LOCK_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe("lock is not a regular file")
        _check_owned(st, "lock")
        _take_lock(fd, blocking, timeout, shared)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
