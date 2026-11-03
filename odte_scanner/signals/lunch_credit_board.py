"""Today's Lunch-Break Credit cards. No network in the pure helpers.

SPY and QQQ can be a Webull paper credit spread. SPX is a Telegram alert.
A card is sent only after its 5-minute bar has closed, and only inside
12:00–14:15 CT. The order path is the Webull vertical combo.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.signals.detected import commit_day_store, day_detected, format_ct_hms, remember_detected
from odte_scanner.signals.diamond import YAHOO_SYMBOL, Bar
from odte_scanner.signals.diamond_board import load_intraday_bars, occ_symbol
from odte_scanner.signals.lunch_credit import (
    LOG_KIND,
    ORDER_PATH,
    PRIMARY_RULES,
    PRIORITY,
    SYMBOLS,
    WEBULL_SYMBOLS,
    LunchRules,
    leg_mid,
    long_put,
    max_risk_usd,
    natural_credit,
    opening_limit,
    replay,
    spread_mid,
)
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "lunch_credit_day.json"
CUSTOMER_NAME = "🌤️ Lunch-Break Credit"
BAR_MINUTES = 5
EXIT_PLAN = (
    "EXIT plan (lunch credit) · buy the spread back at 50% of the credit · "
    "stop if the mark reaches 2× the credit · flat 2:45 PM CT. One spread. "
    "SPX is not a Webull order."
)


def bar_is_closed(bar: Bar, now: datetime) -> bool:
    stamp = bar.ct()
    if now.tzinfo is None:
        return False
    return stamp + timedelta(minutes=BAR_MINUTES) <= now.astimezone(stamp.tzinfo)


def _quote(row: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not isinstance(row, dict):
        return None, None
    try:
        bid = float(row.get("bid")) if row.get("bid") not in (None, "") else None
    except (TypeError, ValueError):
        bid = None
    try:
        ask = float(row.get("ask")) if row.get("ask") not in (None, "") else None
    except (TypeError, ValueError):
        ask = None
    return bid, ask


def choose_put(symbol: str, strike: float, chain: list[dict[str, Any]] | None, *, today: date) -> dict[str, Any] | None:
    """0DTE put at ``strike``. The nearest listed strike wins when the exact one is missing."""
    if strike <= 0 or not chain:
        return None
    best: dict[str, Any] | None = None
    best_key: tuple[int, float] | None = None
    for row in chain:
        if not isinstance(row, dict):
            continue
        if str(row.get("right") or "P").upper()[:1] != "P":
            continue
        try:
            listed = float(row.get("strike"))
            expiry = str(row.get("expiry") or "")[:10]
            exp = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            continue
        if (exp - today).days != 0 or listed <= 0:
            continue
        exact = 0 if abs(listed - strike) < 0.02 else 1
        key = (exact, abs(listed - strike))
        if best_key is None or key < best_key:
            best_key = key
            bid, ask = _quote(row)
            best = {
                "strike": listed,
                "expiry": expiry,
                "bid": bid,
                "ask": ask,
                "contract": str(row.get("contract") or "") or occ_symbol(symbol, expiry, listed, "P"),
                "right": "P",
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


def phase_for(now_ct: datetime, cards: list[dict[str, Any]], rules: LunchRules) -> str:
    """waiting, tracking, scanning, triggered, or closed."""
    clock = now_ct.time()
    if clock < rules.range_start:
        return "waiting"
    if clock < rules.range_end and not cards:
        return "tracking"
    if not cards:
        return "scanning" if clock < rules.entry_end else "closed"
    if any(not row.get("closed") for row in cards):
        return "triggered"
    return "closed"


def build_lunch_board(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    chains: dict[str, list[dict[str, Any]]] | None = None,
    fetch_chain: Callable[[str, float, str], list[dict[str, Any]]] | None = None,
    rules: LunchRules | None = None,
) -> dict[str, Any]:
    """Capped cards for the session. ``send`` waits until the break bar closes."""
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
    trades = [row for row in replay(visible, rules) if row.get("day") == day]
    path = store_path or DEFAULT_STORE
    store = _read_store(path)
    chain_book = chains or {}
    detected = day_detected(store, day)
    cards: list[dict[str, Any]] = []
    for row in trades:
        symbol = str(row["symbol"]).upper()
        spot = float(row["entry"])
        chain = chain_book.get(symbol)
        if chain is None and fetch_chain is not None and window_open and symbol in WEBULL_SYMBOLS:
            try:
                chain = fetch_chain(symbol, spot, "P")
            except Exception as exc:  # noqa: BLE001
                logger.info("lunch credit chain %s failed: %s", symbol, exc)
                chain = None
        short_target = float(row["short_strike"])
        long_target = float(row["long_strike"])
        short = choose_put(symbol, short_target, chain, today=now_ct.date())
        long = choose_put(symbol, long_target, chain, today=now_ct.date())
        expiry = day
        if short is None:
            short = {
                "strike": short_target,
                "expiry": expiry,
                "bid": None,
                "ask": None,
                "contract": occ_symbol(symbol, expiry, short_target, "P"),
            }
        if long is None:
            long = {
                "strike": long_target,
                "expiry": expiry,
                "bid": None,
                "ask": None,
                "contract": occ_symbol(symbol, expiry, long_target, "P"),
            }
        # A chain that only has one of the two strikes is not a spread.
        if abs(float(short["strike"]) - float(long["strike"])) < 0.5:
            long = {
                "strike": long_put(float(short["strike"]), rules.width),
                "expiry": short.get("expiry") or expiry,
                "bid": None,
                "ask": None,
                "contract": occ_symbol(symbol, str(short.get("expiry") or expiry)[:10], long_put(float(short["strike"]), rules.width), "P"),
            }
        mid = spread_mid(short.get("bid"), short.get("ask"), long.get("bid"), long.get("ask"))
        natural = natural_credit(short.get("bid"), long.get("ask"))
        limit = opening_limit(mid, natural)
        quoted = mid is not None and mid > 0 and limit is not None
        send_order = bool(window_open and symbol in WEBULL_SYMBOLS and quoted)
        send_alert = bool(window_open and (quoted or symbol not in WEBULL_SYMBOLS))
        width = abs(float(short["strike"]) - float(long["strike"]))
        credit = float(mid or 0)
        card = {
            "symbol": symbol,
            "action": "LUNCH_CREDIT",
            "desk": LOG_KIND,
            "customer": CUSTOMER_NAME,
            "lunch_credit": True,
            "right": "P",
            "spot": spot,
            "entry": spot,
            "signaled_at_cst": f"{day} {row['at_ct']} CT",
            "day_cst": day,
            "at_ct": row["at_ct"],
            "range_high": row.get("range_high"),
            "detail": (
                f"{CUSTOMER_NAME} · first 5-minute close above the "
                f"10:00–12:00 CT range high {row.get('range_high')} · "
                f"signal {row['at_ct']} CT · flat 2:45 PM CT."
            ),
            "exit_plan": EXIT_PLAN,
            "webull": symbol in WEBULL_SYMBOLS,
            "send": send_alert,
            "send_order": send_order,
            "closed": False,
            "contracts": row.get("contracts") or 1,
            "order_path": ORDER_PATH,
            "width": width,
            "credit": round(credit, 4) if credit else None,
            "credit_mid": None if mid is None else round(mid, 4),
            "credit_natural": None if natural is None else round(natural, 4),
            "limit_price": limit,
            "max_risk_usd": max_risk_usd(credit, width=width, contracts=int(row.get("contracts") or 1)) if credit else None,
            "short_strike": short.get("strike"),
            "long_strike": long.get("strike"),
            "strike": short.get("strike"),
            "expiry": short.get("expiry") or expiry,
            "dte": 0,
            "bid": short.get("bid"),
            "ask": short.get("ask"),
            "contract": short.get("contract"),
            "short": {
                "strike": short.get("strike"),
                "bid": short.get("bid"),
                "ask": short.get("ask"),
                "contract": short.get("contract"),
                "expiry": short.get("expiry") or expiry,
            },
            "long": {
                "strike": long.get("strike"),
                "bid": long.get("bid"),
                "ask": long.get("ask"),
                "contract": long.get("contract"),
                "expiry": long.get("expiry") or expiry,
            },
            "short_mid": leg_mid(short.get("bid"), short.get("ask")),
            "long_mid": leg_mid(long.get("bid"), long.get("ask")),
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
        cards.append(card)
    allowed = [str(row["symbol"]).upper() for row in cards]
    commit_day_store(
        store,
        path,
        _write_store,
        day=day,
        symbols=allowed,
        detected=detected,
        active=bool(window_open and allowed),
    )
    return {
        "ok": True,
        "routed": True,
        "day": day,
        "window_open": window_open,
        "cap": rules.daily_cap,
        "phase": phase_for(now_ct, cards, rules),
        "customer": CUSTOMER_NAME,
        "order_path": ORDER_PATH,
        "signals": cards,
        "qualified": [
            {
                "symbol": row["symbol"],
                "at_ct": row["at_ct"],
                "short_strike": row.get("short_strike"),
                "long_strike": row.get("long_strike"),
            }
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
            "The range high is tracked from 10:00 to 12:00 CT. "
            "The first 5-minute close above it, through 2:15 PM CT, sells the "
            "ATM put and buys the put $2 lower. SPX is not a Webull order. Flat 2:45 PM CT."
        ),
    }


def load_lunch_board(
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    bars: dict[str, list[Bar]] | None = None,
) -> dict[str, Any]:
    """Live entry point. Failures become an error board so the fast loop keeps going."""
    try:
        if bars is None:
            bars = load_intraday_bars(list(SYMBOLS))
        if not bars:
            moment = now or datetime.now(timezone.utc)
            now_ct = moment.astimezone(CT) if moment.tzinfo else moment.replace(tzinfo=CT)
            phase = "waiting" if now_ct.time() < time(10, 0) else "tracking"
            return {
                "ok": False,
                "routed": True,
                "signals": [],
                "qualified": [],
                "phase": phase,
                "customer": CUSTOMER_NAME,
                "order_path": ORDER_PATH,
                "note": "Lunch-Break Credit needs intraday bars. None were loaded.",
            }

        def fetch_chain(symbol: str, spot: float, side: str) -> list[dict[str, Any]]:
            return _yahoo_chain(symbol, side, now=now)

        return build_lunch_board(
            bars,
            now=now,
            store_path=store_path,
            fetch_chain=fetch_chain,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("lunch credit board failed: %s", exc)
        return {
            "ok": False,
            "routed": True,
            "signals": [],
            "qualified": [],
            "phase": "scanning",
            "customer": CUSTOMER_NAME,
            "order_path": ORDER_PATH,
            "error": str(exc)[:200],
        }


def _yahoo_chain(symbol: str, side: str, *, now: datetime | None) -> list[dict[str, Any]]:
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
