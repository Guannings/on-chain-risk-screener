"""Async monitor runner — wires source → state → decision → console.

Phase 1a: console-only output, no alert delivery, no execute path. The
decision engine is a noop that just computes windowed deltas for display.
Phase 2 wires real rules + alert channels + audit log.

Loop semantics:

    1. Pull one event from the source.
    2. Append to state.
    3. Call decide(state) -> Decision.
    4. Print state + decision to stdout.
    5. (Loop until Ctrl+C or max_ticks reached.)

The source's own `stream()` method controls the polling cadence; the runner
is fully reactive.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from memecheck.common.format import fmt_usd
from memecheck.monitor.decision import Decision, decide_noop
from memecheck.monitor.source import (
    DexScreenerPollSource,
    LiquidityEvent,
    LiquiditySource,
)
from memecheck.monitor.state import MonitorState


@dataclass
class RunStats:
    ticks: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    last_event: Optional[LiquidityEvent] = None
    last_decision: Optional[Decision] = None


def _fmt_delta(p: Optional[float]) -> str:
    return "  n/a" if p is None else f"{p:+6.2f}%"


def _print_tick(tick: int, ev: LiquidityEvent, dec: Decision) -> None:
    print(
        f"[tick {tick:>4d}] "
        f"liq {fmt_usd(ev.liquidity_usd):>9s}  "
        f"px ${ev.price_usd:.8g}  "
        f"vs L0 {_fmt_delta(dec.metrics.get('delta_vs_baseline_pct'))}  "
        f"10s {_fmt_delta(dec.metrics.get('delta_10s_pct'))}  "
        f"60s {_fmt_delta(dec.metrics.get('delta_60s_pct'))}  "
        f"5m {_fmt_delta(dec.metrics.get('delta_300s_pct'))}  "
        f"[{dec.action}]"
    )


async def run_monitor(
    source: LiquiditySource,
    *,
    max_ticks: Optional[int] = None,
    print_each_tick: bool = True,
) -> RunStats:
    """Drive the monitor loop until cancelled or max_ticks reached.

    Returns a RunStats with the final tick count and last observed
    state. Useful for tests that want to drive the loop with a fake
    source for a deterministic number of events.
    """
    stats = RunStats()
    state = MonitorState()
    stream = source.stream()
    try:
        async for ev in stream:
            stats.ticks += 1
            stats.last_event = ev
            state.add(ev)
            dec = decide_noop(state)
            stats.last_decision = dec
            if print_each_tick:
                _print_tick(stats.ticks, ev, dec)
            if max_ticks is not None and stats.ticks >= max_ticks:
                break
    except asyncio.CancelledError:
        # Allow graceful Ctrl+C from CLI.
        pass
    return stats


# ----------------------------- CLI entry --------------------------------


def _print_header(source_pool_repr: str) -> None:
    print(f"\n########## memecheck watch — {source_pool_repr} ##########")
    print("(Phase 1a — console only, no alerts wired yet)")
    print(
        "  tick   liq         price        vs L0    10s     60s     5m     action"
    )


def run_watch_cli(
    address: str,
    *,
    forced_chain: Optional[str],
    interval: float,
    max_ticks: Optional[int],
) -> int:
    """Synchronous CLI entrypoint for `memecheck watch <addr>`."""
    try:
        source = DexScreenerPollSource(
            address,
            interval_seconds=interval,
            forced_chain=forced_chain,
            on_error=lambda msg: print(f"  ! {msg}", file=sys.stderr),
        )
    except RuntimeError as e:
        print(f"watch: {e}", file=sys.stderr)
        return 3
    _print_header(
        f"{source.pool.base_symbol or '?'}/{source.pool.quote_symbol or '?'} "
        f"on {source.pool.chain} via {source.pool.dex_id or '?'} "
        f"({source.pool.pair_address})"
    )

    async def _main() -> RunStats:
        # Install a clean Ctrl+C handler that cancels the loop politely.
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _stop() -> None:
            stop_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _stop)
            loop.add_signal_handler(signal.SIGTERM, _stop)
        except (NotImplementedError, AttributeError):
            # Some environments (Windows, some test runners) don't allow this.
            pass

        monitor_task = asyncio.create_task(
            run_monitor(source, max_ticks=max_ticks)
        )
        stop_task = asyncio.create_task(stop_event.wait())
        done, _pending = await asyncio.wait(
            [monitor_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        return monitor_task.result() if monitor_task.done() and not monitor_task.cancelled() else RunStats()

    try:
        stats = asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 0
    print(f"\nstopped after {stats.ticks} tick(s).", file=sys.stderr)
    return 0
