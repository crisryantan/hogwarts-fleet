"""Fixed locations and settings for the fleet scripts and hooks.

Every location is a module constant. Code reads them at call time through this
module, so tests point them at temp folders with mock.patch.object. Nothing here
comes from the environment.
"""
from __future__ import annotations

# Where things live. The office holds the store and every control; the castle is where desks work.
OFFICE_ROOT = "/Users/crisryantan/.hogwarts"
CASTLE_ROOT = "/Users/crisryantan/hogwarts"
DB_PATH = "/Users/crisryantan/.hogwarts/state/pensieve.db"
TRANSCRIPTS_ROOT = "/Users/crisryantan/.claude/projects"
USER_HOME_DIR = "/Users/crisryantan"

# Absolute binaries. No PATH lookup.
CLAUDE_BIN = "/Users/crisryantan/.local/bin/claude"
CODEX_BIN = "/opt/homebrew/bin/codex"
PYTHON_WRAPPER = ("/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty")
GIT_BIN = "/usr/bin/git"
BASH_BIN = "/bin/bash"
CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

# Filled in at onboarding (docs/ONBOARDING.md stage 2). Later-stage scripts such as the Map read these.
GITHUB_ACCOUNT = "<github-account>"
WATCHED_REPOS = ("<repos-to-watch>",)

# Desks with a folder in the castle (desks/<name>/inbox, outbox, scratchpad.md).
CASTLE_DESKS = ("mcgonagall", "harry", "hermione", "moody", "ron", "snape", "portrait")
# Desks a person drives. The Owl Post rings them with a routine event.
# Snape is a subagent that Ryan summons, so owls to him are refused.
INTERACTIVE_DESKS = ("mcgonagall", "ryan-claude-1")
# Desks run_desk can launch. The registry family must match.
HEADLESS_CLAUDE = ("hermione", "ron", "portrait")
HEADLESS_CODEX = ("harry", "moody")
HEADLESS_DESKS = HEADLESS_CLAUDE + HEADLESS_CODEX
# The desk the castle hooks speak for, unless --desk names another interactive desk.
HOOK_DESK = "mcgonagall"
# Sessions in the castle that are not McGonagall's (hook input agent_type differs) are recorded as Ryan's own.
OWN_SESSION_DESK = "ryan-claude-1"
# Only these desks may open a request to a headless desk, and only those requests start a run.
WAKE_SENDERS = ("mcgonagall",)
# Desks whose startup digest covers the whole fleet, not only their own tasks.
FLEET_VIEW_DESKS = ("mcgonagall",)

# Headless launch settings.
CLAUDE_TOOLS = {
    "hermione": "Read,Grep,Glob,Write,Edit,Bash",
    "ron": "Read,Grep,Glob,Write,Edit,Bash",
    "portrait": "Read,Grep,Glob,Write,Edit",
}
MAX_BUDGET_USD = {"hermione": "2.00", "ron": "0.25", "portrait": "2.00"}
# Codex desks run under a fleet permission profile, never --sandbox: on 0.160.0 the --sandbox modes
# let commands read the whole disk, and only a profile can limit reads. The profile is an allowlist:
# the platform paths tools need, the reads below, the desk's working folder with this access,
# its own castle folder to read, the castle tasks to read, its repo's .git to read, and no network.
CODEX_ACCESS = {"harry": "write", "moody": "read"}
CODEX_EXTRA_READS = ("/opt/homebrew",)
# Only these desks may write their own outbox.
CODEX_OUTBOX_WRITERS = ("harry",)
# Desks that build in a worktree. The Owl Post starts them only once their task has one.
WORKTREE_DESKS = ("harry",)
# Reviewer for each author family. A pass needs the other family.
REVIEWER_FOR_FAMILY = {"codex": "hermione", "claude": "moody"}
# A Codex desk with no worktree of its own runs here, never in its desk folder.
CODEX_WORK_DIR = "work"
# Castle folders each Claude desk may read through --add-dir. Writes there are denied by its settings.
CLAUDE_READ_DIRS = {"hermione": ("tasks", "worktrees"), "ron": ("tasks",), "portrait": ()}
# Runs per desk in any 24 hours, and spend for the Claude desks, read from the store's metrics.
DAILY_RUN_CAP = {"hermione": 20, "ron": 40, "portrait": 3, "harry": 10, "moody": 20}
DAILY_SPEND_CAP_USD = {"hermione": 20.0, "ron": 5.0, "portrait": 4.0}
DAY_SECONDS = 86400
# A run waits this long for another run of the same desk to finish.
DESK_LOCK_WAIT_SECONDS = 1860
RUN_TIMEOUT_SECONDS = 1800
ENABLED_MARKER = "enabled"
CLAUDE_SETTINGS_FILE = "settings.json"
CODEX_PROFILE_FILE = "codex.toml"
BRIEF_FILE = "BRIEF.md"
MCP_JOB_PREFIX = "mcp-"

# Git, verify and review scripts.
GIT_TIMEOUT_SECONDS = 300
VERIFY_TIMEOUT_SECONDS = 900
VERIFY_OUTPUT_MAX_BYTES = 262144
EVIDENCE_EXCERPT_LINES = 40
DEFAULT_BASE = "origin/main"
# Words that never leave the fleet: in branch names, commit messages and PR text.
FLEET_WORDS = ("hogwarts", "mcgonagall", "harry", "hermione", "moody", "ron", "snape", "dumbledore",
               "marauder", "marauders", "gringotts", "owlpost", "headmaster", "pensieve")

# Size limits.
OWL_MAX_BYTES = 65536
BODY_FILE_MAX_BYTES = 65536
BRIEF_MAX_BYTES = 65536
SETTINGS_MAX_BYTES = 65536
INBOX_COPY_MAX_BYTES = 262144
HOOK_INPUT_MAX_BYTES = 8 * 1024 * 1024
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
SCRATCHPAD_BUDGET_BYTES = 6144
# An owl file younger than this that does not parse yet may still be being written.
OWL_SETTLE_SECONDS = 2

# Hook budgets.
DIGEST_MAX_LINES = 39
INFLIGHT_CAP = 10
DIGEST_EVENT_LINES = 8
QUEUED_CAP = 20
DRAIN_MAX_CHARS = 1500
TEMPUS_THRESHOLD = 200_000
EXTRACT_CAP = 4000
SESSION_CAP = 16000
CLOSE_TOKEN_TTL = 60
# Transcript "entrypoint" values that mark a session a person is typing into.
# Undocumented field: anything else, or no value at all, refuses the close.
CLOSE_ALLOWED_ENTRYPOINTS = ("cli", "claude-desktop")
# The prompt's own transcript entry must be at most this old when the hook reads it.
CLOSE_PROMPT_MAX_AGE = 30

# The doorbell carries no text from any desk.
DOORBELL_KIND = "owl.doorbell"
DOORBELL_SUMMARY = "An owl is waiting in your inbox."


def office_desk_dir(desk: str) -> str:
    return f"{OFFICE_ROOT}/desks/{desk}"


def castle_desk_dir(desk: str) -> str:
    return f"{CASTLE_ROOT}/desks/{desk}"


def locks_dir() -> str:
    return f"{OFFICE_ROOT}/locks"


def runs_dir() -> str:
    return f"{OFFICE_ROOT}/runs"


def logs_dir() -> str:
    return f"{OFFICE_ROOT}/logs"


def worktree_dir(name: str) -> str:
    return f"{CASTLE_ROOT}/worktrees/{name}"
