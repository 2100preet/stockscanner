"""Synthetic minute bars for the Challenge replay. No Tradier archive required."""
from datetime import date, datetime

from odte_scanner.challenge.replay import Bar, Session, replay_books


def _bar(
    day: date,
    hour: int,
    minute: int,
    price: float,
    *,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: int = 200,
) -> Bar:
    return Bar(
        at=datetime(day.year, day.month, day.day, hour, minute),
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price if close is None else close,
        volume=volume,
    )


def _session(day: date, contract: str, symbol: str, bars: list[Bar], dte: int = 0) -> Session:
    return Session(
        contract=contract,
        symbol=symbol,
        expiry=day,
        right="C",
        strike=600,
        day=day,
        bars=bars,
        dte=dte,
    )


def test_sniper_banks_twenty_percent_and_compounds_from_1k():
    day = date(2026, 11, 2)
    bars = [_bar(day, 9, minute, 1.50) for minute in range(30, 55)]
    bars.append(_bar(day, 9, 55, 1.50, high=1.90, close=1.85))
    result = replay_books([_session(day, "SPY261102C00600000", "SPY", bars)])
    sleeve = result["sleeve"]
    assert sleeve["trades"] == 1
    fill = sleeve["fills"][0]
    assert fill["qty"] == 2  # $150 x 2 = $300, the 30% cap of $1,000
    assert fill["reason"] == "bank sniper +20%"
    assert fill["pnl"] == 60.0
    assert sleeve["end_equity"] == 1060.0
    assert sleeve["win_rate_pct"] == 100.0
    assert sleeve["max_drawdown_usd"] == 0.0
    webull = result["webull"]
    assert webull["fills"][0]["qty"] == 1  # 0 DTE stays 1 contract
    assert webull["fills"][0]["reason"] == "flatten last bar"
    assert webull["end_equity"] == 1035.0


def test_three_losses_pause_the_next_session():
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    symbols = ["QQQ", "SPY", "IWM", "QQQ"]
    sessions = []
    for day, symbol in zip(days, symbols):
        bars = [_bar(day, 9, minute, 1.00, low=0.70) for minute in range(30, 40)]
        sessions.append(_session(day, f"{symbol}{day.strftime('%y%m%d')}C00500000", symbol, bars))
    result = replay_books(sessions)
    # Three stops, then the fourth day is paused (resume is 2 days, and quality-only blocks snipers).
    assert result["sleeve"]["trades"] == 3
    assert all(fill["pnl"] < 0 for fill in result["sleeve"]["fills"])
    assert result["sleeve"]["skipped"].get("paused_3_losses", 0) >= 1
    assert result["sleeve"]["end_equity"] < 1000
