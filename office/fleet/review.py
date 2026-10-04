"""The review script: the cross-model review for one commit, recorded from the reviewer's own output.

Ryan runs it from his terminal.

  fleet review <task-id>
      A build desk's task (Harry, Codex). Commits the desk's uncommitted work with the message from
      its handoff (outside the sandbox, through gitops, so no hook runs), then reviews HEAD.
  fleet review own --repo-dir <checkout> --title "<what this change does>" [--intent-file F] [--task <id>]
      A commit from one of Ryan's own Claude sessions. Makes a task for ryan-claude-1 with a TASK.md,
      and a detached worktree at that checkout's HEAD, so the reviewer reads exactly that commit.
      --task <id> reviews a fix round on the same task.

Then, for either:
1. verify runs the acceptance checks and writes evidence for this sha;
2. the commit is recorded on the author's task;
3. a review request goes from the author's task to the reviewer of the other family
   (Codex work to Hermione, Claude work to Moody), with an inbox copy for the reviewer. It is the
   task's next round: rounds past REVIEW_ROUND_CAP are refused until Ryan runs
   castle task allow-round <task-id>. Only a reviewer run that recorded a verdict uses up a round;
   one that crashed, timed out, was refused by a cap or hit a vendor limit does not, and the daily
   run caps bound those retries. A review of this task still waiting for its reviewer
   (a cap refused it, or the reviewer was busy) is superseded, so only the newest commit is reviewed;
4. run_desk runs the reviewer, which needs Ryan's enabled file for that desk. A reviewer at its
   daily cap leaves the request waiting, and Ryan hears which cap and when it resets;
5. the last REVIEW block in the reviewer's own output must name this task and this sha. Its verdict
   is recorded with the review file in the office, where no desk can change it;
6. on PASS the author's task moves to awaiting_close, which is what the push gate checks.
   CHANGES leaves it active for a fix round. HEADMASTER leaves it active and tells Ryan.

The reviewer's task is closed as superseded once its verdict is recorded, so the desk is free for the
next review. Only Ryan closes a task as complete.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Optional

from hogwarts import capacity, ids, owlery, pensieve
from hogwarts.errors import ConflictError

from fleet import common, config, gitops, owl_post, run_desk, safefs, verify, worktree
from fleet.safefs import FleetError

REVIEW_HEADER = re.compile(r"REVIEW (tk_[0-9a-f]{16}) @ ([0-9a-f]{40})")
VERDICT_LINE = re.compile(r"VERDICT: (PASS|CHANGES|HEADMASTER)")
SECTION_HEADER = re.compile(r"[A-Z][A-Z -]{2,40}(?: \(.*\))?")
COMMIT_SUBJECT_MAX = 100
COMMIT_MESSAGE_MAX = 4000
REVIEW_MAX_BYTES = 262144
OWN_DESK = config.OWN_SESSION_DESK


# Reading desk output


def review_block(text: str, task_id: str, sha: str) -> tuple:
    """(verdict, block) from the last REVIEW block, which must name this task and this sha."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if REVIEW_HEADER.fullmatch(line.strip())]
    if not starts:
        raise FleetError("the reviewer's output has no REVIEW block")
    block = lines[starts[-1]:]
    header = REVIEW_HEADER.fullmatch(block[0].strip())
    if (header.group(1), header.group(2)) != (task_id, sha):
        raise FleetError("the reviewer's REVIEW block names a different task or commit")
    verdicts = [match.group(1) for match in (VERDICT_LINE.fullmatch(line.strip()) for line in block) if match]
    if not verdicts:
        raise FleetError("the reviewer's REVIEW block has no VERDICT line")
    return verdicts[-1], "\n".join(block).strip() + "\n"


def reviewer_output(desk: str, family: str, run_id: str) -> str:
    """The reviewer's final text: Claude's JSON result, or Codex's last message file."""
    run_id = safefs.check_component(run_id)
    with safefs.opened_dir(config.OFFICE_ROOT, "runs", desk) as fd:
        if family == "codex":
            raw = safefs.read_regular(fd, f"{run_id}-last-message.md", REVIEW_MAX_BYTES, "review output")
            return raw.decode("utf-8", "replace")
        raw = safefs.read_regular(fd, f"{run_id}.out", run_desk.RUN_OUTPUT_MAX_BYTES, "review output")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FleetError("the reviewer's run output is not JSON") from None
    if isinstance(data, list):
        data = next((item for item in reversed(data) if isinstance(item, dict) and item.get("type") == "result"), {})
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, str):
        raise FleetError("the reviewer's run output has no result text")
    return result


def commit_message(handoff: str, check_words: bool = True) -> tuple:
    """(subject, body) from the COMMIT MESSAGE section of a build desk's handoff."""
    lines = handoff.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "COMMIT MESSAGE") + 1
    except StopIteration:
        raise FleetError("the handoff has no COMMIT MESSAGE section") from None
    section = []
    for line in lines[start:]:
        if SECTION_HEADER.fullmatch(line.strip()):
            break
        section.append(line.rstrip())
    while section and not section[0].strip():
        section.pop(0)
    if not section:
        raise FleetError("the handoff's COMMIT MESSAGE section is empty")
    subject, body = section[0].strip(), "\n".join(section[1:]).strip()
    if not subject or len(subject) > COMMIT_SUBJECT_MAX or common.one_line(subject, COMMIT_SUBJECT_MAX) != subject:
        raise FleetError("the commit subject must be one printable line of at most 100 characters")
    if len(body) > COMMIT_MESSAGE_MAX or "\x00" in body:
        raise FleetError("the commit message body is too long")
    word = gitops.fleet_words_in(subject + "\n" + body) if check_words else None
    if word:
        raise FleetError(f"the commit message contains a fleet word ({word})")
    return subject, body


def latest_result(conn, task: dict) -> Optional[str]:
    """The body of the newest result owl the desk posted for its own request."""
    if task["request_id"] is None:
        return None
    results = [owl for owl in owlery.request_owls(conn, task["request_id"])
               if owl["kind"] == "result" and owl["sender"] == task["desk"]]
    if not results:
        return None
    newest = max(results, key=lambda owl: (owl["created_at"], owl["id"]))
    row = owlery._owl(conn, newest["id"])  # a plain lookup, so McGonagall's copy stays unread
    if row is None or row["body"] is None:
        raise FleetError("the desk's handoff owl has no body, or it was purged")
    return row["body"]


# Writing files


def _replace(dir_fd: int, name: str, data: bytes) -> None:
    temp = f".{name}.{secrets.token_hex(4)}.tmp"
    safefs.write_new(dir_fd, temp, data)
    safefs.move(dir_fd, temp, dir_fd, name)


def _castle_task_file(holder_id: str, name: str, text: str) -> str:
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", holder_id) as fd:
        _replace(fd, name, text.encode("utf-8"))
    return f"{config.CASTLE_ROOT}/tasks/{holder_id}/{name}"


# The review itself


def _request_body(task: dict, sha: str, record: dict, holder_id: str, handoff: bool) -> str:
    worktree_path = record["path"]
    lines = [
        f"Review request for task {task['id']} at {sha}.",
        f"The author desk is {task['desk']}. You are the reviewer from the other model family.",
        f"Worktree: {worktree_path}",
        f"Diff: git -C {worktree_path} diff --no-ext-diff --no-textconv {record['base']}...HEAD",
        f"Evidence for this sha: {config.CASTLE_ROOT}/tasks/{holder_id}/evidence.md",
    ]
    if handoff:
        lines.append(f"Author's handoff, context only: {config.CASTLE_ROOT}/tasks/{holder_id}/handoff.md")
    lines += ["TASK.md is at the task_md path in this owl.",
              f"End with your review block. Its first line is exactly: REVIEW {task['id']} @ {sha}"]
    return "\n".join(lines) + "\n"


def _deliver(conn, owl_id: str, recipient: str, body: str) -> None:
    owl = next(item for item in owlery.inbox(conn, recipient, include_acked=True) if item["id"] == owl_id)
    copy = owl_post._inbox_copy(owl, body, None, owl_post.task_context(conn, owl["task_id"]))
    with safefs.opened_dir(config.CASTLE_ROOT, "desks", recipient, "inbox") as fd:
        if safefs.lstat(fd, f"{owl_id}.json") is None:
            safefs.write_new(fd, f"{owl_id}.json", copy)
    owlery.mark_delivered(conn, owl_id)


def _finish_reviewer_task(conn, request_id: str, reviewer_task_id: str) -> None:
    """Free the reviewer desk: its task closes as superseded by the recorded review."""
    if pensieve.get_task(conn, reviewer_task_id)["status"] != "closed":
        pensieve.close_task(conn, reviewer_task_id, "superseded")
    request = owlery.get_request(conn, request_id)
    if request["outcome"] is None and request["phase"] == "result_posted":
        owlery.advance(conn, request_id, "task_closed", detail="review recorded")
        owlery.advance(conn, request_id, "cleaned", detail="review recorded")


def _open_round(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int]) -> dict:
    """The review request as the task's next round. A refused round tells Ryan the task and the count."""
    try:
        return capacity.open_review_round(
            conn, task["id"], reviewer, sha, f"review {task['id']} @ {sha[:12]}", body=body,
            max_rounds=config.REVIEW_ROUND_CAP,
            idempotency_key=f"review:{task['id']}:{sha[:12]}:{secrets.token_hex(4)}", now=now)
    except capacity.RoundCapReached as exc:
        pensieve.add_event(conn, task["desk"], "review.round-cap", "headmaster",
                           f"task {task['id']} asked for review round {exc.round}, past the cap of"
                           f" {exc.max_rounds} rounds, so nothing went to {reviewer}. castle task allow-round"
                           f" {task['id']} allows one more round",
                           task_id=task["id"], dedupe_key=f"review:round-cap:{task['id']}:{exc.round}", now=now)
        raise FleetError(str(exc)) from None


def run_review(conn, task: dict, record: dict, sha: str, holder_id: str, handoff: bool,
               now: Optional[int] = None) -> dict:
    author = pensieve.get_desk(conn, task["desk"])
    reviewer = config.REVIEWER_FOR_FAMILY.get(author["family"])
    if reviewer is None:
        raise FleetError("this author's family has no reviewer")
    if not run_desk.is_enabled(reviewer):
        raise FleetError(f"{reviewer} is not enabled, so no review can run")
    evidence = verify.verify(conn, task["id"])
    if evidence["sha"] != sha:
        raise FleetError("HEAD moved before the review started; run the review again")
    pensieve.record_commit(conn, task["id"], record["repo"], sha)
    body = _request_body(task, sha, record, holder_id, handoff)
    opened = _open_round(conn, task, reviewer, sha, body, now)
    request_id, reviewer_task, owl_id = opened["request"]["id"], opened["task"], opened["owl"]["id"]
    _deliver(conn, owl_id, reviewer, body)
    pensieve.set_worktree(conn, reviewer_task["id"], task["worktree"])
    waits = (f"round {opened['round']} of {task['id']} @ {sha[:12]} waits as request {request_id}; run the review"
             f" again after the reset or a castle desk cap bump, and that review supersedes this one")
    if run_desk.over_daily_cap(conn, reviewer, now) is not None:
        run_desk.report_cap(conn, reviewer, now)
        raise run_desk.Capped(f"{reviewer} is at its fleet daily cap, so {waits}")
    started = []

    def start() -> None:
        # Under the reviewer's desk lock, once its caps allow the run: a refused run leaves the request waiting.
        pensieve.start_task(conn, reviewer_task["id"])
        owlery.advance(conn, request_id, "claimed", detail="review script")
        owlery.advance(conn, request_id, "running", detail="review script")
        started.append(True)

    try:
        try:
            result = run_desk.run(conn, reviewer, owl_id, on_start=start)
        except run_desk.Capped:
            raise run_desk.Capped(f"{reviewer} reached its fleet daily cap while it waited for its desk lock,"
                                  f" so {waits}") from None
        if result.get("cap_source") is not None:
            raise FleetError(f"the {reviewer} run stopped at the vendor's own usage limit (cap_source"
                             f" {result['cap_source']}); a fleet cap bump does not lift it")
        if result["exit_code"] != 0:
            raise FleetError(f"the {reviewer} run did not finish cleanly; its log is in the office runs folder")
        family = pensieve.get_desk(conn, reviewer)["family"]
        verdict, block = review_block(reviewer_output(reviewer, family, result["run_id"]), task["id"], sha)
        name = f"review-{sha}-{reviewer}-{result['run_id']}.md"
        with safefs.opened_dir(config.OFFICE_ROOT, "reviews", task["id"], create=True) as fd:
            safefs.write_new(fd, name, block.encode("utf-8"))
        owlery.record_review(conn, record["repo"], sha, task["id"], reviewer, verdict,
                             review_path=f"{ids.REVIEWS_ROOT}/{task['id']}/{name}")
        castle_review = _castle_task_file(holder_id, "review-latest.md", block)
        _castle_task_file(holder_id, f"review-{sha[:12]}-{reviewer}.md", block)
        posted = owlery.send(conn, reviewer, task["desk"], "result", f"review {verdict} {task['id']} @ {sha[:12]}",
                             body=block, task_id=reviewer_task["id"], request_id=request_id)
        owlery.mark_delivered(conn, posted["id"])
        owlery.read(conn, posted["id"], task["desk"])
        owlery.ack(conn, posted["id"], task["desk"])
        owlery.advance(conn, request_id, "result_posted", detail=verdict)
    finally:
        if started:
            _finish_reviewer_task(conn, request_id, reviewer_task["id"])
    if verdict == "PASS" and pensieve.get_task(conn, task["id"])["status"] == "active":
        pensieve.mark_awaiting_close(conn, task["id"])
    if verdict == "HEADMASTER":
        pensieve.add_event(conn, reviewer, "review.headmaster", "headmaster",
                           "a reviewer handed a decision to you; read review-latest.md in the task folder",
                           task_id=task["id"], dedupe_key=f"review:headmaster:{task['id']}:{sha}")
    return {"task_id": task["id"], "sha": sha, "repo": record["repo"], "reviewer": reviewer, "verdict": verdict,
            "round": opened["round"], "superseded": [item["request_id"] for item in opened["superseded"]],
            "review": castle_review, "evidence": evidence["evidence"]["castle"], "failed_checks": evidence["failed"]}


def review_build(conn, task_id: str) -> dict:
    """A build desk's task: commit its work from the handoff, then review HEAD."""
    task = pensieve.get_task(conn, ids.check("task", task_id))
    if task["desk"] not in config.WORKTREE_DESKS:
        raise FleetError("use 'fleet review own' for your own sessions; this is for a build desk's task")
    if task["status"] != "active":
        raise FleetError("the task must be active (a passed task is already awaiting close)")
    record = gitops.find_record(worktree.castle_path(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree with an office record")
    holder_id, _ = verify.task_md(conn, task["id"])
    handoff = latest_result(conn, task)
    if handoff is not None:
        _castle_task_file(holder_id, "handoff.md", handoff)
    if gitops.dirty(record):
        if handoff is None:
            raise FleetError("the worktree has changes but the desk posted no handoff with a commit message")
        subject, body = commit_message(handoff, check_words=record["repo"] not in config.FLEET_WORDS_ALLOWED_REPOS)
        gitops.git(["add", "-A", "--", ".", *gitops.link_excludes(record)], record["git_dir"], record["path"])
        message = ["-m", subject] + (["-m", body] if body else [])
        gitops.git(["commit", "--no-verify", *message], record["git_dir"], record["path"])
    sha = gitops.rev(record)
    if sha == gitops.rev(record, record["base"]):
        raise FleetError("there is nothing to review: HEAD is still the base")
    return run_review(conn, task, record, sha, holder_id, handoff is not None)


def _write_own_task_md(task_id: str, title: str, intent: str) -> str:
    """TASK.md for an own-session review. Lines shaped like acceptance criteria go under that heading."""
    lines = intent.strip().splitlines()
    criteria = [line.strip() for line in lines if verify.AC_LINE.fullmatch(line.strip())]
    words = "\n".join(line for line in lines if line.strip() not in criteria).strip() or title
    checks = "\n".join(criteria) if criteria else "None given. The reviewer judges the diff against the Intent."
    text = (f"# {task_id} {title}\n\n## Intent\n{words}\n\n## Acceptance criteria\n{checks}\n\n## Spec\n"
            "A commit from one of Ryan's own Claude sessions, reviewed by the other model family.\n")
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", task_id, create=True) as fd:
        safefs.write_new(fd, "TASK.md", text.encode("utf-8"))
    return f"{ids.TASKS_ROOT}/{task_id}/TASK.md"


def review_own(conn, repo_dir: str, title: Optional[str] = None, intent: Optional[str] = None,
               task_id: Optional[str] = None, base: str = config.DEFAULT_BASE, fetch: bool = True) -> dict:
    """A commit from one of Ryan's own Claude sessions, reviewed by Moody in a detached worktree."""
    reviewer = config.REVIEWER_FOR_FAMILY["claude"]
    if not run_desk.is_enabled(reviewer):
        raise FleetError(f"{reviewer} is not enabled, so no review can run")
    repo_dir = gitops.check_repo_dir(repo_dir)
    common_dir = f"{repo_dir}/.git"
    sha = gitops.git(["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], common_dir).strip()
    if gitops.SHA.fullmatch(sha) is None:
        raise FleetError("git did not return a full commit sha")
    if task_id is None:
        title = ids.clean_text(title or "", "title", 200, single_line=True)
        if not title:
            raise FleetError("a new review needs --title")
        new_id = ids.new_id("task")
        intent_path = _write_own_task_md(new_id, title, intent or title)
        task = pensieve.create_task(conn, OWN_DESK, title, intent_path=intent_path, task_id=new_id)
        try:
            pensieve.start_task(conn, task["id"])
        except ConflictError:
            pensieve.close_task(conn, task["id"], "abandoned")
            raise FleetError("your own sessions already have a task in review; pass --task <id> "
                             "for a fix round on it") from None
        record = worktree.add_worktree(conn, task["id"], repo_dir, base, None, fetch, detach_at=sha)
        task = pensieve.set_worktree(conn, task["id"], f"{ids.WORKTREES_ROOT}/{task['id']}")
    else:
        task = pensieve.get_task(conn, ids.check("task", task_id))
        if task["desk"] != OWN_DESK or task["status"] != "active":
            raise FleetError("--task must be an active task of your own sessions")
        record = gitops.find_record(worktree.castle_path(task["worktree"]))
        if record is None or record["repo_dir"] != repo_dir:
            raise FleetError("that task's worktree is for a different checkout")
        gitops.git(["checkout", "--detach", sha], record["git_dir"], record["path"])
    if gitops.rev(record) != sha:
        raise FleetError("the review worktree is not at your checkout's HEAD")
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", task["id"]) as fd:
        has_handoff = safefs.is_safe_regular(fd, "handoff.md")
    return run_review(conn, task, record, sha, task["id"], handoff=has_handoff)
