"""60-second Polymarket poller: universe refresh, fills, midpoints, alerts.

Alert only. This module imports no order path and never calls Webull, Sniper
LIVE, or any broker. Bullflow use is limited to ONE optional REST
``historicalOptionsFlow`` request per fired alert (never the SSE stream),
and only when ``BULLFLOW_API_KEY`` is set; any failure fails open.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from odte_scanner.polymarket import ALERT_LABEL
from odte_scanner.polymarket.client import PolymarketClient, PolymarketError, fill_notional
from odte_scanner.polymarket.detector import (
    CT,
    ET,
    DetectorSettings,
    WalletSignal,
    check_odds_spike,
    ct_label,
    evaluate_wallet,
    implied_direction,
    mid_change,
    to_epoch,
)
from odte_scanner.polymarket.universe import (
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TAG_IDS,
    UniverseMarket,
    build_universe,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = ROOT / "outputs" / "polymarket_monitor_state.json"
MAX_SEEN = 5_000
MAX_ALERTS = 25
MAX_MID_POINTS = 120
MAX_DEDUPE = 2_000
MAX_MID_TOKENS = 400
BULLFLOW_GOLDEN_PREMIUM = 1_000_000.0


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def monitor_enabled(config: dict | None = None) -> bool:
    """Env ``POLYMARKET_MONITOR_ENABLED`` wins; else ``polymarket.enabled`` (default on)."""
    raw = (os.environ.get("POLYMARKET_MONITOR_ENABLED") or "").strip()
    if raw:
        return _env_flag("POLYMARKET_MONITOR_ENABLED", True)
    cfg = (config or {}).get("polymarket") if isinstance(config, dict) else None
    if isinstance(cfg, dict) and cfg.get("enabled") is not None:
        return bool(cfg.get("enabled"))
    return True


# --------------------------------------------------------------------------
# State: cursors, last midpoints, 24h dedupe, recent alerts. Bounded on disk.
# --------------------------------------------------------------------------
class MonitorState:
    def __init__(self, path: Path | str | None = None) -> None:
        env_path = (os.environ.get("POLYMARKET_STATE_PATH") or "").strip()
        self.path = Path(path) if path else (Path(env_path) if env_path else DEFAULT_STATE_PATH)
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.cursors: dict[str, int] = {}
        self.seen: deque[str] = deque(maxlen=MAX_SEEN)
        self._seen_set: set[str] = set()
        self.mids: dict[str, list[list[float]]] = {}
        self.dedupe: dict[str, dict[str, Any]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.read_only = False

    def load(self) -> "MonitorState":
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self
        if not isinstance(raw, dict):
            return self
        self.cursors = {str(k): int(v) for k, v in (raw.get("cursors") or {}).items() if isinstance(v, (int, float))}
        for key in (raw.get("seen") or [])[-MAX_SEEN:]:
            self.mark_seen(str(key))
        self.mids = {
            str(k): [[int(p[0]), float(p[1])] for p in v if isinstance(p, list) and len(p) == 2][-MAX_MID_POINTS:]
            for k, v in (raw.get("mids") or {}).items()
            if isinstance(v, list)
        }
        self.dedupe = {str(k): dict(v) for k, v in (raw.get("dedupe") or {}).items() if isinstance(v, dict)}
        self.alerts = [a for a in (raw.get("alerts") or []) if isinstance(a, dict)][-MAX_ALERTS:]
        return self

    def save(self) -> bool:
        if self.read_only:
            return False
        body = {
            "version": 1,
            "cursors": self.cursors,
            "seen": list(self.seen),
            "mids": self.mids,
            "dedupe": self.dedupe,
            "alerts": self.alerts[-MAX_ALERTS:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, separators=(",", ":")))
            tmp.replace(self.path)
            return True
        except OSError as exc:
            # Read-only filesystem: keep going in memory.
            logger.warning("polymarket state not writable (%s); memory only", type(exc).__name__)
            self.read_only = True
            return False

    def mark_seen(self, key: str) -> None:
        if key in self._seen_set:
            return
        if len(self.seen) == self.seen.maxlen:
            self._seen_set.discard(self.seen[0])
        self.seen.append(key)
        self._seen_set.add(key)

    def is_seen(self, key: str) -> bool:
        return key in self._seen_set

    def record_mid(self, token_id: str, ts: int, mid: float) -> None:
        pts = self.mids.setdefault(token_id, [])
        pts.append([int(ts), float(mid)])
        cutoff = ts - 5_400
        self.mids[token_id] = [p for p in pts if p[0] >= cutoff][-MAX_MID_POINTS:]
        if len(self.mids) > MAX_MID_TOKENS:
            stale = sorted(self.mids, key=lambda k: self.mids[k][-1][0] if self.mids[k] else 0)
            for k in stale[: len(self.mids) - MAX_MID_TOKENS]:
                self.mids.pop(k, None)

    def prune(self, now_ts: int, dedupe_sec: int) -> None:
        self.dedupe = {k: v for k, v in self.dedupe.items() if now_ts - int(v.get("ts") or 0) < dedupe_sec}
        if len(self.dedupe) > MAX_DEDUPE:
            keep = sorted(self.dedupe.items(), key=lambda kv: -int(kv[1].get("ts") or 0))[:MAX_DEDUPE]
            self.dedupe = dict(keep)


def fill_key(row: dict) -> str:
    return "|".join(
        str(row.get(k) or "") for k in ("transaction_hash", "proxy_wallet", "token_id", "side", "size", "timestamp")
    )


# --------------------------------------------------------------------------
# Optional Bullflow REST confirmation (single request, fail open, never SSE)
# --------------------------------------------------------------------------
def _session_date(ts: int) -> str:
    day = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def bullflow_confirmation(
    ticker: str | None,
    direction: str | None,
    when_ts: int,
    *,
    fetch: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """At most ONE ``GET /v1/data/historicalOptionsFlow``. Never opens the SSE stream."""
    if not ticker:
        return {"checked": False, "note": "no mapped ticker"}
    try:
        from odte_scanner.signals import bullflow
    except Exception:  # noqa: BLE001
        return {"checked": False, "note": "bullflow module unavailable"}
    if not bullflow.api_key_from_env():
        return {"checked": False, "configured": False, "note": "not configured"}
    getter = fetch or bullflow.fetch_historical_options_flow
    try:
        res = getter(
            date=_session_date(when_ts),
            ticker=ticker,
            min_premium=BULLFLOW_GOLDEN_PREMIUM,
            limit=200,
            max_pages=1,
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "configured": True, "note": f"unavailable ({type(exc).__name__})"}
    if isinstance(res, dict) and res.get("status_code") == 429:
        return {"checked": False, "configured": True, "note": "unavailable (rate limited)"}
    if not isinstance(res, dict) or not res.get("ok"):
        err = res.get("error") if isinstance(res, dict) else None
        return {"checked": False, "configured": True, "note": f"unavailable ({str(err or 'error')[:60]})"}
    calls = puts = 0
    for row in res.get("data") or []:
        try:
            premium = float(row.get("premium") or 0)
        except (TypeError, ValueError):
            continue
        if premium < BULLFLOW_GOLDEN_PREMIUM or str(row.get("side") or "") not in {"Ask", "Above Ask"}:
            continue
        typ = str(row.get("optionType") or "").upper()[:1]
        if typ == "C":
            calls += 1
        elif typ == "P":
            puts += 1
    same = calls if direction == "call" else puts if direction == "put" else 0
    note = (
        f"{same} same-direction $1M+ {direction} sweep(s) on {ticker}"
        if direction
        else f"{calls} call / {puts} put $1M+ ask-side prints on {ticker}"
    )
    return {
        "checked": True,
        "configured": True,
        "ticker": ticker,
        "direction": direction,
        "calls_1m": calls,
        "puts_1m": puts,
        "same_direction": same,
        "confirmed": bool(direction and same > 0),
        "note": note,
    }


# --------------------------------------------------------------------------
# Telegram copy
# --------------------------------------------------------------------------
def _cents(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.1f}¢"


def _short(wallet: str) -> str:
    w = str(wallet or "")
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w


def format_alert_text(alert: dict[str, Any]) -> str:
    """Plain-text Telegram body. Labelled for review; never claims proof."""
    lines = [f"🛰️ {ALERT_LABEL}"]
    head = f"{alert.get('tier_label') or 'Tier ?'} · score {alert.get('score', 0)}/100"
    if alert.get("priority") == "high":
        head += " · HIGH priority"
    if alert.get("escalation"):
        head += f" · UPDATE ({alert['escalation']})"
    lines.append(head)
    lines.append(str(alert.get("question") or ""))
    lines.append(f"Ticker: {alert.get('ticker') or '—'}")
    if alert.get("kind") == "wallet":
        first, last = alert.get("first_fill_ts"), alert.get("last_fill_ts")
        span = ct_label(last) if first == last else f"{ct_label(first).replace(' CT', '')}–{ct_label(last)}"
        lines.append(
            f"BUY {str(alert.get('outcome') or '').upper()} @ {_cents(alert.get('avg_price'))} · "
            f"${alert.get('cluster_usd', 0):,.2f} · {alert.get('cluster_shares', 0):,.1f} shares · "
            f"{alert.get('n_fills', 0)} fill{'' if alert.get('n_fills') == 1 else 's'} in 2h ({span})"
        )
        age = alert.get("wallet_age_days")
        mk = alert.get("distinct_markets")
        share = alert.get("condition_share")
        wallet_bits = [
            f"Wallet: {alert.get('name') or '—'} ({_short(alert.get('wallet') or '')})",
            f"joined {age:.1f}d ago" if age is not None else "join date unknown",
            f"{mk} markets" if mk is not None else "markets unknown",
        ]
        if share is not None:
            wallet_bits.append(f"{share * 100:.0f}% of volume here")
        lines.append(" · ".join(wallet_bits))
        ab = alert.get("absorption_pct")
        lines.append(f"Ask-book absorption: {ab:.0f}%" if ab is not None else "Ask-book absorption: n/a")
    else:
        sp = alert.get("spike") or {}
        lines.append(
            f"Odds spike {str(alert.get('outcome') or 'YES').upper()}: {_cents(sp.get('base_mid'))} → {_cents(sp.get('mid'))} "
            f"(+{sp.get('jump', 0) * 100:.1f}¢, +{sp.get('jump_pct', 0):.0f}%) in {sp.get('minutes', '?')} min · "
            f"${sp.get('volume_usd', 0):,.0f} traded"
        )
    m1h, d1h = alert.get("mid_1h_ago"), alert.get("mid_1h_change")
    if d1h is not None:
        sign = "+" if d1h > -0.0005 else "−"
        lines.append(f"Odds 1h: {_cents(m1h)} → {_cents(alert.get('mid_now'))} ({sign}{abs(d1h) * 100:.1f}¢)")
    else:
        lines.append(f"Odds 1h: n/a (mid now {_cents(alert.get('mid_now'))})")
    if alert.get("reasons"):
        lines.append("Why: " + " · ".join(alert["reasons"]))
    bf = alert.get("bullflow") or {}
    lines.append(f"Bullflow: {bf.get('note') or 'not checked'}")
    lines.append("Alert only — no orders placed. Pattern match, not proof of insider trading.")
    lines.append(str(alert.get("url") or "https://polymarket.com"))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------
class PolymarketMonitor:
    def __init__(
        self,
        *,
        client: PolymarketClient | None = None,
        state: MonitorState | None = None,
        settings: DetectorSettings | None = None,
        config: dict | None = None,
        sender: Callable[[str], Any] | None = None,
        bullflow_fetch: Callable[..., dict] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        cfg = dict(((config or {}).get("polymarket") or {}) if isinstance(config, dict) else {})
        self.cfg = cfg
        self.client = client or PolymarketClient()
        self.state = state or MonitorState((os.environ.get("POLYMARKET_STATE_PATH") or "").strip() or cfg.get("state_path")).load()
        self.settings = settings or DetectorSettings.from_mapping(cfg)
        self.sender = sender
        self.bullflow_fetch = bullflow_fetch
        self.clock = clock
        self.poll_sec = max(30, int(cfg.get("poll_sec") or 60))
        self.universe_refresh_sec = max(300, int(cfg.get("universe_refresh_sec") or 1_200))
        self.min_cash = float(cfg.get("min_fill_usd") or 250.0)
        self.max_wallet_lookups = max(1, int(cfg.get("max_wallet_lookups") or 8))
        self.startup_lookback_sec = int(cfg.get("startup_lookback_sec") or 300)
        self.tag_ids = list(cfg.get("tag_ids") or DEFAULT_TAG_IDS)
        self.search_queries = list(cfg.get("search_queries") or DEFAULT_SEARCH_QUERIES)
        self.focus = list(cfg.get("focus") or ((config or {}).get("tickers") if isinstance(config, dict) else []) or [])
        self.max_markets = int(cfg.get("max_markets") or 150)
        self.universe: dict[str, UniverseMarket] = {}
        self.universe_meta: dict[str, Any] = {}
        self.universe_ts: int | None = None
        self.last_poll_ts: int | None = None
        self.last_error: str | None = None
        self.polls = 0
        self.running = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._user_stats_cache: dict[str, tuple[int, dict]] = {}

    # -- universe --------------------------------------------------------
    def set_universe(self, markets: Iterable[UniverseMarket], now_ts: int | None = None) -> None:
        self.universe = {m.condition_id: m for m in markets}
        self.universe_ts = int(now_ts if now_ts is not None else self.clock())

    def refresh_universe(self, now_ts: int) -> None:
        markets, meta = build_universe(
            self.client,
            tag_ids=self.tag_ids,
            search_queries=self.search_queries,
            focus=self.focus,
            max_markets=self.max_markets,
        )
        if markets or not self.universe:
            self.set_universe(markets, now_ts)
        self.universe_meta = meta

    # -- one poll --------------------------------------------------------
    def poll_once(self, now_ts: int | None = None) -> list[dict[str, Any]]:
        now = int(now_ts if now_ts is not None else self.clock())
        fired: list[dict[str, Any]] = []
        with self._lock:
            try:
                if not self.universe or self.universe_ts is None or now - self.universe_ts >= self.universe_refresh_sec:
                    self.refresh_universe(now)
                mids = self._poll_mids(now)
                rows_by_cid = self._poll_trades()
                fired.extend(self._scan_wallets(rows_by_cid, mids, now))
                fired.extend(self._scan_spikes(rows_by_cid, mids, now))
                self.last_error = None
            except PolymarketError as exc:
                self.last_error = str(exc)[:200]
                logger.warning("polymarket poll failed: %s", self.last_error)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                logger.exception("polymarket poll crashed")
            self.last_poll_ts = now
            self.polls += 1
            self.state.prune(now, self.settings.dedupe_sec)
            self.state.save()
        return fired

    def _poll_mids(self, now: int) -> dict[str, float]:
        tokens = [m.token_ids[0] for m in self.universe.values() if m.token_ids]
        if not tokens:
            return {}
        mids = self.client.midpoints(tokens)
        for tok, mid in mids.items():
            self.state.record_mid(tok, now, mid)
        return mids

    def _poll_trades(self) -> dict[str, list[dict]]:
        cids = list(self.universe)
        out: dict[str, list[dict]] = {}
        for i in range(0, len(cids), 20):
            batch = cids[i : i + 20]
            for row in self.client.trades(batch, min_cash=self.min_cash, taker_only=False):
                out.setdefault(str(row.get("condition_id") or ""), []).append(row)
        return out

    def _new_rows(self, cid: str, rows: list[dict], now: int) -> list[dict]:
        cursor = self.state.cursors.get(cid)
        floor = cursor if cursor is not None else now - self.startup_lookback_sec
        fresh: list[dict] = []
        newest = cursor or 0
        for r in rows:
            ts = to_epoch(r.get("timestamp")) or 0
            newest = max(newest, ts)
            key = fill_key(r)
            if ts < floor or self.state.is_seen(key):
                continue
            self.state.mark_seen(key)
            fresh.append(r)
        self.state.cursors[cid] = max(newest, floor)
        return fresh

    def _user_stats(self, wallet: str, now: int) -> dict:
        hit = self._user_stats_cache.get(wallet)
        if hit and now - hit[0] < 1_800:
            return hit[1]
        stats = self.client.user_stats(wallet)
        self._user_stats_cache[wallet] = (now, stats)
        if len(self._user_stats_cache) > 500:
            for k in sorted(self._user_stats_cache, key=lambda k: self._user_stats_cache[k][0])[:100]:
                self._user_stats_cache.pop(k, None)
        return stats

    def _scan_wallets(self, rows_by_cid: dict[str, list[dict]], mids: dict[str, float], now: int) -> list[dict]:
        s = self.settings
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for cid, rows in rows_by_cid.items():
            if cid not in self.universe:
                continue
            for r in self._new_rows(cid, rows, now):
                if str(r.get("side") or "").upper() != "BUY":
                    continue
                try:
                    price = float(r.get("price") or 1)
                except (TypeError, ValueError):
                    continue
                if price > s.longshot_max + 1e-9:
                    continue
                wallet = str(r.get("proxy_wallet") or "").lower()
                if wallet:
                    groups.setdefault((wallet, cid, str(r.get("token_id") or "")), []).append(r)
        # Biggest new longshot buyers first; cap lookups per poll.
        ranked = sorted(groups.items(), key=lambda kv: -sum(fill_notional(r) for r in kv[1]))
        fired: list[dict] = []
        for (wallet, cid, token), new_rows in ranked[: self.max_wallet_lookups]:
            market = self.universe[cid]
            stats = self._user_stats(wallet, now)
            activity = self.client.activity(wallet, cid, start=now - 14 * 86_400)
            market_usd = [fill_notional(r) for r in rows_by_cid.get(cid, [])
                          if str(r.get("proxy_wallet") or "").lower() != wallet]
            name = str(new_rows[0].get("name") or new_rows[0].get("pseudonym") or "")
            outcome = market.outcome_for_token(token) or str(new_rows[0].get("outcome") or "")
            sig = evaluate_wallet(
                wallet=wallet, condition_id=cid, token_id=token, activity=activity,
                user_stats=stats, now_ts=now, market_fills_usd=market_usd,
                settings=s, name=name, outcome=outcome,
            )
            if not sig.tier:
                continue
            decision = self._dedupe_decision(f"w|{wallet}|{cid}", sig.cluster_usd, now)
            if decision is None:
                continue
            try:
                book = self.client.book(token)
            except PolymarketError:
                book = None
            if book:
                sig = evaluate_wallet(
                    wallet=wallet, condition_id=cid, token_id=token, activity=activity,
                    user_stats=stats, now_ts=now, market_fills_usd=market_usd, book=book,
                    settings=s, name=name, outcome=outcome,
                )
            alert = self._wallet_alert(market, sig, mids, now, decision)
            self._fire(alert, f"w|{wallet}|{cid}", sig.cluster_usd, now)
            fired.append(alert)
        return fired

    def _dedupe_decision(self, key: str, usd: float, now: int) -> str | None:
        """'' for a first alert, 'cluster grew Nx' for an escalation, None to suppress."""
        prev = self.state.dedupe.get(key)
        if not prev or now - int(prev.get("ts") or 0) >= self.settings.dedupe_sec:
            return ""
        last_usd = float(prev.get("usd") or 0)
        esc = int(prev.get("escalations") or 0)
        if usd > 0 and last_usd > 0 and usd >= last_usd * self.settings.escalate_ratio and esc < self.settings.max_escalations:
            return f"cluster grew {usd / last_usd:.1f}x"
        return None

    def _yes_mid(self, market: UniverseMarket, mids: dict[str, float]) -> float | None:
        tok = market.token_ids[0] if market.token_ids else None
        return mids.get(tok) if tok else None

    def _outcome_mid(self, market: UniverseMarket, token: str, mids: dict[str, float]) -> tuple[float | None, list]:
        yes_tok = market.token_ids[0] if market.token_ids else ""
        hist = self.state.mids.get(yes_tok, [])
        yes_mid = mids.get(yes_tok)
        if token == yes_tok or not token:
            return yes_mid, hist
        return (1 - yes_mid if yes_mid is not None else None), [[t, 1 - m] for t, m in hist]

    def _wallet_alert(self, market: UniverseMarket, sig: WalletSignal, mids: dict[str, float], now: int, escalation: str) -> dict:
        mid_now, hist = self._outcome_mid(market, sig.token_id, mids)
        m1h, d1h = mid_change(hist, now, mid_now)
        alert = {
            "kind": "wallet",
            "label": ALERT_LABEL,
            "tier": sig.tier,
            "tier_label": sig.tier_label,
            "score": sig.score,
            "question": market.question,
            "ticker": market.ticker,
            "url": market.url,
            "condition_id": market.condition_id,
            "mid_now": mid_now,
            "mid_1h_ago": m1h,
            "mid_1h_change": d1h,
            "escalation": escalation or None,
            "ts": now,
            "ts_ct": ct_label(now),
            "alert_only": True,
        }
        alert.update({k: v for k, v in sig.to_dict().items() if k not in alert})
        alert["bullflow"] = bullflow_confirmation(
            market.ticker, implied_direction(market.question, sig.outcome), sig.last_fill_ts or now, fetch=self.bullflow_fetch
        )
        alert["priority"] = "high" if (alert["bullflow"].get("confirmed") or sig.score >= 75) else "normal"
        alert["text"] = format_alert_text(alert)
        return alert

    def _scan_spikes(self, rows_by_cid: dict[str, list[dict]], mids: dict[str, float], now: int) -> list[dict]:
        fired: list[dict] = []
        for cid, market in self.universe.items():
            yes_tok = market.token_ids[0] if market.token_ids else None
            if not yes_tok or yes_tok not in mids:
                continue
            vol = sum(fill_notional(r) for r in rows_by_cid.get(cid, [])
                      if (to_epoch(r.get("timestamp")) or 0) >= now - self.settings.spike_window_sec)
            hist = self.state.mids.get(yes_tok, [])
            for outcome_idx in (0, 1):
                mid_now = mids[yes_tok] if outcome_idx == 0 else 1 - mids[yes_tok]
                h = hist if outcome_idx == 0 else [[t, 1 - m] for t, m in hist]
                spike = check_odds_spike(h, now_ts=now, mid_now=mid_now, recent_volume_usd=vol, settings=self.settings)
                if not spike:
                    continue
                key = f"s|{cid}"
                prev = self.state.dedupe.get(key)
                if prev and now - int(prev.get("ts") or 0) < self.settings.dedupe_sec:
                    break
                outcome = market.outcomes[outcome_idx] if outcome_idx < len(market.outcomes) else ("Yes", "No")[outcome_idx]
                m1h, d1h = mid_change(h, now, mid_now)
                alert = {
                    "kind": "odds_spike",
                    "label": ALERT_LABEL,
                    "tier": "B",
                    "tier_label": "Tier B — Odds spike",
                    "score": min(100, int(40 + spike["jump_pct"] / 5)),
                    "question": market.question,
                    "ticker": market.ticker,
                    "url": market.url,
                    "condition_id": cid,
                    "outcome": outcome,
                    "spike": spike,
                    "mid_now": mid_now,
                    "mid_1h_ago": m1h,
                    "mid_1h_change": d1h,
                    "reasons": [f"mid +{spike['jump'] * 100:.1f}¢ (+{spike['jump_pct']:.0f}%) in {spike['minutes']} min",
                                f"${spike['volume_usd']:,.0f} volume"],
                    "ts": now,
                    "ts_ct": ct_label(now),
                    "alert_only": True,
                }
                alert["bullflow"] = bullflow_confirmation(
                    market.ticker, implied_direction(market.question, outcome), now, fetch=self.bullflow_fetch
                )
                alert["priority"] = "high" if alert["bullflow"].get("confirmed") else "normal"
                alert["text"] = format_alert_text(alert)
                self._fire(alert, key, 0.0, now)
                fired.append(alert)
                break
        return fired

    def _fire(self, alert: dict, key: str, usd: float, now: int) -> None:
        prev = self.state.dedupe.get(key) or {}
        esc = int(prev.get("escalations") or 0) + (1 if alert.get("escalation") else 0)
        self.state.dedupe[key] = {"ts": now, "usd": usd, "escalations": esc}
        sent: Any = None
        try:
            if self.sender is not None:
                sent = self.sender(alert["text"])
            else:
                from odte_scanner.alerts.telegram import send_telegram_text

                sent = send_telegram_text(alert["text"])
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "error": type(exc).__name__}
        alert["telegram"] = {"ok": bool(isinstance(sent, dict) and sent.get("ok"))}
        slim = {k: alert.get(k) for k in (
            "kind", "tier", "tier_label", "score", "priority", "question", "ticker", "url", "outcome",
            "avg_price", "cluster_usd", "cluster_shares", "wallet", "name", "wallet_age_days",
            "distinct_markets", "absorption_pct", "mid_now", "mid_1h_change", "reasons", "ts", "ts_ct",
            "escalation", "label",
        )}
        self.state.alerts = (self.state.alerts + [slim])[-MAX_ALERTS:]

    # -- worker ----------------------------------------------------------
    def run_forever(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self.poll_sec)
        finally:
            self.running = False

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        now = int(self.clock())
        today = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(CT).date()
        alerts_today = [a for a in self.state.alerts
                        if a.get("ts") and datetime.fromtimestamp(int(a["ts"]), tz=timezone.utc).astimezone(CT).date() == today]
        return {
            "ok": self.last_error is None,
            "enabled": True,
            "running": self.running,
            "alert_only": True,
            "label": ALERT_LABEL,
            "universe_count": len(self.universe),
            "universe_refreshed": self.universe_ts,
            "last_poll": self.last_poll_ts,
            "last_poll_ct": ct_label(self.last_poll_ts) if self.last_poll_ts else None,
            "last_error": self.last_error,
            "polls": self.polls,
            "api_calls": getattr(self.client, "calls", None),
            "alerts_today": len(alerts_today),
            "recent_alerts": list(reversed(self.state.alerts[-10:])),
        }


_MONITOR: PolymarketMonitor | None = None
_MONITOR_LOCK = threading.Lock()


def get_monitor() -> PolymarketMonitor | None:
    return _MONITOR


def reset_monitor_for_tests() -> None:
    global _MONITOR
    with _MONITOR_LOCK:
        if _MONITOR is not None:
            _MONITOR.stop()
        _MONITOR = None


def start_polymarket_worker(config_path: str | None = None) -> bool:
    """Start one daemon poller per process. Off in offline exports and when disabled."""
    global _MONITOR
    if _env_flag("SIGNAL_DESK_OFFLINE", False):
        return False
    config: dict = {}
    try:
        from odte_scanner.config import load_config

        config = load_config(config_path) or {}
    except Exception:  # noqa: BLE001
        config = {}
    if not monitor_enabled(config):
        logger.info("Polymarket monitor disabled")
        return False
    with _MONITOR_LOCK:
        if _MONITOR is not None:
            return False
        _MONITOR = PolymarketMonitor(config=config)
        threading.Thread(target=_MONITOR.run_forever, name="polymarket-monitor", daemon=True).start()
    return True


def public_block() -> dict[str, Any]:
    """Snapshot / scan_status health. Never includes secrets."""
    mon = _MONITOR
    if mon is None:
        enabled = monitor_enabled(None) and not _env_flag("SIGNAL_DESK_OFFLINE", False)
        return {
            "ok": False,
            "enabled": enabled,
            "running": False,
            "alert_only": True,
            "label": ALERT_LABEL,
            "universe_count": 0,
            "last_poll": None,
            "last_poll_ct": None,
            "last_error": None,
            "polls": 0,
            "alerts_today": 0,
            "recent_alerts": [],
        }
    return mon.status()
