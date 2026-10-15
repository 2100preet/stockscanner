"""Telegram channel posts, one-time join requests, and revoke-on-cancel.

Uses TELEGRAM_BOT_TOKEN. Paid alerts go to TELEGRAM_PAID_CHANNEL_ID.
The free digest goes to TELEGRAM_FREE_CHANNEL_ID.
The existing personal desk chat (TELEGRAM_CHAT_ID) is left alone.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def bot_token() -> str | None:
    value = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
    return value or None


def paid_channel_id() -> str | None:
    value = (os.environ.get("TELEGRAM_PAID_CHANNEL_ID") or "").strip()
    return value or None


def free_channel_id() -> str | None:
    value = (os.environ.get("TELEGRAM_FREE_CHANNEL_ID") or "").strip()
    return value or None


def webhook_secret() -> str | None:
    value = (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    return value or None


def paid_channel_configured() -> bool:
    return bool(bot_token() and paid_channel_id())


def free_channel_configured() -> bool:
    return bool(bot_token() and free_channel_id())


def send_channel_message(chat_id: str, text: str, *, http_post=None) -> dict[str, Any]:
    token = bot_token()
    if not token:
        return {"ok": False, "skipped": True, "error": "TELEGRAM_BOT_TOKEN is not set"}
    if not chat_id:
        return {"ok": False, "skipped": True, "error": "chat id is not set"}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "skipped": True, "error": "empty body"}
    return _call(
        "sendMessage",
        {"chat_id": chat_id, "text": body[:4000], "disable_web_page_preview": True},
        http_post=http_post,
    )


def create_invite(name: str, *, http_post=None) -> dict[str, Any]:
    """Join-request link so the webhook learns the Telegram user id before entry."""
    channel = paid_channel_id()
    if not paid_channel_configured() or not channel:
        return {
            "ok": False,
            "skipped": True,
            "error": "Paid Telegram channel is not configured (TELEGRAM_BOT_TOKEN + TELEGRAM_PAID_CHANNEL_ID)",
        }
    result = _call(
        "createChatInviteLink",
        {
            "chat_id": channel,
            "name": (name or "subscriber")[:32],
            "creates_join_request": True,
        },
        http_post=http_post,
    )
    link = ((result.get("result") or {}) if isinstance(result.get("result"), dict) else {}).get("invite_link")
    if result.get("ok") and link:
        result["invite_link"] = link
    return result


def revoke_invite(invite_link: str, *, http_post=None) -> dict[str, Any]:
    channel = paid_channel_id()
    if not invite_link or not channel or not bot_token():
        return {"ok": False, "skipped": True, "error": "nothing to revoke"}
    return _call(
        "revokeChatInviteLink",
        {"chat_id": channel, "invite_link": invite_link},
        http_post=http_post,
    )


def remove_member(user_id: int | str, *, http_post=None) -> dict[str, Any]:
    """Ban then unban so a canceled subscriber is removed and can rejoin later."""
    channel = paid_channel_id()
    if not channel or not bot_token() or user_id in (None, ""):
        return {"ok": False, "skipped": True, "error": "no telegram user to remove"}
    banned = _call(
        "banChatMember",
        {"chat_id": channel, "user_id": int(user_id), "revoke_messages": False},
        http_post=http_post,
    )
    if not banned.get("ok"):
        return banned
    unbanned = _call(
        "unbanChatMember",
        {"chat_id": channel, "user_id": int(user_id), "only_if_banned": True},
        http_post=http_post,
    )
    return {"ok": bool(unbanned.get("ok")), "banned": True, "unbanned": bool(unbanned.get("ok")), "error": unbanned.get("error")}


def approve_join(user_id: int | str, *, http_post=None) -> dict[str, Any]:
    channel = paid_channel_id()
    if not channel:
        return {"ok": False, "skipped": True, "error": "TELEGRAM_PAID_CHANNEL_ID is not set"}
    return _call(
        "approveChatJoinRequest",
        {"chat_id": channel, "user_id": int(user_id)},
        http_post=http_post,
    )


def decline_join(user_id: int | str, *, http_post=None) -> dict[str, Any]:
    channel = paid_channel_id()
    if not channel:
        return {"ok": False, "skipped": True, "error": "TELEGRAM_PAID_CHANNEL_ID is not set"}
    return _call(
        "declineChatJoinRequest",
        {"chat_id": channel, "user_id": int(user_id)},
        http_post=http_post,
    )


def revoke_access(subscriber: dict[str, Any], *, http_post=None) -> dict[str, Any]:
    errors: list[str] = []
    link = subscriber.get("telegram_invite_link")
    user_id = subscriber.get("telegram_user_id")
    revoked = revoke_invite(str(link), http_post=http_post) if link else {"ok": True, "skipped": True}
    if revoked.get("error") and not revoked.get("skipped"):
        errors.append(str(revoked.get("error")))
    removed = remove_member(user_id, http_post=http_post) if user_id else {"ok": True, "skipped": True}
    if removed.get("error") and not removed.get("skipped"):
        errors.append(str(removed.get("error")))
    return {"ok": not errors, "revoked": revoked, "removed": removed, "errors": errors}


def _call(method: str, payload: dict[str, Any], *, http_post=None) -> dict[str, Any]:
    token = bot_token()
    if not token:
        return {"ok": False, "skipped": True, "error": "TELEGRAM_BOT_TOKEN is not set"}
    post = http_post or requests.post
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = post(url, json=payload, timeout=20)
    except requests.RequestException as exc:
        logger.warning("telegram %s failed: %s", method, exc)
        return {"ok": False, "error": str(exc)[:300]}
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}
    if response.status_code >= 400 or not (isinstance(body, dict) and body.get("ok")):
        description = body.get("description") if isinstance(body, dict) else None
        logger.warning("telegram %s status %s: %s", method, response.status_code, description)
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": str(description or "Telegram request failed")[:300],
        }
    return {"ok": True, "result": body.get("result")}
