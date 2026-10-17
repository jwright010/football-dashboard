"""
Import one week's lineups for the private Yahoo league.

    python import_manual.py yahoo_week_3.json

The file is built from a screenshot of the matchup page. Each lineup
entry is [slot, name as shown, team]:

    {
      "season": 2026,
      "week": 3,
      "league": "752951",
      "me":       {"name": "Sunscreen and Tums",
                   "lineup": [["QB", "C. Williams", "CHI"], ...]},
      "opponent": {"name": "The Dom-inator's",
                   "lineup": [["QB", "J. Allen", "BUF"], ...]}
    }

Re-importing the same week replaces it, which is how a lineup change
after the screenshot gets picked up.
"""

import sys

import db
import manual
import roster


def ask(name, candidates):
    print(f"\n  '{name}' matches {len(candidates)} players:")
    for index, athlete in enumerate(candidates, start=1):
        print(f"    {index}. {roster.describe(athlete)}")
    print("    0. skip this one")
    while True:
        answer = input("  Which one? ").strip()
        if answer in ("0", "", "s", "skip"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("  Enter a number from the list.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_manual.py <file.json>")
        return 1

    db.init()
    try:
        season, week, stored, problems = manual.import_file(sys.argv[1], chooser=ask)
    except FileNotFoundError:
        print(f"Not found: {sys.argv[1]}")
        return 1
    except (KeyError, ValueError, IndexError) as exc:
        print(f"Could not read {sys.argv[1]}: {exc}")
        return 1

    print(f"Season {season}, week {week}. Stored {stored} player(s).")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")

    matchup = manual.compute_matchup(week)
    if not matchup:
        return 1

    for key in ("me", "opponent"):
        side = matchup[key]
        print(f"\n{side['name']}   {side['points']:.2f}")
        for p in side["players"]:
            pts = "--" if p["points"] is None else f"{p['points']:.2f}"
            print(f"  {p['slot']:6} {p['name']:26} {pts:>7}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
