"""Loading a season's inputs, and refusing the ones that would produce a meaningless number.

Four tables make a simulated season, and each arrives from a sibling folder by path:

    skater projections   what a manager could know about tonight     (out of sample)
    skater actuals       what happened                               (target_*)
    goalie starts        what happened in goal                       (per dressed goalie)
    availability         who was unavailable, and stayed unavailable (injured_at_lockout)

The one that needs guarding is the first. `Projections/reports/lambdas_<season>_<variant>.
parquet` is produced by whichever boosters `predict.py` is pointed at (the deployment build by default),
and right now those are the **deployment** build, trained on all three seasons including the
one this simulator replays. Handing that table to the full-system manager and a naive
season-to-date rate to the streamer does not compare a modelling stack against a streaming
strategy; it compares a manager who has seen the season's results against one who has not, and
the full system wins for a reason that says nothing about either. `load_projections` therefore
takes the *holdout* build's scored season -- `predictions_A.parquet`, fit on 2023-24 and
2024-25 -- and renames its `pred_*` columns into the `lambda_*` names the sampler expects.

That rename is not cosmetic: `predict.py` applies two clamps after the boosters run, and they
have to be re-applied here or the sampler is handed impossible parameters.
"""

import logging

import numpy as np
import pandas as pd

import paths

log = logging.getLogger("inputs")

COUNT_CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim"]

# pred_* -> the lambda-table names Simulation/sampler.py requires.
PREDICTION_RENAMES = {
    "pred_plays": "p_plays",
    "pred_toi": "toi",
    "pred_ev_toi": "ev_toi",
    "pred_pp_toi": "pp_toi",
    "pred_pp_point_share": "pp_point_share",
    # Required since the variant-A holdout was rebuilt (2026-09-23). The old predictions_A
    # predated the short-handed-point model, so this column used to be optional and zero-filled
    # -- 0.38% of skater points, understated for every manager alike.
    "pred_sh_point_share": "sh_point_share",
    **{f"pred_{c}": f"lambda_{c}" for c in COUNT_CATEGORIES},
}

KEY_COLUMNS = ["season_id", "game_id", "game_date", "team_id", "player_id", "position"]
TARGET_CATEGORIES = ["shots", "hits", "blocks", "assists", "goals", "pim", "points",
                     "ppp", "shp"]


class ProvenanceError(RuntimeError):
    """Raised when an input would make the measurement unfalsifiable."""


def load_projections(season: str, variant: str = "A") -> pd.DataFrame:
    """The skater lambda table for a season, out of sample, with `predict.py`'s clamps.

    Returns exactly the columns `Simulation/sampler.py:REQUIRED_COLUMNS` asks for, plus keys.
    """
    path = paths.holdout_predictions(season, variant)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is written by `Projections/train.py --all` followed by "
            f"`evaluate.py`, i.e. a build that HOLDS OUT {season}. Do not substitute "
            f"lambdas_{season}_{variant}.parquet -- see this module's docstring.")
    table = pd.read_parquet(path)

    missing = set(PREDICTION_RENAMES) - set(table.columns)
    if missing:
        raise ProvenanceError(f"{path} is missing {sorted(missing)}; it does not look like a "
                              f"scored holdout from Projections/evaluate.py")
    _assert_out_of_sample(table, season, path)

    keep = [c for c in KEY_COLUMNS if c in table.columns]
    out = table[keep].join(table[list(PREDICTION_RENAMES)].rename(columns=PREDICTION_RENAMES))
    out["game_date"] = pd.to_datetime(out["game_date"])

    # The two clamps predict.py applies after the boosters (predict.py:120-128). Without them
    # a negative lambda or a pp+sh share above 1 reaches the sampler, which will draw an
    # impossible strength split rather than complain.
    out["p_plays"] = out["p_plays"].clip(0.0, 1.0)
    for column in ["toi", "ev_toi", "pp_toi"] + [f"lambda_{c}" for c in COUNT_CATEGORIES]:
        out[column] = out[column].clip(lower=0.0)
    power_play = out["pp_point_share"].clip(0.0, 1.0)
    out["pp_point_share"] = power_play
    out["sh_point_share"] = np.minimum(out["sh_point_share"].clip(0.0, 1.0), 1.0 - power_play)

    log.info("projections: %d rows, %d games, %d skaters, %s..%s (holdout build, %s)",
             len(out), out["game_id"].nunique(), out["player_id"].nunique(),
             out["game_date"].min().date(), out["game_date"].max().date(), path.name)
    return out


def _assert_out_of_sample(table: pd.DataFrame, season: str, path) -> None:
    """One season's worth of rows, and every row carrying a realized target.

    A scored holdout has exactly the holdout season in it. The deployment build's predictions
    cover every season it trained on, so a row count spanning several seasons is the tell --
    and `season_id` is recoded to 1 in these files, so it cannot be checked by value.
    """
    if "target_played" not in table.columns:
        raise ProvenanceError(
            f"{path} carries no target_* columns, so it cannot be a scored holdout. A file of "
            f"projections with no realized outcomes is a deployment prediction, which for "
            f"{season} would be in-sample.")
    days = pd.to_datetime(table["game_date"])
    span = (days.max() - days.min()).days
    if span > 400:
        raise ProvenanceError(
            f"{path} spans {span} days ({days.min().date()}..{days.max().date()}), i.e. more "
            f"than one season. A scored holdout covers the held-out season only; this looks "
            f"like a deployment build, which would be in-sample on {season}.")
    _assert_in_season(days, season, path)


def season_window(season: str):
    """July 1 of a season's first year through June 30 of the next: every game of "2024-25"
    falls inside it, and no game of any other season does."""
    first = int(season.split("-")[0])
    return pd.Timestamp(f"{first}-07-01"), pd.Timestamp(f"{first + 1}-06-30")


def _assert_in_season(days, season: str, path) -> None:
    """Every row is dated inside the season asked for.

    The span check above cannot see this: a clean one-season file for the WRONG season passes it.
    That is not hypothetical -- with one unkeyed predictions file, `load_projections("2024-25")`
    returned the 2025-26 holdout, which would have tuned section 11 on the final holdout.
    """
    start, end = season_window(season)
    days = pd.to_datetime(days)
    outside = (days < start) | (days > end)
    if outside.any():
        raise ProvenanceError(
            f"{path} has {int(outside.sum())} of {len(days)} rows dated outside {season} "
            f"({start.date()}..{end.date()}): {days.min().date()}..{days.max().date()}. It "
            f"belongs to another season.")


def load_ros(season: str):
    """Rest-of-season projections for the replayed season, from a build that never saw it.

    Optional: returns None when the file has not been built, and a manager that needs it says so.
    Three checks, because the deployment build sits one directory over and would produce the same
    columns: every row carries its realized window (`target_*`), the table spans one season, and
    the seasons it was trained on do not include this one.
    """
    path = paths.ros_predictions(season)
    if not path.exists():
        log.info("no rest-of-season projections at %s; managers that need them will refuse", path)
        return None
    table = pd.read_parquet(path)
    if "target_games" not in table.columns:
        raise ProvenanceError(f"{path} carries no realized windows, so it cannot be a scored "
                              f"holdout build.")
    _assert_out_of_sample(table.rename(columns={"target_games": "target_played"}), season, path)
    trained_on = set(str(table["trained_on"].iloc[0]).split(","))
    if season in trained_on:
        raise ProvenanceError(f"{path} was trained on {sorted(trained_on)}, which includes "
                              f"{season}: in-sample on the season being replayed.")
    table["game_date"] = pd.to_datetime(table["game_date"])
    out = table[[c for c in table.columns if not c.startswith("target_")]]
    log.info("rest-of-season: %d rows, %d players, trained on %s (%s)", len(out),
             out["player_id"].nunique(), sorted(trained_on), path.name)
    return out


def load_external_projections(season: str, opener, undated: str) -> pd.DataFrame:
    """The external sources' preseason projections for `season`, one row per (source, player).

    Refused unless every dated source was published before `opener`, the season's first game: a
    projection sheet refreshed in November has seen October, and a draft board built from it knows
    who got hot. `undated` ("include" or "exclude", strategy: draft.undated_sources) decides the
    sources with no publish date -- Dom's 2025-26 sheet -- and is logged either way, never silent.
    """
    path = paths.external_projections(season)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `python build_external_projections.py "
                                f"--season {season}` in ModelFeatures/.")
    table = pd.read_parquet(path)
    published = pd.to_datetime(table["published_on"])
    opener = pd.Timestamp(opener).normalize()
    late = sorted(table.loc[published >= opener, "source"].unique())
    if late:
        raise ProvenanceError(f"{path}: {late} published on or after {season}'s first game "
                              f"({opener.date()}) -- a board from them knows the season.")
    undated_sources = sorted(table.loc[published.isna(), "source"].unique())
    if undated_sources and undated == "exclude":
        table = table[published.notna()]
    elif undated not in ("include", "exclude"):
        raise ValueError(f"undated sources: {undated!r}; use include or exclude")
    log.info("external projections: %d sources, %d rows, published by %s; undated %s %s",
             table["source"].nunique(), len(table), published.max().date(),
             "included:" if undated == "include" else "excluded:", undated_sources or "none")
    return table.reset_index(drop=True)


def load_actuals(season: str) -> pd.DataFrame:
    """The realized skater line per player-game -- what a lineup actually scored."""
    table = pd.read_parquet(paths.base_table(season),
                            columns=KEY_COLUMNS + ["target_played"]
                            + [f"target_{c}" for c in TARGET_CATEGORIES])
    table["game_date"] = pd.to_datetime(table["game_date"])
    log.info("actuals: %d rows, %.1f%% dressed", len(table), 100 * table["target_played"].mean())
    return table


def load_goalie_starts(season: str) -> pd.DataFrame:
    """The realized per-start goalie line, already named for the scoreset's goalie block."""
    path = paths.goalie_starts(season)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- run `python build_goalie_starts.py --season {season}` in "
            f"ModelFeatures/. It is the only input here that needs the database.")
    table = pd.read_parquet(path)
    table["game_date"] = pd.to_datetime(table["game_date"])
    log.info("goalie starts: %d dressed rows, %d starts, %d pulls over %d games",
             len(table), int(table["is_starter"].sum()), int(table["pulled"].sum()),
             table["game_id"].nunique())
    return table


def load_availability(season: str, variant: str = "A") -> pd.DataFrame:
    """Who was unavailable on a given night, skaters and goalies together.

    `injured_at_lockout` is the persistent absence layer, and it is what makes IR a lever and
    streaming worth anything. Without it availability is a memoryless per-night Bernoulli, so
    nobody is ever out for six weeks and injury replacement -- most of real streaming -- does
    not exist. Measured on 2025-26: the flag is set on 12.6% of skater rows and 10.7% of goalie
    rows, and 99.97% of flagged skater rows did not dress. It is effectively a hard gate, which
    is why it is treated as exogenous and identical across replications: it is the real
    season's injury history, and nothing a fantasy manager does changes it.

    A manager sees only TODAY's flag, never the length of the spell -- which is close to what a
    real manager reads off a morning injury report. Only the first game of a spell is genuinely
    ambiguous at lockout time.
    """
    goalies = pd.read_parquet(paths.lineup_features(season, variant),
                              columns=["game_id", "team_id", "player_id", "position",
                                       "injured_at_lockout", "label_starting_goalie"])
    goalies = goalies[goalies["position"] == "G"].drop(columns="label_starting_goalie")
    skaters = pd.read_parquet(paths.skater_features(season, variant),
                              columns=["game_id", "team_id", "player_id", "position",
                                       "injured_at_lockout"])
    table = pd.concat([skaters, goalies], ignore_index=True)
    table["injured_at_lockout"] = table["injured_at_lockout"].fillna(False).astype(bool)
    table = table.drop_duplicates(["game_id", "player_id"])
    log.info("availability: %d rows, %.1f%% flagged unavailable",
             len(table), 100 * table["injured_at_lockout"].mean())
    return table


def load_goalie_candidates(season: str, variant: str = "A") -> pd.DataFrame:
    """The goalie universe: who was a plausible starter, and who actually started.

    `label_starting_goalie` is an OUTCOME and must never reach a manager -- `view.py` is what
    keeps it out. It is here because the engine resolves the night with it.
    """
    table = pd.read_parquet(paths.lineup_features(season, variant),
                            columns=["season_id", "game_id", "game_date", "team_id", "player_id",
                                     "position", "injured_at_lockout", "feat_starting_goalie",
                                     "label_starting_goalie"])
    table = table[table["position"] == "G"].copy()
    table["game_date"] = pd.to_datetime(table["game_date"])
    for column in ("feat_starting_goalie", "label_starting_goalie", "injured_at_lockout"):
        table[column] = table[column].fillna(False).astype(bool)
    team_games = table.groupby(["game_id", "team_id"]).ngroups
    labelled = int(table["label_starting_goalie"].sum())
    log.info("goalie candidates: %d rows over %d team-games (%.2f per team-game), "
             "%d with a labelled starter", len(table), team_games,
             len(table) / max(team_games, 1), labelled)
    if labelled < team_games:
        log.warning("%d team-games have no derived starting goalie; the engine falls back to "
                    "the goalie-start export's IsStarter flag for those",
                    team_games - labelled)
    return table


def load_p_start(season: str):
    """The fitted P(start) per goalie per team-game, if it has been built.

    Optional by design: rungs 1 to 3 do not read it, so the ladder still runs without it and says
    so rather than failing. Rung 4 does read it, and without it rung 4 would fall back to the same
    box-score share rung 3 uses, which would quietly remove half of what is being tested.
    """
    path = paths.PROJECTIONS_REPORTS / f"goalie_pstart_{season}.parquet"
    if not path.exists():
        log.warning("%s not found -- rung 4 will fall back to rung 3's naive start share. Build "
                    "it with `python goalie_starts.py --train --save --predict --season %s` in "
                    "Projections/.", path.name, season)
        return None
    table = pd.read_parquet(path)
    table["game_date"] = pd.to_datetime(table["game_date"])
    _assert_in_season(table["game_date"], season, path)
    log.info("p_start: %d goalie rows, mean %.4f (%s)", len(table), table["p_start"].mean(),
             path.name)
    return table


def load_goalie_history(season: str) -> pd.DataFrame:
    """Every season's per-start goalie lines BEFORE `season`, for the numbers a manager may know
    about goalies in general -- the league-average line. The replayed season's own starts are its
    outcomes, and averaging them hands every manager 2025-26's goalie scoring before it happened.
    """
    first = int(season[:4])
    earlier = [f"{y}-{str(y + 1)[2:]}" for y in range(first - 10, first)]
    frames = [load_goalie_starts(s) for s in earlier if paths.goalie_starts(s).exists()]
    if not frames:
        raise FileNotFoundError(f"no goalie starts before {season} -- the league-average goalie "
                                f"line needs at least one earlier season")
    table = pd.concat(frames, ignore_index=True)
    if (table["game_date"] >= pd.Timestamp(f"{first}-07-01")).any():
        raise AssertionError(f"goalie history for {season} reaches into {season} itself")
    return table


def load_season(season: str, variant: str = "A") -> dict:
    """Everything a run needs, loaded once and shared across every manager and replication."""
    return {
        "season": season,
        "projections": load_projections(season, variant),
        "actuals": load_actuals(season),
        "goalie_starts": load_goalie_starts(season),
        "goalie_history": load_goalie_history(season),
        "goalie_candidates": load_goalie_candidates(season, variant),
        "availability": load_availability(season, variant),
        "p_start": load_p_start(season),
        "ros": load_ros(season),
    }


def _side(slot: str) -> str:
    """Goalie, defence or forward: the three kinds of player a slot code can belong to."""
    return slot if slot in ("G", "D") else "F"


def load_eligibility(config, universe: pd.DataFrame) -> dict:
    """{player_id: frozenset of slot codes} -- which slots each player may legally fill.

    Eligibility is the platform's, not the NHL's, and it is what makes the nightly lineup an
    assignment problem instead of a sort (see `league.py`). Two things about the source are worth
    stating rather than burying:

    **It exists for 2026-27 only**, and the backtest replays 2025-26. That is acceptable for
    eligibility in a way it is emphatically not for ADP. Eligibility only widens which lineups are
    legal, it widens them for every manager at once, and a player's eligible positions are close
    to stable year over year. A 2026-27 *draft board*, by contrast, encodes how players performed
    in 2025-26 -- the season being replayed -- so drafting from it would hand the field a season
    of hindsight. Hence eligibility here, and a prior-season ranking in `Decisions/draft.py`.

    **Coverage is 97.8% and the rest fall back to their NHL position.** A fallback player is
    strictly no worse off than under single-position eligibility, so the gap cannot flatter
    anyone.

    Cross-side rows are rejected, not mapped. Yahoo 2026-27 lists one C/D and ESPN one C/G; a
    skater cannot be a goalie and vice versa, so those are name-resolution collisions in the
    import rather than generous eligibility, and honouring one would let a skater fill a G slot.
    """
    import league as league_module

    path = paths.fantasy_positions(config.eligibility_platform, config.eligibility_season)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- run `python build_fantasy_positions.py --platform "
            f"{config.eligibility_platform} --season {config.eligibility_season}` in "
            f"ModelFeatures/.")
    listed = pd.read_parquet(path)

    known = set(league_module.SKATER_POSITIONS) | {league_module.GOALIE_POSITION}
    unknown = set(listed["fantasy_position"]) - known
    if unknown:
        raise ValueError(f"{path} carries slot codes {sorted(unknown)} this league has no name "
                         f"for; known: {sorted(known)}")

    eligible = (listed.groupby("player_id")["fantasy_position"]
                .apply(lambda s: frozenset(s)).to_dict())

    # The NHL position is the fallback and also the arbiter of which side a player is on.
    primary = dict(zip(universe["player_id"],
                       universe["position"].map(league_module.NHL_TO_FANTASY)))
    goalie = league_module.GOALIE_POSITION

    out, fallbacks, conflicts = {}, 0, []
    for player_id, nhl_slot in primary.items():
        if nhl_slot is None:
            continue
        slots = eligible.get(player_id)
        if not slots:
            out[player_id] = frozenset({nhl_slot})
            fallbacks += 1
            continue
        # Keep only the side the NHL says he is on, and never leave him with nothing. Three sides,
        # not two: a forward listed at D is a collision too. Yahoo 2026-27 lists Carolina's
        # Sebastian Aho (a centre) at C and D -- the Islanders' defenceman shares his name -- and
        # value over replacement credits a flexible player against his scarcest position, which
        # put him third on the board.
        same_side = frozenset(s for s in slots if _side(s) == _side(nhl_slot))
        if same_side != slots:
            conflicts.append(player_id)
        out[player_id] = same_side or frozenset({nhl_slot})

    multi = sum(1 for s in out.values() if len(s) > 1)
    log.info("eligibility (%s %s): %d players, %.1f%% multi-position, %d fell back to their NHL "
             "position (%.1f%%)", config.eligibility_platform, config.eligibility_season,
             len(out), 100 * multi / max(len(out), 1), fallbacks,
             100 * fallbacks / max(len(out), 1))
    if conflicts:
        log.warning("dropped cross-side eligibility for %d player(s) %s -- a skater listed at G "
                    "or a goalie at a skater slot, or a forward at D, is a name-resolution "
                    "collision in the fantasy import, not eligibility", len(conflicts),
                    conflicts[:5])
    return out
