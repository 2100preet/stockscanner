"""Option-dollar bar for the Edge panel.

The bar is fixed: at least 40 settled option trades, a 95% interval on
expectancy per contract above zero, and a positive later half. The scorecard's
underlying move is not this test. An empty route is the honest result. The
route does not mute Telegram and does not block sandbox paper buys.
"""

from __future__ import annotations

from typing import Any

from odte_scanner.edge.metrics import clears_bar, summarize_pairs

MIN_SAMPLES = 40

# Telegram desk name -> option-dollar source. Unknown desks stay muted.
ALERT_DESK_SOURCE = {
    "Gold Setup": "Gold Setup",
    "Diamond": "Diamond",
    "Sniper": "Sniper",
    "Golden Flow": "Golden Flow",
    "Options": "Options",
    "Explosive": "Lottery",
    "RIP/META": "RIP",
    "Beauty": "Beauty",
    "Levels": "Levels",
    "ML6": "ML6",
    "Challenge": "Challenge",
    "0DTE $1K": "0DTE $1K",
    "SPX Strategy": "SPX Strategy",
}

# Sources the panel always lists, even when the sample is empty.
TRACKED_SOURCES = (
    "Gold Setup",
    "Diamond",
    "Sniper",
    "Golden Flow",
    "Options",
    "Challenge",
    "RIP",
    "Lottery",
    "0DTE $1K",
    "SPX Strategy",
    "Beauty",
    "Levels",
    "ML6",
)


def edge_gate_enabled(cfg: dict[str, Any] | None) -> bool:
    block = (cfg or {}).get("edge_gate")
    if not isinstance(block, dict):
        return True
    return bool(block.get("enabled", True))


def source_for_webull(desk: str) -> str:
    key = str(desk or "").strip().lower()
    if key in {"sure_winner", "golden_exit"}:
        return "Gold Setup"
    if key == "diamond":
        return "Diamond"
    if key == "sniper":
        return "Sniper"
    if key == "golden_flow":
        return "Golden Flow"
    if key == "challenge":
        return "Challenge"
    if key == "lottery":
        return "Lottery"
    if key in {"odte", "weekly", "swing"}:
        return "Options"
    return key or "unknown"


def source_for_alert(desk: str) -> str:
    return ALERT_DESK_SOURCE.get(str(desk or ""), "")


def journal_source(trade: dict[str, Any], default: str = "Options") -> str:
    reason = f"{trade.get('entry_reason') or ''} {trade.get('exit_reason') or ''}".lower()
    bucket = str(trade.get("dte_bucket") or trade.get("horizon") or "").lower()
    if "sniper" in reason:
        return "Sniper"
    if "diamond" in reason:
        return "Diamond"
    if "sure" in reason or "gold setup" in reason:
        return "Gold Setup"
    if "golden flow" in reason:
        return "Golden Flow"
    if "rip" in reason or "meta" in reason:
        return "RIP"
    if "challenge" in reason:
        return "Challenge"
    if "lottery" in reason or "explosive" in reason:
        return "Lottery"
    if "0dte $1k" in reason or "odte_1k" in reason or bucket in {"odte_1k", "1k"}:
        return "0DTE $1K"
    if default:
        return default
    return "Options"


def decide_sources(
    samples: dict[str, list[tuple[str, float]]],
    *,
    min_samples: int = MIN_SAMPLES,
) -> list[dict[str, Any]]:
    """One row per tracked source, plus any extra source that has trades."""
    names = list(TRACKED_SOURCES)
    for name in samples:
        if name not in names:
            names.append(name)
    rows: list[dict[str, Any]] = []
    for name in names:
        summary = summarize_pairs(samples.get(name) or [])
        summary["source"] = name
        summary["unit"] = "usd_per_contract"
        summary["routed"] = clears_bar(summary, min_samples=min_samples)
        rows.append(summary)
    return rows


def routed_sources(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["source"]) for row in rows if row.get("routed")]


def alert_desks_for(sources: list[str] | set[str]) -> list[str]:
    allowed = set(sources)
    return [desk for desk, source in ALERT_DESK_SOURCE.items() if source in allowed]
