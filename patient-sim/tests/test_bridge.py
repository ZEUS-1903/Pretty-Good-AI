"""Bridge tests with a fake Twilio socket and a fake Realtime session.

These cover the parts that are painful to debug over a real phone line: that
outbound audio is paced one frame per inbound frame, that the recording stays
aligned, that a barge-in flushes the queue and truncates the model's item at the
right offset, and that the tool calls do what they claim.

Run with:  python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from patientsim import bridge as bridge_mod  # noqa: E402
from patientsim.audio import FRAME_BYTES  # noqa: E402
from patientsim.config import Settings  # noqa: E402
from patientsim.scenarios import Persona, Scenario  # noqa: E402
from patientsim.transcript import CallRecord  # noqa: E402

AGENT_FRAME = base64.b64encode(bytes([0x20]) * FRAME_BYTES).decode()


class FakeTwilioWS:
    """Replays a scripted stream and captures everything the bridge sends."""

    def __init__(self, media_frames: int) -> None:
        self.sent: list[dict] = []
        self._script: list[dict] = [
            {"event": "connected"},
            {
                "event": "start",
                "start": {"streamSid": "MZ123", "callSid": "CA123", "customParameters": {}},
            },
        ]
        self._script += [
            {"event": "media", "media": {"track": "inbound", "payload": AGENT_FRAME}}
            for _ in range(media_frames)
        ]
        self._script.append({"event": "stop"})
        self._i = 0

    async def receive_text(self) -> str:
        if self._i >= len(self._script):
            await asyncio.sleep(3600)
        msg = self._script[self._i]
        self._i += 1
        # Pace media like a real stream (compressed 20 ms -> 5 ms so tests are fast).
        await asyncio.sleep(0.005 if msg.get("event") == "media" else 0)
        return json.dumps(msg)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def media_payloads(self) -> list[bytes]:
        return [
            base64.b64decode(m["media"]["payload"])
            for m in self.sent
            if m.get("event") == "media"
        ]


class FakeRealtime:
    """Stands in for RealtimeSession; the test pushes server events by hand."""

    instance: "FakeRealtime | None" = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.audio_in: list[str] = []
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        FakeRealtime.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def events(self):
        while True:
            yield await self._queue.get()

    def push(self, event: dict) -> None:
        self._queue.put_nowait(event)

    async def append_audio(self, b64: str) -> None:
        self.audio_in.append(b64)

    async def send(self, event: dict) -> None:
        self.sent.append(event)

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.sent.append({"type": "truncate", "item_id": item_id, "audio_end_ms": audio_end_ms})

    async def function_result(self, call_id: str, output: dict) -> None:
        self.sent.append({"type": "function_result", "call_id": call_id, "output": output})

    async def create_response(self, instructions: str | None = None) -> None:
        self.sent.append({"type": "response.create"})


def make_settings() -> Settings:
    return Settings(
        twilio_account_sid="AC",
        twilio_auth_token="t",
        twilio_from_number="+15550000000",
        target_number="+18054398008",
        public_base_url="https://x.test",
        use_ngrok=False,
        port=8080,
        openai_api_key="sk-test",
        realtime_model="gpt-realtime-2.1",
        realtime_voice="marin",
        transcription_model="gpt-4o-transcribe",
        analysis_model="gpt-5.6-terra",
        max_call_seconds=30,
        hangup_grace_seconds=0.0,
        inter_call_seconds=0,
        twilio_recording=False,
        artifacts_dir=Path("/tmp/patientsim-test"),
    )


def make_scenario() -> Scenario:
    return Scenario(
        id="t",
        title="test",
        category="test",
        goal="book something",
        persona=Persona(name="Test Patient", dob="Jan 1 1990", phone="555-0100"),
    )


async def drive(media_frames: int, script):
    """Run the bridge against the fakes, feeding realtime events via `script`."""
    monkey = bridge_mod.RealtimeSession
    bridge_mod.RealtimeSession = FakeRealtime
    try:
        ws = FakeTwilioWS(media_frames)
        record = CallRecord(call_id="c", scenario_id="t", scenario_title="test", index=1)
        wav = Path("/tmp/patientsim-test/out.wav")
        wav.parent.mkdir(parents=True, exist_ok=True)
        br = bridge_mod.CallBridge(ws, make_scenario(), make_settings(), record, wav)
        runner = asyncio.create_task(br.run())
        await asyncio.sleep(0.05)
        await script(br, FakeRealtime.instance, ws)
        await asyncio.wait_for(runner, timeout=10)
        return br, ws, record
    finally:
        bridge_mod.RealtimeSession = monkey


@pytest.mark.asyncio
async def test_audio_is_paced_one_frame_per_inbound_frame():
    async def script(br, rt, ws):
        rt.push({"type": "response.created"})
        # Ten frames of bot audio arrive in a single burst.
        rt.push(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_1",
                "delta": base64.b64encode(bytes([0x10]) * FRAME_BYTES * 10).decode(),
            }
        )
        await asyncio.sleep(0.4)

    br, ws, record = await drive(60, script)
    out = ws.media_payloads()
    assert out, "bridge sent no audio to Twilio"
    assert all(len(f) == FRAME_BYTES for f in out), "frames must be exactly 20 ms"
    # A 10-frame burst must not be dumped at once; it is metered out over ticks.
    assert len(out) <= 12
    # One recorder tick per inbound frame, both channels the same length.
    assert br.recorder.frames == len(FakeRealtime.instance.audio_in)
    assert len(br.recorder._left) == len(br.recorder._right)


@pytest.mark.asyncio
async def test_inbound_audio_is_forwarded_untranscoded():
    async def script(br, rt, ws):
        await asyncio.sleep(0.3)

    br, ws, record = await drive(30, script)
    rt = FakeRealtime.instance
    assert len(rt.audio_in) == 30
    assert rt.audio_in[0] == AGENT_FRAME, "payload should pass through byte-for-byte"


@pytest.mark.asyncio
async def test_barge_in_flushes_queue_and_truncates_at_played_offset():
    async def script(br, rt, ws):
        rt.push({"type": "response.created"})
        rt.push(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_9",
                "delta": base64.b64encode(bytes([0x10]) * FRAME_BYTES * 40).decode(),
            }
        )
        await asyncio.sleep(0.05)          # only part of it gets played
        rt.push({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.2)

    br, ws, record = await drive(60, script)

    assert record.barge_ins == 1
    assert not br._outbound, "queued audio must be dropped on barge-in"
    assert any(m.get("event") == "clear" for m in ws.sent), "Twilio buffer must be flushed"

    trunc = [e for e in FakeRealtime.instance.sent if e["type"] == "truncate"]
    assert len(trunc) == 1
    assert trunc[0]["item_id"] == "item_9"
    # Truncation offset must equal what actually reached the phone, and be less
    # than the 800 ms that was generated.
    played = trunc[0]["audio_end_ms"]
    assert 0 < played < 800, played
    assert played == len(ws.media_payloads()) * 20


@pytest.mark.asyncio
async def test_flag_issue_records_and_resumes_the_conversation():
    async def script(br, rt, ws):
        rt.push({"type": "response.created"})
        rt.push(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_1",
                "name": "flag_issue",
                "arguments": json.dumps(
                    {
                        "severity": "high",
                        "category": "hallucination",
                        "summary": "Invented an address",
                        "agent_quote": "We're at 12 Fake Street",
                    }
                ),
            }
        )
        rt.push({"type": "response.done"})
        await asyncio.sleep(0.2)

    br, ws, record = await drive(30, script)
    assert len(record.flags) == 1
    assert record.flags[0].severity == "high"
    sent = FakeRealtime.instance.sent
    assert any(e["type"] == "function_result" for e in sent)
    # No audio was produced in that response, so the model is nudged to continue.
    assert any(e["type"] == "response.create" for e in sent)


@pytest.mark.asyncio
async def test_duplicate_tool_call_ids_are_ignored():
    async def script(br, rt, ws):
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "dup",
            "name": "flag_issue",
            "arguments": json.dumps({"severity": "low", "category": "other", "summary": "x"}),
        }
        rt.push({"type": "response.created"})
        rt.push(dict(event))
        rt.push(dict(event))
        await asyncio.sleep(0.2)

    br, ws, record = await drive(30, script)
    assert len(record.flags) == 1


@pytest.mark.asyncio
async def test_end_call_drains_audio_then_stops():
    async def script(br, rt, ws):
        rt.push({"type": "response.created"})
        rt.push(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_2",
                "delta": base64.b64encode(bytes([0x10]) * FRAME_BYTES * 3).decode(),
            }
        )
        rt.push(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "bye",
                "name": "end_call",
                "arguments": json.dumps({"outcome": "goal_achieved", "notes": "booked"}),
            }
        )
        await asyncio.sleep(0.3)

    br, ws, record = await drive(200, script)
    assert record.outcome == "goal_achieved"
    assert record.outcome_notes == "booked"
    assert br.stop.is_set()
    # The goodbye made it out before hanging up.
    assert len(ws.media_payloads()) >= 3


@pytest.mark.asyncio
async def test_transcripts_from_both_sides_are_captured():
    async def script(br, rt, ws):
        rt.push(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Thanks for calling, how can I help?",
            }
        )
        rt.push(
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "Hi, I'd like to book an appointment.",
            }
        )
        await asyncio.sleep(0.2)

    br, ws, record = await drive(30, script)
    speakers = {t.speaker for t in record.turns}
    assert speakers == {"AGENT", "PATIENT"}
    assert all(t.at >= 0 for t in record.turns)
