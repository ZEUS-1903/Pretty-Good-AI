# Architecture

## How it works

One process runs everything. The CLI opens a public tunnel and a FastAPI
websocket server, then places an outbound call through Twilio with inline TwiML
containing `<Connect><Stream url="wss://…/ws/media?call_id=…">`. When the callee
answers, Twilio opens a bidirectional websocket to that URL and starts sending
20 ms G.711 mu-law frames. The handler looks up the `call_id`, loads the
scenario, and opens a second websocket to the OpenAI Realtime API configured for
`audio/pcmu` in both directions — the same codec Twilio speaks, so audio moves
between the two sockets as opaque base64 with no resampling, no PCM conversion
and no transcoding latency anywhere in the loop. The Realtime session is given a
persona prompt built from the scenario plus two tools: `flag_issue`, which
silently records a suspected defect with a timestamp, and `end_call`, which the
model calls after saying goodbye.

The inbound stream is the clock. Every inbound frame drives exactly one tick:
forward it to the model, emit at most one frame of the patient's audio back to
Twilio, and append one frame to each channel of a local stereo recorder. That
single rule gives real-time playback pacing, a self-correcting clock and a
recording whose two channels stay aligned for the whole call, with no second
timer competing for the event loop. When the model produces audio it lands in a
queue and is metered out one frame per tick; when the agent talks over us, the
queue is dropped, Twilio is told to flush, and the model's own conversation item
is truncated at the number of milliseconds that actually reached the phone. On
hangup the WAV is written, transcoded to MP3, and the transcript the agent's
side from Realtime input transcription, ours from the model's exact output
transcript is written alongside a JSON record with per-call metrics. A second
pass then re-reads every transcript with a reasoning model and merges its
findings with the in-call flags into a single ranked `BUG_REPORT.md`.

---

## Decisions and tradeoffs

### Speech-to-speech, not a cascaded pipeline

The alternative was STT → LLM → TTS with something like Deepgram + a chat model
+ ElevenLabs. I chose the Realtime API because the thing being graded first is
whether the bot *sounds like a caller*, and in a cascade the endpointing is your
problem: you own the decision about when the far end has finished a sentence,
and every hop adds latency that shows up as an unnatural gap before every reply.
A production voice agent pauses mid-turn while it looks things up, which is
exactly the case where naive VAD talks over it.

The costs of this choice are real. Speech-to-speech is more expensive per
minute, and you get less control over the exact words spoken you cannot force
a literal line the way you can when you own the TTS. That second point matters
for a *test harness*, where reproducibility is worth something. I accepted it
because the scenarios test behaviour under natural conversation rather than
exact string inputs, and because the model following an intent ("ask for Sunday
twice, then accept the alternative") produces more realistic pressure on the
agent than a fixed script would. Where determinism mattered I moved it out of
the speech path entirely: the tools are deterministic, and the scenario file
pins the goal, the probes and the success criteria.

### Raw Twilio Media Streams, not a managed voice-agent platform

Vapi, Retell and Bland would have had a bot on the phone in an afternoon. They
were the wrong tool here because the deliverable is not a voice agent it is
*evidence about someone else's voice agent*. Those platforms own the audio path,
which means the recording, the turn boundaries and the latency numbers are
whatever they choose to expose. Owning the socket is what makes it possible to
record cleanly separated channels, to measure the agent's response gap, and to
barge in deliberately at a chosen moment rather than hoping the platform's VAD
cooperates. It also keeps the whole thing portable and free of another vendor.

The price is about three hundred lines that a platform would have given me:
frame pacing, barge-in truncation, recording, reconnection edges. Those are
covered by the test suite precisely because they are the parts that fail
silently over a real phone line.

I also considered OpenAI's SIP path (`/v1/realtime/calls`), which can accept
calls directly and would remove one hop. It is a good fit for *receiving* calls;
for placing outbound PSTN calls, and for getting an independent second recording
out of the carrier, Twilio still earns its place.

### Turn-taking is configured per scenario

Default is `semantic_vad` at medium eagerness a model decides whether the far
end has finished a thought rather than counting milliseconds of silence, which
is markedly better at not stepping on an agent that pauses mid-sentence.
`interruption` raises eagerness to high because interrupting is the point of
that test. `vague-caller` switches to `server_vad`, because plain energy VAD is
the only mode that supports `idle_timeout_ms`; that scenario needs the patient
to sit in silence and see whether the agent re-prompts or just dies.

### Barge-in truncation

When the agent talks over us, three things have to happen together: drop our
queued audio, flush Twilio's buffer with a `clear` event, and send
`conversation.item.truncate` with the offset that actually played. Skipping the
third is the classic bug the model still believes it finished the sentence,
and its next turn references words nobody heard. Because playback is paced one
frame per tick, the played offset is just a frame counter, and it is exact
rather than estimated. There is a test that asserts the truncation offset equals
the audio actually emitted.

### Recording locally, with Twilio as a cross-check

Both directions of audio already pass through this process, so recording here is
free and gives perfectly separated channels: patient left, agent right. Driving
it from the same tick that paces playback is what keeps the channels aligned, so
transcript timestamps line up with the audio. Twilio's dual-channel recording is
still fetched when enabled, as an independent copy in case the stream ever drops
frames but the local file is the one to listen to.

`audioop` would have made the mu-law conversion a one-liner; it was removed from
the standard library in Python 3.13, so there is a 256-entry lookup table in
`audio.py` instead. Fewer surprises than an interpreter-version-dependent
dependency.

### Two-pass bug detection

In-call flags and post-call triage find different things, and one cannot replace
the other. The bot flagging in the moment gets an accurate timestamp and knows
what it was trying to do; a transcript reviewer sees the whole arc and catches
contradictions across ninety seconds. Both are treated as *candidates*. The
report says so in its own header, because a bug report padded with unverified
model output is worse than a short one and the transcripts feeding the
analyser are machine-generated and imperfect. Note that Realtime input
transcription runs as a separate asynchronous pass and is explicitly documented
as guidance rather than exactly what the model heard; the recording is the
ground truth, and the report points at both.

### Scenarios as data

A scenario is a YAML file: persona, goal, probes, success criteria, things to
watch for, turn-taking mode. This is the part that made iteration cheap. After
listening to the first calls, the fixes were mostly prompt and scenario edits
tightening turn length, stopping the bot from volunteering its date of birth
before being asked, adding explicit close-out rules so calls end naturally
instead of running to the time limit. Those changes are recorded in the comments
at the top of `prompts.py`, next to the behaviour each one was fixing.

### What is deliberately not here

Single process, in-memory registry, one call at a time, no retries, no queue, no
database. Concurrency would multiply cost and make it harder to hear what went
wrong. This is a test harness meant to be run by one person a few dozen times,
and it is built to that.

## Known limitations

- The agent-latency metric is measured from our last emitted frame to their
  first detected speech, so it includes VAD detection overhead. Treat it as a
  floor, useful for spotting multi-second dead air rather than as a benchmark.
- Input transcription is approximate; verify quotes against the audio.
- One call at a time. Twelve scenarios take roughly twenty minutes wall-clock
  with the default twenty-second spacing.
- If the callee never answers, the media stream never opens; the runner times
  out after sixty seconds, records the call as `no_answer` and moves on rather
  than retrying.
