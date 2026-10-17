"""
Can GitHub's servers reach everything the dashboard depends on?

Your laptop reaches ESPN and Yahoo from an office or home connection.
GitHub runs code from data-center addresses, which some services treat
differently. This tries each outside call the dashboard makes and
reports what answers. Nothing is written anywhere.

Cookie values are never printed, only whether they are present.
"""

import json
import os
import sys

import httpx

SEASON = os.getenv("ESPN_SEASON_ID", "2026")
LEAGUE = os.getenv("ESPN_LEAGUE_ID", "")
YAHOO_LEAGUE = os.getenv("YAHOO_PUBLIC_LEAGUE_ID", "")
S2 = os.getenv("ESPN_S2", "").strip().strip('"')
SWID = os.getenv("ESPN_SWID", "").strip().strip('"')
if SWID and not SWID.startswith("{"):
    SWID = "{" + SWID + "}"

HONEST = {"User-Agent": "football-dashboard/1.0", "Accept": "application/json"}
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://football.fantasysports.yahoo.com/",
}
SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
FANTASY = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
           f"{SEASON}/segments/0/leagues/{LEAGUE}")

results = []


def record(name, ok, note):
    results.append((name, ok, note))


def get(url, **kwargs):
    kwargs.setdefault("timeout", 30.0)
    kwargs.setdefault("follow_redirects", True)
    return httpx.get(url, **kwargs)


print("Secrets present:")
print(f"  ESPN_S2   {'yes, ' + str(len(S2)) + ' characters' if S2 else 'NO'}")
print(f"  ESPN_SWID {'yes, ' + str(len(SWID)) + ' characters' if SWID else 'NO'}")
print()

# 1. Scoreboard: the one every other part depends on
event_id = None
try:
    r = get(f"{SITE}/scoreboard", headers=HONEST)
    if r.status_code == 200:
        events = r.json().get("events") or []
        started = [e for e in events
                   if (e.get("status") or {}).get("type", {}).get("state") in ("in", "post")]
        event_id = (started or events or [{}])[0].get("id")
        record("ESPN scoreboard", True, f"{len(events)} games this week")
    else:
        record("ESPN scoreboard", False, f"HTTP {r.status_code}")
except Exception as exc:
    record("ESPN scoreboard", False, f"{type(exc).__name__}: {exc}")

# 2. Box score: player stats for rooting goals and the Yahoo private league
if event_id:
    try:
        r = get(f"{SITE}/summary", params={"event": event_id}, headers=HONEST)
        players = len(((r.json() if r.status_code == 200 else {}).get("boxscore") or {}).get("players") or [])
        record("ESPN box score", r.status_code == 200,
               f"HTTP {r.status_code}, {players} team blocks")
    except Exception as exc:
        record("ESPN box score", False, f"{type(exc).__name__}: {exc}")
else:
    record("ESPN box score", False, "skipped, no game id from the scoreboard")

# 3. Team roster: name lookups when importing
try:
    r = get(f"{SITE}/teams/8/roster", headers=HONEST)
    record("ESPN team roster", r.status_code == 200, f"HTTP {r.status_code}")
except Exception as exc:
    record("ESPN team roster", False, f"{type(exc).__name__}: {exc}")

# 4. ESPN Fantasy league: needs your cookies
if LEAGUE and S2 and SWID:
    try:
        r = get(FANTASY, params={"view": "mTeam"}, headers=HONEST,
                cookies={"espn_s2": S2, "SWID": SWID})
        teams = len(r.json().get("teams") or []) if r.status_code == 200 else 0
        note = f"HTTP {r.status_code}, {teams} teams"
        if r.status_code == 401:
            note += " (cookies rejected: re-copy them, without quotes)"
        record("ESPN Fantasy league", r.status_code == 200 and teams > 0, note)
    except Exception as exc:
        record("ESPN Fantasy league", False, f"{type(exc).__name__}: {exc}")
else:
    record("ESPN Fantasy league", False, "skipped, a secret or the league id is missing")

# 5. ESPN projections: the Sunscreen and Tums projection model
if LEAGUE and S2 and SWID:
    try:
        headers = dict(HONEST)
        headers["x-fantasy-filter"] = json.dumps({"players": {"filterIds": {"value": [3916387]}}})
        r = get(FANTASY, params={"view": "kona_playercard"}, headers=headers,
                cookies={"espn_s2": S2, "SWID": SWID})
        count = len(r.json().get("players") or []) if r.status_code == 200 else 0
        record("ESPN projections", r.status_code == 200 and count > 0,
               f"HTTP {r.status_code}, {count} player(s) returned")
    except Exception as exc:
        record("ESPN projections", False, f"{type(exc).__name__}: {exc}")
else:
    record("ESPN projections", False, "skipped, a secret or the league id is missing")

# 6. Yahoo public league
if YAHOO_LEAGUE:
    try:
        r = get(f"https://pub-api-rw.fantasysports.yahoo.com/fantasy/v2/league/"
                f"nfl.l.{YAHOO_LEAGUE}/scoreboard", params={"format": "json"},
                headers=BROWSER)
        record("Yahoo public league", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as exc:
        record("Yahoo public league", False, f"{type(exc).__name__}: {exc}")
else:
    record("Yahoo public league", False, "skipped, no league id")

print(f"{'check':22} {'result':7} note")
print("-" * 64)
for name, ok, note in results:
    print(f"{name:22} {'PASS' if ok else 'FAIL':7} {note}")
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed} of {len(results)} passed")
sys.exit(0)
