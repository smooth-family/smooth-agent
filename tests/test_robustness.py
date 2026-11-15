"""Tool-argument tolerance, the repeated-call notice, and provider usage kept in the session record."""
import tempfile
import unittest
from pathlib import Path

from smooth_agent.agent import Agent
from smooth_agent.processes import _toint as proc_toint
from smooth_agent.routers import Completion
from smooth_agent.store import Store
from smooth_agent.tools import Registry, _toint

from tests.test_stage5 import StubRouter, answer


def call(name, args="{}", cid="c1", usage=None):
    c = {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
    return Completion(text="", model="main-model", provider="main", tool_calls=[c], finish_reason="tool_calls",
                      message={"role": "assistant", "content": None, "tool_calls": [c]}, usage=usage or {})


class IntArgTests(unittest.TestCase):
    def test_float_strings_and_floats_become_ints(self):
        for f in (_toint, proc_toint):
            self.assertEqual(f("40.0"), 40)
            self.assertEqual(f(40.0), 40)
            self.assertEqual(f("7"), 7)
            self.assertEqual(f(None, 5), 5)
            self.assertEqual(f("", 5), 5)

    def test_read_file_accepts_float_offset(self):
        from smooth_agent.tools import Workspace, default_registry
        ws = Path(tempfile.mkdtemp())
        (ws / "a.txt").write_text("one\ntwo\nthree\n")
        reg = default_registry(Workspace(ws))
        out = reg.run("read_file", {"path": "a.txt", "offset": "2.0", "limit": 1.0})
        self.assertIn("two", out)
        self.assertNotIn("Error", out)


class RepeatNoticeTests(unittest.TestCase):
    def _run(self, repeats):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        script = [call("ping", cid=f"c{i}") for i in range(repeats)] + [answer("done")]
        router = StubRouter(lambda messages: script.pop(0))
        agent = Agent(router, reg)
        agent.run("go")
        return [m["content"] for m in agent.history if m.get("role") == "tool"]

    def test_third_identical_call_gets_a_notice(self):
        results = self._run(3)
        self.assertEqual(results[:2], ["pong", "pong"])
        self.assertIn("3 times in a row", results[2])
        self.assertTrue(results[2].endswith("pong"))

    def test_two_identical_calls_get_none(self):
        self.assertEqual(self._run(2), ["pong", "pong"])


class UsageRecordTests(unittest.TestCase):
    def test_usage_saved_in_record_but_never_sent_back(self):
        store = Store(Path(tempfile.mkdtemp()) / "s.db")
        store.new_session("s1")
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        script = [call("ping", usage={"prompt_tokens": 11, "completion_tokens": 2}), answer("done")]
        router = StubRouter(lambda messages: script.pop(0))
        Agent(router, reg, store=store, session_id="s1").run("go")

        saved = [m for m in store.session_messages("s1") if m.get("role") == "assistant"]
        self.assertEqual(saved[0]["_usage"], {"prompt_tokens": 11, "completion_tokens": 2})
        self.assertEqual(saved[0]["_model"], "main-model")
        # the model never sees the record-only fields, live or after resume
        self.assertFalse(any(k.startswith("_") for m in router.calls[-1]["messages"] for k in m))
        again = Agent(StubRouter(lambda messages: answer("ok")), reg, store=store)
        again.resume("s1")
        self.assertFalse(any(k.startswith("_") for m in again.history for k in m))


if __name__ == "__main__":
    unittest.main()
