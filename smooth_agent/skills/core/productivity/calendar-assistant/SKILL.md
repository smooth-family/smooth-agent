---
name: calendar-assistant
description: Read, search and book the owner's calendar from plain language — agenda, free slots, and events created on approval.
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [calendar, scheduling, agenda, caldav, google-calendar]
    category: productivity
---

# Calendar assistant

Work the owner's calendar through the `calendar` tool (Google Calendar or
CalDAV, whichever is set in `[calendar]`). Reading is free; creating an event
asks the owner first.

## Agenda and search

- "What's on today / this week?" → `calendar` `action:"agenda"` with `days` (1 =
  today, 7 = the week). Render `YYYY-MM-DD HH:MM — title (@ location)`, soonest
  first, and call out the next thing explicitly.
- "When's my call with X?" → `action:"search"`, `query:"X"`, a wider `days`.

## Finding a free slot

The tool lists events, not gaps — compute the gaps yourself:

1. Pull the agenda for the day(s) in question.
2. Within the owner's working hours (ask once if unknown, then remember it with
   `memory`), find windows with no event of the requested length.
3. Offer two or three concrete options rather than one.

## Booking

Turn plain language into a concrete event and confirm before creating:

- Resolve relative dates against today (the system prompt states it) and the
  owner's timezone. "besok jam 9" → the right RFC3339 `start`/`end`.
- Default the duration to 30–60 min if unspecified; ask only if it matters.
- Then `calendar` `action:"create"` with `summary`, `start`, `end`, optional
  `location`/`description`. This asks the owner — if they decline, don't retry;
  offer an edit instead.

Example:

```
calendar  action:create  summary:"1:1 with Lia"  start:"2026-11-06T15:00:00Z"  end:"2026-11-06T15:30:00Z"
```

## Pair it with other tools

- After booking from an email request, offer to `email_send` a confirmation
  (asks first).
- For a recurring personal routine ("remind me to review goals every Friday"),
  a `cronjob` that delivers to chat is simpler than a calendar event.
- Combine with `daily-briefing` so the agenda shows up every morning.

## Guardrails

- Never create, move, or cancel an event without the owner's explicit yes.
- Double-check the date math out loud for anything more than a day away.
