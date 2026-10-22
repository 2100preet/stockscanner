"""Live-tape coverage and slow-name strike band."""

from odte_scanner.data.live_quotes import board_quote_symbols, momentum_from_closes
from odte_scanner.options.selector import otm_band_for_expected_move


def test_board_quotes_include_every_name_around_60():
    rows = [{"symbol": f"S{i:02d}", "score": 61 + (i % 5)} for i in range(25)]
    rows.append({"symbol": "LOW", "score": 40})
    rows.append({"symbol": "COST", "ensemble_score": 72})
    syms = board_quote_symbols(rows, extra=["UNH", "COST"], min_score=60, limit=40)
    assert "LOW" not in syms
    assert len([s for s in syms if s.startswith("S")]) == 25
    assert syms[0] == "COST" or "COST" in syms
    assert "UNH" in syms
    assert syms.count("COST") == 1


def test_momentum_from_closes_skips_a_short_tape():
    assert momentum_from_closes([10.0]) == (None, None)
    mom5, mom15 = momentum_from_closes([100.0, 100.5, 101.0, 100.2])
    assert mom5 is not None and abs(mom5 - ((100.2 / 101.0 - 1) * 100)) < 1e-9
    assert mom15 is not None and abs(mom15 - ((100.2 / 100.0 - 1) * 100)) < 1e-9


def test_slow_expected_move_tightens_the_otm_band():
    assert otm_band_for_expected_move(1.1, 3.0) == (0.25, 1.1)
    assert otm_band_for_expected_move(0.4, 3.0) == (0.25, 0.75)
    assert otm_band_for_expected_move(2.4, 3.0) == (3.0, 3.0)
    assert otm_band_for_expected_move(None, 3.0) == (3.0, 3.0)
