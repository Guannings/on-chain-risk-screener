"""Append-only JSONL audit log.

Every monitored run writes a single newline-delimited JSON file with one
record per significant event (tick observation, decision evaluation,
action dispatch, error). Records are flushed on every write so a hard
crash doesn't lose any state that was already evaluated.

File naming: `<audit_dir>/<chain>-<addr-fingerprint>-<utc-timestamp>.jsonl`
where addr-fingerprint is the first 8 + last 4 chars of the address.
Override `--audit-dir` to point elsewhere; pass `--no-audit` to disable.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO


def _fingerprint(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:8]}-{addr[-4:]}"


def _utc_filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class AuditLogger:
    """Append-only JSONL writer. Use `.write(kind, payload)` per entry."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._path: Optional[Path] = None
        self._fh: Optional[TextIO] = None
        if not enabled:
            return
        if path is None:
            raise ValueError("AuditLogger requires a path when enabled")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        # Line buffering — every write hits disk on newline.
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

    @classmethod
    def for_run(
        cls,
        chain: str,
        address: str,
        *,
        audit_dir: Optional[Path] = None,
        enabled: bool = True,
    ) -> "AuditLogger":
        if not enabled:
            return cls(path=None, enabled=False)
        base = audit_dir or Path.cwd() / "audit"
        filename = f"{chain}-{_fingerprint(address)}-{_utc_filename_timestamp()}.jsonl"
        return cls(path=base / filename, enabled=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        if not self._enabled or self._fh is None:
            return
        entry = {
            "ts": time.time(),
            "iso_ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **payload,
        }
        self._fh.write(json.dumps(entry, default=str) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
