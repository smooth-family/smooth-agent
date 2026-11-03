---
name: smooth-agent
description: How Smooth Agent itself works and is configured — providers, keys, tools, skills, workspace, MCP, browser, troubleshooting.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [smooth-agent, config, setup, help]
    category: smooth-agent
---

# Smooth Agent: setup and configuration

Use this when the owner asks how to set something up, change a provider or model,
turn a tool on, or why something does not work.

## Files

- `~/.smooth-agent/config.toml` — providers, `[agent]`, `[tools]`, `[browser]`,
  `[vision]`, `[image]`, `[tts]`, `[mcp]` (a `smooth-agent.toml` in the current
  folder wins over it).
- `~/.smooth-agent/.env` — keys (`DEEPSEEK_API_KEY=…`, `OPENAI_API_KEY=…`,
  `ANTHROPIC_API_KEY=…`, `GLM_API_KEY=…`, `GITHUB_TOKEN=…`, …). Loaded at start.
- `~/.smooth-agent/skills/` — the owner's skills; `mcp.json` — MCP servers;
  `processes/` — logs of background commands.
- The workspace (`[agent] workspace`, default `./workspace`) is the only folder
  the file tools can touch.

## Providers

```toml
default = "main"
fallbacks = ["backup"]          # tried in order when the default fails

[providers.main]
provider = "deepseek"           # openai, anthropic, deepseek, glm, groq, openrouter, kimi,
model = "deepseek-chat"         # smooth-router, 9router, openai-compatible (+ base_url)

[providers.backup]
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"   # e.g. a local model server
model = "qwen3:8b"
```

`smooth-agent providers` lists them; `smooth-agent doctor --ping` tests the default.

## Turning tools on

```toml
[tools]
terminal = true      # shell + Python + background processes (asks first unless confirm = false)
search = "auto"      # set TAVILY_API_KEY / BRAVE_API_KEY / SEARXNG_URL for better search
optional_skills = false
enable_skills = ["sherlock"]

[browser]
enabled = true       # pip install "smooth-agent[browser]" && python -m playwright install chromium

[image]
provider = "main"
model = "gpt-image-1"
```

## Connected services (built in)

- `http_request` — any API; secrets as `{{env:NAME}}` (filled in, never shown). Non-GET asks first.
- `email_search` / `email_read` / `email_send` — `[email] user` + `EMAIL_PASSWORD` (an app
  password). Gmail, Outlook, Yahoo, iCloud and Zoho need nothing else. Sending asks first.
- `git` (no terminal needed) and `github` (issues, PRs, search) — `GITHUB_TOKEN` in `.env`.
  Commits, pushes and anything written on GitHub ask first.
- `sql_query` — read-only SQLite.
- `web_watch` — check a page / JSON API every N minutes; you hear only when it changes.

## Skills

`smooth-agent skills list|search <words>|install <name|git-url|path>|remove <name>`.
The agent can also write its own with the `skill_manage` tool.

## Memory and sessions

- `smooth-agent memory list|add|search|remove|pin` — the same notes the `memory`
  tool writes; pinned and relevant ones are recalled into every message.
- `smooth-agent sessions list|show|search`, `smooth-agent run --resume`.
- `[agent] memory = false` turns memory and saved sessions off.

## Scheduled jobs

- `smooth-agent cron add <name> "<schedule>" "<prompt>"` — schedules: `daily 07:00`,
  `weekdays 08:30`, `weekly mon 09:00`, `every 2h`, `*/15 * * * *`, `at 2026-11-03 09:00`.
- `--script "<command>"` makes a watcher: the command runs first; no output means
  nothing to report (no model call); with a prompt, the agent works on the output.
- A run that replies exactly `[SILENT]` sends nothing.
- Jobs run only while `smooth-agent daemon` is running; `smooth-agent jobs` shows
  results and failures (each failed run is retried with backoff).
- Set `[agent] timezone` so schedules use the owner's local time.

## Troubleshooting

- "no key" → add the variable to `~/.smooth-agent/.env`.
- Tool missing → it is off in `[tools]` / `[browser]` / not configured (`[image]`, `[tts]`).
- Model errors 429/5xx → add a provider to `fallbacks`.
- MCP problems → the `native-mcp` skill.
