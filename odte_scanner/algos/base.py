from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HORIZONS = ("0dte", "weekly", "swing")


@dataclass
class AlgoSignal:
    name: str
    score: float  # 0–100
    bullish: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "bullish": self.bullish,
            "details": self.details,
        }


@dataclass
class TickerScore:
    symbol: str
    ensemble_score: float
    signals: list[AlgoSignal]
    last_price: float
    expected_move_pct: float
    reasons: list[str] = field(default_factory=list)
    horizon: str = "0dte"
    confirms: int = 0
    quality: bool = False
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None

    @property
    def bullish(self) -> bool:
        return self.ensemble_score >= 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "horizon": self.horizon,
            "ensemble_score": round(self.ensemble_score, 2),
            "last_price": round(self.last_price, 4),
            "expected_move_pct": round(self.expected_move_pct, 3),
            "bullish": self.bullish,
            "confirms": self.confirms,
            "quality": self.quality,
            "entry": round(self.entry, 4) if self.entry is not None else None,
            "stop": round(self.stop, 4) if self.stop is not None else None,
            "target": round(self.target, 4) if self.target is not None else None,
            "risk_reward": round(self.risk_reward, 2) if self.risk_reward is not None else None,
            "reasons": self.reasons,
            "signals": [s.to_dict() for s in self.signals],
        }
