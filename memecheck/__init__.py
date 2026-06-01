"""memecheck — on-chain risk screener.

Two lifecycles share the same analyzers and verdict layer:
  * scanner: synchronous, one-shot, stdlib-only — call `memecheck scan <addr>`
  * monitor: asynchronous, long-running, websocket-based (Phase 1+)

The names at the top level of this package re-export the public API so the
existing test suite (which imports `import memecheck` and accesses symbols
as attributes) works unchanged against the new package layout.
"""

from __future__ import annotations

# Public API re-exports — preserved for backward compatibility with the
# legacy flat memecheck.py module.

# Verdict layer + thresholds
from memecheck.common.verdict import (  # noqa: F401
    DEAD_VOL_LIQ_RATIO,
    DEAD_VOL_LIQ_TRUST_CEILING,
    EXIT_SLIPPAGE_FLAG_PCT,
    EXIT_SLIPPAGE_SEVERE_PCT,
    HARD_PASS_FLAG_COUNT,
    INSIDER_CONCENTRATION_PCT,
    LOW_LIQ_MC_RATIO,
    LP_LOCKED_FLOOR_PCT,
    NEW_TOKEN_HOURS,
    SELL_PRESSURE_RATIO,
    SELL_TAX_CEILING_PCT,
    THIN_LIQ_USD,
    TOP10_CONCENTRATION_PCT,
    WASH_VOL_LIQ_RATIO,
    exit_code_for,
    make_verdict,
)

# Format helpers
from memecheck.common.format import (  # noqa: F401
    fmt_usd,
    is_solana_address,
    pct,
)

# HTTP / source clients
from memecheck.common.http import get_json, UA  # noqa: F401
from memecheck.common.sources import (  # noqa: F401
    EVM_CHAIN_IDS,
    _liq_of,
    fetch_dexscreener,
    fetch_honeypot,
    fetch_rugcheck,
)

# Analyzers
from memecheck.common.analyzers import (  # noqa: F401
    _classify_honeypot_error,
    analyze_dexscreener,
    analyze_honeypot,
    analyze_rugcheck,
)

# Liquidation math
from memecheck.common.liquidation import (  # noqa: F401
    liq_report,
    liq_report_dict,
    liquidation_price,
)

# Exit-liquidity simulator
from memecheck.common.liquidity_math import (  # noqa: F401
    DEFAULT_FEE_BPS,
    cp_amm_out,
    derive_reserves,
    fee_bps_for_dex,
    max_safe_buy_usd,
    round_trip_slippage,
)

# Runner + CLI entry
from memecheck.scanner.runner import run_token  # noqa: F401
from memecheck.cli import main  # noqa: F401
