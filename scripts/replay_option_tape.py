#!/usr/bin/env python3
"""Replay paper fills on Tradier 1-minute option prints. No orders.

Does not read daily.json. Does not apply a delta proxy. Prints are not bids
or asks.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odte_scanner.backtest.option_tape import (  # noqa: E402
    coverage_report,
    metrics,
    prepare_trip,
    reconcile,
    run_variants,
)


def _window(rows, start: str, end: str):
    chosen = [row for row in rows if start <= row.day <= end and row.eligible]
    by_all = run_variants(chosen)
    # run_variants filters eligible again; pass only eligible so denominator matches
    packed = {}
    for name, trades in by_all.items():
        stats = metrics(trades)
        stats["eligible_denominator"] = len(chosen)
        stats["filled_of_eligible"] = f"{stats['trades']}/{len(chosen)}"
        desks = {}
        for desk in sorted({trade.desk for trade in trades}):
            cell = metrics([trade for trade in trades if trade.desk == desk])
            cell["rows_in_desk"] = sum(1 for trade in trades if trade.desk == desk)
            desks[desk] = cell
        packed[name] = {"all": stats, "by_desk": desks}
    return {"eligible": len(chosen), "variants": packed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Option-print replay. Read-only.")
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    snap = json.loads(args.snapshot.read_text(encoding="utf-8"))
    trips = [row for row in (snap.get("webull_book") or {}).get("trips") or [] if isinstance(row, dict)]
    seen: set[str] = set()
    unique = []
    for trip in trips:
        key = str(trip.get("order_id") or "") + "|" + str(trip.get("contract") or "") + "|" + str(trip.get("entered_at") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(trip)
    prepared = [prepare_trip(trip, args.bars) for trip in unique]
    recon = [reconcile(row) for row in prepared]
    arith_bad = [row for row in recon if abs(row["arithmetic_gap"]) > 0.05]
    with_entry = [row for row in recon if row["entry_minute"] is not None]
    entry_inside = [row for row in with_entry if row["entry_inside_print"] is True]
    exit_known = [row for row in recon if row["exit_minute_present"]]
    exit_inside = [row for row in exit_known if row["exit_inside_print"] is True]
    focus_rows = [row for row in prepared if "2026-10-31" <= row.day <= "2026-11-06"]
    report = {
        "source": "tradier_timesales_option_prints",
        "not_bid_ask": True,
        "daily_bars_used": False,
        "proxy_used": False,
        "slippage": {
            "stop": "min(stop, bar low). The low is a trade print, not a bid.",
            "target": "the limit, only after a print trades at or above it. Not an ask.",
            "flatten": "that minute's close print, not a bid.",
            "same_minute": "if low reaches the stop and high reaches the target, the stop fills.",
            "size": "original quantity. Skip when one lot risks more than $175. Never upsize.",
        },
        "generated_at": datetime.now().astimezone().isoformat(),
        "trips": len(prepared),
        "fill_arithmetic": {
            "note": "Recorded P&L versus (exit-entry)*100*qty. This does not use bars.",
            "checked": len(recon),
            "mismatch_over_5_cents": len(arith_bad),
            "examples": arith_bad[:8],
        },
        "fill_versus_print": {
            "note": "Whether the recorded price sits inside that minute's print high/low. Neighbor minutes are not used.",
            "entry_minute_present": len(with_entry),
            "entry_inside_print": len(entry_inside),
            "entry_outside_print": len(with_entry) - len(entry_inside),
            "exit_minute_present": len(exit_known),
            "exit_inside_print": len(exit_inside),
            "exit_outside_print": len(exit_known) - len(exit_inside),
        },
        "coverage": coverage_report(prepared),
        "excluded": [
            {
                "desk": row.desk,
                "symbol": row.symbol,
                "day": row.day,
                "contract": row.contract,
                "reason": row.exclude_reason,
            }
            for row in prepared
            if not row.eligible
        ],
        "focus": _window(focus_rows, "2026-10-31", "2026-11-06"),
        "full": _window(prepared, "0000-01-01", "9999-12-31"),
    }
    # Actual P&L on the eligible set, for the denominator. Not a modeled exit.
    for label, rows in (("focus", focus_rows), ("full", prepared)):
        elig = [row for row in rows if row.eligible]
        report[label]["actual_pnl_on_eligible"] = round(sum(row.actual_pnl for row in elig), 2)
        report[label]["actual_trades"] = len(elig)
    report["eligible_trades"] = [
        {
            "desk": row.desk,
            "symbol": row.symbol,
            "day": row.day,
            "contract": row.contract,
            "entry": row.entry,
            "exit": row.exit_px,
            "actual_pnl": row.actual_pnl,
            "qty": row.qty,
        }
        for row in prepared
        if row.eligible
    ]
    report["entry_outside_print"] = [
        {
            "desk": row["desk"],
            "symbol": row["symbol"],
            "day": row["day"],
            "contract": row["contract"],
            "entry": row["entry"],
            "entry_minute": row["entry_minute"],
        }
        for row in recon
        if row["entry_inside_print"] is False
    ]
    report["verdict"] = {
        "meets_20_session_gate": False,
        "sessions_in_archive": 16,
        "reason": (
            "The archive has 16 weekdays. That cannot meet a 20-session gate. "
            "Minute prints also cannot show that a staged or rejected live sell would have filled."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    cov = report["coverage"]
    print("trips", report["trips"])
    print("arithmetic mismatches", report["fill_arithmetic"]["mismatch_over_5_cents"])
    print("entry minute", report["fill_versus_print"])
    print("coverage full", cov["full"]["trips"], "eligible", cov["full"]["eligible"], cov["full"]["exclude_reasons"])
    print("coverage focus", cov["focus"]["trips"], "eligible", cov["focus"]["eligible"], cov["focus"]["by_desk"])
    for label in ("focus", "full"):
        print(f"\n== {label} eligible {report[label]['eligible']} actual pnl {report[label]['actual_pnl_on_eligible']} ==")
        for name, block in report[label]["variants"].items():
            stats = block["all"]
            print(
                f"{name:<22} {stats['filled_of_eligible']:>8} win {stats['win_rate']} "
                f"pnl {stats['pnl']} pf {stats['profit_factor']} dd {stats['max_drawdown']} "
                f"worst {stats['worst_day']} {stats['worst_day_pnl']}"
            )
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
