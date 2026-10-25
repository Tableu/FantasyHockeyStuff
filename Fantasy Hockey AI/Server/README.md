# Server

The phone app's server (Fantasy Hockey Phone App Plan). Today: the Linux test environment of
Phase 1 -- the plan steps run in a container capped like the Azure VM (2 vCPU, 1 GiB RAM + 2 GiB
swap) against this PC's SQL Server -- and the server (Phase 1.5): server.py, the plan refreshes
behind a small HTTP API, which the app (app/, phone or PC browser) shows. Since 2026-10-24 the
engine those refreshes run -- the live runner, the plan pass, the platform adapters -- lives here
too (it was Live/; see "The engine" below), and the desktop windows are gone but for the draft
tools (Desktop/).

## The server

    python server.py                    # every active league (beagles, espn-la), today, http://127.0.0.1:8000
    python server.py --league beagles   # one league; repeat --league for several
    python server.py --league beagles --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --skip-snapshots --no-auto      # rehearsal

One server plans every league. A refresh takes the snapshots and tonight's projections once, then
reads, plans and saves each league in turn (one league failing leaves the others' plans), and the
auto window re-plans every league. Every call but the job's takes `?league=<name>`, defaulting to
the first league served; plan_gui.py sends its own `--league`.

| Call | Does |
| --- | --- |
| GET /plan | The league's newest saved plan for the plan's day -- today, or tomorrow once every game today has started (today's until tomorrow's is saved): {league, file, saved_at, plan} (404 before the first) |
| POST /refresh {"mode", "scope"} | Starts a refresh and returns its job; a tap while one runs joins it. mode: "full" (injury, line-chart and goalie reports, projections, leagues read again), "quick" (the same without line charts), "news" (reports and projections, leagues planned on today's kept read), "league" (leagues read again and planned, no reports; on a day with no projections yet, those first), "plan" (planned on the kept read and your choices, about 2 s, queued behind a running refresh). scope: "league" (?league= alone) or "all"; default "all" for full and quick (plan_gui), "league" for the rest. The auto window plans every league |
| GET /jobs/{id}?after=n | The job's state, each league's saved plan file, its progress lines after line n |
| GET /status | The leagues served, the day, the running job, when each step last ran (with the league's own read), its newest plan |
| GET /games | Today's games: score, clock, how many of your players and your opponent's are in each |
| GET /goals | Every goal today, newest first, with the fantasy points it earned either side |
| GET /games/{id}?after=n | One game: line score, team stats, box score with fantasy points, plays after sortOrder n, each with the fantasy points it earned either side (`fp`; a goalie's decision comes with the box score) |
| GET /games/{id}/lines | Each team's lines, pairs and power-play / penalty-kill units as used |
| GET /choices | What you chose in the app (choices.py): {league, choices: {days: {date: {moves}}, together}} |
| GET /tonight | Every player you and your opponent hold: his game today and, once it starts, his box-score line with fantasy points; your lineup's live total |
| GET /roster | The app's Roster screen from the newest plan: lineup as it stands (league slot order), bench, IR; tonight's projection, season FP, FP/g, ROS and stats, the plan's notes |
| GET /jobs | The recent refreshes, newest first, without their lines |
| GET /matchup | The week's matchup: the platform's scores (every team's week points, yours and your opponent's by day, slots to play on Fleaflicker; read at most once a minute) with the newest plan's projection and P(win) when the plan is this week's |
| GET /suggestions?week=this or next | The app's AI suggestions from the newest plan: today's moves, the upgrades, the week plans with your picks marked |
| POST /choices/pick {"week", "label", "index", "player_id"} | A week plan's option picked into your week with the plan's drop (a chained rental's earlier pick with it), or taken back if picked; returns the suggestions as they now stand |
| GET /week?week=this or next | The app's Weekly planner: your week from your picks on plan A's workbench -- each night's points, open slots, your picks (with why one is left out) and who starts in each slot |
| GET /week/picker?day=&occupant= | The slot picker: the free agents who would start that night, the plans' picks first (occupant: the rental whose slot it is) |
| GET /week/drops?day=&add=&occupant= | Who that pick may drop, each with the week's gain, and the one to select first (the plan's drop) |
| POST /week/edit {"week", "action", "day", "add", "drop", "rental"} | pick, replace (a rental), unpick or clear; saved, returns the week as it now stands |
| PUT /choices {"choices": {...}} | Replaces them; returns {league, choices}. Nothing re-plans until a refresh, or POST /refresh {"mode": "plan"}?league= (that league on its last read, about 2 s) |

The live games calls are games.py's: the NHL's public feeds held in memory (nothing goes to the
database), one shared copy per feed for every league, fetched again only once the NHL's cache says it expired (about
20 s) and only when someone asks. The lines come from the NHL's HTML time-on-ice reports (about a
minute behind; asked with If-Modified-Since, which they honour with a 304) and the nightly lineup
build's clustering. Live, the strength each second comes from who the reports put on the ice, not
the play-by-play's situation codes, which lag and misreport during a game. Fantasy owners come from
the newest plan and players.parquet's NHL ids.

The refreshes are planpass.Planner's and save the plan files under reports/<league>/plans/.
The auto window (a refresh ~30 minutes before each group of games -- Full when the line charts
are none today or over 4 h old (LINES_MAX_AGE), else Quick -- and one once today's last
game has started) runs in the server unless --no-auto. From that last puck on, the plan is
tomorrow's: its moves, lineup and projections, since tonight has no lineup left to set. It
listens on this PC only (in the container: --host 0.0.0.0, which compose publishes to 127.0.0.1
alone); the tunnel reaches it from outside, behind the login.

## The engine

What a refresh runs, moved here from Live/ on 2026-10-24 (module names kept):

```
live.py             the live runner: the shipped manager on the real league (see its docstring)
planpass.py         the pass's steps: snapshots, tonight, read league, plan, save; Planner runs them
                    as the server's refreshes; --make-fake writes a made-up league to rehearse on
leagues.py          the league registry (Settings/leagues/<name>.json): --league / ?league=
choices.py          what you chose in the app (reports/<league>/choices.json): your week's picks
                    and drops, and whether the week plans follow today's upgrades
weekbook.py         the Weekly planner: your week built from your picks on plan A's workbench
draft_board.py      the VOR board from the external sources' consensus (live.py builds it every plan;
                    reports/<league>/draft_board_*)
sync_league_settings.py  detects a league's settings (Settings/rosters + scoring) from its platform,
                    daily in the server; --check translates the saved answers in fixtures/settings/
platforms/          read-only platform adapters: fleaflicker.py, espn.py (private leagues via
                    cookies), standalone.py. Platform ids -> PlayerIDs via platforms.PlayerIds
seasonlayer.py      the one bridge into Season/ (league, state, view, schedule, inputs, ...)
livepaths.py        this folder's own locations (never named paths.py -- see seasonlayer.py)
fixtures/           made-up leagues and saved platform answers
reports/<league>/   one folder per league: plans, choices, the last read, draft boards (gitignored)
```

Recommend-only throughout -- nothing submits a pick or a move to a platform. `Season/` is the
backtest simulator and never touches a platform or the network; the engine shares its league
model through `seasonlayer.py` (the flat-module convention: Season/ goes on `sys.path`, and no
module here may share a name with one in Season/, Decisions/ or Simulation/ -- checked at import).

**On ESPN** (`espn-la`) the runner reads rosters, IR, lineup slots and the matchup from ESPN, and
who is on waivers until when (`Espn.waivers`, so an unrostered player on waivers is a claim, not
an add), and moves used this week (each team's `transactionCounter.matchupAcquisitionTotals`)
against this week's limit (`Espn.acquisitions`): `matchupAcquisitionLimit`, per day when
`matchupLimitPerScoringPeriod` -- espn-la's 1 a day is 6 in its 6-day week 1 and 7 in a full week.
`acquisitionLimit` is not it (-1 on espn-la). Both count in the week a move made now takes effect
(`Espn.move_period`, from `transactionScoringPeriod`): after the day's first puck that is
tomorrow, so a Sunday-night move counts toward next week.

The in-season inputs come from the other folders: tonight's projections from
`ModelFeatures/build_tonight.py` + `Projections/project_tonight.py`, the live injury and lineup
reports from `pipeline/snapshot_live.py`.

**Moves are priced on the rest-of-season model** (since 2026-10-03; `live.LiveRunner.skater_ros`):
each skater's rate per team game is the newest `Projections/reports/ros_projections_<season>_<date>.parquet`
on or before the plan's day, scored under the league's own scoring. The nightly job rebuilds the
season's feature table and runs `ros_predict.py` after the games. A skater it has not projected
(not dressed yet) gets the preseason board scaled to the model's level, so one comparison never
mixes the two; goalies keep their start share. With no projection file the plan prices on the
board alone and lists that under its problems. Until then the plan priced on the preseason board,
frozen; the history-prior model measured level with it in the realistic league and, unlike the
board, follows the season (Decisions/valuation.py `rate`).

**Blended with the board since 2026-10-06** (`adddrop.rate_source = "blend"` in both live strategies):
a skater on both gets the board, scaled to the model's level, weighted 7/(7+games he has played this
season), and the model for the rest (Decisions/valuation.py `BLEND_GAMES`). Jake Sanderson left
espn-la's opener after 4:29; the model alone cut him from 309 to 164 rest-of-season points and the plan
rented him away for Cam York. Blended he is 1.82 a team game, not 1.15, and kept. Neutral in the
realistic league (+0.94 +/- 0.84 pts/wk, 2024-25, 56 drafts); the user's call. Confirmed on 2025-26
(56 drafts): reverting to the model alone -4.19 +/- 1.01, both halves negative.

**The draft board**: `python draft_board.py --season 2026-27 --weights points-league` writes
`reports/<league>/draft_board_2026-27_<scoring>.csv/.md` -- rank, player, team, positions, value, VOR,
the position he is measured against, sources, what the value rests on, and Yahoo and ESPN ADP beside
it. 2026-27 goalies have three sources (Dailyfaceoff, Dom, Lineup Experts), so a goalie needs two
of them or falls back. The draft tools that show it on draft night are in Desktop/.

## The phone app

`app/` is the phone app (a PWA, plan `phone-app-pwa.md`), served by server.py at `/app/`: static
files, no build step. Preact + htm are vendored in `app/vendor/` (htm 3.1.1's standalone build) and the IBM
Plex fonts in `app/fonts/`, so nothing loads from a CDN. The app only shows what the API returns.

Built so far: P0, the header (league, day, how long ago each step ran, amber past 2 h; on the PC
one Refresh button, a Quick of the league shown), a running refresh's progress lines, the bottom
tabs, and the More tab (Refresh: League, News, Quick, Full for the league shown; league switch,
refresh log from `GET /jobs`, server status). Pull down on Roster, Planner, AI or Matchup runs a
Quick of the league shown; a screen with no plan yet has a Make a plan button; P1, the Roster screen (`app/roster.js`): your lineup
as it stands, bench and IR from `GET /roster` (roster.py: tonight's projection, the season in the
league's scoring), with tonight's games and live points from `GET /tonight` (games.py, every 20 s).
P2, the AI suggestions screen (`app/ai.js`): today's moves, the upgrades and the week plans from
`GET /suggestions` (suggestions.py); an option tapped picks him into your week with the plan's drop
(`POST /choices/pick`, choices.py's pick_from_plan -- the window's click, moved there).
P3, the Weekly planner (`app/planner.js`): your week from your picks on plan A's workbench (`GET
/week`, week.py on weekbook.py), the slot picker (`GET /week/picker`, `GET /week/drops`) and
every change through `POST /week/edit` -- the window's picker logic, moved into weekbook.py and
choices.py so both use it. P4, Games (`app/games.js`): today's games (`GET /games`) with the goal feed
(`GET /goals`) in a sheet; a game full screen with its Summary, Box and Plays (`GET /games/{id}`, each
play with the fantasy points it earned) and Lines (`GET /games/{id}/lines`), polled every 5 s while
live. P5, Matchup (`app/matchup.js`, `GET /matchup`, matchup.py): the week's score, P(win), tonight live
against projected, the week day by day and the league's week -- the scores read from the platform
(platforms: `scoreboard`, `daily_points`, kept a minute), the projection the plan's own
(`matchup_projection`, each side's nights from the z's projection in Decisions/managers.py). P6, offline
and install: the service worker (`app/sw.js`) keeps the last answer for each screen's data (status,
plan, roster, suggestions, week, matchup, choices, the refresh log) and serves it when the server
is out of reach, with an "Offline · showing what was saved at ..." banner; live game calls are never
kept. Pull down at the top of a screen to run Quick. The app's files are served with `Cache-Control:
no-cache`, so an edit shows on the next load.

On a PC (a window at least 1024 px wide, `app/layout.js`) the same files lay out wider, as the
Fantasy Hockey PC design canvas: the tabs become a sidebar; Roster shows tonight's and the season's
columns together; the Planner's picker is a panel beside the calendar; AI shows today's moves and
the upgrades beside the week plans; Games puts the list, the chosen game and the goal feed side by
side; Matchup and More use two columns. Content fills the window; 600–1023 px is the phone layout
across the window. Esc closes the picker, the goal feed and the menus.

Testing it never uses port 8000 or a real league's name: run a test server on 8001 under a
temporary `Settings/leagues/<name>-test.json` (deleted after), e.g.

    python server.py --league beagles-test --port 8001 --date 2026-09-29 \
        --league-file fixtures/beagles/fake_league.json --skip-snapshots --no-auto

then open `http://127.0.0.1:8001/app/` in Chrome (DevTools device mode, 390x844). On an Android
phone over USB: `chrome://inspect` -> Port forwarding 8001 -> localhost:8001, then open
`http://localhost:8001/app/` on the phone; as `localhost` it can be installed without HTTPS.

## Login

The server is meant to be reached from the internet (plan: Phone App Remote Access), so every call
but `/login`, `/logout` and the app's files (`/app/...`) needs a session (auth.py):

- **The phone** logs in with the password (`POST /login`) and gets a signed cookie for 90 days,
  renewed once it has under 60 left; More -> Log out ends it and deletes the saved screens.
  Five wrong passwords from one address in 15 minutes and it waits 15 minutes.
- **Settings:** `"plan-server"` in `Settings/secrets.json` (gitignored): the password's scrypt hash
  and the session-signing secret. Read again when the file changes, no restart needed. (A key for
  plan_gui went with the window, 2026-10-24.)
- **Setting the password:** `python auth.py set-password` (in this folder, on Windows or in the
  container). A new password signs everyone out. Until one is set every call answers 503.
- FastAPI's `/docs`, `/redoc` and `/openapi.json` are off.

## Running it

Needs Docker Desktop (it installs WSL2).

    cd "Fantasy Hockey AI/Server"
    docker compose up -d --build        # the container, running server.py (localhost:8000 on Windows)
    docker compose logs -f server       # its output
    docker compose exec server bash     # a shell in it; the repo is /app
    cd /app/pipeline && python snapshot_live.py --kind injuries --dry-run
    docker compose down                 # stops it (otherwise it comes back with Docker Desktop)

Memory: the leagues are planned in a child process per refresh (planpass.plan_in_child), which
exits after, so the server stays the same size however many leagues it plans; it held about 480 MiB
with two leagues planned in its own process (2026-10-02), of which under 1 MB per league was
anything it kept.

A rehearsal server (--league, --date, --league-file, --skip-snapshots, --no-auto) runs by hand in the shell
on another port, e.g. `python server.py --host 0.0.0.0 --port 8001 ...`, which compose does not
publish -- or stop the container and run server.py on Windows.

- **Code and data** are not in the image: the repo is mounted at /app, so an edit on Windows runs
  in the container at once. Reading the mounted Windows folder is slower than a Linux disk, so
  timings here are an upper bound for the VM.
- **Database:** the same credentials files as on Windows; `NHLSTATS_DB_SERVER` (compose.yaml)
  replaces their `localhost`, read by pipeline/nhl_pipeline/config.py and
  ModelFeatures/nhlstats_db.py. Since 2026-10-15 (plan container-own-db) it is `db,1433`: the
  compose `db` service, SQL Server Express with a slim copy of NHLStats (db_seed.py: this season
  whole, last season's games and lineups, no raw responses), the only database anything writes.
  The nightly job ends with sync_back.py, which brings the PC's full NHLStats (model training's)
  up to it: other tables row by row, this season's new games whole. The PC's database is written
  by nothing else -- a one-off import goes to the container's (`NHLSTATS_DB_SERVER=127.0.0.1,14330`
  on Windows), or the copy-back overwrites it.
  - **Daily jobs** (scheduled.py, `--jobs`): nightly 04:00, player teams 05:00, injuries 10:00 and
    15:00, each caught up after a sleep. The Windows FantasyHockey-* tasks are disabled, not deleted.
  - **Rollback:** `NHLSTATS_DB_SERVER: host.docker.internal` in compose.yaml (the copy-back then
    stops by itself), `docker compose up -d server`, and re-enable the four tasks
    (`Enable-ScheduledTask FantasyHockey-NightlyIngest` etc.).
  - **Re-seed:** a backup of the PC's NHLStats (`sqlcmd -S localhost -E -C -Q "BACKUP DATABASE
    NHLStats TO DISK=N'C:\Users\Public\Documents\NHLStats_container.bak' WITH COPY_ONLY,
    COMPRESSION, INIT"`) moved into dbseed/, then `python db_seed.py` with the server stopped.
  - **Checks:** trial_check.py compares the PC's database with the container's game by game.
- **Time zone:** America/Vancouver, so "today" is the same day as on the PC.
- **Port 8000** is published to this PC only (127.0.0.1): the app in a browser on the PC at
  `http://127.0.0.1:8000/app/` (its wide layout), and the tunnel from outside.
- Measured 2026-10-02: a full refresh 36 s, a quick one 21 s (one league). 2026-10-12, both
  leagues: a quick one 27 s before the leagues' reads ran alongside the data steps, 19 s after,
  17 s once build_tonight kept its played-games history between runs (one league: 12 s).
- **measure_steps.py** times each refresh and nightly step in its own process with its peak memory
  (the plan's Phase 0 table): `docker compose exec server python measure_steps.py`. Linux only.
  2026-10-02 in the container: every step ran; the largest was build_tonight on a late-season day
  (34 s, 417 MB), and the container peaked at 635 MiB with no swap.
