"""Joins a lineup variant onto the lineup-independent base table and derives the features
that depend on the lineup.

The base table is built once per season and reused for both variants, which is the point of
the split: variant A is one row per candidate, variant B is `--copies` rows per candidate
with independently perturbed lineups, and recomputing hundreds of rolling columns per copy
would be wasted work and could let copies disagree on columns that must be identical.

Three things can only be derived here, because they need the lineup and the base table at the
same time:

  * linemate and partner quality -- the variant gives mate ids, the base table gives those
    players' own rolling rates, so it is a self-join of the base on (game, team, mate).
  * the opposing starting goalie's form -- the variant's *goalie* rows carry
    feat_starting_goalie (two per game, one per team), which names the goalie a skater is
    about to face; his rolling save performance comes from the goalie table.
  * `*_known` flags -- distinguishing "this player has no power-play unit" from "we do not
    know his unit", which a NULL alone cannot express.
"""

import pandas as pd

SKATER_POSITIONS = ("C", "L", "R", "D")

MATE_RATE_COLUMNS = ["points_p60_l20", "ixg_all_p60_l20", "shots_p60_l20"]
PARTNER_RATE_COLUMNS = ["blocks_p60_l20", "points_p60_l20"]

LINEUP_PASSTHROUGH = ["lineup_age_days", "injured_at_lockout", "games_dressed_lookback",
                      "variant", "copy_index"]
LINEUP_FEATURES = ["feat_dressed", "feat_line", "feat_pair", "feat_pp", "feat_pk",
                   "feat_starting_goalie"]
LINEUP_LABELS = ["label_dressed", "label_line", "label_pair", "label_pp", "label_pk",
                 "label_starting_goalie", "label_in_spell"]


def starting_goalies(lineup: pd.DataFrame) -> pd.DataFrame:
    """(game_id, team_id) -> the goalie the variant expects to start. Taken from the lineup
    table's goalie rows before the skater filter drops them."""
    goalies = lineup[(lineup["position"] == "G") & (lineup["feat_starting_goalie"] == True)]  # noqa: E712
    return (goalies[["game_id", "team_id", "player_id", "copy_index"]]
            .rename(columns={"player_id": "opp_goalie_player_id"})
            .drop_duplicates(subset=["game_id", "team_id", "copy_index"]))


def _mate_rates(base: pd.DataFrame, frame: pd.DataFrame, id_column: str, rate_columns: list,
                prefix: str) -> pd.DataFrame:
    """Look up one named teammate's own rolling rates from the base table."""
    lookup = (base[["game_id", "team_id", "player_id"] + rate_columns]
              .rename(columns={"player_id": id_column,
                               **{c: f"{prefix}{c}" for c in rate_columns}}))
    return frame.merge(lookup, on=["game_id", "team_id", id_column], how="left")


def assemble(base: pd.DataFrame, goalies: pd.DataFrame, lineup: pd.DataFrame) -> pd.DataFrame:
    """One row per (game, team, skater candidate, copy): base features + lineup features."""
    goalie_starts = starting_goalies(lineup)

    skaters = lineup[lineup["position"].isin(SKATER_POSITIONS)].copy()
    keep = (["season_id", "game_id", "game_date", "team_id", "player_id", "position"]
            + [c for c in LINEUP_PASSTHROUGH + LINEUP_FEATURES + LINEUP_LABELS if c in skaters.columns]
            + [c for c in skaters.columns if c.startswith("feat_") and c.endswith("_id")])
    skaters = skaters[list(dict.fromkeys(keep))]
    skaters["game_date"] = pd.to_datetime(skaters["game_date"])

    base_columns = [c for c in base.columns if c not in ("position", "game_date", "season_id")]
    frame = skaters.merge(base[base_columns], on=["game_id", "team_id", "player_id"], how="left")

    # Linemate / partner quality, from the base table's own rolling rates.
    for id_column, rate_columns, prefix in (
        ("feat_mate1_id", MATE_RATE_COLUMNS, "mate1_"),
        ("feat_mate2_id", MATE_RATE_COLUMNS, "mate2_"),
        ("feat_partner_id", PARTNER_RATE_COLUMNS, "partner_"),
    ):
        if id_column in frame:
            frame = _mate_rates(base, frame, id_column, rate_columns, prefix)

    derived = {}
    for rate in MATE_RATE_COLUMNS:
        pair = [f"mate1_{rate}", f"mate2_{rate}"]
        if all(c in frame for c in pair):
            derived[f"linemates_{rate}"] = frame[pair].mean(axis=1)
    if derived:
        frame = pd.concat([frame, pd.DataFrame(derived, index=frame.index)], axis=1)

    # The opposing starting goalie and his form entering this game. The join includes
    # copy_index because variant B perturbs the starter per copy (a measured 58% chance the
    # chart's starter is not the one who played), so copy 1 can face a different goalie than
    # copy 2 of the same candidate. Variant A has a single copy_index of 0 throughout.
    frame = frame.merge(
        goalie_starts.rename(columns={"team_id": "opp_team_id"}),
        on=["game_id", "opp_team_id", "copy_index"], how="left",
    )
    goalie_form = goalies.drop(columns=["team_id"]).rename(columns={"player_id": "opp_goalie_player_id"})
    frame = frame.merge(goalie_form, on=["game_id", "opp_goalie_player_id"], how="left")

    # "No unit" vs "unit unknown": feat_dressed says whether the source lineup knew him at all.
    known = frame["feat_dressed"].fillna(False).astype(bool)
    flags = {f"{unit}_known": known & frame[f"feat_{unit}"].notna()
             for unit in ("line", "pair", "pp", "pk") if f"feat_{unit}" in frame}
    return pd.concat([frame, pd.DataFrame(flags, index=frame.index)], axis=1).copy()
