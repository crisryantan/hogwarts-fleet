"""Gringotts - Backup: a local backup, daily at 23:30, and its restore drill. No model.

The backup is one gzip tarball, mode 0600, in the office backups folder (~/.hogwarts/backups). Nothing syncs
that folder anywhere, and this script never copies an archive anywhere else. It holds:
- claude/: from ~/.claude, the settings files, CLAUDE.md, keybindings.json, the agents, commands, skills,
  hooks and output-styles folders, and each project's memory folder;
- codex/: from ~/.codex, config.toml, AGENTS.md and the prompts and rules folders;
- office/: the office except its logs, runs, locks, backups and state folders, with a consistent copy of the
  store database (office/state/pensieve.db) in place of the live state folder;
- castle/: the castle except its git worktrees;
- MANIFEST.json, last: each file's size and sha256, and what was left out.

Left out everywhere: any file or folder named like a credential, token or auth file (CREDENTIAL_NAMES), git
metadata at any depth (anything named .git, and any folder laid out like one, with HEAD, objects and refs),
links, files with more than one hard link, anything that isn't a plain file or folder, and files over
BACKUP_FILE_MAX_BYTES. Git metadata goes because a credential once committed stays in its objects even after
the working file is scrubbed.

In every settings or MCP JSON file, each string under a key named env or headers, or named like a token,
secret, password, key, auth or cookie (SECRET_KEY), at any depth, is blanked. In config.toml, a value is
blanked when any part of its full dotted path, the table header and the dotted key together, is such a name,
and comments are dropped. The TOML scrubber reads only plain one-line forms. When it meets one it does not
fully understand, such as a multi-line string or array, an inline table it would keep, an odd table header
or a quoted key with an escape, it leaves the whole file out. A file that ought to be scrubbed but can't be
is left out, never kept as it was.

Archives older than BACKUP_KEEP_DAYS are deleted, always keeping the newest one.

The restore drill (--drill [ARCHIVE]) takes the newest archive, or the one named, and restores it into a
fresh folder inside the backups folder, never over the live folders. It checks the archive's own mode, that
every entry is a plain file with a plain relative name, every file against the manifest, that nothing named
like a credential and no git metadata is inside, that every scrubbed JSON and TOML file has no secret-named
value left and that the database copy passes SQLite's integrity check. Then it removes the folder.

In shadow mode a failure only reaches the job's log. Once Ryan removes the patrol shadow file, a failed
backup or drill is also a headmaster event.

Run it with the wrapper line, adding --drill for the drill:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.gringotts import main; sys.exit(main())'
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import time
from typing import Iterator, Optional
from urllib.parse import quote

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import db  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, patrol, safefs  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

DAY = 86400
ARCHIVE = re.compile(r"gringotts-([0-9]{8}-[0-9]{6})\.tar\.gz")
MANIFEST = "MANIFEST.json"
DB_MEMBER = "office/state/pensieve.db"
LOCK = "gringotts.lock"
LOCK_WAIT_SECONDS = 120
CREDENTIAL_NAMES = (
    ".credentials.json", "*credential*", "auth.json", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", "id_dsa*", ".netrc", ".npmrc", ".pypirc", ".env", ".env.*", "hosts.yml", "*token*.json",
    "*secret*", "*.keychain*",
)
SKIPPED_NAMES = ("__pycache__", ".DS_Store")
# The top-level names taken from ~/.claude and ~/.codex, and the ones left out of the office and the castle.
CLAUDE_TAKE = ("settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json", "agents", "commands",
               "skills", "hooks", "output-styles")
CODEX_TAKE = ("config.toml", "AGENTS.md", "prompts", "rules")
OFFICE_SKIP = ("logs", "runs", "locks", "state", config.BACKUP_DIR)
CASTLE_SKIP = ("worktrees",)
SCRUB_MAX_BYTES = 1024 * 1024
SECRET_KEY = re.compile(r"token|secret|password|passwd|passphrase|api[_-]?key|private[_-]?key|access[_-]?key"
                        r"|(?:^|[_-])key$|bearer|credential|auth|cookie", re.IGNORECASE)
# Keys whose whole value is blanked, whatever is under them.
BLANKED_KEYS = ("env", "headers", "http_headers", "env_http_headers")
GIT_DIR_PARTS = ("HEAD", "objects", "refs")
# The plain one-line TOML forms the scrubber reads. A quoted key with a backslash is not one of them.
_TOML_PART = r"(?:[A-Za-z0-9_-]+|\"[^\"\\\x00-\x1f\x7f]*\"|'[^'\x00-\x1f\x7f]*')"
TOML_PART = re.compile(_TOML_PART)
TOML_DOTTED = re.compile(rf"{_TOML_PART}(?:[ \t]*\.[ \t]*{_TOML_PART})*")
TOML_HEADER = re.compile(rf"(\[\[?)[ \t]*({TOML_DOTTED.pattern})[ \t]*(\]\]?)")
TOML_STRING = re.compile(r"\"(?:[^\"\\\x00-\x08\x0a-\x1f\x7f]|\\.)*\"|'[^'\x00-\x08\x0a-\x1f\x7f]*'")
TOML_BARE = re.compile(r"[A-Za-z0-9_.:+-]+")
TOML_SCALAR = re.compile(
    r"true|false|[+-]?(?:inf|nan)|0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+"
    r"|[+-]?[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})?)?"
    r"|[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?")
TOML_BLANKS = ('""', "''", "[]", "{}")


class Problem(FleetError):
    """The backup could not be made, or the drill found something wrong."""


def credential_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in CREDENTIAL_NAMES)


def secret_name(key: str) -> bool:
    """A key whose value is blanked whole: env or headers, or named like a credential."""
    return key.lower() in BLANKED_KEYS or SECRET_KEY.search(key) is not None


def _blank(node):
    """Every string under node made empty. Numbers, booleans and nulls hold no credential and stay."""
    if isinstance(node, str):
        return ""
    if isinstance(node, dict):
        return {key: _blank(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_blank(item) for item in node]
    return node


def _has_text(node) -> bool:
    if isinstance(node, str):
        return node != ""
    if isinstance(node, dict):
        return any(_has_text(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_text(item) for item in node)
    return False


def _parse_json(data: bytes):
    try:
        return json.loads(data.decode("utf-8")), True
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, False


def _scrub_json(data: bytes) -> Optional[bytes]:
    """Every value under a secret-named key (secret_name), at any depth, blanked. None when the file is not
    JSON."""
    parsed, ok = _parse_json(data)
    if not ok:
        return None

    def walk(node):
        if isinstance(node, dict):
            return {key: _blank(value) if secret_name(key) else walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    try:
        return (json.dumps(walk(parsed), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except RecursionError:
        return None


def _json_left(data: bytes) -> bool:
    """True when a restored JSON file still has text under a secret-named key, or does not parse."""
    parsed, ok = _parse_json(data)
    if not ok:
        return True

    def left(node) -> bool:
        if isinstance(node, dict):
            return any((secret_name(key) and _has_text(value)) or left(value) for key, value in node.items())
        if isinstance(node, list):
            return any(left(item) for item in node)
        return False

    try:
        return left(parsed)
    except RecursionError:
        return True


def _toml_path_secret(path: list) -> bool:
    """True when any part of a full TOML path is secret-named, or is the set table of shell_environment_policy."""
    return any(secret_name(part) or (part == "set" and index and path[index - 1] == "shell_environment_policy")
               for index, part in enumerate(path))


def _toml_parts(dotted: str) -> list:
    return [part[1:-1] if part[0] in "\"'" else part for part in TOML_PART.findall(dotted)]


def _skip_blank(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def _toml_value(text: str, pos: int, depth: int = 0) -> Optional[tuple]:
    """Where the one-line TOML value at pos ends, and whether it holds an inline table. None for a value the
    scrubber does not fully understand: a multi-line string, an array or table that does not close on this
    line, or a bare word that is not a TOML number, boolean, date or time."""
    if depth > 16 or text.startswith(('"""', "'''"), pos):
        return None
    match = TOML_STRING.match(text, pos)
    if match is not None:
        return match.end(), False
    match = TOML_BARE.match(text, pos)
    if match is not None:
        return (match.end(), False) if TOML_SCALAR.fullmatch(match.group()) else None
    opener = text[pos:pos + 1]
    if opener not in ("[", "{"):
        return None
    closer, tables, pos = ("]" if opener == "[" else "}"), opener == "{", _skip_blank(text, pos + 1)
    while not text.startswith(closer, pos):
        if opener == "{":
            key = TOML_DOTTED.match(text, pos)
            if key is None:
                return None
            pos = _skip_blank(text, key.end())
            if not text.startswith("=", pos):
                return None
            pos = _skip_blank(text, pos + 1)
        found = _toml_value(text, pos, depth + 1)
        if found is None:
            return None
        pos, inner = found[0], found[1]
        tables = tables or inner
        pos = _skip_blank(text, pos)
        if text.startswith(",", pos):
            pos = _skip_blank(text, pos + 1)
        elif not text.startswith(closer, pos):
            return None
    return pos + 1, tables


def _toml_lines(data: bytes) -> Optional[list]:
    """config.toml as (head, value, secret) per line: for a key line, head is the line up to its value, value
    the value's text and secret whether its full path, header and dotted key together, is secret-named; for
    any other line, value is None. Comments are dropped. None when a line is not a form the scrubber fully
    understands: a multi-line value, an inline table under a path it would keep, an odd table header, a
    quoted key with an escape, or anything else."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    table: list = []
    found = []
    for raw in lines:
        line = raw[:-1] if raw.endswith("\r") else raw
        start = _skip_blank(line, 0)
        indent, body = line[:start], line[start:]
        if body == "" or body.startswith("#"):
            found.append(("", None, False))
            continue
        header = TOML_HEADER.match(body)
        if header is not None:
            rest = body[header.end():].strip()
            if len(header.group(1)) != len(header.group(3)) or (rest and not rest.startswith("#")):
                return None
            table = _toml_parts(header.group(2))
            found.append((indent + header.group(0), None, False))
            continue
        if body.startswith("["):
            return None
        key = TOML_DOTTED.match(body)
        if key is None:
            return None
        pos = _skip_blank(body, key.end())
        if not body.startswith("=", pos):
            return None
        pos = _skip_blank(body, pos + 1)
        value = _toml_value(body, pos)
        if value is None:
            return None
        rest = body[value[0]:].strip()
        if rest and not rest.startswith("#"):
            return None
        secret = _toml_path_secret(table + _toml_parts(key.group()))
        if value[1] and not secret:
            return None
        found.append((f"{indent}{key.group()} = ", body[pos:value[0]], secret))
    return found


def _toml_blank(value: str) -> str:
    """A secret-named TOML value made empty, keeping its kind: a number, boolean, date or time stays."""
    if value[0] in "\"'":
        return '""'
    if value[0] in "[{":
        return "[]" if value[0] == "[" else "{}"
    return value


def _scrub_toml(data: bytes) -> Optional[bytes]:
    """config.toml with every value under a secret-named path blanked and its comments dropped. None leaves
    the file out, when any line is a form the scrubber does not fully understand."""
    found = _toml_lines(data)
    if found is None:
        return None
    out = [head if value is None else head + (_toml_blank(value) if secret else value)
           for head, value, secret in found]
    return ("\n".join(out) + "\n").encode("utf-8")


def _toml_left(data: bytes) -> bool:
    """True when a restored config.toml still has a value under a secret-named path, or cannot be read."""
    found = _toml_lines(data)
    if found is None:
        return True
    return any(secret and value is not None and value not in TOML_BLANKS and _toml_blank(value) != value
               for _, value, secret in found)


def scrub_kind(name: str) -> Optional[str]:
    """json for a settings or MCP JSON file, toml for a Codex config.toml, None for a file kept as it is."""
    lowered = name.lower()
    if lowered.endswith(".json") and (lowered.startswith("settings") or "mcp" in lowered):
        return "json"
    return "toml" if lowered == "config.toml" else None


def scrub(name: str, data: bytes) -> Optional[bytes]:
    """The bytes to keep for one file, scrubbed when it is a settings, MCP or Codex config file. None leaves
    the file out."""
    kind = scrub_kind(name)
    if kind is None:
        return data
    if len(data) > SCRUB_MAX_BYTES:
        return None
    return _scrub_json(data) if kind == "json" else _scrub_toml(data)


def git_name(name: str) -> bool:
    return name.lower() == ".git"


def git_folder(dir_fd: int) -> bool:
    """A folder laid out like a git folder (a bare repo, or a .git under another name): HEAD, objects, refs."""
    return all(safefs.lstat(dir_fd, part) is not None for part in GIT_DIR_PARTS)


# Making the archive


class Builder:
    """Adds plain files to an open tar, scrubbed, with a manifest of what went in and what was left out."""

    def __init__(self, tar: tarfile.TarFile, made_at: int) -> None:
        self.tar, self.made_at = tar, made_at
        self.files: dict = {}
        self.skipped: list = []

    def add_bytes(self, arcname: str, data: bytes) -> None:
        info = tarfile.TarInfo(arcname)
        info.size, info.mtime, info.mode = len(data), self.made_at, 0o600
        self.tar.addfile(info, io.BytesIO(data))
        self.files[arcname] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def add_file(self, dir_fd: int, name: str, arcname: str) -> None:
        if credential_name(name):
            self.skipped.append({"path": arcname, "why": "named like a credential"})
            return
        try:
            fd = os.open(name, safefs.READ_FLAGS, dir_fd=dir_fd)
        except OSError:
            self.skipped.append({"path": arcname, "why": "could not be opened as a plain file"})
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                self.skipped.append({"path": arcname, "why": "not a plain file with one link"})
                return
            if info.st_size > config.BACKUP_FILE_MAX_BYTES:
                self.skipped.append({"path": arcname, "why": "larger than the backup's file limit"})
                return
            chunks, total = [], 0
            while total <= config.BACKUP_FILE_MAX_BYTES:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
        data = scrub(name, b"".join(chunks))
        if data is None or len(data) > config.BACKUP_FILE_MAX_BYTES:
            self.skipped.append({"path": arcname, "why": "could not be scrubbed, so it was left out"})
            return
        self.add_bytes(arcname, data)

    def add_tree(self, dir_fd: int, prefix: str, take: Optional[tuple] = None, skip: tuple = ()) -> None:
        """Every plain file under a folder, opened one component at a time with no link followed."""
        for name in sorted(os.listdir(dir_fd)):
            if name in SKIPPED_NAMES or name in skip or (take is not None and name not in take):
                continue
            arcname = f"{prefix}/{name}"
            if any(ord(char) < 32 for char in name):
                self.skipped.append({"path": prefix + "/?", "why": "a name with a control character"})
                continue
            if git_name(name):
                self.skipped.append({"path": arcname, "why": "git metadata"})
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                if credential_name(name):
                    self.skipped.append({"path": arcname, "why": "named like a credential"})
                    continue
                try:
                    child = os.open(name, safefs.DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    self.skipped.append({"path": arcname, "why": "could not be opened as a plain folder"})
                    continue
                try:
                    if git_folder(child):
                        self.skipped.append({"path": arcname, "why": "git metadata"})
                    else:
                        self.add_tree(child, arcname)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                self.add_file(dir_fd, name, arcname)
            else:
                self.skipped.append({"path": arcname, "why": "a link or special file"})

    def add_root(self, root: str, prefix: str, take: Optional[tuple] = None, skip: tuple = ()) -> None:
        try:
            fd = safefs.open_root(root)
        except safefs.Missing:
            self.skipped.append({"path": prefix, "why": "not there"})
            return
        except FleetError:
            self.skipped.append({"path": prefix, "why": "not a private plain folder of yours"})
            return
        try:
            self.add_tree(fd, prefix, take, skip)
        finally:
            os.close(fd)

    def add_memory(self) -> None:
        """Each Claude project's memory folder, and nothing else from the projects folder."""
        try:
            projects = safefs.open_dir(config.CLAUDE_CONFIG_DIR, "projects")
        except FleetError:
            return
        try:
            for name in sorted(os.listdir(projects)):
                try:
                    project = os.open(name, safefs.DIR_FLAGS, dir_fd=projects)
                except OSError:
                    continue
                try:
                    self.add_tree(project, f"claude/projects/{name}", take=("memory",))
                finally:
                    os.close(project)
        finally:
            os.close(projects)

    def add_database(self, backups_fd: int) -> None:
        """A consistent copy of the store database, made with SQLite's own backup into a private temp file."""
        temp = f".snapshot-{secrets.token_hex(6)}.db"
        os.close(safefs.create_new(backups_fd, temp))
        try:
            source = db.connect_readonly(config.DB_PATH)
            try:
                target = sqlite3.connect(f"{config.OFFICE_ROOT}/{config.BACKUP_DIR}/{temp}")
                try:
                    source.backup(target)
                    # The copy takes the store's WAL mode, which a read-only open of a lone file cannot use.
                    target.execute("PRAGMA journal_mode=DELETE")
                finally:
                    target.close()
            finally:
                source.close()
            self.add_bytes(DB_MEMBER, safefs.read_regular(backups_fd, temp, config.BACKUP_FILE_MAX_BYTES,
                                                          "database copy"))
        finally:
            for name in (temp, "%s-journal" % temp, "%s-wal" % temp, "%s-shm" % temp):
                if safefs.lstat(backups_fd, name) is not None:
                    os.unlink(name, dir_fd=backups_fd)


def archive_name(ts: int) -> str:
    return f"gringotts-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}.tar.gz"


def archives(backups_fd: int) -> list:
    """The archive names in the backups folder, oldest first."""
    return sorted(name for name in os.listdir(backups_fd) if ARCHIVE.fullmatch(name))


def prune(backups_fd: int, ts: int) -> list:
    """Delete archives older than BACKUP_KEEP_DAYS, always keeping the newest."""
    removed = []
    cutoff = ts - config.BACKUP_KEEP_DAYS * DAY
    for name in archives(backups_fd)[:-1]:
        made = time.mktime(time.strptime(ARCHIVE.fullmatch(name).group(1), "%Y%m%d-%H%M%S"))
        if made < cutoff and safefs.is_safe_regular(backups_fd, name):
            os.unlink(name, dir_fd=backups_fd)
            removed.append(name)
    return removed


def backup(now: Optional[int] = None) -> dict:
    """Make tonight's archive, then prune the old ones."""
    ts = common.now_stamp(now)
    name = archive_name(ts)
    with safefs.opened_dir(config.OFFICE_ROOT, config.BACKUP_DIR, create=True) as backups_fd:
        temp = f".{name}.{secrets.token_hex(6)}.tmp"
        out_fd = safefs.create_new(backups_fd, temp)
        try:
            with os.fdopen(out_fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                    built = Builder(tar, ts)
                    built.add_root(config.CLAUDE_CONFIG_DIR, "claude", take=CLAUDE_TAKE)
                    built.add_memory()
                    built.add_root(config.CODEX_CONFIG_DIR, "codex", take=CODEX_TAKE)
                    built.add_root(config.OFFICE_ROOT, "office", skip=OFFICE_SKIP)
                    built.add_root(config.CASTLE_ROOT, "castle", skip=CASTLE_SKIP)
                    built.add_database(backups_fd)
                    manifest = {"made_at": ts, "files": built.files, "skipped": built.skipped}
                    data = (json.dumps(manifest, ensure_ascii=True, indent=1, sort_keys=True) + "\n").encode("ascii")
                    built.add_bytes(MANIFEST, data)
                raw.flush()
                os.fsync(raw.fileno())
            os.rename(temp, name, src_dir_fd=backups_fd, dst_dir_fd=backups_fd)
        except BaseException:
            if safefs.lstat(backups_fd, temp) is not None:
                os.unlink(temp, dir_fd=backups_fd)
            raise
        removed = prune(backups_fd, ts)
        size = safefs.lstat(backups_fd, name).st_size
    return {"ok": True, "archive": f"{config.OFFICE_ROOT}/{config.BACKUP_DIR}/{name}", "bytes": size,
            "files": len(built.files), "left_out": len(built.skipped), "pruned": removed}


# The restore drill


def _member_parts(member: tarfile.TarInfo) -> Optional[list]:
    """The plain relative path parts of a regular file entry, or None for anything else."""
    name = member.name
    if not member.isreg() or not name or name.startswith("/") or "\\" in name or any(ord(c) < 32 for c in name):
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def _restore(root_fd: int, parts: list, data: bytes) -> None:
    """Write one file under the drill folder, making its folders, never following a link."""
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, safefs.DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        out = os.open(parts[-1], safefs.NEW_FLAGS, 0o600, dir_fd=fd)
        try:
            safefs.write_all(out, data)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _check_database(path: str) -> Optional[str]:
    try:
        conn = sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"the database copy does not open: {type(exc).__name__}"
    return None if result and result[0] == "ok" else "the database copy failed its integrity check"


def _drill_archive(raw, root_fd: int, folder: str) -> tuple:
    problems, restored = [], {}
    with tarfile.open(fileobj=raw, mode="r:gz") as tar:
        manifest = None
        for member in tar.getmembers():
            parts = _member_parts(member)
            if parts is None:
                problems.append(f"an entry is not a plain file with a plain name: {common.one_line(member.name, 120)}")
                continue
            if member.size > config.BACKUP_FILE_MAX_BYTES * 2:
                problems.append(f"{member.name} is larger than the drill reads")
                continue
            data = tar.extractfile(member).read(member.size + 1)
            if member.name == MANIFEST:
                manifest = data
                continue
            if credential_name(parts[-1]):
                problems.append(f"{member.name} is named like a credential")
            if any(git_name(part) for part in parts):
                problems.append(f"{member.name} is git metadata")
            kind = scrub_kind(parts[-1])
            if (kind == "json" and _json_left(data)) or (kind == "toml" and _toml_left(data)):
                problems.append(f"{member.name} still has a value under a secret-named key")
            _restore(root_fd, parts, data)
            restored[member.name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    try:
        listed = json.loads(manifest.decode("ascii"))["files"] if manifest is not None else None
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        listed = None
    if not isinstance(listed, dict):
        problems.append("the archive has no readable manifest")
    else:
        for path in sorted(set(listed) | set(restored)):
            if listed.get(path) != restored.get(path):
                problems.append(f"{path} does not match the manifest")
    if DB_MEMBER not in restored:
        problems.append("the archive has no database copy")
    else:
        problem = _check_database(f"{folder}/{DB_MEMBER}")
        if problem:
            problems.append(problem)
    return problems, restored


def drill(archive: Optional[str] = None, now: Optional[int] = None, keep: bool = False) -> dict:
    """Restore an archive into a fresh folder in the backups folder and check it. Never touches a live folder."""
    ts = common.now_stamp(now)
    with safefs.opened_dir(config.OFFICE_ROOT, config.BACKUP_DIR) as backups_fd:
        names = archives(backups_fd)
        if archive is None:
            if not names:
                raise Problem("there is no archive to drill yet")
            archive = names[-1]
        elif archive not in names:
            raise Problem("name an archive in the backups folder, like gringotts-YYYYMMDD-HHMMSS.tar.gz")
        problems = []
        info = safefs.lstat(backups_fd, archive)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            problems.append("the archive is not a private plain file of yours with mode 0600")
        drill_name = f"drill-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}-{secrets.token_hex(4)}"
        folder = f"{config.OFFICE_ROOT}/{config.BACKUP_DIR}/{drill_name}"
        os.mkdir(drill_name, 0o700, dir_fd=backups_fd)
        try:
            root_fd = os.open(drill_name, safefs.DIR_FLAGS, dir_fd=backups_fd)
            try:
                archive_fd = os.open(archive, safefs.READ_FLAGS, dir_fd=backups_fd)
                with os.fdopen(archive_fd, "rb") as raw:
                    found, restored = _drill_archive(raw, root_fd, folder)
                problems += found
            finally:
                os.close(root_fd)
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            problems.append(f"the archive could not be read whole: {type(exc).__name__}")
            restored = {}
        finally:
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)
    return {"ok": not problems, "archive": archive, "files": len(restored), "problems": problems[:20],
            "restored_to": folder if keep else None}


def report_failure(result: dict, now: Optional[int] = None) -> None:
    """Once live, a failed backup or drill is a headmaster event. In shadow mode it only reaches the log."""
    shadow = patrol.shadow_on()
    if shadow:
        return
    try:
        conn = common.connect()
    except StoreError:
        return
    try:
        what = "restore drill" if result.get("drill") else "backup"
        detail = result.get("error") or "; ".join(result.get("problems", [])[:2])
        patrol.tell_ryan(conn, shadow, "gringotts", f"Gringotts' {what} failed: {detail}",
                         f"gringotts:{what}:{patrol.local_day(now)}", now, desk="gringotts")
    except StoreError:
        pass
    finally:
        conn.close()


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Gringotts' own lock, so a backup and a drill never overlap."""
    with safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True) as locks_fd, \
            safefs.held_lock(locks_fd, LOCK, blocking=True, timeout=LOCK_WAIT_SECONDS):
        yield


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and (args[0] != "--drill" or len(args) > 2):
        sys.stderr.write("gringotts takes no arguments, or --drill [ARCHIVE]\n")
        return 2
    is_drill = bool(args)
    try:
        with locked():
            result = drill(args[1] if len(args) == 2 else None) if is_drill else backup()
    except (FleetError, StoreError, OSError, tarfile.TarError, sqlite3.Error) as exc:
        result = {"ok": False, "error": common.one_line(exc, 300) if not isinstance(exc, OSError)
                  else type(exc).__name__}
    result = {"drill": is_drill, **result}
    if not result["ok"]:
        report_failure(result)
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
