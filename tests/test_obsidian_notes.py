import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import obsidian_notes


class ObsidianNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Nathan's Vault"
        self.root.mkdir()
        (self.root / ".obsidian").mkdir()
        (self.root / ".obsidian" / "private.md").write_text("secret marker")
        (self.root / "School").mkdir()
        (self.root / "School" / "Physics.md").write_text("Refraction lab notes")
        (self.root / "Workout.md").write_text("Leg day volume and reps")
        self.state = Path(self.temp.name) / "settings" / "obsidian-vault.json"
        patcher = patch.object(obsidian_notes, "_state_file", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self):
        with patch.object(obsidian_notes, "_choose_vault", return_value=self.root):
            return obsidian_notes.obsidian_notes({"action": "connect"})

    def test_connect_and_find_local_notes(self):
        self.assertIn("Connected", self.connect())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertIn("School/Physics.md", obsidian_notes.obsidian_notes(
            {"action": "search", "query": "refraction"}))
        self.assertIn("Refraction lab notes", obsidian_notes.obsidian_notes(
            {"action": "read", "query": "Physics"}))
        self.assertIn("Workout.md", obsidian_notes.obsidian_notes({"action": "recent"}))

    def test_hidden_files_and_paths_outside_vault_are_not_read(self):
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("external secret marker")
        (self.root / "outside.md").symlink_to(outside)
        self.connect()
        self.assertIn("no Obsidian notes", obsidian_notes.obsidian_notes(
            {"action": "search", "query": "secret marker"}))
        self.assertIn("Please give", obsidian_notes.obsidian_notes(
            {"action": "read", "query": "../outside.md"}))
        self.assertIn("could not find", obsidian_notes.obsidian_notes(
            {"action": "read", "query": "outside.md"}))

    def test_duplicate_note_names_need_a_folder(self):
        (self.root / "Other").mkdir()
        (self.root / "Other" / "Physics.md").write_text("Different physics notes")
        self.connect()
        result = obsidian_notes.obsidian_notes({"action": "read", "query": "Physics"})
        self.assertIn("Several notes", result)
        self.assertIn("School/Physics.md", result)
        self.assertIn("Different physics notes", obsidian_notes.obsidian_notes(
            {"action": "read", "query": "Other/Physics"}))

    def test_invalid_folder_does_not_replace_connection(self):
        self.connect()
        with patch.object(obsidian_notes, "_choose_vault", return_value=Path(self.temp.name)):
            self.assertIn("could not connect", obsidian_notes.obsidian_notes({"action": "connect"}))
        self.assertIn("Workout.md", obsidian_notes.obsidian_notes({"action": "recent"}))


if __name__ == "__main__":
    unittest.main()
