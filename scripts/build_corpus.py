"""Build a labelled corpus of historical rug events. Run from repo root:

    python scripts/build_corpus.py --chain solana --max-events 30 --output corpus/

The script scans new pools on the chosen chain, pulls OHLCV history,
applies the collapse detector, and writes per-event tapes + an
aggregated tape for one-shot sweep.

Then run:

    memecheck backtest corpus/aggregated.csv \\
        --labels corpus/aggregated.labels.csv

    memecheck sweep corpus/aggregated.csv corpus/aggregated.labels.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from memecheck.common.corpus import (
    find_rug_candidates,
    scan_pool_for_rug,
    write_aggregated,
    write_tape,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_corpus")
    parser.add_argument(
        "--chain", default="solana",
        help="DexScreener chain slug (solana, ethereum, base, bsc, ...)",
    )
    parser.add_argument("--max-events", type=int, default=30, dest="max_events")
    parser.add_argument("--max-pages", type=int, default=10, dest="max_pages")
    parser.add_argument(
        "--output", type=Path, default=Path("corpus"),
        help="output directory (default: corpus/)",
    )
    parser.add_argument(
        "--relaxed", action="store_true",
        help="catch deep corrections, not just pure rugs (higher hit rate, "
             "lower purity — use when scanning currently-active pool indexes)",
    )
    parser.add_argument(
        "--seed-addresses", type=Path, default=None, dest="seed_addresses",
        help="path to a text file with one '<chain>,<pool_address>,<name>' per line. "
             "Skips the index-scan step and feeds these directly into the detector. "
             "Best for curated rug lists.",
    )
    args = parser.parse_args(argv)

    if args.seed_addresses is not None:
        cands = _from_seed_file(args.seed_addresses, args.relaxed)
    else:
        print(f"Scanning {args.chain} for rugs (target {args.max_events} events)…\n")
        cands = find_rug_candidates(
            args.chain,
            max_candidates=args.max_events,
            max_pages=args.max_pages,
            on_progress=print,
            relaxed=args.relaxed,
        )

    if not cands:
        print("\nNo rug candidates found. Try a different chain or more pages.", file=sys.stderr)
        return 1

    print(f"\nFound {len(cands)} rug events. Writing per-event tapes…")
    out_dir = args.output
    for cand in cands:
        ev_dir = out_dir / cand.pool_address[:12]
        write_tape(cand, ev_dir)

    print(f"Writing aggregated tape for one-shot sweep…")
    tape_path, labels_path = write_aggregated(cands, out_dir)
    print(f"  → {tape_path}")
    print(f"  → {labels_path}")

    print("\nNext steps:")
    print(f"  memecheck backtest {tape_path} --labels {labels_path}")
    print(f"  memecheck sweep    {tape_path} {labels_path}")
    return 0


def _from_seed_file(seed_path: Path, relaxed: bool):
    """Read 'chain,address,name' triples and run each through the detector."""
    import time
    cands = []
    print(f"Reading seed addresses from {seed_path}…")
    for raw in seed_path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) < 2:
            print(f"  skip: bad line {raw!r}")
            continue
        chain = parts[0]
        addr = parts[1]
        name = parts[2] if len(parts) > 2 else "?"
        print(f"  scanning {name[:30]:<30}  ({addr[:8]}...)")
        time.sleep(2.1)
        cand = scan_pool_for_rug(chain, addr, name, relaxed=relaxed)
        if cand is not None:
            cands.append(cand)
            print(
                f"  ✓ RUG  {name[:30]:<30}  "
                f"peak ${cand.peak_close:.6g} → rug ${cand.rug_close:.6g}"
            )
        else:
            print(f"  miss   {name[:30]:<30}  (no collapse pattern detected)")
    return cands


if __name__ == "__main__":
    sys.exit(main())
