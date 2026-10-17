"""
Fantasy layer.

Fetches matchups from each platform and joins every player to the live
state of the NFL game their pro team is playing. ESPN's fantasy API
uses the same numeric team ids as its scoreboard API, so that join is
direct.

Results are cached in memory. The dashboard page refreshes far more
often than the underlying data changes, and the fantasy endpoints are
slower than the scoreboard.
"""

import logging
import threading
import time

import db
from clock import format_remaining, is_live, period_label, total_remaining_seconds

log = logging.getLogger("fantasy")

# Seconds before a cached matchup is considered stale.
TTL_LIVE = 45
TTL_IDLE = 600

_cache = {}
_lock = threading.Lock()


def current_week():
    """Week the scoreboard is currently on, or None if nothing stored."""
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT week FROM game
            WHERE season = (SELECT MAX(season) FROM game)
            ORDER BY week DESC LIMIT 1
            """
        ).fetchone()
        return row["week"] if row else None
    finally:
        conn.close()


def any_games_live():
    conn = db.connect()
    try:
        return bool(
            conn.execute("SELECT 1 FROM game WHERE state='in' LIMIT 1").fetchone()
        )
    finally:
        conn.close()


def espn_id_by_abbrev():
    """Every abbreviation any platform uses, mapped to ESPN's team id.

    Yahoo reports a player's pro team as an abbreviation rather than an
    id, and the three platforms disagree on a few of them, so all three
    alias columns are folded into one lookup.
    """
    conn = db.connect()
    try:
        mapping = {}
        for r in conn.execute(
            """SELECT espn_id, abbrev_canonical, abbrev_yahoo,
                      abbrev_espn_fantasy FROM team"""
        ).fetchall():
            for alias in (r["abbrev_canonical"], r["abbrev_yahoo"],
                          r["abbrev_espn_fantasy"]):
                if alias:
                    mapping[alias.upper()] = str(r["espn_id"])
        return mapping
    finally:
        conn.close()


def game_state_by_espn_team():
    """Map every pro team's ESPN id to its current game state this week."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT g.*,
                   ta.espn_id AS away_espn, ta.abbrev_canonical AS away_abbrev,
                   th.espn_id AS home_espn, th.abbrev_canonical AS home_abbrev
            FROM game g
            JOIN team ta ON ta.id = g.away_team_id
            JOIN team th ON th.id = g.home_team_id
            WHERE g.season = (SELECT MAX(season) FROM game)
              AND g.week   = (SELECT MAX(week) FROM game
                              WHERE season = (SELECT MAX(season) FROM game))
            """
        ).fetchall()
    finally:
        conn.close()

    mapping = {}
    for r in rows:
        live = is_live(r["state"], r["status_detail"])
        remaining = total_remaining_seconds(
            r["period"], r["clock_seconds"], r["state"]
        )
        label = period_label(
            r["period"], r["clock_seconds"], r["state"], r["status_detail"]
        )

        for espn_id, own, opp, own_score, opp_score in (
            (r["away_espn"], r["away_abbrev"], r["home_abbrev"],
             r["away_score"], r["home_score"]),
            (r["home_espn"], r["home_abbrev"], r["away_abbrev"],
             r["home_score"], r["away_score"]),
        ):
            mapping[str(espn_id)] = {
                "event_id": r["event_id"],
                "abbrev": own,
                "is_live": live,
                "state": r["state"],
                "label": label,
                "remaining": format_remaining(remaining) if live else None,
                "own_abbrev": own,
                "opp_abbrev": opp,
                "own_score": own_score,
                "opp_score": opp_score,
                "score_line": f"{own} {own_score}, {opp} {opp_score}",
            }
    return mapping


def annotate(players, states):
    """Attach game state to each player, keyed on their pro team."""
    out = []
    for player in players:
        state = states.get(str(player.get("pro_team_espn_id") or ""))
        enriched = dict(player)
        enriched["game"] = state
        enriched["game_is_live"] = bool(state and state["is_live"])
        out.append(enriched)
    return out


def _yahoo_matchup(week):
    """Yahoo's public league. No credentials involved.

    Only the public league is reachable this way. The private one
    returns 401 here and 403 on the authenticated host, so it has no
    path at all.
    """
    import yahoo_public

    matchup = yahoo_public.get_matchup(week)
    if not matchup:
        return None

    roster = yahoo_public.get_roster(week)
    abbrev_to_espn = espn_id_by_abbrev()
    for player in roster:
        player["pro_team_espn_id"] = abbrev_to_espn.get(
            (player.get("pro_team") or "").upper(), ""
        )
        # Yahoo's per-player projections come back empty on this host.
        player["slot"] = player.get("position", "")
        player["injury_status"] = player.get("status", "")

    states = game_state_by_espn_team()
    players = annotate(roster, states)
    starters = [p for p in players if p.get("is_starter")]

    matchup["me"]["players"] = players
    matchup["me"]["yet_to_play"] = sum(1 for p in starters if not p["points"])
    # Yahoo's public host does not publish a live win probability.
    matchup["me"]["win_probability"] = None

    # The opponent's roster is a second request. The head-to-head score
    # is what the card needs, so it is not fetched.
    matchup["opponent"]["players"] = []
    matchup["opponent"]["yet_to_play"] = 0
    matchup["opponent"]["win_probability"] = None

    matchup["platform"] = "YAHOO"
    matchup["source_ok"] = True
    return matchup


def _manual_matchup(week):
    """The private Yahoo league: lineup from a screenshot, scored live."""
    import manual
    matchup = manual.compute_matchup(week)
    if not matchup:
        return None
    # Attach each player's live game, the same as the other sources, so
    # the card can list who is playing right now.
    states = game_state_by_espn_team()
    for side in ("me", "opponent"):
        matchup[side]["players"] = annotate(matchup[side]["players"], states)
    return matchup


# Sources that are optional. When nothing has been imported for the
# week they are left off the page rather than shown as an error.
QUIET_WHEN_EMPTY = {"yahoo_manual"}

# Sources rebuilt on every refresh rather than cached here. The manual
# league reads its lineup from the local database, so an import or a
# lineup change has to show on the next refresh, not minutes later. Its
# slow parts already cache themselves: box scores for 25 seconds and
# projections for 15 minutes.
UNCACHED = {"yahoo_manual"}


def _espn_matchup(week):
    """Uncached ESPN fetch. Raises on failure."""
    import espn_client

    matchup = espn_client.get_matchup(week)
    if not matchup:
        return None

    states = game_state_by_espn_team()
    for side in ("me", "opponent"):
        if matchup[side]:
            matchup[side]["players"] = annotate(
                matchup[side]["players"], states
            )
    matchup["platform"] = "ESPN"
    matchup["source_ok"] = True
    return matchup


def get_matchups(force=False):
    """Every configured fantasy matchup, cached.

    A platform failing returns an entry carrying the error rather than
    taking the whole dashboard down with it.
    """
    week = current_week()
    if week is None:
        return []

    ttl = TTL_LIVE if any_games_live() else TTL_IDLE
    now = time.time()
    results = []

    sources = (
        ("espn", _espn_matchup),
        ("yahoo", _yahoo_matchup),
        ("yahoo_manual", _manual_matchup),
    )
    for key, fetch in sources:
        with _lock:
            cached = None if key in UNCACHED else _cache.get(key)
            fresh = (
                cached
                and not force
                and cached["week"] == week
                and now - cached["at"] < ttl
            )
            if fresh:
                if cached["value"] is not None:
                    results.append(cached["value"])
                continue

        try:
            value = fetch(week)
            if value is None and key in QUIET_WHEN_EMPTY:
                continue
            if value is None:
                value = {
                    "platform": key.upper(),
                    "source_ok": False,
                    "error": f"No matchup found for week {week}.",
                    "week": week,
                }
        except Exception as exc:
            log.error("%s fantasy fetch failed: %s", key, exc)
            value = {
                "platform": key.upper(),
                "source_ok": False,
                "error": str(exc),
                "week": week,
            }

        if key not in UNCACHED:
            with _lock:
                _cache[key] = {"at": now, "week": week, "value": value}
        results.append(value)

    return results
