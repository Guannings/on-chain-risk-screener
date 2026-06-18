"""CEX perp health screen — analyser thresholds + verdict logic, mocked HTTP."""

from __future__ import annotations

import math

import pytest

from memecheck.common import cex_health as cex_mod
from memecheck.common.cex_health import (
    BASIS_BLOWOUT_PCT,
    CEX_HARD_PASS_FLAG_COUNT,
    ELEVATED_FUNDING_PER_8H_PCT,
    EXTREME_24H_MOVE_PCT,
    EXTREME_FUNDING_PER_8H_PCT,
    THIN_VOLUME_USD,
    WIDE_SPREAD_BPS,
    analyze_cex_perp,
    exit_code_for_cex,
    fetch_cex_ticker,
    make_cex_verdict,
)


# ----------------------------- fixtures ----------------------------------


def _healthy_ticker(symbol: str = "PF_XRPUSD", mark: float = 1.18) -> dict:
    """A clean-looking perp — no flags expected."""
    return {
        "symbol": symbol,
        "markPrice": mark,
        "indexPrice": mark,
        "last": mark,
        "bid": mark - 0.0005,
        "ask": mark + 0.0005,
        "vol24h": 8_000_000,
        "openInterest": 12_000_000,
        "change24h": 0.5,
        "high24h": mark * 1.02,
        "low24h": mark * 0.98,
        # Mild funding: -2e-5 absolute / mark 1.18 → -0.0136%/8h (elevated but not extreme)
        # Lower it so the test produces a TRULY clean ticker.
        "fundingRate": -2e-6,                  # → -0.00136% / 8h
        "fundingRatePrediction": -1.9e-6,
    }


# ----------------------------- fetcher -----------------------------------


def test_fetch_cex_ticker_finds_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        cex_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_healthy_ticker("PF_XRPUSD")]},
    )
    t = fetch_cex_ticker("XRP")
    assert t is not None
    assert t["symbol"] == "PF_XRPUSD"


def test_fetch_cex_ticker_btc_uses_xbt_alias(monkeypatch) -> None:
    monkeypatch.setattr(
        cex_mod, "get_json",
        lambda url, timeout=15: {"tickers": [_healthy_ticker("PF_XBTUSD", mark=64000)]},
    )
    t = fetch_cex_ticker("BTC")
    assert t is not None
    assert t["symbol"] == "PF_XBTUSD"


def test_fetch_cex_ticker_missing_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(
        cex_mod, "get_json",
        lambda url, timeout=15: {"tickers": []},
    )
    assert fetch_cex_ticker("XRP") is None


def test_fetch_cex_ticker_http_error_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(
        cex_mod, "get_json",
        lambda url, timeout=15: {"_error": "HTTP 503"},
    )
    assert fetch_cex_ticker("XRP") is None


# ----------------------------- analyser thresholds -----------------------


def test_healthy_ticker_no_flags() -> None:
    flags, notes, metrics = analyze_cex_perp(_healthy_ticker())
    assert flags == []
    assert metrics["funding_per_8h_pct"] is not None
    assert abs(metrics["funding_per_8h_pct"]) < ELEVATED_FUNDING_PER_8H_PCT


def test_extreme_funding_flag() -> None:
    t = _healthy_ticker()
    # Force funding to -0.10% / 8h (well above the 0.05% threshold)
    # 0.10% = (raw / mark) * 8 * 100  →  raw = 0.10/100/8 * mark = mark/8000
    t["fundingRate"] = -t["markPrice"] / 8000  # -0.0125% per 8h... let me redo
    # Want |funding_8h| >= 0.05.  (raw/mark)*800 = 0.05  → raw = 0.05*mark/800 = mark*6.25e-5
    t["fundingRate"] = -t["markPrice"] * 6.25e-5 * 2  # *2 for safety > threshold
    flags, _notes, metrics = analyze_cex_perp(t, side="short")
    assert metrics["funding_per_8h_pct"] is not None
    assert abs(metrics["funding_per_8h_pct"]) >= EXTREME_FUNDING_PER_8H_PCT
    assert any("extreme" in f.lower() for f in flags)


def test_funding_headwind_for_short_when_negative() -> None:
    t = _healthy_ticker()
    # Funding noticeably negative (shorts pay longs) at elevated level.
    t["fundingRate"] = -t["markPrice"] * 4e-5  # → -0.032%/8h (above 0.02 threshold)
    flags, notes, _m = analyze_cex_perp(t, side="short")
    assert any("short pays" in f.lower() or "headwind" in n.lower() for f, n in
               zip(flags, notes)) or any("PAY funding" in n for n in notes)


def test_funding_tailwind_for_long_when_negative() -> None:
    t = _healthy_ticker()
    t["fundingRate"] = -t["markPrice"] * 4e-5
    flags, notes, _m = analyze_cex_perp(t, side="long")
    # Longs receive funding when it's negative — that's a tailwind, NOT a flag.
    assert all("long pays" not in f.lower() for f in flags)
    assert any("RECEIVE" in n or "tailwind" in n.lower() for n in notes)


def test_basis_blowout_flag() -> None:
    t = _healthy_ticker()
    t["markPrice"] = 1.20
    t["indexPrice"] = 1.18  # mark > index by ~1.7%, above 0.5% threshold
    flags, _notes, metrics = analyze_cex_perp(t)
    assert metrics["basis_pct"] is not None
    assert metrics["basis_pct"] > BASIS_BLOWOUT_PCT
    assert any("premium" in f.lower() or "basis" in f.lower() for f in flags)


def test_thin_volume_flag() -> None:
    t = _healthy_ticker()
    t["vol24h"] = 100  # 100 contracts × $1.18 = $118, well below threshold
    flags, _notes, metrics = analyze_cex_perp(t)
    assert metrics["vol_24h_usd_approx"] < THIN_VOLUME_USD
    assert any("thin" in f.lower() and "volume" in f.lower() for f in flags)


def test_wide_spread_flag() -> None:
    t = _healthy_ticker()
    mark = t["markPrice"]
    # Force spread = 50 bps
    t["bid"] = mark - mark * 0.0025
    t["ask"] = mark + mark * 0.0025
    flags, _notes, metrics = analyze_cex_perp(t)
    assert metrics["spread_bps"] > WIDE_SPREAD_BPS
    assert any("spread" in f.lower() and "wide" in f.lower() for f in flags)


def test_extreme_24h_move_flag() -> None:
    t = _healthy_ticker()
    t["change24h"] = 15.0  # 15% rally
    flags, _notes, _m = analyze_cex_perp(t)
    assert any("rallied" in f.lower() for f in flags)


# ----------------------------- verdict -----------------------------------


def test_verdict_no_flags_is_clean() -> None:
    v = make_cex_verdict([])
    assert "no automatic red flags" in v.lower()
    assert exit_code_for_cex(v) == 0


def test_verdict_few_flags_is_risky() -> None:
    v = make_cex_verdict(["one flag", "two flag"])
    assert v.startswith("RISKY")
    assert exit_code_for_cex(v) == 1


def test_verdict_many_flags_is_hard_pass() -> None:
    flags = ["a", "b", "c"]
    v = make_cex_verdict(flags)
    assert v == "HARD PASS"
    assert exit_code_for_cex(v) == 1


# ----------------------------- metrics shape -----------------------------


def test_metrics_keys_are_stable() -> None:
    """Downstream consumers (cex-prep, JSON output) depend on these keys."""
    _f, _n, m = analyze_cex_perp(_healthy_ticker(), side="short")
    for k in (
        "symbol", "side", "mark", "index", "last", "spread_bps",
        "vol_24h_contracts", "vol_24h_usd_approx",
        "open_interest_contracts", "open_interest_usd_approx",
        "change_24h_pct", "high_24h", "low_24h",
        "funding_per_8h_pct", "funding_per_8h_pct_predicted",
        "funding_apy_pct", "basis_pct",
    ):
        assert k in m, f"missing metric key: {k}"
