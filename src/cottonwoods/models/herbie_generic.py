"""Point pullers for the remaining Herbie models: NAM, NAM Nest, ECMWF HRES (deterministic) and GEFS, ECMWF ENS
(ensembles, precip only). HRRR and GFS keep their own pullers (hrrr.py, gfs.py).

The idea is the same for every model: for each forecast hour, read the file's inventory (.idx), download only
the messages we need (byte ranges), and pull the nearest grid value at each point. Precip windows are read
from the inventory text instead of assumed, because every model stores them differently:
    NAM:       "0-12 hour acc" (since start) and "9-12 hour acc" (bucket) in the same file
    NAM Nest:  "9-12 hour acc" (3 h buckets)
    GEFS:      "6-9" / "6-12 hour acc" (buckets that reset every 6 h)
    ECMWF:     tp = total since start, in METERS (inventory has no window text)
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from .accum import deaccumulate, parse_acc_window
from . import fields as fl
from .fields import open_grib, parallel_hours
from .points import nearest_index


# model name -> Herbie settings and how its inventory names things
SPECS = {
    # "fields": inventory searches for the profile fields (models/fields.py); NAM's 12 km grid is small, so it
    # sends height on every level; NAM Nest's 3 km grid only at 500 mb (the rest is computed from temperatures)
    "NAM":     {"herbie": {"model": "nam", "product": "awphys"}, "precip": r":APCP:surface:",
                "fields": [fl.NCEP_LEVELS_ALL_HGT, fl.NCEP_WIND, fl.NCEP_SURFACE]},
    "NAMNEST": {"herbie": {"model": "nam", "product": "conusnest.hiresf"}, "precip": r":APCP:surface:",
                "fields": [fl.NCEP_LEVELS, fl.NCEP_EXTRA, fl.NCEP_SURFACE]},
    "IFS":     {"herbie": {"model": "ifs", "product": "oper"}, "precip": r":tp:sfc:", "precip_m": True,
                "fields": [fl.ECMWF_LEVELS, fl.ECMWF_SURFACE]},
    "GEFS":    {"herbie": {"model": "gefs", "product": "atmos.5"}, "precip": r":APCP:surface:", "members": 31},
    # ENS open data on this source holds the 50 perturbed members only (no control run)
    "ENS":     {"herbie": {"model": "ifs", "product": "enfo"}, "precip": r":tp:sfc:", "precip_m": True, "members": 50},
}


def choose_precip(lines: list[str], fxx: int, cumulative_only: bool = False) -> tuple[str, tuple[int, int]] | None:
    """Pick one precip message from inventory lines: the since-start total if present, else the latest bucket.

    Returns (line, (start_hr, end_hr)). Lines without window text (ECMWF tp) are a total from hour 0.
    """
    found = []
    for line in lines:
        win = parse_acc_window(line)
        found.append((line, win if win else (0, fxx)))
    if not found:
        return None
    zero = [f for f in found if f[1][0] == 0 and f[1][1] == fxx]
    if zero or cumulative_only:
        return zero[0] if zero else None
    ends = [f for f in found if f[1][1] == fxx]
    return max(ends, key=lambda f: f[1][0]) if ends else None


def _values(ds, points: dict[str, dict], scale: float = 1.0, member_dim: str | None = None) -> dict:
    """{point: value} (or {point: {member: value}}) at each point's nearest grid cell."""
    var = list(ds.data_vars)[0]
    lats, lons = ds["latitude"].values, ds["longitude"].values
    out = {}
    for name, pt in points.items():
        idx, dist = nearest_index(lats, lons, pt["lat"], pt["lon"])
        arr = ds[var]
        if member_dim:
            # ENS: one array per member along `member_dim`; index the grid part only
            vals = arr.values.reshape(arr.shape[0], *fl.grid_shape(lats, lons))[(slice(None), *idx)] * scale
            out[name] = ({int(m): float(v) for m, v in zip(ds[member_dim].values, vals)}, dist)
        else:
            out[name] = (float(arr.values.reshape(fl.grid_shape(lats, lons))[idx]) * scale, dist)
    return out


def pull_run(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None,
             workers: int = 6) -> pd.DataFrame:
    """One run of a deterministic model (NAM, NAMNEST, IFS): per-step precip plus the profile fields.

    Forecast hours are downloaded in parallel threads and decoded one at a time (fields.parallel_hours).
    """
    from herbie import Herbie  # lazy: offline tests don't need herbie

    spec = SPECS[model]
    kw = {"save_dir": save_dir} if save_dir else {}
    scale = 1000.0 if spec.get("precip_m") else 1.0  # ECMWF precip is in meters -> mm
    acc: dict[str, list] = {p: [] for p in points}
    extra: dict[tuple[str, int], dict] = {}

    def download(fxx):
        h = Herbie(init, fxx=fxx, verbose=False, **spec["herbie"], **kw)
        pick = choose_precip(list(h.inventory(spec["precip"])["search_this"]), fxx)
        if pick is None:
            raise RuntimeError(f"no precip message ending at f{fxx:03d}")
        line, window = pick
        paths = []
        # Profile fields (temps, humidity, heights, wind, 2 m, surface pressure, CAPE). A search the file can't
        # match (e.g. CAPE missing from ECMWF open data) just leaves that column out.
        for search in spec["fields"]:
            try:
                paths.append(h.download(search, verbose=False))
            except Exception:  # noqa: BLE001
                pass
        return window, h.download(re.escape(line), verbose=False), paths

    def decode(fxx, got):
        (a, b), p_path, f_paths = got
        p_vals = _values(open_grib(p_path)[0], points, scale)
        fvals = fl.point_fields([open_grib(p) for p in f_paths], points)
        for name in points:
            acc[name].append((a, b, p_vals[name][0]))
            extra[(name, b)] = {"grid_dist_km": p_vals[name][1], **fvals[name]}

    parallel_hours(download, decode, [int(f) for f in fxx_list], workers, label=f"{model.lower()} {init:%Y-%m-%d %H}z")

    t0 = pd.Timestamp(init)
    rows = []
    for name in points:
        for s, e, amt in deaccumulate(acc[name]):
            row = {"model": model, "init": t0, "lead_hr": e, "valid_start": t0 + pd.Timedelta(hours=s),
                   "valid_end": t0 + pd.Timedelta(hours=e), "point": name, "qpf_mm": amt, "source": "herbie"}
            row.update(extra.get((name, e), {}))
            rows.append(row)
    return pd.DataFrame(rows)


def pull_ensemble(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None,
                  workers: int = 8) -> pd.DataFrame:
    """One ensemble run (GEFS or ENS), precip only, one row per point x member x step (column `member`).

    GEFS keeps each member in its own file (c00 = control = member 0, p01..p30), so every (hour, member) pair is a
    separate tiny download; ECMWF ENS keeps all 50 perturbed members of a forecast hour in one file. Either way the
    downloads run in parallel threads and decoding happens one file at a time (fields.parallel_hours).
    """
    from herbie import Herbie

    spec = SPECS[model]
    kw = {"save_dir": save_dir} if save_dir else {}
    scale = 1000.0 if spec.get("precip_m") else 1.0
    acc: dict[tuple[str, int], list] = {}
    dist: dict[str, float] = {}
    fxx_list = [int(f) for f in fxx_list]

    if model == "ENS":
        def download(fxx):
            h = Herbie(init, fxx=fxx, verbose=False, **spec["herbie"], **kw)
            return h.download(spec["precip"], verbose=False)   # tp of every member: total since start

        def decode(fxx, path):
            for d in open_grib(path):
                mdim = "number" if "number" in d.dims else None
                for name, (v, dd) in _values(d, points, scale, member_dim=mdim).items():
                    dist[name] = dd
                    members = v if isinstance(v, dict) else {int(d["number"].values) if "number" in d.coords else 0: v}
                    for m, x in members.items():
                        acc.setdefault((name, m), []).append((0, fxx, x))

        items = fxx_list
    else:
        def download(item):
            fxx, m = item
            h = Herbie(init, fxx=fxx, member=m, verbose=False, **spec["herbie"], **kw)
            pick = choose_precip(list(h.inventory(spec["precip"])["search_this"]), fxx)
            if pick is None:
                raise RuntimeError(f"member {m}: no precip message ending at f{fxx:03d}")
            line, window = pick
            return window, h.download(re.escape(line), verbose=False)

        def decode(item, got):
            (a, b), path = got
            for name, (v, dd) in _values(open_grib(path)[0], points, scale).items():
                dist[name] = dd
                acc.setdefault((name, item[1]), []).append((a, b, v))

        items = [(f, m) for f in fxx_list for m in range(spec["members"])]

    parallel_hours(download, decode, items, workers, label=f"{model.lower()} {init:%Y-%m-%d %H}z")

    t0 = pd.Timestamp(init)
    rows = []
    for (name, m), recs in acc.items():
        for s, e, amt in deaccumulate(recs):
            rows.append({"model": model, "init": t0, "member": m, "lead_hr": e,
                         "valid_start": t0 + pd.Timedelta(hours=s), "valid_end": t0 + pd.Timedelta(hours=e),
                         "point": name, "grid_dist_km": dist.get(name), "qpf_mm": amt, "source": "herbie"})
    return pd.DataFrame(rows)
