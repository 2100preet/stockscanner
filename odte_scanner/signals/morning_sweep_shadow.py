"""Mega-cap Morning Sweep — SHADOW desk (alerts only, never an order).

Rules, exactly as pre-registered in the Nov 24, 2026 Bullflow study
(``/workspace/bullflow_best/PREREG.md`` rule P1, 11-ticker fitted set):

1. A LIVE Bullflow print (from Golden Flow's existing SSE consumer; this module
   never opens a connection and never polls Bullflow) that is a SWEEP, side
   Ask / Above Ask, a CALL on one of AAPL AMD AMZN AVGO GOOGL META MSFT NFLX
   NVDA PLTR TSLA, currentVolume > openInterest, premium >= $500K,
   sigScore >= 0.6, 2-7 calendar DTE, printed 8:30-9:29:59 AM CT.
2. First qualifying print per ticker per day.
3. Shadow entry: Tradier ask of the SAME contract >= 60 s after the print
   (skip if the spread is > 10% of mid). Telegram alert at that moment.
4. Shadow exit: Tradier bid 60 minutes after entry. Return =
   (bid - ask - 2 x $0.0065) / ask  ($0.65 per contract per side).
5. 30 trading days. Promotion gate (never automatic): >= 25 trades, t >= 2,
   PF >= 1.3, no ticker > 35% of P&L.

Imports no broker, router or order path. Kill switch:
``MORNING_SWEEP_SHADOW_OFF=1``. Ledger: ``outputs/morning_sweep_shadow.json``.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from odte_scanner.ext_hours.common import (
    CT,
    ET,
    JsonLedger,
    default_sender,
    env_flag,
    is_trading_day,
    money,
    now_ct,
    resolve_path,
)

logger = logging.getLogger(__name__)

KILL_ENV = "MORNING_SWEEP_SHADOW_OFF"
LEDGER_ENV = "MORNING_SWEEP_SHADOW_PATH"
SHADOW_LABEL = "SHADOW (alerts only, no order placed)"
UNIVERSE = ("AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NFLX", "NVDA", "PLTR", "TSLA")
MIN_PREMIUM = 500_000.0
MIN_SIG = 0.6
DTE_MIN, DTE_MAX = 2, 7
WINDOW_START, WINDOW_END = time(8, 30), time(9, 30)  # CT
ENTRY_DELAY = timedelta(seconds=60)
ENTRY_GIVE_UP = timedelta(minutes=10)
HOLD = timedelta(minutes=60)
EXIT_GIVE_UP = timedelta(minutes=30)
MAX_SPREAD = 0.10
COMMISSION = 0.0065  # $0.65 per contract per side, in option-price units
ASK_SIDES = {"ask", "above ask"}
SHADOW_DAYS = 30
GATE = {"min_trades": 25, "min_t": 2.0, "min_pf": 1.3, "max_ticker_share": 0.35}
START_DAY = "2026-11-25"

BACKTEST_LINE = "Backtest (Jul 2025–Nov 2026, 11 mega-caps, 198 trades): 50% win, +5.3%/trade, PF 1.72"
WARNING_LINE = "⚠️ Failed the 30-ticker holdout (−5.7%/trade). Shadow only, not a trade signal."
HEADER = "🔎 MORNING SWEEP — " + SHADOW_LABEL


def is_off() -> bool:
    return env_flag(KILL_ENV)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ts(raw: dict[str, Any]) -> datetime | None:
    t = raw.get("timestamp")
    v = _f(t)
    if v is not None:
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(t, str):
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def occ_symbol(raw: dict[str, Any]) -> str:
    return str(raw.get("symbol") or "").strip().upper().removeprefix("O:")


def qualifies(raw: dict[str, Any]) -> tuple[bool, str]:
    """Pure rule check on an official Bullflow trade payload."""
    if not isinstance(raw, dict) or raw.get("isCancelled"):
        return False, "cancelled"
    ticker = str(raw.get("ticker") or "").upper()
    if ticker not in UNIVERSE:
        return False, "universe"
    if str(raw.get("optionType") or "").upper()[:1] != "C":
        return False, "not_call"
    if str(raw.get("tradeType") or "").upper() != "SWEEP":
        return False, "not_sweep"
    if str(raw.get("side") or "").strip().lower() not in ASK_SIDES:
        return False, "not_ask_side"
    vol, oi = _f(raw.get("currentVolume")), _f(raw.get("openInterest"))
    if vol is None or oi is None or not vol > oi:
        return False, "not_opening"
    if (_f(raw.get("premium")) or 0.0) < MIN_PREMIUM:
        return False, "premium"
    if (_f(raw.get("sigScore")) or 0.0) < MIN_SIG:
        return False, "sig_score"
    dt = _ts(raw)
    if dt is None:
        return False, "no_time"
    try:
        exp = date.fromisoformat(str(raw.get("expirationDate") or "")[:10])
    except ValueError:
        return False, "no_expiry"
    dte = (exp - dt.astimezone(ET).date()).days
    if not DTE_MIN <= dte <= DTE_MAX:
        return False, "dte"
    local = dt.astimezone(CT)
    if not is_trading_day(local.date()):
        return False, "not_trading_day"
    if not WINDOW_START <= local.time() < WINDOW_END:
        return False, "time_window"
    if not occ_symbol(raw):
        return False, "no_contract"
    return True, "ok"


def tally(trades: list[dict[str, Any]]) -> dict[str, Any]:
    done = [t for t in trades if t.get("status") == "closed" and _f(t.get("ret")) is not None]
    rets = [float(t["ret"]) for t in done]
    n = len(rets)
    out: dict[str, Any] = {"trades": n, "wins": sum(1 for r in rets if r > 0)}
    out["losses"] = n - out["wins"]
    out["win_rate"] = out["wins"] / n if n else None
    out["avg_ret"] = sum(rets) / n if n else None
    gains, losses = sum(r for r in rets if r > 0), -sum(r for r in rets if r < 0)
    out["profit_factor"] = (gains / losses) if losses > 0 else None  # None when no losing trade yet
    if n > 2:
        m = out["avg_ret"]
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (n - 1))
        out["t_stat"] = (m / (sd / math.sqrt(n))) if sd > 0 else None
    else:
        out["t_stat"] = None
    per: dict[str, float] = {}
    for t in done:
        per[t["ticker"]] = per.get(t["ticker"], 0.0) + float(t["ret"])
    total = sum(rets)
    out["total_ret"] = total
    out["per_ticker"] = {
        k: {"trades": sum(1 for t in done if t["ticker"] == k), "sum_ret": v, "share": (v / total) if total > 0 else None}
        for k, v in sorted(per.items(), key=lambda kv: -kv[1])
    }
    shares = [v["share"] for v in out["per_ticker"].values() if v["share"] is not None]
    out["max_ticker_share"] = max(shares) if shares else None
    out["pnl_per_1k"] = round(total * 1000.0, 2)
    return out


def trading_days_between(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def gate(t: dict[str, Any], today: date) -> dict[str, Any]:
    start = date.fromisoformat(START_DAY)
    days = trading_days_between(start, today) if today >= start else 0
    checks = {
        "trades": t["trades"] >= GATE["min_trades"],
        "t_stat": (t.get("t_stat") or -99) >= GATE["min_t"],
        "profit_factor": (t.get("profit_factor") or 0) >= GATE["min_pf"],
        "concentration": t.get("max_ticker_share") is not None and t["max_ticker_share"] <= GATE["max_ticker_share"],
    }
    if days < SHADOW_DAYS:
        verdict = "collecting"
    elif t["trades"] < GATE["min_trades"]:
        verdict = "extend (fewer than 25 trades)"
    else:
        verdict = "pass (needs owner approval to promote to paper)" if all(checks.values()) else "fail"
    return {**GATE, "shadow_days": SHADOW_DAYS, "start_day": START_DAY, "trading_days_elapsed": days,
            "checks": checks, "verdict": verdict, "auto_promote": False}


def _contract_label(t: dict[str, Any]) -> str:
    exp = t.get("expiry") or ""
    md = f"{exp[5:7]}/{exp[8:10]}" if len(exp) == 10 else exp
    k = t.get("strike")
    ks = "" if k is None else (f"{k:g}")
    return f"{t['ticker']} {md} ${ks}C"


def _clock(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    d = datetime.fromisoformat(dt_iso).astimezone(CT)
    return d.strftime("%-I:%M %p CT")


def build_alert_text(t: dict[str, Any]) -> str:
    vol, oi = t.get("volume"), t.get("open_interest")
    flow = f"Bullflow ${t['premium'] / 1000:,.0f}K {t.get('side') or 'Ask'} sweep"
    if vol is not None and oi is not None:
        flow += f" · vol {int(vol):,} > OI {int(oi):,}"
    if t.get("sig_score") is not None:
        flow += f" · sig {t['sig_score']:.2f}"
    lines = [HEADER, f"{_contract_label(t)} · {flow}"]
    lines.append(f"Shadow entry Limit {money(t['entry_ask'])} (ask) at {_clock(t['entry_at'])}")
    lines.append(f"Time exit {_clock(t['exit_due'])} at the bid (Limit)")
    if t.get("gex_regime") and t["gex_regime"] != "unknown":
        lines.append(f"Prior-day SPY GEX: {t['gex_regime']} (context only)")
    lines.append(BACKTEST_LINE)
    lines.append(WARNING_LINE)
    return "\n".join(lines)


def tally_text(tl: dict[str, Any]) -> str:
    avg = "—" if tl["avg_ret"] is None else f"{tl['avg_ret'] * 100:+.1f}%"
    return f"Shadow tally: {tl['trades']} trades, {tl['wins']}W/{tl['losses']}L, {avg} avg."


def build_result_text(t: dict[str, Any], tl: dict[str, Any]) -> str:
    if t["status"] == "no_exit_quote":
        return f"{_contract_label(t)} Morning Sweep (shadow): no exit quote, not counted. {tally_text(tl)}"
    return (
        f"{_contract_label(t)} Morning Sweep result (shadow): Limit entry {money(t['entry_ask'])} (ask) → "
        f"Limit exit {money(t['exit_bid'])} (bid) = {t['ret'] * 100:+.1f}%. {tally_text(tl)}"
    )


class TradierOptionQuotes:
    """Read-only Tradier market data for OCC symbols (no account / order endpoints)."""

    def quote(self, occ: str) -> dict[str, Any] | None:
        from odte_scanner.data import tradier

        res = tradier.fetch_quotes([occ])
        row = (res.get("quotes") or {}).get(occ.upper())
        return dict(row) if isinstance(row, dict) else None


class MorningSweepShadow:
    def __init__(
        self,
        *,
        quotes: Any | None = None,
        sender: Callable[[str], Any] | None = None,
        path: Any | None = None,
        regime: Callable[[], str] | None = None,
    ) -> None:
        self.quotes = quotes if quotes is not None else TradierOptionQuotes()
        self.sender = sender if sender is not None else default_sender
        self.ledger = JsonLedger(path or resolve_path(LEDGER_ENV, "outputs/morning_sweep_shadow.json"),
                                 {"trades": [], "orders_placed": 0})
        self.regime = regime or _spy_regime
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.prints_seen = 0

    # Called from the Bullflow SSE thread: must be fast and never raise.
    def on_print(self, raw: dict[str, Any], *_: Any) -> bool:
        try:
            if is_off():
                return False
            self.prints_seen += 1
            ok, _why = qualifies(raw)
            if not ok:
                return False
            dt = _ts(raw)
            day = dt.astimezone(CT).date().isoformat()
            ticker = str(raw.get("ticker")).upper()
            with self._lock:
                data = self.ledger.load()
                if any(t["ticker"] == ticker and t["date"] == day for t in data["trades"]):
                    return False
                data["trades"].append({
                    "id": f"{day}:{ticker}",
                    "bullflow_id": str(raw.get("id") or ""),
                    "date": day,
                    "ticker": ticker,
                    "contract": occ_symbol(raw),
                    "expiry": str(raw.get("expirationDate") or "")[:10],
                    "strike": _f(raw.get("strikePrice")),
                    "premium": _f(raw.get("premium")),
                    "side": raw.get("side"),
                    "volume": _f(raw.get("currentVolume")),
                    "open_interest": _f(raw.get("openInterest")),
                    "sig_score": _f(raw.get("sigScore")),
                    "print_price": _f(raw.get("tradePrice")),
                    "signal_at": dt.isoformat(),
                    "status": "signal",
                })
                self.ledger.save(data)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            logger.warning("morning sweep shadow on_print failed: %s", type(exc).__name__)
            return False

    def _q(self, occ: str) -> dict[str, Any] | None:
        try:
            return self.quotes.quote(occ)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return None

    def tick(self, now: datetime | None = None) -> None:
        if is_off():
            return
        now = now_ct(lambda: now) if now else now_ct()
        with self._lock:
            data = self.ledger.load()
            changed = False
            for t in data["trades"]:
                if t["status"] == "signal":
                    sig = datetime.fromisoformat(t["signal_at"])
                    if now < sig + ENTRY_DELAY:
                        continue
                    if now > sig + ENTRY_GIVE_UP:
                        t["status"] = "no_entry_quote"
                        changed = True
                        continue
                    q = self._q(t["contract"])
                    bid, ask = _f((q or {}).get("bid")), _f((q or {}).get("ask"))
                    if not ask or ask <= 0 or bid is None or bid < 0:
                        continue
                    mid = (bid + ask) / 2
                    t["entry_bid"], t["entry_ask"] = bid, ask
                    t["entry_spread"] = (ask - bid) / mid if mid > 0 else None
                    if t["entry_spread"] is None or t["entry_spread"] > MAX_SPREAD:
                        t["status"] = "skipped_spread"
                        changed = True
                        continue
                    t["entry_at"] = now.isoformat()
                    t["exit_due"] = (now + HOLD).isoformat()
                    try:
                        t["gex_regime"] = self.regime()
                    except Exception:  # noqa: BLE001
                        t["gex_regime"] = "unknown"
                    t["status"] = "open"
                    t["alert"] = self._send(build_alert_text(t))
                    changed = True
                elif t["status"] == "open":
                    due = datetime.fromisoformat(t["exit_due"])
                    if now < due:
                        continue
                    q = self._q(t["contract"])
                    bid = _f((q or {}).get("bid"))
                    if bid is None or bid < 0 or (q or {}).get("ask") in (None, 0):
                        if now > due + EXIT_GIVE_UP:
                            t["status"] = "no_exit_quote"
                            t["result_alert"] = self._send(build_result_text(t, tally(data["trades"])))
                            changed = True
                        continue
                    t["exit_bid"], t["exit_at"] = bid, now.isoformat()
                    t["ret"] = (bid - t["entry_ask"] - 2 * COMMISSION) / t["entry_ask"]
                    t["pnl_per_1k"] = round(t["ret"] * 1000.0, 2)
                    t["status"] = "closed"
                    t["result_alert"] = self._send(build_result_text(t, tally(data["trades"])))
                    changed = True
            if changed:
                data["orders_placed"] = 0
                self.ledger.save(data)

    def _send(self, text: str) -> dict[str, Any]:
        try:
            res = self.sender(text) or {}
            return {"ok": bool(res.get("ok")) if isinstance(res, dict) else False}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def status(self, today: date | None = None) -> dict[str, Any]:
        data = self.ledger.load()
        trades = data.get("trades") or []
        tl = tally(trades)
        d = today or now_ct().date()
        return {
            "ok": True,
            "shadow_only": True,
            "orders_placed": 0,
            "enabled": not is_off(),
            "kill_switch": KILL_ENV,
            "label": SHADOW_LABEL,
            "source": "Bullflow live tape (Golden Flow's existing SSE consumer) + Tradier option quotes",
            "rules": {
                "universe": list(UNIVERSE), "trade_type": "SWEEP", "side": "Ask / Above Ask", "right": "CALL",
                "opening": "currentVolume > openInterest", "min_premium": MIN_PREMIUM, "min_sig_score": MIN_SIG,
                "dte": [DTE_MIN, DTE_MAX], "window_ct": "08:30-09:30", "entry": "Tradier ask >= 60 s after print",
                "exit": "Tradier bid 60 min after entry", "commission_per_side": 0.65, "max_spread": MAX_SPREAD,
            },
            "backtest": BACKTEST_LINE,
            "warning": WARNING_LINE,
            "tally": tl,
            "gate": gate(tl, d),
            "trades": trades[-50:][::-1],
            "prints_seen": self.prints_seen,
            "last_error": self.last_error,
        }


def _spy_regime() -> str:
    from odte_scanner.signals.gex_regime import regime_for

    return regime_for("SPY")


# --------------------------------------------------------------- worker
TICK_SEC = 15
_LOCK = threading.Lock()
_STOP = threading.Event()
_DESK: MorningSweepShadow | None = None
_THREAD: threading.Thread | None = None


def _loop() -> None:
    while not _STOP.is_set():
        desk = _DESK
        if desk is not None:
            try:
                desk.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("morning sweep shadow tick failed: %s", type(exc).__name__)
        _STOP.wait(TICK_SEC)


def start_morning_sweep_shadow(register: Callable[[Callable[..., Any]], Any] | None = None) -> bool:
    """Start once per process and subscribe to the EXISTING Bullflow tape.

    ``register`` defaults to ``BullflowTape.add_listener`` of the process
    singleton. Registering never starts or opens a stream.
    """
    global _DESK, _THREAD
    if env_flag("SIGNAL_DESK_OFFLINE") or is_off():
        return False
    with _LOCK:
        if _THREAD is not None:
            return False
        _DESK = MorningSweepShadow()
        if register is None:
            from odte_scanner.signals.bullflow import get_tape

            register = get_tape().add_listener
        register(_DESK.on_print)
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="morning-sweep-shadow", daemon=True)
        _THREAD.start()
    return True


def reset_for_tests() -> None:
    global _DESK, _THREAD
    _STOP.set()
    _DESK = _THREAD = None


def block() -> dict[str, Any]:
    try:
        if _DESK is not None:
            out = _DESK.status()
            out["running"] = True
        else:
            out = MorningSweepShadow(quotes=object(), sender=lambda _t: {"ok": False}).status()
            out["running"] = False
        try:
            from odte_scanner.signals.bullflow import public_block

            hb = public_block()
            out["stream"] = {"connected": bool(hb.get("connected")), "events_today": hb.get("events_today")}
        except Exception:  # noqa: BLE001
            out["stream"] = {"connected": False}
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "shadow_only": True, "orders_placed": 0, "enabled": not is_off(), "last_error": type(exc).__name__}
