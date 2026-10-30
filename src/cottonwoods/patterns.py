"""PATTERN archive (build step 5b, owner approved 2026-10-27, built 2026-10-29): coarse regional maps of the long-range
models, so troughs and ridges can be compared between models and runs (the owner's 3-7 day "trend/convergence"
method), and later labelled and scored. The point archive (archive/forecasts.py) is untouched by this.

Per run, every 6 h, two grids (column `grid`; config `patterns`):
    "1deg"    out to 240 h, 25-60 N x 150-90 W (Pacific troughs to the Plains), every model AREA-AVERAGED onto the
              same 1 degree grid (area_mean), so models compare like for like
    "native"  out to 120 h, 35-45 N x 120-105 W (around Utah), each model's own grid points, NOT averaged (owner,
              2026-10-29: shortwaves matter, and 1 deg smooths features under ~200-300 km and lowers their peaks)
Fields:
    z500_m     500 mb height             vort500_s  500 mb RELATIVE vorticity (GFS's absolute vorticity minus f)
    u250_ms, v250_ms  250 mb wind (jet)  mslp_hpa   sea-level pressure        t850_k     850 mb temperature
GEFS adds the ensemble spread of each (<field>_sd) next to its mean; GEFS has no vorticity. Sources (checked
2026-10-29), native resolution and download per step: GFS 0.25 deg ~2.5 MB (NCEP's own 1 deg file is interpolated,
not averaged: its vorticity matched IFS at r 0.37 vs 0.57 for 0.25 averaged), GEFS mean + spread 0.5 deg ~1 MB,
IFS 0.25 deg ~3 MB (00/12z only, to spare ECMWF's server, which throttled us 10/28). Not yet: ECMWF ENS (open data
has members only, no mean/spread: ~150 MB a step to compute) and GDPS (one global file per field and step, ~1 GB a
run; its GeoMet server could cut the box server-side). Stored as data/archive/patterns/model=GFS/init=2026102900
.parquet, one row per grid x step x point, float32, ~4.5 MB a run.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .archive.io import read_df, suffix_for, write_df
from .config import ARCHIVE_DIR

OMEGA = 7.2921e-5                                  # Earth's rotation (1/s), for the Coriolis parameter f
NCEP = r":(HGT|ABSV):500 mb:|:(UGRD|VGRD):250 mb:|:PRMSL:|:TMP:850 mb:"
SPECS = {   # model -> its Herbie settings per product ("" = the field itself, "_sd" = ensemble spread)
    "GFS": {"": {"model": "gfs", "product": "pgrb2.0p25"}, "search": NCEP},
    "GEFS": {"": {"model": "gefs", "product": "atmos.5", "member": "avg"},
             "_sd": {"model": "gefs", "product": "atmos.5", "member": "spr"}, "search": NCEP},
    "IFS": {"": {"model": "ifs", "product": "oper", "resolution": "0p25"},
            "search": r":(gh|vo):500:pl|:(u|v):250:pl|:msl:|:t:850:pl"},
}
COLUMNS = {"gh": "z500_m", "absv": "vort500_s", "vo": "vort500_s", "u": "u250_ms", "v": "v250_ms",
           "prmsl": "mslp_hpa", "msl": "mslp_hpa", "t": "t850_k"}
SCALE = {"mslp_hpa": 0.01}                          # Pa -> hPa


def pattern_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "patterns"


def pattern_path(model: str, init, cfg: dict, root: Path | None = None) -> Path:
    return pattern_dir(root) / f"model={model}" / f"init={pd.Timestamp(init):%Y%m%d%H}{suffix_for(cfg['archive']['format'])}"


def steps(cfg: dict) -> list[int]:
    p = cfg["patterns"]
    return list(range(p["step_hours"], p["max_hours"] + 1, p["step_hours"]))


def area_mean(vals: np.ndarray, res: float, step: float) -> np.ndarray:
    """Each point's average over the step x step cell centred on it (edge points of the cell weighted half), so a finer
    grid (IFS 0.25, GEFS 0.5 deg) is compared like for like with GFS's 1 deg: sampling every 4th IFS point kept its
    small-scale vorticity noise (GFS vs IFS 500 mb vorticity correlated 0.29, heights 0.997)."""
    n = int(round(step / res))
    if n <= 1:
        return vals
    k = np.ones(n + 1)
    k[[0, -1]] = 0.5
    k /= k.sum()
    vals = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 0, vals)
    return np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, vals)


def _frame(var: str, vals: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> tuple[str, pd.DataFrame]:
    """(column, long frame lat/lon/value) for one field's values on the given grid rows/columns, in our units."""
    col = COLUMNS[var]
    vals = vals * SCALE.get(col, 1.0)
    if var == "absv":                                       # absolute -> relative vorticity
        vals = vals - 2 * OMEGA * np.sin(np.radians(lats))[:, None]
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return col, pd.DataFrame({"lat": la.ravel().round(2), "lon": lo.ravel().round(2), col: vals.ravel()})


def cut_box(ds, pcfg: dict) -> dict[str, pd.DataFrame]:
    """{column: long frame (lat, lon, value)} of each field in one decoded dataset, inside the box, on the grid_deg
    grid (each value the area mean of its grid_deg cell; longitudes as -180..180)."""
    lat = ds["latitude"].values
    lon = ((ds["longitude"].values + 180) % 360) - 180
    (la0, la1), (lo0, lo1), step = pcfg["box"]["lat"], pcfg["box"]["lon"], pcfg["grid_deg"]
    on = lambda x: np.isclose(np.mod(x, step), 0) | np.isclose(np.mod(x, step), step)  # noqa: E731
    yi = np.where((lat >= la0) & (lat <= la1) & on(lat))[0]
    xi = np.where((lon >= lo0) & (lon <= lo1) & on(lon))[0]
    res = abs(float(lat[1] - lat[0]))
    pad = int(round(step / res))                            # the cells' edges reach this many points past the box
    ys = slice(max(yi.min() - pad, 0), yi.max() + pad + 1)
    xs = slice(max(xi.min() - pad, 0), xi.max() + pad + 1)
    out = {}
    for var in [v for v in ds.data_vars if v in COLUMNS]:
        sub = area_mean(ds[var].values[ys, xs].astype("float64"), res, step)
        col, df = _frame(var, sub[np.ix_(yi - ys.start, xi - xs.start)], lat[yi], lon[xi])
        out[col] = df
    return out


def native_box(ds, icfg: dict) -> dict[str, pd.DataFrame]:
    """Like cut_box, for the inner box at the model's own grid points (no averaging), so shortwaves keep their size
    and strength."""
    lat = ds["latitude"].values
    lon = ((ds["longitude"].values + 180) % 360) - 180
    (la0, la1), (lo0, lo1) = icfg["box"]["lat"], icfg["box"]["lon"]
    yi = np.where((lat >= la0) & (lat <= la1))[0]
    xi = np.where((lon >= lo0) & (lon <= lo1))[0]
    out = {}
    for var in [v for v in ds.data_vars if v in COLUMNS]:
        col, df = _frame(var, ds[var].values[np.ix_(yi, xi)].astype("float64"), lat[yi], lon[xi])
        out[col] = df
    return out


def pull_pattern(model: str, init: datetime, cfg: dict, save_dir: str | None = None, workers: int = 4,
                 throttle: dict | None = None) -> pd.DataFrame:
    """One run's pattern maps: rows (grid, lead_hr, lat, lon), the fields (COLUMNS' names, + _sd spreads for GEFS) as float32."""
    from herbie import Herbie  # lazy: offline tests don't need herbie
    from .models.fields import open_grib, parallel_hours

    spec, pcfg = SPECS[model], cfg["patterns"]
    kinds = [k for k in spec if k != "search"]
    kw = {"save_dir": save_dir} if save_dir else {}
    got: dict[tuple[int, str], dict] = {}

    def download(item):
        fxx, kind = item
        return Herbie(init, fxx=fxx, verbose=False, **spec[kind], **kw).download(spec["search"], verbose=False)

    def decode(item, path):
        grids = {"1deg": {}, "native": {}}
        for ds in open_grib(path):
            grids["1deg"].update(cut_box(ds, pcfg))
            if item[0] <= pcfg["inner"]["max_hours"]:
                grids["native"].update(native_box(ds, pcfg["inner"]))
        got[item] = grids

    parallel_hours(download, decode, [(f, k) for f in steps(cfg) for k in kinds], workers,
                   label=f"{model.lower()} pattern {init:%Y-%m-%d %H}z", throttle=throttle)
    frames = []
    for fxx in steps(cfg):
        for grid in ("1deg", "native"):
            parts = [df.rename(columns={c: c + kind for c in df.columns if c not in ("lat", "lon")}).set_index(["lat", "lon"])
                     for kind in kinds for df in got.get((fxx, kind), {}).get(grid, {}).values()]
            if parts:
                frames.append(pd.concat(parts, axis=1).reset_index().assign(grid=grid, lead_hr=fxx))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    num = [c for c in df.columns if c not in ("grid", "lat", "lon", "lead_hr")]
    df[num] = df[num].astype("float32")
    return df[["grid", "lead_hr", "lat", "lon", *num]]


def archive_pattern(model: str, init: datetime, cfg: dict, root: Path | None = None, puller=None,
                    **pull_kw) -> tuple[Path, str]:
    """Archive one run's pattern maps unless already there. Returns (path, 'exists' | 'archived'). Raises
    RuntimeError if less than 90% of the 1 deg steps came back (usually still posting: the next pass tries again)."""
    path = pattern_path(model, init, cfg, root)
    if path.exists():
        return path, "exists"
    df = (puller or pull_pattern)(model, init, cfg, **pull_kw)
    want = len(steps(cfg))
    have = df.loc[df["grid"] == "1deg", "lead_hr"].nunique() if not df.empty else 0
    if have < 0.9 * want:
        raise RuntimeError(f"{model} {init:%Y-%m-%d %H}z pattern: {have}/{want} steps (likely still posting)")
    write_df(df.assign(init=pd.Timestamp(init)), path)
    return path, "archived"


def load_pattern(model: str, init, cfg: dict, root: Path | None = None, grid: str | None = None) -> pd.DataFrame:
    """One archived run's maps; grid "1deg" or "native" for just that grid."""
    path = pattern_path(model, init, cfg, root)
    df = read_df(path) if path.exists() else pd.DataFrame()
    return df[df["grid"] == grid] if grid and not df.empty else df
