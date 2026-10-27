"""Synoptic Data (MesoWest) client for non-NRCS stations such as Alta Collins (CLN).

UNTESTED: written without a token. Follows the public timeseries API
(https://docs.synopticdata.com/services/time-series); check the variable names on first run.
Needs a free token in the SYNOPTIC_TOKEN environment variable.
"""
from __future__ import annotations

import os

import pandas as pd
import requests

BASE = "https://api.synopticdata.com/v2"
TIMEOUT = 60

# Synoptic variable -> element code used in the obs archive (same codes as SNOTEL where they overlap).
# PREC = gauge accumulated precip (in); it resets when the gauge is serviced, so use positive diffs.
VARS = {"precip_accum": "PREC", "snow_depth": "SNWD", "snow_interval": "SNWI", "air_temp": "TOBS"}


def token() -> str | None:
    # Read from an environment variable so the secret never sits in the code or gets committed to git.
    # Set it once in a terminal with:  setx SYNOPTIC_TOKEN yourtoken   (then open a new terminal)
    return os.environ.get("SYNOPTIC_TOKEN")


def fetch(stids: list[str], begin: str, end: str, vars_: dict[str, str] = VARS) -> pd.DataFrame:
    """Fetch station timeseries as a tidy frame: station, element, time_utc, value (english units).

    begin/end are 'YYYY-MM-DD' (UTC). Synoptic returns UTC times, so no time zone shift is needed.
    """
    tok = token()
    if not tok:
        raise RuntimeError("SYNOPTIC_TOKEN is not set (free token at https://synopticdata.com).")
    r = requests.get(
        f"{BASE}/stations/timeseries",
        params={
            "stid": ",".join(stids),
            "start": pd.Timestamp(begin).strftime("%Y%m%d%H%M"),
            "end": pd.Timestamp(end).strftime("%Y%m%d%H%M"),
            "vars": ",".join(vars_),
            "units": "english",
            "obtimezone": "utc",
            "token": tok,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    js = r.json()
    # Synoptic reports some problems (bad token, unknown station) inside a normal-looking reply, so check it.
    if js.get("SUMMARY", {}).get("RESPONSE_CODE") != 1:
        raise RuntimeError(f"Synoptic error: {js.get('SUMMARY', {}).get('RESPONSE_MESSAGE')}")
    rows = []
    for st in js.get("STATION", []):
        ob = st.get("OBSERVATIONS", {})
        # Unlike SNOTEL, each variable is its own list, lined up with the date_time list by position.
        times = pd.to_datetime(ob.get("date_time", []), utc=True).tz_localize(None)
        for key, values in ob.items():
            # keys look like "precip_accum_set_1"; derived series end in "d" and are skipped
            var = key.rsplit("_set_", 1)[0]
            if var not in vars_ or key.endswith("d"):
                continue
            # zip walks two lists side by side: (time 1, value 1), (time 2, value 2), ...
            for t, v in zip(times, values):
                rows.append({"station": st.get("STID"), "element": vars_[var], "time_utc": t, "value": v})
    df = pd.DataFrame(rows, columns=["station", "element", "time_utc", "value"])
    # sub-hourly reports: keep the last value in each hour so the archive matches SNOTEL's hourly grain
    if not df.empty:
        df["value"] = pd.to_numeric(df["value"], errors="coerce")  # errors="coerce": unreadable -> NaN
        df = df.dropna(subset=["value"]).sort_values("time_utc")
        df["time_utc"] = df["time_utc"].dt.ceil("h")  # round UP: a 10:55 report counts toward 11:00
        df = df.drop_duplicates(["station", "element", "time_utc"], keep="last")
    return df.reset_index(drop=True)
