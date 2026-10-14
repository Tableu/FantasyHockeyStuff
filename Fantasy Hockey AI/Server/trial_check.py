#!/usr/bin/env python
"""The jobs trial's morning check (plan container-own-db): did the container's database, written by
the server's jobs, get the same games as the PC's, written by the Windows tasks?

    python trial_check.py                     # PC (localhost) vs container (127.0.0.1,14330)

For each current-season game: its rows in every game-keyed table, on both sides. Identity ids
differ between the two (each database numbers its own rows), so rows are compared on their
values without the id columns. Raw API responses are left out (the slim copy keeps none of the
PC's). Then the non-game tables the jobs write, by row count.
"""

import sys

import sync_back as sb

STAMPS = {"CreatedAt", "CalculatedAt"}                         # when a row was written, not what
SKIP = {"Ingestion.RawApiResponses", "Ingestion.IngestionRuns"}   # fetch logs, not data
WATCHED = ["Reference.Players", "Reference.PlayerTeamHistory", "Injuries.Spells", "Stats.PlayerSeasonStats",
           "Stats.SeasonTotals", "Live.PlayerStatus"]


def values(cursor, table: sb.Table, where: str, game_id: int, ids: set) -> list:
    keep = [c for c in table.columns if c not in ids and c not in STAMPS]
    cursor.execute(f"SELECT {', '.join(f'[{c}]' for c in keep)} FROM {table.name} WHERE {where}", game_id)
    return sorted(tuple(r) for r in cursor.fetchall())


def main():
    pc = sb.connect("localhost", "NHLStats").cursor()
    box = sb.connect("127.0.0.1,14330", "NHLStats").cursor()
    box.execute("SELECT TOP 1 SeasonID FROM Game.Games ORDER BY GameDate DESC")
    season = box.fetchone()[0]
    games = {}
    for side, cursor in (("pc", pc), ("container", box)):
        cursor.execute("SELECT GameID, NHLGameID FROM Game.Games WHERE SeasonID = ?", season)
        games[side] = {nhl: gid for gid, nhl in cursor.fetchall()}
    both = sorted(set(games["pc"]) & set(games["container"]))
    for side, other in (("pc", "container"), ("container", "pc")):
        only = sorted(set(games[side]) - set(games[other]))
        if only:
            print(f"games only in the {side}'s database: {only}")
    # Every identity column, and every column that points at one (ids differ between the databases).
    box.execute("""SELECT c.name FROM sys.identity_columns c
                   UNION SELECT COL_NAME(fc.parent_object_id, fc.parent_column_id) FROM sys.foreign_key_columns fc
                   JOIN sys.identity_columns ic ON ic.object_id = fc.referenced_object_id AND ic.column_id = fc.referenced_column_id""")
    ids = {r[0] for r in box.fetchall()} - {"PlayerID", "TeamID", "SeasonID", "SituationID", "CalculationVersionID"}
    differ = 0
    for name, where in sb.GAME_TABLES:
        if name in SKIP:
            continue
        table = sb.Table(box, name)
        bad = [nhl for nhl in both if values(pc, table, where, games["pc"][nhl], ids)
               != values(box, table, where, games["container"][nhl], ids)]
        differ += len(bad)
        print(f"{'same  ' if not bad else 'DIFFER'} {name:36} {len(both)} games" + (f", differ: {bad[:10]}" if bad else ""))
    for name in WATCHED:
        counts = []
        for cursor in (pc, box):
            cursor.execute(f"SELECT COUNT(*) FROM {name}")
            counts.append(cursor.fetchone()[0])
        print(f"{'same  ' if counts[0] == counts[1] else 'counts'} {name:36} pc {counts[0]}, container {counts[1]}")
    print("TRIAL CHECK:", "games identical" if not differ else f"{differ} game-table differences")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
