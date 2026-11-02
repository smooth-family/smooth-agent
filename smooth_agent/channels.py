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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
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


@dataclass
class Inbound:
    channel: str
    chat_id: str          # where to reply
    text: str
    user_id: str = ""     # who sent it
    name: str = ""        # display name, for logs
    raw: dict = field(default_factory=dict)


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

    def _api(self, method: str, payload: dict, *, timeout: float = 65.0) -> dict:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        _status, data = _post_json(url, payload, timeout=timeout)
        return data

    def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        parts = chunk(text)
        for i, part in enumerate(parts):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
            data = self._api("sendMessage", payload)
            if not data.get("ok"):  # e.g. 403 "bot can't initiate conversation": the owner never pressed Start
                raise RuntimeError(f"telegram sendMessage: {data.get('description') or data}")

    def typing(self, chat_id: str) -> None:
        try:
            self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass

    def start(self, hub: "ChannelHub") -> None:
        super().start(hub)
        if not self.token:
            raise RuntimeError("telegram: no bot token (set the token env in [channels.telegram])")
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
                          raw=msg)
        if self.hub:
            self.hub.dispatch(self, inbound)


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


class ChannelHub:
    """Runs the channels and routes their messages to per-chat agent sessions.

    `agent_for(session_id, channel, chat_id)` must return a ready Agent (built on
    the shared runtime). Messages are handled one at a time by a single worker
    thread so the model is never hit by many requests at once and so tool
    approvals and `send_message` always target the chat being handled.
    """

    def __init__(self, channels: list[Channel], agent_for: Callable[[str, str, str], Any], *,
                 log: Callable[[str], None] = print, approval_timeout: float = 180.0,
                 webhook_host: str = "0.0.0.0", webhook_port: int = 8808):
        self.channels = {c.name: c for c in channels}
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

    # --- intake ---
    def dispatch(self, channel: Channel, inbound: Inbound) -> None:
        if not inbound.text.strip():
            return
        if not channel.is_owner(inbound):
            self.log(f"{channel.name}: ignored {inbound.chat_id} ({inbound.name})")
            try:
                channel.send(inbound.chat_id, "This Smooth Agent is private. To use it, add this id to the channel's "
                             f"owners list:\n{inbound.chat_id}")
            except Exception:
                pass
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
        preview = json.dumps(args, ensure_ascii=False)
        if len(preview) > 500:
            preview = preview[:500] + "…"
        key = uuid.uuid4().hex[:12]
        pending = self._pending[key] = _Pending(threading.Event())
        text = f"Run `{name}`?\n{preview}"
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
    def _handle(self, channel: Channel, inbound: Inbound) -> None:
        session_id = f"{channel.name}-{inbound.chat_id}"
        agent = self._agents.get(session_id)
        if agent is None:
            agent = self._agents[session_id] = self.agent_for(session_id, channel.name, inbound.chat_id)
        self._active = (channel, inbound.chat_id)
        typing = getattr(channel, "typing", None)
        if typing:
            typing(inbound.chat_id)
        try:
            turn = agent.run(inbound.text)
            reply = turn.text or "(no answer)"
            if turn.stopped == "max_steps":
                reply += "\n\n(stopped at the step limit)"
        except Exception as e:
            self.log(f"{channel.name}: run error: {e}")
            reply = f"Something went wrong handling that: {e}"
        finally:
            self._active = None
        try:
            channel.send(inbound.chat_id, reply)
        except Exception as e:
            self.log(f"{channel.name}: send error: {e}")

    def _run(self) -> None:
        while not self._stop:
            item = self._q.get()
            if item is None:
                return
            channel, inbound = item
            self.log(f"{channel.name}: {inbound.name or inbound.chat_id}: {inbound.text[:80]}")
            self._handle(channel, inbound)

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
        "finding, a reminder, the result of long work. By default it replies in the chat you are handling; pass "
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
