"""Deprecated — see `memecheck.common.threshold_sweep`.

This module name was claimed during Batch F v0.7 work before the existing
`threshold_sweep` module was discovered. Kept as a stub to redirect.

To run a sweep:

    memecheck sweep <tape.csv> <labels.csv>
"""

from __future__ import annotations

from memecheck.common.threshold_sweep import (    # re-export
    SweepPoint,
    format_sweep_table,
    pareto_frontier,
    sweep,
)

__all__ = ["SweepPoint", "format_sweep_table", "pareto_frontier", "sweep"]
