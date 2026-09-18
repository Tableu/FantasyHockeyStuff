"""The lockout-time lineup feature table for the projection models, in two variants that
bracket what the live Daily Faceoff feed will provide (docs/fantasy-ai/data-sources.md):

  A  previous game's opening lineup -- leak-proof, noisier than a pre-game chart
  B  this game's opening lineup put through perturb() -- the chart's likely error profile

Training the same model on each and scoring the B-model on A-features bounds how much is
being bet on the live feed's quality. Both variants carry the same columns, plus the actual
game-N lineup as labels (for P(plays) and for evaluating the variants), and one row per
(game, team, candidate) where the candidate pool is everyone lockout-knowable: skaters and
goalies who dressed for the team in its previous CANDIDATE_LOOKBACK games, anyone in an
Injuries.Spells spell for the team on game day, and -- so labels cover them -- anyone who
actually dressed (a fresh call-up shows up with empty A-features, which is exactly what a
chart-less model would have known about him).

Leakage rule, asserted in code: a variant-A feature only ever reads a game dated before
the target; variant B reads the target's own lineup and nothing later, and its healthy-extras
pool for scratches is the lookback pool minus the injured -- never a later game.
"""

import random

import pandas as pd

from nhl_pipeline.lineups import perturb, store

CANDIDATE_LOOKBACK = 10

FEATURE_COLUMNS = ["dressed", "line", "pair", "pp", "pk", "starting_goalie", "mate1_id", "mate2_id", "partner_id"]


def _last_known(history: list, player_id: int, attr: str, had_attr: str):
    """The player's most recent unit of that kind from a game where the team actually had
    that strength state (so "no PP unit" is a real fact, not an artefact of a penalty-free
    night). history is oldest -> newest."""
    for g in reversed(history):
        if getattr(g, had_attr) and player_id in g.players and g.players[player_id].dressed:
            return getattr(g.players[player_id], attr)
    return None


def _lineup_columns(source: store.TeamGame | None, player_id: int, history: list) -> dict:
    """Feature columns for one player from one lineup (previous game for A, perturbed target
    for B); history supplies last-known PP/PK when the source game had none."""
    empty = {c: None for c in FEATURE_COLUMNS}
    if source is None or player_id not in source.players:
        empty["dressed"] = False
        return empty
    pl = source.players[player_id]
    mates = sorted(source.mates(player_id, "line")) if pl.line else []
    partner = sorted(source.mates(player_id, "pair")) if pl.pair else []
    return {
        "dressed": pl.dressed,
        "line": pl.line,
        "pair": pl.pair,
        "pp": pl.pp if source.had_pp else _last_known(history, player_id, "pp", "had_pp"),
        "pk": pl.pk if source.had_pk else _last_known(history, player_id, "pk", "had_pk"),
        "starting_goalie": pl.starting_goalie,
        "mate1_id": mates[0] if len(mates) > 0 else None,
        "mate2_id": mates[1] if len(mates) > 1 else None,
        "partner_id": partner[0] if partner else None,
    }


def build_lineup_features(cursor, season_ids: list, variant: str, rates: dict | None = None,
                          copies: int = 1, seed: int = 0) -> pd.DataFrame:
    assert variant in ("A", "B")
    if variant == "B" and rates is None:
        raise ValueError("variant B needs perturb rates (calibration.perturb_rates)")
    team_games = store.load_team_games(cursor, season_ids)
    spells = store.load_injury_spells(cursor, season_ids)
    rng = random.Random(seed)

    rows = []
    for team_id, games in team_games.items():
        for i, target in enumerate(games):
            history = [g for g in games[max(0, i - CANDIDATE_LOOKBACK):i] if g.season_id == target.season_id]
            if not history:
                continue  # season opener: nothing lockout-knowable about this team's deployment yet
            prev = history[-1]
            assert prev.game_date < target.game_date, "variant-A source must predate the target"

            pool = {}
            for g in history:
                for p, pl in g.players.items():
                    if pl.dressed:
                        pool[p] = pl.position
            injured_today = {p for (t, p) in spells if t == team_id and store.injured_on(spells, team_id, p, target.game_date)}
            candidates = set(pool) | injured_today | set(target.players)
            positions = dict(pool)
            positions.update({p: pl.position for p, pl in target.players.items()})
            for p in injured_today - set(positions):
                positions[p] = None

            healthy_extras = {
                p: pos for p, pos in pool.items()
                if p not in target.players and p not in injured_today and pos != "G"
            }
            sources = [(0, prev)] if variant == "A" else [
                (k, perturb.perturb(target, healthy_extras, rates, rng)) for k in range(copies)
            ]

            for copy_index, source in sources:
                for p in candidates:
                    row = {
                        "season_id": target.season_id, "game_id": target.game_id, "nhl_game_id": target.nhl_game_id,
                        "game_date": target.game_date, "team_id": team_id, "player_id": p,
                        "position": positions.get(p), "variant": variant, "copy_index": copy_index,
                        "lineup_age_days": (target.game_date - prev.game_date).days if variant == "A" else 0,
                        "injured_at_lockout": p in injured_today,
                        "games_dressed_lookback": sum(1 for g in history if p in g.players and g.players[p].dressed),
                    }
                    row.update({f"feat_{k}": v for k, v in _lineup_columns(source, p, history).items()})
                    actual = target.players.get(p)
                    row.update({
                        "label_dressed": bool(actual and actual.dressed),
                        "label_line": actual.line if actual else None,
                        "label_pair": actual.pair if actual else None,
                        "label_pp": actual.pp if actual else None,
                        "label_pk": actual.pk if actual else None,
                        "label_starting_goalie": bool(actual and actual.starting_goalie),
                    })
                    rows.append(row)

    frame = pd.DataFrame(rows)
    for col in ("feat_line", "feat_pair", "feat_pp", "feat_pk", "feat_mate1_id", "feat_mate2_id", "feat_partner_id",
                "label_line", "label_pair", "label_pp", "label_pk"):
        frame[col] = frame[col].astype("Int64")
    return frame
