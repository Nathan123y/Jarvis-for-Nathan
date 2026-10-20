"""Phone audio <-> Gemini Live audio.

Twilio streams calls as 8 kHz G.711 mu-law; Gemini Live takes 16 kHz 16-bit PCM
and answers in 24 kHz 16-bit PCM. Python 3.13 removed audioop, so the codec is
done here with numpy. Phone audio stops at about 3.4 kHz, so plain linear
interpolation up and an averaging decimator down are all the quality a call can
carry.
"""
from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _decode_table() -> np.ndarray:
    codes = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = codes & 0x80
    exponent = (codes >> 4) & 0x07
    mantissa = codes & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign, -magnitude, magnitude).astype(np.int16)


_DECODE = _decode_table()


def ulaw_to_pcm16(data: bytes) -> np.ndarray:
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    s = samples.astype(np.int32)
    sign = np.where(s < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(s), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(magnitude)).astype(np.int32) - 7, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def upsample_8k_to_16k(samples: np.ndarray) -> np.ndarray:
    if len(samples) == 0:
        return samples.astype(np.int16)
    src = np.arange(len(samples))
    dst = np.arange(len(samples) * 2) / 2.0
    return np.interp(dst, src, samples.astype(np.float32)).astype(np.int16)


def downsample_24k_to_8k(samples: np.ndarray) -> np.ndarray:
    """Average each run of three samples: a crude low-pass and decimate in one."""
    n = len(samples) // 3 * 3
    if n == 0:
        return np.zeros(0, dtype=np.int16)
    return samples[:n].astype(np.int32).reshape(-1, 3).mean(axis=1).astype(np.int16)


def twilio_to_gemini(ulaw: bytes) -> bytes:
    """One Twilio media payload -> 16 kHz PCM bytes for Gemini."""
    return upsample_8k_to_16k(ulaw_to_pcm16(ulaw)).tobytes()


class GeminiToTwilio:
    """24 kHz PCM from Gemini -> 8 kHz mu-law for Twilio. Keeps the odd bytes
    and samples between chunks, since Gemini's chunk sizes are arbitrary."""

    def __init__(self):
        self._pending = b""

    def convert(self, pcm24: bytes) -> bytes:
        data = self._pending + pcm24
        usable = len(data) // 6 * 6            # whole groups of three 16-bit samples
        self._pending = data[usable:]
        if not usable:
            return b""
        samples = np.frombuffer(data[:usable], dtype=np.int16)
        return pcm16_to_ulaw(downsample_24k_to_8k(samples))
