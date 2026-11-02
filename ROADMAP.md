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

## Stage 3 — Memory + scheduling (next)

- Long-term memory: typed notes (fact / preference / decision / finding /
  blocker) with full-text recall, injected into each turn; `memory` tool.
- Session history saved and searchable (`session_search`); resume a session.
- Context compression for long conversations.
- Durable job queue + cron (`cronjob` tool, `smooth-agent cron add/list/remove`,
  `smooth-agent daemon`): scheduled agent runs that survive restarts and retry.
- `clarify` tool (ask the owner a question with options).

## Stage 4 — Channels + packaging

- Chat channels for one owner: Telegram first, then WhatsApp / Discord / Slack
  as plugins (`send_message` tool, owner-only access, approvals by button).
- `smooth-agent daemon` runs channels + cron as a service (systemd unit, launchd plist).
- Docker image, `pipx install`, one-command install script.

## Later

- Desktop control (computer use).
- Voice conversations.
- Optional multi-tenant mode from the same codebase.
