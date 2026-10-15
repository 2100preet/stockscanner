"""HTML for the GitHub Pages landing page and the Fly /subscribe routes."""

from __future__ import annotations

import json

from odte_scanner.billing.copy import DISCLAIMER, PRODUCT_NAME
from odte_scanner.billing.plans import PLANS

_CSS = """
:root {
  --bg: #0c1210; --ink: #eef4f0; --muted: #8b9e94; --line: rgba(238,244,240,.12);
  --long: #3ecf8e; --gold: #f0c14b; --panel: rgba(18,28,24,.78);
}
* { box-sizing: border-box; }
body {
  margin: 0; color: var(--ink);
  font-family: "DM Sans", sans-serif;
  background:
    radial-gradient(900px 420px at 8% -10%, rgba(62,207,142,.18), transparent 55%),
    radial-gradient(700px 380px at 100% 0%, rgba(240,193,75,.12), transparent 46%),
    linear-gradient(165deg, #08100e, var(--bg) 42%, #101814);
}
a { color: var(--long); }
.wrap { width: min(1080px, calc(100% - 1.5rem)); margin: 0 auto; padding: 1.4rem 0 3rem; }
.nav { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-bottom: 1.4rem; }
.brand { font-family: "Instrument Serif", Georgia, serif; font-size: 1.7rem; letter-spacing: -.03em; color: inherit; text-decoration: none; }
.brand em { font-style: italic; color: var(--long); }
.desk-link { color: var(--muted); font-size: .92rem; }
h1 { font-family: "Instrument Serif", Georgia, serif; font-weight: 400; font-size: clamp(2.4rem, 6vw, 4.2rem); line-height: .95; letter-spacing: -.03em; margin: 0 0 .6rem; }
h1 em { font-style: italic; color: var(--gold); }
.lede { color: var(--muted); font-size: 1.08rem; max-width: 40rem; }
.banner {
  border: 1px solid rgba(240,193,75,.45); background: rgba(240,193,75,.08);
  border-radius: .9rem; padding: .85rem 1rem; margin: 1.1rem 0 1.4rem; color: #f3e6c0;
}
.grid { display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 1rem; padding: 1rem 1rem 1.1rem;
  display: flex; flex-direction: column; gap: .45rem;
}
.card.featured { border-color: rgba(62,207,142,.55); }
.card h2 { margin: 0; font-size: 1.15rem; }
.price { font-family: "Instrument Serif", Georgia, serif; font-size: 2.2rem; margin: 0; }
.price span { font-family: "DM Sans", sans-serif; font-size: .95rem; color: var(--muted); }
.card ul { margin: .2rem 0 .4rem; padding-left: 1.1rem; color: var(--muted); }
.card li { margin: .28rem 0; }
label { display: block; font-size: .82rem; color: var(--muted); margin-top: .6rem; }
input[type="email"], input[type="text"] {
  width: 100%; margin-top: .25rem; padding: .6rem .7rem; border-radius: .6rem;
  border: 1px solid var(--line); background: rgba(0,0,0,.25); color: var(--ink); font: inherit;
}
.hp { position: absolute; left: -9999px; height: 0; overflow: hidden; }
button, .btn {
  border: 0; border-radius: .65rem; padding: .62rem .9rem; font: 600 .92rem/1 "DM Sans", sans-serif;
  background: var(--long); color: #062016; cursor: pointer; text-decoration: none; display: inline-block;
}
button.secondary, .btn.secondary { background: transparent; color: var(--ink); border: 1px solid var(--line); }
button:disabled { opacity: .5; cursor: not-allowed; }
.actions { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: auto; }
.check { display: flex; gap: .5rem; align-items: flex-start; color: var(--muted); font-size: .88rem; margin: .8rem 0; }
.check input { margin-top: .2rem; }
.note { color: var(--muted); font-size: .88rem; }
.footer { margin-top: 2rem; color: var(--muted); font-size: .85rem; display: flex; flex-wrap: wrap; gap: .8rem; }
.msg { min-height: 1.2rem; font-size: .9rem; }
.legal { max-width: 44rem; }
.legal h1 { font-size: clamp(2rem, 4vw, 3rem); }
.legal h2 { font-size: 1.15rem; margin-top: 1.4rem; }
.legal p { color: var(--muted); }
"""


def render_site(api_base: str) -> dict[str, str]:
    return {
        "index.html": landing_page(api_base),
        "success.html": success_page(api_base),
        "terms.html": legal_page("Terms of use", _TERMS),
        "privacy.html": legal_page("Privacy", _PRIVACY),
        "refund.html": legal_page("Refunds and cancellation", _REFUNDS),
    }


def landing_page(api_base: str) -> str:
    cards = "\n".join(_card(plan) for plan in PLANS)
    body = f"""
    <p class="lede">Unusual flow and gold setups, filtered. A few alerts a day, pushed to Telegram. $29 for Pro. The scanner desk stays public.</p>
    <div class="banner">{DISCLAIMER} Signals are impersonal: every subscriber receives the same note. This service does not trade for you, copy trades, or ask for brokerage credentials. Hypothetical or paper results are not shown here.</div>
    <section class="card" style="margin-bottom:1rem">
      <h2>Free Morning Spark</h2>
      <p class="note">Weekday digest on the public Telegram channel, including a delayed Gold Setup recap. No card. A newsletter vendor is not connected yet — the email is stored on the scanner host.</p>
      <form id="digestForm">
        <label>Email <input type="email" name="email" required autocomplete="email" placeholder="you@example.com"></label>
        <label class="hp">Company <input type="text" name="company" tabindex="-1" autocomplete="off"></label>
        <div class="actions" style="margin-top:.7rem"><button type="submit">Get the free digest</button></div>
      </form>
      <p id="digestMsg" class="msg" role="status"></p>
    </section>
    <label>Email for a paid plan <input id="paidEmail" type="email" autocomplete="email" placeholder="you@example.com"></label>
    <label class="check"><input id="ack" type="checkbox"> I understand this is not investment advice, that trading can lose money, and that a paid plan renews until I cancel in the Stripe customer portal.</label>
    <div class="grid">{cards}</div>
    <p id="payMsg" class="msg" role="status"></p>
    <p id="stripeNote" class="note" hidden>Checkout appears when the API and Stripe prices are configured.</p>
    <p class="note" id="foundingLeft"></p>
    <div class="actions">
      <button type="button" class="secondary" id="portalBtn">Manage or cancel a subscription</button>
    </div>
    """
    return _shell(
        f"{PRODUCT_NAME} — alerts",
        body,
        api_base,
        extra_script=_LANDING_JS,
    )


def success_page(api_base: str) -> str:
    body = f"""
    <h1>You're in.</h1>
    <p class="lede">If the payment went through, a one-time Telegram join link shows up here. Open it and request to join. The bot approves the request for this subscription.</p>
    <div class="banner">{DISCLAIMER}</div>
    <p id="accessMsg" class="msg">Checking the checkout…</p>
    <p id="inviteWrap" hidden><a class="btn" id="inviteLink" href="">Open the Telegram invite</a></p>
    <p class="note">Cancel anytime from the Stripe customer portal. Canceling revokes the invite and removes the Telegram user who joined with it.</p>
    """
    return _shell("Subscription", body, api_base, extra_script=_SUCCESS_JS)


def legal_page(title: str, paragraphs: list[tuple[str, str]]) -> str:
    blocks = [f"<h1>{title}</h1>", "<p>Draft for a securities lawyer to review. This is not legal advice.</p>"]
    for heading, text in paragraphs:
        blocks.append(f"<h2>{heading}</h2><p>{text}</p>")
    blocks.append(f"<div class='banner'>{DISCLAIMER}</div>")
    return _shell(title, "<div class='legal'>" + "".join(blocks) + "</div>", "")


def _card(plan: dict) -> str:
    featured = " featured" if plan["id"] == "pro" else ""
    if plan["id"] == "free":
        price = "<p class='price'>$0 <span>/ digest</span></p>"
        actions = "<p class='note'>Use the email form above. No checkout.</p>"
    else:
        month = int(plan["price_month"])
        price = f"<p class='price'>${month} <span>/mo</span></p>"
        if plan.get("price_year"):
            price += f"<p class='note'>or ${int(plan['price_year'])}/yr</p>"
        buttons = [f"<button type='button' data-tier='{plan['id']}' data-interval='month'>${month}/mo</button>"]
        if plan.get("price_year"):
            buttons.append(
                f"<button type='button' class='secondary' data-tier='{plan['id']}' data-interval='year'>${int(plan['price_year'])}/yr</button>"
            )
        actions = "<div class='actions'>" + "".join(buttons) + "</div>"
    items = "".join(f"<li>{feature}</li>" for feature in plan["features"])
    return (
        f"<article class='card{featured}'><h2>{plan['name']}</h2>{price}"
        f"<p class='note'>{plan['blurb']}</p><ul>{items}</ul>{actions}</article>"
    )


def _shell(title: str, body: str, api_base: str, extra_script: str = "") -> str:
    api_literal = json.dumps(api_base)
    script = f"<script>window.SPARKY_API_BASE = {api_literal};</script>"
    if extra_script:
        script += f"<script>{extra_script}</script>"
    first, last = PRODUCT_NAME.split()
    # CSS braces must not go through an f-string.
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{PRODUCT_NAME}: filtered Gold Setup and Golden Flow alerts. Not investment advice.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
  <style>__CSS__</style>
</head>
<body>
  <div class="wrap">
    <div class="nav">
      <a class="brand" href="./">{first} <em>{last}</em></a>
      <a class="desk-link" href="../">Scanner desk</a>
    </div>
    {body}
    <footer class="footer">
      <span>{DISCLAIMER}</span>
      <a href="terms.html">Terms</a>
      <a href="privacy.html">Privacy</a>
      <a href="refund.html">Refunds</a>
    </footer>
  </div>
  {script}
</body>
</html>
""".replace("__CSS__", _CSS)


_LANDING_JS = r"""
function api(path) {
  var base = (window.SPARKY_API_BASE || "").replace(/\/$/, "");
  return base + path;
}
function msg(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text || "";
}
async function refreshPlans() {
  try {
    var res = await fetch(api("/api/billing/plans"));
    if (!res.ok) throw new Error("plans");
    var body = await res.json();
    var left = body.founding_remaining;
    var el = document.getElementById("foundingLeft");
    if (el && left != null) {
      el.textContent = left > 0
        ? (left + " of " + body.founding_cap + " founding spots left")
        : "Founding member spots are full";
    }
    document.querySelectorAll("[data-tier='founding']").forEach(function (btn) {
      btn.disabled = left === 0;
    });
    var note = document.getElementById("stripeNote");
    if (note) note.hidden = !!body.stripe_configured;
  } catch (err) {
    var noteEl = document.getElementById("stripeNote");
    if (noteEl) {
      noteEl.hidden = false;
      noteEl.textContent = "Pricing is shown here. Checkout connects when the API is reachable.";
    }
  }
}
async function startCheckout(tier, interval) {
  var email = (document.getElementById("paidEmail").value || "").trim();
  var ack = document.getElementById("ack");
  if (!ack.checked) {
    msg("payMsg", "Check the box under the email field before checkout.");
    return;
  }
  if (!email) {
    msg("payMsg", "Add an email for the receipt and the subscriber record.");
    return;
  }
  msg("payMsg", "Opening Stripe…");
  var res = await fetch(api("/api/billing/checkout"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tier: tier, interval: interval, email: email, company: "" })
  });
  var body = {};
  try { body = await res.json(); } catch (err) { body = {}; }
  if (body.url) {
    window.location.href = body.url;
    return;
  }
  msg("payMsg", body.error || "Checkout did not start.");
}
document.getElementById("digestForm").addEventListener("submit", async function (ev) {
  ev.preventDefault();
  var data = new FormData(ev.target);
  var res = await fetch(api("/api/billing/digest"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: data.get("email"), company: data.get("company") || "" })
  });
  var body = {};
  try { body = await res.json(); } catch (err) { body = {}; }
  msg("digestMsg", body.ok ? "Saved. Follow the public Telegram channel for the Morning Spark digest." : (body.error || "Could not save that email."));
});
document.querySelectorAll("[data-tier]").forEach(function (btn) {
  btn.addEventListener("click", function () {
    startCheckout(btn.getAttribute("data-tier"), btn.getAttribute("data-interval"));
  });
});
document.getElementById("portalBtn").addEventListener("click", async function () {
  var email = (document.getElementById("paidEmail").value || "").trim();
  if (!email) {
    msg("payMsg", "Enter the email on the subscription.");
    return;
  }
  var res = await fetch(api("/api/billing/portal"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: email })
  });
  var body = {};
  try { body = await res.json(); } catch (err) { body = {}; }
  if (body.url) window.location.href = body.url;
  else msg("payMsg", body.error || "No billing portal for that email yet.");
});
refreshPlans();
"""


_SUCCESS_JS = r"""
function api(path) {
  var base = (window.SPARKY_API_BASE || "").replace(/\/$/, "");
  return base + path;
}
async function poll() {
  var params = new URLSearchParams(window.location.search);
  var sessionId = params.get("session_id") || "";
  var note = document.getElementById("accessMsg");
  if (!sessionId) {
    note.textContent = "Missing checkout session. Return to pricing and try again.";
    return;
  }
  for (var i = 0; i < 8; i++) {
    var res = await fetch(api("/api/billing/access?session_id=" + encodeURIComponent(sessionId)));
    var body = {};
    try { body = await res.json(); } catch (err) { body = {}; }
    if (body.invite_url) {
      note.textContent = "Join request link is ready. Telegram will ask the bot to approve you.";
      var wrap = document.getElementById("inviteWrap");
      var link = document.getElementById("inviteLink");
      link.href = body.invite_url;
      wrap.hidden = false;
      return;
    }
    if (body.error && res.status >= 400) {
      note.textContent = body.error;
      return;
    }
    note.textContent = "Waiting for Stripe to confirm the subscription…";
    await new Promise(function (resolve) { setTimeout(resolve, 2000); });
  }
  note.textContent = "Stripe has not confirmed this checkout yet. Refresh in a minute, or check the receipt email.";
}
poll();
"""


_TERMS = [
    (
        "What this is",
        f"{PRODUCT_NAME} publishes impersonal market notes: a free Morning Spark digest and, for paid plans, the same Gold Setup and Golden Flow alerts to every subscriber. It is not investment advice, not a personal recommendation, and not a brokerage.",
    ),
    (
        "No trading for you",
        "The service does not place trades, copy trades, manage money, or accept brokerage logins or account credentials. The operator's paper-trading account is internal and is not part of the subscription.",
    ),
    (
        "Subscriptions",
        "Pro is $29 per month or $249 per year, with a 7-day trial when Stripe is configured that way. Pro+ is $59 per month or $499 per year. Founding Member is $19 per month for the first 100 people, and that Stripe price is the price those subscribers keep. Paid plans renew until you cancel. Pro+ extras beyond the shared Telegram alerts are not included yet.",
    ),
    (
        "Risk",
        f"{DISCLAIMER} You can lose some or all of the money you use to trade.",
    ),
    (
        "Operator",
        "Operated from Chicago, Illinois. These terms are a draft for counsel review.",
    ),
]

_PRIVACY = [
    (
        "What is stored",
        "The digest form stores your email on the scanner host. Checkout stores your email, Stripe customer id, subscription id, and the Telegram invite issued for that subscription. If you request to join the paid channel, the bot stores the Telegram user id so access can be removed on cancel.",
    ),
    (
        "Who else processes it",
        "Stripe processes the payment. Telegram delivers the channel posts and join requests. GitHub Pages hosts this public page and does not receive the subscriber file.",
    ),
    (
        "What this draft does not do",
        "The list is not sold. A separate newsletter vendor is not connected yet. This page is a draft privacy note for counsel to replace.",
    ),
]

_REFUNDS = [
    (
        "Cancel",
        "Use Manage or cancel on the pricing page, or the link in the Stripe receipt. Cancellation takes effect as Stripe's customer portal records it. The paid Telegram invite is revoked and a joined user is removed from the channel.",
    ),
    (
        "Charges",
        "A partial billing period is not refunded except where the law requires it. The Pro trial, when Stripe applies it, can be canceled before it converts.",
    ),
]
