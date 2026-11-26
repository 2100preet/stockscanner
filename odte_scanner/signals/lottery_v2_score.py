"""Score the Lottery v2 log. Display only.

Reads tickets already appended to ``outputs/lottery_v2_log.json``, replays each
one on that session's intraday option bars, and writes a frozen result to
``outputs/lottery_v2_scores.json`` once the exit ladder is done. Open tickets
stay live and are marked again on the next snapshot. Nothing here places,
sizes, or routes an order. Lottery v2 stays log-only.

A failure in :func:`public_block` or :func:`score_for_snapshot` is an
``ok: false`` block. The snapshot board around it is left as it was.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery_v2 import (
    ET,
    LotteryV2Rules,
    _as_et,
    _bar_time,
    _clock_reached,
    _f,
    replay_ladder,
    ticket_budget,
)

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "outputs" / "lottery_v2_log.json"

LABEL = "Lottery v2 log"
NOTE = "log only · not traded"
HISTORY_FETCH_CAP = 8
RETRY_AFTER = timedelta(minutes=30)
TERMINAL_REASONS = {"10x", "giveback", "time_exit"}

FetchBars = Callable[..., list[dict[str, Any]]]

_LOCK = threading.RLock()


def _ct_label(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    ct = moment.astimezone(CT)
    hour = ct.hour % 12 or 12
    suffix = "AM" if ct.hour < 12 else "PM"
    return f"{hour}:{ct.minute:02d} {suffix} CT"


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_et(value)
    if value is None or value == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if len(text) == 10:
        return None
    try:
        return _as_et(datetime.fromisoformat(text))
    except ValueError:
        return None


def session_day(row: dict[str, Any]) -> str:
    day = str(row.get("session_date") or "")[:10]
    if len(day) == 10:
        return day
    logged = _parse_time(row.get("logged_at"))
    return logged.date().isoformat() if logged is not None else ""


def ticket_key(row: dict[str, Any]) -> str:
    """Same identity ``append_lottery_v2_log`` uses: session date + contract."""
    ident = str(row.get("contract") or row.get("symbol") or "")
    return f"{session_day(row)}|{ident}"


def session_is_closed(session_date: str, now: datetime, rules: LotteryV2Rules) -> bool:
    if len(str(session_date or "")) < 10:
        return False
    try:
        day = datetime.fromisoformat(str(session_date)[:10]).date()
    except ValueError:
        return False
    moment = _as_et(now)
    if moment.date() > day:
        return True
    if moment.date() < day:
        return False
    return moment.timetz().replace(tzinfo=None) >= _parse_clock(rules.time_exit_et)


def _parse_clock(text: str) -> dtime:
    hour, minute = str(text or "15:45").split(":", 1)
    return dtime(int(hour), int(minute))


def _reason_label(reason: str, rules: LotteryV2Rules) -> str:
    giveback = int(round(float(rules.giveback_from_peak) * 100))
    labels = {
        "10x": f"{float(rules.rest_multiple):.0f}×",
        "giveback": f"{giveback}% giveback from peak",
        "time_exit": f"time exit {rules.time_exit_et} ET",
        "last_print": "last print",
        "live": "live",
        "no_bars": "no bars yet",
        "no_entry": "no entry",
        "error": "score failed",
    }
    return labels.get(reason, reason or "—")


def _contracts(row: dict[str, Any], ask: float | None, rules: LotteryV2Rules) -> tuple[int, float | None]:
    raw = row.get("contracts")
    try:
        contracts = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        contracts = 0
    spent = _f(row.get("size_usd"))
    if contracts < 1 and ask is not None and ask > 0:
        contracts, implied = ticket_budget(ask, rules)
        if spent is None:
            spent = implied
    return max(0, contracts), spent


def _size_budget(ask: float | None, rules: LotteryV2Rules) -> float | None:
    if ask is None:
        return None
    return float(rules.size_usd if ask <= rules.cheap_ask else rules.size_usd_rich)


def _identity(row: dict[str, Any], rules: LotteryV2Rules, ask: float | None) -> dict[str, Any]:
    contracts, spent = _contracts(row, ask, rules)
    entry_at = _parse_time(row.get("logged_at"))
    right = str(row.get("right") or "C").upper()[:1]
    return {
        "session_date": session_day(row),
        "symbol": str(row.get("symbol") or "").upper(),
        "right": "P" if right == "P" else "C",
        "side": row.get("side") or "",
        "contract": row.get("contract"),
        "strike": _f(row.get("strike")),
        "expiry": row.get("expiry"),
        "entry": ask,
        "entry_at": entry_at.isoformat() if entry_at is not None else (row.get("logged_at") or None),
        "entry_ct": _ct_label(entry_at),
        "contracts": contracts,
        "size_usd": spent,
        "size_budget": _size_budget(ask, rules),
        "log_only": True,
        "routed": False,
        "not_traded": True,
    }


def _blank_prices(view: dict[str, Any]) -> dict[str, Any]:
    view.update(
        exit=None,
        exit_at=None,
        exit_ct=None,
        mark=None,
        mark_ct=None,
        half_exit=None,
        rest_exit=None,
        pnl_per_contract=None,
        pnl_at_size=None,
        hit_5x=False,
        hit_10x=False,
        went_to_zero=False,
        blended_multiple=None,
    )
    return view


def _unscored_view(
    row: dict[str, Any],
    rules: LotteryV2Rules,
    *,
    reason: str,
    detail: str | None = None,
    final: bool = False,
) -> dict[str, Any]:
    ask = _f(row.get("ask"))
    view = _identity(row, rules, ask)
    view.update(status="unscored", reason=reason, reason_label=_reason_label(reason, rules), final=final)
    if detail:
        view["detail"] = detail
    return _blank_prices(view)


def _prepare_bars(
    raw: list[dict[str, Any]] | None,
    *,
    session_date: str,
    entry_at: datetime | None,
    rules: LotteryV2Rules,
) -> list[dict[str, Any]]:
    """Bars on that session at or after the log time, through the hard exit."""
    dated: list[tuple[datetime, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for bar in raw or []:
        if not isinstance(bar, dict):
            continue
        item = dict(bar)
        if item.get("close") is None and item.get("c") is not None:
            item["close"] = item.get("c")
            item["high"] = item.get("high", item.get("h"))
            item["ts"] = item.get("ts") or item.get("t")
        if _f(item.get("close")) is None:
            continue
        ts = _bar_time(item)
        if ts is None:
            undated.append(item)
            continue
        if session_date and ts.date().isoformat() != session_date:
            continue
        if entry_at is not None and ts < entry_at:
            continue
        dated.append((ts, item))
    if not dated:
        return undated
    dated.sort(key=lambda pair: pair[0])
    kept: list[dict[str, Any]] = []
    for ts, item in dated:
        kept.append(item)
        if _clock_reached(ts, rules.time_exit_et):
            break
    return kept


def _apply_replay(view: dict[str, Any], replay: dict[str, Any], *, rules: LotteryV2Rules, finished: bool) -> None:
    reason = str(replay.get("reason") or "last_print")
    if not finished:
        reason = "live"
    exit_at = _parse_time(replay.get("exit_ts"))
    pnl = replay.get("pnl_per_contract")
    contracts = int(view.get("contracts") or 0)
    pnl_at = round(float(pnl) * contracts, 2) if isinstance(pnl, (int, float)) and contracts else None
    view.update(
        status="final" if finished else "live",
        final=finished,
        reason=reason,
        reason_label=_reason_label(reason, rules),
        half_exit=replay.get("half_exit"),
        rest_exit=replay.get("rest_exit"),
        blended_multiple=replay.get("blended_multiple"),
        pnl_per_contract=pnl,
        pnl_at_size=pnl_at,
        hit_5x=bool(replay.get("hit_5x")),
        hit_10x=bool(replay.get("hit_10x")),
        went_to_zero=bool(replay.get("went_to_zero")),
        ladder_reason=replay.get("reason"),
    )
    if finished:
        view.update(
            exit=replay.get("blended_exit"),
            exit_at=replay.get("exit_ts"),
            exit_ct=_ct_label(exit_at),
            mark=None,
            mark_ct=None,
        )
    else:
        view.update(
            exit=None,
            exit_at=None,
            exit_ct=None,
            mark=replay.get("last"),
            mark_ct=_ct_label(exit_at),
        )


def _score_row(
    row: dict[str, Any],
    *,
    rules: LotteryV2Rules,
    now: datetime,
    fetch: FetchBars,
) -> dict[str, Any]:
    ask = _f(row.get("ask"))
    day = session_day(row)
    if ask is None or ask <= 0:
        return _unscored_view(row, rules, reason="no_entry", final=True)
    raw = fetch(row, session_date=day)
    bars = _prepare_bars(raw, session_date=day, entry_at=_parse_time(row.get("logged_at")), rules=rules)
    view = _identity(row, rules, ask)
    if not bars:
        view.update(status="unscored", reason="no_bars", reason_label=_reason_label("no_bars", rules), final=False)
        return _blank_prices(view)
    replay = replay_ladder(float(ask), bars, rules=rules)
    if not replay.get("ok"):
        view.update(status="unscored", reason="no_bars", reason_label=_reason_label("no_bars", rules), final=False)
        return _blank_prices(view)
    closed = session_is_closed(day, now, rules)
    finished = replay.get("reason") in TERMINAL_REASONS or (replay.get("reason") == "last_print" and closed)
    _apply_replay(view, replay, rules=rules, finished=finished)
    return view


def _read_log(path: Path) -> list[dict[str, Any]]:
    from odte_scanner.json_util import load_json_state

    loaded = load_json_state(path)
    if not isinstance(loaded, list):
        return []
    return [row for row in loaded if isinstance(row, dict) and row.get("action") == "LOG"]


def _read_store(path: Path) -> dict[str, Any]:
    from odte_scanner.json_util import load_json_state

    loaded = load_json_state(path)
    if isinstance(loaded, dict) and isinstance(loaded.get("scores"), dict):
        return loaded
    return {"scores": {}}


def _write_store(path: Path, payload: dict[str, Any]) -> None:
    from odte_scanner.json_util import atomic_write_text, dumps_strict

    atomic_write_text(path, dumps_strict(payload, indent=2, default=str))


def _sum(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    if not vals:
        return None
    return round(sum(vals), 2)


def _payload(views: list[dict[str, Any]], *, today: str, now: datetime) -> dict[str, Any]:
    today_rows = [row for row in views if row.get("session_date") == today]
    today_rows.sort(key=lambda row: (str(row.get("entry_at") or ""), str(row.get("symbol") or ""), str(row.get("contract") or "")))
    finals = [row for row in views if row.get("status") == "final" and isinstance(row.get("pnl_per_contract"), (int, float))]
    wins = sum(1 for row in finals if float(row["pnl_per_contract"]) > 0)
    days = [str(row.get("session_date")) for row in views if row.get("session_date")]
    start = min(days) if days else None
    n = len(finals)
    return {
        "ok": True,
        "label": LABEL,
        "log_only": True,
        "routed": False,
        "not_traded": True,
        "note": NOTE,
        "day": today,
        "start_day": start,
        "today": {
            "day": today,
            "tickets": today_rows,
            "n": len(today_rows),
            "live": sum(1 for row in today_rows if row.get("status") == "live"),
            "final": sum(1 for row in today_rows if row.get("status") == "final"),
            "pnl_per_contract": _sum(today_rows, "pnl_per_contract"),
            "pnl_at_size": _sum(today_rows, "pnl_at_size"),
        },
        "since_start": {
            "start_day": start,
            "trades": n,
            "wins": wins,
            "win_rate_pct": round(wins / n * 100.0, 1) if n else None,
            "pnl_per_contract": _sum(finals, "pnl_per_contract") or 0.0,
            "pnl_at_size": _sum(finals, "pnl_at_size") or 0.0,
            "hit_5x": sum(1 for row in finals if row.get("hit_5x")),
            "went_to_zero": sum(1 for row in finals if row.get("went_to_zero")),
            "open": sum(1 for row in views if row.get("status") == "live"),
            "unscored": sum(1 for row in views if row.get("status") == "unscored"),
        },
        "updated_at": _as_et(now).isoformat(),
    }


def _due(stored: dict[str, Any], now: datetime) -> bool:
    nxt = _parse_time(stored.get("next_try_at"))
    return nxt is None or _as_et(now) >= nxt


def build_block(
    *,
    log_path: str | Path | None = None,
    score_path: str | Path | None = None,
    rules: LotteryV2Rules | None = None,
    now: datetime | None = None,
    fetch_bars: FetchBars | None = None,
    max_history_fetches: int = HISTORY_FETCH_CAP,
) -> dict[str, Any]:
    """Score the log. Finalized tickets are read back and not fetched again."""
    rules = rules or LotteryV2Rules()
    moment = _as_et(now)
    today = moment.date().isoformat()
    log_file = Path(log_path) if log_path is not None else DEFAULT_LOG
    store_file = Path(score_path) if score_path is not None else log_file.with_name("lottery_v2_scores.json")
    fetch = fetch_bars or default_fetch_bars
    with _LOCK:
        rows = _read_log(log_file)
        store = _read_store(store_file)
        scores: dict[str, Any] = dict(store.get("scores") or {})
        views: dict[str, dict[str, Any]] = {}
        pending_today: list[tuple[str, dict[str, Any]]] = []
        pending_hist: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for row in rows:
            key = ticket_key(row)
            if not session_day(row) or not (row.get("contract") or row.get("symbol")):
                continue
            if not key or key in seen:
                continue
            seen.add(key)
            stored = scores.get(key)
            if isinstance(stored, dict) and stored.get("final") and isinstance(stored.get("view"), dict):
                views[key] = stored["view"]
                continue
            if isinstance(stored, dict) and not _due(stored, moment) and isinstance(stored.get("view"), dict):
                views[key] = stored["view"]
                continue
            bucket = pending_today if session_day(row) == today else pending_hist
            bucket.append((key, row))
        pending_hist.sort(key=lambda item: (session_day(item[1]), item[0]))
        dirty = False
        history_fetches = 0

        def take(key: str, row: dict[str, Any]) -> None:
            nonlocal dirty
            try:
                view = _score_row(row, rules=rules, now=moment, fetch=fetch)
            except Exception as exc:  # noqa: BLE001
                logger.info("lottery v2 score %s failed: %s", key, exc)
                view = _unscored_view(row, rules, reason="error", detail=str(exc)[:160], final=False)
            views[key] = view
            record: dict[str, Any] = {"final": bool(view.get("final")), "view": view}
            closed = session_is_closed(session_day(row), moment, rules)
            if not record["final"] and closed and view.get("pnl_per_contract") is None:
                record["next_try_at"] = (moment + RETRY_AFTER).isoformat()
            scores[key] = record
            dirty = True

        for key, row in pending_today:
            take(key, row)
        for key, row in pending_hist:
            if history_fetches >= max(0, int(max_history_fetches)):
                stored = scores.get(key)
                if isinstance(stored, dict) and isinstance(stored.get("view"), dict):
                    views[key] = stored["view"]
                else:
                    views[key] = _unscored_view(row, rules, reason="no_bars", detail="waiting for a fetch slot")
                continue
            history_fetches += 1
            take(key, row)
        if dirty:
            _write_store(store_file, {"scores": scores, "updated_at": moment.isoformat()})
    return _payload(list(views.values()), today=today, now=moment)


def failure_block(exc: BaseException | str) -> dict[str, Any]:
    return {
        "ok": False,
        "label": LABEL,
        "log_only": True,
        "routed": False,
        "not_traded": True,
        "today": {
            "day": None,
            "tickets": [],
            "n": 0,
            "live": 0,
            "final": 0,
            "pnl_per_contract": None,
            "pnl_at_size": None,
        },
        "since_start": {
            "start_day": None,
            "trades": 0,
            "wins": 0,
            "win_rate_pct": None,
            "pnl_per_contract": 0.0,
            "pnl_at_size": 0.0,
            "hit_5x": 0,
            "went_to_zero": 0,
            "open": 0,
            "unscored": 0,
        },
        "error": str(exc)[:200],
        "note": "Lottery v2 score unavailable.",
    }


def public_block(**kwargs: Any) -> dict[str, Any]:
    """Snapshot block. A failure here never raises."""
    try:
        return build_block(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lottery v2 score block failed: %s", exc)
        return failure_block(exc)


def score_for_snapshot(board: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
    """Attach ``score`` onto the lottery board. The board's tickets stay put."""
    out = dict(board or {})
    try:
        out["score"] = public_block(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lottery v2 score block failed: %s", exc)
        out["score"] = failure_block(exc)
    return out


# ---------------------------------------------------------------- bars


def default_fetch_bars(ticket: dict[str, Any], *, session_date: str) -> list[dict[str, Any]]:
    """Intraday option bars for one logged contract. Empty when every source misses.

    Tradier timesales, then Polygon minute aggs, then Yahoo 1-minute, then
    Unusual Whales. The first source that returns a close wins. A source that
    errors is skipped. This never places an order.
    """
    contract = str(ticket.get("contract") or "").upper().replace("O:", "").strip()
    if not contract or len(str(session_date or "")) < 10:
        return []
    day = str(session_date)[:10]
    for name, fetcher in (
        ("tradier", _tradier_option_bars),
        ("polygon", _polygon_option_bars),
        ("yahoo", _yahoo_option_bars),
        ("unusual_whales", _uw_option_bars),
    ):
        try:
            bars = fetcher(contract, day)
        except Exception as exc:  # noqa: BLE001
            logger.info("lottery v2 %s bars %s failed: %s", name, contract, exc)
            continue
        if bars:
            return bars
    return []


def _tradier_option_bars(contract: str, day: str) -> list[dict[str, Any]]:
    from odte_scanner.data.tradier import fetch_timesales

    res = fetch_timesales(
        contract,
        interval="1min",
        start=f"{day} 09:30",
        end=f"{day} 16:00",
        session_filter="open",
        timeout=8.0,
    )
    if not isinstance(res, dict) or not res.get("ok"):
        return []
    return [row for row in (res.get("bars") or []) if isinstance(row, dict)]


def _polygon_option_bars(contract: str, day: str) -> list[dict[str, Any]]:
    from datetime import timezone

    from odte_scanner.data.polygon import _get

    res = _get(
        f"/v2/aggs/ticker/O:{contract}/range/1/minute/{day}/{day}",
        params={"adjusted": "true", "sort": "asc", "limit": 50000},
        timeout=8.0,
    )
    if not isinstance(res, dict) or not res.get("ok"):
        return []
    rows = (res.get("payload") or {}).get("results") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("c") is None:
            continue
        stamp = None
        raw_ts = row.get("t")
        if isinstance(raw_ts, (int, float)):
            stamp = datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc).astimezone(ET).isoformat()
        out.append(
            {
                "ts": stamp,
                "open": row.get("o"),
                "high": row.get("h"),
                "low": row.get("l"),
                "close": row.get("c"),
                "volume": row.get("v"),
            }
        )
    return out


def _yahoo_option_bars(contract: str, day: str) -> list[dict[str, Any]]:
    import requests
    from datetime import timezone

    start = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET)
    end = datetime.fromisoformat(day).replace(hour=16, minute=5, tzinfo=ET)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{contract}?interval=1m&period1={int(start.timestamp())}&period2={int(end.timestamp())}"
    )
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
    if response.status_code != 200:
        return []
    body = response.json() if response.content else {}
    result = ((body.get("chart") or {}).get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    out: list[dict[str, Any]] = []
    for i, ts in enumerate(stamps):
        if i >= len(closes) or closes[i] is None:
            continue
        moment = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET)
        high = highs[i] if i < len(highs) and highs[i] is not None else closes[i]
        out.append({"ts": moment.isoformat(), "close": closes[i], "high": high})
    return out


def _uw_option_bars(contract: str, day: str) -> list[dict[str, Any]]:
    from odte_scanner.signals.unusual_whales import fetch_option_contract_intraday

    res = fetch_option_contract_intraday(contract, date=day, timeout=8)
    if not isinstance(res, dict) or not res.get("ok"):
        return []
    out: list[dict[str, Any]] = []
    for row in res.get("data") or []:
        if not isinstance(row, dict) or row.get("close") is None:
            continue
        ts = row.get("start_time") or row.get("tape_time") or row.get("timestamp") or row.get("time") or row.get("date")
        out.append(
            {
                "ts": ts,
                "close": row.get("close"),
                "high": row.get("high") if row.get("high") is not None else row.get("close"),
            }
        )
    return out
