# Hogwarts fleet

A Harry Potter themed fleet of single-purpose AI agents for Claude Code and Codex. Seven desks each do one job. A desk runs one model process at a time, but Harry, Hermione, Moody, Ron and your own sessions each keep many tasks in flight, so a task waiting for fixes blocks nothing. Four plain scripts move messages, watch PRs, take backups and keep each desk on the model its job needs, all at zero tokens. A reviewer from the other model family checks every change before it leaves the laptop. You are the Headmaster: merges, deploys, credentials, settings and anything sent to a person always come back to you. Everything that controls the fleet lives in a folder no desk can write.

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

**What ships today:** the store and its `castle` CLI, the castle, McGonagall, Snape, the Owl Post and the session hooks, and the review loop: the `fleet` command's worktree, verify, review and push scripts and the push gate. Also built: busy-day caps on runs and spend, review rounds that stop at three, Ollivander - Model Keeper, and a read-only live view of every desk. The store and fleet test suites cover it. The review loop is tested on temp repos but not yet on real tasks. The PR patrol and the nightly memory review are designed, and their desks, briefs, settings and launchd templates are here, but the scripts that drive them come in later stages. [DESIGN.md](docs/DESIGN.md#known-limits) lists the gaps.

## Start here

**[docs/ONBOARDING.md](docs/ONBOARDING.md)** takes a fresh Mac to a working fleet, one stage at a time. Each stage ends with a check that tells you it worked.

- [Handbook](docs/HANDBOOK.md): daily use, which desk to ask, cheat sheet and troubleshooting. There is also a [standalone HTML copy](docs/handbook.html) you can open in a browser.
- [Design](docs/DESIGN.md): how the fleet works and the five risks it closes.
- [Customise](docs/CUSTOMISE.md): change a desk's role card, pin a model, block models, add or retire a desk, change caps, budgets and schedules.
- [Uninstall](docs/UNINSTALL.md): take it all off again, by script or by hand.
- [Setup prompt](prompts/setup-prompt.md): let a fresh Claude Code session walk you through onboarding.

## At a glance

**The desks.** McGonagall - Chief of Staff, Harry - Senior Engineer, Hermione - Staff Engineer, Moody - Security Reviewer, Ron - Release Engineer, Snape - Data Analyst and Dumbledore - Knowledge Manager. The name after the dash is the job.

**The scripts.** Owl Post - Message Router and Ollivander - Model Keeper (built), Marauder's Map - PR Watcher and Gringotts - Backup (later stages). None of them uses a model. Ollivander reads each desk's role card and keeps the desk on the newest model of the tier its job needs, inside its own model family.

**Busy days and the live view.** Every headless desk has a daily run cap, and the Claude desks a spend cap too. They guard against runaway loops, they don't ration a normal day, and they reset at local midnight. A desk still runs one owl at a time, while its other tasks wait without blocking it, and `fleet feed` plus one herdr space per desk lets you watch them work without typing into anything.

**The Headmaster gate.** You. No desk merges, deploys, signs in, changes settings or sends anything to a person. A task closes only when you type `Mischief managed <task-id>`.

**Two homes.**

- `~/.hogwarts` is the office. It holds the store (a SQLite database and the `castle` CLI), the hooks, the scripts, each desk's brief and locked-down settings, and the launchd templates. No desk can write here, and Claude desks cannot read it.
- `~/hogwarts` is the castle, where desks work. It holds the charter, the plan, each desk's scratchpad, task pads, inbox and outbox, the task folders and the git worktrees.

**Five risks it closes.**

1. One terminal pane driving another. No desk that can run commands runs inside a terminal multiplexer. The only live sessions there are McGonagall and Snape, who have no shell tool, which is checked before each one opens, and every other desk gets a read-only feed.
2. Desks inheriting your broad allow list. Every headless desk starts restricted, with its own settings.
3. A watchdog typing into sessions. Nothing types into a session. Warnings only.
4. Faked identity. A desk can only post to its own outbox, and the Owl Post stamps the sender from the folder.
5. Code loading from folders agents can write. The controls live in the office, and the store runs on the system Python with a cleared environment.

**Requirements.** macOS, Homebrew, the Claude Code CLI, the Codex CLI, the system `/usr/bin/python3` 3.9 or newer, SQLite with FTS5 (the macOS build has it), `jq`, `git`, `gh`, `ripgrep` and `shellcheck`. RTK is optional.

## What is in this repo

| Path | What it is | Installed to |
| --- | --- | --- |
| `office/` | The store, `bin/castle`, `bin/fleet`, `bin/hogwarts-spaces`, the fleet scripts and hooks, both test suites, desk briefs, role cards and settings, launchd templates, pending settings snippets and the avatars | `~/.hogwarts` |
| `castle/` | The charter, an empty plan, standing orders, desk folders, McGonagall's settings and agent file, and a Codex config that keeps any Codex session opened there read-only | `~/hogwarts` |
| `claude-agents/snape.md` | Snape's user-level agent file | `~/.claude/agents/snape.md`, only if absent |
| `install.sh` | Installs the two homes, creates the database, registers the desks and runs the tests | |
| `scripts/owlpost-setup.sh` | Your one command to switch on the Owl Post and send a test owl (onboarding stage 3) | |
| `scripts/codex-boundary-test.sh` | Proves the Codex desks' permission profile on your Mac: no office, no folder it isn't given, no network. Run it before enabling Harry or Moody, and after every Codex upgrade | |
| `scripts/codex-exec-boundary-test.py` | Proves the boundary again under real `codex exec` runs for Harry and Moody, launched exactly the way the desk launcher does: the folders, temp folders and network above, a borrowed folder, quiet Xcode tools, and none of your own Codex hooks or MCP servers. Sends a short prompt to OpenAI and costs a few cents | |
| `uninstall.sh` | A dry run by default. With `--yes` it removes the fleet and keeps an archive | |

`install.sh` never touches `~/.claude/settings.json`, `~/.codex` or launchd. Those changes are yours to apply, and the onboarding guide shows how.
