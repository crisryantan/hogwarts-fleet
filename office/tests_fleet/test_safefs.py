"""safefs.open_root under a sandbox that refuses to open ancestors it lets a process see, such as Codex's seatbelt."""
from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import temp_dir

from fleet import config, safefs

OFFICE = Path(__file__).resolve().parents[1]
REAL_OPEN = os.open
REAL_LSTAT = os.lstat


class DeniedAncestorTests(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        os.mkdir(self.root / "real")
        os.mkdir(self.root / "real" / "x")
        os.symlink("real", self.root / "alias")
        self.opened = []

    def deny(self, *names):
        """Patch os.open so a folder-relative open of any of names fails the way the sandbox fails it, with EPERM."""
        def fake_open(path, flags, *args, dir_fd=None, **kwargs):
            self.opened.append(path)
            if dir_fd is not None and path in names:
                raise PermissionError(errno.EPERM, "Operation not permitted", path)
            return REAL_OPEN(path, flags, *args, dir_fd=dir_fd, **kwargs)
        patcher = mock.patch.object(safefs.os, "open", side_effect=fake_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def first(self) -> str:
        return str(self.root).strip("/").split("/")[0]

    def test_walks_past_an_ancestor_it_may_not_open(self):
        self.deny(self.first())
        fd = safefs.open_root(str(self.root / "real" / "x"))
        try:
            self.assertTrue(os.path.samestat(os.fstat(fd), os.stat(self.root / "real" / "x")))
        finally:
            os.close(fd)
        self.assertIn(str(self.root / "real" / "x"), self.opened)

    def test_refuses_a_symlinked_ancestor_below_a_denied_one_without_following_it(self):
        self.deny(self.first())
        with self.assertRaisesRegex(safefs.Unsafe, "not a plain directory"):
            safefs.open_root(str(self.root / "alias" / "x"))
        self.assertNotIn(str(self.root / "alias" / "x"), self.opened)

    def test_refuses_a_denied_component_that_is_itself_a_link(self):
        self.deny("alias")
        with self.assertRaisesRegex(safefs.Unsafe, "not a plain directory"):
            safefs.open_root(str(self.root / "alias" / "x"))
        self.assertNotIn(str(self.root / "alias" / "x"), self.opened)

    def test_refuses_a_link_swapped_in_after_the_check(self):
        self.deny(self.first())
        alias, real = str(self.root / "alias"), str(self.root / "real")
        with mock.patch.object(safefs.os, "lstat", side_effect=lambda p: REAL_LSTAT(real if p == alias else p)):
            with self.assertRaisesRegex(safefs.Unsafe, "reached through a link"):
                safefs.open_root(str(self.root / "alias" / "x"))

    def test_a_link_the_walk_refused_is_never_opened_whole(self):
        # Only a component refused with EPERM is retried whole; under a real sandbox that is an ancestor, never alias.
        retried, real_whole = [], safefs._open_whole
        def spy(path, parts, depth):
            retried.append(parts[depth])
            return real_whole(path, parts, depth)
        with mock.patch.object(safefs, "_open_whole", side_effect=spy):
            with self.assertRaisesRegex(safefs.Unsafe, "not a plain directory"):
                safefs.open_root(str(self.root / "alias" / "x"))
        self.assertNotIn("alias", retried)

    def test_a_missing_folder_past_a_denied_ancestor_is_missing(self):
        self.deny(self.first())
        with self.assertRaises(safefs.Missing):
            safefs.open_root(str(self.root / "real" / "gone"))

    def test_refuses_when_the_opened_path_cannot_be_read_back(self):
        self.deny(self.first())
        with mock.patch.object(safefs, "fcntl", types.SimpleNamespace(fcntl=fcntl.fcntl)):
            with self.assertRaisesRegex(safefs.Unsafe, "one folder at a time"):
                safefs.open_root(str(self.root / "real" / "x"))

    def test_the_folder_must_still_be_ours(self):
        self.deny(self.first())
        os.chmod(self.root / "real" / "x", 0o777)
        with self.assertRaisesRegex(safefs.Unsafe, "group or world writable"):
            safefs.open_root(str(self.root / "real" / "x"))


PROBE = """
import os, sys
sys.path.insert(0, sys.argv[1])
from fleet import safefs
root = sys.argv[2]
try:
    os.close(os.open("/" + root.strip("/").split("/")[0], safefs.DIR_FLAGS))
    print("ancestor opened")
except PermissionError:
    print("ancestor denied")
os.close(safefs.open_root(root + "/real/x"))
print("root opened")
try:
    safefs.open_root(root + "/alias/x")
    print("link followed")
except safefs.Unsafe:
    print("link refused")
"""


@unittest.skipUnless(sys.platform == "darwin" and os.access(config.CODEX_BIN, os.X_OK), "needs the Codex sandbox")
class CodexSandboxTests(unittest.TestCase):
    def test_open_root_works_inside_the_codex_sandbox(self):
        root = temp_dir(self)
        os.mkdir(root / "home", 0o700)
        os.mkdir(root / "real")
        os.mkdir(root / "real" / "x")
        os.symlink("real", root / "alias")
        reads = [":minimal", *config.CODEX_EXTRA_READS, str(OFFICE)]
        entries = [f'"{path}"="read"' for path in reads] + [f'"{root}"="write"']
        table = "{filesystem={" + ", ".join(entries) + "}, network={enabled=false}}"
        argv = [config.CODEX_BIN, "sandbox", "-c", f"permissions.safefs-test={table}", "-P", "safefs-test", "--"]
        env = {"HOME": str(root / "home"), "PATH": config.CHILD_PATH, "LANG": "en_US.UTF-8"}
        started = subprocess.run([*argv, "/usr/bin/true"], env=env, capture_output=True, timeout=60)
        if started.returncode != 0:
            self.skipTest("the Codex sandbox cannot start here, as inside another sandbox")
        result = subprocess.run([*argv, sys.executable, "-I", "-B", "-c", PROBE, str(OFFICE), str(root)],
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual([line for line in result.stdout.splitlines() if line],
                         ["ancestor denied", "root opened", "link refused"])


if __name__ == "__main__":
    unittest.main()
