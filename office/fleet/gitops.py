"""Git for the fleet scripts, run outside every desk sandbox without trusting what a desk can write.

A desk can rewrite any file in its worktree, including the worktree's own .git pointer file and
hook folders such as .husky. So every git call here:
- takes --git-dir and --work-tree from the office record for that worktree, never from the worktree;
- runs with core.hooksPath=/dev/null and core.fsmonitor=false, so no hook or monitor command runs;
- uses the absolute git binary and a fixed environment. HOME is kept so Ryan's own ~/.gitconfig
  (name, email, credential helper) still applies. No desk can write that file. GIT_NO_LAZY_FETCH is set, so
  a partial clone never fetches a missing object quietly with those credentials: git reports it missing.

Office records live in ~/.hogwarts/worktrees/<name>.json, one per castle worktree, written only by
the worktree and review scripts. They name the repo folder (a main checkout or a linked worktree of one), the main
checkout's .git folder, the branch and base.

This module and run_desk are the only fleet modules that start git or a desk. This module also runs the only gh
commands that write to GitHub, three shapes held exactly by one guard (check_write_argv): gh pr create --draft for the
review loop's automatic draft PR (open_draft_pr), and for a PR follow-up's replies, gh api --method POST to reply in a
review thread (post_reply) or to comment on the PR (post_pr_comment). Each runs in the office with a fixed environment
and its text on stdin, never in a worktree, so no file a desk wrote can steer it, and nothing gh prints about a login
reaches an event. Nothing here resolves a thread, requests a review, marks a PR ready or merges: no such shape passes
the guard. An answer that does not say for sure whether a comment was posted raises Uncertain, so the caller reads
it back from GitHub and never posts it again.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from typing import Optional

from hogwarts import ids, pensieve
from hogwarts.errors import ValidationError

from fleet import common, config, safefs
from fleet.safefs import FleetError

RECORD_DIR = "worktrees"
RECORD_MAX_BYTES = 8192
SAFE_PATH = re.compile(r"/[A-Za-z0-9._@+/-]{1,400}")
BRANCH = re.compile(r"[a-z0-9][a-z0-9._/-]{0,99}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
GITHUB_URL = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?"
)
SHA = re.compile(r"[0-9a-f]{40}")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/pull/([0-9]{1,10})")
PR_TITLE_MAX = 100
PR_BODY_MAX = 20000
# gh exits 4 when it needs a login. Any of these words in what it printed is treated as a login problem too, so its
# text is never repeated.
GH_AUTH_EXIT = 4
GH_AUTH_WORDS = re.compile(r"(?i)auth|log ?in|token|credential|password|\b40[13]\b|forbidden|saml|sso|permission")
WORD_SPLIT = re.compile(r"[^a-z0-9]+")
# A follow-up's two write shapes: a reply in a review thread (to the thread's first comment) and a PR comment.
_REPO_PART = r"([A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100})"
REPLY_PATH = re.compile(_REPO_PART + r"/pulls/([1-9][0-9]{0,9})/comments/([1-9][0-9]{0,19})/replies")
PR_COMMENT_PATH = re.compile(_REPO_PART + r"/issues/([1-9][0-9]{0,9})/comments")
COMMENT_ID = re.compile(r"[1-9][0-9]{0,19}")
# gh api says (HTTP 4xx) when GitHub answered with a refusal, so nothing was posted.
GH_HTTP_REFUSED = re.compile(r"\(HTTP 4[0-9]{2}\)")
GH_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
WRITE_BODY_MAX = 65536
HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "core.editor=true",
    "-c", "protocol.ext.allow=never",
    "-c", "core.quotePath=true",  # unusual path names come back escaped, one per line
)
OUTPUT_MAX_CHARS = 200_000
# A scan (git(..., whole=True)) reads every character git printed, up to this many, and refuses more rather than
# check only part of it.
SCAN_MAX_CHARS = 20_000_000
CLEAN_MAX_PATHS = 200
CLEAN_MAX_CHARS = 50_000


def check_safe_path(path: object, label: str) -> str:
    """An absolute, normalised path with only plain characters, so it is safe in TOML and argv."""
    if not isinstance(path, str) or SAFE_PATH.fullmatch(path) is None:
        raise FleetError(f"{label} must be an absolute path with plain characters only")
    parts = path.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise FleetError(f"{label} must be a normalised absolute path")
    return path


NOT_A_CHECKOUT = "the repo folder must be a main checkout with its own .git folder, or a linked worktree of one"
MAIN_GONE = ("the repo folder is a linked worktree whose main checkout, or its entry there, is gone (moved, removed or"
             " pruned): run git worktree repair from the main checkout, or name the main checkout instead")
POINTER_MAX_BYTES = 4096


def check_repo_dir(path: object) -> str:
    """A checkout in Ryan's home, outside the fleet: a main checkout or a linked worktree of one (repo_dirs)."""
    return repo_dirs(path)["repo_dir"]


def repo_dirs(path: object) -> dict:
    """{"repo_dir", "git_dir", "common_dir", "main_dir"} for a checkout in Ryan's home. A main checkout is a real
    folder with a real .git folder, which is both its git_dir and its common_dir. A linked worktree is a real folder
    whose .git file names its entry <main>/.git/worktrees/<id>: that entry is its git_dir (HEAD, index) and
    <main>/.git is its common_dir (objects, refs, config, other worktrees). Read from the files themselves, never
    through git, so no repo config runs. Both folders get every check: plain characters, inside the home folder,
    outside the office and the castle, no symlink on the way. The entry's commondir must name <main>/.git and its
    gitdir must name this .git file back, so a .git file can point only at the entry made for this folder. A bare repo
    (by its folder name or its core.bare), a worktree of one, or a worktree whose main checkout or entry is gone, is
    refused."""
    path = _check_checkout(path, "the repo folder", "repo folder")
    try:
        st = os.lstat(path + "/.git")
    except OSError:
        raise FleetError(NOT_A_CHECKOUT) from None
    if stat.S_ISDIR(st.st_mode):
        _check_not_bare(path + "/.git")
        return {"repo_dir": path, "git_dir": path + "/.git", "common_dir": path + "/.git", "main_dir": path}
    if not stat.S_ISREG(st.st_mode):
        raise FleetError(NOT_A_CHECKOUT)
    line = _read_pointer(path + "/.git", "the repo folder's .git file")
    if not line.startswith("gitdir: "):
        raise FleetError("the repo folder's .git file does not name a git folder")
    entry = _pointed(path, line[len("gitdir: "):], "the git folder the .git file names")
    worktrees, name = os.path.split(entry)
    common_dir = os.path.dirname(worktrees)
    if os.path.basename(worktrees) != "worktrees" or not name:
        raise FleetError("the repo folder's .git file does not name a worktree entry of a main checkout")
    if os.path.basename(common_dir) != ".git":
        raise FleetError("the repo folder is a worktree of a bare repo or a separate git folder, and the fleet builds"
                         " only from a main checkout or a linked worktree of one")
    main_dir = _check_checkout(os.path.dirname(common_dir), "the worktree's main checkout", "the main checkout",
                               entry)
    for folder in (common_dir, worktrees, entry):
        try:
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                raise FleetError("the worktree's main checkout has no real .git folder with this worktree's entry")
        except OSError:
            raise FleetError(MAIN_GONE) from None
    if _pointed(entry, _read_pointer(entry + "/commondir", "the worktree entry's commondir file"),
                "the worktree entry's commondir") != common_dir:
        raise FleetError("the worktree entry's commondir does not name its main checkout's .git folder")
    back = _pointed(entry, _read_pointer(entry + "/gitdir", "the worktree entry's gitdir file"),
                    "the worktree entry's gitdir")
    if back != path + "/.git":
        raise FleetError("the worktree entry the .git file names points back to another folder, or to this one"
                         " spelled another way, so the .git file is refused")
    _check_not_bare(common_dir)
    return {"repo_dir": path, "git_dir": entry, "common_dir": common_dir, "main_dir": main_dir}



def _check_not_bare(common_dir: str) -> None:
    """Refuse a bare repo, which can sit in a folder named .git too: its config must say core.bare = false, as git
    init and git clone write it, read with the hardened git every other read here uses. Any other spelling refuses."""
    if git(["config", "--get", "core.bare"], common_dir, check=False).strip() != "false":
        raise FleetError("the repo folder's git folder is a bare repo (core.bare is not false), and the fleet builds"
                         " only from a main checkout or a linked worktree of one")


def _check_place(path: str, label: str) -> None:
    """The place rules every repo folder keeps, on the path as spelled: inside the home folder, outside the fleet."""
    if not path.startswith(config.USER_HOME_DIR + "/"):
        raise FleetError(f"{label} must be inside your home folder")
    for root in (config.OFFICE_ROOT, config.CASTLE_ROOT):
        if path == root or path.startswith(root + "/"):
            raise FleetError(f"{label} must be outside the office and the castle")


def _check_checkout(path: object, label: str, plain: str, entry: Optional[str] = None) -> str:
    """A checkout folder: plain characters (plain names it for check_safe_path), _check_place, no symlink on the way
    and a real folder. For a main checkout found through a worktree's .git file, entry is the worktree's entry there,
    so a main checkout that is gone is told apart from a folder that was never one."""
    path = check_safe_path(path, plain)
    _check_place(path, label)
    if entry is not None and not os.path.lexists(entry):
        raise FleetError(MAIN_GONE)
    if os.path.realpath(path) != path:
        raise FleetError(f"{label} must not go through a symlink")
    if not os.path.isdir(path):
        raise FleetError(NOT_A_CHECKOUT)
    return path


def _read_pointer(path: str, label: str) -> str:
    """The one line of a small git pointer file (.git, commondir, gitdir), read without following a symlink, with
    the line ending git itself drops taken off."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise FleetError(f"{label} is missing") from None
    except OSError:
        raise FleetError(f"{label} cannot be read as a plain file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FleetError(f"{label} is not a plain file")
        raw = os.read(fd, POINTER_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise FleetError(f"{label} is not plain text") from None
    if len(raw) > POINTER_MAX_BYTES or not text or any(char in text for char in "\r\n\0"):
        raise FleetError(f"{label} is not one plain line")
    return text


def _pointed(base: str, target: str, label: str) -> str:
    """The absolute path a pointer file names, relative ones from base (a folder already checked to have no symlink).
    A relative one may climb only at its start (../../x), so reading it by spelling and git following it on disk
    agree; the result must have plain characters and no symlink on the way."""
    if not target.startswith("/"):
        parts = target.split("/")
        climb = 0
        while climb < len(parts) and parts[climb] == "..":
            climb += 1
        if any(part in ("", ".", "..") for part in parts[climb:]):
            raise FleetError(f"{label} is not a plain path")
        target = os.path.normpath(f"{base}/{target}")
    path = check_safe_path(target, label)
    if os.path.realpath(path) != path:
        raise FleetError(f"{label} must not go through a symlink")
    return path



def same_checkout(first: str, second: str) -> bool:
    """Whether two checkout paths name one real folder, by device and inode rather than by spelling, since a
    case-insensitive disk takes one checkout under paths that differ only in letter case."""
    try:
        one, two = os.lstat(first), os.lstat(second)
    except OSError:
        return False
    return stat.S_ISDIR(one.st_mode) and stat.S_ISDIR(two.st_mode) and os.path.samestat(one, two)


def check_branch(name: object) -> str:
    """A branch the fleet makes and pushes: lowercase, plain and free of fleet words, since teammates see it."""
    if not isinstance(name, str) or BRANCH.fullmatch(name) is None or ".." in name \
            or name.endswith((".lock", "/", ".")) or "//" in name or "/." in name:
        raise FleetError("branch names use lowercase letters, digits, dot, dash, underscore and slash")
    fleet_word = fleet_words_in(name)
    if fleet_word:
        raise FleetError(f"the branch name contains a fleet word ({fleet_word})")
    return name


def check_lineage_branch(git_dir: str, name: object) -> str:
    """A branch Ryan made, which an own-session review records as its lineage: any name git itself takes as a
    branch (git check-ref-format --branch, run here after the store's own rule), in 1 to 255 bytes of
    printable ASCII with no whitespace. Capitals and fleet words are fine, since the name is only compared,
    never pushed or shown to teammates. The refusal never repeats the name."""
    try:
        name = pensieve.check_review_branch(name)
    except ValidationError as exc:
        raise FleetError(str(exc)) from None
    try:
        out = git(["check-ref-format", "--branch", name], git_dir)
    except FleetError:
        raise FleetError("git does not take it as a branch name") from None
    if out != name + "\n":  # --branch expands a shorthand such as @{-1}, so the name must come back unchanged
        raise FleetError("git reads it as a shorthand for another branch, not as a branch name")
    return name


def current_branch(git_dir: str) -> Optional[str]:
    """The branch a checkout has out, unchecked, or None when its HEAD is detached."""
    ref = git(["symbolic-ref", "--quiet", "HEAD"], git_dir, check=False).strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") and len(ref) > len("refs/heads/") else None


def has_branch(git_dir: str, name: str) -> bool:
    """Whether the checkout has exactly this local branch."""
    out = git(["for-each-ref", "--format=%(refname)", f"refs/heads/{name}"], git_dir, check=False)
    return f"refs/heads/{name}" in out.splitlines()


def check_ref(name: object, label: str = "ref") -> str:
    if not isinstance(name, str) or REF.fullmatch(name) is None or ".." in name or name.endswith(".lock"):
        raise FleetError(f"{label} is not a plain git ref")
    return name


def fleet_words_in(text: str) -> Optional[str]:
    """The first fleet word in text, matched as a whole word, or None. Fleet names never leave the fleet."""
    for word in WORD_SPLIT.split(text.lower()):
        if word in config.FLEET_WORDS:
            return word
    return None


def repo_slug(url: str) -> str:
    """owner/repo for a GitHub remote URL, checked against the store's repo id rule."""
    match = GITHUB_URL.fullmatch(url.strip())
    if match is None:
        raise FleetError("the remote is not a plain GitHub URL")
    return ids.check("repo", f"{match.group(1)}/{match.group(2)}")


# Running git


def child_env() -> dict:
    return {
        "HOME": config.USER_HOME_DIR,
        "PATH": config.CHILD_PATH,
        "LANG": "en_US.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        **config.GIT_NO_LAZY_FETCH_ENV,
        "RTK_DISABLED": "1",
    }


# Ancestry is read from the commits as written: replace refs and an info/grafts file can hide a parent link
# without changing any sha, and a commit-graph file can answer for a commit that is gone.
TRUE_HISTORY_FLAGS = ("--no-replace-objects", "-c", "core.commitGraph=false")
TRUE_HISTORY_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null/no-grafts"}


def git(args: list, git_dir: Optional[str], work_tree: Optional[str] = None, check: bool = True,
        timeout: Optional[int] = None, folder: Optional[str] = None, whole: bool = False) -> str:
    """Run one git command with the hardening flags. Returns stdout, cut at OUTPUT_MAX_CHARS. Never uses a shell.
    whole is for output that is checked, such as the commit messages and the diff a push scans: all of stdout comes
    back, and more than SCAN_MAX_CHARS is refused, so nothing past a cut is ever passed unread. A failure quotes what
    git printed, scrubbed whole before it is cut (common.scrubbed_line)."""
    code, out, err = _run(args, git_dir, work_tree, timeout, folder, whole=whole)
    if check and code != 0:
        raise FleetError(f"git {args[0]} failed: {common.scrubbed_line(err, 300)}")
    return out


def _run(args: list, git_dir: Optional[str], work_tree: Optional[str], timeout: Optional[int],
         folder: Optional[str], true_history: bool = False, whole: bool = False) -> tuple:
    """(exit code, stdout, stderr) of one hardened git command. true_history reads commits as written. whole returns
    all of stdout, or refuses it past SCAN_MAX_CHARS (see git)."""
    if git_dir is not None:
        argv = [config.GIT_BIN, "--git-dir", git_dir]
        if work_tree is not None:
            argv += ["--work-tree", work_tree]
        cwd = work_tree or git_dir
    elif folder is not None:
        argv, cwd = [config.GIT_BIN, "-C", folder], folder
    else:
        raise FleetError("git needs a git folder or a working folder")
    argv += [*HARDENING, *(TRUE_HISTORY_FLAGS if true_history else ()), *args]
    env = {**child_env(), **(TRUE_HISTORY_ENV if true_history else {})}
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout or config.GIT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise FleetError(f"git {args[0]} timed out") from None
    out = done.stdout.decode("utf-8", "replace")
    if whole and len(out) > SCAN_MAX_CHARS:
        raise FleetError(f"git {args[0]} printed more than {SCAN_MAX_CHARS} characters, too much to check whole")
    return done.returncode, out if whole else out[:OUTPUT_MAX_CHARS], done.stderr.decode("utf-8", "replace")


# The draft PR


def check_pr_title(title: object) -> str:
    """One printable line of at most PR_TITLE_MAX characters that cannot be read as a flag."""
    if not isinstance(title, str) or not title or len(title) > PR_TITLE_MAX \
            or common.one_line(title, PR_TITLE_MAX) != title or title.startswith("-"):
        raise FleetError("the PR title must be one printable line of at most 100 characters, not starting with -")
    return title


def draft_pr_argv(repo: str, head: str, base: str, title: str) -> list:
    """gh pr create for a draft PR, with the body read from stdin. Nothing in it can mark the PR ready or merge it."""
    return [config.GH_BIN, "pr", "create", "--draft", "--repo", ids.check("repo", repo), "--head", check_branch(head),
            "--base", check_ref(base, "PR base"), "--title", check_pr_title(title), "--body-file", "-"]


class Uncertain(FleetError):
    """gh may or may not have done what it was asked (it timed out, printed more than the fleet reads, or answered
    something that does not parse). Nothing is tried again; the caller reads GitHub back instead."""


class Refused(FleetError):
    """GitHub, or gh before it reached GitHub, refused the write, so it surely did not happen."""


def check_draft_pr_argv(argv: object) -> None:
    """Refuse every gh command but the exact draft PR shape draft_pr_argv builds, checked again field by field."""
    if not isinstance(argv, list) or len(argv) != 14 or argv[:4] != [config.GH_BIN, "pr", "create", "--draft"] \
            or argv[4::2] != ["--repo", "--head", "--base", "--title", "--body-file"] or argv[13] != "-":
        raise FleetError("the fleet runs no gh command but gh pr create --draft with its fixed flags")
    if draft_pr_argv(argv[5], argv[7], argv[9], argv[11]) != argv:
        raise FleetError("the draft PR command has a value it does not allow")


def gh_env() -> dict:
    """git's fixed environment, plus the account name, which gh finds its keychain login under, and no prompts."""
    account = os.path.basename(config.USER_HOME_DIR)
    return {**child_env(), "USER": account, "LOGNAME": account, "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}


def reply_argv(repo: str, number: int, comment_id: str) -> list:
    """gh api for one reply in a review thread, to the thread's first comment, with its JSON on stdin."""
    number = ids.check_int(number, "PR number", minimum=1, maximum=9999999999)
    if not isinstance(comment_id, str) or COMMENT_ID.fullmatch(comment_id) is None:
        raise FleetError("a reply goes to a comment named by its decimal id")
    return [config.GH_BIN, "api", "--method", "POST",
            f"repos/{ids.check('repo', repo)}/pulls/{number}/comments/{comment_id}/replies", "--input", "-"]


def pr_comment_argv(repo: str, number: int) -> list:
    """gh api for one comment on a PR's conversation, with its JSON on stdin."""
    number = ids.check_int(number, "PR number", minimum=1, maximum=9999999999)
    return [config.GH_BIN, "api", "--method", "POST", f"repos/{ids.check('repo', repo)}/issues/{number}/comments",
            "--input", "-"]


def check_write_argv(argv: object, repo: Optional[str] = None) -> str:
    """The one guard on every gh command that writes to GitHub: the draft PR, a reply in a review thread, or a PR
    comment, each rebuilt from its parts and compared whole, and, when repo is given, aimed at that repo only. Which
    one it is ("draft-pr", "reply" or "pr-comment"), or a FleetError for anything else: a ready or merged PR, a
    resolved thread, a review request, a mutation, another method, another repo, another path or an extra flag."""
    if isinstance(argv, list) and argv[:4] == [config.GH_BIN, "pr", "create", "--draft"]:
        check_draft_pr_argv(argv)
        if repo is not None and argv[5].lower() != repo.lower():
            raise FleetError("the fleet writes to GitHub only on the repo it was asked to")
        return "draft-pr"
    if not isinstance(argv, list) or len(argv) != 7 or argv[:4] != [config.GH_BIN, "api", "--method", "POST"] \
            or argv[5:] != ["--input", "-"] or not isinstance(argv[4], str):
        raise FleetError("the fleet writes to GitHub only with its three fixed gh commands")
    path = argv[4]
    if path.startswith("repos/"):
        found = None
        reply = REPLY_PATH.fullmatch(path[len("repos/"):])
        if reply is not None and reply_argv(reply.group(1), int(reply.group(2)), reply.group(3)) == argv:
            found = ("reply", reply.group(1))
        comment = PR_COMMENT_PATH.fullmatch(path[len("repos/"):])
        if comment is not None and pr_comment_argv(comment.group(1), int(comment.group(2))) == argv:
            found = ("pr-comment", comment.group(1))
        if found is not None:
            if repo is not None and found[1].lower() != repo.lower():
                raise FleetError("the fleet writes to GitHub only on the repo it was asked to")
            return found[0]
    raise FleetError("the fleet writes to GitHub only with its three fixed gh commands")


def run_gh_write(argv: list, body: bytes) -> tuple:
    """(exit code, stdout, stderr) of one checked reply or PR comment, run once in the office with its JSON on stdin.
    Tests replace this. A timeout raises Uncertain: the comment may or may not be there."""
    if check_write_argv(argv) == "draft-pr":
        raise FleetError("the draft PR runs through run_gh_pr")
    try:
        done = subprocess.run(argv, cwd=config.OFFICE_ROOT, env=gh_env(), input=body, capture_output=True,
                              timeout=config.GH_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise Uncertain("gh did not answer in time, so the comment may or may not be posted") from None
    except OSError:
        raise Refused(f"gh is not at {config.GH_BIN}; set GH_BIN in fleet/config.py") from None
    return (done.returncode, done.stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
            done.stderr.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS])


def _post(argv: list, text: str, repo: str, number: int, fragment: str) -> dict:
    """Run one write and read its answer strictly: {"id", "url", "login"} of the comment GitHub made. Refused when
    GitHub (or gh before it) refused it, so it surely was not posted; Uncertain for anything that does not say."""
    if not isinstance(text, str) or not text.strip() or len(text) > WRITE_BODY_MAX or "\x00" in text:
        raise FleetError("a comment is text of at most 65536 characters")
    body = json.dumps({"body": text}, ensure_ascii=True).encode("ascii")
    check_write_argv(argv, repo)
    code, out, err = run_gh_write(argv, body)
    if len(out) >= OUTPUT_MAX_CHARS or len(err) >= OUTPUT_MAX_CHARS:
        raise Uncertain("gh printed more than the fleet reads, so nothing it printed is repeated and the comment may"
                        " or may not be posted")
    if code != 0:
        login = code == GH_AUTH_EXIT or GH_AUTH_WORDS.search(err) is not None
        if code == GH_AUTH_EXIT or (login and GH_HTTP_REFUSED.search(err)):
            raise Refused("gh is not signed in to GitHub, or GitHub refused its login: check gh auth status in your"
                          " terminal")
        if login:
            raise Uncertain("gh reported a login problem without GitHub's refusal, so the comment may or may not be"
                            " posted: check gh auth status in your terminal")
        last = [line for line in pensieve.scrub(err).splitlines() if line.strip()]
        line = common.scrubbed_line(last[-1] if last else f"exit {code}", 200)
        if GH_HTTP_REFUSED.search(err):
            raise Refused(f"GitHub refused the comment: {line}")
        raise Uncertain(f"gh exited {code} without GitHub's answer, so the comment may or may not be posted: {line}")
    try:
        answer = common.strict_json(out.encode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Uncertain("gh's answer is not JSON, so the comment may or may not be posted") from None
    comment_id = answer.get("id") if isinstance(answer, dict) else None
    login = answer.get("user", {}).get("login") if isinstance(answer, dict) and isinstance(answer.get("user"), dict) \
        else None
    if type(comment_id) is not int or not 0 < comment_id < 10 ** 20 or not isinstance(login, str) \
            or GH_LOGIN.fullmatch(login) is None:
        raise Uncertain("gh's answer names no comment, so it may or may not be posted")
    url = f"https://github.com/{repo}/pull/{number}#{fragment}{comment_id}"
    html_url = answer.get("html_url")
    if not isinstance(html_url, str) or html_url.lower() != url.lower():
        raise Uncertain("gh's answer links somewhere else, so the comment may or may not be posted where it should")
    return {"id": str(comment_id), "url": html_url, "login": login}


def post_reply(repo: str, number: int, comment_id: str, text: str) -> dict:
    """Post one reply in a review thread, to the thread's first comment (GitHub refuses a reply to a reply)."""
    return _post(reply_argv(repo, number, comment_id), text, repo, number, "discussion_r")


def post_pr_comment(repo: str, number: int, text: str) -> dict:
    """Post one comment on a PR's conversation."""
    return _post(pr_comment_argv(repo, number), text, repo, number, "issuecomment-")


def remote_tip(record: dict, branch: str) -> Optional[str]:
    """The commit origin's branch points at now, read with git ls-remote, or None when it cannot be read for sure:
    git failed, or not exactly one line names refs/heads/<branch> with a full sha."""
    ref = f"refs/heads/{check_branch(branch)}"
    try:
        code, out, _ = _run(["ls-remote", "origin", ref], record["common_dir"], None, None, None)
    except FleetError:
        return None
    if code != 0:
        return None
    found = []
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            found.append(sha.strip())
    if len(found) != 1 or SHA.fullmatch(found[0]) is None:
        return None
    return found[0]


def run_gh_pr(argv: list, body: bytes) -> tuple:
    """(exit code, stdout, stderr) of the checked draft PR command, run once in the office. Tests replace this."""
    check_draft_pr_argv(argv)
    try:
        done = subprocess.run(argv, cwd=config.OFFICE_ROOT, env=gh_env(), input=body, capture_output=True,
                              timeout=config.GH_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise FleetError("gh did not answer in time, so the PR may or may not be open: look at the repo's pull"
                         " requests") from None
    except OSError:
        raise FleetError(f"gh is not at {config.GH_BIN}; set GH_BIN in fleet/config.py") from None
    return (done.returncode, done.stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
            done.stderr.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS])


def open_draft_pr(repo: str, head: str, base: str, title: str, body: str) -> str:
    """Open a draft PR from head into base and return its URL. It runs once and is never retried. A login problem is
    named without anything gh printed, and any other failure keeps only the last line of what gh printed, scrubbed
    whole first, so no line of a credential is kept on its own. Output that may have been cut at OUTPUT_MAX_CHARS is
    never read for a login word, a line to repeat or the PR's URL."""
    if not isinstance(body, str) or not body.strip() or len(body) > PR_BODY_MAX or "\x00" in body:
        raise FleetError(f"the PR body must be text of at most {PR_BODY_MAX} characters")
    code, out, err = run_gh_pr(draft_pr_argv(repo, head, base, title), body.encode("utf-8"))
    if len(out) >= OUTPUT_MAX_CHARS or len(err) >= OUTPUT_MAX_CHARS:
        raise FleetError(f"gh exited {code} and printed more than the fleet reads, so nothing it printed is repeated"
                         " and the PR may or may not be open: look at the repo's pull requests")
    if code != 0:
        if code == GH_AUTH_EXIT or GH_AUTH_WORDS.search(err):
            raise FleetError("gh is not signed in to GitHub, or GitHub refused its login: check gh auth status in"
                             " your terminal")
        last = [line for line in pensieve.scrub(err).splitlines() if line.strip()]
        raise FleetError("gh pr create failed: " + common.scrubbed_line(last[-1] if last else f"exit {code}", 200))
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    match = PR_URL.fullmatch(lines[-1]) if lines else None
    if match is None or f"{match.group(1)}/{match.group(2)}".lower() != repo.lower():
        raise FleetError("gh named no PR of this repo, so it may or may not be open: look at the repo's pull requests")
    return lines[-1]


def is_ancestor(git_dir: str, ancestor: str, sha: str) -> Optional[bool]:
    """Whether commit ancestor is sha or one of its ancestors, by the parents its commits really name (no
    replace refs or grafts), or None when this checkout cannot tell: it is shallow, so its history may stop
    short of ancestor, or git failed. A commit missing from the object store is no ancestor only when every
    commit sha builds on is there, since a clone cut short with its shallow file removed says it is full."""
    if SHA.fullmatch(ancestor) is None or SHA.fullmatch(sha) is None:
        raise FleetError("an ancestry check needs two full commit shas")
    shallow = git(["rev-parse", "--is-shallow-repository"], git_dir).strip()
    if shallow not in ("true", "false"):
        return None
    if _run(["cat-file", "-e", f"{ancestor}^{{commit}}"], git_dir, None, None, None, true_history=True)[0] != 0:
        if shallow == "true":
            return None
        walked = _run(["rev-list", "--count", sha], git_dir, None, None, None, true_history=True)[0]
        return False if walked == 0 else None
    code = _run(["merge-base", "--is-ancestor", ancestor, sha], git_dir, None, None, None, true_history=True)[0]
    if code == 0:
        return True
    return False if code == 1 and shallow == "false" else None


def merge_point(git_dir: str, sha: str, tip: str, walk_max: int) -> Optional[str]:
    """The oldest commit on tip's first-parent line that has sha as an ancestor, read from the commits as written:
    sha itself when it sits on that line, else the commit that brought it in. None when this checkout cannot say:
    git failed, the checkout is shallow, sha is not in tip's history, or the line holds more than walk_max commits
    that have sha as an ancestor."""
    if SHA.fullmatch(sha) is None or SHA.fullmatch(tip) is None:
        raise FleetError("a merge point needs two full commit shas")
    code, out, _ = _run(["rev-list", "--first-parent", "-n", str(int(walk_max) + 1), tip], git_dir, None, None,
                        None, true_history=True, whole=True)
    line = out.split()
    if code != 0 or not line or line[0] != tip or any(SHA.fullmatch(item) is None for item in line):
        return None
    if is_ancestor(git_dir, sha, tip) is not True:
        return None
    low, high = 0, len(line) - 1  # line[low] has sha as an ancestor; find the oldest that does
    last = is_ancestor(git_dir, sha, line[high])
    if last is None:
        return None
    if last:
        return sha if line[high] == sha else None  # past the walk, unless the walk ended on sha itself
    while high - low > 1:
        middle = (low + high) // 2
        found = is_ancestor(git_dir, sha, line[middle])
        if found is None:
            return None
        low, high = (middle, high) if found else (low, middle)
    return line[low]


def fetch_branch(git_dir: str, branch: str) -> str:
    """Fetch origin's branch into its remote-tracking ref with an explicit refspec, so a checkout whose own refspec
    leaves that branch out (a single-branch clone) is never read at a stale tip, and return the fetched tip's sha."""
    branch = check_ref(branch, "branch")
    git(["fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], git_dir)
    sha = git(["rev-parse", "--verify", "--end-of-options", f"refs/remotes/origin/{branch}^{{commit}}"],
              git_dir).strip()
    if SHA.fullmatch(sha) is None:
        raise FleetError("git did not return a full commit sha for the fetched branch")
    return sha


def worktree_paths(git_dir: str) -> list:
    """Every worktree path git lists for the repo whose .git folder is git_dir. A read git fails refuses."""
    out = git(["worktree", "list", "--porcelain"], git_dir, whole=True)
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def is_shallow(git_dir: str) -> bool:
    """Whether this checkout says it is shallow. Only a shallow checkout can be cured with git fetch --unshallow;
    git refuses that on a full clone. False when git says it is full or cannot answer."""
    return git(["rev-parse", "--is-shallow-repository"], git_dir, check=False).strip() == "true"


def git_in(folder: str, args: list, check: bool = True, timeout: int = 10) -> str:
    """git in a folder Ryan's own session works in, found the normal way, with the hardening flags.

    Only the push gate uses this, for checks that must stay fast. Desk worktrees use git() instead.
    """
    return git(args, git_dir=None, work_tree=None, check=check, timeout=timeout, folder=folder)


def rev(record: dict, ref: str = "HEAD") -> str:
    sha = git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], record["git_dir"],
              record["path"]).strip()
    if SHA.fullmatch(sha) is None:
        raise FleetError("git did not return a full commit sha")
    return sha


def remote_slug(record: dict, remote: str = "origin") -> str:
    url = git(["config", "--get", f"remote.{remote}.url"], record["common_dir"]).strip()
    return repo_slug(url)


def link_excludes(record: dict) -> list:
    """Pathspecs that keep the worktree's read-only dependency links out of status and add."""
    return [f":(exclude){name}" for name in record.get("links") or []]


def borrowed_link(record: dict, name: str) -> bool:
    """Whether <worktree>/<name> is still the link to the repo folder's copy that the worktree script made."""
    path = f"{record['path']}/{name}"
    return os.path.islink(path) and os.readlink(path) == f"{record['repo_dir']}/{name}"


def check_links(record: dict) -> None:
    """Refuse a worktree whose dependency link was replaced. Status and add skip that path, so a replacement
    would let checks use files that no commit holds."""
    for name in record.get("links") or []:
        if os.path.lexists(f"{record['path']}/{name}") and not borrowed_link(record, name):
            raise FleetError(f"{name} in the worktree is no longer the read-only link to the repo folder's copy, "
                             "so checks could use files no commit holds; delete it from the worktree first")


def ignored(record: dict) -> list:
    """Every git-ignored path in the worktree as git names it, relative to its root, the dependency links included:
    a folder ignored whole is one entry ending in /. A read git fails refuses, and so does more than git can list
    whole."""
    out = git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory", "--no-empty-directory",
               "--", "."], record["git_dir"], record["path"], whole=True)
    return [name for name in out.split("\0") if name]


def flagged(record: dict) -> list:
    """Every tracked path in the worktree that git is told not to check: assume-unchanged (a lowercase tag in git
    ls-files -v) or skip-worktree (S). git status shows no change to such a file, and git worktree remove deletes it.
    A read git fails refuses, and so does more than git can list whole."""
    out = git(["ls-files", "-v", "-z", "--", "."], record["git_dir"], record["path"], whole=True)
    found = []
    for entry in out.split("\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1] != " ":
            raise FleetError("git ls-files -v printed an entry it could not read")
        if entry[0] == "S" or entry[0].islower():
            found.append(entry[2:])
    return found


def _clean(record: dict, mode: str) -> list:
    out = git(["clean", mode, "-d", "-X", "--", ".", *link_excludes(record)], record["git_dir"], record["path"])
    if len(out) >= OUTPUT_MAX_CHARS:
        raise FleetError("git clean listed more than the fleet can record")
    return out.splitlines()


def clean_ignored(record: dict) -> list:
    """Remove every git-ignored path in the worktree except the dependency links, and return each one as git
    names it (escaped when unusual).

    Status never shows ignored files, so a desk could leave one (a nested node_modules, a .env) that
    checks would use although no commit holds it. A dry run comes first: content git clean would skip,
    such as a nested git repository, or more paths than the evidence can list, is refused before anything
    is removed. Anything ignored that is still there afterwards is refused too.
    """
    planned = _clean(record, "-n")
    skipped = [line for line in planned if not line.startswith("Would remove ")]
    if skipped:
        raise FleetError("the worktree holds ignored content git clean would not remove, such as a nested git "
                         f"repository ({common.scrubbed_line(skipped[0], 200)}); remove it by hand, then verify again")
    names = [line[len("Would remove "):] for line in planned]
    if len(names) > CLEAN_MAX_PATHS or sum(len(name) for name in names) > CLEAN_MAX_CHARS:
        raise FleetError(f"the worktree holds {len(names)} git-ignored paths, more than the evidence can list; "
                         "remove them by hand, then verify again")
    removed = [line[len("Removing "):] for line in _clean(record, "-f") if line.startswith("Removing ")]
    left = git(["status", "--porcelain", "--ignored", "--untracked-files=all", "--", ".", *link_excludes(record)],
               record["git_dir"], record["path"]).strip()
    if removed != names or left:
        raise FleetError("git-ignored content is still in the worktree after the cleanup; remove it by hand, "
                         "then verify again")
    return removed


def dirty(record: dict) -> bool:
    """Whether the worktree has changes outside its dependency links. Refuses a replaced link."""
    check_links(record)
    return bool(git(["status", "--porcelain", "--untracked-files=all", "--", ".", *link_excludes(record)],
                    record["git_dir"], record["path"]).strip())


# Office records


def record_name(worktree_path: str) -> str:
    """The record name for a castle worktree path: its folder name under the worktrees root."""
    prefix = config.CASTLE_ROOT + "/worktrees/"
    if not worktree_path.startswith(prefix):
        raise FleetError("the worktree is not under the castle worktrees folder")
    return safefs.check_component(worktree_path[len(prefix):])


def _check_record(data: object, name: str) -> dict:
    if not isinstance(data, dict):
        raise FleetError("worktree record is not a JSON object")
    record = {
        "name": data.get("name"),
        "task_id": data.get("task_id"),
        "path": data.get("path"),
        "repo_dir": data.get("repo_dir"),
        "common_dir": data.get("common_dir"),
        "git_dir": data.get("git_dir"),
        "branch": data.get("branch"),
        "base": data.get("base"),
        "base_ref": data.get("base_ref"),
        "repo": data.get("repo"),
        "links": data.get("links") or [],
    }
    if record["name"] != name:
        raise FleetError("worktree record name does not match its file")
    ids.check("task", record["task_id"])
    if record["path"] != f"{config.CASTLE_ROOT}/worktrees/{name}":
        raise FleetError("worktree record path is not its castle worktree")
    for key in ("repo_dir", "common_dir", "git_dir"):
        check_safe_path(record[key], f"worktree record {key}")
    # The repo folder is a main checkout, whose .git folder is common_dir, or a linked worktree of the main checkout
    # whose .git folder it is (repo_dirs, run when the record was written).
    if not record["common_dir"].endswith("/.git"):
        raise FleetError("worktree record common_dir is not a main checkout's .git folder")
    if record["common_dir"] != record["repo_dir"] + "/.git":
        for key in ("repo_dir", "common_dir"):
            _check_place(record[key], f"worktree record {key}")
    if record["git_dir"] != f"{record['common_dir']}/worktrees/{name}":
        raise FleetError("worktree record git_dir is not the repo's entry for this worktree")
    if record["branch"] is not None:
        check_branch(record["branch"])
    check_ref(record["base"], "worktree record base")
    if record["base_ref"] is not None:  # records from before the base was kept as a commit have no base_ref
        check_ref(record["base_ref"], "worktree record base_ref")
    ids.check("repo", record["repo"])
    if not isinstance(record["links"], list) or any(name not in config.LINKABLE_DEPS for name in record["links"]):
        raise FleetError("worktree record links name a folder that is not a linkable dependency")
    return record


def read_record(name: str) -> dict:
    name = safefs.check_component(name)
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR) as fd:
        raw = safefs.read_regular(fd, f"{name}.json", RECORD_MAX_BYTES, "worktree record")
    try:
        data = common.strict_json(raw)
    except (UnicodeDecodeError, ValueError):
        raise FleetError("worktree record is not strict JSON") from None
    return _check_record(data, name)


def find_record(worktree_path: Optional[str]) -> Optional[dict]:
    """The record for a castle worktree, or None when there is no worktree or no record."""
    if not worktree_path:
        return None
    try:
        return read_record(record_name(worktree_path))
    except safefs.Missing:
        return None


def drop_record(name: str) -> None:
    """Remove the office record of a worktree that was taken back before any task held it."""
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR) as fd:
        os.unlink(f"{safefs.check_component(name)}.json", dir_fd=fd)


def write_record(record: dict) -> dict:
    record = _check_record(record, record["name"])
    data = (json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")
    with safefs.opened_dir(config.OFFICE_ROOT, RECORD_DIR, create=True) as fd:
        safefs.write_new(fd, f"{record['name']}.json", data)
    return record
