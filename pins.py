"""
Pinned players for the Top Players view.

Pins live in the local database rather than the browser, so the TV
window and the laptop window always agree and a reload keeps them.
Each pin belongs to one week, which is how they clear themselves when a
new week starts: the dashboard only reads the current week's pins.

A player is identified by the same key the player view merges on,
normalized name plus pro team, so a pin follows the person across every
league and rooting goal they appear in.

This module creates its own table, so no database migration is needed.
"""

from datetime import datetime, timezone

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS pin (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    player_key  TEXT    NOT NULL,
    pinned_at   TEXT    NOT NULL,
    PRIMARY KEY (season, week, player_key)
);
"""


def ensure_table():
    conn = db.connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def current(season, week):
    """Keys pinned for this week."""
    ensure_table()
    conn = db.connect()
    try:
        return {
            r["player_key"] for r in conn.execute(
                "SELECT player_key FROM pin WHERE season = ? AND week = ?",
                (season, week),
            ).fetchall()
        }
    finally:
        conn.close()


def set_pin(season, week, key, on):
    """Pin or unpin one player for this week. Earlier weeks are tidied away."""
    ensure_table()
    conn = db.connect()
    try:
        if on:
            conn.execute(
                "INSERT OR REPLACE INTO pin (season, week, player_key, pinned_at) "
                "VALUES (?, ?, ?, ?)",
                (season, week, key, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute(
                "DELETE FROM pin WHERE season = ? AND week = ? AND player_key = ?",
                (season, week, key),
            )
        conn.execute(
            "DELETE FROM pin WHERE season < ? OR (season = ? AND week < ?)",
            (season, season, week),
        )
        conn.commit()
    finally:
        conn.close()
