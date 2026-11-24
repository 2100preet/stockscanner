"""Shared helpers: CT clock, NYSE trading days, atomic JSON ledgers, Telegram."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]

# NYSE full-day closures. Half days are skipped too (no AMC drift / bias logic).
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26"}


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def is_full_trading_day(d: date) -> bool:
    return is_trading_day(d) and d.isoformat() not in NYSE_HALF_DAYS


def next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def prev_trading_day(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


def now_ct(clock: Callable[[], datetime] | None = None) -> datetime:
    dt = clock() if clock else datetime.now(CT)
    return dt.astimezone(CT) if dt.tzinfo else dt.replace(tzinfo=CT)


def in_window(now: datetime, start: time, end: time) -> bool:
    t = now.astimezone(CT).time()
    return start <= t < end


def resolve_path(env_name: str, default_rel: str) -> Path:
    raw = (os.environ.get(env_name) or "").strip()
    p = Path(raw) if raw else Path(default_rel)
    return p if p.is_absolute() else ROOT / p


class JsonLedger:
    """Small thread-safe JSON file with atomic writes (lives on the Fly volume)."""

    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self._default = default
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    return data
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("ledger %s unreadable: %s", self.path.name, exc)
            return json.loads(json.dumps(self._default))

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(data, fh, indent=1, default=str)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


def default_sender(text: str) -> dict[str, Any]:
    """Same Telegram bot + chat the app already uses (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."""
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text)


def money(x: float | None) -> str:
    return "—" if x is None else f"${x:,.2f}"


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{digits}f}%"
