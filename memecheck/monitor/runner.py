"""Async monitor runner — wires source → state → decision → audit → alert.

Phase 2: the decision engine, audit log, and env-gated alert channels are
all live. The runner is still source-agnostic — swap `DexScreenerPollSource`
for `RaydiumVaultSource` (Phase 1b) and the rest is unchanged.

Loop semantics per tick:
  1. Pull one event from the source.
  2. Append to state.
  3. Call decider.decide(state) -> Decision.
  4. Audit-log the tick + decision.
  5. If decision is ALERT or EXECUTE, fan out to alert channels.
  6. Print one-line summary to stdout; non-NONE decisions also stand out.

The runner does NOT sign transactions. Phase 3 wires that behind a separate
opt-in gate.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from memecheck.common.format import fmt_usd
from memecheck.monitor.action.alert import AlertDispatcher, DispatchResult
from memecheck.monitor.audit import AuditLogger
from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    ACTION_NONE,
    DEFAULT_CONFIG,
    Decider,
    Decision,
    DecisionConfig,
    decide_noop,
)
from memecheck.monitor.source import (
    DexScreenerPollSource,
    LiquidityEvent,
    LiquiditySource,
)
from memecheck.monitor.state import MonitorState


@dataclass
class RunStats:
    ticks: int = 0
    alerts_fired: int = 0
    executes_fired: int = 0
    started_at: float = field(default_factory=time.time)
    last_event: Optional[LiquidityEvent] = None
    last_decision: Optional[Decision] = None


def _fmt_delta(p: Optional[float]) -> str:
    """7-char wide: '+12.34%', '-12.34%', or a centered '·' for not-covered."""
    if p is None:
        return "   ·   "
    return f"{p:+6.2f}%"


def _fmt_price(x: float) -> str:
    """Adaptive price format — bare string, no padding."""
    if x == 0:
        return "$0"
    if x >= 1000:
        return f"${x:,.2f}"            # $67,234.12
    if x >= 1:
        return f"${x:.4f}"             # $791.5500
    if x >= 0.01:
        return f"${x:.6f}"             # $0.014523
    return f"${x:.4g}"                 # $1.234e-05


def _print_tick(tick: int, ev: LiquidityEvent, dec: Decision) -> None:
    """One clean data row. Action label is suppressed for NONE — see _print_alert."""
    print(
        f" {tick:>4d} │"
        f" {fmt_usd(ev.liquidity_usd):>8s} │"
        f" {_fmt_price(ev.price_usd):>10s} │"
        f" {_fmt_delta(dec.metrics.get('delta_vs_baseline_pct'))} │"
        f" {_fmt_delta(dec.metrics.get('delta_10s_pct'))} │"
        f" {_fmt_delta(dec.metrics.get('delta_60s_pct'))} │"
        f" {_fmt_delta(dec.metrics.get('delta_300s_pct'))}"
    )
    if dec.action == ACTION_ALERT:
        sys.stdout.write(
            f"      \033[1;33m⚠ ALERT\033[0m  {dec.reason or ''}\n"
        )
    elif dec.action == ACTION_EXECUTE:
        sys.stdout.write(
            f"      \033[1;31m⛔ EXECUTE\033[0m  {dec.reason or ''}\n"
        )


async def run_monitor(
    source: LiquiditySource,
    *,
    decider: Optional[Decider] = None,
    alerts: Optional[AlertDispatcher] = None,
    audit: Optional[AuditLogger] = None,
    header: str = "",
    max_ticks: Optional[int] = None,
    print_each_tick: bool = True,
) -> RunStats:
    """Drive the monitor loop until cancelled or max_ticks reached.

    Decider, alerts, and audit are all optional. When None:
      - decider → noop (always NONE)
      - alerts → no-op dispatch
      - audit → no-op log

    Returns RunStats with final counts and last state.
    """
    stats = RunStats()
    state = MonitorState()
    _decider = decider  # may be None — fall back to decide_noop below.
    stream = source.stream()

    try:
        async for ev in stream:
            stats.ticks += 1
            stats.last_event = ev
            state.add(ev)
            dec = _decider.decide(state) if _decider is not None else decide_noop(state)
            stats.last_decision = dec

            if audit is not None:
                audit.write("tick", {
                    "tick": stats.ticks,
                    "event": {
                        "ts": ev.ts,
                        "liquidity_usd": ev.liquidity_usd,
                        "price_usd": ev.price_usd,
                        "base_reserve": ev.base_reserve,
                        "quote_reserve": ev.quote_reserve,
                        "quote_price_usd": ev.quote_price_usd,
                        "source": ev.source,
                    },
                    "decision": {
                        "action": dec.action,
                        "reason": dec.reason,
                        "metrics": dec.metrics,
                    },
                })

            if print_each_tick:
                _print_tick(stats.ticks, ev, dec)

            if dec.action in (ACTION_ALERT, ACTION_EXECUTE) and alerts is not None:
                results: list[DispatchResult] = await alerts.dispatch(dec, ev, header)
                if dec.action == ACTION_ALERT:
                    stats.alerts_fired += 1
                else:
                    stats.executes_fired += 1
                if audit is not None:
                    audit.write("dispatch", {
                        "tick": stats.ticks,
                        "action": dec.action,
                        "results": [
                            {"channel": r.channel, "ok": r.ok, "detail": r.detail}
                            for r in results
                        ],
                    })

            if max_ticks is not None and stats.ticks >= max_ticks:
                break
    except asyncio.CancelledError:
        pass
    return stats


# ----------------------------- CLI entry --------------------------------


def _print_header(pool_repr: str, channel_names: list[str], audit_path: Optional[Path]) -> None:
    print(f"\n########## memecheck watch — {pool_repr} ##########")
    print(f"alert channels: {', '.join(channel_names) if channel_names else '(none)'}")
    if audit_path is not None:
        print(f"audit log: {audit_path}")
    print()  # blank line before the table
    # Widths match _print_tick exactly:  4 / 8 / 10 / 7 / 7 / 7 / 7
    print(
        f" {'#':>4s} │"
        f" {'liq':>8s} │"
        f" {'price':>10s} │"
        f" {'vs L0':>7s} │"
        f" {'Δ 10s':>7s} │"
        f" {'Δ 60s':>7s} │"
        f" {'Δ 5m':>7s}"
    )
    # Header rule.  ─ between columns, ┼ at the junctions.
    print(
        "─────┼──────────┼────────────┼─────────┼─────────┼─────────┼─────────"
    )


def run_watch_cli(
    address: str,
    *,
    forced_chain: Optional[str],
    interval: float,
    max_ticks: Optional[int],
    audit_enabled: bool = True,
    audit_dir: Optional[Path] = None,
    config: DecisionConfig = DEFAULT_CONFIG,
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

    decider = Decider(config)
    alerts = AlertDispatcher.from_env(include_console=True)
    audit = AuditLogger.for_run(
        chain=source.pool.chain,
        address=address,
        audit_dir=audit_dir,
        enabled=audit_enabled,
    )

    pool_repr = (
        f"{source.pool.base_symbol or '?'}/{source.pool.quote_symbol or '?'} "
        f"on {source.pool.chain} via {source.pool.dex_id or '?'} "
        f"({source.pool.pair_address})"
    )
    _print_header(pool_repr, alerts.channel_names, audit.path)

    if audit.enabled:
        audit.write("start", {
            "address": address,
            "chain": source.pool.chain,
            "pair_address": source.pool.pair_address,
            "dex_id": source.pool.dex_id,
            "base_symbol": source.pool.base_symbol,
            "quote_symbol": source.pool.quote_symbol,
            "interval_seconds": interval,
            "config": {
                "critical_liq_ratio": config.critical_liq_ratio,
                "large_event_pct": config.large_event_pct,
                "large_event_debounce": config.large_event_debounce,
                "slow_bleed_60s_pct": config.slow_bleed_60s_pct,
                "slow_bleed_300s_pct": config.slow_bleed_300s_pct,
                "slow_bleed_debounce": config.slow_bleed_debounce,
            },
            "alert_channels": alerts.channel_names,
        })

    async def _main() -> RunStats:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _stop() -> None:
            stop_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _stop)
            loop.add_signal_handler(signal.SIGTERM, _stop)
        except (NotImplementedError, AttributeError):
            pass

        monitor_task = asyncio.create_task(
            run_monitor(
                source,
                decider=decider,
                alerts=alerts,
                audit=audit,
                header=pool_repr,
                max_ticks=max_ticks,
            )
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
    finally:
        if audit.enabled:
            audit.write("stop", {
                "ticks": (stats.ticks if "stats" in dir() else 0),
            })
            audit.close()
    print(
        f"\nstopped after {stats.ticks} tick(s), "
        f"{stats.alerts_fired} alert(s), {stats.executes_fired} execute(s).",
        file=sys.stderr,
    )
    return 0
