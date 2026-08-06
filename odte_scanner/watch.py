from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.calendars import resolve_universe, resolve_yahoo_symbol
from odte_scanner.config import load_config
from odte_scanner.data.live_quotes import fetch_live_quotes
from odte_scanner.scanner import run_scan

logger = logging.getLogger(__name__)


def _alert_key(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('symbol')}|{candidate.get('expiry')}|{candidate.get('strike')}"


def run_watch(
    config_path: str | None = None,
    *,
    interval_sec: float | None = None,
    max_cycles: int | None = None,
    place_paper: bool = False,
    tickers_filter: list[str] | None = None,
) -> None:
    """
    Continuous scanner loop (24/5 style).

    Not literally every second — Yahoo / free data will rate-limit. Default ~60s.
    Includes extended-hours quotes when available (premarket / after-hours / overnight bars).
    """
    cfg = load_config(config_path)
    watch_cfg = cfg.get("watch") or {}
    interval = float(interval_sec if interval_sec is not None else watch_cfg.get("interval_sec", 60))
    interval = max(15.0, interval)  # hard floor to protect data source
    full_scan_every = int(watch_cfg.get("full_scan_every_n", 1))
    quote_top_n = int(watch_cfg.get("quote_top_n", 25))
    min_score = float((cfg.get("scan") or {}).get("min_score", 62))
    out_dir = Path(watch_cfg.get("output_dir", "outputs/watch"))
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "watch_state.json"

    aliases = {str(k).upper(): str(v) for k, v in (cfg.get("symbol_aliases") or {}).items()}
    universe = tickers_filter or resolve_universe(cfg)
    # Ensure aliases resolve for live quotes
    aliases = {s: resolve_yahoo_symbol(s, cfg) for s in universe} | aliases

    seen_alerts: set[str] = set()
    if state_path.exists():
        try:
            seen_alerts = set(json.loads(state_path.read_text()).get("seen_alerts", []))
        except Exception:  # noqa: BLE001
            seen_alerts = set()

    cycle = 0
    logger.info(
        "Watch started: interval=%ss universe=%d full_scan_every=%d (Ctrl+C to stop)",
        interval,
        len(universe),
        full_scan_every,
    )

    while True:
        cycle += 1
        started = datetime.now(timezone.utc)
        snapshot: dict[str, Any] = {
            "cycle": cycle,
            "started_at": started.isoformat(),
            "interval_sec": interval,
        }

        # Always refresh live / extended-hours tape for the watchlist
        quote_syms = universe[: max(quote_top_n, len(universe))]
        quotes = fetch_live_quotes(quote_syms, aliases=aliases)
        # Sort by Robinhood-style session change when present, else vs prior close
        def _sort_key(q):
            return q.session_change_pct if q.session_change_pct is not None else q.change_pct

        movers = sorted(quotes.values(), key=_sort_key)
        snapshot["quotes"] = {s: q.to_dict() for s, q in quotes.items()}
        snapshot["weakest"] = [q.to_dict() for q in movers[:8]]
        snapshot["strongest"] = [q.to_dict() for q in movers[-8:][::-1]]

        report = None
        if cycle % full_scan_every == 0:
            # Full algo + options strike scan; paper optional (usually off in watch)
            report = run_scan(config_path, place_paper=place_paper)
            snapshot["scan"] = {
                "session_weekday": report.get("session_weekday"),
                "scores_top": report.get("scores", [])[:15],
                "call_candidates": report.get("call_candidates", [])[:10],
            }
            # Overlay live change% onto candidates when present
            for c in snapshot["scan"]["call_candidates"]:
                q = quotes.get(c["symbol"])
                if q:
                    c["live_last"] = round(q.last, 4)
                    c["live_change_pct"] = round(q.change_pct, 3)
                    c["live_session"] = q.session

            new_alerts = []
            for c in report.get("call_candidates", []):
                if c.get("score", 0) < min_score:
                    continue
                key = _alert_key(c)
                if key in seen_alerts:
                    continue
                seen_alerts.add(key)
                new_alerts.append(c)
            snapshot["new_alerts"] = new_alerts
            if new_alerts:
                logger.info(
                    "NEW alerts: %s",
                    ", ".join(
                        f"{a['symbol']} {a['strike']}c @ {a['ask']} (score {a['score']:.0f})"
                        for a in new_alerts
                    ),
                )

            # Keep signal journal in sync when watch runs full scans
            try:
                from odte_scanner.config import load_config
                from odte_scanner.signals.actions import build_action_board
                from odte_scanner.trading.journal import SignalJournal

                cfg = load_config(config_path)
                jcfg = cfg.get("journal") or {}
                if jcfg.get("enabled", True):
                    actions = build_action_board(
                        candidates=report.get("call_candidates") or [],
                        scores=report.get("scores") or [],
                        quotes=snapshot.get("quotes") or {},
                        ledger=None,
                        buy_score=float((cfg.get("actions") or {}).get("buy_score", 70)),
                        win_rate_table=report.get("win_rates"),
                    )
                    journal = SignalJournal(
                        jcfg.get("path", "outputs/signal_journal.json"),
                        starting_cash=float(jcfg.get("starting_cash", 5000)),
                    )
                    sync = journal.sync_from_actions(
                        actions,
                        max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                        auto_enter=bool(jcfg.get("auto_enter", True)),
                        auto_exit=bool(jcfg.get("auto_exit", True)),
                    )
                    snapshot["journal_sync"] = {
                        "entered": sync.get("entered"),
                        "exited": sync.get("exited"),
                        "performance": sync.get("performance"),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.debug("journal sync skipped: %s", exc)

        snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
        latest = out_dir / "latest_watch.json"
        latest.write_text(json.dumps(snapshot, indent=2))
        (out_dir / f"watch_{started.strftime('%Y%m%d_%H%M%S')}.json").write_text(
            json.dumps(snapshot, indent=2)
        )
        state_path.write_text(
            json.dumps(
                {
                    "seen_alerts": sorted(seen_alerts)[-500:],
                    "last_cycle": cycle,
                    "updated_at": snapshot["finished_at"],
                },
                indent=2,
            )
        )

        # Console summary
        weak = snapshot["weakest"][:3]
        strong = snapshot["strongest"][:3]
        logger.info(
            "cycle=%d quotes=%d weak=%s strong=%s",
            cycle,
            len(quotes),
            [
                (
                    w["symbol"],
                    round(w.get("session_change_pct") or w["change_pct"], 2),
                )
                for w in weak
            ],
            [
                (
                    s["symbol"],
                    round(s.get("session_change_pct") or s["change_pct"], 2),
                )
                for s in strong
            ],
        )

        if max_cycles is not None and cycle >= max_cycles:
            logger.info("Reached max_cycles=%s — stopping watch", max_cycles)
            break

        # Sleep remaining interval
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        sleep_for = max(1.0, interval - elapsed)
        time.sleep(sleep_for)
