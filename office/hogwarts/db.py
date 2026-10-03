from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union

from .errors import ConflictError, IntegrityError, NotFoundError, StoreError, ValidationError

DEFAULT_DB = Path("/Users/crisryantan/.hogwarts/state/pensieve.db")
CODE_ROOT = Path(os.path.abspath(__file__)).parent.parent
SCHEMA_VERSION = 3
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

MIGRATIONS = ((1, V1), (2, V2), (3, V3))


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
    report["bytecode"] = stray_bytecode(_absolute(CODE_ROOT if code_root is None else code_root))
    problems += [f"bytecode can load in place of reviewed source: {item}" for item in report["bytecode"]]
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
