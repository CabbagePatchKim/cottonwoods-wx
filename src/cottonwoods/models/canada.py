"""Canadian models (RDPS 10 km, GDPS 15 km) from Environment Canada's MSC Datamart. Free, no login.

The Datamart keeps only about the last 30 days, and there is no long public archive, so these models must be
archived live (scripts/run_models.bat does this every 3 hours). Each file holds ONE field for one forecast hour:
    https://dd.weather.gc.ca/20261026/WXO-DD/model_rdps/10km/00/012/20261026T00Z_MSC_RDPS_AirTemp_IsbL-0700_RLatLon0.09_PT012H.grib2
RDPS runs 00/06/12/18z to 84 h; GDPS runs only 00/12z, to 240 h. Precip is the total since the run started (mm).
Docs: https://eccc-msc.github.io/open-data/msc-data/readme_en/
"""
from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import requests

from .accum import deaccumulate
from . import fields as fl
from .points import nearest_index

BASE = "https://dd.weather.gc.ca"
TIMEOUT = 120
WORKERS = 8  # parallel downloads; Environment Canada's server handles this comfortably
BATCH = 40  # files held in memory at once (see pull_run)
GRIDS = {"RDPS": ("model_rdps/10km", "RLatLon0.09"), "GDPS": ("model_gdps/15km", "LatLon0.15")}


class NotPosted(Exception):
    """The file isn't on the Datamart: run not finished yet, or older than the ~30-day window."""


def _url(model: str, init: datetime, fxx: int, field: str) -> str:
    folder, grid = GRIDS[model]
    name = f"{init:%Y%m%dT%H}Z_MSC_{model}_{field}_{grid}_PT{fxx:03d}H.grib2"
    return f"{BASE}/{init:%Y%m%d}/WXO-DD/{folder}/{init:%H}/{fxx:03d}/{name}"


def _fields() -> dict[str, str]:
    """Our column name -> Datamart field name. One file each, so the list is kept lean: humidity only from 750 mb up
    and height only at 500 mb (profile.level_heights fills the rest from temperatures)."""
    out = {"precip": "Precip-Accum_Sfc", "psfc_hpa": "Pressure_Sfc", "t2m_k": "AirTemp_AGL-2m",
           "td2m_k": "DewPoint_AGL-2m", "cape_jkg": "CAPE_Sfc", "z_500_m": "GeopotentialHeight_IsbL-0500"}
    out.update({f"t_{lev}_k": f"AirTemp_IsbL-{lev:04d}" for lev in fl.LEVELS_HPA})
    out.update({f"rh_{lev}": f"RelativeHumidity_IsbL-{lev:04d}" for lev in fl.LEVELS_HPA if lev <= 750})
    for lev in fl.WIND_LEVELS:
        out[f"u_{lev}_ms"], out[f"v_{lev}_ms"] = f"WindU_IsbL-{lev:04d}", f"WindV_IsbL-{lev:04d}"
    return out


# Unit fixes to match the other models (checked against a live pull; see tests): pressure Pa -> hPa
SCALE = {"psfc_hpa": 0.01}


def _download(model: str, init: datetime, fxx: int, field: str, session) -> bytes:
    """One single-field file's bytes. Safe to run in threads (no GRIB decoding here)."""
    r = session.get(_url(model, init, fxx, field), timeout=TIMEOUT)
    if r.status_code == 404:
        raise NotPosted(f"{model} {init:%Y-%m-%d %H}z f{fxx:03d} {field}")
    r.raise_for_status()
    return r.content


def _decode_points(content: bytes, points: dict[str, dict]) -> dict:
    """Each point's nearest grid value from one GRIB file. Main thread only: ecCodes, the GRIB decoder,
    crashes the whole program when several threads decode at once."""
    import xarray as xr  # lazy, like the other pullers

    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        # indexpath="" stops cfgrib leaving .idx files next to the temp file
        with xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""}) as ds:
            var = list(ds.data_vars)[0]
            vals, lats, lons = ds[var].values, ds["latitude"].values, ds["longitude"].values
        out = {}
        for name, pt in points.items():
            idx, dist = nearest_index(lats, lons, pt["lat"], pt["lon"])
            out[name] = (float(vals[idx]), dist)
        return out
    finally:
        os.remove(path)


def _read_points(model: str, init: datetime, fxx: int, field: str, points: dict[str, dict], session) -> dict:
    return _decode_points(_download(model, init, fxx, field, session), points)


def pull_run(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None) -> pd.DataFrame:
    """All forecast hours of one RDPS or GDPS run, in the same row format as the HRRR/GFS pullers."""
    fxx_list = [int(f) for f in fxx_list]
    fields = _fields()
    session = requests.Session()
    # Check one file first: if the run isn't posted, stop before firing hundreds of requests.
    _read_points(model, init, fxx_list[-1], fields["precip"], {}, session)

    jobs = [(fxx, col, field) for fxx in fxx_list for col, field in fields.items()]
    values: dict[tuple[int, str], dict] = {}
    failed = []

    def get(job):
        fxx, col, field = job
        try:
            return job, _download(model, init, fxx, field, session)
        except Exception as e:  # noqa: BLE001  (one missing field leaves a gap, not a failed run)
            failed.append((fxx, field, repr(e)))
            return job, None

    # Download in parallel threads, decode one file at a time here (see _decode_points).
    # Batches of BATCH files: downloads run ahead of decoding, so without batches a GDPS run (600 files of
    # 1-4 MB) could sit in memory all at once. A batch is downloaded, decoded and dropped before the next.
    with ThreadPoolExecutor(WORKERS) as pool:
        for i in range(0, len(jobs), BATCH):
            for (fxx, col, field), content in pool.map(get, jobs[i:i + BATCH]):
                if content is not None:
                    try:
                        values[(fxx, col)] = _decode_points(content, points)
                    except Exception as e:  # noqa: BLE001
                        failed.append((fxx, field, repr(e)))
    if failed:
        print(f"[{model.lower()}] {len(failed)} of {len(jobs)} files failed for init {init}: {failed[:2]}")

    t0 = pd.Timestamp(init)
    rows = []
    for name in points:
        # precip is a running total from hour 0, so every record is (0, fxx, total) and deaccumulate differences them
        acc = [(0, fxx, values[(fxx, "precip")][name][0]) for fxx in fxx_list if (fxx, "precip") in values]
        for s, e, amt in deaccumulate(acc):
            row = {"model": model, "init": t0, "lead_hr": e, "valid_start": t0 + pd.Timedelta(hours=s),
                   "valid_end": t0 + pd.Timedelta(hours=e), "point": name, "qpf_mm": amt, "source": "msc"}
            for col in fields:
                if col != "precip" and (e, col) in values:
                    v, dist = values[(e, col)][name]
                    row[col] = v * SCALE.get(col, 1.0)
                    row["grid_dist_km"] = dist
            rows.append(row)
    return pd.DataFrame(rows)
