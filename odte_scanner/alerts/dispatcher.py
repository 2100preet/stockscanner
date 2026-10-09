"""Collect BUY/SELL pulses from a desk snapshot and push Telegram / WhatsApp alerts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from odte_scanner.alerts.telegram import configured as telegram_configured
from odte_scanner.alerts.telegram import send_telegram_text
from odte_scanner.alerts.whatsapp import configured as whatsapp_configured
from odte_scanner.alerts.whatsapp import send_whatsapp_text

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEN = ROOT / "outputs" / "desk_alert_seen.json"
LEGACY_SEEN = ROOT / "outputs" / "whatsapp_alert_seen.json"

_BUY_ACTIONS = {
    "BUY_NOW",
    "BUY_RIP",
    "BUY_BEAUTY",
    "BUY_LEVEL",
    "BUY_PUT",
    "PUT_NOW",
    "CALL_NOW",
    "ENTRY",
    "RADAR_HOT",
}
_SELL_ACTIONS = {
    "SELL_NOW",
    "EXIT",
    "SELL_PUT",
    "SELL_CALL",
}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _load_seen(path: Path) -> set[str]:
    # Only fall back to legacy when using the default desk seen path
    candidates = [path]
    if path.resolve() == DEFAULT_SEEN.resolve() and LEGACY_SEEN.exists():
        candidates.append(LEGACY_SEEN)
    for p in candidates:
        if p is None or not p.exists():
            continue
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, list):
                return {str(x) for x in raw}
            if isinstance(raw, dict):
                keys = raw.get("keys") or raw.get("seen") or []
                return {str(x) for x in keys}
        except Exception as exc:  # noqa: BLE001
            logger.debug("alert seen load failed %s: %s", p, exc)
    return set()


def _save_seen(path: Path, keys: set[str], *, keep: int = 400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keys)[-keep:]
    path.write_text(json.dumps({"keys": ordered}, indent=2))
    # Keep legacy file in sync for older caches
    try:
        LEGACY_SEEN.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_SEEN.write_text(json.dumps({"keys": ordered}, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _row_key(row: dict[str, Any], side: str, desk: str) -> str:
    sym = str(row.get("symbol") or "").upper()
    occ = str(row.get("contract") or row.get("occ") or "").upper()
    act = str(row.get("action") or row.get("alert_action") or side).upper()
    strike = row.get("strike")
    exp = str(row.get("expiry") or "")[:10]
    signaled = str(row.get("signaled_at") or row.get("signaled_at_cst") or "")[:19]
    return f"{desk}|{side}|{sym}|{act}|{occ or f'{strike}:{exp}'}|{signaled}"


def _fmt_alert(row: dict[str, Any], side: str, desk: str) -> str:
    sym = str(row.get("symbol") or "?").upper()
    right = str(row.get("right") or "C").upper()[:1]
    strike = row.get("strike")
    exp = str(row.get("expiry") or "")[:10]
    ask = row.get("ask")
    spot = row.get("spot") or row.get("last")
    when = row.get("signaled_at_cst") or ""
    detail = str(row.get("detail") or row.get("headline") or row.get("reason") or "")[:180]
    strike_s = f"{strike}{right}" if strike is not None else right
    ask_s = f"${float(ask):.2f}" if ask is not None else "—"
    spot_s = f"${float(spot):.2f}" if spot is not None else "—"
    emoji = "🟢" if side == "BUY" else "🔴"
    lines = [
        f"{emoji} {side} · {desk}",
        f"{sym} {strike_s} · exp {exp or '—'}",
        f"Ask {ask_s} · spot {spot_s}",
    ]
    if when:
        lines.append(f"Asked {when}")
    if detail:
        lines.append(detail)
    lines.append("Signal Desk · stockscanner")
    return "\n".join(lines)


def collect_trade_alerts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror the UI Buy/Sell Now pulse set for outbound delivery."""
    out: list[dict[str, Any]] = []
    seen_local: set[str] = set()

    def push(row: dict[str, Any] | None, side: str, desk: str) -> None:
        if not isinstance(row, dict):
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        act = str(
            row.get("alert_action") or row.get("desk_action") or row.get("action") or side
        ).upper()
        if side == "BUY" and act not in _BUY_ACTIONS and "BUY" not in act and act not in {
            "PUT_NOW",
            "CALL_NOW",
            "ENTRY",
            "RADAR_HOT",
        }:
            if not any(x in act for x in ("BUY", "PUT_NOW", "CALL_NOW", "ENTRY", "HOT")):
                return
        if side == "SELL" and act not in _SELL_ACTIONS and "SELL" not in act and act != "EXIT":
            if "EXIT" not in act and "SELL" not in act:
                return
        key = _row_key(row, side, desk)
        if key in seen_local:
            return
        seen_local.add(key)
        out.append(
            {
                "key": key,
                "side": side,
                "desk": desk,
                "symbol": sym,
                "action": act,
                "contract": row.get("contract"),
                "strike": row.get("strike"),
                "expiry": row.get("expiry"),
                "ask": row.get("ask"),
                "message": _fmt_alert(row, side, desk),
                "row": row,
            }
        )

    acts = snapshot.get("actions") or {}
    for r in acts.get("buy_now") or []:
        push(r, "BUY", "Options")
    for r in acts.get("sell_now") or []:
        push(r, "SELL", "Options")

    lot = snapshot.get("lottery") or {}
    for r in lot.get("buy_now") or []:
        push(r, "BUY", "Explosive")
    for r in lot.get("sell_now") or []:
        push(r, "SELL", "Explosive")

    rip = snapshot.get("rip_radar") or {}
    for r in rip.get("buy_rip") or rip.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_RIP"}, "BUY", "RIP/META")

    beauty = snapshot.get("beauty_monthly") or {}
    for r in beauty.get("buy_beauty") or beauty.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_BEAUTY"}, "BUY", "Beauty")

    levels = snapshot.get("level_watch") or {}
    for r in levels.get("buy_level") or levels.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_LEVEL"}, "BUY", "Levels")

    ic = snapshot.get("spx_credit") or {}
    for r in ic.get("sell_credit") or ic.get("buy_now") or []:
        push(
            {
                **(r or {}),
                "action": (r or {}).get("alert_action") or "BUY_NOW",
                "ask": (r or {}).get("credit") or (r or {}).get("ask"),
            },
            "BUY",
            "SPX Credit",
        )
    for r in ic.get("exit_now") or ic.get("sell_now") or []:
        push(
            {
                **(r or {}),
                "action": (r or {}).get("alert_action") or "SELL_NOW",
                "entry_ask": (r or {}).get("entry_ask") or (r or {}).get("credit"),
                "bid": (r or {}).get("debit_to_close") or (r or {}).get("bid"),
                "ask": (r or {}).get("debit_to_close") or (r or {}).get("ask"),
            },
            "SELL",
            "SPX Credit",
        )

    ml = (snapshot.get("ml6") or {}).get("actions") or snapshot.get("ml6") or {}
    for r in ml.get("buy_now") or []:
        push(r, "BUY", "ML6")
    for r in ml.get("sell_now") or []:
        push(r, "SELL", "ML6")

    ch = snapshot.get("challenge") or {}
    for r in ch.get("entry") or ch.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_NOW"}, "BUY", "Challenge")
    for r in ch.get("exit") or ch.get("sell_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "SELL_NOW"}, "SELL", "Challenge")

    o1k = snapshot.get("odte_1k") or {}
    for r in o1k.get("put_now") or o1k.get("entry") or o1k.get("in") or []:
        push(
            {
                **(r or {}),
                "action": (r or {}).get("alert_action") or "BUY_NOW",
                "right": (r or {}).get("right") or "P",
            },
            "BUY",
            "0DTE $1K",
        )
    for r in o1k.get("exit_now") or o1k.get("exit") or o1k.get("out") or []:
        push(
            {**(r or {}), "action": (r or {}).get("alert_action") or "SELL_NOW"},
            "SELL",
            "0DTE $1K",
        )

    return out


def _provider_labels(tg_ok: bool, wa_cfg: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if tg_ok:
        labels.append("telegram")
    if wa_cfg.get("ok"):
        if wa_cfg.get("callmebot"):
            labels.append("callmebot")
        elif wa_cfg.get("twilio"):
            labels.append("twilio")
        elif wa_cfg.get("meta"):
            labels.append("meta")
        else:
            labels.append("whatsapp")
    return labels


def _send_all_channels(message: str) -> tuple[list[str], list[str]]:
    """Send to every configured channel. Returns (ok_providers, errors)."""
    ok_providers: list[str] = []
    errors: list[str] = []

    tg = telegram_configured()
    if tg.get("ok"):
        res = send_telegram_text(message)
        if res.get("ok"):
            ok_providers.append("telegram")
        else:
            errors.append(f"telegram: {res.get('error') or res}")

    wa = whatsapp_configured()
    if wa.get("ok") and _env_flag("WHATSAPP_ALERTS_ENABLED", True):
        res = send_whatsapp_text(message)
        if res.get("ok"):
            ok_providers.append(str(res.get("provider") or "whatsapp"))
        else:
            errors.append(f"whatsapp: {res.get('error') or res}")

    return ok_providers, errors


def dispatch_snapshot_alerts(
    snapshot: dict[str, Any],
    *,
    seen_path: str | Path | None = None,
    dry_run: bool = False,
    max_send: int = 8,
) -> dict[str, Any]:
    """Send Telegram/WhatsApp for *new* BUY/SELL pulses. No-op if none configured."""
    # Global pause
    if not _env_flag("DESK_ALERTS_ENABLED", True):
        return {"ok": False, "skipped": True, "reason": "DESK_ALERTS_ENABLED=0"}

    tg_cfg = telegram_configured()
    wa_cfg = whatsapp_configured()
    wa_enabled = _env_flag("WHATSAPP_ALERTS_ENABLED", True)
    any_channel = bool(tg_cfg.get("ok") or (wa_cfg.get("ok") and wa_enabled))

    alerts = collect_trade_alerts(snapshot)
    path = Path(seen_path) if seen_path else DEFAULT_SEEN
    seen = _load_seen(path)
    providers = _provider_labels(bool(tg_cfg.get("ok")), wa_cfg if wa_enabled else {})

    # First-ever run: seed without spamming the whole board
    prime_only = (os.environ.get("DESK_ALERTS_PRIME") or os.environ.get("WHATSAPP_ALERTS_PRIME") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not seen and not prime_only:
        for a in alerts:
            seen.add(a["key"])
        _save_seen(path, seen)
        return {
            "ok": True,
            "configured": any_channel,
            "primed": True,
            "seeded": len(alerts),
            "sent": 0,
            "providers": providers,
            "provider": providers[0] if providers else None,
            "note": "Seeded current board — next new BUY/SELL will alert",
        }

    fresh = [a for a in alerts if a["key"] not in seen]
    if not fresh:
        return {
            "ok": True,
            "configured": any_channel,
            "sent": 0,
            "candidates": len(alerts),
            "providers": providers,
            "note": "no new pulses",
        }

    if not any_channel or dry_run:
        if dry_run:
            for a in fresh:
                seen.add(a["key"])
            _save_seen(path, seen)
        return {
            "ok": bool(dry_run),
            "configured": any_channel,
            "dry_run": dry_run,
            "would_send": [a["message"] for a in fresh[:max_send]],
            "sent": 0,
            "fresh": len(fresh),
            "skipped": not any_channel,
            "providers": providers,
            "error": None if any_channel else "No alert channel secrets set (Telegram or WhatsApp)",
        }

    sent: list[dict[str, Any]] = []
    errors: list[str] = []
    for a in fresh[: max(1, int(max_send))]:
        ok_providers, errs = _send_all_channels(a["message"])
        errors.extend(errs)
        if ok_providers:
            seen.add(a["key"])
            sent.append(
                {
                    "key": a["key"],
                    "symbol": a["symbol"],
                    "side": a["side"],
                    "desk": a["desk"],
                    "providers": ok_providers,
                    "provider": ok_providers[0],
                }
            )
        # If every channel failed, leave unseen for retry

    _save_seen(path, seen)
    return {
        "ok": len(sent) > 0 or not errors,
        "configured": True,
        "sent": len(sent),
        "results": sent,
        "fresh": len(fresh),
        "errors": errors[:8],
        "providers": providers,
        "provider": providers[0] if providers else None,
    }
