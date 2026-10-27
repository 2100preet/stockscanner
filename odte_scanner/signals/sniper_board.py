"""Today's Sniper cards. No network in the pure helpers.

The live loader reuses Diamond's bar fetch (Tradier, then Yahoo). A card is
sent only inside 8:45–10:30 CT, only for the first three symbols, and only
with a real ask. SPX is a signal, not a Webull order.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.signals.diamond import INDEX_ETFS, YAHOO_SYMBOL, Bar
from odte_scanner.signals.diamond_board import load_intraday_bars, occ_symbol
from odte_scanner.signals.sniper import DAILY_CAP, PRIORITY, apply_daily_cap, run_universe, sniper_routed
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "sniper_day.json"
EXIT_PLAN = (
    "EXIT plan (sniper) · sell half at +25% premium · stop the rest at breakeven "
    "and trail half the peak gain · hard stop −40% · flat 10:30 AM CT. "
    "In-sample model, not a promise."
)


def choose_contract(
    symbol: str,
    spot: float,
    chain: list[dict[str, Any]] | None,
    *,
    right: str,
    today: date,
) -> dict[str, Any] | None:
    """ATM or one strike in the money. Index products stay inside 2 DTE."""
    if spot <= 0 or not chain:
        return None
    side = "P" if str(right).upper().startswith("P") else "C"
    index = symbol in INDEX_ETFS
    best: dict[str, Any] | None = None
    best_key: tuple[int, int, float] | None = None
    for row in chain:
        if not isinstance(row, dict):
            continue
        row_right = str(row.get("right") or "C").upper()[:1]
        if row_right != side:
            continue
        try:
            strike = float(row.get("strike"))
            expiry = str(row.get("expiry") or "")[:10]
            exp = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        dte = (exp - today).days
        if index:
            if dte < 0 or dte > 2:
                continue
        elif dte < 0 or dte > 7:
            continue
        otm = strike > spot if side == "C" else strike < spot
        gap = abs(strike - spot)
        key = (1 if otm else 0, dte, gap)
        if best_key is None or key < best_key:
            best_key = key
            best = {
                "symbol": symbol,
                "right": side,
                "strike": strike,
                "expiry": expiry,
                "dte": dte,
                "ask": row.get("ask"),
                "bid": row.get("bid"),
                "contract": str(row.get("contract") or "") or occ_symbol(symbol, expiry, strike, side),
                "spot": spot,
            }
    return best


def _lock_symbols(selected: list[dict[str, Any]], store: dict[str, Any], day: str) -> list[str]:
    prior = [str(sym).upper() for sym in (store.get("symbols") or [])] if store.get("day") == day else []
    ordered = [str(row["symbol"]).upper() for row in selected]
    kept = [sym for sym in prior if sym in ordered]
    for sym in ordered:
        if sym in kept:
            continue
        if len(kept) >= DAILY_CAP:
            break
        kept.append(sym)
    return kept[:DAILY_CAP]


def _read_store(path: Path) -> dict[str, Any]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write_store(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob), encoding="utf-8")


def build_sniper_board(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    chains: dict[str, list[dict[str, Any]]] | None = None,
    fetch_chain: Callable[[str, float, str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Today's capped cards. ``send`` is false once 10:30 CT has passed."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    now_ct = moment.astimezone(CT)
    day = now_ct.date().isoformat()
    routed = sniper_routed()
    window_open = time(8, 45) <= now_ct.time() < time(10, 30)
    path = store_path or DEFAULT_STORE
    store = _read_store(path)
    trades = [
        row
        for row in run_universe(bars_by_symbol)
        if row.get("day") == day and str(row.get("at") or "") <= moment.astimezone(timezone.utc).isoformat()
    ]
    ranked = apply_daily_cap(trades)
    allowed = _lock_symbols(ranked, store, day)
    if window_open and routed and allowed:
        fresh = {"day": day, "symbols": allowed}
        if fresh != {"day": store.get("day"), "symbols": store.get("symbols")}:
            _write_store(path, fresh)
    chain_book = chains or {}
    cards: list[dict[str, Any]] = []
    for row in ranked:
        symbol = str(row["symbol"]).upper()
        if symbol not in allowed:
            continue
        side = "P" if str(row.get("side") or "C").upper().startswith("P") else "C"
        spot = float(row["entry"])
        chain = chain_book.get(symbol)
        if chain is None and fetch_chain is not None and window_open:
            try:
                chain = fetch_chain(symbol, spot, side)
            except Exception as exc:  # noqa: BLE001
                logger.info("sniper chain %s failed: %s", symbol, exc)
                chain = None
        picked = choose_contract(symbol, spot, chain, right=side, today=now_ct.date())
        ask = None if picked is None else picked.get("ask")
        try:
            ask_ok = ask is not None and float(ask) > 0
        except (TypeError, ValueError):
            ask_ok = False
        send_order = bool(routed and window_open and symbol in allowed and ask_ok and symbol != "SPX")
        send_alert = bool(routed and window_open and symbol in allowed and ask_ok)
        word = "put" if side == "P" else "call"
        card = {
            "symbol": symbol,
            "action": "SNIPER",
            "desk": "Sniper",
            "sniper": True,
            "right": side,
            "spot": spot,
            "entry": spot,
            "signaled_at": row["at"],
            "signaled_at_cst": f"{day} {row['at_ct']} CT",
            "day_cst": day,
            "at_ct": row["at_ct"],
            "detail": (
                f"Sniper · 15-minute opening-range retest, 9/21 EMA stack, {word} · "
                f"signal {row['at_ct']} CT · flat 10:30 CT. In-sample."
            ),
            "exit_plan": EXIT_PLAN,
            "webull": symbol != "SPX",
            "send": send_alert,
            "send_order": send_order,
        }
        if picked:
            card.update(
                {
                    "strike": picked["strike"],
                    "expiry": picked["expiry"],
                    "dte": picked["dte"],
                    "ask": picked.get("ask"),
                    "bid": picked.get("bid"),
                    "contract": picked["contract"],
                    "right": picked["right"],
                }
            )
        cards.append(card)
    return {
        "ok": True,
        "routed": routed,
        "day": day,
        "window_open": window_open,
        "cap": DAILY_CAP,
        "signals": cards,
        "qualified": [
            {"symbol": row["symbol"], "at_ct": row["at_ct"], "side": row.get("side")}
            for row in sorted(
                trades,
                key=lambda item: (
                    item["at"],
                    PRIORITY.index(item["symbol"]) if item["symbol"] in PRIORITY else 99,
                ),
            )
        ],
        "note": (
            "Sniper calls and puts. At most 3 names a day. Sent only from 8:45 to 10:30 CT, "
            "and only with a live ask. SPX is not a Webull order. "
            "The 60-day model is in-sample."
        ),
    }


def load_sniper_board(*, now: datetime | None = None, store_path: Path | None = None) -> dict[str, Any]:
    """Live entry point used by the desk. Failures become an empty board."""
    try:
        bars = load_intraday_bars()
        if not bars:
            return {
                "ok": False,
                "routed": sniper_routed(),
                "signals": [],
                "qualified": [],
                "note": "Sniper needs intraday bars. None were loaded.",
            }

        def fetch_chain(symbol: str, spot: float, side: str) -> list[dict[str, Any]]:
            return _yahoo_chain(symbol, side, now=now)

        return build_sniper_board(bars, now=now, store_path=store_path, fetch_chain=fetch_chain)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sniper board failed: %s", exc)
        return {"ok": False, "routed": False, "signals": [], "qualified": [], "error": str(exc)[:200]}


def _yahoo_chain(symbol: str, side: str, *, now: datetime | None) -> list[dict[str, Any]]:
    """Nearest calls or puts. Used after the cap, so this is at most three names."""
    import yfinance as yf

    yahoo = YAHOO_SYMBOL.get(symbol, symbol)
    ticker = yf.Ticker(yahoo)
    expirations = list(ticker.options or [])
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(CT).date()
    want = "puts" if str(side).upper().startswith("P") else "calls"
    right = "P" if want == "puts" else "C"
    rows: list[dict[str, Any]] = []
    for expiry in expirations[:6]:
        try:
            exp = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            continue
        if (exp - today).days > 8:
            continue
        try:
            book = ticker.option_chain(expiry)
            table = book.puts if want == "puts" else book.calls
        except Exception:  # noqa: BLE001
            continue
        if table is None or table.empty:
            continue
        for _, opt in table.iterrows():
            strike = opt.get("strike")
            if strike is None:
                continue
            rows.append(
                {
                    "right": right,
                    "strike": float(strike),
                    "expiry": str(expiry)[:10],
                    "bid": opt.get("bid"),
                    "ask": opt.get("ask"),
                    "contract": str(opt.get("contractSymbol") or ""),
                }
            )
    return rows
