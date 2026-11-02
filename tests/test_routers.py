import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from smooth_agent.routers import Provider, Router, RouterError, _to_anthropic_body


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}  # path -> (status, body) or callable(reqjson)->(status, body); set per subclass

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        spec = self.routes.get(self.path)
        if spec is None:
            self.send_response(404)
            self.end_headers()
            return
        status, body = spec(req) if callable(spec) else spec
        self.send_response(status)
        self.send_header("content-type", "text/event-stream" if req.get("stream") else "application/json")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())


def start_server(routes):
    # a fresh Handler subclass per server so concurrent servers never share routes
    handler = type("_H", (_Handler,), {"routes": routes})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def oa_json(text):
    return json.dumps({"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 5}})


def oa_stream(*parts):
    out = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in parts)
    return out + "data: [DONE]\n\n"


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.env = {"K": "secret"}

    def prov(self, base, **kw):
        cfg = {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K", **kw}
        return Provider.from_config(kw.get("name", "p"), cfg, env=self.env)

    def test_openai_complete_and_done_stripped(self):
        srv, base = start_server({"/chat/completions": lambda r: (200, oa_json("hi") + "data: [DONE]")})
        try:
            r = Router([self.prov(base)])
            c = r.complete([{"role": "user", "content": "x"}])
            self.assertEqual(c.text, "hi")
            self.assertEqual(c.usage["total_tokens"], 5)
        finally:
            srv.shutdown()

    def test_stream_deltas(self):
        srv, base = start_server({"/chat/completions": lambda r: (200, oa_stream("he", "llo"))})
        try:
            r = Router([self.prov(base)])
            got = "".join(r.stream([{"role": "user", "content": "x"}]))
            self.assertEqual(got, "hello")
        finally:
            srv.shutdown()

    def test_fallback_on_5xx(self):
        calls = {"a": 0, "b": 0}

        def a(_r):
            calls["a"] += 1
            return (503, json.dumps({"error": {"message": "busy"}}))

        def b(_r):
            calls["b"] += 1
            return (200, oa_json("from-b"))

        srv_a, ba = start_server({"/chat/completions": a})
        srv_b, bb = start_server({"/chat/completions": b})
        try:
            r = Router([self.prov(ba, name="a"), self.prov(bb, name="b")], default="a", fallbacks=["b"])
            c = r.complete([{"role": "user", "content": "x"}])
            self.assertEqual(c.text, "from-b")
            self.assertEqual(c.provider, "b")
            self.assertEqual((calls["a"], calls["b"]), (1, 1))
        finally:
            srv_a.shutdown()
            srv_b.shutdown()

    def test_anthropic_body_translation(self):
        body = _to_anthropic_body(
            [{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"}],
            "claude-haiku-5-5", 100, False, {},
        )
        self.assertEqual(body["system"], "be nice")
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        self.assertEqual(body["model"], "claude-haiku-5-5")

    def test_anthropic_wire_complete(self):
        payload = json.dumps({"content": [{"type": "text", "text": "ANS"}], "usage": {"input_tokens": 3}})
        srv, base = start_server({"/v1/messages": lambda r: (200, payload)})
        try:
            p = Provider.from_config("an", {"provider": "anthropic", "model": "claude-haiku-5-5",
                                            "base_url": base, "key_env": "K"}, env=self.env)
            c = Router([p]).complete([{"role": "user", "content": "x"}])
            self.assertEqual(c.text, "ANS")
        finally:
            srv.shutdown()

    def test_missing_key_is_skipped_then_fails_clearly(self):
        p = Provider.from_config("p", {"provider": "openai", "model": "gpt-x", "key_env": "ABSENT"}, env={})
        self.assertFalse(p.ready)
        with self.assertRaises(RouterError):
            Router([p]).complete([{"role": "user", "content": "x"}])

    def test_local_endpoint_needs_no_key(self):
        p = Provider.from_config("l", {"provider": "9router", "model": "m",
                                       "base_url": "http://127.0.0.1:20140/v1"}, env={})
        self.assertTrue(p.ready)

    def test_redacted_never_leaks_key(self):
        p = self.prov("https://x/v1")
        self.assertNotIn("secret", json.dumps(p.redacted()))
        self.assertEqual(p.redacted()["key"], "set")

    def test_default_must_exist(self):
        with self.assertRaises(RouterError):
            Router([self.prov("https://x/v1", name="a")], default="nope")


if __name__ == "__main__":
    unittest.main()
