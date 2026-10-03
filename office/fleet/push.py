"""The push script: push a build task's branch, pinned to the commit that passed review.

Ryan runs it from his terminal:
  fleet push <task-id> [--yes]

It refuses unless:
- the task is a build desk's task with a worktree record and a branch;
- the worktree is clean and HEAD has a pass in the store, so the other family's reviewer said PASS
  for this exact commit;
- the branch name, every commit message from the base to HEAD, and every added line of the diff are
  free of fleet words.
It then shows the repo, branch, commit and commit list, and waits for Ryan to type the branch name
(--yes skips that). It pushes exactly the reviewed commit, `git push origin <sha>:refs/heads/<branch>`,
never with --force, so a remote branch that moved makes git refuse. It opens no PR: it prints the
gh command for a draft PR, which Ryan runs after reading the PR text.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from hogwarts import ids, owlery, pensieve

from fleet import config, gitops, worktree
from fleet.safefs import FleetError

ADDED_LINE_PREFIX = "+"
MAX_LISTED = 20


def _fleet_word_hits(record: dict, sha: str) -> list:
    """file:line for each added line in base...sha that contains a fleet word, capped."""
    diff = gitops.git(["diff", "--no-ext-diff", "--no-textconv", "-U0", f"{record['base']}...{sha}"],
                      record["git_dir"], record["path"])
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
    messages = gitops.git(["log", "--format=%B", f"{record['base']}..{sha}"], record["git_dir"], record["path"])
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
    refspec = f"{sha}:refs/heads/{branch}"
    try:
        gitops.git(["push", "origin", refspec], record["git_dir"], record["path"])
    except FleetError as exc:
        raise FleetError(f"{exc}. To push by hand: git -C {record['path']} push origin {refspec}") from None
    subject = gitops.git(["log", "-1", "--format=%s", sha], record["git_dir"], record["path"]).strip()
    title = subject.replace("\\", "").replace('"', "'")
    draft = f'gh pr create --draft --repo {record["repo"]} --head {branch} --title "{title}" --body-file <file>'
    return {"task_id": plan["task"]["id"], "repo": record["repo"], "branch": branch, "sha": sha,
            "draft_pr_command": draft}


def ask_terminal(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline()
