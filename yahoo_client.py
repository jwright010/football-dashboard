"""
Yahoo Fantasy API client.

Yahoo returns JSON that is really XML in disguise: attributes arrive as
arrays of single-key objects, collections are keyed by stringified
integers alongside a 'count', and the nesting depth varies by endpoint.
Rather than follow fixed paths, this module searches for the structures
it needs, which survives Yahoo's shape changes.

Run directly to discover your teams:

    python yahoo_client.py
"""

import json
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx

from yahoo_auth import get_access_token

BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
DEBUG_DIR = Path(__file__).parent / "debug"


def get(path, debug_name=None):
    """Authenticated GET against the Fantasy API, returning parsed JSON."""
    token = get_access_token()
    separator = "&" if "?" in path else "?"
    url = f"{BASE}/{path}{separator}format=json"

    response = httpx.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "football-dashboard/1.0",
        },
        timeout=30.0,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Yahoo rejected the token (401). Delete yahoo_token.json and "
            "re-run: python yahoo_auth.py"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Yahoo returned {response.status_code} for {path}\n"
            f"{response.text[:500]}"
        )

    payload = response.json()

    if debug_name:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{debug_name}.json").write_text(
            json.dumps(payload, indent=2)
        )

    return payload


# ---------------------------------------------------------------------
# Yahoo JSON helpers
# ---------------------------------------------------------------------

def walk(node):
    """Yield every dict and list nested anywhere inside node."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        yield node
        for item in node:
            yield from walk(item)


def flatten(node):
    """Merge every dict inside node into one flat dict.

    Yahoo spreads an entity's attributes across a list of single-key
    dicts. Flattening recovers them. Nested collections are skipped so
    a team's attributes do not absorb its players.
    """
    result = {}

    def visit(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ("players", "teams", "matchups", "roster"):
                    continue
                if isinstance(value, (dict, list)):
                    if key in ("name", "team_points", "team_projected_points",
                               "player_points", "player_projected_points",
                               "selected_position", "team_logos"):
                        result[key] = value
                    else:
                        visit(value)
                else:
                    result[key] = value
        elif isinstance(item, list):
            for sub in item:
                visit(sub)

    visit(node)
    return result


def collection(container, singular):
    """Pull items out of a Yahoo numeric-keyed collection.

    Yahoo shapes collections as
        {"0": {"team": [...]}, "1": {"team": [...]}, "count": 2}
    """
    items = []
    if not isinstance(container, dict):
        return items
    for key, value in container.items():
        if key == "count" or not key.isdigit():
            continue
        if isinstance(value, dict) and singular in value:
            items.append(value[singular])
    return items


def find_collections(payload, plural, singular):
    """Find every collection of the given kind anywhere in the payload."""
    found = []
    for node in walk(payload):
        if isinstance(node, dict) and plural in node:
            items = collection(node[plural], singular)
            if items:
                found.append(items)
    return found


def points_of(block):
    """Yahoo wraps point totals as {'total': '87.42', ...}."""
    if isinstance(block, dict):
        value = block.get("total")
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def name_of(block):
    if isinstance(block, dict):
        return block.get("full") or block.get("first") or ""
    return block or ""


# ---------------------------------------------------------------------
# Public calls
# ---------------------------------------------------------------------

def discover_teams(debug=False):
    """Every NFL fantasy team on the authorized account.

    Using the 'nfl' game alias avoids hardcoding a season game key,
    which changes every year.
    """
    payload = get(
        "users;use_login=1/games;game_keys=nfl/teams",
        debug_name="discover_teams" if debug else None,
    )

    teams = []
    seen = set()
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
                "league_key": ".".join(key.split(".")[:3]) if key else None,
            })
    return teams


def get_matchup(team_key, week, debug=False):
    """Head-to-head score and projection for one team's week."""
    payload = get(
        f"team/{team_key}/matchups;weeks={week}",
        debug_name=f"matchup_{team_key}_{week}" if debug else None,
    )

    groups = find_collections(payload, "teams", "team")
    pair = next((g for g in groups if len(g) == 2), None)
    if not pair:
        return None

    sides = []
    for node in pair:
        flat = flatten(node)
        sides.append({
            "team_key": flat.get("team_key"),
            "name": name_of(flat.get("name")) or flat.get("name"),
            "points": points_of(flat.get("team_points")),
            "projected": points_of(flat.get("team_projected_points")),
        })

    mine = next((s for s in sides if s["team_key"] == team_key), sides[0])
    opponent = next((s for s in sides if s["team_key"] != team_key), None)
    return {"week": week, "me": mine, "opponent": opponent}


def get_roster(team_key, week, debug=False):
    """Per-player points for one team's week."""
    payload = get(
        f"team/{team_key}/roster;week={week}/players/stats;type=week;week={week}",
        debug_name=f"roster_{team_key}_{week}" if debug else None,
    )

    players = []
    seen = set()
    for group in find_collections(payload, "players", "player"):
        for node in group:
            flat = flatten(node)
            key = flat.get("player_key")
            if not key or key in seen:
                continue
            seen.add(key)

            position_block = flat.get("selected_position")
            position = ""
            if isinstance(position_block, (dict, list)):
                position = flatten(position_block).get("position", "")

            players.append({
                "player_key": key,
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
    week = int(sys.argv[1]) if len(sys.argv) > 1 else None

    try:
        teams = discover_teams(debug=True)
    except Exception as exc:
        print(f"Discovery failed: {exc}")
        sys.exit(1)

    if not teams:
        print(
            "No NFL teams found on this Yahoo account.\n"
            "Raw response saved to debug/discover_teams.json"
        )
        sys.exit(1)

    print(f"Found {len(teams)} team(s):\n")
    for team in teams:
        print(f"  team_key: {team['team_key']}")
        print(f"  name:     {team['name']}")
        print(f"  league:   {team['league_key']}\n")

    print("Add these to .env:\n")
    for index, team in enumerate(teams, start=1):
        print(f"YAHOO_TEAM_{index}_KEY={team['team_key']}")

    if week:
        print(f"\nWeek {week} detail:\n")
        for team in teams:
            matchup = get_matchup(team["team_key"], week, debug=True)
            if matchup and matchup["opponent"]:
                me, opp = matchup["me"], matchup["opponent"]
                print(f"  {me['name']}  {me['points']:.2f} "
                      f"(proj {me['projected']:.2f})")
                print(f"  {opp['name']}  {opp['points']:.2f} "
                      f"(proj {opp['projected']:.2f})\n")
            else:
                print(f"  {team['name']}: no matchup found for week {week}\n")

    print("Raw responses saved to the debug folder.")
