"""NRCS AWDB REST client for SNOTEL data.

UNTESTED: written offline. The endpoint paths and JSON shape below follow the public AWDB REST API
(https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html); check against that doc on first run.
"""
from __future__ import annotations

import pandas as pd
import requests

BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
TIMEOUT = 60


def find_stations(name_contains: str, state: str = "UT") -> pd.DataFrame:
    """List active SNOTEL stations whose name contains a string. Use this to fill in triplets."""
    # requests.get sends an HTTP request, like a browser loading a URL. params become ?key=value in the URL.
    r = requests.get(
        f"{BASE}/stations",
        params={"stateCodes": state, "networkCodes": "SNTL", "activeOnly": "true"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()  # turn an HTTP error (404, 500...) into a Python exception instead of failing silently
    df = pd.DataFrame(r.json())  # the reply is JSON (a list of dicts); each dict becomes a row
    if df.empty:
        return df
    mask = df["name"].str.contains(name_contains, case=False, na=False)
    keep = [c for c in ("stationTriplet", "name", "elevation", "latitude", "longitude") if c in df.columns]
    return df.loc[mask, keep].reset_index(drop=True)


def fetch(
    triplets: list[str],
    begin: str,
    end: str,
    elements: tuple[str, ...] = ("WTEQ", "SNWD", "PREC", "TOBS"),
    duration: str = "HOURLY",
) -> pd.DataFrame:
    """Fetch SNOTEL data as a tidy frame: station, element, time, value.

    WTEQ = snow water equivalent (in), SNWD = snow depth (in), PREC = accumulated precip (in),
    TOBS = observed air temperature (F). Dates are 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' in station local time.
    """
    r = requests.get(
        f"{BASE}/data",
        params={
            "stationTriplets": ",".join(triplets),
            "elements": ",".join(elements),
            "duration": duration,
            "beginDate": begin,
            "endDate": end,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    # The JSON is nested: station -> element blocks -> values. Flatten it into "long" (tidy) format, one row
    # per station/element/time, which is easy to filter and pivot later.
    rows = []
    for station in r.json():
        trip = station.get("stationTriplet")
        for block in station.get("data", []):
            # .get(key, {}) returns {} if the key is missing, so the chained .get can't crash
            el = block.get("stationElement", {}).get("elementCode")
            for v in block.get("values", []):
                rows.append({"station": trip, "element": el, "time": v.get("date"), "value": v.get("value")})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"])
    return df


def swe_increase(df: pd.DataFrame, station: str, smooth_hours: int = 6) -> float:
    """Largest SWE rise over the frame's window: peak minus the lowest value before the peak.

    Hourly pillow readings flicker by +/-0.1", so summing every hourly uptick counts noise as snow (a dry
    storm window at Snowbird added up to 6"). A rolling median removes the flicker first; taking the rise
    to the peak, rather than end minus start, ignores melt or settling after the storm.
    """
    s = df[(df.station == station) & (df.element == "WTEQ")].sort_values("time")["value"].dropna()
    if s.empty:
        return float("nan")
    # center=True: each hour becomes the median of the hours around it, not just the hours before it
    sm = s.rolling(smooth_hours, center=True, min_periods=1).median().reset_index(drop=True)
    # cummin() = lowest value so far at each hour. sm - cummin = rise since that low; the max is the storm gain.
    return float((sm - sm.cummin()).max())
