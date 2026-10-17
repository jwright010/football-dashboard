"""
The private Yahoo league, from a weekly lineup screenshot.

League 752951 has no API path: 401 on the public host, 403 on the
authenticated one. So its lineups come in once a week from a screenshot,
and its points are computed live from ESPN box scores using the league's
own scoring rules (yahoo_scoring.py).

Only the roster is static for the week. Every point shown is live.

Players resolve to ESPN athlete ids at import, the same lazy resolution
the rooting tracker uses, and each is stored under the full name ESPN
reports. That matters beyond scoring: a player the screenshot shows as
'C. Williams' has to be 'Caleb Williams' to merge with the same person
on another roster in the player view.

This module creates its own table, so no database migration is needed.
"""

import json
import logging
from datetime import datetime, timezone

import boxscore
import db
import projection
import roster
import yahoo_scoring

log = logging.getLogger("manual")

DEFAULT_LEAGUE = "752951"

# Slots that name a real position, which can be used to break a tie
# between two players whose names both match.
POSITION_SLOTS = {"QB", "RB", "WR", "TE", "K"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_player (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    league      TEXT    NOT NULL,
    side        TEXT    NOT NULL,          -- 'me' or 'opponent'
    team_name   TEXT    NOT NULL,          -- fantasy team name
    slot        TEXT    NOT NULL,          -- QB, RB, W/R/T, K, DEF ...
    label       TEXT    NOT NULL,          -- full name as ESPN reports it
    team_id     INTEGER NOT NULL REFERENCES team(id),
    athlete_id  TEXT,                      -- null for team defenses
    entered_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_week ON manual_player(season, week, league);
"""


class Ambiguous(Exception):
    def __init__(self, name, candidates):
        self.name = name
        self.candidates = candidates
        super().__init__(f"{name}: {len(candidates)} candidates")


def ensure_table():
    conn = db.connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def team_row(conn, abbrev):
    text = (abbrev or "").strip().upper()
    return conn.execute(
        """
        SELECT id, espn_id, abbrev_canonical FROM team
        WHERE UPPER(abbrev_canonical)    = ?
           OR UPPER(abbrev_yahoo)        = ?
           OR UPPER(abbrev_espn_fantasy) = ?
        """,
        (text, text, text),
    ).fetchone()


def resolve_athlete(espn_team_id, name, slot, chooser=None):
    """One athlete for a typed name, or raise.

    Screenshots show 'C. Williams', which the roster search already
    handles through its first-initial tier. When two players still
    match, a real position in the slot usually settles it.
    """
    candidates = roster.search(espn_team_id, name)
    if not candidates:
        raise LookupError(f"{name}: not on that team's roster")

    if len(candidates) > 1 and (slot or "").upper() in POSITION_SLOTS:
        narrowed = [
            c for c in candidates
            if (c.get("position") or "").upper() == slot.upper()
        ]
        if narrowed:
            candidates = narrowed

    if len(candidates) == 1:
        return candidates[0]

    if chooser:
        chosen = chooser(name, candidates)
        if chosen:
            return chosen
    raise Ambiguous(name, candidates)


def current_season():
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(season) AS s FROM game").fetchone()
        return row["s"] if row and row["s"] else None
    finally:
        conn.close()


def import_file(path, chooser=None):
    """Store one week's lineups. Returns (season, week, stored, problems)."""
    ensure_table()
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    season = int(data.get("season") or current_season())
    week = int(data["week"])
    league = str(data.get("league") or DEFAULT_LEAGUE)
    now = datetime.now(timezone.utc).isoformat()

    stored, problems = 0, []
    conn = db.connect()
    try:
        conn.execute(
            "DELETE FROM manual_player WHERE season = ? AND week = ? AND league = ?",
            (season, week, league),
        )

        for side in ("me", "opponent"):
            block = data.get(side) or {}
            team_name = block.get("name") or side
            for entry in block.get("lineup") or []:
                slot, name, team_abbrev = entry[0], entry[1], entry[2]
                team = team_row(conn, team_abbrev)
                if not team:
                    problems.append(f"{name}: unknown team {team_abbrev!r}")
                    continue

                label, athlete_id = name, None
                if slot.upper() not in yahoo_scoring.DEFENSE_SLOTS:
                    try:
                        athlete = resolve_athlete(team["espn_id"], name, slot, chooser)
                        label = athlete["name"]
                        athlete_id = athlete["athlete_id"]
                    except Ambiguous as exc:
                        names = ", ".join(c["name"] for c in exc.candidates[:5])
                        problems.append(f"{name} ({team_abbrev}): ambiguous, {names}")
                        continue
                    except LookupError as exc:
                        problems.append(f"{exc} ({team_abbrev})")
                        continue
                    except roster.RosterUnavailable as exc:
                        problems.append(
                            f"{name} ({team_abbrev}): {exc}. Re-run the import "
                            "to pick this player up."
                        )
                        continue

                conn.execute(
                    """
                    INSERT INTO manual_player
                        (season, week, league, side, team_name, slot,
                         label, team_id, athlete_id, entered_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (season, week, league, side, team_name, slot.upper(),
                     label, team["id"], athlete_id, now),
                )
                stored += 1
        conn.commit()
    finally:
        conn.close()

    return season, week, stored, problems


def compute_matchup(week, league=DEFAULT_LEAGUE):
    """The week's head-to-head, scored live. None if nothing imported."""
    ensure_table()
    season = current_season()
    if season is None:
        return None

    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT m.*, t.espn_id AS team_espn, t.abbrev_canonical AS abbrev
            FROM manual_player m JOIN team t ON t.id = m.team_id
            WHERE m.season = ? AND m.week = ? AND m.league = ?
            ORDER BY m.id
            """,
            (season, week, league),
        ).fetchall()

        games = {}
        for g in conn.execute(
            """
            SELECT g.*, ta.espn_id AS away_espn, th.espn_id AS home_espn
            FROM game g
            JOIN team ta ON ta.id = g.away_team_id
            JOIN team th ON th.id = g.home_team_id
            WHERE g.season = ? AND g.week = ?
            """,
            (season, week),
        ).fetchall():
            games[str(g["away_espn"])] = g
            games[str(g["home_espn"])] = g
    finally:
        conn.close()

    if not rows:
        return None

    summaries = {}
    sides = {"me": {"name": None, "players": []},
             "opponent": {"name": None, "players": []}}

    # One request for every player on both sides. If it fails, points
    # still show; only the projection drops to "pending".
    def projection_id(r):
        if r["slot"].upper() in yahoo_scoring.DEFENSE_SLOTS:
            return projection.defense_id(r["team_espn"])
        return int(r["athlete_id"]) if r["athlete_id"] else None

    try:
        lines = projection.fetch([projection_id(r) for r in rows], week)
    except Exception as exc:
        log.error("Projection fetch failed: %s", exc)
        lines = None

    for r in rows:
        team_espn = str(r["team_espn"])
        game = games.get(team_espn)
        state = game["state"] if game else "pre"

        opponent_espn = None
        if game:
            opponent_espn = (
                str(game["home_espn"])
                if str(game["away_espn"]) == team_espn
                else str(game["away_espn"])
            )

        summary = None
        if game and state in ("in", "post"):
            event_id = game["event_id"]
            if event_id not in summaries:
                try:
                    summaries[event_id] = boxscore.fetch_summary(event_id)
                except Exception as exc:
                    log.error("Box score fetch failed for %s: %s", event_id, exc)
                    summaries[event_id] = None
            summary = summaries[event_id]

        points = yahoo_scoring.score(
            summary, r["slot"], r["athlete_id"], team_espn, opponent_espn
        )
        # Absent from a box score once the game is under way means no
        # stats, which is zero points rather than unknown.
        if points is None and state in ("in", "post"):
            points = 0.0

        projected = None
        if lines is not None:
            pid = projection_id(r)
            parts = None
            if r["slot"].upper() in yahoo_scoring.DEFENSE_SLOTS and summary:
                parts = yahoo_scoring.score_defense(summary, team_espn, opponent_espn)[1]
            projected = projection.projected_final(
                lines.get(str(pid)) if pid is not None else None,
                r["slot"], points, game, parts,
            )

        side = sides[r["side"]]
        side["name"] = r["team_name"]
        side["players"].append({
            "name": r["label"],
            "slot": r["slot"],
            "position": r["slot"],
            "pro_team_espn_id": team_espn,
            "espn_athlete_id": r["athlete_id"],
            "points": points,
            "projected": projected,
            "injury_status": "",
            "is_starter": True,
            "_state": state,
        })

    for side in sides.values():
        side["points"] = round(
            sum(p["points"] or 0 for p in side["players"]), 2
        )
        side["projected"] = (
            round(sum(p["projected"] or 0 for p in side["players"]), 2)
            if lines is not None else None
        )
        side["win_probability"] = None
        side["yet_to_play"] = sum(
            1 for p in side["players"]
            if p["_state"] != "post" and not p["points"]
        )
        for p in side["players"]:
            p.pop("_state", None)

    return {
        "week": week,
        "me": sides["me"],
        "opponent": sides["opponent"],
        "platform": "YAHOO",
        "source": "manual",
        "source_ok": True,
    }
