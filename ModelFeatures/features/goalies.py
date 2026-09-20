"""The goalie feature table: one row per (game, team, goalie candidate).

The skater half of this package answers "what will he produce tonight". The goalie half has
to answer two questions that behave very differently, and the build plan's analysis is what
shapes the feature set:

  * **Will he start?** A goalie who does not start scores nothing, and unlike a skater's
    P(plays) this is close to a coin flip on a back-to-back. It is the single largest source
    of goalie variance.
  * **What happens if he does?** Per-shot value works out to `1.25 x SV% - 1` under a typical
    scoring system, so *shots against* dominates -- and shots against is a property of the
    two teams, not of the goalie. Team shot suppression and opponent shot generation are far
    more predictable than save percentage, which is why the team and opponent families here
    carry more weight than the goalie's own recent form does.

Everything is computable at the daily lineup lock, under the same rule as `base.py`: rolling
windows are attached by an as-of join on the date, and team form is already shifted by one
game by `context.team_form`.

Reuses the skater machinery wholesale rather than reimplementing it -- `context.team_form`,
`context.opponent_form`, `context.schedule_context`, `injuries.team_injury_context` and every
helper in `rolling.py` are all keyed on (game, team) or (player, season) and do not care
which position the row belongs to.
"""

import logging

import pandas as pd

from features import base, extract, rolling

log = logging.getLogger(__name__)

# Summed over each rolling window, then turned into rates.
VOLUME_STATS = ["shots_against", "saves", "goals_against", "xga", "gsax", "toi"]

# Counted over each window: how the tandem has actually been used.
USAGE_STATS = ["started", "won", "lost", "ot_lost", "shutout"]


def goalie_facts(cursor, season_ids: list) -> pd.DataFrame:
    """Every goalie appearance in the seasons, with the derived outcome flags."""
    facts = extract.goalie_game_facts(cursor, season_ids)
    facts["game_date"] = pd.to_datetime(facts["game_date"])
    facts = facts.sort_values(["player_id", "game_date", "game_id"],
                              kind="mergesort").reset_index(drop=True)

    decision = facts["decision"]
    facts["started"] = facts["is_starter"].fillna(False).astype("float64")
    facts["won"] = (decision == "W").astype("float64")
    facts["lost"] = (decision == "L").astype("float64")
    facts["ot_lost"] = (decision == "O").astype("float64")
    # A shutout is the goalie of record with nothing past him. A relief appearance with no
    # goals allowed is not a shutout, which is why this keys off the decision.
    facts["shutout"] = ((decision == "W") & (facts["goals_against"] == 0)).astype("float64")
    return facts


def goalie_history(facts: pd.DataFrame) -> pd.DataFrame:
    """Rolling form as of the end of each appearance, for an as-of join onto a target date.

    shift=0 for the same reason as the skater family: the as-of join in
    `base.attach_player_history` already guarantees the source game is strictly earlier, so
    shifting here as well would throw away the most recent game.
    """
    keys = ["player_id", "season_id"]
    rolled = rolling.rolling_sums(facts, keys, VOLUME_STATS + USAGE_STATS, how="sum", shift=0)
    played = rolling.games_played(facts, keys, shift=0)

    history = pd.concat([
        facts[["player_id", "season_id", "game_id", "game_date", "team_id"]].rename(
            columns={"game_id": "source_game_id", "game_date": "source_game_date",
                     "team_id": "source_team_id"}),
        rolled, played,
    ], axis=1)

    for suffix in [f"l{w}" for w in rolling.WINDOWS] + [rolling.SEASON_TO_DATE]:
        shots = history[f"shots_against_{suffix}"]
        safe_shots = shots.where(shots > 0)
        minutes = history[f"toi_{suffix}"].where(lambda s: s > 0) / 60.0
        history[f"sv_pct_{suffix}"] = history[f"saves_{suffix}"] / safe_shots
        history[f"gsax_per_shot_{suffix}"] = history[f"gsax_{suffix}"] / safe_shots
        history[f"xg_against_per_shot_{suffix}"] = history[f"xga_{suffix}"] / safe_shots
        history[f"shots_against_p60_{suffix}"] = history[f"shots_against_{suffix}"] / minutes * 60
        history[f"goals_against_p60_{suffix}"] = history[f"goals_against_{suffix}"] / minutes * 60
        appearances = history[f"gp_{suffix}"].where(lambda s: s > 0)
        history[f"start_share_{suffix}"] = history[f"started_{suffix}"] / appearances
        history[f"win_pct_{suffix}"] = history[f"won_{suffix}"] / appearances

    # Is his form moving? The same trend idea the skater table uses for ice time.
    history["sv_pct_trend"] = history["sv_pct_l5"] - history["sv_pct_l20"]
    history["workload_trend"] = history["shots_against_p60_l5"] - history["shots_against_p60_l20"]
    return history


def tandem_partner(candidates: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """The *other* goalie on the same team in the same game, and his form.

    A starter's value depends on who else is available: a team with a capable 1B splits
    starts differently from one carrying an AHL call-up, and it changes both the chance of
    starting and the chance of being pulled. Built by self-joining the candidate set on
    (game, team), which is why it lives here rather than in the base build.
    """
    keep = ["sv_pct_std", "gsax_per_shot_std", "start_share_std", "gp_std"]
    available = [c for c in keep if c in history.columns]
    partner_form = history[["source_game_id", "player_id"] + available]

    pairs = candidates[["game_id", "team_id", "player_id"]].merge(
        candidates[["game_id", "team_id", "player_id"]].rename(columns={"player_id": "partner_id"}),
        on=["game_id", "team_id"], how="left")
    pairs = pairs[pairs["player_id"] != pairs["partner_id"]]
    # More than two goalies can be listed after a recall; keep the one with the most history.
    pairs = pairs.drop_duplicates(["game_id", "team_id", "player_id"], keep="first")
    return pairs


def build_goalie_base(cursor, season_ids: list, candidates: pd.DataFrame) -> pd.DataFrame:
    """The lineup-independent half: his history as of the lock, his team's and the
    opponent's form, schedule, injuries, and what actually happened."""
    facts = goalie_facts(cursor, season_ids)
    history = goalie_history(facts)

    # The injury context wants skater facts (it counts injured forwards and defence), which
    # is exactly what a goalie needs to know: a missing top-4 defenceman means more shots.
    skater_facts = extract.player_games(cursor, season_ids)
    skater_facts["game_date"] = pd.to_datetime(skater_facts["game_date"])
    teams = base.team_context(cursor, season_ids, skater_facts)

    candidates = candidates.copy()
    candidates["game_date"] = pd.to_datetime(candidates["game_date"])
    frame = base.attach_player_history(candidates, history)
    frame = frame.merge(teams.drop(columns=["season_id", "game_date"]),
                        on=["game_id", "team_id"], how="left")

    prior_by_season = extract.prior_season_ids(cursor, season_ids)
    prior_ids = sorted({p for p in prior_by_season.values() if p is not None})
    if prior_ids:
        prior_facts = goalie_facts(cursor, prior_ids)
        prior = prior_season_form(prior_facts, prior_by_season)
        if not prior.empty:
            frame = frame.merge(prior, on=["player_id", "season_id"], how="left")

    frame = add_targets(frame, facts)
    base.assert_no_leakage(frame)
    return frame


def prior_season_form(prior_facts: pd.DataFrame, prior_by_season: dict) -> pd.DataFrame:
    """Last season's rate line per goalie, keyed to the season it is a prior *for*."""
    grouped = prior_facts.groupby(["player_id", "season_id"], as_index=False).agg(
        prev_appearances=("game_id", "size"),
        prev_starts=("started", "sum"),
        prev_shots_against=("shots_against", "sum"),
        prev_saves=("saves", "sum"),
        prev_goals_against=("goals_against", "sum"),
        prev_gsax=("gsax", "sum"),
        prev_wins=("won", "sum"),
        prev_shutouts=("shutout", "sum"),
        prev_toi=("toi", "sum"),
    )
    shots = grouped["prev_shots_against"].where(lambda s: s > 0)
    grouped["prev_sv_pct"] = grouped["prev_saves"] / shots
    grouped["prev_gsax_per_shot"] = grouped["prev_gsax"] / shots
    minutes = grouped["prev_toi"].where(lambda s: s > 0) / 60.0
    grouped["prev_shots_against_p60"] = grouped["prev_shots_against"] / minutes * 60

    forward = {prior: target for target, prior in prior_by_season.items() if prior is not None}
    grouped["season_id"] = grouped["season_id"].map(forward)
    return grouped.dropna(subset=["season_id"]).astype({"season_id": "int64"})


def add_targets(frame: pd.DataFrame, facts: pd.DataFrame) -> pd.DataFrame:
    """What actually happened in the row's own game.

    Joined on (game, player, team) -- never (game, player) -- for the same reason the skater
    table is: a goalie traded mid-season sits in his old club's candidate pool for a while,
    and a two-key join would hand that phantom row the line he posted for his new club.
    """
    columns = ["toi", "shots_against", "saves", "goals_against", "xga", "gsax",
               "started", "won", "lost", "ot_lost", "shutout"]
    actual = facts[["game_id", "player_id", "team_id"] + columns].rename(
        columns={c: f"target_{c}" for c in columns})
    merged = frame.merge(actual, on=["game_id", "player_id", "team_id"], how="left")

    played = (merged["target_toi"].notna() & (merged["target_toi"] > 0)).rename("target_played")
    merged = pd.concat([merged, played], axis=1)
    for column in ("target_started", "target_won", "target_lost", "target_ot_lost",
                   "target_shutout"):
        merged[column] = merged[column].fillna(0.0)
    shots = merged["target_shots_against"]
    merged["target_sv_pct"] = merged["target_saves"] / shots.where(shots > 0)
    return merged.copy()


def assemble(goalie_base: pd.DataFrame, lineup: pd.DataFrame) -> pd.DataFrame:
    """Join a lineup variant on, and derive the features that need both.

    The variant supplies what the lock believed about tonight's starter; everything else was
    already computable without it.
    """
    keys = ["game_id", "team_id", "player_id"]
    columns = keys + [c for c in ("copy_index", "variant", "feat_dressed",
                                  "feat_starting_goalie", "label_dressed",
                                  "label_starting_goalie", "lineup_age_days",
                                  "injured_at_lockout", "games_dressed_lookback")
                      if c in lineup.columns]
    frame = lineup[columns].merge(goalie_base, on=keys, how="inner", suffixes=("", "_base"))

    # The tandem partner's season-to-date form, from the base table's own rows.
    partner_columns = ["sv_pct_std", "gsax_per_shot_std", "start_share_std", "gp_std"]
    available = [c for c in partner_columns if c in goalie_base.columns]
    if available:
        lookup = goalie_base[["game_id", "team_id", "player_id"] + available].rename(
            columns={"player_id": "partner_id",
                     **{c: f"partner_{c}" for c in available}})
        pairs = tandem_partner(goalie_base, goalie_base)
        frame = frame.merge(pairs, on=keys, how="left")
        frame = frame.merge(lookup, on=["game_id", "team_id", "partner_id"], how="left")

    frame["starter_known"] = frame["feat_starting_goalie"].notna() if \
        "feat_starting_goalie" in frame.columns else False
    return frame.reset_index(drop=True)
