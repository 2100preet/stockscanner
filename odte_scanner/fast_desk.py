"""Sniper and Diamond hot path.

The focus scan takes minutes, and the live desk only sends Telegram after
that scan returns. This loop reloads the Sniper/Diamond 5-minute bars and
quotes on its own thread, then sends the entry alert and routes the paper
buy without waiting for the scan.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, time as clock, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

_worker_started = False
_lock = threading.Lock()

HOT_START = clock(8, 30)
# 10:30 is the entry cutoff. The extra minutes let an unfilled buy cancel
# on this loop instead of waiting for the next focus scan.
HOT_END = clock(10, 40)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def fast_pause_sec() -> float:
    """Seconds between hot cycles. Default 20, clamped to 15–30."""
    try:
        raw = float(os.environ.get("FAST_DESK_PAUSE_SEC") or 20)
    except (TypeError, ValueError):
        raw = 20.0
    return min(30.0, max(15.0, raw))


def in_hot_window(now: datetime | None = None) -> bool:
    """Weekday 8:30–10:40 America/Chicago."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    ct = moment.astimezone(CT)
    if ct.weekday() >= 5:
        return False
    current = ct.time()
    return HOT_START <= current < HOT_END


def load_hot_boards(now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One bar fetch for both books. Chains run only for the capped names."""
    from odte_scanner.signals.diamond_board import load_diamond_board, load_intraday_bars
    from odte_scanner.signals.sniper_board import load_sniper_board

    bars = load_intraday_bars()
    diamond = load_diamond_board(now=now, bars=bars)
    sniper = load_sniper_board(now=now, bars=bars)
    return diamond, sniper


def _default_dispatch(snapshot: dict[str, Any]) -> dict[str, Any]:
    from odte_scanner.alerts.dispatcher import dispatch_snapshot_alerts

    # Do not prime-seed an empty seen file. The first Sniper of the day has
    # to leave the building, not get marked seen without a send.
    return dispatch_snapshot_alerts(snapshot, prime=False)


def _default_route(
    diamond: dict[str, Any],
    sniper: dict[str, Any],
    now: datetime,
    config_path: str | None,
) -> dict[str, Any]:
    from odte_scanner.config import load_config
    from odte_scanner.edge.gate import edge_gate_enabled
    from odte_scanner.live_desk import resolve_webull_live_flags
    from odte_scanner.signals.exit_rules import load_exit_rules
    from odte_scanner.trading.auto_trader import AutoTrader, paper_risk_kwargs
    from odte_scanner.trading.webull import WebullBroker

    cfg = load_config(config_path)
    lt = cfg.get("live_trading") or {}
    flags = resolve_webull_live_flags(lt)
    ledger = Path(lt.get("ledger_path", "outputs/webull_orders.json"))
    if not ledger.is_absolute():
        ledger = ROOT / ledger
    broker = WebullBroker(
        enabled=bool(flags["enabled"]),
        dry_run=bool(flags["dry_run"]),
        region=str(lt.get("region") or "us"),
        sandbox=bool(flags["sandbox"]),
        paper=bool(flags["paper"]),
        account_id=flags.get("account_id") or lt.get("account_id"),
        app_key=lt.get("app_key"),
        app_secret=lt.get("app_secret"),
        ledger_path=ledger,
        submit_interval_sec=float(lt.get("submit_interval_sec", 1.5)),
    )
    desk_cfg = dict(lt.get("desks") or {})
    trader = AutoTrader(
        broker,
        require_perfect_hist=bool(lt.get("require_perfect_hist", True)),
        min_hist_win_pct=float(lt.get("min_hist_win_pct", 100)),
        min_hist_win_samples=int(lt.get("min_hist_win_samples", 3)),
        desks=desk_cfg or None,
        max_contracts=int(lt.get("max_contracts", 1)),
        max_orders_per_sync=int(lt.get("max_orders_per_sync", 3)),
        max_golden_buys_per_sync=int(lt.get("max_golden_buys_per_sync", 2)),
        golden_only=bool(flags.get("golden_only", True)),
        close_legacy_positions=bool(lt.get("close_legacy_positions", False)),
        second_desk=str(lt.get("second_desk") or "challenge"),
        exit_rules=load_exit_rules(lt.get("exits")),
        **paper_risk_kwargs(lt, cfg.get("risk") or {}),
        edge_gate=edge_gate_enabled(cfg),
    )
    return trader.sync_hot(diamond=diamond, sniper=sniper, now=now)


def run_fast_desk_cycle(
    config_path: str | None = None,
    *,
    now: datetime | None = None,
    boards: tuple[dict[str, Any], dict[str, Any]] | None = None,
    load_boards: Callable[..., tuple[dict[str, Any], dict[str, Any]]] | None = None,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    route: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh Sniper/Diamond, alert, then route. Does not run the focus scan."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not in_hot_window(moment) and not _env_flag("FAST_DESK_ALWAYS", False):
        return {
            "ok": True,
            "skipped": True,
            "reason": "outside 8:30-10:40 CT",
            "scan": "not_used",
        }

    if boards is not None:
        diamond, sniper = boards
    else:
        try:
            loader = load_boards or load_hot_boards
            diamond, sniper = loader(moment)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fast desk boards failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200], "scan": "not_used"}

    snapshot = {
        "diamond": diamond if isinstance(diamond, dict) else {},
        "sniper": sniper if isinstance(sniper, dict) else {},
    }
    sample = None
    try:
        from odte_scanner.alerts.dispatcher import collect_trade_alerts

        preview = collect_trade_alerts(snapshot)
        if preview:
            sample = preview[0].get("message")
    except Exception as exc:  # noqa: BLE001
        logger.info("fast desk preview failed: %s", exc)

    alert_meta: dict[str, Any] = {"ok": False}
    try:
        sender = dispatch or _default_dispatch
        alert_meta = sender(snapshot) or {"ok": False}
    except Exception as exc:  # noqa: BLE001
        alert_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("fast desk alert failed: %s", exc)

    route_meta: dict[str, Any] = {"ok": False}
    try:
        if route is None:
            route_meta = _default_route(snapshot["diamond"], snapshot["sniper"], moment, config_path)
        else:
            route_meta = route(snapshot["diamond"], snapshot["sniper"], moment) or {}
    except Exception as exc:  # noqa: BLE001
        route_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("fast desk route failed: %s", exc)

    logger.info(
        "fast desk cycle sniper=%s diamond=%s sent=%s",
        len((snapshot["sniper"] or {}).get("signals") or []),
        len((snapshot["diamond"] or {}).get("signals") or []),
        (alert_meta or {}).get("sent"),
    )
    return {
        "ok": True,
        "skipped": False,
        "scan": "not_used",
        "alerts": alert_meta,
        "route": route_meta,
        "sample_alert": sample,
        "sniper_signals": len((snapshot["sniper"] or {}).get("signals") or []),
        "diamond_signals": len((snapshot["diamond"] or {}).get("signals") or []),
    }


def _worker_loop(config_path: str | None) -> None:
    pause = fast_pause_sec()
    logger.info("Fast Sniper/Diamond desk started pause=%ss", pause)
    while True:
        try:
            if in_hot_window() or _env_flag("FAST_DESK_ALWAYS", False):
                run_fast_desk_cycle(config_path)
            else:
                time.sleep(30)
                continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("fast desk worker crash: %s", exc)
        time.sleep(fast_pause_sec())


def start_fast_desk_worker(config_path: str | None = None) -> bool:
    """Start the hot loop once per process. The focus scan cannot block it."""
    global _worker_started
    if not _env_flag("FAST_DESK_LOOP", True):
        logger.info("FAST_DESK_LOOP disabled — Sniper/Diamond stay on the focus scan")
        return False
    if _env_flag("SIGNAL_DESK_OFFLINE", False):
        return False
    with _lock:
        if _worker_started:
            return False
        _worker_started = True
    threading.Thread(
        target=_worker_loop,
        args=(config_path,),
        name="fast-desk-worker",
        daemon=True,
    ).start()
    return True
