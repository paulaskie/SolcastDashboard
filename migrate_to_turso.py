"""Copy the bundled SQLite history into an empty Turso database."""

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from database import (
    SYNC_MANIFEST_TABLE,
    connect_turso,
    create_sync_manifest,
    local_primary_key_columns,
    sync_row_hash,
    sync_row_key,
    turso_configured,
)

APP_DIR = Path(__file__).resolve().parent
SOURCE_DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
MIGRATION_TABLE = "_dashboard_migrations"
MAX_SCRIPT_SIZE = 256_000


def quote_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def source_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_tables(source):
    return source.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        f"AND name <> '{SYNC_MANIFEST_TABLE}' ORDER BY name"
    ).fetchall()


def insert_rows(
    target, source, table, replace_existing=False, progress_callback=None, total_rows=0,
):
    quoted_table = quote_identifier(table)
    cursor = source.execute(f"SELECT * FROM {quoted_table}")
    columns = [description[0] for description in cursor.description]
    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    conflict_action = "REPLACE" if replace_existing else "IGNORE"
    rows_copied = 0
    batch = []
    batch_size = 0

    for row in cursor:
        values = ", ".join(sql_literal(value) for value in row)
        batch.append(
            f"INSERT OR {conflict_action} INTO {quoted_table} ({quoted_columns}) "
            f"VALUES ({values});"
        )
        batch_size += len(batch[-1])
        rows_copied += 1
        if batch_size >= MAX_SCRIPT_SIZE:
            target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")
            batch.clear()
            batch_size = 0
            if progress_callback:
                progress_callback(table, rows_copied, total_rows)

    if batch:
        target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")
    if progress_callback:
        progress_callback(table, rows_copied, total_rows)
    return rows_copied


def execute_sql_batches(target, statements):
    batch = []
    batch_size = 0
    for statement in statements:
        statement_size = len(statement)
        if batch and batch_size + statement_size > MAX_SCRIPT_SIZE:
            target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")
            batch = []
            batch_size = 0
        batch.append(statement)
        batch_size += statement_size
    if batch:
        target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")


def upload_to_turso(source_path, progress_callback=None):
    """Upload only local rows that are new or changed since the previous sync."""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {source_path}")
    if not turso_configured():
        raise RuntimeError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN before uploading.")

    rows_uploaded = {}
    with closing(sqlite3.connect(source_path)) as source:
        create_sync_manifest(source)
        tables = source_tables(source)
        if not tables:
            raise RuntimeError(f"No application tables found in {source_path}.")

        target = connect_turso()
        try:
            create_sync_manifest(target)
            remote_manifest = {
                (table_name, row_key): row_hash
                for table_name, row_key, row_hash in target.execute(
                    f'SELECT table_name, row_key, row_hash '
                    f'FROM "{SYNC_MANIFEST_TABLE}"'
                ).fetchall()
            }
            local_manifest = {
                (table_name, row_key): row_hash
                for table_name, row_key, row_hash in source.execute(
                    f'SELECT table_name, row_key, row_hash '
                    f'FROM "{SYNC_MANIFEST_TABLE}"'
                ).fetchall()
            }

            for table, create_sql in tables:
                if not create_sql:
                    continue
                create_sql = re.sub(
                    r"^CREATE TABLE\s+",
                    "CREATE TABLE IF NOT EXISTS ",
                    create_sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
                target.execute(create_sql)

            local_manifest_rows_to_seed = []
            for table_name, row_key, row_hash in source.execute(
                f'SELECT table_name, row_key, row_hash '
                f'FROM "{SYNC_MANIFEST_TABLE}"'
            ):
                if (table_name, row_key) not in remote_manifest:
                    local_manifest_rows_to_seed.append((table_name, row_key, row_hash))

            changed_manifest_rows = []
            for index, (table, _create_sql) in enumerate(tables, start=1):
                if not _create_sql:
                    continue
                quoted_table = quote_identifier(table)
                source_cursor = source.execute(f"SELECT * FROM {quoted_table}")
                source_columns = [
                    description[0] for description in source_cursor.description or ()
                ]
                target_columns = [
                    description[0]
                    for description in target.execute(
                        f"SELECT * FROM {quoted_table} LIMIT 0"
                    ).description or ()
                ]
                shared_columns = [
                    column for column in source_columns if column in target_columns
                ]
                if not shared_columns:
                    raise RuntimeError(
                        f"No compatible columns found while uploading Turso table {table}."
                    )
                primary_key_columns = local_primary_key_columns(source, table)
                if not set(primary_key_columns).issubset(shared_columns):
                    raise RuntimeError(
                        f"The Turso table {table} is missing one or more primary-key columns."
                    )
                column_indexes = [
                    source_columns.index(column) for column in shared_columns
                ]
                quoted_columns = ", ".join(
                    quote_identifier(column) for column in shared_columns
                )
                local_baseline = {
                    row_key: row_hash
                    for (table_name, row_key), row_hash in local_manifest.items()
                    if table_name == table
                }
                changed_count = 0

                def changed_row_statements():
                    nonlocal changed_count
                    for row in source_cursor:
                        shared_values = tuple(
                            row[column_index] for column_index in column_indexes
                        )
                        row_key = sync_row_key(
                            primary_key_columns, shared_columns, shared_values,
                        )
                        row_hash = sync_row_hash(shared_columns, shared_values)
                        if local_baseline.get(row_key) == row_hash:
                            continue
                        changed_count += 1
                        changed_manifest_rows.append((table, row_key, row_hash))
                        values_sql = ", ".join(
                            sql_literal(value) for value in shared_values
                        )
                        yield (
                            f"INSERT OR REPLACE INTO {quoted_table} ({quoted_columns}) "
                            f"VALUES ({values_sql});"
                        )

                execute_sql_batches(target, changed_row_statements())
                rows_uploaded[table] = changed_count
                if progress_callback:
                    progress_callback(
                        table, changed_count, changed_count, index, len(tables),
                    )

            manifest_statements = (
                f'INSERT OR REPLACE INTO "{SYNC_MANIFEST_TABLE}" '
                f"(table_name, row_key, row_hash) VALUES "
                f"({sql_literal(table_name)}, {sql_literal(row_key)}, "
                f"{sql_literal(row_hash)});"
                for table_name, row_key, row_hash in (
                    local_manifest_rows_to_seed + changed_manifest_rows
                )
            )
            execute_sql_batches(target, manifest_statements)

            indexes = source.execute(
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
                target.execute(create_sql)
            target.commit()

            source.executemany(
                f'INSERT OR REPLACE INTO "{SYNC_MANIFEST_TABLE}" '
                "(table_name, row_key, row_hash) VALUES (?, ?, ?)",
                local_manifest_rows_to_seed + changed_manifest_rows,
            )
            source.commit()
        finally:
            target.close()
    return rows_uploaded


def migrate():
    if not SOURCE_DB_PATH.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {SOURCE_DB_PATH}")
    if not turso_configured():
        raise RuntimeError(
            "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN before running this migration."
        )

    migration_id = source_digest(SOURCE_DB_PATH)
    with closing(sqlite3.connect(SOURCE_DB_PATH)) as source:
        tables = source_tables(source)
        if not tables:
            raise RuntimeError(f"No application tables found in {SOURCE_DB_PATH}.")

        target = connect_turso()
        try:
            target.execute(f"""
                CREATE TABLE IF NOT EXISTS {quote_identifier(MIGRATION_TABLE)} (
                    source_sha256 TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
            """)
            prior = target.execute(
                f"SELECT status FROM {quote_identifier(MIGRATION_TABLE)} "
                "WHERE source_sha256 = ?",
                (migration_id,),
            ).fetchone()
            if prior and prior[0] == "complete":
                print("This SQLite database has already been migrated to Turso.")
                return

            if not prior:
                target_tables = target.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    f"AND name <> '{MIGRATION_TABLE}'"
                ).fetchall()
                populated_tables = []
                for (name,) in target_tables:
                    count = target.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(name)}"
                    ).fetchone()[0]
                    if count:
                        populated_tables.append(name)
                if populated_tables:
                    raise RuntimeError(
                        "Turso already contains application data; refusing to mix it with "
                        "the bundled database. Use an empty Turso database for this migration."
                    )

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            target.execute(
                f"INSERT OR REPLACE INTO {quote_identifier(MIGRATION_TABLE)} "
                "(source_sha256, status, updated_at_utc) VALUES (?, 'started', ?)",
                (migration_id, now),
            )
            target.commit()

            for table, create_sql in tables:
                if not create_sql:
                    continue
                create_sql = re.sub(
                    r"^CREATE TABLE\s+",
                    "CREATE TABLE IF NOT EXISTS ",
                    create_sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
                target.execute(create_sql)
                count = insert_rows(target, source, table)
                expected_count = source.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
                actual_count = target.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                ).fetchone()[0]
                if actual_count != expected_count:
                    raise RuntimeError(
                        f"Row count mismatch for {table}: expected {expected_count}, "
                        f"found {actual_count} in Turso."
                    )
                print(f"Migrated {count:,} rows from {table}.")

            indexes = source.execute(
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
                target.execute(create_sql)

            target.execute(
                f"UPDATE {quote_identifier(MIGRATION_TABLE)} "
                "SET status = 'complete', updated_at_utc = ? WHERE source_sha256 = ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), migration_id),
            )
            target.commit()
            print("SQLite history migration to Turso completed.")
        finally:
            target.close()


if __name__ == "__main__":
    migrate()
