from odte_scanner.options.walls import (
    WALL_EXIT_BUFFER_USD,
    ladder_from_sides,
    wall_exit_levels,
    walls_from_ladder,
)


def test_wall_buffer_default_is_ten_cents():
    assert WALL_EXIT_BUFFER_USD == 0.10


def test_call_soft_exit_is_ten_cents_below_wall():
    levels = wall_exit_levels(
        right="C",
        spot=100.0,
        call_wall=110.0,
        put_wall=95.0,
        call_wall_oi=5000,
        put_wall_oi=4000,
    )
    assert levels["primary_wall"] == 110.0
    assert levels["primary_wall_side"] == "call"
    assert levels["soft_exit"] == 109.90
    assert levels["opposite_wall"] == 95.0
    assert "CALL wall" in levels["exit_hint"]


def test_put_soft_exit_is_ten_cents_above_wall():
    levels = wall_exit_levels(
        right="P",
        spot=100.0,
        call_wall=110.0,
        put_wall=95.0,
        buffer_usd=0.10,
    )
    assert levels["primary_wall"] == 95.0
    assert levels["primary_wall_side"] == "put"
    assert levels["soft_exit"] == 95.10


def test_walls_from_ladder_picks_max_oi():
    ladder = ladder_from_sides(
        symbol="TEST",
        spot=100.0,
        expiry="2026-08-15",
        dte=9,
        calls=[
            {"strike": 100.0, "open_interest": 100, "volume": 10, "iv": 0.3},
            {"strike": 105.0, "open_interest": 9000, "volume": 20, "iv": 0.3},
            {"strike": 110.0, "open_interest": 500, "volume": 5, "iv": 0.3},
        ],
        puts=[
            {"strike": 95.0, "open_interest": 8000, "volume": 15, "iv": 0.3},
            {"strike": 90.0, "open_interest": 200, "volume": 2, "iv": 0.3},
            {"strike": 100.0, "open_interest": 50, "volume": 1, "iv": 0.3},
        ],
    )
    walls = walls_from_ladder(ladder, right="C", buffer_usd=0.10)
    assert walls["call_wall"] == 105.0
    assert walls["put_wall"] == 95.0
    assert walls["soft_exit"] == 104.90
    assert walls["call_wall_oi"] == 9000
