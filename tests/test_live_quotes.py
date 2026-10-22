from odte_scanner.data.live_quotes import LiveQuote, _session_label, prioritize_quote_symbols
import pandas as pd


def test_session_label_regular():
    ts = pd.Timestamp("2026-08-05 10:30:00", tz="America/New_York")
    assert _session_label(ts) == "regular"


def test_session_label_premarket():
    ts = pd.Timestamp("2026-08-05 07:32:00", tz="America/New_York")
    assert _session_label(ts) == "prepost"


def test_session_label_overnight():
    ts = pd.Timestamp("2026-08-05 01:15:00", tz="America/New_York")
    assert _session_label(ts) == "overnight"


def test_top_scored_symbols_are_quoted_ahead_of_alphabetical_board():
    scores = [
        {"symbol": "AAA", "ensemble_score": 10},
        {"symbol": "ADBE", "ensemble_score": 67.2},
        {"symbol": "ZZZ", "ensemble_score": 90},
    ]
    board = [{"symbol": "MMM"}, {"symbol": "AAA"}]
    picked = prioritize_quote_symbols(board, scores, top_n=2)
    assert picked == ["ZZZ", "ADBE"]
    assert "AAA" not in picked


def test_live_quote_dataclass():
    q = LiveQuote(
        symbol="MU",
        last=876.74,
        prev_close=892.67,
        change=-15.93,
        change_pct=-1.78,
        session="prepost",
        asof="2026-08-05T07:32:00",
    )
    d = q.to_dict()
    assert d["symbol"] == "MU"
    assert d["change_pct"] == -1.78
