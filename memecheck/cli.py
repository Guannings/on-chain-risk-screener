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
from typing import Any, Optional

from memecheck.common.liquidation import liq_report, liq_report_dict
from memecheck.scanner.runner import run_token

_SUBCOMMANDS = {
    "scan", "watch", "calc", "plan", "prep",
    "cex-check", "cex-prep", "cex-watch",
    "hl-stream",
    "journal", "backtest", "sweep",
}


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
    scan.add_argument(
        "--check-deployer", dest="check_deployer", action="store_true",
        help="walk the mint's first-tx fee payer + score their prior "
             "deployments by current depth (Solana only; adds 5-30s of "
             "Solana RPC + DEX-API calls)",
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
    watch.add_argument(
        "--latency-log", dest="latency_log", default=None,
        help="path to JSONL file for per-tick latency samples "
             "(fetch_s / decide_s / dispatch_s / total_s); "
             "p50/p99 summary always printed at exit",
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

    # ---- plan ------------------------------------------------------------
    plan = sub.add_parser(
        "plan",
        help="position-sizing / R-multiple trade planner (no network)",
        description=(
            "Compute position notional, margin, liquidation distance, "
            "TP prices for R-multiple targets, expected fees and funding "
            "for a planned trade. Pure math; no token lookup."
        ),
    )
    plan.add_argument(
        "--account", type=float, required=True,
        help="account / wallet size in USD",
    )
    plan.add_argument(
        "--risk", type=float, default=1.0,
        help="risk per trade as %% of account (default 1.0)",
    )
    plan.add_argument(
        "--entry", type=float, required=True, help="entry price",
    )
    plan.add_argument(
        "--stop", type=float, required=True, help="stop-loss price",
    )
    plan.add_argument(
        "--leverage", type=float, default=None,
        help="leverage (default: minimum needed to fit the position)",
    )
    plan.add_argument(
        "--tp", type=float, action="append", dest="tp",
        help="R-multiple TP target (repeatable; default 1, 2, 3)",
    )
    plan.add_argument(
        "--maint-margin", type=float, default=None, dest="maint_margin",
        help="maintenance margin ratio (overrides venue tier; defaults to "
             "venue/symbol tier lookup, or 0.5%% if no venue is given)",
    )
    plan.add_argument(
        "--fee-bps", type=int, default=10, dest="fee_bps",
        help="round-trip fee in basis points (default 10 = 0.10%%)",
    )
    plan.add_argument(
        "--funding", type=float, default=None,
        help="explicit funding rate per 8h cycle in %%; overrides --symbol "
             "auto-fetch (default 0.01 if neither --funding nor --symbol given)",
    )
    plan.add_argument(
        "--symbol", type=str, default=None,
        help="auto-fetch the current funding rate for this token from "
             "Kraken Futures (e.g. XRP, BTC, ETH, SOL). Overridden by "
             "--funding if both are passed.",
    )
    plan.add_argument(
        "--hold-hours", type=float, default=24.0, dest="hold_hours",
        help="expected hold time in hours for funding cost (default 24)",
    )
    plan.add_argument(
        "--venue", type=str, default=None,
        help="CEX venue for tiered-MMR lookup "
             "(kraken-futures, bybit, deribit). Affects liquidation "
             "distance for large positions.",
    )
    plan.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- prep ------------------------------------------------------------
    prep = sub.add_parser(
        "prep",
        help="composed pre-entry workflow: scan + plan, gated by verdict",
        description=(
            "Run scan and plan together as one pre-entry checklist. The "
            "plan's computed position notional is fed into scan's exit-sim "
            "automatically. If scan returns HONEYPOT or HARD PASS, prep "
            "REFUSES to print the plan — that's the point. Pass --force "
            "to override. Explicitly NOT a trading bot: nothing here "
            "signs, sends, or executes anything."
        ),
    )
    prep.add_argument("address", help="token contract / mint address")
    prep.add_argument("--chain", help="force EVM chain for scan")
    prep.add_argument(
        "--account", type=float, required=True,
        help="account / wallet size in USD",
    )
    prep.add_argument(
        "--risk", type=float, default=1.0,
        help="risk per trade as %% of account (default 1.0)",
    )
    prep.add_argument(
        "--entry", type=float, required=True, help="entry price",
    )
    prep.add_argument(
        "--stop", type=float, required=True, help="stop-loss price",
    )
    prep.add_argument(
        "--leverage", type=float, default=None,
        help="leverage (default: minimum needed to fit the position)",
    )
    prep.add_argument(
        "--symbol", type=str, default=None,
        help="auto-fetch funding rate for this CEX perp ticker "
             "(e.g. XRP). Separate from the on-chain address.",
    )
    prep.add_argument(
        "--funding", type=float, default=None,
        help="explicit funding rate per 8h cycle in %% (overrides --symbol)",
    )
    prep.add_argument(
        "--hold-hours", type=float, default=24.0, dest="hold_hours",
        help="expected hold time in hours (default 24)",
    )
    prep.add_argument(
        "--force", action="store_true",
        help="print the plan even if scan returns HARD PASS or HONEYPOT "
             "(strongly discouraged)",
    )
    prep.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- cex-check -------------------------------------------------------
    cex_check = sub.add_parser(
        "cex-check",
        help="pre-trade health screen for a CEX perpetual (Kraken Futures)",
        description=(
            "Pulls live ticker for the given CEX perp from Kraken Futures and "
            "checks funding, basis, volume, spread, and recent volatility. "
            "Same (flags, notes, verdict) shape as `scan` but for centralised "
            "perps instead of on-chain DEX pools."
        ),
    )
    cex_check.add_argument(
        "symbol", help="ticker for the CEX perp (e.g. XRP, BTC, ETH, SOL)",
    )
    cex_check.add_argument(
        "--side", choices=["long", "short"], default=None,
        help="trade side for funding-direction analysis (optional)",
    )
    cex_check.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- cex-prep --------------------------------------------------------
    cex_prep = sub.add_parser(
        "cex-prep",
        help="composed CEX pre-entry workflow: cex-check + plan, gated by verdict",
        description=(
            "CEX-side equivalent of `prep`. Runs cex-check on the symbol, "
            "then plan, and refuses to print the plan if cex-check verdict "
            "is HARD PASS. Funding rate is auto-fetched from the same "
            "ticker call. Explicitly NOT a trading bot."
        ),
    )
    cex_prep.add_argument("symbol", help="ticker for the CEX perp (e.g. XRP)")
    cex_prep.add_argument(
        "--account", type=float, required=True, help="account size in USD",
    )
    cex_prep.add_argument(
        "--risk", type=float, default=1.0,
        help="risk per trade as %% of account (default 1.0)",
    )
    cex_prep.add_argument(
        "--entry", type=float, required=True, help="entry price",
    )
    cex_prep.add_argument(
        "--stop", type=float, required=True, help="stop-loss price",
    )
    cex_prep.add_argument(
        "--leverage", type=float, default=None,
        help="leverage (default: minimum needed)",
    )
    cex_prep.add_argument(
        "--hold-hours", type=float, default=24.0, dest="hold_hours",
        help="expected hold time in hours (default 24)",
    )
    cex_prep.add_argument(
        "--force", action="store_true",
        help="print the plan even if cex-check returns HARD PASS",
    )
    cex_prep.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- cex-watch -------------------------------------------------------
    cex_watch = sub.add_parser(
        "cex-watch",
        help="real-time monitor for a CEX perp (funding, basis, OI)",
        description=(
            "Polls Kraken Futures every INTERVAL seconds and alerts on "
            "funding spikes, basis blowouts, and sudden OI drops. Same "
            "alert dispatcher as `watch` — Telegram / Discord / ntfy "
            "env-gated."
        ),
    )
    cex_watch.add_argument("symbol", help="CEX perp ticker (e.g. XRP, BTC)")
    cex_watch.add_argument(
        "--side", choices=["long", "short"], default=None,
        help="trade side for funding-direction analysis (optional)",
    )
    cex_watch.add_argument(
        "--interval", type=float, default=30.0,
        help="poll cadence in seconds (default 30)",
    )
    cex_watch.add_argument(
        "--max-ticks", type=int, default=None, dest="max_ticks",
        help="stop after this many ticks (default: run until Ctrl+C)",
    )
    cex_watch.add_argument(
        "--no-audit", dest="no_audit", action="store_true",
        help="disable the JSONL audit log",
    )
    cex_watch.add_argument(
        "--audit-dir", dest="audit_dir", default=None,
        help="directory for the audit log (default ./audit)",
    )

    # ---- hl-stream ------------------------------------------------------
    hl_stream = sub.add_parser(
        "hl-stream",
        help="sub-second Hyperliquid trade / mid stream (WebSocket)",
        description=(
            "Subscribes to Hyperliquid's public WebSocket and prints trades "
            "or all-mids ticks in real time. Uses the stdlib-only WS client "
            "shipped in memecheck.common.ws_client — no extra dependencies."
        ),
    )
    hl_stream.add_argument(
        "coin", nargs="?", default=None,
        help="coin symbol (e.g. BTC, SOL, HYPE); omit when using --mids",
    )
    hl_stream.add_argument(
        "--mids", action="store_true",
        help="subscribe to allMids instead of one coin's trades",
    )
    hl_stream.add_argument(
        "--max-events", type=int, default=None, dest="max_events",
        help="stop after this many messages (default: run until Ctrl+C)",
    )

    # ---- journal --------------------------------------------------------
    journal = sub.add_parser(
        "journal",
        help="view past prep / cex-prep runs from the trade journal",
        description=(
            "List entries from the SQLite trade journal (default "
            "~/.memecheck/journal.sqlite). Every `prep` and `cex-prep` "
            "run auto-logs verdict, planned notional, and timestamps."
        ),
    )
    journal.add_argument(
        "--last", type=int, default=20, dest="last_n",
        help="show the most recent N entries (default 20)",
    )
    journal.add_argument(
        "--symbol", default=None,
        help="filter to a specific symbol or address",
    )
    journal.add_argument(
        "--venue", choices=["dex", "cex"], default=None,
        help="filter to DEX or CEX entries",
    )
    journal.add_argument(
        "--path", default=None,
        help="override journal database path",
    )
    journal.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- backtest -------------------------------------------------------
    backtest = sub.add_parser(
        "backtest",
        help="replay a tape against the decision engine",
        description=(
            "Take a CSV tape of (timestamp, liquidity_usd, price_usd), "
            "feed it into the Decider, and report what actions the rules "
            "would have fired. Optionally compare against labels for "
            "precision / recall on rug detection."
        ),
    )
    backtest.add_argument("tape", help="path to CSV tape file")
    backtest.add_argument(
        "--labels", default=None,
        help="optional path to ground-truth labels CSV "
             "(format: timestamp,event where event ∈ rug|migration|none)",
    )
    backtest.add_argument(
        "--critical-ratio", type=float, default=None, dest="critical_ratio",
        help="override critical_liq_ratio threshold",
    )
    backtest.add_argument(
        "--json", dest="as_json", action="store_true",
        help="emit structured JSON",
    )

    # ---- sweep ----------------------------------------------------------
    sweep_p = sub.add_parser(
        "sweep",
        help="grid-search Decider thresholds against a labelled corpus",
        description=(
            "Replay the same tape with different threshold values, compute "
            "precision/recall/F1 for each combination, identify the Pareto "
            "frontier. Closes the 'thresholds hand-tuned with no data behind "
            "them' critique once you have a labelled corpus."
        ),
    )
    sweep_p.add_argument("tape", help="path to CSV tape file")
    sweep_p.add_argument("labels", help="path to labels CSV (required for sweep)")
    sweep_p.add_argument(
        "--critical-ratios", type=str, default="0.3,0.4,0.5,0.6,0.7",
        dest="critical_ratios",
        help="comma-sep grid for critical_liq_ratio (default 0.3..0.7)",
    )
    sweep_p.add_argument(
        "--large-event-pcts", type=str, default="-25,-20,-15",
        dest="large_event_pcts",
        help="comma-sep grid for large_event_pct (default -25,-20,-15)",
    )
    sweep_p.add_argument(
        "--slow-bleed-60s-pcts", type=str, default="-15,-10,-7",
        dest="slow_bleed_60s_pcts",
        help="comma-sep grid for slow_bleed_60s_pct (default -15,-10,-7)",
    )
    sweep_p.add_argument(
        "--tolerance", type=float, default=60.0,
        help="match-window tolerance in seconds (default 60)",
    )
    sweep_p.add_argument(
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
    if args.check_deployer:
        # Solana-only by design. EVM has a different "deployer" concept
        # (contract creator), out of scope here.
        chain = (result.get("chain") or "").lower()
        if chain != "solana":
            if not args.as_json:
                print("\n  ↻ --check-deployer requested but token isn't on Solana — skipped.")
        else:
            if not args.as_json:
                print("\n  ↻ Scoring deployer history (this takes 5-30s of RPC calls)…")
            from memecheck.common.deployer import score_deployer
            try:
                report = score_deployer(args.address.strip(), self_mint=args.address.strip())
            except Exception as e:    # noqa: BLE001
                report = None
                if not args.as_json:
                    print(f"  ! deployer scoring failed: {e}")
            if report is not None:
                result["deployer_history"] = {
                    "deployer": report.deployer,
                    "prior_mint_count": len(report.prior_mints),
                    "sampled_count": report.sampled_count,
                    "dead_count": report.dead_count,
                    "flag": report.flag,
                    "note": report.note,
                }
                if not args.as_json:
                    if report.flag:
                        print(f"  [!] DEPLOYER HISTORY: {report.flag}")
                    if report.note:
                        print(f"      {report.note}")
    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
    return code


def _run_watch(args: argparse.Namespace) -> int:
    # Lazy import: only pull in the async machinery when actually watching.
    from pathlib import Path
    from memecheck.monitor.runner import run_watch_cli
    audit_dir = Path(args.audit_dir) if args.audit_dir else None
    latency_log = Path(args.latency_log) if args.latency_log else None
    return run_watch_cli(
        address=args.address.strip(),
        forced_chain=args.chain,
        interval=args.interval,
        max_ticks=args.max_ticks,
        audit_enabled=not args.no_audit,
        audit_dir=audit_dir,
        latency_log=latency_log,
    )


def _run_calc(args: argparse.Namespace) -> int:
    if args.as_json:
        print(json.dumps(liq_report_dict(args.liq, args.lev), indent=2))
    else:
        liq_report(args.liq, args.lev)
    return 0


def _resolve_funding(
    explicit_funding: Optional[float],
    symbol: Optional[str],
) -> tuple[float, Optional[float], Optional[str], dict[str, Any]]:
    """Resolve funding rate from --funding / --symbol with explicit precedence.

    Returns (pct_per_8h, pct_per_8h_predicted_or_None, human_note, extra_dict_for_json).
    """
    from memecheck.common.position import DEFAULT_FUNDING_PCT_8H

    if explicit_funding is not None:
        note = None
        if symbol:
            note = (
                f"using --funding {explicit_funding:+.4f}% (override; "
                f"--symbol {symbol.upper()} ignored)"
            )
        return explicit_funding, None, note, {}

    if symbol:
        from memecheck.common.funding import fetch_funding_rate
        result = fetch_funding_rate(symbol)
        if result is not None:
            note = (
                f"auto-fetched {result.symbol} funding from {result.source}: "
                f"{result.rate_per_8h_pct:+.4f}% per 8h "
                f"(mark ${result.mark_price})"
            )
            if result.rate_per_8h_pct_next is not None:
                note += (
                    f"; next-cycle predicted "
                    f"{result.rate_per_8h_pct_next:+.4f}%"
                )
            return (
                result.rate_per_8h_pct,
                result.rate_per_8h_pct_next,
                note,
                {
                    "symbol": result.symbol,
                    "source": result.source,
                    "raw_rate": result.raw_rate,
                    "raw_unit": result.raw_unit,
                    "mark_price": result.mark_price,
                    "perp_symbol": result.perp_symbol,
                    "rate_per_8h_pct_next": result.rate_per_8h_pct_next,
                },
            )
        return (
            DEFAULT_FUNDING_PCT_8H,
            None,
            (
                f"could not fetch funding for {symbol.upper()} "
                f"(symbol not listed on Kraken Futures, or fetch failed); "
                f"using default {DEFAULT_FUNDING_PCT_8H:+.4f}% / 8h"
            ),
            {},
        )

    return DEFAULT_FUNDING_PCT_8H, None, None, {}


def _run_plan(args: argparse.Namespace) -> int:
    from memecheck.common.position import compute_plan, format_plan, plan_to_dict

    funding_pct, funding_pct_next, funding_note, funding_extra = _resolve_funding(
        args.funding, args.symbol
    )

    plan = compute_plan(
        account_usd=args.account,
        risk_pct=args.risk,
        entry_price=args.entry,
        stop_price=args.stop,
        leverage=args.leverage,
        tp_r_multiples=args.tp or None,
        maint_margin=args.maint_margin,
        fee_bps=args.fee_bps,
        funding_pct_8h=funding_pct,
        funding_pct_8h_next=funding_pct_next,
        hold_hours=args.hold_hours,
        venue=args.venue,
        symbol=args.symbol,
    )

    if args.as_json:
        payload = plan_to_dict(plan)
        if funding_note or funding_extra:
            payload["funding_resolution"] = {"note": funding_note, **funding_extra}
        print(json.dumps(payload, indent=2, default=str))
    else:
        if funding_note:
            print(f"  ↻ {funding_note}")
        print(format_plan(plan))

    if plan.safety_level == "danger":
        return 2
    if plan.safety_level == "warn":
        return 1
    return 0


def _run_prep(args: argparse.Namespace) -> int:
    """Composed pre-entry workflow: scan + plan with verdict-based gating.

    Sequence:
      1. Compute the trade plan (pure math, no I/O).
      2. Run scan on the address, with --buy-size set to plan's notional
         so the exit-sim runs at the actual size you'd trade.
      3. If scan's verdict is HONEYPOT or HARD PASS, REFUSE to print the
         plan (unless --force). That's the whole point of prep.
      4. If RISKY, warn loudly but print the plan — user opted in.
      5. If clean, green-light and print the plan.

    Explicitly NOT a trading bot. Nothing in this function signs, sends,
    or executes any transaction.
    """
    from memecheck.common.position import (
        compute_plan,
        format_plan,
        plan_to_dict,
    )
    from memecheck.scanner.runner import run_token

    # ----- Step 1: compute plan (pure math) ------------------------------
    funding_pct, funding_pct_next, funding_note, funding_extra = _resolve_funding(
        args.funding, args.symbol
    )
    plan = compute_plan(
        account_usd=args.account,
        risk_pct=args.risk,
        entry_price=args.entry,
        stop_price=args.stop,
        leverage=args.leverage,
        funding_pct_8h=funding_pct,
        funding_pct_8h_next=funding_pct_next,
        hold_hours=args.hold_hours,
        symbol=args.symbol,
    )

    # ----- Step 2: run scan with buy_size = plan's notional --------------
    if not args.as_json:
        print("\n========== STEP 1/2 — Scanning token ==========")
    scan_result, scan_exit = run_token(
        args.address.strip(),
        forced_chain=args.chain,
        as_json=args.as_json,
        buy_size_usd=plan.position_notional_usd,
        max_slippage_pct=5.0,
        fee_bps_override=None,
    )
    verdict = scan_result.get("verdict") or ""

    # ----- Step 3: gate decision -----------------------------------------
    is_honeypot = verdict.startswith("HONEYPOT")
    is_hard_pass = verdict.startswith("HARD PASS")
    is_risky = verdict.startswith("RISKY")
    refuse = (is_honeypot or is_hard_pass) and not args.force

    if args.as_json:
        # JSON mode: include both halves + the gating decision.
        out = {
            "scan": scan_result,
            "verdict": verdict,
            "gate": {
                "refused": refuse,
                "reason": (
                    "HONEYPOT" if is_honeypot else
                    "HARD PASS" if is_hard_pass else
                    "RISKY (printed with warning)" if is_risky else
                    "clean"
                ),
                "force": args.force,
            },
        }
        if not refuse:
            plan_dict: dict[str, Any] = plan_to_dict(plan)
            if funding_note or funding_extra:
                plan_dict["funding_resolution"] = {
                    "note": funding_note, **funding_extra
                }
            out["plan"] = plan_dict
        print(json.dumps(out, indent=2, default=str))
        return _combined_exit_code(scan_exit, plan, refuse)

    # Human-readable: print the gating banner, then plan if not refused.
    print()
    bar = "=" * 60
    if refuse:
        if is_honeypot:
            tag, msg = "⛔ REFUSING TO PRINT PLAN", "scan detected a HONEYPOT"
        else:
            tag, msg = "⛔ REFUSING TO PRINT PLAN", "scan returned HARD PASS"
        print(bar)
        print(f"{tag}")
        print(f"   Reason: {msg}.")
        print(f"   This is exactly the kind of trade prep was built to stop.")
        print(f"   Pass --force to override (please don't).")
        print(bar)
        return _combined_exit_code(scan_exit, plan, refuse=True)

    if is_risky:
        print(bar)
        print("⚠  Scan returned RISKY.")
        print("   Plan follows so you can size with eyes open if you still")
        print("   want to enter. Proceed only with money already written off.")
        print(bar)
    elif is_honeypot or is_hard_pass:
        # Refusal bypassed via --force.
        print(bar)
        print(f"⛔ Scan verdict: {verdict}")
        print("   You passed --force. Plan follows under protest.")
        print(bar)
    else:
        print(bar)
        print("✓ Scan clean — no automatic red flags.")
        print("   Plan follows. The decision is still yours.")
        print(bar)

    print("\n========== STEP 2/2 — Trade plan ==========")
    if funding_note:
        print(f"  ↻ {funding_note}")
    print(format_plan(plan))
    _log_journal_safely(
        venue="dex",
        symbol_or_addr=args.address.strip(),
        side=plan.side,
        account_usd=args.account,
        entry_price=args.entry,
        stop_price=args.stop,
        leverage=plan.leverage_used,
        position_notional_usd=plan.position_notional_usd,
        risk_usd=plan.dollar_risk_usd,
        verdict=verdict,
        refused=False,
        forced=bool(is_honeypot or is_hard_pass),
        funding_per_8h_pct=funding_pct,
        notes=None,
    )
    return _combined_exit_code(scan_exit, plan, refuse=False)


def _log_journal_safely(**kwargs: Any) -> None:
    """Best-effort journal write — never crash a `prep` run because the
    journal write failed."""
    try:
        from memecheck.common.journal import log_entry
        log_entry(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def _run_cex_watch(args: argparse.Namespace) -> int:
    from pathlib import Path
    from memecheck.monitor.cex_runner import run_cex_watch_cli
    audit_dir = Path(args.audit_dir) if args.audit_dir else None
    return run_cex_watch_cli(
        symbol=args.symbol,
        side=args.side,
        interval=args.interval,
        max_ticks=args.max_ticks,
        audit_enabled=not args.no_audit,
        audit_dir=audit_dir,
    )


def _run_journal(args: argparse.Namespace) -> int:
    from pathlib import Path
    from memecheck.common.journal import list_entries
    p = Path(args.path) if args.path else None
    entries = list_entries(
        limit=args.last_n,
        symbol_or_addr=args.symbol,
        venue=args.venue,
        path=p,
    )
    if args.as_json:
        print(json.dumps(
            [{
                "id": e.id, "iso_ts": e.iso_ts, "venue": e.venue,
                "symbol_or_addr": e.symbol_or_addr, "side": e.side,
                "account_usd": e.account_usd, "entry_price": e.entry_price,
                "stop_price": e.stop_price, "leverage": e.leverage,
                "position_notional_usd": e.position_notional_usd,
                "risk_usd": e.risk_usd, "verdict": e.verdict,
                "refused": e.refused, "forced": e.forced,
                "funding_per_8h_pct": e.funding_per_8h_pct, "notes": e.notes,
            } for e in entries],
            indent=2, default=str,
        ))
        return 0
    if not entries:
        print("(no journal entries yet — run `prep` or `cex-prep` to create some)")
        return 0
    print(f"\n########## memecheck journal — last {len(entries)} entry/entries ##########")
    print()
    print(
        f" {'id':>4s} │ {'when (UTC)':>19s} │ {'venue':>5s} │ "
        f"{'symbol':>12s} │ {'side':>5s} │ {'notional':>10s} │ "
        f"{'verdict':>12s} │ refused"
    )
    print("─" * 110)
    for e in entries:
        short_when = e.iso_ts[:19]
        short_sym = (e.symbol_or_addr[:10] + "…") if len(e.symbol_or_addr) > 12 else e.symbol_or_addr
        verdict_short = (e.verdict or "")[:12]
        print(
            f" {e.id:>4d} │ {short_when:>19s} │ {e.venue:>5s} │ "
            f"{short_sym:>12s} │ {(e.side or '?'):>5s} │ "
            f"${e.position_notional_usd:>9,.2f} │ "
            f"{verdict_short:>12s} │ {'yes' if e.refused else 'no'}"
        )
    return 0


def _run_backtest(args: argparse.Namespace) -> int:
    from memecheck.common.backtest import run_backtest_cli
    return run_backtest_cli(
        tape_path=args.tape,
        labels_path=args.labels,
        critical_ratio_override=args.critical_ratio,
        as_json=args.as_json,
    )


def _run_sweep(args: argparse.Namespace) -> int:
    """Grid search over Decider thresholds."""
    from pathlib import Path
    from memecheck.common.threshold_sweep import (
        format_sweep_table, pareto_frontier, sweep,
    )

    def _parse_grid(s: str) -> list[float]:
        out: list[float] = []
        for chunk in s.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.append(float(chunk))
            except ValueError:
                print(f"sweep: bad grid value {chunk!r}", file=sys.stderr)
                sys.exit(3)
        return out

    crits = _parse_grid(args.critical_ratios)
    larges = _parse_grid(args.large_event_pcts)
    bleeds = _parse_grid(args.slow_bleed_60s_pcts)
    if not crits or not larges or not bleeds:
        print("sweep: all three grids must be non-empty", file=sys.stderr)
        return 3

    points = sweep(
        Path(args.tape), Path(args.labels),
        critical_ratios=crits,
        large_event_pcts=larges,
        slow_bleed_60s_pcts=bleeds,
        tolerance_seconds=args.tolerance,
    )
    frontier = pareto_frontier(points)

    if args.as_json:
        print(json.dumps({
            "tape": args.tape,
            "labels": args.labels,
            "grid": {
                "critical_ratios": crits,
                "large_event_pcts": larges,
                "slow_bleed_60s_pcts": bleeds,
            },
            "tolerance_seconds": args.tolerance,
            "points": [p.__dict__ for p in points],
            "pareto_frontier": [p.__dict__ for p in frontier],
        }, indent=2, default=str))
    else:
        print(format_sweep_table(points, frontier))
    return 0


def _combined_exit_code(scan_exit: int, plan: Any, refuse: bool) -> int:
    """Worst of scan exit and plan safety level wins.

    Refusal latches at exit 2 regardless of what the plan thinks.
    """
    if refuse:
        return 2
    plan_exit = 0
    if plan.safety_level == "danger":
        plan_exit = 2
    elif plan.safety_level == "warn":
        plan_exit = 1
    return max(scan_exit, plan_exit)


def _run_cex_check(args: argparse.Namespace) -> int:
    from memecheck.common.cex_health import (
        analyze_cex_perp,
        exit_code_for_cex,
        fetch_cex_ticker,
        make_cex_verdict,
    )

    ticker = fetch_cex_ticker(args.symbol)
    if ticker is None:
        msg = (
            f"could not find {args.symbol.upper()} perp on Kraken Futures "
            f"(symbol not listed or fetch failed)"
        )
        if args.as_json:
            print(json.dumps({"error": msg, "symbol": args.symbol.upper()}, indent=2))
        else:
            print(f"cex-check: {msg}", file=sys.stderr)
        return 3

    flags, notes, metrics = analyze_cex_perp(ticker, side=args.side)
    verdict = make_cex_verdict(flags)

    if args.as_json:
        print(json.dumps(
            {
                "symbol": args.symbol.upper(),
                "side": args.side,
                "metrics": metrics,
                "flags": flags,
                "verdict": verdict,
            },
            indent=2,
            default=str,
        ))
        return exit_code_for_cex(verdict)

    print(f"\n########## cex-check: {args.symbol.upper()} perp ##########")
    if args.side:
        print(f"  Side: {args.side.upper()}")
    print("\n--- Market ---")
    for n in notes:
        print(f"  {n}")
    print("\n================ RED FLAGS ================")
    if flags:
        for f in flags:
            print(f"  [!] {f}")
    else:
        print("  (none)")
    print(f"\nVerdict: {verdict}")
    print("Not financial advice. Crowded positioning and funding extremes can persist for weeks.")
    return exit_code_for_cex(verdict)


def _run_cex_prep(args: argparse.Namespace) -> int:
    """CEX-side composed workflow: cex-check + plan, gated like prep."""
    from memecheck.common.cex_health import (
        analyze_cex_perp,
        exit_code_for_cex,
        fetch_cex_ticker,
        make_cex_verdict,
    )
    from memecheck.common.position import (
        DEFAULT_FUNDING_PCT_8H,
        compute_plan,
        format_plan,
        plan_to_dict,
    )

    # Side inferred from entry/stop.
    side = "long" if args.stop < args.entry else "short"

    # Single ticker call serves both the screen and the funding rate.
    ticker = fetch_cex_ticker(args.symbol)
    if ticker is None:
        msg = (
            f"cex-prep: could not find {args.symbol.upper()} perp on "
            f"Kraken Futures"
        )
        print(msg, file=sys.stderr)
        return 3

    # ----- Step 1: CEX health screen ------------------------------------
    flags, notes, metrics = analyze_cex_perp(ticker, side=side)
    verdict = make_cex_verdict(flags)

    # Funding from the same ticker, normalised exactly as the funding module does.
    funding_pct = metrics.get("funding_per_8h_pct")
    funding_pct_next = metrics.get("funding_per_8h_pct_predicted")
    if funding_pct is None:
        funding_pct = DEFAULT_FUNDING_PCT_8H

    # ----- Step 2: compute plan ----------------------------------------
    plan = compute_plan(
        account_usd=args.account,
        risk_pct=args.risk,
        entry_price=args.entry,
        stop_price=args.stop,
        leverage=args.leverage,
        funding_pct_8h=funding_pct,
        funding_pct_8h_next=funding_pct_next,
        hold_hours=args.hold_hours,
        venue="kraken-futures",
        symbol=args.symbol,
    )

    is_hard_pass = verdict.startswith("HARD PASS")
    is_risky = verdict.startswith("RISKY")
    refuse = is_hard_pass and not args.force

    if args.as_json:
        out = {
            "cex_check": {
                "symbol": args.symbol.upper(),
                "side": side,
                "metrics": metrics,
                "flags": flags,
                "verdict": verdict,
            },
            "gate": {
                "refused": refuse,
                "reason": (
                    "HARD PASS" if is_hard_pass else
                    "RISKY (printed with warning)" if is_risky else
                    "clean"
                ),
                "force": args.force,
            },
        }
        if not refuse:
            out["plan"] = plan_to_dict(plan)
            out["plan"]["funding_resolution"] = {
                "note": (
                    f"auto-fetched from Kraken Futures ticker for "
                    f"{args.symbol.upper()}: {funding_pct:+.4f}% per 8h"
                ),
            }
        print(json.dumps(out, indent=2, default=str))
        return _combined_exit_code(exit_code_for_cex(verdict), plan, refuse)

    # Human-readable mode.
    print(f"\n========== STEP 1/2 — CEX health check ==========")
    print(f"\n########## cex-check: {args.symbol.upper()} perp ##########")
    print(f"  Side: {side.upper()} (inferred from entry/stop)")
    print("\n--- Market ---")
    for n in notes:
        print(f"  {n}")
    print("\n================ RED FLAGS ================")
    if flags:
        for f in flags:
            print(f"  [!] {f}")
    else:
        print("  (none)")
    print(f"\nVerdict: {verdict}")

    bar = "=" * 60
    print()
    if refuse:
        print(bar)
        print("⛔ REFUSING TO PRINT PLAN")
        print(f"   Reason: cex-check returned HARD PASS.")
        print(f"   This is exactly the kind of trade prep was built to stop.")
        print(f"   Pass --force to override (please don't).")
        print(bar)
        return 2

    if is_risky:
        print(bar)
        print("⚠  cex-check returned RISKY.")
        print("   Plan follows so you can size with eyes open if you still")
        print("   want to enter. Proceed only with money already written off.")
        print(bar)
    elif is_hard_pass:
        print(bar)
        print("⛔ cex-check verdict: HARD PASS — but you passed --force.")
        print("   Plan follows under protest.")
        print(bar)
    else:
        print(bar)
        print("✓ cex-check clean — no automatic red flags.")
        print("   Plan follows. The decision is still yours.")
        print(bar)

    print(f"\n========== STEP 2/2 — Trade plan ==========")
    print(
        f"  ↻ auto-fetched {args.symbol.upper()} funding from kraken-futures: "
        f"{funding_pct:+.4f}% per 8h"
    )
    print(format_plan(plan))
    _log_journal_safely(
        venue="cex",
        symbol_or_addr=args.symbol.upper(),
        side=side,
        account_usd=args.account,
        entry_price=args.entry,
        stop_price=args.stop,
        leverage=plan.leverage_used,
        position_notional_usd=plan.position_notional_usd,
        risk_usd=plan.dollar_risk_usd,
        verdict=verdict,
        refused=False,
        forced=bool(is_hard_pass),
        funding_per_8h_pct=funding_pct,
        notes=None,
    )
    return _combined_exit_code(exit_code_for_cex(verdict), plan, refuse=False)


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

  {b}plan --account A --entry E --stop S{r}
       Position-sizing / R-multiple planner. Given your account,
       entry, and stop, computes notional, margin, liquidation
       distance with a safety check, and TP prices at 1R / 2R / 3R
       net of fees + funding. Use BEFORE every leveraged trade.

       Example:
         memecheck plan --account 1000 --entry 0.0001 --stop 0.000094 --risk 1

  {b}prep <ADDRESS> --account A --entry E --stop S{r}
       Composed pre-entry workflow for ON-CHAIN tokens (DEX).
       Runs scan, then plan, REFUSES to print the plan if scan
       returns HONEYPOT or HARD PASS. Plan's notional is auto-fed
       into scan's exit-sim so price impact is checked at your
       real size. Not a trading bot.

       Example:
         memecheck prep 0x6982... --account 1000 --entry 0.0001 --stop 0.000094

  {b}cex-check <SYMBOL>{r}     CEX PERP pre-trade screen.
       Pulls live ticker from Kraken Futures and checks funding,
       basis, volume, spread, 24h move. Same shape as scan, but
       for centralised perps (XRP, BTC, ETH, SOL, ...). Pass
       --side long|short for funding-direction analysis.

       Example:
         memecheck cex-check XRP --side short

  {b}cex-prep <SYMBOL> --account A --entry E --stop S{r}
       Composed CEX pre-entry workflow. cex-check + plan, gated
       on HARD PASS the same way prep is. Funding rate auto-
       fetched from the same Kraken Futures ticker. Not a bot.

       Example:
         memecheck cex-prep XRP --account 1000 --entry 1.16 --stop 1.20

  {b}cex-watch <SYMBOL>{r}     CEX perp REAL-TIME monitor.
       Polls Kraken Futures, alerts on funding extremes, basis
       blowouts, OI drops. Same alert dispatcher as `watch`.

       Example:
         memecheck cex-watch XRP --side short --interval 30

  {b}journal{r}                Your trade-prep history.
       Every prep / cex-prep auto-logs to ~/.memecheck/journal.sqlite.
       `memecheck journal` shows the last 20 entries.

  {b}backtest <tape.csv>{r}    Replay a tape against the decision engine.
       Reports actions taken and (with --labels) precision/recall
       on rug detection. Samples in samples/ to try immediately.

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
    elif args.cmd == "plan":
        sys.exit(_run_plan(args))
    elif args.cmd == "prep":
        sys.exit(_run_prep(args))
    elif args.cmd == "cex-check":
        sys.exit(_run_cex_check(args))
    elif args.cmd == "cex-prep":
        sys.exit(_run_cex_prep(args))
    elif args.cmd == "cex-watch":
        sys.exit(_run_cex_watch(args))
    elif args.cmd == "hl-stream":
        from memecheck.monitor.hl_stream import run_hl_stream_cli
        sys.exit(run_hl_stream_cli(args))
    elif args.cmd == "journal":
        sys.exit(_run_journal(args))
    elif args.cmd == "backtest":
        sys.exit(_run_backtest(args))
    elif args.cmd == "sweep":
        sys.exit(_run_sweep(args))
    else:
        _print_menu()
        sys.exit(0)


if __name__ == "__main__":
    main()
