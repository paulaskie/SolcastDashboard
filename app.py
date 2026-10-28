import os
import json
import functools
import subprocess
import sys
import math
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from database import (
    DB_OPERATIONAL_ERRORS,
    connect_database,
    read_sql_query,
    turso_configured,
    turso_enabled,
)

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
from sigen_data import developer_api_configured as sigen_developer_api_configured
from sigen_data import fetch_actuals as fetch_sigen_actuals
from solcast_data import (
    SITE_ID as SITE_ID_DEFAULT,
    TZ_NAME,
    fetch_estimated_actuals,
    fetch_forecast,
    parse_period_hours,
)

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
PV_SYSTEM_CAPACITY_KW = 9.5
SIGEN_BATTERY_CAPACITY_KWH = 24.0
SIGEN_INVERTER_LIMIT_KW = 10.0
SIGEN_SITE_EXPORT_LIMIT_KW = max(0.0, float(os.getenv("SIGEN_EXPORT_LIMIT_KW", "10")))
OPENMETEO_DEFAULT_PERFORMANCE_RATIO = 0.80
OPENMETEO_CALIBRATION_DATE = "2026-10-09"
OPENMETEO_CALIBRATION_MIN_SAMPLES = 10

st.set_page_config(page_title="Solar Forecast", page_icon="☀️", layout="wide")


def log_performance(function):
    @functools.wraps(function)
    def timed_function(*args, **kwargs):
        started_at = time.perf_counter()
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            print(
                "PERF operation=%s status=error error_type=%s duration_seconds=%.3f"
                % (
                    function.__name__,
                    type(error).__name__,
                    time.perf_counter() - started_at,
                ),
                flush=True,
            )
            raise
        print(
            "PERF operation=%s duration_seconds=%.3f"
            % (function.__name__, time.perf_counter() - started_at),
            flush=True,
        )
        return result

    return timed_function


@st.cache_resource
@log_performance
def _initialize_db(database_path, database_url):
    conn = connect_database(database_path)
    table_renames = (
        ("forecast_intervals", "solcast_forecast_30min"),
        ("actual_intervals", "solcast_estimate_30min"),
        ("openmeteo_hourly", "openmeteo_estimate_hourly"),
        ("openmeteo_daily", "openmeteo_estimate_daily"),
        ("meter_energy_actuals", "sigen_energy_actuals"),
        ("meter_power_actuals", "sigen_power_actuals"),
    )
    for old_table, new_table in table_renames:
        existing_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        if old_table in existing_tables and new_table not in existing_tables:
            conn.execute(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
        elif old_table in existing_tables and new_table in existing_tables:
            old_columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{old_table}")')}
            new_columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{new_table}")')}
            shared_columns = sorted(old_columns.intersection(new_columns))
            if shared_columns:
                columns_sql = ", ".join(f'"{column}"' for column in shared_columns)
                conn.execute(
                    f'INSERT OR IGNORE INTO "{new_table}" ({columns_sql}) '
                    f'SELECT {columns_sql} FROM "{old_table}"'
                )
            conn.execute(f'DROP TABLE "{old_table}"')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS solcast_forecast_30min (
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
        CREATE TABLE IF NOT EXISTS solcast_estimate_30min (
            downloaded_at_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            period TEXT,
            pv_estimate REAL,
            raw_json TEXT,
            PRIMARY KEY (downloaded_at_utc, period_end_utc)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sigen_energy_actuals (
            period_end_utc TEXT PRIMARY KEY,
            period_end_local TEXT NOT NULL,
            energy_kwh REAL NOT NULL,
            battery_soc REAL,
            source_file TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sigen_power_actuals (
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
    for table in ("sigen_energy_actuals", "sigen_power_actuals"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "raw_json" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{{}}'")
    energy_columns = {row[1] for row in conn.execute("PRAGMA table_info(sigen_energy_actuals)")}
    if "battery_soc" not in energy_columns:
        conn.execute("ALTER TABLE sigen_energy_actuals ADD COLUMN battery_soc REAL")
    for period_end_utc, raw_json in conn.execute(
        "SELECT period_end_utc, raw_json FROM sigen_energy_actuals WHERE battery_soc IS NULL"
    ).fetchall():
        battery_soc = extract_battery_soc(raw_json)
        if battery_soc is not None:
            try:
                conn.execute(
                    "UPDATE sigen_energy_actuals SET battery_soc = ? WHERE period_end_utc = ?",
                    (float(battery_soc), period_end_utc),
                )
            except (TypeError, ValueError):
                pass
    conn.commit()
    conn.close()


def connect_db():
    _initialize_db(str(DB_PATH), os.getenv("TURSO_DATABASE_URL", "").strip())
    return connect_database(DB_PATH)

def date_picker_with_arrows(label, value, key, min_value=None, max_value=None):
    """Render a calendar picker with previous-day and next-day arrows."""
    current_value = st.session_state.get(key)
    if current_value is None:
        current_value = value
    if isinstance(current_value, str):
        current_value = pd.Timestamp(current_value).date()
    if min_value is not None and current_value < min_value:
        current_value = min_value
    if max_value is not None and current_value > max_value:
        current_value = max_value
    if key not in st.session_state or st.session_state[key] != current_value:
        st.session_state[key] = current_value
    previous_col, picker_col, next_col = st.columns([1, 5, 1], vertical_alignment="bottom")
    with previous_col:
        previous_clicked = st.button(
            "←", key=f"{key}_previous",
            help="Previous day",
            disabled=min_value is not None and current_value <= min_value,
            width="stretch",
        )
    with next_col:
        next_clicked = st.button(
            "→", key=f"{key}_next",
            help="Next day",
            disabled=max_value is not None and current_value >= max_value,
            width="stretch",
        )
    if previous_clicked:
        current_value -= timedelta(days=1)
        if min_value is not None:
            current_value = max(current_value, min_value)
        st.session_state[key] = current_value
    elif next_clicked:
        current_value += timedelta(days=1)
        if max_value is not None:
            current_value = min(current_value, max_value)
        st.session_state[key] = current_value
    with picker_col:
        selected_value = st.date_input(
            label, value=None, min_value=min_value, max_value=max_value, key=key,
        )
    return selected_value

def half_width_date_picker_with_arrows(label, value, key, min_value=None, max_value=None):
    """Render a chart date picker in the left half of the available row."""
    date_col, _ = st.columns(2)
    with date_col:
        return date_picker_with_arrows(label, value, key, min_value, max_value)

def place_legend_below_chart(fig):
    """Place Plotly legends below the plot area so they fit narrow screens."""
    fig.update_layout(
        legend=dict(orientation="h", x=0, y=-0.25, xanchor="left", yanchor="top"),
        margin=dict(b=90),
    )

def merge_time_windows(windows):
    """Merge overlapping windows so export capacity is never counted twice."""
    merged = []
    for start, end in sorted((start, end) for start, end in windows if start < end):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

def overlapping_time_segments(interval_start, interval_end, windows):
    """Return the portions of a forecast interval covered by scenario windows."""
    return [
        (max(interval_start, start), min(interval_end, end))
        for start, end in windows
        if max(interval_start, start) < min(interval_end, end)
    ]

def resample_meter_power_to_30min(meter_power, local_date):
    """Return Sigen power averaged into 30-minute bins for one local date."""
    if meter_power.empty:
        return pd.DataFrame(columns=["time_local", "Solar Production (kW)"])
    day_start = pd.Timestamp(local_date).tz_localize(TZ_NAME)
    day_end = day_start + pd.DateOffset(days=1)
    half_hour_times = pd.date_range(
        start=day_start + pd.Timedelta(minutes=30), end=day_end, freq="30min",
    )
    meter_samples = meter_power[["period_end_local", "Solar Production (kW)"]].copy()
    meter_samples["period_end_local"] = pd.to_datetime(meter_samples["period_end_local"])
    meter_bins = (
        meter_samples.set_index("period_end_local")["Solar Production (kW)"]
        .sort_index()
        .resample("30min", origin=day_start, closed="right", label="right")
        .agg(["mean", "count"])
    )
    resampled_meter = meter_bins["mean"].where(meter_bins["count"] == 6)
    return pd.DataFrame({
        "time_local": half_hour_times,
        "Solar Production (kW)": resampled_meter.reindex(half_hour_times).to_numpy(),
    })

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

def refresh_amber_actuals_since_last_current(api_key, include_rate_limit=False):
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
        return (0, None) if include_rate_limit else 0
    result = fetch_amber_actuals_between(
        api_key, start_utc, now_utc, include_rate_limit=include_rate_limit,
    )
    if include_rate_limit:
        rows_to_save, rate_limit_remaining = result
    else:
        rows_to_save = result
    if rows_to_save:
        save_amber_prices(rows_to_save)
    return (len(rows_to_save), rate_limit_remaining) if include_rate_limit else len(rows_to_save)

def amber_rate_limit_message(**remaining_by_request):
    values = [
        f"{name}: {remaining if remaining is not None else 'not provided'}"
        for name, remaining in remaining_by_request.items()
    ]
    return "Amber RateLimit-Remaining · " + " · ".join(values)

def solcast_rate_limit_message(**remaining_by_request):
    values = [
        f"{name}: {remaining if remaining is not None else 'not provided'}"
        for name, remaining in remaining_by_request.items()
    ]
    return "Solcast X-RateLimit-Remaining · " + " · ".join(values)

def sigen_quota_message(quota):
    if not isinstance(quota, dict):
        return "Sigen quota remaining: not provided by the client."
    daily = quota.get("daily", {}).get("remaining")
    minute = quota.get("minute", {}).get("remaining")
    burst = quota.get("burst_10_seconds", {}).get("remaining")
    unit = quota.get("unit") or "resource units"
    return (
        f"Sigen quota remaining ({unit}) · daily: {daily if daily is not None else '—'}"
        f" · minute: {minute if minute is not None else '—'}"
        f" · 10-second burst: {burst if burst is not None else '—'}"
    )

def show_amber_rate_limit_from_error(error):
    remaining = getattr(error, "amber_rate_limit_remaining", None)
    if remaining is not None:
        st.caption(amber_rate_limit_message(response=remaining))

@log_performance
def load_amber_prices():
    conn = connect_db()
    actuals = read_sql_query(
        "SELECT * FROM amber_prices WHERE lower(price_type) <> 'forecastinterval'", conn,
    )
    forecasts = read_sql_query(
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
    latest_snapshot = conn.execute(
        "SELECT MAX(downloaded_at_utc) FROM solcast_forecast_30min"
    ).fetchone()[0]
    if latest_snapshot:
        previous = read_sql_query(
            "SELECT period_end_utc, period, pv_estimate, pv_estimate10, pv_estimate90 "
            "FROM solcast_forecast_30min WHERE downloaded_at_utc = ? ORDER BY period_end_utc",
            conn, params=(latest_snapshot,),
        )
        incoming = df.copy()
        incoming["period_end_utc"] = pd.to_datetime(
            incoming["period_end_utc"], utc=True, errors="coerce",
        )
        previous["period_end_utc"] = pd.to_datetime(
            previous["period_end_utc"], utc=True, errors="coerce",
        )
        incoming = incoming.sort_values("period_end_utc").reset_index(drop=True)
        previous = previous.sort_values("period_end_utc").reset_index(drop=True)
        same_forecast = len(incoming) == len(previous)
        if same_forecast:
            same_forecast = incoming["period_end_utc"].equals(previous["period_end_utc"])
        if same_forecast:
            same_forecast = incoming["period"].fillna("PT30M").astype(str).equals(
                previous["period"].fillna("PT30M").astype(str)
            )
        for column in ("pv_estimate", "pv_estimate10", "pv_estimate90"):
            if not same_forecast:
                break
            incoming_values = pd.to_numeric(incoming[column], errors="coerce").to_numpy(dtype=float)
            previous_values = pd.to_numeric(previous[column], errors="coerce").to_numpy(dtype=float)
            same_forecast = np.allclose(
                incoming_values, previous_values, rtol=1e-9, atol=1e-9, equal_nan=True,
            )
        if same_forecast:
            conn.close()
            return downloaded_at, False
    with conn:
        for _, row in df.iterrows():
            raw = row.to_json(date_format="iso")
            conn.execute("""
                INSERT OR REPLACE INTO solcast_forecast_30min
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
    return downloaded_at, True

def save_actuals(df, downloaded_at):
    if df.empty:
        return
    conn = connect_db()
    with conn:
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO solcast_estimate_30min
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

@log_performance
def load_latest_actuals():
    conn = connect_db()
    actuals = read_sql_query("SELECT * FROM solcast_estimate_30min ORDER BY downloaded_at_utc, period_end_utc", conn)
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

@log_performance
def load_history():
    conn = connect_db()
    hist = read_sql_query("SELECT * FROM solcast_forecast_30min ORDER BY downloaded_at_utc, period_end_utc", conn)
    conn.close()
    if hist.empty:
        for column in ("period_end_local", "period_hours", "energy_kwh", "local_date"):
            if column not in hist:
                hist[column] = pd.Series(dtype="object")
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

@log_performance
def load_openmeteo_hourly():
    conn = connect_db()
    try:
        weather = read_sql_query(
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM openmeteo_estimate_hourly "
            "WHERE location_key = (SELECT location_key FROM openmeteo_estimate_hourly "
            "ORDER BY fetched_at_utc DESC LIMIT 1) ORDER BY time_utc",
            conn,
        )
    except DB_OPERATIONAL_ERRORS:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

@log_performance
def load_openmeteo_forecast_hourly():
    conn = connect_db()
    try:
        weather = read_sql_query(
            "WITH ranked_forecasts AS ("
            "SELECT time_utc, time_local, global_tilted_irradiance, "
            "global_tilted_irradiance_east, fetched_at_utc, "
            "ROW_NUMBER() OVER (PARTITION BY time_utc "
            "ORDER BY fetched_at_utc DESC, location_key DESC) AS row_num "
            "FROM openmeteo_forecast_hourly) "
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM ranked_forecasts WHERE row_num = 1 ORDER BY time_utc",
            conn,
        )
    except DB_OPERATIONAL_ERRORS:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

@log_performance
def load_openmeteo_forecast_snapshots():
    conn = connect_db()
    try:
        weather = read_sql_query(
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east FROM openmeteo_forecast_snapshots "
            "ORDER BY fetched_at_utc, time_utc",
            conn,
        )
    except DB_OPERATIONAL_ERRORS:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

@log_performance
def load_openmeteo_historical_forecast_hourly():
    conn = connect_db()
    try:
        weather = read_sql_query(
            "WITH ranked_forecasts AS ("
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east, "
            "ROW_NUMBER() OVER (PARTITION BY time_utc "
            "ORDER BY fetched_at_utc DESC, location_key DESC) AS row_num "
            "FROM openmeteo_historical_forecast_hourly) "
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM ranked_forecasts WHERE row_num = 1 ORDER BY time_utc",
            conn,
        )
    except DB_OPERATIONAL_ERRORS:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

@log_performance
def load_openmeteo_daily():
    conn = connect_db()
    daily_frames = []
    for table in ("openmeteo_estimate_daily", "openmeteo_forecast_daily"):
        try:
            weather_daily = read_sql_query(
                f"SELECT date_local, sunrise_local, sunset_local, daylight_duration FROM {table} "
                f"WHERE location_key = (SELECT location_key FROM {table} "
                "ORDER BY fetched_at_utc DESC LIMIT 1)",
                conn,
            )
        except DB_OPERATIONAL_ERRORS:
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
    # Four east panels and sixteen north panels: combine the two GTI series by panel count.
    weather["weighted_panel_irradiance"] = (
        weather["global_tilted_irradiance"] * (16 / 20)
        + weather["global_tilted_irradiance_east"] * (4 / 20)
    )
    # Panel-weighted GTI is W/m²; scale by array capacity and system performance ratio.
    weather["openmeteo_power_kw"] = (
        weather["weighted_panel_irradiance"] / 1000
        * PV_SYSTEM_CAPACITY_KW * OPENMETEO_DEFAULT_PERFORMANCE_RATIO
    )
    return weather.dropna(subset=["time_local", "openmeteo_power_kw"])

@log_performance
def calibrate_openmeteo_performance_ratio(archive_weather, meter_power):
    """Fit a fixed PV performance ratio from matching 30-minute means."""
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
    calibration_samples = pd.DataFrame({
        "base_power_kw": pd.to_numeric(aligned_irradiance, errors="coerce").to_numpy()
        * (PV_SYSTEM_CAPACITY_KW / 1000),
        "actual_power_kw": pd.to_numeric(
            meter_power["Solar Production (kW)"], errors="coerce",
        ).to_numpy(),
    }, index=meter_times).dropna()
    day_start = pd.Timestamp(OPENMETEO_CALIBRATION_DATE).tz_localize(TZ_NAME)
    calibration_bins = (
        calibration_samples.resample(
            "30min", origin=day_start, closed="right", label="right",
        )
        .agg(["mean", "count"])
    )
    calibration = calibration_bins[
        (calibration_bins[("base_power_kw", "count")] == 6)
        & (calibration_bins[("actual_power_kw", "count")] == 6)
    ]
    calibration = calibration[
        (calibration.index.date == day_start.date())
        & (calibration[("base_power_kw", "mean")] >= 0.2)
    ]
    calibration = calibration[
        calibration[("actual_power_kw", "mean")].notna()
    ]
    day_count = calibration.index.normalize().nunique()
    if len(calibration) < OPENMETEO_CALIBRATION_MIN_SAMPLES or day_count != 1:
        return OPENMETEO_DEFAULT_PERFORMANCE_RATIO, len(calibration), day_count

    base = calibration[("base_power_kw", "mean")].to_numpy()
    actual = calibration[("actual_power_kw", "mean")].to_numpy()
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

def intervals_cover_full_local_day(period_end_utc, period_hours, local_date):
    """Return true only when intervals continuously cover the whole local day."""
    day_start = pd.Timestamp(local_date).tz_localize(TZ_NAME).tz_convert("UTC")
    day_end = (
        (pd.Timestamp(local_date) + pd.DateOffset(days=1))
        .tz_localize(TZ_NAME).tz_convert("UTC")
    )
    ends = pd.to_datetime(pd.Series(period_end_utc).reset_index(drop=True), utc=True, errors="coerce")
    hours = pd.to_numeric(pd.Series(period_hours).reset_index(drop=True), errors="coerce")
    intervals = pd.DataFrame({
        "start": ends - pd.to_timedelta(hours, unit="h"),
        "end": ends,
    }).dropna().sort_values("start")
    cursor = day_start
    tolerance = pd.Timedelta(seconds=1)
    for row in intervals.itertuples(index=False):
        if row.end <= cursor:
            continue
        if row.start > cursor + tolerance:
            return False
        cursor = max(cursor, row.end)
        if cursor >= day_end - tolerance:
            return True
    return False

def daily_forecast_summary(solcast_forecast, openmeteo_historical, openmeteo_forecast, current_date):
    """Use Solcast for today and tomorrow; fill later incomplete days from Open-Meteo."""
    tomorrow_date = (pd.Timestamp(current_date) + pd.DateOffset(days=1)).date().isoformat()
    dates = set(solcast_forecast.get("local_date", []))
    dates.update(openmeteo_historical.get("local_date", []))
    dates.update(openmeteo_forecast.get("local_date", []))
    rows = []

    for local_date in sorted(dates):
        solcast_day = solcast_forecast[
            solcast_forecast["local_date"] == local_date
        ].copy()
        solcast_is_full_day = (
            not solcast_day.empty and intervals_cover_full_local_day(
                solcast_day["period_end_utc"], solcast_day["period_hours"], local_date,
            )
        )
        if solcast_is_full_day or (
            current_date <= local_date <= tomorrow_date and not solcast_day.empty
        ):
            rows.append({
                "local_date": local_date,
                "Forecast_kWh": pd.to_numeric(solcast_day["energy_kwh"], errors="coerce").sum(),
                "Peak_kW": pd.to_numeric(solcast_day["pv_estimate"], errors="coerce").max(),
                "Intervals": int(solcast_day["pv_estimate"].count()),
                "Forecast_source": "Solcast",
            })
            continue

        openmeteo_start_date = (
            pd.Timestamp(current_date) + pd.DateOffset(days=2)
        ).date().isoformat()
        if local_date < openmeteo_start_date:
            continue

        openmeteo_candidates = []
        for source_name, source_frame in (
            ("Open-Meteo historical forecast", openmeteo_historical),
            ("Open-Meteo forecast", openmeteo_forecast),
        ):
            source_day = source_frame[
                source_frame["local_date"] == local_date
            ].copy()
            if source_day.empty:
                continue
            times = pd.to_datetime(source_day["time_utc"], utc=True, errors="coerce")
            sample_step_hours = times.sort_values().diff().dt.total_seconds().median() / 3600
            if pd.isna(sample_step_hours) or sample_step_hours <= 0:
                sample_step_hours = 0.25
            complete = intervals_cover_full_local_day(
                times + pd.to_timedelta(sample_step_hours, unit="h"),
                [sample_step_hours] * len(source_day), local_date,
            )
            openmeteo_candidates.append((complete, source_name, source_day, sample_step_hours))

        if openmeteo_candidates:
            complete_candidates = [candidate for candidate in openmeteo_candidates if candidate[0]]
            _, source_name, source_day, sample_step_hours = (
                complete_candidates[0] if complete_candidates else openmeteo_candidates[0]
            )
            power = pd.to_numeric(source_day["openmeteo_power_kw"], errors="coerce")
            rows.append({
                "local_date": local_date,
                "Forecast_kWh": power.sum() * sample_step_hours,
                "Peak_kW": power.max(),
                "Intervals": int(power.count()),
                "Forecast_source": source_name,
            })

    return pd.DataFrame(rows, columns=[
        "local_date", "Forecast_kWh", "Peak_kW", "Intervals", "Forecast_source",
    ]).sort_values("local_date")

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
            battery_soc = extract_battery_soc(raw_json)
            try:
                battery_soc = float(battery_soc) if battery_soc is not None else None
            except (TypeError, ValueError):
                battery_soc = None
            conn.execute("""
                INSERT INTO sigen_energy_actuals
                    (period_end_utc, period_end_local, energy_kwh, battery_soc, source_file, imported_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_end_utc) DO UPDATE SET
                    period_end_local=excluded.period_end_local,
                    energy_kwh=excluded.energy_kwh,
                    battery_soc=COALESCE(excluded.battery_soc, sigen_energy_actuals.battery_soc),
                    source_file=excluded.source_file,
                    imported_at_utc=excluded.imported_at_utc,
                    raw_json=excluded.raw_json
                WHERE period_end_local IS NOT excluded.period_end_local
                   OR energy_kwh IS NOT excluded.energy_kwh
                   OR (excluded.battery_soc IS NOT NULL AND battery_soc IS NOT excluded.battery_soc)
                   OR source_file IS NOT excluded.source_file
                   OR raw_json IS NOT excluded.raw_json
            """, (
                period_end.tz_convert("UTC").isoformat(), period_end.isoformat(),
                float(row["Total Solar Production Energy (kWh)"]), battery_soc,
                energy_source, imported_at, raw_json,
            ))
        for _, row in power_actuals.iterrows():
            period_end = pd.Timestamp(row["period_end_local"])
            raw_json = row.get("Raw Sigen JSON")
            if not isinstance(raw_json, str) or not raw_json.strip():
                raw_json = row.to_json(date_format="iso")
            conn.execute("""
                INSERT INTO sigen_power_actuals
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

@log_performance
def load_meter_actuals():
    conn = connect_db()
    energy = read_sql_query(
        "SELECT period_end_utc, energy_kwh, battery_soc, raw_json "
        "FROM sigen_energy_actuals ORDER BY period_end_utc", conn,
    )
    power = read_sql_query("SELECT period_end_utc, power_kw FROM sigen_power_actuals ORDER BY period_end_utc", conn)
    conn.close()
    if not energy.empty:
        energy["period_end_utc"] = pd.to_datetime(energy["period_end_utc"], utc=True)
        energy["period_end_local"] = energy["period_end_utc"].dt.tz_convert(TZ_NAME)
        energy["local_date"] = energy["period_end_local"].dt.date.astype(str)
        energy["Total Solar Production Energy (kWh)"] = energy["energy_kwh"]
        energy["Battery state of charge (%)"] = energy["battery_soc"]
        energy["Battery state of charge (%)"] = pd.to_numeric(
            energy["Battery state of charge (%)"], errors="coerce",
        )
        energy["load_power_kw"] = energy["raw_json"].map(
            lambda raw: extract_sigen_value(raw, "loadPower")
        )
        energy["load_power_kw"] = pd.to_numeric(energy["load_power_kw"], errors="coerce")
        energy = energy.drop(columns=["battery_soc", "raw_json"])
    if not power.empty:
        power["period_end_utc"] = pd.to_datetime(power["period_end_utc"], utc=True)
        power["period_end_local"] = power["period_end_utc"].dt.tz_convert(TZ_NAME)
        power["local_date"] = power["period_end_local"].dt.date.astype(str)
        power["Solar Production (kW)"] = power["power_kw"]
    return energy, power

def extract_sigen_value(raw_json, field_name):
    """Read a numeric field from a stored Sigen energy JSON row."""
    try:
        payload = json.loads(raw_json) if isinstance(raw_json, str) else {}
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    normalized_field = "".join(character for character in field_name.lower() if character.isalnum())
    field_aliases = {
        "batsoc": {"batsoc", "batterysoc", "stateofcharge", "batterystateofcharge"},
    }
    allowed_fields = field_aliases.get(normalized_field, {normalized_field})
    for key, value in payload.items():
        normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
        if normalized_key in allowed_fields:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None

def extract_battery_soc(raw_json):
    """Read battery state of charge from a stored Sigen energy JSON row."""
    return extract_sigen_value(raw_json, "batSoc")

st.title("☀️ Solar Forecast Dashboard")
st.caption("Rooftop PV forecast · Daily energy estimates in Australia/Sydney time")

upload_local_turso_clicked = False
with st.sidebar:
    st.header("Controls")
    database_label = (
        "Turso Cloud" if turso_enabled()
        else f"Local database · {DB_PATH.name}"
    )
    st.caption(database_label)
    api_key = os.getenv("SOLCAST_API_KEY", "").strip()
    with st.expander("Solcast", expanded=True):
        st.caption(f"API key {'ready' if api_key else 'missing'} · Site {SITE_ID_DEFAULT}")
        fetch_clicked = st.button(
            "Download latest forecast", type="primary", width="stretch", disabled=not bool(api_key),
        )

    with st.expander("Open-Meteo"):
        openmeteo_latest_archive_date = (
            pd.Timestamp.now(tz=TZ_NAME).date() - timedelta(days=1)
        )
        openmeteo_default_start_date = openmeteo_latest_archive_date - timedelta(days=12)
        with st.form("openmeteo_refresh_form"):
            openmeteo_start_date = st.date_input(
                "Archive start date", value=openmeteo_default_start_date,
                key="openmeteo_archive_start_date", max_value=openmeteo_latest_archive_date,
            )
            openmeteo_end_date = st.date_input(
                "Archive end date", value=openmeteo_latest_archive_date,
                key="openmeteo_archive_end_date", max_value=openmeteo_latest_archive_date,
            )
            openmeteo_refresh_clicked = st.form_submit_button(
                "Refresh archive and forecast", width="stretch",
                help="Refreshes the selected satellite archive and historical forecast dates, then fetches a 7-day weather forecast.",
            )

    with st.expander("Sigen actuals"):
        with st.form("sigen_update_form"):
            if sigen_developer_api_configured():
                sigen_start_date = st.date_input(
                    "History date", value=pd.Timestamp.now(tz=TZ_NAME).date(),
                    key="sigen_api_history_date",
                )
                sigen_end_date = sigen_start_date
                st.caption(
                    "Using the Sigen Developer API. It limits history access to one request per station "
                    "every five minutes, so update one day at a time."
                )
            else:
                st.caption(f"Station {SIGEN_STATION_ID}")
                sigen_start_date = st.date_input(
                    "Start date", value=pd.Timestamp("2026-10-01").date(), key="sigen_start_date",
                )
                sigen_end_date = st.date_input(
                    "End date", value=pd.Timestamp.now(tz=TZ_NAME).date(), key="sigen_end_date",
                )
            sigen_update_clicked = st.form_submit_button(
                "Update power actuals", width="stretch",
                help=(
                    "Reads the selected day's five-minute Sigen power history from the Developer API."
                    if sigen_developer_api_configured() else
                    "Reads five-minute Sigen power through the selected end date using the installed Sigen client."
                ),
            )

    with st.expander("Amber prices"):
        amber_api_key = os.getenv("AMBER_API_KEY", "").strip()
        st.caption(f"API key {'ready' if amber_api_key else 'missing'}")
        with st.form("amber_fetch_form"):
            amber_date = st.date_input(
                "Price date", value=pd.Timestamp.now(tz=TZ_NAME).date(), key="amber_fetch_date",
            )
            amber_fetch_clicked = st.form_submit_button(
                "Fetch date and forecast", width="stretch", disabled=not bool(amber_api_key),
                help="Fetches the selected date and requests up to 48 hours of current-price forecasts.",
            )

    if turso_configured() and not turso_enabled():
        with st.expander("Database sync"):
            upload_local_turso_clicked = st.button(
                "Upload local database to Turso",
                width="stretch",
            )

if upload_local_turso_clicked:
    try:
        from migrate_to_turso import upload_to_turso

        upload_progress = st.progress(0, text="Preparing local database upload…")

        def report_upload_progress(table, rows, table_total, table_index, table_count):
            fraction = (table_index - 1 + rows / max(table_total, 1)) / table_count
            upload_progress.progress(
                min(fraction, 1.0),
                text=f"Uploading {table}: {rows:,} of {table_total:,} rows",
            )

        with st.spinner("Uploading local database to Turso…"):
            uploaded_rows = upload_to_turso(DB_PATH, report_upload_progress)
        upload_progress.progress(1.0, text="Upload complete")
        uploaded_count = sum(uploaded_rows.values())
        if uploaded_count:
            st.success(
                f"Uploaded {uploaded_count:,} new or changed rows across "
                f"{len(uploaded_rows)} tables to Turso."
            )
        else:
            st.success("Turso is up to date; no new or changed rows needed uploading.")
    except Exception as error:
        st.error(f"Could not upload local database to Turso: {error}")

if openmeteo_refresh_clicked and openmeteo_end_date >= openmeteo_start_date:
    try:
        with st.spinner(
            f"Refreshing Open-Meteo archive from {openmeteo_start_date} to "
            f"{openmeteo_end_date} and the 7-day forecast…"
        ):
            command = ([sys.executable, "--openmeteo"] if getattr(sys, "frozen", False)
                       else [sys.executable, str(APP_DIR / "openmeteo.py")])
            command.extend([
                "--start-date", openmeteo_start_date.isoformat(),
                "--end-date", openmeteo_end_date.isoformat(),
            ])
            result = subprocess.run(
                command,
                cwd=APP_DIR, capture_output=True, text=True, timeout=300,
            )
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip() or "Open-Meteo refresh failed."
            st.error(details)
        else:
            st.session_state["openmeteo_refresh_message"] = (
                f"Open-Meteo archive and historical forecasts {openmeteo_start_date} to {openmeteo_end_date} refreshed. "
                + (result.stdout.strip().splitlines()[-1]
                   if result.stdout.strip() else "Open-Meteo data refreshed.")
            )
            st.rerun()
    except Exception as e:
        st.error(f"Could not refresh Open-Meteo data: {e}")
elif openmeteo_refresh_clicked:
    st.error("Archive end date must be on or after the start date.")

if st.session_state.get("openmeteo_refresh_message"):
    st.success(st.session_state.pop("openmeteo_refresh_message"))

if amber_fetch_clicked:
    try:
        with st.spinner("Fetching prices from Amber…"):
            amber_rows, prices_remaining = fetch_amber_prices(
                amber_api_key, amber_date.isoformat(), include_rate_limit=True,
            )
            amber_current_rows, current_remaining = fetch_amber_current_prices(
                amber_api_key, next_intervals=576, include_rate_limit=True,
            )
            save_amber_prices(amber_rows)
            save_amber_prices(amber_current_rows)
        forecast_count = sum(1 for row in amber_current_rows if row.get("type") == "ForecastInterval")
        st.session_state["amber_download_message"] = (
            f"Saved {len(amber_rows)} price intervals for {amber_date.isoformat()} and "
            f"{forecast_count} five-minute forecast intervals from the current-prices feed."
        )
        st.session_state["amber_rate_limit_message"] = amber_rate_limit_message(
            prices=prices_remaining, current_prices=current_remaining,
        )
        st.rerun()
    except requests.RequestException as e:
        show_amber_rate_limit_from_error(e)
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if st.session_state.get("amber_download_message"):
    st.success(st.session_state.pop("amber_download_message"))
if st.session_state.get("amber_rate_limit_message"):
    st.caption(st.session_state.pop("amber_rate_limit_message"))

if sigen_update_clicked and sigen_end_date >= sigen_start_date:
    try:
        with st.spinner("Updating meter actuals from Sigen…"):
            sigen_energy, sigen_power, sigen_quota = fetch_sigen_actuals(
                SIGEN_STATION_ID, sigen_start_date, sigen_end_date, include_quota=True,
            )
            sigen_saved = len(sigen_power)
            if sigen_saved:
                save_meter_actuals(sigen_energy, sigen_power, source_file="Sigen Open Platform")
        st.session_state["sigen_update_message"] = (
            f"Updated {sigen_saved:,} five-minute power and energy actuals through "
            f"{sigen_end_date.isoformat()}."
        )
        st.session_state["sigen_quota_message"] = sigen_quota_message(sigen_quota)
        st.rerun()
    except Exception as e:
        st.error(f"Could not update actuals from Sigen: {e}")
elif sigen_update_clicked:
    st.error("Sigen end date must be on or after the start date.")

if st.session_state.get("sigen_update_message"):
    st.success(st.session_state.pop("sigen_update_message"))
if st.session_state.get("sigen_quota_message"):
    st.caption(st.session_state.pop("sigen_quota_message"))

if fetch_clicked:
    try:
        with st.spinner("Downloading forecast from Solcast…"):
            fresh, payload, forecast_remaining = fetch_forecast(
                api_key, include_rate_limit=True,
            )
            stamp, forecast_saved = save_forecast(fresh, payload)
        actuals = pd.DataFrame()
        actuals_error = None
        actuals_remaining = None
        try:
            actuals, _, actuals_remaining = fetch_estimated_actuals(
                api_key, include_rate_limit=True,
            )
            save_actuals(actuals, stamp)
        except Exception as e:
            actuals_error = str(e)
        forecast_status = (
            f"Saved {len(fresh)} forecast intervals"
            if forecast_saved else "Forecast unchanged; skipped a duplicate forecast revision"
        )
        st.session_state["last_download_message"] = (
            f"{forecast_status} and saved {len(actuals)} estimated actual intervals at {stamp} UTC."
        )
        st.session_state["solcast_rate_limit_message"] = solcast_rate_limit_message(
            forecast=forecast_remaining, estimated_actuals=actuals_remaining,
        )
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
if st.session_state.get("solcast_rate_limit_message"):
    st.caption(st.session_state.pop("solcast_rate_limit_message"))
if st.session_state.get("actuals_download_error"):
    st.warning(f"Estimated actuals could not be refreshed; showing the most recently saved actuals, if available. {st.session_state['actuals_download_error']}")

energy_actuals, power_actuals = load_meter_actuals()
history = load_history()
if history.empty:
    latest = history.copy()
else:
    latest_stamp = history["downloaded_at_utc"].max()
    latest = history[history["downloaded_at_utc"] == latest_stamp].copy()
    latest = latest.sort_values("period_end_local")
actuals = load_latest_actuals()
openmeteo_hourly = load_openmeteo_hourly()
openmeteo_forecast_hourly = load_openmeteo_forecast_hourly()
openmeteo_forecast_snapshots = load_openmeteo_forecast_snapshots()
openmeteo_historical_forecast_hourly = load_openmeteo_historical_forecast_hourly()
openmeteo_performance_ratio, performance_ratio_samples, performance_ratio_days = (
    calibrate_openmeteo_performance_ratio(openmeteo_hourly, power_actuals)
)
openmeteo_hourly = apply_openmeteo_performance_ratio(
    openmeteo_hourly, openmeteo_performance_ratio,
)
openmeteo_forecast_hourly = apply_openmeteo_performance_ratio(
    openmeteo_forecast_hourly, openmeteo_performance_ratio,
)
openmeteo_forecast_snapshots = apply_openmeteo_performance_ratio(
    openmeteo_forecast_snapshots, openmeteo_performance_ratio,
)
openmeteo_historical_forecast_hourly = apply_openmeteo_performance_ratio(
    openmeteo_historical_forecast_hourly, openmeteo_performance_ratio,
)
openmeteo_daily = load_openmeteo_daily()
amber_prices = load_amber_prices()


@st.fragment
@log_performance
def _render_amber_and_battery_panels():
    scenario_default_date = pd.Timestamp.now(tz=TZ_NAME).date()
    scenario_start_date = st.session_state.get(
        "battery_soc_scenario_start_date_v2", scenario_default_date,
    )
    scenario_start_time = st.session_state.get(
        "battery_soc_scenario_start_time_v2", dt_time(18, 0),
    )
    scenario_end_date = st.session_state.get(
        "battery_soc_scenario_end_date_v2", scenario_default_date,
    )
    scenario_end_time = st.session_state.get(
        "battery_soc_scenario_end_time_v2", dt_time(19, 0),
    )
    scenario_second_enabled = st.session_state.get(
        "battery_soc_scenario_second_enabled", False,
    )
    scenario_second_start_date = st.session_state.get(
        "battery_soc_scenario_second_start_date", scenario_default_date,
    )
    scenario_second_start_time = st.session_state.get(
        "battery_soc_scenario_second_start_time", dt_time(20, 0),
    )
    scenario_second_end_date = st.session_state.get(
        "battery_soc_scenario_second_end_date", scenario_default_date,
    )
    scenario_second_end_time = st.session_state.get(
        "battery_soc_scenario_second_end_time", dt_time(21, 0),
    )
    scenario_start = pd.Timestamp(datetime.combine(
        scenario_start_date, scenario_start_time,
    )).tz_localize(TZ_NAME)
    scenario_end = pd.Timestamp(datetime.combine(
        scenario_end_date, scenario_end_time,
    )).tz_localize(TZ_NAME)
    scenario_periods = [(scenario_start, scenario_end)]
    if scenario_second_enabled:
        scenario_periods.append((
            pd.Timestamp(datetime.combine(
                scenario_second_start_date, scenario_second_start_time,
            )).tz_localize(TZ_NAME),
            pd.Timestamp(datetime.combine(
                scenario_second_end_date, scenario_second_end_time,
            )).tz_localize(TZ_NAME),
        ))
    scenario_periods = merge_time_windows(scenario_periods)
    st.subheader("Amber electricity price over time")
    if amber_prices.empty:
        st.info("No Amber prices are saved yet. Set AMBER_API_KEY and choose **Fetch Amber prices** in the sidebar.")
    else:
        amber_prices["local_date"] = amber_prices["start_time_local"].dt.date.astype(str)
        price_dates = sorted(pd.to_datetime(amber_prices["local_date"]).dt.date.unique())
        amber_default = amber_date if amber_date in price_dates else price_dates[-1]
        amber_date_col, amber_channel_col = st.columns(2)
        with amber_date_col:
            amber_chosen_date = date_picker_with_arrows(
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
            # A forecast can come from an earlier snapshot and extend back to midnight.
            # Once actual/current prices cover those buckets, only show the dotted
            # forecast after the latest observed bucket for that channel.
            latest_observed_bucket = (
                actual_like.assign(
                    _bucket_start_utc=actual_like["_start_utc"].dt.floor("5min"),
                )
                .groupby("channel_type")["_bucket_start_utc"]
                .max()
                .to_dict()
            )
            if latest_observed_bucket:
                observed_cutoff = amber_intraday["channel_type"].map(latest_observed_bucket)
                stale_forecast = (
                    amber_intraday["price_type"].str.lower().eq("forecastinterval")
                    & observed_cutoff.notna()
                    & (amber_intraday["_bucket_start_utc"] <= observed_cutoff)
                )
                amber_intraday = amber_intraday[~stale_forecast].copy()
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
        chart_day_start = pd.Timestamp(amber_chosen_date).tz_localize(TZ_NAME)
        chart_day_end = chart_day_start + pd.DateOffset(days=1)
        price_fig.update_xaxes(range=[chart_day_start, chart_day_end])
        for scenario_window_start, scenario_window_end in scenario_periods:
            scenario_window_start = max(scenario_window_start, chart_day_start)
            scenario_window_end = min(scenario_window_end, chart_day_end)
            if scenario_window_start < scenario_window_end:
                price_fig.add_shape(
                    type="rect", xref="x", yref="paper",
                    x0=scenario_window_start, x1=scenario_window_end,
                    y0=0, y1=1, layer="below",
                    fillcolor="rgba(128,128,128,0.2)", line=dict(width=0),
                )
        place_legend_below_chart(price_fig)
        st.plotly_chart(price_fig, width="stretch")

    st.subheader("Sigen battery state of charge")
    battery_soc = (
        energy_actuals.dropna(subset=["Battery state of charge (%)"])
        if not energy_actuals.empty else pd.DataFrame()
    )
    if battery_soc.empty:
        st.info("No battery state-of-charge values were found in sigen_energy_actuals.battery_soc.")
    else:
        battery_soc_dates = sorted(battery_soc["local_date"].unique())
        battery_soc_default = battery_soc_dates[-1]
        forecast_date_candidates = []
        if not history.empty and "local_date" in history:
            forecast_date_candidates.extend(history["local_date"].dropna().astype(str).tolist())
        if not openmeteo_forecast_hourly.empty and "local_date" in openmeteo_forecast_hourly:
            forecast_date_candidates.extend(
                openmeteo_forecast_hourly["local_date"].dropna().astype(str).tolist()
            )
        latest_selectable_date = max([*battery_soc_dates, *forecast_date_candidates])
        battery_soc_date = half_width_date_picker_with_arrows(
            "Battery SOC date", pd.Timestamp(battery_soc_default).date(),
            min_value=pd.Timestamp(battery_soc_dates[0]).date(),
            max_value=pd.Timestamp(latest_selectable_date).date(),
            key="sigen_battery_soc_date",
        ).isoformat()
        latest_soc = battery_soc.sort_values("period_end_local").iloc[-1]
        latest_soc_time = pd.Timestamp(latest_soc["period_end_local"])
        load_samples = energy_actuals[
            energy_actuals["load_power_kw"].notna()
            & (energy_actuals["period_end_local"] <= latest_soc_time)
            & (energy_actuals["period_end_local"] > latest_soc_time - pd.Timedelta(days=7))
        ].copy()
        if not load_samples.empty:
            load_samples["slot_end"] = load_samples["period_end_local"].dt.ceil("30min")
            load_samples["slot"] = load_samples["slot_end"].dt.strftime("%H:%M")
            load_profile = load_samples.groupby("slot")["load_power_kw"].mean()
            average_load_kw = float(load_samples["load_power_kw"].mean())
        else:
            load_profile = pd.Series(dtype=float)
            average_load_kw = 0.0
        scenario_generation = pd.DataFrame(columns=["time_local", "solcast_kw", "openmeteo_kw"])
        if not history.empty and not openmeteo_forecast_hourly.empty:
            solcast_projection = history.dropna(subset=["pv_estimate"]).copy()
            solcast_projection = (
                solcast_projection.sort_values("downloaded_at_utc", ascending=False)
                .drop_duplicates("period_end_utc", keep="first")
            )
            solcast_projection = solcast_projection[
                solcast_projection["period_end_utc"] > latest_soc_time.tz_convert("UTC")
            ][["period_end_local", "pv_estimate"]].rename(
                columns={"period_end_local": "time_local", "pv_estimate": "solcast_kw"}
            )
            openmeteo_projection = openmeteo_forecast_hourly[
                ["time_local", "openmeteo_power_kw"]
            ].dropna().copy()
            openmeteo_projection["time_local"] = pd.to_datetime(openmeteo_projection["time_local"])
            openmeteo_projection["time_local"] = (
                openmeteo_projection["time_local"].dt.floor("30min")
                + pd.Timedelta(minutes=30)
            )
            openmeteo_projection = openmeteo_projection.groupby("time_local", as_index=False).agg(
                openmeteo_kw=("openmeteo_power_kw", "mean"),
            )
            scenario_generation = solcast_projection.merge(
                openmeteo_projection, on="time_local", how="inner",
            ).sort_values("time_local")
        scenario_current_date = pd.Timestamp.now(tz=TZ_NAME).date()
        scenario_cols = st.columns([1, 1, 1, 1])
        with scenario_cols[0]:
            scenario_start_date = st.date_input(
                "Sell from date", value=scenario_current_date,
                key="battery_soc_scenario_start_date_v2",
            )
        with scenario_cols[1]:
            scenario_start_time = st.time_input(
                "Sell from time", value=dt_time(18, 0),
                key="battery_soc_scenario_start_time_v2",
            )
        with scenario_cols[2]:
            scenario_end_date = st.date_input(
                "Sell until date", value=scenario_current_date,
                key="battery_soc_scenario_end_date_v2",
            )
        with scenario_cols[3]:
            scenario_end_time = st.time_input(
                "Sell until time", value=dt_time(19, 0),
                key="battery_soc_scenario_end_time_v2",
            )
        scenario_target_placeholders = {0: st.empty()}
        scenario_second_enabled = st.checkbox(
            "Add a second sell period", key="battery_soc_scenario_second_enabled",
        )
        if scenario_second_enabled:
            scenario_second_cols = st.columns([1, 1, 1, 1])
            with scenario_second_cols[0]:
                scenario_second_start_date = st.date_input(
                    "Second sell from date", value=scenario_current_date,
                    key="battery_soc_scenario_second_start_date",
                )
            with scenario_second_cols[1]:
                scenario_second_start_time = st.time_input(
                    "Second sell from time", value=dt_time(20, 0),
                    key="battery_soc_scenario_second_start_time",
                )
            with scenario_second_cols[2]:
                scenario_second_end_date = st.date_input(
                    "Second sell until date", value=scenario_current_date,
                    key="battery_soc_scenario_second_end_date",
                )
            with scenario_second_cols[3]:
                scenario_second_end_time = st.time_input(
                    "Second sell until time", value=dt_time(21, 0),
                    key="battery_soc_scenario_second_end_time",
                )
            scenario_target_placeholders[1] = st.empty()
        else:
            scenario_second_start_date = st.session_state.get(
                "battery_soc_scenario_second_start_date", scenario_current_date,
            )
            scenario_second_start_time = st.session_state.get(
                "battery_soc_scenario_second_start_time", dt_time(20, 0),
            )
            scenario_second_end_date = st.session_state.get(
                "battery_soc_scenario_second_end_date", scenario_current_date,
            )
            scenario_second_end_time = st.session_state.get(
                "battery_soc_scenario_second_end_time", dt_time(21, 0),
            )
        scenario_start = pd.Timestamp(datetime.combine(
            scenario_start_date, scenario_start_time,
        )).tz_localize(TZ_NAME)
        scenario_end = pd.Timestamp(datetime.combine(
            scenario_end_date, scenario_end_time,
        )).tz_localize(TZ_NAME)
        scenario_period_slots = [(0, scenario_start, scenario_end)]
        if scenario_second_enabled:
            scenario_period_slots.append((
                1,
                pd.Timestamp(datetime.combine(
                    scenario_second_start_date, scenario_second_start_time,
                )).tz_localize(TZ_NAME),
                pd.Timestamp(datetime.combine(
                    scenario_second_end_date, scenario_second_end_time,
                )).tz_localize(TZ_NAME),
            ))
        scenario_periods = merge_time_windows([
            (period_start, period_end)
            for _, period_start, period_end in scenario_period_slots
        ])
        scenario_window_throughputs_kwh = [0.0] * len(scenario_period_slots)
        if not load_samples.empty and not scenario_generation.empty and scenario_period_slots:
            for generation_row in scenario_generation.itertuples(index=False):
                interval_end = pd.Timestamp(generation_row.time_local)
                interval_start = interval_end - pd.Timedelta(minutes=30)
                typical_load_kw = float(
                    load_profile.get(interval_end.strftime("%H:%M"), average_load_kw)
                )
                solar_kw = min(
                    SIGEN_INVERTER_LIMIT_KW,
                    max(0.0, (float(generation_row.solcast_kw)
                              + float(generation_row.openmeteo_kw)) / 2),
                )
                max_total_export_kw = min(
                    max(0.0, SIGEN_INVERTER_LIMIT_KW - typical_load_kw),
                    SIGEN_SITE_EXPORT_LIMIT_KW,
                )
                solar_export_kw = min(
                    max(0.0, solar_kw - typical_load_kw), max_total_export_kw,
                )
                remaining_export_kw = max(0.0, max_total_export_kw - solar_export_kw)
                for period_index, period_start, period_end in scenario_period_slots:
                    if period_start >= period_end:
                        continue
                    overlaps = overlapping_time_segments(
                        interval_start, interval_end, [(period_start, period_end)],
                    )
                    period_hours = sum(
                        (overlap_end - overlap_start).total_seconds() / 3600
                        for overlap_start, overlap_end in overlaps
                    )
                    scenario_window_throughputs_kwh[period_index] += (
                        remaining_export_kw * period_hours
                    )
        scenario_inverter_throughput_kwh = sum(scenario_window_throughputs_kwh)
        scenario_inverter_max_sell_kwh_by_window = [
            min(SIGEN_BATTERY_CAPACITY_KWH, throughput)
            for throughput in scenario_window_throughputs_kwh
        ]
        scenario_inverter_max_sell_pcts = [
            min(100, int(np.floor(
                max_kwh / SIGEN_BATTERY_CAPACITY_KWH * 100 + 1e-9
            )))
            for max_kwh in scenario_inverter_max_sell_kwh_by_window
        ]
        scenario_soc_start_by_window = [None] * len(scenario_period_slots)
        if (not scenario_generation.empty and not battery_soc.empty
                and pd.Timedelta(0) <= pd.Timestamp.now(tz=TZ_NAME) - latest_soc_time
                <= pd.Timedelta(hours=12)):
            preview_target_kwh = []
            for period_index, _, _ in scenario_period_slots:
                preview_key = f"battery_soc_scenario_sell_pct_{period_index + 1}"
                try:
                    preview_pct = float(st.session_state.get(
                        preview_key,
                        min(20, scenario_inverter_max_sell_pcts[period_index]),
                    ))
                except (TypeError, ValueError):
                    preview_pct = 0.0
                preview_pct = float(np.clip(
                    preview_pct, 0, scenario_inverter_max_sell_pcts[period_index],
                ))
                preview_target_kwh.append(
                    SIGEN_BATTERY_CAPACITY_KWH * preview_pct / 100
                )

            def scenario_soc_at_window_start(window_start):
                """Run the scenario through a window's start and return its SOC there."""
                soc = float(latest_soc["Battery state of charge (%)"])
                sold_by_window = [0.0] * len(scenario_period_slots)
                for generation_row in scenario_generation.itertuples(index=False):
                    forecast_time = pd.Timestamp(generation_row.time_local)
                    if forecast_time <= latest_soc_time:
                        continue
                    interval_start = max(
                        forecast_time - pd.Timedelta(minutes=30), latest_soc_time,
                    )
                    interval_end = min(forecast_time, window_start)
                    if interval_start >= interval_end:
                        if interval_start >= window_start:
                            break
                        continue
                    slot = forecast_time.strftime("%H:%M")
                    load_kw = float(load_profile.get(slot, average_load_kw))
                    solar_kw = min(
                        SIGEN_INVERTER_LIMIT_KW,
                        max(0.0, (float(generation_row.solcast_kw)
                                  + float(generation_row.openmeteo_kw)) / 2),
                    )
                    sale_segments = []
                    for period_index, period_start, period_end in scenario_period_slots:
                        if period_start >= period_end:
                            continue
                        sale_start = max(interval_start, period_start)
                        sale_end = min(interval_end, period_end)
                        if sale_start < sale_end:
                            sale_segments.append((period_index, sale_start, sale_end))
                    sale_hours = sum(
                        (sale_end - sale_start).total_seconds() / 3600
                        for _, sale_start, sale_end in sale_segments
                    )
                    if sale_hours > 0:
                        max_total_export_kw = min(
                            max(0.0, SIGEN_INVERTER_LIMIT_KW - load_kw),
                            SIGEN_SITE_EXPORT_LIMIT_KW,
                        )
                        solar_export_kw = min(
                            max(0.0, solar_kw - load_kw), max_total_export_kw,
                        )
                        battery_export_headroom_kw = max(
                            0.0, max_total_export_kw - solar_export_kw,
                        )
                        net_battery_kwh = (
                            (solar_kw - load_kw) * ((interval_end - interval_start).total_seconds() / 3600 - sale_hours)
                            + (solar_kw - load_kw - solar_export_kw) * sale_hours
                        )
                    else:
                        battery_export_headroom_kw = 0.0
                        net_battery_kwh = (
                            solar_kw - load_kw
                        ) * (interval_end - interval_start).total_seconds() / 3600
                    soc += (
                        net_battery_kwh * 0.95
                        if net_battery_kwh >= 0 else net_battery_kwh / 0.95
                    ) / SIGEN_BATTERY_CAPACITY_KWH * 100
                    soc = float(np.clip(soc, 0, 100))
                    for period_index, sale_start, sale_end in sale_segments:
                        segment_hours = (sale_end - sale_start).total_seconds() / 3600
                        sale_kwh = max(0.0, min(
                            battery_export_headroom_kw * segment_hours,
                            preview_target_kwh[period_index] - sold_by_window[period_index],
                            soc / 100 * SIGEN_BATTERY_CAPACITY_KWH * 0.95,
                        ))
                        sold_by_window[period_index] += sale_kwh
                        soc = max(
                            0.0,
                            soc - sale_kwh / (
                                SIGEN_BATTERY_CAPACITY_KWH * 0.95
                            ) * 100,
                        )
                    if interval_end >= window_start:
                        break
                return soc

            for period_index, period_start, period_end in scenario_period_slots:
                if period_start < period_end and period_start >= latest_soc_time:
                    scenario_soc_start_by_window[period_index] = (
                        scenario_soc_at_window_start(period_start)
                    )

        # Bound each target by the scenario SOC remaining at that window's start.
        scenario_max_sell_kwh_by_window = []
        for period_index, period_start, period_end in scenario_period_slots:
            start_soc = scenario_soc_start_by_window[period_index]
            if start_soc is None:
                scenario_max_sell_kwh_by_window.append(
                    scenario_inverter_max_sell_kwh_by_window[period_index]
                )
                continue
            available_battery_kwh = max(
                0.0, start_soc / 100 * SIGEN_BATTERY_CAPACITY_KWH * 0.95,
            )
            scenario_max_sell_kwh_by_window.append(min(
                scenario_inverter_max_sell_kwh_by_window[period_index],
                available_battery_kwh,
            ))
        scenario_max_sell_pcts = [
            min(100, int(np.floor(
                max_kwh / SIGEN_BATTERY_CAPACITY_KWH * 100 + 1e-9
            )))
            for max_kwh in scenario_max_sell_kwh_by_window
        ]
        scenario_sell_pcts = [0.0] * len(scenario_period_slots)
        for window_index, (period_index, _, _) in enumerate(scenario_period_slots):
            max_pct = scenario_max_sell_pcts[period_index]
            with scenario_target_placeholders[period_index].container():
                input_cols = st.columns([1, 3], vertical_alignment="center")
                with input_cols[0]:
                    raw_pct = st.text_input(
                        f"Sell target for period {window_index + 1} (%)",
                        value=st.session_state.get(
                            f"battery_soc_scenario_sell_pct_{window_index + 1}",
                            str(min(20, max_pct)),
                        ),
                        key=f"battery_soc_scenario_sell_pct_{window_index + 1}",
                        help="Percentage of the 24 kWh battery to target during this sell period.",
                    )
                with input_cols[1]:
                    _, period_start, period_end = scenario_period_slots[window_index]
                    if period_start < period_end:
                        period_window_text = (
                            f"{period_start.strftime('%d %b %H:%M')}–"
                            f"{period_end.strftime('%d %b %H:%M')}"
                        )
                    else:
                        period_window_text = "Invalid time window"
                    start_soc_text = (
                        f"SOC at start {scenario_soc_start_by_window[period_index]:.1f}%"
                        if scenario_soc_start_by_window[period_index] is not None
                        else "SOC forecast unavailable"
                    )
                    st.markdown(
                        f"**Maximum: {max_pct}%**",
                        text_alignment="right",
                    )
                    st.markdown(
                        f"{scenario_max_sell_kwh_by_window[period_index]:.1f} kWh  ·  "
                        f"{period_window_text}  ·  {start_soc_text}"
                    )
            try:
                sell_pct = float(raw_pct.strip())
                if not np.isfinite(sell_pct):
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                sell_pct = 0.0
                st.warning(f"Sell target for period {window_index + 1} must be a number; using 0%.")
            if sell_pct < 0:
                sell_pct = 0.0
                st.warning(f"Sell target for period {window_index + 1} cannot be negative; using 0%.")
            if sell_pct > max_pct:
                sell_pct = float(max_pct)
                st.warning(
                    f"Sell target for period {window_index + 1} exceeds its maximum; "
                    f"using {max_pct}%."
                )
            scenario_sell_pcts[period_index] = sell_pct
        st.caption(
            f"Estimated additional battery export capacity across the selected windows: "
            f"{scenario_inverter_throughput_kwh:.1f} kWh. Per-window capacity: "
            f"{', '.join(f'{value:.1f} kWh' for value in scenario_window_throughputs_kwh) or '—'}. "
            f"Maximum target per window: "
            f"{', '.join(f'{value:.1f} kWh ({pct}%)' for value, pct in zip(scenario_max_sell_kwh_by_window, scenario_max_sell_pcts)) or '—'}. "
            f"Export limit assumed: "
            f"{SIGEN_SITE_EXPORT_LIMIT_KW:.1f} kW. Set SIGEN_EXPORT_LIMIT_KW to your configured site limit."
        )
        battery_soc_intraday = battery_soc[
            battery_soc["local_date"] == battery_soc_date
        ].sort_values("period_end_local")
        soc_fig = go.Figure()
        soc_fig.add_trace(go.Scatter(
            x=battery_soc_intraday["period_end_local"],
            y=battery_soc_intraday["Battery state of charge (%)"],
            mode="lines", name="Battery state of charge",
            line=dict(width=2, color="#4c78a8"), connectgaps=False,
            hovertemplate="%{x|%H:%M}<br>Battery SOC: %{y:.1f}%<extra></extra>",
        ))
        soc_day_start = pd.Timestamp(battery_soc_date).tz_localize(TZ_NAME)
        projection_available = False
        projection_caption = ""
        now_local = pd.Timestamp.now(tz=TZ_NAME)
        can_project_from_latest_soc = (
            pd.Timedelta(0) <= now_local - latest_soc_time <= pd.Timedelta(hours=12)
        )
        projection_path = pd.DataFrame()
        scenario_sale_revenue = 0.0
        scenario_sold_kwh = 0.0
        scenario_priced_kwh = 0.0
        scenario_amber_price_lookup = pd.DataFrame()
        if not amber_prices.empty:
            scenario_amber_prices = amber_prices[
                (amber_prices["channel_type"] == "feedIn")
                & amber_prices["per_kwh"].notna()
                & amber_prices["price_type"].str.lower().isin(
                    ("actualinterval", "currentinterval", "forecastinterval")
                )
            ].copy()
            if not scenario_amber_prices.empty:
                scenario_amber_prices["_start_local"] = pd.to_datetime(
                    scenario_amber_prices["start_time_local"]
                )
                scenario_amber_prices["_end_local"] = pd.to_datetime(
                    scenario_amber_prices["end_time_local"]
                )
                scenario_amber_prices["_priority"] = scenario_amber_prices[
                    "price_type"
                ].str.lower().map({"actualinterval": 0, "currentinterval": 1, "forecastinterval": 2})
                scenario_amber_prices["_duration_minutes"] = (
                    (scenario_amber_prices["_end_local"]
                     - scenario_amber_prices["_start_local"]).dt.total_seconds() / 60
                )
                bucket_rows = []
                for _, price_row in scenario_amber_prices.iterrows():
                    bucket_start = price_row["_start_local"].floor("5min")
                    while bucket_start < price_row["_end_local"]:
                        bucket_rows.append({
                            "bucket_start": bucket_start,
                            "per_kwh": float(price_row["per_kwh"]),
                            "priority": int(price_row["_priority"]),
                            "duration_minutes": float(price_row["_duration_minutes"]),
                            "fetched_at_utc": price_row["fetched_at_utc"],
                        })
                        bucket_start += pd.Timedelta(minutes=5)
                if bucket_rows:
                    scenario_amber_price_lookup = (
                        pd.DataFrame(bucket_rows)
                        .sort_values(
                            ["priority", "duration_minutes", "fetched_at_utc"],
                            ascending=[True, True, False],
                        )
                        .drop_duplicates("bucket_start", keep="first")
                        .set_index("bucket_start")
                    )
        if (can_project_from_latest_soc and not load_samples.empty
                and not openmeteo_forecast_hourly.empty and not history.empty):
            paired_generation = scenario_generation.copy()
            if not paired_generation.empty:
                current_soc = float(latest_soc["Battery state of charge (%)"])
                low_soc = current_soc
                high_soc = current_soc
                midpoint_soc = current_soc
                low_values, high_values, midpoint_values = [], [], []
                for forecast_row in paired_generation.itertuples(index=False):
                    slot = forecast_row.time_local.strftime("%H:%M")
                    load_kw = float(load_profile.get(slot, average_load_kw))
                    load_kwh = load_kw * 0.5
                    solar_values = [max(0.0, float(forecast_row.solcast_kw)),
                                    max(0.0, float(forecast_row.openmeteo_kw))]

                    def advance_soc(soc, solar_kw):
                        net_battery_kwh = solar_kw * 0.5 - load_kwh
                        stored_kwh = net_battery_kwh * 0.95 if net_battery_kwh >= 0 else net_battery_kwh / 0.95
                        return float(np.clip(
                            soc + stored_kwh / SIGEN_BATTERY_CAPACITY_KWH * 100, 0, 100,
                        ))

                    low_soc = advance_soc(low_soc, min(solar_values))
                    high_soc = advance_soc(high_soc, max(solar_values))
                    midpoint_soc = advance_soc(midpoint_soc, sum(solar_values) / 2)
                    low_values.append(low_soc)
                    high_values.append(high_soc)
                    midpoint_values.append(midpoint_soc)

                projection_times = paired_generation["time_local"]
                projection_times = pd.DatetimeIndex([latest_soc_time]).append(
                    pd.DatetimeIndex(projection_times)
                )
                low_values = [current_soc, *low_values]
                high_values = [current_soc, *high_values]
                midpoint_values = [current_soc, *midpoint_values]
                projection_path = pd.DataFrame({
                    "time_local": projection_times,
                    "soc_low": low_values,
                    "soc_high": high_values,
                    "soc_midpoint": midpoint_values,
                })
                if any(sell_pct > 0 for sell_pct in scenario_sell_pcts) and scenario_periods:
                    target_sale_kwh_by_window = [
                        SIGEN_BATTERY_CAPACITY_KWH * sell_pct / 100
                        for sell_pct in scenario_sell_pcts
                    ]
                    window_sold_kwh = [0.0] * len(scenario_period_slots)
                    scenario_soc = current_soc
                    scenario_values = [scenario_soc]
                    for forecast_row in paired_generation.itertuples(index=False):
                        forecast_time = pd.Timestamp(forecast_row.time_local)
                        slot = forecast_time.strftime("%H:%M")
                        typical_load_kw = float(load_profile.get(slot, average_load_kw))
                        solar_kw = min(
                            SIGEN_INVERTER_LIMIT_KW,
                            max(0.0, (float(forecast_row.solcast_kw)
                                      + float(forecast_row.openmeteo_kw)) / 2),
                        )
                        interval_start = forecast_time - pd.Timedelta(minutes=30)
                        sale_segments = []
                        if forecast_time > latest_soc_time:
                            for period_index, period_start, period_end in scenario_period_slots:
                                if period_start >= period_end:
                                    continue
                                sale_start = max(interval_start, period_start)
                                sale_end = min(forecast_time, period_end)
                                if sale_start < sale_end:
                                    sale_segments.append((period_index, sale_start, sale_end))
                        sale_hours = sum(
                            (sale_end - sale_start).total_seconds() / 3600
                            for _, sale_start, sale_end in sale_segments
                        )
                        sale_period_active = sale_hours > 0
                        battery_export_headroom_kw = 0.0
                        if sale_period_active:
                            max_total_export_kw = min(
                                max(0.0, SIGEN_INVERTER_LIMIT_KW - typical_load_kw),
                                SIGEN_SITE_EXPORT_LIMIT_KW,
                            )
                            solar_export_kw = min(
                                max(0.0, solar_kw - typical_load_kw),
                                max_total_export_kw,
                            )
                            battery_export_headroom_kw = max(
                                0.0, max_total_export_kw - solar_export_kw,
                            )
                            net_battery_kwh = (
                                (solar_kw - typical_load_kw)
                                * (0.5 - sale_hours)
                                + (solar_kw - typical_load_kw - solar_export_kw)
                                * sale_hours
                            )
                        else:
                            net_battery_kwh = (solar_kw - typical_load_kw) * 0.5
                        if net_battery_kwh >= 0:
                            scenario_soc += (
                                net_battery_kwh * 0.95
                                / SIGEN_BATTERY_CAPACITY_KWH * 100
                            )
                        else:
                            scenario_soc += (
                                net_battery_kwh / 0.95
                                / SIGEN_BATTERY_CAPACITY_KWH * 100
                            )
                        scenario_soc = float(np.clip(scenario_soc, 0, 100))
                        forecast_time = pd.Timestamp(forecast_time)
                        if sale_period_active:
                            for period_index, sale_start, sale_end in sale_segments:
                                segment_hours = (sale_end - sale_start).total_seconds() / 3600
                                available_ac_energy_kwh = (
                                    scenario_soc / 100
                                    * SIGEN_BATTERY_CAPACITY_KWH * 0.95
                                )
                                segment_sale_kwh = max(0.0, min(
                                    battery_export_headroom_kw * segment_hours,
                                    target_sale_kwh_by_window[period_index]
                                    - window_sold_kwh[period_index],
                                    available_ac_energy_kwh,
                                ))
                                window_sold_kwh[period_index] += segment_sale_kwh
                                scenario_sold_kwh += segment_sale_kwh
                                scenario_soc = max(
                                    0.0,
                                    scenario_soc - segment_sale_kwh / (
                                        SIGEN_BATTERY_CAPACITY_KWH * 0.95
                                    ) * 100,
                                )
                                if segment_sale_kwh > 0 and not scenario_amber_price_lookup.empty:
                                    price_values = []
                                    price_weights = []
                                    price_start = sale_start.floor("5min")
                                    while price_start < sale_end:
                                        bucket_end = price_start + pd.Timedelta(minutes=5)
                                        overlap_start = max(price_start, sale_start)
                                        overlap_end = min(bucket_end, sale_end)
                                        overlap_minutes = max(
                                            0.0,
                                            (overlap_end - overlap_start).total_seconds() / 60,
                                        )
                                        try:
                                            bucket_price = scenario_amber_price_lookup.loc[
                                                price_start, "per_kwh"
                                            ]
                                            if isinstance(bucket_price, pd.Series):
                                                bucket_price = bucket_price.iloc[0]
                                        except KeyError:
                                            bucket_price = None
                                        if bucket_price is not None and overlap_minutes > 0:
                                            price_values.append(float(bucket_price))
                                            price_weights.append(overlap_minutes)
                                        price_start = bucket_end
                                    if price_weights:
                                        average_price_cents = float(np.average(
                                            price_values, weights=price_weights,
                                        ))
                                        priced_fraction = min(
                                            1.0,
                                            sum(price_weights) / max(segment_hours * 60, 1e-9),
                                        )
                                        priced_sale_kwh = segment_sale_kwh * priced_fraction
                                        scenario_sale_revenue += (
                                            priced_sale_kwh * average_price_cents / 100
                                        )
                                        scenario_priced_kwh += priced_sale_kwh
                        scenario_values.append(scenario_soc)
                    projection_path["soc_scenario"] = scenario_values
                projection_available = True
                projection_caption = (
                    f"Dashed forecast band uses Solcast and Open-Meteo production estimates, "
                    f"with time-of-day average Sigen load (overall mean {average_load_kw:.2f} kW) "
                    "from the previous seven days. "
                    f"It assumes {SIGEN_BATTERY_CAPACITY_KWH:.0f} kWh usable capacity, 95% charge/discharge efficiency, "
                    "and does not model grid charging, export, or curtailment."
                )
        if not projection_caption:
            projection_caption = (
                "Forecast range needs a recent Sigen SOC reading, both forecast providers, "
                "and recent Sigen load readings."
            )
        if projection_available:
            day_end = soc_day_start + pd.DateOffset(days=1)
            prior_state = projection_path[
                projection_path["time_local"] <= soc_day_start
            ].tail(1)
            day_states = projection_path[
                (projection_path["time_local"] > soc_day_start)
                & (projection_path["time_local"] <= day_end)
            ]
            if not prior_state.empty:
                prior_day_state = {
                    "time_local": [soc_day_start],
                    "soc_low": [prior_state.iloc[0]["soc_low"]],
                    "soc_high": [prior_state.iloc[0]["soc_high"]],
                    "soc_midpoint": [prior_state.iloc[0]["soc_midpoint"]],
                }
                if "soc_scenario" in projection_path:
                    prior_day_state["soc_scenario"] = [
                        prior_state.iloc[0]["soc_scenario"]
                    ]
                day_states = pd.concat([
                    pd.DataFrame(prior_day_state),
                    day_states,
                ], ignore_index=True)
            if not day_states.empty:
                soc_fig.add_trace(go.Scatter(
                    x=day_states["time_local"], y=day_states["soc_low"],
                    mode="lines", name="Forecast range low",
                    line=dict(width=1, color="rgba(123,97,168,0.65)", dash="dot"),
                    hovertemplate="%{x|%H:%M}<br>Lower SOC: %{y:.1f}%<extra></extra>",
                ))
                soc_fig.add_trace(go.Scatter(
                    x=day_states["time_local"], y=day_states["soc_high"],
                    mode="lines", name="Forecast range high",
                    line=dict(width=1, color="rgba(123,97,168,0.65)", dash="dot"),
                    fill="tonexty", fillcolor="rgba(123,97,168,0.14)",
                    hovertemplate="%{x|%H:%M}<br>Upper SOC: %{y:.1f}%<extra></extra>",
                ))
                soc_fig.add_trace(go.Scatter(
                    x=day_states["time_local"], y=day_states["soc_midpoint"],
                    mode="lines", name="Forecast SOC midpoint",
                    line=dict(width=2, color="#7b61a8", dash="dot"),
                    hovertemplate="%{x|%H:%M}<br>Forecast SOC: %{y:.1f}%<extra></extra>",
                ))
                if "soc_scenario" in day_states:
                    soc_fig.add_trace(go.Scatter(
                        x=day_states["time_local"], y=day_states["soc_scenario"],
                        mode="lines", name="SOC scenario with battery sales",
                        line=dict(width=2, color="#d62728", dash="dash"),
                        hovertemplate="%{x|%H:%M}<br>Scenario SOC: %{y:.1f}%<extra></extra>",
                    ))
        soc_fig.update_layout(
            xaxis_title="Time (Sydney)", yaxis_title="Battery state of charge (%)",
            margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified",
        )
        soc_fig.update_yaxes(range=[0, 100], ticksuffix="%")
        soc_fig.update_xaxes(
            range=[soc_day_start, soc_day_start + pd.DateOffset(days=1)], fixedrange=True,
        )
        place_legend_below_chart(soc_fig)
        st.plotly_chart(soc_fig, width="stretch")
        st.caption("Battery SOC is read from the battery_soc column in sigen_energy_actuals. " + projection_caption)
        if any(sell_pct > 0 for sell_pct in scenario_sell_pcts):
            if not scenario_periods:
                st.caption("Choose valid sell start and end dates and times to show the sales scenario.")
            else:
                scenario_window_labels = "; ".join(
                    f"{window_start.strftime('%d %b %Y %H:%M')}–{window_end.strftime('%d %b %Y %H:%M')}"
                    for window_start, window_end in scenario_periods
                )
                scenario_target_labels = "; ".join(
                    f"period {index + 1}: {sell_pct:g}% "
                    f"({SIGEN_BATTERY_CAPACITY_KWH * sell_pct / 100:.1f} kWh AC)"
                    for index, sell_pct in enumerate(scenario_sell_pcts)
                )
                st.caption(
                    f"Sales scenario targets {scenario_target_labels} "
                    f"during {scenario_window_labels} Sydney time. "
                    "Export is capped each 30-minute period by inverter and site export headroom after typical load and solar export; "
                    "the scenario line appears when a forecast SOC path is available."
                )
                predicted_sale_value = -scenario_sale_revenue
                sale_value_label = (
                    f"-${abs(predicted_sale_value):,.2f}"
                    if predicted_sale_value < 0
                    else f"${predicted_sale_value:,.2f}"
                ) if scenario_priced_kwh > 0 else "—"
                priced_coverage = (
                    scenario_priced_kwh / scenario_sold_kwh * 100
                    if scenario_sold_kwh > 0 else 0.0
                )
                sale_metric_cols = st.columns(3)
                sale_metric_cols[0].metric(
                    "Predicted Amber sale value", sale_value_label,
                    help="Estimated revenue from simulated battery export, using Amber feed-in prices for matching intervals.",
                )
                sale_metric_cols[1].metric("Scenario energy sold", f"{scenario_sold_kwh:.2f} kWh")
                sale_metric_cols[2].metric("Energy with Amber prices", f"{priced_coverage:.0f}%")
                st.caption(
                    "Sale value is an estimate using Amber feed-in prices (actual/current prices where available, "
                    "otherwise forecast prices). Unpriced intervals are excluded from the value."
                )


_render_amber_and_battery_panels()

if history.empty:
    st.info("No forecast data saved yet. Configure SOLCAST_API_KEY and choose **Download latest forecast**.")
    if openmeteo_forecast_hourly.empty and openmeteo_historical_forecast_hourly.empty:
        st.markdown("""
        **What this dashboard will show**
        - Daily forecast energy (kWh)
        - Half-hourly forecast power (kW)
        """)
        st.stop()

today = pd.Timestamp.now(tz=TZ_NAME).date().isoformat()
daily = daily_forecast_summary(
    latest, openmeteo_historical_forecast_hourly, openmeteo_forecast_hourly, today,
)
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
tomorrow = (pd.Timestamp.now(tz=TZ_NAME).normalize() + pd.Timedelta(days=1)).date().isoformat()
today_row = daily.loc[daily["local_date"] == today]
today_total_kwh = None
if not today_row.empty:
    today_total_kwh = today_row["Forecast_kWh"].fillna(0).iloc[0] + today_row["Actual_energy_kWh"].fillna(0).iloc[0]
tomorrow_val = daily.loc[daily["local_date"] == tomorrow, "Forecast_kWh"].dropna()
best = (
    daily.loc[daily["Forecast_kWh"].idxmax()]
    if daily["Forecast_kWh"].notna().any() else None
)

st.subheader("Daily forecast generation")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Today forecast + meter actual", f"{today_total_kwh:.1f} kWh" if today_total_kwh is not None else "—")
k2.metric("Tomorrow forecast", f"{tomorrow_val.iloc[0]:.1f} kWh" if len(tomorrow_val) else "—")
k3.metric("Best forecast day", f"{best['Forecast_kWh']:.1f} kWh" if best is not None else "—",
          help=str(best["local_date"]) if best is not None else None)
k4.metric(
    "Peak forecast power",
    f"{daily['Peak_kW'].max():.2f} kW" if daily["Peak_kW"].notna().any() else "—",
)
if not history.empty:
    st.caption(f"Latest forecast downloaded: {latest_stamp.strftime('%d %b %Y, %H:%M UTC')} · {len(latest)} intervals")
else:
    st.caption("Daily forecast bars use Open-Meteo because no Solcast forecast is saved.")

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
    x=daily["local_date"],
    y=daily["Forecast_kWh"].where(daily["Forecast_source"] == "Solcast"),
    name="Solcast forecast energy (kWh)",
    customdata=daily_tooltip_data,
    hovertemplate="%{x}<br>Forecast: %{customdata[0]}<br>Meter actual: %{customdata[2]}<br>Solcast estimated actual: %{customdata[1]}<br>Sigen non-zero production: %{customdata[3]}<br>Open-Meteo daylight duration: %{customdata[4]}<extra></extra>"
))
fig.add_trace(go.Bar(
    x=daily["local_date"],
    y=daily["Forecast_kWh"].where(
        daily["Forecast_source"].str.startswith("Open-Meteo", na=False)
    ),
    name="Open-Meteo forecast energy (kWh)",
    marker_color="#7b61a8",
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
place_legend_below_chart(fig)
st.plotly_chart(fig, width="stretch")

def format_kwh(value):
    return f"{value:.3f} kWh" if pd.notna(value) else "—"

def generated_date_label(timestamps):
    parsed = pd.to_datetime(pd.Series(timestamps), utc=True, errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().tz_convert(TZ_NAME).strftime("%d %b %Y")

def power_accuracy_metrics(label, predictions, time_col, power_col, meter_power,
                           interval_hours, issue_time_col=None):
    """Score forecast power against interpolated Sigen samples at the same times."""
    if predictions.empty or meter_power.empty:
        return None
    frame = predictions[[time_col, power_col] + ([issue_time_col] if issue_time_col else [])].copy()
    frame["valid_time_utc"] = pd.to_datetime(frame[time_col], utc=True, errors="coerce")
    frame["forecast_kw"] = pd.to_numeric(frame[power_col], errors="coerce")
    frame = frame.dropna(subset=["valid_time_utc", "forecast_kw"])
    actual = meter_power[["period_end_local", "Solar Production (kW)"]].copy()
    actual["valid_time_utc"] = pd.to_datetime(actual["period_end_local"], utc=True, errors="coerce")
    actual["actual_kw"] = pd.to_numeric(actual["Solar Production (kW)"], errors="coerce")
    actual = actual.dropna(subset=["valid_time_utc", "actual_kw"])
    if frame.empty or actual.empty:
        return None
    actual_series = actual.drop_duplicates("valid_time_utc", keep="last").set_index("valid_time_utc")["actual_kw"].sort_index()
    requested_times = pd.DatetimeIndex(frame["valid_time_utc"].drop_duplicates()).sort_values()
    aligned_times = actual_series.index.union(requested_times).sort_values()
    aligned_actual = (
        actual_series.reindex(aligned_times)
        .interpolate(method="time", limit_area="inside")
        .reindex(requested_times)
    )
    frame["actual_kw"] = frame["valid_time_utc"].map(aligned_actual)
    frame = frame.dropna(subset=["actual_kw"])
    if frame.empty:
        return None
    error = frame["forecast_kw"] - frame["actual_kw"]
    if interval_hours is None:
        sample_times = pd.DatetimeIndex(frame["valid_time_utc"].drop_duplicates()).sort_values()
        interval_hours = sample_times.to_series().diff().dt.total_seconds().median() / 3600
    if pd.isna(interval_hours) or interval_hours <= 0:
        return None
    forecast_energy = frame["forecast_kw"].sum() * interval_hours
    actual_energy = frame["actual_kw"].sum() * interval_hours
    energy_error_pct = (
        (forecast_energy - actual_energy) / actual_energy * 100
        if actual_energy > 0 else float("nan")
    )
    return {
        "Series": label,
        "Matched periods": len(frame),
        "MAE (kW)": error.abs().mean(),
        "RMSE (kW)": (error.pow(2).mean()) ** 0.5,
        "Bias (kW)": error.mean(),
        "Energy error (%)": energy_error_pct,
        "Forecast energy (kWh)": forecast_energy,
        "Sigen energy (kWh)": actual_energy,
    }

def forecast_lead_time_scores(forecasts, time_col, power_col, meter_power, interval_hours):
    """Calculate error metrics by how far ahead each saved forecast was issued."""
    if forecasts.empty or "fetched_at_utc" not in forecasts or meter_power.empty:
        return pd.DataFrame()
    frame = forecasts.copy()
    frame["valid_time_utc"] = pd.to_datetime(frame[time_col], utc=True, errors="coerce")
    frame["fetched_at_utc"] = pd.to_datetime(frame["fetched_at_utc"], utc=True, errors="coerce")
    frame["lead_hours"] = (frame["valid_time_utc"] - frame["fetched_at_utc"]).dt.total_seconds() / 3600
    bins = [0, 24, 48, 72, 168]
    labels = ["0–24 hours", "1–2 days", "2–3 days", "3–7 days"]
    frame["Lead time"] = pd.cut(frame["lead_hours"], bins=bins, labels=labels, right=True)
    rows = []
    for lead_label in labels:
        group = frame[frame["Lead time"] == lead_label]
        score = power_accuracy_metrics(
            lead_label, group, time_col, power_col, meter_power, interval_hours,
        )
        if score:
            rows.append(score)
    return pd.DataFrame(rows)


@st.fragment
@log_performance
def _render_solcast_power_by_time():
    st.subheader("Solcast Forecast and actual power by time")
    available_dates = sorted(set(latest["local_date"])
                             .union(actuals["local_date"] if not actuals.empty else [])
                             .union(power_actuals["local_date"] if not power_actuals.empty else [])
                             .union(openmeteo_forecast_hourly["local_date"] if not openmeteo_forecast_hourly.empty else [])
                             .union(openmeteo_historical_forecast_hourly["local_date"] if not openmeteo_historical_forecast_hourly.empty else []))
    actual_dates = set(actuals["local_date"]) if not actuals.empty else set()
    actual_dates.update(power_actuals["local_date"] if not power_actuals.empty else [])
    overlap_dates = sorted(set(latest["local_date"]).intersection(actual_dates))
    default_date = overlap_dates[-1] if overlap_dates else (available_dates[-1] if available_dates else None)
    chosen_date = half_width_date_picker_with_arrows(
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
    solcast_forecast_intraday = solcast_comparison_forecast(history, latest, chosen_date)
    if not solcast_forecast_intraday.empty and not meter_power_intraday.empty:
        solcast_power = solcast_forecast_intraday.set_index("period_end_local")[["pv_estimate"]].sort_index()
        actual_times = pd.DatetimeIndex(meter_power_intraday["period_end_local"])
        combined_times = solcast_power.index.union(actual_times).sort_values()
        aligned_solcast_power = (
            solcast_power.reindex(combined_times)
            .interpolate(method="time", limit_area="inside")
            .reindex(actual_times)
        )
        solcast_comparison_weather = pd.DataFrame({
            "time_local": actual_times,
            "solcast_forecast_power_kw": aligned_solcast_power["pv_estimate"].to_numpy(),
        }).dropna(subset=["solcast_forecast_power_kw"])

    solcast_estimate_kwh = None
    solcast_meter_kwh = None
    solcast_accuracy_score = None
    solcast_comparison_intervals = 0
    if not solcast_comparison_weather.empty and not meter_power_intraday.empty:
        matched_solcast_power = meter_power_intraday[
            ["period_end_local", "Solar Production (kW)"]
        ].merge(
            solcast_comparison_weather[["time_local", "solcast_forecast_power_kw"]],
            left_on="period_end_local", right_on="time_local", how="inner",
        )
        solcast_comparison_intervals = len(matched_solcast_power)
        solcast_estimate_kwh = matched_solcast_power["solcast_forecast_power_kw"].sum() * (5 / 60)
        solcast_meter_kwh = matched_solcast_power["Solar Production (kW)"].sum() * (5 / 60)
        if solcast_meter_kwh > 0:
            solcast_accuracy_score = max(
                0.0,
                100 * (1 - abs(solcast_estimate_kwh - solcast_meter_kwh) / solcast_meter_kwh),
            )

    solcast_day_start = pd.Timestamp(chosen_date).tz_localize(TZ_NAME).tz_convert("UTC")
    solcast_day_end = (
        (pd.Timestamp(chosen_date) + pd.DateOffset(days=1))
        .tz_localize(TZ_NAME).tz_convert("UTC")
    )
    solcast_full_day_forecast = history[
        (history["period_end_utc"] > solcast_day_start)
        & (history["period_end_utc"] <= solcast_day_end)
    ].copy()
    solcast_full_day_forecast = (
        solcast_full_day_forecast.sort_values("downloaded_at_utc", ascending=False)
        .drop_duplicates("period_end_utc", keep="first")
        .dropna(subset=["pv_estimate"])
    )
    if actuals.empty:
        solcast_full_day_actuals = pd.DataFrame(
            columns=["period_end_utc", "period_hours", "energy_kwh"],
        )
    else:
        solcast_full_day_actuals = actuals[
            (actuals["period_end_utc"] > solcast_day_start)
            & (actuals["period_end_utc"] <= solcast_day_end)
        ].copy()
    solcast_full_day_estimate_kwh = None
    solcast_full_day_basis = ""
    solcast_generated_date = None
    actual_period_ends = set(pd.to_datetime(solcast_full_day_actuals["period_end_utc"], utc=True))
    forecast_period_ends = pd.to_datetime(solcast_full_day_forecast["period_end_utc"], utc=True)
    uncovered_forecast = solcast_full_day_forecast.loc[~forecast_period_ends.isin(actual_period_ends)]
    solcast_full_day_intervals = pd.concat([
        solcast_full_day_actuals[["period_end_utc", "period_hours", "energy_kwh"]],
        uncovered_forecast[["period_end_utc", "period_hours", "energy_kwh"]],
    ], ignore_index=True)
    if not solcast_full_day_intervals.empty and intervals_cover_full_local_day(
        solcast_full_day_intervals["period_end_utc"],
        solcast_full_day_intervals["period_hours"],
        chosen_date,
    ):
        solcast_full_day_estimate_kwh = pd.to_numeric(
            solcast_full_day_intervals["energy_kwh"], errors="coerce",
        ).sum()
        solcast_source_timestamps = pd.concat([
            solcast_full_day_actuals["downloaded_at_utc"],
            uncovered_forecast["downloaded_at_utc"],
        ], ignore_index=True)
        solcast_generated_date = generated_date_label(solcast_source_timestamps)
        if not solcast_full_day_actuals.empty and not uncovered_forecast.empty:
            solcast_full_day_basis = "Solcast estimated actuals plus forecasts for uncovered intervals"
        elif not solcast_full_day_actuals.empty:
            solcast_full_day_basis = "Full-day Solcast estimated actuals"
        else:
            solcast_full_day_basis = "Full-day saved Solcast forecast"

    solcast_metric_cols = st.columns(4)
    solcast_metric_cols[0].metric(
        "Full-day Solcast estimate" + (f" · {solcast_generated_date}" if solcast_generated_date else ""),
        f"{solcast_full_day_estimate_kwh:.2f} kWh" if solcast_full_day_estimate_kwh is not None else "—",
        help=solcast_full_day_basis or "Full-day Solcast data is incomplete or unavailable for the selected date.",
    )
    solcast_metric_cols[1].metric(
        "Solcast forecast energy (matched periods)",
        f"{solcast_estimate_kwh:.2f} kWh" if solcast_estimate_kwh is not None else "—",
        help="Forecast energy interpolated to the five-minute Sigen timestamps available for the selected date.",
    )
    solcast_metric_cols[2].metric(
        "Sigen actual energy (matched periods)",
        f"{solcast_meter_kwh:.2f} kWh" if solcast_meter_kwh is not None else "—",
        help="Meter energy over the same five-minute periods as the interpolated Solcast forecast.",
    )
    solcast_metric_cols[3].metric(
        "Solcast estimate accuracy",
        f"{solcast_accuracy_score:.1f}%" if solcast_accuracy_score is not None else "—",
        help="100% minus absolute percentage error against Sigen energy over forecast-matched periods, floored at 0%.",
    )
    if solcast_comparison_intervals:
        st.caption(f"Daily Solcast forecast comparison uses {solcast_comparison_intervals:,} five-minute Sigen intervals with interpolated forecast data.")
    elif meter_power_intraday.empty:
        st.caption("No Sigen power actuals are available for this date, so the energy comparison and accuracy score are unavailable.")
    else:
        st.caption("No matching Solcast forecast and Sigen timestamps are available for this date, so the energy comparison and accuracy score are unavailable.")
    fig2 = go.Figure()
    solcast_chart_forecast = solcast_forecast_intraday
    if not solcast_chart_forecast.empty:
        fig2.add_trace(go.Scatter(
            x=solcast_chart_forecast["period_end_local"], y=solcast_chart_forecast["pv_estimate"],
            mode="lines", name="Solcast forecast power (kW)",
            line=dict(width=2, color="#636EFA", dash="dot"),
            customdata=solcast_chart_forecast[["energy_kwh", "forecast_source"]].to_numpy(),
            hovertemplate=(
                "%{x|%H:%M}<br>Forecast: %{y:.2f} kW"
                "<br>Forecast energy: %{customdata[0]:.3f} kWh"
                "<br>Source: %{customdata[1]}<extra></extra>"
            ),
        ))
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
            line=dict(width=2, color="#83C9FF"),
            hovertemplate="%{x|%H:%M}<br>Meter actual power: %{y:.3f} kW<extra></extra>"
        ))
    meter_power_30min_solcast = resample_meter_power_to_30min(meter_power_intraday, chosen_date)
    if not meter_power_30min_solcast.empty:
        fig2.add_trace(go.Scatter(
            x=meter_power_30min_solcast["time_local"],
            y=meter_power_30min_solcast["Solar Production (kW)"],
            mode="lines", connectgaps=False,
            name="Sigen meter actual power (30-minute mean)",
            line=dict(width=2, color="#83C9FF", dash="dash"),
            hovertemplate=(
                "%{x|%H:%M}<br>Sigen meter 30-minute mean: "
                "%{y:.3f} kW<extra></extra>"
            ),
        ))
    if solcast_chart_forecast.empty and actual_intraday.empty and meter_power_intraday.empty:
        st.info("No forecast or estimated actual intervals are saved for this date.")
    fig2.update_layout(xaxis_title="Time (Sydney)", yaxis_title="Power (kW)",
                       margin=dict(l=10, r=10, t=20, b=10), hovermode="closest")
    solcast_chart_day_start = pd.Timestamp(chosen_date).tz_localize(TZ_NAME)
    solcast_chart_day_end = solcast_chart_day_start + pd.DateOffset(days=1)
    fig2.update_xaxes(
        range=[solcast_chart_day_start, solcast_chart_day_end], fixedrange=True,
    )
    place_legend_below_chart(fig2)
    st.plotly_chart(fig2, width="stretch")

    st.markdown("**Solcast accuracy against Sigen actuals**")
    solcast_accuracy_rows = []
    score = power_accuracy_metrics(
        "Solcast forecast", solcast_chart_forecast, "period_end_utc", "pv_estimate",
        meter_power_intraday, 0.5,
    )
    if score:
        solcast_accuracy_rows.append(score)
    score = power_accuracy_metrics(
        "Solcast estimated actual", actual_intraday, "period_end_utc", "pv_estimate",
        meter_power_intraday, 0.5,
    )
    if score:
        solcast_accuracy_rows.append(score)
    if solcast_accuracy_rows:
        st.dataframe(pd.DataFrame(solcast_accuracy_rows), width="stretch", hide_index=True)
        st.caption("MAE and RMSE show typical point error; positive bias and energy error mean over-prediction. Scores use matched timestamps only.")
    else:
        st.caption("Accuracy scores appear when Sigen actuals overlap the selected Solcast date.")

    solcast_lead_rows = history[history["local_date"] == chosen_date]
    solcast_lead_scores = forecast_lead_time_scores(
        solcast_lead_rows, "period_end_utc", "pv_estimate", meter_power_intraday, 0.5,
    )
    if not solcast_lead_scores.empty:
        with st.expander("Solcast forecast accuracy by lead time"):
            st.dataframe(solcast_lead_scores, width="stretch", hide_index=True)
            st.caption("Each saved forecast interval is scored at its original issue time, grouped by time until the interval ends.")


_render_solcast_power_by_time()

@st.fragment
@log_performance
def _render_openmeteo_power_by_time():
    st.subheader("Open-Meteo estimated and actual power by time")
    if (
        performance_ratio_samples >= OPENMETEO_CALIBRATION_MIN_SAMPLES
        and performance_ratio_days == 1
    ):
        performance_ratio_caption = (
            f"a calibrated {openmeteo_performance_ratio:.3f} ratio from "
            f"{performance_ratio_samples:,} daylight 30-minute means on "
            f"{OPENMETEO_CALIBRATION_DATE}"
        )
    else:
        performance_ratio_caption = (
            f"the default {OPENMETEO_DEFAULT_PERFORMANCE_RATIO:.2f} ratio "
            f"(need {OPENMETEO_CALIBRATION_MIN_SAMPLES} matching daylight 30-minute means "
            f"on {OPENMETEO_CALIBRATION_DATE} to calibrate)"
        )
    st.caption(
        "Open-Meteo estimates weight 22° north- and east-facing tilted irradiance by panel count "
        "(16 north, 4 east) at -32.9295, 151.7801, scaled to 9.5 kW with "
        f"{performance_ratio_caption}. Actual power comes from the Sigen meter. "
        "The satellite archive is plotted alongside the hourly historical forecast and the live 15-minute forecast."
    )
    openmeteo_dates = sorted(openmeteo_hourly["local_date"].unique()) if not openmeteo_hourly.empty else []
    openmeteo_forecast_dates = (
        sorted(openmeteo_forecast_hourly["local_date"].unique())
        if not openmeteo_forecast_hourly.empty else []
    )
    openmeteo_historical_forecast_dates = (
        sorted(openmeteo_historical_forecast_hourly["local_date"].unique())
        if not openmeteo_historical_forecast_hourly.empty else []
    )
    complete_openmeteo_dates = []
    if openmeteo_dates:
        valid_weather_rows = openmeteo_hourly.groupby("local_date")["global_tilted_irradiance"].count()
        complete_openmeteo_dates = sorted(valid_weather_rows[valid_weather_rows >= 100].index)
    meter_dates = set(power_actuals["local_date"]) if not power_actuals.empty else set()
    all_openmeteo_dates = sorted(
        set(openmeteo_dates).union(openmeteo_forecast_dates).union(openmeteo_historical_forecast_dates)
    )
    default_openmeteo_dates = sorted(
        (set(complete_openmeteo_dates).union(openmeteo_forecast_dates)).intersection(meter_dates)
    )
    if all_openmeteo_dates:
        # Open the chart on today's forecast when it is available, so the forecast
        # trace and full-day estimate are visible without needing to change dates.
        forecast_dates_from_today = [date for date in openmeteo_forecast_dates if date >= today]
        if forecast_dates_from_today:
            default_openmeteo_date = forecast_dates_from_today[0]
        elif openmeteo_forecast_dates:
            default_openmeteo_date = openmeteo_forecast_dates[-1]
        elif openmeteo_historical_forecast_dates:
            historical_dates_to_today = [
                date for date in openmeteo_historical_forecast_dates if date <= today
            ]
            default_openmeteo_date = (
                historical_dates_to_today[-1]
                if historical_dates_to_today else openmeteo_historical_forecast_dates[-1]
            )
        elif default_openmeteo_dates:
            default_openmeteo_date = default_openmeteo_dates[-1]
        else:
            default_openmeteo_date = complete_openmeteo_dates[-1] if complete_openmeteo_dates else all_openmeteo_dates[-1]
        openmeteo_chosen_date_value = half_width_date_picker_with_arrows(
            "Open-Meteo comparison date",
            value=pd.Timestamp(default_openmeteo_date).date(),
            min_value=pd.Timestamp(all_openmeteo_dates[0]).date(),
            max_value=pd.Timestamp(all_openmeteo_dates[-1]).date(),
            key="openmeteo_comparison_date_picker",
        )
        openmeteo_chosen_date = openmeteo_chosen_date_value.isoformat()
        st.session_state["openmeteo_chosen_date"] = openmeteo_chosen_date
    else:
        openmeteo_chosen_date = None
        st.session_state["openmeteo_chosen_date"] = None
        st.info("No Open-Meteo archive or forecast data is saved yet. Refresh it from the sidebar.")
    openmeteo_intraday = (
        openmeteo_hourly[openmeteo_hourly["local_date"] == openmeteo_chosen_date].copy()
        if openmeteo_chosen_date is not None else pd.DataFrame()
    )
    openmeteo_forecast_intraday = (
        openmeteo_forecast_hourly[openmeteo_forecast_hourly["local_date"] == openmeteo_chosen_date].copy()
        if openmeteo_chosen_date is not None and not openmeteo_forecast_hourly.empty else pd.DataFrame()
    )
    openmeteo_historical_forecast_intraday = (
        openmeteo_historical_forecast_hourly[
            openmeteo_historical_forecast_hourly["local_date"] == openmeteo_chosen_date
        ].copy()
        if (
            openmeteo_chosen_date is not None
            and openmeteo_chosen_date >= "2026-10-01"
            and not openmeteo_historical_forecast_hourly.empty
        )
        else pd.DataFrame()
    )
    meter_openmeteo_intraday = (
        power_actuals[power_actuals["local_date"] == openmeteo_chosen_date].copy()
        if openmeteo_chosen_date is not None and not power_actuals.empty else pd.DataFrame()
    )
    openmeteo_aligned_to_actuals = pd.DataFrame()
    openmeteo_forecast_comparison = (
        openmeteo_forecast_intraday
        if not openmeteo_forecast_intraday.empty
        else openmeteo_historical_forecast_intraday
    )
    openmeteo_forecast_source = (
        "weather forecast"
        if not openmeteo_forecast_intraday.empty
        else "historical forecast"
    )
    if not openmeteo_forecast_comparison.empty and not meter_openmeteo_intraday.empty:
        weather_power = openmeteo_forecast_comparison.set_index("time_local")[[
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

    # Compare forecast and measured energy over the same five-minute Sigen samples.
    comparison_weather = openmeteo_aligned_to_actuals

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

    full_day_source = None
    full_day_frame = openmeteo_forecast_comparison
    if not full_day_frame.empty:
        full_day_source = openmeteo_forecast_source.title()
    else:
        full_day_frame = openmeteo_intraday
        if not full_day_frame.empty:
            full_day_source = "Satellite archive"

    full_day_estimate_kwh = None
    openmeteo_generated_date = None
    if len(full_day_frame) > 1:
        full_day_frame = full_day_frame.sort_values("time_local")
        interval_hours = (
            full_day_frame["time_local"].diff().dt.total_seconds().median() / 3600
        )
        if pd.notna(interval_hours) and interval_hours > 0:
            sample_times = pd.to_datetime(full_day_frame["time_local"], utc=True)
            sample_ends = sample_times + pd.to_timedelta(interval_hours, unit="h")
            if intervals_cover_full_local_day(
                sample_ends, [interval_hours] * len(sample_ends), openmeteo_chosen_date,
            ):
                full_day_estimate_kwh = (
                    pd.to_numeric(full_day_frame["openmeteo_power_kw"], errors="coerce").sum()
                    * interval_hours
                )
                if "fetched_at_utc" in full_day_frame:
                    openmeteo_generated_date = generated_date_label(full_day_frame["fetched_at_utc"])

    metric_cols = st.columns(4)
    metric_cols[0].metric(
        (f"Full-day Open-Meteo {full_day_source.lower()}" if full_day_source else "Full-day Open-Meteo estimate")
        + (f" · {openmeteo_generated_date}" if openmeteo_generated_date else ""),
        f"{full_day_estimate_kwh:.2f} kWh" if full_day_estimate_kwh is not None else "—",
        help="Full-day energy from the historical forecast, live forecast, or satellite archive. Shows a dash if any part of the selected local day is missing.",
    )
    metric_cols[1].metric(
        "Open-Meteo forecast energy (matched periods)",
        f"{openmeteo_estimate_kwh:.2f} kWh" if openmeteo_estimate_kwh is not None else "—",
        help="Forecast energy interpolated to the five-minute Sigen timestamps available for the selected date.",
    )
    metric_cols[2].metric(
        "Sigen actual energy (matched periods)",
        f"{meter_comparison_kwh:.2f} kWh" if meter_comparison_kwh is not None else "—",
        help="Meter energy over the same five-minute periods as the interpolated Open-Meteo forecast.",
    )
    metric_cols[3].metric(
        "Open-Meteo estimate accuracy",
        f"{accuracy_score:.1f}%" if accuracy_score is not None else "—",
        help="100% minus absolute percentage error against Sigen energy over forecast-matched periods, floored at 0%.",
    )
    if comparison_intervals:
        st.caption(f"Daily Open-Meteo forecast comparison uses {comparison_intervals:,} five-minute Sigen intervals with interpolated forecast data.")
    elif meter_openmeteo_intraday.empty:
        st.caption("No Sigen power actuals are available for this date, so the energy comparison and accuracy score are unavailable.")
    else:
        st.caption("No matching Open-Meteo forecast and Sigen timestamps are available for this date, so the energy comparison and accuracy score are unavailable.")

    forecast_frequency_source = (
        openmeteo_forecast_comparison
        if not openmeteo_forecast_comparison.empty
        else openmeteo_intraday
    )
    sigen_30min_actuals = (
        resample_meter_power_to_30min(meter_openmeteo_intraday, openmeteo_chosen_date)
        if not meter_openmeteo_intraday.empty and not forecast_frequency_source.empty
        else pd.DataFrame(columns=["time_local", "Solar Production (kW)"])
    )

    fig3 = go.Figure()
    if not meter_openmeteo_intraday.empty:
        fig3.add_trace(go.Scatter(
            x=meter_openmeteo_intraday["period_end_local"],
            y=meter_openmeteo_intraday["Solar Production (kW)"],
            mode="lines", name="Sigen meter actual power (kW)",
            line=dict(width=2, color="#83C9FF"),
            hovertemplate="%{x|%H:%M}<br>Sigen meter actual: %{y:.3f} kW<extra></extra>"
        ))
    if not sigen_30min_actuals.empty:
        fig3.add_trace(go.Scatter(
            x=sigen_30min_actuals["time_local"],
            y=sigen_30min_actuals["Solar Production (kW)"],
            mode="lines", connectgaps=False,
            name="Sigen meter actual power (30-minute mean)",
            line=dict(width=2, color="#83C9FF", dash="dash"),
            hovertemplate=(
                "%{x|%H:%M}<br>Sigen meter 30-minute mean: "
                "%{y:.3f} kW<extra></extra>"
            ),
        ))
    if not openmeteo_intraday.empty:
        # Preserve the satellite archive's native time steps and short-term variation.
        plotted_weather = openmeteo_intraday
        fig3.add_trace(go.Scatter(
            x=plotted_weather["time_local"], y=plotted_weather["openmeteo_power_kw"],
            mode="lines", name="Open-Meteo satellite estimate (kW)",
            line=dict(width=2, color="#7b61a8"),
            customdata=plotted_weather["weighted_panel_irradiance"],
            hovertemplate="%{x|%H:%M}<br>Open-Meteo estimate: %{y:.2f} kW"
                          "<br>16:4 north/east weighted irradiance: %{customdata:.0f} W/m²<extra></extra>"
        ))
    if not openmeteo_forecast_comparison.empty:
        # Keep the selected forecast visible even when Sigen actuals cover only part of today.
        plotted_forecast = openmeteo_forecast_comparison
        forecast_label = (
            "historical forecast"
            if openmeteo_forecast_intraday.empty
            else "weather forecast"
        )
        fig3.add_trace(go.Scatter(
            x=plotted_forecast["time_local"], y=plotted_forecast["openmeteo_power_kw"],
            mode="lines", name=f"Open-Meteo {forecast_label} estimate (kW)",
            line=dict(width=2, color="#7b61a8", dash="dash"),
            customdata=plotted_forecast["weighted_panel_irradiance"],
            hovertemplate=f"%{{x|%H:%M}}<br>Open-Meteo {forecast_label}: %{{y:.2f}} kW"
                          "<br>16:4 north/east weighted irradiance: %{customdata:.0f} W/m²<extra></extra>"
        ))
    if (meter_openmeteo_intraday.empty and openmeteo_intraday.empty
            and openmeteo_forecast_intraday.empty and openmeteo_historical_forecast_intraday.empty):
        st.info("No Sigen, Open-Meteo, or Solcast forecast data is saved for this date.")
    if openmeteo_chosen_date is not None:
        chart_day_start = pd.Timestamp(openmeteo_chosen_date).tz_localize(TZ_NAME)
        chart_day_end = chart_day_start + pd.DateOffset(days=1)
        fig3.update_xaxes(range=[chart_day_start, chart_day_end], fixedrange=True)
    fig3.update_layout(xaxis_title="Time (Sydney)", yaxis_title="Power (kW)",
                       margin=dict(l=10, r=10, t=20, b=10), hovermode="closest")
    place_legend_below_chart(fig3)
    st.plotly_chart(fig3, width="stretch")

    st.markdown("**Open-Meteo accuracy against Sigen actuals**")
    openmeteo_accuracy_rows = []
    score = power_accuracy_metrics(
        "Open-Meteo satellite estimate", openmeteo_intraday, "time_local", "openmeteo_power_kw",
        meter_openmeteo_intraday, None,
    )
    if score:
        openmeteo_accuracy_rows.append(score)
    if not openmeteo_historical_forecast_intraday.empty:
        score = power_accuracy_metrics(
            "Open-Meteo historical forecast", openmeteo_historical_forecast_intraday,
            "time_local", "openmeteo_power_kw", meter_openmeteo_intraday, None,
        )
        if score:
            openmeteo_accuracy_rows.append(score)
    if not openmeteo_forecast_intraday.empty:
        score = power_accuracy_metrics(
            "Open-Meteo weather forecast", openmeteo_forecast_intraday,
            "time_local", "openmeteo_power_kw", meter_openmeteo_intraday, 0.25,
        )
        if score:
            openmeteo_accuracy_rows.append(score)
    if openmeteo_accuracy_rows:
        st.dataframe(pd.DataFrame(openmeteo_accuracy_rows), width="stretch", hide_index=True)
        st.caption("MAE and RMSE show typical point error; positive bias and energy error mean over-prediction. Scores use matched timestamps only.")
    else:
        st.caption("Accuracy scores appear when Sigen actuals overlap the selected Open-Meteo date.")

    openmeteo_lead_rows = openmeteo_forecast_snapshots[
        openmeteo_forecast_snapshots["local_date"] == openmeteo_chosen_date
    ] if not openmeteo_forecast_snapshots.empty else pd.DataFrame()
    openmeteo_lead_scores = forecast_lead_time_scores(
        openmeteo_lead_rows, "time_utc", "openmeteo_power_kw", meter_openmeteo_intraday, 0.25,
    )
    if not openmeteo_lead_scores.empty:
        with st.expander("Open-Meteo forecast accuracy by lead time"):
            st.dataframe(openmeteo_lead_scores, width="stretch", hide_index=True)
            st.caption("Each saved 15-minute forecast interval is scored at its original issue time, grouped by time until the interval starts.")


_render_openmeteo_power_by_time()

@st.fragment
@log_performance
def render_forecast_comparison(
    history,
    latest,
    openmeteo_forecast_hourly,
    openmeteo_historical_forecast_hourly,
    openmeteo_chosen_date,
):
    st.subheader("Solcast vs Open-Meteo forecast power")
    st.caption(
        "Both forecasts are shown in kW for the same Sydney date. Missing Solcast "
        "points are filled from the newest earlier saved forecast that has that timestamp."
    )
    solcast_forecast_dates = set(history["local_date"])
    openmeteo_forecast_date_set = (
        set(openmeteo_forecast_hourly["local_date"])
        if not openmeteo_forecast_hourly.empty else set()
    )
    openmeteo_historical_forecast_date_set = (
        set(openmeteo_historical_forecast_hourly["local_date"])
        if not openmeteo_historical_forecast_hourly.empty else set()
    )
    forecast_comparison_dates = sorted(
        solcast_forecast_dates.intersection(
            openmeteo_forecast_date_set.union(openmeteo_historical_forecast_date_set)
        )
    )
    if not forecast_comparison_dates:
        st.info(
            "No dates are available in both the latest Solcast and Open-Meteo forecasts."
        )
        return

    comparison_default = (
        openmeteo_chosen_date if openmeteo_chosen_date in forecast_comparison_dates
        else forecast_comparison_dates[-1]
    )
    comparison_date = half_width_date_picker_with_arrows(
        "Forecast comparison date", pd.Timestamp(comparison_default).date(),
        min_value=pd.Timestamp(forecast_comparison_dates[0]).date(),
        max_value=pd.Timestamp(forecast_comparison_dates[-1]).date(),
        key="forecast_comparison_date",
    ).isoformat()
    solcast_comparison = solcast_comparison_forecast(
        history, latest, comparison_date,
    )
    openmeteo_comparison_source = (
        openmeteo_historical_forecast_hourly
        if comparison_date in openmeteo_historical_forecast_date_set
        else openmeteo_forecast_hourly
    )
    openmeteo_comparison = openmeteo_comparison_source[
        openmeteo_comparison_source["local_date"] == comparison_date
    ].sort_values("time_local")
    comparison_fig = go.Figure()
    comparison_fig.add_trace(go.Scatter(
        x=solcast_comparison["period_end_local"], y=solcast_comparison["pv_estimate"],
        customdata=solcast_comparison["forecast_source"],
        mode="lines", name="Solcast forecast power (kW)",
        line=dict(width=2, color="#636EFA", dash="dot"),
        hovertemplate="%{x|%H:%M}<br>Solcast forecast: %{y:.2f} kW<br>%{customdata}<extra></extra>",
    ))
    comparison_fig.add_trace(go.Scatter(
        x=openmeteo_comparison["time_local"], y=openmeteo_comparison["openmeteo_power_kw"],
        mode="lines", name=(
            "Open-Meteo historical forecast power (kW)"
            if comparison_date in openmeteo_historical_forecast_date_set
            else "Open-Meteo forecast power (kW)"
        ),
        line=dict(width=2, color="#7b61a8", dash="dot"),
        hovertemplate="%{x|%H:%M}<br>Open-Meteo forecast: %{y:.2f} kW<extra></extra>",
    ))
    comparison_fig.update_layout(
        xaxis_title="Time (Sydney)", yaxis_title="Forecast power (kW)",
        margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified",
    )
    place_legend_below_chart(comparison_fig)
    comparison_fig.update_xaxes(
        range=[pd.Timestamp(comparison_date).tz_localize(TZ_NAME),
               pd.Timestamp(comparison_date).tz_localize(TZ_NAME) + pd.DateOffset(days=1)]
    )
    st.plotly_chart(comparison_fig, width="stretch")


render_forecast_comparison(
    history,
    latest,
    openmeteo_forecast_hourly,
    openmeteo_historical_forecast_hourly,
    st.session_state.get("openmeteo_chosen_date"),
)

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
    marker_color="#E76F51",
    hovertemplate="%{x}<br>Open-Meteo daylight within Sigen data coverage: %{y:.2f} h<extra></extra>",
))
sunshine_fig.update_layout(
    barmode="group", xaxis_title="Date (Sydney)", yaxis_title="Hours",
    margin=dict(l=10, r=10, t=20, b=10), xaxis_tickangle=-35,
)
place_legend_below_chart(sunshine_fig)
st.plotly_chart(sunshine_fig, width="stretch")

@st.fragment
@log_performance
def _render_solcast_forecast_revisions():
    st.subheader("Solcast forecast revisions")
    if history.empty:
        st.info("Solcast forecast revision history starts after the next forecast download.")
    else:
        solcast_revision_counts = history.groupby("local_date")["downloaded_at_utc"].nunique()
        solcast_revision_dates = sorted(
            solcast_revision_counts[solcast_revision_counts >= 2].index
        )
        if not solcast_revision_dates:
            solcast_snapshot_count = history["downloaded_at_utc"].nunique()
            st.info(
                f"Saved {solcast_snapshot_count} Solcast forecast run. "
                "Refresh again to compare revisions for a day."
            )
        else:
            solcast_revision_default = (
                today if today in solcast_revision_dates else solcast_revision_dates[-1]
            )
            solcast_revision_date = half_width_date_picker_with_arrows(
                "Solcast forecast revision date", pd.Timestamp(solcast_revision_default).date(),
                min_value=pd.Timestamp(solcast_revision_dates[0]).date(),
                max_value=pd.Timestamp(solcast_revision_dates[-1]).date(),
                key="solcast_revision_date",
            ).isoformat()
            solcast_revision_rows = history[
                history["local_date"] == solcast_revision_date
            ].copy()
            solcast_run_stamps = sorted(
                solcast_revision_rows["downloaded_at_utc"].dropna().unique()
            )
            solcast_total_revision_count = len(solcast_run_stamps)
            if solcast_total_revision_count > 10:
                solcast_run_stamps = solcast_run_stamps[-10:]
                st.caption(
                    f"Showing the 10 most recent of {solcast_total_revision_count} "
                    "saved Solcast forecasts for this day."
                )
            else:
                st.caption(
                    f"Comparing {solcast_total_revision_count} saved Solcast forecasts "
                    "for this day; each line is labeled by fetch time."
                )
            solcast_revision_fig = go.Figure()
            for run_index, run_stamp in enumerate(solcast_run_stamps):
                run_rows = solcast_revision_rows[
                    solcast_revision_rows["downloaded_at_utc"] == run_stamp
                ].sort_values("period_end_local")
                run_local_time = pd.to_datetime(run_stamp, utc=True).tz_convert(TZ_NAME)
                is_latest = run_index == len(solcast_run_stamps) - 1
                solcast_revision_fig.add_trace(go.Scatter(
                    x=run_rows["period_end_local"], y=run_rows["pv_estimate"],
                    mode="lines",
                    name=f"Fetched {run_local_time.strftime('%d %b %H:%M')}",
                    line=dict(
                        color=px.colors.qualitative.Plotly[
                            run_index % len(px.colors.qualitative.Plotly)
                        ],
                        width=2.5 if is_latest else 1.5,
                        dash="solid" if is_latest else "dash",
                    ),
                    customdata=run_rows["pv_estimate10"],
                    hovertemplate=(
                        "%{x|%H:%M}<br>Forecast: %{y:.2f} kW"
                        "<br>P10: %{customdata:.2f} kW<extra>%{fullData.name}</extra>"
                    ),
                ))
            solcast_revision_day_start = pd.Timestamp(
                solcast_revision_date
            ).tz_localize(TZ_NAME)
            solcast_revision_fig.update_layout(
                xaxis_title="Time (Sydney)", yaxis_title="Forecast power (kW)",
                margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified",
            )
            solcast_revision_fig.update_xaxes(
                range=[solcast_revision_day_start,
                       solcast_revision_day_start + pd.DateOffset(days=1)],
                fixedrange=True,
            )
            place_legend_below_chart(solcast_revision_fig)
            st.plotly_chart(solcast_revision_fig, width="stretch")


_render_solcast_forecast_revisions()

@st.fragment
@log_performance
def _render_openmeteo_forecast_revisions():
    st.subheader("Open-Meteo forecast revisions")
    if openmeteo_forecast_snapshots.empty:
        st.info("Forecast revision history starts after the next Open-Meteo refresh.")
    else:
        revision_counts = openmeteo_forecast_snapshots.groupby("local_date")["fetched_at_utc"].nunique()
        revision_dates = sorted(revision_counts[revision_counts >= 2].index)
        if not revision_dates:
            snapshot_count = openmeteo_forecast_snapshots["fetched_at_utc"].nunique()
            st.info(
                f"Saved {snapshot_count} Open-Meteo forecast run. Refresh again to compare revisions for a day."
            )
        else:
            revision_default = today if today in revision_dates else revision_dates[-1]
            revision_date = half_width_date_picker_with_arrows(
                "Open-Meteo forecast revision date", pd.Timestamp(revision_default).date(),
                min_value=pd.Timestamp(revision_dates[0]).date(),
                max_value=pd.Timestamp(revision_dates[-1]).date(),
                key="openmeteo_revision_date",
            ).isoformat()
            revision_day_rows = openmeteo_forecast_snapshots[
                openmeteo_forecast_snapshots["local_date"] == revision_date
            ].copy()
            run_stamps = sorted(revision_day_rows["fetched_at_utc"].dropna().unique())
            total_revision_count = len(run_stamps)
            if total_revision_count > 10:
                run_stamps = run_stamps[-10:]
                st.caption(f"Showing the 10 most recent of {total_revision_count} saved forecasts for this day.")
            else:
                st.caption(f"Comparing {total_revision_count} saved forecasts for this day; each line is labeled by fetch time.")
            revision_fig = go.Figure()
            for run_index, run_stamp in enumerate(run_stamps):
                run_rows = revision_day_rows[
                    revision_day_rows["fetched_at_utc"] == run_stamp
                ].sort_values("time_local")
                run_local_time = pd.to_datetime(run_stamp, utc=True).tz_convert(TZ_NAME)
                is_latest = run_index == len(run_stamps) - 1
                revision_fig.add_trace(go.Scatter(
                    x=run_rows["time_local"], y=run_rows["openmeteo_power_kw"],
                    mode="lines",
                    name=f"Fetched {run_local_time.strftime('%d %b %H:%M')}",
                    line=dict(
                        color=px.colors.qualitative.Plotly[run_index % len(px.colors.qualitative.Plotly)],
                        width=2.5 if is_latest else 1.5,
                        dash="solid" if is_latest else "dash",
                    ),
                    customdata=run_rows["weighted_panel_irradiance"],
                    hovertemplate=(
                        "%{x|%H:%M}<br>Forecast: %{y:.2f} kW"
                        "<br>Weighted irradiance: %{customdata:.0f} W/m²<extra>%{fullData.name}</extra>"
                    ),
                ))
            revision_day_start = pd.Timestamp(revision_date).tz_localize(TZ_NAME)
            revision_fig.update_layout(
                xaxis_title="Time (Sydney)", yaxis_title="Forecast power (kW)",
                margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified",
            )
            revision_fig.update_xaxes(
                range=[revision_day_start, revision_day_start + pd.DateOffset(days=1)],
                fixedrange=True,
            )
            place_legend_below_chart(revision_fig)
            st.plotly_chart(revision_fig, width="stretch")

_render_openmeteo_forecast_revisions()
