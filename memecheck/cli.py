"""Top-level CLI dispatch — `memecheck scan / watch / calc`.

Backward-compat shims preserved so existing invocations keep working:
  * A bare `memecheck <addr>` is treated as implicit `scan`.
  * `memecheck --liq X --lev Y` (no positional, no subcommand) is treated
    as implicit `calc`.

Phase 1a adds the `watch` subcommand (DexScreener REST polling). Phase 1b
will plug a Solana-vault websocket source behind the same flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from memecheck.common.liquidation import liq_report, liq_report_dict
from memecheck.scanner.runner import run_token

_SUBCOMMANDS = {"scan", "watch", "calc"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memecheck",
        description=(
            "On-chain risk screener. `scan` runs a one-shot pre-trade check; "
            "`watch` runs a real-time post-trade monitor."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", metavar="{scan,watch,calc}")

    # ---- scan ------------------------------------------------------------
    scan = sub.add_parser(
        "scan",
        help="one-shot pre-trade screen (the original behavior)",
        description="Aggregate DexScreener + RugCheck/honeypot.is into a verdict.",
    )
    scan.add_argument("address", help="token contract / mint address")
    scan.add_argument(
        "--chain", help="force EVM chain (ethereum/base/bsc/arbitrum/...)"
    )
    scan.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON instead of human-readable text",
    )
    scan.add_argument(
        "--buy-size", type=float, dest="buy_size",
        help="USD size for the exit-liquidity simulator (price impact at this size)",
    )
    scan.add_argument(
        "--max-slippage", type=float, dest="max_slippage", default=5.0,
        help="target price-impact in %% for max-safe-buy estimate (default 5)",
    )
    scan.add_argument(
        "--fee-bps", type=int, dest="fee_bps",
        help="override pool swap fee in basis points (auto-detected otherwise)",
    )
    scan.add_argument(
        "--liq", type=float, help="entry price for liquidation calc (appended to scan)"
    )
    scan.add_argument(
        "--lev", type=float, help="leverage for liquidation calc (appended to scan)"
    )

    # ---- watch -----------------------------------------------------------
    watch = sub.add_parser(
        "watch",
        help="real-time monitor of the pool's liquidity (Phase 1a: console output only)",
        description=(
            "Watch the deepest pool for a token. Phase 1a polls DexScreener every "
            "INTERVAL seconds and prints the windowed liquidity deltas to stdout. "
            "No alerts are sent and no transactions are signed."
        ),
    )
    watch.add_argument("address", help="token contract / mint address")
    watch.add_argument(
        "--chain", help="force EVM chain (ethereum/base/bsc/arbitrum/...)"
    )
    watch.add_argument(
        "--interval", type=float, default=5.0,
        help="poll cadence in seconds (default 5)",
    )
    watch.add_argument(
        "--max-ticks", type=int, default=None, dest="max_ticks",
        help="stop after this many ticks (default: run until Ctrl+C)",
    )
    watch.add_argument(
        "--no-audit", dest="no_audit", action="store_true",
        help="disable the JSONL audit log",
    )
    watch.add_argument(
        "--audit-dir", dest="audit_dir", default=None,
        help="directory for the audit log (default ./audit)",
    )

    # ---- calc ------------------------------------------------------------
    calc = sub.add_parser(
        "calc",
        help="liquidation-price calculator (no token lookup)",
        description="Isolated-margin liquidation-price math, no network calls.",
    )
    calc.add_argument("--liq", type=float, required=True, help="entry price")
    calc.add_argument("--lev", type=float, required=True, help="leverage")
    calc.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    return parser


def _prepend_implicit_subcommand(argv: list[str]) -> list[str]:
    """Backward compat: a bare address is implicit `scan`; bare --liq/--lev is calc."""
    if not argv:
        return argv
    first = argv[0]
    if first in _SUBCOMMANDS or first in {"-h", "--help"}:
        return argv
    if not first.startswith("-"):
        # First arg is the address → implicit scan.
        return ["scan"] + argv
    # No address and no subcommand — check for the legacy --liq/--lev pattern.
    if "--liq" in argv and "--lev" in argv:
        return ["calc"] + argv
    return argv


def _run_scan(args: argparse.Namespace) -> int:
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
    return code


def _run_watch(args: argparse.Namespace) -> int:
    # Lazy import: only pull in the async machinery when actually watching.
    from pathlib import Path
    from memecheck.monitor.runner import run_watch_cli
    audit_dir = Path(args.audit_dir) if args.audit_dir else None
    return run_watch_cli(
        address=args.address.strip(),
        forced_chain=args.chain,
        interval=args.interval,
        max_ticks=args.max_ticks,
        audit_enabled=not args.no_audit,
        audit_dir=audit_dir,
    )


def _run_calc(args: argparse.Namespace) -> int:
    if args.as_json:
        print(json.dumps(liq_report_dict(args.liq, args.lev), indent=2))
    else:
        liq_report(args.liq, args.lev)
    return 0


def _print_menu() -> None:
    # Apply bold only when stdout is a real terminal (skip when piped to file).
    if sys.stdout.isatty():
        b, r = "\033[1m", "\033[0m"
    else:
        b = r = ""
    menu = f"""\
memecheck — on-chain risk screener for memecoins
─────────────────────────────────────────────────

WHAT IT DOES
  Saves you from buying tokens that can't be exited, and warns you
  while you hold one that the pool is bleeding. Three lifecycles:

  {b}scan <ADDRESS>{r}       BEFORE you buy.
       Pulls live data from DexScreener + RugCheck (Solana)
       + honeypot.is (EVM). Prints a red-flag list and a verdict.
       Add --buy-size <USD> to also simulate the price impact of
       your intended buy.

       Example:
         memecheck scan EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm --buy-size 50

  {b}watch <ADDRESS>{r}      AFTER you buy, while you're holding.
       Polls the deepest pool every few seconds, tracks rolling
       liquidity deltas (10s / 60s / 5min), alerts on rugs and
       slow bleeds. Optional push to Telegram / Discord / ntfy
       via env vars. Press Ctrl+C to stop.

       Example:
         memecheck watch 0x6982508145454Ce325dDbE47a25d4ec3d2311933 --chain ethereum

  {b}calc --liq P --lev L{r}  Liquidation-price calculator for
       leveraged positions. No network, no token lookup.

       Example:
         memecheck calc --liq 0.0001 --lev 10

WHAT IT IS NOT
  - Not financial advice.
  - Not a trading bot — it does not sign or send transactions.
  - Not a price predictor — it catches rugs, not bad bets.

MORE
  Full flags per subcommand:
    memecheck scan --help
    memecheck watch --help
    memecheck calc --help

  Project + source:
    https://github.com/Guannings/on-chain-risk-screener
"""
    sys.stdout.write(menu)
    sys.stdout.flush()


def main(argv: Optional[list[str]] = None) -> None:
    if sys.version_info < (3, 9):
        sys.stderr.write("memecheck requires Python 3.9 or newer.\n")
        sys.exit(1)

    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    # Bare invocation (no args) shows the friendly menu, not the dry
    # argparse usage. --help still routes to argparse for power users.
    if not raw_argv:
        _print_menu()
        sys.exit(0)

    raw_argv = _prepend_implicit_subcommand(raw_argv)

    parser = _build_parser()
    args = parser.parse_args(raw_argv)

    if args.cmd is None:
        _print_menu()
        sys.exit(0)

    if args.cmd == "scan":
        sys.exit(_run_scan(args))
    elif args.cmd == "watch":
        sys.exit(_run_watch(args))
    elif args.cmd == "calc":
        sys.exit(_run_calc(args))
    else:
        _print_menu()
        sys.exit(0)


if __name__ == "__main__":
    main()
