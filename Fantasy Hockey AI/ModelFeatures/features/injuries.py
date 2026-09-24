"""Who a team is missing, as of a game date.

`Injuries.Spells` gives the dated spells (imported from the NHL Injury Viz history); the
weight of an absence comes from the missing player's *role*, which is his last known
deployment in `Lineups.GameLineups` before that date. A team missing its PP1 winger and its
top-pair defenceman is in a different position from one missing two fourth-liners, and the
counts here are what let a model see that.

Everything is as-of the game date and strictly backwards-looking: the role lookup is an
as-of join against games already played, so nothing about the target game leaks in.
"""

import pandas as pd

from lineups import store


def injured_players(spells: dict, team_id: int, game_date) -> set:
    """The team's players KNOWN to be out at the lockout on that date: inside a spell that had
    already cost them a game. Not `store.injured_on`, which is also true on a spell's first game
    -- realized absence the lockout could not always see (see `store.injured_known_on`)."""
    return {
        player_id
        for (spell_team, player_id) in spells
        if spell_team == team_id and store.injured_known_on(spells, team_id, player_id, game_date)
    }


def last_known_roles(player_games: pd.DataFrame) -> pd.DataFrame:
    """One row per (player, game) carrying the deployment that game revealed, sorted for an
    as-of join: position, forward line, defence pair, PP unit."""
    roles = player_games.loc[
        player_games["dressed"].fillna(False).astype(bool),
        ["player_id", "game_date", "position", "actual_line", "actual_pair", "actual_pp"],
    ].copy()
    roles["game_date"] = pd.to_datetime(roles["game_date"])
    return roles.sort_values(["game_date", "player_id"], kind="mergesort").reset_index(drop=True)


def team_injury_context(team_games: pd.DataFrame, spells: dict, player_games: pd.DataFrame) -> pd.DataFrame:
    """Per (game, team): how many forwards / defencemen are out, and how many of them were
    top-six forwards, top-four defencemen or first-unit power-play players."""
    roles = last_known_roles(player_games)

    rows = []
    for team_id, game_id, game_date in team_games[["team_id", "game_id", "game_date"]].itertuples(index=False):
        for player_id in injured_players(spells, team_id, game_date):
            rows.append((game_id, team_id, pd.Timestamp(game_date), player_id))

    if not rows:
        return pd.DataFrame({"game_id": team_games["game_id"], "team_id": team_games["team_id"],
                             "injured_forwards": 0, "injured_defence": 0, "injured_top6_forwards": 0,
                             "injured_top4_defence": 0, "injured_pp1": 0})

    out = pd.DataFrame(rows, columns=["game_id", "team_id", "game_date", "player_id"])
    out = out.sort_values(["game_date", "player_id"], kind="mergesort").reset_index(drop=True)

    # As-of join: each injured player's most recent pre-game deployment.
    merged = pd.merge_asof(
        out, roles, on="game_date", by="player_id", allow_exact_matches=False, direction="backward",
    )

    merged["is_forward"] = merged["position"].isin(["C", "L", "R"])
    merged["is_defence"] = merged["position"] == "D"
    summary = merged.groupby(["game_id", "team_id"], as_index=False).agg(
        injured_forwards=("is_forward", "sum"),
        injured_defence=("is_defence", "sum"),
        injured_top6_forwards=("actual_line", lambda s: int((s <= 2).sum())),
        injured_top4_defence=("actual_pair", lambda s: int((s <= 2).sum())),
        injured_pp1=("actual_pp", lambda s: int((s == 1).sum())),
    )

    base = team_games[["game_id", "team_id"]].merge(summary, on=["game_id", "team_id"], how="left")
    count_columns = ["injured_forwards", "injured_defence", "injured_top6_forwards",
                     "injured_top4_defence", "injured_pp1"]
    base[count_columns] = base[count_columns].fillna(0).astype(int)
    return base
