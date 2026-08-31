"""Build flow leaders from persisted ladder cache (offline/Pages-safe)."""
from __future__ import annotations

from typing import Any

from odte_scanner.echo.flow import build_flow_leaders, build_option_flow
from odte_scanner.echo.flow_deltas import load_all_cached_ladders


def flow_leaders_from_cache(*, top_n: int = 20) -> list[dict[str, Any]]:
    ladders = load_all_cached_ladders()
    if not ladders:
        return []
    flow = build_option_flow(ladders)
    return build_flow_leaders(flow.get("prints") or [], top_n=top_n)
