---
name: native-mcp
description: Connect MCP servers to Smooth Agent (local stdio or remote HTTP), check their tools, fix connection problems.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [mcp, tools, integration]
    category: smooth-agent
---

# Connecting MCP servers

Smooth Agent is an MCP client. Every tool of a connected server shows up as an
agent tool named `mcp_<server>_<tool>` at the start of the next session.

## Add a server

Local server started as a process (stdio):

```bash
smooth-agent mcp add github --env 'GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}' -- npx -y @modelcontextprotocol/server-github
smooth-agent mcp add qmd -- qmd mcp
```

Remote server (Streamable HTTP):

```bash
smooth-agent mcp add docs --url https://example.com/mcp --header 'Authorization=Bearer ${DOCS_TOKEN}'
```

- `${VAR}` in `--env` and `--header` values is read from the environment when the
  server starts. Put secrets in `~/.smooth-agent/.env` (loaded at start), never
  in the command line literally.
- `mcp add` connects once and lists the tools; `--no-check` skips that (for a
  server that is not running yet).
- Servers are stored in `~/.smooth-agent/mcp.json`. The same entries can be
  written in the main config as `[mcp.servers.<name>]` tables (they win over mcp.json).

## Check and manage

```bash
smooth-agent mcp list               # configured servers
smooth-agent mcp tools <name>       # connect and list its tools
smooth-agent mcp remove <name>
smooth-agent doctor                 # overall status, MCP included
```

Optional per-server keys in mcp.json: `"timeout"` (seconds, default 60),
`"include"` / `"exclude"` (lists of tool names), `"enabled": false`, `"cwd"`.
`[mcp] confirm = true` in the config makes the agent ask before every MCP call.

## When a server does not connect

1. Run the command by hand (`npx -y …`) and read its error: missing runtime
   (`node`, `uv`), wrong package name, missing token.
2. `smooth-agent mcp tools <name>` shows the server's own error message.
3. A stdio server must speak JSON-RPC on stdout only; servers that print logs to
   stdout break the protocol (look for a `--stdio` or quiet flag).
4. A remote server answering 401/403 needs a header (`--header 'Authorization=Bearer ${TOKEN}'`).
5. Tools appear only in a new session: `smooth-agent run` again after adding.
