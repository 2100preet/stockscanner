"""Pre-market expected move. Display and context only.

The rule of 16: a 1-sigma day is about VIX / sqrt(252). This desk uses 15.87.
VIX 32 is about 2% from the prior close. The morning print is frozen before
the cash open and is not a trading filter. Lunch-Break Credit and Afternoon
Puts keep their live rules. SPX stays an alert. Webull stays on the paper host.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "expected_move_day.json"

# sqrt(252) ≈ 15.8745. The desk uses 15.87 so VIX 32 is about 2% a day.
DIVISOR = 15.87
SYMBOLS: tuple[str, ...] = ("SPX", "SPY", "QQQ")
YAHOO_SYMBOL = {"SPX": "^GSPC", "VIX": "^VIX", "VIX1D": "^VIX1D"}

PREMARKET_START = time(8, 15)
PREMARKET_LOCK = time(8, 25)
PREMARKET_END = time(8, 30)
SEND_START = time(8, 20)
CATCHUP_END = time(9, 0)
RTH_START = time(8, 30)
RTH_END = time(15, 0)
TRACK_END = time(15, 15)

NOTE = (
    "1-sigma expected move is VIX / 15.87, centered on the prior close. "
    "VIX1D and the 0DTE straddle are same-day checks when those feeds answer. "
    "This panel does not change a live order."
)


def daily_move_pct(vol_index: float | None, divisor: float = DIVISOR) -> float | None:
    """Percent, not a fraction. VIX 32.1 → 32.1 / 15.87 ≈ 2.02."""
    number = _pos(vol_index)
    if number is None or divisor <= 0:
        return None
    return number / float(divisor)


def move_points(anchor: float | None, pct: float | None) -> float | None:
    """Convert a percent move into price points off ``anchor``."""
    price = _pos(anchor)
    width = _pos(pct)
    if price is None or width is None:
        return None
    return price * width / 100.0


def one_sigma_band(anchor: float, points: float) -> tuple[float, float]:
    """Low and high of the 1-sigma range around ``anchor``."""
    return float(anchor) - float(points), float(anchor) + float(points)


def move_used_pct(price: float | None, prior_close: float | None, points: float | None) -> float | None:
    """|price − prior close| as a percent of the 1-sigma move. 100 means the full band."""
    last = _pos(price)
    base = _pos(prior_close)
    width = _pos(points)
    if last is None or base is None or width is None:
        return None
    return abs(last - base) / width * 100.0


def range_used_pct(high: float | None, low: float | None, points: float | None) -> float | None:
    """Session high−low as a percent of the 1-sigma move."""
    top = _pos(high)
    bottom = _pos(low)
    width = _pos(points)
    if top is None or bottom is None or width is None or top + 1e-9 < bottom:
        return None
    return (top - bottom) / width * 100.0


def leg_mid(bid: Any, ask: Any) -> float | None:
    """Two-sided mid. A missing or crossed side is not a mid."""
    buy = _pos(bid)
    sell = _pos(ask)
    if buy is None or sell is None or sell + 1e-9 < buy:
        return None
    return (buy + sell) / 2.0


def straddle_implied_pct(
    spot: float | None,
    call_bid: Any = None,
    call_ask: Any = None,
    put_bid: Any = None,
    put_ask: Any = None,
) -> dict[str, float] | None:
    """ATM straddle mid / spot, in percent. None when either wing has no mid."""
    price = _pos(spot)
    call = leg_mid(call_bid, call_ask)
    put = leg_mid(put_bid, put_ask)
    if price is None or call is None or put is None:
        return None
    mid = call + put
    if mid <= 0:
        return None
    return {"mid": round(mid, 4), "pct": round(mid / price * 100.0, 4), "spot": round(price, 4)}


def atm_straddle_from_rows(
    rows: list[dict[str, Any]] | None,
    spot: float | None,
    today: Any,
) -> dict[str, Any] | None:
    """Nearest strike that has a 0DTE call mid and put mid. None if the chain is thin."""
    price = _pos(spot)
    day = _as_date(today)
    if price is None or day is None or not rows:
        return None
    calls: dict[float, dict[str, Any]] = {}
    puts: dict[float, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _as_date(row.get("expiry")) != day:
            continue
        strike = _pos(row.get("strike"))
        if strike is None:
            continue
        right = str(row.get("right") or "").upper()[:1]
        if right == "C":
            calls[strike] = row
        elif right == "P":
            puts[strike] = row
    best: dict[str, Any] | None = None
    best_dist: float | None = None
    for strike, call in calls.items():
        put = puts.get(strike)
        if put is None:
            continue
        implied = straddle_implied_pct(price, call.get("bid"), call.get("ask"), put.get("bid"), put.get("ask"))
        if implied is None:
            continue
        dist = abs(strike - price)
        if best_dist is None or dist < best_dist - 1e-9:
            best_dist = dist
            best = {**implied, "strike": strike}
    return best


def format_em_used(value: Any) -> str | None:
    """Alert context. ``EM used 45%``. Blank when the morning print is missing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return f"EM used {number:.0f}%"


def format_expected_move_message(board: dict[str, Any]) -> str:
    """One pre-market Telegram line, plus VIX1D / straddle when those exist."""
    vix = _pos(board.get("vix"))
    pct = _pos(board.get("pct"))
    if vix is None or pct is None:
        return ""
    by = {str(row.get("symbol") or ""): row for row in (board.get("symbols") or []) if isinstance(row, dict)}
    parts = [f"🧭 EXPECTED MOVE (VIX {vix:.1f} → ±{pct:.1f}%)"]
    spx = by.get("SPX") or {}
    shown = _pos(spx.get("display_points"))
    if shown is not None and spx.get("low") is not None and spx.get("high") is not None:
        parts.append(f"SPX ±{shown:.0f} pts ({_comma(spx['low'])}–{_comma(spx['high'])})")
    spy = by.get("SPY") or {}
    spy_pts = _pos(spy.get("display_points"))
    if spy_pts is not None:
        parts.append(f"SPY ±${spy_pts:.1f}")
    qqq = by.get("QQQ") or {}
    qqq_pts = _pos(qqq.get("display_points"))
    if qqq_pts is not None:
        parts.append(f"QQQ ±${qqq_pts:.1f}")
    if len(parts) == 1:
        return ""
    line = " · ".join(parts)
    extras: list[str] = []
    vix1d = _pos(board.get("vix1d"))
    vix1d_pct = _pos(board.get("vix1d_pct"))
    if vix1d is not None and vix1d_pct is not None:
        extras.append(f"VIX1D {vix1d:.1f} → ±{vix1d_pct:.1f}%")
    straddles = board.get("straddle") if isinstance(board.get("straddle"), dict) else {}
    for symbol in ("SPY", "QQQ"):
        row = straddles.get(symbol) if isinstance(straddles.get(symbol), dict) else None
        if row and _pos(row.get("pct")) is not None:
            extras.append(f"{symbol} straddle ±{float(row['pct']):.1f}%")
    if extras:
        return line + "\n" + " · ".join(extras)
    return line


def format_breach_message(board: dict[str, Any]) -> str:
    """Single intraday note when SPX has used the whole 1-sigma move."""
    by = {str(row.get("symbol") or ""): row for row in (board.get("symbols") or []) if isinstance(row, dict)}
    spx = by.get("SPX") or {}
    used = spx.get("move_used_pct")
    points = _pos(spx.get("display_points"))
    anchor = _pos(spx.get("anchor"))
    last = _pos(spx.get("last"))
    if used is None or points is None or anchor is None or last is None:
        return ""
    return (
        f"🧭 EXPECTED MOVE · SPX used {float(used):.0f}% of its "
        f"±{points:.0f} pt move (prior {_comma(anchor)} → {_comma(last)})"
    )


def build_expected_move(
    *,
    vix: float | None,
    anchors: dict[str, float] | None = None,
    vix1d: float | None = None,
    marks: dict[str, dict[str, Any]] | None = None,
    straddles: dict[str, Any] | None = None,
    anchor_kind: str = "prior_close",
    source: str | None = None,
    day: str | None = None,
    frozen: bool = True,
) -> dict[str, Any]:
    """Public board from already-loaded numbers. No network."""
    state: dict[str, Any] = {
        "day": day,
        "vix": vix,
        "vix1d": vix1d,
        "source": source,
        "anchors": {},
        "marks": marks or {},
        "straddle": straddles or {},
        "frozen": frozen,
    }
    pct = daily_move_pct(vix)
    for symbol, raw in (anchors or {}).items():
        price = _pos(raw)
        if price is None or pct is None:
            continue
        state["anchors"][str(symbol).upper()] = {
            "price": price,
            "kind": anchor_kind,
            "points": price * pct / 100.0,
        }
    return public_board(state)


def public_board(state: dict[str, Any] | None) -> dict[str, Any]:
    """Portal / snapshot shape. Marks are the latest tape. The morning print stays put."""
    blob = state if isinstance(state, dict) else {}
    vix = _pos(blob.get("vix"))
    pct = daily_move_pct(vix)
    vix1d = _pos(blob.get("vix1d"))
    vix1d_pct = daily_move_pct(vix1d)
    marks = blob.get("marks") if isinstance(blob.get("marks"), dict) else {}
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        anchor = (blob.get("anchors") or {}).get(symbol) if isinstance(blob.get("anchors"), dict) else None
        anchor = anchor if isinstance(anchor, dict) else {}
        price = _pos(anchor.get("price"))
        points = _pos(anchor.get("points"))
        if price is not None and pct is not None and points is None:
            points = price * pct / 100.0
        mark = marks.get(symbol) if isinstance(marks.get(symbol), dict) else {}
        last = _pos(mark.get("last"))
        high = _pos(mark.get("high") if mark.get("high") is not None else mark.get("day_high"))
        low = _pos(mark.get("low") if mark.get("low") is not None else mark.get("day_low"))
        shown = None if points is None else _display_points(symbol, points)
        row: dict[str, Any] = {
            "symbol": symbol,
            "anchor": None if price is None else round(price, 4),
            "anchor_kind": anchor.get("kind") or None,
            "points": None if points is None else round(points, 4),
            "display_points": shown,
            "low": None if price is None or shown is None else round(price - shown, 4),
            "high": None if price is None or shown is None else round(price + shown, 4),
            "last": None if last is None else round(last, 4),
            "move_used_pts": None,
            "move_used_pct": None,
            "range_pts": None,
            "range_used_pct": None,
            "session_high": None if high is None else round(high, 4),
            "session_low": None if low is None else round(low, 4),
        }
        used = move_used_pct(last, price, points)
        if used is not None and last is not None and price is not None:
            row["move_used_pts"] = round(abs(last - price), 4)
            row["move_used_pct"] = round(used, 2)
        span = range_used_pct(high, low, points)
        if span is not None and high is not None and low is not None:
            row["range_pts"] = round(high - low, 4)
            row["range_used_pct"] = round(span, 2)
        rows.append(row)
    ok = bool(vix and pct and any(row.get("points") for row in rows))
    board: dict[str, Any] = {
        "ok": ok,
        "day": blob.get("day"),
        "vix": None if vix is None else round(vix, 4),
        "vix1d": None if vix1d is None else round(vix1d, 4),
        "pct": None if pct is None else round(pct, 4),
        "vix1d_pct": None if vix1d_pct is None else round(vix1d_pct, 4),
        "divisor": DIVISOR,
        "source": blob.get("source"),
        "frozen": bool(blob.get("frozen")),
        "symbols": rows,
        "straddle": blob.get("straddle") if isinstance(blob.get("straddle"), dict) else {},
        "telegram_sent": bool(blob.get("telegram_sent")),
        "breach_sent": bool(blob.get("breach_sent")),
        "note": NOTE,
    }
    board["message"] = format_expected_move_message(board) if ok else ""
    spx = next((row for row in rows if row["symbol"] == "SPX"), {})
    breached = bool(spx.get("move_used_pct") is not None and float(spx["move_used_pct"]) >= 100.0)
    board["breach"] = {
        "spx": breached,
        "sent": bool(blob.get("breach_sent")),
        "message": format_breach_message(board) if breached else "",
    }
    return board


def in_premarket_window(now: datetime | None = None) -> bool:
    """Weekday 8:15–8:30 CT, before the cash open."""
    moment = _ct(now)
    if moment.weekday() >= 5:
        return False
    clock = moment.time()
    return PREMARKET_START <= clock < PREMARKET_END


def em_used_pct_at(symbol: str, price: Any, *, store_path: Path | None = None) -> float | None:
    """Percent of the frozen morning move already used at ``price``."""
    state = read_state(store_path)
    anchors = state.get("anchors") if isinstance(state.get("anchors"), dict) else {}
    anchor = anchors.get(str(symbol or "").upper())
    if not isinstance(anchor, dict):
        return None
    return move_used_pct(price, anchor.get("price"), anchor.get("points"))


def attach_em_used(card: dict[str, Any], *, store_path: Path | None = None) -> None:
    """Stamp ``em_used_pct`` on a card. A missing morning print leaves the card alone."""
    if not isinstance(card, dict):
        return
    try:
        price = card.get("entry") if card.get("entry") not in (None, "") else card.get("spot")
        pct = em_used_pct_at(str(card.get("symbol") or ""), price, store_path=store_path)
    except Exception:  # noqa: BLE001
        return
    if pct is None:
        return
    card["em_used_pct"] = round(float(pct), 1)


def read_state(path: Path | None = None) -> dict[str, Any]:
    dest = path or DEFAULT_STORE
    try:
        blob = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    dest = path or DEFAULT_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(state), encoding="utf-8")


def load_public_board(path: Path | None = None) -> dict[str, Any]:
    """Snapshot block from the day file. No network."""
    state = read_state(path)
    if not state:
        return {
            "ok": False,
            "symbols": [],
            "straddle": {},
            "vix": None,
            "pct": None,
            "note": "Expected move prints around 8:15 CT.",
        }
    return public_board(state)


def run_expected_move_cycle(
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    fetch_vol: Callable[[], dict[str, Any]] | None = None,
    fetch_marks: Callable[[], dict[str, Any]] | None = None,
    fetch_straddle: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    publish: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Refresh the morning print, send it once, and flag one SPX breach.

    The print freezes at 8:25 CT (or on the first later look). The Telegram
    line goes out once between 8:20 and 9:00 CT. After that the tape updates
    the percent used and nothing else. A quiet feed does not block the desks.
    """
    moment = _ct(now)
    path = store_path or DEFAULT_STORE
    sender = send or _default_send
    publisher = publish or _default_publish
    if moment.weekday() >= 5:
        board = load_public_board(path)
        _safe_publish(publisher, board)
        return board

    day = moment.date().isoformat()
    state = read_state(path)
    if str(state.get("day") or "") != day:
        state = {"day": day}
    clock = moment.time()
    vol_fetch = fetch_vol or default_fetch_vol
    mark_fetch = fetch_marks or default_fetch_marks
    straddle_fetch = fetch_straddle or default_fetch_straddle

    need_print = not state.get("frozen") and PREMARKET_START <= clock < TRACK_END
    if need_print:
        try:
            vol = vol_fetch() or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("expected move vol fetch failed: %s", exc)
            vol = {}
        try:
            marks = mark_fetch() or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("expected move quote fetch failed: %s", exc)
            marks = {}
        _absorb_print(state, vol, marks, moment)
        if state.get("vix") and not state.get("straddle_checked"):
            try:
                found = straddle_fetch(state) or {}
            except Exception as exc:  # noqa: BLE001
                logger.info("expected move straddle fetch failed: %s", exc)
                found = {}
            if not isinstance(found, dict):
                found = {}
            # A thin pre-market chain can show up a few minutes later. Stop retrying
            # once the print locks, and keep going with no straddle if it never does.
            if found or clock >= PREMARKET_LOCK or state.get("frozen"):
                state["straddle"] = found
                state["straddle_checked"] = True
        # Lock the print once every name has a prior close, or at 8:29 CT with
        # whatever names arrived. A VIX print with no quotes stays open so the
        # next cycle can still fill the anchors.
        if state.get("vix") and _anchors_complete(state) and clock >= PREMARKET_LOCK:
            state["frozen"] = True
        elif state.get("vix") and _anchors_ready(state) and clock >= time(8, 29):
            state["frozen"] = True
    elif state.get("vix") and PREMARKET_START <= clock < TRACK_END:
        try:
            marks = mark_fetch() or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("expected move quote refresh failed: %s", exc)
            marks = {}
        if isinstance(marks, dict) and marks:
            state["marks"] = _slim_marks(marks)

    if (
        state.get("frozen")
        and not state.get("telegram_sent")
        and SEND_START <= clock < CATCHUP_END
        and _anchors_ready(state)
    ):
        board = public_board(state)
        text = str(board.get("message") or "")
        if text:
            result = _safe_send(sender, text)
            if result.get("ok"):
                state["telegram_sent"] = True
                state["telegram_text"] = text

    board = public_board(state)
    breach = board.get("breach") if isinstance(board.get("breach"), dict) else {}
    if (
        state.get("frozen")
        and breach.get("spx")
        and not state.get("breach_sent")
        and RTH_START <= clock < RTH_END
    ):
        text = str(breach.get("message") or "")
        if text:
            result = _safe_send(sender, text)
            if result.get("ok"):
                state["breach_sent"] = True
                state["breach_text"] = text
                board = public_board(state)

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        write_state(state, path)
    except OSError as exc:
        logger.info("expected move store write failed: %s", exc)
    board = public_board(state)
    _safe_publish(publisher, board)
    return board


def default_fetch_vol() -> dict[str, Any]:
    """CBOE delayed VIX and VIX1D. Yahoo is only the fallback. No new key."""
    vix = None
    vix1d = None
    source = None
    try:
        from odte_scanner.signals.free_feeds import fetch_cboe_vol_term

        levels = (fetch_cboe_vol_term(timeout=8) or {}).get("levels") or {}
        vix = _pos((levels.get("VIX") or {}).get("last"))
        vix1d = _pos((levels.get("VIX1D") or {}).get("last"))
        if vix is not None:
            source = "cboe"
    except Exception as exc:  # noqa: BLE001
        logger.info("cboe vol term failed: %s", exc)
    if vix is None:
        vix = _yahoo_last("^VIX")
        if vix is not None:
            source = "yahoo"
    if vix1d is None:
        vix1d = _yahoo_last("^VIX1D")
    return {"vix": vix, "vix1d": vix1d, "source": source}


_MARK_CACHE: dict[str, Any] = {"at": 0.0, "marks": None}


def default_fetch_marks() -> dict[str, Any]:
    """Prior close and the latest tape for SPX, SPY, and QQQ.

    Quotes are the same Yahoo chart feed the desk already uses, with a short
    timeout and a 20-second cache so this cycle does not hold up the lunch
    and afternoon loops.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    now = time.monotonic()
    cached = _MARK_CACHE.get("marks")
    if isinstance(cached, dict) and cached and now - float(_MARK_CACHE.get("at") or 0) < 20:
        return cached
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = pool.map(_yahoo_mark, SYMBOLS)
        for symbol, quote in zip(SYMBOLS, fetched):
            if quote:
                out[symbol] = quote
    if "SPX" not in out:
        cboe = _cboe_index("SPX")
        if cboe:
            out["SPX"] = cboe
    if out:
        _MARK_CACHE["at"] = now
        _MARK_CACHE["marks"] = out
        return out
    return cached if isinstance(cached, dict) else {}


def default_fetch_straddle(state: dict[str, Any]) -> dict[str, Any]:
    """ATM 0DTE straddle mid / spot for SPY and QQQ. Missing chain → empty."""
    from odte_scanner.options.yahoo_session import fetch_option_chain

    day = _as_date(state.get("day"))
    if day is None:
        return {}
    out: dict[str, Any] = {}
    marks = state.get("marks") if isinstance(state.get("marks"), dict) else {}
    anchors = state.get("anchors") if isinstance(state.get("anchors"), dict) else {}
    for symbol in ("SPY", "QQQ"):
        mark = marks.get(symbol) if isinstance(marks.get(symbol), dict) else {}
        anchor = anchors.get(symbol) if isinstance(anchors.get(symbol), dict) else {}
        spot = _pos(mark.get("last")) or _pos(anchor.get("price"))
        if spot is None:
            continue
        try:
            payload = fetch_option_chain(symbol, expiry=day.isoformat(), use_cache=True, max_age_minutes=30)
        except Exception as exc:  # noqa: BLE001
            logger.info("straddle chain %s failed: %s", symbol, exc)
            payload = None
        found = straddle_from_yahoo(payload, spot, day)
        if found:
            out[symbol] = found
    return out


def straddle_from_yahoo(payload: dict[str, Any] | None, spot: float, today: Any) -> dict[str, Any] | None:
    """Pull the ATM 0DTE wings out of a Yahoo option-chain payload."""
    if not isinstance(payload, dict):
        return None
    day = _as_date(today)
    if day is None:
        return None
    rows: list[dict[str, Any]] = []
    for book in payload.get("options") or []:
        if not isinstance(book, dict):
            continue
        if not _yahoo_expiry_matches(book.get("expirationDate"), day):
            continue
        for right, key in (("C", "calls"), ("P", "puts")):
            for opt in book.get(key) or []:
                if not isinstance(opt, dict):
                    continue
                rows.append(
                    {
                        "right": right,
                        "strike": opt.get("strike"),
                        "expiry": day.isoformat(),
                        "bid": opt.get("bid"),
                        "ask": opt.get("ask"),
                    }
                )
    return atm_straddle_from_rows(rows, spot, day)


def _absorb_print(state: dict[str, Any], vol: dict[str, Any], marks: dict[str, Any], moment: datetime) -> None:
    vix = _pos(vol.get("vix"))
    if vix is not None:
        state["vix"] = vix
        state["source"] = vol.get("source") or state.get("source")
    vix1d = _pos(vol.get("vix1d"))
    if vix1d is not None:
        state["vix1d"] = vix1d
    if isinstance(marks, dict) and marks:
        state["marks"] = _slim_marks(marks)
        anchors = state.get("anchors") if isinstance(state.get("anchors"), dict) else {}
        pct = daily_move_pct(state.get("vix"))
        if pct is not None and not state.get("frozen"):
            for symbol in SYMBOLS:
                mark = marks.get(symbol) if isinstance(marks.get(symbol), dict) else None
                if not mark:
                    continue
                price = _pos(mark.get("prev_close")) or _pos(mark.get("last"))
                if price is None:
                    continue
                kind = "prior_close" if _pos(mark.get("prev_close")) else "premarket"
                anchors[symbol] = {"price": price, "kind": kind, "points": price * pct / 100.0}
            state["anchors"] = anchors
    state["day"] = moment.date().isoformat()


def _slim_marks(marks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for symbol, row in marks.items():
        if not isinstance(row, dict):
            continue
        out[str(symbol).upper()] = {
            "last": row.get("last"),
            "prev_close": row.get("prev_close"),
            "high": row.get("high", row.get("day_high")),
            "low": row.get("low", row.get("day_low")),
        }
    return out


def _anchors_ready(state: dict[str, Any]) -> bool:
    anchors = state.get("anchors") if isinstance(state.get("anchors"), dict) else {}
    return any(_pos((anchors.get(symbol) or {}).get("points")) for symbol in SYMBOLS)


def _anchors_complete(state: dict[str, Any]) -> bool:
    anchors = state.get("anchors") if isinstance(state.get("anchors"), dict) else {}
    return all(_pos((anchors.get(symbol) or {}).get("points")) for symbol in SYMBOLS)


def _display_points(symbol: str, points: float) -> float:
    if symbol == "SPX":
        return float(round(points))
    return round(float(points), 1)


def _comma(value: Any) -> str:
    return f"{round(float(value)):,.0f}"


def _pos(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _as_date(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not isinstance(value, str):
        return value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _yahoo_expiry_matches(raw: Any, day) -> bool:
    try:
        stamp = int(raw)
    except (TypeError, ValueError):
        return _as_date(raw) == day
    utc = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
    et = datetime.fromtimestamp(stamp, tz=ZoneInfo("America/New_York")).date()
    return day in {utc, et}


def _ct(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    return moment.astimezone(CT)


def _yahoo_mark(symbol: str) -> dict[str, Any] | None:
    """One chart quote. Four-second cap so a slow feed cannot stall the desks."""
    import requests

    yahoo = YAHOO_SYMBOL.get(symbol, symbol)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}"
        "?interval=1m&range=1d&includePrePost=true"
    )
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        response.raise_for_status()
        result = ((response.json().get("chart") or {}).get("result") or [None])[0] or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("expected move quote %s failed: %s", symbol, exc)
        return None
    meta = result.get("meta") if isinstance(result, dict) else {}
    if not isinstance(meta, dict):
        return None
    last = _pos(meta.get("regularMarketPrice") or meta.get("postMarketPrice") or meta.get("preMarketPrice"))
    prev = _pos(meta.get("chartPreviousClose") or meta.get("previousClose"))
    if last is None and prev is None:
        return None
    pre = _pos(meta.get("preMarketPrice"))
    # Before the cash open the regular price is often still the prior close.
    # Prefer the pre-market print as the live mark when it is present.
    if pre is not None and last is not None and abs(last - (prev or last)) < 1e-6:
        last = pre
    return {
        "last": last or prev,
        "prev_close": prev or last,
        "high": _pos(meta.get("regularMarketDayHigh")),
        "low": _pos(meta.get("regularMarketDayLow")),
        "session": "yahoo_chart",
    }


def _yahoo_last(symbol: str) -> float | None:
    try:
        from odte_scanner.options.yahoo_session import fetch_yahoo_quote

        quote = fetch_yahoo_quote(symbol)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(quote, dict):
        return None
    return _pos(quote.get("last"))


def _cboe_index(symbol: str) -> dict[str, Any] | None:
    try:
        import requests

        response = requests.get(
            f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{symbol}.json",
            timeout=8,
        )
        response.raise_for_status()
        data = (response.json().get("data") or response.json())
    except Exception as exc:  # noqa: BLE001
        logger.info("cboe %s quote failed: %s", symbol, exc)
        return None
    if not isinstance(data, dict):
        return None
    last = _pos(data.get("current_price") or data.get("last") or data.get("close"))
    prev = _pos(data.get("prev_day_close") or data.get("previous_close") or data.get("prev_close"))
    if prev is None and last is not None:
        try:
            change = float(data.get("price_change"))
        except (TypeError, ValueError):
            change = None
        if change is not None:
            prev = _pos(last - change)
    if last is None and prev is None:
        return None
    return {
        "last": last or prev,
        "prev_close": prev or last,
        "high": _pos(data.get("high") or data.get("day_high")),
        "low": _pos(data.get("low") or data.get("day_low")),
        "session": "cboe",
    }


def _default_send(text: str) -> dict[str, Any]:
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text)


def _safe_send(send: Callable[[str], dict[str, Any]], text: str) -> dict[str, Any]:
    try:
        result = send(text) or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("expected move telegram failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}
    return result if isinstance(result, dict) else {"ok": False}


def _default_publish(board: dict[str, Any]) -> None:
    from odte_scanner.hot_state import publish_hot

    publish_hot(expected_move=board)


def _safe_publish(publish: Callable[[dict[str, Any]], None], board: dict[str, Any]) -> None:
    try:
        publish(board)
    except Exception as exc:  # noqa: BLE001
        logger.info("expected move publish failed: %s", exc)
