"""JSON subscriber store on the Fly volume (outputs/)."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from odte_scanner.billing.plans import PAID_TIERS, founding_cap

ROOT = Path(__file__).resolve().parents[2]
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

ACTIVE_STATUSES = {"active", "trialing"}
PAID_COUNT_STATUSES = {"active", "trialing", "past_due", "canceled"}
PENDING_TTL = timedelta(hours=24)


def store_path() -> Path:
    raw = (os.environ.get("SUBSCRIBERS_PATH") or "").strip()
    if raw:
        return Path(raw)
    return ROOT / "outputs" / "subscribers.json"


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()) if path.is_absolute() else str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SubscriberStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else store_path()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"subscribers": [], "events": []}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"subscribers": [], "events": []}
        if not isinstance(raw, dict):
            return {"subscribers": [], "events": []}
        subs = raw.get("subscribers")
        events = raw.get("events")
        raw["subscribers"] = subs if isinstance(subs, list) else []
        raw["events"] = events if isinstance(events, list) else []
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def _locked(self):
        return _lock_for(self.path)

    def founding_taken(self, *, now: datetime | None = None) -> int:
        moment = now or _now()
        with self._locked():
            data = self._read()
            return _count_founding(data["subscribers"], moment)

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        key = _email(email)
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("email") == key:
                    return dict(row)
        return None

    def get_by_id(self, subscriber_id: str) -> dict[str, Any] | None:
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("id") == subscriber_id:
                    return dict(row)
        return None

    def get_by_session(self, session_id: str) -> dict[str, Any] | None:
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("stripe_checkout_session_id") == session_id:
                    return dict(row)
        return None

    def get_by_customer(self, customer_id: str) -> dict[str, Any] | None:
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("stripe_customer_id") == customer_id:
                    return dict(row)
        return None

    def get_by_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("stripe_subscription_id") == subscription_id:
                    return dict(row)
        return None

    def get_by_invite(self, invite_link: str) -> dict[str, Any] | None:
        link = (invite_link or "").strip()
        if not link:
            return None
        with self._locked():
            for row in self._read()["subscribers"]:
                if row.get("telegram_invite_link") == link:
                    return dict(row)
        return None

    def add_free(self, email: str) -> dict[str, Any]:
        """Save a digest email. An active paid row stays on its paid tier."""
        key = _email(email)
        with self._locked():
            data = self._read()
            row = _find_email(data["subscribers"], key)
            if row is None:
                row = _blank(key, tier="free", status="active")
                data["subscribers"].append(row)
            elif not (row.get("tier") in PAID_TIERS and row.get("status") in ACTIVE_STATUSES | {"past_due", "pending"}):
                row["tier"] = "free"
                row["status"] = "active"
            row["digest"] = True
            row["updated_at"] = _iso()
            row.setdefault("created_at", row["updated_at"])
            self._write(data)
            return dict(row)

    def begin_checkout(
        self,
        *,
        email: str,
        tier: str,
        interval: str,
        session_id: str,
        price_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        key = _email(email)
        moment = now or _now()
        with self._locked():
            data = self._read()
            if tier == "founding" and _count_founding(data["subscribers"], moment, extra_email=key) >= founding_cap():
                raise FoundingFullError("Founding member spots are full")
            row = _find_email(data["subscribers"], key)
            if row is not None and row.get("tier") in PAID_TIERS and row.get("status") in ACTIVE_STATUSES:
                raise AlreadySubscribedError(dict(row))
            if row is None:
                row = _blank(key, tier=tier, status="pending")
                data["subscribers"].append(row)
            row["tier"] = tier
            row["interval"] = interval
            row["status"] = "pending"
            row["price_id"] = price_id
            if tier == "founding":
                row["founding_claimed"] = True
            row["stripe_checkout_session_id"] = session_id
            row["updated_at"] = _iso(moment)
            row.setdefault("created_at", row["updated_at"])
            self._write(data)
            return dict(row)

    def claim_event(self, event_id: str) -> bool:
        """Return True the first time an event id is seen."""
        event_id = (event_id or "").strip()
        if not event_id:
            return False
        with self._locked():
            data = self._read()
            events = [str(item) for item in data["events"]]
            if event_id in events:
                return False
            events.append(event_id)
            data["events"] = events[-500:]
            self._write(data)
            return True

    def release_event(self, event_id: str) -> None:
        """Drop a claim so Stripe can retry a handler that failed."""
        event_id = (event_id or "").strip()
        if not event_id:
            return
        with self._locked():
            data = self._read()
            data["events"] = [str(item) for item in data["events"] if str(item) != event_id]
            self._write(data)

    def activate(
        self,
        *,
        email: str | None,
        tier: str | None,
        interval: str | None,
        customer_id: str | None,
        subscription_id: str | None,
        session_id: str | None,
        status: str,
        price_id: str | None = None,
    ) -> dict[str, Any]:
        key = _email(email) if email else ""
        with self._locked():
            data = self._read()
            row = None
            if session_id:
                row = next((item for item in data["subscribers"] if item.get("stripe_checkout_session_id") == session_id), None)
            if row is None and subscription_id:
                row = next((item for item in data["subscribers"] if item.get("stripe_subscription_id") == subscription_id), None)
            if row is None and customer_id:
                row = next((item for item in data["subscribers"] if item.get("stripe_customer_id") == customer_id), None)
            if row is None and key:
                row = _find_email(data["subscribers"], key)
            if row is None:
                row = _blank(key or "unknown@local", tier=tier or "pro", status=status)
                data["subscribers"].append(row)
            if row.get("status") == "canceled":
                row["telegram_invite_link"] = None
                row["telegram_user_id"] = None
                row["telegram_access"] = None
            if key:
                row["email"] = key
            if tier:
                row["tier"] = tier
            if interval:
                row["interval"] = interval
            if customer_id:
                row["stripe_customer_id"] = customer_id
            if subscription_id:
                row["stripe_subscription_id"] = subscription_id
            if session_id:
                row["stripe_checkout_session_id"] = session_id
            if price_id:
                row["price_id"] = price_id
            row["status"] = status
            row["updated_at"] = _iso()
            if status in ACTIVE_STATUSES:
                row["canceled_at"] = None
            self._write(data)
            return dict(row)

    def mark_canceled(
        self,
        *,
        subscription_id: str | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._locked():
            data = self._read()
            row = _find_billing(data["subscribers"], subscription_id=subscription_id, customer_id=customer_id)
            if row is None:
                return None
            row["status"] = "canceled"
            row["canceled_at"] = _iso()
            row["updated_at"] = row["canceled_at"]
            row["telegram_access"] = "revoked"
            self._write(data)
            return dict(row)

    def mark_payment_failed(
        self,
        *,
        subscription_id: str | None = None,
        customer_id: str | None = None,
        invoice_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._locked():
            data = self._read()
            row = _find_billing(data["subscribers"], subscription_id=subscription_id, customer_id=customer_id)
            if row is None:
                return None
            if row.get("status") != "canceled":
                row["status"] = "past_due"
            row["payment_failed_at"] = _iso()
            if invoice_id:
                row["last_invoice_id"] = invoice_id
            row["updated_at"] = row["payment_failed_at"]
            self._write(data)
            return dict(row)

    def set_invite(self, subscriber_id: str, invite_link: str | None, *, error: str | None = None) -> dict[str, Any] | None:
        with self._locked():
            data = self._read()
            row = next((item for item in data["subscribers"] if item.get("id") == subscriber_id), None)
            if row is None:
                return None
            row["telegram_invite_link"] = invite_link
            row["telegram_error"] = error
            if invite_link:
                row["telegram_access"] = "invited"
            row["updated_at"] = _iso()
            self._write(data)
            return dict(row)

    def set_telegram_user(self, invite_link: str, user_id: int | str) -> dict[str, Any] | None:
        link = (invite_link or "").strip()
        with self._locked():
            data = self._read()
            row = next((item for item in data["subscribers"] if item.get("telegram_invite_link") == link), None)
            if row is None:
                return None
            row["telegram_user_id"] = int(user_id)
            row["telegram_access"] = "joined"
            row["updated_at"] = _iso()
            self._write(data)
            return dict(row)


class FoundingFullError(Exception):
    pass


class AlreadySubscribedError(Exception):
    def __init__(self, subscriber: dict[str, Any]) -> None:
        super().__init__("already subscribed")
        self.subscriber = subscriber


def _email(value: str | None) -> str:
    return (value or "").strip().lower()


def _blank(email: str, *, tier: str, status: str) -> dict[str, Any]:
    moment = _iso()
    return {
        "id": uuid.uuid4().hex,
        "email": email,
        "tier": tier,
        "interval": None,
        "status": status,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "stripe_checkout_session_id": None,
        "price_id": None,
        "telegram_invite_link": None,
        "telegram_user_id": None,
        "telegram_access": None,
        "telegram_error": None,
        "created_at": moment,
        "updated_at": moment,
        "canceled_at": None,
        "payment_failed_at": None,
    }


def _find_email(rows: list[dict[str, Any]], email: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("email") == email:
            return row
    return None


def _find_billing(
    rows: list[dict[str, Any]],
    *,
    subscription_id: str | None,
    customer_id: str | None,
) -> dict[str, Any] | None:
    if subscription_id:
        for row in rows:
            if row.get("stripe_subscription_id") == subscription_id:
                return row
    if customer_id:
        for row in rows:
            if row.get("stripe_customer_id") == customer_id:
                return row
    return None


def _count_founding(rows: list[dict[str, Any]], now: datetime, *, extra_email: str | None = None) -> int:
    """Lifetime founding spots, plus a recent unpaid checkout so two tabs cannot both pass.

    ``founding_claimed`` stays set if that person later moves to another plan,
    so a used spot does not reopen.
    """
    taken = 0
    for row in rows:
        claimed = bool(row.get("founding_claimed") or row.get("tier") == "founding")
        if not claimed:
            continue
        if extra_email and row.get("email") == extra_email and row.get("status") == "pending":
            continue
        status = str(row.get("status") or "")
        if status in PAID_COUNT_STATUSES:
            taken += 1
            continue
        if status == "pending":
            created = _parse_iso(row.get("updated_at") or row.get("created_at"))
            if created is not None and now - created < PENDING_TTL:
                taken += 1
    return taken
