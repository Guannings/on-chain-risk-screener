"""Top-level CLI dispatch.

For now: a single `main()` that matches the legacy memecheck.py interface so
existing invocations and tests keep working. The future monitor's `watch`
subcommand will be added here when Phase 1 ships.
"""

from __future__ import annotations

import argparse
import json
import sys

from memecheck.common.liquidation import liq_report, liq_report_dict
from memecheck.scanner.runner import run_token


def main() -> None:
    if sys.version_info < (3, 9):
        sys.stderr.write("memecheck requires Python 3.9 or newer.\n")
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="On-chain risk screener for a single token",
        prog="memecheck",
    )
    ap.add_argument("address", nargs="?", help="token contract / mint address")
    ap.add_argument(
        "--chain", help="force EVM chain (ethereum/base/bsc/arbitrum/...)"
    )
    ap.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON instead of human-readable text",
    )
    ap.add_argument(
        "--buy-size", type=float, dest="buy_size",
        help="USD size for the exit-liquidity simulator (round-trip slippage at this size)",
    )
    ap.add_argument(
        "--max-slippage", type=float, dest="max_slippage", default=5.0,
        help="target price-impact in %% for max-safe-buy estimate (default 5)",
    )
    ap.add_argument(
        "--fee-bps", type=int, dest="fee_bps",
        help="override pool swap fee in basis points (auto-detected from dexId otherwise)",
    )
    ap.add_argument(
        "--liq", type=float, help="entry price for liquidation calc"
    )
    ap.add_argument(
        "--lev", type=float, help="leverage for liquidation calc"
    )
    args = ap.parse_args()

    # liquidation-only mode (no address)
    if args.liq and args.lev and not args.address:
        if args.as_json:
            print(json.dumps(liq_report_dict(args.liq, args.lev), indent=2))
        else:
            liq_report(args.liq, args.lev)
        return

    if not args.address:
        ap.print_help()
        sys.exit(1)

    result, code = run_token(
        args.address.strip(),
        forced_chain=args.chain,
        as_json=args.as_json,
        buy_size_usd=args.buy_size,
        max_slippage_pct=args.max_slippage,
        fee_bps_override=args.fee_bps,
    )

    if args.liq and args.lev:
        result["liquidation"] = liq_report_dict(args.liq, args.lev)
        if not args.as_json:
            liq_report(args.liq, args.lev)

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))

    sys.exit(code)


if __name__ == "__main__":
    main()
