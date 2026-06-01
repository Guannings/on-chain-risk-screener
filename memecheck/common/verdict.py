"""Verdict thresholds and decision functions.

All thresholds live here so they can be reviewed in one place and overridden
from CLI flags or the future monitor config.
"""

from __future__ import annotations

from typing import Any, Optional

# Verdict thresholds — tweak here, document in README.
HARD_PASS_FLAG_COUNT: int = 4         # >= this many flags => HARD PASS
THIN_LIQ_USD: float = 20_000.0        # below this is "thin"
LOW_LIQ_MC_RATIO: float = 0.03        # liq/mc under this is "tiny float"
WASH_VOL_LIQ_RATIO: float = 50.0      # 24h vol / liq above this hints at wash
DEAD_VOL_LIQ_RATIO: float = 0.05      # 24h vol / liq below this is "dead"
DEAD_VOL_LIQ_TRUST_CEILING: float = 2_000_000.0  # only trust 'dead' under this liq
NEW_TOKEN_HOURS: float = 24.0         # younger than this => peak rug window
SELL_PRESSURE_RATIO: float = 1.5      # sells/buys above this => distribution
LP_LOCKED_FLOOR_PCT: float = 50.0     # LP locked under this => dev can pull
TOP10_CONCENTRATION_PCT: float = 50.0 # top10 above this => one-dump risk
INSIDER_CONCENTRATION_PCT: float = 15.0
SELL_TAX_CEILING_PCT: float = 10.0    # sell tax above this is a flag

# Exit-liquidity simulator thresholds (only fire when --buy-size is given).
EXIT_SLIPPAGE_FLAG_PCT: float = 5.0      # round-trip above this gets a flag
EXIT_SLIPPAGE_SEVERE_PCT: float = 20.0   # above this is a severe flag


def make_verdict(all_flags: list[str], honeypot_metrics: Optional[dict[str, Any]]) -> str:
    if honeypot_metrics and honeypot_metrics.get("is_honeypot"):
        return "HONEYPOT — do not buy"
    if not all_flags:
        return "No automatic red flags — but 'no flags' != 'good bet'. Narrative/timing still decide it."
    if len(all_flags) >= HARD_PASS_FLAG_COUNT:
        return "HARD PASS"
    return "RISKY — proceed only with money already written off"


def exit_code_for(verdict: str, all_flags: list[str]) -> int:
    if verdict.startswith("HONEYPOT"):
        return 2
    if verdict.startswith("HARD PASS") or verdict.startswith("RISKY"):
        return 1
    return 0
