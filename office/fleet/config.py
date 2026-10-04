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
# Read-only tool folders: Homebrew, and Xcode and the Command Line Tools, where /usr/bin/python3 and git live.
CODEX_EXTRA_READS = ("/opt/homebrew", "/Applications/Xcode.app", "/Library/Developer")
# Only these desks may write their own outbox.
CODEX_OUTBOX_WRITERS = ("harry",)
# Dependency folders a worktree may borrow, read-only, from the main checkout (see fleet/toolchain.py).
LINKABLE_DEPS = ("node_modules",)
# Desks that build in a worktree. The Owl Post starts them only once their task has one.
WORKTREE_DESKS = ("harry",)
# Reviewer for each author family. A pass needs the other family.
REVIEWER_FOR_FAMILY = {"codex": "hermione", "claude": "moody"}
# A Codex desk with no worktree of its own runs here, never in its desk folder.
CODEX_WORK_DIR = "work"
# Castle folders each Claude desk may read through --add-dir. Writes there are denied by its settings.
CLAUDE_READ_DIRS = {"hermione": ("tasks", "worktrees"), "ron": ("tasks",), "portrait": ()}
# Runs per desk in one cap day, counted from launch rows so a killed run counts, and spend for the Claude
# desks, from the cost runs recorded. Sized for a busy day of 12 to 14 PRs plus side work. Ryan lifts one
# for the rest of the day with castle desk cap.
DAILY_RUN_CAP = {"hermione": 80, "ron": 120, "portrait": 3, "harry": 40, "moody": 80}
DAILY_SPEND_CAP_USD = {"hermione": 60.0, "ron": 10.0, "portrait": 4.0}
DAY_SECONDS = 86400
# When the cap day starts: caps, bumps and cap events all reset then. None is local midnight on this Mac,
# daylight saving included; a number fixes the reset that many seconds after UTC midnight instead.
# A cap day resets all at once, so a desk busy on both sides of the reset can use up to two days' cap
# within hours. Ryan's call whether to add a rolling 24 hour guard on top.
CAP_RESET_UTC_SECONDS = None
# A desk at this share of a cap today gets one headmaster event per cap per day.
CAP_WARN_FRACTION = 0.8
# Review rounds per author task. The next one waits for Ryan's castle task allow-round. Only a reviewer
# run that recorded a verdict uses up a round; the daily run caps bound retries of runs that did not.
REVIEW_ROUND_CAP = 3
# A failed run whose error text matches one of these hit the vendor's own usage or rate limit, not a
# fleet cap. Matched without case against Claude's result text when is_error is set, and against the
# message that ended a failed Codex run (its last turn.failed, else its last error event).
CLAUDE_PLAN_LIMIT_PATTERNS = (
    r"usage limit", r"(?:session|weekly|opus|sonnet|[0-9]+[ -]hour) limit reached",
    r"hit your (?:usage |session |weekly )?limit", r"out of (?:extra )?usage",
    r"rate[ _-]?limit", r"too many requests", r"\b429\b",
)
CODEX_PLAN_LIMIT_PATTERNS = (
    r"usage[ _-]?limit", r"hit your (?:usage )?limit", r"rate[ _-]?limit", r"too many requests", r"\b429\b",
    r"quota",
)
# A run the Owl Post starts waits this long for another run of the same desk to finish. A review never waits.
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
# The temp folder every user and app shares. Codex's ":minimal" set makes it writable, so every Codex
# profile denies it outright: it holds other sessions' scratch files.
SHARED_TEMP_ROOT = "/private/tmp"
# Each Codex desk that writes, and each verify run, gets its own temp folder instead, set as TMPDIR,
# inside the per-user temp folder: <user temp>/hogwarts-<name>. Nothing else in that folder is granted
# except xcrun's lookup cache, read-only, so /usr/bin shims resolve without trying to write it.
DESK_TEMP_PREFIX = "hogwarts-"
XCRUN_CACHE = "xcrun_db"
# The castle charter files every desk follows. Codex desks may read them.
CASTLE_CHARTERS = ("CLAUDE.md", "AGENTS.md")
# Words that never leave the fleet: in branch names, commit messages and PR text.
FLEET_WORDS = ("hogwarts", "mcgonagall", "harry", "hermione", "moody", "ron", "snape", "dumbledore",
               "marauder", "marauders", "gringotts", "owlpost", "headmaster", "pensieve")
# The fleet's own public kit, where these names are the product. Commit messages and added lines there
# skip the fleet-word check. Branch names never do. Set at onboarding with GITHUB_ACCOUNT.
FLEET_WORDS_ALLOWED_REPOS = (GITHUB_ACCOUNT + "/hogwarts-fleet",)

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
