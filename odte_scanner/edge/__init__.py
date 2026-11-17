"""Measured-edge panel. Information until a source proves positive option expectancy.

``build_edge_report`` loads lazily. ``odte_scanner.signals.diamond`` imports
``odte_scanner.edge.metrics``; an eager import of the view here pulled the
Diamond study back into a half-initialized ``signals.diamond`` and raised
ImportError whenever Diamond was the first of the two to load.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_edge_report"]


def __getattr__(name: str) -> Any:
    if name == "build_edge_report":
        from odte_scanner.edge.view import build_edge_report

        return build_edge_report
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
