"""Collect BUY/SELL pulses from a desk snapshot and push Telegram / WhatsApp alerts.

Gold Setup (Sure Winner MAX/HIGH) and Golden Flow prints are part of this
personal-chat pulse. They are collected first so the per-cycle send cap cannot
drop them behind a long RIP tape. Each of those desks sends one message per
symbol: the strongest contract is the headline and up to two others are extra
lines. SOLID cards, non-golden flow, and chart triangles stay off this path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from odte_scanner.alerts.telegram import configured as telegram_configured
from odte_scanner.alerts.telegram import send_telegram_text
from odte_scanner.alerts.whatsapp import configured as whatsapp_configured
from odte_scanner.alerts.whatsapp import send_whatsapp_text
from odte_scanner.trading.golden import is_golden_unusual_flow, is_sure_winner_gold

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


_GROUPED_DESKS = frozenset({"Gold Setup", "Golden Flow"})


def _load_seen(path: Path) -> tuple[set[str], dict[str, list[float]]]:
    # Only fall back to legacy when using the default desk seen path
    candidates = [path]
    if path.resolve() == DEFAULT_SEEN.resolve() and LEGACY_SEEN.exists():
        candidates.append(LEGACY_SEEN)
    for p in candidates:
        if p is None or not p.exists():
            continue
        from odte_scanner.json_util import load_json_state

        raw = load_json_state(p)
        if isinstance(raw, list):
            return {str(x) for x in raw}, {}
        if isinstance(raw, dict):
            keys = raw.get("keys") or raw.get("seen") or []
            strengths: dict[str, list[float]] = {}
            raw_strengths = raw.get("strengths") or {}
            if isinstance(raw_strengths, dict):
                for key, value in raw_strengths.items():
                    if isinstance(value, (list, tuple)):
                        strengths[str(key)] = [float(part) for part in value]
            return {str(x) for x in keys}, strengths
    return set(), {}


def _save_seen(
    path: Path,
    keys: set[str],
    strengths: dict[str, list[float]] | None = None,
    *,
    keep: int = 400,
) -> None:
    from odte_scanner.json_util import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keys)[-keep:]
    kept = set(ordered)
    stored = {
        key: value
        for key, value in (strengths or {}).items()
        if key in kept and isinstance(value, list)
    }
    payload = json.dumps({"keys": ordered, "strengths": stored}, indent=2)
    atomic_write_text(path, payload)
    # Keep legacy file in sync for older caches
    try:
        LEGACY_SEEN.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(LEGACY_SEEN, payload)
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


_OCC_RE = re.compile(r"([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})")


def _occ_parts(contract: Any) -> dict[str, Any]:
    """Read right, strike, and expiry from an OCC symbol when the row omitted them."""
    raw = str(contract or "").upper().replace("O:", "").replace(" ", "")
    match = _OCC_RE.search(raw)
    if not match:
        return {}
    _root, yymmdd, right, strike_raw = match.groups()
    return {
        "right": right,
        "expiry": f"{2000 + int(yymmdd[:2]):04d}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "strike": int(strike_raw) / 1000.0,
    }


def _resolved_contract(row: dict[str, Any]) -> tuple[str, Any, str]:
    occ = _occ_parts(row.get("contract") or row.get("occ"))
    right = str(row.get("right") or "").strip().upper()[:1]
    if right not in {"C", "P"}:
        right = str(occ.get("right") or "")
    strike = row.get("strike")
    if strike is None or strike == "":
        strike = occ.get("strike")
    expiry = str(row.get("expiry") or occ.get("expiry") or "")
    return right, strike, expiry


def _contract_circle(row: dict[str, Any]) -> str:
    """Green for calls and bullish prints, red for puts and bearish prints."""
    right, _strike, _expiry = _resolved_contract(row)
    if right == "P":
        return "🔴"
    if right == "C":
        return "🟢"
    sentiment = str(row.get("sentiment") or "").strip().lower()
    if sentiment in {"bearish", "short"}:
        return "🔴"
    return "🟢"


def _compact_expiry(raw: Any) -> str:
    text = str(raw or "")[:10]
    parts = text.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[1])}/{int(parts[2])}"
    return text


def _compact_strike(strike: Any, right: str) -> str:
    if strike is None or strike == "":
        return right
    try:
        number = float(strike)
    except (TypeError, ValueError):
        base = str(strike)
    else:
        base = str(int(number)) if number.is_integer() else f"{number:g}"
    return f"{base}{right}" if right else base


def _signal_headline(row: dict[str, Any], desk: str) -> str | None:
    """Plain-text first line for Gold Setup and Golden Flow. No Markdown."""
    if desk not in {"Gold Setup", "Golden Flow"}:
        return None
    right, strike, expiry = _resolved_contract(row)
    parts: list[str] = []
    if desk == "Gold Setup":
        parts.extend(["⭐", _contract_circle(row), "SURE WINNER"])
    else:
        parts.extend([_contract_circle(row), "GOLDEN FLOW"])
    parts.append(str(row.get("symbol") or "?").upper())
    strike_s = _compact_strike(strike, right)
    if strike_s:
        parts.append(strike_s)
    expiry_s = _compact_expiry(expiry)
    if expiry_s:
        parts.append(expiry_s)
    if desk == "Gold Setup":
        tier = str(row.get("confidence_tier") or "").strip().upper()
        if tier:
            parts.append(tier)
    return " ".join(parts)


def _num(value: Any) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _contract_strength(row: dict[str, Any]) -> list[float]:
    """Higher is a stronger headline. Tier, then score, then flow, then premium."""
    tier = str(row.get("confidence_tier") or "").strip().upper()
    tier_rank = {"MAX": 2.0, "HIGH": 1.0}.get(tier, 0.0)
    score = _num(row.get("score") if row.get("score") is not None else row.get("ensemble_score"))
    flow = abs(_num(row.get("flow_score")))
    premium = _num(row.get("premium_notional"))
    volume = _num(row.get("volume"))
    return [tier_rank, score, flow, premium, volume]


def _strength_gt(current: list[float] | None, previous: list[float] | None) -> bool:
    cur = [float(part) for part in (current or [])]
    prev = [float(part) for part in (previous or [])]
    width = max(len(cur), len(prev))
    cur.extend([0.0] * (width - len(cur)))
    prev.extend([0.0] * (width - len(prev)))
    return cur > prev


def _session_day() -> str:
    from odte_scanner.time_cst import CT

    return datetime.now(CT).strftime("%Y-%m-%d")


def _session_key(alert: dict[str, Any]) -> str:
    return f"session|{_session_day()}|{alert['desk']}|{alert['side']}|{alert['symbol']}"


def _contract_id(row: dict[str, Any]) -> str:
    occ = str(row.get("contract") or row.get("occ") or "").upper()
    if occ:
        return occ
    right, strike, expiry = _resolved_contract(row)
    return f"{strike}:{expiry}:{right}"


def _also_line(row: dict[str, Any]) -> str:
    right, strike, expiry = _resolved_contract(row)
    strike_s = _compact_strike(strike, right)
    expiry_s = _compact_expiry(expiry)
    ask = row.get("ask")
    if ask is None and row.get("mid") is not None:
        ask = row.get("mid")
    ask_s = f"${float(ask):.2f}" if ask is not None else "—"
    bits = [_contract_circle(row)]
    if strike_s:
        bits.append(strike_s)
    if expiry_s:
        bits.append(expiry_s)
    return f"Also {' '.join(bits)} · ask {ask_s}"


def _fmt_alert(
    row: dict[str, Any],
    side: str,
    desk: str,
    extras: list[dict[str, Any]] | None = None,
) -> str:
    sym = str(row.get("symbol") or "?").upper()
    headline = _signal_headline(row, desk)
    if headline:
        right, strike, exp = _resolved_contract(row)
        exp = str(exp or "")[:10]
    else:
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
    if headline:
        lines = [headline]
    else:
        emoji = "🟢" if side == "BUY" else "🔴"
        lines = [f"{emoji} {side} · {desk}"]
    lines.extend(
        [
            f"{sym} {strike_s} · exp {exp or '—'}",
            f"Ask {ask_s} · spot {spot_s}",
        ]
    )
    for extra in extras or []:
        if isinstance(extra, dict):
            lines.append(_also_line(extra))
    if when:
        lines.append(f"Asked {when}")
    if detail:
        lines.append(detail)
    lines.append("Signal Desk · stockscanner")
    return "\n".join(lines)


def _option_flow(snapshot: dict[str, Any]) -> dict[str, Any]:
    echo = snapshot.get("echo") if isinstance(snapshot.get("echo"), dict) else {}
    flow = echo.get("option_flow") if isinstance(echo.get("option_flow"), dict) else None
    if flow is None and isinstance(snapshot.get("option_flow"), dict):
        flow = snapshot["option_flow"]
    return flow or {}


def _golden_flow_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Golden unusual-flow prints only. Chart-pattern triangles are not flow."""
    flow = _option_flow(snapshot)
    rows = [row for row in (flow.get("golden") or []) if isinstance(row, dict)]
    if not rows:
        rows = [row for row in (flow.get("prints") or []) if isinstance(row, dict)]
    return [row for row in rows if is_golden_unusual_flow(row)]


def collect_trade_alerts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror the UI Buy/Sell Now pulse set, plus Gold Setup and Golden Flow.

    Gold and golden rows are first. ``dispatch_snapshot_alerts`` sends at most
    eight new messages, and a RIP burst would otherwise use every slot.
    """
    out: list[dict[str, Any]] = []
    seen_local: set[str] = set()

    def push(row: dict[str, Any] | None, side: str, desk: str, *, force: bool = False) -> None:
        if not isinstance(row, dict):
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        act = str(
            row.get("alert_action") or row.get("desk_action") or row.get("action") or side
        ).upper()
        if not force:
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
        if side == "BUY":
            from odte_scanner.options.quote_sanity import quote_bundle_ok

            if not quote_bundle_ok(row):
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

    sure = snapshot.get("sure_winners") if isinstance(snapshot.get("sure_winners"), dict) else {}
    for raw in list(sure.get("winners") or []) + list(sure.get("buy_now") or []):
        if not isinstance(raw, dict) or not is_sure_winner_gold(raw):
            continue
        row = dict(raw)
        row["action"] = row.get("action") or "SURE_WINNER"
        tier = str(row.get("confidence_tier") or "").strip().upper()
        if tier and tier not in str(row.get("detail") or ""):
            prefix = f"{tier} Gold Setup"
            row["detail"] = f"{prefix} · {row['detail']}" if row.get("detail") else prefix
        push(row, "BUY", "Gold Setup", force=True)

    for raw in _golden_flow_rows(snapshot):
        row = dict(raw)
        row["action"] = "GOLDEN_FLOW"
        if row.get("ask") is None and row.get("mid") is not None:
            row["ask"] = row["mid"]
        if not row.get("detail"):
            premium = row.get("premium_notional")
            try:
                row["detail"] = f"Golden flow · premium ${float(premium):,.0f}"
            except (TypeError, ValueError):
                row["detail"] = "Golden flow"
        push(row, "BUY", "Golden Flow", force=True)

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

    return _collapse_symbol_alerts(out)


def _collapse_symbol_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Gold Setup / Golden Flow message per symbol. Headline is the strongest contract."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[Any] = []
    for alert in alerts:
        if alert.get("desk") not in _GROUPED_DESKS:
            order.append(alert)
            continue
        bucket = (str(alert.get("desk")), str(alert.get("symbol")))
        if bucket not in grouped:
            grouped[bucket] = []
            order.append(bucket)
        grouped[bucket].append(alert)

    collapsed: list[dict[str, Any]] = []
    for item in order:
        if not isinstance(item, tuple):
            collapsed.append(item)
            continue
        by_contract: dict[str, dict[str, Any]] = {}
        for alert in grouped[item]:
            row = alert.get("row") if isinstance(alert.get("row"), dict) else {}
            cid = _contract_id(row)
            prev = by_contract.get(cid)
            if prev is None or _strength_gt(_contract_strength(row), _contract_strength(prev.get("row") or {})):
                by_contract[cid] = alert
        ranked = sorted(
            by_contract.values(),
            key=lambda alert: _contract_strength(alert.get("row") or {}),
            reverse=True,
        )
        primary = ranked[0]
        extras = [alert.get("row") or {} for alert in ranked[1:3]]
        merged = dict(primary)
        merged["key"] = _session_key(primary)
        merged["grouped"] = True
        merged["strength"] = _contract_strength(primary.get("row") or {})
        merged["message"] = _fmt_alert(primary.get("row") or {}, primary["side"], primary["desk"], extras)
        collapsed.append(merged)
    return collapsed


def _mark_alert(alert: dict[str, Any], seen: set[str], strengths: dict[str, list[float]]) -> None:
    seen.add(alert["key"])
    if alert.get("grouped"):
        strengths[str(alert["key"])] = [float(part) for part in (alert.get("strength") or [])]


def _alert_is_fresh(alert: dict[str, Any], seen: set[str], strengths: dict[str, list[float]]) -> bool:
    key = alert["key"]
    if key not in seen:
        return True
    if not alert.get("grouped"):
        return False
    previous = strengths.get(key)
    if not previous:
        return False
    return _strength_gt(alert.get("strength"), previous)


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
    seen, strengths = _load_seen(path)
    providers = _provider_labels(bool(tg_cfg.get("ok")), wa_cfg if wa_enabled else {})

    # First-ever run with a channel: seed without spamming the whole board.
    # Do not seed when nothing can send — that would permanently swallow the
    # open board, and those keys would stay silent after secrets are added.
    prime_only = (os.environ.get("DESK_ALERTS_PRIME") or os.environ.get("WHATSAPP_ALERTS_PRIME") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not seen and not prime_only:
        if not any_channel:
            return {
                "ok": False,
                "configured": False,
                "primed": False,
                "seeded": 0,
                "sent": 0,
                "fresh": len(alerts),
                "skipped": True,
                "providers": providers,
                "error": "No alert channel secrets set (Telegram or WhatsApp)",
                "note": "Not seeded — this board can still alert once a channel is configured",
            }
        for a in alerts:
            _mark_alert(a, seen, strengths)
        _save_seen(path, seen, strengths)
        return {
            "ok": True,
            "configured": True,
            "primed": True,
            "seeded": len(alerts),
            "sent": 0,
            "providers": providers,
            "provider": providers[0] if providers else None,
            "note": "Seeded current board — next new BUY/SELL will alert",
        }

    fresh = [a for a in alerts if _alert_is_fresh(a, seen, strengths)]
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
                _mark_alert(a, seen, strengths)
            _save_seen(path, seen, strengths)
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
            _mark_alert(a, seen, strengths)
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

    _save_seen(path, seen, strengths)
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
