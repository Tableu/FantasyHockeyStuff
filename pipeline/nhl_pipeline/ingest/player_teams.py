"""Reference.PlayerTeamHistory -- the team each player is signed with, from his NHL landing page
(`currentTeamAbbrev`, `isActive`; api/player_landing.py). Plan: ~/.claude/plans/nhl-roster-sync.md.

The landing page, not the team rosters: measured 2026-09-26, the roster endpoint leaves long-term
injured players off (21 of 88 OUT/SUSP players were on no roster), while every one of their pages
names his club; a released or unsigned player's page has no team and `isActive` false.

One open stint (`EndDate IS NULL`) per player per season, enforced by UX_PTH_OpenStint:

- a team and no open stint: open one from `as_of`;
- the open stint's team: nothing;
- another team (trade, waiver claim, signing elsewhere): close the old stint the day before
  `as_of` and open the new one;
- no team (released, unsigned, gone to Europe): close the open stint.

A 404 or a failed fetch changes nothing for that player. Each page is archived and its bio
applied through ingest.player_bio (the same page), and `Reference.Players.Active` follows
`isActive`.

Which players: anyone a current projection source projects, anyone in the league's Fleaflicker
pool for the season, and anyone with an open stint (so a release is seen) -- about 1,500, about
12 minutes at the client's pacing.
"""

import logging
from datetime import date, timedelta

from nhl_pipeline.ingest import player_bio

log = logging.getLogger("ingest.player_teams")

_OPEN_STINT_INDEX_DDL = (
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_PTH_OpenStint' "
    "AND object_id = OBJECT_ID('Reference.PlayerTeamHistory')) "
    "CREATE UNIQUE INDEX UX_PTH_OpenStint ON Reference.PlayerTeamHistory (PlayerID, SeasonID) "
    "WHERE EndDate IS NULL"
)


def ensure_open_stint_index(cursor) -> None:
    """At most one open stint per player per season (nhl_database_schema.sql)."""
    cursor.execute(_OPEN_STINT_INDEX_DDL)


def nhl_teams(cursor, season_id: int) -> dict:
    """{Abbreviation: TeamID} for the season's NHL clubs -- the teams in its regular-season
    schedule. Reference.Teams.Active also flags international, junior and All-Star sides."""
    cursor.execute("""
        SELECT DISTINCT t.Abbreviation, t.TeamID
        FROM Reference.Schedule s
        JOIN Reference.Teams t ON t.TeamID IN (s.HomeTeamID, s.AwayTeamID)
        WHERE s.SeasonID = ? AND s.GameType = '2'""", season_id)
    teams = {r.Abbreviation: r.TeamID for r in cursor.fetchall()}
    if len(teams) != 32:
        log.warning("%d NHL teams in the season's schedule (expected 32)", len(teams))
    return teams


def players_to_check(cursor, season_id: int, also=()) -> list:
    """(PlayerID, NHLPlayerID, FullName): projected by a current source, in the league's
    Fleaflicker pool for the season, holding an open stint, or in `also` -- the players the
    run's backfill just added or aliased, who join the pool and projections only when those
    imports next run."""
    cursor.execute("""
        SELECT p.PlayerID, p.NHLPlayerID, p.FullName
        FROM Reference.Players p
        WHERE p.NHLPlayerID IS NOT NULL AND p.PlayerID IN (
            SELECT PlayerID FROM Projections.SkaterProjections
            UNION SELECT PlayerID FROM Projections.GoalieProjections
            UNION SELECT x.PlayerID FROM Fantasy.PlatformPlayerIDs x
                  JOIN Fantasy.Platforms f ON f.FantasyPlatformID = x.FantasyPlatformID
                  WHERE f.PlatformName = 'Fleaflicker' AND x.SeasonID = ?
            UNION SELECT PlayerID FROM Reference.PlayerTeamHistory
                  WHERE SeasonID = ? AND EndDate IS NULL)
        ORDER BY p.PlayerID""", season_id, season_id)
    players = [(r.PlayerID, r.NHLPlayerID, r.FullName) for r in cursor.fetchall()]
    missing = set(also) - {p[0] for p in players}
    if missing:
        marks = ",".join("?" * len(missing))
        cursor.execute(f"SELECT PlayerID, NHLPlayerID, FullName FROM Reference.Players "
                       f"WHERE NHLPlayerID IS NOT NULL AND PlayerID IN ({marks})", *missing)
        players += [(r.PlayerID, r.NHLPlayerID, r.FullName) for r in cursor.fetchall()]
    return players


def open_stints(cursor, season_id: int) -> dict:
    """{PlayerID: (PlayerTeamHistoryID, TeamID)} for the season's open stints."""
    cursor.execute("""
        SELECT PlayerTeamHistoryID, PlayerID, TeamID FROM Reference.PlayerTeamHistory
        WHERE SeasonID = ? AND EndDate IS NULL""", season_id)
    return {r.PlayerID: (r.PlayerTeamHistoryID, r.TeamID) for r in cursor.fetchall()}


def sync_player_teams(cursor, season_id: int, as_of: date, limit: int | None = None,
                      also=()) -> dict:
    ensure_open_stint_index(cursor)
    teams = nhl_teams(cursor, season_id)
    abbrev_of = {team_id: abbrev for abbrev, team_id in teams.items()}
    stints = open_stints(cursor, season_id)
    players = players_to_check(cursor, season_id, also)
    if limit:
        players = players[:limit]
    log.info("checking %d player(s) against their NHL page, as of %s", len(players), as_of)

    counts = {"checked": 0, "opened": 0, "moved": 0, "closed": 0, "unchanged": 0,
              "no_team": 0, "not_found": 0, "failed": 0, "unknown_team": 0}
    moves = []
    for i, (player_id, nhl_player_id, name) in enumerate(players, 1):
        try:
            page = player_bio.fetch_landing(cursor, player_id, nhl_player_id)
        except Exception as error:  # a bad fetch skips this player only
            counts["failed"] += 1
            log.warning("  %s (NHL %s): fetch failed: %s", name, nhl_player_id, error)
            continue
        if page is None:
            counts["not_found"] += 1
            continue
        counts["checked"] += 1
        player_bio.apply_bio(cursor, player_id, page)
        if "isActive" in page:
            cursor.execute("UPDATE Reference.Players SET Active = ? WHERE PlayerID = ?",
                           bool(page["isActive"]), player_id)

        abbrev = page.get("currentTeamAbbrev")
        team_id = teams.get(abbrev) if abbrev else None
        if abbrev and team_id is None:
            counts["unknown_team"] += 1
            log.warning("  %s: team %r is not one of the season's NHL clubs", name, abbrev)
        current = stints.get(player_id)

        if current and current[1] == team_id:
            counts["unchanged"] += 1
        else:
            if current:
                cursor.execute("UPDATE Reference.PlayerTeamHistory SET EndDate = ? "
                               "WHERE PlayerTeamHistoryID = ?", as_of - timedelta(days=1), current[0])
            if team_id is not None:
                cursor.execute("INSERT INTO Reference.PlayerTeamHistory "
                               "(PlayerID, TeamID, SeasonID, StartDate, EndDate) VALUES (?, ?, ?, ?, NULL)",
                               player_id, team_id, season_id, as_of)
                counts["moved" if current else "opened"] += 1
                if current:
                    moves.append(f"{name} {abbrev_of.get(current[1], current[1])} -> {abbrev}")
            elif current:
                counts["closed"] += 1
                moves.append(f"{name} {abbrev_of.get(current[1], current[1])} -> no team")
            else:
                counts["no_team"] += 1
        if i % 250 == 0:
            log.info("  %d/%d", i, len(players))

    for move in moves:
        log.info("  moved: %s", move)
    log.info("player teams: %s", counts)
    return counts
