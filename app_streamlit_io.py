"""Streamlit Community Cloud entry point for the dashboard."""

import os
import runpy
from pathlib import Path

import streamlit as st
from database import USE_TURSO_ENV


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "solcast_history.sqlite3"

# Community Cloud secrets are exposed through st.secrets, while the existing
# dashboard reads these settings from the process environment. Bridge them
# before running app.py so the local and cloud entry points share one app.
for secret_name in (
    "SOLCAST_API_KEY",
    "AMBER_API_KEY",
    "SIGEN_EXPORT_LIMIT_KW",
    "SIGEN_APP_KEY",
    "SIGEN_APP_SECRET",
    "SIGEN_REGION",
    "SIGEN_API_BASE_URL",
    "SIGEN_SYSTEM_ID",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
):
    if os.getenv(secret_name):
        continue
    try:
        secret_value = st.secrets[secret_name]
    except (FileNotFoundError, KeyError):
        continue
    os.environ[secret_name] = str(secret_value)

has_turso_url = bool(os.getenv("TURSO_DATABASE_URL", "").strip())
has_turso_token = bool(os.getenv("TURSO_AUTH_TOKEN", "").strip())
if has_turso_url != has_turso_token:
    st.error(
        "Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or remove both "
        "to use the bundled database."
    )
    st.stop()

os.environ[USE_TURSO_ENV] = "0"

if not has_turso_url and not DATABASE_PATH.is_file():
    st.error(
        "The bundled solcast_history.sqlite3 database is missing. "
        "Configure Turso or include it in the GitHub branch used for this deployment."
    )
    st.stop()

os.environ["SOLCAST_DB_PATH"] = str(DATABASE_PATH)

if has_turso_url:
    @st.cache_resource
    def _load_turso_snapshot(database_path, database_url):
        from database import pull_turso_snapshot

        return pull_turso_snapshot(database_path)

    try:
        _load_turso_snapshot(str(DATABASE_PATH), os.getenv("TURSO_DATABASE_URL", "").strip())
    except Exception as error:
        st.error(f"Could not load the Turso database snapshot: {error}")
        st.stop()

runpy.run_path(str(APP_DIR / "app.py"), run_name="__main__")
