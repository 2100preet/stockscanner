"""Kotegawa and Million on the Webull PAPER account: real fills are the book.

Uses the real WebullBroker with a fake sandbox order API, so the whole path
(stage → desk-tagged client id → place → detail → reprice/cancel → book) is
exercised without the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.desks import webull_desk
from odte_scanner.desks.kotegawa import advance_webull as kot_advance
from odte_scanner.desks.kotegawa import run_kotegawa
from odte_scanner.desks.million import advance_webull as mil_advance
from odte_scanner.desks.million import run_million
from odte_scanner.desks.sp100 import sp100_universe
from odte_scanner.trading.risk_controls import daily_tally
from odte_scanner.trading.webull import WebullBroker, build_equity_place_order

CT = ZoneInfo("America/Chicago")
ROUTE_ENV = {"KOTEGAWA_ACCOUNT_ID": "ACCT-MARGIN", "MILLION_ACCOUNT_ID": "ACCT-MARGIN"}
LIVE = {"sandbox": True, "paper": True}


class FakeOrderApi:
    """Sandbox order API stand-in. ``fill`` decides what each order does."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.placed: list[tuple[str, str, dict]] = []
        self.cancelled: list[str] = []
        self.fills: dict[str, tuple[str, float | None, int | None]] = {}
        self.policy = None  # callable(body) -> ("FILLED"|"SUBMITTED"|"PARTIAL FILLED", price, qty)

    def _place(self, kind, account, orders):
        body = dict(orders[0])
        cid = body["client_order_id"]
        status, price, qty = (self.policy(body) if self.policy else ("SUBMITTED", None, 0))
        self.orders[cid] = {"body": body, "status": status, "price": price, "filled": qty or 0}
        self.placed.append((kind, account, body))
        return {"client_order_id": cid}

    def place_option(self, account, orders):
        return self._place("option", account, orders)

    def place_order(self, account, orders):
        return self._place("equity", account, orders)

    def get_order_detail(self, account, cid):
        row = self.orders[cid]
        return {
            "order_status": row["status"],
            "filled_quantity": str(row["filled"]),
            "total_quantity": row["body"]["quantity"],
            "avg_filled_price": None if row["price"] is None else str(row["price"]),
        }

    def _cancel(self, account, cid):
        row = self.orders[cid]
        if row["status"] == "FILLED":
            raise RuntimeError("order already filled")
        row["status"] = "CANCELLED"
        self.cancelled.append(cid)
        return {"ok": True}

    cancel_option = _cancel
    cancel_order = _cancel

    def fill(self, cid, price, qty=None):
        row = self.orders[cid]
        row["status"] = "FILLED" if qty is None or qty >= int(row["body"]["quantity"]) else "PARTIAL FILLED"
        row["price"] = price
        row["filled"] = int(row["body"]["quantity"]) if qty is None else qty


def _broker(tmp_path: Path, desk: str, now: datetime, api: FakeOrderApi, monkeypatch) -> WebullBroker:
    monkeypatch.setenv("WEBULL_LOCAL_RATE_LIMIT", "0")
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        account_id="ACCT-MARGIN",
        app_key="k",
        app_secret="s",
        ledger_path=tmp_path / f"{desk}_orders.json",
        submit_interval_sec=0,
    )
    broker._order_api = api
    broker._session_now = now
    return broker


def _book(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---- Million -------------------------------------------------------------------

TAPE = {
    "SPY": [
        {"ts": datetime(2026, 11, 16, 9, 35, tzinfo=CT), "high": 100.0, "low": 99.8, "close": 99.9, "volume": 100, "interval": "1m", "closed": True},
        {"ts": datetime(2026, 11, 16, 9, 36, tzinfo=CT), "high": 101.0, "low": 100.2, "close": 100.6, "volume": 100, "interval": "1m", "closed": True},
    ]
}
CHAIN = {
    "SPY": [
        {"right": "C", "strike": 100.0, "dte": 0, "expiry": "2026-11-16", "bid": 0.60, "ask": 0.70, "contract": "SPY261116C00100000"},
        {"right": "C", "strike": 100.0, "dte": 1, "expiry": "2026-11-17", "bid": 1.80, "ask": 2.00, "contract": "SPY261117C00100000"},
    ]
}


def _mil_section(path: Path) -> dict:
    return {
        "enabled": True,
        "paper_only": True,
        "starting_equity": 1_000,
        "state_path": str(path),
        "signals": ["vwap_reclaim"],
    }


def _seed_simulated_million(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "desk": "million_challenge",
                "starting_equity": 1000,
                "cash": 1108.25,
                "day": "2026-11-13",
                "day_start_equity": 1000,
                "trades": [
                    {
                        "id": "mil-QQQ-2026-11-17-9045f873",
                        "symbol": "QQQ",
                        "contract": "QQQ261117C00657000",
                        "contracts": 1,
                        "entry": 1.986,
                        "exit": 3.068,
                        "cost": 198.6,
                        "pnl": 108.25,
                        "status": "closed",
                        "route": "internal",
                        "exit_session": "2026-11-13",
                        "exit_reason": "take_profit",
                    }
                ],
                "log_only": [],
            }
        )
    )


def test_million_routes_to_webull_and_books_only_the_real_fill(tmp_path: Path, monkeypatch):
    path = tmp_path / "million.json"
    _seed_simulated_million(path)
    api = FakeOrderApi()
    now = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
    broker = _broker(tmp_path, "million", now, api, monkeypatch)
    live_quote = {"bid": 1.20, "ask": 1.30}  # the chain above is a stale 0.60/0.70

    view = run_million(
        _mil_section(path), chains=CHAIN, intraday=TAPE, now=now, path=path,
        live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda t: live_quote,
    )
    book = _book(path)
    # Simulated history archived and labelled; the Webull book starts at $1,000.
    assert book["era"] == "webull"
    assert book["simulated"]["trades"][0]["pnl"] == pytest.approx(108.25)
    assert "Not achievable" in book["simulated"]["trades"][0]["audit"]
    assert view["simulated"]["label"].startswith("Simulated")
    from odte_scanner.desks.portal_feed import _card
    card = _card(view, label="MILLION", name="Million", desk="million", doublings=True)
    assert card["fills"] == "webull" and card["working"] and card["working"][0]["order_label"].endswith("(working)")
    assert card["simulated"]["trades"][0]["pnl"] == pytest.approx(108.25)
    trade = book["trades"][0]
    assert trade["status"] == "pending" and trade["route"] == "webull"
    kind, account, body = api.placed[0]
    assert kind == "option" and account == "ACCT-MARGIN"
    assert body["client_order_id"].startswith("mil-")
    assert body["side"] == "BUY" and float(body["limit_price"]) == pytest.approx(1.25)
    assert book["cash"] == pytest.approx(1000)  # nothing booked before a fill
    assert "Limit $1.25 (working)" in view["cycle_alerts"][-1]["message"]
    assert view["working"][0]["order_label"] == "Limit $1.25 (working)"

    # 20 s later still working: walk up to the live ask.
    later = datetime(2026, 11, 16, 9, 40, 20, tzinfo=CT)
    broker._session_now = later
    mil_advance(_mil_section(path), live_trading=LIVE, now=later, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda t: live_quote)
    assert api.cancelled == [body["client_order_id"]]
    second = api.placed[1][2]
    assert float(second["limit_price"]) == pytest.approx(1.30)

    # Webull fills it at 1.29: the book records 1.29, not a mid.
    api.fill(second["client_order_id"], 1.29)
    filled_at = datetime(2026, 11, 16, 9, 40, 30, tzinfo=CT)
    mil_advance(_mil_section(path), live_trading=LIVE, now=filled_at, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda t: live_quote)
    book = _book(path)
    trade = book["trades"][0]
    assert trade["status"] == "open"
    assert trade["entry"] == pytest.approx(1.29)
    assert book["cash"] == pytest.approx(1000 - 129)
    assert any("Filled @ $1.29 (Webull paper)" in a["message"] for a in book["recent_alerts"])

    # The main AutoTrader must not see this lot on the shared account.
    held = webull_desk.isolated_holdings({"million": path})
    assert held == {"SPY261116C00100000": 1.0}
    rows = webull_desk.subtract_isolated(
        [{"contract": "SPY261116C00100000", "quantity": 3.0, "account_id": "ACCT-MARGIN"}], held, "ACCT-MARGIN"
    )
    assert rows[0]["quantity"] == 2.0

    # +50% on the live quote → Webull sell starts at the mid, then walks to the bid.
    up = {"bid": 1.95, "ask": 2.05}
    tp_at = datetime(2026, 11, 16, 9, 45, tzinfo=CT)
    broker._session_now = tp_at
    run_million(_mil_section(path), quotes={"SPY261116C00100000": up}, now=tp_at, path=path,
                live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda t: up)
    sell = api.placed[-1][2]
    assert sell["side"] == "SELL" and float(sell["limit_price"]) == pytest.approx(2.00)
    assert _book(path)["trades"][0]["status"] == "closing"
    walk = datetime(2026, 11, 16, 9, 45, 20, tzinfo=CT)
    broker._session_now = walk
    mil_advance(_mil_section(path), live_trading=LIVE, now=walk, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda t: up)
    at_bid = api.placed[-1][2]
    assert float(at_bid["limit_price"]) == pytest.approx(1.95)
    api.fill(at_bid["client_order_id"], 1.96)
    done = datetime(2026, 11, 16, 9, 45, 30, tzinfo=CT)
    mil_advance(_mil_section(path), live_trading=LIVE, now=done, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda t: up)
    book = _book(path)
    trade = book["trades"][0]
    assert trade["status"] == "closed"
    assert trade["exit"] == pytest.approx(1.96)
    assert trade["pnl"] == pytest.approx(196 - 129)
    assert book["cash"] == pytest.approx(1000 - 129 + 196)

    # The main desks' daily tally (−$800 account limit) never sees Million.
    main = WebullBroker(ledger_path=tmp_path / "main_orders.json")
    assert daily_tally(main.orders, now=done)["account_pnl"] == 0
    desk_ledger = (tmp_path / "million_orders.json").read_text()
    assert '"isolated_desk": "million"' in desk_ledger
    main_path = tmp_path / "main_orders.json"
    assert not main_path.exists() or "mil-" not in main_path.read_text()


def test_million_unfilled_buy_is_cancelled_with_a_not_filled_alert(tmp_path: Path, monkeypatch):
    path = tmp_path / "million.json"
    api = FakeOrderApi()
    now = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
    broker = _broker(tmp_path, "million", now, api, monkeypatch)
    quote = {"bid": 1.20, "ask": 1.30}
    run_million(_mil_section(path), chains=CHAIN, intraday=TAPE, now=now, path=path,
                live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda t: quote)
    late = datetime(2026, 11, 16, 9, 41, 40, tzinfo=CT)  # 100 s > 90 s chase
    broker._session_now = late
    mil_advance(_mil_section(path), live_trading=LIVE, now=late, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda t: quote)
    book = _book(path)
    assert book["trades"][0]["status"] == "not_filled"
    assert book["cash"] == pytest.approx(1000)
    assert any("NOT FILLED" in a["message"] for a in book["recent_alerts"])
    # No second entry that session.
    again = datetime(2026, 11, 16, 9, 50, tzinfo=CT)
    broker._session_now = again
    run_million(_mil_section(path), chains=CHAIN, intraday=TAPE, now=again, path=path,
                live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda t: quote)
    assert len(_book(path)["trades"]) == 1


def test_million_without_a_live_quote_does_not_route_a_stale_chain_price(tmp_path: Path, monkeypatch):
    path = tmp_path / "million.json"
    api = FakeOrderApi()
    now = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
    broker = _broker(tmp_path, "million", now, api, monkeypatch)
    run_million(_mil_section(path), chains=CHAIN, intraday=TAPE, now=now, path=path,
                live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda t: None)
    assert api.placed == []
    assert _book(path)["trades"] == []


def test_million_stays_internal_without_paper_submit(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WEBULL_PAPER_SUBMIT", raising=False)
    path = tmp_path / "million.json"
    view = run_million(_mil_section(path), chains=CHAIN, intraday=TAPE, now=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
                       path=path, live_trading=LIVE, environ=ROUTE_ENV)
    assert view["route"] == "internal"
    assert _book(path)["route_reason"] == "Webull paper submit is off"


# ---- Kotegawa ------------------------------------------------------------------


def _names(n: int) -> list[str]:
    return list(sp100_universe())[:n]


def _dropped(close: float, *, day: str, n: int = 30) -> list[dict]:
    bars = [{"date": f"2026-10-{i+1:02d}", "open": 100, "high": 100, "low": 100, "close": 100} for i in range(n - 1)]
    bars.append({"date": day, "open": close, "high": close, "low": close, "close": close})
    return bars


def _kot_section(path: Path) -> dict:
    return {"enabled": True, "paper_only": True, "starting_equity": 100_000, "state_path": str(path)}


def test_equity_body_is_a_core_session_limit():
    from odte_scanner.trading.webull import WebullOrderIntent

    body = build_equity_place_order(
        WebullOrderIntent(id="kot-abc", desk="kotegawa", action="BUY", symbol="nvda", right="C", contract=None,
                          expiry=None, strike=None, quantity=128, limit_price=155.531, meta={"instrument": "EQUITY"})
    )
    assert body["instrument_type"] == "EQUITY"
    assert body["support_trading_session"] == "CORE"
    assert body["order_type"] == "LIMIT" and body["limit_price"] == "155.53"
    assert body["quantity"] == "128" and body["symbol"] == "NVDA"


def test_kotegawa_legacy_positions_finish_simulated_then_new_entries_go_to_webull(tmp_path: Path, monkeypatch):
    path = tmp_path / "kot.json"
    old, new = _names(2)
    path.write_text(json.dumps({
        "desk": "kotegawa", "starting_equity": 100000, "cash": 80000.0,
        "trades": [{"id": "kot-old", "symbol": old, "shares": 200, "entry": 100.0, "cost": 20000.0,
                    "entry_at": "2026-11-16T14:51:00-06:00", "entry_session": "2026-11-16", "status": "open",
                    "route": "internal"}],
    }))
    api = FakeOrderApi()
    wed = datetime(2026, 11, 18, 14, 50, tzinfo=CT)  # day 3 of the Monday entry
    broker = _broker(tmp_path, "kotegawa", wed, api, monkeypatch)
    quotes = {old: {"last": 89.0, "bid": 88.99, "ask": 89.01}, new: {"last": 80.0, "bid": 79.98, "ask": 80.02}}
    bars = {old: _dropped(89.0, day="2026-11-18"), new: _dropped(80.0, day="2026-11-18")}

    view = run_kotegawa(_kot_section(path), bars=bars, quotes=quotes, now=wed, path=path,
                        live_trading=LIVE, environ=ROUTE_ENV, broker=broker, quote_fetcher=lambda s: {})
    book = _book(path)
    # The simulated position closed on the internal ledger at 14:50 and was archived.
    assert book["era"] == "webull"
    sim = book["simulated"]["trades"][0]
    assert sim["symbol"] == old and sim["status"] == "closed" and sim["exit_reason"] == "time_exit"
    assert view["simulated"]["label"].startswith("Simulated")
    # New entries are Webull stock orders on the margin paper account, sized off $100,000.
    equity_orders = [b for kind, acct, b in api.placed if kind == "equity"]
    assert equity_orders and all(b["client_order_id"].startswith("kot-") for b in equity_orders)
    by_symbol = {b["symbol"]: b for b in equity_orders}
    assert set(by_symbol) == {old, new}
    assert by_symbol[new]["side"] == "BUY"
    assert float(by_symbol[new]["limit_price"]) == pytest.approx(80.00)
    assert int(by_symbol[new]["quantity"]) == int(20000 // 80.02)
    assert book["cash"] == pytest.approx(100000)

    api.fill(by_symbol[new]["client_order_id"], 80.01)
    api.fill(by_symbol[old]["client_order_id"], 89.0, qty=100)  # partial
    t2 = datetime(2026, 11, 18, 14, 50, 20, tzinfo=CT)
    broker._session_now = t2
    kot_advance(_kot_section(path), live_trading=LIVE, now=t2, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda s: {k: quotes[k] for k in s})
    book = _book(path)
    rows = {t["symbol"]: t for t in book["trades"]}
    assert rows[new]["status"] == "open" and rows[new]["entry"] == pytest.approx(80.01)
    # Partial fill on the other name: the rest keeps working at the live ask.
    assert rows[old]["status"] == "pending"
    t3 = datetime(2026, 11, 18, 14, 59, tzinfo=CT)  # entry deadline
    broker._session_now = t3
    kot_advance(_kot_section(path), live_trading=LIVE, now=t3, environ=ROUTE_ENV, broker=broker,
                quote_fetcher=lambda s: {k: quotes[k] for k in s})
    rows = {t["symbol"]: t for t in _book(path)["trades"]}
    assert rows[old]["status"] == "open" and rows[old]["shares"] == 100
    shares_new = int(rows[new]["shares"])
    assert _book(path)["cash"] == pytest.approx(100000 - shares_new * 80.01 - 100 * 89.0)


def test_routing_only_ever_uses_the_desk_account_and_the_sandbox(tmp_path: Path):
    assert webull_desk.desk_broker("million", {"mode": "internal"}) is None
    assert webull_desk.desk_broker(
        "million", {"mode": "sandbox", "account_id": "X", "host": "api.webull.com"}
    ) is None
    seen = {}
    webull_desk.desk_broker(
        "kotegawa",
        {"mode": "sandbox", "account_id": "ACCT", "host": "api.sandbox.webull.com"},
        factory=lambda **kw: seen.update(kw),
    )
    assert seen["account_id"] == "ACCT"
    assert str(seen["ledger_path"]).endswith("kotegawa_webull_orders.json")
