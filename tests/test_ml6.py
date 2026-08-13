"""ML6 neocloud earnings model tests."""

from __future__ import annotations

from datetime import date

import pandas as pd

from odte_scanner.data.universe import liquid_universe, ml6_universe, resolve_scan_universe
from odte_scanner.ml6.board import build_ml6_board
from odte_scanner.ml6.scoring import reaction_gate, score_ml6_name
from odte_scanner.ml6.watchlist import (
    BOTTOM_LINE_RULES,
    ML6_WATCHLIST,
    STATUS_BUY_IF,
    STATUS_WAIT,
    STATUS_WATCH,
    ml6_tickers,
)


def _fake_hist(last: float = 12.0, high: float = 20.0, vol: float = 500_000) -> pd.DataFrame:
    n = 80
    closes = [high * (1 - 0.01 * i) for i in range(n - 1)] + [last]
    highs = [max(c, high * 0.95) for c in closes]
    lows = [c * 0.97 for c in closes]
    opens = closes[:]
    volumes = [vol] * n
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def test_ml6_watchlist_has_required_names():
    for sym in ("FRMI", "TSSI", "IREN", "ORCL", "APLD", "CORZ", "NBIS", "CRWV"):
        assert sym in ML6_WATCHLIST
        assert ML6_WATCHLIST[sym].get("earnings_date")
        assert ML6_WATCHLIST[sym].get("themes")
        assert ML6_WATCHLIST[sym].get("status") in {
            STATUS_WATCH,
            STATUS_WAIT,
            STATUS_BUY_IF,
        }


def test_bottom_line_rules_cover_frmi_tssi_iren():
    tickers = {r["ticker"] for r in BOTTOM_LINE_RULES}
    assert tickers == {"FRMI", "TSSI", "IREN"}
    text = " ".join(r["rule"] for r in BOTTOM_LINE_RULES)
    assert "confirmed reaction" in text.lower() or "confirmed reaction" in text
    assert "after-hours high" in text.lower() or "VWAP" in text
    assert "AI ARR" in text or "contracted revenue" in text


def test_universe_keeps_liquid_and_adds_ml6():
    liquid = liquid_universe()
    for sym in ("SPY", "QQQ", "NVDA", "ORCL"):
        assert sym in liquid
    for sym in ("FRMI", "TSSI", "IREN", "NBIS", "CRWV", "APLD", "CORZ"):
        assert sym in liquid
    ml6 = ml6_universe()
    assert set(ml6_tickers()).issubset(set(ml6))
    cfg = {"tickers": ["SPY", "QQQ"], "universe": {"mode": "focus"}}
    assert resolve_scan_universe(cfg, mode="ml6") == ml6


def test_frmi_reaction_gate_blocks_auto_buy():
    meta = {"symbol": "FRMI", "status": STATUS_WAIT}
    gate = reaction_gate(meta=meta, days=0, quote=None)
    assert gate["blocked_auto_buy"] is True
    assert gate["status"] == STATUS_WAIT


def test_tssi_status_is_buy_only_if_accepted():
    row = score_ml6_name("TSSI", _fake_hist(), asof=date(2026, 8, 13))
    assert row["status"] == STATUS_BUY_IF
    assert row["blocked_auto_buy"] is True
    assert "VWAP" in (row.get("gate") or "") or "AH" in (row.get("gate") or "")


def test_iren_watch_mentions_arr():
    row = score_ml6_name("IREN", _fake_hist(last=8.0, high=18.0), asof=date(2026, 8, 13))
    assert row["status"] == STATUS_WATCH
    assert "ARR" in (row.get("blurb") or "") or "contracted revenue" in (row.get("blurb") or "")
    assert row["days_to_earnings"] == (date(2026, 8, 27) - date(2026, 8, 13)).days


def test_build_ml6_board_offline():
    hist = {s: _fake_hist() for s in ml6_tickers()}
    board = build_ml6_board(hist, symbols=ml6_tickers())
    assert board["horizon"] == "ml6"
    assert len(board["bottom_line_rules"]) == 3
    by_sym = {r["symbol"]: r for r in board["watchlist"]}
    assert by_sym["FRMI"]["status"] == STATUS_WAIT
    assert by_sym["TSSI"]["status"] == STATUS_BUY_IF
    assert by_sym["IREN"]["status"] == STATUS_WATCH
    assert all("ensemble_score" in r for r in board["watchlist"])


def test_cli_parser_accepts_ml6_horizon():
    from odte_scanner.cli import build_parser

    p = build_parser()
    args = p.parse_args(["scan", "--horizon", "ml6", "--no-paper"])
    assert args.horizon == "ml6"
