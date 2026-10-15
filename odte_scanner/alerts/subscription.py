"""Paid Telegram alerts for Gold Setup and Golden Flow, plus the free digest.

Personal desk alerts still use TELEGRAM_CHAT_ID. This module only posts to
TELEGRAM_PAID_CHANNEL_ID and TELEGRAM_FREE_CHANNEL_ID.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.billing.copy import DISCLAIMER, IMPERSONAL, NOT_A_TRADE
from odte_scanner.billing.telegram_access import (
    free_channel_configured,
    free_channel_id,
    paid_channel_configured,
    paid_channel_id,
    send_channel_message,
)
from odte_scanner.trading.golden import is_golden_unusual_flow, is_sure_winner_gold

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CT = ZoneInfo("America/Chicago")


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def seen_path() -> Path:
    raw = (os.environ.get("SUBSCRIPTION_ALERT_SEEN") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "outputs" / "subscription_alert_seen.json"


def digest_state_path() -> Path:
    raw = (os.environ.get("MORNING_SPARK_PATH") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "outputs" / "morning_spark.json"


def collect_subscription_signals(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Gold Setup (MAX/HIGH) and Golden Flow only. Same text for every subscriber."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    sure = snapshot.get("sure_winners") if isinstance(snapshot.get("sure_winners"), dict) else {}
    rows = list(sure.get("winners") or []) + list(sure.get("buy_now") or [])
    for row in rows:
        if not isinstance(row, dict) or not is_sure_winner_gold(row):
            continue
        key = _gold_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "key": key,
            "kind": "gold_setup",
            "symbol": str(row.get("symbol") or "").upper(),
            "message": format_gold_setup(row),
        })

    for row in _golden_flow_rows(snapshot):
        key = _flow_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "key": key,
            "kind": "golden_flow",
            "symbol": str(row.get("symbol") or "").upper(),
            "message": format_golden_flow(row),
        })
    return out


def format_gold_setup(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "?").upper()
    tier = str(row.get("confidence_tier") or "filtered").upper()
    lines = [
        "Gold Setup",
        f"Ticker: {symbol}",
        f"Trigger: equity setup cleared the {tier} filter",
        f"Context: {_contract_phrase(row)}. {NOT_A_TRADE} {IMPERSONAL}",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_golden_flow(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "?").upper()
    lines = [
        "Golden Flow",
        f"Ticker: {symbol}",
        "Trigger: options activity ranked in the golden tier on the chain snapshot",
        (
            f"Context: {_contract_phrase(row)}. Flow shows activity that stood out versus that chain. "
            f"It does not say what happens next. {IMPERSONAL}"
        ),
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_morning_spark(snapshot: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(CT)
    snap = snapshot if isinstance(snapshot, dict) else {}
    lines = [
        f"Morning Spark — {moment.strftime('%b')} {moment.day}",
        "Free daily digest. Real-time Gold Setup and Golden Flow alerts stay on the paid channel.",
        "",
        "Gold Setups on the last published board (delayed, not real-time):",
    ]
    golds = [row for row in collect_subscription_signals(snap) if row["kind"] == "gold_setup"]
    if golds:
        for item in golds[:5]:
            lines.append(f"• {item['symbol']} — { _one_line_context(item['message']) }")
    else:
        lines.append("• None cleared the filter on the last published board.")
    lines.append("")
    lines.append("Earnings on the watchlist:")
    earnings = _earnings_lines(snap)
    lines.extend(earnings or ["• None listed on the last board."])
    lines.append("")
    lines.append("Movers on the last snapshot:")
    movers = _mover_lines(snap)
    lines.extend(movers or ["• No quote changes on the last board."])
    lines.append("")
    lines.append("No card is required for this digest.")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def dispatch_subscription_alerts(
    snapshot: dict[str, Any],
    *,
    seen: Path | None = None,
    dry_run: bool = False,
    max_send: int = 8,
    http_post=None,
) -> dict[str, Any]:
    if _env_flag("SKIP_SUBSCRIPTION_ALERTS") or (os.environ.get("SUBSCRIPTION_ALERTS_ENABLED") or "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return {"ok": False, "skipped": True, "reason": "subscription alerts disabled"}
    signals = collect_subscription_signals(snapshot)
    path = Path(seen) if seen else seen_path()
    already = _load_seen(path)
    configured = paid_channel_configured()
    if not already and signals and not _env_flag("SUBSCRIPTION_ALERTS_SEND_EXISTING"):
        _save_seen(path, already | {item["key"] for item in signals})
        return {
            "ok": True,
            "primed": True,
            "seeded": len(signals),
            "sent": 0,
            "configured": configured,
            "note": "Seeded the current board. The next new Gold Setup or Golden Flow will post.",
        }
    fresh = [item for item in signals if item["key"] not in already]
    if not fresh:
        return {"ok": True, "sent": 0, "configured": configured, "candidates": len(signals), "note": "no new signals"}
    if not configured or dry_run:
        if dry_run:
            _save_seen(path, already | {item["key"] for item in fresh})
        return {
            "ok": bool(dry_run),
            "dry_run": dry_run,
            "configured": configured,
            "sent": 0,
            "fresh": len(fresh),
            "would_send": [item["message"] for item in fresh[:max_send]],
            "skipped": not configured,
            "error": None if configured else "TELEGRAM_PAID_CHANNEL_ID is not set",
        }
    channel = paid_channel_id() or ""
    sent: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in fresh[: max(1, int(max_send))]:
        result = send_channel_message(channel, item["message"], http_post=http_post)
        if result.get("ok"):
            already.add(item["key"])
            sent.append({"key": item["key"], "symbol": item["symbol"], "kind": item["kind"]})
        else:
            errors.append(str(result.get("error") or "send failed"))
    _save_seen(path, already)
    return {
        "ok": bool(sent) or not errors,
        "configured": True,
        "sent": len(sent),
        "results": sent,
        "fresh": len(fresh),
        "errors": errors[:8],
    }


def in_digest_window(now: datetime | None = None) -> bool:
    moment = (now or datetime.now(timezone.utc)).astimezone(CT)
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return (7 * 60 + 45) <= minutes < (12 * 60)


def publish_morning_digest(
    snapshot: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
    http_post=None,
) -> dict[str, Any]:
    if not free_channel_configured():
        return {"ok": False, "skipped": True, "reason": "TELEGRAM_FREE_CHANNEL_ID is not set"}
    moment = now or datetime.now(timezone.utc)
    if not force and not in_digest_window(moment):
        return {"ok": True, "skipped": True, "reason": "outside the weekday morning window"}
    state_path = digest_state_path()
    state = _load_json(state_path)
    day = moment.astimezone(CT).date().isoformat()
    if not force and state.get("sent_on") == day:
        return {"ok": True, "skipped": True, "reason": "already sent today", "sent_on": day}
    snap = snapshot if isinstance(snapshot, dict) else _load_json(ROOT / "outputs" / "last_api_snapshot.json")
    text = format_morning_spark(snap, now=moment)
    channel = free_channel_id() or ""
    result = send_channel_message(channel, text, http_post=http_post)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "skipped": bool(result.get("skipped"))}
    state["sent_on"] = day
    state["text"] = text
    _write_json(state_path, state)
    return {"ok": True, "sent": 1, "sent_on": day, "channel": channel}


def maybe_publish_morning_digest(**kwargs: Any) -> dict[str, Any]:
    try:
        return publish_morning_digest(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("morning digest failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _golden_flow_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    echo = snapshot.get("echo") if isinstance(snapshot.get("echo"), dict) else {}
    flow = echo.get("option_flow") if isinstance(echo.get("option_flow"), dict) else None
    if flow is None and isinstance(snapshot.get("option_flow"), dict):
        flow = snapshot["option_flow"]
    flow = flow or {}
    rows = [row for row in (flow.get("golden") or []) if isinstance(row, dict)]
    if not rows:
        rows = [row for row in (flow.get("prints") or []) if isinstance(row, dict) and is_golden_unusual_flow(row)]
    return [row for row in rows if is_golden_unusual_flow(row)]


def _contract_phrase(row: dict[str, Any]) -> str:
    right = str(row.get("right") or "C").upper()
    side = "put" if right.startswith("P") else "call"
    strike = row.get("strike")
    expiry = str(row.get("expiry") or "")[:10]
    bits = [side]
    if strike is not None and strike != "":
        bits.append(_num(strike))
    if expiry:
        bits.append(f"exp {expiry}")
    return " ".join(bits)


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _gold_key(row: dict[str, Any]) -> str:
    signaled = str(row.get("signaled_at") or row.get("signaled_at_cst") or "")[:10]
    return "|".join([
        "gold",
        str(row.get("symbol") or "").upper(),
        str(row.get("contract") or ""),
        str(row.get("expiry") or "")[:10],
        _num(row.get("strike")) if row.get("strike") is not None else "",
        str(row.get("confidence_tier") or "").upper(),
        signaled,
    ])


def _flow_key(row: dict[str, Any]) -> str:
    return "|".join([
        "flow",
        str(row.get("symbol") or "").upper(),
        str(row.get("contract") or ""),
        str(row.get("expiry") or "")[:10],
        _num(row.get("strike")) if row.get("strike") is not None else "",
        str(row.get("right") or "").upper(),
    ])


def _one_line_context(message: str) -> str:
    for line in message.splitlines():
        if line.startswith("Context:"):
            return line.replace("Context:", "").strip().split(".")[0]
    return "filtered setup"


def _earnings_lines(snapshot: dict[str, Any]) -> list[str]:
    challenge = snapshot.get("challenge") if isinstance(snapshot.get("challenge"), dict) else {}
    rows = challenge.get("earnings_watch") if isinstance(challenge.get("earnings_watch"), list) else []
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        when = row.get("next_earnings") or row.get("earnings_label") or row.get("earnings_date") or ""
        days = row.get("days_to_earnings")
        extra = f"{days}d" if days is not None else str(when)
        lines.append(f"• {symbol} — {extra}".rstrip(" —"))
        if len(lines) >= 5:
            break
    return lines


def _mover_lines(snapshot: dict[str, Any]) -> list[str]:
    watch = snapshot.get("watch") if isinstance(snapshot.get("watch"), dict) else {}
    quotes = watch.get("quotes") if isinstance(watch.get("quotes"), dict) else {}
    ranked: list[tuple[float, str]] = []
    for symbol, quote in quotes.items():
        if not isinstance(quote, dict):
            continue
        raw = quote.get("change_pct")
        if raw is None:
            raw = quote.get("session_change_pct")
        try:
            change = float(raw)
        except (TypeError, ValueError):
            continue
        ranked.append((change, str(symbol).upper()))
    ranked.sort(key=lambda item: abs(item[0]), reverse=True)
    lines = []
    for change, symbol in ranked[:3]:
        lines.append(f"• {symbol} {change:+.1f}%")
    return lines


def _load_seen(path: Path) -> set[str]:
    data = _load_json(path)
    keys = data.get("keys") if isinstance(data, dict) else None
    if isinstance(keys, list):
        return {str(item) for item in keys}
    return set()


def _save_seen(path: Path, keys: set[str]) -> None:
    _write_json(path, {"keys": sorted(keys)[-400:]})


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
