"""Database connection helpers for local SQLite and Turso Cloud."""

import os
import sqlite3
from pathlib import Path

import turso_serverless

TURSO_DATABASE_URL_ENV = "TURSO_DATABASE_URL"
TURSO_AUTH_TOKEN_ENV = "TURSO_AUTH_TOKEN"
DB_OPERATIONAL_ERRORS = (sqlite3.OperationalError, turso_serverless.OperationalError)


def turso_configured():
    return bool(
        os.getenv(TURSO_DATABASE_URL_ENV, "").strip()
        or os.getenv(TURSO_AUTH_TOKEN_ENV, "").strip()
    )


def connect_database(database_path):
    """Connect to Turso when configured, otherwise use the local SQLite file."""
    database_url = os.getenv(TURSO_DATABASE_URL_ENV, "").strip()
    auth_token = os.getenv(TURSO_AUTH_TOKEN_ENV, "").strip()
    if database_url or auth_token:
        if not database_url or not auth_token:
            raise RuntimeError(
                "Configure both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or remove both "
                "to use the local SQLite database."
            )
        return turso_serverless.connect(database_url, auth_token=auth_token)
    return sqlite3.connect(Path(database_path))


def read_sql_query(query, connection, params=None):
    """Read query results into a DataFrame using a DB-API connection."""
    import pandas as pd

    cursor = connection.execute(query, () if params is None else params)
    columns = [column[0] for column in cursor.description or ()]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
