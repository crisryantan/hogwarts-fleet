"""The review script: the cross-model review for one commit, recorded from the reviewer's own output.

The Owl Post starts it when a build desk posts its handoff (the review loop, below), and Ryan runs it from his
terminal as the fallback.

  fleet review <task-id>
      A build desk's task (Harry, Codex). Commits the desk's uncommitted work with the message from
      its handoff (outside the sandbox, through gitops, so no hook runs), then reviews HEAD.
  fleet review own --repo-dir <checkout> --title "<what this change does>" [--intent-file F] [--task <id>]
      A commit from one of Ryan's own Claude sessions. Makes a task for ryan-claude-1 with a TASK.md,
      and a detached worktree at that checkout's HEAD, so the reviewer reads exactly that commit.
      --task <id> reviews a fix round on the same task.

One review of an author task runs at a time. Before anything changes, the review takes that task's
review lock without waiting; if another review of the task holds it, or a run of the build desk on it
(which holds it until its process ends), this one is refused at once and changes nothing. Under the lock
the checkout, the evidence, the round, the reviewer's run and the verdict all belong to the one sha this
review asked for. The reviewer's own process inherits this lock
and the reviewer desk's run slot this review holds, so a review killed mid-run (SIGKILL, a crash) still
holds both until its reviewer's process ends too. Then, for either:
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
   Busy means other runs hold every one of the reviewer desk's run slots (config.RUN_SLOTS): Moody and
   Hermione have two, so reviews of two different tasks run at once and a third is queued, while a desk
   with one slot runs one process at a time as before. A reviewer takes many tasks, so its other active
   tasks never make it busy; only a reviewer desk that takes one task at a time and has an active one is
   busy too. A reviewer at its daily cap leaves the request queued as well, and Ryan hears which cap and
   when it resets. Running the review again later supersedes the queued request of that task only.
   Otherwise run_desk runs the reviewer in the slot this review holds, which needs Ryan's enabled file
   for that desk;
5. the last REVIEW block in the reviewer's own output must name this task and this sha. Its verdict
   is recorded with the review file in the office, where no desk can change it;
6. on PASS the author's task moves to awaiting_close, which is what the push gate checks.
   CHANGES leaves it active for a fix round. HEADMASTER leaves it active and tells Ryan.

The review loop. When Harry posts the handoff for his own active task, the Owl Post records it and starts
auto_review for that task in a process of its own (main). Under the task's loop lock, so one runs per task, it
takes the newest handoff the Owl Post recorded that still passes owl_post.handoff_problem, newest by the order the
store keeps the request's owls, never by their random ids. A newer one that no longer passes is finished saying why,
so an owl that is no handoff never stands in for one that is. It waits up to AUTO_REVIEW_AUTHOR_WAIT_SECONDS for the
run that posted it to end, takes the task review lock, chooses and checks the handoff again under it, and only then
finishes the older ones as superseded and runs the same review as fleet review <task-id>, bound to that one owl
(_review_build handoff_owl), so an owl that came in during the wait never supplies its commit message. A manual
review, a manual fleet build and the loop never open two rounds at once, and a run of Harry on the task holds the
same lock until it ends (run_desk.task_lock), so neither review entry point runs beside it; a manual review also
refuses while a launch of his on the task has recorded no usage yet. What it cannot do yet (the author's run
still going, the reviewer's run slots all busy, another review of the task running) it leaves to the Owl Post's
next pass, for at most AUTO_REVIEW_WAIT_LIMIT_SECONDS. Each try it starts work on is counted, so a review killed
part way is started again at most AUTO_REVIEW_MAX_TRIES times in all. Any other ending, a verdict, a refusal or an
error, finishes the handoff for good, and every ending that needs Ryan raises one headmaster event. After a verdict:
- CHANGES starts Harry's fix round through the same path as fleet build, unless the task has used its
  REVIEW_ROUND_CAP rounds with no allowance left: then nothing starts, and Ryan hears the task and its verdict.
  Harry's next handoff starts the next review the same way.
- PASS starts no build or review. The task awaits close, as it does after any PASS. While Ryan has opted in
  (push.auto_draft_pr_on, one file in the office), the reviewed commit is pushed and a draft PR opened from the
  handoff's COMMIT MESSAGE and PR BODY DRAFT through push.push_draft_pr, and Ryan hears its URL, or why it stopped.
  It is never retried, never ready for review and never merged. Without the opt-in, Ryan hears it is ready for push.
- HEADMASTER starts nothing more, and Ryan hears it as he does from any review.
A handoff is finished before anything after its verdict starts, so a killed review never reviews a fix round
that is still being written. What follows the verdict of each round the loop opens is kept in the office reviews
folder (owl_post.write_after, after-<request>.json): "review" from before its reviewer starts, "acting" with the
step (fix-round, push or pr) before anything that reaches outside the office begins, and "done" once it has ended
and Ryan heard what he must. The Owl Post starts the loop again for a task with a record that is not done, and the
loop finishes it first, under the task review lock and without opening a round (_auto_recover): a round that
recorded no verdict needs nothing; one with a verdict has its handoff finished and what every review does after a
verdict done again (settle_verdict), and a step that had not begun starts then, once. A step that had begun may or
may not have happened, so it is never started again by itself: Ryan hears so once, unless its own ending was told.
A review Ryan runs with fleet review stops at its verdict, as it always has.

A build desk's review is refused before anything changes when it would read exactly what the task's last verdict
judged: HEAD is that round's commit and the desk's latest handoff is the same text. Each round that runs records,
in the office reviews folder, the commit and the sha256 of the handoff it was opened for. A round with no readable
record (one from before records were kept) never refuses a review, so the guard can only stop a repeat, never a
new commit or a new handoff.

The verdict is recorded on its round in the same transaction that stores it, so the round counts even
if publishing the review afterwards fails. A review holds one run slot of the reviewer desk from before its
round opens until its reviewer task is closed as superseded, once its verdict is recorded or its run fails,
and the reviewer's process holds that slot too while it runs. The round records its slot in the row that
opens it, before its reviewer task can start. So a reviewer task still active while its round's own slot is
free was left by a review that died (killed, or its cleanup failed) and whose reviewer is gone: the next
review that holds that slot closes it, and a round with no verdict stops counting. That is the slot the
review took for itself, or another it takes without waiting just while it closes the task, so for that moment
the desk can look busy to a third review. A slot it cannot take belongs to a live review, or to the reviewer
a killed review left running, in another slot, and that round's task is left alone. A round that records no
slot (opened while every slot was busy, or before slots were recorded) counts as slot 0, the desk lock of
old. Only review-round tasks are closed this way, never the reviewer's other active tasks, and only the
start() in run_review starts a review-round task, so under its slot every active one was left by a dead
review. A reviewer task is never closed while its round's slot is held by anyone but the review that
closes it. A review that finds its reviewer busy does not count such a round of its own task either, since
it holds the task's review lock, but leaves closing it to a review that can take that round's slot. Only
Ryan closes a task as complete.

Author tasks run side by side: Harry, Hermione, Moody, Ron and Ryan's own sessions may each hold many active tasks,
so a task waiting for a fix round blocks nothing. A review of Ryan's own sessions follows the branch his checkout
has out: a new task records it, and a detached HEAD is refused. The name is Ryan's own, so any name git itself
takes as a branch (git check-ref-format --branch) counts, capitals, @ and dots included, as long as it is 1 to 255
bytes of printable ASCII with no whitespace. The fleet's lowercase, fleet-word-free rule is only for the branches
the fleet makes and pushes; a lineage name is only compared. A review's lineage is its branch and its commits. A
new review without --task is refused before anything changes for a commit another task already holds (review it
with --task), while an active own task on the same checkout follows the same branch, and while HEAD builds on (is
or descends from) a commit recorded on any active own task of the same repository (same origin), from any checkout
of it. A fix commit then goes on its open task with --task, and its rounds and its cap carry on, allowance or not,
whatever branch it was made on: the same branch, a branch made off a capped one with the old one kept, a renamed
branch, or a second clone. Only work that builds on no open task's commits starts a new task with its own count,
which is how one checkout carries several PRs in flight; a branch stacked on an open task's commits goes on that
task or waits until it passes, since a task awaiting close blocks nothing. An active task whose branch the
checkout no longer has (renamed or deleted), or that names none, also refuses a new task, since its work may be
this same work under a new name. --task <id> moves the task to the branch now out when HEAD builds on its commits
or its own branch is gone; it never moves a task onto a branch another task follows, and never takes on work
built on another open task's commits. A shallow checkout, whose history may stop short of a recorded commit, is
refused rather than guessed about, and told to fetch its full history; in a full clone a recorded commit it lacks
is no ancestor, since git keeps every commit its branches reach. A full clone whose own history is missing or
damaged also cannot tell, and is told to fetch the missing commits or clone again, since git refuses --unshallow
on it. A checkout is matched as a folder (device and inode), not by how its path is
spelled. These choices are made under one short lock, so two reviews started at once on one branch never both
make a task. Rewritten commits (a rebase, squash or cherry-pick onto a new branch name) are new commits, so they
start a fresh task, and the handbook asks Ryan not to route around his cap that way. A new task that fails before
its first round opens is closed as abandoned, unless its commit was already recorded on it: then it stays active,
--task <id> retries it, and a new review of that commit names it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import time
from typing import Iterator, Optional

from hogwarts import capacity, ids, owlery, pensieve
from hogwarts.errors import ConflictError, StoreError

from fleet import common, config, gitops, owl_post, push, run_desk, safefs, verify, worktree
from fleet.safefs import FleetError

REVIEW_HEADER = re.compile(r"REVIEW (tk_[0-9a-f]{16}) @ ([0-9a-f]{40})")
VERDICT_LINE = re.compile(r"VERDICT: (PASS|CHANGES|HEADMASTER)")
SECTION_HEADER = re.compile(r"[A-Z][A-Z -]{2,40}(?: \(.*\))?")
COMMIT_SUBJECT_MAX = 100
COMMIT_MESSAGE_MAX = 4000
REVIEW_MAX_BYTES = 262144
OWN_DESK = config.OWN_SESSION_DESK
REVIEW_RUNNING = "a review of this task, or a run of its build desk on it, is going; run this again when it ends"
OWN_LINEAGE_LOCK = "review-own-lineage.lock"
OWN_LINEAGE_WAIT_SECONDS = 120
ROUND_RECORD_MAX_BYTES = 1024
AUTHOR_POLL_SECONDS = 2
SHA256 = re.compile(r"[0-9a-f]{64}")


class Unchanged(FleetError):
    """A build desk's review refused because HEAD and the desk's latest handoff are what the last verdict judged."""


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


def pr_body(handoff: str) -> str:
    """The PR BODY DRAFT section of a build desk's handoff, as the body of its draft PR."""
    lines = handoff.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "PR BODY DRAFT") + 1
    except StopIteration:
        raise FleetError("the handoff has no PR BODY DRAFT section") from None
    section = []
    for line in lines[start:]:
        if SECTION_HEADER.fullmatch(line.strip()):
            break
        section.append(line.rstrip())
    body = "\n".join(section).strip()
    if not body:
        raise FleetError("the handoff's PR BODY DRAFT section is empty")
    return body + "\n"


def desk_results(conn, task: dict) -> list:
    """The result owls the desk posted for its own request, oldest first, in the order the store keeps them
    (owlery.request_owls: by time, then as stored), so two in the same second keep the order they came in. Owl ids
    are random, so they never order anything."""
    if task["request_id"] is None:
        return []
    return [owl for owl in owlery.request_owls(conn, task["request_id"])
            if owl["kind"] == "result" and owl["sender"] == task["desk"]]


def latest_result_owl(conn, task: dict) -> Optional[dict]:
    """The newest result owl the desk posted for its own request, or None."""
    results = desk_results(conn, task)
    return results[-1] if results else None


def latest_result(conn, task: dict) -> Optional[str]:
    """The body of the newest result owl the desk posted for its own request."""
    newest = latest_result_owl(conn, task)
    return None if newest is None else owl_body(conn, newest["id"])


def owl_body(conn, owl_id: str) -> str:
    """A handoff owl's text."""
    row = owlery._owl(conn, owl_id)  # a plain lookup, so McGonagall's copy stays unread
    if row is None or row["body"] is None:
        raise FleetError("the desk's handoff owl has no body, or it was purged")
    return row["body"]


def handoff_digest(handoff: Optional[str]) -> Optional[str]:
    """The sha256 of a handoff's text, or None for no handoff."""
    return None if handoff is None else hashlib.sha256(handoff.encode("utf-8")).hexdigest()


# What each round was opened to review


def _round_record_name(request_id: str) -> str:
    return f"round-{ids.check('request', request_id)}.json"


def record_round_inputs(task_id: str, request_id: str, sha: str, handoff_sha256: Optional[str]) -> None:
    """Keep, in the office where no desk can write, the commit and the handoff a round was opened for. Written
    whole through a temp file and a rename, so a reader sees the old file or the new one, never part of one."""
    data = {"request_id": request_id, "sha": ids.check("sha", sha), "handoff_sha256": handoff_sha256}
    raw = (json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    with safefs.opened_dir(config.OFFICE_ROOT, "reviews", ids.check("task", task_id), create=True) as fd:
        _replace(fd, _round_record_name(request_id), raw)


def round_inputs(task_id: str, request_id: str) -> Optional[dict]:
    """What a round was opened to review, {sha, handoff_sha256}, or None when its record is missing or cannot be
    read whole. None only ever lets a review through, as it did before records were kept."""
    try:
        with safefs.opened_dir(config.OFFICE_ROOT, "reviews", ids.check("task", task_id)) as fd:
            raw = safefs.read_regular(fd, _round_record_name(request_id), ROUND_RECORD_MAX_BYTES, "round record")
        data = common.strict_json(raw)
    except (FleetError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"request_id", "sha", "handoff_sha256"} \
            or data["request_id"] != request_id or not isinstance(data["sha"], str) \
            or gitops.SHA.fullmatch(data["sha"]) is None:
        return None
    digest = data["handoff_sha256"]
    if digest is not None and (not isinstance(digest, str) or SHA256.fullmatch(digest) is None):
        return None
    return {"sha": data["sha"], "handoff_sha256": digest}


def refuse_unchanged(conn, task: dict, sha: str, handoff: Optional[str]) -> None:
    """Refuse a review whose reviewer would read exactly what the task's last verdict judged: HEAD is that round's
    commit and the desk's latest handoff is the same text. A new commit or a new handoff always gets through, and
    so does a last round whose record is missing. Before it refuses, it finishes what every review does after its
    verdict (settle_verdict), in case that round's review was killed before it did."""
    judged = [row for row in capacity.review_rounds(conn, task["id"]) if row["has_verdict"]]
    if not judged or judged[-1]["sha"] != sha:
        return
    last = judged[-1]
    inputs = round_inputs(task["id"], last["request_id"])
    if inputs is None or inputs["sha"] != sha or inputs["handoff_sha256"] != handoff_digest(handoff):
        return
    # That round's review may have been killed right after its verdict was recorded: finish what it left.
    settle_verdict(conn, task, last["reviewer"], last["verdict"], last["sha"])
    raise Unchanged(f"nothing new to review: HEAD {sha[:12]} and {task['desk']}'s latest handoff are what round"
                    f" {last['round']} already judged ({last['verdict']}), so no round was opened; a new commit or a"
                    f" new handoff from {task['desk']} opens the next one")


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
    task is refused at once, and so is a review while a run of its build desk on it is going, since that run
    holds the same lock until its process ends (run_desk.task_lock)."""
    task_id = ids.check("task", task_id)
    with contextlib.ExitStack() as stack:
        try:
            lock_fd = stack.enter_context(run_desk.task_lock(task_id))
        except safefs.Busy:
            raise FleetError(REVIEW_RUNNING) from None
        yield lock_fd


def _recover_stranded(conn, reviewer: str, slot: run_desk.Slot, now: Optional[int]) -> None:
    """Close the reviewer tasks a review left active when it died (killed, or its cleanup failed), so a round
    with no verdict stops counting. Called under slot, a run slot of the reviewer desk. A round's task is closed
    only while its own slot is held here: slot itself, or the round's slot taken without waiting for just this
    close. Every live review holds its round's slot until its reviewer task is closed, and its reviewer's process
    holds it while it runs, so a slot that cannot be taken means that round may be live, and it is left alone.
    A round with no slot recorded counts as slot 0, whose lock is the desk lock of old."""
    for row in capacity.stranded_rounds(conn, reviewer):
        index = 0 if row["slot"] is None else row["slot"]
        with contextlib.ExitStack() as held:
            if index != slot.index:
                try:
                    held.enter_context(run_desk.slot_lock(reviewer, index))
                except safefs.Busy:
                    continue
            # Read again under the round's slot: its own review may have closed the task since the list was read.
            if pensieve.get_task(conn, row["reviewer_task_id"])["status"] != "active":
                continue
            _finish_reviewer_task(conn, row["request_id"], row["reviewer_task_id"])
            counted = ("its recorded verdict still counts" if row["has_verdict"]
                       else "it recorded no verdict, so its round does not count")
            pensieve.add_event(conn, reviewer, "review.recovered", "routine",
                               f"an earlier review of task {row['task_id']} ended without closing {reviewer}'s task"
                               f" {row['reviewer_task_id']}, so the next review closed it; {counted}",
                               task_id=row["task_id"], dedupe_key=f"review:recovered:{row['request_id']}", now=now)


def _open_round(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int],
                slot: Optional[int] = None) -> dict:
    """The review request as the task's next round, recording the reviewer's run slot this review holds, None
    for a round queued while every slot is busy. A refused round tells Ryan the task and the count."""
    try:
        return capacity.open_review_round(
            conn, task["id"], reviewer, sha, f"review {task['id']} @ {sha[:12]}", body=body,
            max_rounds=config.REVIEW_ROUND_CAP,
            idempotency_key=f"review:{task['id']}:{sha[:12]}:{secrets.token_hex(4)}", review_locked=True,
            slot=slot, now=now)
    except capacity.RoundCapReached as exc:
        pensieve.add_event(conn, task["desk"], "review.round-cap", "headmaster",
                           f"task {task['id']} asked for review round {exc.round}, past the cap of"
                           f" {exc.max_rounds} rounds, so nothing went to {reviewer}. castle task allow-round"
                           f" {task['id']} allows one more round",
                           task_id=task["id"], dedupe_key=f"review:round-cap:{task['id']}:{exc.round}", now=now)
        raise FleetError(str(exc)) from None


def _open_and_deliver(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int],
                      slot: Optional[int] = None) -> dict:
    opened = _open_round(conn, task, reviewer, sha, body, now, slot)
    _deliver(conn, opened["owl"]["id"], reviewer, body)
    pensieve.set_worktree(conn, opened["task"]["id"], task["worktree"])
    return opened


def _queued(conn, task: dict, reviewer: str, sha: str, body: str, now: Optional[int], result: dict) -> dict:
    """The reviewer is busy (every one of its run slots is held, or a single-task reviewer has an active task):
    the request is queued, holding no slot, no round is used, and nothing waits."""
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
                       request_id: str, reviewer_task_id: str, owl_id: str, start, slot: run_desk.Slot,
                       keep_fds: tuple) -> tuple:
    """Run the reviewer in the run slot this review holds and record its verdict on the round, then publish the
    review. (verdict, castle path)"""
    result = run_desk.run(conn, reviewer, owl_id, on_start=start, lock_held=slot, keep_fds=keep_fds)
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
               now: Optional[int] = None, inputs: Optional[dict] = None) -> dict:
    """The review of one sha, called under the author task's review lock (task_lock_fd) once the worktree is
    at that sha. inputs, {handoff_sha256}, is recorded with the round that runs, before its reviewer starts."""
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
              "evidence": evidence["evidence"]["castle"], "failed_checks": evidence["failed"],
              "malformed_checks": evidence["malformed"]}
    with contextlib.ExitStack() as held:
        try:
            slot = held.enter_context(run_desk.desk_lock(reviewer, wait=False))
        except safefs.Busy:
            return _queued(conn, task, reviewer, sha, body, now, result)
        _recover_stranded(conn, reviewer, slot, now)
        if pensieve.blocking_task(conn, reviewer) is not None:
            return _queued(conn, task, reviewer, sha, body, now, result)
        opened = _open_and_deliver(conn, task, reviewer, sha, body, now, slot.index)
        request_id, reviewer_task_id, owl_id = opened["request"]["id"], opened["task"]["id"], opened["owl"]["id"]
        if inputs is not None:
            # Before the reviewer starts: a round whose record cannot be written never runs, and stays waiting.
            record_round_inputs(task["id"], request_id, sha, inputs["handoff_sha256"])
            if inputs.get("handoff_owl") is not None:
                # The review loop's own round: what follows its verdict is tracked from here (see _after_verdict).
                owl_post.write_after(task["id"], request_id, inputs["handoff_owl"], "review")
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
                                                        reviewer_task_id, owl_id, start, slot,
                                                        (task_lock_fd, slot.fd))
        except BaseException:
            if started:
                # A cleanup that fails here must not hide why the review failed; the next review that holds
                # this round's run slot closes the task instead.
                with contextlib.suppress(Exception):
                    _finish_reviewer_task(conn, request_id, reviewer_task_id)
            raise
        _finish_reviewer_task(conn, request_id, reviewer_task_id)
    settle_verdict(conn, task, reviewer, verdict, sha)
    return {**result, "verdict": verdict, "round": opened["round"], "request_id": request_id,
            "superseded": [item["request_id"] for item in opened["superseded"]], "review": castle_review}


def settle_verdict(conn, task: dict, reviewer: str, verdict: str, sha: str) -> None:
    """What every review does once its verdict is recorded: on PASS the author's task moves to awaiting_close, and
    a HEADMASTER verdict tells Ryan. Each is done at most once, so it runs again safely where a review killed after
    its verdict was recorded may not have done it (refuse_unchanged, the review loop's recovery)."""
    if verdict == "PASS" and pensieve.get_task(conn, task["id"])["status"] == "active":
        pensieve.mark_awaiting_close(conn, task["id"])
    if verdict == "HEADMASTER":
        pensieve.add_event(conn, reviewer, "review.headmaster", "headmaster",
                           "a reviewer handed a decision to you; read review-latest.md in the task folder",
                           task_id=task["id"], dedupe_key=f"review:headmaster:{task['id']}:{sha}")


def review_build(conn, task_id: str) -> dict:
    """A build desk's task: commit its work from the handoff, then review HEAD."""
    with task_review_lock(task_id) as lock_fd:
        return _review_build(conn, ids.check("task", task_id), lock_fd)


def _review_build(conn, task_id: str, lock_fd: int, handoff_owl: Optional[str] = None) -> dict:
    """The review of a build desk's task under its review lock (lock_fd). Its handoff is the desk's newest result
    owl, or for the review loop the handoff owl it chose and checked under this same lock (handoff_owl), so no owl
    that came in meanwhile can stand in for it."""
    task = pensieve.get_task(conn, task_id)
    if task["desk"] not in config.WORKTREE_DESKS:
        raise FleetError("use 'fleet review own' for your own sessions; this is for a build desk's task")
    if task["status"] != "active":
        raise FleetError("the task must be active (a passed task is already awaiting close)")
    if _author_running(conn, task, None):
        # Its run holds this lock until its process ends; a launch with no usage yet is checked too, for a run whose
        # process outlived the lock (one started before runs held it).
        raise FleetError(f"{task['desk']}'s run on this task may still be going, so nothing was reviewed; review it"
                         " once that run has ended")
    record = gitops.find_record(worktree.castle_path(task["worktree"]))
    if record is None:
        raise FleetError("this task has no worktree with an office record")
    holder_id, _ = verify.task_md(conn, task["id"])
    newest_id = handoff_owl
    if newest_id is None:
        newest = latest_result_owl(conn, task)
        newest_id = None if newest is None else newest["id"]
    handoff = None if newest_id is None else owl_body(conn, newest_id)
    dirty = gitops.dirty(record)
    if not dirty:
        # Uncommitted work always makes a new commit. A clean worktree is refused here, before anything changes,
        # when it and the handoff are what the last verdict judged.
        refuse_unchanged(conn, task, gitops.rev(record), handoff)
    if handoff is not None:
        _castle_task_file(holder_id, "handoff.md", handoff)
    if dirty:
        if handoff is None:
            raise FleetError("the worktree has changes but the desk posted no handoff with a commit message")
        subject, body = commit_message(handoff, check_words=record["repo"] not in config.FLEET_WORDS_ALLOWED_REPOS)
        gitops.git(["add", "-A", "--", ".", *gitops.link_excludes(record)], record["git_dir"], record["path"])
        message = ["-m", subject] + (["-m", body] if body else [])
        gitops.git(["commit", "--no-verify", *message], record["git_dir"], record["path"])
    sha = gitops.rev(record)
    if sha == gitops.rev(record, record["base"]):
        raise FleetError("there is nothing to review: HEAD is still the base")
    result = run_review(conn, task, record, sha, holder_id, handoff is not None, lock_fd,
                        inputs={"handoff_sha256": handoff_digest(handoff), "handoff_owl": handoff_owl})
    return {**result, "handoff_owl": newest_id}


# The review loop


def auto_review(conn, task_id: str, now: Optional[int] = None) -> dict:
    """The review the Owl Post starts when a build desk posts a handoff (see the module notes). Never raises for
    a refused or failed review: each ending is in the result, and what needs Ryan is an event. What followed an
    earlier verdict of the loop and was cut off by a kill is finished first (_auto_recover)."""
    task_id = ids.check("task", task_id)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(owl_post.auto_review_lock(task_id, wait=config.AUTO_REVIEW_LOCK_WAIT_SECONDS))
        except safefs.Busy:
            return {"task_id": task_id, "outcome": "another automatic review of this task is running"}
        afters = owl_post.unfinished_afters(task_id)
        if not afters:
            return _auto_review(conn, task_id, now)
        recovered = _auto_recover(conn, pensieve.get_task(conn, task_id), afters, now)
        if recovered is None:
            return {"task_id": task_id, "outcome": "waiting: another review of this task, or a run of its build desk"
                                                   " on it, is going; the Owl Post tries again on its next pass"}
        return {**_auto_review(conn, task_id, now), "recovered": recovered}


def _auto_review(conn, task_id: str, now: Optional[int]) -> dict:
    task = pensieve.get_task(conn, task_id)
    newest = _pending_handoff(conn, task, now)
    if newest is None:
        return {"task_id": task_id, "outcome": "no handoff of this task waits for its review"}
    if not _author_run_over(conn, task, now, wait=True):
        return _auto_wait(conn, task, newest, f"{task['desk']}'s run on it is still going", now)
    reviewer = config.REVIEWER_FOR_FAMILY[pensieve.get_desk(conn, task["desk"])["family"]]
    if _reviewer_busy(conn, reviewer, now):
        return _auto_wait(conn, task, newest, f"{reviewer} is busy with other reviews", now)
    with contextlib.ExitStack() as held:
        try:
            lock_fd = held.enter_context(task_review_lock(task_id))
        except FleetError:
            return _auto_wait(conn, task, newest, "another review of this task is running", now)
        # Again under the lock, which a manual fleet build also takes: no run of the author may have begun since.
        if not _author_run_over(conn, task, now, wait=False):
            return _auto_wait(conn, task, newest, f"{task['desk']}'s run on it is still going", now)
        # Chosen and checked again under the lock, before anything is superseded: a handoff that came in during the
        # wait is the newest now, and the review reads only the one chosen here.
        newest = _pending_handoff(conn, task, now)
        if newest is None:
            return {"task_id": task_id, "outcome": "no handoff of this task waits for its review"}
        newest_id = newest["id"]
        for owl_id in owl_post.unfinished_handoffs(task_id):
            if owl_id != newest_id:
                owl_post.finish_handoff(task_id, owl_id, "superseded by a newer handoff from the same desk")
        tried = owl_post.take_try(task_id, newest_id)
        if tried is None:
            return _auto_finish(conn, task, newest_id, f"the automatic review stopped: it started"
                                f" {config.AUTO_REVIEW_MAX_TRIES} times and each try ended without finishing (killed,"
                                f" or the Mac stopped); run fleet review {task_id}", "headmaster", now)
        try:
            result = _review_build(conn, task_id, lock_fd, handoff_owl=newest_id)
        except Unchanged as exc:
            return _auto_finish(conn, task, newest_id, str(exc), "routine", now)
        except (FleetError, StoreError) as exc:
            return _auto_finish(conn, task, newest_id, f"the automatic review stopped: {common.one_line(exc, 300)};"
                                f" once that is sorted, fleet review {task_id} runs it", "headmaster", now)
        except Exception as exc:  # noqa: BLE001 - an unexpected error still finishes the handoff and tells Ryan
            return _auto_finish(conn, task, newest_id, f"the automatic review stopped on an unexpected"
                                f" {type(exc).__name__}; fleet review {task_id} runs it", "headmaster", now)
        if result["queued"] is not None:
            owl_post.give_back_try(task_id, newest_id, tried)
            return {**_auto_wait(conn, task, newest, f"{reviewer} became busy", now), "review": result}
        # Finished before anything starts after the verdict, so no later try reviews a fix round mid-write. What
        # follows is tracked in the round's after record, which run_review wrote before the reviewer started.
        owl_post.finish_handoff(task_id, newest_id, f"round {result['round']} at {result['sha'][:12]} recorded"
                                f" {result['verdict']}")
        after = _after_verdict(conn, task, result, lock_fd, now, checked_owl=newest_id)
    return {"task_id": task_id, "owl_id": newest_id, "outcome": f"reviewed: {result['verdict']}", "next": after,
            "review": result}


def _pending_handoff(conn, task: dict, now: Optional[int]) -> Optional[dict]:
    """The handoff the review loop reviews next: the newest one the Owl Post gave it that it has not finished with
    and that still passes owl_post.handoff_problem, newest by the order the store keeps the request's owls
    (desk_results). One newer than it that no longer passes is finished, saying why, so a result that is no handoff,
    or a handoff that fails its checks, never stands in for one that passes or gets it superseded. Nothing older
    than the one chosen is touched here."""
    unfinished = owl_post.unfinished_handoffs(task["id"])
    results = desk_results(conn, task)
    for owl in reversed([owl for owl in results if owl["id"] in unfinished]):
        problem = owl_post.handoff_problem(conn, owl)
        if problem is None:
            return owl
        _auto_finish(conn, task, owl["id"], f"no review: {problem}", "routine", now)
    known = {owl["id"] for owl in results}
    for owl_id in unfinished:
        if owl_id not in known:
            _auto_finish(conn, task, owl_id, "no review: the handoff is not a result of this task's request",
                         "routine", now)
    return None


def _auto_recover(conn, task: dict, afters: list, now: Optional[int]) -> Optional[list]:
    """Finish what followed a verdict of the loop's own rounds that a kill cut off (afters, from
    owl_post.unfinished_afters), under the task's review lock, or None while another review or a build run holds it.
    It never opens a round. A step that had not begun starts now, once; one that had begun may or may not have
    happened, so it never runs again by itself and Ryan hears so, once (_interrupted)."""
    with contextlib.ExitStack() as held:
        try:
            lock_fd = held.enter_context(task_review_lock(task["id"]))
        except FleetError:
            return None
        rows = capacity.review_rounds(conn, task["id"])
        rounds = {row["request_id"]: row for row in rows}
        order = {row["request_id"]: index for index, row in enumerate(rows)}
        # In the order the rounds were opened, never by their random ids.
        afters = sorted(afters, key=lambda record: order.get(record["request_id"], len(order)))
        return [{"request_id": record["request_id"],
                 "outcome": _recover_after(conn, task, record, rounds.get(record["request_id"]), lock_fd, now)}
                for record in afters]


def _recover_after(conn, task: dict, record: dict, row: Optional[dict], lock_fd: int, now: Optional[int]) -> str:
    """One round's unfinished after record (see _auto_recover). row is the round, from capacity.review_rounds."""
    task_id, request_id, owl_id = task["id"], record["request_id"], record["owl_id"]
    if record["state"] == "review" and (row is None or not row["has_verdict"]):
        # Its review ended with no verdict (no live review holds this lock), so nothing follows it. Its handoff, if
        # still unfinished, gets its next try as any other.
        owl_post.write_after(task_id, request_id, owl_id, "done")
        return "its round recorded no verdict, so nothing follows it"
    if row is not None and row["has_verdict"]:
        if owl_id is not None and owl_id in owl_post.unfinished_handoffs(task_id):
            # Its verdict is in, so the handoff is never reviewed again.
            owl_post.finish_handoff(task_id, owl_id, f"round {row['round']} at {row['sha'][:12]} recorded"
                                    f" {row['verdict']}")
        settle_verdict(conn, task, row["reviewer"], row["verdict"], row["sha"])
    if record["state"] == "review":
        # Nothing after the verdict had begun, so it starts now, once.
        result = {"verdict": row["verdict"], "round": row["round"], "sha": row["sha"], "request_id": request_id,
                  "handoff_owl": owl_id}
        return _after_verdict(conn, pensieve.get_task(conn, task_id), result, lock_fd, now, checked_owl=owl_id)
    return _interrupted(conn, task, record, row, now)


def _interrupted(conn, task: dict, record: dict, row: Optional[dict], now: Optional[int]) -> str:
    """Ryan hears, once, that a step after a verdict had begun when the loop was cut off, so it may or may not have
    happened, and what to look at. It never runs again by itself. A push or PR whose own ending was already told
    (push:draft-pr or push:auto-failed) is left as told."""
    task_id, desk = task["id"], task["desk"]
    step = record["step"] if row is not None and row["has_verdict"] else None
    sha = None if step is None else row["sha"]
    told = step in ("push", "pr") and any(pensieve.events_with_key_prefix(conn, f"{kind}:{task_id}:{sha}")
                                          for kind in ("push:draft-pr", "push:auto-failed"))
    if step == "fix-round":
        text = (f"round {row['round']} of task {task_id} recorded CHANGES, and the review loop was stopped while it"
                f" started the fix round, so {desk} may or may not have started on it; nothing was started again:"
                f" fleet build {task_id} starts it if no run of {desk} on it is going")
    elif step == "push":
        text = (f"task {task_id} passed review at {sha[:12]}, and the review loop was stopped while it pushed it, so"
                " it may or may not be pushed, and no draft PR was opened; nothing was tried again: look at the"
                f" task's branch, and fleet push {task_id} pushes it by hand")
    elif step == "pr":
        text = (f"task {task_id} passed review and {sha[:12]} is pushed, but the review loop was stopped while it"
                " opened the draft PR, so it may or may not be open; nothing was tried again: look at the repo's"
                " pull requests, and open it by hand if it is not there")
    else:
        text = (f"the review loop was stopped part way through what follows a review round of task {task_id}, and"
                " its record of where it was cannot be read, so nothing was tried again: look at the task's"
                " worktree, branch and pull requests")
    if not told:
        pensieve.add_event(conn, desk, "review.interrupted", "headmaster", common.one_line(text, 480),
                           task_id=task_id, dedupe_key=f"review:interrupted:{record['request_id']}", now=now)
    owl_post.write_after(task_id, record["request_id"], record["owl_id"], "done")
    if told:
        return "its ending was already told"
    return f"interrupted ({step or 'unknown step'}): Ryan heard it may or may not have happened"


def _auto_finish(conn, task: dict, owl_id: str, text: str, verdict: str, now: Optional[int]) -> dict:
    """Finish a handoff for good, the event first, so a review killed in between tells Ryan once on its retry. An
    error's text can quote git, so it is scrubbed of anything shaped like a credential before it is kept."""
    text = pensieve.scrub(text)
    pensieve.add_event(conn, task["desk"], "review.auto", verdict, common.one_line(f"task {task['id']}: {text}", 480),
                       task_id=task["id"], dedupe_key=f"review:auto:{owl_id}", now=now)
    owl_post.finish_handoff(task["id"], owl_id, text)
    return {"task_id": task["id"], "owl_id": owl_id, "outcome": text}


def _auto_wait(conn, task: dict, owl: dict, why: str, now: Optional[int]) -> dict:
    """Leave a handoff for the Owl Post's next pass, unless it has waited AUTO_REVIEW_WAIT_LIMIT_SECONDS since it
    was posted: then it is finished, and Ryan hears why."""
    waited = common.now_stamp(now) - owl["created_at"]
    if waited >= config.AUTO_REVIEW_WAIT_LIMIT_SECONDS:
        hours = config.AUTO_REVIEW_WAIT_LIMIT_SECONDS // 3600
        return _auto_finish(conn, task, owl["id"], f"the automatic review waited {hours} hours and gave up, since"
                            f" {why}; fleet review {task['id']} runs it", "headmaster", now)
    return {"task_id": task["id"], "owl_id": owl["id"],
            "outcome": f"waiting: {why}; the Owl Post tries again on its next pass"}


def _author_running(conn, task: dict, now: Optional[int]) -> bool:
    """Whether a launch of the author desk for this task has recorded no usage yet and may still be running."""
    since = common.now_stamp(now) - config.RUNNING_WINDOW_SECONDS
    return any(row["task_id"] == task["id"] and row["metric_id"] is None and row["launched_at"] > since
               for row in capacity.list_launches(conn, task["desk"]))


def _author_run_over(conn, task: dict, now: Optional[int], wait: bool) -> bool:
    """Whether no run of the author desk is going on this task, waiting up to AUTO_REVIEW_AUTHOR_WAIT_SECONDS when
    wait is set, so the review never commits work its author is still writing."""
    deadline = time.monotonic() + (config.AUTO_REVIEW_AUTHOR_WAIT_SECONDS if wait else 0)
    while _author_running(conn, task, now):
        if time.monotonic() >= deadline:
            return False
        time.sleep(AUTHOR_POLL_SECONDS)
    return True


def _reviewer_busy(conn, reviewer: str, now: Optional[int]) -> bool:
    """Whether the reviewer looks busy from the store alone: as many runs going as it has run slots, or, for a
    reviewer that takes one task at a time, an active task. It takes no lock, so it never makes the reviewer look
    busy to another review; run_review still decides for itself."""
    since = common.now_stamp(now) - config.RUNNING_WINDOW_SECONDS
    going = [row for row in capacity.open_launches(conn, reviewer) if row["launched_at"] > since]
    return len(going) >= run_desk.run_slots(reviewer) or pensieve.blocking_task(conn, reviewer) is not None


def _after_verdict(conn, task: dict, result: dict, lock_fd: int, now: Optional[int],
                   checked_owl: Optional[str] = None) -> str:
    """What the review loop starts after its own round's verdict, under the task's review lock (lock_fd), which a
    fix round's run is handed. The round's after record says acting, with the step, before a fix round, a push or a
    PR begins, and done once all of it has ended and Ryan heard what he must, so a kill at any point leaves the next
    pass to finish it (_recover_after) and none of them ever starts twice. checked_owl is the handoff the loop
    checked before the review; only a review that read that same handoff can push."""
    verdict, round_no, task_id, request_id = result["verdict"], result["round"], task["id"], result["request_id"]
    owl_id = result.get("handoff_owl")
    if verdict == "HEADMASTER":
        outcome = "nothing more starts: the reviewer handed the decision to Ryan"
    elif verdict == "PASS":
        outcome = _after_pass(conn, task, result, now, checked_owl)
    elif capacity.needs_allowance(conn, task_id, config.REVIEW_ROUND_CAP):
        pensieve.add_event(conn, task["desk"], "review.loop-stopped", "headmaster",
                           f"task {task_id} has used its {config.REVIEW_ROUND_CAP} review rounds and round"
                           f" {round_no} recorded {verdict}, so the review loop stopped and no fix round started;"
                           f" castle task allow-round {task_id} allows one more, then fleet build {task_id} starts it",
                           task_id=task_id, dedupe_key=f"review:loop-stopped:{task_id}:{round_no}", now=now)
        outcome = "stopped at the review round cap"
    else:
        owl_post.write_after(task_id, request_id, owl_id, "acting", "fix-round")
        try:
            outcome = worktree.build(conn, task_id, lock_fd)["desk"]
        except (FleetError, StoreError, OSError) as exc:
            outcome = f"it did not start ({common.one_line(exc, 200)})"
        if not outcome.startswith(f"started {task['desk']} "):
            pensieve.add_event(conn, task["desk"], "review.fix-round", "headmaster",
                               common.one_line(pensieve.scrub(f"round {round_no} of task {task_id} recorded {verdict},"
                                                              f" but its fix round did not start: {outcome}; fleet"
                                                              f" build {task_id} starts it"), 480),
                               task_id=task_id, dedupe_key=f"review:fix-round:{task_id}:{round_no}", now=now)
    owl_post.write_after(task_id, request_id, owl_id, "done")
    return outcome


def _after_pass(conn, task: dict, result: dict, now: Optional[int], checked_owl: Optional[str] = None) -> str:
    """A PASS starts no build or review. Without Ryan's opt-in he hears the task is ready for his push. With it, the
    reviewed commit is pushed and a draft PR opened, once, and he hears its URL or why it stopped. Either way it is
    one headmaster event. A review that read a newer handoff than the one the loop checked never pushes. The push
    and the PR are each recorded as begun in the round's after record before they start (see _after_verdict)."""
    task_id, sha = task["id"], result["sha"]
    if not push.auto_draft_pr_on() or checked_owl is None or result.get("handoff_owl") != checked_owl:
        pensieve.add_event(conn, task["desk"], "review.ready-for-push", "headmaster",
                           f"task {task_id} passed review at {sha[:12]} and is ready for push: fleet push {task_id}",
                           task_id=task_id, dedupe_key=f"review:ready:{task_id}:{sha}", now=now)
        return "ready for push"

    def begun(step: str) -> None:
        owl_post.write_after(task_id, result["request_id"], checked_owl, "acting", step)

    try:
        if result.get("handoff_owl") is None:
            raise FleetError("the review had no handoff to take the PR text from")
        handoff = owl_body(conn, result["handoff_owl"])
        title, _ = commit_message(handoff, check_words=False)
        pushed = push.push_draft_pr(conn, task_id, sha, title, pr_body(handoff), on_step=begun)
    except (FleetError, StoreError, OSError) as exc:
        reason = common.one_line(pensieve.scrub(str(exc) if not isinstance(exc, OSError) else type(exc).__name__),
                                 300)
        pensieve.add_event(conn, task["desk"], "push.auto-failed", "headmaster",
                           common.one_line(f"task {task_id} passed review at {sha[:12]}, but the automatic draft PR"
                                           f" stopped and was not retried: {reason}; fleet push {task_id} pushes it by"
                                           " hand", 480),
                           task_id=task_id, dedupe_key=f"push:auto-failed:{task_id}:{sha}", now=now)
        return f"the automatic draft PR stopped: {reason}"
    pensieve.add_event(conn, task["desk"], "push.draft-pr", "headmaster",
                       common.one_line(f"task {task_id} passed review: {sha[:12]} is pushed to {pushed['branch']} and"
                                       f" draft PR {pushed['pr_url']} is open; read it, and mark it ready yourself",
                                       480),
                       task_id=task_id, dedupe_key=f"push:draft-pr:{task_id}:{sha}", now=now)
    return f"opened draft PR {pushed['pr_url']}"


def main(argv: Optional[list] = None) -> int:
    """The automatic review the Owl Post starts (run_desk.spawn_review): one argument, the build task's id. Prints
    one JSON object, like the fleet command."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not isinstance(args[0], str) or ids.PATTERNS["task"].fullmatch(args[0]) is None:
        sys.stderr.write("the automatic review takes one build task id\n")
        return 2
    try:
        conn = common.connect()
    except StoreError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 300)}, ensure_ascii=True) + "\n")
        return 1
    try:
        with common.ended_by_signals():
            data = auto_review(conn, args[0])
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=True) + "\n")
        return 0
    except (FleetError, StoreError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 600)}, ensure_ascii=True) + "\n")
        return 1
    finally:
        conn.close()


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
    branch = _own_branch(common_dir)
    if task_id is None:
        title = ids.clean_text(title or "", "title", 200, single_line=True)
        if not title:
            raise FleetError("a new review needs --title")
        repo = gitops.repo_slug(gitops.git(["config", "--get", "remote.origin.url"], common_dir))
        with own_lineage_lock():
            _check_new_own(conn, repo_dir, common_dir, repo, sha, branch)
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
                pensieve.set_review_branch(conn, task["id"], branch)
                record = worktree.add_worktree(conn, task["id"], repo_dir, base, None, fetch, detach_at=sha)
                task = pensieve.set_worktree(conn, task["id"], f"{ids.WORKTREES_ROOT}/{task['id']}")
            except BaseException:
                _abandon_unreviewed(conn, task["id"])
                raise
        try:
            return _review_own_at(conn, task, record, sha, lock_fd)
        except BaseException:
            _abandon_unreviewed(conn, task["id"])
            raise
    task = pensieve.get_task(conn, target)
    if task["desk"] != OWN_DESK or task["status"] != "active":
        raise FleetError("--task must be an active task of your own sessions")
    record = gitops.find_record(worktree.castle_path(task["worktree"]))
    if record is None or not gitops.same_checkout(record["repo_dir"], repo_dir):
        raise FleetError("that task's worktree is for a different checkout")
    with own_lineage_lock():
        task = _continue_own(conn, task, repo_dir, common_dir, record["repo"], sha, branch)
        # Inside the lock: from here a review on a branch stacked on sha sees it as this task's (see _lineage_shas).
        gitops.git(["checkout", "--detach", sha], record["git_dir"], record["path"])
    return _review_own_at(conn, task, record, sha, lock_fd)


def _abandon_unreviewed(conn, task_id: str) -> None:
    """Close a new own task that failed before its first round opened, so it never sits active, unless its
    commit is already recorded on it: that task stays active for --task to retry, since a closed task never
    reopens and the commit can belong to no other task."""
    with contextlib.suppress(Exception):
        if not capacity.review_rounds(conn, task_id) and not pensieve.task_commits(conn, task_id):
            pensieve.close_task(conn, task_id, "abandoned")


@contextlib.contextmanager
def own_lineage_lock() -> Iterator[None]:
    """Held while a review of Ryan's own sessions decides which task its branch belongs to and records it,
    so two reviews started at once on one branch never both make a task. Never held through a review run."""
    with contextlib.ExitStack() as stack:
        locks_fd = stack.enter_context(safefs.opened_dir(config.OFFICE_ROOT, "locks", create=True))
        try:
            stack.enter_context(safefs.held_lock(locks_fd, OWN_LINEAGE_LOCK, blocking=True,
                                                 timeout=OWN_LINEAGE_WAIT_SECONDS))
        except safefs.Busy:
            raise FleetError("another review of your own sessions is still starting; run this again once it"
                             " has") from None
        yield


def _own_branch(common_dir: str) -> str:
    """The branch the checkout has out, which names the review's lineage. A detached HEAD has none. Any name git
    takes as a branch counts, in printable ASCII: the fleet's branch rule is only for the branches it pushes."""
    branch = gitops.current_branch(common_dir)
    if branch is None:
        raise FleetError("your checkout's HEAD is detached, so this review has no branch to follow: check out"
                         " the branch the commit is on and run it again")
    try:
        return gitops.check_lineage_branch(common_dir, branch)
    except FleetError as exc:
        raise FleetError(f"your checkout's branch cannot name a review: {exc}") from None


def _own_tasks_on(conn, repo_dir: str) -> list:
    """The active own-session tasks whose worktree is for this checkout, matched as a folder."""
    found = []
    for task in pensieve.list_tasks(conn, desk=OWN_DESK, status="active"):
        record = None if task["worktree"] is None else gitops.find_record(worktree.castle_path(task["worktree"]))
        if record is not None and gitops.same_checkout(record["repo_dir"], repo_dir):
            found.append(task)
    return found


def _continue_text(task: dict) -> str:
    return f"fleet review own --repo-dir <checkout> --task {task['id']}"


def _same_repo(first: str, second: str) -> bool:
    """Whether two GitHub slugs name one repository. GitHub takes owner and name in any letter case, so an
    origin URL spelled in another case is still that repository."""
    return first.lower() == second.lower()


def _holder(conn, repo: str, sha: str) -> Optional[dict]:
    """The recorded commit sha of this repository, whatever letter case its slug was recorded in, or None."""
    return next((row for row in pensieve.commits_with_sha(conn, sha) if _same_repo(row["repo"], repo)), None)


def _worktree_head(record: dict) -> Optional[str]:
    """The commit an own task's review worktree is detached at, or None when it is gone or git cannot say."""
    if not os.path.isdir(record["git_dir"]):
        return None
    try:
        out = gitops.git(["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], record["git_dir"],
                         check=False).strip()
    except (FleetError, OSError):
        return None
    return out if gitops.SHA.fullmatch(out) else None


def _lineage_shas(conn, task: dict, repo: str) -> list:
    """The commits on an own task for this repository, oldest first: its recorded commits, its rounds, and last
    the commit its review worktree is at. That one is set under the lineage lock, so a review whose checks are
    still running, before its commit is recorded, already claims the work stacked on it."""
    shas = [row["sha"] for row in pensieve.task_commits(conn, task["id"]) if _same_repo(row["repo"], repo)]
    record = None if task["worktree"] is None else gitops.find_record(worktree.castle_path(task["worktree"]))
    mine = record is not None and _same_repo(record["repo"], repo)
    if shas or mine:
        shas += [row["sha"] for row in capacity.review_rounds(conn, task["id"]) if row["sha"] not in shas]
    pending = _worktree_head(record) if mine else None
    if pending is not None and pending not in shas:
        shas.append(pending)
    return shas


def _built_on(conn, common_dir: str, task: dict, repo: str, sha: str) -> tuple:
    """(commit, known): the newest commit recorded on task that sha builds on (is or descends from), with
    known True, or (None, True) when it builds on none. A shallow checkout, whose history may stop short of a
    recorded commit, or a git that cannot answer, gives (that commit, False)."""
    unknown = None
    for recorded in reversed(_lineage_shas(conn, task, repo)):
        found = gitops.is_ancestor(common_dir, recorded, sha)
        if found:
            return recorded, True
        if found is None and unknown is None:
            unknown = recorded
    return unknown, unknown is None


def _rounds_hint(conn, task: dict) -> str:
    cap = config.REVIEW_ROUND_CAP
    if not capacity.needs_allowance(conn, task["id"], cap):
        return ""
    return (f" once castle task allow-round {task['id']} allows one more round, since it has used its {cap} review"
            f" rounds, or close that task first")


def _elsewhere(task: dict, repo_dir: str) -> str:
    """Where to run --task from when the task was opened on another clone of the repository."""
    record = None if task["worktree"] is None else gitops.find_record(worktree.castle_path(task["worktree"]))
    if record is None or gitops.same_checkout(record["repo_dir"], repo_dir):
        return ""
    return (f" (that task's worktree is for the checkout {record['repo_dir']}, so bring this commit there and run"
            f" it from that checkout)")


def _check_new_own(conn, repo_dir: str, common_dir: str, repo: str, sha: str, branch: str) -> None:
    """Refuse a new own task, before anything is made, for a commit another task already holds, while an
    active task on this checkout follows the same branch, or one the checkout no longer has, and while HEAD
    builds on a commit recorded on any active own task of this repository, from any checkout of it. So a fix
    commit always goes on its open task and its round count, allowance or not, whatever branch or clone it
    was made on, and only work that builds on no open task's commits starts a new task with its own count.
    The checkout is matched as a folder, so another letter case of its path on a case-insensitive disk is
    still that checkout."""
    held = _holder(conn, repo, sha)
    if held is not None:
        owner = pensieve.get_task(conn, held["task_id"])
        if owner["desk"] == OWN_DESK and owner["status"] == "active":
            raise FleetError(f"HEAD {sha[:12]} is already task {owner['id']}; run fleet review own --repo-dir"
                             f" <checkout> --task {owner['id']} to review it again")
        raise FleetError(f"HEAD {sha[:12]} already belongs to task {owner['id']}, which is"
                         f" {owner['status'].replace('_', ' ')}; make a new commit for a new review")
    for other in _own_tasks_on(conn, repo_dir):
        followed = other["review_branch"]
        if followed == branch:
            raise FleetError(f"branch {branch} on this checkout is task {other['id']}, so its fix commits go on"
                             f" that task: run {_continue_text(other)}{_rounds_hint(conn, other)}")
        if followed is None or not gitops.has_branch(common_dir, followed):
            gone = ("names no branch" if followed is None
                    else f"follows branch {followed}, which this checkout no longer has")
            raise FleetError(f"task {other['id']} on this checkout {gone}, so it may be this same work renamed:"
                             f" run {_continue_text(other)} to go on with it here, or close that task first")
    # Lineage by ancestry: a branch made off an open task's commits, renamed, or made in a second clone is
    # still that task's work. A task awaiting close is done, so work built on it starts anew.
    owner, recorded, known = _lineage_owner(conn, common_dir, repo, sha)
    if owner is not None and not known:
        raise FleetError(_unknown_text(common_dir, owner, recorded) + f", run {_continue_text(owner)} if it is that"
                         f" task's work, or close that task first")
    if owner is not None:
        raise FleetError(f"HEAD builds on commit {recorded[:12]} of task {owner['id']}, so it is that task's work"
                         f" and goes on its round count: run {_continue_text(owner)}{_rounds_hint(conn, owner)}"
                         f"{_elsewhere(owner, repo_dir)}")


def _lineage_owner(conn, common_dir: str, repo: str, sha: str, skip: Optional[str] = None) -> tuple:
    """(task, commit, known) for an active own task of this repository, other than skip, that sha builds on,
    or (None, None, True). A task it surely builds on comes before one a shallow checkout cannot rule out."""
    unsure = (None, None, True)
    for other in pensieve.list_tasks(conn, desk=OWN_DESK, status="active"):
        if other["id"] == skip:
            continue
        recorded, known = _built_on(conn, common_dir, other, repo, sha)
        if recorded is not None and known:
            return other, recorded, True
        if recorded is not None and unsure[0] is None:
            unsure = (other, recorded, False)
    return unsure


def _unknown_text(common_dir: str, task: dict, recorded: str) -> str:
    """Why the review cannot tell whether HEAD builds on a task's commit, and what to do about it. Only a shallow
    checkout is cured by fetching its full history, since git refuses --unshallow on a full clone. A full clone
    that cannot tell has missing or damaged history, so it is told to fetch what is missing or clone again."""
    cannot = f"the review cannot tell whether HEAD builds on commit {recorded[:12]} of task {task['id']}"
    if gitops.is_shallow(common_dir):
        return (f"this checkout is shallow, so {cannot}: fetch its full history (git fetch --unshallow) and run"
                f" this again")
    return (f"this checkout's history is incomplete or unreadable, so {cannot}: fetch the missing commits (git fetch"
            f" origin), or clone the repository again, and run this again")


def _continue_own(conn, task: dict, repo_dir: str, common_dir: str, repo: str, sha: str, branch: str) -> dict:
    """A fix round with --task goes on that task's branch. HEAD built on another open own task's commits is
    that task's work, so it is refused here. The task moves to the checkout's branch when HEAD builds on a
    commit recorded on it (a branch made off it), or when its own branch is gone (renamed or deleted) or was
    never recorded, and never onto a branch another task follows."""
    for other in _own_tasks_on(conn, repo_dir):
        if other["id"] != task["id"] and other["review_branch"] == branch:
            raise FleetError(f"branch {branch} on this checkout is task {other['id']}, not task {task['id']}:"
                             f" run {_continue_text(other)}, or check out task {task['id']}'s branch")
    owner, recorded, known = _lineage_owner(conn, common_dir, repo, sha, skip=task["id"])
    if owner is not None and not known:
        raise FleetError(_unknown_text(common_dir, owner, recorded) + f", or close task {owner['id']} first")
    if owner is not None:
        raise FleetError(f"HEAD builds on commit {recorded[:12]} of task {owner['id']}, not task {task['id']}, so it"
                         f" goes on that task's round count: run {_continue_text(owner)}"
                         f"{_rounds_hint(conn, owner)}")
    if task["review_branch"] == branch:
        return task
    if task["review_branch"] is not None and gitops.has_branch(common_dir, task["review_branch"]):
        recorded, known = _built_on(conn, common_dir, task, repo, sha)
        if recorded is not None and not known:
            raise FleetError(_unknown_text(common_dir, task, recorded) + f", or check out branch"
                             f" {task['review_branch']} to go on with that task")
        if recorded is None:
            raise FleetError(f"task {task['id']} follows branch {task['review_branch']}, which this checkout still"
                             f" has: check it out to go on with that task, or leave out --task to start a new task"
                             f" for branch {branch}")
    return pensieve.set_review_branch(conn, task["id"], branch)


def _review_own_at(conn, task: dict, record: dict, sha: str, lock_fd: int) -> dict:
    if gitops.rev(record) != sha:
        raise FleetError("the review worktree is not at your checkout's HEAD")
    with safefs.opened_dir(config.CASTLE_ROOT, "tasks", task["id"]) as fd:
        has_handoff = safefs.is_safe_regular(fd, "handoff.md")
    return run_review(conn, task, record, sha, task["id"], has_handoff, lock_fd)
