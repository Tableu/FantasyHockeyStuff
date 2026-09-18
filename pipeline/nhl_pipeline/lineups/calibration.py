"""How much a team's deployment moves between consecutive games -- the rates perturb.py
applies to an actual opening lineup to mimic a pre-game line chart's errors.

Game N-1 -> N churn is a deliberate OVER-estimate of that error: a pre-game chart already
knows tonight's scratches, call-ups and most line shuffles, and only misses the late ones,
so noise at these rates makes the projection models trust lineup features *less* than the
live feed deserves -- the safe direction (a backtest that understates live accuracy). They
are the stand-in until the live Daily Faceoff snapshot job has run long enough to measure
the real chart-vs-opening discrepancy, which then replaces them (docs/fantasy-ai/
data-sources.md). Every rate is conditional on what a chart could not have known: a
scratch is only counted when the player wasn't in an Injuries.Spells spell on game day.
"""

from collections import Counter

from nhl_pipeline.lineups import store


def _rate(hits: int, tries: int) -> float | None:
    return round(hits / tries, 4) if tries else None


def measure_churn(cursor, season_ids: list) -> dict:
    team_games = store.load_team_games(cursor, season_ids)
    spells = store.load_injury_spells(cursor, season_ids)

    n = Counter()
    line_moves = Counter()   # |delta line| for forwards whose line number changed
    for team_id, games in team_games.items():
        for prev, cur in zip(games, games[1:]):
            if prev.season_id != cur.season_id:
                continue
            n["team_game_pairs"] += 1

            for p, pl in prev.players.items():
                if pl.is_goalie or not pl.dressed:
                    continue
                if store.injured_on(spells, team_id, p, cur.game_date):
                    n["healthy_skaters_injured_next"] += 1
                    continue
                n["healthy_skaters"] += 1
                now = cur.players.get(p)
                if now is None or not now.dressed:
                    n["scratched"] += 1
                    continue

                if pl.is_forward and pl.line and now.line:
                    n["forwards_with_line_both"] += 1
                    if prev.mates(p, "line") != cur.mates(p, "line"):
                        n["forward_mates_changed"] += 1
                    if pl.line != now.line:
                        n["forward_line_changed"] += 1
                        line_moves[abs(pl.line - now.line)] += 1
                if pl.is_defence and pl.pair and now.pair:
                    n["defence_with_pair_both"] += 1
                    if prev.mates(p, "pair") != cur.mates(p, "pair"):
                        n["defence_partner_changed"] += 1
                    if pl.pair != now.pair:
                        n["defence_pair_changed"] += 1

                if prev.had_pp and cur.had_pp:
                    n["skaters_pp_known_both"] += 1
                    if pl.pp != now.pp:
                        n["pp_unit_changed"] += 1
                    if pl.pp == 1 and now.pp != 1:
                        n["pp1_dropped"] += 1
                    if pl.pp == 2 and now.pp == 1:
                        n["pp2_promoted"] += 1
                if prev.had_pk and cur.had_pk:
                    n["skaters_pk_known_both"] += 1
                    if pl.pk != now.pk:
                        n["pk_unit_changed"] += 1

            # Who came IN: dressed now, not dressed last game (call-ups, returns, scratches undone).
            for p, now in cur.players.items():
                if now.is_goalie or not now.dressed:
                    continue
                before = prev.players.get(p)
                if before is None or not before.dressed:
                    n["skaters_in"] += 1

            prev_starter = next((p for p, pl in prev.players.items() if pl.starting_goalie), None)
            cur_starter = next((p for p, pl in cur.players.items() if pl.starting_goalie), None)
            if prev_starter and cur_starter:
                n["goalie_pairs"] += 1
                if prev_starter != cur_starter:
                    n["goalie_switched"] += 1

    total_moves = sum(line_moves.values())
    return {
        "team_game_pairs": n["team_game_pairs"],
        "scratch_rate": _rate(n["scratched"], n["healthy_skaters"]),
        "skaters_in_per_game": _rate(n["skaters_in"], n["team_game_pairs"]),
        "forward_mates_changed_rate": _rate(n["forward_mates_changed"], n["forwards_with_line_both"]),
        "forward_line_changed_rate": _rate(n["forward_line_changed"], n["forwards_with_line_both"]),
        "forward_line_move_distribution": {str(k): _rate(v, total_moves) for k, v in sorted(line_moves.items())},
        "defence_partner_changed_rate": _rate(n["defence_partner_changed"], n["defence_with_pair_both"]),
        "defence_pair_changed_rate": _rate(n["defence_pair_changed"], n["defence_with_pair_both"]),
        "pp_unit_changed_rate": _rate(n["pp_unit_changed"], n["skaters_pp_known_both"]),
        "pp1_dropped_rate": _rate(n["pp1_dropped"], n["skaters_pp_known_both"]),
        "pp2_promoted_rate": _rate(n["pp2_promoted"], n["skaters_pp_known_both"]),
        "pk_unit_changed_rate": _rate(n["pk_unit_changed"], n["skaters_pk_known_both"]),
        "goalie_switch_rate": _rate(n["goalie_switched"], n["goalie_pairs"]),
        "counts": dict(n),
    }


def perturb_rates(churn: dict) -> dict:
    """Turns measure_churn() output into perturb.perturb() per-game edit counts. Each edit
    touches several players, so the counts are backed out from the per-player rates: a
    forward swap gives new linemates to the two swapped players and their four mates (6 of
    12 forwards) and a new line NUMBER to just the two; a whole-unit reorder renumbers 6; a
    D swap touches 4 of 6 partners and 2 pair numbers, a reorder 4; a special-teams
    promotion or substitution changes 2 players' unit. Approximate by design -- these are
    upper-bound stand-ins (module docstring), checked against 1,000 draws in the tests."""
    f_mates = churn["forward_mates_changed_rate"] or 0
    f_line = churn["forward_line_changed_rate"] or 0
    d_partner = churn["defence_partner_changed_rate"] or 0
    d_pair = churn["defence_pair_changed_rate"] or 0
    pp_promos = 5 * (churn["pp2_promoted_rate"] or 0)
    pk_promos = 4 * (churn["pk_unit_changed_rate"] or 0) / 2
    f_swaps = 12 * f_mates / 6
    d_swaps = 6 * d_partner / 4
    return {
        "scratch_per_skater": churn["scratch_rate"] or 0,
        "forward_swaps_per_game": round(f_swaps, 3),
        "forward_line_move_distribution": churn["forward_line_move_distribution"] or {"1": 1.0},
        "defence_swaps_per_game": round(d_swaps, 3),
        "forward_unit_reorders_per_game": round(max(0.0, (12 * f_line - 2 * f_swaps) / 6), 3),
        "defence_unit_reorders_per_game": round(max(0.0, (6 * d_pair - 2 * d_swaps) / 4), 3),
        "pp_promotions_per_game": round(pp_promos, 3),
        "pk_promotions_per_game": round(pk_promos, 3),
        "pp_substitutions_per_game": round(max(0.0, (18 * (churn["pp_unit_changed_rate"] or 0) - 2 * pp_promos) / 2), 3),
        "pk_substitutions_per_game": round(max(0.0, (18 * (churn["pk_unit_changed_rate"] or 0) - 2 * pk_promos) / 2), 3),
    }
