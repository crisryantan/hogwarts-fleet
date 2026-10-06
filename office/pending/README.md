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

McGonagall writes `tasks/<id>/TASK.md` and waits for your go. Nothing she does registers the task in the store. When you're happy with the draft, type exactly this, and nothing else, as your prompt in her session:

```
go <tk_id>
```

The castle's UserPromptSubmit hook takes it from there, the same way it takes "Mischief managed". It checks that you typed the prompt yourself, then registers the task, routes it to Harry, gives his task its worktree and starts his run, all in that one prompt. Its Spec section must open with three lines, which say where the build goes:

```
## Spec
repo: <path-to-your-checkout>
branch: <new-branch>
base: origin/main
```

`repo:` gets the same checks as `--repo-dir` below, `branch:` the same as `--branch`, and `base:` the same as `--base`. The go stores the three values and the TASK.md's sha256 with the task. The worktree is made from the stored values only, so editing TASK.md afterwards changes none of them. If TASK.md changes while the go runs, or anything refuses before the go finishes, nothing is registered, routed or made, and you can type the go again. A go for a task that's already registered changes nothing. A go or a `fleet worktree` for a branch that another one is still making in the same repo is refused at once, so run it again when that one ends. If one says the store couldn't say whether its task kept the worktree, it took nothing back: check the task with `castle task show`, and remove what it names by hand only if the task has no worktree.

The hook prints what happened, or why nothing did. A go only works in McGonagall's session: in any other session, your own included, it says so and changes nothing. When it can't confirm you typed the go, it says so and changes nothing too. Register the task by hand instead:

```
~/.hogwarts/bin/castle task create --id <tk_id> --desk mcgonagall --title "<title>" --intent-path ~/hogwarts/tasks/<tk_id>/TASK.md
```

Then tell McGonagall it's registered, so she routes it, and give Harry's task its worktree as in (g). A request for a task the store does not know is refused, and you get a headmaster event.

"Mischief managed <task-id>" closes a task only when it is awaiting close and the hook can see that you typed the prompt yourself. If the hook says it could not confirm, close it from your terminal:

```
~/.hogwarts/bin/castle token mint <tk_id>
~/.hogwarts/bin/castle task close <tk_id> --reason complete --token-stdin
```

Paste the token from the first command into the second when it waits for stdin. The token works once and expires.

A task in a PR follow-up (step 6b of (g)) is active again until its follow-up passes, so `Mischief managed` waits; close it from your terminal as above if you are done with it, which also stops its follow-up.

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

1. McGonagall writes TASK.md and you type `go <task-id>` in her session, as in (d). The hook registers her task, routes it to Harry, gives his task its worktree on the Spec's new branch and starts his run if Harry is enabled. Then carry on at step 4. Each acceptance check is either one backtick command and nothing else, which verify runs, or plain words with no backticks, which the reviewer judges. A check that mixes a backtick command with other text is marked malformed in the evidence and never runs.
2. Steps 2 and 3 are the fallback, for a go the hook couldn't confirm, and for a build McGonagall routes by owl. You register her task by hand as in (d), and she posts a request to Harry. The Owl Post holds it and you get a headmaster event: a build task is waiting for its worktree.
3. Give Harry's task a worktree on a new branch. If Harry is enabled, his run starts. Under a TASK.md a go started, it takes only the repo, branch and base the go stored:

```
~/.hogwarts/bin/fleet worktree <harry-task-id> --repo-dir <path-to-your-checkout> --branch <new-branch>
```

If a go said Harry's run didn't start, start it with `~/.hogwarts/bin/fleet build <harry-task-id>`.

4. Harry leaves his changes uncommitted and posts a handoff with a COMMIT MESSAGE section. Once Hermione is enabled, the Owl Post starts the review on its own: the review script commits for him outside his sandbox, runs the acceptance checks, and runs Hermione. Only a handoff the Owl Post stamped as Harry's, for his own active task with a worktree, starts one, and only once. A handoff that starts nothing leaves a routine event saying why. The review reads the newest handoff that still passes those checks, and only that one: an owl that is not a handoff never takes its place. The review first waits for Harry's run to end. While Hermione's run slots are all busy, or another review of the task is running, it waits and tries again on each Owl Post pass, for up to four hours. A review that dies part way is started again on the next pass, three tries at most. When it gives up or fails, you get a headmaster event. Run it by hand only then, or when the Owl Post is off:

```
~/.hogwarts/bin/fleet review <harry-task-id>
```

5. On CHANGES, the review starts Harry's fix round on its own. He reads `review-latest.md` next to TASK.md, and his next handoff starts the next review. After `REVIEW_ROUND_CAP` rounds (three) the loop stops, and you get one headmaster event naming the task and its last verdict. On HEADMASTER nothing more starts, and you get the event as before. A review whose commit and handoff are what the last verdict already judged is refused before anything changes. `fleet build` and every review of a task, by hand or automatic, take the same per-task lock, so a fix round never starts mid-review and two reviews never run at once. The run a fix round starts is handed that lock and holds it until it ends, so no review starts while Harry may still be writing, and a review you run by hand meanwhile is refused. If a review is killed after its verdict, the next Owl Post pass finishes what follows it without opening another round. It first publishes the review from the copy kept in the office, if the kill came before `review-latest.md` and the result owl were written, so nothing acts on a missing or older review; if that copy cannot be read, you get one headmaster event and nothing follows the verdict. An ending you already heard about, such as a push that stopped before it began, is left as it is and never tried again. So is a verdict that a newer round of the task has replaced, for example after a review you ran by hand: its review stays published, and nothing follows it. Otherwise a fix round, push or draft PR it had not begun starts then, once, and one it had begun is never started again, since it may or may not have happened. You get one headmaster event saying so, and what to look at. Start a fix round by hand after `castle task allow-round <harry-task-id>` at the cap, when the loop could not start one (Harry disabled or at his daily cap, which you hear about), or after a review you ran by hand, which stops at its verdict:

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

6b. Teammates' comments (opt-in). Once the review loop opened a draft PR for a task, the store binds that PR to the task. With follow-ups on, each Map round reads every bound PR that is still open for a task awaiting close, with the patrol's read-only GraphQL query, and picks the comments that are new to the store:

   - Who counts as a teammate: someone GitHub lists as `OWNER`, `MEMBER` or `COLLABORATOR` on the repo, never a bot, never the PR's author, and never an account in `FOLLOWUP_IGNORED_LOGINS`. Review thread comments, review bodies that comment or ask for changes, and conversation comments all count. A thread comment you already answered by hand in that thread doesn't.
   - What waits: a comment from before the PR was bound or written while follow-ups were not live, a PR whose newest comment is under five minutes old (so one review goes as one follow-up), a review of the task still running, and anything only you can fix (Harry or Hermione off or at their cap, a dirty worktree, gh signed in as another account, a PR whose head moved, or a PR too big to read whole), which you hear about once a day.
   - What happens: the comments are recorded as handled, Harry's fix request is stored and his task goes back to active, in one store transaction. The GitHub text goes to `followup-<n>.md` next to TASK.md, scrubbed and quoted line by line as data, and Harry starts on that request. His handoff carries a `THREADS (<follow-up id>)` section marking every item FIXED (he changed the code) or PUSHBACK (an answer, a decline or his evidence), each with its one-line reply. Hermione reviews his fix and his replies as usual, with two rounds of her own for each follow-up, apart from the task's three.
   - On PASS: every reply is checked again (one line of plain ASCII, no em dash, no fleet word, nothing shaped like a credential, no mention, no link outside the repo, no markup such as backticks, asterisks, tildes or underscores around a word), the PR is read again, and the reviewed commit is pushed to the same branch, never forced and never as a new PR, when it is new. Then each reply goes out once with your `gh` login: in its review thread, or as a PR comment quoting the review or comment it answers. The quote is the comment's first line, scrubbed with the whole comment before it is cut, and only when that line is plain text with no link; otherwise the reply links to the comment. Before every reply the PR is read again: if it closed, its head moved, its branch changed or `gh` is signed in as someone else, no more replies go out. You get one headmaster event naming the PR, the commit and how many replies went out in your name. Any stop is one headmaster event too, naming the step and what to do next. Nothing resolves a thread, requests a review, marks the PR ready or merges.
   - After a kill: the push is read back from the remote branch and each reply from the PR, so nothing is pushed or posted twice; what may or may not have happened is told once. A reply that GitHub refused, that came back from another login or that may or may not be on the PR is recorded in the same store transaction that stops the follow-up, and no reply ever goes out after one that did not end posted.

   Switch it on and off from your terminal. It needs draft PRs on and the patrol out of shadow mode, and follows the same file rules as the draft PR switch:

```
echo on > ~/.hogwarts/pr-followup
rm ~/.hogwarts/pr-followup
```

   Replies go out only while `gh` is signed in as the account in `GITHUB_ACCOUNT`, checked before the push, before every reply and on every answer. A reply that names a teammate whose name is also a fleet word is refused, so replies leave names out. Switching off stops routing at once and stops the next push or reply; an open follow-up is still tidied up by the next Map round, even one that cannot read GitHub (a cut-off routing undone, a closed task's follow-up ended). Such a round never routes anything. In shadow mode with the switch on, each round writes `patrol/followup/<stamp>.md`, counting the last seven days of comments as if follow-ups had been live (`FOLLOWUP_SHADOW_WINDOW_SECONDS`); it opens no live period, so going live later routes only comments written after that. `castle followup show <harry-task-id>` lists a follow-up's comments and replies. The store moves to version 11 when the updated office first opens it, so going back to an older office means restoring a backup.

7. For a commit from one of your own Claude sessions, Moody reviews it in a detached worktree. On PASS, that session's `git push` gets through the gate. For a fix round, pass `--task <id>` instead of `--title`:

```
~/.hogwarts/bin/fleet review own --repo-dir <path-to-your-checkout> --title "<what the change does>"
```

8. To rerun the acceptance checks on their own: `~/.hogwarts/bin/fleet verify <task-id>`. Each check runs inside a Codex permission profile with no network and no office, through `codex sandbox`, which runs no model.

Moody's reviews send the diff to OpenAI, so `fleet review own` runs only once Moody is enabled, after your organization approves Codex for its source code.

## (h) Dumbledore applies his own additions (opt-in)

Off by default. With it on, Dumbledore's weeknight job applies the additions in the patch his run wrote that night: each `fact_add` and `memory_note_add` the store takes, one by one. Every `fact_retire`, `fact_edit` and `archive_move` waits for you, and so does an addition the store refused or one that would change memory already there. An operation out of schema never applies. Dumbledore still applies nothing himself: the office script does.

Turn it on from your terminal, and off by deleting the file:

```
echo on > ~/.hogwarts/auto-portrait
rm ~/.hogwarts/auto-portrait
```

It follows the same rules as the draft PR switch in (g): it counts only while it is a plain file in the office that you own and no one else can write, holding exactly `on`. A file anywhere else, a desk folder, his inbox, outbox or scratchpad, an owl, the patch itself or `standing-orders.md`, is ignored, and no flag or store row turns it on. It is read twice each night, before his run and again just before anything applies. Off at either read, nothing applies that night, and you get the usual row saying his patch is ready.

- **Which patch.** Only a patch file that did not exist before his nightly run started is applied. The job holds every run slot he could have, however many `RUN_SLOTS` gives him, from before it looks in his outbox until it has stored the patch, and his process keeps them all even if the job is killed, so no other run of his can write the file in between. A patch any other run of his wrote, such as one McGonagall's request started, is never applied by it, even if the nightly run edits it: that night stops and tells you, and you apply what you accept by hand.
- **What it keeps.** It reads the file once, then keeps the checked additions and the file's sha256 in the store, and applies from that copy, never from the file again. No text that fails the store's scrubber is ever stored.
- **What you hear.** Each night with a patch ends in one headmaster row: what applied and what waits, with the exact `castle portrait apply <date> --sha256 <hash> --only <ids>` command for the rest (or `castle portrait show <date>` when the command does not fit). A night that stops, for a patch that was there before the run, a file that was refused, Ollivander's stop keeping his run from starting, a kill or a store refusal, tells you once why and names `castle portrait show <date>`. A night whose run failed, was refused or called a model blocked here gets only that run's own row; if the store refuses that row, one stop row tells you instead, that night or from the next job. A night with no patch sends none. `castle portrait patches` and `castle portrait show <date>` keep each night's line, even after the file is gone or when it or his outbox cannot be read, and `show` says when the file changed since.
- **Kills.** A kill is finished by the next weeknight job without reading his file again. A night cut off before the patch was stored is closed and told once, naming `castle portrait show <date>`; when his run had already sent its own row (a failed run, a cap, a vendor limit or a blocked model), that row is the only one, since a night is only ever closed in the same write as its row. If you switch auto-portrait off before the same day's rerun, the cut-off night stays open until the rerun's own row is written, and then closes with it. A night cut off after the patch was stored is applied from the stored copy. Nothing applies twice: this switch and `castle portrait apply` share one ledger, and a patch you have started applying by hand is left to you.
- **The brake.** Ollivander's stop file holds it too: with the stop in place, nothing applies, on the first night or on the next.
- **The store.** The store adds its `auto_patches` table, a new schema version, the first time anything opens it after you update the office. Going back to older code then means restoring a Gringotts backup.
