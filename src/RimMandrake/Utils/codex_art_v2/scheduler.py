#!/usr/bin/env python3
"""scheduler.py — turn a live `account/rateLimits/read` into a batch verdict.

This is the piece of CODEX_PROPOSAL_ART_WORKER.md that earns its keep on its own,
independently of the persistent-worker architecture around it. The existing
one-shot pipeline is BLIND to account usage: a batch driver
(`gen_sea_facings.py`, `gen_livestock_mockups.py`) will happily launch twenty
generations into a weekly window that is 90% spent, and only find out by failing
partway through with quota errors that look like the flaky-generation failures
the skill already tells you to just retry.

`infrastructure/state/LESSONS_INBOX.md` states the constraint this reads:
"the weekly token budget, not per-minute image rate, is the binding limit."
That number exists, is free to read, and nothing currently reads it.

Pure functions over a limits dict. No I/O, no network -- so the whole policy
table is testable against synthetic values with no app-server at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass
class Window:
    """One usage window. `used_percent` is CONSUMED; remaining is 100 - used."""
    label: str
    used_percent: float | None
    duration_mins: int | None
    resets_at: int | None  # Unix seconds

    @property
    def remaining_percent(self) -> float | None:
        return None if self.used_percent is None else 100.0 - self.used_percent

    def resets_at_pacific(self) -> str | None:
        """Full Pacific date/time with the CORRECT seasonal suffix.

        Never label a daylight-saving timestamp PST just because someone said
        "Pacific" generically -- zoneinfo picks PST/PDT from the actual date.
        """
        if not self.resets_at:
            return None
        return datetime.fromtimestamp(self.resets_at, PACIFIC).strftime(
            "%Y-%m-%d %I:%M:%S %p %Z")

    def resets_at_utc(self) -> str | None:
        if not self.resets_at:
            return None
        return datetime.fromtimestamp(self.resets_at, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Verdict:
    """What the scheduler permits right now, and why."""
    max_workers: int
    max_iterations_per_job: int | None
    reason: str
    warn: bool = False
    stop: bool = False
    next_tier_report_recommended: bool = False
    primary: Window | None = None
    secondary: Window | None = None
    plan_type: str | None = None
    reset_credits_available: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def may_dispatch(self) -> bool:
        return not self.stop and self.max_workers > 0


def parse_limits(raw: dict[str, Any]) -> tuple[Window, Window, dict]:
    """Extract the two windows from an `account/rateLimits/read` result.

    Prefers `rateLimitsByLimitId.codex` over the flat `rateLimits`, per the
    proposal -- and verified against a real 0.153.1 read on 2026-09-06, where
    both were present and identical.
    """
    block = (raw.get("rateLimitsByLimitId") or {}).get("codex") or raw.get("rateLimits") or {}
    prim = block.get("primary") or {}
    sec = block.get("secondary") or {}
    primary = Window("5-hour", prim.get("usedPercent"),
                     prim.get("windowDurationMins"), prim.get("resetsAt"))
    secondary = Window("weekly", sec.get("usedPercent"),
                       sec.get("windowDurationMins"), sec.get("resetsAt"))
    return primary, secondary, block


def decide(raw: dict[str, Any]) -> Verdict:
    """Apply the policy table. The MOST RESTRICTIVE matching row wins.

    `null` means UNKNOWN, never zero -- an unreadable window is treated as
    dangerous, not as empty.
    """
    primary, secondary, block = parse_limits(raw)
    credits = raw.get("rateLimitResetCredits") or {}
    v = Verdict(
        max_workers=4, max_iterations_per_job=None, reason="",
        primary=primary, secondary=secondary,
        plan_type=block.get("planType"),
        reset_credits_available=int(credits.get("availableCount") or 0),
    )

    p = primary.used_percent
    s = secondary.used_percent

    if block.get("rateLimitReachedType"):
        v.stop, v.max_workers, v.warn = True, 0, True
        v.reason = (f"provider reports rateLimitReachedType="
                    f"{block['rateLimitReachedType']}: stop dispatch, do not probe "
                    f"with another image")
        v.next_tier_report_recommended = True
        return v

    if block.get("spendControlReached"):
        v.stop, v.max_workers, v.warn = True, 0, True
        v.reason = "spendControlReached: stop dispatch"
        return v

    if p is None or s is None:
        v.max_workers, v.max_iterations_per_job, v.warn = 1, 1, True
        v.reason = ("a usage window read as null (UNKNOWN, not zero): at most 1 "
                    "worker until it is clarified")
        return v

    worst = max(p, s)
    if s >= 97:
        v.stop, v.max_workers, v.warn = True, 0, True
        v.reason = f"weekly at {s:.0f}% used: preserve returned work and stop the pool"
    elif s >= 90 or p >= 90:
        v.stop, v.max_workers, v.warn = True, 0, True
        v.reason = (f"a window is at {worst:.0f}% used: no new image calls, "
                    f"checkpoint and wait for the reset")
    elif 80 <= s <= 89 or 70 <= p <= 89:
        v.max_workers, v.max_iterations_per_job, v.warn = 1, 2, True
        v.reason = (f"weekly {s:.0f}% / 5-hour {p:.0f}% used: warn once, at most 1 "
                    f"worker, new jobs capped at 2 iterations")
    elif 70 <= s <= 79:
        v.max_workers, v.max_iterations_per_job = 2, None
        v.reason = (f"weekly {s:.0f}% used: at most 2 workers, no unbounded "
                    f"overnight batch")
    else:
        v.max_workers, v.max_iterations_per_job = 4, None
        v.reason = (f"weekly {s:.0f}% / 5-hour {p:.0f}% used: up to 4 independent "
                    f"workers")

    v.next_tier_report_recommended = bool(s >= 75 or p >= 80 or s >= 80)

    if v.reset_credits_available:
        v.notes.append(
            f"{v.reset_credits_available} full-reset credit(s) available. They are "
            f"OWNER-CONTROLLED: report the count, never redeem one.")
    v.notes.append(
        "The account exposes no images-remaining count. Do not convert a "
        "percentage into a promised number of images.")
    return v


def render(v: Verdict) -> str:
    """One human-readable block. Every reset carries its full Pacific datetime."""
    lines = []
    plan = v.plan_type or "unknown plan"
    lines.append(f"plan: {plan}")
    for w in (v.primary, v.secondary):
        if w is None:
            continue
        if w.used_percent is None:
            lines.append(f"{w.label:<8} UNKNOWN (null, which is not zero)")
            continue
        hrs = f"{w.duration_mins // 60}h" if w.duration_mins else "?"
        lines.append(
            f"{w.label:<8} {w.used_percent:>5.0f}% used / "
            f"{w.remaining_percent:>3.0f}% left  (window {hrs})  "
            f"resets {w.resets_at_pacific() or 'unknown'}")
    lines.append("")
    verdict = "STOP" if v.stop else f"DISPATCH up to {v.max_workers} worker(s)"
    if v.max_iterations_per_job:
        verdict += f", max {v.max_iterations_per_job} iteration(s) per job"
    lines.append(f"verdict: {verdict}")
    lines.append(f"because: {v.reason}")
    if v.next_tier_report_recommended:
        lines.append("next_tier_report_recommended: true  (a marker only -- it "
                     "triggers no research and no prose)")
    for n in v.notes:
        lines.append(f"note: {n}")
    return "\n".join(lines)
