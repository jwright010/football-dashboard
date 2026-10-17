"""
Import a week of rooting interests.

Accepts either the spreadsheet or a plain text file:

    python import_rooting.py rooting_interests.xlsx
    python import_rooting.py rooting_week_2.txt

The spreadsheet has dropdowns for Team, Stat and Direction, so the only
free entry is the player name and the number. Its rows are turned into
the same text lines the parser below reads, which means both routes go
through one set of rules and cannot drift apart.

File format, one goal per line:

    Week 2

    Jahmyr Gibbs (DET) rush yds over 85
    Amon-Ra St. Brown (DET) rec over 6
    Josh Allen (BUF) pass TD over 2
    Travis Kelce (KC) any TD
    James Cook (BUF) rush yds under 60

    BUF win
    KC points over 24
    DAL points under 17

Player lines need the team in parentheses, which is how the right game
and roster get found. Blank lines, '#' comments, and a leading 'Week N'
line are ignored.

When a name matches more than one player, you are asked to choose. The
athlete id is then stored permanently, so this only happens once.

Re-importing the same week replaces it.
"""

import re
import sys
from pathlib import Path

import db
import rooting

# The seed row shipped in the template, and the row it ships in.
# It is only skipped when found untouched in that exact row, so a real
# goal that happens to match the example is never silently dropped.
EXAMPLE_ROW = ("Jahmyr Gibbs", "DET", "rush yds", "over", 85)
EXAMPLE_ROW_NUMBER = 7


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_workbook(path):
    """Turn the Rooting sheet into text lines. Returns (week, lines, notes)."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError(
            "Reading .xlsx needs openpyxl. Run: pip install openpyxl"
        )

    workbook = load_workbook(path, data_only=True)
    sheet = workbook["Rooting"] if "Rooting" in workbook.sheetnames \
        else workbook.worksheets[0]

    week = sheet["B2"].value
    week = int(week) if week else None

    lines, notes = [], []
    for offset, row in enumerate(
        sheet.iter_rows(min_row=7, max_col=5, values_only=True)
    ):
        row_number = 7 + offset
        player, team, stat, direction, target = (
            _clean(row[0]), _clean(row[1]), _clean(row[2]),
            _clean(row[3]), _clean(row[4]),
        )

        if not team and not stat:
            continue

        if row_number == EXAMPLE_ROW_NUMBER and (
            player, team, stat, direction, target
        ) == tuple(_clean(v) for v in EXAMPLE_ROW):
            notes.append(
                "Skipped the untouched example row. Overwrite or delete it."
            )
            continue

        if not team:
            notes.append(f"Skipped a row with no team: {player or stat!r}")
            continue
        if not stat:
            notes.append(f"Skipped {player or team!r}: no stat chosen")
            continue

        if direction.lower() in ("(none)", "none"):
            direction = ""

        # Goals that carry no number.
        if stat.lower() in ("win", "any td"):
            goal = stat
        elif direction and target:
            goal = f"{stat} {direction} {target}"
        else:
            notes.append(
                f"Skipped {player or team!r} {stat!r}: "
                "needs both a direction and a target"
            )
            continue

        lines.append(f"{player} ({team}) {goal}" if player
                     else f"{team} {goal}")

    return week, lines, notes


def ask(query, candidates):
    """Prompt for the right player when a name is ambiguous."""
    import roster

    print(f"\n  '{query}' matches {len(candidates)} players:")
    for index, athlete in enumerate(candidates, start=1):
        print(f"    {index}. {roster.describe(athlete)}")
    print("    0. skip this one")

    while True:
        answer = input("  Which one? ").strip()
        if answer in ("0", "s", "skip", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("  Enter a number from the list.")


def infer_week(text, path):
    """Week number from a 'Week N' line, or from the filename."""
    match = re.search(r"^\s*week\s+(\d+)", text, re.IGNORECASE | re.MULTILINE)
    if match:
        return int(match.group(1))
    match = re.search(r"week[_-]?(\d+)", path, re.IGNORECASE)
    return int(match.group(1)) if match else None


def current_season():
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(season) AS s FROM game").fetchone()
        return row["s"] if row and row["s"] else None
    finally:
        conn.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_rooting.py <file.txt>")
        return 1

    path = sys.argv[1]
    db.init()

    if not Path(path).exists():
        print(f"Not found: {path}")
        return 1

    season = current_season()
    if not season:
        print("No games stored yet. Run: python poller.py")
        return 1

    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            week, lines, notes = read_workbook(path)
        except Exception as exc:
            print(f"Could not read {path}: {exc}")
            return 1
        for note in notes:
            print(f"  {note}")
        if notes:
            print()
        text = "\n".join(lines)
        if week is None:
            week = infer_week(text, path)
    else:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        week = infer_week(text, path)

    if week is None:
        print("Could not tell which week this is.")
        print("Put the number in cell B2 of the spreadsheet, or add a "
              "'Week 2' line to the text file.")
        return 1

    goals, errors = rooting.parse_text(text)

    if errors:
        print(f"{len(errors)} line(s) could not be read:")
        for error in errors:
            print(f"  {error}")
        print()

    if not goals:
        print("Nothing to import.")
        return 1

    print(f"Season {season}, week {week}. Read {len(goals)} goal(s).")

    stored, problems = rooting.import_goals(season, week, goals, chooser=ask)

    print(f"\nStored {stored} goal(s)")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")

    if not stored:
        return 1

    detail = rooting.week_detail(season, week)
    print(f"\nWeek {week}: {detail['achieved']} of {detail['total']} "
          f"achieved so far\n")

    for item in detail["items"]:
        mark = {
            "achieved": "+", "failed": "x", "live": ">",
            "pending": ".", "noshow": "?",
        }.get(item["status"], " ")
        current = "--" if item["current"] is None else f"{item['current']:g}"
        where = f"vs {item['opponent']}" if item["opponent"] else ""
        print(f"  {mark} {item['label']:24} {item['goal_text']:26} "
              f"{current:>6}   {item['status']:9} {where}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
