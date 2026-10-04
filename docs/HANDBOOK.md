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
3. **Code tab: give her one real ask.** Ask for work that changes code, in your own words. Claude asks before she writes `tasks/<id>/TASK.md`; allow it, read it, fix anything wrong, then reply "go". She hands you one `castle task create` command. A data question is different. It gets no TASK.md and no command. She hands you a prompt for Snape instead, and you run it as in step 7.
4. **Terminal: register the task.** Paste the command she gave you, then check it's there with `~/.hogwarts/bin/castle task list`.
5. **Both: do the work.** Until Harry is switched on, you do the work in your usual Claude session in that repo. Mark the task started first:

   ```
   ~/.hogwarts/bin/castle task start <task-id>
   ```

6. **Both: close it.** After you merge, mark it ready to close in the Terminal, then type `Mischief managed <task-id>` to McGonagall. Done when she confirms the task closed. If she says she couldn't confirm it was you typing, close it from the Terminal with `castle token mint` and `castle task close`, as in [Troubleshooting](#troubleshooting).

   ```
   ~/.hogwarts/bin/castle task await-close <task-id>
   ```

7. **Code tab: ask Snape for numbers.** Start a new session in any folder except `hogwarts`. McGonagall's session can't call Snape. Paste: "Use the snape agent to..." If your question has a link in it, start with "Read <link>, then use the snape agent to...", because Snape can't open links. He answers with the query behind every number. Done when he gives you numbers with their queries. Paste his answer back to McGonagall if you want it filed.

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
| **herdr** (optional) | A terminal workspace manager: tabs and split panes, and one space per desk if you want a live view of the fleet. | Type `herdr` in Terminal. It isn't a Mac app, which is why Spotlight can't see it. | Only if you like a multi-pane view. Desks that can run commands never run inside it, so most spaces are read-only feeds. |

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
4. **Check her ticket.** For real work she writes `tasks/<id>/TASK.md`: your words under Intent, numbered acceptance criteria with the check for each, and what's out of scope. Claude asks you before she writes it. Fix anything that's wrong, then reply "go". She hands you one `castle task create` command to register it. Run that in Terminal. A data question skips all of this: she hands you a prompt to run with Snape in another session.
5. **Let it move.** She posts the work to the right desk through her outbox, and Claude asks you before each post. The Owl Post delivers it. You'll see rows for anything that needs you at the start of your next message to her.
6. **Merge and close.** When a PR is green and reviewed, you merge it yourself. Then type `Mischief managed <task-id>` to her. That exact phrase is the only thing that closes a task.

Good first prompts:

- "What's in flight, and is anything waiting on me?"
- "Write a TASK.md for: rename the retry constant in my web-app repo and update its tests. Don't route it yet."
- "I need p50, p75, p90 and p95 page load time for yesterday." She writes the Snape prompt for you to run in another session.
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
| Pull numbers from the warehouse or observability tools, or read an experiment | Snape - Data Analyst | A new session outside `~/hogwarts`: "Use the snape agent to..." |
| Find out why a desk lacked context, or tidy what the fleet remembers | Dumbledore - Knowledge Manager | His nightly patch, or ask McGonagall |
| See or steer which model each desk runs, or find out why one moved | Ollivander - Model Keeper | He runs by himself every morning. `castle desk models` shows his picks, and his notes arrive with your other rows |

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
| Ollivander - Model Keeper | Installed, off | Load his daily job (5.5). Until then every desk runs the model it was registered with. |
| The live view: `fleet feed` and the herdr spaces | Installed, off | Run `hogwarts-spaces` (5.6). `fleet feed` itself works any time. |
| Busy-day caps and review rounds | Live once installed | Nothing. They guard every headless run, so they matter once a desk is on. `castle desk caps` shows today's numbers. |

A desk switches on when you create its `enabled` file after reading its dry run, one desk at a time. Nothing switches itself on.

## Daily rhythm

- **Morning.** Open McGonagall's session. Her digest, and Ron's lineup once he's on, tell you what's in flight and what needs you.
- **Starting work.** Ask her in your own words. Check the TASK.md she writes, then say "go".
- **Quick data questions.** In a new session outside `~/hogwarts`: "Use the snape agent to..." When the question has a link, start with "Read <link>, then". He answers with the query behind every number.
- **When a session gets long.** If you see the Tempus warning (past about 200k tokens), ask for a Checkpoint and start a fresh session. Long sessions are the single biggest cost.
- **Reviews.** They happen before anything is pushed, by the other model family. You'll see the verdict in your rows.
- **Model notes.** Ollivander's notes arrive with your other rows. A move to a cheaper or equal model has already happened. A costlier one waits for `castle desk model <desk> --approve`.
- **Cap warnings.** A desk at 80% of a daily cap sends one note. At the cap its next run waits for the reset or a bump, as in [Busy days and caps](#busy-days-and-caps).
- **Wrapping up.** Merge what's ready yourself, then type `Mischief managed <task-id>` for each finished task.

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

**How he picks**

- Claude desks take the alias for their tier: `opus` for frontier, `sonnet` for workhorse and `haiku` for fast. An alias always means the newest model of that line that your Claude Code knows, so a new release needs no edit.
- Codex desks take a model from Codex's own catalog. He files each one by the wording of its description, then takes the top-ranked visible model of the tier. He skips anything the catalog calls older, legacy or previous generation, and anything that retires within 30 days.
- The effort is the card's, lowered to the nearest level that model lists.
- A name that fits no tier, or more than one, is never picked. You get one note asking you to file it with `castle model line <name> <line>`, where the line is `frontier`, `workhorse`, `fast` or `ignore`. Your filing beats his guess.

**What happens after a pick**

- A headless desk's first pick applies by itself. That covers Hermione, Ron, Dumbledore, Harry and Moody.
- After that, a move to the same tier or a cheaper one (fast, then workhorse, then frontier) applies by itself, with a note.
- A costlier move waits as a pending pick. To say yes, run `castle desk model <desk> --approve`. If a later pass no longer makes that pick, for any reason, it's dropped and you get a quiet note. `--approve` also checks the pick against the latest catalog first, and refuses one that's gone, hidden, filed under another tier or retiring within 30 days.
- McGonagall and Snape take their model from the `model:` line in their agent files. Ollivander never edits those files. For them he only sends a note with the one line to change, even for a first pick.

**Look and steer**

```
castle desk models                  each desk's tier, model, pin and pending pick
fleet ollivander --dry-run          the whole plan as JSON, changing nothing
castle desk model <desk> <model>    pin a desk to one model
castle desk model <desk> --role     unpin, so the role picks again
castle desk model <desk> --approve  take a pending costlier pick
castle model line <name> <line>     file a model name under a tier
```

`fleet` is `~/.hogwarts/bin/fleet`, the same kind of command as `castle`. A pin takes an alias or a full `claude-` model id for a Claude desk, and a slug from the last Codex catalog for a Codex desk. Ollivander leaves a pinned desk alone.

**Safety nets**

- **The two-run trial.** The first two runs after a switch are a trial. If both fail, the desk goes back to its previous model, pinned, and you get a note. Unpin it with `--role` once you've looked. A run that Claude's or Codex's own usage limit stopped never counts. A switch you made yourself, by pin or approval, is never reverted. You hear about the failures and your choice stands. Pinning the model a desk is on during its trial ends the trial, so two failures after that never move it. Because a revert pins, it never lands on a model you filed as ignore, or one the latest catalog no longer lists, hides or retires within 30 days. The desk stays where it is, unpinned, and you get a note.
- **The blocklist.** If your organization forbids some models, list them in `BLOCKED_MODEL_PREFIXES` in `~/.hogwarts/fleet/config.py`. It is empty in the kit. Each entry is a lowercase prefix, matched against Claude aliases, full Claude ids and Codex slugs. To forbid a whole Claude line, list its alias and its id prefix, for example `("<alias>", "claude-<alias>-")`. A blocked model is never picked, pinned, filed or launched, and neither is an alias that has ever run as one, however many switches or later runs came between. If every model of a tier is blocked, the desk keeps the one it has and you get one note. While anything is blocked, a Codex desk with no model of its own isn't launched either, because the Codex CLI default can't be checked against the list. Ollivander's first pass gives each unpinned Codex desk its pick, so run `~/.hogwarts/bin/fleet ollivander` once after you fill the list, or pin a model. A desk a failed trial pinned to no model is one he leaves alone, so pin it a model or hand it back with `--role`. The refusal and his daily note both say so.
- **The stop file.** When a CLI update goes wrong, Ollivander writes `~/.hogwarts/state/ollivander-stop`, and no headless desk launches until you've looked and run `castle ollivander clear`.
- **CLI updates.** They are off unless you make the plain file `~/.hogwarts/desks/ollivander/update-clis`. With it, each pass runs `claude update` and `brew upgrade --cask codex`, then checks both versions and every enabled desk's dry run. Any failure stops the desks. So does a new Codex version, because the Codex boundary is proven per version: rerun `scripts/codex-boundary-test.sh` first. A new Claude Code version is only a note. An update never starts while a desk is between its last check and the end of its run. If a pass dies part way through an update, the next one stops the desks.
- **The daily job.** `com.hogwarts.ollivander` runs at 06:00 every day. Onboarding stage 5.5 loads it.

## Busy days and caps

Every headless desk has a daily cap on runs, and the Claude desks have a cap on spend too. The caps are runaway guards, not targets. They're sized for a busy day of a dozen or so PRs plus side work, so a normal day never sees them.

| Desk | Runs a day | Spend a day |
| --- | --- | --- |
| Hermione - Staff Engineer | 80 | $60 |
| Ron - Release Engineer | 120 | $10 |
| Dumbledore - Knowledge Manager | 3 | $4 |
| Harry - Senior Engineer | 40 | none |
| Moody - Security Reviewer | 80 | none |

The day resets at local midnight on your Mac, daylight saving included. A run counts the moment it starts, so one that gets killed or crashes still counts. Spend comes from the cost each run records.

- **The warning.** A desk that reaches 80% of a cap sends one note for that cap that day.
- **The cap event.** At the cap, the desk's next run doesn't start and its request keeps waiting. You get one note that names the cap, how much was used, how many requests are waiting, when it resets and the command that lifts it.
- **Look and lift.** `castle desk caps` shows today's numbers for every headless desk. McGonagall, Snape and the scripts have no cap, so they don't appear. `castle desk cap <desk> --runs +N` or `--spend +X` raises one cap until the next reset, then it falls back. A bump is at most +500 runs or +$500. `--spend` works only for Hermione, Ron and Dumbledore, because Harry and Moody have no spend cap.
- **Reviews never wait.** One review of a task runs at a time. Start a second while the first is going and it stops at once with "a review of this task is already running; run it again when it ends", without touching anything. If the reviewer is busy with another task, or at its cap, the review request is queued and the command says so. Run `fleet review <task-id>` again later, after the reset or a bump if it was the cap. For a task from your own Claude sessions, run `fleet review own --repo-dir <checkout> --task <task-id>` instead. The new review replaces the queued one, so only the newest commit of a task gets reviewed.
- **Three rounds per task.** A task gets three review rounds. Only a round where the reviewer recorded a verdict counts. A crash, a timeout, a cap refusal or a vendor limit doesn't use one up. If a review gets killed partway, its reviewer keeps going until it finishes, and until then that task can't be reviewed again and the reviewer counts as busy. After that the round doesn't count, and the next review that finds that reviewer free cleans up after it. The next round waits for you: `castle task allow-round <task-id>` allows exactly one more, and a queued round doesn't use it up. `castle task rounds <task-id>` lists every round and whether it counts.
- **Which limit hit.** The note says whether it was the fleet's cap or the vendor's own limit: `cap_source fleet`, or `claude_plan` or `codex_plan` when your Claude or Codex plan's own usage or rate limit stopped the run. A bump can't lift a plan limit. It clears on the vendor's own reset.

## Watch live, run short

Desks run short. A headless desk takes one owl, does the job and exits, one run at a time, so there's no long session to sit inside. You can still watch every desk work, without typing into anything.

- `fleet feed --desk <name>` follows one desk. Use the desk's registry name. Dumbledore's is `portrait`, and a name that matches no desk just shows nothing. `fleet feed --desk owl-post` follows every owl.
- `fleet feed --all` follows every desk at once.

A feed prints a line whenever something happens: owls to and from the desk (kind and subject, never the body), the start and end of each run with its model, time, tokens and cost, the desk's notes to you, and, while a run is going, what the desk says and which tools it calls. It is read-only. It opens the store read-only, only reads files, and strips every control sequence from what a desk wrote, so a desk can't steer your terminal through it. Ctrl+C stops it.

**One space per desk.** `~/.hogwarts/bin/hogwarts-spaces` opens a herdr space for each desk, named like "Hermione - Staff Engineer". McGonagall and Snape get live sessions, because neither has a shell tool. Hers opens in `~/hogwarts`, and his opens in your Documents folder, outside the castle. Every other space, including the Owl Post and Ollivander, runs a feed, because any herdr pane can type into any other pane, and a desk that can run commands must never sit where it could type into the rest. Before it opens either live session, it reads that agent's definition (Snape's `~/.claude/agents/snape.md`, McGonagall's in `~/hogwarts/.claude/agents/`, plus any other file that defines the same agent) and refuses the space with FAILED unless it has a `tools:` line and every tool on it, built-in or MCP, is on that agent's trusted list in `~/.hogwarts/desks/<agent>/live-tools.json`. Names must match exactly, so a new tool stays out until you add it there, even one from a server that's already on the list. [CUSTOMISE.md](CUSTOMISE.md#trust-a-tool-in-a-live-space) shows how. A file anywhere in those folders whose name it can't read plainly, or whose frontmatter doesn't read cleanly, gets checked too. Each live session runs the `claude` that `CLAUDE_BIN` in `~/.hogwarts/fleet/config.py` names, and starts with a fixed list of built-in tools, every command-running tool denied and skills turned off, so an edit made after the check still gets no shell. That also means Snape can't load a skill in his live space. He can still read a skill's file.

```
~/.hogwarts/bin/hogwarts-spaces --dry-run
~/.hogwarts/bin/hogwarts-spaces
```

- `--dry-run` lists what it would open and changes nothing.
- `--only "<label>"` handles just the space with exactly that name.
- `--herdr <path>` points it at herdr if that isn't at `~/.local/bin/herdr`.
- A space whose name already exists is left alone, so running it twice is safe. It prints OK, SKIP or FAILED for each space.

**herdr in five minutes.** herdr keeps several terminal sessions in one window and keeps them running after you close it. It's handy for the spaces above, and for watching a couple of your own `claude` or `codex` sessions side by side.

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
| `castle desk caps` | See today's runs and spend against each desk's caps |
| `castle desk cap <desk> --runs +N` | Lift a desk's run cap until the next reset |
| `castle desk cap <desk> --spend +X` | Lift a desk's spend cap until the next reset |
| `castle task rounds <task-id>` | See a task's review rounds and which count |
| `castle task allow-round <task-id>` | Allow one more review round |
| `castle desk models` | See each desk's tier, model, pin and pending pick |
| `castle desk model <desk> --approve` | Approve a costlier pick that's waiting |
| `castle desk model <desk> <model>` | Pin a desk to a model |
| `castle desk model <desk> --role` | Unpin a desk |
| `castle model line <name> <line>` | File a model name as frontier, workhorse, fast or ignore |
| `castle ollivander clear` | Clear Ollivander's stop file so desks launch again |
| `castle doctor` | Health check |

**fleet** (the same path style as castle: `~/.hogwarts/bin/fleet`)

| Type | To |
| --- | --- |
| `fleet feed --desk <name>` | Watch one desk, read-only |
| `fleet feed --all` | Watch every desk, read-only |
| `fleet ollivander --dry-run` | See Ollivander's plan without changing anything |
| `fleet review <task-id>` | Review a build desk's newest commit, or run a queued review again |
| `fleet review own --repo-dir <checkout> --task <task-id>` | The same for a task from your own Claude sessions |
| `~/.hogwarts/bin/hogwarts-spaces` | Open one herdr space per desk |

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
- **Never bypass.** Don't start Claude or Codex with any skip-permissions or bypass flag, and don't run a desk that can run commands inside herdr.

## Troubleshooting

| You see | Do this |
| --- | --- |
| Spotlight can't find herdr, castle or codex | They're command-line tools. Open Terminal and type the name there. |
| `owlpost-setup.sh` prints FAILED | It stopped at that step and changed nothing after it. The lines above say why. Fix that, then run it again; steps that already passed are safe to repeat. |
| `command not found: castle` | Use the full path `~/.hogwarts/bin/castle`, or add the shortcut from one-time setup and open a new Terminal window. |
| `install.sh` says a folder already exists | Nothing was changed. The fleet is already installed. Use `./install.sh --force` only if you want a fresh copy; it moves the old folders aside first. |
| A tool name with `<warehouse-mcp>`, `<observability-mcp>` or `<chat-mcp>` in it | A placeholder was never filled. See onboarding stage 2. |
| A headless desk never answers | Run `claude auth status`. Headless desks need the command line signed in. Check the desk has an `enabled` file. Then run `castle audit` to see where the request stopped. If a note says a cap is reached, see [Busy days and caps](#busy-days-and-caps). |
| No headless desk launches, and a note mentions Ollivander | His stop file is in place. Read the note and his log in `~/.hogwarts/logs`, then run `castle ollivander clear`. |
| A review round was refused | The task has used its three rounds. Run `castle task rounds <task-id>` to look, then `castle task allow-round <task-id>` if one more is worth it. |
| A desk keeps an old model after a note said it would move | A costlier move waits for you: `castle desk model <desk> --approve`. A pinned desk never moves. `castle desk models` shows both. |
| `hogwarts-spaces` prints FAILED | herdr isn't running or isn't at `~/.local/bin/herdr`. Open herdr, or pass `--herdr <path>`. A space it already made is left alone when you run it again. If the line says refused, that agent's definition has no `tools:` line or lists a tool that isn't on its trusted list in `~/.hogwarts/desks/<agent>/live-tools.json`. Compare it with the repo copy, `claude-agents/snape.md` or `castle/.claude/agents/mcgonagall.md`, and put it right, or trust a read-only tool as [CUSTOMISE.md](CUSTOMISE.md#trust-a-tool-in-a-live-space) shows. If it says the list is not trusted, it's a link or others can write it; put a plain copy back with `chmod 600`. Then run it again. If it says claude is not at a path, fix `CLAUDE_BIN` in `~/.hogwarts/fleet/config.py`. |
| McGonagall doesn't introduce herself | Make sure the session's folder is `~/hogwarts`. Her settings only apply there. |
| The Tempus warning appears | Ask for a Checkpoint, then start a fresh session. She picks up from the digest. |
| "Mischief managed" says it could not confirm | Close the task from your terminal: `castle token mint <task-id>`, then `castle task close <task-id> --reason complete --token-stdin` and paste the token. |
| `castle` exits with code 3 | The database was busy or a rule refused the change, such as a second active task for one desk. Read the JSON message. |
| `castle doctor` exits with code 5 | Something about permissions or the database looks unsafe. The JSON says what. Don't loosen permissions to make it pass. |

## Uninstall

Everything the fleet adds lives in two folders, a few background jobs and one agent file. Your own settings only change if you applied the prepared changes yourself, so undoing those is also yours.

From your clone of the repo, run `./uninstall.sh` to see every step it would take. It changes nothing. Then run `./uninstall.sh --yes` to do it. It archives both folders to `~/hogwarts-fleet-archive-<timestamp>.tar.gz` before it removes them, and it never edits your Claude or Codex settings. [UNINSTALL.md](UNINSTALL.md) has the same steps by hand, and how to restore from the archive.
