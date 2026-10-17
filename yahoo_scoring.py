"""
Scoring for Yahoo league 752951 (Sunscreen and Tums).

That league has no API path, so its points are computed here from ESPN
box scores. The rules were derived from a real matchup screenshot that
showed both stat lines and point totals for 22 players.

Confirmed exactly, 20 of 20 skill-position players to the cent:

    passing yards   0.04 each (1 per 25)     receptions        0.5
    passing TD      4                        receiving yards   0.1 each
    interception   -1                        receiving TD      6
    rushing yards   0.1 each (1 per 10)      fumble lost      -2
    rushing TD      6

Kickers, confirmed on one kicker:
    field goals     0.1 per yard of each made kick
    PAT             1
Confirmed from the league's settings page:
    missed FG      -1, at every distance
    missed PAT     -1

Defense, confirmed on one team:
    sack            1
    points allowed  14-20 is worth 1
Confirmed from the league's settings page: the full points-allowed
tiers, interceptions 2, fumble recoveries 2, touchdowns 6.

Not scoreable from a box score at all: two-point conversions, safeties,
blocked kicks, extra-point returns, and offensive fumble-return
touchdowns. They are rare, but they are real gaps.

Run directly to check the kicker parsing against a finished game:

    python yahoo_scoring.py
"""

import re

import boxscore

SKILL = {
    "pass_yds": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yds": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yds": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
    "kr_td": 6.0,
    "pr_td": 6.0,
}

FG_PER_YARD = 0.1
PAT = 1.0
PAT_MISSED = -1.0
FG_MISSED = -1.0

DEF_SACK = 1.0
DEF_INT = 2.0
DEF_FUMBLE_RECOVERY = 2.0
DEF_TD = 6.0

KICKER_SLOTS = {"K", "PK"}
DEFENSE_SLOTS = {"DEF", "D/ST", "DST"}


def points_allowed_value(allowed):
    """Yahoo's standard points-allowed tiers. Only 14-20 is confirmed."""
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


def _blocks(summary):
    return (summary.get("boxscore") or {}).get("players") or []


def team_sum(summary, espn_team_id, category, label, first_part=False):
    """Sum one box score column across every player on one team.

    first_part handles compound cells like '4-38' (sacks-yards), where
    only the leading number is wanted.
    """
    total = 0.0
    for block in _blocks(summary):
        if str((block.get("team") or {}).get("id")) != str(espn_team_id):
            continue
        for cat in block.get("statistics") or []:
            if cat.get("name") != category:
                continue
            labels = cat.get("labels") or []
            if label not in labels:
                continue
            index = labels.index(label)
            for row in cat.get("athletes") or []:
                stats = row.get("stats") or []
                if index >= len(stats):
                    continue
                raw = str(stats[index]).strip()
                if first_part:
                    raw = raw.replace("-", "/").split("/")[0]
                try:
                    total += float(raw)
                except ValueError:
                    continue
    return total


FG_TEXT = re.compile(
    r"(\d{1,2})\s*(?:yd|yds|yard|yards)\.?\s+(?:field\s+goal|fg)\b", re.I
)


def fg_distances(summary, espn_team_id):
    """Distance of every made field goal by one team.

    The box score only reports makes, attempts and the longest kick.
    This league scores by total yards, so the distances come from the
    scoring plays instead. A team has one kicker in practice, so the
    team's field goals are the kicker's.
    """
    distances = []
    for play in summary.get("scoringPlays") or []:
        team = str(((play.get("team") or {}).get("id")) or "")
        if team != str(espn_team_id):
            continue
        text = play.get("text") or play.get("shortText") or ""
        match = FG_TEXT.search(text)
        if match:
            distances.append(int(match.group(1)))
    return distances


def score_player(summary, athlete_id):
    """Skill-position points, or None if absent from the box score."""
    if not summary or not athlete_id:
        return None
    total, seen = 0.0, False
    for key, weight in SKILL.items():
        value = boxscore.player_stat(summary, athlete_id, key)
        if value is None:
            continue
        seen = True
        total += value * weight
    return round(total, 2) if seen else None


def score_kicker(summary, athlete_id, espn_team_id):
    """Kicker points plus the distances used, for showing the work."""
    if not summary or not athlete_id:
        return None, []
    made = boxscore.player_stat(summary, athlete_id, "fg_made")
    attempts = boxscore.player_stat(summary, athlete_id, "fg_att")
    extra = boxscore.player_stat(summary, athlete_id, "xp_made")
    extra_att = boxscore.player_stat(summary, athlete_id, "xp_att")
    if made is None and extra is None:
        return None, []

    distances = fg_distances(summary, espn_team_id)
    missed = max(0.0, (attempts or 0) - (made or 0))
    missed_extra = max(0.0, (extra_att or 0) - (extra or 0))
    points = (FG_PER_YARD * sum(distances)
              + PAT * (extra or 0)
              + PAT_MISSED * missed_extra
              + FG_MISSED * missed)
    return round(points, 2), distances


def score_defense(summary, espn_team_id, opponent_espn_id):
    """Team defense points plus the parts, for showing the work.

    Each figure is read from the opponent's side of the box score where
    that is exact: sacks are times their passers were sacked,
    interceptions are passes they threw away, fumble recoveries are
    fumbles they lost.
    """
    if not summary:
        return None, {}

    sacks = team_sum(summary, opponent_espn_id, "passing", "SACKS",
                     first_part=True)
    interceptions = team_sum(summary, opponent_espn_id, "passing", "INT")
    recoveries = team_sum(summary, opponent_espn_id, "fumbles", "LOST")
    touchdowns = (
        team_sum(summary, espn_team_id, "defensive", "TD")
        + team_sum(summary, espn_team_id, "kickReturns", "TD")
        + team_sum(summary, espn_team_id, "puntReturns", "TD")
    )
    allowed = boxscore.team_points(summary, opponent_espn_id) or 0

    points = (DEF_SACK * sacks
              + DEF_INT * interceptions
              + DEF_FUMBLE_RECOVERY * recoveries
              + DEF_TD * touchdowns
              + points_allowed_value(allowed))

    parts = {
        "sacks": sacks, "interceptions": interceptions,
        "fumble_recoveries": recoveries, "touchdowns": touchdowns,
        "points_allowed": allowed,
    }
    return round(points, 2), parts


def score(summary, slot, athlete_id, espn_team_id, opponent_espn_id):
    """Dispatch on roster slot. Returns points or None."""
    slot = (slot or "").upper()
    if slot in DEFENSE_SLOTS:
        return score_defense(summary, espn_team_id, opponent_espn_id)[0]
    if slot in KICKER_SLOTS:
        return score_kicker(summary, athlete_id, espn_team_id)[0]
    return score_player(summary, athlete_id)
