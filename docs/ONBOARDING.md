# Onboarding: from a fresh Mac to a working fleet

Follow these stages in order. Each one ends with a "You're done when" check. Don't start the next stage until the check passes. A one-page checklist sits at the end.

Run every command in the Terminal app, one at a time. Press Cmd+Space, type Terminal and press Enter. Spotlight only finds Mac apps, so typing `claude`, `codex` or `castle` there finds nothing.

Nothing in this guide asks you to paste a password or token into a desk. Sign-ins happen in your own terminal or browser.

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

4. Optional: RTK compresses Bash output. Install the binary only. Don't add its hook yet, because stage 5 decides that from real numbers.

   ```
   brew info rtk
   brew install rtk
   ```

5. Optional: herdr gives you tabs and panes for your own sessions. The fleet never needs it. Install it from its own project page if you want it.

**You're done when** all of these print a path or a version, and the last line prints `fts5 ok`:

```
command -v git jq gh rg shellcheck claude codex
/usr/bin/python3 --version
/usr/bin/python3 -I -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)"); print("fts5 ok")'
```

## Stage 1: Clone and install

1. Sign `gh` in so you can clone a private repo. A browser window opens and you approve it there.

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

   It copies `office/` to `~/.hogwarts` and `castle/` to `~/hogwarts`. It adds `~/.claude/agents/snape.md` only if that file is missing. The fleet uses fixed absolute paths on purpose, so the installer rewrites the original home folder in the copied files to yours. It sets every folder to mode 0700 and every file to 0600, with `bin/castle` at 0700. It makes the castle a local git repo with no remote, creates the database, registers the twelve desks, and runs both test suites.

   It refuses to touch an existing `~/.hogwarts` or `~/hogwarts`. If you really want to reinstall, `./install.sh --force` first moves each folder aside to `<folder>.pre-install-<timestamp>`.

   It never touches `~/.claude/settings.json`, `~/.codex` or launchd.

**You're done when** the installer ends with lines like these, and then lists the placeholder files for stage 2:

```
tests: Ran 380 tests in ... OK
tests_fleet: Ran 119 tests in ... OK
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

4. Fill in the placeholders. The fleet ships without anyone's server names. These five tokens stand in for yours:

   | Placeholder | What to put there | Files |
   | --- | --- | --- |
   | `<warehouse-mcp>` | The name of your SQL warehouse MCP server, exactly as `/mcp` lists it in a Claude session | `~/.claude/agents/snape.md`, `~/.hogwarts/desks/snape/settings.json`, `~/hogwarts/.claude/settings.json` |
   | `<observability-mcp>` | The name of your metrics, logs and traces MCP server | the same three files |
   | `<chat-mcp>` | The name of your team chat MCP server | `~/hogwarts/.claude/settings.json`, `~/hogwarts/.claude/agents/mcgonagall.md`, and the settings for Hermione, Ron, Snape and the portrait in `~/.hogwarts/desks/` |
   | `<github-account>` | The GitHub account the fleet's scripts should use | `~/.hogwarts/fleet/config.py` |
   | `<repos-to-watch>` | The repos the later-stage PR watcher should follow, as `owner/repo` | `~/.hogwarts/fleet/config.py` |

   If you don't have a server of one kind, use the name `none`. A rule for a server you don't have matches nothing.

   Fill the three server names with one command. Replace `WAREHOUSE`, `OBSERVABILITY` and `CHAT` with your names first.

   ```
   cd ~ && grep -rlI -e '<warehouse-mcp>' -e '<observability-mcp>' -e '<chat-mcp>' .hogwarts hogwarts .claude/agents/snape.md |
     while IFS= read -r f; do
       sed -i '' -e 's/<warehouse-mcp>/WAREHOUSE/g' -e 's/<observability-mcp>/OBSERVABILITY/g' -e 's/<chat-mcp>/CHAT/g' "$f"
     done
   ```

   The tool names after each server name are a starting point. Snape's list names six warehouse tools and seven observability tools. Open a Claude session, run `/mcp`, and change any name that differs from what your servers offer. Keep Snape to read-only tools.

   Then open `~/.hogwarts/fleet/config.py` in a plain text editor such as `nano` and set the two GitHub lines, for example:

   ```
   GITHUB_ACCOUNT = "your-account"
   WATCHED_REPOS = ("your-account/your-repo", "your-org/another-repo")
   ```

5. Close off any other MCP server McGonagall has no business using. Her deny list in `~/hogwarts/.claude/settings.json` already names the warehouse, observability and chat send tools, the desktop app's own servers, and the Gmail connector's write tools. Add `"mcp__<server>"` for each other server `/mcp` shows you, such as a browser, ticketing or cloud console server. Keep the file valid JSON.

**You're done when** all of these hold:

- `claude auth status`, `codex login status` and `gh auth status` show you signed in, with the right GitHub account active.
- This prints nothing:

  ```
  grep -rn -e '<warehouse-mcp>' -e '<observability-mcp>' -e '<chat-mcp>' -e '<github-account>' -e '<repos-to-watch>' ~/.hogwarts ~/hogwarts ~/.claude/agents/snape.md
  ```

- The settings files still parse as JSON, and the fleet tests still pass:

  ```
  jq -e . ~/hogwarts/.claude/settings.json ~/.hogwarts/desks/*/settings.json >/dev/null && echo json ok
  cd ~/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests_fleet -t .
  ```

## Stage 3: Apply the pending settings yourself

Some changes touch your own Claude settings and start a background job. The fleet only prepares them. You read each one and apply it. The full notes are in `~/.hogwarts/pending/README.md`.

1. Back up your user settings first, with a timestamped name the uninstaller knows how to find.

   ```
   cd ~/.claude
   [ -f settings.json ] && cp -p settings.json "settings.json.pre-hogwarts-$(date +%Y%m%d-%H%M)"
   ```

2. Apply `a1`, the deny rules. They stop your own Claude sessions from reading the office or running the store. Do this before you first use Snape, since he is a user-level agent with Read.

   If you have no `~/.claude/settings.json` yet, copy the snippet in:

   ```
   cp ~/.hogwarts/pending/a1-user-settings-deny.merge.json ~/.claude/settings.json
   ```

   If you have one, merge the deny rules into it, check the difference, then move it into place:

   ```
   cd ~/.claude
   jq -s '.[0] as $s | .[1].permissions.deny as $add | ($s.permissions.deny // []) as $old | $s | .permissions.deny = $old + ($add - $old)' \
     settings.json ~/.hogwarts/pending/a1-user-settings-deny.merge.json > settings.json.new
   diff settings.json settings.json.new
   mv settings.json.new settings.json
   ```

3. Leave `a2`, the push gate, for stage 5. Its script does not exist yet, and a hook that cannot import fails open and adds noise.

4. Load the Owl Post. Follow `~/.hogwarts/pending/b-owlpost-launchctl.txt` one step at a time. It has a dry pass, the load, a test and the undo.

5. Read `~/.hogwarts/pending/c-codex-approval.txt`. Harry and Moody send code to OpenAI, so they stay off until your organization approves Codex for its source code, and until stage 5 proves their read boundary.

6. Optional clean-up that saves tokens in every session: switch off connectors and MCP servers you never call, with `/mcp` in a session or in your claude.ai connector settings.

**You're done when** all of these hold:

- `jq '.permissions.deny' ~/.claude/settings.json` lists the three `~/.hogwarts/**` rules.
- `ls ~/.claude/*.pre-hogwarts-*` shows your backup, if you had a settings file before.
- `launchctl print gui/$(id -u)/com.hogwarts.owlpost | head -5` shows the job.

## Stage 4: First session with McGonagall, and a smoke test

These checks prove the store, the Owl Post and the task flow work before any desk does real work. If you add the shortcut from the [handbook](HANDBOOK.md#one-time-setup), you can type `castle` instead of `~/.hogwarts/bin/castle`.

1. Check the store.

   ```
   ~/.hogwarts/bin/castle doctor
   ```

   It prints JSON ending in `"ok": true`.

2. Send an owl from McGonagall to Hermione. You write the file by hand here, the same way a desk would.

   ```
   printf '%s\n' '{"to": "hermione", "kind": "fyi", "subject": "owl post test", "body": "hello"}' > ~/hogwarts/desks/mcgonagall/outbox/hello.json
   ```

   The Owl Post moves it within a few seconds. If you have not loaded it yet, run one pass by hand. Your shell fills in `$HOME` before `env -i` clears the environment, so the script itself still reads nothing from it. It prints one line of JSON.

   ```
   /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$HOME/.hogwarts'); from fleet.owl_post import main; sys.exit(main())"
   ```

3. Check that it arrived. Hermione's inbox holds a copy named after the owl id, and McGonagall's `.sent/` folder holds your file with the owl id in front. Then send one back.

   ```
   ls ~/hogwarts/desks/hermione/inbox/ ~/hogwarts/desks/mcgonagall/outbox/.sent/
   printf '%s\n' '{"to": "mcgonagall", "kind": "fyi", "subject": "owl post reply", "body": "hello back"}' > ~/hogwarts/desks/hermione/outbox/reply.json
   ```

   Run the pass again if the Owl Post is not loaded. Then:

   ```
   ls ~/hogwarts/desks/mcgonagall/inbox/
   ~/.hogwarts/bin/castle owl inbox mcgonagall
   ```

4. Open McGonagall's first session. In the Claude desktop app, open the Code tab, start a session and pick the `hogwarts` folder in your home folder. Or type `cd ~/hogwarts` and then `claude`. Trust the folder when asked. Her first line is "McGonagall - Chief of Staff." and her digest follows. It lists the reply owl you just sent her, and marks that fyi owl as read once it has shown it to you.

5. Ask her for a TASK.md. For example: "Write a TASK.md for: add a short CONTRIBUTING note to one of my repos. Don't route it yet." Claude asks you before she writes the file. Read the draft and say go. She then gives you one `castle task create` command. Run it in Terminal, then check it:

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

6. Close each test task as abandoned. That needs no close token. Use `"$id"` for the one you made by hand, or the id McGonagall gave you.

   ```
   ~/.hogwarts/bin/castle task close "$id" --reason abandoned
   ```

**You're done when** `castle doctor` says ok, each inbox holds the other desk's owl as `<owl id>.json`, each outbox's `.sent/` folder holds the file you wrote with the owl id in front of its name, McGonagall introduced herself with a digest, and `castle task list` shows your test task as closed.

## Stage 5: Switch on the later stages, one at a time

The fleet grows in the same order as its published rollout. Each later stage needs scripts that are not built yet. The launchd templates for them already sit in `~/.hogwarts/launchd/`, but the modules they call (`fleet.map`, `fleet.morning`, `fleet.keeper`, `fleet.portrait` and `fleet.gringotts`) do not exist, so don't load those plists until each stage ships its code and tests.

Every headless desk starts disabled. A desk is enabled only by a plain file named `enabled` in its office folder. One desk at a time, read the exact command the fleet would run for it, then make the file. `--dry-run` prints the command as JSON and runs nothing.

```
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c "import sys; sys.path.insert(0, '$HOME/.hogwarts'); from fleet.run_desk import main; sys.exit(main())" hermione --dry-run
touch ~/.hogwarts/desks/hermione/enabled
```

Remove the file to switch the desk off again: `rm ~/.hogwarts/desks/hermione/enabled`.

### 5.1 The review loop

Nothing leaves the laptop without a pass from the other model family for that exact commit.

- Needs, not built yet: the worktree, verify and review scripts, and the push gate at `fleet/hooks/push_gate.py`.
- Then: apply `a2` from `~/.hogwarts/pending` to wire the push gate into your user settings, with a fresh `.pre-hogwarts-` backup. Enable Hermione. Enable Harry and Moody only after your organization approves Codex and the stage proves they cannot read `~/.hogwarts`. On Codex 0.160.0 the fleet profile cannot enforce that yet.

**You're done when** three PRs went out with passes tied to their commits, at least one real finding changed a diff, and the gate blocked a test push that had no pass.

### 5.2 Patrol in shadow mode

Prove the cheap jobs are right before they can interrupt you.

- Needs, not built yet: the Marauder's Map, Ron's jobs (morning lineup and keeper's watch) and Gringotts.
- Then: enable Ron, and load the map, morning, keeper and gringotts plists. For three days they write to files only. Hermione's bot pass runs in draft mode, so every reply is a draft. Do one Gringotts restore drill.

**You're done when** the morning lineup matched `gh` three days running, a spot check of 50 of Ron's verdicts found nothing urgent marked routine, and at least 75% of map rounds cost zero tokens.

### 5.3 The nightly portrait

Close the memory loop, with you approving every change.

- Needs, not built yet: the nightly export of the day's extracts and fact candidates into the portrait's inbox.
- Then: enable the portrait and load its plist. It runs in proposals-only mode with read-only chat. You apply its patches yourself with `castle fact apply --file <patch> --sha256 <hash>`. Run `rtk discover --all --since 30` for a real savings number, with no hook yet.

**You're done when** you have reviewed two nightly patches, the first weekly scoreboard shows the budgets held, and you have a go or no-go on RTK.

### 5.4 After that

If the numbers justify it: the RTK hook in hook-only mode, with its exclude list set and recall off first. Then write your own standing orders in `~/hogwarts/standing-orders.md`. A standing order can never include a merge or a deploy.

## Stage 6: Daily use

From here on, the [handbook](HANDBOOK.md) is your guide: which desk to ask, the daily rhythm, the cheat sheet, the rules only you perform, and what to do when something looks off. Start every piece of work with McGonagall.

## One-page checklist

- [ ] **0** Command Line Tools, Homebrew, `claude-code`, `codex`, `jq`, `gh`, `ripgrep`, `shellcheck` installed. RTK binary optional. `fts5 ok` printed.
- [ ] **1** `gh auth login`. Repo cloned outside `~/hogwarts`. `./install.sh` ended with both suites OK and `castle doctor: ok`.
- [ ] **2** `claude auth login`, `codex login`, right `gh` account active.
- [ ] **2** Five placeholders filled. The placeholder grep prints nothing. JSON parses. Fleet tests pass.
- [ ] **2** McGonagall's deny list covers every other MCP server you have.
- [ ] **3** `~/.claude/settings.json` backed up as `.pre-hogwarts-<timestamp>`.
- [ ] **3** `a1` deny rules applied. `a2` left for later.
- [ ] **3** Owl Post loaded with `b-owlpost-launchctl.txt`.
- [ ] **3** Codex approval note read. Harry and Moody stay off.
- [ ] **4** `castle doctor` ok.
- [ ] **4** Owl round trip: McGonagall to Hermione and back.
- [ ] **4** McGonagall introduced herself with a digest.
- [ ] **4** A TASK.md registered with `castle task create`, then closed as abandoned.
- [ ] **5.1** Review loop built, `a2` applied, Hermione on. Codex desks only after approval.
- [ ] **5.2** Map, Ron and Gringotts in shadow for three days. Restore drill done.
- [ ] **5.3** Portrait proposals reviewed twice. RTK go or no-go.
- [ ] **5.4** RTK hook and standing orders, only if the numbers say so.
