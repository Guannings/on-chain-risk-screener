"""Raydium AMM v4 layout decoder tests."""

from __future__ import annotations

import struct

import pytest

from memecheck.common import raydium as r_mod
from memecheck.common.raydium import (
    AMM_V4_ACCOUNT_LEN,
    decode_raydium_pool,
    parse_amm_v4_layout,
)
from memecheck.common.solana_rpc import base58_decode, base58_encode


def _build_layout_buf(
    base_mint: str = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",   # WIF
    quote_mint: str = "So11111111111111111111111111111111111111112",     # SOL
    base_vault: str = "7UYZ4vX13mmGiopayLZAduo8aie77yZ3o8FMzTeAX8uJ",
    quote_vault: str = "7e9ExBAvDvuJP3GE6eKL5aSMi4RfXv3LkQaiNZBPmffR",
    base_decimals: int = 6,
    quote_decimals: int = 9,
) -> bytes:
    """Build a synthetic AMM v4 buffer with the four pubkeys + two decimals
    at their real offsets. Other bytes left as zeroes."""
    buf = bytearray(AMM_V4_ACCOUNT_LEN)
    struct.pack_into("<Q", buf, 32, base_decimals)
    struct.pack_into("<Q", buf, 40, quote_decimals)
    buf[336:368] = base58_decode(base_vault)
    buf[368:400] = base58_decode(quote_vault)
    buf[400:432] = base58_decode(base_mint)
    buf[432:464] = base58_decode(quote_mint)
    return bytes(buf)


def test_parse_returns_none_for_short_buffer() -> None:
    assert parse_amm_v4_layout(b"\x00" * 100) is None


def test_parse_extracts_known_wif_pool() -> None:
    """Live numbers verified against the on-chain WIF/SOL pool."""
    buf = _build_layout_buf()
    parsed = parse_amm_v4_layout(buf)
    assert parsed is not None
    assert parsed["base_mint"] == "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    assert parsed["quote_mint"] == "So11111111111111111111111111111111111111112"
    assert parsed["base_vault"] == "7UYZ4vX13mmGiopayLZAduo8aie77yZ3o8FMzTeAX8uJ"
    assert parsed["quote_vault"] == "7e9ExBAvDvuJP3GE6eKL5aSMi4RfXv3LkQaiNZBPmffR"
    assert parsed["base_decimals"] == 6
    assert parsed["quote_decimals"] == 9


def test_decode_returns_none_for_non_raydium_owner(monkeypatch) -> None:
    """A pool owned by some other program (e.g. CPMM) must be rejected."""
    import base64
    buf = _build_layout_buf()
    monkeypatch.setattr(
        r_mod, "get_account_info",
        lambda pk: {
            "owner": "SOME_OTHER_PROGRAM_ID_NOT_AMM_V4",
            "data": [base64.b64encode(buf).decode("ascii"), "base64"],
        },
    )
    assert decode_raydium_pool("WHATEVER") is None


def test_decode_assembles_pool_object_end_to_end(monkeypatch) -> None:
    """Stub both RPC calls and verify the assembled RaydiumPool dataclass."""
    import base64
    buf = _build_layout_buf()

    monkeypatch.setattr(
        r_mod, "get_account_info",
        lambda pk: {
            "owner": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # AMM v4
            "data": [base64.b64encode(buf).decode("ascii"), "base64"],
        },
    )
    # Vault balances — pretend WIF=12_597_887 (6 dec), SOL=29_787 (9 dec).
    def fake_token_balance(pk):
        if pk == "7UYZ4vX13mmGiopayLZAduo8aie77yZ3o8FMzTeAX8uJ":
            return {"amount": "12597887004516", "decimals": 6, "uiAmount": 12597887.004516}
        return {"amount": "29787244651521", "decimals": 9, "uiAmount": 29787.244651521}
    monkeypatch.setattr(r_mod, "get_token_account_balance", fake_token_balance)

    pool = decode_raydium_pool("EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx")
    assert pool is not None
    assert pool.base_mint == "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    assert pool.quote_mint == "So11111111111111111111111111111111111111112"
    assert pool.base_reserve_ui == pytest.approx(12_597_887.004516)
    assert pool.quote_reserve_ui == pytest.approx(29_787.244651521)


def test_decode_handles_missing_uiAmount(monkeypatch) -> None:
    """Some RPCs don't include uiAmount — fall back to raw / 10^decimals."""
    import base64
    buf = _build_layout_buf()
    monkeypatch.setattr(
        r_mod, "get_account_info",
        lambda pk: {
            "owner": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
            "data": [base64.b64encode(buf).decode("ascii"), "base64"],
        },
    )
    monkeypatch.setattr(
        r_mod, "get_token_account_balance",
        lambda pk: {"amount": "1000000000", "decimals": 6},
    )
    pool = decode_raydium_pool("X")
    assert pool is not None
    assert pool.base_reserve_ui == pytest.approx(1000.0)
    assert pool.quote_reserve_ui == pytest.approx(1.0)    # 10^9 / 10^9
