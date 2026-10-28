"""Forecast archive: one file per model run, laid out so runs can be listed, filtered and compared.

    data/archive/forecasts/model=HRRR/init=2026110212.parquet
    data/archive/forecasts/model=GFS/init=2026110212.parquet
    data/archive/manifest.csv      (what was archived, how complete it was, when)

Each file holds one row per (valid step, point) with step QPF, the layer temperatures, and per-step Kuchera
snowfall. Files are never edited after the run is archived, so the archive is an honest record of what the
model said at each init time (which is what trend and verification work needs).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import modes, profile, regimes, slr
from . import cells as cellblend
from ..config import ARCHIVE_DIR
from ..models.registry import REGISTRY, check_cycle, get_puller
from .io import read_df, suffix_for, write_df

MIN_COMPLETE = 0.90  # a run missing more than 10% of its steps is not archived (cron retries it next time)


def forecast_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "forecasts"


# root is only passed by the tests (a temporary folder). Normal use leaves it None -> data/archive.
def run_path(model: str, init, cfg: dict, root: Path | None = None) -> Path:
    suffix = suffix_for(cfg["archive"]["format"])
    return forecast_dir(root) / f"model={model}" / f"init={pd.Timestamp(init):%Y%m%d%H}{suffix}"


def derive_run_fields(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Everything computed from a run's raw pulled fields: THE place to look when a derived number seems wrong.

    Steps, in order:
      0. cells.blend_points        coarse models (config model_cells): each resort point's pulled numbers become
                                   the bilinear blend of the 2x2 cells around it (cell rows untouched); cell_blend
      1. regimes.add_step_regimes  700 mb flow per step: flow_dir700, flow (SW/W/NW/N/S/E), thermal, regime
      2. modes.add_precip_modes    frontal / post-frontal / cellular / lake-effect per step: precip_mode, front_time
      3. add_snowfall              station profile + wet-bulb snow/mix/rain + Kuchera: snow_in, snow_level_m, ...
    Steps 1-3 add columns and leave the pulled ones alone. Ensembles are archived as precip only (no
    temperatures), so they get step 0 (blended precip per member) and nothing else.
    """
    df = cellblend.blend_points(df, cfg)
    if not _temp_cols(df):
        return df
    df = regimes.add_step_regimes(df, cfg["regimes"])
    df = modes.add_precip_modes(df, cfg["regimes"])
    return add_snowfall(df, cfg)


def _temp_cols(df: pd.DataFrame) -> dict[int, str]:
    """{level hPa: column} for the t_<level>_k temperature columns."""
    return {int(c.split("_")[1]): c for c in df.columns if c.startswith("t_") and c.endswith("_k")}


def add_snowfall(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Add tmax_k, slr_kuchera and snow_in (per step) from the t_<level>_k columns.

    When a run has the profile fields (t2m_k, td2m_k, rh_<lev>, z_<lev>_m), each step is first moved to the
    station's real elevation (cottonwoods.profile): Kuchera uses the warmest layer from the STATION up, and the
    wet-bulb at the station decides snow / mix / rain. snow_in = water x Kuchera ratio x snow fraction.
    snow_in_raw keeps plain Kuchera (model column, no phase) for comparison. Older runs without those fields get
    plain Kuchera only. Ensembles are archived as precip only (no temperatures), so they are returned unchanged.
    """
    kcfg = cfg["kuchera"]
    # map level -> column, put the levels in order, then Kuchera per row
    tcols = _temp_cols(df)
    if not tcols:
        return df
    temps = df[[tcols[lev] for lev in sorted(tcols)]].copy()
    temps.columns = sorted(tcols)
    out = df.copy()  # don't modify the caller's DataFrame
    psfc = df["psfc_hpa"] if "psfc_hpa" in df.columns else None  # older archives may lack it
    out["tmax_k"] = slr.layer_tmax(temps, kcfg["bottom_level_hpa"], kcfg["top_level_hpa"], psfc)
    out["slr_kuchera"] = slr.kuchera_slr(out["tmax_k"].to_numpy(), kcfg["slr_floor"], kcfg["slr_cap"])
    out["snow_in"] = slr.qpf_mm_to_in(out["qpf_mm"]) * out["slr_kuchera"]
    if not {"t2m_k", "td2m_k"} <= set(df.columns):
        return out

    pcfg = cfg["precip_type"]
    elev = {p: v["elev_ft"] * 0.3048 for p, v in cfg["points"].items() if "elev_ft" in v}
    prof = pd.DataFrame([
        profile.station_profile(r, sorted(tcols), elev.get(r["point"]), pcfg, kcfg["top_level_hpa"])
        for r in df.to_dict("records")
    ], index=df.index)
    if prof.empty or "tmax_stn_k" not in prof:
        return out
    out = out.join(prof)
    have = out["tmax_stn_k"].notna()
    out["snow_in_raw"] = out["snow_in"]
    out.loc[have, "tmax_k"] = out.loc[have, "tmax_stn_k"]
    out.loc[have, "slr_kuchera"] = slr.kuchera_slr(out.loc[have, "tmax_k"].to_numpy(), kcfg["slr_floor"], kcfg["slr_cap"])
    # When the wet-bulb says snow but warm air drives Kuchera to ~0, the snow is wet and dense, not absent:
    # any step with snow in it gets at least snow_min_slr, 3:1 (until the Veals snow-ratio model replaces Kuchera)
    snowy = have & (out["snow_frac"] > 0)
    out.loc[snowy, "slr_kuchera"] = out.loc[snowy, "slr_kuchera"].clip(lower=pcfg["snow_min_slr"])
    out.loc[have, "snow_in"] = (slr.qpf_mm_to_in(out.loc[have, "qpf_mm"]) * out.loc[have, "slr_kuchera"]
                                * out.loc[have, "snow_frac"])
    return out


def run_points(model: str, cfg: dict) -> dict[str, dict]:
    """Points to extract for this model: the configured points, plus for coarse models the 2x2 block of grid
    points around the Cottonwoods (config model_cells; older configs: gfs_cells for GFS only)."""
    points = dict(cfg["points"])
    cells = cellblend.model_cells(model, cfg)
    if cells:
        for lat in cells["lats"]:
            for lon in cells["lons"]:
                points[cellblend.cell_name(model, lat, lon)] = {"lat": lat, "lon": lon}
    return points


def _append_manifest(row: dict, root: Path | None) -> None:
    """Append one line to manifest.csv. Several stream processes can finish runs at the same moment, so the
    write is guarded by a small lock file (created exclusively; others wait their turn)."""
    import os
    import time

    path = (root or ARCHIVE_DIR) / "manifest.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name("manifest.lock")
    for _ in range(300):                       # up to ~30 s
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        fd = None                              # stale lock left by a crash: write anyway
    try:
        new = pd.DataFrame([row])
        if path.exists():
            new = pd.concat([pd.read_csv(path), new], ignore_index=True)
        new.to_csv(path, index=False)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock)
        except OSError:
            pass


def archive_run(
    model: str,
    init: datetime,
    cfg: dict,
    root: Path | None = None,
    force: bool = False,
    save_dir: str | None = None,
    puller=None,
) -> tuple[Path, str]:
    """Archive one model run. Returns (path, status) with status 'exists' or 'archived'.

    Raises RuntimeError if nothing came back or the run is too incomplete (usually: not fully posted yet).
    """
    # Already archived? Skip it, which is what makes re-running cron safe.
    check_cycle(init, cfg, model)  # only 00/06/12/18z runs, for every model (GDPS: 00/12z)
    path = run_path(model, init, cfg, root)
    if path.exists() and not force:
        return path, "exists"

    spec = REGISTRY[model]
    fxx = spec.fxx_list(init, cfg)
    # Tests pass a fake `puller` so no download happens; normally we look up the real one for this model.
    pull = puller or get_puller(model, cfg)
    points = run_points(model, cfg)
    df = pull(init, fxx, points, save_dir)
    if df is None or df.empty:
        raise RuntimeError(f"{model} {init:%Y-%m-%d %H}z: nothing retrieved (run not posted yet, or network/Herbie problem)")

    # Count the steps that actually came back vs. what a full run has (hours x points, x members for ensembles).
    expected = len(fxx) * len(points) * max(spec.members, 1)
    valid = int(df["qpf_mm"].notna().sum())
    if valid / expected < MIN_COMPLETE:
        raise RuntimeError(
            f"{model} {init:%Y-%m-%d %H}z: only {valid}/{expected} steps retrieved "
            f"(likely still posting); not archived, will retry"
        )

    df = derive_run_fields(df, cfg)
    df["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    write_df(df, path)
    _append_manifest(
        {"model": model, "init": f"{pd.Timestamp(init):%Y-%m-%d %H:%M}", "n_expected": expected, "n_valid": valid,
         "fetched_at": f"{df['fetched_at'].iloc[0]:%Y-%m-%d %H:%M}", "path": str(path)},
        root,
    )
    return path, "archived"


def _init_from_name(p: Path) -> pd.Timestamp:
    # "init=2026110212.parquet" -> stem "init=2026110212" -> "2026110212" -> 2026-11-02 12:00
    return pd.to_datetime(p.stem.split("=")[1], format="%Y%m%d%H")


def load_forecasts(cfg: dict | None = None, models=None, start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    """Read archived runs (optionally filtered by model and init-time range) into one frame."""
    base = forecast_dir(root)
    if not base.exists():
        return pd.DataFrame()
    # glob finds files matching a wildcard pattern. Filtering on file NAMES before reading means we only open
    # the runs we need, which matters once the archive holds thousands of runs.
    files = [p for p in base.glob("model=*/init=*.*") if p.suffix in (".parquet", ".csv")]
    if models:
        want = {m.upper() for m in models}
        files = [p for p in files if p.parent.name.split("=")[1].upper() in want]
    if start is not None:
        files = [p for p in files if _init_from_name(p) >= pd.Timestamp(start)]
    if end is not None:
        files = [p for p in files if _init_from_name(p) <= pd.Timestamp(end)]
    frames = [read_df(p) for p in sorted(files)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
