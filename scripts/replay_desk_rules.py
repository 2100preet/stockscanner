#!/usr/bin/env python3
"""Replay desk rules on public signals. Does not place or change any order.

Example:
  python3 scripts/replay_desk_rules.py \\
    --ledger /tmp/ledger.jsonl --snapshot /tmp/snapshot.json \\
    --out outputs/rule_replay.json

With no paths, the script GETs the public read-only ledger and snapshot.
Premiums are a 0.50-delta proxy on Yahoo underlying bars, not option prints.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odte_scanner.backtest.rule_load import (  # noqa: E402
    load_ledger_signals,
    load_snapshot_fills,
    load_underlying_bars,
    merge_ideas,
    routed_symbols,
)
from odte_scanner.backtest.rule_replay import ROUTED, realized_size_screen, summarize  # noqa: E402

LEDGER_URL = "https://stockscanner.fly.dev/scorecard/ledger.jsonl"
SNAPSHOT_URL = "https://stockscanner.fly.dev/api/snapshot"
GATES = {
    "sessions": 20,
    "profit_factor": 1.3,
    "win_rate": None,
}


def _fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 — public read-only GET
        dest.write_bytes(response.read())
    return dest


def _print_window(name: str, window: dict) -> None:
    print(f"\n== {name}  ideas={window.get('ideas')} ==")
    header = (
        f"{'variant':<22} {'trades':>6} {'win%':>6} {'pnl':>10} "
        f"{'avgW':>8} {'avgL':>8} {'PF':>6} {'maxDD':>10} {'worst day':<12}"
    )
    print(header)
    for variant, stats in window.get("variants", {}).items():
        win = stats.get("win_rate")
        win_s = "  n/a" if win is None else f"{win * 100:5.1f}%"
        pf = stats.get("profit_factor")
        pf_s = "  n/a" if pf is None else f"{pf:6.2f}"
        worst = stats.get("worst_day") or "—"
        print(
            f"{variant:<22} {stats.get('trades', 0):6} {win_s:>6} "
            f"{stats.get('pnl', 0):10.0f} {str(stats.get('avg_win')):>8} "
            f"{str(stats.get('avg_loss')):>8} {pf_s:>6} {stats.get('max_drawdown', 0):10.0f} "
            f"{worst:<12} {stats.get('worst_day_pnl')}"
        )
    print("-- by desk, baseline vs proposed --")
    for variant in ("baseline", "proposed"):
        desks = window.get("by_desk", {}).get(variant, {})
        for desk in sorted(desks):
            if desk not in ROUTED:
                continue
            stats = desks[desk]
            print(
                f"  {variant:<16} {desk:<16} trades={stats.get('trades')} "
                f"pnl={stats.get('pnl')} pf={stats.get('profit_factor')} "
                f"maxDD={stats.get('max_drawdown')}"
            )


def _verdict(report: dict) -> dict:
    focus = report["windows"]["focus"]["variants"].get("proposed") or {}
    full = report["windows"]["full"]["variants"].get("proposed") or {}
    sessions = len({row["day"] for row in report["windows"]["full"].get("trades", {}).get("proposed", []) if row.get("filled")})
    pf = full.get("profit_factor")
    reasons = []
    if sessions < GATES["sessions"]:
        reasons.append(f"{sessions} sessions with a fill, gate is {GATES['sessions']}")
    if pf is None or pf < GATES["profit_factor"]:
        reasons.append(f"full-sample profit factor {pf} is under {GATES['profit_factor']}")
    sanity = report.get("sanity") or {}
    reasons.append(
        f"delta-proxy exits on the same real fills disagree with realized P&L "
        f"by ${sanity.get('error')} ({sanity.get('matched_fills')} fills)"
    )
    reasons.append("premium path is a 0.50-delta proxy, not 1-minute option bars")
    reasons.append("Oct 16–27 paper fills are not in the public order file, so the sanity check cannot cover them")
    return {
        "meets_go_live_gates": False,
        "reasons": reasons,
        "focus_proposed_pnl": focus.get("pnl"),
        "full_proposed_pnl": full.get("pnl"),
        "focus_baseline_pnl": (report["windows"]["focus"]["variants"].get("baseline") or {}).get("pnl"),
        "full_baseline_pnl": (report["windows"]["full"]["variants"].get("baseline") or {}).get("pnl"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only replay of desk entry and exit rules.")
    parser.add_argument("--ledger", type=Path, help="ledger.jsonl path. Fetched if omitted.")
    parser.add_argument("--snapshot", type=Path, help="snapshot JSON path. Fetched if omitted.")
    parser.add_argument("--cache", type=Path, default=Path("outputs/rule_replay_bars.json"))
    parser.add_argument("--out", type=Path, default=Path("outputs/rule_replay.json"))
    parser.add_argument("--start", default="2026-10-16")
    parser.add_argument("--end", default="2026-11-06")
    args = parser.parse_args()

    cache_dir = Path("outputs")
    cache_dir.mkdir(parents=True, exist_ok=True)
    ledger = args.ledger or _fetch(LEDGER_URL, cache_dir / "ledger.jsonl")
    snapshot = args.snapshot or _fetch(SNAPSHOT_URL, cache_dir / "snapshot.json")

    ledger_ideas = load_ledger_signals(ledger, start=args.start, end=args.end)
    order_ideas, pnl = load_snapshot_fills(snapshot, start=args.start, end=args.end)
    ideas = merge_ideas(ledger_ideas, order_ideas, pnl)
    symbols = routed_symbols(ideas)
    print(
        f"ideas={len(ideas)} ledger={len(ledger_ideas)} orders={len(order_ideas)} "
        f"routed_symbols={len(symbols)} closed_trips={len(pnl)}"
    )
    bars = load_underlying_bars(symbols, start=args.start, end=args.end, cache=args.cache)
    covered = sum(1 for symbol in symbols if bars.get(symbol))
    print(f"underlying series={covered} of {len(symbols)}")

    report = summarize(ideas, bars)
    report["proxy"] = {
        "option_bars": False,
        "reason": "TRADIER_ACCESS_TOKEN and POLYGON_API_KEY are not in this environment.",
        "model": (
            "Yahoo underlying bars, 1-minute on 2026-11-02 through 2026-11-06 and "
            "2-minute from 2026-10-16 through 2026-11-01. Option premium = anchor "
            "+ 0.50 delta times the underlying move, floored at $0.01. "
            "A 4% half-spread is the bid/ask. This is not an option print."
        ),
        "symbols_with_bars": covered,
        "symbols": symbols,
    }
    report["universe"] = {
        "ledger_signals": len(ledger_ideas),
        "filled_buys": len(order_ideas),
        "merged_ideas": len(ideas),
        "by_desk": {},
    }
    for idea in ideas:
        cell = report["universe"]["by_desk"].setdefault(idea.desk, {"ideas": 0, "with_actual_pnl": 0})
        cell["ideas"] += 1
        if idea.actual_pnl is not None:
            cell["with_actual_pnl"] += 1
    book = json.loads(snapshot.read_text(encoding="utf-8")).get("webull_book") or {}
    trips = [row for row in (book.get("trips") or []) if isinstance(row, dict)]
    chicago = ZoneInfo("America/Chicago")

    def _on(day_key: str, start: str, end: str) -> list[dict]:
        kept = []
        for trip in trips:
            raw = str(trip.get(day_key) or "")
            try:
                moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=chicago)
            day = moment.astimezone(chicago).date().isoformat()
            if start <= day <= end:
                kept.append(trip)
        return kept

    report["realized_size_screen"] = {
        "focus_entry_day": realized_size_screen(_on("entered_at", "2026-10-31", "2026-11-06")),
        "order_file_exit_day": realized_size_screen(_on("exited_at", "2026-10-16", "2026-11-06")),
        "missing": (
            "The public order file starts 2026-10-28. Oct 16–27, including the "
            "Gold Setup −$2,841 day, are not in these fills."
        ),
    }
    report["verdict"] = _verdict(report)
    # Trade lists are useful and large. Keep them in the file, not on stdout.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _print_window("focus Oct 31–Nov 6", report["windows"]["focus"])
    _print_window("full Oct 16–Nov 6", report["windows"]["full"])
    print("\nSANITY", json.dumps(report["sanity"]))
    print("VERDICT", json.dumps(report["verdict"], indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
