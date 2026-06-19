# Changelog

All notable changes to memecheck. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.6.2 — 2026-06-19

Multi-source fallback pass. Closes self-review #4 (single-source dependency).

### Added — GeckoTerminal DEX fallback (`geckoterminal.py` + `fetch_dex_pairs`)
- New module adapts CoinGecko's GeckoTerminal pools API to the
  DexScreener pair-dict shape that the rest of the codebase consumes.
  No new dependencies; same `derive_reserves` math works end-to-end.
- Unified `fetch_dex_pairs(addr, forced_chain)` tries DexScreener
  first, then GeckoTerminal. Result is tagged with `_source` so
  callers know where the data came from.
- Scanner and monitor source both use the unified dispatcher.
  Tool keeps working when DexScreener is down.
- Network slug translation table covers Ethereum/BSC/Base/Arbitrum/
  Polygon/Optimism/Avalanche/Solana/Fantom.

### Added — Deribit + BitMEX funding sources (`funding.py`)
- `fetch_deribit_funding` hits `/public/ticker?instrument_name=
  BTC-PERPETUAL` and normalises `current_funding` from per-8h decimal
  to per-8h percent. Mark price carried through. Deribit doesn't
  publish a stable predicted rate, so that field stays None.
- `fetch_bitmex_funding` hits `/instrument?symbol=XBTUSD`, normalises
  `fundingRate` (per-8h decimal → per-8h percent) AND
  `indicativeFundingRate` (next-cycle prediction → also normalised).
  BTC → XBT alias built in.
- Source order: Kraken Futures → Hyperliquid → Deribit → BitMEX.
  Dispatched dynamically by name so each source is individually
  monkey-patchable.
- All four sources picked for non-China / non-Musk-firm criteria.

### Tests
227 passing (was 209). mypy clean across 35 source files.

---

## v0.6.1 — 2026-06-19

Real-time gaps pass. Three additions close the longstanding "is this thing
actually real-time?" critiques from the self-review (#5, #6, #9).

### Added — stdlib-only WebSocket client (`ws_client.py` + `hyperliquid_ws.py`)
- Minimal RFC 6455 client built on `socket` + `ssl`. No `websockets`
  dependency added — zero-runtime-dep promise preserved.
- Supports TLS, the upgrade handshake (key generation + Accept validation),
  text frames at all three payload-length encodings (7/16/64-bit), masked
  outbound frames, ping → automatic pong, and close frames.
- Hyperliquid wrapper subscribes to `trades` and `allMids`. Sub-second event
  flow against `wss://api.hyperliquid.xyz/ws`.
- New subcommand `memecheck hl-stream <COIN>` prints live trades; `--mids`
  switches to the all-coins mid-price tick stream. `--max-events` bounds.

### Added — pool-migration auto-resolve (`source.py`)
- The DEX watch source now detects when the watched pool's liquidity
  drops below 50% of its baseline AND a *different* pool for the same
  token has at least 1.5× the watched depth. When both fire, it
  switches the watched pool, re-baselines liquidity, and emits a
  `migration:` notice. 60-second cooldown prevents thrashing.
- Closes the pump.fun → Raydium migration blind spot — previously the
  monitor would stare at the empty old pool indefinitely.
- New `enable_migration_resolve` flag on `DexScreenerPollSource` lets
  callers disable the behaviour for testing.

### Added — latency-SLO instrumentation (`latency.py`)
- New `LatencyRecorder` collects per-tick `fetch_s / decide_s /
  dispatch_s / total_s` samples and computes p50/p99 percentiles.
- `watch` accepts `--latency-log <PATH>` to write per-tick JSONL for
  offline analysis. Summary is always printed at exit.
- Ring buffer caps at 50k samples so 24-hour runs don't balloon memory.
- Answers the "how slow is this thing?" question with actual measured
  numbers instead of vibes (typical WIF poll: ~360ms p50, ~580ms p99
  end-to-end on consumer broadband).

### Tests
209 passing (was 187). mypy clean across 34 source files. Closes
self-review items #5, #6, #9 from the v0.5 post-mortem.

---

## v0.6.0 — 2026-06-19

Math-layer accuracy pass. Three sharp upgrades to the position planner and
exit simulator, all surfacing higher-fidelity numbers than the previous
release.

### Added — tiered maintenance margin (`mmr_tiers.py`)
- Published MMR schedules for Kraken Futures, Bybit, and Deribit (BTC + alt
  defaults per venue).
- `plan` accepts `--venue {kraken-futures, bybit, deribit}`. Combined with
  `--symbol`, the planner looks up the correct tier for the position notional
  and uses that MMR for the liquidation formula instead of the constant 0.5%.
- `--maint-margin` still wins if explicitly set. New `maint_margin_source`
  field on `PositionPlan` records which path was taken.
- `cex-prep` automatically passes `venue="kraken-futures"` so its liquidation
  distance reflects exchange-real behaviour at any size.

### Added — predicted-funding-aware planner
- `FundingRateResult` now carries `rate_per_8h_pct_next`. Kraken's
  `fundingRatePrediction` field is normalised the same way as the current rate
  and populated; Hyperliquid leaves it `None`.
- `compute_plan` accepts `funding_pct_8h_next`. When held for ≥1 cycle, the
  first cycle uses the predicted rate and remaining cycles use the current
  rate. Better cost estimate than the previous "current rate × all cycles".
- Output shows both numbers, e.g.
  `(cycle-1 uses -0.007% predicted, then -0.004%)`.

### Added — Jupiter-routed multi-pool exit-sim (`jupiter.py`)
- New module hitting Jupiter's public quote API
  (`lite-api.jup.ag/swap/v1/quote`). For Solana scans with `--buy-size`, the
  scanner reports a *realistic* multi-pool price-impact estimate alongside
  the V2 single-pool estimate.
- Output adds a `Realistic (Jupiter, N-hop route)` line and, when V2 fired
  a conservative flag that Jupiter contradicts, a softening note ("Jupiter
  would route around the thin pool").
- Covers concentrated-liquidity (Whirlpool, CLMM) and multi-pool splits —
  both of which the V2 math previously ignored. EVM still V2-only (1inch or
  0x would be the parallel client; out of scope here).

### Tests
187 passing (was 169). mypy clean across 30 source files. Closes
self-review items #15, #16, #17 from the v0.5 post-mortem.

---

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
