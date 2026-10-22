"""Sure-winner desk — 1–2 hist-gated tickets per day, plus one promoted slot.

Selects from Options BUY_NOW + Challenge ENTRY that clear perfect/elite hist
certainty, have a live contract/ask, and rank by confidence. Sticky for the
America/Chicago trading day so the board does not churn every snapshot.

Confidence tiers (color-coded on the desk):
  MAX   — perfect hist (100% n≥3) + quality score → GO HUGE (×3)
  HIGH  — elite hist (≥85% n≥5) + quality → SIZE UP (×2)
  SOLID — strong hist (≥80% n≥5) + quality → FULL SIZE (×1.5)

Research / paper only — hist edge ≠ guaranteed future profit.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import CT, signal_timestamps, to_cst_label

ET = ZoneInfo("America/New_York")

TIER_RANK = {"MAX": 0, "HIGH": 1, "SOLID": 2}
SIZE_BY_TIER = {
    "MAX": {"size_mult": 3.0, "size_label": "GO HUGE", "size_hint": "3× base size — highest conviction"},
    "HIGH": {"size_mult": 2.0, "size_label": "SIZE UP", "size_hint": "2× base size — elite hist + tape"},
    "SOLID": {"size_mult": 1.5, "size_label": "FULL SIZE", "size_hint": "1.5× base — strong hist, still size up"},
}
COLOR_BY_TIER = {
    "MAX": "gold",
    "HIGH": "amber",
    "SOLID": "soft-amber",
}


@dataclass
class SureWinner:
    action: str  # SURE_WINNER
    symbol: str
    confidence_tier: str  # MAX | HIGH | SOLID
    confidence_pct: float
    size_mult: float
    size_label: str
    size_hint: str
    color: str
    headline: str
    detail: str
    desk: str = "Sure Winner"
    source_desk: str = "Options"
    certainty_tier: str | None = None  # perfect | elite | strong
    right: str = "C"
    strike: float | None = None
    expiry: str | None = None
    dte: int | None = None
    dte_bucket: str | None = None
    ask: float | None = None
    bid: float | None = None
    contract: str | None = None
    score: float | None = None
    ensemble_score: float | None = None
    win_pct: float | None = None
    win_samples: int | None = None
    hit_1pct: float | None = None
    hit_2pct: float | None = None
    live_change_pct: float | None = None
    spot: float | None = None
    exit_plan: str | None = None
    call_wall: float | None = None
    put_wall: float | None = None
    soft_exit: float | None = None
    wall_buffer_usd: float | None = None
    playbook: list[str] = field(default_factory=list)
    sure_winner: bool = True
    go_huge: bool = False
    signaled_at: str | None = None
    signaled_at_cst: str | None = None
    day_cst: str | None = None
    pick_key: str | None = None
    max_contracts: int | None = None
    promotion_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def certainty_from_hist(win_pct: float | None, n: int | None) -> str:
    """Mirror challenge._tier — perfect / elite / strong / watch."""
    try:
        win = float(win_pct) if win_pct is not None else 0.0
    except (TypeError, ValueError):
        win = 0.0
    try:
        samples = int(n or 0)
    except (TypeError, ValueError):
        samples = 0
    if win >= 99.9 and samples >= 3:
        return "perfect"
    if win >= 85 and samples >= 5:
        return "elite"
    if win >= 80 and samples >= 5:
        return "strong"
    return "watch"


def confidence_tier_for(
    *,
    certainty: str,
    score: float | None,
    hit_1pct: float | None,
) -> str | None:
    """Map hist certainty + quality into MAX / HIGH / SOLID (or None = skip)."""
    try:
        sc = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        sc = 0.0
    try:
        hit = float(hit_1pct) if hit_1pct is not None else 0.0
    except (TypeError, ValueError):
        hit = 0.0

    if certainty == "perfect" and sc >= 68:
        return "MAX"
    if certainty == "elite" and sc >= 78 and hit >= 70:
        return "MAX"
    if certainty == "elite" and sc >= 70:
        return "HIGH"
    if certainty == "strong" and sc >= 75 and hit >= 65:
        return "HIGH"
    if certainty == "strong" and sc >= 72:
        return "SOLID"
    if certainty == "perfect":
        return "HIGH"  # perfect hist but softer score still sizes up
    return None


def confidence_pct_for(tier: str, win_pct: float | None, score: float | None) -> float:
    base = {"MAX": 94.0, "HIGH": 88.0, "SOLID": 82.0}.get(tier, 75.0)
    try:
        win = float(win_pct or 0)
    except (TypeError, ValueError):
        win = 0.0
    try:
        sc = float(score or 0)
    except (TypeError, ValueError):
        sc = 0.0
    # Blend a little of hist + ensemble into the display confidence
    bump = min(4.0, (win - 80.0) * 0.08) + min(2.0, max(0.0, sc - 70.0) * 0.1)
    return round(min(98.0, base + bump), 1)


def _pick_key(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").upper()
    occ = str(row.get("contract") or "")
    exp = str(row.get("expiry") or "")
    strike = row.get("strike")
    right = str(row.get("right") or "C").upper()
    return f"{sym}|{occ}|{exp}|{strike}|{right}"


def _day_cst(now: datetime | None = None) -> str:
    now = now or datetime.now(tz=CT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CT)
    return now.astimezone(CT).strftime("%Y-%m-%d")


def load_day_store(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    from odte_scanner.json_util import load_json_state

    data = load_json_state(p)
    return data if isinstance(data, dict) else {}


def save_day_store(path: str | Path | None, store: dict[str, Any]) -> None:
    if not path:
        return
    p = Path(path)
    try:
        from odte_scanner.json_util import atomic_write_text

        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(store, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _normalize_candidate(row: dict[str, Any], *, source_desk: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").upper()
    if not sym:
        return None
    action = str(row.get("action") or "").upper()
    # Only live buy-side promotions
    if action and action not in {
        "BUY_NOW",
        "ENTRY",
        "BUY_RIP",
        "RADAR_HOT",
        "SNIPER",
        "SURE_WINNER",
    }:
        return None
    ask = row.get("ask")
    try:
        ask_f = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        ask_f = None
    contract = row.get("contract")
    if not contract or ask_f is None or ask_f <= 0:
        return None

    win = row.get("win_pct")
    if win is None:
        win = row.get("hist_win_pct")
    n = row.get("win_samples")
    if n is None:
        n = row.get("hist_samples")
    score = row.get("score")
    if score is None:
        score = row.get("ensemble_score")
    if score is None:
        score = row.get("strength")

    certainty = row.get("certainty_tier") or certainty_from_hist(win, n)
    tier = confidence_tier_for(
        certainty=str(certainty),
        score=score if isinstance(score, (int, float)) else None,
        hit_1pct=row.get("hit_1pct"),
    )
    if tier is None:
        return None

    return {
        **row,
        "symbol": sym,
        "ask": ask_f,
        "contract": str(contract),
        "right": str(row.get("right") or "C").upper()[:1] or "C",
        "win_pct": float(win) if win is not None else None,
        "win_samples": int(n) if n is not None else None,
        "score": float(score) if score is not None else None,
        "certainty_tier": certainty,
        "confidence_tier": tier,
        "source_desk": source_desk,
        "pick_key": _pick_key({**row, "symbol": sym, "contract": contract}),
    }


INDEX_SURE_SYMBOLS = frozenset({"SPY", "QQQ", "IWM", "SPX", "XSP", "DIA"})


def gex_blocks_sure_candidate(
    row: dict[str, Any],
    red_flag: dict[str, Any] | None,
    *,
    respect_gex: bool = True,
) -> bool:
    """True when CBOE/Red Flag says skip this index 0–1 DTE call as a sure winner.

    LONG_GAMMA → pin/range day — no SPY/QQQ/IWM short-dated long calls.
    RED_FLAG + block_0dte_long_calls → same (dealer short-gamma / charm crush).
    """
    if not respect_gex or not red_flag:
        return False
    sym = str(row.get("symbol") or "").upper()
    if sym not in INDEX_SURE_SYMBOLS:
        return False
    right = str(row.get("right") or "C").upper()[:1]
    if right == "P":
        return False
    dte = row.get("dte")
    bucket = str(row.get("dte_bucket") or row.get("horizon") or "")
    short_dte = bucket == "0dte" or (dte is not None and int(dte) <= 1)
    if not short_dte:
        return False
    cboe = red_flag.get("cboe_gex") if isinstance(red_flag.get("cboe_gex"), dict) else {}
    regime = str(
        cboe.get("regime")
        or red_flag.get("gex_regime")
        or ""
    ).upper()
    if regime == "LONG_GAMMA":
        return True
    if red_flag.get("block_0dte_long_calls"):
        return True
    if str(red_flag.get("state") or "").upper() == "RED_FLAG":
        return True
    return False


def collect_candidates(
    *,
    actions: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    rip_radar: dict[str, Any] | None = None,
    min_ask: float = 0.15,
    max_ask: float = 5.0,
    mega_max_ask: float | None = None,
    allow_solid: bool = True,
    red_flag: dict[str, Any] | None = None,
    respect_gex: bool = True,
) -> list[dict[str, Any]]:
    """Pull buy-side tickets that can become sure winners."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    mega_cap = float(mega_max_ask) if mega_max_ask is not None else float(max_ask)

    def _ask_cap(sym: str) -> float:
        try:
            from odte_scanner.signals.rip_radar import is_mega_rip_symbol

            if is_mega_rip_symbol(sym):
                return mega_cap
        except Exception:  # noqa: BLE001
            pass
        return float(max_ask)

    def push(row: dict[str, Any] | None, desk: str) -> None:
        if not row:
            return
        if gex_blocks_sure_candidate(row, red_flag, respect_gex=respect_gex):
            return
        norm = _normalize_candidate(row, source_desk=desk)
        if not norm:
            return
        if not allow_solid and norm["confidence_tier"] == "SOLID":
            return
        ask = float(norm["ask"])
        cap = _ask_cap(str(norm.get("symbol") or ""))
        if ask < min_ask or ask > cap:
            return
        from odte_scanner.options.quote_sanity import quote_bundle_ok

        if not quote_bundle_ok(norm):
            return
        key = norm["pick_key"]
        if key in seen:
            return
        seen.add(key)
        out.append(norm)

    acts = actions or {}
    for r in acts.get("buy_now") or []:
        push(r, "Options")
    ch = challenge or {}
    for r in ch.get("entry") or []:
        push({**r, "action": r.get("action") or "ENTRY"}, "Challenge")
    rip = rip_radar or {}
    for r in rip.get("buy_rip") or rip.get("buy_now") or []:
        # RIP only if it already has elite/perfect hist (normalize will filter)
        push({**r, "action": r.get("action") or "BUY_RIP"}, "RIP/META")

    out.sort(
        key=lambda r: (
            TIER_RANK.get(str(r.get("confidence_tier")), 9),
            0 if str(r.get("certainty_tier")) == "perfect" else 1,
            -(float(r.get("win_pct") or 0)),
            -(float(r.get("hit_1pct") or 0)),
            -(float(r.get("score") or 0)),
            float(r.get("ask") or 99),
        )
    )
    return out


def _to_sure_winner(row: dict[str, Any], *, day: str) -> SureWinner:
    tier = str(row["confidence_tier"])
    promoted = bool(row.get("promotion_path"))
    if promoted:
        sizing = {
            "size_mult": float(row.get("size_mult") or 1.0),
            "size_label": str(row.get("size_label") or "1-LOT"),
            "size_hint": str(row.get("size_hint") or "Promoted gold — 1 contract, not GO HUGE"),
        }
    else:
        sizing = SIZE_BY_TIER[tier]
    conf = row.get("confidence_pct") if promoted and row.get("confidence_pct") is not None else None
    if conf is None:
        conf = confidence_pct_for(tier, row.get("win_pct"), row.get("score"))
    certainty = str(row.get("certainty_tier") or ("watch" if promoted else "strong"))
    win = row.get("win_pct")
    n = row.get("win_samples")
    ts = signal_timestamps()
    if row.get("signaled_at"):
        ts = {
            "signaled_at": str(row["signaled_at"]),
            "signaled_at_cst": str(
                row.get("signaled_at_cst") or to_cst_label(row["signaled_at"]) or row["signaled_at"]
            ),
        }
    right = str(row.get("right") or "C").upper()
    side = "PUT" if right == "P" else "CALL"
    if promoted and row.get("headline"):
        headline = str(row["headline"])
    else:
        headline = (
            f"{tier} · {sizing['size_label']} — {row['symbol']} {side} "
            f"({certainty} hist {float(win or 0):.0f}% n={int(n or 0)})"
        )
    if promoted and row.get("detail"):
        detail = str(row["detail"])
        detail_bits = None
    else:
        detail = None
        detail_bits = [
            f"Sure-winner #{tier}",
            sizing["size_hint"],
            f"hist {float(win or 0):.0f}% n={int(n or 0)}",
            f"conf ~{conf:.0f}%",
        ]
    if detail_bits is not None and row.get("hit_1pct") is not None:
        detail_bits.append(f"strike rate ≥1% {float(row['hit_1pct']):.0f}%")
    src_detail = row.get("headline") or row.get("detail") or row.get("recommend_reason") or ""
    if detail_bits is not None and src_detail:
        detail_bits.append(str(src_detail)[:180])
    playbook = list(row.get("playbook") or [])
    playbook = [f"sure_{tier.lower()}", sizing["size_label"].lower().replace(" ", "_"), *playbook][:8]
    if promoted and row.get("promotion_path"):
        playbook = [str(row["promotion_path"]), *playbook][:8]
    try:
        max_contracts = int(row["max_contracts"]) if row.get("max_contracts") is not None else None
    except (TypeError, ValueError):
        max_contracts = None

    return SureWinner(
        action="SURE_WINNER",
        symbol=str(row["symbol"]).upper(),
        confidence_tier=tier,
        confidence_pct=float(conf),
        size_mult=float(sizing["size_mult"]),
        size_label=sizing["size_label"],
        size_hint=sizing["size_hint"],
        color=str(row.get("color") or "gold") if promoted else COLOR_BY_TIER[tier],
        headline=headline,
        detail=detail if detail is not None else " · ".join(detail_bits or []),
        source_desk=str(row.get("source_desk") or "Options"),
        certainty_tier=certainty,
        right=right,
        strike=row.get("strike"),
        expiry=row.get("expiry"),
        dte=row.get("dte"),
        dte_bucket=row.get("dte_bucket") or row.get("horizon"),
        ask=row.get("ask"),
        bid=row.get("bid"),
        contract=row.get("contract"),
        score=row.get("score"),
        ensemble_score=row.get("ensemble_score") or row.get("score"),
        win_pct=row.get("win_pct"),
        win_samples=row.get("win_samples"),
        hit_1pct=row.get("hit_1pct"),
        hit_2pct=row.get("hit_2pct"),
        live_change_pct=row.get("live_change_pct") or row.get("session_change_pct"),
        spot=row.get("spot") or row.get("live_last") or row.get("live_spot"),
        exit_plan=row.get("exit_plan") or row.get("wall_exit_hint"),
        call_wall=row.get("call_wall"),
        put_wall=row.get("put_wall"),
        soft_exit=row.get("soft_exit"),
        wall_buffer_usd=row.get("wall_buffer_usd"),
        playbook=playbook,
        go_huge=tier == "MAX" and not promoted,
        signaled_at=ts["signaled_at"],
        signaled_at_cst=ts["signaled_at_cst"],
        day_cst=day,
        pick_key=row.get("pick_key") or _pick_key(row),
        max_contracts=max_contracts,
        promotion_path=str(row.get("promotion_path") or "") or None,
    )


def _select_bucket(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    store_picks: list[dict[str, Any]],
    tier_ok: set[str],
    taken_symbols: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Sticky-then-fill one bucket. ``limit`` is this bucket's own cap."""
    by_key = {str(c["pick_key"]): c for c in candidates if c.get("pick_key")}
    taken = set(taken_symbols or ())
    selected: list[dict[str, Any]] = []
    kept_keys: list[str] = []

    def _take(cand: dict[str, Any], key: str) -> bool:
        sym = str(cand.get("symbol") or "").upper()
        if not sym or sym in taken:
            return False
        selected.append(cand)
        kept_keys.append(key)
        taken.add(sym)
        return True

    for prior in store_picks:
        if len(selected) >= limit:
            break
        key = str(prior.get("pick_key") or "")
        cand = by_key.get(key)
        if not cand or cand.get("confidence_tier") not in tier_ok:
            continue
        _take(cand, key)

    for cand in candidates:
        if len(selected) >= limit:
            break
        key = str(cand.get("pick_key") or "")
        if not key or key in kept_keys:
            continue
        if cand.get("confidence_tier") not in tier_ok:
            continue
        if cand.get("confidence_tier") == "SOLID" and any(
            s.get("confidence_tier") in {"MAX", "HIGH"} for s in selected
        ):
            better = [
                c
                for c in candidates
                if c.get("pick_key") not in kept_keys
                and c.get("confidence_tier") in {"MAX", "HIGH"}
            ]
            if better:
                continue
        _take(cand, key)
    return selected, kept_keys


def select_sure_winners(
    candidates: list[dict[str, Any]],
    *,
    max_per_day: int = 2,
    day: str | None = None,
    store: dict[str, Any] | None = None,
    prefer_tiers: tuple[str, ...] = ("MAX", "HIGH", "SOLID"),
    promoted: list[dict[str, Any]] | None = None,
    promoted_extra_slots: int = 1,
    total_cap: int = 3,
) -> tuple[list[SureWinner], dict[str, Any]]:
    """Pick hist-gated winners plus a reserved slot for promoted golds.

    Hist-gated names stay hard-capped at 2 (``max_per_day`` cannot raise
    that). Promoted strong-trend / confirmed-flow cards use
    ``promoted_extra_slots`` and never push the board past ``total_cap``.
    """
    day = day or _day_cst()
    max_n = max(1, min(int(max_per_day), 2))  # hist hard cap 1–2
    extra = max(0, int(promoted_extra_slots))
    total = max(max_n, min(int(total_cap), max_n + extra))
    store = dict(store or {})
    if store.get("day") != day:
        store = {"day": day, "picks": []}

    tier_ok = set(prefer_tiers)
    hist = [c for c in candidates if not c.get("promotion_path")]
    promo = [c for c in (promoted or []) if c.get("promotion_path") and c.get("pick_key")]
    priors = [p for p in (store.get("picks") or []) if isinstance(p, dict)]
    hist_priors = [p for p in priors if not p.get("promotion_path")]
    promo_priors = [p for p in priors if p.get("promotion_path")]

    hist_selected, _hist_keys = _select_bucket(
        hist, limit=max_n, store_picks=hist_priors, tier_ok=tier_ok
    )
    promo_room = max(0, min(extra, total - len(hist_selected)))
    taken = {str(c.get("symbol") or "").upper() for c in hist_selected}
    promo_selected, _promo_keys = _select_bucket(
        promo,
        limit=promo_room,
        store_picks=promo_priors,
        tier_ok={"MAX", "HIGH"},
        taken_symbols=taken,
    )
    selected = hist_selected + promo_selected

    winners = [_to_sure_winner(c, day=day) for c in selected[:total]]
    # Preserve first-seen time for sticky picks
    prior_by_key = {str(p.get("pick_key")): p for p in priors if p.get("pick_key")}
    new_picks = []
    for w in winners:
        prior = prior_by_key.get(str(w.pick_key) or "")
        if prior and prior.get("signaled_at"):
            w.signaled_at = str(prior["signaled_at"])
            w.signaled_at_cst = str(
                prior.get("signaled_at_cst") or to_cst_label(w.signaled_at) or w.signaled_at
            )
        new_picks.append(
            {
                "pick_key": w.pick_key,
                "symbol": w.symbol,
                "contract": w.contract,
                "confidence_tier": w.confidence_tier,
                "promotion_path": w.promotion_path,
                "signaled_at": w.signaled_at,
                "signaled_at_cst": w.signaled_at_cst,
            }
        )
    store = {"day": day, "picks": new_picks}
    return winners, store


def annotate_buy_rows(
    rows: list[dict[str, Any]] | None,
    winners: list[SureWinner] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stamp sure-winner flags onto matching buy_now / entry rows for color coding."""
    if not rows:
        return list(rows or [])
    win_maps: dict[str, dict[str, Any]] = {}
    for w in winners:
        d = w.to_dict() if isinstance(w, SureWinner) else dict(w)
        key = str(d.get("pick_key") or _pick_key(d))
        win_maps[key] = d
        occ = str(d.get("contract") or "")
        if occ:
            win_maps[f"occ:{occ}"] = d
    out = []
    for raw in rows:
        row = dict(raw)
        key = _pick_key(row)
        match = win_maps.get(key) or win_maps.get(f"occ:{row.get('contract') or ''}")
        if match:
            row["sure_winner"] = True
            row["go_huge"] = bool(match.get("go_huge"))
            row["confidence_tier"] = match.get("confidence_tier")
            row["confidence_pct"] = match.get("confidence_pct")
            row["size_mult"] = match.get("size_mult")
            row["size_label"] = match.get("size_label")
            row["size_hint"] = match.get("size_hint")
            row["color"] = match.get("color")
            if not row.get("certainty_tier"):
                row["certainty_tier"] = match.get("certainty_tier")
        out.append(row)
    return out


def _option_rows_from_actions(actions: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(actions, dict):
        return rows
    for key in ("buy_now", "wait", "all", "buy_now_0dte", "buy_now_weekly", "buy_now_calls"):
        for row in actions.get(key) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def build_sure_winner_board(
    *,
    actions: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    rip_radar: dict[str, Any] | None = None,
    max_per_day: int = 2,
    min_ask: float = 0.15,
    max_ask: float = 5.0,
    mega_max_ask: float | None = None,
    allow_solid: bool = True,
    store_path: str | Path | None = "outputs/sure_winners_day.json",
    now: datetime | None = None,
    red_flag: dict[str, Any] | None = None,
    respect_gex: bool = True,
    scores: list[dict[str, Any]] | None = None,
    option_flow: Any = None,
    option_rows: list[dict[str, Any]] | None = None,
    quotes: dict[str, Any] | None = None,
    promotion_cfg: dict[str, Any] | None = None,
    promoted_extra_slots: int | None = None,
    total_cap: int | None = None,
) -> dict[str, Any]:
    """Build the capped sure-winner board and annotate source desks."""
    day = _day_cst(now)
    store = load_day_store(store_path)
    candidates = collect_candidates(
        actions=actions,
        challenge=challenge,
        rip_radar=rip_radar,
        min_ask=min_ask,
        max_ask=max_ask,
        mega_max_ask=mega_max_ask,
        allow_solid=allow_solid,
        red_flag=red_flag,
        respect_gex=respect_gex,
    )
    cfg = dict(promotion_cfg or {})
    extra = int(
        promoted_extra_slots
        if promoted_extra_slots is not None
        else cfg.get("sure_winners_promoted_extra_slots", 1)
    )
    cap_total = int(total_cap if total_cap is not None else cfg.get("sure_winners_total_cap", 3))
    from odte_scanner.signals.trend_gold import collect_promotions

    contract_rows = list(option_rows or []) + _option_rows_from_actions(actions)
    promoted = collect_promotions(
        scores=scores,
        option_rows=contract_rows,
        option_flow=option_flow,
        quotes=quotes,
        cfg=cfg,
        now=now,
        red_flag=red_flag,
        respect_gex=respect_gex,
    )
    prefer = ("MAX", "HIGH", "SOLID") if allow_solid else ("MAX", "HIGH")
    winners, store = select_sure_winners(
        candidates,
        max_per_day=max_per_day,
        day=day,
        store=store,
        prefer_tiers=prefer,
        promoted=promoted,
        promoted_extra_slots=extra,
        total_cap=cap_total,
    )
    save_day_store(store_path, store)

    winner_dicts = [w.to_dict() for w in winners]
    # Annotate source boards in-place copies for the snapshot
    actions_out = dict(actions or {})
    if actions_out.get("buy_now") is not None:
        actions_out["buy_now"] = annotate_buy_rows(actions_out.get("buy_now"), winners)
        for key in ("buy_now_0dte", "buy_now_weekly", "buy_now_calls", "buy_now_puts", "all"):
            if actions_out.get(key) is not None:
                actions_out[key] = annotate_buy_rows(actions_out.get(key), winners)

    challenge_out = dict(challenge or {})
    if challenge_out.get("entry") is not None:
        challenge_out["entry"] = annotate_buy_rows(challenge_out.get("entry"), winners)
    if challenge_out.get("tickets") is not None:
        challenge_out["tickets"] = annotate_buy_rows(challenge_out.get("tickets"), winners)

    max_n = sum(1 for w in winners if w.confidence_tier == "MAX")
    high_n = sum(1 for w in winners if w.confidence_tier == "HIGH")
    solid_n = sum(1 for w in winners if w.confidence_tier == "SOLID")

    hist_cap = max(1, min(int(max_per_day), 2))
    note = (
        f"Cap {hist_cap}/day hist-gated · sticky CST day {day}. "
        "MAX = GO HUGE (×3), HIGH = SIZE UP (×2), SOLID = FULL SIZE (×1.5). "
        f"Promoted trend/flow gold uses {max(0, extra)} reserved slot "
        f"(board cap {max(hist_cap, min(cap_total, hist_cap + max(0, extra)))}). "
        "Hist gate is unchanged. Hist edge ≠ guaranteed profit."
    )
    if respect_gex:
        note += " GEX/Red Flag checked — index 0–1 DTE calls skipped on LONG_GAMMA / RED_FLAG."
    if not winners:
        note = (
            f"No sure-winner this snapshot — need perfect/elite hist + live contract/ask "
            f"(cap {max(1, min(max_per_day, 2))}/day). {note}"
        )

    gex_meta = None
    if red_flag:
        cboe = red_flag.get("cboe_gex") if isinstance(red_flag.get("cboe_gex"), dict) else {}
        gex_meta = {
            "respect_gex": bool(respect_gex),
            "state": red_flag.get("state"),
            "block_0dte_long_calls": bool(red_flag.get("block_0dte_long_calls")),
            "cboe_regime": cboe.get("regime"),
        }

    return {
        "generated_at": datetime.now(tz=CT).isoformat(),
        "day_cst": day,
        "max_per_day": max(1, min(int(max_per_day), 2)),
        "promoted_extra_slots": max(0, extra),
        "total_cap": max(hist_cap, min(cap_total, hist_cap + max(0, extra))),
        "winners": winner_dicts,
        "primary": winner_dicts[0] if winner_dicts else None,
        "buy_now": winner_dicts,  # alias for NOW board collectors
        "candidates_n": len(candidates),
        "counts": {
            "winners": len(winners),
            "max": max_n,
            "high": high_n,
            "solid": solid_n,
            "go_huge": sum(1 for w in winners if w.go_huge),
            "promoted": sum(1 for w in winners if w.promotion_path),
            "candidates": len(candidates),
        },
        "size_guide": SIZE_BY_TIER,
        "note": note,
        "gex": gex_meta,
        "actions": actions_out,
        "challenge": challenge_out,
        "store": store,
    }
