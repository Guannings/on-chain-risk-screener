"""Deribit + BitMEX funding fetcher tests (no live network)."""

from __future__ import annotations

import pytest

from memecheck.common import funding as funding_mod
from memecheck.common.funding import (
    _SOURCE_NAMES,
    fetch_bitmex_funding,
    fetch_deribit_funding,
    fetch_funding_rate,
)


# ----------------------------- Deribit -----------------------------------


def test_deribit_parses_per_8h_decimal(monkeypatch) -> None:
    def fake_get_json(url, timeout=15):
        assert "BTC-PERPETUAL" in url
        return {"result": {
            "current_funding": 0.0001,    # 0.01% per 8h
            "mark_price": 62_500.0,
        }}
    monkeypatch.setattr(funding_mod, "get_json", fake_get_json)
    r = fetch_deribit_funding("BTC")
    assert r is not None
    assert r.source == "deribit"
    assert r.perp_symbol == "BTC-PERPETUAL"
    assert r.rate_per_8h_pct == pytest.approx(0.01)
    assert r.mark_price == 62_500.0
    assert r.rate_per_8h_pct_next is None    # Deribit doesn't publish a stable prediction


def test_deribit_returns_none_on_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"_error": "503"},
    )
    assert fetch_deribit_funding("BTC") is None


def test_deribit_returns_none_on_missing_funding(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"result": {"mark_price": 100}},
    )
    assert fetch_deribit_funding("BTC") is None


# ----------------------------- BitMEX ------------------------------------


def test_bitmex_uses_xbt_alias_for_btc(monkeypatch) -> None:
    captured = {}
    def fake_get_json(url, timeout=15):
        captured["url"] = url
        return [{
            "fundingRate": 0.000047,
            "indicativeFundingRate": -0.00007,
            "markPrice": 62715.76,
        }]
    monkeypatch.setattr(funding_mod, "get_json", fake_get_json)
    r = fetch_bitmex_funding("BTC")
    assert r is not None
    assert "XBTUSD" in captured["url"]
    assert r.perp_symbol == "XBTUSD"
    assert r.rate_per_8h_pct == pytest.approx(0.0047)
    assert r.rate_per_8h_pct_next == pytest.approx(-0.007)


def test_bitmex_returns_none_on_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(funding_mod, "get_json", lambda url, timeout=15: [])
    assert fetch_bitmex_funding("BTC") is None


# ----------------------------- source order ------------------------------


def test_source_names_include_new_venues() -> None:
    assert "fetch_deribit_funding" in _SOURCE_NAMES
    assert "fetch_bitmex_funding" in _SOURCE_NAMES
    # Kraken still first for majors.
    assert _SOURCE_NAMES[0] == "fetch_kraken_funding"


def test_fetch_funding_falls_through_to_deribit(monkeypatch) -> None:
    """Kraken + Hyperliquid say None; Deribit answers → result is from Deribit."""
    from memecheck.common.funding import FundingRateResult
    monkeypatch.setattr(funding_mod, "fetch_kraken_funding", lambda s: None)
    monkeypatch.setattr(funding_mod, "fetch_hyperliquid_funding", lambda s: None)
    monkeypatch.setattr(
        funding_mod, "fetch_deribit_funding",
        lambda s: FundingRateResult(
            symbol=s.upper(), rate_per_8h_pct=0.005, raw_rate=0.00005,
            raw_unit="decimal per 8h (relative)", source="deribit",
            mark_price=62500.0, perp_symbol="BTC-PERPETUAL",
        ),
    )
    r = fetch_funding_rate("BTC")
    assert r is not None
    assert r.source == "deribit"


def test_fetch_funding_falls_through_to_bitmex(monkeypatch) -> None:
    """Three sources empty; BitMEX answers last."""
    from memecheck.common.funding import FundingRateResult
    monkeypatch.setattr(funding_mod, "fetch_kraken_funding", lambda s: None)
    monkeypatch.setattr(funding_mod, "fetch_hyperliquid_funding", lambda s: None)
    monkeypatch.setattr(funding_mod, "fetch_deribit_funding", lambda s: None)
    monkeypatch.setattr(
        funding_mod, "fetch_bitmex_funding",
        lambda s: FundingRateResult(
            symbol=s.upper(), rate_per_8h_pct=0.0047, raw_rate=0.000047,
            raw_unit="decimal per 8h (relative)", source="bitmex",
            mark_price=62715.0, perp_symbol="XBTUSD",
        ),
    )
    r = fetch_funding_rate("BTC")
    assert r is not None
    assert r.source == "bitmex"
