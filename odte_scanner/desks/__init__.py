"""Isolated paper desks.

Kotegawa and Million Challenge keep their own ledgers, sizing, and daily
loss counters. Nothing in this package reads or writes another desk's state.
"""

from odte_scanner.desks.service import portal_view, scorecard_rows

__all__ = ["portal_view", "scorecard_rows"]
