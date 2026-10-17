"""
Import one week of Pick'em picks from a JSON file.

    python import_picks.py picks_week_2.json

The file looks like:

    {
      "season": 2026,
      "week": 2,
      "picks": ["DET", "BUF", "KC", "PHI"]
    }

Team abbreviations from any of the three platforms are accepted, so
both WAS and WSH resolve to Washington. Re-importing the same week
replaces it, which is how you correct a mistake.
"""

import sys

import db
import picks


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_picks.py <file.json>")
        return 1

    path = sys.argv[1]
    db.init()

    try:
        season, week, (stored, problems) = picks.load_file(path)
    except FileNotFoundError:
        print(f"Not found: {path}")
        return 1
    except (KeyError, ValueError) as exc:
        print(f"Could not read {path}: {exc}")
        print('Expected keys: "season", "week", "picks"')
        return 1

    print(f"Season {season}, week {week}")
    print(f"Stored {stored} pick(s)")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")

    if not stored:
        return 1

    detail = picks.week_detail(season, week)
    print(f"\nWeek {week}: {detail['count']} of {detail['total']} "
          f"won or leading\n")

    for p in detail["picks"]:
        mark = "+" if p["counts"] else " "
        if p["is_live"]:
            state = f"{p['my_score']}-{p['their_score']} {p['label']}"
        elif p["status"] == "pending":
            state = "not started"
        else:
            state = f"{p['my_score']}-{p['their_score']} Final"
        print(f"  {mark} {p['picked']:4} over {p['opponent']:4}  "
              f"{p['status']:9} {state}")

    season_info = picks.season_total(season)
    print(f"\nSeason total: {season_info['total']} "
          f"across {season_info['weeks_tracked']} week(s) tracked")
    if season_info["baseline"]:
        print(f"  (includes a baseline of {season_info['baseline']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
