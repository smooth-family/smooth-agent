"""Stage 6: channel parity — files and voice in/out, approval buttons on WhatsApp, Slack, Matrix and LINE."""
import base64
import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent import channels as chmod
from smooth_agent.channels import (ChannelConfig, ChannelHub, Inbound, LineChannel, MatrixChannel, SlackChannel,
                                   WhatsAppChannel, fetch_to, file_kind, multipart)

QUIET = dict(log=lambda m: None)


class FakeHub:
    """Just enough of ChannelHub for a channel's parsing code (button() is the real one)."""

    def __init__(self):
        self.dispatched, self.resolved = [], []
        self._shared = {}
        self._webhook = object()

    def dispatch(self, channel, inbound):
        self.dispatched.append(inbound)

    def resolve_approval(self, key, ok):
        self.resolved.append((key, ok))

    def log(self, msg):
        pass

    def button(self, channel, data, *, user_id="", chat_id=""):
        return ChannelHub.button(self, channel, data, user_id=user_id, chat_id=chat_id)

    SHARE_TTL = ChannelHub.SHARE_TTL

    def share_file(self, path, ttl=None):
        return ChannelHub.share_file(self, path, ttl)

    def shared(self, token):
        return ChannelHub.shared(self, token)


# ---------------------------------------------------------------- helpers

class _Files(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/file":
            body = b"hello file"
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
        elif self.path == "/big":
            body = b"x" * 2000
            self.send_response(200)
        elif self.path == "/html":
            body = b"<html>login</html>"
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("location", f"http://localhost:{port}/auth")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        elif self.path == "/auth":
            body = (self.headers.get("authorization") or "none").encode()
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Files)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_fetch_ok_cap_and_html(self):
        p = fetch_to(self.base + "/file", self.tmp / "a.bin")
        self.assertEqual(p.read_bytes(), b"hello file")
        with self.assertRaisesRegex(RuntimeError, "limit"):
            fetch_to(self.base + "/big", self.tmp / "b.bin", max_bytes=1000)
        self.assertFalse((self.tmp / "b.bin").exists(), "a refused download leaves no partial file")
        with self.assertRaisesRegex(RuntimeError, "web page"):
            fetch_to(self.base + "/html", self.tmp / "c.bin")
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            fetch_to(self.base + "/nope", self.tmp / "d.bin")
        with self.assertRaisesRegex(RuntimeError, "not a web URL"):
            fetch_to("file:///etc/passwd", self.tmp / "e.bin")

    def test_token_is_not_sent_to_another_host_on_redirect(self):
        same = fetch_to(self.base + "/auth", self.tmp / "same", headers={"authorization": "Bearer SECRET"})
        self.assertEqual(same.read_bytes(), b"Bearer SECRET")
        other = fetch_to(self.base + "/redirect", self.tmp / "other", headers={"authorization": "Bearer SECRET"})
        self.assertEqual(other.read_bytes(), b"none")

    def test_multipart_and_kinds(self):
        body, ctype = multipart({"a": "1", "skip": ""}, {"file": ('we"ird.txt', "text/plain", b"DATA")})
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b'name="a"\r\n\r\n1', body)
        self.assertNotIn(b'name="skip"', body)
        self.assertIn(b'filename="we_ird.txt"', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertEqual(file_kind(Path("x.PNG"))[0], "image")
        self.assertEqual(file_kind(Path("x.ogg")), ("voice", "audio/ogg"))
        self.assertEqual(file_kind(Path("x.mp3"))[0], "audio")
        self.assertEqual(file_kind(Path("x.mp4"))[0], "video")
        self.assertEqual(file_kind(Path("x.xlsx"))[0], "document")


# ---------------------------------------------------------------- WhatsApp

class WhatsAppTests(unittest.TestCase):
    def _ch(self, **opts):
        ch = WhatsAppChannel(ChannelConfig("whatsapp", owners=["628"], options={"token": "T", "phone_id": "P", **opts}), **QUIET)
        ch.hub = FakeHub()
        return ch

    def _post(self, ch, *messages, headers=None):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}).encode()
        return ch.handle_webhook("POST", "/webhook/whatsapp", headers or {}, body)

    def test_media_voice_and_location_in(self):
        ch = self._ch()
        self._post(ch, {"from": "628", "type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
                   {"from": "628", "type": "document", "document": {"id": "D1", "filename": "laporan.pdf", "caption": "cek ini"}},
                   {"from": "628", "type": "location", "location": {"latitude": -6.2, "longitude": 106.8, "name": "Kantor"}})
        voice, doc, loc = ch.hub.dispatched
        self.assertEqual(voice.attachments[0]["kind"], "voice")
        self.assertEqual((doc.text, doc.attachments[0]["name"], doc.attachments[0]["kind"]), ("cek ini", "laporan.pdf", "document"))
        self.assertIn("-6.2,106.8", loc.text)

    def test_button_tap_only_from_owner(self):
        ch = self._ch()
        tap = lambda who: {"from": who, "type": "interactive",
                           "interactive": {"type": "button_reply", "button_reply": {"id": "a:K1", "title": "✅ Allow"}}}
        self._post(ch, tap("999"))
        self.assertEqual(ch.hub.resolved, [])
        self._post(ch, tap("628"))
        self.assertEqual(ch.hub.resolved, [("K1", True)])
        self.assertEqual(ch.hub.dispatched, [], "a button tap is not a chat message")

    def test_signature_when_app_secret_is_set(self):
        ch = self._ch(app_secret="S")
        body = json.dumps({"entry": []}).encode()
        self.assertEqual(ch.handle_webhook("POST", "/webhook/whatsapp", {}, body)[0], 401)
        sig = "sha256=" + hmac.new(b"S", body, hashlib.sha256).hexdigest()
        self.assertEqual(ch.handle_webhook("POST", "/webhook/whatsapp", {"X-Hub-Signature-256": sig}, body)[0], 200)

    def test_send_with_buttons(self):
        ch = self._ch()
        sent = []
        with mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: sent.append(p) or (200, {})):
            ch.send("628", "Run `ls`?", buttons=[("✅ Allow", "a:K"), ("❌ Deny", "d:K")])
        self.assertEqual(len(sent), 1)
        inter = sent[0]["interactive"]
        self.assertEqual(sent[0]["type"], "interactive")
        self.assertEqual(inter["body"]["text"], "Run `ls`?")
        self.assertEqual([b["reply"]["id"] for b in inter["action"]["buttons"]], ["a:K", "d:K"])

    def test_download_and_send_file(self):
        ch = self._ch()
        tmp = Path(tempfile.mkdtemp())
        lookups = []

        def fake_http(method, url, **k):
            lookups.append((method, url))
            if url.endswith("/media"):
                return 200, b'{"id": "UP1"}'
            return 200, b'{"url": "https://lookaside.example/x", "mime_type": "audio/ogg"}'

        def fake_fetch(url, dest, headers=None, **k):
            dest.write_bytes(b"OGG")
            self.assertEqual(headers["authorization"], "Bearer T")
            return dest

        with mock.patch.object(chmod, "_http", side_effect=fake_http), mock.patch.object(chmod, "fetch_to", side_effect=fake_fetch):
            p = ch.download({"kind": "voice", "file_id": "A1", "name": "", "mime": "audio/ogg"}, tmp)
        self.assertEqual(p.suffix, ".ogg")
        self.assertEqual(p.read_bytes(), b"OGG")

        doc = tmp / "report.xlsx"
        doc.write_bytes(b"PK")
        sent = []
        with mock.patch.object(chmod, "_http", side_effect=fake_http), \
                mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: sent.append(p) or (200, {})):
            how = ch.send_file("628", doc, "hasil")
        self.assertEqual(how, "document")
        self.assertEqual(sent[0]["type"], "document")
        self.assertEqual(sent[0]["document"], {"id": "UP1", "caption": "hasil", "filename": "report.xlsx"})


# ---------------------------------------------------------------- Slack

class SlackTests(unittest.TestCase):
    def _ch(self):
        ch = SlackChannel(ChannelConfig("slack", owners=["U1"], options={"token": "xoxb", "signing_secret": "sec"}), **QUIET)
        ch.hub = FakeHub()
        return ch

    def _signed(self, ch, body: bytes, ctype="application/json"):
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(b"sec", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
        return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": ctype}

    def test_file_share_and_voice_clip(self):
        ch = self._ch()
        body = json.dumps({"event": {"type": "message", "subtype": "file_share", "channel": "D1", "user": "U1", "text": "",
                                     "files": [{"id": "F1", "name": "audio_message.webm", "mimetype": "audio/webm",
                                                "subtype": "slack_audio", "url_private_download": "https://files.slack.com/F1",
                                                "duration_ms": 4200},
                                               {"id": "F2", "name": "a.png", "mimetype": "image/png",
                                                "url_private_download": "https://files.slack.com/F2"}]}}).encode()
        self.assertEqual(ch.handle_webhook("POST", "/webhook/slack", self._signed(ch, body), body)[0], 200)
        (inb,) = ch.hub.dispatched
        self.assertEqual([a["kind"] for a in inb.attachments], ["voice", "photo"])
        self.assertEqual(inb.attachments[0]["duration"], 4)
        with mock.patch.object(chmod, "fetch_to", side_effect=lambda url, dest, headers=None, **k: (url, headers)):
            url, headers = ch.download(inb.attachments[1], Path(tempfile.mkdtemp()))
        self.assertEqual((url, headers["authorization"]), ("https://files.slack.com/F2", "Bearer xoxb"))

    def test_buttons_out_and_tap_in(self):
        ch = self._ch()
        sent = []
        with mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: sent.append((url, p)) or (200, {"ok": True})):
            ch.send("D1", "Allow `rm x`?", buttons=[("✅ Allow", "a:K"), ("❌ Deny", "d:K")])
            actions = sent[0][1]["blocks"][1]["elements"]
            self.assertEqual([(a["value"], a["style"]) for a in actions], [("a:K", "primary"), ("d:K", "danger")])
            for who, n in (("U9", 0), ("U1", 1)):
                payload = {"type": "block_actions", "user": {"id": who}, "channel": {"id": "D1"},
                           "actions": [{"value": "a:K"}], "response_url": "https://hooks.slack.com/r"}
                body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
                st, _ = ch.handle_webhook("POST", "/webhook/slack",
                                          self._signed(ch, body, "application/x-www-form-urlencoded"), body)
                self.assertEqual(st, 200)
                self.assertEqual(len(ch.hub.resolved), n)
        self.assertEqual(ch.hub.resolved, [("K", True)])
        self.assertEqual(sent[-1], ("https://hooks.slack.com/r", {"replace_original": True, "text": "✅ Allowed"}))

    def test_send_file_three_steps(self):
        ch = self._ch()
        f = Path(tempfile.mkdtemp()) / "chart.png"
        f.write_bytes(b"PNG")
        calls = []

        def fake_http(method, url, **k):
            calls.append(url)
            if "getUploadURLExternal" in url:
                return 200, b'{"ok": true, "upload_url": "https://files.slack.com/upload/x", "file_id": "F9"}'
            return 200, b"OK"

        posted = []
        with mock.patch.object(chmod, "_http", side_effect=fake_http), \
                mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: posted.append((url, p)) or (200, {"ok": True})):
            self.assertEqual(ch.send_file("D1", f, "grafik"), "image")
        self.assertEqual(calls[1], "https://files.slack.com/upload/x")
        self.assertTrue(posted[0][0].endswith("files.completeUploadExternal"))
        self.assertEqual(posted[0][1]["files"], [{"id": "F9", "title": "chart.png"}])
        self.assertEqual(posted[0][1]["initial_comment"], "grafik")


# ---------------------------------------------------------------- Matrix

class MatrixTests(unittest.TestCase):
    def _ch(self):
        ch = MatrixChannel(ChannelConfig("matrix", owners=["@me:hs"], options={"token": "t", "user_id": "@bot:hs"}), **QUIET)
        ch.hub = FakeHub()
        self.calls = []
        ch._req = lambda method, path, payload=None, **k: self.calls.append((method, path, payload)) or {"event_id": f"$e{len(self.calls)}"}
        return ch

    def _sync(self, ch, *events):
        ch._handle_sync({"rooms": {"join": {"!r:hs": {"timeline": {"events": list(events)}}}}})

    def test_media_in(self):
        ch = self._ch()
        self._sync(ch,
                   {"type": "m.room.message", "sender": "@me:hs", "content": {
                       "msgtype": "m.audio", "body": "Voice message", "url": "mxc://hs/V1",
                       "info": {"mimetype": "audio/ogg", "duration": 3000}, "org.matrix.msc3245.voice": {}}},
                   {"type": "m.room.message", "sender": "@me:hs", "content": {
                       "msgtype": "m.file", "body": "baca ini ya", "filename": "doc.pdf", "url": "mxc://hs/D1"}},
                   {"type": "m.room.message", "sender": "@me:hs", "content": {
                       "msgtype": "m.image", "body": "secret.png", "file": {"url": "mxc://hs/E1", "key": {}}}})
        voice, doc, enc = ch.hub.dispatched
        self.assertEqual((voice.attachments[0]["kind"], voice.attachments[0]["duration"]), ("voice", 3))
        self.assertEqual((doc.text, doc.attachments[0]["name"]), ("baca ini ya", "doc.pdf"))
        self.assertTrue(enc.attachments[0]["encrypted"])
        with self.assertRaisesRegex(RuntimeError, "end-to-end encrypted"):
            ch.download(enc.attachments[0], Path(tempfile.mkdtemp()))

    def test_download_tries_authenticated_media_first(self):
        ch = self._ch()
        urls = []

        def fake_fetch(url, dest, headers=None, **k):
            urls.append(url)
            if "/client/v1/" in url:
                raise RuntimeError("download failed: HTTP 404")
            dest.write_bytes(b"x")
            return dest

        with mock.patch.object(chmod, "fetch_to", side_effect=fake_fetch):
            ch.download({"file_id": "mxc://hs/ABC", "name": "a.txt"}, Path(tempfile.mkdtemp()))
        self.assertEqual(len(urls), 2)
        self.assertIn("/_matrix/client/v1/media/download/hs/ABC", urls[0])
        self.assertIn("/_matrix/media/v3/download/hs/ABC", urls[1])

    def test_reaction_approval(self):
        ch = self._ch()
        ch.send("!r:hs", "Allow?", buttons=[("✅ Allow", "a:K"), ("❌ Deny", "d:K")])
        reacts = [c for c in self.calls if "/send/m.reaction/" in c[1]]
        self.assertEqual([r[2]["m.relates_to"]["key"] for r in reacts], ["✅", "❌"])
        prompt = reacts[0][2]["m.relates_to"]["event_id"]
        react = lambda who, key: {"type": "m.reaction", "sender": who,
                                  "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": prompt, "key": key}}}
        self._sync(ch, react("@bot:hs", "✅"), react("@x:hs", "✅"))  # its own reaction and a stranger's
        self.assertEqual(ch.hub.resolved, [])
        self._sync(ch, react("@me:hs", "❌"))
        self.assertEqual(ch.hub.resolved, [("K", False)])

    def test_send_file_uploads_then_posts(self):
        ch = self._ch()
        f = Path(tempfile.mkdtemp()) / "reply.ogg"
        f.write_bytes(b"OGG")
        with mock.patch.object(chmod, "_http", return_value=(200, b'{"content_uri": "mxc://hs/U1"}')) as up:
            self.assertEqual(ch.send_file("!r:hs", f), "voice message")
        self.assertIn("/_matrix/media/v3/upload?filename=reply.ogg", up.call_args[0][1])
        content = self.calls[-1][2]
        self.assertEqual((content["msgtype"], content["url"]), ("m.audio", "mxc://hs/U1"))
        self.assertIn("org.matrix.msc3245.voice", content)


# ---------------------------------------------------------------- LINE

class LineTests(unittest.TestCase):
    def _ch(self, **opts):
        ch = LineChannel(ChannelConfig("line", owners=["Uown"], options={"token": "T", "secret": "S", **opts}), **QUIET)
        ch.hub = FakeHub()
        return ch

    def _post(self, ch, *events):
        body = json.dumps({"events": list(events)}).encode()
        sig = base64.b64encode(hmac.new(b"S", body, hashlib.sha256).digest()).decode()
        return ch.handle_webhook("POST", "/webhook/line", {"X-Line-Signature": sig}, body)

    def test_media_and_postback(self):
        ch = self._ch()
        self._post(ch,
                   {"type": "message", "source": {"userId": "Uown"},
                    "message": {"type": "audio", "id": "111", "duration": 5000, "contentProvider": {"type": "line"}}},
                   {"type": "message", "source": {"userId": "Uown"},
                    "message": {"type": "file", "id": "222", "fileName": "data.csv", "fileSize": 10}},
                   {"type": "postback", "source": {"userId": "Uother"}, "postback": {"data": "a:K"}},
                   {"type": "postback", "source": {"userId": "Uown"}, "postback": {"data": "d:K"}})
        audio, f = ch.hub.dispatched
        self.assertEqual((audio.attachments[0]["kind"], audio.attachments[0]["duration"]), ("audio", 5))
        self.assertEqual(f.attachments[0]["name"], "data.csv")
        self.assertEqual(ch.hub.resolved, [("K", False)])
        with mock.patch.object(chmod, "fetch_to", side_effect=lambda url, dest, headers=None, **k: url):
            url = ch.download(audio.attachments[0], Path(tempfile.mkdtemp()))
        self.assertEqual(url, "https://api-data.line.me/v2/bot/message/111/content")

    def test_quick_reply_buttons(self):
        ch = self._ch()
        sent = []
        with mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: sent.append(p) or (200, {})):
            ch.send("Uown", "Allow?", buttons=[("✅ Allow", "a:K"), ("❌ Deny", "d:K")])
        items = sent[0]["messages"][-1]["quickReply"]["items"]
        self.assertEqual([i["action"]["data"] for i in items], ["a:K", "d:K"])

    def test_files_out_need_public_url_and_are_served_once_shared(self):
        self.assertFalse(self._ch().supports_files)
        with self.assertRaisesRegex(RuntimeError, "public_url"):
            self._ch().send_file("Uown", Path(__file__))
        ch = self._ch(public_url="https://agent.example")
        self.assertTrue(ch.supports_files)
        img = Path(tempfile.mkdtemp()) / "p.png"
        img.write_bytes(b"PNG")
        sent = []
        with mock.patch.object(chmod, "_post_json", side_effect=lambda url, p, **k: sent.append(p) or (200, {})):
            self.assertEqual(ch.send_file("Uown", img), "image")
            self.assertEqual(ch.send_file("Uown", Path(__file__)), "download link")
        url = sent[0]["messages"][0]["originalContentUrl"]
        self.assertTrue(url.startswith("https://agent.example/media/"))
        token = url.split("/")[4]
        self.assertEqual(ch.hub.shared(token), img)
        self.assertIsNone(ch.hub.shared("nope"))
        ch.hub._shared[token] = (img, time.time() - 1)
        self.assertIsNone(ch.hub.shared(token), "an expired link serves nothing")


# ---------------------------------------------------------------- hub: media route + typed answers on button channels

class HubTests(unittest.TestCase):
    def test_media_route_serves_shared_files_only(self):
        hub = ChannelHub([], lambda *a: None, webhook_host="127.0.0.1", webhook_port=0, **QUIET)
        hub._webhook = chmod.WebhookServer("127.0.0.1", 0, {}, media=hub.shared, **QUIET)
        hub._webhook.start()
        try:
            port = hub._webhook._srv.server_address[1]
            f = Path(tempfile.mkdtemp()) / "a.txt"
            f.write_text("shared!")
            link = hub.share_file(f)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{link}", timeout=5) as r:
                self.assertEqual(r.read(), b"shared!")
            for bad in ("/media/deadbeef/a.txt", "/media/", "/media/../etc/passwd"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}", timeout=5)
                self.assertEqual(e.exception.code, 404)
        finally:
            hub._webhook.stop()

    def test_typed_yes_works_on_a_button_channel(self):
        class Btn(chmod.Channel):
            kind = "fake"
            supports_buttons = True

            def __init__(self):
                super().__init__(ChannelConfig("fake", owners=["1"]), **QUIET)
                self.sent = []

            def send(self, chat_id, text, *, buttons=None):
                self.sent.append((text, buttons))

        ch = Btn()
        hub = ChannelHub([ch], lambda *a: None, approval_timeout=5, **QUIET)
        hub._active = (ch, "1")
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("ok", hub.approve("terminal", {"command": "ls"})))
        t.start()
        for _ in range(100):
            if ch.sent:
                break
            time.sleep(0.02)
        self.assertTrue(ch.sent[0][1], "buttons were offered")
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", user_id="1", text="gas"))
        t.join(5)
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
