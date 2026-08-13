"""Thin wrapper around the OpenAI Realtime API (GA interface).

Notes that cost real debugging time and are easy to get wrong:

* The Realtime *beta* interface was removed from the API in May 2026. This
  client targets the GA shape only: ``session.type = "realtime"``, audio config
  nested under ``session.audio.input`` / ``session.audio.output``, formats as
  objects (``{"type": "audio/pcmu"}``) rather than strings, and the
  ``response.output_audio.*`` event names.
* ``audio/pcmu`` is G.711 mu-law at 8 kHz, which is exactly what Twilio Media
  Streams carries. Selecting it on both ends of the session means there is no
  resampling anywhere in the loop.
* Input transcription is a separate asynchronous pass, not what the model
  literally heard. It is good enough to read and to feed an analyser, but it is
  not ground truth - the recording is.
"""

from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import websockets

REALTIME_URL = "wss://api.openai.com/v1/realtime"


class RealtimeSession:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        voice: str,
        instructions: str,
        tools: list[dict[str, Any]],
        transcription_model: str,
        turn_detection: dict[str, Any],
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._instructions = instructions
        self._tools = tools
        self._transcription_model = transcription_model
        self._turn_detection = turn_detection
        self._ws: websockets.WebSocketClientProtocol | None = None

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> "RealtimeSession":
        url = f"{REALTIME_URL}?model={self._model}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            self._ws = await websockets.connect(url, additional_headers=headers)
        except TypeError:
            # websockets < 14 spells the argument differently.
            self._ws = await websockets.connect(url, extra_headers=headers)
        await self._configure()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _configure(self) -> None:
        await self.send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": self._model,
                    "instructions": self._instructions,
                    "output_modalities": ["audio"],
                    "tools": self._tools,
                    "tool_choice": "auto",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            "noise_reduction": {"type": "far_field"},
                            "transcription": {
                                "model": self._transcription_model,
                                "language": "en",
                            },
                            "turn_detection": self._turn_detection,
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": self._voice,
                            "speed": 1.0,
                        },
                    },
                },
            }
        )

    # -- io ---------------------------------------------------------------

    async def send(self, event: dict[str, Any]) -> None:
        assert self._ws is not None, "session not connected"
        await self._ws.send(json.dumps(event))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._ws is not None, "session not connected"
        async for raw in self._ws:
            yield json.loads(raw)

    # -- convenience ------------------------------------------------------

    async def append_audio(self, ulaw_b64: str) -> None:
        await self.send({"type": "input_audio_buffer.append", "audio": ulaw_b64})

    async def append_audio_bytes(self, ulaw: bytes) -> None:
        await self.append_audio(base64.b64encode(ulaw).decode())

    async def cancel_response(self) -> None:
        await self.send({"type": "response.cancel"})

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Tell the model how much of its own audio the far end actually heard.

        Without this the model believes it finished a sentence that the agent
        talked over, and its next turn references things nobody heard it say.
        """
        await self.send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": max(0, audio_end_ms),
            }
        )

    async def function_result(self, call_id: str, output: dict[str, Any]) -> None:
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output),
                },
            }
        )

    async def create_response(self, instructions: str | None = None) -> None:
        event: dict[str, Any] = {"type": "response.create"}
        if instructions:
            event["response"] = {"instructions": instructions}
        await self.send(event)


def turn_detection_config(
    mode: str, *, eagerness: str = "medium", idle_timeout_ms: int | None = None
) -> dict[str, Any]:
    """Build the VAD block.

    ``semantic`` uses a model to decide the far end has finished a thought. It
    is markedly better at not interrupting a production voice agent, which
    pauses mid-sentence while it looks things up. ``server`` is plain
    energy-based VAD; it is the only mode that supports ``idle_timeout_ms``,
    which is how the silence/hesitation scenarios get the agent to re-prompt.
    """
    if mode == "server":
        cfg: dict[str, Any] = {
            "type": "server_vad",
            "threshold": 0.55,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 700,
            "create_response": True,
            "interrupt_response": True,
        }
        if idle_timeout_ms:
            cfg["idle_timeout_ms"] = max(5000, min(30000, idle_timeout_ms))
        return cfg
    return {
        "type": "semantic_vad",
        "eagerness": eagerness,
        "create_response": True,
        "interrupt_response": True,
    }
