"""The audio bridge: Twilio Media Stream <-> OpenAI Realtime session.

Design notes
------------
*The inbound stream is the clock.* Twilio delivers one 20 ms mu-law frame every
20 ms for the life of the call. Every inbound frame drives exactly one tick:
forward it to the model, emit at most one frame of the patient's audio back to
Twilio, and append one frame to each channel of the recorder. That gives
real-time pacing, a self-correcting clock and a perfectly aligned stereo
recording without a second timer task fighting the event loop.

*Barge-in is handled explicitly.* When the agent starts talking over us we drop
our queued audio, tell Twilio to flush anything it already holds, and truncate
the model's own conversation item at the number of milliseconds that actually
reached the phone. Skipping that last step is the classic bug: the model keeps
believing it finished a sentence nobody heard and its next turn makes no sense.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from typing import Any

from .audio import FRAME_BYTES, FRAME_MS
from .config import Settings
from .prompts import TOOLS, build_instructions
from .realtime import RealtimeSession, turn_detection_config
from .recorder import StereoRecorder
from .scenarios import Scenario
from .transcript import CallRecord

log = logging.getLogger("patientsim.bridge")

DEAD_AIR_SECONDS = 5.0


class CallBridge:
    def __init__(
        self,
        twilio_ws: Any,
        scenario: Scenario,
        settings: Settings,
        record: CallRecord,
        wav_path,
    ) -> None:
        self.ws = twilio_ws
        self.scenario = scenario
        self.settings = settings
        self.record = record
        self.recorder = StereoRecorder(wav_path)

        self.stream_sid: str = ""
        self.stop = asyncio.Event()

        self._outbound = bytearray()          # patient audio waiting to go to the phone
        self._inbound_frames: deque[bytes] = deque(maxlen=200)
        self._rt: RealtimeSession | None = None

        self._response_active = False
        self._response_had_audio = False
        self._current_item_id: str | None = None
        self._sent_ms_this_response = 0
        self._handled_calls: set[str] = set()
        self._needs_continue = False

        self._agent_speaking = False
        self._last_bot_frame_at: float | None = None
        self._last_voice_at = time.monotonic()
        self._dead_air_noted = False

        self.ending = False
        self._drain_deadline: float | None = None

    # -- entry point ------------------------------------------------------

    async def run(self) -> None:
        await self._await_start()
        self.record.start_clock()

        instructions = build_instructions(self.scenario)
        td = turn_detection_config(
            self.scenario.turn_detection,
            eagerness=self.scenario.eagerness,
            idle_timeout_ms=self.scenario.idle_timeout_ms,
        )

        async with RealtimeSession(
            api_key=self.settings.openai_api_key,
            model=self.settings.realtime_model,
            voice=self.settings.realtime_voice,
            instructions=instructions,
            tools=TOOLS,
            transcription_model=self.settings.transcription_model,
            turn_detection=td,
        ) as rt:
            self._rt = rt
            limit = self.scenario.max_seconds or self.settings.max_call_seconds
            tasks = [
                asyncio.create_task(self._pump_twilio(), name="twilio"),
                asyncio.create_task(self._pump_realtime(), name="realtime"),
                asyncio.create_task(self._watchdog(limit), name="watchdog"),
            ]
            try:
                await self.stop.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        self.record.duration_seconds = self.recorder.duration_seconds
        self.recorder.write_wav()

    async def _await_start(self) -> None:
        """Consume Twilio frames until the stream 'start' event arrives."""
        while True:
            raw = await self.ws.receive_text()
            msg = json.loads(raw)
            if msg.get("event") == "start":
                start = msg["start"]
                self.stream_sid = start["streamSid"]
                self.record.twilio_call_sid = start.get("callSid", "")
                self.record.note("stream_start", stream_sid=self.stream_sid)
                return
            if msg.get("event") == "stop":
                self.stop.set()
                return

    # -- Twilio side ------------------------------------------------------

    async def _pump_twilio(self) -> None:
        try:
            while not self.stop.is_set():
                msg = json.loads(await self.ws.receive_text())
                event = msg.get("event")

                if event == "media":
                    payload = msg["media"]["payload"]
                    await self._on_inbound_frame(payload)
                elif event == "stop":
                    self.record.note("stream_stop")
                    self.stop.set()
                elif event == "dtmf":
                    self.record.note("dtmf", digit=msg.get("dtmf", {}).get("digit"))
                elif event == "mark":
                    self.record.note("mark", name=msg.get("mark", {}).get("name"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the call is over either way
            log.info("twilio stream ended: %s", exc)
            self.stop.set()

    async def _on_inbound_frame(self, payload_b64: str) -> None:
        agent_frame = base64.b64decode(payload_b64)

        # 1. straight through to the model - same codec, no transcoding
        if self._rt is not None:
            await self._rt.append_audio(payload_b64)

        # 2. emit at most one frame of our own audio, keeping real-time pacing
        bot_frame: bytes | None = None
        if len(self._outbound) >= FRAME_BYTES:
            bot_frame = bytes(self._outbound[:FRAME_BYTES])
            del self._outbound[:FRAME_BYTES]
        elif self._outbound and self.ending:
            bot_frame = bytes(self._outbound).ljust(FRAME_BYTES, b"\xff")
            self._outbound.clear()

        if bot_frame is not None:
            await self._send_media(bot_frame)
            self._sent_ms_this_response += FRAME_MS
            self._last_bot_frame_at = time.monotonic()
            self._last_voice_at = time.monotonic()
            self._dead_air_noted = False

        # 3. one aligned tick into the recording
        self.recorder.tick(bot_frame, agent_frame)

        self._check_dead_air()
        self._check_drain()

    async def _send_media(self, ulaw: bytes) -> None:
        await self.ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(ulaw).decode()},
                }
            )
        )

    async def _clear_twilio(self) -> None:
        await self.ws.send_text(
            json.dumps({"event": "clear", "streamSid": self.stream_sid})
        )

    def _check_dead_air(self) -> None:
        if self._agent_speaking or self._dead_air_noted:
            return
        gap = time.monotonic() - self._last_voice_at
        if gap > DEAD_AIR_SECONDS:
            self._dead_air_noted = True
            self.record.note("dead_air", seconds=round(gap, 1))
            self.record.add_flag(
                severity="medium",
                category="latency_or_dead_air",
                summary=f"{gap:.1f}s of silence with no audio from either side.",
                source="post_call",
            )

    def _check_drain(self) -> None:
        if not self.ending:
            return
        if self._outbound:
            return
        if self._drain_deadline is None:
            self._drain_deadline = time.monotonic() + self.settings.hangup_grace_seconds
        elif time.monotonic() >= self._drain_deadline:
            self.stop.set()

    # -- Realtime side ----------------------------------------------------

    async def _pump_realtime(self) -> None:
        assert self._rt is not None
        try:
            async for event in self._rt.events():
                await self._handle_realtime_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime stream ended: %s", exc)
            self.record.error = f"realtime: {exc}"
            self.stop.set()

    async def _handle_realtime_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype == "response.output_audio.delta":
            self._outbound += base64.b64decode(event["delta"])
            self._response_had_audio = True
            self._current_item_id = event.get("item_id", self._current_item_id)

        elif etype == "response.created":
            self._response_active = True
            self._response_had_audio = False
            self._sent_ms_this_response = 0

        elif etype == "response.done":
            self._response_active = False
            await self._maybe_continue()

        elif etype == "response.output_audio_transcript.done":
            self.record.add_turn("PATIENT", event.get("transcript", ""))

        elif etype == "conversation.item.input_audio_transcription.completed":
            self.record.add_turn("AGENT", event.get("transcript", ""))

        elif etype == "input_audio_buffer.speech_started":
            await self._on_agent_speech_started()

        elif etype == "input_audio_buffer.speech_stopped":
            self._agent_speaking = False
            self._last_voice_at = time.monotonic()

        elif etype == "input_audio_buffer.timeout_triggered":
            self.record.note("idle_timeout")

        elif etype == "response.function_call_arguments.done":
            await self._on_function_call(
                event.get("call_id", ""), event.get("name", ""), event.get("arguments", "{}")
            )

        elif etype == "error":
            detail = event.get("error", {})
            # Truncate/cancel races are expected and harmless; keep them out of the log.
            if detail.get("code") not in {"response_cancel_not_active", "item_truncate_invalid"}:
                log.warning("realtime error: %s", detail)
                self.record.note("realtime_error", detail=detail)

    async def _on_agent_speech_started(self) -> None:
        self._agent_speaking = True
        self._last_voice_at = time.monotonic()
        self._dead_air_noted = False

        # Agent response latency: gap between our last emitted frame and their
        # first word. Includes VAD detection overhead, so treat it as a floor.
        if self._last_bot_frame_at is not None:
            gap_ms = int((time.monotonic() - self._last_bot_frame_at) * 1000)
            if 0 < gap_ms < 30_000:
                self.record.agent_latencies_ms.append(gap_ms)
            self._last_bot_frame_at = None

        # Barge-in: they talked over us.
        if self._outbound or self._response_active:
            self.record.barge_ins += 1
            self.record.note("barge_in", played_ms=self._sent_ms_this_response)
            self._outbound.clear()
            await self._clear_twilio()
            if self._rt is not None and self._current_item_id and self._response_active:
                await self._rt.truncate(self._current_item_id, self._sent_ms_this_response)

    async def _on_function_call(self, call_id: str, name: str, arguments: str) -> None:
        if not call_id or call_id in self._handled_calls:
            return
        self._handled_calls.add(call_id)
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        assert self._rt is not None
        if name == "flag_issue":
            self.record.add_flag(
                severity=args.get("severity", "medium"),
                category=args.get("category", "other"),
                summary=args.get("summary", ""),
                agent_quote=args.get("agent_quote", ""),
                expected=args.get("expected", ""),
            )
            log.info("flag [%s] %s", args.get("severity"), args.get("summary"))
            await self._rt.function_result(call_id, {"recorded": True})
            self._needs_continue = True

        elif name == "end_call":
            self.record.outcome = args.get("outcome", "other")
            self.record.outcome_notes = args.get("notes", "")
            self.record.note("end_call", **args)
            await self._rt.function_result(call_id, {"ok": True})
            self.ending = True

        else:
            await self._rt.function_result(call_id, {"error": f"unknown tool {name}"})

    async def _maybe_continue(self) -> None:
        """Nudge the model back into the conversation after a silent tool call."""
        if not self._needs_continue or self.ending:
            self._needs_continue = False
            return
        self._needs_continue = False
        if self._response_had_audio:
            return  # it spoke and flagged in the same turn; nothing to resume
        if self._rt is not None:
            await self._rt.create_response(
                "Continue the call naturally as the patient. Do not mention the note you just made."
            )

    # -- safety net -------------------------------------------------------

    async def _watchdog(self, limit_seconds: int) -> None:
        try:
            await asyncio.sleep(limit_seconds)
            self.record.note("time_limit_reached", seconds=limit_seconds)
            if self.record.outcome == "incomplete":
                self.record.outcome = "timeout"
            self.stop.set()
        except asyncio.CancelledError:
            raise
