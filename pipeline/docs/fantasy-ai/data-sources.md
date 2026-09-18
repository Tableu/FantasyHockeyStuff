# Fantasy Hockey AI — lineup & injury data research (2026-09-16/17)

Build plan: https://claude.ai/artifact/Khj9dhdvfa3SB4r1xpKLLi (Rev. 4, H2H points league).
This covers the plan's Section 1 gap: pre-game lineups and injury reports, live and historical.

## Live sources (verified by fetching on 2026-09-16)

| Need | Source | Format | Notes |
|---|---|---|---|
| Forward lines, D pairs, PP1/PP2, PK1/PK2, per-team injuries | Daily Faceoff `https://www.dailyfaceoff.com/teams/{slug}/line-combinations` | JSON in the page's `<script id="__NEXT_DATA__">` → `props.pageProps.combinations` | Each player has `groupIdentifier` (`f1`–`f4`, `d1`–`d3`, `pp1/pp2`, `pk1/pk2`, `g`, `ir`), `injuryStatus`, `gameTimeDecision`; team-level `updatedAt` and `sourceName` (offseason shows "2026 Offseason (Projected)"). 32 fetches/day. robots.txt allows pages, disallows `/api/`. `fantasydataId` = SportsDataIO player id (their upstream; paid option). |
| Starting goalies | Daily Faceoff `https://www.dailyfaceoff.com/starting-goalies/{YYYY-MM-DD}` | Same embedded JSON, `pageProps.data` list of games | `home/awayNewsStrengthName` (Confirmed/Likely/…), `…NewsCreatedAt` (when it became known), source name/URL. **Past dates work** back to at least 2022-01 (2020-02 and earlier return empty). |
| Injury status + IR eligibility | ESPN fantasy pool (already wrapped: `nhl_pipeline/api/espn_fantasy.py`) | JSON | Per player `injuryStatus` ∈ `ACTIVE / DAY_TO_DAY / OUT / INJURY_RESERVE / SUSPENSION`, `injured` flag. 69 flagged on 2026-09-16. |
| Injury detail | ESPN `https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries` | JSON | `status`, `details.fantasyStatus` (`Day-To-Day` / `OUT` / `IR-LT`), `details.returnDate`, comment, ESPN athlete id in `athlete.links`. Returns 403 to browser-spoofed User-Agents; plain client UA works. |
| Official IR / LTIR, est. return | PuckPedia `https://puckpedia.com/injuries` | HTML; each row's `data-content` attr holds `OUT|Knee`, "Estimated Return: …" | robots.txt explicitly allows public-content crawlers. |
| Ground truth: who dressed | NHL API `gamecenter/{id}/boxscore` | JSON | Empty while `gameState == FUT` (checked 2026020001). Expected to populate at `PRE` (~warmups) — verify on first preseason game 2026-09-19 23:00Z (DAL@STL, 2026010001). |
| Fallback | Rotowire `https://www.rotowire.com/hockey/nhl-lineups.php` | HTML | Empty on no-game days; structure unverified. |

Ruled out: NHL API has no injury or pre-game lineup endpoint (`/v1/roster/{team}/current` lists
everyone with no status); Left Wing Lock redirects to an anti-bot `accessCheck.php`; Yahoo is
OAuth-only; Daily Faceoff has no standalone injury page (injuries come from the `ir` group on
each team page).

IR eligibility is platform-defined: ESPN allows only `IR` or `O` tags in the IR slot (DTD not
eligible); Yahoo's IR slot takes IR designations and IR+ takes IR, DTD, or O. Store the raw
status and compute eligibility per the league's platform (not yet confirmed which).

## Historical sources for training (skaters only — goalies excluded from the history effort)

### Injuries: NHL Injury Viz database — IMPORTED (2026-09-17)
- **In the DB:** `Injuries.Spells` (one row per regular-season absence, with SeasonID/TeamID/
  PlayerID, team game numbers *and* the derived StartDate/EndDate/NHLGameIDs), fed by
  `import_injury_history.py` → `nhl_pipeline/ingest/injury_history.py`. Playoff rows are
  skipped (no game numbers). Names resolve via `Injuries.PlayerNameAliases` (local match
  first, NHL player search fallback for players not yet in `Reference.Players`); leftovers
  sit in `Injuries.UnresolvedPlayerNames` for a manual alias. As a by-product the same run
  fills `Reference.Schedule` (and the defunct ATL/PHX/ARI/UTA-59 rows in `Reference.Teams`)
  for every season it touches, via the `club-schedule-season` endpoint.
- Blog: https://nhlinjuryviz.blogspot.com/2015/11/nhl-injury-database.html (Tableau Public embed).
- Download: `https://public.tableau.com/workbooks/NHLinjurydatabase.twb` — actually a `.twbx`
  zip containing `Data/TableauTemp/*.hyper` (720 KB). Read with `pip install tableauhyperapi`;
  table `"Extract"."Extract"`.
- 24,953 injury spells, 2000/01 → 2025/26 (+ playoffs rows), ~900 per regular season; file last
  rebuilt June 2026. Columns: `Team` (city name), `Player` (`Last, First`), `Position` (F/D/G),
  `Injury Type`, `Games Missed`, `Cap Hit`, `CHIP`, `Season` (`2025/26`), plus hidden
  `Start`/`End` = **team game numbers (1–82)** bracketing the absence, `Injury type group`.
  Every regular-season row has `Start`/`End` and satisfies `Games Missed = End − Start + 1`;
  the 1,781 null-`Start` rows are all playoff rows. `Player2` carries a disambiguating
  `(2)`/`(D)` suffix for same-named players; `Position` gains a `"Retired"` suffix on LTIR
  contract dumps (kept, flagged `IsRetiredContract`).
- Scope: injuries and illnesses only — healthy scratches, suspensions, personal absences
  excluded. Compiled from PuckPedia/CBS/TSN/CapFriendly (and sportsforecaster pre-2008/09).
- Caveats: realized absences, not pre-game designations (no DTD/questionable, no IR flag);
  the first game of a spell is the only ambiguous one for lockout-time knowability; needs
  name/team normalization and game-number → date mapping via each team's schedule.

### Pre-game forward lines: 5v5hockey — PARTIAL
- https://5v5hockey.com/hockey/historical-line-matchups/ — archive of Daily Faceoff pre-game
  **forward lines** by date (no D pairs / PP / PK). Free registration; ag-grid behind login,
  described as covering "the current season". Unverified: earliest date, CSV export.
- Value: one season of true pre-game lines to measure how far pre-game lines differ from
  opening lines, and to train/validate the projected-lineup model.

### Rejected
- gamedaytweets.com/lines: @GameDayLines tweet archive; ≤250 most-recent tweets per team
  (~1 season), 91% truncated with "…", free text, image tweets — not parseable history.
  Only useful as a timestamped "line news broke" feed for the live job.
- Wayback Machine captures of Daily Faceoff team pages: ~10–35 captured days per year per
  team — spot checks only.

## Chosen approach for historical lineups: derive opening lines from Game.Shifts

**In the DB (2026-09-17):** `Lineups.GameLineups` (one row per game/team/player: dressed,
F line 1-4, D pair 1-3, PP/PK unit 1-2, starting goalie, P1-EV/5v5/PP/SH seconds), derived by
`nhl_pipeline/calc/lineups.py` as the `LINEUPS` stage of every game ingest and by
`backfill_lineups.py` for games already loaded. `run_daily.py --season YYYYYYYY` backfills a
past season (date range from `Reference.Schedule`). Prototype: `lines_from_shifts.py` (next
to this file). For each team, count seconds each pair of forwards (or defensemen) shares on
ice at 5v5 during the opening window of period 1, greedily cluster into trios/pairs; PP/PK
units the same way over all seconds the team is up/down a skater (strength from the
play-event timeline, since shift charts overlap at every line change).

**Unit rank comes from period-1 EV TOI, not full-game TOI** (revises the note below):
full-game TOI encodes in-game injuries/benchings, i.e. game-N information, into what is used
as a lockout-time feature. Measured on 2025-26: the line *number* changes between consecutive
games 60% of the time under P1 ranking and still 52% under full-game ranking, so line number
is a weak signal either way; trio *membership* (36% of forwards get a new linemate, 20% of D
a new partner) is the stable feature. Structure check: 93% of team-games cluster into four
full trios, 99% into three full pairs; the derived starter is the most-used goalie 97% of the
time (the rest are pulls).

Validation on 2025-26 (`Game.Shifts` already holds all 1,312 games):
- TOR vs ANA 2026-03-12: derived PP1 includes Maccelli and PP2 includes Groulx, matching a
  beat-reporter tweet from that date ("Maccelli moved to PP1, Groulx on PP2").
- CAR 2026-03-20 trios match the four trios reporters posted in June with one winger swap.
- Trio membership identical across 300/600/1200 s windows; only L1–L4 ordering moved, so
  number lines by full-game TOI, not the opening window.
- Handles 11F/7D dressings and absences (Matthews out 2026-03-20 = injury DB games 67–82).

Coverage: NHL shift charts exist from 2010-11, so `run_daily.py --backfill` on older seasons
extends this to 10+ seasons of lines, pairs, and PP units.

Leakage framing: opening lines of game N are actual deployment, not the pre-game card. Use
them (1) as the **label** for a projected-lineup model, and (2) as a near-equivalent of the
live Daily Faceoff feature, quantifying the gap on 2025-26 via 5v5hockey.

## Agreed design directions

**Training feature for the projection models (decided 2026-09-17): variant B primary,
variant A control, projected-lineup model deferred.** The training feature must have the same
error distribution as the live feature (Daily Faceoff's pre-game chart, which differs from the
opening lineup only by late scratches / a winger swap / PP changes):
- **B** = actual opening lineup of game N through `perturb()` (structured, calibrated noise).
  Matches live best; the failure mode is under-calibrated noise → the model over-trusts
  lineup features → backtest overstates live. Guarded by calibrating from an over-estimate.
- **A** = previous game's opening lineup. Leak-proof, noisier than live → the model
  under-weights lineup info (safe direction). Kept as the control: train on A and on B,
  score the B-model on A-features; the gap bounds the bet on the live feed's quality.
- **C** = projected-lineup model: a smarter A; its job is P(plays) and the DFO-outage
  fallback, not the training feature. Not built yet.

Built: `nhl_pipeline/lineups/{store,calibration,perturb,features}.py` and
`build_lineup_features.py` → `data/lineups/features_{A|B}_{season}.parquet` (one row per
game/team/lockout-knowable candidate: dressed in the team's last 10 games ∪ injured for the
team that day ∪ actually dressed; `feat_*` columns from the variant's source lineup,
`label_*` from the actual one, linemate ids for feature lookups). Leakage is asserted in
code: A only reads games dated before the target; B's healthy-extras pool is the lookback
pool minus `Injuries.Spells` — never a later game.

**Noise calibration, staged:** `calibration.measure_churn` measures game N-1 → N churn from
`GameLineups` (2025-26: scratch 4.2% of healthy skaters, new linemate 36% F / 20% D, PP unit
change 22%, PK 36%) and `perturb_rates` turns it into per-game edit counts; these are a
deliberate over-estimate of chart error and get replaced by the measured DFO-vs-opening
discrepancy once the live snapshot job has run (`build_lineup_features.py --rates`). The
goalie rate is a fixed 10% judgment call (consecutive-game starter churn is rotation, not
chart error). The churn → edit-count mapping is approximate: membership-based rates land
within ~10-20% of target, rank-based ones ~35% under (adjacent reorders cancel).

**Projected-lineup model:** predicts, as of lockout for game N, P(dresses) per rostered skater
and unit assignment (trio / pair / PP unit). Labels = opening lines of game N. Features (all
pre-game): last game's opening lines and ~5-game unit history (line number, PP unit, TOI share,
trio tenure), injury status at lockout + roster events, rest/back-to-back/home/opponent, team
churn tendency. Build order: persistence baseline (copy last lines, drop injured, promote
extra, re-rank) → learned P(dresses) classifier + pairwise "still linemates?" model reassembled
with the same greedy clustering → evaluate on held-out seasons vs baseline and vs Daily Faceoff
(2025-26). Used in backtests as the lineup feature; live, DFO lines override unit assignments
and the model supplies P(plays) and the ingest-failure fallback.

## Next steps
1. ~~`GameLineups` derivation~~ — done 2026-09-17 (`Lineups.GameLineups`, feature variants A/B).
2. ~~`import_injury_history.py`~~ — done 2026-09-17. Two names remain unresolved (Tommy
   Westlund, Scott Thomas: not in the NHL search index).
3. Backfill older seasons: `python run_daily.py --season YYYYYYYY` (~45 min each; 2024-25 done
   as the smoke test), then `backfill_lineups.py --season YYYY-YY` if the season was loaded
   before the LINEUPS stage existed. Shift charts exist from 2010-11.
4. Daily lockout snapshot job for the live sources above (`Lineups.*` tables keyed by
   game/date/snapshot time), after confirming the league's hosting platform.
