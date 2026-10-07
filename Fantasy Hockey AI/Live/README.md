# Live

The tools that act on a real league: the preseason draft board, the draft-night assistant and
window, and the in-season runner that writes each day's plan. Recommend-only throughout -- nothing
here submits a pick or a move to a platform.

```
draft_board.py      the VOR board from the external sources' consensus (reports/<league>/draft_board_*)
draft_assistant.py  draft night in the terminal: follows the live draft (Fleaflicker), or --standalone
draft_gui.py        the same in a window
live.py             the live runner: the shipped manager on the real league (see its docstring)
plan_gui.py         the day's plan in a window, from the plan server (Server/server.py): its newest
                    plan on opening, Full / Quick refresh buttons that run on the server, and the
                    server's own re-plan ~30 min before each group of games followed as it runs;
                    Games, Goals and Lines tabs follow tonight's games live (Server/games.py)
run_live.py         the same pass in the terminal (reports/<league>/plans/plan_{date}_*.md)
planpass.py         the pass's steps, shared by both: snapshots, tonight, read league, plan, save
sheets.py           the windows' read-only tksheet tables: row colours by tag, header-click sorting
leagues.py          the league registry (Settings/leagues/<name>.json): --league on every tool
platforms/          read-only platform adapters: fleaflicker.py (draft board, rosters + IR, lineup
                    slots, moves used this week, matchup, league settings), espn.py (settings + scoring,
                    rosters + IR, lineup slots, matchup, draft; private leagues via cookies),
                    standalone.py. Platform ids -> PlayerIDs via platforms.PlayerIds
sync_league_settings.py  detects a league's settings (Settings/rosters + scoring) from its platform;
                    the adapters' league_settings() translate, Season/league.py says what they mean
seasonlayer.py      the one bridge into Season/ (league, state, view, schedule, inputs, ...)
livepaths.py        Live's own locations (never named paths.py -- see seasonlayer.py)
fixtures/           made-up leagues for exercising the tools before a league has rosters
reports/<league>/   one folder per league: draft boards, the draft-assistant view, plans (gitignored)
```

Moved out of `Season/` on 2026-09-25: `Season/` is the backtest simulator and never touches a
platform or the network; this folder does both. It shares `Season/`'s league model through
`seasonlayer.py` (the flat-module convention: Season/ goes on `sys.path`, and no module here may
share a name with one in Season/, Decisions/ or Simulation/ -- checked at import).

```
python draft_assistant.py                                   # follow the draft (league beagles)
python draft_assistant.py --team 63341 --replay-season 2025 # rehearse on the 2025 draft
python draft_assistant.py --league <an espn league>         # an ESPN draft, once it is over (see below)
python draft_gui.py --league espn-la --standalone --slot 1  # a live ESPN draft: double-click every pick
python draft_assistant.py --league espn --slot 10           # an unreadable draft: type picks
python draft_gui.py                                         # the window
python plan_gui.py                                          # today's plan from the server at 127.0.0.1:8000
python plan_gui.py --league espn-la                        # the ESPN league's plan, from the same server
python plan_gui.py --server http://host:8000                # another server
python run_live.py --make-fake                              # fixtures/beagles/fake_league.json
python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --refresh
python run_live.py --date 2026-09-29 --platform-season 2025          # rehearse on 12090's 2025 rosters
python run_live.py --window --refresh                       # in season: reads the league from Fleaflicker
```

**ESPN drafts cannot be followed live.** ESPN's read API (`mDraftDetail`) lists every pick slot
with no player until the draft is over (espn-la, 2026-09-26: 0 picks at 2.5 minutes in, all 220
the moment it finished), and ESPN re-draws the pick order when the draft starts. So a live ESPN
draft runs `--standalone --slot <your slot once the order is drawn>`, every pick double-clicked;
the adapter reads the finished draft fine.

**In season on ESPN** (`--league espn-la`) the runner reads rosters, IR, lineup slots and the
matchup from ESPN, and who is on waivers until when (`Espn.waivers`, so an unrostered player on
waivers is a claim, not an add), and moves used this week (each team's
`transactionCounter.matchupAcquisitionTotals`) against this week's limit (`Espn.acquisitions`):
`matchupAcquisitionLimit`, per day when `matchupLimitPerScoringPeriod` -- espn-la's 1 a day is 6 in
its 6-day week 1 and 7 in a full week. `acquisitionLimit` is not it (-1 on espn-la). Both count
in the week a move made now takes effect (`Espn.move_period`, from `transactionScoringPeriod`):
after the day's first puck that is tomorrow, so a Sunday-night move counts toward next week.

Plans run in the plan server (Server/server.py: when the window asks, and its auto window) or
when you call run_live.py. Task Scheduler keeps the injury snapshots and the nightly ingest
running; the server's `--skip-snapshots` uses those instead of taking new ones, and its `--date`,
`--league-file` and `--now` are the rehearsal options plan_gui.py used to take.

The in-season inputs come from the other folders: tonight's projections from
`ModelFeatures/build_tonight.py` + `Projections/project_tonight.py` (run by `--refresh`), the
live injury and lineup reports from `pipeline/snapshot_live.py`.

**Moves are priced on the rest-of-season model** (since 2026-10-03; `live.LiveRunner.skater_ros`):
each skater's rate per team game is the newest `Projections/reports/ros_projections_<season>_<date>.parquet`
on or before the plan's day, scored under the league's own scoring. The nightly ingest
(`pipeline/run_nightly_ingest.cmd`) rebuilds the season's feature table and runs `ros_predict.py`
after the games. A skater it has not projected (not dressed yet) gets the preseason board scaled
to the model's level, so one comparison never mixes the two; goalies keep their start share. With
no projection file the plan prices on the board alone and lists that under its problems. Until
then the plan priced on the preseason board, frozen; the history-prior model measured level with
it in the realistic league and, unlike the board, follows the season (Decisions/valuation.py
`rate`). The Roster and Free agents tabs' Rate and ROS pts are these numbers.

**Blended with the board since 2026-10-06** (`adddrop.rate_source = "blend"` in both live strategies):
a skater on both gets the board, scaled to the model's level, weighted 7/(7+games he has played this
season), and the model for the rest (Decisions/valuation.py `BLEND_GAMES`). Jake Sanderson left
espn-la's opener after 4:29; the model alone cut him from 309 to 164 rest-of-season points and the plan
rented him away for Cam York. Blended he is 1.82 a team game, not 1.15, and kept. Neutral in the
realistic league (+0.94 +/- 0.84 pts/wk, 2024-25, 56 drafts); the user's call. Confirmed on 2025-26
(56 drafts): reverting to the model alone -4.19 +/- 1.01, both halves negative.

## The draft board

**The live board**: `python draft_board.py --season 2026-27 --weights points-league` writes
`reports/<league>/draft_board_2026-27_<scoring>.csv/.md` -- rank, player, team, positions, value, VOR,
the position he is measured against, sources, what the value rests on, and Yahoo and ESPN ADP beside
it for reading whether a target will last to the next pick. 2026-27 goalies have three sources
(Dailyfaceoff, Dom, Lineup Experts), so a goalie needs two of them or falls back.

## Draft night: `draft_assistant.py`

The league is Fleaflicker 12090, and its scoring and slots are exactly `points-league` and
`rosters/league.json` (14 teams, C2 LW2 RW2 F1 D4 F/D1 G2, 4 bench, 2 IR, 18-round snake).

```
python draft_assistant.py --team "Burnaby Beagles"          # follows the live draft, every 10 s
python draft_assistant.py --team "Burnaby Beagles" --once   # one snapshot
python draft_assistant.py --team "Burnaby Beagles" --manual # the API is down: type each pick
```

**Settings (the window's ⚙).** A popup laid out like the aggregate workbook's Settings sheet:
positions and ADP platform, the playoff dates (OFF/POG), Roster Settings (teams, C, LW, RW, W, F,
D, UTIL(F/D), G, BN) and the points per stat for the 14 stats the projections carry. Apply rebuilds
the board -- values, replacement levels and slot fits all follow -- and refuses what the league
config refuses (an odd team count); Save as default writes the league's `Settings/leagues/` file, which the
window and `draft_assistant.py` start from; League defaults puts back `rosters/league.json`,
`scoring/points-league.json` and the platform's playoff weeks. The simulator never reads the file.
Setting the sheet's own values (12 teams, PIM off) reproduces its FanPts: MacKinnon 540.0.

Players are valued on one platform's positions, `draft.eligibility_platform` in
`Settings/strategy.json` (default Fleaflicker, the league's own; the aggregate workbook's "Site
Used (POS)"). `--eligibility yahoo` overrides it for a run, and "Positions from" in the window's
⚙ settings popup rebuilds the board on another platform live (about 0.3 s): values over replacement,
replacement levels and which slots a player fills all follow. The simulator keeps
`rosters/league.json`'s Yahoo positions either way.

It reads Fleaflicker's public draft board (read only; it never submits a pick) and shows:

- who is on the clock and how many picks until your next two;
- the best available by value over replacement on Fleaflicker's positions, with whether each fills
  one of your open starting slots, and one platform's ADP beside it (`draft.adp_platform` in
  `Settings/strategy.json`, default ESPN; `--adp` overrides it, and so does the window's ⚙ settings popup),
  used only for "likely gone by your next pick";
- your roster and open slots, and the positional-need warning;
- the last 10 picks, goalie and defence runs, and any pick it could not match (listed, never
  dropped).

Picks are matched by Fleaflicker's own player id (`Fantasy.PlatformPlayerIDs`, written by
`pipeline/import_fantasy_fleaflicker.py`, exported by `ModelFeatures/build_players.py`); all 400
of the board's top players have one. The latest view is also written to
`reports/<league>/draft_assistant.md`.

**The same thing in a window:** `draft_gui.py` (tkinter, same flags, `--manual` = double-click a
player to record the pick on the clock). Two tabs: **Rankings**, a spreadsheet of the whole board
with each player's projected line in every stat the league scores plus GP (the consensus line,
or last season's totals for a player valued on last season -- the line his value came from); click
a header to sort (blanks always last); filter by position, "fills my slot" or name; "show taken"
greys taken players; "Reset view" puts sort, filter and search back. **OFF** and **POG** are the aggregate
workbook's schedule columns for the player's team, with its formulas (`draft_board.schedule_counts`,
checked equal to the workbook's Schedule Info sheet for all 32 teams): OFF is games on nights with 8
or fewer NHL games from opening night through the end of the fantasy playoffs, POG is games during
the fantasy playoffs. The playoff weeks are the league's last `playoff_weeks` on Fleaflicker's
schedule -- weeks 24-26, 2027-03-15 to 2027-04-04 (the workbook's own Settings default, the NHL
season's last three weeks, is 03-20 to 04-10). `draft_board.py --playoffs START END` adds them to
the saved board. The **🩹** column is Dobber's Band-Aid Boys
(`pipeline/import_injury_risk.py` -> `Injuries.RiskLists` -> `injury_risk.parquet` from
`build_players.py`): Certified (virtually guaranteed to miss games, a significant risk to miss 12+),
Trainee (probably six or seven, some risk of 12+) or Goalie (listed apart, no tier), the way the
aggregate workbook's 🩹 checkbox marks them, shown as 🩹 C, T or G. **Periph %** is the share of a skater's points from
his peripheral categories -- hits, blocks, shots and PIM, the scored stats that are not scoring --
under the league's scoring, from the same stat line as his value (`draft_board.peripheral_share`;
blank for goalies). Top-300 skaters run from 19% (Kucherov, Draisaitl) to 83% (Lauzon). Shown only; the value is the projections' either way. **Boom @N** and **Bust @N** are his odds were you to take him at your next pick N: from `Season/boom_bust.py`'s saved run (5,000 simulated seasons, 200 simulated drafts of realistic opponents), boom = P(he returns what the pick a round earlier usually does), bust = P(he returns less than the pick a round later usually does). Read them comparatively -- a 25% bust is safer than a 60% at the same pick; they are graded in `Season/docs/boom-bust-check.md`. The saved board CSV carries them at each player's median room pick (`room_pick`, `boom_pct`, `bust_pct`) with `p10`/`p90` season points and `exp_gp`; the assistant's table shows them at your next pick too. They are hidden when the window's scoring weights or room differ from the run; rerun `python boom_bust.py --season 2026-27 --draft-date <today>` in Season/ after the projections change. And **Draft board**, rounds by teams in draft order, coloured by
position, with the pick on the clock and your column highlighted. The sidebar has the clock, your
next picks, open slots, your roster, the last picks and unmatched picks.

**Rehearsal.** `--replay-season 2025` replays this league's finished 2025 draft as if it were live:
every poll really reads Fleaflicker (`FetchLeagueDraftBoard&season=2025`), and the picks not yet
"made" are hidden, one revealed every `--replay-seconds` (default 5) from `--replay-start`. Your
team is the same id both years (63341; in 2025 it was One if by Landeskog), so pass `--team 63341`:

```
python draft_gui.py --team 63341 --replay-season 2025 --replay-seconds 3
python draft_assistant.py --team 63341 --replay-season 2025 --replay-start 20 --once
```

Checked 2026-09-25: all 252 of the 2025 draft's real picks match a player by Fleaflicker id, none
unmatched, and the window follows the replay pick by pick.

**Before the draft,** refresh positions and ids from the league:

```
python import_fantasy_fleaflicker.py                                          # pipeline/
python import_injury_risk.py                                                   # pipeline/, the Band-Aid Boys
python build_players.py                                                        # ModelFeatures/
python build_fantasy_positions.py --platform Fleaflicker --season 2026-27     # ModelFeatures/
python build_schedule.py --season 2026-27                                     # ModelFeatures/, for OFF and POG
```
