"""Second sandbox account and the per-desk exit rules from the exit study."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from odte_scanner.challenge.tracker import ChallengeTracker
from odte_scanner.signals.actions import decide_exit
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import CASH_ACCOUNT_NUMBER, WebullBroker, find_account_id
from tests.session_clock import session_et


def test_find_account_id_skips_the_account_number():
    payload = {
        "accounts": [
            {"account_number": "DEA72Q84", "account_id": "111"},
            {"accountNumber": CASH_ACCOUNT_NUMBER, "accountId": CASH_ACCOUNT_NUMBER},
            {"account_number": CASH_ACCOUNT_NUMBER, "account_id": "cash-internal-9"},
        ]
    }
    assert find_account_id(payload, CASH_ACCOUNT_NUMBER) == "cash-internal-9"
    assert find_account_id(payload, "MISSING") is None


def test_challenge_routes_to_account_2_and_gold_stays_on_account_1(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        account_id_2="cash-2",
    )
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
        challenge={
            "entry": [
                {
                    "symbol": "JPM",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 1.58,
                    "contract": "JPM261016C00345000",
                    "expiry": "2026-10-16",
                    "strike": 345,
                }
            ],
            "exit": [],
            "tickets": [],
        },
    )
    by_symbol = {o["symbol"]: o for o in out["submitted"]}
    assert by_symbol["DOCU"]["desk"] == "sure_winner"
    assert by_symbol["DOCU"]["meta"].get("account_id") in (None, "", "margin-1")
    assert by_symbol["DOCU"]["meta"].get("account_id") != "cash-2"
    assert by_symbol["JPM"]["desk"] == "challenge"
    assert by_symbol["JPM"]["meta"]["account_id"] == "cash-2"
    assert by_symbol["JPM"]["status"] == "dry_run"


def test_missing_account_2_does_not_send_challenge_to_the_margin_account(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
    )
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge")
    out = trader.sync(
        challenge={
            "entry": [
                {
                    "symbol": "JPM",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 1.58,
                    "contract": "JPM261016C00345000",
                    "expiry": "2026-10-16",
                    "strike": 345,
                }
            ]
        }
    )
    assert out["submitted_n"] == 0
    assert any("WEBULL_ACCOUNT_ID_2" in f["reason"] for f in out["filtered"])


def test_account_number_is_refused_as_account_2(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        account_id_2=CASH_ACCOUNT_NUMBER,
    )
    trader = AutoTrader(broker, golden_only=True, second_desk="challenge")
    out = trader.sync(
        challenge={
            "entry": [
                {
                    "symbol": "JPM",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 1.58,
                    "contract": "JPM261016C00345000",
                    "expiry": "2026-10-16",
                    "strike": 345,
                }
            ]
        }
    )
    assert out["submitted_n"] == 0
    assert any("account number" in f["reason"] for f in out["filtered"])


def test_eod_mode_does_not_stop_a_fifty_percent_dip():
    trade = {
        "status": "open",
        "symbol": "MRNA",
        "entry_ask": 2.0,
        "bid": 0.8,
        "right": "C",
        "dte_bucket": "0dte",
        "expiry": "2026-10-16",
    }
    now = session_et(11, 0)
    stopped = decide_exit(trade, quote={"last": 100}, score_by_symbol={"MRNA": 70}, now=now)
    held = decide_exit(
        trade,
        quote={"last": 100},
        score_by_symbol={"MRNA": 70},
        now=now,
        exit_mode="eod",
    )
    assert stopped is not None and stopped.action == "SELL_NOW"
    assert held is None or held.action != "SELL_NOW"


def test_challenge_tp100_holds_through_the_sprint_bank(tmp_path: Path):
    tr = ChallengeTracker(tmp_path / "ch.json", starting_cash=1000, exit_rule="tp100")
    entered = tr.enter(
        {
            "action": "ENTRY",
            "symbol": "AAPL",
            "right": "C",
            "ask": 2.0,
            "contract": "AAPL261016C00200000",
            "expiry": "2026-10-16",
            "strike": 200,
            "horizon": "sprint",
            "hold_style": "sprint",
            "dte": 2,
            "spot": 198,
            "target_premium_mult": 1.5,
            "hold_min_days": 0,
            "hold_max_days": 2,
            "hold_ideal_days": 1,
            "ensemble_score": 70,
        }
    )
    assert entered is not None
    entered.entered_at = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    early = tr.evaluate_open(entered, mark=2.90, quote={}, sprint_desk=True)
    assert early["action"] == "HOLD"
    hit = tr.evaluate_open(entered, mark=4.10, quote={}, sprint_desk=True)
    assert hit["action"] == "EXIT"
    assert "100" in hit["detail"]
