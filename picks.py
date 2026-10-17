"""
Pick'em layer.

Picks are entered once per week from a screenshot of the Yahoo Pick'em
page. Everything after that is computed locally against the scoreboard,
so counts update live with no further input.

Scoring is straight picks: one point per correct game. Following the
stated methodology, a pick counts toward the week when it has already
won OR is currently leading.

Season total is summed from stored picks, so a week never entered is a
week that does not count. SEASON_BASELINE covers weeks that predate
this dashboard.
"""

import json
import logging
from datetime import datetime, timezone

import db
from clock import format_remaining, is_live, period_label, total_remaining_seconds

log = logging.getLogger("picks")

# Wins from weeks before picks were tracked here. Set this to the
# confirmed total Yahoo shows for those weeks, or leave at 0.
SEASON_BASELINE = 8


def resolve_team(conn, abbrev):
    """Find a team by any abbreviation the three platforms use."""
    text = (abbrev or "").strip().upper()
    if not text:
        return None
    row = conn.execute(
        """
        SELECT id, abbrev_canonical FROM team
        WHERE UPPER(abbrev_canonical)    = ?
           OR UPPER(abbrev_yahoo)        = ?
           OR UPPER(abbrev_espn_fantasy) = ?
        """,
        (text, text, text),
    ).fetchone()
    return row


def find_game(conn, season, week, team_id):
    """The game that team plays in the given week, if it is stored."""
    return conn.execute(
        """
        SELECT * FROM game
        WHERE season = ? AND week = ?
          AND (home_team_id = ? OR away_team_id = ?)
        LIMIT 1
        """,
        (season, week, team_id, team_id),
    ).fetchone()


def import_picks(season, week, abbrevs, replace=True):
    """Store one week of picks. Returns (stored, problems)."""
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    stored, problems = 0, []

    try:
        if replace:
            conn.execute(
                "DELETE FROM pick WHERE season = ? AND week = ?", (season, week)
            )

        for abbrev in abbrevs:
            team = resolve_team(conn, abbrev)
            if not team:
                problems.append(f"{abbrev}: not a recognized team abbreviation")
                continue

            game = find_game(conn, season, week, team["id"])
            if not game:
                problems.append(
                    f"{abbrev}: no week {week} game stored. "
                    "Run the poller, or check the week number."
                )
                continue

            conn.execute(
                """
                INSERT INTO pick (season, week, event_id, team_id, entered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(season, week, event_id)
                DO UPDATE SET team_id = excluded.team_id,
                              entered_at = excluded.entered_at
                """,
                (season, week, game["event_id"], team["id"], now),
            )
            stored += 1

        conn.commit()
    finally:
        conn.close()

    return stored, problems


def _status(game, picked_team_id):
    """Grade one pick against the current state of its game."""
    picked_home = game["home_team_id"] == picked_team_id
    mine = game["home_score"] if picked_home else game["away_score"]
    theirs = game["away_score"] if picked_home else game["home_score"]

    state = game["state"]
    live = is_live(state, game["status_detail"])

    if state == "post":
        if mine > theirs:
            return "won", mine, theirs
        if mine < theirs:
            return "lost", mine, theirs
        return "push", mine, theirs

    if live:
        if mine > theirs:
            return "leading", mine, theirs
        if mine < theirs:
            return "trailing", mine, theirs
        return "tied", mine, theirs

    return "pending", mine, theirs


# Statuses that count toward the running total.
COUNTING = {"won", "leading"}


def week_detail(season, week):
    """Every pick for a week, graded, plus the counts."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT p.event_id, p.team_id,
                   tp.abbrev_canonical AS picked,
                   g.*,
                   ta.abbrev_canonical AS away_abbrev,
                   th.abbrev_canonical AS home_abbrev
            FROM pick p
            JOIN team tp ON tp.id = p.team_id
            JOIN game g  ON g.event_id = p.event_id
            JOIN team ta ON ta.id = g.away_team_id
            JOIN team th ON th.id = g.home_team_id
            WHERE p.season = ? AND p.week = ?
            ORDER BY g.kickoff_utc, g.event_id
            """,
            (season, week),
        ).fetchall()
    finally:
        conn.close()

    picks = []
    for r in rows:
        status, mine, theirs = _status(r, r["team_id"])
        live = is_live(r["state"], r["status_detail"])
        remaining = total_remaining_seconds(
            r["period"], r["clock_seconds"], r["state"]
        )
        opponent = (
            r["away_abbrev"] if r["picked"] == r["home_abbrev"] else r["home_abbrev"]
        )

        picks.append({
            "event_id": r["event_id"],
            "picked": r["picked"],
            "opponent": opponent,
            "status": status,
            "counts": status in COUNTING,
            "my_score": mine,
            "their_score": theirs,
            "is_live": live,
            "label": period_label(
                r["period"], r["clock_seconds"], r["state"], r["status_detail"]
            ),
            "remaining": format_remaining(remaining) if live else None,
            "kickoff_utc": r["kickoff_utc"],
        })

    tally = {}
    for p in picks:
        tally[p["status"]] = tally.get(p["status"], 0) + 1

    return {
        "season": season,
        "week": week,
        "picks": picks,
        "count": sum(1 for p in picks if p["counts"]),
        "total": len(picks),
        "settled": sum(1 for p in picks if p["status"] in ("won", "lost", "push")),
        "tally": tally,
    }


def season_total(season, through_week=None):
    """Wins from settled weeks plus anything currently won or leading.

    Counting 'leading' in the season figure keeps it consistent with
    the weekly number, which is what the stated methodology asks for.
    """
    conn = db.connect()
    try:
        weeks = [
            r["week"] for r in conn.execute(
                "SELECT DISTINCT week FROM pick WHERE season = ? ORDER BY week",
                (season,),
            ).fetchall()
        ]
    finally:
        conn.close()

    if through_week is not None:
        weeks = [w for w in weeks if w <= through_week]

    total = SEASON_BASELINE
    by_week = []
    for week in weeks:
        detail = week_detail(season, week)
        total += detail["count"]
        by_week.append({
            "week": week,
            "count": detail["count"],
            "total": detail["total"],
        })

    return {
        "season": season,
        "total": total,
        "baseline": SEASON_BASELINE,
        "weeks_tracked": len(weeks),
        "by_week": by_week,
    }


def summary():
    """Everything the dashboard needs, or None when no picks are stored."""
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

    season, week = row["season"], row["week"]
    detail = week_detail(season, week)
    if not detail["picks"]:
        return {
            "season": season,
            "week": week,
            "has_picks": False,
            "season_total": season_total(season)["total"],
        }

    season_info = season_total(season)
    return {
        "season": season,
        "week": week,
        "has_picks": True,
        "week_count": detail["count"],
        "week_total": detail["total"],
        "week_settled": detail["settled"],
        "tally": detail["tally"],
        "picks": detail["picks"],
        "season_total": season_info["total"],
        "weeks_tracked": season_info["weeks_tracked"],
    }


def load_file(path):
    """Read a picks JSON file and import it."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    season = int(data["season"])
    week = int(data["week"])
    abbrevs = [
        p["team"] if isinstance(p, dict) else p
        for p in data["picks"]
    ]
    return season, week, import_picks(season, week, abbrevs)
