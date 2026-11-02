import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent import tools as tools_mod
from smooth_agent.agent import Agent
from smooth_agent.guard import redact, scrubbed_env
from smooth_agent.routers import Provider, Router, _from_anthropic_message, _to_anthropic_body
from smooth_agent.skills import SkillLibrary, add_skill_tools, parse_front_matter
from smooth_agent.tools import Registry, Workspace, default_registry, html_to_text, todo_tool


def serve(handler_fn):
    """HTTP server whose response is computed from the request JSON: handler_fn(path, req) -> (status, dict)."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            status, body = handler_fn(self.path, req)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def ws():
    return Workspace(tempfile.mkdtemp())


class GuardTests(unittest.TestCase):
    def test_redacts_keys_keeps_addresses(self):
        text = ("key sk-ant-api03-abcdefghijklmnop and ghp_" + "a" * 36 + " addr 0x" + "1" * 40
                + " pk 0x" + "f" * 64 + " OPENAI_API_KEY=supersecretvalue Authorization: Bearer abcdefghijklmnop")
        out = redact(text)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertNotIn("a" * 36, out)
        self.assertIn("0x" + "1" * 40, out)  # public address untouched
        self.assertNotIn("f" * 64, out)
        self.assertNotIn("supersecretvalue", out)

    def test_scrubbed_env(self):
        env = scrubbed_env(("MY_ROUTER",), base={"PATH": "/bin", "OPENAI_API_KEY": "x", "MY_ROUTER": "y", "GH_TOKEN": "z", "HOME": "/h"})
        self.assertEqual(set(env) - {"SMOOTH_AGENT"}, {"PATH", "HOME"})


class AnthropicWireTests(unittest.TestCase):
    def test_body_translation_with_tools(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "checking", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "now", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "2026"},
            {"role": "tool", "tool_call_id": "t2", "content": "data"},
        ]
        tools = [{"type": "function", "function": {"name": "now", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
        b = _to_anthropic_body(msgs, "claude", 100, False, {"tools": tools, "tool_choice": "auto"})
        self.assertEqual(b["system"], "sys")
        self.assertEqual(b["tools"][0]["input_schema"], {"type": "object", "properties": {}})
        self.assertEqual(b["tool_choice"], {"type": "auto"})
        self.assertEqual([m["role"] for m in b["messages"]], ["user", "assistant", "user"])
        uses = [x for x in b["messages"][1]["content"] if x["type"] == "tool_use"]
        self.assertEqual(uses[1]["input"], {"path": "a"})
        results = b["messages"][2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])  # merged into one user turn

    def test_response_parsing(self):
        text, calls, msg, finish = _from_anthropic_message({"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "ok"}, {"type": "tool_use", "id": "u1", "name": "now", "input": {"a": 1}}]})
        self.assertEqual((text, finish), ("ok", "tool_calls"))
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"a": 1})
        self.assertEqual(msg["tool_calls"][0]["id"], "u1")

    def test_agent_loop_over_anthropic_wire(self):
        def h(path, req):
            has_result = any(isinstance(m.get("content"), list) and any(c.get("type") == "tool_result" for c in m["content"])
                             for m in req["messages"])
            if not has_result:
                return 200, {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "u1", "name": "now", "input": {}}],
                             "usage": {"input_tokens": 5, "output_tokens": 2}}
            return 200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "it is now"}], "usage": {"input_tokens": 9, "output_tokens": 3}}
        srv, base = serve(h)
        try:
            p = Provider.from_config("a", {"provider": "anthropic", "model": "claude", "base_url": base, "key_env": "K"}, env={"K": "x"})
            turn = Agent(Router([p]), default_registry(ws(), allow_http=False)).run("time?")
            self.assertEqual(turn.text, "it is now")
            self.assertEqual([c["name"] for c in turn.tool_calls], ["now"])
            self.assertEqual(turn.usage["total_tokens"], 19)
        finally:
            srv.shutdown()


class ToolTests(unittest.TestCase):
    def test_terminal_needs_approval(self):
        w = ws()
        reg = default_registry(w, allow_http=False, terminal=True)
        self.assertIn("denied", reg.run("terminal", {"command": "echo hi"}))
        reg.approver = lambda n, a: True
        self.assertIn("hi", reg.run("terminal", {"command": "echo hi"}))
        self.assertIn("[exit 3]", reg.run("terminal", {"command": "exit 3"}))

    def test_terminal_env_has_no_keys_and_times_out(self):
        reg = default_registry(ws(), allow_http=False, terminal=True, approver=lambda n, a: True,
                               secret_env_names=("ROUTER_X",))
        with mock.patch.dict("os.environ", {"FAKE_API_KEY": "leak1", "ROUTER_X": "leak2"}):
            out = reg.run("terminal", {"command": "env"})
        self.assertNotIn("leak1", out)
        self.assertNotIn("leak2", out)
        self.assertIn("timed out", reg.run("terminal", {"command": "sleep 5", "timeout": 1}))
        self.assertIn("outside the workspace", reg.run("terminal", {"command": "ls", "workdir": "../.."}))

    def test_execute_code(self):
        reg = default_registry(ws(), allow_http=False, terminal=True, approver=lambda n, a: True)
        out = reg.run("execute_code", {"code": "print(6*7)"})
        self.assertIn("[exit 0]", out)
        self.assertIn("42", out)

    def test_no_terminal_by_default(self):
        reg = default_registry(ws(), allow_http=False)
        self.assertNotIn("terminal", reg.names())
        self.assertNotIn("execute_code", reg.names())

    def test_patch_read_search(self):
        w = ws()
        reg = default_registry(w, allow_http=False)
        reg.run("write_file", {"path": "src/a.py", "content": "x = 1\ny = 2\nx = 1\n"})
        self.assertIn("matches 2", reg.run("patch", {"mode": "replace", "path": "src/a.py", "old_string": "x = 1", "new_string": "x = 9"}))
        self.assertIn("replaced 2", reg.run("patch", {"mode": "replace", "path": "src/a.py", "old_string": "x = 1", "new_string": "x = 9", "replace_all": True}))
        self.assertIn("not found", reg.run("patch", {"mode": "replace", "path": "src/a.py", "old_string": "zzz", "new_string": ""}))
        out = reg.run("read_file", {"path": "src/a.py", "offset": 2, "limit": 1})
        self.assertEqual(out.splitlines()[0].strip(), "2\ty = 2")
        self.assertIn("1 more lines", out)
        self.assertIn("src/a.py:1:x = 9", reg.run("search_files", {"pattern": r"x = \d"}))
        self.assertIn("2\tsrc/a.py", reg.run("search_files", {"pattern": "x =", "output_mode": "count"}))
        self.assertEqual(reg.run("search_files", {"pattern": "*.py", "target": "files"}), "src/a.py")
        if shutil.which("git") or shutil.which("patch"):
            diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n x = 9\n-y = 2\n+y = 3\n x = 9\n"
            self.assertIn("applied", reg.run("patch", {"mode": "patch", "patch": diff}))
            self.assertIn("y = 3", (w.root / "src/a.py").read_text())
        bad = "--- a/../../etc/x\n+++ b/../../etc/x\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertIn("outside the workspace", reg.run("patch", {"mode": "patch", "patch": bad}))

    def test_todo(self):
        t = todo_tool()
        r = Registry()
        r.add(t)
        out = r.run("todo", {"todos": [{"id": "1", "content": "plan", "status": "completed"}, {"id": "2", "content": "build", "status": "pending"}]})
        self.assertIn("1/2 done", out)
        out = r.run("todo", {"todos": [{"id": "2", "content": "build", "status": "in_progress"}], "merge": True})
        self.assertIn("[~] 2. build", out)
        r2 = Registry()
        r2.add(todo_tool())
        self.assertEqual(r2.run("todo", {}), "(no todos)")  # separate lists

    def test_html_and_duckduckgo_parse(self):
        self.assertEqual(html_to_text("<html><script>x()</script><p>Hello&amp;bye</p><p>two</p></html>"), "Hello&bye\n\ntwo")
        page = ('<div><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=1">Ex <b>A</b></a>'
                '<a class="result__snippet" href="x">Snippet one</a></div>')
        with mock.patch.object(tools_mod, "_post_form", return_value=page):
            res = tools_mod.web_search("q", 3, backend="duckduckgo")
        self.assertEqual(res, [{"title": "Ex A", "url": "https://example.com/a", "snippet": "Snippet one"}])


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        core = self.root / "bundled" / "core" / "dev"
        opt = self.root / "bundled" / "optional" / "x"
        for d, name, extra in ((core / "alpha", "alpha", "platforms: [linux, macos]"),
                               (core / "maconly", "maconly", '"os": ["darwin"]'),
                               (opt / "beta", "beta", "")):
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: |\n  does {name}\n  well\n{extra}\n---\n# {name}\nbody\n")
        (core / "alpha" / "references").mkdir()
        (core / "alpha" / "references" / "r.md").write_text("ref")
        self.user = self.root / "user"

    def lib(self, **kw):
        return SkillLibrary(user_dir=self.user, bundled=self.root / "bundled", **kw)

    def test_front_matter(self):
        meta, body = parse_front_matter('---\nname: a\ndescription: >\n  one\n  two\nplatforms: [linux]\nmetadata: { "x": { "requires": { "bins": ["op", "jq"] } } }\n---\nBODY')
        self.assertEqual(meta["description"], "one two")
        self.assertEqual(meta["platforms"], ["linux"])
        self.assertEqual(meta["requires_bins"], ["op", "jq"])
        self.assertEqual(body, "BODY")

    def test_os_filter_optional_gating_and_view(self):
        lib = self.lib()
        names = {s.name for s in lib.available()}
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)  # optional, not enabled
        self.assertEqual("maconly" in names, __import__("sys").platform == "darwin")
        self.assertIn("beta", {s.name for s in self.lib(enabled={"beta"}).available()})
        v = lib.view("alpha")
        self.assertIn("does alpha", v)
        self.assertIn("references/r.md", v)
        self.assertEqual(lib.view("alpha", "references/r.md"), "ref")
        with self.assertRaises(Exception):
            lib.view("alpha", "../../../etc/passwd")

    def test_manage_and_override(self):
        lib = self.lib()
        reg = Registry()
        add_skill_tools(reg, lib)
        out = reg.run("skill_manage", {"action": "create", "name": "mine", "content": "---\nname: mine\ndescription: my flow\n---\nsteps"})
        self.assertIn("created", out)
        self.assertIn("mine", reg.run("skills_list", {}))
        self.assertIn("patched", reg.run("skill_manage", {"action": "patch", "name": "mine", "old_string": "steps", "new_string": "better steps"}))
        self.assertIn("better steps", reg.run("skill_view", {"name": "mine"}))
        # editing a bundled skill makes a user copy that overrides it; the bundled file is untouched
        self.assertIn("patched", reg.run("skill_manage", {"action": "patch", "name": "alpha", "old_string": "body", "new_string": "mine now"}))
        self.assertEqual(lib.get("alpha").source, "user")
        self.assertIn("body", (self.root / "bundled/core/dev/alpha/SKILL.md").read_text())
        self.assertIn("error", reg.run("skill_manage", {"action": "create", "name": "../evil", "content": "x"}))
        self.assertIn("deleted", reg.run("skill_manage", {"action": "delete", "name": "mine"}))

    def test_bundled_library_loads(self):
        lib = SkillLibrary(user_dir=self.user)
        self.assertGreater(len(lib.skills), 80)
        self.assertLess(len(lib.index_text()), 13_000)
        self.assertNotIn("godmode", lib.skills)


class DelegateTests(unittest.TestCase):
    def test_parallel_subagents(self):
        def h(path, req):
            names = [t["function"]["name"] for t in req.get("tools") or []]
            last = req["messages"][-1]
            if "delegate_task" in names:  # the main agent
                if last["role"] == "tool":
                    return 200, {"choices": [{"message": {"role": "assistant", "content": "combined: " + last["content"].replace("\n", " ")}}]}
                return 200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "d1", "type": "function", "function": {"name": "delegate_task", "arguments": json.dumps(
                        {"tasks": [{"goal": "TASK-A"}, {"goal": "TASK-B"}], "context": "shared"})}}]}}]}
            # a sub-agent: no delegate tool, answers its own goal
            goal = "A" if "TASK-A" in last["content"] else "B"
            assert "shared" in last["content"]
            return 200, {"choices": [{"message": {"role": "assistant", "content": f"done-{goal}"}}]}
        srv, base = serve(h)
        try:
            p = Provider.from_config("p", {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K"}, env={"K": "x"})
            events = []
            a = Agent(Router([p]), default_registry(ws(), allow_http=False), delegate=True, on_event=lambda e, d: events.append(e))
            turn = a.run("do both")
            self.assertIn("done-A", turn.text)
            self.assertIn("done-B", turn.text)
            self.assertIn("Task 1", turn.text)
            self.assertIn("sub:text", events)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
