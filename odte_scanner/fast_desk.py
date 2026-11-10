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
last_alert_thread: threading.Thread | None = None

HOT_START = clock(8, 30)
# 10:30 is the entry cutoff. The extra minutes let an unfilled buy cancel
# on this loop instead of waiting for the next focus scan.
HOT_END = clock(10, 40)
# Afternoon Puts enters 10:00–14:15 CT. The loop stays up through the 14:45 flat.
AFTERNOON_START = clock(10, 0)
AFTERNOON_END = clock(14, 50)


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


def in_afternoon_window(now: datetime | None = None) -> bool:
    """Weekday 10:00–14:50 America/Chicago. Entries stop at 14:15; exits run to 14:45."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    ct = moment.astimezone(CT)
    if ct.weekday() >= 5:
        return False
    current = ct.time()
    return AFTERNOON_START <= current < AFTERNOON_END


def load_hot_boards(now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One bar fetch for both books. Chains run only for the capped names."""
    from odte_scanner.signals.diamond_board import load_diamond_board, load_intraday_bars
    from odte_scanner.signals.sniper_board import load_sniper_board

    bars = load_intraday_bars()
    diamond = load_diamond_board(now=now, bars=bars)
    sniper = load_sniper_board(now=now, bars=bars)
    return diamond, sniper


def _default_dispatch(snapshot: dict[str, Any]) -> dict[str, Any]:
    from odte_scanner.alerts.dispatcher import dispatch_hot_alerts_async

    # Do not prime-seed an empty seen file. The first Sniper of the day has
    # to leave the building, not get marked seen without a send. The HTTP
    # call runs on a side thread so it cannot delay the paper order.
    return dispatch_hot_alerts_async(snapshot)


def build_paper_trader(config_path: str | None = None):
    """One Webull paper broker + trader from config (same settings every loop uses)."""
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
        submit_interval_sec=float(lt.get("submit_interval_sec", 0.5)),
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
    return broker, trader


def _default_route(
    diamond: dict[str, Any],
    sniper: dict[str, Any],
    now: datetime,
    config_path: str | None,
) -> dict[str, Any]:
    _broker, trader = build_paper_trader(config_path)
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
    """Refresh Sniper/Diamond, route, then alert. Does not run the focus scan."""
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
    # Route first. A Telegram round-trip must not sit in front of the fill.
    route_meta: dict[str, Any] = {"ok": False}
    try:
        if route is None:
            route_meta = _default_route(snapshot["diamond"], snapshot["sniper"], moment, config_path)
        else:
            route_meta = route(snapshot["diamond"], snapshot["sniper"], moment) or {}
    except Exception as exc:  # noqa: BLE001
        route_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("fast desk route failed: %s", exc)

    try:
        from odte_scanner.alerts.dispatcher import promote_routed_hot_alerts

        routed_rows = list((route_meta or {}).get("submitted") or [])
        routed_rows.extend(list((route_meta or {}).get("recent") or []))
        promote_routed_hot_alerts(snapshot, routed_rows)
    except Exception as exc:  # noqa: BLE001
        logger.info("fast desk promote failed: %s", exc)

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
        thread = alert_meta.get("thread") if isinstance(alert_meta, dict) else None
        if isinstance(thread, threading.Thread):
            global last_alert_thread
            last_alert_thread = thread
            alert_meta = {key: value for key, value in alert_meta.items() if key != "thread"}
    except Exception as exc:  # noqa: BLE001
        alert_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("fast desk alert failed: %s", exc)

    try:
        from odte_scanner.hot_state import publish_hot

        publish_hot(diamond=snapshot["diamond"], sniper=snapshot["sniper"])
    except Exception as exc:  # noqa: BLE001
        logger.info("fast desk publish failed: %s", exc)

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


def _default_afternoon_route(board: dict[str, Any], now: datetime, config_path: str | None) -> dict[str, Any]:
    """Paper route for Afternoon Puts. Does not submit a Sniper or Diamond buy."""
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
        submit_interval_sec=float(lt.get("submit_interval_sec", 0.5)),
    )
    trader = AutoTrader(
        broker,
        require_perfect_hist=bool(lt.get("require_perfect_hist", True)),
        min_hist_win_pct=float(lt.get("min_hist_win_pct", 100)),
        min_hist_win_samples=int(lt.get("min_hist_win_samples", 3)),
        desks=dict(lt.get("desks") or {}) or None,
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
    return trader.sync_hot(afternoon=board, now=now)


def _publish_afternoon_failure(error: str) -> None:
    """Put a crashed cycle on the hot board so /api/snapshot is not null."""
    try:
        from odte_scanner.hot_state import publish_hot

        publish_hot(
            afternoon={
                "ok": False,
                "error": error,
                "signals": [],
                "qualified": [],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon puts publish failed: %s", exc)


def run_afternoon_puts_cycle(
    config_path: str | None = None,
    *,
    now: datetime | None = None,
    board: dict[str, Any] | None = None,
    load_board: Callable[..., dict[str, Any]] | None = None,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    route: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh Afternoon Puts, route the paper buy, then alert. The focus scan is not used."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not in_afternoon_window(moment) and not _env_flag("FAST_DESK_ALWAYS", False):
        return {
            "ok": True,
            "skipped": True,
            "reason": "outside 10:00-14:50 CT",
            "scan": "not_used",
        }
    if board is None:
        try:
            loader = load_board
            if loader is None:
                from odte_scanner.signals.afternoon_puts_board import load_afternoon_board

                loader = load_afternoon_board
            # Keyword-only: load_afternoon_board(*, now=..., store_path=..., bars=...).
            board = loader(now=moment)
        except Exception as exc:  # noqa: BLE001
            logger.exception("afternoon puts board failed: %s", exc)
            error = str(exc)[:200]
            _publish_afternoon_failure(error)
            return {"ok": False, "error": error, "scan": "not_used"}
    board = board if isinstance(board, dict) else {}
    snapshot = {"afternoon_puts": board}
    route_meta: dict[str, Any] = {"ok": False}
    try:
        if route is None:
            route_meta = _default_afternoon_route(board, moment, config_path)
        else:
            route_meta = route(board, moment) or {}
    except Exception as exc:  # noqa: BLE001
        route_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("afternoon puts route failed: %s", exc)
    try:
        from odte_scanner.alerts.dispatcher import promote_routed_hot_alerts

        routed_rows = list((route_meta or {}).get("submitted") or [])
        routed_rows.extend(list((route_meta or {}).get("recent") or []))
        promote_routed_hot_alerts(snapshot, routed_rows)
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon puts promote failed: %s", exc)
    sample = None
    try:
        from odte_scanner.alerts.dispatcher import collect_trade_alerts

        preview = collect_trade_alerts(snapshot)
        if preview:
            sample = preview[0].get("message")
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon puts preview failed: %s", exc)
    alert_meta: dict[str, Any] = {"ok": False}
    try:
        sender = dispatch or _default_dispatch
        alert_meta = sender(snapshot) or {"ok": False}
        thread = alert_meta.get("thread") if isinstance(alert_meta, dict) else None
        if isinstance(thread, threading.Thread):
            global last_alert_thread
            last_alert_thread = thread
            alert_meta = {key: value for key, value in alert_meta.items() if key != "thread"}
    except Exception as exc:  # noqa: BLE001
        alert_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("afternoon puts alert failed: %s", exc)
    try:
        from odte_scanner.hot_state import publish_hot

        publish_hot(afternoon=board)
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon puts publish failed: %s", exc)
    try:
        from odte_scanner.scorecard.service import observe_snapshot

        observe_snapshot(afternoon=board, now=moment, skip_eval=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("afternoon puts scorecard failed: %s", exc)
    logger.info(
        "afternoon puts cycle signals=%s sent=%s",
        len(board.get("signals") or []),
        (alert_meta or {}).get("sent"),
    )
    return {
        "ok": True,
        "skipped": False,
        "scan": "not_used",
        "alerts": alert_meta,
        "route": route_meta,
        "sample_alert": sample,
        "afternoon_signals": len(board.get("signals") or []),
        "phase": board.get("phase"),
    }


def in_lunch_window(now: datetime | None = None) -> bool:
    """Weekday 10:00–14:50 CT. The range starts at 10:00 and the flat is 14:45."""
    return in_afternoon_window(now)


def _publish_lunch_failure(error: str) -> None:
    try:
        from odte_scanner.hot_state import publish_hot

        publish_hot(
            lunch={
                "ok": False,
                "error": error,
                "signals": [],
                "qualified": [],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("lunch credit publish failed: %s", exc)


def _default_lunch_route(board: dict[str, Any], now: datetime, config_path: str | None) -> dict[str, Any]:
    """Paper route for Lunch-Break Credit. Does not submit another desk's order."""
    from odte_scanner.config import load_config
    from odte_scanner.live_desk import resolve_webull_live_flags
    from odte_scanner.trading.lunch_router import LunchRouter
    from odte_scanner.trading.webull import WebullBroker

    cfg = load_config(config_path)
    lt = cfg.get("live_trading") or {}
    desks = dict(lt.get("desks") or {})
    enabled = bool(desks.get("lunch_credit", True))
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
        submit_interval_sec=float(lt.get("submit_interval_sec", 0.5)),
    )
    if not enabled:
        for card in board.get("signals") or []:
            if isinstance(card, dict):
                card["send"] = False
                card["send_order"] = False
    return LunchRouter(broker, enabled=enabled).sync(board, now)


def run_lunch_credit_cycle(
    config_path: str | None = None,
    *,
    now: datetime | None = None,
    board: dict[str, Any] | None = None,
    load_board: Callable[..., dict[str, Any]] | None = None,
    dispatch: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    route: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh Lunch-Break Credit, route the paper spread, then alert."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not in_lunch_window(moment) and not _env_flag("FAST_DESK_ALWAYS", False):
        return {
            "ok": True,
            "skipped": True,
            "reason": "outside 10:00-14:50 CT",
            "scan": "not_used",
        }
    if board is None:
        try:
            loader = load_board
            if loader is None:
                from odte_scanner.signals.lunch_credit_board import load_lunch_board

                loader = load_lunch_board
            board = loader(now=moment)
        except Exception as exc:  # noqa: BLE001
            logger.exception("lunch credit board failed: %s", exc)
            error = str(exc)[:200]
            _publish_lunch_failure(error)
            return {"ok": False, "error": error, "scan": "not_used"}
    board = board if isinstance(board, dict) else {}
    route_meta: dict[str, Any] = {"ok": False}
    try:
        if route is None:
            route_meta = _default_lunch_route(board, moment, config_path)
        else:
            route_meta = route(board, moment) or {}
    except Exception as exc:  # noqa: BLE001
        route_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("lunch credit route failed: %s", exc)
    snapshot = {"lunch_credit": board}
    sample = None
    try:
        from odte_scanner.alerts.dispatcher import collect_trade_alerts

        preview = collect_trade_alerts(snapshot)
        if preview:
            sample = preview[0].get("message")
    except Exception as exc:  # noqa: BLE001
        logger.info("lunch credit preview failed: %s", exc)
    alert_meta: dict[str, Any] = {"ok": False}
    try:
        sender = dispatch or _default_dispatch
        alert_meta = sender(snapshot) or {"ok": False}
        thread = alert_meta.get("thread") if isinstance(alert_meta, dict) else None
        if isinstance(thread, threading.Thread):
            global last_alert_thread
            last_alert_thread = thread
            alert_meta = {key: value for key, value in alert_meta.items() if key != "thread"}
    except Exception as exc:  # noqa: BLE001
        alert_meta = {"ok": False, "error": str(exc)[:200]}
        logger.exception("lunch credit alert failed: %s", exc)
    try:
        from odte_scanner.hot_state import publish_hot

        publish_hot(lunch=board)
    except Exception as exc:  # noqa: BLE001
        logger.info("lunch credit publish failed: %s", exc)
    logger.info(
        "lunch credit cycle signals=%s sent=%s path=%s",
        len(board.get("signals") or []),
        (alert_meta or {}).get("sent"),
        board.get("order_path"),
    )
    return {
        "ok": True,
        "skipped": False,
        "scan": "not_used",
        "alerts": alert_meta,
        "route": route_meta,
        "sample_alert": sample,
        "lunch_signals": len(board.get("signals") or []),
        "phase": board.get("phase"),
        "order_path": board.get("order_path"),
    }


def _board_or_error(loader: Callable[..., dict[str, Any]], now: datetime) -> dict[str, Any]:
    """A dict the hot overlay can publish. Null is only for 'not loaded yet'."""
    try:
        board = loader(now=now)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200], "signals": [], "qualified": []}
    if not isinstance(board, dict):
        return {"ok": False, "error": "board was empty", "signals": [], "qualified": []}
    if board.get("ok") is False and not board.get("error"):
        board = dict(board)
        board["error"] = str(board.get("note") or "board unavailable")[:200]
    return board


def publish_index_boards_if_missing(now: datetime | None = None) -> dict[str, Any]:
    """Publish Afternoon Puts and Lunch-Break Credit once when the loop is quiet.

    After 14:50 CT the trading cycles do not run, so a fresh process would
    leave both keys null on /api/snapshot. This loads the boards and does
    not route an order or send an alert.
    """
    from odte_scanner.hot_state import hot_snapshot, publish_hot

    moment = now or datetime.now(timezone.utc)
    hot = hot_snapshot()
    published: dict[str, Any] = {}
    if not isinstance(hot.get("afternoon_puts"), dict):
        from odte_scanner.signals.afternoon_puts_board import load_afternoon_board

        board = _board_or_error(load_afternoon_board, moment)
        publish_hot(afternoon=board)
        published["afternoon_puts"] = board.get("ok")
    if not isinstance(hot.get("lunch_credit"), dict):
        from odte_scanner.signals.lunch_credit_board import load_lunch_board

        board = _board_or_error(load_lunch_board, moment)
        publish_hot(lunch=board)
        published["lunch_credit"] = board.get("ok")
    return published


def _worker_loop(config_path: str | None) -> None:
    pause = fast_pause_sec()
    logger.info("Fast desk started pause=%ss", pause)
    while True:
        try:
            try:
                from odte_scanner.signals.expected_move import run_expected_move_cycle

                run_expected_move_cycle()
            except Exception as exc:  # noqa: BLE001
                logger.exception("expected move cycle failed: %s", exc)
            try:
                from odte_scanner.signals.confluence import run_confluence_cycle

                if in_hot_window() or in_afternoon_window():
                    run_confluence_cycle()
            except Exception as exc:  # noqa: BLE001
                logger.exception("confluence cycle failed: %s", exc)
            hot = in_hot_window() or _env_flag("FAST_DESK_ALWAYS", False)
            afternoon = in_afternoon_window() or _env_flag("FAST_DESK_ALWAYS", False)
            if not hot and not afternoon:
                publish_index_boards_if_missing()
                time.sleep(30)
                continue
            if hot:
                run_fast_desk_cycle(config_path)
            if afternoon:
                run_afternoon_puts_cycle(config_path)
                run_lunch_credit_cycle(config_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fast desk worker crash: %s", exc)
        time.sleep(fast_pause_sec())


def start_fast_desk_worker(config_path: str | None = None) -> bool:
    """Start the hot entry loop and the fast exit loop once per process."""
    try:
        from odte_scanner.fast_exits import start_fast_exit_worker

        start_fast_exit_worker(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("fast exit worker did not start: %s", exc)
    return _start_fast_desk_worker(config_path)


def _start_fast_desk_worker(config_path: str | None = None) -> bool:
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
