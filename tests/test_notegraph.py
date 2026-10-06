import tempfile
import unittest
from pathlib import Path

from core import notegraph as ng
from core.orb import orb_spin, orb_brightness, orb_scale, ORB_IMAGE
from memory import config_manager as cm


class LinkParsing(unittest.TestCase):
    def test_alias_heading_block_are_stripped(self):
        t = "a [[One]] b [[Two|alias]] [[Three#Head]] [[Four^blk]] [[ ]]"
        self.assertEqual(ng.parse_links(t), ["One", "Two", "Three", "Four"])

    def test_no_links(self):
        self.assertEqual(ng.parse_links("plain"), [])
        self.assertEqual(ng.parse_links(None), [])


class BuildGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def note(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_edges_resolve_case_and_folder_insensitively(self):
        a = self.note("A.md", "see [[b]] and [[School/C]] and [[Missing]] [[A]]")
        b = self.note("B.md", "back to [[A]]")
        c = self.note("School/C.md", "")
        g = ng.build_graph(self.root, [a, b, c])
        self.assertEqual(g["nodes"], ["A", "B", "C"])
        self.assertEqual(g["edges"], [(0, 1), (0, 2)])   # deduped, no self-link, no missing

    def test_cap_keeps_most_connected(self):
        hub = self.note("hub.md", " ".join(f"[[n{i}]]" for i in range(5)))
        notes = [hub] + [self.note(f"n{i}.md", "") for i in range(5)]
        old = ng.MAX_NODES
        ng.MAX_NODES = 3
        try:
            g = ng.build_graph(self.root, notes)
        finally:
            ng.MAX_NODES = old
        self.assertEqual(len(g["nodes"]), 3)
        self.assertIn("hub", g["nodes"])

    def test_unreadable_note_is_skipped(self):
        g = ng.build_graph(self.root, [self.root / "gone.md"])
        self.assertEqual(g, {"nodes": [], "edges": []})


class Layout(unittest.TestCase):
    def test_bounds_and_determinism(self):
        e = [(0, 1), (1, 2), (2, 3)]
        p1, p2 = ng.layout(4, e), ng.layout(4, e)
        self.assertEqual(p1, p2)
        self.assertTrue(all(-1.0001 <= v <= 1.0001 for xy in p1 for v in xy))

    def test_small_cases(self):
        self.assertEqual(ng.layout(0, []), [])
        self.assertEqual(ng.layout(1, []), [(0.0, 0.0)])


class OrbRules(unittest.TestCase):
    def test_spin_by_state(self):
        self.assertLess(orb_spin("IDLE", False, True), orb_spin("IDLE", False, False))
        self.assertGreater(orb_spin("THINKING", False, False), orb_spin("IDLE", False, False))

    def test_brightness_bounds(self):
        self.assertEqual(orb_brightness(5, True, True), 0.30)
        self.assertLessEqual(orb_brightness(9, True, False), 1.0)
        self.assertGreater(orb_brightness(1, False, False), orb_brightness(0, False, False))
        self.assertEqual(orb_brightness(None, False, False), 0.72)

    def test_scale(self):
        self.assertAlmostEqual(orb_scale(1.0, 0), 1.0)
        self.assertGreater(orb_scale(1.0, 1), 1.0)
        self.assertGreaterEqual(orb_scale(0, 0), 0.5)

    def test_artwork_ships(self):
        self.assertTrue(ORB_IMAGE.is_file())


class Config(unittest.TestCase):
    def test_voice_default_is_in_list(self):
        self.assertIn(cm.DEFAULT_VOICE, cm.AVAILABLE_VOICES)
        self.assertEqual(cm.DEFAULT_VOICE, "Gacrux")

    def test_hud_cycle(self):
        self.assertEqual(cm.next_hud_style("orb"), "face")
        self.assertEqual(cm.next_hud_style("face"), "core")
        self.assertEqual(cm.next_hud_style("core"), "orb")
        self.assertEqual(cm.next_hud_style("junk"), "orb")


if __name__ == "__main__":
    unittest.main()
