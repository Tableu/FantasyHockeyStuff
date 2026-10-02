#!/usr/bin/env python
"""Takes one live snapshot into the Live schema -- injury reports, line charts or starting
goalies -- for the lineup-lock decisions (see nhl_pipeline/ingest/live_snapshots.py for the
tables and why they are change logs). Run by the plan window while it is open (Fantasy Hockey
AI/Live/plan_gui.py, via planpass.py), or by hand; injuries are also scheduled at 10:00 and 15:00
(FantasyHockey-LiveInjuries, run_live_snapshot.cmd).

Each source is its own transaction: a failing source is recorded in Live.SnapshotRuns with its
error and does not stop the others.

    injuries   Fleaflicker league injury designations (OUT / IR) + ESPN's injury report. Fleaflicker's
               whole listing (44 calls) runs once every 20 h; the runs between read the rostered
               players plus, by id, everyone ESPN or Fleaflicker reports (about 5 calls)
    lines      Daily Faceoff's 32 team line charts, and the injury / GTD flags on them
    goalies    Daily Faceoff's starting goalies for a date (default: today, this PC's date).
               Skips the fetch when every game that day has started, unless --force.

Usage:
    python snapshot_live.py --kind injuries
    python snapshot_live.py --kind lines --dry-run          # fetch + parse + diff, rolled back
    python snapshot_live.py --kind goalies --date 2026-10-01
    python snapshot_live.py --kind injuries --max-age 30    # skip a source snapshotted < 30 min ago
    python snapshot_live.py --kind injuries --full-listing  # the whole Fleaflicker listing now
    python snapshot_live.py --kind injuries --targeted      # never the whole listing (plan window opening)
"""

import argparse
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from nhl_pipeline import db, fantasy_leagues, name_resolver
from nhl_pipeline.api import dailyfaceoff, espn_injuries, fleaflicker
from nhl_pipeline.ingest import live_snapshots as live
from nhl_pipeline.ingest.season import ensure_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("snapshot_live")

SEASON = "2026-27"
# Daily Faceoff's 32 line charts, fetched this many at a time (one after another took ~10 s).
CHART_WORKERS = 4
# The full Fleaflicker listing (44 calls) runs when the last one is this old; the runs between
# read only the players likely to be flagged (_fleaflicker_targeted, about 5 calls).
FULL_LISTING_EVERY = dt.timedelta(hours=20)
# Fewer rostered players than this is not a drafted league's whole roster read (268 on 12090).
ROSTERED_MIN = 200


def run_source(conn, kind: str, source_key: str, work, dry_run: bool, max_age=None) -> None:
    """One source, one transaction: `work(cursor, run_id)` returns (seen, written). With
    `max_age` (minutes), a source whose last successful snapshot of this kind is younger is
    skipped: its rows are still current."""
    cursor = conn.cursor()
    source = live.source_id(cursor, source_key)
    at = live.utc_now()
    if max_age is not None:
        cursor.execute("SELECT MAX(SnapshotAt) FROM Live.SnapshotRuns WHERE Kind = ? AND SourceID = ? "
                       "AND Error IS NULL AND RowsSeen IS NOT NULL", kind, source)
        last = cursor.fetchone()[0]
        if last is not None and at - last < dt.timedelta(minutes=max_age):
            log.info("%s/%s: skipped, last snapshot %d min ago (max age %d)", kind, source_key,
                     (at - last).total_seconds() // 60, max_age)
            return
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


def snapshot_injuries(conn, season_id, teams, player_index, dry_run, max_age=None, full_listing=False,
                      targeted_only=False):
    # ESPN is fetched once, first: its report also picks which players the Fleaflicker read asks about.
    try:
        espn_rows, espn_error = espn_injuries.get_injuries(), None
    except Exception as exc:
        espn_rows, espn_error = None, exc

    def fleaflicker_work(cursor, run_id, source):
        league_id = fantasy_leagues.fleaflicker_league_id()
        resolver = live.fleaflicker_resolver(cursor, season_id, player_index)
        last_full = live.last_full_read(cursor, "injuries", source)
        # `targeted_only` (the plan window opening): never the full listing for age or a failed ESPN
        # read -- only when there are no drafted rosters to read.
        reason = ("asked for" if full_listing else None if targeted_only
                  else "ESPN's report failed" if espn_rows is None
                  else "no full listing yet" if last_full is None
                  else f"last full listing {(live.utc_now() - last_full).total_seconds() / 3600:.0f} h ago"
                  if live.utc_now() - last_full >= FULL_LISTING_EVERY else None)
        targeted = None
        if reason is None:
            targeted = _fleaflicker_targeted(cursor, league_id, resolver, espn_rows, teams, player_index)
        if targeted is None:
            log.info("injuries/fleaflicker: full listing (%s)", reason or "rosters not drafted")
            rows, covered = _fleaflicker_full(league_id), None
        else:
            rows, covered = targeted
        live.set_scope(cursor, run_id, "full" if covered is None else "targeted")
        status = live.fleaflicker_status_rows(rows, teams, resolver)
        _warn_unresolved("Fleaflicker", status)
        return len(status), live.write_status(cursor, run_id, source, status, covered_external_ids=covered)

    def espn_work(cursor, run_id, source):
        if espn_error is not None:
            raise espn_error
        resolver = live.injuries_resolver(cursor, source, player_index, "espn")
        status = live.espn_status_rows(espn_rows, teams, resolver)
        _warn_unresolved("ESPN", status)
        return len(status), live.write_status(cursor, run_id, source, status)

    run_source(conn, "injuries", "fleaflicker", fleaflicker_work, dry_run, max_age)
    run_source(conn, "injuries", "espn", espn_work, dry_run, max_age)


def _fleaflicker_full(league_id) -> list:
    # Designations are the platform's, not the league's: read through the registry's active
    # Fleaflicker league (today 12090). Sorted by the season before SEASON: once the first
    # scoring period opens, the default listing stops at the 200 players scoring now, and on
    # 2026-09-29 22:00 a snapshot saw 1 injured player instead of ~79 and wrote the other 78
    # as cleared (write_status: off the report = ACTIVE) -- the live plan then activated two
    # IR players who were OUT. get_players now refuses a listing that short.
    return fleaflicker.injuries(fleaflicker.get_players(league_id, sort_season=int(SEASON[:4]) - 1))


def _fleaflicker_targeted(cursor, league_id, resolver, espn_rows, teams, player_index):
    """Fleaflicker's flags for the players likely to have one, in about 5 calls instead of the
    full listing's 44: every rostered player (one FetchLeagueRosters call), and by id everyone
    ESPN reports now or reported last time (a player who left ESPN's report is how it shows an
    activation) plus everyone Fleaflicker itself holds injured. Returns (injury rows, the external
    ids read): only those can be cleared, so a player this read did not cover keeps his status.
    New Fleaflicker-only injuries of unrostered players ESPN never lists wait for the full listing.
    None when the league has no drafted rosters to read (then the full listing runs)."""
    rostered = fleaflicker.get_league_rosters(league_id)
    covered = {str(entry["proPlayer"]["id"]) for entry in rostered}
    if len(covered) < ROSTERED_MIN:
        return None
    fleaflicker_id = {player_id: external_id for external_id, player_id in resolver.id_map.items()}
    espn = live.injuries_resolver(cursor, live.source_id(cursor, "espn"), player_index, "espn")
    espn_ids = ({r["player_id"] for r in live.espn_status_rows(espn_rows, teams, espn)}
                if espn_rows is not None else set())
    espn_ids |= set(live.held_injured(cursor, live.source_id(cursor, "espn")).values())
    wanted = {fleaflicker_id[p] for p in espn_ids if p in fleaflicker_id}
    wanted |= set(live.held_injured(cursor, live.source_id(cursor, "fleaflicker")))
    wanted -= covered
    listed = fleaflicker.get_players_by_id(league_id, wanted)
    covered |= {str(entry["proPlayer"]["id"]) for entry in listed}
    log.info("injuries/fleaflicker: targeted read, %d rostered + %d asked by id (%d listed)",
             len(rostered), len(wanted), len(listed))
    return fleaflicker.injuries(rostered + listed), covered


def snapshot_lines(conn, teams, player_index, dry_run):
    def work(cursor, run_id, source):
        resolver = live.injuries_resolver(cursor, source, player_index, "dailyfaceoff")
        def fetch(slug):
            try:
                return dailyfaceoff.line_chart(slug)
            except Exception:
                log.exception("Daily Faceoff chart %s failed; its players keep their last status", slug)
                return None

        # Fetched CHART_WORKERS at a time; matched and resolved here, in order, on this cursor.
        with ThreadPoolExecutor(max_workers=CHART_WORKERS) as pool:
            fetched = list(pool.map(fetch, dailyfaceoff.team_slugs()))
        charts = []
        for chart in fetched:
            if chart is None:
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
    parser.add_argument("--full-listing", action="store_true",
                        help="injuries: read Fleaflicker's whole listing even if one ran in the last 20 h")
    parser.add_argument("--targeted", action="store_true",
                        help="injuries: never Fleaflicker's whole listing (unless no rosters are drafted)")
    parser.add_argument("--max-age", type=int, default=None,
                        help="injuries: skip a source whose last good snapshot is younger (minutes)")
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
        snapshot_injuries(conn, season_id, teams, player_index, args.dry_run, args.max_age,
                          args.full_listing, args.targeted)
        merge_status(conn, args.dry_run)
    elif args.kind == "lines":
        snapshot_lines(conn, teams, player_index, args.dry_run)
        merge_status(conn, args.dry_run)
    else:
        game_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        snapshot_goalies(conn, teams, player_index, game_date, args.force, args.dry_run)


if __name__ == "__main__":
    main()
