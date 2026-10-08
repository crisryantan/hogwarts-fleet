#!/bin/sh
# Take the Hogwarts fleet off this Mac.
#
# With no flags this is a dry run. It prints every step it would take and
# changes nothing. With --yes it acts. It never edits your Claude or Codex
# settings: it shows you what to restore instead.
#
# Exit codes: 0 when nothing is left for you, 1 when your settings still mention
# the fleet (or a step failed), 2 when it stopped at a worktree with uncommitted
# changes or at a fleet loops that is still running.

set -eu
umask 077

say() { printf '%s\n' "$*"; }
die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
Usage: ./uninstall.sh [--yes] [--no-archive]

With no flags it prints every step it would take and changes nothing.
  --yes         act on the steps.
  --no-archive  with --yes, remove ~/.hogwarts and ~/hogwarts without
                archiving them first. Without --yes it changes nothing.
EOF
}

YES=0
ARCHIVE=1
for arg in "$@"; do
	case $arg in
	--yes) YES=1 ;;
	--no-archive) ARCHIVE=0 ;;
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

[ -n "${HOME:-}" ] || die "HOME is not set."
case $HOME in
/) die "HOME is /. Refusing." ;;
/*) ;;
*) die "HOME must be an absolute path." ;;
esac

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
OFFICE="$HOME/.hogwarts"
CASTLE="$HOME/hogwarts"
AGENTS="$HOME/.claude/agents"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_NOW=$(id -u)
NEEDS_YOU=0
BLOCKED=0

# The fleet loops lock of the office in $1 (locks/loops.lock), taken on fd 9 and kept until fd 9 closes, so no fleet
# loops starts from that office meanwhile. It fails while a running fleet loops holds it.
hold_loops_lock() {
	[ -d "$1" ] || return 0
	mkdir -p "$1/locks" && chmod 700 "$1/locks" || return 1
	[ ! -L "$1/locks/loops.lock" ] || return 1
	exec 9>>"$1/locks/loops.lock" || return 1
	/usr/bin/python3 -I -B -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null
}

# Print a command, and run it only with --yes.
act() {
	if [ "$YES" = 1 ]; then
		say "  run: $*"
		"$@"
	else
		say "  would run: $*"
	fi
}

if [ "$YES" = 1 ]; then
	say "Uninstalling the Hogwarts fleet from $HOME"
else
	say "DRY RUN for $HOME. Nothing changes. Run ./uninstall.sh --yes to act."
fi

# --- 1. Background jobs ------------------------------------------------------

say ""
say "1. Background jobs (com.hogwarts.*)"
# fleet loops runs the jobs from a terminal window instead; it has to stop first, from that window. With --yes its lock
# is held from here to the end, so none starts while the office goes. A dry run only checks, in a subshell.
if [ "$YES" = 1 ]; then
	loops_free() { hold_loops_lock "$OFFICE"; }
else
	loops_free() { [ ! -f "$OFFICE/locks/loops.lock" ] || (hold_loops_lock "$OFFICE"); }
fi
if [ -d "$OFFICE" ] && ! loops_free; then
	say "  fleet loops is running. Press Ctrl+C in its window and wait for \"fleet loops: stopped\"."
	if [ "$YES" = 1 ]; then
		say "Stopped before changing anything. Run ./uninstall.sh --yes again once fleet loops has stopped."
		exit 2
	fi
fi
jobs_seen=""
for plist in "$LAUNCH_AGENTS"/com.hogwarts.*.plist; do
	[ -e "$plist" ] || continue
	label=$(basename "$plist" .plist)
	jobs_seen="$jobs_seen $label"
	if launchctl print "gui/$UID_NOW/$label" >/dev/null 2>&1; then
		act launchctl bootout "gui/$UID_NOW/$label"
	else
		say "  $label is not loaded."
	fi
	act rm "$plist"
done
loaded=$(launchctl list 2>/dev/null | awk '$3 ~ /^com\.hogwarts\./ {print $3}' || true)
for label in $loaded; do
	case " $jobs_seen " in
	*" $label "*) continue ;;
	esac
	jobs_seen="$jobs_seen $label"
	act launchctl bootout "gui/$UID_NOW/$label"
done
[ -n "$jobs_seen" ] || say "  None found."

# --- 2. Your settings ----------------------------------------------------------

say ""
say "2. Your Claude and Codex settings (never edited here)"
SETTINGS="$HOME/.claude/settings.json $HOME/.claude/settings.local.json $HOME/.codex/config.toml $HOME/.codex/hooks.json"
for file in $SETTINGS; do
	[ -f "$file" ] || continue
	if grep -qi 'hogwarts' "$file"; then
		NEEDS_YOU=1
		say "  $file mentions the fleet on these lines:"
		grep -ni 'hogwarts' "$file" | cut -c1-200 | sed 's/^/    line /'
		newest=""
		for backup in "$file".pre-hogwarts-*; do
			[ -e "$backup" ] || continue
			say "    backup: $backup"
			newest=$backup
		done
		if [ -n "$newest" ]; then
			say "    To restore the newest backup, run: cp -p \"$newest\" \"$file\""
		else
			say "    No .pre-hogwarts- backup sits next to it. Remove those lines by hand."
		fi
	else
		say "  $file: no fleet references."
	fi
done

# --- 3. RTK ------------------------------------------------------------------

say ""
say "3. RTK"
rtk_hook=0
for file in $SETTINGS; do
	[ -f "$file" ] || continue
	if grep -q 'rtk hook' "$file"; then
		rtk_hook=1
	fi
done
if [ "$rtk_hook" = 1 ]; then
	say "  An RTK hook is in your settings. If you added it for the fleet and want it gone, run this yourself:"
	say "    rtk init -g --uninstall"
else
	say "  No RTK hook found."
fi

# --- 4. Git worktrees ----------------------------------------------------------

say ""
say "4. Git worktrees under $CASTLE/worktrees"
worktrees_seen=0
if [ -d "$CASTLE/worktrees" ]; then
	for tree in "$CASTLE"/worktrees/*; do
		[ -d "$tree" ] || continue
		worktrees_seen=1
		if [ ! -e "$tree/.git" ]; then
			say "  $tree is not a git worktree. It goes into the archive with the castle."
			continue
		fi
		common=$(git -C "$tree" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
			say "  Could not read $tree as a git worktree. Check it by hand."
			BLOCKED=1
			continue
		}
		case $common in
		*/.git) repo=${common%/.git} ;;
		*) repo=$common ;;
		esac
		changes=$(git -C "$tree" status --porcelain 2>/dev/null) || changes="unreadable"
		if [ -n "$changes" ]; then
			say "  $tree has uncommitted changes. Commit or discard them, then run this again."
			say "    Then: git -C \"$repo\" worktree remove \"$tree\""
			BLOCKED=1
		else
			act git -C "$repo" worktree remove "$tree"
		fi
	done
fi
[ "$worktrees_seen" = 1 ] || say "  None found."
if [ "$BLOCKED" = 1 ]; then
	say ""
	if [ "$YES" = 1 ]; then
		say "Stopped before removing anything else. Fix the worktrees above and run ./uninstall.sh --yes again."
		exit 2
	fi
	say "With --yes it would stop here, before step 5, until those worktrees are clean."
fi

# --- 5. Archive and remove the two folders -------------------------------------

say ""
say "5. Archive and remove $OFFICE and $CASTLE"
present=""
[ -e "$OFFICE" ] && present="$present .hogwarts"
[ -e "$CASTLE" ] && present="$present hogwarts"
if [ -z "$present" ]; then
	say "  Neither folder is here."
elif [ "$ARCHIVE" = 0 ]; then
	say "  Skipping the archive (--no-archive)."
	for name in $present; do
		act rm -rf "$HOME/$name"
	done
else
	archive="$HOME/hogwarts-fleet-archive-$(date +%Y%m%d-%H%M%S).tar.gz"
	[ ! -e "$archive" ] || die "$archive already exists. Wait a second and run again."
	# shellcheck disable=SC2086
	act tar -czf "$archive" -C "$HOME" $present
	if [ "$YES" = 1 ]; then
		chmod 600 "$archive"
		listing=$(tar -tzf "$archive") || die "could not read $archive back. Nothing was removed."
		for name in $present; do
			printf '%s\n' "$listing" | awk -v p="$name/" 'index($0, p) == 1 { found = 1 } END { exit !found }' ||
				die "$archive does not list $name/. Nothing was removed."
		done
		say "  Checked: the archive lists$present."
	else
		say "  would check that the archive lists$present, then set it to mode 0600"
	fi
	for name in $present; do
		act rm -rf "$HOME/$name"
	done
fi

# --- 6. Snape's agent file -------------------------------------------------------

say ""
say "6. Snape's agent file"
if [ -e "$AGENTS/snape.md" ]; then
	if cmp -s "$REPO_DIR/claude-agents/snape.md" "$AGENTS/snape.md"; then
		act rm "$AGENTS/snape.md"
	else
		say "  $AGENTS/snape.md differs from the repo copy (for example, filled-in placeholders), so it stays."
		say "  If it is the fleet's, remove it yourself: rm \"$AGENTS/snape.md\""
	fi
else
	say "  Not present."
fi

# --- 7. Optional tools -----------------------------------------------------------

say ""
say "7. Optional: tools the fleet asked you to install. Other tools may use them, so this script never removes them."
say "    brew uninstall rtk"
say "    brew uninstall --cask codex"
say "    brew uninstall ripgrep shellcheck"

say ""
if [ "$NEEDS_YOU" = 1 ]; then
	say "Your settings still mention the fleet. Restore or edit them as shown in step 2."
	exit 1
fi
if [ "$YES" = 1 ]; then
	say "Done."
else
	say "Dry run finished. Nothing changed."
	[ "$BLOCKED" = 0 ] || exit 2
fi
