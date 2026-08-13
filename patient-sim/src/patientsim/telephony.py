"""Twilio: place the outbound call and wire its audio to our websocket.

``<Connect><Stream>`` gives a bidirectional stream: Twilio sends us the far
end's audio and plays back whatever we send it. TwiML is passed inline on
``calls.create`` rather than served from a webhook, which removes one public
endpoint and one round trip from the setup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from twilio.rest import Client

from .config import Settings

log = logging.getLogger("patientsim.telephony")


def make_client(settings: Settings) -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def build_twiml(settings: Settings, call_id: str) -> str:
    url = escape(f"{settings.ws_url()}?call_id={call_id}")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{url}" /></Connect>'
        "</Response>"
    )


def place_call(settings: Settings, call_id: str):
    """Dial the assessment number. Guarded so a bad env var cannot dial elsewhere."""
    to = settings.target_number
    if not to.startswith("+"):
        raise ValueError(f"TARGET_NUMBER must be E.164, got {to!r}")

    client = make_client(settings)
    kwargs = {
        "to": to,
        "from_": settings.twilio_from_number,
        "twiml": build_twiml(settings, call_id),
        "time_limit": settings.max_call_seconds + 30,
    }
    if settings.twilio_recording:
        # Belt and braces: we record locally too, but Twilio's copy is a useful
        # cross-check if the local stream ever drops frames.
        kwargs["record"] = True
        kwargs["recording_channels"] = "dual"

    call = client.calls.create(**kwargs)
    log.info("placed call %s -> %s (call_id=%s)", call.sid, to, call_id)
    return call


def hangup(settings: Settings, call_sid: str) -> None:
    if not call_sid:
        return
    try:
        make_client(settings).calls(call_sid).update(status="completed")
    except Exception as exc:  # noqa: BLE001 - already finished is fine
        log.debug("hangup(%s): %s", call_sid, exc)


def fetch_twilio_recording(settings: Settings, call_sid: str, dest: Path) -> Path | None:
    """Download Twilio's own dual-channel copy of the call, if one exists."""
    if not call_sid:
        return None
    try:
        client = make_client(settings)
        recordings = list(client.calls(call_sid).recordings.list(limit=1))
        if not recordings:
            return None
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Recordings/{recordings[0].sid}.mp3"
        )
        resp = requests.get(
            url,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=60,
        )
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch Twilio recording for %s: %s", call_sid, exc)
        return None
