"""Streamlit Community Cloud entry point for the dashboard."""

import os
import runpy
from pathlib import Path

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "solcast_history.sqlite3"

# Community Cloud secrets are exposed through st.secrets, while the existing
# dashboard reads these settings from the process environment. Bridge them
# before running app.py so the local and cloud entry points share one app.
for secret_name in (
    "SOLCAST_API_KEY",
    "AMBER_API_KEY",
    "SIGEN_EXPORT_LIMIT_KW",
):
    if os.getenv(secret_name):
        continue
    try:
        secret_value = st.secrets[secret_name]
    except (FileNotFoundError, KeyError):
        continue
    os.environ[secret_name] = str(secret_value)

if not DATABASE_PATH.is_file():
    st.error(
        "The bundled solcast_history.sqlite3 database is missing. "
        "Include it in the GitHub branch used for this deployment."
    )
    st.stop()

os.environ["SOLCAST_DB_PATH"] = str(DATABASE_PATH)
runpy.run_path(str(APP_DIR / "app.py"), run_name="__main__")
