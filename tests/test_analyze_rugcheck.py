"""Analyzer tests for RugCheck — fully mocked."""

from __future__ import annotations

import memecheck


def test_clean_rugcheck_no_flags(clean_rugcheck) -> None:
    flags, notes, metrics = memecheck.analyze_rugcheck(clean_rugcheck)
    assert flags == []
    assert metrics["mint_authority"] is None
    assert metrics["freeze_authority"] is None
    assert metrics["lp_locked_pct"] == 100.0
    assert metrics["top10_pct"] == 10.0
    # The "revoked" notes should be present
    joined = " ".join(notes).lower()
    assert "mint authority: revoked" in joined
    assert "freeze authority: revoked" in joined


def test_high_concentration_flags_everything(high_concentration_rugcheck) -> None:
    flags, _notes, metrics = memecheck.analyze_rugcheck(high_concentration_rugcheck)
    joined = " ".join(flags).lower()
    assert "mint authority not revoked" in joined
    assert "lp locked" in joined or "of lp locked" in joined
    assert "top 10 wallets" in joined
    assert "insider" in joined
    # Explicit "danger" risk surfaced
    assert any("rugcheck risk" in f.lower() for f in flags)
    # Metrics computed correctly
    assert metrics["top10_pct"] == 75.0  # 5*12 + 5*3
    assert metrics["insider_pct"] == 60.0
    assert metrics["lp_locked_pct"] == 20.0


def test_unavailable_rugcheck_no_crash(unavailable_rugcheck) -> None:
    flags, notes, metrics = memecheck.analyze_rugcheck(unavailable_rugcheck)
    assert flags == []
    assert metrics["available"] is False
    assert any("rugcheck: unavailable" in n.lower() for n in notes)


def test_empty_rugcheck_no_crash() -> None:
    flags, notes, metrics = memecheck.analyze_rugcheck({})
    assert flags == []
    assert metrics["available"] is False
