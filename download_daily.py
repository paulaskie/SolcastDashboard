"""Download and store the latest Solcast forecast. Intended for Windows Task Scheduler."""
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
SITE_ID = "1c66-66cb-9735-8787"
API_URL = f"https://api.solcast.com.au/rooftop_sites/{SITE_ID}/forecasts?format=json"

def parse_period_hours(period):
    if not isinstance(period, str):
        return 0.5
    m = re.fullmatch(r"PT(?:(\\d+(?:\\.\\d+)?)H)?(?:(\\d+(?:\\.\\d+)?)M)?", period)
    if not m:
        return 0.5
    return float(m.group(1) or 0) + float(m.group(2) or 0) / 60

def main():
    api_key = os.getenv("SOLCAST_API_KEY", "").strip()
    if not api_key:
        print("ERROR: Set the SOLCAST_API_KEY environment variable for the scheduled task.", file=sys.stderr)
        return 2
    response = requests.get(API_URL, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }, timeout=30)
    if response.status_code in (401, 403, 402, 429):
        print(f"ERROR: Solcast returned HTTP {response.status_code}: {response.text[:500]}", file=sys.stderr)
        return 1
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("forecasts")
    if not isinstance(rows, list) or not rows:
        print("ERROR: No forecasts list returned by Solcast.", file=sys.stderr)
        return 1
    df = pd.DataFrame(rows)
    if "period_end" not in df.columns or "pv_estimate" not in df.columns:
        print("ERROR: Response missing period_end or pv_estimate fields.", file=sys.stderr)
        return 1
    for col in ("pv_estimate", "pv_estimate10", "pv_estimate90"):
        if col not in df:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "period" not in df:
        df["period"] = "PT30M"
    df["period_end_utc"] = pd.to_datetime(df["period_end"], utc=True, errors="coerce")
    df = df.dropna(subset=["period_end_utc", "pv_estimate"])
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
