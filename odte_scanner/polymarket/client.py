"""Read-only public Polymarket client: Gamma, Data API v2, CLOB.

No auth, no keys. Every call is a plain GET/POST against a public endpoint.
Field names follow the live JSON as of Nov 2026:

* Gamma ``/events`` rows carry ``markets`` with ``conditionId``,
  ``clobTokenIds`` (JSON string), ``outcomes`` / ``outcomePrices`` (JSON
  strings), ``active``, ``closed``.
* Data API v2 rows are snake_case and wrapped as ``{"data": [...],
  "pagination": {...}}``; ``/v2/trades`` takes ``condition`` (<= 20 ids),
  ``taker_only`` (default true: makers are hidden), ``filter_type=CASH``,
  ``filter_amount``. Notional is ``size * price``.
* CLOB ``POST /midpoints`` returns ``{token_id: "0.0595"}``; ``GET /book``
  returns string ``price`` / ``size`` levels.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_URL = "https://data-api.polymarket.com/v2"
CLOB_URL = "https://clob.polymarket.com"
USER_AGENT = "stockscanner-polymarket/1.0"
MAX_CONDITIONS_PER_CALL = 20


class PolymarketError(RuntimeError):
    """A public read failed (HTTP error, 429, bad JSON)."""


class PolymarketClient:
    """Thin public client. ``session`` is injectable so tests stay hermetic."""

    def __init__(self, session: Any | None = None, *, timeout: float = 10.0) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.calls = 0

    # -- transport -------------------------------------------------------
    def _request(self, method: str, url: str, *, params: dict | None = None, body: Any = None) -> Any:
        self.calls += 1
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            if method == "POST":
                resp = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise PolymarketError(f"{type(exc).__name__} on {url.split('?')[0]}") from exc
        status = getattr(resp, "status_code", 0)
        if status == 429:
            raise PolymarketError("HTTP 429 rate limited")
        if status != 200:
            raise PolymarketError(f"HTTP {status} on {url.split('?')[0]}")
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PolymarketError("invalid JSON") from exc

    # -- Gamma -----------------------------------------------------------
    def events_by_tag(self, tag_id: int, *, limit: int = 100) -> list[dict]:
        data = self._request(
            "GET",
            f"{GAMMA_URL}/events",
            params={"tag_id": int(tag_id), "closed": "false", "active": "true", "limit": int(limit)},
        )
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def public_search(self, query: str, *, limit_per_type: int = 10) -> list[dict]:
        data = self._request(
            "GET",
            f"{GAMMA_URL}/public-search",
            params={"q": query, "limit_per_type": int(limit_per_type), "events_status": "active"},
        )
        events = data.get("events") if isinstance(data, dict) else None
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    # -- Data API v2 -----------------------------------------------------
    def trades(
        self,
        condition_ids: Iterable[str],
        *,
        min_cash: float = 250.0,
        limit: int = 500,
        taker_only: bool = False,
    ) -> list[dict]:
        """Recent fills for up to 20 conditions, newest first.

        ``taker_only=False`` matters: a resting bid that gets hit (the Nov 22
        Jensen wallet) only shows up on the maker side.
        """
        ids = [c for c in dict.fromkeys(condition_ids) if c][:MAX_CONDITIONS_PER_CALL]
        if not ids:
            return []
        data = self._request(
            "GET",
            f"{DATA_URL}/trades",
            params={
                "condition": ",".join(ids),
                "filter_type": "CASH",
                "filter_amount": float(min_cash),
                "limit": int(limit),
                "taker_only": "true" if taker_only else "false",
            },
        )
        rows = data.get("data") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def user_stats(self, wallet: str) -> dict:
        data = self._request("GET", f"{DATA_URL}/user-stats", params={"user": wallet})
        row = data.get("data") if isinstance(data, dict) else None
        return row if isinstance(row, dict) else {}

    def activity(self, wallet: str, condition_id: str, *, start: int | None = None, limit: int = 500) -> list[dict]:
        params: dict[str, Any] = {
            "user": wallet,
            "condition": condition_id,
            "type": "TRADE",
            "limit": int(limit),
            "sort_direction": "DESC",
        }
        if start:
            params["start"] = int(start)
        data = self._request("GET", f"{DATA_URL}/activity", params=params)
        rows = data.get("data") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # -- CLOB ------------------------------------------------------------
    def midpoints(self, token_ids: Iterable[str]) -> dict[str, float]:
        ids = [t for t in dict.fromkeys(token_ids) if t]
        out: dict[str, float] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            data = self._request("POST", f"{CLOB_URL}/midpoints", body=[{"token_id": t} for t in chunk])
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        out[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
        return out

    def book(self, token_id: str) -> dict:
        data = self._request("GET", f"{CLOB_URL}/book", params={"token_id": token_id})
        return data if isinstance(data, dict) else {}


def parse_json_list(value: Any) -> list:
    """Gamma ships ``outcomes`` / ``clobTokenIds`` as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def fill_notional(row: dict) -> float:
    """USD notional of a Data API fill: ``size * price`` (size is shares)."""
    try:
        return float(row.get("size") or 0) * float(row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0
