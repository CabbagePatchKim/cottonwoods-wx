"""Canadian models (RDPS 10 km, GDPS 15 km) from Environment Canada's MSC Datamart. Free, no login.

The Datamart keeps only about the last 30 days, and there is no long public archive, so these models must be
archived live (scripts/run_models.bat does this every 3 hours). Each file holds ONE field for one forecast hour:
    https://dd.weather.gc.ca/20261026/WXO-DD/model_rdps/10km/00/012/20261026T00Z_MSC_RDPS_AirTemp_IsbL-0700_RLatLon0.09_PT012H.grib2
RDPS runs 00/06/12/18z to 84 h; GDPS runs only 00/12z, to 240 h. Precip is the total since the run started (mm).
Docs: https://eccc-msc.github.io/open-data/msc-data/readme_en/
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from .accum import deaccumulate
from . import fields as fl
from .points import nearest_index

BASE = "https://dd.weather.gc.ca"
TIMEOUT = 120
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


_GRIDS: dict = {}   # grid description -> (lats, lons, shape); a run's ~1,400 files share one or two grids


def _grid(gid):
    """Latitudes/longitudes of a message's grid, read once per grid: 1-D for a regular lat-lon grid (GDPS),
    2-D for the rotated one (RDPS)."""
    import eccodes

    key = tuple(eccodes.codes_get(gid, k) for k in ("gridType", "Ni", "Nj", "latitudeOfFirstGridPointInDegrees",
                                                    "longitudeOfFirstGridPointInDegrees"))
    if key not in _GRIDS:
        ni, nj = key[1], key[2]
        if key[0] == "regular_ll":
            _GRIDS[key] = (eccodes.codes_get_array(gid, "distinctLatitudes"),
                           eccodes.codes_get_array(gid, "distinctLongitudes"), (nj, ni))
        else:
            _GRIDS[key] = (eccodes.codes_get_array(gid, "latitudes").reshape(nj, ni),
                           eccodes.codes_get_array(gid, "longitudes").reshape(nj, ni), (nj, ni))
    return _GRIDS[key]


def _decode_points(content: bytes, points: dict[str, dict]) -> dict:
    """Each point's nearest grid value from one single-field GRIB file, decoded straight from memory with ecCodes
    (identical values to the cfgrib route it replaced, without a temp file or xarray: checked on GDPS and RDPS
    fields 2026-10-27). Main thread only: ecCodes crashes the whole program when several threads decode at once."""
    import eccodes  # lazy, like the other pullers

    gid = eccodes.codes_new_from_message(content)
    try:
        vals = eccodes.codes_get_values(gid)
        if eccodes.codes_get(gid, "bitmapPresent"):
            vals = np.where(vals == eccodes.codes_get(gid, "missingValue"), np.nan, vals)
        lats, lons, shape = _grid(gid)
    finally:
        eccodes.codes_release(gid)
    vals = vals.reshape(shape)
    out = {}
    for name, pt in points.items():
        idx, dist = nearest_index(lats, lons, pt["lat"], pt["lon"])
        out[name] = (float(vals[idx]), dist)
    return out


def _read_points(model: str, init: datetime, fxx: int, field: str, points: dict[str, dict], session) -> dict:
    return _decode_points(_download(model, init, fxx, field, session), points)


def pull_run(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
             workers: int) -> pd.DataFrame:
    """All forecast hours of one RDPS or GDPS run, in the same row format as the HRRR/GFS pullers.
    workers: parallel downloads (config archive.download_workers.canada)."""
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
    with ThreadPoolExecutor(workers) as pool:
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
