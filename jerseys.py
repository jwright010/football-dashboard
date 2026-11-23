"""
Jersey numbers for the player views.

Numbers come from ESPN's team rosters. A team's roster is fetched once and
saved in the local database, one row per player, so a number costs no
request after that. The phone version's database is carried between GitHub
builds, so it asks ESPN about a team at most once a day, not every build.

Nothing here may break the page. If ESPN can't be reached, the number is
simply left out, and a failed team isn't tried again for 10 minutes so a
slow ESPN can't slow every refresh.

The tables are created on first use, so no database migration is needed.
"""

import logging
import time

import db
import roster

log = logging.getLogger("jerseys")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jersey (
    team_espn_id TEXT NOT NULL,
    norm_name    TEXT NOT NULL,
    athlete_id   TEXT,
    jersey       TEXT NOT NULL,
    PRIMARY KEY (team_espn_id, norm_name)
);
CREATE TABLE IF NOT EXISTS jersey_team (
    team_espn_id TEXT PRIMARY KEY,
    fetched_at   REAL NOT NULL
);
"""

TTL_SECONDS = 24 * 3600
RETRY_AFTER_SECONDS = 10 * 60

# Team id -> the time before which a failed fetch isn't tried again.
_retry_after = {}


def _ensure(conn):
    conn.executescript(SCHEMA)


def _is_stale(team):
    conn = db.connect()
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT fetched_at FROM jersey_team WHERE team_espn_id = ?", (team,)
        ).fetchone()
        return row is None or time.time() - row["fetched_at"] > TTL_SECONDS
    finally:
        conn.close()


def refresh_team(team):
    """Fetch one team's roster and save every number on it."""
    athletes = roster.fetch_roster(team)
    rows = [
        (team, roster.normalize(a["name"]), a["athlete_id"], str(a["jersey"]))
        for a in athletes if a.get("jersey")
    ]
    conn = db.connect()
    try:
        _ensure(conn)
        conn.execute("DELETE FROM jersey WHERE team_espn_id = ?", (team,))
        conn.executemany(
            "INSERT OR REPLACE INTO jersey "
            "(team_espn_id, norm_name, athlete_id, jersey) VALUES (?,?,?,?)", rows
        )
        conn.execute(
            "INSERT OR REPLACE INTO jersey_team (team_espn_id, fetched_at) VALUES (?,?)",
            (team, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def lookup(team_espn_id, name, athlete_id=None):
    """A player's jersey number as text, or "" when it can't be found."""
    team = str(team_espn_id or "")
    if not team or not name:
        return ""
    try:
        if _is_stale(team) and time.time() >= _retry_after.get(team, 0):
            try:
                refresh_team(team)
            except Exception as exc:
                _retry_after[team] = time.time() + RETRY_AFTER_SECONDS
                log.warning("Couldn't fetch jersey numbers for team %s: %s", team, exc)

        conn = db.connect()
        try:
            row = None
            if athlete_id:
                row = conn.execute(
                    "SELECT jersey FROM jersey WHERE team_espn_id = ? AND athlete_id = ?",
                    (team, str(athlete_id)),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT jersey FROM jersey WHERE team_espn_id = ? AND norm_name = ?",
                    (team, roster.normalize(name)),
                ).fetchone()
            return row["jersey"] if row else ""
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Jersey lookup failed for %s: %s", name, exc)
        return ""
