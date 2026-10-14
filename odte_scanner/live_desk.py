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
    """Merge config + env for Webull enable/dry_run/paper.

    Safety:
      • dry_run stays at the config default (true) unless a paper submit switch
        is explicit.
      • The production host is never selected. WEBULL_LIVE cannot place live orders.
      • Paper mode (config ``paper: true``, WEBULL_PAPER=1, or the default) uses
        api.sandbox.webull.com.
      • WEBULL_PAPER_SUBMIT=1 with keys and an account id submits to that
        sandbox/paper account and leaves dry_run false.
      • Keys alone never disable dry_run.
    """
    lt = dict(lt or {})
    key = (os.environ.get("WEBULL_APP_KEY") or lt.get("app_key") or "").strip()
    secret = (os.environ.get("WEBULL_APP_SECRET") or lt.get("app_secret") or "").strip()
    keys_ok = bool(key and secret)
    live = _env_flag("WEBULL_LIVE", False)
    paper_env = _env_flag("WEBULL_PAPER", False)
    paper_submit = _env_flag("WEBULL_PAPER_SUBMIT", False)
    paper = bool(lt.get("paper", False)) or paper_env or paper_submit
    paper_acct = (os.environ.get("WEBULL_PAPER_ACCOUNT_ID") or "").strip()
    account_id = (os.environ.get("WEBULL_ACCOUNT_ID") or str(lt.get("account_id") or "")).strip()
    if paper and paper_acct:
        account_id = paper_acct
    account_id = account_id or None
    acct_ok = bool(account_id)

    golden_env = (os.environ.get("WEBULL_GOLDEN_ONLY") or "").strip()
    if golden_env:
        golden_only = _env_flag("WEBULL_GOLDEN_ONLY", True)
    else:
        golden_only = bool(lt.get("golden_only", True))

    enabled = bool(lt.get("enabled", False))
    dry_run = bool(lt.get("dry_run", True))
    cfg_submit = enabled and not dry_run
    # Production (api.webull.com) is not selectable. WEBULL_LIVE is recorded
    # and ignored so a leftover Fly secret cannot place a live order.
    sandbox = True
    if not paper:
        paper = True
    if (paper_submit or cfg_submit) and keys_ok and acct_ok:
        enabled = True
        dry_run = False
    else:
        enabled = bool(lt.get("enabled", False))
        dry_run = True if not cfg_submit else bool(lt.get("dry_run", True))
        if not (keys_ok and acct_ok):
            dry_run = True

    return {
        "enabled": enabled,
        "dry_run": dry_run,
        "keys_ok": keys_ok,
        "account_ok": acct_ok,
        "account_id": account_id,
        "webull_live_env": live,
        "paper": paper,
        "paper_submit_env": paper_submit,
        "sandbox": sandbox,
        "golden_only": golden_only,
        "host": "api.sandbox.webull.com" if sandbox else "api.webull.com",
    }
