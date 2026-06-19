"""Hyperliquid WebSocket subscription wrapper.

Hyperliquid's public WS endpoint is `wss://api.hyperliquid.xyz/ws`.
Subscriptions use this envelope:

    {"method": "subscribe",
     "subscription": {"type": "<channel>", "coin": "<COIN>"}}

Useful public channels:
  - "trades"     — every trade for one coin
  - "allMids"    — mid-price ticks for all coins (no coin field needed)
  - "l2Book"     — orderbook snapshots
  - "candle"     — OHLCV with an "interval" field

Server messages:
  {"channel": "subscriptionResponse", "data": {...}}      ack
  {"channel": "trades",      "data": [<trade>, ...]}
  {"channel": "allMids",     "data": {"mids": {"BTC": "...", ...}}}

Heartbeat: send `{"method": "ping"}` periodically; HL replies with a
pong message on the WS layer (the underlying ws_client also handles
RFC 6455 control-frame pings transparently).

This module is a thin yielding adapter — the WS client does the heavy
lifting; we add subscribe envelopes, JSON parsing, and a small
normalisation layer so callers see clean dataclasses instead of raw
strings.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from memecheck.common.ws_client import WebSocket, WebSocketError


HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass(frozen=True)
class HLTrade:
    """One trade from the Hyperliquid trades channel."""
    coin: str
    px: float
    sz: float
    side: str               # "B" (buy) or "A" (sell)
    ts_ms: int


@dataclass(frozen=True)
class HLMids:
    """One mid-price tick from allMids."""
    mids: dict           # coin → price (as float)
    received_at: float   # local time the message was received


def _to_float(x: object) -> Optional[float]:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def stream_trades(coin: str, timeout: float = 30.0) -> Iterator[HLTrade]:
    """Yield HLTrade objects forever. Caller breaks the loop to stop."""
    canon = coin.upper().lstrip("$")
    sub = json.dumps({
        "method": "subscribe",
        "subscription": {"type": "trades", "coin": canon},
    })
    with WebSocket(HL_WS_URL, timeout=timeout) as ws:
        ws.send_text(sub)
        for raw in ws.messages():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("channel") != "trades":
                continue
            for t in msg.get("data") or []:
                px = _to_float(t.get("px"))
                sz = _to_float(t.get("sz"))
                if px is None or sz is None:
                    continue
                yield HLTrade(
                    coin=str(t.get("coin") or canon),
                    px=px,
                    sz=sz,
                    side=str(t.get("side") or ""),
                    ts_ms=int(t.get("time") or 0),
                )


def stream_all_mids(timeout: float = 30.0) -> Iterator[HLMids]:
    """Yield HLMids snapshots forever. allMids is high-frequency — caller
    should rate-limit downstream work."""
    sub = json.dumps({
        "method": "subscribe",
        "subscription": {"type": "allMids"},
    })
    with WebSocket(HL_WS_URL, timeout=timeout) as ws:
        ws.send_text(sub)
        for raw in ws.messages():
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("channel") != "allMids":
                continue
            data = msg.get("data") or {}
            mids_raw = data.get("mids") or {}
            mids: dict = {}
            for k, v in mids_raw.items():
                f = _to_float(v)
                if f is not None:
                    mids[str(k)] = f
            yield HLMids(mids=mids, received_at=time.time())
