# Football Dashboard

A local web dashboard that shows every weekly football interest in one place:
fantasy matchups, Pick'em standing, and live NFL game state.

Runs entirely on your own machine. No hosting, no cloud account, no cost.
Credentials never leave the laptop.

---

## Current status

| Piece | State |
|---|---|
| NFL scoreboard poller | Working. Verified against completed games. |
| ESPN Fantasy (1 team) | Working. Live scores, projections, per-player detail. |
| Yahoo Pick'em | Working. Manual weekly import, live grading. |
| Merged player view | Working. Every active player across all sources, one row each. |
| Rooting interests | Working. Any player, live box score stats, progress bars. |
| Yahoo Fantasy, public league | Working, no credentials. |
| Yahoo Fantasy, private league | Working from a weekly lineup screenshot, scored live. |
| DraftKings sportsbook | **Removed from scope.** See *Decisions*. |

Live in-game clock behavior has not yet been observed during an active game.
That is the one remaining validation.

---

## How it works

Three layers, each depending only on the one below it.

```
  Dashboard page  (auto-refreshes every 15s)
        |
  API  /api/dashboard
        |
  +-----+--------+-----------+
  |              |           |
fantasy.py    picks.py    load_games()
  |              |           |
  +--------------+-----------+
                 |
            game table  <-- poller.py <-- ESPN scoreboard
                 |
            team table  (32 rows, abbreviation crosswalk)
```

**The key design choice:** nothing is graded remotely. Platforms are read once
for *positions* (your roster, your picks), and everything after that is
computed locally against the scoreboard. That means Pick'em counts update live
without re-scraping, and the only network calls during games are the scoreboard
and the fantasy APIs.

### Why the team table exists

ESPN's scoreboard API, ESPN Fantasy, and Yahoo disagree on team abbreviations
(WSH vs WAS being the live example). Every join resolves through
`team.abbrev_canonical`. ESPN Fantasy happens to use the same numeric team IDs
as ESPN's scoreboard API, so fantasy players join to live games directly with no
player-level crosswalk needed.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Web server, dashboard page, background scheduler |
| `poller.py` | Fetches ESPN scoreboard, normalizes, writes to database |
| `clock.py` | Game clock math and its edge cases |
| `db.py` | Schema and the 32-team seed data |
| `fantasy.py` | Fetches matchups, joins players to live game state, caches |
| `espn_client.py` | ESPN Fantasy API client |
| `picks.py` | Pick'em storage and local grading |
| `import_picks.py` | CLI to load a week of picks from JSON |
| `players.py` | Merges fantasy starters and rooting goals into one list |
| `build_site.py` | Builds the phone version on GitHub |
| `pins.py` | Players pinned to Top Players, one set per week |
| `fetch_logos.py` | One-time download of the 32 team logos |
| `rooting.py` | Rooting interests: parsing, storage, grading |
| `boxscore.py` | ESPN box score reader and stat catalog |
| `roster.py` | Team roster lookup for player name resolution |
| `import_rooting.py` | CLI to load a week of rooting interests |
| `yahoo_auth.py` | Yahoo OAuth setup and token refresh |
| `yahoo_public.py` | Yahoo public league, no credentials (the working route) |
| `manual.py` | Private Yahoo league: lineup storage and live scoring |
| `yahoo_scoring.py` | That league's scoring rules, derived from a real matchup |
| `projection.py` | That league's projected finals, from ESPN's projected stat lines |
| `import_manual.py` | CLI to load a week's lineups for the private league |
| `validate_scoring.py` | Checks those rules against real box scores |
| `yahoo_client.py` | Yahoo authenticated client (built, blocked on 403) |
| `yahoo_diagnose.py` | Tests Yahoo endpoints to check whether the 403 cleared |
| `test_clock.py` | 34 clock test cases |

Generated at runtime and excluded from version control: `.env`,
`yahoo_token.json`, `football.db`, `debug/`.

---

## Setup

### Requirements

- Python 3.11 or newer (3.13 in use)
- A Yahoo account owning the fantasy teams and Pick'em pool
- An ESPN account in the fantasy league

### Install

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Credentials

Create a `.env` file in the project folder:

```
ESPN_LEAGUE_ID=1151985
ESPN_TEAM_ID=12
ESPN_SEASON_ID=2026
ESPN_SWID="{YOUR-SWID-WITH-BRACES}"
ESPN_S2="your_long_espn_s2_cookie"

YAHOO_CLIENT_ID=your_client_id
YAHOO_CLIENT_SECRET=your_client_secret
YAHOO_REDIRECT_URI=https://localhost:8000/callback
YAHOO_LEAGUE_1_ID=752951
YAHOO_TEAM_1_ID=3
YAHOO_LEAGUE_2_ID=1335544
YAHOO_TEAM_2_ID=1
YAHOO_PICKEM_GROUP_ID=your_group_id
```

**Getting the ESPN cookies:** open `fantasy.espn.com` logged in, press F12,
Application tab, Cookies, `https://fantasy.espn.com`. Copy `SWID` (keep the
braces) and `espn_s2` (long, URL-encoded, ~300 chars). These rotate whenever you
log out of ESPN, so expect to re-copy them occasionally.

**Getting the league and team IDs:** they are in the URL of your team page.

Quote only the two ESPN cookie values. Everything else goes unquoted. Type the
quotes in VS Code rather than pasting them from elsewhere, or you may get curly
quotes, which break the HTTP layer with an ASCII encoding error.

### Team logos

Run once, after the poller has stored a week of games:

```
python fetch_logos.py
```

That saves 32 PNGs into `static/logos/`, which the dashboard serves itself.
Keeping them local means the page makes no external browser requests at
runtime: nothing for a corporate proxy to block, and no silent breakage if
ESPN changes its CDN paths.

A missing logo is not fatal. The image hides itself and the row renders
normally, so a rebrand or a failed download degrades quietly.

### Yahoo authorization

```
python yahoo_auth.py
```

Opens a URL, you approve, the browser fails to load a page (expected), you copy
the whole address bar back into the terminal. Writes `yahoo_token.json`.
Refreshes automatically after that.

---

## Running

```
uvicorn app:app --port 8000
```

Then open `http://localhost:8000`.

Leave that terminal running while using the dashboard. Ctrl+C stops it. Run
other commands in a second terminal so the server keeps going.

Polling adapts on its own: every 25 seconds while any game is live, every 10
minutes when games are scheduled today, hourly otherwise. Fantasy data is
cached 45 seconds during live games and 10 minutes otherwise.

---

## Weekly workflow

Once per week, after picks lock:

1. Screenshot your Yahoo Pick'em picks page.
2. Create `picks_week_N.json` in the project folder:

```json
{
  "season": 2026,
  "week": 2,
  "picks": ["DET", "NE", "KC", "PHI", "BAL"]
}
```

3. Import it:

```
python import_picks.py picks_week_2.json
```

Re-importing the same week replaces it, which is how you fix a mistake. Any
platform's abbreviations work, so WAS and WSH both resolve. The importer reports
any team it could not match or could not find a game for.

Everything after that is automatic. Counts update live off the scoreboard.

### The player view

The top section lists every active player once, gathering each reason you care
about them. A player who is both a fantasy starter and a rooting interest gets
one row: the goal and its progress bar lead, and the fantasy lines nest
underneath.

**Merging keys on normalized name plus pro team.** Athlete id does not work as
the key: ESPN supplies one and Yahoo does not, so the same person arrives under
two keys and never merges. Name alone could collide two different players; name
and team together cannot, because two people with the same normalized name on
the same NFL team is not a real situation. The ESPN athlete id is then an
attribute, filled by whichever source has it, and used for box score lookups.

A player on both an ESPN and a Yahoo roster gets one row with both fantasy
lines nested under it.

Fantasy points are **each league's own number**, exactly as that platform
registers it. Two leagues with different scoring settings will show different
totals for the same player, which is correct: those are the points that decide
each matchup.

Only starters appear. Bench, IR, and your opponent's players are excluded. The
Fantasy card still shows both sides of each matchup.

**Order:** game state (live, then not started), then total interests, then
rooting goals, then fantasy teams, then fantasy points, then name. Every fantasy
team and every rooting goal counts as its own interest, so a player on two
rosters with two rooting goals counts as four. With interests tied, more rooting
goals wins, which is what puts a player you're actively rooting for ahead of one
who's simply on two rosters.

**A player whose game has ended drops off this list**, with a count of how many
were hidden. There is nothing left to watch there. Their results stay in the
Rooting and Fantasy cards, which are the record of how each goal finished.

Section order on the page: Your players, Rooting for, Fantasy, Pick'em, then
the scoreboard.

### Views

Three buttons in the header: Standard, Views, and Top Players. Views is the
TV layout; the button was renamed from TV. Neither the view nor the Views tab
is remembered: reloading opens Standard on the Home tab, though
the automatic 15-second updates never switch anything mid-game. The header
stays visible in every view and on every tab, so switching is always one click.

- **Standard.** Your players and Rooting for side by side, stacking into one
  column on a narrow window, with Fantasy, Pick'em, and the scoreboard below.
  Here, Your players lists only players on a fantasy roster; players you're
  only rooting for appear in Rooting for alone. Both columns have Pin buttons.
  In Rooting for, each player goal gets one, and a player with several goals
  has a button on each row, all controlling the same pin. Team goals have
  none, since Top Players shows players. The Views tabs and Top Players still
  include rooting-only players.
- **Top Players.** Your pinned players first, then, if you've pinned fewer
  than four, your most invested players fill the rest. Pin as many as you
  like; the grid grows two across. Pin or unpin with the button on any player
  row, in Standard, the Views My Players tab, or Top Players itself. Pins are
  saved on the server, so the TV and laptop windows always agree and a reload
  keeps them, and they clear themselves when a new week starts. A pinned
  player stays on Top Players after their game ends, showing the final line,
  until you unpin them. Among pinned players the order matches Your players:
  live, then not started, then finished. Meant for the laptop while the TV runs the
  Views layout, which needs the laptop set to **Extend** rather than Mirror
  (Windows+P).
- **Views.** Six tabs, sized for viewing on a TV across a room:

| Tab | Shows |
|---|---|
| Home | Your live players; fantasy summaries; your Pick'em status |
| My Players | Everyone, in two columns: live, and not started |
| Fantasy | Each head-to-head, colored by projected margin |
| Rooting Interests | Every goal with a progress bar and an on-track verdict. Goals still in play first, then not started, then goals already decided in a live game, then finished games |
| BOZO | Your BOZO interests as cards in the Top Players style, without fantasy lines: logo and name; team and position; the game line, amber while live; your wording with the live number. Moneylines and spreads show the team's current margin, like "+3". The number is green when hit or on track, red when missed or off track, and gray before kickoff. Two summary lines sit on top: "X of Y hit" and "X in play, Y remaining, Z finished". Finished counts goals settled either way, including one decided while its game is still going, so the three numbers always add up to the total. |
| Teams | Every game in three columns, with your pick and your interests. While a game is live, your own fantasy starters in it also show as outlined chips, by name only; a player starting for you in several leagues gets one chip. |

**Fantasy colors** compare your projected final against your opponent's:
green for a projected win by more than 20, red for a projected loss by more
than 20, yellow when within 20 either way. A league without a projection shows
gray.

**On-track verdicts** on Rooting Interests:

| Verdict | Meaning |
|---|---|
| On track, green | At the current pace, the goal is met. An `over` already hit counts. |
| Off track, red | At the current pace, it is missed. An `under` already passed counts. |
| No verdict yet | Too early for pace, or a goal like `any TD` that only resolves when it happens or time runs out |
| Not started, light gray | Game hasn't kicked off |
| Final, light blue | Game over, with a green or red dot for hit or missed. The player's name and numbers turn light gray. |

Pace drives the verdict for yardage and receptions. Everything else carries a
**rule provisional** tag, because its rule is a first draft: `win` is green
while leading, `win by` compares the current margin to the target, `game
total` and team `points` use pace, and touchdowns and other lumpy counts stay
at no verdict until they hit or the game ends.

**Teams tab order:** live games first, then games carrying a rooting interest,
then the rest. Within each group, upcoming by kickoff, finished last. Each
card shows your Pick'em pick and its status, plus one indicator per rooting
interest in that game.

**TV sizing** lives at the top of `app.py`:

```python
TV_DISTANCE_FEET = 15
TV_DIAGONAL_INCHES = 40
TV_SIZE_ADJUST = 0.8
```

The first two set a base text size for glance-reading at that distance. Text
is sized as a share of screen height, so TV resolution and mirror-versus-extend
make no difference. `TV_SIZE_ADJUST` fine-tunes on top: 1.0 is exactly what
the distance math asks for, and 0.8 sits about midway between that and the
Standard view. On a 1080-line screen, that's 22-pixel base text against 16 for
Standard and 27.5 at 1.0. At 0.8, about 3 players fit per screen when each
carries a goal and two leagues. Lower values fit more but read harder from
across the room; the small labels, like game clocks, go first.

**Change flash.** A brief color pulse marks what changed since the last refresh:

| What | Threshold | Color |
|---|---|---|
| Game score | any change | amber |
| Fantasy points | a move of 2 or more | green up, soft red down |
| Rooting goal | a move of at least 7% of the target | green if it helps, soft red if it hurts |

An under going up hurts, so it pulses red. Opponent numbers pulse amber, since
their gain isn't yours. The page sees data per refresh, not per play, so two
small plays between refreshes can add up to one flash.

**Pace.** Yardage and reception goals still in play show a projection at the
current rate, like "on pace for 113". It appears once a tenth of the game has
passed, since one early play swings it wildly, and is skipped in overtime,
whose length isn't fixed.

### Rooting interests

**How the spreadsheet is read.** The importer reads "Rooting Revised" first,
then "Rooting", taking the first tab with a recognized header row. Columns are
found by header name, so their order doesn't matter. The week number is the
value beside the "Week" label at the top.

**Your wording, the Revised and Team columns.** Type each goal as you'd say it;
the Team column always decides the team:

| You type | It means |
|---|---|
| James Cook 80+ Rush Yds | At least 80 |
| James Cook 60.5- Rush Yds | 60.5 or fewer, so 60 or fewer |
| Davanate Adams Anytime TD | Scores a touchdown |
| Jaguars Moneyline | Team wins |
| Patriots 7.5+ Spread | Team wins by more than 7.5, so 8 or more |
| Packers 26.5+ Total Pts | Team scores at least 26.5, so 27 or more |

Any stat the importer knows works after the number, such as Rush Yds, Pass Yds,
Rec Yds, Rec, Rush + Rec Yds, Pass TDs, Completions, Sacks, or Field Goals.
"Yds" alone is refused, since it could mean rushing or receiving. A line it
can't read is reported as a notice and skipped, never guessed.

Your wording is shown on every card in place of the generated text. For a
player, the part after his name is shown, since the name already appears.

**BOZO marks.** A BOZO? column on "Rooting Revised," anywhere in the header
row, marks goals with Yes or Y. Without that column, nothing shows on the BOZO
tab, and a notice says so. In a text file, end a line with `[BOZO]`.

**The older Player, Team, Stat, Direction, and Target columns** still work on
any tab that has them.

**Re-imports after code changes.** The GitHub build re-imports every file
whenever the import code changes (`IMPORT_VERSION` in `build_site.py`), so the
order code and data files are uploaded in doesn't matter.

Easiest route is `rooting_interests.xlsx`. Put the week number in cell B2 and
fill the shaded rows. Team, Stat and Direction are dropdowns, so the only free
entry is the player name and the number. Leave Player blank for a team goal,
and leave Direction and Target blank when the stat is `win` or `any TD`.

```
python import_rooting.py rooting_interests.xlsx
```

The importer turns those rows into the same text lines the parser reads, so
both routes go through one set of rules and cannot drift apart. It reports any
row it skipped and why.

Needs `openpyxl`, which is in requirements.txt.

A plain text file works too, one goal per line:

```
Week 2

Jahmyr Gibbs (DET) rush yds over 85
Amon-Ra St. Brown (DET) rec over 6
Josh Allen (BUF) pass TD over 2
Travis Kelce (KC) any TD
James Cook (BUF) rush yds under 60

BUF win
KC points over 24
DAL points under 17
```

Then:

```
python import_rooting.py rooting_week_2.txt
```

Player lines need the team in parentheses. That is how the right game and
roster get found, and it removes the duplicate-name problem, since there are
two Josh Allens in the league but only one on Buffalo.

**Name resolution is lazy.** On import, the team's roster is searched and the
ESPN athlete id is stored permanently on the record. An ambiguous name prompts
you to choose, showing position and jersey. Grading afterward is an exact id
lookup, never fuzzy matching. Rosters are static, so this runs before kickoff
and a bad name surfaces while there is still time to fix it.

**Goal semantics:**

- `over N` is strictly greater than N
- `under N` is N or fewer, and only settles when the game ends
- `at least N` is shorthand for `over N-1` Half-point targets round up first: "at least 26.5" means 27 or more.
- `any TD` is total TDs over 0

An `over` clinches the moment it is met, since these stats do not fall. An
`under` can fail early, the instant the number is passed, but cannot succeed
until the game is final.

**States:** achieved (green), live (amber), failed (red, dimmed), pending (not
started), noshow (game running, player absent from the box score, meaning
inactive or no touches yet).

**Supported stats:** passing yards, passing TDs, completions, attempts,
interceptions thrown, rushing yards, rushing TDs, carries, receptions,
receiving yards, receiving TDs, targets, rushing + receiving yards, all purpose
yards, total TDs, tackles, solo tackles, sacks, tackles for loss, passes
defended, interceptions caught, field goals made and attempted, kicking points,
fumbles, fumbles lost.

**Team goals:** `win`, `win by over N`, `points over N`, `game total over N`.
Game total is both teams' points combined.

**A lead is not a win.** `win` and `win by` can swing either way, so they stay
live until the game ends rather than clinching early. Everything else only
climbs, so an `over` clinches the moment it is met.

Common phrasings are aliased, so `rush yds`, `rushing yards`, and `rush yards`
all work.

### Private Yahoo league (Sunscreen and Tums)

League 752951 has no API path, so its lineups come in once a week from a
screenshot of the matchup page. Only the roster is static; **every point is
computed live** from ESPN box scores using the league's own rules.

Send the screenshot after lineups are set and it becomes `yahoo_week_N.json`,
with each entry as `[slot, name as shown, team]`. Then:

```
python import_manual.py yahoo_week_3.json
```

ESPN occasionally refuses a valid roster request and serves it normally
moments later, so a failed lookup is retried twice before it counts. If it
still fails, that one player is reported and the rest of the import carries
on; re-running picks the player up. The rooting importer behaves the same way.

Names resolve against the team's roster, so `C. Williams` on Chicago becomes
Caleb Williams, and the full name is what gets stored. That is what lets him
merge with the same person on your ESPN roster in the player view. When two
players still match, a real position in the slot settles it; a flex slot
prompts you. Re-importing a week replaces it, which is how a late lineup
change gets picked up. Imports show on the dashboard's next refresh; this league
is rebuilt every refresh rather than cached, since its lineup lives in your
own database.

**The scoring was derived, not assumed.** The Week 2 screenshot showed stat
lines and Yahoo's point totals together for 22 players:

- Skill positions: 20 of 20 exact to the cent. Half PPR, 1 per 25 passing
  yards, 1 per 10 rushing and receiving, 4 per passing TD, 6 per rushing and
  receiving TD, -1 per interception, -2 per fumble lost.
- Kicker: 0.1 per yard of each made field goal, 1 per PAT, -1 per missed
  field goal at any distance, -1 per missed PAT. The first two were derived from
  the screenshot; the misses were confirmed from the league's settings page.
  Field goal distances come from ESPN's scoring plays, since the box score only
  reports makes, attempts, and the longest kick.
- Defense: 1 per sack, 2 per interception, 2 per fumble recovery, 6 per
  touchdown, and the full points-allowed scale from 10 for a shutout to -4 for
  35 or more. All confirmed from the league's settings page.
- Not scoreable from a box score: two-point conversions, safeties, blocked
  kicks, extra-point returns, and offensive fumble-return touchdowns.

**Projections** come from ESPN's per-player projected stat lines, rescored
with the rules above (`projection.py`), then blended with what has happened:
the projection before kickoff, actual points plus the projection scaled to the
share of game remaining while live, and the actual once final. A team
defense's points-allowed tier uses the current score plus its remaining
projected points against, since the tier depends on the final score.

ESPN identifies stats by number. The codes were proven by recomputing ESPN's
own totals from its raw stat lines: Lamar Jackson to within 0.01, Jahmyr Gibbs
to the cent, Ka'imi Fairbairn within the rounding of the probe's display. That
check also revealed ESPN reports total made field-goal yards directly, which is
exactly what this league scores, so kickers need no approximation. Defense
codes are internally consistent but could not be checked against a total,
since the ESPN league's own defense settings are unknown; treat those as
strongly inferred.

One approximation: ESPN's points-allowed buckets don't line up with this
league's tiers at 18 to 21, so that bucket is scored three-quarters as 14 to 20
and a quarter as 21 to 27.

If the projection request fails, points still show and the Fantasy tab reads
"projection pending" rather than a guessed color. Projections are cached for 15
minutes.

`python validate_scoring.py` scores the Week 2 players whose games were final
at the screenshot, straight from ESPN's box scores, and compares against
Yahoo's totals.

Team defenses merge across platforms on the team alone, since every platform
names them differently ("Texans" against "Texans D/ST").

### Scoring methodology

Straight picks, one point per game. A pick counts toward the week when it has
**already won or is currently leading**. Season total sums those counts across
every week imported.

A week never imported is a week that does not count. `SEASON_BASELINE` in
`picks.py` covers weeks predating this dashboard; set it to the confirmed Yahoo
total for those weeks.

---

## Phone version

A copy of the dashboard is published on GitHub Pages and works on any phone.
GitHub runs `build_site.py` on a schedule, using the same code the laptop runs,
and publishes the page and its data as plain files. Nothing connects to the
laptop.

**Refresh.** Every 5 minutes during Thursday, Sunday, and Monday game windows,
hourly otherwise, and right away whenever a file is uploaded. GitHub's schedule
can run late at busy times, so expect data 5 to 15 minutes old. The header shows
the age, as in "As of 4:35 PM · 6 min ago."

**Weekly imports.** Upload files to the `imports` folder in the repository:
`picks_week_N.json` (keep every week; the season total needs them),
`rooting_interests.xlsx`, and `yahoo_week_N.json`. Uploading a file with the
same name replaces it. The page rebuilds within a couple of minutes. Problems
an import would print on the laptop, like an ambiguous name, appear in a notice
at the top of the page until the file is fixed. A Pick'em file for a week that
hasn't started waits and imports automatically once it does.

The laptop and the phone keep separate data, so import to whichever ones you
use.

**Memory between runs.** The database is carried from run to run, so imports
only rerun when a file changes and requests to ESPN stay modest. If it's ever
lost, the next run rebuilds everything from `imports/`.

**Pins** on the phone are saved in the phone's browser, separate from the
laptop's, and clear each new week.

**Secrets.** Your ESPN cookies are stored as GitHub secrets `ESPN_S2` and
`ESPN_SWID`, never in the repository. If you log out of ESPN everywhere, the
cookie rotates and the ESPN card fails; copy the new value into the `ESPN_S2`
secret. League and team ids sit in the workflow file; they aren't secret.

**Visibility.** The page is public: anyone with the link can see team names,
picks, and rooting interests. Never upload `.env` or `yahoo_token.json`.

**Keep in mind.** GitHub pauses scheduled runs in a public repository after 60
days without changes; weekly uploads prevent that. Saturday games late in the
season fall to the hourly schedule until a window is added for them.

## Known issues

### Yahoo: one league works, one does not

**The authenticated API is dead on this account.** Every endpoint on
`fantasysports.yahooapis.com` returns:

> This application is not authorized to perform this action.

Including `game/nfl`, which needs almost no permission, and including the
*public* league. Ruled out across two separate apps: permissions (Fantasy
Sports / Read confirmed), consent scope (the screen explicitly grants fantasy
access), stale consent, client type, resource paths, URL encoding, client id
mismatch, and a full day of propagation time. This is an app entitlement
problem on Yahoo's side.

**But there is another door.** Yahoo's own web app calls
`pub-api-rw.fantasysports.yahoo.com/fantasy/v2`, which serves the same API and
answers **with no token, no crumb, and no cookies** for public leagues:

| | pub-api-rw | yahooapis |
|---|---|---|
| public league 1335544 | **200** | 403 |
| private league 752951 | 401 | 403 |

So league 1335544 works completely, with no credentials. League 752951 is
private and has no path: 401 without auth, 403 with it, since the same broken
entitlement follows the token.

`yahoo_public.py` is that route. `yahoo_client.py` and `yahoo_auth.py` stay in
the repo in case the entitlement ever clears; `python yahoo_diagnose.py` is
the check.

**Two limits worth knowing:**

- Per-player projections come back empty on the public host. Actual points,
  team totals, and team projections all work. Actual points are what matter
  live, so this is noted rather than worked around.
- No live win probability, which only ESPN publishes.

### The old 403 detail (kept for reference)

Every Yahoo Fantasy endpoint returns:

> This application is not authorized to perform this action.

Including `game/nfl`, which needs almost no permission. What has been ruled out:

- App permissions. Fantasy Sports with Read is checked and confirmed.
- Consent scope. The screen explicitly grants fantasy access.
- Stale consent. A second app was created so the screen could not be skipped.
- Client type. Confidential Client is correct for this flow.
- Resource paths. All nine tested endpoints fail identically.
- URL encoding. Semicolons are transmitted intact.
- Client ID mismatch. Verified against the app page.

This is a known and long-running Yahoo-side problem with no documented fix.
New apps sometimes need a day or more before Fantasy API access works.

**To retry:**

```
python yahoo_diagnose.py
```

If `game info (nfl)` returns 200 instead of 403, the entitlement propagated and
`yahoo_client.py` should work. It is already built and its parser is tested
against realistic Yahoo JSON fixtures.

**If it never clears:** the fallback is browser-session capture, reading Yahoo's
own internal endpoints from a logged-in tab. That carries meaningful risk on a
work machine. See *Decisions*.

### ESPN endpoint fragility

ESPN's fantasy API is undocumented and has changed hosts before. Field names
have shifted between seasons. If parsing breaks, `debug/` holds the raw
responses, which is enough to correct the parser.

---

## Environment problems already solved

These cost real time. Documented so they are not re-diagnosed.

**Corporate TLS inspection.** A security appliance re-signs HTTPS traffic with
an internal certificate. Windows trusts it; Python's bundled certificate list
does not. Symptom: `CERTIFICATE_VERIFY_FAILED ... self-signed certificate in
certificate chain`.

Fixed with `truststore`, imported before `httpx` in every module that makes
requests:

```python
import truststore
truststore.inject_into_ssl()
import httpx
```

This reads the Windows certificate store. It installs nothing, changes no system
setting, needs no admin rights, and does **not** disable verification.

**ESPN scoreboard 403 from Akamai.** Sending a Chrome User-Agent without the
rest of a browser's headers gets rejected as spoofing. An honest client name
passes cleanly:

```python
HEADERS = {"User-Agent": "football-dashboard/1.0"}
```

Tested six variants. No headers, honest UA, and a complete browser header set
all work. A bare Chrome UA does not.

**ESPN Fantasy returning nameless players.** Including `view=mRoster` alongside
`mMatchupScore` makes ESPN strip player objects down to stats only. The working
combination is:

```python
["mMatchupScore", "mScoreboard", "mSettings", "mTeam"]
```

That also supplies `totalPointsLive`, `totalProjectedPointsLive`, and
`winProbability`, none of which appear with `mBoxscore`.

**Curly quotes in `.env`.** Pasting cookie values through an app that
autocorrects quotes produces `'ascii' codec can't encode character '\u201d'`.
Retype the quotes in VS Code.

**Windows strftime.** `%-I` works on Linux and throws on Windows. The hour is
built by hand in `clock_12h()` instead.

---

## ESPN endpoint shapes, confirmed by probe

**Roster** (`/teams/{id}/roster`): `athletes` is a list of position groups
(offense, defense, specialTeam and so on), each with `position` and `items`.
Athlete fields include `id`, `displayName`, `firstName`, `lastName`, `jersey`,
`position.abbreviation`, `status.name`.

**Box score** (`/summary?event={id}`): `boxscore.players` holds two team blocks,
each with `team` and `statistics`. There are ten categories: passing, rushing,
receiving, fumbles, defensive, interceptions, kickReturns, puntReturns, kicking,
punting. Each carries a `labels` array and an `athletes` array whose `stats`
align positionally to the labels.

Compound values needing a split: passing `C/ATT` arrives as `'26/38'`, passing
`SACKS` as `'4-38'`, kicking `FG` as `'1/2'` and `XP` as `'4/4'`.

`header.competitions[0].competitors[]` carries team scores.

## Decisions

**DraftKings removed from scope.** Originally planned. Dropped because no US
sportsbook offers an API for your own bet slips, and every workaround (browser
extension, session automation) is behaviorally indistinguishable from
credential-stealing malware, which is a poor fit for a work laptop running
endpoint detection. Email parsing via the Gmail API was the lowest-risk option
and remains available if scope changes.

No DraftKings code was ever written. Nothing needs removing.

**Deferred: move to a personal machine.** Doing so would resolve, in one step,
the corporate proxy and certificate friction, the browser-extension risk, and
the question of accumulating betting records on company hardware. It would also
reopen DraftKings and the Yahoo browser fallback. The code is portable and setup
would take under an hour now that the environment problems are understood.
Tabled deliberately, to revisit.

**Pick'em entered manually.** Picks lock once a week and take about two minutes
to enter. Automating it would require the same browser-session capture that
carries the risk above, for very little gain, since grading is local either way.

**Top-3 competitor comparison dropped.** The original spec included comparing
against the top three players in the pool. That requires every member's
individual picks, not just their totals, which means scraping the full group
grid. Cut in favor of showing your own numbers only. Consequence: live-adjusted
*rank* is not computable, only your count and total.

---

## Future work

- Wire in the two Yahoo fantasy teams once the 403 clears
- Validate live clock behavior during an active game
- Optionally show Yahoo's official Pick'em rank, noting it reflects completed
  games only and will lag the live count during a slate
- Consider a combined by-game view showing every interest touching each live
  game, including conflicts (holding a team while your fantasy opponent starts
  its quarterback)
