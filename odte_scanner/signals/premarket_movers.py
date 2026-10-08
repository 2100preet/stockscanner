"""Premarket / extended-hours movers → session special-eye.

Fetches live (prepost) quotes over liquid + catalyst seeds, ranks
gainers/losers, and merges the session set into desk special-eye so
RIP / options / UW keep a tight watch on names moving before the open.

Static catalyst seeds (news-driven) are always included even if the
quote fetch is thin — so WOLF/HAE-class headlines never fall off.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.signals.rip_radar import DESK_SPECIAL_EYE

logger = logging.getLogger(__name__)

# Known catalyst / headline seeds — always on today's eye even before quotes land.
# Update when the desk calls out premarket news movers.
CATALYST_SEEDS: dict[str, str] = {
    "WOLF": "DoD conditional $1.5B 30y loan — SiC / defense",
    "HAE": "CSL to roll Haemonetics plasma platform US by end-2027",
    "PEP": "Q3 beat; cut FY EPS growth guide to 2.5–3.5%",
    "PLTR": "Goldman upgrade to Buy / $230 PT",
    "XOM": "Oil jump — energy complex bid",
    "CVX": "Oil jump — energy complex bid",
    "LEVI": "Rev miss / cautious sales outlook — soft open risk",
    "APLD": "AI data-center rev +322% to ~$342M",
}

DEFAULT_STORE = Path("outputs/premarket_eye.json")


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def _sym(v: Any) -> str:
    return str(v or "").replace(".", "-").upper().strip()


def catalyst_seed_symbols() -> list[str]:
    return sorted(CATALYST_SEEDS)


def static_special_eye(cfg: dict[str, Any] | None = None) -> set[str]:
    actions = (cfg or {}).get("actions") or {}
    raw = actions.get("desk_special_eye") or list(DESK_SPECIAL_EYE)
    out = {_sym(s) for s in raw if s}
    out |= set(DESK_SPECIAL_EYE)
    out |= set(CATALYST_SEEDS)
    out.discard("")
    return out


def _in_premarket_window(now: datetime | None = None) -> bool:
    """True roughly 4:00–9:30 ET (and after-hours 16:00–20:00 for continuity)."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    ts = now.astimezone(et) if now else datetime.now(et)
    if ts.weekday() >= 5:
        return False
    mins = ts.hour * 60 + ts.minute
    return (4 * 60 <= mins < 9 * 60 + 30) or (16 * 60 <= mins < 20 * 60)


def _candidate_pool(cfg: dict[str, Any] | None = None) -> list[str]:
    """Catalyst seeds + special-eye + focus; expand to liquid in premarket window."""
    from odte_scanner.calendars import resolve_universe
    from odte_scanner.data.universe import liquid_universe

    cfg = cfg or {}
    seen: set[str] = set()
    out: list[str] = []
    sources: list[list[str]] = [
        catalyst_seed_symbols(),
        sorted(static_special_eye(cfg)),
        resolve_universe(cfg),
    ]
    # Full liquid fan-out only when extended-hours movers matter
    if _in_premarket_window():
        sources.append(liquid_universe()[:120])
    for src in sources:
        for s in src:
            u = _sym(s)
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


def _row_from_quote(sym: str, q: Any, *, catalyst: str | None = None) -> dict[str, Any]:
    if hasattr(q, "to_dict"):
        d = q.to_dict()
    elif isinstance(q, dict):
        d = dict(q)
    else:
        d = {}
    pct = _f(d.get("session_change_pct"))
    if pct is None:
        pct = _f(d.get("change_pct"))
    last = _f(d.get("last")) or _f(d.get("price"))
    return {
        "symbol": sym,
        "last": last,
        "prev_close": _f(d.get("prev_close") or d.get("pre_close")),
        "change_pct": _f(d.get("change_pct")),
        "session_change_pct": pct,
        "session": d.get("session") or "unknown",
        "mom_5m_pct": _f(d.get("mom_5m_pct")),
        "mom_15m_pct": _f(d.get("mom_15m_pct")),
        "catalyst": catalyst or CATALYST_SEEDS.get(sym),
        "source": "live_quotes",
    }


def rank_movers(
    quotes: dict[str, Any],
    *,
    min_abs_pct: float = 1.5,
    min_price: float = 2.0,
    top_n: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for sym, q in (quotes or {}).items():
        su = _sym(sym)
        row = _row_from_quote(su, q)
        pct = row.get("session_change_pct")
        last = row.get("last")
        if pct is None:
            continue
        if last is not None and last < min_price and su not in CATALYST_SEEDS:
            continue
        if abs(float(pct)) < min_abs_pct and su not in CATALYST_SEEDS:
            continue
        rows.append(row)
    gainers = sorted(rows, key=lambda r: float(r.get("session_change_pct") or 0), reverse=True)
    losers = sorted(rows, key=lambda r: float(r.get("session_change_pct") or 0))
    return gainers[:top_n], losers[:top_n]


def fetch_premarket_movers(
    cfg: dict[str, Any] | None = None,
    *,
    quotes: dict[str, Any] | None = None,
    aliases: dict[str, str] | None = None,
    min_abs_pct: float = 1.5,
    min_price: float = 2.0,
    top_n: int = 12,
    max_quote_symbols: int = 160,
) -> dict[str, Any]:
    """Rank premarket / session movers and build a session special-eye set."""
    cfg = cfg or {}
    actions = cfg.get("actions") or {}
    min_abs_pct = float(actions.get("premarket_min_abs_pct", min_abs_pct))
    min_price = float(actions.get("premarket_min_price", min_price))
    top_n = int(actions.get("premarket_top_n", top_n))
    max_quote_symbols = int(actions.get("premarket_max_quote_symbols", max_quote_symbols))

    pool = _candidate_pool(cfg)[:max_quote_symbols]
    used_quotes = quotes
    if used_quotes is None:
        try:
            from odte_scanner.data.live_quotes import fetch_live_quotes

            used_quotes = fetch_live_quotes(pool, aliases=aliases or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("premarket quote fetch failed: %s", exc)
            used_quotes = {}

    # Normalize LiveQuote objects → dict by symbol
    qmap: dict[str, Any] = {}
    for k, v in (used_quotes or {}).items():
        qmap[_sym(k)] = v

    gainers, losers = rank_movers(
        qmap, min_abs_pct=min_abs_pct, min_price=min_price, top_n=top_n
    )

    # Ensure catalyst seeds appear even if quote missing / below threshold
    have = {r["symbol"] for r in gainers} | {r["symbol"] for r in losers}
    seed_rows: list[dict[str, Any]] = []
    for sym, note in CATALYST_SEEDS.items():
        if sym in have:
            # Attach catalyst note onto existing rows
            for bucket in (gainers, losers):
                for r in bucket:
                    if r["symbol"] == sym:
                        r["catalyst"] = note
            continue
        q = qmap.get(sym)
        if q is not None:
            seed_rows.append(_row_from_quote(sym, q, catalyst=note))
        else:
            seed_rows.append(
                {
                    "symbol": sym,
                    "last": None,
                    "prev_close": None,
                    "change_pct": None,
                    "session_change_pct": None,
                    "session": "prepost",
                    "catalyst": note,
                    "source": "catalyst_seed",
                }
            )

    session_eye = sorted(
        static_special_eye(cfg)
        | {r["symbol"] for r in gainers if float(r.get("session_change_pct") or 0) >= min_abs_pct}
        | {
            r["symbol"]
            for r in losers
            if float(r.get("session_change_pct") or 0) <= -min_abs_pct
        }
        | set(CATALYST_SEEDS)
    )

    board = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": date.today().isoformat(),
        "gainers": gainers,
        "losers": losers,
        "catalysts": seed_rows,
        "session_eye": session_eye,
        "static_eye": sorted(static_special_eye(cfg)),
        "counts": {
            "gainers": len(gainers),
            "losers": len(losers),
            "catalysts": len(CATALYST_SEEDS),
            "session_eye": len(session_eye),
            "quoted": len(qmap),
        },
        "rules": [
            f"rank liquid+seed quotes by session_change_pct (min |%| {min_abs_pct}, min px ${min_price:.0f})",
            "catalyst seeds always on session special-eye",
            "merged into RIP seed / option priority / UW focus",
        ],
        "purpose": (
            "Premarket movers desk — keep special eye on names already ripping or "
            "soft into the open so algo sleeves (RIP / lottery / challenge) see them early."
        ),
    }
    return board


def save_session_eye(board: dict[str, Any], path: str | Path | None = None) -> Path:
    out = Path(path or DEFAULT_STORE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(board, indent=2, default=str))
    return out


def load_session_eye(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path or DEFAULT_STORE)
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text())
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def effective_special_eye(
    cfg: dict[str, Any] | None = None,
    *,
    board: dict[str, Any] | None = None,
    store_path: str | Path | None = None,
) -> set[str]:
    """Static desk eye ∪ catalyst seeds ∪ latest session movers."""
    eye = static_special_eye(cfg)
    src = board if board is not None else load_session_eye(store_path)
    for s in src.get("session_eye") or []:
        u = _sym(s)
        if u:
            eye.add(u)
    return eye


def build_premarket_board(
    cfg: dict[str, Any] | None = None,
    *,
    quotes: dict[str, Any] | None = None,
    aliases: dict[str, str] | None = None,
    persist: bool = True,
    store_path: str | Path | None = None,
) -> dict[str, Any]:
    board = fetch_premarket_movers(cfg, quotes=quotes, aliases=aliases)
    if persist:
        try:
            save_session_eye(board, store_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("premarket eye persist failed: %s", exc)
    return board
