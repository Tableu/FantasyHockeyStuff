#!/usr/bin/env python
"""Copies the container's database back into the PC's (plan container-own-db; the plan doc's
Phase 3 copy-back): after the cutover the container's database is the only one written, and the
PC's full NHLStats, kept for model training, is brought up to date from it each night.

    python sync_back.py                                  # container (127.0.0.1,14330) -> PC (localhost)
    python sync_back.py --source db,1433 --target host.docker.internal     # from inside the container
    python sync_back.py --target-db NHLStats_pcsim --target 127.0.0.1,14330 # the test copy
    python sync_back.py --dry-run                        # what it would copy, nothing written

Both sides through the pipeline's writer login (pipeline/databaseCredentials.txt), the server
given here. What it copies:

- Games: every game with an import run (Ingestion.IngestionRuns) newer than the target's newest.
  Each is replaced whole on the target in one transaction: its rows deleted in every game-keyed
  table (children first), the source's inserted with their own ids (IDENTITY_INSERT). Games go in
  the order of their newest run and the copy stops at the first failure, so the target's newest
  run is always a true watermark.
- Every other table: compared row by row on its primary key against the source, then rows
  deleted (children first), changed and inserted (parents first), before the games, which point
  at their players and teams.

The target must be written by nothing else (the plan's single writer), or its rows are replaced.
"""

import argparse
import shlex
import sys
import time
from collections import defaultdict
from pathlib import Path

import pyodbc

ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS = ROOT / "pipeline" / "databaseCredentials.txt"

# Game-keyed tables, children first, with how a row finds its game (one GameID, `?`).
GAME_TABLES = [
    ("Analytics.ShotExpectedGoals", "ShotID IN (SELECT s.ShotID FROM Game.Shots s WHERE s.GameID = ?)"),
    ("Game.Goals", "GameID = ?"),
    ("Game.PlayOnIcePlayers", "PlayID IN (SELECT p.PlayID FROM Game.Plays p WHERE p.GameID = ?)"),
    ("Game.Shots", "GameID = ?"),
    ("Game.Plays", "GameID = ?"),
    ("Game.Shifts", "GameID = ?"),
    ("Analytics.GoalieGameAdvancedStats", "GameID = ?"),
    ("Analytics.PlayerGameAdvancedStats", "GameID = ?"),
    ("Analytics.PlayerGameOnIceStats", "GameID = ?"),
    ("Lineups.GameLineups", "GameID = ?"),
    ("Stats.PlayerGameStats", "GameID = ?"),
    ("Ingestion.IngestionRuns", "GameID = ?"),
    ("Ingestion.RawApiResponses", "GameID = ?"),
    ("Game.Games", "GameID = ?"),
]
GAME_TABLE_NAMES = {table for table, _ in GAME_TABLES}


def connect(server: str, database: str) -> pyodbc.Connection:
    parser = argparse.ArgumentParser()
    for flag in ("-S", "-U", "-P", "-d"):
        parser.add_argument(flag)
    creds, _ = parser.parse_known_args(shlex.split(CREDENTIALS.read_text(encoding="utf-8").strip())[1:])
    conn = pyodbc.connect("DRIVER={ODBC Driver 18 for SQL Server};"
                          f"SERVER={server};DATABASE={database};UID={creds.U};PWD={creds.P};"
                          "Encrypt=yes;TrustServerCertificate=yes;")
    conn.autocommit = False
    return conn


class Table:
    """A table's writable columns, identity column and primary key (from the source's catalog)."""

    def __init__(self, cursor, name: str):
        self.name = name
        schema, table = name.split(".")
        cursor.execute("""SELECT c.name, c.is_identity FROM sys.columns c
                          WHERE c.object_id = OBJECT_ID(?) AND c.is_computed = 0 ORDER BY c.column_id""", name)
        rows = cursor.fetchall()
        self.columns = [r[0] for r in rows]
        self.identity = next((r[0] for r in rows if r[1]), None)
        cursor.execute("""SELECT col.name FROM sys.indexes i
                          JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                          JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id
                          WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1 ORDER BY ic.key_ordinal""", name)
        self.key = [r[0] for r in cursor.fetchall()]
        if not self.key:
            raise SystemExit(f"{name} has no primary key: cannot copy it row by row")

    def select(self, where="1 = 1") -> str:
        return f"SELECT {', '.join(f'[{c}]' for c in self.columns)} FROM {self.name} WHERE {where}"

    def insert(self, cursor, rows):
        if not rows:
            return
        cols = ", ".join(f"[{c}]" for c in self.columns)
        marks = ", ".join("?" for _ in self.columns)
        if self.identity:
            cursor.execute(f"SET IDENTITY_INSERT {self.name} ON")
        cursor.fast_executemany = True
        cursor.executemany(f"INSERT INTO {self.name} ({cols}) VALUES ({marks})", [tuple(r) for r in rows])
        cursor.fast_executemany = False
        if self.identity:
            cursor.execute(f"SET IDENTITY_INSERT {self.name} OFF")


def table_order(cursor) -> list:
    """Every user table, parents before children (foreign keys)."""
    cursor.execute("SELECT s.name + '.' + t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id")
    tables = [r[0] for r in cursor.fetchall()]
    cursor.execute("""SELECT OBJECT_SCHEMA_NAME(parent_object_id) + '.' + OBJECT_NAME(parent_object_id),
                             OBJECT_SCHEMA_NAME(referenced_object_id) + '.' + OBJECT_NAME(referenced_object_id)
                      FROM sys.foreign_keys WHERE parent_object_id <> referenced_object_id""")
    parents = defaultdict(set)
    for child, parent in cursor.fetchall():
        parents[child].add(parent)
    ordered, placed = [], set()
    while len(ordered) < len(tables):
        ready = sorted(t for t in tables if t not in placed and parents[t] <= placed)
        if not ready:
            raise SystemExit("foreign keys form a cycle: " + ", ".join(t for t in tables if t not in placed))
        ordered += ready
        placed |= set(ready)
    return ordered


def diff_table(src, tgt, table: Table):
    """(new rows, changed rows, keys gone) of the target against the source."""
    src.execute(table.select())
    source = {tuple(r[table.columns.index(k)] for k in table.key): tuple(r) for r in src.fetchall()}
    tgt.execute(table.select())
    target = {tuple(r[table.columns.index(k)] for k in table.key): tuple(r) for r in tgt.fetchall()}
    new = [row for key, row in source.items() if key not in target]
    changed = [row for key, row in source.items() if key in target and target[key] != row]
    gone = [key for key in target if key not in source]
    return new, changed, gone


def sync_small(src, tgt, order, dry_run, echo) -> None:
    """Every non-game table brought to the source's rows: all deletes first, children first (a
    row the source deleted and re-created under a new id would otherwise collide with its old
    copy on the table's unique keys), then inserts and updates, parents first -- before the
    games, which point at their players and teams."""
    diffs = []
    for name in order:
        if name in GAME_TABLE_NAMES:
            continue
        table = Table(src, name)
        new, changed, gone = diff_table(src, tgt, table)
        if new or changed or gone:
            echo(f"  {name}: {len(new)} new, {len(changed)} changed, {len(gone)} gone")
            diffs.append((table, new, changed, gone))
    if dry_run:
        return
    for table, _, _, gone in reversed(diffs):            # children first
        if gone:
            keys = " AND ".join(f"[{k}] = ?" for k in table.key)
            tgt.executemany(f"DELETE FROM {table.name} WHERE {keys}", gone)
    for table, new, changed, _ in diffs:                  # parents first
        settable = [c for c in table.columns if c not in table.key and c != table.identity]
        if changed and settable:
            sets = ", ".join(f"[{c}] = ?" for c in settable)
            keys = " AND ".join(f"[{k}] = ?" for k in table.key)
            idx = {c: i for i, c in enumerate(table.columns)}
            tgt.executemany(f"UPDATE {table.name} SET {sets} WHERE {keys}",
                            [tuple(r[idx[c]] for c in settable) + tuple(r[idx[k]] for k in table.key) for r in changed])
        table.insert(tgt, new)


def touched_games(src, tgt, echo):
    """The games to copy, oldest run first, and the watermark. Only the current season's: the
    source holds earlier seasons' games without their play-by-play (db_seed's trim), so copying
    one would replace the target's full rows with a partial set -- reported, never copied."""
    tgt.execute("SELECT ISNULL(MAX(IngestionRunID), 0) FROM Ingestion.IngestionRuns")
    mark = tgt.fetchone()[0]
    src.execute("SELECT TOP 1 SeasonID FROM Game.Games ORDER BY GameDate DESC")
    current = src.fetchone()[0]
    src.execute("""SELECT r.GameID, MAX(r.IngestionRunID), MAX(g.SeasonID) FROM Ingestion.IngestionRuns r
                   JOIN Game.Games g ON g.GameID = r.GameID
                   WHERE r.IngestionRunID > ? GROUP BY r.GameID ORDER BY 2""", mark)
    rows = src.fetchall()
    for game_id, _, season in rows:
        if season != current:
            echo(f"  game {game_id} (season {season}): not copied -- only the current season is held whole")
    return [game_id for game_id, _, season in rows if season == current], mark


def copy_game(src, tgt, game_id: int, tables: dict) -> int:
    for name, where in GAME_TABLES:                       # children first
        tgt.execute(f"DELETE FROM {name} WHERE {where}", game_id)
    copied = 0
    for name, where in reversed(GAME_TABLES):             # parents first
        table = tables[name]
        src.execute(table.select(where), game_id)
        rows = src.fetchall()
        table.insert(tgt, rows)
        copied += len(rows)
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="127.0.0.1,14330", help="the container's database server")
    parser.add_argument("--target", default="localhost", help="the PC's database server")
    parser.add_argument("--source-db", default="NHLStats")
    parser.add_argument("--target-db", default="NHLStats")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    echo = lambda text: print(text, flush=True)       # noqa: E731
    t0 = time.time()
    src_conn, tgt_conn = connect(args.source, args.source_db), connect(args.target, args.target_db)
    src, tgt = src_conn.cursor(), tgt_conn.cursor()
    order = table_order(src)
    echo(f"copy-back {args.source}/{args.source_db} -> {args.target}/{args.target_db}"
         f"{' (dry run)' if args.dry_run else ''}")

    echo("other tables:")
    sync_small(src, tgt, order, args.dry_run, echo)
    if not args.dry_run:
        tgt_conn.commit()

    games, mark = touched_games(src, tgt, echo)
    echo(f"games: {len(games)} with import runs after {mark}")
    tables = {name: Table(src, name) for name in GAME_TABLE_NAMES}
    for game_id in games:
        if args.dry_run:
            continue
        try:
            rows = copy_game(src, tgt, game_id, tables)
            tgt_conn.commit()
            echo(f"  game {game_id}: {rows} rows")
        except Exception:
            tgt_conn.rollback()
            echo(f"  game {game_id}: FAILED -- stopping here; the next run starts again from it")
            raise

    echo(f"done in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    sys.exit(main())
