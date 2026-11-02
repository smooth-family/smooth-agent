---
name: coding-agent
description: "Delegate coding work to Codex, Claude Code, or OpenCode as background workers; not simple edits or read-only code lookup."
metadata:
  {
    "smooth":
      {
        "emoji": "🧩",
        "requires":
          {
            "anyBins": ["claude", "codex", "opencode"],
            "config": ["skills.entries.coding-agent.enabled"],
          },
        "install":
          [
            {
              "id": "node-claude",
              "kind": "node",
              "package": "@anthropic-ai/claude-code",
              "bins": ["claude"],
              "label": "Install Claude Code CLI (npm)",
            },
            {
              "id": "node-codex",
              "kind": "node",
              "package": "@openai/codex",
              "bins": ["codex"],
              "label": "Install Codex CLI (npm)",
            },
          ],
      },
  }
---

# Coding Agent

Use for background feature builds, PR reviews, large refactors, and issue-to-PR loops. Do not use for simple edits, read-only lookup, ACP thread-bound work, or any run inside `~/.smooth-agent`, `$SMOOTH_AGENT_HOME`, or active Smooth Agent state dirs.

## Hard rules

- Always launch with `terminal(background=true, notify_on_complete=true)`: Smooth Agent
  tells you when the worker exits and shows its last output.
- Run the agents non-interactively: Codex `codex exec --full-auto "<task>"`, OpenCode
  `opencode run "<task>"`, Claude Code `claude --permission-mode bypassPermissions --print "<task>"`.
- Monitor with `process` (poll / log); do not kill slow workers without cause.
- If user asked for a specific agent, use that agent.
- If worker fails/hangs, respawn or ask; do not silently hand-code instead.
- Never checkout branches or run background coding agents in `~/Projects/smooth-agent`; use an isolated checkout.

## Finishing

End every worker prompt with: "When done, print a short summary of what changed,
the test results, and the branch or PR URL." When the exit notice arrives, read the
full log with `process(action="log")`, check the result yourself, and report it. If
the owner asked to be told on a chat channel, use `send_message`.

## Launch forms

Write the worker prompt to a temp file first. This avoids shell quoting bugs when the required notification block contains quotes or newlines.

```bash
PROMPT=$(mktemp -t smooth-agent-worker-prompt.XXXXXX)
cat >"$PROMPT" <<'EOF'
Task.
<notification block>
EOF
printf 'prompt file: %s\n' "$PROMPT"
```

Use `$PROMPT` when launching from the same shell/session. If using a separate tool call, substitute the printed path.

Codex:

```bash
bash pty:true background:true workdir:/path/repo command:"codex exec - < \"$PROMPT\""
```

Claude Code:

```bash
bash background:true workdir:/path/repo command:"claude --permission-mode bypassPermissions --print < \"$PROMPT\""
```

OpenCode:

```bash
bash pty:true background:true workdir:/path/repo command:"opencode run < \"$PROMPT\""
```

## Long issue-to-PR work

1. Create/reuse a GitHub issue as durable spec.
2. Include issue URL, repo, base branch, expected PR, proof, and notification route.
3. Tell worker to branch, implement, test, run review until no accepted actionable findings, open PR.
4. Return issue URL and `sessionId` immediately.
5. Monitor with `process`; cancel through Task Registry if mirrored there.

## Scratch Codex

Codex needs a trusted git repo:

```bash
SCRATCH=$(mktemp -d)
git -C "$SCRATCH" init
PROMPT=$(mktemp -t smooth-agent-worker-prompt.XXXXXX)
cat >"$PROMPT" <<'EOF'
Build X.
<notification block>
EOF
printf 'prompt file: %s\n' "$PROMPT"
bash pty:true background:true workdir:$SCRATCH command:"codex exec - < \"$PROMPT\""
```

## Process actions

- `list`: running/recent sessions.
- `poll`: status.
- `log`: output.
- `submit`: send input + Enter.
- `write`: raw stdin.
- `paste`: paste text.
- `kill`: terminate.

## Status to user

- Say what started, where, and `sessionId`.
- Update only on milestone, worker question, error, user action needed, or finish.
- If killed, say why.
