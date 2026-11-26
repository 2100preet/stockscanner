"""Heavy-volume Reversal (7-14 DTE, multi-day) — SHADOW desk (alerts only, never an order).

Frozen v2 rule ("PRIMARY-v2", /workspace/reversal_study/PREREG_v2.md, frozen
Nov 26 2026 12:06 AM CT): T5 volume-climax reversal, extension gate EA, regime = all.

Calls (bullish reversal), on 5-minute RTH bars of SPY QQQ IWM AMD NVDA TSLA SLV:
1. The previous completed bar set a NEW session low, its volume was >= 3x the
   20-day average volume of that same 5-minute slot, and it closed in the upper
   60% of its range ((close - low) / range >= 0.4).
2. The current completed bar closes ABOVE that climax bar's high.
3. Extension gate EA: (session open - session low so far) >= 0.5 x ATR(20)
   (ATR = 20-day mean true range of prior regular-session daily bars).
4. Signal-bar start time 8:55 AM-1:30 PM CT, never the session's last bar;
   first signal per ticker per side per day.
Puts are the exact mirror (new session HIGH, close in the lower 60% of the
range, next bar closes BELOW the climax bar's low, (high so far - open) >= 0.5 ATR).

Shadow trade: the first listed expiry >= 7 calendar days out (Friday expiry for
single stocks, any listed date for SPY/QQQ/IWM), strike 0.5 ATR out of the money
from the signal-bar close (calls: next listed strike at/above, puts: at/below).
Entry = Tradier ask ("Limit"). Exit = first Tradier bid with return >= +100%,
else the bid at the next trading day's close (2:58 PM CT, 11:58 AM on half days).
Return = (bid - ask - 2 x $0.0065) / ask ($0.65 per contract per side).

30 trading days from 2026-11-27. Promotion gate (never automatic, owner approval
required): >= 30 closed trades, t >= 2.0, no single ticker > 50% of profit.

Data: read-only Tradier market data (daily history, 5-minute timesales,
expirations, chains, quotes). Never touches Bullflow (no SSE, no REST) or
Polygon. Imports no broker, router or order path. Kill switch:
``REVERSAL_SHADOW_OFF=1``. Ledger: ``outputs/reversal_shadow.json``.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from odte_scanner.ext_hours.common import (
    CT,
    ET,
    JsonLedger,
    NYSE_HALF_DAYS,
    default_sender,
    env_flag,
    is_trading_day,
    money,
    next_trading_day,
    now_ct,
    resolve_path,
)
from odte_scanner.signals.morning_sweep_shadow import tally, trading_days_between

logger = logging.getLogger(__name__)

KILL_ENV = "REVERSAL_SHADOW_OFF"
LEDGER_ENV = "REVERSAL_SHADOW_PATH"
SHADOW_LABEL = "SHADOW (alerts only, no order placed)"
UNIVERSE = ("SPY", "QQQ", "IWM", "AMD", "NVDA", "TSLA", "SLV")
ETFS = {"SPY", "QQQ", "IWM"}
CLIMAX_VOL_X = 3.0
CLIMAX_CLOSE_POS = 0.4
EXT_ATR = 0.5
OTM_ATR = 0.5
MIN_DTE = 7
TARGET = 1.0  # +100%
ATR_DAYS = 20
TOD_DAYS, TOD_MIN_DAYS = 20, 10
SIGNAL_START, SIGNAL_END = time(8, 55), time(13, 30)  # CT, signal-bar start
SESSION_OPEN = time(8, 30)
EXIT_TIME, EXIT_TIME_HALF = time(14, 58), time(11, 58)
BAR = timedelta(minutes=5)
ENTRY_GIVE_UP = timedelta(minutes=10)
EXIT_GIVE_UP = timedelta(minutes=30)
COMMISSION = 0.0065
SHADOW_DAYS = 30
GATE = {"min_trades": 30, "min_t": 2.0, "max_ticker_share": 0.5}
START_DAY = "2026-11-27"
BAR_REFRESH = timedelta(seconds=60)

BACKTEST_LINE = ("Backtest v2 (7 tickers, Dec 2024–Jun 2026 dev): 149 trades, 50% win, +13.8%/trade, PF 1.66 · "
                 "held-out Jul–Nov 2026: 38 trades, 42% win, +17.6%/trade, PF 1.73, t 0.97")
WARNING_LINE = "⚠️ Failed the pre-registered holdout test (t 0.97, NVDA 67% of profit). Shadow only, not a trade signal."
GATE_LINE = "30-trading-day shadow test · promotion gate: 30+ trades, t ≥ 2.0, no ticker > 50% of profit · never auto-promotes"
HEADER = "🔁 REVERSAL 7–14 DTE — " + SHADOW_LABEL


def is_off() -> bool:
    return env_flag(KILL_ENV)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------------ pure rule
def atr20(daily: list[dict[str, Any]], today: date) -> float | None:
    """20-day mean true range of regular-session daily bars strictly before ``today``."""
    rows = []
    for b in daily:
        try:
            d = date.fromisoformat(str(b.get("date"))[:10])
        except ValueError:
            continue
        h, lo, c = _f(b.get("high")), _f(b.get("low")), _f(b.get("close"))
        if d < today and h is not None and lo is not None and c is not None:
            rows.append((d, h, lo, c))
    rows.sort()
    trs = []
    for i in range(1, len(rows)):
        _, h, lo, _c = rows[i]
        pc = rows[i - 1][3]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if len(trs) < ATR_DAYS:
        return None
    return sum(trs[-ATR_DAYS:]) / ATR_DAYS


def tod_avg(history: list[dict[str, Any]], today: date) -> dict[str, float]:
    """Average 5-minute volume per CT slot ('HH:MM') over the prior 20 sessions (min 10)."""
    by_day: dict[date, dict[str, float]] = {}
    for b in history:
        t = b["t"].astimezone(CT)
        if t.date() >= today:
            continue
        by_day.setdefault(t.date(), {})[t.strftime("%H:%M")] = float(b["v"])
    days = sorted(by_day)[-TOD_DAYS:]
    out: dict[str, float] = {}
    slots = {s for d in days for s in by_day[d]}
    for s in slots:
        vals = [by_day[d][s] for d in days if s in by_day[d]]
        if len(vals) >= TOD_MIN_DAYS:
            out[s] = sum(vals) / len(vals)
    return out


def detect(bars: list[dict[str, Any]], atr: float, todv: dict[str, float]) -> dict[str, dict[str, Any]]:
    """First climax-reversal signal per side on COMPLETED 5-minute RTH bars of one session.

    ``bars``: dicts with t (bar start, tz-aware), o, h, l, c, v, oldest first.
    Mirrors the backtest's ``signals.day_signals`` T5/EA logic exactly.
    """
    out: dict[str, dict[str, Any]] = {}
    if not bars or not atr or atr <= 0:
        return out
    n = len(bars)
    for side in ("C", "P"):
        sgn = 1.0 if side == "C" else -1.0
        # mirrored series: for puts negate prices so the call logic applies unchanged
        o = [sgn * b["o"] for b in bars]
        h = [sgn * (b["h"] if side == "C" else b["l"]) for b in bars]
        lo = [sgn * (b["l"] if side == "C" else b["h"]) for b in bars]
        c = [sgn * b["c"] for b in bars]
        tm = [b["t"].astimezone(CT) for b in bars]
        lowsofar, iL = math.inf, -1
        for i in range(n):
            if lo[i] < lowsofar:
                lowsofar, iL = lo[i], i
            t = tm[i].time()
            # the backtest required a following bar to exist (entry = next bar open);
            # live, the next bar is the one now forming, so every completed bar is eligible
            # except the session's final bar (14:55 CT).
            if t < SIGNAL_START or t > SIGNAL_END:
                continue
            if (o[0] - lowsofar) / atr < EXT_ATR:
                continue
            if i >= 1 and iL == i - 1:
                slot = tm[i - 1].strftime("%H:%M")
                tv = todv.get(slot)
                if tv and tv > 0 and bars[i - 1]["v"] >= CLIMAX_VOL_X * tv:
                    rng = h[i - 1] - lo[i - 1]
                    if rng > 0 and (c[i - 1] - lo[i - 1]) / rng >= CLIMAX_CLOSE_POS and c[i] > h[i - 1]:
                        out[side] = {
                            "side": side,
                            "signal_bar": tm[i].isoformat(),
                            "climax_bar": tm[i - 1].isoformat(),
                            "climax_vol_x": round(bars[i - 1]["v"] / tv, 2),
                            "ref_price": bars[i]["c"],
                            "session_extreme": abs(lowsofar),
                            "extension_atr": round((o[0] - lowsofar) / atr, 3),
                        }
                        break
    return out


def pick_expiry(expirations: list[str], today: date, ticker: str) -> str | None:
    """First listed expiry >= today + 7 calendar days; single stocks use the Friday expiry
    (Thursday when that Friday is an exchange holiday), matching the backtest."""
    floor = today + timedelta(days=MIN_DTE)
    exps = sorted(e for e in expirations if _valid_date(e) and date.fromisoformat(e) >= floor)
    if ticker in ETFS:
        return exps[0] if exps else None
    for e in exps:
        d = date.fromisoformat(e)
        if d.weekday() == 4:
            return e
        if d.weekday() == 3 and not is_trading_day(d + timedelta(days=1)):
            return e
    return None


def _valid_date(s: Any) -> bool:
    try:
        date.fromisoformat(str(s))
        return True
    except ValueError:
        return False


def pick_strike(strikes: list[float], ref: float, atr: float, side: str) -> float | None:
    target = ref + OTM_ATR * atr if side == "C" else ref - OTM_ATR * atr
    ks = sorted(float(k) for k in strikes)
    if side == "C":
        up = [k for k in ks if k >= target - 1e-9]
        return up[0] if up else None
    dn = [k for k in ks if k <= target + 1e-9]
    return dn[-1] if dn else None


def exit_due(entry_day: date) -> datetime:
    d = next_trading_day(entry_day)
    t = EXIT_TIME_HALF if d.isoformat() in NYSE_HALF_DAYS else EXIT_TIME
    return datetime.combine(d, t, CT)


def occ(ticker: str, expiry: str, side: str, strike: float) -> str:
    return f"{ticker}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{side}{int(round(strike * 1000)):08d}"


def gate(t: dict[str, Any], today: date) -> dict[str, Any]:
    start = date.fromisoformat(START_DAY)
    days = trading_days_between(start, today) if today >= start else 0
    checks = {
        "trades": t["trades"] >= GATE["min_trades"],
        "t_stat": (t.get("t_stat") or -99) >= GATE["min_t"],
        "concentration": t.get("max_ticker_share") is not None and t["max_ticker_share"] <= GATE["max_ticker_share"],
    }
    if days < SHADOW_DAYS:
        verdict = "collecting"
    elif t["trades"] < GATE["min_trades"]:
        verdict = "extend (fewer than 30 trades)"
    else:
        verdict = "pass (needs owner approval to promote to paper)" if all(checks.values()) else "fail"
    return {**GATE, "shadow_days": SHADOW_DAYS, "start_day": START_DAY, "trading_days_elapsed": days,
            "checks": checks, "verdict": verdict, "auto_promote": False}


# ------------------------------------------------------------------ copy
def _label(t: dict[str, Any]) -> str:
    exp = t.get("expiry") or ""
    md = f"{exp[5:7]}/{exp[8:10]}" if len(exp) == 10 else exp
    k = t.get("strike")
    return f"{t['ticker']} {md} ${'' if k is None else f'{k:g}'}{t['side']}"


def _clock(iso: str | None) -> str:
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso).astimezone(CT)
    return d.strftime("%a %-I:%M %p CT")


def build_alert_text(t: dict[str, Any]) -> str:
    kind = "bullish (calls)" if t["side"] == "C" else "bearish (puts)"
    lines = [HEADER,
             f"{_label(t)} · heavy-volume {kind} reversal on the 5-min bar at {_clock(t['signal_bar'])} "
             f"(climax volume {t.get('climax_vol_x')}x normal, {t.get('extension_atr')} ATR stretch)",
             f"Shadow entry Limit {money(t['entry_ask'])} (ask) at {_clock(t['entry_at'])}",
             f"Target +100%: Limit {money(t['target_bid'])} or better (bid), else time exit "
             f"{_clock(t['exit_due'])} at the bid (Limit)",
             GATE_LINE, BACKTEST_LINE, WARNING_LINE]
    return "\n".join(lines)


def tally_text(tl: dict[str, Any]) -> str:
    avg = "—" if tl["avg_ret"] is None else f"{tl['avg_ret'] * 100:+.1f}%"
    return f"Shadow tally: {tl['trades']} trades, {tl['wins']}W/{tl['losses']}L, {avg} avg."


def build_result_text(t: dict[str, Any], tl: dict[str, Any]) -> str:
    if t["status"] == "no_exit_quote":
        return f"{_label(t)} Reversal (shadow): no exit quote, not counted. {tally_text(tl)}"
    why = "target +100% hit" if t.get("exit_reason") == "target" else "next-day close"
    return (f"{_label(t)} Reversal result (shadow, {why}): Limit entry {money(t['entry_ask'])} (ask) → "
            f"Limit exit {money(t['exit_bid'])} (bid) = {t['ret'] * 100:+.1f}%. {tally_text(tl)}")


# ------------------------------------------------------------------ data (read-only)
class TradierMarket:
    """Read-only Tradier market data. No account, order or stream endpoints."""

    def daily(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        from odte_scanner.data import tradier

        return list(tradier.fetch_history(ticker, start=start.isoformat(), end=end.isoformat()).get("bars") or [])

    def bars5(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        from odte_scanner.data import tradier

        res = tradier.fetch_timesales(ticker, interval="5min", start=f"{start.isoformat()} 09:30",
                                      end=f"{end.isoformat()} 16:00", session_filter="open")
        out = []
        for r in res.get("bars") or []:
            ts = r.get("time") or r.get("timestamp")
            try:
                t = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            t = t.replace(tzinfo=ET) if t.tzinfo is None else t
            vals = [_f(r.get(k)) for k in ("open", "high", "low", "close", "volume")]
            if any(v is None for v in vals):
                continue
            out.append({"t": t.astimezone(CT), "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3], "v": vals[4]})
        return out

    def expirations(self, ticker: str) -> list[str]:
        from odte_scanner.data import tradier

        res = tradier.fetch_expirations(ticker)
        raw = res.get("expirations") or res.get("dates") or []
        return [str(x.get("date") if isinstance(x, dict) else x) for x in raw]

    def strikes(self, ticker: str, expiry: str, side: str) -> list[float]:
        from odte_scanner.data import tradier

        opts = tradier.fetch_option_chain(ticker, expiry).get("options") or []
        want = "call" if side == "C" else "put"
        return [float(o["strike"]) for o in opts if str(o.get("option_type")) == want and _f(o.get("strike")) is not None]

    def quotes(self, occs: list[str]) -> dict[str, dict[str, Any]]:
        from odte_scanner.data import tradier

        res = tradier.fetch_quotes(occs)
        return {k.upper(): dict(v) for k, v in (res.get("quotes") or {}).items() if isinstance(v, dict)}


# ------------------------------------------------------------------ desk
class ReversalShadow:
    def __init__(self, *, market: Any | None = None, sender: Callable[[str], Any] | None = None,
                 path: Any | None = None) -> None:
        self.market = market if market is not None else TradierMarket()
        self.sender = sender if sender is not None else default_sender
        self.ledger = JsonLedger(path or resolve_path(LEDGER_ENV, "outputs/reversal_shadow.json"),
                                 {"trades": [], "orders_placed": 0})
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self._prep: dict[str, dict[str, Any]] = {}
        self._prep_day: date | None = None
        self._last_fetch: dict[str, datetime] = {}
        self.scans = 0

    # ---- daily prep: ATR(20) and slot volume averages (once per ticker per day)
    def _prepare(self, ticker: str, today: date) -> dict[str, Any] | None:
        if self._prep_day != today:
            self._prep, self._prep_day = {}, today
        if ticker in self._prep:
            return self._prep[ticker]
        try:
            daily = self.market.daily(ticker, today - timedelta(days=45), today - timedelta(days=1))
            hist = self.market.bars5(ticker, today - timedelta(days=35), today - timedelta(days=1))
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return None
        info = {"atr": atr20(daily, today), "todv": tod_avg(hist, today)}
        if not info["atr"] or not info["todv"]:
            return None
        self._prep[ticker] = info
        return info

    def _scan(self, now: datetime, data: dict[str, Any]) -> bool:
        today = now.date()
        changed = False
        for ticker in UNIVERSE:
            taken = {t["side"] for t in data["trades"] if t["ticker"] == ticker and t["date"] == today.isoformat()}
            if taken >= {"C", "P"}:
                continue
            last = self._last_fetch.get(ticker)
            if last is not None and now - last < BAR_REFRESH:
                continue
            prep = self._prepare(ticker, today)
            if prep is None:
                continue
            self._last_fetch[ticker] = now
            try:
                bars = self.market.bars5(ticker, today, today)
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                continue
            bars = [b for b in bars if b["t"].date() == today and b["t"].time() >= SESSION_OPEN and b["t"] + BAR <= now]
            self.scans += 1
            for side, sig in detect(bars, prep["atr"], prep["todv"]).items():
                if side in taken:
                    continue
                trade = self._open(ticker, side, sig, prep["atr"], now)
                data["trades"].append(trade)
                changed = True
        return changed

    def _open(self, ticker: str, side: str, sig: dict[str, Any], atr: float, now: datetime) -> dict[str, Any]:
        today = now.date()
        t: dict[str, Any] = {"id": f"{today.isoformat()}:{ticker}:{side}", "date": today.isoformat(), "ticker": ticker,
                             "side": side, "atr": round(atr, 4), "detected_at": now.isoformat(), **sig,
                             "status": "signal", "rule": "T5_climax EA all, 7-14 DTE 0.5 ATR OTM, +100% or next-day close"}
        try:
            exp = pick_expiry(self.market.expirations(ticker), today, ticker)
            k = pick_strike(self.market.strikes(ticker, exp, side), sig["ref_price"], atr, side) if exp else None
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            exp = k = None
        if not exp or k is None:
            t["status"] = "no_contract"
            return t
        t.update(expiry=exp, strike=k, contract=occ(ticker, exp, side, k),
                 dte=(date.fromisoformat(exp) - today).days)
        return t

    def tick(self, now: datetime | None = None) -> None:
        if is_off():
            return
        now = now_ct(lambda: now) if now else now_ct()
        today = now.date()
        with self._lock:
            data = self.ledger.load()
            changed = False
            if is_trading_day(today) and today.isoformat() >= START_DAY:
                in_scan = (datetime.combine(today, SIGNAL_START, CT) + BAR <= now
                           <= datetime.combine(today, SIGNAL_END, CT) + 2 * BAR)
                if in_scan:
                    changed |= self._scan(now, data)
                changed |= self._manage(now, data)
            if changed:
                data["orders_placed"] = 0
                self.ledger.save(data)

    def _manage(self, now: datetime, data: dict[str, Any]) -> bool:
        live = [t for t in data["trades"] if t["status"] in ("signal", "open")]
        if not live:
            return False
        session_open = datetime.combine(now.date(), SESSION_OPEN, CT)
        if now < session_open:
            return False
        try:
            book = self.market.quotes(sorted({t["contract"] for t in live}))
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            book = {}
        changed = False
        for t in live:
            q = book.get(t["contract"].upper()) or {}
            bid, ask = _f(q.get("bid")), _f(q.get("ask"))
            if t["status"] == "signal":
                if now > datetime.fromisoformat(t["detected_at"]) + ENTRY_GIVE_UP:
                    t["status"] = "no_entry_quote"
                    changed = True
                    continue
                if not ask or ask <= 0 or bid is None or bid < 0:
                    continue
                t["entry_bid"], t["entry_ask"], t["entry_at"] = bid, ask, now.isoformat()
                t["entry_spread"] = (ask - bid) / ((ask + bid) / 2) if ask + bid > 0 else None
                t["target_bid"] = round(ask * (1 + TARGET) + 2 * COMMISSION, 2)
                t["exit_due"] = exit_due(date.fromisoformat(t["date"])).isoformat()
                t["status"] = "open"
                t["alert"] = self._send(build_alert_text(t))
                changed = True
                continue
            due = datetime.fromisoformat(t["exit_due"])
            if bid is not None and bid >= 0 and ask:
                ret = (bid - t["entry_ask"] - 2 * COMMISSION) / t["entry_ask"]
                t["last_bid"], t["last_mark_at"] = bid, now.isoformat()
                t["max_ret"] = max(_f(t.get("max_ret")) or -1.0, ret)
                if ret >= TARGET or now >= due:
                    self._close(t, bid, now, "target" if ret >= TARGET else "time", data)
                    changed = True
                    continue
            if now > due + EXIT_GIVE_UP:
                t["status"] = "no_exit_quote"
                t["result_alert"] = self._send(build_result_text(t, tally(data["trades"])))
                changed = True
        return changed

    def _close(self, t: dict[str, Any], bid: float, now: datetime, why: str, data: dict[str, Any]) -> None:
        t["exit_bid"], t["exit_at"], t["exit_reason"] = bid, now.isoformat(), why
        t["ret"] = (bid - t["entry_ask"] - 2 * COMMISSION) / t["entry_ask"]
        t["pnl_per_1k"] = round(t["ret"] * 1000.0, 2)
        t["status"] = "closed"
        t["result_alert"] = self._send(build_result_text(t, tally(data["trades"])))

    def _send(self, text: str) -> dict[str, Any]:
        assert "sure winner" not in text.lower()
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
            "orders_enabled": False,
            "orders_placed": 0,
            "enabled": not is_off(),
            "kill_switch": KILL_ENV,
            "label": SHADOW_LABEL,
            "test": "30-trading-day shadow test (alerts only, no live orders)",
            "source": "Tradier read-only market data (5-min bars, daily history, chains, quotes). No Bullflow, no Polygon.",
            "rules": {
                "universe": list(UNIVERSE), "trigger": "heavy-volume climax reversal bar (T5, EA)",
                "climax_volume_x": CLIMAX_VOL_X, "climax_close_position": CLIMAX_CLOSE_POS,
                "extension_atr": EXT_ATR, "signal_window_ct": "08:55-13:30", "dte_min": MIN_DTE,
                "strike": "0.5 ATR OTM", "entry": "Tradier ask (Limit)", "target": "+100% at the bid",
                "time_exit": "next trading day close at the bid", "commission_per_side": 0.65,
            },
            "backtest": BACKTEST_LINE,
            "warning": WARNING_LINE,
            "promotion_gate_text": GATE_LINE,
            "tally": tl,
            "gate": gate(tl, d),
            "open_positions": [t for t in trades if t.get("status") in ("signal", "open")][::-1],
            "closed": [t for t in trades if t.get("status") not in ("signal", "open")][-50:][::-1],
            "scans": self.scans,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------- worker
TICK_SEC = 20
_LOCK = threading.Lock()
_STOP = threading.Event()
_DESK: ReversalShadow | None = None
_THREAD: threading.Thread | None = None


def _loop() -> None:
    while not _STOP.is_set():
        desk = _DESK
        if desk is not None:
            try:
                desk.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("reversal shadow tick failed: %s", type(exc).__name__)
        _STOP.wait(TICK_SEC)


def start_reversal_shadow() -> bool:
    global _DESK, _THREAD
    if env_flag("SIGNAL_DESK_OFFLINE") or is_off():
        return False
    with _LOCK:
        if _THREAD is not None:
            return False
        _DESK = ReversalShadow()
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="reversal-shadow", daemon=True)
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
            out = ReversalShadow(market=object(), sender=lambda _t: {"ok": False}).status()
            out["running"] = False
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "shadow_only": True, "orders_enabled": False, "orders_placed": 0,
                "enabled": not is_off(), "last_error": type(exc).__name__}
