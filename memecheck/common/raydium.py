"""Raydium AMM v4 on-chain pool decoder.

When the off-chain aggregators (DexScreener, GeckoTerminal) all fail or
return stale data, we can read pool reserves directly from the chain
via the Solana RPC. This module decodes the AMM v4 liquidity-state
account layout.

Layout (offsets verified live against the WIF/SOL pool
EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx):

    field            type      offset    length
    ---------------- --------- -------   ------
    ...                                  (32 bytes per u64 chunk earlier)
    baseDecimal      u64           32       8
    quoteDecimal     u64           40       8
    ...                                  (state, fee numerators, etc.)
    baseVault        publicKey    336      32
    quoteVault       publicKey    368      32
    baseMint         publicKey    400      32
    quoteMint        publicKey    432      32
    ...

Total account length: 752 bytes.

CPMM and CLMM pools have different layouts — explicitly NOT supported
here; calling code should check the account owner and fall back if it
isn't the AMM v4 program.

A successful decode yields a `RaydiumPool` with token-side amounts in
UI units (not atomic) ready for downstream math.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Optional

from memecheck.common.solana_rpc import (
    RAYDIUM_AMM_V4_PROGRAM,
    SolanaRPCError,
    base58_encode,
    get_account_info,
    get_token_account_balance,
)


AMM_V4_ACCOUNT_LEN = 752

# Field offsets within the AMM v4 layout.
_OFFSET_BASE_DECIMAL = 32
_OFFSET_QUOTE_DECIMAL = 40
_OFFSET_BASE_VAULT = 336
_OFFSET_QUOTE_VAULT = 368
_OFFSET_BASE_MINT = 400
_OFFSET_QUOTE_MINT = 432


@dataclass(frozen=True)
class RaydiumPool:
    """One Raydium AMM v4 pool's decoded state + vault balances."""
    pool_address: str
    base_mint: str
    quote_mint: str
    base_vault: str
    quote_vault: str
    base_decimals: int
    quote_decimals: int
    base_reserve_ui: float       # full-token units, e.g. 12_597_887.004 WIF
    quote_reserve_ui: float      # full-token units, e.g. 29_787.244 SOL


def parse_amm_v4_layout(data: bytes) -> Optional[dict]:
    """Decode the publicKey + decimal fields. Pure — no I/O.

    Returns a dict with mint/vault Base58 strings + decimals, or None
    if the buffer is the wrong length.
    """
    if len(data) < AMM_V4_ACCOUNT_LEN:
        return None
    (base_decimals,) = struct.unpack_from("<Q", data, _OFFSET_BASE_DECIMAL)
    (quote_decimals,) = struct.unpack_from("<Q", data, _OFFSET_QUOTE_DECIMAL)
    return {
        "base_vault": base58_encode(data[_OFFSET_BASE_VAULT:_OFFSET_BASE_VAULT + 32]),
        "quote_vault": base58_encode(data[_OFFSET_QUOTE_VAULT:_OFFSET_QUOTE_VAULT + 32]),
        "base_mint": base58_encode(data[_OFFSET_BASE_MINT:_OFFSET_BASE_MINT + 32]),
        "quote_mint": base58_encode(data[_OFFSET_QUOTE_MINT:_OFFSET_QUOTE_MINT + 32]),
        "base_decimals": int(base_decimals),
        "quote_decimals": int(quote_decimals),
    }


def decode_raydium_pool(pool_address: str) -> Optional[RaydiumPool]:
    """Fetch + decode a Raydium AMM v4 pool entirely on-chain.

    Returns None when:
      - account doesn't exist
      - account owner is not the AMM v4 program (CPMM/CLMM pools)
      - account data is the wrong length
      - any vault lookup fails

    Three RPC calls per successful decode: one getAccountInfo, two
    getTokenAccountBalance.
    """
    try:
        info = get_account_info(pool_address)
    except SolanaRPCError:
        return None
    if info is None:
        return None
    if info.get("owner") != RAYDIUM_AMM_V4_PROGRAM:
        return None
    data_field = info.get("data")
    if not data_field or not isinstance(data_field, list) or not data_field[0]:
        return None
    try:
        raw = base64.b64decode(data_field[0])
    except (ValueError, TypeError):
        return None
    parsed = parse_amm_v4_layout(raw)
    if parsed is None:
        return None

    try:
        base_bal = get_token_account_balance(parsed["base_vault"])
        quote_bal = get_token_account_balance(parsed["quote_vault"])
    except SolanaRPCError:
        return None
    if base_bal is None or quote_bal is None:
        return None

    # Prefer uiAmount when published; fall back to dividing raw amount by 10^decimals.
    def _ui(bal: dict, decimals: int) -> Optional[float]:
        v = bal.get("uiAmount")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        raw_amt = bal.get("amount")
        if raw_amt is None:
            return None
        try:
            return float(raw_amt) / (10 ** decimals)
        except (TypeError, ValueError, OverflowError):
            return None

    base_ui = _ui(base_bal, parsed["base_decimals"])
    quote_ui = _ui(quote_bal, parsed["quote_decimals"])
    if base_ui is None or quote_ui is None:
        return None

    return RaydiumPool(
        pool_address=pool_address,
        base_mint=parsed["base_mint"],
        quote_mint=parsed["quote_mint"],
        base_vault=parsed["base_vault"],
        quote_vault=parsed["quote_vault"],
        base_decimals=parsed["base_decimals"],
        quote_decimals=parsed["quote_decimals"],
        base_reserve_ui=base_ui,
        quote_reserve_ui=quote_ui,
    )
