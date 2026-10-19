"""Amber API requests and response validation for the dashboard."""

import pandas as pd
import requests

TZ_NAME = "Australia/Sydney"

SITE_ID = "01M4M4A4ZJW5FK6KCP6JKMC10H"
PRICES_URL = f"https://api.amber.com.au/v1/sites/{SITE_ID}/prices"
CURRENT_PRICES_URL = f"{PRICES_URL}/current"


def _get_rows(url, api_key, params, timeout, description):
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
        params=params,
        timeout=timeout,
    )
    if response.status_code in (401, 403):
        raise ValueError(f"Amber rejected the API key ({response.status_code}). Check AMBER_API_KEY and site access.")
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError(f"Amber {description} response was not a list of price intervals.")
    return rows


def fetch_prices(api_key, price_date):
    return _get_rows(
        PRICES_URL, api_key,
        {"startDate": price_date, "endDate": price_date, "resolution": 5},
        30, "prices",
    )


def fetch_current_prices(api_key, next_intervals=576):
    return _get_rows(
        CURRENT_PRICES_URL, api_key,
        {"next": next_intervals, "resolution": 5},
        45, "current-prices",
    )


def fetch_actuals_between(api_key, start_utc, end_utc):
    """Fetch Amber actual intervals in the UTC window, querying local calendar days."""
    start_utc = pd.to_datetime(start_utc, utc=True)
    end_utc = pd.to_datetime(end_utc, utc=True)
    if end_utc <= start_utc:
        return []
    start_date = start_utc.tz_convert(TZ_NAME).date()
    end_date = end_utc.tz_convert(TZ_NAME).date()
    actual_rows = []
    for day in pd.date_range(start_date, end_date, freq="D"):
        for row in fetch_prices(api_key, day.date().isoformat()):
            if str(row.get("type") or "").lower() not in {"actualinterval", "currentinterval"}:
                continue
            interval_end = pd.to_datetime(row.get("endTime"), utc=True, errors="coerce")
            if pd.notna(interval_end) and start_utc < interval_end <= end_utc:
                actual_rows.append(row)
    return actual_rows
