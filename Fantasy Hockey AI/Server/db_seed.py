#!/usr/bin/env python
"""Seeds the container's own database (compose service `db`) from a backup of the PC's NHLStats:
restore, attach the scripts' logins, trim to what the live side reads, shrink (plan
container-own-db, the doc's Phase 3 on this PC). Run on the PC, from this folder:

    python db_seed.py                       # restore dbseed/NHLStats_container.bak, then trim
    python db_seed.py --keep-full           # restore only (the A/B's untrimmed baseline)

The backup is the user's (the PC login may not BACKUP):

    sqlcmd -S localhost -E -C -Q "BACKUP DATABASE NHLStats TO DISK = N'C:\\Users\\Public\\Documents\\NHLStats_container.bak' WITH COPY_ONLY, COMPRESSION, INIT"

then moved into dbseed/ (gitignored). The restore keeps every id and identity seed, so the copy
counts on from the PC's values. The logins are the ones in pipeline/ and ModelFeatures/
databaseCredentials.txt, so a script reaches either database by NHLSTATS_DB_SERVER alone.

Trim (KEEP below): the current season keeps everything; last season its games and the tables
the A/B showed the live builds read; older seasons and the raw API responses go.
"""

import argparse
import shlex
import time
from pathlib import Path

import pyodbc

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BACKUP = "/seed/NHLStats_container.bak"            # dbseed/ as the db container mounts it
SERVER = "127.0.0.1,14330"                         # the db container, from the PC
CREDENTIALS = [ROOT / "pipeline" / "databaseCredentials.txt",
               ROOT / "Fantasy Hockey AI" / "ModelFeatures" / "databaseCredentials.txt"]

# Game-keyed tables, children first (the delete order). (table, how its rows find their game).
GAME_TABLES = [
    ("Analytics.ShotExpectedGoals", "ShotID IN (SELECT s.ShotID FROM Game.Shots s WHERE s.GameID IN ({games}))"),
    ("Game.Goals", "GameID IN ({games})"),
    ("Game.PlayOnIcePlayers", "PlayID IN (SELECT p.PlayID FROM Game.Plays p WHERE p.GameID IN ({games}))"),
    ("Game.Shots", "GameID IN ({games})"),
    ("Game.Plays", "GameID IN ({games})"),
    ("Game.Shifts", "GameID IN ({games})"),
    ("Analytics.GoalieGameAdvancedStats", "GameID IN ({games})"),
    ("Analytics.PlayerGameAdvancedStats", "GameID IN ({games})"),
    ("Analytics.PlayerGameOnIceStats", "GameID IN ({games})"),
    ("Lineups.GameLineups", "GameID IN ({games})"),
    ("Stats.PlayerGameStats", "GameID IN ({games})"),
    ("Ingestion.IngestionRuns", "GameID IN ({games})"),
    ("Ingestion.RawApiResponses", "GameID IN ({games})"),
    ("Game.Games", "GameID IN ({games})"),
]
# Last season (the one before the current): the game tables it keeps. The rest of its game rows go.
LAST_SEASON_KEEPS = {"Game.Games", "Stats.PlayerGameStats", "Lineups.GameLineups"}
BATCH = 200_000


def sa_connection(database="master"):
    env = dict(line.split("=", 1) for line in (HERE / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)
    return pyodbc.connect("DRIVER={ODBC Driver 18 for SQL Server};"
                          f"SERVER={SERVER};DATABASE={database};UID=sa;PWD={env['MSSQL_SA_PASSWORD']};"
                          "Encrypt=yes;TrustServerCertificate=yes;", autocommit=True)


def credentials(path):
    parser = argparse.ArgumentParser()
    for flag in ("-S", "-U", "-P", "-d"):
        parser.add_argument(flag)
    args, _ = parser.parse_known_args(shlex.split(path.read_text(encoding="utf-8").strip())[1:])
    return args.U, args.P


def restore(cursor):
    cursor.execute("IF DB_ID('NHLStats') IS NOT NULL ALTER DATABASE NHLStats SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
    cursor.execute(f"RESTORE DATABASE NHLStats FROM DISK = N'{BACKUP}' WITH REPLACE, "
                   "MOVE N'NHLStats' TO N'/var/opt/mssql/data/NHLStats.mdf', "
                   "MOVE N'NHLStats_log' TO N'/var/opt/mssql/data/NHLStats_log.ldf'")
    while cursor.nextset():                         # RESTORE reports progress as result sets
        pass
    cursor.execute("ALTER DATABASE NHLStats SET MULTI_USER")
    cursor.execute("ALTER DATABASE NHLStats SET RECOVERY SIMPLE")


def logins(cursor):
    """The scripts' logins, with the PC's passwords, attached to the restored database's users."""
    for path in CREDENTIALS:
        user, password = credentials(path)
        cursor.execute("USE master")
        cursor.execute("SELECT COUNT(*) FROM sys.server_principals WHERE name = ?", user)
        if not cursor.fetchone()[0]:
            quoted = password.replace("'", "''")
            cursor.execute(f"CREATE LOGIN [{user}] WITH PASSWORD = N'{quoted}', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF")
        cursor.execute("USE NHLStats")
        cursor.execute("SELECT COUNT(*) FROM sys.database_principals WHERE name = ?", user)
        cursor.execute(f"ALTER USER [{user}] WITH LOGIN = [{user}]" if cursor.fetchone()[0]
                       else f"CREATE USER [{user}] FOR LOGIN [{user}]")


def delete_batched(cursor, table, where):
    total = 0
    while True:
        cursor.execute(f"DELETE TOP ({BATCH}) FROM {table} WHERE {where}")
        total += cursor.rowcount
        if cursor.rowcount < BATCH:
            return total


def trim(cursor):
    cursor.execute("USE NHLStats")
    cursor.execute("SELECT MAX(SeasonID) FROM Game.Games WHERE GameDate = (SELECT MAX(GameDate) FROM Game.Games)")
    current = cursor.fetchone()[0]
    cursor.execute("SELECT SeasonID, StartYear FROM Reference.Seasons")
    start = dict(cursor.fetchall())
    last = max((s for s in start if start[s] < start[current]), key=lambda s: start[s])
    older = [s for s in start if start[s] < start[last]]
    print(f"seasons: current {current} ({start[current]}), last {last} ({start[last]}), dropped {sorted(older)}")
    dropped_games = f"SELECT GameID FROM Game.Games WHERE SeasonID IN ({','.join(map(str, older)) or 'NULL'})"
    last_games = f"SELECT GameID FROM Game.Games WHERE SeasonID = {last}"
    for table, where in GAME_TABLES:
        t = time.time()
        n = delete_batched(cursor, table, where.format(games=dropped_games))
        if table not in LAST_SEASON_KEEPS:
            n += delete_batched(cursor, table, where.format(games=last_games))
        print(f"  {table:36} {n:>9} rows deleted ({time.time() - t:.0f} s)")
    n = delete_batched(cursor, "Ingestion.RawApiResponses", "1 = 1")
    print(f"  {'Ingestion.RawApiResponses (the rest)':36} {n:>9} rows deleted")


def shrink(cursor):
    cursor.execute("USE NHLStats")
    cursor.execute("CHECKPOINT")
    cursor.execute("DBCC SHRINKFILE (NHLStats, 0) WITH NO_INFOMSGS")
    cursor.execute("DBCC SHRINKFILE (NHLStats_log, 64) WITH NO_INFOMSGS")
    cursor.execute("SELECT name, size * 8 / 1024, FILEPROPERTY(name, 'SpaceUsed') * 8 / 1024 FROM sys.database_files")
    for name, size, used in cursor.fetchall():
        print(f"  {name}: {size} MB ({used} MB used)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep-full", action="store_true", help="restore only, no trim")
    args = parser.parse_args()
    cursor = sa_connection().cursor()
    t = time.time()
    restore(cursor)
    logins(cursor)
    print(f"restored and logins attached ({time.time() - t:.0f} s)")
    if not args.keep_full:
        trim(cursor)
        shrink(cursor)
    print(f"done in {time.time() - t:.0f} s")


if __name__ == "__main__":
    main()
