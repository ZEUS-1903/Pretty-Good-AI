"""In-memory record of a single call, plus the transcript writers.

Both sides of the conversation are captured. The agent's words come from the
Realtime input-transcription pass; the patient's words come from the model's own
output transcript, which is exact. Timestamps are seconds from the moment the
media stream opened, so they line up with the audio file.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def mmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):d}:{int(seconds % 60):02d}"


@dataclass
class Turn:
    at: float
    speaker: str  # "PATIENT" (our bot) or "AGENT" (system under test)
    text: str


@dataclass
class Flag:
    at: float
    severity: str
    category: str
    summary: str
    agent_quote: str = ""
    expected: str = ""
    source: str = "in_call"  # in_call | post_call


@dataclass
class CallRecord:
    call_id: str
    scenario_id: str
    scenario_title: str
    index: int
    started_at: float = field(default_factory=time.time)
    twilio_call_sid: str = ""
    turns: list[Turn] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    agent_latencies_ms: list[int] = field(default_factory=list)
    barge_ins: int = 0
    outcome: str = "incomplete"
    outcome_notes: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    audio_paths: dict[str, str] = field(default_factory=dict)

    _t0: float = field(default=0.0, repr=False)

    def start_clock(self) -> None:
        self._t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    def add_turn(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.turns.append(Turn(at=self.now(), speaker=speaker, text=text))

    def add_flag(self, **kwargs: Any) -> None:
        kwargs.setdefault("at", self.now())
        self.flags.append(Flag(**kwargs))

    def note(self, kind: str, **payload: Any) -> None:
        self.events.append({"at": round(self.now(), 3), "kind": kind, **payload})

    # -- writers ----------------------------------------------------------

    @property
    def slug(self) -> str:
        return f"call-{self.index:02d}-{self.scenario_id}"

    def write(self, artifacts_dir: Path) -> dict[str, Path]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        txt_path = artifacts_dir / f"{self.slug}.txt"
        json_path = artifacts_dir / f"{self.slug}.json"

        lines = [
            f"Call {self.index:02d} - {self.scenario_title} [{self.scenario_id}]",
            f"Twilio call SID: {self.twilio_call_sid or 'n/a'}",
            f"Duration: {mmss(self.duration_seconds)}   Outcome: {self.outcome}",
            "-" * 72,
            "",
        ]
        for turn in sorted(self.turns, key=lambda t: t.at):
            lines.append(f"[{mmss(turn.at)}] {turn.speaker}: {turn.text}")
        if self.flags:
            lines += ["", "-" * 72, "ISSUES FLAGGED DURING THE CALL", ""]
            for flag in self.flags:
                lines.append(
                    f"[{mmss(flag.at)}] {flag.severity.upper()} / {flag.category}: {flag.summary}"
                )
                if flag.agent_quote:
                    lines.append(f"          agent said: {flag.agent_quote}")
                if flag.expected:
                    lines.append(f"          expected:   {flag.expected}")
        txt_path.write_text("\n".join(lines) + "\n")

        payload = asdict(self)
        payload.pop("_t0", None)
        payload["metrics"] = self.metrics()
        json_path.write_text(json.dumps(payload, indent=2, default=str))
        return {"txt": txt_path, "json": json_path}

    def metrics(self) -> dict[str, Any]:
        lat = sorted(self.agent_latencies_ms)
        return {
            "turns_total": len(self.turns),
            "turns_agent": sum(1 for t in self.turns if t.speaker == "AGENT"),
            "turns_patient": sum(1 for t in self.turns if t.speaker == "PATIENT"),
            "duration_seconds": round(self.duration_seconds, 1),
            "barge_ins": self.barge_ins,
            "flags": len(self.flags),
            "agent_response_ms_median": lat[len(lat) // 2] if lat else None,
            "agent_response_ms_max": lat[-1] if lat else None,
        }
