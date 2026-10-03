# The design

Seven single-purpose agents, named after the Harry Potter characters who fit each job. A reviewer from the other model family checks every change before it leaves the laptop. Plain scripts do the patrolling. Memory gets tidied every night. Most of the token saving comes from shorter sessions. The human stays the Headmaster.

## The short version

- **One desk, one job.** Seven agents, each with one role and one task at a time. A single-threaded agent is easier to reason about than one juggling three conversations.
- **No agent pushes without a review from the other model family.** When Codex writes code, Claude reviews it. When Claude writes code, including in your own sessions, Codex reviews it. The pass is tied to the exact commit, and a push gate checks for it.
- **The controls sit where no agent can change them.** The store, review passes, close tokens, hooks and desk settings live in a folder no desk can write and Claude desks can't read. A desk can only post to its own outbox, so it can't pretend to be another desk.
- **Scripts patrol and models only judge.** Plain scripts check PRs and CI and move messages at zero tokens. Haiku wakes only when something changed. Opus is kept for review and the nightly memory pass.
- **Memory knows when a fact changes.** Replacing a fact closes the old one and keeps it with its dates. Normal reads see only current facts. Volatile state like PR status carries a live lookup or a short expiry.
- **Shorter sessions before anything clever.** The biggest saving is restarting at about 200k context instead of running to 400k or more.
- **You stay the Headmaster.** Merges, deploys, credentials, prod changes, security config and anything sent to another person always come back to you.

## Five risks, and what closes each one

These are the ways a fleet of agents running side by side goes wrong. Each fix is enforced by something an agent can't talk its way past: the OS sandbox, launch flags, the store, or you.

| # | The risk | What could happen | How the fleet closes it | Enforced by |
| --- | --- | --- | --- | --- |
| 1 | Any terminal pane can drive any other pane | In a multiplexer like herdr, any pane can type into, prompt or close any other. A hijacked builder could press Enter on a reviewer's dialog or type a close command into your chat. | No desk runs inside herdr. Headless desks run under the Bash sandbox, which blocks Unix sockets, so they can't reach herdr's socket. McGonagall runs in the Code tab, where no agent can type. Approvals come only from your own typing. | OS sandbox |
| 2 | Desks inherit your broad allow list | An agent started the normal way inherits its user's allow list. A prompt injection could run code, send data or push with no prompt. | Every headless Claude desk starts with `--restricted`, which ignores user, project and local settings, plus its own settings file: sandbox on, a minimal allow list, explicit denies and `disableBypassPermissionsMode`. Codex desks run with `--ignore-user-config`, `--ignore-rules`, a fleet-owned profile and an explicit `--sandbox read-only` or `workspace-write`, never a bypass flag. | Launch flags, desk settings |
| 3 | A watchdog that compacts every session | A watchdog that sends `/compact` to every session on a timer interrupts your own sessions as well as the fleet's. | Nothing in the fleet types into a session. Tempus reads real token usage and only warns. Headless runs are bounded by time and budget. Automation acts only on registered desks, by exact id. | Design, desk registry |
| 4 | Identity is a flag anyone can fake | With a `--from` flag, a builder could send "review passed" as the reviewer, read another desk's mail, or close its own task. | Desks never call the store. Each desk writes only to its own outbox. The Owl Post, run by launchd outside any sandbox, stamps the sender from the folder it read. Review families come from the desk registry. Closing a task needs a one-time token that only you can mint: the hook when you type "Mischief managed" in your own session, or `castle token mint` in your terminal. | Filesystem sandbox, the store |
| 5 | Code loads from folders agents can write | Fleet scripts that import code from a folder agents write to, or pick binaries from environment variables, let any desk that writes there run code inside every other desk. | The store, hooks, scripts, briefs and desk settings live in `~/.hogwarts`. No desk can write there, and Claude desks can't read it. The store runs only through `bin/castle`, which clears the environment, runs the system Python in isolated mode with no bytecode cache, reads no environment variables, and refuses a symlinked or group-writable database. | Sandbox, permission rules, store code |

## Who can touch what

| Desk | Store and settings | Its own desk folder | Other desks | Task worktree | Network from the shell | Message a person |
| --- | --- | --- | --- | --- | --- | --- |
| McGonagall, interactive | No | Write, plus tasks and PLAN.md | No | Read | Your settings | Drafts only |
| Harry, Codex | No | Write outbox | No | Write, his task only | Off | No |
| Hermione, reviewer | No | Write | No | Read | Off | No |
| Moody, reviewer | No | None, the script keeps his output | No | Read | Off | No |
| Ron, Snape, the portrait | No | Write | No | No | Named hosts only | No |
| Owl Post and Map scripts | Read and write | Read outboxes, deliver inboxes | No | No | GitHub API | Only under a standing order |
| You | Everything | Everything | Everything | Everything | Yes | Yes, and only you merge, deploy or close a task |

The Bash sandbox covers shell commands and everything they start. Each desk's sandbox denies reads and writes on `~/.hogwarts`. File tools and MCP tools sit outside the sandbox, so permission rules deny Read, Edit and Write there too, and `--strict-mcp-config` limits each headless desk to the MCP servers its job needs. Snape is a subagent, so his protection is his explicit tool list. The push gate covers agent pushes. Pushes you make by hand stay yours.

## The staff

| Desk | Model | Job | Never |
| --- | --- | --- | --- |
| McGonagall - Chief of Staff | Claude Opus, interactive | Turns your ask into TASK.md (your words verbatim, numbered acceptance criteria, out of scope), keeps PLAN.md, routes work to one desk at a time, drafts chat replies | Writes or reviews code, sends anything, closes a task |
| Harry - Senior Engineer | Codex, workspace-write | One task in its own git worktree: code, a failing-first test for each bug fix, local commits, a handoff note and a PR body draft | Pushes, opens a PR, uses git stash, weakens a test, folds in a second fix |
| Hermione - Staff Engineer | Claude Opus, headless | Reviews Codex-written diffs against Intent and evidence. Triages PR review comments and drafts replies | Edits code, approves on GitHub, reviews Claude-written code |
| Moody - Security Reviewer | Codex, read-only | Reviews Claude-written diffs, including yours, security first | Writes anything, accepts a summary instead of the diff |
| Ron - Release Engineer | Claude Haiku, headless | Sorts PR and CI changes as routine or for you, calls reds real, flaky, infra or unsure, writes the morning lineup and weekly scoreboard from script numbers | Retries or unblocks a build, computes a number himself |
| Snape - Data Analyst | Claude Sonnet, subagent | Read-only warehouse and observability reads with provenance on every number | Writes anywhere, prints raw rows or PII |
| Dumbledore - Knowledge Manager | Claude Opus, headless | Reviews the day each weeknight and proposes memory and brief changes as typed operations | Applies his own patch, deletes memory |

Three scripts use no model. The **Owl Post - Message Router** moves owls between desks and stamps the sender. The **Marauder's Map - PR Watcher** diffs PR and CI state every 15 minutes on weekdays and wakes Ron only on change. **Gringotts - Backup** takes a nightly local backup with credentials left out.

## How a task moves

1. **Ask.** You tell McGonagall what you want. Small things she answers directly.
2. **Ticket.** She writes TASK.md with your words under Intent, plus numbered acceptance criteria and their checks. You say go, and Intent is frozen. You register it with one `castle task create` command.
3. **Build.** A script makes a fresh worktree. Harry implements, commits locally and writes a handoff note.
4. **Evidence.** A verify script runs every acceptance check and records the command, exit code and output for that commit.
5. **Cross-model review.** Codex-written work goes to Hermione and Claude-written work goes to Moody. The review script records the verdict from the reviewer's own output. The store counts a pass only when the families differ, and any new commit voids it.
6. **Push and PR.** A hook asks the store for a pass on HEAD and blocks any agent's `git push` without one. Opening a ready PR waits for your yes, because it notifies people.
7. **Patrol.** Ron watches CI and files bot comments. Hermione reproduces or rebuts each one.
8. **Merge.** You merge and deploy, then type "Mischief managed <task-id>".

## The timetable

| Job | Owner | When | Model |
| --- | --- | --- | --- |
| Owl Post | Script | Whenever an outbox changes, plus a sweep every 5 minutes | None |
| Marauder's Map rounds | Script, then Ron | Every 15 minutes, weekdays 08:00 to 19:00 | None, Haiku on change |
| Morning lineup | Ron | Weekdays 08:30 | Haiku |
| Keeper's watch | Ron | 09:00, 13:00 and 17:00 on weekdays | None when green, Haiku on red |
| The Pensieve review | The portrait | Weekdays 22:30 | Opus |
| Gringotts | Script | Daily 23:30 | None |

The launchd templates for all six sit in `office/launchd/`. Only the Owl Post's module exists today.

## Two homes and the memory

- **The office, `~/.hogwarts`.** The store package, `bin/castle`, the database at `state/pensieve.db`, the fleet scripts and hooks, each desk's brief and settings, launchd templates, pending settings snippets and the avatars. No desk can read or write it.
- **The castle, `~/hogwarts`.** The charter (`CLAUDE.md`), `PLAN.md`, `standing-orders.md`, a folder per desk with `scratchpad.md`, `inbox/` and `outbox/`, `tasks/<id>/` and `worktrees/`. It is a local git repo with no remote.
- **The Pensieve.** One SQLite file with full-text search. A SessionEnd hook stores a capped extract of each castle session at zero tokens: your prompts and the final replies, never tool output, scrubbed of emails, IPs, tokens and long hashes.
- **Facts know when they change.** Each fact can carry a subject key, and only one fact per key is current. Replacing a fact closes the old one and keeps its dates. You can ask what was true, or what the fleet believed, on any past date. A fact that looks like PR status, build colour or a rollout percentage is refused unless it carries the command that fetches the live value, or expires within a week.
- **Budgets.** The charter at about 600 tokens, the fleet memory index at 4KB, scratchpads at 6KB, and the startup digest under 40 lines. Nothing is deleted. Stale facts are closed or archived. Only raw extracts (after 90 days) and owl bodies (after 30) are purged.

## The store

Standard-library Python that runs on the Mac's built-in Python 3.9, with a CLI called `castle` that prints JSON. Nothing a desk runs can call it. Only your terminal and the fleet's scripts and hooks use it.

| What it holds | What it guarantees |
| --- | --- |
| Desks and tasks | One active task per desk. A closed task never reopens. Closing as complete needs a hashed, single-use, expiring token only you can mint. |
| Owls | A duplicate send collapses into one. Reading is separate from acknowledging. One answer per question, and a result only from the desk that was asked. |
| Requests | Phases only move forward. A desk can defer or decline with a reason. |
| Review passes | A pass counts only for that exact commit, only when it is registered on the author's task, and only when the reviewer's family differs from the author's. |
| Facts | One current fact per subject. Volatile facts need a live lookup or a short expiry. Nightly changes arrive as typed operations applied all or nothing. |

The full contract is in `office/README.md`. 380 store tests and 119 fleet tests pass on the system Python. They include checks that no code reads environment variables and that hostile ids and paths are refused at every entry point.

## Spending fewer tokens

An example baseline, from one person's last 80 Claude Code sessions: a fresh desktop session started at about 88k tokens before anything was typed, and the median call carried about 435k of context over 30 days. 196 tools from four connectors loaded into every session and none were called. Bash output came to 14.8M tokens in 30 days. Re-measure your own before you trust these.

- **Tempus.** One task per session. At about 200k context, write a Checkpoint and start fresh. A hook prints one line once a session passes 200k.
- **Lean memory.** Keep the memory index small and archive older entries.
- **Idle connectors off.** Switch off connectors and MCP servers you never call.
- **Per-desk tool sets.** Headless desks load only what their job needs, with `--strict-mcp-config`, `--tools` and a budget cap.
- **RTK, carefully.** RTK compresses Bash output. Measure with `rtk discover` before adding its hook. When you do, use `--hook-only`, exclude review-critical commands such as `git diff`, `git show`, `git push` and `gh pr`, turn off its recall store, and keep it off the Codex desks. Reviewers read diffs with `RTK_DISABLED=1`.

## What always comes back to you

- Merges, even green ones. No auto-merge, and no standing order can contain a merge.
- Deploys and pipeline gates: rollbacks, retries, rebuilds, unblocks and flag changes.
- Anything that changes prod.
- Credentials and logins. No desk ever enters, reads or prints one.
- Security and config: permissions, hooks, settings, MCP, plugins and sandbox. Desks propose a diff and you apply it.
- Installs. Homebrew only, after you've read `brew info`.
- Anything sent to a person: chat, email, tickets, PR threads, review requests and opening a ready PR.
- Public repo text: branch names, commits and PR text before the first push. Character names never leave the fleet.
- Scope: editing Intent, adding criteria, splitting a PR.
- Force pushes and deletions, including branches, PRs and memory.

The only pre-approvals are the ones you write in `standing-orders.md`.

## Rollout

| Stage | What | Done when |
| --- | --- | --- |
| 0 Clean up | Prerequisites, sign-ins, deny rules for your own sessions, idle connectors off | Your own settings reviewed and the office denied to your sessions |
| 1 The front desk | The castle, the Owl Post, McGonagall, Snape and the hooks | An owl round trip works, a forged sender is refused, a second task for one desk is refused |
| 2 Review loop | Worktree, verify, review and push scripts, the push gate, Harry, Hermione and Moody | Three PRs went out with passes tied to their commits, and the gate blocked a push with no pass |
| 3 Shadow | The Map and Ron's jobs writing to files only, Hermione's bot pass in draft mode, Gringotts with a restore drill | The morning lineup matched `gh` three days running |
| 4 Memory loop | The nightly export and the portrait in proposals-only mode, the weekly scoreboard, an RTK number | Two nightly patches reviewed and an RTK go or no-go |
| 5 After that | The RTK hook and standing orders, if the numbers say so | A normal week where you only answered what needed you |

Stages 0 and 1 are built and installed by this repo. Stages 2 to 5 need code that is not written yet. [ONBOARDING.md](ONBOARDING.md) stage 5 says what each one needs.

## Known limits

These are honest gaps in what ships today.

- **Codex desks rely on a beta feature for their read boundary.** On Codex 0.160.0 the `--sandbox` modes let commands read the whole disk. So `run_desk` never passes `--sandbox` to Harry or Moody. It passes a fleet permission profile instead: an allowlist with the desk's working folder, its own castle folder, the castle tasks and its repo's `.git` folder, no network, and `~/.hogwarts` denied by name. `scripts/codex-boundary-test.sh` proves that kind of profile through `codex sandbox`, with no model and no tokens. Permission profiles are marked beta, so rerun that test after every Codex upgrade, and confirm it once under a real `codex exec` run before enabling Harry.
- **No hook field says a person typed the prompt.** Claude Code's hook input does not separate an interactive session from `claude -p`. So the close hook also checks the session transcript: every entry must name the `cli` or `claude-desktop` entrypoint, and the prompt's own entry must be a typed human prompt from the last 30 seconds. If it can't confirm that, it refuses and prints the `castle` commands to close the task from your terminal.
- **McGonagall asks you each time.** Her outbox writes and TASK.md edits are on the castle's ask list, so Claude asks you before every owl she posts and every TASK.md she writes. That is deliberate friction, and you can't pre-approve it from inside a session.
- **Later stages are not built.** The worktree, verify and review scripts, the push gate, the Marauder's Map, Ron's jobs, the portrait's nightly export and Gringotts do not exist yet. Their launchd templates and the `a2` push gate snippet are placeholders for those stages.
- **Deny rules for Bash match the usual command form only.** They are not a wall around a program. The sandbox stays the real boundary for desks.
- **The push gate only covers agent pushes.** Pushes you make by hand from a terminal stay yours.

## Where the ideas came from

The fleet borrows from a teammate's own agent fleet, from firstmate (one front agent that never does project work, bounded extracts, a digest ordered for truncation, durable requests with a doorbell), from RTK, and from Graphiti's temporal facts. Graphiti itself was left out: it keeps replaced facts in default search results, costs several model calls per write, and needs a graph database. SQLite with subject keys, validity windows and current-only reads gives the useful part at zero tokens, with nothing leaving the Mac.
