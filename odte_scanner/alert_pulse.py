"""Fast BUY/SELL alert pulse — snapshot + Telegram without Pages deploy."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]


def alert_log_path() -> Path:
    return Path(ROOT) / "outputs" / "desk_alert_log.jsonl"


def build_offline_snapshot(config_path: str | None = None) -> dict[str, Any]:
    """Build the same offline desk snapshot Pages uses (Tradier marks when configured).

    The offline flag is request-scoped. Leaving it set on the process made the
    live Fly host stamp every board ``host=github-pages``, which the TTL cache
    refuses, so every UI poll rebuilt under the snapshot lock.
    """
    from odte_scanner.ui import create_app

    previous = os.environ.get("SIGNAL_DESK_OFFLINE")
    os.environ["SIGNAL_DESK_OFFLINE"] = "1"
    try:
        app = create_app(config_path)
        with app.test_client() as client:
            res = client.get("/api/snapshot?offline=1")
            if res.status_code != 200:
                raise RuntimeError(f"snapshot pulse failed: HTTP {res.status_code} {res.data[:500]!r}")
            payload = res.get_json()
            if not isinstance(payload, dict):
                raise RuntimeError("snapshot pulse returned non-JSON object")
        return payload
    finally:
        if previous is None:
            os.environ.pop("SIGNAL_DESK_OFFLINE", None)
        else:
            os.environ["SIGNAL_DESK_OFFLINE"] = previous


def append_alert_log(
    attempts: list[dict[str, Any]] | None,
    *,
    path: str | Path | None = None,
) -> int:
    """Append one JSON line per send attempt. Never rewrites earlier lines."""
    rows = [row for row in (attempts or []) if isinstance(row, dict)]
    if not rows:
        return 0
    dest = Path(path) if path else alert_log_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def read_alert_log(
    *,
    day: str | None = None,
    limit: int = 200,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Today's sent-alert log in America/Chicago, oldest first."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ct = ZoneInfo("America/Chicago")
    day_s = day or datetime.now(ct).strftime("%Y-%m-%d")
    dest = Path(path) if path else alert_log_path()
    rows: list[dict[str, Any]] = []
    if dest.exists():
        for line in dest.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            stamp = str(row.get("time") or "")
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=ZoneInfo("UTC"))
                row_day = when.astimezone(ct).strftime("%Y-%m-%d")
            except ValueError:
                row_day = stamp[:10]
            if row_day == day_s:
                rows.append(row)
    cap = max(1, int(limit))
    return {"day": day_s, "n": len(rows), "alerts": rows[-cap:]}


def read_alert_status() -> dict[str, Any]:
    """Public alert health: channel flags and the last pulse summary. No secrets."""
    from odte_scanner.alerts.telegram import configured as telegram_configured
    from odte_scanner.alerts.whatsapp import configured as whatsapp_configured
    from odte_scanner.json_util import load_json_state

    tg = telegram_configured()
    wa = whatsapp_configured()
    last_path = ROOT / "outputs" / "desk_alerts_last.json"
    last_raw = load_json_state(last_path) if last_path.exists() else {}
    if not isinstance(last_raw, dict):
        last_raw = {}
    meta = last_raw.get("desk_alerts") if isinstance(last_raw.get("desk_alerts"), dict) else {}
    results = meta.get("results") if isinstance(meta.get("results"), list) else []
    symbols: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        symbols.append(
            {
                "symbol": row.get("symbol"),
                "desk": row.get("desk"),
                "side": row.get("side"),
            }
        )
    errors = meta.get("errors") if isinstance(meta.get("errors"), list) else []
    log = read_alert_log(limit=12)
    return {
        "telegram": {
            "ok": bool(tg.get("ok")),
            "token_set": bool(tg.get("token_set")),
            "chat_set": bool(tg.get("chat_set")),
            "enabled": bool(tg.get("enabled")),
        },
        "whatsapp_configured": bool(wa.get("ok")),
        "last": {
            "generated_at": last_raw.get("generated_at"),
            "snapshot_at": last_raw.get("snapshot_at"),
            "configured": meta.get("configured"),
            "sent": meta.get("sent"),
            "primed": meta.get("primed"),
            "note": meta.get("note") or meta.get("error"),
            "providers": meta.get("providers") or [],
            "errors": [str(item)[:200] for item in errors[:8]],
            "symbols": symbols[:12],
            "log_today_n": log["n"],
            "log_today": log["alerts"],
        },
    }


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
    # The edge table does not mute. Golden Flow filters are inside the collector.
    alert_meta = dispatch_snapshot_alerts(payload) or {"ok": False}
    try:
        append_alert_log(alert_meta.get("attempts") if isinstance(alert_meta, dict) else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert log append failed: %s", exc)

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
