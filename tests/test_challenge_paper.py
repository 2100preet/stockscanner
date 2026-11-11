"""Challenge Webull paper is opt-in, sandbox-only, and capped at 1–2 contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401
from odte_scanner.alerts.exits import (
    collect_exit_alerts,
    format_challenge_entry_alert,
    format_exit_alert,
    plain_exit_reason,
)
from odte_scanner.trading import risk_controls as rc
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.challenge_paper import (
    apply_challenge_paper_board,
    challenge_paper_enabled,
    order_row_from_open_trade,
    pace_months_for_deadline,
)
from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent
from tests.session_clock import iso_day, session_et


def _entry(symbol: str = "JPM", ask: float = 0.50, expiry: str = "2026-10-16") -> dict:
    yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return {
        "symbol": symbol,
        "right": "C",
        "action": "ENTRY",
        "ask": ask,
        "bid": ask - 0.02,
        "contract": f"{symbol}{yymmdd}C00345000",
        "expiry": expiry,
        "strike": 345,
    }


def _broker(tmp_path: Path, *, account_id_2: str | None = "cash-2") -> WebullBroker:
    return WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        account_id_2=account_id_2,
        submit_interval_sec=0,
    )


def test_config_ships_paper_off_and_no_deadline():
    from odte_scanner.config import load_config

    cfg = load_config()
    assert cfg["live_trading"]["challenge_paper"] is False
    assert not cfg["actions"]["challenge_deadline"]


def test_flag_defaults_off_and_env_wins(monkeypatch):
    monkeypatch.delenv("CHALLENGE_PAPER", raising=False)
    assert challenge_paper_enabled({}) is False
    assert challenge_paper_enabled({"challenge_paper": False}) is False
    assert challenge_paper_enabled({"challenge_paper": True}) is True
    monkeypatch.setenv("CHALLENGE_PAPER", "1")
    assert challenge_paper_enabled({"challenge_paper": False}) is True
    monkeypatch.setenv("CHALLENGE_PAPER", "0")
    assert challenge_paper_enabled({"challenge_paper": True}) is False
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("CHALLENGE_PAPER", raising=False)
    assert challenge_paper_enabled({}) is False


def test_default_off_still_routes_gold_and_skips_challenge(tmp_path: Path):
    broker = _broker(tmp_path)
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge")
    out = trader.sync(
        sure_winners={
            "winners": [
                {
                    "symbol": "DOCU",
                    "right": "C",
                    "sure_winner": True,
                    "confidence_tier": "HIGH",
                    "ask": 1.1,
                    "bid": 1.0,
                    "contract": "DOCU261016C00076000",
                    "expiry": "2026-10-16",
                    "strike": 76,
                }
            ]
        },
        challenge={"entry": [_entry()], "exit": [], "tickets": []},
    )
    symbols = {row["symbol"]: row for row in out["submitted"]}
    assert "DOCU" in symbols
    assert symbols["DOCU"]["desk"] == "sure_winner"
    assert "JPM" not in symbols
    assert any("challenge paper is off" in row["reason"] for row in out["filtered"])


def test_flag_on_routes_challenge_to_account_2_and_alerts(tmp_path: Path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        "odte_scanner.alerts.telegram.send_telegram_text",
        lambda text, **_k: sent.append(text) or {"ok": True},
    )
    broker = _broker(tmp_path)
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge", challenge_paper=True)
    out = trader.sync(challenge={"entry": [_entry()], "exit": [], "tickets": []})
    assert out["submitted_n"] == 1
    order = out["submitted"][0]
    assert order["desk"] == "challenge"
    assert order["meta"]["account_id"] == "cash-2"
    assert order["meta"].get("account_id") != "margin-1"
    assert any(line.startswith("🏁 CHALLENGE ENTRY") and "paper sandbox" in line for line in sent)
    assert "WEBULL" not in sent[0]


def test_paper_orders_are_the_journal_opens_not_display_entries(tmp_path: Path):
    broker = _broker(tmp_path)
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge", challenge_paper=True)
    display = _entry("QQQ", ask=1.2, expiry="2026-11-06")
    journal = _entry("SPY", ask=1.4, expiry="2026-11-06")
    journal["from_challenge_journal"] = True
    out = trader.sync(
        challenge={
            "entry": [display],
            "tickets": [{**display, "action": "ENTRY"}],
            "exit": [],
            "paper_orders": [journal],
        }
    )
    symbols = [row["symbol"] for row in out["submitted"] if row["action"] == "BUY"]
    assert symbols == ["SPY"]


def test_refuses_a_broker_that_is_not_paper_sandbox(tmp_path: Path):
    broker = _broker(tmp_path)
    broker.paper = False
    broker.sandbox = False
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge", challenge_paper=True)
    out = trader.sync(challenge={"entry": [_entry()], "exit": [], "tickets": []})
    assert out["submitted_n"] == 0
    assert any("paper sandbox only" in row["reason"] for row in out["filtered"])


def test_quantity_is_at_most_two_and_one_on_same_day(tmp_path: Path):
    now = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path)
    trader = AutoTrader(
        broker,
        golden_only=True,
        second_desk="challenge",
        challenge_paper=True,
        max_risk_per_trade_usd=250,
        challenge_max_contracts=5,
    )
    assert trader.challenge_max_contracts == 2
    weekly_out = trader.sync(
        challenge={"entry": [_entry("BABA", ask=0.50, expiry=weekly)], "exit": [], "tickets": []},
        now=now,
    )
    assert weekly_out["submitted"][0]["quantity"] == 2
    same_day = trader.sync(
        challenge={"entry": [_entry("IWM", ask=0.40, expiry=iso_day(now))], "exit": [], "tickets": []},
        now=now,
    )
    iwm = next(row for row in same_day["submitted"] if row["symbol"] == "IWM")
    assert iwm["quantity"] == 1


def test_journal_size_is_used_and_still_capped(tmp_path: Path):
    now = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path)
    trader = AutoTrader(
        broker,
        golden_only=True,
        second_desk="challenge",
        challenge_paper=True,
        max_risk_per_trade_usd=250,
    )
    row = _entry("NVDA", ask=1.20, expiry=weekly)
    row["contracts_for_bankroll"] = 2
    out = trader.sync(challenge={"entry": [row], "exit": [], "tickets": []}, now=now)
    assert out["submitted"][0]["quantity"] == 2
    fat = _entry("AMD", ask=1.20, expiry=weekly)
    fat["contracts_for_bankroll"] = 6
    capped = trader.sync(challenge={"entry": [fat], "exit": [], "tickets": []}, now=now)
    amd = next(row for row in capped["submitted"] if row["symbol"] == "AMD")
    assert amd["quantity"] == 2


def test_kill_switch_and_desk_loss_block_challenge_entries_only(tmp_path: Path, monkeypatch):
    now = session_et(11, 0)
    broker = _broker(tmp_path)
    expiry = iso_day(now)
    contract = f"SPY{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00600000"
    buy = broker.stage(
        desk="challenge",
        action="BUY",
        symbol="SPY",
        contract=contract,
        expiry=expiry,
        strike=600,
        quantity=1,
        limit_price=5.0,
        meta={"account_id": "cash-2"},
    )
    buy.status, buy.fill_price = "filled", 5.0
    sell = broker.stage(
        desk="challenge",
        action="SELL",
        symbol="SPY",
        contract=contract,
        expiry=expiry,
        strike=600,
        quantity=1,
        limit_price=0.90,
        meta={"closes_order_id": buy.id, "entry_price": 5.0, "exit_tier": "challenge"},
    )
    sell.status, sell.fill_price = "filled", 0.90
    sell.updated_at = (now - timedelta(minutes=20)).astimezone(timezone.utc).isoformat()
    broker.save()
    block = rc.entry_block("challenge", orders=broker.orders, now=now)
    assert block and "Challenge" in block["reason"]
    assert rc.entry_block("sniper", orders=broker.orders, now=now) is None

    trader = AutoTrader(broker, golden_only=True, second_desk="challenge", challenge_paper=True)
    monkeypatch.setenv("PAPER_ENTRY_KILL", "1")
    skipped = trader.sync(
        challenge={"entry": [_entry("QQQ", expiry=expiry)], "exit": [], "tickets": []},
        sure_winners={
            "winners": [
                {
                    "symbol": "DOCU",
                    "right": "C",
                    "sure_winner": True,
                    "confidence_tier": "HIGH",
                    "ask": 1.1,
                    "bid": 1.0,
                    "contract": "DOCU261016C00076000",
                    "expiry": expiry,
                    "strike": 76,
                }
            ]
        },
        now=now,
    )
    bought = {row["symbol"] for row in skipped["submitted"] if row["action"] == "BUY"}
    assert "QQQ" not in bought
    assert "DOCU" not in bought
    assert any("kill switch" in str(row.get("paper_entry_skipped") or row.get("reason") or "") or "kill switch" in row["reason"] for row in skipped["filtered"])


def test_challenge_exit_alert_uses_the_paper_line():
    assert plain_exit_reason("challenge 0–1DTE time-stop — flatten by 15:45 ET", tier="challenge") == "15:45 ET flat"
    assert "Stop" not in plain_exit_reason("challenge 0–1DTE time-stop — flatten by 15:45 ET", tier="challenge")
    now = datetime(2026, 11, 6, 19, 50, tzinfo=timezone.utc)
    buy = WebullOrderIntent(
        id="buy-ch",
        desk="challenge",
        action="BUY",
        symbol="SPY",
        right="C",
        contract="SPY261106C00739000",
        expiry="2026-11-06",
        strike=739,
        quantity=1,
        limit_price=2.10,
        fill_price=2.10,
        status="filled",
        created_at="2026-11-06T14:35:00+00:00",
        updated_at="2026-11-06T14:36:00+00:00",
        meta={"account_id": "cash-2"},
    )
    sell = WebullOrderIntent(
        id="sell-ch",
        desk="challenge",
        action="SELL",
        symbol="SPY",
        right="C",
        contract="SPY261106C00739000",
        expiry="2026-11-06",
        strike=739,
        quantity=1,
        limit_price=1.05,
        fill_price=1.05,
        reason="stop loss -50% ≤ −50%",
        status="filled",
        created_at="2026-11-06T19:50:00+00:00",
        updated_at="2026-11-06T19:50:00+00:00",
        meta={"exit_tier": "challenge", "entry_price": 2.10, "entry_at": "2026-11-06T14:35:00+00:00", "closes_order_id": "buy-ch"},
    )
    alerts = collect_exit_alerts([buy, sell], now=now, seen=set())
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "challenge"
    assert alerts[0]["message"].startswith("🏁 CHALLENGE EXIT — SPY")
    assert "paper" not in alerts[0]["message"].lower() or "Stop" in alerts[0]["message"]
    text = format_exit_alert(
        tier="challenge",
        symbol="SPY",
        strike=739,
        right="C",
        expiry="2026-11-06",
        contracts=1,
        entry=2.10,
        entry_at="2026-11-06T14:35:00+00:00",
        exit_price=4.20,
        exit_at="2026-11-06T18:00:00+00:00",
        reason="take profit +100% ≥ +100%",
        tally="Today 1W/0L · +$210",
    )
    assert "Target +100% hit" in text
    preview = format_challenge_entry_alert(buy)
    assert preview.startswith("🏁 CHALLENGE ENTRY — SPY")
    assert "paper sandbox" in preview


def test_past_deadline_does_not_compress_the_pace():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    assert pace_months_for_deadline("2026-10-31", configured=1.0, now=now) == 1.0
    assert pace_months_for_deadline(None, configured=1.0, now=now) == 1.0
    assert pace_months_for_deadline("", configured=1.5, now=now) == 1.5
    future = pace_months_for_deadline("2026-12-31", configured=1.0, now=now)
    assert future > 1.0


def test_open_trade_row_omits_the_journal_stop_so_the_config_ladder_stays():
    row = order_row_from_open_trade(
        {
            "status": "open",
            "symbol": "QQQ",
            "right": "C",
            "contract": "QQQ261106C00660000",
            "entry_ask": 1.4,
            "expiry": "2026-11-06",
            "strike": 660,
            "contracts": 1,
            "target_profit_pct": 20,
            "stop_loss_pct": 20,
            "exit_plan": "EXIT when premium ≥$1.68 (+20%) or stop −20%",
        }
    )
    assert row is not None
    assert row["ask"] == 1.4
    assert row["contracts_for_bankroll"] == 1
    assert "target_profit_pct" not in row
    assert "stop_loss_pct" not in row
    assert "exit_plan" not in row
    board = apply_challenge_paper_board({"entry": [row]}, [row | {"status": "open"}], {"challenge_paper": False})
    assert board["paper_orders"] == []
    assert board["challenge_paper"] is False
