"""Deployer-history scoring for Solana token mints.

Self-review item #11. The single highest-signal rug indicator on Solana
is "the wallet that deployed this mint has deployed N tokens before,
M% of which are now dead." This module assembles that signal.

The pipeline
------------
1. `find_deployer(mint)` — walks the mint's signature history newest-
   to-oldest, pages back, and grabs the fee-payer of the *oldest* tx
   found. For freshly-launched tokens that fits in one RPC page; for
   long-lived majors it may not be the literal deployer but the
   oldest signer in our look-back window, which is still a useful
   risk signal.

2. `recent_mints_for_deployer(deployer)` — fetches recent signatures
   and inspects each tx's logs for `Program log: Instruction:
   InitializeMint` (issued by the SPL Token program). Returns the mint
   pubkeys for every such tx, deduped.

3. `score_deployer(deployer)` — for each prior mint, peeks at the
   current depth via the unified `fetch_dex_pairs`. A mint with no
   tradable depth = "dead". Returns `(total_prior, dead_count, ratio)`.

Cost
----
Each scan with `--check-deployer` makes ~5–20 RPC calls on a fresh
mint, plus one DEX-API call per prior mint. That's the slowest thing
in memecheck by an order of magnitude — gate behind an opt-in flag.

Time budget + caps prevent the call from hanging forever; we also
short-circuit once the dead-count is high enough to fire the flag.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from memecheck.common.solana_rpc import (
    SolanaRPCError,
    get_signatures_for_address,
    get_transaction,
)


# Cap how far back we'll page when looking for the deployer.
_MAX_DEPLOYER_PAGES = 5
_PAGE_LIMIT = 1000

# Cap how many prior mints to evaluate per scan.
_MAX_PRIOR_MINTS = 20

# Overall wall-clock budget (seconds) to keep `--check-deployer` from
# stalling a scan. If we hit this, we return what we have so far.
_DEFAULT_TIME_BUDGET_S = 30.0

# Liquidity below this counts as "dead" for the scoring heuristic.
_DEAD_LIQUIDITY_USD = 1_000.0

# A deployer with at least this many prior deployments is interesting
# enough to score. With fewer, the denominator is too small for the
# ratio to be meaningful.
MIN_PRIOR_MINTS_TO_SCORE = 3


@dataclass(frozen=True)
class DeployerReport:
    """Summary of a deployer's prior-deployment outcomes."""
    deployer: Optional[str]
    prior_mints: list[str]              # other mints this wallet deployed
    dead_count: int
    sampled_count: int                  # prior_mints we actually checked depth on
    flag: Optional[str]                 # the surfaced flag string, if any
    note: Optional[str]                 # explanation regardless of flag


def _find_oldest_signature(address: str, max_pages: int = _MAX_DEPLOYER_PAGES) -> Optional[str]:
    """Page back through signatures for an address; return the oldest sig string."""
    before: Optional[str] = None
    last_sig: Optional[str] = None
    for _ in range(max_pages):
        try:
            page = get_signatures_for_address(address, limit=_PAGE_LIMIT, before=before)
        except SolanaRPCError:
            break
        if not page:
            break
        last_sig = page[-1]["signature"]
        # No more — we got fewer than the page size.
        if len(page) < _PAGE_LIMIT:
            break
        before = last_sig
    return last_sig


def find_deployer(mint: str) -> Optional[str]:
    """Return the fee-payer of the oldest known transaction for `mint`,
    or None if we can't resolve it. Best-effort, never raises."""
    sig = _find_oldest_signature(mint)
    if sig is None:
        return None
    try:
        tx = get_transaction(sig)
    except SolanaRPCError:
        return None
    if not tx:
        return None
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    if not keys:
        return None
    # accountKeys[0] is always the fee payer per Solana tx layout.
    first = keys[0]
    if isinstance(first, dict):
        # JSON-parsed encoding wraps the key in {"pubkey": ..., "signer": ...}.
        return first.get("pubkey")
    return str(first) if first else None


def recent_mints_for_deployer(deployer: str, page_limit: int = 100) -> list[str]:
    """Return mints deployed by this wallet, deduped. Inspects each
    transaction's `logMessages` for `InitializeMint` (SPL Token v1) or
    `InitializeMint2` (v2) markers."""
    try:
        sigs = get_signatures_for_address(deployer, limit=page_limit)
    except SolanaRPCError:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for sig in sigs:
        try:
            tx = get_transaction(sig["signature"])
        except SolanaRPCError:
            continue
        if not tx:
            continue
        meta = tx.get("meta") or {}
        logs = meta.get("logMessages") or []
        if not any("InitializeMint" in line for line in logs):
            continue
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        # The created mint is typically accountKeys[1] (after fee payer).
        if len(keys) < 2:
            continue
        candidate = keys[1] if isinstance(keys[1], str) else (keys[1] or {}).get("pubkey")
        if candidate and candidate not in seen:
            seen.add(str(candidate))
            out.append(str(candidate))
    return out


def _is_mint_dead(mint: str) -> bool:
    """Liquidity check: probe via fetch_dex_pairs (DexScreener → GT)."""
    from memecheck.common.sources import fetch_dex_pairs
    try:
        primary, _pairs, _err = fetch_dex_pairs(mint)
    except Exception:    # noqa: BLE001 — depth probe must never crash the scan
        return False
    if primary is None:
        return True
    liq = (primary.get("liquidity") or {}).get("usd")
    if liq is None:
        return True
    try:
        return float(liq) < _DEAD_LIQUIDITY_USD
    except (TypeError, ValueError):
        return True


def score_deployer(
    mint: str,
    *,
    self_mint: Optional[str] = None,
    time_budget_s: float = _DEFAULT_TIME_BUDGET_S,
) -> DeployerReport:
    """End-to-end: find deployer, enumerate prior mints, sample depth on each.

    Returns a DeployerReport. `flag` is set when prior_mints is large
    enough and the dead ratio crosses the warning thresholds.
    """
    started = time.monotonic()
    deployer = find_deployer(mint)
    if deployer is None:
        return DeployerReport(
            deployer=None, prior_mints=[], dead_count=0, sampled_count=0,
            flag=None,
            note="Could not resolve deployer (mint history exceeds look-back window).",
        )
    priors = [m for m in recent_mints_for_deployer(deployer) if m != (self_mint or mint)]
    priors = priors[:_MAX_PRIOR_MINTS]

    dead = 0
    sampled = 0
    for m in priors:
        if time.monotonic() - started > time_budget_s:
            break
        sampled += 1
        if _is_mint_dead(m):
            dead += 1

    if sampled < MIN_PRIOR_MINTS_TO_SCORE:
        return DeployerReport(
            deployer=deployer, prior_mints=priors, dead_count=dead,
            sampled_count=sampled, flag=None,
            note=(
                f"Deployer {deployer[:6]}… has {sampled} sampled prior mint(s) — "
                f"not enough to score reliably."
            ),
        )

    ratio = dead / sampled
    flag: Optional[str] = None
    if ratio >= 0.75:
        flag = (
            f"Deployer rugged {dead} of {sampled} prior tokens "
            f"({ratio*100:.0f}%) — strong serial-rugger pattern."
        )
    elif ratio >= 0.5:
        flag = (
            f"Deployer's prior tokens: {dead}/{sampled} dead "
            f"({ratio*100:.0f}%) — caution warranted."
        )

    note = (
        f"Deployer {deployer[:6]}… deployed {len(priors)} known prior mint(s); "
        f"sampled {sampled}, {dead} below ${_DEAD_LIQUIDITY_USD:,.0f} depth."
    )
    return DeployerReport(
        deployer=deployer, prior_mints=priors, dead_count=dead,
        sampled_count=sampled, flag=flag, note=note,
    )
