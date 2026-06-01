"""End-to-end run_token with all three sources monkeypatched — no network.

Note on monkeypatch targets: after the v0.2 refactor, source clients are
imported into `memecheck.scanner.runner` as locals, so monkeypatching the
re-exports at the top-level `memecheck` package would not intercept the
runner's lookups. We patch at the runner's own namespace, which is the
standard Python idiom — patch where the function is *used*, not where it
is *defined*.
"""

from __future__ import annotations

import memecheck
from memecheck.scanner import runner as scanner_runner


# A throwaway EVM-looking address (not used for any real lookup, only to drive
# the EVM branch through is_solana_address).
EVM_ADDR = "0x1111111111111111111111111111111111111111"
SOL_ADDR = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def test_run_token_clean_evm_path(monkeypatch, clean_dex_pairs, clean_honeypot) -> None:
    # Reuse the clean_dex_pairs fixture but tell DexScreener the chain is base.
    pairs = [dict(p, chainId="base") for p in clean_dex_pairs]
    monkeypatch.setattr(scanner_runner, "fetch_dexscreener", lambda a, c=None: (pairs[0], pairs, None))
    monkeypatch.setattr(scanner_runner, "fetch_honeypot", lambda a, cid: clean_honeypot)

    result, code = memecheck.run_token(EVM_ADDR, forced_chain="base", as_json=True)

    assert result["chain_type"] == "evm"
    assert "dexscreener" in result["sources"]
    assert "honeypot" in result["sources"]
    assert result["sources"]["honeypot"]["chain_id"] == 8453
    assert result["flags"] == []
    assert code == 0


def test_run_token_honeypot_exit_code(monkeypatch, clean_dex_pairs, honeypot_response) -> None:
    pairs = [dict(p, chainId="ethereum") for p in clean_dex_pairs]
    monkeypatch.setattr(scanner_runner, "fetch_dexscreener", lambda a, c=None: (pairs[0], pairs, None))
    monkeypatch.setattr(scanner_runner, "fetch_honeypot", lambda a, cid: honeypot_response)

    result, code = memecheck.run_token(EVM_ADDR, as_json=True)

    assert any("honeypot" in f.lower() for f in result["flags"])
    assert result["verdict"].startswith("HONEYPOT")
    assert code == 2


def test_run_token_solana_path_with_high_concentration(
    monkeypatch, clean_dex_pairs, high_concentration_rugcheck
) -> None:
    monkeypatch.setattr(
        scanner_runner, "fetch_dexscreener", lambda a, c=None: (clean_dex_pairs[0], clean_dex_pairs, None)
    )
    monkeypatch.setattr(scanner_runner, "fetch_rugcheck", lambda mint: high_concentration_rugcheck)

    result, code = memecheck.run_token(SOL_ADDR, as_json=True)

    assert result["chain_type"] == "solana"
    assert "rugcheck" in result["sources"]
    # At least 4 flags from rugcheck alone -> HARD PASS
    assert result["verdict"] == "HARD PASS"
    assert code == 1


def test_run_token_no_data(monkeypatch) -> None:
    monkeypatch.setattr(
        scanner_runner, "fetch_dexscreener", lambda a, c=None: (None, [], "No DEX pairs found")
    )
    # Even with no DexScreener data, the EVM branch still calls honeypot.is.
    monkeypatch.setattr(scanner_runner, "fetch_honeypot", lambda a, cid: {"_error": "n/a"})

    result, code = memecheck.run_token(EVM_ADDR, as_json=True)
    assert "error" in result["sources"]["dexscreener"]
    # No flags raised -> exit 0 (data was unavailable, not bad).
    assert code == 0
