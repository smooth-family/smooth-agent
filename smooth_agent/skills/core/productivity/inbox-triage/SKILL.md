---
name: inbox-triage
description: Triage the owner's email — group what matters, draft replies for approval, and keep the noise out.
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [email, triage, inbox, replies, productivity]
    category: productivity
---

# Inbox triage

Turn a full inbox into a short, sorted picture and get replies moving — without
sending anything the owner hasn't seen.

Needs `[email]` configured (the `email_search` / `email_read` / `email_send`
tools). Reading never marks mail as read; sending always asks the owner first.

## 1. Pull the recent mail

- `email_search` for the window the owner cares about — default unread in the
  last 1–3 days (`unseen since 2d`). Widen only if they ask.
- `email_read` the ones whose subject/sender suggest they need a person. Don't
  read everything; headers are usually enough to sort.

## 2. Sort into four buckets

- **Reply now** — a real person waiting on the owner.
- **Reply later / FYI** — informational, no action today.
- **Do** — asks for a small task (pay, confirm, send a file).
- **Noise** — newsletters, notifications, automated mail.

Show it compactly, newest first:

```
Reply now (2)
  • Bob — "Invoice overdue?" — wants the Oct invoice
  • Lia — "Reschedule Thu?" — asks to move the 1:1
Do (1)
  • Netlify — build failed on main
FYI (4) · Noise (11)
```

## 3. Draft, don't send

For each **Reply now**, draft a short reply in the owner's voice and show it.
Only on an explicit yes, `email_send` with `reply_to` set so it threads:

- Match their language and tone (often casual Indonesian).
- Keep it to the point; propose a concrete next step or time.
- Never invent facts, amounts, or commitments — if unsure, ask the owner one
  question instead of guessing.

## 4. Offer to automate the sorting

If the owner wants this every morning, save it as a workflow and schedule it so
only the summary (not drafts) arrives in chat:

```
workflow create
  name: inbox-triage
  steps: |
    1. Search unread email from the last {{window}}.
    2. Sort into Reply now / Do / FYI / Noise; one line each for the first two.
    3. Reply with the summary only. Do not send any email. [SILENT] if the inbox
       is quiet.
  params: [{"name": "window", "default": "1d"}]

cronjob create  name: inbox-triage  schedule: weekdays 08:00  workflow: inbox-triage  deliver: telegram
```

## Guardrails

- Sending email is the only step that leaves the machine — it always asks.
- Don't delete or move mail unless the owner explicitly asks (and only then).
- Keep summaries free of full quoted threads; link or name the sender instead.
