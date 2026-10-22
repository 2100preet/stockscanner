"""Promote a strong trend, or a Golden Flow the tape confirms, to sure_winner_gold.

This path does not touch the hist-win gate. A name still needs its own
score, confirm, relative-strength, and gap-and-go bars, a near-the-money
contract inside the premium cap, and a clock inside the entry window.
Position size stays at one contract. The card is HIGH (gold on the desk),
not MAX / GO HUGE.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.signals.tape import algo_score, tape_direction

ET = ZoneInfo("America/New_York")

# Safety caps used when a caller only sets prefer_near_money.
NEAR_OTM_CAP = 1.25
NEAR_ITM_CAP = 1.50


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _cfg_float(cfg: dict[str, Any], key: str, default: float) -> float:
    number = _num(cfg.get(key))
    return float(default if number is None else number)


def _cfg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    number = _num(cfg.get(key))
    return int(default if number is None else number)


def _hhmm(text: str) -> time:
    hour, minute = str(text or "00:00").split(":", 1)
    return time(int(hour), int(minute))


def _as_et(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def entry_window_open(
    now: datetime | None,
    *,
    start_et: str = "09:35",
    end_et: str = "14:30",
) -> bool:
    """True from start inclusive through end exclusive, weekdays, ET."""
    et = _as_et(now)
    if et.weekday() >= 5:
        return False
    clock = et.timetz().replace(tzinfo=None)
    return _hhmm(start_et) <= clock < _hhmm(end_et)


def _window_for(cfg: dict[str, Any], prefix: str) -> tuple[str, str]:
    start = str(
        cfg.get(f"{prefix}_entry_start_et")
        or cfg.get("trend_gold_entry_start_et")
        or "09:35"
    )
    end = str(
        cfg.get(f"{prefix}_entry_end_et")
        or cfg.get("trend_gold_entry_end_et")
        or "14:30"
    )
    return start, end


def _score_of(row: dict[str, Any]) -> float:
    for key in ("ensemble_score", "score", "strength"):
        number = _num(row.get(key))
        if number is not None:
            return number
    return 0.0


def _confirms_of(row: dict[str, Any]) -> int:
    number = _num(row.get("confirms"))
    if number is not None:
        return int(number)
    count = 0
    for sig in row.get("signals") or []:
        if isinstance(sig, dict) and sig.get("bullish"):
            count += 1
    return count


def _overlay_quote(row: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(quote, dict):
        return row
    out = dict(row)
    if out.get("live_change_pct") is None:
        live = quote.get("session_change_pct")
        if live is None:
            live = quote.get("change_pct")
        if live is not None:
            out["live_change_pct"] = live
    if out.get("mom_5m_pct") is None and quote.get("mom_5m_pct") is not None:
        out["mom_5m_pct"] = quote.get("mom_5m_pct")
    if out.get("last_price") is None and quote.get("last") is not None:
        out["last_price"] = quote.get("last")
    if not out.get("bars") and quote.get("bars"):
        out["bars"] = quote.get("bars")
    return out


def trend_block_reason(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> str | None:
    """None when the scanner trend bundle clears. Does not check the clock or a contract."""
    cfg = cfg or {}
    min_score = _cfg_float(cfg, "trend_gold_min_score", 65.0)
    min_confirms = _cfg_int(cfg, "trend_gold_min_confirms", 6)
    min_rs = _cfg_float(cfg, "trend_gold_min_relative_strength", 80.0)
    min_gap = _cfg_float(cfg, "trend_gold_min_gap_and_go", 70.0)
    score = _score_of(row)
    confirms = _confirms_of(row)
    rs = algo_score(row, "relative_strength")
    gap = algo_score(row, "gap_and_go")
    if score < min_score:
        return f"score {score:.1f} < {min_score:.0f}"
    if confirms < min_confirms:
        return f"confirms {confirms} < {min_confirms}"
    if rs is None or rs < min_rs:
        return f"relative strength {rs} < {min_rs:.0f}"
    if gap is None or gap < min_gap:
        return f"gap_and_go {gap} < {min_gap:.0f}"
    direction = tape_direction(None, row)
    if direction in {"flat", "down"}:
        return f"tape {direction} — gap is not holding"
    return None


def is_strong_trend(row: dict[str, Any] | None, cfg: dict[str, Any] | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    return trend_block_reason(row, cfg) is None


def moneyness_pct(spot: float | None, strike: float | None, right: str = "C") -> float | None:
    """Positive means OTM for the given right."""
    if spot is None or strike is None or spot <= 0 or strike <= 0:
        return None
    if str(right or "C").upper().startswith("P"):
        return (float(spot) - float(strike)) / float(spot) * 100.0
    return (float(strike) - float(spot)) / float(spot) * 100.0


def _spot_of(*rows: Any) -> float | None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("spot", "live_last", "live_spot", "last_price", "last", "underlying_price"):
            number = _num(row.get(key))
            if number is not None and number > 0:
                return number
    return None


def _in_band(mny: float | None, *, max_otm: float, max_itm: float) -> bool:
    if mny is None:
        return False
    return -abs(max_itm) <= mny <= abs(max_otm)


def _ask_of(row: dict[str, Any]) -> float | None:
    number = _num(row.get("ask"))
    if number is None or number <= 0:
        number = _num(row.get("mid") or row.get("mark"))
    if number is None or number <= 0:
        return None
    return number


def _right_of(row: dict[str, Any]) -> str:
    right = str(row.get("right") or "C").upper()
    if right.startswith("P"):
        return "P"
    return "C"


def _is_golden_print(row: dict[str, Any]) -> bool:
    from odte_scanner.trading.golden import is_golden_unusual_flow

    return is_golden_unusual_flow(row)


def _flow_prints(option_flow: Any) -> list[dict[str, Any]]:
    if isinstance(option_flow, list):
        rows = option_flow
    elif isinstance(option_flow, dict):
        rows = option_flow.get("prints")
        if not rows:
            rows = list(option_flow.get("golden") or [])
    else:
        return []
    return [row for row in rows or [] if isinstance(row, dict)]


def _index_by_symbol(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            out.setdefault(sym, []).append(row)
    return out


def _contract_choice(
    row: dict[str, Any],
    *,
    spot: float | None,
    source: str,
    max_otm: float,
    max_itm: float,
    max_ask: float,
) -> dict[str, Any] | None:
    ask = _ask_of(row)
    if ask is None or ask > max_ask:
        return None
    contract = str(row.get("contract") or "").strip()
    if not contract:
        return None
    right = _right_of(row)
    strike = _num(row.get("strike"))
    use_spot = _spot_of(row) or spot
    mny = _num(row.get("moneyness_pct"))
    if mny is None:
        mny = moneyness_pct(use_spot, strike, right)
    if not _in_band(mny, max_otm=max_otm, max_itm=max_itm):
        return None
    return {
        "row": row,
        "source": source,
        "ask": ask,
        "contract": contract,
        "right": right,
        "strike": strike,
        "spot": use_spot,
        "moneyness_pct": mny,
        "distance": abs(float(mny or 0.0)),
    }


def _direction_matches(scanner: dict[str, Any], right: str) -> bool:
    """Bullish scanner confirms a call. A put needs an explicit weak tape."""
    if right == "C":
        return tape_direction(None, scanner) != "down" and _score_of(scanner) >= 50.0
    return tape_direction(None, scanner) == "down" and _score_of(scanner) <= 45.0


def _build_card(
    scanner: dict[str, Any],
    choice: dict[str, Any],
    *,
    path: str,
    max_contracts: int,
) -> dict[str, Any]:
    from odte_scanner.signals.sure_winners import _pick_key

    row = dict(choice["row"])
    sym = str(scanner.get("symbol") or row.get("symbol") or "").upper()
    score = _score_of(scanner)
    confirms = _confirms_of(scanner)
    rs = algo_score(scanner, "relative_strength") or 0.0
    gap = algo_score(scanner, "gap_and_go") or 0.0
    win = scanner.get("win_pct")
    if win is None:
        win = scanner.get("hist_win_pct")
    samples = scanner.get("win_samples")
    if samples is None:
        samples = scanner.get("hist_samples")
    label = "FLOW GOLD" if path == "confirmed_flow" else "TREND GOLD"
    strike = choice.get("strike")
    strike_bit = f" {float(strike):g}{choice['right']}" if strike is not None else ""
    headline = (
        f"HIGH · {label} — {sym}{strike_bit} "
        f"(score {score:.1f}, {confirms} confirms, RS {rs:.0f}, gap {gap:.0f})"
    )
    detail = (
        f"Promoted {path.replace('_', ' ')} — hist gate unchanged"
        + (f" (hist {float(win):.0f}% n={int(samples)})" if win is not None and samples is not None else "")
        + f". 1 contract, ask ${choice['ask']:.2f}, moneyness {float(choice['moneyness_pct']):+.2f}%."
    )
    card = {
        **row,
        "symbol": sym,
        "action": "SURE_WINNER",
        "sure_winner": True,
        "confidence_tier": "HIGH",
        "confidence_pct": round(min(92.0, 86.0 + min(6.0, max(0.0, score - 65.0) * 0.3)), 1),
        "go_huge": False,
        "color": "gold",
        "size_mult": 1.0,
        "size_label": "1-LOT",
        "size_hint": "Promoted gold — 1 contract, not GO HUGE",
        "max_contracts": int(max_contracts),
        "promotion_path": path,
        "source_desk": "Golden Flow" if path == "confirmed_flow" else "Trend",
        "right": choice["right"],
        "strike": strike,
        "ask": choice["ask"],
        "contract": choice["contract"],
        "spot": choice.get("spot"),
        "moneyness_pct": choice.get("moneyness_pct"),
        "score": score,
        "ensemble_score": score,
        "confirms": confirms,
        "relative_strength": rs,
        "gap_and_go": gap,
        "win_pct": win,
        "win_samples": samples,
        "live_change_pct": scanner.get("live_change_pct"),
        "headline": headline,
        "detail": detail,
        "exit_plan": row.get("exit_plan") or "TP +80% / SL −50% · promoted 1-lot",
    }
    card["pick_key"] = _pick_key(card)
    return card


def collect_promotions(
    *,
    scores: list[dict[str, Any]] | None = None,
    option_rows: list[dict[str, Any]] | None = None,
    option_flow: Any = None,
    quotes: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    now: datetime | None = None,
    red_flag: dict[str, Any] | None = None,
    respect_gex: bool = True,
) -> list[dict[str, Any]]:
    """Near-money cards that may use the reserved sure-winner slot."""
    cfg = dict(cfg or {})
    trend_on = bool(cfg.get("trend_gold_enabled", True))
    flow_on = bool(cfg.get("flow_gold_enabled", True))
    if not trend_on and not flow_on:
        return []

    quotes = quotes or {}
    by_score: dict[str, dict[str, Any]] = {}
    for raw in scores or []:
        if not isinstance(raw, dict):
            continue
        sym = str(raw.get("symbol") or "").upper()
        if not sym:
            continue
        current = by_score.get(sym)
        if current is None or _score_of(raw) >= _score_of(current):
            by_score[sym] = raw

    contracts_by_sym = _index_by_symbol([r for r in (option_rows or []) if isinstance(r, dict)])
    prints_by_sym = _index_by_symbol(_flow_prints(option_flow))
    symbols = set(by_score) | (set(prints_by_sym) if flow_on else set())

    from odte_scanner.signals.sure_winners import gex_blocks_sure_candidate

    cards: list[dict[str, Any]] = []
    for sym in sorted(symbols):
        scanner = _overlay_quote(dict(by_score.get(sym) or {"symbol": sym}), quotes.get(sym))
        scanner["symbol"] = sym
        if trend_block_reason(scanner, cfg):
            continue
        if gex_blocks_sure_candidate(scanner, red_flag, respect_gex=respect_gex):
            continue

        choices: list[dict[str, Any]] = []
        if flow_on:
            start, end = _window_for(cfg, "flow_gold")
            if entry_window_open(now, start_et=start, end_et=end):
                flow_otm = _cfg_float(cfg, "flow_gold_max_otm_pct", _cfg_float(cfg, "trend_gold_max_otm_pct", NEAR_OTM_CAP))
                flow_itm = _cfg_float(cfg, "flow_gold_max_itm_pct", _cfg_float(cfg, "trend_gold_max_itm_pct", NEAR_ITM_CAP))
                flow_ask = _cfg_float(cfg, "flow_gold_max_ask", 8.0)
                spot = _spot_of(scanner, quotes.get(sym))
                for print_row in prints_by_sym.get(sym) or []:
                    if not _is_golden_print(print_row):
                        continue
                    if not _direction_matches(scanner, _right_of(print_row)):
                        continue
                    choice = _contract_choice(
                        print_row,
                        spot=spot,
                        source="confirmed_flow",
                        max_otm=flow_otm,
                        max_itm=flow_itm,
                        max_ask=flow_ask,
                    )
                    if choice:
                        choices.append(choice)
        if trend_on:
            start, end = _window_for(cfg, "trend_gold")
            if entry_window_open(now, start_et=start, end_et=end):
                trend_otm = _cfg_float(cfg, "trend_gold_max_otm_pct", NEAR_OTM_CAP)
                trend_itm = _cfg_float(cfg, "trend_gold_max_itm_pct", NEAR_ITM_CAP)
                trend_ask = _cfg_float(cfg, "trend_gold_max_ask", 8.0)
                spot = _spot_of(scanner, quotes.get(sym))
                for contract_row in contracts_by_sym.get(sym) or []:
                    if not _direction_matches(scanner, _right_of(contract_row)):
                        continue
                    choice = _contract_choice(
                        contract_row,
                        spot=spot,
                        source="strong_trend",
                        max_otm=trend_otm,
                        max_itm=trend_itm,
                        max_ask=trend_ask,
                    )
                    if choice:
                        choices.append(choice)
        if not choices:
            continue
        # Nearest strike. A confirmed flow wins a tie so the swept contract is the one we paper.
        choices.sort(key=lambda c: (c["distance"], 0 if c["source"] == "confirmed_flow" else 1, c["ask"]))
        best = choices[0]
        cap_key = "flow_gold_max_contracts" if best["source"] == "confirmed_flow" else "trend_gold_max_contracts"
        cards.append(
            _build_card(
                scanner,
                best,
                path=str(best["source"]),
                max_contracts=max(1, _cfg_int(cfg, cap_key, 1)),
            )
        )
    cards.sort(key=lambda c: (-float(c.get("score") or 0), str(c.get("symbol"))))
    return cards
