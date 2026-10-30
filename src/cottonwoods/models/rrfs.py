"""RRFS point extraction via Herbie (NOAA Rapid Refresh Forecast System, 3 km; owner, 2026-10-30).

RRFS became operational on 2026-11-03 (NWS SCN 26-48), replacing NAM, NAM Nest, HREF and SREF. Files are on AWS
(noaa-rrfs-ops-pds), with .idx inventories, so only the fields needed here are downloaded (like HRRR):
  RRFS      deterministic, product 2dfld (surface) + prslev (45 pressure levels), CONUS 3 km
  RRFSENS   the 5 ensemble members m001-m005, products 2dfldnomads + prslevnomads (14 levels: no 650 / 550 mb, so
            those columns are simply absent), CONUS 3 km. Members carry temperatures, so each gets its own snow.
Precipitation and RRFS's own snowfall (ASNOW, its built-in snow, kept as snow_native_in to compare with ours) are
read as totals since init ("0-N hour acc") and turned into steps (accum.deaccumulate), so 1 h and 3 h steps work
the same and a missing hour can't double-count.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .accum import deaccumulate
from .fields import NCEP_EXTRA, NCEP_LEVELS, NCEP_SURFACE, open_grib, parallel_hours, point_fields
from .points import nearest_index
from .registry import REGISTRY

M_TO_IN = 39.3701


def since_init(field: str, fxx: int) -> str:
    """Inventory search for a total since init. At whole days the files say "0-1 day acc", not "0-24 hour acc"
    (missing that lost hours 24 / 48 / 72 of every run in the first real test, 2026-10-30)."""
    span = f"(?:{fxx} hour|{fxx // 24} day)" if fxx % 24 == 0 else f"{fxx} hour"
    return f":{field}:surface:0-{span} acc"


def _first_var(ds_list):
    """(values, lats, lons) of the first data variable in what cfgrib returned for a one-field search."""
    ds = ds_list[0]
    v = list(ds.data_vars)[0]
    return ds[v].values, ds["latitude"].values, ds["longitude"].values


def pull_run(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
             workers: int) -> pd.DataFrame:
    """Every forecast hour of one RRFS run (model "RRFS") or of its 5 members ("RRFSENS", a `member` column)."""
    from herbie import Herbie  # imported lazily so offline tests don't need herbie

    ens = REGISTRY[model].members > 0
    members = list(range(1, REGISTRY[model].members + 1)) if ens else [None]
    sfc, prs = ("2dfldnomads", "prslevnomads") if ens else ("2dfld", "prslev")
    kw = {"save_dir": save_dir} if save_dir else {}
    jobs = [(m, int(f)) for m in members for f in fxx_list]

    def download(job):
        m, fxx = job
        extra = {"member": m} if m is not None else {}
        h_sfc = Herbie(init, model="rrfs", product=sfc, fxx=fxx, verbose=False, **extra, **kw)
        h_prs = Herbie(init, model="rrfs", product=prs, fxx=fxx, verbose=False, **extra, **kw)
        return {"apcp": h_sfc.download(since_init("APCP", fxx), verbose=False),
                "asnow": h_sfc.download(since_init("ASNOW", fxx), verbose=False),
                "fields": [h_sfc.download(NCEP_SURFACE, verbose=False), h_prs.download(NCEP_LEVELS, verbose=False),
                           h_prs.download(NCEP_EXTRA, verbose=False)]}

    totals, fields = {}, {}

    def decode(job, paths):
        apcp, lats, lons = _first_var(open_grib(paths["apcp"]))
        try:
            asnow = _first_var(open_grib(paths["asnow"]))[0]
        except Exception:  # noqa: BLE001  (own snowfall is a comparison extra; a run without it still counts)
            asnow = None
        f = point_fields([open_grib(p) for p in paths["fields"]], points)
        for name, pt in points.items():
            idx, dist = nearest_index(lats, lons, pt["lat"], pt["lon"])
            totals[(job, name)] = (float(apcp[idx]), float(asnow[idx]) * M_TO_IN if asnow is not None else np.nan, dist)
            fields[(job, name)] = f[name]

    parallel_hours(download, decode, jobs, workers, label=f"{model.lower()} {init:%Y-%m-%d %H}z")
    t0, rows = pd.Timestamp(init), []
    for m in members:
        for name in points:
            have = [f for f in (int(x) for x in fxx_list) if ((m, f), name) in totals]
            water = deaccumulate([(0, f, totals[((m, f), name)][0]) for f in have])
            snow = dict(((a, b), v) for a, b, v in deaccumulate([(0, f, totals[((m, f), name)][1]) for f in have]))
            for a, b, amt in water:
                row = {"model": model, "init": t0, "lead_hr": b, "valid_start": t0 + pd.Timedelta(hours=a),
                       "valid_end": t0 + pd.Timedelta(hours=b), "point": name, "grid_dist_km": totals[((m, b), name)][2],
                       "qpf_mm": amt, "snow_native_in": snow.get((a, b), np.nan), **fields[((m, b), name)]}
                if m is not None:
                    row["member"] = m
                rows.append(row)
    return pd.DataFrame(rows).sort_values(["lead_hr", "point"]).reset_index(drop=True) if rows else pd.DataFrame()
