"""Local two-channel call recorder.

We already have both directions of audio inside this process: the agent's audio
arrives from Twilio and the patient bot's audio arrives from the Realtime API.
Recording locally therefore gives us perfectly separated channels for free, and
it is driven by the same 20 ms clock that paces playback, so the two channels
stay time-aligned for the whole call.

Layout: LEFT = our patient bot, RIGHT = the agent under test.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from .audio import FRAME_BYTES, SAMPLE_RATE, SILENT_FRAME, ulaw_to_pcm16


class StereoRecorder:
    """Accumulates one 20 ms frame per channel per tick, then writes a WAV."""

    def __init__(self, wav_path: Path) -> None:
        self.wav_path = wav_path
        self._left = bytearray()
        self._right = bytearray()
        self.frames = 0

    def tick(self, bot_ulaw: bytes | None, agent_ulaw: bytes | None) -> None:
        """Append exactly one frame to each channel. None means silence."""
        self._left += _fit(bot_ulaw)
        self._right += _fit(agent_ulaw)
        self.frames += 1

    @property
    def duration_seconds(self) -> float:
        return self.frames * FRAME_BYTES / SAMPLE_RATE

    def write_wav(self) -> Path:
        left = ulaw_to_pcm16(bytes(self._left))
        right = ulaw_to_pcm16(bytes(self._right))
        interleaved = bytearray(len(left) * 2)
        interleaved[0::4] = left[0::2]
        interleaved[1::4] = left[1::2]
        interleaved[2::4] = right[0::2]
        interleaved[3::4] = right[1::2]

        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(bytes(interleaved))
        return self.wav_path


def _fit(frame: bytes | None) -> bytes:
    if not frame:
        return SILENT_FRAME
    if len(frame) == FRAME_BYTES:
        return frame
    if len(frame) > FRAME_BYTES:
        return frame[:FRAME_BYTES]
    return frame + bytes([0xFF]) * (FRAME_BYTES - len(frame))


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def to_mp3(wav_path: Path, mp3_path: Path | None = None) -> Path | None:
    """Transcode to MP3 (the submission format). Returns None if ffmpeg is absent."""
    if not have_ffmpeg():
        return None
    mp3_path = mp3_path or wav_path.with_suffix(".mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-af", "highpass=f=80,loudnorm=I=-18:TP=-2:LRA=11",
            "-c:a", "libmp3lame", "-q:a", "4",
            str(mp3_path),
        ],
        check=True,
    )
    return mp3_path
