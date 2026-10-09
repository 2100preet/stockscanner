"""Explosive / lottery-ticket 0DTE–1DTE call upside estimates.

Flags cheap short-dated calls where a realistic underlying rip
(≈1–5%) can turn a small premium into multi-bagger option P&L —
the class of moves like a ~$6 SPY call exploding toward deep ITM.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

# Spot moves we stress for convexity (percent)
STRESS_MOVES_PCT = (1.0, 2.0, 3.0, 5.0)


@dataclass
class ExplosiveCandidate:
    symbol: str
    contract: str
    expiry: str
    dte: int
    strike: float
    spot: float
    ask: float
    bid: float
    moneyness_pct: float
    volume: int
    open_interest: int
    score: float
    # Estimated option mark if spot rises by X% (intrinsic floor + residual time value)
    upside_at_1pct: float
    upside_at_2pct: float
    upside_at_3pct: float
    upside_at_5pct: float
    mult_at_1pct: float
    mult_at_2pct: float
    mult_at_3pct: float
    mult_at_5pct: float
    best_mult: float
    best_move_pct: float
    lottery_score: float
    thesis: str
    dte_bucket: str = "0dte"

    @property
    def pct_gain_best(self) -> float:
        return round((self.best_mult - 1.0) * 100.0, 0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = (
            f"{'0DTE' if self.dte <= 1 else '1DTE'} "
            f"{self.expiry} {self.strike:g}c @ ${self.ask:.2f} → "
            f"up to {self.best_mult:.0f}× on +{self.best_move_pct:.0f}% rip"
        )
        d["pct_gain_best"] = self.pct_gain_best
        return d


def _est_option_after_move(
    *,
    spot: float,
    strike: float,
    ask: float,
    move_pct: float,
    dte: int,
) -> float:
    """Estimated mark after an upward spot move.

    Deep ITM → mostly intrinsic (this is how $cheap→$huge 0DTE days happen).
    Near/OTM → intrinsic floor plus shrinking extrinsic.
    """
    if spot <= 0 or ask <= 0:
        return 0.0
    new_spot = spot * (1.0 + move_pct / 100.0)
    intrinsic = max(0.0, new_spot - strike)
    itm_pct = (new_spot - strike) / new_spot * 100.0  # positive = ITM
    if itm_pct >= 1.5:
        # Deep ITM: trade near intrinsic (tiny residual on 0DTE)
        residual = max(0.05, ask * (0.05 if dte <= 0 else 0.12))
        return intrinsic + residual
    if itm_pct >= 0:
        residual = ask * (0.25 if dte <= 0 else 0.4)
        return max(intrinsic, intrinsic + residual * 0.5, ask * 0.9)
    # Still OTM — extrinsic only, decays with distance
    otm_pct = -itm_pct
    keep = max(0.05, 0.85 - otm_pct * 0.25) * (0.65 if dte <= 0 else 1.0)
    return max(ask * keep, intrinsic)


def score_lottery(
    *,
    ask: float,
    mult_2: float,
    mult_3: float,
    mult_5: float,
    moneyness_pct: float,
    dte: int,
    volume: int,
    open_interest: int,
    ensemble_score: float,
) -> float:
    """0–100 score favoring cheap convex 0DTE/1DTE tickets."""
    if ask <= 0:
        return 0.0
    # Prefer premium in the "can go parabolic" band (roughly $0.50–$15)
    if ask < 0.25:
        premium_fit = 35.0
    elif ask <= 8.0:
        premium_fit = 100.0
    elif ask <= 15.0:
        premium_fit = 75.0
    elif ask <= 25.0:
        premium_fit = 45.0
    else:
        premium_fit = 15.0

    # Convexity from stress multiples (3×=300%, 10×=1000%, 100×=10_000%)
    conv = 0.0
    for m, w in ((mult_2, 0.35), (mult_3, 0.4), (mult_5, 0.25)):
        if m >= 100:
            conv += 100.0 * w
        elif m >= 30:
            conv += 90.0 * w
        elif m >= 10:
            conv += 75.0 * w
        elif m >= 5:
            conv += 55.0 * w
        elif m >= 3:
            conv += 40.0 * w
        elif m >= 2:
            conv += 25.0 * w

    # Slightly OTM often has the juiciest 0DTE lottery payoff
    if -0.4 <= moneyness_pct <= 1.5:
        mny = 100.0
    elif -1.0 <= moneyness_pct <= 3.0:
        mny = 80.0
    elif moneyness_pct <= 5.0:
        mny = 55.0
    else:
        mny = 25.0

    dte_fit = 100.0 if dte <= 0 else (90.0 if dte == 1 else 40.0)
    liq = 40.0
    if volume >= 500 or open_interest >= 1000:
        liq = 100.0
    elif volume >= 50 or open_interest >= 200:
        liq = 75.0
    elif volume >= 10 or open_interest >= 50:
        liq = 55.0

    ens = min(100.0, max(0.0, ensemble_score))
    return round(
        0.28 * premium_fit
        + 0.34 * conv
        + 0.14 * mny
        + 0.12 * dte_fit
        + 0.07 * liq
        + 0.05 * ens,
        2,
    )


def build_explosive_from_candidate(
    c: dict[str, Any],
    *,
    min_best_mult: float = 3.0,
    min_mult_at_3pct: float = 2.5,
    min_mult_at_1pct: float = 0.0,
) -> ExplosiveCandidate | None:
    ask = float(c.get("ask") or 0)
    spot = float(c.get("spot") or c.get("live_spot") or 0)
    strike = float(c.get("strike") or 0)
    dte = int(c.get("dte") or 0)
    if ask <= 0 or spot <= 0 or strike <= 0:
        return None
    if dte > 1:
        return None  # focus true 0DTE / 1DTE lottery window

    ups: dict[float, float] = {}
    mults: dict[float, float] = {}
    for mv in STRESS_MOVES_PCT:
        est = _est_option_after_move(spot=spot, strike=strike, ask=ask, move_pct=mv, dte=dte)
        ups[mv] = round(est, 4)
        mults[mv] = round(est / ask, 2) if ask else 0.0

    best_move = max(STRESS_MOVES_PCT, key=lambda m: mults[m])
    best_mult = mults[best_move]
    # Need at least ~3× (≈+200%) on a 2–5% rip to count as "explosive"
    # Radar lane can pass a lower 1% mult floor for cheap near-money wings.
    if best_mult < min_best_mult and mults[3.0] < min_mult_at_3pct:
        if min_mult_at_1pct <= 0 or mults[1.0] < min_mult_at_1pct:
            return None

    lottery = score_lottery(
        ask=ask,
        mult_2=mults[2.0],
        mult_3=mults[3.0],
        mult_5=mults[5.0],
        moneyness_pct=float(c.get("moneyness_pct") or 0),
        dte=dte,
        volume=int(c.get("volume") or 0),
        open_interest=int(c.get("open_interest") or 0),
        ensemble_score=float(c.get("score") or 0),
    )
    thesis = (
        f"Cheap {dte}DTE call — if spot rips +{best_move:.0f}%, "
        f"est. mark ${ups[best_move]:.2f} (~{best_mult:.0f}× / +{(best_mult-1)*100:.0f}%). "
        f"Same class of convexity as rare $6→parabolic 0DTE days (not a guarantee)."
    )
    return ExplosiveCandidate(
        symbol=str(c.get("symbol")),
        contract=str(c.get("contract") or ""),
        expiry=str(c.get("expiry") or ""),
        dte=dte,
        strike=strike,
        spot=spot,
        ask=ask,
        bid=float(c.get("bid") or 0),
        moneyness_pct=float(c.get("moneyness_pct") or 0),
        volume=int(c.get("volume") or 0),
        open_interest=int(c.get("open_interest") or 0),
        score=float(c.get("score") or 0),
        upside_at_1pct=ups[1.0],
        upside_at_2pct=ups[2.0],
        upside_at_3pct=ups[3.0],
        upside_at_5pct=ups[5.0],
        mult_at_1pct=mults[1.0],
        mult_at_2pct=mults[2.0],
        mult_at_3pct=mults[3.0],
        mult_at_5pct=mults[5.0],
        best_mult=best_mult,
        best_move_pct=best_move,
        lottery_score=lottery,
        thesis=thesis,
        dte_bucket="0dte" if dte <= 1 else "weekly",
    )


def find_explosive_calls(
    symbol: str,
    spot: float,
    *,
    score: float = 0.0,
    max_dte: int = 1,
    otm_pct_max: float = 4.0,
    itm_pct_max: float = 0.8,
    max_ask: float = 15.0,
    min_ask: float = 0.35,
    yahoo_symbol: str | None = None,
    limit: int = 4,
    min_best_mult: float = 3.0,
    min_mult_at_3pct: float = 2.5,
    min_mult_at_1pct: float = 0.0,
) -> list[ExplosiveCandidate]:
    """Pull wider OTM short-dated calls optimized for lottery convexity."""
    fetch_sym = yahoo_symbol or symbol
    if spot <= 0:
        return []
    try:
        t = yf.Ticker(fetch_sym)
        expirations = list(t.options or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("explosive chain unavailable %s: %s", fetch_sym, exc)
        return []
    if not expirations:
        return []

    today = __import__("datetime").datetime.now().date()
    out: list[ExplosiveCandidate] = []
    for exp in expirations[:8]:
        try:
            dte = (__import__("datetime").datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte < 0 or dte > max_dte:
            continue
        try:
            calls = t.option_chain(exp).calls
        except Exception:  # noqa: BLE001
            continue
        if calls is None or calls.empty:
            continue
        for _, row in calls.iterrows():
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            mny = (strike - spot) / spot * 100
            if mny < -itm_pct_max or mny > otm_pct_max:
                continue
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last_px = float(row.get("lastPrice") or 0)
            if ask <= 0 and last_px > 0:
                ask = last_px
                bid = bid or last_px * 0.95
            if ask < min_ask or ask > max_ask:
                continue
            contract = str(row.get("contractSymbol") or "")
            if not contract:
                continue
            raw = {
                "symbol": symbol,
                "contract": contract,
                "expiry": exp,
                "dte": dte,
                "strike": strike,
                "spot": spot,
                "ask": ask,
                "bid": bid,
                "moneyness_pct": mny,
                "volume": int(row.get("volume") or 0),
                "open_interest": int(row.get("openInterest") or 0),
                "score": score,
            }
            ec = build_explosive_from_candidate(
                raw,
                min_best_mult=min_best_mult,
                min_mult_at_3pct=min_mult_at_3pct,
                min_mult_at_1pct=min_mult_at_1pct,
            )
            if ec:
                out.append(ec)

    out.sort(key=lambda x: (x.lottery_score, x.best_mult), reverse=True)
    # de-dupe by strike/expiry
    seen: set[tuple[str, float]] = set()
    uniq: list[ExplosiveCandidate] = []
    for ec in out:
        key = (ec.expiry, ec.strike)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ec)
        if len(uniq) >= limit:
            break
    return uniq


def build_radar_wing_board(
    *,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    focus_symbols: list[str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    min_ask: float = 0.15,
    max_ask: float = 2.50,
    otm_pct_max: float = 1.50,
    itm_pct_max: float = 0.40,
    per_symbol: int = 3,
    max_total: int = 18,
    enrich_live: bool = True,
) -> list[dict[str, Any]]:
    """Cheap near-money 0DTE wings for Discord-style radar (SPY/QQQ/…).

    Uses a lower ask floor than the main explosive board so $0.20–$0.35
    index calls (Mike-style) can surface without diluting BUY NOW.
    """
    aliases = aliases or {}
    quotes = quotes or {}
    focus = [str(s).upper() for s in (focus_symbols or ["SPY", "QQQ", "IWM", "DIA"])]
    score_map = {
        str(s.get("symbol")): float(s.get("ensemble_score") or 0) for s in (scores or [])
    }
    board: list[ExplosiveCandidate] = []
    have: set[tuple[str, str, float]] = set()

    # Fold any already-fetched cheap candidates that fit the wing band
    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if focus and sym not in focus:
            continue
        ask = float(c.get("ask") or 0)
        if ask < min_ask or ask > max_ask:
            continue
        dte = c.get("dte")
        if dte is not None and int(dte) > 1:
            continue
        ec = build_explosive_from_candidate(
            {**c, "symbol": sym},
            min_best_mult=2.2,
            min_mult_at_3pct=1.8,
            min_mult_at_1pct=1.5,
        )
        if not ec:
            continue
        key = (ec.symbol, ec.expiry, ec.strike)
        if key in have:
            continue
        have.add(key)
        board.append(ec)

    if enrich_live:
        for sym in focus:
            q = quotes.get(sym) or {}
            spot = float(q.get("last") or 0)
            if spot <= 0:
                for c in candidates or []:
                    if str(c.get("symbol") or "").upper() == sym and c.get("spot"):
                        spot = float(c["spot"])
                        break
            if spot <= 0:
                # Last-resort Yahoo last for focus indices so Pages still sees wings
                try:
                    t = yf.Ticker(aliases.get(sym) or sym)
                    hist = t.history(period="1d", interval="1m")
                    if hist is not None and not hist.empty:
                        spot = float(hist["Close"].iloc[-1])
                    else:
                        info_fast = getattr(t, "fast_info", None)
                        if info_fast is not None:
                            spot = float(getattr(info_fast, "last_price", 0) or 0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("radar spot unavailable %s: %s", sym, exc)
                    spot = 0.0
            if spot <= 0:
                continue
            found = find_explosive_calls(
                sym,
                spot,
                score=score_map.get(sym, 0.0),
                yahoo_symbol=aliases.get(sym),
                min_ask=min_ask,
                max_ask=max_ask,
                otm_pct_max=otm_pct_max,
                itm_pct_max=itm_pct_max,
                limit=per_symbol,
                min_best_mult=2.2,
                min_mult_at_3pct=1.8,
                min_mult_at_1pct=1.5,
            )
            for ec in found:
                key = (ec.symbol, ec.expiry, ec.strike)
                if key in have:
                    continue
                have.add(key)
                board.append(ec)

    board.sort(key=lambda x: (x.lottery_score, x.best_mult), reverse=True)
    return [e.to_dict() for e in board[:max_total]]


def _pick_chase_symbols(
    scores: list[dict[str, Any]] | None,
    quotes: dict[str, dict[str, Any]] | None,
    *,
    max_n: int = 10,
    min_ensemble: float = 55.0,
) -> list[str]:
    """Prefer names that are already ripping or scoring soft-bullish for chase wings."""
    quotes = quotes or {}
    try:
        from odte_scanner.signals.rip_radar import is_mega_rip_symbol
    except Exception:  # noqa: BLE001
        def is_mega_rip_symbol(symbol: str) -> bool:  # type: ignore[misc]
            return False

    ranked: list[tuple[float, str]] = []
    for s in scores or []:
        sym = str(s.get("symbol") or "").upper()
        if not sym:
            continue
        ens = float(s.get("ensemble_score") or 0)
        em = float(s.get("expected_move_pct") or 0)
        rs = 0.0
        for sig in s.get("signals") or []:
            if str(sig.get("name") or "") == "relative_strength":
                rs = float((sig.get("details") or {}).get("ret_pct") or 0)
                break
        q = quotes.get(sym) or {}
        live = q.get("session_change_pct")
        if live is None:
            live = q.get("change_pct")
        live_f = float(live) if live is not None else 0.0
        mega = is_mega_rip_symbol(sym)
        # Megas with EM fuel (MU open ~score 45 / EM 3%) must not be excluded
        mega_early = mega and (ens >= 40.0 or em >= 2.0)
        if ens < min_ensemble and live_f < 1.2 and rs < 2.5 and not mega_early:
            continue
        chase_rank = ens + live_f * 4.0 + max(0.0, rs) * 0.6 + (em * 2.0 if mega else 0.0)
        ranked.append((chase_rank, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _, sym in ranked:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= max_n:
            break
    return out


def build_chase_wing_board(
    *,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    focus_symbols: list[str] | None = None,
    min_ask: float = 0.20,
    max_ask: float = 12.0,
    otm_pct_max: float = 8.0,
    itm_pct_max: float = 0.5,
    per_symbol: int = 2,
    max_total: int = 16,
    enrich_live: bool = True,
    max_live_symbols: int = 8,
) -> list[dict[str, Any]]:
    """Far-OTM / richer-ask convex wings for the chase-aware lane (MU-style runners)."""
    aliases = aliases or {}
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol")): float(s.get("ensemble_score") or 0) for s in (scores or [])
    }
    board: list[ExplosiveCandidate] = []
    have: set[tuple[str, str, float]] = set()

    focus_set = {str(s).upper() for s in (focus_symbols or [])} if focus_symbols else None

    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if focus_set and sym not in focus_set:
            continue
        ask = float(c.get("ask") or 0)
        if ask < min_ask or ask > max_ask:
            continue
        dte = c.get("dte")
        if dte is not None and int(dte) > 1:
            continue
        mny = c.get("moneyness_pct")
        if mny is not None and float(mny) > otm_pct_max:
            continue
        ec = build_explosive_from_candidate(
            {**c, "symbol": sym},
            min_best_mult=3.5,
            min_mult_at_3pct=3.0,
            min_mult_at_1pct=1.2,
        )
        if not ec:
            continue
        key = (ec.symbol, ec.expiry, ec.strike)
        if key in have:
            continue
        have.add(key)
        board.append(ec)

    if enrich_live:
        live_syms = list(focus_symbols or []) if focus_symbols else []
        if not live_syms:
            live_syms = _pick_chase_symbols(scores, quotes, max_n=max_live_symbols)
        for sym in live_syms[:max_live_symbols]:
            q = quotes.get(sym) or {}
            spot = float(q.get("last") or 0)
            if spot <= 0:
                for c in candidates or []:
                    if str(c.get("symbol") or "").upper() == sym and c.get("spot"):
                        spot = float(c["spot"])
                        break
            if spot <= 0:
                # Fall back to score last_price from scan
                for s in scores or []:
                    if str(s.get("symbol") or "").upper() == sym and s.get("last_price"):
                        spot = float(s["last_price"])
                        break
            if spot <= 0:
                continue
            found = find_explosive_calls(
                sym,
                spot,
                score=score_map.get(sym, 0.0),
                yahoo_symbol=aliases.get(sym),
                min_ask=min_ask,
                max_ask=max_ask,
                otm_pct_max=otm_pct_max,
                itm_pct_max=itm_pct_max,
                limit=per_symbol,
                min_best_mult=3.5,
                min_mult_at_3pct=3.0,
                min_mult_at_1pct=1.2,
            )
            for ec in found:
                key = (ec.symbol, ec.expiry, ec.strike)
                if key in have:
                    continue
                have.add(key)
                board.append(ec)

    board.sort(key=lambda x: (x.lottery_score, x.best_mult), reverse=True)
    return [e.to_dict() for e in board[:max_total]]


def _mega_wing_symbols(
    scores: list[dict[str, Any]] | None,
    *,
    max_n: int = 8,
    min_score: float = 40.0,
    min_expected_move_pct: float = 2.0,
) -> list[tuple[str, float, float]]:
    """MU-class names that can rip from a mid score — always worth a cheap-wing sweep.

    Returns (symbol, ensemble_score, spot_hint) ranked by expected-move then score.
    """
    try:
        from odte_scanner.signals.rip_radar import is_mega_rip_symbol
    except Exception:  # noqa: BLE001
        return []

    ranked: list[tuple[float, str, float, float]] = []
    for s in scores or []:
        sym = str(s.get("symbol") or "").upper()
        if not sym or not is_mega_rip_symbol(sym):
            continue
        ens = float(s.get("ensemble_score") or 0)
        em = float(s.get("expected_move_pct") or 0)
        spot = float(s.get("last_price") or s.get("last") or s.get("entry") or 0)
        if ens < min_score and em < min_expected_move_pct:
            continue
        # Prefer elevated EM (rip fuel) even when ensemble is only mid-40s
        rank = em * 8.0 + ens
        ranked.append((rank, sym, ens, spot))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for _, sym, ens, spot in ranked:
        if sym in seen:
            continue
        seen.add(sym)
        out.append((sym, ens, spot))
        if len(out) >= max_n:
            break
    return out


def build_explosive_board(
    candidates: list[dict[str, Any]],
    *,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    enrich_live: bool = True,
    per_symbol: int = 2,
    max_total: int = 24,
    mega_enrich: bool | None = None,
) -> list[dict[str, Any]]:
    """Combine scan candidates + optional wider chain search into a ranked board.

    Even when ``enrich_live=False`` (fast snapshot path), mega names with elevated
    expected move still get a cheap OTM wing sweep — otherwise MU @ score 45 only
    surfaces puts and the $1–$2 call lottery never appears at the open.
    """
    aliases = aliases or {}
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol")): float(s.get("ensemble_score") or 0) for s in (scores or [])
    }

    board: list[ExplosiveCandidate] = []
    for c in candidates or []:
        dte = c.get("dte")
        if dte is not None and int(dte) > 1:
            continue
        # Explosive / lottery lane is call convexity — skip puts from the weak sleeve
        if str(c.get("right") or "C").upper().startswith("P"):
            continue
        ec = build_explosive_from_candidate(c)
        if ec:
            board.append(ec)

    have = {(e.symbol, e.expiry, e.strike) for e in board}

    def _spot_for(sym: str, hint: float = 0.0) -> float:
        q = quotes.get(sym) or {}
        spot = float(q.get("last") or 0)
        if spot <= 0:
            spot = float(hint or 0)
        if spot <= 0:
            for c in candidates or []:
                if str(c.get("symbol") or "").upper() == sym and c.get("spot"):
                    spot = float(c["spot"])
                    break
        return spot

    def _absorb(found: list[ExplosiveCandidate]) -> None:
        for ec in found:
            key = (ec.symbol, ec.expiry, ec.strike)
            if key in have:
                continue
            have.add(key)
            board.append(ec)

    # Default ON even when enrich_live=False — megas must not be puts-only at the open.
    if mega_enrich is not False:
        for sym, sc, hint in _mega_wing_symbols(scores, max_n=8):
            spot = _spot_for(sym, hint)
            if spot <= 0:
                continue
            found = find_explosive_calls(
                sym,
                spot,
                score=sc,
                yahoo_symbol=aliases.get(sym),
                otm_pct_max=5.0,
                min_ask=0.25,
                max_ask=8.0,
                limit=per_symbol,
                min_best_mult=2.5,
                min_mult_at_3pct=2.0,
            )
            _absorb(found)

    if enrich_live:
        # Top scored names get a wider OTM lottery sweep
        ranked_syms = sorted(score_map.items(), key=lambda kv: kv[1], reverse=True)[:12]
        for sym, sc in ranked_syms:
            spot = _spot_for(sym)
            if spot <= 0:
                continue
            found = find_explosive_calls(
                sym,
                spot,
                score=sc,
                yahoo_symbol=aliases.get(sym),
                limit=per_symbol,
            )
            _absorb(found)

    board.sort(key=lambda x: (x.lottery_score, x.best_mult), reverse=True)
    return [e.to_dict() for e in board[:max_total]]
