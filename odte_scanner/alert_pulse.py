"""Fast BUY/SELL alert pulse — snapshot + Telegram without Pages deploy."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]


def build_offline_snapshot(config_path: str | None = None) -> dict[str, Any]:
    """Build the same offline desk snapshot Pages uses (Tradier marks when configured)."""
    from odte_scanner.ui import create_app

    os.environ["SIGNAL_DESK_OFFLINE"] = "1"
    app = create_app(config_path)
    with app.test_client() as client:
        res = client.get("/api/snapshot?offline=1")
        if res.status_code != 200:
            raise RuntimeError(f"snapshot pulse failed: HTTP {res.status_code} {res.data[:500]!r}")
        payload = res.get_json()
        if not isinstance(payload, dict):
            raise RuntimeError("snapshot pulse returned non-JSON object")
    return payload


def pulse_desk_alerts(
    config_path: str | None = None,
    *,
    skip_if_env: str = "SKIP_DESK_ALERTS",
) -> dict[str, Any]:
    """Rebuild board from latest scan outputs and push new BUY/SELL Telegram alerts."""
    if (os.environ.get(skip_if_env) or "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"ok": False, "skipped": True, "note": f"{skip_if_env} set"}

    from odte_scanner.alerts import dispatch_snapshot_alerts
    from odte_scanner.json_util import dumps_strict

    payload = build_offline_snapshot(config_path)
    alert_meta = dispatch_snapshot_alerts(payload) or {"ok": False}

    out = ROOT / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_at": payload.get("generated_at"),
        "desk_alerts": alert_meta,
        "sell_now": ((payload.get("actions") or {}).get("counts") or {}).get("sell_now"),
        "buy_now_puts": ((payload.get("actions") or {}).get("counts") or {}).get("buy_now_puts"),
    }
    (out / "desk_alerts_last.json").write_text(dumps_strict(record, indent=2, default=str))
    return alert_meta


def run_alert_loop(
    *,
    config_path: str | None = None,
    end_hour_utc: int = 20,
    end_minute_utc: int = 5,
    pause_sec: float = 20.0,
    max_cycles: int | None = None,
) -> int:
    """Scan + alert until RTH end (UTC). Designed for a long GitHub Actions job."""
    cycles = 0
    while True:
        now = datetime.now(timezone.utc)
        if now.hour > end_hour_utc or (
            now.hour == end_hour_utc and now.minute >= end_minute_utc
        ):
            logger.info("alert loop: past RTH end %02d:%02d UTC — stop", end_hour_utc, end_minute_utc)
            break
        if now.hour < 13 or (now.hour == 13 and now.minute < 25):
            logger.info("alert loop: before RTH — sleep 60s")
            time.sleep(60)
            continue

        cycles += 1
        logger.info("alert loop cycle %s at %s", cycles, now.isoformat())
        try:
            from odte_scanner.scanner import run_scan

            run_scan(config_path, place_paper=False, universe_mode="focus")
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert loop scan failed: %s", exc)

        try:
            meta = pulse_desk_alerts(config_path)
            logger.info(
                "alert pulse: ok=%s sent=%s primed=%s note=%s providers=%s",
                meta.get("ok"),
                meta.get("sent"),
                meta.get("primed"),
                meta.get("note") or meta.get("error"),
                meta.get("providers"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert loop pulse failed: %s", exc)

        # Refresh static site files each cycle (deploy is the Actions Live workflow).
        try:
            from odte_scanner.pages_export import export_pages

            # SKIP_DESK_ALERTS if pulse already sent this cycle
            os.environ["SKIP_DESK_ALERTS"] = "1"
            export_pages(out_dir="site", config_path=config_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert loop export-pages failed: %s", exc)
        finally:
            os.environ.pop("SKIP_DESK_ALERTS", None)

        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(max(5.0, float(pause_sec)))
    return cycles
