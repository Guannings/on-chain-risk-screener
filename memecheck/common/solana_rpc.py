"""Minimal Solana JSON-RPC client (stdlib only) + Base58 codec.

Why this exists
---------------
Self-review item #8: when both DexScreener AND GeckoTerminal degrade,
the tool currently has no way to read pool state. This module wires up
direct on-chain reads via the official public RPC at
`api.mainnet-beta.solana.com`, with a stdlib HTTP client and a Base58
encoder/decoder.

It also unblocks self-review item #11 — deployer-history scoring —
which needs to walk a wallet's transaction signatures to find prior
mint deployments.

Scope deliberately kept small: this file is the *RPC transport* + the
Base58 codec. Per-program decoders (Raydium AMM v4, SPL token mint)
live in their own modules.

Base58 in 30 lines: implements the Bitcoin alphabet (same as Solana).
The standard pubkey on Solana is 32 bytes → 43-44 char Base58 string.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

# Raydium AMM v4 program ID. Used to verify a pool account is indeed
# laid out as AMM v4 (not CLMM, not CPMM).
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# SPL Token program ID, used to validate that a vault account is a
# token account before reading its balance.
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


# ----------------------------- Base58 codec ------------------------------


_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def base58_encode(data: bytes) -> str:
    """Encode raw bytes to a Base58 string (Bitcoin / Solana alphabet)."""
    # Count leading zeros — each becomes a leading '1'.
    n_leading = 0
    for b in data:
        if b == 0:
            n_leading += 1
        else:
            break
    # Convert to big integer, then divide by 58 repeatedly.
    num = int.from_bytes(data, "big")
    digits: list[int] = []
    while num > 0:
        num, rem = divmod(num, 58)
        digits.append(rem)
    encoded = bytes(_B58_ALPHABET[d] for d in reversed(digits)).decode("ascii")
    return "1" * n_leading + encoded


def base58_decode(s: str) -> bytes:
    """Decode a Base58 string to raw bytes. Raises ValueError on bad input."""
    n_leading_ones = 0
    for c in s:
        if c == "1":
            n_leading_ones += 1
        else:
            break
    num = 0
    for c in s:
        try:
            num = num * 58 + _B58_INDEX[ord(c)]
        except KeyError as e:
            raise ValueError(f"invalid Base58 char: {c!r}") from e
    if num == 0:
        body = b""
    else:
        body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    return b"\x00" * n_leading_ones + body


# ----------------------------- RPC transport ----------------------------


class SolanaRPCError(Exception):
    pass


def rpc_call(
    method: str,
    params: list[Any],
    *,
    url: str = SOLANA_RPC_URL,
    timeout: float = 20.0,
) -> Any:
    """Make one JSON-RPC call. Returns the `result` payload, or raises.

    Public RPCs rate-limit aggressively (~100 req/10s on mainnet-beta).
    Callers that hammer this should batch or back off.
    """
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "memecheck-rpc/0.6",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SolanaRPCError(f"HTTP {e.code} on {method}") from e
    except (urllib.error.URLError, ValueError, OSError) as e:
        raise SolanaRPCError(f"network/parse error on {method}: {e}") from e

    if "error" in payload and payload["error"]:
        err = payload["error"]
        raise SolanaRPCError(f"{method} returned error: {err}")
    return payload.get("result")


# ----------------------------- convenience wrappers ---------------------


def get_account_info(pubkey: str, encoding: str = "base64") -> Optional[dict]:
    """Return the account dict (lamports, owner, data, ...) or None if missing."""
    res = rpc_call("getAccountInfo", [pubkey, {"encoding": encoding}])
    if not res:
        return None
    return res.get("value")


def get_token_account_balance(pubkey: str) -> Optional[dict]:
    """Return {'amount': str, 'decimals': int, 'uiAmount': float | None} or None."""
    res = rpc_call("getTokenAccountBalance", [pubkey])
    if not res:
        return None
    return res.get("value")


def get_signatures_for_address(
    pubkey: str, limit: int = 100, before: Optional[str] = None
) -> list[dict]:
    """Return up to `limit` recent signature info dicts for an address.

    Each dict has keys: signature, slot, blockTime, err, memo. The list is
    sorted newest-first. For older history, page with `before=<signature>`.
    """
    params: list[Any] = [pubkey, {"limit": limit}]
    if before:
        params[1]["before"] = before
    res = rpc_call("getSignaturesForAddress", params)
    if not res:
        return []
    return res if isinstance(res, list) else []


def get_transaction(signature: str, encoding: str = "json") -> Optional[dict]:
    """Return a parsed transaction dict, or None if not found / dropped."""
    res = rpc_call(
        "getTransaction",
        [signature, {"encoding": encoding, "maxSupportedTransactionVersion": 0}],
    )
    return res
