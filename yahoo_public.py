"""
Yahoo Fantasy, public league, no credentials.

Yahoo serves the full Fantasy API for public leagues from
pub-api-rw.fantasysports.yahoo.com with no token, no crumb, and no
cookies. That is the host its own web app calls.

This is a different door from fantasysports.yahooapis.com, which
returns 403 for every endpoint on this account, including ones that
need no permission at all. That is an app entitlement problem on
Yahoo's side, not something configuration can fix.

Reach, and the limit of it:
  league 1335544 is public  -> everything below works
  league 752951 is private  -> 401 here, 403 there, no path

The response shape is identical to the authenticated API, so the
parsing helpers in yahoo_client are reused unchanged.

Run directly to check the connection:

    python yahoo_public.py 2
"""

import json
import logging
import os
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx
from dotenv import load_dotenv

from yahoo_client import (
    collection,
    find_collections,
    flatten,
    name_of,
    points_of,
)

load_dotenv()

BASE = "https://pub-api-rw.fantasysports.yahoo.com/fantasy/v2"
DEBUG_DIR = Path(__file__).parent / "debug"

log = logging.getLogger("yahoo_public")

# Yahoo serves this host to its own web client, so it expects to look
# like the web client.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://football.fantasysports.yahoo.com/",
}

LEAGUE_ID = os.getenv("YAHOO_PUBLIC_LEAGUE_ID") or os.getenv("YAHOO_LEAGUE_2_ID")
TEAM_ID = os.getenv("YAHOO_PUBLIC_TEAM_ID") or os.getenv("YAHOO_TEAM_2_ID")


def league_key(league_id=None):
    return f"nfl.l.{league_id or LEAGUE_ID}"


def team_key(league_id=None, team_id=None):
    return f"{league_key(league_id)}.t.{team_id or TEAM_ID}"


def _same_team(key, team_id):
    """Match a returned team_key against a team id.

    Requests accept 'nfl' as the game alias, but responses come back
    with the real season game key, so '461.l.123.t.4' never equals a
    key built from 'nfl'. Only the trailing team id is comparable.
    """
    if not key:
        return False
    tail = str(key).rsplit(".t.", 1)
    return len(tail) == 2 and tail[1] == str(team_id)


def get(path, debug_name=None):
    """Unauthenticated GET against the public Fantasy API."""
    separator = "&" if "?" in path else "?"
    url = f"{BASE}/{path}{separator}format=json"

    response = httpx.get(url, headers=HEADERS, timeout=30.0,
                         follow_redirects=False)

    if response.status_code == 401:
        raise RuntimeError(
            f"Yahoo returned 401 for {path}. That league is private, so "
            "this route cannot reach it."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Yahoo returned {response.status_code} for {path}\n"
            f"{response.text[:300]}"
        )

    payload = response.json()
    if debug_name:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{debug_name}.json").write_text(json.dumps(payload, indent=2))
    return payload


def league_info(league_id=None, debug=False):
    payload = get(f"league/{league_key(league_id)}",
                  debug_name="yp_league" if debug else None)
    for node in payload.get("fantasy_content", {}).values():
        if isinstance(node, (dict, list)):
            flat = flatten(node)
            if flat.get("league_key"):
                return {
                    "league_key": flat.get("league_key"),
                    "name": flat.get("name"),
                    "current_week": flat.get("current_week"),
                    "num_teams": flat.get("num_teams"),
                    "scoring_type": flat.get("scoring_type"),
                }
    return None


def list_teams(league_id=None, debug=False):
    payload = get(f"league/{league_key(league_id)}/teams",
                  debug_name="yp_teams" if debug else None)
    teams, seen = [], set()
    for group in find_collections(payload, "teams", "team"):
        for node in group:
            flat = flatten(node)
            key = flat.get("team_key")
            if not key or key in seen:
                continue
            seen.add(key)
            teams.append({
                "team_key": key,
                "team_id": flat.get("team_id"),
                "name": name_of(flat.get("name")) or flat.get("name"),
            })
    return teams


def get_matchup(week, league_id=None, team_id=None, debug=False):
    """Head-to-head for one team's week, from the league scoreboard."""
    wanted = str(team_id or TEAM_ID)
    payload = get(
        f"league/{league_key(league_id)}/scoreboard;week={week}",
        debug_name=f"yp_scoreboard_w{week}" if debug else None,
    )

    for group in find_collections(payload, "teams", "team"):
        if len(group) != 2:
            continue
        sides = []
        for node in group:
            flat = flatten(node)
            sides.append({
                "team_key": flat.get("team_key"),
                "name": name_of(flat.get("name")) or flat.get("name"),
                "points": points_of(flat.get("team_points")),
                "projected": points_of(flat.get("team_projected_points")),
            })
        if not any(_same_team(s["team_key"], wanted) for s in sides):
            continue

        mine = next(s for s in sides if _same_team(s["team_key"], wanted))
        opponent = next(s for s in sides if not _same_team(s["team_key"], wanted))
        return {"week": week, "me": mine, "opponent": opponent}
    return None


def get_roster(week, league_id=None, team_id=None, debug=False):
    """Per-player points for one team's week."""
    key = team_key(league_id, team_id)
    payload = get(
        f"team/{key}/roster;week={week}/players/stats;type=week;week={week}",
        debug_name=f"yp_roster_w{week}" if debug else None,
    )

    players, seen = [], set()
    for group in find_collections(payload, "players", "player"):
        for node in group:
            flat = flatten(node)
            player_key = flat.get("player_key")
            if not player_key or player_key in seen:
                continue
            seen.add(player_key)

            position_block = flat.get("selected_position")
            position = ""
            if isinstance(position_block, (dict, list)):
                position = flatten(position_block).get("position", "")

            players.append({
                "player_key": player_key,
                "player_id": flat.get("player_id"),
                "name": name_of(flat.get("name")),
                "position": position,
                "pro_team": (flat.get("editorial_team_abbr") or "").upper(),
                "points": points_of(flat.get("player_points")),
                "projected": points_of(flat.get("player_projected_points")),
                "status": flat.get("status") or "",
                "is_starter": position not in ("BN", "IR", "IR+", ""),
            })
    return players


if __name__ == "__main__":
    week = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    if not LEAGUE_ID:
        print("No league id. Set YAHOO_LEAGUE_2_ID in .env")
        sys.exit(1)

    print(f"Public league {LEAGUE_ID}, no credentials used.\n")

    try:
        info = league_info(debug=True)
    except Exception as exc:
        print(f"Failed: {exc}")
        sys.exit(1)

    if info:
        print(f"  league:  {info['name']}")
        print(f"  teams:   {info['num_teams']}")
        print(f"  scoring: {info['scoring_type']}")
        print(f"  week:    {info['current_week']}\n")

    teams = list_teams(debug=True)
    print(f"{len(teams)} teams:")
    for team in teams:
        mine = "  <- you" if str(team["team_id"]) == str(TEAM_ID) else ""
        print(f"  {team['team_id']:>3}  {team['name']}{mine}")

    if not any(str(t["team_id"]) == str(TEAM_ID) for t in teams):
        print(f"\n  WARNING: YAHOO_TEAM_2_ID is {TEAM_ID!r}, which is not in "
              "that list. Set it to your team id from the list above.")

    print(f"\nWeek {week} matchup:\n")
    matchup = get_matchup(week, debug=True)
    if not matchup:
        print(f"  No matchup found for week {week}.")
        sys.exit(0)

    for side in ("me", "opponent"):
        data = matchup[side]
        print(f"  {data['name']}   {data['points']:.2f} "
              f"(proj {data['projected']:.2f})")

    print("\nRoster:\n")
    try:
        roster = get_roster(week, debug=True)
    except Exception as exc:
        print(f"  Roster failed: {exc}")
        sys.exit(0)

    for player in roster:
        if not player["is_starter"]:
            continue
        flag = f" [{player['status']}]" if player["status"] else ""
        print(f"  {player['position']:5} {player['name']:24} "
              f"{player['pro_team']:4} {player['points']:6.2f}  "
              f"proj {player['projected']:6.2f}{flag}")

    print("\nRaw responses saved under debug/")
