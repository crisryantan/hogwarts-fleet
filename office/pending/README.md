# Pending changes

Nothing in this folder has been applied. Each item changes your own config or starts a job, so you apply it yourself. Back up any file before you touch it, as `<file>.pre-hogwarts-<YYYYMMDD-HHMM>`.

## (0) The fleet must be in the office before anyone opens ~/hogwarts

The castle's `.claude/settings.json` takes effect as soon as it's installed. Every Claude session you open in `~/hogwarts` runs its four hooks. Each hook imports `fleet.hooks.<name>` from `~/.hogwarts` through the wrapper line, so `fleet/` must sit in the office first.

The hooks use the `-c` import form. If `fleet/` is ever missing, the import fails with exit 1, which Claude Code treats as a non-blocking error. A missing script file would exit 2 instead, and on UserPromptSubmit that blocks every prompt.

Check the install from your terminal:

```
ls -ld ~/.hogwarts/fleet ~/.hogwarts/logs
cd ~/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests_fleet -t .
~/.hogwarts/bin/castle doctor
```

## (a) User settings: the deny rules and the push gate

File: `~/.claude/settings.json`. Back it up first:

```
cp ~/.claude/settings.json ~/.claude/settings.json.pre-hogwarts-$(date +%Y%m%d-%H%M)
```

There are two snippets. Merge each one by hand: add the list items to the existing arrays and keep everything else.

`a1-user-settings-deny.merge.json` denies the office to your own Claude sessions through the file tools, and denies the usual ways of running `castle` and `fleet` from Bash. If your settings have no `deny` list yet, this adds one. A Bash deny rule only matches the command as Claude usually writes it. It is not a wall around the program, so the sandbox stays the real boundary for desks.

Apply a1 before you first summon Snape. He is a user-level subagent with Read, so until a1 is in place he could read the office from any of your sessions.

`a2-user-settings-push-gate.merge.json` adds a `PreToolUse` hook on `Bash` that runs the push gate. If you already have a `PreToolUse` Bash hook, it goes next to it as a new entry in the same array. Check afterwards: `grep -c push_gate ~/.claude/settings.json` prints 1.

The push gate blocks an agent's `git push` unless every commit it pushes has a review pass from the other model family. What else it does:

- It refuses force, delete and mirror pushes, pushes chained to other commands, and anything it cannot read. It fails closed.
- It never allows anything your settings would otherwise ask about.
- It reads text, so it is a guardrail against an agent pushing by habit or mistake, not a wall. A session with your permissions can always push some other way.
- Pushes you type in your own terminal never reach it.

Undo: copy the backup back over `~/.claude/settings.json`.

## (b) Load the Owl Post job

File: `b-owlpost-launchctl.txt`. It has the exact commands, a test and the undo.

The Owl Post runs whenever a desk outbox changes (`WatchPaths` on all seven outboxes), and every five minutes to sweep up a file it left for later. It runs the same wrapper line as `castle`, logs to `~/.hogwarts/logs` with a 077 umask, and needs no network. Load it only after the castle exists, because launchd watches the outbox folders.

Only McGonagall's request owls start a headless run. Any other owl waits in the inbox. The one other thing an owl starts is a review: Harry's handoff for his own active task starts that task's review, as in (g). A refused owl file raises a headmaster event, so you see it in the digest.

Most of the other jobs have setup scripts in the kit. `scripts/patrol-setup.sh` loads the patrol's five plists from `launchd/` (map, morning, keeper, scoreboard, gringotts) in shadow mode, at onboarding stage 5.2. `scripts/portrait-setup.sh` loads Dumbledore's, at onboarding stage 5.3. You load Ollivander's job by hand at onboarding stage 5.5.

## (c) Codex approval and enabling desks

File: `c-codex-approval.txt`.

Every headless desk starts disabled. The Owl Post delivers to a disabled desk's inbox but never launches it, and `castle audit` escalates an owl that sits unacked for two hours. To enable a desk you make a plain file named `enabled` in its office folder. Read the desk's `--dry-run` command first. Hermione, Ron and Dumbledore also need `claude auth login` before you enable them.

Harry and Moody stay disabled until your organization approves Codex for its source code. Their read boundary comes from a fleet permission profile:

- run_desk runs them under that profile, an allowlist that denies the office and every folder it does not name, with no network.
- Neither may touch `/private/tmp` or your per-user temp folder. Harry gets a private temp folder of his own as `TMPDIR`, emptied before each run, and both may only read xcrun's lookup cache.
- `scripts/codex-boundary-test.sh` in the kit proves that kind of profile on your Mac, and `scripts/codex-exec-boundary-test.py` proves it again under real `codex exec` runs. Run both before enabling Harry, and after every Codex upgrade.

## (d) Registering and closing a task

McGonagall writes `tasks/<id>/TASK.md` and waits for your go. Nothing registers the task in the store for her. After your go, she gives you one command to run in your terminal:

```
~/.hogwarts/bin/castle task create --id <tk_id> --desk mcgonagall --title "<title>" --intent-path ~/hogwarts/tasks/<tk_id>/TASK.md
```

She routes the task only after you say it is registered. A request for a task the store does not know is refused, and you get a headmaster event.

"Mischief managed <task-id>" closes a task only when it is awaiting close and the hook can see that you typed the prompt yourself. If the hook says it could not confirm, close it from your terminal:

```
~/.hogwarts/bin/castle token mint <tk_id>
~/.hogwarts/bin/castle task close <tk_id> --reason complete --token-stdin
```

Paste the token from the first command into the second when it waits for stdin. The token works once and expires.

Headmaster events stay in the digest and on each prompt until you ack them with `castle event ack <id>`.

## (e) Codex hooks: the push gate for your own Codex sessions

File: `~/.codex/hooks.json`. Create it if it doesn't exist, and back it up first if it does. Merge `a3-codex-hooks-push-gate.merge.json` into it: the `PreToolUse` entry goes into its `hooks` object. Codex sends hooks the same fields as Claude Code and blocks on exit 2, so the same gate script works.

Codex runs a hook only after you trust it: open a Codex session, type `/hooks`, and trust the push gate entry.

Harry and Moody never load it, because their runs use `--ignore-user-config` and `features.hooks = false`, and they have no network. So this covers your own interactive Codex sessions only.

Undo: restore the backup, or remove the entry.

## (f) Desk settings: the push gate for headless desks

The desk settings for Hermione, Ron and Dumbledore include the push gate as a top-level `hooks` object. `a4-desk-settings-push-gate.merge.json` shows that block on its own.

This is defence in depth. Hermione and Dumbledore have no network, and Ron's sandbox reaches only api.github.com, which takes no `git push`, so a push fails anyway. Hooks in a `--settings` file run under `--restricted`, so the gate blocks there too. If the hook cannot load, it exits 1, which Claude Code treats as a non-blocking error, so it changes nothing else.

Undo: remove the `hooks` object from each desk's settings.

## (g) The review loop, step by step

Every `fleet` command prints one JSON object with `"ok": true` or `"ok": false` and a reason. Find task ids with `~/.hogwarts/bin/castle task list`.

`--repo-dir` below is the main checkout of your repo: a folder inside your home folder, outside the office and the castle, with its own `.git` folder and no symlink on the way.

1. McGonagall writes TASK.md, you say go, and you register her task as in (d). Each acceptance check is either one backtick command and nothing else, which verify runs, or plain words with no backticks, which the reviewer judges. A check that mixes a backtick command with other text is marked malformed in the evidence and never runs.
2. She posts a request to Harry. The Owl Post holds it and you get a headmaster event: a build task is waiting for its worktree.
3. Give Harry's task a worktree on a new branch. If Harry is enabled, his run starts:

```
~/.hogwarts/bin/fleet worktree <harry-task-id> --repo-dir <path-to-your-checkout> --branch <new-branch>
```

4. Harry leaves his changes uncommitted and posts a handoff with a COMMIT MESSAGE section. Once Hermione is enabled, the Owl Post starts the review on its own: the review script commits for him outside his sandbox, runs the acceptance checks, and runs Hermione. Only a handoff the Owl Post stamped as Harry's, for his own active task with a worktree, starts one, and only once. A handoff that starts nothing leaves a routine event saying why. The review reads the newest handoff that still passes those checks, and only that one: an owl that is not a handoff never takes its place. The review first waits for Harry's run to end. While Hermione's run slots are all busy, or another review of the task is running, it waits and tries again on each Owl Post pass, for up to four hours. A review that dies part way is started again on the next pass, three tries at most. When it gives up or fails, you get a headmaster event. Run it by hand only then, or when the Owl Post is off:

```
~/.hogwarts/bin/fleet review <harry-task-id>
```

5. On CHANGES, the review starts Harry's fix round on its own. He reads `review-latest.md` next to TASK.md, and his next handoff starts the next review. After `REVIEW_ROUND_CAP` rounds (three) the loop stops, and you get one headmaster event naming the task and its last verdict. On HEADMASTER nothing more starts, and you get the event as before. A review whose commit and handoff are what the last verdict already judged is refused before anything changes. `fleet build` and every review of a task, by hand or automatic, take the same per-task lock, so a fix round never starts mid-review and two reviews never run at once. The run a fix round starts is handed that lock and holds it until it ends, so no review starts while Harry may still be writing, and a review you run by hand meanwhile is refused. If a review is killed after its verdict, the next Owl Post pass finishes what follows it without opening another round: a fix round, push or draft PR it had not begun starts then, once, and one it had begun is never started again, since it may or may not have happened. You get one headmaster event saying so, and what to look at. Start a fix round by hand after `castle task allow-round <harry-task-id>` at the cap, when the loop could not start one (Harry disabled or at his daily cap, which you hear about), or after a review you ran by hand, which stops at its verdict:

```
~/.hogwarts/bin/fleet build <harry-task-id>
```

6. On PASS, no build or review starts, and the task awaits close. What happens next depends on one opt-in, which is off by default.

   With it off, the review loop gives you one headmaster event saying the task is ready for push. Push exactly the reviewed commit yourself. It shows the commits and waits for you to type the branch name, then prints the `gh` command for a draft PR. Read the PR text before you run that:

```
~/.hogwarts/bin/fleet push <harry-task-id>
```

   With it on, the review loop does that push for you after its own PASS: exactly the reviewed commit, to the branch the worktree was made on, through every check `fleet push` makes, and never forced. Then it opens a draft PR with your `gh` login, titled with the handoff's COMMIT MESSAGE subject, with the handoff's PR BODY DRAFT as its body. Before it pushes anything, it refuses PR text or commit messages that hold a fleet word, or anything shaped like a token, key or email. Each check reads every commit message and every added line whole, or refuses when there is too much to read. You get one headmaster event with the PR's URL. If anything fails, such as a push check, the remote, `gh` or its login, it stops there, retries nothing, and you get one headmaster event saying why. It never reads or prints a credential. Push by hand with `fleet push` as above. The PR stays a draft: marking it ready, requesting reviews and merging stay yours. A review you start by hand with `fleet review` never pushes.

   The opt-in is one file in the office, which no desk can write. Turn it on from your terminal, and off by deleting the file:

```
echo on > ~/.hogwarts/auto-draft-pr
rm ~/.hogwarts/auto-draft-pr
```

   It counts only while it is a plain file you own that no one else can write, holding exactly `on`. A file anywhere else, a desk folder, a task folder, an owl, TASK.md or `standing-orders.md`, is ignored. Write the same order in `standing-orders.md` for people to read, but code never parses it.

7. For a commit from one of your own Claude sessions, Moody reviews it in a detached worktree. On PASS, that session's `git push` gets through the gate. For a fix round, pass `--task <id>` instead of `--title`:

```
~/.hogwarts/bin/fleet review own --repo-dir <path-to-your-checkout> --title "<what the change does>"
```

8. To rerun the acceptance checks on their own: `~/.hogwarts/bin/fleet verify <task-id>`. Each check runs inside a Codex permission profile with no network and no office, through `codex sandbox`, which runs no model.

Moody's reviews send the diff to OpenAI, so `fleet review own` runs only once Moody is enabled, after your organization approves Codex for its source code.
