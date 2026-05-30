"""Shared pytest fixtures: mocked API payloads.

These deliberately mirror the *shape* of the real responses, not full content.
No live network calls anywhere in the test suite.
"""

from __future__ import annotations

import pytest


# --------------------------- DexScreener fixtures -------------------------

@pytest.fixture
def clean_dex_pairs() -> list[dict]:
    """Healthy token: deep liquidity, balanced flow, mature, multi-pool on one chain."""
    return [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "baseToken": {"symbol": "CLEAN"},
            "quoteToken": {"symbol": "SOL"},
            "liquidity": {"usd": 8_000_000},
            "volume": {"h24": 4_000_000},
            "txns": {"h24": {"buys": 1500, "sells": 1450}},
            "marketCap": 120_000_000,
            "fdv": 120_000_000,
            "pairCreatedAt": 1_700_000_000_000,  # late 2023
        },
        {
            "chainId": "solana",
            "dexId": "orca",
            "baseToken": {"symbol": "CLEAN"},
            "quoteToken": {"symbol": "USDC"},
            "liquidity": {"usd": 2_000_000},
            "volume": {"h24": 800_000},
            "txns": {"h24": {"buys": 300, "sells": 280}},
            "marketCap": 120_000_000,
            "pairCreatedAt": 1_701_000_000_000,
        },
    ]


@pytest.fixture
def thin_dex_pairs() -> list[dict]:
    """Brand-new, thin-liquidity token — should trip multiple flags."""
    # Created 6 hours ago in test time terms by being recent enough; we use
    # a far-future created_ms value paired with checking only thresholds in tests.
    # For age-based flag tests we patch datetime.now in the test itself.
    return [
        {
            "chainId": "base",
            "dexId": "uniswap",
            "baseToken": {"symbol": "THIN"},
            "quoteToken": {"symbol": "WETH"},
            "liquidity": {"usd": 8_000},        # below THIN_LIQ_USD
            "volume": {"h24": 4_000},           # vol/liq = 0.5, healthy
            "txns": {"h24": {"buys": 10, "sells": 40}},  # sells >> buys
            "marketCap": 5_000_000,             # liq/mc = 0.0016 -> flag
            "pairCreatedAt": 1_900_000_000_000,
        }
    ]


@pytest.fixture
def wash_dex_pairs() -> list[dict]:
    """Possible wash trading: 24h vol >> liquidity."""
    return [
        {
            "chainId": "ethereum",
            "dexId": "uniswap",
            "baseToken": {"symbol": "WASH"},
            "quoteToken": {"symbol": "WETH"},
            "liquidity": {"usd": 100_000},
            "volume": {"h24": 10_000_000},   # 100x liq
            "txns": {"h24": {"buys": 5000, "sells": 5000}},
            "marketCap": 50_000_000,
            "pairCreatedAt": 1_700_000_000_000,
        }
    ]


# --------------------------- RugCheck fixtures ----------------------------

@pytest.fixture
def clean_rugcheck() -> dict:
    return {
        "score_normalised": 5,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "markets": [{"lp": {"lpLockedPct": 100.0}}],
        "topHolders": [{"pct": 1.0, "insider": False} for _ in range(10)],
        "risks": [],
    }


@pytest.fixture
def high_concentration_rugcheck() -> dict:
    """Top-10 wallets hold the bag; mint authority not revoked."""
    return {
        "score_normalised": 85,
        "token": {
            "mintAuthority": "SomeMintAuth1111111111111111111111111111111",
            "freezeAuthority": None,
        },
        "markets": [{"lp": {"lpLockedPct": 20.0}}],   # < 50 -> flag
        "topHolders": (
            [{"pct": 12.0, "insider": True} for _ in range(5)]   # 60% insiders
            + [{"pct": 3.0, "insider": False} for _ in range(5)] # +15
        ),
        "risks": [
            {"level": "danger", "name": "Top holders", "description": "Top 10 hold over 70%."},
        ],
    }


@pytest.fixture
def unavailable_rugcheck() -> dict:
    return {"_error": "HTTP 503 for https://api.rugcheck.xyz/..."}


# --------------------------- honeypot.is fixtures -------------------------

@pytest.fixture
def clean_honeypot() -> dict:
    return {
        "honeypotResult": {"isHoneypot": False},
        "simulationResult": {"buyTax": 0.0, "sellTax": 0.0},
        "contractCode": {"openSource": True},
        "flags": [],
    }


@pytest.fixture
def honeypot_response() -> dict:
    return {
        "honeypotResult": {"isHoneypot": True, "honeypotReason": "Cannot sell"},
        "simulationResult": {"buyTax": 5.0, "sellTax": 99.0},
        "contractCode": {"openSource": False},
        "flags": ["High sell tax"],
    }


@pytest.fixture
def high_tax_honeypot() -> dict:
    return {
        "honeypotResult": {"isHoneypot": False},
        "simulationResult": {"buyTax": 2.0, "sellTax": 25.0},
        "contractCode": {"openSource": True},
        "flags": [],
    }
