"""Central-time helpers for BUY NOW / SELL NOW signal stamps."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Actions that keep a sticky "asked" time while continuously on the board.
STICKY_ACTIONS = frozenset(
    {
        "BUY_NOW",
        "SELL_NOW",
        "BUY_RIP",
        "BUY_BEAUTY",
        "BUY_LEVEL",
        "PUT_NOW",
        "CALL_NOW",
        "EXIT",
    }
)

# BUY-side stamps cleared when the same OCC flips to SELL / EXIT.
_BUY_ACTIONS_FOR_OCC = frozenset(
    {
        "BUY_NOW",
        "BUY_RIP",
        "BUY_BEAUTY",
        "BUY_LEVEL",
        "PUT_NOW",
        "CALL_NOW",
        "ENTRY",
        "RADAR_HOT",
        "SNIPER",
    }
)


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


def signal_store_key(symbol: str, action: str, contract: str | None = None) -> str:
    """Sticky key: SYMBOL:ACTION[:OCC] so exit/re-enter of the same OCC gets a fresh time."""
    sym = str(symbol or "").upper()
    act = str(action or "").upper()
    occ = str(contract or "").strip().upper()
    if occ:
        return f"{sym}:{act}:{occ}"
    return f"{sym}:{act}"


def merge_first_signal_time(
    store: dict[str, Any],
    *,
    symbol: str,
    action: str,
    signaled_at: str,
    signaled_at_cst: str,
    contract: str | None = None,
) -> dict[str, Any]:
    """Keep the first BUY NOW / SELL NOW time per symbol+action[+OCC] (don't reset on refresh)."""
    out = dict(store or {})
    key = signal_store_key(symbol, action, contract)
    existing = out.get(key)
    if existing and existing.get("signaled_at"):
        return out
    entry: dict[str, Any] = {
        "symbol": str(symbol).upper(),
        "action": str(action).upper(),
        "signaled_at": signaled_at,
        "signaled_at_cst": signaled_at_cst,
        "first_seen_at": signaled_at,
    }
    occ = str(contract or "").strip().upper()
    if occ:
        entry["contract"] = occ
    out[key] = entry
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
    contract: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return first-pulse UTC + CST for symbol/action[/OCC], creating them if needed.

    OCC-scoped keys do **not** inherit legacy SYMBOL:ACTION stamps — after an exit
    clears the OCC key, a re-enter must get a fresh asked time.
    """
    key = signal_store_key(symbol, action, contract)
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
        contract=contract,
    )
    return ts["signaled_at"], ts["signaled_at_cst"], updated


def clear_signal_time(
    store: dict[str, Any],
    *,
    symbol: str,
    action: str,
    contract: str | None = None,
) -> dict[str, Any]:
    """Drop a sticky stamp so the next pulse gets a fresh asked time."""
    out = dict(store or {})
    key = signal_store_key(symbol, action, contract)
    out.pop(key, None)
    # Also drop legacy symbol-only key when clearing an OCC-scoped exit.
    if contract:
        legacy = signal_store_key(symbol, action, None)
        out.pop(legacy, None)
    return out


def clear_signal_times_for_contracts(
    store: dict[str, Any],
    contracts: set[str] | frozenset[str] | list[str],
    *,
    actions: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Clear sticky stamps for closed/exited OCCs (any matching action unless restricted)."""
    wanted = {str(c).strip().upper() for c in (contracts or []) if c}
    if not wanted:
        return dict(store or {})
    act_filter = {str(a).upper() for a in actions} if actions is not None else None
    out: dict[str, Any] = {}
    for key, val in (store or {}).items():
        if not isinstance(val, dict):
            out[key] = val
            continue
        occ = str(val.get("contract") or "").strip().upper()
        if not occ and isinstance(key, str) and key.count(":") >= 2:
            # SYMBOL:ACTION:OCC
            occ = key.rsplit(":", 1)[-1].upper()
        act = str(val.get("action") or "").upper()
        if not act and isinstance(key, str) and ":" in key:
            parts = key.split(":")
            if len(parts) >= 2:
                act = parts[1].upper()
        if occ and occ in wanted and (act_filter is None or act in act_filter):
            continue
        out[key] = val
    return out


def prune_signal_store_to_active(
    store: dict[str, Any],
    active_keys: set[str] | frozenset[str],
    *,
    sticky_actions: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Keep sticky entries only while the pulse is still on the board.

    When a row exits / leaves BUY or SELL NOW, its stamp is dropped so a later
    re-enter of the same OCC gets a new ``signaled_at``.
    """
    sticky = {str(a).upper() for a in (sticky_actions or STICKY_ACTIONS)}
    active = {str(k) for k in (active_keys or set())}
    out: dict[str, Any] = {}
    for key, val in (store or {}).items():
        if not isinstance(val, dict):
            out[key] = val
            continue
        act = str(val.get("action") or "").upper()
        if not act and isinstance(key, str) and ":" in key:
            parts = str(key).split(":")
            if len(parts) >= 2:
                act = parts[1].upper()
        if act in sticky and str(key) not in active:
            continue
        out[key] = val
    return out


def clear_buy_stamps_on_sell(
    store: dict[str, Any],
    *,
    symbol: str,
    contract: str | None = None,
) -> dict[str, Any]:
    """When an OCC flips to SELL/EXIT, clear BUY-side sticky stamps for re-entry."""
    out = dict(store or {})
    for act in _BUY_ACTIONS_FOR_OCC:
        out = clear_signal_time(out, symbol=symbol, action=act, contract=contract)
    return out


def append_asked_cst(detail: str | None, *, action: str, signaled_at_cst: str | None) -> str:
    """Append 'asked to buy/sell <CST>' once onto a detail line."""
    base = detail or ""
    if not signaled_at_cst:
        return base
    if "CST" in base or "CDT" in base:
        return base
    u = str(action or "").upper()
    if u.startswith("BUY") or u in {"PUT_NOW", "CALL_NOW", "ENTRY", "RADAR_HOT", "SNIPER"}:
        verb = "asked to buy"
    elif u.startswith("SELL") or u in {"EXIT"}:
        verb = "asked to sell"
    else:
        verb = "signaled"
    if not base:
        return f"{verb} {signaled_at_cst}"
    return f"{base} · {verb} {signaled_at_cst}"


def stamp_buy_sell_times(
    row: dict[str, Any],
    store: dict[str, Any],
    *,
    sticky_actions: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach sticky signaled_at / signaled_at_cst for actionable BUY/SELL-style rows."""
    action = str(row.get("action") or "").upper()
    sticky = sticky_actions or STICKY_ACTIONS
    if action not in sticky:
        return row, store
    sym = str(row.get("symbol") or "").upper()
    if not sym:
        return row, store
    contract = row.get("contract")
    utc, cst, store = resolve_first_signal_time(
        store, symbol=sym, action=action, contract=contract
    )
    out = dict(row)
    out["signaled_at"] = utc
    out["signaled_at_cst"] = cst
    out["detail"] = append_asked_cst(out.get("detail"), action=action, signaled_at_cst=cst)
    return out, store
