# What I learned building a crypto risk screener as a finance student

**By PARVAUX · June 2026**

This started with a $10 loss. Six thousand percent green candle, illiquid as concrete, no way out — the canonical first-trade memecoin experience. The tools to catch it existed; I just had to open four browser tabs to use them. So I wrote a single CLI that runs every check that would have stopped that trade. Then I generalized to CEX perpetuals, position sizing, real-time monitoring, on-chain decoding, and a deployer-history scorer. About six months later, the project is `memecheck` on PyPI: a zero-dependency Python tool with 269 tests and 42 source files. This post is what I learned along the way, because the writing the system taught me more than any class did.

## What memecheck actually does

Two halves under one binary. **DEX side**: `scan <token_address>` returns a HARD PASS / RISKY / OK verdict by composing five checks — DexScreener market structure, RugCheck contract authority (Solana), honeypot.is sell-simulation (EVM), an on-chain Raydium AMM v4 decoder for ground-truth reserves, and an optional deployer-history scorer that walks the mint's creator wallet and reports how many of their prior deployments are now dead. **CEX side**: `cex-check <symbol>` does the equivalent for Kraken Futures / Hyperliquid perpetuals — funding rate vs basis vs open interest, with severity classification.

A position planner sits in the middle: $N = R / d_{sl}$, R-multiple sizing with side-aware funding cost, tier-aware maintenance margin from published exchange schedules, predicted-funding-cycle awareness. Then composed workflows (`prep`, `cex-prep`) gate the planner output on the screen verdict, so the calculator literally refuses to size a position that the scan classified as a rug. Real-time monitors (`watch`, `cex-watch`) replay these checks every few seconds against the live pool / perp and dispatch alerts to console / Telegram / Discord / ntfy.

## The math worth deriving from scratch

I shipped without understanding the underlying math, then circled back and forced myself to derive each piece on paper. The four that mattered:

**Constant-product AMM impact.** Given a Uniswap v2-style pool with reserves $R_{in}$ and $R_{out}$ and a swap of $A_{in}$ tokens (after fee $f$), the output is:

$$A_{out} = \frac{R_{out} \cdot A_{in} (1 - f)}{R_{in} + A_{in} (1 - f)}$$

That single formula generates everything `scan --buy-size` reports: effective fill price, immediate round-trip loss (bounded by $\sim 2f$ on V2), and the max-safe-buy at a target price impact. Concentrated-liquidity pools (Uniswap v3, Orca Whirlpool) break this formula at tick crossings — memecheck handles that by delegating to Jupiter's quote API on Solana, which routes across pools and ticks for a realistic impact estimate alongside the conservative V2 single-pool number.

**R-multiple sizing.** Pick a fixed risk budget $R$ in dollars (e.g. $1\%$ of account). Position notional is $N = R / d_{sl}$ where $d_{sl}$ is fractional stop-loss distance. A 1R take-profit needs only $d_{sl}$ in your favor; 3R needs $3 d_{sl}$. Independent of leverage, independent of conviction, independent of vibes. This was the first thing my finance classes gestured at and the first thing the planner enforces.

**Isolated-margin liquidation.** For a long at entry $P$ with leverage $L$ and maintenance margin $mm$:

$$P_{liq}^{long} = P \left( 1 - \frac{1}{L} + mm \right)$$

Symmetric for shorts. The "tier-aware" piece is that real exchanges don't use a constant $mm$ — they publish tiered schedules where MMR rises with position notional. A $5{,}000 position on Kraken uses $mm = 0.004$; a $5{,}000{,}000 position uses $mm = 0.05$. Ignoring tiers under-estimates liquidation distance by 5-10x at size. The `--venue` flag picks the right tier from published Kraken / Bybit / Deribit schedules.

**Side-aware funding.** Positive perp funding means longs pay shorts. So funding cost is signed: $+r \cdot N \cdot c$ for longs, $-r \cdot N \cdot c$ for shorts, where $c$ is hold-cycles. I shipped this wrong the first time and a live `cex-prep XRP` short produced a net P&L *higher* than gross — a free-money bug. The regression test now covers all four side × funding-sign combinations. Lesson: sign conventions deserve real tests, not vibe checks.

## Architecture, in one sentence

Three input families (DEX sources, CEX perp sources, backtest tape, pure math) feed three shared engines (threshold analyzers, the Decider with windowed-delta semantics, the position planner) that emit to a common output bus (stdout, JSONL audit log, SQLite trade journal, env-gated alert dispatcher, exit codes). Every subcommand is a path through that graph. The clean separation means a new chain or venue is a 50-line adapter, not a fork.

The Decider rules: critical floor ($L_t/L_0 < 0.5$), large single event ($\Delta L_{10s} \leq -20\%$ debounced by 2), slow bleed ($\Delta L_{60s} \leq -10\%$ AND $\Delta L_{300s} \leq -15\%$ escalated after 6 ticks). Each one corresponds to a real-world rug pattern: atomic LP pull, large dump, slow distribution. The thresholds were hand-picked from DeFi norms initially — which brings us to the part I'm least proud of, and how I fixed it.

## The hardest critique: "Did you measure it?"

For the first five months, the honest answer to "do your thresholds work?" was "they feel right based on DeFi research norms." That's not validation. So I built a corpus pipeline: `scripts/build_corpus.py` auto-detects historical rug events from GeckoTerminal's free OHLCV endpoint (no API key) by matching the on-chain shape — sustained peak, ≥80% drawdown, no recovery — and writes per-event tape and label CSVs in the format the backtest harness already consumes. `memecheck sweep` then grids over the decision-rule thresholds and reports precision, recall, F1 across the corpus.

The starter corpus I built this weekend is N=4 events scanned live across three chains: SOLANGELES (Solana, $-80.7\%$), ASTEROID (Ethereum, $-80.3\%$), DOGEUS (Ethereum, $-85.0\%$), ODIC (BSC, $-85.4\%$). Across the entire shipped threshold grid: **100% event-level recall**. Tick-level precision is about $0.2\%$, but that's a repeated-firing artifact — the rules continue to fire every tick after a threshold is crossed, so a single rug contributes hundreds of "predictions." The right framing is "given a labelled rug, do the rules fire at least once within the 60-second detection tolerance?" Yes, 4 of 4 here.

N=4 is small. The honest path to N=200 is paid data ($15/mo Bitquery, free tier on The Graph for EVM) or live forward monitoring across many tokens for weeks. The harness consumes the same CSV format either way, so growing the corpus is mechanical, not architectural.

## What I'd own up to in an interview

The synthetic-tape backtest in early versions only proved rules don't fire on noise. The real-data N=4 sweep is better but small. Threshold values are still hand-set, not learned from the corpus — I built the sweep infrastructure but haven't run a full Pareto optimization on labelled data yet. The exit-liquidity simulator is V2-only on EVM (Jupiter handles Solana); concentrated-liquidity math on Uniswap v3 isn't directly implemented. The watch monitor polls REST every 5-30 seconds rather than using sub-second websockets for most data (the Hyperliquid WS source is the exception, built on a hand-rolled stdlib RFC 6455 client because the project has a zero-dependency promise). And the tool is AI-assisted authorship — I want to be straightforward about that in a way that doesn't dodge it: the design decisions, threshold values, math validations, and the parts of the architecture that survive critical review are mine; the typing and the boilerplate aren't. If you pick a file from this repo and ask why it's structured the way it is, that's the right question to ask, and answering it is how I demonstrate ownership.

## What building this taught me

The most useful thing wasn't writing the code — it was discovering, after the fact, how much of finance is just careful sign conventions. The free-funding-for-shorts bug, the side-aware liquidation formula, the position-sizing math that doesn't reference leverage at all: each is "trivially obvious" until you write it down wrong once. Production trading systems are a lot of this — boring discipline about signs, units, and edge cases — wearing the costume of risk-adjusted return optimization. The Python is the easy part.

The second thing was learning to value a tool that prevents losses you can't see. Most of memecheck's value, if I'm using it correctly, looks like decisions I didn't make — pools I didn't buy, positions I didn't size up, perps I avoided around funding flips. There's no notification when a defensive tool works. That's a hard product category to feel rewarded by. Building something useful and unmeasurable was, I think, a more honest finance education than any model-portfolio exercise I've done in school.

If you want to look at the code: `pip install memecheck`, or [github.com/Guannings/on-chain-risk-screener](https://github.com/Guannings/on-chain-risk-screener). The CHANGELOG walks through what got built when. The README is roughly four parts: how to use it on DEX, how to use it on CEX, the math the planner does, and a "what's actually under the hood" tour of the engineering decisions. The full self-review of weakspots and how I closed them is also there. I'd rather you read the README cold than be pitched on it.

---

*PARVAUX is a Public Finance and Economics double major at National Chengchi University, Taipei. Find more at [github.com/Guannings](https://github.com/Guannings).*
