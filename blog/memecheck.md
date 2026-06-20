# Building memecheck, a crypto risk screener I wrote because I lost ten bucks

**PARVAUX · June 2026**

Earlier this month I bought a Solana memecoin that had surged six thousand percent in maybe half an hour. I put in ten dollars. When I tried to sell, the slippage was so bad the swap basically wouldn't go through. The pool had something like ten dollars of real liquidity. I'd lost the trade before I made it and I made it anyway. The coin is still in my Phantom wallet because I can't even sell it.

I'm a Public Finance and Economics double major at NCCU, so I had no business buying any memecoin, but I also had no business being so casual about checking. The tools to catch this trade existed. RugCheck would have told me about the holder concentration. DexScreener would have told me about the depth. A sober look at the price action would have told me about everything. I had four browser tabs open and I didn't read any of them, because four tabs is more friction than I had patience for.

So the next weekend I wrote a single Python file, about 350 lines, called `memecheck.py`. It took a token address, called three APIs, and printed a verdict. I tried it on a few tokens and felt smarter about not buying them. That was the whole project for a week or two.

The version of `memecheck` I'm describing now is a lot bigger than that, and was mostly built over the last couple of weeks. It's on PyPI, it has 269 tests, and it does substantially more than the original. Most of that work happened in collaboration with Claude. I want to say that up front because there's no point hiding it: a lot of the typing and the boilerplate isn't mine. The design decisions, threshold values, math derivations, and the parts of the architecture that I can defend on a whiteboard are mine. If you pick a file from the repo and ask why it's structured the way it is, that's what I owe you an answer for, and the README's Part 4 is my attempt at that answer in long form.

This post is the short version. What I built, what I learned, what I'd say if you asked me about it in an interview.

## What it actually does

There's a DEX side and a CEX side, both reached from one CLI. `scan <token_address>` is the pre-trade check: it pulls market data from DexScreener (or GeckoTerminal if DS is having a bad day), contract data from RugCheck on Solana or honeypot.is on EVM, decodes the Raydium AMM v4 pool state directly from Solana RPC for ground-truth reserves, and optionally walks the deployer's wallet to count how many of their previous mints are now dead. It composes all of that into one of three verdicts: HARD PASS, RISKY, or OK.

`cex-check <SYMBOL>` is the equivalent for perpetuals. Kraken Futures and Hyperliquid funding rates (Deribit and BitMEX as fallbacks), basis vs mark price, open interest deltas. Same severity ladder.

A position planner sits in the middle of all this. Give it your account size, your stop, and your risk budget and it does R-multiple sizing with side-aware funding cost, tier-aware maintenance margin, and a take-profit table. Then `prep` and `cex-prep` are composed workflows that run the screen and the planner together, and refuse to print a position size if the screen verdict was HARD PASS.

A real-time monitor (`watch`, `cex-watch`) replays these checks every few seconds and pings Telegram or Discord or ntfy if anything breaks.

## Math worth deriving

I shipped the first version without really understanding the formulas. Then I forced myself to derive each one, and the math was less intimidating than I thought.

The constant-product AMM, given pool reserves $R_{in}$ and $R_{out}$ and a swap of $A_{in}$ tokens after fee $f$:

$$A_{out} = \frac{R_{out} \cdot A_{in}(1-f)}{R_{in} + A_{in}(1-f)}$$

That's all the exit-liquidity simulator runs on. The effective fill price, the immediate round-trip loss (capped at roughly $2f$ on a Uniswap V2-style pool), and the max-safe-buy at a given target price impact all fall out of that formula and a binary search. For Solana the V2 estimate is intentionally pessimistic because Orca's Whirlpools and most Raydium CLMM pools are concentrated-liquidity, so the tool also calls Jupiter's quote API for a realistic multi-pool number and shows both side by side.

R-multiple sizing, where $R$ is a fixed dollar risk budget and $d_{sl}$ is the fractional stop-loss distance:

$$N = \frac{R}{d_{sl}}$$

Position notional. Independent of leverage, independent of conviction. The thing finance classes gesture at without enforcing, and the thing the planner refuses to violate.

Isolated-margin liquidation on a long at entry $P$ with leverage $L$ and maintenance margin $mm$:

$$P_{liq}^{long} = P\left(1 - \frac{1}{L} + mm\right)$$

The piece I got wrong the first time was treating $mm$ as a constant. Real exchanges publish tiered schedules where the maintenance margin rises with position notional. Kraken Futures at five thousand dollars is at 0.4%, at five million dollars it's at 5%. Ignoring tiers underestimates liquidation distance by something like ten times at size. The `--venue` flag picks the right tier from Kraken's, Bybit's, and Deribit's published schedules. None of this matters at retail scale, but it was a useful exercise.

Funding cost on a perp is signed. Positive funding means longs pay shorts. I wrote it the wrong way the first time, ignored the side: cost = $r \cdot N \cdot c$ where $c$ is hold cycles, regardless of long or short. The right version flips the sign: $+r$ for longs, $-r$ for shorts. I noticed because a `cex-prep XRP` short produced a net P&L *higher* than gross. Free money is the universal sign-error indicator. There's a regression test now covering all four side × funding-sign combinations.

## Architecture in one paragraph

Three things produce events in this system: DEX pools, CEX perps, and a backtest replay. They all flow into the same engine, which is a windowed state buffer plus a Decider with three rules. The rules are: a critical floor at $L_t/L_0 < 0.5$, a large single-tick event at $\Delta L_{10s} \leq -20\%$, and a slow bleed when $\Delta L_{60s} \leq -10\%$ and $\Delta L_{300s} \leq -15\%$ both hold. Output goes to stdout, a JSONL audit log, a SQLite trade journal, and an alert dispatcher with env-gated channels. Adding a new chain or venue is a fifty-line adapter, not a fork. The full thing is around 4500 lines of Python with zero runtime dependencies, which I'm slightly proud of.

## Did you measure it

For most of the build, my answer to "do your thresholds actually work" was that I picked them from DeFi research norms and they seemed reasonable. Which isn't an answer, it's a vibe. So I built a corpus pipeline that scrapes GeckoTerminal's OHLCV endpoint, auto-detects rug-shaped events (sustained peak, eighty-percent-plus drawdown, no recovery), and writes them out in the format the backtest harness already consumes. Then `memecheck sweep` grids over the rule thresholds and reports precision and recall.

The corpus I built this weekend is small. Four real events: SOLANGELES on Solana, ASTEROID and DOGEUS on Ethereum, ODIC on BSC, all between eighty and eighty-five percent peak-to-trough drops. Across every combination of thresholds I tried, the rules caught all four. Hundred percent recall on a corpus of four. Precision at the tick level is around 0.2%, but that's because the rules continue firing every tick after a threshold is crossed, so a single rug event contributes thousands of "predictions." At the event level, four out of four.

Four is not a publishable N. The path to a real corpus is either fifteen dollars a month for Bitquery or a multi-week forward-monitoring run across hundreds of new launches. The harness consumes the same CSV format either way. Building bigger is mechanical, not architectural.

## Things I'd own up to

The synthetic tapes that shipped before the real corpus only proved the rules don't fire on noise. The real-data sweep is better, but it's N=4. Threshold values are still hand-set instead of optimized against labels. The exit simulator's V2 math is wrong for concentrated-liquidity pools, which I cover on Solana via Jupiter but not on EVM. The real-time monitor polls REST every few seconds instead of subscribing to websockets for most data, with one exception, the Hyperliquid stream, where I wrote an RFC 6455 client from scratch on top of `socket` and `ssl` to stay within the zero-dependency promise. And I haven't actually used the tool in real trading. I should, before I claim the journal is useful.

## What it taught me

The thing that surprised me wasn't anything about Python. It was how much of finance is sign conventions. The funding-cost bug, the side-aware liquidation formula, the position sizing that turns out not to reference leverage at all — each one is obvious right until you write it wrong once. Production trading code is, I think, a lot of this. Boring discipline about signs and units and edge cases, dressed up in risk-adjusted return language. The math part is easier than the bookkeeping.

The other thing I learned is to respect tools whose value you can't see. If I'm using memecheck the way it's meant to be used, most of what it does looks like decisions I didn't make. Pools I didn't buy. Sizes I didn't pick. There's no notification when a defensive tool works, which makes them hard to be motivated by, and I think that's why most retail traders don't have them. I started this because of ten dollars. I kept going on it because I wanted to understand how the prevention worked, not because I was getting any reward from it.

The repo is at [github.com/Guannings/on-chain-risk-screener](https://github.com/Guannings/on-chain-risk-screener) if you want to read the actual code. `pip install memecheck` if you want to run it. The README is long and the CHANGELOG is detailed; I'd start with the README's Part 4, which is the engineering tour, if you have ten minutes.
