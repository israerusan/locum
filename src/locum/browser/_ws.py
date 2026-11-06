"""A minimal WebSocket client (RFC 6455), standard library only.

Just what a Chrome DevTools Protocol session needs: a ``ws://`` connection
(DevTools listens on localhost only), masked text frames out, text frames
(fragmented or not) in, ping/pong and close. No TLS, no extensions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Optional, Tuple
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(OSError):
    """The WebSocket handshake or stream failed (or the peer closed it)."""


class WebSocket:
    """A connected client WebSocket. ``send(text)`` / ``recv(timeout)``; thread-safe sends."""

    MAX_MESSAGE = 256 * 1024 * 1024

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise WebSocketError(f"only ws:// URLs are supported, got {url!r}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._send_lock = threading.Lock()
        self._buf = b""
        self.closed = False
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self._sock.sendall(req.encode("ascii"))
        head = self._read_until(b"\r\n\r\n", limit=65536).decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            self._sock.close()
            raise WebSocketError(f"WebSocket handshake refused: {status!r}")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        accept = ""
        for line in head.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "sec-websocket-accept":
                accept = v.strip()
        if accept != expected:
            self._sock.close()
            raise WebSocketError("WebSocket handshake: bad Sec-WebSocket-Accept")

    # ----------------------------------------------------------------- io
    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > limit:
                raise WebSocketError("WebSocket handshake response too long")
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed during the WebSocket handshake")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read_exact(self, n: int) -> bytes:
        out = bytearray()
        if self._buf:
            take = self._buf[:n]
            out += take
            self._buf = self._buf[len(take):]
        while len(out) < n:
            chunk = self._sock.recv(min(1 << 20, n - len(out)))
            if not chunk:
                self.closed = True
                raise WebSocketError("WebSocket connection closed")
            out += chunk
        return bytes(out)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if n < 4096 else _mask(payload, mask)
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        if self.closed:
            raise WebSocketError("WebSocket is closed")
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _recv_frame(self) -> Tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8))[0]
        if n > self.MAX_MESSAGE:
            raise WebSocketError(f"WebSocket frame of {n} bytes is too large")
        mask = self._read_exact(4) if b2 & 0x80 else None
        data = self._read_exact(n)
        if mask:
            data = _mask(data, mask)
        return fin, opcode, data

    def recv(self, timeout: Optional[float] = None) -> str:
        """The next text message; raises ``socket.timeout`` after ``timeout`` seconds."""
        self._sock.settimeout(timeout)
        parts = []
        while True:
            fin, opcode, data = self._recv_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                try:
                    self._send_frame(OP_CLOSE, data[:2])
                except OSError:
                    pass
                raise WebSocketError("WebSocket closed by the peer")
            parts.append(data)
            if sum(len(p) for p in parts) > self.MAX_MESSAGE:
                raise WebSocketError("WebSocket message too large")
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "little") if n else 0
    return (int.from_bytes(data, "little") ^ key).to_bytes(n, "little") if n else b""


__all__ = ["WebSocket", "WebSocketError"]
