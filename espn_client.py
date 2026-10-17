"""
ESPN Fantasy Football client.

The endpoint is undocumented and has moved hosts before, so every field
access is defensive and the raw responses are saved for inspection.

Useful property: ESPN's fantasy API identifies pro teams with the same
numeric ids its scoreboard API uses, so player teams join straight to
the team table already in the database.

Run directly to check the connection:

    python espn_client.py 3
"""

import json
import os
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx
from dotenv import load_dotenv

load_dotenv()

LEAGUE_ID = os.getenv("ESPN_LEAGUE_ID")
TEAM_ID = os.getenv("ESPN_TEAM_ID")
SEASON = os.getenv("ESPN_SEASON_ID", "2026")
SWID = os.getenv("ESPN_SWID", "")
ESPN_S2 = os.getenv("ESPN_S2", "")

HOST = "https://lm-api-reads.fantasy.espn.com"
DEBUG_DIR = Path(__file__).parent / "debug"

# lineupSlotId values that mean the player is not in the starting lineup.
BENCH_SLOTS = {20, 21}

SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE",
    6: "TE", 7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL",
    12: "CB", 13: "S", 14: "DB", 15: "DP", 16: "D/ST", 17: "K",
    18: "P", 19: "HC", 20: "BE", 21: "IR", 23: "FLEX",
}

POSITION_NAMES = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST",
}

# statSourceId: 0 is what actually happened, 1 is ESPN's projection.
ACTUAL, PROJECTED = 0, 1


def _cookies():
    if not SWID or not ESPN_S2:
        return None
    swid = SWID if SWID.startswith("{") else "{" + SWID + "}"
    return {"SWID": swid, "espn_s2": ESPN_S2}


def get_league(views, week=None, debug_name=None):
    """Fetch the league with one or more views applied."""
    if not LEAGUE_ID:
        raise RuntimeError("ESPN_LEAGUE_ID missing from .env")

    url = (
        f"{HOST}/apis/v3/games/ffl/seasons/{SEASON}"
        f"/segments/0/leagues/{LEAGUE_ID}"
    )
    params = [("view", v) for v in views]
    if week is not None:
        params.append(("scoringPeriodId", str(week)))

    response = httpx.get(
        url,
        params=params,
        cookies=_cookies(),
        headers={
            "User-Agent": "football-dashboard/1.0",
            "Accept": "application/json",
        },
        timeout=30.0,
        follow_redirects=True,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "ESPN returned 401. The league is private and the cookies are "
            "missing or expired. Re-copy espn_s2 and SWID into .env."
        )
    if response.status_code == 404:
        raise RuntimeError(
            f"ESPN returned 404 for league {LEAGUE_ID} season {SEASON}. "
            "Check ESPN_LEAGUE_ID and ESPN_SEASON_ID in .env."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"ESPN returned {response.status_code}\n{response.text[:400]}"
        )

    payload = response.json()
    # Some views return a single-element list rather than an object.
    if isinstance(payload, list):
        payload = payload[0] if payload else {}

    if debug_name:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{debug_name}.json").write_text(json.dumps(payload, indent=2))

    return payload


def team_name(team):
    """ESPN has used several shapes for team names across seasons."""
    name = team.get("name")
    if name:
        return name.strip()
    parts = [team.get("location", ""), team.get("nickname", "")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or team.get("abbrev") or f"Team {team.get('id')}"


def list_teams(debug=False):
    payload = get_league(["mTeam"], debug_name="espn_teams" if debug else None)
    teams = []
    for team in payload.get("teams", []):
        teams.append({
            "team_id": team.get("id"),
            "name": team_name(team),
            "abbrev": team.get("abbrev", ""),
            "wins": (team.get("record", {}).get("overall", {}) or {}).get("wins"),
            "losses": (team.get("record", {}).get("overall", {}) or {}).get("losses"),
            "standing": team.get("playoffSeed") or team.get("rankCalculatedFinal"),
        })
    return teams


def _stat_total(player, week, source):
    """Pull a player's weekly actual or projected total.

    statSplitTypeId 1 means a single week rather than a season total.
    """
    for stat in player.get("stats", []) or []:
        if stat.get("scoringPeriodId") != week:
            continue
        if stat.get("statSourceId") != source:
            continue
        if stat.get("statSplitTypeId") not in (1, None):
            continue
        value = stat.get("appliedTotal")
        if value is not None:
            return round(float(value), 2)
    return 0.0


def parse_entry(entry, week):
    """Turn one roster entry into a flat player dict."""
    pool = entry.get("playerPoolEntry") or {}
    player = pool.get("player") or entry.get("player") or {}

    slot_id = entry.get("lineupSlotId")
    position_id = player.get("defaultPositionId")

    return {
        "player_id": player.get("id"),
        "name": player.get("fullName") or "Unknown",
        "slot": SLOT_NAMES.get(slot_id, str(slot_id)),
        "position": POSITION_NAMES.get(position_id, ""),
        "pro_team_espn_id": str(player.get("proTeamId") or ""),
        "points": _stat_total(player, week, ACTUAL),
        "projected": _stat_total(player, week, PROJECTED),
        "injury_status": player.get("injuryStatus") or "",
        "is_starter": slot_id not in BENCH_SLOTS,
    }


def _first_number(block, *keys):
    """First key holding a non-zero number, else 0.0."""
    for key in keys:
        value = block.get(key)
        if value:
            try:
                return round(float(value), 2)
            except (TypeError, ValueError):
                continue
    return 0.0


def _side(block, week, teams_by_id):
    if not block:
        return None
    roster = (
        block.get("rosterForCurrentScoringPeriod")
        or block.get("rosterForMatchupPeriod")
        or block.get("rosterForMatchupPeriodDelayed")
        or {}
    )
    players = [parse_entry(e, week) for e in roster.get("entries", []) or []]
    starters = [p for p in players if p["is_starter"]]

    # ESPN keeps the in-progress total separate from the settled one,
    # and both read zero before any game starts. Summing the starters
    # is the fallback.
    points = _first_number(block, "totalPointsLive", "totalPoints")
    if not points:
        points = round(sum(p["points"] for p in starters), 2)

    # ESPN's own live projection already blends actual and projected,
    # so prefer it over recomputing.
    projected = _first_number(
        block, "totalProjectedPointsLive", "totalProjectedPoints"
    )
    if not projected:
        projected = round(
            sum(p["points"] if p["points"] else p["projected"]
                for p in starters), 2
        )

    win_probability = block.get("winProbability")

    team_id = block.get("teamId")
    return {
        "team_id": team_id,
        "name": teams_by_id.get(team_id, f"Team {team_id}"),
        "points": points,
        "projected": projected,
        "win_probability": round(float(win_probability), 3)
            if win_probability is not None else None,
        "yet_to_play": sum(1 for p in starters if p["points"] == 0),
        "players": players,
    }


def get_matchup(week, team_id=None, debug=False):
    """Head-to-head detail for one week, including both rosters."""
    team_id = int(team_id or TEAM_ID)
    # Including mRoster causes ESPN to strip player objects down to
    # stats only, with no name or team. These four views return full
    # players plus the live score fields.
    payload = get_league(
        ["mMatchupScore", "mScoreboard", "mSettings", "mTeam"],
        week=week,
        debug_name=f"espn_matchup_w{week}" if debug else None,
    )

    teams_by_id = {
        t.get("id"): team_name(t) for t in payload.get("teams", []) or []
    }

    for game in payload.get("schedule", []) or []:
        if game.get("matchupPeriodId") != week:
            continue
        home = game.get("home") or {}
        away = game.get("away") or {}
        if team_id not in (home.get("teamId"), away.get("teamId")):
            continue

        mine_block = home if home.get("teamId") == team_id else away
        opp_block = away if home.get("teamId") == team_id else home

        return {
            "week": week,
            "me": _side(mine_block, week, teams_by_id),
            "opponent": _side(opp_block, week, teams_by_id),
        }
    return None


if __name__ == "__main__":
    week = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    missing = [
        name for name, value in (
            ("ESPN_LEAGUE_ID", LEAGUE_ID),
            ("ESPN_TEAM_ID", TEAM_ID),
        ) if not value
    ]
    if missing:
        print("Missing from .env: " + ", ".join(missing))
        sys.exit(1)

    print(f"League {LEAGUE_ID}, season {SEASON}, "
          f"cookies {'present' if _cookies() else 'absent'}\n")

    try:
        teams = list_teams(debug=True)
    except Exception as exc:
        print(f"Failed: {exc}")
        sys.exit(1)

    print(f"{len(teams)} teams in the league:")
    for team in teams:
        mine = "  <- you" if str(team["team_id"]) == str(TEAM_ID) else ""
        record = ""
        if team["wins"] is not None:
            record = f"  {team['wins']}-{team['losses']}"
        print(f"  {team['team_id']:>3}  {team['name']}{record}{mine}")

    print(f"\nWeek {week} matchup:\n")
    try:
        matchup = get_matchup(week, debug=True)
    except Exception as exc:
        print(f"Failed: {exc}")
        sys.exit(1)

    if not matchup:
        print(f"  No matchup found for week {week}.")
        print("  Raw response saved to debug/espn_matchup_w%d.json" % week)
        sys.exit(0)

    for label in ("me", "opponent"):
        side = matchup[label]
        if not side:
            continue
        chance = ""
        if side["win_probability"] is not None:
            chance = f"  win {side['win_probability'] * 100:.0f}%"
        print(f"  {side['name']}   {side['points']:.2f} "
              f"(proj {side['projected']:.2f}){chance}"
              f"   {side['yet_to_play']} starters yet to score")
        starters = [p for p in side["players"] if p["is_starter"]]
        for p in starters:
            flag = f" [{p['injury_status'][:3]}]" if p["injury_status"] not in ("", "ACTIVE") else ""
            print(f"      {p['slot']:5} {p['name']:22} "
                  f"{p['points']:6.2f}  proj {p['projected']:6.2f}{flag}")
        print()

    print("Raw responses saved to the debug folder.")
