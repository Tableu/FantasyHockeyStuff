# Live

The tools that act on a real league: the preseason draft board, the draft-night assistant and
window, and the in-season runner that writes each day's plan. Recommend-only throughout -- nothing
here submits a pick or a move to a platform.

```
draft_board.py      the VOR board from the external sources' consensus (reports/draft_board_*.csv/.md)
draft_assistant.py  draft night in the terminal: follows the live draft (Fleaflicker), or --standalone
draft_gui.py        the same in a window
live.py             the live runner: the shipped manager on the real league (see its docstring)
run_live.py         writes the day's plan (reports/plans/<league>/plan_{date}_*.md); --window per game
leagues.py          the league registry (Settings/leagues/<name>.json): --league on every tool
platforms/          read-only platform adapters: fleaflicker.py (draft board, rosters + IR, lineup
                    slots, moves used this week, matchup, rules), espn.py (settings + scoring,
                    rosters + IR, lineup slots, matchup, draft; private leagues via cookies),
                    standalone.py. Platform ids -> PlayerIDs via platforms.PlayerIds
import_league_settings.py  writes a league's Settings/rosters + scoring files from its platform
seasonlayer.py      the one bridge into Season/ (league, state, view, schedule, inputs, ...)
livepaths.py        Live's own locations (never named paths.py -- see seasonlayer.py)
fixtures/           made-up leagues for exercising the tools before a league has rosters
reports/            generated boards and plans (gitignored)
```

Moved out of `Season/` on 2026-09-25: `Season/` is the backtest simulator and never touches a
platform or the network; this folder does both. It shares `Season/`'s league model through
`seasonlayer.py` (the flat-module convention: Season/ goes on `sys.path`, and no module here may
share a name with one in Season/, Decisions/ or Simulation/ -- checked at import).

```
python draft_assistant.py                                   # follow the draft (league beagles)
python draft_assistant.py --team 63341 --replay-season 2025 # rehearse on the 2025 draft
python draft_assistant.py --league <an espn league>         # follow an ESPN draft (see leagues/)
python draft_assistant.py --league espn --slot 10           # an unreadable draft: type picks
python draft_gui.py                                         # the window
python run_live.py --make-fake                              # fixtures/beagles/fake_league.json
python run_live.py --date 2026-09-29 --league-file fixtures/beagles/fake_league.json --refresh
python run_live.py --date 2026-09-29 --platform-season 2025          # rehearse on 12090's 2025 rosters
python run_live.py --window --refresh                       # in season: reads the league from Fleaflicker
```

The in-season inputs come from the other folders: tonight's projections from
`ModelFeatures/build_tonight.py` + `Projections/project_tonight.py` (run by `--refresh`), the
live injury and lineup reports from `pipeline/snapshot_live.py`.

## The draft board

**The live board**: `python draft_board.py --season 2026-27 --weights points-league` writes
`reports/draft_board_2026-27_league_<scoring>.csv/.md` -- rank, player, team, positions, value, VOR,
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
`scoring/points-league.json` and Fleaflicker's playoff weeks. The simulator never reads the file.
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
`reports/draft_assistant.md`.

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
blank for goalies). Top-300 skaters run from 19% (Kucherov, Draisaitl) to 83% (Lauzon). Shown only; the value is the projections' either way. And **Draft board**, rounds by teams in draft order, coloured by
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
