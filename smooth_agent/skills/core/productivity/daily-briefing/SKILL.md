---
name: daily-briefing
description: Build the owner a morning briefing from their calendar, inbox and watched pages, and schedule it to arrive every day in chat.
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [briefing, routine, calendar, email, schedule, workflow]
    category: productivity
---

# Daily briefing

Pull the owner's day together into one short message: what's on the calendar,
what needs a reply in the inbox, and anything they asked you to watch. Then save
it as a workflow and schedule it so it arrives on its own each morning.

Use only the tools that are actually configured — skip a section rather than
failing the whole briefing if one service is off.

## Gather (in parallel where you can)

- **Agenda** — `calendar` with `action:"agenda"`, `days:1` (today) or `days:2` to
  include tomorrow. List each event as `time — title`.
- **Inbox** — `email_search` for recent unread (e.g. `unseen since 1d`), then
  `email_read` only the few that look like they need the owner. Summarise who
  wrote and what they want in one line each. Never mark mail read.
- **Watched pages / news** — if the owner keeps `web_watch` jobs or a news
  source, include only what changed since yesterday.
- **Open loops** — `memory` recall of `blocker` and `decision` notes still open,
  and any `todo` items left from yesterday.

## Write it

Keep it skimmable and short — this is read on a phone over coffee:

```
☀️ Good morning — {weekday}
🗓  3 events · first: 09:30 Standup
📬 2 emails need you: Bob (invoice), Lia (reschedule?)
🔔 1 watched page changed: pricing update
✅ Carry over: finish the deploy note
```

Lead with the single most time-sensitive thing. Offer to act (“reply to Bob?”,
“block 2–3pm for the deploy?”) rather than just listing.

## Make it recur

Once the owner likes the format, save it so it runs itself:

```
workflow create
  name: morning-briefing
  description: Daily briefing from calendar + inbox + watches
  steps: |
    1. Get today's agenda (calendar, days 1).
    2. Find unread email from the last day that needs the owner; one line each.
    3. Note any watched pages that changed and open todos/blockers.
    4. Write the briefing in the short format and reply with it. If nothing is
       worth sending, reply exactly [SILENT].
```

Then schedule it to land in chat:

```
cronjob create
  name: morning-briefing
  schedule: weekdays 07:00
  workflow: morning-briefing
  deliver: telegram
```

The scheduler must be running (`smooth-agent daemon`, or the installed service).
A run that finds nothing new should reply `[SILENT]` so no empty message is sent.

## Tips

- Respect the owner's timezone (schedules use it already).
- Keep the briefing to what they can act on before the day starts; move detail
  into a follow-up only if they ask.
- If the owner wants it as a file (e.g. a weekly rollup), `create_document` a
  short `.docx`/`.xlsx` and `send_file` it instead of a long chat message.
