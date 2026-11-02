"""Stage 4: chat channels, the hub, approvals, delivery and config."""
import json
import threading
import time
import unittest

from smooth_agent.agent import Turn
from smooth_agent.channels import (Channel, ChannelConfig, ChannelHub, TelegramChannel, WhatsAppChannel,
                                    add_send_message_tool, build_channels, chunk)
from smooth_agent.config import load_channels
from smooth_agent.tools import Registry


class FakeChannel(Channel):
    kind = "fake"
    supports_buttons = True

    def __init__(self, owners, *, auto_approve=False, name="fake"):
        super().__init__(ChannelConfig(name, owners=[str(o) for o in owners], auto_approve=auto_approve), log=lambda m: None)
        self.sent = []  # (chat_id, text, buttons)

    def send(self, chat_id, text, *, buttons=None):
        self.sent.append((str(chat_id), text, buttons))


class FakeAgent:
    def __init__(self, session_id):
        self.session_id = session_id
        self.seen = []

    def run(self, text):
        self.seen.append(text)
        return Turn(text=f"echo:{text}", steps=1)


class DummyHub:
    def __init__(self):
        self.dispatched = []
        self.resolved = []

    def dispatch(self, channel, inbound):
        self.dispatched.append(inbound)

    def resolve_approval(self, key, ok):
        self.resolved.append((key, ok))


class ChunkTests(unittest.TestCase):
    def test_chunk(self):
        self.assertEqual(chunk("short"), ["short"])
        parts = chunk("a\n" * 4000, limit=100)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual("".join(p.replace("\n", "") for p in parts), "a" * 4000)
        self.assertEqual(chunk(""), ["(no answer)"])
        big = "x" * 250
        self.assertTrue(all(len(p) <= 100 for p in chunk(big, limit=100)))


class ConfigTests(unittest.TestCase):
    def test_load_channels_resolves_env(self):
        cfg = {"channels": {
            "enabled": ["telegram", "wechat", "nope"],
            "webhook_port": 9000,
            "telegram": {"token_env": "TG", "owners": [111, "222"], "auto_approve": True},
            "wechat": {"webhook_url_env": "WURL", "owners": ["robot"]},
        }}
        ch = load_channels(cfg, env={"TG": "secret-token", "WURL": "https://robot"})
        self.assertEqual(ch.webhook_port, 9000)
        self.assertEqual([s.name for s in ch.specs], ["telegram", "wechat"])  # unknown dropped
        tg = ch.specs[0]
        self.assertEqual(tg.owners, ["111", "222"])
        self.assertTrue(tg.auto_approve)
        self.assertEqual(tg.options["token"], "secret-token")
        self.assertNotIn("token_env", tg.options)
        self.assertEqual(ch.specs[1].options["webhook_url"], "https://robot")

    def test_default_token_env(self):
        ch = load_channels({"channels": {"enabled": ["telegram"], "telegram": {"owners": ["1"]}}},
                           env={"TELEGRAM_BOT_TOKEN": "fromdefault"})
        self.assertEqual(ch.specs[0].options["token"], "fromdefault")

    def test_build_channels(self):
        ch = load_channels({"channels": {"enabled": ["telegram"], "telegram": {"owners": ["1"], "token_env": "T"}}},
                           env={"T": "x"})
        built = build_channels(ch.specs, log=lambda m: None)
        self.assertEqual(len(built), 1)
        self.assertIsInstance(built[0], TelegramChannel)


class HubTests(unittest.TestCase):
    def _hub(self, channel, agents=None):
        agents = agents if agents is not None else {}

        def agent_for(sid, ch, chat):
            a = agents.get(sid)
            if a is None:
                a = agents[sid] = FakeAgent(sid)
            return a
        return ChannelHub([channel], agent_for, log=lambda m: None), agents

    def test_owner_only(self):
        ch = FakeChannel(owners=["111"])
        hub, _ = self._hub(ch)
        ch.start(hub)
        from smooth_agent.channels import Inbound
        hub.dispatch(ch, Inbound(channel="fake", chat_id="999", text="hi", user_id="999"))
        self.assertTrue(hub._q.empty())
        self.assertIn("private", ch.sent[-1][1])  # stranger told it is private, with their id
        self.assertIn("999", ch.sent[-1][1])

    def test_message_roundtrip_and_session_per_chat(self):
        ch = FakeChannel(owners=["111", "222"])
        hub, agents = self._hub(ch)
        hub.start()
        from smooth_agent.channels import Inbound
        hub.dispatch(ch, Inbound(channel="fake", chat_id="111", text="hello", user_id="111"))
        hub.dispatch(ch, Inbound(channel="fake", chat_id="222", text="world", user_id="222"))
        deadline = time.time() + 5
        while len([s for s in ch.sent]) < 2 and time.time() < deadline:
            time.sleep(0.02)
        hub.stop()
        texts = [t for _, t, _ in ch.sent]
        self.assertIn("echo:hello", texts)
        self.assertIn("echo:world", texts)
        self.assertEqual(set(agents), {"fake-111", "fake-222"})  # one session per chat

    def test_auto_approve(self):
        ch = FakeChannel(owners=["1"], auto_approve=True)
        hub, _ = self._hub(ch)
        hub._active = (ch, "1")
        self.assertTrue(hub.approve("terminal", {"command": "ls"}))
        self.assertEqual(ch.sent, [])  # no button prompt when auto-approving

    def test_button_approval(self):
        ch = FakeChannel(owners=["1"])
        hub, _ = self._hub(ch)
        hub._active = (ch, "1")
        result = {}
        t = threading.Thread(target=lambda: result.update(ok=hub.approve("terminal", {"command": "rm x"})))
        t.start()
        deadline = time.time() + 5
        while not ch.sent and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(ch.sent and ch.sent[0][2], "expected an approval message with buttons")
        key = ch.sent[0][2][0][1].split(":", 1)[1]  # callback data "a:<key>"
        hub.resolve_approval(key, True)
        t.join(timeout=5)
        self.assertTrue(result.get("ok"))

    def test_approval_no_active_chat(self):
        hub, _ = self._hub(FakeChannel(owners=["1"]))
        self.assertFalse(hub.approve("terminal", {}))

    def test_send_message_tool(self):
        ch = FakeChannel(owners=["1"])
        hub, _ = self._hub(ch)
        reg = Registry()
        add_send_message_tool(reg, hub)
        out = reg.run("send_message", {"text": "ping", "channel": "fake", "chat_id": "42"})
        self.assertIn("sent to fake (42)", out)
        self.assertEqual(ch.sent[-1][:2], ("42", "ping"))
        # defaults to the active chat
        hub._active = (ch, "77")
        reg.run("send_message", {"text": "yo"})
        self.assertEqual(ch.sent[-1][:2], ("77", "yo"))
        # falls back to the owner when no chat is known
        hub._active = None
        reg.run("send_message", {"text": "owner"})
        self.assertEqual(ch.sent[-1][0], "1")

    def test_deliver_hook(self):
        ch = FakeChannel(owners=["1"])
        hub, _ = self._hub(ch)
        deliver = hub.deliver_hook()
        deliver({"name": "news", "deliver": "fake"}, "the summary", True)
        self.assertEqual(ch.sent[-1][0], "1")  # went to the owner
        self.assertIn("the summary", ch.sent[-1][1])
        self.assertIn("[news]", ch.sent[-1][1])
        deliver({"name": "news", "deliver": "fake:9"}, "oops", False)  # explicit chat + failure prefix
        self.assertEqual(ch.sent[-1][0], "9")
        self.assertIn("cron failed", ch.sent[-1][1])
        deliver({"name": "x", "deliver": "unknown"}, "nope", True)  # unknown channel: silently skipped
        self.assertNotIn("nope", ch.sent[-1][1])


class TelegramParseTests(unittest.TestCase):
    def test_message_and_callback(self):
        tg = TelegramChannel(ChannelConfig("telegram", owners=["111"], options={"token": "x"}), log=lambda m: None)
        tg._api = lambda method, payload, **k: {"ok": True}  # no network
        hub = DummyHub()
        tg.hub = hub
        tg._dispatch({"message": {"chat": {"id": 111}, "from": {"id": 111, "username": "me"}, "text": "hi there"}})
        self.assertEqual(len(hub.dispatched), 1)
        self.assertEqual((hub.dispatched[0].chat_id, hub.dispatched[0].text), ("111", "hi there"))
        tg._dispatch({"callback_query": {"id": "q1", "data": "a:ABC123"}})
        self.assertEqual(hub.resolved, [("ABC123", True)])
        tg._dispatch({"callback_query": {"id": "q2", "data": "d:ABC123"}})
        self.assertEqual(hub.resolved[-1], ("ABC123", False))


class WhatsAppWebhookTests(unittest.TestCase):
    def _ch(self):
        return WhatsAppChannel(ChannelConfig("whatsapp", owners=["628"],
                               options={"token": "t", "phone_id": "p", "verify_token": "V"}), log=lambda m: None)

    def test_verify(self):
        wa = self._ch()
        wa.hub = DummyHub()
        status, body = wa.handle_webhook("GET", "/webhook/whatsapp?hub.verify_token=V&hub.challenge=98765", {}, b"")
        self.assertEqual((status, body), (200, b"98765"))
        status, _ = wa.handle_webhook("GET", "/webhook/whatsapp?hub.verify_token=wrong&hub.challenge=x", {}, b"")
        self.assertEqual(status, 403)

    def test_inbound(self):
        wa = self._ch()
        hub = DummyHub()
        wa.hub = hub
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "628", "type": "text", "text": {"body": "hello"}},
            {"from": "628", "type": "image", "image": {}},  # non-text ignored
        ]}}]}]}
        status, _ = wa.handle_webhook("POST", "/webhook/whatsapp", {}, json.dumps(payload).encode())
        self.assertEqual(status, 200)
        self.assertEqual(len(hub.dispatched), 1)
        self.assertEqual(hub.dispatched[0].text, "hello")


if __name__ == "__main__":
    unittest.main()
