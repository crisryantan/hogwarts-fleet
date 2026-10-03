#!/bin/sh
# Tests a Codex permission profile for Harry before any desk uses it.
# Run it yourself in Terminal, from your clone of the kit:  sh scripts/codex-boundary-test.sh
# It runs small shell commands inside Codex's sandbox. No model runs, no tokens are used and
# nothing is sent to OpenAI. It only touches a test folder it makes, and deletes it at the end.
set -u

CODEX=/opt/homebrew/bin/codex
OFFICE="$HOME/.hogwarts"
TEST="$HOME/.hogwarts-boundary-test-$$"
NAME=fleet-boundary-test

passed=0
failed=0
ok() { printf '   OK      %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf '   FAILED  %s\n' "$1"; failed=$((failed + 1)); }
detail() { printf '%s\n' "$1" | head -2 | cut -c1-160 | sed 's/^/           /'; }
cleanup() { rm -rf "$TEST"; }
trap cleanup EXIT INT TERM

# The profile under test: an allowlist. The desk gets the platform paths tools need, write access
# to its worktree and outbox, read access to the task folder, no network, and nothing else.
# The office is also denied by name, in case a later edit widens the reads by mistake.
PROFILE="permissions.$NAME={filesystem={\":minimal\"=\"read\", \":workspace_roots\"={\".\"=\"write\"}, \"$TEST/tasks\"=\"read\", \"$TEST/outbox\"=\"write\", \"$OFFICE\"=\"deny\"}, network={enabled=false}}"

run() { "$CODEX" sandbox -c "$PROFILE" -P "$NAME" -C "$TEST/wt" -- "$@" 2>&1; }

expect_ok() {
  label=$1; shift
  out=$(run "$@"); rc=$?
  if [ "$rc" -eq 0 ]; then ok "$label"; else bad "$label (exit $rc)"; detail "$out"; fi
}

# Passes only when the sandbox itself refused. Output is never printed when the desk got in.
expect_denied() {
  label=$1; shift
  out=$(run "$@"); rc=$?
  if [ "$rc" -eq 0 ]; then
    bad "$label: the desk got in"
  elif printf '%s' "$out" | grep -q "Operation not permitted"; then
    ok "$label"
  else
    bad "$label: refused, but not by the sandbox (exit $rc)"; detail "$out"
  fi
}

echo "Codex version: $("$CODEX" --version 2>&1)"
echo ""
echo "Setting up a throwaway test folder"
if mkdir -p "$TEST/wt" "$TEST/outbox" "$TEST/tasks" "$TEST/outside" \
  && printf 'worktree file\n' > "$TEST/wt/README.md" \
  && printf 'test task\n' > "$TEST/tasks/TASK.md" \
  && printf 'not for the desk\n' > "$TEST/outside/private.txt"; then
  ok "made $TEST"
else
  bad "could not make $TEST"; exit 1
fi

echo ""
echo "Check 1: commands run under the profile at all"
out=$(run /bin/echo sandbox-ok); rc=$?
if [ "$rc" -eq 0 ] && [ "$out" = "sandbox-ok" ]; then
  ok "a plain command ran inside the profile"
else
  bad "a plain command did not run (exit $rc)"; detail "$out"
  printf '\nStopped here, because the other checks mean nothing until this one passes.\n'
  printf 'Copy everything above and paste it to Claude.\n'
  exit 1
fi

echo ""
echo "Checks 2 to 5: what the desk should be able to do"
expect_ok "reads its worktree" /bin/cat "$TEST/wt/README.md"
expect_ok "writes in its worktree" /usr/bin/touch "$TEST/wt/new-file"
expect_ok "reads the task folder" /bin/cat "$TEST/tasks/TASK.md"
expect_ok "writes to its outbox" /usr/bin/touch "$TEST/outbox/owl.json"

echo ""
echo "Checks 6 to 9: what the desk must not be able to do"
expect_denied "cannot list the office" /bin/ls "$OFFICE"
expect_denied "cannot read a folder it was not given" /bin/cat "$TEST/outside/private.txt"
expect_denied "cannot write the task folder" /usr/bin/touch "$TEST/tasks/forged.md"
expect_denied "cannot write outside its folders" /usr/bin/touch "$TEST/outside/new-file"

echo ""
echo "Check 10: no network"
out=$(run /usr/bin/curl -sS -m 8 -o /dev/null https://example.com); rc=$?
if [ "$rc" -ne 0 ]; then ok "could not reach example.com"; detail "$out"; else bad "reached example.com"; fi

echo ""
if [ "$failed" -eq 0 ]; then
  echo "All $passed checks passed. The profile keeps a Codex desk out of the office and out of"
  echo "every folder it is not given. The test folder has been deleted."
else
  echo "$failed check(s) failed, $passed passed. The test folder has been deleted."
  echo "Copy everything above and paste it to Claude."
fi
