# Smooth Agent — Roadmap

We grow Smooth Agent in stages; each stage lands working, tested and installable.

## Principle

Capability and stability are the goal, not line count. Keep the surface small,
the config obvious, and every stage runnable by someone who just `git clone`d it.

## Stage 0 — Skeleton + multi-router  ✅

- Packaging, license, CLI. `smooth_agent.routers`: one chat API over
  OpenAI-compatible and Anthropic-native endpoints, presets for OpenAI,
  Anthropic, DeepSeek, GLM, Groq, OpenRouter, Kimi, Smooth Router, 9router and
  custom gateways, ordered fallback, keys from the environment.

## Stage 1 — Agent loop  ✅

- Tool-calling loop over the router; `smooth-agent run` / `ask`.

## Stage 2 — Tools + skills  ✅ (v0.2)

- Files, web, todo, delegate_task, terminal / execute_code (opt-in, approval,
  key-free environment), Anthropic-native tool calling, secret redaction.
- Bundled skill library with precedence (workspace > user > extra > core >
  optional), OS filter, missing-binary hints, `skill_manage`.

## Stage 2b — More tools  ✅ (v0.3)

- `~/.smooth-agent` home: config, `.env`, skills, MCP servers, process logs.
- Background processes (`terminal background=true`, `process`, exit notices
  delivered to the agent automatically).
- Browser automation (Playwright/Chromium, persistent profile, element refs,
  console, screenshots, browser_vision).
- Vision, image generation, text-to-speech through the configured providers.
- MCP client (stdio + Streamable HTTP with SSE), `smooth-agent mcp add/list/tools/remove`.
- `smooth-agent setup`, `doctor`, `skills list/search/install/remove`.
- Smooth Agent's own skills: `smooth-agent` (configuration guide), `native-mcp`.

## Stage 3 — Memory + scheduling  ✅ (v0.4)

- One SQLite database (`~/.smooth-agent/smooth.db`, WAL, FTS5).
- Long-term memory: typed notes (user / preference / decision / finding /
  blocker / note), pinned + relevant ones recalled into every turn; `memory`
  tool and `smooth-agent memory`.
- Every session saved; `session_search` tool, `smooth-agent sessions`,
  `run --resume`.
- Context compression: older messages summarised, tool calls never split from
  their results.
- Durable job queue with leases and retries; schedules (cron, `every 2h`,
  `daily 09:00`, `at …`) in the owner's timezone; `cronjob` tool,
  `smooth-agent cron`, `jobs`, `daemon`; script watchers (no model call when
  nothing changed) and the `[SILENT]` reply.
- `clarify` tool.

## Stage 4 — Channels + installer (v0.5.0)

- Seven chat channels for one owner, picked during setup: Telegram (two-way,
  approval buttons), WhatsApp, Slack, Matrix, LINE (two-way), WeChat Work and
  Discord (delivery). `send_message` tool, owner-only access, approvals by button
  or a typed yes/no, cron results delivered to a channel (`--deliver telegram`).
- One saved session per chat (persists across restarts); a single worker
  serializes turns so the model is never hit by parallel requests.
- `smooth-agent daemon` runs channels + cron together; `smooth-agent service`
  installs a systemd user unit (Linux) or launchd agent (macOS).
- Installer: one-line `install.sh` with the Smooth Agent banner, colours and
  numbered steps (pipx or a private venv, PATH check, setup wizard); the setup
  wizard itself is a 4-step flow with the same look.
- Docker image + compose (runs as an unprivileged user, fixes bind-mount
  ownership, healthcheck), `pipx install`.
- README with banner, badges and screenshots rendered from real output
  (`scripts/term_svg.py`); CI on Python 3.9 – 3.13, installer and Docker build.

Still to do:

- Two-way Discord (bot gateway), Signal, MS Teams, Google Chat, Feishu/Lark,
  Mattermost, email, SMS — on the same `Channel` base.
- Approval buttons on Slack / LINE / Matrix (typed yes/no works today).

## Stage 5 — Chat as the main way to use it (v0.6.0)

- Commands (`/new`, `/status`, `/model`, `/stop`, `/help`) and live shell-style progress.
- Files in (photos, PDF, Office, OpenDocument, archives incl. RAR) and out (`create_document`,
  `send_file`); voice notes in, optional voice replies.
- Persona files, whole-machine file access with approvals, continued long answers.

Still to do: receiving files and voice on WhatsApp, Slack, Matrix and LINE; approval buttons on
Slack, LINE and Matrix.

## Later

- Desktop control (computer use).
- Voice conversations.
- Optional multi-tenant mode from the same codebase.
