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

    def _approving_hub(self, timeout):
        """A button-less channel whose agent asks for approval in the middle of its turn."""
        ch = FakeChannel(owners=["1"])
        ch.supports_buttons = False
        box = {}

        class AskingAgent:
            def run(self, text):
                ok = box["hub"].approve("terminal", {"command": "deploy"})
                return Turn(text=f"ran={ok} after:{text}", steps=1)
        hub = ChannelHub([ch], lambda *a: AskingAgent(), log=lambda m: None, approval_timeout=timeout)
        box["hub"] = hub
        hub.start()
        return hub, ch

    def _wait(self, cond, secs=5.0):
        end = time.time() + secs
        while not cond() and time.time() < end:
            time.sleep(0.02)
        return cond()

    def test_typed_yes_approves_while_worker_waits(self):
        from smooth_agent.channels import Inbound
        hub, ch = self._approving_hub(timeout=10)
        try:
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="deploy it", user_id="1"))
            self.assertTrue(self._wait(lambda: any("Reply yes" in t for _, t, _ in ch.sent)), ch.sent)
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="Ya!", user_id="1"))
            self.assertTrue(self._wait(lambda: any(t.startswith("ran=") for _, t, _ in ch.sent)), ch.sent)
            self.assertIn("ran=True after:deploy it", [t for _, t, _ in ch.sent])
            self.assertEqual(hub._awaiting, {})
            self.assertTrue(hub._q.empty())  # "Ya!" was consumed as the answer, not queued as a message
        finally:
            hub.stop()

    def test_typed_no_and_unrelated_text(self):
        from smooth_agent.channels import Inbound
        hub, ch = self._approving_hub(timeout=10)
        try:
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="deploy", user_id="1"))
            self.assertTrue(self._wait(lambda: hub._awaiting))
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="what is it doing?", user_id="1"))
            self.assertEqual(hub._q.qsize(), 1)  # a real message is queued, not taken as an answer
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="gak", user_id="1"))
            self.assertTrue(self._wait(lambda: any(t == "ran=False after:deploy" for _, t, _ in ch.sent)), ch.sent)
        finally:
            hub.stop()

    def test_no_answer_is_denied_and_said(self):
        from smooth_agent.channels import Inbound
        hub, ch = self._approving_hub(timeout=0.3)
        try:
            hub.dispatch(ch, Inbound(channel="fake", chat_id="1", text="deploy", user_id="1"))
            self.assertTrue(self._wait(lambda: any(t.startswith("ran=") for _, t, _ in ch.sent)), ch.sent)
            texts = [t for _, t, _ in ch.sent]
            self.assertIn("ran=False after:deploy", texts)
            self.assertTrue(any(t.startswith("No answer") for t in texts))
        finally:
            hub.stop()

    def test_failed_channel_is_dropped(self):
        class Broken(FakeChannel):
            def start(self, hub):
                raise RuntimeError("no token")
        good, bad = FakeChannel(owners=["1"]), Broken(owners=["1"], name="broken")
        hub = ChannelHub([good, bad], lambda *a: FakeAgent("x"), log=lambda m: None)
        hub.start()
        hub.stop()
        self.assertEqual(list(hub.channels), ["fake"])

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


def _slack_sig(secret, ts, body):
    import hashlib
    import hmac
    return "v0=" + hmac.new(secret.encode(), b"v0:" + str(ts).encode() + b":" + body, hashlib.sha256).hexdigest()


class SlackTests(unittest.TestCase):
    def _ch(self):
        from smooth_agent.channels import SlackChannel
        ch = SlackChannel(ChannelConfig("slack", owners=["U1"], options={"token": "xoxb", "signing_secret": "s3"}),
                          log=lambda m: None)
        ch.hub = DummyHub()
        return ch

    def _post(self, ch, payload, *, ts=None, secret="s3", extra=None):
        body = json.dumps(payload).encode()
        ts = ts if ts is not None else int(time.time())
        h = {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": _slack_sig(secret, ts, body), **(extra or {})}
        return ch.handle_webhook("POST", "/webhook/slack", h, body)

    def test_url_verification(self):
        self.assertEqual(self._post(self._ch(), {"type": "url_verification", "challenge": "abc"}), (200, b"abc"))

    def test_message_and_filters(self):
        ch = self._ch()
        ev = {"type": "event_callback", "event": {"type": "message", "user": "U1", "channel": "D9", "text": "hi"}}
        self.assertEqual(self._post(ch, ev)[0], 200)
        self.assertEqual((ch.hub.dispatched[0].chat_id, ch.hub.dispatched[0].user_id), ("D9", "U1"))
        self._post(ch, {"event": {"type": "message", "bot_id": "B", "channel": "D9", "text": "echo"}})
        self._post(ch, {"event": {"type": "message", "subtype": "message_changed", "channel": "D9", "text": "x"}})
        self._post(ch, ev, extra={"X-Slack-Retry-Num": "1"})
        self.assertEqual(len(ch.hub.dispatched), 1)  # bot echo, edits and retries ignored

    def test_bad_signature_and_replay(self):
        ch = self._ch()
        ev = {"event": {"type": "message", "user": "U1", "channel": "D", "text": "x"}}
        self.assertEqual(self._post(ch, ev, secret="wrong")[0], 401)
        self.assertEqual(self._post(ch, ev, ts=int(time.time()) - 3600)[0], 401)  # stale timestamp
        self.assertEqual(ch.handle_webhook("POST", "/webhook/slack", {}, b"{}")[0], 401)
        self.assertEqual(ch.hub.dispatched, [])


class LineTests(unittest.TestCase):
    def test_verify_and_parse(self):
        import base64
        import hashlib
        import hmac
        from smooth_agent.channels import LineChannel
        ch = LineChannel(ChannelConfig("line", owners=["Uabc"], options={"token": "t", "secret": "sec"}), log=lambda m: None)
        ch.hub = DummyHub()
        body = json.dumps({"events": [
            {"type": "message", "message": {"type": "text", "text": "halo"}, "source": {"type": "user", "userId": "Uabc"}},
            {"type": "message", "message": {"type": "sticker"}, "source": {"userId": "Uabc"}},
            {"type": "follow", "source": {"userId": "Uabc"}},
        ]}).encode()
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        self.assertEqual(ch.handle_webhook("POST", "/webhook/line", {"x-line-signature": sig}, body)[0], 200)
        self.assertEqual(len(ch.hub.dispatched), 1)
        self.assertEqual((ch.hub.dispatched[0].chat_id, ch.hub.dispatched[0].text), ("Uabc", "halo"))
        self.assertEqual(ch.handle_webhook("POST", "/webhook/line", {"x-line-signature": "nope"}, body)[0], 401)


class MatrixTests(unittest.TestCase):
    def test_sync_messages_echo_and_invite(self):
        from smooth_agent.channels import MatrixChannel
        ch = MatrixChannel(ChannelConfig("matrix", owners=["@me:hs"], options={"token": "t", "user_id": "@bot:hs"}),
                           log=lambda m: None)
        ch.hub = DummyHub()
        calls = []
        ch._req = lambda method, path, payload=None, **k: calls.append((method, path)) or {}
        ch._handle_sync({"rooms": {
            "join": {"!r:hs": {"timeline": {"events": [
                {"type": "m.room.message", "sender": "@me:hs", "content": {"msgtype": "m.text", "body": "hey"}},
                {"type": "m.room.message", "sender": "@bot:hs", "content": {"msgtype": "m.text", "body": "echo"}},
                {"type": "m.room.message", "sender": "@me:hs", "content": {"msgtype": "m.image", "body": "x.png"}},
            ]}}},
            "invite": {
                "!ok:hs": {"invite_state": {"events": [{"type": "m.room.member", "sender": "@me:hs", "content": {"membership": "invite"}}]}},
                "!spam:hs": {"invite_state": {"events": [{"type": "m.room.member", "sender": "@x:hs", "content": {"membership": "invite"}}]}},
            },
        }})
        self.assertEqual([(i.chat_id, i.text) for i in ch.hub.dispatched], [("!r:hs", "hey")])
        joined = [p for m, p in calls if p.endswith("/join")]
        self.assertEqual(len(joined), 1)
        self.assertIn("%21ok%3Ahs", joined[0])  # only the owner's invite is accepted

    def test_send_puts_message(self):
        from smooth_agent.channels import MatrixChannel
        ch = MatrixChannel(ChannelConfig("matrix", options={"token": "t"}), log=lambda m: None)
        calls = []
        ch._req = lambda method, path, payload=None, **k: calls.append((method, path, payload)) or {}
        ch.send("!r:hs", "hello")
        self.assertEqual(calls[0][0], "PUT")
        self.assertIn("/rooms/%21r%3Ahs/send/m.room.message/", calls[0][1])
        self.assertEqual(calls[0][2], {"msgtype": "m.text", "body": "hello"})


class DeliveryWebhookTests(unittest.TestCase):
    def test_discord_chunks_and_no_mentions(self):
        from unittest import mock

        from smooth_agent import channels as chmod
        ch = chmod.DiscordChannel(ChannelConfig("discord", options={"webhook_url": "https://d/x"}), log=lambda m: None)
        with mock.patch.object(chmod, "_post_json", return_value=(204, {})) as p:
            ch.send("webhook", "y" * 4000)
        self.assertEqual(p.call_count, 3)  # 1900 + 1900 + 200
        url, payload = p.call_args_list[0][0]
        self.assertEqual(url, "https://d/x")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


class SetupHelperTests(unittest.TestCase):
    def test_strip_channels_table(self):
        from smooth_agent.config import tomllib

        from smooth_agent.cli import _strip_channels_table
        src = ('default = "m"\n[agent]\nx = 1\n[channels]\nenabled = []\n# [channels.telegram]\n'
               '[channels.slack]\nowners = []\n[mcp]\nconfirm = false\n')
        out = _strip_channels_table(src) + '\n[channels]\nenabled = ["telegram"]\n'
        cfg = tomllib.loads(out)  # would raise on a duplicate [channels] table
        self.assertEqual(cfg["channels"], {"enabled": ["telegram"]})
        self.assertEqual(cfg["mcp"], {"confirm": False})
        self.assertEqual(cfg["agent"], {"x": 1})

    def test_example_config_parses_with_all_channels(self):
        from smooth_agent.config import tomllib
        from pathlib import Path
        from smooth_agent.channels import CHANNEL_TYPES
        ex = (Path(__file__).resolve().parents[1] / "smooth_agent" / "data" / "config.example.toml").read_text()
        tomllib.loads(ex)
        for name in CHANNEL_TYPES:
            self.assertIn(f"[channels.{name}]", ex, f"example config should document {name}")


if __name__ == "__main__":
    unittest.main()
