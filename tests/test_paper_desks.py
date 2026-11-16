"""Kotegawa and Million Challenge: separate paper books, rules, and routing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
    intraday_bars_from_frame,
    oversold_bounce_signals,
    select_bounce_call,
    select_reclaim_call,
    vwap_reclaim_signals,
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
        "signals": ["vwap_reclaim", "challenge_quality", "oversold_bounce"],
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
    assert cfg["million_challenge"]["signals"] == [
        "vwap_reclaim",
        "challenge_quality",
        "oversold_bounce",
    ]
    vwap = cfg["million_challenge"]["vwap_reclaim"]
    assert vwap["target_pct"] == 50 and vwap["stop_pct"] == 40
    assert vwap["time_stop_minutes"] == 60
    assert vwap["flatten_ct"] == "14:55"
    assert vwap["entry_cutoff_ct"] == "14:30"
    assert vwap["touch_pct"] == 0.005
    twin = cfg["million_challenge"]["vwap_reclaim_1dte"]
    assert twin["log_only"] is True and twin["target_pct"] == 50 and twin["stop_pct"] == 30
    quality = cfg["million_challenge"]["challenge_quality"]
    assert quality["log_only"] is True
    assert cfg["million_challenge"]["ledger_epoch"] == 1
    bounce = cfg["million_challenge"]["oversold_bounce"]
    assert bounce["log_only"] is True
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


def _option_book(px: float, *, contract: str, symbol: str, underlying: float) -> dict:
    """Option premium under the OCC key, stock price under the underlying."""
    return {
        contract: {"bid": px, "ask": px, "last": px},
        symbol: {"last": underlying, "bid": underlying, "ask": underlying},
    }


def _quality_log(path: Path, symbol: str | None = None) -> dict:
    rows = [row for row in _book(path).get("log_only") or [] if row.get("source") == "challenge_quality"]
    if symbol is not None:
        rows = [row for row in rows if row.get("symbol") == symbol]
    assert len(rows) == 1
    return rows[0]


def test_challenge_quality_is_log_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "SHOULD-NOT-USE")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("MILLION_ACCOUNT_ID", raising=False)

    def boom(_payload):
        raise AssertionError("log-only challenge_quality must not send an order")

    path = tmp_path / "million_state.json"
    section = _mil_section(path, signals=["challenge_quality"])
    board = {
        "entry": [
            _quality("NVDA"),
            _quality("SPY", contract="SPY0"),
            _quality("AAPL", certainty_tier="weak", hist_win_pct=50, hist_samples=2, contract="AAPL0"),
        ]
    }
    view = run_million(
        section,
        challenge=board,
        now=MONDAY,
        path=path,
        host="api.webull.com",
        transport=boom,
    )
    assert view["open_positions"] == 0
    assert view["cash"] == 1000
    assert view["trades"] == 0
    assert view["cycle_alerts"] == []
    assert view["entries_halted"] is False
    assert _book(path)["trades"] == []
    assert _book(path)["sandbox_orders"] == []
    logged = _quality_log(path, "NVDA")
    fill = ((1.9 + 2.0) / 2) * 1.0005
    assert logged["status"] == "open"
    assert logged["log_only"] is True
    assert logged["route"] == "log_only"
    assert logged["contracts"] == 1
    assert logged["entry"] == pytest.approx(fill)
    # SPY is an index sniper, and the weak AAPL ticket is not quality. Neither is logged.
    assert all(row.get("symbol") not in {"SPY", "AAPL"} for row in _book(path)["log_only"])

    held = run_million(
        section,
        quotes=_option_book(fill * 1.19, contract=logged["contract"], symbol="NVDA", underlying=180),
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    assert held["cash"] == 1000
    assert held["entries_halted"] is False
    assert _quality_log(path, "NVDA")["status"] == "open"
    assert held["cycle_alerts"] == []

    stopped = run_million(
        section,
        quotes=_option_book(fill * 0.80, contract=logged["contract"], symbol="NVDA", underlying=180),
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    closed = _quality_log(path, "NVDA")
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "hard_stop"
    assert closed["exit"] == pytest.approx(fill * 0.80 * 0.9995)
    assert stopped["cash"] == 1000
    assert stopped["entries_halted"] is False
    assert stopped["trades"] == 0
    assert _book(path)["trades"] == []


def test_challenge_quality_logs_the_would_be_exit_without_cash(tmp_path: Path):
    path = tmp_path / "million_state.json"
    section = _mil_section(path, signals=["challenge_quality"])
    run_million(
        section,
        challenge={"entry": [_quality("AMD", expiry="2026-11-16", dte=0, contract="AMD0")]},
        now=MONDAY,
        path=path,
    )
    logged = _quality_log(path, "AMD")
    fill = logged["entry"]
    winner = run_million(
        section,
        quotes=_option_book(fill * 1.25, contract="AMD0", symbol="AMD", underlying=100),
        now=MONDAY,
        path=path,
        signals=["challenge_quality"],
    )
    assert _quality_log(path, "AMD")["exit_reason"] == "take_profit"
    assert _quality_log(path, "AMD")["exit"] == pytest.approx(fill * 1.25 * 0.9995)
    assert winner["trades"] == 0
    assert winner["cash"] == 1000
    assert winner["open_positions"] == 0

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
        quotes=_option_book(1.95, contract="MSFT0", symbol="MSFT", underlying=400),
        now=datetime(2026, 11, 16, 15, 46, tzinfo=ET),
        path=path2,
        signals=["challenge_quality"],
    )
    assert _quality_log(path2, "MSFT")["exit_reason"] == "flatten_0dte"
    assert _quality_log(path2, "MSFT")["exit"] == pytest.approx(1.95 * 0.9995)
    assert flat["open_positions"] == 0
    assert flat["cash"] == 1000

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
        quotes=_option_book(1.95, contract="COST261118C00100000", symbol="COST", underlying=400),
        now=datetime(2026, 11, 16, 15, 46, tzinfo=ET),
        path=path3,
        signals=["challenge_quality"],
    )
    assert _quality_log(path3, "COST")["status"] == "open"
    assert held["open_positions"] == 0
    assert held["cash"] == 1000


def test_million_sizes_ten_percent_and_skips_an_oversized_contract(tmp_path: Path):
    cheap = tmp_path / "cheap.json"
    run_million(
        _mil_section(cheap, signals=["challenge_quality"]),
        challenge={"entry": [_quality("NFLX", bid=0.40, ask=0.40)]},
        now=MONDAY,
        path=cheap,
    )
    cheap_ticket = [row for row in _book(cheap)["log_only"] if row["source"] == "challenge_quality"][0]
    assert cheap_ticket["contracts"] == 2
    assert cheap_ticket["status"] == "open"
    assert _book(cheap)["trades"] == []
    assert _book(cheap)["cash"] == 1000

    fat = tmp_path / "fat.json"
    view = run_million(
        _mil_section(fat, signals=["challenge_quality"]),
        challenge={"entry": [_quality("ORCL", bid=3.0, ask=3.0)]},
        now=MONDAY,
        path=fat,
    )
    assert view["open_positions"] == 0
    assert view["cash"] == 1000
    assert _book(fat)["trades"] == []
    skipped = [row for row in _book(fat)["log_only"] if row["source"] == "challenge_quality"][0]
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "oversized"


def test_million_one_position_and_daily_loss_limits(tmp_path: Path):
    path = tmp_path / "one.json"
    section = _mil_section(path, signals=["challenge_quality"])
    view = run_million(
        section,
        challenge={"entry": [_quality("NVDA"), _quality("AAPL", contract="AAPL1")]},
        now=MONDAY,
        path=path,
    )
    assert view["open_positions"] == 0
    assert view["cash"] == 1000
    assert _book(path)["trades"] == []
    assert {row["symbol"] for row in _book(path)["log_only"]} == {"NVDA", "AAPL"}

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
    tape, chain = _vwap_context()
    halted = run_million(
        _mil_section(losers, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=losers,
    )
    assert halted["entries_halted"] is True
    assert halted["halt_reason"] == "two losers"
    assert halted["open_positions"] == 0
    assert all(row.get("status") != "open" for row in _book(losers)["trades"])

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
        _mil_section(loss, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=loss,
    )
    assert blocked["entries_halted"] is True
    assert blocked["open_positions"] == 0
    assert all(row.get("status") != "open" for row in _book(loss)["trades"])


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
    assert first["open_positions"] == 0
    assert first["cash"] == 1000
    assert [row["source"] for row in first["log_only"]] == ["challenge_quality", "oversold_bounce"]
    assert first["log_only"][0]["symbol"] == "AMD"
    assert _book(path)["trades"] == []

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
    assert second["open_positions"] == 0
    assert second["cash"] == 1000
    assert [row["source"] for row in second["log_only"]] == ["oversold_bounce", "challenge_quality"]
    logged = [row for row in _book(other)["log_only"] if row["source"] == "oversold_bounce"]
    assert len(logged) == 1
    assert logged[0]["symbol"] == "QQQ"
    assert logged[0]["dte"] == 1
    assert logged[0]["expiry"] == "2026-11-17"
    assert logged[0]["status"] == "open"
    assert all(row.get("source") != "oversold_bounce" for row in _book(other)["trades"])
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


def _bounce_log(path: Path) -> dict:
    rows = [row for row in _book(path).get("log_only") or [] if row.get("source") == "oversold_bounce"]
    assert len(rows) == 1
    return rows[0]


def test_oversold_exits_are_fifty_forty_and_the_same_session_close(tmp_path: Path):
    path = tmp_path / "giveback.json"
    section = _mil_section(path, signals=["oversold_bounce"])
    opened = _enter_bounce(path, section)
    assert opened["open_positions"] == 0
    assert opened["cash"] == 1000
    assert _book(path)["trades"] == []
    entry = _bounce_log(path)["entry"]
    quote = lambda px: _option_book(px, contract="DTE1", symbol="QQQ", underlying=102)
    held = run_million(section, quotes=quote(entry * 1.49), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert held["open_positions"] == 0
    assert _bounce_log(path)["status"] == "open"
    # A giveback is not a trail. The old +80% arm / +50% lock is gone.
    faded = run_million(section, quotes=quote(entry * 1.10), now=datetime(2026, 11, 16, 12, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert _bounce_log(path)["status"] == "open"
    assert faded["cash"] == 1000
    target = run_million(section, quotes=quote(entry * 1.50), now=datetime(2026, 11, 16, 12, 5, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert target["open_positions"] == 0
    assert _bounce_log(path)["exit_reason"] == "take_profit"
    assert _bounce_log(path)["exit"] == pytest.approx(entry * 1.50 * 0.9995)
    assert _book(path)["trades"] == []

    stop_path = tmp_path / "stop.json"
    stop_section = _mil_section(stop_path, signals=["oversold_bounce"])
    _enter_bounce(stop_path, stop_section)
    entry = _bounce_log(stop_path)["entry"]
    shallow = run_million(stop_section, quotes=quote(entry * 0.80), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert _bounce_log(stop_path)["status"] == "open"
    assert shallow["cash"] == 1000
    almost = run_million(stop_section, quotes=quote(entry * 0.61), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert _bounce_log(stop_path)["status"] == "open"
    stopped = run_million(stop_section, quotes=quote(entry * 0.60), now=datetime(2026, 11, 16, 11, 6, tzinfo=CT), path=stop_path, signals=["oversold_bounce"])
    assert stopped["open_positions"] == 0
    assert _bounce_log(stop_path)["exit_reason"] == "hard_stop"
    assert _bounce_log(stop_path)["exit"] == pytest.approx(entry * 0.60 * 0.9995)
    assert stopped["entries_halted"] is False

    clock = tmp_path / "clock.json"
    clock_section = _mil_section(clock, signals=["oversold_bounce"])
    _enter_bounce(clock, clock_section)
    entry = _bounce_log(clock)["entry"]
    before = run_million(clock_section, quotes=quote(entry), now=datetime(2026, 11, 16, 14, 54, tzinfo=CT), path=clock, signals=["oversold_bounce"])
    assert _bounce_log(clock)["status"] == "open"
    assert before["cash"] == 1000
    flat = run_million(clock_section, quotes=quote(entry), now=datetime(2026, 11, 16, 14, 55, tzinfo=CT), path=clock, signals=["oversold_bounce"])
    assert flat["open_positions"] == 0
    assert _bounce_log(clock)["exit_reason"] == "session_flat"

    late = tmp_path / "late.json"
    late_section = _mil_section(late, signals=["oversold_bounce"])
    _enter_bounce(late, late_section)
    entry = _bounce_log(late)["entry"]
    nxt = run_million(late_section, quotes=quote(entry), now=TUESDAY_OPEN, path=late, signals=["oversold_bounce"])
    assert nxt["open_positions"] == 0
    assert _bounce_log(late)["exit_reason"] == "session_flat"
    assert nxt["cash"] == 1000


def test_oversold_target_and_stop_stay_configurable(tmp_path: Path):
    path = tmp_path / "custom.json"
    section = _mil_section(path, signals=["oversold_bounce"], oversold_bounce={"target_pct": 30, "stop_pct": 25, "flatten_ct": "14:55"})
    _enter_bounce(path, section)
    entry = _bounce_log(path)["entry"]
    quote = lambda px: _option_book(px, contract="DTE1", symbol="QQQ", underlying=102)
    held = run_million(section, quotes=quote(entry * 1.29), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert _bounce_log(path)["status"] == "open"
    assert held["open_positions"] == 0
    done = run_million(section, quotes=quote(entry * 1.30), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=path, signals=["oversold_bounce"])
    assert _bounce_log(path)["exit_reason"] == "take_profit"
    assert done["open_positions"] == 0
    assert done["cash"] == 1000

    path2 = tmp_path / "custom_stop.json"
    section2 = _mil_section(path2, signals=["oversold_bounce"], oversold_bounce={"target_pct": 30, "stop_pct": 25})
    _enter_bounce(path2, section2)
    entry = _bounce_log(path2)["entry"]
    still = run_million(section2, quotes=quote(entry * 0.76), now=datetime(2026, 11, 16, 11, 0, tzinfo=CT), path=path2, signals=["oversold_bounce"])
    assert _bounce_log(path2)["status"] == "open"
    stopped = run_million(section2, quotes=quote(entry * 0.75), now=datetime(2026, 11, 16, 11, 5, tzinfo=CT), path=path2, signals=["oversold_bounce"])
    assert _bounce_log(path2)["exit_reason"] == "hard_stop"
    assert _book(path2)["trades"] == []


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
    tape, chain = _vwap_context()
    mil_view = run_million(
        _mil_section(mil, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=mil,
    )
    assert kot_view["open_positions"] == 1
    assert mil_view["open_positions"] == 1
    assert _book(kot)["desk"] == "kotegawa"
    assert _book(mil)["desk"] == "million_challenge"
    assert "SPY" not in json.dumps(_book(kot))
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
    tape, chain = _vwap_context()
    run_million(
        _mil_section(mil, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
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


UTC = timezone.utc


def _vwap_bar(moment: datetime, high: float, low: float, close: float, *, volume: float = 100, interval: str = "1m", closed: bool = True) -> dict:
    return {
        "ts": moment,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "interval": interval,
        "closed": closed,
    }


def _reclaim_tape(touch_high: float = 101.0, *, interval: str = "1m", second: datetime | None = None, closed: bool = True) -> list[dict]:
    """Bar 1 prints under its VWAP. Bar 2 is the reclaim, unless touch_high is short of it."""
    start = datetime(2026, 11, 16, 9, 35, tzinfo=CT)
    nxt = second or datetime(2026, 11, 16, 9, 36, tzinfo=CT)
    return [
        _vwap_bar(start, 100.0, 99.8, 99.9, interval=interval),
        _vwap_bar(nxt, touch_high, 100.2, 100.6, interval=interval, closed=closed),
    ]


def _reclaim_chain() -> list[dict]:
    spot = 100.0
    return [
        {"right": "P", "strike": spot, "dte": 0, "expiry": "2026-11-16", "bid": 1.0, "ask": 1.1, "contract": "PUT"},
        {"right": "C", "strike": spot + 5, "dte": 0, "expiry": "2026-11-16", "bid": 0.40, "ask": 0.50, "contract": "FAR0"},
        {"right": "C", "strike": spot, "dte": 0, "expiry": "2026-11-16", "bid": 1.20, "ask": 1.30, "contract": "DTE0"},
        {"right": "C", "strike": spot, "dte": 1, "expiry": "2026-11-17", "bid": 1.80, "ask": 2.00, "contract": "DTE1"},
        {"right": "C", "strike": spot, "dte": 5, "expiry": "2026-11-23", "bid": 1.50, "ask": 1.60, "contract": "WEEK"},
    ]


def _series_vwap(bars: list[dict]) -> float:
    total = 0.0
    volume = 0.0
    for bar in bars:
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        total += typical * bar["volume"]
        volume += bar["volume"]
    return total / volume


def _vwap_context(tape: list[dict] | None = None, chain: list[dict] | None = None):
    return {"SPY": tape if tape is not None else _reclaim_tape()}, {"SPY": chain if chain is not None else _reclaim_chain()}


def _marks_at_entry(path: Path, *, primary: float = 1.0, twin: float = 1.0) -> dict:
    book = _book(path)
    quotes = {}
    for row in list(book.get("trades") or []) + list(book.get("log_only") or []):
        if row.get("status") != "open" or row.get("entry") in (None, 0):
            continue
        scale = twin if row.get("source") == "vwap_reclaim_1dte" else primary
        px = float(row["entry"]) * scale
        for key in (row.get("contract"), row.get("id")):
            if key:
                quotes[key] = {"last": px, "bid": px, "ask": px}
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in quotes:
            # A stock print in the same map must not become the option mark.
            quotes[symbol] = {"last": 400.0, "bid": 400.0, "ask": 400.0}
    return quotes


def test_vwap_reclaim_triggers_after_price_was_below_session_vwap():
    now = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
    short = _reclaim_tape(100.70)
    assert short[-1]["high"] < _series_vwap(short) * 1.005
    assert vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": short}, now=now) == []

    exact = _reclaim_tape(100.71)
    assert exact[-1]["high"] >= _series_vwap(exact) * 1.005
    hit = vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": exact}, now=now)
    assert len(hit) == 1
    assert hit[0]["source"] == "vwap_reclaim"
    assert hit[0]["symbol"] == "SPY"
    assert hit[0]["contract"] == "DTE0"
    assert hit[0]["dte"] == 0
    assert hit[0]["log_only"] is False
    assert hit[0]["twin"]["source"] == "vwap_reclaim_1dte"
    assert hit[0]["twin"]["contract"] == "DTE1"
    assert hit[0]["twin"]["dte"] == 1
    assert hit[0]["twin"]["log_only"] is True
    assert hit[0]["twin"]["stop_pct"] == 30
    assert hit[0]["stop_pct"] == 40

    never_below = [
        _vwap_bar(datetime(2026, 11, 16, 9, 35, tzinfo=CT), 100, 100, 100),
        _vwap_bar(datetime(2026, 11, 16, 9, 36, tzinfo=CT), 102, 101, 101.5),
    ]
    premarket = [_vwap_bar(datetime(2026, 11, 16, 8, 0, tzinfo=CT), 90, 89, 89.5), *never_below]
    assert vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": never_below}, now=now) == []
    assert vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": premarket}, now=now) == []
    forming = _reclaim_tape(closed=False)
    assert vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": forming}, now=now) == []
    daily = [_vwap_bar(datetime(2026, 11, 16, 9, 36, tzinfo=CT), 110, 90, 105, interval="1d")]
    assert vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": daily}, now=now) == []
    assert vwap_reclaim_signals(chains={"NVDA": _reclaim_chain()}, intraday={"NVDA": _reclaim_tape()}, now=now) == []
    assert vwap_reclaim_signals(chains={"QQQ": _reclaim_chain()}, intraday={"QQQ": _reclaim_tape(interval="5m")}, now=now)[0]["interval"] == "5m"

    # 20:29 UTC is 14:29 CT. Reading the UTC hour as Chicago would reject the bar.
    late_ok = _reclaim_tape(second=datetime(2026, 11, 16, 20, 29, tzinfo=UTC))
    assert vwap_reclaim_signals(
        chains={"SPY": _reclaim_chain()},
        intraday={"SPY": late_ok},
        now=datetime(2026, 11, 16, 14, 29, tzinfo=CT),
    )
    assert vwap_reclaim_signals(
        chains={"SPY": _reclaim_chain()},
        intraday={"SPY": _reclaim_tape()},
        now=datetime(2026, 11, 16, 14, 30, tzinfo=CT),
    ) == []
    assert vwap_reclaim_signals(
        chains={"SPY": _reclaim_chain()},
        intraday={"SPY": _reclaim_tape(second=datetime(2026, 11, 16, 20, 30, tzinfo=UTC))},
        now=datetime(2026, 11, 16, 20, 30, tzinfo=UTC),
    ) == []

    fallback = [row for row in _reclaim_chain() if row["contract"] != "DTE0" and row["contract"] != "FAR0"]
    picked = select_reclaim_call(fallback, 100.6, now, min_dte=0, prefer_dte=0)
    assert picked["contract"] == "DTE1"
    only_today = select_reclaim_call(
        [row for row in _reclaim_chain() if row["dte"] == 0],
        100.6,
        now,
        min_dte=1,
        prefer_dte=1,
    )
    assert only_today is None


def test_vwap_frame_keeps_the_utc_instant():
    import pandas as pd

    index = pd.DatetimeIndex(
        [
            datetime(2026, 11, 16, 15, 35, tzinfo=UTC),
            datetime(2026, 11, 16, 15, 36, tzinfo=UTC),
        ]
    )
    tape = _reclaim_tape()
    frame = pd.DataFrame(
        {
            "Open": [99.9, 100.4],
            "High": [tape[0]["high"], tape[1]["high"]],
            "Low": [tape[0]["low"], tape[1]["low"]],
            "Close": [tape[0]["close"], tape[1]["close"]],
            "Volume": [100, 100],
        },
        index=index,
    )
    now = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
    bars = intraday_bars_from_frame(frame, interval="1m", now=now)
    assert all(bar["closed"] for bar in bars)
    assert bars[0]["ts"].astimezone(CT).hour == 9
    hit = vwap_reclaim_signals(chains={"SPY": _reclaim_chain()}, intraday={"SPY": bars}, now=now)
    assert len(hit) == 1 and hit[0]["contract"] == "DTE0"


def test_vwap_reclaim_is_the_primary_paper_trade(tmp_path: Path):
    path = tmp_path / "vwap.json"
    section = _mil_section(path, signals=["vwap_reclaim", "challenge_quality"])
    tape, chain = _vwap_context()
    view = run_million(
        section,
        challenge={"entry": [_quality("AMD")]},
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=path,
        vix={"level": 18.4, "prior_close": 17.1},
    )
    assert view["open_positions"] == 1
    assert view["open"][0]["symbol"] == "SPY"
    assert view["open"][0]["source"] == "vwap_reclaim"
    trade = _book(path)["trades"][0]
    assert trade["contract"] == "DTE0"
    assert trade["dte"] == 0
    assert trade["contracts"] == 1
    fill = 1.25 * 1.0005
    assert trade["entry"] == pytest.approx(fill)
    assert trade["cost"] == pytest.approx(fill * 100)
    assert _book(path)["cash"] == pytest.approx(1000 - fill * 100)
    assert view["cash"] == round(1000 - fill * 100, 2)
    assert trade["vix"] == pytest.approx(18.4)
    assert trade["vix_vs_prior"] == "up"
    assert trade["time_stop_minutes"] == 60
    assert trade["target_pct"] == 50 and trade["stop_pct"] == 40
    assert view["cycle_alerts"][0]["message"].startswith("[MILLION][VWAP] BUY SPY")
    assert "VIX 18.40 up" in view["cycle_alerts"][0]["message"]
    twin = [row for row in _book(path)["log_only"] if row["source"] == "vwap_reclaim_1dte"]
    assert len(twin) == 1
    assert twin[0]["contract"] == "DTE1"
    assert twin[0]["status"] == "open"
    assert twin[0]["stop_pct"] == 30
    assert twin[0]["route"] == "log_only"
    assert twin[0]["vix_vs_prior"] == "up"
    assert _book(path)["sandbox_orders"] == []
    assert all(row["source"] != "vwap_reclaim_1dte" for row in _book(path)["trades"])
    assert all(row.get("symbol") != "AMD" for row in _book(path)["trades"])
    quality_logs = [row for row in _book(path)["log_only"] if row["source"] == "challenge_quality"]
    assert len(quality_logs) == 1 and quality_logs[0]["symbol"] == "AMD" and quality_logs[0]["status"] == "open"

    # The 1DTE twin stops at -30 while the filled 0DTE is still inside -40.
    faded = run_million(
        section,
        quotes=_marks_at_entry(path, primary=0.70, twin=0.70),
        now=datetime(2026, 11, 16, 9, 50, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert faded["open_positions"] == 1
    assert _book(path)["trades"][0]["status"] == "open"
    assert twin_row(path)["status"] == "closed"
    assert twin_row(path)["exit_reason"] == "hard_stop"
    assert _book(path)["cash"] == pytest.approx(1000 - fill * 100)


def twin_row(path: Path) -> dict:
    return [row for row in _book(path)["log_only"] if row["source"] == "vwap_reclaim_1dte"][0]


def test_vwap_reclaim_time_stop_uses_elapsed_instants(tmp_path: Path):
    path = tmp_path / "time.json"
    section = _mil_section(path, signals=["vwap_reclaim"])
    tape, chain = _vwap_context()
    run_million(
        section,
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 10, 0, tzinfo=CT),
        path=path,
        vix={"last": 16.0, "prev_close": 16.0},
    )
    book = _book(path)
    book["trades"][0]["entry_at"] = "2026-11-16T16:00:00+00:00"
    book["log_only"][0]["entry_at"] = "2026-11-16T16:00:00+00:00"
    path.write_text(json.dumps(book), encoding="utf-8")
    assert _book(path)["trades"][0]["vix_vs_prior"] == "unchanged"

    held = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 10, 59, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert held["open_positions"] == 1
    assert twin_row(path)["status"] == "open"

    utc_held = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 16, 59, tzinfo=UTC),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert utc_held["open_positions"] == 1

    stopped = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 17, 0, tzinfo=UTC),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert stopped["open_positions"] == 0
    assert _book(path)["trades"][0]["exit_reason"] == "time_stop"
    assert twin_row(path)["exit_reason"] == "time_stop"
    again = run_million(
        section,
        chains=chain,
        intraday=tape,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 12, 0, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert again["open_positions"] == 0
    assert len(_book(path)["trades"]) == 1


def test_vwap_reclaim_flattens_at_1455_ct(tmp_path: Path):
    path = tmp_path / "flat.json"
    section = _mil_section(path, signals=["vwap_reclaim"])
    late = _reclaim_tape(second=datetime(2026, 11, 16, 14, 11, tzinfo=CT))
    late[0]["ts"] = datetime(2026, 11, 16, 14, 10, tzinfo=CT)
    run_million(
        section,
        chains={"SPY": _reclaim_chain()},
        intraday={"SPY": late},
        now=datetime(2026, 11, 16, 14, 20, tzinfo=CT),
        path=path,
        vix={"level": 15.0, "prior_close": 16.2},
    )
    assert _book(path)["trades"][0]["vix_vs_prior"] == "down"
    before = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 14, 54, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert before["open_positions"] == 1
    assert twin_row(path)["status"] == "open"

    # 14:55 UTC is 08:55 CT. The flatten is the Chicago instant, not the UTC clock.
    utc_morning = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 14, 55, tzinfo=UTC),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert utc_morning["open_positions"] == 1

    flat = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 20, 55, tzinfo=UTC),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert flat["open_positions"] == 0
    assert _book(path)["trades"][0]["exit_reason"] == "session_flat"
    assert twin_row(path)["status"] == "open"

    twin_done = run_million(
        section,
        quotes=_marks_at_entry(path),
        now=datetime(2026, 11, 16, 15, 20, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    assert twin_done["open_positions"] == 0
    assert twin_row(path)["exit_reason"] == "time_stop"
    assert flat["cash"] == twin_done["cash"]


def test_vwap_reclaim_falls_back_to_the_nearest_expiry_without_0dte(tmp_path: Path):
    path = tmp_path / "nearest.json"
    chain = [row for row in _reclaim_chain() if row["dte"] != 0]
    run_million(
        _mil_section(path, signals=["vwap_reclaim"]),
        chains={"SPY": chain},
        intraday={"SPY": _reclaim_tape()},
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=path,
    )
    trade = _book(path)["trades"][0]
    assert trade["contract"] == "DTE1"
    assert trade["dte"] == 1
    assert twin_row(path)["contract"] == "WEEK"
    assert twin_row(path)["status"] == "open"
    assert twin_row(path)["log_only"] is True


def test_oversold_bounce_is_log_only_and_does_not_fill(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "SHOULD-NOT-USE")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID_2", "SHOULD-NOT-USE-2")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("MILLION_ACCOUNT_ID", raising=False)

    def boom(_payload):
        raise AssertionError("log-only oversold bounce must not send an order")

    path = tmp_path / "shadow.json"
    section = _mil_section(path, signals=["oversold_bounce"])
    view = run_million(
        section,
        bars={"QQQ": _setup_daily()},
        chains={"QQQ": _bounce_chain()},
        intraday={"QQQ": [_minute(10, 5, 103.5)]},
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=path,
        host="api.webull.com",
        transport=boom,
        vix={"level": 19.0, "prior_close": 18.0},
    )
    assert view["open_positions"] == 0
    assert view["cash"] == 1000
    assert view["route"] == "internal"
    assert view["cycle_alerts"] == []
    assert _book(path)["trades"] == []
    assert _book(path)["sandbox_orders"] == []
    logged = _bounce_log(path)
    assert logged["status"] == "open"
    assert logged["route"] == "log_only"
    assert logged["vix"] == pytest.approx(19.0)
    assert logged["vix_vs_prior"] == "up"
    assert "SHOULD-NOT-USE" not in path.read_text(encoding="utf-8")

    lost = run_million(
        section,
        quotes=_option_book(logged["entry"] * 0.60, contract="DTE1", symbol="QQQ", underlying=102),
        now=datetime(2026, 11, 16, 11, 0, tzinfo=CT),
        path=path,
        signals=["oversold_bounce"],
        vix={"level": 17.0, "prior_close": 18.0},
    )
    assert lost["entries_halted"] is False
    assert lost["cash"] == 1000
    assert _bounce_log(path)["exit_reason"] == "hard_stop"
    assert _bounce_log(path)["exit"] == pytest.approx(logged["entry"] * 0.60 * 0.9995)
    assert _bounce_log(path)["pnl"] < 0

    quiet = tmp_path / "no-vix.json"
    run_million(
        _mil_section(quiet, signals=["oversold_bounce"]),
        bars={"QQQ": _setup_daily()},
        chains={"QQQ": _bounce_chain()},
        intraday={"QQQ": [_minute(10, 5, 103.5)]},
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=quiet,
    )
    assert _bounce_log(quiet)["status"] == "open"
    assert _bounce_log(quiet)["vix"] is None
    assert _bounce_log(quiet)["vix_vs_prior"] is None


def test_vwap_reclaim_never_falls_back_to_a_shared_or_live_account(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "SHOULD-NOT-USE")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID_2", "SHOULD-NOT-USE-2")
    monkeypatch.setenv("WEBULL_PAPER_ACCOUNT_ID", "SHOULD-NOT-USE-PAPER")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("MILLION_ACCOUNT_ID", raising=False)
    tape, chain = _vwap_context()

    def boom(_payload):
        raise AssertionError("transport called")

    missing = tmp_path / "missing.json"
    blocked = run_million(
        _mil_section(missing, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=missing,
        host=SANDBOX_HOST,
        live_trading={"sandbox": True, "paper": True},
        environ=_GuardEnv(),
        transport=boom,
    )
    assert blocked["route"] == "internal"
    assert blocked["open_positions"] == 1
    assert _book(missing)["sandbox_orders"] == []
    assert "SHOULD-NOT-USE" not in missing.read_text(encoding="utf-8")
    assert "account_id" not in blocked

    live = tmp_path / "live.json"
    stayed = run_million(
        _mil_section(live, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=live,
        host="api.webull.com",
        environ=_GuardEnv({"MILLION_ACCOUNT_ID": "M-SANDBOX", "WEBULL_LIVE": "1"}),
        transport=boom,
    )
    assert stayed["route"] == "internal"
    assert stayed["open_positions"] == 1
    assert _book(live)["sandbox_orders"] == []

    calls = []

    def transport(payload):
        calls.append(payload)
        return {"ok": True}

    routed = tmp_path / "routed.json"
    sent = run_million(
        _mil_section(routed, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=routed,
        host=SANDBOX_HOST,
        live_trading={"sandbox": True, "paper": True},
        environ=_GuardEnv(
            {
                "MILLION_ACCOUNT_ID": "M-SANDBOX",
                "WEBULL_ACCOUNT_ID": "SHOULD-NOT-USE",
                "WEBULL_ACCOUNT_ID_2": "SHOULD-NOT-USE-2",
                "WEBULL_LIVE": "1",
            }
        ),
        transport=transport,
    )
    assert sent["route"] == "sandbox"
    assert len(calls) == 1
    assert calls[0]["account_id"] == "M-SANDBOX"
    assert calls[0]["host"] == SANDBOX_HOST
    assert calls[0]["order"]["contract"] == "DTE0"
    assert "SHOULD-NOT-USE" not in json.dumps(calls)
    assert _book(routed)["sandbox_orders"] == [
        {"symbol": "SPY", "side": "BUY", "account_id": "M-SANDBOX", "host": SANDBOX_HOST}
    ]
    assert "account_id" not in sent

    calls.clear()
    refused = tmp_path / "refused.json"
    paper = run_million(
        _mil_section(refused, signals=["vwap_reclaim"]),
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=refused,
        host="api.webull.com",
        live_trading={"sandbox": False, "paper": False},
        environ={"MILLION_ACCOUNT_ID": "M-SANDBOX", "WEBULL_LIVE": "1", "WEBULL_ACCOUNT_ID": "SHOULD-NOT-USE"},
        transport=transport,
    )
    assert paper["route"] == "internal"
    assert calls == []
    assert _book(refused)["sandbox_orders"] == []
    with pytest.raises(RoutingError):
        submit_sandbox_order(
            desk="million",
            account_id="SHOULD-NOT-USE",
            host="api.webull.com",
            order={"side": "BUY", "symbol": "SPY"},
            transport=transport,
        )


def _seed_option(
    path: Path,
    *,
    source: str = "challenge_quality",
    entry: float = 0.19,
    contracts: int = 5,
    symbol: str = "TSLA",
    contract: str = "TSLA261116C00390000",
    strike: float = 390,
    expiry: str = "2026-11-16",
    spot: float = 376.92,
    last_mark: float | None = None,
    flatten_ct: str | None = None,
    cash: float | None = None,
) -> None:
    """An already-open option. Entry is the premium, not the stock."""
    cost = entry * 100.0 * contracts
    path.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 1000 - cost if cash is None else cash,
                "day": "2026-11-16",
                "day_start_equity": 1000,
                "ledger_epoch": 1,
                "trades": [
                    {
                        "id": f"mil-{symbol.lower()}",
                        "source": source,
                        "log_only": False,
                        "symbol": symbol,
                        "right": "C",
                        "contract": contract,
                        "expiry": expiry,
                        "strike": strike,
                        "dte": 0,
                        "contracts": contracts,
                        "entry": entry,
                        "last_mark": entry if last_mark is None else last_mark,
                        "spot": spot,
                        "cost": cost,
                        "peak": entry,
                        "status": "open",
                        "entry_at": "2026-11-16T09:11:00-06:00",
                        "entry_session": "2026-11-16",
                        "target_pct": 50 if source == "vwap_reclaim" else None,
                        "stop_pct": 40 if source == "vwap_reclaim" else None,
                        "time_stop_minutes": 60 if source == "vwap_reclaim" else None,
                        "flatten_ct": flatten_ct,
                    }
                ],
                "log_only": [],
            }
        ),
        encoding="utf-8",
    )


def test_million_exit_uses_the_option_quote_not_the_underlying(tmp_path: Path, caplog):
    """The 9:11 CT TSLA print: 5x at $0.19 must not sell at the $376.92 stock."""
    caplog.set_level(logging.WARNING)
    path = tmp_path / "tsla.json"
    _seed_option(path)
    section = _mil_section(path, signals=[])
    stock = {"TSLA": {"last": 376.92, "bid": 376.92, "ask": 376.95}}
    held = run_million(section, quotes=stock, now=datetime(2026, 11, 16, 10, 0, tzinfo=CT), path=path)
    trade = _book(path)["trades"][0]
    assert held["open_positions"] == 1
    assert trade["status"] == "open"
    assert trade.get("exit") is None
    assert held["cash"] == pytest.approx(1000 - 0.19 * 500)
    assert "376.92" not in " ".join(alert["message"] for alert in held["cycle_alerts"])
    assert any("refused" in rec.message and "TSLA" in rec.message for rec in caplog.records)

    quiet = run_million(
        section,
        quotes={**stock, "TSLA261116C00390000": {"bid": 0.22, "ask": 0.24, "last": 0.23}},
        now=datetime(2026, 11, 16, 10, 5, tzinfo=CT),
        path=path,
    )
    assert quiet["open_positions"] == 1
    assert _book(path)["trades"][0]["status"] == "open"

    sold = run_million(
        section,
        quotes={**stock, "TSLA261116C00390000": {"bid": 0.24, "ask": 0.26, "last": 376.92}},
        now=datetime(2026, 11, 16, 10, 6, tzinfo=CT),
        path=path,
    )
    closed = _book(path)["trades"][0]
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "take_profit"
    assert closed["exit"] == pytest.approx(0.24 * 0.9995)
    assert closed["exit"] != pytest.approx(376.92)
    assert _book(path)["cash"] == pytest.approx(1000 - 0.19 * 500 + 0.24 * 0.9995 * 500)
    assert "376.92" not in sold["cycle_alerts"][0]["message"]

    fetched = tmp_path / "fetched.json"
    _seed_option(fetched)
    seen = []

    def fetch(trade):
        seen.append(trade["contract"])
        return {"bid": 0.30, "ask": 0.32, "last": 0.31}

    filled = run_million(
        _mil_section(fetched, signals=[]),
        quotes=stock,
        now=datetime(2026, 11, 16, 10, 0, tzinfo=CT),
        path=fetched,
        quote_fetcher=fetch,
    )
    assert seen == ["TSLA261116C00390000"]
    got = _book(fetched)["trades"][0]
    assert got["exit_reason"] == "take_profit"
    assert got["exit"] == pytest.approx(0.30 * 0.9995)
    assert _book(fetched)["cash"] == pytest.approx(1000 - 0.19 * 500 + 0.30 * 0.9995 * 500)


def test_million_sanity_guard_refuses_impossible_marks(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING)
    path = tmp_path / "bound.json"
    _seed_option(path)
    section = _mil_section(path, signals=[])
    now = datetime(2026, 11, 16, 10, 0, tzinfo=CT)
    refused = run_million(
        section,
        quotes={
            "TSLA": {"last": 376.92, "bid": 376.92},
            "TSLA261116C00390000": {"bid": 3.81, "ask": 3.90, "last": 3.85},
        },
        now=now,
        path=path,
    )
    assert refused["open_positions"] == 1
    assert _book(path)["cash"] == pytest.approx(1000 - 0.19 * 500)
    assert any("20x" in rec.message for rec in caplog.records)

    wide = tmp_path / "wide.json"
    _seed_option(wide, entry=10.0, contracts=1, symbol="QQQ", contract="QQQ0", strike=500, spot=100)
    blocked = run_million(
        _mil_section(wide, signals=[]),
        quotes={"QQQ": {"last": 100.0, "bid": 100.0}, "QQQ0": {"bid": 60.0, "ask": 61.0, "last": 60.0}},
        now=now,
        path=wide,
    )
    assert blocked["open_positions"] == 1
    assert any("half the underlying" in rec.message for rec in caplog.records)

    allowed = run_million(
        _mil_section(wide, signals=[]),
        quotes={"QQQ": {"last": 100.0, "bid": 100.0}, "QQQ0": {"bid": 40.0, "ask": 41.0, "last": 40.0}},
        now=datetime(2026, 11, 16, 10, 5, tzinfo=CT),
        path=wide,
    )
    assert _book(wide)["trades"][0]["exit"] == pytest.approx(40.0 * 0.9995)
    assert allowed["open_positions"] == 0

    poisoned = tmp_path / "poison.json"
    _seed_option(poisoned)

    def stock_as_option(_trade):
        return {"bid": 376.92, "last": 376.92, "ask": 376.95}

    stayed = run_million(
        _mil_section(poisoned, signals=[]),
        quotes={"TSLA": {"last": 376.92, "bid": 376.92}},
        now=now,
        path=poisoned,
        quote_fetcher=stock_as_option,
    )
    assert stayed["open_positions"] == 1
    assert _book(poisoned)["trades"][0].get("exit") is None


def test_million_flatten_without_option_quote_uses_last_mark_or_zero(tmp_path: Path):
    path = tmp_path / "flat.json"
    _seed_option(
        path,
        source="vwap_reclaim",
        entry=1.25,
        contracts=1,
        symbol="SPY",
        contract="SPY0",
        strike=100,
        expiry="2026-11-16",
        spot=500,
        last_mark=1.40,
        flatten_ct="14:55",
    )
    section = _mil_section(path, signals=[])
    flat = run_million(
        section,
        quotes={"SPY": {"last": 500.0, "bid": 500.0, "ask": 500.2}},
        now=datetime(2026, 11, 16, 14, 55, tzinfo=CT),
        path=path,
    )
    closed = _book(path)["trades"][0]
    assert closed["exit_reason"] == "session_flat"
    assert closed["exit"] == pytest.approx(1.40 * 0.9995)
    assert closed["exit"] != pytest.approx(500)
    assert flat["cash"] == pytest.approx(1000 - 1.25 * 100 + 1.40 * 0.9995 * 100)

    worthless = tmp_path / "zero.json"
    _seed_option(
        worthless,
        source="vwap_reclaim",
        entry=0.19,
        contracts=5,
        strike=390,
        expiry="2026-11-16",
        spot=376.92,
        last_mark=0.19,
        flatten_ct="14:55",
    )
    expired = run_million(
        _mil_section(worthless, signals=[]),
        quotes={"TSLA": {"last": 376.92, "bid": 376.92}},
        now=TUESDAY_OPEN,
        path=worthless,
    )
    gone = _book(worthless)["trades"][0]
    assert gone["exit"] == 0
    assert gone["exit_reason"] == "session_flat"
    assert gone["pnl"] == pytest.approx(-0.19 * 500)
    assert expired["cash"] == pytest.approx(1000 - 0.19 * 500)
    assert "376.92" not in expired["cycle_alerts"][0]["message"]

    itm = tmp_path / "itm.json"
    _seed_option(
        itm,
        source="vwap_reclaim",
        entry=1.25,
        contracts=1,
        symbol="SPY",
        contract="SPY0",
        strike=100,
        expiry="2026-11-16",
        spot=376,
        last_mark=1.40,
        flatten_ct="14:55",
    )
    run_million(
        _mil_section(itm, signals=[]),
        quotes={"SPY": {"last": 376.0, "bid": 376.0}},
        now=TUESDAY_OPEN,
        path=itm,
    )
    kept = _book(itm)["trades"][0]
    assert kept["exit"] == pytest.approx(1.40 * 0.9995)
    assert kept["exit"] != pytest.approx(376)


def test_million_ledger_epoch_resets_only_the_million_book(tmp_path: Path):
    mil = tmp_path / "million_challenge_state.json"
    kot = tmp_path / "kotegawa_state.json"
    webull = tmp_path / "webull_orders.json"
    mil.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 189365.68,
                "day": "2026-11-16",
                "day_start_equity": 189365.68,
                "trades": [
                    {
                        "id": "bad",
                        "symbol": "TSLA",
                        "status": "closed",
                        "pnl": 188365.68,
                        "exit": 376.92,
                        "exit_session": "2026-11-16",
                    }
                ],
                "milestones": [{"level": 2000, "reached": True, "reached_at": "2026-11-16T09:20:00-06:00"}],
            }
        ),
        encoding="utf-8",
    )
    kot_body = json.dumps(
        {"desk": "kotegawa", "paper_only": True, "cash": 100000, "starting_equity": 100000, "trades": [{"id": "keep", "status": "open"}]}
    )
    kot.write_text(kot_body, encoding="utf-8")
    webull_body = '{"orders": [{"id": "sandbox-leave-me"}]}'
    webull.write_text(webull_body, encoding="utf-8")

    view = run_million(
        _mil_section(mil, ledger_epoch=1, signals=[]),
        now=MONDAY,
        path=mil,
        host="api.webull.com",
        environ=_GuardEnv(),
    )
    assert view["cash"] == 1000
    assert view["equity"] == 1000
    assert view["doublings"] == 0
    assert view["open_positions"] == 0
    assert view["trades"] == 0
    assert view["ledger_epoch"] == 1
    assert all(not row["reached"] for row in view["milestones"])
    assert _book(mil)["trades"] == []
    archives = list(tmp_path.glob("million_challenge_state.*.bak.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["cash"] == 189365.68
    assert kot.read_text(encoding="utf-8") == kot_body
    assert webull.read_text(encoding="utf-8") == webull_body
    assert not list(tmp_path.glob("kotegawa_state.*.bak.json"))

    again = run_million(_mil_section(mil, ledger_epoch=1, signals=[]), now=MONDAY, path=mil)
    assert again["cash"] == 1000
    assert len(list(tmp_path.glob("million_challenge_state.*.bak.json"))) == 1

    kept = tmp_path / "kept.json"
    kept.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "ledger_epoch": 1,
                "paper_only": True,
                "starting_equity": 1000,
                "cash": 1500,
                "day": "2026-11-16",
                "day_start_equity": 1500,
                "trades": [],
            }
        ),
        encoding="utf-8",
    )
    held = run_million(_mil_section(kept, ledger_epoch=1, signals=[]), now=MONDAY, path=kept)
    assert held["cash"] == 1500
    assert list(tmp_path.glob("kept.*.bak.json")) == []


def test_vwap_reclaim_still_fills(tmp_path: Path):
    path = tmp_path / "vwap.json"
    section = _mil_section(path, signals=["vwap_reclaim", "challenge_quality"])
    tape, chain = _vwap_context()
    opened = run_million(
        section,
        challenge={"entry": [_quality("NVDA")]},
        chains=chain,
        intraday=tape,
        now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
        path=path,
    )
    assert opened["open_positions"] == 1
    assert opened["open"][0]["source"] == "vwap_reclaim"
    assert opened["cash"] < 1000
    trade = _book(path)["trades"][0]
    assert trade["contract"] == "DTE0"
    assert _book(path)["trades"] and all(row["source"] == "vwap_reclaim" for row in _book(path)["trades"])
    quality = [row for row in _book(path)["log_only"] if row["source"] == "challenge_quality"]
    assert len(quality) == 1 and quality[0]["status"] == "open"
    entry = trade["entry"]
    bid = entry * 1.60
    sold = run_million(
        section,
        quotes={
            "DTE0": {"bid": bid, "ask": bid, "last": bid},
            "DTE1": {"bid": entry, "ask": entry, "last": entry},
            "SPY": {"last": 400.0, "bid": 400.0, "ask": 400.2},
        },
        now=datetime(2026, 11, 16, 9, 50, tzinfo=CT),
        path=path,
        signals=["vwap_reclaim"],
    )
    closed = _book(path)["trades"][0]
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "take_profit"
    assert closed["exit"] == pytest.approx(bid * 0.9995)
    assert closed["exit"] != pytest.approx(400)
    assert _book(path)["cash"] == pytest.approx(1000 - entry * 100 + bid * 0.9995 * 100)
    assert sold["cycle_alerts"][0]["message"].startswith("[MILLION][VWAP] SELL SPY")
    assert "400.00" not in sold["cycle_alerts"][0]["message"]
