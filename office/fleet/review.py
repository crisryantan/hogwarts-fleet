"""The review script: the cross-model review for one commit, recorded from the reviewer's own output.

Ryan runs it from his terminal.

  fleet review <task-id>
      A build desk's task (Harry, Codex). Commits the desk's uncommitted work with the message from
      its handoff (outside the sandbox, through gitops, so no hook runs), then reviews HEAD.
  fleet review own --repo-dir <checkout> --title "<what this change does>" [--intent-file F] [--task <id>]
      A commit from one of Ryan's own Claude sessions. Makes a task for ryan-claude-1 with a TASK.md,
      and a detached worktree at that checkout's HEAD, so the reviewer reads exactly that commit.
      --task <id> reviews a fix round on the same task.

One review of an author task runs at a time. Before anything changes, the review takes that task's
review lock without waiting; if another review of the task holds it, this one is refused at once and
changes nothing. Under the lock the checkout, the evidence, the round, the reviewer's run and the
verdict all belong to the one sha this review asked for. The reviewer's own process inherits this lock
and the reviewer's desk lock, so a review killed mid-run (SIGKILL, a crash) still holds both until its
reviewer's process ends too. Then, for either:
1. verify runs the acceptance checks and writes evidence for this sha;
2. the commit is recorded on the author's task;
3. a review request goes from the author's task to the reviewer of the other family
   (Codex work to Hermione, Claude work to Moody), with an inbox copy for the reviewer. It is the
   task's next round: rounds past REVIEW_ROUND_CAP are refused until Ryan runs
   castle task allow-round <task-id>. Only a reviewer run that recorded a verdict uses up a round;
   one that crashed, timed out, was refused by a cap or hit a vendor limit does not, and the daily
   run caps bound those retries. A request of this task still queued for its reviewer is superseded,
   so only the newest commit is reviewed;
4. nothing waits in line. A busy reviewer leaves the request queued, and the review returns "queued".
   Busy means another run holds the reviewer's desk lock, since a desk runs one process at a time. A
   reviewer takes many tasks, so its other active tasks never make it busy; only a reviewer desk that
   takes one task at a time and has an active one is busy too. A reviewer at its daily cap leaves the
   request queued as well, and Ryan hears which cap and when it resets. Running the review again later
   supersedes the queued request of that task only. Otherwise run_desk runs the reviewer, which needs
   Ryan's enabled file for that desk;
5. the last REVIEW block in the reviewer's own output must name this task and this sha. Its verdict
   is recorded with the review file in the office, where no desk can change it;
6. on PASS the author's task moves to awaiting_close, which is what the push gate checks.
   CHANGES leaves it active for a fix round. HEADMASTER leaves it active and tells Ryan.

The verdict is recorded on its round in the same transaction that stores it, so the round counts even
if publishing the review afterwards fails. A review holds the reviewer's desk lock from before its round
opens until its reviewer task is closed as superseded, once its verdict is recorded or its run fails, and
the reviewer's process holds it too while it runs. So a reviewer task still active while that lock is free
was left by a review that died (killed, or its cleanup failed) and whose reviewer is gone: the next review
that takes the lock closes it, and a round with no verdict stops counting. Only review-round tasks are
closed this way, never the reviewer's other active tasks, and only the start() in run_review starts a
review-round task, so under the lock every active one was left by a dead review. A reviewer task is never
closed while its desk lock is held. A review that finds its reviewer busy does not count such a round of
its own task either, since it holds the task's review lock, but leaves closing it to a review that can take
the desk lock. Only Ryan closes a task as complete.

Author tasks run side by side: Harry, Hermione, Moody, Ron and Ryan's own sessions may each hold many active
tasks, so a task waiting for a fix round blocks nothing. Ryan's own sessions start a new review whatever else
of theirs is in review or waiting for fixes, with two refusals made before anything changes: a commit another
task already holds (review it with --task), and a checkout whose open task is at its round cap, so leaving out
--task never gets past the cap. A new task that fails before its first round opens is closed as abandoned.
"""
from __future__ import annotations

import contextlib
import re
import secrets
from typing import Iterator, Optional

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
REVIEW_RUNNING = "a review of this task is already running; run it again when it ends"


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
    """The reviewer's final text: the result event of Claude's stream-json output, or Codex's last message file."""
    run_id = safefs.check_component(run_id)
    with safefs.opened_dir(config.OFFICE_ROOT, "runs", desk) as fd:
        if family == "codex":
            raw = safefs.read_regular(fd, f"{run_id}-last-message.md", REVIEW_MAX_BYTES, "review output")
            return raw.decode("utf-8", "replace")
        # The tail, like run_desk: stream-json keeps every tool result, and the result event comes last.
        raw, _ = safefs.read_range(fd, f"{run_id}.out", None, run_desk.RUN_OUTPUT_MAX_BYTES, "review output")
    result = run_desk.claude_result(raw).get("result")
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


@contextlib.contextmanager
def task_review_lock(task_id: str) -> Iterator[int]:
    """One review of an author task at a time, taken without waiting before anything changes and held
    until the review ends, yielding its fd for the reviewer's process to inherit. A second review of the
    task is refused at once."""
    task_id = ids.check("task", task_id)
    with contextlib.ExitStack() as stack:
        locks_fd = stack.enter_context(safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True))
        try:
            lock_fd = stack.enter_context(safefs.held_lock(locks_fd, f"review-{task_id}.lock", blocking=False))
        except safefs.Busy:
            raise FleetError(REVIEW_RUNNING) from None
        yield lock_fd


def _recover_stranded(conn, reviewer: str, now: Optional[int]) -> None:
    """Close the reviewer tasks a review left active when it died (killed, or its cleanup failed), so the
    desk is free and a round with no verdict stops counting. Only called under the reviewer's desk lock,
    which every live review holds until its reviewer task is closed, and its reviewer's process while it runs."""
    for row in capacity.stranded_rounds(conn, reviewer):
        _finish_reviewer_task(conn, row["request_id"], row["reviewer_task_id"])
        counted = ("its recorded verdict still counts" if row["has_verdict"]
                   else "it recorded no verdict, so its round does not count")
        pensieve.add_event(conn, reviewer, "review.recovered", "routine",
                           f"an earlier review of task {row['task_id']} ended without closing {reviewer}'s task"
                           f" {row['reviewer_task_id']}, so the next review closed it; {counted}",
                           task_id=row["task_id"], dedupe_key=f"review:recovered:{row['request_id']}", now=now)


def _open_round(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int]) -> dict:
    """The review request as the task's next round. A refused round tells Ryan the task and the count."""
    try:
        return capacity.open_review_round(
            conn, task["id"], reviewer, sha, f"review {task['id']} @ {sha[:12]}", body=body,
            max_rounds=config.REVIEW_ROUND_CAP,
            idempotency_key=f"review:{task['id']}:{sha[:12]}:{secrets.token_hex(4)}", review_locked=True, now=now)
    except capacity.RoundCapReached as exc:
        pensieve.add_event(conn, task["desk"], "review.round-cap", "headmaster",
                           f"task {task['id']} asked for review round {exc.round}, past the cap of"
                           f" {exc.max_rounds} rounds, so nothing went to {reviewer}. castle task allow-round"
                           f" {task['id']} allows one more round",
                           task_id=task["id"], dedupe_key=f"review:round-cap:{task['id']}:{exc.round}", now=now)
        raise FleetError(str(exc)) from None


def _open_and_deliver(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int]) -> dict:
    opened = _open_round(conn, task, reviewer, sha, body, now)
    _deliver(conn, opened["owl"]["id"], reviewer, body)
    pensieve.set_worktree(conn, opened["task"]["id"], task["worktree"])
    return opened


def _queued(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int], result: dict) -> dict:
    """The reviewer is busy (its desk lock is held, or a single-task reviewer has an active task): the request
    is queued, no round is used, and nothing waits."""
    opened = _open_and_deliver(conn, task, reviewer, sha, body, now)
    return {**result, "verdict": None, "round": opened["round"], "request_id": opened["request"]["id"],
            "superseded": [item["request_id"] for item in opened["superseded"]], "review": None,
            "queued": f"queued: {reviewer} is busy; run {_again(task)} again later"}


def _again(task: dict) -> str:
    """The command that reviews this task again."""
    if task["desk"] == OWN_DESK:
        return f"fleet review own --repo-dir <checkout> --task {task['id']}"
    return f"fleet review {task['id']}"


def _review_and_record(conn, task: dict, record: dict, sha: str, holder_id: str, reviewer: str,
                       request_id: str, reviewer_task_id: str, owl_id: str, start, keep_fds: tuple) -> tuple:
    """Run the reviewer and record its verdict on the round, then publish the review. (verdict, castle path)"""
    result = run_desk.run(conn, reviewer, owl_id, on_start=start, lock_held=True, keep_fds=keep_fds)
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
    # From here the round counts, even if publishing the review below fails.
    capacity.record_round_verdict(conn, request_id, record["repo"], verdict,
                                  review_path=f"{ids.REVIEWS_ROOT}/{task['id']}/{name}")
    castle_review = _castle_task_file(holder_id, "review-latest.md", block)
    _castle_task_file(holder_id, f"review-{sha[:12]}-{reviewer}.md", block)
    posted = owlery.send(conn, reviewer, task["desk"], "result", f"review {verdict} {task['id']} @ {sha[:12]}",
                         body=block, task_id=reviewer_task_id, request_id=request_id)
    owlery.mark_delivered(conn, posted["id"])
    owlery.read(conn, posted["id"], task["desk"])
    owlery.ack(conn, posted["id"], task["desk"])
    owlery.advance(conn, request_id, "result_posted", detail=verdict)
    return verdict, castle_review


def run_review(conn, task: dict, record: dict, sha: str, holder_id: str, handoff: bool, task_lock_fd: int,
               now: Optional[int] = None) -> dict:
    """The review of one sha, called under the author task's review lock (task_lock_fd) once the worktree is
    at that sha."""
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
    result = {"task_id": task["id"], "sha": sha, "repo": record["repo"], "reviewer": reviewer, "queued": None,
              "evidence": evidence["evidence"]["castle"], "failed_checks": evidence["failed"]}
    with contextlib.ExitStack() as held:
        try:
            desk_lock_fd = held.enter_context(run_desk.desk_lock(reviewer, wait=False))
        except safefs.Busy:
            return _queued(conn, task, reviewer, sha, body, now, result)
        _recover_stranded(conn, reviewer, now)
        if pensieve.blocking_task(conn, reviewer) is not None:
            return _queued(conn, task, reviewer, sha, body, now, result)
        opened = _open_and_deliver(conn, task, reviewer, sha, body, now)
        request_id, reviewer_task_id, owl_id = opened["request"]["id"], opened["task"]["id"], opened["owl"]["id"]
        if run_desk.over_daily_cap(conn, reviewer, now) is not None:
            run_desk.report_cap(conn, reviewer, now)
            raise run_desk.Capped(f"{reviewer} is at its fleet daily cap, so round {opened['round']} of {task['id']}"
                                  f" @ {sha[:12]} waits as request {request_id}; run the review again after the"
                                  f" reset or a castle desk cap bump, and that review supersedes this one")
        started = []

        def start() -> None:
            # Called by run_desk once its caps allow the run: a refused run leaves the request queued.
            pensieve.start_task(conn, reviewer_task_id)
            started.append(True)  # before anything else can fail, so the task is closed below whatever happens
            owlery.advance(conn, request_id, "claimed", detail="review script")
            owlery.advance(conn, request_id, "running", detail="review script")

        try:
            verdict, castle_review = _review_and_record(conn, task, record, sha, holder_id, reviewer, request_id,
                                                        reviewer_task_id, owl_id, start, (task_lock_fd, desk_lock_fd))
        except BaseException:
            if started:
                # A cleanup that fails here must not hide why the review failed; the next review that takes
                # this reviewer's desk lock closes the task instead.
                with contextlib.suppress(Exception):
                    _finish_reviewer_task(conn, request_id, reviewer_task_id)
            raise
        _finish_reviewer_task(conn, request_id, reviewer_task_id)
    if verdict == "PASS" and pensieve.get_task(conn, task["id"])["status"] == "active":
        pensieve.mark_awaiting_close(conn, task["id"])
    if verdict == "HEADMASTER":
        pensieve.add_event(conn, reviewer, "review.headmaster", "headmaster",
                           "a reviewer handed a decision to you; read review-latest.md in the task folder",
                           task_id=task["id"], dedupe_key=f"review:headmaster:{task['id']}:{sha}")
    return {**result, "verdict": verdict, "round": opened["round"], "request_id": request_id,
            "superseded": [item["request_id"] for item in opened["superseded"]], "review": castle_review}


def review_build(conn, task_id: str) -> dict:
    """A build desk's task: commit its work from the handoff, then review HEAD."""
    with task_review_lock(task_id) as lock_fd:
        return _review_build(conn, ids.check("task", task_id), lock_fd)


def _review_build(conn, task_id: str, lock_fd: int) -> dict:
    task = pensieve.get_task(conn, task_id)
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
    return run_review(conn, task, record, sha, holder_id, handoff is not None, lock_fd)


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
    target = ids.new_id("task") if task_id is None else ids.check("task", task_id)
    with task_review_lock(target) as lock_fd:
        return _review_own(conn, repo_dir, title, intent, task_id, target, base, fetch, lock_fd)


def _review_own(conn, repo_dir: str, title: Optional[str], intent: Optional[str], task_id: Optional[str],
                target: str, base: str, fetch: bool, lock_fd: int) -> dict:
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
        _check_new_own(conn, repo_dir, common_dir, sha)
        intent_path = _write_own_task_md(target, title, intent or title)
        task = pensieve.create_task(conn, OWN_DESK, title, intent_path=intent_path, task_id=target)
        try:
            pensieve.start_task(conn, task["id"])
        except ConflictError as exc:
            # Only a store where ryan-claude-1 still takes one task at a time refuses here.
            pensieve.close_task(conn, task["id"], "abandoned")
            raise FleetError(f"{OWN_DESK} could not start a new task: {exc}; castle desk many-tasks {OWN_DESK}"
                             " lets it hold many") from None
        try:
            record = worktree.add_worktree(conn, task["id"], repo_dir, base, None, fetch, detach_at=sha)
            task = pensieve.set_worktree(conn, task["id"], f"{ids.WORKTREES_ROOT}/{task['id']}")
            return _review_own_at(conn, task, record, sha, lock_fd)
        except BaseException:
            # A new task that failed before its first round opened is closed, so it never sits active.
            with contextlib.suppress(Exception):
                if not capacity.review_rounds(conn, task["id"]):
                    pensieve.close_task(conn, task["id"], "abandoned")
            raise
    else:
        task = pensieve.get_task(conn, target)
        if task["desk"] != OWN_DESK or task["status"] != "active":
            raise FleetError("--task must be an active task of your own sessions")
        record = gitops.find_record(worktree.castle_path(task["worktree"]))
        if record is None or record["repo_dir"] != repo_dir:
            raise FleetError("that task's worktree is for a different checkout")
        gitops.git(["checkout", "--detach", sha], record["git_dir"], record["path"])
    return _review_own_at(conn, task, record, sha, lock_fd)


def _check_new_own(conn, repo_dir: str, common_dir: str, sha: str) -> None:
    """Refuse a new own task, before anything is made, while an open task on the same checkout is at its round
    cap, so leaving out --task never gets past the cap, or for a commit another task already holds."""
    for other in pensieve.list_tasks(conn, desk=OWN_DESK, status="active"):
        record = None if other["worktree"] is None else gitops.find_record(worktree.castle_path(other["worktree"]))
        if record is None or record["repo_dir"] != repo_dir:
            continue
        if capacity.needs_allowance(conn, other["id"], config.REVIEW_ROUND_CAP):
            raise FleetError(f"task {other['id']} on this checkout is at its review round cap; castle task"
                             f" allow-round {other['id']} allows one more round on it, or close it first")
    repo = gitops.repo_slug(gitops.git(["config", "--get", "remote.origin.url"], common_dir))
    held = pensieve.get_commit(conn, repo, sha)
    if held is not None:
        owner = pensieve.get_task(conn, held["task_id"])
        if owner["desk"] == OWN_DESK and owner["status"] == "active":
            raise FleetError(f"HEAD {sha[:12]} is already task {owner['id']}; run fleet review own --repo-dir"
                             f" <checkout> --task {owner['id']} to review it again")
        raise FleetError(f"HEAD {sha[:12]} already belongs to task {owner['id']}, which is"
                         f" {owner['status'].replace('_', ' ')}; make a new commit for a new review")


def _review_own_at(conn, task: dict, record: dict, sha: str, lock_fd: int) -> dict:
    if gitops.rev(record) != sha:
        raise FleetError("the review worktree is not at your checkout's HEAD")
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", task["id"]) as fd:
        has_handoff = safefs.is_safe_regular(fd, "handoff.md")
    return run_review(conn, task, record, sha, task["id"], has_handoff, lock_fd)
