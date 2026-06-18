"""Position-sizing / R-multiple trade planner.

Pure math. Given account size, risk tolerance, entry, and stop, computes the
single position that risks exactly the user's chosen fraction of account if
stopped out — plus TP prices for R-multiple targets, expected fees and
funding, and a liquidation-distance safety check.

All formulas:

    Dollar risk:          R = A · risk_pct / 100
    SL distance:          d_sl = |P_entry - P_stop| / P_entry
    Position notional:    N = R / d_sl
    Quantity:             q = N / P_entry
    Liquidation (long):   P_liq = P_entry · (1 - 1/L + mm)
    Liquidation (short):  P_liq = P_entry · (1 + 1/L - mm)
    TP at kR (long):      P_tp = P_entry + k · (P_entry - P_stop)
    TP at kR (short):     P_tp = P_entry - k · (P_stop - P_entry)
    Round-trip fee:       N · fee_bps / 10_000
    Funding over t hours: N · funding_pct_8h / 100 · (t / 8)

The math intentionally ignores slippage on the fill, stop hunts, ADL events,
and tiered maintenance margin. The whole point is to make the position-
sizing decision deterministic before the trade — the messy real-world
deltas are listed in the printed reminder so they're not forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ----------------------------- defaults ----------------------------------

DEFAULT_RISK_PCT: float = 1.0
DEFAULT_TP_R: tuple[float, ...] = (1.0, 2.0, 3.0)
DEFAULT_MAINT_MARGIN: float = 0.005         # 0.5%
DEFAULT_FEE_BPS: int = 10                   # 0.10% round-trip (typical taker × 2 on majors)
DEFAULT_FUNDING_PCT_8H: float = 0.01        # 0.01% per 8h cycle (neutral baseline)
DEFAULT_HOLD_HOURS: float = 24.0

# Safety thresholds for the SL-vs-liquidation distance ratio.
_LIQ_RATIO_DANGER: float = 1.0              # SL >= liquidation: not a stop, just a liquidation
_LIQ_RATIO_WARN: float = 0.70               # SL is within 30% of liquidation distance


# ----------------------------- result ------------------------------------


@dataclass(frozen=True)
class TPScenario:
    r: float
    price: float
    gross_pnl_usd: float
    net_pnl_usd: float


@dataclass(frozen=True)
class PositionPlan:
    # Echo of inputs (so JSON output is self-describing)
    account_usd: float
    risk_pct: float
    entry_price: float
    stop_price: float
    leverage_requested: Optional[float]
    fee_bps: int
    funding_pct_8h: float
    hold_hours: float
    maint_margin: float

    # Derived classification
    side: str                                # "long" or "short"

    # Risk + size
    dollar_risk_usd: float
    sl_distance_pct: float
    position_notional_usd: float
    quantity: float
    leverage_used: float
    margin_required_usd: float

    # Liquidation safety
    liquidation_price: float
    liquidation_distance_pct: float
    sl_to_liq_ratio: float
    safety_level: str                        # "ok" | "warn" | "danger"
    safety_message: Optional[str]

    # Costs
    round_trip_fee_usd: float
    estimated_funding_usd: float

    # R-multiple TP scenarios
    tp_scenarios: list[TPScenario] = field(default_factory=list)


# ----------------------------- core --------------------------------------


def compute_plan(
    *,
    account_usd: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: Optional[float] = None,
    tp_r_multiples: Optional[list[float]] = None,
    maint_margin: float = DEFAULT_MAINT_MARGIN,
    fee_bps: int = DEFAULT_FEE_BPS,
    funding_pct_8h: float = DEFAULT_FUNDING_PCT_8H,
    hold_hours: float = DEFAULT_HOLD_HOURS,
) -> PositionPlan:
    if account_usd <= 0:
        raise ValueError("account_usd must be positive")
    if risk_pct <= 0 or risk_pct > 100:
        raise ValueError("risk_pct must be in (0, 100]")
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("prices must be positive")
    if entry_price == stop_price:
        raise ValueError("entry_price and stop_price cannot be equal")
    if maint_margin < 0 or maint_margin >= 1:
        raise ValueError("maint_margin must be in [0, 1)")
    if hold_hours < 0:
        raise ValueError("hold_hours must be non-negative")

    side = "long" if stop_price < entry_price else "short"
    tp_multiples = list(tp_r_multiples) if tp_r_multiples else list(DEFAULT_TP_R)

    dollar_risk = account_usd * (risk_pct / 100.0)
    sl_distance_pct = abs(entry_price - stop_price) / entry_price * 100.0

    # Position sized so that hitting the stop loses exactly dollar_risk.
    position_notional = dollar_risk / (sl_distance_pct / 100.0)
    quantity = position_notional / entry_price

    # Leverage: caller's value if given; else minimum needed to fit the trade.
    if leverage is None or leverage <= 0:
        leverage_used = max(1.0, position_notional / account_usd)
    else:
        leverage_used = float(leverage)
    margin_required = position_notional / leverage_used

    # Liquidation (isolated-margin approximation, constant maint margin).
    if side == "long":
        liquidation_price = entry_price * (1 - 1 / leverage_used + maint_margin)
    else:
        liquidation_price = entry_price * (1 + 1 / leverage_used - maint_margin)
    liquidation_price = max(liquidation_price, 0.0)
    liquidation_distance_pct = abs(entry_price - liquidation_price) / entry_price * 100.0

    if liquidation_distance_pct > 0:
        sl_to_liq_ratio = sl_distance_pct / liquidation_distance_pct
    else:
        sl_to_liq_ratio = float("inf")

    safety_level, safety_message = _classify_safety(
        sl_distance_pct, liquidation_distance_pct, sl_to_liq_ratio
    )

    # Costs.
    round_trip_fee = position_notional * (fee_bps / 10_000.0)
    funding_cycles = hold_hours / 8.0
    # Funding sign convention:
    #   POSITIVE funding rate = longs pay shorts
    #   NEGATIVE funding rate = shorts pay longs
    # `estimated_funding` here represents the COST TO THE TRADER (positive =
    # outflow, negative = inflow), so the sign must flip based on trade side:
    #   long:  cost = +rate * notional * cycles    (positive rate hurts long)
    #   short: cost = -rate * notional * cycles    (negative rate hurts short)
    funding_signed = position_notional * (funding_pct_8h / 100.0) * funding_cycles
    estimated_funding = funding_signed if side == "long" else -funding_signed

    # R-multiple TP scenarios.
    scenarios: list[TPScenario] = []
    for k in tp_multiples:
        if side == "long":
            tp_price = entry_price + k * (entry_price - stop_price)
        else:
            tp_price = entry_price - k * (stop_price - entry_price)
        gross = k * dollar_risk
        net = gross - round_trip_fee - estimated_funding
        scenarios.append(
            TPScenario(r=k, price=tp_price, gross_pnl_usd=gross, net_pnl_usd=net)
        )

    return PositionPlan(
        account_usd=account_usd,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        leverage_requested=leverage,
        fee_bps=fee_bps,
        funding_pct_8h=funding_pct_8h,
        hold_hours=hold_hours,
        maint_margin=maint_margin,
        side=side,
        dollar_risk_usd=dollar_risk,
        sl_distance_pct=sl_distance_pct,
        position_notional_usd=position_notional,
        quantity=quantity,
        leverage_used=leverage_used,
        margin_required_usd=margin_required,
        liquidation_price=liquidation_price,
        liquidation_distance_pct=liquidation_distance_pct,
        sl_to_liq_ratio=sl_to_liq_ratio,
        safety_level=safety_level,
        safety_message=safety_message,
        round_trip_fee_usd=round_trip_fee,
        estimated_funding_usd=estimated_funding,
        tp_scenarios=scenarios,
    )


def _classify_safety(
    sl_pct: float, liq_pct: float, ratio: float
) -> tuple[str, Optional[str]]:
    if ratio >= _LIQ_RATIO_DANGER:
        msg = (
            f"DANGER: stop ({sl_pct:.2f}%) is at or beyond liquidation "
            f"({liq_pct:.2f}%). The stop will not protect you — the position "
            "will liquidate first. Reduce leverage or move stop closer."
        )
        return "danger", msg
    if ratio >= _LIQ_RATIO_WARN:
        msg = (
            f"WARNING: stop ({sl_pct:.2f}%) is {ratio*100:.0f}% of the way to "
            f"liquidation ({liq_pct:.2f}%). A fast wick could fill the stop "
            "below your liquidation price. Consider lower leverage or wider stop."
        )
        return "warn", msg
    return "ok", None


# ----------------------------- formatting --------------------------------


def _fmt_usd(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"${x:,.2f}"
    return f"${x:,.2f}"


def _fmt_price(x: float) -> str:
    if x >= 1000:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:.4f}"
    if x >= 0.01:
        return f"${x:.6f}"
    return f"${x:.4g}"


def format_plan(p: PositionPlan) -> str:
    lines: list[str] = []
    lines.append("=== TRADE PLAN ===")
    side_label = "LONG" if p.side == "long" else "SHORT"
    lines.append(f"  Side:       {side_label}")
    lines.append(f"  Entry:      {_fmt_price(p.entry_price)}")
    sl_sign = "-" if p.side == "long" else "+"
    lines.append(
        f"  Stop:       {_fmt_price(p.stop_price)}   "
        f"({sl_sign}{p.sl_distance_pct:.2f}% from entry)"
    )
    lines.append(f"  Account:    {_fmt_usd(p.account_usd)}")
    lines.append(
        f"  Risk:       {p.risk_pct:.2f}%   ({_fmt_usd(p.dollar_risk_usd)})"
    )
    lines.append("")
    lines.append("=== SIZE ===")
    lines.append(f"  Position notional:  {_fmt_usd(p.position_notional_usd)}")
    lines.append(f"  Quantity:           {p.quantity:,.6g} tokens")
    leverage_note = ""
    if p.leverage_requested is None or p.leverage_requested <= 0:
        leverage_note = "  (minimum to fit position)"
    lines.append(f"  Leverage:           {p.leverage_used:.2f}x{leverage_note}")
    lines.append(f"  Margin required:    {_fmt_usd(p.margin_required_usd)}")
    lines.append("")
    lines.append("=== LIQUIDATION SAFETY ===")
    liq_sign = "-" if p.side == "long" else "+"
    lines.append(
        f"  Liquidation price:  {_fmt_price(p.liquidation_price)}   "
        f"({liq_sign}{p.liquidation_distance_pct:.2f}% from entry)"
    )
    lines.append(
        f"  Stop / Liq ratio:   {p.sl_to_liq_ratio:.2f}   "
        f"(want ≤ 0.70 for safety)"
    )
    if p.safety_message:
        tag = "⛔" if p.safety_level == "danger" else "⚠"
        lines.append(f"  {tag} {p.safety_message}")
    else:
        lines.append("  ✓ stop is comfortably inside liquidation distance.")
    lines.append("")
    lines.append("=== COSTS (estimated) ===")
    lines.append(
        f"  Round-trip fee:     {_fmt_usd(p.round_trip_fee_usd)}   "
        f"({p.fee_bps} bps)"
    )
    lines.append(
        f"  Funding ({p.hold_hours:.0f}h hold):  {_fmt_usd(p.estimated_funding_usd)}   "
        f"({p.funding_pct_8h:+.3f}% / 8h)"
    )
    lines.append("")
    lines.append("=== R-MULTIPLE TAKE-PROFIT SCENARIOS ===")
    for s in p.tp_scenarios:
        lines.append(
            f"  {s.r:>4.1f}R  exit {_fmt_price(s.price)}   "
            f"gross {_fmt_usd(s.gross_pnl_usd)}   "
            f"net   {_fmt_usd(s.net_pnl_usd)}"
        )
    lines.append("")
    lines.append("=== REMINDERS ===")
    lines.append(
        "  - Net P&L is gross minus modeled fees and funding only."
    )
    lines.append(
        "  - Not modeled: fill slippage, stop-hunt wicks, ADL events,"
    )
    lines.append(
        "    tiered maintenance margin on large positions."
    )
    lines.append(
        "  - Always set TP and SL as a SINGLE OCO bracket with reduce-only"
    )
    lines.append(
        "    flags and MARK-price triggers. Never enter without a stop attached."
    )
    return "\n".join(lines)


def plan_to_dict(p: PositionPlan) -> dict[str, Any]:
    return {
        "inputs": {
            "account_usd": p.account_usd,
            "risk_pct": p.risk_pct,
            "entry_price": p.entry_price,
            "stop_price": p.stop_price,
            "leverage_requested": p.leverage_requested,
            "fee_bps": p.fee_bps,
            "funding_pct_8h": p.funding_pct_8h,
            "hold_hours": p.hold_hours,
            "maint_margin": p.maint_margin,
        },
        "side": p.side,
        "risk": {
            "dollar_risk_usd": p.dollar_risk_usd,
            "sl_distance_pct": p.sl_distance_pct,
        },
        "size": {
            "position_notional_usd": p.position_notional_usd,
            "quantity": p.quantity,
            "leverage_used": p.leverage_used,
            "margin_required_usd": p.margin_required_usd,
        },
        "liquidation": {
            "price": p.liquidation_price,
            "distance_pct": p.liquidation_distance_pct,
            "sl_to_liq_ratio": p.sl_to_liq_ratio,
            "safety_level": p.safety_level,
            "safety_message": p.safety_message,
        },
        "costs": {
            "round_trip_fee_usd": p.round_trip_fee_usd,
            "estimated_funding_usd": p.estimated_funding_usd,
        },
        "tp_scenarios": [
            {
                "r": s.r,
                "price": s.price,
                "gross_pnl_usd": s.gross_pnl_usd,
                "net_pnl_usd": s.net_pnl_usd,
            }
            for s in p.tp_scenarios
        ],
    }
