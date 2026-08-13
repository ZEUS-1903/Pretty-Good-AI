# patient-sim

An automated patient that phones a voice AI receptionist, holds a real
conversation with it, records both sides, and reports what it broke.

It places outbound PSTN calls through Twilio and drives the conversation with
OpenAI's Realtime speech-to-speech model. Each call runs a scenario — a persona,
a goal, and a list of things the patient must get answered — and the bot steers
the conversation toward that goal the way an actual caller would, rather than
reading a script and hanging up.

```
python -m patientsim run --all
```

That places one call per scenario, writes an MP3, a transcript and a structured
JSON record for each, then triages everything into `BUG_REPORT.md`.

---

## Setup

**Prerequisites**

- Python 3.11+
- `ffmpeg` on your PATH (produces the MP3s — `brew install ffmpeg` /
  `apt install ffmpeg`)
- A Twilio account with one voice-capable number
- An OpenAI API key with access to a Realtime model
- `ngrok` or any other public HTTPS tunnel

**Install**

```bash
git clone <this repo> && cd patient-sim
make setup                 # venv + dependencies
cp .env.example .env       # then fill it in
```

**Expose the media bridge.** Twilio streams audio to a `wss://` address, so the
server needs to be reachable from the internet. Either run your own tunnel:

```bash
ngrok http 8080
# put the https URL in PUBLIC_BASE_URL
```

…or set `USE_NGROK=true` and the runner opens and closes one for you.

**Verify before spending money**

```bash
make check
```

This checks credentials, confirms your account can actually use the models named
in `.env`, finds `ffmpeg`, loads the scenarios, and reaches both APIs. Fix
anything marked `XX` first — a bad model name fails at the moment the call
connects, which wastes a call.

---

## Running

```bash
make calls                          # every scenario, then build the report
make one S=weekend-request          # a single scenario
make scenarios                      # list what is available
make report                         # rebuild BUG_REPORT.md from saved calls
```

Or directly:

```bash
python -m patientsim run --all --repeat 2   # two passes over the set
python -m patientsim run -s urgent-symptom
```

Calls are numbered sequentially and never overwrite earlier ones, so you can add
runs incrementally as you iterate.

## What you get

For every call, in `artifacts/`:

| File | Contents |
| --- | --- |
| `call-07-weekend-request.mp3` | Stereo recording. Left channel is the patient bot, right is the agent under test. |
| `call-07-weekend-request.txt` | Readable transcript, `[m:ss] SPEAKER: text`, with issues flagged during the call listed at the bottom. |
| `call-07-weekend-request.json` | Same content structured, plus per-call metrics: turn counts, barge-ins, agent response latencies, event log. |
| `call-07-weekend-request-twilio.mp3` | Twilio's own dual-channel copy, as a cross-check. Optional (`TWILIO_RECORDING`). |

And at the repo root, `BUG_REPORT.md`: every issue ranked by severity, each one
pointing at a call file and a timestamp.

## Scenarios

Scenarios are YAML, one file each in `scenarios/`. Adding a test case means
adding a file — no code.

| id | What it probes |
| --- | --- |
| `schedule-new-patient` | Baseline booking, new-patient intake |
| `reschedule` | Moving a slot; is the original actually released? |
| `cancel-and-rebook` | Two tasks in one call without losing the first |
| `medication-refill` | Refill capture; must not give dosing advice |
| `hours-location-insurance` | Facts, plus a deliberately wrong read-back to see if it corrects you |
| `weekend-request` | Asks for Sunday; does it check office hours before confirming? |
| `interruption` | Deliberate barge-in, changing the request mid-sentence |
| `vague-caller` | Ambiguity and long silences; does it invent a reason for the call? |
| `urgent-symptom` | Chest tightness mentioned casually; does it escalate or just book? |
| `identity-mismatch` | Wrong DOB; does it leak details before verification passes? |
| `multi-intent` | Three requests at once and a date-arithmetic trap |
| `noise-and-repair` | Hard-to-spell surname, repeated corrections, a repair request |

A scenario controls the persona, the goal, what must be answered, what counts as
a bug, and the turn-taking mode. Most use semantic VAD for polite turn-taking;
`vague-caller` switches to server VAD so it can use an idle timeout and sit
silently to see whether the agent re-prompts.

## How bugs get found

Two passes, kept separate on purpose.

**During the call**, the patient bot has a `flag_issue` tool. When the agent
contradicts itself, invents a fact or confirms something impossible, the bot
flags it silently and keeps talking. These flags carry the timestamp of the
moment it happened, which makes them straightforward to verify against the
audio.

**After the call**, the full transcript goes to a reasoning model that looks for
what is only visible in hindsight — a confirmation that contradicts something
said ninety seconds earlier, a booking that was never actually completed. It is
instructed to return nothing rather than pad the list.

Both feed one ranked report. **Every entry is a candidate, not a finding.**
Machine transcription is imperfect. Listen to the audio at each timestamp and
delete anything that does not hold up before you submit.

## Costs

A three-minute call is roughly $0.02 in Twilio charges and the bulk of the spend
is Realtime audio tokens. Twelve calls lands comfortably inside a $20 budget.
To cut it further: set `REALTIME_MODEL=gpt-realtime-2.1-mini`, and lower
`MAX_CALL_SECONDS`. `MAX_CALL_SECONDS` and Twilio's own `time_limit` both cap
runaway calls.

## Safety

`TARGET_NUMBER` defaults to the assessment line and `make check` warns if it has
been changed. The bot dials that number and nothing else. All personas are
fictional; no real personal data is ever sent.

## Tests

```bash
python -m pytest tests -q
```

The suite drives the bridge against a fake Twilio socket and a fake Realtime
session, covering the parts that are miserable to debug over a live phone line:
audio pacing, recorder alignment, barge-in truncation offsets, tool-call
handling and hangup draining.

## Layout

```
src/patientsim/
  bridge.py      audio bridge: Twilio <-> Realtime, pacing, barge-in, recording
  realtime.py    OpenAI Realtime (GA) websocket client
  telephony.py   Twilio dialing, TwiML, recording retrieval
  server.py      FastAPI websocket endpoint
  runner.py      CLI: server + tunnel + call loop + reporting
  prompts.py     persona construction and tool definitions
  scenarios.py   YAML scenario loader
  recorder.py    aligned stereo WAV -> MP3
  transcript.py  call record and transcript writers
  analyze.py     post-call triage and report assembly
  audio.py       G.711 mu-law helpers
scenarios/       one YAML file per test case
artifacts/       recordings, transcripts, JSON records
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for why it is built this way.
