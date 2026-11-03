"""Voice: speech to text for the voice notes people send, text to speech for voice replies.

Speech to text uses the OpenAI-compatible `/audio/transcriptions` endpoint, so any of
these work: OpenAI (whisper-1, gpt-4o-transcribe), Groq (whisper-large-v3), a local
whisper server, or a gateway such as 9router (e.g. model gemini/gemini-3.6-flash).
Text to speech uses `/audio/speech` (OpenAI tts-1, gemini/...-tts through a gateway, …).

    [stt]
    provider = "main"                  # a provider from [providers] (its URL and key are used)
    model = "whisper-1"                # default: the provider's model
    # language = "id"                  # optional hint

    [tts]
    provider = "main"
    model = "tts-1"
    voice = "alloy"

ffmpeg (when installed) converts what the endpoint cannot take — video notes, odd
formats — and turns speech into Telegram's OGG/Opus voice format.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .routers import Router, RouterError

AUDIO_EXT = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".aac", ".mp4", ".mpeg", ".mpga", ".mov"}
DIRECT_EXT = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".mpeg", ".mpga"}  # sent as is
MAX_UPLOAD = 25 * 1024 * 1024
MIME = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".wav": "audio/wav", ".flac": "audio/flac", ".webm": "audio/webm", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg"}


class VoiceError(Exception):
    pass


@dataclass
class VoiceSettings:
    stt_provider: str | None = None
    stt_model: str | None = None
    stt_language: str | None = None
    tts_provider: str | None = None
    tts_model: str | None = None
    tts_voice: str = "alloy"

    @property
    def can_listen(self) -> bool:
        return bool(self.stt_provider)

    @property
    def can_speak(self) -> bool:
        return bool(self.tts_provider and self.tts_model)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "VoiceSettings":
        s, t = cfg.get("stt") or {}, cfg.get("tts") or {}
        return cls(stt_provider=s.get("provider"), stt_model=s.get("model"), stt_language=s.get("language"),
                   tts_provider=t.get("provider"), tts_model=t.get("model"), tts_voice=str(t.get("voice", "alloy")))


def ffmpeg_ok() -> bool:
    return bool(shutil.which("ffmpeg"))


def duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def to_mp3(src: Path, dest: Path) -> Path:
    """Audio track of anything ffmpeg reads -> small mono MP3 (good for speech recognition)."""
    if not ffmpeg_ok():
        raise VoiceError("ffmpeg is needed to convert this file (install ffmpeg)")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", "48k", str(dest)], capture_output=True, timeout=300)
    if r.returncode or not dest.exists():
        raise VoiceError(f"ffmpeg could not read the audio: {r.stderr.decode(errors='replace')[-200:]}")
    return dest


def to_voice_ogg(src: Path, dest: Path) -> Path:
    """Any audio -> OGG/Opus, the format Telegram shows as a voice note."""
    if not ffmpeg_ok():
        raise VoiceError("ffmpeg is needed to make a voice note")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-c:a", "libopus", "-b:a", "32k",
                        "-ac", "1", str(dest)], capture_output=True, timeout=300)
    if r.returncode or not dest.exists():
        raise VoiceError(f"ffmpeg could not make the voice note: {r.stderr.decode(errors='replace')[-200:]}")
    return dest


def to_m4a(src: Path, dest: Path) -> Path:
    """Any audio -> AAC in .m4a (the only audio LINE plays)."""
    if not ffmpeg_ok():
        raise VoiceError("ffmpeg is needed to convert the audio (install ffmpeg)")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "64k",
                        str(dest)], capture_output=True, timeout=300)
    if r.returncode or not dest.exists():
        raise VoiceError(f"ffmpeg could not convert the audio: {r.stderr.decode(errors='replace')[-200:]}")
    return dest


def _multipart(fields: dict[str, str], name: str, data: bytes, mime: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n" + f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def _post(url: str, headers: dict[str, str], data: bytes, timeout: float, tries: int = 3) -> bytes:
    """POST with retries on rate limits and gateway errors (speech endpoints behind gateways hiccup)."""
    last = ""
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (configured provider)
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in (408, 429, 500, 502, 503, 504) or attempt == tries - 1:
                raise VoiceError(last) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            if attempt == tries - 1:
                raise VoiceError(f"could not reach the speech service: {e}") from None
        time.sleep(2 * (attempt + 1))
    raise VoiceError(last)


def _provider(router: Router, name: str | None):
    try:
        p = router.provider(name)
    except (RouterError, KeyError) as e:
        raise VoiceError(f"no provider '{name}' for speech: {e}") from None
    if p.wire != "openai":
        raise VoiceError(f"provider '{p.name}' is not OpenAI-compatible; speech needs /audio endpoints")
    if not p.ready:
        raise VoiceError(f"provider '{p.name}' has no key ({p.key_env or 'n/a'})")
    return p


def transcribe(router: Router, path: Path, vs: VoiceSettings) -> str:
    """Speech in an audio (or video) file -> text."""
    if not vs.can_listen:
        raise VoiceError("speech to text is not set up: add [stt] provider (and model) to the config")
    from .routers import _auth_headers
    p = _provider(router, vs.stt_provider)
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        src = path
        if path.suffix.lower() not in DIRECT_EXT or path.stat().st_size > MAX_UPLOAD:
            src = to_mp3(path, Path(tmp) / "speech.mp3")  # video notes, odd formats, very long recordings
        if src.stat().st_size > MAX_UPLOAD:
            raise VoiceError("the recording is too long to transcribe in one go (over 25 MB even as MP3)")
        fields = {"model": vs.stt_model or p.model, "response_format": "json"}
        if vs.stt_language:
            fields["language"] = vs.stt_language
        body, ctype = _multipart(fields, src.name, src.read_bytes(), MIME.get(src.suffix.lower(), "application/octet-stream"))
        headers = {k: v for k, v in _auth_headers(p).items() if k.lower() != "content-type"}
        headers["content-type"] = ctype
        try:
            raw = _post(f"{p.base_url}/audio/transcriptions", headers, body, timeout=max(120.0, p.timeout))
        except VoiceError as e:
            if src is path and ffmpeg_ok() and "HTTP 400" in str(e):  # the endpoint did not like the format
                mp3 = to_mp3(path, Path(tmp) / "speech.mp3")
                body, ctype = _multipart(fields, mp3.name, mp3.read_bytes(), "audio/mpeg")
                headers["content-type"] = ctype
                raw = _post(f"{p.base_url}/audio/transcriptions", headers, body, timeout=max(120.0, p.timeout))
            else:
                raise
    try:
        text = json.loads(raw.decode("utf-8", "replace")).get("text", "")
    except ValueError:
        text = raw.decode("utf-8", "replace")
    return " ".join(str(text).split())


def speak(router: Router, text: str, vs: VoiceSettings) -> tuple[bytes, str]:
    """Text -> (audio bytes, file extension)."""
    if not vs.can_speak:
        raise VoiceError("text to speech is not set up: add [tts] provider and model to the config")
    try:
        raw, ctype = router.post("audio/speech", {"model": vs.tts_model, "input": text[:4000], "voice": vs.tts_voice,
                                                  "response_format": "mp3"}, provider=vs.tts_provider, timeout=180)
    except RouterError as e:
        raise VoiceError(f"text to speech failed: {e}") from None
    return raw, audio_ext(raw, ctype)


def audio_ext(raw: bytes, ctype: str = "") -> str:
    """The real format of returned audio (some services send WAV whatever was asked)."""
    if raw[:4] == b"RIFF":
        return ".wav"
    if raw[:4] == b"OggS":
        return ".ogg"
    if raw[:4] == b"fLaC":
        return ".flac"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    return {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/opus": ".ogg",
            "audio/flac": ".flac"}.get(ctype.split(";")[0].strip().lower(), ".mp3")


def speakable(text: str, limit: int = 900) -> str:
    """Reply text as it should sound: no Markdown, code or links read aloud; cut at a sentence."""
    import re
    t = re.sub(r"```.*?```", " (code in the message) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "(link in the message)", t)
    t = re.sub(r"[*_#>|]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = max(t.rfind(". ", 0, limit), t.rfind("! ", 0, limit), t.rfind("? ", 0, limit))
        t = t[:cut + 1] if cut > limit // 3 else t[:limit] + "…"
    return t


def add_voice_tools(reg, router: Router, workspace, vs: VoiceSettings) -> None:
    from .tools import ToolError
    if not vs.can_listen:
        return

    @reg.tool(
        "transcribe_audio",
        "Turn the speech in an audio or video file in the workspace (voice note, recording, meeting, video) into text.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    def _transcribe(args: dict[str, Any]) -> str:
        p = workspace.resolve(args["path"])
        if not p.is_file():
            raise ToolError(f"no such file: {args['path']}")
        try:
            text = transcribe(router, p, vs)
        except VoiceError as e:
            raise ToolError(str(e)) from None
        secs = duration(p)
        return (f"[{secs:.0f} s] " if secs else "") + (text or "(no speech found)")


__all__ = ["VoiceSettings", "VoiceError", "transcribe", "speak", "speakable", "to_voice_ogg", "to_mp3", "duration",
           "audio_ext", "add_voice_tools", "AUDIO_EXT"]
