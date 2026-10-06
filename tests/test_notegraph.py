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
        self.assertEqual(g["paths"], [str(a), str(b), str(c)])

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
        self.assertEqual(g, {"nodes": [], "edges": [], "paths": []})


class Layout(unittest.TestCase):
    def test_bounds_and_determinism(self):
        e = [(0, 1), (1, 2), (2, 3)]
        p1, p2 = ng.layout(4, e), ng.layout(4, e)
        self.assertEqual(p1, p2)
        self.assertTrue(all(-1.0001 <= v <= 1.0001 for xy in p1 for v in xy))

    def test_small_cases(self):
        self.assertEqual(ng.layout(0, []), [])
        self.assertEqual(ng.layout(1, []), [(0.0, 0.0)])


class Layout3D(unittest.TestCase):
    def test_deterministic_and_bounded(self):
        e = [(0, i) for i in range(1, 30)] + [(30, 31)]
        p1 = ng.layout3d(40, e)
        self.assertEqual(p1, ng.layout3d(40, e))
        self.assertEqual(len(p1), 40)
        for x, y, z in p1:
            self.assertLessEqual((x * x + y * y + z * z) ** 0.5, 1.2 + 1e-9)

    def test_linked_notes_sit_closer_than_strangers(self):
        e = [(0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5)]
        p = ng.layout3d(6, e)
        d = lambda a, b: sum((p[a][k] - p[b][k]) ** 2 for k in range(3)) ** 0.5
        self.assertLess(d(0, 1), d(0, 4))

    def test_one_far_orphan_does_not_shrink_the_cluster(self):
        e = [(0, i) for i in range(1, 20)]
        p = ng.layout3d(25, e)            # 5 unlinked notes
        spread = max(abs(c) for xyz in p[:20] for c in xyz)
        self.assertGreater(spread, 0.4)

    def test_small_cases(self):
        self.assertEqual(ng.layout3d(0, []), [])
        self.assertEqual(ng.layout3d(1, []), [(0.0, 0.0, 0.0)])


class Projection(unittest.TestCase):
    def test_centre_maps_to_centre(self):
        (x, y, s, z), = ng.project([(0, 0, 0)], 1.0, 0.3, 1.0, 800, 600)
        self.assertAlmostEqual(x, 400)
        self.assertAlmostEqual(y, 300)
        self.assertAlmostEqual(s, 1.0)

    def test_near_side_is_bigger_and_yaw_turns(self):
        (_, _, s_near, z_near), (_, _, s_far, z_far) = ng.project(
            [(0, 0, 1), (0, 0, -1)], 0.0, 0.0, 1.0, 800, 600)
        self.assertGreater(s_near, s_far)
        self.assertGreater(z_near, z_far)
        (x0, *_), = ng.project([(1, 0, 0)], 0.0, 0.0, 1.0, 800, 600)
        (x1, *_), = ng.project([(1, 0, 0)], 3.14159, 0.0, 1.0, 800, 600)
        self.assertGreater(x0, 400)
        self.assertLess(x1, 400)

    def test_zoom_scales_distance(self):
        (a, *_), = ng.project([(1, 0, 0)], 0, 0, 1.0, 800, 600)
        (b, *_), = ng.project([(1, 0, 0)], 0, 0, 2.0, 800, 600)
        self.assertAlmostEqual(b - 400, 2 * (a - 400))

    def test_obsidian_uri_is_encoded(self):
        u = ng.obsidian_uri("/Users/n/Vault/My Note & co.md")
        self.assertTrue(u.startswith("obsidian://open?path="))
        self.assertNotIn(" ", u)
        self.assertNotIn("&", u.split("=", 1)[1])


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
        self.assertEqual(cm.DEFAULT_VOICE, "Algenib")

    def test_hud_cycle(self):
        self.assertEqual(cm.next_hud_style("orb"), "face")
        self.assertEqual(cm.next_hud_style("face"), "core")
        self.assertEqual(cm.next_hud_style("core"), "orb")
        self.assertEqual(cm.next_hud_style("junk"), "orb")


if __name__ == "__main__":
    unittest.main()
