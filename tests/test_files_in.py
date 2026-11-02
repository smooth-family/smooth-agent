"""Files the owner sends in chat: Telegram parsing, albums, download, and what the agent is told."""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent import channels as chmod
from smooth_agent.agent import Turn
from smooth_agent.channels import (Channel, ChannelConfig, ChannelHub, Inbound, TelegramChannel, safe_filename,
                                    telegram_attachments, unique_path)

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63f8cfc0f01f0005000201a2e1c1950000000049454e44ae426082")


class ParseTests(unittest.TestCase):
    def test_photo_document_voice(self):
        msg = {"message_id": 7, "caption": "cek ini",
               "photo": [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 900}]}
        att = telegram_attachments(msg)
        self.assertEqual(att, [{"kind": "photo", "file_id": "big", "name": "photo-7.jpg", "size": 900, "mime": "image/jpeg"}])
        att = telegram_attachments({"message_id": 8, "document": {"file_id": "d", "file_name": "Laporan Q3.xlsx",
                                                                   "file_size": 5, "mime_type": "application/vnd.ms-excel"}})
        self.assertEqual((att[0]["kind"], att[0]["name"]), ("document", "Laporan Q3.xlsx"))
        att = telegram_attachments({"message_id": 9, "voice": {"file_id": "v", "duration": 4}})
        self.assertEqual((att[0]["name"], att[0]["duration"]), ("voice-9.ogg", 4))
        self.assertEqual(telegram_attachments({"message_id": 1, "text": "hi"}), [])

    def test_names(self):
        self.assertEqual(safe_filename("../../.ssh/authorized_keys"), "authorized_keys")
        d = Path(tempfile.mkdtemp())
        (d / "a.pdf").write_text("x")
        self.assertEqual(unique_path(d, "a.pdf").name, "a-1.pdf")


class FakeHub:
    def __init__(self):
        self.got = []

    def dispatch(self, channel, inbound):
        self.got.append(inbound)

    def resolve_approval(self, key, ok):
        pass


class AlbumTests(unittest.TestCase):
    def test_album_becomes_one_message(self):
        tg = TelegramChannel(ChannelConfig("telegram", owners=["1"], options={"token": "t"}), log=lambda m: None)
        tg._api = lambda *a, **k: {"ok": True}
        tg.ALBUM_WAIT = 0.2
        tg.hub = FakeHub()
        base = {"chat": {"id": 1}, "from": {"id": 1}, "media_group_id": "g1"}
        for i in range(3):
            msg = dict(base, message_id=i, photo=[{"file_id": f"p{i}"}], **({"caption": "tiga foto"} if i == 1 else {}))
            tg._dispatch({"message": msg})
        tg._dispatch({"message": {"chat": {"id": 1}, "from": {"id": 1}, "message_id": 9, "text": "separate"}})
        deadline = time.time() + 3
        while len(tg.hub.got) < 2 and time.time() < deadline:
            time.sleep(0.02)
        album = [m for m in tg.hub.got if m.attachments][0]
        self.assertEqual([a["file_id"] for a in album.attachments], ["p0", "p1", "p2"])
        self.assertEqual(album.text, "tiga foto")
        self.assertEqual(len(tg.hub.got), 2)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        files = {"photos/file_1.jpg": PNG}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # Bot API methods
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                reply = {"ok": True, "result": {"file_path": "photos/file_1.jpg"}} if self.path.endswith("/getFile") \
                    and body.get("file_id") == "F1" else {"ok": False, "description": "file not found"}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # the file host
                key = self.path.split("/file/bott/", 1)[-1]
                data = files.get(key)
                self.send_response(200 if data else 404)
                self.end_headers()
                self.wfile.write(data or b"")
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.api = mock.patch.object(chmod, "TELEGRAM_API", f"http://127.0.0.1:{self.srv.server_address[1]}")
        self.api.start()
        self.tg = TelegramChannel(ChannelConfig("telegram", owners=["1"], options={"token": "t"}), log=lambda m: None)

    def tearDown(self):
        self.api.stop()
        self.srv.shutdown()

    def test_download_saves_file(self):
        d = Path(tempfile.mkdtemp()) / "inbox"
        p = self.tg.download({"file_id": "F1", "name": "../evil/foto.jpg", "size": len(PNG)}, d)
        self.assertEqual(p, d / "foto.jpg")
        self.assertEqual(p.read_bytes(), PNG)
        p2 = self.tg.download({"file_id": "F1", "name": "foto.jpg"}, d)
        self.assertEqual(p2.name, "foto-1.jpg")  # never overwrites

    def test_errors(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(RuntimeError) as cm:
            self.tg.download({"file_id": "F1", "name": "big.zip", "size": 50 * 1024 * 1024}, d)
        self.assertIn("20 MB", str(cm.exception))
        with self.assertRaises(RuntimeError):
            self.tg.download({"file_id": "missing", "name": "x"}, d)


class FileChannel(Channel):
    kind = "fake"

    def __init__(self, payloads):
        super().__init__(ChannelConfig("fake", owners=["1"]), log=lambda m: None)
        self.payloads = payloads
        self.sent = []

    def send(self, chat_id, text, *, buttons=None):
        self.sent.append(text)

    def download(self, att, dest_dir):
        if att["file_id"] == "broken":
            raise RuntimeError("network down")
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = unique_path(dest_dir, safe_filename(att["name"]))
        p.write_bytes(self.payloads[att["file_id"]])
        return p


class HubFileTests(unittest.TestCase):
    def test_agent_is_told_what_arrived(self):
        ws = Path(tempfile.mkdtemp())
        seen = []

        class A:
            def run(self, text):
                seen.append(text)
                return Turn(text="ok", steps=1)
        ch = FileChannel({"img": PNG, "pdf": b"%PDF-1.4 ...", "zip": b"PK\x03\x04rest", "voice": b"OggS"})
        hub = ChannelHub([ch], lambda *a: A(), log=lambda m: None, workspace_root=ws)
        hub.start()
        self.addCleanup(hub.stop)
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", user_id="1", text="", attachments=[
            {"kind": "photo", "file_id": "img", "name": "photo-1.jpg"},
            {"kind": "document", "file_id": "pdf", "name": "invoice.pdf"},
            {"kind": "document", "file_id": "zip", "name": "data.zip"},
            {"kind": "voice", "file_id": "voice", "name": "voice-2.ogg", "duration": 3},
            {"kind": "document", "file_id": "broken", "name": "lost.docx"}]))
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        text = seen[0]
        today = time.strftime("%Y-%m-%d")
        self.assertIn("without a message", text)
        self.assertIn(f"inbox/{today}/photo-1.jpg", text)
        self.assertIn("vision_analyze", text)
        self.assertIn(f"inbox/{today}/invoice.pdf", text)
        self.assertIn("read_file converts it", text)
        self.assertIn("extract_archive", text)
        self.assertIn("voice file, 3 s", text)
        self.assertIn("lost.docx: could not be downloaded (network down)", text)
        self.assertTrue((ws / "inbox" / today / "invoice.pdf").exists())

    def test_caption_is_the_request(self):
        ws = Path(tempfile.mkdtemp())
        seen = []

        class A:
            def run(self, text):
                seen.append(text)
                return Turn(text="ok", steps=1)
        ch = FileChannel({"img": PNG})
        hub = ChannelHub([ch], lambda *a: A(), log=lambda m: None, workspace_root=ws)
        hub.start()
        self.addCleanup(hub.stop)
        hub.dispatch(ch, Inbound(channel="fake", chat_id="1", user_id="1", text="ini struk apa?",
                                 attachments=[{"kind": "photo", "file_id": "img", "name": "p.jpg"}]))
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(seen[0].startswith("ini struk apa?"))


if __name__ == "__main__":
    unittest.main()
