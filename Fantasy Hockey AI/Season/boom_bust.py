#!/usr/bin/env python
"""Boom and bust odds, measured against the pick you spend on a player.
Plan: ~/.claude/plans/boom-bust-odds.md, step 3.

    python boom_bust.py --season 2026-27 --draft-date 2026-09-26          # the live board's odds
    python boom_bust.py --season 2025-26 --draft-date 2025-10-07          # a backtest (step 4)

Three pieces:

1. **The room.** `--drafts` simulated drafts with every seat a realistic opponent
   (`Settings/field.json`, `source_subsets`: each seat drafts by value over replacement on the
   consensus of 1-3 random projection sources, at most `opponent_max_goalies` goalies). No ADP --
   ADP is built for other platforms' formats; it only grades these odds (step 4).
2. **The seasons.** `--draws` whole seasons for every projected player
   (Simulation/season_spread.py): games from the games-played model, per-game scoring from the
   consensus rates with a league-wide and a personal shock.
3. **The par curve.** Draw d is paired with room draft d mod R. In it, each position's
   replacement is the realized points of the three best-*projected* players nobody drafted (what
   the wire offers on opening day, as it turned out), and a player's realized VOR is his points
   minus the lowest replacement among his positions -- the board's own rule. par(p) is the mean
   realized VOR of whoever went at pick p, smoothed to never rise.

Then, for a player taken at pick p (T = one round):

    bust  = P(realized VOR <  par(p + T))    worth less than what the next round usually returns
    boom  = P(realized VOR >= par(p - T))    worth what the round before usually returns

The board reports them at each player's median pick in the simulated room (`room_pick`); the
draft assistant reads the saved draws and asks at your next pick instead.

Writes reports/boom_bust_<season>_<scoring>.parquet (per player), ..._par.csv (the curve) and
..._vor_draws.npy + ..._players.csv (the realized-VOR draws, for any other pick).
"""

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import field as field_module
import inputs
import league as league_module
import paths
import simlayer  # noqa: F401 -- puts Simulation/ on sys.path
import state as state_module
import draftroom
from decisionlayer import draft as draft_module
from decisionlayer import load_strategy

import games_played
import season_spread

log = logging.getLogger("boom_bust")

REPLACEMENT_DEPTH = 3          # undrafted players averaged for a position's realized replacement
POSITION_CODES = {"LW": "L", "RW": "R"}
SIM_REPORTS = paths.SIMULATION_DIR / "reports"
FALLBACK_DEFAULT = {"ratio": 0.6, "precision": 1.0, "players": 0}   # no consensus history yet


def previous(season: str) -> str:
    first = int(season[:4]) - 1
    return f"{first}-{str(first + 1)[2:]}"


# --- the board and the room ---------------------------------------------------------------------

def board_inputs(season, prior, config, scoreset, strategy, draft_date):
    external = inputs.load_external_projections(season, draft_date, strategy.undated_sources)
    last = draft_module.prior_season_board(inputs.load_actuals(prior),
                                           inputs.load_goalie_starts(prior), scoreset)
    last.index = last.index.astype(int)
    values = draft_module.values_for("consensus", scoreset, last, external=external,
                                     min_sources=strategy.vor_min_sources)
    players = pd.read_parquet(paths.players()).set_index("player_id")
    universe = (players.loc[players.index.intersection(values.index), ["position"]]
                .rename_axis("player_id").reset_index())
    universe["position"] = universe["position"].replace(POSITION_CODES)
    eligibility = inputs.load_eligibility(config, universe)
    values = values[[p in eligibility for p in values.index]]
    return external, last, values, eligibility


def room_drafts(external, last, values, eligibility, config, scoreset, field, drafts: int):
    """[{player_id: overall pick}] for `drafts` all-opponent rooms."""
    cache = {}
    names = external["source"].unique()
    out = []
    for r in range(drafts):
        boards = {}
        for seat in range(config.teams):
            sources = field_module.draw_sources(names, field.sources_per_opponent, r, seat)
            if sources not in cache:
                v = draft_module.source_board(external, sources, last, scoreset)
                cache[sources] = draft_module.vor_board(v[[p in eligibility for p in v.index]],
                                                        config, eligibility)
            boards[seat] = cache[sources]
        state = state_module.LeagueState(config, sorted(eligibility), eligibility)
        draftroom.run(state, config, values, eligibility, replication=r, boards=boards,
                      goalie_caps={seat: field.max_goalies for seat in range(config.teams)})
        order = draftroom.seat_order(config, r)
        picks = {}
        for rnd in range(config.roster_size):
            seats = order if (rnd % 2 == 0 or config.draft["type"] != "snake") else order[::-1]
            for k, seat in enumerate(seats):
                roster = state.teams[seat].roster
                if rnd < len(roster):
                    picks[roster[rnd]] = rnd * config.teams + k + 1
        out.append(picks)
        if (r + 1) % 25 == 0:
            log.info("room drafts: %d of %d (%d opponent boards built)", r + 1, drafts, len(cache))
    return out


# --- the seasons --------------------------------------------------------------------------------

def _consensus_history(season, totals, players, teams, before_frames):
    """(consensus games, actual games) for players outside the games-played pool, in the
    seasons before `season` that have consensus projections."""
    history = []
    for s, frame in before_frames:
        acts = totals[(totals["season"] == s) & ~totals["is_goalie"].astype(bool)]
        history.append((frame.loc[~frame["in_pool"], "cons_games"],
                        acts.set_index("player_id")["gp"]))
    return history


def season_draws(season, scoreset_weights, draws, seed):
    """Fantasy points and games (draws x players) for every projected skater and goalie."""
    feats = paths.FEATURES_DIR
    totals, injury, team_games, players = games_played.load_inputs(feats)
    teams = pd.read_parquet(paths.teams())
    rate_fit = json.loads((SIM_REPORTS / f"rate_error_{season}.json").read_text())
    gp_sk = json.loads((SIM_REPORTS / f"games_played_skaters_{season}.json").read_text())
    gp_g = json.loads((SIM_REPORTS / f"games_played_goalies_{season}.json").read_text())
    for fit in (rate_fit, gp_sk, gp_g):
        trained = fit.get("seasons") or []
        if season in trained or fit.get("for_season", season) != season:
            raise AssertionError(f"a fit for {season} was trained on it: {fit.get('for_season')}")

    external = pd.read_parquet(paths.external_projections(season))
    cons_sk = season_spread.consensus_skaters(external, teams, min_sources=1)
    frame_sk = season_spread.skater_frame(season, totals, injury, team_games, players, cons_sk,
                                          rate_fit["marcel_k"])
    earlier = sorted(p.name[len("external_projections_"):-len(".parquet")]
                     for p in feats.glob("external_projections_*.parquet"))
    earlier = [s for s in earlier if s < season]
    before = []
    for s in earlier:
        ext = pd.read_parquet(paths.external_projections(s))
        fit_s = json.loads((SIM_REPORTS / f"rate_error_{s}.json").read_text())
        before.append((s, season_spread.skater_frame(
            s, totals, injury, team_games, players,
            season_spread.consensus_skaters(ext, teams, min_sources=1), fit_s["marcel_k"])))
    fallback = (season_spread.fit_fallback(_consensus_history(season, totals, players, teams,
                                                              before))
                if before else FALLBACK_DEFAULT)
    log.info("skater games outside the model's pool: %s (from %s)", fallback,
             earlier or "the default: no earlier consensus")

    rng = np.random.default_rng(seed)
    pts_sk, gp_sk_draws = season_spread.sample_skaters(frame_sk, gp_sk, rate_fit, fallback,
                                                       scoreset_weights["skaters"], draws, rng)

    cons_g = season_spread.consensus_goalies(external, scoreset_weights["goalies"], min_sources=1)
    frame_g = season_spread.goalie_frame(season, totals, injury, team_games, players, cons_g)
    shock = season_spread.fit_goalie_shock(totals, scoreset_weights["goalies"], season)
    pts_g, gp_g_draws = season_spread.sample_goalies(frame_g, gp_g, shock, FALLBACK_DEFAULT,
                                                     draws, rng)
    index = pd.Index(list(frame_sk.index) + list(frame_g.index), name="player_id")
    if index.has_duplicates:
        raise AssertionError("a player is both a skater and a goalie in the consensus")
    return (np.hstack([pts_sk, pts_g]), np.hstack([gp_sk_draws, gp_g_draws]), index,
            {"skater_fallback": fallback, "goalie_shock": shock})


# --- par and the odds ---------------------------------------------------------------------------

def _non_increasing(y: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators for a non-increasing fit, equal weights."""
    blocks = [[float(v), 1] for v in y]
    out = []
    for v, w in blocks:
        out.append([v, w])
        while len(out) > 1 and out[-2][0] < out[-1][0]:
            v2, w2 = out.pop()
            v1, w1 = out.pop()
            out.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2])
    return np.concatenate([[v] * w for v, w in out])


def realized_vor(points, index, values, eligibility, rooms):
    """VOR (draws x players): points minus the lowest realized replacement among a player's
    positions, replacement read from the room paired with each draw."""
    col = {p: j for j, p in enumerate(index)}
    positions = draft_module.POSITIONS
    by_value = [p for p in values.sort_values(ascending=False).index if p in col]
    elig_mask = np.zeros((len(index), len(positions)), dtype=bool)
    for p, j in col.items():
        for k, pos in enumerate(positions):
            elig_mask[j, k] = pos in eligibility.get(p, ())
    vor = np.empty_like(points)
    for r, picks in enumerate(rooms):
        draws = np.arange(r, points.shape[0], len(rooms))
        if not len(draws):
            continue
        rep = np.zeros((len(draws), len(positions)), dtype=np.float32)
        for k, pos in enumerate(positions):
            left = [col[p] for p in by_value if p not in picks and pos in eligibility.get(p, ())]
            rep[:, k] = points[np.ix_(draws, left[:REPLACEMENT_DEPTH])].mean(axis=1) if left else 0
        # The lowest replacement among each player's positions (the board's rule).
        big = np.where(elig_mask[None, :, :], rep[:, None, :], np.inf)
        floor = big.min(axis=2)
        floor[~np.isfinite(floor)] = 0.0
        vor[draws] = points[draws] - floor
    return vor


def par_curve(vor, index, rooms, picks_total) -> np.ndarray:
    col = {p: j for j, p in enumerate(index)}
    sums = np.zeros(picks_total)
    counts = np.zeros(picks_total)
    for r, picks in enumerate(rooms):
        draws = np.arange(r, vor.shape[0], len(rooms))
        for p, k in picks.items():
            if p in col:
                sums[k - 1] += vor[draws, col[p]].sum()
                counts[k - 1] += len(draws)
    raw = sums / np.maximum(counts, 1)
    return _non_increasing(raw)


def odds_at(vor_column: np.ndarray, pick: float, par: np.ndarray, margin: int) -> tuple:
    """(boom, bust) for one player's realized-VOR draws, taken at `pick` (1-based)."""
    p = int(round(pick))
    later = par[min(p + margin, len(par)) - 1]
    earlier = par[max(p - margin, 1) - 1]
    return float((vor_column >= earlier).mean()), float((vor_column < later).mean())


# --- CLI ----------------------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", required=True)
    parser.add_argument("--prior-season", default=None)
    parser.add_argument("--rules", default="league", help="Settings/rosters file (default league)")
    parser.add_argument("--scoring", default="points-league")
    parser.add_argument("--eligibility", default="fleaflicker", help="Positions platform")
    parser.add_argument("--draft-date", required=True, help="Sources published before this")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--field", default=None, help="Settings/field file (default field.json)")
    parser.add_argument("--drafts", type=int, default=200)
    parser.add_argument("--draws", type=int, default=5000)
    parser.add_argument("--margin", type=int, default=None, help="Picks (default: one round)")
    parser.add_argument("--seed", type=int, default=20260926)
    args = parser.parse_args()

    from dataclasses import replace

    config = league_module.load(args.rules)
    config = replace(config, eligibility_platform=args.eligibility.lower())
    scoreset = simlayer.load_scoreset(args.scoring)
    weights = json.loads(paths.scoreset(args.scoring).read_text())
    strategy = load_strategy(args.strategy)
    field = field_module.load(args.field)
    if field.opponent_board != "source_subsets":
        raise SystemExit("the room needs source_subsets opponents (Settings/field.json)")
    prior = args.prior_season or previous(args.season)
    margin = args.margin or config.teams

    external, last, values, eligibility = board_inputs(args.season, prior, config, scoreset,
                                                       strategy, pd.Timestamp(args.draft_date))
    vor_board = draft_module.vor_board(values, config, eligibility)
    rooms = room_drafts(external, last, values, eligibility, config, scoreset, field,
                        args.drafts)

    points, games, index, fits = season_draws(args.season, weights, args.draws, args.seed)
    missing = [p for r in rooms for p in r if p not in set(index)]
    if missing:
        log.warning("%d drafted player-slots have no season draws (no source projects them); "
                    "they count at zero", len(missing))
    vor = realized_vor(points, index, values, eligibility, rooms)
    par = par_curve(vor, index, rooms, config.teams * config.roster_size)

    picks = pd.DataFrame([{p: k for p, k in r.items()} for r in rooms]).T
    room_pick = picks.median(axis=1)
    drafted_share = picks.notna().mean(axis=1)
    rows = []
    for j, p in enumerate(index):
        pick = room_pick.get(p, np.nan)
        boom, bust = odds_at(vor[:, j], pick, par, margin) if pd.notna(pick) else (np.nan, np.nan)
        q = np.percentile(points[:, j], [10, 50, 90])
        rows.append({"player_id": p, "room_pick": pick, "drafted_share": drafted_share.get(p, 0.0),
                     "boom_pct": boom, "bust_pct": bust, "mean": points[:, j].mean(),
                     "p10": q[0], "p50": q[1], "p90": q[2], "exp_gp": games[:, j].mean(),
                     "vor_board": vor_board.get(p, np.nan)})
    out = pd.DataFrame(rows).set_index("player_id").sort_values("room_pick")

    stem = f"boom_bust_{args.season}_{args.scoring}"
    reports = paths.REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    out.to_parquet(reports / f"{stem}.parquet")
    pd.DataFrame({"pick": np.arange(1, len(par) + 1), "par": par}).to_csv(
        reports / f"{stem}_par.csv", index=False)
    np.save(reports / f"{stem}_vor_draws.npy", vor.astype(np.float32))
    pd.Series(index, name="player_id").to_csv(reports / f"{stem}_players.csv", index=False)
    (reports / f"{stem}_meta.json").write_text(json.dumps({
        "season": args.season, "scoring": args.scoring, "drafts": args.drafts,
        "draws": args.draws, "margin": margin, "draft_date": args.draft_date,
        "seed": args.seed, **fits}, indent=1, default=float))
    names = pd.read_parquet(paths.players()).set_index("player_id")["name"]
    show = out.head(40).assign(player=names.reindex(out.head(40).index))
    print(show[["player", "room_pick", "boom_pct", "bust_pct", "mean", "p10", "p90",
                "exp_gp"]].round(2).to_string())
    print("par (every 14th pick):", np.round(par[::config.teams], 1).tolist())
    log.info("-> %s", reports / f"{stem}.parquet")


if __name__ == "__main__":
    main()
