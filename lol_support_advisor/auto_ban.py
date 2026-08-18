from __future__ import annotations

import random
from collections.abc import Callable


AUTO_BAN_TARGET_MIN_MS = 15_000
AUTO_BAN_TARGET_MAX_MS = 18_000
AUTO_BAN_STAGE_LEAD_MIN_MS = 900
AUTO_BAN_STAGE_LEAD_MAX_MS = 1_400
AUTO_BAN_STALE_TIMER_GRACE_SECONDS = 0.35


def choose_auto_ban_target_ms(
    randint: Callable[[int, int], int] | None = None,
) -> int:
    """Choose a safe, non-identical point before the ban timer expires."""
    picker = randint or random.randint
    return int(picker(AUTO_BAN_TARGET_MIN_MS, AUTO_BAN_TARGET_MAX_MS))


def choose_auto_ban_stage_lead_ms(
    randint: Callable[[int, int], int] | None = None,
) -> int:
    """Choose a short pause between showing the champion and banning it."""
    picker = randint or random.randint
    return int(picker(AUTO_BAN_STAGE_LEAD_MIN_MS, AUTO_BAN_STAGE_LEAD_MAX_MS))


def auto_ban_monitor_due(
    remaining_ms: int | None,
    target_ms: int,
    deadline: float,
    now_monotonic: float,
) -> bool:
    """Combine Riot's timer with a guarded monotonic stale-timer watchdog."""
    if remaining_ms is None:
        return now_monotonic >= deadline
    return (
        remaining_ms <= max(0, int(target_ms))
        or now_monotonic
        >= deadline + AUTO_BAN_STALE_TIMER_GRACE_SECONDS
    )


def auto_ban_stage_due(
    remaining_ms: int | None,
    target_ms: int,
    stage_lead_ms: int,
    deadline: float,
    now_monotonic: float,
) -> bool:
    """Return true only shortly before the final ban commit is due."""
    lead_ms = max(0, int(stage_lead_ms))
    return auto_ban_monitor_due(
        remaining_ms,
        max(0, int(target_ms)) + lead_ms,
        float(deadline) - lead_ms / 1000.0,
        now_monotonic,
    )


def auto_ban_deadline_after_timer_sample(
    previous_remaining_ms: int | None,
    fresh_remaining_ms: int | None,
    target_ms: int,
    now_monotonic: float,
    current_deadline: float,
) -> float:
    """Anchor the watchdog when Riot's real timer appears or is extended."""
    if fresh_remaining_ms is None:
        return current_deadline
    if (
        previous_remaining_ms is not None
        and fresh_remaining_ms <= previous_remaining_ms + 750
    ):
        return current_deadline
    return now_monotonic + max(
        0.0, (fresh_remaining_ms - max(0, int(target_ms))) / 1000.0,
    )


def projected_auto_ban_remaining_ms(
    remaining_ms: int | None,
    sampled_at: float,
    now_monotonic: float,
) -> int | None:
    """Project one Riot timer sample for display without driving the action."""
    if remaining_ms is None or sampled_at <= 0:
        return None
    elapsed_ms = max(0.0, now_monotonic - sampled_at) * 1000.0
    return max(0, int(round(float(remaining_ms) - elapsed_ms)))
