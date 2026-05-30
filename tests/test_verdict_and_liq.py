"""Verdict logic, exit codes, and the liquidation-price calculator."""

from __future__ import annotations

import math

import memecheck


# ----------------------------- verdict -----------------------------------

def test_no_flags_means_no_red_flag_verdict() -> None:
    v = memecheck.make_verdict([], None)
    assert "no automatic red flags" in v.lower()
    assert memecheck.exit_code_for(v, []) == 0


def test_few_flags_is_risky() -> None:
    flags = ["thin liq", "sell pressure"]
    v = memecheck.make_verdict(flags, None)
    assert v.startswith("RISKY")
    assert memecheck.exit_code_for(v, flags) == 1


def test_many_flags_is_hard_pass() -> None:
    flags = ["a", "b", "c", "d"]
    v = memecheck.make_verdict(flags, None)
    assert v == "HARD PASS"
    assert memecheck.exit_code_for(v, flags) == 1


def test_honeypot_overrides_flag_count() -> None:
    v = memecheck.make_verdict([], {"is_honeypot": True})
    assert v.startswith("HONEYPOT")
    assert memecheck.exit_code_for(v, []) == 2


# ----------------------------- liquidation calc --------------------------

def test_long_liq_below_entry() -> None:
    # 10x long, mm = 0.5% -> liq ~ entry * (1 - 0.1 + 0.005) = entry * 0.905
    lp = memecheck.liquidation_price(100.0, 10, side="long")
    assert math.isclose(lp, 90.5, rel_tol=1e-9)


def test_short_liq_above_entry() -> None:
    # 10x short -> liq ~ entry * (1 + 0.1 - 0.005) = entry * 1.095
    lp = memecheck.liquidation_price(100.0, 10, side="short")
    assert math.isclose(lp, 109.5, rel_tol=1e-9)


def test_higher_leverage_means_tighter_liq() -> None:
    far = memecheck.liquidation_price(100.0, 2, "long")
    close = memecheck.liquidation_price(100.0, 20, "long")
    # Tighter liq means closer to entry from below
    assert close > far


def test_liq_report_dict_shape() -> None:
    d = memecheck.liq_report_dict(1.23, 5)
    assert d["entry"] == 1.23
    assert d["leverage"] == 5
    assert set(d["sides"].keys()) == {"long", "short"}
    assert d["sides"]["long"]["adverse_move_pct"] > 0
