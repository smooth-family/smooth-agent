import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from smooth_agent.agent import Agent
from smooth_agent.routers import Provider, Router
from smooth_agent.tools import Registry, ToolError, Tool, Workspace, default_registry, parse_arguments


def start_scripted_server(script):
    """script: list of response dicts (OpenAI chat-completion JSON) served in order."""
    state = {"i": 0, "seen": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            state["seen"].append(json.loads(self.rfile.read(n) or b"{}"))
            resp = script[min(state["i"], len(script) - 1)]
            state["i"] += 1
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", state


def tool_call_msg(name, args, call_id="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def answer_msg(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"total_tokens": 7}}


class ToolTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.ws = Workspace(self.tmp)

    def test_write_then_read_roundtrip(self):
        reg = default_registry(self.ws, allow_http=False)
        self.assertIn("wrote", reg.run("write_file", {"path": "a/b.txt", "content": "hello"}))
        self.assertEqual(reg.run("read_file", {"path": "a/b.txt"}), "hello")
        self.assertIn("b.txt", reg.run("list_dir", {"path": "a"}))

    def test_path_containment_blocks_escape(self):
        reg = default_registry(self.ws, allow_http=False)
        out = reg.run("read_file", {"path": "../../etc/passwd"})
        self.assertIn("outside the workspace", out)

    def test_unknown_tool_and_handler_error_are_returned_not_raised(self):
        reg = Registry()

        @reg.tool("boom", "raises", {"type": "object", "properties": {}})
        def _boom(_a):
            raise ToolError("nope")

        self.assertIn("unknown tool", reg.run("missing", {}))
        self.assertEqual(reg.run("boom", {}), "error: nope")

    def test_http_tool_absent_when_disabled(self):
        reg = default_registry(self.ws, allow_http=False)
        self.assertNotIn("http_get", reg.names())

    def test_parse_arguments(self):
        self.assertEqual(parse_arguments('{"a":1}'), {"a": 1})
        self.assertEqual(parse_arguments({"a": 1}), {"a": 1})
        self.assertEqual(parse_arguments(""), {})
        self.assertEqual(parse_arguments("not json"), {})

    def test_schema_shape(self):
        t = Tool("x", "d", {"type": "object", "properties": {}}, lambda a: "ok")
        s = t.schema()
        self.assertEqual(s["type"], "function")
        self.assertEqual(s["function"]["name"], "x")


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.ws = Workspace(tempfile.mkdtemp())

    def _agent(self, base, **kw):
        p = Provider.from_config("p", {"provider": "openai-compatible", "model": "m",
                                       "base_url": base, "key_env": "K"}, env={"K": "x"})
        reg = default_registry(self.ws, allow_http=False)
        return Agent(Router([p]), reg, **kw)

    def test_tool_call_then_answer(self):
        srv, base, state = start_scripted_server([
            tool_call_msg("write_file", {"path": "note.txt", "content": "hi"}),
            answer_msg("done, wrote note.txt"),
        ])
        try:
            events = []
            a = self._agent(base)
            a.on_event = lambda e, d: events.append((e, d))
            turn = a.run("write hi to note.txt")
            self.assertEqual(turn.text, "done, wrote note.txt")
            self.assertEqual(turn.steps, 2)
            self.assertEqual([t["name"] for t in turn.tool_calls], ["write_file"])
            self.assertEqual(self.ws.resolve("note.txt").read_text(), "hi")
            # the tool result was fed back as a role:tool message
            roles = [m["role"] for m in a.history]
            self.assertIn("tool", roles)
            self.assertIn(("tool", {"name": "write_file", "arguments": {"path": "note.txt", "content": "hi"}}), events)
        finally:
            srv.shutdown()

    def test_plain_answer_no_tools(self):
        srv, base, _ = start_scripted_server([answer_msg("just text")])
        try:
            turn = self._agent(base).run("hello")
            self.assertEqual(turn.text, "just text")
            self.assertEqual(turn.steps, 1)
            self.assertEqual(turn.usage.get("total_tokens"), 7)
        finally:
            srv.shutdown()

    def test_max_steps_guard(self):
        # asks for a tool every step → after max_steps the loop makes one final
        # no-tools call (script index == max_steps) that returns the answer
        srv, base, _ = start_scripted_server([
            tool_call_msg("now", {}), tool_call_msg("now", {}),
            answer_msg("final"),
        ])
        try:
            turn = self._agent(base, max_steps=2).run("loop")
            self.assertEqual(turn.stopped, "max_steps")
            self.assertEqual(turn.text, "final")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
