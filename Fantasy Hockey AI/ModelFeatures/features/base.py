"""The lineup-independent half of the skater feature table.

One row per (game, team, skater candidate), where the candidate universe comes from the
lineup features' own keys so that assemble.py's join is 1:1 by construction. Everything here
is computable at the daily lineup lock: the player's own history as of that date, his team's
and opponent's form entering the game, the schedule, the rink, and who the team has injured.

The player family is attached with an as-of join rather than a shift, because a candidate who
did not play the target game has no row in the per-game frame to shift against. `build_base`
then asserts, on the finished table, that every row's history came from a game strictly
earlier than its own -- the leakage guarantee checked against the data rather than assumed
from the code.

The goalie rolling table is built here too but kept separate: it is not part of the skater
grain, and assemble.py uses it only to attach the opposing starting goalie's form.
"""

import pandas as pd

from features import context, extract, injuries, rolling
from lineups import store

# Boxscore counting stats that roll as sums, then become per-60 rates.
COUNTING_STATS = [
    "goals", "assists", "points", "shots", "hits", "blocks", "giveaways", "takeaways",
    "pim", "ppp", "shp", "faceoff_wins", "faceoff_losses",
]
# Ice-time columns roll as sums (for the per-60 denominators) and as means (for the role view).
TOI_STATS = ["toi", "ev_toi", "pp_toi", "sh_toi", "p1_ev_seconds"]
# Individual analytics, per situation suffix.
ANALYTIC_STATS = ["icf_all", "iff_all", "ixg_all", "icf_5v5", "iff_5v5", "ixg_5v5", "ixg_5v4"]
# On-ice rate stats roll as means -- they are already rates per game.
ONICE_STATS = ["oi_cf_pct_5v5", "oi_xgf_pct_5v5", "oi_sh_pct_5v5", "oi_pdo_5v5"]
ZONE_STATS = ["oz_starts", "dz_starts", "nz_starts"]

PREV_SEASON_STATS = ["toi", "shots", "goals", "assists", "points", "hits", "blocks", "pim", "ppp"]

SITUATION_SUFFIX = {
    extract.SITUATION_ALL: "all",
    extract.SITUATION_5V5: "5v5",
    extract.SITUATION_5V4: "5v4",
    extract.SITUATION_4V5: "4v5",
}


def _pivot_situations(long: pd.DataFrame, value_columns: list, keep: list) -> pd.DataFrame:
    """Long (game, player, situation) -> wide {stat}_{situation} columns."""
    frames = []
    for situation_id, suffix in SITUATION_SUFFIX.items():
        part = long[long["situation_id"] == situation_id]
        if part.empty:
            continue
        part = part[keep + value_columns].rename(columns={c: f"{c}_{suffix}" for c in value_columns})
        frames.append(part.set_index(keep))
    if not frames:
        return pd.DataFrame(index=pd.MultiIndex.from_arrays([[]] * len(keep), names=keep))
    return pd.concat(frames, axis=1).reset_index()


def player_game_facts(cursor, season_ids: list) -> pd.DataFrame:
    """Per (game, team, player) actually played: boxscore stats, the analytics pivoted by
    situation, and zone starts. This is the frame the player family rolls over.

    A missing Analytics row means "no events in that situation", not "unknown", so the
    analytic columns are coalesced to 0 wherever the player had ice time at all -- the
    situation tables only carry rows where something happened (83,597 at 5v5 against 4,966 at
    4v5), and treating that as NULL would throw away most of the PP and PK signal.
    """
    facts = extract.player_games(cursor, season_ids)
    facts["ev_toi"] = (facts["toi"] - facts["pp_toi"].fillna(0) - facts["sh_toi"].fillna(0)).clip(lower=0)

    individual = _pivot_situations(
        extract.player_individual_analytics(cursor, season_ids),
        ["icf", "iff", "ixg"], ["game_id", "player_id"],
    )
    onice = _pivot_situations(
        extract.player_onice_analytics(cursor, season_ids),
        ["oi_cf_pct", "oi_xgf_pct", "oi_sh_pct", "oi_pdo"], ["game_id", "player_id"],
    )
    zones = extract.zone_starts(cursor, season_ids)

    facts = facts.merge(individual, on=["game_id", "player_id"], how="left")
    facts = facts.merge(onice, on=["game_id", "player_id"], how="left")
    facts = facts.merge(zones, on=["game_id", "player_id", "team_id"], how="left")

    played = facts["toi"].fillna(0) > 0
    for column in ONICE_STATS:
        if column not in facts:   # a season with no games yet (the live build on opening night)
            facts[column] = pd.NA
    for column in ANALYTIC_STATS + ZONE_STATS:
        if column not in facts:
            facts[column] = pd.NA
        facts[column] = pd.to_numeric(facts[column], errors="coerce").where(~played | facts[column].notna(), 0.0)

    facts["game_date"] = pd.to_datetime(facts["game_date"])
    return facts.sort_values(["player_id", "game_date", "game_id"], kind="mergesort").reset_index(drop=True)


def player_history(facts: pd.DataFrame) -> pd.DataFrame:
    """The player family: every rolling window as of the end of each game he played, ready to
    be attached to a target date by an as-of join (hence shift=0 -- see rolling.py)."""
    keys = ["player_id", "season_id"]
    sums = COUNTING_STATS + TOI_STATS + ANALYTIC_STATS + ZONE_STATS

    rolled = rolling.rolling_sums(facts, keys, sums, how="sum", shift=0)
    means = rolling.rolling_sums(facts, keys, TOI_STATS + ONICE_STATS, how="mean", prefix="mean_", shift=0)
    played = rolling.games_played(facts, keys, shift=0)
    rates = rolling.per60(rolled, COUNTING_STATS + ANALYTIC_STATS, "toi")
    pp_rates = rolling.per60(rolled, ["ppp"], "pp_toi", prefix="pp_")
    ev_rates = rolling.per60(rolled, ["shots", "points"], "ev_toi", prefix="ev_")

    history = pd.concat([
        facts[["player_id", "season_id", "game_id", "game_date", "team_id"]].rename(
            columns={"game_id": "source_game_id", "game_date": "source_game_date",
                     "team_id": "source_team_id"}),
        rolled, means, played, rates, pp_rates, ev_rates,
    ], axis=1)

    for suffix in [f"l{w}" for w in rolling.WINDOWS] + [rolling.SEASON_TO_DATE]:
        shots = history[f"shots_{suffix}"]
        history[f"shooting_pct_{suffix}"] = (history[f"goals_{suffix}"] / shots.where(shots > 0))
        faceoffs = history[f"faceoff_wins_{suffix}"] + history[f"faceoff_losses_{suffix}"]
        history[f"faceoff_pct_{suffix}"] = history[f"faceoff_wins_{suffix}"] / faceoffs.where(faceoffs > 0)
        zone = history[f"oz_starts_{suffix}"] + history[f"dz_starts_{suffix}"]
        history[f"oz_start_pct_{suffix}"] = history[f"oz_starts_{suffix}"] / zone.where(zone > 0)

    # Role trend: is his deployment rising or falling?
    history["ev_toi_trend"] = history["mean_ev_toi_l5"] - history["mean_ev_toi_l20"]
    history["pp_toi_trend"] = history["mean_pp_toi_l5"] - history["mean_pp_toi_l20"]
    history["sh_toi_trend"] = history["mean_sh_toi_l5"] - history["mean_sh_toi_l20"]
    return history


def attach_player_history(candidates: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """As-of join: each candidate row gets the player's history from his most recent game
    strictly before the target game's date."""
    left = candidates.sort_values(["game_date", "player_id"], kind="mergesort").reset_index(drop=True)
    right = history.sort_values(["source_game_date", "player_id"], kind="mergesort").reset_index(drop=True)
    right = right.drop(columns=["season_id"])
    # Typed even when empty -- a season with no games yet (the live build on opening night).
    right["player_id"] = right["player_id"].astype(left["player_id"].dtype)
    right["source_game_date"] = pd.to_datetime(right["source_game_date"])

    merged = pd.merge_asof(
        left, right,
        left_on="game_date", right_on="source_game_date",
        by="player_id", direction="backward", allow_exact_matches=False,
    )
    return merged


def team_context(cursor, season_ids: list, player_facts: pd.DataFrame, tonight: dict | None = None) -> pd.DataFrame:
    """Everything keyed on (game, team): form, the opponent's form, schedule, rink, injuries.
    `tonight` (live build, ModelFeatures/tonight.py) adds not-yet-played games as placeholder
    rows with empty stats, so the same shift-by-one windows give the form entering them, and
    overlays the injury reports known now onto the spells."""
    team_games = extract.team_games(cursor, season_ids)
    games = extract.games(cursor, season_ids)
    spells = store.load_injury_spells(cursor, season_ids)
    if tonight is not None:
        team_games = pd.concat([_before(team_games, tonight), tonight["team_games"]], ignore_index=True)
        games = pd.concat([_before(games, tonight), tonight["games"]], ignore_index=True)
        spells = store.merge_spells(spells, tonight["spells"])

    form = context.team_form(team_games)
    frame = team_games[["game_id", "team_id", "opp_team_id", "is_home", "season_id", "game_date"]]
    frame = frame.merge(form.rename(columns={c: f"team_{c}" for c in form.columns
                                             if c not in ("game_id", "team_id")}),
                        on=["game_id", "team_id"], how="left")
    frame = frame.merge(context.opponent_form(form, team_games), on=["game_id", "team_id"], how="left")
    frame = frame.merge(context.schedule_context(team_games, games), on=["game_id", "team_id"], how="left")
    frame = frame.merge(context.arena_factors(team_games), on=["game_id", "team_id"], how="left")
    frame = frame.merge(injuries.team_injury_context(team_games, spells, player_facts),
                        on=["game_id", "team_id"], how="left")

    # The opponent's rest, which lives in the same schedule frame under their team id.
    opponent_rest = context.schedule_context(team_games, games)[
        ["game_id", "team_id", "days_rest", "is_back_to_back", "games_last_4d"]
    ].rename(columns={"team_id": "opp_team_id", "days_rest": "opp_days_rest",
                      "is_back_to_back": "opp_is_back_to_back", "games_last_4d": "opp_games_last_4d"})
    return frame.merge(opponent_rest, on=["game_id", "opp_team_id"], how="left")


def goalie_history(cursor, season_ids: list, tonight: dict | None = None) -> pd.DataFrame:
    """Rolling save performance per goalie as of the end of each game he played, for an as-of
    join onto a target date (assemble.py) -- the player family's pattern (shift=0 here, the join
    excludes the target date).

    It used to be joined on the target game's own row with a shift, which exists only for a
    goalie who played that game: the expected opposing starter's form was present when he did
    play and missing when he did not, so the missingness told the skater models who started
    (193 of 480 rows on one replayed date). As of the date, it is there either way."""
    goalies = extract.goalie_games(cursor, season_ids)
    if tonight is not None:
        goalies = _before(goalies, tonight)
    goalies["game_date"] = pd.to_datetime(goalies["game_date"])
    goalies = goalies.sort_values(["player_id", "game_date", "game_id"], kind="mergesort").reset_index(drop=True)

    keys = ["player_id", "season_id"]
    cols = ["shots_against", "saves", "goals_against", "xga", "gsax"]
    rolled = rolling.rolling_sums(goalies, keys, cols, windows=(10,), how="sum", shift=0)
    played = rolling.games_played(goalies, keys, windows=(10,), shift=0)

    frame = pd.concat([goalies[["player_id", "season_id", "game_date"]], rolled, played], axis=1)
    for suffix in ("l10", rolling.SEASON_TO_DATE):
        shots = frame[f"shots_against_{suffix}"].where(lambda s: s > 0)
        frame[f"goalie_sv_pct_{suffix}"] = frame[f"saves_{suffix}"] / shots
        frame[f"goalie_gsax_per_shot_{suffix}"] = frame[f"gsax_{suffix}"] / shots
        frame[f"goalie_gp_{suffix}"] = frame[f"gp_{suffix}"]
    keep = ["player_id", "season_id", "game_date"] + [c for c in frame.columns if c.startswith("goalie_")]
    return frame[keep].rename(columns={"game_date": "goalie_source_date"})


def _before(frame: pd.DataFrame, tonight: dict) -> pd.DataFrame:
    """The live build sees only games played before its date -- on a real game day nothing
    later exists yet; replaying a past date, this is what makes the replay honest."""
    return frame[pd.to_datetime(frame["game_date"]) < pd.Timestamp(tonight["date"])]


def build_base(cursor, season_ids: list, candidates: pd.DataFrame, tonight: dict | None = None) -> tuple:
    """(skaters, goalie_history) for the given seasons, over the candidate universe. `tonight`
    is the live build's not-yet-played games (see team_context)."""
    facts = player_game_facts(cursor, season_ids)
    if tonight is not None:
        facts = _before(facts, tonight)
    history = player_history(facts)
    teams = team_context(cursor, season_ids, facts, tonight)
    goalies = goalie_history(cursor, season_ids, tonight)

    candidates = candidates.copy()
    candidates["game_date"] = pd.to_datetime(candidates["game_date"])
    skaters = attach_player_history(candidates, history)
    skaters = skaters.merge(teams.drop(columns=["season_id", "game_date"]),
                            on=["game_id", "team_id"], how="left")

    # The prior-season family needs the *previous* season's games, which are not in `facts`
    # (this build is scoped to the target season), so they are pulled separately. Without
    # this the prev_* columns simply never appear.
    prior_by_season = extract.prior_season_ids(cursor, season_ids)
    prior_ids = sorted({p for p in prior_by_season.values() if p is not None})
    if prior_ids:
        prior_facts = extract.player_games(cursor, prior_ids)
        prior = rolling.prior_season_aggregate(prior_facts, prior_by_season, PREV_SEASON_STATS)
        if not prior.empty:
            skaters = skaters.merge(prior, on=["player_id", "season_id"], how="left")

    skaters = add_age(skaters, extract.player_birthdates(cursor))
    skaters = add_targets(skaters, facts)
    # Opening night has no history in the new season for anyone; only the live build meets it.
    assert_no_leakage(skaters, require_history=tonight is None)
    return skaters, goalies


def add_age(skaters: pd.DataFrame, birthdates: pd.DataFrame) -> pd.DataFrame:
    """`age_years`: the player's age on the game date, in years.

    Lineup-independent, so it lives in the base table and reaches both variants and the
    rest-of-season model -- which is where an aging curve matters: young players rise and older
    ones fall over a season, and nothing else in the table says which a player is. A birth date
    is known forever, so there is no leakage question. A player with no bio yet gets NaN rather
    than an imputed age; LightGBM routes missing values on its own, and `verify.age` reports
    the coverage.
    """
    merged = skaters.merge(birthdates, on="player_id", how="left")
    merged["age_years"] = (merged["game_date"] - merged["birth_date"]).dt.days / 365.25
    return merged.drop(columns=["birth_date"])


def add_targets(skaters: pd.DataFrame, facts: pd.DataFrame) -> pd.DataFrame:
    """What actually happened in the row's own game -- the modelling targets.

    The join carries `team_id`, and must. A player traded mid-season stays in his old team's
    candidate pool for a while, correctly marked as not dressing for them; joining on
    (game, player) alone handed that phantom row the stat line he produced for his *new*
    team the same night, so 16 player-games a season showed a skater playing for both clubs
    at once. With the team in the key the phantom gets no row, and `target_played` is False
    as it should be.
    """
    target_columns = ["toi", "ev_toi", "pp_toi", "shots", "hits", "blocks", "goals", "assists",
                      "points", "pim", "ppp", "shp", "ixg_all"]
    actual = facts[["game_id", "player_id", "team_id"] + target_columns].rename(
        columns={c: f"target_{'ixg' if c == 'ixg_all' else c}" for c in target_columns}
    )
    merged = skaters.merge(actual, on=["game_id", "player_id", "team_id"], how="left")
    played = (merged["target_toi"].notna() & (merged["target_toi"] > 0)).rename("target_played")
    return pd.concat([merged, played], axis=1).copy()


def assert_no_leakage(skaters: pd.DataFrame, require_history: bool = True) -> None:
    """Every attached history must come from a game strictly before the row's own."""
    known = skaters["source_game_date"].notna()
    if require_history and not known.any():
        raise AssertionError("no row got any player history -- the as-of join matched nothing")
    offenders = skaters.loc[known & (skaters["source_game_date"] >= skaters["game_date"])]
    if len(offenders):
        raise AssertionError(
            f"{len(offenders)} row(s) carry history from the target game or later, e.g. "
            f"{offenders.iloc[0][['game_id', 'player_id', 'game_date', 'source_game_date']].to_dict()}"
        )
