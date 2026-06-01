"""Data source clients: DexScreener, RugCheck, honeypot.is.

Each function returns raw parsed JSON (or {'_error': ...}) so analyzers can
be tested independently of network behavior.
"""

from __future__ import annotations

from typing import Any, Optional

from memecheck.common.http import get_json

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
