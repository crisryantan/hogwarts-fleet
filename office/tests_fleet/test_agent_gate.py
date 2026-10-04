"""fleet.agent_gate: the check before McGonagall's or Snape's live herdr session opens."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from tests.support import temp_dir

from fleet import agent_gate

ROOT = Path(__file__).resolve().parents[1]
# The kit's own definitions, present in a clone of the repo but not in an installed office.
KIT_MCGONAGALL = ROOT.parent / "castle" / ".claude" / "agents" / "mcgonagall.md"
KIT_SNAPE = ROOT.parent / "claude-agents" / "snape.md"
KIT_SETTINGS = ROOT.parent / "castle" / ".claude" / "settings.json"


def definition(name: str, tools: str = "tools: Read, Grep, Glob", extra: str = "") -> str:
    lines = ["---", f"name: {name}", "description: A test agent.", "model: sonnet"]
    lines += [tools] if tools else []
    lines += [extra] if extra else []
    return "\n".join(lines + ["---", "", "# Body", ""])


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.house = temp_dir(self)
        self.user_agents = self.house / ".claude" / "agents"
        self.castle = self.house / "hogwarts"
        self.documents = self.house / "Documents"
        for folder in (self.user_agents, self.castle / ".claude" / "agents", self.documents):
            folder.mkdir(parents=True)
        self.settings = self.castle / ".claude" / "settings.json"
        self.settings.write_text(json.dumps({"agent": "mcgonagall"}))
        self.snape = self.user_agents / "snape.md"
        self.mcgonagall = self.castle / ".claude" / "agents" / "mcgonagall.md"
        self.snape.write_text(definition("snape"))
        self.mcgonagall.write_text(definition("mcgonagall"))
        # A temp office holding a copy of the kit's trusted tool lists.
        self.office = self.house / ".hogwarts"
        for agent in ("snape", "mcgonagall"):
            (self.office / "desks" / agent).mkdir(parents=True)
            shutil.copy(ROOT / "desks" / agent / agent_gate.LIVE_TOOLS_FILE, self.trusted_file(agent))

    def trusted_file(self, agent: str) -> Path:
        return self.office / "desks" / agent / agent_gate.LIVE_TOOLS_FILE

    def trust(self, agent: str, tools) -> None:
        self.trusted_file(agent).write_text(json.dumps({"tools": tools}))

    def check_snape(self, office: Path = None) -> None:
        agent_gate.check("snape", self.snape, self.documents, self.user_agents, office=office or self.office)

    def check_mcgonagall(self, office: Path = None) -> None:
        agent_gate.check("mcgonagall", self.mcgonagall, self.castle, self.user_agents, self.settings,
                         office=office or self.office)

    def assertRefused(self, check, fragment: str) -> None:
        with self.assertRaises(agent_gate.Refused) as caught:
            check()
        self.assertIn(fragment, str(caught.exception))


class DefinitionTests(GateCase):
    def test_safe_definitions_pass(self):
        self.check_snape()
        self.check_mcgonagall()

    @unittest.skipUnless(KIT_SNAPE.is_file() and KIT_MCGONAGALL.is_file(), "the kit agent files are only in a clone")
    def test_the_kit_definitions_pass(self):
        self.snape.write_text(KIT_SNAPE.read_text())
        self.mcgonagall.write_text(KIT_MCGONAGALL.read_text())
        self.settings.write_text(KIT_SETTINGS.read_text())
        # Against the kit's own trusted lists, read where an install puts them.
        self.check_snape(office=ROOT)
        self.check_mcgonagall(office=ROOT)

    def test_bash_and_its_helpers_are_refused(self):
        for tool in ("Bash", "BashOutput", "KillShell", "KillBash", "NotebookEdit", "Monitor", "Task", "Agent",
                     "bash", "PowerShell", "SendMessage"):
            with self.subTest(tool=tool):
                self.snape.write_text(definition("snape", f"tools: Read, {tool}, Grep"))
                self.assertRefused(self.check_snape, tool)

    def test_a_missing_or_empty_tools_line_is_refused(self):
        self.snape.write_text(definition("snape", tools=""))
        self.assertRefused(self.check_snape, "no tools: line, which gives it every tool")
        self.mcgonagall.write_text(definition("mcgonagall", "tools:"))
        self.assertRefused(self.check_mcgonagall, "lists nothing")

    def test_a_read_query_tool_passes(self):
        self.snape.write_text(definition("snape", "tools: Read, mcp__<warehouse-mcp>__execute_query"))
        self.check_snape()

    def test_anything_but_a_plain_tool_name_is_refused(self):
        for tool in ("Bash(git status)", "*", "mcp__whole-server", "Read Bash"):
            with self.subTest(tool=tool):
                self.snape.write_text(definition("snape", f"tools: Read, {tool}"))
                self.assertRefused(self.check_snape, "not a plain tool name")

    def test_list_forms_are_read_too(self):
        self.snape.write_text(definition("snape", "tools:\n  - Read\n  - Grep"))
        self.check_snape()
        self.snape.write_text(definition("snape", "tools:\n- Read\n- Bash"))
        self.assertRefused(self.check_snape, "Bash")
        self.snape.write_text(definition("snape", "tools: [Read, 'Bash']"))
        self.assertRefused(self.check_snape, "Bash")

    def test_keys_that_can_start_something_are_refused(self):
        for extra in ("hooks:\n  Stop: []", "mcpServers:\n  - box", "permissionMode: bypassPermissions",
                      "skills: deploy"):
            with self.subTest(extra=extra):
                self.snape.write_text(definition("snape", extra=extra))
                self.assertRefused(self.check_snape, "not on the trusted list")

    def test_a_duplicate_tools_key_is_refused(self):
        self.snape.write_text(definition("snape", extra="tools: Bash"))
        self.assertRefused(self.check_snape, "is not a single key")

    def test_a_missing_or_misnamed_file_is_refused(self):
        self.snape.unlink()
        self.assertRefused(self.check_snape, "is missing")
        self.snape.write_text(definition("severus"))
        self.assertRefused(self.check_snape, "does not name itself snape")
        self.snape.write_text("# Snape\n\nNo frontmatter.\n")
        self.assertRefused(self.check_snape, "has no frontmatter")


class OtherDefinitionTests(GateCase):
    def test_a_project_agent_of_the_same_name_is_checked(self):
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "snape.md").write_text(definition("snape", "tools: Read, Bash"))
        self.assertRefused(self.check_snape, str(project / "snape.md"))

    def test_another_file_that_names_itself_the_agent_is_checked(self):
        (self.user_agents / "nested").mkdir()
        (self.user_agents / "nested" / "potions.md").write_text(definition("snape", tools=""))
        self.assertRefused(self.check_snape, "potions.md")
        (self.user_agents / "user-mcgonagall.md").write_text(definition("mcgonagall", "tools: Bash"))
        self.assertRefused(self.check_mcgonagall, "user-mcgonagall.md")

    def test_unrelated_agents_are_left_alone(self):
        (self.user_agents / "builder.md").write_text(definition("builder", "tools: Bash, Edit"))
        (self.user_agents / "notes.md").write_text("no frontmatter at all\n")
        self.check_snape()
        self.check_mcgonagall()



class FrontmatterTrapTests(GateCase):
    """Frontmatter that Claude's loader reads differently from a line-by-line reading."""

    def test_a_name_only_yaml_resolves_is_checked(self):
        # Each resolves to the agent's name under YAML; with hooks and no tools it must be refused.
        lines = ("name: {a} # analyst", "name: &a {a}", "name: !!str {a}", 'name: "{a}\\x21"', "name: >-\n  {a}",
                 "name:\n  {a}", '"name": {a}', "'name': other\n<<: {{name: {a}}}", "? name\n: {a}")
        for agent, folder, check in (("snape", self.documents / ".claude" / "agents", self.check_snape),
                                     ("mcgonagall", self.castle / ".claude" / "agents", self.check_mcgonagall)):
            folder.mkdir(parents=True, exist_ok=True)
            for line in lines:
                with self.subTest(agent=agent, line=line):
                    helper = folder / "helper.md"
                    helper.write_text("---\n" + line.format(a=agent) + "\ndescription: x\n"
                                      "hooks:\n  SessionStart: []\n---\nbody\n")
                    self.assertRefused(check, str(helper))
            (folder / "helper.md").unlink()

    def test_a_plainly_quoted_name_still_counts(self):
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "helper.md").write_text(definition("'snape'", "tools: Read, Bash"))
        self.assertRefused(self.check_snape, "helper.md")
        (project / "helper.md").write_text(definition('"Snape"', "tools: Read, Bash"))
        self.assertRefused(self.check_snape, "helper.md")
        (project / "helper.md").write_text(definition('"severus"', "tools: Read, Bash"))
        self.check_snape()

    def test_three_dashes_inside_a_line_are_refused(self):
        # Claude ends the frontmatter at the first ---, so the tools line after it would be lost.
        self.snape.write_text("---\nname: snape\ndescription: Snape --- the analyst\ntools: Read, Grep\n---\n")
        self.assertRefused(self.check_snape, "line 3 holds ---")
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        self.snape.write_text(definition("snape"))
        (project / "x.md").write_text("---\nname: snape\ntools: Read\nmodel: sonnet---\nhooks: {}\n")
        self.assertRefused(self.check_snape, "x.md")

    def test_line_breaks_python_and_yaml_disagree_on_are_refused(self):
        for mark in (" ", " ", "\x85", "\x0b", "\x0c", "\r"):
            with self.subTest(mark=repr(mark)):
                self.snape.write_text(f"---\nname: snape\ndescription: x{mark}tools: Read\n---\n")
                self.assertRefused(self.check_snape, "control or line break character")
        self.snape.write_text("---\r\nname: snape\r\ndescription: x\r\ntools: Read, Grep\r\n---\r\n")
        self.check_snape()

    def test_yaml_words_are_not_tool_names(self):
        for value in ("null", "Null", "NULL", "true", "false", "~", "yes", "Frobnicate", "read"):
            with self.subTest(value=value):
                self.snape.write_text(definition("snape", f"tools: {value}"))
                with self.assertRaises(agent_gate.Refused):
                    self.check_snape()
        self.snape.write_text(definition("snape", "tools: null"))
        self.assertRefused(self.check_snape, "null is not on snape's trusted tool list")

    def test_quoting_must_be_plain(self):
        self.snape.write_text(definition("snape", 'tools: "Read, Grep"'))
        self.check_snape()
        for tools in ("tools: 'Read\", Grep", 'tools: "Re\\x61d"', "tools: Read # Grep", "tools: &t Read",
                      "tools: *t", "tools: !!null", "tools: [Read,\n  Grep]"):
            with self.subTest(tools=tools):
                self.snape.write_text(definition("snape", tools))
                self.assertRefused(self.check_snape, "not a plain tool name")

    def test_a_key_without_a_space_after_its_colon_is_refused(self):
        self.snape.write_text("---\nname: snape\ndescription: x\ntools:Read\n---\n")
        self.assertRefused(self.check_snape, "is not a single key")

    def test_a_tab_indent_is_refused(self):
        self.snape.write_text("---\nname: snape\ntools:\n\t- Read\n---\n")
        self.assertRefused(self.check_snape, "indented with a tab")

    def test_the_agent_file_must_name_itself_plainly(self):
        self.snape.write_text(definition("Snape"))
        self.assertRefused(self.check_snape, "does not name itself snape plainly")
        self.snape.write_text(definition('"snape"'))
        self.check_snape()

    def test_a_file_that_does_not_read_cleanly_is_checked(self):
        (self.user_agents / "broken.md").write_bytes(b"---\nname: sn\xffape\ntools: Read\n---\n")
        self.assertRefused(self.check_snape, "broken.md: it is not UTF-8 text")
        (self.user_agents / "broken.md").write_text("---\nname: snape\nname: severus\ntools: Read\n---\n")
        self.assertRefused(self.check_snape, "broken.md")

    def test_a_linked_folder_is_followed(self):
        outside = self.house / "elsewhere"
        outside.mkdir()
        (outside / "helper.md").write_text(definition("snape", "tools: Read, Bash"))
        project = self.documents / ".claude" / "agents"
        project.mkdir(parents=True)
        (project / "linked").symlink_to(outside)
        (outside / "loop").symlink_to(project)
        self.assertRefused(self.check_snape, "helper.md")

    def test_unrelated_agents_with_any_keys_are_still_left_alone(self):
        (self.user_agents / "ops.md").write_text(definition("ops", "tools: Bash", extra="hooks:\n  Stop: []"))
        (self.user_agents / "Notes.MD").write_text("---\nname: notes\n---\n")
        self.check_snape()

class TrustedListTests(GateCase):
    """Every tool a live definition lists must be on its agent's trusted list, by exact name."""

    def test_an_unknown_mcp_tool_is_refused(self):
        for tool in ("mcp__notes__read_note", "mcp__<warehouse-mcp>__drop_table", "mcp__k8s__exec_in_pod",
                     "mcp__term__run_in_terminal", "mcp__terminal__read_terminal", "mcp__ops__kubectl_apply",
                     "mcp__gateway__execute_tool", "mcp__box__shell"):
            with self.subTest(tool=tool):
                self.snape.write_text(definition("snape", f"tools: Read, {tool}"))
                self.assertRefused(self.check_snape, f"{tool} is not on snape's trusted tool list")

    def test_a_pane_input_tool_is_refused(self):
        # Tools that type into or drive another pane can have harmless names, so only the trusted list stops them.
        for tool in ("mcp__herdr__pane_run", "mcp__herdr__pane_send_keys", "mcp__desktop__type_text",
                     "mcp__desktop__press_key"):
            for agent, path, check in (("snape", self.snape, self.check_snape),
                                       ("mcgonagall", self.mcgonagall, self.check_mcgonagall)):
                with self.subTest(tool=tool, agent=agent):
                    path.write_text(definition(agent, f"tools: Read, {tool}"))
                    self.assertRefused(check, f"{tool} is not on {agent}'s trusted tool list")
                    path.write_text(definition(agent))

    def test_a_mutation_tool_on_a_trusted_server_is_refused(self):
        for tool in ("mcp__<chat-mcp>__send_message", "mcp__<chat-mcp>__create_message",
                     "mcp__<chat-mcp>__update_message", "mcp__<chat-mcp>__delete_message"):
            with self.subTest(tool=tool):
                self.mcgonagall.write_text(definition("mcgonagall", f"tools: Read, mcp__<chat-mcp>__list_messages, {tool}"))
                self.assertRefused(self.check_mcgonagall, f"{tool} is not on mcgonagall's trusted tool list")

    def test_only_an_exact_name_matches(self):
        for tool in ("mcp__<chat-mcp>__list_messages_and_send", "mcp__<chat-mcp>__List_messages",
                     "mcp__<chat-mcp>__list", "mcp__chat-mcp__list_messages", "grep", "READ"):
            with self.subTest(tool=tool):
                self.mcgonagall.write_text(definition("mcgonagall", f"tools: Read, {tool}"))
                self.assertRefused(self.check_mcgonagall, f"{tool} is not on mcgonagall's trusted tool list")
        for tool in ("mcp__<chat-mcp>", "mcp__<chat-mcp>__*", "mcp__<chat-mcp>__list*"):
            with self.subTest(tool=tool):
                self.mcgonagall.write_text(definition("mcgonagall", f"tools: Read, {tool}"))
                self.assertRefused(self.check_mcgonagall, "not a plain tool name")

    def test_each_agent_has_its_own_list(self):
        self.snape.write_text(definition("snape", "tools: Read, mcp__<chat-mcp>__list_messages"))
        self.assertRefused(self.check_snape, "mcp__<chat-mcp>__list_messages is not on snape's trusted tool list")
        self.mcgonagall.write_text(definition("mcgonagall", "tools: Read, mcp__<warehouse-mcp>__execute_query"))
        self.assertRefused(self.check_mcgonagall, "is not on mcgonagall's trusted tool list")

    def test_a_tool_added_to_the_list_passes(self):
        self.snape.write_text(definition("snape", "tools: Read, mcp__notes__read_note"))
        self.assertRefused(self.check_snape, "mcp__notes__read_note is not on snape's trusted tool list")
        self.trust("snape", ["Read", "mcp__notes__read_note"])
        self.check_snape()

    def test_a_trusted_list_naming_a_command_runner_is_refused(self):
        # Refused even when no definition lists the tool: the list itself must not trust one.
        for tool in ("Bash", "bash", "BashOutput", "KillShell", "Task", "Agent", "Monitor", "SendMessage",
                     "PowerShell"):
            with self.subTest(tool=tool):
                self.trust("snape", ["Read", "Grep", "Glob", tool])
                self.assertRefused(self.check_snape, f"{self.trusted_file('snape')}: {tool} can run commands")

    def test_a_trusted_list_naming_anything_but_plain_safe_tools_is_refused(self):
        for tool, reason in (("Frobnicate", "Frobnicate is not a built-in tool known to run nothing"),
                             ("mcp__<chat-mcp>__*", "is not a plain tool name"),
                             ("mcp__<chat-mcp>", "is not a plain tool name"),
                             ("Bash(git status)", "is not a plain tool name"),
                             (3, "3 is not a plain tool name")):
            with self.subTest(tool=tool):
                self.trust("snape", ["Read", tool])
                self.assertRefused(self.check_snape, reason)

    def test_a_missing_or_malformed_trusted_list_is_refused(self):
        path = self.trusted_file("snape")
        for text in (json.dumps({"tools": []}), json.dumps(["Read"]), json.dumps({"tools": "Read"}),
                     json.dumps({"tools": ["Read"], "extra": ["Bash"]})):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertRefused(self.check_snape, "must be an object whose only key, tools, lists at least one tool")
        path.write_text("{not json")
        self.assertRefused(self.check_snape, f"{path} could not be read as JSON")
        path.unlink()
        self.assertRefused(self.check_snape, f"{path} is missing, so no tool is trusted for snape")

    def test_a_trusted_list_must_be_a_private_plain_file(self):
        # A linked, shared or loosely writable list could add a pane-input tool, so each is refused.
        tool = "mcp__herdr__pane_run"
        self.snape.write_text(definition("snape", f"tools: Read, {tool}"))
        outside = self.house / "outside"
        outside.mkdir()
        (outside / agent_gate.LIVE_TOOLS_FILE).write_text(json.dumps({"tools": ["Read", tool]}))
        path = self.trusted_file("snape")
        desk = path.parent

        path.unlink()
        path.symlink_to(outside / agent_gate.LIVE_TOOLS_FILE)
        self.assertRefused(self.check_snape, "trusted tool list is a symlink or cannot be opened")
        path.unlink()

        os.link(outside / agent_gate.LIVE_TOOLS_FILE, path)
        self.assertRefused(self.check_snape, "trusted tool list has more than one hard link")
        path.unlink()

        self.trust("snape", ["Read", tool])
        path.chmod(0o666)
        self.assertRefused(self.check_snape, "trusted tool list is group or world writable")
        path.chmod(0o644)
        desk.chmod(0o777)
        self.assertRefused(self.check_snape, "snape is group or world writable")
        desk.chmod(0o755)
        self.check_snape()  # the same list, held privately in the office, is trusted

        shutil.rmtree(desk)
        desk.symlink_to(outside)
        self.assertRefused(self.check_snape, "snape is not a plain directory")
        desk.unlink()
        (self.office / "desks").rename(outside / "desks")
        (self.office / "desks").symlink_to(outside / "desks")
        self.assertRefused(self.check_snape, "desks is not a plain directory")

    def test_the_office_path_may_hold_no_link(self):
        linked = self.house / "linked-office"
        linked.symlink_to(self.office)
        self.assertRefused(lambda: self.check_snape(office=linked), "is not trusted")

    def test_an_agent_name_cannot_leave_the_desks_folder(self):
        for agent in ("../snape", "Snape", "", "snape/x"):
            with self.subTest(agent=agent):
                with self.assertRaises(agent_gate.Refused):
                    agent_gate.trusted_tools(agent, self.office)

    def test_the_kit_lists_are_safe_and_use_placeholders(self):
        placeholder = re.compile(r"mcp__<(warehouse|observability|chat)-mcp>__[a-z_]+")
        for agent in ("snape", "mcgonagall"):
            with self.subTest(agent=agent):
                tools = agent_gate.trusted_tools(agent, ROOT)
                self.assertIn("ToolSearch", tools)
                for tool in tools:
                    self.assertTrue(tool in agent_gate.SAFE_BUILTINS or placeholder.fullmatch(tool), tool)


class SettingsTests(GateCase):
    def test_the_castle_settings_must_still_name_mcgonagall(self):
        self.settings.write_text(json.dumps({"agent": "builder"}))
        self.assertRefused(self.check_mcgonagall, "no longer makes mcgonagall the default agent")
        self.settings.write_text("{not json")
        self.assertRefused(self.check_mcgonagall, "could not be read as JSON")

    def test_local_settings_may_not_name_another_agent(self):
        local = self.settings.with_name("settings.local.json")
        local.write_text(json.dumps({"permissions": {}}))
        self.check_mcgonagall()
        local.write_text(json.dumps({"agent": "mcgonagall"}))
        self.check_mcgonagall()
        local.write_text(json.dumps({"agent": "builder"}))
        self.assertRefused(self.check_mcgonagall, "makes builder the default agent")


class MainTests(GateCase):
    def run_main(self, *args) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = agent_gate.main([str(arg) for arg in args])
        return code, out.getvalue()

    def test_a_pass_prints_nothing_and_a_refusal_one_line(self):
        args = ("snape", self.snape, self.documents, self.user_agents)
        self.assertEqual(self.run_main(*args), (0, ""))
        self.snape.write_text(definition("snape", "tools: Read, Bash"))
        code, out = self.run_main(*args)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("Bash can run commands", out)

    def test_bad_arguments(self):
        self.assertEqual(self.run_main("snape", self.snape)[0], 2)
        self.assertEqual(self.run_main("snape", "snape.md", self.documents, self.user_agents)[0], 2)


if __name__ == "__main__":
    unittest.main()
