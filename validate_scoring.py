"""
Check the private league's scoring against real Week 2 box scores.

Your Week 2 matchup screenshot showed Yahoo's own point totals. For the
players whose games were already final when it was taken, those totals
are a fixed answer key. This scores the same players from ESPN's box
scores and compares.

It is the only test that touches real data end to end: roster lookup,
box score parsing, the kicker's field goal distances from the scoring
plays, and the defense's points-allowed tier.

Players whose games were still in progress at the screenshot are left
out, since their Yahoo totals were partial. A small difference can also
come from a stat correction made after the game.

    python validate_scoring.py
"""

import sys

import boxscore
import db
import manual
import poller
import yahoo_scoring

WEEK = 2

# (slot, name as the screenshot shows it, team, Yahoo's total)
ANSWER_KEY = [
    ("QB",  "C. Williams",  "CHI",  8.72),
    ("RB",  "B. Robinson",  "ATL",  9.60),
    ("RB",  "D. Swift",     "CHI", 10.40),
    ("WR",  "J. Williams",  "DET",  4.30),
    ("TE",  "M. Andrews",   "BAL",  7.90),
    ("K",   "K. Fairbairn", "HOU",  7.20),
    ("RB",  "K. Monangai",  "CHI",  6.80),
    ("WR",  "W. Robinson",  "TEN",  1.40),
    ("WR",  "D. Douglas",   "NE",   1.40),
    ("QB",  "J. Allen",     "BUF", 40.82),
    ("WR",  "C. Watson",    "GB",  12.10),
    ("WR",  "G. Wilson",    "NYJ", 14.20),
    ("TE",  "K. Pitts Sr.", "ATL",  2.00),
    ("DEF", "Texans",       "HOU",  6.00),
]


def main():
    db.init()
    season = manual.current_season()
    if not season:
        print("No games stored. Start the dashboard once so the poller runs.")
        return 1

    # Make sure Week 2's final scores are stored. This doubles as the
    # backfill from the startup steps and is harmless to repeat.
    result = poller.poll_once(season=season, week=WEEK, keep_raw=False)
    if not result["ok"]:
        print(f"Could not fetch week {WEEK}: {result['error']}")
        return 1
    print(f"Week {WEEK} refreshed: {result['written']} games stored.\n")

    conn = db.connect()
    try:
        games = {}
        for g in conn.execute(
            """
            SELECT g.*, ta.espn_id AS away_espn, th.espn_id AS home_espn
            FROM game g
            JOIN team ta ON ta.id = g.away_team_id
            JOIN team th ON th.id = g.home_team_id
            WHERE g.season = ? AND g.week = ?
            """,
            (season, WEEK),
        ).fetchall():
            games[str(g["away_espn"])] = g
            games[str(g["home_espn"])] = g
    finally:
        conn.close()

    exact = off = skipped = 0
    print(f"{'player':22} {'yahoo':>7} {'ours':>7}  diff   note")
    print("-" * 70)

    for slot, name, abbrev, expected in ANSWER_KEY:
        conn = db.connect()
        try:
            team = manual.team_row(conn, abbrev)
        finally:
            conn.close()
        team_espn = str(team["espn_id"])
        game = games.get(team_espn)

        if not game or game["state"] != "post":
            print(f"{name:22} {expected:7.2f} {'--':>7}         game not final, skipped")
            skipped += 1
            continue

        opponent = (str(game["home_espn"]) if str(game["away_espn"]) == team_espn
                    else str(game["away_espn"]))
        summary = boxscore.fetch_summary(game["event_id"])

        note = ""
        athlete_id = None
        label = name
        if slot != "DEF":
            try:
                athlete = manual.resolve_athlete(team["espn_id"], name, slot)
                athlete_id, label = athlete["athlete_id"], athlete["name"]
            except Exception as exc:
                print(f"{name:22} {expected:7.2f} {'--':>7}         could not resolve: {exc}")
                skipped += 1
                continue

        if slot == "K":
            ours, distances = yahoo_scoring.score_kicker(summary, athlete_id, team_espn)
            note = f"FG distances {distances}"
        elif slot == "DEF":
            ours, parts = yahoo_scoring.score_defense(summary, team_espn, opponent)
            note = (f"sacks {parts.get('sacks'):g}, int {parts.get('interceptions'):g}, "
                    f"fr {parts.get('fumble_recoveries'):g}, PA {parts.get('points_allowed'):g}")
        else:
            ours = yahoo_scoring.score_player(summary, athlete_id)

        if ours is None:
            ours = 0.0
        diff = round(ours - expected, 2)
        if abs(diff) < 0.005:
            exact += 1
            mark = "ok"
        else:
            off += 1
            mark = "OFF"
        shown = label if len(label) <= 22 else label[:21] + "."
        print(f"{shown:22} {expected:7.2f} {ours:7.2f}  {diff:+.2f}  {mark}  {note}")

    print(f"\n{exact} exact, {off} off, {skipped} skipped")
    return 0 if off == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
