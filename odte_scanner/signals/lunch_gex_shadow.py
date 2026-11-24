"""Lunch-Break Credit negative-GEX shadow log. SHADOW ONLY — never an order.

When Lunch-Break Credit fires an entry on SPY or QQQ and that symbol's
prior-day EOD GEX regime is negative, record a hypothetical second put
credit spread with the short strike $2 further out (same width, same exits:
buy back at 50% of the credit, stop at 2x the credit, flat 2:45 PM CT).
Both legs are priced from live option quotes at entry and on every cycle
until exit. The real desk's fill is read (read-only) from the paper order
ledger and kept next to the shadow for comparison.

This module imports no broker, router, or order builder. It reads quotes and
JSON files and writes only its own log on the Fly volume.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from odte_scanner.signals.lunch_credit import (
    PRIMARY_RULES,
    WEBULL_SYMBOLS,
    LunchRules,
    exit_reason,
    natural_credit,
    spread_mid,
)
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "outputs" / "lunch_gex_shadow.json"
DEFAULT_LEDGER = ROOT / "outputs" / "webull_orders.json"
SHADOW_OFFSET = 2.0
DESK = "lunch_credit"
MAX_ROWS = 400
_lock = threading.RLock()

QuoteFn = Callable[[list[str]], dict[str, tuple[float | None, float | None]]]


def log_path() -> Path:
    raw = (os.environ.get("LUNCH_GEX_SHADOW_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_LOG


def occ(symbol: str, expiry: str, strike: float, right: str = "P") -> str:
    yymmdd = expiry.replace("-", "")[2:8]
    return f"{symbol.upper()}{yymmdd}{right.upper()}{int(round(float(strike) * 1000)):08d}"


def shadow_strikes(short_strike: float, width: float, offset: float = SHADOW_OFFSET) -> tuple[float, float]:
    """Short $offset further out of the money (lower for puts), same width."""
    short = float(short_strike) - float(offset)
    return short, short - float(width)


def _read(path: Path) -> dict[str, Any]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write(path: Path, blob: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("lunch gex shadow log not writable: %s", type(exc).__name__)
        return False


def tradier_quotes(contracts: list[str]) -> dict[str, tuple[float | None, float | None]]:
    """Live option bid/ask from Tradier (read-only market data)."""
    from odte_scanner.data import tradier

    res = tradier.fetch_quotes(list(contracts))
    out: dict[str, tuple[float | None, float | None]] = {}
    quotes = res.get("quotes") if isinstance(res, dict) else None
    if isinstance(quotes, dict):
        for key, row in quotes.items():
            if not isinstance(row, dict):
                continue
            out[str(key).upper()] = (_num(row.get("bid")), _num(row.get("ask")))
    return out


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def real_desk_fill(symbol: str, day: str, *, ledger: Path | None = None) -> dict[str, Any]:
    """Read-only lookup of the real Lunch desk's open/close fills for ``symbol`` on ``day``."""
    blob = _read(ledger or DEFAULT_LEDGER)
    out: dict[str, Any] = {"status": "none"}
    for row in blob.get("orders") or []:
        if not isinstance(row, dict):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if str(row.get("desk") or meta.get("golden_kind") or "") != DESK:
            continue
        if str(row.get("symbol") or "").upper() != symbol.upper() or meta.get("leg"):
            continue
        created = str(row.get("created_at") or "")
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp.astimezone(CT).date().isoformat() != day:
                continue
        except ValueError:
            continue
        role = meta.get("role")
        filled = str(row.get("status") or "") in {"filled", "partial"} or str(meta.get("fill_state") or "") in {"filled", "partial"}
        if role == "open":
            out.update({
                "status": "filled" if filled else str(row.get("status") or "working"),
                "short_strike": _num(meta.get("short_strike")) or _num(row.get("strike")),
                "long_strike": _num(meta.get("long_strike")),
                "credit": _num(row.get("fill_price")) if filled else None,
                "limit": _num(row.get("limit_price")),
            })
        elif role == "close" and filled:
            out.update({
                "exit_debit": _num(row.get("fill_price")) or _num(row.get("limit_price")),
                "exit_reason": meta.get("exit_reason"),
                "status": "closed",
            })
    credit, debit = out.get("credit"), out.get("exit_debit")
    if credit is not None and debit is not None:
        out["pnl_usd"] = round((credit - debit) * 100, 2)
    return out


def _fired(card: dict[str, Any]) -> bool:
    return bool(card.get("send") or card.get("send_order")) and str(card.get("symbol") or "").upper() in WEBULL_SYMBOLS


def sync_shadow(
    board: dict[str, Any] | None,
    now: datetime,
    *,
    regime_for: Callable[[str], str],
    quotes: QuoteFn | None = None,
    path: Path | None = None,
    ledger: Path | None = None,
    rules: LunchRules | None = None,
) -> dict[str, Any]:
    """Open/mark/close shadow spreads. Writes only the shadow log. Never orders."""
    rules = rules or PRIMARY_RULES
    quote_fn = quotes or tradier_quotes
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    now_ct = moment.astimezone(CT)
    day = now_ct.date().isoformat()
    p = path or log_path()
    flat = now_ct.time() >= rules.flat_ct
    with _lock:
        blob = _read(p)
        rows: list[dict[str, Any]] = [r for r in (blob.get("trades") or []) if isinstance(r, dict)]
        by_id = {r.get("id"): r for r in rows}
        # 1) open: one shadow per symbol per day, only on a fired entry + negative GEX
        for card in (board or {}).get("signals") or []:
            if not isinstance(card, dict) or not _fired(card):
                continue
            sym = str(card["symbol"]).upper()
            rid = f"{day}|{sym}"
            if rid in by_id:
                continue
            regime = regime_for(sym)
            if regime != "negative":
                continue
            if now_ct.time() >= rules.entry_end:
                continue
            short_k = _num(card.get("short_strike"))
            long_k = _num(card.get("long_strike"))
            if short_k is None or long_k is None:
                continue
            width = abs(short_k - long_k) or rules.width
            s_short, s_long = shadow_strikes(short_k, width)
            expiry = str(card.get("expiry") or day)[:10]
            row = {
                "id": rid,
                "day": day,
                "symbol": sym,
                "regime": regime,
                "shadow_only": True,
                "status": "pending_quote",
                "signal_at_ct": card.get("at_ct"),
                "expiry": expiry,
                "width": width,
                "desk_short_strike": short_k,
                "desk_long_strike": long_k,
                "desk_signal_credit": _num(card.get("credit")),
                "short_strike": s_short,
                "long_strike": s_long,
                "short_contract": occ(sym, expiry, s_short),
                "long_contract": occ(sym, expiry, s_long),
                "created_at": moment.astimezone(timezone.utc).isoformat(),
            }
            rows.append(row)
            by_id[rid] = row
        # 2) price entries and marks from live quotes
        live = [r for r in rows if r.get("day") == day and r.get("status") in {"pending_quote", "open"}]
        if live:
            wanted = sorted({c for r in live for c in (r["short_contract"], r["long_contract"])})
            try:
                book = quote_fn(wanted) or {}
            except Exception as exc:  # noqa: BLE001
                logger.info("lunch gex shadow quotes failed: %s", type(exc).__name__)
                book = {}
            for r in live:
                sb, sa = book.get(r["short_contract"], (None, None))
                lb, la = book.get(r["long_contract"], (None, None))
                mid = spread_mid(sb, sa, lb, la)
                stamp = moment.astimezone(timezone.utc).isoformat()
                if r["status"] == "pending_quote":
                    if mid is not None and mid > 0 and not flat:
                        r.update({
                            "status": "open",
                            "credit": round(mid, 4),
                            "credit_natural": natural_credit(sb, la),
                            "entry_quotes": {"short": [sb, sa], "long": [lb, la]},
                            "entry_at": stamp,
                            "entry_at_ct": now_ct.strftime("%H:%M:%S"),
                        })
                    elif flat or now_ct.time() >= rules.entry_end:
                        r.update({"status": "no_quote", "closed_at": stamp})
                    continue
                credit = float(r["credit"])
                reason = exit_reason(credit, float(mid if mid is not None else credit), flat=flat) if (mid is not None or flat) else None
                if mid is not None:
                    r["last_mark"] = round(mid, 4)
                    r["last_mark_at"] = stamp
                    r["max_mark"] = round(max(float(r.get("max_mark") or mid), mid), 4)
                    r["min_mark"] = round(min(float(r.get("min_mark") or mid), mid), 4)
                if reason:
                    debit = mid if mid is not None else r.get("last_mark")
                    r.update({
                        "status": "closed",
                        "exit_reason": reason if mid is not None else "flat_no_quote",
                        "exit_debit": None if debit is None else round(float(debit), 4),
                        "exit_quotes": {"short": [sb, sa], "long": [lb, la]},
                        "closed_at": stamp,
                        "closed_at_ct": now_ct.strftime("%H:%M:%S"),
                    })
                    if debit is not None:
                        r["pnl_usd"] = round((credit - float(debit)) * 100, 2)
        # 3) keep the real desk fill next to every shadow from today
        for r in rows:
            if r.get("day") == day:
                try:
                    r["desk"] = real_desk_fill(r["symbol"], day, ledger=ledger)
                except Exception as exc:  # noqa: BLE001
                    r["desk"] = {"status": "unavailable", "error": type(exc).__name__}
        cutoff = (now_ct.date() - timedelta(days=60)).isoformat()
        rows = [r for r in rows if str(r.get("day") or "") >= cutoff][-MAX_ROWS:]
        _write(p, {"version": 1, "trades": rows})
    return summary(now=moment, path=p)


def summary(*, now: datetime | None = None, path: Path | None = None, days: int = 30) -> dict[str, Any]:
    """Last-``days`` shadow vs real desk results."""
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(CT).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    rows = [r for r in (_read(path or log_path()).get("trades") or []) if isinstance(r, dict) and str(r.get("day") or "") >= cutoff]
    closed = [r for r in rows if r.get("status") == "closed" and r.get("pnl_usd") is not None]
    desk_closed = [r for r in rows if isinstance(r.get("desk"), dict) and r["desk"].get("pnl_usd") is not None]
    wins = sum(1 for r in closed if r["pnl_usd"] > 0)
    return {
        "ok": True,
        "shadow_only": True,
        "days": days,
        "rule": "Negative prior-day GEX (±2% of spot): second put credit spread, short $2 further out, same width and exits. Not traded.",
        "trades": len(rows),
        "closed": len(closed),
        "wins": wins,
        "win_rate_pct": round(100 * wins / len(closed), 1) if closed else None,
        "shadow_pnl_usd": round(sum(r["pnl_usd"] for r in closed), 2),
        "desk_pnl_usd_same_days": round(sum(r["desk"]["pnl_usd"] for r in desk_closed), 2),
        "rows": list(reversed(rows))[:60],
    }
