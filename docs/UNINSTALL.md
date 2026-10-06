# Uninstall

Everything the fleet adds to your Mac lives in two folders, a few background jobs and one agent file. Your own Claude and Codex settings only changed if you applied the pending snippets yourself, so undoing those is also yours. The uninstaller shows you exactly what to restore. It never edits those files.

## First, switch the automations off

Four files in the office switch on the fleet's automations: `auto-draft-pr`, `pr-followup`, `auto-close` and `auto-portrait`. They go with the office in step 5, but remove them before you start, whether you use the script or not:

```
rm -f ~/.hogwarts/auto-draft-pr ~/.hogwarts/pr-followup ~/.hogwarts/auto-close ~/.hogwarts/auto-portrait
```

Stopping the background jobs doesn't end a review or a closer pass that is already running, since each runs in a process of its own. With the files gone, each finds its switch off the next time it checks, so no draft PR, follow-up push or reply, proven close or memory addition starts after that.

The uninstaller never touches GitHub. Branches the review loop pushed, draft PRs it opened and replies it posted in your name stay there. Close or delete them on GitHub yourself if you don't want them.

## With the script

Run it from your clone of the repo.

```
cd ~/hogwarts-fleet
./uninstall.sh
```

With no flags it is a dry run. It prints every step it would take and changes nothing. Read it, then act:

```
./uninstall.sh --yes
```

It archives both folders before it removes them. If you are sure you want no archive, add `--no-archive` to `--yes`. On its own, `--no-archive` changes nothing.

It is safe to run again. A second run finds nothing left to do.

It exits with 0 when nothing is left for you, with 1 when your settings still mention the fleet or a step failed, and with 2 when it stopped at a worktree with uncommitted changes.

## The same steps by hand

1. **Stop the background jobs.** For each `com.hogwarts.*` job that is loaded, unload it, then remove its plist.

   ```
   launchctl list | grep com.hogwarts
   launchctl bootout gui/$(id -u)/com.hogwarts.owlpost
   rm ~/Library/LaunchAgents/com.hogwarts.owlpost.plist
   ```

   Repeat for any other `com.hogwarts.*` label the first command shows. Ollivander's job, `com.hogwarts.ollivander`, is one of them, so the script and this loop both cover it.

   The herdr spaces are not in either folder. They live in herdr itself, so close each one there. Run `herdr workspace list` to see them. The spaces the fleet opened are named like "Hermione - Staff Engineer", plus one each for the Owl Post and Ollivander. Closing a space ends what runs in it and nothing else.

2. **Check your settings.** These four files may mention the fleet: `~/.claude/settings.json`, `~/.claude/settings.local.json`, `~/.codex/config.toml` and `~/.codex/hooks.json`.

   ```
   grep -ni hogwarts ~/.claude/settings.json ~/.claude/settings.local.json ~/.codex/config.toml ~/.codex/hooks.json
   ls -1 ~/.claude/*.pre-hogwarts-* ~/.codex/*.pre-hogwarts-*
   ```

   If a file has a `.pre-hogwarts-<timestamp>` backup next to it, the newest one sorts last. Copy it back over the file:

   ```
   cp -p ~/.claude/settings.json.pre-hogwarts-<newest> ~/.claude/settings.json
   ```

   If there is no backup, remove the lines that mention the fleet by hand. You're done when the grep prints nothing.

3. **RTK.** If your settings mention `rtk hook` and you added it for the fleet, remove it with `rtk init -g --uninstall`.

4. **Git worktrees.** Harry's builds, Moody's reviews of your own commits and the closer's after-merge checks all use git worktrees under `~/hogwarts/worktrees`. Remove each one from its repo first, so the repo forgets it. Commit or discard any changes in it before you do.

   ```
   git -C ~/hogwarts/worktrees/<task> status --short
   git -C <the repo it came from> worktree remove ~/hogwarts/worktrees/<task>
   ```

   A worktree named `<task-id>.merged-<12 hex>` is the closer's, detached at a merge commit with no branch. Any changes in it are files its after-merge checks left. The script stops at it like any other worktree with changes. If you don't need those files, add `--force` to `worktree remove` to take it back.

5. **Archive, then remove the two folders.** Keep a private copy, check it lists both folders, then delete them.

   ```
   archive=~/hogwarts-fleet-archive-$(date +%Y%m%d-%H%M%S).tar.gz
   (umask 077; tar -czf "$archive" -C ~ .hogwarts hogwarts)
   tar -tzf "$archive" | grep -c -e '^\.hogwarts/' -e '^hogwarts/'
   rm -rf ~/.hogwarts ~/hogwarts
   ```

   The count must be more than zero before you run `rm`.

6. **Snape's agent file.** Remove `~/.claude/agents/snape.md` if it is the fleet's. The script only removes it when it is byte for byte the repo copy. Once you fill in its placeholders it differs, so the script leaves it and tells you.

   ```
   rm ~/.claude/agents/snape.md
   ```

7. **Optional: the tools.** Remove them only if nothing else you use needs them.

   ```
   brew uninstall rtk
   brew uninstall --cask codex
   brew uninstall ripgrep shellcheck
   ```

`./install.sh --force` may also have left `~/.hogwarts.pre-install-<timestamp>` or `~/hogwarts.pre-install-<timestamp>` folders. Delete them when you no longer need them.

## Restore from the archive

The archive holds both folders exactly as they were, including the database.

```
tar -xzf ~/hogwarts-fleet-archive-<timestamp>.tar.gz -C ~
~/.hogwarts/bin/castle doctor
```

Then switch the Owl Post back on with `sh scripts/owlpost-setup.sh` from your clone, and the patrol and Dumbledore's review with `sh scripts/patrol-setup.sh` and `sh scripts/portrait-setup.sh` if you had them on. Load Ollivander's job again as in [onboarding stage 5.5](ONBOARDING.md#55-ollivander-the-model-keeper), reopen the herdr spaces with `~/.hogwarts/bin/hogwarts-spaces`, and re-apply any settings you restored away in step 2.

`patrol-setup.sh` puts the patrol back in shadow mode when the archive has no `~/.hogwarts/patrol/shadow`, so delete that file again once you're ready, and follow-ups route nothing until you do. The four automation switches come back as the archive held them, so off if you removed them first. Switch on each one you want again with `echo on > ~/.hogwarts/<file>`, as in [CUSTOMISE.md](CUSTOMISE.md#switch-the-automations-on-and-off).

## Remove the repo

Your clone is an ordinary folder. Delete it when you no longer need it:

```
rm -rf ~/hogwarts-fleet
```

If you forked the repo on GitHub and no longer want the fork, open your fork, go to Settings, and use "Delete this repository" at the bottom of the page.
