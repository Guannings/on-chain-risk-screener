"""WebSocket client frame-layer tests (no network).

The socket / TLS side is exercised by live `hl-stream` runs against
Hyperliquid; these tests cover the pure RFC 6455 frame parser and
builder so we trust the bit-twiddling without round-tripping over the
network.
"""

from __future__ import annotations

import struct

import pytest

from memecheck.common.ws_client import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    _expected_accept,
    build_close_frame,
    build_pong_frame,
    build_text_frame,
    parse_frame,
)


# ----------------------------- parse_frame -------------------------------


def test_parse_short_text_frame_from_server() -> None:
    """A 5-byte text payload sent unmasked (server → client direction)."""
    payload = b"hello"
    frame_bytes = bytes([0x80 | OPCODE_TEXT, len(payload)]) + payload
    frame, consumed = parse_frame(frame_bytes)
    assert frame is not None
    assert frame.fin is True
    assert frame.opcode == OPCODE_TEXT
    assert frame.payload == payload
    assert consumed == len(frame_bytes)


def test_parse_medium_payload_uses_16bit_length() -> None:
    payload = b"x" * 300
    head = bytes([0x80 | OPCODE_TEXT, 126]) + struct.pack(">H", 300)
    frame, consumed = parse_frame(head + payload)
    assert frame is not None
    assert frame.payload == payload
    assert consumed == len(head) + 300


def test_parse_large_payload_uses_64bit_length() -> None:
    payload = b"y" * (1 << 16)
    head = bytes([0x80 | OPCODE_TEXT, 127]) + struct.pack(">Q", 1 << 16)
    frame, consumed = parse_frame(head + payload)
    assert frame is not None
    assert frame.payload == payload
    assert consumed == len(head) + (1 << 16)


def test_parse_incomplete_buffer_returns_none() -> None:
    """A truncated frame should return (None, 0) so the caller can read more."""
    head = bytes([0x80 | OPCODE_TEXT, 10])    # claims 10 bytes…
    frame, consumed = parse_frame(head + b"only4")
    assert frame is None
    assert consumed == 0


def test_parse_handles_masked_payload() -> None:
    """Even though servers shouldn't mask, the parser must un-mask if MASK
    bit is set — used when we feed back our own outbound frames in tests."""
    payload = b"hello"
    mask = bytes([0xAA, 0xBB, 0xCC, 0xDD])
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame_bytes = bytes([0x80 | OPCODE_TEXT, 0x80 | len(payload)]) + mask + masked
    frame, _ = parse_frame(frame_bytes)
    assert frame is not None
    assert frame.payload == payload


def test_parse_recognises_control_opcodes() -> None:
    for opcode in (OPCODE_PING, OPCODE_PONG, OPCODE_CLOSE):
        frame_bytes = bytes([0x80 | opcode, 0])
        frame, _ = parse_frame(frame_bytes)
        assert frame is not None
        assert frame.opcode == opcode


# ----------------------------- build_*_frame -----------------------------


def test_build_text_frame_round_trips() -> None:
    """A built text frame should parse back to the same payload."""
    msg = b'{"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}'
    out = build_text_frame(msg)
    frame, _ = parse_frame(out)
    assert frame is not None
    assert frame.opcode == OPCODE_TEXT
    assert frame.payload == msg


def test_build_text_frame_handles_medium_payload() -> None:
    """Crosses the 126-byte threshold → must use 16-bit length encoding."""
    payload = b"a" * 200
    out = build_text_frame(payload)
    frame, _ = parse_frame(out)
    assert frame is not None
    assert frame.payload == payload


def test_build_pong_includes_request_payload() -> None:
    """Pong must echo the ping's payload per RFC 6455 §5.5.3."""
    ping_payload = b"ping123"
    out = build_pong_frame(ping_payload)
    frame, _ = parse_frame(out)
    assert frame is not None
    assert frame.opcode == OPCODE_PONG
    assert frame.payload == ping_payload


def test_build_close_frame_uses_status_1000() -> None:
    out = build_close_frame()
    frame, _ = parse_frame(out)
    assert frame is not None
    assert frame.opcode == OPCODE_CLOSE
    # First two payload bytes = status code, big-endian
    code = struct.unpack(">H", frame.payload[:2])[0]
    assert code == 1000


# ----------------------------- handshake helpers ------------------------


def test_expected_accept_matches_rfc_example() -> None:
    """RFC 6455 §1.3 example: key 'dGhlIHNhbXBsZSBub25jZQ==' →
    accept 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='"""
    accept = _expected_accept(b"dGhlIHNhbXBsZSBub25jZQ==")
    assert accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
