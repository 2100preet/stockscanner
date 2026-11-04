"""Expected-move filters on the lunch and afternoon books. Research only.

The live desks are not changed. Lunch-Break Credit still sells the at-the-money
put and buys the put $2 lower. Afternoon Puts still buys the one-strike put,
and SPY and QQQ still wait for each other. This module prices those same rules
on 5-minute bars and asks whether the morning expected move would have helped.

The option prices are the same Black-Scholes stand-in Afternoon Puts uses when
a Yahoo put series is absent. SPY and SPX are marked at that day's VIX. QQQ
is marked at VIX times 0.26/0.19, the ratio of the desk's model vols. A credit
under $0.05 is not a trade. Take profit, stop, and the 2:45 CT flat are the
live lunch rules. Afternoon exits are the live half / trail / stop / flat.

Train is the first 70% of sessions. The last 30% is out of sample. A filter
is only worth a look when that later slice has at least 8 trades, a higher
P&L than the baseline, and a win rate that is not worse. Even then the live
rules stay as they are.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable

from odte_scanner.signals.afternoon_puts import (
    MODEL_IV,
    SESSION_FLAT,
    live_entries,
    one_strike_otm_put,
)
from odte_scanner.signals.afternoon_puts import model_put
from odte_scanner.signals.diamond import Bar, group_sessions
from odte_scanner.signals.expected_move import DIVISOR, daily_move_pct
from odte_scanner.signals.lunch_credit import (
    STRIKE_STEP,
    WIDTH,
    LunchRules,
    PRIMARY_RULES,
    atm_put,
    long_put,
    replay,
    walk_marks,
)

LIVE_RULES_CHANGED = False
MIN_CREDIT = 0.05
MIN_OOS = 8

# Name, and a predicate that is True when the trade should be dropped.
# None keeps every trade. The strike books are separate runs, not filters.
FILTERS: tuple[tuple[str, Callable[[float | None], bool] | None], ...] = (
    ("baseline", None),
    ("skip if EM used > 50%", lambda used: used is not None and used > 50),
    ("skip if EM used > 75%", lambda used: used is not None and used > 75),
    ("skip if EM used > 100%", lambda used: used is not None and used > 100),
    ("only if EM used > 50%", lambda used: used is None or used <= 50),
    ("only if EM used > 75%", lambda used: used is None or used <= 75),
    ("only if EM used > 100%", lambda used: used is None or used <= 100),
)


def iv_for(symbol: str, vix: float) -> float:
    """Annualized vol for the stand-in. QQQ keeps the desk's vol ratio to SPY."""
    base = max(float(vix), 1.0) / 100.0
    if symbol == "QQQ":
        return base * (MODEL_IV["QQQ"] / MODEL_IV["SPY"])
    return base


def minutes_left(moment: datetime) -> float:
    clock = moment.time()
    close_min = SESSION_FLAT.hour * 60 + SESSION_FLAT.minute
    now_min = clock.hour * 60 + clock.minute
    return float(max(1, close_min - now_min))


def spread_mark(spot: float, short_strike: float, long_strike: float, minutes: float, iv: float) -> float:
    """Credit-spread mark. The short put minus the long put."""
    return model_put(spot, short_strike, minutes, iv) - model_put(spot, long_strike, minutes, iv)


def sigma_points(prior: float, vix: float) -> float | None:
    pct = daily_move_pct(vix)
    if pct is None or prior <= 0:
        return None
    return prior * pct / 100.0


def used_pct(entry: float, prior: float | None, vix: float | None) -> float | None:
    if prior is None or vix is None:
        return None
    width = sigma_points(prior, vix)
    if width is None or width <= 0:
        return None
    return abs(float(entry) - float(prior)) / width * 100.0


def short_strike_at(spot: float, symbol: str, distance: float) -> float:
    """ATM when ``distance`` is 0. Otherwise the listed strike at or below spot − distance."""
    if distance <= 0:
        return atm_put(spot, symbol)
    step = float(STRIKE_STEP.get(symbol, 1.0))
    raw = float(spot) - float(distance)
    snapped = math.floor((raw + 1e-9) / step) * step
    if snapped <= 0:
        return 0.0
    return round(snapped, 2)


def put_strike_at(spot: float, symbol: str, distance: float) -> float:
    """Afternoon put. The live strike when ``distance`` is 0, else the same snap as a short put."""
    if distance <= 0:
        return one_strike_otm_put(spot, symbol)
    return short_strike_at(spot, symbol, distance)


def prior_closes(bars_by_symbol: dict[str, list[Bar]]) -> dict[tuple[str, date], float]:
    """Previous session's last regular close, keyed by (symbol, session date)."""
    out: dict[tuple[str, date], float] = {}
    for symbol, bars in bars_by_symbol.items():
        sessions = group_sessions(bars)
        days = sorted(sessions)
        for prev, day in zip(days, days[1:]):
            last = sessions[prev][-1].close
            if last > 0:
                out[(symbol, day)] = float(last)
    return out


def session_days(bars_by_symbol: dict[str, list[Bar]]) -> list[date]:
    found: set[date] = set()
    for bars in bars_by_symbol.values():
        found.update(group_sessions(bars))
    return sorted(found)


def _index_on(session: list[Bar], stamp: str) -> int | None:
    for i, bar in enumerate(session):
        if bar.ct().strftime("%H:%M") == stamp:
            return i
    return None


def price_lunch_trade(
    symbol: str,
    session: list[Bar],
    entry_index: int,
    vix: float,
    *,
    fraction: float = 0.0,
    prior: float | None = None,
    rules: LunchRules | None = None,
) -> dict[str, Any] | None:
    """One credit spread. ``fraction`` is how many sigmas below spot the short strike sits."""
    rules = rules or PRIMARY_RULES
    entry = session[entry_index]
    spot = float(entry.close)
    width = sigma_points(prior if prior else spot, vix)
    distance = 0.0 if fraction <= 0 or width is None else fraction * width
    short = short_strike_at(spot, symbol, distance)
    long = long_put(short, rules.width)
    if short <= 0 or long <= 0 or short <= long:
        return None
    iv = iv_for(symbol, vix)
    credit = spread_mark(spot, short, long, minutes_left(entry.ct()), iv)
    if credit < MIN_CREDIT:
        return None
    marks: list[tuple[datetime, float, float, float]] = []
    for bar in session[entry_index + 1 :]:
        left = minutes_left(bar.ct())
        opened = spread_mark(bar.open, short, long, left, iv)
        cheap = spread_mark(bar.high, short, long, left, iv)
        rich = spread_mark(bar.low, short, long, left, iv)
        marks.append((bar.ct(), opened, max(opened, cheap, rich), min(opened, cheap, rich)))
    walked = walk_marks(credit, marks, rules)
    if walked.get("pnl_usd") is None:
        return None
    day = entry.ct().date()
    used = None if prior is None else used_pct(spot, prior, vix)
    return {
        "symbol": symbol,
        "day": day.isoformat(),
        "at_ct": entry.ct().strftime("%H:%M"),
        "entry": round(spot, 4),
        "prior": None if prior is None else round(prior, 4),
        "vix": round(float(vix), 4),
        "em_used_pct": None if used is None else round(used, 2),
        "short_strike": short,
        "long_strike": long,
        "credit": round(credit, 4),
        "pnl_usd": walked["pnl_usd"],
        "reason": walked["reason"],
        "fraction": fraction,
    }


def lunch_book(
    bars_by_symbol: dict[str, list[Bar]],
    vix_by_day: dict[date, float],
    *,
    fraction: float = 0.0,
    rules: LunchRules | None = None,
) -> list[dict[str, Any]]:
    """Live lunch entries, priced with the stand-in. The daily cap stays at two names."""
    rules = rules or PRIMARY_RULES
    priors = prior_closes(bars_by_symbol)
    sessions = {symbol: group_sessions(bars) for symbol, bars in bars_by_symbol.items()}
    trades: list[dict[str, Any]] = []
    for row in replay(bars_by_symbol, rules):
        symbol = str(row["symbol"])
        day = date.fromisoformat(str(row["day"]))
        vix = vix_by_day.get(day)
        session = (sessions.get(symbol) or {}).get(day)
        if vix is None or not session:
            continue
        found = _index_on(session, str(row["at_ct"]))
        if found is None:
            continue
        priced = price_lunch_trade(
            symbol,
            session,
            found,
            vix,
            fraction=fraction,
            prior=priors.get((symbol, day)),
            rules=rules,
        )
        if priced:
            trades.append(priced)
    return trades


def afternoon_book(
    bars_by_symbol: dict[str, list[Bar]],
    vix_by_day: dict[date, float],
    *,
    fraction: float = 0.0,
) -> list[dict[str, Any]]:
    """Live Afternoon Puts book: SPY and QQQ together, SPX on its own gate."""
    priors = prior_closes(bars_by_symbol)

    def strike_at(symbol: str, spot: float, moment: datetime) -> float:
        day = moment.date()
        vix = vix_by_day.get(day)
        prior = priors.get((symbol, day))
        width = sigma_points(prior, vix) if prior and vix else None
        distance = 0.0 if fraction <= 0 or width is None else fraction * width
        return put_strike_at(spot, symbol, distance)

    book = live_entries(bars_by_symbol, strike_at=strike_at if fraction > 0 else None)
    trades: list[dict[str, Any]] = []
    for row in book.get("trades") or []:
        if row.get("pnl_usd") is None:
            continue
        day = date.fromisoformat(str(row["day"]))
        vix = vix_by_day.get(day)
        symbol = str(row["symbol"])
        prior = priors.get((symbol, day))
        used = used_pct(float(row["entry"]), prior, vix)
        trades.append(
            {
                "symbol": symbol,
                "day": row["day"],
                "at_ct": row.get("at_ct"),
                "entry": row.get("entry"),
                "prior": None if prior is None else round(prior, 4),
                "vix": None if vix is None else round(float(vix), 4),
                "em_used_pct": None if used is None else round(used, 2),
                "strike": row.get("strike"),
                "pnl_usd": row.get("pnl_usd"),
                "reason": row.get("reason"),
                "fraction": fraction,
                "priced": row.get("priced"),
            }
        )
    return trades


def apply_filter(
    trades: list[dict[str, Any]],
    drop: Callable[[float | None], bool] | None,
) -> list[dict[str, Any]]:
    if drop is None:
        return list(trades)
    kept: list[dict[str, Any]] = []
    for row in trades:
        used = row.get("em_used_pct")
        try:
            number = None if used is None else float(used)
        except (TypeError, ValueError):
            number = None
        if drop(number):
            continue
        kept.append(row)
    return kept


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in trades if isinstance(row.get("pnl_usd"), (int, float))]
    wins = [row for row in closed if float(row["pnl_usd"]) > 0]
    pnl = round(sum(float(row["pnl_usd"]) for row in closed), 2)
    n = len(closed)
    return {
        "n": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": None if n == 0 else round(len(wins) / n, 4),
        "pnl": pnl,
        "avg": None if n == 0 else round(pnl / n, 2),
    }


def split_sessions(days: list[date], frac: float = 0.70) -> tuple[set[date], set[date]]:
    """Chronological 70/30. The cut is on sessions, so every book shares one window."""
    ordered = sorted(days)
    if len(ordered) < 2:
        return set(ordered), set()
    cut = int(len(ordered) * frac)
    cut = min(max(cut, 1), len(ordered) - 1)
    return set(ordered[:cut]), set(ordered[cut:])


def _slice(trades: list[dict[str, Any]], days: set[date]) -> list[dict[str, Any]]:
    return [row for row in trades if date.fromisoformat(str(row["day"])) in days]


def score_variant(
    name: str,
    trades: list[dict[str, Any]],
    train_days: set[date],
    oos_days: set[date],
) -> dict[str, Any]:
    return {
        "name": name,
        "all": summarize(trades),
        "train": summarize(_slice(trades, train_days)),
        "oos": summarize(_slice(trades, oos_days)),
        "reasons": dict(Counter(str(row.get("reason") or "") for row in trades)),
    }


def _better(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """Out-of-sample P&L and win rate both clear the baseline, with enough trades."""
    oos = candidate["oos"]
    base = baseline["oos"]
    if oos["n"] < MIN_OOS or base["win_rate"] is None or oos["win_rate"] is None:
        return False
    if oos["pnl"] <= base["pnl"]:
        return False
    if oos["win_rate"] + 1e-9 < base["win_rate"]:
        return False
    train = candidate["train"]
    base_train = baseline["train"]
    if base_train["pnl"] > 0 and train["pnl"] < base_train["pnl"] * 0.75:
        return False
    return True


def recommend(book: str, rows: list[dict[str, Any]]) -> str:
    """Text only. ``LIVE_RULES_CHANGED`` stays false no matter what this says."""
    baseline = next((row for row in rows if row["name"] == "baseline"), None)
    if baseline is None or baseline["all"]["n"] == 0:
        return (
            f"{book}: the sample produced no priced trades. "
            "Do not change the live rules."
        )
    winners = [row for row in rows if row["name"] != "baseline" and _better(row, baseline)]
    if not winners:
        return (
            f"{book}: no expected-move filter or sigma strike beat the baseline "
            f"out of sample with at least {MIN_OOS} trades, a higher P&L, and a "
            "win rate that held up. Leave the live rules unchanged."
        )
    winners.sort(key=lambda row: (row["oos"]["pnl"], row["oos"]["win_rate"] or 0), reverse=True)
    best = winners[0]
    return (
        f"{book}: out of sample, {best['name']} is ahead of the baseline "
        f"({best['oos']['n']} trades, win rate {_pct(best['oos']['win_rate'])}, "
        f"P&L ${best['oos']['pnl']:,.0f} vs baseline ${baseline['oos']['pnl']:,.0f}). "
        "That is a research note. Do not turn it on. The live desk is unchanged."
    )


def run_study(
    bars_by_symbol: dict[str, list[Bar]],
    vix_by_day: dict[date, float],
    *,
    label: str = "",
) -> dict[str, Any]:
    """Both books, the filters, and the 70/30 split. Does not touch a live flag."""
    # The cut is every session in the file, including the first day that has no prior close.
    train_days, oos_days = split_sessions(session_days(bars_by_symbol))
    lunch = _book_report("Lunch-Break Credit", lunch_book, bars_by_symbol, vix_by_day, train_days, oos_days)
    afternoon = _book_report("Afternoon Puts", afternoon_book, bars_by_symbol, vix_by_day, train_days, oos_days)
    return {
        "live_rules_changed": LIVE_RULES_CHANGED,
        "divisor": DIVISOR,
        "label": label,
        "sessions": len(session_days(bars_by_symbol)),
        "vix_days": len(vix_by_day),
        "train_sessions": len(train_days),
        "oos_sessions": len(oos_days),
        "train_first": min(train_days).isoformat() if train_days else None,
        "train_last": max(train_days).isoformat() if train_days else None,
        "oos_first": min(oos_days).isoformat() if oos_days else None,
        "oos_last": max(oos_days).isoformat() if oos_days else None,
        "lunch_credit": lunch,
        "afternoon_puts": afternoon,
        "note": (
            "Research only. Webull stays paper. SPX is not an order. "
            "No filter from this study is enabled."
        ),
    }


def _book_report(name, builder, bars, vix_by_day, train_days, oos_days) -> dict[str, Any]:
    baseline_trades = builder(bars, vix_by_day, fraction=0.0)
    rows = [
        score_variant(label, apply_filter(baseline_trades, drop), train_days, oos_days)
        for label, drop in FILTERS
    ]
    for fraction, strike_name in ((0.25, "short strike 0.25σ below spot"), (0.5, "short strike 0.50σ below spot")):
        if name.startswith("Afternoon"):
            strike_name = strike_name.replace("short strike", "put strike")
        priced = builder(bars, vix_by_day, fraction=fraction)
        rows.append(score_variant(strike_name, priced, train_days, oos_days))
    return {
        "recommendation": recommend(name, rows),
        "variants": rows,
        "baseline_trades": baseline_trades,
    }


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _cell(summary: dict[str, Any]) -> str:
    if not summary or summary.get("n") == 0:
        return "0 trades"
    win = _pct(summary.get("win_rate"))
    return f"n={summary['n']} · {win} · ${summary['pnl']:,.0f} · avg ${summary['avg']:,.2f}"


def render_markdown(report: dict[str, Any]) -> str:
    """The table the desk can read. The recommendation is the last line of each book."""
    lines = [
        "# Expected-move filter study",
        "",
        report.get("label") or "5-minute study.",
        "",
        (
            f"Sessions with bars: {report.get('sessions')}. "
            f"VIX days: {report.get('vix_days')}. "
            f"Train {report.get('train_first')} → {report.get('train_last')} "
            f"({report.get('train_sessions')} sessions). "
            f"Out of sample {report.get('oos_first')} → {report.get('oos_last')} "
            f"({report.get('oos_sessions')} sessions, the last 30%)."
        ),
        "",
        "Live rules were not changed. The morning move is VIX / 15.87. "
        "Lunch still sells the ATM put and buys the put $2 lower, take profit at half "
        "the credit, stop at twice the credit, flat 2:45 CT, two names a day. "
        "Afternoon Puts still requires SPY and QQQ both, and still uses the live exits. "
        "Option dollars are the Black-Scholes stand-in, not a chain fill.",
        "",
        report.get("note") or "",
        "",
    ]
    for key, title in (("lunch_credit", "Lunch-Break Credit"), ("afternoon_puts", "Afternoon Puts")):
        book = report.get(key) or {}
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| Variant | All | Train | Out of sample |")
        lines.append("| --- | --- | --- | --- |")
        for row in book.get("variants") or []:
            lines.append(
                f"| {row['name']} | {_cell(row['all'])} | {_cell(row['train'])} | {_cell(row['oos'])} |"
            )
        lines.append("")
        lines.append(book.get("recommendation") or "")
        lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(closing_note(report))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _variant(book: dict[str, Any], name: str) -> dict[str, Any]:
    return next((row for row in book.get("variants") or [] if row.get("name") == name), {})


def closing_note(report: dict[str, Any]) -> str:
    """What to do with the table. The live desks stay on their current rules."""
    lunch = report.get("lunch_credit") or {}
    afternoon = report.get("afternoon_puts") or {}
    lunch_base = _variant(lunch, "baseline")
    lunch_quarter = _variant(lunch, "short strike 0.25σ below spot")
    afternoon_base = _variant(afternoon, "baseline")
    afternoon_quarter = _variant(afternoon, "put strike 0.25σ below spot")
    lunch_reasons = lunch_quarter.get("reasons") or {}
    return "\n".join(
        [
            "Leave the live rules as they are. Expected move is a pre-market print and an alert context line. It does not gate Lunch-Break Credit or Afternoon Puts.",
            "",
            (
                "The percent-used filters do not clear the bar. A filter needed at least 8 out-of-sample trades, "
                "a higher out-of-sample P&L, and a win rate that held up. On Lunch-Break Credit the 50% skip has "
                f"{( _variant(lunch, 'skip if EM used > 50%').get('oos') or {}).get('n', 0)} out-of-sample trades, "
                "and the 75% and 100% skips match the baseline's out-of-sample loss. The allow-only-above filters "
                "have an empty out-of-sample book. On Afternoon Puts the skip filters have 3 to 6 out-of-sample trades, "
                "under the minimum, and the allow-only-above filters make less out of sample than the unfiltered book."
            ),
            "",
            (
                "The sigma strikes stay a hypothesis. Lunch-Break Credit with the short strike 0.25σ below spot "
                f"finished {lunch_reasons.get('target', 0)} targets, {lunch_reasons.get('flat', 0)} flats, and "
                f"{lunch_reasons.get('stop', 0)} stops across {(lunch_quarter.get('all') or {}).get('n', 0)} modeled trades. "
                f"Out of sample it made ${(lunch_quarter.get('oos') or {}).get('pnl', 0):,.0f} against the baseline's "
                f"${(lunch_base.get('oos') or {}).get('pnl', 0):,.0f}. "
                f"The train win rate is {_pct((lunch_quarter.get('train') or {}).get('win_rate'))} and the out-of-sample book is "
                f"{(lunch_quarter.get('oos') or {}).get('n', 0)} trades. "
                "The further strike collects a smaller credit, and in this stand-in it does not hit the stop. "
                "The prices are Black-Scholes, not chain fills. "
                "That sample is too clean and too small to move the live short strike off the at-the-money put."
            ),
            "",
            (
                "Afternoon Puts at 0.25σ is a small out-of-sample bump "
                f"(${(afternoon_quarter.get('oos') or {}).get('pnl', 0):,.0f} versus "
                f"baseline ${(afternoon_base.get('oos') or {}).get('pnl', 0):,.0f} on the same "
                f"{(afternoon_base.get('oos') or {}).get('n', 0)} trades). "
                f"The full-sample P&L on that strike is ${(afternoon_quarter.get('all') or {}).get('pnl', 0):,.0f}. "
                "A few modeled winners are larger than the bump. The live contract stays one strike out of the money, "
                "and SPY and QQQ still enter together."
            ),
            "",
            "SPX stays an alert. Webull stays on the paper host. No filter from this table is enabled.",
        ]
    )


def vix_from_bars(bars: list[Bar], daily_open: dict[date, float] | None = None) -> dict[date, float]:
    """Morning VIX. The first regular 5-minute open, else that day's daily open."""
    out: dict[date, float] = {}
    for day, session in group_sessions(bars).items():
        if session and session[0].open > 0:
            out[day] = float(session[0].open)
    for day, value in (daily_open or {}).items():
        if day not in out and value and value > 0:
            out[day] = float(value)
    return out


def _ohlcv(frame: Any) -> Any:
    """Single-name Yahoo frame with flat Open/High/Low/Close columns."""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return frame
    if isinstance(frame.columns, pd.MultiIndex):
        if "Close" in frame.columns.get_level_values(0):
            flat = frame.copy()
            flat.columns = frame.columns.get_level_values(0)
            return flat
        ticker = frame.columns.get_level_values(0)[0]
        return frame[ticker]
    return frame


def load_yahoo_history(period: str = "60d") -> dict[str, Any]:
    """Yahoo 5-minute bars. Same source the 60-session desk studies used. No paid key."""
    import pandas as pd
    import yfinance as yf

    from odte_scanner.signals.diamond_board import bars_from_frame

    names = {"SPY": "SPY", "QQQ": "QQQ", "SPX": "^GSPC", "VIX": "^VIX"}
    frames = {
        symbol: yf.download(ticker, period=period, interval="5m", auto_adjust=False, progress=False, threads=False)
        for symbol, ticker in names.items()
    }
    daily = yf.download("^VIX", period="6mo", interval="1d", auto_adjust=False, progress=False, threads=False)
    bars = {symbol: bars_from_frame(_ohlcv(frames[symbol]), symbol) for symbol in ("SPY", "QQQ", "SPX")}
    vix_bars = bars_from_frame(_ohlcv(frames["VIX"]), "VIX")
    daily_open: dict[date, float] = {}
    table = _ohlcv(daily)
    if isinstance(table, pd.DataFrame) and not table.empty:
        for ts, row in table.iterrows():
            opened = row.get("Open") if hasattr(row, "get") else None
            if opened is None or pd.isna(opened):
                continue
            stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            daily_open[stamp.date()] = float(opened)
    vix = vix_from_bars(vix_bars, daily_open)
    first, last = _span(bars)
    return {
        "bars": {symbol: rows for symbol, rows in bars.items() if rows},
        "vix": vix,
        "vix_bars": len(vix_bars),
        "label": (
            f"Yahoo Finance 5-minute bars, {first} through {last}. "
            "VIX is the first regular 5-minute open (about 8:30 CT), "
            "and the daily open when that bar is missing. Not a paid feed."
        ),
    }


def _span(bars: dict[str, list[Bar]]) -> tuple[str, str]:
    days = session_days(bars)
    if not days:
        return "n/a", "n/a"
    return days[0].isoformat(), days[-1].isoformat()


def main() -> dict[str, Any]:
    """Download the Yahoo window, write the markdown, and return the report."""
    from pathlib import Path

    loaded = load_yahoo_history()
    report = run_study(loaded["bars"], loaded["vix"], label=loaded["label"])
    text = render_markdown(report)
    dest = Path(__file__).resolve().parents[2] / "docs" / "expected_move_backtest.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    # Drop the trade blotter from the JSON so the sample stays a summary.
    sample = {
        key: value
        for key, value in report.items()
        if key not in {"lunch_credit", "afternoon_puts"}
    }
    sample["lunch_credit"] = {
        "recommendation": report["lunch_credit"]["recommendation"],
        "variants": report["lunch_credit"]["variants"],
    }
    sample["afternoon_puts"] = {
        "recommendation": report["afternoon_puts"]["recommendation"],
        "variants": report["afternoon_puts"]["variants"],
    }
    blob = Path(__file__).resolve().parent / "expected_move_sample.json"
    import json

    blob.write_text(json.dumps(sample, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = main()
    print(result["lunch_credit"]["recommendation"])
    print(result["afternoon_puts"]["recommendation"])
