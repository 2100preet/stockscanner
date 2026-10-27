"""SPX 0DTE levels, regime, structures, paper ledger, and alert text."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.scorecard.service import observe_snapshot
from odte_scanner.signals.spx_strategy import (
    build_spx_desk,
    classify_regime,
    compute_levels,
    replay_proxy,
    select_strategies,
)
from odte_scanner.trading.golden import is_golden_unusual_flow, is_sure_winner_gold

CT = ZoneInfo("America/Chicago")
CORE = datetime(2026, 10, 26, 11, 0, tzinfo=CT)
LATE = datetime(2026, 10, 26, 13, 30, tzinfo=CT)
FLAT = datetime(2026, 10, 26, 14, 50, tzinfo=CT)


def _opt(strike: float, right: str, bid: float, ask: float, oi: int = 100, iv: float = 0.2) -> dict:
    return {
        "strike": strike,
        "right": right,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "oi": oi,
        "iv": iv,
        "gamma": 0.002 if oi >= 1000 else 0.0004,
    }


def _ladder(spot: float = 7677.0) -> list[dict]:
    rows = []
    for strike in range(7580, 7785, 5):
        dist = abs(strike - spot)
        extrinsic = max(0.4, 8.0 - dist * 0.25)
        call = max(0.3, (spot - strike) * 0.45 + extrinsic)
        put = max(0.3, (strike - spot) * 0.45 + extrinsic)
        oi = 8000 if strike in {7670, 7675} else (2500 if dist <= 25 else 150)
        if strike == 7700:
            oi = 4000
        rows.append(_opt(strike, "C", round(call * 0.96, 2), round(call * 1.04, 2), oi, 0.22))
        rows.append(_opt(strike, "P", round(put * 0.96, 2), round(put * 1.04, 2), oi, 0.22))
    return rows


def _levels() -> dict:
    return {
        "anchor_spot": 7660.0,
        "spot": 7677.0,
        "em": 20.0,
        "ut1": 7670.0,
        "ut2": 7680.0,
        "ut3": 7700.0,
        "lt1": 7650.0,
        "lt2": 7640.0,
        "lt3": 7630.0,
        "band_low": 7640.0,
        "band_high": 7680.0,
        "pin": {"strike": 7670.0, "low": 7665.0, "high": 7670.0},
        "expiry": "2026-10-26",
        "day": "2026-10-26",
        "atm_iv": 0.22,
    }


def _priced() -> list[dict]:
    """Known markets around the hand-built levels."""
    rows = []
    for strike, call_bid, call_ask, put_bid, put_ask in (
        (7620, 40, 41, 0.4, 0.6),
        (7630, 32, 33, 0.7, 0.9),
        (7640, 24, 25, 1.1, 1.3),
        (7650, 18, 19, 1.6, 1.8),
        (7660, 12, 12.4, 2.2, 2.5),
        (7670, 8.0, 8.4, 3.2, 3.6),
        (7680, 4.4, 4.8, 6.0, 6.4),
        (7690, 2.6, 2.9, 10, 11),
        (7700, 1.8, 2.0, 16, 17),
        (7710, 0.7, 0.9, 24, 25),
        (7720, 0.35, 0.5, 32, 33),
    ):
        rows.append(_opt(strike, "C", call_bid, call_ask, 500))
        rows.append(_opt(strike, "P", put_bid, put_ask, 500))
    return rows


def test_levels_pin_and_expected_move_band():
    levels = compute_levels(7677, _ladder(), symbol="SPX", now=CORE)
    assert levels["em"] > 0
    assert levels["lt3"] < levels["lt2"] < levels["lt1"] < levels["anchor_spot"]
    assert levels["anchor_spot"] < levels["ut1"] < levels["ut2"] < levels["ut3"]
    assert levels["band_low"] == levels["lt2"]
    assert levels["band_high"] == levels["ut2"]
    pin = levels["pin"]
    assert pin["low"] <= 7670 <= pin["high"] or pin["low"] <= 7675 <= pin["high"]
    assert "straddle" in levels["definitions"] or "straddle" in levels["em_source"] or levels["em_source"] == "atm_straddle"


def test_missing_momentum_does_not_fade_or_look_quiet():
    levels = _levels()
    regime = classify_regime(
        atm_iv=0.25,
        rv20=0.16,
        rv5=0.14,
        session_change=None,
        mom5=None,
        spot=7700,
        levels=levels,
    )
    assert regime["trend"] == "extended"
    assert regime["iv"] == "rich"
    quiet = classify_regime(
        atm_iv=0.25,
        rv20=0.16,
        rv5=0.14,
        session_change=0.1,
        mom5=None,
        spot=7668,
        levels=levels,
    )
    assert quiet["trend"] != "range"


def test_call_credit_when_ut3_fades_and_condor_when_pinned():
    levels = _levels()
    fade = classify_regime(
        atm_iv=0.25, rv20=0.16, rv5=0.18, session_change=0.6, mom5=-0.04, spot=7700, levels=levels
    )
    assert fade["trend"] == "fade_high"
    credit = select_strategies(levels, fade, _priced(), now=CORE, symbol="SPX", wing=10)
    call = next(row for row in credit if row["strategy"] == "call_credit")
    assert call["action"] == "ENTER"
    assert call["structure"] == "7700/7710"
    assert call["credit"] == round(1.8 - 0.9, 2)
    assert call["max_loss"] == round((10 - call["credit"]) * 100, 2)
    assert "50–70%" in call["message"]
    assert call["message"].startswith("🦅 SPX CALL CREDIT")

    levels["spot"] = 7668
    pinned = classify_regime(
        atm_iv=0.25, rv20=0.16, rv5=0.14, session_change=0.05, mom5=0.01, spot=7668, levels=levels
    )
    assert pinned["trend"] == "range"
    condors = select_strategies(levels, pinned, _priced(), now=CORE, symbol="SPX", wing=10)
    condor = next(row for row in condors if row["strategy"] == "iron_condor")
    assert condor["action"] == "ENTER"
    assert condor["structure"] == "7630/7640 – 7680/7690"
    assert condor["message"].startswith("🦅 SPX IRON CONDOR")
    assert "max loss" in condor["message"]
    assert condor["size"]["risk_pct"] == 0.01


def test_rich_iv_does_not_fall_back_to_a_naked_long():
    levels = dict(_levels())
    levels["spot"] = 7688
    options = _priced()
    for row in options:
        if row["right"] == "C" and row["strike"] >= 7690:
            row["bid"] = 0
    rows = select_strategies(levels, {"iv": "rich", "trend": "strong_up"}, options, now=CORE, symbol="SPX", wing=10)
    assert all(row["strategy"] != "long_call" for row in rows)
    assert all(row["strategy"] != "call_debit" for row in rows)


def test_rich_trend_is_a_debit_spread_and_cheap_trend_is_a_long():
    levels = dict(_levels())
    levels["spot"] = 7688
    rich = {"iv": "rich", "trend": "strong_up"}
    debit = select_strategies(levels, rich, _priced(), now=CORE, symbol="SPX", wing=10)
    assert debit[0]["strategy"] == "call_debit"
    assert debit[0]["action"] == "ENTER"
    cheap = {"iv": "cheap", "trend": "strong_up"}
    long = select_strategies(levels, cheap, _priced(), now=CORE, symbol="SPX", wing=10)
    assert long[0]["strategy"] == "long_call"
    assert long[0]["size"]["risk_pct"] == 0.005


def test_time_gates_block_late_condors_and_flatten():
    levels = _levels()
    levels["spot"] = 7668
    regime = {"iv": "rich", "trend": "range"}
    late = select_strategies(levels, regime, _priced(), now=LATE, symbol="SPX", wing=10)
    assert late[0]["action"] == "WATCH"
    assert "1:00 PM" in late[0]["reason"]
    flat = select_strategies(levels, regime, _priced(), now=FLAT, symbol="SPX", wing=10)
    assert flat[0]["action"] == "FLAT"


def test_running_tape_at_ut3_is_not_a_credit():
    levels = _levels()
    regime = classify_regime(
        atm_iv=0.25, rv20=0.16, rv5=0.2, session_change=1.2, mom5=0.2, spot=7702, levels=levels
    )
    assert regime["trend"] == "strong_up"
    rows = select_strategies(levels, regime, _priced(), now=CORE, symbol="SPX", wing=10)
    assert all(row["strategy"] != "call_credit" for row in rows)


def test_paper_book_take_profit_and_scorecard_are_separate(tmp_path, monkeypatch):
    levels = _levels()
    levels["spot"] = 7668
    regime = {"iv": "rich", "trend": "range"}
    chain = {
        "symbol": "SPX",
        "expiry": "2026-10-26",
        "spot": 7668,
        "options": _priced(),
        "source": "tradier",
        "session_change_pct": 0.05,
        "mom_5m_pct": 0.01,
    }
    # Realized vol from a flat history so the passed bars keep IV rich via frozen atm... 
    # build recomputes regime from bars. Feed a low realized vol.
    bars = [{"close": 7600 + index * 0.2} for index in range(40)]
    state = tmp_path / "state.json"
    board = build_spx_desk(
        chain=chain,
        bars=bars,
        now=CORE,
        state_path=state,
        fetch=False,
        cfg={"spx_strategy": {"enabled": True}},
    )
    assert board["ok"] is True
    assert board["levels"]["ut2"] > board["levels"]["anchor_spot"]
    condor = next(row for row in board["setups"] if row["strategy"] == "iron_condor")
    assert condor["action"] == "ENTER"
    assert len(board["book"]["open"]) == 1

    # Second pulse keeps the morning levels and closes the condor once the wings are cheap.
    cheap = []
    for row in _priced():
        copied = dict(row)
        copied["bid"] = 0.15
        copied["ask"] = 0.2
        copied["mid"] = 0.175
        cheap.append(copied)
    chain["options"] = cheap
    chain["spot"] = 7668
    again = build_spx_desk(chain=chain, bars=bars, now=CORE, state_path=state, fetch=False)
    assert again["levels"]["anchor_spot"] == board["levels"]["anchor_spot"]
    assert again["levels"]["ut3"] == board["levels"]["ut3"]
    assert again["book"]["closed"]
    assert again["book"]["closed"][0]["exit_reason"] == "take_profit"
    assert again["book"]["closed"][0]["win"] is True

    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("SCORECARD_LEDGER", str(ledger))
    monkeypatch.setenv("SCORECARD_EVAL", "0")
    view = observe_snapshot(spx_board=again, now=CORE)
    assert view["summary"]["count"] == 0
    assert view["rows"] == []
    stats = view["spx_strategies"]["by_strategy"]["SPX Iron Condor"]
    assert stats["closed"] == 1
    assert stats["wins"] == 1
    assert stats["pnl"] > 0
    second = observe_snapshot(spx_board=again, now=CORE)
    assert second["spx_strategies"]["count"] == 1
    assert is_sure_winner_gold(condor) is False
    assert is_golden_unusual_flow(condor) is False


def test_alert_is_one_message_per_setup_and_does_not_drop_gold():
    snapshot = {
        "sure_winners": {
            "winners": [
                {
                    "symbol": "NVDA",
                    "action": "SURE_WINNER",
                    "confidence_tier": "HIGH",
                    "sure_winner": True,
                    "go_huge": False,
                    "contract": "NVDA261023C00180000",
                    "expiry": "2026-10-23",
                    "strike": 180,
                    "right": "C",
                    "ask": 2.4,
                    "bid": 2.2,
                    "spot": 178,
                    "score": 80,
                }
            ]
        },
        "spx_strategy": {
            "setups": [
                {
                    "action": "ENTER",
                    "strategy": "iron_condor",
                    "symbol": "SPX",
                    "day": "2026-10-26",
                    "structure": "7640/7650 – 7710/7720",
                    "expiry": "2026-10-26",
                    "credit": 2.4,
                    "message": "🦅 SPX IRON CONDOR 7640/7650 – 7710/7720 credit $2.40, max loss $760.00, exit take 50–70% of credit",
                },
                {
                    "action": "WATCH",
                    "strategy": "call_credit",
                    "symbol": "SPX",
                    "day": "2026-10-26",
                    "structure": "7700/7710",
                    "message": "should not send",
                },
            ]
        },
    }
    alerts = collect_trade_alerts(snapshot)
    spx = [row for row in alerts if row["desk"] == "SPX Strategy"]
    gold = [row for row in alerts if row["desk"] == "Gold Setup"]
    assert len(spx) == 1
    assert spx[0]["symbol"] == "SPX"
    assert spx[0]["contract"] == "7640/7650 – 7710/7720"
    assert spx[0]["message"].startswith("🦅 SPX IRON CONDOR")
    assert len(gold) == 1
    assert gold[0]["symbol"] == "NVDA"


def test_replay_proxy_is_honest_when_history_is_thin_or_synthetic():
    short = [{"date": f"2026-01-{day:02d}", "open": 100, "high": 101, "low": 99, "close": 100} for day in range(1, 12)]
    thin = replay_proxy(short)
    assert thin["thin"] is True
    assert thin["option_fills"] is False
    assert "not credit captured" in thin["note"]

    bars = []
    price = 100.0
    for index in range(60):
        drift = 0.02 if index % 17 == 0 else 0.0
        opened = price
        close = price + drift
        bars.append(
            {
                "date": f"2026-03-{(index % 27) + 1:02d}-{index:02d}",
                "open": opened,
                "high": max(opened, close) + 0.05,
                "low": min(opened, close) - 0.05,
                "close": close,
            }
        )
        price = close
    report = replay_proxy(bars)
    assert report["days"] >= 30
    assert report["thin"] is False
    assert report["condor_range_held_rate"] is not None
    assert report["option_fills"] is False
