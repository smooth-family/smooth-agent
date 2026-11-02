"""Voice notes: speech to text, voice replies, Telegram voice upload."""
import email.parser
import email.policy
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from smooth_agent import channels as chmod
from smooth_agent.agent import Turn
from smooth_agent.channels import Channel, ChannelConfig, ChannelHub, Inbound, TelegramChannel, unique_path
from smooth_agent.routers import Provider, Router
from smooth_agent.voice import VoiceError, VoiceSettings, audio_ext, speakable, transcribe

OGG = b"OggS" + b"\x00" * 200


def parse_multipart(ctype, body):
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"content-type: " + ctype.encode() + b"\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = (part.get_filename(), part.get_payload(decode=True))
    return out


class SpeechServer:
    """A fake OpenAI-compatible speech service: /audio/transcriptions and /audio/speech."""

    def __init__(self, fail_first=0):
        self.seen, self.fail_first, outer = [], fail_first, self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = self.rfile.read(n)
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(b'{"error":"busy"}')
                    return
                if self.path.endswith("/audio/transcriptions"):
                    parts = parse_multipart(self.headers.get("content-type", ""), body)
                    outer.seen.append({k: (v[0], v[1][:4]) for k, v in parts.items()})
                    data = json.dumps({"text": "  halo   bang, cek laporan  "}).encode()
                    ctype = "application/json"
                else:
                    outer.seen.append(json.loads(body))
                    data, ctype = b"RIFF" + b"\x00" * 300, "audio/wav"
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.end_headers()
                self.wfile.write(data)
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def router(self):
        return Router([Provider.from_config("speech", {"provider": "openai-compatible", "model": "gemini/gemini-3.6-flash",
                                                         "base_url": self.url, "key_env": "K"}, env={"K": "k"})])

    def close(self):
        self.srv.shutdown()


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "voice.ogg").write_bytes(OGG)

    def test_multipart_upload_and_clean_text(self):
        s = SpeechServer()
        try:
            text = transcribe(s.router(), self.d / "voice.ogg", VoiceSettings(stt_provider="speech", stt_language="id"))
        finally:
            s.close()
        self.assertEqual(text, "halo bang, cek laporan")
        sent = s.seen[0]
        self.assertEqual(sent["model"][1], b"gemi")      # provider's model used when [stt] model is not set
        self.assertEqual(sent["language"][1], b"id")
        self.assertEqual(sent["file"], ("voice.ogg", b"OggS"))

    def test_retries_gateway_errors(self):
        s = SpeechServer(fail_first=2)
        try:
            with mock.patch("smooth_agent.voice.time.sleep"):
                text = transcribe(s.router(), self.d / "voice.ogg", VoiceSettings(stt_provider="speech"))
        finally:
            s.close()
        self.assertEqual(text, "halo bang, cek laporan")

    def test_not_configured(self):
        with self.assertRaises(VoiceError) as cm:
            transcribe(Router([Provider.from_config("x", {"provider": "openai-compatible", "model": "m", "base_url": "http://127.0.0.1:9/v1"})]), self.d / "voice.ogg", VoiceSettings())
        self.assertIn("[stt]", str(cm.exception))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_video_note_is_converted_first(self):
        import subprocess
        mp4 = self.d / "note.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest", str(mp4)], check=True)
        s = SpeechServer()
        try:
            transcribe(s.router(), mp4, VoiceSettings(stt_provider="speech"))
        finally:
            s.close()
        self.assertEqual(s.seen[0]["file"][0], "speech.mp3")  # audio track only, as mp3


class TextTests(unittest.TestCase):
    def test_speakable(self):
        t = speakable("**Siap** bang. Cek `ls -la`:\n```bash\nrm -rf /\n```\nLihat https://smooth.family ya.")
        self.assertNotIn("rm -rf", t)
        self.assertNotIn("**", t)
        self.assertIn("(code in the message)", t)
        self.assertIn("(link in the message)", t)
        long = speakable("Kalimat satu. " * 200, limit=100)
        self.assertTrue(long.endswith("."))
        self.assertLessEqual(len(long), 100)

    def test_audio_ext(self):
        self.assertEqual(audio_ext(b"RIFF....WAVE", "audio/mpeg"), ".wav")  # trust the bytes over the header
        self.assertEqual(audio_ext(b"ID3\x04", ""), ".mp3")
        self.assertEqual(audio_ext(b"OggS", ""), ".ogg")


class VoiceChannel(Channel):
    kind = "fake"
    supports_voice = True

    def __init__(self, mode="auto"):
        super().__init__(ChannelConfig("fake", owners=["1"], options={"voice_replies": mode}), log=lambda m: None)
        self.sent, self.voices = [], []

    def send(self, chat_id, text, *, buttons=None):
        self.sent.append(text)

    def send_voice(self, chat_id, ogg, caption=""):
        self.voices.append(ogg.name)

    def download(self, att, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = unique_path(dest_dir, att["name"])
        p.write_bytes(OGG)
        return p


class HubVoiceTests(unittest.TestCase):
    def run_hub(self, mode, inbound, *, listen=True):
        ws = Path(tempfile.mkdtemp())
        seen = []

        class A:
            def run(self, text):
                seen.append(text)
                return Turn(text="Siap, laporan sudah dicek.", steps=1)
        ch = VoiceChannel(mode)
        hub = ChannelHub([ch], lambda *a: A(), log=lambda m: None, workspace_root=ws)
        if listen:
            hub.listen = lambda path: "tolong cek laporan penjualan"

        def speak(text, folder):
            p = folder / "reply.voice.ogg"
            p.write_bytes(OGG)
            return p
        hub.speak = speak
        hub.start()
        self.addCleanup(hub.stop)
        hub.dispatch(ch, inbound)
        deadline = time.time() + 5
        while not ch.sent and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        return seen, ch

    def voice(self, text=""):
        return Inbound(channel="fake", chat_id="1", user_id="1", text=text,
                       attachments=[{"kind": "voice", "file_id": "v", "name": "voice-5.ogg", "duration": 4}])

    def test_voice_note_becomes_the_request_and_gets_a_voice_reply(self):
        seen, ch = self.run_hub("auto", self.voice())
        self.assertTrue(seen[0].startswith("tolong cek laporan penjualan"))
        self.assertIn("voice note", seen[0])
        self.assertEqual(ch.sent, ["Siap, laporan sudah dicek."])  # text always comes
        self.assertEqual(ch.voices, ["reply.voice.ogg"])           # plus the same answer as a voice note

    def test_voice_with_caption_keeps_the_caption_as_request(self):
        seen, ch = self.run_hub("auto", self.voice("ringkas ini"))
        self.assertTrue(seen[0].startswith("ringkas ini"))
        self.assertIn("transcript: «tolong cek laporan penjualan»", seen[0])

    def test_reply_modes(self):
        _, ch = self.run_hub("off", self.voice())
        self.assertEqual(ch.voices, [])
        _, ch = self.run_hub("auto", Inbound(channel="fake", chat_id="1", user_id="1", text="halo"))
        self.assertEqual(ch.voices, [])  # typed message: typed answer only
        _, ch = self.run_hub("always", Inbound(channel="fake", chat_id="1", user_id="1", text="halo"))
        self.assertEqual(ch.voices, ["reply.voice.ogg"])

    def test_without_stt_the_agent_is_told(self):
        seen, ch = self.run_hub("auto", self.voice(), listen=False)
        self.assertIn("could not be transcribed (speech to text is not set up", seen[0])


class TelegramVoiceUploadTests(unittest.TestCase):
    def test_send_voice_multipart(self):
        got = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                got["path"] = self.path
                got["parts"] = parse_multipart(self.headers.get("content-type", ""), self.rfile.read(n))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true, "result": {}}')
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(chmod, "TELEGRAM_API", f"http://127.0.0.1:{srv.server_address[1]}"):
                tg = TelegramChannel(ChannelConfig("telegram", owners=["1"], options={"token": "t"}), log=lambda m: None)
                ogg = Path(tempfile.mkdtemp()) / "r.ogg"
                ogg.write_bytes(OGG)
                tg.send_voice("42", ogg)
        finally:
            srv.shutdown()
        self.assertTrue(got["path"].endswith("/bott/sendVoice"))
        self.assertEqual(got["parts"]["chat_id"][1], b"42")
        self.assertEqual(got["parts"]["voice"][0], "r.ogg")
        self.assertTrue(got["parts"]["voice"][1].startswith(b"OggS"))


if __name__ == "__main__":
    unittest.main()
