from odte_scanner.data.live_quotes import LiveQuote, _session_label
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
