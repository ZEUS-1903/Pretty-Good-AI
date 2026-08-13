"""Websocket endpoint that Twilio connects to for bidirectional audio."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from . import registry
from .bridge import CallBridge
from .config import get_settings

log = logging.getLogger("patientsim.server")

app = FastAPI(title="patient-sim media bridge")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "active_calls": str(len(registry.REGISTRY))}


@app.websocket("/ws/media")
async def media(ws: WebSocket) -> None:
    await ws.accept()

    # Twilio sends "connected" then "start"; the call_id rides along in the
    # start frame's customParameters. We peek at it here without consuming the
    # start frame, so the bridge can read the stream metadata itself.
    call_id = ws.query_params.get("call_id", "")
    slot = registry.get(call_id) if call_id else None
    if slot is None:
        log.error("media stream for unknown call_id=%r; closing", call_id)
        await ws.close(code=1008)
        return

    slot.connected.set()
    settings = get_settings()
    bridge = CallBridge(ws, slot.scenario, settings, slot.record, slot.wav_path)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        log.info("call %s: twilio disconnected", call_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("call %s failed", call_id)
        slot.record.error = str(exc)
    finally:
        slot.finished.set()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
