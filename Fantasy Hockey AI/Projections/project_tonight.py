#!/usr/bin/env python
"""Scores tonight's rows (ModelFeatures/build_tonight.py) and applies what the live reports know
that the models do not:

- skaters: the per-game chain (models/<season>/skaters/B, the deployment build) -> p_plays and
  the lambdas, exactly as predict.project scores a feature table. A questionable player (DTD, or
  a game-time decision on his team's chart) has p_plays capped at Settings/live.json's
  `questionable_p_plays_cap` -- the models never saw either status.
- goalies: P(start) (models/<season>/goalie_start) over the season's goalie history with
  tonight's candidates appended, the same as-of features goalie_starts builds. The model never
  sees a starter report, so a Daily Faceoff report overrides it: the named goalie gets
  `goalie_report_p_start[strength]` and his partner(s) share the rest in proportion to the
  model. A questionable goalie without a report is capped like a skater, and the team-game is
  renormalized.

Writes reports/live/tonight_{date}.parquet: one row per candidate, skaters and goalies, with the
model's number beside the adjusted one and a note saying what changed it.

Usage:
    python project_tonight.py                  # today
    python project_tonight.py --date 2026-10-01
"""

import argparse
import datetime as dt
import json
import logging

import lightgbm as lgb
import numpy as np
import pandas as pd

import goalie_starts
import paths
import predict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("project_tonight")

LIVE_FEATURES_DIR = paths.FEATURES_DIR.parent / "live"
LINEUPS_DIR = paths.FEATURES_DIR.parent / "lineups"
LIVE_SETTINGS = paths.PROJECT_ROOT.parent / "Settings" / "live.json"
OUT_DIR = paths.REPORTS_DIR / "live"


def season_of(game_date: dt.date) -> str:
    start = game_date.year if game_date.month >= 7 else game_date.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def load_settings() -> dict:
    settings = json.loads(LIVE_SETTINGS.read_text(encoding="utf-8"))
    return {"caps": {k: float(v) for k, v in settings["questionable_p_plays_cap"].items() if not k.startswith("_")},
            "reports": {k: float(v) for k, v in settings["goalie_report_p_start"].items() if not k.startswith("_")}}


def score_skaters(table: pd.DataFrame, season: str, questionable: dict, caps: dict) -> pd.DataFrame:
    predict.MODEL_DIR = paths.models_dir(season, "skaters", "B")
    table = table.copy()
    table["game_date"] = pd.to_datetime(table["game_date"])
    out = predict.project(table)
    out["p_plays_model"] = out["p_plays"]
    status = out["player_id"].map(lambda p: questionable.get(str(int(p))))
    cap = status.map(caps)
    capped = cap.notna() & (out["p_plays"] > cap)
    out.loc[capped, "p_plays"] = cap[capped]
    out["questionable"] = status
    out["note"] = np.where(capped, "capped: " + status.fillna("") , None)
    out["injured_at_lockout"] = table["injured_at_lockout"].fillna(False).astype(bool).to_numpy()
    out["kind"] = "skater"
    return out


def goalie_history(season: str) -> pd.DataFrame:
    """The season's goalie candidates so far (empty before the first game)."""
    if not (LINEUPS_DIR / f"features_A_{season}.parquet").exists() or \
            not (paths.FEATURES_DIR / f"goalie_starts_{season}.parquet").exists():
        log.warning("no %s goalie history parquets yet -- tonight's P(start) has no in-season history", season)
        return pd.DataFrame()
    return goalie_starts._candidates(season)


def score_goalies(goalies: pd.DataFrame, skaters: pd.DataFrame, context: pd.DataFrame, season: str,
                  questionable: dict, settings: dict) -> pd.DataFrame:
    rows = goalies.copy()
    rows["game_date"] = pd.to_datetime(rows["game_date"])
    for column in ("injured_at_lockout", "feat_starting_goalie"):
        rows[column] = rows[column].fillna(False).astype(bool)
    rows["label_dressed"] = False
    rows["label_starting_goalie"] = False
    rows["is_starter"] = False
    rows["appeared"] = False
    rows["season"] = season
    team_context = (skaters[["game_id", "team_id", "is_home", "days_rest", "is_back_to_back", "games_last_4d"]]
                    .drop_duplicates(["game_id", "team_id"]))
    rows = rows.merge(team_context, on=["game_id", "team_id"], how="left")
    rows["is_home"] = rows["is_home"].fillna(0).astype(bool)

    history = goalie_history(season)
    if len(history):
        # Only games before tonight: live there are no others; replaying a past date there are.
        history = history[history["game_date"] < rows["game_date"].min()]
    table = pd.concat([history, rows], ignore_index=True) if len(history) else rows
    table = goalie_starts._within_team_game(goalie_starts._as_of(table))
    table = table[table["game_id"] < 0].copy()   # tonight's placeholders only

    models = goalie_starts.models_dir_for(season)
    booster = lgb.Booster(model_file=str(models / "goalie_start.txt"))
    table["p_start_raw"] = booster.predict(goalie_starts._matrix(table))
    table = goalie_starts.normalize(table)
    table["p_start_model"] = table["p_start"]
    table["note"] = None

    reports = context.set_index(["game_id", "team_id"])[["report_goalie", "report_strength"]]
    for (game_id, team_id), group in table.groupby(["game_id", "team_id"]):
        index = group.index
        named, strength = reports.loc[(game_id, team_id)] if (game_id, team_id) in reports.index else (None, None)
        rate = settings["reports"].get(strength) if strength else None
        if rate is not None and named is not None and not pd.isna(named) and (group["player_id"] == named).any():
            is_named = table.loc[index, "player_id"] == named
            others = table.loc[index[~is_named.to_numpy()], "p_start_model"]
            share = others / others.sum() if others.sum() > 0 else pd.Series(1.0 / max(len(others), 1), index=others.index)
            table.loc[index[is_named.to_numpy()], "p_start"] = rate
            table.loc[others.index, "p_start"] = (1.0 - rate) * share
            table.loc[index, "note"] = f"report: {strength}"
            continue
        status = table.loc[index, "player_id"].map(lambda p: questionable.get(str(int(p))))
        cap = status.map(settings["caps"])
        over = cap.notna() & (table.loc[index, "p_start"] > cap)
        if over.any():
            capped_ids = over[over].index
            freed = float((table.loc[capped_ids, "p_start"] - cap[over]).sum())
            table.loc[capped_ids, "p_start"] = cap[over]
            rest = index.difference(capped_ids)
            if len(rest) and table.loc[rest, "p_start"].sum() > 0:
                weights = table.loc[rest, "p_start"] / table.loc[rest, "p_start"].sum()
                table.loc[rest, "p_start"] += freed * weights
            table.loc[capped_ids, "note"] = "capped: " + status[over]
    table["questionable"] = table["player_id"].map(lambda p: questionable.get(str(int(p))))
    table["kind"] = "goalie"
    return table[["season_id", "game_id", "game_date", "team_id", "player_id", "position", "kind",
                  "p_start", "p_start_model", "injured_at_lockout", "questionable", "note"]]


def main():
    parser = argparse.ArgumentParser(description="Score tonight's rows and apply the live reports")
    parser.add_argument("--date", default=None, help="game date (default: today on this PC)")
    args = parser.parse_args()
    game_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    season = season_of(game_date)
    stem = f"tonight_{game_date.isoformat()}"
    skaters = pd.read_parquet(LIVE_FEATURES_DIR / f"{stem}_skaters.parquet")
    goalies = pd.read_parquet(LIVE_FEATURES_DIR / f"{stem}_goalies.parquet")
    context = pd.read_parquet(LIVE_FEATURES_DIR / f"{stem}_context.parquet")
    questionable = json.loads((LIVE_FEATURES_DIR / f"{stem}_questionable.json").read_text(encoding="utf-8"))
    settings = load_settings()

    skater_out = score_skaters(skaters, season, questionable, settings["caps"])
    goalie_out = score_goalies(goalies, skaters, context, season, questionable, settings)
    out = pd.concat([skater_out, goalie_out], ignore_index=True)
    out = out.merge(context[["game_id", "team_id", "nhl_game_id", "start_time_utc", "lineup_source"]],
                    on=["game_id", "team_id"], how="left")
    paths.ensure(OUT_DIR)
    path = OUT_DIR / f"{stem}.parquet"
    out.to_parquet(path, index=False)
    log.info("%s: %d skaters (%d capped), %d goalies (%d adjusted) -> %s", game_date, len(skater_out),
             int(skater_out["note"].notna().sum()), len(goalie_out), int(goalie_out["note"].notna().sum()), path)


if __name__ == "__main__":
    main()
