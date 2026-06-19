"""Standalone Hyperliquid stream printer (sub-second).

Run:  memecheck hl-stream BTC
      memecheck hl-stream --mids               # all-mids tick stream
      memecheck hl-stream BTC --max-events 50  # bounded for smoke tests

Shows the WS plumbing actually works end-to-end against the live exchange,
and gives the user a useful tool in its own right (sub-second mark-price
and trade flow). cex-watch integration follows in a later batch.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional


def run_hl_stream_cli(args: argparse.Namespace) -> int:
    if args.mids:
        return _run_mids(max_events=args.max_events)
    if not args.coin:
        print("hl-stream: --mids or a coin symbol is required", file=sys.stderr)
        return 2
    return _run_trades(coin=args.coin, max_events=args.max_events)


def _run_trades(coin: str, max_events: Optional[int]) -> int:
    from memecheck.common.hyperliquid_ws import stream_trades

    print(f"hl-stream: subscribing to {coin.upper()} trades on Hyperliquid…", file=sys.stderr)
    n = 0
    start = time.time()
    try:
        for t in stream_trades(coin):
            side = "BUY " if t.side.upper() == "B" else "SELL"
            print(
                f"  {time.strftime('%H:%M:%S', time.localtime(t.ts_ms / 1000))}"
                f"  {side}  {t.sz:>10.4f} {t.coin:<6} @ ${t.px:,.4f}"
            )
            n += 1
            if max_events is not None and n >= max_events:
                break
    except KeyboardInterrupt:
        pass
    elapsed = time.time() - start
    rate = (n / elapsed) if elapsed > 0 else 0
    print(
        f"\nhl-stream: {n} trades in {elapsed:.1f}s "
        f"({rate:.1f} trades/sec)",
        file=sys.stderr,
    )
    return 0


def _run_mids(max_events: Optional[int]) -> int:
    from memecheck.common.hyperliquid_ws import stream_all_mids

    print("hl-stream: subscribing to allMids on Hyperliquid…", file=sys.stderr)
    n = 0
    start = time.time()
    try:
        for snap in stream_all_mids():
            sample = list(snap.mids.items())[:5]
            head = "  ".join(f"{k}:${v:,.4f}" for k, v in sample)
            print(f"  {time.strftime('%H:%M:%S')}  {head}  …({len(snap.mids)} coins)")
            n += 1
            if max_events is not None and n >= max_events:
                break
    except KeyboardInterrupt:
        pass
    elapsed = time.time() - start
    rate = (n / elapsed) if elapsed > 0 else 0
    print(
        f"\nhl-stream: {n} snapshots in {elapsed:.1f}s "
        f"({rate:.1f} snapshots/sec)",
        file=sys.stderr,
    )
    return 0
