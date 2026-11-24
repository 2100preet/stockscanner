"""One daemon thread that ticks both shadow desks every 20 seconds."""
from __future__ import annotations

import logging
import threading
from typing import Any

from odte_scanner.ext_hours import ah_earnings, overnight_bias
from odte_scanner.ext_hours.common import env_flag

logger = logging.getLogger(__name__)

TICK_SEC = 20
_LOCK = threading.Lock()
_STOP = threading.Event()
_AH: ah_earnings.AhEarningsShadow | None = None
_BIAS: overnight_bias.OvernightBias | None = None
_THREAD: threading.Thread | None = None


def _loop() -> None:
    while not _STOP.is_set():
        for desk in (_AH, _BIAS):
            if desk is None:
                continue
            try:
                desk.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ext-hours desk tick failed: %s", type(exc).__name__)
        _STOP.wait(TICK_SEC)


def start_ext_hours_worker(config_path: str | None = None) -> bool:
    """Start once per process. Off in offline exports or when both kill switches are set."""
    global _AH, _BIAS, _THREAD
    if env_flag("SIGNAL_DESK_OFFLINE"):
        return False
    with _LOCK:
        if _THREAD is not None:
            return False
        if not ah_earnings.is_off():
            _AH = ah_earnings.AhEarningsShadow()
        if not overnight_bias.is_off():
            _BIAS = overnight_bias.OvernightBias()
        if _AH is None and _BIAS is None:
            logger.info("ext-hours shadow desks disabled by kill switches")
            return False
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="ext-hours-shadow", daemon=True)
        _THREAD.start()
    return True


def reset_for_tests() -> None:
    global _AH, _BIAS, _THREAD
    _STOP.set()
    _AH = _BIAS = _THREAD = None


def ah_block() -> dict[str, Any]:
    try:
        if _AH is not None:
            out = _AH.status()
            out["running"] = True
            return out
        return _idle_ah()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "enabled": not ah_earnings.is_off(), "shadow": True, "orders_placed": 0, "last_error": type(exc).__name__}


def _idle_ah() -> dict[str, Any]:
    """Ledger view when the worker is not running in this process (read-only)."""
    desk = ah_earnings.AhEarningsShadow(quotes=object(), sender=lambda _t: {"ok": False})
    out = desk.status()
    out["running"] = False
    return out


def bias_block() -> dict[str, Any]:
    try:
        desk = _BIAS or overnight_bias.OvernightBias(quotes=object(), sender=lambda _t: {"ok": False})
        out = desk.status()
        out["running"] = _BIAS is not None
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "enabled": not overnight_bias.is_off(), "context_only": True, "orders_placed": 0, "last_error": type(exc).__name__}
