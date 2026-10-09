"""Mega early call / lottery wing coverage (MU open score-45 miss)."""

from __future__ import annotations

from types import SimpleNamespace

from odte_scanner.options.explosive import _mega_wing_symbols, _pick_chase_symbols, build_explosive_board
from odte_scanner.signals.rip_radar import is_mega_rip_symbol


def test_mu_is_mega():
    assert is_mega_rip_symbol("MU") is True


def test_mega_wing_symbols_includes_mid_score_high_em():
    scores = [
        {"symbol": "MU", "ensemble_score": 45.0, "expected_move_pct": 3.4, "last_price": 1017.0},
        {"symbol": "ZZZ", "ensemble_score": 80.0, "expected_move_pct": 1.0, "last_price": 10.0},
        {"symbol": "AAPL", "ensemble_score": 42.0, "expected_move_pct": 2.5, "last_price": 230.0},
    ]
    picked = _mega_wing_symbols(scores, max_n=8, min_score=40.0, min_expected_move_pct=2.0)
    syms = [s for s, _, _ in picked]
    assert "MU" in syms
    assert "AAPL" in syms
    assert "ZZZ" not in syms  # not a mega


def test_chase_picks_mega_below_min_ensemble():
    scores = [
        {"symbol": "MU", "ensemble_score": 45.0, "expected_move_pct": 3.4, "signals": []},
        {"symbol": "SLOW", "ensemble_score": 50.0, "expected_move_pct": 0.5, "signals": []},
    ]
    out = _pick_chase_symbols(scores, quotes={}, max_n=10, min_ensemble=55.0)
    assert "MU" in out
    assert "SLOW" not in out


def test_explosive_board_skips_puts_and_mega_enriches(monkeypatch):
    """Puts-only scan candidates must not starve mega call wings."""
    calls = {
        "found": 0,
    }

    def fake_find(sym, spot, **kwargs):
        calls["found"] += 1
        from odte_scanner.options.explosive import ExplosiveCandidate

        return [
            ExplosiveCandidate(
                symbol=sym,
                contract=f"{sym}261007C01050000",
                expiry="2026-10-07",
                dte=0,
                strike=1050.0,
                spot=spot,
                ask=1.4,
                bid=1.2,
                moneyness_pct=3.2,
                volume=100,
                open_interest=200,
                score=45.0,
                upside_at_1pct=2.8,
                upside_at_2pct=7.0,
                upside_at_3pct=16.8,
                upside_at_5pct=42.0,
                mult_at_1pct=2.0,
                mult_at_2pct=5.0,
                mult_at_3pct=12.0,
                mult_at_5pct=30.0,
                best_mult=30.0,
                best_move_pct=5.0,
                lottery_score=70.0,
                thesis="test wing",
                dte_bucket="0dte",
            )
        ]

    monkeypatch.setattr(
        "odte_scanner.options.explosive.find_explosive_calls",
        fake_find,
    )
    board = build_explosive_board(
        [
            {
                "symbol": "MU",
                "right": "P",
                "strike": 1000,
                "ask": 3.35,
                "dte": 0,
                "spot": 1017.0,
            }
        ],
        scores=[
            {
                "symbol": "MU",
                "ensemble_score": 45.0,
                "expected_move_pct": 3.4,
                "last_price": 1017.0,
            }
        ],
        enrich_live=False,
        mega_enrich=True,
        max_total=10,
    )
    assert calls["found"] >= 1
    assert any(r.get("symbol") == "MU" and float(r.get("strike") or 0) == 1050 for r in board)
    # Put-only candidate must not become an explosive call ticket
    assert not any(str(r.get("right") or "C").upper().startswith("P") for r in board)


def test_should_fetch_mega_early_calls_mu_open():
    from odte_scanner.scanner import should_fetch_mega_early_calls

    assert should_fetch_mega_early_calls(
        "MU", ensemble_score=45.09, expected_move_pct=3.36, min_score=62
    )
    assert not should_fetch_mega_early_calls(
        "MU", ensemble_score=45.09, expected_move_pct=0.5, min_score=62
    )
    assert not should_fetch_mega_early_calls(
        "SLOW", ensemble_score=45.09, expected_move_pct=3.36, min_score=62
    )
    # Already above buy floor → normal path, not "early"
    assert not should_fetch_mega_early_calls(
        "MU", ensemble_score=69.0, expected_move_pct=3.0, min_score=62
    )
