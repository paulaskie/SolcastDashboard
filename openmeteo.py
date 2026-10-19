import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import openmeteo_requests

import pandas as pd
import requests_cache
from retry_requests import retry

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SOLCAST_DB_PATH", str(APP_DIR / "solcast_history.sqlite3")))

# Setup the Open-Meteo API client with cache and retry on error
cache_session = requests_cache.CachedSession('.cache', expire_after = 0)
retry_session = retry(cache_session, retries = 5, backoff_factor = 0.2)
openmeteo = openmeteo_requests.Client(session = retry_session)

# Make sure all required weather variables are listed here
# The order of variables in hourly or daily is important to assign them correctly below
url = "https://satellite-api.open-meteo.com/v1/archive"
params = {
	"latitude": -32.9295,
	"longitude": 151.7801,
	"daily": ["sunrise", "sunset", "daylight_duration", "sunshine_duration", "shortwave_radiation_sum"],
	"hourly": ["shortwave_radiation", "direct_radiation", "diffuse_radiation", "direct_normal_irradiance", "shortwave_radiation_clear_sky", "global_tilted_irradiance", "terrestrial_radiation"],
	"models": "jma_jaxa_himawari",
	"timezone": "auto",
	"tilt": 22,
	"azimuth": 180,
	"temporal_resolution": "native",
	"start_date": "2026-10-08",
	# The satellite archive is published with a delay and currently ends at yesterday.
	"end_date": (pd.Timestamp.now(tz="Australia/Sydney").date() - pd.Timedelta(days=1)).isoformat(),
}
responses = openmeteo.weather_api(url, params = params)

# Fetch east-facing tilted irradiance separately; the API accepts one GTI azimuth per request.
east_params = {**params, "hourly": ["global_tilted_irradiance"], "azimuth": -90}
east_response = openmeteo.weather_api(url, params = east_params)[0]
east_hourly = east_response.Hourly()
east_hourly_dataframe = pd.DataFrame({
	"date": pd.date_range(
		start = pd.to_datetime(east_hourly.Time(), unit = "s", utc = True),
		end = pd.to_datetime(east_hourly.TimeEnd(), unit = "s", utc = True),
		freq = pd.Timedelta(seconds = east_hourly.Interval()),
		inclusive = "left",
	).tz_convert(east_response.Timezone().decode()),
	"global_tilted_irradiance_east": east_hourly.Variables(0).ValuesAsNumpy(),
})

# Process first location. Add a for-loop for multiple locations or weather models
response = responses[0]
latitude = response.Latitude()
longitude = response.Longitude()
elevation = response.Elevation()
local_timezone = response.Timezone().decode()
timezone_abbreviation = response.TimezoneAbbreviation().decode()
utc_offset_seconds = response.UtcOffsetSeconds()

# Process hourly data. The order of variables needs to be the same as requested.
hourly = response.Hourly()
hourly_shortwave_radiation = hourly.Variables(0).ValuesAsNumpy()
hourly_direct_radiation = hourly.Variables(1).ValuesAsNumpy()
hourly_diffuse_radiation = hourly.Variables(2).ValuesAsNumpy()
hourly_direct_normal_irradiance = hourly.Variables(3).ValuesAsNumpy()
hourly_shortwave_radiation_clear_sky = hourly.Variables(4).ValuesAsNumpy()
hourly_global_tilted_irradiance = hourly.Variables(5).ValuesAsNumpy()
hourly_terrestrial_radiation = hourly.Variables(6).ValuesAsNumpy()

hourly_data = {
	"date": pd.date_range(
		start = pd.to_datetime(hourly.Time(), unit = "s", utc = True),
		end =  pd.to_datetime(hourly.TimeEnd(), unit = "s", utc = True),
		freq = pd.Timedelta(seconds = hourly.Interval()),
		inclusive = "left"
	).tz_convert(response.Timezone().decode())
}

hourly_data["shortwave_radiation"] = hourly_shortwave_radiation
hourly_data["direct_radiation"] = hourly_direct_radiation
hourly_data["diffuse_radiation"] = hourly_diffuse_radiation
hourly_data["direct_normal_irradiance"] = hourly_direct_normal_irradiance
hourly_data["shortwave_radiation_clear_sky"] = hourly_shortwave_radiation_clear_sky
hourly_data["global_tilted_irradiance"] = hourly_global_tilted_irradiance
hourly_data["terrestrial_radiation"] = hourly_terrestrial_radiation

hourly_dataframe = pd.DataFrame(data = hourly_data)
hourly_dataframe = hourly_dataframe.merge(
	east_hourly_dataframe, on = "date", how = "left", validate = "one_to_one",
)

# Process daily data. The order of variables needs to be the same as requested.
daily = response.Daily()
daily_sunrise = daily.Variables(0).ValuesInt64AsNumpy()
daily_sunset = daily.Variables(1).ValuesInt64AsNumpy()
daily_sunrise_local = pd.to_datetime(daily_sunrise, unit = "s", utc = True).tz_convert(response.Timezone().decode())
daily_sunset_local = pd.to_datetime(daily_sunset, unit = "s", utc = True).tz_convert(response.Timezone().decode())
daily_daylight_duration = daily.Variables(2).ValuesAsNumpy()
daily_sunshine_duration = daily.Variables(3).ValuesAsNumpy()
daily_shortwave_radiation_sum = daily.Variables(4).ValuesAsNumpy()

daily_data = {
	# Sunrise identifies the local calendar date represented by each daily value.
	"date": daily_sunrise_local.normalize()
}

daily_data["sunrise"] = daily_sunrise_local
daily_data["sunset"] = daily_sunset_local
daily_data["daylight_duration"] = daily_daylight_duration
daily_data["sunshine_duration"] = daily_sunshine_duration
daily_data["shortwave_radiation_sum"] = daily_shortwave_radiation_sum

daily_dataframe = pd.DataFrame(data = daily_data)


def ensure_weather_tables(conn):
	conn.execute("""
		CREATE TABLE IF NOT EXISTS openmeteo_hourly (
			location_key TEXT NOT NULL,
			time_utc TEXT NOT NULL,
			time_local TEXT NOT NULL,
			shortwave_radiation REAL,
			direct_radiation REAL,
			diffuse_radiation REAL,
			direct_normal_irradiance REAL,
			shortwave_radiation_clear_sky REAL,
			global_tilted_irradiance REAL,
			global_tilted_irradiance_east REAL,
			terrestrial_radiation REAL,
			latitude REAL NOT NULL,
			longitude REAL NOT NULL,
			elevation_m REAL,
			timezone TEXT NOT NULL,
			model TEXT NOT NULL,
			fetched_at_utc TEXT NOT NULL,
			raw_json TEXT NOT NULL,
			PRIMARY KEY (location_key, time_utc)
		)
	""")
	hourly_columns = {row[1] for row in conn.execute("PRAGMA table_info(openmeteo_hourly)")}
	if "global_tilted_irradiance_east" not in hourly_columns:
		conn.execute("ALTER TABLE openmeteo_hourly ADD COLUMN global_tilted_irradiance_east REAL")
	conn.execute("""
		CREATE TABLE IF NOT EXISTS openmeteo_daily (
			location_key TEXT NOT NULL,
			date_local TEXT NOT NULL,
			sunrise_local TEXT,
			sunset_local TEXT,
			daylight_duration REAL,
			sunshine_duration REAL,
			shortwave_radiation_sum REAL,
			latitude REAL NOT NULL,
			longitude REAL NOT NULL,
			elevation_m REAL,
			timezone TEXT NOT NULL,
			model TEXT NOT NULL,
			fetched_at_utc TEXT NOT NULL,
			raw_json TEXT NOT NULL,
			PRIMARY KEY (location_key, date_local)
		)
	""")


def json_value(value):
	if pd.isna(value):
		return None
	if isinstance(value, (pd.Timestamp, datetime)):
		return value.isoformat()
	if hasattr(value, "item"):
		return value.item()
	return value


def save_openmeteo_data(hourly_df, daily_df):
	location_key = f"{latitude:.4f},{longitude:.4f}:{params['models']}"
	fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
	metadata = {
		"latitude": latitude,
		"longitude": longitude,
		"elevation_m": elevation,
		"timezone": local_timezone,
		"timezone_abbreviation": timezone_abbreviation,
		"utc_offset_seconds": utc_offset_seconds,
		"model": params["models"],
	}
	conn = sqlite3.connect(DB_PATH)
	ensure_weather_tables(conn)
	with conn:
		# Correct legacy daily keys if a timezone/DST conversion labeled them by the prior day.
		conn.execute("""
			DELETE FROM openmeteo_daily
			WHERE location_key = ? AND date_local <> substr(sunrise_local, 1, 10)
		""", (location_key,))
		for _, row in hourly_df.iterrows():
			local_time = pd.Timestamp(row["date"])
			time_utc = local_time.tz_convert("UTC").isoformat()
			values = {key: json_value(row[key]) for key in hourly_df.columns if key != "date"}
			raw = json.dumps({**metadata, "time_local": local_time.isoformat(), **values}, separators=(",", ":"))
			conn.execute("""
				INSERT INTO openmeteo_hourly
			(location_key, time_utc, time_local, shortwave_radiation, direct_radiation,
			 diffuse_radiation, direct_normal_irradiance, shortwave_radiation_clear_sky,
			 global_tilted_irradiance, global_tilted_irradiance_east, terrestrial_radiation, latitude, longitude,
				 elevation_m, timezone, model, fetched_at_utc, raw_json)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(location_key, time_utc) DO UPDATE SET
				 time_local=excluded.time_local,
				 shortwave_radiation=excluded.shortwave_radiation,
				 direct_radiation=excluded.direct_radiation,
				 diffuse_radiation=excluded.diffuse_radiation,
				 direct_normal_irradiance=excluded.direct_normal_irradiance,
				 shortwave_radiation_clear_sky=excluded.shortwave_radiation_clear_sky,
				 global_tilted_irradiance=excluded.global_tilted_irradiance,
				 global_tilted_irradiance_east=excluded.global_tilted_irradiance_east,
				 terrestrial_radiation=excluded.terrestrial_radiation,
				 elevation_m=excluded.elevation_m, timezone=excluded.timezone,
				 fetched_at_utc=excluded.fetched_at_utc, raw_json=excluded.raw_json
			""", (
				location_key, time_utc, local_time.isoformat(),
				*[values.get(key) for key in (
					"shortwave_radiation", "direct_radiation", "diffuse_radiation",
					"direct_normal_irradiance", "shortwave_radiation_clear_sky",
				"global_tilted_irradiance", "global_tilted_irradiance_east", "terrestrial_radiation",
				)],
				latitude, longitude, elevation, local_timezone, params["models"], fetched_at, raw,
			))
		for _, row in daily_df.iterrows():
			local_date = pd.Timestamp(row["date"]).date().isoformat()
			values = {key: json_value(row[key]) for key in daily_df.columns if key != "date"}
			raw = json.dumps({**metadata, "date_local": local_date, **values}, separators=(",", ":"))
			conn.execute("""
				INSERT INTO openmeteo_daily
				(location_key, date_local, sunrise_local, sunset_local, daylight_duration,
				 sunshine_duration, shortwave_radiation_sum, latitude, longitude, elevation_m,
				 timezone, model, fetched_at_utc, raw_json)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(location_key, date_local) DO UPDATE SET
				 sunrise_local=excluded.sunrise_local, sunset_local=excluded.sunset_local,
				 daylight_duration=excluded.daylight_duration,
				 sunshine_duration=excluded.sunshine_duration,
				 shortwave_radiation_sum=excluded.shortwave_radiation_sum,
				 elevation_m=excluded.elevation_m, timezone=excluded.timezone,
				 fetched_at_utc=excluded.fetched_at_utc, raw_json=excluded.raw_json
			""", (
				location_key, local_date,
				values.get("sunrise"), values.get("sunset"),
				values.get("daylight_duration"), values.get("sunshine_duration"),
				values.get("shortwave_radiation_sum"), latitude, longitude, elevation,
				local_timezone, params["models"], fetched_at, raw,
			))
	conn.close()
	return fetched_at


fetched_at = save_openmeteo_data(hourly_dataframe, daily_dataframe)
print(f"Saved {len(hourly_dataframe)} hourly and {len(daily_dataframe)} daily Open-Meteo rows to {DB_PATH.name} (fetched {fetched_at} UTC).")


# Keep weather-model forecasts separate from the satellite archive observations.
forecast_url = "https://api.open-meteo.com/v1/forecast"
forecast_start_date = pd.Timestamp.now(tz="Australia/Sydney").date()
forecast_params = {
	"latitude": -32.9295,
	"longitude": 151.7801,
	"daily": ["sunrise", "sunset", "daylight_duration", "sunshine_duration", "shortwave_radiation_sum"],
	"minutely_15": ["shortwave_radiation", "direct_radiation", "diffuse_radiation", "direct_normal_irradiance", "shortwave_radiation_clear_sky", "global_tilted_irradiance", "terrestrial_radiation"],
	"timezone": "Australia/Sydney",
	"tilt": 22,
	"azimuth": 180,
	"start_date": forecast_start_date.isoformat(),
	"end_date": (forecast_start_date + pd.Timedelta(days=7)).isoformat(),
}
forecast_response = openmeteo.weather_api(forecast_url, params=forecast_params)[0]
east_forecast_params = {**forecast_params, "minutely_15": ["global_tilted_irradiance"], "daily": [], "azimuth": -90}
east_forecast_response = openmeteo.weather_api(forecast_url, params=east_forecast_params)[0]

forecast_minutely_15 = forecast_response.Minutely15()
forecast_minutely_15_times = pd.date_range(
	start=pd.to_datetime(forecast_minutely_15.Time(), unit="s", utc=True),
	end=pd.to_datetime(forecast_minutely_15.TimeEnd(), unit="s", utc=True),
	freq=pd.Timedelta(seconds=forecast_minutely_15.Interval()), inclusive="left",
).tz_convert(forecast_response.Timezone().decode())
forecast_minutely_15_df = pd.DataFrame({
	"date": forecast_minutely_15_times,
	"shortwave_radiation": forecast_minutely_15.Variables(0).ValuesAsNumpy(),
	"direct_radiation": forecast_minutely_15.Variables(1).ValuesAsNumpy(),
	"diffuse_radiation": forecast_minutely_15.Variables(2).ValuesAsNumpy(),
	"direct_normal_irradiance": forecast_minutely_15.Variables(3).ValuesAsNumpy(),
	"shortwave_radiation_clear_sky": forecast_minutely_15.Variables(4).ValuesAsNumpy(),
	"global_tilted_irradiance": forecast_minutely_15.Variables(5).ValuesAsNumpy(),
	"terrestrial_radiation": forecast_minutely_15.Variables(6).ValuesAsNumpy(),
})
east_forecast_minutely_15 = east_forecast_response.Minutely15()
east_forecast_times = pd.date_range(
	start=pd.to_datetime(east_forecast_minutely_15.Time(), unit="s", utc=True),
	end=pd.to_datetime(east_forecast_minutely_15.TimeEnd(), unit="s", utc=True),
	freq=pd.Timedelta(seconds=east_forecast_minutely_15.Interval()), inclusive="left",
).tz_convert(east_forecast_response.Timezone().decode())
east_forecast_minutely_15_df = pd.DataFrame({
	"date": east_forecast_times,
	"global_tilted_irradiance_east": east_forecast_minutely_15.Variables(0).ValuesAsNumpy(),
})
forecast_minutely_15_df = forecast_minutely_15_df.merge(
	east_forecast_minutely_15_df, on="date", how="left", validate="one_to_one",
)

forecast_daily = forecast_response.Daily()
forecast_sunrise = pd.to_datetime(
	forecast_daily.Variables(0).ValuesInt64AsNumpy(), unit="s", utc=True,
).tz_convert(forecast_response.Timezone().decode())
forecast_sunset = pd.to_datetime(
	forecast_daily.Variables(1).ValuesInt64AsNumpy(), unit="s", utc=True,
).tz_convert(forecast_response.Timezone().decode())
forecast_daily_df = pd.DataFrame({
	"date": forecast_sunrise.normalize(),
	"sunrise": forecast_sunrise,
	"sunset": forecast_sunset,
	"daylight_duration": forecast_daily.Variables(2).ValuesAsNumpy(),
	"sunshine_duration": forecast_daily.Variables(3).ValuesAsNumpy(),
	"shortwave_radiation_sum": forecast_daily.Variables(4).ValuesAsNumpy(),
})


def save_openmeteo_forecast(hourly_df, daily_df, response):
	latitude = response.Latitude()
	longitude = response.Longitude()
	elevation = response.Elevation()
	timezone_name = response.Timezone().decode()
	model_name = "best_match"
	location_key = f"{latitude:.4f},{longitude:.4f}:forecast:{model_name}"
	fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
	metadata = {
		"latitude": latitude, "longitude": longitude, "elevation_m": elevation,
		"timezone": timezone_name, "model": model_name,
		"north_tilt": 22, "north_azimuth": 180,
		"east_tilt": 22, "east_azimuth": -90,
	}
	conn = sqlite3.connect(DB_PATH)
	with conn:
		conn.execute("""
			CREATE TABLE IF NOT EXISTS openmeteo_forecast_hourly (
				location_key TEXT NOT NULL, time_utc TEXT NOT NULL, time_local TEXT NOT NULL,
				shortwave_radiation REAL, direct_radiation REAL, diffuse_radiation REAL,
				direct_normal_irradiance REAL, shortwave_radiation_clear_sky REAL,
				global_tilted_irradiance REAL, global_tilted_irradiance_east REAL,
				terrestrial_radiation REAL, latitude REAL NOT NULL, longitude REAL NOT NULL,
				elevation_m REAL, timezone TEXT NOT NULL, model TEXT NOT NULL,
				fetched_at_utc TEXT NOT NULL, raw_json TEXT NOT NULL,
				PRIMARY KEY (location_key, time_utc)
			)
		""")
		conn.execute("""
			CREATE TABLE IF NOT EXISTS openmeteo_forecast_daily (
				location_key TEXT NOT NULL, date_local TEXT NOT NULL, sunrise_local TEXT,
				sunset_local TEXT, daylight_duration REAL, sunshine_duration REAL,
				shortwave_radiation_sum REAL, latitude REAL NOT NULL, longitude REAL NOT NULL,
				elevation_m REAL, timezone TEXT NOT NULL, model TEXT NOT NULL,
				fetched_at_utc TEXT NOT NULL, raw_json TEXT NOT NULL,
				PRIMARY KEY (location_key, date_local)
			)
		""")
		for _, row in hourly_df.iterrows():
			local_time = pd.Timestamp(row["date"])
			values = {key: json_value(row[key]) for key in hourly_df.columns if key != "date"}
			raw = json.dumps({**metadata, "time_local": local_time.isoformat(), **values}, separators=(",", ":"))
			conn.execute("""
				INSERT INTO openmeteo_forecast_hourly (
					location_key, time_utc, time_local, shortwave_radiation, direct_radiation,
					diffuse_radiation, direct_normal_irradiance, shortwave_radiation_clear_sky,
					global_tilted_irradiance, global_tilted_irradiance_east, terrestrial_radiation,
					latitude, longitude, elevation_m, timezone, model, fetched_at_utc, raw_json
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(location_key, time_utc) DO UPDATE SET
					time_local=excluded.time_local, shortwave_radiation=excluded.shortwave_radiation,
					direct_radiation=excluded.direct_radiation, diffuse_radiation=excluded.diffuse_radiation,
					direct_normal_irradiance=excluded.direct_normal_irradiance,
					shortwave_radiation_clear_sky=excluded.shortwave_radiation_clear_sky,
					global_tilted_irradiance=excluded.global_tilted_irradiance,
					global_tilted_irradiance_east=excluded.global_tilted_irradiance_east,
					terrestrial_radiation=excluded.terrestrial_radiation,
					fetched_at_utc=excluded.fetched_at_utc, raw_json=excluded.raw_json
			""", (
				location_key, local_time.tz_convert("UTC").isoformat(), local_time.isoformat(),
				*[values.get(key) for key in (
					"shortwave_radiation", "direct_radiation", "diffuse_radiation",
					"direct_normal_irradiance", "shortwave_radiation_clear_sky",
					"global_tilted_irradiance", "global_tilted_irradiance_east", "terrestrial_radiation",
				)], latitude, longitude, elevation, timezone_name, model_name, fetched_at, raw,
			))
		for _, row in daily_df.iterrows():
			local_date = pd.Timestamp(row["date"]).date().isoformat()
			values = {key: json_value(row[key]) for key in daily_df.columns if key != "date"}
			raw = json.dumps({**metadata, "date_local": local_date, **values}, separators=(",", ":"))
			conn.execute("""
				INSERT INTO openmeteo_forecast_daily (
					location_key, date_local, sunrise_local, sunset_local, daylight_duration,
					sunshine_duration, shortwave_radiation_sum, latitude, longitude, elevation_m,
					timezone, model, fetched_at_utc, raw_json
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(location_key, date_local) DO UPDATE SET
					sunrise_local=excluded.sunrise_local, sunset_local=excluded.sunset_local,
					daylight_duration=excluded.daylight_duration, sunshine_duration=excluded.sunshine_duration,
					shortwave_radiation_sum=excluded.shortwave_radiation_sum,
					fetched_at_utc=excluded.fetched_at_utc, raw_json=excluded.raw_json
			""", (
				location_key, local_date,
				values.get("sunrise"), values.get("sunset"), values.get("daylight_duration"),
				values.get("sunshine_duration"), values.get("shortwave_radiation_sum"),
				latitude, longitude, elevation, timezone_name, model_name, fetched_at, raw,
			))
	conn.close()
	return fetched_at


forecast_fetched_at = save_openmeteo_forecast(forecast_minutely_15_df, forecast_daily_df, forecast_response)
print(
	f"Saved {len(forecast_minutely_15_df)} 15-minute and {len(forecast_daily_df)} daily Open-Meteo forecast rows "
	f"to {DB_PATH.name} (fetched {forecast_fetched_at} UTC)."
)
