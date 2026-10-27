"""Per-desk exit rules from the October 2026 exit study.

The study replayed logged entries on Yahoo 1-minute option bars. These defaults
are what that replay found, and ``config.yaml`` can override any desk.

``premium`` sells on the take-profit / stop-loss percents.
``eod`` does not take those premium exits. The 15:45 ET clock for a same-session
bucket still runs in ``decide_exit`` — the study's gain on that bucket came from
not stopping at −50%, not from holding through the close.

``scale_out`` is the paper ladder for a lot that was opened with enough
contracts. Smaller lots and same-day expiry stay on the full TP/SL exit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScaleStep:
    """Sell ``sell_pct`` of the original contract count once premium is up ``at_pct``."""

    at_pct: float
    sell_pct: float


@dataclass(frozen=True)
class ScaleOut:
    """Partial exits for a paper lot. Fractions are of the original quantity.

    ``trail_giveback_pct`` is how far the premium may fall from its peak before
    the runner is sold. The trail arms once the peak reaches ``trail_after_pct``.
    The hard stop (flatten everything) stays on the parent ``ExitRule``.
    """

    min_contracts: int
    steps: tuple[ScaleStep, ...]
    trail_giveback_pct: float | None = None
    trail_after_pct: float | None = None


# Sure Winner: +40% sell 40%, +80% sell 40% of original N, trail the rest
# −25% from the peak. Challenge: +40% sell 50%, +100% sell 30%, trail −20%.
# RIP +50/−50 tickets: +30% sell 50%, +50% sell 30%. A lot smaller than
# min_contracts does not use the ladder.
SURE_WINNER_SCALE = ScaleOut(
    min_contracts=3,
    steps=(ScaleStep(40.0, 40.0), ScaleStep(80.0, 40.0)),
    trail_giveback_pct=25.0,
    trail_after_pct=80.0,
)
CHALLENGE_SCALE = ScaleOut(
    min_contracts=2,
    steps=(ScaleStep(40.0, 50.0), ScaleStep(100.0, 30.0)),
    trail_giveback_pct=20.0,
    trail_after_pct=100.0,
)
RIP_SCALE = ScaleOut(
    min_contracts=2,
    steps=(ScaleStep(30.0, 50.0), ScaleStep(50.0, 30.0)),
)


@dataclass(frozen=True)
class GoldExitSpec:
    """Sure Winner exits. The card text and the sell engine both read this.

    ``arm_pct`` is the +30% line: a 0–1 DTE lot sells in full, and a 2+ DTE
    lot sells half when the quantity can split. ``trail_arm_pct`` arms the
    one-lot / remaining-lot trail. Once the peak is there, the lot leaves if
    premium falls back to entry or gives back half the peak gain, whichever
    comes first. ``runner_target_pct`` is the optional full exit for whatever
    is still open. Same-day uses ``same_day_stop_pct``; everything else keeps
    ``stop_loss_pct``. ``flatten_ct`` is 2:30 PM America/Chicago and applies
    only when the contract expires today.
    """

    arm_pct: float = 30.0
    runner_target_pct: float = 80.0
    stop_loss_pct: float = 50.0
    same_day_stop_pct: float = 40.0
    partial_sell_pct: float = 50.0
    trail_arm_pct: float = 30.0
    trail_keep_fraction: float = 0.5
    flatten_ct: str = "14:30"


DEFAULT_GOLD_EXIT = GoldExitSpec()

# Sniper sells half at +25% when two contracts are open, then trails the rest
# back to entry or half the peak gain. A one-lot cannot split, so it trails
# the whole position once +25% prints. The hard stop is the same-day −40%,
# and the clock is 10:30 CT. The runner target is intentionally out of reach
# so the trail and the clock are the exits the 60-day sample used.
DEFAULT_SNIPER_EXIT = GoldExitSpec(
    arm_pct=25.0,
    runner_target_pct=1000.0,
    stop_loss_pct=40.0,
    same_day_stop_pct=40.0,
    partial_sell_pct=50.0,
    trail_arm_pct=25.0,
    trail_keep_fraction=0.5,
    flatten_ct="10:30",
)


@dataclass(frozen=True)
class ExitRule:
    mode: str  # premium | eod
    take_profit_pct: float
    stop_loss_pct: float
    scale_out: ScaleOut | None = None
    gold: GoldExitSpec | None = None


# Best same-sample rule per desk. Desks with no option path keep the prior
# +80% / −50% premium rule. See docs/exit_study.md.
# Sure Winner no longer uses the +40/+80 ladder. The gold spec above is the
# live rule for that book. Challenge and RIP keep their own ladders.
DEFAULT_EXIT_RULES: dict[str, ExitRule] = {
    "sure_winner": ExitRule("premium", 30.0, 50.0, None, DEFAULT_GOLD_EXIT),
    "odte": ExitRule("eod", 80.0, 50.0),
    "weekly": ExitRule("premium", 100.0, 50.0),
    "swing": ExitRule("premium", 80.0, 50.0),
    "lottery": ExitRule("premium", 80.0, 50.0),
    "challenge": ExitRule("premium", 100.0, 50.0, CHALLENGE_SCALE),
    "rip": ExitRule("premium", 30.0, 50.0, RIP_SCALE),
}


def _parse_scale_out(raw: Any, base: ScaleOut | None) -> ScaleOut | None:
    """Parse a config ``scale_out:`` block. ``false`` turns the ladder off."""
    if raw is False or raw is None:
        return None
    if not isinstance(raw, dict):
        return base
    steps_raw = raw.get("steps", None)
    if steps_raw is None and base is not None:
        steps = base.steps
    else:
        parsed: list[ScaleStep] = []
        for step in steps_raw or []:
            if not isinstance(step, dict):
                continue
            at = step.get("at_pct", step.get("trigger_pct"))
            sell = step.get("sell_pct", step.get("fraction_pct"))
            if at is None or sell is None:
                continue
            parsed.append(ScaleStep(float(at), float(sell)))
        steps = tuple(sorted(parsed, key=lambda step: step.at_pct))
    min_n = int(raw.get("min_contracts", base.min_contracts if base else 2))
    giveback = raw.get("trail_giveback_pct", base.trail_giveback_pct if base else None)
    after = raw.get("trail_after_pct", base.trail_after_pct if base else None)
    return ScaleOut(
        min_contracts=max(1, min_n),
        steps=steps,
        trail_giveback_pct=None if giveback in (None, "") else float(giveback),
        trail_after_pct=None if after in (None, "") else float(after),
    )


def _gold_number(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if value in (None, ""):
        return float(default)
    return float(value)


def gold_spec_from_config(raw: dict[str, Any] | None, base: GoldExitSpec | None = None) -> GoldExitSpec:
    """Read the Sure Winner block. Missing keys keep ``base``."""
    spec = base or DEFAULT_GOLD_EXIT
    block = raw if isinstance(raw, dict) else {}
    flatten = str(block.get("flatten_ct") or spec.flatten_ct).strip() or spec.flatten_ct
    return GoldExitSpec(
        arm_pct=_gold_number(block, "take_profit_pct", spec.arm_pct),
        runner_target_pct=_gold_number(block, "runner_target_pct", spec.runner_target_pct),
        stop_loss_pct=_gold_number(block, "stop_loss_pct", spec.stop_loss_pct),
        same_day_stop_pct=_gold_number(block, "same_day_stop_pct", spec.same_day_stop_pct),
        partial_sell_pct=_gold_number(block, "partial_sell_pct", spec.partial_sell_pct),
        trail_arm_pct=_gold_number(block, "trail_arm_pct", spec.trail_arm_pct),
        trail_keep_fraction=_gold_number(block, "trail_keep_fraction", spec.trail_keep_fraction),
        flatten_ct=flatten,
    )


def load_exit_rules(raw: dict[str, Any] | None) -> dict[str, ExitRule]:
    """Overlay a config ``exits:`` map on the study defaults."""
    out = dict(DEFAULT_EXIT_RULES)
    for key, val in (raw or {}).items():
        if not isinstance(val, dict):
            continue
        name = str(key).strip().lower()
        base = out.get(name, ExitRule("premium", 80.0, 50.0))
        mode = str(val.get("mode") or base.mode).strip().lower()
        if mode not in {"premium", "eod"}:
            mode = base.mode
        scale = base.scale_out
        if "scale_out" in val:
            scale = _parse_scale_out(val.get("scale_out"), base.scale_out)
        gold = base.gold
        if name == "sure_winner" or gold is not None:
            gold = gold_spec_from_config(val, gold or DEFAULT_GOLD_EXIT)
        out[name] = ExitRule(
            mode=mode,
            take_profit_pct=float(val.get("take_profit_pct", base.take_profit_pct)),
            stop_loss_pct=float(val.get("stop_loss_pct", base.stop_loss_pct)),
            scale_out=scale,
            gold=gold,
        )
    return out


@dataclass(frozen=True)
class PremiumExit:
    """One paper exit slice. ``quantity`` 0 means hold and keep the peak."""

    quantity: int
    detail: str
    peak_mark: float | None = None
    trail_armed: bool = False
    new_rungs: tuple[int, ...] = ()


def whole_contracts(original: int, sell_pct: float) -> int:
    """Whole contracts for a rung. Round down; a fraction below 1 sells nothing."""
    if original <= 0 or sell_pct <= 0:
        return 0
    return int(float(original) * float(sell_pct) / 100.0 + 1e-9)


def plan_premium_exit(
    *,
    entry: float,
    mark: float,
    held: int,
    original: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    scale: ScaleOut | None,
    same_day: bool,
    rungs_done: set[int] | frozenset[int] | None = None,
    peak_mark: float | None = None,
) -> PremiumExit | None:
    """Full TP/SL, or a scale-out slice when the lot is large enough.

    A hard stop always sells everything still open. The ladder never sells a
    piece while premium is below the entry. A same-day lot, or one opened
    below ``min_contracts``, keeps the single full exit.
    """
    if entry <= 0 or mark <= 0 or held < 1:
        return None
    done = set(rungs_done or ())
    pct = (mark - entry) / entry * 100.0
    stop = entry * (1.0 - abs(float(stop_loss_pct)) / 100.0)
    if mark <= stop + 1e-4:
        return PremiumExit(
            held,
            f"stop loss {pct:+.0f}% ≤ −{abs(float(stop_loss_pct)):.0f}%",
        )

    use_scale = (
        scale is not None
        and not same_day
        and int(original) >= int(scale.min_contracts)
        and held >= 1
    )
    if not use_scale:
        target = entry * (1.0 + float(take_profit_pct) / 100.0)
        if mark + 1e-4 >= target:
            return PremiumExit(
                held,
                f"take profit {pct:+.0f}% ≥ +{float(take_profit_pct):.0f}%",
            )
        return None

    peak = float(mark if peak_mark is None else max(float(peak_mark), mark))
    trail_armed = False
    if scale.trail_after_pct is not None:
        arm_at = entry * (1.0 + float(scale.trail_after_pct) / 100.0)
        trail_armed = peak + 1e-4 >= arm_at

    qty = 0
    crossed: list[ScaleStep] = []
    new_rungs: list[int] = []
    # Partials only while the lot is still a winner. A loser waits for the stop.
    if mark + 1e-4 >= entry:
        for index, step in enumerate(scale.steps):
            if index in done or float(step.at_pct) <= 0:
                continue
            trigger = entry * (1.0 + float(step.at_pct) / 100.0)
            if mark + 1e-4 < trigger:
                continue
            piece = whole_contracts(int(original), step.sell_pct)
            if piece < 1:
                continue
            add = min(piece, held - qty)
            if add < 1:
                continue
            qty += add
            crossed.append(step)
            new_rungs.append(index)

    if qty >= 1:
        labels = "/".join(f"+{step.at_pct:.0f}%" for step in crossed)
        return PremiumExit(
            qty,
            f"scale out {labels} — sell {qty} of {original}",
            peak,
            trail_armed,
            tuple(new_rungs),
        )

    giveback = scale.trail_giveback_pct
    if (
        trail_armed
        and giveback is not None
        and float(giveback) > 0
        and mark < peak - 1e-4
    ):
        line = peak * (1.0 - float(giveback) / 100.0)
        if mark <= line + 1e-4:
            return PremiumExit(
                held,
                f"trail −{float(giveback):.0f}% from peak — sell runner {held}",
                peak,
                True,
                (),
            )
    return PremiumExit(0, "", peak, trail_armed, ())


def rule_for_bucket(rules: dict[str, ExitRule] | None, bucket: Any) -> ExitRule:
    """Map an action-board DTE bucket onto a desk rule."""
    table = rules or DEFAULT_EXIT_RULES
    text = str(bucket or "odte").lower()
    if "week" in text:
        key = "weekly"
    elif "swing" in text or "leap" in text:
        key = "swing"
    elif "lot" in text:
        key = "lottery"
    elif "challenge" in text:
        key = "challenge"
    else:
        key = "odte"
    return table.get(key, DEFAULT_EXIT_RULES["odte"])


def challenge_uses_tp100(rules: dict[str, ExitRule] | None) -> bool:
    rule = (rules or DEFAULT_EXIT_RULES).get("challenge", DEFAULT_EXIT_RULES["challenge"])
    return rule.mode == "premium" and float(rule.take_profit_pct) >= 100.0


def gold_exit_plan_text(
    spec: GoldExitSpec | None = None,
    *,
    dte: int | None = None,
    same_day: bool = False,
) -> str:
    """Card text for a Sure Winner. The percents are the gold spec, not a second copy."""
    rule = spec or DEFAULT_GOLD_EXIT
    short = bool(same_day or (dte is not None and int(dte) <= 1))
    trail = (
        f"trail after +{float(rule.trail_arm_pct):.0f}% "
        f"(back to entry, or give back half the peak gain)"
    )
    if short and same_day:
        return (
            f"EXIT plan (gold) · sell all at +{float(rule.arm_pct):.0f}% · "
            f"stop −{abs(float(rule.same_day_stop_pct)):.0f}% · "
            f"{trail} · flatten 2:30 PM CT"
        )
    if short:
        return (
            f"EXIT plan (gold) · sell all at +{float(rule.arm_pct):.0f}% · "
            f"stop −{abs(float(rule.stop_loss_pct)):.0f}% · {trail}"
        )
    return (
        f"EXIT plan (gold) · sell half at +{float(rule.arm_pct):.0f}% when qty allows · "
        f"runner +{float(rule.runner_target_pct):.0f}% · "
        f"stop −{abs(float(rule.stop_loss_pct)):.0f}% · {trail}"
    )


def plan_gold_exit(
    *,
    entry: float,
    mark: float,
    held: int,
    original: int,
    spec: GoldExitSpec | None = None,
    same_day: bool = False,
    short_dated: bool = False,
    half_done: bool = False,
    peak_mark: float | None = None,
) -> PremiumExit | None:
    """Sure Winner premium exit.

    A hard stop sells the contracts still open. 0–1 DTE sells the whole lot
    at the arm. 2+ DTE sells half at the arm when at least two contracts are
    open, and the rest can leave at the runner target. Once the peak has
    reached the trail arm, the remaining lot — including a 1-lot and a
    same-day lot whose +30% print was missed — leaves if premium is back at
    entry or has given back half the peak gain, whichever is hit first.
    """
    rule = spec or DEFAULT_GOLD_EXIT
    if entry <= 0 or mark <= 0 or held < 1:
        return None
    stop_pct = abs(float(rule.same_day_stop_pct if same_day else rule.stop_loss_pct))
    pct = (mark - entry) / entry * 100.0
    stop = entry * (1.0 - stop_pct / 100.0)
    if mark <= stop + 1e-4:
        return PremiumExit(held, f"stop loss {pct:+.0f}% ≤ −{stop_pct:.0f}%")

    peak = float(mark if peak_mark is None else max(float(peak_mark), mark))
    arm_pct = float(rule.trail_arm_pct)
    arm_at = entry * (1.0 + arm_pct / 100.0)
    trail_armed = peak + 1e-4 >= arm_at
    keep = min(1.0, max(0.0, float(rule.trail_keep_fraction)))
    # Half the peak gain kept means the exit is the midpoint on the way down.
    # Entry is the other line. The higher of the two is touched first.
    lock = entry + keep * (peak - entry)
    trail_line = max(entry, lock)

    target_pct = float(rule.arm_pct)
    if short_dated and mark + 1e-4 >= entry * (1.0 + target_pct / 100.0):
        return PremiumExit(
            held,
            f"take profit {pct:+.0f}% ≥ +{target_pct:.0f}%",
            peak,
            trail_armed,
            (),
        )

    runner_pct = float(rule.runner_target_pct)
    if not short_dated and mark + 1e-4 >= entry * (1.0 + runner_pct / 100.0):
        return PremiumExit(
            held,
            f"take profit {pct:+.0f}% ≥ +{runner_pct:.0f}%",
            peak,
            True,
            (),
        )

    if (
        not short_dated
        and not half_done
        and held >= 2
        and mark + 1e-4 >= entry * (1.0 + target_pct / 100.0)
    ):
        piece = int(held * float(rule.partial_sell_pct) / 100.0 + 1e-9)
        if piece >= held:
            piece = held - 1
        if piece >= 1:
            return PremiumExit(
                piece,
                f"scale out +{target_pct:.0f}% — sell {piece} of {int(original)}",
                peak,
                trail_armed,
                (0,),
            )

    if trail_armed and mark < peak - 1e-4 and mark <= trail_line + 1e-4:
        if mark <= entry + 1e-4:
            why = "back to entry"
        else:
            why = "gave back half the peak gain"
        return PremiumExit(
            held,
            f"trail {why} — sell {held}",
            peak,
            True,
            (),
        )
    return PremiumExit(0, "", peak, trail_armed, ())
