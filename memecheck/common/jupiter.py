"""Jupiter aggregator quote API client.

Used to get a *realistic* price-impact estimate for Solana token buys,
accounting for multi-pool routing and concentrated-liquidity math.

The naive constant-product V2 estimate in `liquidity_math.py` is
intentionally pessimistic — it computes price impact on the deepest
*single* pool, ignoring the fact that real routers (Jupiter on Solana,
1inch on EVM) split trades across pools and CLMM ticks. Jupiter's
production routing is exactly the answer to "what would you actually
pay?" so we delegate to it for Solana and label the result as
realistic.

Endpoint: https://lite-api.jup.ag/swap/v1/quote (no auth, public).

EVM tokens are out of scope here — for a parallel EVM implementation,
1inch or 0x would be the equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from memecheck.common.http import get_json


# Well-known quote-side mints on Solana. The decimals are baked in
# because DexScreener occasionally omits them from the pair payload.
SOLANA_QUOTE_DECIMALS: dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
}


@dataclass(frozen=True)
class JupiterQuote:
    """One realistic-impact estimate from Jupiter's aggregator."""

    input_mint: str
    output_mint: str
    in_amount: int                       # raw atomic units in
    out_amount: int                      # raw atomic units out
    price_impact_pct: float              # already in percent (not decimal)
    route_hops: int                      # number of pools the route traverses
    raw: dict = None  # type: ignore[assignment]


def fetch_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_in_atomic: int,
    slippage_bps: int = 50,
    restrict_intermediate_tokens: bool = True,
) -> Optional[JupiterQuote]:
    """Hit Jupiter's quote API. Returns None on failure or malformed payload."""
    if amount_in_atomic <= 0:
        return None
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_in_atomic),
        "slippageBps": str(slippage_bps),
    }
    if restrict_intermediate_tokens:
        params["restrictIntermediateTokens"] = "true"
    url = "https://lite-api.jup.ag/swap/v1/quote?" + urlencode(params)
    data = get_json(url, timeout=10)
    if "_error" in data:
        return None
    try:
        out_amount = int(data.get("outAmount") or 0)
        if out_amount <= 0:
            return None
        # priceImpactPct in Jupiter's response is a decimal string like "0.00016".
        impact = float(data.get("priceImpactPct") or 0)
        return JupiterQuote(
            input_mint=str(data.get("inputMint") or input_mint),
            output_mint=str(data.get("outputMint") or output_mint),
            in_amount=int(data.get("inAmount") or amount_in_atomic),
            out_amount=out_amount,
            price_impact_pct=impact * 100,    # convert decimal → percent
            route_hops=len(data.get("routePlan") or []),
            raw=data,
        )
    except (TypeError, ValueError):
        return None


def estimate_realistic_buy_for_solana(
    base_mint: str,
    quote_mint: str,
    buy_size_usd: float,
    quote_price_usd: float,
    quote_decimals: Optional[int] = None,
) -> Optional[JupiterQuote]:
    """High-level helper: convert a USD buy size into a Jupiter quote
    against the given quote-side token (SOL / USDC / etc.).

    Returns None if the quote can't be fetched (Jupiter outage, no route,
    unsupported mint, etc.). Callers should fall back to the V2 estimate
    in that case.
    """
    if buy_size_usd <= 0 or quote_price_usd <= 0:
        return None
    decimals = quote_decimals
    if decimals is None:
        decimals = SOLANA_QUOTE_DECIMALS.get(quote_mint)
    if decimals is None:
        # Without decimals we can't safely build the atomic amount.
        return None
    quote_units = buy_size_usd / quote_price_usd
    atomic = int(round(quote_units * (10 ** decimals)))
    if atomic <= 0:
        return None
    return fetch_jupiter_quote(
        input_mint=quote_mint,
        output_mint=base_mint,
        amount_in_atomic=atomic,
    )
