# Changelog

All notable changes to memecheck. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.5.0 — 2026-06-18

### Added

- **`cex-check` subcommand** — pre-trade health screen for CEX perpetuals.
  Pulls a live ticker from Kraken Futures and checks funding magnitude,
  funding direction vs trade side, mark-vs-index basis, 24h volume, bid-ask
  spread, and 24h price move. Same `(flags, notes, verdict)` shape as `scan`.
- **`cex-prep` subcommand** — composed CEX pre-entry workflow that runs
  `cex-check` and the position planner together. Funding rate is auto-fetched
  from the same ticker call (one round trip). Refuses to print the plan if
  the screen returns `HARD PASS` (`--force` to override).
- **`prep` subcommand** — composed DEX pre-entry workflow. Runs `scan` then
  the planner; plan's computed notional is fed into scan's exit-sim so the
  price-impact check runs at the actual size you'd trade. Refuses to print
  the plan on `HONEYPOT` or `HARD PASS`.
- **Hyperliquid funding source** in `fetch_funding_rate` — closes the
  DEX-perp gap. Symbol auto-detect prefers Kraken Futures and falls back
  to Hyperliquid for symbols only listed there.
- **`cex-watch` subcommand** — real-time CEX perp monitor. Polls
  Kraken Futures every N seconds; alerts on funding spikes, basis
  blowouts, and sudden OI drops via the same dispatcher as `watch`.
- **Trade journal** — every `prep` / `cex-prep` run auto-logs verdict, planned
  notional, and outcome to `~/.memecheck/journal.sqlite`. New `journal`
  subcommand to view history (`memecheck journal --last 20`).
- **Backtest harness** — `memecheck backtest <tape.csv> --labels labels.csv`
  replays a tape of (ts, liquidity_usd, price_usd) through the Decider
  and reports precision / recall vs ground-truth labels. Ships with four
  synthetic tapes for immediate use.

### Fixed

- **Funding sign for shorts.** Previously the planner treated funding
  uniformly regardless of side: a short paying negative funding was
  credited as income instead of charged as cost, so net P&L came out
  *higher* than gross. Fixed: `estimated_funding_usd` now represents the
  cost to the trader, with the sign flipping correctly based on
  inferred side. Verified across all four side × funding-sign cases.

### Changed

- README updated with the DEX × CEX matrix and a current command map.
- Architecture diagram refreshed to show both screening and monitoring
  flows on both venues.
- `pyproject.toml` description updated to reflect current scope.
- `mypy` added to CI; type errors now fail the build.

## v0.4.0 — 2026-06-18

### Added

- Real-time monitor decision engine (`Decider`) with three rules:
  critical floor, large single event, slow bleed. Per-rule debounce
  counters. Severity ordering: `EXECUTE > ALERT > NONE`.
- JSONL audit log written to `./audit/<chain>-<addr>-<utc>.jsonl` per
  watch run. `--no-audit` and `--audit-dir` flags.
- Four alert channels with env-gated activation: console (always on),
  Telegram, Discord webhook, ntfy. Stdlib urllib for all HTTP — zero
  new runtime deps.

### Changed

- Strict windowed-delta semantics: `windowed_delta_pct(W)` returns
  `None` unless the buffer spans at least `W` seconds. Prevents the
  60-second slow-bleed rule from firing off 10 seconds of observations.

## v0.3.0 — 2026-06-18

### Added

- `watch` subcommand. Polls DexScreener every N seconds, maintains a
  rolling buffer of liquidity events, computes windowed deltas (`vs L0`,
  `Δ 10s`, `Δ 60s`, `Δ 5m`) for display. Noop decision engine; alert
  channels and real rules in v0.4.

## v0.2.0 — 2026-06-18

### Added

- Package refactor: flat `memecheck.py` split into `memecheck/` with
  `common/`, `scanner/`, and `monitor/` subpackages. Backward-compat
  shim retained for `python3 memecheck.py <addr>` invocations.
- **Exit-liquidity simulator** (`scan --buy-size <USD>`). Constant-product
  math on the deepest pool; reports price impact, immediate round-trip
  slippage, and the max safe buy size at a target impact threshold.

### Fixed

- Documented that round-trip slippage on a constant-product AMM is
  bounded by ~2 × fee regardless of pool depth — the metric users
  actually want to look at is *price impact*, which scales with trade
  size relative to depth.

## v0.1.0 — 2026-06-18

### Added

- Initial release. `scan` subcommand: pre-trade risk screen aggregating
  DexScreener, RugCheck (Solana), and honeypot.is (EVM). Auto-detects
  chain from address format. `calc` subcommand: isolated-margin
  liquidation-price calculator. Zero runtime dependencies.
