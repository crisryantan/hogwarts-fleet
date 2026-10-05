"""The push script: push a build task's branch, pinned to the commit that passed review.

Ryan runs it from his terminal:
  fleet push <task-id> [--yes]

It refuses unless:
- the task is a build desk's task with a worktree record and a branch;
- the worktree is clean and HEAD has a pass in the store, so the other family's reviewer said PASS
  for this exact commit;
- the branch name, every commit message from the base to HEAD, and every added line of the diff are
  free of fleet words. Each scan reads all that git prints, or refuses the push when it is too much to read.
It then shows the repo, branch, commit and commit list, and waits for Ryan to type the branch name
(--yes skips that). It pushes exactly the reviewed commit, `git push origin <sha>:refs/heads/<branch>`,
never with --force, so a remote branch that moved makes git refuse. It opens no PR: it prints the
gh command for a draft PR, which Ryan runs after reading the PR text.

push_draft_pr is the review loop's push after a PASS, only while Ryan has opted in with the one file
config.AUTO_DRAFT_PR_FILE in the office (auto_draft_pr_on). His opt-in stands in for the typed branch name;
every other check is check()'s, run the same way. It pushes only the commit that passed, to the branch the
worktree record holds, then opens a draft PR whose title is the handoff's commit subject and whose body is
its PR BODY DRAFT, through gitops.open_draft_pr. Before anything is pushed, the PR text and the commit
messages are refused when they hold a fleet word or anything shaped like a credential, key or email. It
never opens a ready PR, merges, forces or retries. Any failure stops it where it is and raises a
FleetError naming what was and was not done.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from hogwarts import ids, owlery, pensieve

from fleet import common, config, gitops, safefs, worktree
from fleet.safefs import FleetError

ADDED_LINE_PREFIX = "+"
MAX_LISTED = 20
OPT_IN_MAX_BYTES = 64
# What pensieve.scrub puts in place of a credential, key or email. Text any of its patterns matches this way never
# goes out by itself. Its hex and IP address marks are left out, since commit shas and version numbers look like them.
SENSITIVE_MARKS = ("[private_key]", "[credentials]", "[jwt]", "[token]", "[secret]", "[aws_key]", "[email]")


def _fleet_word_hits(record: dict, sha: str) -> list:
    """file:line for each added line in base...sha that contains a fleet word, capped. Every line of the diff is
    read, or the push is refused (gitops.git whole)."""
    diff = gitops.git(["diff", "--no-ext-diff", "--no-textconv", "-U0", f"{record['base']}...{sha}"],
                      record["git_dir"], record["path"], whole=True)
    hits, path, line_no = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            try:
                line_no = int(line.split("+", 1)[1].split(",", 1)[0].split(" ", 1)[0]) - 1
            except (IndexError, ValueError):
                line_no = 0
        elif line.startswith(ADDED_LINE_PREFIX):
            line_no += 1
            word = gitops.fleet_words_in(line[1:])
            if word:
                hits.append(f"{path}:{line_no} ({word})")
    return hits[:MAX_LISTED]


def check(conn, task_id: str) -> dict:
    """Everything the push needs, or a FleetError naming the first thing that is wrong."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS:
        raise FleetError("only a build desk's task is pushed by this script; push your own work by hand")
    record = gitops.find_record(worktree.castle_path(task["worktree"]))
    if record is None or record["branch"] is None:
        raise FleetError("this task has no worktree record with a branch")
    if gitops.dirty(record):
        raise FleetError("the worktree has uncommitted changes")
    sha = gitops.rev(record)
    if not owlery.has_pass(conn, record["repo"], sha):
        raise FleetError("HEAD has no review pass from the other model family; run fleet review first")
    branch = gitops.check_branch(record["branch"])
    if record["repo"] not in config.FLEET_WORDS_ALLOWED_REPOS:
        messages = gitops.git(["log", "--format=%B", f"{record['base']}..{sha}"], record["git_dir"], record["path"],
                              whole=True)
        word = gitops.fleet_words_in(messages)
        if word:
            raise FleetError(f"a commit message contains a fleet word ({word})")
        hits = _fleet_word_hits(record, sha)
        if hits:
            raise FleetError("added lines contain fleet words: " + ", ".join(hits))
    commits = gitops.git(["log", "--no-decorate", "--oneline", f"{record['base']}..{sha}"], record["git_dir"],
                         record["path"]).strip().splitlines()
    return {"task": task, "record": record, "sha": sha, "branch": branch, "commits": commits}


def push(conn, task_id: str, confirm: Optional[Callable[[str], str]] = None) -> dict:
    plan = check(conn, task_id)
    record, sha, branch = plan["record"], plan["sha"], plan["branch"]
    summary = (f"Repo {record['repo']}\nBranch {branch}\nCommit {sha}\n"
               + "".join(f"  {line}\n" for line in plan["commits"][:MAX_LISTED]))
    if confirm is not None:
        answer = confirm(summary + "Type the branch name to push it, or anything else to stop: ")
        if answer.strip() != branch:
            raise FleetError("not pushed: the branch name was not typed")
    _push_exact(record, sha, branch)
    subject = gitops.git(["log", "-1", "--format=%s", sha], record["git_dir"], record["path"]).strip()
    title = subject.replace("\\", "").replace('"', "'")
    draft = f'gh pr create --draft --repo {record["repo"]} --head {branch} --title "{title}" --body-file <file>'
    return {"task_id": plan["task"]["id"], "repo": record["repo"], "branch": branch, "sha": sha,
            "draft_pr_command": draft}


def _push_exact(record: dict, sha: str, branch: str) -> str:
    """git push origin <sha>:refs/heads/<branch>: exactly that commit, never forced, so a remote branch that moved
    makes git refuse."""
    refspec = f"{sha}:refs/heads/{branch}"
    try:
        gitops.git(["push", "origin", refspec], record["git_dir"], record["path"])
    except FleetError as exc:
        raise FleetError(f"{exc}. To push by hand: git -C {record['path']} push origin {refspec}") from None
    return refspec


def auto_draft_pr_on() -> bool:
    """Whether Ryan opted in to the automatic draft PR: the plain file config.AUTO_DRAFT_PR_FILE in the office, his
    own and writable by no one else, reached with no link on the way, holds exactly "on". It is read from nowhere
    else, so nothing a desk can write turns it on. Missing, unreadable or anything else is off."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT) as fd:
            raw = safefs.read_regular(fd, config.AUTO_DRAFT_PR_FILE, OPT_IN_MAX_BYTES, "the draft PR opt-in")
    except (FleetError, OSError):
        return False
    return raw.strip() == b"on"


def sensitive_mark(text: str) -> Optional[str]:
    """What text holds that looks like a credential, key or email, or None. Each of pensieve.scrub's patterns is
    matched directly, in the order scrub applies them, so a match counts whatever it would be replaced with: a
    mark already in the text, such as a password that holds the word [secret], never hides one."""
    for pattern, replacement in pensieve._SCRUBBERS:  # scrub's own patterns, so the two never drift apart
        for match in pattern.finditer(text):
            put = replacement if isinstance(replacement, str) else replacement(match)
            mark = next((mark for mark in SENSITIVE_MARKS if mark in put), None)
            if mark is not None:
                return mark.strip("[]")
        text = pattern.sub(replacement, text)
    return None


def pr_base(record: dict) -> str:
    """The branch the PR asks to merge into: the origin branch the worktree's base named."""
    base_ref = record.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref.startswith("origin/"):
        raise FleetError("the worktree's base is not a branch of origin, so the PR has no base branch to name")
    return gitops.check_ref(base_ref[len("origin/"):], "PR base")


def check_pr_text(record: dict, title: str, body: str) -> None:
    """Refuse PR text that holds a fleet word (outside the kit's own repo) or anything shaped like a credential."""
    gitops.check_pr_title(title)
    if not isinstance(body, str) or not body.strip() or len(body) > gitops.PR_BODY_MAX or "\x00" in body:
        raise FleetError(f"the PR body must be text of at most {gitops.PR_BODY_MAX} characters")
    if record["repo"] not in config.FLEET_WORDS_ALLOWED_REPOS:
        word = gitops.fleet_words_in(title + "\n" + body)
        if word:
            raise FleetError(f"the PR text contains a fleet word ({word})")
    mark = sensitive_mark(title + "\n" + body)
    if mark is not None:
        raise FleetError(f"the PR text holds what looks like a credential or personal data ({mark}); read it and"
                         " open the PR by hand")


def push_draft_pr(conn, task_id: str, sha: str, title: str, body: str,
                  on_step: Optional[Callable[[str], None]] = None) -> dict:
    """Push exactly the commit that passed review and open a draft PR for it (see the module notes). Nothing is
    pushed unless every check passes first, and a draft PR that fails to open after the push says so. on_step is
    called with "push" just before the push starts and "pr" just before gh starts, so a caller can record each as
    begun first; when it raises, that step never starts."""
    plan = check(conn, task_id)
    record, branch = plan["record"], plan["branch"]
    if not isinstance(sha, str) or gitops.SHA.fullmatch(sha) is None or plan["sha"] != sha:
        raise FleetError("HEAD is not the commit that passed review, so nothing was pushed")
    base = pr_base(record)
    check_pr_text(record, title, body)
    messages = gitops.git(["log", "--format=%B", f"{record['base']}..{sha}"], record["git_dir"], record["path"],
                          whole=True)
    mark = sensitive_mark(messages)
    if mark is not None:
        raise FleetError(f"a commit message holds what looks like a credential or personal data ({mark}), so"
                         " nothing was pushed")
    if on_step is not None:
        on_step("push")
    _push_exact(record, sha, branch)
    if on_step is not None:
        try:
            on_step("pr")
        except (FleetError, OSError):
            raise FleetError(f"{sha[:12]} is pushed to {branch}, but the draft PR was not opened, since its start"
                             " could not be recorded first") from None
    try:
        url = gitops.open_draft_pr(record["repo"], branch, base, title, body)
    except FleetError as exc:
        raise FleetError(f"{sha[:12]} is pushed to {branch}, but the draft PR did not open: {exc}") from None
    return {"task_id": plan["task"]["id"], "repo": record["repo"], "branch": branch, "base": base, "sha": sha,
            "pr_url": common.one_line(url, 200)}


def ask_terminal(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline()
