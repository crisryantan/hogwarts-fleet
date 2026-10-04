# Hogwarts store

The Hogwarts store is the single source of truth for the planned Hogwarts fleet. It holds desks, tasks, peer requests (owls), outcomes, review passes, close tokens, session memory (the Pensieve), curated facts and run metrics.

It is a small Python 3.9+ package that uses only the standard library, plus a CLI named `castle`. It is not wired into anything. There are no hooks, no launchd jobs, no agent files and no settings changes.

## Trust model

Nothing a desk runs can call the store, and desks (the agents) have no file access to this directory. Only Ryan's own terminal and the fleet's scripts and hooks use it, and those run outside every desk sandbox:

- the Owl Post;
- the Map;
- the worktree, verify and review scripts;
- the push gate;
- the close-token and SessionEnd hooks.

Sender identity is a parameter that the calling script supplies. The Owl Post derives it from the desk outbox directory a file came from. The store still validates every input strictly, as defence in depth.

File modes (0700 directories, 0600 files) keep other users out. They do not stop processes that run as the same user. Keeping agents away from this directory is the job of the agent sandbox.

## Layout

```
/Users/crisryantan/.hogwarts            mode 0700
  README.md
  .gitignore                            ignores state/
  bin/castle                            POSIX sh wrapper, mode 0700
  bin/fleet                             POSIX sh wrapper for the fleet command, mode 0700
  bin/hogwarts-spaces                   opens one herdr space per desk, after fleet/agent_gate.py checks the live ones
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
  fleet/                                the fleet scripts and hooks: run_desk.py, review.py, push.py, owl_post.py,
                                        ollivander.py, feed.py, tools.py (the fleet command) and their helpers
  tests/                                unittest suite
  state/pensieve.db                     the real database, created by castle init
```

`bin/castle` is exactly:

```
#!/bin/sh
exec /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from hogwarts.cli import main; sys.exit(main())' "$@"
```

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
| `owls.body_path` | under `/Users/crisryantan/hogwarts/desks/<sender>/outbox/` |
| `tasks.intent_path` | exactly `/Users/crisryantan/hogwarts/tasks/<task_id>/TASK.md`, for that task's own id |
| `tasks.worktree` | under `/Users/crisryantan/hogwarts/worktrees/` |
| `review_passes.review_path` | under `/Users/crisryantan/.hogwarts/reviews/`, in the office, which no desk can write |

The first three sit in the castle (`/Users/crisryantan/hogwarts`), where desks work. Review files sit in the office (`/Users/crisryantan/.hogwarts`), so a desk cannot write or swap a review the store points at.

Paths must be absolute and already normalised, with no control characters, so `..`, `.` and repeated slashes are refused. Whoever later opens one of these files (a fleet script or a desk) must open it with `O_NOFOLLOW` and check that the resolved path is still under its root.

The only file the CLI reads is the ops file for `castle fact apply --file PATH`. The path must be absolute and normalised. Every directory above it, up to `/`, must be a real directory (not a symlink) owned by root or the current user, and must not be group or world writable unless it has the sticky bit, like `/private/tmp`. The file is opened with `O_NOFOLLOW` and `O_NONBLOCK`, so a symlink or a FIFO is refused without blocking. It must be a regular file owned by the current user, not group or world writable, and at most 256KB. It must be UTF-8 JSON with no repeated keys inside an object and no `NaN` or `Infinity`. With `--sha256 HEX`, the bytes read must hash to that value, or nothing is applied. Pass the hash taken when the file was reviewed, so the reviewed bytes are the applied bytes.

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
- Close tokens are returned once. Only their sha256 is stored. They are single use, expire, and are consumed in the same transaction as the close. A database trigger refuses `close_reason = 'complete'` unless the task has a consumed token or its parent closed as complete.
- A review must cite the task that recorded the commit. Families are always looked up from the database. A PASS needs a reviewer from `claude`, `codex` or `human` whose family differs from the author's. A non-human reviewer also needs a review request from the author's task.
- A fact's `lookup` is the command or URL that fetches the live value. It is stored as single line text and never executed. A lookup that the scrubber would change (a token, a password, URL credentials, an email, an IP address or a hex string of 32 or more characters) is refused, and the error names what matched. A lookup must also be either an `https` URL with no credentials and no quotes, spaces, `$`, `;`, `|`, `<`, `>`, backticks or parentheses, or a `gh` or `bk` command whose words use only letters, digits and `._/:=@,-`. So no shell syntax can be stored. Anything that later runs a lookup must still treat it as untrusted text, check that the subcommand only reads, and never pass it through a shell.
- CLI output is `json.dumps(ensure_ascii=True)`. List views never include owl bodies.
- Close tokens and owl bodies never travel on argv. They come from stdin only.

## Schema summary (version 5)

All tables are STRICT when SQLite supports it. Timestamps are integer unix seconds.

| Table | Purpose |
| --- | --- |
| `schema_version` | Applied migrations. |
| `desks` | Name, family (`claude`, `codex`, `human`, `script`), role, model. Desks are immutable. `fleet` is reserved. |
| `tasks` | Status `queued`, `active`, `awaiting_close`, `closed`. One active task per desk and one per session (partial unique indexes). Tasks are inserted queued. A closed task never reopens (trigger). |
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
| `review_rounds` | One row per review request of an author task, with its round number, whether a newer commit superseded it, and the review it recorded. That review is stored and tied to its round in one step and never changes, so a round with a verdict counts even if publishing the review afterwards failed. |
| `run_launches` | One row per headless run, written before its process starts, so the run counts toward the daily run cap even if it is killed before it records usage. Its usage is the `metrics` row tied to it once it ends, set once. Never deleted. |
| `round_allowances` | One row each time you allow another review round with `castle task allow-round`. Immutable. |
| `model_lines` | How you filed a model name: `frontier`, `workhorse`, `fast` or `ignore`. The latest row per name wins. Immutable. |
| `model_catalog` | The model names each family offered at Ollivander's last look, with whether the catalog listed each, the tier it was filed under then and when it retires. `castle desk model --approve` checks a pending pick against it. |
| `desk_models` | Each Claude and Codex desk's role need, current model and effort, pin, pending pick and trial state, including how the last trial ended (`trial_end`: `passed`, `pinned`, `held`, `revert_blocked` or `reverted`). The `desks` table itself stays immutable. |
| `model_changes` | Every model switch, with its reason: `initial`, `role`, `pin`, `approved` or `revert`. Immutable. |
| `model_resolutions` | Every full Claude id each alias was seen to run as, with its first and last sighting. Only the last sighting moves, and rows are never deleted. |

Triggers also block deletes on desks, tasks, task commits, requests, events, facts, owls and review passes. Fact triggers require `valid_from` and `recorded_at` on every row, and keep `valid_to`, `closed_at` and `end_reason` set or unset together, with `valid_to` no earlier than `valid_from` and `closed_at` no earlier than `recorded_at`. `superseded_by` is only set on a superseded row. `restores` never changes once written.

Migration 2 adds the fact columns, backfills `valid_from` and `recorded_at` from `created_at`, and builds the index and `facts_fts`. Migration 3 adds `facts.restores` and the trigger that keeps it fixed. Migration 4 adds the cap and review round tables. Migration 5 adds the model tables. Each column is added only while it is missing, so running a migration again changes nothing.

## Why facts work this way

A fact that changes is replaced explicitly with `supersede`, never guessed at. The replaced row is kept, with its validity window closed, so history and as-of reads still see it. A key's windows never overlap, so as-of reads give one answer per key. A closed row is never reopened. When a replacement is withdrawn, the earlier fact comes back as a new row, so belief history is never rewritten either. The default read (`current_facts`, `context_facts` for a desk, and `castle fact list`) returns only current facts: not superseded, withdrawn, expired or archived. Volatile state such as PR status, build colour or rollout percentage goes stale fast, so a fact whose text looks volatile is accepted only with a `lookup` that fetches the live value, or as a perishable fact that expires within 7 days.

The volatility lint matches words, not meaning, so it also catches ordinary lasting prose: "open question", "red flag", "open source", "closed-form", "go-live checklist", "patent pending", "released under MIT", "issue #42", "+16.1%" and "NEVER open ACME/legacyapp PRs" are all refused. This is on purpose, since a false refusal costs a rewrite and a missed volatile fact goes stale silently. The remedy for a lasting fact is to reword it ("unresolved question", "warning sign", "never create ACME/legacyapp PRs"). Do not add a placeholder lookup, and do not make a lasting rule perishable to get past the lint, because a perishable fact quietly drops out after a week. There is no recorded override yet. Any importer, including one for MEMORY.md, must reword or skip, and never fall back to perishable.

## API

Every function takes a connection from `db.connect(path)` as its first argument. Functions that depend on time take an optional `now`.

- `hogwarts.db`: `connect(path, create=True)`, `connect_readonly(path)`, `migrate(conn)`, `pending_statements(conn, statements)`, `schema_version(conn)`, `transaction(conn)`, `snapshot(conn)`, `doctor(path, code_root=None)`, `stray_bytecode(root)`, `DEFAULT_DB`.
- `hogwarts.pensieve`
  - Desks: `add_desk`, `get_desk`, `list_desks`.
  - Tasks: `create_task(desk, title, intent_path=None, parent_task_id=None, request_id=None, session_id=None, worktree=None, task_id=None)`, `start_task`, `mark_awaiting_close(task, repo=None, sha=None)`, `record_commit`, `get_commit`, `close_task`, `closed_ancestors`, `get_task`, `list_tasks`.
  - Events: `add_event`, `drain(max_chars=1500)`, `ack(event_id)`.
  - Memory: `record_session`, `get_session`, `add_extract`, `add_keypoint`, `find(query, limit)`, `fts_query`, `fts_phrases`, `scrub(text)`.
  - Facts: `add_fact(scope, text, tier, source, expires_at=None, subject_key=None, valid_from=None, lookup=None)` (the same function as `facts.add_fact`), `touch`, `decay`, `archive_stale`, `archive`, `list_facts(scope=None, include_archived=False, include_closed=False)` (open rows, plus archived or closed rows when asked), `context_facts(desk)` (current fleet and desk facts).
  - Metrics: `add_metric`, `summary(since)`.
- `hogwarts.facts`
  - Writes: `add_fact`, `supersede(scope, subject_key, text, source, tier="aging", valid_from=None, lookup=None, expires_at=None)`, `withdraw(fact_id, desk=None)`, `expire()`, `set_key(fact_id, subject_key)`, `apply_ops(ops)`.
  - Reads: `current_facts(scope=None)`, `find_facts(query, scope=None, include_history=False, limit=10)`, `as_of_world(t, scope=None)`, `as_of_belief(t, scope=None)`, `history(scope, subject_key)`, `contradiction_candidates(since, limit_per_fact=3)`.
  - Lint: `VOLATILE_PATTERNS`, `volatile_match(text)`, `LOOKUP_COMMAND`.
- `hogwarts.capacity`: `day_bounds(now, reset_offset)`, `add_bump`, `active_bumps`, `list_bumps`, `cap_status`, `record_cap_hit`, `list_cap_hits`, `waiting_requests`, `record_launch`, `record_launch_usage`, `list_launches`, `open_review_round`, `record_round_verdict`, `review_rounds`, `stranded_rounds`, `allow_round`.
- `hogwarts.wands`: `classify`, `ryan_lines`, `record_catalog`, `last_catalog`, `catalog_entry`, `get_desk_model`, `list_desk_models`, `set_need`, `apply_model`, `set_pending`, `clear_pending`, `approve`, `pin`, `unpin`, `desk_choice`, `record_outcome`, `changes`, `base_alias`, `record_resolution`, `resolutions`, `resolved_id`, `blocked_resolution`, `blocked_resolutions`, `clear_stop`. The calls that file, pin, approve or apply a model take the fleet's `BLOCKED_MODEL_PREFIXES` and refuse a name one of them matches. Pin, approve, apply, a pending pick and a trial's revert also refuse an alias that ever ran as a full id one of them matches. A labelled alias such as `opus[1m]` shares the plain alias's resolutions. `approve` also refuses a pick that the latest stored catalog no longer lists, hides, files under another tier or shows retiring within 30 days. `pin` on the desk's current model ends its trial, and its result carries a `warning` when the latest catalog hides the model or shows it retiring soon. `record_outcome` never reverts a desk pinned since its switch, nor, while anything is blocked, onto no model at all, nor onto a model filed as ignore or one the latest catalog no longer lists, hides or shows retiring within 30 days.
- `hogwarts.watch` (read only, for a connection from `db.connect_readonly`): `marks`, `owls_after`, `headmaster_events_after`, `metrics_after`, `run_recorded`.
- `hogwarts.owlery`
  - Owls: `send`, `inbox`, `read`, `ack`, `mark_delivered`.
  - Requests: `REQUEST_PHASES`, `open_request`, `advance`, `defer`, `decline`, `get_request`, `list_requests`, `request_owls`.
  - Reviews: `record_review`, `latest_review`, `has_pass`.
  - Tokens: `mint`, `consume`.
  - Retention: `purge(now, body_days=30, extract_days=90)`, `audit(now, escalate=False)`.

### Behaviour notes

- Writes run in `BEGIN IMMEDIATE` through `db.transaction`. A write helper nests inside another write transaction as a savepoint, so a nested helper that fails undoes its own changes even when the caller catches the error and commits. It refuses with `StoreError` inside a `snapshot` or a transaction the caller opened, and so does `consume`.
- `create_task` takes an optional `task_id`, so a fleet script can mint the id (`tk_` and 16 lowercase hex digits), write `tasks/<task_id>/TASK.md`, then register the task. An id that is already taken raises `ConflictError`. An `intent_path` needs that `task_id` and must be exactly `/Users/crisryantan/hogwarts/tasks/<task_id>/TASK.md`. Without a `task_id` the store mints one and the task has no intent path.
- `start_task` only starts a queued task whose ancestors are all open. A desk or session that already has an active task raises `ConflictError`. `mark_awaiting_close` frees the desk and can record the head commit.
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
- `audit` is read only. It lists stale requests (60 minutes), owls to re-ring (30 to 120 minutes unacked), owls to escalate (120 minutes or more), owls never delivered after 30 minutes, active tasks older than 8 hours, tasks awaiting close for 24 hours since they started, queued tasks under a closed parent, and review passes whose task is missing. With `escalate=True` it adds headmaster events with dedupe keys, so repeating it adds nothing.
- `has_pass(repo, sha)` finds the author through the commit's task. It is true only when that task is awaiting close or closed complete, and the latest review of the commit is a PASS from a different, PASS-capable family.

## CLI

All output is JSON. Errors go to stderr as JSON with the exit code below. `--help` prints argparse text.

```
castle init
castle doctor
castle desk add NAME --family F [--role R] [--model M]
castle desk list
castle desk cap DESK (--runs +N | --spend +X)
castle desk caps
castle desk model DESK (MODEL | --role | --approve)
castle desk models
castle model line NAME frontier|workhorse|fast|ignore
castle ollivander clear
castle task create --desk D --title T [--id TASK] [--intent-path P] [--parent TASK] [--request REQ] [--session S] [--worktree P]
castle task start|show TASK
castle task await-close TASK [--repo O/N --sha SHA]
castle task commit TASK --repo O/N --sha SHA
castle task close TASK --reason complete|abandoned|superseded [--token-stdin]
castle task list [--desk D] [--status S]
castle task allow-round TASK
castle task rounds TASK
castle token mint TASK [--ttl SECONDS]
castle owl send --from D --to D --kind K --subject S [--body-path P | --body-stdin] [--task T] [--request R] [--reply-to OWL] [--key K]
castle owl inbox DESK [--all]
castle owl read|ack OWL --as DESK
castle request open --from D --to D --title T [--body-path P | --body-stdin] [--parent TASK] [--key K]
castle request advance REQ PHASE [--detail TEXT]
castle request defer|decline REQ --reason R
castle request show REQ
castle request list [--desk D] [--phase P] [--open]
castle review record --repo O/N --sha SHA --task TASK --reviewer DESK --verdict V [--review-path P]
castle review check --repo O/N --sha SHA
castle event add --desk D --kind K --verdict V --summary S [--task T] [--dedupe-key K]
castle event drain [--max-chars N]
castle event ack ID
castle pensieve session SESSION --project P [--desk D] [--model M] [--started-at N] [--ended-at N] [--first-turn-tokens N] [--total-input-tokens N]
castle pensieve extract SESSION --role user|assistant (--text T | --text-stdin) [--seq N]
castle pensieve keypoint (--text T | --text-stdin) [--tags a,b] [--session S]
castle pensieve find QUERY [--limit N]
castle fact add --scope fleet|DESK --tier T --text T [--expires-at N] [--source S] [--subject-key K] [--valid-from N] [--lookup L]
castle fact touch ID
castle fact decay [--archive]
castle fact archive ID [ID ...]
castle fact list [--scope S] [--archived] [--history]
castle fact list --context DESK
castle fact supersede --scope fleet|DESK --subject-key K --text T [--tier T] [--expires-at N] [--source S] [--valid-from N] [--lookup L]
castle fact withdraw ID [--desk D]
castle fact expire
castle fact current [--scope S]
castle fact find QUERY [--scope S] [--history] [--limit N]
castle fact as-of (--world T | --belief T) [--scope S]
castle fact history --scope S --subject-key K
castle fact candidates --since N [--limit-per-fact N]
castle fact apply --file PATH [--sha256 HEX]
castle metric add --desk D --run-id R --model M --input-tokens N --output-tokens N --cache-read-tokens N --cost-usd X --duration-ms N [--ts N]
castle metric summary [--since N]
castle purge [--body-days N] [--extract-days N]
castle audit [--escalate]
```

Every command except `init` and `doctor` needs an existing database. `init` is safe to run twice. `request show` includes the request's owls as metadata. `fact decay --archive` archives what is stale at that moment. `fact list` shows open rows. `--history` adds superseded, withdrawn and expired rows, and `--archived` adds archived ones. `--context` lists what a desk sees and takes neither flag.

`fact apply --file` takes a JSON list of ops, for example:

```
[
  {"op": "set_key", "fact_id": 12, "subject_key": "ci.main"},
  {"op": "supersede", "scope": "fleet", "subject_key": "ci.main", "text": "main needs one approval", "source": "portrait"},
  {"op": "withdraw", "fact_id": 14, "desk": "ryan-claude"},
  {"op": "archive", "fact_id": 9}
]
```

`desk cap` raises one desk's runs or spend cap until the next cap reset, which is local midnight unless `CAP_RESET_UTC_SECONDS` in the fleet's config says otherwise. `desk model DESK MODEL` pins a desk to a model of its own family, `--role` unpins it and `--approve` takes a pending costlier pick, once it has checked the pick still qualifies. `ollivander clear` removes Ollivander's stop file. The cap numbers, the review round cap and the blocklist are the fleet's settings, kept in `fleet/config.py` next to this package.

`token mint` prints the raw token once. Never send its stdout to a log file, and never set a launchd `StandardOutPath` for a job that mints tokens.

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

```
cd /Users/crisryantan/.hogwarts && /usr/bin/python3 -I -B -m unittest discover -s tests -t . -v
```

The hardened form uses the wrapper's interpreter line:

```
cd /Users/crisryantan/.hogwarts && /usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -m unittest discover -s tests -t . -v
```

`discover -t .` puts the repo root on `sys.path`, which `-I` would otherwise drop. Discover is the only supported way to run the suite. Running one module by dotted name fails under `-I`, so use `-p test_tasks.py` instead.

Tests make their temporary directories under the constant `/private/tmp`, so `tempfile` never reads `TMPDIR`. They never touch `state/` and never read environment variables.

## Mapping from the teammate's modules

| `db_adapter.py` / `dispatch_store.py` | Here |
| --- | --- |
| `MemoryManager` task registry | `pensieve.create_task`, `start_task`, `mark_awaiting_close`, `close_task` |
| One open task per session or desk, `TaskConflictError` | Partial unique indexes on active tasks per desk and per session, `ConflictError`. Parallel Ryan sessions each get their own desk, for example `ryan-claude-1` and `ryan-claude-2`, both family `claude`. |
| `append_event` | `pensieve.add_event`, `drain`, `ack` |
| `mem fact add`, context build | `pensieve.add_fact`, `context_facts`, `touch`, `decay`, `archive_stale`, `archive`, plus `facts.supersede`, `withdraw`, `current_facts` and the as-of reads |
| `DispatchStore` private message bodies | `owls.body` or `owls.body_path`, returned only by `read` |
| Dedupe by content hash | Idempotency keys, plus content dedupe into unacked owls and in-flight requests |
| Read separate from ack | `owlery.read`, `owlery.ack` |
| One answer per question | Partial unique index on answers |
| Request scoped mailbox | `owls.request_id`, parties must match the request, `owlery.request_owls` |
| `REQUEST_PHASES` forward only saga, crash safe resume | `owlery.REQUEST_PHASES`, `advance` (idempotent at the current phase, each phase checks its evidence) |
| Record first close | `awaiting_close` with the head commit, then `close_task` with a close token |
| Orphan close when the parent closes | Cascade in `close_task`, `start_task` refusing a closed ancestor, audit list of orphaned queued tasks |
| Recover and audit | `owlery.audit` (including never delivered owls), plus idempotent `open_request`, `send` and `advance` for resume |
| `DispatchStoreError` | `StoreError` and its subclasses |

Dropped on purpose: herdr pane and tab bookkeeping, typing into terminals, session ids parsed from free text fields, file locks held across waits, and paths taken from the environment.
