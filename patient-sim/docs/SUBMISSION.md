# Submission checklist and video outlines

Working notes for finishing the submission. Not part of the graded deliverables,
but the two videos are graded heavily, so it is worth planning them.

## Before recording anything

- [ ] `make check` is all green
- [ ] At least 10 calls in `artifacts/`, each a full 1–3 minute conversation.
      Discard and rerun any call that is a single question and a hangup, or where
      the bot talks over the agent constantly — those fail the first-pass bar.
- [ ] **Listen to every recording end to end.** This is the first thing the
      reviewer does. If a call sounds robotic, fix the prompt and rerun it
      rather than shipping it.
- [ ] `BUG_REPORT.md` regenerated, then manually verified entry by entry against
      the audio. Delete anything you cannot hear.
- [ ] `.env` is not committed; `.env.example` is
- [ ] Repository is public
- [ ] Keep your Twilio and OpenAI usage receipts (reimbursed up to $20)

## Video 1 — project walkthrough (3 min max, webcam on)

The brief asks to see *how you think*, not a feature tour. Suggested shape:

1. **(0:00–0:20) What it is.** One sentence, then play ten seconds of real call
   audio. Lead with the artifact, not the architecture.
2. **(0:20–1:10) The one decision that mattered.** Speech-to-speech over a
   cascaded pipeline, and raw Twilio Media Streams over a managed platform —
   what each bought and what each cost. Name the alternatives you rejected and
   why; that is what is actually being assessed.
3. **(1:10–2:00) The hard part.** Barge-in: dropping the queue, flushing Twilio,
   and truncating the model's item at the offset that really played — and what
   the conversation sounds like when you skip that last step.
4. **(2:00–2:40) The best bug you found**, with the audio.
5. **(2:40–3:00) What you would do next** with more time.

Do not walk through the file tree. Do not read the README aloud.

## Video 2 — debugging with AI (webcam on)

Show one real problem being solved, start to finish. Pick a failure you actually
hit — a good candidate is the first call where turn-taking went wrong, or the
Realtime session rejecting a beta-shaped `session.update`. What to make visible:

- the symptom first (play the bad audio, or show the error)
- your actual prompt, including the context you chose to give the model
- the model's suggestion, and **where you disagreed with it or checked it**
- the fix, and the verification that it worked

The interesting part is your judgement about which suggestions to trust, not the
speed of the edit. Narrate the reasoning as you go.

## Form fields

- GitHub repo URL (public)
- Two Loom URLs (public)
- The single Twilio number used for every call, E.164 — this is
  `TWILIO_FROM_NUMBER` from your `.env`. Double-check it; the reviewer cannot
  grade the calls without it.
