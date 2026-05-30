"""Analyzer tests for honeypot.is — fully mocked."""

from __future__ import annotations

import memecheck


def test_clean_honeypot_no_flags(clean_honeypot) -> None:
    flags, notes, metrics = memecheck.analyze_honeypot(clean_honeypot)
    assert flags == []
    assert metrics["is_honeypot"] is False
    assert metrics["open_source"] is True
    assert any("can sell" in n.lower() for n in notes)


def test_honeypot_detected(honeypot_response) -> None:
    flags, _notes, metrics = memecheck.analyze_honeypot(honeypot_response)
    joined = " ".join(flags).lower()
    assert "honeypot" in joined
    assert "cannot sell" in joined
    # High sell tax also flagged
    assert any("sell tax" in f.lower() for f in flags)
    # Closed source also flagged
    assert any("open source" in f.lower() for f in flags)
    assert metrics["is_honeypot"] is True
    assert metrics["open_source"] is False


def test_high_sell_tax_flagged(high_tax_honeypot) -> None:
    flags, _notes, metrics = memecheck.analyze_honeypot(high_tax_honeypot)
    assert any("sell tax" in f.lower() and "skims" in f.lower() for f in flags)
    assert metrics["sell_tax_pct"] == 25.0
    assert metrics["is_honeypot"] is False


def test_unavailable_honeypot_no_crash() -> None:
    flags, notes, metrics = memecheck.analyze_honeypot({"_error": "boom"})
    assert flags == []
    assert metrics["available"] is False
    assert any("honeypot.is: unavailable" in n.lower() for n in notes)
    # The classifier should fall through to the raw message for unknown errors.
    assert metrics["error_reason"] == "boom"


def test_rate_limit_classified() -> None:
    _, notes, metrics = memecheck.analyze_honeypot(
        {"_error": "HTTP 429 for https://api.honeypot.is/v2/IsHoneypot?address=0x..."}
    )
    assert "rate-limited" in metrics["error_reason"]
    assert any("rate-limited" in n.lower() for n in notes)


def test_service_error_classified() -> None:
    _, _notes, metrics = memecheck.analyze_honeypot(
        {"_error": "HTTP 503 for https://api.honeypot.is/v2/IsHoneypot?address=0x..."}
    )
    assert "service error" in metrics["error_reason"]


def test_timeout_classified() -> None:
    _, _notes, metrics = memecheck.analyze_honeypot(
        {"_error": "TimeoutError: The read operation timed out (https://api.honeypot.is/...)"}
    )
    assert "timed out" in metrics["error_reason"]


def test_pair_not_found_classified() -> None:
    # honeypot.is sometimes returns 200 OK with {"error": "Pair not found"} in the body.
    _, _notes, metrics = memecheck.analyze_honeypot({"error": "Pair not found"})
    assert "no liquidity pair" in metrics["error_reason"].lower()


def test_http_400_classified_as_pair_or_index_issue() -> None:
    _, _notes, metrics = memecheck.analyze_honeypot({"_error": "HTTP 400 for ..."})
    assert "pair" in metrics["error_reason"].lower() or "index" in metrics["error_reason"].lower()


def test_empty_response_classified() -> None:
    _, _notes, metrics = memecheck.analyze_honeypot({})
    assert metrics["error_reason"] == "no response"
