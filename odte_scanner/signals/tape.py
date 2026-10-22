"""Tape direction when the 5-minute print is missing.

A missing ``mom_5m_pct`` is not a flat tape. Callers fall through to the
latest bar, the session/day change or gap, then relative strength. An
explicit 5-minute print still wins, including a flat or down one.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _first(sources: tuple[Any, ...], keys: tuple[str, ...]) -> float | None:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            number = _num(src.get(key))
            if number is not None:
                return number
    return None


def algo_score(row: dict[str, Any] | None, name: str) -> float | None:
    """Read one algo score from a flat key, an algo map, or a signals list."""
    if not isinstance(row, dict):
        return None
    direct = _num(row.get(name))
    if direct is not None:
        return direct
    algos = row.get("algos") or row.get("algo_scores")
    if isinstance(algos, dict):
        raw = algos.get(name)
        if isinstance(raw, dict):
            number = _num(raw.get("score"))
        else:
            number = _num(raw)
        if number is not None:
            return number
    for sig in row.get("signals") or []:
        if isinstance(sig, dict) and str(sig.get("name") or "") == name:
            number = _num(sig.get("score"))
            if number is not None:
                return number
    return None


def _bar_return_pct(quote: dict[str, Any] | None, candidate: dict[str, Any] | None) -> float | None:
    for src in (quote, candidate):
        if not isinstance(src, dict):
            continue
        named = _num(src.get("bar_change_pct"))
        if named is None:
            named = _num(src.get("last_bar_change_pct"))
        if named is not None:
            return named
        bars = src.get("bars") or src.get("intraday")
        if not isinstance(bars, list) or len(bars) < 2:
            continue
        closes: list[float] = []
        for bar in bars:
            if isinstance(bar, dict):
                close = _num(bar.get("close") if bar.get("close") is not None else bar.get("Close"))
                if close is None:
                    close = _num(bar.get("c"))
            else:
                close = _num(bar)
            if close is not None and close > 0:
                closes.append(close)
        if len(closes) >= 6:
            return (closes[-1] / closes[-6] - 1.0) * 100.0
        if len(closes) >= 2:
            return (closes[-1] / closes[-2] - 1.0) * 100.0
    return None


def _classify_pct(value: float, *, up: float, down: float) -> str:
    if value > up:
        return "up"
    if value < down:
        return "down"
    return "flat"


def tape_direction(
    quote: dict[str, Any] | None,
    candidate: dict[str, Any] | None = None,
) -> str:
    """Return ``up``, ``flat``, ``down``, or ``unknown``.

    Order: live 5-minute momentum, latest bar return, session/day/gap
    percent, then relative strength with the gap-and-go score. Missing
    inputs stay ``unknown`` so a dark quote is not read as a flat tape.
    """
    mom = _first((quote, candidate), ("mom_5m_pct",))
    if mom is not None:
        return _classify_pct(mom, up=0.05, down=-0.05)

    bar = _bar_return_pct(quote, candidate)
    if bar is not None:
        return _classify_pct(bar, up=0.05, down=-0.05)

    day = _first(
        (quote, candidate),
        (
            "session_change_pct",
            "change_pct",
            "live_change_pct",
            "day_change_pct",
            "gap_pct",
            "gap_change_pct",
        ),
    )
    if day is not None:
        # A held gap or a green session is not a flat 5-minute print.
        return _classify_pct(day, up=0.30, down=-0.30)

    rs = algo_score(candidate, "relative_strength")
    if rs is None:
        rs = algo_score(quote if isinstance(quote, dict) else None, "relative_strength")
    gap = algo_score(candidate, "gap_and_go")
    if gap is None:
        gap = algo_score(quote if isinstance(quote, dict) else None, "gap_and_go")
    if rs is not None and rs >= 80.0 and (gap is None or gap >= 45.0):
        return "up"
    if rs is not None and rs <= 40.0 and (gap is None or gap <= 55.0):
        return "down"
    return "unknown"


def call_otm_tape_blocked(mom5: float | None, tape_dir: str) -> bool:
    """Far-OTM calls wait on a flat or down tape. A missing 5m print does not."""
    if mom5 is not None:
        return mom5 <= 0.05
    return tape_dir in {"flat", "down"}


def put_otm_tape_blocked(mom5: float | None, tape_dir: str) -> bool:
    """Far-OTM puts wait on a flat or up tape. A missing 5m print does not."""
    if mom5 is not None:
        return mom5 >= -0.05
    return tape_dir in {"flat", "up"}


def call_lacks_rebound(mom5: float | None, tape_dir: str) -> bool:
    if mom5 is not None:
        return mom5 <= 0.0
    return tape_dir != "up"


def put_lacks_dump(mom5: float | None, tape_dir: str) -> bool:
    if mom5 is not None:
        return mom5 >= 0.0
    return tape_dir != "down"
