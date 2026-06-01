"""Async liquidity sources.

A monitor's `source` produces a stream of `LiquidityEvent`s — one event per
observation of the pool's reserves and derived price. The rest of the
monitor pipeline (state → decision → action) consumes from this stream and
does not care which source produced it.

Phase 1a ships one implementation:

    DexScreenerPollSource
      Polls https://api.dexscreener.com/latest/dex/pairs/{chain}/{pairAddr}
      every `interval` seconds. Resolves the deepest pool for the token
      at construction time via the existing scanner's fetch_dexscreener.
      No new runtime deps — uses stdlib urllib via asyncio.run_in_executor.

Phase 1b will add:

    RaydiumVaultSource
      Subscribes to the pool's two vault accounts via the Solana RPC
      websocket (`accountSubscribe`, jsonParsed encoding). Sub-second
      latency. Solana Raydium AMM v4 only.

Both sources yield the same `LiquidityEvent` shape, so the rest of the
monitor is source-agnostic.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from memecheck.common.http import get_json
from memecheck.common.liquidity_math import derive_reserves
from memecheck.common.sources import fetch_dexscreener


@dataclass(frozen=True)
class LiquidityEvent:
    """One snapshot of a pool's state."""

    ts: float                      # unix-epoch seconds, when this observation was made
    base_reserve: float            # reserves of the base (token-of-interest) side
    quote_reserve: float           # reserves of the quote (SOL/USDC/WETH) side
    quote_price_usd: float         # USD per 1 quote token
    liquidity_usd: float           # 2 * quote_reserve * quote_price_usd
    price_usd: float               # marginal price of base in USD
    source: str = "dexscreener-poll"
    chain: Optional[str] = None
    pair_address: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class LiquiditySource:
    """Abstract async source. Implementations override `stream()`."""

    def stream(self) -> AsyncIterator[LiquidityEvent]:
        raise NotImplementedError


@dataclass
class _ResolvedPool:
    chain: str
    pair_address: str
    base_symbol: Optional[str]
    quote_symbol: Optional[str]
    dex_id: Optional[str]


def _resolve_pool(addr: str, forced_chain: Optional[str]) -> _ResolvedPool:
    """Find the deepest pool for `addr`, return its chain + pair address.

    Reuses the existing synchronous fetch_dexscreener so the scanner's
    aggregation/chain-locking logic is shared.
    """
    primary, _pairs, err = fetch_dexscreener(addr, forced_chain)
    if err or not primary:
        raise RuntimeError(f"Cannot resolve pool for {addr}: {err or 'no pair'}")
    chain = primary.get("chainId")
    pair_address = primary.get("pairAddress")
    if not chain or not pair_address:
        raise RuntimeError(
            f"DexScreener returned an incomplete pair for {addr}: chain={chain!r}, "
            f"pairAddress={pair_address!r}"
        )
    return _ResolvedPool(
        chain=str(chain),
        pair_address=str(pair_address),
        base_symbol=(primary.get("baseToken") or {}).get("symbol"),
        quote_symbol=(primary.get("quoteToken") or {}).get("symbol"),
        dex_id=primary.get("dexId"),
    )


def _event_from_pair(pair: dict[str, Any], pool: _ResolvedPool) -> Optional[LiquidityEvent]:
    """Build a LiquidityEvent from a DexScreener pair JSON. None if unusable."""
    derived = derive_reserves(pair)
    if derived is None:
        return None
    base_r, quote_r, qp_usd = derived
    try:
        price_usd = float(pair.get("priceUsd")) if pair.get("priceUsd") is not None else None
    except (TypeError, ValueError):
        price_usd = None
    if price_usd is None:
        # Fall back to constant-product derived price.
        if base_r > 0:
            price_usd = (quote_r / base_r) * qp_usd
        else:
            return None
    return LiquidityEvent(
        ts=time.time(),
        base_reserve=base_r,
        quote_reserve=quote_r,
        quote_price_usd=qp_usd,
        liquidity_usd=2 * quote_r * qp_usd,
        price_usd=price_usd,
        source="dexscreener-poll",
        chain=pool.chain,
        pair_address=pool.pair_address,
        raw={
            "chain": pool.chain,
            "pair_address": pool.pair_address,
            "dex_id": pool.dex_id,
        },
    )


class DexScreenerPollSource(LiquiditySource):
    """Polls DexScreener's /pairs endpoint and yields LiquidityEvents.

    On construction, resolves the deepest pool for `addr` (so subsequent
    polls hit a single specific pair endpoint, not the token endpoint).
    Errors from individual polls are swallowed and reported via a callback;
    a single bad poll never crashes the stream.
    """

    def __init__(
        self,
        addr: str,
        interval_seconds: float = 5.0,
        forced_chain: Optional[str] = None,
        on_error: Optional[callable] = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._addr = addr
        self._interval = interval_seconds
        self._on_error = on_error or (lambda msg: None)
        # Resolve once, eagerly, so caller knows immediately if the token isn't tradable.
        self._pool = _resolve_pool(addr, forced_chain)

    @property
    def pool(self) -> _ResolvedPool:
        return self._pool

    def _poll_once(self) -> Optional[LiquidityEvent]:
        url = (
            f"https://api.dexscreener.com/latest/dex/pairs/"
            f"{self._pool.chain}/{self._pool.pair_address}"
        )
        data = get_json(url)
        if "_error" in data:
            self._on_error(f"poll error: {data['_error']}")
            return None
        # /pairs endpoint returns either {"pair": {...}} or {"pairs": [...]}.
        pair = data.get("pair")
        if not pair:
            pairs = data.get("pairs") or []
            pair = pairs[0] if pairs else None
        if not pair:
            self._on_error("poll error: empty pair payload")
            return None
        event = _event_from_pair(pair, self._pool)
        if event is None:
            self._on_error("poll error: could not derive reserves")
        return event

    async def stream(self) -> AsyncIterator[LiquidityEvent]:
        loop = asyncio.get_running_loop()
        while True:
            event = await loop.run_in_executor(None, self._poll_once)
            if event is not None:
                yield event
            await asyncio.sleep(self._interval)
