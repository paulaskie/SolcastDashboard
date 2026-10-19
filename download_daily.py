"""Download and store the latest Solcast forecast. Intended for Windows Task Scheduler."""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from solcast_data import fetch_forecast

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
def main():
    api_key = os.getenv("SOLCAST_API_KEY", "").strip()
    if not api_key:
        print("ERROR: Set the SOLCAST_API_KEY environment variable for the scheduled task.", file=sys.stderr)
        return 2
    try:
        df, _payload = fetch_forecast(api_key)
    except requests.RequestException as exc:
        print(f"ERROR contacting Solcast: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if df.empty:
        print("ERROR: No usable forecast intervals.", file=sys.stderr)
        return 1

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecast_intervals (
            downloaded_at_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            period TEXT,
            pv_estimate REAL,
            pv_estimate10 REAL,
            pv_estimate90 REAL,
            raw_json TEXT,
            PRIMARY KEY (downloaded_at_utc, period_end_utc)
        )
    """)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO forecast_intervals
                (downloaded_at_utc, period_end_utc, period, pv_estimate, pv_estimate10, pv_estimate90, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                stamp,
                pd.Timestamp(row["period_end_utc"]).isoformat(),
                str(row.get("period", "PT30M")),
                None if pd.isna(row["pv_estimate"]) else float(row["pv_estimate"]),
                None if pd.isna(row["pv_estimate10"]) else float(row["pv_estimate10"]),
                None if pd.isna(row["pv_estimate90"]) else float(row["pv_estimate90"]),
                row.to_json(date_format="iso"),
            ))
    conn.close()
    print(f"Saved {len(df)} forecast intervals at {stamp} UTC to {DB_PATH}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        print(f"ERROR contacting Solcast: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
