"""
Builds the phone version of the dashboard.

GitHub runs this on a schedule. It runs the same pipeline the laptop
server runs, then saves the page and its data as plain files that
GitHub Pages publishes:

    _site/index.html       the same page the laptop serves
    _site/dashboard.json   the data the page draws

The database is carried between runs (see .github/workflows/site.yml),
so this only re-imports a file when its contents change. If the carried
database is ever lost, everything rebuilds from the files in imports/,
which are the record of truth.

Files it reads from imports/:

    picks_week_N.json      Pick'em, one file per week; keep every week
    rooting_interests.xlsx rooting goals; or rooting_week_N.txt
    yahoo_week_N.json      Sunscreen and Tums lineups

Problems an import would print on the laptop, like an ambiguous name,
are shown as a notice at the top of the page instead, and stay there
until the file changes.
"""

import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
import import_rooting
import manual
import picks
import poller
import rooting

ROOT = Path(__file__).parent
IMPORTS = ROOT / "imports"
OUT = ROOT / "_site"

# Logos come straight from ESPN on the phone, where there is no
# corporate network in the way.
LOGO = "https://a.espncdn.com/i/teamlogos/nfl/500/{lower}.png"

# Raise this whenever the way files are imported changes. Every file then
# imports again on the next run, so the order code and data files are
# uploaded in never matters.
#   2: BOZO column; columns read by header name
#   3: Revised wording, unders with "-", position and wording stored
#   4: without a BOZO? column, nothing is marked BOZO
IMPORT_VERSION = "4"

# A problem worded this way came from ESPN refusing a roster lookup,
# which usually clears on its own, so that file is tried again next run.
RETRY_MARKER = "Re-run the import"


class NotYet(Exception):
    """A file for a week that hasn't started. Expected, not an error."""

log = logging.getLogger("build_site")

SCHEMA = """
CREATE TABLE IF NOT EXISTS build_meta (
    name    TEXT PRIMARY KEY,
    digest  TEXT NOT NULL,
    notes   TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------
# Remembering which files have been imported
# ---------------------------------------------------------------------

def _digest(path):
    return hashlib.sha256(IMPORT_VERSION.encode() + path.read_bytes()).hexdigest()


def _state(name):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT digest, notes FROM build_meta WHERE name = ?", (name,)
        ).fetchone()
        return (row["digest"], json.loads(row["notes"])) if row else (None, [])
    finally:
        conn.close()


def _remember(name, digest, notes):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO build_meta (name, digest, notes) VALUES (?,?,?)",
            (name, digest, json.dumps(notes)),
        )
        conn.commit()
    finally:
        conn.close()


def _forget(name):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM build_meta WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------

def _current():
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT season, MAX(week) AS week FROM game "
            "WHERE season = (SELECT MAX(season) FROM game)"
        ).fetchone()
        return (row["season"], row["week"]) if row and row["week"] else (None, None)
    finally:
        conn.close()


def _week_final(season, week):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(state = 'post') AS done FROM game "
            "WHERE season = ? AND week = ?", (season, week),
        ).fetchone()
        return row["n"] > 0 and row["n"] == row["done"]
    finally:
        conn.close()


def _week_stored(season, week):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT 1 FROM game WHERE season = ? AND week = ? LIMIT 1", (season, week)
        ).fetchone() is not None
    finally:
        conn.close()


def _week_in_name(path):
    match = re.search(r"week[_-]?(\d+)", path.name, re.IGNORECASE)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------

def _run_import(path, importer):
    """Import one file if it changed. Returns this file's notices."""
    name = f"imports/{path.name}"
    digest = _digest(path)
    stored_digest, stored_notes = _state(name)
    if digest == stored_digest:
        return stored_notes

    try:
        problems = importer(path)
    except NotYet as early:
        _forget(name)
        return [f"{path.name}: {early}"]
    except Exception as exc:
        log.error("Import of %s failed: %s", name, exc)
        _forget(name)
        return [f"{path.name}: could not be imported ({exc}). It will be tried again."]

    shown = [f"{path.name}: {p}" for p in problems]
    if any(RETRY_MARKER in p for p in problems):
        _forget(name)
    else:
        _remember(name, digest, shown)
    return shown


def _import_picks(season, current_week):
    def importer(path):
        week = _week_in_name(path) or int(json.loads(path.read_text())["week"])
        if week > current_week:
            # Polling a future week would move the whole page forward early.
            raise NotYet(f"waiting for week {week} to start; it imports automatically then")
        if not _week_stored(season, week) or not _week_final(season, week):
            poller.poll_once(season=season, week=week, keep_raw=False)
        _, _, (stored, problems) = picks.load_file(path)
        return problems

    notes = []
    for path in sorted(IMPORTS.glob("picks_week_*.json")):
        notes += _run_import(path, importer)
    return notes


def _import_rooting(season):
    def importer(path):
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            week, lines, problems = import_rooting.read_workbook(str(path))
            text = "\n".join(lines)
        else:
            text = path.read_text(encoding="utf-8")
            week, problems = import_rooting.infer_week(text, str(path)), []
        if week is None:
            return ["no week number found; put it in cell B2 or a 'Week N' line"]
        goals, errors = rooting.parse_text(text)
        stored, more = rooting.import_goals(season, week, goals, chooser=None)
        return list(problems) + list(errors) + list(more)

    notes = []
    paths = sorted(IMPORTS.glob("rooting*.xlsx")) + sorted(IMPORTS.glob("rooting*.txt"))
    for path in paths:
        notes += _run_import(path, importer)
    return notes


def _import_manual():
    def importer(path):
        _, _, stored, problems = manual.import_file(str(path), chooser=None)
        return problems

    notes = []
    for path in sorted(IMPORTS.glob("yahoo_week_*.json")):
        notes += _run_import(path, importer)
    return notes


# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.init()
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.close()
    IMPORTS.mkdir(exist_ok=True)

    notes = []
    result = poller.poll_once(keep_raw=False)
    if not result["ok"]:
        notes.append(f"Scores couldn't be refreshed this run ({result['error']}).")

    season, week = _current()
    if season is None:
        print("No games stored and none could be fetched.")
        return 1

    # Earlier weeks feed the Pick'em season total, so make sure their
    # results are final. Once they are, this costs nothing.
    for path in IMPORTS.glob("picks_week_*.json"):
        past = _week_in_name(path)
        if past and past < week and not _week_final(season, past):
            poller.poll_once(season=season, week=past, keep_raw=False)

    notes += _import_picks(season, week)
    notes += _import_rooting(season)
    notes += _import_manual()

    import app   # imported late: it reads the database the steps above filled

    data = app.dashboard_data()
    data["build"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }

    OUT.mkdir(exist_ok=True)
    (OUT / "dashboard.json").write_text(json.dumps(data), encoding="utf-8")

    page = app.index()
    if hasattr(page, "body"):
        page = page.body.decode("utf-8")
    settings = json.dumps({"dataUrl": "dashboard.json", "logo": LOGO})
    assert page.count("<script>") == 1, "expected exactly one script block in the page"
    page = page.replace(
        "<script>", f"<script>window.STATIC_SITE = {settings};</script>\n<script>", 1
    )
    (OUT / "index.html").write_text(page, encoding="utf-8")
    # Pages would otherwise try to process the folder as a Jekyll site.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Built week {data['week']}: {len(data['games'])} games, "
          f"{len(data.get('matchups') or [])} fantasy matchups, "
          f"{len(notes)} notice(s)")
    for note in notes:
        print(f"  notice: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
