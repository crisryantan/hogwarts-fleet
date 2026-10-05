#!/bin/sh
# Runs both test suites against an installed copy of the office whose private values are made up.
# Run it yourself in Terminal, from your clone of the kit:  sh scripts/installed-office-check.sh
#
# It installs the kit into a throwaway home folder, swaps the private values a real install fills in (the
# GitHub account, the watched repos, the blocked models and the MCP names) for fake ones, and runs both
# suites there. A test that quietly leans on your own private values passes in your office and fails here.
# The installer runs with a cleared environment: only HOME (the throwaway folder), a fixed PATH and LANG, so
# no inherited variable such as GIT_DIR can point it at your own ~/.hogwarts or ~/hogwarts. The folder is deleted at the end. No model runs and no tokens are used.
set -eu
umask 077

WRAPPER="/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty"
PLACEHOLDERS='<warehouse-mcp>|<observability-mcp>|<chat-mcp>|<github-account>|<repos-to-watch>'

die() { printf 'installed-office-check: %s\n' "$*" >&2; exit 1; }

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
[ -f "$REPO_DIR/install.sh" ] || die "run this from a clone of the hogwarts-fleet repo."
WORK=$(mktemp -d /private/tmp/hogwarts-installed-check.XXXXXX)
trap 'rm -rf "$WORK"' EXIT INT TERM
FAKE_HOME="$WORK/home"
mkdir -m 700 "$FAKE_HOME"
OFFICE="$FAKE_HOME/.hogwarts"

# A fresh install into the throwaway home. It runs both suites once on the kit's own unfilled placeholders.
/usr/bin/env -i HOME="$FAKE_HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=en_US.UTF-8 /bin/sh "$REPO_DIR/install.sh" \
	>"$WORK/install.log" 2>&1 || { cat "$WORK/install.log" >&2; die "install.sh failed."; }

# The overlay: made-up values where your real ones go. Tests are left alone, and so is every other value.
rewrite() { target=$1; shift; sed "$@" "$target" >"$target.rewrite" && mv "$target.rewrite" "$target"; }
CONFIG="$OFFICE/fleet/config.py"
rewrite "$CONFIG" \
	-e 's|^GITHUB_ACCOUNT = .*|GITHUB_ACCOUNT = "synthetic-owner"|' \
	-e 's|^WATCHED_REPOS = .*|WATCHED_REPOS = ("synthetic-owner/synthetic-repo",)|' \
	-e 's|^BLOCKED_MODEL_PREFIXES: tuple = .*|BLOCKED_MODEL_PREFIXES: tuple = ("synthetic-blocked-",)|'
for line in 'GITHUB_ACCOUNT = "synthetic-owner"' 'BLOCKED_MODEL_PREFIXES: tuple = ("synthetic-blocked-",)'; do
	grep -qxF -- "$line" "$CONFIG" || die "could not set \"$line\" in the installed config."
done
grep -rlI -E "$PLACEHOLDERS" "$OFFICE/desks" "$FAKE_HOME/hogwarts" "$FAKE_HOME/.claude/agents" |
	while IFS= read -r file; do
		rewrite "$file" -e 's|<warehouse-mcp>|synthetic-warehouse|g' -e 's|<observability-mcp>|synthetic-observability|g' \
			-e 's|<chat-mcp>|synthetic-chat|g' -e 's|<github-account>|synthetic-owner|g' \
			-e 's|<repos-to-watch>|synthetic-owner/synthetic-repo|g'
	done
! grep -rqI -E "$PLACEHOLDERS" "$OFFICE/desks" "$FAKE_HOME/hogwarts" "$FAKE_HOME/.claude/agents" ||
	die "a placeholder was left unfilled in the installed copy."

for suite in tests tests_fleet; do
	# shellcheck disable=SC2086
	if (cd "$OFFICE" && $WRAPPER -m unittest discover -s "$suite" -t .) >"$WORK/$suite.log" 2>&1; then
		printf '%s: %s %s\n' "$suite" "$(grep -E '^Ran [0-9]+ tests' "$WORK/$suite.log")" "$(tail -n 1 "$WORK/$suite.log")"
	else
		cat "$WORK/$suite.log" >&2
		die "the $suite suite failed against the installed office with synthetic private values."
	fi
done
printf 'installed-office-check: both suites pass with synthetic private values.\n'
