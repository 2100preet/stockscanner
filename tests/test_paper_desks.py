"""Kotegawa and Million Challenge: separate paper books, rules, and routing."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.config import load_config
from odte_scanner.desks.fills import buy_fill, sell_fill
from odte_scanner.desks.kotegawa import run_kotegawa
from odte_scanner.desks.million import TARGET_DOUBLINGS, milestone_levels, run_million
from odte_scanner.desks.routing import (
    FORBIDDEN_ACCOUNT_ENV,
    SANDBOX_HOST,
    RoutingError,
    resolve_route,
    submit_sandbox_order,
)
from odte_scanner.desks.service import scorecard_rows
from odte_scanner.desks.signals import (
    build_signals,
    oversold_bounce_signals,
    select_bounce_call,
)
from odte_scanner.desks.sp100 import in_sp100, oversold_universe, sp100_universe
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

ENTRY = datetime(2026, 11, 13, 14, 50, tzinfo=CT)  # Friday, cash-close window
MONDAY = datetime(2026, 11, 16, 10, 0, tzinfo=CT)
TUESDAY_OPEN = datetime(2026, 11, 17, 10, 0, tzinfo=CT)
TUESDAY_BEFORE_CLOSE = datetime(2026, 11, 17, 14, 59, tzinfo=CT)
TUESDAY_CLOSE = datetime(2026, 11, 17, 15, 0, tzinfo=CT)


def _names(n: int) -> list[str]:
    return list(sp100_universe())[:n]


def _dropped(close: float, *, day: str = "2026-11-13", n: int = 30) -> list[dict]:
    bars = []
    for i in range(n - 1):
        bars.append({"date": f"2026-10-{i+1:02d}" if i < 28 else day, "open": 100, "high": 100, "low": 100, "close": 100})
    bars.append({"date": day, "open": close, "high": close, "low": close, "close": close})
    return bars


def _book(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _kot_section(path: Path, **extra) -> dict:
    cfg = {
        "enabled": True,
        "paper_only": True,
        "starting_equity": 100_000,
        "state_path": str(path),
    }
    cfg.update(extra)
    return cfg


def _mil_section(path: Path, **extra) -> dict:
    cfg = {
        "enabled": True,
        "paper_only": True,
        "starting_equity": 1_000,
        "state_path": str(path),
        "signals": ["challenge_quality", "oversold_bounce"],
        "oversold_bounce": {"target_pct": 50, "stop_pct": 40, "flatten_ct": "14:55"},
    }
    cfg.update(extra)
    return cfg


def _quality(symbol: str = "NVDA", **extra) -> dict:
    row = {
        "symbol": symbol,
        "action": "ENTRY",
        "certainty_tier": "perfect",
        "hist_win_pct": 100,
        "hist_samples": 5,
        "right": "C",
        "contract": f"{symbol}261118C00100000",
        "expiry": "2026-11-18",
        "dte": 2,
        "strike": 100,
        "bid": 1.9,
        "ask": 2.0,
        "spot": 100,
    }
    row.update(extra)
    return row


def _setup_daily(*, close: float = 102, high: float = 103, low: float = 100, n: int = 40) -> list[dict]:
    """Completed sessions ending Friday 2026-11-13. The last bar is the setup."""
    bars = []
    for i in range(n - 1):
        bars.append(
            {
                "date": f"2026-09-{(i % 28) + 1:02d}",
                "open": 100,
                "high": 101,
                "low": 100,
                "close": 100.5,
            }
        )
    bars.append({"date": "2026-11-13", "open": close, "high": high, "low": low, "close": close})
    return bars


def _minute(hh: int, mm: int, high: float, *, close: float | None = None, interval: str = "1m", closed: bool = True) -> dict:
    return {
        "ts": datetime(2026, 11, 16, hh, mm, tzinfo=CT),
        "high": high,
        "low": high - 0.4,
        "close": high if close is None else close,
        "interval": interval,
        "closed": closed,
    }


def _bounce_chain(symbol: str = "QQQ", spot: float = 102) -> list[dict]:
    return [
        {"symbol": symbol, "right": "P", "strike": spot, "dte": 1, "expiry": "2026-11-17", "bid": 1, "ask": 1.1, "contract": "PUT"},
        {"symbol": symbol, "right": "C", "strike": spot, "dte": 0, "expiry": "2026-11-16", "bid": 1.2, "ask": 1.3, "contract": "DTE0"},
        {"symbol": symbol, "right": "C", "strike": spot + 8, "dte": 1, "expiry": "2026-11-17", "bid": 0.4, "ask": 0.5, "contract": "FAR"},
        {"symbol": symbol, "right": "C", "strike": spot, "dte": 5, "expiry": "2026-11-23", "bid": 2, "ask": 2.1, "contract": "WEEK"},
        {"symbol": symbol, "right": "C", "strike": spot, "dte": 1, "expiry": "2026-11-17", "bid": 1.4, "ask": 1.5, "contract": "DTE1"},
    ]


class _GuardEnv(dict):
    def get(self, key, default=None):
        if key in FORBIDDEN_ACCOUNT_ENV:
            raise AssertionError(f"routing read {key}")
        return super().get(key, default)


def test_sp100_universe_is_current_and_excludes_index_etfs():
    names = sp100_universe()
    assert len(names) == len(set(names)) == 101
    assert "AAPL" in names and "NVDA" in names and "BRK-B" in names
    assert "SPY" not in names and "QQQ" not in names
    assert oversold_universe() == ("SPY", "QQQ")
    assert not in_sp100("SPY")


def test_fills_use_mid_or_ask_and_bid_plus_slippage():
    assert buy_fill(bid=1.9, ask=2.0) == pytest.approx(1.95 * 1.0005)
    assert buy_fill(ask=2.0) == pytest.approx(2.0 * 1.0005)
    assert sell_fill(bid=1.9, ask=2.0) == pytest.approx(1.9 * 0.9995)
    assert sell_fill(last=10) == pytest.approx(10 * 0.9995)


def test_config_enables_both_desks_without_touching_existing_ones():
    cfg = load_config()
    assert cfg["kotegawa"]["enabled"] is True
    assert cfg["kotegawa"]["paper_only"] is True
    assert cfg["kotegawa"]["starting_equity"] == 100000
    assert cfg["kotegawa"]["state_path"] == "outputs/kotegawa_state.json"
    assert cfg["million_challenge"]["enabled"] is True
    assert cfg["million_challenge"]["paper_only"] is True
    assert cfg["million_challenge"]["starting_equity"] == 1000
    assert cfg["million_challenge"]["signals"] == ["challenge_quality", "oversold_bounce"]
    bounce = cfg["million_challenge"]["oversold_bounce"]
    assert bounce["target_pct"] == 50 and bounce["stop_pct"] == 40
    assert bounce["flatten_ct"] == "14:55"
    assert cfg["million_challenge"]["state_path"] != cfg["kotegawa"]["state_path"]
    assert cfg["live_trading"]["challenge_paper"] is False
    assert cfg["live_trading"]["second_desk"] == "challenge"
    challenge_exit = cfg["live_trading"]["exits"]["challenge"]
    assert challenge_exit["take_profit_pct"] == 100
    assert challenge_exit["stop_loss_pct"] == 50
    assert "challenge_ledger" not in cfg["kotegawa"]["state_path"]
    assert "webull_orders" not in cfg["million_challenge"]["state_path"]


def test_kotegawa_buys_the_deepest_sp100_names_near_the_close(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    symbols = _names(6)
    bars = {sym: _dropped(80 + i * 2 if i < 5 else 89) for i, sym in enumerate(symbols)}
    bars["SPY"] = _dropped(70)
    view = run_kotegawa(_kot_section(path), bars=bars, now=ENTRY, path=path)
    opened = [row["symbol"] for row in view["open"]]
    assert opened == symbols[:5]
    assert "SPY" not in opened
    assert symbols[5] not in opened
    book = _book(path)
    assert book["desk"] == "kotegawa"
    first = book["trades"][0]
    assert first["shares"] == int((100_000 * 0.20) // first["entry"])
    assert first["entry"] == pytest.approx(80 * 1.0005)
    assert view["route"] == "internal"
    assert "[KOTEGAWA]" in view["cycle_alerts"][0]["message"]
    assert "account_id" not in view


def test_kotegawa_skips_a_name_that_is_not_ten_percent_under_the_sma(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym = _names(1)[0]
    view = run_kotegawa(_kot_section(path), bars={sym: _dropped(90)}, now=ENTRY, path=path)
    assert view["open_positions"] == 0
    assert view["trades"] == 0


def test_kotegawa_entry_window_is_about_1450_ct(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym = _names(1)[0]
    bars = {sym: _dropped(80)}
    section = _kot_section(path)
    too_early = run_kotegawa(section, bars=bars, now=datetime(2026, 11, 13, 14, 49, tzinfo=CT), path=path)
    assert too_early["open_positions"] == 0
    opened = run_kotegawa(section, bars=bars, now=ENTRY, path=path)
    assert opened["open_positions"] == 1
    later = tmp_path / "later.json"
    after = run_kotegawa(
        _kot_section(later),
        bars=bars,
        now=datetime(2026, 11, 13, 15, 1, tzinfo=CT),
        path=later,
    )
    assert after["open_positions"] == 0


def test_kotegawa_has_no_tight_stop_and_uses_five_and_fifteen(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym = _names(1)[0]
    section = _kot_section(path)
    opened = run_kotegawa(section, bars={sym: _dropped(80)}, now=ENTRY, path=path)
    entry = opened["open"][0]["entry"]
    for pct in (0.98, 0.97):
        still = run_kotegawa(
            section,
            quotes={sym: {"last": entry * pct, "bid": entry * pct}},
            now=datetime(2026, 11, 13, 14, 40, tzinfo=CT),
            path=path,
        )
        assert still["open_positions"] == 1
        assert still["trades"] == 0
    stopped = run_kotegawa(
        section,
        quotes={sym: {"last": entry * 0.85, "bid": entry * 0.85}},
        now=datetime(2026, 11, 16, 11, 0, tzinfo=CT),
        path=path,
    )
    assert stopped["trades"] == 1
    assert _book(path)["trades"][0]["exit_reason"] == "disaster_stop"
    assert _book(path)["trades"][0]["exit"] == pytest.approx(entry * 0.85 * 0.9995)


def test_kotegawa_take_profit_is_five_percent_intraday(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym = _names(1)[0]
    section = _kot_section(path)
    opened = run_kotegawa(section, bars={sym: _dropped(80)}, now=ENTRY, path=path)
    entry = opened["open"][0]["entry"]
    done = run_kotegawa(
        section,
        quotes={sym: {"last": entry * 1.05, "bid": entry * 1.05}},
        now=datetime(2026, 11, 13, 14, 40, tzinfo=CT),
        path=path,
    )
    assert done["open_positions"] == 0
    trade = _book(path)["trades"][0]
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit"] == pytest.approx(entry * 1.05 * 0.9995)
    assert "[KOTEGAWA]" in done["cycle_alerts"][0]["message"]


def test_kotegawa_time_exit_is_the_close_of_trading_day_3(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym = _names(1)[0]
    section = _kot_section(path)
    opened = run_kotegawa(section, bars={sym: _dropped(80)}, now=ENTRY, path=path)
    entry = opened["open"][0]["entry"]
    quote = {sym: {"last": entry, "bid": entry}}
    monday = run_kotegawa(section, quotes=quote, now=datetime(2026, 11, 16, 15, 0, tzinfo=CT), path=path)
    assert monday["open_positions"] == 1
    before = run_kotegawa(section, quotes=quote, now=TUESDAY_BEFORE_CLOSE, path=path)
    assert before["open_positions"] == 1
    closed = run_kotegawa(section, quotes=quote, now=TUESDAY_CLOSE, path=path)
    assert closed["open_positions"] == 0
    assert _book(path)["trades"][0]["exit_reason"] == "time_exit"


def test_kotegawa_daily_loss_halts_entries_and_still_exits(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    sym, other = _names(2)
    seeded = {
        "desk": "kotegawa",
        "paper_only": True,
        "starting_equity": 100_000,
        "cash": 80_000,
        "trades": [
            {
                "id": "kot-old",
                "symbol": sym,
                "shares": 100,
                "entry": 50,
                "cost": 5000,
                "status": "open",
                "entry_session": "2026-11-13",
            },
            {
                "id": "kot-loss",
                "symbol": "CLOSED",
                "shares": 1,
                "entry": 1,
                "cost": 1,
                "status": "closed",
                "exit_session": "2026-11-13",
                "pnl": -4000,
            },
        ],
        "recent_alerts": [],
        "sandbox_orders": [],
    }
    path.write_text(json.dumps(seeded), encoding="utf-8")
    view = run_kotegawa(
        _kot_section(path),
        bars={other: _dropped(80), sym: _dropped(80)},
        quotes={sym: {"last": 52.5, "bid": 52.5}},
        now=ENTRY,
        path=path,
    )
    book = _book(path)
    closed = [row for row in book["trades"] if row["id"] == "kot-old"][0]
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "take_profit"
    assert view["entries_halted"] is True
    assert all(row.get("status") != "open" for row in book["trades"])
    assert not any(row["symbol"] == other and row["status"] == "open" for row in book["trades"])


def test_kotegawa_refuses_another_desks_state_file(tmp_path: Path):
    path = tmp_path / "shared.json"
    original = '{"desk": "million_challenge", "cash": 5, "trades": []}'
    path.write_text(original, encoding="utf-8")
    view = run_kotegawa(_kot_section(path), bars={_names(1)[0]: _dropped(80)}, now=ENTRY, path=path)
    assert view["status"] == "blocked"
    assert path.read_text(encoding="utf-8") == original


def test_million_challenge_quality_entry_exit_and_size(tmp_path: Path):
    path = tmp_path / "million_state.json"
    section = _mil_section(path, signals=["challenge_quality"])
    board = {
        "entry": [
            _quality("NVDA"),
            _quality("SPY", contract="SPY0"),
            _quality("AAPL", certainty_tier="weak", hist_win_pct=50, hist_samples=2, contract="AAPL0"),
        ]
    }
    view = run_million(section, challenge=board, now=MONDAY, path=path)
    assert view["open_positions"] == 1
    assert view["open"][0]["symbol"] == "NVDA"
    assert view["open"][0]["contracts"] == 1
    trade = _book(path)["trades"][0]
    fill = ((1.9 + 2.0) / 2) * 1.0005
    assert trade["entry"] == pytest.approx(fill)
    assert trade["source"] == "challenge_quality"
    assert "[MILLION]" in view["cycle_alerts"][0]["message"]

    held = run_million(
        section,
        quotes={"NVDA": {"last": fill * 1.19, "bid": fill * 1.19}},
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    assert held["open_positions"] == 1

    stopped = run_million(
        section,
        quotes={"NVDA": {"last": fill * 0.80, "bid": fill * 0.80}},
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    assert stopped["open_positions"] == 0
    assert _book(path)["trades"][0]["exit_reason"] == "hard_stop"


def test_million_quality_target_is_25_not_40_stop_and_flattens_0dte(tmp_path: Path):
    path = tmp_path / "million_state.json"
    section = _mil_section(path, signals=["challenge_quality"])
    opened = run_million(
        section,
        challenge={"entry": [_quality("AMD", expiry="2026-11-16", dte=0, contract="AMD0")]},
        now=MONDAY,
        path=path,
    )
    fill = opened["open"][0]["entry"]
    winner = run_million(
        section,
        quotes={"AMD": {"last": fill * 1.25, "bid": fill * 1.25}},
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    assert _book(path)["trades"][0]["exit_reason"] == "take_profit"
    assert winner["trades"] == 1

    path2 = tmp_path / "flat.json"
    section2 = _mil_section(path2, signals=["challenge_quality"])
    run_million(
        section2,
        challenge={"entry": [_quality("MSFT", expiry="2026-11-16", dte=0, contract="MSFT0")]},
        now=MONDAY,
        path=path2,
    )
    flat = run_million(
        section2,
        quotes={"MSFT": {"last": 1.95, "bid": 1.95}},
        now=datetime(2026, 11, 16, 15, 46, tzinfo=ET),
        path=path2,
        signals=["challenge_quality"],
    )
    assert _book(path2)["trades"][0]["exit_reason"] == "flatten_0dte"
    assert flat["open_positions"] == 0

    path3 = tmp_path / "swing.json"
    section3 = _mil_section(path3, signals=["challenge_quality"])
    run_million(
        section3,
        challenge={"entry": [_quality("COST")]},
        now=MONDAY,
        path=path3,
    )
    held = run_million(
        section3,
        quotes={"COST": {"last": 1.95, "bid": 1.95}},
        now=datetime(2026, 11, 16, 15, 46, tzinfo=ET),
        path=path3,
        signals=["challenge_quality"],
    )
    assert held["open_positions"] == 1


def test_million_sizes_ten_percent_and_skips_an_oversized_contract(tmp_path: Path):
    cheap = tmp_path / "cheap.json"
    run_million(
        _mil_section(cheap, signals=["challenge_quality"]),
        challenge={"entry": [_quality("NFLX", bid=0.40, ask=0.40)]},
        now=MONDAY,
        path=cheap,
    )
    assert _book(cheap)["trades"][0]["contracts"] == 2

    fat = tmp_path / "fat.json"
    view = run_million(
        _mil_section(fat, signals=["challenge_quality"]),
        challenge={"entry": [_quality("ORCL", bid=3.0, ask=3.0)]},
        now=MONDAY,
        path=fat,
    )
    assert view["open_positions"] == 0
    assert view["cash"] == 1000


def test_million_one_position_and_daily_loss_limits(tmp_path: Path):
    path = tmp_path / "one.json"
    section = _mil_section(path, signals=["challenge_quality"])
    view = run_million(
        section,
        challenge={"entry": [_quality("NVDA"), _quality("AAPL", contract="AAPL1")]},
        now=MONDAY,
        path=path,
    )
    assert view["open_positions"] == 1
    assert view["open"][0]["symbol"] == "NVDA"

    losers = tmp_path / "losers.json"
    losers.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 900,
                "day": "2026-11-16",
                "day_start_equity": 1000,
                "trades": [
                    {"id": "a", "symbol": "AAA", "status": "closed", "pnl": -10, "exit_session": "2026-11-16"},
                    {"id": "b", "symbol": "BBB", "status": "closed", "pnl": -12, "exit_session": "2026-11-16"},
                ],
            }
        ),
        encoding="utf-8",
    )
    halted = run_million(
        _mil_section(losers, signals=["challenge_quality"]),
        challenge={"entry": [_quality("NVDA")]},
        now=MONDAY,
        path=losers,
    )
    assert halted["entries_halted"] is True
    assert halted["halt_reason"] == "two losers"
    assert halted["open_positions"] == 0

    loss = tmp_path / "loss.json"
    loss.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 800,
                "day": "2026-11-16",
                "day_start_equity": 1000,
                "trades": [
                    {"id": "c", "symbol": "CCC", "status": "closed", "pnl": -200, "exit_session": "2026-11-16"},
                ],
            }
        ),
        encoding="utf-8",
    )
    blocked = run_million(
        _mil_section(loss, signals=["challenge_quality"]),
        challenge={"entry": [_quality("NVDA")]},
        now=MONDAY,
        path=loss,
    )
    assert blocked["entries_halted"] is True
    assert blocked["open_positions"] == 0


def test_million_milestones_track_doublings(tmp_path: Path):
    assert milestone_levels()[0] == 2000
    assert milestone_levels()[-1] == 1_024_000
    assert len(milestone_levels()) == 10
    assert TARGET_DOUBLINGS == pytest.approx(9.97)
    path = tmp_path / "miles.json"
    path.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 2000,
                "day": "2026-11-16",
                "day_start_equity": 2000,
                "trades": [],
            }
        ),
        encoding="utf-8",
    )
    view = run_million(_mil_section(path, signals=[]), now=MONDAY, path=path)
    assert view["equity"] == 2000
    assert view["doublings"] == pytest.approx(1.0)
    assert view["target_doublings"] == pytest.approx(9.97)
    reached = [row["level"] for row in view["milestones"] if row["reached"]]
    assert reached == [2000]


def test_oversold_break_buys_the_nearest_expiry_beyond_today():
    picked = select_bounce_call(_bounce_chain(), 102, MONDAY)
    assert picked["contract"] == "DTE1"
    assert picked["dte"] == 1
    only_today = select_bounce_call(
        [{"right": "C", "strike": 102, "dte": 0, "expiry": "2026-11-16", "bid": 1, "ask": 1.1, "contract": "DTE0"}],
        102,
        MONDAY,
    )
    assert only_today is None

    bars = {"QQQ": _setup_daily()}
    chains = {"QQQ": _bounce_chain()}
    quiet = {"QQQ": [_minute(10, 0, 102.5)]}
    assert oversold_bounce_signals(bars, chains=chains, intraday=quiet, now=datetime(2026, 11, 16, 10, 1, tzinfo=CT)) == []
    forming = {"QQQ": [_minute(10, 5, 103.4, closed=False)]}
    assert oversold_bounce_signals(bars, chains=chains, intraday=forming, now=datetime(2026, 11, 16, 10, 6, tzinfo=CT)) == []
    daily_only = {"QQQ": [{"ts": datetime(2026, 11, 16, 10, 0, tzinfo=CT), "high": 110, "close": 108, "interval": "1d", "closed": True}]}
    assert oversold_bounce_signals(bars, chains=chains, intraday=daily_only, now=datetime(2026, 11, 16, 10, 5, tzinfo=CT)) == []
    assert oversold_bounce_signals(bars, chains=chains, now=datetime(2026, 11, 16, 10, 5, tzinfo=CT)) == []

    hit = oversold_bounce_signals(
        bars,
        chains=chains,
        intraday={"QQQ": [_minute(10, 0, 102.4), _minute(10, 5, 103.4, interval="5m")]},
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
    )
    assert len(hit) == 1
    assert hit[0]["symbol"] == "QQQ"
    assert hit[0]["contract"] == "DTE1"
    assert hit[0]["dte"] == 1
    assert "near_40d_low" in hit[0]["reason"]
    assert "break_setup_high" in hit[0]["reason"]
    assert oversold_bounce_signals({"NVDA": _setup_daily()}, chains={"NVDA": _bounce_chain("NVDA")}, intraday={"NVDA": [_minute(10, 5, 104)]}, now=MONDAY) == []
    # The setup day's own tape is not an entry. The break belongs to the next session.
    friday = datetime(2026, 11, 13, 14, 0, tzinfo=CT)
    assert oversold_bounce_signals(bars, chains=chains, intraday={"QQQ": [_minute(10, 5, 104)]}, now=friday) == []


def test_oversold_setup_is_the_low_or_the_sma_and_the_mix_keeps_order(tmp_path: Path):
    sma_bars = []
    for _ in range(24):
        sma_bars.append({"date": "2026-10-01", "open": 100, "high": 80, "low": 80, "close": 100})
    sma_bars.append({"date": "2026-11-13", "open": 88, "high": 90, "low": 87, "close": 89})
    chain = _bounce_chain("SPY", spot=89)
    tape = {"SPY": [_minute(9, 35, 90.2, interval="1m")]}
    hit = oversold_bounce_signals({"SPY": sma_bars}, chains={"SPY": chain}, intraday=tape, now=datetime(2026, 11, 16, 9, 36, tzinfo=CT))
    assert len(hit) == 1 and "below_sma25" in hit[0]["reason"]
    assert hit[0]["contract"] == "DTE1"

    path = tmp_path / "mix.json"
    challenge = {"entry": [_quality("AMD")]}
    overs = {"QQQ": _setup_daily()}
    chains = {"QQQ": _bounce_chain()}
    intra = {"QQQ": [_minute(10, 5, 103.5)]}
    first = run_million(
        _mil_section(path),
        challenge=challenge,
        bars=overs,
        chains=chains,
        intraday=intra,
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=path,
        signals=["challenge_quality", "oversold_bounce"],
    )
    assert first["open"][0]["source"] == "challenge_quality"
    assert first["open"][0]["symbol"] == "AMD"

    other = tmp_path / "mix2.json"
    second = run_million(
        _mil_section(other),
        challenge=challenge,
        bars=overs,
        chains=chains,
        intraday=intra,
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=other,
        signals=["oversold_bounce", "challenge_quality"],
    )
    assert second["open"][0]["source"] == "oversold_bounce"
    assert second["open"][0]["symbol"] == "QQQ"
    assert _book(other)["trades"][0]["dte"] == 1
    assert _book(other)["trades"][0]["expiry"] == "2026-11-17"
    unused, ignored = build_signals(["nope"], challenge=challenge, now=MONDAY)
    assert unused == [] and ignored == ["nope"]


def _enter_bounce(path: Path, section: dict):
    return run_million(
        section,
        bars={"QQQ": _setup_daily()},
        chains={"QQQ": _bounce_chain()},
        intraday={"QQQ": [_minute(10, 5, 103.5)]},
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=path,
        signals=["oversold_bounce"],
    )


def test_oversold_exits_are_fifty_forty_and_the_same_session_close(tmp_path: Path):
    path = tmp_path / "giveback.json"
    section = _mil_section(path, signals=["oversold_bounce"])
    opened = _enter_bounce(path, section)
    entry = opened["open"][0]["entry"]
    quote = lambda px: {"QQQ": {"last": px, "bid": px}}
    held = run_million(section, quotes=quote(entry * 1.49), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert held["open_positions"] == 1
    # A giveback is not a trail. The old +80% arm / +50% lock is gone.
    faded = run_million(section, quotes=quote(entry * 1.10), now=datetime(2026, 11, 16, 12, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert faded["open_positions"] == 1
    target = run_million(section, quotes=quote(entry * 1.50), now=datetime(2026, 11, 16, 12, 5, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert target["open_positions"] == 0
    assert _book(path)["trades"][0]["exit_reason"] == "take_profit"

    stop_path = tmp_path / "stop.json"
    stop_section = _mil_section(stop_path, signals=["oversold_bounce"])
    opened = _enter_bounce(stop_path, stop_section)
    entry = opened["open"][0]["entry"]
    shallow = run_million(stop_section, quotes=quote(entry * 0.80), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert shallow["open_positions"] == 1
    almost = run_million(stop_section, quotes=quote(entry * 0.61), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert almost["open_positions"] == 1
    stopped = run_million(stop_section, quotes=quote(entry * 0.60), now=datetime(2026, 11, 16, 11, 6, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert stopped["open_positions"] == 0
    assert _book(stop_path)["trades"][0]["exit_reason"] == "hard_stop"

    clock = tmp_path / "clock.json"
    clock_section = _mil_section(clock, signals=["oversold_bounce"])
    opened = _enter_bounce(clock, clock_section)
    entry = opened["open"][0]["entry"]
    before = run_million(clock_section, quotes=quote(entry), now=datetime(2026, 11, 16, 14, 54, tzinfo=CT), path=clock, signals=["oversold_bounce"])
    assert before["open_positions"] == 1
    flat = run_million(clock_section, quotes=quote(entry), now=datetime(2026, 11, 16, 14, 55, tzinfo=CT), path=clock, signals=["oversold_bounce"])
    assert flat["open_positions"] == 0
    assert _book(clock)["trades"][0]["exit_reason"] == "session_flat"

    late = tmp_path / "late.json"
    late_section = _mil_section(late, signals=["oversold_bounce"])
    _enter_bounce(late, late_section)
    nxt = run_million(late_section, quotes=quote(entry), now=TUESDAY_OPEN, path=late, signals=["oversold_bounce"])
    assert nxt["open_positions"] == 0
    assert _book(late)["trades"][0]["exit_reason"] == "session_flat"


def test_oversold_target_and_stop_stay_configurable(tmp_path: Path):
    path = tmp_path / "custom.json"
    section = _mil_section(path, signals=["oversold_bounce"], oversold_bounce={"target_pct": 30, "stop_pct": 25, "flatten_ct": "14:55"})
    opened = _enter_bounce(path, section)
    entry = opened["open"][0]["entry"]
    quote = lambda px: {"QQQ": {"last": px, "bid": px}}
    held = run_million(section, quotes=quote(entry * 1.29), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert held["open_positions"] == 1
    done = run_million(section, quotes=quote(entry * 1.30), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert _book(path)["trades"][0]["exit_reason"] == "take_profit"
    assert done["open_positions"] == 0

    path2 = tmp_path / "custom_stop.json"
    section2 = _mil_section(path2, signals=["oversold_bounce"], oversold_bounce={"target_pct": 30, "stop_pct": 25})
    opened = _enter_bounce(path2, section2)
    entry = opened["open"][0]["entry"]
    still = run_million(section2, quotes=quote(entry * 0.76), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path2, signals=["oversold_bounce"])
    assert still["open_positions"] == 1
    stopped = run_million(section2, quotes=quote(entry * 0.75), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=path2, signals=["oversold_bounce"])
    assert _book(path2)["trades"][0]["exit_reason"] == "hard_stop"


def test_desks_do_not_share_state_files_or_daily_limits(tmp_path: Path):
    kot = tmp_path / "kotegawa_state.json"
    mil = tmp_path / "million_challenge_state.json"
    challenge = tmp_path / "challenge_ledger.json"
    paper = tmp_path / "paper_ledger.json"
    webull = tmp_path / "webull_orders.json"
    sentinel = '{"cash": 123, "trades": [{"id": "keep"}]}'
    for path in (challenge, paper, webull):
        path.write_text(sentinel, encoding="utf-8")

    sym = _names(1)[0]
    kot_view = run_kotegawa(_kot_section(kot), bars={sym: _dropped(80)}, now=ENTRY, path=kot)
    mil_view = run_million(
        _mil_section(mil, signals=["challenge_quality"]),
        challenge={"entry": [_quality()]},
        now=MONDAY,
        path=mil,
    )
    assert kot_view["open_positions"] == 1
    assert mil_view["open_positions"] == 1
    assert _book(kot)["desk"] == "kotegawa"
    assert _book(mil)["desk"] == "million_challenge"
    assert "NVDA" not in json.dumps(_book(kot))
    assert sym not in {row["symbol"] for row in _book(mil)["trades"]}
    for path in (challenge, paper, webull):
        assert path.read_text(encoding="utf-8") == sentinel

    halted = tmp_path / "halted_million.json"
    halted.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "starting_equity": 1000,
                "cash": 800,
                "day": "2026-11-16",
                "day_start_equity": 1000,
                "trades": [{"id": "x", "symbol": "ZZ", "status": "closed", "pnl": -250, "exit_session": "2026-11-16"}],
            }
        ),
        encoding="utf-8",
    )
    million = run_million(_mil_section(halted, signals=["challenge_quality"]), challenge={"entry": [_quality()]}, now=MONDAY, path=halted)
    assert million["entries_halted"] is True
    still = run_kotegawa(_kot_section(kot), bars={_names(2)[1]: _dropped(80)}, now=ENTRY, path=kot)
    assert still["entries_halted"] is False
    assert still["open_positions"] == 2


def test_missing_account_id_never_routes_to_shared_or_live_accounts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "SHOULD-NOT-USE")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID_2", "SHOULD-NOT-USE-2")
    monkeypatch.setenv("WEBULL_PAPER_ACCOUNT_ID", "SHOULD-NOT-USE-PAPER")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("KOTEGAWA_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("MILLION_ACCOUNT_ID", raising=False)

    env = _GuardEnv()
    for desk in ("kotegawa", "million"):
        route = resolve_route(desk, host=SANDBOX_HOST, environ=env)
        assert route["mode"] == "internal"
        assert route["account_id"] is None
        assert "SHOULD-NOT-USE" not in json.dumps(route)

    def boom(_payload):
        raise AssertionError("transport called")

    kot = tmp_path / "kot.json"
    sym = _names(1)[0]
    view = run_kotegawa(
        _kot_section(kot),
        bars={sym: _dropped(80)},
        now=ENTRY,
        path=kot,
        host=SANDBOX_HOST,
        transport=boom,
    )
    assert view["route"] == "internal"
    saved = kot.read_text(encoding="utf-8")
    assert "SHOULD-NOT-USE" not in saved
    assert _book(kot)["sandbox_orders"] == []

    mil = tmp_path / "mil.json"
    million = run_million(
        _mil_section(mil, signals=["challenge_quality"]),
        challenge={"entry": [_quality()]},
        now=MONDAY,
        path=mil,
        host="api.webull.com",
        transport=boom,
    )
    assert million["route"] == "internal"
    assert "SHOULD-NOT-USE" not in mil.read_text(encoding="utf-8")

    with pytest.raises(RoutingError):
        submit_sandbox_order(
            desk="kotegawa",
            account_id="SHOULD-NOT-USE",
            host="api.webull.com",
            order={"side": "BUY"},
            transport=boom,
        )


def test_dedicated_sandbox_id_routes_and_production_host_does_not(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KOTEGAWA_ACCOUNT_ID", "K-SANDBOX")
    monkeypatch.setenv("MILLION_ACCOUNT_ID", "M-SANDBOX")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "SHOULD-NOT-USE")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID_2", "SHOULD-NOT-USE-2")
    monkeypatch.setenv("WEBULL_LIVE", "1")

    calls = []

    def transport(payload):
        calls.append(payload)
        return {"ok": True}

    kot = tmp_path / "kot.json"
    sym = _names(1)[0]
    view = run_kotegawa(
        _kot_section(kot),
        bars={sym: _dropped(80)},
        now=ENTRY,
        path=kot,
        host=SANDBOX_HOST,
        live_trading={"sandbox": True, "paper": True},
        transport=transport,
    )
    assert view["route"] == "sandbox"
    assert calls and calls[0]["account_id"] == "K-SANDBOX"
    assert calls[0]["host"] == SANDBOX_HOST
    assert "SHOULD-NOT-USE" not in json.dumps(calls)
    assert "account_id" not in view

    calls.clear()
    other = tmp_path / "prod.json"
    blocked = run_kotegawa(
        _kot_section(other),
        bars={sym: _dropped(80)},
        now=ENTRY,
        path=other,
        host="api.webull.com",
        transport=transport,
    )
    assert blocked["route"] == "internal"
    assert calls == []

    route = resolve_route(
        "million",
        live_trading={"sandbox": True, "paper": True},
        environ=_GuardEnv({"MILLION_ACCOUNT_ID": "M-SANDBOX"}),
    )
    assert route["mode"] == "sandbox"
    assert route["account_id"] == "M-SANDBOX"
    assert route["host"] == SANDBOX_HOST
    live_off = resolve_route(
        "million",
        live_trading={"sandbox": False, "paper": False},
        environ={"MILLION_ACCOUNT_ID": "M-SANDBOX", "WEBULL_LIVE": "1"},
    )
    assert live_off["mode"] == "internal"
    assert live_off["account_id"] is None


def test_paper_only_is_enforced(tmp_path: Path):
    path = tmp_path / "off.json"
    view = run_kotegawa(
        {"enabled": True, "paper_only": False, "starting_equity": 100_000},
        bars={_names(1)[0]: _dropped(80)},
        now=ENTRY,
        path=path,
    )
    assert view["status"] == "blocked"
    assert view["reason"] == "paper_only"
    assert not path.exists()


def test_scorecard_rows_and_telegram_labels(tmp_path: Path):
    kot = tmp_path / "kotegawa_state.json"
    mil = tmp_path / "million_challenge_state.json"
    sym = _names(1)[0]
    run_kotegawa(_kot_section(kot), bars={sym: _dropped(80)}, now=ENTRY, path=kot)
    run_million(
        _mil_section(mil, signals=["challenge_quality"]),
        challenge={"entry": [_quality()]},
        now=MONDAY,
        path=mil,
    )
    cfg = {
        "kotegawa": _kot_section(kot),
        "million_challenge": _mil_section(mil),
    }
    rows = scorecard_rows(cfg)
    assert [row["desk"] for row in rows] == ["Kotegawa", "Million"]
    for row in rows:
        assert {"trades", "win_rate", "realized_pnl", "open_positions", "equity"} <= set(row)
    assert rows[0]["equity"] == 100_000
    assert rows[0]["open_positions"] == 1
    assert rows[0]["trades"] == 0
    assert rows[1]["open_positions"] == 1
    assert "paperDeskTable" in PAGE
    assert "Kotegawa and Million Challenge" in PAGE

    kot_book = _book(kot)
    mil_book = _book(mil)
    alerts = collect_trade_alerts(
        {
            "kotegawa": {"alerts": [{"key": "KOTEGAWA|BUY|1", "side": "BUY", "desk": "Kotegawa", "symbol": sym, "message": "[KOTEGAWA] BUY " + sym}]},
            "million_challenge": {"alerts": [{"key": "MILLION|BUY|1", "side": "BUY", "desk": "Million", "symbol": "NVDA", "message": "[MILLION] BUY NVDA"}]},
        }
    )
    messages = [row["message"] for row in alerts]
    assert any(text.startswith("[KOTEGAWA]") for text in messages)
    assert any(text.startswith("[MILLION]") for text in messages)
    assert kot_book["desk"] != mil_book["desk"]
