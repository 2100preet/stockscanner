"""Outbound trade alerts (WhatsApp, etc.)."""

from odte_scanner.alerts.dispatcher import dispatch_snapshot_alerts

__all__ = ["dispatch_snapshot_alerts"]
