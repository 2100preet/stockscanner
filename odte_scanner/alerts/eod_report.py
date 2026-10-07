"""End-of-day Telegram report at ~3:00 PM ET (cash close window for the desk).

Summarizes today's recommended buys/sells plus closed and open P&L for the day.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.alerts.telegram import configured as telegram_configured
from odte_scanner.alerts.telegram import send_telegram_text
from odte_scanner.time_cst import to_cst_label

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / "outputs" / "eod_report_sent.json"
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _day_key_ct(iso: str | None) -> str | None:
    dt = _parse_dt(iso)
    if not dt:
        return None
    return dt.astimezone(CT).date().isoformat()


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ("−" if v < 0 else "")
    return f"{sign}${abs(v):.2f}"


def _pct(v: float | None) -> str:
    if v is None:
        return ""
    return f" ({v:+.0f}%)"


def _trade_label(r: dict[str, Any]) -> str:
    name = r.get("trade_name") or r.get("name")
    if name:
        return str(name)
    sym = str(r.get("symbol") or "?").upper()
    right = str(r.get("right") or "C").upper()[:1]
    strike = r.get("strike")
    try:
        if strike is not None:
            return f"{sym} {float(strike):g}{right}"
    except (TypeError, ValueError):
        pass
    return f"{sym} {right}"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def in_eod_window(
    now: datetime | None = None,
    *,
    hour_et: int = 15,
    minute_et: int = 0,
    window_minutes: int = 20,
) -> bool:
    """True during the 3:00 PM ET send window on weekdays."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ET)
    if local.weekday() >= 5:
        return False
    start = local.replace(hour=hour_et, minute=minute_et, second=0, microsecond=0)
    elapsed = (local - start).total_seconds()
    return 0 <= elapsed < max(60, window_minutes * 60)


def _collect_day_buys(snapshot: dict[str, Any], day: str) -> list[dict[str, Any]]:
    """BUY NOW pulses recommended / entered on this CT day."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(row: dict[str, Any] | None, desk: str, *, require_day: bool = False) -> None:
        if not isinstance(row, dict):
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        when = row.get("signaled_at") or row.get("entered_at") or row.get("recommended_at")
        day_entry = row.get("day_entry") or _day_key_ct(when)
        # Live board buys are always listed at EOD; journal opens need day match.
        if require_day and day_entry != day and _day_key_ct(when) != day:
            return
        key = f"{desk}|{sym}|{row.get('contract') or row.get('strike')}"
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "symbol": sym,
                "desk": desk,
                "name": _trade_label(row),
                "ask": _as_float(row.get("ask") if row.get("ask") is not None else row.get("entry_ask")),
                "when": to_cst_label(when) if when else None,
            }
        )

    acts = snapshot.get("actions") or {}
    for r in acts.get("buy_now") or []:
        push(r, "Options")
    lot = snapshot.get("lottery") or {}
    for r in lot.get("buy_now") or []:
        push(r, "Explosive")
    rip = snapshot.get("rip_radar") or {}
    for r in rip.get("buy_rip") or rip.get("buy_now") or []:
        push(r, "RIP/META")
    beauty = snapshot.get("beauty_monthly") or {}
    for r in beauty.get("buy_beauty") or beauty.get("buy_now") or []:
        push(r, "Beauty")
    levels = snapshot.get("level_watch") or {}
    for r in levels.get("buy_level") or levels.get("buy_now") or []:
        push(r, "Levels")
    ml = (snapshot.get("ml6") or {}).get("actions") or snapshot.get("ml6") or {}
    for r in ml.get("buy_now") or []:
        push(r, "ML6")
    ch = snapshot.get("challenge") or {}
    for r in ch.get("entry") or ch.get("buy_now") or []:
        push(r, "Challenge")
    o1k = snapshot.get("odte_1k") or {}
    for r in o1k.get("put_now") or o1k.get("entry") or o1k.get("in") or []:
        push(r, "0DTE $1K")

    # Journal / daily_pnl opens entered today
    dp = snapshot.get("daily_pnl") or {}
    for r in dp.get("open") or []:
        if not isinstance(r, dict) or not r.get("took"):
            continue
        if r.get("day_entry") == day or _day_key_ct(r.get("entered_at")) == day:
            push(
                {**r, "ask": r.get("entry_ask")},
                str(r.get("category") or "journal"),
                require_day=True,
            )

    return out


def _collect_day_sells(snapshot: dict[str, Any], day: str) -> list[dict[str, Any]]:
    """SELL NOW / closed exits for this CT day."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(row: dict[str, Any] | None, desk: str) -> None:
        if not isinstance(row, dict):
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        when = (
            row.get("exited_at")
            or row.get("closed_at")
            or row.get("signaled_at")
            or row.get("day_exit")
        )
        day_hit = row.get("day_exit") == day or _day_key_ct(
            row.get("exited_at") or row.get("closed_at") or row.get("signaled_at")
        ) == day
        # Live SELL NOW board always included at EOD
        live_sell = str(row.get("action") or "").upper() in {"SELL_NOW", "EXIT"} or desk.endswith(
            "live"
        )
        if not day_hit and not live_sell:
            return
        key = f"{desk}|{sym}|{row.get('contract') or row.get('strike')}|{when}"
        if key in seen:
            return
        seen.add(key)
        entry = _as_float(row.get("entry_ask") if row.get("entry_ask") is not None else row.get("entry_price"))
        exit_px = _as_float(
            row.get("exit_bid")
            if row.get("exit_bid") is not None
            else (row.get("bid") if row.get("bid") is not None else row.get("ask"))
        )
        pnl = _as_float(row.get("pnl_usd"))
        pct = _as_float(row.get("profit_pct") if row.get("profit_pct") is not None else row.get("unrealized_pct"))
        if pnl is None and entry and exit_px is not None:
            contracts = int(row.get("contracts") or 1)
            pnl = round((exit_px - entry) * 100 * contracts, 2)
            if entry > 0:
                pct = round((exit_px - entry) / entry * 100.0, 2)
        out.append(
            {
                "symbol": sym,
                "desk": desk.replace("|live", ""),
                "name": _trade_label(row),
                "entry_ask": entry,
                "exit_bid": exit_px,
                "pnl_usd": pnl,
                "profit_pct": pct,
                "when": to_cst_label(when) if when and when != day else None,
            }
        )

    acts = snapshot.get("actions") or {}
    for r in acts.get("sell_now") or []:
        push({**(r or {}), "action": "SELL_NOW"}, "Options|live")
    lot = snapshot.get("lottery") or {}
    for r in lot.get("sell_now") or []:
        push({**(r or {}), "action": "SELL_NOW"}, "Explosive|live")
    ml = (snapshot.get("ml6") or {}).get("actions") or snapshot.get("ml6") or {}
    for r in ml.get("sell_now") or []:
        push({**(r or {}), "action": "SELL_NOW"}, "ML6|live")
    ch = snapshot.get("challenge") or {}
    for r in ch.get("exit") or ch.get("sell_now") or []:
        push({**(r or {}), "action": "SELL_NOW"}, "Challenge|live")
    o1k = snapshot.get("odte_1k") or {}
    for r in o1k.get("exit_now") or o1k.get("exit") or o1k.get("out") or []:
        push({**(r or {}), "action": "SELL_NOW"}, "0DTE $1K|live")

    dp = snapshot.get("daily_pnl") or {}
    for r in dp.get("closed") or []:
        if isinstance(r, dict) and (r.get("day_exit") == day or _day_key_ct(r.get("exited_at")) == day):
            push(r, str(r.get("category") or "closed"))

    return out


def build_eod_report(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build structured EOD payload + Telegram message text."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day_ct = dt.astimezone(CT).date().isoformat()
    day_et = dt.astimezone(ET).date().isoformat()
    et_label = dt.astimezone(ET).strftime("%b %d, %Y · %I:%M %p ET").lstrip("0").replace(" 0", " ")

    dp = snapshot.get("daily_pnl") or {}
    closed_today = [
        r
        for r in (dp.get("closed") or [])
        if isinstance(r, dict)
        and (r.get("day_exit") == day_ct or _day_key_ct(r.get("exited_at")) == day_ct)
        and r.get("took")
    ]
    open_rows = [r for r in (dp.get("open") or []) if isinstance(r, dict) and r.get("took")]

    closed_pnl = round(sum(float(r.get("pnl_usd") or 0) for r in closed_today), 2)
    open_pnl = round(sum(float(r.get("unrealized_pnl_usd") or 0) for r in open_rows), 2)

    buys = _collect_day_buys(snapshot, day_ct)
    sells = _collect_day_sells(snapshot, day_ct)

    # Prefer day bucket from by_day when present
    for bucket in dp.get("by_day") or []:
        if isinstance(bucket, dict) and bucket.get("day") == day_ct:
            if bucket.get("realized_pnl_usd") is not None:
                closed_pnl = float(bucket["realized_pnl_usd"])
            break

    lines = [
        f"📊 EOD desk report · {et_label}",
        f"Session day (CT): {day_ct}",
        "",
        f"Closed P&L today: {_money(closed_pnl)}  ({len(closed_today)} exits)",
        f"Open P&L (mark):  {_money(open_pnl)}  ({len(open_rows)} open)",
        "",
    ]

    lines.append(f"BUY recommended ({len(buys)})")
    if buys:
        for b in buys[:20]:
            ask = b.get("ask")
            ask_s = f"${ask:.2f}" if ask is not None else "—"
            lines.append(f"  · {b['name']} · {b['desk']} · ask {ask_s}")
        if len(buys) > 20:
            lines.append(f"  · … +{len(buys) - 20} more")
    else:
        lines.append("  · none")

    lines.append("")
    lines.append(f"SELL / exits ({len(sells)})")
    if sells:
        for s in sells[:20]:
            entry = s.get("entry_ask")
            exit_px = s.get("exit_bid")
            if entry is not None and exit_px is not None:
                path = f"bought ${entry:.2f} → sell ${exit_px:.2f}"
            elif exit_px is not None:
                path = f"sell ${exit_px:.2f}"
            else:
                path = "exit"
            pnl_s = _money(s.get("pnl_usd")) + _pct(s.get("profit_pct"))
            lines.append(f"  · {s['name']} · {path} · {pnl_s}")
        if len(sells) > 20:
            lines.append(f"  · … +{len(sells) - 20} more")
    else:
        lines.append("  · none")

    if open_rows:
        lines.append("")
        lines.append(f"Still open ({len(open_rows)})")
        for r in open_rows[:12]:
            entry = _as_float(r.get("entry_ask"))
            mark = _as_float(r.get("mark"))
            upnl = _as_float(r.get("unrealized_pnl_usd"))
            path = (
                f"bought ${entry:.2f} → mark ${mark:.2f}"
                if entry is not None and mark is not None
                else (f"bought ${entry:.2f}" if entry is not None else "open")
            )
            lines.append(f"  · {_trade_label(r)} · {path} · {_money(upnl)}")
        if len(open_rows) > 12:
            lines.append(f"  · … +{len(open_rows) - 12} more")

    lines.append("")
    lines.append("Signal Desk · stockscanner")
    message = "\n".join(lines)

    return {
        "day_ct": day_ct,
        "day_et": day_et,
        "et_label": et_label,
        "closed_pnl_usd": closed_pnl,
        "open_pnl_usd": open_pnl,
        "closed_n": len(closed_today),
        "open_n": len(open_rows),
        "buys": buys,
        "sells": sells,
        "message": message,
    }


def maybe_send_eod_report(
    snapshot: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    state_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send EOD Telegram once per ET day during the 3:00 PM window."""
    if not _env_flag("EOD_TELEGRAM_ENABLED", True):
        return {"ok": False, "skipped": True, "reason": "EOD_TELEGRAM_ENABLED=0"}

    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    hour = int(os.environ.get("EOD_TELEGRAM_HOUR_ET") or 15)
    minute = int(os.environ.get("EOD_TELEGRAM_MINUTE_ET") or 0)
    window = int(os.environ.get("EOD_TELEGRAM_WINDOW_MIN") or 20)

    if not force and not in_eod_window(dt, hour_et=hour, minute_et=minute, window_minutes=window):
        return {
            "ok": True,
            "skipped": True,
            "reason": "outside_eod_window",
            "hour_et": hour,
            "minute_et": minute,
        }

    day_et = dt.astimezone(ET).date().isoformat()
    path = Path(state_path) if state_path else DEFAULT_STATE
    state = _load_state(path)
    if not force and state.get("sent_day_et") == day_et:
        return {"ok": True, "skipped": True, "reason": "already_sent", "day_et": day_et}

    if snapshot is None:
        from odte_scanner.alert_pulse import build_offline_snapshot

        snapshot = build_offline_snapshot()

    report = build_eod_report(snapshot, now=dt)
    tg = telegram_configured()
    if dry_run or not tg.get("ok"):
        if not dry_run:
            # Do not mark sent when not configured — retry when secrets land
            return {
                "ok": False,
                "skipped": True,
                "reason": "telegram_not_configured",
                "report": {k: v for k, v in report.items() if k != "message"},
                "message": report["message"],
            }
        _save_state(
            path,
            {
                "sent_day_et": day_et,
                "sent_at": dt.isoformat(),
                "dry_run": True,
                "closed_pnl_usd": report["closed_pnl_usd"],
                "open_pnl_usd": report["open_pnl_usd"],
            },
        )
        return {
            "ok": True,
            "dry_run": True,
            "day_et": day_et,
            "message": report["message"],
            "closed_pnl_usd": report["closed_pnl_usd"],
            "open_pnl_usd": report["open_pnl_usd"],
        }

    res = send_telegram_text(report["message"])
    if res.get("ok"):
        _save_state(
            path,
            {
                "sent_day_et": day_et,
                "sent_at": dt.isoformat(),
                "message_id": res.get("message_id"),
                "closed_pnl_usd": report["closed_pnl_usd"],
                "open_pnl_usd": report["open_pnl_usd"],
                "buys_n": len(report["buys"]),
                "sells_n": len(report["sells"]),
            },
        )
        logger.info(
            "EOD telegram sent day=%s closed=%s open=%s",
            day_et,
            report["closed_pnl_usd"],
            report["open_pnl_usd"],
        )
        return {
            "ok": True,
            "sent": True,
            "day_et": day_et,
            "message_id": res.get("message_id"),
            "closed_pnl_usd": report["closed_pnl_usd"],
            "open_pnl_usd": report["open_pnl_usd"],
            "buys_n": len(report["buys"]),
            "sells_n": len(report["sells"]),
        }

    return {
        "ok": False,
        "sent": False,
        "error": res.get("error"),
        "day_et": day_et,
        "message": report["message"],
    }
