"""MCP client: use the tools of any Model Context Protocol server.

Servers come from `[mcp.servers.<name>]` in smooth-agent.toml and from
$SMOOTH_AGENT_HOME/mcp.json (what `smooth-agent mcp add` writes):

    [mcp.servers.github]                       # local server over stdio
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-github"]
    env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }  # ${VAR} is read from the environment

    [mcp.servers.docs]                         # remote server over Streamable HTTP
    url = "https://example.com/mcp"
    headers = { Authorization = "Bearer ${DOCS_TOKEN}" }

Each server tool becomes an agent tool named mcp_<server>_<tool>. Only the stdlib
is used: JSON-RPC 2.0 over newline-delimited stdio, or HTTP POST with JSON or
SSE replies and the Mcp-Session-Id header.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .tools import Registry, Tool, ToolError

PROTOCOL_VERSION = "2025-06-18"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, env: dict[str, str]) -> str:
    return _ENV_REF.sub(lambda m: env.get(m.group(1), ""), str(value))


@dataclass
class ServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    enabled: bool = True
    include: list[str] = field(default_factory=list)  # only these tools (empty = all)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "ServerConfig":
        return cls(name=name, command=str(d.get("command", "")), args=[str(a) for a in d.get("args", [])],
                   env={str(k): str(v) for k, v in (d.get("env") or {}).items()}, cwd=str(d.get("cwd", "")),
                   url=str(d.get("url", "")), headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
                   timeout=float(d.get("timeout", 60)), enabled=bool(d.get("enabled", True)),
                   include=[str(x) for x in d.get("include", [])], exclude=[str(x) for x in d.get("exclude", [])])

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k in ("command", "args", "env", "cwd", "url", "headers", "include", "exclude"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.timeout != 60.0:
            d["timeout"] = self.timeout
        if not self.enabled:
            d["enabled"] = False
        return d


class MCPError(Exception):
    pass


class _Stdio:
    def __init__(self, cfg: ServerConfig, env: dict[str, str]):
        child_env = {**env, **{k: _expand(v, env) for k, v in cfg.env.items()}}
        try:
            self.proc = subprocess.Popen([cfg.command, *[_expand(a, env) for a in cfg.args]], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env,
                                         cwd=cfg.cwd or None, start_new_session=True)
        except OSError as e:
            raise MCPError(f"could not start '{cfg.command}': {e}") from None
        self.timeout = cfg.timeout
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                waiter = self._pending.pop(msg["id"], None)
                if waiter:
                    waiter[1].update(msg)
                    waiter[0].set()
            elif "id" in msg and "method" in msg:
                self._answer_server_request(msg)
        for ev, box in list(self._pending.values()):  # the server exited
            box["error"] = {"message": "server exited. " + " | ".join(self.stderr_tail[-3:])}
            ev.set()

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.decode("utf-8", "replace").strip()[:300]])[-20:]

    def _answer_server_request(self, msg: dict) -> None:
        if msg["method"] == "ping":
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif msg["method"] == "roots/list":
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": []}})
        else:
            self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported by this client"}})

    def _write(self, msg: dict) -> None:
        with self._lock:
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise MCPError(f"server pipe closed: {e}") from None

    def request(self, rid: int, method: str, params: dict | None, timeout: float | None = None) -> dict:
        ev, box = threading.Event(), {}
        self._pending[rid] = (ev, box)
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        if not ev.wait(timeout or self.timeout):
            self._pending.pop(rid, None)
            raise MCPError(f"{method} timed out")
        if "error" in box:
            raise MCPError(box["error"].get("message", "error"))
        return box.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class _HTTP:
    def __init__(self, cfg: ServerConfig, env: dict[str, str]):
        self.url = cfg.url
        self.timeout = cfg.timeout
        self.headers = {k: _expand(v, env) for k, v in cfg.headers.items()}
        self.session_id: str | None = None
        self.protocol: str | None = None

    def _post(self, msg: dict, timeout: float | None = None) -> dict | None:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.protocol:
            headers["mcp-protocol-version"] = self.protocol
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)  # noqa: S310
        except urllib.error.HTTPError as e:
            raise MCPError(f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MCPError(f"connect: {e}") from None
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if "id" not in msg:
            resp.read()
            return None
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if ev.get("id") == msg["id"] and ("result" in ev or "error" in ev):
                    return ev
            raise MCPError("stream ended without a reply")
        body = resp.read()
        return json.loads(body) if body.strip() else {}

    def request(self, rid: int, method: str, params: dict | None, timeout: float | None = None) -> dict:
        reply = self._post({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}, timeout)
        if reply and "error" in reply:
            raise MCPError(reply["error"].get("message", "error"))
        return (reply or {}).get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def close(self) -> None:
        pass


class MCPClient:
    def __init__(self, cfg: ServerConfig, env: dict[str, str] | None = None):
        self.cfg = cfg
        env = dict(os.environ if env is None else env)
        if cfg.url:
            self.t: _Stdio | _HTTP = _HTTP(cfg, env)
        elif cfg.command:
            self.t = _Stdio(cfg, env)
        else:
            raise MCPError(f"server '{cfg.name}' needs a command or a url")
        self._id = 0
        self._id_lock = threading.Lock()
        self.server_info: dict = {}
        self.tools: list[dict] = []

    def _next(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def start(self) -> "MCPClient":
        res = self.t.request(self._next(), "initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "smooth-agent", "version": __version__}})
        self.server_info = res.get("serverInfo") or {}
        if isinstance(self.t, _HTTP):
            self.t.protocol = res.get("protocolVersion") or PROTOCOL_VERSION
        self.t.notify("notifications/initialized")
        tools, cursor = [], None
        for _ in range(50):
            res = self.t.request(self._next(), "tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        inc, exc = set(self.cfg.include), set(self.cfg.exclude)
        self.tools = [t for t in tools if (not inc or t.get("name") in inc) and t.get("name") not in exc]
        return self

    def call(self, name: str, arguments: dict) -> str:
        res = self.t.request(self._next(), "tools/call", {"name": name, "arguments": arguments or {}}, timeout=max(self.cfg.timeout, 120))
        parts = []
        for c in res.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") in ("image", "audio"):
                parts.append(f"[{c['type']}: {c.get('mimeType', '?')}, {len(c.get('data', '')) * 3 // 4} bytes]")
            elif c.get("type") == "resource":
                r = c.get("resource") or {}
                parts.append(r.get("text") or f"[resource {r.get('uri', '')}]")
            elif c.get("type") == "resource_link":
                parts.append(f"[resource {c.get('uri', '')}] {c.get('name', '')}")
        if not parts and res.get("structuredContent") is not None:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        text = "\n".join(p for p in parts if p) or "(no content)"
        return f"error: {text}" if res.get("isError") else text

    def close(self) -> None:
        self.t.close()


def tool_name(server: str, tool: str) -> str:
    raw = f"mcp_{server}_{tool}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return clean[:64]


# ---------- server list (config + mcp.json) ----------

def json_path(home: Path) -> Path:
    return home / "mcp.json"


def load_servers(cfg: dict[str, Any], home: Path) -> dict[str, ServerConfig]:
    servers: dict[str, ServerConfig] = {}
    p = json_path(home)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for name, d in (data.get("mcpServers") or data.get("servers") or {}).items():
                servers[name] = ServerConfig.from_dict(name, d)
        except (json.JSONDecodeError, OSError):
            pass
    for name, d in ((cfg.get("mcp") or {}).get("servers") or {}).items():
        servers[name] = ServerConfig.from_dict(name, d)  # the toml wins over mcp.json
    return servers


def save_server(home: Path, sc: ServerConfig) -> Path:
    p = json_path(home)
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    data.setdefault("mcpServers", {})[sc.name] = sc.to_dict()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def remove_server(home: Path, name: str) -> bool:
    p = json_path(home)
    if not p.is_file():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    found = data.get("mcpServers", {}).pop(name, None) is not None
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return found


class MCPManager:
    """Connects every enabled server and registers its tools."""

    def __init__(self, servers: dict[str, ServerConfig]):
        self.servers = {n: s for n, s in servers.items() if s.enabled}
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}

    def connect(self, env: dict[str, str] | None = None) -> None:
        def one(name: str, sc: ServerConfig) -> None:
            try:
                self.clients[name] = MCPClient(sc, env).start()
            except (MCPError, OSError) as e:
                self.errors[name] = str(e)
        threads = [threading.Thread(target=one, args=(n, s), daemon=True) for n, s in self.servers.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    def register(self, reg: Registry, *, needs_approval: bool = False) -> int:
        n = 0
        for server, client in self.clients.items():
            for t in client.tools:
                name = t.get("name", "")
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                desc = f"[MCP {server}] {t.get('description') or t.get('title') or name}"[:1024]

                def handler(args, _c=client, _n=name):
                    try:
                        return _c.call(_n, args)
                    except MCPError as e:
                        raise ToolError(f"MCP {_c.cfg.name}: {e}") from None
                reg.add(Tool(name=tool_name(server, name), description=desc, parameters=schema, handler=handler,
                             needs_approval=needs_approval))
                n += 1
        return n

    def close(self) -> None:
        for c in self.clients.values():
            c.close()


__all__ = ["MCPClient", "MCPManager", "ServerConfig", "MCPError", "load_servers", "save_server", "remove_server", "tool_name"]
