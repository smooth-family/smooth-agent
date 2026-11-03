---
name: meeting-notes
description: Turn a voice note, transcript or raw notes into clean meeting notes with decisions and action items, saved where the owner keeps them.
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [notes, meetings, summary, notion, documents, productivity]
    category: productivity
---

# Meeting notes

Take whatever the owner captured — a voice note, a transcript, bullet scraps —
and return structured notes they can reuse.

## Input

- A **voice note** arrives transcribed already; work from the transcript.
- A **file** (transcript, PDF, docx) is read on arrival — summarise from it.
- Pasted or typed rough notes — clean them up as given.

If the recording is long, don't lose the specifics: pull exact names, numbers,
dates and owners of tasks rather than smoothing them away.

## Output format

```
# {Meeting} — {date}
Attendees: …

## Summary
2–4 sentences: what it was about and the outcome.

## Decisions
- …

## Action items
- [ ] {task} — {owner} — {due}

## Open questions
- …
```

Keep decisions and action items concrete and attributable. Flag anything
ambiguous as an open question instead of guessing.

## Where to put it

Offer the owner the destination that fits their setup:

- **Notion** — if `notion` is configured, `append` the notes to the project
  page they name (asks first). Use `notion` `action:"search"` to find the page
  id when they give a title.
- **A document** — `create_document` a `.docx` and `send_file` it.
- **Drive** — if they keep notes in `gdrive`, tell them the file and offer to
  draft a follow-up email with the action items (`email_send`, asks first).
- **Just chat** — paste the notes directly for a quick one-off.

## Follow-through

- Offer to turn action items with dates into `calendar` events or a `cronjob`
  reminder (each asks before it books/schedules).
- Save a one-line `memory` note of any decision that affects ongoing work, so it
  resurfaces later.
