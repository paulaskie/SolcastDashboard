Warning: truncated output (original token count: 23627)
Total output lines: 1957

import os
import sqlite3
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
    fetch_estimated_actuals,
    fetch_forecast,
    parse_period_hours,
)

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))
PV_SYSTEM_CAPACITY_KW = 9.5
OPENMETEO_DEFAULT_PERFORMANCE_RATIO = 0.80
OPENMETEO_CALIBRATION_DATE = "2026-10-09"

st.set_page_config(page_title="Solar Forecast", page_icon="☀️", layout="wide")

def connect_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    return conn

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
    return downloaded_at

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

def load_latest_actuals():
    conn = connect_db()
    actuals = pd.read_sql_query("SELECT * FROM solcast_estimate_30min ORDER BY downloaded_at_utc, period_end_utc", conn)
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
    hist = pd.read_sql_query("SELECT * FROM solcast_forecast_30min ORDER BY downloaded_at_utc, period_end_utc", conn)
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
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east "
            "FROM openmeteo_estimate_hourly "
            "WHERE location_key = (SELECT location_key FROM openmeteo_estimate_hourly "
            "ORDER BY fetched_at_utc DESC LIMIT 1) ORDER BY time_utc",
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
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

def load_openmeteo_forecast_snapshots():
    conn = connect_db()
    try:
        weather = pd.read_sql_query(
            "SELECT time_utc, time_local, fetched_at_utc, global_tilted_irradiance, "
            "global_tilted_irradiance_east FROM openmeteo_forecast_snapshots "
            "ORDER BY fetched_at_utc, time_utc",
            conn,
        )
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

def load_openmeteo_historical_forecast_hourly():
    conn = connect_db()
    try:
        weather = pd.read_sql_query(
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
    except sqlite3.OperationalError:
        return pd.DataFrame()
    finally:
        conn.close()
    return prepare_openmeteo_estimates(weather)

def load_openmeteo_daily():
    conn = connect_db()
    daily_frames = []
    for table in ("openmeteo_estimate_daily", "openmeteo_forecast_daily"):
        try:
            weather_daily = pd.read_sql_query(
                f"SELECT date_local, sunrise_local, sunset_local, daylight_duration FROM {table} "
                f"WHERE location_key = (SELECT location_key FROM {table} "
                "ORDER BY fetched_at_utc DESC LIMIT 1)",
                conn,
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
    calibration = calibration[
        calibration["time_local"].dt.date == pd.Timestamp(OPENMETEO_CALIBRATION_DATE).date()
    ]
    day_count = calibration["time_local"].dt.date.nunique()
    if len(calibration) < 50 or day_count != 1:
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
                INSERT INTO sigen_energy_actuals
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

def load_meter_actuals():
    conn = connect_db()
    energy = pd.read_sql_query("SELECT period_end_utc, energy_kwh FROM sigen_energy_actuals ORDER BY period_end_utc", conn)
    power = pd.read_sql_query("SELECT period_end_utc, power_kw FROM sigen_power_actuals ORDER BY period_end_utc", conn)
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
        openmeteo_start_date = date_picker_with_arrows(
            "Archive start date", value=openmeteo_default_start_date,
            key="openmeteo_archive_start_date", max_value=openmeteo_latest_archive_date,
        )
        openmeteo_end_date = date_picker_with_arrows(
            "Archive end date", value=openmeteo_latest_archive_date,
            key="openmeteo_archive_end_date", min_value=openmeteo_start_date,
            max_value=openmeteo_latest_archive_date,
        )
        openmeteo_refresh_clicked = st.button(
            "Refresh archive and forecast", width="stretch",
            help="Refreshes the selected satellite archive and historical forecast dates, then fetches a 7-day weather forecast.",
        )

    with st.expander("Sigen actuals"):
        st.caption(f"Station {SIGEN_STATION_ID}")
        sigen_start_date = date_picker_with_arrows(
            "Start date", value=pd.Timestamp("2026-10-01").date(), key="sigen_start_date",
        )
        sigen_end_date = date_picker_with_arrows(
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
        amber_date = date_picker_with_arrows(
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

if amber_update_actuals_clicked:
    try:
        with st.spinner("Updating Amber actual prices through now…"):
            amber_updated_count, rate_limit_remaining = refresh_amber_actuals_since_last_current(
                amber_api_key, include_rate_limit=True,
            )
        st.session_state["amber_download_message"] = (
            f"Updated {amber_updated_count:,} actual Amber price intervals since the last CurrentInterval."
        )
        st.session_state["amber_rate_limit_message"] = amber_rate_limit_message(
            prices=rate_limit_remaining,
        )
        st.rerun()
    except requests.RequestException as e:
        show_amber_rate_limit_from_error(e)
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if amber_48_clicked:
    try:
        with st.spinner("Fetching the next 48 five-minute periods from Amber…"):
            amber_48_rows, rate_limit_remaining = fetch_amber_current_prices(
                amber_api_key, next_intervals=48, include_rate_limit=True,
            )
            save_amber_prices(amber_48_rows)
        forecast_count = sum(1 for row in amber_48_rows if row.get("type") == "ForecastInterval")
        st.session_state["amber_download_message"] = (
            f"Saved {forecast_count} forecast intervals from Amber's next-48-period current-prices feed."
        )
        st.session_state["amber_rate_limit_message"] = amber_rate_limit_message(
            current_prices=rate_limit_remaining,
        )
        st.rerun()
    except requests.RequestException as e:
        show_amber_rate_limit_from_error(e)
        st.error(f"Could not contact Amber: {e}")
    except Exception as e:
        st.error(str(e))

if sigen_update_clicked:
    try:
        with st.spinner("Updating meter actuals from Sigen Open Platform…"):
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
            stamp = save_forecast(fresh, payload)
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
        st.session_state["last_download_message"] = f"Saved {len(fresh)} forecast intervals and {len(actuals)} estimated actual intervals at {stamp} UTC."
        st.session_state["solcast_rate_limit_message"] = solcast_rate_limit_message(
            forecast=forecast_remaining, estimated_actuals=actuals_remaining,
        )
        if actuals_error:
            st.session_state["actuals_download_error"] = actuals_error
        else:
            st.session_s…3627 tokens truncated…l_date", "Sigen_nonzero_hours", "Sigen_daylight_hours_covered"]],
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
place_legend_below_chart(fig)
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
place_legend_below_chart(sunshine_fig)
st.plotly_chart(sunshine_fig, width="stretch")

st.subheader("Solcast Forecast and actual power by time")
available_dates = sorted(set(latest["local_date"])
                         .union(actuals["local_date"] if not actuals.empty else [])
                         .union(power_actuals["local_date"] if not power_actuals.empty else []))
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
def format_kwh(value):
    return f"{value:.3f} kWh" if pd.notna(value) else "—"

def generated_date_label(timestamps):
    parsed = pd.to_datetime(pd.Series(timestamps), utc=True, errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().tz_convert(TZ_NAME).strftime("%d %b %Y")

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
    "Solcast estimated actual energy",
    f"{solcast_estimate_kwh:.2f} kWh" if solcast_estimate_kwh is not None else "—",
    help="Estimated energy over five-minute Sigen timestamps that have matching Solcast estimated-actual data.",
)
solcast_metric_cols[2].metric(
    "Sigen actual energy (matched periods)",
    f"{solcast_meter_kwh:.2f} kWh" if solcast_meter_kwh is not None else "—",
    help="Meter energy over the same matched five-minute periods as the Solcast estimate.",
)
solcast_metric_cols[3].metric(
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
solcast_chart_forecast = solcast_comparison_forecast(history, latest, chosen_date)
if not solcast_chart_forecast.empty:
    fig2.add_trace(go.Scatter(
        x=solcast_chart_forecast["period_end_local"], y=solcast_chart_forecast["pv_estimate"],
        mode="lines", name="Solcast forecast power (kW)",
        line=dict(width=2, color="#7b61a8", dash="dot"),
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
        line=dict(width=2, color="#2a9d8f"),
        hovertemplate="%{x|%H:%M}<br>Meter actual power: %{y:.3f} kW<extra></extra>"
    ))
meter_power_30min_solcast = resample_meter_power_to_30min(meter_power_intraday, chosen_date)
if not meter_power_30min_solcast.empty:
    fig2.add_trace(go.Scatter(
        x=meter_power_30min_solcast["time_local"],
        y=meter_power_30min_solcast["Solar Production (kW)"],
        mode="lines", connectgaps=False,
        name="Sigen meter actual power (30-minute mean)",
        line=dict(width=2, color="#006d77", dash="dash"),
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

st.subheader("Open-Meteo estimated and actual power by time")
if performance_ratio_samples >= 50 and performance_ratio_days == 1:
    performance_ratio_caption = (
        f"a calibrated {openmeteo_performance_ratio:.3f} ratio from "
        f"{performance_ratio_samples:,} daylight samples on {OPENMETEO_CALIBRATION_DATE}"
    )
else:
    performance_ratio_caption = (
        f"the default {OPENMETEO_DEFAULT_PERFORMANCE_RATIO:.2f} ratio "
        f"(need 50 matching daylight samples on {OPENMETEO_CALIBRATION_DATE} to calibrate)"
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
openmeteo_historical_forecast_intraday = (
    openmeteo_historical_forecast_hourly[
        openmeteo_historical_forecast_hourly["local_date"] == openmeteo_chosen_date
    ].copy()
    if openmeteo_chosen_date is not None and not openmeteo_historical_forecast_hourly.empty
    else pd.DataFrame()
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
forecast_weather_intraday = (
    openmeteo_historical_forecast_intraday
    if not openmeteo_historical_forecast_intraday.empty
    else openmeteo_forecast_intraday
)
if comparison_weather.empty and not forecast_weather_intraday.empty and not meter_openmeteo_intraday.empty:
    forecast_power = forecast_weather_intraday.set_index("time_local")[["openmeteo_power_kw"]].sort_index()
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

full_day_source = None
full_day_frame = openmeteo_forecast_intraday
if not full_day_frame.empty:
    full_day_source = "Forecast"
else:
    full_day_frame = openmeteo_intraday
    if not full_day_frame.empty:
        full_day_source = "Satellite archive"
    else:
        full_day_frame = openmeteo_historical_forecast_intraday
        if not full_day_frame.empty:
            full_day_source = "Historical forecast"

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
    "Open-Meteo estimated energy",
    f"{openmeteo_estimate_kwh:.2f} kWh" if openmeteo_estimate_kwh is not None else "—",
    help="Estimated energy over Sigen timestamps that have matching Open-Meteo data.",
)
metric_cols[2].metric(
    "Sigen actual energy (matched periods)",
    f"{meter_comparison_kwh:.2f} kWh" if meter_comparison_kwh is not None else "—",
    help="Meter energy over the same matched five-minute periods as the Open-Meteo estimate.",
)
metric_cols[3].metric(
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

forecast_frequency_source = (
    openmeteo_forecast_intraday
    if not openmeteo_forecast_intraday.empty
    else openmeteo_historical_forecast_intraday
    if not openmeteo_historical_forecast_intraday.empty
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
        line=dict(width=2, color="#2a9d8f"),
        hovertemplate="%{x|%H:%M}<br>Sigen meter actual: %{y:.3f} kW<extra></extra>"
    ))
if not sigen_30min_actuals.empty:
    fig3.add_trace(go.Scatter(
        x=sigen_30min_actuals["time_local"],
        y=sigen_30min_actuals["Solar Production (kW)"],
        mode="lines", connectgaps=False,
        name="Sigen meter actual power (30-minute mean)",
        line=dict(width=2, color="#006d77", dash="dash"),
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
        line=dict(width=2, color="#f2a541"),
        customdata=plotted_weather["weighted_panel_irradiance"],
        hovertemplate="%{x|%H:%M}<br>Open-Meteo estimate: %{y:.2f} kW"
                      "<br>16:4 north/east weighted irradiance: %{customdata:.0f} W/m²<extra></extra>"
    ))
if not openmeteo_historical_forecast_intraday.empty:
    plotted_historical_forecast = openmeteo_historical_forecast_intraday
    fig3.add_trace(go.Scatter(
        x=plotted_historical_forecast["time_local"],
        y=plotted_historical_forecast["openmeteo_power_kw"],
        mode="lines", name="Open-Meteo historical forecast estimate (kW)",
        line=dict(width=2, color="#4c78a8", dash="dash"),
        customdata=plotted_historical_forecast["weighted_panel_irradiance"],
        hovertemplate=(
            "%{x|%H:%M}<br>Open-Meteo historical forecast: %{y:.2f} kW"
            "<br>16:4 north/east weighted irradiance: %{customdata:.0f} W/m²<extra></extra>"
        ),
    ))
if (not openmeteo_forecast_intraday.empty
        and (openmeteo_chosen_date >= today or openmeteo_historical_forecast_intraday.empty)):
    # Keep every forecast hour visible; Sigen actuals can cover only part of today.
    plotted_forecast = openmeteo_forecast_intraday
    fig3.add_trace(go.Scatter(
        x=plotted_forecast["time_local"], y=plotted_forecast["openmeteo_power_kw"],
        mode="lines", name="Open-Meteo weather forecast estimate (kW)",
        line=dict(width=2, color="#7b61a8", dash="dash"),
        customdata=plotted_forecast["weighted_panel_irradiance"],
        hovertemplate="%{x|%H:%M}<br>Open-Meteo forecast estimate: %{y:.2f} kW"
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
        revision_date = st.selectbox(
            "Forecast date with multiple saved runs", revision_dates,
            index=revision_dates.index(revision_default),
            format_func=lambda value: pd.Timestamp(value).strftime("%d %b %Y"),
            key="openmeteo_revision_date",
        )
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

st.subheader("Solcast vs Open-Meteo forecast power")
st.caption("Both forecasts are shown in kW for the same Sydney date. Missing Solcast points are filled from the newest earlier saved forecast that has that timestamp.")
solcast_forecast_dates = set(history["local_date"])
openmeteo_forecast_date_set = set(openmeteo_forecast_hourly["local_date"]) if not openmeteo_forecast_hourly.empty else set()
openmeteo_historical_forecast_date_set = (
    set(openmeteo_historical_forecast_hourly["local_date"])
    if not openmeteo_historical_forecast_hourly.empty else set()
)
forecast_comparison_dates = sorted(
    solcast_forecast_dates.intersection(
        openmeteo_forecast_date_set.union(openmeteo_historical_forecast_date_set)
    )
)
if forecast_comparison_dates:
    comparison_default = (
        openmeteo_chosen_date if openmeteo_chosen_date in forecast_comparison_dates
        else forecast_comparison_dates[-1]
    )
    comparison_date = half_width_date_picker_with_arrows(
        "Forecast comparison date", pd.Timestamp(comparison_default).date(),
        min_value=pd.Timestamp(forecast_comparison_dates[0]).date(),
        max_value=pd.Timestamp(forecast_comparison_dates[-1]).date(),
        key="forecast_comparison_date",
    )
    comparison_date = comparison_date.isoformat()
    solcast_comparison = solcast_comparison_forecast(history, latest, comparison_date)
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
        line=dict(width=2, color="#7b61a8", dash="dot"),
        hovertemplate="%{x|%H:%M}<br>Solcast forecast: %{y:.2f} kW<br>%{customdata}<extra></extra>",
    ))
    comparison_fig.add_trace(go.Scatter(
        x=openmeteo_comparison["time_local"], y=openmeteo_comparison["openmeteo_power_kw"],
        mode="lines", name=(
            "Open-Meteo historical forecast power (kW)"
            if comparison_date in openmeteo_historical_forecast_date_set
            else "Open-Meteo forecast power (kW)"
        ),
        line=dict(width=2, color="#4c78a8", dash="dot"),
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
