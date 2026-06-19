"""Position-sizing / R-multiple planner — math + safety classification."""

from __future__ import annotations

import math

import pytest

from memecheck.common.position import (
    DEFAULT_TP_R,
    PositionPlan,
    compute_plan,
    format_plan,
    plan_to_dict,
)


# ----------------------------- basic math --------------------------------


def test_long_side_inferred_from_stop_below_entry() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    assert p.side == "long"


def test_short_side_inferred_from_stop_above_entry() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=105.0,
    )
    assert p.side == "short"


def test_dollar_risk_is_account_times_risk_pct() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=2.0, entry_price=100.0, stop_price=95.0,
    )
    assert math.isclose(p.dollar_risk_usd, 200.0, rel_tol=1e-9)


def test_sl_distance_pct_known_value() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    assert math.isclose(p.sl_distance_pct, 5.0, rel_tol=1e-9)


def test_position_notional_makes_stop_exactly_equal_risk() -> None:
    """If notional × SL% = dollar_risk, stopping out loses exactly R."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    loss_at_stop = p.position_notional_usd * (p.sl_distance_pct / 100.0)
    assert math.isclose(loss_at_stop, p.dollar_risk_usd, rel_tol=1e-9)


def test_short_side_position_sizing_matches_long() -> None:
    """The math is symmetric — short of same SL% gets same notional."""
    long = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    short = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=105.0,
    )
    assert math.isclose(
        long.position_notional_usd, short.position_notional_usd, rel_tol=1e-9
    )


# ----------------------------- leverage ----------------------------------


def test_default_leverage_fits_position_when_small() -> None:
    """A position smaller than account needs leverage = 1 (no leverage)."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    # SL=5%, risk=$100 → notional=$2000, account=$10k. Fits at 1x.
    assert p.leverage_used == 1.0


def test_default_leverage_grows_when_position_exceeds_account() -> None:
    """Small SL % + large account → notional > account; auto-leverage kicks in."""
    p = compute_plan(
        account_usd=1_000, risk_pct=1.0, entry_price=100.0, stop_price=99.5,
    )
    # SL=0.5%, risk=$10 → notional=$2000. With $1000 account, need 2x.
    assert p.leverage_used == pytest.approx(2.0, rel=1e-9)


def test_explicit_leverage_overrides_auto() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        leverage=10.0,
    )
    assert p.leverage_used == 10.0
    # Notional unchanged; margin shrinks.
    assert math.isclose(p.margin_required_usd, p.position_notional_usd / 10.0)


# ----------------------------- liquidation safety ------------------------


def test_low_leverage_safe_classification() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        leverage=2.0,
    )
    # At 2x, liq distance ≈ 50% - 0.5% = 49.5%. SL is 5% → ratio ~0.10. Safe.
    assert p.safety_level == "ok"
    assert p.safety_message is None


def test_high_leverage_triggers_danger() -> None:
    """At 20× with a 5% stop, the stop is BEYOND the ~5% liquidation distance."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        leverage=20.0,
    )
    # liq distance ≈ 1/20 - 0.005 = 0.045 = 4.5%. SL=5% > 4.5%. Danger.
    assert p.safety_level == "danger"
    assert p.safety_message is not None and "DANGER" in p.safety_message


def test_high_leverage_warn_band() -> None:
    """Stop at 70-100% of liquidation distance should warn, not danger."""
    # liq distance at 15× ≈ 1/15 - 0.005 ≈ 6.17%. SL=5% → ratio ≈ 0.81. Warn.
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        leverage=15.0,
    )
    assert p.safety_level == "warn"
    assert p.safety_message is not None and "WARNING" in p.safety_message


# ----------------------------- TP scenarios ------------------------------


def test_tp_at_1r_long() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    # 1R target should be entry + 1 × (entry - stop) = 100 + 5 = 105.
    s1 = next(s for s in p.tp_scenarios if s.r == 1.0)
    assert math.isclose(s1.price, 105.0, rel_tol=1e-9)
    assert math.isclose(s1.gross_pnl_usd, p.dollar_risk_usd, rel_tol=1e-9)


def test_tp_at_2r_short() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=105.0,
    )
    s2 = next(s for s in p.tp_scenarios if s.r == 2.0)
    assert math.isclose(s2.price, 90.0, rel_tol=1e-9)
    assert math.isclose(s2.gross_pnl_usd, 2 * p.dollar_risk_usd, rel_tol=1e-9)


def test_net_pnl_subtracts_fees_and_funding() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        fee_bps=20, funding_pct_8h=0.05, hold_hours=24,
    )
    s1 = next(s for s in p.tp_scenarios if s.r == 1.0)
    expected_net = s1.gross_pnl_usd - p.round_trip_fee_usd - p.estimated_funding_usd
    assert math.isclose(s1.net_pnl_usd, expected_net, rel_tol=1e-9)


def test_long_pays_positive_funding() -> None:
    """POSITIVE funding rate means longs pay shorts → cost to a long."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        funding_pct_8h=+0.05, hold_hours=24,
    )
    assert p.side == "long"
    assert p.estimated_funding_usd > 0          # positive cost
    # Net must be lower than gross because of the cost.
    s = next(x for x in p.tp_scenarios if x.r == 1.0)
    assert s.net_pnl_usd < s.gross_pnl_usd


def test_long_receives_negative_funding() -> None:
    """NEGATIVE funding rate means shorts pay longs → income for a long."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        funding_pct_8h=-0.05, hold_hours=24, fee_bps=0,
    )
    assert p.estimated_funding_usd < 0          # negative cost = income
    s = next(x for x in p.tp_scenarios if x.r == 1.0)
    assert s.net_pnl_usd > s.gross_pnl_usd     # better than gross


def test_short_pays_negative_funding() -> None:
    """NEGATIVE funding rate means shorts pay longs → cost to a short.

    The original bug: net was higher than gross because funding sign wasn't
    flipped for shorts. This test locks in the fix.
    """
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=105.0,
        funding_pct_8h=-0.05, hold_hours=24,
    )
    assert p.side == "short"
    assert p.estimated_funding_usd > 0          # positive cost to the short
    s = next(x for x in p.tp_scenarios if x.r == 1.0)
    assert s.net_pnl_usd < s.gross_pnl_usd      # net must be < gross


def test_short_receives_positive_funding() -> None:
    """POSITIVE funding rate means longs pay shorts → income for a short."""
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=105.0,
        funding_pct_8h=+0.05, hold_hours=24, fee_bps=0,
    )
    assert p.side == "short"
    assert p.estimated_funding_usd < 0          # negative cost = income
    s = next(x for x in p.tp_scenarios if x.r == 1.0)
    assert s.net_pnl_usd > s.gross_pnl_usd


def test_predicted_funding_used_for_first_cycle() -> None:
    """When the venue publishes a next-cycle prediction, cycle 1 uses
    the predicted rate and remaining cycles use the current rate.

    Compare two long-side plans:
      a) current=+0.10, predicted=None → all cycles at +0.10
      b) current=+0.10, predicted=+0.30 → cycle 1 at +0.30, rest at +0.10

    Plan (b) should have higher funding cost than (a).
    """
    a = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        funding_pct_8h=+0.10, funding_pct_8h_next=None, hold_hours=24, fee_bps=0,
    )
    b = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        funding_pct_8h=+0.10, funding_pct_8h_next=+0.30, hold_hours=24, fee_bps=0,
    )
    assert b.estimated_funding_usd > a.estimated_funding_usd


def test_venue_tier_changes_liquidation_distance() -> None:
    """At a large notional, a venue's tiered MMR (higher than 0.5%) should
    pull the liquidation price closer to entry vs the constant default."""
    # SL 0.05% → position notional ~$10k * 1% / 0.0005 = $200k.
    # At Bybit BTC tier, that's still inside the lowest band.
    # At a $50M-equivalent position, MMR escalates.
    small = compute_plan(
        account_usd=100_000, risk_pct=1.0, entry_price=100.0, stop_price=99.5,
        leverage=10, venue="bybit", symbol="BTC",
    )
    huge = compute_plan(
        account_usd=100_000_000, risk_pct=10.0, entry_price=100.0, stop_price=99.9,
        leverage=10, venue="bybit", symbol="BTC",
    )
    # Bigger position must use higher MMR → closer liquidation distance.
    assert huge.maint_margin > small.maint_margin
    assert "bybit" in huge.maint_margin_source


def test_unknown_venue_keeps_existing_behavior() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        venue="not-a-real-venue",
    )
    # Unknown venue falls back to 0.5% — no crash.
    assert p.maint_margin == 0.005


def test_explicit_maint_margin_overrides_venue() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        maint_margin=0.02, venue="kraken-futures", symbol="ETH",
    )
    assert p.maint_margin == 0.02
    assert "explicit" in p.maint_margin_source


def test_custom_tp_multiples() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
        tp_r_multiples=[0.5, 1.5, 4.0],
    )
    assert [s.r for s in p.tp_scenarios] == [0.5, 1.5, 4.0]


def test_default_tp_multiples_are_1_2_3() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    assert [s.r for s in p.tp_scenarios] == list(DEFAULT_TP_R)


# ----------------------------- input validation --------------------------


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        compute_plan(account_usd=0, risk_pct=1, entry_price=100, stop_price=95)
    with pytest.raises(ValueError):
        compute_plan(account_usd=1000, risk_pct=0, entry_price=100, stop_price=95)
    with pytest.raises(ValueError):
        compute_plan(account_usd=1000, risk_pct=200, entry_price=100, stop_price=95)
    with pytest.raises(ValueError):
        compute_plan(account_usd=1000, risk_pct=1, entry_price=100, stop_price=100)
    with pytest.raises(ValueError):
        compute_plan(account_usd=1000, risk_pct=1, entry_price=-1, stop_price=95)


# ----------------------------- formatting + json -------------------------


def test_format_plan_includes_key_sections() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    out = format_plan(p)
    assert "TRADE PLAN" in out
    assert "SIZE" in out
    assert "LIQUIDATION SAFETY" in out
    assert "TAKE-PROFIT SCENARIOS" in out
    assert "1.0R" in out or "1R" in out
    # Reminder block surfaces what the model doesn't capture.
    assert "REMINDERS" in out
    assert "slippage" in out.lower() or "ADL" in out


def test_plan_to_dict_round_trip_shape() -> None:
    p = compute_plan(
        account_usd=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0,
    )
    d = plan_to_dict(p)
    assert d["side"] == "long"
    assert d["inputs"]["account_usd"] == 10_000
    assert d["size"]["leverage_used"] == 1.0
    assert d["liquidation"]["safety_level"] == "ok"
    assert len(d["tp_scenarios"]) == 3
