"""Temp office, castle, transcripts and database for the fleet tests.

Built on the store's own test support: temp folders come from tests.support.temp_dir
under the constant /private/tmp, the database is a fresh temp file, and every fleet
location constant is patched to point inside the temp folder. The real database,
castle and office are never touched.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import db, ids, pensieve
from tests.support import NOW, temp_dir

from fleet import config

REGISTRY = (
    ("mcgonagall", "claude", "McGonagall - Chief of Staff", "opus"),
    ("harry", "codex", "Harry - Senior Engineer", None),
    ("hermione", "claude", "Hermione - Staff Engineer", "opus"),
    ("moody", "codex", "Moody - Security Reviewer", None),
    ("ron", "claude", "Ron - Release Engineer", "haiku"),
    ("snape", "claude", "Snape - Data Analyst", "sonnet"),
    ("portrait", "claude", "Dumbledore - Knowledge Manager", "opus"),
    ("ryan-claude-1", "claude", "Ryan's own Claude sessions", None),
    ("ryan", "human", "Headmaster", None),
    ("owl-post", "script", "Owl Post - Message Router", None),
    ("map", "script", "Marauder's Map - PR Watcher", None),
    ("gringotts", "script", "Gringotts - Backup", None),
)
OFFICE_DESKS = ("harry", "hermione", "moody", "ron", "snape", "portrait")
SCRATCHPAD = "# Scratchpad\n\n## Now\n\n## Notes\n\n## Checkpoint\n"
REAL_CASTLE = "/Users/crisryantan/hogwarts"
REAL_OFFICE = "/Users/crisryantan/.hogwarts"
CASTLE_SHARED = (".git", ".claude", "tasks", "worktrees", "CLAUDE.md", "PLAN.md", "standing-orders.md", ".gitignore")


def claude_settings(desk: str) -> dict:
    """A locked-down Claude desk settings file, written out by hand with the real castle paths."""
    deny_write = [REAL_OFFICE] + [f"{REAL_CASTLE}/{name}" for name in CASTLE_SHARED]
    deny_write += [f"{REAL_CASTLE}/desks/{other}" for other in config.CASTLE_DESKS if other != desk]
    deny_write.append(f"{REAL_CASTLE}/desks/{desk}/inbox")
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "allowUnixSockets": False,
            "filesystem": {"allowWrite": [f"{REAL_CASTLE}/desks/{desk}"], "denyRead": [REAL_OFFICE],
                           "denyWrite": deny_write},
        },
        "permissions": {
            "deny": ["Read(~/.hogwarts/**)", "Edit(~/.hogwarts/**)"],
            "allow": ["Read"],
            "disableBypassPermissionsMode": "disable",
        },
    }
CODEX_PROFILE = """# fleet-owned Codex profile
model_reasoning_effort = "high"
approval_policy = "never"

[sandbox_workspace_write]
network_access = false

[shell_environment_policy]
inherit = "core"
"""
TOKEN_SHAPE = r"[A-Za-z0-9_-]{43}"


class FleetCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir(self)
        self.office = self.tmp / "office"
        self.castle = self.tmp / "castle"
        self.transcripts = self.tmp / "transcripts"
        for folder in (self.office, self.castle, self.transcripts):
            folder.mkdir(mode=0o700)
        self.db_path = self.office / "state" / "pensieve.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        for name, family, role, model in REGISTRY:
            pensieve.add_desk(self.conn, name, family, role=role, model=model, now=NOW)
        self._build_castle()
        self._build_office()
        for name, value in (
            ("OFFICE_ROOT", str(self.office)),
            ("CASTLE_ROOT", str(self.castle)),
            ("DB_PATH", str(self.db_path)),
            ("TRANSCRIPTS_ROOT", str(self.transcripts)),
        ):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _build_castle(self) -> None:
        for name in ("desks", "tasks", "worktrees"):
            (self.castle / name).mkdir(mode=0o700)
        for desk in config.CASTLE_DESKS:
            folder = self.castle / "desks" / desk
            folder.mkdir(mode=0o700)
            (folder / "inbox").mkdir(mode=0o700)
            (folder / "outbox").mkdir(mode=0o700)
            self.write_file(folder / "scratchpad.md", SCRATCHPAD)

    def _build_office(self) -> None:
        (self.office / "desks").mkdir(mode=0o700)
        for desk in OFFICE_DESKS:
            folder = self.office / "desks" / desk
            folder.mkdir(mode=0o700)
            self.write_file(folder / "BRIEF.md", f"# {desk} brief\nI do one job and never push.\n")
            if desk in config.HEADLESS_CODEX:
                self.write_file(folder / "codex.toml", CODEX_PROFILE)
            else:
                self.write_file(folder / "settings.json", json.dumps(claude_settings(desk)))

    # files

    @staticmethod
    def write_file(path: Path, text, mode: int = 0o600) -> Path:
        data = text.encode("utf-8") if isinstance(text, str) else text
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(path, mode)
        return path

    def outbox(self, desk: str) -> Path:
        return self.castle / "desks" / desk / "outbox"

    def inbox(self, desk: str) -> Path:
        return self.castle / "desks" / desk / "inbox"

    def write_owl(self, desk: str, name: str, payload) -> Path:
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return self.write_file(self.outbox(desk) / name, data)

    def enable(self, desk: str) -> None:
        self.write_file(self.office / "desks" / desk / config.ENABLED_MARKER, "", mode=0o644)

    def write_transcript(self, entries: list, name: str = "session.jsonl") -> str:
        folder = self.transcripts / "-project"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / name
        self.write_file(path, "".join(json.dumps(entry) + "\n" for entry in entries))
        return str(path)

    # store helpers

    def events(self) -> list:
        rows = self.conn.execute("SELECT kind, verdict, desk, summary, acked_at FROM events ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def outbox_path(self, desk: str, name: str) -> str:
        return f"{ids.outbox_root(desk)}/{name}"

    def run_hook(self, module, payload, argv=None, now: int = NOW) -> tuple:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        out, err = io.StringIO(), io.StringIO()
        code = module.main(argv=[] if argv is None else argv, stdin=io.BytesIO(raw), stdout=out, stderr=err, now=now)
        return code, out.getvalue(), err.getvalue()


PROMPT_ID = "5d0c9a3e-7777-4888-9999-0aaabbbcccdd"


def user_entry(text, **extra) -> dict:
    """A prompt Ryan typed, shaped like the desktop app writes it (NOW is 2027-01-15T08:00:00Z)."""
    entry = {"type": "user", "entrypoint": "claude-desktop", "timestamp": "2027-01-15T08:00:00.000Z",
             "promptId": "0d1e2f30-1111-4222-8333-444455556666", "promptSource": "sdk",
             "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}
    entry.update(extra)
    return entry


def peer_entry(text, **extra) -> dict:
    """Text another session sent in with send_message, shaped like the desktop app writes it."""
    return user_entry(text, origin={"kind": "peer"}, isMeta=True, promptSource="system", **extra)


def assistant_entry(message_id: str, blocks: list, usage: int = 1000, model: str = "claude-opus-5-5", **extra) -> dict:
    entry = {
        "type": "assistant", "entrypoint": "claude-desktop", "timestamp": "2027-01-15T08:01:00.000Z",
        "message": {"id": message_id, "role": "assistant", "model": model, "content": blocks,
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": usage - 110,
                              "cache_creation_input_tokens": 100, "output_tokens": 50}},
    }
    entry.update(extra)
    return entry


def tool_result_entry(tool_id: str, output: str) -> dict:
    return {"type": "user", "entrypoint": "claude-desktop", "timestamp": "2027-01-15T08:00:30.000Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}]},
            "toolUseResult": {"stdout": output}}
