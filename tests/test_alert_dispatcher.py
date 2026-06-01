"""Alert dispatcher + channels — env-gated, mocked HTTP, no live network."""

from __future__ import annotations

import asyncio

import pytest

from memecheck.monitor.action import alert as alert_mod
from memecheck.monitor.action.alert import (
    AlertDispatcher,
    ConsoleAlertChannel,
    DiscordWebhookAlertChannel,
    NtfyAlertChannel,
    TelegramAlertChannel,
)
from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    ACTION_NONE,
    Decision,
)
from memecheck.monitor.source import LiquidityEvent


def _mk_event(liq: float = 1000.0) -> LiquidityEvent:
    return LiquidityEvent(
        ts=12345.0,
        base_reserve=100.0,
        quote_reserve=1.0,
        quote_price_usd=100.0,
        liquidity_usd=liq,
        price_usd=0.5,
        source="test",
    )


def _mk_decision(action: str, reason: str = "test reason") -> Decision:
    return Decision(action=action, reason=reason, metrics={
        "delta_vs_baseline_pct": -25.0,
        "delta_10s_pct": -22.0,
        "delta_60s_pct": -12.0,
        "delta_300s_pct": -16.0,
    })


# ----------------------------- env gating --------------------------------


def test_telegram_returns_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MEMECHECK_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("MEMECHECK_TELEGRAM_CHAT_ID", raising=False)
    assert TelegramAlertChannel.from_env() is None


def test_telegram_returns_channel_with_env(monkeypatch) -> None:
    monkeypatch.setenv("MEMECHECK_TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("MEMECHECK_TELEGRAM_CHAT_ID", "12345")
    ch = TelegramAlertChannel.from_env()
    assert ch is not None
    assert ch.name == "telegram"


def test_discord_returns_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MEMECHECK_DISCORD_WEBHOOK", raising=False)
    assert DiscordWebhookAlertChannel.from_env() is None


def test_discord_returns_channel_with_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "MEMECHECK_DISCORD_WEBHOOK",
        "https://discord.com/api/webhooks/xxx/yyy",
    )
    ch = DiscordWebhookAlertChannel.from_env()
    assert ch is not None
    assert ch.name == "discord"


def test_ntfy_returns_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MEMECHECK_NTFY_TOPIC", raising=False)
    assert NtfyAlertChannel.from_env() is None


def test_ntfy_returns_channel_with_env(monkeypatch) -> None:
    monkeypatch.setenv("MEMECHECK_NTFY_TOPIC", "memecheck-test")
    ch = NtfyAlertChannel.from_env()
    assert ch is not None
    assert ch.name == "ntfy"


# ----------------------------- console channel ---------------------------


def test_console_channel_writes_to_stderr(capsys) -> None:
    ch = ConsoleAlertChannel()
    res = ch.send_sync(_mk_decision(ACTION_ALERT), _mk_event(), header="TEST/SOL")
    assert res.ok
    cap = capsys.readouterr()
    assert "[ALERT]" in cap.err
    assert "TEST/SOL" in cap.err


# ----------------------------- network channels (mocked) -----------------


def test_telegram_send_makes_correct_request(monkeypatch) -> None:
    calls: list[tuple[str, dict, bytes]] = []
    def fake_post(url, body, headers, timeout=10):
        calls.append((url, headers, body))
        return True, "HTTP 200"
    monkeypatch.setattr(alert_mod, "_post", fake_post)

    ch = TelegramAlertChannel(token="ABCXYZ", chat_id="42")
    res = ch.send_sync(_mk_decision(ACTION_ALERT), _mk_event(), header="X/Y")
    assert res.ok
    assert res.channel == "telegram"
    assert len(calls) == 1
    url, headers, body = calls[0]
    assert "api.telegram.org" in url
    assert "ABCXYZ" in url
    assert headers["Content-Type"] == "application/json"
    assert b"chat_id" in body
    assert b"42" in body


def test_discord_send_makes_correct_request(monkeypatch) -> None:
    calls: list[tuple[str, dict, bytes]] = []
    def fake_post(url, body, headers, timeout=10):
        calls.append((url, headers, body))
        return True, "HTTP 204"
    monkeypatch.setattr(alert_mod, "_post", fake_post)

    ch = DiscordWebhookAlertChannel(webhook_url="https://discord.com/api/webhooks/x/y")
    res = ch.send_sync(_mk_decision(ACTION_EXECUTE), _mk_event(), header="X/Y")
    assert res.ok
    assert len(calls) == 1
    url, _headers, body = calls[0]
    assert url == "https://discord.com/api/webhooks/x/y"
    assert b"content" in body


def test_ntfy_send_uses_priority_for_execute(monkeypatch) -> None:
    captured: dict = {}
    def fake_post(url, body, headers, timeout=10):
        captured["url"] = url
        captured["headers"] = headers
        return True, "HTTP 200"
    monkeypatch.setattr(alert_mod, "_post", fake_post)

    ch = NtfyAlertChannel(topic="my-topic", server="https://ntfy.example")
    ch.send_sync(_mk_decision(ACTION_EXECUTE), _mk_event(), header="X/Y")
    assert captured["url"] == "https://ntfy.example/my-topic"
    assert captured["headers"]["Priority"] == "5"

    captured.clear()
    ch.send_sync(_mk_decision(ACTION_ALERT), _mk_event(), header="X/Y")
    assert captured["headers"]["Priority"] == "3"


def test_network_channel_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        alert_mod, "_post", lambda *a, **kw: (False, "HTTP 500")
    )
    ch = DiscordWebhookAlertChannel(webhook_url="https://discord.com/api/webhooks/x/y")
    res = ch.send_sync(_mk_decision(ACTION_ALERT), _mk_event(), header="X/Y")
    assert not res.ok
    assert "500" in res.detail


# ----------------------------- dispatcher --------------------------------


def test_dispatcher_does_nothing_on_none() -> None:
    d = AlertDispatcher(channels=[ConsoleAlertChannel()])
    results = asyncio.run(
        d.dispatch(_mk_decision(ACTION_NONE), _mk_event(), header="X/Y")
    )
    assert results == []


def test_dispatcher_fans_out_on_alert(capsys) -> None:
    d = AlertDispatcher(channels=[ConsoleAlertChannel()])
    results = asyncio.run(
        d.dispatch(_mk_decision(ACTION_ALERT), _mk_event(), header="X/Y")
    )
    assert len(results) == 1
    assert results[0].ok
    cap = capsys.readouterr()
    assert "[ALERT]" in cap.err


def test_dispatcher_from_env_includes_console_when_no_other_channels(monkeypatch) -> None:
    for v in (
        "MEMECHECK_TELEGRAM_TOKEN",
        "MEMECHECK_TELEGRAM_CHAT_ID",
        "MEMECHECK_DISCORD_WEBHOOK",
        "MEMECHECK_NTFY_TOPIC",
    ):
        monkeypatch.delenv(v, raising=False)
    d = AlertDispatcher.from_env()
    assert d.channel_names == ["console"]


def test_dispatcher_from_env_includes_all_configured(monkeypatch) -> None:
    monkeypatch.setenv("MEMECHECK_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("MEMECHECK_TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("MEMECHECK_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/x/y")
    monkeypatch.setenv("MEMECHECK_NTFY_TOPIC", "topic")
    d = AlertDispatcher.from_env()
    assert d.channel_names == ["console", "telegram", "discord", "ntfy"]
