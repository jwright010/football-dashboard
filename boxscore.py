"""
ESPN box score reader.

The summary endpoint returns per-player stats as parallel label and
value arrays inside ten categories. STATS below maps a friendly name
onto the category and label that holds it.

Confirmed against a real box score, including the compound fields:
passing 'C/ATT' arrives as '26/38', 'SACKS' as '4-38', and kicking
'FG' as '2/3'.
"""

import logging
import threading
import time

import truststore
truststore.inject_into_ssl()

import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
HEADERS = {"User-Agent": "football-dashboard/1.0", "Accept": "application/json"}

log = logging.getLogger("boxscore")

# Box scores change slowly relative to how often the page refreshes.
TTL_SECONDS = 25
_cache = {}
_lock = threading.Lock()


def _whole(value):
    """First half of a compound like '26/38' or '4-38'."""
    return value.replace("-", "/").split("/")[0]


def _second(value):
    parts = value.replace("-", "/").split("/")
    return parts[1] if len(parts) > 1 else "0"


# friendly name -> (category, label, extractor, display)
STATS = {
    "pass_yds":   ("passing",       "YDS",    None,    "passing yards"),
    "pass_td":    ("passing",       "TD",     None,    "passing TDs"),
    "pass_int":   ("passing",       "INT",    None,    "interceptions thrown"),
    "pass_comp":  ("passing",       "C/ATT",  _whole,  "completions"),
    "pass_att":   ("passing",       "C/ATT",  _second, "pass attempts"),

    "rush_yds":   ("rushing",       "YDS",    None,    "rushing yards"),
    "rush_td":    ("rushing",       "TD",     None,    "rushing TDs"),
    "carries":    ("rushing",       "CAR",    None,    "carries"),

    "rec":        ("receiving",     "REC",    None,    "receptions"),
    "rec_yds":    ("receiving",     "YDS",    None,    "receiving yards"),
    "rec_td":     ("receiving",     "TD",     None,    "receiving TDs"),
    "targets":    ("receiving",     "TGTS",   None,    "targets"),

    "tackles":    ("defensive",     "TOT",    None,    "tackles"),
    "solo":       ("defensive",     "SOLO",   None,    "solo tackles"),
    "sacks":      ("defensive",     "SACKS",  None,    "sacks"),
    "tfl":        ("defensive",     "TFL",    None,    "tackles for loss"),
    "pass_def":   ("defensive",     "PD",     None,    "passes defended"),

    "int_caught": ("interceptions", "INT",    None,    "interceptions caught"),

    "fg_made":    ("kicking",       "FG",     _whole,  "field goals made"),
    "fg_att":     ("kicking",       "FG",     _second, "field goal attempts"),
    "xp_made":    ("kicking",       "XP",     _whole,  "extra points made"),
    "xp_att":     ("kicking",       "XP",     _second, "extra point attempts"),
    "kick_pts":   ("kicking",       "PTS",    None,    "kicking points"),

    "kr_td":      ("kickReturns",   "TD",     None,    "kick return TDs"),
    "pr_td":      ("puntReturns",   "TD",     None,    "punt return TDs"),
    "def_td":     ("defensive",     "TD",     None,    "defensive TDs"),

    "fumbles":    ("fumbles",       "FUM",    None,    "fumbles"),
    "fum_lost":   ("fumbles",       "LOST",   None,    "fumbles lost"),
}

# Stats that are the sum of several box score cells.
COMPOSITES = {
    "total_td": [
        ("rushing", "TD"), ("receiving", "TD"),
        ("kickReturns", "TD"), ("puntReturns", "TD"),
    ],
    "rush_rec_yds": [
        ("rushing", "YDS"), ("receiving", "YDS"),
    ],
    "all_purpose_yds": [
        ("rushing", "YDS"), ("receiving", "YDS"),
        ("kickReturns", "YDS"), ("puntReturns", "YDS"),
    ],
}

COMPOSITE_LABELS = {
    "total_td": "total TDs",
    "rush_rec_yds": "rushing + receiving yards",
    "all_purpose_yds": "all purpose yards",
}

# Words a person might type, mapped onto the keys above.
ALIASES = {
    "passing yards": "pass_yds", "pass yds": "pass_yds", "pass yards": "pass_yds",
    "passing tds": "pass_td", "pass td": "pass_td", "passing td": "pass_td",
    "interceptions": "pass_int", "ints": "pass_int",
    "completions": "pass_comp", "attempts": "pass_att",
    "rushing yards": "rush_yds", "rush yds": "rush_yds", "rush yards": "rush_yds",
    "rushing tds": "rush_td", "rush td": "rush_td", "rushing td": "rush_td",
    "carries": "carries", "rushes": "carries", "attempts rushing": "carries",
    "receptions": "rec", "rec": "rec", "catches": "rec",
    "receiving yards": "rec_yds", "rec yds": "rec_yds", "rec yards": "rec_yds",
    "receiving tds": "rec_td", "rec td": "rec_td", "receiving td": "rec_td",
    "targets": "targets", "tgts": "targets",
    "tackles": "tackles", "total tackles": "tackles",
    "solo tackles": "solo", "sacks": "sacks",
    "tackles for loss": "tfl", "tfl": "tfl",
    "passes defended": "pass_def",
    "interceptions caught": "int_caught",
    "field goals": "fg_made", "fg": "fg_made", "field goals made": "fg_made",
    "kicking points": "kick_pts",
    "fumbles": "fumbles", "fumbles lost": "fum_lost",
    "any td": "total_td", "total tds": "total_td", "td": "total_td",
    "touchdown": "total_td", "touchdowns": "total_td", "total td": "total_td",

    "rush + rec yds": "rush_rec_yds",
    "rush + rec yards": "rush_rec_yds",
    "rushing + receiving yards": "rush_rec_yds",
    "scrimmage yards": "rush_rec_yds",
    "scrimmage yds": "rush_rec_yds",

    "all purpose yds": "all_purpose_yds",
    "all purpose yards": "all_purpose_yds",
    "all-purpose yards": "all_purpose_yds",
    "apy": "all_purpose_yds",
}


def resolve_stat(text):
    """Turn typed text into a stat key, or None."""
    key = (text or "").strip().lower()
    if key in STATS or key in COMPOSITES:
        return key
    return ALIASES.get(key)


def stat_label(key):
    if key in COMPOSITE_LABELS:
        return COMPOSITE_LABELS[key]
    entry = STATS.get(key)
    return entry[3] if entry else key


def fetch_summary(event_id, force=False):
    """Box score for one game, cached briefly."""
    now = time.time()
    with _lock:
        cached = _cache.get(event_id)
        if cached and not force and now - cached["at"] < TTL_SECONDS:
            return cached["value"]

    response = httpx.get(
        f"{BASE}/summary", params={"event": str(event_id)},
        headers=HEADERS, timeout=30.0, follow_redirects=True,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ESPN summary returned {response.status_code} for event {event_id}"
        )
    payload = response.json()

    with _lock:
        _cache[event_id] = {"at": now, "value": payload}
    return payload


def _to_number(raw):
    if raw in (None, "", "--", "-"):
        return 0.0
    try:
        return float(str(raw).strip())
    except ValueError:
        return 0.0


def _category_value(block, category, label, extractor, athlete_id):
    """Pull one athlete's value out of one stat category."""
    for cat in block.get("statistics") or []:
        if cat.get("name") != category:
            continue
        labels = cat.get("labels") or []
        if label not in labels:
            return None
        index = labels.index(label)

        for row in cat.get("athletes") or []:
            athlete = row.get("athlete") or {}
            if str(athlete.get("id")) != str(athlete_id):
                continue
            stats = row.get("stats") or []
            if index >= len(stats):
                return None
            raw = stats[index]
            if extractor:
                raw = extractor(str(raw))
            return _to_number(raw)
        return None
    return None


def player_stat(summary, athlete_id, stat_key):
    """One player's value for one stat.

    Returns None when the player does not appear in the box score,
    which means inactive or simply no touches yet. Those are different
    and the caller distinguishes them using game state.
    """
    box = summary.get("boxscore") or {}
    blocks = box.get("players") or []

    if stat_key in COMPOSITES:
        total = None
        for block in blocks:
            for category, label in COMPOSITES[stat_key]:
                value = _category_value(block, category, label, None, athlete_id)
                if value is not None:
                    total = (total or 0.0) + value
        return round(total, 1) if total is not None else None

    entry = STATS.get(stat_key)
    if not entry:
        return None
    category, label, extractor, _ = entry

    for block in blocks:
        value = _category_value(block, category, label, extractor, athlete_id)
        if value is not None:
            return value
    return None


def team_points(summary, espn_team_id):
    """Final or current points for one team, from the box score header."""
    header = summary.get("header") or {}
    for competition in header.get("competitions") or []:
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            if str(team.get("id")) == str(espn_team_id):
                return _to_number(competitor.get("score"))
    return None
