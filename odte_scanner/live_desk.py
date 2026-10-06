"""Always-on live desk worker (Railway / Render / local UI).

GitHub Pages cannot stream ticks — it only refreshes when Actions deploys.
This worker runs *inside* the live Flask host and continuously:

  1. focus scan (Tradier / UW / Polygon when secrets set)
  2. Telegram pulse for *new* BUY/SELL
  3. Offline snapshot rebuild → Webull auto_sync (stage or live submit)

Cadence ≈ one focus-scan duration + short pause (back-to-back). That is the
practical floor unless the scan universe is cut further.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_worker_started = False
_lock = threading.Lock()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _in_rth_window(*, extended: bool = False) -> bool:
    """US cash RTH in UTC (approx 13:30–20:00). Extended = weekdays 12:00–21:00 UTC."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    if extended:
        return 12 <= now.hour < 21
    if now.hour < 13 or (now.hour == 13 and now.minute < 25):
        return False
    if now.hour > 20 or (now.hour == 20 and now.minute >= 5):
        return False
    return True


def run_live_desk_cycle(config_path: str | None = None) -> dict[str, Any]:
    """One scan + Telegram/Webull pulse. Safe to call from a background thread."""
    from odte_scanner.alert_pulse import pulse_desk_alerts
    from odte_scanner.scanner import run_scan

    started = datetime.now(timezone.utc).isoformat()
    scan_err = None
    try:
        run_scan(config_path, place_paper=False, universe_mode="focus")
    except Exception as exc:  # noqa: BLE001
        scan_err = str(exc)
        logger.exception("live desk scan failed: %s", exc)

    alert_meta: dict[str, Any] = {"ok": False}
    try:
        alert_meta = pulse_desk_alerts(config_path) or {"ok": False}
    except Exception as exc:  # noqa: BLE001
        alert_meta = {"ok": False, "error": str(exc)}
        logger.exception("live desk pulse failed: %s", exc)

    return {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scan_error": scan_err,
        "desk_alerts": alert_meta,
    }


def _worker_loop(config_path: str | None) -> None:
    pause = float(os.environ.get("LIVE_DESK_PAUSE_SEC") or 15)
    pause = max(5.0, pause)
    extended = _env_flag("LIVE_DESK_EXTENDED", False)
    always = _env_flag("LIVE_DESK_ALWAYS", False)
    logger.info(
        "Live desk worker started pause=%ss extended=%s always=%s",
        pause,
        extended,
        always,
    )
    while True:
        try:
            if always or _in_rth_window(extended=extended):
                out = run_live_desk_cycle(config_path)
                logger.info(
                    "live desk cycle done sent=%s note=%s scan_err=%s",
                    (out.get("desk_alerts") or {}).get("sent"),
                    (out.get("desk_alerts") or {}).get("note")
                    or (out.get("desk_alerts") or {}).get("error"),
                    out.get("scan_error"),
                )
            else:
                logger.debug("live desk: outside RTH — sleep")
                time.sleep(60)
                continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("live desk worker crash: %s", exc)
        time.sleep(pause)


def start_live_desk_worker(config_path: str | None = None) -> bool:
    """Start background live desk once per process. Returns True if started."""
    global _worker_started
    if not _env_flag("LIVE_DESK_LOOP", True):
        logger.info("LIVE_DESK_LOOP disabled — no background scan worker")
        return False
    # Pages / offline export must never start the worker
    if _env_flag("SIGNAL_DESK_OFFLINE", False):
        return False
    with _lock:
        if _worker_started:
            return False
        _worker_started = True
    t = threading.Thread(
        target=_worker_loop,
        args=(config_path,),
        name="live-desk-worker",
        daemon=True,
    )
    t.start()
    return True


def resolve_webull_live_flags(lt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge config + env for Webull enable/dry_run.

    WEBULL_LIVE=1 with APP_KEY+SECRET → enabled and dry_run=False (real submits).
    Keys alone → enabled for staging, dry_run stays config default (usually True).
    """
    lt = dict(lt or {})
    key = (os.environ.get("WEBULL_APP_KEY") or lt.get("app_key") or "").strip()
    secret = (os.environ.get("WEBULL_APP_SECRET") or lt.get("app_secret") or "").strip()
    keys_ok = bool(key and secret)
    live = _env_flag("WEBULL_LIVE", False)
    enabled = bool(lt.get("enabled", False))
    if live and keys_ok:
        enabled = True
    dry_run = bool(lt.get("dry_run", True))
    if live and keys_ok:
        dry_run = False
    return {
        "enabled": enabled,
        "dry_run": dry_run,
        "keys_ok": keys_ok,
        "webull_live_env": live,
    }
