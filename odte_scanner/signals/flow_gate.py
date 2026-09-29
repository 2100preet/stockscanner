"""Tier-1 flow gate — calls need bullish flow leaders; puts need bearish."""
from __future__ import annotations

from typing import Any


def _leader_map(flow_leaders: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in flow_leaders or []:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            out[sym] = row
    return out


def apply_flow_gate(
    sig: Any,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    require_flow_confirm: bool = True,
    flow_leaders_top_n: int = 12,
    flow_min_net_score: float = 8.0,
    flow_min_tier: str = "aggressive",
    require_vol_gt_oi: bool = False,
) -> Any:
    """Demote BUY NOW → WAIT unless option-flow proxy aligns with call/put side.

    Duck-typed: works on ActionSignal, LotteryAction, or any object with
    ``action``, ``symbol``, ``detail``, ``headline``, ``strength`` (+ optional ``right``).
    """
    if not require_flow_confirm or getattr(sig, "action", None) != "BUY_NOW":
        return sig
    sym = str(getattr(sig, "symbol", "") or "").upper()
    if not sym:
        return sig
    leaders = _leader_map(flow_leaders)
    row = leaders.get(sym)
    is_put = str(getattr(sig, "right", None) or "C").upper() == "P"
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
            sig.headline = str(sig.headline or "").replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: put vs bullish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(float(sig.strength or 0), 48.0)
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
            sig.headline = str(sig.headline or "").replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: call vs bearish flow "
                f"(got {sentiment}, net {net:+.0f})"
            )
            sig.strength = min(float(sig.strength or 0), 48.0)
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
    sig: Any,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    flow_min_net_score: float = 8.0,
) -> Any:
    """Promote HOLD → SELL_NOW when Unusual Whales flow flips against the open side."""
    if getattr(sig, "action", None) != "HOLD":
        return sig
    sym = str(getattr(sig, "symbol", "") or "").upper()
    if not sym:
        return sig
    row = _leader_map(flow_leaders).get(sym)
    if not row or str(row.get("source") or "") != "unusual_whales":
        return sig
    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    is_put = str(getattr(sig, "right", None) or "C").upper() == "P"
    if (not is_put) and sentiment == "bearish" and net <= -flow_min_net_score:
        sig.action = "SELL_NOW"
        sig.headline = str(sig.headline or "").replace("HOLD", "SELL NOW", 1)
        if "SELL NOW" not in str(sig.headline or ""):
            sig.headline = f"SELL NOW {sym} — UW flow flipped bearish"
        sig.detail = (
            f"{sig.detail} · UW exit: bearish flow net {net:+.0f} "
            f"(put prem ${float(row.get('put_premium') or 0):,.0f})"
        )
        sig.strength = max(float(sig.strength or 0), 72.0)
        return sig
    if is_put and sentiment == "bullish" and net >= flow_min_net_score:
        sig.action = "SELL_NOW"
        sig.headline = str(sig.headline or "").replace("HOLD", "SELL NOW", 1)
        if "SELL NOW" not in str(sig.headline or ""):
            sig.headline = f"SELL NOW {sym} put — UW flow flipped bullish"
        sig.detail = (
            f"{sig.detail} · UW exit: bullish call flow net {net:+.0f} "
            f"(call prem ${float(row.get('call_premium') or 0):,.0f})"
        )
        sig.strength = max(float(sig.strength or 0), 72.0)
        return sig
    return sig


def apply_uw_buy_boost(
    sig: Any,
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    flow_min_net_score: float = 8.0,
) -> Any:
    """Annotate / boost BUY_NOW strength when UW confirms the side."""
    if getattr(sig, "action", None) != "BUY_NOW":
        return sig
    sym = str(getattr(sig, "symbol", "") or "").upper()
    row = _leader_map(flow_leaders).get(sym)
    if not row or str(row.get("source") or "") != "unusual_whales":
        return sig
    sentiment = str(row.get("sentiment") or "neutral")
    net = float(row.get("net_flow_score") or 0)
    is_put = str(getattr(sig, "right", None) or "C").upper() == "P"
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


def apply_uw_market_tide(
    sig: Any,
    *,
    market_tide: dict[str, Any] | None = None,
    hard_block: bool = False,
) -> Any:
    """Market-wide UW tide overlay on BUY_NOW.

    Default is soft (annotate + strength haircut). Set hard_block=True to force WAIT
    on fresh calls when tide is strongly bearish.
    """
    if not market_tide or not market_tide.get("ok"):
        return sig
    if getattr(sig, "action", None) != "BUY_NOW":
        return sig
    is_put = str(getattr(sig, "right", None) or "C").upper() == "P"
    sentiment = str(market_tide.get("sentiment") or "neutral")
    tide_m = float(market_tide.get("tide_net") or 0) / 1e6
    if (not is_put) and sentiment == "bearish":
        if hard_block:
            sig.action = "WAIT"
            sig.headline = str(sig.headline or "").replace("BUY NOW", "WAIT", 1)
            sig.detail = (
                f"{sig.detail} · blocked: UW market-tide bearish "
                f"(call−put ${tide_m:+.1f}M)"
            )
            sig.strength = min(float(sig.strength or 0), 50.0)
        else:
            sig.detail = (
                f"{sig.detail} · UW tide bearish (${tide_m:+.1f}M) — prefer puts / wait rip"
            )
            sig.strength = min(float(sig.strength or 0), max(55.0, float(sig.strength or 0) - 10.0))
    elif is_put and sentiment == "bullish":
        sig.detail = (
            f"{sig.detail} · UW tide soft: market bullish — puts need stronger dump"
        )
    elif sentiment in {"bullish", "bearish"}:
        sig.detail = f"{sig.detail} · UW tide {sentiment} (${tide_m:+.1f}M)"
    return sig


def annotate_dict_with_uw(
    row: dict[str, Any],
    *,
    flow_leaders: list[dict[str, Any]] | None = None,
    market_tide: dict[str, Any] | None = None,
    darkpool_symbols: set[str] | list[str] | None = None,
    buy_actions: set[str] | None = None,
    right_default: str = "C",
    hard_block: bool = True,
    flow_min_net_score: float = 8.0,
) -> dict[str, Any]:
    """Annotate / soft-veto dict desk rows (RIP, Beauty, Levels, 0DTE $1K)."""
    buy_actions = buy_actions or {
        "BUY_NOW",
        "BUY_RIP",
        "BUY_BEAUTY",
        "BUY_LEVEL",
        "PUT_NOW",
        "CALL_NOW",
        "ENTRY",
    }
    out = dict(row)
    action = str(out.get("action") or out.get("alert_action") or "").upper()
    sym = str(out.get("symbol") or "").upper()
    if not sym or action not in buy_actions:
        return out
    right = str(out.get("right") or right_default).upper()
    is_put = right == "P" or action in {"PUT_NOW"}
    leaders = _leader_map(flow_leaders)
    uw = leaders.get(sym)
    detail = str(out.get("detail") or out.get("status_detail") or out.get("thesis") or "")
    strength = float(out.get("strength") or out.get("ensemble_score") or out.get("score") or 0)

    if uw and str(uw.get("source") or "") == "unusual_whales":
        sentiment = str(uw.get("sentiment") or "neutral")
        net = float(uw.get("net_flow_score") or 0)
        if (not is_put) and sentiment == "bearish" and net <= -flow_min_net_score:
            if hard_block:
                if action in {"BUY_NOW", "ENTRY"}:
                    out["action"] = "WAIT"
                elif action.startswith("BUY_"):
                    out["action"] = action.replace("BUY_", "WATCH_", 1)
                out["alert_action"] = "WAIT"
            detail = f"{detail} · UW veto: bearish flow net {net:+.0f}".strip(" ·")
            strength = min(strength, 48.0)
        elif is_put and sentiment == "bullish" and net >= flow_min_net_score:
            if hard_block:
                if action in {"BUY_NOW", "PUT_NOW", "ENTRY"}:
                    out["action"] = "WAIT"
                out["alert_action"] = "WAIT"
            detail = f"{detail} · UW veto: bullish call flow net {net:+.0f}".strip(" ·")
            strength = min(strength, 48.0)
        elif (not is_put) and sentiment == "bullish" and net >= flow_min_net_score:
            detail = f"{detail} · UW BUY confirm net {net:+.0f}".strip(" ·")
            strength = min(99.0, strength + 8.0)
        elif is_put and sentiment == "bearish" and net <= -flow_min_net_score:
            detail = f"{detail} · UW PUT confirm net {net:+.0f}".strip(" ·")
            strength = min(99.0, strength + 8.0)
        else:
            detail = f"{detail} · UW {sentiment} net {net:+.0f}".strip(" ·")

    dp = {str(s).upper() for s in (darkpool_symbols or [])}
    if sym in dp:
        detail = f"{detail} · UW dark-pool print today".strip(" ·")
        strength = min(99.0, strength + 3.0)

    if market_tide and market_tide.get("ok"):
        tide_s = str(market_tide.get("sentiment") or "neutral")
        tide_m = float(market_tide.get("tide_net") or 0) / 1e6
        if (not is_put) and tide_s == "bearish":
            detail = f"{detail} · UW tide bearish (${tide_m:+.1f}M)".strip(" ·")
            strength = min(strength, max(55.0, strength - 8.0))
        elif tide_s in {"bullish", "bearish"}:
            detail = f"{detail} · UW tide {tide_s} (${tide_m:+.1f}M)".strip(" ·")

    out["detail"] = detail
    if "status_detail" in out:
        out["status_detail"] = detail
    if "strength" in out or action.startswith("BUY") or action in {"PUT_NOW", "CALL_NOW"}:
        out["strength"] = strength
    out["uw_annotated"] = True
    return out
