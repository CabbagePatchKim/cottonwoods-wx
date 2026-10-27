"""GFS point extraction. pull_run() takes each run from the NCAR GDEX archive when it has it (fast server-side
subsets, 2015 onward; see gfs_gdex.py) and otherwise from AWS via Herbie (0.25 degree pgrb2, 2021 onward + live).

UNTESTED against live data: written offline. Things to verify on first run:
  - the APCP accumulation window text in Herbie's inventory (parsed in _pull_run_aws)
  - variable names in the xarray datasets (tp / t)
Output rows match the HRRR puller so both land in the same archive schema.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .accum import choose_precip, deaccumulate
from . import fields as fl
from .fields import open_grib, parallel_hours
from .points import nearest_index


def pull_run(init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
             workers: int, gdex_workers: int) -> pd.DataFrame:
    """One GFS run: GDEX first, AWS (Herbie) if GDEX doesn't have it. Same model data either way."""
    from . import gfs_gdex

    try:
        return gfs_gdex.pull_run(init, fxx_list, points, workers=gdex_workers)
    except gfs_gdex.NotInArchive:
        return _pull_run_aws(init, fxx_list, points, save_dir, workers=workers)


def _pull_run_aws(init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
                  workers: int) -> pd.DataFrame:
    """GFS from AWS: forecast hours downloaded in parallel threads, decoded one at a time (fields.parallel_hours)."""
    import re

    from herbie import Herbie  # lazy: offline tests don't need herbie

    kw = {"save_dir": save_dir} if save_dir else {}
    # GFS precip comes in overlapping buckets, so first collect every raw bucket per point, and turn them
    # into per-step amounts only after all hours are read (deaccumulate, below).
    acc: dict[str, list] = {p: [] for p in points}       # point -> [(start_hr, end_hr, mm), ...]
    temps: dict[tuple[str, int], dict] = {}              # (point, hour) -> profile fields
    dist: dict[str, float] = {}
    elev: dict[str, float] = {}                          # model ground height (m) of each point's cell
    fxx_list = [int(f) for f in fxx_list]

    def download(fxx):
        h = Herbie(init, model="gfs", product="pgrb2.0p25", fxx=fxx, verbose=False, **kw)
        # the since-start total when the file has it (GFS has carried both since 2021), else the bucket
        pick = choose_precip(list(h.inventory("APCP:surface")["search_this"]), fxx)
        if pick is None:
            raise RuntimeError(f"no precip message ending at f{fxx:03d}")
        line, window = pick
        paths = [h.download(s, verbose=False) for s in (fl.NCEP_LEVELS_ALL_HGT, fl.NCEP_WIND, fl.NCEP_SURFACE)]
        # terrain never changes between forecast hours, so it's read with the first hour only
        hgt = h.download("HGT:surface", verbose=False) if fxx == fxx_list[0] else None
        return window, h.download(re.escape(line), verbose=False), paths, hgt

    def decode(fxx, got):
        (a, b), p_path, f_paths, hgt = got
        ds_p = open_grib(p_path)[0]
        pvar = [v for v in ds_p.data_vars if v in ("tp", "APCP")][0]
        lats, lons = ds_p["latitude"].values, ds_p["longitude"].values
        # profile fields: temps, humidity, heights on every level (0.25 deg fields are small), wind, 2 m, CAPE
        fvals = fl.point_fields([open_grib(p) for p in f_paths], points)
        for name, pt in points.items():
            idx, d = nearest_index(lats, lons, pt["lat"], pt["lon"])
            dist[name] = d
            acc[name].append((a, b, float(ds_p[pvar].values[idx])))
            temps[(name, b)] = fvals[name]
        if hgt is not None:
            ds_h = open_grib(hgt)[0]
            hvar = [v for v in ds_h.data_vars if v in ("orog", "gh", "HGT")][0]
            for name, pt in points.items():
                elev[name] = float(ds_h[hvar].values[nearest_index(lats, lons, pt["lat"], pt["lon"])[0]])

    parallel_hours(download, decode, fxx_list, workers, label=f"gfs {init:%Y-%m-%d %H}z")

    t0 = pd.Timestamp(init)
    rows = []
    # Second pass: one output row per point per step, matching the HRRR puller's columns.
    for name in points:
        for s, e, amt in deaccumulate(acc[name]):
            row = {
                "model": "GFS",
                "init": t0,
                "lead_hr": e,
                "valid_start": t0 + pd.Timedelta(hours=s),
                "valid_end": t0 + pd.Timedelta(hours=e),
                "point": name,
                "grid_dist_km": dist.get(name),
                "grid_elev_m": elev.get(name),
                "qpf_mm": amt,
                "source": "aws",
            }
            row.update(temps.get((name, e), {}))  # add the profile columns for the step's end hour
            rows.append(row)
    return pd.DataFrame(rows)
