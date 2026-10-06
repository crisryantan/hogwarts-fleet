from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hogwarts import capacity, db, ids, owlery, pensieve

NOW = 1_800_000_000
HOUR = 3600
DAY = 86400
REPO = "acme/web-app"
SHA = "0123456789abcdef0123456789abcdef01234567"
TEST_TMP_ROOT = Path("/private/tmp")


def temp_dir(case: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp(prefix="hogwarts-test-", dir=TEST_TMP_ROOT))
    case.addCleanup(shutil.rmtree, path, True)
    return path


def outbox_file(desk: str, name: str = "body.txt") -> str:
    return f"{ids.outbox_root(desk)}/{name}"


def intent_file(task_id: str) -> str:
    return f"{ids.TASKS_ROOT}/{task_id}/{ids.INTENT_FILE}"


def worktree(name: str = "wt") -> str:
    return f"{ids.WORKTREES_ROOT}/{name}"


def review_file(name: str = "review.md") -> str:
    return f"{ids.REVIEWS_ROOT}/{name}"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir(self)
        self.db_path = self.tmp / "state" / "pensieve.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def desk(self, name: str = "alpha", family: str = "claude") -> dict:
        return pensieve.add_desk(self.conn, name, family, now=NOW)

    def desks(self) -> None:
        self.desk("alpha", "claude")
        self.desk("beta", "codex")

    def task(self, desk: str = "alpha", title: str = "build the thing", **kwargs) -> dict:
        kwargs.setdefault("now", NOW)
        return pensieve.create_task(self.conn, desk, title, **kwargs)

    def started(self, desk: str = "alpha", now: int = NOW, **kwargs) -> dict:
        task = self.task(desk, **kwargs)
        return pensieve.start_task(self.conn, task["id"], now=now)

    def close_complete(self, task_id: str, now: int = NOW) -> dict:
        token = owlery.mint(self.conn, task_id, "cli", now=now)["token"]
        return pensieve.close_task(self.conn, task_id, "complete", token, now=now)

    def post_result(self, request_id: str, now: int = NOW) -> dict:
        request = owlery.get_request(self.conn, request_id)
        return owlery.send(self.conn, request["recipient"], request["requester"], "result", "done",
                           request_id=request_id, now=now)

    def count(self, table: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


MERGE_SHA = "2123456789abcdef0123456789abcdef01234567"
GO_DIGEST = "c" * 64


def go_build(conn, parent_id: str = "tk_00000000000000a1", now: int = NOW) -> dict:
    """A build a typed go registered, as the store holds it once its review passed: the go task on gamma (claude)
    with its TASK.md and spec, still queued, and its build task on beta (codex) awaiting close after a round PASS from
    alpha (claude) at SHA, with that round's reviewer task closed. Needs desks alpha, beta and gamma."""
    pensieve.create_task(conn, "gamma", "the go task", intent_path=intent_file(parent_id), task_id=parent_id, now=now)
    pensieve.record_spec(conn, parent_id, "/private/tmp/checkout", "fix/site", "origin/main", GO_DIGEST, now=now)
    build = owlery.open_request(conn, "gamma", "beta", "build it", parent_task_id=parent_id, now=now)["task"]["id"]
    pensieve.start_task(conn, build, now=now)
    pensieve.record_commit(conn, build, REPO, SHA, now=now)
    opened = capacity.open_review_round(conn, build, "alpha", SHA, "review it", now=now)
    capacity.record_round_verdict(conn, opened["request"]["id"], REPO, "PASS", now=now)
    pensieve.close_task(conn, opened["task"]["id"], "superseded", now=now)
    pensieve.mark_awaiting_close(conn, build, now=now)
    return {"parent": parent_id, "build": build, "request": opened["request"]["id"]}


def proof(**changes) -> dict:
    """A proven close's proof of SHA landing as MERGE_SHA, with one command check and one written check by alpha."""
    return {"repo": REPO, "pass_sha": SHA, "merge_sha": MERGE_SHA, "landed": "pr", "pr_number": 7, "ci": "green",
            "ci_checks": 2, "command_checks": 1, "written_checks": 1, "judge_desk": "alpha",
            "evidence_path": review_file("tk_00000000000000a2/close-evidence.md"), "evidence_sha256": "d" * 64,
            **changes}


CLOSURE_COLUMNS = ("task_id", "kind", "via_task_id", "repo", "pass_sha", "merge_sha", "landed", "pr_number", "ci",
                   "ci_checks", "command_checks", "written_checks", "judge_desk", "evidence_path", "evidence_sha256",
                   "recorded_at")


def insert_closure(conn, task_id: str, kind: str = "proven", via: str = None, **changes) -> None:
    """A raw closure row, as a writer that skips pensieve.close_proven would write it."""
    row = {"task_id": task_id, "kind": kind, "via_task_id": via, **proof(**changes), "recorded_at": NOW}
    conn.execute(f"INSERT INTO task_closures({', '.join(CLOSURE_COLUMNS)}) VALUES"
                 f" ({', '.join('?' for _ in CLOSURE_COLUMNS)})", tuple(row[name] for name in CLOSURE_COLUMNS))
