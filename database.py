"""Database connection helpers for local SQLite and Turso Cloud."""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import turso_serverless

TURSO_DATABASE_URL_ENV = "TURSO_DATABASE_URL"
TURSO_AUTH_TOKEN_ENV = "TURSO_AUTH_TOKEN"
USE_TURSO_ENV = "SOLCAST_USE_TURSO"
SYNC_MANIFEST_TABLE = "_dashboard_sync_manifest"
DB_OPERATIONAL_ERRORS = (sqlite3.OperationalError, turso_serverless.OperationalError)


def turso_configured():
    return bool(
        os.getenv(TURSO_DATABASE_URL_ENV, "").strip()
        or os.getenv(TURSO_AUTH_TOKEN_ENV, "").strip()
    )


def turso_enabled():
    return os.getenv(USE_TURSO_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def connect_turso():
    database_url = os.getenv(TURSO_DATABASE_URL_ENV, "").strip()
    auth_token = os.getenv(TURSO_AUTH_TOKEN_ENV, "").strip()
    if not database_url or not auth_token:
        raise RuntimeError(
            "Configure both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN before using Turso."
        )
    return turso_serverless.connect(database_url, auth_token=auth_token)


def connect_database(database_path):
    """Connect to Turso only when opted in; otherwise use the local SQLite file."""
    if turso_enabled():
        return connect_turso()
    return sqlite3.connect(Path(database_path))


def sync_value(value):
    if value is None:
        return ("null", "")
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, int):
        return ("int", str(value))
    return ("text", str(value))


def sync_row_key(primary_key_columns, columns, row):
    column_indexes = {column: index for index, column in enumerate(columns)}
    key_columns = primary_key_columns or columns
    if any(column not in column_indexes for column in key_columns):
        raise RuntimeError("A table primary key is missing from its shared columns.")
    return json.dumps(
        [sync_value(row[column_indexes[column]]) for column in key_columns],
        separators=(",", ":"),
    )


def sync_row_hash(columns, row):
    encoded = json.dumps(
        [sync_value(value) for value in row],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_sync_manifest(connection):
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS "{SYNC_MANIFEST_TABLE}" (
            table_name TEXT NOT NULL,
            row_key TEXT NOT NULL,
            row_hash TEXT NOT NULL,
            PRIMARY KEY (table_name, row_key)
        )
    """)


def local_primary_key_columns(connection, table_name):
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    return [
        row[1]
        for row in sorted(
            connection.execute(f"PRAGMA table_info({quoted_table})").fetchall(),
            key=lambda column: column[5],
        )
        if row[5]
    ]


def pull_turso_snapshot(database_path):
    """Replace matching local application tables with a one-time Turso snapshot."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    remote = connect_turso()
    local = None
    try:
        local = sqlite3.connect(database_path)
        local.execute("BEGIN")
        tables = remote.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('_dashboard_migrations', "
            f"'{SYNC_MANIFEST_TABLE}') ORDER BY name"
        ).fetchall()
        if not tables:
            raise RuntimeError("The Turso database does not contain dashboard tables.")

        create_sync_manifest(local)
        table_group = {
            "amber_prices": "amber",
            "amber_price_forecast_snapshots": "amber",
            "sigen_energy_actuals": "sigen",
            "sigen_power_actuals": "sigen",
        }
        group_priority = {"amber": 0, "sigen": 1}
        tables.sort(
            key=lambda table: (
                group_priority.get(table_group.get(table[0]), 2),
                table[0],
            )
        )

        row_counts = {}
        for table_name, create_sql in tables:
            table_started_at = time.perf_counter()
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            if not create_sql:
                continue
            create_sql = re.sub(
                r"^CREATE TABLE\s+",
                "CREATE TABLE IF NOT EXISTS ",
                create_sql,
                count=1,
                flags=re.IGNORECASE,
            )
            local.execute(create_sql)
            remote_cursor = remote.execute(f"SELECT * FROM {quoted_table}")
            remote_columns = [
                description[0] for description in remote_cursor.description or ()
            ]
            local_columns = {
                row[1] for row in local.execute(f"PRAGMA table_info({quoted_table})")
            }
            shared_columns = [
                column for column in remote_columns if column in local_columns
            ]
            if not shared_columns:
                raise RuntimeError(
                    f"No compatible columns found while loading Turso table {table_name}."
                )
            remote_rows = remote_cursor.fetchall()
            column_indexes = [remote_columns.index(column) for column in shared_columns]
            quoted_columns = ", ".join(
                '"' + column.replace('"', '""') + '"' for column in shared_columns
            )
            placeholders = ", ".join("?" for _ in shared_columns)
            local.execute(f"DELETE FROM {quoted_table}")
            local.executemany(
                f"INSERT OR REPLACE INTO {quoted_table} ({quoted_columns}) "
                f"VALUES ({placeholders})",
                (
                    tuple(row[index] for index in column_indexes)
                    for row in remote_rows
                ),
            )
            row_counts[table_name] = len(remote_rows)
            primary_key_columns = local_primary_key_columns(local, table_name)
            local.execute(
                f'DELETE FROM "{SYNC_MANIFEST_TABLE}" WHERE table_name = ?',
                (table_name,),
            )
            local_rows = local.execute(
                f"SELECT {quoted_columns} FROM {quoted_table}"
            ).fetchall()
            local.executemany(
                f'INSERT OR REPLACE INTO "{SYNC_MANIFEST_TABLE}" '
                "(table_name, row_key, row_hash) VALUES (?, ?, ?)",
                (
                    (
                        table_name,
                        sync_row_key(primary_key_columns, shared_columns, row),
                        sync_row_hash(shared_columns, row),
                    )
                    for row in local_rows
                ),
            )
            print(
                "PERF operation=turso_snapshot_table group=%s table=%s rows=%d "
                "duration_seconds=%.3f"
                % (
                    table_group.get(table_name, "other"),
                    table_name,
                    len(remote_rows),
                    time.perf_counter() - table_started_at,
                ),
                flush=True,
            )

        indexes = remote.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
        for (create_sql,) in indexes:
            create_sql = re.sub(
                r"^CREATE (UNIQUE )?INDEX\s+",
                r"CREATE \1INDEX IF NOT EXISTS ",
                create_sql,
                count=1,
                flags=re.IGNORECASE,
            )
            local.execute(create_sql)
        local.commit()
        return row_counts
    except Exception:
        if local is not None:
            local.rollback()
        raise
    finally:
        if local is not None:
            local.close()
        remote.close()


def read_sql_query(query, connection, params=None):
    """Read query results into a DataFrame using a DB-API connection."""
    import pandas as pd

    cursor = connection.execute(query, () if params is None else params)
    columns = [column[0] for column in cursor.description or ()]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
