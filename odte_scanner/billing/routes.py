"""Flask routes for checkout, webhooks, digest signup, and the public pages."""

from __future__ import annotations

import logging
import re
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request

from odte_scanner.billing.copy import DISCLAIMER
from odte_scanner.billing.pages import render_site
from odte_scanner.billing.plans import PAID_TIERS, founding_cap, plan_by_id, public_catalog, stripe_webhook_secret
from odte_scanner.billing.store import AlreadySubscribedError, FoundingFullError, SubscriberStore
from odte_scanner.billing.stripe_client import (
    StripeError,
    checkout_return_urls,
    create_checkout_session,
    create_portal_session,
    public_api_base,
    public_site_url,
    retrieve_checkout_session,
    verify_stripe_signature,
)
from odte_scanner.billing.telegram_access import (
    approve_join,
    create_invite,
    decline_join,
    paid_channel_configured,
    revoke_access,
    webhook_secret as telegram_webhook_secret,
)

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ACTIVE = {"active", "trialing"}


def get_store() -> SubscriberStore:
    return SubscriberStore()


@billing_bp.after_request
def _cors(response: Response):
    if request.path.startswith("/api/billing/"):
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and _origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@billing_bp.route("/api/billing/<path:_rest>", methods=["OPTIONS"])
def billing_options(_rest: str):
    return ("", 204)


@billing_bp.get("/subscribe")
@billing_bp.get("/subscribe/")
def subscribe_index():
    return _html("index.html")


@billing_bp.get("/subscribe/<page>")
def subscribe_page(page: str):
    name = page if page.endswith(".html") else f"{page}.html"
    pages = render_site("")
    if name not in pages:
        abort(404)
    return _html(name)


@billing_bp.get("/api/billing/plans")
def plans():
    catalog = public_catalog()
    catalog["founding_remaining"] = max(0, catalog["founding_cap"] - get_store().founding_taken())
    catalog["paid_channel_configured"] = paid_channel_configured()
    catalog["disclaimer"] = DISCLAIMER
    return jsonify(catalog)


@billing_bp.post("/api/billing/digest")
def digest_signup():
    payload = request.get_json(silent=True) or {}
    if str(payload.get("company") or "").strip():
        return jsonify({"ok": True, "stored": False})
    email = _require_email(payload.get("email"))
    if email is None:
        return jsonify({"ok": False, "error": "Enter a valid email."}), 400
    row = get_store().add_free(email)
    return jsonify({"ok": True, "stored": True, "tier": row.get("tier"), "status": row.get("status")})


@billing_bp.post("/api/billing/checkout")
def checkout():
    payload = request.get_json(silent=True) or {}
    if str(payload.get("company") or "").strip():
        return jsonify({"ok": False, "error": "Checkout was not started."}), 400
    email = _require_email(payload.get("email"))
    if email is None:
        return jsonify({"ok": False, "error": "Enter a valid email."}), 400
    tier = str(payload.get("tier") or "").strip().lower()
    interval = str(payload.get("interval") or "month").strip().lower()
    plan = plan_by_id(tier)
    if plan is None or tier not in PAID_TIERS:
        return jsonify({"ok": False, "error": "Unknown plan."}), 400
    if interval not in {"month", "year"}:
        return jsonify({"ok": False, "error": "Interval must be month or year."}), 400
    if interval == "year" and not plan.get("price_year"):
        return jsonify({"ok": False, "error": "That plan is monthly only."}), 400
    if tier == "founding" and get_store().founding_taken() >= founding_cap():
        return jsonify({"ok": False, "error": "Founding member spots are full"}), 409
    existing = get_store().get_by_email(email)
    if existing and existing.get("tier") in PAID_TIERS and existing.get("status") in _ACTIVE:
        return jsonify({
            "ok": False,
            "error": "That email already has an active subscription. Use Manage or cancel.",
        }), 409
    try:
        session = create_checkout_session(
            email=email,
            tier=tier,
            interval=interval,
            origin=request.headers.get("Origin"),
        )
        get_store().begin_checkout(
            email=email,
            tier=tier,
            interval=interval,
            session_id=session["id"],
            price_id=session["price_id"],
        )
    except AlreadySubscribedError:
        return jsonify({
            "ok": False,
            "error": "That email already has an active subscription. Use Manage or cancel.",
        }), 409
    except FoundingFullError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except StripeError as exc:
        status = exc.status_code or 502
        return jsonify({"ok": False, "error": str(exc)}), status
    return jsonify({"ok": True, "url": session["url"], "id": session["id"]})


@billing_bp.post("/api/billing/portal")
def portal():
    payload = request.get_json(silent=True) or {}
    email = _require_email(payload.get("email"))
    if email is None:
        return jsonify({"ok": False, "error": "Enter a valid email."}), 400
    row = get_store().get_by_email(email)
    customer = (row or {}).get("stripe_customer_id")
    if not customer:
        return jsonify({"ok": False, "error": "No Stripe customer is stored for that email yet."}), 404
    _success, cancel = checkout_return_urls(request.headers.get("Origin"))
    try:
        session = create_portal_session(customer_id=str(customer), return_url=cancel)
    except StripeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status_code or 502
    return jsonify({"ok": True, "url": session["url"]})


@billing_bp.get("/api/billing/access")
def access():
    session_id = str(request.args.get("session_id") or "").strip()
    if not session_id.startswith("cs_"):
        return jsonify({"ok": False, "error": "Missing checkout session."}), 400
    store = get_store()
    row = store.get_by_session(session_id)
    if row is None or row.get("status") not in _ACTIVE | {"past_due"}:
        try:
            session = retrieve_checkout_session(session_id)
        except StripeError as exc:
            if row is None:
                return jsonify({"ok": False, "error": str(exc)}), exc.status_code or 502
            session = None
        if isinstance(session, dict) and _session_is_paid(session):
            row = _apply_checkout_session(store, session)
    if row is None:
        return jsonify({"ok": True, "status": "pending", "invite_url": None, "disclaimer": DISCLAIMER})
    if row.get("status") in _ACTIVE and row.get("tier") in PAID_TIERS and not row.get("telegram_invite_link"):
        row = _issue_invite(store, row) or row
    invite = row.get("telegram_invite_link") if row.get("status") in _ACTIVE else None
    return jsonify({
        "ok": True,
        "status": row.get("status"),
        "tier": row.get("tier"),
        "invite_url": invite,
        "disclaimer": DISCLAIMER,
    })


@billing_bp.post("/api/billing/webhook")
def stripe_webhook():
    secret = stripe_webhook_secret()
    if not secret:
        return jsonify({"ok": False, "error": "STRIPE_WEBHOOK_SECRET is not set"}), 503
    payload = request.get_data() or b""
    if not verify_stripe_signature(payload, request.headers.get("Stripe-Signature"), secret):
        return jsonify({"ok": False, "error": "Invalid Stripe signature"}), 400
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify({"ok": False, "error": "Expected a JSON event"}), 400
    event_id = str(event.get("id") or "")
    store = get_store()
    if event_id and not store.claim_event(event_id):
        return jsonify({"ok": True, "duplicate": True})
    try:
        result = _handle_stripe_event(store, event)
    except Exception as exc:  # noqa: BLE001
        logger.exception("stripe webhook failed: %s", exc)
        if event_id:
            store.release_event(event_id)
        return jsonify({"ok": False, "error": "Webhook handler failed"}), 500
    return jsonify({"ok": True, **result})


@billing_bp.post("/api/telegram/webhook")
def telegram_webhook():
    expected = telegram_webhook_secret()
    if expected:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if sent != expected:
            return jsonify({"ok": False, "error": "Invalid Telegram secret"}), 401
    update = request.get_json(silent=True) or {}
    if not isinstance(update, dict):
        return jsonify({"ok": True, "ignored": True})
    result = handle_telegram_update(get_store(), update)
    return jsonify({"ok": True, **result})


@billing_bp.post("/api/billing/digest/publish")
def publish_digest():
    import os

    expected = (os.environ.get("BILLING_ADMIN_TOKEN") or "").strip()
    if not expected:
        return jsonify({"ok": False, "error": "BILLING_ADMIN_TOKEN is not set"}), 503
    sent = request.headers.get("X-Admin-Token") or ""
    auth = request.headers.get("Authorization") or ""
    if sent != expected and auth != f"Bearer {expected}":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    from odte_scanner.alerts.subscription import publish_morning_digest

    payload = request.get_json(silent=True) or {}
    result = publish_morning_digest(force=bool(payload.get("force")))
    status = 200 if result.get("ok") or result.get("skipped") else 502
    return jsonify(result), status


def handle_telegram_update(store: SubscriberStore, update: dict[str, Any], *, http_post=None) -> dict[str, Any]:
    join = update.get("chat_join_request")
    if not isinstance(join, dict):
        return {"ignored": True}
    user = join.get("from") or {}
    user_id = user.get("id")
    link = ((join.get("invite_link") or {}) if isinstance(join.get("invite_link"), dict) else {}).get("invite_link")
    if not user_id or not link:
        return {"ignored": True}
    row = store.get_by_invite(str(link))
    if row is None or row.get("status") not in _ACTIVE:
        decline_join(user_id, http_post=http_post)
        return {"approved": False, "reason": "no active subscription for this invite"}
    existing = row.get("telegram_user_id")
    if existing and int(existing) != int(user_id):
        decline_join(user_id, http_post=http_post)
        return {"approved": False, "reason": "invite already used"}
    approved = approve_join(user_id, http_post=http_post)
    if not approved.get("ok"):
        return {"approved": False, "error": approved.get("error")}
    store.set_telegram_user(str(link), int(user_id))
    return {"approved": True}


def _handle_stripe_event(store: SubscriberStore, event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("type") or "")
    obj = ((event.get("data") or {}) if isinstance(event.get("data"), dict) else {}).get("object") or {}
    if not isinstance(obj, dict):
        obj = {}
    if kind == "checkout.session.completed":
        if not _session_is_paid(obj):
            return {"handled": kind, "ignored": True}
        row = _apply_checkout_session(store, obj)
        return {"handled": kind, "status": row.get("status"), "tier": row.get("tier")}
    if kind == "customer.subscription.deleted":
        row = store.mark_canceled(subscription_id=obj.get("id"), customer_id=_id(obj.get("customer")))
        if row:
            revoke_access(row)
        return {"handled": kind, "revoked": bool(row)}
    if kind == "customer.subscription.updated":
        status = str(obj.get("status") or "")
        if status in {"canceled", "unpaid", "incomplete_expired"}:
            row = store.mark_canceled(subscription_id=obj.get("id"), customer_id=_id(obj.get("customer")))
            if row:
                revoke_access(row)
            return {"handled": kind, "revoked": bool(row), "status": status}
        if status in _ACTIVE:
            meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            row = store.activate(
                email=None,
                tier=meta.get("tier"),
                interval=meta.get("interval"),
                customer_id=_id(obj.get("customer")),
                subscription_id=obj.get("id"),
                session_id=None,
                status="trialing" if status == "trialing" else "active",
            )
            if row.get("tier") in PAID_TIERS and not row.get("telegram_invite_link"):
                _issue_invite(store, row)
            return {"handled": kind, "status": status}
        if status == "past_due":
            row = store.mark_payment_failed(subscription_id=obj.get("id"), customer_id=_id(obj.get("customer")))
            return {"handled": kind, "status": "past_due", "found": bool(row)}
        return {"handled": kind, "status": status, "ignored": True}
    if kind == "invoice.payment_failed":
        sub_id = _invoice_subscription_id(obj)
        row = store.mark_payment_failed(
            subscription_id=sub_id,
            customer_id=_id(obj.get("customer")),
            invoice_id=obj.get("id"),
        )
        return {"handled": kind, "found": bool(row)}
    return {"handled": kind, "ignored": True}


def _apply_checkout_session(store: SubscriberStore, session: dict[str, Any]) -> dict[str, Any]:
    meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    email = session.get("customer_email") or ((session.get("customer_details") or {}) if isinstance(session.get("customer_details"), dict) else {}).get("email")
    status = "trialing" if str(session.get("payment_status") or "") == "no_payment_required" else "active"
    row = store.activate(
        email=str(email or ""),
        tier=str(meta.get("tier") or session.get("client_reference_id") or "") or None,
        interval=str(meta.get("interval") or "") or None,
        customer_id=_id(session.get("customer")),
        subscription_id=_id(session.get("subscription")),
        session_id=session.get("id"),
        status=status,
    )
    if row.get("tier") in PAID_TIERS and row.get("status") in _ACTIVE and not row.get("telegram_invite_link"):
        issued = _issue_invite(store, row)
        if issued:
            return issued
    return row


def _issue_invite(store: SubscriberStore, row: dict[str, Any]) -> dict[str, Any] | None:
    name = f"{row.get('tier') or 'sub'}-{str(row.get('id') or '')[:8]}"
    created = create_invite(name)
    if created.get("invite_link"):
        return store.set_invite(str(row.get("id")), str(created["invite_link"]))
    return store.set_invite(str(row.get("id")), None, error=str(created.get("error") or "invite failed"))


def _session_is_paid(session: dict[str, Any]) -> bool:
    if str(session.get("mode") or "subscription") not in {"subscription", ""}:
        if session.get("mode") not in {None, "subscription"}:
            return False
    if str(session.get("status") or "complete") not in {"complete", ""}:
        return False
    payment = str(session.get("payment_status") or "")
    if payment in {"paid", "no_payment_required", ""}:
        return bool(session.get("subscription") or session.get("id"))
    return False


def _invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
    direct = _id(invoice.get("subscription"))
    if direct:
        return direct
    parent = invoice.get("parent") if isinstance(invoice.get("parent"), dict) else {}
    details = parent.get("subscription_details") if isinstance(parent.get("subscription_details"), dict) else {}
    return _id(details.get("subscription"))


def _id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        inner = value.get("id")
        return str(inner) if inner else None
    text = str(value).strip()
    return text or None


def _require_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if not email or len(email) > 200 or not _EMAIL.match(email):
        return None
    return email


def _origin_allowed(origin: str) -> bool:
    allowed = {
        public_site_url(),
        public_api_base(),
        "https://2100preet.github.io",
        "https://stockscanner.fly.dev",
    }
    import os

    extra = (os.environ.get("BILLING_CORS_ORIGINS") or "").split(",")
    allowed.update(item.strip().rstrip("/") for item in extra if item.strip())
    if origin in allowed:
        return True
    host = origin.split("://", 1)[-1].split("/", 1)[0]
    if host.endswith(".github.io") and origin.startswith("https://"):
        return True
    hostname = host.split(":", 1)[0]
    return hostname in {"localhost", "127.0.0.1"}


def _html(name: str) -> Response:
    page = render_site("")[name]
    return Response(page, mimetype="text/html")
