"""SQLite-backed trade journal.

Auto-logged by `prep` and `cex-prep`: every run writes a row with the
verdict, planned notional, and timestamps so you can close the feedback
loop between *what the tool said* and *what happened*.

Default path: ~/.memecheck/journal.sqlite (override with --journal-path).
Stdlib sqlite3 only — no new runtime deps.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


_DEFAULT_PATH: Path = Path.home() / ".memecheck" / "journal.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_ts TEXT NOT NULL,
    venue TEXT NOT NULL,            -- 'dex' or 'cex'
    symbol_or_addr TEXT NOT NULL,
    side TEXT,                      -- 'long' or 'short'
    account_usd REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    leverage REAL NOT NULL,
    position_notional_usd REAL NOT NULL,
    risk_usd REAL NOT NULL,
    verdict TEXT,                   -- scan verdict / cex-check verdict
    refused INTEGER NOT NULL,       -- 0/1
    forced INTEGER NOT NULL,        -- 0/1
    funding_per_8h_pct REAL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_symbol_or_addr ON trades(symbol_or_addr);
"""


@dataclass(frozen=True)
class JournalEntry:
    id: int
    ts: float
    iso_ts: str
    venue: str
    symbol_or_addr: str
    side: Optional[str]
    account_usd: float
    entry_price: float
    stop_price: float
    leverage: float
    position_notional_usd: float
    risk_usd: float
    verdict: Optional[str]
    refused: bool
    forced: bool
    funding_per_8h_pct: Optional[float]
    notes: Optional[str]


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else _DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    with closing(conn.cursor()) as cur:
        cur.executescript(_SCHEMA)
    conn.commit()
    return conn


def log_entry(
    *,
    venue: str,
    symbol_or_addr: str,
    side: Optional[str],
    account_usd: float,
    entry_price: float,
    stop_price: float,
    leverage: float,
    position_notional_usd: float,
    risk_usd: float,
    verdict: Optional[str],
    refused: bool,
    forced: bool,
    funding_per_8h_pct: Optional[float] = None,
    notes: Optional[str] = None,
    path: Optional[Path] = None,
) -> int:
    """Insert one row, return the assigned id."""
    now = time.time()
    iso = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        with closing(conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO trades (
                    ts, iso_ts, venue, symbol_or_addr, side,
                    account_usd, entry_price, stop_price, leverage,
                    position_notional_usd, risk_usd,
                    verdict, refused, forced,
                    funding_per_8h_pct, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now, iso, venue, symbol_or_addr, side,
                    account_usd, entry_price, stop_price, leverage,
                    position_notional_usd, risk_usd,
                    verdict, int(refused), int(forced),
                    funding_per_8h_pct, notes,
                ),
            )
            entry_id = cur.lastrowid
            conn.commit()
            return entry_id or 0
    finally:
        conn.close()


def list_entries(
    *,
    limit: int = 20,
    symbol_or_addr: Optional[str] = None,
    venue: Optional[str] = None,
    path: Optional[Path] = None,
) -> list[JournalEntry]:
    """Return the most recent entries first, optionally filtered."""
    conn = _connect(path)
    try:
        sql = "SELECT * FROM trades WHERE 1=1"
        args: list[Any] = []
        if symbol_or_addr:
            sql += " AND symbol_or_addr = ?"
            args.append(symbol_or_addr)
        if venue:
            sql += " AND venue = ?"
            args.append(venue)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with closing(conn.cursor()) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [
            JournalEntry(
                id=r["id"],
                ts=r["ts"],
                iso_ts=r["iso_ts"],
                venue=r["venue"],
                symbol_or_addr=r["symbol_or_addr"],
                side=r["side"],
                account_usd=r["account_usd"],
                entry_price=r["entry_price"],
                stop_price=r["stop_price"],
                leverage=r["leverage"],
                position_notional_usd=r["position_notional_usd"],
                risk_usd=r["risk_usd"],
                verdict=r["verdict"],
                refused=bool(r["refused"]),
                forced=bool(r["forced"]),
                funding_per_8h_pct=r["funding_per_8h_pct"],
                notes=r["notes"],
            )
            for r in rows
        ]
    finally:
        conn.close()
