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
                       workspace=args.workspace, max_steps=args.max_steps or None, overrides=overrides,
                       ask=_clarify_prompt if sys.stdin.isatty() else None)
    rt.agent.on_event = _print_event
    for w in rt.warnings:
        print(f"  ! {w}", file=sys.stderr)
    return rt


def _clarify_prompt(question: str, choices: list[str]) -> str | None:
    with _approve_lock:
        print(f"\n  ? {question}", flush=True)
        for i, c in enumerate(choices, 1):
            print(f"    {i}. {c}")
        try:
            ans = input("    answer: ").strip()
        except EOFError:
            return None
    if ans.isdigit() and choices and 1 <= int(ans) <= len(choices):
        return choices[int(ans) - 1]
    return ans or None


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
    elif event == "compressed":
        print(f"{pad}(compressed {data['messages']} older messages to keep the context small)", flush=True)


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
        if args.resume:
            if rt.store is None:
                return _err("sessions are off ([agent] memory = false)")
            sid = args.resume
            if sid == "last":
                rows = [r for r in rt.store.sessions(5, channel="cli") if r["id"] != rt.agent.session_id and r["n"]]
                if not rows:
                    return _err("no earlier session to resume")
                sid = rows[0]["id"]
            n = rt.agent.resume(sid)
            print(f"resumed session {sid} ({n} messages)")
        tools = len(rt.registry.names())
        skills = len(rt.agent.skills.available()) if rt.agent.skills else 0
        mem = f", {rt.memory.count()} memories" if rt.memory else ""
        print(f"smooth-agent {__version__} — {tools} tools, {skills} skills{mem} — workspace: {rt.workspace.root}")
        print("type a message; /reset starts a new session, /exit quits.")
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
                print(f"(new session {rt.agent.session_id})")
                continue
            turn = rt.agent.run(line)
            print(f"\nsmooth> {turn.text}")


# ---------- setup / doctor ----------

def _cmd_setup(args) -> int:
    from .banner import Steps, bold, cyan, dim, print_banner
    home = ensure_home()
    load_dotenv(home / ".env")  # a key already saved in .env counts as set
    cfg_path = home / "config.toml"
    example = Path(__file__).parent / "data" / "config.example.toml"
    interactive = sys.stdin.isatty()
    print_banner(__version__)
    print(f"  {dim('home')}  {home}")
    steps = Steps(4)

    steps.start("Model provider")
    keep = cfg_path.exists() and not args.force
    provider = model = ""
    if keep:
        steps.ok(f"keeping {cfg_path}")
        steps.note("run `smooth-agent setup --force` to start over")
    else:
        choices = [k for k in PRESETS if k != "openai-compatible"]
        if interactive and not args.provider:
            steps.note("options: " + ", ".join(choices))
        provider = args.provider or _ask("        provider", "deepseek", choices=choices)
        default_models = {"openai": "gpt-5-mini", "anthropic": "claude-haiku-5-5", "deepseek": "deepseek-chat",
                          "glm": "glm-4.6", "groq": "llama-3.3-70b-versatile", "openrouter": "anthropic/claude-haiku-5-5",
                          "kimi": "kimi-k2-turbo-preview", "smooth-router": "anthropic/claude-haiku-5-5", "9router": "cc/claude-haiku-5-5"}
        model = args.model or _ask("        model", default_models.get(provider, ""))
        text = example.read_text(encoding="utf-8")
        text = text.replace('provider = "deepseek"        # PROVIDER', f'provider = "{provider}"').replace('model = "deepseek-chat"       # MODEL', f'model = "{model}"')
        cfg_path.write_text(text, encoding="utf-8")
        steps.ok(f"{bold(provider)} · {model}")
        steps.note(f"wrote {cfg_path}")

    steps.start("API key")
    key_env = PRESETS[provider].get("key_env") if provider else None
    if keep:
        steps.ok("unchanged")
    elif not key_env:
        steps.ok("this provider needs no key")
    elif os.environ.get(key_env):
        steps.ok(f"{key_env} found in the environment")
    else:
        key = args.key if args.key is not None else _ask(f"        {key_env} (blank = later)", "", secret=True)
        if key:
            set_env_value(key_env, key)
            steps.ok(f"saved {key_env} to {home / '.env'} (mode 600)")
        else:
            steps.warn(f"no key yet — add {key_env}=… to {home / '.env'} or rerun with --key")

    steps.start("Chat channels")
    (home / "skills").mkdir(exist_ok=True)
    names: list[str] = []
    if getattr(args, "channels", None) is not None:
        names = _setup_channels(cfg_path, args.channels, steps, interactive=False)
    elif interactive and not getattr(args, "no_channels", False):
        names = _setup_channels(cfg_path, None, steps, interactive=True)
    if not names:
        steps.ok("skipped — terminal only for now")
        steps.note("add later: smooth-agent setup --channels telegram")

    steps.start("Ready")
    nexts = [("smooth-agent doctor", "check what works on this machine"), ("smooth-agent run", "talk to your agent")]
    if names and Path("/.dockerenv").exists():
        nexts += [("docker compose up -d", f"start {', '.join(names)} + scheduled jobs")]
    elif names:
        nexts += [("smooth-agent daemon", f"start {', '.join(names)} + scheduled jobs"),
                  ("smooth-agent service --install", "keep it running after reboot")]
    width = max(len(c) for c, _ in nexts)
    for cmd, what in nexts:
        steps.ok(f"{cyan(cmd.ljust(width))}   {what}")  # pad before colouring so columns line up
    print()
    return 0


def _strip_channels_table(text: str) -> str:
    """Remove an existing [channels] table and its [channels.*] sub-tables (live
    ones; commented lines are left alone) so a fresh one can be appended
    without a duplicate-table TOML error."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            skipping = s == "[channels]" or s.startswith("[channels.")
        if not skipping:
            out.append(line)
    return "".join(out)


# what each channel needs, for the config table and the "fill these in" hints
_CHANNEL_TABLES = {
    "telegram": ('token_env = "TELEGRAM_BOT_TOKEN"\nowners = {owners}\nauto_approve = false\n',
                 "TELEGRAM_BOT_TOKEN (from @BotFather) and your numeric id in owners"),
    "whatsapp": ('token_env = "WHATSAPP_TOKEN"\nphone_id = ""\nverify_token = ""\napp_secret_env = "WHATSAPP_APP_SECRET"\nowners = {owners}\n',
                 "WHATSAPP_TOKEN, phone_id, verify_token (+ WHATSAPP_APP_SECRET to check signatures); webhook -> <host>:8808/webhook/whatsapp"),
    "wechat": ('webhook_url_env = "WECHAT_ROBOT_URL"\nowners = ["robot"]\n', "WECHAT_ROBOT_URL (group robot)"),
    "slack": ('token_env = "SLACK_BOT_TOKEN"\nsigning_secret_env = "SLACK_SIGNING_SECRET"\nowners = {owners}\n',
              "SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET; Event Subscriptions AND Interactivity -> <host>:8808/webhook/slack; "
              "scopes chat:write, im:history, files:read, files:write"),
    "matrix": ('homeserver = "https://matrix.org"\ntoken_env = "MATRIX_TOKEN"\nowners = {owners}\n',
               "MATRIX_TOKEN (bot access token), homeserver, your @id in owners"),
    "line": ('token_env = "LINE_CHANNEL_TOKEN"\nsecret_env = "LINE_CHANNEL_SECRET"\npublic_url = ""\nowners = {owners}\n',
             "LINE_CHANNEL_TOKEN, LINE_CHANNEL_SECRET; webhook -> <host>:8808/webhook/line; "
             "public_url = your https address of port 8808 to send files"),
    "discord": ('token_env = "DISCORD_BOT_TOKEN"\nowners = {owners}\nguild_mode = "mention"\n',
                "DISCORD_BOT_TOKEN (Developer Portal → Bot; MESSAGE CONTENT intent for server channels) and your user id "
                "in owners — or only webhook_url_env = \"DISCORD_WEBHOOK_URL\" for delivery"),
}


def _setup_channels(cfg_path: Path, pick: str | None, steps, *, interactive: bool) -> list[str]:
    """Pick chat channels and write their [channels.*] tables. Returns the names enabled."""
    from .channels import CHANNEL_TYPES
    if pick is None:
        steps.note("available: " + ", ".join(CHANNEL_TYPES))
        pick = _ask("        enable (comma-separated, blank = skip)", "")
    names, unknown = [], []
    for n in (x.strip().lower() for x in str(pick).split(",") if x.strip()):
        (names if n in CHANNEL_TYPES else unknown).append(n)
    names = list(dict.fromkeys(names))
    for n in unknown:
        steps.warn(f"unknown channel '{n}' (have: {', '.join(CHANNEL_TYPES)})")
    if not names:
        return []
    owners: dict[str, str] = {}
    if interactive and "telegram" in names:
        token = _ask("        TELEGRAM_BOT_TOKEN (blank = later)", "", secret=True)
        if token:
            set_env_value("TELEGRAM_BOT_TOKEN", token)
            steps.ok("saved TELEGRAM_BOT_TOKEN to .env")
        owner = _ask("        your Telegram numeric id (blank = later)", "")
        if owner:
            owners["telegram"] = owner
    text = _strip_channels_table(cfg_path.read_text(encoding="utf-8")).rstrip("\n") + "\n"
    text += f'\n[channels]\nenabled = {json.dumps(names)}\nwebhook_host = "0.0.0.0"\nwebhook_port = 8808\n'
    for n in names:
        body, _hint = _CHANNEL_TABLES[n]
        text += f"\n[channels.{n}]\n" + body.replace("{owners}", json.dumps([owners[n]] if n in owners else []))
    cfg_path.write_text(text, encoding="utf-8")
    steps.ok("enabled " + ", ".join(names))
    for n in names:
        steps.note(f"{n}: {_CHANNEL_TABLES[n][1]}")
    if any(n not in owners and n != "wechat" for n in names):
        steps.note("tip: message your bot once — it replies with the id to put in owners")
    return names


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
    from .banner import dim, green
    ok = lambda b: green("ok ") if b else dim("-- ")  # noqa: E731
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
                print(f"{ok(True)} ping        {c.provider} answered in {c.first_ms} ms: {c.text.strip()[:40]!r}")
            except RouterError as e:
                print(f"{ok(False)} ping        {e}")
    except RouterError as e:
        print(f"{ok(False)} providers   {e}")
    st = Settings.from_config(cfg)
    from .browser import BrowserSettings, available
    bs = BrowserSettings.from_config(cfg)
    print(f"{ok(st.http)} web tools   search backend: {st.search}")
    print(f"{ok(st.terminal)} terminal    {'on' if st.terminal else 'off'} (confirm={'yes' if st.confirm else 'no'})")
    print(f"{ok(bs.enabled and available())} browser     {'on' if bs.enabled else 'off'}; playwright {'installed' if available() else 'missing'}")
    from .connect import EmailSettings
    es = EmailSettings.from_config(cfg)
    print(f"{ok(es.configured)} email       " + (f"{es.user} via {es.imap_host}" if es.configured else "off (set [email] user + EMAIL_PASSWORD)"))
    gh_env = str((cfg.get("github") or {}).get("token_env") or "GITHUB_TOKEN")
    print(f"{ok(bool(os.environ.get(gh_env)))} github      " + (f"{gh_env} set" if os.environ.get(gh_env) else f"no {gh_env} (public reads only)")
          + f"; git {'installed' if shutil.which('git') else 'missing'}")
    from .skills import SkillLibrary
    lib = SkillLibrary(optional=st.optional_skills, enabled=set(st.enable_skills))
    missing = sorted({b for s in lib.available() for b in s.missing_bins})
    print(f"{ok(True)} skills      {len(lib.available())} available here; {len(lib.hidden_optional)} optional off")
    if missing:
        print(f"    some skills want: {', '.join(missing[:20])}")
    from .config import load_channels
    chcfg = load_channels(cfg)
    if chcfg.specs:
        for s in chcfg.specs:
            has = bool(s.options.get("token") or s.options.get("webhook_url"))
            print(f"{ok(has and bool(s.owners))} channel     {s.name} — owners {len(s.owners)}, credential {'set' if has else 'MISSING'}")
    else:
        print(f"{ok(False)} channels    none enabled ([channels] enabled = [...])")
    from .mcp import load_servers
    servers = load_servers(cfg, home)
    print(f"{ok(bool(servers))} mcp         {', '.join(servers) or 'no servers (smooth-agent mcp add …)'}")
    try:
        import pypdf  # noqa: F401
        pdf = "pypdf"
    except ImportError:
        pdf = "pdftotext" if shutil.which("pdftotext") else ""
    print(f"{ok(bool(pdf))} pdf         {pdf or 'missing — pip install pypdf'}"
          f"{'; scanned PDFs → images via pdftoppm' if shutil.which('pdftoppm') else '; scanned PDFs need pdftoppm (poppler)'}")
    print(f"{ok(True)} office      Word, Excel, PowerPoint, OpenDocument (built in)"
          f"{'; old .doc/.xls via LibreOffice' if shutil.which('libreoffice') or shutil.which('soffice') else ''}")
    rar = next((b for b in ("unrar", "7z", "7zz", "unar", "bsdtar") if shutil.which(b)), "")
    print(f"{ok(bool(rar))} archives    zip, tar, gz built in; rar/7z {'via ' + rar if rar else 'need unrar, unar or 7-Zip'}")
    for b in ("git", "node", "npx", "ffmpeg", "gh", "tesseract"):
        print(f"{ok(bool(shutil.which(b)))} {b:<11} {shutil.which(b) or 'not found'}")
    return 0


# ---------- skills ----------

def _skill_lib(args, include_optional: bool = False):
    from .skills import SkillLibrary
    try:
        st = Settings.from_config(load_config(args.config))
    except FileNotFoundError:
        st = Settings()
    return SkillLibrary(workspace=Path(st.workspace).expanduser() if st.workspace else agent_home() / "workspace",
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


# ---------- sessions / memory ----------

def _store():
    from .store import Store
    return Store(ensure_home() / "smooth.db")


def _when(ts: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts)) if ts else "-"


def _cmd_sessions(args) -> int:
    st = _store()
    action = args.action or "list"
    if action == "list":
        for r in st.sessions(args.limit):
            print(f"{r['id']:<28} {_when(r['updated'])}  {r['channel']:<8} {r['n']:>4} msgs  {(r['title'] or '')[:60]}")
        return 0
    if action == "show":
        if not args.arg:
            return _err("usage: smooth-agent sessions show <id>")
        for m in st.session_messages(args.arg):
            role = m.get("role")
            if role == "tool":
                print(f"  [tool result] {redact(str(m.get('content')))[:300]}")
            elif m.get("tool_calls"):
                for c in m["tool_calls"]:
                    print(f"  [{c.get('function', {}).get('name')}] {redact(str(c.get('function', {}).get('arguments')))[:200]}")
            else:
                print(f"\n{role}> {redact(str(m.get('content') or ''))}")
        return 0
    if action == "search":
        rows = st.search_messages(args.arg or "", args.limit)
        for r in rows:
            print(f"{_when(r['created'])} [{r['session_id']}] {r['role']}: {redact(r['snip'])}")
        return 0 if rows else _err("nothing found")
    if action == "delete":
        if not args.arg or not st.delete_session(args.arg):
            return _err(f"no session '{args.arg}'")
        print(f"deleted {args.arg}")
        return 0
    return _err("actions: list, show <id>, search <words>, delete <id>")


def _cmd_memory(args) -> int:
    from .memory import KINDS, Memory
    from .tools import ToolError
    mem = Memory(_store())
    action = args.action or "list"
    try:
        if action == "list":
            rows = mem.list(args.kind)
            for r in rows:
                print(f"#{r['id']:<5} {r['kind']:<11}{' *' if r['pinned'] else '  '} {r['text']}")
            print(f"\n{len(rows)} memories" + (" (* = pinned)" if any(r["pinned"] for r in rows) else ""))
            return 0
        if action == "add":
            if not args.text:
                return _err('usage: smooth-agent memory add "text" [--kind preference]')
            print(f"remembered #{mem.add(' '.join(args.text), args.kind or 'note')}")
            return 0
        if action == "search":
            for r in mem.search(" ".join(args.text or []), 20):
                print(f"#{r['id']:<5} {r['kind']:<11} {r['text']}")
            return 0
        if action in ("remove", "pin", "unpin"):
            if not args.text or not args.text[0].lstrip("#").isdigit():
                return _err(f"usage: smooth-agent memory {action} <id>")
            mid = int(args.text[0].lstrip("#"))
            if action == "remove":
                if not mem.delete(mid):
                    return _err(f"no memory #{mid}")
                print(f"forgot #{mid}")
                return 0
            mem.update(mid, pinned=action == "pin")
            print(f"{action}ned #{mid}")
            return 0
    except ToolError as e:
        return _err(str(e))
    return _err(f"actions: list [--kind {'|'.join(KINDS)}], add <text>, search <words>, remove|pin|unpin <id>")


# ---------- cron / jobs / daemon ----------

def _tz(args) -> str | None:
    try:
        return Settings.from_config(load_config(args.config)).timezone or None
    except FileNotFoundError:
        return None


def _cmd_cron(args) -> int:
    from .schedule import parse
    from .scheduler import Crons, DaemonLock, fmt_time
    from .tools import ToolError
    st = _store()
    tz = _tz(args)
    crons = Crons(st, tz)
    action = args.action or "list"
    try:
        if action == "list":
            rows = crons.list()
            for r in rows:
                runs = f"{r['runs']}/{r['repeat']}" if r["repeat"] else str(r["runs"])
                print(f"{r['name']:<20} {'on ' if r['enabled'] else 'off'} {r['schedule']:<20} next {fmt_time(r['next_run'], tz):<16} "
                      f"runs {runs:<5} {r['last_status'] or '-':<5} {(r['prompt'] or '$ ' + (r.get('script') or ''))[:50]}")
            if not rows:
                print('no cron jobs. add one:\n  smooth-agent cron add morning "daily 07:00" "Summarise the top tech news for me"')
            elif not DaemonLock.running(ensure_home()):
                print("\nnote: the scheduler is not running — start it with `smooth-agent daemon` (or install it as a service).")
            return 0
        if action == "add":
            if len(args.rest) < 3 and not (len(args.rest) == 2 and args.script):
                return _err('usage: smooth-agent cron add <name> "<schedule>" "<prompt>" [--script CMD] [--repeat N] [--skill NAME]...')
            name, schedule, prompt = args.rest[0], args.rest[1], " ".join(args.rest[2:])
            r = crons.create(name, schedule, prompt, repeat=args.repeat or 0, skills=args.skill or [],
                             deliver=args.deliver or "", model=args.model or "", script=args.script or "")
            print(f"added '{name}' — {parse(schedule).describe()}, next run {fmt_time(r['next_run'], tz)}")
            if not DaemonLock.running(ensure_home()):
                print("start the scheduler with: smooth-agent daemon")
            return 0
        if not args.rest:
            return _err(f"usage: smooth-agent cron {action} <name>")
        if action in ("pause", "resume"):
            r = crons.update(args.rest[0], enabled=action == "resume")
            print(f"{r['name']}: {'on, next ' + fmt_time(r['next_run'], tz) if r['enabled'] else 'paused'}")
            return 0
        if action == "remove":
            if not crons.remove(args.rest[0]):
                return _err(f"no cron job '{args.rest[0]}'")
            print(f"removed {args.rest[0]}")
            return 0
        if action == "run":
            # run it right now in this terminal (handy to test a prompt or a script)
            from .scheduler import Jobs, Scheduler
            cr = crons.get(args.rest[0])
            out: list[str] = []
            sched = Scheduler(st, ensure_home(), lambda prompt, c: _run_job(args, prompt, c), tz=tz,
                              deliver=[lambda c, text, ok: out.append(text)], log=lambda m: print(m, file=sys.stderr))
            Jobs(st).enqueue("cron", {"name": cr["name"]}, priority=10)
            sched.run_one()
            print(out[0] if out else "(nothing to report)")
            return 0
    except ToolError as e:
        return _err(str(e))
    return _err("actions: list, add, pause, resume, remove, run")


def _run_job(args, prompt: str, cr: dict) -> str:
    """One scheduled run: a fresh agent, its own session, no human to ask."""
    from .runtime import build_runtime, new_session_id
    confirm = True
    try:
        confirm = Settings.from_config(load_config(args.config)).confirm
    except FileNotFoundError:
        pass
    approver = (lambda _n, _a: True) if not confirm else (lambda _n, _a: False)
    rt = build_runtime(args.config, approver=approver, ask=None, channel="cron",
                       session_id=new_session_id(f"cron-{cr['name']}-"), model=cr.get("model") or None)
    with rt:
        rt.agent.system += ("\n\nThis is a scheduled run with no one watching: do the task completely on your own, "
                            "then reply with the result the owner should read.")
        turn = rt.agent.run(prompt)
        return turn.text + (f"\n\n[stopped: {turn.stopped}]" if turn.stopped else "")


def _cmd_jobs(args) -> int:
    from .scheduler import Jobs
    for j in Jobs(_store()).list(args.state, args.limit):
        payload = json.loads(j["payload"] or "{}")
        err = f" — {j['last_error'].splitlines()[0][:80]}" if j["last_error"] else ""
        print(f"#{j['id']:<5} {j['state']:<8} {j['kind']}:{payload.get('name', '')}  try {j['attempts']}/{j['max_attempts']}  {_when(j['updated'])}{err}")
    return 0


def _cmd_daemon(args) -> int:
    import signal
    import time as _t
    from .scheduler import DaemonLock, Scheduler
    home = ensure_home()
    load_dotenv(home / ".env")
    lock = DaemonLock(home)
    if not lock.acquire():
        return _err(f"a daemon is already running for {home}")
    log = lambda m: print(f"{_when(_t.time())} {m}", flush=True)  # noqa: E731

    # chat channels (optional): one shared runtime, one agent session per chat
    hub = rt = None
    deliver = []
    if not getattr(args, "no_channels", False):
        hub, rt = _start_channels(args, home, log)
        if hub:
            deliver = [hub.deliver_hook()]

    sched = Scheduler(_store(), home, lambda prompt, cr: _run_job(args, prompt, cr), tz=_tz(args),
                      deliver=deliver, log=log)
    signal.signal(signal.SIGTERM, lambda *_: sched.stop())
    signal.signal(signal.SIGINT, lambda *_: sched.stop())
    where = "channels + schedules" if hub else "schedules"
    print(f"smooth-agent daemon {__version__} — home {home}; running {where}, checking every {args.interval:g}s", flush=True)
    try:
        sched.loop(interval=args.interval, once=args.once)
    finally:
        if hub:
            hub.stop()
        if rt:
            rt.close()
    print("daemon stopped", flush=True)
    return 0


def _start_channels(args, home, log):
    """Build the channel hub on a shared runtime. Returns (hub, runtime) or (None, None)."""
    from .channels import ChannelHub, add_send_file_tool, add_send_message_tool, build_channels
    from .config import load_channels
    from .runtime import build_chat_agent, build_runtime
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        return None, None
    chcfg = load_channels(cfg)
    if not chcfg.specs:
        return None, None

    hub_ref: dict = {}

    confirm = Settings.from_config(cfg).confirm

    def approve(name: str, a: dict) -> bool:
        if not confirm:  # [tools] confirm = false: the owner chose to run everything without asking
            return True
        h = hub_ref.get("hub")
        return bool(h and h.approve(name, a))

    try:
        rt = build_runtime(args.config, approver=approve, ask=None, channel="channel")
    except RouterError as e:
        log(f"channels: not started ({e})")
        return None, None
    for w in rt.warnings:
        log(f"  ! {w}")
    if rt.store is not None and not rt.store.session_messages(rt.agent.session_id):
        # the shared runtime's own agent is only a template for the per-chat agents: drop its empty session
        rt.store.delete_session(rt.agent.session_id)
    channels = build_channels(chcfg.specs, log=log)
    if not channels:
        rt.close()
        return None, None
    hub = ChannelHub(channels, lambda sid, ch, chat: build_chat_agent(rt, sid, ch, chat), log=log,
                     webhook_host=chcfg.webhook_host, webhook_port=chcfg.webhook_port,
                     workspace_root=rt.workspace.root)
    hub_ref["hub"] = hub
    _give_voice(hub, rt, log)
    hub.start()
    if not hub.channels:  # every channel failed to start: run schedules only
        hub.stop()
        rt.close()
        log("channels: none started; running scheduled jobs only")
        return None, None
    add_send_message_tool(rt.registry, hub)  # after start, so it lists only the live channels
    add_send_file_tool(rt.registry, hub, rt.workspace)
    return hub, rt


def _give_voice(hub, rt, log) -> None:
    """Speech to text for voice notes ([stt]) and voice replies ([tts]) on the channel hub."""
    from .voice import VoiceError, ffmpeg_ok, speak, speakable, to_voice_ogg, transcribe
    vs = rt.voice
    if vs is None:
        return
    if vs.can_listen:
        hub.listen = lambda path: transcribe(rt.router, path, vs)
    if vs.can_speak and ffmpeg_ok():
        def _speak(text: str, folder):
            import time as _t
            words = speakable(text)
            if not words:
                raise VoiceError("nothing to say")
            raw, ext = speak(rt.router, words, vs)
            src = folder / f"reply-{_t.strftime('%Y%m%d-%H%M%S')}{ext}"
            src.write_bytes(raw)
            return to_voice_ogg(src, src.with_suffix(".voice.ogg"))
        hub.speak = _speak
    elif vs.can_speak:
        log("voice replies need ffmpeg (to make Telegram voice notes); install ffmpeg")
    log(f"voice: listening {'on' if hub.listen else 'off ([stt])'}, speaking {'on' if hub.speak else 'off ([tts])'}")


# ---------- channels ----------

def _cmd_channels(args) -> int:
    from .channels import build_channels
    from .config import load_channels
    load_dotenv(ensure_home() / ".env")
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        return _err(str(e))
    chcfg = load_channels(cfg)
    action = args.action or "list"
    if action == "list":
        if not chcfg.specs:
            print("no channels enabled. add one under [channels] in your config, e.g.:\n"
                  '  enabled = ["telegram"]\n  [channels.telegram]\n  token_env = "TELEGRAM_BOT_TOKEN"\n  owners = ["<your id>"]')
            return 0
        for s in chcfg.specs:
            tok = s.options.get("token") or s.options.get("webhook_url") or ""
            key = "set" if tok else "MISSING"
            print(f"{s.name:<10} owners={', '.join(s.owners) or 'NONE':<20} credential={key:<8} "
                  f"auto_approve={'yes' if s.auto_approve else 'no'}")
        print(f"\nwebhook server: {chcfg.webhook_host}:{chcfg.webhook_port}")
        print("start them with: smooth-agent daemon")
        return 0
    if action == "test":
        if not args.rest:
            return _err('usage: smooth-agent channels test <name> [chat_id] "<message>"')
        name = args.rest[0]
        spec = next((s for s in chcfg.specs if s.name == name), None)
        if not spec:
            return _err(f"channel '{name}' is not enabled in the config")
        rest = args.rest[1:]
        chat_id = rest[0] if len(rest) > 1 else (spec.owners[0] if spec.owners else "")
        text = " ".join(rest[1:]) if len(rest) > 1 else (rest[0] if rest else "Test message from Smooth Agent.")
        if not chat_id:
            return _err(f"no chat id: pass one or set owners for '{name}'")
        ch = build_channels([spec])[0]
        try:
            ch.send(str(chat_id), text)
        except Exception as e:
            return _err(f"send failed: {e}")
        print(f"sent to {name} ({chat_id})")
        return 0
    return _err("actions: list, test")


# ---------- service ----------

LAUNCHD_LABEL = "family.smooth.agent"


def service_files(home: Path, python: str | None = None, label: str = LAUNCHD_LABEL) -> dict[str, str]:
    """The service definitions for this install: a systemd user unit and a launchd plist.
    Both run `<this python> -m smooth_agent.cli daemon`, so a same-named script elsewhere
    on PATH can never be started by mistake."""
    from xml.sax.saxutils import escape
    py = python or sys.executable
    unit = (
        "[Unit]\n"
        "Description=Smooth Agent (chat channels + scheduled jobs)\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={py} -m smooth_agent.cli daemon\n"
        f"Environment=SMOOTH_AGENT_HOME={home}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    log = home / "daemon.log"
    args = "".join(f"\n    <string>{escape(a)}</string>" for a in (py, "-m", "smooth_agent.cli", "daemon"))
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{escape(label)}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>{args}\n  </array>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n"
        f"    <key>SMOOTH_AGENT_HOME</key>\n    <string>{escape(str(home))}</string>\n"
        "    <key>PYTHONUNBUFFERED</key>\n    <string>1</string>\n  </dict>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        f"  <key>StandardOutPath</key>\n  <string>{escape(str(log))}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{escape(str(log))}</string>\n"
        "</dict>\n</plist>\n"
    )
    return {"systemd": unit, "launchd": plist}


def _cmd_service(args) -> int:
    import re
    home = ensure_home()
    name = args.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        return _err("--name: letters, digits, '.', '_' or '-'")
    # one service per home: a second install on the same machine just needs another --name
    label = LAUNCHD_LABEL if name == "smooth-agent" else f"family.smooth.{name}"
    files = service_files(home, label=label)
    kind = args.kind or ("launchd" if sys.platform == "darwin" else "systemd")
    if kind == "launchd":
        dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        enable = (f"  launchctl bootstrap gui/$(id -u) {dest}\n"
                  f"  launchctl kickstart -k gui/$(id -u)/{label}   # restart\n"
                  f"  tail -f {home / 'daemon.log'}   # logs")
    else:
        dest = Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
        enable = ("  systemctl --user daemon-reload\n"
                  f"  systemctl --user enable --now {name}\n"
                  "  loginctl enable-linger $USER   # keep running after you log out (servers)\n"
                  f"  journalctl --user -u {name} -f   # logs")
    if not args.install:
        print(files[kind])
        print(f"# install: smooth-agent service --install{' --kind ' + kind if args.kind else ''}  (writes {dest})")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(files[kind], encoding="utf-8")
    print(f"wrote {dest}\nstart it with:\n{enable}")
    return 0



# ---------- parser ----------

def _add_agent_args(sp) -> None:
    sp.add_argument("-p", "--provider", help="provider name (default from config)")
    sp.add_argument("-w", "--workspace", help="workspace directory the agent works in (default ~/.smooth-agent/workspace)")
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
    sp.add_argument("--channels", metavar="LIST", help="enable chat channels without asking, e.g. telegram,slack")
    sp.add_argument("--no-channels", action="store_true", help="skip the chat-channel step")
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
    sp.add_argument("--resume", nargs="?", const="last", help="continue a saved session (id, default: the last one)")
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("sessions", help="saved conversations: list / show / search / delete")
    sp.add_argument("action", nargs="?", choices=["list", "show", "search", "delete"], default="list")
    sp.add_argument("arg", nargs="?")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=_cmd_sessions)

    sp = sub.add_parser("memory", help="long-term memory: list / add / search / remove / pin / unpin")
    sp.add_argument("action", nargs="?", choices=["list", "add", "search", "remove", "pin", "unpin"], default="list")
    sp.add_argument("text", nargs="*")
    sp.add_argument("--kind", help="user, preference, decision, finding, blocker, note")
    sp.set_defaults(func=_cmd_memory)

    sp = sub.add_parser("cron", help="scheduled agent runs: list / add / pause / resume / remove / run")
    sp.add_argument("action", nargs="?", choices=["list", "add", "pause", "resume", "remove", "run"], default="list")
    sp.add_argument("rest", nargs="*", help='add: <name> "<schedule>" "<prompt>"; others: <name>')
    sp.add_argument("--repeat", type=int, help="stop after N runs")
    sp.add_argument("--skill", action="append", help="skill to load before the run (repeatable)")
    sp.add_argument("--deliver", help="where to send the result (e.g. telegram)")
    sp.add_argument("--model", help="model for this job (default: the provider's)")
    sp.add_argument("--script", help="shell command run first; empty output = nothing to report (watchers)")
    sp.set_defaults(func=_cmd_cron)

    sp = sub.add_parser("jobs", help="the job queue (scheduled runs and their results)")
    sp.add_argument("--state", choices=["queued", "running", "done", "failed"])
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=_cmd_jobs)

    sp = sub.add_parser("daemon", help="run chat channels + scheduled jobs (keep it running, or install as a service)")
    sp.add_argument("--interval", type=float, default=15.0, help="seconds between schedule checks")
    sp.add_argument("--once", action="store_true", help="check once, run what is due, then exit")
    sp.add_argument("--no-channels", action="store_true", help="run only scheduled jobs, not chat channels")
    sp.set_defaults(func=_cmd_daemon)

    sp = sub.add_parser("channels", help="chat channels: list configured ones / send a test message")
    sp.add_argument("action", nargs="?", choices=["list", "test"], default="list")
    sp.add_argument("rest", nargs="*", help='test: <name> [chat_id] "<message>"')
    sp.set_defaults(func=_cmd_channels)

    sp = sub.add_parser("service", help="print or install a background service for the daemon")
    sp.add_argument("--install", action="store_true", help="write it (systemd user unit / launchd agent)")
    sp.add_argument("--kind", choices=["systemd", "launchd"], help="default: launchd on macOS, systemd elsewhere")
    sp.add_argument("--name", default="smooth-agent", help="service name (default smooth-agent; use another for a 2nd install)")
    sp.set_defaults(func=_cmd_service)

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
