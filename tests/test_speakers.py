"""Speaker routing. Core Audio is faked: nothing real is switched."""
import unittest
from unittest.mock import patch

from core import system_output
from plugins import speakers, youtube_music


class FakeOutputs:
    def __init__(self, connected, current):
        self.connected, self.now, self.switched = list(connected), current, []

    def find(self, name):
        return next((n for n in self.connected if n.lower().startswith(name.lower())), None)

    def switch_to(self, name):
        match = self.find(name)
        if match:
            self.now = match
            self.switched.append(match)
        return bool(match)

    def current(self):
        return self.now


class SpeakerTests(unittest.TestCase):
    def use(self, connected, current="MacBook Air Speakers", voice="MacBook Air Speakers"):
        self.fake = FakeOutputs(connected, current)
        for p in (patch.object(system_output, "find", side_effect=self.fake.find),
                  patch.object(system_output, "switch_to", side_effect=self.fake.switch_to),
                  patch.object(system_output, "current", side_effect=self.fake.current),
                  patch.object(speakers, "get_plugin_config", return_value={}),
                  patch.object(speakers, "get_output_device", return_value=voice)):
            p.start()
            self.addCleanup(p.stop)

    def test_switches_to_the_speakers_and_back(self):
        self.use(["P24h-10", "MacBook Air Speakers"])
        self.assertEqual(speakers.run({"action": "speakers"}), "Sound is playing on your speakers now.")
        self.assertEqual(self.fake.now, "P24h-10")
        self.assertEqual(speakers.run({"action": "macbook"}), "Sound is back on the MacBook speakers.")
        self.assertEqual(self.fake.now, "MacBook Air Speakers")

    def test_speakers_off_says_so_and_switches_nothing(self):
        self.use(["MacBook Air Speakers"])
        self.assertEqual(speakers.run({"action": "speakers"}), speakers.SPEAKERS_OFF)
        self.assertIn("off or unplugged", speakers.run({"action": "status"}))
        self.assertEqual(self.fake.switched, [])

    def test_warns_when_jarvis_voice_would_follow(self):
        self.use(["P24h-10", "MacBook Air Speakers"], voice="")
        self.assertIn("My voice is set to the system default", speakers.run({"action": "speakers"}))

    def test_status_names_where_sound_is(self):
        self.use(["P24h-10", "MacBook Air Speakers"], current="P24h-10")
        self.assertEqual(speakers.run({"action": "status"}), "Your speakers are on and sound is playing on them.")
        self.fake.now = "MacBook Air Speakers"
        self.assertIn("playing on MacBook Air Speakers", speakers.run({"action": "status"}))

    def test_music_on_speakers_routes_first_or_refuses(self):
        self.use(["P24h-10", "MacBook Air Speakers"])
        with patch.object(youtube_music, "_play", return_value="Playing X.") as play:
            self.assertEqual(youtube_music.run({"action": "play", "query": "x", "speakers": True}), "Playing X.")
            self.assertEqual(self.fake.now, "P24h-10")
            self.fake.connected = ["MacBook Air Speakers"]
            self.assertEqual(youtube_music.run({"action": "play", "query": "x", "speakers": True}),
                             speakers.SPEAKERS_OFF)
            self.assertEqual(play.call_count, 1)
            # Without "on the speakers" the output is left alone.
            youtube_music.run({"action": "play", "query": "x"})
            self.assertEqual(len(self.fake.switched), 1)


if __name__ == "__main__":
    unittest.main()
