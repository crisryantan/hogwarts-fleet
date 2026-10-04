"""The fleet command: the worktree, verify, review and push scripts, for Ryan's terminal only.

  fleet worktree <task-id> --repo-dir <checkout> --branch <name> [--base origin/main] [--no-fetch]
  fleet build <task-id>
  fleet worktree-remove <task-id>
  fleet verify <task-id>
  fleet review <task-id>
  fleet review own --repo-dir <checkout> --title "<what it does>" [--intent-file <file>] [--base ...] [--no-fetch]
  fleet review own --repo-dir <checkout> --task <task-id>
  fleet push <task-id> [--yes]

Output is one JSON object, like castle. Exit 0 on success, 1 on a refusal or error.
Run it through ~/.hogwarts/bin/fleet, which clears the environment first. No desk can run it:
desks cannot read the office, and Ryan's own sessions are denied it by his settings.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
from typing import Iterator, Optional

if __name__ == "__main__" and "/Users/crisryantan/.hogwarts" not in sys.path:
    sys.path.insert(0, "/Users/crisryantan/.hogwarts")

from hogwarts.errors import StoreError  # noqa: E402

from fleet import common, config, gitops, push, review, verify, worktree  # noqa: E402
from fleet.safefs import FleetError  # noqa: E402

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
    pushed = commands.add_parser("push", allow_abbrev=False)
    pushed.add_argument("task")
    pushed.add_argument("--yes", action="store_true")
    return parser


def run(conn, args: argparse.Namespace) -> object:
    if args.command == "worktree":
        return worktree.create(conn, args.task, args.repo_dir, args.branch, args.base, fetch=not args.no_fetch)
    if args.command == "build":
        return worktree.build(conn, args.task)
    if args.command == "worktree-remove":
        return worktree.remove(conn, args.task)
    if args.command == "verify":
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
    raise FleetError("unknown command")


@contextlib.contextmanager
def ended_by_signals() -> Iterator[None]:
    """SIGTERM or SIGHUP (a closed terminal, a caller's timeout) ends the command through its finally blocks,
    so a review closes its reviewer task and a desk run kills its child, instead of Python dying mid-step."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame) -> None:
        raise SystemExit(128 + signum)

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
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
