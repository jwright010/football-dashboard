"""
ESPN scoreboard poller.

Fetches the NFL scoreboard, normalizes it, and upserts into the game
table. The endpoint is public and undocumented, so every field access
is defensive: a shape change should drop one game, not crash the run.
"""

import json
import logging
from datetime import datetime, timezone

# Reads the Windows/macOS certificate store so corporate TLS inspection
# does not break verification. Must run before httpx is imported.
import truststore
truststore.inject_into_ssl()

import httpx

import db
from clock import parse_clock, is_live

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)

# Akamai fronts this endpoint and rejects a browser User-Agent that
# arrives without the rest of a browser's headers. An honest client
# name passes cleanly and has nothing to keep in sync.
HEADERS = {
    "User-Agent": "football-dashboard/1.0",
}

log = logging.getLogger("poller")


def fetch_scoreboard(season=None, week=None, season_type=2, timeout=15.0):
    """Fetch raw scoreboard JSON.

    With no arguments, ESPN returns the current week, which is what we
    want in normal operation. Explicit season and week are for backfill
    and testing.
    """
    params = {}
    if season is not None:
        params["dates"] = str(season)
    if week is not None:
        params["week"] = str(week)
        params["seasontype"] = str(season_type)

    with httpx.Client(timeout=timeout, headers=HEADERS) as client:
        response = client.get(SCOREBOARD_URL, params=params or None)
        response.raise_for_status()
        return response.json()


def _competitor(competitors, home_away):
    for c in competitors:
        if c.get("homeAway") == home_away:
            return c
    return None


def normalize_event(event):
    """Turn one ESPN event into a flat dict, or None if unusable."""
    try:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        comp = competitions[0]

        competitors = comp.get("competitors") or []
        home = _competitor(competitors, "home")
        away = _competitor(competitors, "away")
        if not home or not away:
            return None

        status = comp.get("status") or event.get("status") or {}
        status_type = status.get("type") or {}

        period = int(status.get("period") or 0)
        clock_seconds = parse_clock(status.get("displayClock") or "")
        state = (status_type.get("state") or "pre").lower()
        status_detail = status_type.get("shortDetail") or status_type.get("detail") or ""

        season_block = event.get("season") or {}
        week_block = event.get("week") or {}

        possession_espn_id = comp.get("situation", {}).get("possession")

        return {
            "event_id": str(event.get("id")),
            "season": int(season_block.get("year") or 0),
            "season_type": int(season_block.get("type") or 2),
            "week": int(week_block.get("number") or 0),
            "kickoff_utc": comp.get("date") or event.get("date") or "",
            "home_espn_id": str((home.get("team") or {}).get("id")),
            "away_espn_id": str((away.get("team") or {}).get("id")),
            "home_score": int(home.get("score") or 0),
            "away_score": int(away.get("score") or 0),
            "period": period,
            "clock_seconds": clock_seconds,
            "state": state,
            "status_detail": status_detail,
            "possession_espn_id": (
                str(possession_espn_id) if possession_espn_id else None
            ),
        }
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        log.warning("Skipped malformed event %s: %s", event.get("id"), exc)
        return None


def upsert_games(conn, rows):
    """Write normalized games. Returns (written, skipped)."""
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    skipped = 0

    for row in rows:
        home_id = db.team_id_by_espn_id(conn, row["home_espn_id"])
        away_id = db.team_id_by_espn_id(conn, row["away_espn_id"])

        # An unmapped team means the seed data is stale. Skip loudly
        # rather than writing a broken row.
        if home_id is None or away_id is None:
            log.error(
                "Unmapped ESPN team id in event %s (home=%s away=%s). "
                "Update TEAMS in db.py.",
                row["event_id"], row["home_espn_id"], row["away_espn_id"],
            )
            skipped += 1
            continue

        possession_id = (
            db.team_id_by_espn_id(conn, row["possession_espn_id"])
            if row["possession_espn_id"]
            else None
        )

        conn.execute(
            """
            INSERT INTO game (
                event_id, season, week, season_type, kickoff_utc,
                home_team_id, away_team_id, home_score, away_score,
                period, clock_seconds, state, status_detail,
                possession_team_id, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                home_score         = excluded.home_score,
                away_score         = excluded.away_score,
                period             = excluded.period,
                clock_seconds      = excluded.clock_seconds,
                state              = excluded.state,
                status_detail      = excluded.status_detail,
                possession_team_id = excluded.possession_team_id,
                kickoff_utc        = excluded.kickoff_utc,
                updated_at         = excluded.updated_at
            """,
            (
                row["event_id"], row["season"], row["week"], row["season_type"],
                row["kickoff_utc"], home_id, away_id,
                row["home_score"], row["away_score"],
                row["period"], row["clock_seconds"], row["state"],
                row["status_detail"], possession_id, now,
            ),
        )
        written += 1

    conn.commit()
    return written, skipped


def poll_once(season=None, week=None, keep_raw=True):
    """One full poll cycle. Returns a summary dict."""
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()

    try:
        payload = fetch_scoreboard(season=season, week=week)
    except Exception as exc:
        conn.execute(
            "INSERT INTO poll_log (fetched_at, source, error) VALUES (?,?,?)",
            (now, "espn_scoreboard", str(exc)),
        )
        conn.commit()
        conn.close()
        log.error("Scoreboard fetch failed: %s", exc)
        return {"ok": False, "error": str(exc), "written": 0, "skipped": 0}

    events = payload.get("events") or []
    rows = [r for r in (normalize_event(e) for e in events) if r]
    written, skipped = upsert_games(conn, rows)

    conn.execute(
        """INSERT INTO poll_log
           (fetched_at, source, http_status, game_count, raw_json)
           VALUES (?,?,?,?,?)""",
        (
            now, "espn_scoreboard", 200, written,
            json.dumps(payload) if keep_raw else None,
        ),
    )
    conn.commit()

    live = sum(1 for r in rows if is_live(r["state"], r["status_detail"]))
    conn.close()

    return {
        "ok": True,
        "events": len(events),
        "written": written,
        "skipped": skipped,
        "live": live,
    }


def next_interval_seconds(conn):
    """Adaptive polling cadence.

    25s while any game is live, 10 min when games are scheduled today
    but none have started, 1 hour otherwise.
    """
    live = conn.execute(
        "SELECT COUNT(*) FROM game WHERE state = 'in'"
    ).fetchone()[0]
    if live:
        return 25

    today = datetime.now(timezone.utc).date().isoformat()
    upcoming = conn.execute(
        "SELECT COUNT(*) FROM game WHERE state = 'pre' AND kickoff_utc LIKE ?",
        (f"{today}%",),
    ).fetchone()[0]
    return 600 if upcoming else 3600


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.init()
    result = poll_once()
    if result["ok"]:
        print(
            f"Fetched {result['events']} events, "
            f"wrote {result['written']}, skipped {result['skipped']}, "
            f"{result['live']} live"
        )
    else:
        print(f"Failed: {result['error']}")
