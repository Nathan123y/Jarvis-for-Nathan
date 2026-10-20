import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import file_controller as fc


class FileControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.allowed = self.root / "allowed"
        self.outside = self.root / "outside"
        self.allowed.mkdir()
        self.outside.mkdir()
        self.safe_roots = patch.object(fc, "_SAFE_ROOTS", [self.allowed])
        self.safe_roots.start()

    def tearDown(self):
        self.safe_roots.stop()
        self.temp.cleanup()

    def test_read_only_access_can_reach_outside_mutation_roots(self):
        note = self.outside / "note.txt"
        note.write_text("visible", encoding="utf-8")
        self.assertEqual(fc.read_file(str(note)), "visible")

    def test_move_outside_mutation_roots_is_denied(self):
        note = self.outside / "note.txt"
        note.write_text("keep me", encoding="utf-8")
        result = fc.move_file(str(note), destination=str(self.allowed / "note.txt"))
        self.assertIn("Access denied (source)", result)
        self.assertTrue(note.exists())

    def test_copy_from_read_only_location_into_safe_root(self):
        note = self.outside / "note.txt"
        destination = self.allowed / "copied.txt"
        note.write_text("copy me", encoding="utf-8")
        result = fc.copy_file(str(note), destination=str(destination))
        self.assertIn("Copied", result)
        self.assertEqual(destination.read_text(encoding="utf-8"), "copy me")

    def test_create_and_write_require_explicit_overwrite(self):
        note = self.allowed / "note.txt"
        note.write_text("original", encoding="utf-8")
        self.assertIn("Already exists", fc.create_file(str(note), content="new"))
        self.assertIn("Already exists", fc.write_file(str(note), content="new"))
        self.assertEqual(note.read_text(encoding="utf-8"), "original")
        self.assertIn("Written", fc.write_file(str(note), content="new", overwrite=True))
        self.assertEqual(note.read_text(encoding="utf-8"), "new")

    def test_sensitive_files_are_never_returned(self):
        secret = self.outside / ".env"
        secret.write_text("API_KEY=do-not-leak", encoding="utf-8")
        result = fc.read_file(str(secret))
        self.assertIn("Protected credential file", result)
        self.assertNotIn("do-not-leak", result)

    def test_likely_secrets_are_redacted_from_ordinary_text(self):
        note = self.outside / "notes.txt"
        note.write_text("agenda\npassword=hunter2\nfinish", encoding="utf-8")
        result = fc.read_file(str(note))
        self.assertIn("password=[REDACTED]", result)
        self.assertNotIn("hunter2", result)

    def test_content_search_skips_sensitive_files_and_returns_safe_match(self):
        (self.outside / ".env").write_text("needle=secret", encoding="utf-8")
        safe = self.outside / "notes.txt"
        safe.write_text("A safe needle appears here.", encoding="utf-8")
        result = fc.search_file_contents("needle", str(self.outside))
        self.assertIn("notes.txt", result)
        self.assertNotIn(".env", result)
        self.assertNotIn("secret", result)

    def test_tree_hides_hidden_entries_by_default(self):
        (self.outside / "visible.txt").touch()
        (self.outside / ".hidden.txt").touch()
        result = fc.show_tree(str(self.outside), depth=1)
        self.assertIn("visible.txt", result)
        self.assertNotIn(".hidden.txt", result)

    def test_find_accepts_extension_without_dot(self):
        (self.outside / "report.PDF").touch()
        (self.outside / "report.txt").touch()
        result = fc.find_files("report", "pdf", str(self.outside))
        self.assertIn("report.PDF", result)
        self.assertNotIn("report.txt", result)

    def test_handler_exposes_new_actions(self):
        (self.outside / "hello.txt").write_text("hello world", encoding="utf-8")
        result = fc.file_controller({
            "action": "search_contents",
            "path": str(self.outside),
            "query": "world",
        })
        self.assertIn("hello.txt", result)

    def test_find_ignores_spacing_dashes_and_case(self):
        (self.outside / "EE98 - FL26 - Homework 1.pdf").write_bytes(b"%PDF")
        with patch.object(fc, "_OS", "Linux"):
            result = fc.find_files("EE98FL26_Homework1.pdf", path=str(self.outside))
        self.assertIn("Found 1 file", result)
        self.assertIn("EE98 - FL26 - Homework 1.pdf", result)

    def test_find_matches_numbers_whole_and_spoken(self):
        (self.outside / "EE98 - FL26 - Homework 1.pdf").write_bytes(b"%PDF")
        (self.outside / "EE98 - FL26 - Homework 2.pdf").write_bytes(b"%PDF")
        with patch.object(fc, "_OS", "Linux"):
            result = fc.find_files("ee 98 homework two", path=str(self.outside))
        self.assertIn("Homework 2.pdf", result)
        self.assertNotIn("Homework 1.pdf", result)

    def test_find_offers_closest_names_when_nothing_matches(self):
        (self.outside / "Homework 2 - Nathan.pdf").write_bytes(b"%PDF")
        with patch.object(fc, "_OS", "Linux"):
            result = fc.find_files("Homwork 2 Nathan", path=str(self.outside))
        self.assertIn("No exact match", result)
        self.assertIn("Homework 2 - Nathan.pdf", result)

    def test_find_without_path_searches_home(self):
        with patch.object(fc, "find_files", return_value="ok") as find:
            fc.file_controller({"action": "find", "name": "x"})
        self.assertEqual(find.call_args.kwargs["path"], "home")

    def test_content_search_skips_cloud_only_files(self):
        # The cloud copy is told apart by size; reading it on a real Mac would
        # start an iCloud download and block.
        (self.outside / "local.txt").write_text("needle", encoding="utf-8")
        (self.outside / "cloud.txt").write_text("needle needle", encoding="utf-8")
        with patch.object(fc, "_is_cloud_only", lambda st: st.st_size == 13):
            result = fc.search_file_contents("needle", str(self.outside))
        self.assertIn("local.txt", result)
        self.assertNotIn("cloud.txt", result)

    def test_walk_from_home_skips_library(self):
        home = self.root / "home"
        (home / "Library" / "CloudStorage").mkdir(parents=True)
        (home / "Library" / "notes.txt").write_text("x", encoding="utf-8")
        (home / "Desktop").mkdir()
        (home / "Desktop" / "notes.txt").write_text("x", encoding="utf-8")
        with patch.object(Path, "home", return_value=home):
            found = list(fc._bounded_walk(home, deadline=float("inf")))
        self.assertEqual([p.parent.name for p in found], ["Desktop"])

    def test_locate_file_finds_a_guessed_path_by_name(self):
        home = self.root / "home"
        (home / "Downloads").mkdir(parents=True)
        real = home / "Downloads" / "EE98 - FL26 - Homework 1.pdf"
        real.write_bytes(b"%PDF")
        with patch.object(Path, "home", return_value=home), patch.object(fc, "_OS", "Linux"), \
                patch.dict("os.environ", {"HOME": str(home)}):
            self.assertEqual(fc.locate_file("~/Downloads/EE98 - FL26 - Homework 1.pdf"), real)
            self.assertEqual(fc.locate_file("/Users/nathan/Documents/EE98 - FL26 - Homework 1.pdf"), real)
            self.assertIsNone(fc.locate_file("/Users/nathan/Documents/taxes.pdf"))

    def test_content_search_accepts_text_given_as_name(self):
        (self.outside / "hello.txt").write_text("hello world", encoding="utf-8")
        result = fc.file_controller({"action": "search_contents",
                                     "path": str(self.outside), "name": "world"})
        self.assertIn("hello.txt", result)



class EmptyTrashTests(unittest.TestCase):
    """Finder is faked: nothing real is ever emptied here."""

    def setUp(self):
        from core import confirm
        self.confirm = confirm
        self.shown, self.notes, self.scripts = [], [], []
        confirm.bind(lambda title, detail: self.shown.append(title), lambda: None,
                     notify=self.notes.append)
        self.addCleanup(confirm.bind, None, None)
        self.addCleanup(confirm.resolve, False)
        self.counts = [3, 0]
        p = patch.object(fc, "_finder", side_effect=self.finder)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(fc.platform, "system", return_value="Darwin")
        p.start()
        self.addCleanup(p.stop)

    def finder(self, script, timeout=10):
        self.scripts.append(script)
        return str(self.counts.pop(0)) if script == fc._TRASH_COUNT else ""

    def wait_for_note(self):
        import time
        for _ in range(100):
            if self.notes:
                return self.notes[-1]
            time.sleep(0.01)
        self.fail("the confirmed action never reported back")

    def test_nothing_is_emptied_until_the_user_confirms(self):
        result = fc.file_controller({"action": "empty_trash"})
        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertEqual(self.shown, ["Empty the Trash (3 items)"])
        self.assertNotIn(fc._TRASH_EMPTY, self.scripts)
        self.confirm.resolve(True)
        self.assertIn("3 items permanently deleted", self.wait_for_note())
        self.assertIn(fc._TRASH_EMPTY, self.scripts)

    def test_cancel_leaves_the_trash_alone(self):
        fc.file_controller({"action": "empty_trash"})
        self.confirm.resolve(False)
        self.assertIn("Cancelled", self.wait_for_note())
        self.assertNotIn(fc._TRASH_EMPTY, self.scripts)

    def test_empty_trash_and_missing_permission_need_no_confirmation(self):
        self.counts = [0]
        self.assertEqual(fc.file_controller({"action": "empty_trash"}), "Your Trash is already empty.")
        with patch.object(fc, "_finder", side_effect=PermissionError("Jarvis needs permission to control Finder")):
            self.assertIn("permission to control Finder", fc.file_controller({"action": "empty_trash"}))
        self.assertEqual(self.shown, [])

    def test_items_still_in_use_are_reported(self):
        self.counts = [3, 1]
        fc.file_controller({"action": "empty_trash"})
        self.confirm.resolve(True)
        self.assertIn("1 item couldn't be deleted", self.wait_for_note())

if __name__ == "__main__":
    unittest.main()
