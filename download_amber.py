"""Download Amber electricity prices for a Sydney local date into the dashboard DB."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from amber_data import fetch_prices
from database import connect_database

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("price_date", help="Sydney local date in YYYY-MM-DD format")
    args = parser.parse_args()
    api_key = os.getenv("AMBER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("AMBER_API_KEY is not set in this process environment.")

    rows = fetch_prices(api_key, args.price_date)

    json_path = APP_DIR / "amber_prices.json"
    temporary_json_path = json_path.with_suffix(".json.tmp")
    temporary_json_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    temporary_json_path.replace(json_path)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_database(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amber_prices (
                start_time_utc TEXT NOT NULL,
                end_time_utc TEXT NOT NULL,
                channel_identifier TEXT NOT NULL DEFAULT '',
                channel_type TEXT NOT NULL DEFAULT '',
                price_type TEXT NOT NULL DEFAULT '',
                per_kwh REAL,
                spot_per_kwh REAL,
                fetched_at_utc TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (start_time_utc, end_time_utc, channel_identifier, channel_type, price_type)
            )
        """)
        amber_columns = {row[1] for row in conn.execute("PRAGMA table_info(amber_prices)")}
        if "channel_type" not in amber_columns:
            old_rows = conn.execute("SELECT * FROM amber_prices").fetchall()
            conn.execute("ALTER TABLE amber_prices RENAME TO amber_prices_old")
            conn.execute("""
                CREATE TABLE amber_prices (
                    start_time_utc TEXT NOT NULL,
                    end_time_utc TEXT NOT NULL,
                    channel_identifier TEXT NOT NULL DEFAULT '',
                    channel_type TEXT NOT NULL DEFAULT '',
                    price_type TEXT NOT NULL DEFAULT '',
                    per_kwh REAL,
                    spot_per_kwh REAL,
                    fetched_at_utc TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (start_time_utc, end_time_utc, channel_identifier, channel_type, price_type)
                )
            """)
            for old_row in old_rows:
                raw = json.loads(old_row[7])
                conn.execute("""
                    INSERT INTO amber_prices
                    (start_time_utc, end_time_utc, channel_identifier, channel_type, price_type,
                     per_kwh, spot_per_kwh, fetched_at_utc, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (old_row[0], old_row[1], old_row[2], str(raw.get("channelType") or ""), old_row[3],
                      old_row[4], old_row[5], old_row[6], old_row[7]))
            conn.execute("DROP TABLE amber_prices_old")
        fetched_dates = {str(row.get("date")) for row in rows if row.get("date")}
        if fetched_dates:
            for row_id, raw_json in conn.execute("SELECT rowid, raw_json FROM amber_prices").fetchall():
                try:
                    saved_date = json.loads(raw_json).get("date")
                except (TypeError, json.JSONDecodeError):
                    continue
                if saved_date in fetched_dates:
                    conn.execute("DELETE FROM amber_prices WHERE rowid = ?", (row_id,))
        for row in rows:
            conn.execute("""
                INSERT OR REPLACE INTO amber_prices
                (start_time_utc, end_time_utc, channel_identifier, channel_type, price_type,
                 per_kwh, spot_per_kwh, fetched_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(row["startTime"]), str(row["endTime"]),
                str(row.get("channelIdentifier") or ""), str(row.get("channelType") or ""),
                str(row.get("type") or ""),
                row.get("perKwh"), row.get("spotPerKwh"), fetched_at,
                json.dumps(row, separators=(",", ":")),
            ))
    print(f"Saved {len(rows)} Amber price intervals for {args.price_date} to {DB_PATH.name}.")


if __name__ == "__main__":
    main()
