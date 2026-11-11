"""Replay the Challenge sleeve on option minute bars.

The live desk and this replay share the mechanical rules: one open flip,
0–1 DTE, premium $0.20–$2.50, calls unless the tape is dumping, snipers
(SPY/QQQ/IWM) first, 30% of sleeve cash and at most 2 contracts, skip a
name after a loss for 7 days and after two lifetime losses, pause after
three losses and then block snipers.

Two exit books are reported because the journal and the Webull order
path do not use the same percents:

* sleeve — snipers bank +20%, stop −20%, cut −12% after ~1h, flat at 4h.
  Anything else uses the configured +100% / −50%.
* webull — the paper order path: +100% / −50%, 1 contract on 0 DTE,
  at most 2, and a 15:45 ET flatten. A 2-lot that is not 0 DTE can
  sell half at +40%.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
INDEX = frozenset({"SPY", "QQQ", "IWM"})
CORE = INDEX | frozenset({"NVDA", "TSLA", "AMD", "META", "MU", "AAPL", "MSFT", "AMZN", "GOOGL"})
ENTRY_AT = time(9, 35)
ENTRY_DEADLINE = time(9, 45)
FLAT_AT = time(15, 45)


@dataclass
class Bar:
    at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Session:
    contract: str
    symbol: str
    expiry: date
    right: str
    strike: float
    day: date
    bars: list[Bar]
    dte: int = 0

    @property
    def sniper(self) -> bool:
        return self.symbol in INDEX


@dataclass
class Fill:
    day: str
    contract: str
    symbol: str
    right: str
    dte: int
    qty: int
    entry: float
    exit: float
    entry_at: str
    exit_at: str
    reason: str
    pnl: float
    invested: float
    equity_after: float

    def to_dict(self) -> dict[str, Any]:
        won = self.pnl > 0
        return {
            "day": self.day,
            "contract": self.contract,
            "symbol": self.symbol,
            "right": self.right,
            "dte": self.dte,
            "sniper": self.symbol in INDEX,
            "qty": self.qty,
            "entry": round(self.entry, 4),
            "exit": round(self.exit, 4),
            "entry_at": self.entry_at,
            "exit_at": self.exit_at,
            "reason": self.reason,
            "pnl": round(self.pnl, 2),
            "invested": round(self.invested, 2),
            "equity_after": round(self.equity_after, 2),
            "win": won,
        }


@dataclass
class Book:
    name: str
    cash: float = 1000.0
    trades: list[Fill] = field(default_factory=list)
    consecutive_losses: int = 0
    last_loss: date | None = None
    loss_days: dict[str, list[date]] = field(default_factory=dict)
    lifetime_losses: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)

    @property
    def equity(self) -> float:
        return self.cash


def parse_occ(contract: str) -> tuple[str, date, str, float] | None:
    match = _OCC.match(str(contract or "").upper())
    if not match:
        return None
    symbol, yymmdd, right, strike = match.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return symbol, expiry, right, int(strike) / 1000.0


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:
        return None
    return number


def bars_from_payload(payload: Any) -> list[Bar]:
    series = payload.get("series") if isinstance(payload, dict) else None
    rows = series.get("data") if isinstance(series, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        close = _f(row.get("close"))
        if close is None or close <= 0:
            close = _f(row.get("price"))
        high = _f(row.get("high"))
        low = _f(row.get("low"))
        opened = _f(row.get("open"))
        if close is None or close <= 0:
            continue
        if high is None or high <= 0:
            high = close
        if low is None or low <= 0:
            low = close
        if opened is None or opened <= 0:
            opened = close
        stamp = str(row.get("time") or "")
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        try:
            volume = int(float(row.get("volume") or 0))
        except (TypeError, ValueError):
            volume = 0
        out.append(Bar(at=at, open=opened, high=max(high, opened, close), low=min(low, opened, close), close=close, volume=max(0, volume)))
    out.sort(key=lambda bar: bar.at)
    return out


def _entry_index(bars: list[Bar]) -> int | None:
    for index, bar in enumerate(bars):
        if ENTRY_AT <= bar.at.time() <= ENTRY_DEADLINE:
            return index
    return None


def _touch(bar: Bar, level: float, *, side: str) -> float | None:
    """Fill at the level. A gap through it fills at the open."""
    if side == "sell":
        if bar.open >= level:
            return bar.open
        if bar.high >= level:
            return level
        return None
    if bar.open <= level:
        return bar.open
    if bar.low <= level:
        return level
    return None


def _dollars(entry: float, exit_px: float, qty: int) -> float:
    return (exit_px - entry) * 100.0 * qty


def walk_sleeve(session: Session, entry_i: int, entry: float, qty: int) -> tuple[float, str, str, float]:
    """Return exit price, reason, exit timestamp, pnl. Snipers use +20/−20/4h."""
    start = session.bars[entry_i].at
    sniper = session.sniper
    stop_pct = 0.20 if sniper else 0.50
    target_pct = 0.20 if sniper else 1.00
    stop_px = entry * (1.0 - stop_pct)
    target_px = entry * (1.0 + target_pct)
    early_px = entry * 0.88
    for bar in session.bars[entry_i:]:
        held = (bar.at - start).total_seconds() / 60.0
        target_ready = held >= 15 if sniper else True
        stopped = _touch(bar, stop_px, side="buy")
        banked = _touch(bar, target_px, side="sell") if target_ready else None
        if stopped is not None and banked is not None:
            if bar.open >= target_px:
                return banked, ("bank sniper +20%" if sniper else "target +100%"), bar.at.isoformat(timespec="minutes"), _dollars(entry, banked, qty)
            return stopped, f"stop −{stop_pct * 100:.0f}%", bar.at.isoformat(timespec="minutes"), _dollars(entry, stopped, qty)
        if stopped is not None:
            return stopped, f"stop −{stop_pct * 100:.0f}%", bar.at.isoformat(timespec="minutes"), _dollars(entry, stopped, qty)
        if sniper and held >= 58:
            cut = _touch(bar, early_px, side="buy")
            if cut is not None and (banked is None or bar.open < target_px):
                return cut, "sniper early cut −12%", bar.at.isoformat(timespec="minutes"), _dollars(entry, cut, qty)
        if banked is not None:
            why = "bank sniper +20%" if sniper else "target +100%"
            return banked, why, bar.at.isoformat(timespec="minutes"), _dollars(entry, banked, qty)
        if sniper and held >= 240:
            return bar.close, "sniper flatten 4h", bar.at.isoformat(timespec="minutes"), _dollars(entry, bar.close, qty)
        if bar.at.time() >= FLAT_AT:
            return bar.close, "flatten 15:45 ET", bar.at.isoformat(timespec="minutes"), _dollars(entry, bar.close, qty)
    last = session.bars[-1]
    return last.close, "flatten last bar", last.at.isoformat(timespec="minutes"), _dollars(entry, last.close, qty)


def walk_webull(session: Session, entry_i: int, entry: float, qty: int) -> tuple[float, str, str, float]:
    """+100/−50 and 15:45 ET. A 2-lot that is not 0 DTE sells one at +40%."""
    start = session.bars[entry_i].at
    stop_px = entry * 0.50
    target_px = entry * 2.0
    scale = qty >= 2 and session.dte != 0
    sold = 0
    pnl = 0.0
    peak = entry
    trail_on = False
    for bar in session.bars[entry_i:]:
        peak = max(peak, bar.high)
        held_open = qty - sold
        stopped = _touch(bar, stop_px, side="buy")
        if stopped is not None and held_open > 0:
            pnl += _dollars(entry, stopped, held_open)
            return stopped, "stop −50%", bar.at.isoformat(timespec="minutes"), pnl
        if scale and sold == 0:
            partial = _touch(bar, entry * 1.40, side="sell")
            if partial is not None:
                pnl += _dollars(entry, partial, 1)
                sold = 1
        if peak >= target_px:
            trail_on = True
        if trail_on and scale and sold >= 1:
            trail_px = peak * 0.80
            trailed = _touch(bar, trail_px, side="buy")
            if trailed is not None and held_open - 0 > 0 and trailed < bar.high:
                rest = qty - sold
                pnl += _dollars(entry, trailed, rest)
                return trailed, "trail −20% from peak", bar.at.isoformat(timespec="minutes"), pnl
        if not scale or sold == 0:
            banked = _touch(bar, target_px, side="sell")
            if banked is not None:
                pnl += _dollars(entry, banked, qty - sold)
                return banked, "target +100%", bar.at.isoformat(timespec="minutes"), pnl
        elif sold >= 1 and bar.high >= target_px:
            # 30% of 2 contracts rounds down to 0, so the runner waits for the trail or the clock.
            pass
        if bar.at.time() >= FLAT_AT:
            rest = qty - sold
            pnl += _dollars(entry, bar.close, rest)
            why = "flatten 15:45 ET" if sold == 0 else "flatten 15:45 ET after +40% scale"
            return bar.close, why, bar.at.isoformat(timespec="minutes"), pnl
    last = session.bars[-1]
    rest = qty - sold
    pnl += _dollars(entry, last.close, rest)
    return last.close, "flatten last bar", last.at.isoformat(timespec="minutes"), pnl


def _mode(book: Book, day: date) -> str:
    if book.consecutive_losses < 3:
        return "ok"
    if book.last_loss is None:
        return "paused"
    if (day - book.last_loss).days >= 2:
        return "quality_only"
    return "paused"


def _symbol_blocked(book: Book, symbol: str, day: date) -> str | None:
    if book.lifetime_losses[symbol] >= 2:
        return "lifetime_two_losses"
    for previous in book.loss_days.get(symbol) or []:
        if (day - previous).days <= 7:
            return "loss_cooldown_7d"
    return None


def _size_sleeve(cash: float, premium: float) -> int:
    unit = premium * 100.0
    frac = cash * 0.30
    if unit > frac + 1e-9 or unit > cash + 1e-9:
        return 0
    qty = max(1, min(2, int(frac // unit)))
    while qty > 1 and qty * unit > frac + 1e-9:
        qty -= 1
    return qty


def _size_webull(premium: float, dte: int) -> int:
    if dte == 0:
        return 1
    qty = 1
    while qty < 2 and (qty + 1) * premium * 100.0 <= 250.0 + 1e-6:
        qty += 1
    return qty


def _dumping(under: list[Bar] | None, when: datetime) -> bool | None:
    if not under:
        return None
    now = next((bar for bar in under if bar.at >= when), None)
    if now is None:
        return None
    open_bar = under[0]
    session = (now.close - open_bar.open) / open_bar.open * 100.0 if open_bar.open else 0.0
    prior = None
    cutoff = when - timedelta(minutes=5)
    for bar in under:
        if bar.at <= cutoff:
            prior = bar
        else:
            break
    mom = None
    if prior and prior.close:
        mom = (now.close - prior.close) / prior.close * 100.0
    return (mom is not None and mom <= -0.25) or session <= -0.8


def _record(book: Book, session: Session, qty: int, entry: float, entry_at: str, exit_px: float, exit_at: str, reason: str, pnl: float) -> None:
    # Flat at the close of every replayed session, so equity is cash plus this P&L.
    book.cash = round(book.cash + pnl, 2)
    fill = Fill(
        day=session.day.isoformat(),
        contract=session.contract,
        symbol=session.symbol,
        right=session.right,
        dte=session.dte,
        qty=qty,
        entry=entry,
        exit=exit_px,
        entry_at=entry_at,
        exit_at=exit_at,
        reason=reason,
        pnl=pnl,
        invested=qty * entry * 100.0,
        equity_after=book.cash,
    )
    book.trades.append(fill)
    if pnl < 0:
        book.consecutive_losses += 1
        book.last_loss = session.day
        book.loss_days.setdefault(session.symbol, []).append(session.day)
        book.lifetime_losses[session.symbol] += 1
    elif pnl > 0:
        book.consecutive_losses = 0


def replay_books(
    sessions: list[Session],
    underlying: dict[tuple[str, date], list[Bar]] | None = None,
    *,
    start_cash: float = 1000.0,
) -> dict[str, Any]:
    """One compounded path per exit book, sharing the same entry choices."""
    underlying = underlying or {}
    by_day: dict[date, list[Session]] = {}
    for session in sessions:
        if session.dte not in (0, 1) or len(session.bars) < 5:
            continue
        by_day.setdefault(session.day, []).append(session)
    sleeve = Book("sleeve", cash=start_cash)
    webull = Book("webull", cash=start_cash)
    considered = 0
    for day in sorted(by_day):
        mode = _mode(sleeve, day)
        if mode == "paused":
            sleeve.skipped["paused_3_losses"] += 1
            webull.skipped["paused_3_losses"] += 1
            continue
        if mode == "quality_only":
            sleeve.skipped["quality_only_blocks_snipers"] += 1
            webull.skipped["quality_only_blocks_snipers"] += 1
            continue
        ranked: list[tuple[tuple, Session, int, float]] = []
        for session in by_day[day]:
            considered += 1
            entry_i = _entry_index(session.bars)
            if entry_i is None:
                sleeve.skipped["no_0935_bar"] += 1
                continue
            entry_bar = session.bars[entry_i]
            entry = entry_bar.close
            if entry < 0.20 or entry > 2.50:
                sleeve.skipped["premium_band"] += 1
                continue
            volume = sum(bar.volume for bar in session.bars[: entry_i + 1])
            if volume < 100:
                sleeve.skipped["thin_volume"] += 1
                continue
            if session.right == "P":
                dumping = _dumping(underlying.get((session.symbol, day)), entry_bar.at)
                if dumping is not True:
                    sleeve.skipped["put_without_dump"] += 1
                    continue
            blocked = _symbol_blocked(sleeve, session.symbol, day)
            if blocked:
                sleeve.skipped[blocked] += 1
                continue
            qty = _size_sleeve(sleeve.cash, entry)
            if qty < 1:
                sleeve.skipped["over_cash_frac"] += 1
                continue
            key = (
                0 if session.sniper else 1,
                entry if session.sniper else 0.0,
                0 if session.right == "C" else 1,
                0 if session.dte == 0 else 1,
                0 if session.symbol in CORE else 1,
                -volume,
            )
            ranked.append((key, session, entry_i, entry))
        if not ranked:
            sleeve.skipped["no_candidate"] += 1
            continue
        ranked.sort(key=lambda item: item[0])
        _key, session, entry_i, entry = ranked[0]
        qty_s = _size_sleeve(sleeve.cash, entry)
        exit_px, reason, exit_at, pnl = walk_sleeve(session, entry_i, entry, qty_s)
        _record(sleeve, session, qty_s, entry, session.bars[entry_i].at.isoformat(timespec="minutes"), exit_px, exit_at, reason, pnl)
        qty_w = _size_webull(entry, session.dte)
        # Webull cash is its own book. If 1 lot would exceed what is left, skip.
        if qty_w * entry * 100.0 > webull.cash + 1e-6:
            webull.skipped["over_cash"] += 1
        else:
            w_px, w_reason, w_at, w_pnl = walk_webull(session, entry_i, entry, qty_w)
            _record(webull, session, qty_w, entry, session.bars[entry_i].at.isoformat(timespec="minutes"), w_px, w_at, w_reason, w_pnl)
    return {
        "sleeve": _summarize(sleeve, start_cash),
        "webull": _summarize(webull, start_cash),
        "sessions_considered": considered,
        "days": len(by_day),
    }


def _summarize(book: Book, start_cash: float) -> dict[str, Any]:
    pnls = [trade.pnl for trade in book.trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    invested = sum(trade.invested for trade in book.trades)
    equity = start_cash
    peak = start_cash
    max_dd = 0.0
    path = [{"at": "start", "equity": round(start_cash, 2)}]
    for trade in book.trades:
        equity = trade.equity_after
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        path.append({"at": trade.exit_at, "equity": round(equity, 2), "pnl": round(trade.pnl, 2), "symbol": trade.symbol})
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(book.trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(book.trades), 1) if book.trades else None,
        "pnl_usd": round(sum(pnls), 2),
        "invested_usd": round(invested, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else None,
        "profit_factor_infinite": bool(wins) and gross_loss <= 1e-9,
        "max_drawdown_usd": round(max_dd, 2),
        "start_equity": start_cash,
        "end_equity": round(book.cash, 2),
        "equity_path": path,
        "skipped": dict(book.skipped),
        "fills": [trade.to_dict() for trade in book.trades],
    }


def load_bars_dir(root: Path) -> tuple[list[Session], dict[tuple[str, date], list[Bar]], dict[str, Any]]:
    """Read a tradier_bars directory. Skips empty and non-JSON responses."""
    root = Path(root)
    coverage = root / "coverage.csv"
    sessions: list[Session] = []
    notes: Counter = Counter()
    if not coverage.exists():
        raise FileNotFoundError(coverage)
    with coverage.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("asset_type") != "options":
                continue
            day_text = str(row.get("date") or "")
            if day_text in {"", "daily"}:
                continue
            try:
                n_bars = int(row.get("bars_returned") or 0)
            except ValueError:
                n_bars = 0
            if n_bars <= 0 or "HTTP 200" not in str(row.get("note") or ""):
                notes["option_session_without_bars"] += 1
                continue
            parsed = parse_occ(row.get("symbol") or "")
            if parsed is None:
                notes["unparsed_occ"] += 1
                continue
            symbol, expiry, right, strike = parsed
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                continue
            path = root / str(row.get("path") or "")
            if not path.exists():
                notes["missing_file"] += 1
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                notes["bad_json"] += 1
                continue
            bars = bars_from_payload(payload)
            if len(bars) < 5:
                notes["short_session"] += 1
                continue
            sessions.append(
                Session(
                    contract=str(row.get("symbol")),
                    symbol=symbol,
                    expiry=expiry,
                    right=right,
                    strike=strike,
                    day=day,
                    bars=bars,
                    dte=(expiry - day).days,
                )
            )
            notes["loaded_sessions"] += 1
    underlying: dict[tuple[str, date], list[Bar]] = {}
    under_root = root / "underlying"
    if under_root.exists():
        for path in under_root.glob("*/*.json"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            bars = bars_from_payload(payload)
            if bars:
                underlying[(path.parent.name.upper(), day)] = bars
    meta = {
        "sessions_loaded": len(sessions),
        "underlying_days": len(underlying),
        "notes": dict(notes),
        "index_sessions": sum(1 for session in sessions if session.symbol in INDEX and session.dte in (0, 1)),
        "dte_0_or_1": sum(1 for session in sessions if session.dte in (0, 1)),
    }
    return sessions, underlying, meta


def run_directory(root: Path) -> dict[str, Any]:
    sessions, underlying, meta = load_bars_dir(root)
    result = replay_books(sessions, underlying)
    result["data"] = meta
    result["caveats"] = [
        "The archive has no rows tagged Challenge. It is contracts from Webull orders, round trips, and signal-ledger events, not the full 0–1 DTE chain.",
        "SPXW has no underlying bars. Many option files are HTTP 400 or daily-only; daily candles are not used, because a daily high/low cannot order a stop ahead of a target.",
        "Entry is the 9:35 ET minute close, not the ask. There is no bid/ask, so slippage is not modeled.",
        "Open interest is not in the file, so the OI ≥ 200 rule is not applied. Volume uses the 9:30–9:35 sum and must be at least 100.",
        "Hist-win, ensemble, radar HOT, earnings, and UW flow are not in the bars. After three losses the sleeve goes quality-only and this replay stops, because a non-sniper cannot be shown to be perfect/elite hist.",
        "Puts need a dump on the underlying minute tape (5-minute momentum ≤ −0.25% or session ≤ −0.8%). Missing underlying tape skips the put.",
        "The November journal (8 trades, 37.5% wins, +$83 on $504) is not in this archive and not in the repo, so those fills are not replayed tick for tick.",
        "Only November 5 and November 6 in this archive have 0–1 DTE minute bars. Earlier files are longer-dated, and the sprint desk does not enter them. Most short-dated prints were outside $0.20–$2.50 at 9:35.",
        "A sniper does not bank +20% until about 15 minutes after entry. If the option has already run through that price, the exit is the next bar's open, which can be well above +20%.",
        "A stop is filled from the bar's low. If that low printed before the 9:35 close used as the entry, the stop is early. Neither of the two fills below stopped on the entry minute.",
    ]
    result["reported_november_journal"] = {
        "trades": 8,
        "win_rate_pct": 37.5,
        "pnl_usd": 83,
        "invested_usd": 504,
        "source": "owner note for the November journal sleeve",
        "present_in_archive": False,
        "present_in_repo": False,
    }
    return result
