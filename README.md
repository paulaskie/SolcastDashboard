# Solcast Solar Forecast Dashboard

A local Streamlit app for your Solcast rooftop-site forecast endpoint.

## Features
- Load `SOLCAST_API_KEY` and `AMBER_API_KEY` from the process environment; no API key is entered in the app sidebar.
- Run a separate Solcast authorization checkpoint using the rooftop-site estimated-actuals endpoint, then download the current forecast only if authorization succeeds.
- Daily forecast generation estimates (kWh), peak power (kW), and half-hourly curves.
- Meter actuals loaded from the SQLite database, alongside Solcast estimated actuals.
- On-demand Sigen refresh that writes power and energy actuals to SQLite.
- Amber 5-minute electricity prices stored in the same SQLite database and charted in cents per kWh.
- Historical AEMO 5-minute dispatch energy prices for all NEM regions, stored in the same SQLite database and available as an overlay on the Amber price graph.
- Open-Meteo hourly radiation and daily daylight/radiation data stored in the same SQLite database.
- Saves every download to a local SQLite database (`solcast_history.sqlite3`).
- Export latest intervals or all stored snapshots to CSV.
- Optional Windows Task Scheduler script for a daily download even when the dashboard is closed.

Site ID is preconfigured as `1c66-66cb-9735-8787`.

## Install on Windows
1. Install Python 3.11 or newer from https://www.python.org/downloads/ (enable **Add Python to PATH**).
2. Extract this folder somewhere stable, e.g. `C:\SolcastDashboard`.
3. Open Command Prompt in that folder and run:

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   streamlit run app.py
   ```

4. Set `SOLCAST_API_KEY` and `AMBER_API_KEY` as Windows user environment variables, then restart the terminal and launch the dashboard. The keys are loaded automatically; click **Check API authorization** before downloading a Solcast forecast.

## Build and run the Windows executable

The executable is a single-file Windows x64 package. Build it on Windows (PyInstaller does not cross-compile Windows executables from other operating systems):

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --clean solcast_dashboard.spec
```

Give the other machine the resulting `dist\SolcastDashboard.exe`. On that machine:

1. Copy the EXE to a folder the user can write to, such as `%LOCALAPPDATA%\SolcastDashboard`. The app creates `solcast_history.sqlite3` and its Open-Meteo cache in that folder, so keep the EXE in the same place between runs and back up the database if its history matters.
2. Set API keys in that Windows user's environment (only needed for the corresponding downloads), then sign out and back in or open a fresh terminal so Windows apps inherit them:

   ```bat
   setx SOLCAST_API_KEY "your-solcast-key"
   setx AMBER_API_KEY "your-amber-key"
   ```

3. Double-click `SolcastDashboard.exe`. It starts the local Streamlit server and opens the dashboard in the default browser. It listens on localhost.

Python and Python PATH entries are not needed on the destination machine. Network access is needed for Solcast, Amber, and Open-Meteo features. Meter actuals are read from `meter_energy_actuals` and `meter_power_actuals` in the SQLite database. Copy the database beside the EXE (or set `SOLCAST_DB_PATH`) to bring existing history to another machine. The Sigen Open Platform refresh additionally requires Node.js on PATH and the Sigen `sigen.mjs` client. Set `SIGEN_MCP_SCRIPT` to that script's full path if it is not in the default Codex skills location. The Open-Meteo refresh button works from the executable without a separate Python installation.

To choose another writable database location, set `SOLCAST_DB_PATH` to its full SQLite filename before starting the EXE. If the dashboard does not open, run it from Command Prompt to see the startup error:

```bat
SolcastDashboard.exe
```

The keys are read from the process environment and sent to Solcast or Amber in HTTPS `Authorization: Bearer` headers. They are not stored in the SQLite database or shown in the dashboard. Do not commit or share API keys.

Set the `AMBER_API_KEY` environment variable to enable Amber downloads in the dashboard. Select a Sydney date and choose **Fetch Amber prices and 2-day forecast** to fetch that date from `/prices` plus the next 576 intervals from `/prices/current`, or choose **Fetch next 48 five-minute periods** to request `next=48&resolution=5`. Current forecast downloads are retained as snapshots; for each interval, the chart selects the newest non-null value and falls back to older snapshots when needed. Actual intervals take precedence where available. The graph can show all channels or filter to `general`, `controlledLoad`, or `feedIn`. The key is sent only in HTTPS authorization headers and is not stored in SQLite. You can also run `python download_amber.py YYYY-MM-DD` to fetch a date from a terminal.

Meter actuals are loaded exclusively from the `meter_energy_actuals` and `meter_power_actuals` tables in `solcast_history.sqlite3`. The **Sigen Open Platform** sidebar displays the configured station ID and lets you select a date range. **Update Power Actuals from Sigen** requests each selected day of five-minute energy statistics through the installed Sigen Open Platform client, writes power and derived five-minute energy values plus raw Sigen rows into SQLite, and updates the graphs. The station ID is set in `SIGEN_STATION_ID` in `app.py`. The Node client handles Sigen browser authorization; set `SIGEN_MCP_SCRIPT` only if `sigen.mjs` is installed at a non-default path.

Use **AEMO historical prices** in the sidebar to import public five-minute dispatch prices for a selected AEMO market-date range. Records for all five NEM regions are stored in `aemo_prices`; `per_kwh` and `spot_per_kwh` convert AEMO RRP and ROP from AUD/MWh to cents/kWh, with the original fields retained in `raw_json`. AEMO dispatch timestamps use fixed AEST (UTC+10), and the importer converts them to UTC for storage. AEMO's daily archives may lag recent dispatch dates; the importer saves available dates and reports unavailable archives.

Run `python openmeteo.py` or use **Refresh Open-Meteo data and forecast** in the app sidebar to update archive observations from 8 October through the latest available archive date and fetch the 7-day forecast. Archive data is stored in `openmeteo_daily` and `openmeteo_hourly`; forecast data is kept separately in `openmeteo_forecast_daily` and `openmeteo_forecast_hourly` in the same `solcast_history.sqlite3` database (or the path in `SOLCAST_DB_PATH`). Both sources store separate 22° north and east plane irradiance values; the power estimate weights them 80% north and 20% east to match the panel layout. Matching timestamps are updated on refresh. The Open-Meteo comparison graph includes forecast estimates and aligns irradiance-derived estimates to the selected day's Sigen meter timestamps, interpolating between Open-Meteo samples where needed.

## Daily automatic downloads (Windows Task Scheduler)
A scheduled task is more reliable than relying on a browser dashboard being open. The scheduled script uses the same SQLite database as the app.

1. Create a Windows user environment variable named `SOLCAST_API_KEY` containing your API key:
   - Start → search **Edit environment variables for your account** → **New**
   - Variable name: `SOLCAST_API_KEY`
   - Variable value: your key
   - Restart Command Prompt/Task Scheduler after setting it.
2. Open **Task Scheduler** → **Create Basic Task**.
3. Name it `Solcast daily forecast`; choose **Daily** and a convenient time (e.g. 6:00 AM).
4. Choose **Start a program**:
   - **Program/script:** full path to your venv Python, e.g. `C:\SolcastDashboard\.venv\Scripts\python.exe`
   - **Add arguments:** `download_daily.py`
   - **Start in:** `C:\SolcastDashboard`
5. Finish, then right-click the task → **Run** to test it. Check the app's download history afterward.

Important:
- The authorization checkpoint is a separate estimated-actuals API request before the forecast request. It can count toward API usage; use it sparingly and avoid repeated tests when a 429 rate limit has occurred.
- Use the same folder/database path for the app and scheduled task.
- Task Scheduler must run under the same Windows account that has the `SOLCAST_API_KEY` variable.
- The dashboard and scheduled download script use `SOLCAST_API_KEY` from the environment. Amber downloads use `AMBER_API_KEY` from the environment.
- Solcast hobbyist accounts may have a daily API request quota. This app only requests data when you click the button; the scheduled script makes one request per run. Avoid scheduling both multiple times per day.
- The forecast is future generation, not measured actual production. Energy is estimated by multiplying forecast power (kW) by each interval's duration (hours), then summing by Sydney local date.
- If your Solcast account returns a different response schema, check the error message and the official API docs: https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist
