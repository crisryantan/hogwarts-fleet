#!/bin/sh
# Install the Hogwarts fleet on this Mac.
#
# Copies office/ to ~/.hogwarts and castle/ to ~/hogwarts, adds Snape's agent
# file to ~/.claude/agents if it is missing, points the fixed paths at your home
# folder, sets private file modes, creates the database, registers the desks and
# runs both test suites.
#
# It never touches ~/.claude/settings.json, ~/.codex or launchd. Those steps are
# yours, and ~/.hogwarts/pending/README.md walks through them.

set -eu
umask 077

SOURCE_HOME=/Users/crisryantan
WRAPPER="/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty"
PLACEHOLDERS='<warehouse-mcp>|<observability-mcp>|<chat-mcp>|<github-account>|<repos-to-watch>'

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
Usage: ./install.sh [--force]

Installs the fleet into ~/.hogwarts (the office) and ~/hogwarts (the castle).

Without --force it changes nothing when either folder already exists.
With --force it first moves each existing folder aside to
<folder>.pre-install-<YYYYMMDD-HHMMSS>, then installs fresh.
EOF
}

FORCE=0
for arg in "$@"; do
	case $arg in
	--force) FORCE=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		usage >&2
		exit 2
		;;
	esac
done

# --- Where we are and where we install -------------------------------------

[ "$(uname -s)" = Darwin ] || die "the fleet runs on macOS only."
[ -n "${HOME:-}" ] || die "HOME is not set."
case $HOME in
/) die "HOME is /, which cannot hold the fleet." ;;
/*) ;;
*) die "HOME must be an absolute path." ;;
esac
case $HOME in
*[!A-Za-z0-9._/-]*) die "HOME has characters the fleet's fixed paths cannot hold. Use letters, digits, dot, underscore, hyphen and slash only." ;;
esac
[ -d "$HOME" ] || die "HOME ($HOME) is not a folder."
real_home=$(cd "$HOME" && pwd -P)
[ "$real_home" = "$HOME" ] || die "HOME must be a real, normalised path with no symlink or trailing slash. It resolves to $real_home."

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
[ -d "$REPO_DIR/office/hogwarts" ] && [ -d "$REPO_DIR/castle/.claude" ] || die "run this from a clone of the hogwarts-fleet repo."

OFFICE="$HOME/.hogwarts"
CASTLE="$HOME/hogwarts"
AGENTS="$HOME/.claude/agents"
STAMP=$(date +%Y%m%d-%H%M%S)

case $REPO_DIR in
"$OFFICE" | "$OFFICE"/* | "$CASTLE" | "$CASTLE"/*)
	die "the repo is cloned inside $OFFICE or $CASTLE. Clone it somewhere else, such as ~/hogwarts-fleet."
	;;
esac

# --- Requirements -----------------------------------------------------------

[ -x /usr/bin/python3 ] || die "/usr/bin/python3 is missing. Install the Command Line Tools with: xcode-select --install"
/usr/bin/python3 -I -B -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 ||
	die "/usr/bin/python3 must be 3.9 or newer. Install the Command Line Tools with: xcode-select --install"
/usr/bin/python3 -I -B -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' >/dev/null 2>&1 ||
	die "the system Python's SQLite has no FTS5, which the store needs."

missing=""
for tool in git jq gh rg shellcheck sqlite3 claude codex; do
	command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done

# --- Refuse or move aside an existing install -------------------------------

for dir in "$OFFICE" "$CASTLE"; do
	if [ -e "$dir" ] || [ -L "$dir" ]; then
		[ "$FORCE" = 1 ] || die "$dir already exists, so nothing was changed. To reinstall, run ./install.sh --force. It moves the existing folder to $dir.pre-install-<timestamp> first."
	fi
done
for dir in "$OFFICE" "$CASTLE"; do
	if [ -e "$dir" ] || [ -L "$dir" ]; then
		backup="$dir.pre-install-$STAMP"
		[ ! -e "$backup" ] || die "$backup already exists. Wait a second and run again."
		mv "$dir" "$backup"
		say "Moved the existing $dir to $backup"
	fi
done
for plist in "$HOME"/Library/LaunchAgents/com.hogwarts.*.plist; do
	[ -e "$plist" ] || continue
	say "Note: $plist is already in place. It still points at the paths it was loaded with."
done

# --- Copy -------------------------------------------------------------------

say "Installing the Hogwarts fleet for $HOME"

copy_tree() {
	mkdir -m 700 "$2"
	(cd "$1" && tar -cf - --exclude .gitkeep --exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store .) |
		(cd "$2" && tar -xf -)
}
copy_tree "$REPO_DIR/office" "$OFFICE"
copy_tree "$REPO_DIR/castle" "$CASTLE"
mkdir -p "$OFFICE/logs"
say "Copied the office to $OFFICE and the castle to $CASTLE."

# --- Point the fixed paths at this home folder ------------------------------

if [ "$HOME" != "$SOURCE_HOME" ]; then
	grep -rlI -F -- "$SOURCE_HOME" "$OFFICE" "$CASTLE" | while IFS= read -r file; do
		sed "s#$SOURCE_HOME#$HOME#g" "$file" >"$file.rewrite" && mv "$file.rewrite" "$file"
	done
	if grep -rqI -F -- "$SOURCE_HOME" "$OFFICE" "$CASTLE"; then
		die "some files still name $SOURCE_HOME after the rewrite. Check them with: grep -rn $SOURCE_HOME $OFFICE $CASTLE"
	fi
	say "Rewrote $SOURCE_HOME to $HOME in the copied files."
fi

# The fleet starts Claude and Codex by absolute path. Look in the usual fixed
# places when the default is missing, and say what was chosen.
CONFIG="$OFFICE/fleet/config.py"
set_bin() {
	name=$1
	default=$2
	shift 2
	[ -x "$default" ] && return 0
	for candidate in "$@"; do
		if [ -x "$candidate" ]; then
			sed "s#^$name = \".*\"\$#$name = \"$candidate\"#" "$CONFIG" >"$CONFIG.rewrite" && mv "$CONFIG.rewrite" "$CONFIG"
			grep -q "^$name = \"$candidate\"\$" "$CONFIG" || die "could not set $name in $CONFIG."
			say "Set $name to $candidate in $CONFIG."
			return 0
		fi
	done
	say "Note: $default was not found, so headless desks that need it will not start. docs/CUSTOMISE.md shows how to set $name."
}
set_bin CLAUDE_BIN "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude
set_bin CODEX_BIN /opt/homebrew/bin/codex /usr/local/bin/codex

# --- The castle is a local git repo with no remote --------------------------

if command -v git >/dev/null 2>&1; then
	git -C "$CASTLE" init -q -b main 2>/dev/null || git -C "$CASTLE" init -q ||
		say "Note: git could not initialise $CASTLE. Run git init there yourself later."
else
	say "Note: git is missing, so $CASTLE is not a git repo yet. Run git init there after stage 0."
fi

# --- Private modes ----------------------------------------------------------

find "$OFFICE" "$CASTLE" -type d -exec chmod 700 {} +
find "$OFFICE" "$CASTLE" -type f -exec chmod 600 {} +
chmod 700 "$OFFICE/bin/castle" "$OFFICE/bin/fleet"

# --- Snape's agent file, only if missing ------------------------------------

if [ -e "$AGENTS/snape.md" ] || [ -L "$AGENTS/snape.md" ]; then
	if cmp -s "$REPO_DIR/claude-agents/snape.md" "$AGENTS/snape.md"; then
		say "Snape's agent file is already in place."
	else
		say "Kept your existing $AGENTS/snape.md. It differs from the repo copy, so compare them yourself."
	fi
else
	mkdir -p "$AGENTS"
	cp "$REPO_DIR/claude-agents/snape.md" "$AGENTS/snape.md"
	chmod 600 "$AGENTS/snape.md"
	say "Added Snape's agent file at $AGENTS/snape.md."
fi

# --- Database and desk registry ---------------------------------------------

CASTLE_CLI="$OFFICE/bin/castle"
"$CASTLE_CLI" init >/dev/null
add_desk() {
	"$CASTLE_CLI" desk add "$@" >/dev/null
}
add_desk mcgonagall --family claude --role "McGonagall - Chief of Staff" --model opus
add_desk harry --family codex --role "Harry - Senior Engineer"
add_desk hermione --family claude --role "Hermione - Staff Engineer" --model opus
add_desk moody --family codex --role "Moody - Security Reviewer"
add_desk ron --family claude --role "Ron - Release Engineer" --model haiku
add_desk snape --family claude --role "Snape - Data Analyst" --model sonnet
add_desk portrait --family claude --role "Dumbledore - Knowledge Manager" --model opus
add_desk ryan-claude-1 --family claude --role "Ryan's own Claude sessions"
add_desk ryan --family human --role "Headmaster"
add_desk owl-post --family script --role "Owl Post - Message Router"
add_desk map --family script --role "Marauder's Map - PR Watcher"
add_desk gringotts --family script --role "Gringotts - Backup"
add_desk ollivander --family script --role "Ollivander - Model Keeper"
say "Created the database and registered 13 desks."

# --- Tests and health check -------------------------------------------------

run_suite() {
	log="$OFFICE/logs/install-$1.log"
	# shellcheck disable=SC2086
	if (cd "$OFFICE" && $WRAPPER -m unittest discover -s "$1" -t .) >"$log" 2>&1; then
		say "$1: $(grep -E '^Ran [0-9]+ tests' "$log") $(tail -n 1 "$log")"
	else
		cat "$log" >&2
		die "the $1 suite failed. The full output is in $log."
	fi
}
run_suite tests
run_suite tests_fleet

"$CASTLE_CLI" doctor >/dev/null || die "castle doctor reported a problem. Run $CASTLE_CLI doctor to see it."
say "castle doctor: ok"

# --- What is left for you ---------------------------------------------------

placeholder_files=$(grep -rlI -E "$PLACEHOLDERS" "$OFFICE" "$CASTLE" "$AGENTS/snape.md" 2>/dev/null || true)
if [ -n "$placeholder_files" ]; then
	say ""
	say "Placeholders to fill in (docs/ONBOARDING.md, stage 2):"
	printf '%s\n' "$placeholder_files" | sed 's/^/  /'
fi
if [ -n "$missing" ]; then
	say ""
	say "Not on your PATH yet:$missing (docs/ONBOARDING.md, stage 0)."
fi
say ""
say "Installed. Next steps:"
say "  1. docs/ONBOARDING.md stage 2: sign in and fill in the placeholders."
say "  2. Read $OFFICE/pending/README.md and apply those settings yourself (stage 3)."
say "This script did not touch ~/.claude/settings.json, ~/.codex or launchd."
