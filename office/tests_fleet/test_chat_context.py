"""Repo context for a chat review: the PR head's text files in the folders the PR touches, read by the job with gh
(never by the model) and put under context/ in the turn's work folder as untrusted data, for the reviewer to search
with read-only tools. gh and every turn are fakes here (tests_fleet.test_chat_review.ChatCase): no call leaves the
machine and no model runs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from hogwarts.errors import StoreError

from fleet import chat_context, chat_review, config, gitops, run_desk
from fleet.safefs import FleetError
from tests_fleet.test_chat_review import (HEAD, MOODY_APP_USER, SUMMARY, ChatCase, block, changed_file, deleted_file,
                                          msg)

CONTEXT_MARK = "context-file-text-that-must-stay-out-of-the-log"


class ContextCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.diff = deleted_file("app/core/old/thing.py", ["def gone(): pass"]) + changed_file("app/core/main.py")

    def review(self) -> dict:
        self.deliver(msg())
        self.run_pass()
        [turn] = self.turns
        return turn


class ReadShapeTests(ContextCase):
    def test_the_tree_and_blob_reads_pass_the_guard(self):
        tree = gitops.tree_read_argv("acme/web-app", HEAD)
        blob = gitops.blob_read_argv("acme/web-app", "c" * 40)
        self.assertEqual(tree, [config.GH_BIN, "api", f"repos/acme/web-app/git/trees/{HEAD}?recursive=1"])
        self.assertEqual(blob, [config.GH_BIN, "api", f"repos/acme/web-app/git/blobs/{'c' * 40}"])
        self.assertEqual((gitops.check_read_argv(tree), gitops.check_read_argv(blob)), ("tree", "blob"))

    def test_any_other_shape_is_refused(self):
        base = f"repos/acme/web-app/git/trees/{HEAD}"
        refused = [
            [config.GH_BIN, "api", base],
            [config.GH_BIN, "api", base + "?recursive=0"],
            [config.GH_BIN, "api", base + "?recursive=1&per_page=1"],
            [config.GH_BIN, "api", f"repos/acme/web-app/git/trees/{HEAD[:12]}?recursive=1"],
            [config.GH_BIN, "api", "-X", "POST", base + "?recursive=1"],
            [config.GH_BIN, "api", base + "?recursive=1", "--method", "DELETE"],
            [config.GH_BIN, "api", "-H", gitops.DIFF_ACCEPT, f"repos/acme/web-app/git/blobs/{HEAD}"],
            [config.GH_BIN, "api", f"repos/acme/web-app/git/blobs/{HEAD}/"],
            [config.GH_BIN, "api", f"repos/acme/web-app/git/blobs/{HEAD}?x=1"],
            [config.GH_BIN, "api", "repos/acme/web-app/git/refs/heads/main"],
            [config.GH_BIN, "api", "repos/acme/web-app/contents/app/core/main.py"],
            [config.GH_BIN, "api", f"repos/acme/../git/blobs/{HEAD}"],
            ["/bin/sh", "api", base + "?recursive=1"],
        ]
        for argv in refused:
            with self.subTest(argv=argv[2:]), self.assertRaises((FleetError, StoreError)):
                gitops.check_read_argv(argv)


class ContextTests(ContextCase):
    def test_text_files_in_the_touched_folders_reach_the_turn_under_context(self):
        self.repo_file("app/core/util.py", b"def used():\n    return gone()\n")
        self.repo_file("app/core/deep/more.py", b"from app.core.old.thing import gone\n", mode="100755")
        self.repo_file("app/other.py", b"outside the touched folders\n")
        self.repo_file("docs/readme.md", b"outside too\n")
        self.repo_file("app/core/logo.png", b"\x89PNG not read")
        self.repo_file("app/core/data.txt", b"has a NUL \x00 byte")
        self.repo_file("app/core/latin.txt", b"caf\xe9 is not UTF-8")
        self.repo_file("app/core/big.py", b"x" * (config.CHAT_REVIEW_CONTEXT_FILE_MAX_BYTES + 1))
        self.repo_file("app/core/link.py", b"util.py", mode="120000")  # a symlink
        self.tree["tree"].append({"path": "app/core/vendored", "mode": "160000", "type": "commit", "sha": "d" * 40})
        self.tree["tree"].append({"path": "app/core/deep", "mode": "040000", "type": "tree", "sha": "e" * 40})
        turn = self.review()
        self.assertEqual(turn["context"], {"app/core/util.py": b"def used():\n    return gone()\n",
                                           "app/core/deep/more.py": b"from app.core.old.thing import gone\n"})
        self.assertIn(chat_context.FOLDER, turn["files"])
        self.assertFalse(Path(turn["cwd"]).exists())  # the work folder and the context in it are removed after
        head = turn["input"].split("<<<UNTRUSTED PR DATA")[0]
        self.assertIn(f"REPO CONTEXT context/ in your working directory holds 2 text files of the repo at the PR's"
                      f" head {HEAD}", head)
        self.assertNotIn("NOTE", head)
        self.assertIn("context/", turn["brief"])
        self.assertEqual(self.reads.count("blob"), 4)  # the png, the big file, the link and the submodule never read
        [line] = [line for line in self.log_lines() if line["event"] == "context"]
        self.assertEqual((line["files"], line["skipped"], line["failed"]), (2, 6, 0))

    def test_the_folders_are_the_touched_paths_cut_to_two_levels(self):
        self.assertEqual(chat_context.folders({"a/b/c/d.x", "a/b/e.py", "f/g.py", "f/h/i/j.py", "k/l/m.py"}),
                         ["a/b", "f", "k/l"])
        self.assertEqual(chat_context.folders({"x.py", "a/b/c.py"}), [""])  # a root file covers the whole repo
        self.assertEqual(chat_context.folders({"../a/b.py", "/etc/c/d"}), [])

    def test_the_count_and_size_caps_hold_and_are_noted(self):
        for index in range(5):
            self.repo_file(f"app/core/f{index}.py", b"y" * 100)
        self.patch(config, "CHAT_REVIEW_CONTEXT_MAX_FILES", 3)
        turn = self.review()
        self.assertEqual(sorted(turn["context"]), ["app/core/f0.py", "app/core/f1.py", "app/core/f2.py"])
        self.assertIn("NOTE the repo context is not every file in those folders: it reached the file count cap.",
                      turn["input"])
        self.turns.clear()
        self.patch(config, "CHAT_REVIEW_CONTEXT_MAX_FILES", 400)
        self.patch(config, "CHAT_REVIEW_CONTEXT_MAX_BYTES", 250)
        self.deliver(msg(message="m2"))
        self.run_pass()
        [turn] = self.turns
        self.assertEqual(sorted(turn["context"]), ["app/core/f0.py", "app/core/f1.py"])
        self.assertIn("it reached the size cap.", turn["input"])

    def test_a_truncated_tree_is_noted(self):
        self.repo_file("app/core/util.py", b"ok\n")
        self.tree["truncated"] = True
        turn = self.review()
        self.assertEqual(list(turn["context"]), ["app/core/util.py"])
        self.assertIn("NOTE the repo context is not every file in those folders: GitHub cut its tree short.",
                      turn["input"])

    def test_path_traversal_and_odd_names_are_refused(self):
        self.diff += changed_file("README.md")  # a root file: the whole tree is covered
        outside = Path(config.CHAT_REVIEW_ROOT).parent
        for path in ("../escape.py", "app/core/../../../escape.py", "/tmp/escape.py", "app/core//x.py",
                     "app/core/./x.py", "app/core/sp ace.py", "app/core/new\nline.py", "app/core/" + "d/" * 40 + "x"):
            self.repo_file(path, b"must not land anywhere\n")
        self.repo_file("app/core/kept.py", b"kept\n")
        turn = self.review()
        self.assertEqual(list(turn["context"]), ["app/core/kept.py"])
        self.assertFalse((outside / "escape.py").exists())
        self.assertEqual(self.reads.count("blob"), 1)  # no refused path is even read
        for path in ("../a", "/a", "a//b", "a/./b", "a/../b", "", "a/b c", "a\\b"):
            with self.subTest(path=path):
                self.assertFalse(chat_context.plain_path(path))
        self.assertTrue(chat_context.plain_path("a/b-c/d_e.f"))

    def test_the_work_folder_refuses_a_path_that_is_not_plain(self):
        for path in ("../escape.py", "a/../../escape.py", "/abs.py"):
            with self.subTest(path=path), self.assertRaises(FleetError):
                with chat_review._workdir("f" * 32, "input", context={path: b"x"}):
                    pass
        self.assertFalse((Path(config.CHAT_REVIEW_ROOT) / "escape.py").exists())
        self.assertFalse((Path(config.CHAT_REVIEW_ROOT).parent / "escape.py").exists())
        self.assertEqual(os.listdir(config.CHAT_REVIEW_ROOT), [])

    def test_past_the_time_limit_no_blob_is_read_and_it_is_noted(self):
        self.patch(config, "CHAT_REVIEW_CONTEXT_SECONDS", 0)
        self.repo_file("app/core/util.py", b"ok\n")
        turn = self.review()
        self.assertEqual((turn["context"], self.reads.count("blob")), ({}, 0))
        self.assertIn("it reached the time limit.", turn["input"])
        self.assertEqual(self.records()[0]["outcome"], "reviewed")

    def test_two_paths_one_disk_name_never_replace_each_other(self):
        self.repo_file("app/core/Notes.md", b"first\n")
        self.repo_file("app/core/NOTES.md", b"second\n")
        probe = Path(config.CHAT_REVIEW_ROOT).parent / "Case-Probe"
        probe.write_text("x")
        folds = (probe.parent / "case-probe").exists()  # a case-insensitive disk
        probe.unlink()
        turn = self.review()
        if folds:
            self.assertEqual(list(turn["context"].values()), [b"first\n"])
            self.assertIn("NOTE 1 files of the repo context could not be written", turn["input"])
        else:
            self.assertEqual(sorted(turn["context"].values()), [b"first\n", b"second\n"])

    def test_a_blob_that_is_not_the_one_its_sha_names_is_left_out(self):
        sha = self.repo_file("app/core/swapped.py", b"the real text\n")
        self.blobs[sha] = b"other text of another length\n"
        self.repo_file("app/core/util.py", b"ok\n")
        turn = self.review()
        self.assertEqual(list(turn["context"]), ["app/core/util.py"])
        self.assertIn("NOTE 1 files of the repo context could not be read.", turn["input"])

    def test_a_failed_context_fetch_still_reviews(self):
        self.tree = FleetError("gh failed: HTTP 404")
        turn = self.review()
        self.assertEqual(turn["context"], {})
        self.assertNotIn(chat_context.FOLDER, turn["files"])
        self.assertIn("NOTE the repo context could not be read: review from the diff alone.", turn["input"])
        [record] = self.records()
        self.assertEqual((record["outcome"], record["summary"]), ("reviewed", SUMMARY))
        [line] = [line for line in self.log_lines() if line["event"] == "context"]
        self.assertEqual(line["failed"], 1)

    def test_a_tree_that_is_not_json_still_reviews(self):
        self.tree = {"tree": "not a list"}
        turn = self.review()
        self.assertIn("NOTE the repo context could not be read", turn["input"])
        self.assertEqual(self.records()[0]["outcome"], "reviewed")

    def test_the_switch_off_reads_no_tree(self):
        self.patch(config, "CHAT_REVIEW_REPO_CONTEXT", False)
        self.repo_file("app/core/util.py", b"ok\n")
        turn = self.review()
        self.assertEqual((self.reads, turn["context"]), (["pr", "diff"], {}))
        self.assertIn("NOTE no repo context this time", turn["input"])

    def test_nothing_fetched_reaches_the_log_or_the_office(self):
        self.repo_file(f"app/core/{CONTEXT_MARK}.py", f"secret_value = '{CONTEXT_MARK}'\n".encode("utf-8"))
        turn = self.review()
        self.assertEqual(len(turn["context"]), 1)
        log_text = (self.office / "logs" / chat_review.LOG).read_text()
        self.assertNotIn(CONTEXT_MARK, log_text)
        self.assertNotIn("app/core", log_text)
        self.assertNotIn(CONTEXT_MARK, self.office_text())
        [line] = [line for line in self.log_lines() if line["event"] == "context"]
        self.assertEqual(set(line), {"at", "event", "request", "app", "files", "bytes", "skipped", "failed"})

    def test_a_finding_that_repeats_the_context_is_withheld_and_one_naming_a_context_file_is_kept(self):
        long_path = "app/core/reporting/exporters/csv_exporter_still_used.py"
        copied = "the_exporter_registry.register(LegacyCsvExporter, priority=100)"
        self.repo_file(long_path, f"# {long_path}\n{copied}\n".encode("utf-8"))  # its path is in the context too
        self.answer = ("ok", block(blocking=(f"{long_path}:1 | it still registers the removed exporter | keep it",),
                                   non_blocking=(f"app/core/main.py:1 | it says {copied}",)))
        self.review()
        [record] = self.records()
        self.assertEqual(record["blocking"],
                         [f"B1 {long_path}:1 | it still registers the removed exporter | keep it"])
        self.assertEqual(record["non_blocking"], [f"N1 app/core/main.py:1 | {chat_review.WITHHELD}"])


class ToolTests(ContextCase):
    def test_hermione_has_read_grep_and_glob_only_under_the_report_only_settings(self):
        turn = self.review()
        argv = turn["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[:3], [config.CLAUDE_BIN, "-p", "--restricted"])
        settings = (self.office / "desks" / "hermione" / run_desk.CHAT_REVIEW_SETTINGS_FILE).read_bytes()
        run_desk.check_report_settings(settings, "hermione")
        self.assertEqual(run_desk.check_report_tools(run_desk.CHAT_REVIEW_TOOLS), "Read,Grep,Glob")
        for tools in ("Read,Bash", "Read,Edit", "Read,Grep,Glob,Write", "", "Read,Read", "Read, Grep", None,
                      "Read,WebFetch"):
            with self.subTest(tools=tools), self.assertRaises(FleetError):
                run_desk.check_report_tools(tools)
        data = json.loads(settings)
        for denied in ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "Task", "NotebookEdit"):
            self.assertIn(denied, data["permissions"]["deny"])

    def test_a_broader_tool_list_is_refused_before_any_turn(self):
        self.patch(run_desk, "CHAT_REVIEW_TOOLS", "Read,Grep,Glob,Bash")
        self.deliver(msg())
        self.run_pass()
        self.assertEqual((self.turns, self.google.messages), ([], []))
        self.assertEqual(self.records()[0]["state"], "queued")
        self.assertIn("step-failed", [line["event"] for line in self.log_lines()])

    def test_moodys_codex_turn_keeps_its_read_only_profile_and_gets_the_context_too(self):
        self.second_app()
        self.repo_file("app/core/util.py", b"def used(): pass\n")
        self.answer = ("ok", block())
        self.deliver(msg(app=MOODY_APP_USER))
        self.run_pass()
        [turn] = self.turns
        self.assertEqual(turn["family"], "codex")
        argv = turn["argv"]
        permissions = run_desk.chat_review_permissions("moody")
        at = argv.index(permissions[1])
        self.assertEqual(argv[at - 1:at + 3], permissions)
        table = permissions[1]
        self.assertIn('":workspace_roots"={"."="read"}', table)
        self.assertIn("network={enabled=false}", table)
        self.assertNotIn('"write"', table)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(turn["context"], {"app/core/util.py": b"def used(): pass\n"})
        self.assertIn("context/", turn["brief"])
