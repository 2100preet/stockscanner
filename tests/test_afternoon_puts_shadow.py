"""Afternoon Puts 30-day shadow log: +150% full exit next to the real fills. No orders."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from odte_scanner import fast_exits
from odte_scanner.signals import afternoon_puts_shadow as shadow
from tests.session_clock import iso_day, session_et


@pytest.fixture(autouse=True)
def _fresh_poll_clock():
    shadow._last_poll.clear()
    yield
    shadow._last_poll.clear()


def _buy(oid="wb-b1", *, fill=2.00, at=None, contract=None, symbol="QQQ", desk="afternoon_puts"):
    at = at or session_et(12, 46)  # 11:46 CT
    expiry = iso_day(at)
    occ = contract or f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}P00655000"
    return SimpleNamespace(id=oid, action="BUY", desk=desk, status="filled", fill_price=fill, quantity=1,
                           symbol=symbol, contract=occ, expiry=expiry, strike=655.0, right="P",
                           created_at=at.isoformat(), updated_at=at.isoformat(),
                           meta={"golden_kind": desk, "signal_at_ct": "11:40"}, reason="")


def _sell(buy, *, fill, at, reason="stop loss -41% ≤ −40%"):
    return SimpleNamespace(id="wb-s-" + buy.id, action="SELL", desk="golden_exit", status="filled", fill_price=fill,
                           quantity=1, symbol=buy.symbol, contract=buy.contract, expiry=buy.expiry, strike=buy.strike,
                           right="P", created_at=at.isoformat(), updated_at=at.isoformat(),
                           meta={"closes_order_id": buy.id, "filled_qty": 1.0}, reason=reason)


class Quotes:
    def __init__(self, *bids):
        self.bids = list(bids)
        self.calls = 0

    def __call__(self, row):
        self.calls += 1
        bid = self.bids.pop(0) if self.bids else None
        return None if bid is None else {"bid": bid, "ask": bid + 0.05, "source": "tradier"}


def test_target_hit_records_a_limit_fill_and_the_real_exit_beside_it(tmp_path: Path):
    path = tmp_path / "s.json"
    buy = _buy()
    q = Quotes(2.40, 4.10, 5.05)
    for i in range(3):
        shadow.tick([buy], session_et(13, 0) + timedelta(minutes=i), quote=q, path=path, force_poll=True)
    sold = _sell(buy, fill=8.30, at=session_et(14, 44), reason="trail gave back half the peak gain — sell 1")
    shadow.tick([buy, sold], session_et(14, 45), quote=Quotes(), path=path, force_poll=True)
    out = shadow.summary(now=session_et(14, 46), path=path)
    row = out["rows"][0]
    assert row["shadow_target"] == 5.00 and row["shadow_done"] is True
    assert row["shadow_exit"] == 5.00 and row["shadow_pnl_usd"] == 300.0
    assert row["shadow_exit_why"].startswith("+150% target hit")
    assert row["real_exit"]["fill"] == 8.30 and row["real_pnl_usd"] == 630.0
    assert out["totals"]["hit_150"] == 1 and out["orders"].startswith("none")


def test_after_noon_tag_and_buckets(tmp_path: Path):
    path = tmp_path / "s.json"
    early = _buy("wb-early", at=session_et(12, 46))  # 11:46 CT
    late = _buy("wb-late", at=session_et(13, 5), symbol="SPY")  # 12:05 CT
    shadow.tick([early, late], session_et(13, 10), quote=Quotes(2.1, 2.1), path=path, force_poll=True)
    out = shadow.summary(now=session_et(13, 11), path=path)
    tags = {row["id"]: row["after_noon"] for row in out["rows"]}
    assert tags == {"wb-early": False, "wb-late": True}
    assert out["after_noon"]["entries"] == 1 and out["before_noon"]["entries"] == 1


def test_stop_then_flat_and_no_target(tmp_path: Path):
    path = tmp_path / "s.json"
    buy = _buy()
    shadow.tick([buy], session_et(13, 30), quote=Quotes(1.10), path=path, force_poll=True)
    row = shadow.summary(now=session_et(13, 31), path=path)["rows"][0]
    assert row["shadow_exit_why"].startswith("−40% stop") and row["shadow_pnl_usd"] == -90.0

    path2 = tmp_path / "s2.json"
    buy2 = _buy("wb-b2")
    shadow.tick([buy2], session_et(14, 0), quote=Quotes(3.00), path=path2, force_poll=True)
    shadow.tick([buy2], session_et(15, 45), quote=Quotes(2.50), path=path2)  # 2:45 PM CT
    row2 = shadow.summary(now=session_et(15, 46), path=path2)["rows"][0]
    assert row2["shadow_exit_why"].startswith("flat 2:45 PM CT") and row2["shadow_exit"] == 2.50
    assert row2["touched_150"] is False and row2["max_bid"] == 3.00


def test_polls_are_throttled(tmp_path: Path):
    path = tmp_path / "s.json"
    buy = _buy()
    q = Quotes(2.1, 2.2, 2.3)
    shadow.tick([buy], session_et(13, 0), quote=q, path=path)
    shadow.tick([buy], session_et(13, 0) + timedelta(seconds=7), quote=q, path=path)
    shadow.tick([buy], session_et(13, 0) + timedelta(seconds=25), quote=q, path=path)
    assert q.calls == 2


def test_kill_switch_stops_everything(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AFTERNOON_PUTS_SHADOW_OFF", "1")
    q = Quotes(2.0)
    out = shadow.tick([_buy()], session_et(13, 0), quote=q, path=tmp_path / "s.json", force_poll=True)
    assert out == {"skipped": "AFTERNOON_PUTS_SHADOW_OFF"} and q.calls == 0
    assert not (tmp_path / "s.json").exists()


def test_auto_stops_after_30_days(tmp_path: Path, monkeypatch):
    path = tmp_path / "s.json"
    start = session_et(13, 0)
    shadow.tick([_buy()], start, quote=Quotes(2.1), path=path, force_poll=True)
    later = start + timedelta(days=31)
    q = Quotes(2.0)
    out = shadow.tick([_buy("wb-new", at=later)], later, quote=q, path=path, force_poll=True)
    assert out["skipped"] == "shadow window over" and q.calls == 0
    summary = shadow.summary(now=later, path=path, days=60)
    assert summary["active"] is False and summary["expired"] is True
    assert [row["id"] for row in summary["rows"]] == ["wb-b1"]


def test_ignores_other_desks_and_unfilled(tmp_path: Path):
    path = tmp_path / "s.json"
    sniper = _buy("wb-sn", desk="sniper")
    working = _buy("wb-w")
    working.status = "submitted"
    out = shadow.tick([sniper, working], session_et(13, 0), quote=Quotes(2.0), path=path, force_poll=True)
    assert out["rows"] == 0


def test_shadow_tick_never_places_orders(tmp_path: Path, monkeypatch):
    calls = []
    broker = SimpleNamespace(orders=[_buy()], submit=lambda *a, **k: calls.append(a),
                             stage=lambda *a, **k: calls.append(a))
    monkeypatch.setattr("odte_scanner.options.contract_quote.fetch_contract_quote", Quotes(2.2))
    out = fast_exits.run_shadow_tick(SimpleNamespace(broker=broker), now=session_et(13, 0))
    assert out.get("ok") is True and calls == []
    assert fast_exits.run_shadow_tick(SimpleNamespace(broker=broker), now=session_et(17, 0)) == {
        "skipped": "session closed"
    }


def test_missed_close_uses_last_quote_next_day(tmp_path: Path):
    path = tmp_path / "s.json"
    buy = _buy()
    shadow.tick([buy], session_et(13, 0), quote=Quotes(2.6), path=path, force_poll=True)
    shadow.tick([buy], session_et(13, 0) + timedelta(days=1), quote=Quotes(), path=path)
    row = shadow.summary(now=session_et(13, 1) + timedelta(days=1), path=path)["rows"][0]
    assert row["shadow_done"] and row["shadow_exit"] == 2.6 and "last quote" in row["shadow_exit_why"]


@pytest.fixture
def client():
    from odte_scanner.ui import create_app

    app = create_app()
    app.testing = True
    return app.test_client()


def test_endpoint_serves_the_log(tmp_path: Path, monkeypatch, client):
    shadow.tick([_buy()], session_et(13, 0), quote=Quotes(5.2), force_poll=True)
    body = client.get("/api/afternoon_puts_shadow").get_json()
    assert body["ok"] is True and body["shadow_only"] is True
    assert body["kill_switch"] == "AFTERNOON_PUTS_SHADOW_OFF=1"
    assert body["rows"] and body["rows"][0]["shadow_target"] == 5.0


def test_tile_gets_the_shadow_summary(tmp_path: Path, monkeypatch):
    from odte_scanner.signals import afternoon_puts_board as board_mod

    shadow.tick([_buy()], session_et(13, 0), quote=Quotes(2.4), force_poll=True)
    monkeypatch.setattr(board_mod, "build_afternoon_board", lambda bars, **k: {"ok": True, "signals": []})
    board = board_mod.load_afternoon_board(now=session_et(13, 1), bars={"SPY": [object()]})
    block = board["shadow"]
    assert block["active"] is True and block["totals"]["entries"] == 1
    assert list(block["today"].values())[0]["shadow_target"] == 5.0
