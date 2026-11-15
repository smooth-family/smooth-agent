"""Steering a running turn, interim messages, tool icons, and the owner's protect/secrets guard."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from smooth_agent.agent import Agent
from smooth_agent.channels import ChannelHub, Inbound, describe_step, step_icon
from smooth_agent.routers import Completion
from smooth_agent.tools import PathGuard, Registry, Workspace, default_registry

from tests.test_stage5 import ProgressChannel, StubRouter, answer, wait_for


def call(name, args="{}", cid="c1", text=""):
    c = {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
    return Completion(text=text, model="m", provider="main", tool_calls=[c], finish_reason="tool_calls",
                      message={"role": "assistant", "content": text or None, "tool_calls": [c]})


class SteerTests(unittest.TestCase):
    def test_message_sent_mid_turn_is_read_before_the_next_call(self):
        seen = []
        reg = Registry()
        agent = None

        def slow_ping(args):
            agent.steer("pakai format tabel ya")  # the owner types while the tool runs
            return "pong"
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(slow_ping)
        script = [call("ping"), answer("ok, tabel")]

        def route(messages):
            seen.append(messages[-1]["content"])
            return script.pop(0)
        agent = Agent(StubRouter(route), reg)
        events = []
        agent.on_event = lambda e, d: events.append(e)
        self.assertEqual(agent.run("cek").text, "ok, tabel")
        self.assertIn("pakai format tabel ya", seen[-1])
        self.assertIn("steered", events)
        self.assertEqual(agent.take_inbox(), [])

    def test_steer_during_the_final_answer_continues_the_turn(self):
        agent = None
        script = []

        def route(messages):
            if not script:
                agent.steer("satu lagi")
                script.append(1)
                return answer("first answer")
            return answer("answer with the extra")
        agent = Agent(StubRouter(route), Registry())
        self.assertEqual(agent.run("go").text, "answer with the extra")

    def test_hub_steers_the_running_chat_and_requeues_late_messages(self):
        gate = threading.Event()
        reg = Registry()
        reg.tool("wait", "wait", {"type": "object", "properties": {}})(lambda a: gate.wait(5) and "done")
        script = [call("wait"), answer("first done"), answer("second turn")]
        ch = ProgressChannel()
        agents = {}

        def agent_for(sid, channel, chat):
            agents[sid] = Agent(StubRouter(lambda m: script.pop(0)), reg)
            return agents[sid]
        hub = ChannelHub([ch], agent_for, log=lambda m: None)
        hub.start()
        self.addCleanup(hub.stop)
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="start", user_id="1"))
        self.assertTrue(wait_for(lambda: any(a.running for a in agents.values())))
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="tambahan", user_id="1"))
        self.assertTrue(wait_for(lambda: any("Noted" in s for s in ch.sent)))
        gate.set()
        self.assertTrue(wait_for(lambda: "first done" in ch.sent))


class InterimTests(unittest.TestCase):
    def test_text_before_a_tool_call_is_an_interim_event(self):
        reg = Registry()
        reg.tool("ping", "ping", {"type": "object", "properties": {}})(lambda a: "pong")
        script = [call("ping", text="Saya cek dulu servernya."), answer("beres")]
        events = []
        Agent(StubRouter(lambda m: script.pop(0)), reg, on_event=lambda e, d: events.append((e, d.get("text")))).run("x")
        self.assertIn(("interim", "Saya cek dulu servernya."), events)


class IconTests(unittest.TestCase):
    def test_icons(self):
        self.assertEqual(step_icon("terminal") + " " + describe_step("terminal", {"command": "date"}), "💻 $ date")
        self.assertEqual(step_icon("read_file"), "📖")
        self.assertEqual(step_icon("mcp_x"), "🔌")
        self.assertEqual(step_icon("browser_click"), "🌐")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.g = PathGuard(protect=["/srv/work", "~/proj"], secrets=["/srv/keys/wallet.json"])

    def test_secrets_are_untouchable(self):
        self.assertIn("secrets", self.g.check("read_file", {"path": "/srv/keys/wallet.json"}))
        self.assertIn("secrets", self.g.check("terminal", {"command": "cat /srv/keys/wallet.json"}))

    def test_protected_paths_are_read_only(self):
        ok = ["cat /srv/work/a/STATUS.md", "ls -la /srv/work 2>/dev/null", "git -C /srv/work/e log --oneline 2>&1 | head",
              "tail -n 20 ~/proj/log.txt"]
        for cmd in ok:
            self.assertEqual(self.g.check("terminal", {"command": cmd}), "", cmd)
        bad = ["cd /srv/work/e && git commit -m x", "echo hi > /srv/work/a.txt", "cp x /srv/work/", "rm -rf /srv/work/e",
               "curl -sL url | tar xz -C /srv/work", "python3 /srv/work/build.py", "sed -i s/a/b/ ~/proj/x"]
        for cmd in bad:
            self.assertIn("read-only", self.g.check("terminal", {"command": cmd}), cmd)
        self.assertIn("read-only", self.g.check("write_file", {"path": "/srv/work/x.txt", "content": "x"}))
        self.assertEqual(self.g.check("read_file", {"path": "/srv/work/x.txt"}), "")
        self.assertEqual(self.g.check("terminal", {"command": "echo ok > /tmp/x"}), "")

    def test_registry_enforces_the_guard(self):
        ws = Path(tempfile.mkdtemp())
        reg = default_registry(Workspace(ws))
        reg.guard = PathGuard(protect=[str(ws / "keep")])
        out = reg.run("write_file", {"path": str(ws / "keep" / "a.txt"), "content": "x"})
        self.assertTrue(out.startswith("blocked"))
        self.assertFalse((ws / "keep" / "a.txt").exists())
        self.assertIs(reg.copy().guard, reg.guard)


if __name__ == "__main__":
    unittest.main()
