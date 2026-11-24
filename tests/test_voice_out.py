"""The outside speaking voice (core/voice_out.py): Gemini writes, another engine speaks."""
import asyncio
import unittest
from unittest.mock import patch

import numpy as np

from core import voice_out as vo
from memory import config_manager as cm


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """A fake speech engine: 'audio' is just the text, so the order is easy to check."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def __call__(self, text, voice):
        self.calls.append(text)
        await asyncio.sleep(0.01 if len(text) > 12 else 0.03)   # short ones finish LAST
        if text in self.fail:
            raise RuntimeError("service down")
        return ("TTS:" + text).encode()


def make(engine=None, **kw):
    played = []
    v = vo.VoiceOut(played.append, log=lambda *_: None, synth=engine or Recorder(), **kw)
    v.set_voice("JARVIS")
    return v, played


async def settle():
    for _ in range(40):
        await asyncio.sleep(0.01)


class TextTests(unittest.TestCase):
    def test_raw_chunks_join_with_their_own_spacing(self):
        b = ""
        for c in ["Connor sent", " two", " emails", " to", "day."]:
            b = vo.join_chunk(b, c)
        self.assertEqual(b, "Connor sent two emails today.")

    def test_sentences_and_long_clauses_are_ready_early(self):
        out, rest = vo.split_ready("Connor sent two emails today, reminding you that the lab")
        self.assertEqual(out, ["Connor sent two emails today,"])
        self.assertEqual(rest, "reminding you that the lab")
        self.assertEqual(vo.split_ready("Done, sir. As you wish, sir. ")[0],
                         ["Done, sir.", "As you wish, sir."])          # short clauses stay whole
        self.assertEqual(vo.split_ready("Dr. Smith called. ")[0], ["Dr. Smith called."])

    def test_speakable_drops_markup(self):
        self.assertEqual(vo.speakable("**Done**, sir [ok]"), "Done , sir ok")

    def test_pcm_is_24k_int16_with_lead_silence_cut(self):
        sr = 48000
        x = np.concatenate([np.zeros(sr // 2), 0.5 * np.sin(np.linspace(0, 600, sr // 4))])
        pcm = vo.to_pcm(x, sr)
        n = len(pcm) // 2
        self.assertLess(n, int(0.30 * vo.SR) + 1)                     # half a second of silence gone
        self.assertGreater(n, int(0.24 * vo.SR))

    def test_picker_names_match_the_voice_table(self):
        self.assertEqual(set(cm.EXTERNAL_VOICES), set(vo.VOICES))
        self.assertEqual(vo.live_voice_for("JARVIS"), vo.LIVE_BASE_VOICE)
        self.assertEqual(vo.live_voice_for("Charon"), "Charon")
        self.assertIn(vo.LIVE_BASE_VOICE, cm.AVAILABLE_VOICES)


class SpeakerTests(unittest.TestCase):
    def test_speaks_in_order_even_when_later_sentences_finish_first(self):
        async def go():
            v, played = make()
            v.gemini_audio(b"\0" * 4800)
            done = []
            for c in ["Done,", " sir.", " Connor sent two emails today,", " about the lab."]:
                v.feed(c)
            v.end_turn(lambda: done.append(len(played)))
            await settle()
            return played, done
        played, done = run(go())
        self.assertEqual(played, [b"TTS:Done, sir.", b"TTS:Connor sent two emails today,",
                                  b"TTS:about the lab."])
        self.assertEqual(done, [3])                    # turn released only after its last audio

    def test_text_waits_for_gemini_audio_and_a_resent_tail_is_not_spoken(self):
        async def go():
            v, played = make()
            v.feed("Checking, sir.")                   # transcript can arrive just before audio
            await settle()
            self.assertEqual(played, [])
            v.gemini_audio(b"\0" * 4800)
            v.end_turn()
            await settle()
            v.feed("Checking, sir.")                   # re-sent tail after a tool call: no audio
            v.end_turn()
            await settle()
            return played
        self.assertEqual(run(go()), [b"TTS:Checking, sir."])

    def test_interrupt_drops_what_is_not_played_and_releases_the_turn(self):
        async def go():
            v, played = make()
            v.gemini_audio(b"\0" * 4800)
            done = []
            v.feed("One long sentence that is still being made. ")
            v.end_turn(lambda: done.append("released"))
            v.cancel()
            await settle()
            return played, done
        played, done = run(go())
        self.assertEqual(played, [])
        self.assertEqual(done, ["released"])

    def test_no_transcript_plays_geminis_own_audio(self):
        async def go():
            v, played = make()
            v.gemini_audio(b"\1" * 24000)
            v.end_turn()
            await settle()
            return played
        self.assertEqual(run(go()), [b"\1" * 24000])

    def test_repeated_failure_hands_back_to_gemini(self):
        gave_up = []

        async def go():
            v, played = make(Recorder(fail={"First one.", "Second one."}), on_give_up=gave_up.append)
            v.gemini_audio(b"\0" * 4800)
            v.feed("First one. Second one. ")
            v.end_turn()
            await settle()
            return v
        v = run(go())
        self.assertFalse(v.active)
        self.assertEqual(gave_up, ["JARVIS"])

    def test_gemini_voice_name_switches_it_off(self):
        v, _ = make()
        self.assertTrue(v.active)
        self.assertFalse(v.set_voice("Charon"))

    def test_offline_voice_needs_its_model(self):
        with patch.object(vo, "kokoro_available", return_value=False):
            v = vo.VoiceOut(lambda _: None, log=lambda *_: None)
            self.assertFalse(v.set_voice("Daniel"))
            self.assertTrue(v.set_voice("JARVIS"))     # online voice does not need it


class FallbackChainTests(unittest.TestCase):
    def test_microsoft_failure_falls_back_to_offline_voice(self):
        async def broken(text, v):
            raise RuntimeError("403")
        with patch.object(vo, "_edge", broken), \
             patch.object(vo, "kokoro_available", return_value=True), \
             patch.object(vo, "_kokoro_say", return_value=b"offline"):
            self.assertEqual(run(vo.synthesize("Done, sir.", vo.VOICES["JARVIS"], log=lambda *_: None)),
                             b"offline")



class FakeResponse:
    def __init__(self, status=200, content=b"", payload=None):
        self.status_code, self.content, self._payload = status, content, payload or {}
        self.text = content.decode("latin-1") if isinstance(content, bytes) else str(content)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class ElevenLabsTests(unittest.TestCase):
    def setUp(self):
        vo._eleven_ids.clear()
        self.addCleanup(vo._eleven_ids.clear)

    def settings(self, key="sk_test"):
        return patch.object(vo, "eleven_settings", return_value={
            "key": key, "voice": "Butler", "model": vo.ELEVEN_MODEL})

    def test_without_a_key_the_voice_stays_off(self):
        with self.settings(key=""):
            v = vo.VoiceOut(lambda _: None, log=lambda *_: None)
            self.assertFalse(v.set_voice("Eleven"))

    def test_speech_is_raw_24k_pcm_for_the_named_voice_with_context(self):
        sent = {}
        pcm = (np.sin(np.linspace(0, 300, 12000)) * 12000).astype("<i2").tobytes()

        def get(url, headers, timeout):
            return FakeResponse(payload={"voices": [{"name": "Butler - deep British", "voice_id": "v" * 20}]})

        def post(url, params, headers, json, timeout):
            sent.update(url=url, params=params, body=json, key=headers["xi-api-key"])
            return FakeResponse(content=pcm)

        with self.settings(), patch("requests.get", get), patch("requests.post", post):
            out = vo._eleven_say("Quite so, sir.", {"previous_text": "Done."})
        self.assertTrue(sent["url"].endswith("/text-to-speech/" + "v" * 20))
        self.assertEqual(sent["params"], {"output_format": "pcm_24000"})
        self.assertEqual(sent["body"]["previous_text"], "Done.")
        self.assertEqual(sent["key"], "sk_test")
        self.assertGreater(len(out), 0)

    def test_a_refused_key_hands_back_to_gemini_at_once(self):
        gave_up = []

        async def refused(text, voice):
            raise vo.VoiceUnavailable("ElevenLabs said no (401)")

        async def go():
            with self.settings():
                v = vo.VoiceOut(lambda _: None, log=lambda *_: None, synth=refused,
                                on_give_up=gave_up.append)
                v.set_voice("Eleven")
                v.gemini_audio(b"\0" * 4800)
                v.feed("Only one sentence. ")
                v.end_turn()
                await settle()
                return v
        v = run(go())
        self.assertFalse(v.active)
        self.assertEqual(gave_up, ["Eleven"])

    def test_no_robotic_fallback_for_elevenlabs(self):
        with patch.object(vo, "_eleven_say", side_effect=RuntimeError("timeout")), \
             patch.object(vo, "kokoro_available", return_value=True), \
             patch.object(vo, "_kokoro_say", return_value=b"robot") as kokoro:
            with self.assertRaises(RuntimeError):
                run(vo.synthesize("Done, sir.", vo.VOICES["Eleven"], log=lambda *_: None))
            kokoro.assert_not_called()


if __name__ == "__main__":
    unittest.main()
