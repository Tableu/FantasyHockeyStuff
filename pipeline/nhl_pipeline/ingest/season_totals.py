"""Stats.SeasonTotals -- every player's regular-season totals, one row per player per season,
from the NHL.com stats reports (api.stats_rest). Plan: ~/.claude/plans/boom-bust-odds.md.

Why a table beside Stats.PlayerSeasonStats: that one is a SUM over our own ingested games
(2023-24 on) and is rebuilt from them; this one is the NHL's own season line, back to 2000-01,
so a season-level model (games played, year-to-year rate change) has every player who dressed,
not only the injured ones Injuries.Spells lists. Rows are summed across teams by the source;
`TeamAbbrevs` keeps the teams as the source lists them.

Players the database has never seen (anyone who stopped playing before 2023-24) are added to
Reference.Players under their permanent NHL id, `Active` = 0 -- Reference.Players already holds
everyone who played an ingested game, so a player new here has not played since. Their birth date,
height and weight come from the same reports' `bios`, which also fill a birth date the database
is missing (never overwrite one). A later game ingest upserts onto the same row.

Hits and blocks are NULL before 2005-06: the source reports 0 for everyone, which is "not
tracked", not zero. Re-running a season replaces its rows wholesale.
"""

import logging

from nhl_pipeline import db
from nhl_pipeline.api import stats_rest

log = logging.getLogger("ingest.season_totals")

TABLE = "Stats.SeasonTotals"
FIRST_TRACKED_HITS_SEASON = 20052006

_DDL = """CREATE TABLE Stats.SeasonTotals
(
    SeasonID            INT NOT NULL,
    PlayerID            BIGINT NOT NULL,
    IsGoalie            BIT NOT NULL,
    PositionCode        VARCHAR(5) NULL,
    TeamAbbrevs         VARCHAR(40) NULL,
    GamesPlayed         SMALLINT NOT NULL,
    Goals               SMALLINT NULL,
    Assists             SMALLINT NULL,
    Points              SMALLINT NULL,
    PowerPlayGoals      SMALLINT NULL,
    PowerPlayPoints     SMALLINT NULL,
    ShortHandedGoals    SMALLINT NULL,
    ShortHandedPoints   SMALLINT NULL,
    Shots               SMALLINT NULL,
    Hits                SMALLINT NULL,
    Blocks              SMALLINT NULL,
    PenaltyMinutes      SMALLINT NULL,
    TOIPerGameSeconds   DECIMAL(8,2) NULL,
    GamesStarted        SMALLINT NULL,
    Wins                SMALLINT NULL,
    Losses              SMALLINT NULL,
    OTLosses            SMALLINT NULL,
    Ties                SMALLINT NULL,
    Shutouts            SMALLINT NULL,
    GoalsAgainst        SMALLINT NULL,
    ShotsAgainst        SMALLINT NULL,
    Saves               SMALLINT NULL,
    TOISeconds          INT NULL,
    ImportedAt          DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_SeasonTotals PRIMARY KEY (SeasonID, PlayerID),
    CONSTRAINT FK_SeasonTotals_Season FOREIGN KEY (SeasonID)
        REFERENCES Reference.Seasons(SeasonID),
    CONSTRAINT FK_SeasonTotals_Player FOREIGN KEY (PlayerID)
        REFERENCES Reference.Players(PlayerID)
);"""

_COLUMNS = ["SeasonID", "PlayerID", "IsGoalie", "PositionCode", "TeamAbbrevs", "GamesPlayed",
            "Goals", "Assists", "Points", "PowerPlayGoals", "PowerPlayPoints", "ShortHandedGoals",
            "ShortHandedPoints", "Shots", "Hits", "Blocks", "PenaltyMinutes", "TOIPerGameSeconds",
            "GamesStarted", "Wins", "Losses", "OTLosses", "Ties", "Shutouts", "GoalsAgainst",
            "ShotsAgainst", "Saves", "TOISeconds"]


def ensure_table(cursor) -> None:
    """Create Stats.SeasonTotals if this database predates it (nhl_database_schema.sql)."""
    cursor.execute("IF OBJECT_ID('Stats.SeasonTotals', 'U') IS NULL EXEC('" +
                   " ".join(line.strip() for line in _DDL.splitlines()).replace("'", "''") +
                   "')")


def _split_name(full_name: str, last_name: str) -> str | None:
    if last_name and full_name.endswith(last_name):
        return full_name[: -len(last_name)].strip() or None
    return None


def _player_id(cursor, known: dict, bios: dict, nhl_id: int, full_name: str, last_name: str,
               position: str, shoots: str | None, added: list) -> int:
    if nhl_id not in known:
        bio = bios.get(nhl_id, {})
        known[nhl_id] = db.upsert_get_id(
            cursor, "Reference.Players", "PlayerID",
            {"NHLPlayerID": nhl_id},
            {"FirstName": _split_name(full_name, last_name), "LastName": last_name,
             "FullName": full_name, "PositionCode": position,
             "Shoots": shoots[:1] if shoots else None, "BirthDate": bio.get("birthDate"),
             "HeightInches": bio.get("height"), "WeightLbs": bio.get("weight"), "Active": False},
        )
        added.append((nhl_id, full_name))
    return known[nhl_id]


def _fill_birth_dates(cursor, bios: dict) -> int:
    """A player the database already holds without a birth date gets the report's. Nothing
    already set is overwritten."""
    cursor.execute("SELECT NHLPlayerID FROM Reference.Players WHERE BirthDate IS NULL")
    missing = [int(r[0]) for r in cursor.fetchall()]
    filled = 0
    for nhl_id in missing:
        birth = bios.get(nhl_id, {}).get("birthDate")
        if birth:
            cursor.execute("UPDATE Reference.Players SET BirthDate = ? WHERE NHLPlayerID = ? "
                           "AND BirthDate IS NULL", birth, nhl_id)
            filled += 1
    return filled


def _skater_rows(season_id: int, nhl_season_id: int, summary: list, realtime: list,
                 player_id) -> list:
    extra = {r["playerId"]: r for r in realtime}
    tracked = nhl_season_id >= FIRST_TRACKED_HITS_SEASON
    rows = []
    for r in summary:
        rt = extra.get(r["playerId"], {})
        pid = player_id(r["playerId"], r["skaterFullName"], r["lastName"], r["positionCode"],
                        r.get("shootsCatches"))
        rows.append([
            season_id, pid, False, r["positionCode"], r.get("teamAbbrevs"), r["gamesPlayed"],
            r["goals"], r["assists"], r["points"], r["ppGoals"], r["ppPoints"], r["shGoals"],
            r["shPoints"], r["shots"],
            rt.get("hits") if tracked else None, rt.get("blockedShots") if tracked else None,
            r["penaltyMinutes"], r.get("timeOnIcePerGame"),
            None, None, None, None, None, None, None, None, None, None,
        ])
    return rows


def _goalie_rows(season_id: int, summary: list, player_id) -> list:
    rows = []
    for r in summary:
        pid = player_id(r["playerId"], r["goalieFullName"], r["lastName"], "G",
                        r.get("shootsCatches"))
        toi = r.get("timeOnIce")
        rows.append([
            season_id, pid, True, "G", r.get("teamAbbrevs"), r["gamesPlayed"],
            r.get("goals"), r.get("assists"), r.get("points"), None, None, None, None, None,
            None, None, r.get("penaltyMinutes"),
            toi / r["gamesPlayed"] if toi is not None and r["gamesPlayed"] else None,
            r.get("gamesStarted"), r.get("wins"), r.get("losses"), r.get("otLosses"),
            r.get("ties"), r.get("shutouts"), r.get("goalsAgainst"), r.get("shotsAgainst"),
            r.get("saves"), toi,
        ])
    return rows


def sync_season(cursor, season_id: int, nhl_season_id: int) -> dict:
    """Replace one season's rows. Returns counts, and the players added to Reference.Players."""
    skaters = stats_rest.get_season_report("skater", "summary", nhl_season_id)
    realtime = stats_rest.get_season_report("skater", "realtime", nhl_season_id)
    goalies = stats_rest.get_season_report("goalie", "summary", nhl_season_id)
    bios = {r["playerId"]: r for kind in ("skater", "goalie")
            for r in stats_rest.get_season_report(kind, "bios", nhl_season_id)}

    cursor.execute("SELECT NHLPlayerID, PlayerID FROM Reference.Players")
    known = {int(n): int(p) for n, p in cursor.fetchall()}
    added: list = []

    def player_id(nhl_id, full_name, last_name, position, shoots):
        return _player_id(cursor, known, bios, nhl_id, full_name, last_name, position, shoots, added)

    rows = _skater_rows(season_id, nhl_season_id, skaters, realtime, player_id)
    rows += _goalie_rows(season_id, goalies, player_id)
    seen = [r[1] for r in rows]
    if len(seen) != len(set(seen)):
        raise RuntimeError(f"{nhl_season_id}: a player appears twice (skater and goalie?)")

    db.delete_where(cursor, TABLE, {"SeasonID": season_id})
    cursor.fast_executemany = True
    cursor.executemany(
        f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
        rows)
    cursor.fast_executemany = False
    filled = _fill_birth_dates(cursor, bios)
    return {"skaters": len(skaters), "goalies": len(goalies), "players_added": added,
            "birth_dates_filled": filled}
