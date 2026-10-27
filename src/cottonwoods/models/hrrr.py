"""HRRR point extraction via Herbie.

Reference for HRRR fields: https://herbie.readthedocs.io (model="hrrr", product="sfc" or "prs").
Output is the forecast-evolution table row format (columns listed in models/fields.py):
    model, init, valid_start, valid_end, lead_hr, point, qpf_mm, psfc_hpa, t_<lev>_k, rh_<lev>, t2m_k, ...
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .fields import NCEP_EXTRA, NCEP_LEVELS, NCEP_SURFACE, open_grib, parallel_hours, point_fields
from .points import nearest_index

def pull_run(init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None,
             workers: int = 6) -> pd.DataFrame:
    """All forecast hours of one run: downloads in `workers` parallel threads, decoding one at a time.

    HRRR sfc files hold TWO precip fields after f001: the total since init ("0-12 hour acc") and the last hour
    ("11-12 hour acc"). We ask for the last hour by name; taking the first match summed running totals (10.7"
    instead of ~1.1" for the 12/8/2023 storm). Surface fields (pressure, 2 m temp/dewpoint, CAPE) come from the
    sfc product; temperature, humidity, 500 mb height and wind on pressure levels from the prs product.
    """
    from herbie import Herbie  # imported lazily so offline tests don't need herbie

    kw = {"save_dir": save_dir} if save_dir else {}

    def download(fxx):
        # Runs in a thread: find the files and byte-range download the messages we need (no decoding here)
        h_sfc = Herbie(init, model="hrrr", product="sfc", fxx=fxx, verbose=False, **kw)
        h_prs = Herbie(init, model="hrrr", product="prs", fxx=fxx, verbose=False, **kw)
        return {"precip": h_sfc.download(f"APCP:surface:{fxx - 1}-{fxx} hour acc", verbose=False),
                "fields": [h_sfc.download(NCEP_SURFACE, verbose=False), h_prs.download(NCEP_LEVELS, verbose=False),
                           h_prs.download(NCEP_EXTRA, verbose=False)]}

    rows = []

    def decode(fxx, paths):
        ds_p = open_grib(paths["precip"])[0]
        apcp_name = [v for v in ds_p.data_vars if v in ("tp", "APCP")][0]
        lats, lons = ds_p["latitude"].values, ds_p["longitude"].values
        fields = point_fields([open_grib(p) for p in paths["fields"]], points)
        for name, pt in points.items():
            idx, dist_km = nearest_index(lats, lons, pt["lat"], pt["lon"])
            rows.append({"model": "HRRR", "init": pd.Timestamp(init), "lead_hr": fxx,
                         "valid_end": pd.Timestamp(init) + pd.Timedelta(hours=fxx),
                         "valid_start": pd.Timestamp(init) + pd.Timedelta(hours=fxx - 1),
                         "point": name, "grid_dist_km": dist_km, "qpf_mm": float(ds_p[apcp_name].values[idx]),
                         **fields[name]})

    parallel_hours(download, decode, [int(f) for f in fxx_list], workers, label=f"hrrr {init:%Y-%m-%d %H}z")
    return pd.DataFrame(rows).sort_values(["lead_hr", "point"]).reset_index(drop=True) if rows else pd.DataFrame()
