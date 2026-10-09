"""GEX / OI-wall take-profit alerts for open desk positions.

Strategy
--------
1. Soft wall EXIT (primary): long CALL → take profit when spot ≥ call_wall − buffer;
   long PUT → take profit when spot ≤ put_wall + buffer (default buffer $0.10).
2. Approach ping: warn when spot is within ``approach_pct`` of that soft EXIT so
   the desk can scale out before the magnet.
3. Premium backup: still alert when option mark is ≥ take_profit_pct (default 80%),
   even if walls are missing.

Alerts are deduped via desk_alert_seen.json keys (same as BUY/SELL pulses).
"""

from __future__ import annotations

from typing import Any


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _open_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather open paper / challenge / rec-log positions that can take profit."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(row: dict[str, Any] | None, desk: str) -> None:
        if not isinstance(row, dict):
            return
        st = str(row.get("status") or "open").lower()
        if st in {"closed", "lapsed", "expired"}:
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        occ = str(row.get("contract") or row.get("occ") or "").upper()
        key = f"{desk}|{sym}|{occ or row.get('strike')}"
        if key in seen:
            return
        seen.add(key)
        out.append({**row, "_desk": desk, "symbol": sym, "contract": occ or row.get("contract")})

    insights = snapshot.get("insights") or {}
    for r in insights.get("open_positions") or insights.get("open_trades") or []:
        push(r, "Journal")

    daily = snapshot.get("daily_pnl") or {}
    for r in daily.get("open") or []:
        push(r, str(r.get("category") or r.get("sleeve") or "Journal").title())

    rec = snapshot.get("rec_log") or {}
    for r in rec.get("open_recs") or []:
        push(r, str(r.get("section") or "RecLog").title())
    for sec_name, sec in (rec.get("by_section") or {}).items():
        if not isinstance(sec, dict):
            continue
        for r in sec.get("open_recs") or []:
            push(r, str(sec_name).title())

    ch = snapshot.get("challenge") or {}
    for r in ch.get("hold") or []:
        push({**(r or {}), "status": "open"}, "Challenge")
    book = ch.get("book") or {}
    for r in book.get("open") or book.get("trades") or []:
        if str((r or {}).get("status") or "open").lower() == "open":
            push(r, "Challenge")

    o1k = snapshot.get("odte_1k") or {}
    for r in o1k.get("hold") or o1k.get("book", {}).get("open") or []:
        push({**(r or {}), "status": "open"}, "0DTE $1K")

    return out


def _wall_for(row: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    sym = str(row.get("symbol") or "").upper()
    right = str(row.get("right") or "C").upper()[:1] or "C"
    walls_map = snapshot.get("walls_by_symbol") or {}
    w = dict(walls_map.get(sym) or {})
    # Prefer right-aware soft_exit from the row when present
    soft = row.get("soft_exit")
    if soft is not None:
        w["soft_exit"] = soft
    if row.get("call_wall") is not None:
        w["call_wall"] = row.get("call_wall")
    if row.get("put_wall") is not None:
        w["put_wall"] = row.get("put_wall")
    if row.get("wall_exit_hint") or row.get("exit_hint"):
        w["exit_hint"] = row.get("wall_exit_hint") or row.get("exit_hint")
    w["right"] = right
    # Recompute soft_exit if we have walls but no soft
    if w.get("soft_exit") is None and (w.get("call_wall") is not None or w.get("put_wall") is not None):
        try:
            from odte_scanner.options.walls import wall_exit_levels

            buf = _f(row.get("wall_buffer_usd")) or _f(w.get("wall_buffer_usd")) or 0.10
            refreshed = wall_exit_levels(
                right=right,
                spot=_f(row.get("spot") or row.get("live_last") or row.get("last")),
                call_wall=_f(w.get("call_wall")),
                put_wall=_f(w.get("put_wall")),
                buffer_usd=float(buf),
            )
            w = {**w, **refreshed}
        except Exception:  # noqa: BLE001
            pass
    return w


def _spot_for(row: dict[str, Any], snapshot: dict[str, Any]) -> float | None:
    for key in ("spot", "live_last", "live_spot", "last_price", "last", "mark_spot"):
        v = _f(row.get(key))
        if v is not None and v > 0:
            return v
    sym = str(row.get("symbol") or "").upper()
    quotes = ((snapshot.get("watch") or {}).get("quotes") or {}) if isinstance(snapshot.get("watch"), dict) else {}
    q = quotes.get(sym) or {}
    v = _f(q.get("last") or q.get("price"))
    if v is not None and v > 0:
        return v
    for s in snapshot.get("scores") or []:
        if str(s.get("symbol") or "").upper() == sym:
            v = _f(s.get("last_price") or s.get("entry"))
            if v is not None and v > 0:
                return v
    w = (snapshot.get("walls_by_symbol") or {}).get(sym) or {}
    return _f(w.get("spot"))


def _premium_unreal(row: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    entry = _f(row.get("entry_ask") or row.get("entry") or row.get("entry_price") or row.get("ask"))
    mark = None
    for key in ("bid", "mark", "exit_bid", "live_bid"):
        mark = _f(row.get(key))
        if mark is not None and mark > 0:
            break
    if entry is None or entry <= 0 or mark is None:
        return entry, mark, None
    return entry, mark, (mark - entry) / entry * 100.0


def collect_take_profit_alerts(
    snapshot: dict[str, Any],
    *,
    approach_pct: float = 0.35,
    take_profit_pct: float = 80.0,
    wall_buffer_usd: float = 0.10,
) -> list[dict[str, Any]]:
    """Return TAKE_PROFIT / APPROACH_WALL pulses for open positions."""
    alerts: list[dict[str, Any]] = []
    for row in _open_rows(snapshot):
        right = str(row.get("right") or "C").upper()[:1] or "C"
        is_put = right == "P"
        spot = _spot_for(row, snapshot)
        walls = _wall_for(row, snapshot)
        soft = _f(walls.get("soft_exit"))
        wall = _f(walls.get("primary_wall") or (walls.get("put_wall") if is_put else walls.get("call_wall")))
        entry, mark, unreal = _premium_unreal(row)
        desk = str(row.get("_desk") or "Journal")
        sym = str(row.get("symbol") or "").upper()
        occ = str(row.get("contract") or "")

        kind: str | None = None
        reason_bits: list[str] = []

        if soft is not None and spot is not None and spot > 0:
            if not is_put:
                dist_pct = (soft - spot) / spot * 100.0
                if spot >= soft:
                    kind = "TAKE_PROFIT"
                    reason_bits.append(
                        f"spot ${spot:.2f} ≥ soft EXIT ${soft:.2f} (call wall"
                        + (f" ${wall:.2f}" if wall is not None else "")
                        + f" − ${wall_buffer_usd:.2f})"
                    )
                elif 0 < dist_pct <= float(approach_pct):
                    kind = "APPROACH_WALL"
                    reason_bits.append(
                        f"spot ${spot:.2f} within {dist_pct:.2f}% of call-wall soft EXIT ${soft:.2f}"
                    )
            else:
                dist_pct = (spot - soft) / spot * 100.0
                if spot <= soft:
                    kind = "TAKE_PROFIT"
                    reason_bits.append(
                        f"spot ${spot:.2f} ≤ soft EXIT ${soft:.2f} (put wall"
                        + (f" ${wall:.2f}" if wall is not None else "")
                        + f" + ${wall_buffer_usd:.2f})"
                    )
                elif 0 < dist_pct <= float(approach_pct):
                    kind = "APPROACH_WALL"
                    reason_bits.append(
                        f"spot ${spot:.2f} within {dist_pct:.2f}% of put-wall soft EXIT ${soft:.2f}"
                    )

        if kind is None and unreal is not None and unreal >= float(take_profit_pct):
            kind = "TAKE_PROFIT"
            reason_bits.append(f"premium {unreal:+.0f}% ≥ +{float(take_profit_pct):.0f}% TP")

        if kind is None:
            continue

        hint = walls.get("exit_hint") or walls.get("wall_exit_hint") or ""
        if hint:
            reason_bits.append(str(hint)[:160])
        if unreal is not None and entry is not None and mark is not None:
            reason_bits.append(
                f"option ${entry:.2f} → ${mark:.2f} ({unreal:+.0f}%) · ~${(mark - entry) * 100:+.2f}/ct"
            )

        strike = row.get("strike")
        strike_s = f"{strike}{right}" if strike is not None else right
        emoji = "💰" if kind == "TAKE_PROFIT" else "⚠️"
        lines = [
            f"{emoji} {kind.replace('_', ' ')} · {desk}",
            f"{sym} {strike_s} · {occ or '—'}",
        ]
        if spot is not None:
            lines.append(f"Spot ${spot:.2f}" + (f" · soft EXIT ${soft:.2f}" if soft is not None else ""))
        for bit in reason_bits:
            lines.append(bit)
        lines.append("Signal Desk · take profit at GEX/OI wall — don't gift it back")
        message = "\n".join(lines)

        alerts.append(
            {
                "key": f"tp|{kind}|{desk}|{sym}|{occ or strike}|{soft}",
                "side": "SELL",
                "desk": desk,
                "symbol": sym,
                "action": kind,
                "contract": occ or None,
                "strike": strike,
                "expiry": row.get("expiry"),
                "ask": mark,
                "message": message,
                "row": {
                    **row,
                    "action": kind,
                    "alert_action": kind,
                    "detail": " · ".join(reason_bits),
                    "soft_exit": soft,
                    "spot": spot,
                },
            }
        )
    return alerts
