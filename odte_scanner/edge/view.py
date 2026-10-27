"""Build the Edge panel payload from the scorecard and the paper ledgers."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from odte_scanner.edge.gate import (
    MIN_SAMPLES,
    alert_desks_for,
    decide_sources,
    journal_source,
    routed_sources,
    source_for_webull,
)
from odte_scanner.edge.metrics import (
    bad_spot_reasons,
    dte_bucket,
    moneyness_bucket,
    summarize_pairs,
    tod_bucket,
)
from odte_scanner.edge.filters import GF_DTE, GF_MID, GF_NEAR, GF_OPEN, GF_PUT, GOLD_FAVOR_NOTE
from odte_scanner.edge.diamond_study import public_summary as diamond_public_summary
from odte_scanner.edge.study import PAPER_AUDIT, RECORDED_AT, SPREAD_NOTE, STUDY, UNDERLYING_NOTE
from odte_scanner.scorecard.service import MOVE_NOTE

ROOT = Path(__file__).resolve().parents[2]

LIMITATIONS = [
    "Tradier and Polygon keys were not available for this audit, so option prices were not rebuilt from the chain. Option dollars are paper fills and SPX structure marks.",
    "The scorecard is the underlying move in the signal's direction. It is not an option fill. It does not mute Telegram or turn paper buys off.",
    "Rows whose stored reference price cannot be the underlying (AMAT, ALAB, CRM, NOW, and any strike that disagrees) are left out of the underlying table. The ledger itself is not rewritten.",
    "IV rich or cheap, and the bid-ask, are not stored on the scorecard, so those slices are not estimated.",
    "Each signal has no market-regime tag. The index study is separate and is not a scanner signal.",
    "Opening-range breakout uses about 60 days of 5-minute bars. The daily studies run from 2023-01-03 through 2026-10-26.",
    "The debit-spread column is a capped model of the underlying move, not a quoted vertical.",
    "Diamond is a separate book. Its 60-day figure is an in-sample 0.50-delta model on Yahoo 5-minute bars, not a chain fill. The live option-dollar row stays empty until paper sells close.",
    "Webull stays on the sandbox paper host. Paper buys stay on. This panel does not turn on live trading.",
    "The recommendation log's pulse marks are not used. The same idea was counted many times.",
    "A contract that appears in both the signal journal and the Webull ledger is counted in each. The route still needs the combined sample to clear the bar.",
]


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _trade_samples(blob: Any, default_source: str) -> list[dict[str, Any]]:
    if isinstance(blob, dict):
        trades = blob.get("trades") or []
    elif isinstance(blob, list):
        trades = blob
    else:
        return []
    out: list[dict[str, Any]] = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or "").lower()
        if status != "closed" and not trade.get("exited_at"):
            continue
        pnl = trade.get("pnl_usd")
        if not isinstance(pnl, (int, float)):
            continue
        try:
            qty = float(trade.get("contracts") or trade.get("quantity") or trade.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1.0
        if qty <= 0:
            qty = 1.0
        hold = trade.get("hold_minutes")
        try:
            hold_f = float(hold) if hold is not None else None
        except (TypeError, ValueError):
            hold_f = None
        # A clock flat with no hold and no dollars is not a trade.
        if hold_f == 0 and abs(float(pnl)) < 0.5:
            continue
        out.append(
            {
                "source": journal_source(trade, default_source),
                "pnl": float(pnl) / qty,
                "at": str(trade.get("exited_at") or trade.get("closed_at") or trade.get("entered_at") or ""),
            }
        )
    return out


def _webull_samples(blob: Any) -> list[dict[str, Any]]:
    orders = blob.get("orders") if isinstance(blob, dict) else None
    if not isinstance(orders, list):
        return []
    buys: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sells: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("status") or "") not in {"filled", "partial"}:
            continue
        price = order.get("fill_price")
        if not isinstance(price, (int, float)) or float(price) <= 0:
            continue
        try:
            qty = int(order.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        action = str(order.get("action") or "").upper()
        row = {
            "contract": str(order.get("contract") or ""),
            "desk": str(order.get("desk") or ""),
            "qty": qty,
            "px": float(price),
            "at": str(order.get("updated_at") or order.get("created_at") or ""),
        }
        if not row["contract"]:
            continue
        if action == "BUY":
            buys[row["contract"]].append({**row, "left": qty})
        elif action == "SELL":
            sells.append(row)
    for lots in buys.values():
        lots.sort(key=lambda item: item["at"])
    out: list[dict[str, Any]] = []
    for sell in sorted(sells, key=lambda item: item["at"]):
        lots = buys.get(sell["contract"]) or []
        left = sell["qty"]
        while left > 0 and lots:
            lot = lots[0]
            take = min(left, int(lot["left"]))
            out.append(
                {
                    "source": source_for_webull(str(lot["desk"])),
                    "pnl": (sell["px"] - float(lot["px"])) * 100.0,
                    "at": sell["at"],
                    "contract": sell["contract"],
                }
            )
            lot["left"] = int(lot["left"]) - take
            left -= take
            if lot["left"] <= 0:
                lots.pop(0)
    return out


def _spx_samples(scorecard: dict[str, Any]) -> list[dict[str, Any]]:
    block = scorecard.get("spx_strategies") if isinstance(scorecard.get("spx_strategies"), dict) else {}
    out: list[dict[str, Any]] = []
    for row in block.get("rows") or []:
        if not isinstance(row, dict):
            continue
        outcome = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else {}
        if str(outcome.get("status") or "") != "closed":
            continue
        pnl = outcome.get("pnl")
        if not isinstance(pnl, (int, float)):
            continue
        out.append(
            {
                "source": "SPX Strategy",
                "pnl": float(pnl),
                "at": str(row.get("signaled_at") or ""),
            }
        )
    return out


def load_option_samples(scorecard: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Closed option dollars from the local paper ledgers. Missing files add nothing."""
    samples: list[dict[str, Any]] = []
    samples.extend(_trade_samples(_read_json(ROOT / "outputs" / "signal_journal.json"), "Options"))
    samples.extend(_trade_samples(_read_json(ROOT / "outputs" / "challenge_ledger.json"), "Challenge"))
    samples.extend(_trade_samples(_read_json(ROOT / "outputs" / "odte_1k_ledger.json"), "0DTE $1K"))
    from odte_scanner.edge.round_trips import load_round_trips

    trips = load_round_trips()
    seen = {
        (str(row.get("contract") or ""), str(row.get("at") or ""), round(float(row["pnl"]), 2))
        for row in trips
    }
    samples.extend(trips)
    for row in _webull_samples(_read_json(ROOT / "outputs" / "webull_orders.json")):
        key = (str(row.get("contract") or ""), str(row.get("at") or ""), round(float(row["pnl"]), 2))
        if key in seen:
            continue
        seen.add(key)
        samples.append(row)
    if isinstance(scorecard, dict):
        samples.extend(_spx_samples(scorecard))
    return samples


def _settled_underlying(row: dict[str, Any]) -> float | None:
    outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), dict) else {}
    eod = outcomes.get("eod") if isinstance(outcomes.get("eod"), dict) else {}
    if eod.get("status") != "settled":
        return None
    try:
        return float(eod["return_pct"])
    except (TypeError, ValueError, KeyError):
        return None


def underlying_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Signal-type table plus the strongest and weakest slices. Bad spots are counted out."""
    clean: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("reference_basis") or "") == "structure":
            continue
        if bad_spot_reasons(row):
            dropped += 1
            continue
        clean.append(row)

    def pairs_for(group: list[dict[str, Any]]) -> list[tuple[str, float]]:
        found: list[tuple[str, float]] = []
        for row in group:
            value = _settled_underlying(row)
            if value is None:
                continue
            found.append((str(row.get("signaled_at") or ""), value))
        return found

    def pack(name: str, group: list[dict[str, Any]]) -> dict[str, Any]:
        summary = summarize_pairs(pairs_for(group))
        summary["slice"] = name
        summary["unit"] = "underlying_pct"
        summary["routed"] = False
        per_share: list[tuple[str, float]] = []
        for row in group:
            value = _settled_underlying(row)
            try:
                ref = float(row.get("reference_entry"))
            except (TypeError, ValueError):
                ref = 0.0
            if value is None or ref <= 0:
                continue
            per_share.append((str(row.get("signaled_at") or ""), value / 100.0 * ref))
        dollars = summarize_pairs(per_share)
        summary["per_share"] = dollars.get("expectancy")
        summary["per_share_lo"] = dollars.get("lo")
        summary["per_share_hi"] = dollars.get("hi")
        return summary

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    slices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in clean:
        kind = str(row.get("signal_type") or "unknown")
        by_type[kind].append(row)
        direction = str(row.get("direction") or "unknown")
        slices[f"{kind} · {direction}"].append(row)
        slices[f"{kind} · {dte_bucket(row)}"].append(row)
        slices[f"{kind} · {tod_bucket(row.get('signaled_at'))}"].append(row)
        slices[f"{kind} · {moneyness_bucket(row)}"].append(row)
        ticker = str(row.get("ticker") or "")
        if ticker:
            slices[f"{kind} · {ticker}"].append(row)

    types = [pack(name, group) for name, group in sorted(by_type.items())]
    ranked = []
    for name, group in slices.items():
        packed = pack(name, group)
        if int(packed.get("n") or 0) < 8:
            continue
        ranked.append(packed)
    ranked.sort(key=lambda item: float(item["expectancy"]) if isinstance(item.get("expectancy"), (int, float)) else 0.0)
    highlights = ranked[:5] + ranked[-5:]
    # A short list can repeat the same ends. Keep the first copy.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in highlights:
        if row["slice"] in seen:
            continue
        seen.add(row["slice"])
        unique.append(row)
    unique.sort(key=lambda item: float(item["expectancy"]) if isinstance(item.get("expectancy"), (int, float)) else 0.0)
    return types, unique, dropped


def build_edge_report(*, scorecard: dict[str, Any] | None = None, samples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compact edge snapshot. ``samples`` overrides the on-disk ledgers in tests."""
    card = scorecard if isinstance(scorecard, dict) else {}
    if scorecard is None:
        from odte_scanner.scorecard.service import public_view

        card = public_view()
    raw = samples if samples is not None else load_option_samples(card)
    grouped: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sample in raw:
        if not isinstance(sample, dict):
            continue
        try:
            pnl = float(sample["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped[str(sample.get("source") or "unknown")].append((str(sample.get("at") or ""), pnl))
    option_rows = decide_sources(grouped, min_samples=MIN_SAMPLES)
    route = routed_sources(option_rows)
    desks = alert_desks_for(route)
    types, slices, dropped = underlying_rows(card.get("rows") or [])
    if route:
        verdict = (
            "Information only. "
            + ", ".join(route)
            + " would clear the option-dollar bar. Telegram and Webull paper are not muted by this table."
        )
    else:
        verdict = (
            "Information only. No signal has a proven option edge yet. Telegram still "
            "sends, and Webull paper buys stay on so closed lots can fill this table. "
            "Golden Flow skips are separate filters with a reason on the card. "
            "Open paper lots still exit."
        )
    return {
        "as_of": card.get("as_of"),
        "verdict": verdict,
        "route": route,
        "alert_desks": desks,
        "kept": route,
        "muted": [row["source"] for row in option_rows if not row.get("routed")],
        "bar": {
            "min_samples": MIN_SAMPLES,
            "confidence": 0.95,
            "oos": "later half of the settled trades must also have expectancy above zero",
            "unit": "US dollars per 1 option contract",
        },
        "option_sources": option_rows,
        "underlying": types,
        "slices": slices,
        "bad_spots_excluded": dropped,
        "study": STUDY,
        "study_recorded_at": RECORDED_AT,
        "study_note": UNDERLYING_NOTE,
        "spread_note": SPREAD_NOTE,
        "diamond": diamond_public_summary(),
        "paper_audit": PAPER_AUDIT,
        "paper_audit_at": "2026-10-27T03:04:05+00:00",
        "move_note": MOVE_NOTE,
        "filters": [
            {"applies_to": "Golden Flow alerts", "reason": GF_PUT},
            {"applies_to": "Golden Flow alerts", "reason": GF_NEAR},
            {"applies_to": "Golden Flow alerts", "reason": GF_DTE},
            {"applies_to": "Golden Flow alerts", "reason": GF_OPEN},
            {"applies_to": "Golden Flow alerts", "reason": GF_MID},
            {
                "applies_to": "Gold Setup rank and the standard strike band",
                "reason": GOLD_FAVOR_NOTE
                + " Standard board strikes aim inside 1% of the money instead of a 3% target. Chase caps above 3% are unchanged. SPX uses a debit spread when IV is rich and does not fall back to a naked long.",
            },
        ],
        "limitations": LIMITATIONS,
        "disclaimer": (
            "There is no foolproof system. This table does not mute alerts or paper "
            "buys. A filter that improves the past sample is not proof the next "
            "trade wins. Past results are not a promise."
        ),
    }


def alert_desk_allowlist() -> set[str]:
    return set(build_edge_report().get("alert_desks") or [])


def option_route() -> set[str]:
    return set(build_edge_report().get("route") or [])
