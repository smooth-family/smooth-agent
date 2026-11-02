"""Loading Smooth Agent config from TOML + environment.

The config file names providers and models; secrets stay in the environment.
A minimal config looks like:

    default = "main"
    fallbacks = ["backup"]

    [providers.main]
    provider = "deepseek"      # a preset in routers.PRESETS
    model = "deepseek-chat"
    # key read from $DEEPSEEK_API_KEY

    [providers.backup]
    provider = "smooth-router"
    model = "anthropic/claude-haiku-5-5"
    base_url = "http://127.0.0.1:20150/v1"
    key_env = "SMOOTH_ROUTER_KEY"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10 fallback
    import tomli as tomllib  # type: ignore

from .home import agent_home
from .routers import Router


def default_paths() -> list[Path]:
    """Where the config is looked for, first match wins."""
    env = os.environ.get("SMOOTH_AGENT_CONFIG", "")
    return [p for p in (
        Path(env).expanduser() if env else None,
        Path.cwd() / "smooth-agent.toml",
        agent_home() / "config.toml",
        Path.home() / ".config" / "smooth-agent" / "config.toml",
    ) if p]


DEFAULT_PATHS = default_paths()


def find_config() -> Path | None:
    for p in default_paths():
        if p.is_file():
            return p
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    p = Path(path) if path else find_config()
    if not p or not p.is_file():
        raise FileNotFoundError(
            "no smooth-agent config found; run `smooth-agent setup` (or create smooth-agent.toml / set SMOOTH_AGENT_CONFIG)"
        )
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def build_router(path: str | os.PathLike[str] | None = None, *, env: dict[str, str] | None = None) -> Router:
    return Router.from_config(load_config(path), env=env)


@dataclass
class Settings:
    """[agent] and [tools] from smooth-agent.toml, with safe defaults."""

    workspace: str = ""            # default: ./workspace
    max_steps: int = 12
    max_tokens: int = 1536
    instructions: str = ""         # extra system instructions from the owner
    timezone: str = ""             # e.g. "Asia/Jakarta"; default: the machine's
    context_tokens: int = 64000    # compress older messages beyond ~80% of this
    keep_recent: int = 10          # messages kept word-for-word when compressing
    memory: bool = True            # long-term memory + saved sessions
    http: bool = True              # http_get, web_extract, web_search
    search: str = "auto"           # auto | duckduckgo | brave | tavily | searxng
    terminal: bool = False         # run shell commands on this machine
    code: bool | None = None       # execute_code (defaults to the terminal setting)
    confirm: bool = True           # ask before each terminal / execute_code call
    timeout: int = 180             # seconds per command
    delegate: bool = True          # delegate_task (sub-agents)
    delegate_steps: int = 10
    skills: bool = True
    optional_skills: bool = False
    enable_skills: list[str] = field(default_factory=list)
    skill_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Settings":
        a, t = cfg.get("agent") or {}, cfg.get("tools") or {}
        return cls(
            workspace=str(a.get("workspace", "")), max_steps=int(a.get("max_steps", 12)),
            max_tokens=int(a.get("max_tokens", 1536)), instructions=str(a.get("instructions", "")),
            timezone=str(a.get("timezone", "")), context_tokens=int(a.get("context_tokens", 64000)),
            keep_recent=int(a.get("keep_recent", 10)), memory=bool(a.get("memory", True)),
            http=bool(t.get("http", True)), search=str(t.get("search", "auto")),
            terminal=bool(t.get("terminal", False)), code=(None if t.get("code") is None else bool(t["code"])),
            confirm=bool(t.get("confirm", True)), timeout=int(t.get("timeout", 180)),
            delegate=bool(t.get("delegate", True)), delegate_steps=int(t.get("delegate_steps", 10)),
            skills=bool(t.get("skills", True)), optional_skills=bool(t.get("optional_skills", False)),
            enable_skills=[str(x) for x in t.get("enable_skills", [])], skill_dirs=[str(x) for x in t.get("skill_dirs", [])],
        )


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    return Settings.from_config(load_config(path))


__all__ = ["load_config", "find_config", "build_router", "load_settings", "Settings", "DEFAULT_PATHS"]
