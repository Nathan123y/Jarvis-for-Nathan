"""Jarvis's speaking voice when it is not one of Gemini's own.

Gemini Live still hears the user, decides what to say and calls the tools. With
one of these voices picked, its native audio is set aside and the transcript of
what it said is read out by another speech engine, a sentence at a time, into the
same playback queue Gemini's audio used. So the lip-sync, the waveform, the echo
guard and the interrupt button all keep working unchanged (main.py).

Two details that matter:

* Text is only spoken once Gemini audio has arrived in the same turn. After a tool
  call the API re-sends the tail of the previous transcript WITHOUT audio (see
  main._is_repeat_chunk); without this gate Jarvis would say it twice.
* If a turn ends with audio but no usable transcript, or the outside voice fails
  outright, the turn's own Gemini audio is played instead, so Jarvis never goes
  silent. Two failures in a row switch back to Gemini's voice for the session.

Voices (all free, no key):
* Microsoft neural voices through edge-tts (internet; the video bot uses it too).
  JARVIS = en-GB-ThomasNeural at +4 %. Measured on 2026-11-23 against Marvel's
  JARVIS trailer (Iron Man 3 second-screen app): Thomas matches its pitch (median
  121 Hz vs 121.5, 3.2 vs 3.4 semitones of movement) and, 4 % quicker, its pace.
  A stock voice tuned to the same register, not a copy of the actor.
* Kokoro (offline, memory/kokoro) is the fallback when Microsoft's service fails:
  bm_daniel has the closest offline pitch (120.6 Hz).
* ElevenLabs (paid beyond its free ~10 minutes a month; the user's own key as
  "elevenlabs_api_key" in config/api_keys.json). The most natural of these; its
  library has deep British butler voices. Never a clone of a real person: the
  user asked for the actors' "exact voices" on 11-23 and that was declined.
  The voice is "elevenlabs_voice" (a name from the account's voices, or an ID).
  If it fails (no credit left, key revoked), Gemini's own voice takes over at
  once rather than a robotic stand-in.

The user found the Microsoft voices "way too robotic" (11-23) and asked for
deeper, Alfred-like British voices: Gemini's own Algieba is the default now
(memory/config_manager.DEFAULT_VOICE) and these stay as options in the picker.
"""
from __future__ import annotations

import asyncio
import collections
import io
import re
import threading
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np

SR = 24_000                     # Gemini Live's output rate; the playback stream is opened at it
_SLICE = 2400                   # 50 ms of 16-bit mono at 24 kHz, as main.py queues Gemini's audio
ROOT = Path(__file__).resolve().parent.parent
KOKORO_DIR = ROOT / "memory" / "kokoro"

# Names shown in the voice picker (memory/config_manager.EXTERNAL_VOICES lists the same).
VOICES: dict[str, dict] = {
    "JARVIS": {"engine": "edge", "voice": "en-GB-ThomasNeural", "rate": "+4%", "pitch": "+0Hz"},
    "Alfred": {"engine": "edge", "voice": "en-GB-ThomasNeural", "rate": "-6%", "pitch": "-10Hz"},
    "Ryan":   {"engine": "edge", "voice": "en-GB-RyanNeural", "rate": "+0%", "pitch": "+0Hz"},
    "Daniel": {"engine": "kokoro", "voice": "bm_daniel", "speed": 1.0},
    "Eleven": {"engine": "elevenlabs"},          # voice and model from config (eleven_settings)
}
ELEVEN_URL = "https://api.elevenlabs.io/v1"
ELEVEN_MODEL = "eleven_flash_v2_5"               # their lowest-latency model (~75 ms), half the credits
ELEVEN_DEFAULT_VOICE = "Daniel"                  # their deep British premade voice, until one is chosen
FALLBACK = {"engine": "kokoro", "voice": "bm_daniel", "speed": 1.0}
# The Gemini voice the Live session connects with while an outside voice speaks.
# Its audio is only heard if the outside voice fails, so pick a calm male one.
LIVE_BASE_VOICE = "Charon"

_EDGE_TIMEOUT_S = 8.0
_MAX_RAW_S = 90                 # Gemini audio kept per turn as the safety net


def is_external(name: str) -> bool:
    return name in VOICES


def live_voice_for(name: str) -> str:
    """The Gemini prebuilt voice the session should connect with."""
    return LIVE_BASE_VOICE if is_external(name) else name


# ── text ─────────────────────────────────────────────────────────────────────

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "e.g", "i.e", "etc", "no", "approx", "sr", "jr"}
_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*\s+")


def join_chunk(buf: str, chunk: str) -> str:
    """Transcript chunks arrive raw with their own spacing (' two', ' emails', ' today,'),
    so they are joined as they are: stripping them would split a word cut across two
    chunks."""
    text = buf + str(chunk or "")
    return re.sub(r"[ \t\r\n]+", " ", text).lstrip()


# A clause boundary worth speaking at before the sentence ends. Gemini's transcript
# arrives at speaking pace (measured 11-23: one sentence took 1.8 s to arrive), so
# waiting for the full stop made every long sentence start late.
_CLAUSE = re.compile(r"[,;:—–]\s+")
_CLAUSE_MIN_WORDS = 4


def split_ready(buf: str) -> tuple[list[str], str]:
    """Pieces ready to speak (whole sentences, or clauses of 4+ words), and the rest."""
    out: list[str] = []
    start = 0
    marks = sorted([(m.start(), m.end(), "s") for m in _BOUNDARY.finditer(buf)] +
                   [(m.start(), m.end(), "c") for m in _CLAUSE.finditer(buf)])
    for m_start, m_end, kind in marks:
        if m_start < start:
            continue
        if kind == "c":
            if len(buf[start:m_start].split()) < _CLAUSE_MIN_WORDS:
                continue
            piece = buf[start:m_end].strip()
            if piece:
                out.append(piece)
            start = m_end
            continue
        before = buf[start:m_start].rsplit(None, 1)
        last = before[-1].lower().rstrip(".") if before else ""
        if last in _ABBREV or (len(last) == 1 and last.isalpha() and buf[m_start] == "."):
            continue                       # "Dr. Smith", "J. Cole": not the end of a sentence
        sentence = buf[start:m_end].strip()
        if sentence:
            out.append(sentence)
        start = m_end
    return out, buf[start:]


def speakable(text: str) -> str:
    """Strip what a speech engine would read out literally."""
    text = re.sub(r"[*_#`~|<>\[\]{}]", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


# ── audio ────────────────────────────────────────────────────────────────────

def to_pcm(samples, sr: int) -> bytes:
    """float or int samples at any rate -> 24 kHz 16-bit mono bytes, with the engine's
    leading silence cut (it is pure latency) and the trailing pause capped."""
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size and np.max(np.abs(x)) > 1.5:          # int16 range handed in as numbers
        x = x / 32768.0
    if sr != SR and x.size:
        n = int(round(x.size * SR / sr))
        x = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)
    loud = np.where(np.abs(x) > 0.01)[0]
    if loud.size:
        start = max(0, loud[0] - int(0.03 * SR))
        end = min(x.size, loud[-1] + int(0.22 * SR))
        x = x[start:end]
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


async def _edge(text: str, v: dict) -> bytes:
    import edge_tts
    import soundfile as sf

    async def fetch() -> bytes:
        buf = bytearray()
        comm = edge_tts.Communicate(text, v["voice"], rate=v.get("rate", "+0%"),
                                    pitch=v.get("pitch", "+0Hz"))
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    mp3 = await asyncio.wait_for(fetch(), timeout=_EDGE_TIMEOUT_S)
    if not mp3:
        raise RuntimeError("Microsoft's voice sent no audio")
    x, sr = sf.read(io.BytesIO(mp3), dtype="float32")
    return to_pcm(x, sr)


_kokoro = None
_kokoro_lock = threading.Lock()


def _kokoro_say(text: str, v: dict) -> bytes:
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        samples, sr = _kokoro.create(text, voice=v["voice"], speed=float(v.get("speed", 1.0)), lang="en-gb")
    return to_pcm(samples, sr)


class VoiceUnavailable(RuntimeError):
    """The service refused for a reason retrying won't fix (key, credit, plan)."""


def eleven_settings() -> dict:
    try:
        from memory.config_manager import load_api_keys
        cfg = load_api_keys()
    except Exception:
        cfg = {}
    return {"key": str(cfg.get("elevenlabs_api_key") or "").strip(),
            "voice": str(cfg.get("elevenlabs_voice") or ELEVEN_DEFAULT_VOICE).strip(),
            "model": str(cfg.get("elevenlabs_model") or ELEVEN_MODEL).strip()}


_eleven_ids: dict[str, str] = {}


def _eleven_voice_id(key: str, want: str) -> str:
    """A voice ID as given, or looked up by name among the account's voices."""
    import requests
    if re.fullmatch(r"[A-Za-z0-9]{20}", want):
        return want
    if want.lower() in _eleven_ids:
        return _eleven_ids[want.lower()]
    r = requests.get(f"{ELEVEN_URL}/voices", headers={"xi-api-key": key}, timeout=10)
    if r.status_code in (401, 403):
        raise VoiceUnavailable(f"ElevenLabs refused the key ({r.status_code})")
    r.raise_for_status()
    voices = r.json().get("voices") or []
    for v in voices:
        _eleven_ids[str(v.get("name", "")).lower()] = v.get("voice_id", "")
    for name, vid in _eleven_ids.items():
        if name == want.lower() or name.startswith(want.lower()):
            return vid
    raise VoiceUnavailable(f"no ElevenLabs voice called {want!r} on this account")


def _eleven_say(text: str, v: dict) -> bytes:
    import requests
    cfg = eleven_settings()
    if not cfg["key"]:
        raise VoiceUnavailable("no ElevenLabs key saved")
    vid = _eleven_voice_id(cfg["key"], cfg["voice"])
    body = {"text": text, "model_id": cfg["model"],
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.0,
                               "use_speaker_boost": True}}
    if v.get("previous_text"):
        body["previous_text"] = v["previous_text"]      # keeps the intonation joined up across clauses
    r = requests.post(f"{ELEVEN_URL}/text-to-speech/{vid}", params={"output_format": "pcm_24000"},
                      headers={"xi-api-key": cfg["key"]}, json=body, timeout=12)
    if r.status_code in (401, 402, 403) or "quota" in r.text[:300].lower():
        raise VoiceUnavailable(f"ElevenLabs said no ({r.status_code}): {r.text[:120]}")
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:120]}")
    pcm = np.frombuffer(r.content[: len(r.content) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    return to_pcm(pcm, SR)


def kokoro_available() -> bool:
    return (KOKORO_DIR / "kokoro-v1.0.onnx").is_file() and (KOKORO_DIR / "voices-v1.0.bin").is_file()


async def synthesize(text: str, v: dict, log: Callable[[str], None] = print) -> bytes:
    """Speech for one sentence: the chosen voice, then the offline one."""
    # Only the Microsoft voices fall back to Kokoro (both are synthetic); ElevenLabs
    # hands back to Gemini's natural voice instead of dropping to a robotic one.
    chain = [v] + ([FALLBACK] if v.get("engine") == "edge" and kokoro_available() else [])
    last: Optional[BaseException] = None
    for i, voice in enumerate(chain):
        try:
            if voice["engine"] == "edge":
                return await _edge(text, voice)
            if voice["engine"] == "elevenlabs":
                return await asyncio.to_thread(_eleven_say, text, voice)
            return await asyncio.to_thread(_kokoro_say, text, voice)
        except VoiceUnavailable:
            raise
        except Exception as exc:          # network, service change, missing model
            last = exc
            if i + 1 < len(chain):
                log(f"[Voice] {voice['voice']} failed ({type(exc).__name__}: {str(exc)[:80]}); "
                    f"using the offline voice")
    raise RuntimeError(f"no voice could speak: {last!r}")


# ── the speaker ──────────────────────────────────────────────────────────────

class _Say:
    __slots__ = ("gen", "task")

    def __init__(self, gen: int, task: asyncio.Future):
        self.gen, self.task = gen, task


class _Raw:
    __slots__ = ("gen", "pcm")

    def __init__(self, gen: int, pcm: bytes):
        self.gen, self.pcm = gen, pcm


class _Done:
    __slots__ = ("cb",)

    def __init__(self, cb: Optional[Callable[[], None]]):
        self.cb = cb


class VoiceOut:
    """Turns the Live model's transcript into speech, in order, one sentence at a time.

    All methods are called on the asyncio loop thread (main.py's receive loop)."""

    def __init__(self, put: Callable[[bytes], None], log: Callable[[str], None] = print,
                 synth: Optional[Callable[[str, dict], Awaitable[bytes]]] = None,
                 on_give_up: Optional[Callable[[str], None]] = None):
        self._put = put
        self._log = log
        self._synth = synth or (lambda text, v: synthesize(text, v, log))
        self._on_give_up = on_give_up
        self.voice_name = ""
        self._voice: Optional[dict] = None
        self._gen = 0
        self._order: collections.deque = collections.deque()
        self._wake: Optional[asyncio.Event] = None
        self._runner: Optional[asyncio.Task] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._failures = 0
        self._reset_turn()

    # state
    @property
    def active(self) -> bool:
        return self._voice is not None

    def set_voice(self, name: str) -> bool:
        """Speak with `name` from now on; a Gemini voice name switches this off."""
        self.cancel()
        self._voice = dict(VOICES[name]) if name in VOICES else None
        if self._voice and self._voice["engine"] == "kokoro" and not kokoro_available():
            self._log(f"[Voice] {name} needs the offline model in memory/kokoro; using Gemini's voice")
            self._voice = None
        if self._voice and self._voice["engine"] == "elevenlabs" and not eleven_settings()["key"]:
            self._log("[Voice] ElevenLabs needs your key (elevenlabs_api_key); using Gemini's voice")
            self._voice = None
        self.voice_name = name if self._voice else ""
        self._failures = 0
        return self.active

    def _reset_turn(self) -> None:
        self._buf = ""
        self._held: list[str] = []
        self._turn_audio = False
        self._turn_spoke = False
        self._turn_raw = bytearray()
        self._last_text = ""

    # input from the receive loop
    def gemini_audio(self, pcm: bytes) -> None:
        """Gemini's own audio for this turn: proof the text is real speech, and the
        safety net if the outside voice has nothing to say."""
        if len(self._turn_raw) < _MAX_RAW_S * SR * 2:
            self._turn_raw.extend(pcm)
        if not self._turn_audio:
            self._turn_audio = True
            held, self._held = self._held, []
            for text in held:
                self._take(text)

    def feed(self, text: str) -> None:
        if not self.active:
            return
        if self._turn_audio:
            self._take(text)
        else:
            self._held.append(text)

    def flush(self) -> None:
        """Speak what is pending now (a tool call is about to run)."""
        if self._turn_audio and self._buf.strip():
            self._say(self._buf)
            self._buf = ""

    def end_turn(self, on_done: Optional[Callable[[], None]] = None) -> None:
        """The model finished this turn; `on_done` runs once its last audio is queued."""
        if self._turn_audio and self._buf.strip():
            self._say(self._buf)
        if self._turn_audio and not self._turn_spoke and len(self._turn_raw) >= int(0.3 * SR) * 2:
            self._push(_Raw(self._gen, bytes(self._turn_raw)))       # no transcript came: Gemini's own
        self._reset_turn()
        self._push(_Done(on_done))

    def cancel(self) -> None:
        """Stop everything not yet played (interrupt, new session, new voice)."""
        self._gen += 1
        pending = list(self._order)
        self._order.clear()
        for item in pending:
            if isinstance(item, _Say):
                item.task.cancel()
        for item in pending:
            if isinstance(item, _Done) and item.cb:
                try:
                    item.cb()
                except Exception:
                    pass
        self._reset_turn()

    # internals
    def _take(self, text: str) -> None:
        self._buf = join_chunk(self._buf, text)
        sentences, self._buf = split_ready(self._buf)
        for s in sentences:
            self._say(s)

    def _say(self, text: str) -> None:
        text = speakable(text)
        if not text or not re.search(r"\w", text):
            return
        self._turn_spoke = True
        voice = dict(self._voice or FALLBACK)
        voice["previous_text"], self._last_text = self._last_text, text

        async def job() -> bytes:
            if self._sem is None:
                self._sem = asyncio.Semaphore(3)     # a few sentences ahead, not a flood
            async with self._sem:
                return await self._synth(text, voice)
        self._push(_Say(self._gen, asyncio.ensure_future(job())))

    def _push(self, item) -> None:
        self._order.append(item)
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._runner is None or self._runner.done():
            self._runner = asyncio.ensure_future(self._run())
        self._wake.set()

    async def _run(self) -> None:
        while True:
            if not self._order:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = self._order[0]
            if isinstance(item, _Say):
                try:
                    pcm = await item.task
                    self._failures = 0
                except asyncio.CancelledError:
                    pcm = None
                    if self._runner is not None and self._runner.cancelled():
                        raise
                except VoiceUnavailable as exc:
                    pcm = None
                    self._failures = 2                     # no point trying the next sentence
                    self._log(f"[Voice] {exc}")
                except Exception as exc:
                    pcm = None
                    self._failures += 1
                    self._log(f"[Voice] couldn't speak a sentence: {exc}")
                if self._order and self._order[0] is item:
                    self._order.popleft()
                    if pcm and item.gen == self._gen:
                        self._put(pcm)
                if self._failures >= 2 and self._voice is not None:
                    name = self.voice_name
                    self._voice, self.voice_name = None, ""
                    self._log(f"[Voice] {name} keeps failing; Gemini's voice speaks for now")
                    if self._on_give_up:
                        self._on_give_up(name)
                continue
            self._order.popleft()
            if isinstance(item, _Raw):
                if item.gen == self._gen:
                    self._put(item.pcm)
            elif item.cb:
                try:
                    item.cb()
                except Exception:
                    pass
