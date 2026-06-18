"""`memecheck prep` — composed pre-entry workflow.

Verifies the gating logic: scan + plan together, refusal on HONEYPOT /
HARD PASS, warning on RISKY, green-light on clean. Scan + funding fetcher
both mocked — no live network.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from memecheck import cli
from memecheck.scanner import runner as scanner_runner


# ----------------------------- shared fakes ------------------------------


def _make_args(**overrides):
    """Build a Namespace mirroring the prep argparse output, with overrides."""
    import argparse
    base = dict(
        cmd="prep",
        address="0x1111111111111111111111111111111111111111",
        chain=None,
        account=1000.0,
        risk=1.0,
        entry=0.0001,
        stop=0.000094,
        leverage=None,
        symbol=None,
        funding=0.01,
        hold_hours=24.0,
        force=False,
        as_json=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_scan_to_return(monkeypatch, verdict: str, exit_code: int = 0) -> None:
    """Patch run_token to return a chosen verdict without touching the network."""
    def fake_run_token(addr, forced_chain=None, as_json=False,
                       buy_size_usd=None, max_slippage_pct=5.0,
                       fee_bps_override=None):
        # Print something so we can verify the scan output flows through.
        if not as_json:
            print(f"[FAKE scan: {verdict}]")
        return (
            {"address": addr, "verdict": verdict, "sources": {}, "flags": []},
            exit_code,
        )
    monkeypatch.setattr(scanner_runner, "run_token", fake_run_token)
    # _run_prep imports run_token directly from scanner.runner inside the
    # function body, so the module-level attribute is what gets resolved.


# ----------------------------- refusal cases -----------------------------


def test_prep_refuses_on_honeypot(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "HONEYPOT — do not buy", exit_code=2)
    args = _make_args(entry=1.0, stop=0.95)
    code = cli._run_prep(args)
    out = capsys.readouterr().out
    assert code == 2
    assert "REFUSING TO PRINT PLAN" in out
    assert "HONEYPOT" in out
    # And critically: no plan section should appear.
    assert "TRADE PLAN" not in out
    assert "TAKE-PROFIT SCENARIOS" not in out


def test_prep_refuses_on_hard_pass(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "HARD PASS", exit_code=1)
    args = _make_args(entry=1.0, stop=0.95)
    code = cli._run_prep(args)
    out = capsys.readouterr().out
    assert code == 2  # refusal latches at 2
    assert "REFUSING TO PRINT PLAN" in out
    assert "HARD PASS" in out
    assert "TRADE PLAN" not in out


def test_prep_force_bypasses_refusal(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "HONEYPOT — do not buy", exit_code=2)
    args = _make_args(entry=1.0, stop=0.95, force=True)
    code = cli._run_prep(args)
    out = capsys.readouterr().out
    # Plan is printed under protest. Exit code follows scan severity.
    assert "TRADE PLAN" in out
    assert "under protest" in out
    assert code >= 2  # honeypot scan exit dominates


# ----------------------------- non-refusal cases -------------------------


def test_prep_risky_warns_but_prints_plan(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "RISKY — proceed only with money written off", 1)
    args = _make_args(entry=1.0, stop=0.95)
    code = cli._run_prep(args)
    out = capsys.readouterr().out
    assert "RISKY" in out
    assert "with eyes open" in out
    assert "TRADE PLAN" in out  # plan IS printed
    assert code == 1  # scan exit propagates


def test_prep_clean_prints_plan(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "No automatic red flags — but 'no flags' != 'good bet'.", 0)
    args = _make_args(entry=1.0, stop=0.95)
    code = cli._run_prep(args)
    out = capsys.readouterr().out
    assert "Scan clean" in out
    assert "TRADE PLAN" in out
    assert code == 0


# ----------------------------- coupling: plan notional → scan buy_size --


def test_prep_passes_plan_notional_into_scan(monkeypatch) -> None:
    """Verify the scan's --buy-size receives the plan's computed notional."""
    captured = {}
    def fake_run_token(addr, forced_chain=None, as_json=False,
                       buy_size_usd=None, max_slippage_pct=5.0,
                       fee_bps_override=None):
        captured["buy_size_usd"] = buy_size_usd
        return ({"address": addr, "verdict": "clean", "sources": {}, "flags": []}, 0)
    monkeypatch.setattr(scanner_runner, "run_token", fake_run_token)
    # SL 5%, risk 1% of $1000 = $10, notional = $10 / 0.05 = $200.
    args = _make_args(entry=1.0, stop=0.95, account=1000, risk=1.0)
    cli._run_prep(args)
    assert captured["buy_size_usd"] is not None
    assert abs(captured["buy_size_usd"] - 200.0) < 0.01


# ----------------------------- json output -------------------------------


def test_prep_json_includes_scan_and_plan_when_clean(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "No automatic red flags — but 'no flags' != 'good bet'.", 0)
    args = _make_args(entry=1.0, stop=0.95, as_json=True)
    cli._run_prep(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "scan" in payload
    assert "plan" in payload
    assert payload["gate"]["refused"] is False


def test_prep_json_omits_plan_when_refused(monkeypatch, capsys) -> None:
    _patch_scan_to_return(monkeypatch, "HARD PASS", 1)
    args = _make_args(entry=1.0, stop=0.95, as_json=True)
    cli._run_prep(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "scan" in payload
    assert "plan" not in payload
    assert payload["gate"]["refused"] is True
    assert payload["gate"]["reason"] == "HARD PASS"
