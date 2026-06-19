"""Base58 codec + RPC transport tests (no network for unit, live for smoke)."""

from __future__ import annotations

import pytest

from memecheck.common.solana_rpc import (
    base58_decode,
    base58_encode,
)


# ----------------------------- Base58 codec ------------------------------


def test_base58_round_trips_wrapped_sol_pubkey() -> None:
    """Decode the well-known wrapped-SOL pubkey, re-encode, expect identity."""
    expected = "So11111111111111111111111111111111111111112"
    raw = base58_decode(expected)
    assert len(raw) == 32                          # Solana pubkey
    assert base58_encode(raw) == expected


def test_base58_preserves_leading_zero_bytes() -> None:
    """Leading 0x00 bytes become leading '1' chars (Bitcoin convention)."""
    data = b"\x00\x00\xff"
    s = base58_encode(data)
    assert s.startswith("11")
    assert base58_decode(s) == data


def test_base58_decode_rejects_bad_char() -> None:
    with pytest.raises(ValueError):
        base58_decode("hello-world")    # '-' is not in alphabet


def test_base58_random_round_trip() -> None:
    """Round-trip a few random byte strings of various lengths."""
    import os
    for n in (1, 8, 16, 32, 64, 128):
        data = os.urandom(n)
        assert base58_decode(base58_encode(data)) == data


def test_base58_encodes_empty_bytes() -> None:
    assert base58_encode(b"") == ""
    assert base58_decode("") == b""
