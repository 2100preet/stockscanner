from odte_scanner.challenge.million import _approx_listed_expiry, _suggested_zone
from odte_scanner.options.yahoo_session import list_expiries, pick_challenge_contract


def test_approx_listed_expiry_is_real_date():
    expiry, dte = _approx_listed_expiry(180)
    assert len(expiry) == 10 and expiry[4] == "-"
    assert 60 <= dte <= 240


def test_suggested_zone_has_strike_and_expiry():
    z = _suggested_zone(100.0, "swing", "C")
    assert z["strike"] == 105.0
    assert z["expiry"]
    assert "~" not in str(z["expiry"]) or "DTE" in str(z["expiry"])
    assert z["dte"] >= 21
    assert z["mark_source"] == "zone"


def test_pick_challenge_contract_from_cached_payload(tmp_path, monkeypatch):
    # Point cache at temp and seed a payload
    import odte_scanner.options.yahoo_session as ys

    monkeypatch.setattr(ys, "CHAIN_CACHE", tmp_path)
    # Build fake nearest + dated chain
    import json
    import time
    from datetime import date, datetime, timedelta, timezone

    spot = 154.0
    exp = (date.today() + timedelta(days=140)).isoformat()
    exp_ts = int(datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    nearest = {
        "symbol": "PLTR",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "quote": {"regularMarketPrice": spot},
        "expirationDates": [exp_ts],
        "options": [
            {
                "calls": [
                    {
                        "contractSymbol": "PLTRFAKEC00160000",
                        "strike": 160.0,
                        "bid": 12.0,
                        "ask": 12.5,
                        "lastPrice": 12.4,
                        "openInterest": 2000,
                        "volume": 100,
                    },
                    {
                        "contractSymbol": "PLTRFAKEC00155000",
                        "strike": 155.0,
                        "bid": 0,
                        "ask": 0,
                        "lastPrice": 14.0,
                        "openInterest": 5,
                        "volume": 0,
                    },
                ],
                "puts": [],
            }
        ],
    }
    (tmp_path / "PLTR_nearest.json").write_text(json.dumps(nearest))
    dated = dict(nearest)
    (tmp_path / f"PLTR_{exp_ts}.json").write_text(json.dumps(dated))

    # Avoid network
    monkeypatch.setattr(ys, "_session", lambda force=False: None)

    picked = pick_challenge_contract("PLTR", spot, right="C", min_dte=60, max_dte=250, prefer_dte=140)
    assert picked is not None
    assert picked["strike"] == 160.0
    assert picked["expiry"] == exp
    assert picked["ask"] == 12.5
    assert picked["volume"] == 100
    assert picked["open_interest"] == 2000
    assert picked["mark_source"] == "ask"
    assert picked["contract"].startswith("PLTR")
    # Zero-volume thin OI strike must not win
    assert picked["strike"] != 155.0


def test_list_expiries_dte():
    from datetime import date, datetime, timezone

    today = date.today()
    ts = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()) + 10 * 86400
    rows = list_expiries({"expirationDates": [ts]})
    assert rows and rows[0][1] >= 9
