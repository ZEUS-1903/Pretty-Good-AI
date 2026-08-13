"""Shared state between the call placer and the media-stream websocket handler.

The runner and the webserver live in one process, so a dict is all the
coordination that is needed. Each outbound call carries a ``call_id`` through
the TwiML ``<Parameter>`` element; when Twilio opens the media stream it hands
that id back and the handler can pick up the right scenario and call record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .scenarios import Scenario
from .transcript import CallRecord


@dataclass
class CallSlot:
    call_id: str
    scenario: Scenario
    record: CallRecord
    wav_path: Path
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


REGISTRY: dict[str, CallSlot] = {}


def register(slot: CallSlot) -> None:
    REGISTRY[slot.call_id] = slot


def get(call_id: str) -> CallSlot | None:
    return REGISTRY.get(call_id)


def release(call_id: str) -> None:
    REGISTRY.pop(call_id, None)
