"""
Game clock math for NFL games.

ESPN's scoreboard returns the clock WITHIN the current quarter only.
Total game time remaining has to be derived, and several states break
the naive formula. Those cases are handled explicitly below.
"""

QUARTER_SECONDS = 15 * 60
REGULATION_SECONDS = 4 * QUARTER_SECONDS


def parse_clock(display_clock: str) -> int:
    """Convert ESPN's 'MM:SS' clock string into seconds.

    Returns 0 for anything unparseable rather than raising, because a
    bad clock string should not take down the whole poller.
    """
    if not display_clock:
        return 0
    text = display_clock.strip()
    parts = text.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), float(parts[1])
            return int(minutes * 60 + seconds)
        if len(parts) == 1:
            return int(float(parts[0]))
    except ValueError:
        return 0
    return 0


def total_remaining_seconds(period: int, clock_seconds: int, state: str) -> int:
    """Total game time left, in seconds.

    period      ESPN's quarter number. 1-4 regulation, 5+ overtime.
    clock_sec   Seconds left in the current quarter.
    state       ESPN's status.type.state: 'pre', 'in', or 'post'.
    """
    if state == "pre":
        return REGULATION_SECONDS
    if state == "post":
        return 0

    # Overtime length varies by season type, so the remaining clock
    # is all we can honestly report.
    if period >= 5:
        return max(0, clock_seconds)

    if period < 1:
        return REGULATION_SECONDS

    quarters_left_after_this = 4 - period
    remaining = quarters_left_after_this * QUARTER_SECONDS + clock_seconds

    # Guard rails. Neither of these should fire, but a silently wrong
    # clock is worse than a loud one.
    if remaining < 0:
        return 0
    if remaining > REGULATION_SECONDS:
        return REGULATION_SECONDS
    return remaining


def period_label(period: int, clock_seconds: int, state: str, status_detail: str = "") -> str:
    """Human-readable period label: 'Q2', 'Half', 'End Q1', 'OT', 'Final'."""
    detail = (status_detail or "").lower()

    if state == "pre":
        return "Pre"
    if state == "post":
        return "Final/OT" if period >= 5 else "Final"

    if "halftime" in detail or (period == 2 and clock_seconds == 0):
        return "Half"

    if period >= 5:
        return "OT" if period == 5 else f"OT{period - 4}"

    if clock_seconds == 0 and period in (1, 3):
        return f"End Q{period}"

    return f"Q{period}"


def format_remaining(total_seconds: int) -> str:
    """'40:00 remaining' style string."""
    minutes, seconds = divmod(max(0, total_seconds), 60)
    return f"{minutes}:{seconds:02d}"


def is_live(state: str, status_detail: str = "") -> bool:
    """True only when the game is actually in progress.

    Postponed, delayed, and canceled games report a non-'in' state or a
    status detail that rules them out.
    """
    detail = (status_detail or "").lower()
    for word in ("postponed", "canceled", "cancelled", "delayed", "suspended"):
        if word in detail:
            return False
    return state == "in"


def describe(
    away_abbrev: str,
    away_score: int,
    home_abbrev: str,
    home_score: int,
    period: int,
    clock_seconds: int,
    state: str,
    status_detail: str = "",
) -> str:
    """Full display line, away team first.

    Example: 'NE 21, NYJ 7 - Q2 - 40:00 remaining'
    """
    score = f"{away_abbrev} {away_score}, {home_abbrev} {home_score}"
    label = period_label(period, clock_seconds, state, status_detail)

    if not is_live(state, status_detail) and state != "post":
        if state == "pre":
            return f"{score} - {status_detail or 'Scheduled'}"
        return f"{score} - {status_detail or label}"

    if state == "post":
        return f"{score} - {label}"

    remaining = total_remaining_seconds(period, clock_seconds, state)
    if label == "Half":
        return f"{score} - Half - {format_remaining(remaining)} remaining"
    return f"{score} - {label} - {format_remaining(remaining)} remaining"
