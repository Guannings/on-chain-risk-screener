"""Deployer-history scoring tests (RPC + DEX-API stubbed)."""

from __future__ import annotations

import pytest

from memecheck.common import deployer as dep_mod
from memecheck.common.deployer import (
    DeployerReport,
    find_deployer,
    recent_mints_for_deployer,
    score_deployer,
)


# ----------------------------- find_deployer -----------------------------


def test_find_deployer_returns_fee_payer(monkeypatch) -> None:
    """Oldest tx fee-payer = accountKeys[0]."""
    monkeypatch.setattr(
        dep_mod, "get_signatures_for_address",
        lambda addr, limit=1000, before=None: [
            {"signature": "SIG_NEW", "slot": 100},
            {"signature": "SIG_OLD", "slot": 1},
        ],
    )
    monkeypatch.setattr(
        dep_mod, "get_transaction",
        lambda sig: {
            "transaction": {"message": {
                "accountKeys": ["DEPLOYER_PUBKEY", "MINT_PUBKEY"],
            }},
        },
    )
    assert find_deployer("MINT_PUBKEY") == "DEPLOYER_PUBKEY"


def test_find_deployer_handles_dict_account_keys(monkeypatch) -> None:
    """jsonParsed encoding wraps keys in {pubkey, signer}."""
    monkeypatch.setattr(
        dep_mod, "get_signatures_for_address",
        lambda addr, limit=1000, before=None: [{"signature": "SIG"}],
    )
    monkeypatch.setattr(
        dep_mod, "get_transaction",
        lambda sig: {
            "transaction": {"message": {
                "accountKeys": [{"pubkey": "WRAPPED_DEPLOYER", "signer": True}],
            }},
        },
    )
    assert find_deployer("X") == "WRAPPED_DEPLOYER"


def test_find_deployer_returns_none_when_no_sigs(monkeypatch) -> None:
    monkeypatch.setattr(
        dep_mod, "get_signatures_for_address",
        lambda addr, limit=1000, before=None: [],
    )
    assert find_deployer("X") is None


# ----------------------------- recent_mints ------------------------------


def test_recent_mints_filters_on_initialize_mint_log(monkeypatch) -> None:
    """Only txs whose logMessages contain InitializeMint count."""
    monkeypatch.setattr(
        dep_mod, "get_signatures_for_address",
        lambda addr, limit=100, before=None: [
            {"signature": "SIG_A"}, {"signature": "SIG_B"}, {"signature": "SIG_C"},
        ],
    )
    def tx_for(sig: str) -> dict:
        if sig == "SIG_A":
            return {
                "meta": {"logMessages": ["Program log: Instruction: InitializeMint"]},
                "transaction": {"message": {"accountKeys": ["DEPLOYER", "MINT_A"]}},
            }
        if sig == "SIG_B":
            return {
                "meta": {"logMessages": ["Program log: Instruction: Transfer"]},
                "transaction": {"message": {"accountKeys": ["DEPLOYER", "TOKEN_ACCT"]}},
            }
        if sig == "SIG_C":
            return {
                "meta": {"logMessages": ["Program log: Instruction: InitializeMint2"]},
                "transaction": {"message": {"accountKeys": ["DEPLOYER", "MINT_C"]}},
            }
        return {}
    monkeypatch.setattr(dep_mod, "get_transaction", tx_for)
    mints = recent_mints_for_deployer("DEPLOYER")
    assert set(mints) == {"MINT_A", "MINT_C"}


def test_recent_mints_dedupes(monkeypatch) -> None:
    monkeypatch.setattr(
        dep_mod, "get_signatures_for_address",
        lambda addr, limit=100, before=None: [{"signature": "S1"}, {"signature": "S2"}],
    )
    monkeypatch.setattr(
        dep_mod, "get_transaction",
        lambda s: {
            "meta": {"logMessages": ["Program log: Instruction: InitializeMint"]},
            "transaction": {"message": {"accountKeys": ["D", "SAME_MINT"]}},
        },
    )
    assert recent_mints_for_deployer("D") == ["SAME_MINT"]


# ----------------------------- score_deployer ----------------------------


def _stub_dep(monkeypatch, deployer, prior_mints, dead_set):
    """Wire up find_deployer + recent_mints + _is_mint_dead in one place."""
    monkeypatch.setattr(dep_mod, "find_deployer", lambda m: deployer)
    monkeypatch.setattr(
        dep_mod, "recent_mints_for_deployer", lambda d, page_limit=100: list(prior_mints)
    )
    monkeypatch.setattr(dep_mod, "_is_mint_dead", lambda m: m in dead_set)


def test_score_flags_serial_rugger(monkeypatch) -> None:
    """≥75% dead → strong-pattern flag string."""
    priors = [f"M{i}" for i in range(8)]
    dead = set(priors[:7])    # 7/8 = 87.5%
    _stub_dep(monkeypatch, "DEPLOYER", priors, dead)
    r = score_deployer("CURRENT_MINT", self_mint="CURRENT_MINT")
    assert r.deployer == "DEPLOYER"
    assert r.flag is not None
    assert "strong serial-rugger" in r.flag
    assert r.dead_count == 7
    assert r.sampled_count == 8


def test_score_flags_caution_at_midband(monkeypatch) -> None:
    """50-74% dead → caution flag."""
    priors = [f"M{i}" for i in range(6)]
    dead = set(priors[:3])    # 3/6 = 50%
    _stub_dep(monkeypatch, "DEPLOYER", priors, dead)
    r = score_deployer("X")
    assert r.flag is not None
    assert "caution" in r.flag


def test_score_does_not_flag_clean_deployer(monkeypatch) -> None:
    """All prior tokens still alive → no flag."""
    priors = [f"M{i}" for i in range(5)]
    _stub_dep(monkeypatch, "DEPLOYER", priors, set())
    r = score_deployer("X")
    assert r.flag is None
    assert r.dead_count == 0
    assert r.sampled_count == 5


def test_score_skips_when_too_few_priors(monkeypatch) -> None:
    """Fewer than MIN_PRIOR_MINTS_TO_SCORE → never flag."""
    _stub_dep(monkeypatch, "DEPLOYER", ["M1", "M2"], {"M1", "M2"})
    r = score_deployer("X")
    assert r.flag is None
    assert "not enough to score" in (r.note or "")


def test_score_excludes_self_mint(monkeypatch) -> None:
    """The mint being scanned shouldn't count as a prior of itself."""
    priors = ["SELF", "OTHER1", "OTHER2", "OTHER3", "OTHER4"]
    _stub_dep(monkeypatch, "DEPLOYER", priors, {"OTHER1", "OTHER2"})
    r = score_deployer("SELF", self_mint="SELF")
    assert "SELF" not in r.prior_mints


def test_score_returns_none_flag_when_deployer_unresolved(monkeypatch) -> None:
    monkeypatch.setattr(dep_mod, "find_deployer", lambda m: None)
    r = score_deployer("X")
    assert r.deployer is None
    assert r.flag is None
