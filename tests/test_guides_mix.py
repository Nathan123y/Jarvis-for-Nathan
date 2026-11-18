import tempfile
import unittest
from pathlib import Path

from worker import guides


class ShortMixTests(unittest.TestCase):
    def test_voice_and_music_start_after_the_footage_sound(self):
        mix = guides.short_mix(1.2)
        self.assertIn("adelay=1200|1200", mix)                 # voice and bed wait for the lead
        self.assertIn("sidechaincompress", mix)                # the footage is ducked under the voice, not muted
        self.assertNotIn("volume=0.05", mix)

    def test_captions_move_with_the_voice(self):
        p = Path(tempfile.mktemp(suffix=".ass"))
        p.write_text("[Events]\nDialogue: 0,0:00:01.50,0:00:02.25,Cap,,0,0,0,,hi\n")
        guides.shift_ass(p, 1.2)
        self.assertIn("Dialogue: 0,0:00:02.70,0:00:03.45,", p.read_text())


if __name__ == "__main__":
    unittest.main()
