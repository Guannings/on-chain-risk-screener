"""Async runner for `cex-watch` — polls a CEX perp, prints a tick row, and
alerts on funding extremes, basis blowouts, and OI drops.

Uses the same MonitorState / Decider / alert machinery as the DEX watcher
where possible; the decision rules and event shape are CEX-specific.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from memecheck.common.cex_health import (
    BASIS_BLOWOUT_PCT,
    ELEVATED_FUNDING_PER_8H_PCT,
    EXTREME_FUNDING_PER_8H_PCT,
)
from memecheck.monitor.action.alert import AlertDispatcher, DispatchResult
from memecheck.monitor.audit import AuditLogger
from memecheck.monitor.cex_source import CexPerpEvent, CexPerpPollSource
from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    ACTION_NONE,
    Decision,
)
from memecheck.monitor.source import LiquidityEvent


# How much an OI drop counts as a flag.
OI_DROP_FLAG_PCT: float = 20.0    # >= 20% drop vs baseline triggers ALERT


@dataclass
class CexRunStats:
    ticks: int = 0
    alerts_fired: int = 0
    executes_fired: int = 0
    started_at: float = field(default_factory=time.time)
    last_event: Optional[CexPerpEvent] = None
    last_decision: Optional[Decision] = None


@dataclass
class CexState:
    """Rolling state specific to a CEX perp — different signals than DEX."""

    history: Deque[CexPerpEvent] = field(default_factory=lambda: deque(maxlen=512))
    baseline: Optional[CexPerpEvent] = None

    def add(self, ev: CexPerpEvent) -> None:
        if self.baseline is None:
            self.baseline = ev
        self.history.append(ev)

    @property
    def current(self) -> Optional[CexPerpEvent]:
        return self.history[-1] if self.history else None

    def oi_delta_pct(self) -> Optional[float]:
        cur = self.current
        if cur is None or self.baseline is None:
            return None
        base = self.baseline.open_interest_usd
        if base is None or base <= 0 or cur.open_interest_usd is None:
            return None
        return (cur.open_interest_usd / base - 1.0) * 100.0


def _decide_cex(state: CexState, side: Optional[str] = None) -> Decision:
    """Side-aware decision over CEX perp state.

    Rules (simple for v1; debounce can come later):
      - Funding extreme (|funding_8h| >= 0.05%): ALERT
      - Funding direction unfavorable vs side at elevated level: ALERT
      - Basis blowout (|basis_pct| >= 0.5%): ALERT
      - OI dropped >= 20% vs baseline (positioning unwind): ALERT
    """
    metrics: dict[str, Any] = {}
    cur = state.current
    if cur is None:
        return Decision(action=ACTION_NONE, reason=None, metrics=metrics)

    metrics["mark"] = cur.mark
    metrics["funding_per_8h_pct"] = cur.funding_per_8h_pct
    metrics["basis_pct"] = cur.basis_pct
    metrics["open_interest_usd"] = cur.open_interest_usd
    oi_delta = state.oi_delta_pct()
    metrics["oi_delta_pct"] = oi_delta

    reasons: list[str] = []

    f = cur.funding_per_8h_pct
    if f is not None and abs(f) >= EXTREME_FUNDING_PER_8H_PCT:
        reasons.append(
            f"funding {f:+.3f}%/8h ({f*3*365:+.0f}% APY) is extreme"
        )

    if f is not None and side and abs(f) >= ELEVATED_FUNDING_PER_8H_PCT:
        if side == "long" and f > 0:
            reasons.append(
                f"long pays {f*3:+.3f}%/day (elevated funding headwind)"
            )
        elif side == "short" and f < 0:
            reasons.append(
                f"short pays {-f*3:+.3f}%/day (elevated funding headwind)"
            )

    b = cur.basis_pct
    if b is not None and abs(b) >= BASIS_BLOWOUT_PCT:
        kind = "premium" if b > 0 else "discount"
        reasons.append(f"basis {b:+.2f}% {kind} (mean-reversion risk)")

    if oi_delta is not None and oi_delta <= -OI_DROP_FLAG_PCT:
        reasons.append(
            f"open interest dropped {oi_delta:.0f}% vs baseline (positioning unwind)"
        )

    if reasons:
        return Decision(
            action=ACTION_ALERT,
            reason="; ".join(reasons),
            metrics=metrics,
        )
    return Decision(action=ACTION_NONE, reason=None, metrics=metrics)


def _fmt_pct(p: Optional[float]) -> str:
    return "   ·   " if p is None else f"{p:+6.3f}%"


def _fmt_usd_short(x: Optional[float]) -> str:
    if x is None:
        return "    ·   "
    if abs(x) >= 1e9:
        return f"${x/1e9:6.2f}B"
    if abs(x) >= 1e6:
        return f"${x/1e6:6.2f}M"
    if abs(x) >= 1e3:
        return f"${x/1e3:6.2f}K"
    return f"${x:7.2f}"


def _print_cex_header(symbol: str, side: Optional[str], channels: list[str], audit_path: Optional[Path]) -> None:
    side_label = f" ({side.upper()})" if side else ""
    print(f"\n########## memecheck cex-watch — {symbol} perp{side_label} ##########")
    print(f"alert channels: {', '.join(channels) if channels else '(none)'}")
    if audit_path is not None:
        print(f"audit log: {audit_path}")
    print()
    print(
        f" {'#':>4s} │ {'mark':>11s} │ {'fund/8h':>8s} │ {'basis':>8s} │"
        f" {'vol 24h':>10s} │ {'OI':>10s} │ {'ΔOI':>8s} │ action"
    )
    print(
        "─────┼─────────────┼──────────┼──────────┼────────────┼────────────┼──────────┼────────"
    )


def _print_cex_tick(tick: int, ev: CexPerpEvent, dec: Decision) -> None:
    if dec.action == ACTION_EXECUTE:
        tag = "\033[1;31m[EXECUTE]\033[0m"
    elif dec.action == ACTION_ALERT:
        tag = "\033[1;33m[ALERT]\033[0m"
    else:
        tag = ""
    print(
        f" {tick:>4d} │ "
        f"${ev.mark:>10,.4f} │ "
        f"{_fmt_pct(ev.funding_per_8h_pct)} │ "
        f"{_fmt_pct(ev.basis_pct)} │ "
        f"{_fmt_usd_short(ev.vol_24h_usd)} │ "
        f"{_fmt_usd_short(ev.open_interest_usd)} │ "
        f"{_fmt_pct(dec.metrics.get('oi_delta_pct'))} │ "
        f"{tag}"
    )


def _ce_to_le(ev: CexPerpEvent) -> LiquidityEvent:
    """Adapt CexPerpEvent → LiquidityEvent so the existing alert dispatcher
    (which expects LiquidityEvent) can format the message."""
    return LiquidityEvent(
        ts=ev.ts,
        base_reserve=0.0,
        quote_reserve=0.0,
        quote_price_usd=1.0,
        liquidity_usd=ev.open_interest_usd or 0.0,
        price_usd=ev.mark,
        source=ev.source,
    )


async def run_cex_monitor(
    source: CexPerpPollSource,
    *,
    side: Optional[str] = None,
    alerts: Optional[AlertDispatcher] = None,
    audit: Optional[AuditLogger] = None,
    header: str = "",
    max_ticks: Optional[int] = None,
    print_each_tick: bool = True,
) -> CexRunStats:
    stats = CexRunStats()
    state = CexState()
    try:
        async for ev in source.stream():
            stats.ticks += 1
            stats.last_event = ev
            state.add(ev)
            dec = _decide_cex(state, side=side)
            stats.last_decision = dec

            if audit is not None:
                audit.write("tick", {
                    "tick": stats.ticks,
                    "event": {
                        "ts": ev.ts,
                        "symbol": ev.symbol,
                        "mark": ev.mark,
                        "funding_per_8h_pct": ev.funding_per_8h_pct,
                        "basis_pct": ev.basis_pct,
                        "vol_24h_usd": ev.vol_24h_usd,
                        "open_interest_usd": ev.open_interest_usd,
                    },
                    "decision": {
                        "action": dec.action,
                        "reason": dec.reason,
                        "metrics": dec.metrics,
                    },
                })

            if print_each_tick:
                _print_cex_tick(stats.ticks, ev, dec)

            if dec.action in (ACTION_ALERT, ACTION_EXECUTE) and alerts is not None:
                results: list[DispatchResult] = await alerts.dispatch(
                    dec, _ce_to_le(ev), header
                )
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


def run_cex_watch_cli(
    symbol: str,
    *,
    side: Optional[str],
    interval: float,
    max_ticks: Optional[int],
    audit_enabled: bool = True,
    audit_dir: Optional[Path] = None,
) -> int:
    try:
        source = CexPerpPollSource(
            symbol,
            interval_seconds=interval,
            on_error=lambda msg: print(f"  ! {msg}", file=sys.stderr),
        )
    except RuntimeError as e:
        print(f"cex-watch: {e}", file=sys.stderr)
        return 3

    alerts = AlertDispatcher.from_env(include_console=True)
    audit = AuditLogger.for_run(
        chain="cex",
        address=source.symbol,
        audit_dir=audit_dir,
        enabled=audit_enabled,
    )
    _print_cex_header(source.symbol, side, alerts.channel_names, audit.path)

    if audit.enabled:
        audit.write("start", {
            "symbol": source.symbol,
            "side": side,
            "interval_seconds": interval,
            "alert_channels": alerts.channel_names,
        })

    async def _main() -> CexRunStats:
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
            run_cex_monitor(
                source,
                side=side,
                alerts=alerts,
                audit=audit,
                header=f"{source.symbol} perp",
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
        return monitor_task.result() if monitor_task.done() and not monitor_task.cancelled() else CexRunStats()

    try:
        stats = asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        if audit.enabled:
            audit.write("stop", {})
            audit.close()
    print(
        f"\nstopped after {stats.ticks} tick(s), "
        f"{stats.alerts_fired} alert(s), {stats.executes_fired} execute(s).",
        file=sys.stderr,
    )
    return 0
