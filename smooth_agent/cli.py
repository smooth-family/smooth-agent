"""Smooth Agent command line.

    smooth-agent setup                  # first run: home folder, config, keys
    smooth-agent run                    # interactive agent with tools
    smooth-agent ask "..."              # one-shot agent run
    smooth-agent chat "..."             # plain streamed chat, no tools
    smooth-agent doctor                 # what works on this machine
    smooth-agent providers | skills | mcp | version
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__
from .config import Settings, find_config, load_config
from .guard import redact
from .home import agent_home, ensure_home, load_dotenv, set_env_value
from .routers import PRESETS, Router, RouterError


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _router(args) -> Router:
    load_dotenv(agent_home() / ".env")
    return Router.from_config(load_config(args.config))


# ---------- version / providers / chat ----------

def _cmd_version(_args) -> int:
    print(f"smooth-agent {__version__}")
    return 0


def _cmd_providers(args) -> int:
    try:
        router = _router(args)
    except (FileNotFoundError, RouterError) as e:
        return _err(str(e))
    print(f"default: {router.default}")
    if router.fallbacks:
        print(f"fallbacks: {', '.join(router.fallbacks)}")
    for p in router.providers.values():
        r = p.redacted()
        mark = "*" if p.name == router.default else " "
        print(f" {mark} {r['name']:<14} {r['provider']:<16} {r['model']:<28} key={r['key']:<10} {r['base_url']}")
    return 0


def _cmd_chat(args) -> int:
    try:
        router = _router(args)
    except (FileNotFoundError, RouterError) as e:
        return _err(str(e))
    prompt = args.prompt or sys.stdin.read()
    if not prompt.strip():
        return _err("empty prompt")
    messages = [{"role": "user", "content": prompt}]
    try:
        if args.no_stream:
            print(router.complete(messages, provider=args.provider, model=args.model, max_tokens=args.max_tokens).text)
        else:
            for delta in router.stream(messages, provider=args.provider, model=args.model, max_tokens=args.max_tokens):
                sys.stdout.write(delta)
                sys.stdout.flush()
            sys.stdout.write("\n")
    except RouterError as e:
        print(f"\nrouter error: {e}", file=sys.stderr)
        return 2
    return 0


# ---------- the agent ----------

_approve_lock = threading.Lock()


def _approver(args, confirm: bool):
    """Ask the owner before terminal / execute_code runs, unless confirm=false or --yes."""
    if getattr(args, "yes", False) or not confirm:
        return lambda _n, _a: True

    def ask(name: str, arguments) -> bool:
        what = arguments.get("command") or arguments.get("code") or json.dumps(arguments, ensure_ascii=False)
        if not sys.stdin.isatty():
            print(f"  ! {name} needs approval but there is no terminal to ask; refused (use --yes or confirm=false)", flush=True)
            return False
        with _approve_lock:  # parallel sub-agents must not interleave prompts
            print(f"\n  ? {name}:\n{redact(what)}", flush=True)
            try:
                return input("    run it? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                return False
    return ask


def _runtime(args):
    from .runtime import build_runtime
    overrides: dict = {}
    if getattr(args, "no_http", False):
        overrides["http"] = False
    if getattr(args, "terminal", False):
        overrides["terminal"] = True
    cfg_confirm = True
    try:
        cfg_confirm = Settings.from_config(load_config(args.config)).confirm
    except FileNotFoundError:
        pass
    rt = build_runtime(args.config, approver=_approver(args, cfg_confirm), provider=args.provider,
                       workspace=args.workspace, max_steps=args.max_steps or None, overrides=overrides)
    rt.agent.on_event = _print_event
    for w in rt.warnings:
        print(f"  ! {w}", file=sys.stderr)
    return rt


def _print_event(event: str, data) -> None:
    sub = event.startswith("sub:")
    event = event[4:] if sub else event
    pad = "      " if sub else "  "
    if event == "tool":
        print(f"{pad}→ {data['name']}({redact(json.dumps(data['arguments'], ensure_ascii=False))[:300]})", flush=True)
    elif event == "tool_result":
        r = redact(data["result"])
        print(f"{pad}  {r[:200]}{'…' if len(r) > 200 else ''}", flush=True)
    elif event == "error":
        print(f"{pad}! {redact(data['message'])}", flush=True)
    elif event == "process_done":
        print(f"{pad}✓ {redact(data['text'].splitlines()[0])}", flush=True)


def _cmd_ask(args) -> int:
    prompt = args.prompt or (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not prompt.strip():
        return _err("empty prompt")
    try:
        rt = _runtime(args)
    except (FileNotFoundError, RouterError) as e:
        return _err(str(e))
    with rt:
        turn = rt.agent.run(prompt)
        print(turn.text)
        if turn.stopped:
            print(f"\n[stopped: {turn.stopped}, {turn.steps} steps]", file=sys.stderr)
    return 0


def _cmd_run(args) -> int:
    try:
        rt = _runtime(args)
    except (FileNotFoundError, RouterError) as e:
        return _err(str(e))
    with rt:
        tools = len(rt.registry.names())
        skills = len(rt.agent.skills.available()) if rt.agent.skills else 0
        print(f"smooth-agent {__version__} — {tools} tools, {skills} skills — workspace: {rt.workspace.root}")
        print("type a message, /reset to clear, /exit to quit.")
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in ("/exit", "/quit"):
                return 0
            if line == "/reset":
                rt.agent.reset()
                print("(conversation reset)")
                continue
            turn = rt.agent.run(line)
            print(f"\nsmooth> {turn.text}")


# ---------- setup / doctor ----------

def _cmd_setup(args) -> int:
    home = ensure_home()
    cfg_path = home / "config.toml"
    example = Path(__file__).parent / "data" / "config.example.toml"
    print(f"Smooth Agent home: {home}")
    if cfg_path.exists() and not args.force:
        print(f"config already exists: {cfg_path} (use --force to start over)")
    else:
        provider = args.provider or _ask("LLM provider", "deepseek", choices=[k for k in PRESETS if k != "openai-compatible"])
        preset = PRESETS[provider]
        default_models = {"openai": "gpt-5-mini", "anthropic": "claude-haiku-5-5", "deepseek": "deepseek-chat",
                          "glm": "glm-4.6", "groq": "llama-3.3-70b-versatile", "openrouter": "anthropic/claude-haiku-5-5",
                          "kimi": "kimi-k2-turbo-preview", "smooth-router": "anthropic/claude-haiku-5-5", "9router": "cc/claude-haiku-5-5"}
        model = args.model or _ask("model", default_models.get(provider, ""))
        text = example.read_text(encoding="utf-8")
        text = text.replace('provider = "deepseek"        # PROVIDER', f'provider = "{provider}"').replace('model = "deepseek-chat"       # MODEL', f'model = "{model}"')
        cfg_path.write_text(text, encoding="utf-8")
        print(f"wrote {cfg_path}")
        key_env = preset.get("key_env")
        if key_env and not os.environ.get(key_env):
            key = args.key if args.key is not None else _ask(f"{key_env} (leave empty to set later)", "", secret=True)
            if key:
                set_env_value(key_env, key)
                print(f"saved {key_env} to {home / '.env'} (mode 600)")
            else:
                print(f"set it later: smooth-agent setup --key ... or add {key_env}=... to {home / '.env'}")
    (home / "skills").mkdir(exist_ok=True)
    print("\nnext: smooth-agent doctor   then   smooth-agent run")
    return 0


def _ask(label: str, default: str, *, choices: list[str] | None = None, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        return default
    hint = f" [{'/'.join(choices)}]" if choices and len(choices) < 12 else ""
    if choices and len(choices) >= 12:
        print(f"{label} options: {', '.join(choices)}")
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"{label}: ")
        else:
            val = input(f"{label}{hint} [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    val = val or default
    if choices and val not in choices:
        print(f"  unknown choice '{val}', using {default}")
        return default
    return val


def _cmd_doctor(args) -> int:
    ok = lambda b: "ok " if b else "-- "  # noqa: E731
    home = agent_home()
    load_dotenv(home / ".env")
    print(f"smooth-agent {__version__} on Python {sys.version.split()[0]} ({sys.platform})")
    print(f"{ok(home.exists())} home        {home}")
    cfg_path = find_config() if not args.config else Path(args.config)
    print(f"{ok(bool(cfg_path))} config      {cfg_path or 'not found — run smooth-agent setup'}")
    if not cfg_path:
        return 1
    cfg = load_config(args.config)
    try:
        router = Router.from_config(cfg)
        for p in router.providers.values():
            print(f"{ok(p.ready)} provider    {p.name} ({p.provider}, {p.model}){'' if p.ready else ' — key missing: ' + (p.key_env or '?')}")
        if args.ping:
            try:
                c = router.complete([{"role": "user", "content": "Reply with: ok"}], max_tokens=10)
                print(f"ok  ping        {c.provider} answered in {c.first_ms} ms: {c.text.strip()[:40]!r}")
            except RouterError as e:
                print(f"--  ping        {e}")
    except RouterError as e:
        print(f"--  providers   {e}")
    st = Settings.from_config(cfg)
    from .browser import BrowserSettings, available
    bs = BrowserSettings.from_config(cfg)
    print(f"{ok(st.http)} web tools   search backend: {st.search}")
    print(f"{ok(st.terminal)} terminal    {'on' if st.terminal else 'off'} (confirm={'yes' if st.confirm else 'no'})")
    print(f"{ok(bs.enabled and available())} browser     {'on' if bs.enabled else 'off'}; playwright {'installed' if available() else 'missing'}")
    from .skills import SkillLibrary
    lib = SkillLibrary(optional=st.optional_skills, enabled=set(st.enable_skills))
    missing = sorted({b for s in lib.available() for b in s.missing_bins})
    print(f"ok  skills      {len(lib.available())} available here; {len(lib.hidden_optional)} optional off")
    if missing:
        print(f"    some skills want: {', '.join(missing[:20])}")
    from .mcp import load_servers
    servers = load_servers(cfg, home)
    print(f"{ok(bool(servers))} mcp         {', '.join(servers) or 'no servers (smooth-agent mcp add …)'}")
    for b in ("git", "node", "npx", "ffmpeg", "gh"):
        print(f"{ok(bool(shutil.which(b)))} {b:<11} {shutil.which(b) or 'not found'}")
    return 0


# ---------- skills ----------

def _skill_lib(args, include_optional: bool = False):
    from .skills import SkillLibrary
    try:
        st = Settings.from_config(load_config(args.config))
    except FileNotFoundError:
        st = Settings()
    return SkillLibrary(workspace=Path(st.workspace).expanduser() if st.workspace else Path.cwd() / "workspace",
                        extra_dirs=[Path(d) for d in st.skill_dirs], optional=st.optional_skills or include_optional,
                        enabled=set(st.enable_skills))


def _cmd_skills(args) -> int:
    action = args.action or "list"
    args.arg = " ".join(args.words) if args.words else None
    if action == "list":
        lib = _skill_lib(args, args.all)
        items = lib.list(args.arg)
        for s in items:
            need = f"  (needs: {', '.join(s.missing_bins)})" if s.missing_bins else ""
            print(f"{s.source:<9} {s.category or '-':<22} {s.name:<32} {s.description[:70]}{need}")
        print(f"\n{len(items)} skills available here ({len(lib.skills)} loaded, {len(lib.hidden_optional)} optional not enabled)")
        return 0
    if action == "search":
        if not args.arg:
            return _err("usage: smooth-agent skills search <words>")
        lib = _skill_lib(args, True)
        words = [w.lower() for w in args.arg.split()]
        scored = []
        for s in list(lib.skills.values()) + list(lib.hidden_optional.values()):
            hay = f"{s.name} {s.category} {s.description}".lower()
            matched = [w for w in words if w in hay]
            score = len(matched) * 100 + sum(hay.count(w) * (3 if w in s.name else 1) for w in matched)
            if score:
                scored.append((score, s))
        for _, s in sorted(scored, key=lambda x: -x[0])[:25]:
            tag = "optional (off)" if s.name in lib.hidden_optional and s.name not in lib.skills else s.source
            print(f"{s.name:<32} {tag:<15} {s.description[:80]}")
        return 0 if scored else _err("no matching skills")
    if action == "install":
        return _install_skill(args.arg)
    if action == "remove":
        from .skills import user_dir
        d = user_dir() / str(args.arg or "")
        if not args.arg or not (d / "SKILL.md").exists():
            return _err(f"no user skill '{args.arg}' in {user_dir()}")
        shutil.rmtree(d)
        print(f"removed {d}")
        return 0
    return _err("actions: list [category], search <words>, install <git-url|path>, remove <name>")


def _install_skill(src: str | None) -> int:
    """Install skills from a git repository or a local folder into the user skills dir."""
    import tempfile
    from .skills import parse_front_matter, user_dir
    if not src:
        return _err("usage: smooth-agent skills install <git-url | path>")
    dest_root = user_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = None
    if src.startswith(("http://", "https://", "git@")) or src.endswith(".git"):
        if not shutil.which("git"):
            return _err("git is needed to install from a URL")
        tmp = tempfile.mkdtemp(prefix="smooth-skill-")
        r = subprocess.run(["git", "clone", "--depth", "1", src, tmp], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return _err(f"git clone failed: {r.stderr.strip()[:300]}")
        root = Path(tmp)
    elif not Path(src).expanduser().exists():
        # a bundled optional skill by name ("telephony", or the long form "official/productivity/telephony")
        from .skills import BUNDLED, SkillLibrary
        name = src.rstrip("/").split("/")[-1]
        lib = SkillLibrary(user_dir=None, optional=True)
        sk = lib.skills.get(name)
        if not sk:
            return _err(f"no such path, URL or bundled skill: {src} (try: smooth-agent skills search {name})")
        target = dest_root / sk.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(sk.path, target)
        print(f"installed '{sk.name}' ({sk.source}) into {target}; it is active from the next message")
        return 0
    else:
        root = Path(src).expanduser()
    installed = []
    try:
        for md in sorted(root.rglob("SKILL.md")):
            if ".git" in md.parts:
                continue
            meta, _ = parse_front_matter(md.read_text(encoding="utf-8", errors="replace"))
            name = str(meta.get("name") or md.parent.name).strip()
            if not meta.get("description"):
                print(f"  skipped {md.parent} (no description in front matter)")
                continue
            target = dest_root / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(md.parent, target, ignore=shutil.ignore_patterns(".git"))
            installed.append(name)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if not installed:
        return _err("no SKILL.md with a name and description found")
    print(f"installed {len(installed)} skill(s) into {dest_root}: {', '.join(installed)}")
    return 0


# ---------- mcp ----------

def _cmd_mcp(args) -> int:
    from .mcp import MCPClient, MCPError, ServerConfig, load_servers, remove_server, save_server
    home = ensure_home()
    load_dotenv(home / ".env")
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        cfg = {}
    action = args.action or "list"
    if action == "list":
        servers = load_servers(cfg, home)
        if not servers:
            print("no MCP servers. add one:\n  smooth-agent mcp add <name> -- <command> [args...]\n  smooth-agent mcp add <name> --url https://…/mcp")
        for n, s in servers.items():
            where = s.url or " ".join([s.command, *s.args])
            print(f"{n:<20} {'on ' if s.enabled else 'off'} {where}")
        return 0
    if action == "add":
        if not args.name:
            return _err("usage: smooth-agent mcp add <name> (--url URL | -- command args...)")
        headers = dict(h.split("=", 1) for h in args.header or [] if "=" in h)
        env = dict(e.split("=", 1) for e in args.env or [] if "=" in e)
        cmd = [c for c in (args.cmd or []) if c != "--"]
        if not args.url and not cmd:
            return _err("give --url for a remote server, or `-- command args...` for a local one")
        sc = ServerConfig(name=args.name, url=args.url or "", headers=headers, command=cmd[0] if cmd else "",
                          args=cmd[1:], env=env)
        p = save_server(home, sc)
        print(f"saved '{args.name}' to {p}")
        if not args.no_check:
            return _mcp_check(sc)
        return 0
    if action == "remove":
        if not remove_server(home, args.name or ""):
            return _err(f"no server '{args.name}' in {home / 'mcp.json'}")
        print(f"removed {args.name}")
        return 0
    if action == "tools":
        servers = load_servers(cfg, home)
        names = [args.name] if args.name else list(servers)
        rc = 0
        for n in names:
            if n not in servers:
                rc = _err(f"no server '{n}'")
                continue
            rc = _mcp_check(servers[n]) or rc
        return rc
    return _err("actions: list, add, remove, tools")


def _mcp_check(sc) -> int:
    from .mcp import MCPClient, MCPError, tool_name
    try:
        c = MCPClient(sc).start()
    except (MCPError, OSError) as e:
        return _err(f"{sc.name}: {e}")
    try:
        info = c.server_info
        print(f"{sc.name}: connected to {info.get('name', '?')} {info.get('version', '')} — {len(c.tools)} tools")
        for t in c.tools:
            print(f"  {tool_name(sc.name, t.get('name', '')):<40} {(t.get('description') or '')[:70]}")
    finally:
        c.close()
    return 0


# ---------- parser ----------

def _add_agent_args(sp) -> None:
    sp.add_argument("-p", "--provider", help="provider name (default from config)")
    sp.add_argument("-w", "--workspace", help="workspace directory the agent can read/write (default ./workspace)")
    sp.add_argument("--max-steps", type=int, default=0, help="max tool-call rounds per message (default from config)")
    sp.add_argument("--no-http", action="store_true", help="disable web tools")
    sp.add_argument("--terminal", action="store_true", help="enable terminal + execute_code for this run")
    sp.add_argument("-y", "--yes", action="store_true", help="run terminal / execute_code without asking")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smooth-agent", description="Self-hostable, multi-router AI agent.")
    p.add_argument("-c", "--config", help="path to a config file (else auto-discovered)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("version", help="print version")
    sp.set_defaults(func=_cmd_version)

    sp = sub.add_parser("setup", help="create ~/.smooth-agent, a config and your key")
    sp.add_argument("--provider", help="provider preset (deepseek, openai, anthropic, glm, ...)")
    sp.add_argument("--model")
    sp.add_argument("--key", help="API key to store in ~/.smooth-agent/.env")
    sp.add_argument("--force", action="store_true", help="overwrite an existing config")
    sp.set_defaults(func=_cmd_setup)

    sp = sub.add_parser("doctor", help="check what works on this machine")
    sp.add_argument("--ping", action="store_true", help="also send one tiny request to the default provider")
    sp.set_defaults(func=_cmd_doctor)

    sp = sub.add_parser("providers", help="list configured providers")
    sp.set_defaults(func=_cmd_providers)

    sp = sub.add_parser("chat", help="one-shot chat, no tools")
    sp.add_argument("prompt", nargs="?", help="prompt text (or read from stdin)")
    sp.add_argument("-p", "--provider", help="provider name (default from config)")
    sp.add_argument("-m", "--model", help="override the model")
    sp.add_argument("--max-tokens", type=int, default=1024)
    sp.add_argument("--no-stream", action="store_true", help="wait for the full reply")
    sp.set_defaults(func=_cmd_chat)

    sp = sub.add_parser("ask", help="one-shot agent run with tools")
    sp.add_argument("prompt", nargs="?", help="prompt text (or read from stdin)")
    _add_agent_args(sp)
    sp.set_defaults(func=_cmd_ask)

    sp = sub.add_parser("run", help="interactive agent session with tools")
    _add_agent_args(sp)
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("skills", help="list / search / install / remove skills")
    sp.add_argument("action", nargs="?", choices=["list", "search", "install", "remove"], default="list")
    sp.add_argument("words", nargs="*", help="category (list), words (search), git URL or path (install), name (remove)")
    sp.add_argument("--all", action="store_true", help="list: include optional skills")
    sp.set_defaults(func=_cmd_skills)

    sp = sub.add_parser("mcp", help="MCP servers: list / add / remove / tools")
    sp.add_argument("action", nargs="?", choices=["list", "add", "remove", "tools"], default="list")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--url", help="remote server (Streamable HTTP)")
    sp.add_argument("--header", action="append", help="KEY=VALUE header for --url (repeatable; ${VAR} reads env)")
    sp.add_argument("--env", action="append", help="KEY=VALUE env for a local server (repeatable; ${VAR} reads env)")
    sp.add_argument("--no-check", action="store_true", help="add without connecting")
    sp.add_argument("cmd", nargs=argparse.REMAINDER, help="after --: the command that starts a local server")
    sp.set_defaults(func=_cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if find_config():
            return _cmd_providers(args)
        parser.print_help()
        print("\nfirst time? run: smooth-agent setup")
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
