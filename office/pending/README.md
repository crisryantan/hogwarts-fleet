# Pending changes for Ryan

Nothing in this folder has been applied. Each item changes your own config or starts a job, so you apply it yourself. Back up any file before you touch it, as `<file>.pre-hogwarts-<YYYYMMDD-HHMM>`.

## (0) The fleet must be in the office before anyone opens ~/hogwarts

The castle's `.claude/settings.json` is live. Every Claude session you open in `~/hogwarts` runs its four hooks. Each hook imports `fleet.hooks.<name>` from `/Users/crisryantan/.hogwarts` through the wrapper line, so `fleet/` must sit in the office first.

The hooks use the `-c` import form. If `fleet/` is ever missing, the import fails with exit 1, which Claude Code treats as a non-blocking error. A missing script file would exit 2 instead, and on UserPromptSubmit that blocks every prompt.

Check the install from your terminal:

```
ls -ld /Users/crisryantan/.hogwarts/fleet /Users/crisryantan/.hogwarts/logs
cd /Users/crisryantan/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests_fleet -t .
/Users/crisryantan/.hogwarts/bin/castle doctor
```

## (a) User settings: deny the office, then the push gate

File: `~/.claude/settings.json`. Back it up first:

```
cp ~/.claude/settings.json ~/.claude/settings.json.pre-hogwarts-$(date +%Y%m%d-%H%M)
```

There are two snippets. Merge each one by hand: add the list items to the existing arrays and keep everything else.

`a1-user-settings-deny.merge.json` is safe to apply now:

```json
{
  "permissions": {
    "deny": [
      "Read(~/.hogwarts/**)",
      "Edit(~/.hogwarts/**)",
      "Write(~/.hogwarts/**)",
      "Bash(/Users/crisryantan/.hogwarts/bin/castle *)",
      "Bash(~/.hogwarts/bin/castle *)",
      "Bash(castle *)",
      "Bash(/usr/bin/env -i /usr/bin/python3 *)",
      "Bash(/usr/bin/python3 -I -B *)"
    ]
  }
}
```

Your own Claude sessions get no access to the office through the file tools, and the usual ways of running the store from Bash are denied. If your settings have no `deny` list yet, this adds one. The castle and desk settings use the same Read, Edit and Write rules. A Bash deny rule only matches the command as Claude usually writes it. It is not a wall around the program, so the sandbox stays the real boundary for desks.

Apply a1 before you first summon Snape. He is a user-level subagent with Read, so until a1 is in place he could read the office from any of your sessions.

`a2-user-settings-push-gate.merge.json` adds a `PreToolUse` hook on `Bash` that runs the push gate. If you already have a `PreToolUse` Bash hook, it goes next to it as a new entry in the same array. Apply it only after the push gate script (`fleet/hooks/push_gate.py`, Stage 2) exists and passes its tests. Until then the hook would fail to import on every Bash call. A failing hook exits 1, which Claude Code treats as a non-blocking error, so it would fail open and add noise.

Undo: copy the backup back over `~/.claude/settings.json`.

## (b) Load the Owl Post job

File: `b-owlpost-launchctl.txt`. It has the exact commands, a test and the undo.

The Owl Post runs whenever a desk outbox changes (`WatchPaths` on all seven outboxes), and every five minutes to sweep up a file it left for later. It runs the same wrapper line as `castle`, logs to `~/.hogwarts/logs` with a 077 umask, and needs no network. Load it only after the castle exists, because launchd watches the outbox folders.

Only McGonagall's request owls start a headless run. Any other owl waits in the inbox. A refused owl file raises a headmaster event, so you see it in the digest.

The other five plists in `launchd/` (map, morning, keeper, portrait, gringotts) are templates for later stages. The modules they call do not exist yet, so do not load them.

## (c) Codex approval and enabling desks

File: `c-codex-approval.txt`.

Every headless desk starts disabled. The Owl Post delivers to a disabled desk's inbox but never launches it, and `castle audit` escalates an owl that sits unacked for two hours. Hermione, Ron and the portrait stay disabled until you run `claude auth login`. To enable a desk you make a plain file named `enabled` in its office folder. Read the desk's `--dry-run` command first.

Harry and Moody stay disabled until your organization approves Codex for its source code. Their read boundary is closed: run_desk runs them under a fleet permission profile, an allowlist that denies the office and every folder it does not name, with no network. `scripts/codex-boundary-test.sh` in the kit proves that kind of profile on your Mac. Confirm it once under a real `codex exec` run before enabling Harry.

## (d) Registering and closing a task

McGonagall writes `tasks/<id>/TASK.md` and waits for your go. Nothing registers the task in the store for her. After your go, she gives you one command to run in your terminal:

```
/Users/crisryantan/.hogwarts/bin/castle task create --id <tk_id> --desk mcgonagall --title "<title>" --intent-path /Users/crisryantan/hogwarts/tasks/<tk_id>/TASK.md
```

She routes the task only after you say it is registered. A request for a task the store does not know is refused, and you get a headmaster event.

"Mischief managed <task-id>" closes a task only when it is awaiting close and the hook can see that you typed the prompt yourself. If the hook says it could not confirm, close it from your terminal:

```
/Users/crisryantan/.hogwarts/bin/castle token mint <tk_id>
/Users/crisryantan/.hogwarts/bin/castle task close <tk_id> --reason complete --token-stdin
```

Paste the token from the first command into the second when it waits for stdin. The token works once and expires.

Headmaster events stay in the digest and on each prompt until you ack them with `castle event ack <id>`.
