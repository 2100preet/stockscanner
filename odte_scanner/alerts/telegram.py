"""Telegram Bot API alerts for BUY/SELL pulses.

Env:
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your user/group chat id (e.g. 123456789)

Optional:
  TELEGRAM_ALERTS_ENABLED=0 to pause Telegram only

Never commit the token — set as GitHub Actions secrets.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _env(*names: str) -> str | None:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return None


def configured() -> dict[str, Any]:
    token = _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")
    chat = _env("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")
    enabled_raw = (os.environ.get("TELEGRAM_ALERTS_ENABLED") or "").strip().lower()
    enabled = enabled_raw not in {"0", "false", "no", "off"}
    return {
        "ok": bool(token and chat and enabled),
        "token_set": bool(token),
        "chat_set": bool(chat),
        "enabled": enabled,
    }


def send_telegram_text(body: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Send a plain text Telegram message via Bot API."""
    text = (body or "").strip()
    if not text:
        return {"ok": False, "skipped": True, "error": "empty body"}

    cfg = configured()
    if not cfg["ok"]:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "Telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)",
        }

    token = _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        payload = r.json() if r.content else {}
        if r.status_code >= 400 or not payload.get("ok"):
            err = payload.get("description") or r.text[:400]
            logger.warning("telegram send failed %s: %s", r.status_code, err)
            return {
                "ok": False,
                "configured": True,
                "provider": "telegram",
                "status_code": r.status_code,
                "error": str(err)[:400],
            }
        msg = (payload.get("result") or {}) if isinstance(payload, dict) else {}
        return {
            "ok": True,
            "configured": True,
            "provider": "telegram",
            "message_id": msg.get("message_id"),
            "status_code": r.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram exception: %s", exc)
        return {"ok": False, "configured": True, "provider": "telegram", "error": str(exc)}
