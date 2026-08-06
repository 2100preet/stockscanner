from odte_scanner.options.explosive import (
    _est_option_after_move,
    build_explosive_from_candidate,
    score_lottery,
)


def test_est_option_grows_on_rip():
    ask = 6.0
    # ATM-ish SPY-like: spot 500, strike 502, ask $6
    after = _est_option_after_move(spot=500, strike=502, ask=ask, move_pct=3.0, dte=0)
    assert after > ask
    mult = after / ask
    assert mult >= 2.0


def test_spy_style_lottery_ticket():
    # Cheap OTM 0DTE: ~$1.20 ask a few percent OTM can mark many× on a +5% rip
    # (same convexity class as rare parabolic 0DTE days; not a guarantee)
    c = build_explosive_from_candidate(
        {
            "symbol": "SPY",
            "contract": "SPYTEST",
            "expiry": "2026-08-06",
            "dte": 0,
            "strike": 520,
            "spot": 505,
            "ask": 1.2,
            "bid": 1.05,
            "moneyness_pct": (520 - 505) / 505 * 100,
            "volume": 2000,
            "open_interest": 5000,
            "score": 72,
        }
    )
    assert c is not None
    assert c.mult_at_5pct >= 8.0  # intrinsic ~10.25 on +5% → high multiple vs $1.20
    assert c.best_mult >= 8.0
    assert c.pct_gain_best >= 700
    d = c.to_dict()
    assert "×" in d["label"]


def test_lottery_score_prefers_cheap_convex():
    cheap = score_lottery(
        ask=4.0,
        mult_2=4.0,
        mult_3=8.0,
        mult_5=20.0,
        moneyness_pct=0.8,
        dte=0,
        volume=800,
        open_interest=2000,
        ensemble_score=70,
    )
    rich = score_lottery(
        ask=40.0,
        mult_2=1.2,
        mult_3=1.5,
        mult_5=2.0,
        moneyness_pct=-1.5,
        dte=0,
        volume=10,
        open_interest=20,
        ensemble_score=70,
    )
    assert cheap > rich


def test_rejects_non_convex_weeklyish():
    c = build_explosive_from_candidate(
        {
            "symbol": "SPY",
            "contract": "X",
            "expiry": "2026-08-10",
            "dte": 4,
            "strike": 500,
            "spot": 500,
            "ask": 8.0,
            "bid": 7.5,
            "moneyness_pct": 0,
            "volume": 100,
            "open_interest": 100,
            "score": 60,
        }
    )
    assert c is None
