"""G.711 mu-law helpers.

The standard library's ``audioop`` module was removed in Python 3.13, so the
mu-law -> PCM16 conversion is implemented here with a 256-entry lookup table.
It is a handful of lines and removes a dependency that would otherwise silently
break on newer interpreters.

Telephony constants used throughout the project:
  * 8000 Hz, mono, 8-bit mu-law  (what Twilio Media Streams speaks)
  * 20 ms frame == 160 samples == 160 bytes of mu-law
"""

from __future__ import annotations

import struct

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000  # 160
ULAW_SILENCE = 0xFF
SILENT_FRAME = bytes([ULAW_SILENCE]) * FRAME_BYTES

_BIAS = 0x84
_CLIP = 32635


def _build_ulaw_to_pcm_table() -> list[int]:
    table = []
    for byte in range(256):
        u = ~byte & 0xFF
        t = ((u & 0x0F) << 3) + _BIAS
        t <<= (u & 0x70) >> 4
        table.append((_BIAS - t) if (u & 0x80) else (t - _BIAS))
    return table


ULAW_TO_PCM = _build_ulaw_to_pcm_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Decode mu-law bytes to little-endian signed 16-bit PCM."""
    return struct.pack(f"<{len(data)}h", *(ULAW_TO_PCM[b] for b in data))


def frame_count(data: bytes) -> int:
    return len(data) // FRAME_BYTES


def ms_of(data: bytes) -> int:
    """Duration in milliseconds of a mu-law buffer at 8 kHz."""
    return len(data) * 1000 // SAMPLE_RATE


def rms(pcm16: bytes) -> float:
    """Rough loudness of a PCM16 buffer; used only for dead-air heuristics."""
    if not pcm16:
        return 0.0
    samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16[: len(pcm16) // 2 * 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5
