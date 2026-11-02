# Changelog

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
