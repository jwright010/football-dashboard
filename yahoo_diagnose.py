"""
Narrow down the Yahoo 403 by testing endpoints from simplest upward.

Which ones pass tells us whether the problem is the token's scope, the
resource path, or URL encoding of the semicolons Yahoo uses.
"""

import json
import os
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx
from dotenv import load_dotenv

from yahoo_auth import TOKEN_FILE, get_access_token

load_dotenv()

BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

LEAGUE_1 = os.getenv("YAHOO_LEAGUE_1_ID")
LEAGUE_2 = os.getenv("YAHOO_LEAGUE_2_ID")
TEAM_1 = os.getenv("YAHOO_TEAM_1_ID")

ENDPOINTS = [
    ("game info (nfl)",                 "game/nfl"),
    ("all my games",                    "users;use_login=1/games"),
    ("my nfl games",                    "users;use_login=1/games;game_keys=nfl"),
    ("my nfl teams",                    "users;use_login=1/games;game_keys=nfl/teams"),
    ("my nfl leagues",                  "users;use_login=1/games;game_keys=nfl/leagues"),
]

if LEAGUE_1:
    ENDPOINTS.append((f"league {LEAGUE_1} direct", f"league/nfl.l.{LEAGUE_1}"))
    ENDPOINTS.append((f"league {LEAGUE_1} teams",  f"league/nfl.l.{LEAGUE_1}/teams"))
if LEAGUE_1 and TEAM_1:
    ENDPOINTS.append(
        (f"team {LEAGUE_1}.t.{TEAM_1}", f"team/nfl.l.{LEAGUE_1}.t.{TEAM_1}")
    )
if LEAGUE_2:
    ENDPOINTS.append((f"league {LEAGUE_2} direct", f"league/nfl.l.{LEAGUE_2}"))


def show_token_info():
    if not TOKEN_FILE.exists():
        print("No yahoo_token.json. Run: python yahoo_auth.py")
        return False
    data = json.loads(TOKEN_FILE.read_text())
    print("Token file contents (secrets hidden):")
    for key, value in data.items():
        if key in ("access_token", "refresh_token", "id_token"):
            print(f"  {key}: [{len(str(value))} chars]")
        else:
            print(f"  {key}: {value}")
    print()
    return True


def main():
    if not show_token_info():
        return

    try:
        token = get_access_token()
    except Exception as exc:
        print(f"Could not get an access token: {exc}")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "football-dashboard/1.0",
    }

    print(f"Testing {len(ENDPOINTS)} endpoints.\n")

    for label, path in ENDPOINTS:
        url = f"{BASE}/{path}?format=json"
        try:
            response = httpx.get(url, headers=headers, timeout=30.0)
        except Exception as exc:
            print(f"  ERR   {label}")
            print(f"        {type(exc).__name__}: {exc}")
            continue

        status = response.status_code

        # Confirm httpx did not re-encode the semicolons Yahoo needs.
        sent = str(response.request.url)
        mangled = ";" in path and ";" not in sent

        if status == 200:
            try:
                keys = list(response.json().get("fantasy_content", {}).keys())
            except Exception:
                keys = ["unparseable"]
            print(f"  OK    {label}")
            print(f"        keys: {keys}")
        else:
            print(f"  {status}   {label}")
            body = response.text[:220].replace("\n", " ")
            print(f"        {body}")

        if mangled:
            print(f"        WARNING semicolons were re-encoded: {sent}")

    print("\nSent URL of the first test, for reference:")
    print(f"  {BASE}/game/nfl?format=json")


if __name__ == "__main__":
    main()
