"""FINRA ATS dark pool helpers + volume magnets."""
import pandas as pd

from odte_scanner.echo.darkpool import compute_volume_magnets, _summarize_symbol


def test_volume_magnets_tags():
    idx = pd.date_range("2026-01-01", periods=40, freq="B")
    close = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame(
        {
            "Open": close,
            "High": close + 1.5,
            "Low": close - 1.5,
            "Close": close,
            "Volume": [1_000_000 + (i % 5) * 200_000 for i in range(len(idx))],
        },
        index=idx,
    )
    levels = compute_volume_magnets(df, bins=16, lookback=30)
    assert levels
    tags = {l["tag"] for l in levels}
    assert "hero" in tags
    assert any(t in tags for t in ("support", "resistance", "magnet"))


def test_summarize_symbol_surge_flag():
    hist = [
        {
            "weekStartDate": "2026-06-01",
            "totalWeeklyShareQuantity": 1_000_000,
            "totalWeeklyTradeCount": 10_000,
            "totalNotionalSum": 100_000_000,
        },
        {
            "weekStartDate": "2026-06-08",
            "totalWeeklyShareQuantity": 1_100_000,
            "totalWeeklyTradeCount": 11_000,
            "totalNotionalSum": 110_000_000,
        },
        {
            "weekStartDate": "2026-06-15",
            "totalWeeklyShareQuantity": 2_500_000,
            "totalWeeklyTradeCount": 20_000,
            "totalNotionalSum": 250_000_000,
        },
    ]
    venues = [
        {
            "MPID": "UBSA",
            "marketParticipantName": "UBS ATS",
            "totalWeeklyShareQuantity": 800_000,
            "totalWeeklyTradeCount": 5_000,
        }
    ]
    row = _summarize_symbol("NVDA", hist, venues, magnets=[{"price": 120, "tag": "hero", "volume_share_pct": 12}])
    assert row is not None
    assert row["flag"] == "surge"
    assert row["surge_ratio"] is not None and row["surge_ratio"] >= 1.5
    assert row["venues"][0]["mpid"] == "UBSA"
    assert row["levels"][0]["tag"] == "hero"
