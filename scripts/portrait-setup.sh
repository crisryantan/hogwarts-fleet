#!/bin/sh
# Switch on Dumbledore's nightly review: enable the portrait desk and load its
# weekday 22:30 job, which exports the day from the store and then runs him.
#
# Running this is your step, not a desk's: it enables a headless desk and loads
# a launchd job for your user, which only the Headmaster does. Run it yourself
# in Terminal, from your clone of the repo, once `claude auth login` is done:
#
#   sh scripts/portrait-setup.sh
#
# It prints OK or FAILED after each step and stops at the first failure.
# Nothing here applies a patch. Dumbledore only proposes, and you apply what
# you accept with castle portrait apply. auto-portrait stays off until you
# write ~/.hogwarts/auto-portrait yourself.
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
CASTLE="$HOME/hogwarts"
LABEL=com.hogwarts.portrait
TEMPLATE="$OFFICE/launchd/$LABEL.plist"
AGENTS="$HOME/Library/LaunchAgents"
TARGET="$AGENTS/$LABEL.plist"
DOMAIN="gui/$(id -u)"
MARKER="$OFFICE/desks/portrait/enabled"

# One fleet module, run the way launchd runs it: a cleared environment and the system Python.
run_module() {
	module=$1
	shift
	/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$OFFICE'); from fleet.$module import main; sys.exit(main())" "$@" 2>&1
}

echo "Step 1 of 6: checking the portrait's files"
for file in "$OFFICE/fleet/portrait.py" "$OFFICE/fleet/portrait_patch.py" "$OFFICE/desks/portrait/BRIEF.md" \
	"$OFFICE/desks/portrait/settings.json" "$OFFICE/desks/portrait/role.json" "$TEMPLATE"; do
	[ -f "$file" ] || fail "$file is missing. Reinstall the office from a clone that has stage 4"
done
for folder in "$CASTLE/desks/portrait/inbox" "$CASTLE/desks/portrait/outbox"; do
	[ -d "$folder" ] || fail "$folder is missing"
done
ok "the office and the castle have everything Dumbledore needs"

echo "Step 2 of 6: reading the exact command the fleet would run for Dumbledore"
out=$(run_module run_desk portrait --dry-run)
rc=$?
printf '%s\n' "$out" | grep -E '"(model|effort|cwd)"' | sed 's/^ */           /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -Eq '"ok": ?true'; then ok "the dry run planned a run and started nothing"; else fail "the dry run did not report ok (exit $rc)"; fi
if printf '%s' "$out" | grep -Eq '"blocked": ?true'; then fail "his model is blocked here. castle desk model portrait <model> pins an allowed one"; fi

echo "Step 3 of 6: exporting today's Pensieve by hand, with no owl and no run"
out=$(run_module portrait --export-only)
rc=$?
printf '%s\n' "$out" | head -4 | cut -c1-300 | sed 's/^/           /'
day=$(printf '%s' "$out" | sed -n 's/.*"date": *"\([0-9-]*\)".*/\1/p' | head -1)
if [ "$rc" -eq 0 ] && [ -n "$day" ] && [ -f "$CASTLE/desks/portrait/inbox/export-$day.json" ]; then
	ok "the export for $day is in Dumbledore's inbox"
else
	fail "the export did not report ok (exit $rc)"
fi

echo "Step 4 of 6: switching the portrait desk on"
if [ -f "$MARKER" ] && [ ! -L "$MARKER" ]; then
	ok "it was already on"
elif (umask 077 && : >"$MARKER") && chmod 600 "$MARKER"; then
	ok "made $MARKER"
else
	fail "could not make $MARKER"
fi

echo "Step 5 of 6: checking the job file and copying it into place"
plutil -lint "$TEMPLATE" >/dev/null 2>&1 || fail "the job template did not pass plutil -lint"
if mkdir -p "$OFFICE/logs" "$AGENTS" && cp "$TEMPLATE" "$TARGET" && chmod 644 "$TARGET"; then ok "copied to $TARGET"; else fail "could not copy the job file"; fi

echo "Step 6 of 6: switching the job on"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
	ok "it was already switched on"
else
	launchctl bootstrap "$DOMAIN" "$TARGET" 2>&1 | sed 's/^/           /'
	if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then ok "switched on"; else fail "launchd did not accept the job"; fi
fi

echo ""
echo "All six steps passed. Dumbledore reviews the day at 22:30 on weekdays."
echo "In the morning, nothing has changed until you say so:"
echo "  $OFFICE/bin/castle portrait patches"
echo "  $OFFICE/bin/castle portrait show <date>"
echo "  $OFFICE/bin/castle portrait apply <date> --sha256 <hash from show> [--only <op ids>]"
echo "To switch it off later:  launchctl bootout $DOMAIN/$LABEL && rm $TARGET $MARKER"
