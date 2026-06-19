"""Jupiter quote API integration — mocked HTTP, no live network."""

from __future__ import annotations

import math

import pytest

from memecheck.common import jupiter as jup_mod
from memecheck.common.jupiter import (
    SOLANA_QUOTE_DECIMALS,
    JupiterQuote,
    estimate_realistic_buy_for_solana,
    fetch_jupiter_quote,
)


def test_decimals_table_has_majors() -> None:
    # SOL / USDC / USDT
    assert "So11111111111111111111111111111111111111112" in SOLANA_QUOTE_DECIMALS
    assert "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" in SOLANA_QUOTE_DECIMALS
    assert "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB" in SOLANA_QUOTE_DECIMALS


def test_fetch_jupiter_quote_parses_response(monkeypatch) -> None:
    def fake_get_json(url: str, timeout: int = 15):
        return {
            "inputMint": "InMint",
            "outputMint": "OutMint",
            "inAmount": "1000000000",
            "outAmount": "421613960",
            "priceImpactPct": "0.00016951",
            "routePlan": [{"swapInfo": {"label": "Raydium"}, "percent": 100}],
        }
    monkeypatch.setattr(jup_mod, "get_json", fake_get_json)
    q = fetch_jupiter_quote("InMint", "OutMint", 1_000_000_000)
    assert q is not None
    assert isinstance(q, JupiterQuote)
    assert q.out_amount == 421_613_960
    # priceImpactPct in Jupiter is decimal; we multiply by 100.
    assert math.isclose(q.price_impact_pct, 0.016951, rel_tol=1e-4)
    assert q.route_hops == 1


def test_fetch_jupiter_quote_returns_none_on_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        jup_mod, "get_json",
        lambda url, timeout=15: {"_error": "HTTP 503 service unavailable"},
    )
    assert fetch_jupiter_quote("A", "B", 1_000) is None


def test_fetch_jupiter_quote_returns_none_on_zero_amount() -> None:
    assert fetch_jupiter_quote("A", "B", 0) is None
    assert fetch_jupiter_quote("A", "B", -100) is None


def test_estimate_realistic_buy_converts_usd_to_atomic(monkeypatch) -> None:
    """A $100 buy with SOL at $200 → 0.5 SOL → 500_000_000 lamports."""
    captured = {}
    def fake_get_json(url: str, timeout: int = 15):
        # Verify the URL embeds the amount we computed.
        captured["url"] = url
        return {
            "inputMint": "SOL", "outputMint": "TOKEN",
            "inAmount": "500000000", "outAmount": "10000000",
            "priceImpactPct": "0.001", "routePlan": [{}],
        }
    monkeypatch.setattr(jup_mod, "get_json", fake_get_json)
    q = estimate_realistic_buy_for_solana(
        base_mint="TOKEN",
        quote_mint="So11111111111111111111111111111111111111112",  # SOL
        buy_size_usd=100.0,
        quote_price_usd=200.0,
    )
    assert q is not None
    assert "amount=500000000" in captured["url"]


def test_estimate_realistic_buy_returns_none_for_unknown_quote_mint() -> None:
    """A mint we don't have decimals for is rejected (without explicit decimals)."""
    q = estimate_realistic_buy_for_solana(
        base_mint="TOKEN",
        quote_mint="UNKNOWN_QUOTE_MINT_NOT_IN_TABLE",
        buy_size_usd=100.0,
        quote_price_usd=200.0,
    )
    assert q is None


def test_estimate_realistic_buy_zero_quote_price_returns_none() -> None:
    q = estimate_realistic_buy_for_solana(
        base_mint="TOKEN",
        quote_mint="So11111111111111111111111111111111111111112",
        buy_size_usd=100.0,
        quote_price_usd=0.0,
    )
    assert q is None
