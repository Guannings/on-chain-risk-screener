"""Per-tick latency instrumentation for the watch monitors.

Both `watch` (DEX) and `cex-watch` (CEX) now sample the time taken at
three points on each tick:

    fetch:   t_fetch_end   - t_fetch_start
    decide:  t_decide_end  - t_fetch_end
    dispatch: t_dispatch_end - t_decide_end       (None if no alert fired)

The recorder buffers samples in-memory (capped) and computes percentiles
on demand. On Ctrl+C the runner prints a small SLO summary so the user
knows the answer to "what's my median + p99 time from event-to-alert?".

A separate `--latency-log <path>` writes per-tick JSON to disk for
offline analysis.

Numbers here are wall-clock seconds. We use `time.monotonic()` to be
robust to clock changes during a long run.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Optional


@dataclass
class LatencySample:
    """One tick's measured timings, in seconds."""
    tick: int
    fetch_s: float
    decide_s: float
    dispatch_s: Optional[float]
    total_s: float
    ts: float = field(default_factory=time.time)

    def to_jsonl_dict(self) -> dict:
        return {
            "ts": self.ts,
            "tick": self.tick,
            "fetch_s": round(self.fetch_s, 6),
            "decide_s": round(self.decide_s, 6),
            "dispatch_s": (round(self.dispatch_s, 6) if self.dispatch_s is not None else None),
            "total_s": round(self.total_s, 6),
        }


# A modest cap so even a 24h run with 5-second ticks (~17k samples)
# stays in memory without ballooning. Older samples roll off.
_MAX_BUFFER = 50_000


class LatencyRecorder:
    """Append samples; ask for percentiles when you want them."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._samples: list[LatencySample] = []
        self._log_path = log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate / create on start.
            log_path.write_text("")

    def add(self, sample: LatencySample) -> None:
        self._samples.append(sample)
        if len(self._samples) > _MAX_BUFFER:
            # Drop oldest 10% in one swoop — cheaper than per-add slicing.
            self._samples = self._samples[_MAX_BUFFER // 10 :]
        if self._log_path is not None:
            try:
                with self._log_path.open("a") as f:
                    f.write(json.dumps(sample.to_jsonl_dict()) + "\n")
            except OSError:
                pass

    @property
    def count(self) -> int:
        return len(self._samples)

    def percentile(self, attr: str, q: float) -> Optional[float]:
        """`q` in [0, 1]. Returns None when no samples carry this attribute."""
        values = [
            v for v in (getattr(s, attr) for s in self._samples)
            if v is not None
        ]
        if not values:
            return None
        values = sorted(values)
        # Nearest-rank percentile — simple and matches operational intuition.
        k = max(0, min(len(values) - 1, int(math.ceil(q * len(values))) - 1))
        return values[k]

    def summary(self) -> dict:
        """Compact stats dict suitable for printing or JSON emission."""
        if not self._samples:
            return {"samples": 0}
        return {
            "samples": len(self._samples),
            "fetch_s":    {"p50": median(s.fetch_s for s in self._samples),
                           "p99": self.percentile("fetch_s", 0.99)},
            "decide_s":   {"p50": median(s.decide_s for s in self._samples),
                           "p99": self.percentile("decide_s", 0.99)},
            "total_s":    {"p50": median(s.total_s for s in self._samples),
                           "p99": self.percentile("total_s", 0.99)},
            "dispatch_s": {"p50": self.percentile("dispatch_s", 0.50),
                           "p99": self.percentile("dispatch_s", 0.99)},
        }

    def format_summary(self) -> str:
        s = self.summary()
        if s.get("samples") == 0:
            return "latency: no samples recorded"
        def fmt(stat: dict) -> str:
            p50 = stat.get("p50")
            p99 = stat.get("p99")
            if p50 is None:
                return "  no data"
            return f"  p50 {p50*1000:7.1f} ms   p99 {p99*1000:7.1f} ms" if p99 is not None else f"  p50 {p50*1000:7.1f} ms"
        return (
            f"=== latency SLO over {s['samples']} ticks ===\n"
            f"  fetch    {fmt(s['fetch_s'])}\n"
            f"  decide   {fmt(s['decide_s'])}\n"
            f"  dispatch {fmt(s['dispatch_s'])}\n"
            f"  total    {fmt(s['total_s'])}"
        )
