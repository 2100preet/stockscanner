"""Call / put OI walls → soft exit levels for recommended trades.

Call wall = highest open-interest call strike at/above spot (resistance / dealer supply).
Put wall  = highest open-interest put strike at/below spot (support / dealer demand).

Soft exit default: **$0.10** before the wall on the underlying — take profit before
the magnet/defense zone where large OI often stalls price.
"""
from __future__ import annotations

import logging
from typing import Any

from odte_scanner.echo.gex import compute_gex_profile

logger = logging.getLogger(__name__)

# Soft exit buffer on the underlying before the OI wall (user request: ~10¢)
WALL_EXIT_BUFFER_USD = 0.10


def ladder_from_sides(
    *,
    symbol: str,
    spot: float,
    expiry: str | None,
    dte: int | None,
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "expiry": expiry,
        "dte": int(dte or 0),
        "spot": float(spot or 0),
        "calls": calls,
        "puts": puts,
    }


def ladder_from_yahoo_options(
    payload: dict[str, Any],
    *,
    symbol: str,
    spot: float,
    prefer_dte: int | None = None,
) -> dict[str, Any] | None:
    """Normalize a Yahoo crumb/v7 options payload into an Echo-style ladder."""
    from datetime import date, datetime, timezone

    options = payload.get("options") or []
    if not options:
        return None
    today = date.today()
    best = None
    best_score = 1e18
    for block in options:
        try:
            exp_ts = block.get("expirationDate")
            if exp_ts is None:
                continue
            exp_d = datetime.fromtimestamp(int(exp_ts), tz=timezone.utc).date()
            dte = (exp_d - today).days
            if dte < 0:
                continue
            score = abs(dte - prefer_dte) if prefer_dte is not None else dte
            if score < best_score:
                best_score = score
                best = (block, exp_d.isoformat(), dte)
        except Exception:  # noqa: BLE001
            continue
    if best is None:
        block = options[0]
        best = (block, None, 0)
    block, expiry, dte = best

    def _side(rows: list[dict[str, Any]], right: str) -> list[dict[str, Any]]:
        out = []
        for r in rows or []:
            try:
                k = float(r.get("strike") or 0)
            except (TypeError, ValueError):
                continue
            if k <= 0:
                continue
            out.append(
                {
                    "right": right,
                    "strike": k,
                    "bid": float(r.get("bid") or 0) or None,
                    "ask": float(r.get("ask") or 0) or None,
                    "last": float(r.get("lastPrice") or 0) or None,
                    "volume": int(r.get("volume") or 0),
                    "open_interest": int(r.get("openInterest") or 0),
                    "iv": r.get("impliedVolatility"),
                    "contract": r.get("contractSymbol"),
                }
            )
        return out

    calls = _side(block.get("calls") or [], "C")
    puts = _side(block.get("puts") or [], "P")
    if not calls and not puts:
        return None
    q = payload.get("quote") or {}
    live = q.get("regularMarketPrice") or q.get("postMarketPrice") or spot
    try:
        live_f = float(live)
    except (TypeError, ValueError):
        live_f = float(spot or 0)
    return ladder_from_sides(
        symbol=symbol,
        spot=live_f,
        expiry=expiry,
        dte=dte,
        calls=calls,
        puts=puts,
    )


def wall_exit_levels(
    *,
    right: str,
    spot: float | None,
    call_wall: float | None,
    put_wall: float | None,
    call_wall_oi: int | None = None,
    put_wall_oi: int | None = None,
    buffer_usd: float = WALL_EXIT_BUFFER_USD,
) -> dict[str, Any]:
    """Map walls → soft exit for a long CALL or long PUT recommendation."""
    right = (right or "C").upper()
    spot_f = float(spot or 0) or None
    buf = abs(float(buffer_usd))
    out: dict[str, Any] = {
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_oi": call_wall_oi,
        "put_wall_oi": put_wall_oi,
        "wall_buffer_usd": buf,
        "primary_wall": None,
        "primary_wall_side": None,
        "primary_wall_oi": None,
        "soft_exit": None,
        "wall_distance_usd": None,
        "wall_distance_pct": None,
        "exit_hint": "No OI wall located yet",
        "opposite_wall": None,
    }
    if right == "P":
        wall = put_wall
        side = "put"
        oi = put_wall_oi
        soft = round(float(wall) + buf, 2) if wall is not None else None
        out["opposite_wall"] = call_wall
        # For puts: price falling toward put wall — take profit ~10¢ above the wall
        hint_verb = "above"
    else:
        wall = call_wall
        side = "call"
        oi = call_wall_oi
        soft = round(float(wall) - buf, 2) if wall is not None else None
        out["opposite_wall"] = put_wall
        hint_verb = "below"

    out["primary_wall"] = wall
    out["primary_wall_side"] = side
    out["primary_wall_oi"] = oi
    out["soft_exit"] = soft
    if wall is not None and soft is not None:
        if spot_f and spot_f > 0:
            out["wall_distance_usd"] = round(abs(float(wall) - spot_f), 2)
            out["wall_distance_pct"] = round(abs(float(wall) - spot_f) / spot_f * 100.0, 2)
        oi_txt = f" (OI {int(oi):,})" if oi else ""
        out["exit_hint"] = (
            f"{'PUT' if right == 'P' else 'CALL'} wall ${float(wall):.2f}{oi_txt} — "
            f"soft EXIT underlying ~${soft:.2f} ({hint_verb} wall by ${buf:.2f})"
        )
    return out


def walls_from_ladder(
    ladder: dict[str, Any] | None,
    *,
    right: str = "C",
    buffer_usd: float = WALL_EXIT_BUFFER_USD,
) -> dict[str, Any]:
    if not ladder:
        return wall_exit_levels(
            right=right, spot=None, call_wall=None, put_wall=None, buffer_usd=buffer_usd
        )
    profile = compute_gex_profile(ladder)
    # Recover wall OI from by_strike (full ladder may be trimmed to near strikes)
    call_wall = profile.get("call_wall")
    put_wall = profile.get("put_wall")
    call_oi = None
    put_oi = None
    for row in profile.get("by_strike") or []:
        if call_wall is not None and float(row.get("strike") or 0) == float(call_wall):
            call_oi = int(row.get("call_oi") or 0)
        if put_wall is not None and float(row.get("strike") or 0) == float(put_wall):
            put_oi = int(row.get("put_oi") or 0)
    # If near-strike trim dropped the wall, scan raw ladder sides
    if call_wall is not None and call_oi is None:
        for r in ladder.get("calls") or []:
            if float(r.get("strike") or 0) == float(call_wall):
                call_oi = int(r.get("open_interest") or 0)
                break
    if put_wall is not None and put_oi is None:
        for r in ladder.get("puts") or []:
            if float(r.get("strike") or 0) == float(put_wall):
                put_oi = int(r.get("open_interest") or 0)
                break
    levels = wall_exit_levels(
        right=right,
        spot=profile.get("spot") or ladder.get("spot"),
        call_wall=float(call_wall) if call_wall is not None else None,
        put_wall=float(put_wall) if put_wall is not None else None,
        call_wall_oi=call_oi,
        put_wall_oi=put_oi,
        buffer_usd=buffer_usd,
    )
    levels.update(
        {
            "expiry": profile.get("expiry") or ladder.get("expiry"),
            "dte": profile.get("dte") if profile.get("dte") is not None else ladder.get("dte"),
            "flip": profile.get("flip"),
            "hvl": profile.get("hvl"),
            "regime": profile.get("regime"),
            "net_gex": profile.get("net_gex"),
            "source": "oi_wall",
        }
    )
    return levels


def fetch_walls_for_symbol(
    symbol: str,
    *,
    spot: float | None = None,
    right: str = "C",
    yahoo_symbol: str | None = None,
    prefer_dte: int | None = 21,
    max_dte: int = 45,
    buffer_usd: float = WALL_EXIT_BUFFER_USD,
    yahoo_chain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve walls from provided Yahoo chain, echo cache, or a fresh short ladder."""
    # 1) Explicit chain (challenge already fetched)
    if yahoo_chain:
        ladder = ladder_from_yahoo_options(
            yahoo_chain, symbol=symbol, spot=float(spot or 0), prefer_dte=prefer_dte
        )
        if ladder:
            return walls_from_ladder(ladder, right=right, buffer_usd=buffer_usd)

    # 2) Echo ladder cache / fetch (short-dated walls — still the main dealer magnets)
    try:
        from odte_scanner.echo.chain_ladder import fetch_option_ladder

        ladder = fetch_option_ladder(
            symbol,
            yahoo_symbol=yahoo_symbol,
            spot=spot,
            max_dte=max_dte,
            prefer_dte=prefer_dte,
            use_cache=True,
        )
        if ladder:
            return walls_from_ladder(ladder, right=right, buffer_usd=buffer_usd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("walls ladder %s: %s", symbol, exc)

    # 3) Crumb chain cache
    try:
        from odte_scanner.options.yahoo_session import fetch_option_chain

        chain = fetch_option_chain(symbol, yahoo_symbol=yahoo_symbol, use_cache=True)
        if chain:
            ladder = ladder_from_yahoo_options(
                chain, symbol=symbol, spot=float(spot or 0), prefer_dte=prefer_dte
            )
            if ladder:
                return walls_from_ladder(ladder, right=right, buffer_usd=buffer_usd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("walls yahoo chain %s: %s", symbol, exc)

    return wall_exit_levels(
        right=right, spot=spot, call_wall=None, put_wall=None, buffer_usd=buffer_usd
    )
