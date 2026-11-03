"""Daily P&L for the public scorecard, from Webull paper fills.

Closed dollars use the same match as the Edge book and the exit alerts:
one filled sell, entry from the sell's stored price, otherwise the buy fill.
SPX is not a Webull order. The internal journal is a separate ledger.
Days are the exit date in America/Chicago. Missing history is left blank.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.trading.portal_book import public_desk_name, round_trips, tier_of

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
HISTORY_FLOOR = "2026-10-28"
PAPER_LABEL = (
    "Paper trading — Webull paper account, 1 contract unless noted, not financial advice"
)
CORE_DESKS = ("Diamond", "Sniper", "Gold Setup", "Afternoon Puts")
JOURNAL_CATS = {"lottery", "challenge", "weekly", "0dte", "swing", "odte_1k", "rip", "ml6"}
# Webull orders for these sleeves stay with the internal journal, not the paper total.
JOURNAL_DESKS = {
    "lottery",
    "explosive",
    "challenge",
    "weekly",
    "swing",
    "odte",
    "rip",
    "ml6",
    "odte_1k",
}
JOURNAL_LABELS = {"Lottery", "Challenge", "Options", "RIP", "0DTE $1K", "ML6", "Weekly", "Swing"}
SPX_LABEL = "SPX modeled — not a Webull order and not included in the paper total."
JOURNAL_LABEL = (
    "Internal journal (lottery, challenge, weekly, and other sleeves). "
    "Not included in the Webull paper total."
)

_side_cache: dict[str, Any] = {"at": 0.0, "spx": [], "journal": None, "retained": []}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _day_key(value: Any) -> str | None:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(CT).date().isoformat()
    text = str(value or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and "T" not in text[:10] and len(text) == 10:
        return text[:10]
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT).date().isoformat()


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _money(value: float) -> float:
    return round(float(value), 2)


def _rate(wins: int, losses: int) -> float | None:
    decided = wins + losses
    if not decided:
        return None
    return round(wins / decided * 100.0, 1)


def _empty_desk() -> dict[str, Any]:
    return {"pnl": 0.0, "wins": 0, "losses": 0, "flats": 0, "trades": 0}


def _add_result(bucket: dict[str, Any], pnl: float, *, field: str = "pnl") -> None:
    bucket[field] = _money(float(bucket[field]) + pnl)
    bucket["trades"] += 1
    if pnl > 0:
        bucket["wins"] += 1
    elif pnl < 0:
        bucket["losses"] += 1
    else:
        bucket["flats"] += 1


def _retained_trips(path: Path | None) -> list[dict[str, Any]]:
    """Sell fills kept after the order file drops them. Not estimated trades."""
    dest = path or (ROOT / "outputs" / "paper_round_trips.jsonl")
    if not dest.exists():
        return []
    try:
        lines = dest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            per_contract = float(row["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            qty = int(float(row.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, qty)
        source = str(row.get("source") or "Other")
        if source.lower().replace("_", " ") in {"sure winner", "sure_winner"}:
            source = "Gold Setup"
        sell_id = str(row.get("sell_id") or key.split(":", 1)[0])
        out.append(
            {
                "tier": None,
                "desk": source,
                "symbol": "",
                "contract": str(row.get("contract") or ""),
                "contracts": qty,
                "pnl": _money(per_contract * qty),
                "order_id": sell_id,
                "exited_at": str(row.get("at") or ""),
                "entered_at": "",
                "from_log": True,
                "entry": row.get("entry"),
                "exit": row.get("exit"),
            }
        )
    return out


def _scorecard_spx_rows() -> list[dict[str, Any]]:
    try:
        from odte_scanner.scorecard.service import public_view

        view = public_view()
    except Exception:
        return []
    block = view.get("spx_strategies") if isinstance(view, dict) else None
    rows = block.get("rows") if isinstance(block, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _journal_board(
    *,
    journal_book: dict[str, Any] | None,
    challenge: dict[str, Any] | None,
    odte_1k: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if journal_book is None and challenge is None and odte_1k is None:
        return None
    from odte_scanner.trading.daily_pnl import build_daily_pnl

    return build_daily_pnl(
        journal_book=journal_book,
        challenge=challenge,
        odte_1k=odte_1k,
        include_recommended_not_taken=False,
    )


def _load_side_files() -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    """Scorecard SPX, the internal journal, and the retained round-trip log."""
    now = time.time()
    if now - float(_side_cache.get("at") or 0) < 30 and _side_cache.get("loaded"):
        return _side_cache["spx"], _side_cache["journal"], _side_cache["retained"]
    journal = _read_json(ROOT / "outputs" / "signal_journal.json")
    challenge_raw = _read_json(ROOT / "outputs" / "challenge_ledger.json")
    odte_raw = _read_json(ROOT / "outputs" / "odte_1k_ledger.json")
    challenge = None
    if isinstance(challenge_raw, dict):
        challenge = challenge_raw if isinstance(challenge_raw.get("book"), dict) else {"book": challenge_raw}
    odte = None
    if isinstance(odte_raw, dict):
        odte = odte_raw if isinstance(odte_raw.get("book"), dict) else {"book": odte_raw}
    journal_book = journal if isinstance(journal, dict) else None
    board = _journal_board(journal_book=journal_book, challenge=challenge, odte_1k=odte)
    retained = _retained_trips(None)
    spx = _scorecard_spx_rows()
    _side_cache.update(at=now, loaded=True, spx=spx, journal=board, retained=retained)
    return spx, board, retained


def _mark_lookup(marks: Any) -> dict[str, float]:
    found: dict[str, float] = {}
    if isinstance(marks, dict):
        for key, value in marks.items():
            price = _num(value)
            if price is not None and price > 0:
                found[str(key)] = price
        return found
    for row in marks or []:
        if not isinstance(row, dict):
            continue
        price = _num(row.get("mark") if row.get("mark") is not None else row.get("bid"))
        contract = str(row.get("contract") or "")
        if contract and price is not None and price > 0:
            found[contract] = price
    return found


def _order_mark(order: dict[str, Any]) -> float | None:
    meta = order.get("meta") if isinstance(order.get("meta"), dict) else {}
    for key in ("mark", "bid", "last_price", "market_price"):
        price = _num(meta.get(key) if key in meta else order.get(key))
        if price is not None and price > 0:
            return price
    return None


def _open_positions(orders: list[dict[str, Any]], marks: Any) -> list[dict[str, Any]]:
    """Filled buys not yet sold. Unrealized only when a mark is already stored."""
    book = [order for order in orders if isinstance(order, dict)]
    lots: dict[str, list[dict[str, Any]]] = {}
    sold: dict[str, int] = {}
    for order in book:
        if str(order.get("symbol") or "").upper() == "SPX":
            continue
        if str(order.get("desk") or "").lower() in JOURNAL_DESKS:
            continue
        if str(order.get("status") or "").lower() not in {"filled", "partial"}:
            continue
        contract = str(order.get("contract") or "")
        if not contract:
            continue
        try:
            qty = max(1, int(float(order.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        action = str(order.get("action") or "").upper()
        if action == "SELL":
            sold[contract] = sold.get(contract, 0) + qty
            continue
        if action != "BUY":
            continue
        entry = _num(order.get("fill_price"))
        if entry is None or entry <= 0:
            continue
        lots.setdefault(contract, []).append(
            {
                "qty": qty,
                "entry": entry,
                "order": order,
                "at": str(order.get("updated_at") or order.get("created_at") or ""),
            }
        )
    quoted = _mark_lookup(marks)
    open_rows: list[dict[str, Any]] = []
    for contract, rows in lots.items():
        rows.sort(key=lambda item: item["at"])
        left = sold.get(contract, 0)
        for lot in rows:
            take = min(left, int(lot["qty"]))
            left -= take
            remain = int(lot["qty"]) - take
            if remain <= 0:
                continue
            order = lot["order"]
            tier = tier_of(order)
            mark = quoted.get(contract)
            if mark is None:
                mark = _order_mark(order)
            unrealized = None
            if mark is not None:
                unrealized = _money((mark - float(lot["entry"])) * 100.0 * remain)
            open_rows.append(
                {
                    "desk": public_desk_name(tier, order, None),
                    "symbol": str(order.get("symbol") or "").upper(),
                    "contract": contract,
                    "contracts": remain,
                    "entry": lot["entry"],
                    "mark": mark,
                    "unrealized_pnl_usd": unrealized,
                    "unrealized": True,
                    "entered_at": lot["at"],
                }
            )
    return open_rows


def _spx_days(
    rows: list[dict[str, Any]] | None,
    exits: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Modeled SPX results. These dollars stay out of the Webull total."""
    by_day: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, float]] = set()

    def add(day: str | None, pnl: float, *, label: str, unrealized: bool = False) -> None:
        if not day:
            return
        key = (day, label, round(pnl, 2))
        if key in seen:
            return
        seen.add(key)
        bucket = by_day.setdefault(
            day,
            {"day": day, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "trades": 0, "open_n": 0},
        )
        if unrealized:
            bucket["unrealized_pnl_usd"] = _money(bucket["unrealized_pnl_usd"] + pnl)
            bucket["open_n"] += 1
        else:
            bucket["realized_pnl_usd"] = _money(bucket["realized_pnl_usd"] + pnl)
            bucket["trades"] += 1

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        outcome = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else {}
        pnl = _num(outcome.get("pnl"))
        if pnl is None:
            continue
        day = _day_key(row.get("signaled_at") or row.get("recorded_at"))
        closed = str(outcome.get("status") or "") == "closed"
        add(day, pnl, label=str(row.get("signal_type") or "SPX"), unrealized=not closed)
    for row in exits or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper() != "SPX":
            continue
        entry = _num(row.get("ask"))
        if entry is None or entry <= 0:
            entry = _num(row.get("premium"))
        pct = _num(row.get("option_pct"))
        if entry is None or pct is None:
            continue
        pnl = entry * pct
        day = str(row.get("day") or "")[:10] or _day_key(row.get("exit_at_ct") or row.get("at_ct"))
        add(day, pnl, label=str(row.get("tier") or "SPX"), unrealized=False)
    days = sorted(by_day.values(), key=lambda item: item["day"], reverse=True)
    realized = _money(sum(float(item["realized_pnl_usd"]) for item in days))
    return {
        "label": SPX_LABEL,
        "included_in_webull_total": False,
        "realized_pnl_usd": realized,
        "days": days,
    }


def _journal_days(board: dict[str, Any] | None, fills: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    for trip in fills or []:
        day = _day_key(trip.get("exited_at"))
        if not day:
            continue
        fill_rows.append(
            {
                "day": day,
                "desk": str(trip.get("desk") or trip.get("raw_desk") or "Journal"),
                "realized_pnl_usd": _money(float(trip.get("pnl") or 0)),
                "trades": 1,
            }
        )
    if isinstance(board, dict):
        for row in board.get("by_day") or []:
            if not isinstance(row, dict):
                continue
            cats = [
                cat
                for cat in (row.get("by_category") or [])
                if isinstance(cat, dict) and str(cat.get("category") or "") in JOURNAL_CATS
            ]
            if not cats:
                continue
            pnl = _money(sum(float(cat.get("realized_pnl_usd") or 0) for cat in cats))
            trades = sum(int(cat.get("closed_n") or 0) for cat in cats)
            days.append(
                {
                    "day": row.get("day"),
                    "realized_pnl_usd": pnl,
                    "trades": trades,
                    "categories": [
                        {
                            "category": cat.get("category"),
                            "pnl": cat.get("realized_pnl_usd"),
                            "trades": cat.get("closed_n"),
                        }
                        for cat in cats
                    ],
                }
            )
    return {
        "label": JOURNAL_LABEL,
        "included_in_webull_total": False,
        "realized_pnl_usd": _money(sum(float(row["realized_pnl_usd"]) for row in days)),
        "days": days,
        "webull_sleeve_fills": fill_rows,
        "webull_sleeve_pnl_usd": _money(sum(float(row["realized_pnl_usd"]) for row in fill_rows)),
    }


def _gap_note(days: list[str], *, log_only: list[str], undated: int) -> str:
    if not days:
        note = (
            "No Webull paper fills are in the retained order history. "
            "Earlier days were not estimated."
        )
    else:
        start = min(days)
        end = max(days)
        if start > HISTORY_FLOOR:
            note = (
                f"Retained Webull fills start {start}. "
                f"Order history before {start} aged out of the ledger and was not estimated."
            )
        else:
            note = f"Retained Webull fills run from {start} through {end}."
    if log_only:
        note += (
            " Days kept only in the round-trip log, because the order file no longer has them: "
            + ", ".join(log_only)
            + "."
        )
    if undated:
        note += f" {undated} priced fill(s) have no exit time and are not placed on a day."
    return note


def build_scorecard_daily_pnl(
    orders: list[dict[str, Any]] | None = None,
    *,
    spx_rows: list[dict[str, Any]] | None = None,
    spx_exits: list[dict[str, Any]] | None = None,
    journal_book: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    odte_1k: dict[str, Any] | None = None,
    journal_board: dict[str, Any] | None = None,
    retained: list[dict[str, Any]] | None = None,
    marks: Any = None,
    now: datetime | None = None,
    load_side: bool | None = None,
    ledger_path: Path | None = None,
    round_trip_path: Path | None = None,
) -> dict[str, Any]:
    """One Chicago day per row, newest first, from sandbox paper fills."""
    if orders is None:
        from odte_scanner.hot_state import read_ledger_orders

        orders = read_ledger_orders(ledger_path)
        if load_side is None:
            load_side = True
    elif load_side is None:
        load_side = False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.astimezone(CT).date().isoformat()
    book = [order for order in (orders or []) if isinstance(order, dict)]
    trips = round_trips(book, include_other_desks=True)
    for trip in trips:
        trip["from_log"] = False

    side_spx: list[dict[str, Any]] = []
    side_journal: dict[str, Any] | None = None
    side_retained: list[dict[str, Any]] = []
    if load_side:
        side_spx, side_journal, side_retained = _load_side_files()
    if retained is None and round_trip_path is not None:
        side_retained = _retained_trips(round_trip_path)
    elif retained is not None:
        side_retained = retained

    known_ids = {str(trip.get("order_id") or "") for trip in trips if trip.get("order_id")}
    known_keys = {
        (
            str(trip.get("contract") or ""),
            str(trip.get("exited_at") or ""),
            round(float(trip.get("pnl") or 0), 2),
        )
        for trip in trips
    }
    for extra in side_retained:
        sell_id = str(extra.get("order_id") or "")
        key = (
            str(extra.get("contract") or ""),
            str(extra.get("exited_at") or ""),
            round(float(extra.get("pnl") or 0), 2),
        )
        if sell_id and sell_id in known_ids:
            continue
        if key in known_keys:
            continue
        trips.append(dict(extra))
        if sell_id:
            known_ids.add(sell_id)
        known_keys.add(key)

    by_day: dict[str, dict[str, Any]] = {}
    undated = 0
    journal_fills: list[dict[str, Any]] = []
    routed: list[dict[str, Any]] = []
    for trip in trips:
        raw = str(trip.get("raw_desk") or "").lower()
        desk_name = str(trip.get("desk") or "")
        if raw in JOURNAL_DESKS or desk_name in JOURNAL_LABELS:
            journal_fills.append(trip)
        else:
            routed.append(trip)
    for trip in routed:
        pnl = _money(float(trip.get("pnl") or 0))
        day = _day_key(trip.get("exited_at"))
        if not day:
            undated += 1
            continue
        desk = str(trip.get("desk") or "Other")
        if desk.lower().replace("_", " ") in {"sure winner", "sure_winner"}:
            desk = "Gold Setup"
        bucket = by_day.setdefault(
            day,
            {
                "day": day,
                "realized_pnl_usd": 0.0,
                "wins": 0,
                "losses": 0,
                "flats": 0,
                "trades": 0,
                "desks": {},
                "multi_contract": 0,
                "from_log_only": True,
                "log_trades": 0,
                "order_trades": 0,
            },
        )
        _add_result(bucket, pnl, field="realized_pnl_usd")
        cell = bucket["desks"].setdefault(desk, _empty_desk())
        _add_result(cell, pnl)
        try:
            qty = int(trip.get("contracts") or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty != 1:
            bucket["multi_contract"] += 1
        if trip.get("from_log"):
            bucket["log_trades"] += 1
        else:
            bucket["from_log_only"] = False
            bucket["order_trades"] += 1

    open_rows = _open_positions(book, marks)
    unrealized_values = [row["unrealized_pnl_usd"] for row in open_rows if row.get("unrealized_pnl_usd") is not None]
    unrealized = _money(sum(unrealized_values)) if unrealized_values else None
    marked = len(unrealized_values)
    if not open_rows:
        unrealized_label = None
    elif marked == len(open_rows):
        unrealized_label = "unrealized"
    elif marked:
        unrealized_label = f"unrealized, mark on {marked} of {len(open_rows)}"
    else:
        unrealized_label = "unrealized mark unavailable"

    if open_rows and today not in by_day:
        by_day[today] = {
            "day": today,
            "realized_pnl_usd": 0.0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "trades": 0,
            "desks": {},
            "multi_contract": 0,
            "from_log_only": False,
            "log_trades": 0,
            "order_trades": 0,
        }

    ordered = sorted(by_day)
    running = 0.0
    cumulative: dict[str, float] = {}
    for day in ordered:
        running = _money(running + float(by_day[day]["realized_pnl_usd"]))
        cumulative[day] = running
    present: set[str] = set()
    for bucket in by_day.values():
        present.update(bucket["desks"])
    extras = sorted(name for name in present if name not in CORE_DESKS)
    desk_names = list(CORE_DESKS) + extras

    days_out: list[dict[str, Any]] = []
    for day in reversed(ordered):
        bucket = by_day[day]
        desks_out: dict[str, Any] = {}
        for name in desk_names:
            cell = bucket["desks"].get(name) or _empty_desk()
            desks_out[name] = {
                "pnl": _money(cell["pnl"]),
                "wins": cell["wins"],
                "losses": cell["losses"],
                "trades": cell["trades"],
                "record": f"{cell['wins']}W/{cell['losses']}L",
            }
        note = None
        if bucket["multi_contract"]:
            note = f"{bucket['multi_contract']} fill(s) are more than 1 contract"
        is_today = day == today
        days_out.append(
            {
                "day": day,
                "realized_pnl_usd": _money(bucket["realized_pnl_usd"]),
                "cumulative_pnl_usd": cumulative[day],
                "wins": bucket["wins"],
                "losses": bucket["losses"],
                "flats": bucket["flats"],
                "win_rate_pct": _rate(bucket["wins"], bucket["losses"]),
                "trades": bucket["trades"],
                "contracts_note": note,
                "open_n": len(open_rows) if is_today else 0,
                "unrealized_pnl_usd": unrealized if is_today else None,
                "unrealized_label": unrealized_label if is_today and open_rows else None,
                "desks": desks_out,
                "from_retained_log": bool(bucket["log_trades"] and not bucket["order_trades"]),
            }
        )

    chart = [
        {"day": day, "pnl": _money(by_day[day]["realized_pnl_usd"]), "cumulative": cumulative[day]}
        for day in ordered
        if by_day[day]["trades"] or by_day[day]["realized_pnl_usd"]
    ]
    log_only = [row["day"] for row in days_out if row["from_retained_log"]]
    log_only.reverse()
    if journal_board is None and (journal_book or challenge or odte_1k):
        journal_board = _journal_board(journal_book=journal_book, challenge=challenge, odte_1k=odte_1k)
    elif journal_board is None and load_side:
        journal_board = side_journal
    spx_source = spx_rows if spx_rows is not None else (side_spx if load_side else [])
    closed_days = [row["day"] for row in days_out if row["trades"]]
    wins = sum(row["wins"] for row in days_out)
    losses = sum(row["losses"] for row in days_out)
    trades = sum(row["trades"] for row in days_out)
    realized = _money(sum(row["realized_pnl_usd"] for row in days_out))
    return {
        "ok": True,
        "label": PAPER_LABEL,
        "source": "webull_sandbox_fills",
        "timezone": "America/Chicago",
        "as_of": moment.astimezone(timezone.utc).isoformat(),
        "today": today,
        "history_floor": HISTORY_FLOOR,
        "history_from": min(closed_days) if closed_days else None,
        "history_through": max(closed_days) if closed_days else None,
        "gap_note": _gap_note(closed_days, log_only=log_only, undated=undated),
        "totals": {
            "realized_pnl_usd": realized,
            "cumulative_pnl_usd": realized,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "flats": sum(row["flats"] for row in days_out),
            "win_rate_pct": _rate(wins, losses),
            "open_n": len(open_rows),
            "unrealized_pnl_usd": unrealized,
            "unrealized_label": unrealized_label,
        },
        "desks": desk_names,
        "days": days_out,
        "chart": chart,
        "open_positions": open_rows,
        "spx_modeled": _spx_days(spx_source, spx_exits),
        "journal": _journal_days(journal_board, journal_fills),
    }
