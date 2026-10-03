"""PreToolUse hook on Bash: block an agent's git push unless the commit it pushes has a review pass.

Input (JSON on stdin): tool_name, tool_input.command and cwd, the official PreToolUse fields.
A command without the word "push" is left alone at once: no output, exit 0, and Claude Code's normal
permission rules apply. The gate never prints an allow decision, so it can only take permission away.

A push is blocked with exit 2 and one line on stderr, which Claude Code shows to the model as the
reason. The gate fails closed: a command that mentions push and that it cannot read with certainty is
blocked, and so is any error while checking one. It reads a push only when:
- the push is the whole command, optionally after a single `cd <dir> &&`, with no newline, no
  `$(...)`, backticks, process substitution or redirection other than `2>&1` and `>/dev/null`;
- the program is git, after any VAR=value prefixes and the env, command, exec, nohup, time and rtk
  wrappers, with only -C, --no-pager, -P and --paginate before the subcommand;
- push has no force, force-with-lease, mirror, delete, all, tags, prune, no-verify, push-option,
  atomic, exec or receive-pack option, and no +refspec, :deletion or wildcard refspec;
- the remote (named, or origin, or the branch's upstream) is a plain GitHub URL.
Each pushed commit (the tip of every refspec, or HEAD when none is given) must then have a pass in the
store for that repo. A shell, eval or xargs line that mentions push is blocked as unreadable.

What it is not: a wall against a hostile agent. It reads text, so a git alias for push, or a push
through the GitHub API, gets past it, and a session running with Ryan's own permissions can always find
such a way. It is a guardrail that stops an agent pushing by habit or by mistake. The wall for desks is
their sandbox, which has no network. Pushes Ryan types in his own terminal never reach this hook.

The settings use the import form, like every fleet hook:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.hooks.push_gate import main; sys.exit(main())'
"""
from __future__ import annotations

import io
import os
import re
import shlex
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import owlery  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, gitops  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

BLOCK_EXIT = 2
PUSH_WORD = re.compile(r"push", re.IGNORECASE)
GIT_WORD = re.compile(r"(?:^|[^A-Za-z0-9_.-])git(?:$|[^A-Za-z0-9_.-])")
HARMLESS_REDIRECTS = re.compile(r"\s+(?:2>&1|[12]?>\s*/dev/null)(?=\s|$)")
UNREADABLE = ("\n", "\r", "`", "$(", "<(", ">(", "<", ">")
OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
WRAPPERS = {"command", "exec", "nohup", "time", "rtk"}
SHELL_RUNNERS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "xargs", "sudo", "su", "ssh", "watch",
                 "script", "parallel", "nice", "timeout", "caffeinate", "osascript", "python", "python3",
                 "perl", "ruby", "node", "npx", "make", "just"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
GIT_GLOBAL_SKIP = {"--no-pager", "-P", "--paginate", "--no-optional-locks", "--no-replace-objects"}
PUSH_FLAGS_OK = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--progress", "--no-progress",
                 "--porcelain", "-n", "--dry-run"}
REASON_PREFIX = "Hogwarts push gate blocked this push: "
HOW_TO = (" Get a pass from the other model family first (fleet review in your terminal), or push by hand"
          " from your own terminal.")


class Blocked(Exception):
    """The push is refused, with a reason for the model and for Ryan."""


def split_segments(command: str) -> list:
    """The command as segments of words, with the operator that follows each one."""
    cleaned = HARMLESS_REDIRECTS.sub(" ", command)
    if any(mark in cleaned for mark in UNREADABLE):
        raise Blocked("the command uses a newline, a substitution or a redirection the gate cannot read")
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        raise Blocked("the command could not be split into words") from None
    segments, current = [], []
    for token in tokens:
        if token in OPERATORS:
            segments.append((current, token))
            current = []
        elif token and set(token) <= set("&|;()"):
            raise Blocked(f"the command uses an operator the gate cannot read ({token})")
        else:
            current.append(token)
    segments.append((current, None))
    return [(words, op) for words, op in segments if words or op]


def program(words: list) -> tuple:
    """(program name, its arguments) after VAR=value prefixes and plain wrappers."""
    index = 0
    while index < len(words):
        word = words[index]
        if ASSIGNMENT.fullmatch(word):
            index += 1
        elif os.path.basename(word) == "env":
            index += 1
            while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.fullmatch(words[index])):
                if words[index] in ("-S", "--split-string", "-C", "--chdir"):
                    raise Blocked("env options that run or move the command cannot be read")
                index += 1
        elif os.path.basename(word) in WRAPPERS:
            index += 1
        else:
            return os.path.basename(word), words[index + 1:]
    return "", []


def git_push_args(args: list) -> Optional[tuple]:
    """(-C folders, push arguments) when the git command is a push, else None."""
    folders, index = [], 0
    while index < len(args):
        arg = args[index]
        if arg == "-C" and index + 1 < len(args):
            folders.append(args[index + 1])
            index += 2
        elif arg in GIT_GLOBAL_SKIP:
            index += 1
        elif arg.startswith("-"):
            if "push" in " ".join(args[index:]):
                raise Blocked(f"the git option {arg.split('=')[0]} cannot be read before a push")
            return None
        else:
            return (folders, args[index + 1:]) if arg == "push" else None
    return None


def push_targets(args: list) -> tuple:
    """(remote or None, refspecs) from push arguments, refusing anything that could rewrite or delete."""
    positional, options_done = [], False
    for arg in args:
        if not options_done and arg == "--":
            options_done = True
        elif not options_done and arg.startswith("-"):
            if arg not in PUSH_FLAGS_OK:
                raise Blocked(f"the push option {arg.split('=')[0]} is not allowed through the gate")
        else:
            positional.append(arg)
    remote, refspecs = (positional[0], positional[1:]) if positional else (None, [])
    for spec in refspecs:
        if spec.startswith("+") or spec.startswith(":") or "*" in spec or not spec.split(":", 1)[0]:
            raise Blocked(f"the refspec {spec} would force, delete or match many refs")
    return remote, refspecs


def resolve_dir(base: str, target: str) -> str:
    if "$" in target:
        raise Blocked("a folder with a variable in it cannot be read")
    if target == "~" or target.startswith("~/"):
        target = config.USER_HOME_DIR + target[1:]
    path = os.path.normpath(os.path.join(base, target))
    if not os.path.isdir(path):
        raise Blocked("the push folder does not exist")
    return path


def _git(folder: str, args: list, check: bool = True) -> str:
    return gitops.git_in(folder, args, check=check)


def pushed_commits(folder: str, remote: Optional[str], refspecs: list) -> tuple:
    """(owner/repo, [sha, ...]) for what this push would send."""
    if remote is None:
        upstream = _git(folder, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False).strip()
        remote = upstream.split("/", 1)[0] if "/" in upstream else "origin"
    if gitops.REMOTE.fullmatch(remote) is None:
        raise Blocked("push to a named remote, not a URL or path")
    url = _git(folder, ["config", "--get", f"remote.{remote}.url"], check=False).strip()
    try:
        repo = gitops.repo_slug(url)
    except (FleetError, StoreError):
        raise Blocked(f"the remote {remote} is not a plain GitHub repo") from None
    if not refspecs:
        if not _git(folder, ["symbolic-ref", "-q", "HEAD"], check=False).strip():
            raise Blocked("HEAD is detached; name what to push")
        refspecs = ["HEAD"]
    shas = []
    for spec in refspecs:
        source = spec.split(":", 1)[0]
        sha = _git(folder, ["rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}"], check=False).strip()
        if gitops.SHA.fullmatch(sha) is None:
            raise Blocked(f"{source} is not a commit the gate can find")
        shas.append(sha)
    return repo, shas


def _push_after_unquoting(command: str) -> bool:
    """True when the shell would see the word push once quotes are joined, as in pu\"\"sh."""
    if GIT_WORD.search(command) is None:
        return False
    try:
        return any(word.lower() == "push" for word in shlex.split(command))
    except ValueError:
        return True


def find_push(data: dict) -> Optional[tuple]:
    """(folder, remote, refspecs) when the command is a push the gate can read; None when it is no push."""
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    if PUSH_WORD.search(command) is None and not _push_after_unquoting(command):
        return None
    if GIT_WORD.search(command) is None and not any(
            re.search(rf"(?:^|[\s/]){runner}(?:$|\s)", command) for runner in ("bash", "sh", "zsh", "eval", "xargs")):
        return None
    cwd = common.text_field(data, "cwd", 4096)
    if cwd is None or not cwd.startswith("/"):
        raise Blocked("the hook input has no working folder")
    segments = split_segments(command)
    pushes = []
    for index, (words, _) in enumerate(segments):
        name, args = program(words)
        if name in SHELL_RUNNERS and any(PUSH_WORD.search(word) for word in words):
            raise Blocked(f"a push inside {name} cannot be read; run git push as its own command")
        if name == "git":
            found = git_push_args(args)
            if found is not None:
                pushes.append((index, found))
    if not pushes:
        return None
    if len(pushes) > 1:
        raise Blocked("push one thing per command")
    index, (folders, push_args) = pushes[0]
    folder = cwd
    if index == 1:
        words, op = segments[0]
        if op != "&&" or len(words) != 2 or words[0] != "cd":
            raise Blocked("make the push the whole command, with at most one cd before it")
        folder = resolve_dir(folder, words[1])
    elif index != 0:
        raise Blocked("make the push the whole command, with at most one cd before it")
    if len(segments) != index + 1 or segments[index][1] is not None:
        raise Blocked("make the push the whole command, with nothing after it")
    for target in folders:
        folder = resolve_dir(folder, target)
    remote, refspecs = push_targets(push_args)
    return folder, remote, refspecs


def decide(data: dict, conn_factory=None) -> Optional[str]:
    """None to leave the command alone, else the reason to block it."""
    try:
        found = find_push(data)
        if found is None:
            return None
        folder, remote, refspecs = found
        repo, shas = pushed_commits(folder, remote, refspecs)
        conn = (conn_factory or common.connect)()
        try:
            missing = [sha for sha in shas if not owlery.has_pass(conn, repo, sha)]
        finally:
            conn.close()
        if missing:
            return (REASON_PREFIX + f"{missing[0][:12]} on {repo} has no review pass from the other model family."
                    + HOW_TO)
        return None
    except Blocked as exc:
        return REASON_PREFIX + str(exc) + "." + HOW_TO
    except (StoreError, FleetError, OSError, ValueError) as exc:
        return REASON_PREFIX + f"the gate could not check it ({common.one_line(exc, 120)})." + HOW_TO


def main(argv: Optional[list] = None, stdin=None, stdout=None, stderr=None, now: Optional[int] = None) -> int:
    stdin = sys.stdin.buffer if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    raw = stdin.read(config.HOOK_INPUT_MAX_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    lowered = raw.lower()
    if b"push" not in lowered and b"git" not in lowered:
        return 0
    try:
        data = common.read_hook_input(io.BytesIO(raw))
        reason = decide(data)
    except Exception as exc:  # noqa: BLE001 - a push the gate cannot check is a push it blocks
        reason = REASON_PREFIX + f"the gate could not check it ({type(exc).__name__})." + HOW_TO
    if reason is None:
        return 0
    stderr.write(common.one_line(reason, 600) + "\n")
    return BLOCK_EXIT
