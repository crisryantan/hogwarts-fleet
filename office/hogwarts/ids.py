from __future__ import annotations

import math
import posixpath
import re
import secrets
import time
import unicodedata
from typing import Iterable, Optional, Sequence

from .errors import ValidationError

MAX_INT = 2**63 - 1
# 9999-12-31T23:59:59Z, so a timestamp plus any bounded duration still fits SQLite's 64 bit INTEGER.
MAX_TIME = 253402300799

CASTLE_ROOT = "/Users/crisryantan/hogwarts"
OFFICE_ROOT = "/Users/crisryantan/.hogwarts"
DESKS_ROOT = CASTLE_ROOT + "/desks"
TASKS_ROOT = CASTLE_ROOT + "/tasks"
WORKTREES_ROOT = CASTLE_ROOT + "/worktrees"
REVIEWS_ROOT = OFFICE_ROOT + "/reviews"
INTENT_FILE = "TASK.md"

PREFIXES = {"task": "tk", "request": "rq", "owl": "owl", "review": "rv"}

PATTERNS = {
    "task": re.compile(r"tk_[0-9a-f]{16}"),
    "request": re.compile(r"rq_[0-9a-f]{16}"),
    "owl": re.compile(r"owl_[0-9a-f]{16}"),
    "review": re.compile(r"rv_[0-9a-f]{16}"),
    "desk": re.compile(r"[a-z][a-z0-9-]{1,31}"),
    "sha": re.compile(r"[0-9a-f]{40}"),
    "repo": re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}"),
    "session": re.compile(r"[A-Za-z0-9._-]{8,80}"),
    "label": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\[\]-]{0,79}"),
    "display": re.compile(r"[A-Za-z0-9][A-Za-z0-9 '._&()+-]{0,78}[A-Za-z0-9.)]|[A-Za-z0-9]"),
    "project": re.compile(r"[A-Za-z0-9._/-]{1,200}"),
    "kind": re.compile(r"[a-z][a-z0-9_.-]{0,39}"),
    "tag": re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}"),
    "key": re.compile(r"[A-Za-z0-9._:-]{8,128}"),
    "dedupe": re.compile(r"[A-Za-z0-9._:-]{1,200}"),
    "subject_key": re.compile(r"[a-z0-9][a-z0-9._:/-]{1,79}"),
    # A go spec's branch and base, the shapes fleet/gitops.py makes worktrees from, and a TASK.md's sha256.
    "branch": re.compile(r"[a-z0-9][a-z0-9._/-]{0,99}"),
    "ref": re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}"),
    "sha256": re.compile(r"[0-9a-f]{64}"),
}

NAMES = {
    "task": "task id",
    "request": "request id",
    "owl": "owl id",
    "review": "review id",
    "desk": "desk name",
    "sha": "git sha",
    "repo": "repo",
    "session": "session id",
    "label": "label",
    "display": "display name",
    "project": "project",
    "kind": "kind",
    "tag": "tag",
    "key": "idempotency key",
    "dedupe": "dedupe key",
    "subject_key": "subject key",
    "branch": "branch",
    "ref": "ref",
    "sha256": "sha256",
}

_STRIPPED = re.compile(r"[\x01-\x08\x0b-\x1f\x7f-\x9f]")
_PATH_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_LINE_BREAKS = re.compile(r"[\n\t]+")


def new_id(kind: str) -> str:
    return f"{PREFIXES[kind]}_{secrets.token_hex(8)}"


def check(kind: str, value: object, field: Optional[str] = None) -> str:
    name = field or NAMES[kind]
    if not isinstance(value, str) or PATTERNS[kind].fullmatch(value) is None:
        raise ValidationError(f"invalid {name}")
    if kind == "repo" and any(part in (".", "..") for part in value.split("/")):
        raise ValidationError(f"invalid {name}")
    return value


def optional(kind: str, value: object, field: Optional[str] = None) -> Optional[str]:
    return None if value is None else check(kind, value, field)


def check_enum(value: object, allowed: Sequence[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"invalid {field}")
    return value


def check_int(value: object, field: str, minimum: int = 0, maximum: int = MAX_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"invalid {field}")
    return value


def optional_int(value: object, field: str, minimum: int = 0, maximum: int = MAX_INT) -> Optional[int]:
    return None if value is None else check_int(value, field, minimum, maximum)


def check_amount(value: object, field: str, maximum: float = 1e9) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValidationError(f"invalid {field}")
    return float(value)


def stamp(now: Optional[int]) -> int:
    return int(time.time()) if now is None else check_int(now, "timestamp", maximum=MAX_TIME)


def _require_utf8(value: str, field: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{field} is not valid text") from None


def _strip_format(text: str) -> str:
    # Zero width and bidi control characters (category Cf) can hide words from checks or reorder what is shown.
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def clean_text(value: object, field: str, limit: int, single_line: bool = False, keep_format: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    if "\x00" in value:
        raise ValidationError(f"{field} contains a NUL byte")
    _require_utf8(value, field)
    text = _STRIPPED.sub("", value)
    if not keep_format:
        text = _strip_format(text)
    if single_line:
        text = _LINE_BREAKS.sub(" ", text).strip()
    if not text.strip():
        raise ValidationError(f"{field} is empty")
    if len(text) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters")
    return text


def optional_text(value: object, field: str, limit: int, single_line: bool = False,
                  keep_format: bool = False) -> Optional[str]:
    return None if value is None else clean_text(value, field, limit, single_line, keep_format)


def desk_root(desk: str) -> str:
    return f"{DESKS_ROOT}/{check('desk', desk)}"


def outbox_root(desk: str) -> str:
    return desk_root(desk) + "/outbox"


def check_absolute(value: object, field: str) -> str:
    if not isinstance(value, str) or not 1 < len(value) <= 1024:
        raise ValidationError(f"invalid {field}")
    _require_utf8(value, field)
    if _PATH_FORBIDDEN.search(value) or not value.startswith("/") or value.startswith("//"):
        raise ValidationError(f"invalid {field}")
    if posixpath.normpath(value) != value:
        raise ValidationError(f"invalid {field}")
    return value


def check_path(value: object, field: str, root: str) -> str:
    value = check_absolute(value, field)
    if not value.startswith(root + "/"):
        raise ValidationError(f"{field} must be under {root}")
    return value


def optional_path(value: object, field: str, root: str) -> Optional[str]:
    return None if value is None else check_path(value, field, root)


def intent_path(task_id: str) -> str:
    return f"{TASKS_ROOT}/{check('task', task_id)}/{INTENT_FILE}"


def check_intent_path(value: object, task_id: Optional[str]) -> str:
    value = check_absolute(value, "intent path")
    if task_id is None:
        raise ValidationError("an intent path belongs to one task, so pass that task's id with it")
    expected = intent_path(task_id)
    if value != expected:
        raise ValidationError(f"intent path must be {expected}")
    return value


def optional_intent_path(value: object, task_id: Optional[str]) -> Optional[str]:
    return None if value is None else check_intent_path(value, task_id)


def check_tags(values: Iterable[str]) -> str:
    if isinstance(values, str):
        values = [part for part in values.split(",") if part]
    if not isinstance(values, (list, tuple)):
        raise ValidationError("tags must be a list or a comma separated string")
    tags = [check("tag", value) for value in values]
    if len(tags) > 16:
        raise ValidationError("too many tags")
    return ",".join(dict.fromkeys(tags))
