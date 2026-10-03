#!/bin/sh
# Switch on the Owl Post background job and send one test owl.
#
# Running this is your step, not a desk's: it loads a launchd job for your
# user, which is a change only the Headmaster makes. Run it yourself in
# Terminal, from your clone of the repo:
#
#   sh scripts/owlpost-setup.sh
#
# It prints OK or FAILED after each step and stops at the first failure.
# The same steps by hand are in ~/.hogwarts/pending/b-owlpost-launchctl.txt.
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
LABEL=com.hogwarts.owlpost
TEMPLATE="$OFFICE/launchd/$LABEL.plist"
AGENTS="$HOME/Library/LaunchAgents"
TARGET="$AGENTS/$LABEL.plist"
DOMAIN="gui/$(id -u)"

echo "Step 1 of 6: checking the seven desk outboxes"
count=$(find "$CASTLE/desks" -mindepth 2 -maxdepth 2 -type d -name outbox 2>/dev/null | wc -l | tr -d ' ')
if [ "$count" -eq 7 ]; then ok "found $count outboxes"; else fail "expected 7 outboxes, found $count"; fi

echo "Step 2 of 6: making sure the logs folder exists"
if mkdir -p "$OFFICE/logs" && chmod 700 "$OFFICE/logs"; then ok "$OFFICE/logs is ready"; else fail "could not create $OFFICE/logs"; fi

echo "Step 3 of 6: running the Owl Post once by hand"
out=$(/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$OFFICE'); from fleet.owl_post import main; sys.exit(main())" 2>&1)
rc=$?
printf '%s\n' "$out" | head -8 | sed 's/^/           /'
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -Eq '"ok": ?true'; then ok "one clean pass"; else fail "the hand pass did not report ok (exit $rc)"; fi

echo "Step 4 of 6: checking the job file and copying it into place"
plutil -lint "$TEMPLATE" >/dev/null 2>&1 || fail "the job template did not pass plutil -lint"
if mkdir -p "$AGENTS" && cp "$TEMPLATE" "$TARGET" && chmod 644 "$TARGET"; then ok "copied to $TARGET"; else fail "could not copy the job file"; fi

echo "Step 5 of 6: switching the job on"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
	ok "it was already switched on"
else
	launchctl bootstrap "$DOMAIN" "$TARGET" 2>&1 | sed 's/^/           /'
	if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then ok "switched on"; else fail "launchd did not accept the job"; fi
fi

echo "Step 6 of 6: sending a test owl from McGonagall to Hermione"
printf '%s\n' '{"to": "hermione", "kind": "fyi", "subject": "owl post test", "body": "hello"}' >"$CASTLE/desks/mcgonagall/outbox/hello.json"
# The Owl Post renames a sent file to .sent/owl_<id>-<original name> and
# delivers the inbox copy as owl_<id>.json.
sent_owl() { find "$CASTLE/desks/mcgonagall/outbox/.sent" -maxdepth 1 -name 'owl_*-hello.json' -newer "$TARGET" 2>/dev/null | head -1; }
waited=0
while [ -z "$(sent_owl)" ] && [ "$waited" -lt 30 ]; do
	sleep 2
	waited=$((waited + 2))
done
moved=$(sent_owl)
if [ -n "$moved" ]; then
	ok "the owl was delivered after about $waited seconds"
	owl=$(basename "$moved" | sed 's/-hello\.json$//')
	if [ -e "$CASTLE/desks/hermione/inbox/$owl.json" ]; then ok "it is in the Hermione inbox as $owl.json"; else fail "the owl left the outbox but is not in the Hermione inbox"; fi
else
	echo "           last lines of the Owl Post logs:"
	tail -5 "$OFFICE/logs/owlpost.out.log" "$OFFICE/logs/owlpost.err.log" 2>/dev/null | sed 's/^/           /'
	fail "the test owl did not move within 30 seconds"
fi

echo ""
echo "All six steps passed. The Owl Post is live."
echo "To switch it off later:  launchctl bootout $DOMAIN/$LABEL && rm $TARGET"
