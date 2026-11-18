"""Today's P&L for every desk, in America/Chicago.

Three groups that are never added together:

* Webull paper desks — Webull paper fills are the source of truth.
* Internal paper ledgers — Kotegawa, Million, and the journal sleeves
  (Challenge, Lottery, Weekly, ...). These are not Webull orders.
* Alert-only desks — News Desk, Squeeze Fire, Golden Flow, Power Hour, and
  the modeled SPX plan. Counts, plus the paper-study hypothetical where one
  exists. Never an order.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from odte_scanner.scorecard.daily_pnl import JOURNAL_DESKS, _day_key, _open_positions
from odte_scanner.trading.portal_book import round_trips

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
LABEL = "Today's P&L · paper only, not financial advice"
WEBULL_LABEL = "Webull paper desks — Webull paper fills are the source of truth"
INTERNAL_LABEL = "Internal paper ledgers — not Webull orders, not in the Webull total"
ALERT_LABEL = "Alert-only desks — no orders"
WEBULL_ORDER = ("Sniper", "Diamond", "Gold Setup", "Afternoon Puts", "Lunch-Break Credit")
# Alert-log kinds that belong to each desk row.
ALERT_KINDS = {
    "Sniper": ("Sniper",),
    "Diamond": ("Diamond",),
    "Gold Setup": ("Gold Setup",),
    "Afternoon Puts": ("AfternoonPuts", "Afternoon Puts"),
    "Lunch-Break Credit": ("Lunch", "LunchCredit", "Lunch-Break Credit"),
    "Power Hour": ("Power Hour", "PowerHour"),
    "Kotegawa": ("Kotegawa",),
    "Million": ("Million",),
    "Lottery": ("Explosive", "Lottery"),
    "Golden Flow": ("Golden Flow",),
    "SPX Strategy": ("SPX Strategy",),
}
JOURNAL_NAMES = {
    "challenge": "Challenge",
    "lottery": "Lottery",
    "weekly": "Weekly",
    "0dte": "0DTE",
    "odte_1k": "0DTE $1K",
    "rip": "RIP",
    "swing": "Swing",
    "ml6": "ML6",
    "journal": "Journal",
}
ALWAYS_INTERNAL = ("Challenge", "Lottery")

_alert_memo: dict[str, Any] = {"key": None, "counts": {}}
_mark_memo: dict[str, Any] = {"at": 0.0, "marks": {}}
_lock = threading.Lock()


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value) + 0.0, 2)


def _row(name: str, **extra: Any) -> dict[str, Any]:
    base = {
        "name": name,
        "realized": 0.0,
        "unrealized": None,
        "open_n": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "alerts": 0,
    }
    base.update(extra)
    return base


def _add_trade(row: dict[str, Any], pnl: float) -> None:
    row["trades"] += 1
    row["realized"] = round(row["realized"] + pnl, 2)
    if pnl > 0:
        row["wins"] += 1
    elif pnl < 0:
        row["losses"] += 1


def _total(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out = {"realized": 0.0, "unrealized": None, "trades": 0, "wins": 0, "losses": 0, "open_n": 0}
    for row in rows:
        out["realized"] = round(out["realized"] + float(row.get("realized") or 0), 2)
        if row.get("unrealized") is not None:
            out["unrealized"] = round(float(out["unrealized"] or 0) + float(row["unrealized"]), 2)
        for key in ("trades", "wins", "losses", "open_n"):
            out[key] += int(row.get(key) or 0)
    return out


# ------------------------------------------------------------------ alert counts


def alert_counts(day: str, path: Path | None = None, *, tail_bytes: int = 2_000_000) -> dict[str, int]:
    """Sent alerts per desk kind for one CT day. Reads the log tail, cached on mtime."""
    dest = path or (ROOT / "outputs" / "desk_alert_log.jsonl")
    try:
        stat = dest.stat()
    except OSError:
        return {}
    key = (str(dest), stat.st_mtime_ns, stat.st_size, day)
    with _lock:
        if _alert_memo.get("key") == key:
            return dict(_alert_memo["counts"])
    counts: dict[str, int] = {}
    try:
        with dest.open("rb") as handle:
            if stat.st_size > tail_bytes:
                handle.seek(stat.st_size - tail_bytes)
                handle.readline()
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("ok") is False:
            continue
        if _day_key(row.get("time")) != day:
            continue
        kind = str(row.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    with _lock:
        _alert_memo["key"] = key
        _alert_memo["counts"] = dict(counts)
    return counts


def _alerts_for(name: str, counts: dict[str, int]) -> int:
    return sum(int(counts.get(kind) or 0) for kind in ALERT_KINDS.get(name, (name,)))


# ------------------------------------------------------------------ Webull marks


def _tradier_marks(contracts: list[str]) -> dict[str, float]:
    """Mid (or bid) for a few open contracts, cached 60 s. Empty without a key."""
    want = sorted({c for c in contracts if c})
    if not want or not (os.environ.get("TRADIER_ACCESS_TOKEN") or "").strip():
        return {}
    now = time.monotonic()
    with _lock:
        cached = _mark_memo.get("marks") or {}
        if now - float(_mark_memo.get("at") or 0) < 60 and all(c in cached for c in want):
            return {c: cached[c] for c in want}
    try:
        from odte_scanner.data.tradier import fetch_quotes

        body = fetch_quotes(want, timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        logger.info("today pnl marks failed: %s", exc)
        return {}
    marks: dict[str, float] = {}
    for contract, quote in ((body or {}).get("quotes") or {}).items():
        if not isinstance(quote, dict):
            continue
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        if bid and ask and ask >= bid:
            marks[str(contract).upper()] = round((bid + ask) / 2, 4)
        elif bid:
            marks[str(contract).upper()] = bid
    with _lock:
        _mark_memo["at"] = now
        _mark_memo["marks"] = marks
    return marks


# ------------------------------------------------------------------ groups


def webull_group(
    book: list[dict[str, Any]],
    day: str,
    counts: dict[str, int],
    *,
    marks: dict[str, float] | None = None,
    fetch_marks: bool = True,
) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {name: _row(name) for name in WEBULL_ORDER}
    for trip in round_trips(book, include_other_desks=True):
        if str(trip.get("raw_desk") or "").lower() in JOURNAL_DESKS:
            continue
        if _day_key(trip.get("exited_at")) != day:
            continue
        pnl = _num(trip.get("pnl"))
        if pnl is None:
            continue
        name = str(trip.get("desk") or "Other")
        rows.setdefault(name, _row(name))
        _add_trade(rows[name], pnl)
    opens = _open_positions(book, {})
    live = dict(marks or {})
    if fetch_marks and opens:
        missing = [str(p.get("contract")) for p in opens if p.get("mark") is None and str(p.get("contract")) not in live]
        live.update(_tradier_marks(missing))
    open_rows: list[dict[str, Any]] = []
    for pos in opens:
        name = str(pos.get("desk") or "Other")
        row = rows.setdefault(name, _row(name))
        row["open_n"] += 1
        mark = _num(pos.get("mark"))
        if mark is None:
            mark = live.get(str(pos.get("contract") or "").upper())
        unreal = _num(pos.get("unrealized_pnl_usd"))
        if unreal is None and mark is not None:
            entry = _num(pos.get("entry")) or 0.0
            unreal = round((mark - entry) * 100.0 * int(pos.get("contracts") or 1), 2)
        if unreal is not None:
            row["unrealized"] = round(float(row["unrealized"] or 0) + unreal, 2)
        else:
            row["unrealized_note"] = "mark unavailable"
        open_rows.append(
            {
                "desk": name,
                "symbol": pos.get("symbol"),
                "contract": pos.get("contract"),
                "entry": pos.get("entry"),
                "mark": mark,
                "unrealized": unreal,
                "entered_at": pos.get("entered_at"),
            }
        )
    if "Power Hour" not in rows and _alerts_for("Power Hour", counts):
        rows["Power Hour"] = _row("Power Hour")
    for name, row in rows.items():
        row["alerts"] = _alerts_for(name, counts)
    ordered = [rows[name] for name in WEBULL_ORDER] + [rows[n] for n in sorted(rows) if n not in WEBULL_ORDER]
    return {"label": WEBULL_LABEL, "rows": ordered, "open": open_rows, "total": _total(ordered)}


def _desk_today(view: Any, name: str, day: str, counts: dict[str, int]) -> dict[str, Any]:
    row = _row(name, route="internal paper ledger")
    if not isinstance(view, dict):
        row["note"] = "desk unavailable"
        return row
    for trade in view.get("closed") or []:
        if not isinstance(trade, dict) or _day_key(trade.get("exit_at") or trade.get("exited_at")) != day:
            continue
        pnl = _num(trade.get("pnl"))
        if pnl is not None:
            _add_trade(row, pnl)
    realized_today = _num(view.get("realized_today"))
    if realized_today is not None:
        row["realized"] = round(realized_today, 2)
    open_list = [p for p in view.get("open") or [] if isinstance(p, dict)]
    row["open_n"] = len(open_list)
    row["entries_today"] = sum(1 for p in open_list if _day_key(p.get("entry_at")) == day)
    unreal = _num(view.get("unrealized_pnl"))
    if unreal is None and open_list:
        parts = [_num(p.get("unrealized_pnl")) for p in open_list]
        if all(part is not None for part in parts):
            unreal = round(sum(parts), 2)  # type: ignore[arg-type]
    row["unrealized"] = _money(unreal) if open_list else None
    row["equity"] = _num(view.get("equity"))
    row["alerts"] = _alerts_for(name, counts)
    if view.get("marked_at"):
        row["marked_at"] = view.get("marked_at")
    return row


def _journal_rows(daily: Any, day: str, counts: dict[str, int]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    board = daily if isinstance(daily, dict) else {}
    for entry in board.get("by_day") or []:
        if not isinstance(entry, dict) or str(entry.get("day")) != day:
            continue
        for trade in entry.get("trades") or []:
            if not isinstance(trade, dict):
                continue
            category = str(trade.get("category") or "journal").lower()
            name = JOURNAL_NAMES.get(category, category.title())
            pnl = _num(trade.get("pnl_usd"))
            if pnl is None:
                continue
            row = rows.setdefault(name, _row(name, route="internal journal"))
            _add_trade(row, pnl)
    for pos in board.get("open") or []:
        if not isinstance(pos, dict):
            continue
        category = str(pos.get("category") or "journal").lower()
        name = JOURNAL_NAMES.get(category, category.title())
        row = rows.setdefault(name, _row(name, route="internal journal"))
        row["open_n"] += 1
        unreal = _num(pos.get("unrealized_pnl_usd"))
        if unreal is not None:
            row["unrealized"] = round(float(row["unrealized"] or 0) + unreal, 2)
    for name in ALWAYS_INTERNAL:
        rows.setdefault(name, _row(name, route="internal journal"))
    for name, row in rows.items():
        row["alerts"] = _alerts_for(name, counts)
    ordered = [rows[n] for n in ALWAYS_INTERNAL] + [rows[n] for n in sorted(rows) if n not in ALWAYS_INTERNAL]
    return ordered


def internal_group(body: dict[str, Any], day: str, counts: dict[str, int]) -> dict[str, Any]:
    rows = [
        _desk_today(body.get("million_challenge"), "Million", day, counts),
        _desk_today(body.get("kotegawa"), "Kotegawa", day, counts),
    ]
    rows.extend(_journal_rows(body.get("daily_pnl"), day, counts))
    return {"label": INTERNAL_LABEL, "rows": rows, "total": _total(rows)}


def _squeeze_row(block: Any, day: str) -> dict[str, Any]:
    row = {"name": "Squeeze Fire", "kind": "alert only + paper study", "signals": 0, "hypothetical": None, "marked": 0}
    if not isinstance(block, dict) or str(block.get("day") or "") != day:
        return row
    today = [r for r in block.get("today") or [] if isinstance(r, dict)]
    row["signals"] = len(today)
    total = 0.0
    marked = 0
    for signal in today:
        ask = _num(signal.get("signal_ask"))
        marks = signal.get("marks") if isinstance(signal.get("marks"), dict) else {}
        latest = None
        for name in ("2:55 PM", "+60m", "+30m", "+15m"):
            mark = marks.get(name) if isinstance(marks.get(name), dict) else None
            if mark and not mark.get("missed") and _num(mark.get("bid")) is not None:
                latest = _num(mark.get("bid"))
                break
        if ask and latest is not None:
            total += (latest - ask) * 100.0
            marked += 1
    if marked:
        row["hypothetical"] = round(total, 2)
    row["marked"] = marked
    return row


def alert_group(body: dict[str, Any], day: str, counts: dict[str, int], spx: Any) -> dict[str, Any]:
    news = body.get("news_desk") if isinstance(body.get("news_desk"), dict) else {}
    news_today = str(news.get("day") or "") == day
    power = body.get("power_hour") if isinstance(body.get("power_hour"), dict) else {}
    power_counts = power.get("counts") if isinstance(power.get("counts"), dict) else {}
    rows: list[dict[str, Any]] = [
        {
            "name": "News Desk",
            "kind": "alert only",
            "alerts": int(news.get("sent_today") or 0) if news_today else 0,
            "events": int(news.get("n_events") or 0) if news_today else 0,
        },
        _squeeze_row(body.get("squeeze_fire"), day),
        {"name": "Golden Flow", "kind": "alert only", "alerts": _alerts_for("Golden Flow", counts)},
        {
            "name": "Power Hour",
            "kind": "alert board — no paper trades",
            "alerts": _alerts_for("Power Hour", counts),
            "long": int(power_counts.get("long") or 0),
            "short": int(power_counts.get("short") or 0),
        },
    ]
    spx_row = {"name": "SPX plan (modeled)", "kind": "alert only — modeled, not an order", "alerts": _alerts_for("SPX Strategy", counts)}
    for entry in (spx or {}).get("days") or [] if isinstance(spx, dict) else []:
        if isinstance(entry, dict) and str(entry.get("day")) == day:
            spx_row["modeled_realized"] = _money(_num(entry.get("realized_pnl_usd")))
            spx_row["modeled_unrealized"] = _money(_num(entry.get("unrealized_pnl_usd")))
            spx_row["trades"] = int(entry.get("trades") or 0)
    rows.append(spx_row)
    return {"label": ALERT_LABEL, "rows": rows}


# ------------------------------------------------------------------ public


def build_today_pnl(
    body: dict[str, Any] | None,
    book: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    alert_log: Path | None = None,
    fetch_marks: bool = True,
) -> dict[str, Any]:
    moment = (now or datetime.now(timezone.utc)).astimezone(CT)
    day = moment.date().isoformat()
    snap = body if isinstance(body, dict) else {}
    counts = alert_counts(day, alert_log)
    scorecard = snap.get("scorecard_daily_pnl") if isinstance(snap.get("scorecard_daily_pnl"), dict) else {}
    out = {
        "ok": True,
        "label": LABEL,
        "day": day,
        "as_of": moment.isoformat(timespec="seconds"),
        "timezone": "America/Chicago",
        "webull": webull_group(list(book or []), day, counts, fetch_marks=fetch_marks),
        "internal": internal_group(snap, day, counts),
        "alert_only": alert_group(snap, day, counts, scorecard.get("spx_modeled")),
        "note": (
            "Realized = closed today (exit time, Central). Unrealized = open positions at the latest mark "
            "versus entry. Webull desks and internal ledgers are separate books and are never added together."
        ),
    }
    out["telegram_text"] = format_telegram(out)
    return out


def _usd(value: Any, *, sign: bool = True) -> str:
    number = _num(value)
    if number is None:
        return "n/a"
    if abs(number) < 0.005:
        return "$0"
    text = f"${abs(number):,.2f}"
    if not sign:
        return text
    return ("+" if number > 0 else "−") + text


def _row_text(row: dict[str, Any]) -> str:
    name = "Lunch credit" if row["name"] == "Lunch-Break Credit" else row["name"]
    bits = [f"{name} {_usd(row.get('realized'))}"]
    if row.get("trades"):
        bits.append(f"({row['wins']}W/{row['losses']}L)")
    if row.get("open_n"):
        unreal = row.get("unrealized")
        bits.append(f"open {row['open_n']} {_usd(unreal) if unreal is not None else 'mark n/a'}")
    return " ".join(bits)


def format_telegram(board: dict[str, Any]) -> str:
    try:
        day = datetime.fromisoformat(str(board.get("day"))).strftime("%a %b %-d")
    except ValueError:
        day = str(board.get("day"))
    web = board["webull"]
    web_total = web["total"]
    internal = board["internal"]
    int_total = internal["total"]
    lines = [f"📊 TODAY'S P&L · {day} · paper only"]
    lines.append(
        f"Webull paper desks (fills): {_usd(web_total['realized'])} realized · {web_total['trades']} trades · "
        f"{web_total['wins']}W/{web_total['losses']}L"
        + (f" · open {_usd(web_total['unrealized'])}" if web_total.get("unrealized") is not None else "")
    )
    lines.append(" • " + " · ".join(_row_text(r) for r in web["rows"] if r.get("trades") or r.get("open_n") or r["name"] in WEBULL_ORDER))
    lines.append(
        f"Internal paper ledgers (not Webull): {_usd(int_total['realized'])} realized"
        + (f" · open {_usd(int_total['unrealized'])}" if int_total.get("unrealized") is not None else "")
    )
    lines.append(" • " + " · ".join(_row_text(r) for r in internal["rows"]))
    alert_bits = []
    for row in board["alert_only"]["rows"]:
        if row["name"] == "News Desk":
            alert_bits.append(f"News Desk {row['alerts']} sent")
        elif row["name"] == "Squeeze Fire":
            hyp = row.get("hypothetical")
            alert_bits.append(
                f"Squeeze {row['signals']} signal(s)" + (f", study {_usd(hyp)} hypothetical" if hyp is not None else "")
            )
        elif row["name"] == "SPX plan (modeled)":
            if row.get("modeled_realized") is not None:
                alert_bits.append(f"SPX modeled {_usd(row['modeled_realized'])} (not an order)")
        else:
            alert_bits.append(f"{row['name']} {row.get('alerts', 0)} alert(s)")
    lines.append("Alert only (no orders): " + " · ".join(alert_bits))
    return "\n".join(lines)


# ------------------------------------------------------------------ end-of-day Telegram

EOD_START = (15, 10)
EOD_END = (16, 30)
_eod_started = False
_eod_lock = threading.Lock()


def _eod_state_path() -> Path:
    return ROOT / "outputs" / "today_pnl_eod.json"


def _snapshot_body() -> dict[str, Any]:
    from odte_scanner.hot_state import apply_hot_overlay

    path = ROOT / "outputs" / "last_api_snapshot.json"
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        body = {}
    return apply_hot_overlay(body if isinstance(body, dict) else {})


def maybe_send_eod(
    now: datetime | None = None,
    *,
    state_path: Path | None = None,
    build: Any = None,
    send: Any = None,
) -> dict[str, Any]:
    """Send the day's P&L once, between 3:10 and 4:30 PM CT on weekdays."""
    moment = (now or datetime.now(timezone.utc)).astimezone(CT)
    if moment.weekday() >= 5:
        return {"sent": False, "reason": "weekend"}
    clock = (moment.hour, moment.minute)
    if not (EOD_START <= clock < EOD_END):
        return {"sent": False, "reason": "outside window"}
    day = moment.date().isoformat()
    dest = state_path or _eod_state_path()
    try:
        state = json.loads(dest.read_text())
    except (OSError, ValueError):
        state = {}
    if isinstance(state, dict) and state.get("day") == day and state.get("sent"):
        return {"sent": False, "reason": "already sent"}
    board = build() if build else (_snapshot_body().get("today_pnl") or {})
    text = str((board or {}).get("telegram_text") or "")
    if not text:
        return {"sent": False, "reason": "no board"}
    if send is None:
        from odte_scanner.alerts.telegram import send_telegram_text as send
    result = send(text) or {}
    ok = bool(result.get("ok")) if isinstance(result, dict) else False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"day": day, "sent": ok, "at": moment.isoformat(timespec="seconds")}))
    except OSError:
        pass
    return {"sent": ok, "text": text}


def _eod_loop() -> None:
    while True:
        try:
            maybe_send_eod()
        except Exception as exc:  # noqa: BLE001
            logger.warning("today pnl EOD failed: %s", exc)
        time.sleep(60)


def start_eod_worker() -> bool:
    global _eod_started
    with _eod_lock:
        if _eod_started:
            return False
        _eod_started = True
    threading.Thread(target=_eod_loop, name="today-pnl-eod", daemon=True).start()
    return True
