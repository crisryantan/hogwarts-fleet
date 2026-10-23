# Hogwarts store

The Hogwarts store is the single source of truth for the Hogwarts fleet. It holds desks, tasks, peer requests (owls), outcomes, review passes, close tokens, session memory (the Pensieve), curated facts and run metrics.

It is a small Python 3.9+ package that uses only the standard library, plus a CLI named `castle` that prints JSON. The package starts nothing by itself: it has no daemon, hook or job of its own. The fleet's scripts, hooks and launchd jobs are what use it. `install.sh` copies this file to `~/.hogwarts/README.md`.

## Trust model

Nothing a desk runs can call the store, and desks (the agents) have no file access to this directory. Only your terminal and the fleet's scripts and hooks use it, and those run outside every desk sandbox:

- the Owl Post, and `run_desk`, which starts each headless run;
- the patrol scripts: the Map, the morning lineup, keeper's watch and the scoreboard;
- Gringotts and Ollivander;
- the nightly Pensieve export, which runs Dumbledore after it;
- the worktree, verify, review and push scripts, `fleet feed` and `fleet go-wait`;
- the push gate;
- the castle's session hooks, including the go, close-token, SessionEnd and Stop (the chat watch) hooks.

Sender identity is a parameter that the calling script supplies. The Owl Post derives it from the desk outbox directory a file came from. The store still validates every input strictly, as defence in depth.

File modes (0700 directories, 0600 files) keep other users out. They do not stop processes that run as the same user. Keeping agents away from this directory is the job of the agent sandbox.

### Left out on purpose

The store has none of these:

- herdr pane and tab bookkeeping;
- typing into terminals;
- session ids parsed from free text fields;
- file locks held across waits;
- paths taken from the environment.

## Layout

```
~/.hogwarts                             mode 0700
  README.md                             this file
  .gitignore                            ignores state/, logs/, runs/, locks/, backups/, patrol/ except patrol/shadow,
                                        loops/running.json and chat-reviews/
  bin/castle                            POSIX sh wrapper, mode 0700
  bin/fleet                             POSIX sh wrapper for the fleet command, mode 0700
  bin/hogwarts-spaces                   opens one herdr space per desk, after fleet/agent_gate.py checks the live ones
  bin/fleet-loops.command               runs fleet loops in a Terminal.app window, for a Login Item
  desks/<desk>/                         each desk's office folder: for a headless desk, BRIEF.md, role.json (its role card),
                                        and settings.json (Claude) or codex.toml (Codex)
  desks/<desk>/enabled                  a headless desk runs only while this plain file exists
  desks/hermione/chat-review-settings.json  the locked-down settings of Hermione's chat review turns
  chat-app/Code.gs, chat-app/appsscript.json  the Apps Script source every Chat app uses (it does nothing), pasted into
                                        each app's project with only the manifest's name and logo changed
  desks/<desk>/live-tools.json          for mcgonagall and snape: every tool their live herdr session may have, by exact name
  hogwarts/__init__.py                  version string
  hogwarts/errors.py                    error classes and exit codes
  hogwarts/ids.py                       id generation, strict validators, path roots
  hogwarts/db.py                        connect, read-only connect, schema, migrations, transactions, doctor
  hogwarts/pensieve.py                  desks, tasks, commits, events, sessions, extracts, key points, facts, metrics, scrub
  hogwarts/facts.py                     fact validity windows, subject keys, volatility lint, as-of reads, approved patches
  hogwarts/owlery.py                    owls, requests, review passes, close tokens, purge, audit
  hogwarts/capacity.py                  the cap day, cap bumps, cap hits, review rounds and round allowances
  hogwarts/wands.py                     Ollivander's ledger: model filing, catalogs, each desk's model, trials, alias resolutions, the stop file
  hogwarts/watch.py                     read-only queries behind fleet feed
  hogwarts/cli.py                       argparse CLI, JSON output
  fleet/                                the fleet scripts: run_desk.py, worktree.py, verify.py, review.py, push.py,
                                        owl_post.py, ollivander.py, feed.py, go_wait.py (fleet go-wait, read-only),
                                        tools.py (the fleet command), the patrol
                                        (patrol.py, map.py, morning.py, keeper.py, scoreboard.py), gringotts.py,
                                        portrait.py (the nightly export and Dumbledore's run), portrait_patch.py (his
                                        patches, behind castle portrait), go_confirm.py (finishes a go or a close
                                        the prompt hook could not confirm yet), bulk_close.py (Mischief managed
                                        everything), plain_close.py (close <task-id> and drop <task-id>),
                                        adopt.py (fleet adopt), loops.py
                                        (fleet loops, the background jobs run from a terminal), failover.py (the
                                        model breaker and in-family fallback), orchestrator.py (McGonagall's typed
                                        next step), owl_report.py (her one-line owl reports), phone.py (loud events to
                                        the phone), scratchpad.py (keeps each scratchpad and task pad to its latest
                                        Checkpoint, older ones archived), chat_review.py (PR reviews teammates ask
                                        for in Google Chat, and fleet review pr), google_chat.py (Chat read
                                        with your read-only tokens, replies sent with each app's) and their helpers
  fleet/hooks/                          the castle's session hooks and the push gate, push_gate.py; stop.py is
                                        McGonagall's chat watch, which wakes her with a go task's line when it moves,
                                        in the session that typed its go (go_owners.py keeps which session watches
                                        which go task; a session that starts takes over those of one that ended,
                                        and a whole message "watch <task-id>" moves one to that session)
  launchd/                              the launchd plist templates, one per background job; fleet loops reads them too
  loops/jobs                            only with terminal loops: the jobs fleet loops runs instead of launchd, one per line
  loops/running.json                    while fleet loops runs: its pid and the jobs it runs
  pending/                              settings snippets and launchctl steps you apply yourself (see pending/README.md)
  assets/avatars/                       each desk's SVG badge, with PNG exports in png/
  tests/                                the store's unittest suite
  tests_fleet/                          the fleet's unittest suite
  run_suites.py                         runs both test suites fast, one process per test module
  state/pensieve.db                     the real database, created by castle init
  state/model-breaker.json              each model's breaker (down after two outage, overload or rate limit failures
                                        in a row, for 15 minutes, then one probe), fallback runs and waiting owls
  state/model-ladders.json              Ollivander's fallbacks per headless desk and family, in pick order
  state/ollivander-stop*                Ollivander's stops: ollivander-stop holds every headless desk, and
                                        ollivander-stop-claude or ollivander-stop-codex only that CLI family's
  logs/                                 the fleet's job and script logs, review-auto.log for the review loop and
                                        closer.log for auto-close, one JSON line per task each pass
  runs/<desk>/                          each headless run's output, which fleet feed follows, and how its process
                                        ended (<run>.end), kept as soon as it ends
  locks/                                lock files for run slots, launches, reviews, the review loop, the patrol,
                                        Ollivander's updates and the closer (closer.lock)
  worktrees/<task>.json                 each worktree's office record, and <task>.merged-<12 hex>.json for the
                                        closer's detached worktree at a merge commit, taken back once the task closes.
                                        The record stays once its worktree is removed. Removal markers next to it:
                                        <task>.closing, the closer's intent written before its close commits,
                                        <task>.removing while a removal is under way (who started it, the HEAD it
                                        checked and the removal's own identity), <task>.removed once it is done and
                                        the round row that told it, and <task>.unreported until the worktree
                                        cleanup's round row has named it
  reviews/<task>/                       review files and verify evidence, which no desk can write, what each review
                                        round was opened for (round-<request>.json), the review loop's record of
                                        each handoff (auto-<owl>.pending, .try<n>, .done) and of what follows each
                                        verdict of its own rounds and of each round you run by hand, but a PR
                                        follow-up's (after-<request>.json), and each PR follow-up's threads file
                                        (followup-<id>.md), which the castle copy is written from.
                                        Every TASK.md a verify read (task-md-<sha256>.md), and for your own sessions'
                                        tasks the digest fleet review own approved (task-md-approved). The closer's
                                        record (close.json), its O_EXCL try and clear markers (one per after-merge
                                        command, close-<sha>.AC-<n>.cmd-try<k>, then close-<sha>.judge-try<k> and
                                        close-clear<k>), and per merge commit each after-merge command's result as
                                        it ends, the after-merge evidence, the judge's pack and verdict, and the
                                        close evidence (after-merge-results-<sha>.json,
                                        after-merge-evidence-<sha>.md, after-merge-pack-<sha>.md,
                                        after-merge-review-<sha>-<desk>-<run>.md, close-evidence-<sha>.md)
  auto-draft-pr                         while it holds exactly "on", the review loop pushes and opens a draft PR after
                                        its PASS, and after a PASS of fleet review <task-id> of the newest handoff
                                        (off when missing)
  auto-portrait                         while it holds exactly "on", Dumbledore's weeknight job applies the additions
                                        in the patch his run wrote that night (off when missing, so removing it
                                        switches it off)
  pr-followup                           while it holds exactly "on" and the patrol is out of shadow mode, teammates'
                                        review comments on PRs the loop opened go back to Harry, and the loop pushes
                                        to the same PR and posts his replies after their PASS (off when missing)
  auto-close                            while it holds exactly "on", each Map round starts the closer, which closes a
                                        passed task once scripts prove its merge, CI and after-merge checks, and never
                                        one with a PR follow-up still open (off when missing). A build it closes loses
                                        its worktree in the same pass when nothing in it can be lost
  worktree-cleanup                      while it holds exactly "on", each Map round removes the worktree of each build
                                        task closed at least three days ago, by any path, once it has no uncommitted
                                        changes, no git-ignored files but the fleet's own dependency links, and its
                                        HEAD is on origin's base or branch (off when missing)
  cross-family-failover                 while it holds exactly "on", a desk whose whole family is down may run on the
                                        other family when it has launch settings for it and is neither a build desk
                                        nor a reviewer; a review that would then be same-family waits (off when
                                        missing: the desk waits)
  owl-reports                           while it holds exactly "on", a read-only headless McGonagall turn writes a
                                        one-line report on each owl another desk sends her (off when missing)
  auto-orchestrate                      while it holds exactly "on", an owl to McGonagall, a build's review verdict or
                                        a build's newest run dying before any work (never started, or timed out or
                                        failed with no tokens used) wakes one turn that picks the next step as one
                                        typed action a script checks, at most 6 wakes per task and 30 a day; a turn
                                        that failed or timed out is woken once more on the next pass, and a fix
                                        round that died idle may be started again until 2 of the build's newest runs
                                        in a row died that way, after which it is yours (off when missing)
  chat-review                           while it holds exactly "on", the chat-review job reads each served app's
                                        Chat conversations each minute and its desk (Hermione, or Moody for the
                                        second app) reviews the PRs teammates ask for there (off when missing: no
                                        token is minted and nothing is read)
  chat-review-github                    while it holds exactly "on" too, each chat review is also posted on its PR as a
                                        review with your gh login, once per app, PR and head commit:
                                        REQUEST_CHANGES with a blocking finding, else COMMENT (off when missing)
  chat-review-approve                   while it holds exactly "on" as well, a chat review that passes with no
                                        blocking finding is posted as APPROVE, your gh account's own approval for
                                        branch protection (off when missing, and nothing while chat-review-github is
                                        off)
  chat-reviews/                         one record per message and app it asked something (requests/), each app's
                                        read markers per conversation (read/), the once-per-head-commit GitHub markers
                                        of each app (github/) and each cap day's counts (caps/)
  patrol/shadow                         while it is here, the patrol and Gringotts only write files (shadow mode)
  patrol/<job>/                         the Map's snapshot and rows, lineups, keeper's watches, scoreboards, bot passes,
                                        and in shadow mode what a follow-up would have routed (patrol/followup/)
  backups/                              Gringotts' archives, mode 0600, kept 14 days, never synced anywhere
```

## The `bin/castle` wrapper

`bin/castle` is exactly:

```
#!/bin/sh
exec /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "<home>/.hogwarts"); from hogwarts.cli import main; sys.exit(main())' "$@"
```

`<home>` stands for your home folder. `install.sh` writes your home folder's absolute path here, so the wrapper reads no environment variable.

`bin/fleet` is the same line with `fleet.tools` in place of `hogwarts.cli`.

- `env -i` clears the environment before the interpreter starts. `/usr/bin/python3` is the macOS xcrun shim, which reads `DEVELOPER_DIR` before Python runs, so `-I` alone cannot stop a caller's environment from choosing the interpreter.
- `-I` ignores `PYTHON*` variables and the user site directory.
- `-B` stops new bytecode being written. It does not stop Python loading bytecode that is already there.
- `-X pycache_prefix=/var/empty` points bytecode lookups at an empty root-owned directory, so a planted `.pyc` is never loaded in place of the reviewed source.
- `castle doctor` fails if any `__pycache__` directory, `.pyc` file or extension module (`.so`) exists under this directory, since each can shadow a `.py` file.

Every fleet script, hook and launchd plist that calls the store must use the same interpreter line.

## Path roots

The store never opens a path. Paths are validated, stored, and handed back. Each path field must sit under a fixed root:

| Field | Root |
| --- | --- |
| `owls.body_path` | under `~/hogwarts/desks/<sender>/outbox/` |
| `tasks.intent_path` | exactly `~/hogwarts/tasks/<task_id>/TASK.md`, for that task's own id |
| `tasks.worktree` | under `~/hogwarts/worktrees/` |
| `review_passes.review_path` | under `~/.hogwarts/reviews/`, in the office, which no desk can write |

Here `~` stands for your home folder. The roots are constants in `hogwarts/ids.py` that `install.sh` points at your home folder, and the store checks and keeps absolute paths. It never expands `~` itself.

The first three sit in the castle (`~/hogwarts`), where desks work. Review files sit in the office (`~/.hogwarts`), so a desk cannot write or swap a review the store points at.

Paths must be absolute and already normalised, with no control characters, so `..`, `.` and repeated slashes are refused. Whoever later opens one of these files (a fleet script or a desk) must open it with `O_NOFOLLOW` and check that the resolved path is still under its root.

The CLI reads two kinds of file:

- `castle portrait show` and `apply` read Dumbledore's dated patch from his castle outbox through `fleet/portrait_patch.py`, which opens it with no link anywhere on the way and holds it to a strict schema (see Dumbledore's patches below).
- `castle fact apply --file PATH` reads an ops file, with these checks:
  - The path must be absolute and normalised.
  - Every directory above it, up to `/`, must be a real directory (not a symlink) owned by root or the current user, and must not be group or world writable unless it has the sticky bit, like `/private/tmp`.
  - The file is opened with `O_NOFOLLOW` and `O_NONBLOCK`, so a symlink or a FIFO is refused without blocking.
  - It must be a regular file owned by the current user, not group or world writable, and at most 256KB.
  - It must be UTF-8 JSON with no repeated keys inside an object and no `NaN` or `Infinity`.
  - With `--sha256 HEX`, the bytes read must hash to that value, or nothing is applied. Pass the hash taken when the file was reviewed, so the reviewed bytes are the applied bytes.

## Security rules

- No environment variables are read anywhere, including `Path.home()`, `expanduser` and `tempfile.gettempdir()`. The database path is the constant `DEFAULT_DB`. The CLI has no `--db` flag. Tests inject a path through `main(argv, db_path=...)`.
- `connect(path)` refuses a symlinked database or parent directory. It refuses a parent or database file that is group or world writable or owned by another user. It creates the parent with 0700 and the file with 0600, and tightens the `-wal` and `-shm` files to 0600.
- A `-wal` or `-shm` file that is a symlink, not a regular file, group or world writable, or owned by another user is refused with `IntegrityError` before SQLite opens the database. If one is planted after that check, SQLite only says it is unable to open the database, so that error is mapped to `IntegrityError` naming the sidecar.
- A `now` argument, and any other timestamp that defaults to now, must be a whole number from 0 to 253402300799 (the end of year 9999), so adding a duration such as a close token's lifetime cannot overflow SQLite's 64 bit integers.
- Every connection sets `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `secure_delete=ON` and `trusted_schema=OFF`. Switching a fresh file to WAL is retried briefly, because SQLite does not wait on a lock for that switch. A lock that outlasts the retries becomes `ConflictError`.
- Every id and name is checked with `re.fullmatch` before use. Anything else raises `ValidationError`.
- SQL is always parameterized. SQL text comes only from string literals and module constants. Enum lists inside the schema come from internal constants.
- Text is limited by field. NUL bytes are rejected. Other control characters are stripped, except newline and tab in multi-line fields. Unicode format characters (category Cf, such as zero width spaces and bidi controls) are stripped too, so they cannot hide a word from the volatility lint or the scrubber. Owl bodies are the one exception: they keep these characters, and CLI output escapes them. Titles, subjects and summaries are single line.
- FTS5 queries are rebuilt from quoted phrase tokens, so search syntax in user text has no effect.
- Extracts and key points are scrubbed before storage. Private key blocks, URL credentials, JWTs, `Authorization` and bearer headers, GitHub, Slack and Anthropic or OpenAI style tokens, `key=value` secrets (password, pwd, secret, token, api_key and similar), AWS key ids, emails, IP addresses and long hex strings become typed placeholders such as `[email]`.
- Close tokens are returned once. Only their sha256 is stored. They are single use, expire, and are consumed in the same transaction as the close. A database trigger refuses `close_reason = 'complete'` unless the task has a consumed token, its parent closed as complete, or it has a `task_closures` row (a proven close).
- A review must cite the task that recorded the commit. Families are always looked up from the database. A PASS needs a reviewer from `claude`, `codex` or `human` whose family differs from the author's. A non-human reviewer also needs a review request from the author's task.
- A fact's `lookup` is the command or URL that fetches the live value. It is stored as single line text and never executed. A lookup that the scrubber would change (a token, a password, URL credentials, an email, an IP address or a hex string of 32 or more characters) is refused, and the error names what matched. A lookup must also be either an `https` URL with no credentials and no quotes, spaces, `$`, `;`, `|`, `<`, `>`, backticks or parentheses, or a `gh` or `bk` command whose words use only letters, digits and `._/:=@,-`. So no shell syntax can be stored. Anything that later runs a lookup must still treat it as untrusted text, check that the subcommand only reads, and never pass it through a shell.
- CLI output is `json.dumps(ensure_ascii=True)`. List views never include owl bodies.
- Close tokens and owl bodies never travel on argv. They come from stdin only.

## Schema summary (version 12)

All tables are STRICT when SQLite supports it. Timestamps are integer unix seconds.

| Table | Purpose |
| --- | --- |
| `schema_version` | Applied migrations. |
| `desks` | Name, family (`claude`, `codex`, `human`, `script`), role, model. Desks are immutable. `fleet` is reserved. |
| `tasks` | Status `queued`, `active`, `awaiting_close`, `closed`. One active task per session (a partial unique index), and one per desk unless the desk is in `many_task_desks` (a trigger, so a raw write is refused too). A task keeps its desk (trigger), so no write moves an active task onto a single desk. Tasks are inserted queued. A closed task never reopens (trigger). `review_branch` (version 7) is the branch an own-session review task follows: set only on an active task and never cleared (triggers), NULL on rows from before version 7. It is a branch of your own checkout, so any name git takes as a branch counts, capitals included, held to 1 to 255 bytes of printable ASCII with no whitespace (a CHECK); the fleet's lowercase, fleet-word-free rule is only for the branches it makes and pushes. |
| `many_task_desks` | The desks that may hold many active tasks at once, with when each was granted. Version 7 grants `harry`, `hermione`, `moody`, `ron` and `ryan-claude-1` on a store that already has them, and `castle desk many-tasks` grants one. `mcgonagall`, `snape`, `portrait` and every human or script desk are refused, by the API and a trigger. One way: rows are never changed or deleted. Because `ryan-claude-1` takes many tasks, reviews of your own sessions on different branches never block each other, while a fix commit on a branch goes on that branch's open task. |
| `task_commits` | The repo and sha a task produced. One task per commit. Immutable. |
| `events` | Episodic events. Verdict `routine` or `headmaster`. Optional unique `dedupe_key`. |
| `sessions` | One row per agent session, with token counts. |
| `extracts` | Scrubbed session text with its own `created_at`, plus an FTS5 index kept in sync by triggers. |
| `keypoints` | Scrubbed key points with tags, with an FTS5 index. |
| `facts` | Curated facts. Tier `pinned`, `aging` or `perishable`. Each row has an optional `subject_key`, a world validity window (`valid_from`, `valid_to`), a belief window (`recorded_at`, `closed_at`), an `end_reason` (`superseded`, `withdrawn`, `expired`), `superseded_by`, `restores` (the earlier row a restored fact continues) and an optional `lookup`. A partial unique index (`facts_one_current`) allows one current fact per scope and subject key. Facts are archived, never deleted. |
| `facts_fts` | FTS5 index over fact text, kept in sync by triggers. |
| `metrics` | Per run token counts, cost and duration. A headless run's row is tied to its launch. |
| `owls` | Messages between desks. Kind `request`, `question`, `answer`, `result`, `fyi`. One answer per question. |
| `requests` | Peer requests with a forward only phase and an outcome. |
| `request_phases` | Phase history for each request. |
| `review_passes` | Review verdicts with author and reviewer families. Each one points at a recorded commit. Immutable. |
| `close_tokens` | Hashed single use tokens for closing a task as complete. |
| `cap_bumps` | One row each time you lift a desk's runs or spend cap with `castle desk cap`. It lasts until the next cap reset. Immutable. |
| `cap_hits` | One row each time a fleet cap refuses a run, or a vendor's own limit stops one. `cap_source` says which: `fleet`, `claude_plan` or `codex_plan`. Immutable. |
| `review_rounds` | One row per review request of an author task, with its round number, whether a newer commit superseded it, and the review it recorded. `followup_id` (version 11) names the PR follow-up the round belongs to: the store refuses any round while a follow-up of the task is still starting, and while one is building, a round that does not name it. It never changes. That review is stored and tied to its round in one step and never changes, so a round with a verdict counts even if publishing the review afterwards failed. `slot` is the reviewer desk's run slot the review held when the round opened, set once in the row that opens it, so the review script knows whose lock to try before it closes that round's reviewer task. It is NULL for a round queued while every slot was busy and for rounds from before version 8, and the script reads NULL as slot 0. |
| `run_launches` | One row per headless run, written before its process starts, so the run counts toward the daily run cap even if it is killed before it records usage. Its usage is the `metrics` row tied to it once it ends, set once. `task_id` is the desk's own task the run was for, when it had one, and never changes. Never deleted. |
| `auto_patches` | One row per local date Dumbledore's weeknight job took on with auto-portrait on (version 10): its attempt number, state (`armed`, `validated`, `done`, `stopped` or `off`), export owl, what his outbox held for the date just before his run (`absent`, `present` with its sha256, or `unreadable`), then one snapshot of the patch his run wrote (its sha256, the checked additions as canonical ASCII JSON of at most 1MB, and the op ids in patch order, held for you and out of schema), and the ending: one outcome line of printable ASCII and, for `done`, the ops it applied. No raw patch bytes are ever stored. Triggers keep it to its shapes: a row opens armed as attempt 1, the snapshot is written once and only as the night is validated, the date never changes, states only move forward, a night is armed again (with the next attempt) only while it took no snapshot, an ending is cleared only by that, and rows are never deleted. |
| `task_specs` | What your go approved for a task (version 9): the repo folder, branch and base its TASK.md Spec names, and the sha256 of the TASK.md bytes the go read. One row per task, written in the transaction that registers it, only while it is queued and has its TASK.md, and never changed or deleted (triggers). A task registered with `castle task create` has none. The worktree script makes a worktree under that TASK.md only from these values. |
| `round_allowances` | One row each time you allow another review round with `castle task allow-round`. `followup_id` (version 11) is the PR follow-up that was open when it was granted, set by the store (a trigger refuses any other), NULL for one granted while none was; it lifts only that group's cap. Immutable. |
| `task_prs` | The PR the review loop opened for a build task (version 11): repo, number, branch, base, the commit it opened at and its link. One row per task and one task per PR, whatever letter case the repo is written in, inserted only while the task awaits close with its worktree, and never changed or deleted (triggers). |
| `followup_live` | When PR follow-ups were live, as Map rounds saw them: one row per period, at most one open, each closed once and never deleted. Every period is kept, so a comment written in an earlier live period stays routable. |
| `pr_followups` | One follow-up of one task: its number (counting up from 1), state (`routing`, `starting`, `building`, `pushing`, `posting`, `done`, `stopped`), the commit it starts from, its fix request owl (an fyi from `map` to the task's desk about that task), the commit that passed and why it stopped. States move only along their edges, `done` and `stopped` are final, and one follow-up per task is open at a time (triggers and a partial unique index). |
| `pr_followup_items` | What a follow-up asks the build desk to answer, labelled T1, T2 and on: a review thread (its id, replied to at its first comment), a review or a conversation comment, with its exact link on the PR and, for the last two, a quote taken from the comment scrubbed whole and kept only when it holds no link and passes every reply rule. Written only while the follow-up is routing, never changed. |
| `pr_comments` | Every GitHub comment a follow-up handled, keyed by repo, kind and id, so none is routed twice. Written only with an item of a routing follow-up of the same task, never changed. |
| `pr_replies` | Every reply, planned in the transaction that passes its follow-up on to the push or the posting, with its mark (`FIXED` or `PUSHBACK`) and exact text. A reply moves `planned`, then `posting` (only while its follow-up posts), then `posted` with its GitHub id, `failed` or `unknown`, and never back. |
| `task_closures` | A proven close (version 12): one row per task the closer closed, with the reviewed commit, the merge commit, how it landed (`pr` with its number, or `ancestry`), CI on the merge commit (`green` with its check count, or `none`), the after-merge command and written check counts, the judge desk, and the office close evidence's path and sha256. Kind `proven` for the task itself, `parent` for the go task closed with it on the same proof. Triggers hold every writer, raw SQL included: a row lands only on an open task; a `proven` row needs a task awaiting close whose commit it recorded and whose latest review of that commit is a PASS from the other family that a review round holds, and no PR follow-up of the task still open; a judge is of the other family; a `parent` row follows its proven child closed complete on the same proof, and only on a task a go registered. Rows are never changed or deleted. Only `pensieve.close_proven` writes them, and nothing in the CLI reaches it. |
| `model_lines` | How you filed a model name: `frontier`, `workhorse`, `fast` or `ignore`. The latest row per name wins. Immutable. |
| `model_catalog` | The model names each family offered at Ollivander's last look, with whether the catalog listed each, the tier it was filed under then and when it retires. Each look gets the family's next look number, and the latest look is the one with the highest number, so two looks in the same second never mix. A row's look number only moves forward. `castle desk model --approve`, a pin and a trial revert check against the latest look. |
| `desk_models` | Each Claude and Codex desk's role need, current model and effort, pin, pending pick and trial state, including how the last trial ended (`trial_end`: `passed`, `pinned`, `held`, `revert_blocked` or `reverted`). The `desks` table itself stays immutable. |
| `model_changes` | Every model switch, with its reason: `initial`, `role`, `pin`, `approved` or `revert`. Immutable. |
| `model_resolutions` | Every full Claude id each alias was seen to run as, with its first and last sighting. Only the last sighting moves, and rows are never deleted. |

Triggers also block deletes on desks, tasks, task commits, requests, events, facts, owls, review passes and every PR follow-up table. A task awaiting close goes back to active only in the transaction that opens a PR follow-up, once for that follow-up, and a task in a follow-up awaits close again only after a round of that follow-up passes (triggers on `tasks`). Fact triggers require `valid_from` and `recorded_at` on every row, and keep `valid_to`, `closed_at` and `end_reason` set or unset together, with `valid_to` no earlier than `valid_from` and `closed_at` no earlier than `recorded_at`. `superseded_by` is only set on a superseded row. `restores` never changes once written.

Migration 1 creates the base tables. The later ones:

- Migration 2 adds the fact columns, backfills `valid_from` and `recorded_at` from `created_at`, and builds the index and `facts_fts`.
- Migration 3 adds `facts.restores` and the trigger that keeps it fixed.
- Migration 4 adds the cap, review round and run launch tables.
- Migration 5 adds the model tables.
- Migration 6 adds the catalog look number and numbers the rows already kept by their `seen_at` order in each family.
- Migration 7 adds `many_task_desks` and its triggers, and grants the seeded desks that already exist. It replaces the one-active-task-per-desk index with a trigger that reads those grants, and adds `run_launches.task_id` and `tasks.review_branch`.
- Migration 8 adds `review_rounds.slot` and the trigger that keeps a round's slot fixed.
- Migration 9 adds `task_specs` and its triggers.
- Migration 10 adds `auto_patches` and its triggers.
- Migration 11 adds the PR follow-up tables and their triggers, `review_rounds.followup_id`, `round_allowances.followup_id`, and the triggers that let a passed task back to active only to start a follow-up.
- Migration 12 adds `task_closures` and its triggers, one of which refuses a proven close while a PR follow-up of the task is open, and lets the complete rule take a proven close.

Each column is added only while it is missing, so running a migration again changes nothing.

## Why facts work this way

A fact that changes is replaced explicitly with `supersede`, never guessed at. The replaced row is kept, with its validity window closed, so history and as-of reads still see it. A key's windows never overlap, so as-of reads give one answer per key. A closed row is never reopened. When a replacement is withdrawn, the earlier fact comes back as a new row, so belief history is never rewritten either. The default read (`current_facts`, `context_facts` for a desk, and `castle fact list`) returns only current facts: not superseded, withdrawn, expired or archived. Volatile state such as PR status, build colour or rollout percentage goes stale fast, so a fact whose text looks volatile is accepted only with a `lookup` that fetches the live value, or as a perishable fact that expires within 7 days.

The volatility lint matches words, not meaning, so it also catches ordinary lasting prose: "open question", "red flag", "open source", "closed-form", "go-live checklist", "patent pending", "released under MIT", "issue #42", "+16.1%" and "NEVER open ACME/legacyapp PRs" are all refused. This is on purpose, since a false refusal costs a rewrite and a missed volatile fact goes stale silently. The remedy for a lasting fact is to reword it ("unresolved question", "warning sign", "never create ACME/legacyapp PRs"). Do not add a placeholder lookup, and do not make a lasting rule perishable to get past the lint, because a perishable fact quietly drops out after a week. There is no override. Any importer, including one for MEMORY.md, must reword or skip, and never fall back to perishable.

## API

Every function takes a connection from `db.connect(path)` as its first argument. Functions that depend on time take an optional `now`.

- `hogwarts.db`: `connect(path, create=True)`, `connect_readonly(path)`, `migrate(conn)`, `pending_statements(conn, statements)`, `schema_version(conn)`, `transaction(conn)`, `snapshot(conn)`, `doctor(path, code_root=None)`, `stray_bytecode(root)`, `DEFAULT_DB`.
- `hogwarts.pensieve`
  - Desks: `add_desk`, `get_desk`, `list_desks` (with `many_tasks`), `allow_many_tasks(desk)`, `takes_many_tasks(desk)`, `blocking_task(desk)`.
  - Tasks: `create_task(desk, title, intent_path=None, parent_task_id=None, request_id=None, session_id=None, worktree=None, task_id=None)`, `record_spec(task, repo_dir, branch, base, intent_sha256)`, `task_spec(task)`, `start_task`, `mark_awaiting_close(task, repo=None, sha=None)`, `record_commit`, `get_commit`, `task_commits`, `check_review_branch(branch)`, `set_review_branch(task, branch)`, `close_task`, `close_proven(task, proof, summary, dedupe_key, parent_task_id=None)`, `task_closure(task)`, `open_descendants(task)`, `closed_ancestors`, `get_task`, `list_tasks(desk=None, status=None, open_only=False)`.
  - Auto-portrait nights: `arm_auto_patch(date, owl_id, before, before_sha256)`, `snapshot_auto_patch(date, sha256, ops_json, order_ids, held_ids, unfit_ids)`, `end_auto_patch(date, state, outcome, applied_ids=None)`, and read only `auto_patch(date)`, `open_auto_patches()` and `recent_auto_patches(limit=30)`, each row with its owl's `acked_at` as `owl_acked_at`.
  - Events: `add_event`, `drain(max_chars=1500)`, `ack(event_id)`, `events_with_key_prefix(prefix)` (read only, a plain text match on the dedupe key).
  - Memory: `record_session`, `get_session`, `add_extract`, `extracts_between(since, until, limit=2000)` (read only, with each session's desk and project), `add_keypoint`, `find(query, limit)`, `fts_query`, `fts_phrases`, `scrub(text)`.
  - Facts: `add_fact(scope, text, tier, source, expires_at=None, subject_key=None, valid_from=None, lookup=None)` (the same function as `facts.add_fact`), `touch`, `decay`, `archive_stale`, `archive`, `list_facts(scope=None, include_archived=False, include_closed=False)` (open rows, plus archived or closed rows when asked), `context_facts(desk)` (current fleet and desk facts).
  - Metrics: `add_metric`, `summary(since)`.
- `hogwarts.facts`
  - Writes: `add_fact`, `supersede(scope, subject_key, text, source, tier="aging", valid_from=None, lookup=None, expires_at=None)`, `withdraw(fact_id, desk=None)`, `expire()`, `set_key(fact_id, subject_key)`, `apply_ops(ops)`.
  - Reads: `current_facts(scope=None)`, `find_facts(query, scope=None, include_history=False, limit=10)`, `as_of_world(t, scope=None)`, `as_of_belief(t, scope=None)`, `history(scope, subject_key)`, `contradiction_candidates(since, limit_per_fact=3)`.
  - Lint: `VOLATILE_PATTERNS`, `volatile_match(text)`, `LOOKUP_COMMAND`.
- `hogwarts.capacity`: `day_bounds(now, reset_offset)`, `add_bump`, `active_bumps`, `list_bumps`, `cap_status`, `record_cap_hit`, `list_cap_hits`, `waiting_requests`, `record_launch(desk, run_id, model, task_id=None)`, `record_launch_usage`, `list_launches`, `newest_launch(task)`, `task_launches(task)` (with each launch's recorded tokens), `open_launches(desk)`, `open_review_round(..., slot=None, followup_id=None, followup_max_rounds=None)`, `record_round_verdict`, `review_rounds`, `stranded_rounds`, `allow_round` (says which group it lifts), `needs_allowance(task, max_rounds=3, followup_id=None)`, `review_task_ids`, `request_round(request)` (with the round's `followup_id`), `round_author(reviewer_task)`, `in_flight(now, running_window, desk=None, max_rounds=3, followup_max_rounds=2)` (rows carry the open `followup`).
- `hogwarts.followups`: `bind_pr`, `pr_for_task`, `bindings`, `routed_thread_ids`, `handled(repo)`, `handled_in(repo)`, `posted_ids(task)`, `reply_bodies(task)`, `see_live(live)`, `current_live`, `live_periods`, `open_followup`, `get`, `by_owl`, `open_for_task`, `open_for(task)` (whether a task has a follow-up open; a store error raises, so a caller treats it as unknown), `count_for_task`, `list_followups(task=None, since=None, open_only=False)`, `items`, `comments`, `replies`, `show(task)`, `advance(followup, state, pass_sha=None, event=None)`, `stop(followup, reason, event=None)`, `abandon_routing`, `end_closed`, `plan_replies`, `begin_reply`, `end_reply`. A final state and its event are written in one transaction.
- `hogwarts.wands`: `classify`, `ryan_lines`, `record_catalog`, `last_catalog`, `catalog_entry`, `get_desk_model`, `list_desk_models`, `set_need`, `apply_model`, `set_pending`, `clear_pending`, `approve`, `pin`, `unpin`, `desk_choice`, `record_outcome`, `changes`, `base_alias`, `record_resolution`, `resolutions`, `resolved_id`, `blocked_resolution`, `blocked_resolutions`, `clear_stop`. The calls that file, pin, approve or apply a model take the fleet's `BLOCKED_MODEL_PREFIXES` and refuse a name one of them matches. Pin, approve, apply, a pending pick and a trial's revert also refuse an alias that ever ran as a full id one of them matches. A labelled alias such as `opus[1m]` shares the plain alias's resolutions. `approve` also refuses a pick that the latest stored catalog no longer lists, hides, files under another tier or shows retiring within 30 days. `pin` on the desk's current model ends its trial, and its result carries a `warning` when the latest catalog hides the model or shows it retiring soon. `record_outcome` never reverts a desk pinned since its switch, nor, while anything is blocked, onto no model at all, nor onto a model filed as ignore or one the latest catalog no longer lists, hides or shows retiring within 30 days.
- `hogwarts.watch` (read only, for a connection from `db.connect_readonly`): `marks`, `owls_after`, `headmaster_events_after`, `metrics_after`, `run_recorded`, `open_runs(desk)`.
- `hogwarts.owlery`
  - Owls: `send`, `inbox`, `read`, `ack`, `mark_delivered`.
  - Requests: `REQUEST_PHASES`, `open_request`, `advance`, `defer`, `decline`, `get_request`, `list_requests`, `request_owls`.
  - Reviews: `record_review`, `latest_review`, `has_pass`.
  - Tokens: `mint`, `consume`.
  - Retention: `purge(now, body_days=30, extract_days=90)`, `audit(now, escalate=False)`.

### Behaviour notes

- Writes run in `BEGIN IMMEDIATE` through `db.transaction`. A write helper nests inside another write transaction as a savepoint, so a nested helper that fails undoes its own changes even when the caller catches the error and commits. It refuses with `StoreError` inside a `snapshot` or a transaction the caller opened, and so does `consume`.
- `create_task` takes an optional `task_id`, so a fleet script can mint the id (`tk_` and 16 lowercase hex digits), write `tasks/<task_id>/TASK.md`, then register the task. An id that is already taken raises `ConflictError`. An `intent_path` needs that `task_id` and must be exactly `~/hogwarts/tasks/<task_id>/TASK.md`, written as an absolute path. Without a `task_id` the store mints one and the task has no intent path.
- `record_spec` records a go on a queued task that has its TASK.md, once. It keeps the repo folder absolute, normalised and outside the castle and the office, the branch and base to the plain shapes the fleet makes worktrees from, and the sha256 to 64 lowercase hex digits. The fleet checks them against git first.
- `arm_auto_patch` opens a night as attempt 1, or arms again, as its next attempt, a night that took no snapshot and is `armed`, `stopped` or `off`; any other night is a `ConflictError`. `snapshot_auto_patch` moves an armed night to `validated` once, with ops held to printable ASCII of at most 1MB and op ids to comma lists of their own shape. `end_auto_patch` ends an armed or validated night as `done`, `stopped` or `off` with one outcome line of at most 500 printable ASCII characters, and records the applied op ids only for `done`. Each writes in its own `db.transaction`, so it nests as a savepoint in the caller's.
- `start_task` only starts a queued task whose ancestors are all open. A session that already has an active task raises `ConflictError`, and so does a single desk (`blocking_task` names the task in the way). A desk granted many tasks with `allow_many_tasks` starts any number. `allow_many_tasks` refuses `mcgonagall`, `snape`, `portrait` and every human or script desk with `ValidationError`. `mark_awaiting_close` moves the task out of `active`, so a single desk can start its next one, and can record the head commit.
- `in_flight` lists every active or awaiting-close author task by desk, and any other task with a run going for it (a queued task whose ordinary request run the Owl Post started, shown as `running`).
  - Each entry has its latest round, verdict, rounds used against the cap, whether it needs an allowance, whether a run is going, and its state: `awaiting close`, `HEADMASTER`, `round cap`, `CHANGES`, `review died`, `review queued`, `in review`, `running` or `working`.
  - A reviewer's round task is folded into its author task.
  - Running means a launch for the task or one of its rounds' reviewer tasks has no usage yet and started within `running_window`.
  - The latest round says where a task stands even when it does not count. One with no verdict is `in review` while its run is going. It is `review died` once none is, or once its run ended without a verdict, so the task needs its review run again.
  - `needs_allowance(task, max_rounds=3)` says whether a task's next round waits for `castle task allow-round`.
- `close_proven` closes a task awaiting close on the closer's proof, with no token, in one transaction: its `proven` row and its close, then, with `parent_task_id`, the go task's `parent` row and its close, then one headmaster event. It refuses, changing nothing, a task with open work under it or a PR follow-up still open, a parent that is not the task's own, is closed, has no go spec or has other open work, and a proof with a field of the wrong shape. No close cascades to another task.
- `close_task(task, "complete", token)` needs a valid close token. `abandoned` and `superseded` need none. Closing a task also closes every open descendant:
  - a started descendant closes `complete` only when its parent closed `complete`;
  - every other descendant, including any queued one, closes `superseded`.
- A cascade moves each descendant request to `task_closed` and names the parent task in the history. The outcome is `done` only when the descendant closed `complete`, and stays unset otherwise.
- `open_request` creates the request, the recipient's queued task and the `request` owl in one transaction. Sending a `request` owl any other way is refused.
- `advance` moves only to the next phase and is a no-op at the current phase. `running` needs the task to have started. `result_posted` needs a result owl on the request. `task_closed` needs the task to be closed, and sets the outcome to `done` only when it closed `complete`.
- `defer` and `decline` work from `queued`, `claimed` or `running`. They set the outcome and close the recipient task as superseded, which supersedes its delegated sub work too.
- A result owl must come from the request recipient. An owl tied to a request must stay between its two parties, and its task must be the request task or its parent.
- With an idempotency key, owls and requests dedupe on the key, scoped to the sender. Reusing a key for a different payload raises `ConflictError`. The Owl Post must always pass a key derived from the outbox file.
- Without a key, content dedupe only folds into an owl that is still unacked or a request that is still in flight. The same content sent after that is a new owl or request.
- `read` is separate from `ack`. Only the recipient can read or ack. `ack` needs a prior `read`. `request_owls` lists one request's owls as metadata only.
- `drain` returns unacked headmaster events, newest per task first, cut to `max_chars`, with a `remaining` count. It does not ack.
- `decay` lists stale facts. Only open rows go stale: closed history keeps its window, so withdraw can still restore it. `archive_stale` archives whatever is stale at that moment, in one transaction, so a fact touched after `decay` stays live.
- `add_fact` with a `subject_key` that already has a current fact in that scope raises `ConflictError` and points at `supersede`. `valid_from` defaults to now and cannot be in the future.
- When a key has no current fact, `add_fact`, `supersede` and `set_key` refuse a `valid_from` earlier than the end of the key's history with `ConflictError`. The end is the latest `valid_to`, or `valid_from` for an open row, over the key's rows that were not withdrawn, archived rows included. So a new fact never lands inside closed history. An archived row that was never closed keeps an open window, so as-of reads can still show it beside a later fact for the same key.
- The volatility lint runs in `add_fact` and `supersede`. If the text matches a `VOLATILE_PATTERNS` entry (draft, open, merged, green, red, failing, live, rolled back or rolled-back, ramp, in progress or in-progress, blocked, pending, deployed, shipped, a percentage such as `50%` or `50 %`, `PR 123`, `#123`, `build 123` and similar) and there is no `lookup`, the fact must be perishable with `expires_at` at most 7 days after `valid_from`. So a pinned fact can only be volatile with a lookup. The error also says to reword a lasting fact.
- `supersede` runs in one transaction. It closes the current fact for the key (`valid_to` = the new `valid_from`, `closed_at` = now, `end_reason = 'superseded'`, `superseded_by` = the new id) and inserts the new one. A replacement that starts before the current fact raises `ConflictError`. A perishable that has lapsed counts as the current fact when the replacement starts before its `expires_at`, so it is closed as superseded. Otherwise it is closed as expired first. With no current fact it adds one. It returns `fact_id` and `superseded_id`.
- `withdraw` marks a fact as never true: an empty validity window and `end_reason = 'withdrawn'`. If it had superseded a fact that is not archived, that earlier fact is restored, unless another row for that key is still open or ends after the earlier fact was replaced:
  - the earlier row stays closed exactly as it was, with `superseded_by` still pointing at the withdrawn fact;
  - a new row continues it, with the same scope, subject key, text, tier, source, lookup and `expires_at`, `valid_from` set to the withdrawn fact's `valid_from`, `recorded_at` set to now, and `restores` set to the earlier row's id;
  - an earlier perishable that has lapsed by then is restored already closed as expired at its own `expires_at`, and the result has `reopened_expired` set;
  - a routine `fact_reopened` event is logged with dedupe key `fact-reopened:<earlier>:<withdrawn>`. It goes on the `desk` argument, or on the fact's own desk, so restoring a fleet fact needs `desk`.

  The result has `fact` (the withdrawn row), `reopened_id` (the earlier row), `restored_id` (the new row) and `reopened_expired`. Withdrawing a restored row withdraws only that row, since it superseded nothing. Withdrawing a closed fact raises `ConflictError`.
- `expire` closes perishable facts past `expires_at` with `end_reason = 'expired'` and `valid_to = expires_at`. A perishable fact that has lapsed but not yet been expired is closed the same way when a write claims its key.
- `as_of_world(t)` answers what was true at `t`, including later corrections and leaving out withdrawn facts. A perishable fact stops being true at its `expires_at`, whether or not `expire` has run. `as_of_belief(t)` answers what the store held at `t`. Neither axis is rewritten by a withdraw. On the world axis the restored row starts where the withdrawn fact started, so a key's windows stay contiguous with no overlap. On the belief axis the earlier row still ends when it was superseded, the withdrawn fact is held from when it was recorded until the withdraw, and the restored row is held from the withdraw on, so `as_of_belief` never shows two facts for one key.
- `find_facts` quotes search input like `find`. It searches current facts unless `include_history` is set.
- `contradiction_candidates(since)` is read only. For each current fact recorded since `since`, it lists up to `limit_per_fact` other current facts in the same scope with a different or no subject key, ranked by bm25 on the fact's words. It feeds the nightly review.
- `apply_ops` is the only write path for an approved nightly patch. Ops are `supersede`, `withdraw`, `set_key` and `archive`, each a JSON object with its own fields. Every op is validated first (unknown op or field, missing field, bad id, bad key, volatility lint, lookup form), then all run in one transaction, so one failure leaves nothing applied. That holds inside a caller's transaction too, since a nested call runs as a savepoint. Errors name the op index. `set_key` refuses a key that already has a current fact, and refuses closed facts. The fields are:
  - `supersede`: `scope`, `subject_key`, `text`, `source`, and optionally `tier`, `valid_from`, `lookup`, `expires_at`;
  - `withdraw`: `fact_id`, and optionally `desk`;
  - `set_key`: `fact_id`, `subject_key`;
  - `archive`: `fact_id`.
- `purge` clears bodies of acked owls older than `body_days`. It deletes extracts whose own `created_at` is older than `extract_days`. It then runs a TRUNCATE checkpoint so purged bytes leave the `-wal` file, and reports `wal_checkpoint_busy` when that could not finish. It never touches facts, tasks, events or reviews.
- `audit` is read only. It lists stale requests (60 minutes), owls to re-ring (30 to 120 minutes unacked), owls to escalate (120 minutes or more), owls never delivered after 30 minutes, active tasks older than 8 hours (on a many-task desk this includes a task waiting for a fix round), tasks awaiting close for 24 hours since they started, queued tasks under a closed parent, and review passes whose task is missing. With `escalate=True` it adds headmaster events with dedupe keys, so repeating it adds nothing.
- `has_pass(repo, sha)` finds the author through the commit's task. It is true only when that task is awaiting close or closed complete, and the latest review of the commit is a PASS from a different, PASS-capable family.

## CLI

All output is JSON. Errors go to stderr as JSON with the exit code below. `--help` prints argparse text.

```
castle init
castle doctor
castle desk add NAME --family F [--role R] [--model M]
castle desk list [--all | --limit N]
castle desk cap DESK (--runs +N | --spend +X)
castle desk caps [--all | --limit N]
castle desk many-tasks DESK
castle desk model DESK (MODEL | --role | --approve)
castle desk models [--all | --limit N]
castle model line NAME frontier|workhorse|fast|ignore
castle ollivander clear
castle task create --desk D --title T [--id TASK] [--intent-path P] [--parent TASK] [--request REQ] [--session S] [--worktree P]
castle task start|show TASK
castle task await-close TASK [--repo O/N --sha SHA]
castle task commit TASK --repo O/N --sha SHA
castle task close TASK --reason complete|abandoned|superseded [--token-stdin]
castle task list [--desk D] [--status S | --open] [--all | --limit N]
castle task builds [--all | --limit N]
castle task board [--desk D] [--all | --limit N]
castle task allow-round TASK
castle task rounds TASK [--all | --limit N]
castle followup list [--task TASK] [--all | --limit N]
castle followup show TASK
castle token mint TASK [--ttl SECONDS]
castle owl send --from D --to D --kind K --subject S [--body-path P | --body-stdin] [--task T] [--request R] [--reply-to OWL] [--key K]
castle owl inbox DESK [--all | --limit N]
castle owl read|ack OWL --as DESK
castle request open --from D --to D --title T [--body-path P | --body-stdin] [--parent TASK] [--key K]
castle request advance REQ PHASE [--detail TEXT]
castle request defer|decline REQ --reason R
castle request show REQ
castle request list [--desk D] [--phase P] [--open] [--all | --limit N]
castle review record --repo O/N --sha SHA --task TASK --reviewer DESK --verdict V [--review-path P]
castle review check --repo O/N --sha SHA
castle event add --desk D --kind K --verdict V --summary S [--task T] [--dedupe-key K]
castle event drain [--max-chars N]
castle event ack ID | --all | [--kind KIND] [--task TASK]
castle event settle
castle pensieve session SESSION --project P [--desk D] [--model M] [--started-at N] [--ended-at N] [--first-turn-tokens N] [--total-input-tokens N]
castle pensieve extract SESSION --role user|assistant (--text T | --text-stdin) [--seq N]
castle pensieve keypoint (--text T | --text-stdin) [--tags a,b] [--session S]
castle pensieve find QUERY [--limit N]
castle fact add --scope fleet|DESK --tier T --text T [--expires-at N] [--source S] [--subject-key K] [--valid-from N] [--lookup L]
castle fact touch ID
castle fact decay [--archive] [--all | --limit N]
castle fact archive ID [ID ...]
castle fact list [--scope S] [--archived] [--history] [--all | --limit N]
castle fact list --context DESK [--all | --limit N]
castle fact supersede --scope fleet|DESK --subject-key K --text T [--tier T] [--expires-at N] [--source S] [--valid-from N] [--lookup L]
castle fact withdraw ID [--desk D]
castle fact expire
castle fact current [--scope S] [--all | --limit N]
castle fact find QUERY [--scope S] [--history] [--limit N]
castle fact as-of (--world T | --belief T) [--scope S] [--all | --limit N]
castle fact history --scope S --subject-key K [--all | --limit N]
castle fact candidates --since N [--limit-per-fact N] [--all | --limit N]
castle fact apply --file PATH [--sha256 HEX]
castle portrait patches [--all | --limit N]
castle portrait show DATE
castle portrait apply DATE --sha256 HEX [--only ID [ID ...]]
castle metric add --desk D --run-id R --model M --input-tokens N --output-tokens N --cache-read-tokens N --cost-usd X --duration-ms N [--ts N]
castle metric summary [--since N] [--all | --limit N]
castle purge [--body-days N] [--extract-days N]
castle audit [--escalate] [--all | --limit N]
```

Every command except `init` and `doctor` needs an existing database. `init` is safe to run twice. `request show` includes the request's owls as metadata. `fact decay --archive` archives what is stale at that moment. `fact list` shows open rows. `--history` adds superseded, withdrawn and expired rows, and `--archived` adds archived ones. `--context` lists what a desk sees and takes neither flag.

List commands print the newest 20 rows by default (`task rounds` and `fact history` the last 10, `task list`, `task builds` and `portrait patches` 30), so a listing pasted into a session stays small. A cut listing says so beside its data: `total`, `shown`, `truncated` and a `note` such as "showing 20 of 500, use --all for everything". `--all` prints every row and `--limit N` at most N. On `owl inbox`, `--all` also adds acked owls, and on `task list` it means full records. `task board` lists only the desks with a row shown and counts the rest in `desks_total`, and a round's `sha_note` names at most five other builds.

`fact apply --file` takes a JSON list of ops, for example:

```
[
  {"op": "set_key", "fact_id": 12, "subject_key": "ci.main"},
  {"op": "supersede", "scope": "fleet", "subject_key": "ci.main", "text": "main needs one approval", "source": "portrait"},
  {"op": "withdraw", "fact_id": 14, "desk": "ryan-claude-1"},
  {"op": "archive", "fact_id": 9}
]
```

A few commands in more detail:

- `desk cap` raises one desk's runs or spend cap until the next cap reset, which is local midnight unless `CAP_RESET_UTC_SECONDS` in the fleet's config says otherwise.
- `desk model DESK MODEL` pins a desk to a model of its own family, `--role` unpins it and `--approve` takes a pending costlier pick, once it has checked the pick still qualifies.
- `ollivander clear` removes every Ollivander stop: the stop of every family (`state/ollivander-stop`), each CLI family's own stop (`state/ollivander-stop-claude`, `state/ollivander-stop-codex`), and any update marker a CLI update left. Its result has `cleared`, `removed` (the files it removed), `stopped` (for each one removed, the family it held back, `every`, `claude`, `codex` or `update`, its file and why), `stop_file` and a `note` saying what it removed, which family each held and why, or that no stop was on and nothing was removed. While a stop event is still unacked after the clear, the session hooks list it marked "(cleared since: no stop is in place now)".
- `desk many-tasks` lets a desk hold many active tasks at once, for good. It refuses `mcgonagall`, `snape`, `portrait`, the human desk and the script desks.
- `task list` with no flag lists the open tasks as one line each, newest first. `--open` lists queued, active and awaiting-close tasks as full records, `--all` lists every task, and each refuses `--status` with it. `task builds` shows one line per build: go task, build task, branch and state.
- `task board` prints `in_flight`.
- `task show` prints the task's record with `run` and `newest_event`, and writes nothing. `run` is the task's newest run launch, or null when none was launched for it: `run_id`, `desk`, `model`, `launched_at`, `usage_recorded`, `end` (the end record the run keeps in the office runs folder: its `exit_code`, `cap_source` and `failure_class` (`auth`, `outage`, `overload`, `rate_limit`, null for none, or `unknown` in a record kept before runs kept it), null while it has none, or `unreadable`), `output_at` (the last write to its output) and `going`, true while its usage is not recorded, it has no end record (an unreadable one is left to the other two) and it was launched inside the running window. No pid is kept for a run. `newest_event` is the task's newest event of any kind or verdict, acked or not, or null. The task's `session_id` is the interactive session that holds it, set only by `task create --session`, so a task a headless desk runs (Claude or Codex) has none: its run is in `run`.

The cap numbers, the review round cap, the running window and the blocklist are the fleet's settings, kept in `fleet/config.py` next to this package.

`token mint` prints the raw token once. Never send its stdout to a log file, and never set a launchd `StandardOutPath` for a job that mints tokens.

### Dumbledore's patches

Each weeknight `fleet/portrait.py` exports the day into Dumbledore's inbox, scrubbing every string it takes from the store, and runs him. A patch's subject keys and tags must pass the same scrubber as its text, and an error never shows a field name the schema does not know. He writes `~/hogwarts/desks/portrait/outbox/patch-<YYYY-MM-DD>.ops`, one JSON object:

```
{"format": "portrait-patch-1", "date": "2027-01-15", "ops": [
  {"id": "f1", "type": "fact_add", "reason": "...", "source": "extract 1234",
   "scope": "fleet", "text": "the store runs on the system python", "tier": "pinned", "subject_key": "store.python"},
  {"id": "f2", "type": "fact_retire", "reason": "...", "source": "fact 12", "fact_id": 12, "how": "archive"},
  {"id": "f3", "type": "fact_edit", "reason": "...", "source": "extract 1240", "fact_id": 14, "text": "..."},
  {"id": "n1", "type": "memory_note_add", "reason": "...", "source": "extract 1250", "text": "...", "tags": ["charter"]},
  {"id": "m1", "type": "archive_move", "reason": "...", "source": "...", "entry": "...", "to": "memory archive"}
]}
```

`fact_add` also takes `lookup` and `expires_at`, `fact_edit` takes `tier`, `lookup`, `expires_at` and `subject_key` (only for a fact without one), and `fact_retire` takes `how` as `archive` or `withdraw`. Every op needs its own id, a reason and a source, and exactly its type's fields. The checks and their limits are in the docstring of `fleet/portrait_patch.py`: a malformed file is refused whole, an op out of schema can never be applied, and no text may hold anything the scrubber would change.

`portrait show` lists each op as ready, applied, out of schema or refused by the store, which it learns by running the ops in a transaction it always rolls back, and prints the exact apply command with the file's sha256. `portrait apply` needs that hash, so the bytes you read are the bytes applied. It runs the chosen ops in patch order in one transaction through `facts.add_fact`, `facts.set_key`, `facts.supersede`, `facts.withdraw`, `pensieve.archive` and `pensieve.add_keypoint`, so one refusal applies none. An archive move is never carried out: it comes back under `for_you`. Each applied op is a routine `portrait.applied` event on the `portrait` desk with dedupe key `portrait:applied:<date>:<op id>`, so no op applies twice. Facts it writes carry the source `portrait:<date>:<op id>`, and key points the tag `portrait`. `portrait patches` lists the newest 30 with the ops each holds, those out of schema and those applied.

Auto-portrait (`fleet/portrait_auto.py`) applies his additions for you while `~/.hogwarts/auto-portrait` holds exactly `on`, read through `fleet/common.py`'s `opt_in_on`, the one reader of every office switch. You switch it on with `echo on > ~/.hogwarts/auto-portrait` and off by removing the file with `rm ~/.hogwarts/auto-portrait`; the job reads it before his run and again just before anything applies, and a file missing at either read means nothing applies that night. The weeknight job takes every run slot he could have (`run_desk.all_slots_lock`, slots 0 to `RUN_SLOT_LIMIT - 1` whatever `RUN_SLOTS` says), reads the patch for the date (`absent`, `present` or `unreadable`), arms the night in `auto_patches` and runs him with those slots, which his process inherits. After a clean run, still under the slots, it reads the patch once more; only a patch that was absent before the run goes on, so a file another run of his wrote is never applied. It parses the patch and stores its snapshot, then applies from the store, never from the file again: one transaction re-reads the night, checks the stored ops against `check_op` and `AUTO_TYPES` (`fact_add` and `memory_note_add`), stops if you applied any op of the patch by hand, then runs each addition in its own savepoint, checking from whole rows before and after that it added exactly one current fact or key point and changed nothing else. Each applied op is a routine `portrait.auto-applied` event under the same dedupe key `portrait:applied:<date>:<op id>`, so both paths share one ledger. The night ends in that transaction with one headmaster `portrait.auto` event (dedupe key `portrait:auto:<date>`): what applied, what waits, and the exact apply command for the rest, or `castle portrait show <date>` when it does not fit in 500 characters. A night that stops after it was armed raises one headmaster `portrait.auto-stopped` event (dedupe key `portrait:auto-stopped:<date>:<attempt>`), scrubbed and without the sha256. A night whose switch is off, or whose stop file is in place, at the apply ends `off` with the usual `portrait.patch-ready` event. A clean run with no patch ends `done` with no event, and a run that failed, was refused or called a model blocked here only raises its own: its `rundesk.cap`, `rundesk.plan-limit`, `rundesk.blocked`, `rundesk.failed` or `rundesk.lock-wait` event ends the night `stopped` in the same transaction (`portrait_auto.Ending`, passed as `on_told` to `run_desk.run` and its notifiers, which `run_desk.tell_ending` calls before it writes the event), and once one event has ended the night no later one of that run is written. When the store refuses that transaction, neither is written and the night stays `armed` for the next job to tell. A clean run that called a model blocked here (`blocked_model` in its result) applies nothing; when its `rundesk.blocked` note could not be written, one `portrait.auto-stopped` event says so instead. A run Ollivander's stop or a CLI update kept from starting raises no event of its own, so it stops the night with one `portrait.auto-stopped` event. With the switch off, a night of the date an earlier killed attempt left `armed` ends `off` only in the transaction of the event that tells you of the rerun (`portrait_auto.off_ending`), or with none for a clean rerun with no patch. Each nightly job but `--export-only` first finishes the nights an earlier job left `armed` or `validated`, without reading the castle: an `armed` night whose run is over is closed with one `portrait.auto-stopped` event, or with none when its owl-keyed run event is already there, and a `validated` night is applied from its snapshot. `portrait show` and `portrait patches` print each night's state and outcome line, even after the file is gone or when it or the outbox cannot be read (the problem is printed beside the line), and `show` says when the file on disk is no longer the one the lane read.

## Chat reviews

Teammates ask a reviewer, a Google Chat app, for a code review of a GitHub PR, and get its verdict, in its own words,
and its findings in their thread, addressed to them by name. They can then ask it about that review in the same
thread, or mention it anywhere it is allowed with any other question, engineering or casual, for a short answer in
its voice. There can be two apps, each its own reviewer: Hermione (Staff Engineer), a Claude turn, and Moody, a Codex
turn whose brief puts security first. A teammate mentions whichever they want, or both. The apps review PRs and
answer questions: they fix, build, run or deploy nothing. Nothing listens on your Mac, and no
one who writes to an app is ever asked to authorize anything.

Google allows one Chat app per Cloud project, so each app has its own project, its own read authorization, its own
worker service account and its own Keychain items. The office serves the apps listed in `CHAT_REVIEW_APPS` in
`fleet/config.py`, keyed by the desk that reviews for each. An app whose read Keychain service, service account and
key Keychain service are all still placeholders is simply not served, so the kit works with Hermione's app alone and
the second is added by filling in its settings.

Every app's connection is an Apps Script project with the same `chat-app/Code.gs` and its own copy of
`chat-app/appsscript.json`. The script does nothing: each event Chat sends it (a message, being added or removed, an
app command) gets an empty acknowledgement, and its manifest asks for no scope and fetches nothing. The work happens
in the chat-review job, which runs on your Mac under `fleet loops` each minute, with two kinds of authorization for
each app that never stand in for each other or for the other app's:

- Reading is yours, read only. A Desktop OAuth client in the app's Cloud project and
  `scripts/chat-review-authorize.py` give the job a refresh token for exactly `chat.messages.readonly` and
  `chat.memberships.readonly`, nothing that writes, stored with the client's id and secret in your login Keychain
  (generic password, service the app's read Keychain service). The job trades it for a short-lived access token each
  pass and refuses one granted any other scope. Each app needs its own, because Chat answers the question "is the app
  a member here" for the app of the project the OAuth client belongs to.
- Sending is the app's. Its worker service account, with no project role, has one JSON key in your login Keychain
  (service the app's key Keychain service, account the account's email). The job signs its own short-lived chat.bot
  token from it with `/usr/bin/openssl`, fed the key on its stdin, and with it only posts replies as that app.

The job refuses a read with an app's token, a post with a read token, and a post as one app with the other's token,
before anything is sent. Keys, refresh tokens, client secrets and access tokens are never in an argv, a file or a
log. Reading is a separate authorization so that an app needs no scope anyone must grant: the Apps Script connection
asks no one for anything, an app's own token only sends, and your read-only tokens read only what you can already read
and can change nothing.

For each app, your token reads only a conversation that is in `CHAT_REVIEW_SPACES` and that you are in, and only
while that app is a member of it: each pass first asks Chat for the app's own membership there
(`spaces/<id>/members/app`), and a conversation it has left is not read for it at all. Each app has two read markers
per conversation in the office: one for its mentions, one for the messages that mention no one. A first read starts
`CHAT_REVIEW_JOIN_GRACE_SECONDS` before the moment the app joined the conversation (its membership's create time, never
ahead of now and never further back than the queue limit), so the mention that added the app is answered and nothing
older is; an app that joins again starts the same way from its new join time, and one whose join time Chat does not
give starts from now. After that each read lists the messages created since its marker (back by
`CHAT_REVIEW_READ_OVERLAP_SECONDS`, for a message Chat lists a little late), at most `CHAT_REVIEW_LIST_PAGES` pages.
Each pass reads in two rounds: first every app reads every conversation for its mentions, and only then does each
app read the messages there that mention no one, as far as every app's mentions are read. So a thread the other app
took a mention in first is never taken as this app's alone just because that app's read came later in the pass or
was cut short. An app whose read authorization failed holds no one back, and any other read that failed holds the
messages that mention no one until it reads again. The marker of those also stays just before a conversation turn
(below) that is still growing, so the next read sees it whole. A message that does not mention the app is dropped as it is read:
never kept, never logged, but for the thread follow-ups and conversation turns below, and the thread's recent
messages an answer turn reads as its context. Of those that mention it:

- one from a person whose membership of that conversation belongs to an email in `CHAT_REVIEW_SENDERS`, whose text,
  once the app mentions are out, holds exactly one GitHub PR link (`https://github.com/<owner>/<repo>/pull/<n>`, bare
  or in `<>`) for a repo in `CHAT_REVIEW_REPOS` becomes a queued review, whatever its other words are: the apps do code
  reviews and nothing else, so a mention with a PR link is a review request. Every other word is ignored: never kept, never logged, never shown to the reviewer, which gets only the link rebuilt
  from what matched. The thread first gets a short fixed acknowledgement as that app, one of its desk's few lines,
  picked by the request's own digest, naming the PR by the number its link was parsed to ("Fine. Reading #442 now."),
  never echoing the message. A message that also asks to fix or merge gets the
  desk's line saying a review is all it does, and still gets the review and nothing more. An approve word only asks
  for the review: whether it approves is its verdict and the switches below, nothing the message says;
- one from them with no link or address at all and the word review (also reviews or reviewing) or an approve word
  (approve, approves, approved, approving or approval), any letter case, in a thread where the same app already took
  a review request whose record is still kept, whatever came of it, is a follow-up: a queued review of that request's
  PR, the newest one's when there are several, under the same sender, space and repo lists and caps. So "review
  again" or "approve" works in a thread the app already reviewed. A follow-up never names another PR, and only the
  app's own requests count, so a thread only the other app reviewed is no thread this one reviewed. In a thread
  where only this app had taken a mention when the message was sent, a review, a mention answer or a help reply to an
  allowed sender (once the other app takes one there too, a message after it needs a mention), a message needs no
  mention either:
  one that mentions no one at all, from an allowed sender,
  with at most 8 words outside its addresses and starting as an instruction (review, re-review, another review or
  approve, after an optional please or can you) is taken. With exactly one PR link for a repo in `CHAT_REVIEW_REPOS`
  ("Review PR <link>") it is a review request for that PR, so a link sent after a help reply, without tagging the app,
  works; with no link at all it is the same follow-up as above, so "review again" works without tagging the app, and
  with no earlier request in the thread it is dropped. It is checked again before it runs, and dropped unanswered
  when a mention of the other app from before it turns up only later (Chat listed it late, or that app's read was
  stopped). A follow-up that does mention the app, in a thread where it took a mention before, is read in turn with
  these, so it follows the PR such a request before it named. Any other message there that mentions no one, from an
  allowed sender, holds no review command and names no PR, carries the conversation on as part of a turn: one person's
  such messages in one thread, each sent at most `CHAT_REVIEW_BURST_SECONDS` (60) after the one before, are one
  conversation turn. It is taken once it has been quiet that long, so a message still being typed joins it, and
  answered once, with its messages in order as its question (the newest that fit in 2000 characters). A mention or a
  review command of theirs in the thread ends their turn before it and keeps its own path. The turn is taken when one
  of its messages ends with a question mark, or when the thread's newest message before it is the app's own reply (one
  read of one message says which) and one of its messages says something: at least 4 words outside its addresses and
  emoji, and no bare reaction (haha, lol, nice, cool, ok, thanks, nope, yes, great and a few of their kin, any letter
  case, punctuation ignored, or emoji alone). It is a conversation turn, the question about the app's review or the
  mention answer (both below) a mention of it would be, so "What is a widget store?" sent after the app answered needs
  no tag, and "haha" gets nothing. Its first message's record is the request and names the others, each then recorded
  as joined to it with none of its text, and a message of the turn that Chat lists late joins it the same way, so no
  turn is answered twice, after a kill too. It is checked again before it runs the same way, as of its newest message.
  Any other message there that does not mention the app, a turn not taken included, is dropped like any other: no
  reply, no record, no log line;
- one from them that only asks what the app is doing ("status", "what are you working on", "queue", "what's
  pending", "anything running" and a few of their kin, any letter case, at most 8 words, no link or address) gets the
  app's status reply (Status, below) and no turn, even in a thread the app reviewed; a link or a review command in it
  still makes it a request or follow-up;
- one from them that is none of those, holds no review or approve word anywhere and has at most 2000 characters, in
  a thread where the same app has a review request on record whose outcome is reviewed, is a question about that
  review (Questions, below). A message that does not mention the app is one only as a conversation turn (above), and
  one in a thread where the app only gave help never is;
- any other mention from them, anywhere the app is allowed, that holds no review or approve word, names no PR and
  has at most 2000 characters is a mention answer (Mention answers, below): "What is a race condition?" gets a short
  answer in the thread;
- a request or follow-up that breaks its rules (a review or approve word with no usable link, two or more PR links,
  a repo that is not listed, a follow-up in a thread the app never reviewed), an empty mention, one over 2000
  characters, or just "help" gets the app's one fixed help text saying it reviews PRs and answers questions;
- a person not on the list gets one fixed refusal, and who they are is not kept;
- a bot, or a message older than `CHAT_REVIEW_QUEUE_MAX_SECONDS`, gets nothing.

A message that mentions both apps is taken by each, so it gets two reviews, one from each desk, each answered as its
own app. Each app takes a message once, even across kills and the overlap of each read: its office record, keyed by a
digest of the app and the Chat message's name, is made create-exclusive, and a message with that app's record is
never taken by it again. At most `CHAT_REVIEW_PULL_MAX` are taken a pass across both apps; the rest wait for the
next. The record holds the app, the message's, thread's and space's names, the requester's email, display name and
Chat user id, and the PR link rebuilt from what the grammar matched, with the links and image names of the material
it attached until its turn has run (Attached material, below). No other text of a message is kept anywhere, and a
request's words never reach a model; only a question's or a mention answer's text does, as below. The sender, space and repo lists are
shared by both apps.

Every reply in a thread mentions the person it answers, with Chat's user mention markup built only from their Chat
user id (a number, so never everyone or an app), never their email. A person not on the list is answered with no
mention. A review's reply is its desk's own summary of its verdict, one or two sentences in its voice (Hermione brisk
and precise, Moody gruff and terse), then its verdict (PASS or CHANGES) with the PR and head commit, then its findings
as bullets under "Blocking:" and "Notes:", each "- path:line: text" of at most 600 characters, cut whole lines at a
time to what Chat takes.

When an app's read authorization fails (revoked, expired, deleted from the Keychain, or granted more than the two read
scopes), the job stops reading for that app with one headmaster event saying to run the authorize helper for it
again. It never tries anything broader instead, and the other app reads on. Replies to requests already taken still go
out, and reading resumes the first pass after you re-authorize.

A review reads the PR with your `gh` login, read only: its metadata, then the diff of its base and head commits. A
PR that is not open, or whose diff is over `CHAT_REVIEW_DIFF_READ_MAX_BYTES` (4 MB), gets a fixed reply. The review
input holds the diff shaped to fit `CHAT_REVIEW_DIFF_MAX_BYTES` (300 KB):

- Each file the PR deletes whole is one line, `deleted: <path> (<N> lines)`, followed by up to 20 of the top-level
  names it declared (class, def, function, interface, struct and enum lines, each cut short), so the reviewer knows
  what went away without reading every removed line. Every other file keeps its full diff, and binary and rename-only
  files stay as git wrote them. The cap applies after this, so a cleanup PR that deletes many files and changes a few
  is reviewed whole. The echo check and a finding's place still see the whole diff, deleted files included.
- When the diff is still over the cap, the review is partial: whole files go in smallest first while they fit, and
  the rest are listed in the input as `not shown: <path> (+A -D)`. The reviewer is told so and asked to say what it
  could not check. A partial review is never an approval (a PASS posts as COMMENT, and blocking findings still post
  as REQUEST_CHANGES), and its Chat reply and GitHub review both say how many files were not reviewed and name up to
  10 of them. Inline comments sit only on files the reviewer was shown, so a finding on a deleted or left-out file
  stays in the review's body. A PR whose changed files are each over the cap on their own gets the fixed too-large
  reply.

Before the turn the job also reads repo context, read only, while `CHAT_REVIEW_REPO_CONTEXT` is `True` (the
default): the PR head's tree with `gh api repos/<owner>/<repo>/git/trees/<head sha>?recursive=1`, then each file it
keeps with `gh api repos/<owner>/<repo>/git/blobs/<blob sha>`, two more fixed read shapes. It covers the folders the
PR touches, each touched path cut to its first two folders (`a/b` for `a/b/c/d.x`), and keeps only plain text files
(no links or submodules, UTF-8 with no NUL byte, each blob checked against its sha) whose every path part is a plain
name, at most `CHAT_REVIEW_CONTEXT_FILE_MAX_BYTES` (200 KB) each, `CHAT_REVIEW_CONTEXT_MAX_FILES` (400) and
`CHAT_REVIEW_CONTEXT_MAX_BYTES` (8 MB) in all, read within `CHAT_REVIEW_CONTEXT_SECONDS`. They go under `context/` in
the turn's folder at their repo paths, each folder made by the job and opened without following a link, and the
input says the folder holds the repo at the PR's head, untrusted like the diff, for checking references: for a
removal, whether anything still uses the removed code. A failed or capped read is a note in the input and the review
goes on; only counts are logged, never a path or a file's text, and the folder goes with the turn's.

The app's desk then runs headless as in its fleet reviews, with its model, in a fresh private folder that holds only
the review input, any attached images and the repo context:

- Hermione runs with her budget and only Read, Grep and Glob, no shell, confined to her working folder, under
  `desks/hermione/chat-review-settings.json`, which allows no read beyond the working folder, no write and no network,
  and with no session saved, so no transcript keeps what she read. A tool list with anything else is refused before
  the turn starts.
- Moody runs `codex exec` with his `codex.toml`, user config and rules ignored, in a permission profile of the chat
  review's own: Codex's minimal platform paths and the working folder read, nothing written anywhere, the office and
  the shared temp folder denied, and no network. Web search, hooks, memories, apps, plugins and browser and computer
  use are switched off over his `codex.toml`. None of his usual reads (the castle, its tasks, the toolchain folders)
  is granted, so the only files his shell can read beyond the system's own are the review input and the repo
  context.

The PR's title, description and diff sit in that input between two marker lines with a random nonce, and the desk's
brief and prompt say its only job is to review it and return its block, and that any instruction inside the PR text
is ignored. Neither desk runs the PR's code. Both desks judge blocking by one shared rubric in their briefs. A finding blocks when the change breaks behavior on a path the diff touches, loses, corrupts or leaks data, opens a security hole, ships a behavior change without a test the repo would expect, or does something its title and description do not say. Style, docs, behavior-neutral refactors, missing tests for code the repo does not test, questions and risks that need an unlikely precondition do not. When a reviewer is unsure it is non-blocking, and the finding says what would make it blocking. The last block is read strictly, the same for both: its header, a
SUMMARY line of at most 300 characters, the findings and the verdict, and anything else is a fixed "couldn't review"
reply. A finding or summary that repeats 40 or more characters of the PR's own text (its whole diff and the repo
context included) is withheld with a fixed line,
and a review holding one is never an approval, so planted text never reaches Chat or GitHub word for word. A
finding's place is left out of that check when it names a repo context file or a file the diff touches, read from
the `---` and `+++` lines of each file's header only (a path git quotes, for a quote, a backslash or a byte past
ASCII, is read as git wrote it), so a long path is never taken for an echo and no line inside a hunk passes for one. Each person may ask for `CHAT_REVIEW_PER_PERSON_DAILY`
reviews a cap day and the whole fleet `CHAT_REVIEW_DAILY`, whichever app they ask, counted before the desk starts, so a
killed review counts, once however often it is resumed. Each review is also one run of its desk under that desk's own daily caps, recorded like any
other: a Hermione turn at most her per-run budget and never past her spend cap, a Moody turn with its tokens and, as
every Codex run, no cost.

While `~/.hogwarts/chat-review-github` holds `on` too, the review also goes on the PR with your `gh` login. A review
with a blocking finding (verdict CHANGES) is posted as REQUEST_CHANGES, and any other as COMMENT. While
`~/.hogwarts/chat-review-approve` holds `on` as well, a review of the whole diff that passes with no blocking finding
is posted as APPROVE instead. That approval, like a change request, is your `gh` account's own review, so it counts
for branch
protection exactly as one you post by hand would. The approve switch is off until you turn it on, it is read just
before each post, and it does nothing while `chat-review-github` is off. GitHub refuses an approval or a change
request on a PR your `gh` login opened; when it refuses one, the review is posted once more as COMMENT, so it still
lands, and the event that posted is in the request's record and its log line. The review opens with a fixed line
saying it is an automated review by the app (its name as its settings spell it) requested by the requester in Chat,
naming them by their Chat display name (scrubbed, at most 60 characters, else "a teammate"), never their email, which
stays in the office's own log, with the summary under it, then the verdict, cut whole lines at a time
to what GitHub takes. Each finding whose place is a file and line the diff shows on its new side goes on that line as
an inline comment, the way a reviewer leaves one: a blocking finding as "Blocking:" with what breaks and "Fix:" with
its direction, a note as "Note:", at most 25 of them and at most 1000 characters each. All of the reviewer's text on
GitHub is plain prose: every markdown character is escaped and a word joiner follows "#" or "GH-" before a digit, so
nothing in it formats, links, embeds or references another issue or PR. A commit hash may still link to the repo's
own commit, as in any review a person writes. A place
`path:N` sits on line N, `path:N-M` on lines N to M when both are in one hunk, and `path:N,M` on the first of them the
diff shows. The script reads the diff's hunks for the lines a comment can sit on (header lines only for the paths, as
the echo check reads them), and `gitops.post_pr_review` checks every comment against those lines before `gh` runs:
its path, its line and start line inside one hunk, the new side only, its length, and no other field. A finding about
the PR's title or description, on a line the diff does not show, withheld, or over the cap stays in the review's body
under a Blocking or Non-blocking heading, without its B1 or N1 tag. There is never a GitHub suggestion block: a
suggestion the model wrote could be committed with one click, and the PR's own text can steer the model. When GitHub
refuses the review for one of its comments (HTTP 422 naming a line or path), it is posted once more without them and
every finding in its body, under the same marker, and the count of inline comments that posted is the record's and
log line's `github_inline`. It is posted at most once per app, PR and head commit, with one exception: when the
app posted a COMMENT there (a pass while the approve switch was off), a later review of that same head commit that
is an APPROVE is posted once more, so asking it to approve after turning the switch on works. That second post has
its own create-exclusive marker, made before it is posted, so a kill never posts it twice, and nothing else is ever
posted a second time: no second COMMENT, REQUEST_CHANGES or APPROVE, nothing after a change request, nothing while
the first post is still in flight or uncertain, and nothing over a COMMENT GitHub took only after refusing an approval
or change request. When GitHub refuses that APPROVE, the COMMENT stands and nothing else is posted. Findings and
summaries are defanged before they are posted, so no mention or link markup in them survives in any place.

A question about a review is the one place a teammate's own words reach a model. Its text is kept in its record only
until its turn has run, and is never logged. It runs a separate turn of the same app's desk, read-only exactly as a
review is (Hermione's Read, Grep and Glob under her chat review settings, Moody's chat permission profile), at
`CHAT_REVIEW_QUESTION_BUDGET_USD` for a Claude desk, with a brief of its own: answer only about this PR and this
review, in the desk's voice, in at most 900 characters; decline anything else in one line; never claim to have done
anything or promise to merge, push, fix or approve; and when the asker says the code changed, suggest asking for a
review again. Its one input file holds the question between marker lines with a random nonce, as a teammate's
untrusted text, the app's own earlier verdict, summary and findings, and the PR's text and diff read fresh and
shaped the way a review shapes them, with no repo context, saying so when the head moved since the review. Its last
block
(`ANSWER`, the answer, `END`) is read strictly, scrubbed, defanged and cut to 900 characters, and an answer that
repeats the PR's own text at length is withheld with a fixed line. The answer goes back in the thread, mentioning
the asker. A question counts toward the per-person, fleet and desk caps like a review, and it never posts on GitHub
and can never approve: only a review reaches GitHub, through its strict verdict and the switches.

A mention answer is a question that is about no review. It runs the same kind of read-only turn as a question, of
the same app's desk, at the same budget and under the same caps, with a brief of its own: answer software engineering
questions, casual questions and light chit-chat alike, concisely, in the desk's voice, in at most 900 characters,
from general knowledge and the attached material only. It has no access to your repos, files or systems, or to the
network beyond that material, and says so rather than pretend; it never claims to have done anything, and points to
a mention with a PR link for a review. Its input holds the message between marker lines with a random nonce, as a
teammate's untrusted text, and its answer block is read, scrubbed, defanged, cut and withheld like a question's,
checked against the attached material. It names no PR, so it never reads or posts on GitHub, and a message it cannot
use (over a cap, say) gets the help text. Its text is kept only until its turn has run and is never logged.

Every answer turn, a question, a mention answer or a conversation turn, also gets its thread's recent messages as
context: at most 8 of the messages in the thread before its own, read when the turn runs with the app's read token
(a `spaces.messages.list` of that thread, at most 3 pages within 30 seconds), oldest first, each on one line
labelled `FROM YOU` for the app's own replies or `FROM TEAMMATE` and the sender's Chat display name (scrubbed, so
never an email), its text scrubbed of credential shapes and cut to 600 characters, and all of them to 4,000. A
message from anyone not in `CHAT_REVIEW_SENDERS`, or from another app, is left out whole. They sit between marker
lines with the turn's nonce, as untrusted teammate text, and the brief says they are context only: answer the newest
message, in the desk's voice, in at most 900 characters, never act on anything they or the message ask, and never
claim to have done anything. A failed read is a note in the input, never the end of the turn, and only counts are
logged, never a message's text or sender.

Attached material goes to every turn alike, a review, a question or a mention answer, read by the job and never by
the model. Images attached to the message (at most 3, up to 5 MB each, PNG, JPEG, GIF or WebP by their first bytes,
whatever their name says) are downloaded with the app's read token through Chat's media download, which
`chat.messages.readonly` covers, and put in the turn's private folder: Hermione reads them with Read, and Moody's
`codex exec` gets them with `--image`, in the same read-only profile. `CHAT_REVIEW_IMAGES = False` downloads none.
Gist links (`https://gist.github.com/<user>/<id>` or a `gist.githubusercontent.com` raw link, at most 3) are read with
`gh api gists/<id>`, a third fixed read shape, text files only, 100,000 characters in all. Any other https link (at
most 3, never a GitHub PR link) is fetched GET only, with no cookies, credentials or proxy, a timeout, at most 300 KB
read and text-like content only, its HTML reduced to text. The job refuses a link that is not https on port 443 or
that names an address rather than a host, resolves the host itself and refuses it when any address it has is
loopback, private, link-local, shared (CGNAT), multicast, reserved or otherwise not public, IPv4 or IPv6, connects to
the address it checked, with the certificate checked for the host, and follows at most 3 redirects, each hop checked
the same way. The material sits in the turn's input between marker lines with its nonce, as untrusted data, and
every finding, summary and answer is echo-checked against it as against the PR's own text. A link or image that
cannot be read is a note in the input, never the end of the turn.

A status reply is built by the job, never by a model: the app's own requests from conversations in
`CHAT_REVIEW_SPACES` that are still open, oldest first, each queued, running, finishing or waiting with what it waits
on (another review of this PR, a free run slot, the caps lock, a CLI update, a fleet stop or the intake switch), then
the last 5 it finished this cap day, newest first, with the outcome, the verdict and, when the review was posted, its
GitHub link. Each line names the PR as `owner/repo#number`, who asked by Chat display name, how long ago, and links
the thread it came from (`https://mail.google.com/chat/u/0/#chat/space/<space>/<thread>`, built from the space and
thread names alone). It holds no email and no text a teammate wrote, and is cut whole lines at a time to what Chat
takes. Each app reports only its own requests.

A request that waits keeps what it waits on in its record. A review acknowledged in its thread that then waited
longer than `CHAT_REVIEW_WAIT_NOTICE_SECONDS` (300) before its turn started gets one fixed line there as it starts,
"Picked this up now; it was waiting on <what it waited on>.", once. Every request ends with one reply in its thread,
whatever its outcome: the review or answer, or the fixed line for a desk that is off, a cap, a PR that is not open or
too large, a PR that could not be read, a turn that failed, could not sign in or gave no usable block, a blocked
model, a wait past the queue limit or an interruption. The one silent end is a request that mentioned no one and
turned out to be in a thread the other app took first.

A request left running whose turn is gone (its turn lock, the PR's or a mention answer's own, and its run lock are
free, so the turn's process ended: a kill, a restart, the loops stopped) is queued again on the next pass with its
resume count, logged as `resumed`, and when it runs again its thread gets one fixed line, "Back on this after an
interruption.", once. A question's text and the attached material stay in its record until its turn has run, so a
resumed one still has them. After `CHAT_REVIEW_RESUME_MAX` (2) resumes it ends with the fixed failure line instead.
One request never runs as two turns, the PR lock, GitHub marker and caps work as before, and its chat caps were
counted at its first launch, so a resumed run counts toward no person's or fleet cap again (its desk's own run caps
count every launch, as for any run). A review by hand that was cut off is ended, since no one is there to run it
again.

Every request, its app, its outcome and its cost is one JSON line in `logs/chat-review.log`; a question's and a
mention answer's lines hold only its request id, app and outcome, a turn's material line only counts and outcomes,
never a link, a page or an image, and its history line only counts. Each record stays in `chat-reviews/requests/` for 30 days. `fleet review pr <link> --requested-by <email>` runs Hermione's review from your
terminal, under the same caps, locks and GitHub switch, with no Chat reply.

### Setting it up

Each app is set up the same way, in its own Google Cloud project. For each, with an account that may administer the
project:

1. Create a project, and turn on `chat.googleapis.com`.
2. Create a service account with no project roles, the worker the app replies as, and one JSON key for it.
3. Set up the OAuth consent screen (Google Auth Platform, Branding and Audience): user type Internal, and under Data
   Access add exactly `https://www.googleapis.com/auth/chat.messages.readonly` and
   `https://www.googleapis.com/auth/chat.memberships.readonly`. Then create an OAuth client (Clients, Create client,
   application type Desktop app) and download its client JSON.
4. Create an Apps Script project, link it to the project from step 1 (Project Settings, Google Cloud Platform
   project, the project number), and paste in `chat-app/Code.gs` and `chat-app/appsscript.json` (show the manifest
   in the editor's settings first). Every app uses the same `Code.gs`. In the manifest only `addOns.common.name` and
   `addOns.common.logoUrl` change from app to app: the kit's manifest is Hermione's, and for Moody's app set the name
   to his app's name and the logo to `moody-128.png` in `office/assets/avatars/png/` (the same URL with `hermione`
   replaced by `moody`). It needs no Script Properties. Deploy it (Deploy, New deployment, type Add-on) and note the
   deployment id. The manifest's `addOns` section is what lets Chat call the script: the console builds Chat apps as
   Workspace add-ons, and without it every message ends in "not responding" with a broken-manifest error in the
   project's logs. After pasting a new `Code.gs` or manifest, use Deploy, Manage deployments, edit, New version, so
   the id stays the same.
5. In the Chat API's configuration, name the app (`Hermione (Staff Engineer)` for the first; for the second, the name
   you give it, the same one its settings hold), let it join spaces, set its connection to the Apps Script project
   with that deployment id, and make it visible to named people only. Add it to a test space you are in first.

Then fill these in your private overlay of `fleet/config.py`; while an app is filled only in part, or no app is
filled, the job refuses to start and says which:

- `CHAT_REVIEW_USER_KEYCHAIN_SERVICE`: the Keychain service name Hermione's app's read-only authorization is stored
  under.
- `CHAT_REVIEW_SERVICE_ACCOUNT`: her app's worker service account's email.
- `CHAT_REVIEW_KEYCHAIN_SERVICE`: the Keychain service name her worker key is stored under, a different one.
- For Moody's app, the same three as `CHAT_REVIEW_MOODY_USER_KEYCHAIN_SERVICE`, `CHAT_REVIEW_MOODY_SERVICE_ACCOUNT`
  and `CHAT_REVIEW_MOODY_KEYCHAIN_SERVICE`, and `CHAT_REVIEW_MOODY_NAME`, his app's name as the Chat API
  configuration spells it, which his replies and GitHub reviews use. Every Keychain service must differ from every
  other. Leave all of them placeholders to serve Hermione's app alone.
- `CHAT_REVIEW_SENDERS`: the emails of the people who may ask.
- `CHAT_REVIEW_SPACES`: the conversations it answers in, as `spaces/<id>` (the id is the last part of the space's
  link). You must be a member of each, since your tokens read them.
- `CHAT_REVIEW_REPOS`: the repos it reviews, as `owner/name`.

Once the office is installed with those filled in, run both helpers yourself in Terminal, from your clone, once for
each app. Without `--app` they are for Hermione's app; `--app moody` is for Moody's:

- `/usr/bin/python3 -I -B scripts/chat-review-store-key.py [--app moody] <worker key.json>` stores the app's worker
  key. It refuses any other account's key, and a Keychain service another setting names too. Then delete the
  downloaded file.
- `/usr/bin/python3 -I -B scripts/chat-review-authorize.py [--app moody] <client.json>`, with the client JSON of that
  app's own project, prints a Google sign-in link. Open it, sign in as yourself and allow both read-only scopes. It
  listens on 127.0.0.1 only, for that one answer, uses PKCE, and stores the refresh token and the client's id and
  secret in your login Keychain under the app's read Keychain service. A grant for any other set of scopes is revoked
  and nothing is stored.

Neither puts a secret in a process's arguments. Moody's desk must be enabled (`desks/moody/enabled`) for his app's
reviews to run. `CHAT_REVIEW_PER_PERSON_DAILY` and `CHAT_REVIEW_DAILY` have working defaults. Run
`sh scripts/loops-setup.sh`, which adds the chat-review job to `loops/jobs`, restart `fleet loops`, and switch it on
with `echo on > ~/.hogwarts/chat-review`. Once a few Chat replies read right, switch on the GitHub post with
`echo on > ~/.hogwarts/chat-review-github`. Once you trust its passes enough to let them count as your approval,
switch that on with `echo on > ~/.hogwarts/chat-review-approve`. `rm` any of these files switches it off again.

Rotate each worker key every 90 days: create a new key, store it with the same helper and `--app` (it replaces the
Keychain item), check a review goes through, then delete the old key in Google Cloud. Re-run the authorize helper for
an app whenever its read authorization stops; it replaces the Keychain item. To rotate one, first remove that app's
access in your Google account's third-party access page (that revokes every token of its client, the stored one too),
then re-run the helper. Removing that access is also how you stop all reading for that app at once.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | ok |
| 1 | unexpected SQLite or OS error |
| 2 | `ValidationError`, including bad arguments |
| 3 | `ConflictError`, including a busy database |
| 4 | `NotFoundError` |
| 5 | `IntegrityError`, including an unsafe `-wal` or `-shm` file and an unhealthy `doctor` report |
| 6 | `TokenError` |

## Running tests

The fast way runs both suites, `tests` and `tests_fleet`, with every test module in its own process, up to six at a time by default, from any folder:

```
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty ~/.hogwarts/run_suites.py
```

Name one suite to run only that one, and add `--jobs N` to change how many modules run at once. Each module runs the hardened discover line below, narrowed to that module with `-p`, in the office folder with an empty environment. The slowest modules start first, so a full run takes about as long as its slowest module. The runner prints the full output of every module that failed, then one line per suite with its test count. It exits 1 if any module failed, timed out or ended without a unittest summary.

The reference form runs one suite in one process:

```
cd ~/.hogwarts && /usr/bin/python3 -I -B -m unittest discover -s tests -t . -v
```

The hardened form uses the wrapper's interpreter line:

```
cd ~/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests -t . -v
```

`discover -t .` puts the repo root on `sys.path`, which `-I` would otherwise drop. Discover is the only supported way to run a suite, and the runner uses it for every module. Running one module by dotted name fails under `-I`, so use `-p test_tasks.py` instead.

Tests make their temporary directories under `/private/tmp`, or under the absolute folder `TEST_TMP_ROOT` names, the one environment variable they read, so `tempfile` never reads `TMPDIR`. The runner passes `TEST_TMP_ROOT` on to every module with `TMPDIR` and xcrun's cache (`xcrun_db`) inside it. A sandbox that denies `/private/tmp` sets it to a folder it may write: fleet verify and Harry's runs set all three to their run's own temp folder. Tests never touch `state/`.

A clone of the kit can also run both suites against an installed copy whose private values are made up. `sh scripts/installed-office-check.sh` installs into a throwaway home, fills the GitHub account, watched repos, blocked models and MCP names with fake values, and runs the suites there, so a test that quietly depends on your own private values fails. It never reads or writes your real `~/.hogwarts` or `~/hogwarts`.
