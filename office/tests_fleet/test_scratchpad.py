from __future__ import annotations

import os
import stat
from unittest import mock

from tests.support import NOW

from fleet import config, safefs, scratchpad
from fleet.hooks import pre_compact, session_start
from tests_fleet.support import SCRATCHPAD, FleetCase

SESSION = "0b6f8c1e-1111-4222-8333-944455556666"
HEADER = "# Scratchpad\n\n## Now\n- the live thread\n\n## Notes\n- flaky: web-app lint step\n\n## Checkpoint\n"


def block(number: int, body: str = "") -> str:
    return (f"\n### Checkpoint 2027-01-0{number} 09:00\n- Task: tk_000000000000000{number}\n{body}"
            f"- Next: step {number}\n")


class RotationCase(FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.pad = self.castle / "desks" / "mcgonagall" / "scratchpad.md"
        self.archive_dir = self.castle / "desks" / "mcgonagall" / config.SCRATCHPAD_ARCHIVE_DIR
        self.archive = self.archive_dir / "2027-01.md"

    def write_pad(self, text: str) -> None:
        self.write_file(self.pad, text)

    def rotate(self, key=None) -> dict:
        return scratchpad.rotate("mcgonagall", NOW, key)

    def every_block_kept(self, blocks: list) -> None:
        """Each block is in the live file or the archive, so none was lost."""
        live = self.pad.read_text()
        archived = self.archive.read_text() if self.archive.exists() else ""
        for text in blocks:
            self.assertTrue(text.strip() in live or text.strip() in archived, text)


class SegmentTests(RotationCase):
    def test_blocks_end_at_a_heading_at_their_level_and_skip_code_fences(self):
        text = (HEADER + block(1, "#### Findings\n- one\n```\n### Checkpoint inside a fence\n```\n")
                + "\n## Later notes\n- kept\n" + block(2))
        runs = scratchpad.segments(text.encode())
        self.assertEqual([run[0] for run in runs], ["keep", "keep", "checkpoint", "keep", "checkpoint"])
        first = runs[2]
        self.assertIn(b"#### Findings", text.encode()[first[2]:first[3]])
        self.assertIn(b"inside a fence", text.encode()[first[2]:first[3]])
        self.assertEqual(b"".join(text.encode()[run[2]:run[3]] for run in runs), text.encode())

    def test_a_fence_closes_only_on_its_own_marker(self):
        text = "## Checkpoint\n" + block(1, "````\n~~~\n### Checkpoint fenced\n```\nstill fenced\n````\n")
        runs = scratchpad.segments(text.encode())
        self.assertEqual([run[0] for run in runs], ["keep", "checkpoint"])

    def test_a_bare_checkpoint_heading_is_the_section_not_a_block(self):
        runs = scratchpad.segments(SCRATCHPAD.encode())
        self.assertEqual({run[0] for run in runs}, {"keep"})


class RotateTests(RotationCase):
    def test_keeps_the_header_notes_and_latest_block_and_archives_the_rest(self):
        blocks = [block(1), block(2), block(3)]
        self.write_pad(HEADER + "".join(blocks) + "\n## Later notes\n- still here\n")
        result = self.rotate()
        self.assertEqual((result["archived"], result["warning"]), (2, None))
        live = self.pad.read_text()
        self.assertEqual(live, HEADER + block(3) + "\n## Later notes\n- still here\n")
        archived = self.archive.read_text()
        self.assertIn("## Archived from scratchpad.md at 2027-01-15 08:00 UTC", archived)
        self.assertIn(block(1).strip(), archived)
        self.assertIn(block(2).strip(), archived)
        self.assertNotIn("step 3", archived)
        self.assertEqual(stat.S_IMODE(os.lstat(self.pad).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.archive).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.archive_dir).st_mode), 0o700)
        self.write_pad(self.pad.read_text() + block(4))
        self.assertEqual(self.rotate()["archived"], 1)
        self.assertIn(block(3).strip(), self.archive.read_text())
        self.assertIn(block(1).strip(), self.archive.read_text())

    def test_one_block_or_none_is_left_alone(self):
        for text in (SCRATCHPAD, HEADER + block(1)):
            with self.subTest(text=text[-20:]):
                self.write_pad(text)
                self.assertEqual(self.rotate()["archived"], 0)
                self.assertEqual(self.pad.read_text(), text)
                self.assertFalse(self.archive_dir.exists())

    def test_a_latest_block_over_budget_is_kept_whole_with_one_warning(self):
        big = block(2, "- detail " + "x" * config.SCRATCHPAD_BUDGET_BYTES + "\n")
        self.write_pad(HEADER + block(1) + big)
        result = self.rotate()
        self.assertEqual(self.pad.read_text(), HEADER + big)
        self.assertIn("latest Checkpoint", result["warning"])
        self.assertIn("kept whole", result["warning"])
        self.assertNotIn("\n", result["warning"])

    def test_notes_over_budget_warn_to_move_durable_facts(self):
        notes = HEADER.replace("- flaky", "- note " + "y" * config.SCRATCHPAD_BUDGET_BYTES + "\n- flaky")
        self.write_pad(notes + block(1) + block(2))
        result = self.rotate()
        self.assertEqual(self.pad.read_text(), notes + block(2))
        self.assertIn("TASK.md or the memory store", result["warning"])

    def test_a_pad_rotates_into_its_own_archive_in_the_pads_folder(self):
        pads = self.castle / "desks" / "mcgonagall" / config.PADS_DIR
        pads.mkdir(mode=0o700)
        key = "tk_00000000000000aa"
        self.write_file(pads / f"{key}.md", f"# Pad {key}\n\n## Checkpoint\n" + block(1) + block(2))
        self.assertEqual(self.rotate(key)["archived"], 1)
        self.assertEqual((pads / f"{key}.md").read_text(), f"# Pad {key}\n\n## Checkpoint\n" + block(2))
        self.assertIn("step 1", (pads / config.SCRATCHPAD_ARCHIVE_DIR / f"{key}-2027-01.md").read_text())
        self.assertFalse(self.archive_dir.exists())

    def test_a_missing_file_or_desk_folder_has_nothing_to_rotate(self):
        os.unlink(self.pad)
        self.assertEqual(self.rotate()["archived"], 0)
        self.assertEqual(scratchpad.rotate("ryan-claude-1", NOW)["archived"], 0)
        self.assertFalse((self.castle / "desks" / "ryan-claude-1").exists())

    def test_a_large_file_streams_in_bounded_lines_and_keeps_its_notes(self):
        notes = HEADER.replace("- flaky", "- " + "n" * 5000 + "\n- flaky")
        fenced = block(1, "```\n### Checkpoint inside a fence\n" + "z" * 3000 + "\n```\n")
        self.write_pad(notes + fenced + "".join(block(1, "- filler " + "z" * 300 + "\n") for _ in range(20))
                       + "\n## Later notes\n- still here\n" + block(2))
        with mock.patch.object(config, "SCRATCHPAD_READ_MAX_BYTES", 1024):
            result = self.rotate()
        self.assertEqual(self.pad.read_text(), notes + "\n## Later notes\n- still here\n" + block(2))
        self.assertEqual(result["archived"], 21)
        archived = self.archive.read_text()
        self.assertIn("inside a fence", archived)
        self.assertNotIn("step 2", archived)

    def test_a_file_over_the_rotation_cap_is_left_alone(self):
        text = HEADER + block(1) + block(2) + "- note " + "q" * 5000 + "\n"
        self.write_pad(text)
        with mock.patch.object(config, "SCRATCHPAD_ROTATE_MAX_BYTES", 2048):
            result = self.rotate()
        self.assertEqual(self.pad.read_text(), text)
        self.assertIn("left as it is", result["warning"])
        self.assertFalse(self.archive_dir.exists())


class SafetyTests(RotationCase):
    def test_a_crash_after_the_archive_loses_no_block_and_the_next_run_finishes(self):
        blocks = [block(1), block(2), block(3)]
        text = HEADER + "".join(blocks)
        self.write_pad(text)
        with mock.patch.object(safefs, "move", side_effect=OSError("cut off")):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual(self.pad.read_text(), text)
        self.every_block_kept(blocks)
        self.rotate()
        self.assertEqual(self.pad.read_text(), HEADER + block(3))
        self.every_block_kept(blocks)
        self.assertEqual(self.archive.read_text().count("step 1"), 2)

    def test_a_crash_while_archiving_leaves_the_live_file_alone(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        with mock.patch.object(scratchpad.os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual(self.pad.read_text(), text)
        self.assertEqual([name for name in os.listdir(self.pad.parent) if name.endswith(".tmp")], [])

    def test_a_short_read_is_refused_and_never_replaces_the_file(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real = os.pread
        with mock.patch.object(scratchpad.os, "pread", side_effect=lambda fd, n, at: real(fd, n, at)[:40] or b""):
            self.assertEqual(self.rotate()["archived"], 1)  # short chunks are read on until the end
        self.write_pad(text)
        with mock.patch.object(scratchpad.os, "pread",
                               side_effect=lambda fd, n, at: real(fd, min(n, 60), at) if at < 60 else b""):
            with self.assertRaises(safefs.Unsafe):
                self.rotate()
        self.assertEqual(self.pad.read_text(), text)

    def recovered(self) -> list:
        return sorted(path for path in self.archive_dir.iterdir() if path.name.startswith("recovered-"))

    def test_a_file_written_a_moment_ago_waits_for_the_next_rotation(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        result = scratchpad.rotate("mcgonagall", int(os.stat(self.pad).st_mtime) + 10)
        self.assertEqual((result["archived"], result["warning"]), (0, None))
        self.assertEqual(self.pad.read_text(), text)
        self.assertFalse(self.archive_dir.exists())

    def test_a_file_changed_during_the_rotation_is_left_as_it_is(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real = scratchpad._append_archive

        def desk_writes(*args):
            real(*args)
            with open(self.pad, "a") as handle:
                handle.write(block(3))

        with mock.patch.object(scratchpad, "_append_archive", side_effect=desk_writes):
            result = self.rotate()
        self.assertEqual((result["archived"], result["warning"]), (0, None))
        self.assertEqual(self.pad.read_text(), text + block(3))

    def test_a_write_while_the_temp_file_is_made_is_never_overwritten(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real_replace, real_write = scratchpad._replace, safefs.write_all
        wrote = []

        def desk_writes(fd, data):
            real_write(fd, data)
            if not wrote:
                wrote.append(True)
                with open(self.pad, "a") as handle:
                    handle.write(block(3))

        def replace(*args):  # the desk writes while the temp file is being written
            with mock.patch.object(safefs, "write_all", side_effect=desk_writes):
                return real_replace(*args)

        with mock.patch.object(scratchpad, "_replace", side_effect=replace):
            result = self.rotate()
        self.assertIsNone(result["warning"])
        self.assertEqual(self.pad.read_text(), text + block(3))
        self.assertEqual([name for name in os.listdir(self.pad.parent) if name.endswith(".tmp")], [])

    def test_a_write_between_the_last_check_and_the_rename_is_recovered_whole(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real_move = safefs.move

        def desk_writes_then_move(*args):
            with open(self.pad, "a") as handle:  # still the old file: the rename has not happened
                handle.write(block(3))
            real_move(*args)

        with mock.patch.object(safefs, "move", side_effect=desk_writes_then_move):
            result = self.rotate()
        self.assertEqual(self.pad.read_text(), HEADER + block(2))
        [recovered] = self.recovered()
        self.assertEqual(recovered.read_text(), text + block(3))
        self.assertEqual(recovered.name, "recovered-20270115T080000.md")
        self.assertEqual(stat.S_IMODE(os.lstat(recovered).st_mode), 0o600)
        self.assertIn(recovered.name, result["warning"])
        self.assertNotIn("\n", result["warning"])

    def test_a_write_between_the_rename_and_the_look_after_it_is_recovered_whole(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real_move = safefs.move
        with open(self.pad, "a") as handle:  # a desk that opened the file before the rotation

            def move_then_desk_writes(*args):
                real_move(*args)
                handle.write(block(3))
                handle.flush()

            with mock.patch.object(safefs, "move", side_effect=move_then_desk_writes):
                result = self.rotate()
        self.assertEqual(self.pad.read_text(), HEADER + block(2))
        [recovered] = self.recovered()
        self.assertEqual(recovered.read_text(), text + block(3))
        self.assertIn(recovered.name, result["warning"])

    def test_a_late_write_past_the_cap_is_recovered_cut_with_a_note(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real_move = safefs.move
        with open(self.pad, "a") as handle, \
                mock.patch.object(config, "SCRATCHPAD_ROTATE_MAX_BYTES", len(text) + 100):

            def move_then_desk_writes(*args):
                real_move(*args)
                handle.write("x" * 5000)
                handle.flush()

            with mock.patch.object(safefs, "move", side_effect=move_then_desk_writes):
                self.rotate()
        [recovered] = self.recovered()
        copied = recovered.read_text()
        self.assertTrue(copied.startswith(text))
        self.assertIn(f"(cut at {len(text) + 100} of {len(text) + 5000} bytes)", copied)

    def test_a_failure_after_the_rename_copies_the_old_file_before_letting_it_go(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        real_move, real_fstat = safefs.move, os.fstat
        moved = []

        def move(*args):
            real_move(*args)
            moved.append(True)

        def fstat(fd):
            if moved == [True]:  # the look after the rename fails once
                moved.append(False)
                raise OSError("disk gone")
            return real_fstat(fd)

        with mock.patch.object(safefs, "move", side_effect=move), \
                mock.patch.object(scratchpad.os, "fstat", side_effect=fstat), self.assertRaises(OSError):
            self.rotate()
        [recovered] = self.recovered()
        self.assertEqual(recovered.read_text(), text)
        self.assertEqual(self.pad.read_text(), HEADER + block(2))

    def test_a_held_lock_refuses_and_changes_nothing(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        with mock.patch.object(config, "SCRATCHPAD_LOCK_WAIT_SECONDS", 0), scratchpad.desk_lock("mcgonagall"):
            with self.assertRaises(safefs.Busy):
                self.rotate()
        self.assertEqual(self.pad.read_text(), text)
        self.assertEqual(self.rotate()["archived"], 1)

    def test_links_are_refused(self):
        text = HEADER + block(1) + block(2)
        target = self.write_file(self.tmp / "elsewhere.md", text)
        os.unlink(self.pad)
        os.symlink(target, self.pad)
        with self.assertRaises(safefs.Unsafe):
            self.rotate()
        os.unlink(self.pad)
        os.link(target, self.pad)
        with self.assertRaises(safefs.Unsafe):
            self.rotate()
        os.unlink(self.pad)
        self.write_pad(text)
        outside = self.tmp / "outside"
        outside.mkdir(mode=0o700)
        os.symlink(outside, self.archive_dir)
        with self.assertRaises(safefs.Unsafe):
            self.rotate()
        self.assertEqual(self.pad.read_text(), text)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(target.read_text(), text)

    def test_a_linked_archive_file_is_refused(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        self.archive_dir.mkdir(mode=0o700)
        target = self.write_file(self.tmp / "elsewhere.md", "keep me\n")
        os.symlink(target, self.archive)
        with self.assertRaises(safefs.Unsafe):
            self.rotate()
        self.assertEqual((self.pad.read_text(), target.read_text()), (text, "keep me\n"))


class HookTests(RotationCase):
    def hook_input(self, event: str, **fields) -> dict:
        return {"session_id": SESSION, "transcript_path": "", "cwd": str(self.castle), "hook_event_name": event,
                **fields}

    def test_session_start_rotates_mcgonagalls_scratchpad_before_the_digest(self):
        self.write_pad(HEADER + block(1) + block(2))
        code, out, err = self.run_hook(session_start, self.hook_input("SessionStart", source="startup",
                                                                      agent_type="mcgonagall"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pad.read_text(), HEADER + block(2))
        self.assertIn("step 1", self.archive.read_text())
        self.assertIn(f"(older ones are in {config.SCRATCHPAD_ARCHIVE_DIR}/)", out)

    def test_session_start_prints_one_warning_and_a_failure_never_stops_the_digest(self):
        self.write_pad(HEADER + block(1) + block(2, "x" * config.SCRATCHPAD_BUDGET_BYTES + "\n"))
        payload = self.hook_input("SessionStart", source="startup", agent_type="mcgonagall")
        code, out, _ = self.run_hook(session_start, payload)
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if "kept whole" in line]), 1)
        os.unlink(self.pad)
        os.symlink(self.tmp, self.pad)
        code, out, err = self.run_hook(session_start, payload)
        self.assertEqual(code, 0, err)
        self.assertIn("Scratchpad rotation skipped", out)
        self.assertIn("Memory pointers:", out)

    def test_ryans_own_session_and_a_session_that_carries_on_leave_the_scratchpad_alone(self):
        text = HEADER + block(1) + block(2)
        self.write_pad(text)
        code, _, err = self.run_hook(session_start, self.hook_input("SessionStart", source="startup"))
        self.assertEqual(code, 0, err)
        for source in session_start.NO_ROTATION_SOURCES:
            code, _, err = self.run_hook(session_start, self.hook_input("SessionStart", source=source,
                                                                        agent_type="mcgonagall"))
            self.assertEqual(code, 0, err)
        self.assertEqual(self.pad.read_text(), text)

    def test_pre_compact_rotates_then_appends_its_stub(self):
        self.write_pad(HEADER + block(1) + block(2))
        code, out, err = self.run_hook(pre_compact, self.hook_input("PreCompact", trigger="auto"))
        self.assertEqual(code, 0, err)
        live = self.pad.read_text()
        self.assertNotIn("step 1", live)
        self.assertIn("step 2", live)
        self.assertIn("(pre-compact, auto)", live)
        self.assertIn("step 1", self.archive.read_text())
        self.assertIn("Checkpoint stub added", out)
