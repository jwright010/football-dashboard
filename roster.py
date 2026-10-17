"""
Team roster lookup, used to resolve a typed player name to an ESPN
athlete id once, at import time.

Searching a single team's roster instead of the whole league keeps the
candidate set near 53 and removes the duplicate-name problem: there are
two Josh Allens in the NFL, but only one on Buffalo.

Rosters are static, so this works before kickoff. A name that will not
match is caught while there is still time to fix it.
"""

import logging
import re
import threading
import time

import truststore
truststore.inject_into_ssl()

import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
HEADERS = {"User-Agent": "football-dashboard/1.0", "Accept": "application/json"}

log = logging.getLogger("roster")

TTL_SECONDS = 6 * 3600
_cache = {}
_lock = threading.Lock()

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize(name):
    """Lowercase, strip punctuation and suffixes, collapse whitespace.

    Turns "Ja'Marr Chase", "JaMarr Chase" and "Marvin Harrison Jr."
    into comparable forms.
    """
    text = (name or "").lower()
    text = text.replace("'", "").replace("\u2019", "").replace(".", "")
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    parts = [p for p in text.split() if p not in SUFFIXES]
    return " ".join(parts).strip()


RETRIES = 2
RETRY_DELAYS = (1.0, 3.0)


class RosterUnavailable(RuntimeError):
    """ESPN would not serve a roster, even after retrying."""


def fetch_roster(espn_team_id, force=False):
    """Every athlete on one team, flattened across position groups."""
    key = str(espn_team_id)
    now = time.time()

    with _lock:
        cached = _cache.get(key)
        if cached and not force and now - cached["at"] < TTL_SECONDS:
            return cached["value"]

    # ESPN occasionally answers a valid roster request with an error and
    # then serves it normally moments later, so a failure gets two more
    # tries, a little further apart each time, before it counts.
    last_error = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(RETRY_DELAYS[attempt - 1])
        try:
            response = httpx.get(
                f"{BASE}/teams/{key}/roster",
                headers=HEADERS, timeout=30.0, follow_redirects=True,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code == 200:
            break
        last_error = f"returned {response.status_code}"
    else:
        raise RosterUnavailable(
            f"ESPN roster {last_error} for team {key}, after {RETRIES + 1} tries"
        )
    payload = response.json()

    athletes = []
    groups = payload.get("athletes") or []
    for group in groups:
        # The endpoint groups by offense, defense, specialTeam and so on.
        items = group.get("items") if isinstance(group, dict) else None
        for athlete in (items if items is not None else [group]):
            if not isinstance(athlete, dict) or not athlete.get("id"):
                continue
            position = athlete.get("position") or {}
            status = athlete.get("status") or {}
            athletes.append({
                "athlete_id": str(athlete["id"]),
                "name": athlete.get("displayName") or athlete.get("fullName") or "",
                "first": athlete.get("firstName") or "",
                "last": athlete.get("lastName") or "",
                "jersey": athlete.get("jersey") or "",
                "position": position.get("abbreviation") or position.get("name") or "",
                "status": status.get("name") or status.get("type") or "",
                "group": group.get("position", "") if isinstance(group, dict) else "",
            })

    with _lock:
        _cache[key] = {"at": now, "value": athletes}
    return athletes


def search(espn_team_id, query):
    """Candidate athletes for a typed name, best first.

    Tiers, in order: exact full name, last name plus first initial,
    exact last name, then any substring. Each tier is only consulted
    when the ones above it come back empty.
    """
    wanted = normalize(query)
    if not wanted:
        return []

    athletes = fetch_roster(espn_team_id)
    for athlete in athletes:
        athlete["_norm"] = normalize(athlete["name"])
        athlete["_last"] = normalize(athlete["last"])
        athlete["_first"] = normalize(athlete["first"])

    exact = [a for a in athletes if a["_norm"] == wanted]
    if exact:
        return exact

    parts = wanted.split()
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        initial = [
            a for a in athletes
            if a["_last"] == last and a["_first"][:1] == first[:1]
        ]
        if initial:
            return initial

        by_last = [a for a in athletes if a["_last"] == last]
        if by_last:
            return by_last

    single = parts[-1] if parts else wanted
    by_last = [a for a in athletes if a["_last"] == single]
    if by_last:
        return by_last

    return [a for a in athletes if single in a["_norm"]][:10]


def describe(athlete):
    """'Jahmyr Gibbs  RB #26' for the confirmation prompt."""
    jersey = f" #{athlete['jersey']}" if athlete["jersey"] else ""
    position = f"  {athlete['position']}" if athlete["position"] else ""
    status = ""
    if athlete["status"] and athlete["status"].lower() not in ("active", ""):
        status = f"  [{athlete['status']}]"
    return f"{athlete['name']}{position}{jersey}{status}"
