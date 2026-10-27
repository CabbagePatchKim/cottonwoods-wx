"""GFS from the NCAR GDEX historical archive (dataset d084001, 0.25 degree, 2015-01-15 onward), via THREDDS NCSS.

NCSS (NetCDF Subset Service) cuts the requested variables and a small lat/lon box out of each GRIB file on
NCAR's server, so one forecast hour costs ~5 KB and ~2 s instead of a 250-300 MB download. Free, no login,
CC BY 4.0 (cite: NCEP/NWS/NOAA, doi:10.5065/D65D8PWK). NCAR said it would stop adding new runs in early 2026;
gfs.pull_run falls back to AWS (Herbie) for runs GDEX doesn't have.

Output rows match gfs.pull_run / hrrr.pull_run, plus grid_elev_m (model ground height of the cell used).
"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from .accum import deaccumulate
from . import fields as fl
from .points import nearest_index

NCSS = "https://thredds.rda.ucar.edu/thredds/ncss/grid/files/g/d084001"
TIMEOUT = 90


class NotInArchive(Exception):
    """GDEX has no file for this run/hour (before 2015-01-15, after updates stopped, or a gap)."""


def _url(init: datetime, fxx: int) -> str:
    return f"{NCSS}/{init:%Y}/{init:%Y%m%d}/gfs.0p25.{init:%Y%m%d%H}.f{fxx:03d}.grib2"


def _bbox(points: dict[str, dict], pad: float = 0.3) -> dict:
    """A box around all points, padded so the 2x2 GFS cells around each point are included."""
    lats = [p["lat"] for p in points.values()]
    lons = [p["lon"] for p in points.values()]
    return {"north": max(lats) + pad, "south": min(lats) - pad, "west": min(lons) - pad, "east": max(lons) + pad}


MIXED = "Total_precipitation_surface_Mixed_intervals_Accumulation"
LEVEL_VARS = {"Temperature_isobaric": "t_{lev}_k", "Relative_humidity_isobaric": "rh_{lev}",
              "Geopotential_height_isobaric": "z_{lev}_m", "u-component_of_wind_isobaric": "u_{lev}_ms",
              "v-component_of_wind_isobaric": "v_{lev}_ms"}
SURFACE_VARS = {"Geopotential_height_surface": ("grid_elev_m", 1.0), "Pressure_surface": ("psfc_hpa", 0.01),
                "Temperature_height_above_ground": ("t2m_k", 1.0),
                "Dewpoint_temperature_height_above_ground": ("td2m_k", 1.0),
                "Convective_available_potential_energy_surface": ("cape_jkg", 1.0)}
# THREDDS names of the profile fields (see models/fields.py for what each column is)
PROFILE_VARS = ["Temperature_isobaric", "Relative_humidity_isobaric", "Geopotential_height_isobaric",
                "u-component_of_wind_isobaric", "v-component_of_wind_isobaric", "Geopotential_height_surface",
                "Pressure_surface", "Temperature_height_above_ground", "Dewpoint_temperature_height_above_ground",
                "Convective_available_potential_energy_surface"]


def _precip_vars(fxx: int) -> list[str]:
    """Names the precip field can have, most likely first.

    Older files hold one bucket per hour, named by its length (buckets reset every 6 h: f003, f009, ... are 3 h
    totals; f006, f012, ... are 6 h). Newer files (GFS v16, 2021 on) also hold the total since the run started,
    so THREDDS merges both into one "Mixed_intervals" variable with two time windows.
    """
    return [f"Total_precipitation_surface_{3 if fxx % 6 else 6}_Hour_Accumulation", MIXED]


def fetch_hour(init: datetime, fxx: int, points: dict[str, dict], session: requests.Session | None = None):
    """One forecast hour from GDEX as an xarray Dataset (precip, isobaric temperature, surface height)."""
    import xarray as xr  # lazy, like the Herbie pullers

    for pvar in _precip_vars(fxx):
        # temporal=all: without it the server fails ("Index out of range") on the two-window Mixed_intervals field
        params = [("var", pvar), *[("var", v) for v in PROFILE_VARS],
                  *_bbox(points).items(), ("horizStride", 1), ("accept", "netcdf3"), ("temporal", "all")]
        r = (session or requests).get(_url(init, fxx), params=params, timeout=TIMEOUT)
        if r.status_code == 404:
            raise NotInArchive(f"GDEX has no GFS {init:%Y-%m-%d %H}z f{fxx:03d}")
        if r.status_code == 400 and "not contained" in r.text:
            continue  # this file names precip the other way; try the next name
        r.raise_for_status()
        # netcdf3 bytes -> Dataset in memory; the scipy engine reads netcdf3 without extra libraries
        return xr.open_dataset(io.BytesIO(r.content), engine="scipy").load()
    raise RuntimeError(f"GFS {init:%Y-%m-%d %H}z f{fxx:03d}: no known precip variable name in the GDEX file")


def _rows_from_ds(ds, init: datetime, fxx: int, points: dict[str, dict]) -> tuple[list, dict]:
    """Pull each point's nearest cell out of one hour's subset: accumulation record + temps/elevation."""
    pvar = [v for v in ds.data_vars if v.startswith("Total_precipitation")][0]
    precip = ds[pvar]
    tdim = precip.dims[0]
    # The file states each accumulation window (e.g. 6 -> 9 h); trust it over the f003/f006 rule. With two
    # windows (newer files), use the one starting at hour 0: the total since the run began.
    t0 = np.datetime64(pd.Timestamp(init))
    windows = [tuple(int((x - t0) / np.timedelta64(1, "h")) for x in w) for w in ds[f"{tdim}_bounds"].values]
    k = next((n for n, w in enumerate(windows) if w[0] == 0), max(range(len(windows)), key=lambda n: windows[n][0]))
    a, b = windows[k]
    pgrid = precip.isel({tdim: k}).values
    lats, lons = ds["latitude"].values, ds["longitude"].values
    acc, extra = {}, {}
    for name, pt in points.items():
        (i, j), dist = nearest_index(lats, lons, pt["lat"], pt["lon"])
        acc[name] = (a, b, float(pgrid.reshape(len(lats), len(lons))[i, j]))
        row = {"grid_dist_km": dist}
        for var, col in LEVEL_VARS.items():
            if var not in ds:
                continue
            da = ds[var]
            # The pressure axis name changes between files and variables (isobaric, isobaric4, ...)
            pdim = [d for d in da.dims if d.startswith("isobaric")][0]
            levs = [lev for lev in (fl.WIND_LEVELS if var.startswith(("u-", "v-")) else fl.LEVELS_HPA)]
            for lev in levs:
                # levels are in Pa in these files: 700 hPa = 70000 Pa
                sel = da.sel({pdim: lev * 100.0}, method="nearest")
                row[col.format(lev=lev)] = float(sel.values.reshape(len(lats), len(lons))[i, j])
        for var, (col, scale) in SURFACE_VARS.items():
            if var not in ds:
                continue
            da = ds[var]
            hdim = [d for d in da.dims if d.startswith("height_above_ground")]
            if hdim:  # 2 m temperature/dewpoint share an axis with other heights (80 m, 100 m)
                da = da.sel({hdim[0]: 2.0}, method="nearest")
            row[col] = float(np.asarray(da.values).reshape(len(lats), len(lons))[i, j]) * scale
        extra[name] = row
    return acc, extra


def pull_run(init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
             workers: int) -> pd.DataFrame:
    """All forecast hours of one GFS run from GDEX. Raises NotInArchive if GDEX doesn't have the run.
    workers: parallel requests (config archive.download_workers.gdex; kept low, it's a free public server)."""
    fxx_list = [int(f) for f in fxx_list]
    # Check the first hour on its own: if GDEX lacks the run, stop before firing dozens of requests.
    session = requests.Session()
    first = fetch_hour(init, fxx_list[0], points, session)
    results = {fxx_list[0]: first}
    failed = []

    def get(fxx):
        try:
            return fxx, fetch_hour(init, fxx, points, session)
        except Exception as e:  # noqa: BLE001  (a single missing hour is a gap, not a failed run)
            failed.append((fxx, repr(e)))
            return fxx, None

    with ThreadPoolExecutor(workers) as pool:
        for fxx, ds in pool.map(get, fxx_list[1:]):
            if ds is not None:
                results[fxx] = ds
    if failed:
        print(f"[gfs-gdex] {len(failed)} forecast hours failed for init {init}: {failed[:3]}{' ...' if len(failed) > 3 else ''}")

    acc: dict[str, list] = {p: [] for p in points}       # point -> [(start_hr, end_hr, mm), ...]
    extra: dict[tuple[str, int], dict] = {}              # (point, end hour) -> temps, elevation, distance
    for fxx in sorted(results):
        rec, ex = _rows_from_ds(results[fxx], init, fxx, points)
        for name in points:
            acc[name].append(rec[name])
            extra[(name, rec[name][1])] = ex[name]

    t0 = pd.Timestamp(init)
    rows = []
    for name in points:
        for s, e, amt in deaccumulate(acc[name]):
            row = {"model": "GFS", "init": t0, "lead_hr": e, "valid_start": t0 + pd.Timedelta(hours=s),
                   "valid_end": t0 + pd.Timedelta(hours=e), "point": name, "qpf_mm": amt, "source": "gdex"}
            row.update(extra.get((name, e), {}))
            rows.append(row)
    return pd.DataFrame(rows)
