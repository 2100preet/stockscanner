"""Paper risk controls read from ``config.yaml`` → ``live_trading.risk_controls``.

Everything here is OFF by default except what is explicitly listed as on.
The per-trade dollar stop and risk-based sizing change which trades the
paper book takes and how big they are, so they stay off until a backtest
shows they help. Turn them on in config, not in code.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


@dataclass
class RiskControls:
    # Per-trade dollar stop: sell the whole lot once the open loss reaches
    # this many dollars, even if the % stop is further away. NEEDS A BACKTEST.
    dollar_stop_enabled: bool = False
    dollar_stop_usd: float = 175.0
    # Risk-based sizing: contracts = floor(risk / (premium x stop% x 100)),
    # at least 1, at most ``risk_sizing_max_contracts``. NEEDS A BACKTEST.
    risk_sizing_enabled: bool = False
    risk_per_trade_usd: float = 175.0
    risk_sizing_max_contracts: int = 3
    # Daily loss limits. ON: when hit, new paper ENTRIES stop for the rest of
    # the Chicago day. Exits keep running and signal alerts keep sending.
    daily_limits_enabled: bool = True
    desk_daily_loss_usd: float = 400.0
    desk_daily_losers: int = 2
    account_daily_loss_usd: float = 800.0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "RiskControls":
        raw = dict(raw or {})
        known = {f.name for f in fields(cls)} - {"extra"}
        kwargs: dict[str, Any] = {}
        for name in known:
            if name in raw:
                kwargs[name] = raw.pop(name)
        out = cls(**kwargs)
        out.extra = raw
        out.dollar_stop_enabled = bool(out.dollar_stop_enabled)
        out.risk_sizing_enabled = bool(out.risk_sizing_enabled)
        out.dollar_stop_usd = float(out.dollar_stop_usd)
        out.risk_per_trade_usd = float(out.risk_per_trade_usd)
        out.risk_sizing_max_contracts = max(1, int(out.risk_sizing_max_contracts))
        out.daily_limits_enabled = bool(out.daily_limits_enabled)
        out.desk_daily_loss_usd = abs(float(out.desk_daily_loss_usd))
        out.desk_daily_losers = max(0, int(out.desk_daily_losers))
        out.account_daily_loss_usd = abs(float(out.account_daily_loss_usd))
        return out


def load_risk_controls(cfg: dict[str, Any] | None = None) -> RiskControls:
    if cfg is None:
        try:
            from odte_scanner.config import load_config

            cfg = load_config(None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("risk controls config unavailable, using defaults: %s", exc)
            cfg = {}
    live = (cfg or {}).get("live_trading") or {}
    return RiskControls.from_dict(live.get("risk_controls") if isinstance(live, dict) else None)


def dollar_stop_hit(controls: RiskControls, *, entry: float, mark: float, held: int) -> float | None:
    """Open loss in dollars when it reaches the cap, else None."""
    if not controls.dollar_stop_enabled or held < 1 or entry <= 0 or mark < 0:
        return None
    loss = (float(entry) - float(mark)) * 100.0 * int(held)
    if loss >= float(controls.dollar_stop_usd) - 1e-6:
        return round(loss, 2)
    return None


def risk_sized_qty(controls: RiskControls, *, premium: float | None, stop_pct: float, floor_qty: int = 1) -> int | None:
    """Contracts for a planned loss of ``risk_per_trade_usd`` at the % stop. None when off."""
    if not controls.risk_sizing_enabled:
        return None
    if premium is None or premium <= 0 or stop_pct <= 0:
        return max(1, floor_qty)
    per_lot = float(premium) * abs(float(stop_pct)) / 100.0 * 100.0
    qty = int(math.floor(float(controls.risk_per_trade_usd) / per_lot + 1e-9))
    return max(1, min(controls.risk_sizing_max_contracts, qty))


# --------------------------------------------------------------------------
# Daily loss limits and the manual kill switch
# --------------------------------------------------------------------------

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
KILL_PATH = ROOT / "outputs" / "paper_kill_switch.json"
LIMIT_ALERTS_PATH = ROOT / "outputs" / "daily_limit_alerts.json"

DESK_NAMES = {
    "sniper": "Sniper",
    "diamond": "Diamond",
    "gold": "Gold Setup",
    "afternoon": "Afternoon Puts",
    "lunch": "Lunch-Break Credit",
    "challenge": "Challenge",
}


def desk_tier(desk: str | None, kind: str | None = None) -> str:
    """The P&L bucket a desk's trades land in (same keys as portal round trips)."""
    for value in (kind, desk):
        text = str(value or "").strip().lower()
        if text in {"sniper", "diamond"}:
            return text
        if text in {"afternoon", "afternoon_puts", "afternoonputs"}:
            return "afternoon"
        if text in {"lunch", "lunch_credit", "lunchcredit"}:
            return "lunch"
        if text in {"sure_winner", "sure_winner_gold", "gold", "golden_exit", "gold setup"}:
            return "gold"
    return str(desk or kind or "other").strip().lower() or "other"


def _ct_day(stamp: Any) -> str | None:
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT).date().isoformat()


def _as_dicts(orders: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in orders or []:
        if isinstance(order, dict):
            out.append(order)
        elif hasattr(order, "to_dict"):
            out.append(order.to_dict())
    return out


def daily_tally(orders: list[Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Today's (Chicago) realized paper P&L and losers, per desk and for the account.

    Built from the same round trips as the portal and scorecard, including
    expiry close-outs. SPX is never in it.
    """
    from odte_scanner.trading.portal_book import round_trips

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.astimezone(CT).date().isoformat()
    desks: dict[str, dict[str, Any]] = {}
    lots: dict[tuple[str, str], float] = {}
    total = 0.0
    for trip in round_trips(_as_dicts(orders), include_other_desks=True):
        if _ct_day(trip.get("exited_at")) != today:
            continue
        tier = desk_tier(trip.get("tier") or trip.get("raw_desk"))
        bucket = desks.setdefault(tier, {"pnl": 0.0, "losers": 0, "trades": 0})
        pnl = float(trip.get("pnl") or 0.0)
        bucket["pnl"] = round(bucket["pnl"] + pnl, 2)
        # A scale-out is several sells on one lot. Losers count lots, not sells.
        key = (tier, str(trip.get("contract") or trip.get("order_id") or ""))
        lots[key] = lots.get(key, 0.0) + pnl
        total += pnl
    for (tier, _contract), pnl in lots.items():
        desks[tier]["trades"] += 1
        if pnl < 0:
            desks[tier]["losers"] += 1
    return {"day": today, "account_pnl": round(total, 2), "desks": desks}


def kill_switch_state(path: Path | None = None) -> dict[str, Any]:
    """Manual pause for new paper entries. ``PAPER_ENTRY_KILL=1`` also turns it on."""
    env = str(os.environ.get("PAPER_ENTRY_KILL", "")).strip().lower() in {"1", "true", "on", "yes"}
    dest = path or KILL_PATH
    state: dict[str, Any] = {"on": False}
    try:
        if dest.exists():
            raw = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    state["on"] = bool(state.get("on")) or env
    if env:
        state["source"] = "env PAPER_ENTRY_KILL"
    return state


def set_kill_switch(on: bool, reason: str = "", path: Path | None = None) -> dict[str, Any]:
    from odte_scanner.json_util import atomic_write_text

    dest = path or KILL_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    state = {"on": bool(on), "reason": str(reason or "")[:200], "at": datetime.now(timezone.utc).isoformat()}
    atomic_write_text(dest, json.dumps(state))
    return kill_switch_state(dest)


def entry_block(
    desk: str | None,
    *,
    orders: list[Any] | None,
    now: datetime | None = None,
    controls: RiskControls | None = None,
    kind: str | None = None,
    kill_path: Path | None = None,
    tally: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Why a new paper entry for this desk is blocked right now, or None.

    Only entries. Exits, flattens and Telegram signal alerts are never blocked.
    """
    kill = kill_switch_state(kill_path)
    if kill.get("on"):
        why = str(kill.get("reason") or "manual")
        return {"scope": "kill_switch", "reason": f"kill switch on ({why})", "short": "kill switch"}
    rc = controls or load_risk_controls()
    if not rc.daily_limits_enabled:
        return None
    t = tally if tally is not None else daily_tally(orders, now)
    tier = desk_tier(desk, kind)
    if t["account_pnl"] <= -rc.account_daily_loss_usd + 1e-6:
        return {
            "scope": "account",
            "reason": f"account daily loss limit: ${t['account_pnl']:,.0f} today (limit −${rc.account_daily_loss_usd:,.0f})",
            "short": "account daily loss limit",
            "pnl": t["account_pnl"],
        }
    bucket = t["desks"].get(tier) or {}
    pnl = float(bucket.get("pnl") or 0.0)
    losers = int(bucket.get("losers") or 0)
    name = DESK_NAMES.get(tier, tier)
    if pnl <= -rc.desk_daily_loss_usd + 1e-6:
        return {
            "scope": tier,
            "reason": f"{name} daily loss limit: ${pnl:,.0f} today (limit −${rc.desk_daily_loss_usd:,.0f})",
            "short": f"{name} daily loss limit",
            "pnl": pnl,
        }
    if rc.desk_daily_losers and losers >= rc.desk_daily_losers:
        return {
            "scope": tier,
            "reason": f"{name} daily loser limit: {losers} losing trades today (limit {rc.desk_daily_losers})",
            "short": f"{name} daily loser limit",
            "pnl": pnl,
            "losers": losers,
        }
    return None


def skipped_line(block: dict[str, Any] | None) -> str | None:
    """Line for a Telegram signal alert when the paper entry is not taken."""
    if not block:
        return None
    return f"⛔ Paper entry skipped — {block.get('short') or block.get('reason')}. Signal only; exits still run."


def format_limit_alert(block: dict[str, Any]) -> str:
    return (
        f"⛔ PAPER ENTRIES PAUSED — {block['reason']}. "
        "Open trades keep their stops, targets and flattens; signal alerts keep sending. · paper"
    )


def limit_alerts_due(block: dict[str, Any] | None, *, day: str, path: Path | None = None) -> str | None:
    """Message to send the first time a limit trips today, else None (deduped on disk)."""
    if not block:
        return None
    from odte_scanner.json_util import atomic_write_text

    dest = path or LIMIT_ALERTS_PATH
    key = f"{day}:{block.get('scope')}:{block.get('short')}"
    seen: list[str] = []
    try:
        if dest.exists():
            raw = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                seen = [str(x) for x in raw]
    except (OSError, json.JSONDecodeError):
        seen = []
    if key in seen:
        return None
    seen = [k for k in seen if k.startswith(day)] + [key]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(dest, json.dumps(seen[-200:]))
    except OSError:
        return None
    return format_limit_alert(block)
