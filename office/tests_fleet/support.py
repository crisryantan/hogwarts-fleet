"""Temp office, castle, transcripts and database for the fleet tests.

Built on the store's own test support: temp folders come from tests.support.temp_dir
under the constant /private/tmp, the database is a fresh temp file, and every fleet
location constant is patched to point inside the temp folder. The real database,
castle and office are never touched.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from hogwarts import capacity, db, ids, pensieve
from tests.support import NOW, temp_dir

from fleet import config, run_desk

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
    ("ollivander", "script", "Ollivander - Model Keeper", None),
)
OFFICE_DESKS = ("harry", "hermione", "moody", "ron", "snape", "portrait")
MANY_TASK_DESKS = ("harry", "hermione", "moody", "ron", "ryan-claude-1")
SCRATCHPAD = "# Scratchpad\n\n## Now\n\n## Notes\n\n## Checkpoint\n"
REAL_CASTLE = "/Users/crisryantan/hogwarts"
REAL_OFFICE = "/Users/crisryantan/.hogwarts"
CASTLE_SHARED = (".git", ".claude", "tasks", "worktrees", "CLAUDE.md", "PLAN.md", "standing-orders.md", ".gitignore")
# The office these tests run in. In a clone of the kit it is the kit's office folder, next to install.sh. In an
# installed office it holds that install's own config, trusted lists and agent files, so tests never rely on them.
OFFICE = Path(__file__).resolve().parents[1]
IN_KIT = OFFICE.name == "office" and (OFFICE.parent / "install.sh").is_file()
ONLY_IN_KIT = "checks the kit's shipped files, which are only in a clone of the kit, not in an installed office"
# Copies of the kit's placeholder trusted lists, for tests that need a trusted list in their temp office.
LIVE_TOOLS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live-tools"


def kit_setting(name: str):
    """A setting as the kit's fleet/config.py ships it, read from the file rather than the patched module."""
    tree = ast.parse((OFFICE / "fleet" / "config.py").read_text())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


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

[shell_environment_policy]
inherit = "core"
"""
TOKEN_SHAPE = r"[A-Za-z0-9_-]{43}"


class FakeChild:
    """A desk process that run_desk.start_child would have started. wait() calls the fake the way
    subprocess.run would have, with the run's timeout, so a fake can write output, raise TimeoutExpired,
    or change the store while the desk "runs"."""

    def __init__(self, fake, argv: list, kwargs: dict):
        self.args, self._fake, self._kwargs, self.returncode = argv, fake, kwargs, None

    def wait(self, timeout=None) -> int:
        if self.returncode is None:
            self.returncode = self._fake(self.args, timeout=timeout, check=False, **self._kwargs).returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9


def fake_children(fake=None, returncode: int = 0):
    """Patch run_desk.start_child so no desk process starts. Each launch runs fake(argv, **kwargs), which
    returns a CompletedProcess, or exits with returncode. The patch records each start's arguments."""
    if fake is None:
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(args=argv, returncode=returncode)
    return mock.patch.object(run_desk, "start_child", side_effect=lambda argv, **kwargs: FakeChild(fake, argv, kwargs))


@contextlib.contextmanager
def every_slot(desk: str):
    """Hold every run slot of desk, as other runs in all of them would, so the desk is busy."""
    with contextlib.ExitStack() as held:
        for index in range(run_desk.run_slots(desk)):
            held.enter_context(run_desk.slot_lock(desk, index))
        yield


class FleetCase(unittest.TestCase):
    # The desks granted many tasks at setUp. A test of a single-task desk passes a smaller set.
    many_task_desks = MANY_TASK_DESKS

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
        # As install.sh does: these desks are added after the store migrated, so the V7 grant missed them.
        for name in self.many_task_desks:
            pensieve.allow_many_tasks(self.conn, name, now=NOW)
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
        # Nothing is blocked unless a test blocks it, whatever the office's own config blocks.
        unblocked = mock.patch.object(config, "BLOCKED_MODEL_PREFIXES", ())
        unblocked.start()
        self.addCleanup(unblocked.stop)
        # No desk process ever starts: a test that launches one fakes it with fake_children.
        never = mock.patch.object(run_desk, "start_child", side_effect=AssertionError("no desk process may start"))
        never.start()
        self.addCleanup(never.stop)
        # No automatic review process ever starts either. The Owl Post's starts are recorded here, and a test that
        # wants the review runs review.auto_review itself.
        reviews = mock.patch.object(run_desk, "spawn_review")
        self.spawned_reviews = reviews.start()
        self.addCleanup(reviews.stop)
        # A desk run empties its private folder under the per-user temp folder, so every test gets its own temp
        # folder there unless it sets one. Without this a faked Harry run empties the real hogwarts-harry folder.
        user_temp = self.tmp / "user-temp"
        user_temp.mkdir(mode=0o700)
        temp = mock.patch.object(run_desk, "user_temp_dir", return_value=str(user_temp))
        temp.start()
        self.addCleanup(temp.stop)
        # The cap day follows this Mac's time zone. Tests pin it to UTC unless they set their own.
        zone = mock.patch.object(capacity, "local_utc_offset", return_value=0)
        zone.start()
        self.addCleanup(zone.stop)

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
