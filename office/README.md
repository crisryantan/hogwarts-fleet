# Hogwarts store

The Hogwarts store is the single source of truth for the Hogwarts fleet. It holds desks, tasks, peer requests (owls), outcomes, review passes, close tokens, session memory (the Pensieve), curated facts and run metrics.

It is a small Python 3.9+ package that uses only the standard library, plus a CLI named `castle` that prints JSON. The package starts nothing by itself: it has no daemon, hook or job of its own. The fleet's scripts, hooks and launchd jobs are what use it. `install.sh` copies this file to `~/.hogwarts/README.md`.

## Trust model

Nothing a desk runs can call the store, and desks (the agents) have no file access to this directory. Only your terminal and the fleet's scripts and hooks use it, and those run outside every desk sandbox:

- the Owl Post, and `run_desk`, which starts each headless run;
- the patrol scripts: the Map, the morning lineup, keeper's watch and the scoreboard;
- Gringotts and Ollivander;
- the nightly Pensieve export, which runs Dumbledore after it;
- the worktree, verify, review and push scripts, and `fleet feed`;
- the push gate;
- the castle's session hooks, including the go, close-token and SessionEnd hooks.

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
                                        and loops/running.json
  bin/castle                            POSIX sh wrapper, mode 0700
  bin/fleet                             POSIX sh wrapper for the fleet command, mode 0700
  bin/hogwarts-spaces                   opens one herdr space per desk, after fleet/agent_gate.py checks the live ones
  bin/fleet-loops.command               runs fleet loops in a Terminal.app window, for a Login Item
  desks/<desk>/                         each desk's office folder: for a headless desk, BRIEF.md, role.json (its role card),
                                        and settings.json (Claude) or codex.toml (Codex)
  desks/<desk>/enabled                  a headless desk runs only while this plain file exists
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
                                        owl_post.py, ollivander.py, feed.py, tools.py (the fleet command), the patrol
                                        (patrol.py, map.py, morning.py, keeper.py, scoreboard.py), gringotts.py,
                                        portrait.py (the nightly export and Dumbledore's run), portrait_patch.py (his
                                        patches, behind castle portrait), go_confirm.py (finishes a go or a close
                                        the prompt hook could not confirm yet), bulk_close.py (Mischief managed
                                        everything), adopt.py (fleet adopt), loops.py
                                        (fleet loops, the background jobs run from a terminal), failover.py (the
                                        model breaker and in-family fallback), orchestrator.py (McGonagall's typed
                                        next step), owl_report.py (her one-line owl reports), phone.py (loud events to
                                        the phone), scratchpad.py (keeps each scratchpad and task pad to its latest
                                        Checkpoint, older ones archived) and their helpers
  fleet/hooks/                          the castle's session hooks and the push gate, push_gate.py
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
                                        verdict of its own rounds (after-<request>.json), and each PR follow-up's
                                        threads file (followup-<id>.md), which the castle copy is written from.
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
                                        its PASS (off when missing)
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
- `hogwarts.capacity`: `day_bounds(now, reset_offset)`, `add_bump`, `active_bumps`, `list_bumps`, `cap_status`, `record_cap_hit`, `list_cap_hits`, `waiting_requests`, `record_launch(desk, run_id, model, task_id=None)`, `record_launch_usage`, `list_launches`, `newest_launch(task)`, `task_launches(task)` (with each launch's recorded tokens and when they were recorded), `open_launches(desk)`, `open_review_round(..., slot=None, followup_id=None, followup_max_rounds=None)`, `record_round_verdict`, `review_rounds`, `stranded_rounds`, `allow_round` (says which group it lifts), `needs_allowance(task, max_rounds=3, followup_id=None)`, `review_task_ids`, `request_round(request)` (with the round's `followup_id`), `round_author(reviewer_task)`, `in_flight(now, running_window, desk=None, max_rounds=3, followup_max_rounds=2)` (rows carry the open `followup`).
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
- `ollivander clear` removes Ollivander's stop file, and any update marker a CLI update left. Its result has `cleared`, `removed` (the files it removed), `stop_file` and a `note` saying what it did, or that no stop was on and nothing was removed. While a stop event is still unacked after the clear, the session hooks list it marked "(cleared since: no stop is in place now)".
- `desk many-tasks` lets a desk hold many active tasks at once, for good. It refuses `mcgonagall`, `snape`, `portrait`, the human desk and the script desks.
- `task list` with no flag lists the open tasks as one line each, newest first. `--open` lists queued, active and awaiting-close tasks as full records, `--all` lists every task, and each refuses `--status` with it. `task builds` shows one line per build: go task, build task, branch and state.
- `task board` prints `in_flight`.
- `task show` prints the task's record with `run` and `newest_event`, and writes nothing. `run` is the task's newest run launch, or null when none was launched for it: `run_id`, `desk`, `model`, `launched_at`, `usage_recorded`, `end` (the end record the run keeps in the office runs folder: its `exit_code` and `cap_source`, null while it has none, or `unreadable`), `output_at` (the last write to its output) and `going`, true while its usage is not recorded, it has no end record (an unreadable one is left to the other two) and it was launched inside the running window. No pid is kept for a run. `newest_event` is the task's newest event of any kind or verdict, acked or not, or null. The task's `session_id` is the interactive session that holds it, set only by `task create --session`, so a task a headless desk runs (Claude or Codex) has none: its run is in `run`.

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
