"""Rolling state — the ring buffer of LiquidityEvents and derived metrics.

Pure logic, no I/O. The runner pushes events in; the decision engine reads
windowed deltas out. State is intentionally simple: a bounded deque of
events keyed by timestamp.

Math (from the design doc):

    L_t                 = liquidity right now (most recent event)
    L_0                 = baseline (first event we observed when watch started)
    ΔL_W = (L_t - L_{t-W}) / L_{t-W}    over a window of W seconds

The window-resolved event L_{t-W} is the OLDEST event whose ts is >= now - W;
this is more robust than linear interpolation between samples and matches
the "did the pool lose at least X% in the last W seconds" question the
decision engine actually asks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from memecheck.monitor.source import LiquidityEvent


# Cover ~40 minutes at 5s polling — way more than the 5-minute slow-bleed window.
_DEFAULT_BUFFER_SIZE: int = 512


@dataclass
class MonitorState:
    """Rolling state for a single monitored pool."""

    buffer_size: int = _DEFAULT_BUFFER_SIZE
    _events: Deque[LiquidityEvent] = field(init=False, repr=False)
    _baseline: Optional[LiquidityEvent] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._events = deque(maxlen=self.buffer_size)

    # ------------------------- mutation ----------------------------------

    def add(self, event: LiquidityEvent) -> None:
        if self._baseline is None:
            self._baseline = event
        self._events.append(event)

    # ------------------------- accessors ---------------------------------

    @property
    def count(self) -> int:
        return len(self._events)

    @property
    def current(self) -> Optional[LiquidityEvent]:
        return self._events[-1] if self._events else None

    @property
    def baseline(self) -> Optional[LiquidityEvent]:
        """First event observed since the monitor started. Used as L_0."""
        return self._baseline

    # ------------------------- derived metrics ---------------------------

    def liquidity_vs_baseline_pct(self) -> Optional[float]:
        """(L_t / L_0 - 1) * 100. None until we have at least one event."""
        if self._baseline is None or self.current is None:
            return None
        base = self._baseline.liquidity_usd
        if base <= 0:
            return None
        return (self.current.liquidity_usd / base - 1.0) * 100.0

    def windowed_delta_pct(self, window_seconds: float) -> Optional[float]:
        """ΔL_W as a percentage. None if buffer doesn't span the window.

        Picks the OLDEST event whose ts is >= (now - W) — i.e. the earliest
        sample still inside the lookback window. Reflects the strict
        question 'in the last W seconds, how much did liquidity move?'.
        """
        if self.current is None:
            return None
        now_ts = self.current.ts
        cutoff = now_ts - window_seconds
        # Buffer is small (≤512); linear scan is fine and explicit.
        oldest_in_window: Optional[LiquidityEvent] = None
        for ev in self._events:
            if ev.ts >= cutoff:
                oldest_in_window = ev
                break
        if oldest_in_window is None or oldest_in_window is self.current:
            return None
        base = oldest_in_window.liquidity_usd
        if base <= 0:
            return None
        return (self.current.liquidity_usd / base - 1.0) * 100.0

    def window_covered(self, window_seconds: float) -> bool:
        """True iff the buffer holds at least `window_seconds` of history."""
        if self.current is None or self._events[0] is self.current:
            return False
        span = self.current.ts - self._events[0].ts
        return span >= window_seconds
