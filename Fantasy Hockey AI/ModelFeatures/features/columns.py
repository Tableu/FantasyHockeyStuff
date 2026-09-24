"""Descriptions for the feature table's columns, and the Markdown renderer for them.

Columns are generated combinatorially (stat x window x rate), so describing each one by hand
would rot immediately. Instead each *pattern* is described once and expanded against the
parquet's real schema, and `render()` asserts that every column in the file was matched -- so
a new feature that nobody documented fails the build instead of quietly shipping.
"""

import re

import pandas as pd

WINDOW_TEXT = {
    "l5": "previous 5 games played",
    "l10": "previous 10 games played",
    "l20": "previous 20 games played",
    "std": "season to date",
    "prev": "previous season",
}

# (regex, description) -- first match wins. {w} expands to the window's prose.
PATTERNS = [
    # keys and identity
    (r"^season_id$", "Reference.Seasons key."),
    (r"^game_id$", "Game.Games key of the game being predicted."),
    (r"^game_date$", "Date of the game being predicted."),
    (r"^team_id$", "The team this candidate would play for."),
    (r"^opp_team_id$", "The opposing team."),
    (r"^player_id$", "Reference.Players key."),
    (r"^position$", "Position code (C/L/R/D); goalies are excluded from this table."),
    (r"^is_home$", "1 if the candidate's team is at home."),
    (r"^variant$", "Lineup variant: A (previous game's lineup) or B (actual lineup + calibrated noise)."),
    (r"^copy_index$", "Which perturbed copy of the lineup this row uses (variant B); 0 for A."),
    (r"^source_game_id$", "The player's most recent game before this one, whose history feeds every window."),
    (r"^source_game_date$", "Date of that game. Asserted strictly earlier than game_date."),
    (r"^source_team_id$", "The team he played that game for (differs after a trade)."),

    # lineup features and labels
    (r"^feat_dressed$", "Did the source lineup have him dressed."),
    (r"^feat_(line|pair)$", "Forward line / defence pair in the source lineup."),
    (r"^feat_(pp|pk)$", "Power-play / penalty-kill unit in the source lineup."),
    (r"^feat_starting_goalie$", "Was he the source lineup's starting goalie (always false here)."),
    (r"^feat_(mate1|mate2|partner)_id$", "Line-mate / defence-partner player id from the source lineup."),
    (r"^(line|pair|pp|pk)_known$", "The source lineup knew his unit, as opposed to leaving it unknown."),
    (r"^lineup_age_days$", "Days between the source lineup's game and this one (variant A)."),
    (r"^injured_at_lockout$", "Known out at the lockout: inside an injury spell that had already cost him a game (a spell's first game is not knowable)."),
    (r"^age_years$", "Age on the game date, in years (Reference.Players.BirthDate; NaN where the bio is missing)."),
    (r"^label_in_spell$", "Label: inside an injury spell on the game date (realized absence, first game included)."),
    (r"^games_dressed_lookback$", "Games he dressed in the team's previous 10."),
    (r"^label_dressed$", "Target: did he actually dress."),
    (r"^label_(line|pair|pp|pk)$", "Target: his actual unit in this game."),
    (r"^label_starting_goalie$", "Target: did he actually start (goalies only)."),

    # teammate quality
    (r"^mate[12]_(.+)$", "That line-mate's own {1}."),
    (r"^partner_(.+)$", "The defence partner's own {1}."),
    (r"^linemates_(.+)$", "Mean of both line-mates' {1}."),

    # opposing goalie
    (r"^opp_goalie_player_id$", "The goalie the candidate is expected to face."),
    (r"^goalie_sv_pct_(\w+)$", "That goalie's save percentage over the {w}, entering this game."),
    (r"^goalie_gsax_per_shot_(\w+)$", "His goals saved above expected per shot over the {w}."),
    (r"^goalie_gp_(\w+)$", "Games he had played in the {w}."),

    # player rolling
    (r"^gp_(\w+)$", "Games the player played in the {w} -- how much history the window has."),
    (r"^mean_(toi|ev_toi|pp_toi|sh_toi)_(\w+)$", "Mean {1} seconds per game over the {w}."),
    (r"^mean_p1_ev_seconds_(\w+)$", "Mean period-1 even-strength seconds over the {w}: a depth-chart proxy."),
    (r"^mean_oi_cf_pct_5v5_(\w+)$", "Mean on-ice 5v5 Corsi-for share over the {w}."),
    (r"^mean_oi_xgf_pct_5v5_(\w+)$", "Mean on-ice 5v5 expected-goals share over the {w}."),
    (r"^mean_oi_sh_pct_5v5_(\w+)$", "Mean on-ice 5v5 shooting percentage over the {w}."),
    (r"^mean_oi_pdo_5v5_(\w+)$", "Mean on-ice 5v5 PDO over the {w}."),
    (r"^(ev|pp)_(\w+)_p60_(\w+)$", "{2} per 60 minutes of {1}-strength ice time over the {w}."),
    (r"^(\w+)_p60_(\w+)$", "{1} per 60 minutes of all-situations ice time over the {w}."),
    (r"^shooting_pct_(\w+)$", "Goals per shot over the {w}."),
    (r"^faceoff_pct_(\w+)$", "Faceoff win rate over the {w}."),
    (r"^oz_start_pct_(\w+)$", "Share of the player's on-ice faceoffs taken in the offensive zone over the {w}."),
    (r"^(oz|dz|nz)_starts_(\w+)$", "Offensive/defensive/neutral-zone faceoffs he was on the ice for, summed over the {w}."),
    (r"^(ev|pp|sh)_toi_trend$", "{1} ice time over the last 5 games minus the last 20: is his role rising."),
    (r"^(icf|iff|ixg)_(all|5v5|5v4|4v5)_(\w+)$", "Individual Corsi/Fenwick/expected goals at {2}, summed over the {w}."),
    (r"^(oi_cf_pct|oi_xgf_pct|oi_sh_pct|oi_pdo)_5v5_(\w+)$", "On-ice 5v5 rate summed over the {w} (prefer the mean_ version)."),
    (r"^(toi|ev_toi|pp_toi|sh_toi|p1_ev_seconds)_(\w+)$", "Total {1} seconds over the {w}."),
    (r"^(goals|assists|points|shots|hits|blocks|giveaways|takeaways|pim|ppp|shp|faceoff_wins|faceoff_losses)_(\w+)$",
     "{1} over the {w}."),

    # prior season
    (r"^prev_gp$", "Games played in the previous season. NULL when that season is not ingested."),
    (r"^prev_(\w+)_p60$", "{1} per 60 minutes in the previous season."),
    (r"^prev_toi_per_game$", "Mean ice time per game in the previous season."),
    (r"^prev_(\w+)$", "{1} in the previous season."),

    # team / opponent / context
    (r"^team_(\w+)_p60_(\w+)$", "The team's {1} per 60 over the {w}, entering this game."),
    (r"^team_(\w+)_per_game_(\w+)$", "The team's {1} per game over the {w}."),
    (r"^team_pp_pct_(\w+)$", "The team's power-play conversion rate over the {w}."),
    (r"^team_gp_(\w+)$", "Games the team had played in the {w}."),
    (r"^team_(\w+)$", "The team's {1} entering this game."),
    (r"^opp_(\w+)_p60_(\w+)$", "The opponent's {1} per 60 over the {w}."),
    (r"^opp_(\w+)_per_game_(\w+)$", "The opponent's {1} per game over the {w}."),
    (r"^opp_pp_pct_(\w+)$", "The opponent's power-play conversion rate over the {w}."),
    (r"^opp_gp_(\w+)$", "Games the opponent had played in the {w}."),
    (r"^opp_days_rest$", "Days since the opponent's previous game."),
    (r"^opp_is_back_to_back$", "The opponent is playing the second of two games on consecutive days."),
    (r"^opp_games_last_4d$", "Games the opponent played in the previous 4 days."),
    (r"^opp_(\w+)$", "The opponent's {1} entering this game."),
    (r"^days_rest$", "Days since the candidate's team last played."),
    (r"^is_back_to_back$", "The team is playing the second of two games on consecutive days."),
    (r"^last_game_was_home$", "Was the team's previous game at home -- a crude travel proxy."),
    (r"^changed_venue_type$", "The team switched between home and away since its previous game."),
    (r"^games_last_(\d+)d$", "Games the team played in the previous {1} days."),
    (r"^start_hour_utc$", "Scheduled start hour in UTC (Reference.Schedule; Game.Games has none)."),
    (r"^arena_hit_factor$", "How many hits this rink's scorekeeper records per game relative to the league, season to date."),
    (r"^arena_block_factor$", "The same ratio for blocked shots."),
    (r"^injured_forwards$", "Forwards on the team inside an injury spell on the game date."),
    (r"^injured_defence$", "Defencemen on the team inside an injury spell."),
    (r"^injured_top6_forwards$", "Injured forwards whose last known forward line was 1 or 2."),
    (r"^injured_top4_defence$", "Injured defencemen whose last known pair was 1 or 2."),
    (r"^injured_pp1$", "Injured players whose last known power-play unit was 1."),

    # targets
    (r"^target_played$", "Target: did he take a shift in this game."),
    (r"^target_(\w+)$", "Target: his actual {1} in this game."),
]


def describe(column: str) -> str | None:
    for pattern, text in PATTERNS:
        match = re.match(pattern, column)
        if not match:
            continue
        description = text
        for index, group in enumerate(match.groups(), start=1):
            description = description.replace(f"{{{index}}}", str(group).replace("_", " "))
        window = next((g for g in match.groups()[::-1] if g in WINDOW_TEXT), None)
        return description.replace("{w}", WINDOW_TEXT.get(window, "window"))
    return None


def render(frame: pd.DataFrame, title: str, notes: str = "") -> str:
    """Markdown for every column in the frame. Raises if any column has no description, so
    the doc cannot silently fall behind the table."""
    undescribed = [c for c in frame.columns if describe(c) is None]
    if undescribed:
        raise AssertionError(f"{len(undescribed)} column(s) have no description, e.g. {undescribed[:8]}")

    lines = [f"# {title}", ""]
    if notes:
        lines += [notes, ""]
    lines += [f"{len(frame.columns)} columns, {len(frame):,} rows.", "",
              "| column | non-null | description |", "| --- | --- | --- |"]
    for column in frame.columns:
        filled = frame[column].notna().mean()
        lines.append(f"| `{column}` | {filled:.1%} | {describe(column)} |")
    return "\n".join(lines) + "\n"
