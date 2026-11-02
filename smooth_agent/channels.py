"""Chat channels: talk to Smooth Agent from Telegram, WhatsApp and more.

A `Channel` receives messages from a chat app and sends replies back. The
`ChannelHub` ties channels to the agent: each chat gets its own saved session
(so the conversation persists across restarts), messages are handled one at a
time by a single worker (which keeps the model from being hit by a storm of
parallel requests), and tool calls that need approval are sent to the owner as
tappable buttons.

Only people on a channel's `owners` list are answered — this agent runs on the
owner's own machine with their keys, so it is single-owner by default.

Everything here is stdlib only: Telegram uses long-polling over HTTPS, WhatsApp
and WeChat use their HTTP APIs, and inbound webhooks are served by a small
`http.server`. No third-party packages.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

TELEGRAM_API = "https://api.telegram.org"
WHATSAPP_API = "https://graph.facebook.com/v21.0"
SLACK_API = "https://slack.com/api"
MAX_LEN = 3900  # most apps cap a message near 4096 chars; leave room


def header(headers: dict[str, str], name: str, default: str = "") -> str:
    """Case-insensitive header lookup (webhook servers hand us mixed case)."""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return default


def _http(method: str, url: str, *, headers: dict[str, str] | None = None,
          data: bytes | None = None, timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted API hosts)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None, timeout: float = 60.0) -> tuple[int, dict]:
    h = {"content-type": "application/json", **(headers or {})}
    status, body = _http("POST", url, headers=h, data=json.dumps(payload).encode(), timeout=timeout)
    try:
        return status, json.loads(body or b"{}")
    except ValueError:
        return status, {"raw": body.decode("utf-8", "replace")}


def chunk(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split a long reply on line boundaries so each piece fits one message."""
    text = text or "(no answer)"
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def balance_fences(parts: list[str]) -> list[str]:
    """Close a ``` code block that a chunk boundary cut in half, and reopen it in the next chunk."""
    out, carry = [], ""
    for part in parts:
        part = carry + part
        carry = ""
        if part.count("```") % 2:
            part += "\n```"
            carry = "```\n"
        out.append(part)
    return out


def md_to_html(text: str) -> str:
    """The Markdown chat models write -> Telegram HTML: code blocks, `code`, **bold**, [links](url), # headings."""
    import html
    import re
    out = []
    for i, seg in enumerate(re.split(r"```", text)):
        if i % 2:  # inside a fence: drop a language tag line, keep the code verbatim
            lang, nl, body = seg.partition("\n")
            code = body if nl and re.fullmatch(r"[\w+#.-]*", lang.strip()) else seg
            out.append(f"<pre>{html.escape(code.strip(chr(10)))}</pre>")
            continue
        seg = html.escape(seg, quote=False)
        seg = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", seg)
        seg = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", seg)
        seg = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', seg)
        seg = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", seg)
        out.append(seg)
    return "".join(out)


@dataclass
class Inbound:
    channel: str
    chat_id: str          # where to reply
    text: str
    user_id: str = ""     # who sent it
    name: str = ""        # display name, for logs
    raw: dict = field(default_factory=dict)
    # files sent with the message: {"kind", "file_id", "name", "size", "mime"}; the channel's download() fetches them
    attachments: list[dict] = field(default_factory=list)


def telegram_attachments(msg: dict) -> list[dict]:
    """The files in one Telegram message: the largest photo size, documents, voice, audio, video."""
    mid = msg.get("message_id", "")
    out = []
    if msg.get("photo"):
        ph = msg["photo"][-1]  # sizes come smallest first
        out.append({"kind": "photo", "file_id": ph["file_id"], "name": f"photo-{mid}.jpg", "size": ph.get("file_size", 0),
                    "mime": "image/jpeg"})
    for key, ext in (("document", ""), ("voice", ".ogg"), ("audio", ".mp3"), ("video", ".mp4"), ("video_note", ".mp4"),
                     ("animation", ".mp4")):
        f = msg.get(key)
        if f:
            name = f.get("file_name") or f"{key}-{mid}{ext}"
            out.append({"kind": key, "file_id": f["file_id"], "name": name, "size": f.get("file_size", 0),
                        "mime": f.get("mime_type", ""), "duration": f.get("duration")})
    return out


def safe_filename(name: str) -> str:
    """A file name that is safe on every OS: no folders, no control or reserved characters."""
    import re
    name = Path(str(name).replace("\\", "/")).name.strip().strip(".")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:150] or "file"


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    stem, suffix, n = p.stem, p.suffix, 1
    while p.exists():
        p = folder / f"{stem}-{n}{suffix}"
        n += 1
    return p


@dataclass
class ChannelConfig:
    name: str
    owners: list[str] = field(default_factory=list)   # chat/user ids allowed to talk to the agent
    auto_approve: bool = False                          # approve the owner's tool calls without asking
    options: dict[str, Any] = field(default_factory=dict)


class Channel:
    """Base class. A channel either polls (`start` launches a thread) or is fed
    by the webhook server (`webhook_path` set, `handle_webhook` parses a request)."""

    kind = "base"
    webhook_path: str | None = None
    supports_buttons = False
    supports_progress = False  # post/edit/delete one live progress message

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.name = cfg.name
        self.log = log
        self.hub: "ChannelHub | None" = None
        self._owners = {str(o) for o in cfg.owners}

    # --- lifecycle ---
    def start(self, hub: "ChannelHub") -> None:
        self.hub = hub

    def stop(self) -> None:
        pass

    # --- access ---
    def is_owner(self, inbound: Inbound) -> bool:
        if not self._owners:
            return False
        return inbound.chat_id in self._owners or (inbound.user_id and inbound.user_id in self._owners)

    def owner_chat(self) -> str | None:
        """A chat id to push to (cron delivery, proactive send) when none is given."""
        return next(iter(self.cfg.owners), None)

    # --- output ---
    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        raise NotImplementedError

    # --- files the owner sends ---
    def download(self, att: dict, dest_dir: Path) -> Path:
        raise RuntimeError(f"{self.kind}: receiving files is not supported yet")

    # --- webhook channels ---
    def handle_webhook(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        return 404, b"not found"


# ---------------------------------------------------------------- Telegram

class TelegramChannel(Channel):
    """Telegram via long-polling. Needs a bot token (from @BotFather). Owners are
    numeric chat ids; message the bot once and the agent will tell you yours."""

    kind = "telegram"
    supports_buttons = True

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.token = str(cfg.options.get("token") or "")
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _api(self, method: str, payload: dict, *, timeout: float = 65.0, tries: int = 3) -> dict:
        """One Bot API call; a dropped connection (reset, timeout) is retried with a short backoff."""
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        for attempt in range(tries):
            try:
                _status, data = _post_json(url, payload, timeout=timeout)
                return data
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                if attempt == tries - 1 or method == "getUpdates":  # the poll loop has its own retry
                    raise
                time.sleep(1.0 + attempt)
        return {}

    supports_progress = True
    COMMANDS = [("new", "start a fresh conversation"), ("status", "model, session, memory"),
                ("model", "show or switch the model"), ("stop", "stop what I'm doing"), ("help", "what I can do")]

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        parts = balance_fences(chunk(text))
        for i, part in enumerate(parts):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": md_to_html(part), "parse_mode": "HTML",
                                       "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
            data = self._api("sendMessage", payload)
            if not data.get("ok") and "parse" in str(data.get("description", "")).lower():
                payload.pop("parse_mode")  # markup Telegram would not accept: send it as plain text
                payload["text"] = part
                data = self._api("sendMessage", payload)
            if not data.get("ok"):  # e.g. 403 "bot can't initiate conversation": the owner never pressed Start
                raise RuntimeError(f"telegram sendMessage: {data.get('description') or data}")

    # --- one live progress message: post it, edit it, delete it ---
    @staticmethod
    def _progress_html(text: str) -> str:
        """Header bold; each step as its status mark + the step in <code> (wraps on a phone, unlike <pre>)."""
        import html
        head, _, body = text[:MAX_LEN].partition("\n")
        rows = []
        for line in body.splitlines():
            mark, sp, rest = line.partition(" ")
            if mark in ("⏳", "✓", "✗") and sp:
                rows.append(f"{mark} <code>{html.escape(rest)}</code>")
            else:
                rows.append(f"<i>{html.escape(line)}</i>")
        return f"<b>{html.escape(head)}</b>" + ("\n" + "\n".join(rows) if rows else "")

    def post(self, chat_id: str, text: str) -> str | None:
        data = self._api("sendMessage", {"chat_id": chat_id, "text": self._progress_html(text), "parse_mode": "HTML",
                                         "disable_notification": True, "disable_web_page_preview": True}, timeout=15)
        return str(data["result"]["message_id"]) if data.get("ok") else None

    def edit(self, chat_id: str, message_id: str, text: str) -> None:
        self._api("editMessageText", {"chat_id": chat_id, "message_id": int(message_id), "parse_mode": "HTML",
                                      "text": self._progress_html(text), "disable_web_page_preview": True}, timeout=15)

    def delete(self, chat_id: str, message_id: str) -> None:
        self._api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, timeout=15)

    def typing(self, chat_id: str) -> None:
        try:
            self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not self.token:
            raise RuntimeError("telegram: no bot token (set the token env in [channels.telegram])")
        try:  # the "/" menu in the chat
            self._api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in self.COMMANDS]},
                      timeout=15)
        except Exception as e:
            self.log(f"telegram: could not set the command menu: {e}")
        self._thread = threading.Thread(target=self._poll, name="telegram-poll", daemon=True)
        self._thread.start()
        self.log(f"telegram: polling as bot (owners: {', '.join(self.cfg.owners) or 'NONE — set [channels.telegram] owners'})")

    def stop(self) -> None:
        self._stop.set()

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._api("getUpdates", {"offset": self._offset, "timeout": 50,
                                                "allowed_updates": ["message", "callback_query"]}, timeout=60)
            except Exception as e:
                if not self._stop.is_set():
                    self.log(f"telegram: poll error: {e}")
                    time.sleep(3)
                continue
            if not data.get("ok"):
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                try:
                    self._dispatch(upd)
                except Exception as e:
                    self.log(f"telegram: update error: {e}")

    def _dispatch(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            self._api("answerCallbackQuery", {"callback_query_id": cq.get("id")}, timeout=10)
            data = str(cq.get("data") or "")
            if self.hub and (data.startswith("a:") or data.startswith("d:")):
                self.hub.resolve_approval(data[2:], data.startswith("a:"))
            return
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        text = msg.get("text") or msg.get("caption") or ""
        inbound = Inbound(channel=self.name, chat_id=str(chat.get("id")), text=text,
                          user_id=str(frm.get("id") or ""), name=frm.get("username") or frm.get("first_name") or "",
                          raw=msg, attachments=telegram_attachments(msg))
        group = msg.get("media_group_id")
        if group:  # an album arrives as one message per item: collect them into one
            self._collect_album(str(group), inbound)
            return
        if self.hub:
            self.hub.dispatch(self, inbound)

    ALBUM_WAIT = 1.5  # seconds to wait for the rest of an album

    def _collect_album(self, group: str, inbound: Inbound) -> None:
        if not hasattr(self, "_albums"):
            self._albums: dict[str, Inbound] = {}
            self._album_lock = threading.Lock()
        with self._album_lock:
            first = self._albums.get(group)
            if first is None:
                self._albums[group] = inbound
                timer = threading.Timer(self.ALBUM_WAIT, self._flush_album, args=(group,))
                timer.daemon = True
                timer.start()
                return
            first.attachments += inbound.attachments
            if inbound.text and not first.text:
                first.text = inbound.text

    def _flush_album(self, group: str) -> None:
        with self._album_lock:
            inbound = self._albums.pop(group, None)
        if inbound is not None and self.hub:
            self.hub.dispatch(self, inbound)

    MAX_DOWNLOAD = 20 * 1024 * 1024  # the Bot API serves files up to 20 MB

    def download(self, att: dict, dest_dir: Path) -> Path:
        """Fetch one attachment into dest_dir; returns the saved path."""
        size = int(att.get("size") or 0)
        if size > self.MAX_DOWNLOAD:
            raise RuntimeError(f"{att.get('name')} is {size / 1e6:.0f} MB; Telegram bots can only download files up to 20 MB")
        info = self._api("getFile", {"file_id": att["file_id"]}, timeout=30)
        if not info.get("ok"):
            raise RuntimeError(f"getFile: {info.get('description') or info}")
        url = f"{TELEGRAM_API}/file/bot{self.token}/{info['result']['file_path']}"
        dest = unique_path(dest_dir, safe_filename(att.get("name") or Path(info["result"]["file_path"]).name))
        dest_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:  # noqa: S310 (Telegram file host)
            shutil.copyfileobj(r, fh)
        return dest


# ---------------------------------------------------------------- WhatsApp (Cloud API)

class WhatsAppChannel(Channel):
    """WhatsApp via Meta's Cloud API. Needs a phone-number id, a permanent token
    and a webhook (point Meta at <base>/webhook/whatsapp). Owners are phone
    numbers in international form without '+', e.g. 628123456789."""

    kind = "whatsapp"
    webhook_path = "/webhook/whatsapp"

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.token = str(cfg.options.get("token") or "")
        self.phone_id = str(cfg.options.get("phone_id") or "")
        self.verify_token = str(cfg.options.get("verify_token") or "")

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not (self.token and self.phone_id):
            raise RuntimeError("whatsapp: need token and phone_id in [channels.whatsapp]")
        self.log(f"whatsapp: webhook at {self.webhook_path}; owners: {', '.join(self.cfg.owners) or 'NONE'}")

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        url = f"{WHATSAPP_API}/{self.phone_id}/messages"
        head = {"authorization": f"Bearer {self.token}"}
        for part in chunk(text, 4000):
            _post_json(url, {"messaging_product": "whatsapp", "to": chat_id, "type": "text",
                             "text": {"body": part}}, headers=head)

    def handle_webhook(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            if q.get("hub.verify_token", [""])[0] == self.verify_token and self.verify_token:
                return 200, q.get("hub.challenge", [""])[0].encode()
            return 403, b"forbidden"
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, b"bad json"
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                for m in (ch.get("value") or {}).get("messages", []) or []:
                    if m.get("type") != "text":
                        continue
                    inbound = Inbound(channel=self.name, chat_id=str(m.get("from")), user_id=str(m.get("from")),
                                      text=(m.get("text") or {}).get("body", ""), raw=m)
                    if self.hub:
                        self.hub.dispatch(self, inbound)
        return 200, b"ok"


# ---------------------------------------------------------------- WeChat Work (notify)

class WeChatChannel(Channel):
    """WeChat Work group robot — outbound notifications (cron delivery, alerts).
    Set [channels.wechat] options.webhook_url to the robot URL. Inbound chat is
    not wired yet; use it as a delivery target."""

    kind = "wechat"

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.webhook_url = str(cfg.options.get("webhook_url") or "")

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not self.webhook_url:
            raise RuntimeError("wechat: set options.webhook_url in [channels.wechat]")
        self.log("wechat: delivery robot ready")

    def owner_chat(self) -> str | None:
        return "robot"  # the robot posts to its one group; there is no per-chat target

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        for part in chunk(text, 3900):
            _post_json(self.webhook_url, {"msgtype": "text", "text": {"content": part}})


# ---------------------------------------------------------------- Slack (Events API)

class SlackChannel(Channel):
    """Slack via the Events API (HTTP webhook, signed with the app's signing
    secret) and chat.postMessage. Point the app's Event Subscriptions at
    <host>/webhook/slack and subscribe to message.im (and message.channels if
    you want it in channels). Owners are Slack user ids (U…)."""

    kind = "slack"
    webhook_path = "/webhook/slack"
    MAX_SKEW = 300  # seconds; Slack's replay-protection window

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.token = str(cfg.options.get("token") or "")
        self.signing_secret = str(cfg.options.get("signing_secret") or "")

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not (self.token and self.signing_secret):
            raise RuntimeError("slack: need token (bot token) and signing_secret in [channels.slack]")
        self.log(f"slack: webhook at {self.webhook_path}; owners: {', '.join(self.cfg.owners) or 'NONE'}")

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        head = {"authorization": f"Bearer {self.token}"}
        for part in chunk(text):
            _post_json(f"{SLACK_API}/chat.postMessage", {"channel": chat_id, "text": part}, headers=head)

    def verify(self, headers: dict[str, str], body: bytes, now: float | None = None) -> bool:
        ts = header(headers, "x-slack-request-timestamp")
        sig = header(headers, "x-slack-signature")
        if not (ts and sig and self.signing_secret):
            return False
        try:
            if abs((now or time.time()) - int(ts)) > self.MAX_SKEW:
                return False
        except ValueError:
            return False
        base = b"v0:" + ts.encode() + b":" + body
        want = "v0=" + hmac.new(self.signing_secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, sig)

    def handle_webhook(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if method != "POST":
            return 405, b"method not allowed"
        if not self.verify(headers, body):
            return 401, b"bad signature"
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, b"bad json"
        if data.get("type") == "url_verification":
            return 200, str(data.get("challenge", "")).encode()
        if header(headers, "x-slack-retry-num"):
            return 200, b"ok"  # Slack retries when we were slow; we already queued it
        ev = data.get("event") or {}
        if ev.get("type") == "message" and not ev.get("bot_id") and not ev.get("subtype") and ev.get("text"):
            inbound = Inbound(channel=self.name, chat_id=str(ev.get("channel")), user_id=str(ev.get("user") or ""),
                              text=ev["text"], raw=ev)
            if self.hub:
                self.hub.dispatch(self, inbound)
        return 200, b"ok"


# ---------------------------------------------------------------- Matrix

class MatrixChannel(Channel):
    """Matrix via the client-server API: long-polls /sync, replies in the same
    room. Works with any homeserver (matrix.org or your own). Needs an access
    token for the bot account; owners are Matrix ids like @you:example.org."""

    kind = "matrix"

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.homeserver = str(cfg.options.get("homeserver") or "https://matrix.org").rstrip("/")
        self.token = str(cfg.options.get("token") or "")
        self.user_id = str(cfg.options.get("user_id") or "")  # the bot's own id, to skip its echoes
        self._since: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _req(self, method: str, path: str, payload: dict | None = None, *, timeout: float = 60.0) -> dict:
        h = {"authorization": f"Bearer {self.token}", "content-type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else None
        _s, body = _http(method, f"{self.homeserver}/_matrix/client/v3{path}", headers=h, data=data, timeout=timeout)
        try:
            return json.loads(body or b"{}")
        except ValueError:
            return {}

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        room = urllib.parse.quote(chat_id, safe="")
        for part in chunk(text, 16000):
            txn = uuid.uuid4().hex
            self._req("PUT", f"/rooms/{room}/send/m.room.message/{txn}", {"msgtype": "m.text", "body": part})

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not self.token:
            raise RuntimeError("matrix: need token (bot access token) in [channels.matrix]")
        if not self.user_id:
            self.user_id = str(self._req("GET", "/account/whoami", timeout=15).get("user_id") or "")
        # skip history: start from "now" so old messages are not answered on boot
        self._since = self._req("GET", "/sync?timeout=0&filter=" + urllib.parse.quote(
            json.dumps({"room": {"timeline": {"limit": 0}}})), timeout=30).get("next_batch")
        self._thread = threading.Thread(target=self._poll, name="matrix-sync", daemon=True)
        self._thread.start()
        self.log(f"matrix: syncing as {self.user_id or '?'} on {self.homeserver}; owners: {', '.join(self.cfg.owners) or 'NONE'}")

    def stop(self) -> None:
        self._stop.set()

    def _poll(self) -> None:
        while not self._stop.is_set():
            q = "/sync?timeout=30000" + (f"&since={urllib.parse.quote(self._since)}" if self._since else "")
            try:
                data = self._req("GET", q, timeout=45)
            except Exception as e:
                if not self._stop.is_set():
                    self.log(f"matrix: sync error: {e}")
                    time.sleep(3)
                continue
            if not data.get("next_batch"):
                time.sleep(3)
                continue
            self._since = data["next_batch"]
            try:
                self._handle_sync(data)
            except Exception as e:
                self.log(f"matrix: event error: {e}")

    def _handle_sync(self, data: dict) -> None:
        rooms = ((data.get("rooms") or {}).get("join") or {})
        for room_id, room in rooms.items():
            for ev in ((room.get("timeline") or {}).get("events") or []):
                if ev.get("type") != "m.room.message" or ev.get("sender") == self.user_id:
                    continue
                content = ev.get("content") or {}
                if content.get("msgtype") != "m.text" or not content.get("body"):
                    continue
                inbound = Inbound(channel=self.name, chat_id=room_id, user_id=str(ev.get("sender") or ""),
                                  text=content["body"], name=str(ev.get("sender") or ""), raw=ev)
                if self.hub:
                    self.hub.dispatch(self, inbound)
        # auto-join rooms an owner invites the bot to
        for room_id, room in ((data.get("rooms") or {}).get("invite") or {}).items():
            events = (room.get("invite_state") or {}).get("events") or []
            inviter = next((e.get("sender") for e in events if e.get("type") == "m.room.member"
                            and (e.get("content") or {}).get("membership") == "invite"), "")
            if inviter in self._owners:
                self._req("POST", f"/rooms/{urllib.parse.quote(room_id, safe='')}/join", {}, timeout=15)
                self.log(f"matrix: joined {room_id} (invited by {inviter})")


# ---------------------------------------------------------------- LINE

class LineChannel(Channel):
    """LINE Messaging API: webhook signed with the channel secret, replies by
    push to the sender. Point the webhook at <host>/webhook/line. Owners are
    LINE user ids (U…), shown in the webhook events."""

    kind = "line"
    webhook_path = "/webhook/line"
    API = "https://api.line.me/v2/bot"

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.token = str(cfg.options.get("token") or "")
        self.secret = str(cfg.options.get("secret") or "")

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not (self.token and self.secret):
            raise RuntimeError("line: need token (channel access token) and secret (channel secret) in [channels.line]")
        self.log(f"line: webhook at {self.webhook_path}; owners: {', '.join(self.cfg.owners) or 'NONE'}")

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        parts = chunk(text, 4900)
        head = {"authorization": f"Bearer {self.token}"}
        for i in range(0, len(parts), 5):  # up to 5 messages per push
            _post_json(f"{self.API}/message/push", {"to": chat_id, "messages": [
                {"type": "text", "text": p} for p in parts[i:i + 5]]}, headers=head)

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        sig = header(headers, "x-line-signature")
        if not (sig and self.secret):
            return False
        want = base64.b64encode(hmac.new(self.secret.encode(), body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(want, sig)

    def handle_webhook(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if method != "POST":
            return 405, b"method not allowed"
        if not self.verify(headers, body):
            return 401, b"bad signature"
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, b"bad json"
        for ev in data.get("events", []):
            msg = ev.get("message") or {}
            if ev.get("type") != "message" or msg.get("type") != "text":
                continue
            src = ev.get("source") or {}
            uid = str(src.get("userId") or "")
            chat = str(src.get("groupId") or src.get("roomId") or uid)
            inbound = Inbound(channel=self.name, chat_id=chat, user_id=uid, text=msg.get("text", ""), raw=ev)
            if self.hub:
                self.hub.dispatch(self, inbound)
        return 200, b"ok"


# ---------------------------------------------------------------- Discord (delivery)

class DiscordChannel(Channel):
    """Discord channel webhook — outbound delivery (cron results, alerts,
    send_message). Create one under Channel settings → Integrations → Webhooks
    and set options.webhook_url (or webhook_url_env). Two-way chat through the
    bot gateway is not wired yet."""

    kind = "discord"

    def __init__(self, cfg: ChannelConfig, *, log: Callable[[str], None] = print):
        super().__init__(cfg, log=log)
        self.webhook_url = str(cfg.options.get("webhook_url") or "")

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not self.webhook_url:
            raise RuntimeError("discord: set webhook_url (or webhook_url_env) in [channels.discord]")
        self.log("discord: delivery webhook ready")

    def owner_chat(self) -> str | None:
        return "webhook"

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        for part in chunk(text, 1900):  # Discord caps a message at 2000 chars
            _post_json(self.webhook_url, {"content": part, "allowed_mentions": {"parse": []}})


CHANNEL_TYPES: dict[str, type[Channel]] = {
    "telegram": TelegramChannel,
    "whatsapp": WhatsAppChannel,
    "wechat": WeChatChannel,
    "slack": SlackChannel,
    "matrix": MatrixChannel,
    "line": LineChannel,
    "discord": DiscordChannel,
}


# ---------------------------------------------------------------- webhook server

class WebhookServer:
    """One small HTTP server that routes <path> to the channel that owns it.
    Used by webhook-based channels (WhatsApp). Stdlib http.server."""

    def __init__(self, host: str, port: int, routes: dict[str, Channel], *, log: Callable[[str], None] = print):
        self.host, self.port, self.routes, self.log = host, port, routes, log
        self._srv = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        routes, log = self.routes, self.log

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _route(self, method: str) -> None:
                base = urllib.parse.urlparse(self.path).path
                ch = routes.get(base)
                if not ch:
                    self.send_response(404)
                    self.end_headers()
                    return
                n = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(n) if n else b""
                try:
                    status, data = ch.handle_webhook(method, self.path, dict(self.headers), raw)
                except Exception as e:  # a bad webhook must not kill the server
                    log(f"webhook {base}: {e}")
                    status, data = 500, b"error"
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self._srv = ThreadingHTTPServer((self.host, self.port), H)
        self._thread = threading.Thread(target=self._srv.serve_forever, name="webhook", daemon=True)
        self._thread.start()
        self.log(f"webhook server on {self.host}:{self.port} ({', '.join(self.routes)})")

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()


# ---------------------------------------------------------------- the hub

@dataclass
class _Pending:
    event: threading.Event
    ok: bool = False


# typed answers to an approval prompt on channels without buttons (English + Indonesian)
YES = {"y", "yes", "ya", "iya", "ok", "oke", "okay", "boleh", "allow", "lanjut", "gas", "✅"}
NO = {"n", "no", "tidak", "nggak", "ngga", "gak", "ga", "jangan", "deny", "stop", "batal", "❌"}


def describe_step(name: str, args: dict[str, Any]) -> str:
    """A tool call as a shell-style line: `$ git pull`, `read app.py`, `write app.py`, `open https://…`."""
    a = args if isinstance(args, dict) else {}

    def g(*keys: str) -> str:
        for k in keys:
            v = a.get(k)
            if v:
                return ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        return ""

    verbs: dict[str, Callable[[], str]] = {
        "terminal": lambda: "$ " + g("command"),
        "execute_code": lambda: "python " + (g("code").strip().splitlines() or [""])[0],
        "process": lambda: f"process {g('action')} {g('session_id')}",
        "read_file": lambda: "read " + g("path"),
        "write_file": lambda: "write " + g("path"),
        "patch": lambda: "edit " + (g("path") or "(diff)"),
        "list_dir": lambda: "ls " + (g("path") or "."),
        "search_files": lambda: f"grep {g('pattern')}" + (f" {g('path')}" if g("path") else ""),
        "web_search": lambda: "search " + g("query"),
        "web_extract": lambda: "open " + g("urls", "url"),
        "http_get": lambda: "fetch " + g("url"),
        "browser_navigate": lambda: "open " + g("url"),
        "browser_click": lambda: "click " + g("ref"),
        "browser_type": lambda: "type " + g("text"),
        "memory": lambda: f"memory {g('action')}",
        "session_search": lambda: "recall " + g("query"),
        "skill_view": lambda: "skill " + g("name"),
        "skills_list": lambda: "skills",
        "skill_manage": lambda: f"skill {g('action')} {g('name')}",
        "delegate_task": lambda: "delegate " + (g("goal") or f"{len(a.get('tasks') or [])} tasks"),
        "cronjob": lambda: f"cron {g('action')} {g('name', 'job_id')}",
        "send_message": lambda: "message",
        "todo": lambda: "todo",
        "clarify": lambda: "ask " + g("question"),
        "vision_analyze": lambda: "look " + g("image_url", "path"),
        "image_generate": lambda: "draw " + g("prompt"),
        "text_to_speech": lambda: "speak",
        "now": lambda: "date",
    }
    if name in verbs:
        line = verbs[name]()
        if name == "terminal":
            line = "$ " + _short_command(g("command"))
        elif name in ("web_extract", "http_get", "browser_navigate"):
            line = line.split(" ", 1)[0] + " " + _short_url(line.split(" ", 1)[-1])
    elif name.startswith("browser_"):
        line = f"browser {name[8:]} {g('ref', 'key', 'direction')}"
    elif name.startswith("mcp_"):
        line = "mcp " + name[4:]
    else:
        line = f"{name} {g('path', 'query', 'url', 'name', 'action')}"
    line = " ".join(line.split())
    return line if len(line) <= 64 else line[:63] + "…"


def _short_command(cmd: str) -> str:
    """The part of a shell command worth showing: no leading `cd dir &&`, first line of a multi-line script."""
    import re
    cmd = cmd.strip()
    while True:
        m = re.match(r"cd\s+\S+\s*(?:&&|;)\s*", cmd)
        if not m:
            break
        cmd = cmd[m.end():]
    first, _, rest = cmd.partition("\n")
    return first.strip() + (" …" if rest.strip() else "")


def _short_url(url: str) -> str:
    import re
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")


class Progress:
    """The live progress message of one turn: created on the first tool step, edited as steps
    run and finish (throttled — Telegram allows about one edit a second), deleted when the
    answer is sent. Also keeps the 'typing…' indicator alive on long turns."""

    SHOW = 8          # last N steps on screen
    EDIT_EVERY = 1.5  # seconds between edits for new steps
    TIMER_EVERY = 5.0  # refresh the clock this often when nothing else changed
    TYPING_EVERY = 4.5
    MARK = {"run": "⏳", "ok": "✓", "err": "✗"}

    def __init__(self, channel: "Channel", chat_id: str, *, log: Callable[[str], None] = print):
        self.channel, self.chat_id, self.log = channel, chat_id, log
        self.steps: list[list[str]] = []  # [state, line]
        self.msg_id: str | None = None
        self._dirty = False
        self._last_edit = 0.0
        self._t0 = time.time()
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._tick, name="progress", daemon=True)
        self._thread.start()

    @property
    def lines(self) -> list[str]:
        return [f"{self.MARK[st]} {ln}" for st, ln in self.steps]

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            if event in ("tool", "sub:tool"):
                line = describe_step(data.get("name", ""), data.get("arguments") or {})
                self.steps.append(["run", ("↳ " if event == "sub:tool" else "") + line])
            elif event in ("tool_result", "sub:tool_result"):
                res = str(data.get("result", ""))
                failed = res.startswith(("error", "denied", "cancelled")) or res.startswith("[exit ") and not res.startswith("[exit 0]")
                for step in reversed(self.steps):
                    if step[0] == "run":
                        step[0] = "err" if failed else "ok"
                        break
            elif event == "compressed":
                self.steps.append(["ok", "summarised the earlier conversation"])
            else:
                return
            self._dirty = True

    def _render(self) -> str:
        n = len(self.steps)
        shown = self.lines[-self.SHOW:]
        head = f"⚙️ Working · {int(time.time() - self._t0)}s"
        more = [f"… {n - len(shown)} earlier steps"] if n > len(shown) else []
        return "\n".join([head, *more, *shown])

    def _tick(self) -> None:
        last_typing = 0.0
        while not self._done.wait(0.5):
            now = time.time()
            try:
                if now - last_typing >= self.TYPING_EVERY and hasattr(self.channel, "typing"):
                    self.channel.typing(self.chat_id)
                    last_typing = now
                with self._lock:
                    since = now - self._last_edit
                    due = (self._dirty and since >= self.EDIT_EVERY) or (self.msg_id is not None and since >= self.TIMER_EVERY)
                    text = self._render() if due else ""
                    if due:
                        self._dirty = False
                        self._last_edit = now
                if due:
                    if self.msg_id is None:
                        self.msg_id = self.channel.post(self.chat_id, text)
                    else:
                        self.channel.edit(self.chat_id, self.msg_id, text)
            except Exception as e:  # progress is cosmetic: never break the turn
                self.log(f"{self.channel.name}: progress update failed: {e}")

    def finish(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)
        if self.msg_id is not None:
            try:
                self.channel.delete(self.chat_id, self.msg_id)
            except Exception as e:
                self.log(f"{self.channel.name}: could not remove the progress message: {e}")


HELP = (
    "I'm your Smooth Agent. Just talk to me — I can search and read the web, work with files in my workspace, "
    "remember things about you, run scheduled jobs and (if you turn it on) run commands on this machine.\n\n"
    "/new — start a fresh conversation (the old one is kept)\n"
    "/status — model, session, memory\n"
    "/model — show or switch the model, e.g. /model jerouter\n"
    "/stop — stop what I'm doing\n"
    "/help — this message"
)


def approval_text(name: str, args: dict[str, Any]) -> str:
    """A short, readable approval question (Markdown) instead of raw JSON."""
    what = args.get("command") or args.get("code") or args.get("path")
    if name.startswith("files: "):
        return f"🔐 Allow me to {name[7:]}?\n`{args.get('path', '')}`"
    if what:
        body = str(what)
        body = body if len(body) <= 1500 else body[:1500] + "\n…"
        label = {"terminal": "Run this command", "execute_code": "Run this code"}.get(name, f"Allow {name}")
        return f"🔐 {label}?\n```\n{body}\n```"
    preview = json.dumps(args, ensure_ascii=False)
    return f"🔐 Allow {name}?\n`{preview[:500]}`"


class ChannelHub:
    """Runs the channels and routes their messages to per-chat agent sessions.

    `agent_for(session_id, channel, chat_id)` must return a ready Agent (built on
    the shared runtime). Messages are handled one at a time by a single worker
    thread so the model is never hit by many requests at once and so tool
    approvals and `send_message` always target the chat being handled.
    """

    def __init__(self, channels: list[Channel], agent_for: Callable[[str, str, str], Any], *,
                 log: Callable[[str], None] = print, approval_timeout: float = 180.0,
                 webhook_host: str = "0.0.0.0", webhook_port: int = 8808, workspace_root: Path | None = None):
        self.channels = {c.name: c for c in channels}
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()  # files sent land in inbox/
        self.agent_for = agent_for
        self.log = log
        self.approval_timeout = approval_timeout
        self.webhook_host, self.webhook_port = webhook_host, webhook_port
        self._q: "queue.Queue[tuple[Channel, Inbound] | None]" = queue.Queue()
        self._agents: dict[str, Any] = {}
        self._pending: dict[str, _Pending] = {}
        self._awaiting: dict[tuple[str, str], str] = {}  # (channel, chat) -> approval key waiting for a typed yes/no
        self._active: tuple[Channel, str] | None = None  # (channel, chat_id) being handled now
        self._worker: threading.Thread | None = None
        self._webhook: WebhookServer | None = None
        self._stop = False
        self._turn = threading.Lock()         # held by the worker for a whole turn
        self._agents_lock = threading.Lock()  # get-or-create of per-chat agents
        self._started = time.time()

    # --- intake ---
    def dispatch(self, channel: Channel, inbound: Inbound) -> None:
        if not inbound.text.strip() and not inbound.attachments:
            return
        if not channel.is_owner(inbound):
            self.log(f"{channel.name}: ignored {inbound.chat_id} ({inbound.name})")
            try:
                channel.send(inbound.chat_id, "This Smooth Agent is private. To use it, add this id to the channel's "
                             f"owners list:\n{inbound.chat_id}")
            except Exception:
                pass
            return
        if inbound.text.startswith("/") and self._command(channel, inbound):
            return
        # a typed answer to an approval prompt: resolve it here, because the worker is blocked waiting for it
        key = self._awaiting.get((channel.name, inbound.chat_id))
        if key:
            word = inbound.text.strip().lower().rstrip(".!")
            if word in YES or word in NO:
                self.resolve_approval(key, word in YES)
                return
        self._q.put((channel, inbound))

    def resolve_approval(self, key: str, ok: bool) -> None:
        p = self._pending.get(key)
        if p:
            p.ok = ok
            p.event.set()

    # --- tool-call approval, routed to the chat being handled ---
    def approve(self, name: str, args: dict[str, Any]) -> bool:
        active = self._active
        if not active:
            return False
        channel, chat_id = active
        if channel.cfg.auto_approve:
            return True
        key = uuid.uuid4().hex[:12]
        pending = self._pending[key] = _Pending(threading.Event())
        text = approval_text(name, args)
        minutes = max(1, round(self.approval_timeout / 60))
        try:
            if channel.supports_buttons:
                channel.send(chat_id, text, buttons=[("✅ Allow", f"a:{key}"), ("❌ Deny", f"d:{key}")])
            else:
                self._awaiting[(channel.name, chat_id)] = key
                channel.send(chat_id, text + f"\n\nReply yes to allow or no to deny (within {minutes} min; no reply = deny).")
        except Exception as e:
            self.log(f"{channel.name}: could not ask for approval: {e}")
            self._pending.pop(key, None)
            self._awaiting.pop((channel.name, chat_id), None)
            return False
        try:
            got = pending.event.wait(self.approval_timeout)
        finally:
            self._pending.pop(key, None)
            if self._awaiting.get((channel.name, chat_id)) == key:
                self._awaiting.pop((channel.name, chat_id), None)
        if not got:
            try:
                channel.send(chat_id, f"No answer — `{name}` was not run.")
            except Exception:
                pass
        return bool(got and pending.ok)

    # --- proactive / cron delivery ---
    def send_to(self, channel_name: str, chat_id: str | None, text: str) -> str:
        ch = self.channels.get(channel_name)
        if not ch:
            raise KeyError(f"no channel '{channel_name}' (have: {', '.join(self.channels) or 'none'})")
        target = chat_id or (self._active[1] if self._active and self._active[0].name == channel_name else None) or ch.owner_chat()
        if not target:
            raise ValueError(f"no chat id for '{channel_name}' and no owner configured")
        ch.send(str(target), text)
        return str(target)

    def deliver_hook(self):
        """A Scheduler delivery hook: cron `deliver = 'telegram'` (or 'telegram:<chat>') sends there."""
        def deliver(cron_row: dict, text: str, ok: bool) -> None:
            dest = str(cron_row.get("deliver") or "").strip()
            if not dest:
                return
            name, _, chat = dest.partition(":")
            if name not in self.channels:
                return
            prefix = "" if ok else "⚠️ cron failed\n"
            self.send_to(name, chat or None, f"{prefix}[{cron_row.get('name', 'cron')}]\n{text}")
        return deliver

    # --- the worker ---
    def _agent(self, channel: Channel, chat_id: str) -> Any:
        session_id = f"{channel.name}-{chat_id}"
        with self._agents_lock:
            agent = self._agents.get(session_id)
            if agent is None:
                agent = self._agents[session_id] = self.agent_for(session_id, channel.name, chat_id)
            return agent

    def _handle(self, channel: Channel, inbound: Inbound) -> None:
        agent = self._agent(channel, inbound.chat_id)
        self._active = (channel, inbound.chat_id)
        progress = Progress(channel, inbound.chat_id, log=self.log) if channel.supports_progress else None
        if progress is None and hasattr(channel, "typing"):
            channel.typing(inbound.chat_id)
        prev_events = getattr(agent, "on_event", None)
        if progress is not None:
            agent.on_event = progress.on_event
        try:
            text = self._receive_files(channel, inbound) if inbound.attachments else inbound.text
            turn = agent.run(text)
            reply = turn.text or "(no answer)"
            if turn.stopped == "max_steps":
                reply += "\n\n(stopped at the step limit)"
            elif turn.stopped == "cancelled":
                reply = "⏹ Stopped."
        except Exception as e:
            self.log(f"{channel.name}: run error: {e}")
            reply = f"Something went wrong handling that: {e}"
        finally:
            self._active = None
            if progress is not None:
                agent.on_event = prev_events
                progress.finish()
        try:
            channel.send(inbound.chat_id, reply)
        except Exception as e:
            self.log(f"{channel.name}: send error: {e}")

    def _receive_files(self, channel: Channel, inbound: Inbound) -> str:
        """Download what the owner sent into workspace/inbox/<date>/ and tell the agent what arrived."""
        from .documents import kind_of
        folder = self.workspace_root / "inbox" / time.strftime("%Y-%m-%d")
        lines = []
        for att in inbound.attachments:
            try:
                path = channel.download(att, folder)
            except Exception as e:
                self.log(f"{channel.name}: download failed: {e}")
                lines.append(f"- {att.get('name')}: could not be downloaded ({e})")
                continue
            rel = path.relative_to(self.workspace_root).as_posix() if self.workspace_root in path.parents else str(path)
            size = path.stat().st_size
            human = f"{size / 1e6:.1f} MB" if size >= 1e6 else f"{max(1, size // 1024)} KB"
            kind = kind_of(path)
            if att.get("kind") in ("voice", "audio", "video", "video_note", "animation"):
                how = f"{att['kind']} file" + (f", {att['duration']} s" if att.get("duration") else "") + \
                      " — listening/watching is not available yet; say so if it matters"
            elif kind == "image":
                how = "image — look at it with vision_analyze"
            elif kind == "archive":
                how = "archive — read_file lists it, extract_archive unpacks it (ask for a password if it needs one)"
            elif kind == "document":
                how = "document — read_file converts it to text"
            else:
                how = "read it with read_file"
            lines.append(f"- {rel} ({human}): {how}")
        ask = inbound.text.strip() or ("(The owner sent this without a message: look at it and tell them briefly "
                                       "what it is and what is in it.)")
        return f"{ask}\n\n[files the owner sent, saved in your workspace]\n" + "\n".join(lines)

    def _run(self) -> None:
        while not self._stop:
            item = self._q.get()
            if item is None:
                return
            channel, inbound = item
            files = f" [+{len(inbound.attachments)} file(s)]" if inbound.attachments else ""
            self.log(f"{channel.name}: {inbound.name or inbound.chat_id}: {inbound.text[:80]}{files}")
            with self._turn:
                self._handle(channel, inbound)

    # --- commands (run on the receiving thread, so /stop and /status work while a turn is running) ---
    def _command(self, channel: Channel, inbound: Inbound) -> bool:
        """Handle a /command. Returns False for unknown ones, which then go to the agent as text."""
        word, _, arg = inbound.text.strip().partition(" ")
        cmd = word[1:].split("@", 1)[0].lower()  # /status@mybot in groups
        handler = {"start": self._cmd_help, "help": self._cmd_help, "status": self._cmd_status,
                   "stop": self._cmd_stop, "new": self._cmd_new, "reset": self._cmd_new,
                   "model": self._cmd_model}.get(cmd)
        if handler is None:
            return False
        try:
            reply = handler(channel, inbound.chat_id, arg.strip())
        except Exception as e:
            self.log(f"{channel.name}: /{cmd} failed: {e}")
            reply = f"/{cmd} failed: {e}"
        if reply:
            try:
                channel.send(inbound.chat_id, reply)
            except Exception as e:
                self.log(f"{channel.name}: send error: {e}")
        return True

    def _busy_with(self, channel: Channel, chat_id: str) -> bool:
        a = self._active
        return bool(a and a[0].name == channel.name and a[1] == chat_id)

    def _cmd_help(self, channel: Channel, chat_id: str, arg: str) -> str:
        return HELP

    def _cmd_status(self, channel: Channel, chat_id: str, arg: str) -> str:
        from . import __version__
        agent = self._agent(channel, chat_id)
        up = int(time.time() - self._started)
        uptime = f"{up // 86400}d {up % 86400 // 3600}h" if up >= 86400 else f"{up // 3600}h {up % 3600 // 60}m"
        router = agent.router
        name = agent.provider or router.default
        prov = router.providers.get(name)
        lines = [f"🟢 Smooth Agent {__version__} · up {uptime}",
                 f"model: {name} → {prov.model if prov else '?'}" + (" (set with /model)" if agent.provider else "")]
        if router.fallbacks:
            lines.append("fallback: " + ", ".join(f"{f} → {router.providers[f].model}" for f in router.fallbacks))
        if agent.last_provider:
            lines.append(f"last answer: {agent.last_provider}")
        n = len([m for m in agent.history if m.get("role") in ("user", "assistant")])
        lines.append(f"session: {agent.session_id} · {n} messages")
        if agent.memory is not None:
            try:
                lines.append(f"memory: {agent.memory.count()} notes")
            except Exception:
                pass
        state = "working on a message" if self._busy_with(channel, chat_id) else "idle"
        waiting = self._q.qsize()
        lines.append(f"now: {state}" + (f", {waiting} waiting" if waiting else ""))
        return "\n".join(lines)

    def _cmd_stop(self, channel: Channel, chat_id: str, arg: str) -> str:
        dropped = self._drop_queued(channel, chat_id)
        key = self._awaiting.get((channel.name, chat_id))
        if key:
            self.resolve_approval(key, False)
        if self._busy_with(channel, chat_id):
            self._agent(channel, chat_id).cancel.set()
            return ""  # the turn ends with "⏹ Stopped." itself
        return "Nothing running." + (f" Dropped {dropped} waiting message(s)." if dropped else "")

    def _drop_queued(self, channel: Channel, chat_id: str) -> int:
        kept, dropped = [], 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None and item[0].name == channel.name and item[1].chat_id == chat_id:
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            self._q.put(item)
        return dropped

    def _cmd_new(self, channel: Channel, chat_id: str, arg: str) -> str:
        if not self._turn.acquire(blocking=False):
            return "Busy with a message — send /stop first, then /new."
        try:
            session_id = f"{channel.name}-{chat_id}"
            with self._agents_lock:
                agent = self._agents.pop(session_id, None)
            store = getattr(agent, "store", None) if agent is not None else None
            if store is None:  # no agent built yet in this run: ask a fresh one for its store
                store = getattr(self._agent(channel, chat_id), "store", None)
                with self._agents_lock:
                    self._agents.pop(session_id, None)
            archived = store.archive_session(session_id) if store is not None else None
            return "🆕 Fresh conversation." + (" The old one is saved and still searchable." if archived else "")
        finally:
            self._turn.release()

    def _cmd_model(self, channel: Channel, chat_id: str, arg: str) -> str:
        agent = self._agent(channel, chat_id)
        router = agent.router
        current = agent.provider or router.default
        if not arg:
            rows = [f"{'▶' if n == current else '·'} {n} → {p.model}" for n, p in router.providers.items()]
            return "Models:\n" + "\n".join(rows) + "\n\nSwitch: /model <name>   ·   back to the default: /model default"
        if not self._turn.acquire(blocking=False):
            return "Busy with a message — try again when it's done (or /stop)."
        try:
            if arg == "default":
                agent.provider = None
                return f"Back to the default: {router.default} → {router.providers[router.default].model}"
            if arg not in router.providers:
                return f"No model named '{arg}'. Have: {', '.join(router.providers)}"
            agent.provider = arg
            return f"✅ This chat now uses {arg} → {router.providers[arg].model} (until /model default or a restart)."
        finally:
            self._turn.release()

    # --- lifecycle ---
    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="channel-worker", daemon=True)
        self._worker.start()
        webhook_routes = {}
        for ch in list(self.channels.values()):
            try:
                ch.start(self)
            except Exception as e:
                self.log(f"{ch.name}: not started: {e}")
                self.channels.pop(ch.name, None)  # so send_message / delivery never target a dead channel
                continue
            if ch.webhook_path:
                webhook_routes[ch.webhook_path] = ch
        if webhook_routes:
            self._webhook = WebhookServer(self.webhook_host, self.webhook_port, webhook_routes, log=self.log)
            self._webhook.start()

    def stop(self) -> None:
        self._stop = True
        for ch in self.channels.values():
            try:
                ch.stop()
            except Exception:
                pass
        if self._webhook:
            self._webhook.stop()
        self._q.put(None)


def build_channels(specs: list[ChannelConfig], *, log: Callable[[str], None] = print) -> list[Channel]:
    out: list[Channel] = []
    for spec in specs:
        cls = CHANNEL_TYPES.get(spec.name)
        if not cls:
            log(f"unknown channel '{spec.name}' (have: {', '.join(CHANNEL_TYPES)})")
            continue
        out.append(cls(spec, log=log))
    return out


def add_send_message_tool(registry, hub: ChannelHub) -> None:
    channels = ", ".join(hub.channels) or "none configured"
    registry.tool(
        "send_message",
        "Send a message to the owner on a chat channel (" + channels + "). Use it to proactively reach the owner — a "
        "finding, a reminder, the result of long work (not for progress: in a chat the owner already sees your steps live). By default it replies in the chat you are handling; pass "
        "channel (and optionally chat_id) to target another one.",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "the message to send"},
            "channel": {"type": "string", "description": "channel name, e.g. 'telegram' (default: the current chat)"},
            "chat_id": {"type": "string", "description": "specific chat/user id (default: the current chat or the owner)"},
        }, "required": ["text"]},
    )(lambda args: _send_message(hub, args))


def _send_message(hub: ChannelHub, args: dict[str, Any]) -> str:
    from .tools import ToolError
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("text is required")
    name = str(args.get("channel") or "").strip()
    if not name:
        if hub._active:
            name = hub._active[0].name
        elif len(hub.channels) == 1:
            name = next(iter(hub.channels))
        else:
            raise ToolError(f"channel is required (one of: {', '.join(hub.channels)})")
    try:
        sent = hub.send_to(name, str(args.get("chat_id") or "") or None, text)
    except (KeyError, ValueError) as e:
        raise ToolError(str(e)) from None
    return f"sent to {name} ({sent})"


__all__ = ["Inbound", "ChannelConfig", "Channel", "TelegramChannel", "WhatsAppChannel", "WeChatChannel",
           "SlackChannel", "MatrixChannel", "LineChannel", "DiscordChannel", "WebhookServer", "ChannelHub",
           "CHANNEL_TYPES", "build_channels", "add_send_message_tool", "chunk", "header"]
