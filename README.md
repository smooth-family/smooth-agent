# Smooth Agent

A self-hosted AI agent that runs on your own machine, with your own keys, on any
LLM you choose.

> Status: v0.4 — tool-calling agent, 30+ tools, ~200 skills, parallel
> sub-agents, long-term memory, saved and searchable sessions, scheduled jobs,
> background processes, browser automation, vision / image / speech and MCP
> servers. Chat channels and the installer are next — see [ROADMAP.md](ROADMAP.md).

## Why

- **Single-tenant, self-hosted.** One person, one box, your keys. No SaaS in the
  middle.
- **Any LLM.** OpenAI, Anthropic, DeepSeek, GLM (Zhipu), Groq, OpenRouter, Kimi,
  a local model, Smooth Router / 9router, or any OpenAI-compatible or
  Anthropic-native endpoint — with automatic fallback between them.
- **Real tools.** Files (read, write, patch, search), web (search, read pages,
  APIs), a real browser, vision, image generation, text-to-speech, shell and
  Python with background processes, parallel sub-agents, and any MCP server.
- **Remembers.** Long-term memory of you and your work (recalled automatically
  each message), every conversation saved and searchable, resumable sessions,
  and long chats summarised instead of cut off.
- **Works while you sleep.** Scheduled jobs (`daily 07:00`, `every 2h`, cron)
  that survive restarts and retry, plus cheap script watchers that only wake
  the model when something changed.
- **~200 skills.** Tested playbooks for coding agents, GitHub, documents, media,
  research, finance, productivity, smart home and more. The agent reads a skill
  when a task matches it, and writes or improves its own skills as it learns
  your work.
- **Safe by default.** File tools are confined to one workspace folder; shell and
  code run only when you switch them on, ask before each command, and never see
  your API keys; keys are redacted from everything printed.
- **Yours to modify.** MIT licensed.

## Install

```bash
git clone https://github.com/smooth-family/smooth-agent
cd smooth-agent
pip install -e .                      # core (Python 3.9+, no heavy dependencies)
pip install -e ".[browser]"           # + browser automation
python -m playwright install chromium # (once, for the browser)
```

## First run

```bash
smooth-agent setup      # creates ~/.smooth-agent, picks a provider, stores your key
smooth-agent doctor     # shows what works on this machine (--ping tests the model)
smooth-agent run        # talk to it
```

Everything lives in `~/.smooth-agent/` (override with `SMOOTH_AGENT_HOME`):
`config.toml` (providers and settings), `.env` (keys, mode 600), `skills/` (your
skills), `mcp.json` (MCP servers). See `smooth_agent/data/config.example.toml`
for every option.

## Use

```bash
smooth-agent run                    # interactive agent (/reset, /exit)
smooth-agent run --resume           # continue the last session
smooth-agent ask "find the 3 newest Python releases and save them to releases.md"
smooth-agent ask --terminal "run the tests in ./app and fix what fails"   # asks before each command
smooth-agent chat "hello"           # plain chat, no tools

smooth-agent skills list | search <words> | install <name|git-url|path> | remove <name>
smooth-agent mcp add github --env 'GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}' -- npx -y @modelcontextprotocol/server-github
smooth-agent mcp add docs --url https://example.com/mcp
smooth-agent mcp list | tools <name> | remove <name>
smooth-agent providers

smooth-agent memory list | add "…" --kind preference | search <words> | remove <id>
smooth-agent sessions list | show <id> | search <words>
smooth-agent cron add morning "daily 07:00" "Summarise the top tech news for me"
smooth-agent cron add hn "*/15 * * * *" --script "python3 watch_rss.py --name hn --url https://news.ycombinator.com/rss"
smooth-agent cron list | run <name> | pause <name> | remove <name>
smooth-agent daemon                 # runs the scheduled jobs (keep it running)
```

## Tools

| Tool | What it does | Default |
|---|---|---|
| `read_file`, `write_file`, `patch`, `list_dir`, `search_files` | files inside the workspace | on |
| `web_search`, `web_extract`, `http_get` | search (Tavily / Brave / SearXNG / DuckDuckGo), readable pages, raw APIs | on |
| `todo` | task list for multi-step work | on |
| `delegate_task` | up to 6 sub-agents in parallel | on |
| `skills_list`, `skill_view`, `skill_manage` | find, read and write skills | on |
| `memory` | long-term memory: add / replace / remove / search / pin | on |
| `session_search` | search past conversations | on |
| `cronjob` | schedule agent runs and script watchers | on (scripts need `terminal`) |
| `clarify` | ask you a question with choices | on |
| `vision_analyze` | understand images (URL, file, screenshot) | on |
| `terminal`, `execute_code`, `process` | shell / Python on your machine, background processes with exit notices | `[tools] terminal = true`, asks first |
| `browser_navigate`, `_click`, `_type`, `_press`, `_scroll`, `_back`, `_snapshot`, `_console`, `_get_images`, `_screenshot`, `_vision` | a real Chromium browser with your saved logins | `[browser] enabled = true` |
| `image_generate` | create images into the workspace | when `[image]` is set |
| `text_to_speech` | MP3 from text | when `[tts]` is set |
| `mcp_<server>_<tool>` | every tool of every MCP server you add | per server |

## License

MIT © Smooth. Includes MIT-licensed third-party material; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
