"""S1 Overnight Bias — CONTEXT ONLY (no orders, not a trade signal).

At 2:55 PM CT on full trading days, if QQQ or IWM sits in the bottom 25% of
its day range, show a context line on the portal and send one short
Telegram note. Backtest edge (2000–2026, close→open): about +5–8 bp per
night; tradable via MNQ (QQQ) / M2K (IWM) micro futures.

Every night's close→open result for both symbols is logged (triggered or
not) to ``outputs/overnight_bias_shadow.json`` so the two can be compared.
Kill switch: ``OVERNIGHT_BIAS_OFF=1``. No broker or router imports.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time
from typing import Any, Callable

from odte_scanner.ext_hours.common import (
    JsonLedger,
    default_sender,
    env_flag,
    in_window,
    is_full_trading_day,
    is_trading_day,
    money,
    now_ct,
    prev_trading_day,
    resolve_path,
)

logger = logging.getLogger(__name__)

KILL_ENV = "OVERNIGHT_BIAS_OFF"
SYMBOLS = ("QQQ", "IWM")
FUTURES = {"QQQ": "MNQ", "IWM": "M2K"}
BOTTOM = 0.25
CHECK_START, CHECK_END = time(14, 55), time(15, 0)
SETTLE_START, SETTLE_END = time(8, 35), time(10, 30)
EDGE_LINE = "Backtest edge: about +5–8bp/night close→open (2000–2026)"


def is_off() -> bool:
    return env_flag(KILL_ENV)


def context_line(row: dict[str, Any]) -> str:
    sym = row["symbol"]
    return (
        f"🌙 Overnight bias (context only): {sym} in bottom 25% of today's range "
        f"({row['range_pos'] * 100:.0f}%). {EDGE_LINE}; tradable via {FUTURES[sym]} futures. No order."
    )


def build_note(rows: list[dict[str, Any]]) -> str:
    lines = ["🌙 OVERNIGHT BIAS — context only (no order placed)"]
    for r in rows:
        lines.append(
            f"{r['symbol']} at {r['range_pos'] * 100:.0f}% of today's range "
            f"(last {money(r['last'])}, range {money(r['low'])}–{money(r['high'])}) → tradable via {FUTURES[r['symbol']]} futures"
        )
    lines.append(EDGE_LINE + ".")
    lines.append("Context only — not a trade signal. No orders.")
    return "\n".join(lines)


class OvernightBias:
    def __init__(
        self,
        *,
        quotes: Any | None = None,
        sender: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        ledger_path=None,
    ) -> None:
        if quotes is None:
            from odte_scanner.ext_hours.quotes import QuoteSource

            quotes = QuoteSource(use_polygon=False)
        self.quotes = quotes
        self.sender = sender if sender is not None else default_sender
        self.clock = clock
        self.ledger = JsonLedger(
            ledger_path or resolve_path("OVERNIGHT_BIAS_PATH", "outputs/overnight_bias_shadow.json"),
            {"desk": "overnight_bias", "context_only": True, "nights": [], "runs": {}},
        )
        self._lock = threading.RLock()
        self.last_error: str | None = None

    def tick(self, now: datetime | None = None) -> None:
        if is_off():
            return
        now = now_ct(lambda n=now: n) if now else now_ct(self.clock)
        d = now.date()
        with self._lock:
            data = self.ledger.load()
            runs = data.setdefault("runs", {})
            try:
                if is_full_trading_day(d) and in_window(now, CHECK_START, CHECK_END) and runs.get("check") != d.isoformat():
                    if self.check(d, data):
                        runs["check"] = d.isoformat()
                    self.ledger.save(data)
                if is_trading_day(d) and in_window(now, SETTLE_START, SETTLE_END) and runs.get("settle") != d.isoformat():
                    if self.settle(d, data):
                        runs["settle"] = d.isoformat()
                    self.ledger.save(data)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                logger.warning("overnight bias tick failed: %s", self.last_error)

    def check(self, day: date, data: dict[str, Any]) -> bool:
        quotes = self.quotes.quotes(list(SYMBOLS)) or {}
        rows = []
        for sym in SYMBOLS:
            q = quotes.get(sym) or {}
            last, hi, lo = (float(q.get(k) or 0) for k in ("last", "high", "low"))
            if last <= 0 or hi <= 0 or lo <= 0 or hi < lo:
                continue
            pos = 0.5 if hi == lo else (last - lo) / (hi - lo)
            rows.append({
                "date": day.isoformat(), "symbol": sym, "last_255": round(last, 2), "last": round(last, 2),
                "high": round(hi, 2), "low": round(lo, 2), "range_pos": round(pos, 4),
                "triggered": pos < BOTTOM, "futures": FUTURES[sym], "context_only": True, "orders_placed": 0,
            })
        if not rows:
            return False
        nights = data.setdefault("nights", [])
        keys = {(n["date"], n["symbol"]) for n in nights}
        for r in rows:
            if (r["date"], r["symbol"]) not in keys:
                nights.append(r)
        data["nights"] = nights[-800:]
        hits = [r for r in rows if r["triggered"]]
        data["today"] = {"date": day.isoformat(), "rows": rows, "lines": [context_line(r) for r in hits]}
        if hits:
            note = build_note(hits)
            if "sure winner" in note.lower():
                raise ValueError("forbidden phrase in alert copy")
            try:
                res = self.sender(note)
                ok = bool(isinstance(res, dict) and res.get("ok"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("overnight bias telegram failed: %s", type(exc).__name__)
                ok = False
            for r in hits:
                r["telegram_ok"] = ok
        return True

    def settle(self, day: date, data: dict[str, Any]) -> bool:
        """Fill close (official prior close) and today's open for last night's rows."""
        prev = prev_trading_day(day).isoformat()
        todo = [n for n in data.get("nights", []) if n.get("date") <= prev and n.get("ret") is None]
        if not todo:
            return True
        quotes = self.quotes.quotes(list({n["symbol"] for n in todo})) or {}
        left = 0
        for n in todo:
            q = quotes.get(n["symbol"]) or {}
            op, pc = float(q.get("open") or 0), float(q.get("prevclose") or 0)
            if n["date"] != prev:  # missed a morning: can't reconstruct, mark it
                n["ret"], n["note"] = None, "missed settle"
                n["settled"] = False
                continue
            if op <= 0 or pc <= 0:
                left += 1
                continue
            n.update({"close": round(pc, 2), "open_next": round(op, 2), "ret": round(op / pc - 1, 6), "settled": True})
        return left == 0

    def status(self) -> dict[str, Any]:
        data = self.ledger.load()
        nights = [n for n in data.get("nights", []) if n.get("settled")]

        def agg(rows):
            if not rows:
                return {"nights": 0, "avg_bp": None, "win_pct": None}
            rets = [r["ret"] for r in rows]
            return {"nights": len(rows), "avg_bp": round(sum(rets) / len(rets) * 1e4, 2),
                    "win_pct": round(100 * sum(1 for x in rets if x > 0) / len(rets), 1)}

        by_sym = {}
        for sym in SYMBOLS:
            rs = [n for n in nights if n["symbol"] == sym]
            by_sym[sym] = {"triggered": agg([n for n in rs if n["triggered"]]), "all": agg(rs)}
        today = data.get("today") or {}
        return {
            "ok": self.last_error is None,
            "enabled": not is_off(),
            "context_only": True,
            "orders_placed": 0,
            "kill_switch_env": KILL_ENV,
            "rule": "2:55 PM CT: QQQ/IWM in bottom 25% of day range → context line + Telegram note",
            "edge": EDGE_LINE,
            "futures": FUTURES,
            "today": today,
            "by_symbol": by_sym,
            "recent_nights": list(reversed(data.get("nights", [])[-30:])),
            "runs": data.get("runs", {}),
            "last_error": self.last_error,
        }
