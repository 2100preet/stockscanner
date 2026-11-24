"""S2 After-Hours Earnings Drift — SHADOW desk (alerts only, no orders).

Rules (exactly as backtested in the Nov 24, 2026 extended-hours study):
1. One of the 27 S2 names reports AFTER the close today.
2. At 5:00 PM CT read P5 (last price at 5:00 PM CT) and P4 (4:00 PM CT);
   C = today's 3:00 PM CT close.
3. Skip if |P5/P4 - 1| >= 3% (isolated bad print) or bid/ask spread > 0.5%.
4. R = P5/C - 1; fire when |R| >= 3%. Up -> LONG shares, down -> SHORT shares.
   |R| >= 6% is labelled "Gold Setup (shadow)".
5. Shadow entry: Limit at the 5:00 PM mid +/- 0.1%, good until 7:00 PM CT;
   counted as filled only if after-hours prints trade through the Limit.
6. Exit: next trading day's 8:30 AM CT open (also logs 9:30 AM CT and close).

This module never imports a broker, router or order path. Kill switch:
``AH_EARNINGS_SHADOW_OFF=1``. Ledger: ``outputs/ah_earnings_shadow.json``.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time
from typing import Any, Callable

from odte_scanner.ext_hours import SHADOW_LABEL
from odte_scanner.ext_hours.common import (
    CT,
    JsonLedger,
    default_sender,
    env_flag,
    in_window,
    is_full_trading_day,
    is_trading_day,
    money,
    next_trading_day,
    now_ct,
    prev_trading_day,
    resolve_path,
)
from odte_scanner.ext_hours.earnings_calendar import EarningsCalendar

logger = logging.getLogger(__name__)

KILL_ENV = "AH_EARNINGS_SHADOW_OFF"
THRESHOLD = 0.03
GOLD_THRESHOLD = 0.06
PRINT_CHECK = 0.03
MAX_SPREAD = 0.005
LIMIT_OFFSET = 0.001
EVAL_START, EVAL_END = time(17, 0), time(18, 30)
SETTLE_START, SETTLE_END = time(8, 35), time(10, 30)
MID_START, MID_END = time(9, 30), time(11, 0)
CLOSE_START, CLOSE_END = time(15, 5), time(17, 0)

BACKTEST_LINE = "Backtest (Dec 2023–Nov 2026, 164 trades): 66% win, avg +0.75% net, PF 1.9"
WARNING_LINE = "⚠️ Last earnings season was negative (−0.86%/trade over 15 trades). Shadow only until approved."
HEADER = "🌙 AH EARNINGS DRIFT — " + SHADOW_LABEL
GOLD_HEADER = "🌙 AH EARNINGS DRIFT — Gold Setup (shadow)"


def is_off() -> bool:
    return env_flag(KILL_ENV)


def season_key(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def exit_day_phrase(entry: date) -> str:
    nxt = next_trading_day(entry)
    return "tomorrow" if (nxt - entry).days == 1 else nxt.strftime("%A")


def build_alert_text(t: dict[str, Any]) -> str:
    lines = [GOLD_HEADER, SHADOW_LABEL] if t.get("gold") else [HEADER]
    lines.append(
        f"{t['symbol']} reported after the close: {t['R'] * 100:+.1f}% vs 3:00 PM close "
        f"({money(t['C'])} → {money(t['P5'])} at 5:00 PM CT)"
    )
    when = exit_day_phrase(date.fromisoformat(t["entry_date"]))
    lines.append(f"Setup: {t['side']} shares, hold to {when}'s open")
    lines.append(f"Entry Limit: {money(t['limit'])} (good until 7:00 PM CT, extended hours)")
    if t["side"] == "SHORT":
        lines.append("Short only in after-hours (before 7:00 PM CT): Webull allows no overnight shorting.")
    lines.append(f"Exit: {when} 8:30 AM CT open")
    lines.append(BACKTEST_LINE)
    lines.append(WARNING_LINE)
    return "\n".join(lines)


def season_tally(trades: list[dict[str, Any]], season: str) -> dict[str, Any]:
    done = [t for t in trades if t.get("season") == season and t.get("status") == "closed" and t.get("ret") is not None]
    wins = sum(1 for t in done if t["ret"] > 0)
    avg = sum(t["ret"] for t in done) / len(done) if done else None
    return {"season": season, "trades": len(done), "wins": wins, "losses": len(done) - wins, "avg_ret": avg}


def tally_text(tally: dict[str, Any]) -> str:
    avg = "—" if tally["avg_ret"] is None else f"{tally['avg_ret'] * 100:+.1f}%"
    return f"Season tally: {tally['trades']} trades, {tally['wins']}W/{tally['losses']}L, {avg} avg."


def build_result_text(t: dict[str, Any], tally: dict[str, Any]) -> str:
    if t["status"] == "no_fill":
        return (
            f"{t['symbol']} AH Drift: entry Limit {money(t['limit'])} was not reached 5:00–7:00 PM CT, "
            f"so no shadow trade. {tally_text(tally)}"
        )
    return (
        f"{t['symbol']} AH Drift result: entry Limit {money(t['limit'])} → open {money(t['exit_open'])} "
        f"= {t['ret'] * 100:+.1f}% (shadow). {tally_text(tally)}"
    )


class AhEarningsShadow:
    def __init__(
        self,
        *,
        quotes: Any | None = None,
        calendar: EarningsCalendar | None = None,
        sender: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        ledger_path=None,
    ) -> None:
        if quotes is None:
            from odte_scanner.ext_hours.quotes import QuoteSource

            quotes = QuoteSource()
        self.quotes = quotes
        self.calendar = calendar if calendar is not None else EarningsCalendar()
        self.sender = sender if sender is not None else default_sender
        self.clock = clock
        self.ledger = JsonLedger(
            ledger_path or resolve_path("AH_EARNINGS_SHADOW_PATH", "outputs/ah_earnings_shadow.json"),
            {"desk": "ah_earnings_drift", "shadow": True, "trades": [], "candidates": [], "runs": {}},
        )
        self._lock = threading.RLock()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ ticks
    def tick(self, now: datetime | None = None) -> None:
        if is_off():
            return
        now = now_ct(lambda n=now: n) if now else now_ct(self.clock)
        d = now.date()
        with self._lock:
            data = self.ledger.load()
            runs = data.setdefault("runs", {})
            try:
                if is_full_trading_day(d) and in_window(now, EVAL_START, EVAL_END) and runs.get("eval") != d.isoformat():
                    self.evaluate(d, data)
                    runs["eval"] = d.isoformat()
                    self.ledger.save(data)
                if is_trading_day(d) and in_window(now, SETTLE_START, SETTLE_END) and runs.get("settle") != d.isoformat():
                    if self.settle(d, data):
                        runs["settle"] = d.isoformat()
                    self.ledger.save(data)
                if is_trading_day(d) and in_window(now, MID_START, MID_END):
                    if self._mark(d, data, "px_930"):
                        self.ledger.save(data)
                if is_trading_day(d) and in_window(now, CLOSE_START, CLOSE_END):
                    if self._mark(d, data, "exit_close"):
                        self.ledger.save(data)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                logger.warning("ah earnings shadow tick failed: %s", self.last_error)

    # -------------------------------------------------------------- 5:00 PM CT
    def evaluate(self, day: date, data: dict[str, Any]) -> list[dict[str, Any]]:
        cal = self.calendar.amc_names(day)
        names = list(cal.get("names") or [])
        data["calendar"] = {"date": day.isoformat(), "names": names, "errors": cal.get("errors") or []}
        fired: list[dict[str, Any]] = []
        if not names:
            return fired
        quotes = self.quotes.quotes(names) or {}
        for sym in names:
            cand = self._evaluate_one(sym, day, quotes.get(sym) or {})
            data.setdefault("candidates", []).append(cand)
            if cand.get("decision") != "ALERT":
                continue
            trade = cand["trade"]
            text = build_alert_text(trade)
            trade["telegram_ok"] = self._send(text)
            data.setdefault("trades", []).append(trade)
            fired.append(trade)
        data["candidates"] = data["candidates"][-300:]
        return fired

    def _evaluate_one(self, sym: str, day: date, q: dict[str, Any]) -> dict[str, Any]:
        px = self.quotes.ah_prices(sym, day) or {}
        C, P4, P5 = px.get("C"), px.get("P4"), px.get("P5")
        cand: dict[str, Any] = {"date": day.isoformat(), "symbol": sym, "C": _r2(C), "P4": _r2(P4), "P5": _r2(P5)}
        if not C or not P5:
            cand["decision"] = "SKIP_NO_PRICE"
            return cand
        R = P5 / C - 1
        cand["R"] = round(R, 5)
        if abs(R) < THRESHOLD:
            cand["decision"] = "NO_SIGNAL"
            return cand
        if P4 and abs(P5 / P4 - 1) >= PRINT_CHECK:
            cand["decision"] = "SKIP_PRINT_CHECK"
            return cand
        bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
        spread = None
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            cand["spread"] = round(spread, 5)
            if spread > MAX_SPREAD:
                cand["decision"] = "SKIP_SPREAD"
                return cand
        else:
            mid = P5
        side = "LONG" if R > 0 else "SHORT"
        limit = round(mid * (1 + LIMIT_OFFSET) if side == "LONG" else mid * (1 - LIMIT_OFFSET), 2)
        trade = {
            "id": f"{day.isoformat()}-{sym}",
            "entry_date": day.isoformat(),
            "season": season_key(day),
            "symbol": sym,
            "side": side,
            "R": round(R, 5),
            "gold": abs(R) >= GOLD_THRESHOLD,
            "C": _r2(C),
            "P4": _r2(P4),
            "P5": _r2(P5),
            "bid": _r2(bid) if bid else None,
            "ask": _r2(ask) if ask else None,
            "spread": None if spread is None else round(spread, 5),
            "limit": limit,
            "status": "pending",
            "alert_ct": now_ct(self.clock).strftime("%Y-%m-%d %H:%M CT"),
            "shadow": True,
            "orders_placed": 0,
        }
        cand["decision"] = "ALERT"
        cand["trade"] = trade
        return cand

    # -------------------------------------------------------------- 8:35 AM CT
    def settle(self, day: date, data: dict[str, Any]) -> bool:
        """Settle trades from the previous trading day. True when nothing is left pending."""
        entry_day = prev_trading_day(day).isoformat()
        pending = [t for t in data.get("trades", []) if t.get("status") == "pending" and t.get("entry_date") <= entry_day]
        if not pending:
            return True
        quotes = self.quotes.quotes([t["symbol"] for t in pending]) or {}
        left = 0
        for t in pending:
            rng = self.quotes.ah_range(t["symbol"], date.fromisoformat(t["entry_date"])) or {}
            hi, lo = rng.get("high"), rng.get("low")
            if hi is None or lo is None:
                filled, verified = True, False  # no AH bars: assume the Limit filled, flag it
            else:
                filled = lo <= t["limit"] if t["side"] == "LONG" else hi >= t["limit"]
                verified = True
            t["ah_high"], t["ah_low"], t["fill_verified"] = _r2(hi), _r2(lo), verified
            if not filled:
                t["status"] = "no_fill"
            else:
                op = float((quotes.get(t["symbol"]) or {}).get("open") or 0)
                if op <= 0:
                    left += 1
                    continue
                sign = 1 if t["side"] == "LONG" else -1
                t["exit_open"] = round(op, 2)
                t["exit_date"] = day.isoformat()
                t["ret"] = round(sign * (op / t["limit"] - 1), 5)
                t["status"] = "closed"
            tally = season_tally(data.get("trades", []), t["season"])
            t["result_telegram_ok"] = self._send(build_result_text(t, tally))
        return left == 0

    def _mark(self, day: date, data: dict[str, Any], field: str) -> bool:
        todo = [t for t in data.get("trades", []) if t.get("exit_date") == day.isoformat() and t.get(field) is None]
        if not todo:
            return False
        quotes = self.quotes.quotes([t["symbol"] for t in todo]) or {}
        changed = False
        for t in todo:
            q = quotes.get(t["symbol"]) or {}
            px = float(q.get("last") or q.get("close") or 0)
            if px > 0:
                sign = 1 if t["side"] == "LONG" else -1
                t[field] = round(px, 2)
                t[field + "_ret"] = round(sign * (px / t["limit"] - 1), 5)
                changed = True
        return changed

    # ---------------------------------------------------------------- helpers
    def _send(self, text: str) -> bool:
        if "sure winner" in text.lower():  # copy rule: never
            raise ValueError("forbidden phrase in alert copy")
        try:
            res = self.sender(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ah earnings telegram failed: %s", type(exc).__name__)
            return False
        return bool(isinstance(res, dict) and res.get("ok"))

    def status(self) -> dict[str, Any]:
        data = self.ledger.load()
        trades = data.get("trades", [])
        latest_season = trades[-1]["season"] if trades else season_key(now_ct(self.clock).date())
        return {
            "ok": self.last_error is None,
            "enabled": not is_off(),
            "shadow": True,
            "alert_only": True,
            "orders_placed": 0,
            "label": SHADOW_LABEL,
            "kill_switch_env": KILL_ENV,
            "rules": {
                "trigger_ct": "5:00 PM", "threshold": THRESHOLD, "gold_threshold": GOLD_THRESHOLD,
                "print_check": PRINT_CHECK, "max_spread": MAX_SPREAD, "exit": "next 8:30 AM CT open",
            },
            "calendar": data.get("calendar"),
            "runs": data.get("runs", {}),
            "season_tally": season_tally(trades, latest_season),
            "recent_trades": list(reversed(trades[-25:])),
            "recent_candidates": list(reversed(data.get("candidates", [])[-25:])),
            "last_error": self.last_error,
            "warning": WARNING_LINE,
        }
