# Customise the fleet

Every change on this page is one you make yourself, in your own terminal. No desk can write to the office, so no desk can make these changes for you.

## Before you change anything

- The office (`~/.hogwarts`) holds the code, briefs and settings. The castle (`~/hogwarts`) holds the charter and the desk folders. Edit them with a plain text editor such as `nano`.
- Keep every JSON file valid. Check with `jq -e . <file>`.
- After any change, run both suites and the health check:

  ```
  cd ~/.hogwarts
  /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests -t .
  /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests_fleet -t .
  ~/.hogwarts/bin/castle doctor
  ```

  `/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty run_suites.py`, run in the same folder, runs the same tests of both suites faster, every test module in its own process, and exits 1 if any module fails.

- The desk registry in the database is append-only by design. A desk's family, display name (its role) and model can't be updated or deleted once registered. So a registry change means either a new desk name or a fresh install. `./install.sh --force` moves the old office and castle aside, with their database, and installs fresh from the repo.
- If you want a change to survive the next install, make it in your clone of the repo too, and commit it there.

## Point the fleet at Claude and Codex

The fleet starts `claude` and `codex` by absolute path, never through `PATH`. The paths are two constants in `~/.hogwarts/fleet/config.py`:

```
CLAUDE_BIN = "<home>/.local/bin/claude"
CODEX_BIN = "/opt/homebrew/bin/codex"
```

`<home>` stands for your home folder. `install.sh` writes its absolute path into the file.

`install.sh` checks the default and the usual Homebrew places, and sets them when it finds one. If you install either tool somewhere else, find it with `command -v claude codex` and edit those two lines. `hogwarts-spaces` reads `CLAUDE_BIN` from the same file, so McGonagall's and Snape's live sessions run the same `claude` as the headless desks.

## Trust a tool in a live space

McGonagall's and Snape's live herdr sessions only get tools you've trusted by name. Each has a trusted list in the office, which no desk or agent can write:

```
~/.hogwarts/desks/mcgonagall/live-tools.json
~/.hogwarts/desks/snape/live-tools.json
```

Each file is a JSON object with one key, `tools`, listing every tool that live session may have, built-in and MCP, by its exact name. Before a live space opens, `hogwarts-spaces` refuses it if any definition of that agent lists a tool that isn't on the list.

The list has its own rules:

- Names match exactly. There are no wildcards or prefixes, and case counts.
- The list is refused if it names a built-in tool that can run commands, such as Bash, a subagent or a notebook, or any built-in that `fleet/agent_gate.py` doesn't know runs nothing.
- The list is also refused unless it's a plain file you own, with one hard link and nobody else able to write it, in a plain folder you own. So edit it in place. A symlink to a copy elsewhere, or a list or folder others can write, keeps the space shut.

To add a tool:

1. Check what it does. A live pane sits next to every other pane, so only add a tool that reads. Never add one that sends, writes, deletes, runs a command or types into another pane, even from a server whose other tools are already on the list.
2. Add its exact name, as `/mcp` shows it in a Claude session, to the agent's `live-tools.json`.
3. Add the same name to the agent's `tools:` line, in `~/.claude/agents/snape.md` or `~/hogwarts/.claude/agents/mcgonagall.md`.
4. Run `~/.hogwarts/bin/hogwarts-spaces --dry-run` and check that space no longer says refused.

A built-in tool also has to be in that agent's `--tools` list at the top of `hogwarts-spaces` before the live session gets it.

The kit's lists use the same placeholders as the kit's agent files, `<warehouse-mcp>`, `<observability-mcp>` and `<chat-mcp>`, and onboarding stage 2 fills them in with everything else. If you keep your own fork or copy of the kit with your real server names, put the real names in its copies of these two files too, or the live spaces will be refused.

## Change a desk's model

You change a desk's role, not its model name. Each desk has a role card, `~/.hogwarts/desks/<desk>/role.json`, and Ollivander - Model Keeper picks the model that fits it. The card is strict JSON with exactly four keys:

```
{
  "family": "claude",
  "need": "frontier",
  "effort": "high",
  "why": "Deep reviews of Codex work"
}
```

- `family` is `claude` or `codex`, and it must match the desk's registry family. A desk never changes family.
- `need` is `frontier`, `workhorse` or `fast`. It is the tier of model the job calls for, and the cost order is fast, then workhorse, then frontier.
- `effort` is `low`, `medium`, `high`, `xhigh` or `max`. Ollivander lowers it to the nearest level the picked model lists.
- `why` is one plain line of at most 120 characters.

The kit's defaults:

| Desk | Needs | Effort | Why |
| --- | --- | --- | --- |
| McGonagall - Chief of Staff | frontier | high | Scope and spec judgment, used rarely |
| Hermione - Staff Engineer | frontier | high | Deep reviews of Codex work |
| Moody - Security Reviewer | frontier | high | Security review of Claude work |
| Dumbledore - Knowledge Manager | frontier | medium | One nightly memory review |
| Snape - Data Analyst | workhorse | high | Accurate SQL at moderate cost |
| Harry - Senior Engineer | workhorse | high | Everyday coding |
| Ron - Release Engineer | fast | low | Sorts lots of PR and CI updates |

Edit the card, then run `~/.hogwarts/bin/fleet ollivander --dry-run` to read the new pick. His daily job applies it, or run `~/.hogwarts/bin/fleet ollivander` yourself.

- A move to the same tier or a cheaper one applies with a note.
- A costlier one waits for `castle desk model <desk> --approve`. `--approve` checks the pick against the latest catalog first, and refuses one that's gone, hidden, filed under another tier or retiring within 30 days.
- If a later pass no longer makes a waiting pick, for any reason, it's dropped and you get a quiet note.

How he picks:

- **Claude desks** take the alias for the tier: `opus` for frontier, `sonnet` for workhorse and `haiku` for fast. Those are the `CLAUDE_LINES` in `fleet/config.py`. An alias always means the newest model of its line that your Claude Code knows.
- **Codex desks** take a model from the catalog that `codex debug models` prints. He files each model by the words in its description (`CODEX_LINE_WORDS`), skips any with an excluded word such as "legacy" (`CODEX_EXCLUDED_WORDS`), skips any that retire within 30 days, and takes the top-ranked visible one.
- **A name he can't file** under exactly one tier is never picked. You get one note. File it yourself with `castle model line <name> <line>`, where the line is `frontier`, `workhorse`, `fast` or `ignore`. Your filing outranks the keywords.
- **McGonagall and Snape** take their model from the `model:` line at the top of their agent files, `~/hogwarts/.claude/agents/mcgonagall.md` and `~/.claude/agents/snape.md`. Ollivander never edits those files. When their role would pick something else, he tells you the one line to change.

To fix a desk on one model, pin it. Ollivander then leaves it alone until you unpin it:

```
~/.hogwarts/bin/castle desk model <desk> <model>
~/.hogwarts/bin/castle desk model <desk> --role
~/.hogwarts/bin/castle desk models
```

A Claude desk pins to an alias or a full `claude-` model id. A Codex desk pins to a slug from the last Codex catalog. If that catalog hides the slug or shows it retiring within 30 days, the pin still goes through, and its output carries a `warning` saying so. The registry model and a `model =` line in a Codex desk's `codex.toml` are only what a desk runs before Ollivander has made his first pick. The same goes for a profile the `codex.toml` selects: once the desk has a model, it replaces that profile's model and reasoning effort too.

After any switch, the first two runs are a trial. If both fail, the desk goes back to its previous model, pinned, and you get a note. A run that Claude's or Codex's own usage limit stopped never counts, and a switch you made yourself is never reverted. Pinning the model a desk is on during its trial ends the trial, so two failures after that never move it. Because a revert pins, it never lands on a model you filed as ignore, or one the latest catalog no longer lists, hides or retires within 30 days. The desk stays where it is, unpinned, and you get a note.

## Block models your organization forbids

`BLOCKED_MODEL_PREFIXES` in `~/.hogwarts/fleet/config.py` is empty in the kit. Fill it with lowercase prefixes. Each is matched against Claude Code aliases, full Claude ids and Codex catalog slugs:

```
BLOCKED_MODEL_PREFIXES = ("<model-alias>", "claude-<model-alias>-")
```

To forbid a whole Claude line, list its alias and its full id prefix, as above. What a block does:

- A blocked model is never picked, pinned, filed or launched. Ollivander skips it without a note.
- If every model of a tier is blocked, the desk keeps the one it has and you get one note.
- If a desk's current model is blocked, `run_desk` won't launch the desk until you pin an allowed one.
- A trial that fails never reverts a desk onto a blocked model.
- While anything is blocked, a Codex desk with no model of its own isn't launched either, because the Codex CLI default can't be checked against the list. Ollivander's first pass gives each unpinned Codex desk its pick, so run `~/.hogwarts/bin/fleet ollivander` once after you fill the list, or pin a model.
- A desk a failed trial pinned to no model is one he leaves alone, so pin it a model or hand it back with `--role`. The refusal and his daily note both say so. A failed trial never reverts a Codex desk onto that default while anything is blocked.

An alias counts as blocked once it has run as a blocked full id, even for a helper call that did little of the work. The office remembers every full id each alias has run as, and `opus[1m]` shares the record of `opus`, so switching a desk away and back doesn't make it forget. A later run on an allowed id doesn't lift it, since that run must have started before the block was known. That alias stays refused until you change the list, so pin an allowed alias or full id instead.

## Update the CLIs automatically

Off by default. Make the plain file `~/.hogwarts/desks/ollivander/update-clis` and each of Ollivander's passes first runs `claude update` and `brew upgrade --cask codex`, with the output in `~/.hogwarts/logs/ollivander-update.log`. Then he checks both `--version` commands and every enabled desk's dry run. Any failure stops every headless desk. So does a new Codex version, because the Codex permission boundary is proven per version: run `scripts/codex-boundary-test.sh` and `scripts/codex-exec-boundary-test.py` on it first. A new Claude Code version is only a note.

While the stop file `~/.hogwarts/state/ollivander-stop` exists, or an update is running, no headless desk launches. A run that has passed its last check also holds off an update until its process has exited, so an update never swaps a CLI under a desk that's about to run or still running. If runs keep an update waiting for two minutes, that pass skips the update and the next one tries again. If a pass dies part way through an update, the next pass updates nothing, stops every headless desk and tells you, since the CLIs may have moved with no check run. Run `~/.hogwarts/bin/castle ollivander clear` once you've looked. Remove `update-clis` to switch updates off again.

## Switch the automations on and off

Five automations are off by default. Each has its own file in the office, and only you can write there:

| File | While it holds `on` | It also needs |
| --- | --- | --- |
| `~/.hogwarts/auto-draft-pr` | After the review loop's own PASS, never one you ran by hand, the loop pushes exactly the reviewed commit and opens a draft PR with your `gh` login. Marking it ready, requesting reviews and merging stay yours | The review loop (onboarding stage 5.1) and `gh` signed in |
| `~/.hogwarts/pr-followup` | Review comments from teammates with write access on a PR the loop opened go back to Harry. He fixes or answers each one, the other family reviews, and the loop pushes to the same branch and posts his replies. It never resolves a thread | Draft PRs on, the patrol out of shadow mode, Harry and Hermione enabled, `FOLLOWUP_IGNORED_LOGINS` filled and `gh` signed in as `GITHUB_ACCOUNT` |
| `~/.hogwarts/auto-close` | Each Map round starts the closer. It closes a passed task once scripts prove exactly the reviewed commit merged, CI on the merge commit is green and every after-merge check passes, with one row for each close. It skips a task whose follow-up is still open. A build it closes loses its worktree in the same pass when nothing in it can be lost | The Map's job loaded (stage 5.2), and the reviewer of the other family enabled to judge written after-merge checks |
| `~/.hogwarts/worktree-cleanup` | Each Map round removes the worktree of each build task closed at least three days ago, by any path, once it has no uncommitted changes, no git-ignored files apart from the dependency links the fleet made, and its HEAD is on the base or its branch on origin. It never deletes a branch, keeps anything it can't read and tells you once, and sends one row per round that removed anything | The Map's job loaded (stage 5.2) |
| `~/.hogwarts/auto-portrait` | Dumbledore's weeknight job applies the fact and memory note additions in the patch his run wrote that night. Edits, retires and archive moves wait for you, and so does an addition the store refused | His nightly job loaded (stage 5.3) |

Switch one on from your terminal, and off by removing the file:

```
echo on > ~/.hogwarts/auto-close
rm ~/.hogwarts/auto-close
```

A switch counts only while it is a plain file you own, that no one else can write, reached with no link on the way, holding exactly `on`. Any other text, a symlink, or a file of that name anywhere else, such as a desk folder, a task folder or `standing-orders.md`, leaves it off. Write the order in `standing-orders.md` too if you want people to read it, but code never parses that file. Whichever switches are on, `Mischief managed <task-id>` still closes a task by hand and `castle portrait apply` still applies a patch by hand. The full rules for each are in `~/.hogwarts/pending/README.md`, sections (g) and (h).

## Change a display name or a character

The display name is the desk's job title, such as "Snape - Data Analyst". It appears in:

- the first line each desk says, in its `BRIEF.md` or agent file;
- the agent file's `description:` line, for McGonagall and Snape;
- the registry role, set by `install.sh` (so a fresh install picks it up);
- `office/tests_fleet/support.py` and `office/assets/avatars/README.md`.

Changing the character, such as a different personality or voice, is an edit to the desk's `BRIEF.md` or agent file. Keep the job, the rules and the output block the same, because scripts and reviews depend on their shape. If you change the look, replace the avatar too (see below).

Renaming a desk's short name (the folder name, such as `ron`) is a retire plus an add. Desk names are wired into config, settings, launchd and tests.

## Add a desk

Copy the nearest sibling at every step. A new Claude reviewer starts from Hermione, a new patrol desk from Ron, a new Codex desk from Moody.

1. **Pick a name and a family.** A short lowercase name and `claude` or `codex`. Give it a display name in the form "Character - Job".
2. **Register it.**

   ```
   ~/.hogwarts/bin/castle desk add <name> --family claude --role "<Character> - <Job>" --model sonnet
   ```

   Add the same line to `install.sh` and to `REGISTRY` in `office/tests_fleet/support.py`. A desk that should keep many tasks in flight also needs `~/.hogwarts/bin/castle desk many-tasks <name>`, the same name in the many-tasks loop in `install.sh`, in `MANY_TASK_DESKS` in `office/tests_fleet/support.py` and in `MANY_TASK_DESKS_SEED` in `office/hogwarts/db.py`. The grant is one way. Without it the desk holds one active task at a time.
3. **Config.** In `~/.hogwarts/fleet/config.py`, add the name to `CASTLE_DESKS`, to `ROLE_DESKS` and to `HEADLESS_CLAUDE` or `HEADLESS_CODEX`. A Claude desk also needs entries in `CLAUDE_TOOLS`, `MAX_BUDGET_USD`, `CLAUDE_READ_DIRS`, `DAILY_RUN_CAP` and `DAILY_SPEND_CAP_USD`. A Codex desk needs `CODEX_ACCESS` and `DAILY_RUN_CAP`. The fleet tests check these sets agree with each other and with the registry.
4. **Office files.** Make `~/.hogwarts/desks/<name>/` (mode 0700) with a `BRIEF.md`, a `role.json` role card, plus `settings.json` for Claude or `codex.toml` for Codex (mode 0600). Change every path in the copied settings that names the old desk.
5. **Fence it in.** Every Claude desk's settings must deny sandbox writes to every other desk's folder, and `run_desk` refuses to launch one that doesn't. So add `"$HOME/hogwarts/desks/<name>"` (written out in full) to `sandbox.filesystem.denyWrite` in each other desk's `settings.json`. Add `Edit(~/hogwarts/desks/<name>/**)` and `Write(~/hogwarts/desks/<name>/**)` to the deny list in `~/hogwarts/.claude/settings.json`, so McGonagall can't write there either.
6. **Castle folder.** Make `~/hogwarts/desks/<name>/inbox` and `outbox` (mode 0700) and a `scratchpad.md` with `## Now`, `## Notes` and `## Checkpoint` headings (mode 0600). A Claude desk that keeps many tasks should also keep one pad per task: add it to `TASK_PAD_DESKS` in `fleet/config.py` and `Edit(~/hogwarts/desks/<name>/pads/**)` to its settings allow list. `run_desk` makes each pad at launch.
7. **The Owl Post.** Add the new outbox to `WatchPaths` in `~/.hogwarts/launchd/com.hogwarts.owlpost.plist`, run `plutil -lint` on it, copy it to `~/Library/LaunchAgents/`, then reload it with `launchctl bootout` and `launchctl bootstrap` as in `pending/b-owlpost-launchctl.txt`. With terminal loops, skip the copy and the reload: restart `fleet loops` instead.
8. **Routing.** Tell McGonagall when to use the new desk: add a line to the Routing section of her agent file.
9. **Avatar.** Draw an SVG that follows the style rules in `~/.hogwarts/assets/avatars/README.md`, and export transparent PNGs at 512, 128 and 64.
10. **Live view.** Add a line for the desk to the `spaces()` list in `~/.hogwarts/bin/hogwarts-spaces`, labelled "Character - Job" and running `fleet feed --desk <name>`. Never give a desk that can run commands a live session there. McGonagall's and Snape's spaces only open while every tool on their `tools:` lines is on their trusted lists, so read [Trust a tool in a live space](#trust-a-tool-in-a-live-space) before you add one to either.
11. **Test, then switch on.** Run both suites and `castle doctor`. Read the desk's `--dry-run` command (onboarding stage 5), then create its `enabled` file. Run `fleet ollivander --dry-run` to see its first model pick.

## Retire a desk

The quick and safe way is to switch it off and stop routing to it:

1. `rm ~/.hogwarts/desks/<name>/enabled`, so nothing launches it.
2. Remove it from McGonagall's Routing section.
3. Let its open tasks close. `castle task list --desk <name>` shows them.

Its registry row stays forever, so its history stays readable. To remove its wiring as well, undo steps 3 to 7 of "Add a desk", archive its castle and office folders, and run the tests. Never retire the last Claude reviewer or the last Codex reviewer (see below).

## Change budgets and limits

All in `~/.hogwarts/fleet/config.py`:

| Constant | What it limits |
| --- | --- |
| `MAX_BUDGET_USD` | The most one headless Claude run may spend, passed as `--max-budget-usd`. A run killed before it reports its cost is charged this much |
| `DAILY_RUN_CAP` | Runs per desk in one cap day. The kit sizes it for a busy day: Hermione 80, Ron 120, Dumbledore 3, Harry 40 and Moody 80 |
| `DAILY_SPEND_CAP_USD` | Spend per Claude desk in one cap day, read from the store's metrics: Hermione $60, Ron $10 and Dumbledore $4. The Codex desks have no spend cap |
| `CAP_RESET_UTC_SECONDS` | When the cap day starts. `None` is local midnight on your Mac, daylight saving included. A number fixes the reset that many seconds after UTC midnight instead |
| `CAP_WARN_FRACTION` | The share of a cap, 0.8, at which a desk sends one warning that day |
| `PATROL_STALE_DAYS` | Days without activity (a commit, review, comment or edit) after which a PR moves to the stale group at the bottom of the lineup, 30. Review requests from bots are listed there too and never wake Ron |
| `REVIEW_ROUND_CAP` | Review rounds per author task, 3. The next one waits for `castle task allow-round <task-id>` |
| `FOLLOWUP_ROUND_CAP` | Review rounds of one PR follow-up, 2, apart from the task's own. The next one waits for `castle task allow-round <task-id>` run while that follow-up is open |
| `FOLLOWUP_MAX_PER_TASK` | Follow-ups one task takes, 5. Comments after that wait for you, and you hear once a day |
| `FOLLOWUP_SETTLE_SECONDS` | How old a PR's newest teammate comment must be before its follow-up starts, 300, so one review's comments go as one follow-up |
| `FOLLOWUP_WRITE_ASSOCIATIONS` | Whose comments a follow-up takes, by GitHub's authorAssociation: OWNER, MEMBER and COLLABORATOR |
| `FOLLOWUP_IGNORED_LOGINS` | Service and CI accounts that GitHub lists as people; their comments are never routed. Empty in the kit: fill it before you switch follow-ups on. Compared without letter case |
| `FOLLOWUP_SHADOW_WINDOW_SECONDS` | How far back the shadow-mode dry run in `patrol/followup/` counts comments as if follow-ups had been live, seven days. It never opens a live period, so nothing it counts is ever routed |
| `AUTO_REVIEW_WAIT_LIMIT_SECONDS` | How long the Owl Post keeps trying a handoff whose review can't start yet (Harry's run still going, the reviewer busy or another review of the task running) before you hear, four hours |
| `AUTO_REVIEW_MAX_TRIES` | Tries for an automatic review killed part way, 3, at most 9. A review that ends with a verdict or a refusal is never tried again by itself |
| `AUTO_CLOSE_CI_SETTLE_SECONDS` | How long after a merge is first seen before the closer counts green CI, or no checks, 30 minutes. A red stops it at once |
| `AUTO_CLOSE_MAX_TRIES` | The closer's automatic tries per merge commit for each sandboxed after-merge command and for the judge, 3. An automatic pass starts each of your own sessions' after-merge commands, which run without the sandbox, at most once |
| `AUTO_CLOSE_STALL_SECONDS`, `AUTO_CLOSE_UNKNOWN_GRACE_SECONDS` | When the closer tells you about a wait that keeps going (CI pending, open work, a busy judge), one day, and about a read that keeps failing, two hours |
| `RUN_SLOTS` | How many runs a desk may have going at once: Moody 2, Hermione 2 and every other desk 1, so two reviews of different tasks run at once. Each slot has its own lock, Codex work folder and temp folder, and the caps stay per desk across all of them. Raise it only for a desk whose runs never write the same file, at most 8 |
| `RUN_TIMEOUT_SECONDS` | How long one run may take |
| `TEMPUS_THRESHOLD` | Context size that triggers the Tempus warning |
| `SCRATCHPAD_BUDGET_BYTES` | The scratchpad size the PreCompact hook respects, and the size a rotation warns past |
| `SCRATCHPAD_ARCHIVE_DIR` | The folder next to a scratchpad or task pad where older Checkpoint blocks move, one file per month |
| `SCRATCHPAD_QUIET_SECONDS` | How long after its last write a scratchpad waits before a rotation takes it |
| `TASK_PAD_DESKS` | The desks that get one pad per task, `desks/<desk>/pads/<key>.md`: Hermione and Ron |
| `RUNNING_WINDOW_SECONDS` | How long a launch with no usage yet counts as running in the digest and `castle task board` |
| `DIGEST_MAX_LINES` | The length of McGonagall's startup digest |
| `PORTRAIT_EXPORT_MAX_BYTES`, `PORTRAIT_EXPORT_MAX_FACTS` | How much of the day the nightly export hands Dumbledore: 512KB of extracts and 300 current facts |
| `PORTRAIT_MCP_JOB` | `None` in the kit, so Dumbledore runs with no MCP server. For read-only chat, put an MCP job file naming your chat server at `~/.hogwarts/desks/portrait/mcp-chat.json` and set it to `"chat"`. His settings allow only that server's list and search tools and deny `send_message`, and his runs refuse every tool they don't allow |

The caps guard against runaway loops, so keep them well above a normal day. A cap day resets all at once, so a desk that is busy on both sides of the reset can use up to two days' cap within hours.

For one busy day, don't edit the file. Lift a single cap until the next reset:

```
~/.hogwarts/bin/castle desk caps
~/.hogwarts/bin/castle desk cap <desk> --runs +20
~/.hogwarts/bin/castle desk cap <desk> --spend +5
```

The cap event says whether the fleet's cap or your Claude or Codex plan's own limit stopped a run. A bump lifts only the fleet's cap.

## Change schedules

Each background job is a launchd plist in `~/.hogwarts/launchd/`. The Owl Post sweeps every 300 seconds (`StartInterval`) and also runs whenever an outbox changes. Ollivander's job, `com.hogwarts.ollivander`, runs once a day at 06:00 from one `StartCalendarInterval` entry with an `Hour` and a `Minute`. The other scheduled jobs use the same kind of entry. Gringotts' job runs every day at 23:30. The patrol's jobs and Dumbledore's add a `Weekday` key, with one entry per weekday and time. The Map's job, `com.hogwarts.map`, has one entry for every 15 minutes from 08:00 to 19:00 on weekdays. Each Map round also routes PR follow-ups and starts the closer, so follow-ups are routed and automatic closer passes start only in the Map's hours.

1. Edit the template in `~/.hogwarts/launchd/`.
2. Check it with `plutil -lint`.
3. Copy it to `~/Library/LaunchAgents/`, then `launchctl bootout gui/$(id -u)/<label>` and `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

With terminal loops (`~/.hogwarts/loops/jobs` is there, see [ONBOARDING.md](ONBOARDING.md#terminal-loops)), `fleet loops` reads the same templates and runs them on the same schedule, so step 3 is different: stop `fleet loops` with Ctrl+C and start it again. It runs only a template whose keys it knows (`ProgramArguments` with the wrapper line, `WorkingDirectory`, the two log paths, `Umask`, `EnvironmentVariables`, `RunAtLoad`, `StartInterval`, `StartCalendarInterval`, `WatchPaths`, `KeepAlive` as true or false, `ThrottleInterval`, `AbandonProcessGroup` and `ProcessType` as Background or Standard), and `fleet loops --dry-run` names any job it would refuse. A calendar entry may not have both `Day` and `Weekday`.

Keep the wrapper line, the `Umask` and the log paths as they are. The fleet tests check them, and a job that mints close tokens must never log its stdout.

## Keep the review rule intact

The one rule that matters most: **the author and the reviewer must come from different model families.**

- The store enforces it. A review counts as a pass only when the reviewer's family, looked up from the registry, differs from the author's. A same-family pass is refused, so a fleet with only one family can never pass a review.
- Keep at least one Claude reviewer (Hermione) and one Codex reviewer (Moody). Codex-written work goes to the Claude reviewer, and Claude-written work, including your own sessions, goes to the Codex reviewer. The same reviewer judges a task's written after-merge checks for auto-close, and the store refuses a proven close judged by the author's own family.
- If you move a builder to the other family, make sure a reviewer of the opposite family still exists for its work.
- Register every desk under the family of the model it actually runs. The family is what the store trusts.
- Ollivander never crosses families. A role card whose `family` differs from the registry is refused, and a Claude desk only ever holds a Claude model and a Codex desk a Codex one.

## Rename the Headmaster

The charter, the briefs and the agent files address the Headmaster by the kit's default name, the string `Ryan`. Change that prose freely. Two desk ids carry the same name: `ryan` (you, the human desk) and `ryan-claude-1` (your own Claude sessions in the castle). These are identifiers, not prose:

- `install.sh` registers both, and the fleet tests use both.
- `ryan-claude-1` is also in `INTERACTIVE_DESKS` and `OWN_SESSION_DESK` in `fleet/config.py`, and in `MANY_TASK_DESKS_SEED` in `hogwarts/db.py`.

To rename them, change every place together in your clone, run both suites, then reinstall with `./install.sh --force`.
