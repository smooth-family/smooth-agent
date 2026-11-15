"""Tool-argument tolerance, the repeated-call notice, provider usage in the session record, and tool calls a gateway leaked as text."""
import json
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


class TextToolCallTests(unittest.TestCase):
    MARK = ("Writing it now.<|tool_call:start|>write_file<|tool_arg:start|>path<|tool_arg:value|>a.txt"
            "<|tool_arg:start|>content<|tool_arg:value|>hello\nworld")

    def test_parse(self):
        from smooth_agent.agent import recover_text_tool_calls
        head, calls = recover_text_tool_calls(self.MARK)
        self.assertEqual(head, "Writing it now.")
        self.assertEqual(calls[0]["function"]["name"], "write_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a.txt", "content": "hello\nworld"})

    def _agent(self, finish):
        got = []
        reg = Registry()
        reg.tool("write_file", "w", {"type": "object", "properties": {}})(lambda args: got.append(args) or "wrote")
        first = Completion(text=self.MARK, model="m", provider="main", finish_reason=finish,
                           message={"role": "assistant", "content": self.MARK})
        script = [first, answer("done")]
        router = StubRouter(lambda messages: script.pop(0))
        turn = Agent(router, reg).run("go")
        return turn, got, router

    def test_complete_text_call_runs(self):
        turn, got, _ = self._agent("stop")
        self.assertEqual(got, [{"path": "a.txt", "content": "hello\nworld"}])
        self.assertEqual(turn.text, "done")

    def test_cut_off_text_call_does_not_run_and_turn_continues(self):
        turn, got, router = self._agent("length")
        self.assertEqual(got, [])
        self.assertEqual(turn.text, "done")
        self.assertIn("did NOT run", router.calls[-1]["messages"][-1]["content"])


class KeepGoingTests(unittest.TestCase):
    def _run(self, script, keep_going=3, max_steps=20):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        router = StubRouter(lambda messages: script.pop(0))
        agent = Agent(router, reg, keep_going=keep_going, max_steps=max_steps)
        return agent.run("go"), agent

    def test_stopping_with_a_plan_is_sent_back_to_work(self):
        from smooth_agent.agent import KEEP_GOING
        script = [call("ping", cid="a"), answer("Next: I will ping again."), call("ping", cid="b"),
                  answer("All checked.\nTASK COMPLETE")]
        turn, agent = self._run(script)
        self.assertEqual(turn.text, "All checked.")
        self.assertEqual(sum(KEEP_GOING in str(m.get("content")) for m in agent.history), 1)
        self.assertEqual(len(turn.tool_calls), 2)

    def test_a_plain_reply_without_tools_is_not_nudged(self):
        turn, agent = self._run([answer("halo bang")])
        self.assertEqual(turn.text, "halo bang")

    def test_nudges_are_capped(self):
        script = [call("ping", cid="a")] + [answer(f"plan {i}") for i in range(5)]
        turn, _ = self._run(script, keep_going=2)
        self.assertEqual(turn.text, "plan 2")

    def test_off_by_default(self):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        script = [call("ping"), answer("Next: more work")]
        turn = Agent(StubRouter(lambda messages: script.pop(0)), reg).run("go")
        self.assertEqual(turn.text, "Next: more work")


class OutOfStepsAnswerTests(unittest.TestCase):
    def test_leaked_markup_is_not_shown(self):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        leak = answer("Here is where things stand.<|tool_call:start|>ping<|tool_arg:start|>x<|tool_arg:value|>1")
        script = [call("ping", cid="a"), leak]
        turn = Agent(StubRouter(lambda messages: script.pop(0)), reg, max_steps=1).run("go")
        self.assertEqual(turn.text, "Here is where things stand.")


class PatientRetryTests(unittest.TestCase):
    def _agent(self, outcomes, retry_seconds=60):
        from smooth_agent.routers import RouterError

        def script(messages):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        events = []
        agent = Agent(StubRouter(script), Registry(), retry_seconds=retry_seconds,
                      on_event=lambda e, d: events.append(e))
        agent.retry_first, agent.retry_max = 0.01, 0.02
        return agent, events, RouterError

    def test_busy_then_answer(self):
        from smooth_agent.routers import RouterError
        busy = RouterError("HTTP 503: All providers busy", status=503, retryable=True)
        agent, events, _ = self._agent([busy, busy, answer("done")])
        self.assertEqual(agent.run("go").text, "done")
        self.assertEqual(events.count("retry"), 2)

    def test_not_retryable_fails_at_once(self):
        from smooth_agent.routers import RouterError
        agent, events, _ = self._agent([RouterError("HTTP 401: bad key", status=401), answer("never")])
        turn = agent.run("go")
        self.assertEqual(turn.stopped, "error")
        self.assertEqual(events.count("retry"), 0)

    def test_gives_up_after_retry_seconds(self):
        from smooth_agent.routers import RouterError
        busy = RouterError("HTTP 429", status=429, retryable=True)
        agent, events, _ = self._agent([busy] * 50, retry_seconds=0.05)
        self.assertEqual(agent.run("go").stopped, "error")
        self.assertGreaterEqual(events.count("retry"), 1)

    def test_stop_while_waiting(self):
        import threading
        from smooth_agent.routers import RouterError
        busy = RouterError("HTTP 429", status=429, retryable=True)
        agent, events, _ = self._agent([busy] * 50, retry_seconds=60)
        agent.retry_first = agent.retry_max = 30
        threading.Timer(0.2, agent.cancel.set).start()
        self.assertEqual(agent.run("go").stopped, "cancelled")


class SmartKeepGoingTests(unittest.TestCase):
    def _run(self, replies):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda args: "pong")
        script = [call("ping", cid="a")] + [answer(r) for r in replies]
        return Agent(StubRouter(lambda m: script.pop(0)), reg, keep_going=3, keep_going_mode="smart").run("go")

    def test_a_finished_answer_is_not_nudged(self):
        self.assertEqual(self._run(["Sekarang jam 15:40 UTC di server.", "never"]).text, "Sekarang jam 15:40 UTC di server.")

    def test_a_plan_is_nudged(self):
        self.assertEqual(self._run(["Selanjutnya saya akan cek log-nya.", "Log bersih, semua jalan."]).text,
                         "Log bersih, semua jalan.")

    def test_unfinished_detector(self):
        from smooth_agent.agent import looks_unfinished
        for s in ["Next: copy the starter", "I'll check the logs now.", "Let me look", "langkah berikut:", "sebentar ya"]:
            self.assertTrue(looks_unfinished(s), s)
        for s in ["Hasilnya 3367.", "Done: 3 files listed.", "Harga BTC sekarang $97,120 (CoinGecko)."]:
            self.assertFalse(looks_unfinished(s), s)


class LoopAbortTests(unittest.TestCase):
    def test_same_call_repeated_ends_the_turn(self):
        reg = Registry()
        reg.tool("ls", "ls", {"type": "object", "properties": {}})(lambda a: "same listing")
        script = [call("ls", cid=f"c{i}") for i in range(20)]
        turn = Agent(StubRouter(lambda m: script.pop(0)), reg, loop_abort=4, max_steps=20).run("go")
        self.assertEqual(turn.stopped, "loop")
        self.assertEqual(len(turn.tool_calls), 4)

    def test_off_by_default(self):
        reg = Registry()
        reg.tool("ls", "ls", {"type": "object", "properties": {}})(lambda a: "same")
        script = [call("ls", cid=f"c{i}") for i in range(6)] + [answer("done")]
        self.assertEqual(Agent(StubRouter(lambda m: script.pop(0)), reg, max_steps=20).run("go").text, "done")


if __name__ == "__main__":
    unittest.main()
