import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import obsidian_notes
from core import undo


class ObsidianNotesTests(unittest.TestCase):
    def setUp(self):
        undo.clear()
        self.addCleanup(undo.clear)
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

    def prepare_edit(self, params, player=None):
        callbacks = []
        def request(**kwargs):
            callbacks.append(kwargs["run"])
            return "[CONFIRMATION_PENDING] Review and confirm."
        with (patch.object(obsidian_notes.confirm, "pending_title", return_value=""),
              patch.object(obsidian_notes.confirm, "request", side_effect=request)):
            result = obsidian_notes.obsidian_notes(params, player=player)
        return result, callbacks

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
        self.assertIn("Please give", obsidian_notes.obsidian_notes(
            {"action": "read", "query": ".obsidian/private.md"}))

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

    def test_create_waits_for_confirmation_and_can_be_undone(self):
        self.connect()
        note = self.root / "School" / "Exam Plan.md"
        params = {"action": "create", "query": "School/Exam Plan", "content": "Review chapter 3"}
        result, callbacks = self.prepare_edit(params)
        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertFalse(note.exists())
        self.assertEqual(len(callbacks), 1)
        self.assertIn("Created", callbacks[0]())
        self.assertEqual(note.read_text(), "Review chapter 3")
        self.assertIn("Undone", undo.undo_last())
        self.assertFalse(note.exists())

    def test_create_never_overwrites_and_undo_preserves_later_changes(self):
        self.connect()
        existing = self.root / "Workout.md"
        result, callbacks = self.prepare_edit({"action": "create", "query": "Workout", "content": "No"})
        self.assertIn("already exists", result)
        self.assertFalse(callbacks)
        self.assertEqual(existing.read_text(), "Leg day volume and reps")

        new = self.root / "New.md"
        _, callbacks = self.prepare_edit({"action": "create", "query": "New", "content": "Initial"})
        callbacks[0]()
        new.write_text("Edited in Obsidian")
        self.assertIn("changed", undo.undo_last())
        self.assertEqual(new.read_text(), "Edited in Obsidian")

    def test_append_and_replace_preview_then_undo(self):
        self.connect()
        path = self.root / "Workout.md"
        previews = []
        player = type("Player", (), {"show_content": lambda self, title, text: previews.append((title, text))})()
        _, callbacks = self.prepare_edit({"action": "append", "query": "Workout", "content": "3 sets of squats"}, player)
        self.assertNotIn("squats", path.read_text())
        self.assertIn("3 sets of squats", previews[-1][1])
        callbacks[0]()
        self.assertEqual(path.read_text(), "Leg day volume and reps\n3 sets of squats")
        self.assertIn("Undone", undo.undo_last())
        self.assertEqual(path.read_text(), "Leg day volume and reps")

        _, callbacks = self.prepare_edit({"action": "replace", "query": "Workout",
                                          "old_text": "volume", "new_text": "total volume"}, player)
        self.assertIn("WITH:\ntotal volume", previews[-1][1])
        callbacks[0]()
        self.assertEqual(path.read_text(), "Leg day total volume and reps")
        self.assertIn("Undone", undo.undo_last())
        self.assertEqual(path.read_text(), "Leg day volume and reps")

    def test_edit_aborts_if_obisidian_changed_note_while_confirming(self):
        self.connect()
        path = self.root / "Workout.md"
        _, callbacks = self.prepare_edit({"action": "append", "query": "Workout", "content": "Later"})
        path.write_text("Changed while reviewing")
        self.assertIn("changed while", callbacks[0]())
        self.assertEqual(path.read_text(), "Changed while reviewing")
        self.assertFalse(undo.can_undo())

    def test_replace_requires_one_exact_occurrence_and_rejects_hidden_paths(self):
        self.connect()
        (self.root / "Repeated.md").write_text("same same")
        result, callbacks = self.prepare_edit({"action": "replace", "query": "Repeated",
                                               "old_text": "same", "new_text": "other"})
        self.assertIn("occur once", result)
        self.assertFalse(callbacks)
        for action in ("read", "append", "replace", "create"):
            result, callbacks = self.prepare_edit({"action": action, "query": ".obsidian/private.md",
                                                   "content": "More", "old_text": "secret", "new_text": "changed"})
            self.assertFalse(callbacks)
            self.assertNotIn("secret marker", result)
        self.assertEqual((self.root / ".obsidian" / "private.md").read_text(), "secret marker")

    def test_symlinked_folder_cannot_be_edited(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "Private.md").write_text("external")
        (self.root / "Linked").symlink_to(outside, target_is_directory=True)
        self.connect()
        for action in ("create", "append", "replace"):
            result, callbacks = self.prepare_edit({"action": action, "query": "Linked/Private",
                                                   "content": "More", "old_text": "external", "new_text": "changed"})
            self.assertFalse(callbacks)
        self.assertEqual((outside / "Private.md").read_text(), "external")

    def test_pending_confirmation_blocks_second_edit(self):
        self.connect()
        with patch.object(obsidian_notes.confirm, "pending_title", return_value="EDIT NOTE?"):
            result = obsidian_notes.obsidian_notes({"action": "append", "query": "Workout", "content": "More"})
        self.assertIn("already a confirmation", result)
        self.assertEqual((self.root / "Workout.md").read_text(), "Leg day volume and reps")


if __name__ == "__main__":
    unittest.main()
