"""Assemble a ready-to-run Smooth Agent from the config.

`build_runtime()` is the one place that wires providers, tools, skills,
background processes, vision/image/speech, the browser and MCP servers into an
Agent. The CLI, the scheduler and the chat channels all start from here, so they
behave the same.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import DEFAULT_SYSTEM, Agent
from .config import Settings, find_config, load_config
from .home import ensure_home, load_dotenv
from .routers import Router
from .skills import SkillLibrary
from .tools import Approver, Registry, Workspace, default_registry


@dataclass
class Runtime:
    agent: Agent
    router: Router
    registry: Registry
    workspace: Workspace
    settings: Settings
    config: dict[str, Any]
    home: Path
    store: Any = None
    memory: Any = None
    warnings: list[str] = field(default_factory=list)
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for fn in reversed(self._closers):
            try:
                fn()
            except Exception:
                pass
        self._closers.clear()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def new_session_id(prefix: str = "") -> str:
    import time
    import uuid
    return f"{prefix}{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def build_runtime(config_path: str | None = None, *, approver: Approver | None = None, provider: str | None = None,
                  workspace: str | None = None, max_steps: int | None = None, overrides: dict[str, Any] | None = None,
                  connect_mcp: bool = True, ask: Any = None, session_id: str | None = None, channel: str = "cli",
                  model: str | None = None) -> Runtime:
    home = ensure_home()
    load_dotenv(home / ".env")
    cfg = load_config(config_path)
    st = Settings.from_config(cfg)
    for k, v in (overrides or {}).items():
        setattr(st, k, v)
    router = Router.from_config(cfg)
    ws = Workspace(workspace or (Path(st.workspace).expanduser() if st.workspace else Path.cwd() / "workspace"))
    secret_envs = tuple(p.key_env for p in router.providers.values() if p.key_env)
    warnings: list[str] = []
    closers: list[Callable[[], None]] = []

    from .processes import ProcessManager, add_process_tools
    pm = ProcessManager(home / "processes")
    closers.append(pm.shutdown)

    reg = default_registry(ws, allow_http=st.http, terminal=st.terminal, code=st.code, terminal_timeout=st.timeout,
                           secret_env_names=secret_envs, approver=approver, search_backend=st.search,
                           processes=pm if st.terminal else None)
    if st.terminal:
        add_process_tools(reg, pm)

    from .media import MediaSettings, add_media_tools, describe
    ms = MediaSettings.from_config(cfg)
    add_media_tools(reg, router, ws, ms)

    from .browser import BrowserSession, BrowserSettings, add_browser_tools, available
    bs = BrowserSettings.from_config(cfg)
    if bs.enabled:
        if available():
            session = BrowserSession(bs, profile_root=home)
            closers.append(session.close)
            add_browser_tools(reg, session, ws, describe_image=lambda png, q: describe(router, (png, "image/png"), q, ms))
        else:
            warnings.append('browser is enabled but Playwright is missing: pip install "smooth-agent[browser]" && python -m playwright install chromium')

    if connect_mcp:
        from .mcp import MCPManager, load_servers
        servers = load_servers(cfg, home)
        if servers:
            mgr = MCPManager(servers)
            mgr.connect(dict(os.environ))
            closers.append(mgr.close)
            mcp_cfg = cfg.get("mcp") or {}
            mgr.register(reg, needs_approval=bool(mcp_cfg.get("confirm", False)))
            warnings += [f"MCP server '{n}' did not start: {e}" for n, e in mgr.errors.items()]

    store = mem = None
    sid = session_id or new_session_id()
    if st.memory:
        from .interact import add_session_tools
        from .memory import Memory, add_memory_tool
        from .scheduler import Crons, Jobs, add_cron_tool
        from .store import Store
        store = Store(home / "smooth.db")
        store.new_session(sid, channel=channel)
        mem = Memory(store)
        add_memory_tool(reg, mem, session_id=sid)
        rt_ref: dict[str, Any] = {}
        add_session_tools(reg, store, lambda: rt_ref["agent"].session_id if "agent" in rt_ref else sid)
        add_cron_tool(reg, Crons(store, st.timezone or None), Jobs(store), home, scripts_allowed=st.terminal)
    from .interact import add_clarify_tool
    add_clarify_tool(reg, ask)

    lib = None
    if st.skills:
        lib = SkillLibrary(workspace=ws.root, extra_dirs=[Path(d) for d in st.skill_dirs],
                           optional=st.optional_skills, enabled=set(st.enable_skills))
    system = DEFAULT_SYSTEM + (f"\n\nOwner instructions:\n{st.instructions}" if st.instructions else "")
    system += _clock(st.timezone)
    agent = Agent(router, reg, system=system, provider=provider, max_steps=max_steps or st.max_steps,
                  max_tokens=st.max_tokens, skills=lib, delegate=st.delegate, delegate_steps=st.delegate_steps,
                  processes=pm, store=store, memory=mem, session_id=sid, context_tokens=st.context_tokens,
                  keep_recent=st.keep_recent, model=model)
    if st.memory:
        rt_ref["agent"] = agent
    return Runtime(agent=agent, router=router, registry=reg, workspace=ws, settings=st, config=cfg, home=home,
                   store=store, memory=mem, warnings=warnings, _closers=closers)


def _clock(tz_name: str) -> str:
    """Today's date and the timezone, so 'tomorrow 9am' and schedules come out right (date only: keeps the prompt cacheable)."""
    from datetime import datetime
    from .schedule import get_tz
    try:
        tz = get_tz(tz_name or None)
    except Exception:
        tz = get_tz(None)
    now = datetime.now(tz)
    return f"\n\nToday is {now:%A %Y-%m-%d}; the owner's timezone is {tz_name or now.strftime('%Z') or 'local'} (UTC{now:%z})."


__all__ = ["Runtime", "build_runtime", "find_config"]
