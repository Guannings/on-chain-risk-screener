"""Pre-trade scanner runner.

Synchronous one-shot scan that pulls DexScreener / RugCheck / honeypot.is,
runs the analyzers, and optionally simulates exit-liquidity for a specified
buy size. Same behavior as the legacy memecheck.py — module split only.
"""

from __future__ import annotations

from typing import Any, Optional

from memecheck.common.analyzers import (
    analyze_dexscreener,
    analyze_honeypot,
    analyze_rugcheck,
)
from memecheck.common.format import fmt_usd, is_solana_address, pct
from memecheck.common.liquidity_math import (
    derive_reserves,
    fee_bps_for_dex,
    max_safe_buy_usd,
    round_trip_slippage,
)
from memecheck.common.sources import (
    EVM_CHAIN_IDS,
    fetch_dexscreener,
    fetch_honeypot,
    fetch_rugcheck,
)
from memecheck.common.verdict import (
    EXIT_SLIPPAGE_FLAG_PCT,
    EXIT_SLIPPAGE_SEVERE_PCT,
    exit_code_for,
    make_verdict,
)


def _analyze_exit_liquidity(
    primary: dict[str, Any],
    buy_size_usd: float,
    max_slippage_pct: float,
    fee_bps_override: Optional[int],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Simulate a buy_size_usd buy on the primary pool and report slippage."""
    flags: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {
        "buy_size_usd": buy_size_usd,
        "max_slippage_pct": max_slippage_pct,
        "supported": False,
    }

    derived = derive_reserves(primary)
    if derived is None:
        notes.append(
            "Exit simulator: pool reserves not available from DexScreener — skipping."
        )
        return flags, notes, metrics

    base_r, quote_r, qp_usd = derived
    fee_bps = fee_bps_override if fee_bps_override is not None else fee_bps_for_dex(
        primary.get("dexId")
    )

    sim = round_trip_slippage(buy_size_usd, base_r, quote_r, qp_usd, fee_bps)
    safe = max_safe_buy_usd(max_slippage_pct, base_r, quote_r, qp_usd, fee_bps)

    metrics["supported"] = True
    metrics["fee_bps"] = fee_bps
    metrics["pool_quote_depth_usd"] = sim["pool_quote_depth_usd"]
    metrics["displayed_price_usd"] = sim["displayed_price_usd"]
    metrics["effective_buy_price_usd"] = sim["effective_buy_price_usd"]
    metrics["round_trip_pct"] = sim["round_trip_pct"]
    metrics["realised_usd"] = sim["realised_usd"]
    metrics["price_impact_pct"] = sim["price_impact_pct"]
    metrics["max_safe_buy_usd"] = safe

    notes.append(
        f"Exit-liquidity simulator (buy size: {fmt_usd(buy_size_usd)}, fee {fee_bps} bps)"
    )
    notes.append(f"  Pool quote-side depth: {fmt_usd(sim['pool_quote_depth_usd'])}")
    if sim["displayed_price_usd"] is not None:
        notes.append(
            f"  Displayed price:     ${sim['displayed_price_usd']:.10g}"
        )
    if sim["effective_buy_price_usd"] is not None and sim["price_impact_pct"] is not None:
        notes.append(
            f"  Effective buy price: ${sim['effective_buy_price_usd']:.10g}  "
            f"(price impact {sim['price_impact_pct']:+.1f}%)"
        )
    if sim["round_trip_pct"] is not None and sim["realised_usd"] is not None:
        notes.append(
            f"  Immediate round-trip: {sim['round_trip_pct']:.2f}% loss  "
            f"(would get back {fmt_usd(sim['realised_usd'])})"
        )
        notes.append(
            "    — note: round-trip is bounded by ~2*fee on V2-style AMMs and "
            "is NOT a measure of 'stuck bag' risk."
        )
    notes.append(
        f"  To stay under {max_slippage_pct:.1f}% PRICE IMPACT, buy ≤ {fmt_usd(safe)}."
    )

    impact = sim["price_impact_pct"] or 0
    if impact >= EXIT_SLIPPAGE_SEVERE_PCT:
        flags.append(
            f"Buying {fmt_usd(buy_size_usd)} here moves the price {impact:.0f}% above "
            f"displayed — this pool is too thin for your intended size."
        )
    elif impact >= EXIT_SLIPPAGE_FLAG_PCT:
        flags.append(
            f"Buying {fmt_usd(buy_size_usd)} here moves the price {impact:.1f}% above "
            f"displayed (above {EXIT_SLIPPAGE_FLAG_PCT:.0f}% threshold)."
        )

    # Realistic multi-pool estimate via Jupiter for Solana tokens.
    if (primary.get("chainId") or "").lower() == "solana":
        from memecheck.common.jupiter import estimate_realistic_buy_for_solana
        base_mint = (primary.get("baseToken") or {}).get("address")
        quote_mint = (primary.get("quoteToken") or {}).get("address")
        if base_mint and quote_mint:
            jq = estimate_realistic_buy_for_solana(
                base_mint=str(base_mint),
                quote_mint=str(quote_mint),
                buy_size_usd=buy_size_usd,
                quote_price_usd=qp_usd,
            )
            if jq is not None:
                metrics["jupiter_price_impact_pct"] = jq.price_impact_pct
                metrics["jupiter_route_hops"] = jq.route_hops
                metrics["jupiter_out_amount_atomic"] = jq.out_amount
                notes.append(
                    f"  Realistic (Jupiter, {jq.route_hops}-hop route): "
                    f"price impact {jq.price_impact_pct:+.2f}%  "
                    f"(vs {impact:+.2f}% V2 single-pool estimate)"
                )
                # If the V2 estimate fired a flag but Jupiter says impact
                # is actually small, soften with a note.
                if impact >= EXIT_SLIPPAGE_FLAG_PCT and jq.price_impact_pct < EXIT_SLIPPAGE_FLAG_PCT:
                    notes.append(
                        "  → Single-pool flag may be conservative; Jupiter "
                        "would route around the thin pool."
                    )
            else:
                notes.append(
                    "  Realistic (Jupiter): quote unavailable; V2 estimate only."
                )

    return flags, notes, metrics


def run_token(
    addr: str,
    forced_chain: Optional[str] = None,
    as_json: bool = False,
    buy_size_usd: Optional[float] = None,
    max_slippage_pct: float = 5.0,
    fee_bps_override: Optional[int] = None,
) -> tuple[dict[str, Any], int]:
    """Run the full check. Returns (result_dict, exit_code).
       Prints human-readable output unless as_json=True."""
    result: dict[str, Any] = {
        "address": addr,
        "forced_chain": forced_chain,
        "chain_type": "solana" if is_solana_address(addr) else "evm",
        "sources": {},
        "flags": [],
        "verdict": None,
    }
    if not as_json:
        print(f"\n########## memecheck: {addr} ##########")

    p, pairs, err = fetch_dexscreener(addr, forced_chain)
    if err:
        result["sources"]["dexscreener"] = {"error": err}
        if not as_json:
            print(f"[DexScreener] {err}")

    all_flags: list[str] = []
    dex_metrics: Optional[dict[str, Any]] = None
    honeypot_metrics: Optional[dict[str, Any]] = None

    if p:
        f, notes, dex_metrics = analyze_dexscreener(p, pairs)
        result["sources"]["dexscreener"] = {
            "flags": f, "notes": notes, "metrics": dex_metrics
        }
        if not as_json:
            print("\n--- Market (DexScreener) ---")
            for n in notes:
                print(f"  {n}")
        all_flags += f

    # Exit-liquidity simulator runs only when the user specifies --buy-size,
    # because it's a per-user question (how much do you intend to put in?).
    if p and buy_size_usd is not None and buy_size_usd > 0:
        f, notes, exit_metrics = _analyze_exit_liquidity(
            p, buy_size_usd, max_slippage_pct, fee_bps_override
        )
        result["sources"]["exit_simulator"] = {
            "flags": f, "notes": notes, "metrics": exit_metrics
        }
        if not as_json:
            print("\n--- Exit-liquidity simulator ---")
            for n in notes:
                print(f"  {n}")
        all_flags += f

    if is_solana_address(addr):
        rep = fetch_rugcheck(addr)
        f, notes, rc_metrics = analyze_rugcheck(rep)
        result["sources"]["rugcheck"] = {
            "flags": f, "notes": notes, "metrics": rc_metrics
        }
        if not as_json:
            print("\n--- Contract & holders (RugCheck / Solana) ---")
            for n in notes:
                print(f"  {n}")
        all_flags += f
    else:
        chain = forced_chain or (p.get("chainId") if p else None) or "ethereum"
        cid = EVM_CHAIN_IDS.get(str(chain).lower(), 1)
        hp = fetch_honeypot(addr, cid)
        f, notes, honeypot_metrics = analyze_honeypot(hp)
        result["sources"]["honeypot"] = {
            "chain_id": cid, "flags": f, "notes": notes, "metrics": honeypot_metrics
        }
        if not as_json:
            print(f"\n--- Contract (honeypot.is / EVM chainID {cid}) ---")
            for n in notes:
                print(f"  {n}")
        all_flags += f

    verdict = make_verdict(all_flags, honeypot_metrics)
    result["flags"] = all_flags
    result["verdict"] = verdict

    if not as_json:
        print("\n================ RED FLAGS ================")
        if all_flags:
            for fl in all_flags:
                print(f"  [!] {fl}")
        print(f"\nVerdict: {verdict}")
        print("Not financial advice. The checks catch rugs, not bad bets.")

    code = exit_code_for(verdict, all_flags)
    if not p and not result["sources"]:
        code = 3
    return result, code
