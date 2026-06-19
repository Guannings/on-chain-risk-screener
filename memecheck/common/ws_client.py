"""Minimal stdlib-only WebSocket client (RFC 6455).

Purpose: subscribe to public exchange streams (Hyperliquid first) for
sub-second mark price / trade flow updates, without taking a runtime
dependency on `websockets`, `aiohttp`, or any other PyPI package.

What's implemented:
  - Opening handshake over TLS (Sec-WebSocket-Key generation + Accept validation)
  - Text-frame parsing (opcode 0x1)
  - Outbound text-frame writing with required client masking
  - Ping → automatic pong response
  - Close frame handling
  - Payload-length encodings 7-bit, 16-bit, 64-bit

What's NOT implemented (deliberate scope cut):
  - Permessage-deflate extension
  - Continuation frames across multiple frames (a single text message must
    fit in one frame). Exchange streams comfortably meet this in practice.
  - Binary frames (Hyperliquid uses text/JSON)
  - Subprotocols

Design split:
  - `parse_frame(buf) -> (frame_or_none, bytes_consumed)`   PURE, tested without network.
  - `build_text_frame(payload, mask_key) -> bytes`          PURE.
  - `WebSocket` class                                       socket+ssl, blocking.

The blocking model fits the existing monitor architecture (sources are
generators yielding events). A `for msg in ws.messages():` loop is the
intended usage; the caller can run it in a thread next to the existing
asyncio loop.

References:
  RFC 6455 — The WebSocket Protocol
  https://datatracker.ietf.org/doc/html/rfc6455
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple
from urllib.parse import urlparse


# ----------------------------- frame parsing -----------------------------


OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BIN = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


@dataclass(frozen=True)
class Frame:
    """One parsed WebSocket frame."""
    fin: bool
    opcode: int
    payload: bytes


def parse_frame(buf: bytes) -> Tuple[Optional[Frame], int]:
    """Try to parse a single frame from the front of `buf`.

    Returns (frame, bytes_consumed). If the buffer doesn't yet contain
    a complete frame, returns (None, 0) — caller should read more and
    retry. Pure, doesn't touch any I/O.
    """
    if len(buf) < 2:
        return None, 0
    b0 = buf[0]
    b1 = buf[1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    payload_len = b1 & 0x7F
    offset = 2

    if payload_len == 126:
        if len(buf) < offset + 2:
            return None, 0
        (payload_len,) = struct.unpack(">H", buf[offset:offset + 2])
        offset += 2
    elif payload_len == 127:
        if len(buf) < offset + 8:
            return None, 0
        (payload_len,) = struct.unpack(">Q", buf[offset:offset + 8])
        offset += 8

    if masked:
        if len(buf) < offset + 4:
            return None, 0
        mask_key = buf[offset:offset + 4]
        offset += 4
    else:
        mask_key = None

    if len(buf) < offset + payload_len:
        return None, 0

    payload = buf[offset:offset + payload_len]
    if mask_key is not None:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return Frame(fin=fin, opcode=opcode, payload=bytes(payload)), offset + payload_len


def build_text_frame(payload: bytes, mask_key: Optional[bytes] = None) -> bytes:
    """Build an outbound text frame. Clients MUST mask per RFC 6455 §5.3."""
    if mask_key is None:
        mask_key = os.urandom(4)
    if len(mask_key) != 4:
        raise ValueError("mask_key must be 4 bytes")

    head = bytes([0x80 | OPCODE_TEXT])      # FIN + text opcode
    length = len(payload)
    if length < 126:
        head += bytes([0x80 | length])
    elif length < (1 << 16):
        head += bytes([0x80 | 126]) + struct.pack(">H", length)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", length)
    head += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return head + masked


def build_pong_frame(payload: bytes, mask_key: Optional[bytes] = None) -> bytes:
    """Build a pong control frame (used to answer server pings)."""
    if mask_key is None:
        mask_key = os.urandom(4)
    head = bytes([0x80 | OPCODE_PONG, 0x80 | len(payload)]) + mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return head + masked


def build_close_frame(mask_key: Optional[bytes] = None) -> bytes:
    """Build a clean close frame with status code 1000."""
    if mask_key is None:
        mask_key = os.urandom(4)
    payload = struct.pack(">H", 1000)
    head = bytes([0x80 | OPCODE_CLOSE, 0x80 | len(payload)]) + mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return head + masked


# ----------------------------- handshake helpers -------------------------


_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _generate_key() -> bytes:
    """Per RFC 6455 §4.1: 16 random bytes, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(16))


def _expected_accept(key: bytes) -> str:
    return base64.b64encode(
        hashlib.sha1(key + _WS_MAGIC.encode("ascii")).digest()
    ).decode("ascii")


# ----------------------------- client class ------------------------------


class WebSocketError(Exception):
    pass


class WebSocket:
    """Blocking RFC 6455 client. Use as a context manager:

        with WebSocket("wss://api.hyperliquid.xyz/ws") as ws:
            ws.send_text('{"method":"subscribe","subscription":{...}}')
            for msg in ws.messages():
                print(msg)
                if some_condition:
                    break
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parsed.scheme}")
        self._host = parsed.hostname or ""
        self._port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self._path = parsed.path or "/"
        if parsed.query:
            self._path += "?" + parsed.query
        self._tls = parsed.scheme == "wss"
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        if self._tls:
            ctx = ssl.create_default_context()
            sock: socket.socket = ctx.wrap_socket(raw, server_hostname=self._host)
        else:
            sock = raw

        key = _generate_key()
        req = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key.decode('ascii')}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: memecheck-ws/0.6\r\n"
            f"\r\n"
        )
        sock.sendall(req.encode("ascii"))

        # Read response headers (up to the blank line).
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("server closed during handshake")
            resp += chunk
            if len(resp) > 16384:
                raise WebSocketError("handshake response too large")

        head, _, leftover = resp.partition(b"\r\n\r\n")
        status_line, _, headers = head.decode("iso-8859-1").partition("\r\n")
        if " 101 " not in status_line:
            raise WebSocketError(f"unexpected handshake status: {status_line!r}")

        # Verify Sec-WebSocket-Accept.
        accept_header = None
        for line in headers.split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept_header = value.strip()
                break
        if accept_header != _expected_accept(key):
            raise WebSocketError("Sec-WebSocket-Accept mismatch")

        self._sock = sock
        self._buf = leftover    # any frame bytes piggy-backed on the handshake

    def send_text(self, payload: str) -> None:
        if self._sock is None:
            raise WebSocketError("not connected")
        self._sock.sendall(build_text_frame(payload.encode("utf-8")))

    def _recv_some(self) -> None:
        assert self._sock is not None
        chunk = self._sock.recv(8192)
        if not chunk:
            raise WebSocketError("server closed unexpectedly")
        self._buf += chunk

    def messages(self) -> Iterator[str]:
        """Yield text payloads as decoded strings. Handles ping/pong/close
        transparently. Caller breaks the loop to stop."""
        assert self._sock is not None
        while True:
            frame, consumed = parse_frame(self._buf)
            if frame is None:
                self._recv_some()
                continue
            self._buf = self._buf[consumed:]

            if frame.opcode == OPCODE_PING:
                self._sock.sendall(build_pong_frame(frame.payload))
                continue
            if frame.opcode == OPCODE_PONG:
                continue
            if frame.opcode == OPCODE_CLOSE:
                return
            if frame.opcode == OPCODE_TEXT:
                yield frame.payload.decode("utf-8", errors="replace")
            # ignore binary / unknown opcodes

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendall(build_close_frame())
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
