"""Strong-trend and confirmed-flow promotion to sure_winner_gold.

Replays the 2026-10-22 ADBE tape: score 67.2, 8 confirms, RS 93,
gap-and-go 85, no live 5-minute print, Golden Flow 10/23 260C.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import ActionSignal, apply_hist_win_gate, decide_entry
from odte_scanner.signals.sure_winners import build_sure_winner_board, collect_candidates
from odte_scanner.signals.tape import tape_direction
from odte_scanner.signals.trend_gold import is_strong_trend
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import SANDBOX_TRADE_HOST, WebullBroker

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
# 9:42 AM CT is 10:42 AM ET, inside the 09:35–14:30 ET window.
ADBE_NOW = datetime(2026, 10, 22, 9, 42, tzinfo=CT)


def _adbe_score(**overrides):
    row = {
        "symbol": "ADBE",
        "ensemble_score": 67.2,
        "score": 67.2,
        "confirms": 8,
        "relative_strength": 93,
        "gap_and_go": 85,
        "last_price": 262.64,
        "live_change_pct": None,
        "win_pct": 50.0,
        "win_samples": 6,
        "horizon": "0dte",
        "signals": [
            {"name": "relative_strength", "score": 93, "bullish": True},
            {"name": "gap_and_go", "score": 85, "bullish": True},
        ],
    }
    row.update(overrides)
    return row


def _adbe_flow(**overrides):
    row = {
        "symbol": "ADBE",
        "tier": "golden",
        "flags": ["golden", "large_premium"],
        "right": "C",
        "strike": 260,
        "expiry": "2026-10-23",
        "dte": 1,
        "ask": 4.85,
        "bid": 4.60,
        "contract": "ADBE261023C00260000",
        "spot": 262.64,
    }
    row.update(overrides)
    return row


def _far_call():
    return {
        "symbol": "ADBE",
        "right": "C",
        "strike": 270,
        "expiry": "2026-10-23",
        "dte": 1,
        "dte_bucket": "0dte",
        "ask": 1.10,
        "bid": 1.00,
        "contract": "ADBE261023C00270000",
        "spot": 262.64,
        "moneyness_pct": 2.80,
        "action": "WAIT",
    }


def _hist_buy(sym, *, score=90, strike=100):
    return {
        "action": "BUY_NOW",
        "symbol": sym,
        "right": "C",
        "strike": strike,
        "expiry": "2026-10-30",
        "dte": 6,
        "dte_bucket": "weekly",
        "ask": 1.20,
        "bid": 1.10,
        "contract": f"{sym}261030C{int(round(float(strike) * 1000)):08d}",
        "win_pct": 100,
        "win_samples": 6,
        "hit_1pct": 80,
        "score": score,
        "ensemble_score": score,
        "headline": f"{sym} perfect hist",
    }


def _board(tmp_path: Path, **kwargs):
    scores = kwargs.pop("scores", [_adbe_score()])
    flow = kwargs.pop("flow", _adbe_flow())
    rows = kwargs.pop("option_rows", [_far_call()])
    cfg = kwargs.pop("promotion_cfg", None)
    return build_sure_winner_board(
        actions={"buy_now": kwargs.pop("buy_now", [])},
        scores=scores,
        option_flow={"prints": [flow], "golden": [flow]} if flow else None,
        option_rows=rows,
        promotion_cfg=cfg,
        store_path=tmp_path / "sure.json",
        now=kwargs.pop("now", ADBE_NOW),
        max_per_day=kwargs.pop("max_per_day", 2),
        **kwargs,
    )


def test_missing_momentum_is_not_a_flat_tape():
    assert tape_direction(None, _adbe_score()) == "up"
    assert tape_direction({"mom_5m_pct": 0.0, "session_change_pct": 4.9}, _adbe_score()) == "flat"
    assert tape_direction({"session_change_pct": 4.9}, {"live_change_pct": None}) == "up"
    bars = [{"close": 100}, {"close": 100.2}, {"close": 100.4}, {"close": 100.1}, {"close": 100.3}, {"close": 100.8}]
    assert tape_direction({"bars": bars}, None) == "up"


def test_adbe_oct_22_becomes_sure_winner_gold_when_slot_free(tmp_path: Path):
    board = _board(tmp_path)
    assert board["counts"]["winners"] == 1
    row = board["winners"][0]
    assert row["symbol"] == "ADBE"
    assert row["strike"] == 260
    assert row["contract"] == "ADBE261023C00260000"
    assert row["promotion_path"] == "confirmed_flow"
    assert row["confidence_tier"] == "HIGH"
    assert row["go_huge"] is False
    assert row["max_contracts"] == 1
    assert row["sure_winner"] is True
    assert golden_signal_kind(row) == "sure_winner_gold"
    assert "hist gate unchanged" in row["detail"]
    assert is_strong_trend(_adbe_score()) is True
    # The far 270C the board used that morning is not the card.
    assert row["strike"] != 270


def test_promoted_gold_uses_reserved_slot_without_raising_hist_cap(tmp_path: Path):
    board = _board(
        tmp_path,
        buy_now=[
            _hist_buy("JPM", score=91, strike=210),
            _hist_buy("TSLA", score=90, strike=430),
            _hist_buy("NVDA", score=89, strike=180),
        ],
    )
    winners = board["winners"]
    hist = [w for w in winners if not w.get("promotion_path")]
    promo = [w for w in winners if w.get("promotion_path")]
    assert len(hist) == 2
    assert {w["symbol"] for w in hist} == {"JPM", "TSLA"}
    assert len(promo) == 1
    assert promo[0]["symbol"] == "ADBE"
    assert golden_signal_kind(promo[0]) == "sure_winner_gold"
    assert len(winners) == 3

    blocked = _board(
        tmp_path,
        buy_now=[_hist_buy("JPM", strike=210), _hist_buy("TSLA", strike=430)],
        promoted_extra_slots=0,
    )
    assert all(w["symbol"] != "ADBE" for w in blocked["winners"])
    assert len(blocked["winners"]) == 2


def test_flags_window_far_strike_and_weak_tape_do_not_promote(tmp_path: Path):
    off = _board(
        tmp_path,
        promotion_cfg={"trend_gold_enabled": False, "flow_gold_enabled": False},
    )
    assert off["winners"] == []

    early = _board(tmp_path, now=datetime(2026, 10, 22, 8, 0, tzinfo=ET))
    assert early["winners"] == []

    far_only = _board(tmp_path, flow=None, option_rows=[_far_call()])
    assert far_only["winners"] == []

    weak = _board(
        tmp_path,
        scores=[_adbe_score(ensemble_score=40, score=40, confirms=2, relative_strength=30, gap_and_go=20)],
    )
    assert weak["winners"] == []

    flat = _board(tmp_path, scores=[_adbe_score(mom_5m_pct=0.0)])
    assert flat["winners"] == []

    rich = _board(tmp_path, flow=_adbe_flow(ask=12.5))
    assert rich["winners"] == []

    faded = _board(tmp_path, scores=[_adbe_score(live_change_pct=-1.2)])
    assert faded["winners"] == []

    put_flow = _adbe_flow(right="P", strike=260, contract="ADBE261023P00260000")
    against = _board(tmp_path, flow=put_flow, option_rows=[])
    assert against["winners"] == []

    near = {
        "symbol": "ADBE",
        "right": "C",
        "strike": 262.5,
        "expiry": "2026-10-23",
        "dte": 1,
        "ask": 3.40,
        "bid": 3.20,
        "contract": "ADBE261023C00262500",
        "spot": 262.64,
    }
    trend_only = _board(tmp_path, flow=None, option_rows=[near, _far_call()])
    assert len(trend_only["winners"]) == 1
    assert trend_only["winners"][0]["promotion_path"] == "strong_trend"
    assert trend_only["winners"][0]["strike"] == 262.5
    assert golden_signal_kind(trend_only["winners"][0]) == "sure_winner_gold"


def test_hist_gate_still_blocks_fifty_percent_and_flat_names_wait():
    sig = ActionSignal(
        action="BUY_NOW",
        strength=70,
        headline="BUY NOW ADBE · 0DTE",
        detail="score ok",
        symbol="ADBE",
        win_pct=50.0,
        win_samples=6,
        dte_bucket="0dte",
    )
    blocked = apply_hist_win_gate(sig, min_hist_win_pct=80, min_hist_win_samples=5)
    assert blocked.action == "WAIT"
    assert "blocked" in blocked.detail

    weak_buy = {
        "action": "BUY_NOW",
        "symbol": "ADBE",
        "right": "C",
        "strike": 260,
        "expiry": "2026-10-23",
        "ask": 4.85,
        "contract": "ADBE261023C00260000",
        "win_pct": 50,
        "win_samples": 6,
        "score": 67.2,
    }
    assert collect_candidates(actions={"buy_now": [weak_buy]}) == []

    flat_otm = decide_entry(
        {
            "symbol": "FLAT",
            "score": 80,
            "strike": 270,
            "expiry": "2026-10-23",
            "ask": 1.2,
            "bid": 1.1,
            "contract": "FLAT261023C00270000",
            "dte": 0,
            "dte_bucket": "0dte",
            "moneyness_pct": 2.9,
        },
        quote={
            "session_change_pct": 0.1,
            "mom_5m_pct": 0.0,
            "mom_15m_pct": 0.0,
        },
        buy_score=70,
        now=datetime(2026, 10, 22, 10, 42, tzinfo=ET),
    )
    assert flat_otm.action == "WAIT"
    assert "flat" in flat_otm.detail.lower()

    flat_atm = decide_entry(
        {
            "symbol": "FLAT",
            "score": 80,
            "strike": 262,
            "expiry": "2026-10-23",
            "ask": 3.4,
            "bid": 3.2,
            "contract": "FLAT261023C00262000",
            "dte": 0,
            "dte_bucket": "0dte",
            "moneyness_pct": 0.0,
        },
        quote={
            "last": 262.0,
            "session_change_pct": 0.05,
            "mom_5m_pct": 0.0,
            "mom_15m_pct": 0.0,
        },
        buy_score=70,
        now=datetime(2026, 10, 22, 10, 42, tzinfo=ET),
    )
    assert flat_atm.action == "WAIT"
    assert "bounce" in flat_atm.detail.lower()

    held = decide_entry(
        {
            "symbol": "ADBE",
            "score": 67.2,
            "strike": 270,
            "expiry": "2026-10-23",
            "ask": 1.1,
            "bid": 1.0,
            "contract": "ADBE261023C00270000",
            "dte": 0,
            "dte_bucket": "0dte",
            "moneyness_pct": 2.8,
            "confirms": 8,
            "relative_strength": 93,
            "gap_and_go": 85,
            "live_change_pct": None,
        },
        quote=None,
        buy_score=72,
        now=datetime(2026, 10, 22, 10, 42, tzinfo=ET),
    )
    assert held.action == "WAIT"
    assert "while tape flat" not in held.detail.lower()
    assert "tape flat on the day" not in held.detail.lower()


def test_promoted_card_routes_one_paper_contract(tmp_path: Path):
    board = _board(tmp_path)
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(
        broker,
        require_perfect_hist=True,
        golden_only=True,
        max_risk_per_trade_usd=2000,
        sure_winner_max_contracts=3,
        max_contracts=1,
    )
    out = trader.sync(
        sure_winners={"winners": board["winners"], "buy_now": []},
        option_flow={"prints": [_adbe_flow()], "golden": [_adbe_flow()]},
        now=datetime(2026, 10, 22, 10, 42, tzinfo=ET),
    )
    assert out["submitted_n"] == 1
    order = out["submitted"][0]
    assert order["symbol"] == "ADBE"
    assert order["desk"] == "sure_winner"
    assert order["quantity"] == 1
    assert order["strike"] == 260
    assert order["meta"]["golden_kind"] == "sure_winner_gold"
    assert order["status"] == "dry_run"
    assert out["broker"]["dry_run"] is True
    assert out["broker"]["host"] == SANDBOX_TRADE_HOST
    assert all(o.symbol != "ADBE" or o.quantity == 1 for o in broker.orders)
