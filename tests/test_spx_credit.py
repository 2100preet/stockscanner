"""SPX 0DTE iron-condor credit playbook tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals import spx_credit as sc

ET = ZoneInfo("America/New_York")


def _chain_at(spot: float = 7774.0) -> list[dict]:
    """Synthetic SPX chain around the screenshot strikes."""
    out = []
    for k in range(7680, 7905, 5):
        # Rough wing premiums: closer = richer
        put_otm = max(0.0, spot - k)
        call_otm = max(0.0, k - spot)
        # Put bid/ask
        put_mid = max(0.05, 8.0 * max(0.0, 1.0 - put_otm / 120.0) ** 2)
        call_mid = max(0.05, 8.0 * max(0.0, 1.0 - call_otm / 120.0) ** 2)
        out.append(
            {
                "option_type": "put",
                "strike": float(k),
                "bid": round(put_mid - 0.05, 2),
                "ask": round(put_mid + 0.05, 2),
                "symbol": f"SPX260807P{int(k * 1000):08d}",
                "expiration_date": "2026-10-07",
            }
        )
        out.append(
            {
                "option_type": "call",
                "strike": float(k),
                "bid": round(call_mid - 0.05, 2),
                "ask": round(call_mid + 0.05, 2),
                "symbol": f"SPX260807C{int(k * 1000):08d}",
                "expiration_date": "2026-10-07",
            }
        )
    return out


def test_pick_iron_condor_near_target_credit():
    chain = _chain_at(7774.0)
    pkg = sc.pick_iron_condor(
        chain,
        symbol="SPX",
        spot=7774.0,
        expiry="2026-10-07",
        dte=0,
        wing_width=10.0,
        short_otm_pts=50.0,
        target_credit=0.90,
        min_credit=0.50,
        max_credit=2.50,
        credit_tol=0.50,
    )
    assert pkg is not None
    assert pkg["wing_width"] == 10.0
    assert pkg["short_put"] < 7774 < pkg["short_call"]
    assert pkg["long_put"] == pkg["short_put"] - 10
    assert pkg["long_call"] == pkg["short_call"] + 10
    assert pkg["credit"] >= 0.50
    assert len(pkg["legs"]) == 4


def test_sell_credit_in_regular_session():
    chain = _chain_at(7774.0)
    pkg = sc.pick_iron_condor(
        chain,
        symbol="SPX",
        spot=7774.0,
        expiry="2026-10-07",
        target_credit=0.90,
        min_credit=0.50,
        max_credit=2.50,
        credit_tol=0.60,
    )
    assert pkg
    now = datetime(2026, 10, 7, 11, 30, tzinfo=ET)
    sig = sc.decide_spx_credit_entry(pkg, now=now, stop_debit=1.40)
    assert sig.action == "SELL_CREDIT"
    assert sig.stop_debit == 1.40
    d = sig.to_dict()
    assert d["alert_action"] == "BUY_NOW"


def test_wait_on_open_drive_and_trend_day():
    chain = _chain_at(7774.0)
    pkg = sc.pick_iron_condor(
        chain,
        symbol="SPX",
        spot=7774.0,
        expiry="2026-10-07",
        min_credit=0.50,
        max_credit=2.50,
        credit_tol=0.60,
    )
    assert pkg
    open_drive = datetime(2026, 10, 7, 9, 40, tzinfo=ET)
    sig = sc.decide_spx_credit_entry(pkg, now=open_drive)
    assert sig.action == "WAIT"

    regular = datetime(2026, 10, 7, 11, 0, tzinfo=ET)
    trend = sc.decide_spx_credit_entry(
        pkg, quote={"session_change_pct": 1.5}, now=regular
    )
    assert trend.action == "WAIT"
    assert "trend" in (trend.vetoes or []) or "trend" in trend.detail.lower()


def test_exit_on_stop_and_profit():
    trade = {
        "status": "open",
        "symbol": "SPX",
        "entry_ask": 0.90,
        "credit": 0.90,
        "stop_debit": 1.40,
        "contract": "SPX:2026-10-07:IC:7720/7710-7825/7835",
        "short_put": 7720,
        "long_put": 7710,
        "short_call": 7825,
        "long_call": 7835,
        "wing_width": 10,
    }
    stop = sc.decide_spx_credit_exit(trade, package={"debit_to_close": 1.45})
    assert stop and stop.action == "BUY_TO_CLOSE"
    assert "stop" in stop.detail.lower()

    bank = sc.decide_spx_credit_exit(trade, package={"debit_to_close": 0.40})
    assert bank and bank.action == "BUY_TO_CLOSE"
    assert "bank" in bank.detail.lower()

    hold = sc.decide_spx_credit_exit(
        trade,
        package={"debit_to_close": 0.75},
        now=datetime(2026, 10, 7, 12, 0, tzinfo=ET),
    )
    assert hold and hold.action == "HOLD"


def test_build_board_with_injected_chain():
    now = datetime(2026, 10, 7, 11, 15, tzinfo=ET)
    board = sc.build_spx_credit_board(
        symbols=["SPX"],
        quotes={"SPX": {"last": 7774.0, "session_change_pct": 0.2}},
        chains={"SPX": _chain_at(7774.0)},
        fetch_live=False,
        now=now,
        min_credit=0.50,
        target_credit=0.90,
        stop_debit=1.40,
    )
    assert board["counts"]["sell_credit"] + board["counts"]["wait"] >= 1
    assert "iron condor" in board["purpose"].lower() or "Iron" in board["purpose"]
    assert any("1.40" in r for r in board["rules"])
