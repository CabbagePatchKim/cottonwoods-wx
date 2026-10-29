"""Model winds turned from GRID-relative to EARTH-relative (true north), found 2026-10-29.

HRRR, NAM Nest, NAM (Lambert grids) and RDPS (rotated lat-lon) store u/v along their own grid axes (GRIB
uvRelativeToGrid = 1). Over the Cottonwoods those axes are turned 7-17 degrees from true east/north, so the archive's
700 mb wind directions for those models (and their SW/W/NW/N flow labels) were off by that much: against GFS/IFS
their directions read +16 (HRRR), +12 (NAM), +18 (RDPS) degrees clockwise. GFS, IFS, GDPS, GEFS and ENS are
regular lat-lon grids whose winds are already earth-relative.

GRIDS gives each grid's geometry; grid_angle at Alta is 8.80 deg for HRRR/NAM Nest, 7.03 for NAM/RAP and 16.90 for
RDPS, matching each real file's own lat/lon arrays (8.80, 7.01, 16.84, measured over one grid step). earth_winds
runs as the first step of every derivation and marks the run uv_earth, so a run is never turned twice when it is
derived again.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# model -> ("lambert", standard latitude, central longitude LoV) or ("rotated", north pole lat, north pole lon)
GRIDS = {
    "HRRR": ("lambert", 38.5, -97.5),
    "NAMNEST": ("lambert", 38.5, -97.5),
    "NAM": ("lambert", 25.0, -95.0),
    "RAP": ("lambert", 25.0, -95.0),
    # RDPS: GRIB south pole -31.758312 N, 267.597031 E (rotation 0) -> the rotated north pole is its antipode
    "RDPS": ("rotated", 31.758312, 87.597031),
}


def _unit(lat: float, lon: float) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    return np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def grid_angle(model: str, lat: float, lon: float) -> float:
    """Angle (deg, counterclockwise) from true east to the model grid's x axis at (lat, lon); 0 for earth-relative."""
    if model not in GRIDS:
        return 0.0
    kind, a, b = GRIDS[model]
    if kind == "lambert":                                    # tangent cone: n = sin(standard latitude)
        return float(np.sin(np.radians(a)) * (((b - lon + 180) % 360) - 180))
    p, pole = _unit(lat, lon), _unit(a, b)                   # rotated grid: x runs east around the rotated pole
    east = np.cross([0.0, 0.0, 1.0], p)
    north = np.cross(p, east)
    x = np.cross(pole, p)
    return float(np.degrees(np.arctan2(x @ north, x @ east)))


def earth_winds(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Turn the run's u_<lev>_ms / v_<lev>_ms to earth-relative at each point (its lat/lon from config points or the
    cell name), unless already done (uv_earth). Models with earth-relative grids are only marked."""
    if df.empty or "model" not in df.columns or ("uv_earth" in df.columns and df["uv_earth"].all()):
        return df
    out = df.copy()
    model = out["model"].iloc[0]
    pairs = [(c, "v" + c[1:]) for c in out.columns if c.startswith("u_") and c.endswith("_ms") and "v" + c[1:] in out]
    if model in GRIDS and pairs:
        where = {p: (v["lat"], v["lon"]) for p, v in cfg["points"].items()}
        ang = np.radians(out["point"].map(lambda p: grid_angle(model, *where[p]) if p in where else np.nan))
        if ang.isna().any():
            raise ValueError(f"{model}: no location for points {sorted(set(out.loc[ang.isna(), 'point']))}")
        cos, sin = np.cos(ang), np.sin(ang)
        for u, v in pairs:
            out[u], out[v] = out[u] * cos - out[v] * sin, out[u] * sin + out[v] * cos
    out["uv_earth"] = True
    return out
