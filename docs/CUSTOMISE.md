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

- **McGonagall and Snape** take their model from the `model:` line at the top of their agent files: `~/hogwarts/.claude/agents/mcgonagall.md` and `~/.claude/agents/snape.md`. Edit that line. Their registry row is only a label.
- **Hermione, Ron and the portrait** are launched with the model in their registry row. Change the `--model` on their `add_desk` line in `install.sh`, then reinstall with `./install.sh --force`. Also update the same desk in `office/tests_fleet/support.py` so the tests describe the real registry.
- **Harry and Moody** have no model in the registry. Add a top-level line to the desk's Codex profile, `~/.hogwarts/desks/<desk>/codex.toml`, above the dotted keys:

  ```
  model = "the-model-name"
  ```

  `run_desk` passes it to Codex as `-c model=...`. It refuses any profile line that sets the sandbox, turns network access on or names a bypass.

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
3. **Config.** In `~/.hogwarts/fleet/config.py`, add the name to `CASTLE_DESKS` and to `HEADLESS_CLAUDE` or `HEADLESS_CODEX`. A Claude desk also needs entries in `CLAUDE_TOOLS`, `MAX_BUDGET_USD`, `CLAUDE_READ_DIRS`, `DAILY_RUN_CAP` and `DAILY_SPEND_CAP_USD`. A Codex desk needs `CODEX_SANDBOX` and `DAILY_RUN_CAP`. The fleet tests check these sets agree with each other and with the registry.
4. **Office files.** Make `~/.hogwarts/desks/<name>/` (mode 0700) with a `BRIEF.md`, plus `settings.json` for Claude or `codex.toml` for Codex (mode 0600). Change every path in the copied settings that names the old desk.
5. **Fence it in.** Every Claude desk's settings must deny sandbox writes to every other desk's folder, and `run_desk` refuses to launch one that doesn't. So add `"$HOME/hogwarts/desks/<name>"` (written out in full) to `sandbox.filesystem.denyWrite` in each other desk's `settings.json`. Add `Edit(~/hogwarts/desks/<name>/**)` and `Write(~/hogwarts/desks/<name>/**)` to the deny list in `~/hogwarts/.claude/settings.json`, so McGonagall can't write there either.
6. **Castle folder.** Make `~/hogwarts/desks/<name>/inbox` and `outbox` (mode 0700) and a `scratchpad.md` with `## Now`, `## Notes` and `## Checkpoint` headings (mode 0600).
7. **The Owl Post.** Add the new outbox to `WatchPaths` in `~/.hogwarts/launchd/com.hogwarts.owlpost.plist`, run `plutil -lint` on it, copy it to `~/Library/LaunchAgents/`, then reload it with `launchctl bootout` and `launchctl bootstrap` as in `pending/b-owlpost-launchctl.txt`.
8. **Routing.** Tell McGonagall when to use the new desk: add a line to the Routing section of her agent file.
9. **Avatar.** Draw an SVG that follows the style rules in `~/.hogwarts/assets/avatars/README.md`, and export transparent PNGs at 512, 128 and 64.
10. **Test, then switch on.** Run both suites and `castle doctor`. Read the desk's `--dry-run` command (onboarding stage 5), then create its `enabled` file.

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
| `DAILY_RUN_CAP` | Runs per desk in any 24 hours |
| `DAILY_SPEND_CAP_USD` | Spend per Claude desk in any 24 hours, read from the store's metrics |
| `RUN_TIMEOUT_SECONDS` | How long one run may take |
| `TEMPUS_THRESHOLD` | Context size that triggers the Tempus warning |
| `SCRATCHPAD_BUDGET_BYTES` | The scratchpad size the PreCompact hook respects |
| `DIGEST_MAX_LINES` | The length of McGonagall's startup digest |

## Change schedules

Each background job is a launchd plist in `~/.hogwarts/launchd/`. The Owl Post sweeps every 300 seconds (`StartInterval`) and also runs whenever an outbox changes. The later-stage jobs use `StartCalendarInterval` entries, one per weekday and time.

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

## Rename the Headmaster

"Ryan" appears in prose in the charter, the briefs and the agent files. Change that text freely. Two registry names also carry it: `ryan` (the human) and `ryan-claude-1` (your own Claude sessions in the castle). Those are wired into `INTERACTIVE_DESKS` and `OWN_SESSION_DESK` in `fleet/config.py`, into `install.sh` and into the fleet tests. Rename them together in your clone, then reinstall with `./install.sh --force`.
