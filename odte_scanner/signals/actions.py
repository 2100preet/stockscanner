from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from odte_scanner.signals.hold_rules import (
    exit_plan_text,
    past_no_new_0dte_entries,
    time_stop_reason,
)
from odte_scanner.time_cst import (
    append_asked_cst,
    load_signal_store,
    resolve_first_signal_time,
    save_signal_store,
    signal_timestamps,
)


@dataclass
class ActionSignal:
    action: str  # BUY_NOW | SELL_NOW | HOLD | WAIT
    symbol: str
    strength: float  # 0–100
    headline: str
    detail: str
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    score: float | None = None
    live_last: float | None = None
    live_change_pct: float | None = None
    contract: str | None = None
    trade_id: str | None = None
    dte: int | None = None
    dte_bucket: str | None = None  # 0dte | weekly
    bid: float | None = None
    right: str | None = "C"  # C | P
    exit_plan: str | None = None
    win_pct: float | None = None
    win_samples: int | None = None
    hit_1pct: float | None = None
    hit_2pct: float | None = None
    signaled_at: str | None = None
    signaled_at_cst: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.action in {"BUY_NOW", "SELL_NOW"} and not d.get("signaled_at"):
            d.update(signal_timestamps())
        return d


def _apply_persisted_action(
    sig: ActionSignal,
    store: dict[str, Any],
) -> tuple[ActionSignal, dict[str, Any]]:
    if sig.action not in {"BUY_NOW", "SELL_NOW"}:
        return sig, store
    utc, cst, store = resolve_first_signal_time(store, symbol=sig.symbol, action=sig.action)
    sig.signaled_at = utc
    sig.signaled_at_cst = cst
    sig.detail = append_asked_cst(sig.detail, action=sig.action, signaled_at_cst=cst)
    return sig, store


def _live_pct(quote: dict[str, Any] | None) -> float | None:
    if not quote:
        return None
    if quote.get("session_change_pct") is not None:
        return float(quote["session_change_pct"])
    if quote.get("change_pct") is not None:
        return float(quote["change_pct"])
    return None


def _bucket_label(candidate: dict[str, Any]) -> str:
    bucket = candidate.get("dte_bucket")
    dte = candidate.get("dte")
    if bucket == "weekly" or (dte is not None and int(dte) > 1):
        return "1W"
    return "0DTE"


def decide_entry(
    candidate: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    buy_score: float = 70.0,
    wait_score: float = 62.0,
    max_chase_pct: float = 2.5,
    open_symbols: set[str] | None = None,
    require_live_confirm: bool = True,
    take_profit_pct: float = 80.0,
    stop_loss_pct: float = 50.0,
    weekly_max_hold_days: int = 7,
    odte_flatten_et: str = "15:45",
    now: datetime | None = None,
) -> ActionSignal:
    """
    Turn a call/put candidate into BUY_NOW / WAIT / HOLD.

    BUY NOW is gated — not score-only:
      - live option quote preferred (no synthetic)
      - tape confirm aligned with side (calls need bounce; puts need dump)
      - 0DTE: moneyness / no new entries after 15:00 ET; EXIT plan always attached
      - not chasing an already-extended move
    """
    symbol = str(candidate.get("symbol", ""))
    score = float(candidate.get("score") or 0)
    right = str(candidate.get("right") or "C").upper()
    if right not in {"C", "P"}:
        right = "C"
    is_put = right == "P"
    side_lbl = "put" if is_put else "call"
    live = _live_pct(quote)
    last = None
    if quote and quote.get("last") is not None:
        last = float(quote["last"])
    elif candidate.get("live_spot") is not None:
        last = float(candidate["live_spot"])

    open_symbols = open_symbols or set()
    bucket = _bucket_label(candidate)
    dte = candidate.get("dte")
    dte_bucket = candidate.get("dte_bucket") or ("weekly" if bucket == "1W" else "0dte")
    contract = candidate.get("contract")
    strike = candidate.get("strike")
    ask = candidate.get("ask")
    bid = candidate.get("bid")
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    dist_high = (
        float(quote["dist_from_day_high_pct"])
        if quote and quote.get("dist_from_day_high_pct") is not None
        else None
    )
    dist_low = (
        float(quote["dist_from_day_low_pct"])
        if quote and quote.get("dist_from_day_low_pct") is not None
        else None
    )
    opt_pct = candidate.get("option_percent_change")
    if opt_pct is not None:
        opt_pct = float(opt_pct)
    moneyness = candidate.get("moneyness_pct")
    if moneyness is None and last and strike:
        # Call OTM% = (K-S)/S; put OTM% = (S-K)/S
        if is_put:
            moneyness = (float(last) - float(strike)) / float(last) * 100
        else:
            moneyness = (float(strike) - float(last)) / float(last) * 100

    plan = exit_plan_text(
        dte_bucket=str(dte_bucket),
        dte=int(dte) if dte is not None else None,
        right=right,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        weekly_max_days=weekly_max_hold_days,
        odte_flatten_et=odte_flatten_et,
        soft_exit=candidate.get("soft_exit"),
    )

    base_kwargs = dict(
        symbol=symbol,
        strike=strike,
        expiry=candidate.get("expiry"),
        ask=ask,
        bid=bid,
        score=score,
        live_last=last,
        live_change_pct=live,
        contract=contract,
        dte=dte,
        dte_bucket=dte_bucket,
        right=right,
        exit_plan=plan,
    )

    def _wait(detail: str, strength: float | None = None) -> ActionSignal:
        return ActionSignal(
            action="WAIT",
            strength=float(strength if strength is not None else max(0.0, score * 0.7)),
            headline=f"WAIT {symbol} {side_lbl.upper()} · {bucket}",
            detail=detail,
            **base_kwargs,
        )

    # Never promote synthetic / missing chain quotes to BUY NOW
    if candidate.get("synthetic") or (isinstance(contract, str) and contract.endswith("_SYN")):
        return _wait("No live option chain quote — skipped synthetic strike.", 0)

    if candidate.get("quote_stale") and require_live_confirm:
        return _wait("Option quote stale/unavailable — not buying blind.", 20)

    if symbol in open_symbols:
        return ActionSignal(
            action="HOLD",
            strength=min(100.0, score),
            headline=f"HOLD {symbol}",
            detail=f"Already in an open paper {side_lbl} — manage exit, don't pyramid. {plan}",
            **base_kwargs,
        )

    # Desk practice: no new 0DTE risk after 15:00 ET
    if bucket == "0DTE" and past_no_new_0dte_entries(now):
        return _wait("Past 15:00 ET — no new 0DTE entries (flatten / manage only).")

    # --- Hard tape gates (side-aware) ---
    if not is_put:
        if mom5 is not None and mom5 <= -0.15:
            return _wait(f"5m tape weak ({mom5:+.2f}%) — no BUY NOW call on falling knife.")

        if mom15 is not None and mom15 <= -0.25:
            return _wait(f"15m momentum down ({mom15:+.2f}%) — wait for reclaim.")

        if live is not None and live <= -0.6 and bucket == "0DTE":
            return _wait(f"Session soft for 0DTE call ({live:+.2f}%).")

        if live is not None and live <= -1.5:
            return _wait(f"Session soft ({live:+.2f}%). Let tape stabilize.")

        if opt_pct is not None and opt_pct <= -25:
            return _wait(
                f"Call already down {opt_pct:.0f}% today (ask ${float(ask or 0):.2f}) — not chasing a melting premium."
            )

        if bucket == "0DTE" and last is not None and strike is not None:
            if float(last) < float(strike) * 0.999:
                return _wait(
                    f"Spot {last:.2f} below strike {float(strike):g} — 0DTE call is losing; wait for reclaim."
                )
            if dist_high is not None and dist_high <= -0.45 and (mom5 is None or mom5 <= 0):
                return _wait(
                    f"Off day high ({dist_high:+.2f}%) with no rebound — skip 0DTE call."
                )

        if moneyness is not None and bucket == "0DTE" and moneyness > 0.35 and (mom5 is None or mom5 <= 0.05):
            return _wait(
                f"Strike is {moneyness:.2f}% OTM while tape flat/down — prefer ATM/ITM or WAIT."
            )

        chase_limit = max_chase_pct if bucket == "0DTE" else max_chase_pct + 1.5
        if live is not None and live >= chase_limit and score < buy_score + 5:
            return _wait(f"Already up {live:+.2f}% this session — chase risk on calls.")

        if require_live_confirm and bucket == "0DTE":
            if mom5 is None and mom15 is None and live is None:
                return _wait("No live tape confirm — not buying 0DTE call blind off daily score alone.")
            if mom5 is not None and mom5 < 0.05 and (mom15 is None or mom15 < 0.05):
                return _wait(
                    f"Need short-term bounce for 0DTE call BUY (5m {mom5:+.2f}%"
                    + (f", 15m {mom15:+.2f}%" if mom15 is not None else "")
                    + ")."
                )

        tape_ok = live is None or live > -0.35
        mom_boost = 5 if mom5 and mom5 > 0.1 else 0
    else:
        # Puts: need weakness, not a bounce
        if mom5 is not None and mom5 >= 0.15:
            return _wait(f"5m tape bouncing ({mom5:+.2f}%) — no BUY NOW put into a reclaim.")

        if mom15 is not None and mom15 >= 0.25:
            return _wait(f"15m momentum up ({mom15:+.2f}%) — wait for rollover for puts.")

        if live is not None and live >= 0.6 and bucket == "0DTE":
            return _wait(f"Session firm for 0DTE put ({live:+.2f}%).")

        if live is not None and live >= 1.5:
            return _wait(f"Session strong ({live:+.2f}%). Let dump develop for puts.")

        if opt_pct is not None and opt_pct <= -25:
            return _wait(
                f"Put already down {opt_pct:.0f}% today (ask ${float(ask or 0):.2f}) — not chasing melting premium."
            )

        if bucket == "0DTE" and last is not None and strike is not None:
            if float(last) > float(strike) * 1.001:
                return _wait(
                    f"Spot {last:.2f} above strike {float(strike):g} — 0DTE put is losing; wait for break."
                )
            if dist_low is not None and dist_low >= 0.45 and (mom5 is None or mom5 >= 0):
                return _wait(
                    f"Off day low (+{dist_low:.2f}%) with no fresh dump — skip 0DTE put."
                )

        if moneyness is not None and bucket == "0DTE" and moneyness > 0.35 and (mom5 is None or mom5 >= -0.05):
            return _wait(
                f"Put strike is {moneyness:.2f}% OTM while tape flat/up — prefer ATM/ITM or WAIT."
            )

        chase_limit = max_chase_pct if bucket == "0DTE" else max_chase_pct + 1.5
        if live is not None and live <= -chase_limit and score < buy_score + 5:
            return _wait(f"Already down {live:+.2f}% this session — chase risk on puts.")

        if require_live_confirm and bucket == "0DTE":
            if mom5 is None and mom15 is None and live is None:
                return _wait("No live tape confirm — not buying 0DTE put blind off daily score alone.")
            if mom5 is not None and mom5 > -0.05 and (mom15 is None or mom15 > -0.05):
                return _wait(
                    f"Need short-term dump for 0DTE put BUY (5m {mom5:+.2f}%"
                    + (f", 15m {mom15:+.2f}%" if mom15 is not None else "")
                    + ")."
                )

        tape_ok = live is None or live < 0.35
        mom_boost = 5 if mom5 and mom5 < -0.1 else 0

    # Puts use inverse score: weak ensemble OR explicit put_score
    eff_score = score
    if is_put and candidate.get("put_score") is not None:
        eff_score = float(candidate["put_score"])
    elif is_put and score < wait_score:
        # Bearish sleeve: invert weak bullish score into put strength
        eff_score = max(score, 100.0 - float(score))

    if eff_score >= buy_score and tape_ok:
        strength = min(100.0, eff_score + mom_boost)
        ask_f = float(ask or 0)
        exp = candidate.get("expiry")
        confirm_bits = []
        if mom5 is not None:
            confirm_bits.append(f"5m {mom5:+.2f}%")
        if last is not None:
            confirm_bits.append(f"spot {last:.2f}")
        if opt_pct is not None:
            confirm_bits.append(f"opt {opt_pct:+.0f}%")
        detail = (
            f"[{bucket}] Score {eff_score:.0f} · {exp} {float(strike):g} {side_lbl} @ ask ${ask_f:.2f}"
            + (f" (DTE {dte})" if dte is not None else "")
            + (" · " + ", ".join(confirm_bits) if confirm_bits else "")
            + f" · {plan}"
        )
        return ActionSignal(
            action="BUY_NOW",
            strength=strength,
            headline=f"BUY NOW {symbol} {side_lbl.upper()} · {bucket}",
            detail=detail,
            **base_kwargs,
        )

    if eff_score >= wait_score:
        return _wait(
            f"Score {eff_score:.0f} {side_lbl} watchlist — tape/confirm not clean for BUY NOW. {plan}"
        )

    return _wait(f"Below buy threshold for {side_lbl}.")


def decide_exit(
    trade: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    score_by_symbol: dict[str, float],
    stop_loss_pct: float = 50.0,
    take_profit_pct: float = 80.0,
    sell_score: float = 48.0,
    weekly_max_hold_days: int = 7,
    odte_flatten_et: str = "15:45",
    now: datetime | None = None,
) -> ActionSignal | None:
    if str(trade.get("status") or "open") != "open":
        return None
    symbol = str(trade.get("symbol", ""))
    entry = float(trade.get("entry") or trade.get("entry_ask") or 0)
    live = _live_pct(quote)
    last = float(quote["last"]) if quote and quote.get("last") is not None else None
    score = float(score_by_symbol.get(symbol, trade.get("score") or trade.get("entry_score") or 0))
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    right = str(trade.get("right") or "C").upper()
    if right not in {"C", "P"}:
        right = "C"
    is_put = right == "P"
    side_lbl = "put" if is_put else "call"

    # Live option mark for premium P&L (never fall back to entry for exit price)
    exit_px: float | None = None
    for key in ("bid", "mark", "exit_bid"):
        raw = trade.get(key)
        if raw is not None and float(raw) > 0:
            exit_px = float(raw)
            break
    unreal: float | None = None
    if exit_px is not None and entry > 0:
        unreal = (exit_px - entry) / entry * 100.0

    reasons: list[str] = []
    sell = False
    strength = 60.0

    # Industry clock / max-hold (works offline on Pages without live marks)
    clock = time_stop_reason(
        trade,
        now=now,
        weekly_max_days=weekly_max_hold_days,
        odte_flatten_et=odte_flatten_et,
    )
    if clock:
        sell = True
        strength = max(strength, 96.0)
        reasons.append(clock)

    # Premium take-profit / stop-loss (config risk knobs — were previously unused)
    if unreal is not None and unreal >= float(take_profit_pct):
        sell = True
        strength = max(strength, 92.0)
        reasons.append(f"take profit {unreal:+.0f}% ≥ +{take_profit_pct:.0f}%")

    if unreal is not None and unreal <= -abs(float(stop_loss_pct)):
        sell = True
        strength = max(strength, 95.0)
        reasons.append(f"stop loss {unreal:+.0f}% ≤ −{abs(float(stop_loss_pct)):.0f}%")

    # Soft wall EXIT on underlying (call: spot ≥ soft_exit; put: spot ≤ soft_exit)
    soft = trade.get("soft_exit")
    if soft is not None and last is not None:
        soft_f = float(soft)
        if not is_put and last >= soft_f:
            sell = True
            strength = max(strength, 90.0)
            reasons.append(f"spot {last:.2f} ≥ soft EXIT ${soft_f:.2f} (call wall)")
        elif is_put and last <= soft_f:
            sell = True
            strength = max(strength, 90.0)
            reasons.append(f"spot {last:.2f} ≤ soft EXIT ${soft_f:.2f} (put wall)")

    if score <= sell_score and not is_put:
        sell = True
        strength = max(strength, 85.0)
        reasons.append(f"ensemble cooled to {score:.0f}")
    if is_put and score >= (100.0 - float(sell_score)):
        # Puts: exit when bullish ensemble re-heats
        sell = True
        strength = max(strength, 85.0)
        reasons.append(f"ensemble reheated to {score:.0f} — exit put")

    if not is_put:
        if live is not None and live <= -1.2:
            sell = True
            strength = max(strength, 88.0)
            reasons.append(f"session {live:+.2f}% against long call")
        if mom5 is not None and mom5 <= -0.35:
            sell = True
            strength = max(strength, 90.0)
            reasons.append(f"5m dump {mom5:+.2f}% — exit 0DTE call risk")
        if live is not None and live >= 1.8:
            sell = True
            strength = max(strength, 80.0)
            reasons.append(f"underlying ripped {live:+.2f}% — bank call premium")
    else:
        if live is not None and live >= 1.2:
            sell = True
            strength = max(strength, 88.0)
            reasons.append(f"session {live:+.2f}% against long put")
        if mom5 is not None and mom5 >= 0.35:
            sell = True
            strength = max(strength, 90.0)
            reasons.append(f"5m bounce {mom5:+.2f}% — exit 0DTE put risk")
        if live is not None and live <= -1.8:
            sell = True
            strength = max(strength, 80.0)
            reasons.append(f"underlying dumped {live:+.2f}% — bank put premium")

    detail_extra = ""
    if unreal is not None:
        detail_extra = f" · unreal {unreal:+.0f}% @ ${exit_px:.2f}" if exit_px else f" · unreal {unreal:+.0f}%"

    plan = exit_plan_text(
        dte_bucket=str(trade.get("dte_bucket") or "0dte"),
        dte=int(trade["dte"]) if trade.get("dte") is not None else None,
        right=right,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        weekly_max_days=weekly_max_hold_days,
        odte_flatten_et=odte_flatten_et,
        soft_exit=float(soft) if soft is not None else None,
    )

    kwargs = dict(
        symbol=symbol,
        score=score,
        live_last=last,
        live_change_pct=live,
        contract=trade.get("contract"),
        trade_id=trade.get("id"),
        strike=trade.get("strike"),
        expiry=trade.get("expiry"),
        dte=trade.get("dte"),
        dte_bucket=trade.get("dte_bucket"),
        right=right,
        exit_plan=plan,
        # Critical: price the exit at mark/bid — never entry ask (that forced ~0% P&L)
        ask=exit_px,
        bid=exit_px,
    )

    if sell:
        return ActionSignal(
            action="SELL_NOW",
            strength=strength,
            headline=f"SELL NOW {symbol} {side_lbl.upper()}",
            detail=("; ".join(reasons) or "Exit signal") + detail_extra,
            **kwargs,
        )

    return ActionSignal(
        action="HOLD",
        strength=min(100.0, max(40.0, score)),
        headline=f"HOLD {symbol} {side_lbl.upper()}",
        detail=(
            f"Open {side_lbl} still valid — trail with stop / take-profit / soft wall / clock"
            + detail_extra
            + f". {plan}"
        ),
        **kwargs,
    )


def normalize_open_trade(trade: dict[str, Any]) -> dict[str, Any]:
    """Normalize journal / paper / lottery open rows for decide_exit."""
    t = dict(trade)
    entry = t.get("entry")
    if entry is None:
        entry = t.get("entry_ask")
    t["entry"] = float(entry) if entry is not None else 0.0
    if t.get("entry_ask") is None and entry is not None:
        t["entry_ask"] = float(entry)
    mark = t.get("mark")
    bid = t.get("bid")
    if mark is None and bid is not None and float(bid) > 0:
        t["mark"] = float(bid)
    if (bid is None or float(bid or 0) <= 0) and mark is not None and float(mark) > 0:
        t["bid"] = float(mark)
    t["status"] = str(t.get("status") or "open")
    if t.get("score") is None and t.get("entry_score") is not None:
        t["score"] = t.get("entry_score")
    right = str(t.get("right") or "C").upper()
    # Infer put from OCC-style contract when right missing
    if right not in {"C", "P"}:
        right = "C"
    contract = str(t.get("contract") or "")
    if right == "C" and len(contract) >= 15 and "P" in contract[-15:-6]:
        # e.g. SPY260811P00500000
        import re

        if re.search(r"\d{6}P\d{8}$", contract):
            right = "P"
    t["right"] = right
    return t


def merge_exit_ledgers(
    *sources: dict[str, Any] | list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Union open trades from paper ledger + signal journal for SELL NOW decisions."""
    trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        if src is None:
            continue
        rows = src if isinstance(src, list) else (src.get("trades") or [])
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "open") != "open":
                continue
            t = normalize_open_trade(raw)
            key = str(
                t.get("id")
                or t.get("contract")
                or f"{t.get('symbol')}-{t.get('right')}-{t.get('entry')}-{t.get('strike')}"
            )
            if key in seen:
                continue
            seen.add(key)
            trades.append(t)
    return {"trades": trades}


def _attach_win_stats(sig: ActionSignal, win_table: dict[str, Any] | None) -> ActionSignal:
    from odte_scanner.backtest.win_rates import lookup_win_stats

    stats = lookup_win_stats(win_table, sig.symbol, sig.dte_bucket)
    sig.win_pct = stats.get("win_pct")
    sig.win_samples = int(stats.get("trades") or 0) or None
    sig.hit_1pct = stats.get("hit_1pct")
    sig.hit_2pct = stats.get("hit_2pct")
    if sig.win_pct is None:
        return sig
    # n = sample size: how many historical quality signals this win% is based on
    n = sig.win_samples or 0
    parts = [f"hist win {sig.win_pct:.0f}%"]
    parts.append(f"n={n} samples")
    if sig.hit_1pct is not None:
        # "strike rate" ≈ how often the underlying ripped ≥1% after the signal
        parts.append(f"strike rate ≥1% {sig.hit_1pct:.0f}%")
    if sig.hit_2pct is not None:
        parts.append(f"≥2% {sig.hit_2pct:.0f}%")
    if n and n < 8:
        parts.append("low sample — treat cautiously")
    sig.detail = f"{sig.detail} · " + " · ".join(parts)
    return sig


def apply_hist_win_gate(
    sig: ActionSignal,
    *,
    min_hist_win_pct: float = 80.0,
    min_hist_win_samples: int = 5,
    require_hist_win: bool = True,
) -> ActionSignal:
    """Demote BUY NOW → WAIT unless walk-forward hist win clears the bar.

    This is how the desk targets ≥80% measured win rate on promoted trades:
    only symbols/horizons that already cleared that bar in backtest are buyable.
    Put tickets skip this gate (table is call/underlying-rip oriented).
    """
    if not require_hist_win or sig.action != "BUY_NOW":
        return sig
    if str(sig.right or "C").upper() == "P":
        return sig
    n = int(sig.win_samples or 0)
    win = sig.win_pct
    if win is None:
        sig.action = "WAIT"
        sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
        sig.detail = (
            f"{sig.detail} · blocked: no hist win backtest yet "
            f"(need ≥{min_hist_win_pct:.0f}% over n≥{min_hist_win_samples})"
        )
        sig.strength = min(sig.strength, 45.0)
        return sig
    if n < min_hist_win_samples:
        sig.action = "WAIT"
        sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
        sig.detail = (
            f"{sig.detail} · blocked: hist n={n} < {min_hist_win_samples} "
            f"(need ≥{min_hist_win_pct:.0f}% win)"
        )
        sig.strength = min(sig.strength, 48.0)
        return sig
    if float(win) < float(min_hist_win_pct):
        sig.action = "WAIT"
        sig.headline = sig.headline.replace("BUY NOW", "WAIT", 1)
        sig.detail = (
            f"{sig.detail} · blocked: hist win {win:.0f}% < {min_hist_win_pct:.0f}% target"
        )
        sig.strength = min(sig.strength, 50.0)
        return sig
    return sig


def build_action_board(
    *,
    candidates: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    ledger: dict[str, Any] | None,
    buy_score: float = 70.0,
    wait_score: float = 62.0,
    sell_score: float = 48.0,
    stop_loss_pct: float = 50.0,
    take_profit_pct: float = 80.0,
    max_chase_pct: float = 2.5,
    win_rate_table: dict[str, Any] | None = None,
    min_hist_win_pct: float = 80.0,
    min_hist_win_samples: int = 5,
    require_hist_win: bool = True,
    journal_opens: list[dict[str, Any]] | None = None,
    weekly_max_hold_days: int = 7,
    odte_flatten_et: str = "15:45",
    now: datetime | None = None,
    require_live_confirm: bool = True,
    red_flag: dict[str, Any] | None = None,
    signal_times_path: str | None = "outputs/signal_times.json",
) -> dict[str, Any]:
    score_by_symbol = {
        str(s.get("symbol")): float(s.get("ensemble_score") or 0) for s in scores or []
    }
    # Journal opens drive auto SELL NOW; paper ledger alone was leaving exits dark
    merged = merge_exit_ledgers(ledger, journal_opens)
    open_trades = list(merged.get("trades") or [])
    open_symbols = {str(t.get("symbol")) for t in open_trades}
    store = load_signal_store(signal_times_path)

    buys: list[ActionSignal] = []
    waits: list[ActionSignal] = []
    holds: list[ActionSignal] = []
    sells: list[ActionSignal] = []

    for t in open_trades:
        sig = decide_exit(
            t,
            quote=quotes.get(str(t.get("symbol"))),
            score_by_symbol=score_by_symbol,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            sell_score=sell_score,
            weekly_max_hold_days=weekly_max_hold_days,
            odte_flatten_et=odte_flatten_et,
            now=now,
        )
        if not sig:
            continue
        sig = _attach_win_stats(sig, win_rate_table)
        if sig.action == "SELL_NOW":
            sig, store = _apply_persisted_action(sig, store)
            sells.append(sig)
        else:
            holds.append(sig)

    for c in candidates or []:
        sig = decide_entry(
            c,
            quote=quotes.get(str(c.get("symbol"))),
            buy_score=buy_score,
            wait_score=wait_score,
            max_chase_pct=max_chase_pct,
            open_symbols=open_symbols,
            require_live_confirm=require_live_confirm,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            weekly_max_hold_days=weekly_max_hold_days,
            odte_flatten_et=odte_flatten_et,
            now=now,
        )
        sig = _attach_win_stats(sig, win_rate_table)
        sig = apply_hist_win_gate(
            sig,
            min_hist_win_pct=min_hist_win_pct,
            min_hist_win_samples=min_hist_win_samples,
            require_hist_win=require_hist_win,
        )
        if sig.action == "BUY_NOW":
            sig, store = _apply_persisted_action(sig, store)
            buys.append(sig)
        elif sig.action == "HOLD":
            holds.append(sig)
        else:
            waits.append(sig)

    save_signal_store(signal_times_path, store)

    # Rank buys by historical win% then strength
    buys.sort(key=lambda s: (s.win_pct or 0, s.strength), reverse=True)
    sells.sort(key=lambda s: s.strength, reverse=True)
    waits.sort(key=lambda s: (s.win_pct or 0, s.strength), reverse=True)

    buy_0dte = [s for s in buys if (s.dte_bucket or "0dte") == "0dte"]
    buy_weekly = [s for s in buys if (s.dte_bucket or "") == "weekly"]
    buy_calls = [s for s in buys if (s.right or "C") != "P"]
    buy_puts = [s for s in buys if (s.right or "C") == "P"]
    sell_puts = [s for s in sells if (s.right or "C") == "P"]

    all_signals = buys + sells + holds + waits
    rank = {"SELL_NOW": 0, "BUY_NOW": 1, "HOLD": 2, "WAIT": 3}
    all_signals.sort(key=lambda s: (rank.get(s.action, 9), -(s.win_pct or 0), -s.strength))

    primary = None
    if sells:
        primary = sells[0]
    elif buy_0dte:
        primary = buy_0dte[0]
    elif buy_weekly:
        primary = buy_weekly[0]
    elif holds:
        primary = holds[0]
    elif waits:
        primary = waits[0]

    from odte_scanner.backtest.win_rates import summarize_hist_win_gate

    gate_summary = summarize_hist_win_gate(
        win_rate_table,
        min_hist_win_pct=min_hist_win_pct,
        min_hist_win_samples=min_hist_win_samples,
    )

    board = {
        "primary": primary.to_dict() if primary else None,
        "all": [s.to_dict() for s in all_signals],
        "buy_now": [s.to_dict() for s in buys],
        "buy_now_0dte": [s.to_dict() for s in buy_0dte],
        "buy_now_weekly": [s.to_dict() for s in buy_weekly],
        "buy_now_calls": [s.to_dict() for s in buy_calls],
        "buy_now_puts": [s.to_dict() for s in buy_puts],
        "sell_now": [s.to_dict() for s in sells],
        "sell_now_puts": [s.to_dict() for s in sell_puts],
        "hold": [s.to_dict() for s in holds],
        "wait": [s.to_dict() for s in waits],
        "win_rates_note": (win_rate_table or {}).get("note"),
        "hold_rules": {
            "odte_flatten_et": odte_flatten_et,
            "weekly_max_hold_days": weekly_max_hold_days,
            "require_live_confirm": require_live_confirm,
            "exit_criteria": [
                f"take profit ≥ +{take_profit_pct:.0f}% premium",
                f"stop loss ≤ −{abs(stop_loss_pct):.0f}% premium",
                f"0DTE clock flatten by {odte_flatten_et} ET",
                f"weekly max hold {weekly_max_hold_days}d",
                "soft wall on underlying (call ≥ wall / put ≤ wall)",
                f"ensemble cool ≤ {sell_score:.0f} (calls) / reheated (puts)",
                "tape fail: 5m dump (calls) / 5m bounce (puts)",
                "bank rip/dump on underlying vs side",
            ],
            "note": (
                "SELL NOW only fires for open paper positions. "
                "Every BUY/WAIT includes an EXIT plan (TP/SL/clock). "
                "0DTE flatten by clock; weekly max hold days."
            ),
        },
        "hist_win_gate": {
            "require": require_hist_win,
            "min_hist_win_pct": min_hist_win_pct,
            "min_hist_win_samples": min_hist_win_samples,
            **gate_summary,
        },
        "counts": {
            "buy_now": len(buys),
            "buy_now_0dte": len(buy_0dte),
            "buy_now_weekly": len(buy_weekly),
            "buy_now_calls": len(buy_calls),
            "buy_now_puts": len(buy_puts),
            "sell_now": len(sells),
            "sell_now_puts": len(sell_puts),
            "hold": len(holds),
            "wait": len(waits),
            "all": len(all_signals),
        },
        "signal_times": store,
    }

    if red_flag:
        from odte_scanner.signals.red_flag import apply_red_flag_to_actions

        board = apply_red_flag_to_actions(board, red_flag)

    return board
