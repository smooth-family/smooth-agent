"""Streaming answers: tool calls assembled from deltas, gateways that ignore stream=true,
Anthropic streams, the agent's delta events and the live Telegram draft."""
import json
import unittest

from smooth_agent.agent import Agent
from smooth_agent.routers import Completion, Provider, Router, _anthropic_stream, _openai_stream
from smooth_agent.tools import Registry

from tests.test_routers import start_server


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def lines(*events):
    return [("data", json.dumps(e)) for e in events] + [("data", "[DONE]")]


class OpenAIStreamTests(unittest.TestCase):
    def test_text_tool_calls_and_usage(self):
        got = []
        ev = lines(
            {"choices": [{"delta": {"content": "Let me "}}]},
            {"choices": [{"delta": {"content": "check."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "ping", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"x\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " 1}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "pong", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
        text, calls, message, finish, usage = _openai_stream(ev, got.append)
        self.assertEqual(text, "Let me check.")
        self.assertEqual("".join(got), "Let me check.")
        self.assertEqual([c["function"]["name"] for c in calls], ["ping", "pong"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"x": 1})
        self.assertEqual(message["tool_calls"], calls)
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(usage, {"prompt_tokens": 7, "completion_tokens": 3})

    def test_gateway_that_ignores_stream(self):
        body = json.dumps({"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}], "usage": {"total_tokens": 4}})
        got = []
        text, calls, message, finish, usage = _openai_stream([("raw", body)], got.append)
        self.assertEqual((text, finish, usage["total_tokens"], got), ("whole", "stop", 4, ["whole"]))

    def test_error_event_raises(self):
        from smooth_agent.routers import RouterError
        with self.assertRaises(RouterError):
            _openai_stream(lines({"error": {"message": "busy"}}), lambda s: None)


class AnthropicStreamTests(unittest.TestCase):
    def test_text_and_tool_use(self):
        got = []
        ev = lines(
            {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "On it."}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "ping", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"a\": "}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "2}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
        )
        text, calls, message, finish, usage = _anthropic_stream(ev, got.append)
        self.assertEqual((text, "".join(got), finish), ("On it.", "On it.", "tool_calls"))
        self.assertEqual(calls[0]["id"], "t1")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"a": 2})
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"]), (9, 5))


class RouterStreamingTests(unittest.TestCase):
    def prov(self, base, name="p"):
        return Provider.from_config(name, {"provider": "openai-compatible", "model": "m", "base_url": base, "key_env": "K"}, env={"K": "s"})

    def test_complete_with_on_text_streams_and_returns_a_completion(self):
        seen = {}

        def route(req):
            seen.update(req)
            return 200, sse({"choices": [{"delta": {"content": "hel"}}]}, {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                            {"choices": [], "usage": {"total_tokens": 3}})
        srv, base = start_server({"/chat/completions": route})
        try:
            parts = []
            c = Router([self.prov(base)]).complete([{"role": "user", "content": "x"}], on_text=parts.append)
            self.assertEqual((c.text, parts, c.usage["total_tokens"]), ("hello", ["hel", "lo"], 3))
            self.assertTrue(seen["stream"])
            self.assertEqual(seen["stream_options"], {"include_usage": True})
        finally:
            srv.shutdown()

    def test_fallback_resets_the_draft(self):
        srv1, bad = start_server({"/chat/completions": (503, "busy")})
        srv2, good = start_server({"/chat/completions": lambda r: (200, sse({"choices": [{"delta": {"content": "ok"}}]}))})
        try:
            parts = []
            r = Router([self.prov(bad, "a"), self.prov(good, "b")], fallbacks=["b"])
            c = r.complete([{"role": "user", "content": "x"}], on_text=parts.append)
            self.assertEqual(c.text, "ok")
            self.assertEqual(parts, [None, "ok"])
        finally:
            srv1.shutdown(); srv2.shutdown()


class AgentStreamingTests(unittest.TestCase):
    def test_delta_events(self):
        class StreamRouter:
            providers, default, fallbacks = {}, "main", []

            def complete(self, messages, provider=None, model=None, max_tokens=None, extra=None, on_text=None):
                for p in ("Hal", "o!"):
                    on_text(p)
                return Completion(text="Halo!", model="m", provider="main", finish_reason="stop",
                                  message={"role": "assistant", "content": "Halo!"})

        events = []
        agent = Agent(StreamRouter(), Registry(), on_event=lambda e, d: events.append((e, d.get("text"))))
        agent.stream = True
        self.assertEqual(agent.run("hi").text, "Halo!")
        self.assertEqual([t for e, t in events if e == "delta"], ["Hal", "o!"])
        self.assertIn(("delta_start", None), events)


class DraftRenderTests(unittest.TestCase):
    def test_draft_shown_markup_hidden_and_cleared_by_a_tool(self):
        from smooth_agent.channels import Progress

        class Quiet:
            name = "t"

        p = Progress.__new__(Progress)  # no thread: test the pure parts
        import threading, time
        p.channel, p.chat_id, p.log = Quiet(), "1", print
        p.steps, p.draft, p.msg_id, p._dirty, p._last_edit, p._t0 = [], "", None, False, 0.0, time.time()
        p._lock = threading.Lock()
        p.on_event("delta", {"text": "Jawabannya: 42"})
        self.assertIn("✍️ Jawabannya: 42", p._render())
        p.on_event("delta", {"text": "<|tool_call:start|>ping"})
        self.assertNotIn("tool_call", p._render())
        p.on_event("tool", {"name": "ping", "arguments": {}})
        self.assertNotIn("✍️", p._render())


if __name__ == "__main__":
    unittest.main()
