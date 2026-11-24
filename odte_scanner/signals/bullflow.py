"""Bullflow.io unusual-options tape client (X-API-Key, never in the query string).

Env: ``BULLFLOW_API_KEY`` — never log, print, or persist the key.

Official endpoints (https://www.bullflow.io/api/docs):

  * ``GET /v1/data/historicalOptionsFlow`` — recorded trades, paginated
  * ``GET /v1/streaming/live-options-flow`` — singleton SSE trade stream

When the key is set this module is the Golden Flow source (board + Telegram).
It does not route orders. Cancelled prints are dropped. Startup backfill is
marked so pre-start trades cannot re-fire Telegram alerts.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bullflow.io"
HISTORICAL_PATH = "/v1/data/historicalOptionsFlow"
LIVE_STREAM_PATH = "/v1/streaming/live-options-flow"
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

DEFAULT_GOLDEN_MIN_PREMIUM = 1_000_000.0
DEFAULT_UNUSUAL_MIN_PREMIUM = 250_000.0
DEFAULT_MIN_SIG_SCORE = 0.50
DEFAULT_ALERT_SIDES = frozenset({"Ask", "Above Ask"})
DEFAULT_TRADE_TYPES = frozenset({"SWEEP", "BLOCK", "MULTI"})
DEFAULT_RECONNECT_INITIAL_SEC = 25.0
DEFAULT_RECONNECT_CAP_SEC = 300.0
DEFAULT_MAX_PRINTS = 250

# Historical / live field names from the official YAML reference.
_TRADE_FIELDS = (
    "id",
    "tradeDate",
    "timestamp",
    "symbol",
    "ticker",
    "optionType",
    "expirationDate",
    "strikePrice",
    "premium",
    "tradePrice",
    "tradeSize",
    "tradeType",
    "side",
    "spotPrice",
    "currentBid",
    "currentAsk",
    "currentVolume",
    "openInterest",
    "impliedVolatility",
    "sigScore",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "isCancelled",
    "isOutOfSequence",
)


@dataclass(frozen=True)
class BullflowSettings:
    """Configurable Golden Flow gates. Bid-side prints never Telegram-alert."""

    golden_min_premium: float = DEFAULT_GOLDEN_MIN_PREMIUM
    unusual_min_premium: float = DEFAULT_UNUSUAL_MIN_PREMIUM
    min_sig_score: float = DEFAULT_MIN_SIG_SCORE
    alert_sides: frozenset[str] = DEFAULT_ALERT_SIDES
    trade_types: frozenset[str] = DEFAULT_TRADE_TYPES
    backfill_min_premium: float = DEFAULT_UNUSUAL_MIN_PREMIUM
    reconnect_initial_sec: float = DEFAULT_RECONNECT_INITIAL_SEC
    reconnect_cap_sec: float = DEFAULT_RECONNECT_CAP_SEC
    max_prints: int = DEFAULT_MAX_PRINTS

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None = None) -> "BullflowSettings":
        data = dict(raw or {})
        sides = data.get("alert_sides") or data.get("alertSides")
        types = data.get("trade_types") or data.get("tradeTypes")
        return cls(
            golden_min_premium=_as_float(
                data.get("golden_min_premium", data.get("min_premium")),
                DEFAULT_GOLDEN_MIN_PREMIUM,
            ),
            unusual_min_premium=_as_float(
                data.get("unusual_min_premium"),
                DEFAULT_UNUSUAL_MIN_PREMIUM,
            ),
            min_sig_score=_as_float(data.get("min_sig_score"), DEFAULT_MIN_SIG_SCORE),
            alert_sides=frozenset(str(s) for s in sides) if sides else DEFAULT_ALERT_SIDES,
            trade_types=frozenset(str(t).upper() for t in types) if types else DEFAULT_TRADE_TYPES,
            backfill_min_premium=_as_float(
                data.get("backfill_min_premium"),
                DEFAULT_UNUSUAL_MIN_PREMIUM,
            ),
            reconnect_initial_sec=max(
                25.0,
                _as_float(data.get("reconnect_initial_sec"), DEFAULT_RECONNECT_INITIAL_SEC),
            ),
            reconnect_cap_sec=max(
                60.0,
                _as_float(data.get("reconnect_cap_sec"), DEFAULT_RECONNECT_CAP_SEC),
            ),
            max_prints=max(20, int(data.get("max_prints") or DEFAULT_MAX_PRINTS)),
        )


def api_key_from_env() -> str | None:
    key = (os.environ.get("BULLFLOW_API_KEY") or "").strip()
    return key or None


def _auth_headers(api_key: str) -> dict[str, str]:
    """X-API-Key only. Never put the key in the URL or in logs."""
    return {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "stockscanner-bullflow/1.0",
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _eastern_date(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ET).date().isoformat()


def _to_dt(timestamp: Any) -> datetime | None:
    try:
        val = float(timestamp)
    except (TypeError, ValueError):
        return None
    if val > 1e12:
        val /= 1000.0
    return datetime.fromtimestamp(val, tz=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _cst_label(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT).strftime("%-I:%M:%S %p CT")


def calendar_dte(expiry: str | None, *, now: datetime | None = None) -> int | None:
    raw = str(expiry or "")[:10]
    if len(raw) != 10:
        return None
    try:
        exp = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (exp - moment.astimezone(ET).date()).days


def load_settings(config: dict[str, Any] | None = None) -> BullflowSettings:
    raw: dict[str, Any] | None = None
    if isinstance(config, dict):
        raw = config.get("bullflow")
        if raw is None:
            raw = (config.get("actions") or {}).get("bullflow")
    elif config is None:
        try:
            from odte_scanner.config import load_config

            cfg = load_config()
            raw = (cfg.get("bullflow") or (cfg.get("actions") or {}).get("bullflow"))
        except Exception:  # noqa: BLE001
            raw = None
    settings = BullflowSettings.from_mapping(raw if isinstance(raw, dict) else {})
    env_prem = (os.environ.get("BULLFLOW_GOLDEN_MIN_PREMIUM") or "").strip()
    env_sig = (os.environ.get("BULLFLOW_MIN_SIG_SCORE") or "").strip()
    updates: dict[str, Any] = {}
    if env_prem:
        updates["golden_min_premium"] = _as_float(env_prem, settings.golden_min_premium)
    if env_sig:
        updates["min_sig_score"] = _as_float(env_sig, settings.min_sig_score)
    return replace(settings, **updates) if updates else settings


def parse_sse_frame(line: str) -> dict[str, Any] | None:
    """Parse one SSE ``data:`` line into a JSON event dict, or None."""
    raw = (line or "").strip()
    if not raw.startswith("data:"):
        return None
    payload = raw[5:].lstrip()
    if not payload:
        return None
    try:
        msg = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return msg if isinstance(msg, dict) else None


def next_reconnect_delay(
    attempt: int,
    *,
    initial_sec: float = DEFAULT_RECONNECT_INITIAL_SEC,
    cap_sec: float = DEFAULT_RECONNECT_CAP_SEC,
    jitter: float | None = None,
) -> float:
    """Conservative backoff: >=25s initial, exponential, plus jitter.

    Attempt is 0-based. A 25s floor keeps this process under Bullflow's
    3-connection / 60-second cooldown even after a tight disconnect loop.
    """
    base = max(25.0, float(initial_sec)) * (2 ** max(0, int(attempt)))
    delay = min(base, max(60.0, float(cap_sec)))
    if jitter is None:
        jitter = random.uniform(0.10, 0.30)
    return delay * (1.0 + max(0.0, float(jitter)))


def is_cancelled(raw: dict[str, Any] | None) -> bool:
    if not isinstance(raw, dict):
        return False
    flag = raw.get("isCancelled")
    if flag is True or str(flag).strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def is_buy_side_aggressor(side: Any, *, sides: Iterable[str] = DEFAULT_ALERT_SIDES) -> bool:
    return str(side or "").strip() in set(sides)


def is_classified_trade_type(trade_type: Any, *, types: Iterable[str] = DEFAULT_TRADE_TYPES) -> bool:
    return str(trade_type or "").strip().upper() in {str(t).upper() for t in types}


def classify_bullflow_print(row: dict[str, Any], settings: BullflowSettings | None = None) -> dict[str, Any]:
    """Stamp ``tier`` / ``flags`` / ``alertable`` on a normalized print."""
    cfg = settings or BullflowSettings()
    out = dict(row)
    premium = _as_float(out.get("premium_notional") or out.get("premium"))
    sig = _as_optional_float(out.get("sig_score") if out.get("sig_score") is not None else out.get("sigScore"))
    side = out.get("side") or out.get("execution_side")
    trade_type = out.get("trade_type") or out.get("tradeType")
    vol = _as_optional_int(out.get("volume") if out.get("volume") is not None else out.get("currentVolume"))
    oi = _as_optional_int(
        out.get("open_interest") if out.get("open_interest") is not None else out.get("openInterest")
    )
    ratio = None
    if vol is not None and oi is not None:
        ratio = float(vol) / oi if oi > 0 else (float(vol) if vol > 0 else None)
    right = str(out.get("right") or "C").upper()[:1] or "C"
    buy_side = is_buy_side_aggressor(side, sides=cfg.alert_sides)
    typed = is_classified_trade_type(trade_type, types=cfg.trade_types)
    sig_ok = sig is None or sig >= cfg.min_sig_score
    # Golden = high-conviction buy-side institutional tape. Bid-side stays
    # unusual/aggressive so it can appear on the board without Telegram.
    if buy_side and typed and premium >= cfg.golden_min_premium and sig_ok:
        tier = "golden"
        flags = ["golden", "bullflow", "buy_side"]
        alertable = not bool(out.get("backfill"))
    elif typed and premium >= cfg.unusual_min_premium:
        tier = "unusual"
        flags = ["unusual", "bullflow"]
        alertable = False
    else:
        tier = "aggressive"
        flags = ["aggressive", "bullflow"]
        alertable = False
    if buy_side:
        flags.append("ask_side")
    if ratio is not None and ratio >= 1.0:
        flags.append("vol_gt_oi")
    if typed:
        flags.append(str(trade_type).upper())
    sign = 1.0 if right == "C" else -1.0
    mag = min(80.0, premium / 25_000.0)
    if sig is not None:
        mag += min(20.0, sig * 20.0)
    flow_score = max(-100.0, min(100.0, sign * mag))
    out.update(
        {
            "premium_notional": round(premium, 2),
            "vol_oi_ratio": round(ratio, 3) if ratio is not None else None,
            "flow_score": round(flow_score, 1),
            "tier": tier,
            "flags": flags,
            "sentiment": "bullish" if right == "C" else ("bearish" if right == "P" else "neutral"),
            "alertable": bool(alertable),
            "source": "bullflow",
            "flow_source": "bullflow",
        }
    )
    return out


def trade_to_print(
    raw: dict[str, Any],
    *,
    settings: BullflowSettings | None = None,
    backfill: bool = False,
) -> dict[str, Any] | None:
    """Normalize a Bullflow trade payload into a Golden Flow print row."""
    if not isinstance(raw, dict) or is_cancelled(raw):
        return None
    trade_id = str(raw.get("id") or "").strip()
    if not trade_id:
        return None
    ticker = str(raw.get("ticker") or "").upper().strip()
    occ_raw = str(raw.get("symbol") or "").strip()
    ident = None
    try:
        from odte_scanner.trading.webull import parse_occ_contract

        ident = parse_occ_contract(occ_raw)
    except Exception:  # noqa: BLE001
        ident = None
    if ident:
        ticker = ticker or str(ident.get("symbol") or "").upper()
        right = str(ident.get("right") or "C")
        expiry = str(ident.get("expiry") or raw.get("expirationDate") or "")[:10]
        strike = ident.get("strike")
        contract = ident.get("contract") or occ_raw
    else:
        right = str(raw.get("optionType") or "C").upper()[:1] or "C"
        expiry = str(raw.get("expirationDate") or "")[:10]
        strike = _as_optional_float(raw.get("strikePrice"))
        contract = occ_raw
    if not ticker:
        return None
    dt = _to_dt(raw.get("timestamp"))
    dte = calendar_dte(expiry, now=dt)
    premium = _as_float(raw.get("premium"))
    fill = _as_optional_float(raw.get("tradePrice"))
    size = _as_optional_int(raw.get("tradeSize"))
    volume = _as_optional_int(raw.get("currentVolume"))
    oi = _as_optional_int(raw.get("openInterest"))
    sig = _as_optional_float(raw.get("sigScore"))
    spot = _as_optional_float(raw.get("spotPrice"))
    trade_type = str(raw.get("tradeType") or "").strip().upper() or None
    side = str(raw.get("side") or "").strip() or None
    bid = _as_optional_float(raw.get("currentBid"))
    ask = _as_optional_float(raw.get("currentAsk"))
    moneyness = None
    if spot and spot > 0 and strike is not None:
        moneyness = (float(strike) - float(spot)) / float(spot) * 100.0
    detail_bits = [
        trade_type or "PRINT",
        side or "—",
        f"${premium:,.0f}",
    ]
    if size is not None and fill is not None:
        detail_bits.append(f"{size} @ ${fill:g}")
    elif size is not None:
        detail_bits.append(f"{size} contracts")
    if volume is not None and oi is not None:
        detail_bits.append(f"vol {volume:,} / OI {oi:,}")
    if sig is not None:
        detail_bits.append(f"sig {sig:.2f}")
    signaled = _iso(dt)
    row = {
        "id": trade_id,
        "bullflow_id": trade_id,
        "symbol": ticker,
        "ticker": ticker,
        "contract": contract,
        "right": right,
        "strike": strike,
        "expiry": expiry,
        "dte": dte,
        "spot": spot,
        "last": spot,
        "premium": premium,
        "premium_notional": premium,
        "trade_price": fill,
        "trade_size": size,
        "trade_type": trade_type,
        "side": side,
        "execution_side": side,
        "volume": volume,
        "open_interest": oi,
        "sig_score": sig,
        "implied_volatility": _as_optional_float(raw.get("impliedVolatility")),
        "bid": bid,
        "ask": fill if fill is not None else ask,
        "mid": fill,
        "live_bid": bid,
        "live_ask": ask,
        "moneyness_pct": round(moneyness, 3) if moneyness is not None else None,
        "timestamp": raw.get("timestamp"),
        "trade_date": raw.get("tradeDate"),
        "signaled_at": signaled,
        "signaled_at_cst": _cst_label(dt),
        "is_cancelled": False,
        "is_out_of_sequence": bool(raw.get("isOutOfSequence")),
        "backfill": bool(backfill),
        "source": "bullflow",
        "flow_source": "bullflow",
        "detail": (
            " · ".join(detail_bits)
            + " · Bullflow market tape print — not a Webull fill"
        ),
        "action": "GOLDEN_FLOW",
    }
    return classify_bullflow_print(row, settings)


def _safe_error_body(response: requests.Response) -> str:
    """Short public error text. Never include headers or credentials."""
    try:
        body = response.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error") or body.get("detail")
            if msg:
                return str(msg)[:200]
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {response.status_code}"


def fetch_historical_options_flow(
    *,
    date: str,
    ticker: str | None = None,
    min_premium: float | None = None,
    option_type: str | None = None,
    trade_type: str | None = None,
    side: str | None = None,
    limit: int = 5000,
    max_pages: int = 8,
    timeout: float = 35.0,
    api_key: str | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET /v1/data/historicalOptionsFlow with pagination and 429 Retry-After."""
    key = api_key if api_key is not None else api_key_from_env()
    if not key:
        return {
            "ok": False,
            "configured": False,
            "skipped": True,
            "source": "bullflow",
            "data": [],
            "error": "BULLFLOW_API_KEY not set",
        }
    params: dict[str, Any] = {"date": date, "limit": max(1, min(int(limit), 5000))}
    if ticker:
        params["ticker"] = str(ticker).upper()
    if min_premium is not None:
        params["minPremium"] = float(min_premium)
    if option_type:
        params["optionType"] = str(option_type).upper()[:1]
    if trade_type:
        params["tradeType"] = str(trade_type).upper()
    if side:
        params["side"] = str(side)
    rows: list[dict[str, Any]] = []
    pages = 0
    cursor = None
    client = session or requests
    last_status = None
    while pages < max_pages:
        pages += 1
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        try:
            response = client.get(
                f"{BASE_URL}{HISTORICAL_PATH}",
                headers=_auth_headers(key),
                params=query,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bullflow historical request failed: %s", type(exc).__name__)
            return {
                "ok": False,
                "configured": True,
                "source": "bullflow",
                "data": rows,
                "pages": pages,
                "error": type(exc).__name__,
            }
        last_status = response.status_code
        if response.status_code == 429:
            retry_raw = response.headers.get("Retry-After") or "2"
            try:
                retry_for = max(1, int(float(retry_raw)))
            except (TypeError, ValueError):
                retry_for = 2
            logger.warning("bullflow historical rate-limited; retrying after %ss", retry_for)
            sleep(retry_for)
            continue
        if response.status_code != 200:
            err = _safe_error_body(response)
            logger.warning("bullflow historical failed: HTTP %s", response.status_code)
            return {
                "ok": False,
                "configured": True,
                "source": "bullflow",
                "data": rows,
                "pages": pages,
                "status_code": response.status_code,
                "error": err,
            }
        try:
            page = response.json()
        except Exception:  # noqa: BLE001
            return {
                "ok": False,
                "configured": True,
                "source": "bullflow",
                "data": rows,
                "pages": pages,
                "error": "invalid JSON",
            }
        page_rows = page.get("rows") if isinstance(page, dict) else None
        if isinstance(page_rows, list):
            rows.extend([r for r in page_rows if isinstance(r, dict)])
        has_more = bool(isinstance(page, dict) and page.get("hasMore"))
        cursor = page.get("nextCursor") if isinstance(page, dict) else None
        if not has_more or not cursor:
            break
        sleep(0.25)
    return {
        "ok": True,
        "configured": True,
        "source": "bullflow",
        "data": rows,
        "pages": pages,
        "status_code": last_status,
        "count": len(rows),
    }


class BullflowTape:
    """Process-wide singleton tape: one SSE connection, in-memory prints, health."""

    def __init__(self, *, settings: BullflowSettings | None = None) -> None:
        self.settings = settings or BullflowSettings()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._prints: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._alert_seen: set[str] = set()
        self._connected = False
        self._last_heartbeat: str | None = None
        self._last_event_at: str | None = None
        self._events_today = 0
        self._events_date = ""
        self._last_error: str | None = None
        self._backfill_ok = False
        self._session_date = ""

    def health(self) -> dict[str, Any]:
        with self._lock:
            configured = bool(api_key_from_env())
            source = "bullflow" if configured else "yahoo"
            return {
                "ok": bool(configured and not self._last_error or self._connected or self._backfill_ok),
                "configured": configured,
                "flow_source": source,
                "connected": bool(self._connected) if configured else False,
                "last_heartbeat": self._last_heartbeat,
                "last_event_at": self._last_event_at,
                "events_today": self._events_today if configured else 0,
                "last_error": self._last_error if configured else None,
                "started": bool(self._started) if configured else False,
                "backfill_ok": bool(self._backfill_ok) if configured else False,
                "prints": len(self._prints) if configured else 0,
                "alert_only": True,
            }

    def prints(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._prints[i]) for i in self._order if i in self._prints]

    def as_option_flow(self) -> dict[str, Any]:
        rows = self.prints()
        golden = [r for r in rows if r.get("tier") == "golden"]
        unusual = [r for r in rows if r.get("tier") == "unusual"]
        aggressive = [r for r in rows if r.get("tier") == "aggressive"]
        health = self.health()
        note = (
            "Bullflow live options tape — market prints, not Webull fills. "
            "Golden Flow Telegram alerts are Ask / Above-Ask SWEEP, BLOCK, or MULTI "
            f"at or above ${self.settings.golden_min_premium:,.0f} with sigScore "
            f">= {self.settings.min_sig_score:.2f}."
        )
        if health.get("last_error"):
            note = f"{note} Last stream note: {health['last_error']}"
        return {
            "flow_source": "bullflow",
            "prints": rows[: self.settings.max_prints],
            "golden": golden[:40],
            "unusual": unusual[:40],
            "aggressive": aggressive[:40],
            "counts": {
                "prints": len(rows),
                "golden": len(golden),
                "unusual": len(unusual),
                "aggressive": len(aggressive),
                "bullish": sum(1 for r in rows if r.get("sentiment") == "bullish"),
                "bearish": sum(1 for r in rows if r.get("sentiment") == "bearish"),
            },
            "note": note,
            "bullflow": health,
            "disclaimer": "Market tape prints from Bullflow.io. Not broker fills, not trade advice.",
        }

    def ingest_trade(
        self,
        raw: dict[str, Any],
        *,
        backfill: bool = False,
    ) -> dict[str, Any] | None:
        """Add one official trade payload. Returns the stored print, or None if dropped."""
        print_row = trade_to_print(raw, settings=self.settings, backfill=backfill)
        if print_row is None:
            return None
        trade_id = str(print_row.get("id") or "")
        with self._lock:
            already = trade_id in self._prints
            if backfill:
                print_row["backfill"] = True
                print_row["alertable"] = False
                self._alert_seen.add(trade_id)
            elif trade_id in self._alert_seen or already:
                # Already backfilled or seen — do not re-alert.
                print_row["alertable"] = False
                print_row["backfill"] = print_row.get("backfill") or trade_id in self._alert_seen
            elif print_row.get("alertable"):
                self._alert_seen.add(trade_id)
            self._prints[trade_id] = print_row
            if trade_id in self._order:
                self._order.remove(trade_id)
            self._order.append(trade_id)
            overflow = len(self._order) - self.settings.max_prints
            if overflow > 0:
                for evicted in self._order[:overflow]:
                    self._prints.pop(evicted, None)
                self._order = self._order[overflow:]
            self._bump_event_locked(print_row.get("signaled_at") or print_row.get("trade_date"))
            return dict(print_row)

    def mark_backfill(self, trade_ids: Iterable[str]) -> None:
        with self._lock:
            for raw_id in trade_ids:
                tid = str(raw_id or "").strip()
                if tid:
                    self._alert_seen.add(tid)

    def apply_backfill_rows(self, rows: Iterable[dict[str, Any]]) -> int:
        added = 0
        for raw in rows:
            if self.ingest_trade(raw, backfill=True) is not None:
                added += 1
        with self._lock:
            self._backfill_ok = True
        return added

    def handle_sse_event(self, message: dict[str, Any]) -> str | None:
        """Apply one already-parsed SSE JSON message. Returns the event name."""
        event = str(message.get("event") or "")
        now_iso = datetime.now(timezone.utc).isoformat()
        if event == "init":
            with self._lock:
                self._connected = True
                self._last_error = None
                self._session_date = str(message.get("date") or self._session_date)
            return event
        if event == "heartbeat":
            stamp = message.get("timestamp") or now_iso
            with self._lock:
                self._connected = True
                self._last_heartbeat = str(stamp)
                self._last_error = None
            return event
        if event == "trade":
            payload = message.get("data") if isinstance(message.get("data"), dict) else message
            self.ingest_trade(payload, backfill=False)
            return event
        if event == "date_changed":
            with self._lock:
                self._session_date = str(message.get("date") or "")
                self._events_today = 0
                self._events_date = self._session_date
            return event
        if event == "error":
            with self._lock:
                self._last_error = str(message.get("message") or "stream error")[:200]
            return event
        return event or None

    def start(self) -> bool:
        if not api_key_from_env():
            return False
        with self._lock:
            if self._started:
                return True
            self._started = True
            self._stop.clear()
        thread = threading.Thread(target=self._run, name="bullflow-sse", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=timeout)
        with self._lock:
            self._connected = False
            self._started = False
            self._thread = None

    def run_backfill(self, *, date: str | None = None) -> dict[str, Any]:
        day = date or _eastern_date()
        result = fetch_historical_options_flow(
            date=day,
            min_premium=self.settings.backfill_min_premium,
        )
        if result.get("ok"):
            added = self.apply_backfill_rows(result.get("data") or [])
            result["added"] = added
            logger.info("bullflow backfill stored %s prints for %s", added, day)
        else:
            with self._lock:
                self._last_error = str(result.get("error") or "backfill failed")[:200]
            logger.warning("bullflow backfill failed: %s", result.get("error"))
        return result

    def _bump_event_locked(self, stamp: Any) -> None:
        day = ""
        if isinstance(stamp, str) and len(stamp) >= 10:
            day = stamp[:10]
        if not day:
            day = _eastern_date()
        if day != self._events_date:
            self._events_date = day
            self._events_today = 0
        self._events_today += 1
        self._last_event_at = datetime.now(timezone.utc).isoformat()

    def _run(self) -> None:
        try:
            self.run_backfill()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bullflow backfill crashed: %s", type(exc).__name__)
            with self._lock:
                self._last_error = type(exc).__name__
        attempt = 0
        while not self._stop.is_set():
            key = api_key_from_env()
            if not key:
                with self._lock:
                    self._last_error = "BULLFLOW_API_KEY not set"
                    self._connected = False
                self._stop.wait(self.settings.reconnect_initial_sec)
                continue
            status, err = self._open_stream(key)
            if self._stop.is_set():
                break
            with self._lock:
                self._connected = False
                if err:
                    self._last_error = err[:200]
            if status in {401, 403, 402}:
                # Credentials / plan problem — do not hammer the API.
                logger.warning("bullflow stream rejected with HTTP %s", status)
                self._stop.wait(min(self.settings.reconnect_cap_sec, 120.0))
                attempt = 0
                continue
            delay = next_reconnect_delay(
                attempt,
                initial_sec=self.settings.reconnect_initial_sec,
                cap_sec=self.settings.reconnect_cap_sec,
            )
            if status == 429:
                delay = max(delay, 60.0)
            logger.info("bullflow stream reconnecting in %.0fs", delay)
            attempt = min(attempt + 1, 6)
            self._stop.wait(delay)

    def _open_stream(self, api_key: str) -> tuple[int | None, str | None]:
        try:
            with requests.get(
                f"{BASE_URL}{LIVE_STREAM_PATH}",
                headers={**_auth_headers(api_key), "Accept": "text/event-stream"},
                stream=True,
                timeout=(10, 35),
            ) as response:
                if response.status_code != 200:
                    return response.status_code, _safe_error_body(response)
                with self._lock:
                    self._connected = True
                    self._last_error = None
                logger.info("bullflow SSE connected")
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if self._stop.is_set():
                        return 200, None
                    if not line:
                        continue
                    text = line if isinstance(line, str) else line.decode("utf-8", "replace")
                    message = parse_sse_frame(text)
                    if message is None:
                        continue
                    self.handle_sse_event(message)
                return 200, "stream ended"
        except requests.exceptions.ReadTimeout:
            return 200, "read timeout"
        except requests.exceptions.ConnectionError:
            return None, "connection error"
        except Exception as exc:  # noqa: BLE001
            return None, type(exc).__name__


_TAPE: BullflowTape | None = None
_TAPE_LOCK = threading.Lock()


def get_tape(*, settings: BullflowSettings | None = None) -> BullflowTape:
    global _TAPE
    with _TAPE_LOCK:
        if _TAPE is None:
            _TAPE = BullflowTape(settings=settings or load_settings())
        elif settings is not None:
            _TAPE.settings = settings
        return _TAPE


def reset_tape_for_tests() -> None:
    """Test helper: drop the process singleton. Never used in production."""
    global _TAPE
    with _TAPE_LOCK:
        if _TAPE is not None:
            _TAPE.stop(timeout=0.2)
        _TAPE = None


def start_bullflow_worker(config_path: str | None = None) -> bool:
    """Start the singleton SSE worker once per process. Returns True if started or already running."""
    if (os.environ.get("SIGNAL_DESK_OFFLINE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if not api_key_from_env():
        logger.info("BULLFLOW_API_KEY not set — Golden Flow stays on the Yahoo proxy")
        return False
    settings = load_settings()
    if config_path:
        try:
            from odte_scanner.config import load_config

            settings = load_settings(load_config(config_path))
        except Exception:  # noqa: BLE001
            pass
    tape = get_tape(settings=settings)
    return tape.start()


def public_block() -> dict[str, Any]:
    """Snapshot / scan_status health. Never includes the API key."""
    if not api_key_from_env():
        return {
            "ok": False,
            "configured": False,
            "flow_source": "yahoo",
            "connected": False,
            "last_heartbeat": None,
            "events_today": 0,
            "last_error": None,
            "alert_only": True,
        }
    return get_tape().health()


def option_flow_from_tape() -> dict[str, Any] | None:
    """Return the live/backfill option_flow board when the key is configured."""
    if not api_key_from_env():
        return None
    return get_tape().as_option_flow()
