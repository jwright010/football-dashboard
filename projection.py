"""
Projected finals for the private Yahoo league (Sunscreen and Tums).

ESPN publishes a projected stat line per player per week. This fetches
those lines and rescores them with that league's own rules, then blends
them with what has already happened:

    not started   the projection
    in progress   actual so far + projection x share of game remaining
    finished      the actual

ESPN identifies each stat by a numeric code. The codes below were
checked by recomputing ESPN's own point totals from its raw stat lines:
a quarterback matched to 0.01, a running back to the cent, and a kicker
within 0.02. Defense codes are internally consistent (sack halves are
exactly double the sacks, the points-allowed buckets sum to 1) but could
not be checked against a total, since the ESPN league's own defense
settings are unknown. Treat those as strongly inferred.
"""

import json
import logging
import threading
import time

import truststore
truststore.inject_into_ssl()

import httpx

import espn_client
from clock import REGULATION_SECONDS, total_remaining_seconds

log = logging.getLogger("projection")

TTL_SECONDS = 15 * 60
_cache = {}
_lock = threading.Lock()

HEADERS = {"User-Agent": "football-dashboard/1.0", "Accept": "application/json"}

# ---------------------------------------------------------------------
# ESPN stat codes -> this league's points
# ---------------------------------------------------------------------

SKILL = {
    "3": 0.04,     # passing yards
    "4": 4.0,      # passing touchdowns
    "20": -1.0,    # interceptions thrown
    "19": 2.0,     # passing two-point conversions
    "24": 0.1,     # rushing yards
    "25": 6.0,     # rushing touchdowns
    "26": 2.0,     # rushing two-point conversions
    "53": 0.5,     # receptions
    "42": 0.1,     # receiving yards
    "43": 6.0,     # receiving touchdowns
    "44": 2.0,     # receiving two-point conversions
    "72": -2.0,    # fumbles lost
    "101": 6.0,    # kickoff return touchdowns
    "102": 6.0,    # punt return touchdowns
}

KICKER = {
    "214": 0.1,    # total yards of made field goals
    "86": 1.0,     # PATs made
    "85": -1.0,    # field goals missed
    "88": -1.0,    # PATs missed
}

DEFENSE = {
    "99": 1.0,     # sacks
    "95": 2.0,     # interceptions
    "96": 2.0,     # fumble recoveries
    "105": 6.0,    # defensive and special teams touchdowns
    "98": 2.0,     # safeties
    "97": 2.0,     # blocked kicks
}

# ESPN's points-allowed buckets, as probabilities, with this league's value
# for each. ESPN's 18-21 bucket straddles the league's 14-20 and 21-27
# tiers, so three quarters of it scores as 14-20 and a quarter as 21-27.
POINTS_ALLOWED_BUCKETS = {
    "89": 10.0,               # 0
    "90": 7.0,                # 1-6
    "91": 4.0,                # 7-13
    "92": 1.0,                # 14-17
    "121": 0.75 * 1.0,        # 18-21
    "122": 0.0,               # 22-27
    "123": -1.0,              # 28-34
    "124": -4.0,              # 35-45
    "125": -4.0,              # 46 and up
}
EXPECTED_POINTS_ALLOWED = "120"

KICKER_SLOTS = {"K", "PK"}
DEFENSE_SLOTS = {"DEF", "D/ST", "DST"}


def defense_id(espn_team_id):
    """ESPN's id for a team defense, e.g. the Rams (14) are -16014."""
    return -(16000 + int(espn_team_id))


# ---------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------

def fetch(ids, week):
    """Projected stat lines for these ESPN ids, keyed by id as a string."""
    ids = sorted({int(i) for i in ids if i is not None})
    if not ids:
        return {}
    key = (week, tuple(ids))
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached["at"] < TTL_SECONDS:
            return cached["value"]

    url = (f"{espn_client.HOST}/apis/v3/games/ffl/seasons/{espn_client.SEASON}"
           f"/segments/0/leagues/{espn_client.LEAGUE_ID}")
    headers = dict(HEADERS)
    # Filtering on ids alone returns the current week's projections. Adding
    # the scoring-period filter swaps them for season actuals instead.
    headers["x-fantasy-filter"] = json.dumps({"players": {"filterIds": {"value": ids}}})
    response = httpx.get(url, params={"view": "kona_playercard"}, headers=headers,
                         cookies=espn_client._cookies(), timeout=30.0,
                         follow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f"ESPN projections returned {response.status_code}")

    lines = {}
    for entry in response.json().get("players") or []:
        node = entry.get("player") or entry
        for stat in node.get("stats") or []:
            if (stat.get("scoringPeriodId") == week and stat.get("statSourceId") == 1
                    and stat.get("statSplitTypeId") in (1, None)):
                lines[str(node.get("id"))] = stat.get("stats") or {}
                break

    with _lock:
        _cache[key] = {"at": now, "value": lines}
    return lines


# ---------------------------------------------------------------------
# Scoring a projected line
# ---------------------------------------------------------------------

def _weighted(stats, weights, share=1.0):
    return sum(float(stats.get(code, 0) or 0) * w for code, w in weights.items()) * share


def _points_allowed_value(allowed):
    if allowed <= 0:
        return 10.0
    if allowed <= 6:
        return 7.0
    if allowed <= 13:
        return 4.0
    if allowed <= 20:
        return 1.0
    if allowed <= 27:
        return 0.0
    if allowed <= 34:
        return -1.0
    return -4.0


def full_game(stats, slot):
    """This league's points for a full projected game."""
    slot = (slot or "").upper()
    if slot in DEFENSE_SLOTS:
        tiers = sum(float(stats.get(code, 0) or 0) * value
                    for code, value in POINTS_ALLOWED_BUCKETS.items())
        return _weighted(stats, DEFENSE) + tiers
    if slot in KICKER_SLOTS:
        return _weighted(stats, KICKER)
    return _weighted(stats, SKILL)


def remaining_share(game):
    """Share of regulation still to play: 1 before kickoff, 0 at the end."""
    if not game or game["state"] == "pre":
        return 1.0
    if game["state"] == "post" or game["period"] >= 5:
        return 0.0
    remaining = total_remaining_seconds(
        game["period"], game["clock_seconds"], game["state"]
    )
    return max(0.0, min(1.0, remaining / REGULATION_SECONDS))


def projected_final(stats, slot, actual, game, live_parts=None):
    """Blend what has happened with what is projected to come.

    A defense is handled separately while live, because its points-allowed
    tier is not additive: the final tier depends on the final score, so
    the remaining projected points against are added to the current score
    before the tier is looked up.
    """
    share = remaining_share(game)
    actual = actual or 0.0
    if stats is None:
        return round(actual, 2)
    if share <= 0:
        return round(actual, 2)
    if game is None or game["state"] == "pre":
        return round(full_game(stats, slot), 2)

    slot = (slot or "").upper()
    if slot in DEFENSE_SLOTS and live_parts is not None:
        counts = (
            live_parts.get("sacks", 0) * DEFENSE["99"]
            + live_parts.get("interceptions", 0) * DEFENSE["95"]
            + live_parts.get("fumble_recoveries", 0) * DEFENSE["96"]
            + live_parts.get("touchdowns", 0) * DEFENSE["105"]
        )
        remaining = _weighted(stats, DEFENSE, share)
        final_allowed = (live_parts.get("points_allowed", 0)
                         + float(stats.get(EXPECTED_POINTS_ALLOWED, 0) or 0) * share)
        return round(counts + remaining + _points_allowed_value(final_allowed), 2)

    return round(actual + full_game(stats, slot) * share, 2)
