"""Sniper LIVE mirror: hard caps, kill switch, and isolation from paper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.trading.labels import activity_premium_label
from odte_scanner.trading.sniper_live import (
    ACCOUNT_ENV,
    DAILY_CAP,
    ENABLED_ENV,
    KEY_ENV,
    LIVE_QTY,
    LOSS_CAP_ENV,
    SECRET_ENV,
    SniperLiveBroker,
    build_sniper_live_broker,
    in_sniper_entry_window,
    max_daily_loss,
    mirror_paper_entry,
    portal_view,
    reset_singletons,
    resolve_sniper_live_flags,
)
from odte_scanner.trading.webull import (
    PRODUCTION_TRADE_HOST,
    SANDBOX_TRADE_HOST,
    WebullBroker,
    WebullOrderIntent,
)

CT = ZoneInfo("America/Chicago")
# 9:00 AM CT Wednesday Nov 18, 2026 — inside the Sniper window and the option session.
WINDOW_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=CT)
AFTER_WINDOW = datetime(2026, 11, 18, 11, 0, tzinfo=CT)
SATURDAY = datetime(2026, 11, 21, 9, 0, tzinfo=CT)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_singletons()
    monkeypatch.delenv(ENABLED_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.delenv(ACCOUNT_ENV, raising=False)
    monkeypatch.delenv(LOSS_CAP_ENV, raising=False)
    monkeypatch.delenv("WEBULL_LIVE", raising=False)
    yield
    reset_singletons()


def _live_env(monkeypatch, *, enabled=True, creds=True, loss=None):
    if enabled:
        monkeypatch.setenv(ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(ENABLED_ENV, raising=False)
    if creds:
        monkeypatch.setenv(KEY_ENV, "live-key")
        monkeypatch.setenv(SECRET_ENV, "live-secret")
        monkeypatch.setenv(ACCOUNT_ENV, "LIVE-ACCT")
    if loss is not None:
        monkeypatch.setenv(LOSS_CAP_ENV, str(loss))


def _paper_buy(
    tmp_path: Path,
    *,
    desk="sniper",
    symbol="QQQ",
    qty=2,
    status="submitted",
    contract="QQQ261118P00600000",
    oid="paper-1",
    limit=2.20,
    right="P",
    meta=None,
) -> WebullOrderIntent:
    intent = WebullOrderIntent(
        id=oid,
        desk=desk,
        action="BUY",
        symbol=symbol,
        right=right,
        contract=contract,
        expiry="2026-11-18",
        strike=600.0,
        quantity=qty,
        limit_price=limit,
        status=status,
        reason="test",
        created_at="2026-11-18T15:00:00+00:00",
        updated_at="2026-11-18T15:00:00+00:00",
        meta={"golden_kind": desk if desk in {"sniper", "diamond"} else desk, **(meta or {})},
    )
    return intent


def _armed_broker(tmp_path: Path, monkeypatch, *, now=WINDOW_NOW) -> SniperLiveBroker:
    _live_env(monkeypatch, enabled=True, creds=True)
    broker = build_sniper_live_broker(
        environ=__import__("os").environ,
        ledger_path=tmp_path / "sniper_live_orders.json",
        enabled=True,
        dry_run=False,
    )
    broker._session_now = now
    broker._place_via_openapi = lambda intent: "LIVE-OID-" + str(intent.id)
    return broker


def test_flag_off_by_default_means_no_live_orders(tmp_path, monkeypatch):
    _live_env(monkeypatch, enabled=False, creds=True)
    flags = resolve_sniper_live_flags()
    assert flags["enabled"] is False
    assert flags["ready"] is False
    broker = build_sniper_live_broker(
        environ=__import__("os").environ,
        ledger_path=tmp_path / "off.json",
        enabled=False,
        dry_run=True,
    )
    out = mirror_paper_entry(
        _paper_buy(tmp_path),
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert ENABLED_ENV in out["reason"] or "off" in out["reason"].lower()
    assert broker.orders == [] or all(o.status != "submitted" for o in broker.orders)


def test_missing_creds_sends_no_live_orders(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "1")
    flags = resolve_sniper_live_flags()
    assert flags["ready"] is False
    assert flags["missing"] == [KEY_ENV, SECRET_ENV, ACCOUNT_ENV]
    broker = build_sniper_live_broker(
        environ=__import__("os").environ,
        ledger_path=tmp_path / "live.json",
        enabled=True,
    )
    assert broker.enabled is False
    out = mirror_paper_entry(
        _paper_buy(tmp_path),
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "missing" in out["reason"] or "not ready" in out["reason"]
    assert all(o.status != "submitted" for o in broker.orders)


def test_webull_live_env_does_not_arm_sniper_live(monkeypatch):
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.setenv("WEBULL_APP_KEY", "paper-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "paper-secret")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "paper-acct")
    flags = resolve_sniper_live_flags()
    assert flags["enabled"] is False
    assert flags["ready"] is False
    assert flags["webull_live_env_ignored"] is True
    assert KEY_ENV in flags["missing"]


def test_quantity_is_always_one(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    paper = _paper_buy(tmp_path, qty=5)
    out = mirror_paper_entry(
        paper,
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is True
    assert out["quantity"] == LIVE_QTY == 1
    assert broker.orders[0].quantity == 1
    assert broker.orders[0].order_type == "LIMIT"
    assert broker.orders[0].meta["host"] == PRODUCTION_TRADE_HOST
    assert broker.orders[0].meta["live"] is True


def test_fourth_entry_is_blocked(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    state = tmp_path / "day.json"
    for i in range(DAILY_CAP):
        paper = _paper_buy(
            tmp_path,
            oid=f"paper-{i}",
            contract=f"QQQ261118P0060000{i}",
            symbol="QQQ",
        )
        out = mirror_paper_entry(
            paper,
            now=WINDOW_NOW,
            environ=__import__("os").environ,
            broker=broker,
            state_path=state,
            send_alerts=False,
        )
        assert out["ok"] is True, out
    fourth = _paper_buy(tmp_path, oid="paper-3", contract="SPY261118C00600000", symbol="SPY")
    out = mirror_paper_entry(
        fourth,
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=state,
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "max 3" in out["reason"]
    submitted = [o for o in broker.orders if o.status == "submitted"]
    assert len(submitted) == 3


@pytest.mark.parametrize("desk", ["diamond", "kotegawa", "million", "afternoon_puts", "sure_winner", "golden_flow"])
def test_other_desks_cannot_route_live(tmp_path, monkeypatch, desk):
    broker = _armed_broker(tmp_path, monkeypatch)
    paper = _paper_buy(tmp_path, desk=desk, meta={"golden_kind": desk})
    out = mirror_paper_entry(
        paper,
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "Sniper" in out["reason"] or "not a Sniper" in out["reason"]
    # Direct stage/submit also refuses a foreign desk.
    intent = broker.stage(
        desk=desk,
        action="BUY",
        symbol="SPY",
        right="C",
        contract="SPY261118C00600000",
        expiry="2026-11-18",
        strike=600.0,
        quantity=1,
        limit_price=1.50,
        meta={"golden_kind": desk},
    )
    intent.desk = desk
    intent.meta["golden_kind"] = desk
    submitted = broker.submit(intent)
    assert submitted.status == "skipped"
    assert "Sniper LIVE only accepts the Sniper desk" in (submitted.error or "")


def test_spx_is_never_routed_live(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    paper = _paper_buy(tmp_path, symbol="SPX", contract="SPX261118P06800000")
    out = mirror_paper_entry(
        paper,
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "SPX" in out["reason"]
    assert all(o.status != "submitted" for o in broker.orders)


def test_loss_cap_blocks_new_live_entries(tmp_path, monkeypatch):
    _live_env(monkeypatch, enabled=True, creds=True, loss=-200)
    assert max_daily_loss() == -200.0
    broker = _armed_broker(tmp_path, monkeypatch)
    # A closed live round trip at -$200 (entry $3.00, exit $1.00, 1 contract).
    buy = broker.stage(
        desk="sniper",
        action="BUY",
        symbol="QQQ",
        right="P",
        contract="QQQ261118P00600000",
        expiry="2026-11-18",
        strike=600.0,
        quantity=1,
        limit_price=3.00,
        meta={"golden_kind": "sniper", "live": True},
    )
    buy.status = "filled"
    buy.fill_price = 3.00
    buy.updated_at = "2026-11-18T15:10:00+00:00"
    sell = broker.stage(
        desk="sniper",
        action="SELL",
        symbol="QQQ",
        right="P",
        contract="QQQ261118P00600000",
        expiry="2026-11-18",
        strike=600.0,
        quantity=1,
        limit_price=1.00,
        meta={"golden_kind": "sniper", "live": True, "entry_price": 3.00},
    )
    sell.status = "filled"
    sell.fill_price = 1.00
    sell.updated_at = "2026-11-18T15:20:00+00:00"
    broker.save()
    paper = _paper_buy(tmp_path, oid="paper-new", contract="SPY261118P00600000", symbol="SPY")
    out = mirror_paper_entry(
        paper,
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "loss cap" in out["reason"]
    assert paper.symbol not in {o.symbol for o in broker.orders if o.status == "submitted"}


def test_outside_window_is_blocked(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch, now=AFTER_WINDOW)
    out = mirror_paper_entry(
        _paper_buy(tmp_path),
        now=AFTER_WINDOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=False,
    )
    assert out["ok"] is False
    assert "window" in out["reason"]
    assert in_sniper_entry_window(WINDOW_NOW) is True
    assert in_sniper_entry_window(AFTER_WINDOW) is False
    assert in_sniper_entry_window(SATURDAY) is False


def test_market_orders_are_refused(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    intent = broker.stage(
        desk="sniper",
        action="BUY",
        symbol="QQQ",
        right="P",
        contract="QQQ261118P00600000",
        expiry="2026-11-18",
        strike=600.0,
        quantity=1,
        limit_price=None,
        meta={"golden_kind": "sniper"},
    )
    intent.order_type = "MARKET"
    intent.limit_price = None
    submitted = broker.submit(intent)
    assert submitted.status == "skipped"
    assert "limit" in (submitted.error or "").lower()
    assert "MARKET" in (submitted.error or "")


def test_paper_broker_stays_sandbox_and_ready_live_is_false(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(KEY_ENV, "live-key")
    monkeypatch.setenv(SECRET_ENV, "live-secret")
    monkeypatch.setenv(ACCOUNT_ENV, "LIVE-ACCT")
    paper = WebullBroker(
        enabled=True,
        dry_run=False,
        sandbox=False,
        paper=False,
        account_id="PAPER-ACCT",
        app_key="paper-key",
        app_secret="paper-secret",
        ledger_path=tmp_path / "webull_orders.json",
    )
    assert paper.sandbox is True
    assert paper.paper is True
    assert paper.status()["host"] == SANDBOX_TRADE_HOST
    assert paper.status()["ready_live"] is False
    assert paper._api_host() == SANDBOX_TRADE_HOST
    with pytest.raises(RuntimeError, match="paper-sandbox"):
        # The paper object still refuses a production host even if flags ask for one.
        paper.sandbox = False
        paper.paper = False
        paper._assert_sandbox()


def test_live_broker_is_a_separate_object_and_ignores_paper_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "paper-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "paper-secret")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "PAPER-ACCT")
    _live_env(monkeypatch, enabled=True, creds=True)
    live = _armed_broker(tmp_path, monkeypatch)
    paper = WebullBroker(
        enabled=True,
        dry_run=False,
        account_id="PAPER-ACCT",
        app_key="paper-key",
        app_secret="paper-secret",
        ledger_path=tmp_path / "webull_orders.json",
    )
    assert live is not paper
    live._client, paper._client = object(), object()
    live._order_api, paper._order_api = object(), object()
    assert live._client is not paper._client
    assert live._order_api is not paper._order_api
    assert live.app_key == "live-key"
    assert live.app_key != paper.app_key
    assert live.account_id == "LIVE-ACCT"
    assert live.sandbox is False
    assert live.paper is False
    assert live._api_host() == PRODUCTION_TRADE_HOST
    assert live.path != paper.path


def test_live_premium_labels():
    working = {
        "status": "submitted",
        "limit_price": 2.20,
        "meta": {"live": True, "host": PRODUCTION_TRADE_HOST, "venue": "webull_live"},
    }
    filled = {
        "status": "filled",
        "fill_price": 2.22,
        "limit_price": 2.20,
        "meta": {"live": True, "host": PRODUCTION_TRADE_HOST, "venue": "webull_live"},
    }
    paper = {"status": "filled", "fill_price": 2.22, "limit_price": 2.20, "meta": {"host": SANDBOX_TRADE_HOST}}
    assert activity_premium_label(working) == "Limit $2.20 (working)"
    assert activity_premium_label(filled) == "Filled @ $2.22 (Webull LIVE)"
    assert activity_premium_label(paper) == "Filled @ $2.22"


def test_telegram_tag_and_portal_are_separate(tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        "odte_scanner.alerts.telegram.send_telegram_text",
        lambda body, **_k: sent.append(body) or {"ok": True},
    )
    broker = _armed_broker(tmp_path, monkeypatch)
    out = mirror_paper_entry(
        _paper_buy(tmp_path),
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        send_alerts=True,
    )
    assert out["ok"] is True
    assert sent and all(msg.startswith("[SNIPER LIVE]") for msg in sent)
    view = portal_view(
        now=WINDOW_NOW,
        environ=__import__("os").environ,
        broker=broker,
        state_path=tmp_path / "day.json",
        ledger_path=tmp_path / "sniper_live_orders.json",
    )
    assert view["title"] == "Sniper LIVE"
    assert view["real_money"] is True
    assert view["separate_from_paper"] is True
    assert view["qty"] == 1
    assert view["host"] == PRODUCTION_TRADE_HOST
    assert view["entries_today"] == 1
    assert "paper" in (view["note"] or "").lower() or view["status"] == "armed"


# --- Real-account isolation: the owner's own positions are never touched ---

FRIDAY_LATE = datetime(2026, 11, 20, 14, 40, tzinfo=CT)  # past the 2:30 PM CT 0DTE time-stop


def _wb_positions(*rows):
    out = []
    for sym, right, expiry, strike, qty, cost, last in rows:
        out.append(
            {
                "symbol": sym,
                "quantity": str(qty),
                "cost_price": str(cost),
                "last_price": str(last),
                "instrument_type": "OPTION",
                "option_strategy": "SINGLE",
                "legs": [
                    {
                        "symbol": sym,
                        "instrument_type": "OPTION",
                        "option_type": "CALL" if right == "C" else "PUT",
                        "option_expire_date": expiry,
                        "option_exercise_price": str(strike),
                        "last_price": str(last),
                        "cost": str(cost),
                    }
                ],
            }
        )
    return out


def _bot_fill(broker, contract="QQQ261120C00683000", oid="wb-live-1"):
    order = WebullOrderIntent(
        id=oid,
        desk="sniper",
        action="BUY",
        symbol="QQQ",
        right="C",
        contract=contract,
        expiry="2026-11-20",
        strike=683.0,
        quantity=1,
        limit_price=2.98,
        fill_price=2.98,
        status="filled",
        reason="test",
        created_at="2026-11-20T15:00:00+00:00",
        updated_at="2026-11-20T15:00:00+00:00",
        meta={"golden_kind": "sniper", "live": True, "venue": "webull_live", "fill_state": "filled"},
    )
    broker.orders.insert(0, order)
    broker.save()
    return order


def test_live_positions_hide_owner_manual_holdings(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    payload = _wb_positions(
        ("NFLX", "P", "2026-11-20", 56, 3, 0.03, 0.035),
        ("AAPL", "C", "2026-11-20", 360, 2, 0.68, 0.575),
        ("QQQ", "C", "2026-11-20", 683, 3, 2.98, 2.145),
    )
    broker._fetch_positions = lambda account_id=None: payload
    assert broker.list_option_positions() == []  # nothing the bot bought
    _bot_fill(broker)
    rows = broker.list_option_positions()
    assert [r["contract"] for r in rows] == ["QQQ261120C00683000"]
    assert rows[0]["quantity"] == 1  # owner's other 2 QQQ contracts are not the bot's
    assert rows[0]["owner_qty_hidden"] == 2


def test_live_trader_never_sells_owner_positions(tmp_path, monkeypatch):
    from odte_scanner.trading.sniper_live import get_live_trader

    broker = _armed_broker(tmp_path, monkeypatch, now=FRIDAY_LATE)
    sent = []

    def place(intent):
        sent.append((intent.action, intent.contract, intent.quantity))
        return "LIVE-OID-" + str(intent.id)

    broker._place_via_openapi = place
    # Deep losers, expiring today, past the 0DTE time-stop: on the paper desk
    # these would be sold. On the real account they belong to the owner.
    broker._fetch_positions = lambda account_id=None: _wb_positions(
        ("NFLX", "P", "2026-11-20", 56, 3, 0.90, 0.03),
        ("AAPL", "C", "2026-11-20", 360, 2, 3.00, 0.57),
        ("SPXW", "C", "2026-11-20", 7595, 2, 5.00, 1.12),
    )
    broker.order_history = lambda start, end, **kw: [
        {"client_order_id": "wb-foreign-1", "side": "BUY", "symbol": "AAPL", "option_type": "CALL",
         "strike": "360", "expiry": "2026-11-20", "quantity": "2", "status": "FILLED", "filled_price": "3.00"},
    ]
    trader = get_live_trader(broker=broker, force_new=True)
    trader.sync_exits(now=FRIDAY_LATE)
    assert sent == []
    assert all(o.id != "wb-foreign-1" for o in broker.orders)
    assert not [o for o in broker.orders if o.action == "SELL"]


def test_live_history_import_only_updates_own_orders(tmp_path, monkeypatch):
    broker = _armed_broker(tmp_path, monkeypatch)
    mine = _bot_fill(broker, oid="wb-mine")
    mine.status = "submitted"
    out = broker.import_history(
        [
            {"client_order_id": "wb-mine", "side": "BUY", "symbol": "QQQ", "status": "FILLED", "filled_price": "3.10"},
            {"client_order_id": "wb-other", "side": "BUY", "symbol": "AAPL", "status": "FILLED"},
        ]
    )
    assert out["ignored_foreign"] == 1
    assert {o.id for o in broker.orders} == {"wb-mine"}


def test_live_token_lives_on_volume_apart_from_paper(tmp_path, monkeypatch):
    from odte_scanner.trading import sniper_live as sl

    broker = _armed_broker(tmp_path, monkeypatch)
    monkeypatch.delenv(sl.TOKEN_DIR_ENV, raising=False)
    assert broker._token_dir() == str(sl.DEFAULT_TOKEN_DIR)
    assert "outputs" in Path(broker._token_dir()).parts
    paper = WebullBroker(ledger_path=tmp_path / "paper.json", enabled=False, dry_run=True)
    assert paper._token_dir() is None
    monkeypatch.setenv(sl.TOKEN_DIR_ENV, str(tmp_path / "tok"))
    assert broker._token_dir() == str(tmp_path / "tok")


def test_failed_live_token_check_is_not_retried_every_loop(tmp_path, monkeypatch):
    from odte_scanner.trading import sniper_live as sl

    broker = _armed_broker(tmp_path, monkeypatch)
    clock = [1000.0]
    broker._clock = lambda: clock[0]
    calls = []

    def boom(self):
        calls.append(1)
        raise RuntimeError("ERROR_INIT_TOKEN")

    monkeypatch.setattr(WebullBroker, "_build_trade_api", boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            broker._build_trade_api()
    assert len(calls) == 1
    clock[0] += sl.TOKEN_RETRY_SEC + 1
    with pytest.raises(RuntimeError):
        broker._build_trade_api()
    assert len(calls) == 2
