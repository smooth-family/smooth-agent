"""A small WebSocket client (RFC 6455), standard library only.

Enough for chat gateways (Discord): TLS, the HTTP upgrade handshake, masked
client frames, fragmented messages, ping/pong and close. Text and binary
messages come back from `recv()`; `send_text()` is safe to call from another
thread (e.g. a heartbeat).
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 32 * 1024 * 1024  # a gateway READY can be large; anything past this is refused


class WebSocketClosed(Exception):
    def __init__(self, code: int | None = None, reason: str = ""):
        super().__init__(f"websocket closed ({code}{': ' + reason if reason else ''})")
        self.code, self.reason = code, reason


class WebSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buf = b""
        self.closed = False

    # --- connect ---
    @classmethod
    def connect(cls, url: str, *, timeout: float = 30.0, headers: dict[str, str] | None = None) -> "WebSocket":
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError(f"not a websocket URL: {url}")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        sock: socket.socket = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        host_header = host if u.port is None else f"{host}:{port}"
        lines = [f"GET {path} HTTP/1.1", f"Host: {host_header}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "User-Agent: SmoothAgent"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        ws = cls(sock)
        head = ws._read_until(b"\r\n\r\n", limit=16384)
        status, _, rest = head.decode("latin-1").partition("\r\n")
        if " 101 " not in status + " ":
            sock.close()
            raise ConnectionError(f"websocket handshake failed: {status.strip()}")
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        got = next((ln.split(":", 1)[1].strip() for ln in rest.split("\r\n")
                    if ln.lower().startswith("sec-websocket-accept:")), "")
        if got != want:
            sock.close()
            raise ConnectionError("websocket handshake: bad Sec-WebSocket-Accept")
        sock.settimeout(None)
        return ws

    # --- low level reads ---
    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > limit:
                raise ConnectionError("websocket: header too long")
            data = self.sock.recv(4096)
            if not data:
                raise WebSocketClosed(None, "connection lost during handshake")
            self._buf += data
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            data = self.sock.recv(max(65536, n - len(self._buf)))
            if not data:
                self.closed = True
                raise WebSocketClosed(None, "connection lost")
            self._buf += data
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        masked, n = bool(b2 & 0x80), b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        if n > MAX_MESSAGE:
            raise WebSocketClosed(1009, "message too big")
        mask = self._read(4) if masked else b""
        data = self._read(n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    # --- API ---
    def recv(self) -> str | bytes:
        """The next text (str) or binary (bytes) message. Answers pings; raises WebSocketClosed on close."""
        parts: list[bytes] = []
        first_op = None
        while True:
            fin, op, data = self._frame()
            if op == 0x9:  # ping
                self._send(0xA, data)
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x8:  # close
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                reason = data[2:].decode("utf-8", "replace")
                try:
                    self._send(0x8, data[:2])
                except OSError:
                    pass
                self.closed = True
                raise WebSocketClosed(code, reason)
            if op in (0x1, 0x2):
                first_op, parts = op, [data]
            elif op == 0x0 and first_op is not None:
                parts.append(data)
                if sum(len(p) for p in parts) > MAX_MESSAGE:
                    raise WebSocketClosed(1009, "message too big")
            else:
                continue
            if fin:
                msg = b"".join(parts)
                return msg.decode("utf-8") if first_op == 0x1 else msg

    def _send(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(head + mask + body)

    def send_text(self, text: str) -> None:
        if self.closed:
            raise WebSocketClosed(None, "already closed")
        self._send(0x1, text.encode("utf-8"))

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._send(0x8, struct.pack("!H", code))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


__all__ = ["WebSocket", "WebSocketClosed"]
