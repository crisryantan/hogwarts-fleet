# Hogwarts fleet

A kit for running a small fleet of single-purpose AI coding agents on your Mac, with Claude Code and Codex. Each agent does one job. Before an agent's change can be pushed, a reviewer from the other model family has to pass that exact commit. Plain scripts move messages, watch your PRs, take backups and pick models without spending tokens. You stay in charge: merges, deploys, credentials and anything sent to a person always come back to you, apart from two things you can switch on: a draft PR for each passed task, and replies to teammates' review comments on those PRs.

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

- macOS with the Command Line Tools, which bring `git` and `/usr/bin/python3` 3.9 or newer. The fleet runs on the system Python only.
- SQLite with FTS5. The macOS build has it.
- Homebrew, and from it the Claude Code CLI, the Codex CLI, `jq`, `gh`, `ripgrep` and `shellcheck`.
- `claude`, `codex` and `gh` signed in from your own terminal.
- Your organization's approval before its code goes to OpenAI. Harry and Moody run on Codex, and both start switched off.

Optional:

- MCP servers for a SQL warehouse, observability and team chat. Snape and McGonagall use them. Use the name `none` for any kind you don't have.
- herdr, a third-party terminal workspace manager, for the live view.
- RTK, a third-party tool that compresses Bash output.

## Quick start

Install the prerequisites first, as in stage 0 of [docs/ONBOARDING.md](docs/ONBOARDING.md). Then clone the kit anywhere except `~/hogwarts` or `~/.hogwarts`, and run the installer:

```
gh auth login
gh repo clone crisryantan/hogwarts-fleet ~/hogwarts-fleet
cd ~/hogwarts-fleet
./install.sh
```

`install.sh` copies the kit into two folders in your home, creates the database, registers the desks and runs both test suites. It refuses to touch an existing install. It never changes `~/.claude/settings.json`, `~/.codex` or launchd, and every headless desk starts switched off.

From there, follow [docs/ONBOARDING.md](docs/ONBOARDING.md) from stage 2. You switch on one piece at a time, and each stage ends with a "You're done when" check. If you'd like Claude to walk you through it, paste the [setup prompt](prompts/setup-prompt.md) into a fresh Claude Code session in your clone.

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
| Dumbledore - Knowledge Manager | Claude | Reviews the day each weeknight and proposes memory changes. You apply them, or switch on auto-portrait to let his additions apply themselves. His edits, retires and archive moves always wait for you. |

Each desk's role card asks for a tier (frontier, workhorse or fast), not a model. Ollivander - Model Keeper maps that tier to a model inside the desk's own family, so a desk never changes family and the review rule holds.

Harry, Hermione, Moody, Ron and your own sessions can each keep many tasks open. A desk runs one model process at a time, or two for the reviewers, so a task waiting on fixes doesn't hold up the others. Every headless desk has a daily run cap, and the headless Claude desks a daily spend cap. The caps are there to stop a runaway loop. They're sized for a busy day and reset at local midnight.

### The scripts

None of these uses a model.

| Script | Job |
| --- | --- |
| Owl Post - Message Router | Moves messages (owls) between desks and stamps each sender from the folder it came from. |
| Marauder's Map - PR Watcher | Diffs PR and CI state every 15 minutes, weekdays 08:00 to 19:00, and wakes Ron only for a change that may need you. While you have follow-ups switched on and the patrol is out of shadow mode, each round also sends teammates' review comments on PRs the loop opened back to Harry, while you have auto-close switched on, it starts the closer, and while you have the worktree cleanup switched on, it removes the worktrees of build tasks closed at least three days ago. |
| Gringotts - Backup | Takes a nightly local backup with credentials left out. `fleet gringotts --drill` tests a restore. |
| Ollivander - Model Keeper | Each morning, maps every desk's role card to a model in its own family. |

### How a task moves

1. You ask McGonagall. She writes a TASK.md with your words as the Intent, plus numbered acceptance criteria, each with a check, and a Spec that opens with `repo:`, `branch:` and `base:` lines naming the repo, a new branch and its base. A check is either one backtick command, which a script runs, or plain words, which the reviewer judges. A check that mixes the two is malformed and never runs. A criterion labelled `| after merge:` instead of `| check:` is something only true once the change is merged, and waits until after the merge. You read it, ask for changes until you're happy, then type exactly `go <task-id>` in her session.
2. The hook that reads your prompts checks you typed the go yourself, then registers the task, routes it to Harry, gives his task a fresh git worktree from the Spec and starts his run there when he's enabled. A go works only in McGonagall's session. A go the hook refuses changes nothing and says why. If TASK.md is the cause, McGonagall fixes it and you type the go again. If the hook can't confirm you typed it, or the go still can't be applied, you register the task with `castle task create` instead, McGonagall routes it to Harry by owl, and you give his task its worktree with `fleet worktree`. Work from your own Claude sessions joins at the next step, through `fleet review own`.
3. `fleet verify` runs each acceptance check that is a command and records the command, exit code and output for that commit. It lists after-merge checks and never runs them.
4. `fleet review` sends Codex-written work to Hermione and Claude-written work to Moody. A pass counts only for that exact commit, and only when the reviewer's family differs from the author's. Harry's handoff starts his review by itself, and CHANGES starts his fix round. The loop stops at PASS, at HEADMASTER or at the round cap of three review rounds, and tells you.
5. You run `fleet push`, which pushes exactly the reviewed commit and prints the `gh` command for a draft PR. You open the PR yourself, unless you switch on draft PRs with `echo on > ~/.hogwarts/auto-draft-pr`: then the review loop does both after its own PASS, and the PR stays a draft. `rm ~/.hogwarts/auto-draft-pr` switches it off. The push gate blocks any agent's own `git push` that has no pass.
6. The patrol (the Map, Ron and Hermione's bot pass) watches CI and review comments. The bot pass only writes drafts for you and posts nothing. If you switch on follow-ups with `echo on > ~/.hogwarts/pr-followup`, a review comment from someone with write access on a PR the loop opened goes back to Harry, who fixes the code or answers it. The other family reviews his fix and his replies, and on PASS the loop pushes to the same branch and posts each reply once with your `gh` login. Nothing resolves a thread. Follow-ups need draft PRs on and the patrol out of shadow mode, and `rm ~/.hogwarts/pr-followup` switches them off.
7. You merge and deploy, then close the task by typing `Mischief managed <task-id>`. Or switch on auto-close with `echo on > ~/.hogwarts/auto-close`, and off with `rm ~/.hogwarts/auto-close`. While it's on, each Map round starts the closer. It closes a passed task once scripts prove the reviewed commit landed (a PR merged at exactly that commit, or the commit on its base), CI on the merge commit is green and every after-merge command passes, and the reviewer of the other family judges that every written after-merge check holds. You get one row for each close, naming what proved each check. It skips a task whose follow-up is still open, and a build you registered by hand stays yours to close. Anything it can't prove stops that task and tells you once, and `fleet close <task-id>` tries it again. With auto-close on, typing `Mischief managed <task-id>` closes a task by hand too. When the closer closes one of Harry's builds, it removes that task's worktree in the same pass, but only when nothing in it is uncommitted, it holds no git-ignored files apart from the dependency links the fleet made, and its HEAD is the commit the close proved or one already on the base. The close row says the worktree is being removed, or names one it keeps and why, and a removal a kill cut short is finished on the next Map round.
8. A closed task's worktree stays until you remove it with `fleet worktree-remove <task-id>`, or until you switch on the worktree cleanup with `echo on > ~/.hogwarts/worktree-cleanup` (`rm ~/.hogwarts/worktree-cleanup` switches it off). While it's on, each Map round removes the worktree of every build task closed at least three days ago, however it was closed, once it has no uncommitted changes, no git-ignored files apart from the dependency links the fleet made, and its HEAD is on the base or its branch on origin after a fetch. It never deletes a branch, it skips a worktree whose task is busy, and one it can't remove or can't read stays put and tells you once. A round's removals come to you as one row.

### Two homes

- `~/.hogwarts` is the office. It holds the store (a SQLite database and its `castle` CLI), the `fleet` command, the hooks and scripts, each desk's brief, role card and locked-down settings, the launchd templates and the pending settings snippets. It also holds one switch for each automation: `auto-draft-pr`, `pr-followup`, `auto-close`, `worktree-cleanup` and `auto-portrait`. A switch is on only while it's a plain file you own, that no one else can write, holding exactly `on`, so all five are off until you write them. No desk can write here, and Claude desks can't read it.
- `~/hogwarts` is the castle, where desks work. It holds the charter, the plan, your standing orders, each desk's scratchpad, inbox and outbox, the task folders and the git worktrees. It's a local git repo with no remote.

## Safety model

The design answers five ways a group of agents can go wrong. [DESIGN.md](docs/DESIGN.md#five-risks-and-what-closes-each-one) has the full table.

1. One terminal pane driving another. No desk that can run commands lives in a terminal multiplexer. The only live sessions there are McGonagall and Snape, who have no shell tool, and that is checked before each one opens.
2. Desks inheriting your broad allow list. Headless Claude desks start with `--restricted` and their own settings. Codex desks ignore your Codex config and run under a fleet permission profile.
3. A watchdog typing into sessions. Nothing in the fleet types into a session. It only warns.
4. Faked identity. A desk can post only to its own outbox, and the Owl Post stamps the sender from the folder.
5. Code loading from folders agents can write. The controls live in the office, and the store runs on the system Python with a cleared environment.

Some things always come back to you: merges, deploys, prod changes, credentials and sign-ins, security settings, installs, anything sent to a person (opening a ready PR included, and replies to teammates unless you switch on follow-ups), closing a task unless you switch on auto-close, force pushes and deletions. The only pre-approvals are the standing orders you write yourself and the five switches in the office, and none of them can include a merge or a deploy. No script parses the standing orders, so an automation runs only while its switch is on.

The push gate is a guardrail, not a wall. It reads an agent's Bash command as text and blocks a `git push` that has no pass, which stops a push made by habit or mistake. A desk's real boundary is its sandbox, where the network is off or limited to named hosts. Pushes you type in your own terminal never reach the gate.

## Docs

[docs/ONBOARDING.md](docs/ONBOARDING.md) takes a fresh Mac to a working fleet, one stage at a time.

- [Handbook](docs/HANDBOOK.md): daily use, which desk to ask, the cheat sheet and troubleshooting. There's also a [standalone HTML copy](docs/handbook.html) to open in a browser.
- [Design](docs/DESIGN.md): how the fleet works, the five risks it closes and its known limits.
- [Customise](docs/CUSTOMISE.md): change a desk's role card, pin or block models, add or retire a desk, and change caps, budgets and schedules.
- [Uninstall](docs/UNINSTALL.md): take it all off again, by script or by hand.
- [Setup prompt](prompts/setup-prompt.md): let a fresh Claude Code session walk you through onboarding.

## What is in this repo

| Path | What it is | Installed to |
| --- | --- | --- |
| `office/` | The store, `bin/castle`, `bin/fleet`, `bin/hogwarts-spaces`, the fleet scripts and hooks, both test suites, desk briefs, role cards and settings, launchd templates, pending settings snippets and the avatars | `~/.hogwarts` |
| `castle/` | The charter, an empty plan, standing orders, desk folders, McGonagall's settings and agent file, and a Codex config that keeps any Codex session opened there read-only | `~/hogwarts` |
| `claude-agents/snape.md` | Snape's user-level agent file | `~/.claude/agents/snape.md`, only if absent |
| `install.sh` | Installs the two homes, creates the database, registers the desks and runs the tests | |
| `scripts/owlpost-setup.sh` | Switches on the Owl Post and sends a test owl (onboarding stage 3.2) | |
| `scripts/patrol-setup.sh` | Switches on the patrol in shadow mode (onboarding stage 5.2). It checks gh, runs one Map round, a backup and a restore drill, enables Ron, and loads the Map, morning lineup, keeper's watch, scoreboard and Gringotts jobs | |
| `scripts/portrait-setup.sh` | Switches on Dumbledore (onboarding stage 5.3). It checks his dry run and the export, enables his desk and loads his weekday job. Auto-portrait stays off | |
| `scripts/codex-boundary-test.sh` | Proves the Codex desks' permission profile on your Mac: no office, no folder it isn't given, no network. Run it before enabling Harry or Moody, and after every Codex upgrade | |
| `scripts/codex-exec-boundary-test.py` | Proves the boundary again under real `codex exec` runs for Harry and Moody, launched the way the desk launcher launches them. It checks the same folders and network, the temp folders, a `node_modules` link to a folder outside the task, that Xcode's Python and git run cleanly, and that none of your own Codex hooks or MCP servers start. Sends a short prompt to OpenAI and costs a few cents per desk | |
| `scripts/installed-office-check.sh` | Installs the kit into a throwaway home, fills the GitHub account, watched repos, blocked models and MCP names with made-up values, and runs both suites there. A test that leans on your own private values fails. Never touches your real `~/.hogwarts` or `~/hogwarts` | |
| `uninstall.sh` | A dry run by default. With `--yes` it removes the fleet and keeps an archive | |

`install.sh` never touches `~/.claude/settings.json`, `~/.codex` or launchd. Those changes are yours to apply, and the onboarding guide shows how.

## Status and known limits

Two test suites cover the kit: `tests` for the store and `tests_fleet` for the fleet. Both run on the system Python, and `install.sh` runs them on every install.

- The review loop is tested against temporary git repos.
- The closer is tested against a faked GitHub and temporary git repos. It reads GitHub only through the patrol's read-only queries and writes nothing to it.
- The worktree cleanup is tested against temporary git repos: worktrees with uncommitted changes, with ignored files, with commits that are neither pushed nor merged, ones it can't read and ones in use are never removed, and a removal killed at any step, from just before the closer's close commits, is finished on a later round or reported, never left half done in silence or told twice. It runs only `git worktree remove`, never with `--force`, never `git worktree prune`, and never touches a branch.
- The patrol is tested against a faked GitHub and faked desks. It starts in shadow mode: while `~/.hogwarts/patrol/shadow` exists, the patrol and Gringotts only write files under the office, and none of their findings reach you. The closer is not held by shadow mode: while `~/.hogwarts/auto-close` holds `on`, it closes tasks and tells you either way.
- PR follow-ups are tested against a faked GitHub. They follow only PRs the review loop opened, and only while `~/.hogwarts/pr-followup` holds `on` and the patrol is out of shadow mode. In shadow mode with the switch on, each Map round only writes what it would have routed, under `~/.hogwarts/patrol/followup/`.
- The weeknight memory review by Dumbledore - Knowledge Manager is tested against temporary stores. He only proposes changes, and nothing applies until you run `castle portrait apply`, unless you switch on auto-portrait with `echo on > ~/.hogwarts/auto-portrait` (`rm ~/.hogwarts/auto-portrait` switches it off). It applies only his additions and never removes anything: every retire, edit and archive move waits for you, and so does an addition the store refused. Each night with a patch ends in one row for you that says what applied and what waits, with the command for the rest. If the job is killed, the next weeknight job finishes that night from the copy it stored, or tells you once that it was cut off, and nothing applies twice. The [handbook](docs/HANDBOOK.md#daily-rhythm) has the details.
- The Codex desks' read boundary rests on Codex permission profiles, which Codex marks as beta. Rerun `scripts/codex-boundary-test.sh` after every Codex upgrade.
- The fleet runs on macOS only, for one person on one Mac.

Tested with Claude Code 2.1.274, Codex 0.160.0 and herdr 0.9.3. [DESIGN.md](docs/DESIGN.md#known-limits) lists every known limit.
