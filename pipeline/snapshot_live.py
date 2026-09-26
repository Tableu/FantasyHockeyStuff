#!/usr/bin/env python
"""Takes one live snapshot into the Live schema -- injury reports, line charts or starting
goalies -- for the lineup-lock decisions (see nhl_pipeline/ingest/live_snapshots.py for the
tables and why they are change logs). Run by Windows Task Scheduler (schedule_live_tasks.ps1).

Each source is its own transaction: a failing source is recorded in Live.SnapshotRuns with its
error and does not stop the others.

    injuries   Fleaflicker league injury designations (OUT / IR) + ESPN's injury report
    lines      Daily Faceoff's 32 team line charts, and the injury / GTD flags on them
    goalies    Daily Faceoff's starting goalies for a date (default: today, this PC's date).
               Skips the fetch when every game that day has started, unless --force.

Usage:
    python snapshot_live.py --kind injuries
    python snapshot_live.py --kind lines --dry-run          # fetch + parse + diff, rolled back
    python snapshot_live.py --kind goalies --date 2026-10-01
"""

import argparse
import datetime as dt
import logging

from nhl_pipeline import db, fantasy_leagues, name_resolver
from nhl_pipeline.api import dailyfaceoff, espn_injuries, fleaflicker
from nhl_pipeline.ingest import live_snapshots as live
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("snapshot_live")

SEASON = "2026-27"


def run_source(conn, kind: str, source_key: str, work, dry_run: bool) -> None:
    """One source, one transaction: `work(cursor, run_id)` returns (seen, written)."""
    cursor = conn.cursor()
    source = live.source_id(cursor, source_key)
    at = live.utc_now()
    try:
        run_id = live.start_run(cursor, kind, source, at)
        seen, written = work(cursor, run_id, source)
        live.finish_run(cursor, run_id, seen, written)
    except Exception as exc:  # recorded, then the next source runs
        conn.rollback()
        log.exception("%s/%s failed", kind, source_key)
        if not dry_run:
            live.record_failure(cursor, kind, live.source_id(cursor, source_key), at,
                                f"{type(exc).__name__}: {exc}")
            conn.commit()
        return
    if dry_run:
        conn.rollback()
        log.info("%s/%s: %d seen, %d would be written (dry run, rolled back)", kind, source_key, seen, written)
    else:
        conn.commit()
        log.info("%s/%s: %d seen, %d written", kind, source_key, seen, written)


def snapshot_injuries(conn, season_id, teams, player_index, dry_run):
    def fleaflicker_work(cursor, run_id, source):
        # Designations are the platform's, not the league's: read through the registry's active
        # Fleaflicker league (today 12090).
        rows = fleaflicker.injuries(fleaflicker.get_players(fantasy_leagues.fleaflicker_league_id()))
        resolver = live.fleaflicker_resolver(cursor, season_id, player_index)
        status = live.fleaflicker_status_rows(rows, teams, resolver)
        _warn_unresolved("Fleaflicker", status)
        return len(status), live.write_status(cursor, run_id, source, status)

    def espn_work(cursor, run_id, source):
        rows = espn_injuries.get_injuries()
        resolver = live.injuries_resolver(cursor, source, player_index, "espn")
        status = live.espn_status_rows(rows, teams, resolver)
        _warn_unresolved("ESPN", status)
        return len(status), live.write_status(cursor, run_id, source, status)

    run_source(conn, "injuries", "fleaflicker", fleaflicker_work, dry_run)
    run_source(conn, "injuries", "espn", espn_work, dry_run)


def snapshot_lines(conn, teams, player_index, dry_run):
    def work(cursor, run_id, source):
        resolver = live.injuries_resolver(cursor, source, player_index, "dailyfaceoff")
        charts = []
        for slug in dailyfaceoff.team_slugs():
            try:
                chart = dailyfaceoff.line_chart(slug)
            except Exception:
                log.exception("Daily Faceoff chart %s failed; its players keep their last status", slug)
                continue
            chart["team_id"] = teams.from_abbreviation(chart["team_abbreviation"]) or \
                teams.from_name(chart["team_name"])
            if chart["team_id"] is None:
                log.warning("Daily Faceoff team %s (%s) not matched, skipped", chart["team_name"],
                            chart["team_abbreviation"])
                continue
            # Special-teams rows carry a unit slot (sk1-sk5) where the position goes, so a player's
            # position for the same-name tiebreak is the one his even-strength row gives.
            positions = {}
            for p in chart["players"]:
                if p["position"] and not p["position"].startswith("sk"):
                    positions.setdefault(p["external_id"], p["position"])
            for p in chart["players"]:
                p["player_id"] = resolver.resolve(p["external_id"], p["name"],
                                                  positions.get(p["external_id"]))
            charts.append(chart)
        unresolved = sorted({p["name"] for c in charts for p in c["players"] if p["player_id"] is None})
        if unresolved:
            log.warning("Daily Faceoff: %d unresolved names: %s", len(unresolved), unresolved)
        written = live.write_charts(cursor, run_id, source, charts)
        status = live.dfo_status_rows(charts, resolver)
        written += live.write_status(cursor, run_id, source, status,
                                     covered_team_ids={c["team_id"] for c in charts})
        return sum(len(c["players"]) for c in charts), written

    run_source(conn, "lines", "dailyfaceoff", work, dry_run)


def snapshot_goalies(conn, teams, player_index, game_date: dt.date, force: bool, dry_run):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM Reference.Schedule WHERE GameDate = ? AND "
                   "(StartTimeUTC IS NULL OR StartTimeUTC > SYSUTCDATETIME())", game_date)
    remaining = cursor.fetchone()[0]
    if not force and remaining == 0:
        log.info("goalies: no game left to start on %s, nothing fetched", game_date)
        return

    def work(cursor, run_id, source):
        resolver = live.injuries_resolver(cursor, source, player_index, "dailyfaceoff")
        games = live.schedule_games(cursor, game_date)
        rows = []
        for r in dailyfaceoff.starting_goalies(game_date.isoformat()):
            r["game_date"] = dt.date.fromisoformat(r["date"])
            r["team_id"] = teams.from_name(r["team_name"])
            home, away = teams.from_name(r["home_team_name"]), teams.from_name(r["away_team_name"])
            if r["team_id"] is None:
                log.warning("goalies: team %r not matched, skipped", r["team_name"])
                continue
            r["nhl_game_id"] = games.get((home, away))
            r["player_id"] = resolver.resolve(r["external_id"], r["name"], "g") if r["name"] else None
            rows.append(r)
        _warn_unresolved("Daily Faceoff goalies", [r for r in rows if r["name"]])
        return len(rows), live.write_goalie_reports(cursor, run_id, source, rows)

    run_source(conn, "goalies", "dailyfaceoff", work, dry_run)


def merge_status(conn, dry_run: bool) -> None:
    """Recompute the merged per-player status (Live.PlayerStatus) after an injuries or lines run."""
    cursor = conn.cursor()
    written = live.write_player_status(cursor, live.utc_now())
    if dry_run:
        conn.rollback()
        log.info("merged status: %d would change (dry run, rolled back)", written)
    else:
        conn.commit()
        log.info("merged status: %d changed", written)


def _warn_unresolved(label: str, rows: list) -> None:
    missing = sorted({r["name"] for r in rows if r["player_id"] is None})
    if missing:
        log.warning("%s: %d unresolved names (add aliases): %s", label, len(missing), missing)


def main():
    parser = argparse.ArgumentParser(description="Take one live snapshot into the Live schema")
    parser.add_argument("--kind", required=True, choices=["injuries", "lines", "goalies"])
    parser.add_argument("--date", default=None, help="goalies: the game date (default: today)")
    parser.add_argument("--force", action="store_true", help="goalies: fetch even if every game has started")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    cursor = conn.cursor()
    first = int(SEASON[:4])
    season_id = ensure_season(cursor, {"SeasonID_NHL": first * 10000 + first + 1, "DisplayName": SEASON})
    live.ensure_tables(cursor)
    conn.commit()
    teams = live.Teams(cursor, season_id)
    player_index = name_resolver.load_player_index(cursor)

    if args.kind == "injuries":
        snapshot_injuries(conn, season_id, teams, player_index, args.dry_run)
        merge_status(conn, args.dry_run)
    elif args.kind == "lines":
        snapshot_lines(conn, teams, player_index, args.dry_run)
        merge_status(conn, args.dry_run)
    else:
        game_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        snapshot_goalies(conn, teams, player_index, game_date, args.force, args.dry_run)


if __name__ == "__main__":
    main()
