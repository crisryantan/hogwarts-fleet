"""bin/hogwarts-spaces against a fake herdr that logs every call. The real herdr is never run."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import temp_dir

from fleet import agent_gate
from tests_fleet.support import LIVE_TOOLS_FIXTURES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hogwarts-spaces"
# The script names this home throughout; install.sh rewrites it, and so does each test.
REAL_HOME = "/Users/crisryantan"
NO_SHELL = ("--disallowedTools Bash,BashOutput,KillShell,KillBash,PowerShell,TaskOutput,TaskStop,NotebookEdit,"
            "Monitor,Task,Agent,SendMessage,CronCreate,RemoteTrigger,SlashCommand,Skill --disable-slash-commands")
FEEDS = (("Harry - Senior Engineer", "harry"), ("Hermione - Staff Engineer", "hermione"),
         ("Moody - Security Reviewer", "moody"), ("Ron - Release Engineer", "ron"),
         ("Dumbledore - Knowledge Manager", "portrait"), ("Owl Post - Message Router", "owl-post"),
         ("Ollivander - Model Keeper", "ollivander"))
AGENT = "---\nname: {name}\ndescription: A test agent.\nmodel: sonnet\ntools: {tools}\n---\n\n# Body\n"
# Logs each call as tab-separated arguments, answers like herdr 0.9.3, and fails a create
# for any label listed in the fail file.
FAKE_HERDR = """#!/bin/sh
state={state}
for arg in "$@"; do printf '%s\\t' "$arg"; done >>"$state/calls.log"
printf '\\n' >>"$state/calls.log"
case "$1 $2" in
"workspace list") cat "$state/list.json" ;;
"workspace create")
	if grep -Fxq -- "$4" "$state/fail" 2>/dev/null; then echo "create failed" >&2; exit 1; fi
	printf '{{"result": {{"workspace": {{"workspace_id": "w%s"}}}}}}\\n' "$(wc -l <"$state/calls.log" | tr -d ' ')"
	;;
"pane list") printf '{{"result": {{"panes": [{{"pane_id": "p-%s"}}]}}}}\\n' "$4" ;;
"pane run") printf '{{"result": {{}}}}\\n' ;;
*) exit 2 ;;
esac
"""


class SpacesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = temp_dir(self)
        self.herdr = self.state / "herdr"
        self.herdr.write_text(FAKE_HERDR.format(state=self.state))
        os.chmod(self.herdr, 0o700)
        self.existing(["McGonagall - Chief of Staff", "Someone else's space"])
        # A temp home laid out like an install: the office holds a copy of this checkout's fleet
        # code with its config pointed at the temp home and the temp claude, and the fixture copies
        # of the kit's trusted tool lists, never this office's own. The castle, Documents and user
        # agents folder hold safe definitions. The real home is never read.
        house = self.state / "home"
        self.house = house
        self.castle = house / "hogwarts"
        self.documents = house / "Documents"
        self.user_agents = house / ".claude" / "agents"
        for folder in (self.castle / ".claude" / "agents", self.documents, self.user_agents):
            folder.mkdir(parents=True)
        office = house / ".hogwarts"
        self.office = office
        shutil.copytree(ROOT / "fleet", office / "fleet", ignore=shutil.ignore_patterns("__pycache__"))
        self.config = office / "fleet" / "config.py"
        self.config.write_text(self.config.read_text().replace(REAL_HOME, str(house)))
        for name in ("mcgonagall", "snape"):
            (office / "desks" / name).mkdir(parents=True)
            shutil.copy(LIVE_TOOLS_FIXTURES / f"{name}.json", office / "desks" / name / agent_gate.LIVE_TOOLS_FILE)
        self.fake_claude(house / ".local" / "bin" / "claude")
        # The claude install.sh would pick in this home, whichever one this office's config names.
        self.set_claude_bin(f"{house}/.local/bin/claude")
        (self.castle / ".claude" / "settings.json").write_text(json.dumps({"agent": "mcgonagall"}))
        self.agent("mcgonagall", "Read, Write, Edit, Glob, Grep")
        self.agent("snape", "Read, Grep, Glob, mcp__<warehouse-mcp>__execute_query")
        text = SCRIPT.read_text()
        self.script = self.state / "hogwarts-spaces"
        self.script.write_text(text.replace(REAL_HOME, str(house)))
        self.assertNotIn(REAL_HOME, self.script.read_text())
        claude = f"{house}/.local/bin/claude"
        fleet = f"{house}/.hogwarts/bin/fleet"
        self.spaces = [("McGonagall - Chief of Staff", str(self.castle), f"{claude} --agent mcgonagall --tools Read,Write,Edit,Glob,Grep,ToolSearch {NO_SHELL}"),
                       ("Snape - Data Analyst", str(self.documents), f"{claude} --agent snape --tools Read,Grep,Glob,ToolSearch {NO_SHELL}")]
        self.spaces += [(label, str(self.castle), f"{fleet} feed --desk {desk}") for label, desk in FEEDS]

    def fake_claude(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o700)

    def set_claude_bin(self, value: str) -> None:
        # The line install.sh rewrites when it finds claude somewhere other than the default.
        text, count = re.subn(r'^CLAUDE_BIN = ".*"$', f'CLAUDE_BIN = "{value}"', self.config.read_text(), flags=re.M)
        self.assertEqual(count, 1)
        self.config.write_text(text)

    def agent(self, name: str, tools: str) -> Path:
        folder = self.castle / ".claude" / "agents" if name == "mcgonagall" else self.user_agents
        path = folder / f"{name}.md"
        path.write_text(AGENT.format(name=name, tools=tools))
        return path

    def existing(self, labels: list) -> None:
        workspaces = [{"workspace_id": f"old{index}", "label": label} for index, label in enumerate(labels)]
        (self.state / "list.json").write_text(json.dumps({"result": {"workspaces": workspaces}}))

    def run_script(self, *args) -> tuple:
        done = subprocess.run(["/bin/sh", str(self.script), "--herdr", str(self.herdr), *args], env={"PATH": "/usr/bin:/bin"},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
        return done.returncode, done.stdout.splitlines(), done.stderr

    def calls(self) -> list:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [line.split("\t")[:-1] for line in log.read_text().splitlines()]


class SpacesTests(SpacesCase):
    def test_missing_spaces_are_created_and_existing_ones_skipped(self):
        code, out, err = self.run_script()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, ["SKIP McGonagall - Chief of Staff: already open"]
                         + [f"OK {label}" for label, _, _ in self.spaces[1:]])
        calls = self.calls()
        self.assertEqual(calls[0], ["workspace", "list"])
        creates = [call for call in calls if call[:2] == ["workspace", "create"]]
        self.assertEqual(creates, [["workspace", "create", "--label", label, "--cwd", cwd, "--no-focus"]
                                   for label, cwd, _ in self.spaces[1:]])
        runs = [call for call in calls if call[:2] == ["pane", "run"]]
        self.assertEqual([call[3] for call in runs], [command for _, _, command in self.spaces[1:]])
        # Every pane it types into belongs to a workspace it created on the call just before.
        for index, call in enumerate(calls):
            if call[:2] == ["pane", "run"]:
                self.assertEqual(calls[index - 1][:3], ["pane", "list", "--workspace"])
                self.assertEqual(calls[index - 2][:2], ["workspace", "create"])
                self.assertEqual(call[2], f"p-w{index - 2 + 1}")
        self.assertFalse(any("old0" in arg for call in calls for arg in call))

    def test_a_second_run_changes_nothing(self):
        self.existing([label for label, _, _ in self.spaces])
        code, out, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(out, [f"SKIP {label}: already open" for label, _, _ in self.spaces])
        self.assertEqual(self.calls(), [["workspace", "list"]])

    def test_dry_run_only_lists(self):
        code, out, err = self.run_script("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls(), [["workspace", "list"]])
        self.assertEqual(out[0], "SKIP McGonagall - Chief of Staff: already open")
        self.assertEqual(out[1:], [f"WOULD OPEN {label}: {command} in {cwd}" for label, cwd, command in self.spaces[1:]])

    def test_a_failure_is_reported_and_the_rest_still_open(self):
        (self.state / "fail").write_text("Ron - Release Engineer\n")
        code, out, _ = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("FAILED Ron - Release Engineer: could not create the workspace", out)
        self.assertEqual(sum(line.startswith("OK ") for line in out), len(self.spaces) - 2)
        self.assertFalse(any(call[:2] == ["pane", "run"] and call[3].endswith("--desk ron") for call in self.calls()))

    def test_only_one_label(self):
        code, out, err = self.run_script("--only", "Snape - Data Analyst")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, ["OK Snape - Data Analyst"])
        self.assertEqual([call for call in self.calls() if call[0] == "workspace"][1],
                         ["workspace", "create", "--label", "Snape - Data Analyst", "--cwd",
                          str(self.documents), "--no-focus"])
        self.assertEqual(self.calls()[-1], ["pane", "run", "p-w2", self.spaces[1][2]])
        code, _, err = self.run_script("--only", "Snape")
        self.assertEqual(code, 2)
        self.assertIn("no space is labelled", err)

    def test_a_bad_listing_or_bad_ids_fail_without_typing_anything(self):
        (self.state / "list.json").write_text("not json")
        code, out, _ = self.run_script()
        self.assertEqual((code, out), (1, ["FAILED: could not list the herdr workspaces"]))
        self.assertEqual(self.calls(), [["workspace", "list"]])
        self.existing([])
        self.herdr.write_text(FAKE_HERDR.format(state=self.state).replace('"p-%s"', '"--evil %s"'))
        code, out, _ = self.run_script("--only", "Ron - Release Engineer")
        self.assertEqual(code, 1)
        self.assertTrue(out[0].startswith("FAILED Ron - Release Engineer: the workspace was made"))
        self.assertFalse(any(call[:2] == ["pane", "run"] for call in self.calls()))

    def test_bad_arguments(self):
        for args in (["--bogus"], ["--only"], ["--herdr", "relative/herdr"]):
            with self.subTest(args=args):
                done = subprocess.run(["/bin/sh", str(self.script), *args], env={"PATH": "/usr/bin:/bin"},
                                      capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(done.returncode, 2)
        self.assertEqual(self.calls(), [])


class LiveSessionGateTests(SpacesCase):
    """The boundary for the two live sessions: refuse an unsafe definition, and deny shells anyway."""

    def created(self) -> list:
        return [call[3] for call in self.calls() if call[:2] == ["workspace", "create"]]

    def test_live_sessions_start_with_the_shell_tools_denied(self):
        self.existing([])
        code, out, err = self.run_script("--only", "McGonagall - Chief of Staff")
        self.assertEqual((code, out), (0, ["OK McGonagall - Chief of Staff"]), err)
        code, out, err = self.run_script("--only", "Snape - Data Analyst")
        self.assertEqual((code, out), (0, ["OK Snape - Data Analyst"]), err)
        runs = [call[3] for call in self.calls() if call[:2] == ["pane", "run"]]
        self.assertEqual(runs, [self.spaces[0][2], self.spaces[1][2]])
        for command in runs:
            denied = command.split(" --disallowedTools ")[1].split(" ")[0].split(",")
            self.assertEqual(denied[:4], ["Bash", "BashOutput", "KillShell", "KillBash"])
            # Every tool the gate calls risky is denied here too, and Skill, since a skill can carry hooks.
            self.assertEqual({tool.lower() for tool in denied}, agent_gate.RISKY_TOOLS | {"skill"})
            self.assertTrue(command.endswith(" --disable-slash-commands"))
            # A fixed built-in list, all of it on the gate's safe list, and no Skill.
            tools = command.split(" --tools ")[1].split(" ")[0].split(",")
            self.assertTrue(set(tools) <= agent_gate.SAFE_BUILTINS - {"Skill"}, tools)
            self.assertIn("ToolSearch", tools)
            # And every one of them on that agent's trusted list in the kit, copied into the temp office.
            agent = command.split(" --agent ")[1].split(" ")[0]
            self.assertTrue(set(tools) <= agent_gate.trusted_tools(agent, self.office), (agent, tools))

    def test_an_unsafe_definition_is_refused_and_nothing_is_made_for_it(self):
        self.existing([])
        cases = (("snape", "Read, Bash", "Bash can run commands"),
                 ("snape", "Read, mcp__ops__exec_in_pod", "mcp__ops__exec_in_pod is not on snape's trusted tool list"),
                 ("snape", "Read, mcp__herdr__pane_run", "mcp__herdr__pane_run is not on snape's trusted tool list"),
                 ("mcgonagall", "Read, mcp__<chat-mcp>__send_message",
                  "mcp__<chat-mcp>__send_message is not on mcgonagall's trusted tool list"),
                 ("mcgonagall", None, "no tools: line, which gives it every tool"))
        for name, tools, reason in cases:
            with self.subTest(name=name, tools=tools):
                path = self.agent(name, tools or "Read")
                if tools is None:
                    path.write_text(path.read_text().replace("tools: Read\n", ""))
                (self.state / "calls.log").write_text("")
                code, out, _ = self.run_script()
                self.assertEqual(code, 1)
                label = "Snape - Data Analyst" if name == "snape" else "McGonagall - Chief of Staff"
                refused = [line for line in out if line.startswith("FAILED")]
                self.assertEqual(len(refused), 1)
                self.assertTrue(refused[0].startswith(f"FAILED {label}: refused, {path}: "), refused[0])
                self.assertIn(reason, refused[0])
                self.assertNotIn(label, self.created())
                self.assertEqual(len(self.created()), len(self.spaces) - 1)
                self.agent(name, "Read, Grep")

    def test_a_same_named_project_agent_is_checked_too(self):
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "snape.md").write_text(AGENT.format(name="snape", tools="Read, Bash"))
        code, out, _ = self.run_script("--only", "Snape - Data Analyst")
        self.assertEqual(code, 1)
        self.assertIn(str(project / "snape.md"), out[0])
        self.assertEqual(self.created(), [])

    def test_an_edited_user_snape_that_hides_its_tools_is_refused(self):
        # install.sh keeps a differing ~/.claude/agents/snape.md; these each leave Claude with no tools line.
        snape = self.user_agents / "snape.md"
        for text in ("---\nname: snape\ndescription: Snape --- the analyst\ntools: Read, Grep\n---\n",
                     "---\nname: snape\ndescription: x\u2028tools: Read\n---\n",
                     "---\nname: snape\ndescription: x\ntools: null\n---\n"):
            with self.subTest(text=text):
                snape.write_text(text)
                code, out, _ = self.run_script("--only", "Snape - Data Analyst")
                self.assertEqual(code, 1)
                self.assertTrue(out[0].startswith(f"FAILED Snape - Data Analyst: refused, {snape}: "), out[0])
                self.assertEqual(self.created(), [])

    def test_a_project_agent_that_only_yaml_names_snape_is_checked(self):
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "helper.md").write_text("---\nname: snape # analyst\ndescription: x\n"
                                           "mcpServers:\n  x:\n    command: /bin/sh\n---\n")
        code, out, _ = self.run_script("--only", "Snape - Data Analyst")
        self.assertEqual(code, 1)
        self.assertIn(str(project / "helper.md"), out[0])
        self.assertEqual(self.created(), [])

    def test_mcgonagall_is_refused_when_the_castle_settings_stop_naming_her(self):
        self.existing([])
        (self.castle / ".claude" / "settings.json").write_text(json.dumps({"agent": "builder"}))
        code, out, _ = self.run_script("--only", "McGonagall - Chief of Staff")
        self.assertEqual(code, 1)
        self.assertIn("no longer makes mcgonagall the default agent", out[0])
        self.assertEqual(self.calls(), [["workspace", "list"]])

    def test_dry_run_reports_a_refusal(self):
        self.agent("snape", "Read, KillShell")
        code, out, _ = self.run_script("--dry-run", "--only", "Snape - Data Analyst")
        self.assertEqual(code, 1)
        self.assertIn("KillShell can run commands", out[0])
        self.assertEqual(self.calls(), [["workspace", "list"]])

    def test_a_missing_trusted_list_refuses_the_space(self):
        (self.house / ".hogwarts" / "desks" / "snape" / agent_gate.LIVE_TOOLS_FILE).unlink()
        code, out, _ = self.run_script("--only", "Snape - Data Analyst")
        self.assertEqual(code, 1)
        self.assertIn("live-tools.json is missing, so no tool is trusted for snape", out[0])
        self.assertEqual(self.created(), [])

    def test_an_open_space_is_skipped_without_a_check(self):
        (self.castle / ".claude" / "agents" / "mcgonagall.md").unlink()
        code, out, _ = self.run_script("--only", "McGonagall - Chief of Staff")
        self.assertEqual((code, out), (0, ["SKIP McGonagall - Chief of Staff: already open"]))


class ClaudeBinaryTests(SpacesCase):
    """The live sessions run CLAUDE_BIN from fleet/config.py, the one install.sh picks."""

    def test_the_script_names_no_claude_of_its_own(self):
        self.assertNotIn("bin/claude", SCRIPT.read_text())

    def test_a_homebrew_claude_chosen_by_install_is_used(self):
        brew = self.house / "homebrew" / "bin" / "claude"
        self.fake_claude(brew)
        (self.house / ".local" / "bin" / "claude").unlink()
        self.set_claude_bin(str(brew))
        self.existing([])
        code, out, err = self.run_script()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, [f"OK {label}" for label, _, _ in self.spaces])
        runs = [call[3] for call in self.calls() if call[:2] == ["pane", "run"]]
        default = f"{self.house}/.local/bin/claude "
        self.assertEqual(runs[:2], [command.replace(default, f"{brew} ") for _, _, command in self.spaces[:2]])
        self.assertTrue(all(run.startswith(f"{brew} --agent ") for run in runs[:2]))

    def test_a_missing_claude_fails_the_live_spaces_only(self):
        self.set_claude_bin(f"{self.house}/nowhere/claude")
        self.existing([])
        code, out, _ = self.run_script()
        self.assertEqual(code, 1)
        for label in ("McGonagall - Chief of Staff", "Snape - Data Analyst"):
            self.assertIn(f"FAILED {label}: claude is not at {self.house}/nowhere/claude, the CLAUDE_BIN in "
                          "fleet/config.py", out)
            self.assertNotIn(label, [call[3] for call in self.calls() if call[:2] == ["workspace", "create"]])
        self.assertEqual(out[2:], [f"OK {label}" for label, _ in FEEDS])

    def test_a_claude_path_that_is_not_plain_is_never_typed(self):
        for value in ("claude", f"{self.house}/.local/bin/claude; echo hi", f"{self.house}/my claude"):
            with self.subTest(value=value):
                self.set_claude_bin(value)
                (self.state / "calls.log").write_text("")
                code, out, _ = self.run_script("--only", "Snape - Data Analyst")
                self.assertEqual((code, out), (1, ["FAILED Snape - Data Analyst: CLAUDE_BIN in fleet/config.py is "
                                                   "not a plain absolute path"]))
                self.assertEqual(self.calls(), [["workspace", "list"]])
