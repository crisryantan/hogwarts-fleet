#!/bin/sh
# Move the fleet's background jobs from launchd to a terminal you start (fleet loops).
#
# macOS keeps launchd jobs out of ~/Documents, so the Owl Post, the Map and the
# closer fail on a repo cloned there. Run from a terminal window, the same jobs
# get that window's folder access instead. Running this is your step, not a
# desk's: it unloads launchd jobs for your user. Run it yourself in Terminal,
# from your clone of the repo:
#
#   sh scripts/loops-setup.sh
#
# It makes ~/.hogwarts/loops/jobs, adds every com.hogwarts job it finds in
# ~/Library/LaunchAgents to it, checks fleet loops can run every listed job,
# and only then unloads and removes each of those plists, so no job runs twice
# and a job fleet loops would refuse stays with launchd. It starts nothing:
# start the loops afterwards in a herdr pane or a Terminal window with
# ~/.hogwarts/bin/fleet loops. It prints OK or FAILED after each step and stops
# at the first failure. Safe to run again.
set -u

ok() { printf '   OK      %s\n' "$1"; }
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
LIST="$OFFICE/loops/jobs"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
FLEET="$OFFICE/bin/fleet"

echo "Step 1 of 5: checking the office has fleet loops"
[ -f "$OFFICE/fleet/loops.py" ] && [ -x "$FLEET" ] || fail "$OFFICE/fleet/loops.py or $FLEET is missing. Reinstall the office from this clone first"
ok "the office can run fleet loops"

echo "Step 2 of 5: making the job list"
if [ -L "$OFFICE/loops" ] || [ -L "$LIST" ]; then fail "$LIST or its folder is a symlink"; fi
if mkdir -p "$OFFICE/loops" && chmod 700 "$OFFICE/loops"; then :; else fail "could not make $OFFICE/loops"; fi
if [ -f "$LIST" ]; then
	ok "$LIST is already there"
elif printf '# Background jobs fleet loops runs, one launchd/ job name per line. The setup scripts add to it.\n' >"$LIST" && chmod 600 "$LIST"; then
	ok "made $LIST"
else
	fail "could not make $LIST"
fi

# Adding a job to the list is harmless while launchd still has it: fleet loops skips such a job.
echo "Step 3 of 5: adding each launchd job to the list, unloading nothing yet"
found=""
for plist in "$AGENTS"/com.hogwarts.*.plist; do
	[ -e "$plist" ] || continue
	label=$(basename "$plist" .plist)
	job=${label#com.hogwarts.}
	case $job in
	"" | *[!a-z0-9-]*) fail "$plist is not a fleet job name" ;;
	esac
	[ -f "$OFFICE/launchd/$label.plist" ] || fail "$OFFICE/launchd/$label.plist is missing, so fleet loops could not run $job"
	if ! grep -qx "$job" "$LIST"; then
		printf '%s\n' "$job" >>"$LIST" || fail "could not add $job to $LIST"
	fi
	found="$found $job"
	ok "$job is in the list"
done
[ -n "$found" ] || ok "launchd has no fleet job; the setup scripts will add theirs to $LIST"

echo "Step 4 of 5: checking fleet loops can run every listed job, starting nothing"
out=$("$FLEET" loops --dry-run 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
	printf '%s\n' "$out" | grep -E '"(job|error)"' | sed 's/^ */           /'
	if printf '%s' "$out" | grep -q '"error"'; then fail "a job above cannot run under fleet loops, so launchd keeps every job"; fi
	ok "every listed job can run"
elif [ "$(grep -cE '^[a-z]' "$LIST")" -eq 0 ]; then
	ok "the list is empty until a setup script adds a job"
else
	printf '%s\n' "$out" | head -3 | sed 's/^/           /'
	fail "fleet loops --dry-run did not report ok (exit $rc), so launchd keeps every job"
fi

echo "Step 5 of 5: unloading and removing each moved launchd job"
for job in $found; do
	label="com.hogwarts.$job"
	if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
		launchctl bootout "$DOMAIN/$label" 2>&1 | sed 's/^/           /'
		launchctl print "$DOMAIN/$label" >/dev/null 2>&1 && fail "launchd still has $label loaded"
	fi
	rm "$AGENTS/$label.plist" || fail "could not remove $AGENTS/$label.plist"
	ok "$job now runs only under fleet loops"
done
[ -n "$found" ] || ok "nothing to unload"

echo ""
echo "Done. Start the loops in a herdr pane or a Terminal window and leave it open:"
echo "  $FLEET loops"
echo "Or add $OFFICE/bin/fleet-loops.command to System Settings > General > Login Items."
echo "The window's app needs Documents access: allow it when macOS asks, or in"
echo "System Settings > Privacy & Security > Files and Folders. Never give /usr/bin/python3 Full Disk Access."
echo "To go back to launchd: stop the loops (Ctrl+C), rm $LIST, then run each setup script again."
