"""Run and publish the two isolated paper desks.

The portal snapshot calls :func:`portal_view`. Under pytest that call only
reads the ledgers, so the suite does not download bars or open trades.
Outside pytest it runs a cycle. Daily bars are fetched only when a desk
needs them, and each desk keeps its own bar cache file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.kotegawa import (
    DEFAULT_STATE as KOTEGAWA_STATE,
    public_view as kotegawa_view,
    run_kotegawa,
    scorecard_row as kotegawa_row,
    strategy_summary as kotegawa_strategy,
)
from odte_scanner.desks.kotegawa import _fresh as kotegawa_fresh
from odte_scanner.desks.kotegawa import _load as load_kotegawa
from odte_scanner.desks.million import (
    DEFAULT_STATE as MILLION_STATE,
    public_view as million_view,
    run_million,
    scorecard_row as million_row,
    strategy_summary as million_strategy,
)
from odte_scanner.desks.million import _fresh as million_fresh
from odte_scanner.desks.million import _load as load_million
from odte_scanner.desks.portal_feed import shape_portal_feed
from odte_scanner.desks.sp100 import oversold_universe, sp100_universe
from odte_scanner.desks.store import DeskStateError
from odte_scanner.json_util import atomic_write_text

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
BAR_TTL = timedelta(hours=6)
VWAP_TTL = timedelta(seconds=60)
CT = ZoneInfo("America/Chicago")
# One cycle at a time. The web snapshot and the live desk loop build their own
# Flask apps, so the snapshot gate does not cover both. Two cycles at once both
# load the same book, and the later save drops the other cycle's fills.
_CYCLE_LOCK = threading.Lock()


def _abs(path: str | Path) -> Path:
    dest = Path(path)
    if not dest.is_absolute():
        dest = ROOT / dest
    return dest


def _section(cfg: Mapping[str, Any], name: str) -> dict[str, Any]:
    raw = cfg.get(name) if isinstance(cfg, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def _should_cycle(cycle: bool | None) -> bool:
    if cycle is not None:
        return cycle
    return "PYTEST_CURRENT_TEST" not in os.environ


def _as_of(now: datetime | None):
    if now is None:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=CT)
    return moment.astimezone(CT).date()


def _read_book(loader, fresh, path: Path, starting: float) -> dict[str, Any]:
    try:
        book = loader(path, starting)
    except DeskStateError:
        book = fresh(starting)
        book["status_note"] = "state file belongs to another desk"
    book["state_path"] = str(path)
    return book


def _frame_to_bars(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        def col(*names: str) -> Any:
            for name in names:
                if name in row:
                    return row[name]
            return None

        close = col("Close", "close")
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        if close_f != close_f or close_f <= 0:
            continue
        def num(value: Any, fallback: float) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return fallback
            if number != number or number <= 0:
                return fallback
            return number

        rows.append(
            {
                "date": str(idx)[:10],
                "open": num(col("Open", "open"), close_f),
                "high": num(col("High", "high"), close_f),
                "low": num(col("Low", "low"), close_f),
                "close": close_f,
            }
        )
    return rows


def fetch_daily_bars(symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Best-effort daily bars. A download failure leaves that name out."""
    from odte_scanner.data.fetcher import fetch_history

    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        try:
            frame = fetch_history(symbol, period="6mo", interval="1d", use_cache=True)
        except Exception as exc:  # noqa: BLE001
            logger.info("paper desk bars skipped %s: %s", symbol, exc)
            continue
        bars = _frame_to_bars(frame)
        if bars:
            out[symbol] = bars
    return out


def _cache_path(desk: str) -> Path:
    name = "kotegawa_bars.json" if desk == "kotegawa" else "million_bars.json"
    return ROOT / "outputs" / name


def _load_bar_cache(desk: str, symbols: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    path = _cache_path(desk)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        fetched = datetime.fromisoformat(str(raw.get("fetched_at")))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > BAR_TTL:
        return None
    bars = raw.get("bars")
    if not isinstance(bars, dict):
        return None
    if any(symbol not in bars for symbol in symbols):
        return None
    return {symbol: list(bars.get(symbol) or []) for symbol in symbols}


def _store_bar_cache(desk: str, bars: Mapping[str, list[Mapping[str, Any]]]) -> None:
    path = _cache_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "desk": desk,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bars": bars,
    }
    atomic_write_text(path, json.dumps(payload, default=str))


def _tradier_quotes(symbols: list[str]) -> Mapping[str, Any]:
    from odte_scanner.data.tradier import fetch_quotes

    result = fetch_quotes(symbols, timeout=6.0)
    return result.get("quotes") or {}


def _quotes_for_open_positions(
    book: Mapping[str, Any],
    quotes: Mapping[str, Any] | None,
    fetch: Callable[[list[str]], Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Add live quotes for open Kotegawa stocks the portal scan did not quote.

    The scan quotes the focus list; Kotegawa holds S&P 100 names. Without a
    quote the position is marked (and its stop/target checked) against the
    cached daily bar. At most a handful of symbols, one batched request.
    """
    held = sorted(
        {
            str(row.get("symbol") or "").upper()
            for row in (book.get("trades") or [])
            if isinstance(row, Mapping) and row.get("status") == "open" and row.get("symbol")
        }
    )
    have = dict(quotes or {})
    missing = [sym for sym in held if not isinstance(have.get(sym), Mapping) or have[sym].get("last") in (None, 0)]
    if not missing:
        return dict(quotes) if quotes is not None else None
    loader = fetch or _tradier_quotes
    try:
        fresh = loader(missing) or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("kotegawa quotes skipped: %s", exc)
        return dict(quotes) if quotes is not None else None
    for sym in missing:
        row = fresh.get(sym)
        if isinstance(row, Mapping) and row.get("last") not in (None, 0):
            have[sym] = dict(row)
    return have


def _bars_for(
    desk: str,
    symbols: list[str],
    provided: Mapping[str, list[Mapping[str, Any]]] | None,
    *,
    fetch: bool,
    fetcher: Callable[[list[str]], Mapping[str, list[Mapping[str, Any]]]] | None,
) -> dict[str, list[dict[str, Any]]] | None:
    if provided is not None:
        return {str(key): list(value) for key, value in provided.items()}
    if not fetch:
        return None
    cached = _load_bar_cache(desk, symbols)
    if cached is not None:
        return cached
    loader = fetcher or fetch_daily_bars
    try:
        fresh = loader(symbols)
    except Exception as exc:  # noqa: BLE001
        logger.info("paper desk bar fetch failed: %s", exc)
        return None
    bars = {str(key): [dict(row) for row in value] for key, value in (fresh or {}).items()}
    if bars:
        _store_bar_cache(desk, bars)
    return bars


def _load_vwap_market(now: datetime | None) -> dict[str, Any]:
    """Best-effort SPY/QQQ bars, calls, and a VIX tag for the paper desk.

    A failure leaves that piece empty. The desk then skips the entry instead
    of guessing. Results are reused for a minute so a snapshot burst does
    not download the same bars twice.
    """
    cached = _read_vwap_cache()
    if cached is not None:
        return cached
    try:
        loaded = _fetch_vwap_market(now)
    except Exception as exc:  # noqa: BLE001
        logger.info("vwap reclaim market data skipped: %s", exc)
        return {"intraday": {}, "chains": {}, "vix": None}
    if loaded.get("intraday") or loaded.get("vix"):
        _write_vwap_cache(loaded)
    return loaded


def _vwap_cache_path() -> Path:
    return ROOT / "outputs" / "million_vwap_cache.json"


def _read_vwap_cache() -> dict[str, Any] | None:
    path = _vwap_cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(str(raw.get("fetched_at")))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > VWAP_TTL:
        return None
    intraday = raw.get("intraday")
    chains = raw.get("chains")
    if not isinstance(intraday, dict) or not isinstance(chains, dict):
        return None
    return {"intraday": intraday, "chains": chains, "vix": raw.get("vix")}


def _write_vwap_cache(loaded: Mapping[str, Any]) -> None:
    path = _vwap_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "intraday": loaded.get("intraday") or {},
        "chains": loaded.get("chains") or {},
        "vix": loaded.get("vix"),
    }
    try:
        atomic_write_text(path, json.dumps(payload, default=str))
    except OSError as exc:
        logger.info("vwap cache skipped: %s", exc)


def _fetch_vwap_market(now: datetime | None) -> dict[str, Any]:
    import yfinance as yf

    from odte_scanner.desks.signals import intraday_bars_from_frame
    from odte_scanner.options.yahoo_session import fetch_option_chain
    from odte_scanner.signals.hold_rules import trading_dte

    moment = now or datetime.now(CT)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    intraday: dict[str, list[dict[str, Any]]] = {}
    chains: dict[str, list[dict[str, Any]]] = {}
    for symbol in ("SPY", "QQQ"):
        try:
            frame = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=False, auto_adjust=False)
            bars = intraday_bars_from_frame(frame, interval="1m", now=moment)
            if bars:
                intraday[symbol] = bars
        except Exception as exc:  # noqa: BLE001
            logger.info("vwap bars skipped %s: %s", symbol, exc)
        try:
            rows = _yahoo_call_rows(symbol, moment, fetch_option_chain, trading_dte)
            if rows:
                chains[symbol] = rows
        except Exception as exc:  # noqa: BLE001
            logger.info("vwap chain skipped %s: %s", symbol, exc)
    return {"intraday": intraday, "chains": chains, "vix": _fetch_vix_tag()}


def _yahoo_call_rows(symbol: str, now: datetime, fetch_option_chain, trading_dte) -> list[dict[str, Any]]:
    root = fetch_option_chain(symbol)
    if not isinstance(root, dict):
        return []
    upcoming: list[str] = []
    today = now.astimezone(CT).date()
    for ts in root.get("expirationDates") or []:
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            continue
        if day >= today:
            upcoming.append(day.isoformat())
        if len(upcoming) >= 2:
            break
    payloads = [root]
    for expiry in upcoming:
        book = fetch_option_chain(symbol, expiry=expiry)
        if isinstance(book, dict):
            payloads.append(book)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for block in payload.get("options") or []:
            if not isinstance(block, dict):
                continue
            exp_ts = block.get("expirationDate")
            try:
                expiry = datetime.fromtimestamp(int(exp_ts), tz=timezone.utc).date().isoformat()
            except (TypeError, ValueError, OSError):
                continue
            dte = trading_dte(expiry, now)
            for side, right in (("calls", "C"), ("puts", "P")):
                for contract in block.get(side) or []:
                    if not isinstance(contract, dict):
                        continue
                    contract_id = str(contract.get("contractSymbol") or "")
                    key = contract_id or f"{right}:{expiry}:{contract.get('strike')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "right": right,
                            "strike": contract.get("strike"),
                            "bid": contract.get("bid"),
                            "ask": contract.get("ask"),
                            "expiry": expiry,
                            "dte": dte,
                            "contract": contract_id or None,
                        }
                    )
    return rows


def _fetch_vix_tag() -> dict[str, Any] | None:
    try:
        import yfinance as yf

        frame = yf.Ticker("^VIX").history(period="10d", interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        logger.info("vix tag skipped: %s", exc)
        return None
    if frame is None or getattr(frame, "empty", True) or "Close" not in frame.columns:
        return None
    closes = [float(value) for value in frame["Close"].tolist() if value == value]
    if not closes:
        return None
    level = closes[-1]
    prior = closes[-2] if len(closes) > 1 else None
    return {"level": level, "prior_close": prior}


REMOTE_URL = "https://stockscanner.fly.dev/api/paper_desks"


def _on_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" and "PYTEST_CURRENT_TEST" not in os.environ


def _fetch_remote_books(url: str) -> Mapping[str, Any] | None:
    import requests

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, Mapping) else None


def _remote_view(cfg: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Read-only copy of the live desk books for a static export.

    Alerts are dropped: Fly already sent them, and this copy must not resend.
    """
    url = os.environ.get("PAPER_DESKS_URL") or REMOTE_URL
    try:
        body = _fetch_remote_books(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("paper desks: live books unavailable (%s)", exc)
        return None
    if not body:
        return None
    views: dict[str, dict[str, Any]] = {}
    for key in ("kotegawa", "million_challenge"):
        view = body.get(key)
        if not isinstance(view, Mapping):
            return None
        views[key] = {**dict(view), "alerts": [], "cycle_alerts": [], "source": "live desk (Fly)"}
    config = cfg or {}
    views["kotegawa"].setdefault("strategy", kotegawa_strategy(_section(config, "kotegawa")))
    views["million_challenge"].setdefault("strategy", million_strategy(_section(config, "million_challenge")))
    return {
        "kotegawa": views["kotegawa"],
        "million_challenge": views["million_challenge"],
        "rows": [kotegawa_row(views["kotegawa"]), million_row(views["million_challenge"])],
        "portal": shape_portal_feed(views["kotegawa"], views["million_challenge"], now=now),
    }


def portal_view(
    cfg: Mapping[str, Any] | None = None,
    *,
    challenge: Mapping[str, Any] | None = None,
    quotes: Mapping[str, Any] | None = None,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    vix: Mapping[str, Any] | None = None,
    cycle: bool | None = None,
    fetcher: Callable[[list[str]], Mapping[str, list[Mapping[str, Any]]]] | None = None,
    environ: Mapping[str, str] | None = None,
    quote_fetcher: Callable[[list[str]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Public books plus the scorecard rows for both desks.

    A cycle that finds another cycle running reads the saved books instead of
    advancing them a second time.
    """
    args = dict(
        challenge=challenge,
        quotes=quotes,
        bars=bars,
        chains=chains,
        intraday=intraday,
        now=now,
        vix=vix,
        fetcher=fetcher,
        environ=environ,
        quote_fetcher=quote_fetcher,
    )
    if cycle is None and _on_github_actions():
        # The Pages / alert workflows must never advance these books: their
        # outputs/ starts empty, so a cycle there would open phantom paper
        # trades and send [KOTEGAWA]/[MILLION] alerts. Show the Fly books.
        remote = _remote_view(cfg, now=now)
        if remote is not None:
            return remote
        return _portal_view_once(cfg, cycle=False, **args)
    if not _should_cycle(cycle):
        return _portal_view_once(cfg, cycle=False, **args)
    if not _CYCLE_LOCK.acquire(blocking=False):
        logger.info("paper desks: cycle already running; serving saved books")
        return _portal_view_once(cfg, cycle=False, **args)
    try:
        return _portal_view_once(cfg, cycle=True, **args)
    finally:
        _CYCLE_LOCK.release()


def _portal_view_once(
    cfg: Mapping[str, Any] | None = None,
    *,
    challenge: Mapping[str, Any] | None = None,
    quotes: Mapping[str, Any] | None = None,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    vix: Mapping[str, Any] | None = None,
    cycle: bool | None = None,
    fetcher: Callable[[list[str]], Mapping[str, list[Mapping[str, Any]]]] | None = None,
    environ: Mapping[str, str] | None = None,
    quote_fetcher: Callable[[list[str]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Public books plus the scorecard rows for both desks."""
    config = cfg or {}
    kot_cfg = _section(config, "kotegawa")
    mil_cfg = _section(config, "million_challenge")
    live = config.get("live_trading") if isinstance(config.get("live_trading"), Mapping) else {}
    do_cycle = _should_cycle(cycle)
    kot_path = _abs(kot_cfg.get("state_path") or KOTEGAWA_STATE)
    mil_path = _abs(mil_cfg.get("state_path") or MILLION_STATE)
    kot_start = float(kot_cfg.get("starting_equity") or 100_000)
    mil_start = float(mil_cfg.get("starting_equity") or 1_000)

    kotegawa: dict[str, Any]
    million: dict[str, Any]
    if do_cycle and kot_cfg.get("enabled") is True and kot_cfg.get("paper_only") is True:
        want_bars = bars is not None or bool(kot_cfg.get("fetch_bars", True))
        kot_bars = _bars_for(
            "kotegawa",
            list(sp100_universe()),
            bars,
            fetch=want_bars and bars is None,
            fetcher=fetcher,
        )
        kot_quotes = _quotes_for_open_positions(
            _read_book(load_kotegawa, kotegawa_fresh, kot_path, kot_start),
            quotes,
            quote_fetcher,
        )
        kotegawa = run_kotegawa(
            kot_cfg,
            bars=kot_bars,
            quotes=kot_quotes,
            now=now,
            path=kot_path,
            live_trading=live,
            environ=environ,
        )
    else:
        kotegawa = kotegawa_view(
            _read_book(load_kotegawa, kotegawa_fresh, kot_path, kot_start),
            as_of=_as_of(now),
        )

    sources = [str(name) for name in (mil_cfg.get("signals") or [])]
    needs_bars = "oversold_bounce" in sources
    if do_cycle and mil_cfg.get("enabled") is True and mil_cfg.get("paper_only") is True:
        mil_bars = None
        if needs_bars:
            mil_bars = _bars_for(
                "million",
                list(oversold_universe()),
                bars,
                fetch=bars is None and bool(mil_cfg.get("fetch_bars", True)),
                fetcher=fetcher,
            )
        elif bars is not None:
            mil_bars = {str(key): list(value) for key, value in bars.items()}
        use_intraday = intraday
        use_chains = chains
        use_vix = vix
        want_tape = bool(mil_cfg.get("fetch_bars", True))
        if (
            want_tape
            and "vwap_reclaim" in sources
            and (intraday is None or chains is None or vix is None)
        ):
            loaded = _load_vwap_market(now)
            if intraday is None:
                use_intraday = loaded.get("intraday")
            if chains is None:
                use_chains = loaded.get("chains")
            if vix is None:
                use_vix = loaded.get("vix")
        # ``quotes`` is the equity tape, keyed by underlying. Million marks
        # options by OCC contract and reads this map only as the spot bound.
        million = run_million(
            mil_cfg,
            challenge=challenge,
            bars=mil_bars,
            chains=use_chains,
            intraday=use_intraday,
            quotes=quotes,
            now=now,
            path=mil_path,
            live_trading=live,
            environ=environ,
            vix=use_vix,
        )
    else:
        try:
            held = load_million(mil_path, mil_start, ledger_epoch=mil_cfg.get("ledger_epoch"))
        except DeskStateError:
            held = million_fresh(mil_start)
            held["status_note"] = "state file belongs to another desk"
        held["state_path"] = str(mil_path)
        million = million_view(held, as_of=_as_of(now))
        million["signals"] = sources

    kotegawa = {**kotegawa, "strategy": kotegawa_strategy(kot_cfg)}
    million = {**million, "strategy": million_strategy(mil_cfg)}
    rows = [kotegawa_row(kotegawa), million_row(million)]
    portal = shape_portal_feed(kotegawa, million, now=now)
    return {
        "kotegawa": kotegawa,
        "million_challenge": million,
        "rows": rows,
        "portal": portal,
    }


def scorecard_rows(cfg: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read both ledgers without running a cycle."""
    view = (_remote_view(cfg) if _on_github_actions() else None) or portal_view(cfg, cycle=False)
    return list(view.get("rows") or [])
