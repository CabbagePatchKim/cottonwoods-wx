"""NOAA GHCN-Daily client (NCEI access data service) for cooperative observer stations such as ALTA (USC00420072).

No token needed. Each value is a 24 h total (PRCP, SNOW) or a reading (SNWD) taken once a day by an observer at
a fixed local standard time (ALTA: 18:00, so the "2023-12-03" value covers 6 pm 12/2 to 6 pm 12/3).
Docs: https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation
"""
from __future__ import annotations

import io

import pandas as pd
import requests

BASE = "https://www.ncei.noaa.gov/access/services/data/v1"
TIMEOUT = 120

# PRCP = precip (in, rain + melted snow), SNOW = new snowfall (in), SNWD = snow depth (in)
ELEMENTS = ("PRCP", "SNOW", "SNWD")


def fetch(
    stations: list[str],
    begin: str,
    end: str,
    obs_hour_local: int = 18,
    tz_offset_hours: float = -7,
    elements: tuple[str, ...] = ELEMENTS,
) -> pd.DataFrame:
    """Daily values as a tidy frame: station, element, time_utc, value, trace.

    time_utc is the END of the observer's 24 h day in UTC, so it lines up with forecast windows. The observation
    hour comes from each value's attributes when present (it can change over a station's history), else
    obs_hour_local. Values that failed NOAA's quality checks are dropped.
    """
    r = requests.get(
        BASE,
        params={
            "dataset": "daily-summaries",
            "stations": ",".join(stations),
            "startDate": begin,
            "endDate": end,
            "dataTypes": ",".join(elements),
            "units": "standard",   # inches
            "format": "csv",
            "includeAttributes": "true",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if not r.text.strip():
        return pd.DataFrame(columns=["station", "element", "time_utc", "value", "trace"])
    # io.StringIO lets pandas read the downloaded text as if it were a file
    wide = pd.read_csv(io.StringIO(r.text), dtype=str)
    if wide.empty:  # NOAA posts a few days late: recent requests often come back as a header with no rows
        return pd.DataFrame(columns=["station", "element", "time_utc", "value", "trace"])

    rows = []
    for el in elements:
        if el not in wide.columns:
            continue
        # Attributes are "measurement flag,quality flag,source,obs time HHMM", e.g. "T,,7,1800" = trace.
        attrs = wide.get(f"{el}_ATTRIBUTES", pd.Series("", index=wide.index)).fillna("").str.split(",", expand=True)
        attrs = attrs.reindex(columns=range(4)).fillna("")
        part = pd.DataFrame(
            {
                "station": wide["STATION"],
                "element": el,
                "date": pd.to_datetime(wide["DATE"]),
                "value": pd.to_numeric(wide[el], errors="coerce"),
                "trace": attrs[0] == "T",
                "qflag": attrs[1],
                "hhmm": attrs[3],
            }
        )
        part = part[part["value"].notna() & (part["qflag"] == "")]
        # Observer's local standard time -> UTC. A missing/odd time ("2400" also occurs) falls back to the default.
        hour = pd.to_numeric(part["hhmm"].str[:2], errors="coerce").fillna(obs_hour_local).clip(upper=24)
        part["time_utc"] = part["date"] + pd.to_timedelta(hour, unit="h") - pd.Timedelta(hours=tz_offset_hours)
        rows.append(part[["station", "element", "time_utc", "value", "trace"]])
    if not rows:
        return pd.DataFrame(columns=["station", "element", "time_utc", "value", "trace"])
    return pd.concat(rows, ignore_index=True).sort_values(["station", "element", "time_utc"]).reset_index(drop=True)
