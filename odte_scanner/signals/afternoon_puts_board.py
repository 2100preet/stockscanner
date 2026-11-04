"""Today's Afternoon Puts cards. No network in the pure helpers.

SPY and QQQ can be a Webull paper order. SPX is a Telegram alert and a
modeled exit. A card is sent only after its 5-minute bar has closed, and
only inside 10:00–14:15 CT.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.signals.afternoon_puts import (
    PRIMARY_RULES,
    PRIORITY,
    SYMBOLS,
    WEBULL_SYMBOLS,
    AfternoonRules,
    bar_is_closed,
    live_entries,
    one_strike_otm_put,
)
from odte_scanner.signals.detected import (
    commit_day_store,
    day_detected,
    format_ct_hms,
    remember_detected,
)
from odte_scanner.signals.diamond import YAHOO_SYMBOL, Bar
from odte_scanner.signals.diamond_board import load_intraday_bars, modeled_spx_exits, occ_symbol
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "afternoon_puts_day.json"
CUSTOMER_NAME = "🌅 Afternoon Puts"
LOG_KIND = "AfternoonPuts"
EXIT_PLAN = (
    "EXIT plan (afternoon puts) · sell half at +100% · trail the rest "
    "(out if it gives back half the peak gain) · hard stop −40% from the fill · "
    "flat 2:45 PM CT. One contract. SPX is not a Webull order."
)


def choose_put(
    symbol: str,
    spot: float,
    chain: list[dict[str, Any]] | None,
    *,
    today: date,
) -> dict[str, Any] | None:
    """0DTE put, one strike out of the money. The target strike wins a tie."""
    if spot <= 0 or not chain:
        return None
    target = one_strike_otm_put(spot, symbol)
    best: dict[str, Any] | None = None
    best_key: tuple[int, int, float] | None = None
    for row in chain:
        if not isinstance(row, dict):
            continue
        if str(row.get("right") or "P").upper()[:1] != "P":
            continue
        try:
            strike = float(row.get("strike"))
            expiry = str(row.get("expiry") or "")[:10]
            exp = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            continue
        dte = (exp - today).days
        if dte != 0 or strike <= 0:
            continue
        gap = abs(strike - target)
        exact = 0 if abs(strike - target) < 0.02 else 1
        key = (exact, dte, gap)
        if best_key is None or key < best_key:
            best_key = key
            best = {
                "symbol": symbol,
                "right": "P",
                "strike": strike,
                "expiry": expiry,
                "dte": dte,
                "ask": row.get("ask"),
                "bid": row.get("bid"),
                "contract": str(row.get("contract") or "") or occ_symbol(symbol, expiry, strike, "P"),
                "spot": spot,
            }
    return best


def _read_store(path: Path) -> dict[str, Any]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write_store(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob), encoding="utf-8")


def phase_for(now_ct: datetime, cards: list[dict[str, Any]], rules: AfternoonRules) -> str:
    """waiting, scanning, triggered, or closed."""
    clock = now_ct.time()
    if clock < rules.entry_start:
        return "waiting"
    if not cards:
        return "scanning" if clock < rules.entry_end else "closed"
    if any(not row.get("closed") for row in cards):
        return "triggered"
    return "closed"


def build_afternoon_board(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    chains: dict[str, list[dict[str, Any]]] | None = None,
    fetch_chain: Callable[[str, float, str], list[dict[str, Any]]] | None = None,
    option_bars: dict[str, list[Bar]] | None = None,
    rules: AfternoonRules | None = None,
) -> dict[str, Any]:
    """Capped cards for the session. ``send`` waits until the signal bar closes."""
    rules = rules or PRIMARY_RULES
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    now_ct = moment.astimezone(CT)
    day = now_ct.date().isoformat()
    window_open = rules.entry_start <= now_ct.time() < rules.entry_end
    visible = {
        symbol: [bar for bar in (bars_by_symbol.get(symbol) or []) if bar_is_closed(bar, moment)]
        for symbol in SYMBOLS
    }
    booked = live_entries(visible, option_bars, rules, now=moment)
    trades = [row for row in booked["trades"] if row.get("day") == day]
    waiting = [row for row in booked["waiting"] if row.get("day") == day]
    # Both ETFs fill the cap of two. SPX alerts only when a slot is left.
    paper = [row for row in trades if str(row.get("symbol") or "") in WEBULL_SYMBOLS]
    spx_rows = [row for row in trades if str(row.get("symbol") or "") == "SPX"]
    trades = (paper + spx_rows)[: rules.daily_cap]
    path = store_path or DEFAULT_STORE
    store = _read_store(path)
    stopped = set()
    if store.get("day") == day:
        stopped = {str(sym).upper() for sym in (store.get("stopped") or [])}
    for row in trades:
        if str(row.get("reason") or "") == "stop":
            stopped.add(str(row.get("symbol") or "").upper())
    # The closed card stays up after a stop. The engine will not add a second signal.
    allowed = [str(row["symbol"]).upper() for row in trades][: rules.daily_cap]
    detected = day_detected(store, day)
    chain_book = chains or {}
    cards: list[dict[str, Any]] = []
    for row in trades:
        symbol = str(row["symbol"]).upper()
        if symbol not in allowed:
            continue
        spot = float(row["entry"])
        chain = chain_book.get(symbol)
        if chain is None and fetch_chain is not None and window_open and symbol in WEBULL_SYMBOLS:
            try:
                chain = fetch_chain(symbol, spot, "P")
            except Exception as exc:  # noqa: BLE001
                logger.info("afternoon puts chain %s failed: %s", symbol, exc)
                chain = None
        picked = choose_put(symbol, spot, chain, today=now_ct.date())
        chain_backed = picked is not None
        if picked is None:
            expiry = day
            strike = float(row["strike"])
            premium = float(row["premium"])
            picked = {
                "strike": strike,
                "expiry": expiry,
                "dte": 0,
                "ask": premium if premium > 0 else None,
                "bid": round(premium * 0.95, 2) if premium > 0 else None,
                "contract": occ_symbol(symbol, expiry, strike, "P"),
                "right": "P",
            }
        ask = picked.get("ask")
        try:
            ask_ok = ask is not None and float(ask) > 0
        except (TypeError, ValueError):
            ask_ok = False
        closed = str(row.get("reason") or "") in {"stop", "trail", "flat", "target"}
        # SPY and QQQ need a live chain ask. A modeled premium can alert SPX only.
        send_order = bool(
            window_open and symbol in WEBULL_SYMBOLS and chain_backed and ask_ok and not closed
        )
        send_alert = bool(window_open and ask_ok and (chain_backed or symbol not in WEBULL_SYMBOLS))
        card = {
            "symbol": symbol,
            "action": "AFTERNOON_PUTS",
            "desk": LOG_KIND,
            "customer": CUSTOMER_NAME,
            "afternoon_puts": True,
            "right": "P",
            "spot": spot,
            "entry": spot,
            "signaled_at": row["at"],
            "signaled_at_cst": f"{day} {row['at_ct']} CT",
            "day_cst": day,
            "at_ct": row["at_ct"],
            "detail": (
                f"{CUSTOMER_NAME} · session high in, lower high, under VWAP, "
                f"9 EMA under 21, down {rules.hod_drop_pct:g}% from the high · "
                f"signal {row['at_ct']} CT · flat 2:45 PM CT."
            ),
            "exit_plan": EXIT_PLAN,
            "webull": symbol in WEBULL_SYMBOLS,
            "send": send_alert,
            "send_order": send_order,
            "closed": closed,
            "entry_premium": row.get("premium"),
            "exit_premium": row.get("exit_premium"),
            "half_premium": row.get("half_premium"),
            "option_pct": row.get("option_pct"),
            "pnl_usd": row.get("pnl_usd"),
            "exit_reason": row.get("reason"),
            "exit_at_ct": row.get("exit_at_ct"),
            "contracts": row.get("contracts") or 1,
            "priced": row.get("priced"),
            "strike": picked.get("strike"),
            "expiry": picked.get("expiry"),
            "dte": picked.get("dte", 0),
            "ask": picked.get("ask"),
            "bid": picked.get("bid"),
            "contract": picked.get("contract"),
        }
        stamp = remember_detected(
            detected,
            symbol=symbol,
            at_ct=str(row["at_ct"]),
            side="P",
            now=moment,
            active=send_alert,
        )
        if stamp:
            card["detected_at"] = stamp
            card["detected_at_ct"] = format_ct_hms(stamp)
        from odte_scanner.signals.expected_move import attach_em_used

        attach_em_used(card)
        cards.append(card)
    for row in waiting:
        symbol = str(row.get("symbol") or "").upper()
        if any(str(card.get("symbol") or "").upper() == symbol for card in cards):
            continue
        cards.append(
            {
                "symbol": symbol,
                "action": "AFTERNOON_PUTS",
                "desk": LOG_KIND,
                "customer": CUSTOMER_NAME,
                "afternoon_puts": True,
                "breadth": "waiting",
                "right": "P",
                "webull": symbol in WEBULL_SYMBOLS,
                "send": False,
                "send_order": False,
                "closed": False,
                "contracts": 1,
                "at_ct": row.get("at_ct") or row.get("gate_at_ct"),
                "day_cst": day,
                "detail": (
                    f"{CUSTOMER_NAME} · {symbol} met the put gates at "
                    f"{row.get('gate_at_ct')} CT and is waiting for SPY and QQQ "
                    "both to confirm. No order until the second name confirms."
                ),
                "exit_plan": EXIT_PLAN,
            }
        )
    commit_day_store(
        store,
        path,
        _write_store,
        day=day,
        symbols=allowed,
        detected=detected,
        active=bool(window_open and allowed),
    )
    if stopped:
        fresh = _read_store(path)
        fresh["stopped"] = sorted(stopped)
        _write_store(path, fresh)
    return {
        "ok": True,
        "routed": True,
        "day": day,
        "window_open": window_open,
        "cap": rules.daily_cap,
        "phase": phase_for(now_ct, cards, rules),
        "customer": CUSTOMER_NAME,
        "signals": cards,
        "spx_exits": modeled_spx_exits(trades, cards, allowed, tier="afternoon"),
        "qualified": [
            {"symbol": row["symbol"], "at_ct": row["at_ct"], "side": "P", "strike": row.get("strike")}
            for row in sorted(
                trades,
                key=lambda item: (
                    item["at"],
                    PRIORITY.index(item["symbol"]) if item["symbol"] in PRIORITY else 99,
                ),
            )
        ],
        "note": (
            f"{CUSTOMER_NAME}. At most {rules.daily_cap} names, one each. "
            "SPY and QQQ enter together when the second name confirms, at that "
            "bar's prices, through 2:15 PM CT. SPX is not part of that check "
            "and is not a Webull order. Flat 2:45 PM CT."
        ),
    }


def load_afternoon_board(
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    bars: dict[str, list[Bar]] | None = None,
) -> dict[str, Any]:
    """Live entry point. Failures become an empty board so the fast loop keeps going."""
    try:
        if bars is None:
            bars = load_intraday_bars(list(SYMBOLS))
        if not bars:
            moment = now or datetime.now(timezone.utc)
            now_ct = moment.astimezone(CT) if moment.tzinfo else moment.replace(tzinfo=CT)
            return {
                "ok": False,
                "routed": True,
                "signals": [],
                "qualified": [],
                "phase": "waiting" if now_ct.time() < time(10, 0) else "scanning",
                "customer": CUSTOMER_NAME,
                "note": "Afternoon Puts needs intraday bars. None were loaded.",
            }

        def fetch_chain(symbol: str, spot: float, side: str) -> list[dict[str, Any]]:
            return _yahoo_chain(symbol, side, now=now)

        return build_afternoon_board(
            bars,
            now=now,
            store_path=store_path,
            fetch_chain=fetch_chain,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("afternoon puts board failed: %s", exc)
        return {
            "ok": False,
            "routed": True,
            "signals": [],
            "qualified": [],
            "phase": "scanning",
            "customer": CUSTOMER_NAME,
            "error": str(exc)[:200],
        }


def _yahoo_chain(symbol: str, side: str, *, now: datetime | None) -> list[dict[str, Any]]:
    """0DTE puts for one name. Called only after a signal, so this stays small."""
    import yfinance as yf

    yahoo = YAHOO_SYMBOL.get(symbol, symbol)
    ticker = yf.Ticker(yahoo)
    expirations = list(ticker.options or [])
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(CT).date()
    right = "P" if str(side).upper().startswith("P") else "C"
    want = "puts" if right == "P" else "calls"
    rows: list[dict[str, Any]] = []
    for expiry in expirations[:4]:
        try:
            exp = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            continue
        if exp != today:
            continue
        try:
            book = ticker.option_chain(expiry)
            table = book.puts if want == "puts" else book.calls
        except Exception:  # noqa: BLE001
            continue
        if table is None or getattr(table, "empty", True):
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

