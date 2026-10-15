"""Turn a desk snapshot into public Gold Setup and Golden Flow rows.

Customer-facing names only. The internal Sure Winner label never enters the ledger.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from odte_scanner.json_util import finite_float
from odte_scanner.scorecard.ledger import canonical
from odte_scanner.time_cst import to_cst_label
from odte_scanner.trading.golden import is_golden_unusual_flow, is_sure_winner_gold

GOLD_SETUP = "Gold Setup"
GOLDEN_FLOW = "Golden Flow"


def _num(value: Any, places: int = 6) -> float | None:
    number = finite_float(value)
    if number is None or number <= 0:
        return None
    return round(number, places)


def _direction(row: dict[str, Any]) -> str:
    right = str(row.get("right") or "").upper()
    sentiment = str(row.get("sentiment") or "").lower()
    action = str(row.get("action") or "").upper()
    if right == "P" or sentiment == "bearish" or "PUT" in action:
        return "short"
    return "long"


def _spot(row: dict[str, Any], spots: dict[str, float]) -> float | None:
    for key in ("spot", "live_last", "live_spot", "entry_spot", "last_price", "underlying_price"):
        price = _num(row.get(key))
        if price is not None:
            return price
    ticker = str(row.get("symbol") or "").upper()
    return spots.get(ticker)


def _premium(row: dict[str, Any]) -> float | None:
    for key in ("ask", "mid", "premium"):
        price = _num(row.get(key))
        if price is not None:
            return price
    return None


def _day(row: dict[str, Any], signaled_at: str) -> str:
    """Chicago calendar date. The same contract the next session is a new signal."""
    day = str(row.get("day_cst") or "").strip()
    if len(day) >= 10:
        return day[:10]
    from odte_scanner.time_cst import CT

    try:
        dt = datetime.fromisoformat(signaled_at.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT).strftime("%Y-%m-%d")


def _when(row: dict[str, Any], now: datetime) -> tuple[str, str]:
    raw = row.get("signaled_at")
    if raw:
        text = str(raw).strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            cst = str(row.get("signaled_at_cst") or to_cst_label(text) or text)
            return text, cst
        except ValueError:
            pass
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    utc = now.astimezone(timezone.utc).isoformat()
    return utc, to_cst_label(utc) or utc


def _signal_id(signal_type: str, ticker: str, direction: str, day: str, anchor: str) -> str:
    raw = canonical(
        {
            "signal_type": signal_type,
            "ticker": ticker,
            "direction": direction,
            "day": day,
            "anchor": anchor,
        }
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_signal(
    row: dict[str, Any],
    *,
    signal_type: str,
    spots: dict[str, float],
    now: datetime,
    recorded_at: str,
) -> dict[str, Any] | None:
    ticker = str(row.get("symbol") or "").upper().strip()
    if not ticker:
        return None
    direction = _direction(row)
    signaled_at, signaled_at_ct = _when(row, now)
    day = _day(row, signaled_at)
    contract = str(row.get("contract") or "").strip() or None
    pick = str(row.get("pick_key") or "").strip()
    if signal_type == GOLD_SETUP:
        anchor = pick or contract or ticker
        conviction = str(row.get("confidence_tier") or "").upper() or None
    else:
        strike = row.get("strike")
        expiry = str(row.get("expiry") or "")
        right = str(row.get("right") or "").upper()
        anchor = contract or f"{right}|{expiry}|{strike}|{ticker}"
        conviction = None
    payload: dict[str, Any] = {
        "v": 1,
        "id": _signal_id(signal_type, ticker, direction, day, anchor),
        "signaled_at": signaled_at,
        "signaled_at_ct": signaled_at_ct,
        "recorded_at": recorded_at,
        "ticker": ticker,
        "signal_type": signal_type,
        "direction": direction,
        "reference_entry": _spot(row, spots),
        "reference_basis": "underlying",
        "premium_reference": _premium(row),
        "contract": contract,
        "right": str(row.get("right") or "").upper() or None,
        "conviction": conviction,
        "day": day,
    }
    return payload


def _iter_dicts(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def spot_index(scores: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in _iter_dicts(scores):
        ticker = str(row.get("symbol") or "").upper()
        price = _num(row.get("last_price") if row.get("last_price") is not None else row.get("entry"))
        if ticker and price is not None:
            out[ticker] = price
    return out


def extract_signals(
    *,
    sure_winners: Any = None,
    option_flow: Any = None,
    scores: Any = None,
    now: datetime | None = None,
    recorded_at: str | None = None,
) -> list[dict[str, Any]]:
    """Gold Setup = gold Sure Winner cards. Golden Flow = golden flow prints.

    SOLID cards, unusual/aggressive prints, and chart-pattern triangles are omitted.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamped = recorded_at or moment.astimezone(timezone.utc).isoformat()
    spots = spot_index(scores)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        sid = str(payload["id"])
        if sid in seen:
            return
        seen.add(sid)
        found.append(payload)

    board = sure_winners if isinstance(sure_winners, dict) else {}
    for row in _iter_dicts(board.get("winners")) + _iter_dicts(board.get("buy_now")):
        if not is_sure_winner_gold(row):
            continue
        add(_row_signal(row, signal_type=GOLD_SETUP, spots=spots, now=moment, recorded_at=stamped))

    flow_rows: list[dict[str, Any]] = []
    if isinstance(option_flow, dict):
        golden = _iter_dicts(option_flow.get("golden"))
        if golden:
            flow_rows = golden
        else:
            flow_rows = [row for row in _iter_dicts(option_flow.get("prints")) if is_golden_unusual_flow(row)]
    elif isinstance(option_flow, list):
        flow_rows = [row for row in _iter_dicts(option_flow) if is_golden_unusual_flow(row)]
    for row in flow_rows:
        # A triangle flag is not a flow print. Require the flow tier or flow flags.
        if not is_golden_unusual_flow(row):
            continue
        add(_row_signal(row, signal_type=GOLDEN_FLOW, spots=spots, now=moment, recorded_at=stamped))
    return found
