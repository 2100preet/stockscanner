#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

# Allow running as `python -m odte_scanner.cli` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from odte_scanner.backtest.runner import run_backtest
from odte_scanner.config import load_config
from odte_scanner.data.fetcher import fetch_history, fetch_many
from odte_scanner.scanner import run_scan
from odte_scanner.ui import run_ui
from odte_scanner.watch import run_watch

console = Console()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_scan(args: argparse.Namespace) -> int:
    report = run_scan(
        args.config,
        place_paper=not args.no_paper,
        universe_mode=getattr(args, "universe", None),
    )
    session = report.get("session_weekday", "?")
    scores = report.get("scores", [])
    table = Table(
        title=(
            f"Multi-horizon scan ({session}) — "
            f"{report.get('universe_mode', 'focus')} n={report.get('universe_size', '?')}"
        )
    )
    table.add_column("Symbol")
    table.add_column("Cal")
    table.add_column("Score", justify="right")
    table.add_column("Last", justify="right")
    table.add_column("ExpMove%", justify="right")
    table.add_column("Top reasons")
    for s in scores[:20]:
        reasons = s.get("reasons", [])
        cal = reasons[0] if reasons else "-"
        rest = [r for r in reasons[1:] if not r.startswith("calendar_")][:3]
        table.add_row(
            s["symbol"],
            cal,
            f"{s['ensemble_score']:.1f}",
            f"{s['last_price']:.2f}",
            f"{s['expected_move_pct']:.2f}",
            ", ".join(rest) or "-",
        )
    console.print(table)

    calls = report.get("call_candidates", [])
    if calls:
        ct = Table(title="Call Candidates (0DTE + up to 1 week)")
        ct.add_column("Bucket")
        ct.add_column("Symbol")
        ct.add_column("Expiry")
        ct.add_column("DTE", justify="right")
        ct.add_column("Strike", justify="right")
        ct.add_column("Ask", justify="right")
        ct.add_column("Score", justify="right")
        ct.add_column("Contract")
        for c in calls[:12]:
            ct.add_row(
                "0DTE" if c.get("dte_bucket") == "0dte" else "1W",
                c["symbol"],
                c["expiry"],
                str(c["dte"]),
                f"{c['strike']:.2f}",
                f"{c['ask']:.2f}",
                f"{c['score']:.1f}",
                c["contract"][:28],
            )
        console.print(ct)

    if report.get("paper_trades"):
        console.print("[green]Paper trades placed:[/green]")
        console.print_json(json.dumps(report["paper_trades"]))

    console.print(f"\n[dim]{report.get('disclaimer')}[/dim]")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from odte_scanner.backtest.win_rates import build_win_rate_table, summarize_hist_win_gate

    cfg = load_config(args.config)
    tickers = args.tickers.split(",") if args.tickers else cfg.get("tickers", [])
    bt = cfg.get("backtest", {})
    scan_cfg = cfg.get("scan", {})
    actions_cfg = cfg.get("actions") or {}
    weights = cfg.get("algos", {}).get("weights", {})
    period = "2y"
    histories = fetch_many(tickers, period=period)
    spy = fetch_history(cfg.get("regime", {}).get("spy", "SPY"), period=period)
    vix = fetch_history(cfg.get("regime", {}).get("vix", "^VIX"), period=period)

    results = run_backtest(
        histories,
        spy_df=spy,
        vix_df=vix,
        weights=weights,
        min_score=float(args.min_score or scan_cfg.get("min_score", 62)),
        option_payoff_multiplier=float(bt.get("option_payoff_multiplier", 3.0)),
        option_loss_fraction=float(bt.get("option_loss_fraction", 0.65)),
        start=args.start or bt.get("start"),
    )

    table = Table(title="Signal Backtest (next-day underlying proxy — ungated score filter)")
    table.add_column("Symbol")
    table.add_column("Trades", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("AvgRet%", justify="right")
    table.add_column("Hit≥1%", justify="right")
    table.add_column("Hit≥2%", justify="right")
    table.add_column("Opt E[R]", justify="right")
    for r in results:
        d = r.to_dict()
        table.add_row(
            d["symbol"],
            str(d["trades"]),
            f"{d['win_rate']*100:.1f}",
            f"{d['avg_next_day_ret_pct']:.2f}",
            f"{d['hit_rate_1pct']*100:.1f}",
            f"{d['hit_rate_2pct']*100:.1f}",
            f"{d['expectancy_option_R']:.2f}",
        )
    console.print(table)

    min_win = float(args.min_hist_win or bt.get("min_hist_win_pct") or actions_cfg.get("min_hist_win_pct", 80))
    min_n = int(args.min_hist_n or bt.get("min_hist_win_samples") or actions_cfg.get("min_hist_win_samples", 5))
    console.print(f"\n[bold]Building quality walk-forward win table for ≥{min_win:.0f}% gate (n≥{min_n})…[/bold]")
    win_table = build_win_rate_table(list(tickers), config_path=args.config, force=bool(args.force_win_rates))
    gate = summarize_hist_win_gate(win_table, min_hist_win_pct=min_win, min_hist_win_samples=min_n)

    gt = Table(title=f"High-conviction gate — hist win ≥{min_win:.0f}% · n≥{min_n}")
    gt.add_column("Symbol")
    gt.add_column("Horizon")
    gt.add_column("Win%", justify="right")
    gt.add_column("n", justify="right")
    gt.add_column("Hit≥1%", justify="right")
    for row in gate.get("eligible") or []:
        gt.add_row(
            row["symbol"],
            row["horizon"],
            f"{row['win_pct']:.1f}",
            str(row["trades"]),
            "—" if row.get("hit_1pct") is None else f"{row['hit_1pct']:.1f}",
        )
    console.print(gt)
    console.print(
        f"Eligible pooled win [bold green]{gate.get('pooled_win_pct')}%[/bold green] "
        f"on n={gate.get('pooled_trades')} · ungated quality pool "
        f"{gate.get('ungated_pooled_win_pct')}% on n={gate.get('ungated_pooled_trades')} · "
        f"target_met={gate.get('target_met')}"
    )
    console.print(f"[dim]{gate.get('note')}[/dim]")

    out = Path("outputs/backtest_latest.json")
    out.parent.mkdir(exist_ok=True)
    payload = {
        "ungated_score_filter": [r.to_dict() for r in results],
        "hist_win_gate": gate,
        "min_hist_win_pct": min_win,
        "min_hist_win_samples": min_n,
        "tickers": list(tickers),
        "ticker_count": len(tickers),
    }
    out.write_text(json.dumps(payload, indent=2))
    console.print(f"Wrote {out}")
    # Non-zero exit if gate cannot meet target (so CI / operators notice)
    if args.require_target and not gate.get("target_met"):
        console.print("[red]Hist-win gate target not met.[/red]")
        return 2
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = Path(cfg.get("paper_trading", {}).get("ledger_path", "outputs/paper_ledger.json"))
    if not path.exists():
        console.print("[yellow]No paper ledger yet. Run scan first.[/yellow]")
        return 1
    console.print_json(path.read_text())
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Run continuous 24/5-style scanner (extended-hours quotes + periodic full scans)."""
    console.print(
        "[cyan]Starting 24/5 watch[/cyan] — not every second (rate limits). "
        f"Interval ≈ {args.interval or 60}s. Ctrl+C to stop."
    )
    try:
        run_watch(
            args.config,
            interval_sec=args.interval,
            max_cycles=args.cycles,
            place_paper=args.paper,
            tickers_filter=args.tickers.split(",") if args.tickers else None,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Watch stopped.[/yellow]")
        return 0
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    console.print(f"[green]Signal Desk UI[/green] → http://127.0.0.1:{args.port}")
    run_ui(host=args.host, port=args.port, config_path=args.config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odte-scanner",
        description="Scan liquid tickers for bullish 0DTE/1DTE call setups (1–2% up days).",
    )
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="Run multi-horizon scan and optional paper buys")
    s.add_argument("--no-paper", action="store_true", help="Score only; do not paper trade")
    s.add_argument(
        "--universe",
        choices=["focus", "liquid", "screener", "all"],
        default=None,
        help="focus=options list; liquid/screener≈S&P100+; all=union",
    )
    s.set_defaults(func=cmd_scan)

    w = sub.add_parser(
        "watch",
        help="Continuous 24/5 scanner with extended-hours quotes (premarket/overnight)",
    )
    w.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Seconds between cycles (min 15, default from config ≈ 60)",
    )
    w.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Stop after N cycles (default: run forever)",
    )
    w.add_argument(
        "--paper",
        action="store_true",
        help="Allow paper trades during watch (off by default)",
    )
    w.add_argument("--tickers", default=None, help="Comma-separated subset, e.g. MU,NVDA,SPY")
    w.set_defaults(func=cmd_watch)

    u = sub.add_parser("ui", help="Open local web dashboard for scores, strikes, and movers")
    u.add_argument("--host", default="0.0.0.0")
    u.add_argument("--port", type=int, default=8787)
    u.set_defaults(func=cmd_ui)

    b = sub.add_parser("backtest", help="Walk-forward backtest of ensemble signals")
    b.add_argument("--tickers", default=None, help="Comma-separated override list")
    b.add_argument("--start", default=None)
    b.add_argument("--min-score", type=float, default=None)
    b.add_argument("--min-hist-win", type=float, default=None, help="High-conviction hist win%% gate (default 80)")
    b.add_argument("--min-hist-n", type=int, default=None, help="Min samples for hist-win gate (default 5)")
    b.add_argument("--force-win-rates", action="store_true", help="Rebuild win-rate cache")
    b.add_argument(
        "--require-target",
        action="store_true",
        help="Exit 2 if eligible pooled hist win is below the target",
    )
    b.set_defaults(func=cmd_backtest)

    l = sub.add_parser("ledger", help="Show paper trading ledger")
    l.set_defaults(func=cmd_ledger)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging("DEBUG" if args.verbose else "INFO")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
