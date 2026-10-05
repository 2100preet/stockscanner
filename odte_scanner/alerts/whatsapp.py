"""WhatsApp outbound alerts (Twilio or Meta Cloud API).

Env (Twilio — preferred for personal sandbox):
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_WHATSAPP_FROM   e.g. whatsapp:+14155238886
  WHATSAPP_TO            e.g. whatsapp:+15551234567  (or +15551234567)

Env (Meta Cloud API alternative):
  WHATSAPP_TOKEN
  WHATSAPP_PHONE_NUMBER_ID
  WHATSAPP_TO            E.164 phone, e.g. 15551234567

Never commit tokens. Set as GitHub Actions secrets.
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


def _normalize_to(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.lower().startswith("whatsapp:"):
        return s if s.lower().startswith("whatsapp:") else f"whatsapp:{s}"
    if s.startswith("+"):
        return f"whatsapp:{s}"
    # bare digits → assume E.164 without +
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        return f"whatsapp:+{digits}"
    return None


def _normalize_to_meta(raw: str | None) -> str | None:
    """Meta wants digits only (country code + number, no +)."""
    if not raw:
        return None
    s = raw.strip()
    if s.lower().startswith("whatsapp:"):
        s = s.split(":", 1)[1]
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits or None


def configured() -> dict[str, Any]:
    """Return which WhatsApp backend is ready (no network)."""
    twilio = bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_WHATSAPP_FROM")
        and _normalize_to(_env("WHATSAPP_TO", "TWILIO_WHATSAPP_TO"))
    )
    meta = bool(
        _env("WHATSAPP_TOKEN", "WHATSAPP_CLOUD_TOKEN")
        and _env("WHATSAPP_PHONE_NUMBER_ID")
        and _normalize_to_meta(_env("WHATSAPP_TO"))
    )
    return {
        "ok": twilio or meta,
        "twilio": twilio,
        "meta": meta,
        "to_set": bool(_env("WHATSAPP_TO", "TWILIO_WHATSAPP_TO")),
    }


def send_whatsapp_text(
    body: str,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Send a plain text WhatsApp message. Prefers Twilio, else Meta Cloud."""
    text = (body or "").strip()
    if not text:
        return {"ok": False, "skipped": True, "error": "empty body"}

    cfg = configured()
    if not cfg["ok"]:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "error": "WhatsApp not configured (set TWILIO_* or WHATSAPP_TOKEN secrets)",
        }

    if cfg["twilio"]:
        return _send_twilio(text, timeout=timeout)
    return _send_meta(text, timeout=timeout)


def _send_twilio(body: str, *, timeout: float) -> dict[str, Any]:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_n = _env("TWILIO_WHATSAPP_FROM")
    to_n = _normalize_to(_env("WHATSAPP_TO", "TWILIO_WHATSAPP_TO"))
    if not (sid and token and from_n and to_n):
        return {"ok": False, "configured": False, "skipped": True, "error": "twilio env incomplete"}
    if not from_n.lower().startswith("whatsapp:"):
        from_n = f"whatsapp:{from_n}" if from_n.startswith("+") else f"whatsapp:+{from_n}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        r = requests.post(
            url,
            data={"From": from_n, "To": to_n, "Body": body[:1500]},
            auth=(sid, token),
            timeout=timeout,
        )
        if r.status_code >= 400:
            logger.warning("twilio whatsapp failed %s: %s", r.status_code, r.text[:300])
            return {
                "ok": False,
                "configured": True,
                "provider": "twilio",
                "status_code": r.status_code,
                "error": r.text[:400],
            }
        payload = r.json() if r.content else {}
        return {
            "ok": True,
            "configured": True,
            "provider": "twilio",
            "sid": payload.get("sid"),
            "status": payload.get("status"),
            "status_code": r.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("twilio whatsapp exception: %s", exc)
        return {"ok": False, "configured": True, "provider": "twilio", "error": str(exc)}


def _send_meta(body: str, *, timeout: float) -> dict[str, Any]:
    token = _env("WHATSAPP_TOKEN", "WHATSAPP_CLOUD_TOKEN")
    phone_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    to_n = _normalize_to_meta(_env("WHATSAPP_TO"))
    if not (token and phone_id and to_n):
        return {"ok": False, "configured": False, "skipped": True, "error": "meta env incomplete"}

    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to_n,
                "type": "text",
                "text": {"preview_url": False, "body": body[:1500]},
            },
            timeout=timeout,
        )
        if r.status_code >= 400:
            logger.warning("meta whatsapp failed %s: %s", r.status_code, r.text[:300])
            return {
                "ok": False,
                "configured": True,
                "provider": "meta",
                "status_code": r.status_code,
                "error": r.text[:400],
            }
        payload = r.json() if r.content else {}
        msg_id = None
        try:
            msg_id = (payload.get("messages") or [{}])[0].get("id")
        except Exception:  # noqa: BLE001
            msg_id = None
        return {
            "ok": True,
            "configured": True,
            "provider": "meta",
            "message_id": msg_id,
            "status_code": r.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("meta whatsapp exception: %s", exc)
        return {"ok": False, "configured": True, "provider": "meta", "error": str(exc)}
