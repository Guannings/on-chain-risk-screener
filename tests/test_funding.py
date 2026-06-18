"""Funding-rate fetcher — symbol mapping + Kraken normalisation, mocked HTTP."""

from __future__ import annotations

import math

import pytest

from memecheck.common import funding as funding_mod
from memecheck.common.funding import (
    KRAKEN_RATE_PERIOD_HOURS,
    FundingRateResult,
    _kraken_perp_symbol,
    fetch_funding_rate,
    fetch_kraken_funding,
)


# ----------------------------- symbol mapping ----------------------------


def test_btc_maps_to_xbt() -> None:
    assert _kraken_perp_symbol("BTC") == "PF_XBTUSD"
    assert _kraken_perp_symbol("btc") == "PF_XBTUSD"
    assert _kraken_perp_symbol("$BTC") == "PF_XBTUSD"


def test_other_symbols_passthrough() -> None:
    assert _kraken_perp_symbol("XRP") == "PF_XRPUSD"
    assert _kraken_perp_symbol("ETH") == "PF_ETHUSD"
    assert _kraken_perp_symbol("sol") == "PF_SOLUSD"


# ----------------------------- Kraken fetcher ----------------------------


def _mk_ticker(symbol: str, funding_rate: float, mark: float = 1.0):
    return {
        "symbol": symbol,
        "fundingRate": funding_rate,
        "markPrice": mark,
    }


def test_kraken_xrp_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_mk_ticker("PF_XRPUSD", -2.07e-5, mark=1.18)]},
    )
    r = fetch_kraken_funding("XRP")
    assert r is not None
    assert r.symbol == "XRP"
    assert r.source == "kraken-futures"
    assert r.perp_symbol == "PF_XRPUSD"
    assert r.mark_price == 1.18
    assert r.raw_unit == "USD/contract/hour (absolute)"
    expected_pct = (-2.07e-5 / 1.18) * 8 * 100
    assert math.isclose(r.rate_per_8h_pct, expected_pct, rel_tol=1e-9)


def test_kraken_btc_does_not_explode(monkeypatch) -> None:
    """Regression: a previous bug treated raw as per-cycle percent and BTC came back
    at +58%. The correct normalisation yields a small percent like ~0.0036%."""
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_mk_ticker("PF_XBTUSD", 0.294, mark=64465.54)]},
    )
    r = fetch_kraken_funding("BTC")
    assert r is not None
    assert r.symbol == "BTC"
    assert r.perp_symbol == "PF_XBTUSD"
    # Should be ~ 0.0036% per 8h, definitely under 0.1%.
    assert abs(r.rate_per_8h_pct) < 0.1
    expected = (0.294 / 64465.54) * 8 * 100
    assert math.isclose(r.rate_per_8h_pct, expected, rel_tol=1e-9)


def test_kraken_returns_none_for_unknown_symbol(monkeypatch) -> None:
    """A symbol that doesn't appear in /tickers returns None, doesn't crash."""
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_mk_ticker("PF_XRPUSD", -2.07e-5)]},
    )
    assert fetch_kraken_funding("MADEUP") is None


def test_kraken_returns_none_on_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"_error": "HTTP 503"},
    )
    assert fetch_kraken_funding("XRP") is None


def test_kraken_handles_missing_funding_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {
            "tickers": [{"symbol": "PF_XRPUSD", "markPrice": 1.18}]  # no fundingRate
        },
    )
    assert fetch_kraken_funding("XRP") is None


def test_kraken_handles_non_numeric_funding_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {
            "tickers": [{"symbol": "PF_XRPUSD", "fundingRate": "n/a", "markPrice": "n/a"}]
        },
    )
    assert fetch_kraken_funding("XRP") is None


# ----------------------------- top-level dispatcher ----------------------


def test_fetch_funding_rate_returns_first_source_hit(monkeypatch) -> None:
    """fetch_funding_rate cycles through _SOURCES; Kraken is currently the only one."""
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_mk_ticker("PF_XRPUSD", -2.07e-5)]},
    )
    r = fetch_funding_rate("XRP")
    assert r is not None
    assert isinstance(r, FundingRateResult)


def test_fetch_funding_rate_empty_symbol() -> None:
    assert fetch_funding_rate("") is None
    assert fetch_funding_rate("   ") is None


def test_fetch_funding_rate_returns_none_if_no_source_has_it(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_mod, "get_json",
        lambda url, timeout=15: {"tickers": []},
    )
    assert fetch_funding_rate("XRP") is None
