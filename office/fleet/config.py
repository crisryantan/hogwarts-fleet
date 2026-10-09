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
BREW_BIN = "/opt/homebrew/bin/brew"
PYTHON_WRAPPER = ("/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", "-X", "pycache_prefix=/var/empty")
GIT_BIN = "/usr/bin/git"
BASH_BIN = "/bin/bash"
CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
# A partial clone fetches an object it lacks from its remote the moment git needs it, with nothing said and
# with Ryan's credentials. The environment the fleet gives git sets this, so a missing object is an error
# instead of a silent fetch. An explicit git fetch is unaffected.
GIT_NO_LAZY_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1"}

# Filled in at onboarding (docs/ONBOARDING.md stage 2). Later-stage scripts such as the Map read these.
# The Map watches every open PR GITHUB_ACCOUNT authored. WATCHED_REPOS names the repos whose main branch
# the keeper's watch and the scoreboard read; left as the placeholder, they read the repos of those PRs.
GITHUB_ACCOUNT = "<github-account>"
WATCHED_REPOS = ("<repos-to-watch>",)
GH_BIN = "/opt/homebrew/bin/gh"

# Desks with a folder in the castle (desks/<name>/inbox, outbox, scratchpad.md, and pads/ for TASK_PAD_DESKS).
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
# Desks that keep one pad per task, castle desks/<desk>/pads/<key>.md, as their Checkpoint for a run. Every
# review round of one author task shares that task's pad. Only a Claude desk can write its own folder. Harry's per-task memory is the CHECKPOINT in his handoff, and Moody's
# is review-latest.md in the task folder.
TASK_PAD_DESKS = ("hermione", "ron")
PADS_DIR = "pads"
# Pads a desk keeps across runs rather than per task, as its brief names them: rotated before each launch of the desk,
# like its scratchpad, while no other run of it is going.
SHARED_PADS = {"hermione": ("bot-pass",), "ron": ("patrol",)}
# Reviewer for each author family. A pass needs the other family.
REVIEWER_FOR_FAMILY = {"codex": "hermione", "claude": "moody"}
# A Codex desk with no worktree of its own runs here, never in its desk folder: work for run slot 0, and
# work.slot<n> for run slot n.
CODEX_WORK_DIR = "work"
# Castle folders each Claude desk may read through --add-dir. Writes there are denied by its settings.
CLAUDE_READ_DIRS = {"hermione": ("tasks", "worktrees"), "ron": ("tasks",), "portrait": ()}
# Runs per desk in one cap day, counted from launch rows so a killed run counts, and spend for the Claude
# desks, from the cost runs recorded. A Claude run killed before it reported its cost is charged its
# MAX_BUDGET_USD, so a spend cap never runs low. Sized for a busy day of 12 to 14 PRs plus side work. Ryan
# lifts one for the rest of the day with castle desk cap.
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
# The review loop (review.auto_review). When a build desk posts a handoff, the Owl Post starts its task's review
# in a process of its own. That review first waits this long for the run that posted the handoff to end.
AUTO_REVIEW_AUTHOR_WAIT_SECONDS = 120
# A handoff whose review cannot start yet (its author's run still going, the reviewer busy, another review of the
# task running) is tried again on each Owl Post pass, for at most this long after it was posted. Then Ryan hears.
AUTO_REVIEW_WAIT_LIMIT_SECONDS = 4 * 3600
# An automatic review killed part way is started again on the next Owl Post pass, at most this many tries in
# all (at most 9). A review that ends any other way, with a verdict or a refusal, is never tried again by itself.
AUTO_REVIEW_MAX_TRIES = 3
# How long a starting automatic review waits for another one of the same task to let go of the task's loop lock.
AUTO_REVIEW_LOCK_WAIT_SECONDS = 10
# The automatic draft PR after the review loop's PASS (push.push_draft_pr), off by default. It is on only while
# this plain file in the office, which no desk can write, holds exactly "on". Ryan writes it from his terminal:
#   echo on > ~/.hogwarts/auto-draft-pr
# A file anywhere else, a link, a file someone else owns or can write, or any other text leaves it off.
AUTO_DRAFT_PR_FILE = "auto-draft-pr"
# Auto-portrait (fleet/portrait_auto.py), off by default: after Dumbledore's clean nightly run, the job applies the
# fact and key point additions in the patch tonight's run wrote, and every other op waits for Ryan. It is on only
# while this plain file in the office holds exactly "on", by the same rules as the draft PR file, and removing it
# switches it off:
#   echo on > ~/.hogwarts/auto-portrait
#   rm ~/.hogwarts/auto-portrait
AUTO_PORTRAIT_FILE = "auto-portrait"
# Teammates' review comments on a PR the review loop opened (fleet/followup.py), off by default. On only while this
# plain file in the office holds exactly "on" and the patrol is out of shadow mode:
#   echo on > ~/.hogwarts/pr-followup
# Comments from people with write access go back to the build desk, the other family reviews the fix and the replies,
# and the loop pushes the reviewed commit to the same PR and posts each reply once.
PR_FOLLOWUP_FILE = "pr-followup"
# Auto-close (fleet/closer.py), off by default. While this plain file in the office holds exactly "on", each Map
# round starts the closer, which closes a passed task once scripts prove its merge, CI on the merge commit and every
# after-merge check. Written from the terminal: echo on > ~/.hogwarts/auto-close
AUTO_CLOSE_FILE = "auto-close"
# Worktree cleanup (worktree.sweep_closed), off by default. While this plain file in the office holds exactly "on",
# each Map round removes the worktree of a build task closed for at least WORKTREE_CLEANUP_AFTER_SECONDS, by any path,
# once git shows nothing in it would be lost. Written from the terminal: echo on > ~/.hogwarts/worktree-cleanup
# Auto-close removes the worktree of a task it closes itself, under its own switch, whatever this one says.
WORKTREE_CLEANUP_FILE = "worktree-cleanup"
# While this office file holds "on", each owl the Owl Post delivers to McGonagall is read by one headless McGonagall
# turn, and her one-line report arrives as a desktop notification (fleet/owl_report.py).
OWL_REPORTS_FILE = "owl-reports"
# While this office file holds "on", a desk whose whole family is down may run on the other family, when it has launch
# settings for it and is neither a build desk nor a reviewer; a review that would then be same-family waits
# (fleet/failover.py). Off by default: a desk never changes family, so cross-family review holds.
CROSS_FAMILY_FAILOVER_FILE = "cross-family-failover"
# While this office file holds "on", each item that lands for McGonagall (an owl to her desk, a build's review verdict)
# wakes one headless McGonagall turn that picks the next step as one typed action, which a script checks and runs
# (fleet/orchestrator.py). Off by default: echo on > ~/.hogwarts/auto-orchestrate
ORCHESTRATOR_FILE = "auto-orchestrate"
# While this office file holds "on", each change in where one of McGonagall's open go tasks stands is one line to Ryan
# through the phone transports (fleet/go_watch.py). Off by default: echo on > ~/.hogwarts/auto-go-updates
GO_UPDATES_FILE = "auto-go-updates"
# Every office opt-in file. common.opt_in_on reads only these names, so a typo never reads another office file as a
# switch, every switch is read through that one reader, and a name not listed here always reads off.
OPT_IN_FILES = (AUTO_DRAFT_PR_FILE, AUTO_PORTRAIT_FILE, PR_FOLLOWUP_FILE, AUTO_CLOSE_FILE, WORKTREE_CLEANUP_FILE,
                OWL_REPORTS_FILE, CROSS_FAMILY_FAILOVER_FILE, ORCHESTRATOR_FILE, GO_UPDATES_FILE)
# Review rounds of one follow-up, apart from the task's REVIEW_ROUND_CAP, and the follow-ups one task may take.
FOLLOWUP_ROUND_CAP = 2
FOLLOWUP_MAX_PER_TASK = 5
# At most this many follow-ups start in one Map round; the rest wait for the next.
FOLLOWUP_ROUTES_PER_ROUND = 2
# A PR's newest qualifying comment must be this old before its follow-up starts, so one review goes as one follow-up.
FOLLOWUP_SETTLE_SECONDS = 300
# Items one follow-up carries, and the size of its threads file; the rest wait, whole, for the next follow-up.
FOLLOWUP_MAX_ITEMS = 20
FOLLOWUP_TEXT_MAX = 200_000
# One comment body in the threads file, scrubbed whole before it is cut to this.
FOLLOWUP_COMMENT_MAX = 6000
# Whose comments are followed: GitHub's authorAssociation for people with write access to the repo.
FOLLOWUP_WRITE_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
# Service and CI accounts GitHub lists as people (User, often with MEMBER access). Their comments are never routed.
# Compared without letter case. Fill it before you switch follow-ups on.
FOLLOWUP_IGNORED_LOGINS: tuple = ()
# A reply as the build desk writes it, and the whole comment posted (the quote of a review or comment included).
FOLLOWUP_REPLY_MAX = 400
FOLLOWUP_BODY_MAX = 600
FOLLOWUP_QUOTE_MAX = 120
# A follow-up with no handoff and nothing else going for this long raises one headmaster event.
FOLLOWUP_STALL_SECONDS = 4 * 3600
# A push or reply cut off by a kill is read back from GitHub; a read that keeps failing gives up after this long.
FOLLOWUP_RECONCILE_LIMIT_SECONDS = 4 * 3600
# A reply read back counts only from this long before it was begun, for clocks a little apart.
FOLLOWUP_READBACK_SKEW_SECONDS = 300
# In shadow mode with the switch on, the dry run in patrol/followup/ counts comments from this far back as if follow-ups
# had been live then, so a copy kept in shadow mode from the start still shows what it would route. It never opens a
# live period: a live round routes only comments written while follow-ups are live.
FOLLOWUP_SHADOW_WINDOW_SECONDS = 7 * 86400
# The closer's own lock in the office locks folder: one closer at a time, and fleet close takes it too.
CLOSER_LOCK = "closer.lock"
# Tries an automatic pass may take per merge commit for sandboxed after-merge commands and for the judge. After-merge
# commands that run without the Codex sandbox (your own sessions) are started at most once by an automatic pass.
AUTO_CLOSE_MAX_TRIES = 3
# Neither a green CI nor "no checks" counts until this long after the merge was first seen, so a check GitHub has
# not registered yet is never read as all of CI. A red stops at once.
AUTO_CLOSE_CI_SETTLE_SECONDS = 1800
# A wait that can last (CI pending, open work under the task, the review loop, the judge's slots, a PR merged into
# another base) tells you once after this long, and keeps waiting.
AUTO_CLOSE_STALL_SECONDS = 86400
# A read that keeps failing tells you once after this long of one unknown spell.
AUTO_CLOSE_UNKNOWN_GRACE_SECONDS = 7200
# The merged diff in the judge's pack is cut here, after it is scrubbed whole.
AUTO_CLOSE_PACK_DIFF_MAX_CHARS = 200_000
# The most first-parent commits the closer walks to find the commit that brought the reviewed commit onto its base.
AUTO_CLOSE_WALK_MAX = 10_000
AUTO_CLOSE_RECORD_MAX_BYTES = 8192
# Try and clear markers per merge commit, hand runs included.
AUTO_CLOSE_MARKER_MAX = 99
# A build task's worktree is left this long after its task closed before the worktree cleanup looks at it.
WORKTREE_CLEANUP_AFTER_SECONDS = 3 * 86400
# A failed run whose error text matches one of these hit the vendor's own usage, rate, quota or credit
# limit, not a fleet cap. Matched without case against Claude's result text when is_error is set, and
# against the message that ended a failed Codex run (its last turn.failed, else its last error event).
# When a failed run left no such message, its stderr is read instead. This is the one vendor limit
# detector: the caps label the run with it, and Ollivander never counts such a run toward a trial.
CLAUDE_PLAN_LIMIT_PATTERNS = (
    r"usage limit", r"(?:session|weekly|opus|sonnet|[0-9]+[ -]hour) limit reached",
    r"hit your (?:usage |session |weekly )?limit", r"out of (?:extra )?usage",
    r"rate[ _-]?limit", r"too many requests", r"\b429\b", r"quota", r"credit balance",
)
CODEX_PLAN_LIMIT_PATTERNS = (
    r"usage[ _-]?limit", r"hit your (?:usage )?limit", r"rate[ _-]?limit", r"too many requests", r"\b429\b",
    r"quota", r"credit balance",
)
# A failed run's stderr is read only when its output says nothing, and then only its last line, against
# these narrower words: no bare 429 or quota, which a stack trace or a full disk can print too.
STDERR_PLAN_LIMIT_PATTERNS = (
    r"usage[ _-]?limit", r"(?:session|weekly|opus|sonnet|[0-9]+[ -]hour) limit reached",
    r"hit your (?:usage |session |weekly )?limit", r"out of (?:extra )?usage", r"rate[ _-]?limit",
    r"too many requests", r"quota exceeded for", r"exceeded your (?:current )?quota", r"credit balance",
)
# How many model processes each headless desk may run at once. Each run holds one run slot, and every slot of a
# desk has its own lock, Codex work folder and private temp folder (slot 0 keeps the names a desk had before
# slots). The reviewers take two, so two reviews of different tasks run at once; a desk left out has one. Caps,
# the launch gate and a desk's spend are shared by all its slots. At most hogwarts.db.RUN_SLOT_LIMIT. With
# auto-portrait on, the nightly portrait job holds every slot Dumbledore could have, up to that limit, while it reads
# and stores his patch (run_desk.all_slots_lock), so giving him more slots never lets another run of his write it.
RUN_SLOTS = {"hermione": 2, "ron": 1, "portrait": 1, "harry": 1, "moody": 2}
# A run the Owl Post starts waits this long for a free run slot of its desk. A review never waits.
DESK_LOCK_WAIT_SECONDS = 1860
# A run waits at most this long while another run of its desk checks the caps and records its launch.
DESK_LAUNCH_WAIT_SECONDS = 120
RUN_TIMEOUT_SECONDS = 1800
# A launch with no usage yet counts as running for this long, so a run killed before it recorded usage
# stops showing as running once its timeout has surely passed.
RUNNING_WINDOW_SECONDS = RUN_TIMEOUT_SECONDS + 120
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
# Each Codex desk that writes, and each verify run, gets its own temp folder instead, set as TMPDIR and TEST_TMP_ROOT
# with xcrun's cache in it (run_desk.temp_env), inside the per-user temp folder: <user temp>/hogwarts-<name>, and
# hogwarts-<desk>.slot<n> for a desk's run slot n. Nothing else in that folder is granted
# except xcrun's lookup cache, read-only, so /usr/bin shims resolve without trying to write it.
DESK_TEMP_PREFIX = "hogwarts-"
XCRUN_CACHE = "xcrun_db"
# The castle charter files every desk follows. Codex desks may read them.
CASTLE_CHARTERS = ("CLAUDE.md", "AGENTS.md")
# Words that never leave the fleet: in branch names, commit messages and PR text.
FLEET_WORDS = ("hogwarts", "mcgonagall", "harry", "hermione", "moody", "ron", "snape", "dumbledore",
               "marauder", "marauders", "gringotts", "owlpost", "headmaster", "pensieve", "ollivander")
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
# A scratchpad or task pad keeps only its latest Checkpoint block; older ones move to this folder next to it
# (fleet/scratchpad.py), one append-only file per month.
SCRATCHPAD_ARCHIVE_DIR = "scratchpad-archive"
# A rotation streams the file and holds at most this much of one line in memory; a longer line is plain text.
SCRATCHPAD_READ_MAX_BYTES = 1024 * 1024
# A file larger than this is left as it is, with a warning, so a rotation always ends within a hook's time.
SCRATCHPAD_ROTATE_MAX_BYTES = 64 * 1024 * 1024
# A file written this recently is left for the next rotation, since its desk may still be writing it.
SCRATCHPAD_QUIET_SECONDS = 60
# How long a rotation waits for another one of the same desk (office locks folder, scratchpad-<desk>.lock).
SCRATCHPAD_LOCK_WAIT_SECONDS = 5
# An owl file younger than this that does not parse yet may still be being written.
OWL_SETTLE_SECONDS = 2

# Hook budgets.
DIGEST_MAX_LINES = 39
INFLIGHT_CAP = 10
DIGEST_EVENT_LINES = 8
QUEUED_CAP = 20
DRAIN_MAX_CHARS = 1500
# Per session, the newest headmaster event id already shown (office folder EVENTS_SEEN_DIR); older files are removed.
EVENTS_SEEN_DIR = "events-seen"
EVENTS_SEEN_KEEP_SECONDS = 7 * 86400
TEMPUS_THRESHOLD = 200_000
EXTRACT_CAP = 4000
SESSION_CAP = 16000
CLOSE_TOKEN_TTL = 60
# Transcript "entrypoint" values that mark a session a person is typing into.
# Undocumented field: anything else, or no value at all, refuses the close.
CLOSE_ALLOWED_ENTRYPOINTS = ("cli", "claude-desktop")
# The prompt's own transcript entry must be at most this old when the hook reads it.
CLOSE_PROMPT_MAX_AGE = 30
# Claude Code writes a prompt's own transcript entry only after the UserPromptSubmit hook returns, so a go or a
# Mischief managed whose entry is not there yet is confirmed by one detached process (fleet/go_confirm.py): it waits
# at most this long for the entry, reading the transcript tail every GO_CONFIRM_POLL_SECONDS.
GO_CONFIRM_WAIT_SECONDS = 20
GO_CONFIRM_POLL_SECONDS = 0.25
# A confirmation whose process is gone with no outcome, this long past the wait window, was interrupted: the next hook
# or confirmer for that prompt reports it, and never runs it again.
GO_CONFIRM_STALE_MARGIN_SECONDS = 30
# The office folder that holds one claim per prompt a confirmer was started for, so no prompt is confirmed twice.
# Claims older than GO_CONFIRM_KEEP_SECONDS are removed by later confirmers.
GO_CONFIRM_DIR = "confirm"
GO_CONFIRM_KEEP_SECONDS = 86400
GO_CONFIRM_INPUT_MAX_BYTES = 16384
# The most distinct gos one prompt may start, one "go <task-id>" per line.
GO_MAX_PER_PROMPT = 5
# The background jobs a build needs to move on by itself: the Owl Post starts its runs and reviews, and the Map starts
# the closer. A go applies either way, but says plainly when neither a live fleet loops nor launchd runs one of them.
BUILD_JOBS = ("owlpost", "map")
# Home folders macOS keeps launchd jobs out of without a privacy grant. A build's repo under one needs those jobs run
# by fleet loops from a terminal; while launchd runs them, the go and fleet worktree still apply and say so.
LAUNCHD_BLIND_DIRS = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")

# fleet loops (fleet/loops.py): the background jobs run from a terminal you start, instead of launchd, so they get the
# terminal's folder access. The office folder loops/ holds the job list you choose (one launchd/ job name per line;
# while it is there the setup scripts add to it instead of loading launchd), the marker a live supervisor writes and
# the supervisor's lock. Each job keeps its plist's schedule and logs.
LOOPS_DIR = "loops"
LOOPS_JOBS_FILE = "jobs"
LOOPS_RUNNING_FILE = "running.json"
LOOPS_LOCK = "loops.lock"
LOOPS_LOG = "loops.log"
# How often the supervisor checks its clocks and WatchPaths, and how long a job has to stop after SIGTERM (launchd's
# default ExitTimeOut) before SIGKILL.
LOOPS_TICK_SECONDS = 1.0
LOOPS_STOP_GRACE_SECONDS = 20
# launchd's default ThrottleInterval: the least time between two starts of one job, unless its plist sets one.
LOOPS_THROTTLE_SECONDS = 10
# After a failed run the job waits this long before it may start again, doubled for each failure in a row up to the
# cap. A job with StartInterval, WatchPaths or KeepAlive is retried then; a calendar job waits for its next slot.
LOOPS_BACKOFF_FIRST_SECONDS = 30
LOOPS_BACKOFF_MAX_SECONDS = 600
# Calendar slots missed while the Mac slept run once on wake, as under launchd. The whole gap is looked at, up to a
# year back, which holds every slot an entry can name except 29 February.
LOOPS_CATCH_UP_SECONDS = 366 * 86400
# A desktop notification for each owl delivered to McGonagall's inbox (macOS only). False turns it off.
DESKTOP_NOTIFY = True
DESKTOP_NOTIFY_TIMEOUT_SECONDS = 5
# The headless owl-report turn: its settings file in McGonagall's office folder, tools, model (the cheapest alias the
# fleet already runs), budget, timeout and how much of its output is read. One reporter runs at a time
# (OWL_REPORT_LOCK, handed to each turn's process), one owl per turn, in its own folder under OWL_REPORT_ROOT, at most
# OWL_REPORT_MAX_TURNS turns a run. An owl her output skips is reported with a fallback line after
# OWL_REPORT_MAX_TRIES turns, and a notification that fails is tried OWL_REPORT_NOTIFY_TRIES times.
OWL_REPORT_SETTINGS_FILE = "owl-report-settings.json"
OWL_REPORT_TOOLS = "Read,Grep,Glob"
OWL_REPORT_MODEL = "haiku"
OWL_REPORT_MAX_BUDGET_USD = "0.25"
OWL_REPORT_TIMEOUT_SECONDS = 90
OWL_REPORT_OUTPUT_MAX_BYTES = 65536
OWL_REPORT_LOCK = "owl-report.lock"
# Outside the office and the castle, so the turn's sandbox may read it, and writable by no desk.
OWL_REPORT_ROOT = "/Users/crisryantan/Library/Caches/hogwarts-owl-report"
OWL_REPORT_MAX_TURNS = 15
OWL_REPORT_MAX_TRIES = 3
OWL_REPORT_NOTIFY_TRIES = 3
# The orchestrator's headless turns run under the owl-report settings (run_desk.owl_report_argv) with their own model and
# budget, since picking the next step is a judgement call: one item a turn in its own folder under ORCHESTRATOR_ROOT, at
# most ORCHESTRATOR_MAX_TURNS a run. Wakes are counted in the office
# before each turn starts, so a killed turn counts: at most this many per task, ever, and per cap day.
ORCHESTRATOR_MODEL = "sonnet"
ORCHESTRATOR_MAX_BUDGET_USD = "0.50"
ORCHESTRATOR_LOCK = "orchestrator.lock"
ORCHESTRATOR_ROOT = "/Users/crisryantan/Library/Caches/hogwarts-orchestrator"
ORCHESTRATOR_MAX_TURNS = 10
ORCHESTRATOR_WAKES_PER_TASK = 6
ORCHESTRATOR_WAKES_PER_DAY = 30
# An owl or a verdict wakes her only when it was stored within this long, and after the switch was first seen on.
ORCHESTRATOR_LAND_WINDOW_SECONDS = 7 * 86400
# Finished item records are kept this long, so the same item never wakes her twice.
ORCHESTRATOR_KEEP_SECONDS = 14 * 86400
# How much of an owl's body her context file carries, after it is scrubbed.
ORCHESTRATOR_BODY_MAX = 4000
# Phone delivery (fleet/phone.py): the loud headmaster events go to Ryan off the machine, one ping each. The primary
# transport is a command set only in the private overlay: an absolute argv, never holding a secret, that reads one JSON
# payload on stdin and exits 0 once delivered. None, or a failure, falls back to the macOS notification.
PHONE_COMMAND = None
PHONE_COMMAND_TIMEOUT_SECONDS = 20
PHONE_MAX_PER_PASS = 5
PHONE_KINDS = ("push.draft-pr", "push.auto-failed", "go.refused", "review.headmaster", "review.loop-stopped",
               "review.round-cap", "review.ready-for-push", "review.auto", "review.unpublished", "review.interrupted",
               "review.fix-round", "orchestrator.notify", "orchestrator.cap", "orchestrator.rejected",
               "orchestrator.ask-snape", "orchestrator.auth", "orchestrator.failed", "orchestrator.interrupted",
               # Each needs Ryan: a review no reviewer could judge (only once its tries or wait are spent, or its
               # worktree needs rebuilding), a model family down or a run waiting on it, and a CLI that cannot sign in.
               "review.blocked-on-tooling", "failover.down", "failover.wait", "failover.wait-ended", "failover.auth",
               # Ollivander's stop, which blocks every headless desk run until it is cleared by hand.
               "ollivander.stopped")
# How many new owls in McGonagall's inbox the prompt hook lists at once, with a count of the rest.
INBOX_NOTICE_CAP = 10
# McGonagall's go status block (fleet/go_status.py): at most this many open go tasks, newest first, each with at most
# GO_STATUS_BUILDS build lines, every line cut to GO_STATUS_LINE_CHARS. What each session was shown is kept per
# session in the office folder GO_STATUS_SEEN_DIR, removed after EVENTS_SEEN_KEEP_SECONDS.
GO_STATUS_CAP = 10
GO_STATUS_BUILDS = 2
GO_STATUS_LINE_CHARS = 300
GO_STATUS_SEEN_DIR = "go-status-seen"
# Go updates (fleet/go_watch.py): at most this many lines a pass, each its own ping, and one summary ping for the rest.
# Every line is cut to GO_WATCH_LINE_CHARS. The last state sent for each go task, and one marker per line, are kept
# in the office folder GO_WATCH_DIR.
GO_WATCH_MAX_PER_PASS = 5
GO_WATCH_LINE_CHARS = 200
GO_WATCH_DIR = "go-watch"
# The go confirmer and verify wait at most this long for a watch another process is running, so their change is not
# left to the next pass. Each line is also appended to the office's logs/GO_UPDATES_LOG, which past
# GO_UPDATES_LOG_MAX_BYTES moves to GO_UPDATES_LOG.1 (one older file kept) before the next line.
GO_WATCH_WAIT_SECONDS = 20
GO_UPDATES_LOG = "go-updates.log"
GO_UPDATES_LOG_MAX_BYTES = 256 * 1024
# A seen marker a prompt hook left pending this long, or whose hook process is gone, is taken over by the next hook.
SEEN_PENDING_SECONDS = 60

# The doorbell carries no text from any desk.
DOORBELL_KIND = "owl.doorbell"
DOORBELL_SUMMARY = "An owl is waiting in your inbox."

# Dumbledore's nightly review (fleet/portrait.py). No desk wrote the export, so its owl comes from the Owl
# Post's own desk, the fleet's message router.
PORTRAIT_EXPORT_SENDER = "owl-post"
# The export's size: extracts up to this many bytes, and at most this many current facts.
PORTRAIT_EXPORT_MAX_BYTES = 512 * 1024
PORTRAIT_EXPORT_MAX_FACTS = 300
# The MCP job (office desks/portrait/mcp-<name>.json) that gives Dumbledore chat, or None for no MCP server at
# all. Set it to "chat" once that file names your chat server. Chat stays read-only: his settings allow only
# that server's list and search tools, deny send_message, and his runs refuse every tool not allowed.
PORTRAIT_MCP_JOB = None

# Ollivander, the model keeper. Desks pick a model by role (office desks/<desk>/role.json), never by
# model line, and a desk's family never changes.
OLLIVANDER_DESK = "ollivander"
ROLE_FILE = "role.json"
ROLE_MAX_BYTES = 4096
# Desks with a role card. run_desk launches the headless ones with Ollivander's pick. The others take
# their model from an agent file Ollivander only reports on: (root, path inside it).
ROLE_DESKS = ("mcgonagall", "hermione", "portrait", "snape", "ron", "harry", "moody")
AGENT_FILE_DESKS = {"mcgonagall": ("castle", ".claude/agents/mcgonagall.md"),
                    "snape": ("home", ".claude/agents/snape.md")}
# Claude Code aliases always mean the newest model of their line that the installed Claude Code knows.
CLAUDE_LINES = {"opus": "frontier", "sonnet": "workhorse", "haiku": "fast"}
# Codex models are filed by their catalog description. A model in the excluded list is never picked,
# whatever else it matches. Ryan's "castle model line" filing outranks these words.
CODEX_LINE_WORDS = {
    "frontier": ("frontier", "most demanding"),
    "workhorse": ("workhorse", "coding and everyday"),
    "fast": ("fast and affordable", "easier tasks"),
}
CODEX_EXCLUDED_WORDS = ("previous generation", "older", "legacy")
# An organisation may forbid some models. Each entry is a lowercase prefix matched against Claude Code
# aliases, full Claude ids and Codex catalog slugs: to forbid a Claude line, list its alias and its
# "claude-<alias>-" id prefix. A blocked name is never picked, pinned, filed or launched. Empty in the
# kit: the live office sets its own.
BLOCKED_MODEL_PREFIXES: tuple = ()
# A model that retires within this many days is never picked.
RETIRING_SOON_SECONDS = 30 * 86400
# Cost order. A move to the same or a cheaper class applies by itself; a dearer one waits for Ryan.
COST_ORDER = ("fast", "workhorse", "frontier")
CATALOG_TIMEOUT_SECONDS = 60
CATALOG_MAX_BYTES = 16 * 1024 * 1024
HELP_MAX_BYTES = 1024 * 1024
# CLI updates run only while Ryan keeps this file in the office desks/ollivander folder.
UPDATE_MARKER = "update-clis"
UPDATE_TIMEOUT_SECONDS = 900
CHECK_TIMEOUT_SECONDS = 60
# Kept in the office state folder. While it exists run_desk launches no headless desk.
STATE_DIR = "state"
STOP_FILE = "ollivander-stop"
# Also in the state folder, only while a CLI update and its checks run. run_desk honours it like the stop file.
UPDATING_FILE = "ollivander-updating"
# Also in the state folder: one marker per desk run a stop refused (fleet/stops.py), which the Owl Post's first pass
# after the stop clears starts again, once each.
STOP_HELD_DIR = "stop-held"
# A held run whose launch never started is tried again at most this many launches in all, then dropped and said so.
STOP_RESUMES_MAX = 3
# At most this many held runs are started again (or given up on) in one Owl Post pass, so its one event names each.
STOP_RESTARTS_PER_PASS = 3
STOP_TEXT_MAX_BYTES = 1024
# In the office locks folder. Ollivander holds it exclusively from before he writes the update marker until
# the checks after the update end. run_desk holds it shared from its last stop check until the desk's
# process exists, so launches never wait on each other and never overlap an update. run_desk never waits
# for it: an update in progress refuses the launch at once. Ollivander waits at most this long for it.
UPDATE_LOCK = "ollivander-update.lock"
UPDATE_LOCK_WAIT_SECONDS = 120
# The Codex sandbox boundary was proven on one Codex version, so a new Codex version stops the headless
# desks until Ryan re-proves it and clears the stop. A new Claude Code version is reported only.
VERSION_STOPS = ("codex",)
VERSION_MAX_CHARS = 120
RUN_ERROR_MAX_BYTES = 1024 * 1024

# Model failover (fleet/failover.py). A failed run is classed by the HTTP status in its CLI's structured error output.
FAILOVER_STATUS_CLASSES = {401: "auth", 403: "auth", 429: "rate_limit", 529: "overload", 500: "outage",
                           502: "outage", 503: "outage", 504: "outage"}
# This many outage-class failures in a row mark a model down for FAILOVER_DOWN_SECONDS, then one run probes it.
FAILOVER_TRIP_FAILURES = 2
FAILOVER_DOWN_SECONDS = 15 * 60
# A run that an outage cut off is started again from its checkpoint at most this many times, each under the caps.
FAILOVER_RETRIES = 2
# A run that waited because every model it may run was down is started again by the Owl Post, once per wait, for at
# most this long after it first waited.
FAILOVER_WAIT_LIMIT_SECONDS = 4 * 3600
FAILOVER_MAX_RESUMES = 3
# In the office state folder: the breaker state, and Ollivander's fallback ladders. The lock is in the locks folder.
FAILOVER_STATE_FILE = "model-breaker.json"
FAILOVER_LADDERS_FILE = "model-ladders.json"
FAILOVER_STATE_MAX_BYTES = 256 * 1024
FAILOVER_LOCK = "model-breaker.lock"
FAILOVER_LOCK_WAIT_SECONDS = 10
# Fallback runs kept in the state, for the moved check and for a review that must follow its author's family.
FAILOVER_RUNS_KEPT = 500

# The patrol: the Marauder's Map, Ron's scheduled jobs and Hermione's bot pass (fleet/patrol.py). Each job
# writes its files under the office patrol folder. While the plain file patrol/shadow is there, those files
# are all it writes: no headmaster event and no owl to McGonagall. Ryan removes the file to go live.
PATROL_DIR = "patrol"
SHADOW_FILE = "shadow"
# The script desk the patrol speaks as when it wakes Ron or Hermione.
PATROL_SENDER = "map"
PATROL_LOCK = "patrol.lock"
PATROL_LOCK_WAIT_SECONDS = 1500
# The morning lineup's local time and weekdays, as in launchd/com.hogwarts.morning.plist. A Map round after it on a
# weekday whose lineup file is missing (the Mac slept through 08:30, or GitHub was out of reach) writes it then.
LINEUP_AT = (8, 30)
# A PR with no activity (commit, review, comment or edit) for this many days is listed as stale, below the rest, in
# the lineup and Map round files; review requests from bots are listed there too.
PATROL_STALE_DAYS = 30
LINEUP_WEEKDAYS = (0, 1, 2, 3, 4)
PATROL_STATE_MAX_BYTES = 4 * 1024 * 1024
GH_TIMEOUT_SECONDS = 60
GH_OUTPUT_MAX_BYTES = 16 * 1024 * 1024
# A patrol owl whose file the patrol has not taken (its run did not end cleanly, or left no file it could
# take) is sent again by a later Map round this long after its last try, at most this many times. Then it
# goes to Ryan as a row.
PATROL_RESEND_AFTER_SECONDS = 1800
PATROL_MAX_RESENDS = 2
# Hermione's bot pass waits this long after a PR opens, so the review bots have posted, and takes at
# most this many PRs in one Map round.
BOT_PASS_DELAY_SECONDS = 900
BOT_PASS_MAX_PER_ROUND = 2

# Gringotts, the nightly backup, kept in the office backups folder and never synced anywhere.
CLAUDE_CONFIG_DIR = "/Users/crisryantan/.claude"
CODEX_CONFIG_DIR = "/Users/crisryantan/.codex"
BACKUP_DIR = "backups"
BACKUP_KEEP_DAYS = 14
BACKUP_FILE_MAX_BYTES = 64 * 1024 * 1024


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


def launch_agents_dir() -> str:
    return f"{USER_HOME_DIR}/Library/LaunchAgents"


def worktree_dir(name: str) -> str:
    return f"{CASTLE_ROOT}/worktrees/{name}"
