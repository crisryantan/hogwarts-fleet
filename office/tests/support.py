from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hogwarts import db, ids, owlery, pensieve

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
