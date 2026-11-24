"""Lunch-Break Credit × prior-day GEX regime shadow log. No orders, ever.

Whenever a Lunch-Break Credit card enters (SPY/QQQ, quoted, inside the entry
window) this records:

* ``base`` — the desk's own spread (ATM short put, long $2 lower), tracked
  from live quotes. Recorded on both regimes so they can be compared.
* ``wide`` — on NEGATIVE-GEX days only, a hypothetical second put credit
  spread with the short strike $2 further out (lower) and the same width.

Both use the desk's exits: 50% take-profit, 2× stop, flat 2:45 PM CT.
Nothing here imports the broker or touches the live / paper books. Records
persist on the Fly volume (``outputs/``). New records stop 30 days after the
first one (auto-expire); existing rows stay readable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.signals.lunch_credit import (
    PRIMARY_RULES,
    WEBULL_SYMBOLS,
    exit_reason,
    leg_mid,
    long_put,
    spread_mid,
    spread_pnl,
)
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "lunch_gex_shadow.json"
SHADOW_DAYS = 30
WIDE_OFFSET = 2.0
MARK_EVERY_SEC = 60.0
FLAT_CT = PRIMARY_RULES.flat_ct

_lock = threading.Lock()
_running = threading.Event()
_last_chain: dict[str, float] = {}


def _ct(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def read_store(path: Path | None = None) -> dict[str, Any]:
    try:
        blob = json.loads((path or DEFAULT_STORE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    if not isinstance(blob.get("trades"), list):
        blob["trades"] = []
    return blob


def write_store(blob: dict[str, Any], path: Path | None = None) -> None:
    dest = path or DEFAULT_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob), encoding="utf-8")
    tmp.replace(dest)


def expires_on(store: dict[str, Any]) -> str | None:
    start = store.get("started_on")
    if not start:
        return None
    try:
        return (date.fromisoformat(str(start)) + timedelta(days=SHADOW_DAYS)).isoformat()
    except ValueError:
        return None


def is_active(store: dict[str, Any], day: date) -> bool:
    end = expires_on(store)
    return end is None or day.isoformat() < end


def _quotes(chain: list[dict[str, Any]] | None, today: date) -> dict[float, tuple[float | None, float | None]]:
    out: dict[float, tuple[float | None, float | None]] = {}
    for row in chain or []:
        if not isinstance(row, dict) or str(row.get("right") or "P").upper()[:1] != "P":
            continue
        try:
            if date.fromisoformat(str(row.get("expiry") or "")[:10]) != today:
                continue
            strike = round(float(row.get("strike")), 2)
        except (TypeError, ValueError):
            continue
        out[strike] = (_num(row.get("bid")), _num(row.get("ask")))
    return out


def _leg_mark(book: dict[float, tuple[float | None, float | None]], short: float, long: float) -> tuple[float | None, dict[str, Any]]:
    sb, sa = book.get(round(short, 2), (None, None))
    lb, la = book.get(round(long, 2), (None, None))
    mark = spread_mid(sb, sa, lb, la)
    return mark, {"short_bid": sb, "short_ask": sa, "long_bid": lb, "long_ask": la}


def _new_leg(short: float, width: float) -> dict[str, Any]:
    return {
        "short_strike": short,
        "long_strike": long_put(short, width),
        "status": "pending",
        "credit": None,
        "entry_at": None,
        "last_mark": None,
        "last_mark_at": None,
        "exit_reason": None,
        "exit_debit": None,
        "exit_at": None,
        "pnl_usd": None,
        "pnl_pct": None,
    }


def _open_leg(leg: dict[str, Any], book, now_ct: datetime) -> None:
    mark, q = _leg_mark(book, float(leg["short_strike"]), float(leg["long_strike"]))
    if mark is None or mark <= 0:
        return
    leg.update(status="open", credit=round(mark, 4), entry_at=now_ct.strftime("%H:%M:%S"), entry_quotes=q, last_mark=round(mark, 4), last_mark_at=now_ct.strftime("%H:%M:%S"))


def _close_leg(leg: dict[str, Any], debit: float, reason: str, now_ct: datetime) -> None:
    credit = float(leg.get("credit") or 0)
    dollars, pct = spread_pnl(credit, debit, 1)
    leg.update(status="closed", exit_reason=reason, exit_debit=round(debit, 4), exit_at=now_ct.strftime("%H:%M:%S"), pnl_usd=dollars, pnl_pct=pct)


def _mark_leg(leg: dict[str, Any], book, now_ct: datetime, *, flat: bool) -> None:
    if leg.get("status") != "open":
        return
    mark, _ = _leg_mark(book, float(leg["short_strike"]), float(leg["long_strike"]))
    if mark is not None:
        leg["last_mark"] = round(mark, 4)
        leg["last_mark_at"] = now_ct.strftime("%H:%M:%S")
    use = mark if mark is not None else _num(leg.get("last_mark"))
    credit = float(leg.get("credit") or 0)
    if use is None and not flat:
        return
    reason = exit_reason(credit, float(use if use is not None else credit), flat=flat)
    if reason:
        _close_leg(leg, float(use if use is not None else credit), reason, now_ct)


def _entry_cards(board: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for card in (board or {}).get("signals") or []:
        if not isinstance(card, dict):
            continue
        sym = str(card.get("symbol") or "").upper()
        if sym not in WEBULL_SYMBOLS:
            continue
        if not card.get("send_order") and not card.get("order_status"):
            continue
        out.append(card)
    return out


def _expire_stale(store: dict[str, Any], today: date) -> bool:
    """Close legs left open from an earlier day at their last mark."""
    changed = False
    for row in store["trades"]:
        if row.get("day") == today.isoformat():
            continue
        for key in ("base", "wide"):
            leg = row.get(key)
            if not isinstance(leg, dict):
                continue
            if leg.get("status") == "open":
                last = _num(leg.get("last_mark"))
                credit = float(leg.get("credit") or 0)
                dollars, pct = spread_pnl(credit, last if last is not None else credit, 1)
                leg.update(status="closed", exit_reason="flat_unmarked", exit_debit=last, exit_at=None, pnl_usd=dollars, pnl_pct=pct)
                changed = True
            elif leg.get("status") == "pending":
                leg["status"] = "no_quote"
                changed = True
    return changed


def run_shadow_cycle(
    board: dict[str, Any] | None,
    now: datetime | None = None,
    *,
    store_path: Path | None = None,
    fetch_chain: Callable[[str], list[dict[str, Any]]] | None = None,
    regime_block: dict[str, Any] | None = None,
    force_marks: bool = False,
) -> dict[str, Any]:
    """Open new shadow rows from entered cards, then mark and exit open legs."""
    from odte_scanner.signals.gex_regime import NEGATIVE, cached_block, regime_for

    now_ct = _ct(now)
    today = now_ct.date()
    day = today.isoformat()
    flat = now_ct.time() >= FLAT_CT
    entry_open = PRIMARY_RULES.entry_start <= now_ct.time() < PRIMARY_RULES.entry_end
    block = regime_block if regime_block is not None else cached_block(now)
    getter = fetch_chain or _default_chain
    with _lock:
        store = read_store(store_path)
        changed = _expire_stale(store, today)
        have = {row.get("id") for row in store["trades"]}
        active = is_active(store, today)
        for card in _entry_cards(board) if (active and entry_open) else []:
            sym = str(card.get("symbol")).upper()
            rid = f"{day}|{sym}"
            if rid in have:
                continue
            short = _num(card.get("short_strike"))
            long = _num(card.get("long_strike"))
            if short is None or long is None:
                continue
            width = abs(short - long) or PRIMARY_RULES.width
            info = regime_for(sym, block)
            row = {
                "id": rid,
                "day": day,
                "symbol": sym,
                "at_ct": card.get("at_ct"),
                "regime": info["regime"],
                "regime_label": info["label"],
                "gex_net": info.get("net_gex"),
                "gex_date": info.get("gex_date"),
                "card_credit_mid": _num(card.get("credit_mid")),
                "width": width,
                "base": _new_leg(short, width),
                "wide": _new_leg(round(short - WIDE_OFFSET, 2), width) if info["regime"] == NEGATIVE else None,
                "shadow_only": True,
            }
            store.setdefault("started_on", day)
            store["trades"].append(row)
            have.add(rid)
            changed = True
        todays = [row for row in store["trades"] if row.get("day") == day]
        need = {}
        for row in todays:
            for key in ("base", "wide"):
                leg = row.get(key)
                if isinstance(leg, dict) and leg.get("status") in ("pending", "open"):
                    need.setdefault(row["symbol"], []).append((row, key, leg))
        import time as _time

        for sym, legs in need.items():
            if not force_marks and not flat and _time.monotonic() - _last_chain.get(sym, -1e9) < MARK_EVERY_SEC:
                continue
            _last_chain[sym] = _time.monotonic()
            try:
                chain = getter(sym)
            except Exception as exc:  # noqa: BLE001
                logger.info("lunch gex shadow chain %s failed: %s", sym, type(exc).__name__)
                chain = []
            book = _quotes(chain, today)
            for row, key, leg in legs:
                if leg["status"] == "pending":
                    if entry_open and not flat:
                        _open_leg(leg, book, now_ct)
                    elif flat or now_ct.time() >= PRIMARY_RULES.entry_end:
                        leg["status"] = "no_quote"
                else:
                    _mark_leg(leg, book, now_ct, flat=flat)
            changed = True
        if changed:
            write_store(store, store_path)
    return build_block(store, now)


def _default_chain(symbol: str) -> list[dict[str, Any]]:
    from odte_scanner.signals.lunch_credit_board import _yahoo_chain

    return _yahoo_chain(symbol, "P", now=None)


def run_shadow_cycle_async(board: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """Daemon thread so the Lunch loop never waits on shadow quotes."""
    if _running.is_set():
        return False
    _running.set()
    snap = {"signals": [dict(c) for c in ((board or {}).get("signals") or []) if isinstance(c, dict)]}

    def _work() -> None:
        try:
            run_shadow_cycle(snap, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lunch gex shadow cycle failed: %s", type(exc).__name__)
        finally:
            _running.clear()

    threading.Thread(target=_work, name="lunch-gex-shadow", daemon=True).start()
    return True


def _side(legs: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [leg for leg in legs if leg.get("status") == "closed" and leg.get("pnl_usd") is not None]
    pnl = [float(leg["pnl_usd"]) for leg in closed]
    return {
        "n": len(legs),
        "closed": len(closed),
        "wins": sum(1 for p in pnl if p > 0),
        "total_usd": round(sum(pnl), 2),
        "avg_usd": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "targets": sum(1 for leg in closed if leg.get("exit_reason") == "target"),
        "stops": sum(1 for leg in closed if leg.get("exit_reason") == "stop"),
    }


def build_block(store: dict[str, Any] | None = None, now: datetime | None = None, *, store_path: Path | None = None) -> dict[str, Any]:
    store = store if store is not None else read_store(store_path)
    trades = [row for row in store.get("trades") or [] if isinstance(row, dict)]
    by_regime: dict[str, Any] = {}
    for regime in ("positive", "negative", "unknown"):
        rows = [row for row in trades if row.get("regime") == regime]
        by_regime[regime] = {"base": _side([r["base"] for r in rows if isinstance(r.get("base"), dict)])}
        if regime == "negative":
            by_regime[regime]["wide"] = _side([r["wide"] for r in rows if isinstance(r.get("wide"), dict)])
    pairs = [
        float(r["wide"]["pnl_usd"]) - float(r["base"]["pnl_usd"])
        for r in trades
        if isinstance(r.get("wide"), dict)
        and isinstance(r.get("base"), dict)
        and r["wide"].get("pnl_usd") is not None
        and r["base"].get("pnl_usd") is not None
    ]
    today = _ct(now).date()
    return {
        "ok": True,
        "shadow_only": True,
        "orders": False,
        "title": "Lunch-Break Credit · GEX shadow log",
        "rule": "Negative-GEX days: also track the put spread with the short strike $2 further out (same width, 50% TP, 2× stop, flat 2:45 PM CT). No orders.",
        "started_on": store.get("started_on"),
        "expires_on": expires_on(store),
        "active": is_active(store, today),
        "by_regime": by_regime,
        "wide_minus_base_usd": {"n": len(pairs), "total": round(sum(pairs), 2), "avg": round(sum(pairs) / len(pairs), 2) if pairs else None},
        "recent": trades[-20:][::-1],
    }


def public_block() -> dict[str, Any]:
    try:
        return build_block()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "shadow_only": True, "error": type(exc).__name__, "recent": []}
