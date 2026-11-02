"""Seeing, drawing and speaking: vision, image generation, text-to-speech.

All three go through the owner's configured providers (routers.py), each with its
own provider/model in smooth-agent.toml:

    [vision]                 # defaults to the main provider and its model
    provider = "anthropic"
    model = "claude-haiku-5-5"

    [image]                  # only registered when set
    provider = "openai"
    model = "gpt-image-1"

    [tts]                    # only registered when set
    provider = "openai"
    model = "gpt-4o-mini-tts"
    voice = "alloy"

Files the tools produce are written into the workspace (images/, audio/).
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .routers import Router, RouterError
from .tools import Registry, ToolError, Workspace

MAX_IMAGE_BYTES = 10_000_000

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_mime(data: bytes) -> str | None:
    for sig, mime in _SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_image(ref: str, workspace: Workspace) -> tuple[bytes, str]:
    """An image from an http(s) URL, a data: URL or a workspace path -> (bytes, mime)."""
    ref = str(ref or "").strip()
    if not ref:
        raise ToolError("give image_url: a URL, a data: URL or a workspace path")
    if ref.startswith("data:"):
        try:
            head, b64 = ref.split(",", 1)
            data = base64.b64decode(b64)
        except (ValueError, base64.binascii.Error):
            raise ToolError("bad data: URL") from None
    elif ref.startswith(("http://", "https://")):
        req = urllib.request.Request(ref, headers={"user-agent": "smooth-agent/0.3"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ToolError(f"could not download the image: {e}") from None
    else:
        p = workspace.resolve(ref)
        if not p.is_file():
            raise ToolError(f"no such file: {ref}")
        data = p.read_bytes()[: MAX_IMAGE_BYTES + 1]
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError("image is larger than 10 MB")
    mime = image_mime(data)
    if not mime:
        raise ToolError("not a PNG, JPEG, GIF or WebP image")
    return data, mime


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


@dataclass
class MediaSettings:
    vision_provider: str | None = None
    vision_model: str | None = None
    image_provider: str | None = None
    image_model: str | None = None
    image_size: str = "1024x1024"
    tts_provider: str | None = None
    tts_model: str | None = None
    tts_voice: str = "alloy"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "MediaSettings":
        v, i, t = cfg.get("vision") or {}, cfg.get("image") or {}, cfg.get("tts") or {}
        return cls(vision_provider=v.get("provider"), vision_model=v.get("model"),
                   image_provider=i.get("provider"), image_model=i.get("model"), image_size=str(i.get("size", "1024x1024")),
                   tts_provider=t.get("provider"), tts_model=t.get("model"), tts_voice=str(t.get("voice", "alloy")))


def describe(router: Router, image: tuple[bytes, str], question: str, ms: MediaSettings, max_tokens: int = 1200) -> str:
    """Ask the vision model about one image."""
    data, mime = image
    messages = [{"role": "user", "content": [
        {"type": "text", "text": question or "Describe this image in detail. Copy any text in it exactly."},
        {"type": "image_url", "image_url": {"url": data_url(data, mime)}},
    ]}]
    try:
        c = router.complete(messages, provider=ms.vision_provider, model=ms.vision_model, max_tokens=max_tokens)
    except RouterError as e:
        raise ToolError(f"vision model failed: {e}") from None
    if not c.text.strip():
        raise ToolError("the vision model returned no text (is the configured model able to see images?)")
    return c.text.strip()


def add_media_tools(reg: Registry, router: Router, workspace: Workspace, ms: MediaSettings) -> None:
    @reg.tool(
        "vision_analyze",
        "Look at an image (http(s) URL, data: URL, or a file in the workspace) and answer a question about it: describe "
        "it, read the text in it, check a chart or a screenshot.",
        {"type": "object", "properties": {
            "image_url": {"type": "string"}, "question": {"type": "string"}}, "required": ["image_url"]},
    )
    def _vision(args: dict[str, Any]) -> str:
        return describe(router, load_image(args.get("image_url") or args.get("path") or "", workspace),
                        str(args.get("question") or ""), ms)

    if ms.image_provider or ms.image_model:
        @reg.tool(
            "image_generate",
            "Create an image from a text prompt and save it in the workspace (images/). Returns the file path(s).",
            {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "size": {"type": "string", "description": "e.g. 1024x1024, 1536x1024, 1024x1536"},
                "n": {"type": "integer", "minimum": 1, "maximum": 4},
                "filename": {"type": "string", "description": "optional base name, without extension"},
            }, "required": ["prompt"]},
        )
        def _image(args: dict[str, Any]) -> str:
            body = {"prompt": str(args["prompt"]), "size": str(args.get("size") or ms.image_size),
                    "n": max(1, min(int(args.get("n") or 1), 4))}
            if ms.image_model:
                body["model"] = ms.image_model
            try:
                raw, _ = router.post("images/generations", body, provider=ms.image_provider, timeout=300)
            except RouterError as e:
                raise ToolError(f"image generation failed: {e}") from None
            items = json.loads(raw.decode("utf-8", "replace")).get("data") or []
            if not items:
                raise ToolError("the provider returned no image")
            out_dir = workspace.resolve("images")
            out_dir.mkdir(exist_ok=True)
            base = "".join(c for c in str(args.get("filename") or "") if c.isalnum() or c in "-_") or time.strftime("%Y%m%d-%H%M%S")
            saved = []
            for i, it in enumerate(items, 1):
                if it.get("b64_json"):
                    data = base64.b64decode(it["b64_json"])
                elif it.get("url"):
                    data, _ = load_image(it["url"], workspace)
                else:
                    continue
                ext = {"image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(image_mime(data) or "", "png")
                p = out_dir / (f"{base}.{ext}" if len(items) == 1 else f"{base}-{i}.{ext}")
                p.write_bytes(data)
                saved.append(str(p.relative_to(workspace.root)))
            return "saved " + ", ".join(saved) if saved else "error: no image data in the response"

    if ms.tts_provider or ms.tts_model:
        @reg.tool(
            "text_to_speech",
            "Turn text into speech and save it as an MP3 in the workspace (audio/). Returns the file path.",
            {"type": "object", "properties": {
                "text": {"type": "string"}, "voice": {"type": "string"},
                "filename": {"type": "string", "description": "optional base name, without extension"},
            }, "required": ["text"]},
        )
        def _tts(args: dict[str, Any]) -> str:
            text = str(args.get("text") or "").strip()
            if not text:
                raise ToolError("text is empty")
            body = {"input": text[:4000], "voice": str(args.get("voice") or ms.tts_voice), "response_format": "mp3"}
            if ms.tts_model:
                body["model"] = ms.tts_model
            try:
                raw, ctype = router.post("audio/speech", body, provider=ms.tts_provider, timeout=180)
            except RouterError as e:
                raise ToolError(f"text-to-speech failed: {e}") from None
            if "json" in ctype or len(raw) < 100:
                raise ToolError(f"the provider returned no audio: {raw[:200].decode('utf-8', 'replace')}")
            out_dir = workspace.resolve("audio")
            out_dir.mkdir(exist_ok=True)
            base = "".join(c for c in str(args.get("filename") or "") if c.isalnum() or c in "-_") or time.strftime("%Y%m%d-%H%M%S")
            p = out_dir / f"{base}.mp3"
            p.write_bytes(raw)
            return f"saved {p.relative_to(workspace.root)} ({len(raw) // 1024} KB)"


__all__ = ["MediaSettings", "add_media_tools", "describe", "load_image", "image_mime", "data_url"]
