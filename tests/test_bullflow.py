"""Bullflow Golden Flow client, alerts, skip filters, and Webull isolation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.echo.chain_ladder import select_echo_expiries
from odte_scanner.echo.flow import build_option_flow
from odte_scanner.edge.filters import golden_flow_skip_reason
from odte_scanner.signals.bullflow import (
    BullflowSettings,
    BullflowTape,
    classify_bullflow_print,
    fetch_historical_options_flow,
    is_cancelled,
    next_reconnect_delay,
    parse_sse_frame,
    reset_tape_for_tests,
    trade_to_print,
)
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import WebullBroker

CT = ZoneInfo("America/Chicago")

# Friday Nov 20, 2026 — official historical field names (Unix seconds).
NVDA_ASK_BLOCK = {
    "id": "nvda-160c-ask-block-close",
    "tradeDate": "2026-11-20",
    "timestamp": int(datetime(2026, 11, 20, 14, 45, 14, tzinfo=CT).timestamp()),
    "symbol": "O:NVDA270917C00160000",
    "ticker": "NVDA",
    "optionType": "C",
    "expirationDate": "2027-09-17",
    "strikePrice": 160,
    "premium": 1_283_100,
    "tradePrice": 14.1,
    "tradeSize": 910,
    "tradeType": "BLOCK",
    "side": "Ask",
    "spotPrice": 128.0,
    "currentBid": 13.9,
    "currentAsk": 14.1,
    "currentVolume": 2904,
    "openInterest": 5438,
    "impliedVolatility": 45.0,
    "sigScore": 0.57,
    "isCancelled": False,
}

NVDA_ABOVE_ASK_SWEEP = {
    "id": "nvda-160c-above-ask-sweep",
    "tradeDate": "2026-11-20",
    "timestamp": int(datetime(2026, 11, 20, 8, 59, 39, tzinfo=CT).timestamp()),
    "symbol": "O:NVDA261218C00160000",
    "ticker": "NVDA",
    "optionType": "C",
    "expirationDate": "2026-12-18",
    "strikePrice": 160,
    "premium": 1_067_916,
    "tradePrice": 2.628,
    "tradeSize": 4064,
    "tradeType": "SWEEP",
    "side": "Above Ask",
    "spotPrice": 141.73,
    "currentBid": 2.5,
    "currentAsk": 2.6,
    "currentVolume": 14164,
    "openInterest": 28791,
    "impliedVolatility": 40.0,
    "sigScore": 0.81,
    "isCancelled": False,
}

NVDA_BID_SWEEP = {
    "id": "nvda-160c-bid-sweep-morning",
    "tradeDate": "2026-11-20",
    "timestamp": int(datetime(2026, 11, 20, 8, 41, 53, tzinfo=CT).timestamp()),
    "symbol": "O:NVDA270416C00160000",
    "ticker": "NVDA",
    "optionType": "C",
    "expirationDate": "2027-04-16",
    "strikePrice": 160,
    "premium": 1_027_000,
    "tradePrice": 10.27,
    "tradeSize": 1000,
    "tradeType": "SWEEP",
    "side": "Bid",
    "spotPrice": 139.4,
    "currentVolume": 1183,
    "openInterest": 9841,
    "sigScore": 0.58,
    "isCancelled": False,
}


@pytest.fixture(autouse=True)
def _isolated_tape():
    reset_tape_for_tests()
    yield
    reset_tape_for_tests()


def test_parse_sse_frames():
    assert parse_sse_frame("heartbeat") is None
    init = parse_sse_frame(
        'data: {"event":"init","mode":"live-options-flow","timezone":"America/New_York"}'
    )
    assert init["event"] == "init"
    assert init["mode"] == "live-options-flow"
    hb = parse_sse_frame('data: {"event":"heartbeat","timestamp":"2026-11-24T05:27:20.978Z"}')
    assert hb["event"] == "heartbeat"
    trade = parse_sse_frame('data: {"event":"trade","data":{"id":"abc","ticker":"NVDA"}}')
    assert trade["event"] == "trade"
    assert trade["data"]["id"] == "abc"
    err = parse_sse_frame('data: {"event":"error","message":"retrying"}')
    assert err["event"] == "error"
    date = parse_sse_frame('data: {"event":"date_changed","date":"2026-11-23","timezone":"America/New_York"}')
    assert date["event"] == "date_changed"


def test_reconnect_backoff_is_conservative():
    d0 = next_reconnect_delay(0, initial_sec=25, cap_sec=300, jitter=0.2)
    d1 = next_reconnect_delay(1, initial_sec=25, cap_sec=300, jitter=0.2)
    d2 = next_reconnect_delay(2, initial_sec=25, cap_sec=300, jitter=0.2)
    assert d0 >= 25 * 1.2 - 1e-9
    assert d0 < 40
    assert d1 >= 50 * 1.2 - 1e-9
    assert d2 >= 100 * 1.2 - 1e-9
    capped = next_reconnect_delay(8, initial_sec=25, cap_sec=300, jitter=0.0)
    assert capped == 300
    # Even zero jitter keeps the 25s floor, so 3 attempts cannot fit in 60s.
    assert next_reconnect_delay(0, initial_sec=25, cap_sec=300, jitter=0.0) == 25
    assert 25 + 50 > 60


def test_cancelled_prints_are_dropped():
    raw = dict(NVDA_ASK_BLOCK, id="cancelled-one", isCancelled=True)
    assert is_cancelled(raw) is True
    assert trade_to_print(raw) is None
    tape = BullflowTape()
    assert tape.ingest_trade(raw) is None
    assert tape.prints() == []


def test_deduplicate_by_bullflow_id():
    tape = BullflowTape()
    first = tape.ingest_trade(NVDA_ASK_BLOCK)
    second = tape.ingest_trade(dict(NVDA_ASK_BLOCK, premium=1_500_000))
    assert first is not None and second is not None
    rows = tape.prints()
    assert len(rows) == 1
    assert rows[0]["id"] == "nvda-160c-ask-block-close"
    assert rows[0]["premium_notional"] == 1_500_000
    # Second ingest of the same id is not a fresh alert.
    assert second["alertable"] is False


def test_startup_backfill_does_not_realert():
    tape = BullflowTape()
    stored = tape.ingest_trade(NVDA_ASK_BLOCK, backfill=True)
    assert stored is not None
    assert stored["backfill"] is True
    assert stored["alertable"] is False
    again = tape.ingest_trade(NVDA_ASK_BLOCK, backfill=False)
    assert again["alertable"] is False
    alerts = disp.collect_trade_alerts(
        {"echo": {"option_flow": tape.as_option_flow()}}
    )
    assert all(a.get("desk") != "Golden Flow" for a in alerts)


def test_historical_429_honors_retry_after():
    class _Resp:
        def __init__(self, status, payload=None, retry=None):
            self.status_code = status
            self.headers = {"Retry-After": str(retry)} if retry is not None else {}
            self._payload = payload or {}

        def json(self):
            return self._payload

    calls = {"n": 0, "slept": []}

    class _Session:
        def get(self, url, headers=None, params=None, timeout=None):
            assert "key" not in (params or {})
            assert headers and "X-API-Key" in headers
            assert "key=" not in url
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(429, retry=2)
            return _Resp(
                200,
                {"rows": [NVDA_ASK_BLOCK], "hasMore": False, "nextCursor": None, "count": 1},
            )

    out = fetch_historical_options_flow(
        date="2026-11-20",
        ticker="NVDA",
        min_premium=1_000_000,
        api_key="test-key",
        session=_Session(),
        sleep=lambda s: calls["slept"].append(s),
    )
    assert out["ok"] is True
    assert out["count"] == 1
    assert calls["n"] == 2
    assert calls["slept"] == [2]


def _print(raw, **kwargs):
    row = trade_to_print(raw, settings=BullflowSettings())
    assert row is not None
    row.update(kwargs)
    return classify_bullflow_print(row, BullflowSettings())


def test_friday_nvda_buy_side_prints_are_golden_and_telegram():
    block = _print(NVDA_ASK_BLOCK)
    sweep = _print(NVDA_ABOVE_ASK_SWEEP)
    assert block["tier"] == "golden"
    assert sweep["tier"] == "golden"
    assert block["alertable"] is True
    assert sweep["alertable"] is True
    assert golden_flow_skip_reason(block) is None
    assert golden_flow_skip_reason(sweep) is None
    # Yahoo-measured slices would have been 10–12 CT / long-dated / far OTM.
    mid_morning = _print(
        dict(
            NVDA_ABOVE_ASK_SWEEP,
            id="midday-clone",
            timestamp=int(datetime(2026, 11, 20, 10, 5, 0, tzinfo=CT).timestamp()),
        )
    )
    assert golden_flow_skip_reason(mid_morning) is None

    def _bodies(rows):
        alerts = disp.collect_trade_alerts(
            {"echo": {"option_flow": {"golden": rows, "prints": rows}}}
        )
        return [a["message"] for a in alerts if a.get("desk") == "Golden Flow"]

    block_msgs = _bodies([block])
    sweep_msgs = _bodies([sweep])
    assert block_msgs and sweep_msgs
    assert any("GOLDEN FLOW" in m and "BLOCK" in m and "Ask" in m and "$1,283,100" in m for m in block_msgs)
    assert any("GOLDEN FLOW" in m and "SWEEP" in m and "Above Ask" in m and "$1,067,916" in m for m in sweep_msgs)
    assert all("not a Webull fill" in m for m in block_msgs + sweep_msgs)
    assert all("Sure Winner" not in m for m in block_msgs + sweep_msgs)
    # One-symbol collapse still sends a Golden Flow alert when both print.
    combined = _bodies([block, sweep])
    assert combined and "GOLDEN FLOW" in combined[0] and "$1,283,100" in combined[0]


def test_bid_side_million_print_is_not_telegram_golden():
    bid = _print(NVDA_BID_SWEEP)
    assert bid["tier"] == "unusual"
    assert bid["alertable"] is False
    assert "golden" not in bid["flags"]
    alerts = disp.collect_trade_alerts(
        {"echo": {"option_flow": {"golden": [bid], "prints": [bid], "unusual": [bid]}}}
    )
    assert all(a.get("desk") != "Golden Flow" for a in alerts)
    # Still visible as unusual on the board helper.
    assert bid["premium_notional"] == 1_027_000
    assert bid["side"] == "Bid"


def test_yahoo_skip_filters_still_apply_to_proxy_prints():
    yahoo = {
        "right": "C",
        "moneyness_pct": 2.0,
        "dte": 10,
        "signaled_at": "2026-10-26T16:30:00+00:00",
        "source": "yahoo",
    }
    assert "1–3%" in golden_flow_skip_reason(yahoo)
    assert "puts/shorts" in golden_flow_skip_reason({**yahoo, "right": "P", "moneyness_pct": 0.4})


def test_webull_auto_trader_ignores_bullflow_golden_prints(tmp_path: Path):
    row = _print(NVDA_ASK_BLOCK)
    assert golden_signal_kind(row) is None
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=True, golden_only=True)
    out = trader.sync(option_flow={"golden": [row], "prints": [row]})
    assert out["submitted_n"] == 0
    assert all(o.symbol != "NVDA" for o in broker.orders)
    reasons = " ".join(str(x) for x in (out.get("filtered") or out.get("notes") or []))
    # Flow rows are explicitly marked not auto-traded.
    assert "SPY" not in {o.symbol for o in broker.orders}


def test_build_option_flow_uses_tape_when_key_set(monkeypatch):
    tape = BullflowTape()
    tape.ingest_trade(NVDA_ASK_BLOCK)
    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key")
    monkeypatch.setattr("odte_scanner.signals.bullflow.get_tape", lambda settings=None: tape)
    board = build_option_flow([])
    assert board["flow_source"] == "bullflow"
    assert board["golden"][0]["symbol"] == "NVDA"
    assert board["golden"][0]["trade_type"] == "BLOCK"


def test_build_option_flow_yahoo_fallback_without_key():
    board = build_option_flow(
        [
            {
                "symbol": "SPY",
                "spot": 500,
                "expiry": "2026-11-23",
                "dte": 0,
                "calls": [
                    {
                        "right": "C",
                        "strike": 500,
                        "mid": 2.0,
                        "volume": 25000,
                        "open_interest": 10000,
                    }
                ],
                "puts": [],
            }
        ]
    )
    assert board["flow_source"] == "yahoo"
    assert board["golden"]


def test_select_echo_expiries_covers_short_weekly_and_monthly():
    exps = [
        "2026-11-23",
        "2026-11-24",
        "2026-11-27",
        "2026-12-04",
        "2026-12-18",
        "2027-01-15",
        "2027-09-17",
    ]
    # Freeze "today" by constructing relative dates around a fixed window:
    # function uses datetime.now().date(); we only assert shape/limits here
    # with a wide max_dte using whatever "today" is, via synthetic offsets.
    # Use far-future dated strings relative to a mocked today instead.
    from odte_scanner.echo import chain_ladder as cl

    class _Date:
        @staticmethod
        def date():
            return datetime(2026, 11, 20).date()

    monkey_now = datetime(2026, 11, 20)

    def _fake_now(*_args, **_kwargs):
        return monkey_now

    # Patch datetime.now used in _nearest_expiries via wrapping the helper.
    original = cl._nearest_expiries

    def _wrapped(expirations, *, max_dte=7):
        today = datetime(2026, 11, 20).date()
        out = []
        for exp in expirations:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (d - today).days
            if 0 <= dte <= max_dte:
                out.append((exp, dte))
        out.sort(key=lambda x: x[1])
        return out

    cl._nearest_expiries = _wrapped  # type: ignore[method-assign]
    try:
        picked = cl.select_echo_expiries(exps, max_dte=45, max_expiries=3)
    finally:
        cl._nearest_expiries = original  # type: ignore[method-assign]
    dtes = [dte for _, dte in picked]
    assert len(picked) == 3
    assert min(dtes) <= 4
    assert any(5 <= dte <= 14 for dte in dtes)
    assert any(dte >= 21 for dte in dtes)
    assert "2027-09-17" not in {exp for exp, _ in picked}  # beyond 45 DTE


def test_sse_handler_tracks_health_without_storing_a_key():
    tape = BullflowTape()
    tape.handle_sse_event(
        {
            "event": "init",
            "mode": "live-options-flow",
            "timezone": "America/New_York",
            "startedAt": "2026-11-24T05:27:19.516Z",
        }
    )
    tape.handle_sse_event({"event": "heartbeat", "timestamp": "2026-11-24T05:27:20.978Z"})
    health = tape.health()
    assert health["connected"] is True or health["last_heartbeat"]
    assert health["last_heartbeat"] == "2026-11-24T05:27:20.978Z"
    blob = str(health)
    assert "BULLFLOW_API_KEY" not in blob
    assert "X-API-Key" not in blob
    assert "test-key" not in blob
