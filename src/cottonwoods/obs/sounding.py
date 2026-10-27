"""Weather-balloon (radiosonde) soundings from the Iowa Environmental Mesonet (IEM) archive. No token needed.

Salt Lake City (KSLC, WMO 72572) launches at about 23 and 11 UTC for the nominal 00z and 12z soundings, the only
true upper-air OBSERVATIONS near the Wasatch (model analyses such as RAP or ERA5 are estimates). IEM usually has
a sounding within a couple of hours of launch and keeps history back decades.
Service docs: https://mesonet.agron.iastate.edu/cgi-bin/request/raob.py?help

fetch() returns every reported level; summarize() boils each sounding down to the numbers the snow work uses.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import requests

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/raob.py"
TIMEOUT = 120
COLS = ["station", "time_utc", "levelcode", "pressure_hpa", "height_m", "temp_c", "dewpoint_c", "wind_dir", "wind_kt"]


def fetch(station: str, begin: str, end: str) -> pd.DataFrame:
    """All levels of every sounding with begin <= time <= end (UTC, 'YYYY-MM-DD' or ISO). Keep spans to ~a month."""
    r = requests.get(URL, params={"station": station, "sts": f"{pd.Timestamp(begin):%Y-%m-%dT%H:%M}Z",
                                  "ets": f"{pd.Timestamp(end):%Y-%m-%dT%H:%M}Z"}, timeout=TIMEOUT)
    r.raise_for_status()
    # "M" marks a missing value; levels below the ground (e.g. 1000 and 925 mb at Salt Lake) are all M
    df = pd.read_csv(io.StringIO(r.text), na_values=["M"])
    if df.empty:
        return pd.DataFrame(columns=COLS)
    df = df.rename(columns={"validUTC": "time_utc", "pressure_mb": "pressure_hpa", "tmpc": "temp_c",
                            "dwpc": "dewpoint_c", "drct": "wind_dir", "speed_kts": "wind_kt"})
    df["time_utc"] = pd.to_datetime(df["time_utc"])
    df = df.dropna(subset=["pressure_hpa"])
    return df[COLS].sort_values(["time_utc", "pressure_hpa"], ascending=[True, False]).reset_index(drop=True)


def _rh(t, td):
    """Relative humidity (%) from temperature and dewpoint (C), Magnus formula."""
    es = lambda x: 6.112 * np.exp(17.67 * x / (x + 243.5))  # noqa: E731  saturation vapor pressure (hPa)
    return 100.0 * es(td) / es(t)


def _at(prof: pd.DataFrame, col: str, p: float) -> float:
    """Value at pressure p, interpolated in log(pressure) between reported levels (NaN if outside the profile)."""
    s = prof[["pressure_hpa", col]].dropna().sort_values("pressure_hpa")
    if s.empty or not (s.pressure_hpa.min() <= p <= s.pressure_hpa.max()):
        return np.nan
    return float(np.interp(np.log(p), np.log(s.pressure_hpa), s[col]))


def _crossings(prof: pd.DataFrame, value: float) -> list[float]:
    """Heights (m) where temperature crosses `value` (C), lowest first, by linear interpolation in height."""
    s = prof[["height_m", "temp_c"]].dropna().sort_values("height_m")
    z, t = s.height_m.to_numpy(), s.temp_c.to_numpy() - value
    out = []
    for i in range(len(t) - 1):
        if t[i] == 0:
            out.append(float(z[i]))
        elif t[i] * t[i + 1] < 0:
            out.append(float(z[i] + (z[i + 1] - z[i]) * t[i] / (t[i] - t[i + 1])))
    return out


def summarize(levels: pd.DataFrame, top_hpa: float = 500, bottom_hpa: float = 750) -> pd.DataFrame:
    """One row per sounding with the numbers the snow-ratio and regime work need.

    tmax_layer_c: warmest temperature between bottom_hpa and top_hpa (750 mb is about Alta's height), the observed
                  version of what Kuchera uses. Only real, above-ground levels count.
    freezing_level_m: highest height where the profile crosses 0 C (MSL); ~ the snow level, plus a few hundred m.
    dgz_depth_m: thickness of the -12 to -18 C dendritic growth zone, where the fluffiest snow crystals grow.
    """
    rows = []
    for (station, t), prof in levels.groupby(["station", "time_utc"]):
        layer = prof[(prof.pressure_hpa <= bottom_hpa) & (prof.pressure_hpa >= top_hpa)]
        frz = _crossings(prof, 0.0)
        if not frz and (prof.temp_c.dropna() < 0).all() and prof.temp_c.notna().any():
            frz = [float(prof.dropna(subset=["temp_c"]).height_m.min())]  # below freezing all the way down: report the ground
        # DGZ: from where it first gets to -12 C (going up) to where it first gets to -18 C above that
        z12 = min(_crossings(prof, -12.0), default=np.nan)
        z18 = min((z for z in _crossings(prof, -18.0) if z > z12), default=np.nan)
        row = {"station": station, "time_utc": t, "n_levels": int(prof.temp_c.notna().sum()),
               "tmax_layer_c": float(layer.temp_c.max()) if layer.temp_c.notna().any() else np.nan,
               "freezing_level_m": max(frz) if frz else np.nan,
               "dgz_depth_m": z18 - z12}
        for p in (700, 500):
            t_, td = _at(prof, "temp_c", p), _at(prof, "dewpoint_c", p)
            row[f"t{p}_c"], row[f"rh{p}"] = t_, _rh(t_, td) if not np.isnan(td) else np.nan
            # Winds are only reported at some levels; take the reported level nearest p (within 25 mb).
            w = prof.dropna(subset=["wind_dir", "wind_kt"])
            w = w[(w.pressure_hpa - p).abs() <= 25]
            if len(w):
                near = w.iloc[(w.pressure_hpa - p).abs().argsort()].iloc[0]
                row[f"wdir{p}"], row[f"wspd{p}_kt"] = float(near.wind_dir), float(near.wind_kt)
            else:
                row[f"wdir{p}"], row[f"wspd{p}_kt"] = np.nan, np.nan
        rows.append(row)
    return pd.DataFrame(rows)
