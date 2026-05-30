"""Address-format detection and EVM chain mapping — pure functions, no network."""

from __future__ import annotations

import memecheck


def test_solana_address_recognised() -> None:
    # WIF mint, real format
    assert memecheck.is_solana_address("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm")


def test_evm_address_rejected() -> None:
    assert not memecheck.is_solana_address("0xdAC17F958D2ee523a2206206994597C13D831ec7")


def test_random_string_rejected() -> None:
    assert not memecheck.is_solana_address("hello world")
    assert not memecheck.is_solana_address("0xshort")
    assert not memecheck.is_solana_address("")


def test_evm_chain_map_has_expected_chains() -> None:
    for name in ("ethereum", "base", "bsc", "arbitrum", "polygon", "optimism", "avalanche"):
        assert name in memecheck.EVM_CHAIN_IDS
    # Aliases collapse to the same chain ID
    assert memecheck.EVM_CHAIN_IDS["eth"] == memecheck.EVM_CHAIN_IDS["ethereum"]
    assert memecheck.EVM_CHAIN_IDS["arb"] == memecheck.EVM_CHAIN_IDS["arbitrum"]
    assert memecheck.EVM_CHAIN_IDS["matic"] == memecheck.EVM_CHAIN_IDS["polygon"]
