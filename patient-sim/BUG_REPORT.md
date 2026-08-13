# Bug report

> **This file is a placeholder.** It is overwritten by `python -m patientsim report`
> once real calls exist in `artifacts/`. Nothing below is a finding — it exists
> to show the shape of the output and the standard entries are held to. Replace
> this entire file with the generated one before submitting.

## How this gets produced

1. `python -m patientsim run --all` places the calls and saves a transcript,
   an MP3 and a JSON record per call.
2. Issues flagged live by the patient bot (`flag_issue`) and issues found by
   post-call transcript review are merged, deduplicated by call, and ranked by
   severity.
3. **Then you listen.** Open the MP3 at each timestamp and confirm the agent
   actually said what the transcript claims. Delete anything that does not hold
   up, and tighten the wording of what remains. Machine transcription is
   imperfect and an unverified report is worse than a short one.

## Severity

| Severity | Meaning |
| --- | --- |
| critical | Patient safety, or a false confirmation the practice would have to honour (a booking that does not exist, a refill said to be approved). |
| high | Invented facts, disclosure before identity verification, a caller's actual goal silently dropped. |
| medium | Self-contradiction, repeated misunderstanding, multi-second dead air, poor recovery. |
| low | Awkward but harmless: clumsy repair, over-long turns, unnecessary repetition. |

Not reported: filler words, phrasing preferences, punctuation, transcription
artefacts, or anything that cannot be pointed at in the audio.

## Entry format

Each entry names what happened, why it is a problem, and exactly where to hear
it:

```
### 1. Confirms a Sunday appointment without checking office hours

- **Severity:** high
- **Category:** hallucination
- **Where:** `call-06-weekend-request.txt` at 1:23 (audio: `call-06-weekend-request.mp3`)
- **Found by:** flagged live by the caller
- **Agent said:** "..."
- **Why it matters:** ...
- **Expected:** ...
```

## Findings

_None yet — run the calls._
