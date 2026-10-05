# Onboarding: from a fresh Mac to a working fleet

Follow these stages in order. Each one ends with a "You're done when" check. Don't start the next stage until the check passes. A one-page checklist sits at the end.

Run every command in the Terminal app, one at a time. Press Cmd+Space, type Terminal and press Enter. Spotlight only finds Mac apps, so typing `claude`, `codex` or `castle` there finds nothing.

Nothing in this guide asks you to paste a password or token into a desk. Sign-ins happen in your own terminal or browser.

## The desks you'll meet

Each desk has one job. The guide switches them on one at a time, in this order. You stay in charge of all of them.

| Desk | Its job | You switch it on in |
| --- | --- | --- |
| Owl Post - Message Router | Moves messages ("owls") between desks | Stage 3.2 |
| McGonagall - Chief of Staff | Your front desk. Writes each task, keeps the plan and routes work | Stage 4 |
| Hermione - Staff Engineer | Reviews code that Codex wrote | Stage 5.1 |
| Harry - Senior Engineer | Builds changes in a git worktree, on Codex | Stage 5.1, after your organization approves Codex |
| Moody - Security Reviewer | Reviews code that Claude wrote, including yours, on Codex | Stage 5.1, after your organization approves Codex |
| Marauder's Map - PR Watcher | Watches your PRs and CI | Stage 5.2 |
| Ron - Release Engineer | Morning lineup, keeper's watch and weekly scoreboard | Stage 5.2 |
| Gringotts - Backup | Nightly local backup and a restore drill | Stage 5.2 |
| Dumbledore - Knowledge Manager | Reviews the fleet's memory each weeknight and proposes changes. His desk id is `portrait` | Stage 5.3 |
| Ollivander - Model Keeper | Keeps each desk on the model its job needs | Stage 5.5 |
| Snape - Data Analyst | Answers read-only warehouse and observability questions from your own sessions | Any time after stage 3.1 |

## Stage 0: Prerequisites

The fleet needs the macOS Command Line Tools, Homebrew and a handful of tools from Homebrew. Read `brew info` for each one before you install it.

1. Install the Command Line Tools. They bring `/usr/bin/python3` and `git`.

   ```
   xcode-select --install
   /usr/bin/python3 --version
   ```

   The version must be 3.9 or newer.

2. Install Homebrew if `brew --version` fails. Follow the instructions at https://brew.sh. Its installer is a script you run with curl, so read it first if you like.

3. Install the tools. Run `brew info` on each name first.

   ```
   brew info --cask claude-code codex
   brew install --cask claude-code
   brew install --cask codex
   brew info jq gh ripgrep shellcheck
   brew install jq gh ripgrep shellcheck
   ```

   The Claude desktop app is optional. McGonagall works well in its Code tab: `brew info --cask claude`, then `brew install --cask claude`.

4. Optional: RTK compresses Bash output. Install the binary only. Don't add its hook yet. You decide on that in stage 5, from your own numbers.

   ```
   brew info rtk
   brew install rtk
   ```

5. Optional: herdr gives you tabs and panes, and the live view of the fleet in stage 5.6. The fleet never needs it. Install it from its own project page if you want it.

**You're done when** all of these print a path or a version, and the last line prints `fts5 ok`:

```
command -v git jq gh rg shellcheck claude codex
/usr/bin/python3 --version
/usr/bin/python3 -I -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)"); print("fts5 ok")'
```

## Stage 1: Clone and install

1. Sign `gh` in. The Marauder's Map uses it in stage 5.2. A browser window opens and you approve it there.

   ```
   gh auth login
   ```

2. Clone the repo anywhere except `~/hogwarts` or `~/.hogwarts`. This guide uses `~/hogwarts-fleet`.

   ```
   gh repo clone crisryantan/hogwarts-fleet ~/hogwarts-fleet
   cd ~/hogwarts-fleet
   ```

3. Run the installer.

   ```
   ./install.sh
   ```

   It copies `office/` to `~/.hogwarts`, the office, and `castle/` to `~/hogwarts`, the castle where desks work. It adds `~/.claude/agents/snape.md` only if that file is missing. The fleet uses fixed absolute paths on purpose, so the installer rewrites the kit's built-in home path to yours in the copied files. It sets every folder to mode 0700 and every file to 0600, with `bin/castle` at 0700. It makes the castle a local git repo with no remote, creates the database, registers 13 desks and runs both test suites. The 13 desks are the seven agents, the four scripts, you (desk id `ryan`) and your own Claude sessions (desk id `ryan-claude-1`).

   It refuses to touch an existing `~/.hogwarts` or `~/hogwarts`. If you really want to reinstall, `./install.sh --force` first moves each folder aside to `<folder>.pre-install-<timestamp>`.

   It never touches `~/.claude/settings.json`, `~/.codex` or launchd.

**You're done when** the installer ends with lines like these, and then lists the placeholder files for stage 2:

```
tests: ... OK
tests_fleet: ... OK
castle doctor: ok
```

If it printed a note that `CLAUDE_BIN` or `CODEX_BIN` was not found, fix that now with [CUSTOMISE.md](CUSTOMISE.md#point-the-fleet-at-claude-and-codex).

## Stage 2: Sign in and fill in the placeholders

1. Sign the Claude command line in. The desktop app has its own sign-in, so the terminal needs one too. Headless desks use it.

   ```
   claude auth login
   claude auth status
   ```

2. Sign Codex in.

   ```
   codex login
   codex login status
   ```

3. Check which GitHub account `gh` uses. The fleet's scripts run as that account. If it is the wrong one, switch.

   ```
   gh auth status
   gh auth switch --user <your-account>
   ```

4. Fill in the placeholders. The kit comes with no server names or accounts filled in. These five placeholders stand in for yours:

   | Placeholder | What to put there | Files |
   | --- | --- | --- |
   | `<warehouse-mcp>` | The name of your SQL warehouse MCP server, exactly as `/mcp` lists it in a Claude session | `~/.claude/agents/snape.md`, `~/.hogwarts/desks/snape/settings.json`, `~/.hogwarts/desks/snape/live-tools.json`, `~/hogwarts/.claude/settings.json` |
   | `<observability-mcp>` | The name of your metrics, logs and traces MCP server | the same four files |
   | `<chat-mcp>` | The name of your team chat MCP server | `~/hogwarts/.claude/settings.json`, `~/hogwarts/.claude/agents/mcgonagall.md`, `~/.hogwarts/desks/mcgonagall/live-tools.json`, and the `settings.json` of `hermione`, `ron`, `snape` and `portrait` in `~/.hogwarts/desks/` |
   | `<github-account>` | The GitHub account the fleet's scripts should use | `~/.hogwarts/fleet/config.py` |
   | `<repos-to-watch>` | The repos whose main branch the keeper's watch and the weekly scoreboard read, as `owner/repo`. The Map follows every open PR your GitHub account authored. | `~/.hogwarts/fleet/config.py` |

   If you don't have a server of one kind, use the name `none`. A rule for a server you don't have matches nothing.

   Fill the three server names with one command. Replace `WAREHOUSE`, `OBSERVABILITY` and `CHAT` with your names first.

   ```
   cd ~ && grep -rlI -e '<warehouse-mcp>' -e '<observability-mcp>' -e '<chat-mcp>' .hogwarts hogwarts .claude/agents/snape.md |
     while IFS= read -r f; do
       sed -i '' -e 's/<warehouse-mcp>/WAREHOUSE/g' -e 's/<observability-mcp>/OBSERVABILITY/g' -e 's/<chat-mcp>/CHAT/g' "$f"
     done
   ```

   The tool names after each server name are a starting point. Snape's list names six warehouse tools and seven observability tools. Open a Claude session, run `/mcp`, and change any name that differs from what your servers offer. Keep Snape to read-only tools. Change a name in his agent file and in `~/.hogwarts/desks/snape/live-tools.json` together, since his live space only opens when every tool in the first is on the second. McGonagall's chat tools work the same way with `~/.hogwarts/desks/mcgonagall/live-tools.json`.

   Then open `~/.hogwarts/fleet/config.py` in a plain text editor such as `nano` and set the two GitHub lines, for example:

   ```
   GITHUB_ACCOUNT = "your-account"
   WATCHED_REPOS = ("your-account/your-repo", "your-org/another-repo")
   ```

5. Block every other MCP server McGonagall doesn't need. Her deny list in `~/hogwarts/.claude/settings.json` already names the warehouse, observability and chat send tools, the desktop app's own servers, and the Gmail connector's write tools. Add `"mcp__<server>"` for each other server `/mcp` shows you, such as a browser, ticketing or cloud console server. Keep the file valid JSON.

**You're done when** all of these hold:

- `claude auth status`, `codex login status` and `gh auth status` show you signed in, with the right GitHub account active.
- This prints nothing outside `~/.hogwarts/tests_fleet/`. The tests there name some placeholders on purpose, as fixtures.

  ```
  grep -rn -e '<warehouse-mcp>' -e '<observability-mcp>' -e '<chat-mcp>' -e '<github-account>' -e '<repos-to-watch>' ~/.hogwarts ~/hogwarts ~/.claude/agents/snape.md
  ```

- The settings files still parse as JSON, and the fleet tests still pass:

  ```
  jq -e . ~/hogwarts/.claude/settings.json ~/.hogwarts/desks/*/settings.json >/dev/null && echo json ok
  cd ~/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests_fleet -t .
  ```

## Stage 3: Apply the pending settings yourself

Two changes touch your own Claude settings and start a background job. The kit only prepares them. You apply each one yourself, in this order. The full notes are in `~/.hogwarts/pending/README.md`.

### 3.1 Keep your own Claude sessions out of the office

The deny rules in `~/.hogwarts/pending/a1-user-settings-deny.merge.json` stop your own Claude sessions, and Snape, from reading the office or running `castle`. Apply them before you first use Snape, since he is a user-level agent with Read.

1. Back up your Claude settings first, with a timestamped name the uninstaller knows how to find. Skip this if you have no `~/.claude/settings.json` yet.

   ```
   cp -p ~/.claude/settings.json ~/.claude/settings.json.pre-hogwarts-$(date +%Y%m%d-%H%M)
   ```

2. Add the deny rules. If you have no settings file yet, copy the snippet in:

   ```
   cp ~/.hogwarts/pending/a1-user-settings-deny.merge.json ~/.claude/settings.json
   ```

   If you have one, merge the rules into it, read the difference, then move it into place:

   ```
   cd ~/.claude
   jq -s '.[0] as $s | .[1].permissions.deny as $add | ($s.permissions.deny // []) as $old | $s | .permissions.deny = $old + ($add - $old)' \
     settings.json ~/.hogwarts/pending/a1-user-settings-deny.merge.json > settings.json.new
   diff settings.json settings.json.new
   mv settings.json.new settings.json
   ```

   You can also ask Claude to do the merge. Approve the edit yourself, and keep the backup next to the file.

**You're done when** all of these hold:

- `grep -c hogwarts ~/.claude/settings.json` prints a number above zero.
- `jq '.permissions.deny' ~/.claude/settings.json` lists the three `~/.hogwarts/**` rules.
- `ls ~/.claude/settings.json.pre-hogwarts-*` shows your backup, if you had a settings file before.

### 3.2 Switch on the Owl Post

One command runs every step and prints OK or FAILED after each one. It checks the seven outboxes, makes the logs folder, runs one Owl Post pass by hand, lints and copies the job file, loads the launchd job, then sends a test owl from McGonagall to Hermione and waits for it. It stops at the first failure. Run it from your clone:

```
cd ~/hogwarts-fleet
sh scripts/owlpost-setup.sh
```

If a step prints FAILED, the lines above it say why. The same steps by hand, with a test and the undo, are in `~/.hogwarts/pending/b-owlpost-launchctl.txt`.

**You're done when** all of these hold:

- The last line says all six steps passed.
- `~/hogwarts/desks/mcgonagall/outbox/.sent/` holds the test owl renamed to `owl_<id>-hello.json`, and `~/hogwarts/desks/hermione/inbox/` holds the delivered copy as `owl_<id>.json`.
- `launchctl print gui/$(id -u)/com.hogwarts.owlpost | head -5` shows the job.

### Leave for later

- `a2` adds the push gate. It blocks an agent's push that has no review pass, so apply it when you reach stage 5.1 and the review loop. Sections (e) and (f) of the pending README add the same gate to your Codex hooks and the desk settings, and (g) walks through a real task.
- `~/.hogwarts/pending/c-codex-approval.txt` explains why Harry and Moody stay off. They send code to OpenAI, so they wait until your organization approves Codex for its source code. They also wait until both boundary tests pass on your Mac: `sh scripts/codex-boundary-test.sh`, then `/usr/bin/python3 -I -B scripts/codex-exec-boundary-test.py`, both from your clone. The second one sends a short prompt to OpenAI, so run it only after the approval.
- Optional clean-up that saves tokens in every session: switch off connectors and MCP servers you never call, with `/mcp` in a session or in your claude.ai connector settings.

## Stage 4: First session with McGonagall, and a smoke test

These checks prove the store, the Owl Post and the task flow work before any desk does real work. If you add the shortcut from the [handbook](HANDBOOK.md#one-time-setup), you can type `castle` instead of `~/.hogwarts/bin/castle`.

1. Check the store.

   ```
   ~/.hogwarts/bin/castle doctor
   ```

   It prints JSON ending in `"ok": true`.

2. Complete the owl round trip. Stage 3's script already sent a test owl from McGonagall to Hermione. Now send one back. You write the file by hand here, the same way a desk would.

   ```
   printf '%s\n' '{"to": "mcgonagall", "kind": "fyi", "subject": "owl post reply", "body": "hello back"}' > ~/hogwarts/desks/hermione/outbox/reply.json
   ```

   The Owl Post moves it within a few seconds. It renames the sent file to `owl_<id>-reply.json` in Hermione's `outbox/.sent/` and delivers the copy to McGonagall's inbox as `owl_<id>.json`. Check both, and the store's view:

   ```
   ls ~/hogwarts/desks/hermione/outbox/.sent/ ~/hogwarts/desks/mcgonagall/inbox/
   ~/.hogwarts/bin/castle owl inbox mcgonagall
   ```

   If nothing moved after 30 seconds, run one pass by hand and read what it prints. Your shell fills in `$HOME` before `env -i` clears the environment, so the script itself still reads nothing from it.

   ```
   /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$HOME/.hogwarts'); from fleet.owl_post import main; sys.exit(main())"
   ```

3. Open McGonagall's first session. In the Claude desktop app, open the Code tab, start a session and pick the `hogwarts` folder in your home folder. Or type `cd ~/hogwarts` and then `claude`. Trust the folder when asked. Her first line is "McGonagall - Chief of Staff." and her digest follows. It lists the reply owl you just sent her, and marks that fyi owl as read once it has shown it to you.

4. Ask her for a TASK.md. For example: "Write a TASK.md for: add a short CONTRIBUTING note to one of my repos. Don't route it yet." Claude asks you before she writes the file. Read the draft and say go. She then gives you one `castle task create` command. Run it in Terminal, then check it:

   ```
   ~/.hogwarts/bin/castle task list
   ```

   You can also register a test task without her, which proves the same path:

   ```
   id="tk_$(openssl rand -hex 8)"
   mkdir -m 700 ~/hogwarts/tasks/$id
   printf '# %s smoke test\n\n## Intent\nSmoke test only.\n\n## Acceptance criteria\nAC-1 the task registers | check: castle task list\n\n## Spec\nNone.\n\n## Out of scope\nEverything else.\n' "$id" > ~/hogwarts/tasks/$id/TASK.md
   ~/.hogwarts/bin/castle task create --id $id --desk mcgonagall --title "smoke test" --intent-path ~/hogwarts/tasks/$id/TASK.md
   ```

5. Close each test task as abandoned. That needs no close token. Use `"$id"` for the one you made by hand, or the id McGonagall gave you.

   ```
   ~/.hogwarts/bin/castle task close "$id" --reason abandoned
   ```

**You're done when** `castle doctor` says ok, Hermione's inbox holds stage 3's test owl and McGonagall's inbox holds the reply, each as `owl_<id>.json`, the two sent files sit in their `.sent/` folders as `owl_<id>-hello.json` and `owl_<id>-reply.json`, McGonagall introduced herself with a digest, and `castle task list` shows your test task as closed.

## Stage 5: Switch on the later stages, one at a time

Switch these on in order, one at a time. Each piece starts off, and nothing switches itself on. Pass each stage's check before you start the next.

- 5.1 to 5.4 build on each other: the review loop, then the patrol in shadow mode, then Dumbledore's nightly review, then RTK and your standing orders.
- 5.5 (Ollivander) and 5.6 (the live view) don't depend on the others. Switch them on any time after stage 4.

Every headless desk starts disabled. A desk is enabled only by a plain file named `enabled` in its office folder. One desk at a time, read the exact command the fleet would run for it, then make the file. `--dry-run` prints the command as JSON and runs nothing.

```
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$HOME/.hogwarts'); from fleet.run_desk import main; sys.exit(main())" hermione --dry-run
touch ~/.hogwarts/desks/hermione/enabled
```

Remove the file to switch the desk off again: `rm ~/.hogwarts/desks/hermione/enabled`.

### 5.1 The review loop

An agent's push goes out only with a pass from the other model family for that exact commit. The push gate is a guardrail, not a wall: pushes you type yourself never reach it.

- What it is: the worktree, verify and review scripts (`fleet worktree`, `fleet verify`, `fleet review`, `fleet build` and `fleet push`), and the push gate at `fleet/hooks/push_gate.py`.
- To switch it on: apply `a2` from `~/.hogwarts/pending` to wire the push gate into your user settings, with a fresh `.pre-hogwarts-` backup. Enable Hermione. Enable Harry and Moody only after your organization approves Codex and both `scripts/codex-boundary-test.sh` and `scripts/codex-exec-boundary-test.py` pass on your Mac. Section (g) of the pending README walks through a task.

**You're done when** three PRs have gone out with passes tied to their commits, at least one real finding has changed a diff, and the gate has blocked a test push that had no pass.

### 5.2 Patrol in shadow mode

Prove the cheap jobs are right before they can interrupt you.

- What it is: the Marauder's Map, which diffs your PR and CI state each round. Ron's jobs: the morning lineup, the keeper's watch and the weekly scoreboard. Hermione's bot pass, which triages review threads on your PRs and only ever writes reply drafts. Gringotts, which takes a nightly local backup.
- To switch it on: run `sh scripts/patrol-setup.sh` from your clone of the kit. It checks gh, runs one Map round, a backup and a restore drill, enables Ron and loads the map, morning, keeper, scoreboard and gringotts plists. While `~/.hogwarts/patrol/shadow` exists, which it does from install, they write to files only and send you no rows or owls. Leave it there for at least three days.

**You're done when** the morning lineup has matched `gh` three days running, a spot check of 50 of Ron's verdicts finds nothing urgent marked routine, and at least 75% of Map rounds cost zero tokens. Then delete `~/.hogwarts/patrol/shadow` to let their rows and owls reach you.

### 5.3 Dumbledore's nightly review

Dumbledore reviews the fleet's memory each weeknight and proposes changes. You approve every one.

- What it is: a nightly export of the day's extracts and fact candidates into Dumbledore's inbox, his proposals-only run on it at 22:30 on weekdays, and `castle portrait` to read and apply his patches.
- To switch it on: run `sh scripts/portrait-setup.sh` yourself. It reads Dumbledore's dry run, exports today once by hand, enables his desk and loads his job, printing OK or FAILED after each step. His chat stays off until you give him a read-only MCP job ([CUSTOMISE.md](CUSTOMISE.md#change-budgets-and-limits)). Each morning after a run, `castle portrait show <date>` lists his patch and prints the exact `castle portrait apply <date> --sha256 <hash>` command. Add `--only <ids>` to apply just the operations you accept.
- If you installed RTK, run `rtk discover --all --since 30` to see what it would save on your own sessions. Don't add its hook yet.

**You're done when** you have reviewed two nightly patches, the first weekly scoreboard shows the budgets held, and you have a go or no-go on RTK.

### 5.4 After that

If your numbers justify it, add the RTK hook in hook-only mode, with its exclude list set and recall off first. Then write your own standing orders in `~/hogwarts/standing-orders.md`. A standing order can never include a merge or a deploy.

### 5.5 Ollivander, the model keeper

Ollivander - Model Keeper keeps each desk on the model its job needs, and never moves a desk to the other model family. He has no `enabled` file. His daily job is what switches him on. Until you load it, every desk runs the model it was registered with.

1. Read the role cards. Each one says what its desk needs: a tier (frontier, workhorse or fast), an effort and one line of why. If you disagree with one, edit it as in [CUSTOMISE.md](CUSTOMISE.md#change-a-desks-model).

   ```
   cat ~/.hogwarts/desks/*/role.json
   ```

2. If your organization forbids some models, list them in `BLOCKED_MODEL_PREFIXES` in `~/.hogwarts/fleet/config.py` before his first pass. While anything is listed, a Codex desk doesn't launch until that first pass has given it a model, or you've pinned one. [CUSTOMISE.md](CUSTOMISE.md#block-models-your-organization-forbids) shows how. Skip this if nothing is forbidden.

3. Read his plan. A dry run asks both CLIs what models they offer and prints each desk's pick as JSON. It changes nothing and runs no update.

   ```
   ~/.hogwarts/bin/fleet ollivander --dry-run
   ```

4. Load his daily job, the same way you loaded the Owl Post. Lint the template, copy it in, then load it. It runs at 06:00 every day and doesn't run when loaded.

   ```
   plutil -lint ~/.hogwarts/launchd/com.hogwarts.ollivander.plist
   cp ~/.hogwarts/launchd/com.hogwarts.ollivander.plist ~/Library/LaunchAgents/com.hogwarts.ollivander.plist
   chmod 644 ~/Library/LaunchAgents/com.hogwarts.ollivander.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hogwarts.ollivander.plist
   ```

5. Run his first pass by hand, so you don't wait for 06:00. Each headless desk's first pick applies by itself. Later moves to a cheaper or equal tier apply with a note, and a costlier one waits for you. McGonagall and Snape get a note telling you the one line to change in their agent files.

   ```
   ~/.hogwarts/bin/fleet ollivander
   ```

To take the job off again: `launchctl bootout gui/$(id -u)/com.hogwarts.ollivander`, then `rm ~/Library/LaunchAgents/com.hogwarts.ollivander.plist`. His CLI updates stay off unless you make `~/.hogwarts/desks/ollivander/update-clis`. Watch a few passes before you do.

**You're done when** all of these hold:

- `launchctl print gui/$(id -u)/com.hogwarts.ollivander | head -5` shows the job.
- `~/.hogwarts/bin/castle desk models` lists every desk that has a role card (McGonagall, Hermione, Moody, Dumbledore, Snape, Harry and Ron) with a tier and a model. Your own sessions' desk, `ryan-claude-1`, has no role card, so it shows no tier.
- `ls ~/.hogwarts/state/` shows no `ollivander-stop` file.

### 5.6 The live view

Desks run short, one owl at a time per desk (two at once for the reviewers), so you watch them instead of sitting inside them. A desk's other tasks wait between runs without blocking it. The feeds are read-only, and herdr is optional.

1. Try a feed first. It works without herdr and prints a line when anything happens. Press Ctrl+C to stop it.

   ```
   ~/.hogwarts/bin/fleet feed --all
   ```

2. Open herdr in one Terminal window. In a second window, read what the spaces script would do. It lists one space per desk, named like "Hermione - Staff Engineer", and changes nothing.

   ```
   ~/.hogwarts/bin/hogwarts-spaces --dry-run
   ```

3. Open the spaces. McGonagall and Snape get live sessions, because neither has a shell tool. The script reads their agent files first and refuses either one whose `tools:` line is missing or names a tool that isn't on its trusted list in `~/.hogwarts/desks/<agent>/live-tools.json`. Every other desk, the Owl Post and Ollivander get a read-only feed, since any herdr pane can type into any other. A space that already exists is left alone, so you can run it again.

   ```
   ~/.hogwarts/bin/hogwarts-spaces
   ```

   If it prints FAILED, herdr isn't running or isn't at `~/.local/bin/herdr`. Open it, or add `--herdr <path>`. If the line says refused, compare that agent's file with the repo copy and fix its `tools:` line. If it says claude is not at a path, set `CLAUDE_BIN` as [CUSTOMISE.md](CUSTOMISE.md#point-the-fleet-at-claude-and-codex) shows.

**You're done when** the first command prints "watching every desk, read-only", and the last one printed OK or SKIP for all nine spaces, which herdr's workspace list now shows.

## Stage 6: Daily use

From here on, the [handbook](HANDBOOK.md) is your guide: which desk to ask, the daily rhythm, the cheat sheet, the rules only you perform, and what to do when something looks off. Start every piece of work with McGonagall.

## One-page checklist

- [ ] **0** Command Line Tools, Homebrew, `claude-code`, `codex`, `jq`, `gh`, `ripgrep`, `shellcheck` installed. RTK binary optional. `fts5 ok` printed.
- [ ] **1** `gh auth login`. Repo cloned outside `~/hogwarts`. `./install.sh` ended with both suites OK and `castle doctor: ok`.
- [ ] **2** `claude auth login`, `codex login`, right `gh` account active.
- [ ] **2** Five placeholders filled. The placeholder grep prints nothing outside the test fixtures. JSON parses. Fleet tests pass.
- [ ] **2** McGonagall's deny list covers every other MCP server you have.
- [ ] **3.1** `~/.claude/settings.json` backed up as `.pre-hogwarts-<timestamp>`, then the `a1` deny rules applied. `grep -c hogwarts` prints more than zero.
- [ ] **3.2** `sh scripts/owlpost-setup.sh` printed OK for all six steps. The test owl reached Hermione.
- [ ] **3** `a2` left for later. Codex approval note read. Harry and Moody stay off.
- [ ] **4** `castle doctor` ok.
- [ ] **4** Owl round trip: Hermione's reply reached McGonagall.
- [ ] **4** McGonagall introduced herself with a digest.
- [ ] **4** A TASK.md registered with `castle task create`, then closed as abandoned.
- [ ] **5.1** Review loop on, `a2` applied, Hermione on. Codex desks only after approval and both boundary tests.
- [ ] **5.2** Map, Ron and Gringotts in shadow for three days. Restore drill done. Shadow file deleted once the checks pass.
- [ ] **5.3** Dumbledore's proposals reviewed twice. RTK go or no-go.
- [ ] **5.4** RTK hook and standing orders, only if the numbers say so.
- [ ] **5.5** Role cards read, any forbidden models listed, `fleet ollivander --dry-run` read, the daily job loaded, a first pass run, and `castle desk models` shows a tier for every desk that has a role card.
- [ ] **5.6** `fleet feed --all` prints its first line. `hogwarts-spaces` printed OK or SKIP for all nine spaces.
