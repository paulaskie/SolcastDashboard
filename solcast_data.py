"""Solcast API requests and response parsing for the dashboard."""

import re

import pandas as pd
import requests

SITE_ID = "1c66-66cb-9735-8787"
SITE_URL = f"https://api.solcast.com.au/rooftop_sites/{SITE_ID}"
AUTH_CHECK_URL = f"{SITE_URL}/estimated_actuals?format=json"
ACTUALS_URL = AUTH_CHECK_URL
FORECAST_URL = f"{SITE_URL}/forecasts?format=json"
TZ_NAME = "Australia/Sydney"


class SolcastRateLimitError(ValueError):
    """A Solcast 429 response with its rate limit headers attached."""

    def __init__(self, response, operation):
        headers = response.headers
        self.limit = headers.get("X-RateLimit-Limit")
        self.remaining = headers.get("X-RateLimit-Remaining")
        self.retry_after = headers.get("Retry-After")

        details = [f"Solcast rate limit reached (429) while {operation}."]
        if self.limit is not None:
            details.append(f"Limit: {self.limit} requests in the current window.")
        if self.remaining is not None:
            details.append(f"Remaining: {self.remaining}.")
        if self.retry_after is not None:
            details.append(f"Retry after {self.retry_after} seconds.")
        else:
            details.append("Retry-After was not included in the response.")
        super().__init__(" ".join(details))


def raise_for_rate_limit(response, operation):
    """Raise a useful error for 429 responses without retrying immediately."""
    if response.status_code == 429:
        raise SolcastRateLimitError(response, operation)


def parse_period_hours(period):
    """Convert an ISO-8601 Solcast period such as PT30M into hours."""
    if not isinstance(period, str):
        return 0.5
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?", period)
    if not match:
        return 0.5
    return float(match.group(1) or 0) + float(match.group(2) or 0) / 60


def api_headers(api_key):
    return {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}


def check_authorization(api_key):
    """Check the key against Solcast estimated actuals before forecast requests."""
    response = requests.get(AUTH_CHECK_URL, headers=api_headers(api_key), timeout=20)
    if response.status_code == 401:
        raise ValueError("Authorization failed (401 Unauthorized). Check your Solcast API key.")
    if response.status_code == 403:
        raise ValueError("Authorization check failed (403 Forbidden). This key may not have access to this rooftop site.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required during authorization check. Check your account or plan.")
    raise_for_rate_limit(response, "checking authorization")
    response.raise_for_status()
    return response.json() if response.content else {}


def fetch_forecast(api_key, include_rate_limit=False):
    response = requests.get(FORECAST_URL, headers=api_headers(api_key), timeout=30)
    if response.status_code == 401:
        raise ValueError("Solcast rejected the API key (401 Unauthorized). Check the key and try again.")
    if response.status_code == 403:
        raise ValueError("Solcast returned 403 Forbidden. Check that your account/key has access to this endpoint.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required. You may have reached an account or plan limit.")
    raise_for_rate_limit(response, "fetching the forecast")
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
    for column in ("pv_estimate", "pv_estimate10", "pv_estimate90"):
        if column not in df.columns:
            df[column] = pd.NA
        df[column] = pd.to_numeric(df[column], errors="coerce")
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
    df["energy_kwh"] = df["pv_estimate"] * df["period_hours"]
    df["energy10_kwh"] = pd.to_numeric(df["pv_estimate10"], errors="coerce") * df["period_hours"]
    df["energy90_kwh"] = pd.to_numeric(df["pv_estimate90"], errors="coerce") * df["period_hours"]
    if include_rate_limit:
        return df, payload, response.headers.get("X-RateLimit-Remaining")
    return df, payload


def fetch_estimated_actuals(api_key, include_rate_limit=False):
    response = requests.get(ACTUALS_URL, headers=api_headers(api_key), timeout=30)
    if response.status_code == 401:
        raise ValueError("Solcast rejected the API key (401 Unauthorized) for estimated actuals.")
    if response.status_code == 403:
        raise ValueError("Solcast returned 403 Forbidden for estimated actuals.")
    if response.status_code == 402:
        raise ValueError("Solcast returned 402 Payment Required for estimated actuals.")
    raise_for_rate_limit(response, "fetching estimated actuals")
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("estimated_actuals")
    if not isinstance(rows, list):
        raise ValueError("The response did not contain an 'estimated_actuals' list.")
    df = pd.DataFrame(rows)
    if df.empty:
        if include_rate_limit:
            return df, payload, response.headers.get("X-RateLimit-Remaining")
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
    if include_rate_limit:
        return df, payload, response.headers.get("X-RateLimit-Remaining")
    return df, payload
