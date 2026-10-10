# Hogwarts fleet

A kit for running a small fleet of single-purpose AI coding agents on your Mac, with Claude Code and Codex. Each agent does one job. Before an agent's change can be pushed, a reviewer from the other model family has to pass that exact commit. Plain scripts move messages, watch your PRs, take backups and pick models without spending tokens.

You stay in charge. Merges, deploys, credentials and anything sent to a person come back to you. You can opt in to a few small automations, such as draft PRs for tasks that passed review, and each one is off until you switch it on.

The agents are called desks. Each one is named after a Harry Potter character, with its job after the name, like Hermione - Staff Engineer. In the theme, you are the Headmaster.

<table>
  <tr>
    <td align="center"><img src="office/assets/avatars/png/mcgonagall-128.png" width="88" alt=""><br><sub><b>McGonagall</b><br>Chief of Staff</sub></td>
    <td align="center"><img src="office/assets/avatars/png/harry-128.png" width="88" alt=""><br><sub><b>Harry</b><br>Senior Engineer</sub></td>
    <td align="center"><img src="office/assets/avatars/png/hermione-128.png" width="88" alt=""><br><sub><b>Hermione</b><br>Staff Engineer</sub></td>
    <td align="center"><img src="office/assets/avatars/png/moody-128.png" width="88" alt=""><br><sub><b>Moody</b><br>Security Reviewer</sub></td>
    <td align="center"><img src="office/assets/avatars/png/ron-128.png" width="88" alt=""><br><sub><b>Ron</b><br>Release Engineer</sub></td>
    <td align="center"><img src="office/assets/avatars/png/snape-128.png" width="88" alt=""><br><sub><b>Snape</b><br>Data Analyst</sub></td>
    <td align="center"><img src="office/assets/avatars/png/portrait-128.png" width="88" alt=""><br><sub><b>Dumbledore</b><br>Knowledge Manager</sub></td>
    <td align="center"><img src="office/assets/avatars/png/ollivander-128.png" width="88" alt=""><br><sub><b>Ollivander</b><br>Model Keeper</sub></td>
  </tr>
</table>

## Who it's for

One person on macOS who already works with Claude Code, Codex and GitHub pull requests, and wants agents with narrow jobs and hard guardrails rather than one agent with broad access.

It isn't a hosted service and it isn't multi-user. The fleet runs locally on one Mac, under your own user, with your own Claude and Codex sign-ins.

## Requirements

- macOS with the Command Line Tools, which bring `git` and `/usr/bin/python3` 3.9 or newer. The fleet runs on the system Python only, and its SQLite needs FTS5, which the macOS build has.
- Homebrew, and from it the Claude Code CLI, the Codex CLI, `jq`, `gh`, `ripgrep` and `shellcheck`.
- `claude`, `codex` and `gh` signed in from your own terminal.
- Your organization's approval before its code goes to OpenAI. Harry and Moody run on Codex, and both start switched off.

Optional: MCP servers for a SQL warehouse, observability and team chat (Snape and McGonagall use them), herdr for the live view, and RTK to compress Bash output. [Stage 0 of the onboarding guide](docs/ONBOARDING.md#stage-0-prerequisites) covers all of it.

## Quick start

Install the prerequisites first, then clone the kit anywhere except `~/hogwarts` or `~/.hogwarts`, and run the installer:

```
gh auth login
gh repo clone crisryantan/hogwarts-fleet ~/hogwarts-fleet
cd ~/hogwarts-fleet
./install.sh --terminal-loops
```

`install.sh` copies the kit into two folders in your home, creates the database, registers the desks and runs both test suites. It never changes `~/.claude/settings.json`, `~/.codex` or launchd, and every headless desk starts switched off.

The background jobs (the Owl Post, the Map, the closer and the rest) need a place to run. `--terminal-loops` is the recommended one: they run in a Terminal window with `~/.hogwarts/bin/fleet loops`, which hands them that window's folder access. That matters if your repos live in `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive, because macOS keeps launchd jobs out of those folders. Leave the flag off to use launchd. Either way the jobs stay off until you switch them on. [Terminal loops](docs/ONBOARDING.md#terminal-loops) has the details.

From there, follow [docs/ONBOARDING.md](docs/ONBOARDING.md) from stage 2. You switch on one piece at a time, and each stage ends with a "You're done when" check. If you'd like Claude to walk you through it, paste the [setup prompt](prompts/setup-prompt.md) into a fresh Claude Code session in your clone.

Once McGonagall is set up, open a session in `~/hogwarts`, ask for a change in your own words, read the TASK.md she writes, and type `go <task-id>` as the whole message.

## How it works

### The desks

| Desk | Model family | Job |
| --- | --- | --- |
| McGonagall - Chief of Staff | Claude | Your front desk in `~/hogwarts`. Turns your ask into a TASK.md, keeps the plan, routes work and drafts replies. |
| Harry - Senior Engineer | Codex | Builds each task in its own git worktree, with a failing-first test for each bug fix. Never pushes. |
| Hermione - Staff Engineer | Claude | Reviews Codex-written code, and checks bot review comments on your PRs. |
| Moody - Security Reviewer | Codex | Read-only, security-first review of Claude-written code, including work from your own Claude sessions. |
| Ron - Release Engineer | Claude | Sorts PR and CI changes into routine or for you, and writes the morning lineup and weekly scoreboard. |
| Snape - Data Analyst | Claude | Read-only warehouse and observability queries, with the source of every number. |
| Dumbledore - Knowledge Manager | Claude | Reviews the day each weeknight and proposes memory changes for you to apply. |

Each desk's role card asks for a tier (frontier, workhorse or fast), not a model. Ollivander maps that tier to a model inside the desk's own family, so a desk never changes family and the review rule holds. If a model is down, the desk runs on the next one of its family. Every headless desk has a daily run cap, and the Claude desks a daily spend cap, to stop a runaway loop. See [Busy days and caps](docs/HANDBOOK.md#busy-days-and-caps) and [When a model is down](docs/HANDBOOK.md#when-a-model-is-down).

Four scripts round out the fleet, and none of them uses a model: the Owl Post moves messages between desks and stamps each sender from the folder it came from, the Marauder's Map diffs PR and CI state on weekdays and wakes Ron only when something may need you, Gringotts takes a nightly local backup with credentials left out, and Ollivander's daily job does the model mapping.

### How a task moves

1. **Ask.** You describe the change to McGonagall. She writes a TASK.md with your words as the Intent, numbered acceptance criteria with a check each, and a Spec that names the repo, a new branch and its base. The repo can be a main checkout or a linked git worktree. A check is either one backtick command, which a script runs, or plain words, which a reviewer judges.
2. **Go.** You read the TASK.md, ask for changes until you're happy, then type exactly `go <task-id>` in her session. A hook confirms you typed it yourself, registers the task, routes it to Harry, makes him a fresh git worktree and, when he's enabled, starts his run. A go the hook refuses changes nothing and ends with a `Fix:` line saying what to change. From then on, McGonagall keeps watching the task and tells you in chat each time it moves, until it closes. With the `auto-go-updates` switch on, the same updates also reach your phone or notifications.
3. **Verify.** `fleet verify` runs each command check and records the command, exit code and output for that commit.
4. **Review.** Codex-written work goes to Hermione and Claude-written work goes to Moody. A pass counts only for that exact commit, and only when the reviewer's family differs from the author's. Harry's handoff starts his review, and CHANGES starts his fix round. The loop stops at PASS, at HEADMASTER or after three review rounds, and tells you.
5. **Push.** You run `fleet push`, which pushes exactly the reviewed commit and prints the `gh` command for a draft PR. With `auto-draft-pr` on, the loop does both and the PR stays a draft. The push gate blocks any agent's own `git push` that has no pass.
6. **Follow-ups.** The patrol watches CI and review comments. Hermione's bot pass only drafts replies and posts nothing. With `pr-followup` on, a review comment from a teammate with write access goes back to Harry, the other family reviews his fix, and the loop pushes it and posts his reply.
7. **Close.** You merge and deploy, then type `Mischief managed <task-id>`. With `auto-close` on, a closer instead proves the reviewed commit landed, CI is green and every after-merge check holds, then closes the task and tells you. It removes a closed build's worktree only when nothing in it could be lost. With `worktree-cleanup` on, the Map also sweeps up worktrees of tasks closed for at least three days. Without it, `fleet worktree-remove <task-id>` does it by hand.

The full walk-through, with every edge case, is in [DESIGN.md](docs/DESIGN.md#how-a-task-moves) and the [handbook's daily rhythm](docs/HANDBOOK.md#daily-rhythm).

### Automations

Every automation is one file in the office, off until you write `on` into it, and off again when you remove it. A switch counts only while it's a plain file you own that no one else can write. There are nine:

- `auto-draft-pr`, `pr-followup`, `auto-close` and `worktree-cleanup`, described above.
- `auto-portrait`, which lets Dumbledore's additions apply themselves. His edits, retires and archive moves always wait for you.
- `owl-reports`, a one-line report from McGonagall on each owl another desk sends her.
- `auto-orchestrate`, which lets her headless turn pick the next step from a fixed list of typed actions that a script checks and runs, under daily caps. One of those actions restarts a fix round that died before doing any work.
- `auto-go-updates`, the phone and notification updates from step 2.
- `cross-family-failover`, which lets a desk whose own family is down run on the other one, never a builder or a reviewer.

[Customise](docs/CUSTOMISE.md#switch-the-automations-on-and-off) and the handbook's [What's on after stage 4](docs/HANDBOOK.md#whats-on-after-stage-4) give the exact rules for each.

### Two homes

- `~/.hogwarts` is the office. It holds the store (a SQLite database and its `castle` CLI), the `fleet` command, the hooks and scripts, each desk's brief, role card and locked-down settings, and the switches. No desk can write there, and Claude desks can't read it.
- `~/hogwarts` is the castle, where desks work. It holds the charter, the plan, your standing orders, each desk's inbox and outbox, the task folders and the git worktrees. It's a local git repo with no remote.

## Safety model

The design answers five ways a group of agents can go wrong. [DESIGN.md](docs/DESIGN.md#five-risks-and-what-closes-each-one) has the full table.

1. One terminal pane driving another. No desk that can run commands lives in a terminal multiplexer.
2. Desks inheriting your broad allow list. Headless Claude desks start with `--restricted` and their own settings. Codex desks ignore your Codex config and run under a fleet permission profile.
3. A watchdog typing into sessions. Nothing in the fleet types into a session. It only warns.
4. Faked identity. A desk can post only to its own outbox, and the Owl Post stamps the sender from the folder.
5. Code loading from folders agents can write. The controls live in the office, and the store runs on the system Python with a cleared environment.

Some things always come back to you: merges, deploys, prod changes, credentials and sign-ins, security settings, installs, force pushes, deletions and anything sent to a person. Marking a PR ready is yours, and so are replies to teammates unless you switch on follow-ups. The only pre-approvals are the standing orders you write and the switches in the office, and none of them can include a merge or a deploy.

The push gate is a guardrail, not a wall. It reads an agent's Bash command as text and blocks a `git push` that has no pass, which stops a push made by habit or mistake. A desk's real boundary is its sandbox, where the network is off or limited to named hosts. Pushes you type in your own terminal never reach the gate.

## Docs

- [Onboarding](docs/ONBOARDING.md): takes a fresh Mac to a working fleet, one stage at a time.
- [Handbook](docs/HANDBOOK.md): daily use, which desk to ask, the cheat sheet and troubleshooting. There's also a [standalone HTML copy](docs/handbook.html) to open in a browser.
- [Design](docs/DESIGN.md): how the fleet works, the five risks it closes and its known limits.
- [Customise](docs/CUSTOMISE.md): change a desk's role card, pin or block models, add or retire a desk, and change caps, budgets and schedules.
- [Uninstall](docs/UNINSTALL.md): take it all off again, by script or by hand.
- [Setup prompt](prompts/setup-prompt.md): let a fresh Claude Code session walk you through onboarding.

## What is in this repo

| Path | What it is | Installed to |
| --- | --- | --- |
| `office/` | The store, `bin/castle`, `bin/fleet`, the fleet scripts and hooks, both test suites, desk briefs, role cards and settings, launchd templates, pending settings snippets and the avatars | `~/.hogwarts` |
| `castle/` | The charter, an empty plan, standing orders, desk folders, McGonagall's settings and agent file, and a Codex config that keeps any Codex session opened there read-only | `~/hogwarts` |
| `claude-agents/snape.md` | Snape's user-level agent file | `~/.claude/agents/snape.md`, only if absent |
| `install.sh`, `uninstall.sh` | Install the two homes, or remove them. Uninstall is a dry run unless you pass `--yes`, and keeps an archive | |
| `scripts/*-setup.sh` | Switch on the Owl Post, the patrol (in shadow mode), Dumbledore's nightly review and the terminal loops, one onboarding stage each | |
| `scripts/codex-boundary-test.sh`, `scripts/codex-exec-boundary-test.py` | Prove the Codex desks' permission profile on your Mac, including under real `codex exec` runs. Run them before enabling Harry or Moody, and after every Codex upgrade | |
| `scripts/installed-office-check.sh` | Installs the kit into a throwaway home with made-up values and runs both suites there, so a test that leans on your own private values fails | |

## Limits

- The fleet runs on macOS only, for one person on one Mac.
- The Codex desks' read boundary rests on Codex permission profiles, which Codex marks as beta. Rerun `scripts/codex-boundary-test.sh` after every Codex upgrade.
- The review loop, closer, patrol and follow-ups are tested against temporary git repos and a faked GitHub. Try them on a small real task before you rely on them. The patrol starts in shadow mode, where it only writes files under the office, until you take it out.

Tested with Claude Code 2.1.295, Codex 0.162.0 and herdr 0.9.3. [DESIGN.md](docs/DESIGN.md#known-limits) lists every known limit.
