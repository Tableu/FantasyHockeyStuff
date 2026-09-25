"""Features that belong to the game and the two teams rather than to the player: rest and
schedule density, team/opponent form, and the arena's scorekeeper bias.

All of it obeys the same shift-by-one rule as rolling.py -- a team's form entering game N is
computed from games strictly before N.
"""

import numpy as np
import pandas as pd

from features import rolling

TEAM_WINDOWS = (10,)

# Per-game team rates that get rolled, as (column, denominator) pairs. Rate columns are
# rolled as ratios of their rolled parts, never as an average of per-game ratios, so a
# 3-shot game does not weigh the same as a 40-shot one.
TEAM_SUM_COLS = [
    "shots_for", "shots_against", "goals_for", "goals_against", "xgf", "xga",
    "hits_for", "hits_against", "blocks_for", "blocks_against",
    "pim_for", "pp_goals", "pp_opportunities", "penalties_taken", "skater_toi",
]


def schedule_context(team_games: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """Rest and density per (game, team): days since the team's previous game, whether it is
    the back half of a back-to-back, games in the previous 4 and 7 days, and the start hour.

    Start time comes from Reference.Schedule -- Game.Games.StartTimeUTC is NULL for every
    game -- and is kept as the UTC hour rather than a local one: there is no arena-timezone
    table, and `home_team_id` is in the row, so a model can absorb the offset per team.
    """
    df = team_games.sort_values(["team_id", "game_date", "game_id"], kind="mergesort").reset_index(drop=True)
    keys = ["team_id", "season_id"]

    prev_date = df.groupby(keys, sort=False)["game_date"].shift(1)
    prev_home = df.groupby(keys, sort=False)["is_home"].shift(1)
    days_rest = (pd.to_datetime(df["game_date"]) - pd.to_datetime(prev_date)).dt.days

    out = pd.DataFrame({
        "game_id": df["game_id"],
        "team_id": df["team_id"],
        "days_rest": days_rest,
        "is_back_to_back": (days_rest == 1).astype("float").where(days_rest.notna()),
        "last_game_was_home": prev_home,
        "changed_venue_type": (prev_home != df["is_home"]).astype("float").where(prev_home.notna()),
    })

    # Games in the previous 4 / 7 days: counted from the team's own past dates only.
    dates = pd.to_datetime(df["game_date"])
    for window_days in (4, 7):
        counts = []
        for _, group in df.groupby(keys, sort=False):
            group_dates = pd.to_datetime(group["game_date"]).to_numpy()
            for i, day in enumerate(group_dates):
                earlier = group_dates[:i]
                counts.append(int(((day - earlier) / np.timedelta64(1, "D") <= window_days).sum()))
        out[f"games_last_{window_days}d"] = counts

    start_hour = pd.to_datetime(games.set_index("game_id")["start_time_utc"]).dt.hour
    out["start_hour_utc"] = out["game_id"].map(start_hour)
    return out


def team_form(team_games: pd.DataFrame) -> pd.DataFrame:
    """Rolling team form per (game, team), leak-free: `team_*` columns for the row's own team.
    Rates are built from rolled numerators and denominators."""
    df = team_games.sort_values(["team_id", "game_date", "game_id"], kind="mergesort").reset_index(drop=True)
    keys = ["team_id", "season_id"]

    rolled = rolling.rolling_sums(df, keys, TEAM_SUM_COLS, windows=TEAM_WINDOWS, how="sum")
    gp = rolling.games_played(df, keys, windows=TEAM_WINDOWS)
    frame = pd.concat([df[["game_id", "team_id"]], rolled, gp], axis=1)

    for suffix in [f"l{w}" for w in TEAM_WINDOWS] + [rolling.SEASON_TO_DATE]:
        games_in_window = frame[f"gp_{suffix}"].where(lambda s: s > 0)
        hours = frame[f"skater_toi_{suffix}"] / 3600.0
        hours = hours.where(hours > 0)
        for stat in ("shots_for", "shots_against", "xgf", "xga"):
            frame[f"{stat}_p60_{suffix}"] = frame[f"{stat}_{suffix}"] / hours
        for stat in ("goals_for", "goals_against", "hits_for", "hits_against",
                     "blocks_for", "blocks_against", "pim_for", "penalties_taken",
                     "pp_opportunities"):
            frame[f"{stat}_per_game_{suffix}"] = frame[f"{stat}_{suffix}"] / games_in_window
        opportunities = frame[f"pp_opportunities_{suffix}"].where(lambda s: s > 0)
        frame[f"pp_pct_{suffix}"] = frame[f"pp_goals_{suffix}"] / opportunities

    keep = ["game_id", "team_id"] + [c for c in frame.columns if c.endswith(tuple(
        [f"_l{w}" for w in TEAM_WINDOWS] + [f"_{rolling.SEASON_TO_DATE}"]))]
    return frame[keep]


def opponent_form(form: pd.DataFrame, team_games: pd.DataFrame) -> pd.DataFrame:
    """The same rolling form joined on the opponent, renamed `opp_*`."""
    pairs = team_games[["game_id", "team_id", "opp_team_id"]]
    renamed = form.rename(columns={c: f"opp_{c}" for c in form.columns if c not in ("game_id", "team_id")})
    merged = pairs.merge(renamed, left_on=["game_id", "opp_team_id"], right_on=["game_id", "team_id"],
                         how="left", suffixes=("", "_drop"))
    return merged.drop(columns=[c for c in merged.columns if c.endswith("_drop")] + ["opp_team_id"])


def arena_factors(team_games: pd.DataFrame, min_games: int = 10) -> pd.DataFrame:
    """`arena_hit_factor` / `arena_block_factor` per (game, team): how much more (or less) of
    a stat this rink's scorekeeper records than the league, from games strictly earlier in
    the season.

    The arena is the home team's rink -- Game.Games.Venue is empty for every game, and
    neutral-site games are rare enough to ignore. The factor is the rink's recorded events
    per game to date over the league's per game to date, so 1.0 means "records like everyone
    else"; it stays exactly 1.0 until the rink has `min_games` of history, which keeps early
    season rows from swinging on two games.
    """
    df = team_games.copy()
    df["arena_team_id"] = np.where(df["is_home"] == 1, df["team_id"], df["opp_team_id"])
    # Tonight's not-yet-played games (the live build's placeholders) take no part in the
    # league's rate: their empty stats would count as zero-hit games.
    df["_played"] = ~df["is_placeholder"].fillna(False).astype(bool) if "is_placeholder" in df else True
    # One row per game per arena: both teams' recorded totals in that building.
    per_game = (df.groupby(["season_id", "arena_team_id", "game_id", "game_date"], as_index=False)
                  .agg(hits=("hits_for", "sum"), blocks=("blocks_for", "sum"), played=("_played", "all")))
    per_game = per_game.sort_values(["arena_team_id", "game_date", "game_id"], kind="mergesort").reset_index(drop=True)

    # The rink's own history to date (season-to-date only -- a rink hosts ~41 games a year,
    # so shorter windows would be pure noise).
    keys = ["arena_team_id", "season_id"]
    rolled = rolling.rolling_sums(per_game, keys, ["hits", "blocks"], windows=(), how="sum")
    gp = rolling.games_played(per_game, keys, windows=())
    arena = pd.concat([per_game[["season_id", "arena_team_id", "game_id", "game_date"]], rolled, gp], axis=1)

    # The league's rate to date, as of each game: the same shift-by-one, league-wide.
    league_frames = []
    for season_id, group in per_game.groupby("season_id", sort=False):
        group = group.sort_values(["game_date", "game_id"], kind="mergesort").reset_index(drop=True)
        played = group["played"].astype(float)
        prior_games = played.cumsum().shift(1).where(lambda s: s > 0)
        league_frames.append(pd.DataFrame({
            "season_id": season_id,
            "game_id": group["game_id"],
            "league_hits": (group["hits"] * played).cumsum().shift(1) / prior_games,
            "league_blocks": (group["blocks"] * played).cumsum().shift(1) / prior_games,
        }))
    arena = arena.merge(pd.concat(league_frames, ignore_index=True), on=["season_id", "game_id"], how="left")

    enough_history = arena["gp_std"] >= min_games
    for stat, name in (("hits", "arena_hit_factor"), ("blocks", "arena_block_factor")):
        rink_rate = arena[f"{stat}_std"] / arena["gp_std"].where(arena["gp_std"] > 0)
        league_rate = arena[f"league_{stat}"].where(arena[f"league_{stat}"] > 0)
        arena[name] = (rink_rate / league_rate).where(enough_history, 1.0).fillna(1.0)

    return (df[["game_id", "team_id", "arena_team_id"]]
            .merge(arena[["game_id", "arena_team_id", "arena_hit_factor", "arena_block_factor"]],
                   on=["game_id", "arena_team_id"], how="left")
            .drop(columns=["arena_team_id"]))
