"""Claude Code hooks for McGonagall's interactive sessions in the castle.

Each hook reads its JSON input on stdin. Fields read (official hook input names):
session_id, transcript_path, cwd, hook_event_name, plus source (SessionStart),
prompt and prompt_id (UserPromptSubmit), trigger (PreCompact), agent_type (the
agent a main-thread session runs) and agent_id (present only inside a subagent).
A missing or mistyped field is treated as absent.

A hook never exits 2, so a fleet failure can never block Ryan's prompt. Failures
exit 1 with one line on stderr, which Claude Code shows as a non-blocking error.

The castle settings use the import form, so a missing fleet package also exits 1:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.hooks.session_start import main; sys.exit(main())'
Each module also runs as a script path, but Python exits 2 when that file is missing,
and a UserPromptSubmit exit 2 blocks the prompt, so the settings never use that form.
"""
