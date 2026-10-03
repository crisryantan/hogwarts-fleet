# Hogwarts fleet handbook

This is the practical guide: which program to open, what to type, and who to ask. It assumes you've never used Claude Code, Codex or herdr from a terminal before. Setting the fleet up is in [ONBOARDING.md](ONBOARDING.md). The design and why it's safe are in [DESIGN.md](DESIGN.md). A [standalone HTML copy](handbook.html) of this handbook opens in any browser.

- **1** place to start every time: McGonagall, in a session opened in `~/hogwarts`.
- **7** desks, each named for its job, like "Snape - Data Analyst".
- **0** things an agent can merge, deploy or send without you.
- **2** words that close a task: "Mischief managed", followed by its id.

## Start here

From McGonagall to the whole fleet. Part 1 works as soon as onboarding stage 4 passes. Part 2 builds the rest with Claude, one stage at a time. Each step says where to do it: the **Terminal** app, or the **Code tab** in the Claude desktop app. You don't need an IDE.

### Part 1: McGonagall, from day one

1. **Terminal: sign the Claude command line in.** A browser window opens. Approve it there, then check. Done when it says you're logged in.

   ```
   claude auth login
   claude auth status
   ```

2. **Code tab: open McGonagall.** Start a new session and pick the `hogwarts` folder in your home folder (no dot). Say yes when it asks whether you trust the folder. Then send: "What's in flight, and is anything waiting on me?" Done when she answers as McGonagall with a short digest. If she doesn't, type `/agents` and pick mcgonagall, or use the Terminal instead: `cd ~/hogwarts && claude --agent mcgonagall`.
3. **Code tab: give her one real ask.** Say what you want in your own words. Claude asks before she writes `tasks/<id>/TASK.md`; allow it, read it, fix anything wrong, then reply "go". She hands you one `castle task create` command.
4. **Terminal: register the task.** Paste the command she gave you, then check it's there with `~/.hogwarts/bin/castle task list`.
5. **Both: do the work.** Until Harry is switched on, you do the work in your usual Claude session in that repo. Mark the task started first:

   ```
   ~/.hogwarts/bin/castle task start <task-id>
   ```

6. **Both: close it.** After you merge, mark it ready to close in the Terminal, then type `Mischief managed <task-id>` to McGonagall. Done when she confirms the task closed. If she says she couldn't confirm it was you typing, close it from the Terminal with `castle token mint` and `castle task close`, as in [Troubleshooting](#troubleshooting).

   ```
   ~/.hogwarts/bin/castle task await-close <task-id>
   ```

7. **Any session: ask Snape for numbers.** "Use the snape agent to..." He answers with the query behind every number.

### Part 2: building the rest, one stage at a time

8. **Decide on Codex for your organization's code.** Harry and Moody run on Codex, which sends code to OpenAI. Confirm it's approved before the review stage.
9. **Terminal: lift the deny rules for the build.** Claude can't edit the fleet's office while the rules are on. Keep a copy of your settings with the rules, then restore the backup you made before adding them. That also drops any other settings change made since that backup, so if you've changed settings since, ask Claude to remove just the eight fleet rules instead.

   ```
   cp -p ~/.claude/settings.json ~/.claude/settings.json.with-hogwarts-deny
   cp -p ~/.claude/settings.json.pre-hogwarts-<timestamp> ~/.claude/settings.json
   ```

10. **Code tab: ask Claude to build the stage.** Start a new session in a folder outside `~/hogwarts`, such as your clone of this repo, and send: "Build the review-loop stage of the Hogwarts fleet from docs/DESIGN.md and docs/ONBOARDING.md stage 5. The deny rules are lifted for this session." Claude builds, tests and reviews it, and shows you anything that touches your settings or background jobs before it changes.
11. **Terminal: put the deny rules back.** Done when `grep -c hogwarts ~/.claude/settings.json` prints a number above zero.

    ```
    cp -p ~/.claude/settings.json.with-hogwarts-deny ~/.claude/settings.json
    ```

12. **Terminal: switch desks on, one at a time.** Claude gives you each desk's dry run to read and the one command that switches it on. Do one desk, try it, then the next.
13. **Repeat** steps 9 to 12 for the shadow stage (Ron, the Map and Gringotts, three days in shadow mode) and the memory stage (Dumbledore's nightly review).

## The tools

Four programs, and only one you need every day. Three of them live in the terminal. The terminal is the Terminal app on your Mac: press Cmd+Space, type Terminal, and press Enter. Spotlight only finds Mac apps, so typing a command-line tool's name there finds nothing. You type those names inside Terminal instead.

| Program | What it is | Open it | You'll use it |
| --- | --- | --- | --- |
| **Claude Code** (every day) | Anthropic's coding agent. It runs McGonagall, Hermione, Ron, Snape and the portrait. | The Code tab in the Claude desktop app, or type `claude` in Terminal. | McGonagall's session in `~/hogwarts`, and Snape from any session. |
| **Codex** (behind the scenes) | OpenAI's coding agent. It runs Harry, who builds, and Moody, who reviews Claude-written code. | Type `codex` in Terminal for your own experiments. The fleet runs it for Harry and Moody through a script. | Rarely by hand. McGonagall routes work to Harry. |
| **castle** (when you want to look) | The command line for the fleet's database: tasks, requests between desks, reviews and memory. Only you and the fleet's scripts use it. | Type `~/.hogwarts/bin/castle` in Terminal, followed by a command such as `task list`. | To check what's in flight, or to close a task by hand. |
| **herdr** (optional) | A terminal workspace manager: tabs and split panes for running several of your own agent sessions side by side. | Type `herdr` in Terminal. It isn't a Mac app, which is why Spotlight can't see it. | Only if you like a multi-pane view of your own sessions. Fleet desks never run inside it. |

## One-time setup

[ONBOARDING.md](ONBOARDING.md) walks through the whole setup with a check at every stage. These are the six steps to have done before the first run. Paste one command at a time into Terminal and press Enter.

1. **Sign the Claude command line in.** The desktop app has its own sign-in, so the terminal version needs one too. Headless desks like Ron and the portrait use it. A browser window opens and you approve it there.

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

## Your first session

You don't need to remember which desk does what. Start every piece of work with McGonagall, and she writes it up and sends it to the right desk.

1. **Open her session.** In the Claude desktop app, open the Code tab, start a new session and pick the `hogwarts` folder in your home folder. Or in Terminal, type `cd ~/hogwarts` and then `claude`. The folder's settings make McGonagall the default.
2. **Read her digest.** Her first message lists what's in flight, anything waiting on you, and queued work. On a fresh install it's short.
3. **Ask in your own words.** For example: "Add a unit test for the retry path when the cache is empty, in my web-app repo." Small questions she answers directly.
4. **Check her ticket.** For real work she writes `tasks/<id>/TASK.md`: your words under Intent, numbered acceptance criteria with the check for each, and what's out of scope. Claude asks you before she writes it. Fix anything that's wrong, then reply "go". She hands you one `castle task create` command to register it. Run that in Terminal.
5. **Let it move.** She posts the work to the right desk through her outbox, and Claude asks you before each post. The Owl Post delivers it. You'll see rows for anything that needs you at the start of your next message to her.
6. **Merge and close.** When a PR is green and reviewed, you merge it yourself. Then type `Mischief managed <task-id>` to her. That exact phrase is the only thing that closes a task.

Good first prompts:

- "What's in flight, and is anything waiting on me?"
- "Write a TASK.md for: rename the retry constant in my web-app repo and update its tests. Don't route it yet."
- "Use the snape agent to get p50, p75, p90 and p95 page load time for yesterday, with the query."
- "Draft a reply to the latest message in the release thread. Don't send it."

## Which desk do I ask?

The name after the dash is the job. When in doubt, ask McGonagall. When you know exactly what you want, you can go straight to a desk.

| When I want to | Ask | How to reach them |
| --- | --- | --- |
| Get something done, or I'm not sure who should do it | McGonagall - Chief of Staff | A session opened in `~/hogwarts` |
| Build a feature or fix a bug | Harry - Senior Engineer | Through McGonagall, once Codex is approved |
| Review a diff, make an architecture call, or decide whether a bot comment is right | Hermione - Staff Engineer | Through McGonagall. Reviews also start on their own after Harry hands off |
| Get my own Claude-written change reviewed, or a security read | Moody - Security Reviewer | Through McGonagall, once Codex is approved |
| Know where my PRs stand, whether a red build is real, or watch a rollout | Ron - Release Engineer | His morning lineup, or ask McGonagall |
| Pull numbers from the warehouse or observability tools, or read an experiment | Snape - Data Analyst | From any Claude session: "Use the snape agent to..." |
| Find out why a desk lacked context, or tidy what the fleet remembers | Dumbledore - Knowledge Manager | His nightly patch, or ask McGonagall |

## What's live

What each piece looks like once onboarding is done, and what switches on next.

| Desk or piece | State | What switches it on |
| --- | --- | --- |
| The store and the castle | Live after onboarding stage 1 | Nothing. `castle doctor` confirms it. |
| McGonagall - Chief of Staff | Installed at stage 1, live from stage 4 | Open a session in `~/hogwarts` and trust the folder. |
| Snape - Data Analyst | Live in every Claude session after stage 3 | Nothing more, once the stage 3 deny rules keep your own sessions out of the office. |
| Owl Post - Message Router | Live after onboarding stage 3 | Nothing more. It wakes whenever a desk writes to its outbox. `scripts/owlpost-setup.sh` switches it on and sends a test owl to Hermione. |
| Hermione - Staff Engineer | Installed, off | `claude auth login` (stage 2), then the review stage's scripts (5.1). |
| Harry - Senior Engineer and Moody - Security Reviewer | Installed, off | Codex approved for your organization's source code, then the review stage (5.1). |
| Ron - Release Engineer and the Marauder's Map | Ron installed and off. The Map is not built yet | `claude auth login`, then the shadow stage (5.2). |
| Dumbledore - Knowledge Manager | Installed, off | `claude auth login`, then the memory stage (5.3). |
| Gringotts - Backup | Not built yet | The shadow stage (5.2). |

A desk switches on when you create its `enabled` file after reading its dry run, one desk at a time. Nothing switches itself on.

## Daily rhythm

- **Morning.** Open McGonagall's session. Her digest, and Ron's lineup once he's on, tell you what's in flight and what needs you.
- **Starting work.** Ask her in your own words. Check the TASK.md she writes, then say "go".
- **Quick data questions.** From any session: "Use the snape agent to..." He answers with the query behind every number.
- **When a session gets long.** If you see the Tempus warning (past about 200k tokens), ask for a Checkpoint and start a fresh session. Long sessions are the single biggest cost.
- **Reviews.** They happen before anything is pushed, by the other model family. You'll see the verdict in your rows.
- **Wrapping up.** Merge what's ready yourself, then type `Mischief managed <task-id>` for each finished task.

## herdr in five minutes

herdr keeps several terminal sessions in one window and keeps them running after you close it. It's handy for watching a couple of your own `claude` or `codex` sessions side by side. Fleet desks never run inside it, because any pane in herdr can type into any other pane.

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

Everything `castle` prints is JSON. If you added the shortcut in one-time setup, type `castle` instead of the full path.

**castle**

| Type | To |
| --- | --- |
| `castle task list` | See every task and its status |
| `castle task list --desk harry` | See one desk's tasks |
| `castle event drain` | See what needs you |
| `castle event ack <id>` | Clear a row once you've dealt with it |
| `castle request list --open` | See requests between desks still in flight |
| `castle audit` | Find stuck requests and unanswered owls |
| `castle fact current` | See what the fleet currently believes |
| `castle desk list` | See every desk and its job |
| `castle doctor` | Health check |

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

## Rules worth remembering

Six things only you do.

- **Merge and deploy.** No desk can merge, deploy, press a pipeline gate or change prod.
- **Close tasks.** Only "Mischief managed <task-id>" in McGonagall's session closes one. Silence and a green build don't.
- **Send things.** Desks draft messages. You send them.
- **Sign in.** Desks never see a password or token. If something needs a login, it stops and tells you.
- **Change settings.** Security, permissions, hooks and background jobs are yours to apply. The fleet only prepares the change.
- **Never bypass.** Don't start Claude or Codex with any skip-permissions or bypass flag, and don't run fleet desks inside herdr.

## Troubleshooting

| You see | Do this |
| --- | --- |
| Spotlight can't find herdr, castle or codex | They're command-line tools. Open Terminal and type the name there. |
| `owlpost-setup.sh` prints FAILED | It stopped at that step and changed nothing after it. The lines above say why. Fix that, then run it again; steps that already passed are safe to repeat. |
| `command not found: castle` | Use the full path `~/.hogwarts/bin/castle`, or add the shortcut from one-time setup and open a new Terminal window. |
| `install.sh` says a folder already exists | Nothing was changed. The fleet is already installed. Use `./install.sh --force` only if you want a fresh copy; it moves the old folders aside first. |
| A tool name with `<warehouse-mcp>`, `<observability-mcp>` or `<chat-mcp>` in it | A placeholder was never filled. See onboarding stage 2. |
| A headless desk never answers | Run `claude auth status`. Headless desks need the command line signed in. Check the desk has an `enabled` file. Then run `castle audit` to see where the request stopped. |
| McGonagall doesn't introduce herself | Make sure the session's folder is `~/hogwarts`. Her settings only apply there. |
| The Tempus warning appears | Ask for a Checkpoint, then start a fresh session. She picks up from the digest. |
| "Mischief managed" says it could not confirm | Close the task from your terminal: `castle token mint <task-id>`, then `castle task close <task-id> --reason complete --token-stdin` and paste the token. |
| `castle` exits with code 3 | The database was busy or a rule refused the change, such as a second active task for one desk. Read the JSON message. |
| `castle doctor` exits with code 5 | Something about permissions or the database looks unsafe. The JSON says what. Don't loosen permissions to make it pass. |

## Uninstall

Everything the fleet adds lives in two folders, a few background jobs and one agent file. Your own settings only change if you applied the prepared changes yourself, so undoing those is also yours.

From your clone of the repo, run `./uninstall.sh` to see every step it would take. It changes nothing. Then run `./uninstall.sh --yes` to do it. It archives both folders to `~/hogwarts-fleet-archive-<timestamp>.tar.gz` before it removes them, and it never edits your Claude or Codex settings. [UNINSTALL.md](UNINSTALL.md) has the same steps by hand, and how to restore from the archive.
