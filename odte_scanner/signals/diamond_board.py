"""Build today's Diamond cards from intraday bars. No network in the pure helpers.

The live loader tries Tradier 5-minute timesales, then Yahoo. A card is sent
only inside the entry window, only for the first three symbols of the day,
and only with a real ask. SPX is a signal, not a Webull order.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.signals.detected import (
    commit_day_store,
    day_detected,
    format_ct_hms,
    remember_detected,
)
from odte_scanner.signals.diamond import (
    DAILY_CAP,
    INDEX_ETFS,
    PRIORITY,
    PRIMARY_RULES,
    UNIVERSE,
    YAHOO_SYMBOL,
    Bar,
    apply_daily_cap,
    diamond_routed,
    group_sessions,
    index_market,
    run_universe,
)
from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "diamond_day.json"
EXIT_PLAN = (
    "EXIT plan (diamond) · sell all at +30% premium · stop −40% · flat 10:30 AM CT. "
    "In-sample model, not a promise."
)
_MODELED_EXIT_REASONS = frozenset({"stop", "target", "flat", "trail", "be"})


def modeled_spx_exits(
    trades: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    allowed: list[str],
    *,
    tier: str = "diamond",
) -> list[dict[str, Any]]:
    """SPX is alert-only. The exit alert uses this model's option result."""
    if "SPX" not in {str(sym).upper() for sym in allowed}:
        return []
    card = next((row for row in cards if str(row.get("symbol") or "").upper() == "SPX"), {})
    out: list[dict[str, Any]] = []
    for row in trades:
        if str(row.get("symbol") or "").upper() != "SPX":
            continue
        reason = str(row.get("reason") or "")
        if reason not in _MODELED_EXIT_REASONS:
            continue
        out.append(
            {
                "symbol": "SPX",
                "reason": reason,
                "option_pct": row.get("option_pct"),
                "premium": row.get("premium"),
                "at_ct": row.get("at_ct"),
                "exit_at_ct": row.get("exit_at_ct"),
                "day": row.get("day"),
                "right": row.get("side") or card.get("right") or "C",
                "strike": card.get("strike"),
                "expiry": card.get("expiry"),
                "ask": card.get("ask"),
                "contracts": 1,
                "order_id": f"spx-{tier}-{row.get('day')}",
                "tier": tier,
            }
        )
    return out


def occ_symbol(symbol: str, expiry: str, strike: float, right: str = "C") -> str:
    day = str(expiry)[:10].replace("-", "")
    yymmdd = day[2:8]
    px = int(round(float(strike) * 1000))
    return f"{str(symbol).upper()}{yymmdd}{str(right).upper()[:1]}{px:08d}"


def choose_call(
    symbol: str,
    spot: float,
    chain: list[dict[str, Any]] | None,
    *,
    today: date,
) -> dict[str, Any] | None:
    """ATM or one strike in the money. Index products stay inside 2 DTE."""
    if spot <= 0 or not chain:
        return None
    index = symbol in INDEX_ETFS
    best: dict[str, Any] | None = None
    best_key: tuple[int, float] | None = None
    for row in chain:
        if not isinstance(row, dict):
            continue
        right = str(row.get("right") or "C").upper()[:1]
        if right == "P":
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
        # At the money or in the money. A strike above the spot is only used
        # when the chain has nothing at or below it.
        otm = strike > spot
        gap = strike - spot if otm else spot - strike
        # In-the-money first. An out-of-the-money strike is only a fallback.
        key = (1 if otm else 0, dte, gap)
        if best_key is None or key < best_key:
            best_key = key
            ask = row.get("ask")
            bid = row.get("bid")
            best = {
                "symbol": symbol,
                "right": "C",
                "strike": strike,
                "expiry": expiry,
                "dte": dte,
                "ask": ask,
                "bid": bid,
                "contract": str(row.get("contract") or "") or occ_symbol(symbol, expiry, strike),
                "spot": spot,
            }
    return best


def _lock_symbols(selected: list[dict[str, Any]], store: dict[str, Any], day: str) -> list[str]:
    """Keep the names already sent today, then fill empty slots in signal order."""
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


def index_opening_range(
    bars_by_symbol: dict[str, list[Bar]],
    day: str,
    *,
    start: time,
    end: time,
    min_bars: int,
) -> dict[str, dict[str, float]]:
    """SPY, QQQ, and SPX high/low once the last bar of the range has printed."""
    last_needed = (datetime.combine(date.today(), end) - timedelta(minutes=5)).time()
    levels: dict[str, dict[str, float]] = {}
    for symbol in ("SPY", "QQQ", "SPX"):
        bars = [
            bar
            for bar in bars_by_symbol.get(symbol) or []
            if bar.ct().date().isoformat() == day and start <= bar.ct().time() < end
        ]
        if len(bars) < min_bars:
            continue
        if max(bar.ct().time() for bar in bars) < last_needed:
            continue
        levels[symbol] = {
            "high": round(max(bar.high for bar in bars), 2),
            "low": round(min(bar.low for bar in bars), 2),
        }
    return levels


def _write_store(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob), encoding="utf-8")


def build_diamond_board(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    chains: dict[str, list[dict[str, Any]]] | None = None,
    fetch_chain: Callable[[str, float], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Today's capped cards. ``send`` is false once the 10:30 CT window is over."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    now_ct = moment.astimezone(CT)
    day = now_ct.date().isoformat()
    routed = diamond_routed()
    window_open = time(9, 0) <= now_ct.time() < time(10, 30)
    path = store_path or DEFAULT_STORE
    store = _read_store(path)
    trades = [
        row
        for row in run_universe(bars_by_symbol)
        if row.get("day") == day and str(row.get("at") or "") <= moment.astimezone(timezone.utc).isoformat()
    ]
    # A bar stamped at 09:45 is known once that bar has closed. Compare in UTC.
    ranked = apply_daily_cap(trades)
    allowed = _lock_symbols(ranked, store, day)
    # Only persist a lock while a send can still happen, so a late scan does
    # not consume tomorrow's names or backfill a missed morning.
    detected = day_detected(store, day)
    commit_day_store(
        store,
        path,
        _write_store,
        day=day,
        symbols=allowed,
        detected=detected,
        active=bool(window_open and routed and allowed),
    )
    chain_book = chains or {}
    cards: list[dict[str, Any]] = []
    for row in ranked:
        symbol = str(row["symbol"]).upper()
        if symbol not in allowed:
            continue
        spot = float(row["entry"])
        chain = chain_book.get(symbol)
        if chain is None and fetch_chain is not None and window_open:
            try:
                chain = fetch_chain(symbol, spot)
            except Exception as exc:  # noqa: BLE001
                logger.info("diamond chain %s failed: %s", symbol, exc)
                chain = None
        picked = choose_call(symbol, spot, chain, today=now_ct.date())
        ask = None if picked is None else picked.get("ask")
        try:
            ask_ok = ask is not None and float(ask) > 0
        except (TypeError, ValueError):
            ask_ok = False
        send = bool(routed and window_open and symbol in allowed and ask_ok and symbol != "SPX")
        # SPX still alerts. The Webull skip is separate from Telegram.
        send_alert = bool(routed and window_open and symbol in allowed and ask_ok)
        card = {
            "symbol": symbol,
            "action": "DIAMOND",
            "desk": "Diamond",
            "diamond": True,
            "right": "C",
            "spot": spot,
            "entry": spot,
            "signaled_at": row["at"],
            "signaled_at_cst": f"{day} {row['at_ct']} CT",
            "day_cst": day,
            "at_ct": row["at_ct"],
            "detail": (
                f"Diamond · OR break, VWAP hold, higher low · signal {row['at_ct']} CT · "
                "flat 10:30 CT. In-sample."
            ),
            "exit_plan": EXIT_PLAN,
            "webull": symbol != "SPX",
            "send": send_alert,
            "send_order": send,
        }
        stamp = remember_detected(
            detected,
            symbol=symbol,
            at_ct=str(row["at_ct"]),
            side="C",
            now=moment,
            active=send_alert,
        )
        if stamp:
            card["detected_at"] = stamp
            card["detected_at_ct"] = format_ct_hms(stamp)
        if picked:
            card.update(
                {
                    "strike": picked["strike"],
                    "expiry": picked["expiry"],
                    "dte": picked["dte"],
                    "ask": picked.get("ask"),
                    "bid": picked.get("bid"),
                    "contract": picked["contract"],
                }
            )
        cards.append(card)
    commit_day_store(
        store,
        path,
        _write_store,
        day=day,
        symbols=allowed,
        detected=detected,
        active=bool(window_open and routed and (allowed or detected)),
    )
    return {
        "ok": True,
        "routed": routed,
        "day": day,
        "window_open": window_open,
        "cap": DAILY_CAP,
        "opening_range": index_opening_range(
            bars_by_symbol,
            day,
            start=PRIMARY_RULES.or_start,
            end=PRIMARY_RULES.or_end,
            min_bars=PRIMARY_RULES.min_or_bars,
        ),
        "signals": cards,
        "spx_exits": modeled_spx_exits(trades, cards, allowed, tier="diamond"),
        "qualified": [
            {"symbol": row["symbol"], "at_ct": row["at_ct"]}
            for row in sorted(trades, key=lambda item: (item["at"], PRIORITY.index(item["symbol"]) if item["symbol"] in PRIORITY else 99))
        ],
        "note": (
            "Diamond calls. At most 3 names a day. Sent only from 9:00 to 10:30 CT, "
            "and only with a live ask. SPX is not a Webull order. "
            "The 60-day model is in-sample."
        ),
    }


def bars_from_frame(frame: Any, ticker: str) -> list[Bar]:
    """Yahoo-style OHLCV frame, including a MultiIndex column frame."""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    sub = frame[ticker] if isinstance(frame.columns, pd.MultiIndex) else frame
    out: list[Bar] = []
    for ts, row in sub.iterrows():
        close = row.get("Close") if hasattr(row, "get") else None
        if close is None or pd.isna(close):
            continue
        vol = row.get("Volume")
        stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        out.append(
            Bar(
                ts=stamp,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(close),
                volume=0.0 if vol is None or pd.isna(vol) else float(vol),
            )
        )
    return out


def load_intraday_bars(symbols: list[str] | None = None) -> dict[str, list[Bar]]:
    """Tradier timesales when a token is set, Yahoo 5-minute bars otherwise."""
    names = list(symbols or UNIVERSE)
    loaded: dict[str, list[Bar]] = {}
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_intraday_bars

        if access_token_from_env():
            for symbol in names:
                tradier_symbol = "SPX" if symbol == "SPX" else symbol
                frame = fetch_intraday_bars(tradier_symbol, interval="5min", lookback_days=5)
                bars = bars_from_frame(frame, tradier_symbol) if frame is not None else []
                if not bars and frame is not None:
                    bars = bars_from_frame(frame, symbol)
                if bars:
                    loaded[symbol] = bars
    except Exception as exc:  # noqa: BLE001
        logger.info("diamond tradier bars unavailable: %s", exc)
    missing = [symbol for symbol in names if symbol not in loaded]
    if not missing:
        return loaded
    try:
        import yfinance as yf

        yahoo_names = [YAHOO_SYMBOL.get(symbol, symbol) for symbol in missing]
        frame = yf.download(
            yahoo_names,
            period="5d",
            interval="5m",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        reverse = {YAHOO_SYMBOL.get(symbol, symbol): symbol for symbol in missing}
        if len(missing) == 1:
            loaded[missing[0]] = bars_from_frame(frame, missing[0]) or bars_from_frame(frame, yahoo_names[0])
        else:
            for yahoo, symbol in reverse.items():
                try:
                    loaded[symbol] = bars_from_frame(frame, yahoo)
                except Exception as exc:  # noqa: BLE001
                    logger.info("diamond yahoo %s failed: %s", symbol, exc)
    except Exception as exc:  # noqa: BLE001
        logger.info("diamond yahoo bars unavailable: %s", exc)
    return {symbol: bars for symbol, bars in loaded.items() if bars}


def load_diamond_board(
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    bars: dict[str, list[Bar]] | None = None,
) -> dict[str, Any]:
    """Live entry point used by the desk. Failures become an empty board."""
    try:
        if bars is None:
            bars = load_intraday_bars()
        if "SPY" not in bars:
            return {
                "ok": False,
                "routed": diamond_routed(),
                "signals": [],
                "qualified": [],
                "note": "Diamond needs SPY intraday bars. None were loaded.",
            }
        # Touch the market index so a SPY-less bug fails here, not mid-scan.
        index_market(bars["SPY"])
        group_sessions(bars["SPY"])

        def fetch_chain(symbol: str, spot: float) -> list[dict[str, Any]]:
            return _yahoo_chain(symbol, spot, now=now)

        return build_diamond_board(bars, now=now, store_path=store_path, fetch_chain=fetch_chain)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diamond board failed: %s", exc)
        return {"ok": False, "routed": False, "signals": [], "qualified": [], "error": str(exc)[:200]}


def _yahoo_chain(symbol: str, spot: float, *, now: datetime | None) -> list[dict[str, Any]]:
    """Nearest calls only. Used after the cap, so this is at most three names."""
    import yfinance as yf

    del spot
    yahoo = YAHOO_SYMBOL.get(symbol, symbol)
    ticker = yf.Ticker(yahoo)
    expirations = list(ticker.options or [])
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(CT).date()
    rows: list[dict[str, Any]] = []
    for expiry in expirations[:6]:
        try:
            exp = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            continue
        if (exp - today).days > 8:
            continue
        try:
            table = ticker.option_chain(expiry).calls
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
                    "right": "C",
                    "strike": float(strike),
                    "expiry": str(expiry)[:10],
                    "bid": opt.get("bid"),
                    "ask": opt.get("ask"),
                    "contract": str(opt.get("contractSymbol") or ""),
                }
            )
    return rows


# Imported so a caller can see the session length the loader asks for.
LOOKBACK = timedelta(days=5)
