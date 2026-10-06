# The design

Seven single-purpose agents, named after the Harry Potter characters who fit each job. A reviewer from the other model family checks every change before it leaves your Mac. Plain scripts do the patrolling and keep each desk on the right model. Memory gets tidied every weeknight. Most of the token saving comes from shorter sessions. You stay the Headmaster.

## The short version

- **One desk, one job.** Seven agents, each with one role: McGonagall - Chief of Staff, Harry - Senior Engineer, Hermione - Staff Engineer, Moody - Security Reviewer, Ron - Release Engineer, Snape - Data Analyst and Dumbledore - Knowledge Manager.
- **Short runs, many tasks.** A desk runs one model process at a time, except the two reviewers, Moody and Hermione, which can run two reviews of different tasks at once. Each run is short and single-threaded. Harry, Hermione, Moody, Ron and your own sessions can each keep many tasks in flight between runs, so a task waiting for fixes never blocks another. McGonagall, Snape and Dumbledore keep one task at a time.
- **No agent pushes without a review from the other model family.** When Codex writes code, Claude reviews it. When Claude writes code, including in your own sessions, Codex reviews it. The pass is tied to the exact commit, and a push gate checks for it. A teammate's later review comments go through the same loop.
- **The controls sit where no agent can change them.** The store, review passes, close tokens, hooks and desk settings live in a folder no desk can write and Claude desks can't read. A desk can only post to its own outbox, so it can't pretend to be another desk.
- **Scripts patrol and models only judge.** Plain scripts check PRs and CI and move messages at zero tokens. A fast model wakes only when a change may need you. A frontier model is kept for review and the nightly memory pass.
- **Desks ask for a tier, not a model.** Each desk's role card says what the job needs, and Ollivander - Model Keeper, a script, picks the model. A desk never changes model family, so the review rule holds.
- **Caps guard against runaways.** Every headless desk has a daily run cap, and the headless Claude desks a spend cap. They're sized for a busy day, so they only bite when something loops.
- **Memory knows when a fact changes.** Replacing a fact closes the old one and keeps it with its dates. Normal reads see only current facts. Volatile state like PR status carries a live lookup or a short expiry.
- **Shorter sessions before anything clever.** The biggest saving is restarting at about 200k context instead of letting a session run to twice that or more.
- **You stay the Headmaster.** Merges, deploys, credentials, prod changes, security config and anything sent to another person always come back to you.

## Five risks, and what closes each one

These are the ways a fleet of agents running side by side goes wrong. Each fix is enforced by something an agent can't talk its way past: the OS sandbox, launch flags, the store, or you.

| # | The risk | What could happen | How the fleet closes it | Enforced by |
| --- | --- | --- | --- | --- |
| 1 | Any terminal pane can drive any other pane | In a multiplexer like herdr, any pane can type into, prompt or close any other. A hijacked builder could press Enter on a reviewer's dialog or type a close command into your chat. | No desk that can run commands runs inside herdr. Headless desks run under the Bash sandbox, which blocks Unix sockets, so they can't reach herdr's socket. The only live sessions in herdr are McGonagall and Snape, who have no shell tool. `hogwarts-spaces` checks their agent definitions before opening them, and starts each with a fixed built-in tool list, every command-running tool denied and skills off. Every other desk gets a read-only feed there. Approvals come only from your own typing. | OS sandbox, tool lists, the spaces check |
| 2 | Desks inherit your broad allow list | An agent started the normal way inherits its user's allow list. A prompt injection could run code, send data or push with no prompt. | Every headless Claude desk starts with `--restricted`, which ignores user, project and local settings, plus its own settings file: sandbox on, a minimal allow list, explicit denies and `disableBypassPermissionsMode`. Codex desks run with `--ignore-user-config`, `--ignore-rules` and `--ephemeral`, plus a fleet permission profile passed as `-c` overrides: an allowlist of folders, with no network. They never get `--sandbox` or a bypass flag. | Launch flags, desk settings |
| 3 | A watchdog that compacts every session | A watchdog that sends `/compact` to every session on a timer interrupts your own sessions as well as the fleet's. | Nothing in the fleet types into a session. Tempus, the fleet's context hook, reads real token usage and only warns. Headless runs are bounded by time and budget. Automation acts only on registered desks, by exact id. | Design, desk registry |
| 4 | Identity is a flag anyone can fake | With a `--from` flag, a builder could send "review passed" as the reviewer, read another desk's mail, or close its own task. | Desks never call the store. Each desk writes only to its own outbox. The Owl Post, run by launchd outside any sandbox, stamps the sender from the folder it read. Review families come from the desk registry. Closing a task needs a one-time token that only you can mint: the hook when you type "Mischief managed" in your own session, or `castle token mint` in your terminal. The one other way is a proven close, which the closer records only while you have switched auto-close on, through a store call nothing a desk or the `castle` command can reach. | Filesystem sandbox, the store |
| 5 | Code loads from folders agents can write | Fleet scripts that import code from a folder agents write to, or pick binaries from environment variables, let any desk that writes there run code inside every other desk. | The store, hooks, scripts, briefs and desk settings live in `~/.hogwarts`. No desk can write there, and Claude desks can't read it. The store runs only through `bin/castle`, which clears the environment, runs the system Python in isolated mode with no bytecode cache, reads no environment variables, and refuses a symlinked or group-writable database. | Sandbox, permission rules, store code |

## Who can touch what

| Desk | Store and settings | Its own desk folder | Other desks | Task worktree | Network from the shell | Message a person |
| --- | --- | --- | --- | --- | --- | --- |
| McGonagall - Chief of Staff | No | Write, plus tasks and PLAN.md | No | Read | Your settings | Drafts only |
| Harry - Senior Engineer | No | Write outbox | No | Write, his task only | Off | No |
| Hermione - Staff Engineer | No | Write | No | Read | Off | No |
| Moody - Security Reviewer | No | None, the script keeps his output | No | Read | Off | No |
| Ron - Release Engineer | No | Write | No | No | `api.github.com` only | No |
| Snape - Data Analyst | No | Write | No | No | No shell tool. He reaches only his read-only MCP servers | No |
| Dumbledore - Knowledge Manager | No | Write | No | No | Off | No |
| The Owl Post and the Map (scripts) | Read and write | Read outboxes, deliver inboxes | No | No | GitHub API | Only under a standing order, or the follow-up replies you switch on |
| Ollivander - Model Keeper (script) | Read and write his model rows, and read each role card | None | No | No | The CLIs he asks, and updates only if you switch them on | No. He files notes for you |
| You | Everything | Everything | Everything | Everything | Yes | Yes, and only you merge, deploy or close a task |

The Bash sandbox covers shell commands and everything they start. Each desk's sandbox denies reads and writes on `~/.hogwarts`. File tools and MCP tools sit outside the sandbox, so permission rules deny Read, Edit and Write there too, and `--strict-mcp-config` limits each headless desk to the MCP servers its job needs. Snape is a subagent, so his protection is his explicit tool list. The push gate covers agent pushes. Pushes you make by hand stay yours.

## The staff

| Desk | Model | Job | Never |
| --- | --- | --- | --- |
| McGonagall - Chief of Staff | Claude, frontier tier, interactive | Turns your ask into TASK.md (your words verbatim, numbered acceptance criteria, out of scope), keeps PLAN.md, routes work to one desk at a time, drafts chat replies | Writes or reviews code, sends anything, closes a task |
| Harry - Senior Engineer | Codex, workhorse tier, headless | Each task in its own git worktree, one run at a time: code, a failing-first test for each bug fix, local commits, a handoff note and a PR body draft | Pushes, opens a PR, uses git stash, weakens a test, folds in a second fix |
| Hermione - Staff Engineer | Claude, frontier tier, headless | Reviews Codex-written diffs against Intent and evidence. Triages PR review comments and drafts replies | Edits code, approves on GitHub, reviews Claude-written code |
| Moody - Security Reviewer | Codex, frontier tier, read-only | Reviews Claude-written diffs, including yours, security first | Writes anything, accepts a summary instead of the diff |
| Ron - Release Engineer | Claude, fast tier, headless | Sorts PR and CI changes as routine or for you, calls reds real, flaky, infra or unsure, writes the morning lineup and weekly scoreboard from script numbers | Retries or unblocks a build, computes a number himself |
| Snape - Data Analyst | Claude, workhorse tier, subagent | Read-only warehouse and observability reads with provenance on every number | Writes anywhere, prints raw rows or PII |
| Dumbledore - Knowledge Manager | Claude, frontier tier, headless | Reviews the day each weeknight and proposes fact and memory changes as a dated patch of typed operations, each with its reason and source, plus a ten-line morning note | Applies his own patch (the office applies his additions only while you switch that on), touches the store, deletes memory |

The tier comes from each desk's role card, and Ollivander turns it into a model, as in [Models by role](#models-by-role).

Four scripts use no model. The **Owl Post - Message Router** moves owls between desks and stamps the sender. The **Marauder's Map - PR Watcher** diffs PR and CI state every 15 minutes on weekdays and wakes Ron only for a change that may need you. **Gringotts - Backup** takes a nightly local backup with credentials left out. **Ollivander - Model Keeper** reads each desk's role card every morning and keeps the desk on the model it needs.

## How a task moves

1. **Ask.** You tell McGonagall what you want. Small things she answers directly.
2. **Ticket.** She writes TASK.md with your words under Intent, plus numbered acceptance criteria and their checks, and a Spec that opens with the repo, a new branch and its base. You ask for changes until you're happy, then type `go <task-id>` in her session, and Intent is frozen. A hook checks the prompt is in McGonagall's session and that you typed it yourself, the same way it checks "Mischief managed", then registers the task, stores the repo, branch, base and the TASK.md's hash with it, and routes it to Harry. Editing TASK.md after that changes none of them. A go in any other session, your own included, changes nothing. If the hook can't confirm your typing, nothing happens and you register the task with one `castle task create` command.
3. **Build.** The same go makes a fresh worktree through the worktree script, with every check it makes by hand, based on the commit its base names at that moment, so a later merge to main never changes what a review compares against. Then Harry's run starts. Harry implements, checks his change against the four kinds of problem reviews keep finding, and writes a handoff note.
4. **Evidence.** A verify script runs every acceptance check and records the command, exit code and output for that commit. Each check runs in its own process group, which ends with it, and holds the task's review lock while it runs. Its output is read back only through the file descriptor verify made it with, never by its name, and is normalized and scrubbed of anything shaped like a credential before it's cut to its last lines. A criterion labelled `after merge:` is listed but never run before the merge. verify also keeps the exact TASK.md it read in the office, and each review round records its digest.
5. **Cross-model review.** Codex-written work goes to Hermione and Claude-written work goes to Moody. A reviewer who finds one instance of a problem checks its siblings and lists them all, so one fix round covers the whole kind. The review script records the verdict from the reviewer's own output. The store counts a pass only when the families differ, and any new commit voids it. For Harry's work this is a loop that runs by itself: his handoff starts the review, CHANGES starts his fix round, and it stops at the round cap, on PASS or on HEADMASTER, each time telling you.
6. **Push and PR.** A hook asks the store for a pass on HEAD and blocks any agent's `git push` without one. You push with `fleet push`, unless you opt in with one file in the office: then the review loop pushes the reviewed commit after its PASS and opens a draft PR from Harry's commit message and PR body draft. Opening a ready PR waits for your yes, because it notifies people.
7. **Patrol.** Ron watches CI and files bot comments. Hermione reproduces or rebuts each one. With `~/.hogwarts/pr-followup` on and the patrol out of shadow mode, comments from people with write access on a PR the loop opened go back to Harry as a follow-up. His fixes and his one-line replies get the usual cross-family review. On PASS the loop pushes the reviewed commit to the same branch and posts each reply once. Threads are never resolved, and the PR is never marked ready or merged.
8. **Merge and close.** You merge and deploy, then type "Mischief managed <task-id>". Or switch on auto-close once, with one file in the office (`echo on > ~/.hogwarts/auto-close`). Then each Map round starts the closer, which checks that the TASK.md the passing round read is the one you approved, proves from GitHub and git that the reviewed commit landed, reads CI on the merge commit, runs every after-merge command in a fresh worktree at the merge commit, and has the reviewer of the other family judge every written after-merge check from a scrubbed pack. Only then does it close the task, and McGonagall's go task with it when nothing else is open under it, with one row in your digest naming what proved each check. Anything it can't prove stops that task with one row, and `fleet close <task-id>` tries once more. It writes nothing to GitHub. A task whose PR follow-up is still open is left alone until that follow-up ends, and the store refuses a proven close of it.

## The timetable

| Job | Owner | When | Model |
| --- | --- | --- | --- |
| Owl Post | Script | Whenever an outbox changes, plus a sweep every 5 minutes | None |
| Ollivander | Script | Daily 06:00 | None |
| Marauder's Map rounds | Script, then Ron | Every 15 minutes, weekdays 08:00 to 19:00; while follow-ups are on, each round also sends teammates' new comments back to Harry, and while auto-close is on it starts the closer | None, fast tier on change; the closer's judge at the reviewer's tier |
| Morning lineup | Ron | Weekdays 08:30; a missed one is written by the next Map round | Fast tier |
| Keeper's watch | Ron | 09:00, 13:00 and 17:00 on weekdays | None when green, fast tier on red |
| Weekly scoreboard | Script, then Ron | Mondays 09:00 | None for the numbers, fast tier for the words |
| Bot pass | Map round, then Hermione | Once a PR is 15 minutes old, and when new review threads land | Frontier tier, drafts only |
| Nightly memory review | Dumbledore | Weekdays 22:30 | Frontier tier |
| Gringotts | Script | Daily 23:30 | None |

The launchd templates for all eight jobs sit in `office/launchd/`. The patrol jobs and Gringotts start in shadow mode: while `~/.hogwarts/patrol/shadow` is there, they only write files under the office. Delete it when you want their rows to reach you. The nightly memory review's job runs the nightly export first, which uses no model, and then Dumbledore.

## Models by role

A desk asks for what its job needs, never for a model by name. Its role card, `desks/<desk>/role.json` in the office, holds a tier (frontier, workhorse or fast), an effort and one line of why. Ollivander - Model Keeper turns that into a model:

- A Claude desk takes the Claude Code alias for its tier, so it always gets the newest model of that line. A Codex desk takes a model from Codex's own catalog, filed by the wording of its description.
- He never picks a model that is retiring within 30 days, one the catalog calls older or legacy, or one your organization blocks.
- A Claude alias is judged by every full model id a run on it has reported, helper calls included, and a labelled form like `opus[1m]` shares the plain alias's record. Once an alias has run as a blocked model, no switch, pin, approval, revert or launch puts a desk back on it while that model stays blocked.
- A move to the same tier or a cheaper one applies by itself with a note. A costlier one waits for you, because spend is your call. It waits only while each pass still makes it, and approving it checks it again against the latest catalog the store keeps.
- The first two runs after a switch are a trial. Two failures send the desk back to its previous model. A desk's own model always replaces the one a Codex profile in its `codex.toml` names, and a run reads its model together with the switch it came from, so a trial only ever counts the model that ran. Your own switch or pin always stands, even one made mid-trial. A revert pins the desk, so it only lands on a model a pass could still give it: never one you filed as ignore, or one the latest catalog no longer lists, hides or retires soon.
- While any model is blocked, a Codex desk with no model of its own doesn't launch, and no trial reverts onto one, because the Codex CLI default is a model the fleet can't name, so it can't be checked.
- A pass reads the store, records the catalogs it saw and writes its moves in one transaction. A pin or approval you make meanwhile either lands first, and the pass plans around it, or waits for the pass, and then gets checked against the new catalog.

**Why family never changes.** The store counts a review pass only when the reviewer's family differs from the author's. If Ollivander could move Harry from Codex to Claude, Claude would end up reviewing Claude, and the rule that makes the fleet safe to trust would quietly stop holding. So the card's family has to match the registry, a Claude desk only ever holds a Claude model and a Codex desk a Codex one, and the registry never lets a desk's family change.

**Why a script.** Picking by tier is a lookup against two lists, so it needs no model, spends no tokens and can't be talked round by anything a desk says. McGonagall's and Snape's models live in agent files that he only reports on. A CLI update that moves the Codex version stops every headless desk until you've re-proved the sandbox on it. An update and a run never overlap: a run holds a shared lock from its last stop check until its process has exited, and an update holds that lock alone from before it starts until its checks are done. An update that dies part way leaves a marker behind, and the next pass treats it as a failed update and stops the desks.

## Caps are guards, not targets

Every headless desk has a daily cap on runs, and the headless Claude desks a cap on spend. They exist so a loop can't burn a day's budget while you're away. They aren't a quota to spend. The numbers in `fleet/config.py` assume a busy day of a dozen or so PRs plus side work, so a normal day shouldn't reach them.

- The day resets at local midnight, so a bump you make lasts until then and no longer.
- A run counts toward the run cap the moment it starts, so one that gets killed or crashes still counts.
- Spend comes from the cost each run records. A Claude run killed by a timeout or a signal before it reports its cost has none to record. It is charged the most it could have spent, its per-run budget, and a routine `rundesk.spend_unknown` event stored with its usage marks that cost as an estimate. So the spend cap runs high when a cost is lost, never low.
- A desk at 80% of a cap sends one warning. At the cap its next run doesn't start, and its request keeps waiting for the reset or for your `castle desk cap`.
- A cap day resets all at once, so a desk busy on both sides of the reset can use up to two days' cap within hours. There's no rolling 24 hour guard on top.
- Nothing waits in line. A second review of a task that's already being reviewed stops at once and changes nothing.
- A review whose reviewer is busy (other runs hold every one of its run slots) or at its cap is queued. The next review of that same task replaces it, so only a task's newest commit gets reviewed. Reviews of other tasks never replace it.
- The review the Owl Post starts on Harry's handoff holds no place in any line either. While it can't start (Harry's run still going, the reviewer busy, another review of the task running) it ends at once, and the Owl Post starts it again on each pass, for up to four hours, then tells you. What follows its verdict is written down before it starts, so a review killed after its verdict is finished on the next pass without another round. Its review is published first if the kill came before that, an ending you already heard about is left as it is, and a fix round, push or PR it had begun is never started twice: you hear once that it may or may not have happened.
- A task gets three review rounds, and only a round where the reviewer recorded a verdict counts. A fourth waits for `castle task allow-round`.
- A follow-up gets two review rounds of its own, apart from the task's three, and a task takes at most five follow-ups. `castle task allow-round` lifts the cap of whatever is open when you run it: the open follow-up's, or the build's when none is.
- Every stop says which limit it was. The fleet's cap is yours to lift. The Claude or Codex plan's own usage limit isn't, and no bump pretends to lift it.

## Watching without typing

Nothing in the fleet types into a desk's session, and the live view works the same way. `fleet feed` opens the store read-only and reads each run's own output file, so it only ever reads. It cuts every terminal control sequence out of what a desk wrote before printing it, so a desk can't steer your terminal through the feed.

The herdr spaces follow from risk 1. Any herdr pane can type into any other pane, so an agent that can run commands must never live in one. McGonagall and Snape have no shell tool, so they can have live sessions there. Every other desk keeps running short, one owl per run, and its space only runs a feed.

That rests on their agent files, and the install keeps an `~/.claude/agents/snape.md` you've edited. So each time, `hogwarts-spaces` checks every definition Claude could load for them:

- **Tools.** It refuses the space unless there is an explicit `tools:` list in which every tool, built-in or MCP, is named exactly on that agent's trusted list. Only frontmatter keys that start nothing are allowed, so no hooks or MCP servers.
- **The trusted list.** It's an allowlist, `desks/<agent>/live-tools.json` in the office, which no desk or agent can write, because a tool that types into another pane or sends a message can have a name that looks harmless. It's read like every other office file, with no link anywhere on the way, and only when you own it and nobody else can write it, since it's what the live panes trust. The check also refuses a trusted list that names a built-in able to run commands.
- **Parsing.** It reads the frontmatter the way Claude does, so a `---` inside a line or a YAML word like `null` in `tools:` is refused. Any file whose name only a YAML parser could work out counts as a possible definition and gets checked too.
- **Settings.** The castle settings must still name McGonagall.
- **Launch.** The session starts with `--tools` naming its few built-in tools, every command-running tool and Skill denied, and skills turned off, because a skill can carry hooks. So an edit after the check still gets no shell and no skill. Its MCP tools and frontmatter rest on the check alone, since their server setup lives in your own Claude config.

`hogwarts-spaces` leaves any space that already exists alone, and it types only into a pane it has just made.

## Two homes and the memory

- **The office, `~/.hogwarts`.** The store package, `bin/castle`, the database at `state/pensieve.db`, the fleet scripts and hooks, each desk's brief, role card and settings, launchd templates, pending settings snippets, the avatars, the patrol's files in `patrol/` and Gringotts' archives in `backups/`. No desk can read or write it.
- **The castle, `~/hogwarts`.** The charter (`CLAUDE.md`), `PLAN.md`, `standing-orders.md`, a folder per desk with `scratchpad.md`, `inbox/` and `outbox/` (Hermione and Ron also keep one pad per task in `pads/`), `tasks/<id>/` and `worktrees/`. It is a local git repo with no remote. Its `.gitignore` keeps `worktrees/` and the per-task pads out of git: a pad is a throwaway Checkpoint for one task, and the durable memory is the scratchpads and the Pensieve.
- **The Pensieve.** One SQLite file with full-text search. A SessionEnd hook stores a capped extract of each castle session at zero tokens: your prompts and the final replies, never tool output, scrubbed of emails, IPs, tokens and long hashes.
- **Facts know when they change.** Each fact can carry a subject key, and only one fact per key is current. Replacing a fact closes the old one and keeps its dates. You can ask what was true, or what the fleet believed, on any past date. A fact that looks like PR status, build colour or a rollout percentage is refused unless it carries the command that fetches the live value, or expires within a week.
- **The nightly review.** At 22:30 on weekdays a script exports the day's extracts, the fact candidates and the current facts into Dumbledore's inbox, with every string run through the store's scrubber, then runs him. He writes a dated patch of typed operations (fact add, retire and edit, memory note add, and archive moves for you to make by hand), each with a reason and a source, and a morning note of at most ten lines. With auto-portrait off, nothing changes until you run `castle portrait apply`. It checks every operation against a strict schema, needs the hash of the patch you read, applies the operations you accept through the store's own fact and Pensieve calls in one transaction, and records each one so it never applies twice. With `~/.hogwarts/auto-portrait` holding `on`, the job applies his additions for you, and removing that file (`rm ~/.hogwarts/auto-portrait`) switches it off: the job reads it before his run and again just before anything applies, and a file missing at either read means nothing applies that night. After a clean run the job reads his patch once while it still holds every run slot he could have, keeps the checked additions and the file's hash in the store, and applies each fact and key point addition the store accepts, on its own, from that stored copy. It takes only a patch that was not there before his run started, so a patch another run of his wrote is never applied, and never one from a run that called a model blocked here. Retires, edits and archive moves wait for you, and so does an addition the store refused. One headmaster event says what applied and what waits, with the exact command for the rest, and the same line shows in `castle portrait patches` and `castle portrait show`. A night that stops tells you once why, and a run that failed, was refused or called a blocked model sends only its own event. A night is only ever closed in the same write as the one event that tells you of it, so a kill or a failed write never leaves it closed untold or told twice. A kill is finished by the next night's job without reading his file again: a night cut off before its patch was stored is told once and closed, with no second event when its run already told you, and a night cut off after that is applied from the stored copy. Both apply paths share one ledger, so nothing applies twice.
- **Budgets.** The charter at about 600 tokens, the fleet memory index at 4KB, scratchpads at 6KB, and the startup digest under 40 lines. Nothing is deleted. Stale facts are closed or archived. Only raw extracts (after 90 days) and owl bodies (after 30) are purged.

## The store

Standard-library Python that runs on the Mac's built-in Python 3.9, with a CLI called `castle` that prints JSON. Nothing a desk runs can call it. Only your terminal and the fleet's scripts and hooks use it.

| What it holds | What it guarantees |
| --- | --- |
| Desks and tasks | One active task per desk, except the desks granted many tasks (Harry, Hermione, Moody, Ron and your own sessions), and one per session always. The grant is one way, and McGonagall, Snape, Dumbledore, the human desk and the script desks are refused it. A task never changes desk. A closed task never reopens. Closing as complete needs a hashed, single-use, expiring token only you can mint, or a proven close: a round PASS from the other family on that exact commit, the merge, CI on the merge commit and every after-merge check, held in a closure row the store guards for any writer. |
| Owls | A duplicate send collapses into one. Reading is separate from acknowledging. One answer per question, and a result only from the desk that was asked. |
| Requests | Phases only move forward. A desk can defer or decline with a reason. |
| Review passes | A pass counts only for that exact commit, only when it is registered on the author's task, and only when the reviewer's family differs from the author's. |
| Facts | One current fact per subject. Volatile facts need a live lookup or a short expiry. Nightly changes arrive as typed operations. You apply them all or nothing. With auto-portrait on, his additions apply one by one, each only if the store takes it. |
| PR follow-ups | The PR the loop opened for a task, each follow-up and its state, every teammate comment it handled, and every reply with whether it was posted. A passed task goes back to active only in the transaction that opens a follow-up, and stays active until a round of that follow-up passes. A comment is handled once, a reply is posted once, and no row is ever deleted. |

The full contract is in `office/README.md`. Both the store and fleet test suites run on the system Python. They include checks that no code reads environment variables and that hostile ids and paths are refused at every entry point.

## Spending fewer tokens

Three things usually dominate token use: what loads before you type, how long a session runs, and Bash output. With many connectors switched on, a fresh session can use a large share of the 200k restart point before your first prompt, much of it on tools that never get called. Measure your own with `/context` and `rtk discover` before you change anything.

- **Tempus.** One task per session. At about 200k context, write a Checkpoint and start fresh. A hook prints one line once a session passes 200k.
- **Lean memory.** Keep the memory index small and archive older entries.
- **Idle connectors off.** Switch off connectors and MCP servers you never call.
- **Per-desk tool sets.** Headless desks load only what their job needs, with `--strict-mcp-config`, `--tools` and a budget cap.
- **RTK, carefully.** RTK compresses Bash output. Measure with `rtk discover` before adding its hook. When you do, use `--hook-only`, exclude review-critical commands such as `git diff`, `git show`, `git push` and `gh pr`, turn off its recall store, and keep it off the Codex desks. Reviewers read diffs with `RTK_DISABLED=1`.

## What always comes back to you

- Merges, even green ones. No auto-merge, and no standing order can contain a merge.
- Closing a task, unless you switched auto-close on, and then only once scripts prove the merge, CI and every after-merge check.
- Deploys and pipeline gates: rollbacks, retries, rebuilds, unblocks and flag changes.
- Anything that changes prod.
- Credentials and logins. No desk ever enters, reads or prints one.
- Security and config: permissions, hooks, settings, MCP, plugins and sandbox. Desks propose a diff and you apply it.
- Installs. Homebrew only, after you've read `brew info`.
- Anything sent to a person: chat, email, tickets, PR threads, review requests and opening a ready PR. The one exception after the draft PR is the follow-up replies you switch on (`~/.hogwarts/pr-followup`): one-line replies to teammates' review comments on PRs the loop opened, posted only after the other family passed them and only while `gh` is signed in as your `GITHUB_ACCOUNT`.
- Public repo text: branch names, commits and PR text before the first push. The one exception is the draft PR you opt in to (`~/.hogwarts/auto-draft-pr`), which pushes a reviewed commit and opens a draft from Harry's text after a fleet-word and credential check. Character names never leave the fleet.
- Scope: editing Intent, adding criteria, splitting a PR.
- Force pushes and deletions, including branches, PRs and memory: auto-portrait never retires, edits, archives or moves anything.

The only pre-approvals are the ones you write in `standing-orders.md`.

## Rollout

Switch the fleet on in this order, one stage at a time. The stage numbers match [ONBOARDING.md](ONBOARDING.md), which has the steps and a "You're done when" check for each.

| Stage | What | Done when |
| --- | --- | --- |
| 0 to 3.1 | Prerequisites, the install, sign-ins, deny rules for your own sessions, idle connectors off | Your own settings are reviewed and the office is denied to your sessions |
| 3.2 and 4 | The front desk: the Owl Post, McGonagall, Snape and the hooks | An owl round trip works, a forged sender is refused, and a second active task for McGonagall is refused |
| 5.1 | The review loop: worktree, verify, review and push scripts, the push gate, Harry, Hermione and Moody | Three PRs pushed, each with a pass tied to its commit, and the gate blocked a push with no pass |
| 5.2 | Patrol in shadow mode: the Map and Ron's jobs (lineup, keeper's watch, weekly scoreboard) writing to files only, Hermione's bot pass in draft mode, Gringotts with a restore drill | The morning lineup matches `gh` three days running |
| 5.3 | Dumbledore's nightly review: the nightly export and Dumbledore in proposals-only mode, an RTK number | Two nightly patches reviewed, and an RTK go or no-go |
| 5.4 | The RTK hook and standing orders, if the numbers say so | A normal week where you only answer what needs you |

Ollivander (5.5) and the live view (5.6) can go on any time after stage 4.

`install.sh` covers stage 1 and writes the pending snippets you apply yourself in stage 3. `scripts/owlpost-setup.sh` switches on the Owl Post (3.2). The review loop (5.1) is pending snippets you apply plus one `enabled` file per desk. `scripts/patrol-setup.sh` starts the patrol in shadow mode (5.2), and `scripts/portrait-setup.sh` starts the nightly review (5.3). Stage 5.4 is standing orders and RTK, which need your own numbers first.

## Known limits

What the fleet doesn't do, or only partly does.

- **Codex desks rely on a beta feature for their read boundary.** Codex's `--sandbox` modes let commands read the whole disk (checked on Codex 0.160.0), so `run_desk` never passes `--sandbox` to Harry or Moody. It passes a fleet permission profile instead: an allowlist with the desk's working folder, its own castle folder, the castle tasks and its repo's `.git` folder, a private temp folder for a desk that writes, no network, and `~/.hogwarts` and `/private/tmp` denied by name. `scripts/codex-boundary-test.sh` checks that kind of profile through `codex sandbox`, with no model and no tokens. `scripts/codex-exec-boundary-test.py` checks each desk's real command under `codex exec`, for a few cents per desk. Permission profiles are marked beta, so rerun both after every Codex upgrade. Enable Harry and Moody only after both pass on your Mac.
- **No hook field says a person typed the prompt.** Claude Code's hook input does not separate an interactive session from `claude -p`. So the close and go hooks also check the session transcript: every entry must name the `cli` or `claude-desktop` entrypoint, and the prompt's own entry must be a typed human prompt from the last 30 seconds. If they can't confirm that, they refuse and print the `castle` commands to close or register the task from your terminal.
- **A go runs inside your prompt's hook.** It fetches the base and makes the worktree before your prompt goes through, so a go on a big repo takes a few seconds, inside the hook's timeout. SIGTERM or SIGHUP ends it through its cleanup: the store rolls back and the worktree, its branch and the owl's inbox copy are taken back. If the store can't say whether the go got through, nothing is taken back, and the go names what's left so you can check the task with `castle task show` and remove them yourself if it has no worktree. SIGKILL can't be caught, so one that lands between the worktree and the store write leaves that worktree and its branch behind, and the next go refuses on the branch until you remove them.
- **McGonagall asks you each time.** Her outbox writes and TASK.md edits are on the castle's ask list, so Claude asks you before every owl she posts and every TASK.md she writes. That is deliberate friction, and you can't pre-approve it from inside a session.
- **The review loop is tested on temporary repos.** The fleet suite runs it on throwaway git repos, so try it on a small real task first. `office/pending/README.md` (g) walks through one.
- **The nightly review is tested on temporary stores.** Its tests cover the export, the run and the patch checks. It reviews the local day its job runs in, so when the Mac sleeps through 22:30 and launchd only runs the job on waking after midnight, the new day is reviewed and the missed one never is. The export stops at 512KB of extracts and 300 fact candidates and says how many it left out. Archive moves are notes you carry out by hand, and the export carries no copy of a memory index, so he proposes them only from what the day shows him. His chat is off until you give him a read-only MCP job ([CUSTOMISE.md](CUSTOMISE.md#change-budgets-and-limits)).
- **The patrol is tested against a faked GitHub and faked desks.** Know these before you take it out of shadow mode:
  - The patrol's scripts and the closer read GitHub with `gh api graphql`, which is always an HTTP POST, because only GraphQL says whether a review thread is resolved. A guard lets through only eight fixed queries, none of them a mutation, with checked variables. One reads one PR the review loop opened, whole, for its follow-up, and two are the closer's: the PRs from one branch, and the checks on one merge commit. The one read outside that guard is Ron's own `gh run view` of a failing log, which his brief allows and which only reads.
  - Open PRs come 50 to a page, up to 10 pages per list. A list that can't be read to its end leaves the Map's snapshot as it was, and the round's row says it was incomplete. Within a PR, the patrol sees 100 review threads and 100 checks per commit, and past that those lists are cut.
  - Shadow mode covers Ron's and Hermione's patrol runs too. A cap, near-cap or vendor-limit note from one of those runs lands in the job's file instead of the digest, while the cap itself and the spend still count. Notes about a desk's model still reach the digest in shadow mode (a blocked model, a model change, a failed model trial), since they are about the desk's setup, not the patrol's findings.
  - Ron reads a failing log with `gh run view`, but his sandbox allows only `api.github.com`, so a log GitHub serves from another host may not load, and then his call is UNSURE.
  - Hermione's bot pass sees each thread's diff hunk, not the whole code, unless the PR has a worktree in the castle.
  - A gate shows only when CI reports it to GitHub as a check run that waits for approval or asks for action. A gate GitHub only sees as a pending status looks like any pending check.
  - Gringotts scrubs `config.toml` line by line, by each value's full dotted path, and drops its comments. Any form it doesn't fully read, such as a value that spans lines or an inline table under a name it would keep, leaves that file out of the backup whole.
- **The push gate is a guardrail, not a wall.** It reads the Bash command as text, so a git alias or a push through the GitHub API gets past it. It stops an agent pushing by habit or mistake. Desks get their real boundary from a sandbox with no network.
- **Harry never commits.** A commit in a git worktree writes into the main repo's `.git` folder, and write access there would let a desk plant a hook or config that later runs outside any sandbox. So his profile only reads `.git`, and the review script commits his work for him, pointing git at the repo the office recorded and running no hooks.
- **Deny rules for Bash match the usual command form only.** They are not a wall around a program. The sandbox stays the real boundary for desks.
- **The push gate only covers agent pushes.** Pushes you make by hand from a terminal stay yours.
- **A desk's runs share its run slots.** Most desks have one run slot, and Moody and Hermione two (`RUN_SLOTS`).
  - A run waits for a free slot, so a burst of more than about ten Harry runs can outlast the 31 minute wait, and the late ones give up with their own event.
  - Each slot has its own lock, Codex work folder and private temp folder, and a Codex run's permission profile grants only its own.
  - A review records its slot on its round, and a reviewer task a dead review left open is closed only by whoever holds that slot.
  - The caps stay per desk. A short launch lock stops two slots both passing a cap that only one run fits under, and a desk with two slots holds each run still going at its per-run budget against its spend cap.
  - Each such run also holds a lock of its own that its process inherits. So a run whose launcher was killed keeps its budget held while its process lives, and the next launch decision records what it spent before letting that go.
  - `fleet feed` follows whichever run of a desk wrote last, keeps its place in every other run, even one that never wrote last, and reads each to its end before letting it go.
  - A review round's owl runs only from its own review, so starting it by hand is refused. Two runs of any other owl can overlap on a two-slot desk. That only happens when one is started again by hand, or by a patrol retry after its job was killed, while the first still goes.
  - Update the office while no review is running. A review started on the old code still takes the desk lock as its only lock, and on its way in could close a reviewer task that a new review holds in another slot.
- **Two open build tasks never share one TASK.md.** The evidence, the handoff and the reviews are written next to TASK.md, so `fleet worktree` and `castle task start` both refuse a second open Harry task under the same McGonagall task. Two commands under one TASK.md never both get through: each takes that TASK.md's lock without waiting and holds it until its task is active, and the last check and the start share one store transaction. A command that store transaction refuses takes back its worktree, its new branch and its record, so its task stays queued and the same command can run again. One branch in one repo is made by one command at a time, whichever TASK.md asks for it: `fleet worktree` and a go each take that branch's lock without waiting before they check it's new, and hold it until the task has the worktree or it's taken back. A take-back removes a branch only when git made it for that command, so a branch you or another command made is never removed. The rest rests on the briefs: Hermione's outbox body files start with the task id, and Ron's with the id of the owl that started his run.
- **PR follow-ups are tested against a faked GitHub.** Know these before you switch them on:
  - Only PRs the loop opened since this version was installed are followed. A PR you opened by hand, or one opened while draft PRs were off, never is.
  - `MEMBER` is GitHub's org membership, which doesn't always mean write access to the repo. CI and service accounts that GitHub lists as people go in `FOLLOWUP_IGNORED_LOGINS`.
  - A teammate with write access can ask for a change that passes review and gets pushed to your PR before you read it. That is the point of the feature; the Intent check, the other family's review and the reply rules are what bound it.
  - An edited comment isn't routed again, and a deleted one that was already routed is still answered. A reply to a comment deleted meanwhile fails and stops the replies after it.
  - More than 100 threads, 100 comments in a thread, 100 reviews or 100 conversation comments make the PR unreadable for follow-ups. You hear once a day and answer them by hand.
  - Routing runs only in Map rounds, but the push and replies happen when the review finishes, so a reply can go out in the evening.
  - Times compare GitHub's clock with your Mac's, so a comment written a minute or two before a live period begins may or may not count. Comments written between switching follow-ups on and the next Map round fall outside every live period and are never routed.
  - A follow-up round where Harry changed no code and Hermione said CHANGES makes that review the newest one of the PR's commit, so `fleet push` refuses that commit until a later round passes it.
  - A reply that names a teammate whose name is also a fleet word is refused, so replies leave names out.
- **Auto-close proves what it can and leaves the rest to you.** Know these before you switch it on:
  - It runs from the Map's job, so it needs the Map loaded (onboarding stage 5.2), and it works only in the Map's hours, weekdays 08:00 to 19:00. Shadow mode doesn't hold it back.
  - A PR from the task's branch merged at a head the review never passed, into any base, stops the task rather than closing it. A direct push of an unreviewed head of the branch onto the base, with no PR at all, still lands the reviewed commit by ancestry and closes; catching that needs the remote branch's tip too, which the closer doesn't read yet.
  - A stacked PR merged into another base waits, and closes once its stack lands on the task's base. A wait like that, pending CI, open work under the task, the review loop or busy judge slots tells you once after 24 hours and keeps waiting.
  - Neither green CI nor "no checks" counts until 30 minutes after the merge was first seen, so a check GitHub hasn't registered yet isn't read as all of CI. A red stops at once, even beside a check it can't read, and so does a PR merged at an unreviewed head beside one it can't read.
  - Written after-merge checks are judged from the pack alone: the merged diff, CI names and results, after-merge command output and how it landed. A check that needs a live dashboard or prod data comes back HEADMASTER, and you close that task by hand.
  - A task with a PR follow-up that hasn't ended is never closed by proof, since its replies to teammates may still be on their way: the closer skips it, `fleet close` refuses it and the store refuses the close. Once the follow-up ends, the task is like any other: its newest round must be a PASS, and that commit must have landed.
  - A PASS from before this version, whose round didn't record the TASK.md it read, is closed by hand. So is a hand-registered build with no go, and McGonagall's go task closes only with its build and only when nothing else is open under it.
  - A TASK.md edited after your go, a scope change you approved included, is closed by hand, since the closer acts only on the bytes your go approved. For your own sessions' tasks the approval is the TASK.md `fleet review own` wrote.
  - Your own sessions' tasks are found by the branch name your checkout had at review, so push under that name, into the base the review named (`origin/main` unless `--base`). A checkout whose origin is a fork, with its PRs in the upstream repo, isn't followed.
  - Your own sessions' after-merge commands run without the Codex sandbox, as their pre-merge checks do, on a tree that also holds teammates' merged commits. The closer starts each at most once per merge commit and never runs one again after a kill; only `fleet close` does.
  - No after-merge command runs while Ollivander's stop or a CLI update is in place. The closer reads auto-close, the stop and the update marker again right before each command, holding Ollivander's update lock, and each command keeps that lock and the task's lock until it and everything it started have ended. Switch auto-close off part way and no later command starts; each one that ended keeps its result and never runs again. verify before the merge doesn't wait on that stop yet, so a review can still run checks under a Codex version nobody has proven again.
  - Every judge run keeps how it ended, its exit code and any vendor limit, as soon as its process ends. So a closer killed before it kept that reads the same run's verdict on its next pass and starts no other. If the closer is killed while the judge runs, no process sees how the run ended, so it never counts as a verdict. A run whose output shows it wrote no final text left nothing to lose, and the next try starts. Any other is never thrown away: the task stays unknown, no other judge run starts, and you hear of it after the unknown grace, so read its output in the office runs folder and close the task by hand. A judge output the closer can't read whole, one too large for the part it reads included, keeps its run the same way.
- **Partial clones never fetch behind your back.** The git the fleet runs, and the git its desks and verify checks run, has `GIT_NO_LAZY_FETCH=1`. A clone made with `--filter` then reports a missing object instead of quietly fetching it with your credentials, so a review or check that needs one fails until you fetch it yourself. Git older than 2.44 ignores the setting.

## Credits

The fleet borrows from firstmate (one front agent that never does project work, bounded extracts, a digest ordered for truncation, durable requests with a doorbell), from RTK, and from Graphiti's temporal facts. Graphiti itself was left out: it keeps replaced facts in default search results, costs several model calls per write, and needs a graph database. SQLite with subject keys, validity windows and current-only reads gives the useful part at zero tokens, with nothing leaving the Mac.
