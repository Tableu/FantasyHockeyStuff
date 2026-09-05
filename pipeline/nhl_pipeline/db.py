"""Connection management and a generic MERGE-based upsert helper.

Every target table already has the UNIQUE constraint needed to make these upserts
idempotent (re-running a day's ingestion updates existing rows rather than duplicating
them), so one helper covers every table in the pipeline instead of hand-written
INSERT/UPDATE logic per table.
"""

import pyodbc

from nhl_pipeline.config import get_connection_string


def connect() -> pyodbc.Connection:
    conn = pyodbc.connect(get_connection_string())
    conn.autocommit = False
    return conn


def _merge_sql(table: str, unique_cols: list, update_cols: list, all_cols: list) -> str:
    on_clause = " AND ".join(f"target.{c} = source.{c}" for c in unique_cols)
    select_source = ", ".join(f"? AS {c}" for c in all_cols)
    insert_cols = ", ".join(all_cols)
    insert_vals = ", ".join(f"source.{c}" for c in all_cols)

    update_clause = ""
    if update_cols:
        set_clause = ", ".join(f"{c} = source.{c}" for c in update_cols)
        update_clause = f"WHEN MATCHED THEN UPDATE SET {set_clause}"

    return f"""
        MERGE INTO {table} AS target
        USING (SELECT {select_source}) AS source
        ON {on_clause}
        {update_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});
    """


def upsert(cursor, table: str, unique_cols: dict, update_cols: dict | None = None) -> None:
    """Idempotent insert-or-update. unique_cols are the match/key columns, update_cols
    are overwritten on a match. Columns present only in unique_cols are left untouched
    on an existing row (their value is fixed at first insert)."""
    update_cols = update_cols or {}
    all_cols = {**unique_cols, **update_cols}
    sql = _merge_sql(table, list(unique_cols), list(update_cols), list(all_cols))
    cursor.execute(sql, list(all_cols.values()))


def upsert_get_id(cursor, table: str, id_col: str, unique_cols: dict, update_cols: dict | None = None) -> int:
    """Same as upsert(), but returns the surrogate key of the affected row via OUTPUT. When
    update_cols is empty and the row already existed, WHEN MATCHED has no clause to fire, so
    MERGE's OUTPUT reports nothing for it (only INSERT/UPDATE actions are OUTPUT, not a
    no-op match) -- falls back to a plain SELECT on unique_cols in that case."""
    update_cols = update_cols or {}
    all_cols = {**unique_cols, **update_cols}

    on_clause = " AND ".join(f"target.{c} = source.{c}" for c in unique_cols)
    select_source = ", ".join(f"? AS {c}" for c in all_cols)
    insert_cols = ", ".join(all_cols)
    insert_vals = ", ".join(f"source.{c}" for c in all_cols)
    update_clause = ""
    if update_cols:
        set_clause = ", ".join(f"{c} = source.{c}" for c in update_cols)
        update_clause = f"WHEN MATCHED THEN UPDATE SET {set_clause}"

    sql = f"""
        MERGE INTO {table} AS target
        USING (SELECT {select_source}) AS source
        ON {on_clause}
        {update_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        OUTPUT inserted.{id_col};
    """
    cursor.execute(sql, list(all_cols.values()))
    row = cursor.fetchone()
    if row is not None:
        return row[0]

    where_clause = " AND ".join(f"{c} = ?" for c in unique_cols)
    return fetch_scalar(cursor, f"SELECT {id_col} FROM {table} WHERE {where_clause}", *unique_cols.values())


def delete_where(cursor, table: str, match_cols: dict) -> None:
    """Deletes every row in table matching match_cols exactly -- used to fully replace a
    scope (e.g. one platform's rows for one season) right before repopulating it fresh, so a
    row whose source data no longer applies (a player's eligibility changed, they dropped out
    of the pool) doesn't linger forever the way upsert alone would leave it."""
    where_clause = " AND ".join(f"{c} = ?" for c in match_cols)
    cursor.execute(f"DELETE FROM {table} WHERE {where_clause}", list(match_cols.values()))


def fetch_scalar(cursor, sql: str, *params):
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None
