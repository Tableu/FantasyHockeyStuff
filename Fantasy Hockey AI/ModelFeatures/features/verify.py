"""Checks the built table against the database and against hockey sense.

The point of most of these is that they can *fail*. The leakage check in particular
recomputes features straight from SQL for a random sample of rows, using only games dated
before the row's game -- so it catches a windowing mistake that the in-code assertion in
base.py would happily agree with. Every check prints its own numbers rather than only a
verdict, because a passing check with an implausible number is still a bug.
"""

import logging

import pandas as pd

log = logging.getLogger("verify")

SAMPLE_ROWS = 200


def _report(name: str, ok: bool, detail: str) -> bool:
    log.info("%s %-22s %s", "PASS" if ok else "FAIL", name, detail)
    return ok


def shape(table: pd.DataFrame, base: pd.DataFrame, candidates: pd.DataFrame) -> bool:
    keys = ["game_id", "team_id", "player_id"]
    rows_match = len(base) == len(candidates)
    duplicates = int(base.duplicated(keys).sum())
    copies = table["copy_index"].nunique() if "copy_index" in table else 1
    expected = len(candidates) * copies
    ok = rows_match and duplicates == 0 and len(table) == expected
    return _report("shape", ok,
                   f"base {len(base):,} vs candidates {len(candidates):,}, duplicate keys {duplicates}, "
                   f"table {len(table):,} = {copies} copy/ies x {len(candidates):,}")


def history_coverage(base: pd.DataFrame) -> bool:
    covered = base["source_game_date"].notna().mean()
    # Rows without history are genuine: a player's first game of the season, or a call-up who
    # has not played yet. They should be a small minority.
    ok = 0.90 <= covered <= 1.0
    return _report("history coverage", ok, f"{covered:.1%} of rows have prior-game history")


def leakage_assertion(base: pd.DataFrame) -> bool:
    known = base["source_game_date"].notna()
    bad = int((base.loc[known, "source_game_date"] >= base.loc[known, "game_date"]).sum())
    return _report("leakage (in-table)", bad == 0,
                   f"{bad} row(s) with source_game_date >= game_date")


def leakage_recomputed(cursor, base: pd.DataFrame, season_id: int, sample: int = SAMPLE_ROWS) -> bool:
    """Recompute three features from SQL for a random sample, using only earlier games."""
    rows = base[base["source_game_date"].notna()].sample(min(sample, len(base)), random_state=0)
    mismatches = []

    for row in rows.itertuples(index=False):
        cursor.execute("""
            SELECT TOP 10 pgs.Shots, pgs.TimeOnIceSeconds
            FROM Stats.PlayerGameStats pgs
            JOIN Game.Games g ON g.GameID = pgs.GameID
            WHERE pgs.PlayerID = ? AND g.SeasonID = ? AND g.GameDate < ?
            ORDER BY g.GameDate DESC, g.GameID DESC
        """, row.player_id, season_id, row.game_date.date())
        history = cursor.fetchall()
        expected_shots = sum(h[0] for h in history)
        expected_toi_l5 = sum(h[1] for h in history[:5])

        cursor.execute("""
            SELECT TOP 10 ISNULL(x.xga, 0) AS xga
            FROM (
                SELECT g.GameID, g.GameDate,
                       (SELECT SUM(ISNULL(xg.ExpectedGoals, 0))
                        FROM Game.Shots s
                        LEFT JOIN Analytics.ShotExpectedGoals xg ON xg.ShotID = s.ShotID
                        WHERE s.GameID = g.GameID AND s.TeamID <> ?
                          AND s.ShotEventType IN ('shot-on-goal','missed-shot','goal')) AS xga
                FROM Game.Games g
                WHERE g.SeasonID = ? AND g.GameDate < ? AND ? IN (g.HomeTeamID, g.AwayTeamID)
            ) x
            ORDER BY x.GameDate DESC, x.GameID DESC
        """, row.team_id, season_id, row.game_date.date(), row.team_id)
        expected_team_xga = sum(r[0] for r in cursor.fetchall())

        for name, expected, actual in (
            ("shots_l10", expected_shots, row.shots_l10),
            ("toi_l5", expected_toi_l5, row.toi_l5),
            ("team_xga_l10", expected_team_xga, row.team_xga_l10),
        ):
            if pd.isna(actual) or abs(float(actual) - float(expected)) > 0.01:
                mismatches.append((name, row.game_id, row.player_id, expected, actual))

    return _report("leakage (recomputed)", not mismatches,
                   f"{len(rows)} sampled rows x 3 features; {len(mismatches)} mismatch(es)"
                   + (f", e.g. {mismatches[0]}" if mismatches else ""))


def zone_starts(cursor, base: pd.DataFrame, table: pd.DataFrame, season_id: int) -> bool:
    """Zone starts must partition the player's on-ice faceoffs -- every faceoff he was on the
    ice for lands in exactly one of O/D/N -- and deployment must show through: first-line
    forwards start in the offensive zone more often than fourth-liners."""
    from features import extract

    zones = extract.zone_starts(cursor, [season_id])
    zones["zone_total"] = zones[["oz_starts", "dz_starts", "nz_starts"]].sum(axis=1)

    cursor.execute("""
        SELECT sh.GameID AS game_id, sh.PlayerID AS player_id, COUNT(*) AS on_ice_faceoffs
        FROM Game.Plays p
        JOIN Game.Games g ON g.GameID = p.GameID
        JOIN Game.Shifts sh ON sh.GameID = p.GameID AND sh.PeriodNumber = p.PeriodNumber
         AND sh.ShiftStartSeconds <= p.PeriodTimeSeconds AND sh.ShiftEndSeconds > p.PeriodTimeSeconds
        WHERE g.SeasonID = ? AND p.EventType = 'faceoff' AND p.XCoordinate IS NOT NULL
        GROUP BY sh.GameID, sh.PlayerID
    """, season_id)
    counted = pd.DataFrame.from_records(cursor.fetchall(), columns=["game_id", "player_id", "on_ice_faceoffs"])

    merged = zones.merge(counted, on=["game_id", "player_id"], how="outer", indicator=True)
    unmatched = int((merged["_merge"] != "both").sum())
    disagreements = int((merged["zone_total"] != merged["on_ice_faceoffs"]).sum())
    partition_ok = unmatched == 0 and disagreements == 0

    forwards = table[table["position"].isin(["C", "L", "R"]) & table["oz_start_pct_std"].notna()]
    top = forwards.loc[forwards["feat_line"] == 1, "oz_start_pct_std"].mean()
    bottom = forwards.loc[forwards["feat_line"] == 4, "oz_start_pct_std"].mean()
    ordered = bool(top > bottom)
    return _report("zone starts", partition_ok and ordered,
                   f"{len(merged):,} player-games, {unmatched} unmatched, {disagreements} disagreeing; "
                   f"line-1 OZ% {top:.3f} vs line-4 {bottom:.3f}")


def arena_factors(base: pd.DataFrame) -> bool:
    late = base[base["team_gp_std"] >= 40]
    hit = late["arena_hit_factor"]
    ok = bool(0.9 <= hit.mean() <= 1.1) and hit.std() > 0
    return _report("arena factors", ok,
                   f"late-season hit factor mean {hit.mean():.3f}, sd {hit.std():.3f}, "
                   f"range {hit.min():.2f}-{hit.max():.2f}")


def sanity_correlations(table: pd.DataFrame) -> bool:
    """A rate times expected ice time should predict the counting stat."""
    played = table[table["target_played"] & table["mean_toi_l10"].notna()].copy()
    results = {}
    for stat in ("shots", "hits", "blocks", "goals"):
        rate = played.get(f"{stat}_p60_l20")
        if rate is None:
            continue
        expected = rate * played["mean_toi_l10"] / 3600.0
        results[stat] = float(expected.corr(played[f"target_{stat}"]))
    ok = results.get("shots", 0) > 0.4 and results.get("hits", 0) > 0.4 and results.get("blocks", 0) > 0.4
    return _report("sanity correlations", ok,
                   ", ".join(f"{k} r={v:.3f}" for k, v in results.items()))


def lineup_agreement(table: pd.DataFrame) -> bool:
    """Variant A's dressed flag should agree with what happened about as often as the lineup
    features themselves report (~91%)."""
    if "feat_dressed" not in table or "label_dressed" not in table:
        return _report("lineup agreement", True, "no lineup columns to compare")
    agree = (table["feat_dressed"].fillna(False).astype(bool)
             == table["label_dressed"].fillna(False).astype(bool)).mean()
    return _report("lineup agreement", 0.80 <= agree <= 0.99, f"feat_dressed == label_dressed for {agree:.1%}")


def prior_season(table: pd.DataFrame, has_prior: bool) -> bool:
    """`prev_` is reserved for prior-season features -- a schedule column once called
    prev_game_was_home made this check pass when it should have failed."""
    columns = [c for c in table.columns if c.startswith("prev_")]
    if not columns:
        return _report("prior season", not has_prior, "no prev_* columns present")
    filled = table[columns].notna().any(axis=1).mean()
    ok = (filled > 0.5) if has_prior else (filled == 0)
    return _report("prior season", ok,
                   f"{filled:.1%} of rows have prior-season features (prior ingested: {has_prior})")


def copy_consistency(table: pd.DataFrame) -> bool:
    """Variant B only: across copies of the same candidate, the base features must be
    identical (they do not depend on the lineup) while the lineup features must actually
    differ somewhere (otherwise the perturbation did nothing)."""
    if "copy_index" not in table or table["copy_index"].nunique() < 2:
        return _report("copy consistency", True, "single copy -- nothing to compare")

    keys = ["game_id", "team_id", "player_id"]
    base_sample = ["shots_p60_l20", "mean_toi_l10", "team_xgf_p60_l10", "days_rest", "arena_hit_factor"]
    base_sample = [c for c in base_sample if c in table]
    varying_base = [c for c in base_sample if table.groupby(keys)[c].nunique(dropna=False).max() > 1]

    lineup_columns = [c for c in ("feat_line", "feat_pair", "feat_pp", "feat_dressed") if c in table]
    changed = [c for c in lineup_columns if table.groupby(keys)[c].nunique(dropna=False).max() > 1]

    ok = not varying_base and bool(changed)
    return _report("copy consistency", ok,
                   f"base columns varying across copies: {varying_base or 'none'}; "
                   f"lineup columns that differ between copies: {changed or 'none'}")


def one_team_per_player_game(table: pd.DataFrame) -> bool:
    """Nobody plays for two teams in one game.

    A candidate may legitimately appear for two clubs on the same night -- a player traded
    that week is still in his old team's pool -- but at most one of those rows can have him
    actually playing. This used to fail: the target join omitted `team_id`, so the phantom
    row inherited the stat line from the team he was really on.
    """
    played = table.loc[table["target_played"].astype(bool), ["game_id", "player_id", "team_id"]]
    teams = played.drop_duplicates().groupby(["game_id", "player_id"])["team_id"].nunique()
    offenders = int((teams > 1).sum())
    candidates = table[["game_id", "player_id", "team_id"]].drop_duplicates()
    dual = int((candidates.groupby(["game_id", "player_id"])["team_id"].nunique() > 1).sum())
    return _report("one team per game", offenders == 0,
                   f"{dual} candidate(s) listed for two teams (legitimate after a trade); "
                   f"{offenders} of them played for both")


def dressed_agrees_with_played(table: pd.DataFrame) -> bool:
    """A player the lineup says did not dress cannot have taken a shift."""
    if "label_dressed" not in table.columns:
        return _report("dressed vs played", True, "no label_dressed column -- skipped")
    contradictions = int((~table["label_dressed"].fillna(False).astype(bool)
                          & table["target_played"].astype(bool)).sum())
    share = contradictions / max(len(table), 1)
    return _report("dressed vs played", share < 0.001,
                   f"{contradictions} row(s) not dressed yet played ({share:.3%})")


def injury_flag_knowable(table: pd.DataFrame) -> bool:
    """`injured_at_lockout` flags only absences the lockout could know: a spell that had already
    cost the player a game. His spell's first game is in `label_in_spell` but not in the flag.

    The flag used to be the realized spell, so it was set on the first missed game too -- 885
    rows in 2025-26, 100% right, and P(plays)' second-strongest feature. Checked on one copy of
    each candidate row, in team-game order: an in-spell row must be flagged exactly when his
    previous team game was also in the spell.
    """
    if "label_in_spell" not in table.columns:
        return _report("injury flag knowable", False, "no label_in_spell column -- rebuild the lineup features")
    rows = table
    if "copy_index" in rows.columns:
        rows = rows[rows["copy_index"] == 0]
    rows = rows.sort_values(["team_id", "player_id", "game_date", "game_id"], kind="mergesort")
    flag = rows["injured_at_lockout"].fillna(False).astype(bool)
    spell = rows["label_in_spell"].fillna(False).astype(bool)
    outside = int((flag & ~spell).sum())
    previous = spell.groupby([rows["team_id"], rows["player_id"]]).shift(1).fillna(False).astype(bool)
    first_games = spell & ~previous
    wrong = int((spell & (flag != previous)).sum())
    # A new spell starting the game after another ended looks "wrong" here but is not; allow a
    # sliver for it rather than false-fail.
    return _report("injury flag knowable", outside == 0 and wrong <= 0.001 * max(int(spell.sum()), 1),
                   f"{int(flag.sum())} flagged, {int(first_games.sum())} spell first games unflagged; "
                   f"{outside} flagged outside a spell, {wrong} in-spell rows flagged wrongly")


def run(cursor, table: pd.DataFrame, base: pd.DataFrame, candidates: pd.DataFrame, season_id: int) -> bool:
    from features import extract
    has_prior = extract.prior_season_ids(cursor, [season_id])[season_id] is not None

    log.info("--- verification ---")
    results = [
        shape(table, base, candidates),
        history_coverage(base),
        leakage_assertion(base),
        leakage_recomputed(cursor, base, season_id),
        zone_starts(cursor, base, table, season_id),
        arena_factors(base),
        sanity_correlations(table),
        lineup_agreement(table),
        prior_season(table, has_prior),
        copy_consistency(table),
        one_team_per_player_game(table),
        dressed_agrees_with_played(table),
        injury_flag_knowable(table),
    ]
    passed = sum(1 for r in results if r)
    log.info("%d / %d checks passed", passed, len(results))
    return all(results)
