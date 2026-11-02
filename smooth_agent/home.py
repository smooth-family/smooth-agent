"""Smooth Agent's home directory.

Everything the agent keeps between runs lives under one folder, by default
`~/.smooth-agent` (override with $SMOOTH_AGENT_HOME):

    config.toml     providers, tools, MCP servers, channels
    .env            secrets (API keys, tokens) — loaded at start, never overwritten
    skills/         the owner's own skills (override bundled ones)
    mcp.json        MCP servers added with `smooth-agent mcp add`
    processes/      logs of background commands
    memory/         long-term memory and the job queue (later stages)

Skill scripts find the same folder through the SMOOTH_AGENT_HOME env var, which
every command the agent runs receives.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def agent_home() -> Path:
    return Path(os.environ.get("SMOOTH_AGENT_HOME") or Path.home() / ".smooth-agent").expanduser()


def ensure_home() -> Path:
    h = agent_home()
    h.mkdir(parents=True, exist_ok=True)
    try:
        h.chmod(0o700)  # keys live here
    except OSError:
        pass
    os.environ.setdefault("SMOOTH_AGENT_HOME", str(h))
    return h


_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; quotes stripped; '#' comments and blank lines ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0]
        out[key] = val
    return out


def load_dotenv(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Load $SMOOTH_AGENT_HOME/.env into os.environ. Variables already set win
    unless override=True. Returns the names that were set."""
    p = path or (agent_home() / ".env")
    if not p.is_file():
        return []
    loaded = []
    for k, v in parse_env(p.read_text(encoding="utf-8", errors="replace")).items():
        if override or k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def set_env_value(key: str, value: str, path: Path | None = None) -> Path:
    """Write or replace KEY=value in the home .env (mode 600)."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"bad variable name: {key}")
    p = path or (ensure_home() / ".env")
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    quoted = value if re.match(r"^[A-Za-z0-9_./:@+-]*$", value) else '"' + value.replace('"', '\\"') + '"'
    new, done = [], False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == key:
            if not done:
                new.append(f"{key}={quoted}")
                done = True
            continue
        new.append(line)
    if not done:
        new.append(f"{key}={quoted}")
    p.write_text("\n".join(new) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


__all__ = ["agent_home", "ensure_home", "load_dotenv", "parse_env", "set_env_value"]
