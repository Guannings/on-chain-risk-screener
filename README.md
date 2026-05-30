# memecheck

![tests](https://github.com/Guannings/memecheck/actions/workflows/tests.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A single-file, zero-dependency on-chain **risk screener** for ERC-20 and SPL
tokens. Point it at a contract address and it pulls live data from three public
sources, scores them against a documented set of red-flag thresholds, and prints
either a human-readable report or structured JSON.

> This is a screening tool, not a trading signal. It surfaces the mechanical
> failure modes of a token — rug pulls, honeypots, dead liquidity, insider
> concentration — that a buyer can verify before sending funds. It does not
> predict price. **It is not financial advice.** See the disclaimer at the
> bottom.

## What it checks and why each check matters

Every metric below is aggregated across **every pool for the token on a single
chain**, so a multi-pool token's depth and volume are not under-counted, and
liquidity is never summed across different deployments of the same address.

### Market structure — [DexScreener](https://dexscreener.com) (all chains)

| Check | Why it matters |
|---|---|
| Aggregated USD liquidity | Below ~$20k, you are the slippage on exit. |
| Liq / Market-Cap ratio | A ratio under ~0.03 means a tiny float is propping up a large nominal valuation. Exits move the price violently. |
| 24h volume / liquidity | Above ~50× hints at wash trading or bot churn. Below ~0.05× (and liquidity under $2M) signals dead interest. The tool intentionally distrusts the "dead" verdict on mega-cap tokens because DexScreener's token endpoint can under-count volume for many-pool assets. |
| Age (earliest pool) | Tokens under 24 hours old sit in the peak rug-pull window with no track record. |
| Buys vs sells (24h count) | Sell count above 1.5× buy count is consistent with active distribution. |

### Contract authority and holder structure — [RugCheck](https://rugcheck.xyz) (Solana only)

| Check | Why it matters |
|---|---|
| Mint authority | If not revoked, the deployer can mint more supply at any time and dilute holders to zero. |
| Freeze authority | If still active, the deployer can freeze your wallet and block sells. |
| LP locked / burned % | Below 50% means the deployer can withdraw liquidity (classic rug). |
| Top-10 holder concentration | Above 50% means one coordinated dump ends the chart. |
| Insider wallet concentration | Wallets RugCheck flags as insider-controlled holding >15% is a separate, additive risk. |
| Explicit `risks[]` of level `danger` / `warning` | Surfaced verbatim. |

### Contract behavior — [honeypot.is](https://honeypot.is) (EVM chains)

| Check | Why it matters |
|---|---|
| Honeypot simulation | The contract is simulated end-to-end. If you can buy but the sell function reverts, it's a honeypot. |
| Buy tax / Sell tax | A sell tax above 10% is the contract skimming you on exit. |
| Open-source flag | Closed-source contracts can't be reviewed, so any behavior is possible. |

## Supported chains

- **Solana**: full coverage (DexScreener + RugCheck). Auto-detected from a
  base58 mint address.
- **EVM**: full coverage (DexScreener + honeypot.is) on Ethereum, BNB Smart
  Chain, Base, Arbitrum, Polygon, Optimism, and Avalanche. Auto-detected from a
  `0x…` address. Other EVM chains that DexScreener indexes will still get the
  DexScreener checks; the honeypot check defaults to Ethereum if the chain is
  unrecognised. Force the chain explicitly with `--chain` (see below).

## Install and run

Requirements: **Python 3.9 or newer**. No third-party runtime dependencies — it
only uses the standard library.

```bash
git clone <your fork or this repo>
cd memecheck
python3 memecheck.py <TOKEN_ADDRESS>
```

You can also install it as a CLI:

```bash
pip install .
memecheck <TOKEN_ADDRESS>
```

### Flags

```
memecheck <ADDRESS>                        # auto-detect chain, human-readable
memecheck <ADDRESS> --chain base           # force EVM chain for honeypot check
memecheck <ADDRESS> --json                 # structured output
memecheck --liq 0.0000123 --lev 10         # liquidation-price calculator only
memecheck <ADDRESS> --liq 0.0123 --lev 5   # screen + liquidation calc
```

`--chain` accepts `ethereum`, `bsc`, `base`, `arbitrum`, `polygon`, `optimism`,
`avalanche`, and common aliases (`eth`, `arb`, `matic`, `avax`).

### Exit codes

The tool returns non-zero on findings, so it composes cleanly in shell pipelines
and CI checks.

| Code | Meaning |
|---|---|
| `0` | No automatic red flags found |
| `1` | Red flags raised (`RISKY` or `HARD PASS`) |
| `2` | Honeypot detected (highest severity) |
| `3` | No data available for the supplied address |

### Verdict thresholds

The verdict is a deterministic function of the flag list and is documented as
named constants at the top of [`memecheck.py`](memecheck.py):

- Honeypot detected → `HONEYPOT — do not buy` (exit 2)
- 4 or more flags → `HARD PASS` (exit 1)
- 1–3 flags → `RISKY — proceed only with money already written off` (exit 1)
- 0 flags → no automatic red flags (exit 0)

Tweak the constants if your risk tolerance differs.

## Sample output

Real run against **$WIF** (`EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm`), an
established Solana memecoin:

```
########## memecheck: EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm ##########

--- Market (DexScreener) ---
  Primary pool: $WIF/SOL on solana via raydium  (aggregated over 30 pools)
  Liquidity: $5.11M   MC/FDV: $191.06M   24h vol: $591.17K
  Age: 921.7 days (22121h, earliest pool)
  Liq / MC ratio: 0.027
  24h vol / liq: 0.12x
  24h txns: 6026 buys / 6194 sells

--- Contract & holders (RugCheck / Solana) ---
  RugCheck score: 23 (lower = safer on the normalised scale)
  Mint authority: revoked
  Freeze authority: revoked
  LP locked/burned: 99.7%
  Top 10 holders: 64.3% of supply

================ RED FLAGS ================
  [!] Liq/MC ratio 0.027 is very low — tiny float holding up a big 'valuation'.
  [!] Top 10 wallets hold 64% — one coordinated dump ends it.
  [!] RugCheck risk: High holder concentration — The top 10 users hold more than 50% token supply

Verdict: RISKY — proceed only with money already written off
Not financial advice. The checks catch rugs, not bad bets.
```

And the EVM path against **PEPE** on Ethereum
(`0x6982508145454Ce325dDbE47a25d4ec3d2311933`):

```
########## memecheck: 0x6982508145454Ce325dDbE47a25d4ec3d2311933 ##########

--- Market (DexScreener) ---
  Primary pool: PEPE/WETH on ethereum via uniswap  (aggregated over 7 pools)
  Liquidity: $26.42M   MC/FDV: $1.42B   24h vol: $398.63K
  Age: 1141.8 days (27403h, earliest pool)
  Liq / MC ratio: 0.019
  24h vol / liq: 0.02x
  Low reported vol/liq, but DexScreener's token endpoint can under-count volume on large multi-pool tokens — eyeball the chart before trusting this.
  24h txns: 275 buys / 280 sells

--- Contract (honeypot.is / EVM chainID 1) ---
  Honeypot check: can sell
  Buy tax: 0.0%   Sell tax: 0.0%

================ RED FLAGS ================
  [!] Liq/MC ratio 0.019 is very low — tiny float holding up a big 'valuation'.

Verdict: RISKY — proceed only with money already written off
```

### JSON mode

`--json` emits the same information as a structured object — flags, per-source
metrics, and the verdict — suitable for piping into other tools:

```json
{
  "address": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
  "chain_type": "solana",
  "sources": {
    "dexscreener": {
      "flags": ["Liq/MC ratio 0.027 is very low ..."],
      "metrics": {
        "chain": "solana", "pool_count": 30,
        "liquidity_usd": 5112869.76, "market_cap_usd": 191056565,
        "volume_24h_usd": 591237.93, "liq_mc_ratio": 0.0268,
        "age_hours": 22120.88
      }
    },
    "rugcheck": {
      "metrics": {
        "score": 23, "mint_authority": null, "freeze_authority": null,
        "lp_locked_pct": 99.66, "top10_pct": 64.35
      }
    }
  },
  "flags": ["..."],
  "verdict": "RISKY — proceed only with money already written off"
}
```

## Liquidation-price calculator

`--liq <entry> --lev <leverage>` prints the approximate isolated-margin
liquidation price for both sides. The formulas, with maintenance margin $mm$:

$$P_\text{liq}^{\text{long}} = P \left(1 - \frac{1}{L} + mm\right) \qquad P_\text{liq}^{\text{short}} = P \left(1 + \frac{1}{L} - mm\right)$$

The default $mm = 0.005$ matches typical perp-DEX defaults. The calculator is a
sanity check — it ignores funding, slippage, and venue-specific liquidation
auctions, all of which make your real liquidation closer than this number.

## Limitations and caveats

- **The DexScreener token endpoint can under-count 24h volume** on large
  multi-pool tokens, which is why the "dead volume" flag is only raised below
  $2M of aggregated liquidity. Look at the chart before trusting that flag in
  isolation.
- **RugCheck and honeypot.is have rate limits and occasional downtime.** When
  either is unavailable, the run continues with a `… unavailable` note and the
  remaining sources still produce flags.
- **DexScreener returns every pool indexed against the token address**, which
  on forked EVMs (e.g. PulseChain mirrors of Ethereum contracts) can return
  unexpected primary pools. Use `--chain ethereum` (or whichever) to constrain
  the primary chain.
- **The verdict reflects only the mechanical checks listed above.** A
  green-light result means the contract is not obviously rigged, not that the
  trade is wise. Narrative, market timing, and base-rate skepticism are still
  yours to apply.
- **No code-level audit.** This tool does not disassemble bytecode or run
  static analysis. It relies on honeypot.is's behavioural simulation and on
  RugCheck's aggregated risk data.

## Development

```bash
pip install -e ".[dev]"
pytest -v
```

The test suite uses mocked API payloads with fixtures for a clean token, a
honeypot, and a high-concentration token. No live network calls are made
during tests.

## License

[MIT](LICENSE).

====================================================================================

# **Disclaimer and Terms of Use**

**1. Educational Purpose Only**

This software is for educational and research purposes only and was built as a personal project by PARVAUX, a Public Finance major at National Chengchi University (NCCU). It is not intended to be a source of financial advice, and the author is not a registered financial advisor or licensed securities professional. The heuristics implemented herein — DexScreener liquidity aggregation, RugCheck authority and concentration analysis, honeypot.is behavioural simulation, and the leverage-liquidation calculator — are demonstrations of mechanical risk-screening concepts and should not be construed as a recommendation to buy, sell, hold, short, or leverage any specific token, contract, or asset.

**2. No Financial Advice**

Nothing in this repository constitutes professional financial, legal, or tax advice. Investment and trading decisions should be made based on your own research and consultation with a qualified financial professional in your jurisdiction. The screening logic modelled in this software may not be suitable for your specific financial situation, risk tolerance, or regulatory environment. Cryptocurrency may be restricted, taxed, or unregulated where you live; compliance is solely your responsibility.

**3. Risk of Loss**

All cryptocurrency activity involves risk, including the total loss of principal and, when leverage is used, the loss of more than the principal originally committed.

a. Past Mechanical Safety: A clean screening result is not a guarantee of future safety. Tokens that pass every check in this tool have rugged, been exploited, and lost all value. Conversely, tokens that fail multiple checks have continued to trade.

b. Screening Limitations: This tool performs aggregated metric checks, authority lookups, and a third-party honeypot simulation. It does not disassemble bytecode, perform static or dynamic analysis on the contract, audit upgrade paths, or detect proxy-pattern abuse, governance attacks, oracle manipulation, MEV exposure, bridge risk, or social-engineering risk.

c. Threshold Sensitivity: Verdicts are derived from documented numeric thresholds (liquidity floor, liq/MC ratio, vol/liq ratio, holder concentration, sell tax, etc.) that may not reflect appropriate risk limits for every market regime, chain, or token category.

d. Market Data: Data fetched from third-party APIs (DexScreener, RugCheck, honeypot.is) may be delayed, rate-limited, incomplete, or temporarily unavailable. The tool degrades gracefully but cannot validate the upstream data's correctness.

e. Leverage Math: The liquidation-price calculator is an approximation. It ignores funding rates, slippage, partial-liquidation auctions, insurance-fund socialisation, oracle-based liquidations, and venue-specific maintenance-margin schedules. Your real liquidation will almost always be closer to entry than the formula suggests.

**4. Data Provider Reliability**

The author has no affiliation with DexScreener, RugCheck, or honeypot.is and assumes no responsibility for outages, incorrect data, API breaking changes, or rate-limit denials originating from those services. The tool may stop working at any time if an upstream provider changes its public endpoints. Users are responsible for verifying any flagged or unflagged finding directly against on-chain data and the underlying contract source.

**5. "AS-IS" SOFTWARE WARRANTY**

**THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. IN NO EVENT SHALL THE AUTHOR OR COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.**

**BY USING THIS SOFTWARE, YOU AGREE TO ASSUME ALL RISKS ASSOCIATED WITH YOUR TRADING, INVESTMENT, AND LEVERAGE DECISIONS, RELEASING THE AUTHOR (PARVAUX) FROM ANY LIABILITY REGARDING YOUR FINANCIAL OUTCOMES, SMART-CONTRACT INTERACTIONS, OR EXCHANGE-LEVEL POSITION OUTCOMES.**

====================================================================================
