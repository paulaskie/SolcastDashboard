"""Sigen Open Platform extraction and response parsing."""

import base64
import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import requests

TZ_NAME = "Australia/Sydney"
STATION_ID = 2_610_011_552_188_752
SIGEN_MCP_SCRIPT = Path(os.getenv(
    "SIGEN_MCP_SCRIPT",
    str(Path.home() / ".codex" / "skills" / "sigen-open-platform" / "scripts" / "sigen.mjs"),
))


def developer_api_configured():
    return bool(os.getenv("SIGEN_APP_KEY", "").strip() and os.getenv("SIGEN_APP_SECRET", "").strip())


def _decode_api_data(value):
    """Decode Sigen's sometimes JSON-encoded `data` response field."""
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def _api_base_url():
    configured = os.getenv("SIGEN_API_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    region = os.getenv("SIGEN_REGION", "aus").strip().lower()
    return f"https://openapi-{region}.sigencloud.com"


def _api_headers(token, token_type):
    region = os.getenv("SIGEN_REGION", "aus").strip().lower()
    return {
        "Authorization": f"{token_type} {token}",
        "sigen-region": region,
        "Accept": "application/json",
    }


def _api_request(method, path, token=None, token_type=None, **kwargs):
    url = f"{_api_base_url()}/{path.lstrip('/')}"
    headers = kwargs.pop("headers", {})
    if token:
        headers.update(_api_headers(token, token_type or "Bearer"))
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Sigen API returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Sigen API returned an unexpected response format.")
    if payload.get("code", 0) != 0:
        message = str(payload.get("msg") or "request failed")
        raise RuntimeError(f"Sigen API error {payload.get('code')}: {message}")
    return payload


def _developer_api_token():
    app_key = os.getenv("SIGEN_APP_KEY", "").strip()
    app_secret = os.getenv("SIGEN_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("Set SIGEN_APP_KEY and SIGEN_APP_SECRET in Streamlit secrets.")
    encoded = base64.b64encode(f"{app_key}:{app_secret}".encode("utf-8")).decode("ascii")
    payload = _api_request("POST", "/openapi/auth/login/key", json={"key": encoded})
    data = _decode_api_data(payload.get("data"))
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise RuntimeError("Sigen authentication succeeded without returning an access token.")
    token_type = data.get("tokenType")
    if not isinstance(token_type, str) or not token_type.strip():
        raise RuntimeError("Sigen authentication succeeded without returning a token type.")
    return data["accessToken"], token_type


def _developer_system_id(token, token_type):
    configured = os.getenv("SIGEN_SYSTEM_ID", "").strip()
    if configured:
        return configured
    payload = _api_request("GET", "/openapi/system", token=token, token_type=token_type)
    systems = _decode_api_data(payload.get("data"))
    if isinstance(systems, dict):
        systems = systems.get("itemList") or systems.get("list") or systems.get("systems") or []
    if isinstance(systems, list):
        for system in systems:
            if isinstance(system, dict) and system.get("systemId"):
                return str(system["systemId"])
    raise RuntimeError(
        "No Sigen system was found for this app. Set SIGEN_SYSTEM_ID in Streamlit secrets "
        "to the systemId returned by GET /openapi/system."
    )


def _fetch_developer_api_day(day):
    token, token_type = _developer_api_token()
    system_id = _developer_system_id(token, token_type)
    payload = _api_request(
        "GET",
        f"/openapi/systems/{system_id}/history",
        token=token,
        token_type=token_type,
        params={"level": "Day", "date": day.strftime("%Y-%m-%d")},
    )
    data = _decode_api_data(payload.get("data"))
    if not isinstance(data, dict):
        raise RuntimeError("Sigen returned no historical data for this date.")
    return data.get("itemList") or []


def _fetch_node_day(station_id, day, include_quota=False):
    if not SIGEN_MCP_SCRIPT.exists():
        raise FileNotFoundError(
            f"Sigen Open Platform client not found at {SIGEN_MCP_SCRIPT}. "
            "Set SIGEN_MCP_SCRIPT to the installed sigen.mjs path."
        )
    args = {
        "station_id": int(station_id), "dataset": "energy_statistics", "period": "day",
        "start_date": day.strftime("%Y%m%d"), "end_date": day.strftime("%Y%m%d"),
    }
    result = subprocess.run(
        ["node", str(SIGEN_MCP_SCRIPT), "call", "get_station_energy_data", json.dumps(args)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
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
    rows = payload.get("data", {}).get("itemList", [])
    quota = envelope.get("_meta", {}).get("sigen/quota")
    return (rows, quota) if include_quota else rows


def fetch_day(station_id, day, include_quota=False):
    """Fetch one day's historical energy statistics from Sigen."""
    if developer_api_configured():
        rows = _fetch_developer_api_day(day)
        return (rows, None) if include_quota else rows
    return _fetch_node_day(station_id, day, include_quota=include_quota)


def fetch_actuals(station_id, start_date, end_date, include_quota=False):
    """Fetch a date range and return meter energy and power frames for persistence.

    The developer historical endpoint is limited to one station request every five minutes,
    so the direct API mode accepts one day per update. The Node client retains date-range support.
    """
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    if developer_api_configured() and end_date != start_date:
        raise ValueError(
            "The Sigen developer API allows one station history request every five minutes. "
            "Select a single date for each update."
        )

    power_rows = []
    quota = None
    for day in pd.date_range(start_date, end_date, freq="D"):
        day_result = fetch_day(station_id, day.date(), include_quota=include_quota)
        if include_quota:
            day_rows, quota = day_result
        else:
            day_rows = day_result
        for item in day_rows:
            stamp = pd.to_datetime(item.get("dataTime"), format="%Y%m%d %H:%M", errors="coerce")
            if pd.isna(stamp):
                stamp = pd.to_datetime(item.get("dataTime"), errors="coerce")
            power_kw = pd.to_numeric(item.get("pvTotalPower"), errors="coerce")
            if pd.isna(stamp) or pd.isna(power_kw):
                continue
            if stamp.tzinfo is None:
                local_stamp = stamp.tz_localize(TZ_NAME, ambiguous="NaT", nonexistent="NaT")
            else:
                local_stamp = stamp.tz_convert(TZ_NAME)
            if pd.isna(local_stamp):
                continue
            power_rows.append({
                "period_end_local": local_stamp,
                "Solar Production (kW)": float(power_kw),
                "Raw Sigen JSON": json.dumps(item, separators=(",", ":"), ensure_ascii=False),
            })
    if not power_rows:
        result = (pd.DataFrame(), pd.DataFrame())
        return (*result, quota) if include_quota else result
    power_df = pd.DataFrame(power_rows).sort_values("period_end_local")
    energy_df = power_df[["period_end_local", "Raw Sigen JSON"]].copy()
    energy_df["Total Solar Production Energy (kWh)"] = power_df["Solar Production (kW)"] * (5 / 60)
    return (energy_df, power_df, quota) if include_quota else (energy_df, power_df)
