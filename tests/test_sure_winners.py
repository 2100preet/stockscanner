"""Sure-winner desk — 1–2/day confidence tiers + sticky day store."""

from __future__ import annotations

from pathlib import Path

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
        "contract": f"{sym}261017C00{strike:05d}000",
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
