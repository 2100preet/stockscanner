"""Quote identity and premium sanity for Sure Winner routing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.options.quote_sanity import merge_prior_quote, quote_reject_reason
from odte_scanner.scorecard.signals import extract_signals
from odte_scanner.signals.sure_winners import collect_candidates


def _slv_splice() -> dict:
    """The Oct 22 alert: zone 50C 10/30 labeled with the Oct 23 51-call ask."""
    return {
        "symbol": "SLV",
        "action": "ENTRY",
        "right": "C",
        "contract": "SLV261023C00051000",
        "strike": 50.0,
        "expiry": "2026-10-30",
        "dte": 8,
        "ask": 0.15,
        "bid": 0.14,
        "spot": 50.01,
        "win_pct": 100.0,
        "win_samples": 6,
        "score": 70,
        "hit_1pct": 100,
        "certainty_tier": "perfect",
        "mark_source": "ask",
    }


def test_slv_splice_is_rejected():
    reason = quote_reject_reason(_slv_splice())
    assert reason
    assert "expiry" in reason or "strike" in reason


def test_real_one_day_otm_call_is_kept():
    reason = quote_reject_reason(
        {
            "symbol": "SLV",
            "right": "C",
            "contract": "SLV261023C00051000",
            "strike": 51.0,
            "expiry": "2026-10-23",
            "dte": 1,
            "ask": 0.20,
            "bid": 0.17,
            "spot": 49.86,
        }
    )
    assert reason is None


def test_atm_week_ask_under_half_percent_is_rejected():
    reason = quote_reject_reason(
        {
            "symbol": "SLV",
            "right": "C",
            "contract": "SLV261030C00050000",
            "strike": 50.0,
            "expiry": "2026-10-30",
            "dte": 8,
            "ask": 0.15,
            "bid": 0.14,
            "spot": 50.01,
        }
    )
    assert reason
    assert "0.5%" in reason


def test_zone_does_not_inherit_a_different_contract():
    zone = {
        "symbol": "SLV",
        "right": "C",
        "strike": 50.0,
        "expiry": "2026-10-30",
        "dte": 8,
        "spot": 50.01,
        "mark_source": "zone",
        "action": "WAIT",
    }
    prev = _slv_splice()
    prev["action"] = "ENTRY"
    merge_prior_quote(zone, prev)
    assert zone.get("ask") is None
    assert zone.get("contract") != "SLV261023C00051000"
    assert zone.get("mark_source") == "rejected"


def test_consistent_prior_quote_replaces_the_zone():
    zone = {
        "symbol": "SLV",
        "right": "C",
        "strike": 50.0,
        "expiry": "2026-10-30",
        "dte": 8,
        "spot": 50.01,
        "mark_source": "zone",
        "ask": None,
    }
    prev = {
        "symbol": "SLV",
        "right": "C",
        "contract": "SLV261023C00051000",
        "strike": 51.0,
        "expiry": "2026-10-23",
        "dte": 1,
        "ask": 0.20,
        "bid": 0.17,
        "spot": 49.86,
        "mark_source": "ask",
    }
    merge_prior_quote(zone, prev)
    assert zone["contract"] == "SLV261023C00051000"
    assert zone["strike"] == 51.0
    assert zone["expiry"] == "2026-10-23"
    assert zone["ask"] == 0.20


def test_sure_winner_and_telegram_and_scorecard_drop_the_splice():
    row = _slv_splice()
    row["confidence_tier"] = "HIGH"
    row["sure_winner"] = True
    assert collect_candidates(challenge={"entry": [row]}) == []
    alerts = disp.collect_trade_alerts(
        {"sure_winners": {"winners": [{**row, "action": "SURE_WINNER"}]}}
    )
    assert alerts == []
    found = extract_signals(
        sure_winners={"winners": [{**row, "action": "SURE_WINNER"}]},
        now=datetime.now(timezone.utc),
    )
    assert found == []


def test_stale_quote_timestamp_is_rejected():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    reason = quote_reject_reason(
        {
            "right": "C",
            "contract": "SLV261030C00050000",
            "strike": 50,
            "expiry": "2026-10-30",
            "dte": 8,
            "ask": 1.26,
            "bid": 1.18,
            "spot": 49.86,
            "quote_asof": old,
        }
    )
    assert reason
    assert "stale" in reason
