"""Paper risk controls read from ``config.yaml`` → ``live_trading.risk_controls``.

Everything here is OFF by default except what is explicitly listed as on.
The per-trade dollar stop and risk-based sizing change which trades the
paper book takes and how big they are, so they stay off until a backtest
shows they help. Turn them on in config, not in code.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, fields
from typing import Any

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
