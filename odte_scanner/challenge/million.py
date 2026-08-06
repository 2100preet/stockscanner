"""$1,000 → $1,000,000 challenge path via swing / LEAP call flips.

Selects high hist-win names and recommends strike + expiry tickets sized so
~10–15 compounded premium doubles/near-doubles can theoretically path to $1M.

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

logger = logging.getLogger(__name__)


@dataclass
class ChallengeTicket:
    symbol: str
    horizon: str
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
    thesis: str
    certainty_tier: str  # perfect | elite | strong

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
    # Prefer swing, then higher n / win
    rows.sort(
        key=lambda r: (
            0 if r.get("horizon") == "swing" else 1,
            0 if float(r.get("win_pct") or 0) >= 99.9 else 1,
            -float(r.get("win_pct") or 0),
            -int(r.get("trades") or 0),
        )
    )
    return rows


def select_leap_call(
    symbol: str,
    spot: float,
    *,
    yahoo_symbol: str | None = None,
    min_dte: int = 90,
    max_dte: int = 450,
    otm_pct_max: float = 8.0,
    itm_pct_max: float = 2.0,
    max_ask: float = 80.0,
    min_oi: int = 50,
) -> dict[str, Any] | None:
    """Pick a liquid swing/LEAP call — slight OTM, mid-dated."""
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
        # widen if needed
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

    # Prefer ~180 DTE (6 months) for LEAP-style swing
    targets.sort(key=lambda x: abs(x[1] - 180))
    best: dict[str, Any] | None = None
    best_rank = -1e18

    for expiry, dte in targets[:6]:
        try:
            chain = t.option_chain(expiry)
            calls = chain.calls
        except Exception:  # noqa: BLE001
            continue
        if calls is None or calls.empty:
            continue
        for _, row in calls.iterrows():
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            mny = (strike - spot) / spot * 100.0
            if mny < -itm_pct_max or mny > otm_pct_max:
                continue
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
            # Rank: near 3% OTM, high OI, closer to 180 DTE, tight spread
            rank = (
                40.0
                - abs(mny - 3.0) * 4.0
                - abs(dte - 180) * 0.05
                - spread * 30.0
                + min(20.0, oi / 500.0)
                + min(10.0, vol / 100.0)
            )
            if rank > best_rank:
                best_rank = rank
                best = {
                    "symbol": symbol,
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


def build_challenge_board(
    *,
    win_table: dict[str, Any] | None,
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    start_usd: float = 1000.0,
    target_usd: float = 1_000_000.0,
    flips: int = 12,
    max_tickets: int = 8,
    fetch_contracts: bool = True,
) -> dict[str, Any]:
    quotes = quotes or {}
    aliases = aliases or {}
    scores = scores or []
    score_map: dict[str, dict[str, Any]] = {}
    for s in scores:
        sym = str(s.get("symbol") or "")
        hz = str(s.get("horizon") or "")
        if not sym:
            continue
        # prefer matching horizon, else best score
        prev = score_map.get(sym)
        if prev is None or hz in {"swing", "weekly"} or float(s.get("ensemble_score") or 0) > float(
            prev.get("ensemble_score") or 0
        ):
            if prev is None or hz == "swing" or (
                prev.get("_hz") != "swing" and float(s.get("ensemble_score") or 0) >= float(prev.get("ensemble_score") or 0)
            ):
                row = dict(s)
                row["_hz"] = hz
                score_map[sym] = row

    paths = path_table(start_usd, target_usd)
    primary_path = compound_path(start_usd=start_usd, target_usd=target_usd, flips=flips)
    need_mult = float(primary_path["mult_per_flip"] or 1.8)

    eligible = _eligible_rows(win_table)
    tickets: list[ChallengeTicket] = []

    for row in eligible[: max_tickets + 4]:
        sym = str(row["symbol"])
        spot = None
        q = quotes.get(sym) or {}
        if q.get("last") is not None:
            spot = float(q["last"])
        sc = score_map.get(sym) or {}
        if spot is None and sc.get("last_price") is not None:
            spot = float(sc["last_price"])
        if spot is None and sc.get("entry") is not None:
            spot = float(sc["entry"])

        contract = None
        # Limit live chain hits (Yahoo rate limits) — top names only
        if fetch_contracts and spot and spot > 0 and len(tickets) < 4:
            try:
                # Weekly hist → prefer ~90-180 DTE; swing hist → LEAP window
                if row.get("horizon") == "weekly":
                    contract = select_leap_call(
                        sym,
                        spot,
                        yahoo_symbol=aliases.get(sym),
                        min_dte=60,
                        max_dte=220,
                        otm_pct_max=6.0,
                    )
                else:
                    contract = select_leap_call(
                        sym,
                        spot,
                        yahoo_symbol=aliases.get(sym),
                        min_dte=120,
                        max_dte=450,
                        otm_pct_max=8.0,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("challenge contract fetch %s: %s", sym, exc)
                contract = None

        ask = float(contract["ask"]) if contract and contract.get("ask") else None
        # Size: spend most of bankroll on 1–few contracts (challenge mode)
        contracts_n = 1
        debit = 0.0
        if ask and ask > 0:
            max_contracts = max(1, int(start_usd // (ask * 100)))
            contracts_n = min(max_contracts, 5) if max_contracts else 1
            if contracts_n < 1:
                contracts_n = 1
            debit = round(ask * 100 * contracts_n, 2)
            # If one contract > bankroll, still show ticket as "need larger bankroll / scale"
            if debit > start_usd and ask * 100 <= start_usd * 1.05:
                contracts_n = 1
                debit = round(ask * 100, 2)

        win = float(row.get("win_pct") or 0)
        n = int(row.get("trades") or 0)
        tier = _tier(win, n)
        thesis = (
            f"{'PERFECT' if tier=='perfect' else tier.upper()} hist filter: "
            f"{row.get('horizon')} quality signals won {win:.0f}% (n={n}). "
            f"Challenge target ≈{need_mult:.2f}× premium (~{primary_path['pct_per_flip']:.0f}%) then roll."
        )
        if row.get("hit_1pct") is not None:
            thesis += f" Strike-rate ≥1% underlying: {row['hit_1pct']:.0f}%."

        tickets.append(
            ChallengeTicket(
                symbol=sym,
                horizon=str(row.get("horizon")),
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
                bid=(contract or {}).get("bid"),
                moneyness_pct=(contract or {}).get("moneyness_pct"),
                open_interest=(contract or {}).get("open_interest"),
                volume=(contract or {}).get("volume"),
                contracts_for_bankroll=contracts_n,
                debit_usd=debit,
                target_premium_mult=round(need_mult, 3),
                target_ask=round(ask * need_mult, 2) if ask else None,
                thesis=thesis,
                certainty_tier=tier,
            )
        )
        if len(tickets) >= max_tickets:
            break

    # Rank perfect first
    tickets.sort(
        key=lambda t: (
            0 if t.certainty_tier == "perfect" else 1 if t.certainty_tier == "elite" else 2,
            -t.hist_win_pct,
            -t.hist_samples,
            -(t.ensemble_score or 0),
        )
    )

    perfect_n = sum(1 for t in tickets if t.certainty_tier == "perfect")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_usd": start_usd,
        "target_usd": target_usd,
        "flips": flips,
        "path": primary_path,
        "paths": [
            {
                "flips": p["flips"],
                "pct_per_flip": p["pct_per_flip"],
                "mult_per_flip": p["mult_per_flip"],
            }
            for p in paths
        ],
        "tickets": [t.to_dict() for t in tickets],
        "primary": tickets[0].to_dict() if tickets else None,
        "counts": {
            "tickets": len(tickets),
            "perfect": perfect_n,
            "elite": sum(1 for t in tickets if t.certainty_tier == "elite"),
        },
        "rules": [
            "Only swing / LEAP calls (not 0DTE lottery).",
            "Ticket must clear hist-win filter: prefer 100% (n≥3), else ≥80% (n≥5) on weekly/swing quality signals.",
            f"Each flip targets ~{primary_path['pct_per_flip']:.0f}% option premium gain; take profit and roll — do not revenge-trade losers.",
            "Risk only the challenge bankroll sleeve; max 1 ticket at a time.",
            "Historical underlying direction ≠ option P&L. Past results ≠ future. Research / paper only.",
        ],
        "disclaimer": (
            "No strategy has a guaranteed 100% future win rate. "
            "'Perfect' means the walk-forward sample for that symbol/horizon was 100% on quality signals "
            "(often small n). Options can expire worthless. Not financial advice."
        ),
    }
