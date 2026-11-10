"""Fast exit loop: stops, targets, trails and clock flattens every 5–10 s.

Exits used to run only inside the focus scan (minutes apart) and the hot
entry loop did not sell. On Nov 9 the 10:30 CT Sniper/Diamond flatten went
out at 2:26 PM CT. This loop runs for the whole option session, on its own
thread, with one long-lived broker that reloads the shared ledger each tick.
It never opens a position and never blocks on the scan: when the main sync
holds the lock, the tick is skipped and the next one runs.

Env: FAST_EXIT_LOOP=0 disables it. FAST_EXIT_PAUSE_SEC (default 7, 5–10).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_started = False
LAST: dict[str, Any] = {}


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def exit_pause_sec() -> float:
    try:
        raw = float(os.environ.get("FAST_EXIT_PAUSE_SEC") or 7)
    except (TypeError, ValueError):
        raw = 7.0
    return min(10.0, max(5.0, raw))


def run_exit_tick(trader: Any, now: datetime | None = None) -> dict[str, Any]:
    """One pass. Outside the option session nothing is sent."""
    from odte_scanner.signals.hold_rules import option_session_open

    moment = now or datetime.now(timezone.utc)
    if not option_session_open(moment):
        return {"skipped": "session closed"}
    result = trader.sync_exits(now=moment)
    LAST.update({"at": moment.isoformat(), **{k: v for k, v in result.items() if k != "sent"}})
    LAST["sent_n"] = len(result.get("sent") or [])
    return result


def _loop(config_path: str | None, build: Callable[[str | None], Any] | None = None) -> None:
    from odte_scanner.fast_desk import build_paper_trader

    builder = build or (lambda path: build_paper_trader(path)[1])
    trader = None
    logger.info("Fast exit loop started pause=%ss", exit_pause_sec())
    while True:
        try:
            if trader is None:
                trader = builder(config_path)
            run_exit_tick(trader)
        except Exception as exc:  # noqa: BLE001
            logger.exception("fast exit tick failed: %s", exc)
            trader = None  # rebuild from config next tick
        time.sleep(exit_pause_sec())


def start_fast_exit_worker(config_path: str | None = None) -> bool:
    global _started
    if not _flag("FAST_EXIT_LOOP", True) or _flag("SIGNAL_DESK_OFFLINE", False):
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_loop, args=(config_path,), name="fast-exit-worker", daemon=True).start()
    return True
