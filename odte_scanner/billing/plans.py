"""Public price catalog. Stripe price IDs come from the environment, never the repo."""

from __future__ import annotations

import os
from typing import Any

FOUNDING_CAP_DEFAULT = 100

# (tier, interval) -> env var holding the Stripe Price ID.
PRICE_ENV: dict[tuple[str, str], str] = {
    ("pro", "month"): "STRIPE_PRICE_PRO_MONTHLY",
    ("pro", "year"): "STRIPE_PRICE_PRO_YEARLY",
    ("pro_plus", "month"): "STRIPE_PRICE_PRO_PLUS_MONTHLY",
    ("pro_plus", "year"): "STRIPE_PRICE_PRO_PLUS_YEARLY",
    ("founding", "month"): "STRIPE_PRICE_FOUNDING_MONTHLY",
}

PAID_TIERS = ("pro", "pro_plus", "founding")

# Display prices are the offer. Checkout charges whatever Price the env ID points at.
PLANS: list[dict[str, Any]] = [
    {
        "id": "free",
        "name": "Morning Spark",
        "price_month": 0,
        "price_year": None,
        "trial_days": 0,
        "paid_channel": False,
        "blurb": "Free daily digest before the open. No card.",
        "features": [
            "Weekday digest on the public Telegram channel",
            "Delayed Gold Setup recap from the last published board",
            "Email saved on this server so you can be told when the digest moves",
        ],
    },
    {
        "id": "pro",
        "name": "Pro",
        "price_month": 29,
        "price_year": 249,
        "trial_days": 7,
        "paid_channel": True,
        "blurb": "Real-time Gold Setup and Golden Flow alerts.",
        "features": [
            "Private Telegram channel",
            "Gold Setup and Golden Flow as they clear the filter",
            "7-day trial, card required",
            "Cancel in the Stripe customer portal",
        ],
    },
    {
        "id": "pro_plus",
        "name": "Pro+",
        "price_month": 59,
        "price_year": 499,
        "trial_days": 0,
        "paid_channel": True,
        "blurb": "Same real-time alerts as Pro, at the higher published price.",
        "features": [
            "Everything in Pro",
            "Watchlist filters, an intraday flow summary, a members chat, and a monthly live session are planned and are not included yet",
        ],
    },
    {
        "id": "founding",
        "name": "Founding Member",
        "price_month": 19,
        "price_year": None,
        "trial_days": 0,
        "paid_channel": True,
        "blurb": "Pro alerts at $19/mo for the first 100 people. This Stripe price stays the price you subscribed to.",
        "features": [
            "Same Telegram alerts as Pro",
            "Monthly only",
            "First 100 checkouts",
            "Cancel anytime in the Stripe customer portal",
        ],
    },
]


def founding_cap() -> int:
    raw = (os.environ.get("FOUNDING_MEMBER_CAP") or "").strip()
    if not raw:
        return FOUNDING_CAP_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return FOUNDING_CAP_DEFAULT


def plan_by_id(tier: str) -> dict[str, Any] | None:
    key = (tier or "").strip().lower()
    for plan in PLANS:
        if plan["id"] == key:
            return plan
    return None


def price_env_name(tier: str, interval: str) -> str | None:
    return PRICE_ENV.get(((tier or "").strip().lower(), (interval or "").strip().lower()))


def price_id(tier: str, interval: str) -> str | None:
    name = price_env_name(tier, interval)
    if not name:
        return None
    value = (os.environ.get(name) or "").strip()
    return value or None


def stripe_secret() -> str | None:
    value = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    return value or None


def stripe_webhook_secret() -> str | None:
    value = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    return value or None


def trial_days(tier: str, interval: str) -> int:
    plan = plan_by_id(tier)
    if not plan:
        return 0
    if (interval or "").strip().lower() not in {"month", "year"}:
        return 0
    if plan["id"] != "pro":
        return 0
    try:
        return max(0, int(plan.get("trial_days") or 0))
    except (TypeError, ValueError):
        return 0


def public_catalog() -> dict[str, Any]:
    """Prices and which Stripe prices are configured. No secret values."""
    plans = []
    for plan in PLANS:
        item = {
            "id": plan["id"],
            "name": plan["name"],
            "price_month": plan["price_month"],
            "price_year": plan["price_year"],
            "trial_days": plan["trial_days"],
            "blurb": plan["blurb"],
            "features": list(plan["features"]),
            "paid_channel": plan["paid_channel"],
        }
        if plan["id"] in PAID_TIERS:
            item["prices_configured"] = {
                "month": bool(price_id(plan["id"], "month")),
                "year": bool(price_id(plan["id"], "year")) if plan["price_year"] else False,
            }
        plans.append(item)
    return {
        "plans": plans,
        "stripe_configured": bool(stripe_secret()),
        "founding_cap": founding_cap(),
    }
