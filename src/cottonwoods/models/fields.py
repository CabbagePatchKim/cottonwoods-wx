"""Shared field lists and point extraction for the Herbie-based pullers (HRRR, NAM, NAM Nest, GFS on AWS, ECMWF).

Every deterministic model saves the same columns per step, so cottonwoods.profile can move any of them to a
station's real elevation:
    t_<lev>_k   temperature        rh_<lev>   relative humidity (%)     z_<lev>_m   geopotential height
    u_<lev>_ms, v_<lev>_ms  wind (700 and 500 mb; for the Veals snow-ratio model later)
    t2m_k, td2m_k  2 m temperature and dewpoint     psfc_hpa  surface pressure     cape_jkg  CAPE
Heights: the big high-resolution grids (HRRR, NAM Nest) send height only at 500 mb, to keep downloads down;
profile.level_heights fills the other levels from the temperatures (hypsometric equation).
"""
from __future__ import annotations

import re
import warnings

import numpy as np

# cfgrib prints an xarray "default value for compat will change" FutureWarning for every multi-field read;
# it doesn't affect results and buries real messages in the logs.
warnings.filterwarnings("ignore", category=FutureWarning, module="cfgrib")
# Herbie warns "This pattern is interpreted as a regular expression, and has match groups" for every field search
# (our searches are regexes on purpose): ~1,300 lines per pass that buried the real messages (owner, 2026-10-30)
warnings.filterwarnings("ignore", message="This pattern is interpreted as a regular expression", category=UserWarning)

from .points import nearest_index

LEVELS_HPA = (850, 800, 750, 700, 650, 600, 550, 500)
WIND_LEVELS = (700, 500)
_lv = "|".join(str(lev) for lev in LEVELS_HPA)
_wl = "|".join(str(lev) for lev in WIND_LEVELS)

# Inventory regexes (NCEP GRIB names; ECMWF uses its own short names)
NCEP_LEVELS = rf":(TMP|RH):({_lv}) mb:"
NCEP_LEVELS_ALL_HGT = rf":(TMP|RH|HGT):({_lv}) mb:"
NCEP_EXTRA = rf":HGT:500 mb:|:(UGRD|VGRD):({_wl}) mb:"
NCEP_WIND = rf":(UGRD|VGRD):({_wl}) mb:"
NCEP_SURFACE = r":PRES:surface:|:(TMP|DPT):2 m above ground:|:CAPE:surface:"
ECMWF_LEVELS = rf":(t|r|gh):({_lv}):pl:|:(u|v):({_wl}):pl:"
ECMWF_SURFACE = r":(2t|2d|sp|mucape):sfc:"

# cfgrib variable name -> our column (levels) or (column, scale) for single-level fields
LEVEL_COLS = {"t": "t_{lev}_k", "r": "rh_{lev}", "gh": "z_{lev}_m", "u": "u_{lev}_ms", "v": "v_{lev}_ms"}
SURFACE_COLS = {"t2m": ("t2m_k", 1.0), "d2m": ("td2m_k", 1.0), "sp": ("psfc_hpa", 0.01),
                "cape": ("cape_jkg", 1.0), "mucape": ("cape_jkg", 1.0)}


def grid_shape(lats, lons) -> tuple:
    """Shape of the 2-D field: HRRR/NAM store 2-D lat/lon arrays; GFS/ECMWF store 1-D lat and lon lists."""
    return lats.shape if lats.ndim == 2 else (lats.size, lons.size)


def point_fields(datasets, points: dict[str, dict]) -> dict[str, dict]:
    """{point: {column: value}} from one or more xarray Datasets (whatever cfgrib returned for a search)."""
    # Herbie returns a Dataset or a list of them per search; flatten whatever nesting comes in
    flat, todo = [], [datasets]
    while todo:
        d = todo.pop()
        if isinstance(d, (list, tuple)):
            todo.extend(d)
        elif d is not None:
            flat.append(d)
    datasets = flat
    out: dict[str, dict] = {name: {} for name in points}
    for ds in datasets:
        lats, lons = ds["latitude"].values, ds["longitude"].values
        shape = grid_shape(lats, lons)
        idx = {name: nearest_index(lats, lons, pt["lat"], pt["lon"])[0] for name, pt in points.items()}
        for var in ds.data_vars:
            da = ds[var]
            if var in LEVEL_COLS and "isobaricInhPa" in da.coords:
                levs = np.atleast_1d(da["isobaricInhPa"].values)
                vals = da.values.reshape(len(levs), *shape) if "isobaricInhPa" in da.dims else da.values.reshape(1, *shape)
                for k, lev in enumerate(levs):
                    col = LEVEL_COLS[var].format(lev=int(lev))
                    for name in points:
                        out[name][col] = float(vals[(k, *idx[name])])
            elif var in SURFACE_COLS:
                col, scale = SURFACE_COLS[var]
                vals = da.values.reshape(shape)
                for name in points:
                    out[name][col] = float(vals[idx[name]]) * scale
    return out


# --- parallel downloads, serial decoding ---------------------------------------------------------
# Most of a run's time is waiting on the network (an HRRR hour: ~13 s downloading 19 MB vs ~3 s decoding), so
# forecast hours are DOWNLOADED in parallel threads. DECODING stays on one thread: ecCodes (the GRIB decoder)
# crashes the program when several threads decode at once. Results are decoded as soon as each download finishes.

def open_grib(path) -> list:
    """Every dataset in a downloaded GRIB subset, then delete the file.

    cfgrib.open_datasets, not xarray.open_dataset: the single-dataset reader silently drops fields whose level
    coordinates clash (e.g. wind at 700+500 mb next to height at 500 mb only).
    """
    import os

    import cfgrib

    try:
        dsets = [d.load() for d in cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return dsets


THROTTLED = re.compile(r"\b(503|429)\b|slow ?down|too many requests", re.I)


def parallel_hours(download, decode, items, workers: int = 6, label: str = "", retries: int = 2,
                   throttle: dict | None = None, sleep=None) -> list:
    """Run download(item) in `workers` threads and decode(item, downloaded) on this thread as each finishes.

    A failed download is retried `retries` more times after a short pause: dropped connections ("forcibly closed by
    the remote host") are common and usually succeed on a second try. A server THROTTLING us (503 Slow Down, 429;
    ECMWF's S3 bucket did this to the cloud pass 10/28, leaving most ENS members missing) needs a real pause:
    with `throttle` (config archive.throttle) those errors get throttle["retries"] tries, throttle["wait_s"] x try
    apart. Returns [(item, error)] for items whose download or decode still failed (the rest are handled by decode).
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sleep = sleep or time.sleep

    def with_retry(it):
        attempt = throttled = 0
        while True:
            try:
                return download(it)
            except Exception as e:  # noqa: BLE001
                if throttle and THROTTLED.search(str(e)) and throttled < throttle["retries"]:
                    throttled += 1
                    sleep(throttle["wait_s"] * throttled)
                    continue
                if attempt == retries:
                    raise
                sleep(2 + 3 * attempt)
                attempt += 1

    failed = []
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(with_retry, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                decode(it, fut.result())
            except Exception as e:  # noqa: BLE001  (one bad hour is a gap; archive_run checks completeness)
                failed.append((it, repr(e)[:150]))
    if failed and label:
        print(f"[{label}] {len(failed)} of {len(items)} failed: {failed[:2]}", flush=True)
    return failed
