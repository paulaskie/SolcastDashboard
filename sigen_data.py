"""Sigen Open Platform extraction and response parsing."""

import json
import os
import subprocess
from pathlib import Path

import pandas as pd

TZ_NAME = "Australia/Sydney"
STATION_ID = 2_610_011_552_188_752
SIGEN_MCP_SCRIPT = Path(os.getenv(
    "SIGEN_MCP_SCRIPT",
    str(Path.home() / ".codex" / "skills" / "sigen-open-platform" / "scripts" / "sigen.mjs"),
))


def fetch_day(station_id, day, include_quota=False):
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
    if include_quota:
        quota = envelope.get("_meta", {}).get("sigen/quota")
        return rows, quota
    return rows


def fetch_actuals(station_id, start_date, end_date, include_quota=False):
    """Fetch a date range and return meter energy and power frames for persistence."""
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
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
            power_kw = pd.to_numeric(item.get("pvTotalPower"), errors="coerce")
            if pd.isna(stamp) or pd.isna(power_kw):
                continue
            local_stamp = stamp.tz_localize(TZ_NAME, ambiguous="NaT", nonexistent="NaT")
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
