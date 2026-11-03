# Changelog

## 0.8.0 — connected services

- `http_request`: any API with method, headers, query and JSON; secrets as `{{env:NAME}}`,
  filled in at the last moment and never shown to the model; writes ask first.
- Email: `email_search`, `email_read` (attachments saved to the workspace, HTML mail as
  text, not marked read), `email_send` (attachments, reply in the thread). IMAP/SMTP
  presets for Gmail, Outlook, Yahoo, iCloud and Zoho. Sending asks first.
- `git` without the terminal (status/log/diff/show/branch free; add/commit/pull/push/
  checkout/clone ask first; no option injection; GitHub token passed in the environment,
  never written to `.git/config` or the command line).
- `github`: repo, issues, PRs with files and diff, search, notifications; create issue /
  PR, comment, close / reopen (ask first).
- `sql_query`: read-only SQLite.
- `web_watch`: watch a page or JSON API (regex `select`, `json_path`); the scheduler
  compares snapshots and delivers a diff only when it changes — no model call otherwise.
- `doctor` shows email and GitHub status.

## 0.7.0 — every channel works like Telegram

- **Files and voice in** on WhatsApp, Slack, Matrix, LINE and Discord: photos, documents,
  archives, voice notes (transcribed and answered) — same inbox and readers as Telegram.
- **Files and voice out** (`send_file`, voice replies) on WhatsApp, Slack, Matrix and
  Discord; LINE sends images/audio/download links through `public_url` (links expire in 1 h).
- **Approval buttons** everywhere: WhatsApp reply buttons, Slack Block Kit (Interactivity
  webhook), LINE quick replies, Discord buttons, Matrix ✅ / ❌ reactions. Only an owner's
  tap counts (Telegram included); a typed yes / no works on every channel.
- **Discord is two-way:** Gateway websocket client in the standard library (heartbeat,
  resume, reconnect, falls back to DMs when the Message Content intent is off), DMs and
  @mentions in servers, rate limits waited out. Webhook-only configs keep working.
- WhatsApp webhooks can be signature-checked (`app_secret`); downloads never send a bot
  token to another host after a redirect and refuse login pages and oversized files.

## 0.6.0 — chat as the main way to use it

- **Commands in chat:** `/new`, `/status`, `/model`, `/stop`, `/help` (Telegram menu).
- **Live progress:** one message shows each step shell-style (`$ git pull`, `read app.py`,
  `search …`) with ✓ / ⏳ / ✗, and disappears when the answer arrives.
- **Files in:** photos, PDF (scans become page images for the vision model), Word, Excel,
  PowerPoint, OpenDocument, zip, rar, 7z, tar — read automatically; archives unpacked safely.
- **Files out:** `create_document` makes real `.docx` / `.xlsx`; `send_file` sends any file.
- **Voice:** voice notes are transcribed and answered; optional voice replies.
- **Persona files:** `SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`, `MEMORY.md`
  in the workspace become part of the instructions (empty templates by default).
- **File access:** `[tools] files = "full"` reaches the whole machine, with approvals for
  writes outside the workspace and for secret files.
- Long answers are continued instead of cut off; the agent knows its own paths.
- Fixes: `confirm = false` ignored by the daemon; Telegram errors that were silent; chat
  sessions labelled `cli`; Telegram connections retried.

## 0.5.0 — channels and installer

- Seven chat channels: Telegram, WhatsApp, Slack, Matrix, LINE (two-way), WeChat Work and
  Discord (delivery). Owner-only, one saved conversation per chat, approvals by button or reply.
- `smooth-agent daemon` runs channels and scheduled jobs; `smooth-agent service` installs it
  (systemd / launchd).
- One-line installer, setup wizard, Docker image, CI.

## 0.4.0 — memory and schedules

- Long-term memory, saved and searchable sessions, context compression, scheduled jobs with a
  durable queue.

## 0.3.0 — tools and skills

- Multi-router core (OpenAI, Anthropic, DeepSeek, GLM, Groq, OpenRouter, Kimi, gateways, any
  OpenAI-compatible endpoint), 30+ tools, ~200 skills, browser, MCP, vision, image and speech.
