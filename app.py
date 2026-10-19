import os
import sqlite3
import hashlib
import json
import subprocess
import sys
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from amber_data import fetch_current_prices as fetch_amber_current_prices
from amber_data import fetch_actuals_between as fetch_amber_actuals_between
from amber_data import fetch_prices as fetch_amber_prices
from sigen_data import STATION_ID as SIGEN_STATION_ID
from sigen_data import fetch_actuals as fetch_sigen_actuals
from solcast_data import (
    SITE_ID as SITE_ID_DEFAULT,
    TZ_NAME,
    check_authorization,
    fetch_estimated_actuals,
    fetch_forecast,
    parse_period_hours,
)

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
PV_SYSTEM_CAPACITY_KW = 9.5
OPENMETEO_DEFAULT_PERFORMANCE_RATIO = 0.80

st.set_page_config(page_title="Solar Forecast", page_icon="☀️", layout="wide")

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

def date_picker_with_next(label, value, key, min_value=None, max_value=None):
    """Render a calendar picker with a one-day forward button."""
    current_value = st.session_state.get(key, value)
    if isinstance(current_value, str):
        current_value = pd.Timestamp(current_value).date()
    if min_value is not None and current_value < min_value:
        current_value = min_value
    if max_value is not None and current_value > max_value:
        current_value = max_value
    if key not in st.session_state or st.session_state[key] != current_value:
        st.session_state[key] = current_value
    picker_col, next_col = st.columns([5, 1], vertical_alignment="bottom")
    with next_col:
        next_clicked = st.button(
            "Next date", key=f"{key}_next",
            disabled=max_value is not None and current_value >= max_value,
            width="stretch",
        )
    if next_clicked:
        current_value += timedelta(days=1)
        if max_value is not None:
            current_value = min(current_value, max_value)
        st.session_state[key] = current_value
    with picker_col:
        selected_value = st.date_input(
            label, value=None, min_value=min_value, max_value=max_value, key=key,
        )
    return selected_value

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
    rows_to_save = fetch_amber_actuals_between(api_key, start_utc, now_utc)
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

def solcast_comparison_forecast(history, latest_forecast, local_date):
    """Use the latest forecast where available and backfill gaps from saved snapshots."""
    latest_day = latest_forecast[latest_forecast["local_date"] == local_date].copy()
    latest_day = latest_day.dropna(subset=["pv_estimate"])
    latest_day["forecast_source"] = "Latest download"

    latest_stamp = latest_forecast["downloaded_at_utc"].max()
    previous = history[
        (history["local_date"] == local_date)
        & (history["downloaded_at_utc"] < latest_stamp)
    ].dropna(subset=["pv_estimate"]).copy()
    previous = (previous.sort_values("downloaded_at_utc", ascending=False)
                .drop_duplicates("period_end_utc", keep="first"))
    previous["forecast_source"] = "Earlier saved forecast"

    latest_times = set(latest_day["period_end_utc"])
    previous = previous[~previous["period_end_utc"].isin(latest_times)]
    return (pd.concat([latest_day, previous], ignore_index=True)
            .sort_values("period_end_local"))

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
        * PV_SYSTEM_CAPACITY_KW * OPENMETEO_DEFAULT_PERFORMANCE_RATIO
    )
    return weather.dropna(subset=["time_local", "openmeteo_power_kw"])

def calibrate_openmeteo_performance_ratio(archive_weather, meter_power):
    """Fit a fixed PV performance ratio from matching archive and meter samples."""
    if archive_weather.empty or meter_power.empty:
        return OPENMETEO_DEFAULT_PERFORMANCE_RATIO, 0, 0

    weather_irradiance = archive_weather.set_index("time_local")[
        ["weighted_panel_irradiance"]
    ].sort_index()
    meter_times = pd.DatetimeIndex(meter_power["period_end_local"])
    combined_times = weather_irradiance.index.union(meter_times).sort_values()
    aligned_irradiance = (
        weather_irradiance.reindex(combined_times)
        .interpolate(method="time", limit_area="inside")
        .reindex(meter_times)["weighted_panel_irradiance"]
    )
    base_power_kw = pd.to_numeric(aligned_irradiance, errors="coerce") * (
        PV_SYSTEM_CAPACITY_KW / 1000
    )
    actual_power_kw = pd.to_numeric(meter_power["Solar Production (kW)"], errors="coerce")
    calibration = pd.DataFrame({
        "time_local": meter_times,
        "base_power_kw": base_power_kw.to_numpy(),
        "actual_power_kw": actual_power_kw.to_numpy(),
    }).dropna()
    # Exclude night and very low-light samples, where inverter startup and meter
    # resolution can dominate the ratio.
    calibration = calibration[calibration["base_power_kw"] >= 0.2]
    day_count = calibration["time_local"].dt.date.nunique()
    if len(calibration) < 500 or day_count < 3:
        return OPENMETEO_DEFAULT_PERFORMANCE_RATIO, len(calibration), day_count

    base = calibration["base_power_kw"].to_numpy()
    actual = calibration["actual_power_kw"].to_numpy()
    fitted_ratio = float(np.dot(base, actual) / np.dot(base, base))
    # Guard against invalid telemetry or an incorrect system-capacity setting.
    fitted_ratio = float(np.clip(fitted_ratio, 0.30, 1.00))
    return fitted_ratio, len(calibration), day_count

def apply_openmeteo_performance_ratio(weather, performance_ratio):
    if not weather.empty:
        weather["openmeteo_power_kw"] = (
            weather["weighted_panel_irradiance"] / 1000
            * PV_SYSTEM_CAPACITY_KW * performance_ratio
        )
    return weather

def daily_summary(df):
    out = df.groupby("local_date", as_index=False).agg(
        Forecast_kWh=("energy_kwh", "sum"),
        Peak_kW=("pv_estimate", "max"),
        Intervals=("pv_estimate", "count"),
    )
    return out.sort_values("local_date")

def save_meter_actuals(energy_actuals, power_actuals, source_file=None):
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    energy_source = source_file or "Sigen Open Platform"
    power_source = source_file or "Sigen Open Platform"
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

st.title("☀️ Solar Forecast Dashboard")
st.caption("Rooftop PV forecast · Daily energy estimates in Australia/Sydney time")

with st.sidebar:
    st.header("Controls")
    st.caption(f"Local database · {DB_PATH.name}")
    api_key = os.getenv("SOLCAST_API_KEY", "").strip()
    key_fingerprint = hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest() if api_key.strip() else ""
    authorized = bool(key_fingerprint) and st.session_state.get("authorized_key_fingerprint") == key_fingerprint
    with st.expander("Solcast", expanded=True):
        st.caption(f"API key {'ready' if api_key else 'missing'} · Site {SITE_ID_DEFAULT}")
        check_clicked = st.button(
            "Check authorization", width="stretch", disabled=not bool(api_key),
            help="Makes a separate estimated-actuals request and may use API quota.",
        )
        if authorized:
            st.caption("Authorization passed")
        else:
            st.caption("Authorize before downloading a forecast.")
        fetch_clicked = st.button(
            "Download latest forecast", type="primary", width="stretch", disabled=not authorized,
        )

    with st.expander("Open-Meteo"):
        openmeteo_refresh_clicked = st.button(
            "Refresh archive and forecast", width="stretch",
            help="Refreshes archive data from 8 October through the latest available date and fetches a 7-day forecast.",
        )

    with st.expander("Sigen actuals"):
        st.caption(f"Station {SIGEN_STATION_ID}")
        sigen_start_date = date_picker_with_next(
            "Start date", value=pd.Timestamp("2026-10-01").date(), key="sigen_start_date",
        )
        sigen_end_date = date_picker_with_next(
            "End date", value=pd.Timestamp.now(tz=TZ_NAME).date(), key="sigen_end_date",
            min_value=sigen_start_date,
        )
        sigen_update_clicked = st.button(
            "Update power actuals", width="stretch",
            disabled=sigen_end_date < sigen_start_date,
            help="Reads five-minute Sigen power through the selected end date. Authorization is handled by the installed Sigen client.",
        )

    with st.expander("Amber prices"):
        amber_api_key = os.getenv("AMBER_API_KEY", "").strip()
        st.caption(f"API key {'ready' if amber_api_key else 'missing'}")
        amber_date = date_picker_with_next(
            "Price date", value=pd.Timestamp.now(tz=TZ_NAME).date(), key="amber_fetch_date",
        )
        amber_fetch_clicked = st.button(
            "Fetch date and forecast", width="stretch", disabled=not bool(amber_api_key),
            help="Fetches the selected date and requests up to 48 hours of current-price forecasts.",
        )
        amber_48_clicked = st.button(
            "Fetch next 48 intervals", width="stretch", disabled=not bool(amber_api_key),
        )
        amber_update_actuals_clicked = st.button(
            "Update actual prices through now", width="stretch",
            disabled=not bool(amber_api_key),
            help="Fetches actual intervals from the latest saved CurrentInterval through now.",
        )

if openmeteo_refresh_clicked:
    try:
        with st.spinner("Refreshing Open-Meteo archive and 7-day forecast…"):
            command = ([sys.executable, "--openmeteo"] if getattr(sys, "frozen", False)
                       else [sys.executable, str(APP_DIR / "openmeteo.py")])
            result = subprocess.run(
                command,
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
            sigen_energy, sigen_power = fetch_sigen_actuals(
                SIGEN_STATION_ID, sigen_start_date, sigen_end_date,
            )
            sigen_saved = len(sigen_power)
            if sigen_saved:
                save_meter_actuals(sigen_energy, sigen_power, source_file="Sigen Open Platform")
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
        st.error("Set the SOLCAST_API_KEY environment variable first, then restart the app.")
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
        st.error("Authorization checkpoint has not passed for SOLCAST_API_KEY. Check authorization first.")
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

energy_actuals, power_actuals = load_meter_actuals()
amber_prices = load_amber_prices()
st.subheader("Amber electricity price over time")
if amber_prices.empty:
    st.info("No Amber prices are saved yet. Set AMBER_API_KEY and choose **Fetch Amber prices** in the sidebar.")
else:
    amber_prices["local_date"] = amber_prices["start_time_local"].dt.date.astype(str)
    price_dates = sorted(pd.to_datetime(amber_prices["local_date"]).dt.date.unique())
    amber_default = amber_date if amber_date in price_dates else price_dates[-1]
    amber_date_col, amber_channel_col = st.columns([2, 3])
    with amber_date_col:
        amber_chosen_date = date_picker_with_next(
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
    st.info("No forecast data saved yet. Configure SOLCAST_API_KEY and choose **Download latest forecast**.")
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
openmeteo_performance_ratio, performance_ratio_samples, performance_ratio_days = (
    calibrate_openmeteo_performance_ratio(openmeteo_hourly, power_actuals)
)
openmeteo_hourly = apply_openmeteo_performance_ratio(
    openmeteo_hourly, openmeteo_performance_ratio,
)
openmeteo_forecast_hourly = apply_openmeteo_performance_ratio(
    openmeteo_forecast_hourly, openmeteo_performance_ratio,
)
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
chosen_date = date_picker_with_next(
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
        mode="lines", name="Solcast estimated actual power (kW)",
        line=dict(width=2, color="#f2a541"),
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
if performance_ratio_samples >= 500 and performance_ratio_days >= 3:
    performance_ratio_caption = (
        f"a calibrated {openmeteo_performance_ratio:.3f} ratio from "
        f"{performance_ratio_samples:,} daylight samples over {performance_ratio_days} days"
    )
else:
    performance_ratio_caption = (
        f"the default {OPENMETEO_DEFAULT_PERFORMANCE_RATIO:.2f} ratio "
        "(not enough overlapping history to calibrate)"
    )
st.caption(
    "Open-Meteo estimates use 22° tilted irradiance weighted for your array: "
    "80% north-facing and 20% east-facing, scaled to 9.5 kW with "
    f"{performance_ratio_caption}. Actual power comes from the Sigen meter. "
    "The archive estimate is interpolated to Sigen timestamps; the forecast keeps all 15-minute points for the selected day."
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
    openmeteo_chosen_date_value = date_picker_with_next(
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
st.caption("Both forecasts are shown in kW for the same Sydney date. Missing Solcast points are filled from the newest earlier saved forecast that has that timestamp.")
solcast_forecast_dates = set(history["local_date"])
openmeteo_forecast_date_set = set(openmeteo_forecast_hourly["local_date"]) if not openmeteo_forecast_hourly.empty else set()
forecast_comparison_dates = sorted(solcast_forecast_dates.intersection(openmeteo_forecast_date_set))
if forecast_comparison_dates:
    comparison_default = (
        openmeteo_chosen_date if openmeteo_chosen_date in forecast_comparison_dates
        else forecast_comparison_dates[-1]
    )
    comparison_date = date_picker_with_next(
        "Forecast comparison date", pd.Timestamp(comparison_default).date(),
        min_value=pd.Timestamp(forecast_comparison_dates[0]).date(),
        max_value=pd.Timestamp(forecast_comparison_dates[-1]).date(),
        key="forecast_comparison_date",
    )
    comparison_date = comparison_date.isoformat()
    solcast_comparison = solcast_comparison_forecast(history, latest, comparison_date)
    openmeteo_comparison = openmeteo_forecast_hourly[
        openmeteo_forecast_hourly["local_date"] == comparison_date
    ].sort_values("time_local")
    comparison_fig = go.Figure()
    comparison_fig.add_trace(go.Scatter(
        x=solcast_comparison["period_end_local"], y=solcast_comparison["pv_estimate"],
        customdata=solcast_comparison["forecast_source"],
        mode="lines", name="Solcast forecast power (kW)",
        line=dict(width=2, color="#f2a541", dash="dot"),
        hovertemplate="%{x|%H:%M}<br>Solcast forecast: %{y:.2f} kW<br>%{customdata}<extra></extra>",
    ))
    comparison_fig.add_trace(go.Scatter(
        x=openmeteo_comparison["time_local"], y=openmeteo_comparison["openmeteo_power_kw"],
        mode="lines", name="Open-Meteo forecast power (kW)",
        line=dict(width=2, color="#4c78a8", dash="dot"),
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
