"""The fleet command: the worktree, verify, review and push scripts, for Ryan's terminal only.

  fleet worktree <task-id> --repo-dir <checkout> --branch <name> [--base origin/main] [--no-fetch]
  fleet build <task-id>
  fleet worktree-remove <task-id>
  fleet verify <task-id>
  fleet review <task-id>
  fleet review own --repo-dir <checkout> --title "<what it does>" [--intent-file <file>] [--base ...] [--no-fetch]
  fleet review own --repo-dir <checkout> --task <task-id>
  fleet feed --desk <name> | --all
  fleet push <task-id> [--yes]
  fleet ollivander [--dry-run]
  fleet gringotts [--drill [ARCHIVE]]

Output is one JSON object, like castle. Exit 0 on success, 1 on a refusal or error. fleet feed
is the exception: it prints a live, read-only text feed until Ctrl+C (see fleet/feed.py).
Run it through ~/.hogwarts/bin/fleet, which clears the environment first. No desk can run it:
desks cannot read the office, and Ryan's own sessions are denied it by his settings.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts import ids  # noqa: E402
from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, gitops, push, review, verify, worktree  # noqa: E402
from fleet import gringotts, ollivander  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402
from fleet import feed  # noqa: E402

INTENT_MAX_BYTES = 16384


def read_intent(path: str) -> str:
    """A text file Ryan wrote in his home folder: plain, his own, not a symlink, at most 16KB."""
    path = gitops.check_safe_path(path, "intent file")
    if not path.startswith(config.USER_HOME_DIR + "/") or os.path.realpath(path) != path:
        raise FleetError("the intent file must be a plain file in your home folder")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not (info.st_mode & 0o170000 == 0o100000) or info.st_uid != os.getuid() or info.st_size > INTENT_MAX_BYTES:
            raise FleetError("the intent file must be your own regular file of at most 16KB")
        data = os.read(fd, INTENT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FleetError("the intent file is not UTF-8") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("worktree", allow_abbrev=False)
    made.add_argument("task")
    made.add_argument("--repo-dir", required=True)
    made.add_argument("--branch", required=True)
    made.add_argument("--base", default=config.DEFAULT_BASE)
    made.add_argument("--no-fetch", action="store_true")
    commands.add_parser("build", allow_abbrev=False).add_argument("task")
    commands.add_parser("worktree-remove", allow_abbrev=False).add_argument("task")
    commands.add_parser("verify", allow_abbrev=False).add_argument("task")
    reviewed = commands.add_parser("review", allow_abbrev=False)
    reviewed.add_argument("target", help="a build task id, or 'own' for a commit from your own session")
    reviewed.add_argument("--repo-dir")
    reviewed.add_argument("--title")
    reviewed.add_argument("--intent-file")
    reviewed.add_argument("--task")
    reviewed.add_argument("--base", default=config.DEFAULT_BASE)
    reviewed.add_argument("--no-fetch", action="store_true")
    watched = commands.add_parser("feed", allow_abbrev=False)
    which = watched.add_mutually_exclusive_group(required=True)
    which.add_argument("--desk", help="one desk; owl-post shows every owl")
    which.add_argument("--all", action="store_true", help="every desk")
    pushed = commands.add_parser("push", allow_abbrev=False)
    pushed.add_argument("task")
    pushed.add_argument("--yes", action="store_true")
    commands.add_parser("ollivander", allow_abbrev=False).add_argument("--dry-run", action="store_true")
    banked = commands.add_parser("gringotts", allow_abbrev=False)
    banked.add_argument("--drill", nargs="?", const="newest", default=None, metavar="ARCHIVE")
    return parser


def run(conn, args: argparse.Namespace) -> object:
    if args.command == "worktree":
        return worktree.create(conn, args.task, args.repo_dir, args.branch, args.base, fetch=not args.no_fetch)
    if args.command == "build":
        # The review lock every review of the task holds, the automatic one included, so a fix round never starts
        # while a review is committing, checking or judging the desk's work. The run is handed it, so no review
        # starts until the run has ended either.
        with review.task_review_lock(args.task) as lock_fd:
            return worktree.build(conn, args.task, lock_fd)
    if args.command == "worktree-remove":
        return worktree.remove(conn, args.task)
    if args.command == "verify":
        with review.task_review_lock(args.task):  # never under a running review, whose evidence it would replace
            return verify.verify(conn, args.task)
    if args.command == "review":
        if args.target != "own":
            if any(value is not None for value in (args.repo_dir, args.title, args.intent_file, args.task)):
                raise FleetError("a build task review takes only its task id")
            return review.review_build(conn, args.target)
        if args.repo_dir is None or (args.title is None) == (args.task is None):
            raise FleetError("review own needs --repo-dir and either --title for a new review or --task for a fix round")
        intent = None if args.intent_file is None else read_intent(args.intent_file)
        return review.review_own(conn, args.repo_dir, title=args.title, intent=intent, task_id=args.task,
                                 base=args.base, fetch=not args.no_fetch)
    if args.command == "push":
        return push.push(conn, args.task, confirm=None if args.yes else push.ask_terminal)
    if args.command == "ollivander":
        return ollivander.run(conn, dry_run=args.dry_run)
    if args.command == "gringotts":
        return run_gringotts(args.drill)
    raise FleetError("unknown command")


def run_gringotts(drill: Optional[str]) -> dict:
    """A backup now, or a restore drill of the newest archive or the one named. A drill that found a problem
    is a refusal, so the command exits 1 and names the first problems."""
    with gringotts.locked():
        if drill is None:
            return gringotts.backup()
        result = gringotts.drill(None if drill == "newest" else drill)
    if not result["ok"]:
        raise FleetError("the restore drill found problems: " + "; ".join(result["problems"][:3]))
    return result


# SIGTERM or SIGHUP ends the command through its finally blocks. It lives in common, so run_desk can use it too.
ended_by_signals = common.ended_by_signals


def run_feed(desk: Optional[str]) -> int:
    """The live feed opens the store read-only itself, so it never takes the read-write connection."""
    if desk is not None and ids.PATTERNS["desk"].fullmatch(desk) is None:
        sys.stdout.write("fleet feed: invalid desk name\n")
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return feed.follow(desk)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "feed":
        return run_feed(None if args.all else args.desk)
    try:
        conn = common.connect()
    except StoreError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 300)}, ensure_ascii=True) + "\n")
        return 1
    try:
        with ended_by_signals():
            data = run(conn, args)
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=True, indent=2) + "\n")
        return 0
    except (FleetError, StoreError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": common.one_line(exc, 600)}, ensure_ascii=True) + "\n")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
