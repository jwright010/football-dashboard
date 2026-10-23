"""
SQLite schema and team seed data.

The team table exists to reconcile abbreviation differences between
ESPN's sports API, Yahoo Fantasy, and ESPN Fantasy. Everything
downstream joins through abbrev_canonical.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "football.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS team (
    id                   INTEGER PRIMARY KEY,
    espn_id              TEXT    UNIQUE NOT NULL,
    abbrev_canonical     TEXT    UNIQUE NOT NULL,
    abbrev_yahoo         TEXT,
    abbrev_espn_fantasy  TEXT,
    location             TEXT    NOT NULL,
    display_name         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS game (
    event_id            TEXT    PRIMARY KEY,
    season              INTEGER NOT NULL,
    week                INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,
    kickoff_utc         TEXT    NOT NULL,
    home_team_id        INTEGER NOT NULL REFERENCES team(id),
    away_team_id        INTEGER NOT NULL REFERENCES team(id),
    home_score          INTEGER NOT NULL DEFAULT 0,
    away_score          INTEGER NOT NULL DEFAULT 0,
    period              INTEGER NOT NULL DEFAULT 0,
    clock_seconds       INTEGER NOT NULL DEFAULT 0,
    state               TEXT    NOT NULL DEFAULT 'pre',
    status_detail       TEXT,
    possession_team_id  INTEGER REFERENCES team(id),
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_game_week  ON game(season, season_type, week);
CREATE INDEX IF NOT EXISTS idx_game_state ON game(state);

CREATE TABLE IF NOT EXISTS pick (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    event_id    TEXT    NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES team(id),
    entered_at  TEXT    NOT NULL,
    PRIMARY KEY (season, week, event_id)
);

CREATE INDEX IF NOT EXISTS idx_pick_week ON pick(season, week);

CREATE TABLE IF NOT EXISTS rooting (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    kind         TEXT    NOT NULL,          -- 'player' or 'team'
    label        TEXT    NOT NULL,          -- as typed, for display
    team_id      INTEGER NOT NULL REFERENCES team(id),
    athlete_id   TEXT,                      -- resolved once, then permanent
    stat         TEXT    NOT NULL,          -- key from boxscore.STATS
    direction    TEXT    NOT NULL,          -- 'over' or 'under'
    target       REAL    NOT NULL,
    entered_at   TEXT    NOT NULL,
    bozo         INTEGER NOT NULL DEFAULT 0,   -- marked Yes in the BOZO column
    wording      TEXT,                      -- your own wording, from the Revised column
    position     TEXT,                      -- player position, from the roster
    UNIQUE (season, week, kind, label, stat, direction, target)
);

CREATE INDEX IF NOT EXISTS idx_rooting_week ON rooting(season, week);

CREATE TABLE IF NOT EXISTS poll_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   TEXT NOT NULL,
    source       TEXT NOT NULL,
    http_status  INTEGER,
    game_count   INTEGER,
    error        TEXT,
    raw_json     TEXT
);
"""

# espn_id, canonical, yahoo, espn_fantasy, location, name
# Canonical follows ESPN's sports API. The alias columns are best-known
# values and are verified at runtime by validate_teams.py.
TEAMS = [
    ("22", "ARI", "ARI", "ARI", "Arizona", "Cardinals"),
    ("1",  "ATL", "ATL", "ATL", "Atlanta", "Falcons"),
    ("33", "BAL", "BAL", "BAL", "Baltimore", "Ravens"),
    ("2",  "BUF", "BUF", "BUF", "Buffalo", "Bills"),
    ("29", "CAR", "CAR", "CAR", "Carolina", "Panthers"),
    ("3",  "CHI", "CHI", "CHI", "Chicago", "Bears"),
    ("4",  "CIN", "CIN", "CIN", "Cincinnati", "Bengals"),
    ("5",  "CLE", "CLE", "CLE", "Cleveland", "Browns"),
    ("6",  "DAL", "DAL", "DAL", "Dallas", "Cowboys"),
    ("7",  "DEN", "DEN", "DEN", "Denver", "Broncos"),
    ("8",  "DET", "DET", "DET", "Detroit", "Lions"),
    ("9",  "GB",  "GB",  "GB",  "Green Bay", "Packers"),
    ("34", "HOU", "HOU", "HOU", "Houston", "Texans"),
    ("11", "IND", "IND", "IND", "Indianapolis", "Colts"),
    ("30", "JAX", "JAX", "JAX", "Jacksonville", "Jaguars"),
    ("12", "KC",  "KC",  "KC",  "Kansas City", "Chiefs"),
    ("13", "LV",  "LV",  "LV",  "Las Vegas", "Raiders"),
    ("24", "LAC", "LAC", "LAC", "Los Angeles", "Chargers"),
    ("14", "LAR", "LAR", "LAR", "Los Angeles", "Rams"),
    ("15", "MIA", "MIA", "MIA", "Miami", "Dolphins"),
    ("16", "MIN", "MIN", "MIN", "Minnesota", "Vikings"),
    ("17", "NE",  "NE",  "NE",  "New England", "Patriots"),
    ("18", "NO",  "NO",  "NO",  "New Orleans", "Saints"),
    ("19", "NYG", "NYG", "NYG", "New York", "Giants"),
    ("20", "NYJ", "NYJ", "NYJ", "New York", "Jets"),
    ("21", "PHI", "PHI", "PHI", "Philadelphia", "Eagles"),
    ("23", "PIT", "PIT", "PIT", "Pittsburgh", "Steelers"),
    ("25", "SF",  "SF",  "SF",  "San Francisco", "49ers"),
    ("26", "SEA", "SEA", "SEA", "Seattle", "Seahawks"),
    ("27", "TB",  "TB",  "TB",  "Tampa Bay", "Buccaneers"),
    ("10", "TEN", "TEN", "TEN", "Tennessee", "Titans"),
    ("28", "WSH", "WAS", "WSH", "Washington", "Commanders"),
]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_missing_columns(conn):
    """Bring an older database up to date without losing anything.

    CREATE TABLE IF NOT EXISTS never changes a table that already exists,
    so columns added after a database was made are added here.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(rooting)")}
    added = {
        "bozo": "INTEGER NOT NULL DEFAULT 0",
        "wording": "TEXT",
        "position": "TEXT",
    }
    for name, kind in added.items():
        if name not in have:
            conn.execute(f"ALTER TABLE rooting ADD COLUMN {name} {kind}")
    conn.commit()


def init():
    """Create tables and seed teams. Safe to run repeatedly."""
    conn = connect()
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.executemany(
        """
        INSERT INTO team
            (espn_id, abbrev_canonical, abbrev_yahoo,
             abbrev_espn_fantasy, location, display_name)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(espn_id) DO UPDATE SET
            abbrev_canonical    = excluded.abbrev_canonical,
            abbrev_yahoo        = excluded.abbrev_yahoo,
            abbrev_espn_fantasy = excluded.abbrev_espn_fantasy,
            location            = excluded.location,
            display_name        = excluded.display_name
        """,
        TEAMS,
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM team").fetchone()[0]
    conn.close()
    return count


def team_id_by_espn_id(conn, espn_id):
    row = conn.execute(
        "SELECT id FROM team WHERE espn_id = ?", (str(espn_id),)
    ).fetchone()
    return row["id"] if row else None


if __name__ == "__main__":
    n = init()
    print(f"Database ready at {DB_PATH}")
    print(f"{n} teams seeded")
