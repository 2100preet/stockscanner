#!/usr/bin/env python3
"""Replay the Challenge sleeve on a tradier_bars directory and write the results.

    python scripts/backtest_challenge.py /path/to/tradier_bars

The directory is the extracted archive (coverage.csv, options/, underlying/).
Results go to analysis/challenge_backtest.json next to the repo root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odte_scanner.challenge.replay import run_directory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the Challenge sleeve on Tradier minute bars.")
    parser.add_argument(
        "bars",
        nargs="?",
        default="",
        help="Path to the extracted tradier_bars directory",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "analysis" / "challenge_backtest.json"),
        help="Where to write the results JSON",
    )
    args = parser.parse_args()
    raw = args.bars or __import__("os").environ.get("TRADIER_BARS_DIR") or ""
    root = Path(raw).expanduser()
    if not raw or not (root / "coverage.csv").exists():
        print("Pass the extracted tradier_bars directory (the one that contains coverage.csv).", file=sys.stderr)
        return 2
    result = run_directory(root)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name in ("sleeve", "webull"):
        book = result[name]
        print(
            f"{name}: trades={book['trades']} win%={book['win_rate_pct']} "
            f"pnl={book['pnl_usd']} pf={book['profit_factor']} "
            f"dd={book['max_drawdown_usd']} equity={book['start_equity']}→{book['end_equity']}"
        )
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
