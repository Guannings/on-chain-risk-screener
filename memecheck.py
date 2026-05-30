#!/usr/bin/env python3
"""
memecheck.py — on-chain risk screener for a single token.

Pulls REAL live data (no API keys required for basic use):
  - DexScreener       -> aggregated liquidity, market cap / FDV, 24h volume,
                         age, buy/sell flow across every pool on one chain
  - RugCheck (Solana) -> contract risks, mint/freeze authority, LP locked %,
                         top-holder concentration
  - honeypot.is (EVM) -> honeypot detection, buy/sell tax, source verification

Auto-detects Solana vs EVM from the address format. Zero third-party
dependencies (stdlib only).

Usage:
    python3 memecheck.py <TOKEN_ADDRESS>
    python3 memecheck.py <TOKEN_ADDRESS> --chain base   # force EVM chain
    python3 memecheck.py <TOKEN_ADDRESS> --json         # structured output
    python3 memecheck.py --liq 0.0000123 --lev 10       # liquidation calc only

Exit codes:
    0  no automatic red flags found
    1  red flags raised (RISKY or HARD PASS verdict)
    2  honeypot detected (highest-severity finding)
    3  no data available for the supplied address

Nothing here is financial advice. The checks catch rugs, not bad bets.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):
    sys.stderr.write("memecheck.py requires Python 3.9 or newer.\n")
    sys.exit(1)

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

# ----------------------------- constants ---------------------------------

UA: dict[str, str] = {"User-Agent": "memecheck/1.0"}

# Verdict thresholds — tweak here, document in README.
HARD_PASS_FLAG_COUNT: int = 4         # >= this many flags => HARD PASS
THIN_LIQ_USD: float = 20_000.0        # below this is "thin"
LOW_LIQ_MC_RATIO: float = 0.03        # liq/mc under this is "tiny float"
WASH_VOL_LIQ_RATIO: float = 50.0      # 24h vol / liq above this hints at wash
DEAD_VOL_LIQ_RATIO: float = 0.05      # 24h vol / liq below this is "dead"
DEAD_VOL_LIQ_TRUST_CEILING: float = 2_000_000.0  # only trust 'dead' under this liq
NEW_TOKEN_HOURS: float = 24.0         # younger than this => peak rug window
SELL_PRESSURE_RATIO: float = 1.5      # sells/buys above this => distribution
LP_LOCKED_FLOOR_PCT: float = 50.0     # LP locked under this => dev can pull
TOP10_CONCENTRATION_PCT: float = 50.0 # top10 above this => one-dump risk
INSIDER_CONCENTRATION_PCT: float = 15.0
SELL_TAX_CEILING_PCT: float = 10.0    # sell tax above this is a flag

# DexScreener chainId -> honeypot.is numeric chainID
EVM_CHAIN_IDS: dict[str, int] = {
    "ethereum": 1, "eth": 1,
    "bsc": 56, "binance": 56,
    "base": 8453,
    "arbitrum": 42161, "arb": 42161,
    "polygon": 137, "matic": 137,
    "optimism": 10, "op": 10,
    "avalanche": 43114, "avax": 43114,
}


# ----------------------------- helpers -----------------------------------

def get_json(url: str, timeout: int = 15) -> dict[str, Any]:
    """GET a URL and return parsed JSON, or {'_error': ...} on failure."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} for {url}"}
    except Exception as e:  # noqa: BLE001 — network is wild, swallow + report
        return {"_error": f"{type(e).__name__}: {e} ({url})"}


def is_solana_address(addr: str) -> bool:
    """EVM = 0x + 40 hex; Solana = base58, ~32-44 chars, no 0x."""
    return not addr.startswith("0x") and bool(
        re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr)
    )


def fmt_usd(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x/div:.2f}{unit}"
    return f"${x:,.2f}"


def pct(x: Any) -> str:
    return "n/a" if x is None else f"{float(x):.1f}%"


# --------------------------- data sources --------------------------------

def _liq_of(p: dict[str, Any]) -> float:
    return (p.get("liquidity") or {}).get("usd", 0) or 0


def fetch_dexscreener(
    addr: str, forced_chain: Optional[str] = None
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """Return (primary_pair, same_chain_pairs, error).

    primary = deepest pool (used for price/label); same_chain_pairs = every
    pool for this token on the primary's chain (used for aggregated
    liquidity/volume).
    """
    data = get_json(f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
    if "_error" in data:
        return None, [], data["_error"]
    pairs = data.get("pairs") or []
    if not pairs:
        return None, [], "No DEX pairs found (unlisted, wrong address, or chain not indexed)."
    pairs.sort(key=_liq_of, reverse=True)
    # Lock to one chain so we never sum liquidity across different deployments.
    chain = (forced_chain or pairs[0].get("chainId") or "").lower()
    same_chain = [p for p in pairs if (p.get("chainId") or "").lower() == chain] or pairs
    same_chain.sort(key=_liq_of, reverse=True)
    return same_chain[0], same_chain, None


def fetch_rugcheck(mint: str) -> dict[str, Any]:
    """Full report carries holders + authorities; summary is the fallback."""
    rep = get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if "_error" in rep:
        rep = get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    return rep


def fetch_honeypot(addr: str, chain_id: int) -> dict[str, Any]:
    return get_json(f"https://api.honeypot.is/v2/IsHoneypot?address={addr}&chainID={chain_id}")


# ----------------------------- analysis ----------------------------------

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


def analyze_honeypot(hp: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    flags: list[str] = []
    notes: list[str] = []
    metrics: dict[str, Any] = {
        "is_honeypot": None,
        "buy_tax_pct": None,
        "sell_tax_pct": None,
        "open_source": None,
        "available": True,
    }
    if not hp or "_error" in hp:
        notes.append("honeypot.is: unavailable.")
        metrics["available"] = False
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


# ------------------------- verdict ----------------------------------------

def make_verdict(all_flags: list[str], honeypot_metrics: Optional[dict[str, Any]]) -> str:
    if honeypot_metrics and honeypot_metrics.get("is_honeypot"):
        return "HONEYPOT — do not buy"
    if not all_flags:
        return "No automatic red flags — but 'no flags' != 'good bet'. Narrative/timing still decide it."
    if len(all_flags) >= HARD_PASS_FLAG_COUNT:
        return "HARD PASS"
    return "RISKY — proceed only with money already written off"


def exit_code_for(verdict: str, all_flags: list[str]) -> int:
    if verdict.startswith("HONEYPOT"):
        return 2
    if verdict.startswith("HARD PASS") or verdict.startswith("RISKY"):
        return 1
    return 0


# ------------------------- leverage math ----------------------------------

def liquidation_price(
    entry: float, leverage: float, side: str = "long", maint_margin: float = 0.005
) -> float:
    """Approx isolated-margin liquidation price.
       long:  P_liq = P * (1 - 1/L + mm)
       short: P_liq = P * (1 + 1/L - mm)"""
    if side == "long":
        return entry * (1 - 1 / leverage + maint_margin)
    return entry * (1 + 1 / leverage - maint_margin)


def liq_report(entry: float, leverage: float) -> None:
    print("\n=== LIQUIDATION MATH (isolated margin, approx) ===")
    for side in ("long", "short"):
        lp = liquidation_price(entry, leverage, side)
        move = abs(lp - entry) / entry * 100
        print(
            f"  {side.upper():5s} @ {leverage}x  ->  liq price {lp:.10g}  "
            f"(a {move:.1f}% adverse move wipes you)"
        )
    print(
        "  Reminder: memecoins do double-digit % candles routinely. "
        "Above ~3x, noise alone liquidates you before any thesis plays out."
    )


def liq_report_dict(entry: float, leverage: float) -> dict[str, Any]:
    out: dict[str, Any] = {"entry": entry, "leverage": leverage, "sides": {}}
    for side in ("long", "short"):
        lp = liquidation_price(entry, leverage, side)
        out["sides"][side] = {
            "liq_price": lp,
            "adverse_move_pct": abs(lp - entry) / entry * 100,
        }
    return out


# ------------------------------- runner -----------------------------------

def run_token(
    addr: str, forced_chain: Optional[str] = None, as_json: bool = False
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


def main() -> None:
    ap = argparse.ArgumentParser(description="On-chain risk screener for a single token")
    ap.add_argument("address", nargs="?", help="token contract / mint address")
    ap.add_argument("--chain", help="force EVM chain (ethereum/base/bsc/arbitrum/...)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="emit structured JSON instead of human-readable text")
    ap.add_argument("--liq", type=float, help="entry price for liquidation calc")
    ap.add_argument("--lev", type=float, help="leverage for liquidation calc")
    args = ap.parse_args()

    # liquidation-only mode (no address)
    if args.liq and args.lev and not args.address:
        if args.as_json:
            print(json.dumps(liq_report_dict(args.liq, args.lev), indent=2))
        else:
            liq_report(args.liq, args.lev)
        return

    if not args.address:
        ap.print_help()
        sys.exit(1)

    result, code = run_token(args.address.strip(), args.chain, as_json=args.as_json)

    if args.liq and args.lev:
        result["liquidation"] = liq_report_dict(args.liq, args.lev)
        if not args.as_json:
            liq_report(args.liq, args.lev)

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))

    sys.exit(code)


if __name__ == "__main__":
    main()
