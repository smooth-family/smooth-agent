import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent.agent import Agent
from smooth_agent.home import load_dotenv, parse_env, set_env_value
from smooth_agent.media import MediaSettings, add_media_tools, image_mime, load_image
from smooth_agent.mcp import MCPClient, MCPManager, ServerConfig, load_servers, remove_server, save_server, tool_name
from smooth_agent.processes import ProcessManager, add_process_tools
from smooth_agent.routers import Provider, Router
from smooth_agent.skills import SkillLibrary
from smooth_agent.tools import Registry, Workspace, default_registry

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "mcp_echo_server.py"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def serve(fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            n = int(self.headers.get("content-length", 0))
            body = self.rfile.read(n) if n else b""
            status, headers, data = fn(method, self.path, dict(self.headers), body)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self._go("POST")

        def do_GET(self):
            self._go("GET")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def ws():
    return Workspace(tempfile.mkdtemp())


class HomeTests(unittest.TestCase):
    def test_env_parse_and_write(self):
        self.assertEqual(parse_env('A=1\n# c\nexport B="two words"\nC=x # note\nbad line\n'), {"A": "1", "B": "two words", "C": "x"})
        d = Path(tempfile.mkdtemp())
        p = set_env_value("K1", "v1", d / ".env")
        set_env_value("K1", "v2 with space", p)
        set_env_value("K2", "x", p)
        self.assertEqual(parse_env(p.read_text()), {"K1": "v2 with space", "K2": "x"})
        self.assertEqual(oct(p.stat().st_mode)[-3:], "600")
        with mock.patch.dict(os.environ, {"K2": "kept"}, clear=False):
            loaded = load_dotenv(p)
            self.assertEqual(os.environ["K2"], "kept")  # existing env wins
            self.assertIn("K1", loaded)
        os.environ.pop("K1", None)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.w = ws()
        self.pm = ProcessManager(Path(tempfile.mkdtemp()))
        self.reg = default_registry(self.w, allow_http=False, terminal=True, approver=lambda n, a: True, processes=self.pm)
        add_process_tools(self.reg, self.pm)

    def tearDown(self):
        self.pm.shutdown()

    def test_background_poll_wait_notify(self):
        out = self.reg.run("terminal", {"command": "echo start; sleep 0.5; echo end", "background": True, "notify_on_complete": True})
        self.assertIn("started in the background as p1", out)
        self.assertIn("running", self.reg.run("process", {"action": "list"}))
        waited = self.reg.run("process", {"action": "wait", "session_id": "p1", "timeout": 10})
        self.assertIn("exited 0", waited)
        self.assertIn("end", waited)
        notes = self.pm.take_finished()
        self.assertEqual(len(notes), 1)
        self.assertIn("exit code 0", notes[0])
        self.assertEqual(self.pm.take_finished(), [])  # only once

    def test_stdin_and_kill(self):
        self.reg.run("terminal", {"command": "while read l; do echo got:$l; done", "background": True})
        self.reg.run("process", {"action": "submit", "session_id": "p1", "data": "hello"})
        time.sleep(0.4)
        self.assertIn("got:hello", self.reg.run("process", {"action": "poll", "session_id": "p1"}))
        self.reg.run("process", {"action": "kill", "session_id": "p1"})
        self.assertNotIn("running", self.reg.run("process", {"action": "list"}))
        self.assertIn("error", self.reg.run("process", {"action": "poll", "session_id": "nope"}))

    def test_agent_gets_exit_notice(self):
        calls = []

        def fn(method, path, headers, body):
            req = json.loads(body)
            calls.append(req)
            notice = any("automatic notice" in str(m.get("content")) for m in req["messages"])
            if len(calls) == 1:
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "t", "type": "function", "function": {
                    "name": "terminal", "arguments": json.dumps({"command": "echo built", "background": True, "notify_on_complete": True})}}]}
            elif not notice:
                time.sleep(0.3)
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": f"w{len(calls)}", "type": "function", "function": {"name": "now", "arguments": "{}"}}]}
            else:
                msg = {"role": "assistant", "content": "build finished"}
            return 200, {"content-type": "application/json"}, json.dumps({"choices": [{"message": msg}]}).encode()
        srv, base = serve(fn)
        try:
            p = Provider.from_config("p", {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K"}, env={"K": "x"})
            turn = Agent(Router([p]), self.reg, processes=self.pm, max_steps=8).run("build it")
            self.assertEqual(turn.text, "build finished")
        finally:
            srv.shutdown()


class MediaTests(unittest.TestCase):
    def test_load_image_sources(self):
        w = ws()
        (w.root / "a.png").write_bytes(PNG)
        self.assertEqual(load_image("a.png", w)[1], "image/png")
        self.assertEqual(load_image("data:image/png;base64," + base64.b64encode(PNG).decode(), w)[1], "image/png")
        self.assertEqual(image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        with self.assertRaises(Exception):
            load_image("../x.png", w)

    def test_vision_image_tts_tools(self):
        seen = {}

        def fn(method, path, headers, body):
            req = json.loads(body)
            seen[path] = req
            if path.endswith("/chat/completions"):
                part = req["messages"][0]["content"][1]
                assert part["image_url"]["url"].startswith("data:image/png;base64,")
                return 200, {"content-type": "application/json"}, json.dumps({"choices": [{"message": {"role": "assistant", "content": "a red dot"}}]}).encode()
            if path.endswith("/images/generations"):
                return 200, {"content-type": "application/json"}, json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
            if path.endswith("/audio/speech"):
                return 200, {"content-type": "audio/mpeg"}, b"ID3" + b"\x00" * 500
            return 404, {}, b""
        srv, base = serve(fn)
        try:
            w = ws()
            (w.root / "dot.png").write_bytes(PNG)
            p = Provider.from_config("main", {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K"}, env={"K": "x"})
            reg = Registry()
            add_media_tools(reg, Router([p]), w, MediaSettings(image_model="img-1", tts_model="tts-1"))
            self.assertEqual(reg.run("vision_analyze", {"image_url": "dot.png", "question": "what?"}), "a red dot")
            out = reg.run("image_generate", {"prompt": "a cat", "filename": "cat"})
            self.assertEqual(out, "saved images/cat.png")
            self.assertEqual((w.root / "images/cat.png").read_bytes(), PNG)
            self.assertEqual(seen["/images/generations"]["model"], "img-1")
            self.assertIn("audio/", reg.run("text_to_speech", {"text": "halo", "filename": "hi"}))
            self.assertTrue((w.root / "audio/hi.mp3").exists())
        finally:
            srv.shutdown()

    def test_image_and_tts_off_unless_configured(self):
        reg = Registry()
        add_media_tools(reg, Router([Provider.from_config("m", {"provider": "openai", "model": "x"}, env={"OPENAI_API_KEY": "k"})]), ws(), MediaSettings())
        self.assertEqual(reg.names(), ["vision_analyze"])


class MCPTests(unittest.TestCase):
    def test_stdio_roundtrip_and_registry(self):
        sc = ServerConfig(name="echo", command=sys.executable, args=[str(FIXTURE)])
        m = MCPManager({"echo": sc, "bad": ServerConfig(name="bad", command="/does/not/exist")})
        m.connect()
        try:
            reg = Registry()
            self.assertEqual(m.register(reg), 3)
            self.assertIn("bad", m.errors)
            self.assertEqual(reg.run("mcp_echo_echo", {"text": "hi"}), "echo: hi")
            self.assertEqual(json.loads(reg.run("mcp_echo_add", {"a": 1, "b": 2})), {"sum": 3})
            self.assertEqual(reg.run("mcp_echo_fail", {}), "error: boom")
        finally:
            m.close()

    def _http_server(self, sse: bool):
        state = {"session": None}

        def fn(method, path, headers, body):
            msg = json.loads(body)
            if "id" not in msg:
                return 202, {}, b""
            if msg["method"] == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "web", "version": "2"}}
                hdr = {"mcp-session-id": "s-1"}
            else:
                assert headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") == "s-1"
                if msg["method"] == "tools/list":
                    res = {"tools": [{"name": "ping", "inputSchema": {"type": "object", "properties": {}}}]}
                else:
                    res = {"content": [{"type": "text", "text": "pong"}]}
                hdr = {}
            reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res})
            if sse:
                return 200, {"content-type": "text/event-stream", **hdr}, f"event: message\ndata: {reply}\n\n".encode()
            return 200, {"content-type": "application/json", **hdr}, reply.encode()
        return serve(fn)

    def test_http_json_and_sse(self):
        for sse in (False, True):
            srv, base = self._http_server(sse)
            try:
                c = MCPClient(ServerConfig(name="web", url=base + "/mcp")).start()
                self.assertEqual([t["name"] for t in c.tools], ["ping"])
                self.assertEqual(c.call("ping", {}), "pong")
            finally:
                srv.shutdown()

    def test_server_store_and_names(self):
        home = Path(tempfile.mkdtemp())
        save_server(home, ServerConfig(name="a", command="x", args=["y"], env={"T": "${TOK}"}))
        servers = load_servers({"mcp": {"servers": {"b": {"url": "http://h/mcp"}}}}, home)
        self.assertEqual(set(servers), {"a", "b"})
        self.assertEqual(servers["a"].env, {"T": "${TOK}"})
        self.assertTrue(remove_server(home, "a"))
        self.assertEqual(tool_name("my server", "do.thing"), "mcp_my_server_do_thing")
        self.assertLessEqual(len(tool_name("s" * 40, "t" * 40)), 64)


@unittest.skipUnless(__import__("smooth_agent.browser", fromlist=["available"]).available(), "playwright not installed")
class BrowserTests(unittest.TestCase):
    def test_navigate_click_type(self):
        page = (b'<html><head><title>Shop</title></head><body><h1>Shop</h1><a href="/b">Next</a>'
                b'<input placeholder="Search"><button onclick="document.title=\'clicked\';console.log(\'hi\')">Go</button></body></html>')
        srv, base = serve(lambda m, p, h, b: (200, {"content-type": "text/html"}, page if p == "/" else b"<title>B</title><p>page b</p>"))
        from smooth_agent.browser import BrowserSession, BrowserSettings, add_browser_tools
        s = BrowserSession(BrowserSettings(enabled=True, profile_dir="none"))
        try:
            reg = Registry()
            w = ws()
            add_browser_tools(reg, s, w, describe_image=lambda png, q: f"saw {len(png) > 100}")
            out = reg.run("browser_navigate", {"url": base + "/"})
            self.assertIn('link "Next"', out)
            go = re.search(r'\[@(e\d+)\] button "Go"', out).group(1)
            self.assertIn("title: clicked", reg.run("browser_click", {"ref": "@" + go}))
            self.assertIn("[log] hi", reg.run("browser_console", {}))
            box = re.search(r'\[@(e\d+)\] textbox "Search"', out).group(1)
            self.assertIn("value='shoes'", reg.run("browser_type", {"ref": box, "text": "shoes"}))
            self.assertIn("saw True", reg.run("browser_vision", {"question": "?"}))
            self.assertIn("screenshots/x.png", reg.run("browser_screenshot", {"filename": "x"}))
            nxt = re.search(r'\[@(e\d+)\] link "Next"', out).group(1)
            self.assertIn("page b", reg.run("browser_click", {"ref": nxt}))
            self.assertIn("error", reg.run("browser_click", {"ref": "@e999"}))
            self.assertIn("error", reg.run("browser_navigate", {"url": "file:///etc/passwd"}))
        finally:
            s.close()
            srv.shutdown()


class SkillViewTests(unittest.TestCase):
    def test_skill_dir_is_filled_in(self):
        lib = SkillLibrary(user_dir=None)
        text = lib.view("hyperliquid") if "hyperliquid" in lib.skills else None
        lib2 = SkillLibrary(user_dir=None, optional=True)
        text = lib2.view("hyperliquid")
        self.assertNotIn("${SKILL_DIR}", text)
        self.assertIn(str(lib2.skills["hyperliquid"].path), text)


class CLITests(unittest.TestCase):
    def run_cli(self, *argv, home):
        env = {**os.environ, "SMOOTH_AGENT_HOME": str(home), "PYTHONPATH": str(REPO)}
        env.pop("SMOOTH_AGENT_CONFIG", None)
        return subprocess.run([sys.executable, "-m", "smooth_agent.cli", *argv], capture_output=True, text=True,
                              env=env, cwd=tempfile.mkdtemp(), timeout=120, stdin=subprocess.DEVNULL)

    def test_setup_doctor_mcp_skills(self):
        home = Path(tempfile.mkdtemp()) / "h"
        r = self.run_cli("setup", "--provider", "glm", "--model", "glm-4.6", "--key", "abc123", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('provider = "glm"', (home / "config.toml").read_text())
        self.assertIn("GLM_API_KEY=abc123", (home / ".env").read_text())
        r = self.run_cli("doctor", home=home)
        self.assertIn("ok  provider    main (glm, glm-4.6)", r.stdout)
        r = self.run_cli("mcp", "add", "echo", "--", sys.executable, str(FIXTURE), home=home)
        self.assertIn("3 tools", r.stdout, r.stderr)
        self.assertIn("echo", self.run_cli("mcp", "list", home=home).stdout)
        r = self.run_cli("skills", "install", "telephony", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((home / "skills" / "telephony" / "SKILL.md").exists())
        self.assertIn("github-pr-workflow", self.run_cli("skills", "search", "github", "pr", home=home).stdout)


def _skipped(p: Path) -> bool:
    """Build output and tool caches, not source."""
    return any(part in (".git", ".venv", "__pycache__", "build", "dist") or part.endswith(".egg-info") for part in p.parts)


class BrandTests(unittest.TestCase):
    """Upstream project names must not appear anywhere except the legally required notice."""

    def test_no_upstream_names(self):
        allowed = {REPO / "THIRD_PARTY_NOTICES.md"}
        pat = re.compile(r"hermes|openclaw|clawhub", re.I)
        hits = []
        for p in REPO.rglob("*"):
            if not p.is_file() or p in allowed or _skipped(p) or p.suffix in (".pyc", ".whl"):
                continue
            if p.resolve() == Path(__file__).resolve():
                continue
            if pat.search(str(p.relative_to(REPO))):
                hits.append(str(p.relative_to(REPO)))
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:100]}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
