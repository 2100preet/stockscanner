"""Stripe checkout, webhooks, and the public subscribe page. No live network."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from odte_scanner.billing.pages import landing_page, render_site
from odte_scanner.billing.plans import public_catalog
from odte_scanner.billing.routes import billing_bp
from odte_scanner.billing.store import SubscriberStore
from odte_scanner.billing.stripe_client import create_checkout_session, verify_stripe_signature


def _signature(payload: bytes, secret: str, stamp: int = 1_700_000_000) -> str:
    signed = f"{stamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_example")
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_m")
    monkeypatch.setenv("STRIPE_PRICE_PRO_YEARLY", "price_pro_y")
    monkeypatch.setenv("STRIPE_PRICE_PRO_PLUS_MONTHLY", "price_plus_m")
    monkeypatch.setenv("STRIPE_PRICE_PRO_PLUS_YEARLY", "price_plus_y")
    monkeypatch.setenv("STRIPE_PRICE_FOUNDING_MONTHLY", "price_founding_m")
    monkeypatch.setenv("FOUNDING_MEMBER_CAP", "2")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_PAID_CHANNEL_ID", "-100555")
    app = Flask(__name__)
    app.register_blueprint(billing_bp)
    return app.test_client()


def test_landing_lists_plans_and_disclaimer():
    html = landing_page("https://stockscanner.fly.dev")
    assert "Sure Winner" not in html
    assert "Gold Setup" in html
    assert "Not investment advice" in html
    assert "$29" in html and "$249" in html
    assert "$59" in html and "$499" in html
    assert "$19" in html
    assert "Morning Spark" in html
    assert "window.SPARKY_API_BASE = \"https://stockscanner.fly.dev\"" in html
    pages = render_site("https://stockscanner.fly.dev")
    assert set(pages) == {"index.html", "success.html", "terms.html", "privacy.html", "refund.html"}
    for page in pages.values():
        assert "Sure Winner" not in page
        assert "Not investment advice" in page


def test_catalog_hides_secret_values(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_secret")
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_m")
    catalog = public_catalog()
    blob = json.dumps(catalog)
    assert "sk_test_secret" not in blob
    assert "price_pro_m" not in blob
    assert catalog["stripe_configured"] is True
    pro = next(item for item in catalog["plans"] if item["id"] == "pro")
    assert pro["prices_configured"]["month"] is True
    assert pro["prices_configured"]["year"] is False


def test_signature_accepts_stripe_header_and_rejects_tampering():
    payload = b'{"id":"evt_1"}'
    header = _signature(payload, "whsec_example")
    assert verify_stripe_signature(payload, header, "whsec_example", now=1_700_000_000) is True
    assert verify_stripe_signature(payload + b" ", header, "whsec_example", now=1_700_000_000) is False
    assert verify_stripe_signature(payload, header, "whsec_example", now=1_700_000_000 + 10_000) is False


def test_checkout_session_uses_env_price(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example")
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_m")
    seen = {}

    class Fake:
        status_code = 200

        def json(self):
            return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["url"] = url
        seen["data"] = list(data or [])
        seen["auth"] = headers.get("Authorization")
        return Fake()

    out = create_checkout_session(email="a@example.com", tier="pro", interval="month", http_post=fake_post)
    assert out["url"].startswith("https://checkout.stripe.com/")
    assert seen["auth"] == "Bearer sk_test_example"
    assert ("line_items[0][price]", "price_pro_m") in seen["data"]
    assert ("subscription_data[trial_period_days]", "7") in seen["data"]
    assert "sk_test_example" not in str(seen["data"])


def test_checkout_route_and_founding_cap(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_session(**kwargs):
        calls["n"] += 1
        return {"id": f"cs_test_{calls['n']}", "url": "https://checkout.stripe.com/c/pay/cs", "price_id": "price_founding_m"}

    monkeypatch.setattr("odte_scanner.billing.routes.create_checkout_session", fake_session)
    for email in ("one@example.com", "two@example.com"):
        res = client.post("/api/billing/checkout", json={"email": email, "tier": "founding", "interval": "month"})
        assert res.status_code == 200
        assert res.get_json()["url"].startswith("https://checkout.stripe.com/")
    blocked = client.post(
        "/api/billing/checkout",
        json={"email": "three@example.com", "tier": "founding", "interval": "month"},
    )
    assert blocked.status_code == 409
    yearly = client.post(
        "/api/billing/checkout",
        json={"email": "four@example.com", "tier": "founding", "interval": "year"},
    )
    assert yearly.status_code == 400


def test_webhook_subscribe_cancel_and_payment_failed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    invites = []
    revoked = []

    def fake_invite(name, **kwargs):
        invites.append(name)
        return {"ok": True, "invite_link": "https://t.me/+paid"}

    def fake_revoke(subscriber, **kwargs):
        revoked.append(subscriber.get("email"))
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.billing.routes.create_invite", fake_invite)
    monkeypatch.setattr("odte_scanner.billing.routes.revoke_access", fake_revoke)

    def post_event(event):
        raw = json.dumps(event).encode()
        header = _signature(raw, "whsec_example", stamp=int(time.time()))
        return client.post(
            "/api/billing/webhook",
            data=raw,
            headers={"Stripe-Signature": header, "Content-Type": "application/json"},
        )

    created = post_event({
        "id": "evt_sub",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_9",
                "mode": "subscription",
                "status": "complete",
                "payment_status": "paid",
                "customer": "cus_9",
                "customer_email": "ada@example.com",
                "subscription": "sub_9",
                "metadata": {"tier": "pro", "interval": "month"},
            }
        },
    })
    assert created.status_code == 200
    assert len(invites) == 1 and invites[0].startswith("pro-")
    store = SubscriberStore(tmp_path / "subscribers.json")
    row = store.get_by_email("ada@example.com")
    assert row["status"] == "active"
    assert row["telegram_invite_link"] == "https://t.me/+paid"
    assert row["stripe_customer_id"] == "cus_9"

    duplicate = post_event({
        "id": "evt_sub",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test_9", "payment_status": "paid", "subscription": "sub_9"}},
    })
    assert duplicate.get_json()["duplicate"] is True
    assert len(invites) == 1

    failed = post_event({
        "id": "evt_fail",
        "type": "invoice.payment_failed",
        "data": {"object": {"id": "in_9", "customer": "cus_9", "subscription": "sub_9"}},
    })
    assert failed.status_code == 200
    assert store.get_by_email("ada@example.com")["status"] == "past_due"
    assert revoked == []

    # Restore active so cancel has a joined user to remove.
    store.activate(
        email="ada@example.com",
        tier="pro",
        interval="month",
        customer_id="cus_9",
        subscription_id="sub_9",
        session_id="cs_test_9",
        status="active",
    )
    store.set_telegram_user("https://t.me/+paid", 4242)
    canceled = post_event({
        "id": "evt_cancel",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_9", "customer": "cus_9", "status": "canceled"}},
    })
    assert canceled.status_code == 200
    assert revoked == ["ada@example.com"]
    assert store.get_by_email("ada@example.com")["status"] == "canceled"

    bad = client.post(
        "/api/billing/webhook",
        data=b"{}",
        headers={"Stripe-Signature": "t=1,v1=nope", "Content-Type": "application/json"},
    )
    assert bad.status_code == 400


def test_join_request_approves_one_user(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_PAID_CHANNEL_ID", "-100555")
    store = SubscriberStore(tmp_path / "subscribers.json")
    store.activate(
        email="ada@example.com",
        tier="pro",
        interval="month",
        customer_id="cus_1",
        subscription_id="sub_1",
        session_id="cs_1",
        status="active",
    )
    row = store.get_by_email("ada@example.com")
    store.set_invite(row["id"], "https://t.me/+once")
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], json))

        class Fake:
            status_code = 200

            def json(self):
                return {"ok": True, "result": True}

        return Fake()

    from odte_scanner.billing.routes import handle_telegram_update

    first = handle_telegram_update(
        store,
        {
            "chat_join_request": {
                "from": {"id": 7},
                "invite_link": {"invite_link": "https://t.me/+once"},
            }
        },
        http_post=fake_post,
    )
    assert first["approved"] is True
    assert store.get_by_email("ada@example.com")["telegram_user_id"] == 7
    second = handle_telegram_update(
        store,
        {
            "chat_join_request": {
                "from": {"id": 8},
                "invite_link": {"invite_link": "https://t.me/+once"},
            }
        },
        http_post=fake_post,
    )
    assert second["approved"] is False
    assert calls[0][0] == "approveChatJoinRequest"
    assert calls[1][0] == "declineChatJoinRequest"


def test_digest_signup_and_pages_route(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    saved = client.post("/api/billing/digest", json={"email": "reader@example.com"})
    assert saved.status_code == 200
    assert saved.get_json()["tier"] == "free"
    bot = client.post("/api/billing/digest", json={"email": "bot@example.com", "company": "acme"})
    assert bot.get_json()["stored"] is False
    page = client.get("/subscribe/")
    assert page.status_code == 200
    assert b"Gold Setup" in page.data
    assert b"Sure Winner" not in page.data
    plans = client.get("/api/billing/plans")
    assert plans.get_json()["founding_remaining"] == 2


def test_founding_pending_expires(tmp_path):
    store = SubscriberStore(tmp_path / "subscribers.json")
    old = datetime.now(timezone.utc) - timedelta(days=3)
    store.begin_checkout(
        email="old@example.com",
        tier="founding",
        interval="month",
        session_id="cs_old",
        price_id="price_founding_m",
        now=old,
    )
    assert store.founding_taken() == 0
    store.begin_checkout(
        email="new@example.com",
        tier="founding",
        interval="month",
        session_id="cs_new",
        price_id="price_founding_m",
    )
    assert store.founding_taken() == 1


def test_create_app_serves_subscribe():
    from odte_scanner.ui import create_app

    client = create_app().test_client()
    res = client.get("/subscribe/")
    assert res.status_code == 200
    assert b"Morning Spark" in res.data
    webull = client.get("/api/webull/status")
    assert webull.status_code == 200
