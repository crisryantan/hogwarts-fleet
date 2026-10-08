#!/bin/sh
# Switch on the patrol in shadow mode: the Marauder's Map, Ron's morning
# lineup, keeper's watch and weekly scoreboard, Hermione's bot pass and
# Gringotts' nightly backup.
#
# Running this is your step, not a desk's: it switches Ron on and loads five
# launchd jobs for your user, which are changes only the Headmaster makes.
# Run it yourself in Terminal, from your clone of the repo:
#
#   sh scripts/patrol-setup.sh
#
# It prints OK or FAILED after each step and stops at the first failure.
# Steps that already passed are safe to run again. Shadow mode stays on until
# you delete ~/.hogwarts/patrol/shadow yourself. With terminal loops chosen
# (~/.hogwarts/loops/jobs is there) step 9 adds the five jobs to that list
# instead and loads nothing into launchd.
set -u

ok() { printf '   OK      %s\n' "$1"; }
note() { printf '   NOTE    %s\n' "$1"; }
fail() {
	printf '   FAILED  %s\n\nStopped here. Copy everything above and paste it to Claude.\n' "$1"
	exit 1
}

case ${HOME:-} in
/ | "") fail "HOME is not set to your home folder" ;;
/*) ;;
*) fail "HOME must be an absolute path" ;;
esac
case $HOME in
*[!A-Za-z0-9._/-]*) fail "HOME has characters the fleet's fixed paths cannot hold" ;;
esac

OFFICE="$HOME/.hogwarts"
CONFIG="$OFFICE/fleet/config.py"
SHADOW="$OFFICE/patrol/shadow"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
JOBS="map morning keeper scoreboard gringotts"
LOOPS_LIST="$OFFICE/loops/jobs"

# Run one fleet module through the same cleared-environment line launchd uses.
run_module() {
	module=$1
	shift
	/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$OFFICE'); from fleet.$module import main; sys.exit(main())" "$@"
}

# The value of one NAME = "value" line in the fleet config.
config_value() {
	grep -E "^$1 = \"" "$CONFIG" | head -1 | cut -d '"' -f 2
}

echo "Step 1 of 9: checking the office has the patrol scripts"
for module in patrol map morning keeper scoreboard gringotts; do
	[ -f "$OFFICE/fleet/$module.py" ] || fail "$OFFICE/fleet/$module.py is missing. Install this stage's office files first"
done
for job in $JOBS; do
	[ -f "$OFFICE/launchd/com.hogwarts.$job.plist" ] || fail "$OFFICE/launchd/com.hogwarts.$job.plist is missing"
done
ok "the scripts and the five job templates are in place"

echo "Step 2 of 9: checking gh and its sign-in"
GH=$(config_value GH_BIN)
[ -n "$GH" ] && [ -x "$GH" ] || fail "GH_BIN in $CONFIG does not name an installed gh. Set it to the output of: command -v gh"
if "$GH" auth status >/dev/null 2>&1; then ok "gh at $GH is signed in"; else fail "gh is not signed in. Run: $GH auth login"; fi

echo "Step 3 of 9: checking the GitHub account the Map watches"
ACCOUNT=$(config_value GITHUB_ACCOUNT)
case $ACCOUNT in
"" | "<github-account>") fail "GITHUB_ACCOUNT in $CONFIG is still the placeholder. Put your GitHub login there" ;;
*) ok "the Map watches open PRs by $ACCOUNT" ;;
esac

echo "Step 4 of 9: making sure shadow mode is on"
if [ -f "$SHADOW" ]; then
	ok "shadow mode is on: $SHADOW is there"
else
	if mkdir -p "$OFFICE/patrol" && chmod 700 "$OFFICE/patrol" &&
		printf 'Shadow mode is on. Delete this file to go live.\n' >"$SHADOW" && chmod 600 "$SHADOW"; then
		ok "made $SHADOW, so shadow mode is on"
	else
		fail "could not make $SHADOW"
	fi
fi
if mkdir -p "$OFFICE/logs" && chmod 700 "$OFFICE/logs"; then ok "$OFFICE/logs is ready"; else fail "could not create $OFFICE/logs"; fi

echo "Step 5 of 9: running one Map round by hand"
out=$(run_module map 2>&1)
rc=$?
printf '%s\n' "$out" | head -4 | cut -c 1-300 | sed 's/^/           /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -Eq '"ok": ?true'; then ok "one clean round, written to $OFFICE/patrol/map"; else fail "the round did not report ok (exit $rc)"; fi

echo "Step 6 of 9: taking one Gringotts backup and a restore drill"
out=$(run_module gringotts 2>&1)
rc=$?
printf '%s\n' "$out" | head -2 | cut -c 1-300 | sed 's/^/           /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -Eq '"ok": ?true'; then ok "a backup is in $OFFICE/backups"; else fail "the backup did not report ok (exit $rc)"; fi
out=$(run_module gringotts --drill 2>&1)
rc=$?
printf '%s\n' "$out" | head -2 | cut -c 1-300 | sed 's/^/           /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -Eq '"ok": ?true'; then ok "the restore drill passed, in a temp folder"; else fail "the restore drill did not pass (exit $rc)"; fi

echo "Step 7 of 9: reading Ron's dry run and switching Ron on"
out=$(run_module run_desk ron --dry-run 2>&1)
rc=$?
[ "$rc" -eq 0 ] || fail "Ron's dry run failed (exit $rc): $(printf '%s' "$out" | head -1 | cut -c 1-200)"
printf '%s\n' "$out" | grep -E '"(model|effort|cwd|enabled|stopped|blocked)"' | sed 's/^ */           /'
if [ -f "$OFFICE/desks/ron/enabled" ]; then
	ok "Ron was already switched on"
elif : >"$OFFICE/desks/ron/enabled" && chmod 600 "$OFFICE/desks/ron/enabled"; then
	ok "switched Ron on: $OFFICE/desks/ron/enabled"
else
	fail "could not make $OFFICE/desks/ron/enabled"
fi

echo "Step 8 of 9: checking Hermione for the bot pass"
if [ -f "$OFFICE/desks/hermione/enabled" ]; then
	ok "Hermione is on, so the bot pass writes drafts"
else
	note "Hermione is off, so the Map skips the bot pass until the review stage switches her on"
fi

if [ -f "$LOOPS_LIST" ]; then
	echo "Step 9 of 9: checking the five jobs and adding them to the terminal loops"
	for job in $JOBS; do
		label="com.hogwarts.$job"
		template="$OFFICE/launchd/$label.plist"
		plutil -lint "$template" >/dev/null 2>&1 || fail "$template did not pass plutil -lint"
		[ ! -e "$AGENTS/$label.plist" ] || fail "launchd also has $label. Run sh scripts/loops-setup.sh to move it to the terminal loops"
		grep -qx "$job" "$LOOPS_LIST" || printf '%s\n' "$job" >>"$LOOPS_LIST" || fail "could not add $job to $LOOPS_LIST"
		ok "$job is in $LOOPS_LIST"
	done
	note "fleet loops reads the list when it starts: start it, or stop and start it, with $OFFICE/bin/fleet loops"
else
	echo "Step 9 of 9: checking and loading the five jobs"
	mkdir -p "$AGENTS" || fail "could not create $AGENTS"
	for job in $JOBS; do
		label="com.hogwarts.$job"
		template="$OFFICE/launchd/$label.plist"
		target="$AGENTS/$label.plist"
		plutil -lint "$template" >/dev/null 2>&1 || fail "$template did not pass plutil -lint"
		if ! cp "$template" "$target" || ! chmod 644 "$target"; then fail "could not copy $label into $AGENTS"; fi
		if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
			ok "$label was already loaded"
		else
			launchctl bootstrap "$DOMAIN" "$target" 2>&1 | sed 's/^/           /'
			if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then ok "loaded $label"; else fail "launchd did not accept $label"; fi
		fi
	done
fi

echo ""
echo "All nine steps passed. The patrol runs in shadow mode: it writes files under"
echo "$OFFICE/patrol and $OFFICE/backups, and nothing else."
echo "Compare $OFFICE/patrol/lineup/<date>.md with gh each morning. After three"
echo "weekdays that match, go live with:  rm $SHADOW"
if [ -f "$LOOPS_LIST" ]; then
	echo "To switch the jobs off: remove their lines from $LOOPS_LIST, then restart fleet loops."
else
	echo "To switch the jobs off:  for job in $JOBS; do launchctl bootout $DOMAIN/com.hogwarts.\$job; rm $AGENTS/com.hogwarts.\$job.plist; done"
fi
echo "To switch Ron off:  rm $OFFICE/desks/ron/enabled"
