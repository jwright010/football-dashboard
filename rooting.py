"""
Rooting interests.

Each week you list players and teams you are pulling for, with what you
want them to do. Players resolve to an ESPN athlete id once, at import,
and that id is stored permanently so grading is an exact lookup rather
than fuzzy name matching on every poll.

Goal semantics:
  over N   ->  strictly greater than N
  under N  ->  N or fewer, and only settles when the game ends
  any TD   ->  total TDs over 0

An 'over' clinches the instant it is met, since these stats do not fall.
An 'under' can fail early, the moment the number is passed, but cannot
succeed until the game is final.
"""

import logging
import math
import re
from datetime import datetime, timezone

import boxscore
import db
import roster
from clock import (
    REGULATION_SECONDS,
    format_remaining,
    is_live,
    period_label,
    total_remaining_seconds,
)

log = logging.getLogger("rooting")

TEAM_STATS = {
    "points": "points",
    "pts": "points",
    "team points": "points",
    "win": "win",
    "wins": "win",
    "win by": "win_by",
    "winby": "win_by",
    "margin": "win_by",
    "win margin": "win_by",
    "game total": "game_total",
    "total points": "game_total",
    "combined points": "game_total",
}

TEAM_STAT_LABELS = {
    "points": "points",
    "win": "win",
    "win_by": "win by",
    "game_total": "game total",
}

# Goals with no number attached.
NO_TARGET = {"win"}

# Stats that can swing either way, so they cannot clinch or die early
# and only settle when the game ends. A lead is not a win, and a
# 20-point margin in the third quarter is not a 20-point win.
NON_MONOTONIC = {"win", "win_by"}


class Unresolved(Exception):
    """Raised when a player name needs a human to choose."""

    def __init__(self, query, team_abbrev, candidates):
        self.query = query
        self.team_abbrev = team_abbrev
        self.candidates = candidates
        super().__init__(f"{query} ({team_abbrev}): {len(candidates)} candidates")


# ---------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------

LINE_PLAYER = re.compile(
    r"^\s*(?P<name>.+?)\s*\(\s*(?P<team>[A-Za-z]{2,4})\s*\)\s*"
    r"(?P<rest>.+?)\s*$"
)
GOAL = re.compile(
    r"^(?P<stat>.+?)\s+(?P<dir>over|under|at least|atleast)\s+"
    r"(?P<target>-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def parse_line(line):
    """Turn one typed line into a goal dict, or None if it is not one."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    match = LINE_PLAYER.match(text)
    if match:
        name = match.group("name").strip()
        team = match.group("team").strip().upper()
        rest = match.group("rest").strip()
        return _goal("player", name, team, rest)

    # Team line: "BUF win", "KC points over 24"
    parts = text.split(None, 1)
    if len(parts) == 2 and len(parts[0]) <= 4 and parts[0].isalpha():
        return _goal("team", parts[0].upper(), parts[0].upper(), parts[1].strip())

    return None


def _goal(kind, label, team, rest):
    lowered = rest.strip().lower()

    if kind == "team":
        if TEAM_STATS.get(lowered) in NO_TARGET:
            return {"kind": "team", "label": label, "team": team,
                    "stat": TEAM_STATS[lowered], "direction": "over",
                    "target": 0.0}

        match = GOAL.match(rest)
        if match:
            stat = TEAM_STATS.get(match.group("stat").strip().lower())
            if stat and stat not in NO_TARGET:
                direction = match.group("dir").lower()
                target = float(match.group("target"))
                if direction in ("at least", "atleast"):
                    direction, target = "over", _at_least(target)
                return {"kind": "team", "label": label, "team": team,
                        "stat": stat, "direction": direction, "target": target}
            if stat:
                return {"error": f"{match.group('stat')!r} takes no number"}
        return {"error": f"Could not read team goal: {rest!r}"}

    # Player goals
    if lowered in ("any td", "td", "touchdown", "a td", "any touchdown"):
        return {"kind": "player", "label": label, "team": team,
                "stat": "total_td", "direction": "over", "target": 0.0}

    match = GOAL.match(rest)
    if not match:
        return {"error": f"Could not read goal: {rest!r}"}

    stat = boxscore.resolve_stat(match.group("stat"))
    if not stat:
        return {"error": f"Unknown stat: {match.group('stat')!r}"}

    direction = match.group("dir").lower()
    target = float(match.group("target"))
    if direction in ("at least", "atleast"):
        direction, target = "over", _at_least(target)

    return {"kind": "player", "label": label, "team": team,
            "stat": stat, "direction": direction, "target": target}


def parse_text(text):
    """Parse a whole block. Returns (goals, errors)."""
    goals, errors = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("week"):
            continue
        parsed = parse_line(line)
        if parsed is None:
            errors.append(f"Could not read line: {line!r}")
        elif "error" in parsed:
            errors.append(f"{line!r}: {parsed['error']}")
        else:
            goals.append(parsed)
    return goals, errors


# ---------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------

def _at_least(target):
    """Turn "at least N" into the equivalent "over" target.

    Every stat here counts in whole numbers, so "at least N" means the
    first whole number at or above N. Subtracting 1 only works when N is
    whole: "at least 26.5" means 27 or more, which is "over 26", not
    "over 25.5". Rounding N up first handles both.
    """
    return float(math.ceil(target) - 1)


# Spellings people type that aren't the canonical abbreviation.
TEAM_ALIASES = {
    "JAC": "JAX", "ARZ": "ARI", "GNB": "GB", "KAN": "KC", "NOR": "NO",
    "NWE": "NE", "SFO": "SF", "TAM": "TB", "LVR": "LV", "WAS": "WSH",
}


def team_row(conn, abbrev):
    text = (abbrev or "").strip().upper()
    text = TEAM_ALIASES.get(text, text)
    return conn.execute(
        """
        SELECT id, espn_id, abbrev_canonical FROM team
        WHERE UPPER(abbrev_canonical)    = ?
           OR UPPER(abbrev_yahoo)        = ?
           OR UPPER(abbrev_espn_fantasy) = ?
        """,
        (text, text, text),
    ).fetchone()


def resolve_player(espn_team_id, name, chooser=None):
    """Athlete id for a typed name. Asks chooser when ambiguous."""
    candidates = roster.search(espn_team_id, name)

    if not candidates:
        raise Unresolved(name, espn_team_id, [])
    if len(candidates) == 1:
        return candidates[0]

    if chooser is None:
        raise Unresolved(name, espn_team_id, candidates)
    chosen = chooser(name, candidates)
    if chosen is None:
        raise Unresolved(name, espn_team_id, candidates)
    return chosen


def import_goals(season, week, goals, chooser=None, replace=True):
    """Store a week of rooting interests. Returns (stored, problems)."""
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    stored, problems = 0, []

    try:
        if replace:
            conn.execute(
                "DELETE FROM rooting WHERE season = ? AND week = ?", (season, week)
            )

        for goal in goals:
            team = team_row(conn, goal["team"])
            if not team:
                problems.append(f"{goal['label']}: unknown team {goal['team']!r}")
                continue

            athlete_id, display = None, goal["label"]
            if goal["kind"] == "player":
                try:
                    athlete = resolve_player(team["espn_id"], goal["label"], chooser)
                except roster.RosterUnavailable as exc:
                    problems.append(
                        f"{goal['label']} ({goal['team']}): {exc}. Re-run the "
                        "import to pick this goal up."
                    )
                    continue
                except Unresolved as exc:
                    if exc.candidates:
                        names = ", ".join(a["name"] for a in exc.candidates[:5])
                        problems.append(
                            f"{goal['label']} ({goal['team']}): ambiguous. "
                            f"Candidates: {names}"
                        )
                    else:
                        problems.append(
                            f"{goal['label']}: not on the {goal['team']} roster"
                        )
                    continue
                athlete_id = athlete["athlete_id"]
                display = athlete["name"]

            conn.execute(
                """
                INSERT INTO rooting
                    (season, week, kind, label, team_id, athlete_id,
                     stat, direction, target, entered_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(season, week, kind, label, stat, direction, target)
                DO UPDATE SET athlete_id = excluded.athlete_id,
                              team_id    = excluded.team_id,
                              entered_at = excluded.entered_at
                """,
                (season, week, goal["kind"], display, team["id"], athlete_id,
                 goal["stat"], goal["direction"], goal["target"], now),
            )
            stored += 1

        conn.commit()
    finally:
        conn.close()

    return stored, problems


# ---------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------

def _grade(direction, target, current, state, monotonic=True):
    """Status for one goal given the current value and game state.

    A non-monotonic stat can move either way, so it neither clinches
    nor dies before the game ends. Leading at halftime is not a win.
    """
    if current is None:
        return "pending" if state == "pre" else "noshow"

    if not monotonic:
        if state == "pre":
            return "pending"
        if state != "post":
            return "live"
        met = current > target if direction == "over" else current <= target
        return "achieved" if met else "failed"

    if direction == "over":
        if current > target:
            return "achieved"
        if state == "post":
            return "failed"
        return "pending" if state == "pre" else "live"

    # under: can fail at any moment, can only succeed at the whistle
    if current > target:
        return "failed"
    if state == "post":
        return "achieved"
    return "pending" if state == "pre" else "live"


def _progress(direction, target, current):
    """Fraction of the bar to fill, 0 to 1.

    A margin can be negative, which floors the bar at empty rather
    than rendering something nonsensical.
    """
    if current is None:
        return 0.0
    if current < 0:
        return 0.0
    if direction == "over":
        # 'over 0' means at least one, so treat the goal line as target + 1
        goal = target + 1 if target == int(target) else target
        if goal <= 0:
            return 1.0 if current > 0 else 0.0
        return max(0.0, min(1.0, current / goal))
    if target <= 0:
        return 1.0 if current > 0 else 0.0
    return max(0.0, min(1.0, current / target))


# Stats that accumulate steadily enough for a pace projection to mean
# something. Touchdowns come in lumps, so they are left out.
PACE_STATS = {
    "pass_yds", "rush_yds", "rec_yds", "rush_rec_yds", "all_purpose_yds", "rec",
}

# Too early in a game, one play swings the projection wildly.
PACE_MIN_FRACTION = 0.10


def _game_fraction(game):
    """Share of regulation played, or None when a pace would mislead.

    None before a tenth of the game has gone by, since one early play
    swings the projection wildly, and in overtime, whose length is not
    fixed, so there is no honest denominator.
    """
    if not game or game["period"] >= 5:
        return None
    remaining = total_remaining_seconds(
        game["period"], game["clock_seconds"], game["state"]
    )
    fraction = (REGULATION_SECONDS - remaining) / REGULATION_SECONDS
    return fraction if fraction >= PACE_MIN_FRACTION else None


def _pace(stat, current, game, live):
    """Projected final value at the current rate, or None."""
    if stat not in PACE_STATS or current is None or not live:
        return None
    fraction = _game_fraction(game)
    if fraction is None:
        return None
    return int(round(current / fraction))


# Team stats that accumulate, so a pace means something for them too.
TEAM_PACE_STATS = {"game_total", "points"}


def _track(stat, direction, target, current, status, game, live):
    """Whether a goal is on track. Returns (track, explore).

    track is one of:
      pending     game not started                      light gray
      on / off    in play, on or off track               green / red
      open        in play, no verdict possible yet       neutral
      final_hit   finished and hit                       light blue, green accent
      final_miss  finished and missed                    light blue, red accent

    explore marks goals whose rule is provisional: anything that is
    not a steady accumulating stat, where pace does not apply cleanly.
    """
    explore = stat not in PACE_STATS

    if game and game["state"] == "post":
        return ("final_hit" if status == "achieved" else "final_miss"), explore
    if status == "pending":
        return "pending", explore
    if status == "achieved":
        return "on", explore
    if status == "failed":
        return "off", explore

    if stat in PACE_STATS:
        pace = _pace(stat, current, game, live)
        if pace is None:
            return "open", explore
        hit = pace >= target if direction == "over" else pace <= target
        return ("on" if hit else "off"), explore

    if stat == "win":
        return ("on" if current == 1.0 else "off"), explore
    if stat == "win_by":
        if current is None:
            return "open", explore
        return ("on" if current > target else "off"), explore
    if stat in TEAM_PACE_STATS:
        fraction = _game_fraction(game)
        if current is None or fraction is None:
            return "open", explore
        projected = current / fraction
        hit = projected >= target if direction == "over" else projected <= target
        return ("on" if hit else "off"), explore

    # Touchdowns and other lumpy counts: green once hit, red only once
    # the game ends without it, and no verdict in between.
    return "open", explore


def week_detail(season, week):
    """Every rooting interest for a week, graded against live data."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT r.*, t.abbrev_canonical AS team_abbrev, t.espn_id AS team_espn
            FROM rooting r
            JOIN team t ON t.id = r.team_id
            WHERE r.season = ? AND r.week = ?
            ORDER BY r.id
            """,
            (season, week),
        ).fetchall()

        games = {}
        for r in conn.execute(
            """
            SELECT g.*, ta.espn_id AS away_espn, th.espn_id AS home_espn,
                   ta.abbrev_canonical AS away_abbrev,
                   th.abbrev_canonical AS home_abbrev
            FROM game g
            JOIN team ta ON ta.id = g.away_team_id
            JOIN team th ON th.id = g.home_team_id
            WHERE g.season = ? AND g.week = ?
            """,
            (season, week),
        ).fetchall():
            games[str(r["away_espn"])] = r
            games[str(r["home_espn"])] = r
    finally:
        conn.close()

    # Only fetch box scores for games that a goal actually depends on.
    needed = set()
    for r in rows:
        game = games.get(str(r["team_espn"]))
        if game and game["state"] in ("in", "post"):
            needed.add(game["event_id"])

    summaries = {}
    for event_id in needed:
        try:
            summaries[event_id] = boxscore.fetch_summary(event_id)
        except Exception as exc:
            log.error("Box score fetch failed for %s: %s", event_id, exc)

    results = []
    for r in rows:
        game = games.get(str(r["team_espn"]))
        state = game["state"] if game else "pre"
        summary = summaries.get(game["event_id"]) if game else None

        current = None
        if summary:
            if r["kind"] == "team":
                mine = boxscore.team_points(summary, r["team_espn"])
                theirs = None
                if game:
                    other = (
                        game["home_espn"]
                        if str(game["away_espn"]) == str(r["team_espn"])
                        else game["away_espn"]
                    )
                    theirs = boxscore.team_points(summary, other)

                if r["stat"] == "points":
                    current = mine
                elif r["stat"] == "game_total":
                    if mine is not None and theirs is not None:
                        current = mine + theirs
                elif r["stat"] == "win":
                    if mine is not None and theirs is not None:
                        current = 1.0 if mine > theirs else 0.0
                elif r["stat"] == "win_by":
                    if mine is not None and theirs is not None:
                        current = mine - theirs
            elif r["athlete_id"]:
                current = boxscore.player_stat(
                    summary, r["athlete_id"], r["stat"]
                )

        status = _grade(
            r["direction"], r["target"], current, state,
            monotonic=r["stat"] not in NON_MONOTONIC,
        )
        live = is_live(state, game["status_detail"]) if game else False

        if r["stat"] == "win":
            goal_text = "win"
            target_text = ""
        elif r["stat"] == "total_td" and r["target"] == 0:
            goal_text = "any TD"
            target_text = ""
        else:
            label = TEAM_STAT_LABELS.get(
                r["stat"], boxscore.stat_label(r["stat"])
            )
            target = (
                int(r["target"]) if r["target"] == int(r["target"])
                else r["target"]
            )
            goal_text = f"{label} {r['direction']} {target}"
            target_text = str(target)

        opponent = ""
        if game:
            opponent = (
                game["home_abbrev"]
                if str(game["away_espn"]) == str(r["team_espn"])
                else game["away_abbrev"]
            )

        results.append({
            "id": r["id"],
            "kind": r["kind"],
            "athlete_id": r["athlete_id"],
            "event_id": game["event_id"] if game else None,
            "label": r["label"],
            "team": r["team_abbrev"],
            "opponent": opponent,
            "stat": r["stat"],
            "direction": r["direction"],
            "target": r["target"],
            "target_text": target_text,
            "goal_text": goal_text,
            "current": current,
            "status": status,
            "progress": _progress(r["direction"], r["target"], current),
            "is_live": live,
            "pace": (
                _pace(r["stat"], current, game, live)
                if status == "live" else None
            ),
            **dict(zip(
                ("track", "explore"),
                _track(r["stat"], r["direction"], r["target"], current,
                       status, game, live),
            )),
            "game_label": (
                period_label(game["period"], game["clock_seconds"],
                             game["state"], game["status_detail"])
                if game else ""
            ),
            "remaining": (
                format_remaining(
                    total_remaining_seconds(
                        game["period"], game["clock_seconds"], game["state"]
                    )
                ) if live else None
            ),
        })

    tally = {}
    for item in results:
        tally[item["status"]] = tally.get(item["status"], 0) + 1

    return {
        "season": season,
        "week": week,
        "items": results,
        "tally": tally,
        "achieved": tally.get("achieved", 0),
        "total": len(results),
    }


def summary_for_dashboard():
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT season, week FROM game
            WHERE season = (SELECT MAX(season) FROM game)
            ORDER BY week DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    detail = week_detail(row["season"], row["week"])
    if not detail["items"]:
        return None
    return detail
