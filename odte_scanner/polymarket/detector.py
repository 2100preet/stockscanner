"""Weighted multi-signal anomaly detector for Polymarket flow.

Composite score, 0-100 (each part capped, all inputs public):

  freshness      20  wallet join_date < 7d (10 if < 30d; 0 when unknown)
  concentration  15  <= 2 distinct markets (15), <= 3 (10), or >= 60% of
                     wallet volume on this condition (10)
  longshot       15  entry <= 15c (15), <= 20c (10)
  cluster size   15  5 + 10 * clamp(log10(usd / 1000), 0, 1)   ($1K=5, $10K=15)
  conviction     10  2h cluster vs wallet's largest prior fill on this
                     condition: >= 5x (10), >= 2x (5); first entry (3)
  market outlier 10  cluster vs this market's recent fills: >= p99 (10),
                     >= p95 (7), >= p90 (4); robust z from median/IQR shown
  absorption     10  cluster / (cluster + visible asks within +1c): >= 30%
                     (10), >= 15% (6), >= 5% (3)
  off-hours       5  fill outside 8:30-16:00 ET or on a weekend

Tier A (fresh-wallet longshot): fresh AND concentrated AND longshot AND
>= $1,000 bought in the last 2h. Tier B: >= $10,000 into a longshot (any
wallet) or an odds spike (see ``check_odds_spike``). Nothing here places
orders; the output is an alert dict for human review.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class DetectorSettings:
    longshot_max: float = 0.20
    longshot_strong: float = 0.15
    fresh_days: float = 7.0
    max_wallet_markets: int = 3
    concentration_share: float = 0.60
    tier_a_min_usd: float = 1_000.0
    tier_b_min_usd: float = 10_000.0
    cluster_window_sec: int = 7_200
    spike_abs: float = 0.05
    spike_rel: float = 0.50
    spike_window_sec: int = 3_600
    spike_min_age_sec: int = 300
    spike_min_volume_usd: float = 2_000.0
    absorption_band: float = 0.01
    escalate_ratio: float = 2.0
    max_escalations: int = 2
    dedupe_sec: int = 86_400

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "DetectorSettings":
        data = dict(raw or {})
        kwargs: dict[str, Any] = {}
        for name, f in cls.__dataclass_fields__.items():  # type: ignore[attr-defined]
            if name in data and data[name] is not None:
                try:
                    kwargs[name] = type(f.default)(data[name])
                except (TypeError, ValueError):
                    continue
        return cls(**kwargs)


def to_epoch(value: Any) -> int | None:
    """Data API timestamps are epoch seconds; accept ms and ISO too. None stays None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000) if v > 1e12 else int(v)
    try:
        s = str(value).strip()
        if s.isdigit():
            return to_epoch(int(s))
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def is_off_hours(ts: int) -> bool:
    """Outside 8:30-16:00 ET, or a weekend."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    if dt.weekday() >= 5:
        return True
    minutes = dt.hour * 60 + dt.minute
    return not (8 * 60 + 30 <= minutes < 16 * 60)


def ct_label(ts: int | None) -> str:
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CT)
    return dt.strftime("%-I:%M %p CT")


def ask_absorption(book: dict | None, *, shares_bought: float, exec_price: float, band: float = 0.01) -> float | None:
    """Share of visible ask liquidity near the execution price the cluster took.

    ``cluster / (cluster + asks priced <= exec_price + band)`` from a CLOB
    ``/book`` snapshot taken after the fills. None when no book was read.
    """
    if not isinstance(book, dict) or shares_bought <= 0:
        return None
    asks = book.get("asks")
    if not isinstance(asks, list):
        return None
    depth = 0.0
    for lvl in asks:
        try:
            price = float(lvl.get("price"))
            size = float(lvl.get("size"))
        except (TypeError, ValueError, AttributeError):
            continue
        if price <= exec_price + band + 1e-9:
            depth += max(0.0, size)
    return shares_bought / (shares_bought + depth)


def market_outlier(cluster_usd: float, market_fills_usd: Iterable[float]) -> tuple[float | None, float | None]:
    """(percentile 0-100, robust z) of the cluster vs the market's recent fills."""
    vals = sorted(float(v) for v in market_fills_usd if v is not None and v > 0)
    if len(vals) < 10:
        return None, None
    below = sum(1 for v in vals if v < cluster_usd)
    pct = 100.0 * below / len(vals)
    q1, _, q3 = statistics.quantiles(vals, n=4)
    iqr = q3 - q1
    med = statistics.median(vals)
    z = (cluster_usd - med) / (iqr / 1.349) if iqr > 0 else None
    return pct, z


@dataclass
class WalletSignal:
    wallet: str
    name: str
    condition_id: str
    token_id: str
    outcome: str
    cluster_usd: float
    cluster_shares: float
    n_fills: int
    first_fill_ts: int | None
    last_fill_ts: int | None
    avg_price: float
    max_price: float
    prior_count: int
    prior_max_usd: float
    jump_x: float | None
    market_pctile: float | None
    robust_z: float | None
    absorption_pct: float | None
    wallet_age_days: float | None
    distinct_markets: int | None
    condition_share: float | None
    fresh: bool
    concentrated: bool
    longshot: bool
    off_hours: bool
    score: int
    parts: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    tier: str | None = None
    tier_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["parts"] = dict(self.parts)
        out["reasons"] = list(self.reasons)
        return out


def _distinct_markets(stats: dict | None) -> int | None:
    if not isinstance(stats, dict):
        return None
    raw = stats.get("trades")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _wallet_volume(stats: dict | None) -> float | None:
    if not isinstance(stats, dict):
        return None
    for src in (stats, stats.get("all_time_pnl") if isinstance(stats.get("all_time_pnl"), dict) else {}):
        v = src.get("volume_usdc")
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def evaluate_wallet(
    *,
    wallet: str,
    condition_id: str,
    token_id: str,
    activity: Iterable[dict],
    user_stats: dict | None,
    now_ts: int,
    market_fills_usd: Iterable[float] = (),
    book: dict | None = None,
    settings: DetectorSettings | None = None,
    name: str = "",
    outcome: str = "",
) -> WalletSignal:
    """Score one wallet's recent BUY cluster on one outcome token."""
    s = settings or DetectorSettings()
    rows = [
        r for r in activity
        if isinstance(r, dict)
        and str(r.get("type") or "TRADE").upper() == "TRADE"
        and str(r.get("side") or "").upper() == "BUY"
        and str(r.get("condition_id") or condition_id) == condition_id
        and (to_epoch(r.get("timestamp")) or 0) <= now_ts
    ]
    start = now_ts - s.cluster_window_sec

    def usd(r: dict) -> float:
        if r.get("usdc_size") is not None:
            try:
                return float(r["usdc_size"])
            except (TypeError, ValueError):
                pass
        try:
            return float(r.get("size") or 0) * float(r.get("price") or 0)
        except (TypeError, ValueError):
            return 0.0

    same_token = [r for r in rows if str(r.get("token_id") or token_id) == str(token_id)]
    cluster = [r for r in same_token if (to_epoch(r.get("timestamp")) or 0) >= start
               and float(r.get("price") or 1) <= s.longshot_max + 1e-9]
    prior = [r for r in rows if (to_epoch(r.get("timestamp")) or 0) < start]
    cluster_usd = sum(usd(r) for r in cluster)
    cluster_shares = sum(float(r.get("size") or 0) for r in cluster)
    avg_price = cluster_usd / cluster_shares if cluster_shares > 0 else 0.0
    max_price = max((float(r.get("price") or 0) for r in cluster), default=0.0)
    times = [to_epoch(r.get("timestamp")) for r in cluster]
    times = [t for t in times if t is not None]
    first_ts, last_ts = (min(times), max(times)) if times else (None, None)
    if not name and cluster:
        name = str(cluster[-1].get("name") or cluster[0].get("pseudonym") or "")
    if not outcome and cluster:
        outcome = str(cluster[0].get("outcome") or "")

    prior_max = max((usd(r) for r in prior), default=0.0)
    jump_x = cluster_usd / prior_max if prior_max > 0 else None

    pct, z = market_outlier(cluster_usd, market_fills_usd)
    absorption = ask_absorption(book, shares_bought=cluster_shares, exec_price=max_price or avg_price, band=s.absorption_band)

    join_ts = to_epoch((user_stats or {}).get("join_date")) if isinstance(user_stats, dict) else None
    age_days = (now_ts - join_ts) / 86_400 if join_ts else None
    distinct = _distinct_markets(user_stats)
    vol = _wallet_volume(user_stats)
    cond_usd = sum(usd(r) for r in rows)
    share = min(1.0, cond_usd / vol) if vol else None

    fresh = age_days is not None and age_days < s.fresh_days
    concentrated = (distinct is not None and distinct <= s.max_wallet_markets) or (
        share is not None and share >= s.concentration_share
    )
    longshot = 0 < avg_price <= s.longshot_max + 1e-9
    off = is_off_hours(last_ts) if last_ts else False

    parts: dict[str, float] = {}
    reasons: list[str] = []
    if fresh:
        parts["freshness"] = 20
        reasons.append(f"fresh wallet {age_days:.1f}d")
    elif age_days is not None and age_days < 30:
        parts["freshness"] = 10
        reasons.append(f"young wallet {age_days:.0f}d")
    else:
        parts["freshness"] = 0
        if age_days is None:
            reasons.append("wallet age unknown")
    conc = 0.0
    if distinct is not None and distinct <= 2:
        conc = 15
    elif distinct is not None and distinct <= s.max_wallet_markets:
        conc = 10
    if share is not None and share >= s.concentration_share:
        conc = max(conc, 10)
    parts["concentration"] = conc
    if conc:
        bits = []
        if distinct is not None:
            bits.append(f"{distinct} mkts")
        if share is not None:
            bits.append(f"{share * 100:.0f}% here")
        reasons.append("concentrated " + " / ".join(bits))
    if 0 < avg_price <= s.longshot_strong + 1e-9:
        parts["longshot"] = 15
    elif longshot:
        parts["longshot"] = 10
    else:
        parts["longshot"] = 0
    if parts["longshot"]:
        reasons.append(f"longshot {avg_price * 100:.1f}¢")
    size_pts = 5 + 10 * min(1.0, max(0.0, math.log10(cluster_usd / 1_000))) if cluster_usd >= 1_000 else 0.0
    parts["cluster_size"] = round(size_pts, 1)
    if cluster_usd >= 1_000:
        reasons.append(f"${cluster_usd:,.0f} in {len(cluster)} fill{'' if len(cluster) == 1 else 's'} / 2h")
    if jump_x is None:
        parts["conviction"] = 3 if cluster_usd > 0 else 0
        if cluster_usd > 0:
            reasons.append("first entry on this market")
    elif jump_x >= 5:
        parts["conviction"] = 10
    elif jump_x >= 2:
        parts["conviction"] = 5
    else:
        parts["conviction"] = 0
    if jump_x is not None and jump_x >= 2:
        reasons.append(f"conviction {jump_x:.1f}x prior max fill")
    if pct is None:
        parts["market_outlier"] = 0
    elif pct >= 99:
        parts["market_outlier"] = 10
    elif pct >= 95:
        parts["market_outlier"] = 7
    elif pct >= 90:
        parts["market_outlier"] = 4
    else:
        parts["market_outlier"] = 0
    if parts["market_outlier"]:
        ztxt = f", z {z:.1f}" if z is not None else ""
        reasons.append(f"top {max(1, 100 - int(pct))}% market fill{ztxt}")
    if absorption is None:
        parts["absorption"] = 0
    elif absorption >= 0.30:
        parts["absorption"] = 10
    elif absorption >= 0.15:
        parts["absorption"] = 6
    elif absorption >= 0.05:
        parts["absorption"] = 3
    else:
        parts["absorption"] = 0
    if absorption is not None and parts["absorption"]:
        reasons.append(f"absorbed {absorption * 100:.0f}% of asks")
    parts["off_hours"] = 5 if off else 0
    if off:
        reasons.append("off-hours/weekend")
    score = int(round(min(100.0, sum(parts.values()))))

    sig = WalletSignal(
        wallet=wallet,
        name=name,
        condition_id=condition_id,
        token_id=str(token_id),
        outcome=outcome,
        cluster_usd=round(cluster_usd, 2),
        cluster_shares=round(cluster_shares, 4),
        n_fills=len(cluster),
        first_fill_ts=first_ts,
        last_fill_ts=last_ts,
        avg_price=round(avg_price, 4),
        max_price=max_price,
        prior_count=len(prior),
        prior_max_usd=round(prior_max, 2),
        jump_x=round(jump_x, 2) if jump_x is not None else None,
        market_pctile=round(pct, 1) if pct is not None else None,
        robust_z=round(z, 1) if z is not None else None,
        absorption_pct=round(absorption * 100, 1) if absorption is not None else None,
        wallet_age_days=round(age_days, 2) if age_days is not None else None,
        distinct_markets=distinct,
        condition_share=round(share, 3) if share is not None else None,
        fresh=fresh,
        concentrated=concentrated,
        longshot=longshot,
        off_hours=off,
        score=score,
        parts=parts,
        reasons=reasons,
    )
    if fresh and concentrated and longshot and cluster_usd >= s.tier_a_min_usd:
        sig.tier, sig.tier_label = "A", "Tier A — Fresh-wallet longshot"
        if cluster_usd >= s.tier_b_min_usd:
            sig.reasons.append("whale size")
    elif longshot and cluster_usd >= s.tier_b_min_usd:
        sig.tier, sig.tier_label = "B", "Tier B — Whale longshot"
    return sig


def mid_change(history: Iterable[Iterable[float]], now_ts: int, mid_now: float | None, *, lookback: int = 3_600) -> tuple[float | None, float | None]:
    """(mid ~1h ago, change) from ``[[ts, mid], ...]`` history."""
    pts = [(int(t), float(m)) for t, m in history if t is not None and m is not None and int(t) <= now_ts]
    if mid_now is None or not pts:
        return None, None
    target = now_ts - lookback
    window = [p for p in pts if p[0] >= target - 600]
    if not window:
        return None, None
    base = min(window, key=lambda p: abs(p[0] - target))
    if now_ts - base[0] < 300:
        return None, None
    return base[1], mid_now - base[1]


def check_odds_spike(
    history: Iterable[Iterable[float]],
    *,
    now_ts: int,
    mid_now: float | None,
    recent_volume_usd: float,
    settings: DetectorSettings | None = None,
) -> dict[str, Any] | None:
    """Midpoint jump >= +5c AND >= +50% vs the low of the last 30-60 min on unusual volume."""
    s = settings or DetectorSettings()
    if mid_now is None:
        return None
    pts = [(int(t), float(m)) for t, m in history
           if t is not None and m is not None and now_ts - s.spike_window_sec <= int(t) <= now_ts - 60]
    old_enough = [p for p in pts if now_ts - p[0] >= s.spike_min_age_sec]
    if not old_enough:
        return None
    base_ts, base = min(pts, key=lambda p: p[1])
    if base <= 0:
        return None
    jump = mid_now - base
    rel = jump / base
    if jump >= s.spike_abs - 1e-9 and rel >= s.spike_rel - 1e-9 and recent_volume_usd >= s.spike_min_volume_usd:
        return {
            "base_mid": round(base, 4),
            "base_ts": base_ts,
            "mid": round(mid_now, 4),
            "jump": round(jump, 4),
            "jump_pct": round(rel * 100, 1),
            "volume_usd": round(recent_volume_usd, 2),
            "minutes": round((now_ts - base_ts) / 60),
        }
    return None


BULLISH_RE = r"\b(rejoin\w*|return\w*|approv\w*|beat\w*|ipo|go(?:es)? public|acquir\w*|split|raise\w*)\b"
BEARISH_RE = r"\b(resign\w*|step(?:s|ping)? down|fired|oust\w*|bankrupt\w*|chapter 11|indict\w*|charged|investigat\w*|probe|lawsuit|sued|delist\w*|layoff\w*|miss\w*|default|reject\w*|recall\w*)\b"


def implied_direction(question: str, outcome: str) -> str | None:
    """'call' if the bought outcome reads bullish for the ticker, 'put' if bearish."""
    import re

    q = str(question or "")
    bull = bool(re.search(BULLISH_RE, q, re.IGNORECASE))
    bear = bool(re.search(BEARISH_RE, q, re.IGNORECASE))
    if bull == bear:
        return None
    yes_dir = "call" if bull else "put"
    if str(outcome or "").strip().lower() == "no":
        return "put" if yes_dir == "call" else "call"
    return yes_dir
