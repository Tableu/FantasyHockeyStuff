# Desktop

The draft tools, set aside here when the plan window retired (2026-10-24) and kept for the next
drafts. Everything else the desktop windows did is the app's now (Server/app/, phone or PC browser).

```
draft_assistant.py  draft night in the terminal: follows the live draft (Fleaflicker), or --standalone
draft_gui.py        the same in a window (tkinter + tksheet)
sheets.py           the window's read-only tksheet tables: row colours by tag, header-click sorting
serverlayer.py      the one bridge into the engine (Server/: leagues, platforms, draft_board, ...) and
                    through it Season/ -- the flat-module convention, names checked at import
requirements.txt    the desktop packages (tksheet) on top of the engine's
```

The board they show is the engine's `draft_board.py` (Server/), and the latest view is written to
`Server/reports/<league>/draft_assistant.md`.

```
python draft_assistant.py                                   # follow the draft (league beagles)
python draft_assistant.py --team 63341 --replay-season 2025 # rehearse on the 2025 draft
python draft_assistant.py --league <an espn league>         # an ESPN draft, once it is over (see below)
python draft_gui.py --league espn-la --standalone --slot 1  # a live ESPN draft: double-click every pick
python draft_assistant.py --league espn --slot 10           # an unreadable draft: type picks
python draft_gui.py                                         # the window
```

**ESPN drafts cannot be followed live.** ESPN's read API (`mDraftDetail`) lists every pick slot
with no player until the draft is over (espn-la, 2026-09-26: 0 picks at 2.5 minutes in, all 220
the moment it finished), and ESPN re-draws the pick order when the draft starts. So a live ESPN
draft runs `--standalone --slot <your slot once the order is drawn>`, every pick double-clicked;
the adapter reads the finished draft fine.


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
