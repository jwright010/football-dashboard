"""
Dashboard web app.

Serves the page and runs the pollers in the background. Start with:

    uvicorn app:app --port 8000

Then open http://localhost:8000
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
import fantasy
import picks
import pins
import players
import poller
import rooting
from clock import (
    describe,
    format_remaining,
    is_live,
    period_label,
    total_remaining_seconds,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("app")

TICK_SECONDS = 20

# --- TV view sizing --------------------------------------------------
# Placeholders for your setup. Change these two numbers and the TV view
# rescales; nothing else needs touching.
TV_DISTANCE_FEET = 15
TV_DIAGONAL_INCHES = 40

# Roughly a third of an inch of letter height per ten feet of distance
# reads comfortably at a glance. At 15 feet that is half an inch.
LETTER_INCHES_PER_10_FEET = 1 / 3

# Fine-tuning on top of the distance math. 1.0 is exactly what the math
# asks for; lower is smaller. 0.8 sits about midway between that and the
# Standard view. Lower values fit more on screen but read harder from
# across the room.
TV_SIZE_ADJUST = 0.8


def tv_base_vh():
    """Base text size for the TV view, as a share of screen height.

    Expressed against screen height rather than pixels, so the result is
    the same on any TV resolution and whether the laptop mirrors or
    extends to it.
    """
    screen_height = TV_DIAGONAL_INCHES * 9 / (16 ** 2 + 9 ** 2) ** 0.5
    letter = TV_DISTANCE_FEET / 10 * LETTER_INCHES_PER_10_FEET
    return round(letter / screen_height * 100 * TV_SIZE_ADJUST, 3)
_last_poll = {"at": None}


def tick():
    conn = db.connect()
    try:
        interval = poller.next_interval_seconds(conn)
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    last = _last_poll["at"]
    if last and (now - last).total_seconds() < interval:
        return

    result = poller.poll_once(keep_raw=False)
    if result["ok"]:
        _last_poll["at"] = now
        log.info(
            "Polled: %d events, %d live, next check in %ds",
            result["events"], result["live"], interval,
        )
    else:
        log.error("Poll failed: %s", result["error"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    tick()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(tick, "interval", seconds=TICK_SECONDS, id="poll")
    scheduler.start()
    log.info("Dashboard running at http://localhost:8000")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, title="Football Dashboard")

# Team logos live on disk, so the page makes no external requests.
# The folder is created even when empty: a missing logo simply hides
# the image rather than breaking the row.
STATIC_DIR = Path(__file__).parent / "static"
(STATIC_DIR / "logos").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def to_local(iso_utc):
    if not iso_utc:
        return None
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def clock_12h(dt, with_seconds=False):
    """Windows rejects %-I and Linux rejects %#I, so build the hour by hand."""
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    if with_seconds:
        return f"{hour}:{dt.minute:02d}:{dt.second:02d} {suffix}"
    return f"{hour}:{dt.minute:02d} {suffix}"


def kickoff_label(dt):
    return f"{dt.strftime('%a')} {clock_12h(dt)}" if dt else ""


def load_games():
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT g.*,
               ta.abbrev_canonical AS away_abbrev,
               th.abbrev_canonical AS home_abbrev,
               tp.abbrev_canonical AS poss_abbrev
        FROM game g
        JOIN team ta ON ta.id = g.away_team_id
        JOIN team th ON th.id = g.home_team_id
        LEFT JOIN team tp ON tp.id = g.possession_team_id
        WHERE g.season = (SELECT MAX(season) FROM game)
          AND g.week   = (SELECT MAX(week) FROM game
                          WHERE season = (SELECT MAX(season) FROM game))
        ORDER BY g.kickoff_utc, g.event_id
        """
    ).fetchall()
    conn.close()

    games = []
    for r in rows:
        live = is_live(r["state"], r["status_detail"])
        remaining = total_remaining_seconds(r["period"], r["clock_seconds"], r["state"])
        games.append({
            "event_id": r["event_id"],
            "week": r["week"],
            "away": r["away_abbrev"],
            "home": r["home_abbrev"],
            "away_score": r["away_score"],
            "home_score": r["home_score"],
            "state": r["state"],
            "is_live": live,
            "label": period_label(
                r["period"], r["clock_seconds"], r["state"], r["status_detail"]
            ),
            "remaining": format_remaining(remaining) if live else None,
            "kickoff_local": kickoff_label(to_local(r["kickoff_utc"])),
            "possession": r["poss_abbrev"],
            "line": describe(
                r["away_abbrev"], r["away_score"], r["home_abbrev"], r["home_score"],
                r["period"], r["clock_seconds"], r["state"], r["status_detail"],
            ),
        })
    return games


def dashboard_data():
    """Everything the page draws, as one bundle.

    The local server returns this from /api/dashboard. The phone build
    calls it directly and saves the result as a file, so both versions
    are built from exactly the same assembly.
    """
    games = load_games()
    try:
        matchups = fantasy.get_matchups()
    except Exception as exc:
        log.error("Fantasy layer failed: %s", exc)
        matchups = []

    try:
        pickem = picks.summary()
    except Exception as exc:
        log.error("Pick'em layer failed: %s", exc)
        pickem = None

    try:
        roster_view = players.build()
    except Exception as exc:
        log.error("Player view failed: %s", exc)
        roster_view = None

    try:
        rooting_for = players.annotate_rooting(rooting.summary_for_dashboard())
    except Exception as exc:
        log.error("Rooting layer failed: %s", exc)
        rooting_for = None

    return {
        "week": games[0]["week"] if games else None,
        "updated": clock_12h(datetime.now(), with_seconds=True),
        "live_count": sum(1 for g in games if g["is_live"]),
        "games": games,
        "matchups": matchups,
        "pickem": pickem,
        "rooting": rooting_for,
        "players": roster_view,
    }


@app.get("/api/dashboard")
def api_dashboard():
    return JSONResponse(dashboard_data())


@app.post("/api/pin")
def api_pin(key: str, on: int = 1):
    """Pin (on=1) or unpin (on=0) one player for the current week."""
    current = players._current_week()
    if not current:
        return JSONResponse({"ok": False, "error": "no games stored"}, status_code=409)
    season, week = current
    pins.set_pin(season, week, key, bool(on))
    return JSONResponse({"ok": True, "key": key, "pinned": bool(on)})


@app.get("/api/games")
def api_games():
    games = load_games()
    return JSONResponse({
        "week": games[0]["week"] if games else None,
        "updated": clock_12h(datetime.now(), with_seconds=True),
        "live_count": sum(1 for g in games if g["is_live"]),
        "games": games,
    })


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__TV_BASE_VH__", f"{tv_base_vh()}vh")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Football Dashboard</title>
<style>
  :root {
    --bg:        #0E1420;
    --surface:   #151D2C;
    --rule:      #212C40;
    --text:      #E8ECF4;
    --dim:       #6B7A93;
    --faint:     #465873;
    --live:      #FFB627;
    --ahead:     #4ADE80;
    --behind:    #F87171;
  }

  * { box-sizing: border-box; }
  html { background: var(--bg); }

  body {
    margin: 0;
    padding: 2.5rem 1.25rem 4rem;
    background: var(--bg);
    color: var(--text);
    font-family: "Inter", "Segoe UI Variable Text", "Segoe UI",
                 system-ui, -apple-system, sans-serif;
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }

  .wrap { max-width: 66rem; margin: 0 auto; }

  header {
    display: flex; justify-content: space-between; align-items: baseline;
    padding-bottom: 1.25rem; border-bottom: 1px solid var(--rule);
  }
  h1 { margin: 0; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; }
  .meta { font-size: 0.8125rem; color: var(--dim); text-align: right; }
  .meta .live-count { color: var(--live); }

  h2 {
    margin: 2.25rem 0 0.25rem;
    font-size: 0.875rem; font-weight: 500;
    color: var(--dim); letter-spacing: 0.01em;
  }

  /* ---------- fantasy ---------- */

  .matchup {
    margin-top: 0.75rem; padding: 1.125rem 1.25rem;
    background: var(--surface); border-radius: 0.375rem;
    border: 1px solid var(--rule);
  }

  .matchup-head {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 0.875rem;
  }
  .platform { font-size: 0.75rem; color: var(--dim); letter-spacing: 0.04em; }

  .side {
    display: grid; grid-template-columns: 1fr auto;
    gap: 1rem; align-items: baseline;
    padding: 0.3125rem 0;
  }
  .side-name { font-size: 1rem; font-weight: 500; }
  .side-score { font-size: 1.375rem; font-weight: 600; letter-spacing: -0.01em; }
  .side.them .side-name  { color: var(--dim); }
  .side.them .side-score { color: var(--dim); }

  .matchup-meta {
    margin-top: 0.75rem; padding-top: 0.75rem;
    border-top: 1px solid var(--rule);
    font-size: 0.8125rem; color: var(--dim);
    display: flex; gap: 1rem; flex-wrap: wrap;
  }
  .ahead  { color: var(--ahead); }
  .behind { color: var(--behind); }
  .inplay { color: var(--live); }
  .side-score.ahead  { color: var(--ahead); }
  .side-score.behind { color: var(--behind); }

  .player-group { margin-top: 1rem; }
  .player-group > .group-label {
    font-size: 0.75rem; color: var(--faint);
    margin-bottom: 0.375rem;
  }

  .player {
    display: grid;
    grid-template-columns: 2.75rem 1fr 3.25rem;
    gap: 0.625rem; align-items: baseline;
    padding: 0.3125rem 0;
    font-size: 0.875rem;
  }
  .player .slot  { color: var(--faint); font-size: 0.75rem; }
  .player .pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .player .pts   { text-align: right; font-weight: 600; }
  .player .gm {
    grid-column: 2 / -1;
    font-size: 0.75rem; color: var(--live);
    margin-top: -0.1875rem;
  }
  .player .inj { color: var(--behind); font-size: 0.6875rem; margin-left: 0.25rem; }

  .note { font-size: 0.8125rem; color: var(--faint); padding: 0.5rem 0; }

  /* team logo, shared by both sections */
  .logo {
    width: 1.75rem; height: 1.75rem;
    flex: 0 0 1.75rem;
    object-fit: contain;
  }
  .logo.sm { width: 1.375rem; height: 1.375rem; flex-basis: 1.375rem; }

  /* merged player rows */
  .pl {
    display: flex; gap: 0.75rem; align-items: flex-start;
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--rule);
  }
  .pl:last-child { border-bottom: none; }
  .pl-body { flex: 1 1 auto; min-width: 0; }

  .pl-head {
    display: grid; grid-template-columns: 1fr auto;
    gap: 0.75rem; align-items: baseline;
  }
  .pl-name { font-size: 0.9375rem; font-weight: 500; }
  .pin {
    background: transparent; border: 1px solid var(--rule); color: var(--dim);
    font: inherit; font-size: 0.7rem; line-height: 1.2;
    padding: 0.12rem 0.55rem; border-radius: 999px; cursor: pointer; white-space: nowrap;
  }
  .pin:hover { color: var(--text); border-color: var(--dim); }
  .pin.on { border-color: var(--live); color: var(--live); }
  .pin:disabled { opacity: 0.5; cursor: progress; }
  .pin:focus-visible { outline: 2px solid var(--live); outline-offset: 1px; }
  .pl-meta { font-size: 0.75rem; color: var(--faint); margin-left: 0.4375rem; white-space: nowrap; }
  .pl-when { font-size: 0.75rem; color: var(--faint); margin-top: 0.1875rem; }
  .pl.islive .pl-when { color: var(--live); }

  .pl-goal { margin-top: 0.5rem; }
  .pl-goal-top {
    display: grid; grid-template-columns: 1fr auto auto;
    gap: 0.625rem; align-items: baseline;
    font-size: 0.8125rem;
  }
  .pl-goal-what { color: var(--dim); }
  .pl-goal-pct  { font-weight: 600; font-variant-numeric: tabular-nums; }
  .pl-goal-num  { color: var(--faint); font-variant-numeric: tabular-nums; }
  .pl-goal-num b { color: var(--text); font-weight: 600; }

  .pl-fan {
    margin-top: 0.5rem; padding-left: 0.75rem;
    border-left: 1px solid var(--rule);
  }
  .pl-fan.nested { margin-top: 0.625rem; }
  .pl-fan-row {
    display: grid; grid-template-columns: auto 1fr auto;
    gap: 0.5rem; align-items: baseline;
    font-size: 0.8125rem; padding: 0.125rem 0;
  }
  .pl-fan-plat {
    font-size: 0.6875rem; color: var(--faint);
    letter-spacing: 0.03em;
  }
  .pl-fan-team { color: var(--dim); overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .pl-fan-pts  { font-weight: 600; font-variant-numeric: tabular-nums; }
  .pl-fan-unit { font-size: 0.6875rem; color: var(--faint); font-weight: 400; }

  .goal.achieved .pl-goal-pct { color: var(--ahead); }

  .goal {
    display: flex; gap: 0.625rem; align-items: flex-start;
    padding: 0.5625rem 0; border-bottom: 1px solid var(--rule);
  }
  .goal:last-child { border-bottom: none; }
  .goal-body { flex: 1 1 auto; min-width: 0; }
  .pl-goal.goal { border-bottom: none; padding-left: 0; }
  .pl-goal.goal.live { padding-left: 0.6rem; }

  .goal-top {
    display: grid; grid-template-columns: 1fr auto auto;
    gap: 0.75rem; align-items: baseline;
  }
  .goal-who  { font-size: 0.9375rem; font-weight: 500; }
  .goal-what { font-size: 0.8125rem; color: var(--dim); }
  .goal-now  { font-size: 0.9375rem; font-weight: 600; text-align: right; }
  .goal-now .of { font-size: 0.75rem; color: var(--faint); font-weight: 400; }

  .bar {
    margin-top: 0.4375rem; height: 0.1875rem;
    background: var(--rule); border-radius: 999px; overflow: hidden;
  }
  .bar > span { display: block; height: 100%; border-radius: 999px; }

  .goal-when {
    margin-top: 0.3125rem;
    font-size: 0.75rem; color: var(--faint);
  }

  .goal.achieved .goal-now { color: var(--ahead); }
  .goal.achieved .bar > span { background: var(--ahead); }
  .goal.live     .goal-now { color: var(--live); }
  .goal.live     .bar > span { background: var(--live); }
  .goal.live     .goal-when { color: var(--live); }
  .goal.failed   .goal-who,
  .goal.failed   .goal-what,
  .goal.failed   .goal-now { color: var(--faint); }
  .goal.failed   .bar > span { background: var(--behind); opacity: 0.5; }
  .goal.pending  .goal-who,
  .goal.pending  .goal-what,
  .goal.pending  .goal-now { color: var(--faint); }
  .goal.noshow   .goal-now { color: var(--behind); }

  .tally {
    display: flex; align-items: baseline; gap: 0.75rem;
    margin-bottom: 0.25rem;
  }
  .tally-count {
    font-size: 2.25rem; font-weight: 600;
    letter-spacing: -0.03em; line-height: 1;
  }
  .tally-of { font-size: 0.875rem; color: var(--dim); }

  .pick {
    display: grid;
    grid-template-columns: 0.75rem 1fr auto;
    gap: 0.5rem; align-items: baseline;
    padding: 0.28125rem 0;
    font-size: 0.875rem;
  }
  .pick .mark  { font-weight: 600; }
  .pick .vs    { color: var(--dim); font-size: 0.8125rem; }
  .pick .state { font-size: 0.8125rem; color: var(--dim); text-align: right; }

  .pick.won      .mark  { color: var(--ahead); }
  .pick.leading  .mark  { color: var(--live); }
  .pick.leading  .state { color: var(--live); }
  .pick.lost     .team,
  .pick.lost     .vs,
  .pick.lost     .state { color: var(--faint); }
  .pick.lost     .mark  { color: var(--behind); }
  .pick.trailing .mark  { color: var(--behind); }
  .pick.pending  .team,
  .pick.pending  .vs,
  .pick.pending  .state { color: var(--faint); }
  .err  { font-size: 0.8125rem; color: var(--behind); padding: 0.5rem 0; }

  /* ---------- games ---------- */

  .game {
    display: grid; grid-template-columns: 1fr auto;
    align-items: center; gap: 1rem;
    padding: 0.875rem 0.875rem 0.875rem 1rem;
    border-bottom: 1px solid var(--rule);
  }
  .teams { display: grid; gap: 0.25rem; }
  .team {
    display: grid; grid-template-columns: 3.25rem 2.5rem auto;
    align-items: center; font-size: 1.0625rem;
  }
  .abbrev { font-weight: 600; letter-spacing: 0.01em; }
  .score  { font-weight: 600; text-align: right; }
  .ball {
    display: inline-block;
    width: 0.4375rem; height: 0.4375rem; margin-left: 0.625rem;
    border-radius: 50%; background: var(--live); vertical-align: middle;
  }
  .status { text-align: right; font-size: 0.9375rem; line-height: 1.4; }
  .status .clock { font-weight: 600; }
  .status .sub { display: block; font-size: 0.8125rem; color: var(--dim); }

  .live {
    background: var(--surface);
    border-left: 2px solid var(--live);
    padding-left: 0.875rem;
  }
  .live .status .clock { color: var(--live); }
  .final .abbrev, .final .score, .final .status { color: var(--faint); }
  .final .winner .abbrev, .final .winner .score { color: var(--text); }
  .pre .score { color: var(--faint); }
  .pre .abbrev, .pre .status { color: var(--dim); }

  .empty { padding: 3rem 0; color: var(--dim); }


  /* ---------- view switcher ---------- */
  .head-right { display: flex; align-items: baseline; gap: 1rem; }
  .modes {
    display: inline-flex; border: 1px solid var(--rule);
    border-radius: 0.375rem; overflow: hidden;
  }
  .modes button {
    background: transparent; color: var(--dim); border: 0;
    padding: 0.3rem 0.7rem; font: inherit; font-size: 0.75rem; cursor: pointer;
  }
  .modes button + button { border-left: 1px solid var(--rule); }
  .modes button.on { background: var(--surface); color: var(--text); }
  .modes button:focus-visible { outline: 2px solid var(--live); outline-offset: -2px; }

  #notices {
    margin-top: 1rem; padding: 0.7rem 0.9rem; border-radius: 0.375rem;
    background: rgba(250, 204, 21, 0.08); border: 1px solid rgba(250, 204, 21, 0.35);
    font-size: 0.8rem; color: var(--text);
  }
  #notices b { color: var(--close); font-weight: 600; }
  #notices ul { margin: 0.35rem 0 0; padding-left: 1.1rem; }
  #notices li { margin: 0.15rem 0; color: var(--dim); }

  .pace { display: block; margin-top: 0.25rem; font-size: 0.75rem; color: var(--dim); }

  /* ---------- change flash ---------- */
  :root {
    --flash-gain:  rgba(74, 222, 128, 0.15);
    --flash-loss:  rgba(248, 113, 113, 0.17);
    --flash-score: rgba(255, 182, 39, 0.14);
  }
  @keyframes flash-gain  { from { background-color: var(--flash-gain); }  to { background-color: transparent; } }
  @keyframes flash-loss  { from { background-color: var(--flash-loss); }  to { background-color: transparent; } }
  @keyframes flash-score { from { background-color: var(--flash-score); } to { background-color: transparent; } }
  .flash-gain  { animation: flash-gain  2s ease-out 1; border-radius: 0.3rem; }
  .flash-loss  { animation: flash-loss  2s ease-out 1; border-radius: 0.3rem; }
  .flash-score { animation: flash-score 2s ease-out 1; border-radius: 0.3rem; }

  /* ---------- TV view ---------- */
  /* Everything is sized in rem, so resetting the root size scales the
     whole layout for viewing distance. */
  html.tv { font-size: __TV_BASE_VH__; }
  html.tv body { padding: 1.1rem 1.6rem 2rem; }
  html.tv .wrap { max-width: none; }
  .tv-grid {
    display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
    gap: 1.6rem; align-items: start;
  }
  html.tv h2 { margin-top: 1.1rem; }
  html.tv .logo { width: 2.1rem; height: 2.1rem; flex-basis: 2.1rem; }
  html.tv .pl-name { font-size: 1.05rem; }
  html.tv .pl-goal-pct,
  html.tv .pl-goal-num b,
  html.tv .pl-fan-pts,
  html.tv .goal-now { font-size: 1.3rem; }
  html.tv .bar { height: 0.3rem; }
  html.tv .side-score { font-size: 1.7rem; }
  .tv-summary .side { padding: 0.15rem 0; }
  html.tv .pl { padding: 0.45rem 0; gap: 0.6rem; }
  html.tv .pl-when { margin-top: 0.05rem; }
  html.tv .pl-goal { margin-top: 0.25rem; }
  html.tv .pl-fan { margin-top: 0.25rem; }
  html.tv .pl-fan.nested { margin-top: 0.3rem; }
  html.tv .pl-fan-row { padding: 0; }
  html.tv .matchup { padding: 0.8rem 1rem; }
  html.tv .goal { padding: 0.4rem 0; }


  /* ---------- standard view: side by side ---------- */
  .pair {
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 1.25rem; align-items: start;
  }
  @media (max-width: 58rem) { .pair { grid-template-columns: 1fr; } }

  /* ---------- status palette ---------- */
  :root { --close: #FACC15; --pending: #9AA4B2; --final: #7DD3FC; }

  /* ---------- TV tabs ---------- */
  .tabs {
    display: flex; flex-wrap: wrap; gap: 0.25rem;
    margin: 0.9rem 0 0.4rem; border-bottom: 1px solid var(--rule);
  }
  .tabs button {
    background: transparent; border: 0; color: var(--dim); font: inherit;
    font-size: 0.95rem; padding: 0.45rem 0.9rem; cursor: pointer;
    border-bottom: 3px solid transparent; margin-bottom: -1px;
  }
  .tabs button.on { color: var(--text); border-bottom-color: var(--live); }
  .tabs button:focus-visible { outline: 2px solid var(--live); outline-offset: -2px; }
  .cols2 {
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 1.4rem; align-items: start;
  }

  /* ---------- Fantasy tab ---------- */
  .fx-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr));
    gap: 1rem; margin-top: 0.75rem;
  }
  .fx {
    background: var(--surface); border: 1px solid var(--rule);
    border-left: 0.4rem solid var(--pending);
    border-radius: 0.5rem; padding: 0.9rem 1.1rem;
  }
  .fx.win   { border-left-color: var(--ahead); }
  .fx.loss  { border-left-color: var(--behind); }
  .fx.close { border-left-color: var(--close); }
  .fx-verdict { margin-top: 0.6rem; font-size: 0.8rem; font-weight: 600; }
  .fx.win   .fx-verdict { color: var(--ahead); }
  .fx.loss  .fx-verdict { color: var(--behind); }
  .fx.close .fx-verdict { color: var(--close); }
  .fx.none  .fx-verdict { color: var(--pending); }
  .fx-proj { font-size: 0.78rem; color: var(--dim); margin-top: 0.35rem; }

  /* ---------- track indicators ---------- */
  .trk {
    display: inline-flex; align-items: center; gap: 0.35rem;
    font-size: 0.72rem; font-weight: 600; white-space: nowrap;
    padding: 0.12rem 0.55rem; border-radius: 999px;
  }
  .trk .dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; display: inline-block; }
  .trk.on      { background: rgba(74, 222, 128, 0.15);  color: var(--ahead); }
  .trk.off     { background: rgba(248, 113, 113, 0.15); color: var(--behind); }
  .trk.open    { background: rgba(255, 255, 255, 0.06); color: var(--dim); }
  .trk.pending { background: rgba(154, 164, 178, 0.16); color: #C7CED8; }
  .trk.final_hit, .trk.final_miss { background: rgba(125, 211, 252, 0.16); color: #BAE6FD; }
  .trk.on .dot         { background: var(--ahead); }
  .trk.off .dot        { background: var(--behind); }
  .trk.open .dot       { background: var(--dim); }
  .trk.pending .dot    { background: var(--pending); }
  .trk.final_hit .dot  { background: var(--ahead); }
  .trk.final_miss .dot { background: var(--behind); }
  .explore {
    font-size: 0.66rem; color: var(--dim); white-space: nowrap;
    border: 1px dashed var(--faint); border-radius: 0.25rem; padding: 0.05rem 0.4rem;
  }

  /* ---------- Rooting Interests tab ---------- */
  .ri-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.9rem; }
  .ri {
    display: flex; gap: 0.7rem; align-items: flex-start;
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 0.5rem; padding: 0.8rem 1rem;
  }
  .ri-body { flex: 1 1 auto; min-width: 0; }
  .ri-top { display: grid; grid-template-columns: 1fr auto; gap: 0.6rem; align-items: baseline; }
  .ri-who { font-size: 1rem; font-weight: 500; }
  .ri-target { font-size: 0.8rem; color: var(--dim); }
  .ri-now { font-size: 1.1rem; font-weight: 600; text-align: right; white-space: nowrap; }
  .ri-now .of { font-size: 0.75rem; color: var(--faint); font-weight: 400; }
  .ri-bar { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.45rem; }
  .ri-bar .bar { flex: 1 1 auto; margin-top: 0; }
  .ri-pct { font-size: 0.85rem; font-weight: 600; min-width: 2.8rem; text-align: right; }
  .ri-foot {
    display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center;
    margin-top: 0.5rem; font-size: 0.75rem; color: var(--faint);
  }
  .ri.on .bar > span         { background: var(--ahead); }
  .ri.off .bar > span        { background: var(--behind); }
  .ri.open .bar > span       { background: var(--live); }
  .ri.pending .bar > span    { background: var(--pending); }
  .ri.final_hit .bar > span,
  .ri.final_miss .bar > span { background: var(--final); }
  /* Finished goals: name and numbers drop to light gray. */
  .ri.final_hit .ri-who,  .ri.final_miss .ri-who,
  .ri.final_hit .ri-now,  .ri.final_miss .ri-now,
  .ri.final_hit .ri-pct,  .ri.final_miss .ri-pct { color: var(--pending); }

  /* ---------- embed: ?view=bozo ---------- */
  html.embed .modes { display: none; }
  html.embed body { padding: 0.75rem; }
  html.embed .wrap { max-width: none; }

  /* ---------- BOZO tab ---------- */
  .bozo-summary {
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 0.5rem; padding: 0.9rem 1.1rem; margin: 0.75rem 0 1rem;
  }
  .bozo-hit { font-size: 1.2rem; font-weight: 600; }
  .bozo-hit b { font-size: 1.9rem; margin-right: 0.2rem; }
  .bozo-states { margin-top: 0.3rem; font-size: 0.9rem; color: var(--dim); }
  .bz-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
  @media (max-width: 50rem) { .bz-grid { grid-template-columns: 1fr; } }
  .bz-card {
    display: flex; gap: 0.9rem; align-items: flex-start;
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 0.5rem; padding: 1rem 1.2rem;
  }
  .bz-grid .bz-card .logo { width: 3rem; height: 3rem; flex-basis: 3rem; }
  .bz-body { flex: 1 1 auto; min-width: 0; }
  .bz-name { font-size: 1.35rem; font-weight: 500; line-height: 1.2; }
  .bz-meta { font-size: 0.8rem; color: var(--faint); margin-top: 0.15rem; }
  .bz-when { font-size: 0.8rem; color: var(--faint); margin-top: 0.15rem; }
  .bz-when.is-live { color: var(--live); }
  .bz-goal {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 0.8rem; margin-top: 0.55rem;
  }
  .bz-detail { font-size: 0.95rem; color: var(--dim); }
  .bz-stat { font-size: 1.6rem; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bz-stat.good { color: var(--ahead); }
  .bz-stat.bad  { color: var(--behind); }
  .bz-stat.wait { color: var(--pending); }

  /* ---------- Teams tab ---------- */
  .teams-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.9rem; }
  .gcard {
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 0.5rem; padding: 0.8rem 1rem;
  }
  .gcard.is-live  { border-left: 0.3rem solid var(--live); }
  .gcard.is-final { opacity: 0.8; }
  .gc-team { display: flex; align-items: center; gap: 0.55rem; font-size: 1.05rem; }
  .gc-team .abbrev { flex: 1 1 auto; }
  .gc-team .score { font-weight: 600; }
  .gcard.is-final .gc-team:not(.winner) { color: var(--faint); }
  .gc-status { font-size: 0.78rem; color: var(--dim); margin-top: 0.35rem; }
  .gcard.is-live .gc-status { color: var(--live); }
  .gc-pick { margin-top: 0.5rem; font-size: 0.8rem; font-weight: 600; }
  .gc-pick.won, .gc-pick.leading   { color: var(--ahead); }
  .gc-pick.lost, .gc-pick.trailing { color: var(--behind); }
  .gc-pick.tied { color: var(--close); }
  .gc-pick.pending, .gc-pick.push, .gc-pick.none { color: var(--pending); font-weight: 400; }
  .gc-root { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.45rem; }
  .gc-fan { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.45rem; }
  .fan-chip {
    font-size: 0.72rem; font-weight: 600; white-space: nowrap; color: var(--text);
    border: 1px solid var(--dim); border-radius: 999px; padding: 0.1rem 0.55rem;
  }

  @media (max-width: 70rem) { .teams-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 50rem) { .cols2, .ri-grid, .teams-grid { grid-template-columns: 1fr; } }

  /* ---------- Top Players view ---------- */
  html.focus { font-size: clamp(17px, 2.1vh, 28px); }
  html.focus .wrap { max-width: none; }
  .focus-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem; margin-top: 1.25rem;
  }
  @media (max-width: 50rem) { .focus-grid { grid-template-columns: 1fr; } }
  .focus-card {
    background: var(--surface); border: 1px solid var(--rule);
    border-radius: 0.5rem; padding: 1.1rem 1.25rem;
  }
  .focus-rank { font-size: 0.75rem; color: var(--faint); margin-bottom: 0.5rem; }
  .focus-card .pl { border-bottom: none; padding: 0; }
  .focus-card .logo { width: 3rem; height: 3rem; flex-basis: 3rem; }
  .focus-card .pl-name { font-size: 1.35rem; }
  .focus-card .pl-goal-pct,
  .focus-card .pl-goal-num b,
  .focus-card .pl-fan-pts { font-size: 1.3rem; }
  .focus-card .bar { height: 0.45rem; }
  /* Goal text gets its own line so it never wraps beside the numbers. */
  .focus-card .pl-goal-what { grid-column: 1 / -1; }
  .focus-card .pl-goal-top { grid-template-columns: 1fr auto; row-gap: 0.15rem; }

  @media (max-width: 30rem) {
    header { flex-direction: column; gap: 0.5rem; }
    .meta { text-align: left; }
    .team { grid-template-columns: 2.75rem 2.25rem auto; }
    .matchup { padding: 1rem; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title">Loading</h1>
    <div class="head-right">
      <div class="modes" role="group" aria-label="View">
        <button type="button" data-mode="desk" class="on" aria-pressed="true">Standard</button>
        <button type="button" data-mode="tv" aria-pressed="false">Views</button>
        <button type="button" data-mode="focus" aria-pressed="false">Top Players</button>
      </div>
      <div class="meta">
        <div id="live-count"></div>
        <div id="updated"></div>
      </div>
    </div>
  </header>
  <div id="notices" hidden></div>
  <main id="fantasy"></main>
  <main id="board"></main>
  <main id="tv" hidden></main>
  <main id="focus" hidden></main>
</div>

<script>
const GROUPS = [
  { key: "live", heading: "Live"     },
  { key: "pre",  heading: "Upcoming" },
  { key: "post", heading: "Final"    },
];

// A missing logo hides itself rather than showing a broken image.
// The phone build sets window.STATIC_SITE before this script runs. On the
// local server it is absent and everything behaves as before.
const SITE = window.STATIC_SITE || null;

// A link ending in ?view=bozo shows only the BOZO summary and cards, for
// embedding in another page such as a Google Site. The view buttons, tabs,
// and import notices are hidden; the title and the "As of" line stay.
// Any other value is ignored and the page opens normally.
const EMBED = new URLSearchParams(location.search).get("view") === "bozo";
if (EMBED) document.documentElement.classList.add("embed");

function logoUrl(abbrev) {
  return SITE
    ? SITE.logo.replace("{lower}", encodeURIComponent(abbrev.toLowerCase()))
    : `/static/logos/${encodeURIComponent(abbrev)}.png`;
}

function logoTag(abbrev, small) {
  if (!abbrev) return `<span class="logo${small ? " sm" : ""}"></span>`;
  return `<img class="logo${small ? " sm" : ""}" alt=""
    src="${logoUrl(abbrev)}"
    onerror="this.style.visibility='hidden'">`;
}

/* ---------- phone pins ----------
   The phone version has no server to save pins on, so they live in the
   phone's browser, one set per week. */
function pinStoreKey(week) { return `pins:${week}`; }

function localPins(week) {
  try { return new Set(JSON.parse(localStorage.getItem(pinStoreKey(week)) || "[]")); }
  catch (err) { return new Set(); }
}

function saveLocalPins(week, set) {
  try {
    // Earlier weeks' pins are cleared, matching the laptop's weekly reset.
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && k.startsWith("pins:") && k !== pinStoreKey(week)) localStorage.removeItem(k);
    }
    localStorage.setItem(pinStoreKey(week), JSON.stringify([...set]));
  } catch (err) { /* private browsing can refuse storage; pins just won't stick */ }
}

function applyLocalPins(data) {
  const pv = data.players;
  if (!pv) return;
  const set = localPins(data.week);
  const all = pv.all || pv.players;
  all.forEach(p => { p.pinned = set.has(p.key); });
  pv.players.forEach(p => { p.pinned = set.has(p.key); });
  pv.pinned = all.filter(p => set.has(p.key));
  ((data.rooting && data.rooting.items) || []).forEach(i => {
    if (i.player_key) i.pinned = set.has(i.player_key);
  });
}

function ageText(iso) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return `${hours} hr${hours === 1 ? "" : "s"} ago`;
}

function renderNotices(data) {
  const box = document.getElementById("notices");
  const notes = (data.build && data.build.notes) || [];
  box.hidden = !notes.length;
  if (!notes.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<b>From your last import</b><ul>${
    notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`;
}

// For values placed inside a double-quoted attribute.
function attr(text) {
  return esc(text).replace(/"/g, "&quot;");
}

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ---------- fantasy ---------- */

function playerRow(p, ctx) {
  const game = p.game;
  const injured = p.injury_status &&
                  ["ACTIVE", "NORMAL"].indexOf(p.injury_status) === -1;
  const inj = injured
    ? `<span class="inj">${esc(p.injury_status.slice(0, 3))}</span>` : "";

  let line = "";
  if (game && game.is_live) {
    const left = game.remaining ? ` &middot; ${esc(game.remaining)} left` : "";
    line = `<span class="gm" data-k="${attr("g:" + game.event_id)}">${esc(game.score_line)} &middot; ` +
           `${esc(game.label)}${left}</span>`;
  }

  return `<div class="player" data-k="${attr("mf:" + ctx + "|" + p.name)}">
    <span class="slot">${esc(p.slot)}</span>
    <span class="pname">${esc(p.name)}${inj}</span>
    <span class="pts">${Number(p.points).toFixed(2)}</span>
    ${line}
  </div>`;
}

function playerGroup(label, players, ctx) {
  if (!players.length) return "";
  return `<div class="player-group">
            <div class="group-label">${esc(label)}</div>
            ${players.map(p => playerRow(p, ctx)).join("")}
          </div>`;
}

function matchupCard(m) {
  if (!m.source_ok) {
    return `<div class="matchup">
      <div class="matchup-head"><span class="platform">${esc(m.platform)}</span></div>
      <div class="err">${esc(m.error || "Unavailable")}</div>
    </div>`;
  }

  const me = m.me, them = m.opponent;
  if (!me || !them) return "";

  const margin = me.points - them.points;
  // A tie stays neutral; only a real lead or deficit gets a colour.
  const marginClass = margin > 0 ? "ahead" : (margin < 0 ? "behind" : "");
  const marginText = `${margin >= 0 ? "+" : ""}${margin.toFixed(2)}`;

  const chance = me.win_probability != null
    ? `<span>win ${(me.win_probability * 100).toFixed(0)}%</span>` : "";
  const proj = me.projected != null && them.projected != null
    ? `<span>proj ${me.projected.toFixed(2)} &ndash; ${them.projected.toFixed(2)}</span>`
    : "";
  const source = m.source === "manual"
    ? `<span>lineup from screenshot, scored live</span>` : "";

  const liveMine = me.players.filter(p => p.is_starter && p.game_is_live);
  const liveTheirs = them.players.filter(p => p.is_starter && p.game_is_live);

  const anyLive = liveMine.length || liveTheirs.length;
  const players = anyLive
    ? playerGroup("Your starters in live games", liveMine, `${m.platform}|${me.name}`) +
      playerGroup("Their starters in live games", liveTheirs, `${m.platform}|${them.name}`)
    : `<div class="note">No starters in live games right now.</div>`;

  return `<div class="matchup">
    <div class="matchup-head">
      <span class="platform">${esc(m.platform)}</span>
      <span class="platform ${marginClass}">${marginText}</span>
    </div>

    <div class="side you">
      <span class="side-name">${esc(me.name)}</span>
      <span class="side-score ${marginClass}" data-k="${attr(`t:${m.platform}|${me.name}`)}">${me.points.toFixed(2)}</span>
    </div>
    <div class="side them">
      <span class="side-name">${esc(them.name)}</span>
      <span class="side-score" data-k="${attr(`t:${m.platform}|${them.name}`)}">${them.points.toFixed(2)}</span>
    </div>

    <div class="matchup-meta">
      ${proj}
      ${chance}
      ${source}
      <span>${me.yet_to_play} of yours yet to score</span>
    </div>

    ${players}
  </div>`;
}

/* ---------- pick'em ---------- */

const PICK_MARK = {
  won: "\u2713", leading: "\u2191", lost: "\u2715",
  trailing: "\u2193", tied: "=", push: "=", pending: "\u00b7",
};

function pickRow(p) {
  let state;
  if (p.is_live) {
    state = `${p.my_score}\u2013${p.their_score} &middot; ${esc(p.label)}`;
  } else if (p.status === "pending") {
    state = "not started";
  } else {
    state = `${p.my_score}\u2013${p.their_score} &middot; ${esc(p.label)}`;
  }

  return `<div class="pick ${esc(p.status)}">
    <span class="mark">${PICK_MARK[p.status] || ""}</span>
    <span><span class="team">${esc(p.picked)}</span>
          <span class="vs"> over ${esc(p.opponent)}</span></span>
    <span class="state">${state}</span>
  </div>`;
}

function pickemCard(pe) {
  if (!pe) return "";

  if (!pe.has_picks) {
    return `<div class="matchup">
      <div class="matchup-head">
        <span class="platform">YAHOO PICK'EM</span>
        <span class="platform">Week ${pe.week}</span>
      </div>
      <div class="note">No picks imported for this week yet.</div>
      <div class="matchup-meta"><span>Season ${pe.season_total}</span></div>
    </div>`;
  }

  const order = ["leading", "won", "tied", "trailing", "pending", "lost", "push"];
  const sorted = pe.picks.slice().sort(
    (a, b) => order.indexOf(a.status) - order.indexOf(b.status)
  );

  return `<div class="matchup">
    <div class="matchup-head">
      <span class="platform">YAHOO PICK'EM</span>
      <span class="platform">Week ${pe.week}</span>
    </div>

    <div class="tally">
      <span class="tally-count">${pe.week_count}</span>
      <span class="tally-of">won or leading, of ${pe.week_total} picks</span>
    </div>

    <div class="matchup-meta">
      <span>season ${pe.season_total}</span>
      <span>${pe.week_settled} of ${pe.week_total} settled</span>
      <span>${pe.weeks_tracked} week${pe.weeks_tracked === 1 ? "" : "s"} tracked</span>
    </div>

    <div class="player-group">
      ${sorted.map(pickRow).join("")}
    </div>
  </div>`;
}

/* ---------- merged player list ---------- */

function fantasyLines(rows, nested, pkey) {
  if (!rows.length) return "";
  // Each league's own score, as that platform registers it.
  const body = rows.map(f => {
    const pts = f.platform_points === null || f.platform_points === undefined
      ? "\u2013" : f.platform_points.toFixed(2);
    const inj = f.injury && ["ACTIVE", "NORMAL", ""].indexOf(f.injury) === -1
      ? `<span class="inj">${esc(f.injury.slice(0, 3))}</span>` : "";
    return `<div class="pl-fan-row" data-k="${attr(`pf:${pkey}|${f.platform}|${f.team_name}`)}">
      <span class="pl-fan-plat">${esc(f.platform)}</span>
      <span class="pl-fan-team">${esc(f.team_name)} &middot; ${esc(f.slot)}${inj}</span>
      <span class="pl-fan-pts">${pts}<span class="pl-fan-unit"> pts</span></span>
    </div>`;
  }).join("");
  return `<div class="pl-fan${nested ? " nested" : ""}">${body}</div>`;
}

function goalBlock(g) {
  const current = g.current === null || g.current === undefined
    ? "\u2013" : (Number.isInteger(g.current) ? g.current : g.current.toFixed(1));
  const of = g.target_text ? ` / ${esc(g.target_text)}` : "";

  const pace = g.pace != null ? ` &middot; on pace for ${g.pace}` : "";

  return `<div class="pl-goal goal ${esc(g.status)}" data-k="${attr("r:" + g.id)}">
    <div class="goal-body">
    <div class="pl-goal-top">
      <span class="pl-goal-what">${esc(g.goal_text)}${pace}</span>
      <span class="pl-goal-pct">${g.percent}%</span>
      <span class="pl-goal-num"><b>${current}</b>${of}</span>
    </div>
    <div class="bar"><span style="width:${g.percent}%"></span></div>
    </div>
  </div>`;
}

function playerRowMerged(p) {
  const game = p.game;
  const team = game ? game.abbrev : "";
  const meta = [team, p.position].filter(Boolean).join(" \u00b7 ");

  let when = "not started";
  if (game && game.is_live) {
    const left = game.remaining ? ` &middot; ${esc(game.remaining)} left` : "";
    when = `${esc(game.score_line)} &middot; ${esc(game.label)}${left}`;
  } else if (game && game.state === "post") {
    when = `${esc(game.score_line)} &middot; ${esc(game.label)}`;
  } else if (game) {
    when = `vs ${esc(game.opp_abbrev)} &middot; not started`;
  }

  const goals = p.goals.map(goalBlock).join("");
  const hasGoals = p.goals.length > 0;

  return `<div class="pl${p.is_live ? " islive" : ""}">
    ${logoTag(p.team_abbrev || team)}
    <div class="pl-body">
      <div class="pl-head">
        <span>
          <span class="pl-name">${esc(p.name)}</span>
          <span class="pl-meta">${esc(meta)}</span>
        </span>
        <button type="button" class="pin${p.pinned ? " on" : ""}"
          data-pin="${attr(p.key)}" data-on="${p.pinned ? 0 : 1}"
          aria-pressed="${p.pinned ? "true" : "false"}"
          title="${p.pinned ? "Unpin from Top Players" : "Pin to Top Players"}">${p.pinned ? "Pinned" : "Pin"}</button>
      </div>
      <div class="pl-when"${game ? ` data-k="${attr("g:" + game.event_id)}"` : ""}>${when}</div>
      ${goals}
      ${fantasyLines(p.fantasy, hasGoals, p.key)}
    </div>
  </div>`;
}

function playersCard(pv) {
  if (!pv) return "";

  const finished = pv.finished
    ? `<span>${pv.finished} finished, hidden</span>` : "";

  const head = `<div class="matchup-head">
      <span class="platform">YOUR PLAYERS</span>
      <span class="platform">Week ${pv.week}</span>
    </div>`;

  // Nothing left to show. Say why rather than showing an empty card.
  if (!pv.players.length) {
    const why = pv.finished
      ? `All of your fantasy starters' games have finished. Results are in
         the fantasy cards below.`
      : `No fantasy starters this week.`;
    return `<div class="matchup">
      ${head}
      <div class="note">${why}</div>
    </div>`;
  }

  const bits = [`<span>${pv.total} still to watch</span>`];
  if (pv.live) bits.push(`<span class="inplay">${pv.live} in live games</span>`);
  if (pv.with_goals) bits.push(`<span>${pv.with_goals} with goals</span>`);
  if (finished) bits.push(finished);

  return `<div class="matchup">
    ${head}
    <div class="matchup-meta" style="margin-top:0;border-top:none;padding-top:0">
      ${bits.join("")}
    </div>
    <div class="player-group" style="margin-top:0.5rem">
      ${pv.players.map(playerRowMerged).join("")}
    </div>
  </div>`;
}

/* ---------- rooting ---------- */

function goalRow(g) {
  const current = g.current === null || g.current === undefined
    ? "\u2013" : (Number.isInteger(g.current) ? g.current : g.current.toFixed(1));

  const of = g.target_text ? `<span class="of"> / ${esc(g.target_text)}</span>` : "";

  let when = "";
  if (g.status === "noshow") {
    when = "not in the box score";
  } else if (g.is_live) {
    const left = g.remaining ? ` &middot; ${esc(g.remaining)} left` : "";
    when = `vs ${esc(g.opponent)} &middot; ${esc(g.game_label)}${left}`;
  } else if (g.status === "pending") {
    when = g.opponent ? `vs ${esc(g.opponent)}` : "not started";
  } else {
    when = `vs ${esc(g.opponent)} &middot; ${esc(g.game_label)}`;
  }
  if (g.pace != null) when += ` &middot; on pace for ${g.pace}`;

  return `<div class="goal ${esc(g.status)}" data-k="${attr("r:" + g.id)}">
    ${logoTag(g.team, true)}
    <div class="goal-body">
      <div class="goal-top">
        <span>
          <span class="goal-who">${esc(g.label)}</span>
          <span class="goal-what"> &middot; ${esc(g.goal_text)}</span>
        </span>
        <span class="goal-now">${current}${of}</span>
        ${g.player_key ? `<button type="button" class="pin${g.pinned ? " on" : ""}"
          data-pin="${attr(g.player_key)}" data-on="${g.pinned ? 0 : 1}"
          aria-pressed="${g.pinned ? "true" : "false"}"
          title="${g.pinned ? "Unpin from Top Players" : "Pin to Top Players"}">${g.pinned ? "Pinned" : "Pin"}</button>`
          : "<span></span>"}
      </div>
      <div class="bar"><span style="width:${(g.progress * 100).toFixed(1)}%"></span></div>
      <div class="goal-when"${g.event_id ? ` data-k="${attr("g:" + g.event_id)}"` : ""}>${when}</div>
    </div>
  </div>`;
}

function rootingCard(r) {
  if (!r || !r.items.length) return "";

  const order = ["live", "noshow", "pending", "achieved", "failed"];
  const sorted = r.items.slice().sort(
    (a, b) => order.indexOf(a.status) - order.indexOf(b.status)
  );

  const live = r.tally.live || 0;
  const bits = [`<span>${r.achieved} achieved</span>`];
  if (live) bits.push(`<span class="inplay">${live} in play</span>`);
  if (r.tally.failed) bits.push(`<span>${r.tally.failed} missed</span>`);
  if (r.tally.pending) bits.push(`<span>${r.tally.pending} not started</span>`);

  return `<div class="matchup">
    <div class="matchup-head">
      <span class="platform">ROOTING FOR</span>
      <span class="platform">Week ${r.week}</span>
    </div>

    <div class="tally">
      <span class="tally-count">${r.achieved}</span>
      <span class="tally-of">of ${r.total} goals hit</span>
    </div>

    <div class="matchup-meta">${bits.join("")}</div>

    <div class="player-group">${sorted.map(goalRow).join("")}</div>
  </div>`;
}

/* ---------- games ---------- */

function teamRow(abbrev, score, hasBall, isWinner, state) {
  const dot = hasBall ? '<span class="ball"></span>' : "";
  const win = isWinner ? " winner" : "";
  const shown = state === "pre" ? "&mdash;" : score;
  return `<div class="team${win}">
            <span class="abbrev">${esc(abbrev)}</span>
            <span class="score">${shown}</span>
            <span>${dot}</span>
          </div>`;
}

function gameRow(g) {
  const cls = g.is_live ? "live" : g.state;
  const awayWins = g.state === "post" && g.away_score > g.home_score;
  const homeWins = g.state === "post" && g.home_score > g.away_score;

  let status;
  if (g.is_live) {
    status = `<span class="clock">${esc(g.label)}</span>
              <span class="sub">${esc(g.remaining)} left</span>`;
  } else if (g.state === "pre") {
    status = `<span>${esc(g.kickoff_local)}</span>`;
  } else {
    status = `<span>${esc(g.label)}</span>`;
  }

  return `<div class="game ${cls}" data-k="${attr("g:" + g.event_id)}">
    <div class="teams">
      ${teamRow(g.away, g.away_score, g.possession === g.away, awayWins, g.state)}
      ${teamRow(g.home, g.home_score, g.possession === g.home, homeWins, g.state)}
    </div>
    <div class="status">${status}</div>
  </div>`;
}

/* ---------- shared pieces for the TV tabs ---------- */

const TRACK_TEXT = {
  on: "On track", off: "Off track", open: "No verdict yet",
  pending: "Not started", final_hit: "Final \u00b7 hit", final_miss: "Final \u00b7 missed",
};

function trackPill(track) {
  const t = track || "open";
  return `<span class="trk ${esc(t)}"><span class="dot"></span>${esc(TRACK_TEXT[t] || t)}</span>`;
}

function exploreTag(item) {
  return item.explore
    ? `<span class="explore" title="The on-track rule for this kind of goal is provisional">rule provisional</span>`
    : "";
}

// Projected margin decides the color. Within 20 either way is too close to call.
function projectionVerdict(m) {
  const me = m.me, them = m.opponent;
  if (!m.source_ok || !me || !them) return { cls: "none", text: "Unavailable" };
  if (me.projected == null || them.projected == null) {
    return { cls: "none", text: "Projection pending" };
  }
  const gap = me.projected - them.projected;
  const shown = Math.abs(gap).toFixed(1);
  if (Math.abs(gap) <= 20) {
    return { cls: "close", text: `Too close to call \u00b7 projected ${gap >= 0 ? "+" : "\u2212"}${shown}` };
  }
  return gap > 0
    ? { cls: "win", text: `Projected win by ${shown}` }
    : { cls: "loss", text: `Projected loss by ${shown}` };
}

function fxCard(m) {
  if (!m.source_ok) {
    return `<div class="fx none">
      <div class="matchup-head"><span class="platform">${esc(m.platform)}</span></div>
      <div class="err">${esc(m.error || "Unavailable")}</div>
    </div>`;
  }
  const me = m.me, them = m.opponent;
  if (!me || !them) return "";
  const v = projectionVerdict(m);
  const proj = me.projected != null && them.projected != null
    ? `<div class="fx-proj">projected ${me.projected.toFixed(1)} &ndash; ${them.projected.toFixed(1)}</div>`
    : "";
  const margin = me.points - them.points;
  const lead = margin > 0 ? "ahead" : (margin < 0 ? "behind" : "");

  return `<div class="fx ${v.cls}">
    <div class="matchup-head"><span class="platform">${esc(m.platform)}</span></div>
    <div class="side you">
      <span class="side-name">${esc(me.name)}</span>
      <span class="side-score ${lead}" data-k="${attr(`t:${m.platform}|${me.name}`)}">${me.points.toFixed(2)}</span>
    </div>
    <div class="side them">
      <span class="side-name">${esc(them.name)}</span>
      <span class="side-score" data-k="${attr(`t:${m.platform}|${them.name}`)}">${them.points.toFixed(2)}</span>
    </div>
    ${proj}
    <div class="fx-verdict">${esc(v.text)}</div>
  </div>`;
}

function nextKickoff(data) {
  const next = (data.games || []).find(g => g.state === "pre");
  return next ? next.kickoff_local : "";
}

function playersList(players, emptyText) {
  if (!players.length) return `<div class="note">${emptyText}</div>`;
  return `<div class="matchup"><div class="player-group" style="margin-top:0">
    ${players.map(playerRowMerged).join("")}</div></div>`;
}

/* ---------- TV tabs ---------- */

const TABS = [
  ["home", "Home"], ["players", "My Players"], ["fantasy", "Fantasy"],
  ["rooting", "Rooting Interests"], ["bozo", "BOZO"], ["teams", "Teams"],
];

function tabBar() {
  return `<nav class="tabs" aria-label="Views sections">${TABS.map(([key, label]) =>
    `<button type="button" data-tab="${key}" class="${TAB === key ? "on" : ""}"
       aria-pressed="${TAB === key}">${label}</button>`).join("")}</nav>`;
}

function tabHome(data) {
  const everyone = (data.players && data.players.players) || [];
  const live = everyone.filter(p => p.is_live);
  let empty;
  if (data.live_count) {
    empty = "None of your players are in a live game right now.";
  } else {
    const kickoff = nextKickoff(data);
    empty = `No live games${kickoff ? ` &middot; next kickoff ${esc(kickoff)}` : ""}`;
  }
  const matchups = data.matchups || [];
  const right =
    (matchups.length ? `<h2>Fantasy</h2><div class="fx-grid" style="grid-template-columns:1fr">
        ${matchups.map(fxCard).join("")}</div>` : "") +
    (data.pickem ? `<h2>Pick'em</h2>` + pickemCard(data.pickem) : "");
  return `<div class="cols2">
    <section><h2>Your players &middot; live</h2>${playersList(live, empty)}</section>
    <section>${right}</section>
  </div>`;
}

function tabPlayers(data) {
  const everyone = (data.players && data.players.players) || [];
  const live = everyone.filter(p => p.game_state === "in");
  const later = everyone.filter(p => p.game_state !== "in");
  return `<div class="cols2">
    <section><h2>Live</h2>${playersList(live, "Nobody live right now.")}</section>
    <section><h2>Not started</h2>${playersList(later, "Nobody left to play.")}</section>
  </div>`;
}

function tabFantasy(data) {
  const matchups = data.matchups || [];
  if (!matchups.length) return `<div class="note">No fantasy matchups this week.</div>`;
  return `<div class="fx-grid">${matchups.map(fxCard).join("")}</div>`;
}

function rootingRow(g) {
  const current = g.current === null || g.current === undefined
    ? "\u2013" : (Number.isInteger(g.current) ? g.current : g.current.toFixed(1));
  const of = g.target_text ? `<span class="of"> / ${esc(g.target_text)}</span>` : "";
  const pct = Math.round((g.progress || 0) * 100);

  let when = "";
  if (g.is_live) {
    const left = g.remaining ? ` &middot; ${esc(g.remaining)} left` : "";
    when = `vs ${esc(g.opponent)} &middot; ${esc(g.game_label)}${left}`;
  } else if (g.opponent) {
    const label = g.game_label && g.status !== "pending" ? ` &middot; ${esc(g.game_label)}` : "";
    when = `vs ${esc(g.opponent)}${label}`;
  }
  const pace = g.pace != null ? ` &middot; on pace for ${g.pace}` : "";

  return `<div class="ri ${esc(g.track)}" data-k="${attr("r:" + g.id)}">
    ${logoTag(g.team)}
    <div class="ri-body">
      <div class="ri-top">
        <span>
          <span class="ri-who">${esc(g.label)}</span>
          <span class="ri-target"> &middot; ${esc(g.goal_text)}</span>
        </span>
        <span class="ri-now">${current}${of}</span>
      </div>
      <div class="ri-bar">
        <div class="bar"><span style="width:${pct}%"></span></div>
        <span class="ri-pct">${pct}%</span>
      </div>
      <div class="ri-foot">
        ${trackPill(g.track)}
        ${exploreTag(g)}
        <span${g.event_id ? ` data-k="${attr("g:" + g.event_id)}"` : ""}>${when}${pace}</span>
      </div>
    </div>
  </div>`;
}

const TRACK_ORDER = ["on", "off", "open", "pending", "final_hit", "final_miss"];

// Goals still in play lead; anything already settled sinks to the bottom.
//   0  still in play: on track, off track, no verdict yet
//   1  not started
//   2  decided while the game goes on: an over already hit, an under
//      already blown. Nothing about these can change.
//   3  finished
function rootingGroup(g) {
  if (g.track === "final_hit" || g.track === "final_miss") return 3;
  if (g.status === "achieved" || g.status === "failed") return 2;
  if (g.track === "pending") return 1;
  return 0;
}

function sortRooting(items) {
  return items.slice().sort((a, b) =>
    rootingGroup(a) - rootingGroup(b)
    || TRACK_ORDER.indexOf(a.track) - TRACK_ORDER.indexOf(b.track));
}

function tabRooting(data) {
  const items = (data.rooting && data.rooting.items) || [];
  if (!items.length) return `<div class="note">No rooting interests imported for this week.</div>`;
  return `<div class="ri-grid">${sortRooting(items).map(rootingRow).join("")}</div>`;
}

// Counts for the BOZO summary. Finished = settled, won or lost, including a
// goal decided while its game is still going. In play = still undecided in
// a live game. Remaining = game not started. The three always add up.
function bozoCounts(items) {
  const closed = items.filter(i =>
    i.status === "achieved" || i.status === "failed"
    || i.track === "final_hit" || i.track === "final_miss").length;
  const remaining = items.filter(i => i.track === "pending").length;
  return {
    total: items.length,
    hit: items.filter(i => i.status === "achieved").length,
    closed,
    remaining,
    inPlay: items.length - closed - remaining,
  };
}

function tabBozo(data) {
  const items = ((data.rooting && data.rooting.items) || []).filter(i => i.bozo);
  if (!items.length) {
    return `<div class="note">No rooting interests are marked BOZO this week.</div>`;
  }
  const c = bozoCounts(items);
  return `<div class="bozo-summary">
      <div class="bozo-hit"><b>${c.hit}</b> of ${c.total} hit</div>
      <div class="bozo-states">${c.inPlay} in play, ${c.remaining} remaining, ${c.closed} finished</div>
    </div>
    <div class="bz-grid">${sortRooting(items).map(g => bozoCard(g, data)).join("")}</div>`;
}

// The live number beside each BOZO goal. Moneylines and spreads show the
// team's current margin; everything else shows the stat itself.
function bozoStat(g, data) {
  if (g.track === "pending") return "\u2013";
  if (g.stat === "win" || g.stat === "win_by") {
    const game = (data.games || []).find(x => x.event_id === g.event_id);
    if (!game || game.state === "pre") return "\u2013";
    const home = game.home === g.team;
    const margin = (home ? game.home_score : game.away_score)
                 - (home ? game.away_score : game.home_score);
    return margin > 0 ? `+${margin}` : String(margin);
  }
  if (g.current === null || g.current === undefined) return "\u2013";
  return Number.isInteger(g.current) ? String(g.current) : g.current.toFixed(1);
}

const BOZO_STAT_CLASS = {
  on: "good", final_hit: "good", off: "bad", final_miss: "bad", pending: "wait",
};

// The game line, worded as on Top Players.
function bozoWhen(g, data) {
  const game = (data.games || []).find(x => x.event_id === g.event_id);
  if (!game) return { text: "no game this week", live: false };
  const home = game.home === g.team;
  const opp = home ? game.away : game.home;
  const score = `${esc(g.team)} ${home ? game.home_score : game.away_score}, ` +
                `${esc(opp)} ${home ? game.away_score : game.home_score}`;
  if (game.is_live) {
    const left = game.remaining ? ` &middot; ${esc(game.remaining)} left` : "";
    return { text: `${score} &middot; ${esc(game.label)}${left}`, live: true };
  }
  if (game.state === "post") return { text: `${score} &middot; ${esc(game.label)}`, live: false };
  return { text: `vs ${esc(opp)} &middot; not started`, live: false };
}

// Top Players layout without fantasy lines: logo and name; team and
// position; the game line; then your wording with the live number.
function bozoCard(g, data) {
  const name = g.kind === "team" ? (g.team_name || g.team) : g.label;
  const meta = g.kind === "player" && g.position
    ? `${esc(g.team)} &middot; ${esc(g.position)}` : esc(g.team);
  const when = bozoWhen(g, data);
  return `<div class="bz-card" data-k="${attr("r:" + g.id)}">
    ${logoTag(g.team)}
    <div class="bz-body">
      <div class="bz-name">${esc(name)}</div>
      <div class="bz-meta">${meta}</div>
      <div class="bz-when${when.live ? " is-live" : ""}"${g.event_id ? ` data-k="${attr("g:" + g.event_id)}"` : ""}>${when.text}</div>
      <div class="bz-goal">
        <span class="bz-detail">${esc(g.goal_text)}</span>
        <span class="bz-stat ${BOZO_STAT_CLASS[g.track] || ""}">${bozoStat(g, data)}</span>
      </div>
    </div>
  </div>`;
}

function gameCard(g, pick, interests, fantasy) {
  const final = g.state === "post";
  const cls = g.is_live ? "is-live" : (final ? "is-final" : "");
  const awayWins = final && g.away_score > g.home_score;
  const homeWins = final && g.home_score > g.away_score;
  const team = (abbrev, score, win, ball) => `
    <div class="gc-team${win ? " winner" : ""}">
      ${logoTag(abbrev, true)}
      <span class="abbrev">${esc(abbrev)}${ball ? ' <span class="ball"></span>' : ""}</span>
      <span class="score">${g.state === "pre" ? "&mdash;" : score}</span>
    </div>`;

  let status;
  if (g.is_live) status = `${esc(g.label)} &middot; ${esc(g.remaining)} left`;
  else if (g.state === "pre") status = esc(g.kickoff_local);
  else status = esc(g.label);

  const pickLine = pick
    ? `<div class="gc-pick ${esc(pick.status)}">Pick: ${esc(pick.picked)} &middot; ${esc(pick.status)}</div>`
    : `<div class="gc-pick none">No pick</div>`;

  // One indicator per rooting interest in this game.
  const chips = interests.length
    ? `<div class="gc-root">${interests.map(i => {
        // Drop only the first name, so "Amon-Ra St. Brown" reads "St. Brown".
        const parts = i.label.split(" ");
        const who = i.kind === "team" || parts.length < 2 ? i.label : parts.slice(1).join(" ");
        const tip = `${i.label} \u00b7 ${i.goal_text} \u00b7 ${TRACK_TEXT[i.track] || ""}`;
        return `<span class="trk ${esc(i.track)}" data-k="${attr("r:" + i.id)}" title="${attr(tip)}">` +
               `<span class="dot"></span>${esc(who)} &middot; ${esc(i.goal_text)}</span>`;
      }).join("")}</div>`
    : "";

  return `<div class="gcard ${cls}" data-k="${attr("g:" + g.event_id)}">
    ${team(g.away, g.away_score, awayWins, g.possession === g.away)}
    ${team(g.home, g.home_score, homeWins, g.possession === g.home)}
    <div class="gc-status">${status}</div>
    ${pickLine}
    ${chips}
    ${fantasyChips(fantasy || [])}
  </div>`;
}

// Your fantasy starters playing in a live game, by name only. Outlined, so
// they read apart from the colored rooting chips. One chip per player, even
// when he starts for you in more than one league.
function fantasyChips(players) {
  if (!players.length) return "";
  return `<div class="gc-fan">${players.map(p => {
    const parts = p.name.split(" ");
    const who = parts.length < 2 ? p.name : parts.slice(1).join(" ");
    return `<span class="fan-chip" title="${attr(p.name + " \u00b7 fantasy starter")}">${esc(who)}</span>`;
  }).join("")}</div>`;
}

// Live games first, then games carrying rooting interests, then the rest.
// Within each group, upcoming by kickoff with finished games last.
function tabTeams(data) {
  const games = data.games || [];
  if (!games.length) return `<div class="note">No games stored yet.</div>`;
  const picks = {};
  ((data.pickem && data.pickem.picks) || []).forEach(p => { picks[p.event_id] = p; });
  const byGame = {};
  ((data.rooting && data.rooting.items) || []).forEach(i => {
    if (!i.event_id) return;
    (byGame[i.event_id] = byGame[i.event_id] || []).push(i);
  });
  // Your fantasy starters in games being played right now.
  const fantasyByGame = {};
  const everyone = (data.players && (data.players.all || data.players.players)) || [];
  everyone
    .filter(p => p.fantasy && p.fantasy.length && p.game && p.game.is_live && p.game.event_id)
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach(p => { (fantasyByGame[p.game.event_id] = fantasyByGame[p.game.event_id] || []).push(p); });
  const group = g => g.is_live ? 0 : ((byGame[g.event_id] || []).length ? 1 : 2);
  const ordered = games
    .map((g, index) => ({ g, index }))
    .sort((a, b) =>
      group(a.g) - group(b.g)
      || (a.g.state === "post") - (b.g.state === "post")
      || a.index - b.index)
    .map(x => x.g);
  return `<div class="teams-grid">${ordered.map(g =>
    gameCard(g, picks[g.event_id], byGame[g.event_id] || [], fantasyByGame[g.event_id])).join("")}</div>`;
}

function renderTV(data) {
  const body = {
    home: tabHome, players: tabPlayers, fantasy: tabFantasy,
    rooting: tabRooting, bozo: tabBozo, teams: tabTeams,
  }[TAB] || tabHome;
  return tabBar() + body(data);
}

/* ---------- Top Players ---------- */

// Pinned players first, as many as you have pinned. If that's fewer
// than four, the rest fill from your most invested players, in the same
// order as Your players.
const TOP_FILL = 4;

function renderFocus(data) {
  const pv = data.players || {};
  const pinned = pv.pinned || [];
  const pinnedKeys = new Set(pinned.map(p => p.key));
  const fill = (pv.players || [])
    .filter(p => !pinnedKeys.has(p.key))
    .slice(0, Math.max(0, TOP_FILL - pinned.length));
  const shown = pinned.concat(fill);

  if (!shown.length) {
    return `<div class="note">No players to show yet. They appear once this
      week's rosters and rooting goals are in.</div>`;
  }
  return `<div class="focus-grid">${shown.map(p => `
    <div class="focus-card">
      <div class="focus-rank">${p.pinned ? "Pinned" : "Most invested"} &middot;
        ${p.interests} interest${p.interests === 1 ? "" : "s"}</div>
      ${playerRowMerged(p)}
    </div>`).join("")}</div>`;
}

/* ---------- standard view ---------- */

// Rooting-only players, with no fantasy stake, live in the Rooting for
// column in this view, so Your players narrows to players on a roster.
function fantasyOnly(pv) {
  if (!pv) return pv;
  const list = pv.players.filter(p => p.fantasy.length);
  return Object.assign({}, pv, {
    players: list,
    total: list.length,
    live: list.filter(p => p.is_live).length,
    with_goals: list.filter(p => p.goals.length).length,
    finished: pv.finished_fantasy != null ? pv.finished_fantasy : pv.finished,
  });
}

function renderDesk(data) {
  const matchups = data.matchups || [];
  const left = data.players ? `<h2>Your players</h2>` + playersCard(fantasyOnly(data.players)) : "";
  const right = data.rooting ? `<h2>Rooting for</h2>` + rootingCard(data.rooting) : "";

  let html = "";
  // Side by side when both exist; one alone takes the full width.
  if (left && right) html += `<div class="pair"><div>${left}</div><div>${right}</div></div>`;
  else html += left + right;
  if (matchups.length) {
    html += `<h2>Fantasy</h2>` + matchups.map(matchupCard).join("");
  }
  if (data.pickem) {
    html += `<h2>Pick'em</h2>` + pickemCard(data.pickem);
  }
  document.getElementById("fantasy").innerHTML = html;

  const board = document.getElementById("board");
  if (!data.games.length) {
    board.innerHTML =
      '<p class="empty">No games stored yet. The poller runs every ' +
      'few seconds and this fills in on its own.</p>';
    return;
  }
  board.innerHTML = GROUPS.map(group => {
    const rows = data.games.filter(g =>
      group.key === "live" ? g.is_live : (!g.is_live && g.state === group.key)
    );
    if (!rows.length) return "";
    return `<h2>${group.heading}</h2>` + rows.map(gameRow).join("");
  }).join("");
}

/* ---------- change flash ----------
   The page sees new data every refresh, not individual plays, so a
   change is the difference between two refreshes.
     game scores      any change                      amber
     fantasy points   a move of 2 or more             green up, soft red down
     rooting goals    a move of at least 7% of the    green if it helps,
                      target (100 yards: 7 or more)   soft red if it hurts
   Opponent numbers pulse amber: their gain is not yours.
*/
const FANTASY_THRESHOLD = 2;
const GOAL_SHARE = 0.07;
const EPSILON = 1e-6;

function snapshot(data) {
  const s = {};
  (data.games || []).forEach(g => {
    s["g:" + g.event_id] = { raw: `${g.away_score}-${g.home_score}` };
  });
  const pv = data.players;
  if (pv) pv.players.forEach(p => p.fantasy.forEach(f => {
    s[`pf:${p.key}|${f.platform}|${f.team_name}`] = { v: f.platform_points };
  }));
  ((data.rooting && data.rooting.items) || []).forEach(i => {
    s["r:" + i.id] = { v: i.current, target: i.target, dir: i.direction };
  });
  (data.matchups || []).forEach(m => {
    if (!m.source_ok) return;
    ["me", "opponent"].forEach(side => {
      const t = m[side];
      if (!t) return;
      s[`t:${m.platform}|${t.name}`] = { v: t.points, side };
      (t.players || []).forEach(p => {
        s[`mf:${m.platform}|${t.name}|${p.name}`] = { v: p.points, side };
      });
    });
  });
  return s;
}

function diff(prev, next) {
  const out = [];
  if (!prev) return out;
  for (const [key, now] of Object.entries(next)) {
    const before = prev[key];
    if (!before) continue;

    if (key.startsWith("g:")) {
      if (before.raw !== now.raw) out.push([key, "flash-score"]);
      continue;
    }
    if (now.v == null || before.v == null) continue;
    const delta = now.v - before.v;
    if (Math.abs(delta) < EPSILON) continue;

    if (key.startsWith("r:")) {
      const needed = GOAL_SHARE * Math.abs(now.target || 0);
      if (Math.abs(delta) + EPSILON < needed) continue;
      const helps = now.dir === "under" ? delta < 0 : delta > 0;
      out.push([key, helps ? "flash-gain" : "flash-loss"]);
      continue;
    }

    if (Math.abs(delta) + EPSILON < FANTASY_THRESHOLD) continue;
    if (now.side === "opponent") {
      out.push([key, "flash-score"]);
      continue;
    }
    out.push([key, delta > 0 ? "flash-gain" : "flash-loss"]);
  }
  return out;
}

function applyFlashes(changes) {
  changes.forEach(([key, cls]) => {
    document.querySelectorAll(`[data-k="${CSS.escape(key)}"]`).forEach(el => {
      el.classList.remove("flash-gain", "flash-loss", "flash-score");
      void el.offsetWidth;   // restart the animation if it is mid-play
      el.classList.add(cls);
    });
  });
}

/* ---------- render loop ---------- */

// Not remembered: a reload always opens in the standard view. The
// automatic updates below never change the view.
let MODE = "desk";
let TAB = "home";
let LAST = null;
let PREV = null;

function renderHeader(data) {
  document.getElementById("title").textContent =
    data.week ? `Week ${data.week}` : "No games yet";
  document.getElementById("updated").textContent =
    SITE && data.build
      ? `As of ${data.updated} \u00b7 ${ageText(data.build.at)}`
      : `Updated ${data.updated}`;
  document.getElementById("live-count").innerHTML =
    data.live_count
      ? `<span class="live-count">${data.live_count} live</span>`
      : `${data.games.length} games`;
}

function render(data) {
  renderHeader(data);
  if (EMBED) {
    for (const id of ["fantasy", "board", "focus", "notices"]) {
      document.getElementById(id).hidden = true;
    }
    const box = document.getElementById("tv");
    box.hidden = false;
    box.innerHTML = tabBozo(data);
    return;
  }
  renderNotices(data);
  const shown = {
    fantasy: MODE === "desk", board: MODE === "desk",
    tv: MODE === "tv", focus: MODE === "focus",
  };
  for (const [id, on] of Object.entries(shown)) {
    document.getElementById(id).hidden = !on;
  }
  if (MODE === "tv") {
    document.getElementById("tv").innerHTML = renderTV(data);
  } else if (MODE === "focus") {
    document.getElementById("focus").innerHTML = renderFocus(data);
  } else {
    renderDesk(data);
  }
}

async function refresh() {
  let data;
  try {
    // The phone file changes every few minutes; the minute stamp lets the
    // browser reuse it within a minute but never longer.
    const url = SITE
      ? `${SITE.dataUrl}?t=${Math.floor(Date.now() / 60000)}`
      : "/api/dashboard";
    const response = await fetch(url);
    data = await response.json();
  } catch (err) {
    document.getElementById("updated").textContent = "Reconnecting";
    return;
  }
  if (SITE) applyLocalPins(data);
  const next = snapshot(data);
  const changes = diff(PREV, next);
  PREV = next;
  LAST = data;
  render(data);
  applyFlashes(changes);
}

function setMode(mode) {
  MODE = mode;
  document.documentElement.classList.remove("tv", "focus");
  if (mode !== "desk") document.documentElement.classList.add(mode);
  document.querySelectorAll(".modes button").forEach(button => {
    const on = button.dataset.mode === mode;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
  if (LAST) render(LAST);
  window.scrollTo(0, 0);
}

document.querySelectorAll(".modes button").forEach(button =>
  button.addEventListener("click", () => setMode(button.dataset.mode)));

// Pin buttons appear wherever a player row does, and every refresh
// redraws them, so one listener on the page handles them all.
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-pin]");
  if (!button) return;
  if (SITE) {
    if (!LAST) return;
    const set = localPins(LAST.week);
    if (button.dataset.on === "1") set.add(button.dataset.pin);
    else set.delete(button.dataset.pin);
    saveLocalPins(LAST.week, set);
    applyLocalPins(LAST);
    render(LAST);
    return;
  }
  button.disabled = true;
  try {
    const url = `/api/pin?key=${encodeURIComponent(button.dataset.pin)}&on=${button.dataset.on}`;
    await fetch(url, { method: "POST" });
  } catch (err) {
    // The next refresh shows whatever the server actually holds.
  }
  refresh();
});

function setTab(tab) {
  TAB = tab;
  if (LAST) render(LAST);
  window.scrollTo(0, 0);
}

// The tab bar is redrawn on every refresh, so listen on its container.
document.getElementById("tv").addEventListener("click", event => {
  const button = event.target.closest("[data-tab]");
  if (button) setTab(button.dataset.tab);
});

refresh();
// The phone file only changes every few minutes, so checking once a
// minute is plenty there.
setInterval(refresh, SITE ? 60000 : 15000);
</script>
</body>
</html>
"""
