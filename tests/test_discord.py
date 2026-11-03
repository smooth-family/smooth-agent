"""Stage 6.4: the stdlib WebSocket client and two-way Discord over a (fake) Gateway."""
import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from smooth_agent import channels as chmod
from smooth_agent.channels import ChannelConfig, ChannelHub, DiscordChannel
from smooth_agent.wsclient import GUID, WebSocket, WebSocketClosed

QUIET = dict(log=lambda m: None)


# ---------------------------------------------------------------- a tiny websocket server for tests

class Peer:
    """Server side of one websocket connection (unmasked frames out, masked frames in)."""

    def __init__(self, conn: socket.socket):
        self.conn, self.buf = conn, b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += conn.recv(4096)
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.request = head.decode()
        key = next(l.split(":", 1)[1].strip() for l in self.request.split("\r\n") if l.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())

    def _read(self, n):
        while len(self.buf) < n:
            data = self.conn.recv(65536)
            if not data:
                raise EOFError
            self.buf += data
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        b1, b2 = self._read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        assert b2 & 0x80, "client frames must be masked"
        mask = self._read(4)
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(self._read(n)))
        return b1 & 0x0F, data

    def text(self):
        while True:
            op, data = self.frame()
            if op == 1:
                return data.decode()

    def send(self, payload: bytes, op=1, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        head += bytes([n]) if n < 126 else (bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
        self.conn.sendall(head + payload)

    def send_json(self, obj):
        self.send(json.dumps(obj).encode())

    def close(self, code):
        self.send(struct.pack("!H", code), op=8)


def serve(handler, connections=1):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    errors = []

    def run():
        for _ in range(connections):
            conn, _ = srv.accept()
            try:
                handler(Peer(conn))
            except Exception as e:  # surfaced by the test
                errors.append(e)
            finally:
                time.sleep(0.05)
                conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return f"ws://127.0.0.1:{srv.getsockname()[1]}", t, errors


class WebSocketTests(unittest.TestCase):
    def test_roundtrip_ping_fragments_big_and_close(self):
        got = {}

        def handler(p):
            got["path"] = p.request.split(" ")[1]
            got["hello"] = p.text()
            p.send(b"ping!", op=9)
            op, data = p.frame()
            got["pong"] = (op, data)
            p.send(b"frag", op=1, fin=False)
            p.send(b"mented", op=0, fin=True)
            p.send(b"x" * 70000)
            p.send(b"\x00\x01", op=2)
            p.close(4014)
            p.frame()  # the client's close echo

        url, t, errors = serve(handler)
        ws = WebSocket.connect(url + "/?v=10&encoding=json")
        ws.send_text("hello")
        self.assertEqual(ws.recv(), "fragmented")
        self.assertEqual(len(ws.recv()), 70000)
        self.assertEqual(ws.recv(), b"\x00\x01")
        with self.assertRaises(WebSocketClosed) as e:
            ws.recv()
        self.assertEqual(e.exception.code, 4014)
        ws.close()
        t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(got["path"], "/?v=10&encoding=json")
        self.assertEqual(got["hello"], "hello")
        self.assertEqual(got["pong"], (0xA, b"ping!"))

    def test_bad_handshake(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def run():
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            c.close()
            srv.close()

        threading.Thread(target=run, daemon=True).start()
        with self.assertRaisesRegex(ConnectionError, "403"):
            WebSocket.connect(f"ws://127.0.0.1:{srv.getsockname()[1]}/")


# ---------------------------------------------------------------- Discord

class FakeHub:
    def __init__(self):
        self.dispatched, self.resolved = [], []

    def dispatch(self, channel, inbound):
        self.dispatched.append(inbound)

    def resolve_approval(self, key, ok):
        self.resolved.append((key, ok))

    def log(self, m):
        pass

    def button(self, channel, data, *, user_id="", chat_id=""):
        return ChannelHub.button(self, channel, data, user_id=user_id, chat_id=chat_id)


def bot(**opts):
    ch = DiscordChannel(ChannelConfig("discord", owners=["42"], options={"token": "TOK", **opts}), **QUIET)
    ch.hub = FakeHub()
    ch.bot_id = "999"
    return ch


class DiscordTests(unittest.TestCase):
    def test_delivery_mode_is_unchanged(self):
        ch = DiscordChannel(ChannelConfig("discord", options={"webhook_url": "https://discord.com/api/webhooks/1/x"}), **QUIET)
        self.assertFalse(ch.supports_buttons or ch.supports_files)
        self.assertEqual(ch.owner_chat(), "webhook")
        with mock.patch.object(chmod, "_post_json", return_value=(204, {})) as p:
            ch.send("webhook", "hi")
        self.assertEqual(p.call_args[0][1]["content"], "hi")

    def test_messages_dm_guild_mention_bot_and_voice(self):
        ch = bot()
        msg = lambda **k: {"author": {"id": "42", "username": "me"}, "channel_id": "C1", "content": "", **k}
        ch._on_message(msg(content="halo"))                                           # DM
        ch._on_message(msg(content="talking to people", guild_id="G"))                # server, not mentioned
        ch._on_message(msg(content="<@999> cek ini", guild_id="G", mentions=[{"id": "999"}]))
        ch._on_message({"author": {"id": "7", "bot": True}, "channel_id": "C1", "content": "bot spam"})
        ch._on_message(msg(flags=1 << 13, attachments=[{"id": "A1", "filename": "voice-message.ogg", "content_type": "audio/ogg",
                                                         "url": "https://cdn.discordapp.com/a/1/voice.ogg", "duration_secs": 3.4}]))
        texts = [i.text for i in ch.hub.dispatched]
        self.assertEqual(texts[:2], ["halo", "cek ini"])
        voice = ch.hub.dispatched[2].attachments[0]
        self.assertEqual((voice["kind"], voice["duration"]), ("voice", 3))
        all_mode = bot(guild_mode="all")
        all_mode._on_message(msg(content="talking to people", guild_id="G"))
        self.assertEqual(len(all_mode.hub.dispatched), 1)

    def test_download_only_from_the_cdn_without_the_token(self):
        ch = bot()
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(RuntimeError, "CDN"):
            ch.download({"url": "https://evil.example/x", "name": "x"}, tmp)
        with mock.patch.object(chmod, "fetch_to", side_effect=lambda url, dest, headers=None, **k: (url, headers)):
            url, headers = ch.download({"url": "https://cdn.discordapp.com/attachments/1/2/a.pdf", "name": "a.pdf"}, tmp)
        self.assertIsNone(headers)

    def test_buttons_out_and_taps(self):
        ch = bot()
        calls = []
        ch._rest = lambda method, path, payload=None, **k: calls.append((method, path, payload)) or {}
        ch.send("C1", "Allow `ls`?", buttons=[("✅ Allow", "a:K"), ("❌ Deny", "d:K")])
        row = calls[0][2]["components"][0]["components"]
        self.assertEqual([(b["custom_id"], b["style"]) for b in row], [("a:K", 3), ("d:K", 4)])
        tap = lambda who: {"type": 3, "id": "I", "token": "T", "channel_id": "C1", "data": {"custom_id": "a:K"},
                           "member": {"user": {"id": who}}, "message": {"content": "Allow `ls`?"}}
        ch._on_button(tap("13"))
        self.assertEqual(ch.hub.resolved, [])
        self.assertEqual((calls[-1][2]["type"], calls[-1][2]["data"]["flags"]), (4, 64))
        ch._on_button(tap("42"))
        self.assertEqual(ch.hub.resolved, [("K", True)])
        self.assertEqual(calls[-1][1], "/interactions/I/T/callback")
        self.assertEqual(calls[-1][2]["type"], 7)
        self.assertEqual(calls[-1][2]["data"]["components"], [])
        self.assertTrue(calls[-1][2]["data"]["content"].endswith("✅ Allowed"))

    def test_send_file_and_owner_dm(self):
        ch = bot()
        f = Path(tempfile.mkdtemp()) / "laporan.xlsx"
        f.write_bytes(b"PK")
        with mock.patch.object(chmod, "_http", return_value=(200, b'{"id": "M"}')) as h:
            self.assertEqual(ch.send_file("C1", f, "ini"), "file")
            body = h.call_args.kwargs["data"]
            self.assertIn(b'name="payload_json"', body)
            self.assertIn(b'name="files[0]"; filename="laporan.xlsx"', body)
            self.assertIn("multipart/form-data", h.call_args.kwargs["headers"]["content-type"])
            self.assertEqual(h.call_args.kwargs["headers"]["authorization"], "Bot TOK")
        with mock.patch.object(chmod, "_http", return_value=(200, b'{"id": "DM7"}')):
            self.assertEqual(ch.owner_chat(), "DM7")

    def test_rate_limit_is_waited_out(self):
        ch = bot()
        answers = [(429, b'{"retry_after": 0.01}'), (200, b'{"ok": 1}')]
        with mock.patch.object(chmod, "_http", side_effect=lambda *a, **k: answers.pop(0)):
            self.assertEqual(ch._rest("POST", "/channels/C1/messages", {"content": "x"}), {"ok": 1})

    def test_gateway_identify_events_heartbeat_and_intent_fallback(self):
        seen = {}

        def first(p):
            p.send_json({"op": 10, "d": {"heartbeat_interval": 60}})
            seen["identify"] = json.loads(p.text())
            p.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S", "resume_gateway_url": "ws://unused",
                                                              "user": {"id": "999"}}})
            p.send_json({"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": {"author": {"id": "42"}, "channel_id": "C1",
                                                                       "content": "hai bot"}})
            beat = json.loads(p.text())
            seen["beat"] = beat
            p.send_json({"op": 11})
            p.close(4014)  # "disallowed intents": the bot has no MESSAGE CONTENT intent

        def second(p):
            p.send_json({"op": 10, "d": {"heartbeat_interval": 60000}})
            seen["identify2"] = json.loads(p.text())
            p.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S2", "user": {"id": "999"}}})
            time.sleep(0.3)

        url, t, errors = serve(lambda p: (first if "identify" not in seen else second)(p), connections=2)
        ch = bot()
        ch._rest = lambda method, path, payload=None, **k: {"url": url}
        th = threading.Thread(target=ch._gateway_loop, daemon=True)
        th.start()
        t.join(10)
        ch.stop()
        self.assertEqual(errors, [])
        self.assertEqual(seen["identify"]["op"], 2)
        self.assertEqual(seen["identify"]["d"]["token"], "TOK")
        self.assertEqual(seen["identify"]["d"]["intents"], DiscordChannel.INTENTS)
        self.assertEqual(seen["beat"]["op"], 1)
        self.assertIn(seen["beat"]["d"], (None, 1, 2), "the heartbeat carries the last sequence number seen")
        self.assertEqual(seen["identify2"]["d"]["intents"], DiscordChannel.INTENTS_NO_CONTENT)
        self.assertEqual([i.text for i in ch.hub.dispatched], ["hai bot"])


if __name__ == "__main__":
    unittest.main()
