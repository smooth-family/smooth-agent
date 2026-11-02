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
    processes: Any = None
    voice: Any = None  # VoiceSettings: speech to text / text to speech
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
    default_ws = home / "workspace"  # one fixed place: the persona files live here and the agent can edit them
    ws = Workspace(workspace or (Path(st.workspace).expanduser() if st.workspace else default_ws), access=st.files)
    if ws.root == default_ws.resolve():
        from .persona import ensure_persona
        ensure_persona(ws.root)  # empty templates; unused until the owner (or the agent) writes in them
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

    from .voice import VoiceSettings, add_voice_tools
    vs = VoiceSettings.from_config(cfg)
    add_voice_tools(reg, router, ws, vs)

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
    system += _whereabouts(home, config_path or find_config(), ws.root, st)
    agent = Agent(router, reg, system=system, provider=provider, max_steps=max_steps or st.max_steps,
                  max_tokens=st.max_tokens, skills=lib, delegate=st.delegate, delegate_steps=st.delegate_steps,
                  processes=pm, store=store, memory=mem, session_id=sid, context_tokens=st.context_tokens,
                  keep_recent=st.keep_recent, model=model, persona_dirs=[ws.root, home])
    if st.memory:
        rt_ref["agent"] = agent
    return Runtime(agent=agent, router=router, registry=reg, workspace=ws, settings=st, config=cfg, home=home,
                   store=store, memory=mem, processes=pm, voice=vs, warnings=warnings, _closers=closers)


def build_chat_agent(rt: Runtime, session_id: str, channel: str = "cli", chat_id: str = "") -> Agent:
    """A fresh Agent for one chat, sharing the runtime's router, tools, skills,
    store and memory but with its own saved session. Used by the channel hub so
    each conversation keeps its own history across restarts."""
    reg = rt.registry.copy()  # its own copy so delegate/skill closures bind to this agent
    st = rt.settings
    if rt.store is not None:
        # create (and label) the session before the Agent does, or it would be saved as channel 'cli'
        rt.store.new_session(session_id, channel=channel, meta={"chat_id": chat_id} if chat_id else None)
        rt.store.label_session(session_id, channel)  # repair rows saved by 0.5.0 with the wrong label
    system = rt.agent.system + (CHAT_STYLE.format(channel=channel) if channel not in ("cli", "cron") else "")
    agent = Agent(rt.router, reg, system=system, max_steps=st.max_steps, max_tokens=st.max_tokens,
                  skills=rt.agent.skills, delegate=st.delegate, delegate_steps=st.delegate_steps,
                  processes=rt.processes, store=rt.store, memory=rt.memory, session_id=session_id,
                  context_tokens=st.context_tokens, keep_recent=st.keep_recent,
                  persona_dirs=[rt.workspace.root, rt.home])
    if rt.store is not None and rt.store.session_messages(session_id):
        agent.resume(session_id)  # continue where this chat left off
    return agent


CHAT_STYLE = (
    "\n\nYou are chatting with the owner on {channel}. Write like a helpful person in a chat, not a report: reply in "
    "the owner's language and match their tone (often casual Indonesian — answer casually too), be friendly and "
    "natural, keep it short, use short paragraphs or a simple list, and avoid headers and tables. The owner sees "
    "each tool step live while you work (one progress message that disappears when you answer), so do not send "
    "progress updates with send_message; just do the work and give the final answer. **bold**, `code` and code "
    "blocks are rendered. To give the owner a file (report, spreadsheet, image, audio), create it in the workspace and "
    "send it with send_file instead of pasting long content."
)


def _whereabouts(home: Path, config: Path | None, workspace: Path, st: Settings) -> str:
    """Where this install lives, so the agent never guesses a default path (e.g. the wrong ~/.smooth-agent)."""
    term = "on (each command asks the owner first)" if st.terminal and st.confirm else ("on" if st.terminal else "off")
    if st.files == "full":
        files = ("your file tools reach the whole machine: absolute or ~ paths anywhere, relative paths stay in the "
                 "workspace; writing outside the workspace and reading secret files (keys, .env, ~/.ssh) ask the owner first")
    else:
        files = ('your file tools can only touch the workspace; the owner can open the whole machine with '
                 '[tools] files = "full"')
    return (f"\n\nThis install: home {home}; config {config or home / 'config.toml'}; keys in {home / '.env'}; "
            f"workspace {workspace} ({files}). Terminal/shell tools: {term}; the owner turns them on with "
            "[tools] terminal = true in the config, then restarts the service.")


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


__all__ = ["Runtime", "build_runtime", "build_chat_agent", "find_config"]
