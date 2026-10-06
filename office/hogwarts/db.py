from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union
from urllib.parse import quote

from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

DEFAULT_DB = Path("/Users/crisryantan/.hogwarts/state/pensieve.db")
CODE_ROOT = Path(os.path.abspath(__file__)).parent.parent
SCHEMA_VERSION = 10
WAL_ATTEMPTS = 50
BYTECODE_SUFFIXES = (".pyc", ".pyo", ".so")
SIDECARS = ("-wal", "-shm")

DESK_FAMILIES = ("claude", "codex", "human", "script")
PASS_FAMILIES = ("claude", "codex", "human")
TASK_STATUSES = ("queued", "active", "awaiting_close", "closed")
CLOSE_REASONS = ("complete", "abandoned", "superseded")
EVENT_VERDICTS = ("routine", "headmaster")
EXTRACT_ROLES = ("user", "assistant")
FACT_TIERS = ("pinned", "aging", "perishable")
FACT_END_REASONS = ("superseded", "withdrawn", "expired")
LOOKUP_LIMIT = 300
OWL_KINDS = ("request", "question", "answer", "result", "fyi")
REQUEST_PHASES = ("queued", "claimed", "running", "result_posted", "task_closed", "cleaned")
REQUEST_OUTCOMES = ("done", "deferred", "declined")
REQUEST_REASONS = ("conflict", "safety", "missing_access", "ambiguous_scope")
REVIEW_VERDICTS = ("PASS", "CHANGES", "HEADMASTER")
TOKEN_MINTERS = ("hook", "cli")
CAP_KINDS = ("runs", "spend")
CAP_HIT_CAPS = CAP_KINDS + ("plan",)
CAP_SOURCES = ("fleet", "claude_plan", "codex_plan")
# Ollivander's vocabulary: what a role needs, how hard a model thinks, and how Ryan files a model name.
MODEL_NEEDS = ("frontier", "workhorse", "fast")
MODEL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_LINES = MODEL_NEEDS + ("ignore",)
MODEL_FAMILIES = ("claude", "codex")
MODEL_CHANGE_REASONS = ("initial", "role", "approved", "pin", "revert")
# How the trial after a switch ended: a run passed, Ryan pinned the model, two failures held Ryan's own
# choice or could not revert onto a blocked or unchecked model, or two failures reverted it.
MODEL_TRIAL_ENDS = ("passed", "pinned", "held", "revert_blocked", "reverted")
# Desks that may hold many active tasks at once. V7 grants them on a store that already has them; a fresh
# install grants them with castle desk many-tasks after adding the desks.
MANY_TASK_DESKS_SEED = ("harry", "hermione", "moody", "ron", "ryan-claude-1")
# Desks that always hold one active task at a time: McGonagall, Snape, Dumbledore, Ryan and the scripts.
SINGLE_TASK_DESKS = ("mcgonagall", "snape", "portrait")
SINGLE_TASK_FAMILIES = ("human", "script")
# The longest branch name, in bytes, an own-session review task records. The name is Ryan's own branch, any name
# git takes in printable ASCII, so it is not held to the fleet's rule for the branches it makes and pushes.
REVIEW_BRANCH_MAX = 255
# The most run slots a desk may have (fleet RUN_SLOTS), so a slot a review round records is 0 to this less one.
RUN_SLOT_LIMIT = 8
# A go spec (V9): the longest repo folder path, branch and base a TASK.md's go records. The fleet holds the branch
# and base to its own stricter rules (fleet/gitops.py) before they get here.
SPEC_PATH_MAX = 1024
SPEC_BRANCH_MAX = 100
SPEC_BASE_MAX = 200
# Auto-portrait's nights (AUTO_PATCHES): the states a night moves through, what was in Dumbledore's outbox for its date
# just before his run, the most stored plan bytes, the most bytes of op ids in one comma list (100 ops of 24
# characters and their commas) and the longest outcome line.
AUTO_PATCH_STATES = ("armed", "validated", "done", "stopped", "off")
AUTO_PATCH_BEFORE = ("absent", "present", "unreadable")
AUTO_PATCH_OPS_MAX = 1048576
AUTO_PATCH_IDS_MAX = 2499
AUTO_PATCH_OUTCOME_MAX = 500

PathLike = Union[str, Path]

_STRICT = " STRICT" if sqlite3.sqlite_version_info >= (3, 37, 0) else ""


def _choices(values: Sequence[str]) -> str:
    return "(" + ", ".join("'" + value + "'" for value in values) + ")"


_ENUMS = {
    "families": _choices(DESK_FAMILIES),
    "pass_families": _choices(PASS_FAMILIES),
    "statuses": _choices(TASK_STATUSES),
    "close_reasons": _choices(CLOSE_REASONS),
    "verdicts": _choices(EVENT_VERDICTS),
    "roles": _choices(EXTRACT_ROLES),
    "tiers": _choices(FACT_TIERS),
    "end_reasons": _choices(FACT_END_REASONS),
    "owl_kinds": _choices(OWL_KINDS),
    "phases": _choices(REQUEST_PHASES),
    "history_phases": _choices(REQUEST_PHASES + ("deferred", "declined")),
    "outcomes": _choices(REQUEST_OUTCOMES),
    "reasons": _choices(REQUEST_REASONS),
    "review_verdicts": _choices(REVIEW_VERDICTS),
    "minters": _choices(TOKEN_MINTERS),
    "cap_kinds": _choices(CAP_KINDS),
    "cap_hit_caps": _choices(CAP_HIT_CAPS),
    "cap_sources": _choices(CAP_SOURCES),
    "needs": _choices(MODEL_NEEDS),
    "efforts": _choices(MODEL_EFFORTS),
    "model_lines": _choices(MODEL_LINES),
    "model_families": _choices(MODEL_FAMILIES),
    "change_reasons": _choices(MODEL_CHANGE_REASONS),
    "trial_ends": _choices(MODEL_TRIAL_ENDS),
    "auto_states": _choices(AUTO_PATCH_STATES),
    "auto_before": _choices(AUTO_PATCH_BEFORE),
}


def _enums(sql: str) -> str:
    return sql.format(**_ENUMS)


def _table(sql: str) -> str:
    return _enums(sql) + _STRICT


def _guard(name: str, when: str, message: str) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {name} {when} BEGIN SELECT RAISE(ABORT, '{message}'); END"


SCHEMA_VERSION_TABLE = _table(
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL
    )"""
)

V1 = (
    _table(
        """CREATE TABLE IF NOT EXISTS desks (
        name TEXT PRIMARY KEY NOT NULL,
        family TEXT NOT NULL CHECK (family IN {families}),
        role TEXT,
        model TEXT,
        created_at INTEGER NOT NULL
    )"""
    ),
    _table(
        """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY NOT NULL,
        desk TEXT NOT NULL REFERENCES desks(name),
        title TEXT NOT NULL,
        intent_path TEXT,
        status TEXT NOT NULL CHECK (status IN {statuses}),
        close_reason TEXT CHECK (close_reason IN {close_reasons}),
        parent_task_id TEXT REFERENCES tasks(id),
        request_id TEXT REFERENCES requests(id),
        session_id TEXT,
        worktree TEXT,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        closed_at INTEGER,
        CHECK ((status = 'closed') = (close_reason IS NOT NULL)),
        CHECK ((status = 'closed') = (closed_at IS NOT NULL))
    )"""
    ),
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_active_per_desk ON tasks(desk) WHERE status = 'active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_active_per_session ON tasks(session_id)"
    " WHERE status = 'active' AND session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id)",
    "CREATE INDEX IF NOT EXISTS tasks_desk_status ON tasks(desk, status)",
    _table(
        """CREATE TABLE IF NOT EXISTS task_commits (
        repo TEXT NOT NULL,
        sha TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        recorded_at INTEGER NOT NULL,
        PRIMARY KEY (repo, sha)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS task_commits_task ON task_commits(task_id)",
    _table(
        """CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY NOT NULL,
        requester TEXT NOT NULL REFERENCES desks(name),
        recipient TEXT NOT NULL REFERENCES desks(name),
        title TEXT NOT NULL,
        parent_task_id TEXT REFERENCES tasks(id),
        task_id TEXT REFERENCES tasks(id),
        phase TEXT NOT NULL CHECK (phase IN {phases}),
        outcome TEXT CHECK (outcome IN {outcomes}),
        reason TEXT CHECK (reason IN {reasons}),
        idem_key TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (requester <> recipient),
        CHECK (reason IS NULL OR outcome IN ('deferred', 'declined'))
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS requests_task ON requests(task_id)",
    _table(
        """CREATE TABLE IF NOT EXISTS request_phases (
        id INTEGER PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES requests(id),
        phase TEXT NOT NULL CHECK (phase IN {history_phases}),
        ts INTEGER NOT NULL,
        detail TEXT
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS request_phases_request ON request_phases(request_id)",
    _table(
        """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        desk TEXT NOT NULL REFERENCES desks(name),
        task_id TEXT REFERENCES tasks(id),
        kind TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN {verdicts}),
        summary TEXT NOT NULL,
        dedupe_key TEXT UNIQUE,
        acked_at INTEGER
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS events_pending ON events(verdict, acked_at)",
    _table(
        """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        desk TEXT REFERENCES desks(name),
        project TEXT NOT NULL,
        model TEXT,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        first_turn_tokens INTEGER,
        total_input_tokens INTEGER
    )"""
    ),
    _table(
        """CREATE TABLE IF NOT EXISTS extracts (
        id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        seq INTEGER NOT NULL,
        role TEXT NOT NULL CHECK (role IN {roles}),
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (session_id, seq)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS extracts_created ON extracts(created_at)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS extracts_fts USING fts5(text, content='extracts', content_rowid='id')",
    """CREATE TRIGGER IF NOT EXISTS extracts_ai AFTER INSERT ON extracts BEGIN
        INSERT INTO extracts_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS extracts_ad AFTER DELETE ON extracts BEGIN
        INSERT INTO extracts_fts(extracts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS extracts_au AFTER UPDATE ON extracts BEGIN
        INSERT INTO extracts_fts(extracts_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO extracts_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    _table(
        """CREATE TABLE IF NOT EXISTS keypoints (
        id INTEGER PRIMARY KEY,
        session_id TEXT REFERENCES sessions(session_id),
        text TEXT NOT NULL,
        tags TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )"""
    ),
    "CREATE VIRTUAL TABLE IF NOT EXISTS keypoints_fts USING fts5(text, content='keypoints', content_rowid='id')",
    """CREATE TRIGGER IF NOT EXISTS keypoints_ai AFTER INSERT ON keypoints BEGIN
        INSERT INTO keypoints_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS keypoints_ad AFTER DELETE ON keypoints BEGIN
        INSERT INTO keypoints_fts(keypoints_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS keypoints_au AFTER UPDATE ON keypoints BEGIN
        INSERT INTO keypoints_fts(keypoints_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO keypoints_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    _table(
        """CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL,
        text TEXT NOT NULL,
        tier TEXT NOT NULL CHECK (tier IN {tiers}),
        expires_at INTEGER,
        source TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER NOT NULL,
        archived_at INTEGER,
        CHECK ((tier = 'perishable') = (expires_at IS NOT NULL))
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS facts_scope ON facts(scope)",
    _table(
        """CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        desk TEXT NOT NULL REFERENCES desks(name),
        run_id TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
        output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
        cache_read_tokens INTEGER NOT NULL CHECK (cache_read_tokens >= 0),
        cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
        duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts)",
    _table(
        """CREATE TABLE IF NOT EXISTS owls (
        id TEXT PRIMARY KEY NOT NULL,
        idem_hash TEXT NOT NULL UNIQUE,
        sender TEXT NOT NULL REFERENCES desks(name),
        recipient TEXT NOT NULL REFERENCES desks(name),
        kind TEXT NOT NULL CHECK (kind IN {owl_kinds}),
        task_id TEXT REFERENCES tasks(id),
        request_id TEXT REFERENCES requests(id),
        in_reply_to TEXT REFERENCES owls(id),
        subject TEXT NOT NULL,
        body TEXT,
        body_path TEXT,
        created_at INTEGER NOT NULL,
        delivered_at INTEGER,
        read_at INTEGER,
        acked_at INTEGER,
        purged_at INTEGER,
        CHECK (sender <> recipient),
        CHECK (body IS NULL OR body_path IS NULL),
        CHECK (acked_at IS NULL OR read_at IS NOT NULL)
    )"""
    ),
    "CREATE UNIQUE INDEX IF NOT EXISTS owls_one_answer ON owls(in_reply_to) WHERE kind = 'answer'",
    "CREATE INDEX IF NOT EXISTS owls_recipient ON owls(recipient, acked_at)",
    "CREATE INDEX IF NOT EXISTS owls_request ON owls(request_id)",
    _table(
        """CREATE TABLE IF NOT EXISTS review_passes (
        id TEXT PRIMARY KEY NOT NULL,
        repo TEXT NOT NULL,
        sha TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        author_desk TEXT NOT NULL REFERENCES desks(name),
        author_family TEXT NOT NULL CHECK (author_family IN {families}),
        reviewer_desk TEXT NOT NULL REFERENCES desks(name),
        reviewer_family TEXT NOT NULL CHECK (reviewer_family IN {families}),
        verdict TEXT NOT NULL CHECK (verdict IN {review_verdicts}),
        review_path TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY (repo, sha) REFERENCES task_commits(repo, sha),
        CHECK (verdict <> 'PASS' OR author_family <> reviewer_family),
        CHECK (verdict <> 'PASS' OR reviewer_family IN {pass_families})
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS review_passes_lookup ON review_passes(repo, sha)",
    _table(
        """CREATE TABLE IF NOT EXISTS close_tokens (
        id INTEGER PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        token_hash TEXT NOT NULL UNIQUE,
        minted_by TEXT NOT NULL CHECK (minted_by IN {minters}),
        minted_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        CHECK (expires_at > minted_at)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS close_tokens_task ON close_tokens(task_id)",
    _guard("tasks_closed_is_final", "BEFORE UPDATE ON tasks WHEN OLD.status = 'closed'", "closed task is final"),
    _guard(
        "tasks_start_queued",
        "BEFORE INSERT ON tasks WHEN NEW.status <> 'queued' OR NEW.started_at IS NOT NULL",
        "tasks start queued",
    ),
    _guard(
        "tasks_complete_needs_token",
        "BEFORE UPDATE ON tasks WHEN NEW.close_reason = 'complete'"
        " AND NOT EXISTS (SELECT 1 FROM close_tokens WHERE task_id = NEW.id AND consumed_at IS NOT NULL)"
        " AND NOT EXISTS (SELECT 1 FROM tasks AS parent"
        " WHERE parent.id = NEW.parent_task_id AND parent.close_reason = 'complete')",
        "complete needs a consumed close token or a parent that closed complete",
    ),
    _guard("task_commits_immutable", "BEFORE UPDATE ON task_commits", "task commits are immutable"),
    _guard("task_commits_no_delete", "BEFORE DELETE ON task_commits", "task commits are never deleted"),
    _guard(
        "reviews_bound_to_commit",
        "BEFORE INSERT ON review_passes WHEN NOT EXISTS (SELECT 1 FROM task_commits"
        " WHERE repo = NEW.repo AND sha = NEW.sha AND task_id = NEW.task_id)",
        "a review must cite the task that recorded the commit",
    ),
    _guard("tasks_no_delete", "BEFORE DELETE ON tasks", "tasks are never deleted"),
    _guard("desks_immutable", "BEFORE UPDATE ON desks", "desks are immutable"),
    _guard("desks_no_delete", "BEFORE DELETE ON desks", "desks are never deleted"),
    _guard("requests_no_delete", "BEFORE DELETE ON requests", "requests are never deleted"),
    _guard("events_no_delete", "BEFORE DELETE ON events", "events are never deleted"),
    _guard("facts_no_delete", "BEFORE DELETE ON facts", "facts are never deleted"),
    _guard("owls_no_delete", "BEFORE DELETE ON owls", "owls are never deleted"),
    _guard("reviews_immutable", "BEFORE UPDATE ON review_passes", "review passes are immutable"),
    _guard("reviews_no_delete", "BEFORE DELETE ON review_passes", "review passes are never deleted"),
    _guard(
        "close_tokens_fixed",
        "BEFORE UPDATE OF task_id, token_hash, minted_by, minted_at, expires_at ON close_tokens",
        "close token fields are fixed",
    ),
    _guard(
        "close_tokens_single_use",
        "BEFORE UPDATE ON close_tokens WHEN OLD.consumed_at IS NOT NULL",
        "close token already consumed",
    ),
)

_FACT_SHAPE_BROKEN = (
    "(NEW.end_reason IS NULL) <> (NEW.closed_at IS NULL) OR (NEW.end_reason IS NULL) <> (NEW.valid_to IS NULL)"
    " OR NEW.valid_to < NEW.valid_from OR NEW.closed_at < NEW.recorded_at OR NEW.superseded_by = NEW.id"
    " OR (NEW.superseded_by IS NOT NULL AND NEW.end_reason IS NOT 'superseded')"
)

# A (table, column, statement) entry runs only while that column is missing, so rerunning V2 changes nothing.
V2 = (
    ("facts", "subject_key", "ALTER TABLE facts ADD COLUMN subject_key TEXT"),
    ("facts", "valid_from", "ALTER TABLE facts ADD COLUMN valid_from INTEGER"),
    ("facts", "valid_to", "ALTER TABLE facts ADD COLUMN valid_to INTEGER"),
    ("facts", "recorded_at", "ALTER TABLE facts ADD COLUMN recorded_at INTEGER"),
    ("facts", "closed_at", "ALTER TABLE facts ADD COLUMN closed_at INTEGER"),
    ("facts", "end_reason", _enums("ALTER TABLE facts ADD COLUMN end_reason TEXT CHECK (end_reason IN {end_reasons})")),
    ("facts", "superseded_by", "ALTER TABLE facts ADD COLUMN superseded_by INTEGER REFERENCES facts(id)"),
    ("facts", "lookup", f"ALTER TABLE facts ADD COLUMN lookup TEXT CHECK (length(lookup) <= {LOOKUP_LIMIT})"),
    "UPDATE facts SET valid_from = created_at WHERE valid_from IS NULL",
    "UPDATE facts SET recorded_at = created_at WHERE recorded_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS facts_one_current ON facts(scope, subject_key)"
    " WHERE subject_key IS NOT NULL AND valid_to IS NULL AND archived_at IS NULL",
    "CREATE INDEX IF NOT EXISTS facts_subject ON facts(scope, subject_key, valid_from)",
    "CREATE INDEX IF NOT EXISTS facts_superseded_by ON facts(superseded_by)",
    _guard(
        "facts_times_required",
        "BEFORE INSERT ON facts WHEN NEW.valid_from IS NULL OR NEW.recorded_at IS NULL",
        "facts need valid_from and recorded_at",
    ),
    _guard(
        "facts_times_kept",
        "BEFORE UPDATE OF valid_from, recorded_at ON facts WHEN NEW.valid_from IS NULL OR NEW.recorded_at IS NULL",
        "facts need valid_from and recorded_at",
    ),
    _guard("facts_shape_insert", "BEFORE INSERT ON facts WHEN " + _FACT_SHAPE_BROKEN, "fact validity fields disagree"),
    _guard("facts_shape_update", "BEFORE UPDATE ON facts WHEN " + _FACT_SHAPE_BROKEN, "fact validity fields disagree"),
    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, content='facts', content_rowid='id')",
    """CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
    END""",
    "INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')",
)

V3 = (
    ("facts", "restores", "ALTER TABLE facts ADD COLUMN restores INTEGER REFERENCES facts(id)"),
    _guard(
        "facts_restores_fixed",
        "BEFORE UPDATE OF restores ON facts WHEN OLD.restores IS NOT NEW.restores",
        "a fact keeps the row it restores",
    ),
)

# Busy-day capacity: Ryan's temporary cap bumps, which cap stopped a desk, review rounds per author task,
# and a launch record per headless run.
V4 = (
    _table(
        """CREATE TABLE IF NOT EXISTS cap_bumps (
        id INTEGER PRIMARY KEY,
        desk TEXT NOT NULL REFERENCES desks(name),
        kind TEXT NOT NULL CHECK (kind IN {cap_kinds}),
        amount REAL NOT NULL CHECK (amount > 0),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        CHECK (expires_at > created_at)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS cap_bumps_desk ON cap_bumps(desk, expires_at)",
    _table(
        """CREATE TABLE IF NOT EXISTS cap_hits (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        desk TEXT NOT NULL REFERENCES desks(name),
        cap TEXT NOT NULL CHECK (cap IN {cap_hit_caps}),
        cap_source TEXT NOT NULL CHECK (cap_source IN {cap_sources}),
        run_id TEXT,
        CHECK ((cap_source = 'fleet') = (cap <> 'plan'))
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS cap_hits_desk ON cap_hits(desk, ts)",
    _table(
        """CREATE TABLE IF NOT EXISTS round_allowances (
        id INTEGER PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        granted_at INTEGER NOT NULL
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS round_allowances_task ON round_allowances(task_id)",
    _table(
        """CREATE TABLE IF NOT EXISTS review_rounds (
        request_id TEXT PRIMARY KEY NOT NULL REFERENCES requests(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        reviewer TEXT NOT NULL REFERENCES desks(name),
        sha TEXT NOT NULL,
        round INTEGER NOT NULL CHECK (round >= 1),
        allowance_id INTEGER REFERENCES round_allowances(id),
        created_at INTEGER NOT NULL,
        superseded_by TEXT REFERENCES requests(id),
        superseded_at INTEGER,
        review_id TEXT UNIQUE REFERENCES review_passes(id),
        CHECK ((superseded_by IS NULL) = (superseded_at IS NULL)),
        CHECK (superseded_by IS NULL OR superseded_by <> request_id),
        CHECK (superseded_by IS NULL OR review_id IS NULL)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS review_rounds_task ON review_rounds(task_id)",
    _guard("cap_bumps_immutable", "BEFORE UPDATE ON cap_bumps", "cap bumps are immutable"),
    _guard("cap_bumps_no_delete", "BEFORE DELETE ON cap_bumps", "cap bumps are never deleted"),
    _guard("cap_hits_immutable", "BEFORE UPDATE ON cap_hits", "cap hits are immutable"),
    _guard("cap_hits_no_delete", "BEFORE DELETE ON cap_hits", "cap hits are never deleted"),
    _guard("round_allowances_immutable", "BEFORE UPDATE ON round_allowances", "round allowances are immutable"),
    _guard("round_allowances_no_delete", "BEFORE DELETE ON round_allowances", "round allowances are never deleted"),
    _guard(
        "review_rounds_fixed",
        "BEFORE UPDATE OF request_id, task_id, reviewer, sha, round, allowance_id, created_at ON review_rounds",
        "review round fields are fixed",
    ),
    _guard(
        "review_rounds_superseded_once",
        "BEFORE UPDATE ON review_rounds WHEN OLD.superseded_by IS NOT NULL",
        "a superseded review round is final",
    ),
    # A round's verdict is the review_passes row recorded with it, set once and only for that round's commit.
    _guard(
        "review_rounds_open_without_verdict",
        "BEFORE INSERT ON review_rounds WHEN NEW.review_id IS NOT NULL",
        "a review round opens without a verdict",
    ),
    _guard(
        "review_rounds_verdict_once",
        "BEFORE UPDATE OF review_id ON review_rounds WHEN OLD.review_id IS NOT NULL",
        "a review round verdict is final",
    ),
    _guard(
        "review_rounds_verdict_matches",
        "BEFORE UPDATE OF review_id ON review_rounds WHEN NOT EXISTS (SELECT 1 FROM review_passes"
        " WHERE id = NEW.review_id AND task_id = OLD.task_id AND sha = OLD.sha AND reviewer_desk = OLD.reviewer)",
        "a round verdict must be the review of its commit by its reviewer",
    ),
    _guard("review_rounds_no_delete", "BEFORE DELETE ON review_rounds", "review rounds are never deleted"),
    # One row per headless run, written before its process starts, so a run counts toward the daily run cap
    # even when it is killed before it records usage. Its usage is the metrics row tied to it once it ends.
    _table(
        """CREATE TABLE IF NOT EXISTS run_launches (
        run_id TEXT PRIMARY KEY NOT NULL,
        desk TEXT NOT NULL REFERENCES desks(name),
        model TEXT NOT NULL,
        launched_at INTEGER NOT NULL,
        metric_id INTEGER UNIQUE REFERENCES metrics(id)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS run_launches_desk ON run_launches(desk, launched_at)",
    _guard(
        "run_launches_fixed",
        "BEFORE UPDATE OF run_id, desk, model, launched_at ON run_launches",
        "run launch fields are fixed",
    ),
    _guard(
        "run_launches_open_without_usage",
        "BEFORE INSERT ON run_launches WHEN NEW.metric_id IS NOT NULL",
        "a run launch opens without usage",
    ),
    _guard(
        "run_launches_usage_once",
        "BEFORE UPDATE OF metric_id ON run_launches WHEN OLD.metric_id IS NOT NULL",
        "a run launch usage is final",
    ),
    _guard(
        "run_launches_usage_matches",
        "BEFORE UPDATE OF metric_id ON run_launches WHEN NOT EXISTS (SELECT 1 FROM metrics"
        " WHERE id = NEW.metric_id AND desk = OLD.desk AND run_id = OLD.run_id)",
        "a run launch usage must be the metrics row of that run",
    ),
    _guard("run_launches_no_delete", "BEFORE DELETE ON run_launches", "run launches are never deleted"),
)

# Ollivander, the model keeper. model_lines is Ryan's filing of model names (latest row per name wins),
# model_catalog the names each family offered at the last look (with whether the catalog listed each, the
# line it was filed under then, and when it retires), desk_models each desk's current model
# (the desks table is immutable), model_changes the history of every switch, and model_resolutions every
# full Claude id a run on each alias reported (seq orders the sightings, so the latest is known).
V5 = (
    _table(
        """CREATE TABLE IF NOT EXISTS model_lines (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        line TEXT NOT NULL CHECK (line IN {model_lines}),
        classified_at INTEGER NOT NULL
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS model_lines_name ON model_lines(name, id)",
    _guard("model_lines_immutable", "BEFORE UPDATE ON model_lines", "model lines are immutable"),
    _guard("model_lines_no_delete", "BEFORE DELETE ON model_lines", "model lines are never deleted"),
    _table(
        """CREATE TABLE IF NOT EXISTS model_catalog (
        family TEXT NOT NULL CHECK (family IN {model_families}),
        name TEXT NOT NULL,
        seen_at INTEGER NOT NULL,
        visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
        line TEXT CHECK (line IN {needs}),
        retires_at INTEGER CHECK (retires_at >= 0),
        PRIMARY KEY (family, name)
    )"""
    ),
    _table(
        """CREATE TABLE IF NOT EXISTS desk_models (
        desk TEXT PRIMARY KEY NOT NULL REFERENCES desks(name),
        need TEXT CHECK (need IN {needs}),
        model TEXT,
        effort TEXT CHECK (effort IN {efforts}),
        line TEXT CHECK (line IN {needs}),
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        pending_model TEXT,
        pending_effort TEXT CHECK (pending_effort IN {efforts}),
        pending_line TEXT CHECK (pending_line IN {needs}),
        previous_model TEXT,
        previous_effort TEXT CHECK (previous_effort IN {efforts}),
        previous_line TEXT CHECK (previous_line IN {needs}),
        trial_failures INTEGER CHECK (trial_failures >= 0),
        trial_end TEXT CHECK (trial_end IN {trial_ends}),
        changed_at INTEGER,
        updated_at INTEGER NOT NULL,
        CHECK ((pending_model IS NULL) = (pending_line IS NULL))
    )"""
    ),
    _guard("desk_models_no_delete", "BEFORE DELETE ON desk_models", "desk models are never deleted"),
    _table(
        """CREATE TABLE IF NOT EXISTS model_changes (
        id INTEGER PRIMARY KEY,
        desk TEXT NOT NULL REFERENCES desks(name),
        ts INTEGER NOT NULL,
        from_model TEXT,
        to_model TEXT,
        effort TEXT CHECK (effort IN {efforts}),
        reason TEXT NOT NULL CHECK (reason IN {change_reasons})
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS model_changes_desk ON model_changes(desk, id)",
    _guard("model_changes_immutable", "BEFORE UPDATE ON model_changes", "model changes are immutable"),
    _guard("model_changes_no_delete", "BEFORE DELETE ON model_changes", "model changes are never deleted"),
    _table(
        """CREATE TABLE IF NOT EXISTS model_resolutions (
        id INTEGER PRIMARY KEY,
        alias TEXT NOT NULL,
        full_id TEXT NOT NULL,
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL CHECK (last_seen >= first_seen),
        seq INTEGER NOT NULL,
        UNIQUE (alias, full_id)
    )"""
    ),
    "CREATE INDEX IF NOT EXISTS model_resolutions_alias ON model_resolutions(alias, seq)",
    _guard("model_resolutions_fixed", "BEFORE UPDATE OF id, alias, full_id, first_seen ON model_resolutions",
           "a model resolution only moves its last sighting"),
    _guard("model_resolutions_no_delete", "BEFORE DELETE ON model_resolutions", "model resolutions are never deleted"),
    "CREATE INDEX IF NOT EXISTS metrics_desk ON metrics(desk, id)",
)

# Catalog looks: each look at a family's catalog gets the next look number for that family, so the latest
# look is the highest number even when two looks land in the same second. Existing rows are numbered by
# their seen_at order within each family. A row's look number only moves forward.
V6 = (
    ("model_catalog", "look", "ALTER TABLE model_catalog ADD COLUMN look INTEGER CHECK (look >= 1)"),
    "UPDATE model_catalog SET look = (SELECT COUNT(DISTINCT older.seen_at) FROM model_catalog AS older"
    " WHERE older.family = model_catalog.family AND older.seen_at <= model_catalog.seen_at) WHERE look IS NULL",
    "CREATE INDEX IF NOT EXISTS model_catalog_look ON model_catalog(family, look)",
    _guard("model_catalog_look_required", "BEFORE INSERT ON model_catalog WHEN NEW.look IS NULL",
           "a catalog row needs its look number"),
    _guard("model_catalog_look_forward",
           "BEFORE UPDATE ON model_catalog WHEN NEW.look IS NULL OR NEW.look < OLD.look",
           "a catalog look number only moves forward"),
)

# Many tasks per desk: a desk listed in many_task_desks may hold any number of active tasks, and every other
# desk still holds at most one. The grant is one way, and never reaches McGonagall, Snape, Dumbledore, Ryan or a
# script. The one-per-desk index becomes a trigger that reads the
# grant, so a raw write on a single desk is still refused, and a task keeps its desk, so no write moves an active
# task onto a single desk either. A run launch names the task it ran for, if any. An own-session review task
# records the branch its review lineage follows (review_branch), set only while the task is active and never
# cleared. It is 1 to REVIEW_BRANCH_MAX bytes of printable ASCII with no whitespace (a text value, so no NUL or
# multibyte character hides from the GLOB); pensieve and the review script hold it to git's own branch rules.
# Rows from before V7 keep it NULL, which the review script reads as a lineage it cannot place, so such a
# task is continued with --task or closed rather than silently left behind.
V7 = (
    _table(
        """CREATE TABLE IF NOT EXISTS many_task_desks (
        desk TEXT PRIMARY KEY NOT NULL REFERENCES desks(name),
        granted_at INTEGER NOT NULL
    )"""
    ),
    _guard("many_task_desks_immutable", "BEFORE UPDATE ON many_task_desks", "desk task modes are fixed"),
    _guard("many_task_desks_no_delete", "BEFORE DELETE ON many_task_desks", "desk task modes are never deleted"),
    _guard(
        "many_task_desks_not_single",
        "BEFORE INSERT ON many_task_desks WHEN NEW.desk IN " + _choices(SINGLE_TASK_DESKS)
        + " OR EXISTS (SELECT 1 FROM desks WHERE name = NEW.desk AND family IN " + _choices(SINGLE_TASK_FAMILIES) + ")",
        "this desk keeps one active task at a time",
    ),
    "INSERT OR IGNORE INTO many_task_desks(desk, granted_at) SELECT name, CAST(strftime('%s', 'now') AS INTEGER)"
    " FROM desks WHERE name IN " + _choices(MANY_TASK_DESKS_SEED),
    "DROP INDEX IF EXISTS tasks_one_active_per_desk",
    _guard(
        "tasks_one_active_per_single_desk",
        "BEFORE UPDATE OF status ON tasks WHEN NEW.status = 'active' AND OLD.status <> 'active'"
        " AND NOT EXISTS (SELECT 1 FROM many_task_desks WHERE desk = NEW.desk)"
        " AND EXISTS (SELECT 1 FROM tasks WHERE desk = NEW.desk AND status = 'active' AND id <> NEW.id)",
        "desk already has an active task",
    ),
    _guard("tasks_desk_fixed", "BEFORE UPDATE OF desk ON tasks WHEN OLD.desk IS NOT NEW.desk", "a task keeps its desk"),
    ("run_launches", "task_id", "ALTER TABLE run_launches ADD COLUMN task_id TEXT REFERENCES tasks(id)"),
    _guard(
        "run_launches_task_fixed",
        "BEFORE UPDATE OF task_id ON run_launches WHEN OLD.task_id IS NOT NEW.task_id",
        "a run launch keeps its task",
    ),
    _guard(
        "run_launches_task_of_desk",
        "BEFORE INSERT ON run_launches WHEN NEW.task_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id AND desk = NEW.desk)",
        "a run launch names a task of its own desk",
    ),
    "CREATE INDEX IF NOT EXISTS run_launches_task ON run_launches(task_id)",
    ("tasks", "review_branch", "ALTER TABLE tasks ADD COLUMN review_branch TEXT CHECK (review_branch IS NULL OR"
     " (typeof(review_branch) = 'text' AND length(CAST(review_branch AS BLOB)) BETWEEN 1 AND"
     f" {REVIEW_BRANCH_MAX} AND length(review_branch) = length(CAST(review_branch AS BLOB))"
     " AND review_branch NOT GLOB '*[^!-~]*'))"),
    _guard("tasks_review_branch_new", "BEFORE INSERT ON tasks WHEN NEW.review_branch IS NOT NULL",
           "a review branch is set on an active task"),
    _guard(
        "tasks_review_branch_active",
        "BEFORE UPDATE OF review_branch ON tasks WHEN OLD.review_branch IS NOT NEW.review_branch"
        " AND (OLD.status <> 'active' OR NEW.review_branch IS NULL)",
        "a review branch is set on an active task and never cleared",
    ),
)

# Run slots: a desk may run as many model processes at once as it has run slots (fleet RUN_SLOTS), each holding
# one slot's lock. A review round records the slot its review held when it opened, in the row that opens it, so a
# review that later finds the round's reviewer task still active knows whose lock to try before it closes that task.
# It is NULL for a round opened while every slot was busy and for every round from before V8, and the review script
# reads NULL as slot 0, whose lock is the desk lock of old. A round keeps its slot.
V8 = (
    ("review_rounds", "slot", "ALTER TABLE review_rounds ADD COLUMN slot INTEGER CHECK (slot IS NULL OR slot BETWEEN 0"
     f" AND {RUN_SLOT_LIMIT - 1})"),
    _guard("review_rounds_slot_fixed", "BEFORE UPDATE OF slot ON review_rounds WHEN OLD.slot IS NOT NEW.slot",
           "a review round keeps its slot"),
)

# Go specs: when Ryan types "go <task-id>" on a drafted TASK.md, the hook registers the task and records, in the
# same transaction, the repo folder, branch and base its Spec names and the sha256 of the TASK.md bytes it read.
# The build's worktree is made from these stored values only, never from the file again. One row per registered
# task, written while the task is queued and has its TASK.md, and never changed or deleted. Tasks registered any
# other way have none.
V9 = (
    _table(
        f"""CREATE TABLE IF NOT EXISTS task_specs (
        task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id),
        repo_dir TEXT NOT NULL CHECK (length(repo_dir) BETWEEN 2 AND {SPEC_PATH_MAX}
            AND substr(repo_dir, 1, 1) = '/' AND repo_dir NOT GLOB '*[^ -~]*'),
        branch TEXT NOT NULL CHECK (length(branch) BETWEEN 1 AND {SPEC_BRANCH_MAX}
            AND branch GLOB '[a-z0-9]*' AND branch NOT GLOB '*[^a-z0-9._/-]*'),
        base TEXT NOT NULL CHECK (length(base) BETWEEN 1 AND {SPEC_BASE_MAX}
            AND base GLOB '[A-Za-z0-9]*' AND base NOT GLOB '*[^A-Za-z0-9._/-]*'),
        intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'),
        recorded_at INTEGER NOT NULL
    )"""
    ),
    _guard("task_specs_immutable", "BEFORE UPDATE ON task_specs", "a task keeps the spec it was started with"),
    _guard("task_specs_no_delete", "BEFORE DELETE ON task_specs", "task specs are never deleted"),
    _guard(
        "task_specs_on_a_new_task",
        "BEFORE INSERT ON task_specs WHEN NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id"
        " AND status = 'queued' AND intent_path IS NOT NULL)",
        "a spec is recorded on a queued task with its TASK.md",
    ),
)


def _id_list(column: str) -> str:
    """A comma list of patch op ids: lowercase letters, digits, hyphens and commas, in printable ASCII, or empty."""
    return (f"{column} IS NULL OR (length(CAST({column} AS BLOB)) <= {AUTO_PATCH_IDS_MAX}"
            f" AND length({column}) = length(CAST({column} AS BLOB)) AND {column} NOT GLOB '*[^a-z0-9,-]*')")


# Auto-portrait: one row per local date the nightly job took on with the opt-in on. It is armed under Dumbledore's run
# slot, before his run, with what his outbox held for the date (before, and its sha256 when present). After a clean
# run it takes one snapshot of the patch: its sha256, the checked additions as canonical ASCII JSON (no raw patch
# bytes ever), and the op ids in patch order, held for Ryan and out of schema. The additions are applied from the
# snapshot, never from the file again, in the transaction that ends the night done. A night ends done, stopped or
# off with one outcome line. A night that took no snapshot can be armed again, with the next attempt number; a
# snapshot is written once, a date never changes, done is final, and rows are never deleted.
AUTO_PATCHES = (
    _table(
        f"""CREATE TABLE IF NOT EXISTS auto_patches (
        date TEXT PRIMARY KEY NOT NULL CHECK (length(date) = 10
            AND date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        state TEXT NOT NULL CHECK (state IN {{auto_states}}),
        owl_id TEXT NOT NULL REFERENCES owls(id),
        armed_at INTEGER NOT NULL,
        before TEXT NOT NULL CHECK (before IN {{auto_before}}),
        before_sha256 TEXT CHECK (before_sha256 IS NULL OR (length(before_sha256) = 64
            AND before_sha256 NOT GLOB '*[^0-9a-f]*')),
        sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
        ops TEXT CHECK (ops IS NULL OR (length(CAST(ops AS BLOB)) BETWEEN 2 AND {AUTO_PATCH_OPS_MAX}
            AND length(ops) = length(CAST(ops AS BLOB)) AND ops NOT GLOB '*[^ -~]*')),
        order_ids TEXT CHECK (order_ids IS NULL OR length(order_ids) >= 1) CHECK ({_id_list("order_ids")}),
        held_ids TEXT CHECK ({_id_list("held_ids")}),
        unfit_ids TEXT CHECK ({_id_list("unfit_ids")}),
        applied_ids TEXT CHECK ({_id_list("applied_ids")}),
        outcome TEXT CHECK (outcome IS NULL OR (length(CAST(outcome AS BLOB)) BETWEEN 1 AND {AUTO_PATCH_OUTCOME_MAX}
            AND length(outcome) = length(CAST(outcome AS BLOB)) AND outcome NOT GLOB '*[^ -~]*')),
        validated_at INTEGER,
        finished_at INTEGER,
        CHECK ((before = 'present') = (before_sha256 IS NOT NULL)),
        CHECK ((sha256 IS NULL) = (ops IS NULL) AND (ops IS NULL) = (order_ids IS NULL)
            AND (order_ids IS NULL) = (held_ids IS NULL) AND (held_ids IS NULL) = (unfit_ids IS NULL)
            AND (unfit_ids IS NULL) = (validated_at IS NULL)),
        CHECK (state <> 'armed' OR sha256 IS NULL),
        CHECK (state <> 'validated' OR sha256 IS NOT NULL),
        CHECK ((finished_at IS NULL) = (state IN ('armed', 'validated'))),
        CHECK ((outcome IS NULL) = (finished_at IS NULL)),
        CHECK (applied_ids IS NULL OR state = 'done')
    )"""
    ),
    _guard("auto_patches_no_delete", "BEFORE DELETE ON auto_patches", "auto-portrait nights are never deleted"),
    _guard("auto_patches_date_fixed", "BEFORE UPDATE OF date ON auto_patches WHEN OLD.date IS NOT NEW.date",
           "an auto-portrait night keeps its date"),
    _guard(
        "auto_patches_open_armed",
        "BEFORE INSERT ON auto_patches WHEN NEW.state IS NOT 'armed' OR NEW.attempt IS NOT 1"
        " OR NEW.sha256 IS NOT NULL OR NEW.ops IS NOT NULL OR NEW.order_ids IS NOT NULL OR NEW.held_ids IS NOT NULL"
        " OR NEW.unfit_ids IS NOT NULL OR NEW.validated_at IS NOT NULL OR NEW.applied_ids IS NOT NULL"
        " OR NEW.outcome IS NOT NULL OR NEW.finished_at IS NOT NULL",
        "an auto-portrait night opens armed, as attempt 1, with no snapshot and no ending",
    ),
    _guard(
        "auto_patches_snapshot_once",
        "BEFORE UPDATE ON auto_patches WHEN (OLD.sha256 IS NOT NULL AND (NEW.sha256 IS NOT OLD.sha256"
        " OR NEW.ops IS NOT OLD.ops OR NEW.order_ids IS NOT OLD.order_ids OR NEW.held_ids IS NOT OLD.held_ids"
        " OR NEW.unfit_ids IS NOT OLD.unfit_ids OR NEW.validated_at IS NOT OLD.validated_at))"
        " OR (OLD.sha256 IS NULL AND NEW.sha256 IS NOT NULL AND (OLD.state IS NOT 'armed' OR NEW.state IS NOT 'validated'))",
        "an auto-portrait snapshot is written once, as an armed night is validated",
    ),
    _guard(
        "auto_patches_forward",
        "BEFORE UPDATE OF state ON auto_patches WHEN OLD.state IS NOT NEW.state AND NOT (OLD.state = 'armed'"
        " OR (OLD.state = 'validated' AND NEW.state IN ('done', 'stopped', 'off'))"
        " OR (OLD.state IN ('stopped', 'off') AND NEW.state = 'armed' AND OLD.sha256 IS NULL))",
        "an auto-portrait night only moves forward",
    ),
    _guard(
        "auto_patches_rearm",
        "BEFORE UPDATE ON auto_patches WHEN ((NEW.attempt IS NOT OLD.attempt OR NEW.armed_at IS NOT OLD.armed_at"
        " OR NEW.owl_id IS NOT OLD.owl_id OR NEW.before IS NOT OLD.before"
        " OR NEW.before_sha256 IS NOT OLD.before_sha256 OR (NEW.state = 'armed' AND OLD.state IS NOT 'armed'))"
        " AND NOT (NEW.state = 'armed' AND OLD.state IN ('armed', 'stopped', 'off') AND OLD.sha256 IS NULL"
        " AND NEW.attempt = OLD.attempt + 1))",
        "an auto-portrait night is armed again only with no snapshot, as its next attempt",
    ),
    _guard(
        "auto_patches_ending_fixed",
        "BEFORE UPDATE ON auto_patches WHEN (NEW.outcome IS NOT OLD.outcome OR NEW.applied_ids IS NOT OLD.applied_ids"
        " OR NEW.finished_at IS NOT OLD.finished_at) AND NOT ((OLD.state IN ('armed', 'validated')"
        " AND NEW.state IN ('done', 'stopped', 'off')) OR (OLD.state IN ('stopped', 'off') AND NEW.state = 'armed'))",
        "an auto-portrait ending is written with the final state and cleared only by arming the night again",
    ),
)

MIGRATIONS = ((1, V1), (2, V2), (3, V3), (4, V4), (5, V5), (6, V6), (7, V7), (8, V8), (9, V9), (10, AUTO_PATCHES))


def _uid() -> int:
    return os.getuid()


def _absolute(path: PathLike) -> Path:
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValidationError("database path must be absolute")
    return Path(path)


def _problems(st: os.stat_result, label: str, want_dir: bool) -> list[str]:
    if stat.S_ISLNK(st.st_mode):
        return [f"{label} is a symlink"]
    found = []
    if want_dir and not stat.S_ISDIR(st.st_mode):
        found.append(f"{label} is not a directory")
    if not want_dir and not stat.S_ISREG(st.st_mode):
        found.append(f"{label} is not a regular file")
    if st.st_uid != _uid():
        found.append(f"{label} is not owned by the current user")
    if st.st_mode & 0o022:
        found.append(f"{label} is group or world writable")
    return found


def _require_safe(path: Path, label: str, want_dir: bool) -> None:
    found = _problems(os.lstat(path), label, want_dir)
    if found:
        raise IntegrityError(found[0])


def _prepare_parent(parent: Path, create: bool) -> None:
    if not os.path.lexists(parent):
        if not create:
            raise NotFoundError("database directory does not exist, run castle init")
        try:
            os.mkdir(parent, 0o700)
        except FileNotFoundError:
            raise NotFoundError("the directory above the database directory does not exist") from None
        except FileExistsError:
            pass
        else:
            os.chmod(parent, 0o700, follow_symlinks=False)
    _require_safe(parent, "database directory", want_dir=True)


def _prepare_file(path: Path, create: bool) -> None:
    if not os.path.lexists(path):
        if not create:
            raise NotFoundError("database does not exist, run castle init")
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        except FileExistsError:
            pass
    _require_safe(path, "database file", want_dir=False)
    if os.lstat(path).st_mode & 0o077:
        os.chmod(path, 0o600, follow_symlinks=False)


def tighten_sidecars(path: Path) -> None:
    for suffix in SIDECARS:
        sidecar = Path(str(path) + suffix)
        if not os.path.lexists(sidecar):
            continue
        _require_safe(sidecar, f"database {suffix[1:]} file", want_dir=False)
        if os.lstat(sidecar).st_mode & 0o077:
            os.chmod(sidecar, 0o600, follow_symlinks=False)


def _unsafe_sidecar(path: Path) -> Optional[IntegrityError]:
    for suffix in SIDECARS:
        try:
            found = _problems(os.lstat(str(path) + suffix), f"database {suffix[1:]} file", want_dir=False)
        except FileNotFoundError:
            continue
        if found:
            return IntegrityError(found[0])
    return None


@contextmanager
def _closed_on_error(conn: sqlite3.Connection, path: Path) -> Iterator[None]:
    # SQLite reports a planted -wal or -shm only as "unable to open", so name the unsafe sidecar instead.
    try:
        yield
    except sqlite3.OperationalError as exc:
        conn.close()
        mapped = _busy(exc) or _unsafe_sidecar(path)
        if mapped is None:
            raise
        raise mapped from exc
    except BaseException:
        conn.close()
        raise


def _enable_wal(conn: sqlite3.Connection) -> None:
    # Switching journal mode never calls the busy handler, so racing first opens must retry.
    for attempt in range(WAL_ATTEMPTS):
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _busy(exc) is None:
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))
            continue
        if str(mode).lower() != "wal":
            raise IntegrityError("could not enable WAL journal mode")
        return
    raise ConflictError("database is busy, try again")


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    with _closed_on_error(conn, path):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def connect(path: PathLike, create: bool = True) -> sqlite3.Connection:
    db_path = _absolute(path)
    _prepare_parent(db_path.parent, create)
    _prepare_file(db_path, create)
    tighten_sidecars(db_path)
    conn = _open(db_path)
    with _closed_on_error(conn, db_path):
        migrate(conn)
        tighten_sidecars(db_path)
    return conn


def connect_readonly(path: PathLike) -> sqlite3.Connection:
    """Open an existing database for reading only: never created, migrated or chmodded.

    SQLite opens the file with mode=ro and query_only refuses writes on top of that. It still
    shares the WAL index (-shm) with the writers, which is how a reader sees their commits.
    """
    db_path = _absolute(path)
    if not os.path.lexists(db_path.parent):
        raise NotFoundError("database directory does not exist, run castle init")
    _require_safe(db_path.parent, "database directory", want_dir=True)
    if not os.path.lexists(db_path):
        raise NotFoundError("database does not exist, run castle init")
    _require_safe(db_path, "database file", want_dir=False)
    unsafe = _unsafe_sidecar(db_path)
    if unsafe is not None:
        raise unsafe
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    with _closed_on_error(conn, db_path):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    known = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if known is None:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    if schema_version(conn) == SCHEMA_VERSION:
        return SCHEMA_VERSION
    with transaction(conn):
        conn.execute(SCHEMA_VERSION_TABLE)
        current = schema_version(conn)
        if current > SCHEMA_VERSION:
            raise IntegrityError("database schema is newer than this code")
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            for statement in pending_statements(conn, statements):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )
    return SCHEMA_VERSION


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return fetch_one(conn, "SELECT 1 AS found FROM pragma_table_info(?) WHERE name = ?", (table, column)) is not None


def pending_statements(conn: sqlite3.Connection, statements: Sequence) -> Iterator[str]:
    for statement in statements:
        if isinstance(statement, tuple):
            table, column, statement = statement
            if _has_column(conn, table, column):
                continue
        yield statement


def _busy(exc: sqlite3.OperationalError) -> Optional[ConflictError]:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return ConflictError("database is busy, try again")
    return None


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


_WRITERS: dict = {}


def in_write_transaction(conn: sqlite3.Connection) -> bool:
    return conn.in_transaction and _WRITERS.get(id(conn)) is conn


def _begin(conn: sqlite3.Connection, write: bool) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
    except sqlite3.OperationalError as exc:
        busy = _busy(exc)
        if busy is None:
            raise
        raise busy from exc


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    # A nested write undoes its own changes when it fails, even if the caller catches the error and commits.
    conn.execute("SAVEPOINT nested_write")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO nested_write")
            conn.execute("RELEASE nested_write")
        raise
    conn.execute("RELEASE nested_write")


@contextmanager
def _guarded(conn: sqlite3.Connection, write: bool) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        if write and not in_write_transaction(conn):
            raise StoreError("writes cannot run inside a snapshot or a transaction the caller opened")
        if write:
            with _savepoint(conn):
                yield conn
        else:
            yield conn
        return
    _begin(conn, write)
    if write:
        _WRITERS[id(conn)] = conn
    try:
        yield conn
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        _rollback(conn)
        raise IntegrityError(str(exc)[:200]) from exc
    except BaseException:
        _rollback(conn)
        raise
    finally:
        if write:
            _WRITERS.pop(id(conn), None)


def transaction(conn: sqlite3.Connection):
    return _guarded(conn, write=True)


def snapshot(conn: sqlite3.Connection):
    return _guarded(conn, write=False)


def fetch_one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> Optional[dict]:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


def fetch_all(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _fts5_available() -> bool:
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        return True
    except sqlite3.Error:
        return False
    finally:
        probe.close()


def _describe(path: Path, label: str, want_dir: bool) -> dict:
    if not os.path.lexists(path):
        return {"path": str(path), "exists": False, "problems": [f"{label} does not exist"]}
    st = os.lstat(path)
    return {
        "path": str(path),
        "exists": True,
        "mode": oct(stat.S_IMODE(st.st_mode)),
        "problems": _problems(st, label, want_dir),
    }


def stray_bytecode(root: Path) -> list[str]:
    found = []
    for current, dirs, files in os.walk(root):
        here = Path(current).relative_to(root)
        if here == Path("."):
            dirs[:] = [name for name in dirs if name not in (".git", "state")]
        found += [str(here / name) for name in dirs if name == "__pycache__"]
        found += [str(here / name) for name in files if name.endswith(BYTECODE_SUFFIXES)]
        dirs[:] = [name for name in dirs if name != "__pycache__"]
    return sorted(found)


def _describe_wrapper(root: Path, name: str, module: str) -> dict:
    path = root / "bin" / name
    label = f"{name} wrapper"
    check = _describe(path, label, False)
    if not check["exists"]:
        return check
    if check["mode"] != "0o700":
        check["problems"].append(f"{label} mode is {check['mode']}, expected 0o700")
    if not check["problems"]:
        expected = (
            "#!/bin/sh\n"
            "exec /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty "
            "-c 'import sys; sys.path.insert(0, \"/Users/crisryantan/.hogwarts\"); "
            f"from {module} import main; sys.exit(main())' \"$@\"\n"
        ).encode("utf-8")
        try:
            if path.read_bytes() != expected:
                check["problems"].append(f"{label} content differs from the expected wrapper")
        except OSError as exc:
            check["problems"].append(f"{label} cannot be read: {exc}")
    return check


def doctor(path: PathLike, code_root: Optional[PathLike] = None) -> dict:
    db_path = _absolute(path)
    checks = {
        "directory": _describe(db_path.parent, "database directory", True),
        "database": _describe(db_path, "database file", False),
    }
    for suffix in SIDECARS:
        sidecar = Path(str(db_path) + suffix)
        if os.path.lexists(sidecar):
            checks[suffix[1:]] = _describe(sidecar, f"database {suffix[1:]} file", False)
    problems = [problem for check in checks.values() for problem in check["problems"]]
    report = {"db": str(db_path), "checks": checks, "fts5": _fts5_available()}
    if not report["fts5"]:
        problems.append("FTS5 is not available")
    if not problems:
        report.update(_inspect_database(db_path, problems))
    root = _absolute(CODE_ROOT if code_root is None else code_root)
    report["bytecode"] = stray_bytecode(root)
    problems += [f"bytecode can load in place of reviewed source: {item}" for item in report["bytecode"]]
    report["wrappers"] = {
        name: _describe_wrapper(root, name, module)
        for name, module in (("castle", "hogwarts.cli"), ("fleet", "fleet.tools"))
    }
    problems += [problem for check in report["wrappers"].values() for problem in check["problems"]]
    report["problems"] = problems
    report["ok"] = not problems
    return report


def _inspect_database(path: Path, problems: list[str]) -> dict:
    conn = _open(path)
    try:
        version = schema_version(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if version != SCHEMA_VERSION:
        problems.append(f"schema version is {version}, expected {SCHEMA_VERSION}")
    if integrity != "ok":
        problems.append("integrity check failed")
    if fk_violations:
        problems.append(f"{fk_violations} foreign key violations")
    return {
        "schema_version": version,
        "expected_schema_version": SCHEMA_VERSION,
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "journal_mode": journal,
    }
