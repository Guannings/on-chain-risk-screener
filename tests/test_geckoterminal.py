"""GeckoTerminal fallback — adapter + multi-source dispatch."""

from __future__ import annotations

import pytest

from memecheck.common import sources as src_mod
from memecheck.common import geckoterminal as gt_mod
from memecheck.common.geckoterminal import (
    CHAIN_DS_TO_GT,
    _gt_pool_to_ds_pair,
    _parse_iso_to_ms,
    _split_name,
    _split_token_id,
    fetch_geckoterminal,
)
from memecheck.common.sources import fetch_dex_pairs


# ----------------------------- pure helpers ------------------------------


def test_split_token_id_strips_network_prefix() -> None:
    assert _split_token_id("solana_EKpQGSJ...") == "EKpQGSJ..."
    assert _split_token_id("eth_0xabc123") == "0xabc123"
    assert _split_token_id(None) is None


def test_split_name_handles_slash_separator() -> None:
    assert _split_name("$WIF / SOL") == ("$WIF", "SOL")
    assert _split_name("ETH / USDC") == ("ETH", "USDC")
    assert _split_name(None) == (None, None)
    assert _split_name("nope") == (None, None)


def test_parse_iso_handles_z_suffix() -> None:
    ts = _parse_iso_to_ms("2023-11-20T20:10:04Z")
    assert ts is not None
    assert 1_700_000_000_000 < ts < 1_900_000_000_000


def test_parse_iso_handles_garbage() -> None:
    assert _parse_iso_to_ms(None) is None
    assert _parse_iso_to_ms("nope") is None


# ----------------------------- pool → DS-pair --------------------------


_SAMPLE_GT_POOL: dict = {
    "attributes": {
        "address": "POOL_ADDR",
        "name": "$WIF / SOL",
        "base_token_price_usd": "0.163",
        "quote_token_price_usd": "68.7",
        "base_token_price_native_currency": "0.002361",
        "reserve_in_usd": "4089592.7",
        "fdv_usd": "163000000",
        "market_cap_usd": "162000000",
        "volume_usd": {"h1": "4514.6", "h24": "123745.0"},
        "transactions": {"h24": {"buys": 1500, "sells": 1400}},
        "pool_created_at": "2023-11-20T20:10:04Z",
    },
    "relationships": {
        "base_token": {"data": {"id": "solana_WIF_MINT"}},
        "quote_token": {"data": {"id": "solana_So11..."}},
        "dex": {"data": {"id": "raydium"}},
    },
}


def test_gt_pool_converts_to_ds_shape() -> None:
    pair = _gt_pool_to_ds_pair(_SAMPLE_GT_POOL, "solana")
    assert pair is not None
    assert pair["chainId"] == "solana"
    assert pair["pairAddress"] == "POOL_ADDR"
    assert pair["dexId"] == "raydium"
    assert pair["baseToken"]["symbol"] == "$WIF"
    assert pair["quoteToken"]["symbol"] == "SOL"
    assert float(pair["priceUsd"]) == pytest.approx(0.163)
    assert float(pair["priceNative"]) == pytest.approx(0.002361)
    assert pair["liquidity"]["usd"] == pytest.approx(4089592.7)
    assert pair["volume"]["h24"] == pytest.approx(123745.0)
    assert pair["txns"]["h24"]["buys"] == 1500


def test_gt_pool_rejects_pool_without_price() -> None:
    bad = {
        "attributes": {"reserve_in_usd": "100", "name": "X / Y"},
        "relationships": {},
    }
    assert _gt_pool_to_ds_pair(bad, "solana") is None


# ----------------------------- end-to-end fallback ----------------------


def test_fetch_geckoterminal_returns_deepest(monkeypatch) -> None:
    """When GT returns multiple pools, the adapter sorts by depth desc."""
    def fake_get_json(url, timeout=15):
        return {"data": [
            {"attributes": {
                "address": "SHALLOW",
                "name": "TOK / SOL",
                "base_token_price_usd": "0.1",
                "base_token_price_native_currency": "0.001",
                "reserve_in_usd": "1000",
            }, "relationships": {}},
            {"attributes": {
                "address": "DEEP",
                "name": "TOK / SOL",
                "base_token_price_usd": "0.1",
                "base_token_price_native_currency": "0.001",
                "reserve_in_usd": "1000000",
            }, "relationships": {}},
        ]}
    monkeypatch.setattr(gt_mod, "get_json", fake_get_json)
    primary, pairs, err = fetch_geckoterminal("MINT", forced_chain="solana")
    assert err is None
    assert primary is not None
    assert primary["pairAddress"] == "DEEP"
    assert len(pairs) == 2


def test_fetch_dex_pairs_falls_back_to_gt(monkeypatch) -> None:
    """fetch_dex_pairs hits GT when DexScreener returns an empty result."""
    monkeypatch.setattr(
        src_mod, "_try_dexscreener",
        lambda a, c=None: (None, [], "DexScreener: empty"),
    )
    gt_result = (
        {"_source": "geckoterminal", "pairAddress": "FROM_GT",
         "chainId": "solana", "liquidity": {"usd": 100_000.0}},
        [{"pairAddress": "FROM_GT"}],
        None,
    )
    monkeypatch.setattr(src_mod, "_try_geckoterminal", lambda a, c=None: gt_result)

    primary, pairs, err = fetch_dex_pairs("MINT")
    assert primary is not None
    assert primary["pairAddress"] == "FROM_GT"
    assert primary["_source"] == "geckoterminal"


def test_fetch_dex_pairs_uses_ds_when_available(monkeypatch) -> None:
    """When DexScreener returns data, GT must NOT be called."""
    monkeypatch.setattr(
        src_mod, "_try_dexscreener",
        lambda a, c=None: (
            {"pairAddress": "FROM_DS", "chainId": "solana"}, [], None,
        ),
    )
    def fail_gt(a, c=None):
        raise AssertionError("should not call GT when DS succeeds")
    monkeypatch.setattr(src_mod, "_try_geckoterminal", fail_gt)

    primary, _pairs, err = fetch_dex_pairs("MINT")
    assert primary is not None
    assert primary["pairAddress"] == "FROM_DS"
    # _source gets tagged automatically.
    assert primary["_source"] == "dexscreener"


def test_chain_slug_translation_covers_majors() -> None:
    assert CHAIN_DS_TO_GT["ethereum"] == "eth"
    assert CHAIN_DS_TO_GT["solana"] == "solana"
    assert CHAIN_DS_TO_GT["base"] == "base"
