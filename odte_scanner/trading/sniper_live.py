"""Sniper LIVE mirror — real-money Webull orders, Sniper desk only.

Paper Webull stays hard-locked to the sandbox host and the existing
WEBULL_APP_* credentials. This module is a separate client, a separate
ledger, and a separate object. It never falls back to the paper keys.

Kill switch: SNIPER_LIVE_ENABLED is off when missing or false.
Live creds: WEBULL_LIVE_APP_KEY, WEBULL_LIVE_APP_SECRET, WEBULL_LIVE_ACCOUNT_ID.
Daily realized-loss cap: SNIPER_LIVE_MAX_DAILY_LOSS (default -$200).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.trading.labels import activity_premium_label
from odte_scanner.trading.webull import (
    PRODUCTION_TRADE_HOST,
    SANDBOX_TRADE_HOST,
    EndpointBudget,
    WebullBroker,
    WebullOrderIntent,
    WebullRateLimit,
    webull_trade_host,
    _now,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "outputs" / "sniper_live_orders.json"
DEFAULT_STATE = ROOT / "outputs" / "sniper_live_day.json"

ENABLED_ENV = "SNIPER_LIVE_ENABLED"
LOSS_CAP_ENV = "SNIPER_LIVE_MAX_DAILY_LOSS"
KEY_ENV = "WEBULL_LIVE_APP_KEY"
SECRET_ENV = "WEBULL_LIVE_APP_SECRET"
ACCOUNT_ENV = "WEBULL_LIVE_ACCOUNT_ID"
LIVE_CREDS = (KEY_ENV, SECRET_ENV, ACCOUNT_ENV)
TOKEN_DIR_ENV = "WEBULL_LIVE_TOKEN_DIR"
DEFAULT_TOKEN_DIR = ROOT / "outputs" / ".webull_live_token"
TOKEN_RETRY_SEC = 900.0
_TOKEN_FAIL_AT: float | None = None

LIVE_QTY = 1
DAILY_CAP = 3
DEFAULT_MAX_DAILY_LOSS = -200.0
CT = ZoneInfo("America/Chicago")
ENTRY_OPEN = dt_time(8, 45)
ENTRY_CLOSE = dt_time(10, 30)

# Live calls use their own budget so they cannot consume the paper sandbox quota.
LIVE_BUDGET = EndpointBudget(30)
_LOCK = threading.Lock()
_DAY_LOCK = threading.Lock()
_broker: "SniperLiveBroker | None" = None
_trader: Any = None
_LIVE_SYNC_LOCK = threading.Lock()

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, environ: Mapping[str, str] | None = None, default: bool = False) -> bool:
    raw = str((environ or os.environ).get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_text(name: str, environ: Mapping[str, str] | None = None) -> str:
    return str((environ or os.environ).get(name) or "").strip()


def chicago_now(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def chicago_day(now: datetime | None = None) -> str:
    return chicago_now(now).date().isoformat()


def in_sniper_entry_window(now: datetime | None = None) -> bool:
    """Live entries are allowed only 8:45–10:30 CT on a weekday."""
    stamp = chicago_now(now)
    if stamp.weekday() >= 5:
        return False
    return ENTRY_OPEN <= stamp.time() < ENTRY_CLOSE


def max_daily_loss(environ: Mapping[str, str] | None = None) -> float:
    raw = str((environ or os.environ).get(LOSS_CAP_ENV) or DEFAULT_MAX_DAILY_LOSS)
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        value = DEFAULT_MAX_DAILY_LOSS
    if value > 0:
        value = -value
    return float(value)


def live_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return _env_flag(ENABLED_ENV, environ, default=False)


def creds_present(environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    env = environ or os.environ
    return {
        KEY_ENV: bool(_env_text(KEY_ENV, env)),
        SECRET_ENV: bool(_env_text(SECRET_ENV, env)),
        ACCOUNT_ENV: bool(_env_text(ACCOUNT_ENV, env)),
    }


def missing_cred_names(environ: Mapping[str, str] | None = None) -> list[str]:
    present = creds_present(environ)
    return [name for name in LIVE_CREDS if not present[name]]


def resolve_sniper_live_flags(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Public status. Values of secrets are never included."""
    env = environ or os.environ
    enabled = live_enabled(env)
    missing = missing_cred_names(env)
    keys_ok = not missing
    return {
        "enabled": enabled,
        "keys_ok": keys_ok,
        "account_ok": ACCOUNT_ENV not in missing,
        "ready": bool(enabled and keys_ok),
        "host": PRODUCTION_TRADE_HOST,
        "qty": LIVE_QTY,
        "daily_cap": DAILY_CAP,
        "max_daily_loss": max_daily_loss(env),
        "missing": missing,
        "flag_env": ENABLED_ENV,
        "cred_envs": list(LIVE_CREDS),
        "loss_cap_env": LOSS_CAP_ENV,
        "webull_live_env_ignored": _env_flag("WEBULL_LIVE", env, default=False),
    }


def is_live_broker(broker: Any) -> bool:
    return isinstance(broker, SniperLiveBroker)


def _is_live_order(order: Any) -> bool:
    meta = getattr(order, "meta", None)
    if not isinstance(meta, dict):
        if isinstance(order, dict):
            meta = order.get("meta") if isinstance(order.get("meta"), dict) else order
        else:
            meta = {}
    host = str(meta.get("host") or "")
    desk = str(getattr(order, "desk", None) or (order.get("desk") if isinstance(order, dict) else "") or "")
    return bool(meta.get("live") or meta.get("venue") == "webull_live" or host == PRODUCTION_TRADE_HOST or desk == "sniper_live")


def _is_sniper_buy(order: Any) -> bool:
    action = str(getattr(order, "action", None) or (order.get("action") if isinstance(order, dict) else "") or "").upper()
    desk = str(getattr(order, "desk", None) or (order.get("desk") if isinstance(order, dict) else "") or "").lower()
    meta = getattr(order, "meta", None)
    if not isinstance(meta, dict):
        meta = (order.get("meta") if isinstance(order, dict) and isinstance(order.get("meta"), dict) else {}) or {}
    kind = str(meta.get("golden_kind") or "")
    return action == "BUY" and (desk == "sniper" or kind == "sniper")


def _symbol(order: Any) -> str:
    return str(getattr(order, "symbol", None) or (order.get("symbol") if isinstance(order, dict) else "") or "").upper()


def _load_json(path: Path) -> dict[str, Any]:
    from odte_scanner.json_util import load_json_state

    raw = load_json_state(path)
    return raw if isinstance(raw, dict) else {}


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    from odte_scanner.json_util import atomic_write_text
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, default=str))


def load_day_state(path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_STATE
    day = chicago_day(now)
    raw = _load_json(target)
    if str(raw.get("day") or "") != day:
        raw = {
            "day": day,
            "entries": [],
            "realized_pnl": 0.0,
            "loss_cap_hit": False,
            "loss_cap_alerted": False,
        }
    raw.setdefault("entries", [])
    raw.setdefault("realized_pnl", 0.0)
    raw.setdefault("loss_cap_hit", False)
    raw.setdefault("loss_cap_alerted", False)
    raw["day"] = day
    return raw


def save_day_state(state: dict[str, Any], path: Path | None = None) -> None:
    _save_json(Path(path) if path else DEFAULT_STATE, state)


class SniperLiveBroker(WebullBroker):
    """Production-host Webull client used only by the Sniper LIVE mirror.

    Does not call WebullBroker.__init__, so it never inherits the paper
    sandbox lock, the paper ledger, or WEBULL_APP_* environment fallbacks.
    """

    def __init__(
        self,
        *,
        app_key: str | None,
        app_secret: str | None,
        account_id: str | None,
        ledger_path: str | Path | None = None,
        enabled: bool = True,
        dry_run: bool = False,
        region: str = "us",
        submit_interval_sec: float = 1.5,
        rate_limit_attempts: int = 3,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        key = str(app_key or "").strip()
        secret = str(app_secret or "").strip()
        account = str(account_id or "").strip()
        ready = bool(enabled and key and secret and account)
        self.enabled = ready
        self.dry_run = (not ready) or bool(dry_run)
        self.region = region or "us"
        self.sandbox = False
        self.paper = False
        self.account_id = account or None
        self.account_id_2 = None
        self.app_key = key or None
        self.app_secret = secret or None
        self.path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: list[WebullOrderIntent] = []
        self._baseline: dict[str, str] = {}
        self._disk_sig: tuple[int, int] | None = None
        self._load()
        self._client = None
        self._trade = None
        self._order_api = None
        self.submit_interval_sec = max(0.0, float(submit_interval_sec))
        self.rate_limit_attempts = max(1, int(rate_limit_attempts))
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._last_api_at: float | None = None
        self._live_allowed = True

    def _api_host(self) -> str:
        return PRODUCTION_TRADE_HOST

    def _token_dir(self) -> str | None:
        # The live app has 2FA on, so the SDK keeps a verified access token on
        # disk. Keep it on the persistent volume (not /app/conf, which a deploy
        # wipes, forcing a new phone approval) and apart from the paper token.
        override = _env_text(TOKEN_DIR_ENV)
        return override or str(DEFAULT_TOKEN_DIR)

    def _build_trade_api(self) -> Any:
        # A failed 2FA token check blocks up to 5 minutes and sends a new
        # approval request to the phone. Do not retry it every loop.
        global _TOKEN_FAIL_AT
        if _TOKEN_FAIL_AT is not None and self._clock() - _TOKEN_FAIL_AT < TOKEN_RETRY_SEC:
            raise RuntimeError("Sniper LIVE Webull token not verified; waiting before retry")
        try:
            api = super()._build_trade_api()
        except Exception:
            _TOKEN_FAIL_AT = self._clock()
            raise
        _TOKEN_FAIL_AT = None
        return api

    def _assert_sandbox(self) -> None:
        # Same hook the parent calls before every HTTP call. For this object
        # the rule is the inverse: refuse the paper sandbox.
        self._assert_live_host()

    def _assert_live_host(self) -> None:
        if self.sandbox or self.paper:
            raise RuntimeError(
                "Sniper LIVE broker refused: this object must not run in paper/sandbox mode."
            )
        if not self._live_allowed:
            raise RuntimeError("Sniper LIVE broker refused: live placement is not allowed on this object.")
        host = self._api_host()
        if host != PRODUCTION_TRADE_HOST or host == SANDBOX_TRADE_HOST:
            raise RuntimeError(f"Sniper LIVE broker refused unexpected host {host}")
        if webull_trade_host(sandbox=False) != PRODUCTION_TRADE_HOST:
            raise RuntimeError("Sniper LIVE broker refused: production host helper was remapped.")

    def _call_with_backoff(self, fn: Callable[[], Any], endpoint: str = "other") -> Any:
        last: BaseException | None = None
        attempts = self.rate_limit_attempts
        from odte_scanner.trading.webull import ORDER_ENDPOINTS, ORDER_MAX_WAIT_SEC

        max_wait = ORDER_MAX_WAIT_SEC if endpoint in ORDER_ENDPOINTS else 0.0
        for attempt in range(attempts):
            LIVE_BUDGET.reserve(endpoint, max_wait=max_wait, sleeper=self._sleeper)
            self._throttle()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                from odte_scanner.trading.webull import _is_rate_limit_error

                if not _is_rate_limit_error(exc):
                    raise
                last = exc
                if attempt + 1 >= attempts:
                    break
                delay = max(1.5, float(self.submit_interval_sec or 0)) * (2 ** attempt)
                self._sleeper(delay)
        raise WebullRateLimit(str(last)[:500] if last else "TOO_MANY_REQUESTS")

    def stage(self, **kwargs: Any) -> WebullOrderIntent:
        requested = str(kwargs.get("desk") or "").lower()
        meta = kwargs.get("meta") if isinstance(kwargs.get("meta"), dict) else {}
        kind = str((meta or {}).get("golden_kind") or "")
        foreign = requested not in {"", "sniper", "sniper_live"} and kind != "sniper"
        kwargs["desk"] = "sniper"
        kwargs["quantity"] = LIVE_QTY
        intent = super().stage(**kwargs)
        intent.quantity = LIVE_QTY
        intent.order_type = "LIMIT"
        intent.meta["live"] = True
        intent.meta["venue"] = "webull_live"
        intent.meta["paper"] = False
        intent.meta["sandbox"] = False
        intent.meta["host"] = PRODUCTION_TRADE_HOST
        intent.meta["qty_cap"] = LIVE_QTY
        if foreign:
            intent.desk = requested
            intent.status = "skipped"
            intent.error = "Sniper LIVE only accepts the Sniper desk"
            self.save()
            return intent
        intent.desk = "sniper"
        if _symbol(intent) == "SPX":
            intent.status = "skipped"
            intent.error = "SPX is never a Webull LIVE order"
            self.save()
        return intent

    def submit(self, intent: WebullOrderIntent) -> WebullOrderIntent:
        intent.quantity = LIVE_QTY
        intent.order_type = "LIMIT"
        intent.meta["live"] = True
        intent.meta["venue"] = "webull_live"
        intent.meta["host"] = PRODUCTION_TRADE_HOST
        intent.meta["paper"] = False
        intent.meta["sandbox"] = False
        desk = str(intent.desk or "").lower()
        kind = str((intent.meta or {}).get("golden_kind") or "")
        if desk not in {"sniper", "sniper_live"} and kind != "sniper":
            intent.status = "skipped"
            intent.error = "Sniper LIVE only accepts the Sniper desk"
            intent.updated_at = _now()
            self.save()
            return intent
        intent.desk = "sniper"
        if _symbol(intent) == "SPX":
            intent.status = "skipped"
            intent.error = "SPX is never a Webull LIVE order"
            intent.updated_at = _now()
            self.save()
            return intent
        if str(intent.action or "").upper() == "BUY" and (intent.limit_price is None or float(intent.limit_price) <= 0):
            intent.status = "skipped"
            intent.error = "Sniper LIVE requires a limit price (options cannot use MARKET)"
            intent.updated_at = _now()
            self.save()
            return intent
        if str(intent.action or "").upper() == "SELL" and (intent.limit_price is None or float(intent.limit_price) <= 0):
            intent.status = "skipped"
            intent.error = "Sniper LIVE requires a limit price (options cannot use MARKET)"
            intent.updated_at = _now()
            self.save()
            return intent
        return super().submit(intent)

    # ------------------------------------------------------------------
    # Real-account isolation. The live account also holds the owner's own
    # manual positions and orders. The shared AutoTrader exit code treats
    # any account long without a ledger buy as a lot to manage (premium
    # exits, 2:30 PM CT time-stop flattens), and history import adds any
    # ``wb-`` order it sees. On the paper sandbox that is right; on the real
    # account it would sell positions the bot never opened. So this broker
    # only ever reports contracts it bought itself, capped at its own net
    # quantity, and never adopts orders it did not place.
    # ------------------------------------------------------------------
    def _own_net_qty(self) -> dict[str, float]:
        net: dict[str, float] = {}
        for order in self.orders:
            occ = str(order.contract or "")
            if not occ:
                continue
            meta = order.meta or {}
            if str(meta.get("source") or "") == "webull_history_import":
                continue
            if order.action == "BUY":
                if order.status not in {"submitted", "partial", "filled"}:
                    continue
                if str(order.desk or "") not in {"sniper", "sniper_live"} and str(meta.get("golden_kind") or "") != "sniper":
                    continue
                qty = _float_or(meta.get("filled_qty")) if order.status == "partial" else None
                net[occ] = net.get(occ, 0.0) + float(qty if qty and qty > 0 else (order.quantity or LIVE_QTY))
            elif order.action == "SELL" and order.status in {"filled", "partial"}:
                qty = _float_or(meta.get("filled_qty")) or float(order.quantity or 1)
                net[occ] = net.get(occ, 0.0) - float(qty)
        return {occ: qty for occ, qty in net.items() if qty > 0}

    def list_option_positions(self, account_id: str | None = None) -> list[dict[str, Any]] | None:
        rows = super().list_option_positions(account_id)
        if rows is None:
            return None
        own = self._own_net_qty()
        kept: list[dict[str, Any]] = []
        for row in rows:
            occ = str(row.get("contract") or "")
            cap = own.get(occ)
            if not occ or not cap:
                continue  # owner's own position: never visible to the bot
            item = dict(row)
            try:
                held = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                held = 0.0
            if held > cap:
                item["quantity"] = cap
                item["owner_qty_hidden"] = held - cap
            kept.append(item)
        return kept

    def import_history(self, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
        known = {o.id for o in self.orders}
        mine = [r for r in (rows or []) if str((r or {}).get("client_order_id") or "") in known]
        summary = super().import_history(mine)
        summary["ignored_foreign"] = len(rows or []) - len(mine)
        return summary


def _float_or(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_sniper_live_broker(
    *,
    environ: Mapping[str, str] | None = None,
    ledger_path: str | Path | None = None,
    enabled: bool | None = None,
    dry_run: bool = False,
) -> SniperLiveBroker:
    """Build a live broker from WEBULL_LIVE_* only. Missing creds → not ready."""
    env = environ or os.environ
    flag = live_enabled(env) if enabled is None else bool(enabled)
    return SniperLiveBroker(
        app_key=_env_text(KEY_ENV, env) or None,
        app_secret=_env_text(SECRET_ENV, env) or None,
        account_id=_env_text(ACCOUNT_ENV, env) or None,
        ledger_path=ledger_path or DEFAULT_LEDGER,
        enabled=flag,
        dry_run=dry_run or (not flag),
    )


def get_live_broker(
    *,
    environ: Mapping[str, str] | None = None,
    ledger_path: str | Path | None = None,
    force_new: bool = False,
) -> SniperLiveBroker | None:
    """Process-wide live broker. None when the flag is off and no live book exists."""
    global _broker
    env = environ or os.environ
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
    flags = resolve_sniper_live_flags(env)
    has_book = path.exists() and path.stat().st_size > 2
    if not flags["ready"] and not has_book:
        return None
    with _LOCK:
        if force_new or _broker is None or _broker.path != path:
            _broker = build_sniper_live_broker(
                environ=env,
                ledger_path=path,
                enabled=flags["enabled"] or has_book,
                dry_run=not flags["ready"],
            )
            if flags["ready"]:
                _broker.enabled = True
                _broker.dry_run = False
        return _broker


def get_live_trader(
    *,
    environ: Mapping[str, str] | None = None,
    ledger_path: str | Path | None = None,
    broker: SniperLiveBroker | None = None,
    force_new: bool = False,
) -> Any | None:
    """AutoTrader bound to the live broker, exits and chase only."""
    global _trader
    live = broker or get_live_broker(environ=environ, ledger_path=ledger_path, force_new=force_new)
    if live is None:
        return None
    with _LOCK:
        if force_new or _trader is None or getattr(_trader, "broker", None) is not live:
            from odte_scanner.trading.auto_trader import AutoTrader

            _trader = AutoTrader(
                live,
                require_perfect_hist=False,
                golden_only=True,
                max_contracts=LIVE_QTY,
                max_orders_per_sync=DAILY_CAP,
                max_golden_buys_per_sync=0,
                close_legacy_positions=False,
                desks={
                    "lottery": False,
                    "odte": False,
                    "weekly": False,
                    "swing": False,
                    "challenge": False,
                    "sure_winner": False,
                    "golden_flow": False,
                    "diamond": False,
                    "sniper": True,
                    "afternoon_puts": False,
                    "lunch_credit": False,
                },
                lock=_LIVE_SYNC_LOCK,
            )
        return _trader


def _paper_slot(paper: WebullOrderIntent) -> str:
    meta = paper.meta or {}
    return str(meta.get("replaces") or meta.get("root_paper_id") or paper.id)


def _entry_count(state: dict[str, Any]) -> int:
    slots = {str(row.get("slot") or row.get("paper_id") or "") for row in state.get("entries") or [] if row}
    slots.discard("")
    return len(slots)


def _trip_day(stamp: str) -> str | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT).date().isoformat()


def _realized_from_ledger(broker: SniperLiveBroker, now: datetime | None = None) -> float:
    from odte_scanner.trading.portal_book import round_trips

    day = chicago_day(now)
    total = 0.0
    for trip in round_trips([o.to_dict() for o in broker.orders], include_other_desks=True):
        if _trip_day(str(trip.get("exited_at") or "")) not in {day, None}:
            continue
        pnl = trip.get("pnl")
        if pnl is None:
            continue
        try:
            total += float(pnl)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _record_entry(state: dict[str, Any], paper: WebullOrderIntent, live: WebullOrderIntent) -> None:
    slot = _paper_slot(paper)
    for row in state.get("entries") or []:
        if str(row.get("slot") or "") == slot:
            row["live_id"] = live.id
            row["paper_id"] = paper.id
            row["status"] = live.status
            return
    state.setdefault("entries", []).append(
        {
            "slot": slot,
            "paper_id": paper.id,
            "live_id": live.id,
            "symbol": live.symbol,
            "contract": live.contract,
            "status": live.status,
        }
    )


def _existing_live_for_slot(broker: SniperLiveBroker, slot: str, contract: str | None) -> WebullOrderIntent | None:
    occ = str(contract or "")
    for order in broker.orders:
        meta = order.meta or {}
        if str(meta.get("paper_slot") or "") == slot:
            return order
        if occ and order.contract and str(order.contract) == occ and order.action == "BUY":
            if order.status in {"staged", "submitted", "deferred", "dry_run", "partial", "filled"}:
                return order
    return None


def _working_buy(order: WebullOrderIntent) -> bool:
    return (
        order.action == "BUY"
        and order.status in {"staged", "submitted", "deferred", "dry_run", "partial"}
        and not (order.meta or {}).get("chase_abandoned")
    )


def notify_sniper_live(kind: str, order: WebullOrderIntent | None = None, extra: str = "") -> None:
    """Separate Telegram line, tagged [SNIPER LIVE]. Never includes secrets."""
    bits = ["[SNIPER LIVE]"]
    kind = str(kind or "").lower()
    if kind == "loss_cap":
        bits.append("LOSS CAP HIT")
        if extra:
            bits.append(extra)
    elif order is not None:
        label = {
            "entry": "ENTRY",
            "fill": "FILLED",
            "exit": "EXIT",
            "not_filled": "NOT FILLED",
            "rejected": "NOT FILLED",
        }.get(kind, kind.upper())
        bits.append(label)
        bits.append(f"{order.symbol} {order.contract or order.right or ''}".strip())
        bits.append(f"x{LIVE_QTY}")
        bits.append(activity_premium_label(order.to_dict()))
        if extra:
            bits.append(extra)
        if order.error and kind in {"not_filled", "rejected"}:
            bits.append(str(order.error)[:180])
    else:
        bits.append(kind.upper())
        if extra:
            bits.append(extra)
    body = " · ".join(str(b) for b in bits if b)
    try:
        from odte_scanner.alerts.telegram import send_telegram_text

        send_telegram_text(body)
    except Exception as exc:  # noqa: BLE001
        logger.info("sniper live telegram not sent: %s", exc)


def _alert_once(order: WebullOrderIntent, kind: str, extra: str = "") -> None:
    key = f"alerted_{kind}"
    if (order.meta or {}).get(key):
        return
    notify_sniper_live(kind, order, extra)
    order.meta[key] = True


def _copy_paper_fields(paper: WebullOrderIntent) -> dict[str, Any]:
    meta = dict(paper.meta or {})
    meta.update(
        {
            "live": True,
            "venue": "webull_live",
            "paper_id": paper.id,
            "paper_slot": _paper_slot(paper),
            "root_paper_id": str(meta.get("replaces") or meta.get("root_paper_id") or paper.id),
            "golden_kind": "sniper",
            "gold_exit": True,
            "sniper_flat_ct": "10:30",
            "host": PRODUCTION_TRADE_HOST,
            "paper": False,
            "sandbox": False,
        }
    )
    # Do not reuse the paper account id or sandbox host stamp.
    meta.pop("account_id", None)
    return meta


def _refuse(reason: str, paper: WebullOrderIntent | None = None) -> dict[str, Any]:
    logger.info("sniper live entry refused: %s", reason)
    return {"ok": False, "reason": reason, "paper_id": getattr(paper, "id", None)}


def mirror_paper_entry(
    paper: WebullOrderIntent,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    broker: SniperLiveBroker | None = None,
    state_path: str | Path | None = None,
    send_alerts: bool = True,
) -> dict[str, Any]:
    """When paper Sniper submits a buy, also submit the same contract x1 live."""
    if paper is None:
        return _refuse("no paper order")
    if is_live_broker(paper) or _is_live_order(paper):
        return _refuse("already a live order", paper)
    if not _is_sniper_buy(paper):
        return _refuse("not a Sniper paper buy", paper)
    if _symbol(paper) == "SPX":
        return _refuse("SPX is never a Webull LIVE order", paper)
    if paper.status not in {"submitted", "filled", "partial", "deferred", "staged"}:
        return _refuse(f"paper order not submitted ({paper.status})", paper)

    env = environ or os.environ
    flags = resolve_sniper_live_flags(env)
    if not flags["enabled"]:
        return _refuse(f"{ENABLED_ENV} is off", paper)
    if flags["missing"]:
        return _refuse("missing " + ", ".join(flags["missing"]), paper)
    if not in_sniper_entry_window(now):
        return _refuse("outside Sniper entry window 8:45–10:30 CT", paper)

    live = broker or get_live_broker(environ=env, force_new=broker is None and environ is not None)
    if live is None or not live.enabled or live.dry_run:
        return _refuse("live broker is not ready", paper)

    with _DAY_LOCK:
        state = load_day_state(Path(state_path) if state_path else None, now=now)
        state["realized_pnl"] = _realized_from_ledger(live, now)
        cap = flags["max_daily_loss"]
        if float(state["realized_pnl"]) <= cap:
            state["loss_cap_hit"] = True
            save_day_state(state, Path(state_path) if state_path else None)
            if send_alerts and not state.get("loss_cap_alerted"):
                notify_sniper_live(
                    "loss_cap",
                    extra=f"realized ${state['realized_pnl']:.2f} today · cap ${cap:.0f} · no new live entries",
                )
                state["loss_cap_alerted"] = True
                save_day_state(state, Path(state_path) if state_path else None)
            return _refuse("daily live realized loss cap hit", paper)

        slot = _paper_slot(paper)
        existing = _existing_live_for_slot(live, slot, paper.contract)
        replacing = bool((paper.meta or {}).get("replaces"))
        if existing is not None and existing.status in {"filled", "partial"}:
            return {"ok": True, "reason": "already filled live", "live_id": existing.id, "status": existing.status}
        if existing is not None and _working_buy(existing) and not replacing:
            return {"ok": True, "reason": "already working live", "live_id": existing.id, "status": existing.status}
        if existing is None and not replacing and _entry_count(state) >= DAILY_CAP:
            return _refuse("max 3 live entries per day", paper)

        if existing is not None and _working_buy(existing) and replacing:
            try:
                live.cancel(existing)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sniper live chase cancel failed: %s", exc)

        intent = live.stage(
            desk="sniper",
            action="BUY",
            symbol=paper.symbol,
            right=paper.right,
            contract=paper.contract,
            expiry=paper.expiry,
            strike=paper.strike,
            quantity=LIVE_QTY,
            limit_price=paper.limit_price,
            reason=paper.reason,
            hist_win_pct=paper.hist_win_pct,
            hist_samples=paper.hist_samples,
            meta=_copy_paper_fields(paper),
        )
        if intent.status == "skipped":
            return _refuse(intent.error or "live stage skipped", paper)
        submitted = live.submit(intent)
        _record_entry(state, paper, submitted)
        save_day_state(state, Path(state_path) if state_path else None)
        if send_alerts and submitted.status in {"submitted", "filled", "partial", "deferred"}:
            _alert_once(submitted, "fill" if submitted.status in {"filled", "partial"} else "entry")
        elif send_alerts and submitted.status in {"rejected", "skipped", "cancelled"}:
            _alert_once(submitted, "not_filled")
        return {
            "ok": submitted.status in {"submitted", "filled", "partial", "deferred"},
            "reason": submitted.error or submitted.status,
            "live_id": submitted.id,
            "status": submitted.status,
            "quantity": submitted.quantity,
        }


def _scan_alerts(broker: SniperLiveBroker, state: dict[str, Any], send_alerts: bool) -> None:
    if not send_alerts:
        return
    for order in broker.orders:
        if not _is_live_order(order):
            continue
        if order.action == "BUY" and order.status in {"filled", "partial"}:
            _alert_once(order, "fill")
        if order.action == "BUY" and order.status in {"cancelled", "rejected"} and not (order.meta or {}).get("replaced"):
            _alert_once(order, "not_filled")
        if order.action == "SELL" and order.status in {"filled", "partial"}:
            extra = str(order.reason or "")[:120]
            _alert_once(order, "exit", extra)


def tick_sniper_live(
    *,
    now: datetime | None = None,
    sniper: dict[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    broker: SniperLiveBroker | None = None,
    trader: Any | None = None,
    state_path: str | Path | None = None,
    send_alerts: bool = True,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Chase working live buys and run Sniper exits off the live fill price.

    New entries are not opened here. Open live positions are still exited
    even if SNIPER_LIVE_ENABLED is later turned off, as long as live creds
    are present.
    """
    env = environ or os.environ
    live = broker or get_live_broker(environ=env, ledger_path=ledger_path)
    if live is None:
        return {"skipped": "no live broker"}
    open_or_working = any(
        o.status in {"submitted", "deferred", "partial", "filled", "staged"}
        or (o.action == "BUY" and o.status == "filled")
        for o in live.orders
    )
    flags = resolve_sniper_live_flags(env)
    if not flags["ready"] and not open_or_working:
        return {"skipped": "not armed"}
    if flags["missing"] and open_or_working:
        logger.warning(
            "sniper live has open orders but missing creds: %s",
            ", ".join(flags["missing"]),
        )
        return {"skipped": "missing creds", "missing": flags["missing"]}

    live_trader = trader or get_live_trader(environ=env, broker=live, ledger_path=ledger_path)
    if live_trader is None:
        return {"skipped": "no live trader"}
    out: dict[str, Any] = {"ok": True}
    try:
        if sniper and flags["ready"]:
            live_trader._now = now
            live_trader.broker._session_now = now
            live_trader._chase_working_entries(None, sniper, None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("sniper live chase failed: %s", exc)
        out["chase_error"] = str(exc)[:200]
    try:
        exits = live_trader.sync_exits(now=now)
        out["exits"] = {"sent_n": len(exits.get("sent") or []), "ms": exits.get("ms")}
    except Exception as exc:  # noqa: BLE001
        logger.exception("sniper live exits failed: %s", exc)
        out["exit_error"] = str(exc)[:200]
    with _DAY_LOCK:
        state = load_day_state(Path(state_path) if state_path else None, now=now)
        state["realized_pnl"] = _realized_from_ledger(live, now)
        cap = flags["max_daily_loss"]
        if float(state["realized_pnl"]) <= cap:
            state["loss_cap_hit"] = True
            if send_alerts and not state.get("loss_cap_alerted"):
                notify_sniper_live(
                    "loss_cap",
                    extra=f"realized ${state['realized_pnl']:.2f} today · cap ${cap:.0f} · no new live entries",
                )
                state["loss_cap_alerted"] = True
        save_day_state(state, Path(state_path) if state_path else None)
        _scan_alerts(live, state, send_alerts)
        out["realized_pnl"] = state["realized_pnl"]
        out["entries_today"] = _entry_count(state)
        out["loss_cap_hit"] = bool(state["loss_cap_hit"])
    return out


def _unrealized(broker: SniperLiveBroker) -> tuple[int, int, float | None]:
    working = 0
    open_n = 0
    unreal = 0.0
    has = False
    filled_buys = [
        o
        for o in broker.orders
        if o.action == "BUY" and o.status in {"filled", "partial"} and o.fill_price
    ]
    sold_contracts = {
        str(o.contract or "")
        for o in broker.orders
        if o.action == "SELL" and o.status in {"filled", "partial"}
    }
    for order in broker.orders:
        if order.action == "BUY" and _working_buy(order) and order.status not in {"filled", "partial"}:
            working += 1
    for order in filled_buys:
        occ = str(order.contract or "")
        if occ and occ in sold_contracts:
            continue
        open_n += 1
        mark = None
        try:
            mark = float((order.meta or {}).get("mark") or 0) or None
        except (TypeError, ValueError):
            mark = None
        if mark and order.fill_price:
            unreal += (mark - float(order.fill_price)) * 100.0 * LIVE_QTY
            has = True
    return working, open_n, (round(unreal, 2) if has else None)


def portal_view(
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    broker: SniperLiveBroker | None = None,
    state_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Portal tile payload. Clearly separated from paper. No secret values."""
    env = environ or os.environ
    flags = resolve_sniper_live_flags(env)
    state = load_day_state(Path(state_path) if state_path else None, now=now)
    live = broker
    if live is None:
        path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
        if path.exists():
            live = build_sniper_live_broker(
                environ=env,
                ledger_path=path,
                enabled=False,
                dry_run=True,
            )
    realized = float(state.get("realized_pnl") or 0)
    if live is not None:
        realized = _realized_from_ledger(live, now)
    working = open_n = 0
    unreal = None
    orders: list[dict[str, Any]] = []
    if live is not None:
        working, open_n, unreal = _unrealized(live)
        day = chicago_day(now)
        for order in live.orders:
            created = str(order.created_at or "")
            stamp = created
            try:
                if created and chicago_day(datetime.fromisoformat(created.replace("Z", "+00:00"))) != day:
                    if order.status not in {"submitted", "deferred", "partial", "filled", "staged"}:
                        continue
            except ValueError:
                pass
            row = order.to_dict()
            row["display_price_label"] = activity_premium_label(row)
            row["live_tag"] = "LIVE"
            row.pop("error", None) if False else None
            # Never echo account ids or request bodies.
            meta = dict(row.get("meta") or {})
            for key in ("account_id", "openapi_order", "openapi_body", "app_key"):
                meta.pop(key, None)
            row["meta"] = {k: meta[k] for k in meta if k in {"live", "venue", "host", "paper", "sandbox", "golden_kind", "sniper_flat_ct", "fill_state", "paper_slot"}}
            orders.append(row)
    if not flags["enabled"]:
        status = "off"
        note = "Off. Paper Sniper is unchanged. Live stays off until SNIPER_LIVE_ENABLED=1 and the WEBULL_LIVE_* creds are set."
    elif flags["missing"]:
        status = "missing_creds"
        note = "Flag is on, but live creds are missing: " + ", ".join(flags["missing"]) + ". No live orders will be sent."
    elif state.get("loss_cap_hit"):
        status = "loss_cap"
        note = f"Daily live realized loss cap hit (${realized:.2f} / cap ${flags['max_daily_loss']:.0f}). No new live entries. Open positions still exit."
    elif not in_sniper_entry_window(now):
        status = "window_closed"
        note = "Armed. New live entries only 8:45–10:30 CT. Exits still run. Real money, 1 contract, max 3 names."
    else:
        status = "armed"
        note = "Armed. Mirrors each paper Sniper submit at 1 contract on the real Webull account. Max 3 / day. Not paper."
    return {
        "title": "Sniper LIVE",
        "venue": "webull_live",
        "real_money": True,
        "enabled": flags["enabled"],
        "ready": flags["ready"],
        "status": status,
        "host": PRODUCTION_TRADE_HOST,
        "qty": LIVE_QTY,
        "daily_cap": DAILY_CAP,
        "entries_today": _entry_count(state),
        "max_daily_loss": flags["max_daily_loss"],
        "realized_pnl": realized,
        "unrealized_pnl": unreal,
        "open_n": open_n,
        "working_n": working,
        "loss_cap_hit": bool(state.get("loss_cap_hit")),
        "window_open": in_sniper_entry_window(now),
        "note": note,
        "orders": orders[:20],
        "missing": flags["missing"],
        "flag_env": ENABLED_ENV,
        "separate_from_paper": True,
    }


def reset_singletons() -> None:
    """Tests only."""
    global _broker, _trader, _TOKEN_FAIL_AT
    with _LOCK:
        _broker = None
        _trader = None
        _TOKEN_FAIL_AT = None
