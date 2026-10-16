import os
import sqlite3
import hashlib
import json
import subprocess
import sys
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
SITE_ID_DEFAULT = "1c66-66cb-9735-8787"
SITE_URL = f"https://api.solcast.com.au/rooftop_sites/{SITE_ID_DEFAULT}"
AUTH_CHECK_URL = f"{SITE_URL}/estimated_actuals?format=json"
ACTUALS_URL = AUTH_CHECK_URL
API_URL = f"{SITE_URL}/forecasts?format=json"
TZ_NAME = "Australia/Sydney"
SIGEN_STATION_ID = 2_610_011_552_188_752
PV_SYSTEM_CAPACITY_KW = 9.5
OPENMETEO_PERFORMANCE_RATIO = 0.80
SIGEN_MCP_SCRIPT = Path(os.getenv(
    "SIGEN_MCP_SCRIPT",
    str(Path.home() / ".codex" / "skills" / "sigen-open-platform" / "scripts" / "sigen.mjs"),
))

def first_existing_file(*names):
    candidates = [APP_DIR / name for name in names]
    return next((path for path in candidates if path.exists()), candidates[0])

ENERGY_ACTUALS_FILE = first_existing_file(
    "Sigen_Energy_Actuals.xlsx",
    "20261001_1600_20261014_1000_Sigen_Energy_Actuals.xlsx",
    "20261001_1600_20261014_1000_Energy_Actuals.xlsx",
)
POWER_ACTUALS_FILE = first_existing_file(
    "Sigen_Power_Actuals.xlsx",
    "20261001_1605_20261014_1050_Power_Actuals.xlsx",
)
AMBER_SITE_ID = "01M4M4A4ZJW5FK6KCP6JKMC10H"
AMBER_PRICES_URL = f"https://api.amber.com.au/v1/sites/{AMBER_SITE_ID}/prices"
AMBER_CURRENT_PRICES_URL = f"{AMBER_PRICES_URL}/current"

st.set_page_config(page_title="Solcast Solar Forecast", page_icon="☀️", layout="wide")

def connect_db():
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actual_intervals (
            downloaded_at_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            period TEXT,
            pv_estimate REAL,
            raw_json TEXT,
            PRIMARY KEY (downloaded_at_utc, period_end_utc)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meter_energy_actuals (
            period_end_utc TEXT PRIMARY KEY,
            period_end_local TEXT NOT NULL,
            energy_kwh REAL NOT NULL,
            source_file TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meter_power_actuals (
            period_end_utc TEXT PRIMARY KEY,
            period_end_local TEXT NOT NULL,
            power_kw REAL NOT NULL,
            source_file TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS amber_prices (
            start_time_utc TEXT NOT NULL,
            end_time_utc TEXT NOT NULL,
            channel_type TEXT NOT NULL DEFAULT '',
            price_type TEXT NOT NULL DEFAULT '',
            per_kwh REAL,
            spot_per_kwh REAL,
            fetched_at_utc TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (start_time_utc, end_time_utc, channel_type, price_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS amber_price_forecast_snapshots (
            start_time_utc TEXT NOT NULL,
            end_time_utc TEXT NOT NULL,
            channel_type TEXT NOT NULL DEFAULT '',
            price_type TEXT NOT NULL DEFAULT 'ForecastInterval',
            per_kwh REAL,
            spot_per_kwh REAL,
            fetched_at_utc TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (start_time_utc, end_time_utc, channel_type, fetched_at_utc)
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO amber_price_forecast_snapshots
        (start_time_utc, end_time_utc, channel_type, price_type, per_kwh, spot_per_kwh,
         fetched_at_utc, raw_json)
        SELECT start_time_utc, end_time_utc, channel_type, price_type, per_kwh, spot_per_kwh,
               fetched_at_utc, raw_json
          FROM amber_prices
         WHERE lower(price_type) = 'forecastinterval'
    """)
    for table in ("meter_energy_actuals", "meter_power_actuals"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "raw_json" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{{}}'")
    return conn

def fetch_amber_prices(api_key, price_date):
    response = requests.get(
        AMBER_PRICES_URL,
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
        params={"startDate": price_date, "endDate": price_date, "resolution": 5},
        timeout=30,
    )
    if response.status_code in (401, 403):
        raise ValueError(f"Amber rejected the API key ({response.status_code}). Check AMBER_API_KEY and site access.")
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Amber response was not a list of price intervals.")
    return rows

def fetch_amber_current_prices(api_key, next_intervals=576):
    response = requests.get(
        AMBER_CURRENT_PRICES_URL,
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
        params={"next": next_intervals, "resolution": 5},
        timeout=45,
    )
    if response.status_code in (401, 403):
        raise ValueError(f"Amber rejected the API key ({response.status_code}). Check AMBER_API_KEY and site access.")
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Amber current-prices response was not a list of intervals.")
    return rows

def save_amber_prices(rows):
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    conn = connect_db()
    with conn:
        for row in rows:
            values = (
                str(row["startTime"]), str(row["endTime"]),
                str(row.get("channelType") or ""), str(row.get("type") or ""),
                row.get("perKwh"), row.get("spotPerKwh"), fetched_at,
                json.dumps(row, separators=(",", ":")),
            )
            if str(row.get("type") or "").lower() == "forecastinterval":
                conn.execute("""
                    INSERT OR IGNORE INTO amber_price_forecast_snapshots
                    (start_time_utc, end_time_utc, channel_type, price_type, per_kwh,
                     spot_per_kwh, fetched_at_utc, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, values)
            conn.execute("""
                INSERT OR REPLACE INTO amber_prices
                (start_time_utc, end_time_utc, channel_type, price_type,
                 per_kwh, spot_per_kwh, fetched_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
    conn.close()
    return fetched_at

def refresh_amber_actuals_since_last_current(api_key):
    conn = connect_db()
    row = conn.execute(
        "SELECT end_time_utc FROM amber_prices "
        "WHERE lower(price_type) = 'currentinterval' "
        "ORDER BY fetched_at_utc DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError("No saved Amber CurrentInterval was found. Fetch Amber prices first to establish the starting point.")

    start_utc = pd.to_datetime(row[0], utc=True)
    now_utc = pd.Timestamp.now(tz="UTC")
    if start_utc >= now_utc:
        return 0

    # Amber's historical prices endpoint accepts local calendar dates, so fetch
    # every date touched by the missing interval and keep only actuals in the gap.
    start_local_date = start_utc.tz_convert(TZ_NAME).date()
    end_local_date = now_utc.tz_convert(TZ_NAME).date()
    rows_to_save = []
    for day in pd.date_range(start_local_date, end_local_date, freq="D"):
        day_rows = fetch_amber_prices(api_key, day.date().isoformat())
        for price_row in day_rows:
            if str(price_row.get("type") or "").lower() not in {"actualinterval", "currentinterval"}:
                continue
            interval_end = pd.to_datetime(price_row.get("endTime"), utc=True, errors="coerce")
            if pd.isna(interval_end) or not (start_utc < interval_end <= now_utc):
                continue
            rows_to_save.append(price_row)
    if rows_to_save:
        save_amber_prices(rows_to_save)
    return len(rows_to_save)

def load_amber_prices():
    conn = connect_db()
    actuals = pd.read_sql_query(
        "SELECT * FROM amber_prices WHERE lower(price_type) <> 'forecastinterval'", conn,
    )
    forecasts = pd.read_sql_query(
        "SELECT * FROM amber_price_forecast_snapshots ORDER BY fetched_at_utc DESC", conn,
    )
    conn.close()
    prices = pd.concat([actuals, forecasts], ignore_index=True)
    if not prices.empty:
        prices["start_time_local"] = pd.to_datetime(prices["start_time_utc"], utc=True).dt.tz_convert(TZ_NAME)
        prices["end_time_local"] = pd.to_datetime(prices["end_time_utc"], utc=True).dt.tz_convert(TZ_NAME)
    return prices

def parse_period_hours(period):
    # Solcast normally returns PT30M; supports PT15M, PT1H, etc.
    import re
    if not isinstance(period, str):
        return 0.5
    m = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?", period)
    if not m:
        return 0.5
    return float(m.group(1) or 0) + float(m.group(2) or 0) / 60

def api_headers(api_key):
    return {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}

def check_authorization(api_key):
    """Pre-flight checkpoint: confirm the key can access this rooftop site before fetching forecasts."""
    # Solcast documents forecasts and estimated_actuals for hobbyist rooftop sites;
    # use estimated_actuals as the pre-flight authentication check.
    response = requests.get(AUTH_CHECK_URL, headers=api_headers(api_key), timeout=20)
    if response.status_code == 401:
        raise ValueError("Authorization failed (401 Unauthorized). Check your Solcast API key.")
    if response.status_code == 403:
        raise ValueError("Authorization check failed (403 Forbidden). This key may not have access to this rooftop site.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required during authorization check. Check your account or plan.")
    if response.status_code == 429:
        raise ValueError("Solcast returned 429 during the authorization check. The request quota/rate limit may be exhausted; do not retry repeatedly.")
    response.raise_for_status()
    return response.json() if response.content else {}

def fetch_forecast(api_key):
    response = requests.get(
        API_URL,
        headers=api_headers(api_key),
        timeout=30,
    )
    if response.status_code == 401:
        raise ValueError("Solcast rejected the API key (401 Unauthorized). Check the key and try again.")
    if response.status_code == 403:
        raise ValueError("Solcast returned 403 Forbidden. Check that your account/key has access to this endpoint.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required. You may have reached an account or plan limit.")
    if response.status_code == 429:
        raise ValueError("Solcast rate limit reached (429). Wait until the quota resets before trying again. Response: " + response.text[:500])
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("forecasts")
    if not isinstance(rows, list):
        raise ValueError("The response did not contain a 'forecasts' list. Check the API response format.")
    if not rows:
        raise ValueError("Solcast returned no forecast intervals.")
    df = pd.DataFrame(rows)
    if "period_end" not in df.columns:
        raise ValueError("Forecast response is missing 'period_end'.")
    # Current rooftop-site API uses pv_estimate in kW. Keep optional percentile bands if present.
    for col in ("pv_estimate", "pv_estimate10", "pv_estimate90"):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["period"] = df["period"] if "period" in df.columns else "PT30M"
    df["period_hours"] = df["period"].map(parse_period_hours)
    df["period_end_utc"] = pd.to_datetime(df["period_end"], utc=True, errors="coerce")
    df = df.dropna(subset=["period_end_utc", "pv_estimate"]).copy()
    if df.empty:
        raise ValueError("No usable pv_estimate values were found in the response.")
    df["period_start_utc"] = df["period_end_utc"] - pd.to_timedelta(df["period_hours"], unit="h")
    df["period_end_local"] = df["period_end_utc"].dt.tz_convert(TZ_NAME)
    df["period_start_local"] = df["period_start_utc"].dt.tz_convert(TZ_NAME)
    df["local_date"] = df["period_end_local"].dt.date.astype(str)
    # Integrate average power (kW) across interval length (hours) to estimate energy (kWh).
    df["energy_kwh"] = df["pv_estimate"] * df["period_hours"]
    df["energy10_kwh"] = pd.to_numeric(df["pv_estimate10"], errors="coerce") * df["period_hours"]
    df["energy90_kwh"] = pd.to_numeric(df["pv_estimate90"], errors="coerce") * df["period_hours"]
    return df, payload

def fetch_estimated_actuals(api_key):
    response = requests.get(ACTUALS_URL, headers=api_headers(api_key), timeout=30)
    if response.status_code == 401:
        raise ValueError("Solcast rejected the API key (401 Unauthorized) for estimated actuals.")
    if response.status_code == 403:
        raise ValueError("Solcast returned 403 Forbidden for estimated actuals.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required for estimated actuals.")
    if response.status_code == 429:
        raise ValueError("Solcast rate limit reached (429) while fetching estimated actuals. Wait until the quota resets.")
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("estimated_actuals")
    if not isinstance(rows, list):
        raise ValueError("The response did not contain an 'estimated_actuals' list.")
    df = pd.DataFrame(rows)
    if df.empty:
        return df, payload
    value_col = "pv_estimate" if "pv_estimate" in df.columns else "pv_power_rooftop"
    if value_col not in df.columns or "period_end" not in df.columns:
        raise ValueError("Estimated actuals response is missing power values or 'period_end'.")
    df["pv_estimate"] = pd.to_numeric(df[value_col], errors="coerce")
    df["period"] = df["period"] if "period" in df.columns else "PT30M"
    df["period_end_utc"] = pd.to_datetime(df["period_end"], utc=True, errors="coerce")
    df = df.dropna(subset=["period_end_utc", "pv_estimate"]).copy()
    df["period_hours"] = df["period"].map(parse_period_hours)
    df["period_end_local"] = df["period_end_utc"].dt.tz_convert(TZ_NAME)
    df["local_date"] = df["period_end_local"].dt.date.astype(str)
    df["energy_kwh"] = df["pv_estimate"] * df["period_hours"]
    return df, payload

def save_forecast(df, payload):
    downloaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = connect_db()
    with conn:
        for _, row in df.iterrows():
            raw = row.to_json(date_format="iso")
            conn.execute("""
                INSERT OR REPLACE INTO forecast_intervals
                (downloaded_at_utc, period_end_utc, period, pv_estimate, pv_estimate10, pv_estimate90, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                downloaded_at,
                pd.Timestamp(row["period_end_utc"]).isoformat(),
                str(row.get("period", "PT30M")),
                None if pd.isna(row["pv_estimate"]) else float(row["pv_estimate"]),
                None if pd.isna(row["pv_estimate10"]) else float(row["pv_estimate10"]),
                None if pd.isna(row["pv_estimate90"]) else float(row["pv_estimate90"]),
                raw,
            ))
    conn.close()
    return downloaded_at

def save_actuals(df, downloaded_at):
    if df.empty:
        return
    conn = connect_db()
    with conn:
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO actual_intervals
                (downloaded_at_utc, period_end_utc, period, pv_estimate, raw_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                downloaded_at,
                pd.Timestamp(row["period_end_utc"]).isoformat(),
                str(row.get("period", "PT30M")),
                float(row["pv_estimate"]),
                row.to_json(date_format="iso"),
            ))
    conn.close()

def load_latest_actuals():
    conn = connect_db()
    actuals = pd.read_sql_query("SELECT * FROM actual_intervals ORDER BY downloaded_at_utc, period_end_utc", conn)
    conn.close()
    if actuals.empty:
        return actuals
    actuals["downloaded_at_utc"] = pd.to_datetime(actuals["downloaded_at_utc"], utc=True)
    actuals["period_end_utc"] = pd.to_datetime(actuals["period_end_utc"], utc=True)
    # Keep the most recently fetched estimate for each timestamp while retaining
    # actuals from every saved seven-day response.
    actuals = (actuals.sort_values("downloaded_at_utc")
               .drop_duplicates("period_end_utc", keep="last").copy())
    actuals["period_end_local"] = actuals["period_end_utc"].dt.tz_convert(TZ_NAME)
    actuals["local_date"] = actuals["period_end_local"].dt.date.astype(str)
    actuals["period_hours"] = actuals["period"].map(parse_period_hours)
    actuals["energy_kwh"] = actuals["pv_estimate"] * actuals["period_hours"]
    return actuals.sort_values("period_end_local")

def load_history():
    conn = connect_db()
    hist = pd.read_sql_query("SELECT * FROM forecast_intervals ORDER BY downloaded_at_utc, period_end_utc", conn)
    conn.close()
    if hist.empty:
        return hist
    hist["downloaded_at_utc"] = pd.to_datetime(hist["downloaded_at_utc"], utc=True)
    hist["period_end_utc"] = pd.to_datetime(hist["period_end_utc"], utc=True)
    hist["period_end_local"] = hist["period_end_utc"].dt.tz_convert(TZ_NAME)
    hist["period_hours"] = hist["period"].map(parse_period_hours)
    hist["energy_kwh"] = hist["pv_estimate"] * hist["period_hours"]
    hist["local_date"] = hist["period_end_local"].dt.date.astype(str)
    return hist

def load_openmeteo_hourly():
    conn = connect_db()
    try:
        weather = pd.read_sql_query(
            "SELECT time_utc, time_local, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM openmeteo_hourly ORDER BY time_utc",
            conn,
        )
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

def load_openmeteo_forecast_hourly():
    conn = connect_db()
    try:
        weather = pd.read_sql_query(
            "SELECT time_utc, time_local, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM openmeteo_forecast_hourly ORDER BY time_utc",
            conn,
        )
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

def load_openmeteo_daily():
    conn = connect_db()
    daily_frames = []
    for table in ("openmeteo_daily", "openmeteo_forecast_daily"):
        try:
            weather_daily = pd.read_sql_query(
                f"SELECT date_local, sunrise_local, sunset_local, daylight_duration FROM {table}", conn,
            )
        except sqlite3.OperationalError:
            continue
        if not weather_daily.empty:
            daily_frames.append(weather_daily)
    conn.close()
    if not daily_frames:
        return pd.DataFrame(columns=["local_date", "sunrise_local", "sunset_local", "daylight_duration", "daylight_hours"])
    weather_daily = pd.concat(daily_frames, ignore_index=True)
    weather_daily["daylight_duration"] = pd.to_numeric(weather_daily["daylight_duration"], errors="coerce")
    for column in ("sunrise_local", "sunset_local"):
        weather_daily[column] = pd.to_datetime(weather_daily[column], utc=True, errors="coerce").dt.tz_convert(TZ_NAME)
    # Archive observations take precedence on dates covered by both tables.
    weather_daily = weather_daily.drop_duplicates("date_local", keep="first")
    weather_daily["daylight_hours"] = weather_daily["daylight_duration"] / 3600
    return weather_daily.rename(columns={"date_local": "local_date"})[[
        "local_date", "sunrise_local", "sunset_local", "daylight_duration", "daylight_hours",
    ]]

def prepare_openmeteo_estimates(weather):
    if weather.empty:
        return weather
    weather["time_local"] = pd.to_datetime(weather["time_utc"], utc=True).dt.tz_convert(TZ_NAME)
    weather["local_date"] = weather["time_local"].dt.date.astype(str)
    weather["global_tilted_irradiance"] = pd.to_numeric(
        weather["global_tilted_irradiance"], errors="coerce",
    )
    weather["global_tilted_irradiance_east"] = pd.to_numeric(
        weather["global_tilted_irradiance_east"], errors="coerce",
    )
    weather["weighted_panel_irradiance"] = (
        weather["global_tilted_irradiance"] * 0.80
        + weather["global_tilted_irradiance_east"] * 0.20
    )
    # Plane-weighted GTI is W/m²; scale by array capacity and assumed system PR.
    weather["openmeteo_power_kw"] = (
        weather["weighted_panel_irradiance"] / 1000
        * PV_SYSTEM_CAPACITY_KW * OPENMETEO_PERFORMANCE_RATIO
    )
    return weather.dropna(subset=["time_local", "openmeteo_power_kw"])

def daily_summary(df):
    out = df.groupby("local_date", as_index=False).agg(
        Forecast_kWh=("energy_kwh", "sum"),
        Peak_kW=("pv_estimate", "max"),
        Intervals=("pv_estimate", "count"),
    )
    return out.sort_values("local_date")

def load_excel_actuals(path, value_column):
    """Load a local inverter export and interpret its naive timestamps as Sydney local time."""
    if not path.exists():
        return pd.DataFrame()
    available_columns = pd.read_excel(path, nrows=0).columns.tolist()
    usecols = ["Date", value_column]
    if "Raw Sigen JSON" in available_columns:
        usecols.append("Raw Sigen JSON")
    df = pd.read_excel(path, usecols=usecols)
    timestamps = pd.to_datetime(df["Date"], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(TZ_NAME, ambiguous="NaT", nonexistent="NaT")
    else:
        timestamps = timestamps.dt.tz_convert(TZ_NAME)
    df["period_end_local"] = timestamps
    df["local_date"] = timestamps.dt.date.astype(str)
    df[value_column] = pd.to_numeric(df[value_column], errors="coerce")
    return df.dropna(subset=["period_end_local", value_column]).sort_values("period_end_local")

def save_meter_actuals(energy_actuals, power_actuals, source_file=None):
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    energy_source = source_file or ENERGY_ACTUALS_FILE.name
    power_source = source_file or POWER_ACTUALS_FILE.name
    conn = connect_db()
    with conn:
        for _, row in energy_actuals.iterrows():
            period_end = pd.Timestamp(row["period_end_local"])
            raw_json = row.get("Raw Sigen JSON")
            if not isinstance(raw_json, str) or not raw_json.strip():
                raw_json = row.to_json(date_format="iso")
            conn.execute("""
                INSERT INTO meter_energy_actuals
                    (period_end_utc, period_end_local, energy_kwh, source_file, imported_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_end_utc) DO UPDATE SET
                    period_end_local=excluded.period_end_local,
                    energy_kwh=excluded.energy_kwh,
                    source_file=excluded.source_file,
                    imported_at_utc=excluded.imported_at_utc,
                    raw_json=excluded.raw_json
                WHERE period_end_local IS NOT excluded.period_end_local
                   OR energy_kwh IS NOT excluded.energy_kwh
                   OR source_file IS NOT excluded.source_file
                   OR raw_json IS NOT excluded.raw_json
            """, (
                period_end.tz_convert("UTC").isoformat(), period_end.isoformat(),
                float(row["Total Solar Production Energy (kWh)"]), energy_source, imported_at, raw_json,
            ))
        for _, row in power_actuals.iterrows():
            period_end = pd.Timestamp(row["period_end_local"])
            raw_json = row.get("Raw Sigen JSON")
            if not isinstance(raw_json, str) or not raw_json.strip():
                raw_json = row.to_json(date_format="iso")
            conn.execute("""
                INSERT INTO meter_power_actuals
                    (period_end_utc, period_end_local, power_kw, source_file, imported_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_end_utc) DO UPDATE SET
                    period_end_local=excluded.period_end_local,
                    power_kw=excluded.power_kw,
                    source_file=excluded.source_file,
                    imported_at_utc=excluded.imported_at_utc,
                    raw_json=excluded.raw_json
                WHERE period_end_local IS NOT excluded.period_end_local
                   OR power_kw IS NOT excluded.power_kw
                   OR source_file IS NOT excluded.source_file
                   OR raw_json IS NOT excluded.raw_json
            """, (
                period_end.tz_convert("UTC").isoformat(), period_end.isoformat(),
                float(row["Solar Production (kW)"]), power_source, imported_at, raw_json,
            ))
    conn.close()

def fetch_sigen_day(station_id, day):
    if not SIGEN_MCP_SCRIPT.exists():
        raise FileNotFoundError(
            f"Sigen Open Platform client not found at {SIGEN_MCP_SCRIPT}. "
            "Set SIGEN_MCP_SCRIPT to the installed sigen.mjs path."
        )
    args = {
        "station_id": int(station_id),
        "dataset": "energy_statistics",
        "period": "day",
        "start_date": day.strftime("%Y%m%d"),
        "end_date": day.strftime("%Y%m%d"),
    }
    result = subprocess.run(
        ["node", str(SIGEN_MCP_SCRIPT), "call", "get_station_energy_data", json.dumps(args)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
        # CLI diagnostics deliberately avoid exposing Sigen credentials.
        message = result.stderr.strip() or "Sigen Open Platform request failed."
        raise RuntimeError(message[-1500:])
    try:
        envelope = json.loads(result.stdout)
        response_text = next(item["text"] for item in envelope["content"] if item.get("type") == "text")
        payload = json.loads(response_text)
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise ValueError("Sigen returned an unexpected energy-statistics response.") from exc
    if not payload.get("ok"):
        raise RuntimeError("Sigen did not return energy statistics for this date.")
    return payload.get("data", {}).get("itemList", [])

def refresh_sigen_actuals(station_id, start_date, end_date):
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    power_rows = []
    for day in pd.date_range(start_date, end_date, freq="D"):
        for item in fetch_sigen_day(station_id, day.date()):
            stamp = pd.to_datetime(item.get("dataTime"), format="%Y%m%d %H:%M", errors="coerce")
            power_kw = pd.to_numeric(item.get("pvTotalPower"), errors="coerce")
            if pd.isna(stamp) or pd.isna(power_kw):
                continue
            local_stamp = stamp.tz_localize(TZ_NAME, ambiguous="NaT", nonexistent="NaT")
            if pd.isna(local_stamp):
                continue
            raw = json.dumps(item, separators=(",", ":"), ensure_ascii=False)
            power_rows.append({
                "period_end_local": local_stamp,
                "Solar Production (kW)": float(power_kw),
                "Raw Sigen JSON": raw,
            })
    if not power_rows:
        return 0
    power_df = pd.DataFrame(power_rows).sort_values("period_end_local")
    energy_df = power_df[["period_end_local", "Raw Sigen JSON"]].copy()
    energy_df["Total Solar Production Energy (kWh)"] = power_df["Solar Production (kW)"] * (5 / 60)
    save_meter_actuals(energy_df, power_df, source_file="Sigen Open Platform")
    return len(power_df)

def load_meter_actuals():
    conn = connect_db()
    energy = pd.read_sql_query("SELECT period_end_utc, energy_kwh FROM meter_energy_actuals ORDER BY period_end_utc", conn)
    power = pd.read_sql_query("SELECT period_end_utc, power_kw FROM meter_power_actuals ORDER BY period_end_utc", conn)
    conn.close()
    if not energy.empty:
        energy["period_end_utc"] = pd.to_datetime(energy["period_end_utc"], utc=True)
        energy["period_end_local"] = energy["period_end_utc"].dt.tz_convert(TZ_NAME)
        energy["local_date"] = energy["period_end_local"].dt.date.astype(str)
        energy["Total Solar Production Energy (kWh)"] = energy["energy_kwh"]
    if not power.empty:
        power["period_end_utc"] = pd.to_datetime(power["period_end_utc"], utc=True)
        power["period_end_local"] = power["period_end_utc"].dt.tz_convert(TZ_NAME)
        power["local_date"] = power["period_end_local"].dt.date.astype(str)
        power["Solar Production (kW)"] = power["power_kw"]
    return energy, power

st.title("☀️ Solcast Solar Forecast Dashboard")
st.caption("Rooftop PV forecast · Daily energy estimates in Australia/Sydney time")

with st.sidebar:
    st.header("Connection")
    api_key = st.text_input(
        "Solcast API key",
        value=os.getenv("SOLCAST_API_KEY", ""),
        type="password",
        help="Used for the request only. The dashboard does not save the key to its database.",
    )
    st.caption(f"Site ID: `{SITE_ID_DEFAULT}`")
    key_fingerprint = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest() if api_key.strip() else ""
    check_clicked = st.button("1. Check API authorization", width="stretch", disabled=not bool(api_key.strip()))
    authorized = bool(key_fingerprint) and st.session_state.get("authorized_key_fingerprint") == key_fingerprint
    if authorized:
        st.success("Authorization checkpoint passed for the current API key.")
    else:
        st.info("Check authorization before downloading a forecast.")
    fetch_clicked = st.button("2. Download latest forecast", type="primary", width="stretch", disabled=not authorized)
    st.caption("The authorization check makes a separate estimated-actuals API request and may count toward your Solcast quota. Avoid repeated checks, especially after a 429.")
    st.divider()
    st.header("Data")
    st.write(f"Database: `{DB_PATH.name}`")
    st.caption("The dashboard stores every download locally. For automatic daily downloads when the app is closed, see README.md.")
    openmeteo_refresh_clicked = st.button("Refresh Open-Meteo data and forecast", width="stretch")
    st.caption("Updates archive irradiance from 8 October through its latest available date and the 7-day weather forecast in SQLite.")
    st.divider()
    st.header("Sigen Open Platform")
    st.caption(f"Station ID: `{SIGEN_STATION_ID}`")
    sigen_start_date = st.date_input(
        "Actuals start date", value=pd.Timestamp("2026-10-01").date(), key="sigen_start_date",
    )
    sigen_end_date = st.date_input(
        "Actuals end date (Sydney)", value=pd.Timestamp.now(tz=TZ_NAME).date(), key="sigen_end_date",
    )
    sigen_update_clicked = st.button(
        "Update Power Actuals from Sigen",
        disabled=sigen_end_date < sigen_start_date,
        help="Uses the installed Sigen Open Platform client. Its browser authorization stays with the client; no Sigen token is stored by this app.",
    )
    st.caption("Reads five-minute solar power through the selected end date and stores power, derived energy, and raw Sigen rows in SQLite.")
    st.divider()
    st.header("Amber prices")
    amber_api_key = st.text_input(
        "Amber API key",
        value=os.getenv("AMBER_API_KEY", ""),
        type="password",
        help="Read from AMBER_API_KEY. The key is only sent to Amber and is not stored in the database.",
    )
    amber_date = st.date_input("Price date (Sydney)", value=pd.Timestamp.now(tz=TZ_NAME).date())
    amber_fetch_clicked = st.button("Fetch Amber prices and 2-day forecast", disabled=not bool(amber_api_key.strip()))
    amber_48_clicked = st.button("Fetch next 48 five-minute periods", disabled=not bool(amber_api_key.strip()))
    amber_update_actuals_clicked = st.button(
        "Update Amber prices since last CurrentInterval",
        disabled=not bool(amber_api_key.strip()),
        help="Fetches actual Amber intervals from the most recently saved CurrentInterval through now.",
    )

if openmeteo_refresh_clicked:
    try:
        with st.spinner("Refreshing Open-Meteo archive and 7-day forecast…"):
            result = subprocess.run(
                [sys.executable, str(APP_DIR / "openmeteo.py")],
                cwd=APP_DIR, capture_output=True, text=True, timeout=180,
            )
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip() or "Open-Meteo refresh failed."
            st.error(details)
        else:
            st.session_state["openmeteo_refresh_message"] = (
                result.stdout.strip().splitlines()[-1]
                if result.stdout.strip() else "Open-Meteo data refreshed."
            )
            st.rerun()
    except Exception as e:
        st.error(f"Could not refresh Open-Meteo data: {e}")

if st.session_state.get("openmeteo_refresh_message"):
    st.success(st.session_state.pop("openmeteo_refresh_message"))

if amber_fetch_clicked:
    try:
        with st.spinner("Fetching prices from Amber…"):
            amber_rows = fetch_amber_prices(amber_api_key, amber_date.isoformat())
            amber_current_rows = fetch_amber_current_prices(amber_api_key, next_intervals=576)
            save_amber_prices(amber_rows)
            save_amber_prices(amber_current_rows)
        forecast_count = sum(1 for row in amber_current_rows if row.get("type") == "ForecastInterval")
        st.session_state["amber_download_message"] = (
            f"Saved {len(amber_rows)} price intervals for {amber_date.isoformat()} and "
            f"{forecast_count} five-minute forecast intervals from the current-prices feed."
        )
        st.rerun()
    except requests.RequestException as e:
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if st.session_state.get("amber_download_message"):
    st.success(st.session_state.pop("amber_download_message"))

if amber_update_actuals_clicked:
    try:
        with st.spinner("Updating Amber actual prices through now…"):
            amber_updated_count = refresh_amber_actuals_since_last_current(amber_api_key)
        st.session_state["amber_download_message"] = (
            f"Updated {amber_updated_count:,} actual Amber price intervals since the last CurrentInterval."
        )
        st.rerun()
    except requests.RequestException as e:
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if amber_48_clicked:
    try:
        with st.spinner("Fetching the next 48 five-minute periods from Amber…"):
            amber_48_rows = fetch_amber_current_prices(amber_api_key, next_intervals=48)
            save_amber_prices(amber_48_rows)
        forecast_count = sum(1 for row in amber_48_rows if row.get("type") == "ForecastInterval")
        st.session_state["amber_download_message"] = (
            f"Saved {forecast_count} forecast intervals from Amber's next-48-period current-prices feed."
        )
        st.rerun()
    except requests.RequestException as e:
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if sigen_update_clicked:
    try:
        with st.spinner("Updating meter actuals from Sigen Open Platform…"):
            sigen_saved = refresh_sigen_actuals(
                SIGEN_STATION_ID, sigen_start_date, sigen_end_date,
            )
        st.session_state["sigen_update_message"] = (
            f"Updated {sigen_saved:,} five-minute power and energy actuals through "
            f"{sigen_end_date.isoformat()}."
        )
        st.rerun()
    except Exception as e:
        st.error(f"Could not update actuals from Sigen: {e}")

if st.session_state.get("sigen_update_message"):
    st.success(st.session_state.pop("sigen_update_message"))

if check_clicked:
    st.session_state.pop("authorized_key_fingerprint", None)
    if not api_key.strip():
        st.error("Enter your Solcast API key first.")
    else:
        try:
            with st.spinner("Checking authorization for this rooftop site…"):
                check_authorization(api_key)
            st.session_state["authorized_key_fingerprint"] = key_fingerprint
            st.success("Authorization successful. The forecast download is now enabled.")
            st.rerun()
        except requests.RequestException as e:
            st.error(f"Could not complete authorization check: {e}")
        except Exception as e:
            st.error(str(e))

if fetch_clicked:
    # Re-check the authorization checkpoint in code, not just in the button state.
    if not api_key.strip() or st.session_state.get("authorized_key_fingerprint") != key_fingerprint:
        st.error("Authorization checkpoint has not passed for this API key. Check authorization first.")
    else:
        try:
            with st.spinner("Downloading forecast from Solcast…"):
                fresh, payload = fetch_forecast(api_key)
                stamp = save_forecast(fresh, payload)
            actuals = pd.DataFrame()
            actuals_error = None
            try:
                actuals, _ = fetch_estimated_actuals(api_key)
                save_actuals(actuals, stamp)
            except Exception as e:
                actuals_error = str(e)
            st.session_state["last_download_message"] = f"Saved {len(fresh)} forecast intervals and {len(actuals)} estimated actual intervals at {stamp} UTC."
            if actuals_error:
                st.session_state["actuals_download_error"] = actuals_error
            else:
                st.session_state.pop("actuals_download_error", None)
            st.rerun()
        except requests.RequestException as e:
            st.error(f"Could not contact Solcast: {e}")
        except Exception as e:
            st.error(str(e))

if st.session_state.get("last_download_message"):
    st.success(st.session_state["last_download_message"])
if st.session_state.get("actuals_download_error"):
    st.warning(f"Estimated actuals could not be refreshed; showing the most recently saved actuals, if available. {st.session_state['actuals_download_error']}")

try:
    energy_workbook = load_excel_actuals(ENERGY_ACTUALS_FILE, "Total Solar Production Energy (kWh)")
    power_workbook = load_excel_actuals(POWER_ACTUALS_FILE, "Solar Production (kW)")
    if (not energy_workbook.empty and not power_workbook.empty
            and "Raw Sigen JSON" in energy_workbook.columns):
        sigen_raw_by_time = dict(zip(
            energy_workbook["period_end_local"], energy_workbook["Raw Sigen JSON"],
        ))
        power_workbook["Raw Sigen JSON"] = power_workbook["period_end_local"].map(sigen_raw_by_time)
    save_meter_actuals(energy_workbook, power_workbook)
except ImportError:
    st.error("Reading the Excel actuals requires openpyxl. Install project dependencies from requirements.txt.")
except Exception as e:
    st.warning(f"Could not import the Excel actuals workbooks: {e}")

energy_actuals, power_actuals = load_meter_actuals()
amber_prices = load_amber_prices()
st.subheader("Amber electricity price over time")
if amber_prices.empty:
    st.info("No Amber prices are saved yet. Set AMBER_API_KEY and choose **Fetch Amber prices** in the sidebar.")
else:
    amber_prices["local_date"] = amber_prices["start_time_local"].dt.date.astype(str)
    price_dates = sorted(pd.to_datetime(amber_prices["local_date"]).dt.date.unique())
    amber_default = amber_date if amber_date in price_dates else price_dates[-1]
    amber_date_col, amber_channel_col = st.columns([1, 2])
    with amber_date_col:
        amber_chosen_date = st.date_input(
            "Price date (Sydney)", value=amber_default,
            min_value=price_dates[0], max_value=price_dates[-1], key="amber_price_date_picker",
        )
    with amber_channel_col:
        available_channels = list(dict.fromkeys(amber_prices["channel_type"].dropna().astype(str)))
        default_channels = [channel for channel in ("feedIn", "general") if channel in available_channels]
        selected_channels = st.multiselect(
            "Channels", available_channels,
            default=default_channels or available_channels,
            key="amber_price_channels",
        )
    amber_intraday = amber_prices[amber_prices["local_date"] == amber_chosen_date.isoformat()].copy()
    amber_intraday = amber_intraday[
        amber_intraday["channel_type"].isin(selected_channels)
        & amber_intraday["per_kwh"].notna()
        & amber_intraday["price_type"].str.lower().isin(
            ("actualinterval", "currentinterval", "forecastinterval")
        )
    ].copy()
    amber_intraday["_start_utc"] = pd.to_datetime(amber_intraday["start_time_utc"], utc=True)
    amber_intraday["_end_utc"] = pd.to_datetime(amber_intraday["end_time_utc"], utc=True)
    actual_like = amber_intraday[
        amber_intraday["price_type"].str.lower().isin(("actualinterval", "currentinterval"))
    ].copy()
    actual_like["_priority"] = actual_like["price_type"].str.lower().map(
        {"actualinterval": 0, "currentinterval": 1}
    )
    actual_like["_source_duration_minutes"] = (
        (actual_like["_end_utc"] - actual_like["_start_utc"]).dt.total_seconds() / 60
    ).round().astype(int)
    actual_like = (actual_like.sort_values(
        ["_priority", "_source_duration_minutes", "fetched_at_utc"],
        ascending=[True, True, False],
    ).drop_duplicates(["channel_type", "start_time_utc"], keep="first"))
    forecast = amber_intraday[amber_intraday["price_type"].str.lower() == "forecastinterval"].copy()
    forecast["_source_duration_minutes"] = (
        (forecast["_end_utc"] - forecast["_start_utc"]).dt.total_seconds() / 60
    ).round().astype(int)
    if amber_chosen_date < pd.Timestamp.now(tz=TZ_NAME).date():
        # Historical days should show finalized prices, never stale forecasts.
        forecast = forecast.iloc[0:0].copy()
    forecast = (forecast.sort_values(
        ["fetched_at_utc", "_source_duration_minutes"], ascending=[False, True],
    ).drop_duplicates(["channel_type", "start_time_utc", "end_time_utc"], keep="first"))
    intervals = pd.concat([actual_like, forecast], ignore_index=True, sort=False)
    # Expand any longer Amber source interval to 5-minute buckets, then retain
    # one row per bucket: actual, current, then shortest/latest forecast.
    bucket_rows = []
    for _, source_row in intervals.iterrows():
        bucket_start = source_row["_start_utc"].floor("5min")
        while bucket_start < source_row["_end_utc"]:
            bucket = source_row.copy()
            bucket["_bucket_start_utc"] = bucket_start
            bucket_rows.append(bucket)
            bucket_start += pd.Timedelta(minutes=5)
    amber_intraday = pd.DataFrame(bucket_rows) if bucket_rows else intervals.iloc[0:0].copy()
    if not amber_intraday.empty:
        amber_intraday["_priority"] = amber_intraday["price_type"].str.lower().map(
            {"actualinterval": 0, "currentinterval": 1, "forecastinterval": 2}
        )
        amber_intraday = (amber_intraday.sort_values(
            ["_priority", "_source_duration_minutes", "fetched_at_utc"],
            ascending=[True, True, False],
        ).drop_duplicates(["channel_type", "_bucket_start_utc"], keep="first"))
        amber_intraday["start_time_local"] = amber_intraday["_bucket_start_utc"].dt.tz_convert(TZ_NAME)
        amber_intraday = amber_intraday.sort_values("start_time_local")
    chart_prices = amber_intraday["per_kwh"].astype(float).where(
        amber_intraday["channel_type"] != "feedIn", -amber_intraday["per_kwh"].astype(float)
    ).dropna()
    use_log_axis = not chart_prices.empty and chart_prices.abs().max() > 100
    # Use the symmetric-log transform for large prices so a zero tick can
    # always be shown, including when every selected price is positive.
    use_symmetric_log = use_log_axis

    def symmetric_log(value):
        return np.sign(value) * np.log10(1 + np.abs(value))

    price_fig = go.Figure()
    channel_colors = {"general": "#2ca02c", "feedIn": "#f1c40f", "controlledLoad": "#9467bd"}
    for channel_type, channel_prices in amber_intraday.groupby("channel_type", sort=False):
        channel_prices = channel_prices.sort_values("start_time_local")
        actual_prices = channel_prices[channel_prices["price_type"].str.lower() != "forecastinterval"]
        forecast_prices = channel_prices[channel_prices["price_type"].str.lower() == "forecastinterval"]
        line_color = channel_colors.get(channel_type)

        def add_amber_price_trace(price_series, dash, showlegend, name):
            if price_series.empty:
                return
            plotted_price = -price_series["per_kwh"] if channel_type == "feedIn" else price_series["per_kwh"]
            source_resolution = price_series["_source_duration_minutes"].astype(str) + " min source interval"
            plotted_y = symmetric_log(plotted_price) if use_symmetric_log else plotted_price
            price_fig.add_trace(go.Scatter(
                x=price_series["start_time_local"], y=plotted_y,
                mode="lines", connectgaps=False,
                name=name, showlegend=showlegend,
                line=dict(color=line_color, dash=dash),
                customdata=list(zip(
                    plotted_price.to_numpy(), source_resolution.to_numpy(),
                    price_series["price_type"].to_numpy(),
                )),
                hovertemplate="%{x|%H:%M}<br>Price: %{customdata[0]:.3f} ¢/kWh"
                              "<br>Amber type: %{customdata[2]}<br>%{customdata[1]}<extra></extra>",
            ))

        legend_name = f"Amber · {channel_type} (¢/kWh)"
        if actual_prices.empty:
            # Keep a solid channel key in the legend even on forecast-only dates.
            price_fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines", name=legend_name,
                line=dict(color=line_color),
            ))
        else:
            add_amber_price_trace(actual_prices, "solid", True, legend_name)
        add_amber_price_trace(forecast_prices, "dot", False, legend_name)
    if use_symmetric_log:
        price_limit = float(chart_prices.abs().max())
        max_exponent = max(0, math.ceil(math.log10(price_limit)))
        tick_prices = {0.0}
        for exponent in range(max_exponent + 1):
            decade = 10 ** exponent
            tick_prices.update(value * decade for value in (1, 2, 5) if value * decade <= price_limit)
        symmetric_ticks = sorted({
            tick for value in tick_prices for tick in (-value, value)
            if chart_prices.min() <= tick <= chart_prices.max()
        } | {0.0})
        axis_config = dict(
            type="linear", title="Price (¢/kWh, symmetric log)", tickmode="array",
            tickvals=[float(symmetric_log(value)) for value in symmetric_ticks],
            ticktext=[f"{value:g}" for value in symmetric_ticks],
            range=[
                float(symmetric_log(min(0.0, chart_prices.min()))),
                float(symmetric_log(max(0.0, chart_prices.max()))),
            ],
        )
    else:
        axis_config = dict(title="Price (¢/kWh)", rangemode="tozero")
    price_fig.update_layout(xaxis_title="Time (Sydney)", yaxis=axis_config,
                            margin=dict(l=10, r=10, t=20, b=10), hovermode="closest")
    st.plotly_chart(price_fig, width="stretch")

history = load_history()
if history.empty:
    st.info("No forecast data saved yet. Enter your API key in the sidebar and choose **Download latest forecast**.")
    st.markdown("""
    **What this dashboard will show**
    - Daily forecast energy (kWh)
    - Half-hourly forecast power (kW)
    - Historical forecast downloads, so you can compare how forecasts changed
    """)
    st.stop()

# Latest complete API download is one snapshot.
latest_stamp = history["downloaded_at_utc"].max()
latest = history[history["downloaded_at_utc"] == latest_stamp].copy()
latest = latest.sort_values("period_end_local")
actuals = load_latest_actuals()
openmeteo_hourly = load_openmeteo_hourly()
openmeteo_forecast_hourly = load_openmeteo_forecast_hourly()
openmeteo_daily = load_openmeteo_daily()
daily = daily_summary(latest)
if not actuals.empty:
    actuals_daily = actuals.groupby("local_date", as_index=False).agg(
        Estimated_actual_kWh=("energy_kwh", "sum"),
    )
    daily = daily.merge(actuals_daily, on="local_date", how="outer")
else:
    daily["Estimated_actual_kWh"] = pd.NA
if not energy_actuals.empty:
    meter_energy_daily = energy_actuals.groupby("local_date", as_index=False).agg(
        Actual_energy_kWh=("Total Solar Production Energy (kWh)", "sum"),
    )
    daily = daily.merge(meter_energy_daily, on="local_date", how="outer")
else:
    daily["Actual_energy_kWh"] = pd.NA
if not power_actuals.empty:
    meter_daylight = power_actuals.copy()
    if not openmeteo_daily.empty:
        meter_daylight = meter_daylight.merge(
            openmeteo_daily[["local_date", "sunrise_local", "sunset_local"]],
            on="local_date", how="left",
        )
    else:
        meter_daylight["sunrise_local"] = pd.NaT
        meter_daylight["sunset_local"] = pd.NaT

    def daylight_hours_for_sigen_sample(row):
        period_end = row["period_end_local"]
        sunrise = row["sunrise_local"]
        sunset = row["sunset_local"]
        if pd.isna(sunrise) or pd.isna(sunset):
            # Keep the Sigen duration useful when Open-Meteo sunrise/sunset are unavailable.
            sample_hours = 5 / 60
            return pd.Series({
                "Sigen_nonzero_hours": sample_hours if row["Solar Production (kW)"] > 0 else 0.0,
                "Sigen_daylight_hours_covered": np.nan,
            })
        interval_start = period_end - pd.Timedelta(minutes=5)
        overlap_start = max(interval_start, sunrise)
        overlap_end = min(period_end, sunset)
        overlap_hours = max(0.0, (overlap_end - overlap_start).total_seconds() / 3600)
        return pd.Series({
            "Sigen_nonzero_hours": overlap_hours if row["Solar Production (kW)"] > 0 else 0.0,
            "Sigen_daylight_hours_covered": overlap_hours,
        })

    meter_daylight[["Sigen_nonzero_hours", "Sigen_daylight_hours_covered"]] = meter_daylight.apply(
        daylight_hours_for_sigen_sample, axis=1,
    )
    meter_nonzero_daily = meter_daylight.groupby("local_date", as_index=False).agg(
        Sigen_nonzero_hours=("Sigen_nonzero_hours", "sum"),
    )
    meter_daylight_covered = (
        meter_daylight.groupby("local_date")["Sigen_daylight_hours_covered"]
        .sum(min_count=1).rename("Sigen_daylight_hours_covered").reset_index()
    )
    meter_nonzero_daily = meter_nonzero_daily.merge(meter_daylight_covered, on="local_date", how="left")
    daily = daily.merge(
        meter_nonzero_daily[["local_date", "Sigen_nonzero_hours", "Sigen_daylight_hours_covered"]],
        on="local_date", how="outer",
    )
else:
    daily["Sigen_nonzero_hours"] = pd.NA
    daily["Sigen_daylight_hours_covered"] = pd.NA
if not openmeteo_daily.empty:
    daily = daily.merge(openmeteo_daily[[
        "local_date", "sunrise_local", "sunset_local", "daylight_duration", "daylight_hours",
    ]],
                        on="local_date", how="outer")
else:
    daily["sunrise_local"] = pd.NaT
    daily["sunset_local"] = pd.NaT
    daily["daylight_duration"] = pd.NA
    daily["daylight_hours"] = pd.NA
daily["Forecast_kWh"] = pd.to_numeric(daily["Forecast_kWh"], errors="coerce")
daily["Estimated_actual_kWh"] = pd.to_numeric(daily["Estimated_actual_kWh"], errors="coerce")
daily["Actual_energy_kWh"] = pd.to_numeric(daily["Actual_energy_kWh"], errors="coerce")
daily["Intervals"] = daily["Intervals"].fillna(0).astype(int)
daily["Sigen_nonzero_hours"] = pd.to_numeric(daily["Sigen_nonzero_hours"], errors="coerce")
daily["Sigen_daylight_hours_covered"] = pd.to_numeric(daily["Sigen_daylight_hours_covered"], errors="coerce")
daily["daylight_hours"] = pd.to_numeric(daily["daylight_hours"], errors="coerce")
daily["daylight_hours_comparison"] = daily["Sigen_daylight_hours_covered"].combine_first(daily["daylight_hours"])
daily["Stacked_energy_kWh"] = daily["Forecast_kWh"].fillna(0) + daily["Actual_energy_kWh"].fillna(0)
daily = daily.sort_values("local_date").reset_index(drop=True)
today = pd.Timestamp.now(tz=TZ_NAME).date().isoformat()
tomorrow = (pd.Timestamp.now(tz=TZ_NAME).normalize() + pd.Timedelta(days=1)).date().isoformat()
today_row = daily.loc[daily["local_date"] == today]
today_total_kwh = None
if not today_row.empty:
    today_total_kwh = today_row["Forecast_kWh"].fillna(0).iloc[0] + today_row["Actual_energy_kWh"].fillna(0).iloc[0]
tomorrow_val = daily.loc[daily["local_date"] == tomorrow, "Forecast_kWh"].dropna()
best = daily.loc[daily["Forecast_kWh"].idxmax()] if not daily.empty else None

k1, k2, k3, k4 = st.columns(4)
k1.metric("Today forecast + meter actual", f"{today_total_kwh:.1f} kWh" if today_total_kwh is not None else "—")
k2.metric("Tomorrow forecast", f"{tomorrow_val.iloc[0]:.1f} kWh" if len(tomorrow_val) else "—")
k3.metric("Best forecast day", f"{best['Forecast_kWh']:.1f} kWh" if best is not None else "—",
          help=str(best["local_date"]) if best is not None else None)
k4.metric("Peak forecast power", f"{latest['pv_estimate'].max():.2f} kW")
st.caption(f"Latest forecast downloaded: {latest_stamp.strftime('%d %b %Y, %H:%M UTC')} · {len(latest)} intervals")

st.subheader("Daily forecast generation")
daily["Forecast_tooltip"] = daily["Forecast_kWh"].map(
    lambda value: f"{value:.2f} kWh" if pd.notna(value) else "—"
)
daily["Estimated_actual_tooltip"] = daily["Estimated_actual_kWh"].map(
    lambda value: f"{value:.2f} kWh" if pd.notna(value) else "—"
)
daily["Meter_actual_tooltip"] = daily["Actual_energy_kWh"].map(
    lambda value: f"{value:.2f} kWh" if pd.notna(value) else "—"
)
daily["Sigen_nonzero_tooltip"] = daily["Sigen_nonzero_hours"].map(
    lambda value: f"{value:.2f} h" if pd.notna(value) else "—"
)
daily["daylight_tooltip"] = daily["daylight_hours_comparison"].map(
    lambda value: f"{value:.2f} h" if pd.notna(value) else "—"
)
daily_tooltip_data = daily[["Forecast_tooltip", "Estimated_actual_tooltip", "Meter_actual_tooltip",
                            "Sigen_nonzero_tooltip", "daylight_tooltip"]].to_numpy()
fig = go.Figure()
fig.add_trace(go.Bar(
    x=daily["local_date"], y=daily["Actual_energy_kWh"],
    name="Sigen meter actual energy (kWh)",
    customdata=daily_tooltip_data,
    hovertemplate="%{x}<br>Forecast: %{customdata[0]}<br>Meter actual: %{customdata[2]}<br>Solcast estimated actual: %{customdata[1]}<br>Sigen non-zero production: %{customdata[3]}<br>Open-Meteo daylight duration: %{customdata[4]}<extra></extra>"
))
fig.add_trace(go.Bar(
    x=daily["local_date"], y=daily["Forecast_kWh"],
    name="Solcast forecast energy (kWh)",
    customdata=daily_tooltip_data,
    hovertemplate="%{x}<br>Forecast: %{customdata[0]}<br>Meter actual: %{customdata[2]}<br>Solcast estimated actual: %{customdata[1]}<br>Sigen non-zero production: %{customdata[3]}<br>Open-Meteo daylight duration: %{customdata[4]}<extra></extra>"
))
fig.add_trace(go.Scatter(
    x=daily["local_date"], y=daily["Estimated_actual_kWh"],
    name="Solcast estimated actual energy (kWh)", mode="lines+markers",
    line=dict(color="#f2a541"), marker=dict(size=6),
    customdata=daily_tooltip_data,
    hovertemplate="%{x}<br>Forecast: %{customdata[0]}<br>Meter actual: %{customdata[2]}<br>Solcast estimated actual: %{customdata[1]}<br>Sigen non-zero production: %{customdata[3]}<br>Open-Meteo daylight duration: %{customdata[4]}<extra></extra>"
))
fig.update_layout(barmode="stack")
fig.update_layout(xaxis_title="Date (Sydney)", yaxis_title="Energy (kWh)",
                  margin=dict(l=10, r=10, t=20, b=10), xaxis_tickangle=-35)
st.plotly_chart(fig, width="stretch")

st.subheader("Non-zero production hours vs daylight duration covered")
st.caption("For incomplete dates, the daylight bar and Sigen production hours use only recorded five-minute intervals that overlap sunrise-to-sunset.")
sunshine_fig = go.Figure()
sunshine_fig.add_trace(go.Bar(
    x=daily["local_date"], y=daily["Sigen_nonzero_hours"],
    name="Sigen production above 0 kW (hours)",
    hovertemplate="%{x}<br>Sigen production above 0 kW: %{y:.2f} h<extra></extra>",
))
sunshine_fig.add_trace(go.Bar(
    x=daily["local_date"], y=daily["daylight_hours_comparison"],
    name="Open-Meteo daylight within Sigen data coverage (hours)",
    hovertemplate="%{x}<br>Open-Meteo daylight within Sigen data coverage: %{y:.2f} h<extra></extra>",
))
sunshine_fig.update_layout(
    barmode="group", xaxis_title="Date (Sydney)", yaxis_title="Hours",
    margin=dict(l=10, r=10, t=20, b=10), xaxis_tickangle=-35,
)
st.plotly_chart(sunshine_fig, width="stretch")

st.subheader("Solcast Forecast and actual power by time")
available_dates = sorted(set(latest["local_date"])
                         .union(actuals["local_date"] if not actuals.empty else [])
                         .union(power_actuals["local_date"] if not power_actuals.empty else []))
actual_dates = set(actuals["local_date"]) if not actuals.empty else set()
actual_dates.update(power_actuals["local_date"] if not power_actuals.empty else [])
overlap_dates = sorted(set(latest["local_date"]).intersection(actual_dates))
default_date = overlap_dates[-1] if overlap_dates else (available_dates[-1] if available_dates else None)
chosen_date = st.date_input(
    "Solcast comparison date",
    value=pd.Timestamp(default_date).date(),
    min_value=pd.Timestamp(available_dates[0]).date(),
    max_value=pd.Timestamp(available_dates[-1]).date(),
    key="solcast_comparison_date_picker",
).isoformat()
intraday = latest[latest["local_date"] == chosen_date].copy()
actual_intraday = actuals[actuals["local_date"] == chosen_date].copy() if not actuals.empty else pd.DataFrame()
meter_power_intraday = (power_actuals[power_actuals["local_date"] == chosen_date].copy()
                        if not power_actuals.empty else pd.DataFrame())
def format_kwh(value):
    return f"{value:.3f} kWh" if pd.notna(value) else "—"

forecast_energy_by_time = {
    row["period_end_utc"]: row["energy_kwh"] for _, row in intraday.iterrows()
}
actual_energy_by_time = {
    row["period_end_utc"]: row["energy_kwh"] for _, row in actual_intraday.iterrows()
}
if not intraday.empty:
    intraday["forecast_kwh_tooltip"] = intraday["energy_kwh"].map(format_kwh)
    intraday["actual_kwh_tooltip"] = intraday["period_end_utc"].map(
        lambda period_end: format_kwh(actual_energy_by_time.get(period_end, float("nan")))
    )
if not actual_intraday.empty:
    actual_intraday["actual_kwh_tooltip"] = actual_intraday["energy_kwh"].map(format_kwh)
    actual_intraday["forecast_kwh_tooltip"] = actual_intraday["period_end_utc"].map(
        lambda period_end: format_kwh(forecast_energy_by_time.get(period_end, float("nan")))
    )

solcast_comparison_weather = pd.DataFrame()
if not actual_intraday.empty and not meter_power_intraday.empty:
    solcast_power = actual_intraday.set_index("period_end_local")[["pv_estimate"]].sort_index()
    actual_times = pd.DatetimeIndex(meter_power_intraday["period_end_local"])
    combined_times = solcast_power.index.union(actual_times).sort_values()
    aligned_solcast_power = (
        solcast_power.reindex(combined_times)
        .interpolate(method="time", limit_area="inside")
        .reindex(actual_times)
    )
    solcast_comparison_weather = pd.DataFrame({
        "time_local": actual_times,
        "solcast_estimated_power_kw": aligned_solcast_power["pv_estimate"].to_numpy(),
    }).dropna(subset=["solcast_estimated_power_kw"])

solcast_estimate_kwh = None
solcast_meter_kwh = None
solcast_accuracy_score = None
solcast_comparison_intervals = 0
if not solcast_comparison_weather.empty and not meter_power_intraday.empty:
    matched_solcast_power = meter_power_intraday[
        ["period_end_local", "Solar Production (kW)"]
    ].merge(
        solcast_comparison_weather[["time_local", "solcast_estimated_power_kw"]],
        left_on="period_end_local", right_on="time_local", how="inner",
    )
    solcast_comparison_intervals = len(matched_solcast_power)
    solcast_estimate_kwh = matched_solcast_power["solcast_estimated_power_kw"].sum() * (5 / 60)
    solcast_meter_kwh = matched_solcast_power["Solar Production (kW)"].sum() * (5 / 60)
    if solcast_meter_kwh > 0:
        solcast_accuracy_score = max(
            0.0,
            100 * (1 - abs(solcast_estimate_kwh - solcast_meter_kwh) / solcast_meter_kwh),
        )

solcast_metric_cols = st.columns(3)
solcast_metric_cols[0].metric(
    "Solcast estimated actual energy",
    f"{solcast_estimate_kwh:.2f} kWh" if solcast_estimate_kwh is not None else "—",
    help="Estimated energy over five-minute Sigen timestamps that have matching Solcast estimated-actual data.",
)
solcast_metric_cols[1].metric(
    "Sigen actual energy (matched periods)",
    f"{solcast_meter_kwh:.2f} kWh" if solcast_meter_kwh is not None else "—",
    help="Meter energy over the same matched five-minute periods as the Solcast estimate.",
)
solcast_metric_cols[2].metric(
    "Solcast estimate accuracy",
    f"{solcast_accuracy_score:.1f}%" if solcast_accuracy_score is not None else "—",
    help="100% minus absolute percentage error against Sigen matched-period energy, floored at 0%.",
)
if solcast_comparison_intervals:
    st.caption(f"Daily Solcast energy comparison uses {solcast_comparison_intervals:,} matched five-minute Sigen intervals.")
elif meter_power_intraday.empty:
    st.caption("No Sigen power actuals are available for this date, so the energy comparison and accuracy score are unavailable.")
else:
    st.caption("No matching Solcast estimated-actual and Sigen timestamps are available for this date, so the energy comparison and accuracy score are unavailable.")
fig2 = go.Figure()
if not intraday.empty:
    fig2.add_trace(go.Scatter(
        x=intraday["period_end_local"], y=intraday["pv_estimate"],
        mode="lines", name="Solcast forecast power (kW)",
        line=dict(width=2, color="#f2a541", dash="dot"),
        customdata=intraday[["forecast_kwh_tooltip", "actual_kwh_tooltip"]].to_numpy(),
        hovertemplate="%{x|%H:%M}<br>Forecast: %{y:.2f} kW<br>Forecast energy: %{customdata[0]}<br>Estimated actual energy: %{customdata[1]}<extra></extra>"
    ))
    if intraday["pv_estimate10"].notna().any():
        fig2.add_trace(go.Scatter(x=intraday["period_end_local"], y=intraday["pv_estimate90"],
                                  mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig2.add_trace(go.Scatter(x=intraday["period_end_local"], y=intraday["pv_estimate10"],
                                  mode="lines", line=dict(width=0), fill="tonexty",
                                  fillcolor="rgba(100,149,237,0.18)", name="Solcast 10–90% range", hoverinfo="skip"))
if not actual_intraday.empty:
    fig2.add_trace(go.Scatter(
        x=actual_intraday["period_end_local"], y=actual_intraday["pv_estimate"],
        mode="lines+markers", name="Solcast estimated actual power (kW)",
        line=dict(width=2, color="#f2a541"), marker=dict(size=5),
        customdata=actual_intraday[["forecast_kwh_tooltip", "actual_kwh_tooltip"]].to_numpy(),
        hovertemplate="%{x|%H:%M}<br>Estimated actual: %{y:.2f} kW<br>Forecast energy: %{customdata[0]}<br>Estimated actual energy: %{customdata[1]}<extra></extra>"
    ))
if not meter_power_intraday.empty:
    fig2.add_trace(go.Scatter(
        x=meter_power_intraday["period_end_local"],
        y=meter_power_intraday["Solar Production (kW)"],
        mode="lines", name="Sigen meter actual power (kW)",
        line=dict(width=2, color="#2a9d8f"),
        hovertemplate="%{x|%H:%M}<br>Meter actual power: %{y:.3f} kW<extra></extra>"
    ))
if intraday.empty and actual_intraday.empty and meter_power_intraday.empty:
    st.info("No forecast or estimated actual intervals are saved for this date.")
fig2.update_layout(xaxis_title="Time (Sydney)", yaxis_title="Power (kW)",
                   margin=dict(l=10, r=10, t=20, b=10), hovermode="closest")
st.plotly_chart(fig2, width="stretch")

st.subheader("Open-Meteo estimated and actual power by time")
st.caption(
    "Open-Meteo estimates use 22° tilted irradiance weighted for your array: "
    "80% north-facing and 20% east-facing, scaled to 9.5 kW with an assumed "
    "0.80 performance ratio. Actual power comes from the Sigen meter. "
    "The archive estimate is interpolated to Sigen timestamps; the forecast keeps all hourly points for the selected day."
)
openmeteo_dates = sorted(openmeteo_hourly["local_date"].unique()) if not openmeteo_hourly.empty else []
openmeteo_forecast_dates = (
    sorted(openmeteo_forecast_hourly["local_date"].unique())
    if not openmeteo_forecast_hourly.empty else []
)
complete_openmeteo_dates = []
if openmeteo_dates:
    valid_weather_rows = openmeteo_hourly.groupby("local_date")["global_tilted_irradiance"].count()
    complete_openmeteo_dates = sorted(valid_weather_rows[valid_weather_rows >= 100].index)
meter_dates = set(power_actuals["local_date"]) if not power_actuals.empty else set()
all_openmeteo_dates = sorted(set(openmeteo_dates).union(openmeteo_forecast_dates))
default_openmeteo_dates = sorted(
    (set(complete_openmeteo_dates).union(openmeteo_forecast_dates)).intersection(meter_dates)
)
if not default_openmeteo_dates:
    default_openmeteo_dates = complete_openmeteo_dates or all_openmeteo_dates
if all_openmeteo_dates:
    default_openmeteo_date = default_openmeteo_dates[-1]
    openmeteo_chosen_date_value = st.date_input(
        "Open-Meteo comparison date",
        value=pd.Timestamp(default_openmeteo_date).date(),
        min_value=pd.Timestamp(all_openmeteo_dates[0]).date(),
        max_value=pd.Timestamp(all_openmeteo_dates[-1]).date(),
        key="openmeteo_comparison_date_picker",
    )
    openmeteo_chosen_date = openmeteo_chosen_date_value.isoformat()
else:
    openmeteo_chosen_date = None
    st.info("No Open-Meteo archive or forecast data is saved yet. Refresh it from the sidebar.")
openmeteo_intraday = (
    openmeteo_hourly[openmeteo_hourly["local_date"] == openmeteo_chosen_date].copy()
    if openmeteo_chosen_date is not None else pd.DataFrame()
)
openmeteo_forecast_intraday = (
    openmeteo_forecast_hourly[openmeteo_forecast_hourly["local_date"] == openmeteo_chosen_date].copy()
    if openmeteo_chosen_date is not None and not openmeteo_forecast_hourly.empty else pd.DataFrame()
)
meter_openmeteo_intraday = (
    power_actuals[power_actuals["local_date"] == openmeteo_chosen_date].copy()
    if openmeteo_chosen_date is not None and not power_actuals.empty else pd.DataFrame()
)
openmeteo_aligned_to_actuals = pd.DataFrame()
if not openmeteo_intraday.empty and not meter_openmeteo_intraday.empty:
    weather_power = openmeteo_intraday.set_index("time_local")[[
        "openmeteo_power_kw", "weighted_panel_irradiance",
    ]].sort_index()
    actual_times = pd.DatetimeIndex(meter_openmeteo_intraday["period_end_local"])
    combined_times = weather_power.index.union(actual_times).sort_values()
    aligned_weather = (
        weather_power.reindex(combined_times)
        .interpolate(method="time", limit_area="inside")
        .reindex(actual_times)
    )
    openmeteo_aligned_to_actuals = pd.DataFrame({
        "time_local": actual_times,
        "openmeteo_power_kw": aligned_weather["openmeteo_power_kw"].to_numpy(),
        "weighted_panel_irradiance": aligned_weather["weighted_panel_irradiance"].to_numpy(),
    }).dropna(subset=["openmeteo_power_kw"])

# Compare estimated and measured energy over the same five-minute Sigen samples.
comparison_weather = openmeteo_aligned_to_actuals
if comparison_weather.empty and not openmeteo_forecast_intraday.empty and not meter_openmeteo_intraday.empty:
    forecast_power = openmeteo_forecast_intraday.set_index("time_local")[["openmeteo_power_kw"]].sort_index()
    actual_times = pd.DatetimeIndex(meter_openmeteo_intraday["period_end_local"])
    combined_times = forecast_power.index.union(actual_times).sort_values()
    aligned_forecast = (
        forecast_power.reindex(combined_times)
        .interpolate(method="time", limit_area="inside")
        .reindex(actual_times)
    )
    comparison_weather = pd.DataFrame({
        "time_local": actual_times,
        "openmeteo_power_kw": aligned_forecast["openmeteo_power_kw"].to_numpy(),
    }).dropna(subset=["openmeteo_power_kw"])

openmeteo_estimate_kwh = None
meter_comparison_kwh = None
accuracy_score = None
comparison_intervals = 0
if not comparison_weather.empty and not meter_openmeteo_intraday.empty:
    matched_power = meter_openmeteo_intraday[
        ["period_end_local", "Solar Production (kW)"]
    ].merge(
        comparison_weather[["time_local", "openmeteo_power_kw"]],
        left_on="period_end_local", right_on="time_local", how="inner",
    )
    comparison_intervals = len(matched_power)
    openmeteo_estimate_kwh = matched_power["openmeteo_power_kw"].sum() * (5 / 60)
    meter_comparison_kwh = matched_power["Solar Production (kW)"].sum() * (5 / 60)
    if meter_comparison_kwh > 0:
        accuracy_score = max(
            0.0,
            100 * (1 - abs(openmeteo_estimate_kwh - meter_comparison_kwh) / meter_comparison_kwh),
        )

metric_cols = st.columns(3)
metric_cols[0].metric(
    "Open-Meteo estimated energy",
    f"{openmeteo_estimate_kwh:.2f} kWh" if openmeteo_estimate_kwh is not None else "—",
    help="Estimated energy over Sigen timestamps that have matching Open-Meteo data.",
)
metric_cols[1].metric(
    "Sigen actual energy (matched periods)",
    f"{meter_comparison_kwh:.2f} kWh" if meter_comparison_kwh is not None else "—",
    help="Meter energy over the same matched five-minute periods as the Open-Meteo estimate.",
)
metric_cols[2].metric(
    "Open-Meteo estimate accuracy",
    f"{accuracy_score:.1f}%" if accuracy_score is not None else "—",
    help="100% minus absolute percentage error against Sigen matched-period energy, floored at 0%.",
)
if comparison_intervals:
    st.caption(f"Daily energy comparison uses {comparison_intervals:,} matched five-minute Sigen intervals.")
elif meter_openmeteo_intraday.empty:
    st.caption("No Sigen power actuals are available for this date, so the energy comparison and accuracy score are unavailable.")
else:
    st.caption("No matching Open-Meteo and Sigen timestamps are available for this date, so the energy comparison and accuracy score are unavailable.")
fig3 = go.Figure()
if not meter_openmeteo_intraday.empty:
    fig3.add_trace(go.Scatter(
        x=meter_openmeteo_intraday["period_end_local"],
        y=meter_openmeteo_intraday["Solar Production (kW)"],
        mode="lines", name="Sigen meter actual power (kW)",
        line=dict(width=2, color="#2a9d8f"),
        hovertemplate="%{x|%H:%M}<br>Sigen meter actual: %{y:.3f} kW<extra></extra>"
    ))
if not openmeteo_intraday.empty:
    plotted_weather = openmeteo_aligned_to_actuals if not openmeteo_aligned_to_actuals.empty else openmeteo_intraday
    fig3.add_trace(go.Scatter(
        x=plotted_weather["time_local"], y=plotted_weather["openmeteo_power_kw"],
        mode="lines", name="Open-Meteo satellite estimate (kW)",
        line=dict(width=2, color="#f2a541"),
        customdata=plotted_weather["weighted_panel_irradiance"],
        hovertemplate="%{x|%H:%M}<br>Open-Meteo estimate: %{y:.2f} kW"
                      "<br>80/20 weighted plane irradiance: %{customdata:.0f} W/m²<extra></extra>"
    ))
if not openmeteo_forecast_intraday.empty:
    # Keep every forecast hour visible; Sigen actuals can cover only part of today.
    plotted_forecast = openmeteo_forecast_intraday
    fig3.add_trace(go.Scatter(
        x=plotted_forecast["time_local"], y=plotted_forecast["openmeteo_power_kw"],
        mode="lines", name="Open-Meteo weather forecast estimate (kW)",
        line=dict(width=2, color="#f2a541", dash="dot"),
        customdata=plotted_forecast["weighted_panel_irradiance"],
        hovertemplate="%{x|%H:%M}<br>Open-Meteo forecast estimate: %{y:.2f} kW"
                      "<br>80/20 weighted plane irradiance: %{customdata:.0f} W/m²<extra></extra>"
    ))
if meter_openmeteo_intraday.empty and openmeteo_intraday.empty and openmeteo_forecast_intraday.empty:
    st.info("No Sigen, Open-Meteo archive, or Open-Meteo forecast data is saved for this date.")
if openmeteo_chosen_date is not None:
    chart_day_start = pd.Timestamp(openmeteo_chosen_date).tz_localize(TZ_NAME)
    chart_day_end = chart_day_start + pd.DateOffset(days=1)
    fig3.update_xaxes(range=[chart_day_start, chart_day_end])
fig3.update_layout(xaxis_title="Time (Sydney)", yaxis_title="Power (kW)",
                   margin=dict(l=10, r=10, t=20, b=10), hovermode="closest")
st.plotly_chart(fig3, width="stretch")

st.subheader("Solcast vs Open-Meteo forecast power")
st.caption("Both forecasts are shown in kW at their available timestamps for the same Sydney date.")
solcast_forecast_dates = set(latest["local_date"])
openmeteo_forecast_date_set = set(openmeteo_forecast_hourly["local_date"]) if not openmeteo_forecast_hourly.empty else set()
forecast_comparison_dates = sorted(solcast_forecast_dates.intersection(openmeteo_forecast_date_set))
if forecast_comparison_dates:
    comparison_default = (
        openmeteo_chosen_date if openmeteo_chosen_date in forecast_comparison_dates
        else forecast_comparison_dates[-1]
    )
    comparison_date = st.selectbox(
        "Forecast comparison date", forecast_comparison_dates,
        index=forecast_comparison_dates.index(comparison_default),
        key="forecast_comparison_date",
    )
    solcast_comparison = latest[latest["local_date"] == comparison_date].sort_values("period_end_local")
    openmeteo_comparison = openmeteo_forecast_hourly[
        openmeteo_forecast_hourly["local_date"] == comparison_date
    ].sort_values("time_local")
    comparison_fig = go.Figure()
    comparison_fig.add_trace(go.Scatter(
        x=solcast_comparison["period_end_local"], y=solcast_comparison["pv_estimate"],
        mode="lines", name="Solcast forecast power (kW)",
        line=dict(width=2, color="#f2a541", dash="dot"),
        hovertemplate="%{x|%H:%M}<br>Solcast forecast: %{y:.2f} kW<extra></extra>",
    ))
    comparison_fig.add_trace(go.Scatter(
        x=openmeteo_comparison["time_local"], y=openmeteo_comparison["openmeteo_power_kw"],
        mode="lines+markers", name="Open-Meteo forecast power (kW)",
        line=dict(width=2, color="#4c78a8", dash="dot"), marker=dict(size=5),
        hovertemplate="%{x|%H:%M}<br>Open-Meteo forecast: %{y:.2f} kW<extra></extra>",
    ))
    comparison_fig.update_layout(
        xaxis_title="Time (Sydney)", yaxis_title="Forecast power (kW)",
        margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified",
    )
    comparison_fig.update_xaxes(
        range=[pd.Timestamp(comparison_date).tz_localize(TZ_NAME),
               pd.Timestamp(comparison_date).tz_localize(TZ_NAME) + pd.DateOffset(days=1)]
    )
    st.plotly_chart(comparison_fig, width="stretch")
else:
    st.info("No dates are available in both the latest Solcast forecast and Open-Meteo forecast.")

st.subheader("Forecast download history")
st.caption("Each saved download is retained as a separate snapshot. Daily totals below use the most recent snapshot.")
snapshots = history.groupby("downloaded_at_utc", as_index=False).agg(
    Downloaded_intervals=("period_end_utc", "count"),
    Forecast_start=("period_end_local", "min"),
    Forecast_end=("period_end_local", "max"),
).sort_values("downloaded_at_utc", ascending=False)
st.dataframe(snapshots.head(30), width="stretch", hide_index=True)

with st.expander("View daily totals and export data"):
    st.dataframe(daily, width="stretch", hide_index=True)
    st.download_button("Download latest forecast intervals (CSV)",
                       latest.to_csv(index=False).encode("utf-8"),
                       file_name=f"solcast_forecast_{today}.csv", mime="text/csv")
    st.download_button("Download all stored snapshots (CSV)",
                       history.to_csv(index=False).encode("utf-8"),
                       file_name="solcast_forecast_history.csv", mime="text/csv")
