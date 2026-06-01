"""AuditLogger — file creation, JSONL shape, disable, fingerprint."""

from __future__ import annotations

import json
from pathlib import Path

from memecheck.monitor.audit import AuditLogger, _fingerprint


def test_fingerprint_short_addr_returns_as_is() -> None:
    assert _fingerprint("abc") == "abc"
    assert _fingerprint("0xshort") == "0xshort"


def test_fingerprint_long_addr_truncated() -> None:
    addr = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    fp = _fingerprint(addr)
    assert fp.startswith("EKpQGSJt-")
    assert fp.endswith("-zcjm")


def test_audit_disabled_writes_nothing(tmp_path: Path) -> None:
    a = AuditLogger.for_run(chain="solana", address="x" * 40, audit_dir=tmp_path, enabled=False)
    a.write("tick", {"foo": 1})
    a.write("decision", {"action": "NONE"})
    a.close()
    # No file should exist.
    assert list(tmp_path.iterdir()) == []


def test_audit_enabled_writes_jsonl(tmp_path: Path) -> None:
    a = AuditLogger.for_run(chain="solana", address="x" * 40, audit_dir=tmp_path, enabled=True)
    a.write("tick", {"tick": 1, "liquidity_usd": 1000})
    a.write("decision", {"action": "NONE"})
    a.write("dispatch", {"results": [{"channel": "console", "ok": True}]})
    a.close()

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 3
    entries = [json.loads(ln) for ln in lines]
    assert entries[0]["kind"] == "tick"
    assert entries[0]["tick"] == 1
    assert entries[1]["kind"] == "decision"
    assert entries[1]["action"] == "NONE"
    assert entries[2]["kind"] == "dispatch"
    # Every entry must carry a timestamp.
    for e in entries:
        assert "ts" in e
        assert "iso_ts" in e


def test_audit_context_manager_closes_file(tmp_path: Path) -> None:
    with AuditLogger.for_run(chain="ethereum", address="0x" + "1" * 40, audit_dir=tmp_path) as a:
        a.write("start", {"address": "0x" + "1" * 40})
    # The file should exist after the with block.
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    assert "\"kind\": \"start\"" in content
