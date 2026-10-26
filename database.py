"""Database connection helpers for local SQLite and Turso Cloud."""

import os
import sqlite3
from pathlib import Path

import turso_serverless

TURSO_DATABASE_URL_ENV = "TURSO_DATABASE_URL"
TURSO_AUTH_TOKEN_ENV = "TURSO_AUTH_TOKEN"
USE_TURSO_ENV = "SOLCAST_USE_TURSO"
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


def read_sql_query(query, connection, params=None):
    """Read query results into a DataFrame using a DB-API connection."""
    import pandas as pd

    cursor = connection.execute(query, () if params is None else params)
    columns = [column[0] for column in cursor.description or ()]
    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
