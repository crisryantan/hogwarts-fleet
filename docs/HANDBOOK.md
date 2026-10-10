# Hogwarts fleet handbook

This is the practical guide: which program to open, what to type, and who to ask. It assumes you've never used Claude Code, Codex or herdr from a terminal before. Setting the fleet up is in [ONBOARDING.md](ONBOARDING.md). The design and why it's safe are in [DESIGN.md](DESIGN.md). A [standalone HTML copy](handbook.html) of this handbook opens in any browser.

- **1** place to start every time: McGonagall, in a session opened in `~/hogwarts`.
- **7** desks, each named for its job, like "Snape - Data Analyst".
- **0** things an agent can merge, deploy or send without you.
- **2** words that close a task: "Mischief managed", followed by its id, or by "everything" to close every task that is ready. Or switch on auto-close, and a merged task closes itself once scripts prove it.

## Start here

From McGonagall to the whole fleet. Part 1 works as soon as onboarding stage 4 passes. Part 2 switches on the rest, one stage at a time. Each step says where to do it: the **Terminal** app, or the **Code tab** in the Claude desktop app. You don't need an IDE.

### Part 1: McGonagall, from day one

1. **Terminal: sign the Claude command line in.** A browser window opens. Approve it there, then check. Done when it says you're logged in.

   ```
   claude auth login
   claude auth status
   ```

2. **Code tab: open McGonagall.** Start a new session and pick the `hogwarts` folder in your home folder (no dot). Say yes when it asks whether you trust the folder. Then send: "What's in flight, and is anything waiting on me?" Done when she answers as McGonagall with a short digest. If she doesn't, type `/agents` and pick mcgonagall, or use the Terminal instead: `cd ~/hogwarts && claude --agent mcgonagall`.
3. **Code tab: give her one real ask.** Ask for work that changes code, in your own words. Claude asks before she writes `tasks/<id>/TASK.md`; allow it, read it, fix anything wrong, then reply "go" and tell her Harry isn't switched on yet. She hands you one `castle task create` command. Once Harry is switched on, a build skips steps 4 and 5: you type `go <task-id>` instead, as in [Your first session](#your-first-session), and its review's PASS marks it ready to close. A data question is different. It gets no TASK.md and no command. She hands you a prompt for Snape instead, and you run it as in step 7.
4. **Terminal: register the task.** Paste the command she gave you, then check it's there with `~/.hogwarts/bin/castle task list`.
5. **Both: do the work.** Until Harry is switched on, you do the work in your usual Claude session in that repo. Mark the task started first. McGonagall keeps one active task at a time, so mark the last one ready to close before you start the next:

   ```
   ~/.hogwarts/bin/castle task start <task-id>
   ```

6. **Both: close it.** After you merge, mark it ready to close in the Terminal, then type `Mischief managed <task-id>` to McGonagall. Done when she confirms the task closed. If she says she couldn't confirm it was you typing, close it from the Terminal with `castle token mint` and `castle task close`, as in [Troubleshooting](#troubleshooting).

   ```
   ~/.hogwarts/bin/castle task await-close <task-id>
   ```

7. **Code tab: ask Snape for numbers.** Start a new session in any folder except `hogwarts`, for example Documents. McGonagall's session can't call Snape. Paste: "Use the snape agent to..." If your question has a link in it, start with "Read <link>, then use the snape agent to...", because Snape can't open links. He answers with the query behind every number. Done when he gives you numbers with their queries. Paste his answer back to McGonagall if you want it filed.

### Part 2: the rest of the fleet, one stage at a time

8. **Decide on Codex for your organization's code.** Harry and Moody run on Codex, which sends code to OpenAI. Confirm it's approved before the review loop.
9. **Terminal: switch on the later stages, in order.** Onboarding stage 5 has the steps for each one and a check at the end. Finish one stage before you start the next.
   - **5.1 The review loop.** Apply `a2` from `~/.hogwarts/pending` to wire in the push gate, then enable Hermione. Enable Harry and Moody only once Codex is approved and both `scripts/codex-boundary-test.sh` and `scripts/codex-exec-boundary-test.py` pass on your Mac.
   - **5.2 Patrol in shadow mode.** `sh scripts/patrol-setup.sh` switches on Ron, the Map, Hermione's bot pass and Gringotts. Leave them in shadow mode for three weekdays, as in [The patrol and the backups](#the-patrol-and-the-backups).
   - **5.3 Dumbledore's nightly review.** `sh scripts/portrait-setup.sh` switches it on. He proposes memory changes and you apply them. Once you have read a couple of his patches, you can let his additions apply themselves (see Auto-portrait in [Daily rhythm](#daily-rhythm)).
   - **5.5 and 5.6.** Ollivander and the live view can go on any time after stage 4.
   - **The switches.** Draft PRs, follow-ups, auto-portrait, auto-close and the worktree cleanup each go on with one file in the office, once the stage each needs is on. [What's on after stage 4](#whats-on-after-stage-4) lists them.
10. **Terminal: read each desk's dry run.** Before a desk goes on, read the exact command the fleet would run for it. `--dry-run` prints it as JSON and runs nothing. The 5.2 and 5.3 scripts run it for Ron and Dumbledore before they switch them on. For Hermione, Harry and Moody, run it yourself, with the desk's id at the end:

    ```
    /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$HOME/.hogwarts'); from fleet.run_desk import main; sys.exit(main())" hermione --dry-run
    ```

11. **Terminal: switch desks on, one at a time.** A desk is on while a plain file named `enabled` sits in its office folder. Do one desk, try it, then the next. Remove the file to switch the desk off again.

    ```
    touch ~/.hogwarts/desks/hermione/enabled
    ```

## The tools

Four programs, and only one you need every day. Three of them live in the terminal. The terminal is the Terminal app on your Mac: press Cmd+Space, type Terminal, and press Enter. Spotlight only finds Mac apps, so typing a command-line tool's name there finds nothing. You type those names inside Terminal instead.

| Program | What it is | Open it | You'll use it |
| --- | --- | --- | --- |
| **Claude Code** (every day) | Anthropic's coding agent. It runs McGonagall, Hermione, Ron, Snape and Dumbledore. | The Code tab in the Claude desktop app, or type `claude` in Terminal. | McGonagall's session in `~/hogwarts`, and Snape from any session. |
| **Codex** (behind the scenes) | OpenAI's coding agent. It runs Harry, who builds, and Moody, who reviews Claude-written code. | Type `codex` in Terminal for your own experiments. The fleet runs it for Harry and Moody through a script. | Rarely by hand. McGonagall routes work to Harry. |
| **castle** (when you want to look) | The command line for the fleet's database: tasks, requests between desks, reviews and memory. Only you and the fleet's scripts use it. | Type `~/.hogwarts/bin/castle` in Terminal, followed by a command such as `task list`. | To check what's in flight, or to close a task by hand. |
| **herdr** (optional) | A terminal workspace manager: tabs and split panes, and one space per desk if you want a live view of the fleet. | Type `herdr` in Terminal. It isn't a Mac app, which is why Spotlight can't see it. | Only if you like a multi-pane view. Desks that can run commands never run inside it, so most spaces are read-only feeds. |

## One-time setup

[ONBOARDING.md](ONBOARDING.md) walks through the whole setup with a check at every stage. These are the six steps to have done before the first run. Paste one command at a time into Terminal and press Enter.

1. **Sign the Claude command line in.** The desktop app has its own sign-in, so the terminal version needs one too. Headless desks like Ron and Dumbledore use it. A browser window opens and you approve it there.

   ```
   claude auth login
   ```

   Check: `claude auth status` shows you as logged in.

2. **Confirm Codex is signed in, and approved.** Before Harry or Moody touch your organization's code, confirm Codex is approved for it. Until then, none of that code goes to Codex.

   ```
   codex login status
   ```

3. **Check the fleet's database is healthy.** This reports permissions, the schema version and an integrity check, all as JSON.

   ```
   ~/.hogwarts/bin/castle doctor
   ```

4. **Optional: make `castle` a short command.** This adds one line to your shell settings so you can type `castle` instead of the full path. Open a new Terminal window afterwards.

   ```
   echo "alias castle='$HOME/.hogwarts/bin/castle'" >> ~/.zshrc
   ```

5. **Keep your own Claude sessions out of the office.** Back up your Claude settings first, then add the deny rules from `~/.hogwarts/pending/a1-user-settings-deny.merge.json`. They stop your own sessions, and Snape, from reading the fleet's office or running `castle`. Onboarding stage 3.1 has the merge command. You can also ask Claude to do the merge; approve the edit yourself and keep the backup next to the file.

   ```
   cp -p ~/.claude/settings.json ~/.claude/settings.json.pre-hogwarts-$(date +%Y%m%d-%H%M)
   ```

   Check: `grep -c hogwarts ~/.claude/settings.json` prints a number above zero.

6. **Switch on the Owl Post.** One command runs every step and prints OK or FAILED after each one. It checks the outboxes, runs one pass by hand, installs the background job, and sends a test owl from McGonagall to Hermione. Run it from your clone of the repo.

   ```
   sh ~/hogwarts-fleet/scripts/owlpost-setup.sh
   ```

   Check: the last line says all six steps passed. The sent file is renamed to `owl_<id>-hello.json`, and the owl lands in Hermione's inbox as `owl_<id>.json`.

A go, `fleet worktree` and `fleet review own` work on your checkout wherever it lives. Each one says plainly when the build can't move on by itself. If neither `fleet loops` nor launchd runs the Owl Post and the Map, it tells you to start `fleet loops`. If launchd alone runs them and the repo is in `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive, which macOS keeps launchd jobs out of, it warns you to move the jobs to [a terminal](#where-the-background-jobs-run). Neither one refuses the go.

Every owl another desk sends McGonagall reaches you too: a headmaster event and a macOS notification naming the desk, the task and a one-line status, and a list of her new owls on your next message in her session. Set `DESKTOP_NOTIFY = False` in `~/.hogwarts/fleet/config.py` to turn the notifications off.

The headmaster queue stays quiet. Each session is shown the full list of events once, and only newer ones after that. Events the fleet already settled clear themselves: a go or close refusal followed by its confirmation, and an owl event for an owl McGonagall read. An owl file marked `test: true` never reaches the queue. Clear rows yourself with `castle event ack <id>`, or with `--all`, `--kind <kind>` or `--task <task-id>`.

McGonagall sees where her own work stands without asking you. In her session the hooks show each open go task as go task, build, branch, state and who it waits on, with its newest event, in full once per session and then only what changed. So she never needs you to run `castle task list` for her.

### Where the background jobs run

The Owl Post, the Map, the closer and the other jobs run under launchd by default. They can run from one Terminal window instead, with `fleet loops`: a foreground supervisor that starts each job named in `~/.hogwarts/loops/jobs` on the schedule of its own plist in `~/.hogwarts/launchd/`. Jobs started from your window get that window's folder access, which launchd jobs never get for `~/Documents`. Choose it with `./install.sh --terminal-loops`, or on an existing install with `sh scripts/loops-setup.sh`. [ONBOARDING.md](ONBOARDING.md#terminal-loops) has the steps.

- `~/.hogwarts/bin/fleet loops --dry-run` shows each job's schedule and starts nothing. The loops' own lines go to `~/.hogwarts/logs/loops.log`.
- One `fleet loops` runs at a time. Ctrl+C or closing the window stops it and every job it started. To start it at login, add `~/.hogwarts/bin/fleet-loops.command` as a Login Item.
- A job on an interval, a watch or keep-alive that fails is tried again after a wait that doubles each time, up to ten minutes, and never holds up the other jobs. A job with only calendar times waits for its next slot. A job whose plist launchd also has is skipped, so nothing runs twice.

## Your first session

You don't need to remember which desk does what. Start every piece of work with McGonagall, and she writes it up and sends it to the right desk.

1. **Open her session.** In the Claude desktop app, open the Code tab, start a new session and pick the `hogwarts` folder in your home folder. Or in Terminal, type `cd ~/hogwarts` and then `claude`. The folder's settings make McGonagall the default.
2. **Read her digest.** Her first message lists what's in flight, one line per desk and then the tasks that need you first, the notes waiting on you, and queued work. On a fresh install it's short.
3. **Ask in your own words.** For example: "Add a unit test for the retry path when the cache is empty, in my web-app repo." Small questions she answers directly.
4. **Check her ticket.** For real work she writes `tasks/<id>/TASK.md`: your words under Intent, numbered acceptance criteria with the check for each, a Spec that opens with the lines `repo:`, `branch:` (a new branch) and `base:`, and what's out of scope. Each check is one backtick command, which a script runs, or plain words, which the reviewer judges; a check that mixes the two is malformed and never runs. One labelled `| after merge:` waits until after the merge: the review never holds a PASS back for it, and the closer checks it once auto-close is on. Claude asks you before she writes it. Fix anything that's wrong. For a build, type `go <task-id>` as your whole message, or one per line for several: that registers the task, routes it to Harry, gives him a fresh worktree on the new branch and starts his run if he's switched on. The hook usually confirms your typing a few seconds after you send it, and the result comes as a headmaster event on your next prompt. A go it refuses ends with a `Fix:` line saying what to change before you type it again. For any other desk she hands you one `castle task create` command to run in Terminal instead. A data question skips all of this: she hands you a prompt to run with Snape in another session.
5. **Let it move.** A build runs on its own from here. Harry's handoff starts his review, a CHANGES verdict starts his fix round, and the loop stops at a PASS, a HEADMASTER verdict or the third round. Other work she posts to the right desk through her outbox, and Claude asks you before each post. The Owl Post delivers it. You'll see rows for anything that needs you at the start of your next message to her.
6. **Push, merge and close.** After a PASS, push with `fleet push <task-id>`. Or switch on draft PRs once, with `echo on > ~/.hogwarts/auto-draft-pr`, and the loop pushes the reviewed commit and opens a draft PR for you; `rm ~/.hogwarts/auto-draft-pr` switches it off. Once draft PRs are on, you can also switch on follow-ups with `echo on > ~/.hogwarts/pr-followup` (and off with `rm ~/.hogwarts/pr-followup`): a review comment on a PR the loop opened, from a teammate with write access to the repo, goes back to Harry, who fixes or answers it, and after the other family passes his fix and his replies, the loop pushes to the same PR and posts one reply per thread or comment. Follow-ups need draft PRs on, the patrol out of shadow mode, Harry and Hermione enabled, and `gh` signed in as your `GITHUB_ACCOUNT`, the only account replies ever go out as. Switching off stops routing and the next push or reply at once; an open follow-up is still tidied up by the next Map round, even one that cannot read GitHub. Before every reply the loop reads the PR again, and stops if it closed, its head moved or `gh` is signed in as someone else. A reply that names a teammate whose name is also a fleet word is refused, so replies leave names out. Nothing resolves a thread, requests a review, marks the PR ready or merges. When a PR is green and reviewed, you merge it yourself. Then type `Mischief managed <task-id>` to her. Or switch on auto-close once, with `echo on > ~/.hogwarts/auto-close`, and each Map round starts the closer, which closes a passed task for you after the merge, once scripts prove the reviewed commit landed, CI on the merge commit is green and every after-merge check holds, with the other family judging the written ones; `rm ~/.hogwarts/auto-close` switches it off. It leaves a task alone while its follow-up is still open, and a build you registered by hand stays yours to close until you run `fleet adopt <task-id>` on it. You hear about every close. To close everything that is ready in one go, type `Mischief managed everything` as the whole message. It closes every task that has a PASS and is reviewed, dropped or merged with its after-merge checks proven, and refuses each other open task by name, with the reason and a `Fix:` line saying what to type next. A task in flight is never closed that way. A go task closes once its last build has, while auto-close is on and at least one of its builds was closed by a proven close. A task whose builds were only closed by hand, or with no proof, stays open: close it with `castle task close <id> --reason superseded`. Those phrases, the terminal close in [Troubleshooting](#troubleshooting) for when the hook can't confirm you typed it, and the closer you switched on are the only things that close a task.

Good first prompts:

- "What's in flight, and is anything waiting on me?"
- "Write a TASK.md for: rename the retry constant in my web-app repo and update its tests. Don't route it yet."
- "I need p50, p75, p90 and p95 page load time for yesterday." She writes the Snape prompt for you to run in another session.
- "Draft a reply to the latest message in the release thread. Don't send it."

## Which desk to ask

The name after the dash is the job. When in doubt, ask McGonagall. When you know exactly what you want, you can go straight to a desk.

| You want to | Ask | How to reach them |
| --- | --- | --- |
| Get something done, or you're not sure who should do it | McGonagall - Chief of Staff | A session opened in `~/hogwarts` |
| Build a feature or fix a bug | Harry - Senior Engineer | Through McGonagall: your typed `go <task-id>` in her session, once Codex is approved |
| Review a diff, make an architecture call, or decide whether a bot comment is right | Hermione - Staff Engineer | Through McGonagall. Reviews also start on their own after Harry hands off |
| Get your own Claude-written change reviewed, or a security read | Moody - Security Reviewer | Through McGonagall, once Codex is approved |
| Know where your PRs stand, whether a red build is real, or watch a rollout | Ron - Release Engineer | His morning lineup, or ask McGonagall |
| Pull numbers from the warehouse or observability tools, or read an experiment | Snape - Data Analyst | A new session outside `~/hogwarts`: "Use the snape agent to..." |
| Find out why a desk lacked context, or tidy what the fleet remembers | Dumbledore - Knowledge Manager | His nightly patch, or ask McGonagall |
| See or steer which model each desk runs, or find out why one moved | Ollivander - Model Keeper | He runs by himself every morning. `castle desk models` shows his picks, and his notes arrive with your other rows |

## What's on after stage 4

Where each piece stands once onboarding stage 4 passes, and what switches it on.

| Desk or piece | State | What switches it on |
| --- | --- | --- |
| The store and the castle | On from onboarding stage 1 | Nothing. `castle doctor` confirms it. |
| McGonagall - Chief of Staff | Installed at stage 1, on from stage 4 | Open a session in `~/hogwarts` and trust the folder. |
| Snape - Data Analyst | On in every Claude session from stage 3.1 | Nothing more, once the stage 3.1 deny rules keep your own sessions out of the office. |
| Owl Post - Message Router | On from onboarding stage 3.2 | Nothing more. It wakes whenever a desk writes to its outbox. `scripts/owlpost-setup.sh` switches it on and sends a test owl to Hermione. |
| Hermione - Staff Engineer | Installed, off | `claude auth login` (stage 2), then the review loop (5.1): apply `a2` and enable her. |
| Harry - Senior Engineer and Moody - Security Reviewer | Installed, off | Codex approved for your organization's source code and both boundary scripts passing, then the review loop (5.1). |
| Ron - Release Engineer, the Marauder's Map and Hermione's bot pass | Installed, off. They start in shadow mode, writing files only | `sh scripts/patrol-setup.sh` (patrol in shadow mode, 5.2). Deleting `~/.hogwarts/patrol/shadow` takes them out of shadow mode. |
| Dumbledore - Knowledge Manager | Installed, off | `claude auth login`, then `sh scripts/portrait-setup.sh` (Dumbledore's nightly review, 5.3). |
| Gringotts - Backup | Installed, off | `sh scripts/patrol-setup.sh` (5.2) loads its nightly job and runs a first backup and restore drill. |
| Ollivander - Model Keeper | Installed, off | Load his daily job (5.5). Until then every desk runs the model it was registered with. |
| The live view: `fleet feed` and the herdr spaces | Installed, off | Run `hogwarts-spaces` (5.6). `fleet feed` itself works any time. |
| Busy-day caps and review rounds | On from install | Nothing. They guard every headless run, so they matter once a desk is on. `castle desk caps` shows today's numbers. |
| Draft PRs after a PASS | Installed, off | `echo on > ~/.hogwarts/auto-draft-pr` once the review loop runs (5.1). `rm ~/.hogwarts/auto-draft-pr` switches it off. |
| Teammate PR follow-ups | Installed, off | `echo on > ~/.hogwarts/pr-followup` once draft PRs are on. It routes nothing until the patrol is out of shadow mode (5.2). `rm ~/.hogwarts/pr-followup` switches it off. |
| Auto-portrait: Dumbledore's additions apply themselves | Installed, off | `echo on > ~/.hogwarts/auto-portrait` once his nightly review is on (5.3) and you've read a couple of his patches. `rm ~/.hogwarts/auto-portrait` switches it off. |
| Auto-close | Installed, off | `echo on > ~/.hogwarts/auto-close`. Map rounds start the closer, in shadow mode too, so it needs the patrol loaded (5.2). `rm ~/.hogwarts/auto-close` switches it off. |
| Worktree cleanup | Installed, off | `echo on > ~/.hogwarts/worktree-cleanup`. Map rounds run it, in shadow mode too, so it needs the patrol loaded (5.2). `rm ~/.hogwarts/worktree-cleanup` switches it off. |
| Owl reports: McGonagall's one-line report on each new owl, as a notification | Installed, off | Copy the kit's `office/desks/mcgonagall/owl-report-settings.json` to `~/.hogwarts/desks/mcgonagall/`, then `echo on > ~/.hogwarts/owl-reports`. It needs the Owl Post loaded and `claude` signed in. `rm ~/.hogwarts/owl-reports` switches it off, and the plain notification comes back. `~/.hogwarts/pending/README.md` (d) has the details. |
| Orchestrator: McGonagall picks the next step after an owl or a verdict, as one typed action a script checks | Installed, off | With owl reports' settings file in place, `echo on > ~/.hogwarts/auto-orchestrate`. Her only actions are a fix round, the next review, a data question for Snape (it comes to you), a draft PR after a recorded PASS while `auto-draft-pr` is on, and a one-line note to you. At most 6 wakes per task and 30 a day. `rm ~/.hogwarts/auto-orchestrate` switches it off. |
| Phone pings for loud events (PR opened, a review that needs you or is blocked on tooling, a refused go, a model outage or sign-in failure, her notes and caps) | Installed, on | A macOS notification by default. Your private overlay can set `PHONE_COMMAND` in `fleet/config.py` to an absolute command that reads one JSON payload on stdin and exits 0 once it delivered it; a failure falls back to the notification. |
| Go updates: one line each time one of McGonagall's open go tasks changes where it stands | Installed, off | `echo on > ~/.hogwarts/auto-go-updates`. Each line names the go task, its build, the new state and what you do next, from a fixed table, and goes the way phone pings go. The first pass after you switch it on only records where things stand, and a loud event one of its lines already covers isn't pinged a second time. A line also goes out right after a go is confirmed or refused and right after verify runs for a build round, with how many checks ran and passed. When McGonagall escalates a question to you on a go task, its line says it's waiting on you and to answer her in her session, and that line is the one ping for it. A handoff only says review running once the review loop has taken it. Once a go task reaches PASS, HEADMASTER, the round cap or closed, that line is its last until the go task closes, except that after PASS you still get the draft PR opened line, your cue to merge. Every line is also added to `~/.hogwarts/logs/go-updates.log`, so `tail -F ~/.hogwarts/logs/go-updates.log` shows them live; past 256 KB the log moves to `go-updates.log.1` and a new one starts, which `tail -F` follows. Banners go away after a few seconds, so set the notification style for Script Editor to Persistent in System Settings, Notifications, to keep them on screen. `rm ~/.hogwarts/auto-go-updates` switches it off. |
| **Terminal loops**: the background jobs run from a Terminal window | Installed, off. launchd runs them by default | `./install.sh --terminal-loops`, or `sh scripts/loops-setup.sh` on an existing install, then keep `fleet loops` running. `rm ~/.hogwarts/loops/jobs` and loading the jobs again goes back to launchd. |
| **Model failover**: a desk runs on the next model of its family while its own is down | Installed, on | Nothing. Crossing to the other family is the one opt-in: `echo on > ~/.hogwarts/cross-family-failover`, and only for a desk whose runs no review reads. `rm ~/.hogwarts/cross-family-failover` switches it off. |

A desk switches on when you create its `enabled` file after reading its dry run, one desk at a time. Draft PRs, follow-ups, auto-portrait, auto-close, the worktree cleanup, owl reports, the orchestrator and cross-family failover are each on only while their file in the office is a plain file you own that no one else can write, holding exactly `on`. Nothing switches itself on.

## Daily rhythm

- **Morning.** Open McGonagall's session. Her digest, and Ron's lineup once he's on, tell you what's in flight and what needs you.
- **Starting work.** Ask her in your own words. Check the TASK.md she writes, then type `go <task-id>` for a build, or run the command she gives you for anything else.
- **Quick data questions.** In a new session outside `~/hogwarts`: "Use the snape agent to..." When the question has a link, start with "Read <link>, then". He answers with the query behind every number.
- **When a session gets long.** If you see the Tempus warning (past about 200k tokens), ask for a Checkpoint and start a fresh session. Long sessions are one of the biggest costs.
- **Reviews.** They happen before anything is pushed, by the other model family, and Harry's handoffs start them on their own. You'll see the verdict in your rows.
- **Model notes.** Ollivander's notes arrive with your other rows. A move to a cheaper or equal model has already happened. A costlier one waits for `castle desk model <desk> --approve`.
- **Dumbledore's patch.** With auto-portrait off, after his weeknight review a row says his patch is ready, and it changes nothing until you apply it. `castle portrait show <date>` lists each operation with its reason, its source and whether the store would take it, then prints the exact apply command with the patch's hash. Run it as printed, or with `--only` naming just the operations you accept. Archive moves are yours to make by hand.
- **Auto-portrait.** Switch it on with `echo on > ~/.hogwarts/auto-portrait` and off with `rm ~/.hogwarts/auto-portrait`. While it is on, his additions (new facts and memory notes) apply the night he writes them, each only if the store takes it, and that night's one row says what applied and what waits, with the exact command for the rest (or `castle portrait show <date>` when the command does not fit in the row). Anything that retires, edits or moves memory still waits for you, and so does an addition the store refused. A night that stops tells you once why and names `castle portrait show <date>`. If a patch for the date was already in his outbox before the night's review, or his run called a model that is blocked here, nothing applies that night and you are told. Ollivander's stop file holds it too. A night with no patch sends no row. If the nightly job is killed, the next weeknight job finishes that night without reading his file again. Killed before it stored his patch, the night is closed and one row says it was cut off and names `castle portrait show <date>`, so you apply by hand; when his run had already sent its own row (a failed run, a cap, a vendor limit or a blocked model), that row is the only one. Killed after it stored the patch, the stored additions apply then, and because auto-portrait and `castle portrait apply` share one ledger, nothing applies twice; a patch you have started applying by hand in between is left to you, and the row says so. A night killed on a Friday is finished on Monday. `castle portrait patches` and `castle portrait show <date>` keep each night's line, even when the file is gone or cannot be read.
- **Cap warnings.** A desk at 80% of a daily cap sends one note. At the cap its next run waits for the reset or a bump, as in [Busy days and caps](#busy-days-and-caps).
- **Wrapping up.** Merge what's ready yourself, then type `Mischief managed <task-id>` for each finished task, or `Mischief managed everything` to close every task that is ready at once. With auto-close on, a merged task closes itself on the next Map round once its proof is in, and a row tells you; one it can't prove waits for you, with a row saying why. A build the closer closes loses its worktree in the same pass when nothing in it is uncommitted, it holds no git-ignored files apart from the dependency links the fleet made, and its HEAD is the commit the close proved; the close row says it is being removed, or names one it keeps.
- **Worktrees.** A closed task's worktree stays until you run `fleet worktree-remove <task-id>`, which keeps the branch, refuses a worktree with uncommitted changes and, like `git worktree remove`, deletes ignored files. Or switch on the worktree cleanup with `echo on > ~/.hogwarts/worktree-cleanup`, and off with `rm ~/.hogwarts/worktree-cleanup`. While it's on, each Map round removes the worktree of every build task closed at least three days ago, by Mischief managed, auto-close or any other close, once it has no uncommitted changes, no git-ignored files apart from the dependency links the fleet made, and its HEAD is on the base or its own branch on origin after a fetch. A worktree it can't read, can't fetch for, or finds in use stays, and one it keeps sends you one row. Its removals come as one routine row per round. It runs only `git worktree remove`, never deletes a branch and never prunes.

## Which model each desk runs

You don't pick models by name. Each desk has a role card at `~/.hogwarts/desks/<desk>/role.json` that says what its job needs: a frontier, workhorse or fast model, an effort level and one line of why. Ollivander - Model Keeper reads the cards every morning and picks the model. A desk never changes family, so Claude desks stay on Claude and Codex desks stay on Codex. The cross-family review rule depends on that.

| Desk | Needs | Effort | Why |
| --- | --- | --- | --- |
| McGonagall - Chief of Staff | frontier | high | Scope and spec judgment, used rarely |
| Hermione - Staff Engineer | frontier | high | Deep reviews of Codex work |
| Moody - Security Reviewer | frontier | high | Security review of Claude work |
| Dumbledore - Knowledge Manager | frontier | medium | One nightly memory review |
| Snape - Data Analyst | workhorse | high | Accurate SQL at moderate cost |
| Harry - Senior Engineer | workhorse | high | Everyday coding |
| Ron - Release Engineer | fast | low | Sorts lots of PR and CI updates |

### How he picks

- Claude desks take the alias for their tier: `opus` for frontier, `sonnet` for workhorse and `haiku` for fast. An alias always means the newest model of that line that your Claude Code knows, so a new release needs no edit.
- Codex desks take a model from Codex's own catalog. He files each one by the wording of its description, then takes the top-ranked visible model of the tier. He skips anything the catalog calls older, legacy or previous generation, and anything that retires within 30 days.
- The effort is the card's, lowered to the nearest level that model lists.
- A name that fits no tier, or more than one, is never picked. You get one note asking you to file it with `castle model line <name> <line>`, where the line is `frontier`, `workhorse`, `fast` or `ignore`. Your filing beats his guess.

### What happens after a pick

- A headless desk's first pick applies by itself. That covers Hermione, Ron, Dumbledore, Harry and Moody.
- After that, a move to the same tier or a cheaper one (fast, then workhorse, then frontier) applies by itself, with a note.
- A costlier move waits as a pending pick. To say yes, run `castle desk model <desk> --approve`. If a later pass no longer makes that pick, for any reason, it's dropped and you get a quiet note. `--approve` also checks the pick against the latest catalog first, and refuses one that's gone, hidden, filed under another tier or retiring within 30 days.
- McGonagall and Snape take their model from the `model:` line in their agent files. Ollivander never edits those files. For them he only sends a note with the one line to change, even for a first pick.

### Look and steer

```
castle desk models                  each desk's tier, model, pin and pending pick
fleet ollivander --dry-run          the whole plan as JSON, changing nothing
castle desk model <desk> <model>    pin a desk to one model
castle desk model <desk> --role     unpin, so the role picks again
castle desk model <desk> --approve  take a pending costlier pick
castle model line <name> <line>     file a model name under a tier
```

`fleet` is `~/.hogwarts/bin/fleet`, the same kind of command as `castle`. A pin takes an alias or a full `claude-` model id for a Claude desk, and a slug from the last Codex catalog for a Codex desk. Ollivander leaves a pinned desk alone.

### Safety nets

- **The two-run trial.** The first two runs after a switch are a trial. If both fail, the desk goes back to its previous model, pinned, and you get a note. Unpin it with `--role` once you've looked. A run that Claude's or Codex's own usage limit stopped never counts. A switch you made yourself, by pin or approval, is never reverted. You hear about the failures and your choice stands. Pinning the model a desk is on during its trial ends the trial, so two failures after that never move it. Because a revert pins, it never lands on a model you filed as ignore, or one the latest catalog no longer lists, hides or retires within 30 days. The desk stays where it is, unpinned, and you get a note.
- **The blocklist.** If your organization forbids some models, list them in `BLOCKED_MODEL_PREFIXES` in `~/.hogwarts/fleet/config.py`. It is empty in the kit. Each entry is a lowercase prefix, matched against Claude aliases, full Claude ids and Codex slugs. To forbid a whole Claude line, list its alias and its id prefix, for example `("<alias>", "claude-<alias>-")`. A blocked model is never picked, pinned, filed or launched, and neither is an alias that has ever run as one, however many switches or later runs came between. If every model of a tier is blocked, the desk keeps the one it has and you get one note.
- **Codex desks and the blocklist.** While anything is blocked, a Codex desk with no model of its own isn't launched either, because the Codex CLI default can't be checked against the list. Ollivander's first pass gives each unpinned Codex desk its pick, so run `~/.hogwarts/bin/fleet ollivander` once after you fill the list, or pin a model. A desk a failed trial pinned to no model is one he leaves alone, so pin it a model or hand it back with `--role`. The refusal and his daily note both say so.
- **The stop file.** When a CLI update goes wrong, Ollivander writes `~/.hogwarts/state/ollivander-stop`, and no headless desk launches, and no check command starts, before the merge or after it, until you've looked and run `castle ollivander clear`. You hear once per stop, as a loud event naming the failed check and that command, and every desk session shows the stop at the top of its events until it's cleared. A review that meets the stop part way keeps the evidence of the checks that ran, opens no round and says why. A desk run the stop refused, such as Harry's first run after a go, is held, and the Owl Post's first pass after you clear the stop starts it again, once, and says so.
- **CLI updates.** They are off unless you make the plain file `~/.hogwarts/desks/ollivander/update-clis`. With it, each pass runs `claude update` and `brew upgrade --cask codex`, then checks both versions and every enabled desk's dry run. Any failure stops the desks. So does a new Codex version, because the Codex boundary is proven per version: rerun `scripts/codex-boundary-test.sh` first. A new Claude Code version is only a note. An update never starts while a desk is between its last check and the end of its run. If a pass dies part way through an update, the next one stops the desks.
- **The daily job.** `com.hogwarts.ollivander` runs at 06:00 every day. Onboarding stage 5.5 loads it.

### When a model is down

- **A run that fails.** A failure counts only from the CLI's own error output, never from anything a model wrote. A rate limit, an overload or an outage error counts toward the model's breaker. Sign-in and billing errors never fail over, since another model of the same account would fail the same way: you get one note to fix the sign-in. A run an outage cut off is started again from its checkpoint, twice at most.
- **A model goes down.** Two outage-class failures in a row mark a model down for 15 minutes. You get one note when it goes down and one when it is back. After the 15 minutes the next launch that wants it runs as the one probe, and a clean run closes the breaker.
- **The next model.** A desk whose model is down runs on the next model Ollivander would pick for its role, in its own family and never dearer than the model it is approved on now. Review rounds stay cross-family: a round whose author used the reviewer's own family is refused.
- **The whole family down.** The desk waits: no run starts, its owl stays in its inbox and you hear once. The Owl Post starts the run again once a model of that family is up, for up to four hours and at most three times. An automatic review whose reviewer family is down keeps its handoff pending and starts again the same way.
- **Crossing families.** Only while `~/.hogwarts/cross-family-failover` holds `on` may a desk whose own family is all down run on the other one, and only a desk whose runs no review reads. Builders and reviewers never cross.

## Busy days and caps

Every headless desk has a daily cap on runs, and the Claude desks have a cap on spend too. The caps are runaway guards, not targets. They're sized on the assumption that a busy day means a dozen or so PRs plus side work, so a normal day shouldn't reach them.

| Desk | Runs a day | Spend a day |
| --- | --- | --- |
| Hermione - Staff Engineer | 80 | $60 |
| Ron - Release Engineer | 120 | $10 |
| Dumbledore - Knowledge Manager | 3 | $4 |
| Harry - Senior Engineer | 40 | none |
| Moody - Security Reviewer | 80 | none |

The day resets at local midnight on your Mac, daylight saving included. A run counts the moment it starts, so one that gets killed or crashes still counts. Spend comes from the cost each run records. A Claude run killed before it reports its cost is charged its per-run budget, marked in the store as an estimate, so the spend cap can run high when a cost is lost, never low.

- **The warning.** A desk that reaches 80% of a cap sends one note for that cap that day.
- **The cap event.** At the cap, the desk's next run doesn't start and its request keeps waiting. You get one note that names the cap, how much was used, how many requests are waiting, when it resets and the command that lifts it.
- **Look and lift.** `castle desk caps` shows today's numbers for every headless desk. McGonagall, Snape and the scripts have no cap, so they don't appear. `castle desk cap <desk> --runs +N` or `--spend +X` raises one cap until the next reset, then it falls back. A bump is at most +500 runs or +$500. `--spend` works only for Hermione, Ron and Dumbledore, because Harry and Moody have no spend cap.
- **Reviews never wait.** One review of a task runs at a time. Start a second while the first is going and it stops at once with "a review of this task, or a run of its build desk on it, is going; run this again when it ends", without touching anything. Harry's run on a task holds the same lock until it ends, so a review you start meanwhile stops the same way, and the automatic one waits for his run to end. If the reviewer is busy, meaning other runs hold every one of its run slots, or at its cap, the review request is queued and the command says so. A reviewer's other tasks never make it busy. Run `fleet review <task-id>` again later, after the reset or a bump if it was the cap. A review the Owl Post started on Harry's handoff tries again by itself on each pass while the reviewer is busy, for up to four hours. For a task from your own Claude sessions, run `fleet review own --repo-dir <checkout> --task <task-id>` instead. The new review replaces that task's queued one, never another task's, so only the newest commit of a task gets reviewed.
- **Three rounds per task.** A task gets three review rounds, and only a round where the reviewer recorded a verdict counts. A crash, a timeout, a cap refusal or a vendor limit doesn't use one up. After the third, `castle task allow-round <task-id>` allows exactly one more, and a queued round doesn't use it up. `castle task rounds <task-id>` lists every round and whether it counts.
- **Follow-ups have their own cap.** Each follow-up on a PR gets two review rounds of its own, apart from the task's three, and a task takes at most five follow-ups. `castle task allow-round <task-id>` lifts the cap of whatever is open when you run it: the open follow-up's, or the build's when none is. It says which.
- **A review that dies.** If a review gets killed partway, its reviewer keeps going until it finishes. Until then that task can't be reviewed again, and the reviewer has one run slot fewer. After that the round doesn't count, and the next review that gets that slot cleans up after it. Until then, and once any review ends without a verdict, `castle task board` and the digest show the task as review died, so run its review again.
- **Tooling is not a verdict.** The review script hands each reviewer the round's log, stat and full diff as a file next to TASK.md, so it needs no git of its own. A review whose tooling fails (a reviewer run that exits non-zero, output with no verdict, or a reviewer that says `BLOCKED-ON-TOOLING: <what failed>`) records no verdict and doesn't use up a round. The loop tries it again on the next pass, three tries in all, then you get one BLOCKED-ON-TOOLING row with the reason, never a HEADMASTER decision. Each round's request also names the TASK.md digest it was opened for and says when TASK.md changed since the round before.
- **A review that waits out an outage.** If every model of the reviewer's family is down, an automatic review doesn't fail. Its handoff stays pending and the Owl Post starts it again once a model is back, at most three times and for up to four hours.
- **Which limit hit.** The note says whether it was the fleet's cap or the vendor's own limit: `cap_source fleet`, or `claude_plan` or `codex_plan` when your Claude or Codex plan's own usage or rate limit stopped the run. A bump can't lift a plan limit. It clears on the vendor's own reset.

### Reviews of your own work

Moody reviews work from your own Claude sessions. Those tasks sit under the desk id `ryan-claude-1`, for example on `castle task board`. Start one with `fleet review own --repo-dir <checkout> --title "<what this change does>"`, and send each fix round on the same task with `--task <task-id>`. The fleet works out which task a commit belongs to from your branch and its commits.

- **A finished review holds nothing back.** A review that ended in PASS or HEADMASTER never stops you starting the next one on the same checkout.
- **Check out a branch first.** A review follows the branch your checkout has out and the commits it has reviewed. A detached HEAD is refused.
- **Branch names.** Any name git takes for a branch works, capitals, `@` and dots included, up to 255 characters of plain ASCII with no spaces. The lowercase rule is only for branches the fleet makes and pushes.
- **Fixes go on their open task.** A fix commit needs `--task`. Leaving it out is refused and never starts a fresh count. Past three rounds, that task needs an allow-round or a close.
- **Work built on a task's commits.** The same holds for any commit built on a commit that an open task of yours has recorded, in the same repo. An origin URL spelled in other letter case is still that repo. That covers a branch made off a capped one (even with the old branch kept), a renamed branch, and a branch in a second clone. `--task` then carries that task onto the branch you have out, and the refusal names the task. From a second clone, it tells you to bring the commit back to the checkout the task was opened on.
- **New work is a new task.** Work that doesn't build on any open task's commits gets its own title and its own three rounds. That's how one checkout carries several PRs in flight. A branch stacked on an open task's commits goes on that task, or waits until it passes. Once a task is awaiting close it blocks nothing.
- **Renamed or deleted branches.** If you rename or delete a branch while its task is open, new reviews on that checkout are refused until you go on with that task using `--task`, or close it. `--task` on some other task never takes on work built on an open task's commits.
- **Shallow clones.** A shallow clone can't always tell whether you built on a task's commits, so it's refused until you run `git fetch --unshallow`. So is a clone cut short whose shallow file was deleted. A full clone that simply doesn't have a task's commits is fine.
- **What the fleet reads.** It reads the parents your commits really name, the ones a push sends, so replace refs and grafts don't hide a task's commits. The checkout is the folder itself, so the same checkout typed in other letter case is still that checkout.
- **Rewritten history.** The fleet can't follow it. A rebase, squash or cherry-pick makes new commits, so moving the work that way onto a new branch name starts a new task at round one. The fleet trusts you not to route around the cap like that. Keep a PR's fixes building on the commits its review started with, and close a task rather than work past its cap.

## The patrol and the backups

Plain scripts patrol your PRs, and Ron and Hermione only judge what the scripts found. All of it reads GitHub and never writes to it, and nothing is posted to a PR or a chat, apart from the follow-up pushes and replies you switch on.

| Job | When | What it writes |
| --- | --- | --- |
| Marauder's Map - PR Watcher | Every 15 minutes, weekdays 08:00 to 19:00 | `patrol/map/`: a snapshot of your open PRs and the reviews asked of you by name (not your teams' requests), one row per change marked routine or for-me, and one row per round. A round with a for-me row wakes Ron. Any other round runs no model. |
| Ron's morning lineup | Weekdays 08:30, or the next Map round if the Mac was asleep or offline then | `patrol/lineup/<date>.md`: your PRs with checks, approvals, unresolved threads and age (stale ones grouped at the bottom), the reviews people asked of you (bot and stale requests counted at the bottom), overnight reds and Dumbledore's note, then Ron's words. |
| Ron's keeper's watch | 09:00, 13:00 and 17:00 on weekdays | `patrol/keeper/`: no model while everything is green. A new red gets Ron's REAL, FLAKY, INFRA or UNSURE call and a fix brief for a real failure. A gate waiting on a person is a row for you. |
| Ron's weekly scoreboard | Mondays 09:00 | `patrol/scoreboard/<date>.md`: PRs merged, time to first review, review rounds, red rate on main, cost per desk and the share of Map rounds that ran no model, all from a script. Ron writes only the words. |
| Hermione's bot pass | A Map round, once a PR is 15 minutes old and has review threads she hasn't seen | `patrol/bot-pass/`: a triage table and a reply draft per thread. Drafts only. Threads a PR follow-up took are left out, even after you switch follow-ups off. Anything in a comment shaped like a credential is masked before it's cut, and so is a full commit sha, so each comment names its commit from GitHub's own field instead. |
| PR follow-ups | A Map round, once a teammate's comment is five minutes old | The threads file next to TASK.md, Harry's follow-up run, then the usual review; on PASS a push to the same PR and one reply per thread or comment. In shadow mode with the switch on, only `patrol/followup/`: what it would send Harry, counting the last seven days of comments as if it had been live. |
| Auto-close | A Map round, while `~/.hogwarts/auto-close` holds `on`, in shadow mode too | `logs/closer.log`: one line per task each pass. Next to TASK.md, the after-merge evidence and the judge's verdict. One row for you for each close or stop. |
| Worktree cleanup | A Map round, while `~/.hogwarts/worktree-cleanup` holds `on`, in shadow mode too | `worktrees/<task>.removed` next to each office worktree record, and `.removing` while a removal is under way, so a killed round's removal is finished or reported on the next one. One routine row per round that removed anything, and one row for each worktree it keeps. The round row in `patrol/map/rounds.jsonl` counts what it did. |
| Gringotts - Backup | Daily 23:30 | `backups/gringotts-<date>.tar.gz`, mode 0600, 14 days kept and never synced anywhere: your Claude, Codex and fleet setup with credentials, tokens, auth files and git history left out, and every `env`, headers or secret-named value in settings, MCP and Codex config blanked. |

The folders are in the office, `~/.hogwarts`. Read them in the Terminal, since your own Claude sessions are denied the office.

- **Shadow mode.** While `~/.hogwarts/patrol/shadow` is there, which it is from install, those files are all the patrol writes: no rows in McGonagall's digest and no owls to anyone. That includes the cap and vendor-limit notes from Ron's and Hermione's patrol runs, which land in the job's file instead. Notes about a desk's model (blocked, changed, or a failed trial) still reach your digest. Auto-close is the one exception: once you switch it on, it closes tasks and tells you whatever shadow mode says. Compare each morning's lineup with `gh`. After three weekdays that match, delete the file to take the patrol out of shadow mode. Then each for-me row, each gate, each keeper's watch Ron marked headmaster and each day's lineup and scoreboard also reach you as a row, and follow-ups you switched on start routing comments written from then on. Put the file back to return to shadow mode.
- **A desk that didn't run.** If Ron or Hermione didn't pick a run up, because the desk was off, at its cap or busy, or a run left no file the patrol could take, a Map round at least half an hour later sends it again, twice at most, then gives you a row. A keeper's red counts as called, and a bot pass's threads as seen, only once the patrol has taken the file.
- **The restore drill.** `fleet gringotts --drill` restores the newest archive into a fresh folder inside `~/.hogwarts/backups`, checks every file against the archive's manifest, that nothing credential-shaped and no git metadata is inside, that no secret-named value is left in a settings, MCP or Codex config file and that the database copy is sound, then removes the folder. It never touches your live folders. `fleet gringotts` takes a backup now.

## Watch live, run short

Desks run short. A headless desk takes one owl, does the job and exits, so there's no long session to sit inside. You can still watch every desk work, without typing into anything.

- **Run slots.** Most desks run one at a time. Hermione and Moody have two run slots, so two reviews of different tasks run at once and only a third is queued (`RUN_SLOTS` in [CUSTOMISE.md](CUSTOMISE.md#change-budgets-and-limits)).
- **Many tasks.** A desk can still have many tasks in flight. Harry, Hermione, Moody, Ron and your own sessions keep each task open until its review passes, and a task waiting for fixes blocks nothing.
- **The board.** `castle task board` shows every open task by desk: its round, its verdict and whether a run is going. Filter it to a reviewer such as Moody and it also lists the tasks that reviewer is reviewing or has queued, under their own desks, so a reviewer mid-review never shows an empty board.
- **Pads.** Hermione and Ron keep one pad per task in `~/hogwarts/desks/<desk>/pads/`. Every review round of a task shares that task's pad, so two tasks never mix their notes.

### Feeds

- `fleet feed --desk <name>` follows one desk. Use the desk's registry name. Dumbledore's is `portrait`, and a name that matches no desk just shows nothing. `fleet feed --desk owl-post` follows every owl.
- `fleet feed --all` follows every desk at once.
- A feed prints a line whenever something happens: owls to and from the desk (kind and subject, never the body), the start and end of each run with its model, time, tokens and cost, the desk's notes to you, and, while a run is going, what the desk says and which tools it calls.
- It is read-only. It opens the store read-only, only reads files, and strips every control sequence from what a desk wrote, so a desk can't steer your terminal through it. Ctrl+C stops it.

### One space per desk

`~/.hogwarts/bin/hogwarts-spaces` opens a herdr space for each desk, named like "Hermione - Staff Engineer". McGonagall and Snape get live sessions, because neither has a shell tool. Hers opens in `~/hogwarts`, and his opens in `~/Documents`, outside the castle. Every other space, including the Owl Post and Ollivander, runs a feed. Any herdr pane can type into any other pane, and a desk that can run commands must never sit where it could type into the rest.

Before it opens either live session, it reads that agent's definition: Snape's `~/.claude/agents/snape.md`, McGonagall's in `~/hogwarts/.claude/agents/`, plus any other file that defines the same agent. It refuses the space with FAILED unless the definition has a `tools:` line and every tool on it, built-in or MCP, is on that agent's trusted list in `~/.hogwarts/desks/<agent>/live-tools.json`. Names must match exactly, so a new tool stays out until you add it there, even one from a server that's already on the list. [CUSTOMISE.md](CUSTOMISE.md#trust-a-tool-in-a-live-space) shows how. A file anywhere in those folders whose name it can't read plainly, or whose frontmatter doesn't read cleanly, gets checked too.

Each live session runs the `claude` that `CLAUDE_BIN` in `~/.hogwarts/fleet/config.py` names. It starts with a fixed list of built-in tools, every command-running tool denied and skills turned off, so an edit made after the check still gets no shell. That also means Snape can't load a skill in his live space. He can still read a skill's file.

```
~/.hogwarts/bin/hogwarts-spaces --dry-run
~/.hogwarts/bin/hogwarts-spaces
```

- `--dry-run` lists what it would open and changes nothing.
- `--only "<label>"` handles just the space with exactly that name.
- `--herdr <path>` points it at herdr if that isn't at `~/.local/bin/herdr`.
- A space whose name already exists is left alone, so running it twice is safe. It prints OK, SKIP or FAILED for each space.

### herdr in five minutes

herdr keeps several terminal sessions in one window and keeps them running after you close it. It's handy for the spaces above, and for watching a couple of your own `claude` or `codex` sessions side by side.

1. **Start it.** Open Terminal and type `herdr`. The first launch walks you through a short setup.
2. **Use the prefix.** Every shortcut starts with Ctrl+B. Press it, let go, then press the next key.
3. **Make room.** Ctrl+B then c opens a tab. Ctrl+B then v splits side by side, and Ctrl+B then minus splits top and bottom.
4. **Run something.** In a pane, type `cd ~/hogwarts` and `claude`, or `codex` in a project folder.
5. **Leave it running.** Ctrl+B then q detaches. Your sessions keep going. Type `herdr` again to come back.

| Press Ctrl+B, then | Does |
| --- | --- |
| `?` | Shows every shortcut |
| `c` | New tab |
| `n` / `p` | Next or previous tab |
| `v` / `-` | Split side by side, or top and bottom |
| `h` `j` `k` `l` | Move between panes |
| `z` | Zoom the current pane |
| `x` | Close the current pane |
| `w` | Switch workspace |
| `q` | Detach and leave everything running |

## Cheat sheet

Everything `castle` prints is JSON. The installer links `castle` and `fleet` into `~/.local/bin` when that folder exists, or prints the one line to add to your shell profile; either way you can type `castle` instead of the full path.

**castle**

| Type | To |
| --- | --- |
| `castle task list` | See what is open, one line per task, newest first: task id, desk, title, state and who it waits on |
| `castle task list --desk harry` | The same, for one desk |
| `castle task list --all` | See every task and its status, as full records. List commands show the newest rows by default and say "showing N of M" when they cut; `--all` shows everything |
| `castle task list --open` | See every task that is queued, active or awaiting close, as full records |
| `castle task builds` | See one line per open build: go task, build task, branch and state (`--all` adds closed ones) |
| `castle task board` | See each desk's tasks in flight: round, verdict, and whether a run is going |
| `castle event drain` | See what needs you |
| `castle event ack <id>` | Clear a row once you've dealt with it |
| `castle event ack --all` | Clear every row. `--kind <kind>` clears every row of one kind, `--task <task-id>` every row of one task |
| `castle request list --open` | See requests between desks still in flight |
| `castle audit` | Find stuck requests and unanswered owls |
| `castle fact current` | See what the fleet currently believes |
| `castle portrait patches` | See Dumbledore's dated patches, which operations you applied, and what auto-portrait applied and what waits on each night, even after a file is gone |
| `castle portrait show <date>` | Read one patch, and get the command that applies it. It also says what auto-portrait applied and whether the file changed since |
| `castle portrait apply <date> --sha256 <hash> [--only <ids>]` | Apply the operations you accept from that patch |
| `castle desk list` | See every desk, its job and whether it takes many tasks |
| `castle desk caps` | See today's runs and spend against each desk's caps |
| `castle desk cap <desk> --runs +N` | Lift a desk's run cap until the next reset |
| `castle desk cap <desk> --spend +X` | Lift a desk's spend cap until the next reset |
| `castle task rounds <task-id>` | See a task's review rounds and which count |
| `castle task allow-round <task-id>` | Allow one more review round, for the open follow-up if there is one |
| `castle followup list` | See every PR follow-up and its state |
| `castle followup show <task-id>` | See a task's PR, each follow-up's comments and the replies, posted or not |
| `castle desk models` | See each desk's tier, model, pin and pending pick |
| `castle desk model <desk> --approve` | Approve a costlier pick that's waiting |
| `castle desk model <desk> <model>` | Pin a desk to a model |
| `castle desk model <desk> --role` | Unpin a desk |
| `castle model line <name> <line>` | File a model name as frontier, workhorse, fast or ignore |
| `castle ollivander clear` | Clear Ollivander's stop file so desks launch again |
| `castle doctor` | Health check |

**Typed in McGonagall's session** (each is the whole message, and only your own typing counts)

| Type | To |
| --- | --- |
| `go <task-id>` | Register a build and start Harry. It is the whole message, one per line for several |
| `Mischief managed <task-id>` | Close one finished task |
| `Mischief managed everything` | Close every task that is ready, and see each other open task refused with a reason and a `Fix:` line |

**Claude Code**

| Type | To |
| --- | --- |
| `claude` | Start a session in the current folder |
| `claude --agent snape` | Start a session as one desk |
| `/agents` | List the agents a session can use |
| `/context` | See what's using your context |
| `/model` | Switch model for this session |
| `/mcp` | See your MCP servers and their tools |
| `claude auth status` | Check you're signed in |

**Codex**

| Type | To |
| --- | --- |
| `codex` | Start an interactive session |
| `codex exec "..."` | Run one prompt and exit |
| `codex review` | Run a code review non-interactively |
| `codex resume --last` | Continue your last session |
| `codex login status` | Check you're signed in |

**fleet** (the same path style as castle: `~/.hogwarts/bin/fleet`)

| Type | To |
| --- | --- |
| `fleet feed --desk <name>` | Watch one desk, read-only |
| `fleet feed --all` | Watch every desk, read-only |
| `fleet go-wait <go-task-id> [--since <key>]` | Wait, read-only, until one go task changes where it stands, then print one line: the go task, its build, the new state, what you do next and the key for the next wait. With no `--since` it prints where it stands now. It ends with `closed` once the go task closes, or with `still` after 30 minutes with no change |
| `fleet ollivander --dry-run` | See Ollivander's plan without changing anything |
| `fleet gringotts` | Take a backup now |
| `fleet gringotts --drill` | Restore the newest backup into a temp folder and check it |
| `fleet review <task-id>` | Review a build desk's newest commit by hand. Harry's handoff starts this review by itself |
| `fleet build <task-id>` | Start Harry's run by hand: a fix round the review loop could not start or that follows a review you ran, or a run a go could not start |
| `fleet push <task-id>` | Push a passed build's reviewed commit yourself, after you type its branch name |
| `fleet review own --repo-dir <checkout> --title "<title>"` | Start a review of work from your own Claude sessions |
| `fleet review own --repo-dir <checkout> --task <task-id>` | Review a fix round on a task from your own Claude sessions |
| `fleet close <task-id>` | Try auto-close once more on one task, in the foreground, while auto-close is on |
| `fleet adopt <task-id>` | Let the closer take a build you registered by hand: give McGonagall's task id, check what it shows, and type the id back |
| `fleet loops` | Run the background jobs from this Terminal window, in the foreground. Ctrl+C stops them all |
| `fleet loops --dry-run` | See each job's schedule from its plist and start nothing |
| `fleet worktree-rebuild <task-id>` | Make an open task's worktree one git reads again, when a BLOCKED-ON-TOOLING row says git can't read it. Its files and uncommitted work stay, no repo hook runs, and its dependency links come back |
| `fleet worktree-remove <task-id>` | Remove a closed task's worktree now and keep its branch. It refuses one with uncommitted changes, deletes ignored files as git does, and finishes a removal a kill cut short |
| `~/.hogwarts/bin/hogwarts-spaces` | Open one herdr space per desk |

**Switches** (one file each in the office, off until it holds exactly `on`)

| Type | To |
| --- | --- |
| `echo on > ~/.hogwarts/auto-draft-pr` | Push the reviewed commit and open a draft PR after the review loop's PASS |
| `echo on > ~/.hogwarts/pr-followup` | Send teammates' comments on those PRs back to Harry, once the patrol is out of shadow mode |
| `echo on > ~/.hogwarts/auto-portrait` | Let Dumbledore's nightly additions apply themselves |
| `echo on > ~/.hogwarts/auto-close` | Close a merged task once scripts prove it |
| `echo on > ~/.hogwarts/worktree-cleanup` | Remove the worktree of a build task closed three days ago, once nothing in it can be lost |
| `echo on > ~/.hogwarts/owl-reports` | Get McGonagall's one-line report on each new owl to her as a notification |
| `echo on > ~/.hogwarts/auto-orchestrate` | Let McGonagall pick the next step after an owl or a verdict, as one checked typed action |
| `echo on > ~/.hogwarts/cross-family-failover` | Let a desk whose own family is all down run on the other family, where no review reads its runs |
| `echo on > ~/.hogwarts/auto-go-updates` | Get one line each time one of McGonagall's open go tasks changes where it stands |
| `rm ~/.hogwarts/<file>` | Switch that one off again |

## Rules worth remembering

Six things only you do.

- **Merge and deploy.** No desk can merge, deploy, press a pipeline gate or change prod.
- **Close tasks.** You close one with "Mischief managed <task-id>" in McGonagall's session, or from your terminal with a close token when the hook can't confirm you typed it. The closer closes one only once you switch auto-close on, and only after scripts prove the merge, CI and every after-merge check. Silence and a green build don't.
- **Send things.** Desks draft messages. You send them, except the draft PRs and follow-up replies you switched on.
- **Sign in.** Desks never see a password or token. If something needs a login, it stops and tells you.
- **Change settings.** Security, permissions, hooks and background jobs are yours to apply. The fleet only prepares the change.
- **Never bypass.** Don't start Claude or Codex with any skip-permissions or bypass flag, and don't run a desk that can run commands inside herdr.

## Troubleshooting

| You see | Do this |
| --- | --- |
| Spotlight can't find herdr, castle or codex | They're command-line tools. Open Terminal and type the name there. |
| `owlpost-setup.sh` or `patrol-setup.sh` prints FAILED | It stopped at that step and changed nothing after it. The lines above say why. Fix that, then run it again; steps that already passed are safe to repeat. |
| `command not found: castle` | Use the full path `~/.hogwarts/bin/castle`, or add the shortcut from one-time setup and open a new Terminal window. |
| `install.sh` says a folder already exists | Nothing was changed. The fleet is already installed. Use `./install.sh --force` only if you want a fresh copy; it moves the old folders aside first. |
| A tool name with `<warehouse-mcp>`, `<observability-mcp>` or `<chat-mcp>` in it | A placeholder was never filled. See onboarding stage 2. |
| A headless desk never answers | Run `claude auth status`. Headless desks need the command line signed in. Check the desk has an `enabled` file. Then run `castle audit` to see where the request stopped. If a note says a cap is reached, see [Busy days and caps](#busy-days-and-caps). |
| No headless desk launches, and a note mentions Ollivander | His stop file is in place. Read the note and his log in `~/.hogwarts/logs`, then run `castle ollivander clear`. |
| A go was not applied | The hook, or the headmaster event that follows it, says why. Each go is a line of its own, exactly `go <task-id>`, with nothing else in the message, in McGonagall's session. If TASK.md is the cause (its first line must be `# <task-id> <title>`, and its Spec must open with the `repo:`, `branch:` and `base:` lines), she fixes it and you type the go again. If the hook couldn't confirm you typed it, or the go still can't be applied, run the `castle task create` command the hook or McGonagall gives you, and tell her the task is registered. She routes it to Harry by owl, you give his task its worktree with `fleet worktree` (her task id works too), and `fleet adopt <task-id>` with her task id lets the closer take it, as in `~/.hogwarts/pending/README.md` (g). If a go says Harry's run didn't start, run `fleet build <harry-task-id>`. |
| A review round was refused, or the loop stopped at the round cap | The task has used its three rounds, or its follow-up its two. Run `castle task rounds <task-id>` to look, then `castle task allow-round <task-id>` if one more is worth it. For a build the loop stopped, `fleet build <task-id>` then starts Harry's fix round. |
| A BLOCKED-ON-TOOLING row | The review couldn't gather its evidence; no reviewer judged the change. A worktree git can't read is found before the review starts, and the row gives the command that rebuilds it, `fleet worktree-rebuild <task-id>`. Run it, and the next pass reviews the waiting handoff by itself, for up to four hours; `fleet review <task-id>` runs it now. For any other reason, sort out what the row names, then run `fleet review <task-id>`. For your own task, `fleet review own --repo-dir <checkout> --task <task-id>`, and add `--intent-file <file>` to give that round a new TASK.md; it asks you to type the task id back, so it runs only in your own terminal. |
| The draft PR step stopped after a PASS | Read the event: it says what was and wasn't done. It retries nothing, so push by hand with `fleet push <task-id>`. |
| A follow-up stopped | Read the event: it names the step and why. `castle followup show <task-id>` lists which replies went out and the text of the rest. `fleet push <task-id>` pushes by hand. A task in a follow-up is active again, so `Mischief managed` waits; close it from your terminal if you're done with it, which also ends its follow-up. |
| A desk keeps an old model after a note said it would move | A costlier move waits for you: `castle desk model <desk> --approve`. A pinned desk never moves. `castle desk models` shows both. |
| `hogwarts-spaces` prints FAILED | herdr isn't running or isn't at `~/.local/bin/herdr`. Open herdr, or pass `--herdr <path>`. A space it already made is left alone when you run it again. If the line says refused, that agent's definition has no `tools:` line or lists a tool that isn't on its trusted list in `~/.hogwarts/desks/<agent>/live-tools.json`. Compare it with the repo copy, `claude-agents/snape.md` or `castle/.claude/agents/mcgonagall.md`, and put it right, or trust a read-only tool as [CUSTOMISE.md](CUSTOMISE.md#trust-a-tool-in-a-live-space) shows. If it says the list is not trusted, it's a link or others can write it; put a plain copy back with `chmod 600`. Then run it again. If it says claude is not at a path, fix `CLAUDE_BIN` in `~/.hogwarts/fleet/config.py`. |
| McGonagall doesn't introduce herself | Make sure the session's folder is `~/hogwarts`. Her settings only apply there. |
| The Tempus warning appears | Ask for a Checkpoint, then start a fresh session. She picks up from the digest. |
| "Mischief managed" says it could not confirm | Close the task from your terminal: `castle token mint <task-id>`, then `castle task close <task-id> --reason complete --token-stdin` and paste the token. |
| Auto-close stopped on a task | Read the files its row names next to TASK.md (`after-merge-evidence.md`, `after-merge-review-<sha>.md`). Then close it yourself with `Mischief managed <task-id>`, or fix what stopped it and run `fleet close <task-id>` to try once more. A red, a CHANGES verdict or a merge at another head stops it again. |
| A passed task stays awaiting close after its merge | Check in turn: is `~/.hogwarts/auto-close` there holding `on`; is the Map loaded, and is it a weekday between 08:00 and 19:00; is CI on the merge commit still pending, or inside its 30 minute settle; does the PR's head match the reviewed commit; did the PR go into the base the task names; was TASK.md edited after your go, or the build registered by hand with no go and no `fleet adopt`, either of which leaves it yours to close; is a teammate follow-up of it still open (`castle followup show <task-id>`), which it waits for; is Ollivander's stop in place. `~/.hogwarts/logs/closer.log` has one line per task for each pass. If its line says the judge's run ended with no process left to see how, no other judge run starts: read that run's output in `~/.hogwarts/runs/<judge>/` and close the task yourself. |
| A closed task's worktree is still there | Its row says why: uncommitted changes, ignored files that exist only there (a build output or a `.env`), tracked files marked `assume-unchanged` or `skip-worktree`, a HEAD that is on neither the base nor its branch on origin, a fetch or read that failed, or a folder git doesn't list. Commit and push what you want to keep, then let the next round take it or run `fleet worktree-remove <task-id>`. A row saying a removal was cut short part way means the folder is half gone: look inside, then finish it with `git worktree remove --force <path>` yourself. If no row came, check that `~/.hogwarts/worktree-cleanup` holds `on` and the task closed at least three days ago. |
| Auto-portrait applied nothing one night | Read its row. A row that only says his patch is ready means auto-portrait was off, or Ollivander's stop was in place, when it looked. Any other row says why the night stopped. Either way, `castle portrait show <date>` lists the patch and prints the apply command, so you apply what you accept by hand. |
| A background job never ran, or `fleet loops` skips one | Check that `fleet loops` is running in a Terminal window and read `~/.hogwarts/logs/loops.log`. A job whose plist is still in `~/Library/LaunchAgents` is skipped on purpose, even when it is unloaded. Run `sh scripts/loops-setup.sh`, or unload it and remove the plist from that folder. A job on an interval, a watch or keep-alive that failed waits a growing while before its next try; a calendar-only job waits for its next slot. |
| A note says a model is down | Its desks run on the next model of their family meanwhile. If the whole family is down, a desk's owl waits in its inbox and the Owl Post starts the run once a model is back. A sign-in or billing error never fails over: fix it as the note says, for example with `claude auth login`. |
| `castle` exits with code 3 | The database was busy or a rule refused the change, such as a second active task for McGonagall, who takes one at a time. Read the JSON message. |
| `castle doctor` exits with code 5 | Something about permissions or the database looks unsafe. The JSON says what. Don't loosen permissions to make it pass. |

## Uninstall

Everything the fleet installs lives in two folders, a few background jobs and one agent file. Your own settings only change if you applied the prepared changes yourself, so undoing those is also yours. The uninstaller never touches GitHub or your repos' branches: the branches the fleet made, and whatever the draft PR and follow-up switches pushed or posted for you, stay where they are.

Switch the automations off first. A review or closer pass that is already running is a process of its own, so stopping the background jobs doesn't end it, but with the files gone no draft PR, follow-up push or reply, proven close, worktree removal or memory addition starts after its next check.

```
rm -f ~/.hogwarts/auto-draft-pr ~/.hogwarts/pr-followup ~/.hogwarts/auto-close ~/.hogwarts/worktree-cleanup ~/.hogwarts/auto-portrait ~/.hogwarts/owl-reports ~/.hogwarts/auto-orchestrate ~/.hogwarts/cross-family-failover ~/.hogwarts/auto-go-updates
```

From your clone of the repo, run `./uninstall.sh` to see every step it would take. It changes nothing. Then run `./uninstall.sh --yes` to do it. It stops the background jobs, archives both folders to `~/hogwarts-fleet-archive-<timestamp>.tar.gz` before it removes them, and never edits your Claude or Codex settings. If your settings still mention the fleet, it shows the lines and the backup to restore. [UNINSTALL.md](UNINSTALL.md) has the same steps by hand, and how to restore from the archive.

Commands in this handbook are tested with Claude Code 2.1.274, Codex 0.160.0 and herdr 0.9.3.
