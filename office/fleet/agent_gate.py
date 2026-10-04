"""The check hogwarts-spaces runs before it opens a live session for McGonagall or Snape.

Any herdr pane can type into any other, so a live agent must have no tool that runs a
command or changes the machine beyond its own files. This reads every agent definition
Claude could load under the agent's name (the user agents folder and the .claude/agents
folder of the working directory and each parent), and passes only when each one has an
explicit tools: list in which every tool is on the agent's trusted list, and only
frontmatter keys on the trusted key list. For McGonagall it also checks the castle settings
still name her.

The trusted list is desks/<agent>/live-tools.json in the office, which no desk or agent can
write: every tool the live pane may have, built-in and MCP, by exact name. A tool matches
only when its name is identical; there are no wildcards or prefixes. A trusted list that
names a built-in able to run commands, or any built-in not known to run nothing, is refused.
It is read the way every office desk file is (fleet/safefs.py): no link in any folder on the
way, a regular file with one hard link, owned by the user and writable by no one else.

It reads frontmatter the way Claude finds it: the block ends at the first --- anywhere, so
a --- inside a line is refused. A file whose name line or keys only a YAML parser could
resolve, or whose frontmatter does not read cleanly, counts as a possible definition of the
agent and is checked too, so a quoted, tagged or folded name cannot hide one.

  agent_gate <agent> <agent-file> <cwd> <user-agents-dir> [<settings-file>]

Exits 0 and prints nothing when the session may open. Otherwise prints one line saying
why and exits 1. Reads only; standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional

from . import safefs

# Run commands, drive the ones running, or hand work to an agent or session that can.
RISKY_TOOLS = {"bash", "bashoutput", "killshell", "killbash", "powershell", "taskoutput", "taskstop",
               "notebookedit", "monitor", "task", "agent", "sendmessage", "croncreate", "remotetrigger",
               "slashcommand"}
# The only built-in tools a trusted list may name: they read, search, edit files, fetch a page or load
# a skill, and run nothing. hogwarts-spaces denies Skill in the live session itself.
SAFE_BUILTINS = {"Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "NotebookRead", "WebFetch",
                 "WebSearch", "TodoWrite", "ToolSearch", "Skill", "ListMcpResourcesTool", "ReadMcpResourceTool"}
# Each live agent's trusted tools, at desks/<agent>/live-tools.json under this office.
OFFICE = Path(__file__).resolve().parents[1]
LIVE_TOOLS_FILE = "live-tools.json"
LIVE_TOOLS_MAX_BYTES = 64 * 1024
AGENT_NAME = re.compile(r"[a-z][a-z0-9-]*")
# Frontmatter keys that cannot start anything. hooks, mcpServers, skills and the rest are refused.
TRUSTED_KEYS = {"name", "description", "model", "tools", "disallowedTools", "color", "effort"}
BUILTIN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9<>._-]+__[A-Za-z0-9<>._-]+")
PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# A scalar in plain quotes: no escapes in double quotes, no doubled quote in single ones.
QUOTED = re.compile(r'"([^"\\]*)"|\'([^\']*)\'')
# A name line only a YAML parser could resolve: a comment, anchor, tag, escape or folded value.
UNCLEAR = "\0unclear"


class Refused(Exception):
    pass


def unquote(text: str) -> str:
    match = QUOTED.fullmatch(text)
    if match is None:
        return text
    return match.group(1) if match.group(1) is not None else match.group(2)


def frontmatter(text: str) -> Optional[dict]:
    """Top-level keys of a leading --- block, each mapped to its raw value lines. None if there is none.

    Claude ends the block at the first --- anywhere, so any line holding one that is not the
    closing line is refused, and so is any character Python and YAML split lines on differently.
    """
    text = text[1:] if text.startswith("\ufeff") else text
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines[0].rstrip() != "---":
        return None
    fields: dict = {}
    key = None
    for number, line in enumerate(lines[1:], 2):
        if line.strip() == "---":
            return fields
        if "---" in line:
            raise Refused(f"its frontmatter line {number} holds ---, where Claude would end the frontmatter")
        if not all(char == "\t" or char.isprintable() for char in line):
            raise Refused(f"its frontmatter line {number} holds a control or line break character")
        if line.startswith("\t"):
            raise Refused(f"its frontmatter line {number} is indented with a tab")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] == " " or line.startswith("- "):
            if key is None:
                raise Refused("its frontmatter starts with an indented line")
            fields[key].append(line.strip())
            continue
        key, colon, value = line.partition(":")
        key = key.strip()
        key = unquote(key) if PLAIN_KEY.fullmatch(unquote(key)) else key
        if not colon or key in fields or (value and value[0] not in " \t"):
            raise Refused(f"its frontmatter line {line.strip()[:60]!r} is not a single key")
        fields[key] = [value.strip()] if value.strip() else []
    return None


def declared_name(fields: dict) -> Optional[str]:
    """The name this frontmatter gives, None if it gives none, or UNCLEAR if only YAML could tell."""
    if any(not PLAIN_KEY.fullmatch(key) for key in fields):
        return UNCLEAR
    raw = fields.get("name")
    if raw is None:
        return None
    if len(raw) == 1 and PLAIN_NAME.fullmatch(unquote(raw[0])):
        return unquote(raw[0])
    return UNCLEAR


def tool_names(raw: List[str]) -> List[str]:
    names = []
    for part in raw:
        part = part[2:].strip() if part.startswith("- ") else part
        part = part[1:-1] if part.startswith("[") and part.endswith("]") else part
        names += [unquote(item.strip()) for item in unquote(part).split(",")]
    return [name for name in names if name]


def list_entry_problem(name: str) -> Optional[str]:
    """Why a trusted list may not name this tool, or None when it is an MCP tool or a safe built-in."""
    if MCP_TOOL.fullmatch(name):
        return None
    if not BUILTIN.fullmatch(name):
        return f"{name!r} is not a plain tool name"
    if name.lower() in RISKY_TOOLS:
        return f"{name} can run commands or start an agent that can"
    if name not in SAFE_BUILTINS:
        return f"{name} is not a built-in tool known to run nothing"
    return None


def trusted_tools(agent: str, office: Path = OFFICE) -> set:
    """The exact tool names the agent's live pane may have, from its list in the office."""
    if not AGENT_NAME.fullmatch(agent):
        raise Refused(f"{agent!r} is not a plain agent name")
    path = office / "desks" / agent / LIVE_TOOLS_FILE
    try:
        with safefs.opened_dir(str(office), "desks", agent) as desk_fd:
            raw = safefs.read_regular(desk_fd, LIVE_TOOLS_FILE, LIVE_TOOLS_MAX_BYTES, "trusted tool list")
    except safefs.Missing:
        raise Refused(f"{path} is missing, so no tool is trusted for {agent}") from None
    except safefs.Unsafe as exc:
        raise Refused(f"{path} is not trusted: {exc}") from None
    except OSError:
        raise Refused(f"{path} could not be read") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Refused(f"{path} could not be read as JSON") from None
    if not isinstance(data, dict) or set(data) != {"tools"} or not isinstance(data["tools"], list) \
            or not data["tools"]:
        raise Refused(f"{path} must be an object whose only key, tools, lists at least one tool")
    for name in data["tools"]:
        problem = list_entry_problem(name) if isinstance(name, str) else f"{name!r} is not a plain tool name"
        if problem:
            raise Refused(f"{path}: {problem}")
    return set(data["tools"])


def tool_problem(name: str, agent: str, trusted: set) -> Optional[str]:
    if MCP_TOOL.fullmatch(name) is None and BUILTIN.fullmatch(name) is None:
        return f"{name!r} is not a plain tool name"
    if name.lower() in RISKY_TOOLS:
        return f"{name} can run commands or start an agent that can"
    if name not in trusted:
        return f"{name} is not on {agent}'s trusted tool list"
    return None


def read_frontmatter(path: Path) -> Optional[dict]:
    try:
        return frontmatter(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        raise Refused("it is not UTF-8 text") from None


def check_definition(path: Path, agent: str, trusted: set) -> dict:
    fields = read_frontmatter(path)
    if fields is None:
        raise Refused("it has no frontmatter")
    unknown = sorted(set(fields) - TRUSTED_KEYS)
    if unknown:
        raise Refused(f"its frontmatter key {unknown[0]} is not on the trusted list")
    if "tools" not in fields:
        raise Refused("it has no tools: line, which gives it every tool")
    names = tool_names(fields["tools"])
    if not names:
        raise Refused("its tools: line lists nothing")
    for name in names:
        problem = tool_problem(name, agent, trusted)
        if problem:
            raise Refused(problem)
    return fields


def may_define(path: Path, agent: str) -> bool:
    """False only when the file plainly gives no name, or plainly another one."""
    try:
        fields = read_frontmatter(path)
    except (OSError, Refused):
        return True
    if fields is None:
        return False
    name = declared_name(fields)
    return name == UNCLEAR or (name is not None and name.lower() == agent.lower())


def markdown_files(folder: Path) -> Iterator[Path]:
    """Every .md file under the folder, following linked folders once each."""
    visited = set()
    for root, dirs, files in os.walk(folder, followlinks=True):
        real = os.path.realpath(root)
        if real in visited:
            dirs[:] = []
            continue
        visited.add(real)
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".md"):
                yield Path(root) / name


def definitions(agent: str, cwd: Path, user_agents: Path) -> List[Path]:
    """Every file Claude could load as this agent: named for it, or whose frontmatter may name it."""
    folders = [user_agents] + [folder / ".claude" / "agents" for folder in (cwd, *cwd.parents)]
    found: List[Path] = []
    seen = set()
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in markdown_files(folder):
            key = path.resolve()
            if key not in seen and (path.stem.lower() == agent.lower() or may_define(path, agent)):
                seen.add(key)
                found.append(path)
    return found


def check_settings(agent: str, settings: Path) -> None:
    for path in (settings, settings.with_name(settings.stem + ".local.json")):
        if path != settings and not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Refused(f"{path} could not be read as JSON") from None
        named = data.get("agent") if isinstance(data, dict) else None
        if path == settings and named != agent:
            raise Refused(f"{path} no longer makes {agent} the default agent")
        if path != settings and named not in (None, agent):
            raise Refused(f"{path} makes {named} the default agent")


def check(agent: str, agent_file: Path, cwd: Path, user_agents: Path, settings: Optional[Path] = None,
          office: Path = OFFICE) -> None:
    trusted = trusted_tools(agent, office)
    if not agent_file.is_file():
        raise Refused(f"{agent_file} is missing")
    others = [path for path in definitions(agent, cwd, user_agents) if path.resolve() != agent_file.resolve()]
    for path in [agent_file] + others:
        try:
            fields = check_definition(path, agent, trusted)
        except Refused as exc:
            raise Refused(f"{path}: {exc}") from None
        except OSError:
            raise Refused(f"{path} could not be read") from None
        if path == agent_file and declared_name(fields) != agent:
            raise Refused(f"{agent_file} does not name itself {agent} plainly")
    if settings is not None:
        check_settings(agent, settings)


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (4, 5) or not all(arg.startswith("/") for arg in args[1:]):
        sys.stdout.write("usage: agent_gate <agent> <agent-file> <cwd> <user-agents-dir> [<settings-file>]\n")
        return 2
    paths = [Path(arg) for arg in args[1:]]
    try:
        check(args[0], paths[0], paths[1], paths[2], paths[3] if len(paths) == 4 else None)
    except Refused as exc:
        sys.stdout.write(" ".join(str(exc).split())[:400] + "\n")
        return 1
    except (OSError, UnicodeDecodeError) as exc:
        sys.stdout.write(f"an agent folder could not be read ({type(exc).__name__})\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
