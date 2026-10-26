"""Record Gold Setup and Golden Flow signals and publish the folded scorecard."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.scorecard.evaluate import evaluate_outcome, outcome_fingerprint
from odte_scanner.scorecard.ledger import (
    EVAL_EVENT,
    NOTE_EVENT,
    SIGNAL_EVENT,
    ChainStatus,
    append_event,
    extend_file,
    is_hash_prefix,
    load_events,
    signal_payloads,
    verify_chain,
)
from odte_scanner.scorecard.signals import GOLD_SETUP, GOLDEN_FLOW, extract_signals

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Hypothetical model-signal results, not actual trades and not real-money returns. "
    "Webull paper or sandbox activity is not shown here and is not real performance. "
    "Past performance does not guarantee future results. This is not investment advice."
)

MOVE_NOTE = (
    "Each figure is the underlying price move in the signal's direction from the reference "
    "price at signal time. End of day is that session's close. 1 day and 5 days are the next "
    "one and five trading sessions. Max favorable and max adverse use the session high and low "
    "over that window; the signal-day extreme can include prints from before the signal. "
    "A win is a directional move above zero. Losses stay on the card. "
    "These figures are not option fills, not account profit, and not real-money returns."
)

SPX_NOTE = (
    "SPX 0DTE structures are paper marks in dollars per one-lot, graded on their own. "
    "They are not part of the Gold Setup or Golden Flow win rate, not broker fills, "
    "and not Webull orders."
)

FetchBars = Callable[[str], list[dict[str, Any]]]


def runtime_ledger_path() -> Path:
    env = (os.environ.get("SCORECARD_LEDGER") or "").strip()
    if env:
        return Path(env)
    return ROOT / "outputs" / "scorecard_ledger.jsonl"


def git_ledger_path() -> Path:
    env = (os.environ.get("SCORECARD_GIT_LEDGER") or "").strip()
    if env:
        return Path(env)
    return ROOT / "scorecard" / "ledger.jsonl"


def _mirror_git() -> bool:
    # An explicit single-file ledger (tests, or a host override) is not mirrored.
    if (os.environ.get("SCORECARD_LEDGER") or "").strip():
        return False
    flag = (os.environ.get("SCORECARD_MIRROR_GIT") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _eval_enabled() -> bool:
    flag = (os.environ.get("SCORECARD_EVAL") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _eval_interval_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("SCORECARD_EVAL_INTERVAL_SEC") or 900))
    except ValueError:
        return 900.0


def _stamp_path() -> Path:
    env = (os.environ.get("SCORECARD_EVAL_STAMP") or "").strip()
    if env:
        return Path(env)
    return runtime_ledger_path().with_name("scorecard_eval_stamp")


def _due_for_eval(force: bool) -> bool:
    if not _eval_enabled():
        return False
    if force:
        return True
    stamp = _stamp_path()
    if not stamp.exists():
        return True
    try:
        previous = float(stamp.read_text().strip() or "0")
    except (OSError, ValueError):
        return True
    return (datetime.now(timezone.utc).timestamp() - previous) >= _eval_interval_sec()


def _touch_stamp() -> None:
    stamp = _stamp_path()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(datetime.now(timezone.utc).timestamp()))
    except OSError:
        return


def _load_pair() -> tuple[list[dict[str, Any]], ChainStatus, str | None]:
    """Choose the canonical chain.

    The committed git ledger wins when both files exist. A valid longer chain
    that extends it is used. A broken git ledger is left in place so the break
    stays visible, and nothing new is appended.
    """
    git_path = git_ledger_path()
    run_path = runtime_ledger_path()
    mirror = _mirror_git()
    if not mirror:
        events, err = load_events(run_path)
        if err:
            status = verify_chain(events)
            return events, ChainStatus(False, status.entries, None, status.broken_at or 1, err), err
        return events, verify_chain(events), None

    git_events, git_err = load_events(git_path)
    run_events, run_err = load_events(run_path)
    if git_err:
        status = verify_chain(git_events)
        return git_events, ChainStatus(False, status.entries, None, status.broken_at or 1, git_err), git_err
    git_status = verify_chain(git_events)
    if git_path.exists() and git_events and not git_status.ok:
        return git_events, git_status, git_status.error

    if run_err or not verify_chain(run_events).ok:
        run_events = []

    if is_hash_prefix(git_events, run_events):
        return run_events, verify_chain(run_events), None
    if is_hash_prefix(run_events, git_events):
        return git_events, git_status, None

    # Fork: keep the public chain and append any signals that never landed on it.
    known = {str(row.get("id")) for row in signal_payloads(git_events)}
    events = list(git_events)
    appended = 0
    for payload in signal_payloads(run_events):
        if str(payload.get("id")) in known:
            continue
        append_event(events, SIGNAL_EVENT, payload)
        known.add(str(payload.get("id")))
        appended += 1
    if appended:
        append_event(
            events,
            NOTE_EVENT,
            {
                "v": 1,
                "kind": "fork_merge",
                "appended": appended,
                "detail": (
                    "A second ledger diverged. Missing signals were appended onto the "
                    "public chain. Earlier public lines were not rewritten."
                ),
            },
        )
    return events, verify_chain(events), None


def _disk_is_clean(events: list[dict[str, Any]], err: str | None) -> bool:
    if err:
        return False
    if events and not verify_chain(events).ok:
        return False
    return True


def _persist(after: list[dict[str, Any]]) -> None:
    """Append new canonical lines. A valid public file is never rewritten."""
    if not verify_chain(after).ok:
        return
    if not _mirror_git():
        path = runtime_ledger_path()
        disk, err = load_events(path)
        if not _disk_is_clean(disk, err) or not is_hash_prefix(disk, after):
            return
        extend_file(path, after[len(disk) :])
        return

    git_path = git_ledger_path()
    git_events, git_err = load_events(git_path)
    if not _disk_is_clean(git_events, git_err) or not is_hash_prefix(git_events, after):
        return
    extend_file(git_path, after[len(git_events) :])

    run_path = runtime_ledger_path()
    run_events, run_err = load_events(run_path)
    if run_err:
        return
    if is_hash_prefix(run_events, after):
        extend_file(run_path, after[len(run_events) :])
        return
    if verify_chain(run_events).ok:
        # Volume copy diverged. Point it at the public chain so the next signal
        # extends one ledger. The git file itself was only appended.
        _replace_text(run_path, "".join(_line(event) for event in after))


def _line(event: dict[str, Any]) -> str:
    from odte_scanner.scorecard.ledger import canonical

    return canonical(event) + "\n"


def _replace_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _latest_outcomes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") != EVAL_EVENT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        sid = str(payload.get("signal_id") or "")
        outcome = payload.get("outcome")
        if sid and isinstance(outcome, dict):
            out[sid] = outcome
    return out


def _needs_eval(outcome: dict[str, Any] | None) -> bool:
    if not outcome:
        return True
    for key in ("eod", "d1", "d5"):
        horizon = outcome.get(key) or {}
        if horizon.get("status") != "settled":
            return True
    return False


def _default_fetch(symbol: str) -> list[dict[str, Any]]:
    from odte_scanner.data.fetcher import fetch_history
    from odte_scanner.json_util import finite_float

    aliases = {"SPX": "^SPX", "XSP": "^XSP"}
    yahoo = aliases.get(symbol.upper(), symbol)
    frame = fetch_history(
        symbol,
        period="6mo",
        interval="1d",
        use_cache=True,
        cache_max_age_hours=1,
        yahoo_symbol=yahoo,
    )
    if frame is None or getattr(frame, "empty", True):
        return []
    bars: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        text = str(idx)[:10]
        close = finite_float(row.get("Close") if hasattr(row, "get") else None)
        if close is None:
            continue
        bars.append(
            {
                "date": text,
                "open": finite_float(row.get("Open")),
                "high": finite_float(row.get("High")),
                "low": finite_float(row.get("Low")),
                "close": close,
            }
        )
    bars.sort(key=lambda bar: bar["date"])
    return bars


def _append_evaluations(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    fetch_bars: FetchBars,
) -> None:
    latest = _latest_outcomes(events)
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for payload in signal_payloads(events):
        if str(payload.get("reference_basis") or "") == "structure":
            continue
        if _needs_eval(latest.get(str(payload.get("id")))):
            by_symbol.setdefault(str(payload.get("ticker") or ""), []).append(payload)
    cache: dict[str, list[dict[str, Any]] | None] = {}
    as_of = now.astimezone(timezone.utc).isoformat()
    for ticker, rows in by_symbol.items():
        if ticker not in cache:
            try:
                cache[ticker] = fetch_bars(ticker) if ticker else []
            except Exception:  # noqa: BLE001
                cache[ticker] = None
        bars = cache[ticker]
        if bars is None:
            continue
        for payload in rows:
            outcome = evaluate_outcome(payload, bars, now=now)
            previous = latest.get(str(payload.get("id")))
            if previous and outcome_fingerprint(previous) == outcome_fingerprint(outcome):
                continue
            still_pending = all(
                (outcome.get(key) or {}).get("status") == "pending" for key in ("eod", "d1", "d5")
            ) and outcome.get("mfe_pct") is None
            if previous is None and still_pending:
                continue
            append_event(
                events,
                EVAL_EVENT,
                {"v": 1, "signal_id": payload["id"], "as_of": as_of, "outcome": outcome},
            )


def _horizon_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    wins = 0
    losses = 0
    pending = 0
    moves: list[float] = []
    for row in rows:
        horizon = ((row.get("outcomes") or {}).get(key) or {})
        if horizon.get("status") != "settled":
            pending += 1
            continue
        move = horizon.get("return_pct")
        if horizon.get("win") is True:
            wins += 1
        else:
            losses += 1
        if isinstance(move, (int, float)):
            moves.append(float(move))
    settled = wins + losses
    return {
        "settled": settled,
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / settled) if settled else None,
        "avg_move_pct": (sum(moves) / len(moves)) if moves else None,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(group: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(group),
            "horizons": {key: _horizon_stats(group, key) for key in ("eod", "d1", "d5")},
        }

    by_type = {
        GOLD_SETUP: sum(1 for row in rows if row.get("signal_type") == GOLD_SETUP),
        GOLDEN_FLOW: sum(1 for row in rows if row.get("signal_type") == GOLDEN_FLOW),
    }
    return {
        "count": len(rows),
        "by_type": by_type,
        "horizons": block(rows)["horizons"],
        "by_type_detail": {
            GOLD_SETUP: block([row for row in rows if row.get("signal_type") == GOLD_SETUP]),
            GOLDEN_FLOW: block([row for row in rows if row.get("signal_type") == GOLDEN_FLOW]),
        },
    }


def _structure_fingerprint(outcome: dict[str, Any]) -> str:
    from odte_scanner.scorecard.ledger import canonical

    pnl = outcome.get("pnl")
    if isinstance(pnl, (int, float)):
        pnl = round(float(pnl), 2)
    return canonical(
        {
            "status": outcome.get("status"),
            "pnl": pnl,
            "exit_reason": outcome.get("exit_reason"),
            "win": outcome.get("win"),
        }
    )


def _spx_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("signal_type") or "SPX")
        bucket = by.setdefault(
            name,
            {"count": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        )
        bucket["count"] += 1
        outcome = row.get("outcomes") or {}
        pnl = outcome.get("pnl")
        if outcome.get("status") == "closed":
            bucket["closed"] += 1
            if outcome.get("win") is True:
                bucket["wins"] += 1
            elif outcome.get("win") is False:
                bucket["losses"] += 1
        else:
            bucket["open"] += 1
        if isinstance(pnl, (int, float)):
            bucket["pnl"] = round(float(bucket["pnl"]) + float(pnl), 2)
    return {"note": SPX_NOTE, "count": len(rows), "by_strategy": by, "rows": rows}


def fold(events: list[dict[str, Any]], status: ChainStatus | None = None) -> dict[str, Any]:
    chain = (status or verify_chain(events)).to_dict()
    outcomes = _latest_outcomes(events)
    rows: list[dict[str, Any]] = []
    spx_rows: list[dict[str, Any]] = []
    for payload in signal_payloads(events):
        outcome = outcomes.get(str(payload.get("id"))) or {
            "eod": {"status": "pending", "return_pct": None, "win": None},
            "d1": {"status": "pending", "return_pct": None, "win": None},
            "d5": {"status": "pending", "return_pct": None, "win": None},
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_status": "pending",
        }
        card = {
            "id": payload.get("id"),
            "signaled_at": payload.get("signaled_at"),
            "signaled_at_ct": payload.get("signaled_at_ct"),
            "recorded_at": payload.get("recorded_at"),
            "ticker": payload.get("ticker"),
            "signal_type": payload.get("signal_type"),
            "strategy": payload.get("strategy"),
            "direction": payload.get("direction"),
            "reference_entry": payload.get("reference_entry"),
            "reference_basis": payload.get("reference_basis") or "underlying",
            "premium_reference": payload.get("premium_reference"),
            "contract": payload.get("contract"),
            "conviction": payload.get("conviction"),
            "max_loss": payload.get("max_loss"),
            "outcomes": outcome,
        }
        if card["reference_basis"] == "structure":
            spx_rows.append(card)
        else:
            rows.append(card)
    rows.sort(key=lambda row: (str(row.get("signaled_at") or ""), str(row.get("id") or "")), reverse=True)
    spx_rows.sort(key=lambda row: (str(row.get("signaled_at") or ""), str(row.get("id") or "")), reverse=True)
    head = chain.get("head") or ""
    return {
        "disclaimer": DISCLAIMER,
        "move_note": MOVE_NOTE,
        "chain": chain,
        "audit": {
            "method": "sha256 hash chain over append-only ledger lines",
            "ledger_path": "scorecard/ledger.jsonl",
            "git": "The public scan commits scorecard/ledger.jsonl so history is auditable.",
            "ledger_url": "https://github.com/2100preet/stockscanner/blob/main/scorecard/ledger.jsonl",
            "head": head,
            "head_short": head[:12] if head else None,
        },
        "summary": _summary(rows),
        "rows": rows,
        "spx_strategies": _spx_summary(spx_rows),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def public_view() -> dict[str, Any]:
    events, status, _err = _load_pair()
    return fold(events, status)


def ledger_text() -> str:
    """Raw public ledger bytes. A broken chain is returned as stored so the edit stays visible."""
    events, status, err = _load_pair()
    if err or not status.ok:
        path = git_ledger_path() if _mirror_git() and git_ledger_path().exists() else runtime_ledger_path()
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return "".join(_line(event) for event in events)


def _append_spx_board(events: list[dict[str, Any]], board: dict[str, Any] | None, now: datetime) -> None:
    if not isinstance(board, dict):
        return
    known = {str(row.get("id")) for row in signal_payloads(events)}
    for payload in board.get("ledger_signals") or []:
        if not isinstance(payload, dict) or not payload.get("id"):
            continue
        if str(payload.get("reference_basis") or "") != "structure":
            continue
        if str(payload["id"]) in known:
            continue
        append_event(events, SIGNAL_EVENT, payload)
        known.add(str(payload["id"]))
    latest = _latest_outcomes(events)
    as_of = now.astimezone(timezone.utc).isoformat()
    for mark in board.get("ledger_marks") or []:
        if not isinstance(mark, dict):
            continue
        sid = str(mark.get("signal_id") or "")
        outcome = mark.get("outcome")
        if not sid or not isinstance(outcome, dict) or sid not in known:
            continue
        previous = latest.get(sid)
        if previous and _structure_fingerprint(previous) == _structure_fingerprint(outcome):
            continue
        append_event(
            events,
            EVAL_EVENT,
            {"v": 1, "signal_id": sid, "as_of": as_of, "outcome": outcome},
        )
        latest[sid] = outcome


def observe_snapshot(
    *,
    sure_winners: Any = None,
    option_flow: Any = None,
    scores: Any = None,
    spx_board: dict[str, Any] | None = None,
    now: datetime | None = None,
    fetch_bars: FetchBars | None = None,
    force_eval: bool = False,
) -> dict[str, Any]:
    """Append newly fired signals, then evaluate due outcomes.

    A broken chain is not extended. Webull routing is not involved.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    events, status, err = _load_pair()
    if err or not status.ok:
        view = fold(events, status if not err else ChainStatus(False, status.entries, None, status.broken_at, err or status.error))
        return view

    incoming = extract_signals(
        sure_winners=sure_winners,
        option_flow=option_flow,
        scores=scores,
        now=moment,
    )
    known = {str(row.get("id")) for row in signal_payloads(events)}
    for payload in incoming:
        if str(payload.get("id")) in known:
            continue
        append_event(events, SIGNAL_EVENT, payload)
        known.add(str(payload.get("id")))
        logger.info(
            "scorecard recorded %s %s %s",
            payload.get("signal_type"),
            payload.get("ticker"),
            payload.get("direction"),
        )

    evaluate = fetch_bars is not None or force_eval or _due_for_eval(False)
    if evaluate and _eval_enabled():
        _append_evaluations(events, now=moment, fetch_bars=fetch_bars or _default_fetch)
        _touch_stamp()
    _append_spx_board(events, spx_board, moment)
    _persist(events)
    return fold(events, verify_chain(events))
