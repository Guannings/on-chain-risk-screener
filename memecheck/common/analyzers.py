"""Analyzers — pure functions over raw source payloads.

Each analyzer takes whatever the source client returned and emits
(flags, notes, metrics). metrics is a stable-key dict suitable for JSON
output and downstream consumers (the monitor will read these too).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from memecheck.common.format import fmt_usd, pct
from memecheck.common.sources import _liq_of
from memecheck.common.verdict import (
    DEAD_VOL_LIQ_RATIO,
    DEAD_VOL_LIQ_TRUST_CEILING,
    INSIDER_CONCENTRATION_PCT,
    LOW_LIQ_MC_RATIO,
    LP_LOCKED_FLOOR_PCT,
    NEW_TOKEN_HOURS,
    SELL_PRESSURE_RATIO,
    SELL_TAX_CEILING_PCT,
    THIN_LIQ_USD,
    TOP10_CONCENTRATION_PCT,
    WASH_VOL_LIQ_RATIO,
)


def analyze_dexscreener(
    primary: dict[str, Any], pairs: list[dict[str, Any]]
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return (flags, notes, metrics). metrics is a structured dict for --json."""
    flags: list[str] = []
    notes: list[str] = []

    # ----- aggregate across every pool on this chain -----
    liq = sum(_liq_of(p) for p in pairs) or None
    vol24 = sum((p.get("volume") or {}).get("h24", 0) or 0 for p in pairs) or None
    buys = sum(((p.get("txns") or {}).get("h24") or {}).get("buys", 0) or 0 for p in pairs)
    sells = sum(((p.get("txns") or {}).get("h24") or {}).get("sells", 0) or 0 for p in pairs)
    # market cap is token-level: take it from the deepest pool that reports one
    mcap = next(
        (p.get("marketCap") or p.get("fdv") for p in pairs if p.get("marketCap") or p.get("fdv")),
        None,
    )
    # true age = earliest pool creation across all pools
    created = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
    created_ms = min(created) if created else None

    age_hours: Optional[float] = None
    if created_ms:
        age_hours = (
            datetime.now(timezone.utc) - datetime.fromtimestamp(created_ms / 1000, timezone.utc)
        ).total_seconds() / 3600

    metrics: dict[str, Any] = {
        "chain": primary.get("chainId"),
        "dex": primary.get("dexId"),
        "base_symbol": (primary.get("baseToken") or {}).get("symbol"),
        "quote_symbol": (primary.get("quoteToken") or {}).get("symbol"),
        "pool_count": len(pairs),
        "liquidity_usd": liq,
        "market_cap_usd": mcap,
        "volume_24h_usd": vol24,
        "buys_24h": buys,
        "sells_24h": sells,
        "age_hours": age_hours,
        "liq_mc_ratio": (liq / mcap) if (liq and mcap and mcap > 0) else None,
        "vol_liq_ratio": (vol24 / liq) if (liq and vol24 and liq > 0) else None,
    }

    notes.append(
        f"Primary pool: {metrics['base_symbol'] or '?'}/{metrics['quote_symbol'] or '?'} on "
        f"{metrics['chain'] or '?'} via {metrics['dex'] or '?'}  "
        f"(aggregated over {len(pairs)} pool{'s' if len(pairs)!=1 else ''})"
    )
    notes.append(f"Liquidity: {fmt_usd(liq)}   MC/FDV: {fmt_usd(mcap)}   24h vol: {fmt_usd(vol24)}")

    # age
    if age_hours is not None:
        notes.append(f"Age: {age_hours/24:.1f} days ({age_hours:.0f}h, earliest pool)")
        if age_hours < NEW_TOKEN_HOURS:
            flags.append("Less than 24h old — peak rug window, near-zero track record.")
    else:
        notes.append("Age: unknown")

    # liquidity floor
    if liq is not None and liq < THIN_LIQ_USD:
        flags.append(f"Thin liquidity ({fmt_usd(liq)}) — you ARE the slippage on the way out.")

    # liquidity vs market cap
    if metrics["liq_mc_ratio"] is not None:
        ratio = metrics["liq_mc_ratio"]
        notes.append(f"Liq / MC ratio: {ratio:.3f}")
        if ratio < LOW_LIQ_MC_RATIO:
            flags.append(
                f"Liq/MC ratio {ratio:.3f} is very low — tiny float holding up a big 'valuation'."
            )

    # wash-trade / activity proxy
    if metrics["vol_liq_ratio"] is not None:
        vr = metrics["vol_liq_ratio"]
        notes.append(f"24h vol / liq: {vr:.2f}x")
        if vr > WASH_VOL_LIQ_RATIO:
            flags.append(f"Volume is {vr:.0f}x liquidity — possible wash trading / bot churn.")
        elif vr < DEAD_VOL_LIQ_RATIO and (liq or 0) < DEAD_VOL_LIQ_TRUST_CEILING:
            flags.append("Total volume is negligible vs liquidity — interest looks dead.")
        elif vr < DEAD_VOL_LIQ_RATIO:
            notes.append(
                "Low reported vol/liq, but DexScreener's token endpoint can under-count "
                "volume on large multi-pool tokens — eyeball the chart before trusting this."
            )

    # buy/sell pressure
    if buys or sells:
        notes.append(f"24h txns: {buys} buys / {sells} sells")
        if sells and buys and sells > buys * SELL_PRESSURE_RATIO:
            flags.append("Sells heavily outpacing buys — distribution underway.")

    return flags, notes, metrics


def analyze_rugcheck(rep: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    flags: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {
        "score": None,
        "mint_authority": None,
        "freeze_authority": None,
        "lp_locked_pct": None,
        "top10_pct": None,
        "insider_pct": None,
        "available": True,
    }
    if not rep or "_error" in rep:
        notes.append("RugCheck: unavailable.")
        metrics["available"] = False
        return flags, notes, metrics

    score = rep.get("score_normalised", rep.get("score"))
    metrics["score"] = score
    if score is not None:
        notes.append(f"RugCheck score: {score} (lower = safer on the normalised scale)")

    tok = rep.get("token") or {}
    mint_auth = tok.get("mintAuthority")
    freeze_auth = tok.get("freezeAuthority")
    metrics["mint_authority"] = mint_auth
    metrics["freeze_authority"] = freeze_auth
    if mint_auth:
        flags.append("Mint authority NOT revoked — dev can print more supply at will.")
    else:
        notes.append("Mint authority: revoked")
    if freeze_auth:
        flags.append("Freeze authority active — your wallet/sells can be frozen.")
    else:
        notes.append("Freeze authority: revoked")

    # LP locked / burned
    markets = rep.get("markets") or []
    lp_locked: Optional[float] = None
    for m in markets:
        lp = m.get("lp") or {}
        if lp.get("lpLockedPct") is not None:
            lp_locked = lp.get("lpLockedPct")
            break
    metrics["lp_locked_pct"] = lp_locked
    if lp_locked is not None:
        notes.append(f"LP locked/burned: {pct(lp_locked)}")
        if lp_locked < LP_LOCKED_FLOOR_PCT:
            flags.append(f"Only {pct(lp_locked)} of LP locked — dev can pull liquidity.")

    # top holder concentration
    holders = rep.get("topHolders") or []
    if holders:
        top10 = sum((h.get("pct") or 0) for h in holders[:10])
        metrics["top10_pct"] = top10
        notes.append(f"Top 10 holders: {top10:.1f}% of supply")
        if top10 > TOP10_CONCENTRATION_PCT:
            flags.append(f"Top 10 wallets hold {top10:.0f}% — one coordinated dump ends it.")
        insiders = sum((h.get("pct") or 0) for h in holders if h.get("insider"))
        metrics["insider_pct"] = insiders
        if insiders > INSIDER_CONCENTRATION_PCT:
            flags.append(f"Flagged insider wallets hold ~{insiders:.0f}%.")

    # explicit risks
    for r in rep.get("risks") or []:
        lvl = (r.get("level") or "").lower()
        if lvl in ("danger", "warn", "warning"):
            flags.append(
                f"RugCheck risk: {r.get('name','?')} — {r.get('description','')}".strip()
            )

    return flags, notes, metrics


def _classify_honeypot_error(hp: dict[str, Any]) -> str:
    """Classify why honeypot.is gave us no usable answer.

    Returns a short human-readable label. The raw upstream message is
    inspected for known signals (HTTP 429, 5xx, timeout, missing pair,
    API-body `error` field) and bucketed into one of five categories;
    anything else falls through to a truncated copy of the raw message.
    """
    if not hp:
        return "no response"
    raw = hp.get("_error") or hp.get("error") or ""
    s = str(raw).lower()
    if "http 429" in s or "rate" in s:
        return "rate-limited (try again in a minute)"
    if "timeout" in s:
        return "request timed out"
    if "http 5" in s:
        return "service error (honeypot.is returned 5xx)"
    if "pair" in s and "not" in s:
        return "no liquidity pair found"
    if "http 4" in s:
        # 400/404 from this endpoint usually means: unsupported chain,
        # no LP for the token, or the token isn't indexed yet.
        return "no liquidity pair found, or token not indexed yet"
    if raw:
        return str(raw)[:120]
    return "unknown error"


def analyze_honeypot(hp: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    flags: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {
        "is_honeypot": None,
        "buy_tax_pct": None,
        "sell_tax_pct": None,
        "open_source": None,
        "available": True,
        "error_reason": None,
    }
    # A valid honeypot.is response always carries `honeypotResult`. Anything
    # missing it is treated as unavailable and we classify why.
    if not hp or "_error" in hp or "honeypotResult" not in hp:
        reason = _classify_honeypot_error(hp or {})
        notes.append(f"honeypot.is: unavailable ({reason}).")
        metrics["available"] = False
        metrics["error_reason"] = reason
        return flags, notes, metrics

    hr = hp.get("honeypotResult") or {}
    metrics["is_honeypot"] = bool(hr.get("isHoneypot")) if "isHoneypot" in hr else None
    if hr.get("isHoneypot"):
        flags.append(
            f"HONEYPOT: you can buy but not sell — {hr.get('honeypotReason','no reason given')}."
        )
    else:
        notes.append("Honeypot check: can sell")

    sim = hp.get("simulationResult") or {}
    buy_tax = sim.get("buyTax")
    sell_tax = sim.get("sellTax")
    metrics["buy_tax_pct"] = buy_tax
    metrics["sell_tax_pct"] = sell_tax
    if buy_tax is not None or sell_tax is not None:
        notes.append(f"Buy tax: {pct(buy_tax)}   Sell tax: {pct(sell_tax)}")
        if (sell_tax or 0) > SELL_TAX_CEILING_PCT:
            flags.append(f"Sell tax {pct(sell_tax)} — the contract skims you on exit.")

    contract = hp.get("contractCode") or {}
    metrics["open_source"] = contract.get("openSource")
    if contract.get("openSource") is False:
        flags.append("Contract not fully open source — unverifiable behaviour.")

    for f in hp.get("flags") or []:
        notes.append(f"flag: {f}")

    return flags, notes, metrics
