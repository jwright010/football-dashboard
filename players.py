"""
The merged player view.

One row per human being, gathering every reason you care about them:
a starting slot on any fantasy team, a rooting goal, or both. When a
player is both, the rooting goal leads and the fantasy lines nest
underneath it.

Merging is keyed on ESPN athlete id, which works because ESPN Fantasy
reports the same athlete ids the box score uses. Yahoo uses its own ids,
so Yahoo players merge on a normalized name instead. Any player who
fails to merge simply appears as their own row, which is a visible
miss rather than a silent wrong one.

Fantasy points are each league's own number, exactly as that platform
registers it. Two leagues with different settings will therefore show
different totals for the same player, which is correct: those are the
points that actually count in each matchup.
"""

import logging

import db
import fantasy
import pins
import rooting
import roster

log = logging.getLogger("players")

# Where a player's game is, for sorting and display.
STATE_ORDER = {"in": 0, "pre": 1, "post": 2}


def _espn_id_by_team():
    conn = db.connect()
    try:
        return {
            r["abbrev_canonical"]: str(r["espn_id"])
            for r in conn.execute(
                "SELECT abbrev_canonical, espn_id FROM team"
            ).fetchall()
        }
    finally:
        conn.close()


def _abbrev_by_espn_id():
    conn = db.connect()
    try:
        return {
            str(r["espn_id"]): r["abbrev_canonical"]
            for r in conn.execute(
                "SELECT abbrev_canonical, espn_id FROM team"
            ).fetchall()
        }
    finally:
        conn.close()


DEFENSE_POSITIONS = {"DEF", "D/ST", "DST"}


def _merge_key(entry):
    """Normalized name plus pro team.

    Keying on athlete id does not work across platforms: ESPN supplies
    one and Yahoo does not, so the same person arrives under two
    different keys and never merges. Name alone risks colliding two
    different players; name and team together does not, because two
    people with the same normalized name on the same NFL team is not a
    real situation.

    The athlete id is then an attribute, filled by whichever source
    has it, rather than the thing identity depends on.
    """
    team = entry.get("pro_team_espn_id") or ""
    # A team defense is named differently on every platform ('Texans',
    # 'Texans D/ST'), so it merges on the team alone.
    if (entry.get("position") or "").upper() in DEFENSE_POSITIONS:
        return f"defense|{team}"
    name = roster.normalize(entry.get("name", ""))
    return f"{name}|{team}"


def build():
    """Every active player, merged. Returns None when there is nothing."""
    week_row = _current_week()
    if not week_row:
        return None
    season, week = week_row

    states = fantasy.game_state_by_espn_team()
    merged = {}

    # --- fantasy starters -------------------------------------------
    for matchup in fantasy.get_matchups():
        if not matchup.get("source_ok") or not matchup.get("me"):
            continue
        platform = matchup.get("platform", "?")
        team_name = matchup["me"].get("name", "")

        for player in matchup["me"]["players"]:
            if not player.get("is_starter"):
                continue
            # Only ESPN's ids are ESPN athlete ids. A Yahoo player id is
            # a different namespace and could collide, so Yahoo players
            # merge on a normalized name instead.
            espn_athlete_id = player.get("espn_athlete_id") or (
                str(player["player_id"])
                if platform == "ESPN" and player.get("player_id")
                else None
            )
            entry = {
                "athlete_id": espn_athlete_id,
                "name": player.get("name", ""),
                "position": player.get("position") or player.get("slot") or "",
                "pro_team_espn_id": str(player.get("pro_team_espn_id") or ""),
            }
            key = _merge_key(entry)
            row = merged.setdefault(key, _blank(entry))
            # Whichever platform supplies an ESPN athlete id wins; the
            # others simply have none to give.
            if entry["athlete_id"] and not row["athlete_id"]:
                row["athlete_id"] = entry["athlete_id"]
            row["fantasy"].append({
                "platform": platform,
                "team_name": team_name,
                "slot": player.get("slot", ""),
                "platform_points": player.get("points"),
                "projected": player.get("projected"),
                "injury": player.get("injury_status", ""),
            })
            if not row["position"]:
                row["position"] = entry["position"]

    # --- rooting interests ------------------------------------------
    espn_by_abbrev = _espn_id_by_team()
    detail = rooting.week_detail(season, week)
    for item in detail["items"]:
        if item["kind"] != "player":
            continue
        entry = {
            "athlete_id": (
                str(item["athlete_id"]) if item.get("athlete_id") else None
            ),
            "name": item["label"],
            "position": "",
            "pro_team_espn_id": espn_by_abbrev.get(item["team"], ""),
        }
        key = _merge_key(entry)
        row = merged.setdefault(key, _blank(entry))
        if entry["athlete_id"] and not row["athlete_id"]:
            row["athlete_id"] = entry["athlete_id"]
        row["goals"].append({
            "id": item["id"],
            "event_id": item.get("event_id"),
            "pace": item.get("pace"),
            "goal_text": item["goal_text"],
            "stat": item["stat"],
            "direction": item["direction"],
            "target": item["target"],
            "target_text": item["target_text"],
            "current": item["current"],
            "status": item["status"],
            "progress": item["progress"],
            "percent": round(item["progress"] * 100),
        })

    if not merged:
        return None


    abbrev_by_id = _abbrev_by_espn_id()

    rows = []
    for row in merged.values():
        # Carried on the row itself so a player whose game is missing
        # still gets a logo.
        row["team_abbrev"] = abbrev_by_id.get(row["pro_team_espn_id"], "")
        state = states.get(row["pro_team_espn_id"])
        row["game"] = state
        row["is_live"] = bool(state and state["is_live"])
        row["game_state"] = state["state"] if state else "pre"
        # Highest platform score across this player's fantasy teams,
        # used only for sorting.
        row["_top"] = max(
            [f["platform_points"] or 0 for f in row["fantasy"]] or [0]
        )
        rows.append(row)

    for row in rows:
        # Every fantasy team and every rooting goal is its own interest.
        row["interests"] = len(row["fantasy"]) + len(row["goals"])

    # 1. game state: live, then not started
    # 2. total interests
    # 3. rooting goals
    # 4. fantasy teams. Once 2 and 3 tie this is already equal, since
    #    interests are the two added together; kept to record the intent.
    # Then fantasy points, then name.
    rows.sort(key=lambda r: (
        STATE_ORDER.get(r["game_state"], 3),
        -r["interests"],
        -len(r["goals"]),
        -len(r["fantasy"]),
        -r["_top"],
        r["name"],
    ))
    for row in rows:
        row.pop("_top", None)

    pinned_keys = pins.current(season, week)
    for row in rows:
        row["pinned"] = row["key"] in pinned_keys

    # A player whose game has ended has nothing left to watch, so this
    # section drops them. They stay in the rooting card, which is the
    # record of how each goal finished.
    active = [r for r in rows if r["game_state"] != "post"]
    finished = len(rows) - len(active)
    # The Standard view shows only players with fantasy stakes in Your
    # players, so it needs this count separately.
    finished_fantasy = sum(
        1 for r in rows if r["game_state"] == "post" and r["fantasy"]
    )

    # Pinned players keep their place on Top Players after the final
    # whistle, so this list includes finished games. It follows the same
    # order as everything else: live, then not started, then finished.
    pinned = [r for r in rows if r["pinned"]]

    return {
        "season": season,
        "week": week,
        "players": active,
        "pinned": pinned,
        # Every row, finished games included. The phone version keeps pins
        # in the browser, so it needs the full list to find pinned players
        # whose games have ended.
        "all": rows,
        "total": len(active),
        "finished": finished,
        "finished_fantasy": finished_fantasy,
        "with_goals": sum(1 for r in active if r["goals"]),
        "live": sum(1 for r in active if r["is_live"]),
    }


def annotate_rooting(detail):
    """Give each player goal the same key and pin state the player view uses.

    The Rooting for card pins players too, and a pin has to mean the same
    person wherever it is clicked, so the key comes from the one place
    that defines it.
    """
    if not detail:
        return detail
    espn_by_abbrev = _espn_id_by_team()
    pinned = pins.current(detail["season"], detail["week"])
    for item in detail["items"]:
        if item["kind"] != "player":
            continue
        key = _merge_key({
            "name": item["label"],
            "position": "",
            "pro_team_espn_id": espn_by_abbrev.get(item["team"], ""),
        })
        item["player_key"] = key
        item["pinned"] = key in pinned
    return detail


def _blank(entry):
    return {
        # Stable across refreshes, so the page can tell what changed.
        "key": _merge_key(entry),
        "athlete_id": entry["athlete_id"],
        "name": entry["name"],
        "position": entry["position"],
        "pro_team_espn_id": entry["pro_team_espn_id"],
        "fantasy": [],
        "goals": [],
    }


def _current_week():
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT season, week FROM game
            WHERE season = (SELECT MAX(season) FROM game)
            ORDER BY week DESC LIMIT 1
            """
        ).fetchone()
        return (row["season"], row["week"]) if row else None
    finally:
        conn.close()
