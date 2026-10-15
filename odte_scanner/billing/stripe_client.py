"""Stripe REST calls. Secrets are read from the environment at request time."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import requests

from odte_scanner.billing.copy import DISCLAIMER
from odte_scanner.billing.plans import price_env_name, price_id, stripe_secret, trial_days

API = "https://api.stripe.com/v1"


class StripeError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def verify_stripe_signature(
    payload: bytes,
    header: str | None,
    secret: str | None,
    *,
    now: float | None = None,
    tolerance: int = 300,
) -> bool:
    """Stripe-Signature: t=unix,v1=hex hmac sha256 of '{t}.{payload}'."""
    if not payload or not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in str(header).split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    stamps = parts.get("t") or []
    signatures = parts.get("v1") or []
    if not stamps or not signatures:
        return False
    try:
        stamp = int(stamps[0])
    except ValueError:
        return False
    moment = time.time() if now is None else float(now)
    if abs(moment - stamp) > tolerance:
        return False
    signed = f"{stamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def public_site_url() -> str:
    return (os.environ.get("PUBLIC_SITE_URL") or "https://2100preet.github.io/stockscanner").rstrip("/")


def public_api_base() -> str:
    return (os.environ.get("PUBLIC_API_BASE") or "https://stockscanner.fly.dev").rstrip("/")


def checkout_return_urls(origin: str | None) -> tuple[str, str]:
    """Success and cancel URLs. The browser origin picks Fly vs GitHub Pages."""
    pages = public_site_url()
    api = public_api_base()
    cleaned = (origin or "").rstrip("/")
    if cleaned and (cleaned == api or _local_origin(cleaned)):
        base = cleaned + "/subscribe"
    else:
        base = pages + "/subscribe"
    success = base + "/success.html?session_id={CHECKOUT_SESSION_ID}"
    cancel = base + "/"
    return success, cancel


def create_checkout_session(
    *,
    email: str,
    tier: str,
    interval: str,
    origin: str | None = None,
    http_post=None,
) -> dict[str, Any]:
    secret = stripe_secret()
    if not secret:
        raise StripeError("Stripe is not configured (set STRIPE_SECRET_KEY)", status_code=503)
    env_name = price_env_name(tier, interval)
    if not env_name:
        raise StripeError(f"{tier} is not sold on the {interval} interval", status_code=400)
    chosen = price_id(tier, interval)
    if not chosen:
        raise StripeError(f"Stripe price is not configured (set {env_name})", status_code=503)
    success, cancel = checkout_return_urls(origin)
    pairs: list[tuple[str, str]] = [
        ("mode", "subscription"),
        ("success_url", success),
        ("cancel_url", cancel),
        ("customer_email", email),
        ("client_reference_id", tier),
        ("line_items[0][price]", chosen),
        ("line_items[0][quantity]", "1"),
        ("metadata[tier]", tier),
        ("metadata[interval]", interval),
        ("subscription_data[metadata][tier]", tier),
        ("subscription_data[metadata][interval]", interval),
        ("allow_promotion_codes", "true"),
        (
            "custom_text[submit][message]",
            DISCLAIMER + " The subscription renews until you cancel in the customer portal.",
        ),
    ]
    days = trial_days(tier, interval)
    if days:
        pairs.append(("subscription_data[trial_period_days]", str(days)))
    body = _stripe(
        "POST",
        "/checkout/sessions",
        pairs,
        secret=secret,
        http_post=http_post,
    )
    url = body.get("url")
    session_id = body.get("id")
    if not url or not session_id:
        raise StripeError("Stripe did not return a checkout URL", status_code=502)
    return {"id": session_id, "url": url, "price_id": chosen}


def retrieve_checkout_session(session_id: str, *, http_get=None) -> dict[str, Any]:
    secret = stripe_secret()
    if not secret:
        raise StripeError("Stripe is not configured (set STRIPE_SECRET_KEY)", status_code=503)
    return _stripe("GET", f"/checkout/sessions/{session_id}", None, secret=secret, http_get=http_get)


def create_portal_session(*, customer_id: str, return_url: str, http_post=None) -> dict[str, Any]:
    secret = stripe_secret()
    if not secret:
        raise StripeError("Stripe is not configured (set STRIPE_SECRET_KEY)", status_code=503)
    body = _stripe(
        "POST",
        "/billing_portal/sessions",
        [("customer", customer_id), ("return_url", return_url)],
        secret=secret,
        http_post=http_post,
    )
    url = body.get("url")
    if not url:
        raise StripeError("Stripe did not return a portal URL", status_code=502)
    return {"url": url}


def _stripe(
    method: str,
    path: str,
    pairs: list[tuple[str, str]] | None,
    *,
    secret: str,
    http_post=None,
    http_get=None,
) -> dict[str, Any]:
    url = API + path
    headers = {"Authorization": f"Bearer {secret}"}
    post = http_post or requests.post
    get = http_get or requests.get
    try:
        if method == "GET":
            response = get(url, headers=headers, timeout=20)
        else:
            response = post(url, data=pairs, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise StripeError(f"Stripe request failed: {exc}", status_code=502) from exc
    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:  # noqa: BLE001
        payload = {}
    if response.status_code >= 400:
        message = ((payload.get("error") or {}) if isinstance(payload.get("error"), dict) else {}).get("message")
        raise StripeError(str(message or "Stripe rejected the request")[:400], status_code=502)
    return payload


def _local_origin(origin: str) -> bool:
    host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in {"localhost", "127.0.0.1"}
