"""Sandbox routing guard for the isolated paper desks.

A desk may send an order to Webull's sandbox only when its own account id is
set (``KOTEGAWA_ACCOUNT_ID`` or ``MILLION_ACCOUNT_ID``) and the host in use is
``api.sandbox.webull.com``. A missing id stays on the internal ledger.

This module never reads ``WEBULL_ACCOUNT_ID``, ``WEBULL_ACCOUNT_ID_2``,
``WEBULL_PAPER_ACCOUNT_ID``, or ``WEBULL_LIVE``. ``WEBULL_LIVE`` is not
permission to use the production host, and this code never calls
``api.webull.com``.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

SANDBOX_HOST = "api.sandbox.webull.com"
PRODUCTION_HOST = "api.webull.com"

DESK_ACCOUNT_ENV = {
    "kotegawa": "KOTEGAWA_ACCOUNT_ID",
    "million": "MILLION_ACCOUNT_ID",
}

# Named here so tests can prove the resolver does not look them up.
# The resolver must not pass these names to ``environ.get``.
FORBIDDEN_ACCOUNT_ENV = (
    "WEBULL_ACCOUNT_ID",
    "WEBULL_ACCOUNT_ID_2",
    "WEBULL_PAPER_ACCOUNT_ID",
    "WEBULL_LIVE",
)


class RoutingError(RuntimeError):
    """Refused to route an order off the internal paper ledger."""


def dedicated_account_id(desk: str, environ: Mapping[str, str] | None = None) -> str:
    """Return the desk's own account id, or ``""`` when it is unset.

    Only the desk's dedicated variable is read.
    """
    name = DESK_ACCOUNT_ENV.get(desk)
    if not name:
        return ""
    env = environ if environ is not None else os.environ
    return str(env.get(name) or "").strip()


def sandbox_host_in_use(
    host: str | None = None,
    *,
    live_trading: Mapping[str, Any] | None = None,
) -> bool:
    """True only when the trade host is the Webull sandbox.

    ``WEBULL_LIVE`` is not consulted. A config that asks for the production
    host does not count as sandbox, and a caller that passes
    ``api.webull.com`` does not count either.
    """
    text = str(host or "").strip().lower()
    if not text:
        cfg = live_trading or {}
        sandbox = bool(cfg.get("sandbox", True))
        paper = bool(cfg.get("paper", True))
        text = SANDBOX_HOST if (sandbox or paper) else PRODUCTION_HOST
    return text == SANDBOX_HOST


def resolve_route(
    desk: str,
    *,
    host: str | None = None,
    live_trading: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Choose the internal ledger or the desk's own sandbox account.

    The returned ``account_id`` is either the dedicated id or ``None``.
    Shared Webull account variables are never copied in.
    """
    account = dedicated_account_id(desk, environ)
    if not account:
        return {
            "mode": "internal",
            "account_id": None,
            "host": None,
            "reason": "missing dedicated account id",
        }
    if not sandbox_host_in_use(host, live_trading=live_trading):
        return {
            "mode": "internal",
            "account_id": None,
            "host": None,
            "reason": "sandbox host not in use",
        }
    return {
        "mode": "sandbox",
        "account_id": account,
        "host": SANDBOX_HOST,
        "reason": "dedicated sandbox account",
    }


def submit_sandbox_order(
    *,
    desk: str,
    account_id: str | None,
    host: str | None,
    order: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    transport: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Record a sandbox order, or refuse it.

    The transport is invoked only after the host and account checks pass.
    There is no HTTP client here, and ``api.webull.com`` is refused before
    any transport runs.
    """
    resolved = str(host or "").strip().lower()
    if resolved != SANDBOX_HOST:
        raise RoutingError(f"refusing host {host or PRODUCTION_HOST}")
    expected = dedicated_account_id(desk, environ)
    if not account_id or account_id != expected:
        raise RoutingError("refusing account that is not the dedicated sandbox id")
    payload = {
        "desk": desk,
        "account_id": account_id,
        "host": SANDBOX_HOST,
        "order": dict(order),
    }
    if transport is None:
        return {"ok": True, "submitted": False, "recorded": True, **payload}
    result = transport(payload)
    if isinstance(result, dict):
        return {"ok": True, "submitted": True, **payload, "result": result}
    return {"ok": True, "submitted": True, **payload, "result": result}
