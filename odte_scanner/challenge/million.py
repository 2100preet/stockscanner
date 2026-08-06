"""$1,000 → $1,000,000 challenge path via swing / LEAP calls & puts.

Selects high hist-win names, recommends strike + expiry, hold period, and
ENTRY / HOLD / EXIT status for both calls and puts.

IMPORTANT: "100% filter" means *historical* quality-signal win rate on the
underlying — not a guarantee of future option P&L. Options can go to zero.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from odte_scanner.backtest.win_rates import summarize_hist_win_gate
from odte_scanner.challenge.earnings import earnings_map_for
from odte_scanner.challenge.tracker import hold_period_for
from odte_scanner.data.universe import market_cap_tier

logger = logging.getLogger(__name__)


@dataclass
class ChallengeTicket:
    symbol: str
    horizon: str
    right: str  # C | P
    action: str  # ENTRY | HOLD | EXIT | WAIT
    hist_win_pct: float
    hist_samples: int
    hit_1pct: float | None
    hit_2pct: float | None
    ensemble_score: float | None
    quality: bool
    contract: str | None
    expiry: str | None
    dte: int | None
    strike: float | None
    spot: float | None
    ask: float | None
    bid: float | None
    moneyness_pct: float | None
    open_interest: int | None
    volume: int | None
    contracts_for_bankroll: int
    debit_usd: float
    target_premium_mult: float
    target_ask: float | None
    hold_period_label: str
    hold_approx_label: str  # e.g. ≈55d (30–90d)
    hold_min_days: int
    hold_max_days: int
    hold_ideal_days: int
    approx_hold_days: int
    hold_days: float | None
    trade_id: str | None
    thesis: str
    recommend_reason: str
    reasons: list[str]
    certainty_tier: str  # perfect | elite | strong
    status_detail: str
    market_cap_tier: str
    earnings_window: str
    earnings_label: str
    next_earnings: str | None
    last_earnings: str | None
    days_to_earnings: int | None
    days_since_earnings: int | None
    spot_source: str  # live | cache | scan | none
    quote_asof: str | None
    live_ok: bool
    data_note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compound_path(
    *,
    start_usd: float = 1000.0,
    target_usd: float = 1_000_000.0,
    flips: int = 12,
) -> dict[str, Any]:
    if flips <= 0 or start_usd <= 0:
        return {"flips": flips, "mult_per_flip": None, "pct_per_flip": None, "schedule": []}
    need = target_usd / start_usd
    mult = need ** (1.0 / flips)
    pct = (mult - 1.0) * 100.0
    equity = start_usd
    schedule = []
    for i in range(1, flips + 1):
        equity *= mult
        schedule.append({"flip": i, "equity": round(equity, 2), "gain_pct": round(pct, 1)})
    return {
        "start_usd": start_usd,
        "target_usd": target_usd,
        "flips": flips,
        "total_multiple": round(need, 2),
        "mult_per_flip": round(mult, 4),
        "pct_per_flip": round(pct, 1),
        "schedule": schedule,
        "note": (
            f"Need ~{pct:.0f}% option-premium gain on each of {flips} flips "
            f"(compound) to grow ${start_usd:,.0f} → ${target_usd:,.0f}."
        ),
    }


def path_table(start_usd: float = 1000.0, target_usd: float = 1_000_000.0) -> list[dict[str, Any]]:
    return [compound_path(start_usd=start_usd, target_usd=target_usd, flips=n) for n in range(10, 16)]


def _tier(win: float, n: int) -> str:
    if win >= 99.9 and n >= 3:
        return "perfect"
    if win >= 85 and n >= 5:
        return "elite"
    if win >= 80 and n >= 5:
        return "strong"
    return "watch"


def _eligible_rows(
    win_table: dict[str, Any] | None,
    *,
    prefer_perfect: bool = True,
) -> list[dict[str, Any]]:
    """Prefer 100% hist-win rows; fall back to ≥80% with n≥5 on swing/weekly."""
    perfect = summarize_hist_win_gate(
        win_table, min_hist_win_pct=100.0, min_hist_win_samples=3, horizons=["swing", "weekly"]
    )
    elite = summarize_hist_win_gate(
        win_table, min_hist_win_pct=80.0, min_hist_win_samples=5, horizons=["swing", "weekly"]
    )
    rows = list(perfect.get("eligible") or [])
    seen = {(r["symbol"], r["horizon"]) for r in rows}
    if not prefer_perfect or len(rows) < 4:
        for r in elite.get("eligible") or []:
            key = (r["symbol"], r["horizon"])
            if key not in seen:
                rows.append(r)
                seen.add(key)
    rows.sort(
        key=lambda r: (
            0 if r.get("horizon") == "swing" else 1,
            0 if float(r.get("win_pct") or 0) >= 99.9 else 1,
            -float(r.get("win_pct") or 0),
            -int(r.get("trades") or 0),
        )
    )
    return rows


def _side_from_tape(
    *,
    score: dict[str, Any] | None,
    quote: dict[str, Any] | None,
) -> str:
    """Return C (call) or P (put) from ensemble + live tape."""
    sc = score or {}
    q = quote or {}
    ens = float(sc.get("ensemble_score") or 0)
    bullish = sc.get("bullish")
    if bullish is None:
        bullish = ens >= 62
    mom5 = q.get("mom_5m_pct")
    live = q.get("session_change_pct")
    if live is None:
        live = q.get("change_pct")

    bear_votes = 0
    bull_votes = 0
    if bullish:
        bull_votes += 1
    else:
        bear_votes += 1
    if ens >= 70:
        bull_votes += 1
    elif ens and ens < 48:
        bear_votes += 1
    if mom5 is not None:
        if float(mom5) <= -0.15:
            bear_votes += 1
        elif float(mom5) >= 0.12:
            bull_votes += 1
    if live is not None:
        if float(live) <= -1.0:
            bear_votes += 1
        elif float(live) >= 0.6:
            bull_votes += 1
    return "P" if bear_votes > bull_votes else "C"


def select_leap_option(
    symbol: str,
    spot: float,
    *,
    right: str = "C",
    yahoo_symbol: str | None = None,
    min_dte: int = 90,
    max_dte: int = 450,
    otm_pct_max: float = 8.0,
    itm_pct_max: float = 2.0,
    max_ask: float = 80.0,
    min_oi: int = 50,
) -> dict[str, Any] | None:
    """Pick a liquid swing/LEAP call or put — slight OTM, mid-dated."""
    right = right.upper()
    fetch_sym = yahoo_symbol or symbol
    try:
        t = yf.Ticker(fetch_sym)
        exps = list(t.options or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("LEAP chain fail %s: %s", symbol, exc)
        return None
    if not exps or spot <= 0:
        return None

    today = datetime.now().date()
    targets: list[tuple[str, int]] = []
    for exp in exps:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - today).days
        if min_dte <= dte <= max_dte:
            targets.append((exp, dte))
    if not targets:
        for exp in exps:
            try:
                d = datetime.strptime(exp, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (d - today).days
            if 60 <= dte <= 550:
                targets.append((exp, dte))
    if not targets:
        return None

    prefer_dte = 180
    targets.sort(key=lambda x: abs(x[1] - prefer_dte))
    best: dict[str, Any] | None = None
    best_rank = -1e18

    for expiry, dte in targets[:6]:
        try:
            chain = t.option_chain(expiry)
            table = chain.calls if right == "C" else chain.puts
        except Exception:  # noqa: BLE001
            continue
        if table is None or table.empty:
            continue
        for _, row in table.iterrows():
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            # Calls: strike above spot = OTM (+); Puts: strike below spot = OTM (− as moneyness_pct)
            if right == "C":
                mny = (strike - spot) / spot * 100.0
                if mny < -itm_pct_max or mny > otm_pct_max:
                    continue
                otm_target = 3.0
            else:
                mny = (strike - spot) / spot * 100.0  # negative when OTM put
                if mny > itm_pct_max or mny < -otm_pct_max:
                    continue
                otm_target = -3.0
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            if ask <= 0 and last > 0:
                ask = last
                bid = bid or last * 0.95
            if ask <= 0 or ask > max_ask:
                continue
            oi = int(row.get("openInterest") or 0)
            vol = int(row.get("volume") or 0)
            if oi < min_oi and vol < 20:
                continue
            spread = (ask - bid) / ask if ask else 1.0
            if spread > 0.35:
                continue
            rank = (
                40.0
                - abs(mny - otm_target) * 4.0
                - abs(dte - prefer_dte) * 0.05
                - spread * 30.0
                + min(20.0, oi / 500.0)
                + min(10.0, vol / 100.0)
            )
            if rank > best_rank:
                best_rank = rank
                best = {
                    "symbol": symbol,
                    "right": right,
                    "contract": str(row.get("contractSymbol") or ""),
                    "expiry": expiry,
                    "dte": dte,
                    "strike": strike,
                    "spot": spot,
                    "bid": round(bid, 2),
                    "ask": round(ask, 2),
                    "moneyness_pct": round(mny, 3),
                    "open_interest": oi,
                    "volume": vol,
                    "style": "leap" if dte >= 180 else "swing",
                }
    return best


# Back-compat alias
def select_leap_call(symbol: str, spot: float, **kwargs: Any) -> dict[str, Any] | None:
    return select_leap_option(symbol, spot, right="C", **kwargs)


def _suggested_zone(spot: float, horizon: str, right: str) -> dict[str, Any]:
    step = 1.0 if spot < 50 else (2.5 if spot < 200 else 5.0)
    if right == "C":
        raw = spot * (1.03 if horizon == "weekly" else 1.05)
    else:
        raw = spot * (0.97 if horizon == "weekly" else 0.95)
    strike_zone = round(round(raw / step) * step, 2)
    dte_zone = 120 if horizon == "weekly" else 180
    return {
        "contract": None,
        "right": right,
        "expiry": f"~{dte_zone}DTE listed",
        "dte": dte_zone,
        "strike": strike_zone,
        "spot": spot,
        "bid": None,
        "ask": None,
        "moneyness_pct": round((strike_zone - spot) / spot * 100.0, 2),
        "open_interest": None,
        "volume": None,
        "suggested_zone": True,
    }


def _side_reason(right: str, score: dict[str, Any], quote: dict[str, Any]) -> str:
    ens = score.get("ensemble_score")
    mom5 = quote.get("mom_5m_pct")
    live = quote.get("session_change_pct")
    if live is None:
        live = quote.get("change_pct")
    bits = []
    if ens is not None:
        bits.append(f"ensemble {float(ens):.0f}")
    if score.get("bullish") is True:
        bits.append("bullish quality")
    elif score.get("bullish") is False:
        bits.append("bearish quality")
    if mom5 is not None:
        bits.append(f"5m {float(mom5):+.2f}%")
    if live is not None:
        bits.append(f"session {float(live):+.2f}%")
    side = "CALL" if right == "C" else "PUT"
    return f"{side} from tape ({', '.join(bits) or 'default bias'})"


def _build_reasons(
    *,
    tier: str,
    win: float,
    n: int,
    right: str,
    horizon: str,
    hp: dict[str, Any],
    need_mult: float,
    score: dict[str, Any],
    quote: dict[str, Any],
    row: dict[str, Any],
    cap_tier: str,
    earn: dict[str, Any],
    contract: dict[str, Any] | None,
    action: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    reasons.append(
        f"{'PERFECT' if tier == 'perfect' else tier.upper()} hist-win {win:.0f}% "
        f"(n={n}) on {horizon} quality signals"
    )
    reasons.append(_side_reason(right, score, quote))
    reasons.append(f"Hold window {hp['label']} — EXIT at ~{need_mult:.2f}× premium, stop, or max hold")
    if row.get("hit_1pct") is not None:
        reasons.append(f"Underlying strike-rate ≥1%: {float(row['hit_1pct']):.0f}%")
    if row.get("hit_2pct") is not None:
        reasons.append(f"Underlying strike-rate ≥2%: {float(row['hit_2pct']):.0f}%")
    if score.get("quality"):
        reasons.append("Live quality gate confirmed on scan")
    if score.get("reasons"):
        for r in list(score.get("reasons") or [])[:2]:
            reasons.append(f"Scan: {r}")

    cap_lbl = {
        "mega_large": "Mega/large-cap liquidity",
        "mid": "Mid-cap — larger move potential, still optionable",
        "small": "Small-cap — high beta / wider risk; size carefully",
        "etf": "Liquid ETF — cleaner tape, lower single-name gap risk",
    }.get(cap_tier, "Cap tier unknown")
    reasons.append(cap_lbl)

    window = earn.get("window") or "none"
    if window == "post_earnings":
        reasons.append(
            f"Post-earnings catalyst: {earn.get('label')} — prefer directional continuation "
            f"{'CALL' if right == 'C' else 'PUT'} after IV crush"
        )
    elif window == "pre_earnings":
        reasons.append(
            f"Pre-earnings caution: {earn.get('label')} — prefer LEAP DTE; avoid short weekly lottery"
        )
    elif window == "earnings_day":
        reasons.append(f"Earnings day risk: {earn.get('label')}")
    elif earn.get("next_earnings"):
        reasons.append(f"Next earnings {earn.get('next_earnings')} (not inside pre/post window)")

    if contract and contract.get("suggested_zone"):
        reasons.append("Chain quote unavailable — strike/DTE is a suggested zone")
    if action == "WAIT":
        reasons.append("Sleeve already has an open flip — WAIT before new ENTRY")

    # One-line headline reason
    headline_bits = [f"{tier.upper()} {win:.0f}% hist (n={n})", "CALL" if right == "C" else "PUT"]
    if window == "post_earnings":
        headline_bits.append("post-earnings continuation")
    elif window == "pre_earnings":
        headline_bits.append("pre-earnings LEAP only")
    if cap_tier in {"mid", "small"}:
        headline_bits.append(f"{cap_tier}-cap")
    headline_bits.append(f"hold {hp['label']}")
    return " · ".join(headline_bits), reasons


def build_challenge_board(
    *,
    win_table: dict[str, Any] | None,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    start_usd: float = 1000.0,
    target_usd: float = 1_000_000.0,
    flips: int = 12,
    max_tickets: int = 8,
    fetch_contracts: bool = True,
    fetch_earnings: bool = True,
) -> dict[str, Any]:
    quotes = quotes or {}
    aliases = aliases or {}
    scores = scores or []
    open_trades = open_trades or []
    open_map = {
        (str(t.get("symbol")), str(t.get("right") or "C").upper()): t
        for t in open_trades
        if t.get("status", "open") == "open"
    }

    score_map: dict[str, dict[str, Any]] = {}
    for s in scores:
        sym = str(s.get("symbol") or "")
        hz = str(s.get("horizon") or "")
        if not sym:
            continue
        prev = score_map.get(sym)
        if prev is None or hz == "swing" or (
            prev.get("_hz") != "swing"
            and float(s.get("ensemble_score") or 0) >= float(prev.get("ensemble_score") or 0)
        ):
            row = dict(s)
            row["_hz"] = hz
            score_map[sym] = row

    paths = path_table(start_usd, target_usd)
    primary_path = compound_path(start_usd=start_usd, target_usd=target_usd, flips=flips)
    need_mult = float(primary_path["mult_per_flip"] or 1.8)

    eligible = _eligible_rows(win_table)
    # Prefetch earnings for top candidates (cached; skips network when fetch_earnings=False)
    earn_syms = [str(r["symbol"]) for r in eligible[: max_tickets + 6]]
    earn_map = earnings_map_for(
        earn_syms,
        aliases=aliases,
        fetch=fetch_earnings,
        max_fetch=min(10, max_tickets + 2),
    )

    tickets: list[ChallengeTicket] = []
    chain_fetches = 0
    earn_boosts: dict[str, int] = {}

    for row in eligible[: max_tickets + 6]:
        sym = str(row["symbol"])
        q = quotes.get(sym) or {}
        sc = score_map.get(sym) or {}
        earn = earn_map.get(sym) or {}
        earn_boosts[sym] = int(earn.get("boost") or 0)
        cap_tier = market_cap_tier(sym)
        spot = None
        spot_source = "none"
        quote_asof = q.get("asof")
        sess = str(q.get("session") or "")
        if q.get("last") is not None:
            spot = float(q["last"])
            spot_source = "cache" if sess == "cache" else "live"
        elif sc.get("last_price") is not None:
            spot = float(sc["last_price"])
            spot_source = "scan"
        elif sc.get("entry") is not None:
            spot = float(sc["entry"])
            spot_source = "scan"
        live_ok = spot_source == "live"

        right = _side_from_tape(score=sc, quote=q)
        horizon = str(row.get("horizon") or "swing")
        # Pre-earnings / earnings day → force longer-dated LEAP style
        prefer_leap = bool(earn.get("prefer_leap"))
        if prefer_leap and horizon == "weekly":
            horizon = "swing"
        hp = hold_period_for(horizon, 200 if prefer_leap else None)

        open_t = open_map.get((sym, right))
        # Also match opposite-side open → manage that first
        if open_t is None:
            for rtry in ("C", "P"):
                if (sym, rtry) in open_map:
                    open_t = open_map[(sym, rtry)]
                    right = rtry
                    break

        contract = None
        if fetch_contracts and spot and spot > 0 and chain_fetches < 4 and not open_t:
            try:
                min_dte = 150 if prefer_leap else (60 if horizon == "weekly" else 120)
                max_dte = 450 if prefer_leap or horizon != "weekly" else 220
                contract = select_leap_option(
                    sym,
                    spot,
                    right=right,
                    yahoo_symbol=aliases.get(sym),
                    min_dte=min_dte,
                    max_dte=max_dte,
                    otm_pct_max=8.0 if prefer_leap or horizon != "weekly" else 6.0,
                )
                chain_fetches += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("challenge contract fetch %s: %s", sym, exc)
                contract = None

        if contract is None and spot and spot > 0:
            zone_hz = "swing" if prefer_leap else horizon
            contract = _suggested_zone(spot, zone_hz, right)
            if prefer_leap:
                contract["dte"] = max(int(contract.get("dte") or 0), 180)
                contract["expiry"] = f"~{contract['dte']}DTE listed"
            if open_t:
                # Prefer live open trade contract fields
                contract = {
                    "contract": open_t.get("contract"),
                    "right": open_t.get("right") or right,
                    "expiry": open_t.get("expiry"),
                    "dte": open_t.get("dte_at_entry"),
                    "strike": open_t.get("strike"),
                    "spot": spot,
                    "bid": open_t.get("mark") or open_t.get("exit_bid"),
                    "ask": open_t.get("entry_ask"),
                    "moneyness_pct": None,
                    "open_interest": None,
                    "volume": None,
                }

        # Refresh hold period with DTE
        hp = hold_period_for(horizon, (contract or {}).get("dte"))
        hold_approx = f"≈{hp['ideal_days']}d ({hp['min_days']}–{hp['max_days']}d)"

        ask = float(contract["ask"]) if contract and contract.get("ask") else None
        bid = float(contract["bid"]) if contract and contract.get("bid") else None
        contracts_n = 1
        debit = 0.0
        if ask and ask > 0:
            max_contracts = max(1, int(start_usd // (ask * 100)))
            contracts_n = min(max_contracts, 5) if max_contracts else 1
            debit = round(ask * 100 * contracts_n, 2)
            if debit > start_usd and ask * 100 <= start_usd * 1.05:
                contracts_n = 1
                debit = round(ask * 100, 2)

        win = float(row.get("win_pct") or 0)
        n = int(row.get("trades") or 0)
        tier = _tier(win, n)

        # Status: ENTRY / HOLD / EXIT / WAIT
        action = "ENTRY"
        status_detail = "New challenge ticket — enter when ask is live."
        hold_days = None
        trade_id = None
        if open_t:
            trade_id = open_t.get("id")
            hold_days = open_t.get("hold_days")
            mark = open_t.get("mark") or bid or ask
            entry = float(open_t.get("entry_ask") or 0)
            unreal = None
            if mark and entry:
                unreal = (float(mark) - entry) / entry * 100.0
            target_pct = (need_mult - 1.0) * 100.0
            days = float(hold_days or 0)
            max_d = int(open_t.get("hold_max_days") or hp["max_days"])
            min_d = int(open_t.get("hold_min_days") or hp["min_days"])
            action = "HOLD"
            status_detail = f"Open {right} — holding {days:.1f}d / max {max_d}d"
            if unreal is not None and unreal >= target_pct:
                action = "EXIT"
                status_detail = f"EXIT — target +{unreal:.0f}% hit"
            elif unreal is not None and unreal <= -45:
                action = "EXIT"
                status_detail = f"EXIT — stop {unreal:.0f}%"
            elif days >= max_d:
                action = "EXIT"
                status_detail = f"EXIT — max hold {max_d}d"
            elif open_t.get("last_action") == "EXIT":
                action = "EXIT"
                status_detail = open_t.get("last_action_detail") or "EXIT"
            elif days < min_d:
                status_detail += f" · min hold {min_d}d not reached"
            if open_t.get("last_action") == "HOLD" and open_t.get("last_action_detail"):
                status_detail = str(open_t.get("last_action_detail"))
        elif len(open_map) > 0:
            action = "WAIT"
            status_detail = "WAIT — challenge sleeve already has an open flip (max 1)"
        elif (earn.get("window") == "earnings_day") and not open_t:
            action = "WAIT"
            status_detail = "WAIT — earnings day; skip new long-premium ENTRY"
        elif ask is None:
            action = "ENTRY"
            status_detail = "ENTRY — confirm live ask before fill (hold window set)"
            if earn.get("window") == "pre_earnings":
                status_detail = "ENTRY (LEAP) — earnings nearing; confirm ask & accept IV risk"

        recommend_reason, reasons = _build_reasons(
            tier=tier,
            win=win,
            n=n,
            right=right,
            horizon=horizon,
            hp=hp,
            need_mult=need_mult,
            score=sc,
            quote=q,
            row=row,
            cap_tier=cap_tier,
            earn=earn,
            contract=contract,
            action=action,
        )
        reasons.insert(2, f"Approx hold {hold_approx} before EXIT / roll")
        if spot_source == "live":
            reasons.append(f"Live spot ${spot:.2f}" + (f" @ {quote_asof}" if quote_asof else ""))
        elif spot_source == "cache":
            reasons.append(
                f"Cached daily spot ${spot:.2f}"
                + (f" ({quote_asof})" if quote_asof else "")
                + " — Yahoo live tape rate-limited"
            )
        elif spot_source == "scan":
            reasons.append(f"Scan spot ${spot:.2f} — refresh live quote when Yahoo allows")
        else:
            reasons.append("No spot yet — cannot size strike until quote/cache lands")
        if ask is None:
            reasons.append("Option ask not live yet — strike/DTE zone only until chain quote returns")

        data_note = {
            "live": "Live tape",
            "cache": "Cached daily (Yahoo live limited)",
            "scan": "Scan last price (not live tape)",
            "none": "No spot data",
        }.get(spot_source, spot_source)
        thesis = recommend_reason + f". Approx hold {hold_approx}. " + " ".join(reasons[:3])

        tickets.append(
            ChallengeTicket(
                symbol=sym,
                horizon=horizon,
                right=right,
                action=action,
                hist_win_pct=win,
                hist_samples=n,
                hit_1pct=row.get("hit_1pct"),
                hit_2pct=row.get("hit_2pct"),
                ensemble_score=float(sc["ensemble_score"]) if sc.get("ensemble_score") is not None else None,
                quality=bool(sc.get("quality")),
                contract=(contract or {}).get("contract"),
                expiry=(contract or {}).get("expiry"),
                dte=(contract or {}).get("dte"),
                strike=(contract or {}).get("strike"),
                spot=spot if spot is not None else (contract or {}).get("spot"),
                ask=ask,
                bid=bid,
                moneyness_pct=(contract or {}).get("moneyness_pct"),
                open_interest=(contract or {}).get("open_interest"),
                volume=(contract or {}).get("volume"),
                contracts_for_bankroll=contracts_n,
                debit_usd=debit,
                target_premium_mult=round(need_mult, 3),
                target_ask=round(ask * need_mult, 2) if ask else None,
                hold_period_label=str(hp["label"]),
                hold_approx_label=hold_approx,
                hold_min_days=int(hp["min_days"]),
                hold_max_days=int(hp["max_days"]),
                hold_ideal_days=int(hp["ideal_days"]),
                approx_hold_days=int(hp["ideal_days"]),
                hold_days=hold_days,
                trade_id=trade_id,
                thesis=thesis,
                recommend_reason=recommend_reason,
                reasons=reasons,
                certainty_tier=tier,
                status_detail=status_detail,
                market_cap_tier=cap_tier,
                earnings_window=str(earn.get("window") or "none"),
                earnings_label=str(earn.get("label") or ""),
                next_earnings=earn.get("next_earnings"),
                last_earnings=earn.get("last_earnings"),
                days_to_earnings=earn.get("days_to_earnings"),
                days_since_earnings=earn.get("days_since_earnings"),
                spot_source=spot_source,
                quote_asof=str(quote_asof) if quote_asof else None,
                live_ok=live_ok,
                data_note=data_note,
            )
        )
        if len(tickets) >= max_tickets:
            break

    # Rank: EXIT first, then earnings boost, certainty, hist
    rank_action = {"EXIT": 0, "ENTRY": 1, "HOLD": 2, "WAIT": 3}
    tickets.sort(
        key=lambda t: (
            rank_action.get(t.action, 9),
            -earn_boosts.get(t.symbol, 0),
            0 if t.certainty_tier == "perfect" else 1 if t.certainty_tier == "elite" else 2,
            0 if t.market_cap_tier in {"mid", "small"} and t.earnings_window == "post_earnings" else 1,
            -t.hist_win_pct,
            -t.hist_samples,
        )
    )

    primary = next((t for t in tickets if t.action in {"EXIT", "ENTRY", "HOLD"}), None)
    if primary is None and tickets:
        primary = tickets[0]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_usd": start_usd,
        "target_usd": target_usd,
        "flips": flips,
        "path": primary_path,
        "paths": [
            {"flips": p["flips"], "pct_per_flip": p["pct_per_flip"], "mult_per_flip": p["mult_per_flip"]}
            for p in paths
        ],
        "tickets": [t.to_dict() for t in tickets],
        "primary": primary.to_dict() if primary else None,
        "entry": [t.to_dict() for t in tickets if t.action == "ENTRY"],
        "hold": [t.to_dict() for t in tickets if t.action == "HOLD"],
        "exit": [t.to_dict() for t in tickets if t.action == "EXIT"],
        "counts": {
            "tickets": len(tickets),
            "perfect": sum(1 for t in tickets if t.certainty_tier == "perfect"),
            "elite": sum(1 for t in tickets if t.certainty_tier == "elite"),
            "entry": sum(1 for t in tickets if t.action == "ENTRY"),
            "hold": sum(1 for t in tickets if t.action == "HOLD"),
            "exit": sum(1 for t in tickets if t.action == "EXIT"),
            "calls": sum(1 for t in tickets if t.right == "C"),
            "puts": sum(1 for t in tickets if t.right == "P"),
            "mid_small": sum(1 for t in tickets if t.market_cap_tier in {"mid", "small"}),
            "pre_earnings": sum(1 for t in tickets if t.earnings_window == "pre_earnings"),
            "post_earnings": sum(1 for t in tickets if t.earnings_window == "post_earnings"),
            "live_spot": sum(1 for t in tickets if t.spot_source == "live"),
            "cache_spot": sum(1 for t in tickets if t.spot_source == "cache"),
            "live_ask": sum(1 for t in tickets if t.ask is not None),
        },
        "hold_periods": {
            "weekly": hold_period_for("weekly"),
            "swing": hold_period_for("swing"),
            "leap": hold_period_for("leap", 200),
        },
        "strategy_notes": {
            "earnings": (
                "Prefer post-earnings continuation (IV already crushed) for CALL/PUT swings. "
                "Pre-earnings: LEAP only / caution — short premium gets crushed into the print."
            ),
            "universe": (
                "Liquid mega + mid/small optionables included when they clear the hist-win gate."
            ),
        },
        "rules": [
            "Swing / LEAP only — calls and puts (side from ensemble + tape).",
            "Hist-win filter: prefer 100% (n≥3), else ≥80% (n≥5) on weekly/swing quality signals.",
            "Universe: mega/large + mid/small optionables (IWM sleeve + curated names).",
            "Earnings: boost post-print continuation; caution/LEAP-only into the print; WAIT on earnings day.",
            "Hold periods: weekly 5–14d · swing 20–60d · LEAP 30–90d — EXIT at target, stop, or max hold.",
            f"Each flip targets ~{primary_path['pct_per_flip']:.0f}% option premium; then EXIT and roll.",
            "Status updates: ENTRY (new), HOLD (open inside window), EXIT (target/stop/time).",
            "Max 1 open challenge flip at a time. Research / paper only.",
        ],
        "disclaimer": (
            "No strategy has a guaranteed 100% future win rate. "
            "'Perfect' means the walk-forward sample was 100% on quality signals (often small n). "
            "Earnings moves and IV crush can wipe long premium. Options can expire worthless. "
            "Not financial advice."
        ),
    }
