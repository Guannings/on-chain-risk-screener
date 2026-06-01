"""Alert dispatch — pluggable channels, env-gated.

Phase 2 ships four channels. The console channel is always on. The three
network channels (Telegram, Discord webhook, ntfy) only construct
themselves if the relevant env var is set, so the tool runs out of the
box with zero configuration. None are required.

Environment variables:

  MEMECHECK_TELEGRAM_TOKEN     — bot token from @BotFather
  MEMECHECK_TELEGRAM_CHAT_ID   — target chat id (DM, group, or channel)

  MEMECHECK_DISCORD_WEBHOOK    — full webhook URL from channel Integrations

  MEMECHECK_NTFY_TOPIC         — topic name (https://ntfy.sh/<topic>)
  MEMECHECK_NTFY_SERVER        — optional, defaults to https://ntfy.sh

Network calls go through stdlib urllib via asyncio.run_in_executor so the
async runner is never blocked. No new runtime deps.

Phase 3 will add an EXECUTE-mode wallet-signing action behind a separate
opt-in gate (MEMECHECK_BURNER_KEY env var); the alert channels in this
module never sign anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from memecheck.common.format import fmt_usd
from memecheck.monitor.decision import (
    ACTION_ALERT,
    ACTION_EXECUTE,
    Decision,
)
from memecheck.monitor.source import LiquidityEvent

_UA = {"User-Agent": "memecheck/0.4 monitor"}


@dataclass(frozen=True)
class DispatchResult:
    channel: str
    ok: bool
    detail: str


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 10) -> tuple[bool, str]:
    """Synchronous POST helper. Returns (success, detail)."""
    req = urllib.request.Request(url, data=body, headers={**_UA, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _format_alert(decision: Decision, event: LiquidityEvent, header: str) -> str:
    parts = [
        f"[{decision.action}] {header}",
    ]
    if decision.reason:
        parts.append(decision.reason)
    parts.append(
        f"liq={fmt_usd(event.liquidity_usd)}  px=${event.price_usd:.8g}"
    )
    m = decision.metrics
    deltas = []
    if m.get("delta_vs_baseline_pct") is not None:
        deltas.append(f"vs L0 {m['delta_vs_baseline_pct']:+.2f}%")
    if m.get("delta_10s_pct") is not None:
        deltas.append(f"10s {m['delta_10s_pct']:+.2f}%")
    if m.get("delta_60s_pct") is not None:
        deltas.append(f"60s {m['delta_60s_pct']:+.2f}%")
    if m.get("delta_300s_pct") is not None:
        deltas.append(f"5m {m['delta_300s_pct']:+.2f}%")
    if deltas:
        parts.append("  ".join(deltas))
    return "\n".join(parts)


# ----------------------------- channels ----------------------------------


class AlertChannel:
    name: str = "abstract"

    def send_sync(self, decision: Decision, event: LiquidityEvent, header: str) -> DispatchResult:
        raise NotImplementedError


class ConsoleAlertChannel(AlertChannel):
    name = "console"

    def send_sync(self, decision: Decision, event: LiquidityEvent, header: str) -> DispatchResult:
        body = _format_alert(decision, event, header)
        # All alerts go to stderr so stdout stays usable for tick logs / piping.
        sys.stderr.write("\n" + "=" * 60 + "\n" + body + "\n" + "=" * 60 + "\n")
        sys.stderr.flush()
        return DispatchResult(channel=self.name, ok=True, detail="stderr")


class TelegramAlertChannel(AlertChannel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id

    @classmethod
    def from_env(cls) -> Optional["TelegramAlertChannel"]:
        token = os.environ.get("MEMECHECK_TELEGRAM_TOKEN")
        chat_id = os.environ.get("MEMECHECK_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return None
        return cls(token=token, chat_id=chat_id)

    def send_sync(self, decision: Decision, event: LiquidityEvent, header: str) -> DispatchResult:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": _format_alert(decision, event, header),
            "disable_web_page_preview": True,
        }
        body = json.dumps(payload).encode("utf-8")
        ok, detail = _post(url, body, {"Content-Type": "application/json"})
        return DispatchResult(channel=self.name, ok=ok, detail=detail)


class DiscordWebhookAlertChannel(AlertChannel):
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self._webhook = webhook_url

    @classmethod
    def from_env(cls) -> Optional["DiscordWebhookAlertChannel"]:
        url = os.environ.get("MEMECHECK_DISCORD_WEBHOOK")
        return cls(webhook_url=url) if url else None

    def send_sync(self, decision: Decision, event: LiquidityEvent, header: str) -> DispatchResult:
        payload = {"content": _format_alert(decision, event, header)}
        body = json.dumps(payload).encode("utf-8")
        ok, detail = _post(
            self._webhook, body, {"Content-Type": "application/json"}
        )
        return DispatchResult(channel=self.name, ok=ok, detail=detail)


class NtfyAlertChannel(AlertChannel):
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh") -> None:
        self._topic = topic
        self._server = server.rstrip("/")

    @classmethod
    def from_env(cls) -> Optional["NtfyAlertChannel"]:
        topic = os.environ.get("MEMECHECK_NTFY_TOPIC")
        if not topic:
            return None
        server = os.environ.get("MEMECHECK_NTFY_SERVER", "https://ntfy.sh")
        return cls(topic=topic, server=server)

    def send_sync(self, decision: Decision, event: LiquidityEvent, header: str) -> DispatchResult:
        url = f"{self._server}/{urllib.parse.quote(self._topic, safe='')}"
        body = _format_alert(decision, event, header).encode("utf-8")
        priority = "5" if decision.action == ACTION_EXECUTE else "3"
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": f"memecheck {decision.action}",
            "Priority": priority,
            "Tags": "rotating_light" if decision.action == ACTION_EXECUTE else "warning",
        }
        ok, detail = _post(url, body, headers)
        return DispatchResult(channel=self.name, ok=ok, detail=detail)


# ----------------------------- dispatcher --------------------------------


class AlertDispatcher:
    """Owns a list of AlertChannel instances and fans out alerts to each."""

    def __init__(self, channels: Iterable[AlertChannel]) -> None:
        self._channels: list[AlertChannel] = list(channels)

    @classmethod
    def from_env(cls, *, include_console: bool = True) -> "AlertDispatcher":
        channels: list[AlertChannel] = []
        if include_console:
            channels.append(ConsoleAlertChannel())
        for ctor in (
            TelegramAlertChannel.from_env,
            DiscordWebhookAlertChannel.from_env,
            NtfyAlertChannel.from_env,
        ):
            ch = ctor()
            if ch is not None:
                channels.append(ch)
        return cls(channels=channels)

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self._channels]

    async def dispatch(
        self,
        decision: Decision,
        event: LiquidityEvent,
        header: str,
    ) -> list[DispatchResult]:
        """Send the alert to every channel, in parallel. Returns per-channel results."""
        if decision.action not in (ACTION_ALERT, ACTION_EXECUTE):
            return []
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(None, ch.send_sync, decision, event, header)
            for ch in self._channels
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)
