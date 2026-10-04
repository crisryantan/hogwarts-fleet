# Customise the fleet

Everything here is an edit you make yourself, in your own terminal. No desk can change the office, and that is the point.

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

- The desk registry in the database is append-only by design. A desk's family, display name (its role) and model can't be updated or deleted once registered. So a registry change means either a new desk name or a fresh install. `./install.sh --force` moves the old office and castle aside, with their database, and installs fresh from the repo.
- If you want a change to survive the next install, make it in your clone of the repo too, and commit it there.

## Point the fleet at Claude and Codex

The fleet starts `claude` and `codex` by absolute path, never through `PATH`. The paths are two constants in `~/.hogwarts/fleet/config.py`:

```
CLAUDE_BIN = "/Users/you/.local/bin/claude"
CODEX_BIN = "/opt/homebrew/bin/codex"
```

`install.sh` checks the default and the usual Homebrew places, and sets them when it finds one. If you install either tool somewhere else, find it with `command -v claude codex` and edit those two lines.

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

What ships in the kit:

| Desk | Needs | Effort | Why |
| --- | --- | --- | --- |
| McGonagall - Chief of Staff | frontier | high | Scope and spec judgment, used rarely |
| Hermione - Staff Engineer | frontier | high | Deep reviews of Codex work |
| Moody - Security Reviewer | frontier | high | Security review of Claude work |
| Dumbledore - Knowledge Manager | frontier | medium | One nightly memory review |
| Snape - Data Analyst | workhorse | high | Accurate SQL at moderate cost |
| Harry - Senior Engineer | workhorse | high | Everyday coding |
| Ron - Release Engineer | fast | low | Sorts lots of PR and CI updates |

Edit the card, then run `~/.hogwarts/bin/fleet ollivander --dry-run` to read the new pick. His daily job applies it, or run `~/.hogwarts/bin/fleet ollivander` yourself. A move to the same tier or a cheaper one applies with a note. A costlier one waits for `castle desk model <desk> --approve`.

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

A Claude desk pins to an alias or a full `claude-` model id. A Codex desk pins to a slug from the last Codex catalog. The registry model and a `model =` line in a Codex desk's `codex.toml` are only what a desk runs before Ollivander has made his first pick.

After any switch, the first two runs are a trial. If both fail, the desk goes back to its previous model, pinned, and you get a note. A run that Claude's or Codex's own usage limit stopped never counts, and a switch you made yourself is never reverted.

## Block models your organization forbids

`BLOCKED_MODEL_PREFIXES` in `~/.hogwarts/fleet/config.py` is empty in the kit. Fill it with lowercase prefixes. Each is matched against Claude Code aliases, full Claude ids and Codex catalog slugs:

```
BLOCKED_MODEL_PREFIXES = ("<model-alias>", "claude-<model-alias>-")
```

To forbid a whole Claude line, list its alias and its full id prefix, as above. A blocked model is never picked, pinned, filed or launched. Ollivander skips it without a note. If every model of a tier is blocked, the desk keeps the one it has and you get one note. If its current model is blocked, `run_desk` won't launch the desk until you pin an allowed one. A trial that fails never reverts a desk onto a blocked model.

## Update the CLIs automatically

Off by default. Make the plain file `~/.hogwarts/desks/ollivander/update-clis` and each of Ollivander's passes first runs `claude update` and `brew upgrade --cask codex`, with the output in `~/.hogwarts/logs/ollivander-update.log`. Then he checks both `--version` commands and every enabled desk's dry run. Any failure stops every headless desk. So does a new Codex version, because the Codex permission boundary is proven per version: run `scripts/codex-boundary-test.sh` on it first. A new Claude Code version is only a note.

While the stop file `~/.hogwarts/state/ollivander-stop` exists, or an update is running, no headless desk launches. Run `~/.hogwarts/bin/castle ollivander clear` once you've looked. Remove `update-clis` to switch updates off again.

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

   Add the same line to `install.sh` and to `REGISTRY` in `office/tests_fleet/support.py`.
3. **Config.** In `~/.hogwarts/fleet/config.py`, add the name to `CASTLE_DESKS`, to `ROLE_DESKS` and to `HEADLESS_CLAUDE` or `HEADLESS_CODEX`. A Claude desk also needs entries in `CLAUDE_TOOLS`, `MAX_BUDGET_USD`, `CLAUDE_READ_DIRS`, `DAILY_RUN_CAP` and `DAILY_SPEND_CAP_USD`. A Codex desk needs `CODEX_ACCESS` and `DAILY_RUN_CAP`. The fleet tests check these sets agree with each other and with the registry.
4. **Office files.** Make `~/.hogwarts/desks/<name>/` (mode 0700) with a `BRIEF.md`, a `role.json` role card, plus `settings.json` for Claude or `codex.toml` for Codex (mode 0600). Change every path in the copied settings that names the old desk.
5. **Fence it in.** Every Claude desk's settings must deny sandbox writes to every other desk's folder, and `run_desk` refuses to launch one that doesn't. So add `"$HOME/hogwarts/desks/<name>"` (written out in full) to `sandbox.filesystem.denyWrite` in each other desk's `settings.json`. Add `Edit(~/hogwarts/desks/<name>/**)` and `Write(~/hogwarts/desks/<name>/**)` to the deny list in `~/hogwarts/.claude/settings.json`, so McGonagall can't write there either.
6. **Castle folder.** Make `~/hogwarts/desks/<name>/inbox` and `outbox` (mode 0700) and a `scratchpad.md` with `## Now`, `## Notes` and `## Checkpoint` headings (mode 0600).
7. **The Owl Post.** Add the new outbox to `WatchPaths` in `~/.hogwarts/launchd/com.hogwarts.owlpost.plist`, run `plutil -lint` on it, copy it to `~/Library/LaunchAgents/`, then reload it with `launchctl bootout` and `launchctl bootstrap` as in `pending/b-owlpost-launchctl.txt`.
8. **Routing.** Tell McGonagall when to use the new desk: add a line to the Routing section of her agent file.
9. **Avatar.** Draw an SVG that follows the style rules in `~/.hogwarts/assets/avatars/README.md`, and export transparent PNGs at 512, 128 and 64.
10. **Live view.** Add a line for the desk to the `spaces()` list in `~/.hogwarts/bin/hogwarts-spaces`, labelled "Character - Job" and running `fleet feed --desk <name>`. Never give a desk that can run commands a live session there.
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
| `MAX_BUDGET_USD` | The most one headless Claude run may spend, passed as `--max-budget-usd` |
| `DAILY_RUN_CAP` | Runs per desk in one cap day. The kit sizes it for a busy day: Hermione 80, Ron 120, the portrait 3, Harry 40 and Moody 80 |
| `DAILY_SPEND_CAP_USD` | Spend per Claude desk in one cap day, read from the store's metrics: Hermione $60, Ron $10 and the portrait $4. The Codex desks have no spend cap |
| `CAP_RESET_UTC_SECONDS` | When the cap day starts. `None` is local midnight on your Mac, daylight saving included. A number fixes the reset that many seconds after UTC midnight instead |
| `CAP_WARN_FRACTION` | The share of a cap, 0.8, at which a desk sends one warning that day |
| `REVIEW_ROUND_CAP` | Review rounds per author task, 3. The next one waits for `castle task allow-round <task-id>` |
| `RUN_TIMEOUT_SECONDS` | How long one run may take |
| `TEMPUS_THRESHOLD` | Context size that triggers the Tempus warning |
| `SCRATCHPAD_BUDGET_BYTES` | The scratchpad size the PreCompact hook respects |
| `DIGEST_MAX_LINES` | The length of McGonagall's startup digest |

The caps guard against runaway loops, so keep them well above a normal day. A cap day resets all at once, so a desk that is busy on both sides of the reset can use up to two days' cap within hours.

For one busy day, don't edit the file. Lift a single cap until the next reset:

```
~/.hogwarts/bin/castle desk caps
~/.hogwarts/bin/castle desk cap <desk> --runs +20
~/.hogwarts/bin/castle desk cap <desk> --spend +5
```

The cap event says whether the fleet's cap or your Claude or Codex plan's own limit stopped a run. A bump lifts only the fleet's cap.

## Change schedules

Each background job is a launchd plist in `~/.hogwarts/launchd/`. The Owl Post sweeps every 300 seconds (`StartInterval`) and also runs whenever an outbox changes. Ollivander's job, `com.hogwarts.ollivander`, runs once a day at 06:00 from one `StartCalendarInterval` entry with an `Hour` and a `Minute`. The later-stage jobs use the same kind of entry, one per weekday and time.

1. Edit the template in `~/.hogwarts/launchd/`.
2. Check it with `plutil -lint`.
3. Copy it to `~/Library/LaunchAgents/`, then `launchctl bootout gui/$(id -u)/<label>` and `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

Keep the wrapper line, the `Umask` and the log paths as they are. The fleet tests check them, and a job that mints close tokens must never log its stdout.

## Keep the review rule intact

The one rule that matters most: **the author and the reviewer must come from different model families.**

- The store enforces it. A review counts as a pass only when the reviewer's family, looked up from the registry, differs from the author's. A same-family pass is refused, so a fleet with only one family can never pass a review.
- Keep at least one Claude reviewer (Hermione) and one Codex reviewer (Moody). Codex-written work goes to the Claude reviewer, and Claude-written work, including your own sessions, goes to the Codex reviewer.
- If you move a builder to the other family, make sure a reviewer of the opposite family still exists for its work.
- Register every desk under the family of the model it actually runs. The family is what the store trusts.
- Ollivander never crosses families. A role card whose `family` differs from the registry is refused, and a Claude desk only ever holds a Claude model and a Codex desk a Codex one.

## Rename the Headmaster

"Ryan" appears in prose in the charter, the briefs and the agent files. Change that text freely. Two registry names also carry it: `ryan` (the human) and `ryan-claude-1` (your own Claude sessions in the castle). Those are wired into `INTERACTIVE_DESKS` and `OWN_SESSION_DESK` in `fleet/config.py`, into `install.sh` and into the fleet tests. Rename them together in your clone, then reinstall with `./install.sh --force`.
