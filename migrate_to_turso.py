"""Copy the bundled SQLite history into an empty Turso database."""

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from database import connect_database, turso_configured

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
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def insert_rows(target, source, table):
    quoted_table = quote_identifier(table)
    cursor = source.execute(f"SELECT * FROM {quoted_table}")
    columns = [description[0] for description in cursor.description]
    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    rows_copied = 0
    batch = []
    batch_size = 0

    for row in cursor:
        values = ", ".join(sql_literal(value) for value in row)
        batch.append(
            f"INSERT OR IGNORE INTO {quoted_table} ({quoted_columns}) VALUES ({values});"
        )
        batch_size += len(batch[-1])
        rows_copied += 1
        if batch_size >= MAX_SCRIPT_SIZE:
            target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")
            batch.clear()
            batch_size = 0

    if batch:
        target.executescript("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;")
    return rows_copied


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

        target = connect_database(SOURCE_DB_PATH)
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
