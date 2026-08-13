"""Command line entry point.

    python -m patientsim run --all          # every scenario, then build the report
    python -m patientsim run -s refill-01   # one scenario
    python -m patientsim report             # rebuild the report from saved calls
    python -m patientsim check              # verify credentials and tooling

Everything runs in one process: the websocket server, the tunnel and the call
loop. That keeps the "one command after setup" promise and means the call
placer and the media handler can share state through a plain dict.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import uvicorn

from . import registry, telephony
from .analyze import build_report, triage_call
from .config import Settings, get_settings
from .recorder import have_ffmpeg, to_mp3
from .scenarios import Scenario, load_all, load_one
from .server import app
from .transcript import CallRecord, mmss

log = logging.getLogger("patientsim")

CONNECT_TIMEOUT = 60.0


# --------------------------------------------------------------------------
# infrastructure
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def serving(settings: Settings):
    """Run the websocket server, and a tunnel if we were asked to make one."""
    tunnel = None
    if settings.use_ngrok:
        from pyngrok import ngrok  # imported lazily; optional dependency

        tunnel = ngrok.connect(settings.port, "http")
        settings.public_base_url = tunnel.public_url
        log.info("ngrok tunnel: %s", tunnel.public_url)

    if not settings.public_base_url:
        raise RuntimeError(
            "No PUBLIC_BASE_URL set and USE_NGROK is false. Twilio needs a public "
            "wss:// address to stream audio to. Either run `ngrok http 8080` and "
            "set PUBLIC_BASE_URL, or set USE_NGROK=true."
        )

    config = uvicorn.Config(app, host="0.0.0.0", port=settings.port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    log.info("media bridge listening on %s", settings.ws_url())
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if tunnel is not None:
            from pyngrok import ngrok

            ngrok.disconnect(tunnel.public_url)


def next_index(artifacts_dir: Path) -> int:
    return len(list(artifacts_dir.glob("call-*.json"))) + 1


# --------------------------------------------------------------------------
# one call
# --------------------------------------------------------------------------


async def run_call(settings: Settings, scenario: Scenario, index: int) -> CallRecord:
    call_id = uuid.uuid4().hex[:12]
    record = CallRecord(
        call_id=call_id,
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        index=index,
    )
    wav_path = settings.artifacts_dir / f"{record.slug}.wav"
    slot = registry.CallSlot(call_id=call_id, scenario=scenario, record=record, wav_path=wav_path)
    registry.register(slot)

    print(f"\n[{index:02d}] {scenario.title}  ({scenario.id})")
    call = None
    try:
        call = telephony.place_call(settings, call_id)
        record.twilio_call_sid = call.sid

        try:
            await asyncio.wait_for(slot.connected.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            record.error = "Twilio never opened the media stream (call not answered?)"
            record.outcome = "no_answer"
            print("     no media stream within 60s - giving up on this call")
            return record

        print("     connected, talking...")
        limit = (scenario.max_seconds or settings.max_call_seconds) + 60
        try:
            await asyncio.wait_for(slot.finished.wait(), timeout=limit)
        except asyncio.TimeoutError:
            record.error = "bridge did not finish within the limit"
            record.outcome = "timeout"
    finally:
        if call is not None:
            telephony.hangup(settings, call.sid)
        registry.release(call_id)

    await _save_artifacts(settings, record, wav_path)
    print(
        f"     {mmss(record.duration_seconds)}  outcome={record.outcome}  "
        f"turns={len(record.turns)}  flags={len(record.flags)}"
    )
    return record


async def _save_artifacts(settings: Settings, record: CallRecord, wav_path: Path) -> None:
    if wav_path.exists():
        record.audio_paths["wav"] = str(wav_path)
        mp3 = await asyncio.to_thread(to_mp3, wav_path)
        if mp3:
            record.audio_paths["mp3"] = str(mp3)
        else:
            print("     ffmpeg not found - MP3 not produced (WAV kept)")

    if settings.twilio_recording and record.twilio_call_sid:
        # Twilio finalises recordings a few seconds after the call ends.
        await asyncio.sleep(5)
        dest = settings.artifacts_dir / f"{record.slug}-twilio.mp3"
        got = await asyncio.to_thread(
            telephony.fetch_twilio_recording, settings, record.twilio_call_sid, dest
        )
        if got:
            record.audio_paths["twilio_mp3"] = str(got)

    record.write(settings.artifacts_dir)


# --------------------------------------------------------------------------
# batches and reporting
# --------------------------------------------------------------------------


async def run_batch(settings: Settings, scenarios: list[Scenario], repeat: int) -> list[CallRecord]:
    records: list[CallRecord] = []
    index = next_index(settings.artifacts_dir)
    plan = [s for _ in range(repeat) for s in scenarios]

    async with serving(settings):
        for i, scenario in enumerate(plan):
            record = await run_call(settings, scenario, index)
            records.append(record)
            index += 1
            if i < len(plan) - 1:
                await asyncio.sleep(settings.inter_call_seconds)
    return records


def load_saved_records(settings: Settings) -> list[CallRecord]:
    records: list[CallRecord] = []
    for path in sorted(settings.artifacts_dir.glob("call-*.json")):
        raw = json.loads(path.read_text())
        record = CallRecord(
            call_id=raw.get("call_id", path.stem),
            scenario_id=raw.get("scenario_id", ""),
            scenario_title=raw.get("scenario_title", ""),
            index=raw.get("index", 0),
        )
        record.twilio_call_sid = raw.get("twilio_call_sid", "")
        record.duration_seconds = raw.get("duration_seconds", 0.0)
        record.outcome = raw.get("outcome", "")
        record.agent_latencies_ms = raw.get("agent_latencies_ms", [])
        record.barge_ins = raw.get("barge_ins", 0)
        from .transcript import Flag, Turn

        record.turns = [Turn(**t) for t in raw.get("turns", [])]
        record.flags = [Flag(**f) for f in raw.get("flags", [])]
        records.append(record)
    return records


def do_report(settings: Settings, records: list[CallRecord] | None = None) -> Path:
    records = records or load_saved_records(settings)
    if not records:
        raise SystemExit("No saved calls in artifacts/. Run some calls first.")

    goals = {s.id: s.goal for s in load_all()}
    triaged: dict[str, list] = {}
    print(f"\nTriaging {len(records)} transcripts with {settings.analysis_model}...")
    for record in records:
        triaged[record.call_id] = triage_call(settings, record, goals.get(record.scenario_id, ""))

    out = build_report(records, triaged, settings.artifacts_dir.parent / "BUG_REPORT.md")
    print(f"wrote {out}")
    return out


def do_check(settings: Settings) -> int:
    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else 'XX'}] {label}{('  - ' + detail) if detail else ''}")

    print("Environment")
    line("Twilio credentials", bool(settings.twilio_account_sid and settings.twilio_auth_token))
    line("Twilio from number", settings.twilio_from_number.startswith("+"), settings.twilio_from_number)
    line("Target number", settings.target_number == "+18054398008", settings.target_number)
    line("OpenAI key", settings.openai_api_key.startswith("sk-"))
    line("ffmpeg (needed for MP3)", have_ffmpeg())
    line(
        "public URL",
        bool(settings.public_base_url) or settings.use_ngrok,
        settings.public_base_url or "USE_NGROK=true",
    )

    print("\nScenarios")
    try:
        scenarios = load_all()
        line(f"{len(scenarios)} loaded", len(scenarios) >= 10)
    except Exception as exc:  # noqa: BLE001
        line("scenario files", False, str(exc))

    print("\nTwilio API")
    try:
        acct = telephony.make_client(settings).api.accounts(settings.twilio_account_sid).fetch()
        line("account reachable", True, acct.friendly_name)
    except Exception as exc:  # noqa: BLE001
        line("account reachable", False, str(exc))

    print("\nOpenAI API")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        available = {m.id for m in client.models.list()}
        line(f"realtime model {settings.realtime_model}", settings.realtime_model in available)
        line(f"analysis model {settings.analysis_model}", settings.analysis_model in available)
    except Exception as exc:  # noqa: BLE001
        line("models reachable", False, str(exc))

    print("\n" + ("All good." if ok else "Fix the items marked XX before running calls."))
    return 0 if ok else 1


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="patientsim")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="place calls")
    group = run_p.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--scenario", help="scenario id")
    group.add_argument("-a", "--all", action="store_true", help="every scenario")
    run_p.add_argument("-n", "--repeat", type=int, default=1, help="passes over the set")
    run_p.add_argument("--no-report", action="store_true")

    sub.add_parser("report", help="rebuild BUG_REPORT.md from saved calls")
    sub.add_parser("check", help="validate configuration")
    sub.add_parser("scenarios", help="list scenarios")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "check":
        return do_check(settings)

    if args.command == "scenarios":
        for scenario in load_all():
            print(f"{scenario.id:<24} {scenario.category:<14} {scenario.title}")
        return 0

    if args.command == "report":
        do_report(settings)
        return 0

    scenarios = load_all() if args.all else [load_one(args.scenario)]
    started = time.time()
    records = asyncio.run(run_batch(settings, scenarios, args.repeat))
    print(f"\n{len(records)} calls in {mmss(time.time() - started)}")

    if not args.no_report:
        do_report(settings, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
