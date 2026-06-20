"""Auto-build a labelled corpus of historical rug events.

Rate limiting
-------------
GT's free tier caps at roughly 30 requests/minute. Each scanned pool
requires one OHLCV call, so without throttling a 50-event corpus
build trips the 429 limit within seconds. `find_rug_candidates`
honours a per-call delay (default 2.1s = ~28 req/min) that keeps us
safely under the cap.


Workflow
--------
1. Scan candidate pools (`fetch_new_pools` + paging, or a user-supplied
   address list).
2. For each pool, pull OHLCV (hourly, up to 1000 hours = ~42 days) from
   GeckoTerminal — free, no API key.
3. Apply the rug detector: a pool counts as "rugged" if its close
   price collapsed from a sustained peak to <5% of peak within 24h,
   and never recovered above 20% of peak afterwards.
4. For each detected rug, build a tape (timestamp, liquidity_usd_proxy,
   price_usd) and a labels file marking the rug hour.
5. Save to disk in the format `memecheck backtest` already consumes.

About the liquidity proxy
-------------------------
GT's free OHLCV doesn't include `reserve_in_usd` per row, only on the
current snapshot. We use `volume * close_price` as a TVL-like proxy:
when volume goes to zero AND price stays low, it crashes — which is the
signal the rules care about (ratios, not absolute values). Document
this honestly in the README.

For higher-fidelity reserveUSD-per-hour tapes, you'd need either:
  - A paid Bitquery / Birdeye plan (~$15/mo)
  - The Graph with an API key (free tier, EVM only)
This module sticks to the zero-cost path. Real on-chain reserveUSD
swaps in cleanly later if needed — the corpus format doesn't change.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from memecheck.common.geckoterminal import (
    fetch_new_pools, fetch_ohlcv, fetch_top_pools, fetch_trending_pools,
)


# Detection thresholds. Default: STRICT (catches obvious rugs, low FP).
# RELAXED mode (--mode crash) catches significant drawdowns even with
# partial recovery — useful when the free data sources only expose
# currently-active pools, so we settle for catching deep crashes.
PEAK_TO_TROUGH_CRASH_RATIO = 0.05   # close < 5% of peak counts as crash
NO_RECOVERY_RATIO = 0.20            # max(post_rug_24h) < 20% of peak
MIN_PEAK_VOLUME_USD = 5_000         # ignore micro-pools that never actually had a market
MIN_HOURS_OF_HISTORY = 24           # need at least 24h of data
MIN_PEAK_TO_NOW_HOURS = 12          # peak must be at least 12h before "now"

# Relaxed thresholds for "deep correction" mode.
RELAXED_CRASH_RATIO = 0.20          # close < 20% of peak
RELAXED_NO_RECOVERY_RATIO = 0.50    # max(post_rug_24h) < 50% of peak
RELAXED_MIN_PEAK_VOLUME_USD = 1_000


@dataclass(frozen=True)
class RugCandidate:
    """One detected rug event ready for tape construction."""
    chain: str
    pool_address: str
    pool_name: str
    peak_ts: int
    peak_close: float
    rug_ts: int                     # first hour where close fell to crash band
    rug_close: float
    ohlcv_rows: list[list[float]]   # full OHLCV around the rug


def detect_rug(
    rows: list[list[float]],
    *,
    crash_ratio: float = PEAK_TO_TROUGH_CRASH_RATIO,
    no_recovery_ratio: float = NO_RECOVERY_RATIO,
    min_peak_volume_usd: float = MIN_PEAK_VOLUME_USD,
) -> Optional[tuple[int, float, int, float]]:
    """Apply the collapse rule to one pool's OHLCV.

    `rows` is GT's OHLCV format: each [ts, open, high, low, close, volume].
    Returns (peak_ts, peak_close, rug_ts, rug_close) or None.

    Order is newest-first per GT — we sort to oldest-first for clarity.
    Optional kwargs allow swapping in `RELAXED_*` thresholds to catch
    deep corrections instead of just full rugs.
    """
    if len(rows) < MIN_HOURS_OF_HISTORY:
        return None
    # Sort oldest-first.
    rows_sorted = sorted(rows, key=lambda r: r[0])

    # Find peak by close (must have meaningful volume at the peak hour).
    peak_idx = -1
    peak_close = 0.0
    for i, r in enumerate(rows_sorted):
        close = float(r[4])
        vol = float(r[5])
        if vol >= min_peak_volume_usd and close > peak_close:
            peak_close = close
            peak_idx = i

    if peak_idx < 0 or peak_close <= 0:
        return None
    # Need post-peak history.
    if len(rows_sorted) - peak_idx < MIN_PEAK_TO_NOW_HOURS:
        return None

    # Find the first post-peak hour where close fell below the crash band.
    crash_threshold = crash_ratio * peak_close
    rug_idx = -1
    rug_close = 0.0
    for j in range(peak_idx + 1, len(rows_sorted)):
        close = float(rows_sorted[j][4])
        if close <= crash_threshold:
            rug_idx = j
            rug_close = close
            break
    if rug_idx < 0:
        return None

    # Verify no recovery: max close in the 24h after the rug stays below
    # no_recovery_ratio * peak.
    no_recovery_threshold = no_recovery_ratio * peak_close
    post_rug_window = rows_sorted[rug_idx : rug_idx + 24]
    if any(float(r[4]) > no_recovery_threshold for r in post_rug_window):
        return None

    return (
        int(rows_sorted[peak_idx][0]),
        peak_close,
        int(rows_sorted[rug_idx][0]),
        rug_close,
    )


def scan_pool_for_rug(
    chain: str, pool_address: str, pool_name: str,
    *, relaxed: bool = False,
) -> Optional[RugCandidate]:
    """One pool → rug candidate or None.

    relaxed=True uses RELAXED_* thresholds (catches deep corrections,
    not just full rugs) — useful when sourcing candidates from active
    indexes where pure rugs have already dropped off the list.
    """
    rows = fetch_ohlcv(chain, pool_address, timeframe="hour", limit=1000)
    if rows is None:
        return None
    if relaxed:
        detected = detect_rug(
            rows,
            crash_ratio=RELAXED_CRASH_RATIO,
            no_recovery_ratio=RELAXED_NO_RECOVERY_RATIO,
            min_peak_volume_usd=RELAXED_MIN_PEAK_VOLUME_USD,
        )
    else:
        detected = detect_rug(rows)
    if detected is None:
        return None
    peak_ts, peak_close, rug_ts, rug_close = detected
    return RugCandidate(
        chain=chain, pool_address=pool_address, pool_name=pool_name,
        peak_ts=peak_ts, peak_close=peak_close,
        rug_ts=rug_ts, rug_close=rug_close,
        ohlcv_rows=rows,
    )


def find_rug_candidates(
    chain: str,
    *,
    max_candidates: int = 50,
    max_pages: int = 10,
    on_progress=None,
    sources: tuple[str, ...] = ("top", "trending", "new"),
    rate_limit_seconds: float = 2.1,
    relaxed: bool = False,
) -> list[RugCandidate]:
    """Scan multiple candidate pool universes on `chain` and return up to
    `max_candidates` detected rugs.

    Sources tried in order:
      - "top": GT's /pools endpoint, top by 24h volume (older pools with
        history — best hit rate for past rugs)
      - "trending": pools with elevated recent activity (pump-and-dump
        candidates often appear here)
      - "new": just-created pools (mostly won't have enough history but
        included for completeness)
    """
    candidates: list[RugCandidate] = []
    pool_seen: set[str] = set()
    from typing import Callable
    fetchers: dict[str, Callable[..., list[dict]]] = {
        "top": fetch_top_pools,
        "trending": fetch_trending_pools,
        "new": fetch_new_pools,
    }
    for source_name in sources:
        if len(candidates) >= max_candidates:
            break
        fetcher = fetchers.get(source_name)
        if fetcher is None:
            continue
        if on_progress is not None:
            on_progress(f"\n--- source: {source_name} ---")
        for page in range(1, max_pages + 1):
            if len(candidates) >= max_candidates:
                break
            pools = fetcher(chain, page=page)
            if not pools:
                break
            for p in pools:
                if len(candidates) >= max_candidates:
                    break
                attrs = p.get("attributes") or {}
                addr = attrs.get("address")
                name = attrs.get("name", "?")
                if not addr or addr in pool_seen:
                    continue
                pool_seen.add(addr)
                if on_progress is not None:
                    on_progress(f"  scanning {name[:30]:<30}  ({addr[:8]}...)")
                time.sleep(rate_limit_seconds)
                cand = scan_pool_for_rug(chain, addr, name, relaxed=relaxed)
                if cand is not None:
                    candidates.append(cand)
                    if on_progress is not None:
                        on_progress(
                            f"  ✓ RUG  {name[:30]:<30}  "
                            f"peak ${cand.peak_close:.6f} → rug ${cand.rug_close:.6f}"
                        )
    return candidates


def write_tape(candidate: RugCandidate, out_dir: Path) -> tuple[Path, Path]:
    """Convert one RugCandidate to tape + labels CSVs.

    Tape format matches `memecheck.common.backtest._load_tape` exactly:
        timestamp,liquidity_usd,price_usd

    Liquidity proxy = volume * close (per-hour). Crashes when volume
    vanishes, which is what the rules need.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = out_dir / "tape.csv"
    labels_path = out_dir / "labels.csv"

    rows = sorted(candidate.ohlcv_rows, key=lambda r: r[0])
    with tape_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "liquidity_usd", "price_usd"])
        for row in rows:
            ts, _o, _h, _l, close, vol = row
            liq_proxy = max(0.01, float(vol) * float(close))
            w.writerow([int(ts), f"{liq_proxy:.4f}", f"{float(close):.10f}"])

    with labels_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "event"])
        w.writerow([candidate.rug_ts, "rug"])

    return tape_path, labels_path


def write_aggregated(
    candidates: list[RugCandidate], out_dir: Path
) -> tuple[Path, Path]:
    """Concatenate every candidate's tape into one mega-tape + labels.

    Useful for running a single sweep across the whole corpus instead
    of one per event. Timestamps are offset so each event lives in its
    own non-overlapping window (10-day spacing).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = out_dir / "aggregated.csv"
    labels_path = out_dir / "aggregated.labels.csv"

    WINDOW_SECONDS = 10 * 86400
    with tape_path.open("w", newline="") as tape_f, \
         labels_path.open("w", newline="") as lab_f:
        tw = csv.writer(tape_f)
        lw = csv.writer(lab_f)
        tw.writerow(["timestamp", "liquidity_usd", "price_usd"])
        lw.writerow(["timestamp", "event"])
        for i, cand in enumerate(candidates):
            base = i * WINDOW_SECONDS
            rows = sorted(cand.ohlcv_rows, key=lambda r: r[0])
            t0 = int(rows[0][0])
            for row in rows:
                ts, _o, _h, _l, close, vol = row
                shifted = base + (int(ts) - t0)
                liq_proxy = max(0.01, float(vol) * float(close))
                tw.writerow([shifted, f"{liq_proxy:.4f}", f"{float(close):.10f}"])
            lw.writerow([base + (cand.rug_ts - t0), "rug"])

    return tape_path, labels_path
