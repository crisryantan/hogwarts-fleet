from __future__ import annotations

import unittest

from hogwarts import ids
from hogwarts.errors import ValidationError


class IdGenerationTests(unittest.TestCase):
    def test_new_ids_match_their_patterns(self):
        for kind in ("task", "request", "owl", "review"):
            with self.subTest(kind=kind):
                value = ids.new_id(kind)
                self.assertEqual(ids.check(kind, value), value)

    def test_new_ids_are_unique(self):
        self.assertEqual(len({ids.new_id("task") for _ in range(200)}), 200)


class PatternTests(unittest.TestCase):
    CASES = {
        "task": (["tk_0123456789abcdef"],
                 ["tk_0123456789ABCDEF", "tk_0123", "rq_0123456789abcdef", "tk_0123456789abcdef0"]),
        "request": (["rq_0123456789abcdef"], ["rq_xyz", "tk_0123456789abcdef"]),
        "owl": (["owl_0123456789abcdef"], ["owl_0123456789abcde", "ow_0123456789abcdef"]),
        "review": (["rv_0123456789abcdef"], ["rv_0123456789abcdeg"]),
        "desk": (["ab", "ryan-claude", "a" + "b" * 31], ["a", "Ab", "1ab", "a_b", "a" + "b" * 32, "a b"]),
        "sha": (["0123456789abcdef0123456789abcdef01234567"], ["0123456789ABCDEF0123456789abcdef01234567", "abc"]),
        "repo": (["acme/web-app", "A.b_c/d-e.f"],
                 ["acme", "acme/web/app", "/web", "acme/", "acme/web app", "../etc", "acme/.."]),
        "session": (["abcdefgh", "a.b-c_d12345"], ["short", "a" * 81, "has space1", "semi;colon"]),
        "display": (["McGonagall - Chief of Staff", "Marauder's Map - PR Watcher", "Ryan's own Claude sessions", "Headmaster", "R"],
                    ["", " leading", "trailing ", "tab\there", "new\nline", "a" * 81, "semi;colon", "x" + "\u202e"]),
    }

    def test_task_request_owl_review_patterns(self):
        self._check(("task", "request", "owl", "review"))

    def test_desk_name_pattern(self):
        self._check(("desk",))

    def test_git_sha_pattern(self):
        self._check(("sha",))

    def test_repo_pattern(self):
        self._check(("repo",))

    def test_session_id_pattern(self):
        self._check(("session",))

    def test_display_name_pattern(self):
        self._check(("display",))

    def _check(self, kinds):
        for kind in kinds:
            good, bad = self.CASES[kind]
            for value in good:
                with self.subTest(kind=kind, value=value):
                    self.assertEqual(ids.check(kind, value), value)
            for value in bad:
                with self.subTest(kind=kind, value=value):
                    with self.assertRaises(ValidationError):
                        ids.check(kind, value)

    def test_fullmatch_rejects_trailing_newline(self):
        for kind, value in (("task", "tk_0123456789abcdef\n"), ("desk", "alpha\n"), ("sha", "0" * 40 + "\n")):
            with self.subTest(kind=kind):
                with self.assertRaises(ValidationError):
                    ids.check(kind, value)

    def test_rejects_non_ascii_digits_and_letters(self):
        with self.assertRaises(ValidationError):
            ids.check("task", "tk_0123456789abcde٣")
        with self.assertRaises(ValidationError):
            ids.check("desk", "аlpha")

    def test_rejects_non_strings(self):
        for value in (None, 5, b"tk_0123456789abcdef", ["tk_0123456789abcdef"]):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check("task", value)


class ValueTests(unittest.TestCase):
    def test_check_int_rejects_bool_negative_and_float(self):
        for value in (True, -1, 1.5, "3", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check_int(value, "n")
        self.assertEqual(ids.check_int(0, "n"), 0)

    def test_check_amount_rejects_nan_inf_negative(self):
        for value in (float("nan"), float("inf"), -0.01, True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check_amount(value, "cost")
        self.assertEqual(ids.check_amount(1, "cost"), 1.0)

    def test_check_enum(self):
        self.assertEqual(ids.check_enum("a", ("a", "b"), "x"), "a")
        with self.assertRaises(ValidationError):
            ids.check_enum("A", ("a", "b"), "x")

    def test_check_tags(self):
        self.assertEqual(ids.check_tags("a,b,a"), "a,b")
        self.assertEqual(ids.check_tags(["x-1", "y.2"]), "x-1,y.2")
        self.assertEqual(ids.check_tags(""), "")
        with self.assertRaises(ValidationError):
            ids.check_tags(["Bad Tag"])
        with self.assertRaises(ValidationError):
            ids.check_tags([f"t{i}" for i in range(17)])
        with self.assertRaises(ValidationError):
            ids.check_tags(5)


class TextTests(unittest.TestCase):
    def test_rejects_nul_bytes(self):
        with self.assertRaises(ValidationError):
            ids.clean_text("a\x00b", "body", 100)

    def test_strips_control_characters_but_keeps_newline_and_tab(self):
        self.assertEqual(ids.clean_text("a\x1b[31mb\r\n\tc\x7f\x9b", "body", 100), "a[31mb\n\tc")

    def test_single_line_collapses_newlines_and_tabs(self):
        self.assertEqual(ids.clean_text(" one\n\ttwo \n", "title", 100, single_line=True), "one two")

    def test_limit_is_enforced(self):
        self.assertEqual(len(ids.clean_text("x" * 10, "title", 10)), 10)
        with self.assertRaises(ValidationError):
            ids.clean_text("x" * 11, "title", 10)

    def test_rejects_empty_and_non_text(self):
        for value in ("", "  \n ", "\x01\x02", None, 5):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.clean_text(value, "title", 10)

    def test_rejects_lone_surrogates(self):
        with self.assertRaises(ValidationError):
            ids.clean_text("bad \udcff byte", "body", 100)

    def test_strips_zero_width_and_bidi_format_characters(self):
        hidden = "mer\u200bged \u202egr\u2066een\u2069 \ufeffcaf\u00e9\u00ad"
        self.assertEqual(ids.clean_text(hidden, "text", 100), "merged green café")
        self.assertEqual(ids.clean_text(hidden, "body", 100, keep_format=True), hidden)
        with self.assertRaises(ValidationError):
            ids.clean_text("\u200b\u200d\u2060", "title", 10)
        self.assertEqual(ids.clean_text("x" * 10 + "\u200b", "title", 10), "x" * 10)


class PathTests(unittest.TestCase):
    ROOT = "/srv/hogwarts/outbox"

    def test_accepts_absolute_normalized_paths_under_the_root(self):
        self.assertEqual(ids.check_path(self.ROOT + "/owl/body.txt", "body path", self.ROOT),
                         self.ROOT + "/owl/body.txt")

    def test_rejects_unsafe_paths(self):
        unsafe = ("relative/path", "/a/../b", "/a/./b", "/a//b", "//a", "/a/", "/", "/a\nb", "/a\x00b", "", None,
                  "/" + "a" * 1024, self.ROOT + "/../x", self.ROOT + "//x", self.ROOT + "/x/")
        for value in unsafe:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check_path(value, "body path", self.ROOT)

    def test_paths_must_stay_under_their_root(self):
        for value in ("/Users/crisryantan/.ssh/id_ed25519", self.ROOT, self.ROOT + "-evil/x", "/srv/hogwarts/x"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check_path(value, "body path", self.ROOT)

    def test_desk_roots_are_built_from_validated_desk_names(self):
        self.assertEqual(ids.outbox_root("ryan-claude"), "/Users/crisryantan/hogwarts/desks/ryan-claude/outbox")
        with self.assertRaises(ValidationError):
            ids.outbox_root("../beta")

    def test_path_roots_sit_in_the_castle_except_reviews_in_the_office(self):
        self.assertEqual(
            (ids.DESKS_ROOT, ids.TASKS_ROOT, ids.WORKTREES_ROOT, ids.REVIEWS_ROOT),
            ("/Users/crisryantan/hogwarts/desks", "/Users/crisryantan/hogwarts/tasks",
             "/Users/crisryantan/hogwarts/worktrees", "/Users/crisryantan/.hogwarts/reviews"),
        )

    def test_intent_path_is_exactly_the_tasks_own_task_md(self):
        own = "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/TASK.md"
        self.assertEqual(ids.intent_path("tk_0123456789abcdef"), own)
        self.assertEqual(ids.check_intent_path(own, "tk_0123456789abcdef"), own)
        refused = (
            "/Users/crisryantan/hogwarts/tasks/tk_00000000000000ff/TASK.md",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/NOTES.md",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/task.md",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/TASK.md/",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/x/../TASK.md",
            "/Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/./TASK.md",
            "//Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/TASK.md",
            "/Users/crisryantan/hogwarts-fleet/tasks/tk_0123456789abcdef/TASK.md",
            "/Users/crisryantan/hogwarts/desks/alpha/tk_0123456789abcdef/TASK.md",
            "Users/crisryantan/hogwarts/tasks/tk_0123456789abcdef/TASK.md",
            own + "\n",
            None,
        )
        for value in refused:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ids.check_intent_path(value, "tk_0123456789abcdef")
        with self.assertRaisesRegex(ValidationError, "task's id"):
            ids.check_intent_path(own, None)
        with self.assertRaises(ValidationError):
            ids.intent_path("../tk_0123456789abcdef")
        self.assertIsNone(ids.optional_intent_path(None, None))

    def test_castle_paths_refuse_lookalike_roots(self):
        for root in (ids.DESKS_ROOT, ids.WORKTREES_ROOT, ids.REVIEWS_ROOT):
            for value in (root + "-evil/x", root.replace("hogwarts", "hogwarts-fleet") + "/x",
                          root + "/x/../../escape", root):
                with self.subTest(root=root, value=value):
                    with self.assertRaises(ValidationError):
                        ids.check_path(value, "path", root)
            self.assertEqual(ids.check_path(root + "/x/y", "path", root), root + "/x/y")


class TimestampTests(unittest.TestCase):
    def test_timestamps_are_bounded_so_durations_cannot_overflow(self):
        self.assertEqual(ids.MAX_TIME, 253402300799)
        self.assertEqual(ids.stamp(ids.MAX_TIME), ids.MAX_TIME)
        self.assertEqual(ids.stamp(0), 0)
        for value in (ids.MAX_TIME + 1, ids.MAX_INT, 2**63, -1, True, 1.5, "1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError, "timestamp"):
                    ids.stamp(value)


if __name__ == "__main__":
    unittest.main()
