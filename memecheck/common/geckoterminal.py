"""GeckoTerminal fallback DEX data source.

When DexScreener returns an error or empty payload, we fall back to
GeckoTerminal (CoinGecko's on-chain analytics arm — US/UK based, free
public API, no auth). The endpoint we use:

    GET /api/v2/networks/{network}/tokens/{address}/pools

returns up to 20 pools for the token, sorted by depth. We pick the
deepest, then normalise its attributes into the same DexScreener pair
dict shape the rest of the codebase already consumes.

The trick: `derive_reserves` in `liquidity_math.py` only needs
`priceUsd`, `priceNative`, and `liquidity.usd` to compute V2-equivalent
reserves from GT's data. Everything downstream — flag rules, exit
sim — keeps working unchanged.

Caveats
-------
- GT's `volume_usd` keys differ from DexScreener's (`m5`, `h1`, `h24`
  vs `m5`, `h1`, `h6`, `h24`). We map the ones we need.
- GT doesn't publish per-side liquidity (base/quote token counts);
  we leave those keys absent so `derive_reserves` uses the fallback path.
- GT's chain slugs differ (`eth` vs `ethereum`). `CHAIN_DS_TO_GT`
  translates.
- GT publishes `transactions.h24.buys/sells` separately; we sum.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from memecheck.common.http import get_json


# DexScreener chain slugs → GeckoTerminal network slugs.
CHAIN_DS_TO_GT: dict[str, str] = {
    "ethereum": "eth",
    "bsc": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
    "optimism": "optimism",
    "avalanche": "avax",
    "solana": "solana",
    "fantom": "ftm",
}


def _parse_iso_to_ms(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        # GT returns e.g. "2023-11-20T20:10:04Z"; Python <3.11 needs +00:00.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OSError):
        return None


def _split_token_id(tid: Optional[str]) -> Optional[str]:
    """GT token IDs look like 'solana_<MINT>' or 'eth_<ADDRESS>'. Returns just the address."""
    if not tid:
        return None
    _, _, rest = tid.partition("_")
    return rest or tid


def _split_name(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'$WIF / SOL' → ('$WIF', 'SOL'). Falls back to (None, None) on weird input."""
    if not name or "/" not in name:
        return None, None
    base, _, quote = name.partition("/")
    return base.strip() or None, quote.strip() or None


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _gt_pool_to_ds_pair(pool: dict[str, Any], gt_chain_slug: str) -> Optional[dict[str, Any]]:
    """Convert one GT pool JSON to a DexScreener-shaped pair dict.

    Returns None if the pool is unusable (missing core price / depth).
    """
    attrs = pool.get("attributes") or {}
    rels = pool.get("relationships") or {}

    price_usd = _to_float(attrs.get("base_token_price_usd"))
    price_native = _to_float(attrs.get("base_token_price_native_currency"))
    reserve_usd = _to_float(attrs.get("reserve_in_usd"))
    if not price_usd or not price_native or reserve_usd is None:
        return None

    base_addr = _split_token_id((rels.get("base_token") or {}).get("data", {}).get("id"))
    quote_addr = _split_token_id((rels.get("quote_token") or {}).get("data", {}).get("id"))
    base_sym, quote_sym = _split_name(attrs.get("name"))
    dex_id = (rels.get("dex") or {}).get("data", {}).get("id")
    pair_address = attrs.get("address")

    # Translate GT network slug back to DexScreener's convention.
    ds_chain = next(
        (k for k, v in CHAIN_DS_TO_GT.items() if v == gt_chain_slug),
        gt_chain_slug,
    )

    volume_block = attrs.get("volume_usd") or {}
    h24_vol = _to_float(volume_block.get("h24"))
    h1_vol = _to_float(volume_block.get("h1"))

    txns_block = attrs.get("transactions") or {}
    h24_tx = txns_block.get("h24") or {}
    h24_buys = int(h24_tx.get("buys") or 0)
    h24_sells = int(h24_tx.get("sells") or 0)

    return {
        "_source": "geckoterminal",
        "chainId": ds_chain,
        "pairAddress": pair_address,
        "dexId": dex_id,
        "baseToken": {"address": base_addr, "symbol": base_sym, "name": base_sym},
        "quoteToken": {"address": quote_addr, "symbol": quote_sym, "name": quote_sym},
        "priceUsd": str(price_usd),
        "priceNative": str(price_native),
        "liquidity": {"usd": reserve_usd},
        "volume": {"h24": h24_vol, "h1": h1_vol},
        "txns": {"h24": {"buys": h24_buys, "sells": h24_sells}},
        "pairCreatedAt": _parse_iso_to_ms(attrs.get("pool_created_at")),
        "fdv": _to_float(attrs.get("fdv_usd")),
        "marketCap": _to_float(attrs.get("market_cap_usd")),
    }


def fetch_geckoterminal(
    addr: str,
    forced_chain: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """Try every plausible GT network slug for `addr`; return DS-shaped pairs.

    Returns (primary, all_pairs, error) — same shape as fetch_dexscreener.
    """
    # Determine which network slugs to try.
    if forced_chain:
        gt_slug = CHAIN_DS_TO_GT.get(forced_chain.lower(), forced_chain.lower())
        candidates = [gt_slug]
    else:
        # Try Solana first (memecoins live here), then EVMs by deployment volume.
        candidates = ["solana", "eth", "base", "bsc", "arbitrum"]

    last_err: Optional[str] = None
    for slug in candidates:
        url = f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{addr}/pools"
        data = get_json(url, timeout=15)
        if "_error" in data:
            last_err = data["_error"]
            continue
        pools = data.get("data") or []
        if not pools:
            continue
        converted = [
            p for p in (_gt_pool_to_ds_pair(pool, slug) for pool in pools)
            if p is not None
        ]
        if not converted:
            continue
        # GT returns deepest-first already, but sort to be sure.
        converted.sort(
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
            reverse=True,
        )
        return converted[0], converted, None

    return None, [], last_err or "no pools on any GeckoTerminal network"
