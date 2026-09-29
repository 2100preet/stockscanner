"""Tier-1 flow gate — calls need bullish flow leaders; puts need bearish."""
from __future__ import annotations

from typing import Any

from odte_scanner.signals.actions import ActionSignal


def _leader_map(flow_leaders: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in flow_leaders or []:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            out[sym] = row
    return out


def apply_flow_gate(
    sig: ActionSignal,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    require_flow_confirm: bool = True,
    flow_leaders_top_n: int = 12,
    flow_min_net_score: float = 8.0,
    flow_min_tier: str = "aggressive",
    require_vol_gt_oi: bool = False,
) -> ActionSignal:
    """Demote BUY NOW → WAIT unless option-flow proxy aligns with call/put side."""
    if not require_flow_confirm or sig.action != "BUY_NOW":
        return sig
    sym = str(sig.symbol or "").upper()
    if not sym:
        return sig
    leaders = _leader_map(flow_leaders)
    row = leaders.get(sym)
    is_put = str(sig.right or "C").upper() == "P"
    tier_rank = {"aggressive": 0, "unusual": 1, "golden": 2}
    min_rank = tier_rank.get(str(flow_min_tier or "aggressive").lower(), 0)

    # Missing from leaders = soft pass (most names never hit top-N). Only hard-block
    # when flow IS present and conflicts with call/put side (Bullflow-style veto).
    if row is None:
        sig.detail = (
            f"{sig.detail} · flow n/a: {sym} not in top-{flow_leaders_top_n} leaders "
            "(tape/hist still gate)"
        )
        return sig

    rank = int(row.get("rank") or 999)
    if rank > flow_leaders_top_n:
        sig.detail = (
            f"{sig.detail} · flow n/a: rank #{rank} outside top {flow_leaders_top_n}"
        )
        return sig

    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    top_tier = str(row.get("top_tier") or "aggressive").lower()
    if tier_rank.get(top_tier, 0) < min_rank:
        sig.detail = (
            f"{sig.detail} · flow n/a: tier {top_tier} < {flow_min_tier} (not a veto)"
        )
        return sig

    if is_put:
        if sentiment == "bullish" and net >= flow_min_net_score:
            sig.action = "WAIT"
            sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: put vs bullish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(sig.strength, 48.0)
            return sig
        if sentiment != "bearish" or net > -flow_min_net_score:
            sig.detail = (
                f"{sig.detail} · flow soft: put preferred bearish "
                f"(got {sentiment}, net {net:+.0f})"
            )
            return sig
    else:
        if sentiment == "bearish" and net <= -flow_min_net_score:
            sig.action = "WAIT"
            sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: call vs bearish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(sig.strength, 48.0)
            return sig
        if sentiment != "bullish" or net < flow_min_net_score:
            sig.detail = (
                f"{sig.detail} · flow soft: call preferred bullish "
                f"(got {sentiment}, net {net:+.0f})"
            )
            return sig

    if require_vol_gt_oi and not row.get("vol_gt_oi"):
        sig.detail = f"{sig.detail} · flow soft: no vol>OI on {sym}"
        return sig

    src = str(row.get("source") or "yahoo")
    tag = "UW" if src == "unusual_whales" else "flow"
    sig.detail = (
        f"{sig.detail} · {tag} OK: #{rank} {sentiment} net {net:+.0f} tier {top_tier}"
    )
    return sig


def apply_uw_sell_boost(
    sig: ActionSignal,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    flow_min_net_score: float = 8.0,
) -> ActionSignal:
    """Promote HOLD → SELL_NOW when Unusual Whales flow flips against the open side."""
    if sig.action != "HOLD":
        return sig
    sym = str(sig.symbol or "").upper()
    if not sym:
        return sig
    row = _leader_map(flow_leaders).get(sym)
    if not row or str(row.get("source") or "") != "unusual_whales":
        return sig
    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    is_put = str(sig.right or "C").upper() == "P"
    if (not is_put) and sentiment == "bearish" and net <= -flow_min_net_score:
        sig.action = "SELL_NOW"
        sig.headline = (sig.headline or "").replace("HOLD", "SELL NOW", 1)
        if "SELL NOW" not in (sig.headline or ""):
            sig.headline = f"SELL NOW {sym} — UW flow flipped bearish"
        sig.detail = (
            f"{sig.detail} · UW exit: bearish flow net {net:+.0f} "
            f"(put prem ${float(row.get('put_premium') or 0):,.0f})"
        )
        sig.strength = max(float(sig.strength or 0), 72.0)
        return sig
    if is_put and sentiment == "bullish" and net >= flow_min_net_score:
        sig.action = "SELL_NOW"
        sig.headline = (sig.headline or "").replace("HOLD", "SELL NOW", 1)
        if "SELL NOW" not in (sig.headline or ""):
            sig.headline = f"SELL NOW {sym} put — UW flow flipped bullish"
        sig.detail = (
            f"{sig.detail} · UW exit: bullish call flow net {net:+.0f} "
            f"(call prem ${float(row.get('call_premium') or 0):,.0f})"
        )
        sig.strength = max(float(sig.strength or 0), 72.0)
        return sig
    return sig


def apply_uw_buy_boost(
    sig: ActionSignal,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    flow_min_net_score: float = 8.0,
) -> ActionSignal:
    """Annotate / boost BUY_NOW strength when UW confirms the side."""
    if sig.action != "BUY_NOW":
        return sig
    sym = str(sig.symbol or "").upper()
    row = _leader_map(flow_leaders).get(sym)
    if not row or str(row.get("source") or "") != "unusual_whales":
        return sig
    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    is_put = str(sig.right or "C").upper() == "P"
    if (not is_put) and sentiment == "bullish" and net >= flow_min_net_score:
        sig.strength = min(99.0, float(sig.strength or 0) + 8.0)
        sig.detail = (
            f"{sig.detail} · UW BUY confirm: bullish call flow net {net:+.0f}"
        )
    elif is_put and sentiment == "bearish" and net <= -flow_min_net_score:
        sig.strength = min(99.0, float(sig.strength or 0) + 8.0)
        sig.detail = (
            f"{sig.detail} · UW BUY confirm: bearish put flow net {net:+.0f}"
        )
    return sig
