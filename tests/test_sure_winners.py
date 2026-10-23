"""Sure-winner desk — 1–2/day confidence tiers + sticky day store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.signals.sure_winners import (
    annotate_buy_rows,
    build_sure_winner_board,
    certainty_from_hist,
    collect_candidates,
    confidence_tier_for,
    select_sure_winners,
)


def test_certainty_and_confidence_tiers():
    assert certainty_from_hist(100, 5) == "perfect"
    assert certainty_from_hist(90, 6) == "elite"
    assert certainty_from_hist(82, 5) == "strong"
    assert certainty_from_hist(70, 10) == "watch"

    assert confidence_tier_for(certainty="perfect", score=72, hit_1pct=60) == "MAX"
    assert confidence_tier_for(certainty="elite", score=80, hit_1pct=75) == "MAX"
    assert confidence_tier_for(certainty="elite", score=72, hit_1pct=50) == "HIGH"
    assert confidence_tier_for(certainty="strong", score=76, hit_1pct=70) == "HIGH"
    assert confidence_tier_for(certainty="strong", score=73, hit_1pct=50) == "SOLID"
    assert confidence_tier_for(certainty="watch", score=90, hit_1pct=90) is None


def _buy(sym, *, win, n, score, hit1=70, ask=0.85, strike=100, tier=None):
    return {
        "action": "BUY_NOW",
        "symbol": sym,
        "right": "C",
        "strike": strike,
        "expiry": "2026-10-17",
        "dte": 5,
        "dte_bucket": "weekly",
        "ask": ask,
        "bid": ask - 0.05,
        "contract": f"{sym}261017C{int(round(float(strike) * 1000)):08d}",
        "win_pct": win,
        "win_samples": n,
        "hit_1pct": hit1,
        "hit_2pct": hit1 - 10,
        "score": score,
        "ensemble_score": score,
        "headline": f"{sym} hist-gated BUY",
        "detail": f"{sym} tape ok",
        "certainty_tier": tier,
        "exit_plan": "TP +80% / SL −50%",
    }


def test_collect_and_cap_two_per_day(tmp_path: Path):
    actions = {
        "buy_now": [
            _buy("AAA", win=100, n=6, score=80, hit1=80),  # MAX
            _buy("BBB", win=90, n=8, score=74, hit1=72),  # HIGH
            _buy("CCC", win=82, n=6, score=73, hit1=50),  # SOLID
        ]
    }
    cands = collect_candidates(actions=actions)
    assert len(cands) == 3
    assert cands[0]["symbol"] == "AAA"
    assert cands[0]["confidence_tier"] == "MAX"

    winners, store = select_sure_winners(cands, max_per_day=2, day="2026-10-12", store={})
    assert len(winners) == 2
    assert winners[0].confidence_tier == "MAX"
    assert winners[0].go_huge is True
    assert winners[0].size_mult == 3.0
    assert winners[0].size_label == "GO HUGE"
    assert winners[1].confidence_tier == "HIGH"
    assert winners[1].size_mult == 2.0
    assert store["day"] == "2026-10-12"
    assert len(store["picks"]) == 2


def test_sticky_day_keeps_prior_pick(tmp_path: Path):
    a = _buy("STICK", win=100, n=4, score=75)
    b = _buy("NEW1", win=100, n=5, score=90, strike=110)
    cands = collect_candidates(actions={"buy_now": [b, a]})
    # First pass picks NEW1 (higher score) then STICK
    w1, store = select_sure_winners(cands, max_per_day=1, day="2026-10-12", store={})
    assert w1[0].symbol == "NEW1"
    # Second pass: even if order flips, sticky keeps NEW1
    cands2 = collect_candidates(actions={"buy_now": [a, b]})
    w2, store2 = select_sure_winners(cands2, max_per_day=1, day="2026-10-12", store=store)
    assert w2[0].symbol == "NEW1"
    assert store2["picks"][0]["symbol"] == "NEW1"
    # New day resets
    w3, store3 = select_sure_winners(cands2, max_per_day=1, day="2026-10-13", store=store2)
    assert store3["day"] == "2026-10-13"


def test_contract_swap_keeps_the_other_ticker():
    tsla = _buy("TSLA", win=100, n=6, score=90, strike=390)
    cost = _buy("COST", win=100, n=6, score=80, strike=1025, ask=2.4)
    first, store = select_sure_winners(
        collect_candidates(actions={"buy_now": [tsla, cost]}),
        max_per_day=2,
        day="2026-10-22",
        store={},
    )
    assert [w.symbol for w in first] == ["TSLA", "COST"]

    swapped = _buy("TSLA", win=100, n=6, score=92, strike=400)
    jpm = _buy("JPM", win=100, n=6, score=88, strike=340, ask=1.8)
    second, store2 = select_sure_winners(
        collect_candidates(actions={"buy_now": [swapped, jpm, cost]}),
        max_per_day=2,
        day="2026-10-22",
        store=store,
    )
    assert [w.symbol for w in second] == ["TSLA", "COST"]
    assert second[0].strike == 400
    assert [p["symbol"] for p in store2["picks"]] == ["TSLA", "COST"]
    assert second[1].signaled_at == first[1].signaled_at


def test_two_contracts_for_one_ticker_use_one_slot():
    tsla_a = _buy("TSLA", win=100, n=6, score=90, strike=390)
    tsla_b = _buy("TSLA", win=100, n=6, score=91, strike=400)
    cost = _buy("COST", win=100, n=6, score=80, strike=1025, ask=2.4)
    winners, _store = select_sure_winners(
        collect_candidates(actions={"buy_now": [tsla_a, tsla_b, cost]}),
        max_per_day=2,
        day="2026-10-22",
        store={},
    )
    assert [w.symbol for w in winners] == ["TSLA", "COST"]
    assert winners[0].strike == 400


def test_hard_cap_two_even_if_max_per_day_higher():
    buys = [
        _buy("A", win=100, n=5, score=90, strike=100),
        _buy("B", win=100, n=5, score=89, strike=101),
        _buy("C", win=100, n=5, score=88, strike=102),
    ]
    cands = collect_candidates(actions={"buy_now": buys})
    winners, _ = select_sure_winners(cands, max_per_day=9, day="2026-10-12")
    assert len(winners) == 2


def test_annotate_buy_rows_and_board(tmp_path: Path):
    path = tmp_path / "sure.json"
    actions = {
        "buy_now": [
            _buy("GOLD", win=100, n=5, score=82),
            _buy("SKIP", win=60, n=10, score=90),  # watch — not sure
        ]
    }
    board = build_sure_winner_board(
        actions=actions,
        challenge={"entry": []},
        max_per_day=2,
        store_path=path,
    )
    assert board["counts"]["winners"] == 1
    assert board["winners"][0]["symbol"] == "GOLD"
    assert board["winners"][0]["color"] == "gold"
    annotated = board["actions"]["buy_now"]
    gold = next(r for r in annotated if r["symbol"] == "GOLD")
    skip = next(r for r in annotated if r["symbol"] == "SKIP")
    assert gold["sure_winner"] is True
    assert gold["size_label"] == "GO HUGE"
    assert skip.get("sure_winner") is not True

    # annotate helper alone
    rows = annotate_buy_rows(actions["buy_now"], board["winners"])
    assert rows[0]["sure_winner"] is True


def test_challenge_entry_can_be_sure_winner(tmp_path: Path):
    entry = {
        "action": "ENTRY",
        "symbol": "NVDA",
        "right": "C",
        "strike": 140,
        "expiry": "2026-10-24",
        "dte": 12,
        "ask": 1.25,
        "contract": "NVDA261024C00140000",
        "hist_win_pct": 100,
        "hist_samples": 4,
        "hit_1pct": 75,
        "ensemble_score": 78,
        "certainty_tier": "perfect",
        "recommend_reason": "perfect hist mega",
    }
    board = build_sure_winner_board(
        actions={"buy_now": []},
        challenge={"entry": [entry], "tickets": [entry]},
        store_path=tmp_path / "sw.json",
    )
    assert board["counts"]["winners"] == 1
    assert board["winners"][0]["source_desk"] == "Challenge"
    assert board["challenge"]["entry"][0]["sure_winner"] is True


def test_gex_long_gamma_blocks_index_0dte_sure():
    from odte_scanner.signals.sure_winners import collect_candidates, gex_blocks_sure_candidate

    spy = _buy("SPY", win=100, n=6, score=82, ask=1.2, strike=570)
    spy["dte"] = 0
    spy["dte_bucket"] = "0dte"
    tsla = _buy("TSLA", win=100, n=5, score=80, ask=2.0, strike=370)
    tsla["dte"] = 1
    tsla["dte_bucket"] = "0dte"

    red = {
        "state": "SUPPORTIVE",
        "block_0dte_long_calls": False,
        "cboe_gex": {"ok": True, "regime": "LONG_GAMMA"},
    }
    assert gex_blocks_sure_candidate(spy, red, respect_gex=True) is True
    assert gex_blocks_sure_candidate(tsla, red, respect_gex=True) is False

    cands = collect_candidates(
        actions={"buy_now": [spy, tsla]},
        red_flag=red,
        respect_gex=True,
    )
    assert [c["symbol"] for c in cands] == ["TSLA"]


def test_gex_red_flag_blocks_index_even_without_long_gamma():
    from odte_scanner.signals.sure_winners import collect_candidates

    qqq = _buy("QQQ", win=100, n=6, score=85, ask=1.5, strike=480)
    qqq["dte"] = 1
    qqq["dte_bucket"] = "0dte"
    red = {
        "state": "RED_FLAG",
        "block_0dte_long_calls": True,
        "cboe_gex": {"ok": True, "regime": "SHORT_GAMMA"},
    }
    cands = collect_candidates(
        actions={"buy_now": [qqq]},
        red_flag=red,
        respect_gex=True,
    )
    assert cands == []


def test_radar_hot_index_wing_is_high_gold_without_hist(tmp_path: Path):
    """The cheap SPY/QQQ wings the radar already had should be Sure Winner gold."""
    radar = {
        "hot": [
            {
                "action": "RADAR_HOT",
                "symbol": "SPY",
                "right": "C",
                "ask": 0.35,
                "contract": "SPY261020C00773000",
                "expiry": "2026-10-20",
                "dte": 0,
                "strike": 773,
                "mult_at_1pct": 17,
                "spot": 771.16,
                "detail": "SPY 773c @ $0.35 · ~17× if +1%",
            },
            {
                "action": "RADAR_HOT",
                "symbol": "SPY",
                "right": "C",
                "ask": 0.57,
                "contract": "SPY261020C00772000",
                "expiry": "2026-10-20",
                "dte": 0,
                "strike": 772,
                "mult_at_1pct": 9,
            },
            {
                "action": "RADAR_HOT",
                "symbol": "QQQ",
                "right": "C",
                "ask": 0.38,
                "contract": "QQQ261020C00725000",
                "expiry": "2026-10-20",
                "dte": 0,
                "strike": 725,
                "mult_at_1pct": 9,
                "detail": "QQQ 725c @ $0.38 · ~9× if +1%",
            },
            {
                "action": "RADAR_HOT",
                "symbol": "AAPL",
                "right": "C",
                "ask": 0.40,
                "contract": "AAPL261020C00270000",
                "expiry": "2026-10-20",
                "dte": 0,
                "strike": 270,
                "mult_at_1pct": 12,
            },
        ]
    }
    board = build_sure_winner_board(
        actions={"buy_now": [_buy("SKIP", win=60, n=10, score=90)]},
        radar=radar,
        max_per_day=2,
        store_path=tmp_path / "sw.json",
        now=datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    syms = [w["symbol"] for w in board["winners"]]
    assert syms == ["SPY", "QQQ"]
    spy = board["winners"][0]
    assert spy["confidence_tier"] == "HIGH"
    assert spy["go_huge"] is False
    assert spy["index_convex"] is True
    assert spy["contract"] == "SPY261020C00773000"
    assert "17" in spy["headline"]
    assert spy["exit_plan"].startswith("CONVEX runner")
    from odte_scanner.trading.golden import is_sure_winner_gold

    assert is_sure_winner_gold(spy) is True


def test_long_gamma_still_blocks_the_index_wing(tmp_path: Path):
    radar = {
        "hot": [
            {
                "action": "RADAR_HOT",
                "symbol": "SPY",
                "right": "C",
                "ask": 0.35,
                "contract": "SPY261020C00773000",
                "expiry": "2026-10-20",
                "dte": 0,
                "strike": 773,
                "mult_at_1pct": 17,
            }
        ]
    }
    board = build_sure_winner_board(
        actions={"buy_now": []},
        radar=radar,
        store_path=tmp_path / "sw.json",
        now=datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("America/New_York")),
        red_flag={"state": "SUPPORTIVE", "cboe_gex": {"regime": "LONG_GAMMA"}},
    )
    assert board["winners"] == []


def test_mega_max_ask_allows_tsla_rich_premium():
    """TSLA weekly RIP ~$8 was filtered by sure_winners_max_ask=$5."""
    tsla = _buy("TSLA", win=100, n=5, score=72, ask=8.20, strike=385)
    tsla["action"] = "BUY_RIP"
    tsla["dte"] = 3
    tsla["dte_bucket"] = "weekly"
    blocked = collect_candidates(
        rip_radar={"buy_rip": [tsla]},
        max_ask=5.0,
        mega_max_ask=None,
    )
    assert blocked == []
    ok = collect_candidates(
        rip_radar={"buy_rip": [tsla]},
        max_ask=5.0,
        mega_max_ask=12.0,
    )
    assert len(ok) == 1
    assert ok[0]["symbol"] == "TSLA"
