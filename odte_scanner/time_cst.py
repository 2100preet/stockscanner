"""Central-time helpers for BUY NOW / SELL NOW signal stamps."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_cst_label(iso_or_dt: str | datetime | None, *, with_seconds: bool = True) -> str | None:
    """Human CST/CDT label, e.g. 'Aug 13, 2026, 12:15:03 PM CDT'."""
    if iso_or_dt is None:
        return None
    if isinstance(iso_or_dt, datetime):
        dt = iso_or_dt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        text = str(iso_or_dt).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CT)
    fmt = "%b %d, %Y, %I:%M:%S %p %Z" if with_seconds else "%b %d, %Y, %I:%M %p %Z"
    return local.strftime(fmt).lstrip("0").replace(" 0", " ")


def signal_timestamps() -> dict[str, str]:
    """UTC + CST stamps for a new BUY NOW / SELL NOW pulse."""
    utc = now_utc_iso()
    return {
        "signaled_at": utc,
        "signaled_at_cst": to_cst_label(utc) or utc,
    }


def merge_first_signal_time(
    store: dict[str, Any],
    *,
    symbol: str,
    action: str,
    signaled_at: str,
    signaled_at_cst: str,
) -> dict[str, Any]:
    """Keep the first BUY NOW / SELL NOW time per symbol+action (don't reset on refresh)."""
    out = dict(store or {})
    key = f"{str(symbol).upper()}:{str(action).upper()}"
    existing = out.get(key)
    if existing and existing.get("signaled_at"):
        return out
    out[key] = {
        "symbol": str(symbol).upper(),
        "action": str(action).upper(),
        "signaled_at": signaled_at,
        "signaled_at_cst": signaled_at_cst,
        "first_seen_at": signaled_at,
    }
    return out


def load_signal_store(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_signal_store(path: str | Path | None, store: dict[str, Any]) -> None:
    if not path:
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(store, indent=2))
    except Exception:  # noqa: BLE001
        pass


def resolve_first_signal_time(
    store: dict[str, Any],
    *,
    symbol: str,
    action: str,
) -> tuple[str, str, dict[str, Any]]:
    """Return first-pulse UTC + CST for symbol/action, creating them if needed."""
    key = f"{str(symbol).upper()}:{str(action).upper()}"
    prior = (store or {}).get(key) or {}
    if prior.get("signaled_at"):
        utc = str(prior["signaled_at"])
        cst = str(prior.get("signaled_at_cst") or to_cst_label(utc) or utc)
        return utc, cst, store
    ts = signal_timestamps()
    updated = merge_first_signal_time(
        store,
        symbol=symbol,
        action=action,
        signaled_at=ts["signaled_at"],
        signaled_at_cst=ts["signaled_at_cst"],
    )
    return ts["signaled_at"], ts["signaled_at_cst"], updated


def append_asked_cst(detail: str | None, *, action: str, signaled_at_cst: str | None) -> str:
    """Append 'asked to buy/sell <CST>' once onto a detail line."""
    base = detail or ""
    if not signaled_at_cst:
        return base
    if "CST" in base or "CDT" in base:
        return base
    verb = "asked to buy" if str(action).upper() == "BUY_NOW" else "asked to sell"
    if not base:
        return f"{verb} {signaled_at_cst}"
    return f"{base} · {verb} {signaled_at_cst}"
